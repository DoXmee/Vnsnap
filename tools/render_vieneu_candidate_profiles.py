from __future__ import annotations

import argparse
import gc
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import torch
from vieneu import Vieneu
from vieneu_utils.phonemize_text import phonemize_batch

from render_vieneu_srt import (
    FFMPEG,
    analyze_asr_match,
    audio_duration,
    clean_text,
    load_asr_model,
    load_pack,
    load_ref_codes_for_pack,
    parse_srt,
    prepare_text_for_tts,
    resolve_lora_dir_for_render,
    transcribe_audio,
    trim_edges,
)


@dataclass(frozen=True)
class CandidateProfile:
    """One known-good checkpoint-10000 render profile recovered from approved demos."""

    name: str
    temperature: float
    top_k: int
    head_trim_sec: float = 0.32
    tail_remove_sec: float = 0.0
    tail_pad_sec: float = 0.0
    max_chars: int = 120
    seed_offset: int = 0
    note: str = ""


PROFILES = [
    CandidateProfile("base_notail", 0.55, 25, tail_remove_sec=0.0, seed_offset=0, note="base profile, no tail cut"),
    CandidateProfile("base_seed1_notail", 0.55, 25, tail_remove_sec=0.0, seed_offset=1, note="base no-tail alternate seed 1"),
    CandidateProfile("base_seed2_notail", 0.55, 25, tail_remove_sec=0.0, seed_offset=2, note="base no-tail alternate seed 2"),
    CandidateProfile("base_low_notail", 0.52, 22, tail_remove_sec=0.0, seed_offset=6, note="lower temperature no-tail"),
    CandidateProfile("base_high_notail", 0.58, 30, tail_remove_sec=0.0, seed_offset=7, note="higher temperature no-tail"),
    CandidateProfile("base_high_tail003", 0.58, 30, tail_remove_sec=0.03, seed_offset=7, note="higher temperature with safe 0.03s micro-tail cleanup"),
    CandidateProfile("base_high_tail006", 0.58, 30, tail_remove_sec=0.06, seed_offset=7, note="higher temperature with stronger 0.06s micro-tail cleanup"),
    CandidateProfile("base_high_tail035", 0.58, 30, tail_remove_sec=0.35, seed_offset=7, note="user-approved strong 0.35s generated-tail cleanup"),
    CandidateProfile("base_high_tail045", 0.58, 30, tail_remove_sec=0.45, seed_offset=7, note="higher temperature with strong 0.45s generated-tail cleanup"),
    CandidateProfile("tail000", 0.55, 25, tail_remove_sec=0.0, tail_pad_sec=0.12, seed_offset=1, note="short cue no tail cut plus padding for final syllables that sound clipped"),
    CandidateProfile("tail003", 0.55, 25, tail_remove_sec=0.03, seed_offset=1, note="short cue light tail cleanup requested by user"),
    CandidateProfile("tail005", 0.55, 25, tail_remove_sec=0.05, seed_offset=2, note="approved cue2-style light tail cleanup"),
    CandidateProfile("tail014", 0.55, 25, tail_remove_sec=0.14, seed_offset=3, note="approved cue3-style tail cleanup"),
    CandidateProfile("long_notail", 0.58, 30, tail_remove_sec=0.0, seed_offset=4, note="approved long sentence profile"),
    CandidateProfile("long_tail014", 0.58, 30, tail_remove_sec=0.14, seed_offset=5, note="approved cue4 seedB-like long profile"),
    CandidateProfile("seedA_tail014", 0.52, 22, tail_remove_sec=0.14, seed_offset=6, note="recovered cue4 seedA profile"),
    CandidateProfile("seedB_tail014", 0.58, 30, tail_remove_sec=0.14, seed_offset=7, note="recovered cue4 seedB profile"),
]

PROFILE_BY_NAME = {profile.name: profile for profile in PROFILES}

QUALITY_PROFILE_NAMES = [
    "base_notail",
    "base_seed2_notail",
    "base_high_notail",
    "base_high_tail035",
    "tail000",
    "tail005",
    "long_notail",
    "long_tail014",
]


def quality_profiles_for_text(text: str) -> list[CandidateProfile]:
    """Return a compact candidate set for quality-first single-line rendering."""
    names = list(QUALITY_PROFILE_NAMES)
    token = last_text_token(text)
    words = text_units(text)
    if token == "tiết":
        names = ["tail000", "base_notail", "base_high_notail", "long_tail014"]
    elif token in {"thôi", "phòng", "trống", "đồ"}:
        names = ["tail005", "base_notail", "base_seed2_notail", "base_high_notail", "tail000"]
    elif words >= 10:
        names = ["base_notail", "base_high_notail", "long_notail", "long_tail014", "tail005"]
    seen: set[str] = set()
    return [PROFILE_BY_NAME[name] for name in names if name in PROFILE_BY_NAME and not (name in seen or seen.add(name))]


def hidden_subprocess_kwargs() -> dict:
    """Return Windows flags that prevent FFmpeg/Python helper console windows."""
    if not sys.platform.startswith("win"):
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def approved_short_profile_name(pack: dict) -> str:
    """Return the user-approved short-cue profile stored in the voice pack."""
    approved_profile = pack.get("candidateApprovedProfile", {}) if isinstance(pack, dict) else {}
    name = str(approved_profile.get("shortCueProfile", "tail003") or "tail003")
    return name if name in PROFILE_BY_NAME else "tail003"


def load_lines(args: argparse.Namespace) -> list[str]:
    """Load text lines from SRT, plain text file, or direct text."""
    if args.srt:
        return [str(item["text"]).strip() for item in parse_srt(args.srt) if str(item["text"]).strip()]
    if args.text_file:
        return [line.strip() for line in args.text_file.read_text(encoding="utf-8-sig", errors="replace").splitlines() if line.strip()]
    if args.text:
        return [args.text.strip()]
    return []


def remove_tail(path: Path, seconds: float) -> None:
    """Remove a tiny generated tail artifact without touching the beginning."""
    if seconds <= 0:
        return
    duration = audio_duration(path)
    if duration <= seconds + 0.35:
        return
    tmp = path.with_name(path.stem + "_tail_tmp" + path.suffix)
    filters = (
        f"areverse,atrim=start={seconds:.3f},"
        "afade=t=in:st=0:d=0.045,areverse,"
        "apad=pad_dur=0.100,alimiter=limit=0.92"
    )
    subprocess.run(
        [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), "-af", filters, str(tmp)],
        check=True,
        **hidden_subprocess_kwargs(),
    )
    tmp.replace(path)


def cleanup_tail_artifact(path: Path, seconds: float = 0.20, fade_sec: float = 0.035, pad_sec: float = 0.120) -> None:
    """Remove a short generated artifact after the final word, then add safe silence padding."""
    seconds = max(0.0, float(seconds or 0.0))
    if seconds <= 0:
        return
    duration = audio_duration(path)
    if duration <= seconds + 0.65:
        return
    tmp = path.with_name(path.stem + "_tail_clean_tmp" + path.suffix)
    filters = (
        f"areverse,atrim=start={seconds:.3f},"
        f"afade=t=in:st=0:d={max(0.0, fade_sec):.3f},areverse,"
        f"apad=pad_dur={max(0.0, pad_sec):.3f},alimiter=limit=0.92"
    )
    subprocess.run(
        [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), "-af", filters, str(tmp)],
        check=True,
        **hidden_subprocess_kwargs(),
    )
    tmp.replace(path)


def should_quality_tail_clean(text: str, profile_name: str) -> bool:
    """Return True when a selected quality candidate is known to leave a generated tail artifact."""
    if profile_name not in {"base_notail", "base_high_notail"}:
        return False
    return text_units(text) >= 8


def finalize_candidate_edges(path: Path, head_sec: float, tail_sec: float) -> None:
    """Apply head trim and generated-tail cleanup in a single FFmpeg pass."""
    head_sec = max(0.0, float(head_sec or 0.0))
    tail_sec = max(0.0, float(tail_sec or 0.0))
    if head_sec <= 0 and tail_sec <= 0:
        return
    duration = audio_duration(path)
    if duration <= head_sec + tail_sec + 0.35:
        return
    tmp = path.with_name(path.stem + "_edge_tail_tmp" + path.suffix)
    filters: list[str] = []
    if head_sec > 0:
        filters.append(f"atrim=start={head_sec:.3f},asetpts=N/SR/TB")
    if tail_sec > 0:
        filters.extend([
            "areverse",
            f"atrim=start={tail_sec:.3f}",
            "afade=t=in:st=0:d=0.045",
            "areverse",
            "apad=pad_dur=0.100",
            "alimiter=limit=0.92",
        ])
    subprocess.run(
        [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), "-af", ",".join(filters), str(tmp)],
        check=True,
        **hidden_subprocess_kwargs(),
    )
    tmp.replace(path)


def finalize_audio_array(audio, sample_rate: int, head_sec: float, tail_sec: float, tail_pad_sec: float = 0.0):
    """Apply the approved head/tail cleanup directly on the waveform before saving."""
    arr = np.asarray(audio).copy()
    sample_rate = int(sample_rate or 24000)
    head_sec = max(0.0, float(head_sec or 0.0))
    tail_sec = max(0.0, float(tail_sec or 0.0))
    head = int(round(head_sec * sample_rate))
    tail = int(round(tail_sec * sample_rate))
    min_keep = int(round(0.35 * sample_rate))
    if arr.shape[0] > head + tail + min_keep:
        end = arr.shape[0] - tail if tail > 0 else arr.shape[0]
        arr = arr[head:end]
    elif head > 0 and arr.shape[0] > head + min_keep:
        arr = arr[head:]
    if tail > 0 and arr.shape[0] > min_keep:
        fade = min(int(round(0.045 * sample_rate)), max(1, arr.shape[0]))
        ramp = np.linspace(1.0, 0.0, fade, dtype=np.float32)
        if arr.ndim == 1:
            arr[-fade:] = arr[-fade:] * ramp
        else:
            arr[-fade:] = arr[-fade:] * ramp.reshape(-1, *([1] * (arr.ndim - 1)))
        pad_shape = list(arr.shape)
        pad_shape[0] = int(round(0.100 * sample_rate))
        arr = np.concatenate([arr, np.zeros(pad_shape, dtype=arr.dtype)], axis=0)
        arr = np.clip(arr, -0.92, 0.92)
    elif tail_pad_sec > 0 and arr.shape[0] > min_keep:
        pad_shape = list(arr.shape)
        pad_shape[0] = int(round(float(tail_pad_sec) * sample_rate))
        arr = np.concatenate([arr, np.zeros(pad_shape, dtype=arr.dtype)], axis=0)
    return arr


def text_units(text: str) -> int:
    """Count simple text tokens."""
    return len([part for part in re.split(r"\s+", clean_text(text)) if part])


def last_text_token(text: str) -> str:
    """Return the last readable token for production profile rules."""
    normalized = re.sub(r"[^\wÀ-ỹ]+", " ", clean_text(text).lower(), flags=re.UNICODE).strip()
    parts = [part for part in normalized.split() if part]
    return parts[-1] if parts else ""


def text_similarity(expected: str, actual: str) -> float:
    """Return a robust fallback text similarity score."""
    expected_norm = " ".join(clean_text(expected).lower().split())
    actual_norm = " ".join(clean_text(actual).lower().split())
    if not expected_norm or not actual_norm:
        return 0.0
    return SequenceMatcher(None, expected_norm, actual_norm).ratio()


def score_candidates_with_asr(text: str, candidates: list[dict], asr_model) -> None:
    """Annotate candidates with ASR transcript and similarity score."""
    if asr_model is None:
        return
    for item in candidates:
        if "error" in item:
            continue
        path = Path(str(item["file"]))
        if not path.exists():
            continue
        asr_text = transcribe_audio(asr_model, path)
        match = analyze_asr_match(text, asr_text) if asr_text else {"coverage": 0.0, "ok": False}
        sim = text_similarity(text, asr_text)
        coverage = float(match.get("coverage", 0.0) or 0.0)
        item["asrText"] = asr_text
        item["asrCoverage"] = round(coverage, 4)
        item["asrSimilarity"] = round(sim, 4)
        item["asrOk"] = bool(match.get("ok", False))


def pick_default_profile(text: str, candidates: list[dict], pack: dict | None = None) -> dict:
    """Pick a default best candidate using recovered approved-profile rules.

    This is intentionally conservative. It does not delete candidates; it only chooses
    what to copy as cau_XXXX.mp3. The user can inspect every candidate folder.
    """
    words = text_units(text)
    char_count = len(re.sub(r"\s+", "", text or ""))
    min_safe_duration = max(0.65, min(8.0, max(words / 5.0, char_count / 48.0)))
    expected_duration = max(min_safe_duration, words / 3.2, char_count / 34.0)
    duration_safe = [
        row for row in candidates
        if float(row.get("duration", 0.0) or 0.0) >= min_safe_duration
    ]
    selectable = duration_safe or candidates
    has_break = bool(re.search(r"[,;:]", text))
    short_profile = approved_short_profile_name(pack or {})
    preferred = "long_notail" if words >= 12 or has_break else short_profile
    if re.search(r"\b(chi tiết|thôi|phòng|tiết)\b", text, flags=re.IGNORECASE):
        preferred = "tail014"
    asr_scored = [row for row in selectable if "asrSimilarity" in row]
    if asr_scored:
        words = text_units(text)
        preferred_profiles = [short_profile, "tail005", "tail014", "base_notail"] if words <= 6 else ["long_notail", "tail014", "long_tail014", "seedB_tail014", "seedA_tail014"]
        def profile_rank(name: str) -> int:
            try:
                return len(preferred_profiles) - preferred_profiles.index(name)
            except ValueError:
                return 0

        return max(
            asr_scored,
            key=lambda row: (
                float(row.get("asrSimilarity", 0.0)),
                float(row.get("asrCoverage", 0.0)),
                0 if re.search(r"(^|\\s)(ư|ừ|ờ|um|uhm)($|\\s)", str(row.get("asrText", "")).lower()) else 1,
                profile_rank(str(row.get("profile", ""))),
                -abs(float(row.get("duration", 0.0)) - expected_duration),
            ),
        )
    for item in selectable:
        if item["profile"] == preferred:
            return item
    preferred_order = [short_profile, "tail005", "tail014", "base_notail", "base_high_tail035", "long_notail", "long_tail014"]
    def fallback_rank(row: dict) -> tuple:
        name = str(row.get("profile", ""))
        try:
            profile_score = len(preferred_order) - preferred_order.index(name)
        except ValueError:
            profile_score = 0
        duration = float(row.get("duration", 0.0) or 0.0)
        return (profile_score, -abs(duration - expected_duration), duration)
    return max(selectable, key=fallback_rank)


def rescue_short_selection(text: str, selected: dict, candidates: list[dict]) -> dict:
    """Replace an obviously truncated selected candidate with a duration-safe one."""
    words = text_units(text)
    char_count = len(re.sub(r"\s+", "", text or ""))
    min_safe_duration = max(0.65, min(8.0, max(words / 5.0, char_count / 48.0)))
    selected_duration = float(selected.get("duration", 0.0) or 0.0)
    if selected_duration >= min_safe_duration:
        return selected
    valid = [
        row for row in candidates
        if "error" not in row
        and Path(row.get("file", "")).exists()
        and float(row.get("duration", 0.0) or 0.0) >= min_safe_duration
    ]
    if not valid:
        return selected
    expected_duration = max(min_safe_duration, words / 3.2, char_count / 34.0)
    replacement = max(valid, key=lambda row: (-abs(float(row.get("duration", 0.0) or 0.0) - expected_duration), float(row.get("duration", 0.0) or 0.0)))
    replacement["replacedTruncatedProfile"] = selected.get("profile")
    replacement["truncatedDuration"] = round(selected_duration, 3)
    replacement["minSafeDuration"] = round(min_safe_duration, 3)
    return replacement


def fast_profile_for_text(text: str, pack: dict, force_profile: str = "") -> CandidateProfile:
    """Choose one production profile for a text line without rendering candidates."""
    if force_profile:
        return PROFILE_BY_NAME.get(force_profile, PROFILE_BY_NAME[approved_short_profile_name(pack)])
    words = text_units(text)
    has_break = bool(re.search(r"[,;:]", text))
    if bool(pack.get("candidateAvoidTailCut", False)):
        token = last_text_token(text)
        if token in {"này", "phòng"}:
            return PROFILE_BY_NAME["base_high_tail035"]
        if token == "thôi":
            return PROFILE_BY_NAME["tail005"]
        if token == "tiết":
            return PROFILE_BY_NAME["tail000"]
        if token == "trước":
            return PROFILE_BY_NAME["seedA_tail014"]
        if token == "đồ":
            return PROFILE_BY_NAME["tail014"]
        if token == "hẳn":
            return PROFILE_BY_NAME["long_notail"]
        if token == "khuất":
            return PROFILE_BY_NAME["long_tail014"]
        if token == "trống":
            return PROFILE_BY_NAME["base_seed2_notail"]
        if words >= 12 or has_break:
            return PROFILE_BY_NAME["long_notail"]
        if words >= 9:
            return PROFILE_BY_NAME["base_notail"]
        short_profile = PROFILE_BY_NAME.get(approved_short_profile_name(pack), PROFILE_BY_NAME["base_high_notail"])
        max_micro_tail = float(pack.get("candidateMaxMicroTailCutSec", 0.04) or 0.0)
        return short_profile if short_profile.tail_remove_sec <= max_micro_tail else PROFILE_BY_NAME["base_high_notail"]
    if words >= 12 or has_break:
        return PROFILE_BY_NAME["long_notail"]
    if re.search(r"\b(chi tiáº¿t|thÃ´i|phÃ²ng|tiáº¿t)\b", text, flags=re.IGNORECASE):
        return PROFILE_BY_NAME["tail014"]
    return PROFILE_BY_NAME[approved_short_profile_name(pack)]


def render_candidate(tts, ref_codes, pack: dict, text: str, profile: CandidateProfile, out_file: Path) -> float:
    """Render one candidate profile."""
    prepared = prepare_text_for_tts(tts, text)
    text_seed = int(hashlib.sha256(clean_text(text).encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % 100000
    seed = int(pack.get("candidateSeedBase", 1729)) + text_seed + int(profile.seed_offset)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    max_new_tokens = int(pack.get("candidateSingleMaxNewTokens", 0) or 0)
    if bool(pack.get("candidateUseSingleInferBatch", False)) and max_new_tokens > 0:
        audio = tts.infer_batch(
            [prepared],
            ref_codes=ref_codes,
            ref_text=pack.get("refText", ""),
            temperature=profile.temperature,
            top_k=profile.top_k,
            skip_normalize=True,
            apply_watermark=False,
            max_new_tokens=max_new_tokens,
            min_new_tokens=int(pack.get("candidateSingleMinNewTokens", 40) or 40),
        )[0]
    else:
        audio = tts.infer(
            text=prepared,
            ref_codes=ref_codes,
            ref_text=pack.get("refText", ""),
            max_chars=profile.max_chars,
            temperature=profile.temperature,
            top_k=profile.top_k,
            skip_normalize=True,
            apply_watermark=False,
        )
    sample_rate = int(getattr(tts, "sample_rate", 24000) or 24000)
    audio = finalize_audio_array(audio, sample_rate, profile.head_trim_sec, profile.tail_remove_sec, profile.tail_pad_sec)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tts.save(audio, out_file)
    return len(audio) / float(sample_rate)


def render_candidate_batch(tts, ref_codes, pack: dict, jobs: list[tuple[int, str, CandidateProfile, Path]], batch_size: int, use_limited_tokens: bool = False) -> list[dict]:
    """Render fast-mode jobs with VieNeu true batch inference, grouped by one profile."""
    results: list[dict] = []
    if not jobs:
        return results
    profile = jobs[0][2]
    for start in range(0, len(jobs), max(1, batch_size)):
        batch = jobs[start:start + max(1, batch_size)]
        prepared = [prepare_text_for_tts(tts, text) for _, text, _, _ in batch]
        seed_text = "|".join(clean_text(text) for _, text, _, _ in batch)
        text_seed = int(hashlib.sha256(seed_text.encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % 100000
        seed = int(pack.get("candidateSeedBase", 1729)) + text_seed + int(profile.seed_offset)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        infer_started = time.perf_counter()
        max_new_tokens = 0
        if use_limited_tokens:
            max_new_tokens = int(pack.get("candidateMaxNewTokens", 0) or 0)
            if max_new_tokens <= 0:
                max_len = max(len(clean_text(text)) for _, text, _, _ in batch)
                if profile.name.startswith("long"):
                    max_new_tokens = max(180, min(420, int(max_len * 6.0) + 90))
                else:
                    max_new_tokens = max(90, min(260, int(max_len * 4.2) + 40))
            audios = infer_batch_limited(
                tts=tts,
                texts=prepared,
                ref_codes=ref_codes,
                ref_text=pack.get("refText", ""),
                temperature=profile.temperature,
                top_k=profile.top_k,
                max_new_tokens=max_new_tokens,
            )
        else:
            audios = tts.infer_batch(
                texts=prepared,
                ref_codes=ref_codes,
                ref_text=pack.get("refText", ""),
                temperature=profile.temperature,
                top_k=profile.top_k,
                skip_normalize=True,
                apply_watermark=False,
            )
        infer_sec = time.perf_counter() - infer_started
        for (index, text, _, out_file), audio in zip(batch, audios):
            item_started = time.perf_counter()
            out_file.parent.mkdir(parents=True, exist_ok=True)
            tts.save(audio, out_file)
            finalize_candidate_edges(out_file, profile.head_trim_sec, profile.tail_remove_sec)
            duration = audio_duration(out_file)
            results.append({
                "index": index,
                "text": text,
                "profile": profile.name,
                "file": str(out_file),
                "duration": round(duration, 3),
                "settings": asdict(profile),
                "batchSize": len(batch),
                "batchInferSec": round(infer_sec, 3),
                "maxNewTokens": max_new_tokens or "unbounded",
                "limitedTokens": use_limited_tokens,
                "postSec": round(time.perf_counter() - item_started, 3),
            })
    return results


def infer_batch_limited(tts, texts: list[str], ref_codes, ref_text: str, temperature: float, top_k: int, max_new_tokens: int) -> list:
    """VieNeu standard batch inference with bounded max_new_tokens for faster production clips."""
    if getattr(tts, "_is_quantized_model", False):
        return tts.infer_batch(
            texts=texts,
            ref_codes=ref_codes,
            ref_text=ref_text,
            temperature=temperature,
            top_k=top_k,
            skip_normalize=True,
            apply_watermark=False,
        )
    ref_phonemes = tts.get_ref_phonemes(ref_text)
    chunk_phonemes = phonemize_batch(texts, skip_normalize=True)
    batch_prompt_ids = []
    for phonemes in chunk_phonemes:
        prompt_ids = tts._apply_chat_template(ref_codes, ref_phonemes, phonemes, emotion_tag=getattr(tts, "default_emotion", None))
        batch_prompt_ids.append(torch.tensor(prompt_ids))
    inputs = tts.tokenizer.pad({"input_ids": batch_prompt_ids}, padding=True, return_tensors="pt")
    inputs = {key: value.to(tts.backbone.device) for key, value in inputs.items()}
    speech_end_id = tts.tokenizer.convert_tokens_to_ids("<|SPEECH_GENERATION_END|>")
    with torch.no_grad():
        output_tokens = tts.backbone.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            eos_token_id=speech_end_id,
            do_sample=True,
            temperature=temperature,
            top_k=top_k,
            use_cache=True,
            min_new_tokens=min(40, max_new_tokens),
            pad_token_id=getattr(tts.tokenizer, "eos_token_id", None),
        )
    input_length = inputs["input_ids"].shape[-1]
    all_wavs = []
    for i in range(len(texts)):
        generated_ids = output_tokens[i, input_length:]
        output_str = tts.tokenizer.decode(generated_ids, add_special_tokens=False)
        all_wavs.append(tts._decode(output_str))
    return all_wavs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-dir", type=Path, required=True)
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--text")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--max-cues", type=int, default=0)
    parser.add_argument("--mode", choices=["fast", "quality", "candidates"], default="candidates", help="fast renders one approved profile per line; quality renders a compact safe candidate set per line; candidates renders every profile.")
    parser.add_argument("--fast-batch-size", type=int, default=0, help="VieNeu infer_batch size for fast mode. Default: pack candidateFastBatchSize or 4.")
    parser.add_argument("--force-profile", choices=[profile.name for profile in PROFILES], help="Force one profile for all fast-mode lines.")
    parser.add_argument("--limited-tokens", action="store_true", help="Debug only: bound max_new_tokens for speed. Disabled by default because it can cut final words.")
    parser.add_argument("--asr-select", action="store_true", help="Use faster-whisper small to select best candidate.")
    parser.add_argument("--asr-model", default="small")
    parser.add_argument("--no-asr", action="store_true", help="Skip ASR scoring even in quality mode. Use this for stable UI text rendering.")
    args = parser.parse_args()

    started = time.perf_counter()
    pack = load_pack(args.pack_dir.resolve())
    lines = load_lines(args)
    if args.max_cues:
        lines = lines[: args.max_cues]
    if not lines:
        raise SystemExit("Khong co cau text hop le de render.")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    tts = Vieneu(
        mode=clean_text(str(pack.get("vieneuMode", "standard"))) or "standard",
        backbone_repo=pack.get("model", "pnnbao-ump/VieNeu-TTS-0.3B"),
        gguf_filename=pack.get("ggufFilename", None),
        backbone_device=device,
        codec_repo=pack.get("codec", "neuphonic/distill-neucodec"),
        codec_device=device,
    )
    lora_dir = resolve_lora_dir_for_render(pack)
    if lora_dir is not None:
        tts.load_lora_adapter(str(lora_dir))
    ref_codes = load_ref_codes_for_pack(tts, pack)
    asr_model = None if args.no_asr else (load_asr_model(args.asr_model) if args.asr_select or args.mode == "quality" else None)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    report: list[dict] = []
    if args.mode == "fast":
        batch_size = args.fast_batch_size or int(pack.get("candidateFastBatchSize", 4) or 4)
        if batch_size <= 1:
            for index, text in enumerate(lines, start=1):
                profile = fast_profile_for_text(text, pack, args.force_profile or "")
                cue_dir = args.out_dir / f"cau_{index:04d}_candidates"
                out_file = cue_dir / f"{profile.name}.mp3"
                print(f"Fast single infer {index}/{len(lines)} profile={profile.name}: {text[:100]}")
                started_one = time.perf_counter()
                duration = render_candidate(tts, ref_codes, pack, text, profile, out_file)
                best = {
                    "index": index,
                    "text": text,
                    "profile": profile.name,
                    "file": str(out_file),
                    "duration": round(duration, 3),
                    "settings": asdict(profile),
                    "batchSize": 1,
                    "inferMode": "single_tts_infer",
                    "inferSec": round(time.perf_counter() - started_one, 3),
                    "maxNewTokens": "unbounded",
                    "limitedTokens": False,
                }
                best_out = args.out_dir / f"cau_{index:04d}.mp3"
                shutil.copy2(best["file"], best_out)
                report.append({"index": index, "text": text, "best": best, "bestFile": str(best_out), "candidates": [best]})
                print(f"  selected: {profile.name} audio={duration:.2f}s infer_mode=single_tts_infer")
        else:
            grouped_jobs: dict[str, list[tuple[int, str, CandidateProfile, Path]]] = {}
            for index, text in enumerate(lines, start=1):
                profile = fast_profile_for_text(text, pack, args.force_profile or "")
                cue_dir = args.out_dir / f"cau_{index:04d}_candidates"
                out_file = cue_dir / f"{profile.name}.mp3"
                grouped_jobs.setdefault(profile.name, []).append((index, text, profile, out_file))
            rendered_by_index: dict[int, dict] = {}
            for profile_name, jobs in grouped_jobs.items():
                print(f"Fast batch profile={profile_name}: {len(jobs)} lines, batch_size={batch_size}")
                use_limited_tokens = bool(args.limited_tokens or pack.get("candidateUseLimitedTokens", False))
                for item in render_candidate_batch(tts, ref_codes, pack, jobs, batch_size, use_limited_tokens):
                    rendered_by_index[int(item["index"])] = item
                    print(
                        f"  cau_{int(item['index']):04d} {item['profile']}: "
                        f"audio={item['duration']:.2f}s batch_infer={item['batchInferSec']:.1f}s post={item['postSec']:.1f}s"
                    )
            for index, text in enumerate(lines, start=1):
                best = rendered_by_index.get(index)
                if not best:
                    raise RuntimeError(f"Khong render duoc cau {index}: {text}")
                best_out = args.out_dir / f"cau_{index:04d}.mp3"
                shutil.copy2(best["file"], best_out)
                report.append({"index": index, "text": text, "best": best, "bestFile": str(best_out), "candidates": [best]})
    elif args.mode == "quality":
        for index, text in enumerate(lines, start=1):
            cue_dir = args.out_dir / f"cau_{index:04d}_candidates"
            cue_dir.mkdir(parents=True, exist_ok=True)
            profiles = quality_profiles_for_text(text)
            print(f"Quality render {index}/{len(lines)} profiles={','.join(profile.name for profile in profiles)}: {text[:100]}")
            candidates = []
            for profile in profiles:
                out_file = cue_dir / f"{profile.name}.mp3"
                try:
                    started_one = time.perf_counter()
                    duration = render_candidate(tts, ref_codes, pack, text, profile, out_file)
                    candidates.append({
                        "profile": profile.name,
                        "file": str(out_file),
                        "duration": round(duration, 3),
                        "settings": asdict(profile),
                        "inferSec": round(time.perf_counter() - started_one, 3),
                    })
                    print(f"  {profile.name}: {duration:.2f}s")
                except Exception as exc:
                    candidates.append({
                        "profile": profile.name,
                        "file": str(out_file),
                        "error": str(exc),
                        "settings": asdict(profile),
                    })
                    print(f"  {profile.name}: ERROR {exc}")
            valid = [row for row in candidates if "error" not in row and Path(row["file"]).exists()]
            if not valid:
                raise RuntimeError(f"Khong render duoc candidate nao cho cau {index}: {text}")
            score_candidates_with_asr(text, valid, asr_model)
            best = pick_default_profile(text, valid, pack)
            best = rescue_short_selection(text, best, valid)
            best_out = args.out_dir / f"cau_{index:04d}.mp3"
            shutil.copy2(best["file"], best_out)
            if should_quality_tail_clean(text, str(best.get("profile", ""))):
                cleanup_tail_artifact(best_out, seconds=float(pack.get("candidateQualityTailCleanSec", 0.20) or 0.20))
                best["tailCleanApplied"] = True
                best["tailCleanSec"] = float(pack.get("candidateQualityTailCleanSec", 0.20) or 0.20)
                best["bestFileAfterTailClean"] = str(best_out)
            report.append({"index": index, "text": text, "best": best, "bestFile": str(best_out), "candidates": candidates})
            print(f"  selected: {best['profile']} -> {best_out.name}")
    else:
        for index, text in enumerate(lines, start=1):
            cue_dir = args.out_dir / f"cau_{index:04d}_candidates"
            cue_dir.mkdir(parents=True, exist_ok=True)
            print(f"Candidate render {index}/{len(lines)}: {text[:100]}")
            candidates = []
            for profile in PROFILES:
                out_file = cue_dir / f"{profile.name}.mp3"
                try:
                    duration = render_candidate(tts, ref_codes, pack, text, profile, out_file)
                    candidates.append({
                        "profile": profile.name,
                        "file": str(out_file),
                        "duration": round(duration, 3),
                        "settings": asdict(profile),
                    })
                    print(f"  {profile.name}: {duration:.2f}s")
                except Exception as exc:
                    candidates.append({
                        "profile": profile.name,
                        "file": str(out_file),
                        "error": str(exc),
                        "settings": asdict(profile),
                    })
                    print(f"  {profile.name}: ERROR {exc}")
            valid = [row for row in candidates if "error" not in row and Path(row["file"]).exists()]
            if not valid:
                raise RuntimeError(f"Khong render duoc candidate nao cho cau {index}: {text}")
            score_candidates_with_asr(text, valid, asr_model)
            best = pick_default_profile(text, valid, pack)
            best = rescue_short_selection(text, best, valid)
            best_out = args.out_dir / f"cau_{index:04d}.mp3"
            shutil.copy2(best["file"], best_out)
            report.append({"index": index, "text": text, "best": best, "bestFile": str(best_out), "candidates": candidates})
            print(f"  selected: {best['profile']} -> {best_out.name}")

    try:
        tts.close()
    finally:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()

    manifest = {
        "ok": True,
        "outDir": str(args.out_dir.resolve()),
        "elapsedSec": round(time.perf_counter() - started, 3),
        "total": len(report),
        "mode": args.mode,
        "profiles": [asdict(profile) for profile in PROFILES],
        "clips": report,
    }
    (args.out_dir / "candidate_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "outDir": manifest["outDir"], "total": manifest["total"], "elapsedSec": manifest["elapsedSec"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
