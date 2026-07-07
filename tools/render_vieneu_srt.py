from __future__ import annotations

import argparse
import gc
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave
import concurrent.futures
from array import array
from pathlib import Path

import torch
from vieneu import Vieneu


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / "ffmpeg.exe"
SILERO_VAD_MODEL = None
WHISPERX_ALIGN_CACHE = {}
_CPU_WORKERS = max(2, min(12, (os.cpu_count() or 8)))


def hidden_subprocess_kwargs() -> dict:
    """Return Windows subprocess flags that prevent helper console windows."""
    if not sys.platform.startswith("win"):
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def clean_text(text: str) -> str:
    text = repair_vietnamese_mojibake(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def repair_vietnamese_mojibake(text: str) -> str:
    if not text:
        return text
    markers = ("Ã", "Ä", "Æ", "áº", "á»", "Â")
    if not any(marker in text for marker in markers):
        return text
    try:
        fixed = text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except Exception:
        return text
    bad_before = sum(text.count(marker) for marker in markers)
    bad_after = sum(fixed.count(marker) for marker in markers)
    vi_after = sum(fixed.count(ch) for ch in "ăâđêôơưáàảãạắằẳẵặấầẩẫậéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ")
    return fixed if bad_after < bad_before and vi_after > 0 else text


def rewrite_for_vietnamese_tts(text: str) -> str:
    rewrites = [
        (r"\bnhưng\s+lúc\b", "nhưng khi"),
        (r"\bNhưng\s+lúc\b", "Nhưng khi"),
    ]
    text = clean_text(text)
    for pattern, replacement in rewrites:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def compare_tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFD", clean_text(text).lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("đ", "d")
    tokens = re.findall(r"[a-z0-9]+", text)
    tokens = ["luc" if token == "khi" else token for token in tokens]
    compact = []
    digit_words = {"mot", "hai", "ba", "bon", "nam", "sau", "bay", "tam", "chin"}
    token_aliases = {
        "loi": "luoi",
        "lamdieu": "lam dieu",
        "lieng": "dieu",
        "yieu": "dieu",
        "yeu": "dieu",
        "hoa": "ngoa",
        "ngoai": "ngoa",
        "ty": "tuy",
        "ti": "tuy",
        "tiet": "tien",
        "thuc": "thuoc",
        "thuc": "thuoc",
        "trong": "tram",
        "trongky": "tram ky",
        "trongki": "tram ky",
        "tre": "ke",
        "khe": "ke",
        "keo": "ke",
    }
    multi_token_aliases = {
        "haman": ["ham", "an"],
        "lamdieu": ["lam", "dieu"],
        "trongky": ["tram", "ky"],
        "trongki": ["tram", "ky"],
    }
    for token in tokens:
        if token.isdigit():
            try:
                expanded = compare_tokens(vietnamese_number_under_1000(int(token)))
            except Exception:
                expanded = []
            if expanded:
                compact.extend(expanded)
                continue
        if token in multi_token_aliases:
            compact.extend(multi_token_aliases[token])
            continue
        token = token_aliases.get(token, token)
        if token == "muoi" and compact and compact[-1] in digit_words:
            continue
        compact.append(token)
    return compact


def lcs_length(left: list[str], right: list[str]) -> int:
    if not left or not right:
        return 0
    prev = [0] * (len(right) + 1)
    for a in left:
        cur = [0]
        for j, b in enumerate(right, start=1):
            cur.append(prev[j - 1] + 1 if a == b else max(prev[j], cur[-1]))
        prev = cur
    return prev[-1]


def analyze_asr_match(expected: str, actual: str) -> dict:
    expected_tokens = compare_tokens(expected)
    actual_tokens = compare_tokens(actual)
    lcs = lcs_length(expected_tokens, actual_tokens)
    coverage = lcs / max(1, len(expected_tokens))
    first_match = next((i for i, token in enumerate(actual_tokens) if token in set(expected_tokens[:3])), len(actual_tokens))
    extra_prefix = actual_tokens[:first_match]
    first_expected = expected_tokens[0] if expected_tokens else None
    first_actual = actual_tokens[0] if actual_tokens else None
    first_token_match = bool(first_expected and first_actual == first_expected)
    ok = coverage >= 0.92 and len(extra_prefix) <= 1
    return {
        "ok": ok,
        "coverage": round(coverage, 3),
        "extraPrefix": extra_prefix,
        "firstExpected": first_expected,
        "firstActual": first_actual,
        "firstTokenMatch": first_token_match,
        "expectedTokens": expected_tokens,
        "actualTokens": actual_tokens,
    }


def detect_filler_artifacts(expected: str, actual: str, pack: dict) -> list[str]:
    if not actual:
        return []
    default_fillers = [
        "ừm", "ưm", "ứm", "um", "uhm", "uh", "hm", "hmm",
        "ờ", "ờm", "ừ", "ư", "à", "hừm", "ấn",
    ]
    default_fillers.append("ấn")
    fillers = list(dict.fromkeys([*(pack.get("clipQualityFillerTokens") or []), *default_fillers]))
    expected_l = clean_text(expected).lower()
    actual_l = clean_text(actual).lower()
    found: list[str] = []
    for token in fillers:
        token_l = clean_text(str(token)).lower()
        if not token_l:
            continue
        if re.search(rf"(?<!\w){re.escape(token_l)}(?!\w)", actual_l) and not re.search(rf"(?<!\w){re.escape(token_l)}(?!\w)", expected_l):
            found.append(token_l)
    expected_tokens = set(compare_tokens(expected))
    actual_tokens = compare_tokens(actual)
    for token in fillers:
        filler_norm = compare_tokens(str(token))
        if len(filler_norm) != 1:
            continue
        norm = filler_norm[0]
        if norm and norm in actual_tokens and norm not in expected_tokens and norm not in found:
            found.append(norm)
    return found


def load_asr_model(model_name: str):
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        print(f"VieNeu validate: ASR unavailable ({exc})")
        return None
    if torch.cuda.is_available() and os.environ.get("VIENEU_ASR_DEVICE", "cuda").lower() != "cpu":
        try:
            model = WhisperModel(model_name, device="cuda", compute_type="float16")
            print(f"VieNeu validate: ASR device=cuda model={model_name}")
            return model
        except Exception as exc:
            print(f"VieNeu validate: ASR cuda unavailable, fallback cpu ({exc})")
    return WhisperModel(model_name, device="cpu", compute_type="int8")


def transcribe_audio(asr_model, path: Path) -> str:
    if asr_model is None:
        return ""
    segments, _info = asr_model.transcribe(
        str(path),
        language="vi",
        beam_size=5,
        vad_filter=False,
        condition_on_previous_text=False,
    )
    return clean_text(" ".join(segment.text.strip() for segment in segments))


def word_edge_times(asr_model, path: Path) -> tuple[float | None, float | None]:
    if asr_model is None:
        return None, None
    segments, _info = asr_model.transcribe(
        str(path),
        language="vi",
        beam_size=5,
        vad_filter=False,
        condition_on_previous_text=False,
        word_timestamps=True,
    )
    first_start = None
    last_end = None
    for segment in segments:
        for word in segment.words or []:
            if word.word.strip():
                if first_start is None:
                    first_start = float(word.start)
                last_end = float(word.end)
    return first_start, last_end


def word_timing_edges(asr_model, path: Path) -> list[tuple[str, float, float]]:
    if asr_model is None:
        return []
    segments, _info = asr_model.transcribe(
        str(path),
        language="vi",
        beam_size=5,
        vad_filter=False,
        condition_on_previous_text=False,
        word_timestamps=True,
    )
    words: list[tuple[str, float, float]] = []
    for segment in segments:
        for word in segment.words or []:
            text = word.word.strip()
            if text:
                words.append((text, float(word.start), float(word.end)))
    return words


def whisperx_word_timing_edges(path: Path, text: str, pack: dict) -> list[tuple[str, float, float]]:
    if not bool(pack.get("useWhisperXAlign", False)):
        return []
    try:
        import whisperx
    except Exception as exc:
        print(f"VieNeu artifact gate: WhisperX unavailable ({exc})")
        return []
    device = "cuda" if torch.cuda.is_available() and str(pack.get("whisperXDevice", "cuda")).lower() != "cpu" else "cpu"
    language = str(pack.get("whisperXLanguage", "vi"))
    key = (language, device)
    try:
        if key not in WHISPERX_ALIGN_CACHE:
            WHISPERX_ALIGN_CACHE[key] = whisperx.load_align_model(language_code=language, device=device)
            print(f"VieNeu artifact gate: WhisperX align ready language={language} device={device}")
        align_model, metadata = WHISPERX_ALIGN_CACHE[key]
        audio = whisperx.load_audio(str(path))
        duration = audio_duration(path)
        segments = [{"start": 0.0, "end": duration, "text": clean_text(text)}]
        aligned = whisperx.align(
            segments,
            align_model,
            metadata,
            audio,
            device,
            return_char_alignments=False,
        )
        words = []
        for word in aligned.get("word_segments", []) or []:
            value = str(word.get("word", "")).strip()
            if value and "start" in word and "end" in word:
                words.append((value, float(word["start"]), float(word["end"])))
        return words
    except Exception as exc:
        print(f"VieNeu artifact gate: WhisperX align failed ({exc})")
        return []


def token_for_match(text: str) -> str:
    tokens = compare_tokens(text)
    return tokens[0] if tokens else ""


def alignment_coverage(expected_text: str, words: list[tuple[str, float, float]]) -> float:
    expected = compare_tokens(expected_text)
    actual = [token_for_match(word) for word, _start, _end in words]
    return lcs_length(expected, actual) / max(1, len(expected))


def aligned_word_timing_edges(asr_model, path: Path, text: str, pack: dict) -> list[tuple[str, float, float]]:
    wx_words = whisperx_word_timing_edges(path, text, pack)
    if wx_words:
        coverage = alignment_coverage(text, wx_words)
        span = max(0.0, wx_words[-1][2] - wx_words[0][1]) if wx_words else 0.0
        min_natural, _max_natural = expected_duration_range(text)
        min_span = min_natural * float(pack.get("whisperXMinSpanRatio", 0.70))
        min_cov = float(pack.get("whisperXMinCoverage", 0.82))
        if coverage >= min_cov and span >= min_span:
            return wx_words
        print(f"VieNeu artifact gate: WhisperX weak align coverage={coverage:.3f} span={span:.3f}s min_span={min_span:.3f}s, fallback faster-whisper")
    return word_timing_edges(asr_model, path) if asr_model is not None else []


def cleanup_to_expected_word_bounds(path: Path, asr_model, expected_text: str, pack: dict) -> bool:
    if asr_model is None or not bool(pack.get("clipGuardCropToExpected", False)):
        return False
    expected_tokens = compare_tokens(expected_text)
    if not expected_tokens:
        return False
    words = aligned_word_timing_edges(asr_model, path, expected_text, pack)
    if not words:
        return False
    word_tokens = [token_for_match(word) for word, _start, _end in words]
    first_index = next((i for i, token in enumerate(word_tokens) if token == expected_tokens[0]), None)
    if first_index is None:
        first_set = set(expected_tokens[: min(3, len(expected_tokens))])
        first_index = next((i for i, token in enumerate(word_tokens) if token in first_set), None)
    last_index = None
    final_matches = [i for i, token in enumerate(word_tokens) if token == expected_tokens[-1]]
    if final_matches:
        last_index = final_matches[-1]
    elif bool(pack.get("clipGuardAllowTailFallback", False)):
        for wanted in reversed(expected_tokens[max(0, len(expected_tokens) - 3):]):
            matches = [i for i, token in enumerate(word_tokens) if token == wanted]
            if matches:
                last_index = matches[-1]
                break
    if first_index is None and last_index is None:
        return False

    duration = audio_duration(path)
    start = 0.0
    end = duration
    head_pad = float(pack.get("clipGuardHeadPadSec", 0.018))
    tail_pad = float(pack.get("clipGuardTailPadSec", 0.055))
    if first_index is not None:
        start = max(0.0, float(words[first_index][1]) - head_pad)
    if last_index is not None and (first_index is None or last_index >= first_index):
        end = min(duration, float(words[last_index][2]) + tail_pad)
    if end - start < 0.35:
        return False
    if start <= 0.025 and duration - end <= 0.025:
        return False

    tmp = path.with_name(path.stem + "_guardcrop_tmp" + path.suffix)
    fade = float(pack.get("clipGuardCropFadeSec", 0.018))
    filters = [f"atrim=start={start:.3f}:end={end:.3f}", "asetpts=N/SR/TB"]
    if fade > 0:
        filters.append(f"afade=t=in:st=0:d={min(fade, max(0.0, end - start)):.3f}")
        filters.append(f"afade=t=out:st={max(0.0, end - start - fade):.3f}:d={fade:.3f}")
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            ",".join(filters),
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
        print(
            "VieNeu guard crop: "
            f"{path.name} start={start:.3f}s end={end:.3f}s "
            f"first={words[first_index][0] if first_index is not None else '?'} "
            f"last={words[last_index][0] if last_index is not None else '?'}"
        )
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def last_word_end_time(asr_model, path: Path) -> float | None:
    _first_start, last_end = word_edge_times(asr_model, path)
    return last_end


def merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not intervals:
        return []
    intervals = sorted((float(a), float(b)) for a, b in intervals if b > a)
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        left, right = merged[-1]
        if start <= right:
            merged[-1] = (left, max(right, end))
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(
    bases: list[tuple[float, float]],
    allowed: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    allowed = merge_intervals(allowed)
    leftovers: list[tuple[float, float]] = []
    for base_start, base_end in bases:
        cursor = base_start
        for allow_start, allow_end in allowed:
            if allow_end <= cursor:
                continue
            if allow_start >= base_end:
                break
            if allow_start > cursor:
                leftovers.append((cursor, min(allow_start, base_end)))
            cursor = max(cursor, allow_end)
            if cursor >= base_end:
                break
        if cursor < base_end:
            leftovers.append((cursor, base_end))
    return [(a, b) for a, b in leftovers if b > a]


def vad_speech_segments(path: Path, pack: dict) -> list[tuple[float, float]]:
    if not bool(pack.get("artifactVadEnabled", False)):
        return []
    global SILERO_VAD_MODEL
    try:
        from silero_vad import get_speech_timestamps, load_silero_vad, read_audio
    except Exception as exc:
        print(f"VieNeu artifact gate: Silero VAD unavailable ({exc})")
        return []
    try:
        if SILERO_VAD_MODEL is None:
            SILERO_VAD_MODEL = load_silero_vad()
            print("VieNeu artifact gate: Silero VAD ready")
        sr = 16000
        wav = read_audio(str(path), sampling_rate=sr)
        timestamps = get_speech_timestamps(
            wav,
            SILERO_VAD_MODEL,
            sampling_rate=sr,
            threshold=float(pack.get("artifactVadThreshold", 0.34)),
            min_speech_duration_ms=int(pack.get("artifactVadMinSpeechMs", 35)),
            min_silence_duration_ms=int(pack.get("artifactVadMinSilenceMs", 35)),
            speech_pad_ms=int(pack.get("artifactVadSpeechPadMs", 20)),
        )
        return [(item["start"] / sr, item["end"] / sr) for item in timestamps]
    except Exception as exc:
        print(f"VieNeu artifact gate: Silero VAD failed ({exc})")
        return []


def voice_artifact_report(path: Path, text: str, word_times: list[tuple[str, float, float]], pack: dict) -> dict:
    vad_segments = vad_speech_segments(path, pack)
    if not vad_segments or not word_times:
        return {"ok": True, "vadSegments": vad_segments, "suspicious": []}
    duration = audio_duration(path)
    pad = float(pack.get("artifactWordPadSec", 0.11))
    allowed = [
        (max(0.0, start - pad), min(duration, end + pad))
        for _word, start, end in word_times
        if end > start
    ]
    leftovers = subtract_intervals(vad_segments, allowed)
    suspicious = []
    min_sec = float(pack.get("artifactMinSec", 0.075))
    rms_limit = float(pack.get("artifactRmsLimit", 0.006))
    for start, end in leftovers:
        segment_sec = end - start
        if segment_sec < min_sec:
            continue
        peak, rms = audio_peak_rms(path, start, end)
        if rms < rms_limit and peak < float(pack.get("artifactPeakLimit", 0.08)):
            continue
        suspicious.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(segment_sec, 3),
                "peak": round(peak, 4),
                "rms": round(rms, 4),
            }
        )
    return {
        "ok": not suspicious,
        "vadSegments": [(round(a, 3), round(b, 3)) for a, b in vad_segments],
        "suspicious": suspicious,
    }


def text_units_count(text: str) -> int:
    return len(re.findall(r"[\w\u00c0-\u1ef9]+", clean_text(text), flags=re.UNICODE))


def expected_duration_range(text: str, target_sec: float | None = None) -> tuple[float, float]:
    text = clean_text(text)
    chars = len(text)
    words = max(1, text_units_count(text))
    natural_min = max(0.45, chars / 28.0, words / 5.8)
    natural_max = max(natural_min + 0.8, chars / 5.5, words / 1.4)
    if target_sec and target_sec > 0:
        natural_min = min(natural_min, max(0.35, target_sec * 1.25))
        natural_max = max(natural_max, target_sec * 3.2, 1.2)
    return natural_min, natural_max


def smooth_audio_file(path: Path, add_lead_silence: bool = False) -> None:
    tmp = path.with_name(path.stem + "_smooth_tmp" + path.suffix)
    filters = ["afade=t=in:st=0:d=0.08", "alimiter=limit=0.92"]
    if add_lead_silence:
        filters.insert(0, "adelay=120|120")
    cmd = [
        str(FFMPEG),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-af",
        ",".join(filters),
        str(tmp),
    ]
    subprocess.run(cmd, check=True)
    tmp.replace(path)


def finalize_clip_edges(path: Path, pack: dict, mode: str = "srt") -> bool:
    if not bool(pack.get(f"{mode}FinalizeEdges", False)):
        return False
    duration = audio_duration(path)
    if duration <= 0.25:
        return False
    fade_in = float(pack.get(f"{mode}FinalFadeInSec", 0.025))
    fade_out = float(pack.get(f"{mode}FinalFadeOutSec", 0.075))
    tail_pad = float(pack.get(f"{mode}FinalTailSilenceSec", 0.08))
    fade_out = max(0.0, min(fade_out, duration * 0.25))
    filters = []
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        filters.append(f"afade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out:.3f}")
    if tail_pad > 0:
        filters.append(f"apad=pad_dur={tail_pad:.3f}")
    filters.append("alimiter=limit=0.92")
    tmp = path.with_name(path.stem + "_final_edge_tmp" + path.suffix)
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            ",".join(filters),
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def detect_silences(path: Path, threshold_db: int = -45, min_duration: float = 0.05) -> list[tuple[float, float]]:
    proc = subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"silencedetect=n={threshold_db}dB:d={min_duration:.3f}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    starts: list[float] = []
    silences: list[tuple[float, float]] = []
    for line in (proc.stderr or "").splitlines():
        start_match = re.search(r"silence_start:\s*([0-9.]+)", line)
        if start_match:
            starts.append(float(start_match.group(1)))
            continue
        end_match = re.search(r"silence_end:\s*([0-9.]+)", line)
        if end_match and starts:
            silences.append((starts.pop(0), float(end_match.group(1))))
    return silences


def remove_short_noise_islands(path: Path, pack: dict) -> bool:
    if not bool(pack.get("boundaryCleanupShortIslands", False)):
        return False
    silences = detect_silences(
        path,
        int(pack.get("boundaryCleanupIslandThresholdDb", -45)),
        float(pack.get("boundaryCleanupIslandSilenceMinSec", 0.05)),
    )
    if len(silences) < 2:
        return False
    min_side_silence = float(pack.get("boundaryCleanupIslandSideSilenceSec", 0.18))
    max_island = float(pack.get("boundaryCleanupIslandMaxSec", 0.13))
    min_island = float(pack.get("boundaryCleanupIslandMinSec", 0.035))
    cuts: list[tuple[float, float]] = []
    for left, right in zip(silences, silences[1:]):
        left_dur = left[1] - left[0]
        right_dur = right[1] - right[0]
        island_start = left[1]
        island_end = right[0]
        island_dur = island_end - island_start
        if left_dur >= min_side_silence and right_dur >= min_side_silence and min_island <= island_dur <= max_island:
            cuts.append((island_start, island_end))
    if not cuts:
        return False

    duration = audio_duration(path)
    pieces: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in cuts:
        if start > cursor + 0.01:
            pieces.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration - 0.01:
        pieces.append((cursor, duration))
    if not pieces:
        return False

    tmp = path.with_name(path.stem + "_island_tmp" + path.suffix)
    filter_parts = []
    labels = []
    for index, (start, end) in enumerate(pieces):
        label = f"a{index}"
        filter_parts.append(f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=N/SR/TB[{label}]")
        labels.append(f"[{label}]")
    filter_parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]")
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[out]",
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
        print(
            "VieNeu boundary cleanup islands: "
            + ", ".join(f"{start:.3f}-{end:.3f}s" for start, end in cuts)
        )
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def limit_internal_silences(path: Path, pack: dict) -> bool:
    if not bool(pack.get("textPauseLimitInternalSilence", False)):
        return False
    max_silence = float(pack.get("textPauseMaxDetectedSilenceSec", 0.285))
    silences = detect_silences(
        path,
        int(pack.get("textPauseSilenceThresholdDb", -45)),
        float(pack.get("textPauseSilenceMinSec", 0.04)),
    )
    merged_silences: list[tuple[float, float]] = []
    merge_gap = float(pack.get("textPauseMergeAdjacentSilenceGapSec", 0.035))
    for start, end in silences:
        if merged_silences and start - merged_silences[-1][1] <= merge_gap:
            merged_silences[-1] = (merged_silences[-1][0], end)
        else:
            merged_silences.append((start, end))
    silences = merged_silences
    duration = audio_duration(path)
    cuts: list[tuple[float, float]] = []
    for start, end in silences:
        silence_duration = end - start
        if start < 0.2 or duration - end < 0.2:
            continue
        if silence_duration > max_silence + 0.05:
            cuts.append((start + max_silence, end))
    if not cuts:
        return False

    pieces: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in cuts:
        if start > cursor + 0.01:
            pieces.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration - 0.01:
        pieces.append((cursor, duration))
    if not pieces:
        return False

    tmp = path.with_name(path.stem + "_sil_limit_tmp" + path.suffix)
    filter_parts = []
    labels = []
    for index, (start, end) in enumerate(pieces):
        label = f"s{index}"
        filter_parts.append(f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=N/SR/TB[{label}]")
        labels.append(f"[{label}]")
    filter_parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]")
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[out]",
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
        print(
            "VieNeu pause limit internal silence: "
            + ", ".join(f"{start:.3f}-{end:.3f}s" for start, end in cuts)
        )
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def apply_post_declick(path: Path, pack: dict, mode: str = "text") -> bool:
    enabled = bool(pack.get(f"{mode}PostDeclick", pack.get("textPostDeclick", False)))
    if not enabled:
        return False
    tmp = path.with_name(path.stem + "_declick_tmp" + path.suffix)
    use_adeclick = bool(pack.get(f"{mode}PostDeclickUseAdecl", pack.get("textPostDeclickUseAdecl", True)))
    filters = ["adeclick"] if use_adeclick else []
    if bool(pack.get(f"{mode}PostDeclickLimiter", pack.get("textPostDeclickLimiter", True))):
        filters.append("alimiter=limit=0.92")
    if not filters:
        return False
    try:
        subprocess.run(
            [
                str(FFMPEG),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-af",
                ",".join(filters),
                str(tmp),
            ],
            check=True,
            timeout=float(pack.get(f"{mode}PostDeclickTimeoutSec", pack.get("textPostDeclickTimeoutSec", 45))),
        )
    except subprocess.TimeoutExpired:
        if tmp.exists():
            tmp.unlink()
        print(f"VieNeu post declick skipped: timeout mode={mode}")
        return False
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
        print("VieNeu post declick applied")
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def normalize_clip_silence_edges(path: Path, pack: dict, is_final: bool = False) -> bool:
    if not bool(pack.get("textPauseNormalizeSilenceEdges", False)):
        return False
    duration = audio_duration(path)
    silences = detect_silences(
        path,
        int(pack.get("textPauseSilenceThresholdDb", -45)),
        float(pack.get("textPauseSilenceMinSec", 0.03)),
    )
    if not silences:
        return False

    head_pad = float(pack.get("textPauseHeadPadSec", 0.035))
    tail_pad = float(pack.get("textPauseTailPadSec", 0.075 if not is_final else 0.12))
    start = 0.0
    end = duration

    first = silences[0]
    if first[0] <= 0.01:
        start = max(0.0, first[1] - head_pad)

    last = silences[-1]
    if duration - last[1] <= 0.03:
        end = min(duration, last[0] + tail_pad)

    if end <= start + 0.35:
        return False
    if start <= 0.03 and duration - end <= 0.03:
        return False

    tmp = path.with_name(path.stem + "_sil_edge_tmp" + path.suffix)
    fade = float(pack.get("textPauseEdgeFadeSec", 0.025))
    filters = [
        f"atrim=start={start:.3f}:end={end:.3f}",
        "asetpts=N/SR/TB",
        f"afade=t=in:st=0:d={fade:.3f}",
        f"afade=t=out:st={max(0.0, end - start - fade):.3f}:d={fade:.3f}",
    ]
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            ",".join(filters),
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
        print(
            "VieNeu pause normalize silence: "
            f"{path.name} start={start:.3f}s end={end:.3f}s duration={duration:.3f}s"
        )
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def trim_generated_silence(path: Path) -> None:
    tmp = path.with_name(path.stem + "_trim_tmp" + path.suffix)
    cmd = [
        str(FFMPEG),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-af",
        "silenceremove=start_periods=1:start_duration=0.03:start_threshold=-50dB:"
        "stop_periods=1:stop_duration=0.90:stop_threshold=-50dB:stop_silence=0.28",
        str(tmp),
    ]
    subprocess.run(cmd, check=True)
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
    elif tmp.exists():
        tmp.unlink()


def trim_edges(path: Path, start_sec: float = 0.0, end_sec: float = 0.0) -> None:
    if start_sec <= 0 and end_sec <= 0:
        return
    duration = audio_duration(path)
    if duration <= start_sec + end_sec + 0.25:
        return
    tmp = path.with_name(path.stem + "_edge_tmp" + path.suffix)
    start = max(0.0, start_sec)
    end = max(start + 0.05, duration - max(0.0, end_sec))
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            f"atrim=start={start:.3f}:end={end:.3f},asetpts=N/SR/TB",
            str(tmp),
        ],
        check=True,
    )
    tmp.replace(path)


def trim_nonfinal_tail(path: Path, trim_sec: float = 0.0, fade_sec: float = 0.05) -> None:
    if trim_sec <= 0:
        return
    duration = audio_duration(path)
    if duration <= trim_sec + 0.35:
        return
    end = duration - trim_sec
    fade = max(0.0, min(fade_sec, end * 0.35))
    filters = [f"atrim=0:{end:.3f}", "asetpts=N/SR/TB"]
    if fade > 0:
        filters.append(f"afade=t=out:st={max(0.0, end - fade):.3f}:d={fade:.3f}")
    tmp = path.with_name(path.stem + "_tail_tmp" + path.suffix)
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            ",".join(filters),
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
    elif tmp.exists():
        tmp.unlink()


def trim_initial_noise(path: Path, start_sec: float = 0.0, fade_sec: float = 0.03) -> None:
    if start_sec <= 0:
        return
    duration = audio_duration(path)
    if duration <= start_sec + 0.35:
        return
    tmp = path.with_name(path.stem + "_head_tmp" + path.suffix)
    filters = [f"atrim=start={start_sec:.3f}", "asetpts=N/SR/TB"]
    fade = max(0.0, min(fade_sec, duration - start_sec))
    if fade > 0:
        filters.append(f"afade=t=in:st=0:d={fade:.3f}")
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            ",".join(filters),
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
    elif tmp.exists():
        tmp.unlink()


def cleanup_initial_noise_by_asr(path: Path, asr_model, pack: dict) -> bool:
    if not bool(pack.get("boundaryCleanupAsr", False)) or asr_model is None:
        return False
    first_start, _last_end = word_edge_times(asr_model, path)
    if first_start is None:
        return False
    min_head = float(pack.get("boundaryCleanupHeadMinSec", 0.18))
    if first_start < min_head:
        return False
    pad = float(pack.get("boundaryCleanupHeadPadSec", 0.06))
    max_trim = float(pack.get("boundaryCleanupHeadMaxTrimSec", 0.28))
    trim = min(max(0.0, first_start - pad), max_trim)
    if trim <= 0.03:
        return False
    trim_initial_noise(path, trim, float(pack.get("boundaryCleanupHeadFadeSec", 0.03)))
    print(
        "VieNeu boundary cleanup head: "
        f"{path.name} first_word={first_start:.3f}s trim={trim:.3f}s"
    )
    return True


def cleanup_head_artifact(path: Path, pack: dict) -> bool:
    """Remove short non-text breath/filler artifacts at the very start of a generated clip.

    VieNeu can emit a tiny codec-prompt sound before the first word. ASR often folds
    that sound into the first word timestamp, so this gate uses VAD when possible and
    falls back to a small configured head trim with fade-in.
    """
    if not bool(pack.get("headArtifactCleanup", False)):
        return False
    duration = audio_duration(path)
    if duration <= 0.5:
        return False

    fallback_trim = float(pack.get("headArtifactFallbackTrimSec", 0.0))
    trim = fallback_trim if bool(pack.get("headArtifactFixedTrimOnly", False)) else 0.0
    if trim <= 0.0:
        vad_pack = dict(pack)
        vad_pack["artifactVadEnabled"] = True
        segments = vad_speech_segments(path, vad_pack)
        if len(segments) >= 2:
            first_start, first_end = segments[0]
            next_start, _next_end = segments[1]
            first_len = first_end - first_start
            if (
                first_start <= float(pack.get("headArtifactMaxStartSec", 0.08))
                and first_len <= float(pack.get("headArtifactMaxIslandSec", 0.30))
                and next_start <= float(pack.get("headArtifactSearchSec", 0.75))
            ):
                trim = max(0.0, next_start - float(pack.get("headArtifactKeepPadSec", 0.025)))

    if trim <= 0.0 and fallback_trim > 0:
        trim = fallback_trim

    max_trim = float(pack.get("headArtifactMaxTrimSec", 0.22))
    trim = min(max(0.0, trim), max_trim)
    if trim <= 0.015:
        return False

    trim_initial_noise(path, trim, float(pack.get("headArtifactFadeInSec", 0.025)))
    print(f"VieNeu head artifact cleanup: {path.name} trim={trim:.3f}s")
    return True


def cleanup_nonfinal_tail_by_asr(path: Path, asr_model, pack: dict) -> bool:
    if (
        not bool(pack.get("boundaryCleanupAsr", False))
        or not bool(pack.get("boundaryCleanupTailAsr", False))
        or asr_model is None
    ):
        return False
    last_end = last_word_end_time(asr_model, path)
    if last_end is None:
        return False
    duration = audio_duration(path)
    tail = duration - last_end
    min_tail = float(pack.get("boundaryCleanupTailMinSec", 0.26))
    if tail < min_tail:
        return False
    pad = float(pack.get("boundaryCleanupPadSec", 0.14))
    max_trim = float(pack.get("boundaryCleanupMaxTrimSec", 0.35))
    fade = float(pack.get("boundaryCleanupFadeSec", 0.05))
    target_end = min(duration, max(last_end + pad, duration - max_trim))
    trim = duration - target_end
    if trim <= 0.03:
        return False
    trim_nonfinal_tail(path, trim, fade)
    print(
        "VieNeu boundary cleanup: "
        f"{path.name} duration={duration:.3f}s last_word={last_end:.3f}s trim={trim:.3f}s"
    )
    return True


def make_clip_variant(pack: dict, attempt: int) -> tuple[float, int]:
    base_temp = float(pack.get("temperature", 0.38))
    base_top_k = int(pack.get("topK", 16))
    temp = max(0.25, min(0.72, base_temp + attempt * float(pack.get("retryTemperatureStep", 0.04))))
    top_k = max(8, min(45, base_top_k + attempt * int(pack.get("retryTopKStep", 3))))
    return temp, top_k


def validate_clip(path: Path, text: str, target_sec: float | None = None, min_ratio: float = 0.85) -> tuple[bool, str, float]:
    duration = audio_duration(path)
    min_sec, max_sec = expected_duration_range(text, target_sec)
    if not math.isfinite(duration) or duration <= 0.05:
        return False, f"empty audio duration={duration:.3f}s", duration
    if duration < min_sec * min_ratio:
        return False, f"too short duration={duration:.3f}s min={min_sec:.3f}s", duration
    if duration > max_sec * 1.65:
        return False, f"too long duration={duration:.3f}s max={max_sec:.3f}s", duration
    return True, "ok", duration


def clip_quality_score(path: Path, text: str, asr_model, pack: dict, target_sec: float | None = None) -> dict:
    validate_min_ratio = float(pack.get("clipValidateMinRatio", 0.85))
    if bool(pack.get("clipQualityAllowBestEffort", False)) and asr_model is None:
        validate_min_ratio = min(validate_min_ratio, float(pack.get("clipBestEffortValidateMinRatio", 0.30)))
    ok, reason, duration = validate_clip(path, text, target_sec, validate_min_ratio)
    score = 0.0 if ok else 1000.0
    asr_text = ""
    asr_match = {"ok": True, "coverage": None, "extraPrefix": []}
    first_word = None
    last_word = None
    last_word_for_tail = None
    last_word_duration = None

    if asr_model is not None:
        asr_text = transcribe_audio(asr_model, path)
        asr_match = analyze_asr_match(text, asr_text)
        coverage = float(asr_match["coverage"])
        coverage_min = float(pack.get("clipQualityAsrCoverageMin", 0.92))
        if not asr_match["ok"] and coverage >= coverage_min and len(asr_match["extraPrefix"]) <= 1:
            asr_match["ok"] = True
        score += (1.0 - coverage) * float(pack_score_weight("asrCoverage"))
        score += len(asr_match["extraPrefix"]) * float(pack.get("clipQualityExtraPrefixWeight", 12.0))
        if not bool(asr_match.get("firstTokenMatch", True)):
            score += float(pack.get("clipQualityFirstTokenMismatchWeight", 0.0))
        if not asr_match["ok"]:
            ok = False
            reason = f"asr coverage={asr_match['coverage']} extra={asr_match['extraPrefix']} text={asr_text}"
        filler_tokens = detect_filler_artifacts(text, asr_text, pack)
        if filler_tokens:
            score += len(filler_tokens) * float(pack.get("clipQualityFillerWeight", 1400.0))
            if bool(pack.get("clipQualityRejectFiller", True)):
                ok = False
                reason = f"asr filler={filler_tokens} text={asr_text}"
        if bool(pack.get("clipQualityRejectExtraPrefix", False)) and asr_match["extraPrefix"]:
            ok = False
            reason = f"asr extra prefix={asr_match['extraPrefix']} text={asr_text}"
        if bool(pack.get("clipQualityRejectFirstTokenMismatch", False)) and not bool(asr_match.get("firstTokenMatch", True)):
            ok = False
            reason = f"asr first token mismatch expected={asr_match.get('firstExpected')} actual={asr_match.get('firstActual')} text={asr_text}"
        first_word, last_word = word_edge_times(asr_model, path)
        word_times = aligned_word_timing_edges(asr_model, path, text, pack)
        if word_times:
            last_word_duration = max(0.0, word_times[-1][2] - word_times[-1][1])
            last_word_for_tail = float(word_times[-1][2])
            final_token = compare_tokens(text)[-1] if compare_tokens(text) else ""
            final_min_by_token = pack.get("clipQualityFinalWordMinSecByToken", {}) or {}
            reject_min_by_token = pack.get("clipQualityRejectFinalWordMinSecByToken", {}) or {}
            final_min = float(final_min_by_token.get(final_token, pack.get("clipQualityFinalWordMinSec", 0.20)))
            if last_word_duration < final_min:
                score += (final_min - last_word_duration) * float(pack.get("clipQualityFinalWordShortWeight", 220.0))
            reject_final_min = float(reject_min_by_token.get(final_token, pack.get("clipQualityRejectFinalWordMinSec", 0.0)))
            if bool(pack.get("clipQualityRejectShortFinalWord", False)) and reject_final_min > 0.0:
                if last_word_duration < reject_final_min:
                    ok = False
                    reason = f"short final word duration={last_word_duration:.3f}s min={reject_final_min:.3f}s"
            artifact = voice_artifact_report(path, text, word_times, pack)
            if not artifact["ok"]:
                score += len(artifact["suspicious"]) * float(pack.get("artifactIslandWeight", 900.0))
                score += sum(float(item["duration"]) for item in artifact["suspicious"]) * float(pack.get("artifactDurationWeight", 1600.0))
                if bool(pack.get("artifactReject", True)):
                    ok = False
                    reason = f"voice artifact islands={artifact['suspicious']}"
        else:
            artifact = {"ok": True, "vadSegments": [], "suspicious": []}
    else:
        artifact = {"ok": True, "vadSegments": [], "suspicious": []}

    head_peak = 0.0
    head_rms = 0.0
    head_voice_sec = 0.0
    if first_word is not None:
        head_voice_sec = max(0.0, first_word - float(pack.get("clipQualityHeadPadSec", 0.03)))
        if head_voice_sec > 0.0:
            head_peak, head_rms = audio_peak_rms(path, 0.0, head_voice_sec)
        head_max = float(pack.get("clipQualityHeadMaxSec", 0.10))
        head_rms_limit = float(pack.get("clipQualityHeadRmsLimit", 0.008))
        if head_voice_sec > head_max and head_rms > head_rms_limit:
            score += (head_voice_sec - head_max) * float(pack.get("clipQualityHeadVoiceWeight", 650.0))
            score += max(0.0, head_rms - head_rms_limit) * 1200.0
            if bool(pack.get("clipQualityRejectHeadVoice", False)):
                ok = False
                reason = f"voiced head sec={head_voice_sec:.3f} rms={head_rms:.4f} limit={head_rms_limit:.4f}"

    silences = detect_silences(path, -45, 0.04)
    duration = audio_duration(path)
    internal_islands = []
    for left, right in zip(silences, silences[1:]):
        island = right[0] - left[1]
        if left[1] > 0.15 and duration - right[0] > 0.15 and 0.035 <= island <= 0.24:
            internal_islands.append((left[1], right[0], island))
    score += len(internal_islands) * 10.0
    score += sum(island for *_bounds, island in internal_islands) * 20.0

    if last_word is not None:
        tail_anchor = last_word_for_tail if last_word_for_tail is not None else last_word
        tail_start = min(duration, tail_anchor + 0.05)
        tail_peak, tail_rms = audio_peak_rms(path, tail_start, duration)
        tail_limit = float(pack.get("clipQualityTailRmsLimit", 0.018))
        if tail_rms > tail_limit:
            score += (tail_rms - tail_limit) * 900.0
            if bool(pack.get("clipQualityRejectTailVoice", False)):
                ok = False
                reason = f"voiced tail rms={tail_rms:.4f} limit={tail_limit:.4f}"
        if tail_peak > 0.20:
            score += (tail_peak - 0.20) * 60.0
    else:
        tail_peak, tail_rms = 0.0, 0.0

    peak, rms = audio_peak_rms(path)
    if peak > 0.97:
        score += (peak - 0.97) * 80.0

    return {
        "ok": ok,
        "score": round(score, 4),
        "reason": reason,
        "duration": round(duration, 3),
        "asrText": asr_text,
        "asr": asr_match,
        "firstWord": first_word,
        "lastWord": last_word,
        "lastWordDuration": None if last_word_duration is None else round(last_word_duration, 4),
        "artifact": artifact,
        "internalIslands": internal_islands,
        "tailPeak": round(tail_peak, 4),
        "tailRms": round(tail_rms, 4),
        "headVoiceSec": round(head_voice_sec, 3),
        "headPeak": round(head_peak, 4),
        "headRms": round(head_rms, 4),
        "peak": round(peak, 4),
        "rms": round(rms, 4),
    }


def pack_score_weight(name: str) -> float:
    if name == "asrCoverage":
        return 120.0
    return 1.0


def infer_one_clip(
    tts,
    pack: dict,
    ref_codes,
    text: str,
    out_wav: Path,
    target_sec: float | None = None,
    asr_model=None,
) -> float:
    text = clean_text(text)
    effective_pack = dict(pack)
    target_for_variant = float(target_sec or 0.0)
    token_count_for_variant = len(compare_tokens(text))
    has_punctuation_break = bool(re.search(r"[,;:\uff0c\uff1b\uff1a]", text))
    long_variant = (
        target_for_variant >= float(pack.get("longVariantMinTargetSec", 9999.0))
        or token_count_for_variant >= int(pack.get("longVariantMinWords", 999999))
        or (
            has_punctuation_break
            and token_count_for_variant >= int(pack.get("longVariantPunctuationMinWords", 999999))
        )
    )
    if long_variant:
        effective_pack["temperature"] = float(pack.get("longVariantTemperature", pack.get("temperature", 0.38)))
        effective_pack["topK"] = int(pack.get("longVariantTopK", pack.get("topK", 16)))
        effective_pack["retryTemperatureStep"] = float(pack.get("longVariantRetryTemperatureStep", pack.get("retryTemperatureStep", 0.04)))
        effective_pack["retryTopKStep"] = int(pack.get("longVariantRetryTopKStep", pack.get("retryTopKStep", 3)))
        long_skip = str(pack.get("longVariantTailRemoveSkipFinalTokens", "") or "")
        if long_skip:
            effective_pack["tailRemoveSkipFinalTokens"] = long_skip
            effective_pack["textTailRemoveSkipFinalTokens"] = long_skip
            effective_pack["srtTailRemoveSkipFinalTokens"] = long_skip
        print(
            "VieNeu long variant: "
            f"target={target_for_variant:.3f}s temp={effective_pack['temperature']} topK={effective_pack['topK']}"
        )
    guard_prefix = clean_text(str(pack.get("clipGuardPrefixText", "")))
    guard_suffix = clean_text(str(pack.get("clipGuardSuffixText", "")))
    synth_text = clean_text(" ".join(part for part in [guard_prefix, text, guard_suffix] if part))
    prepared = prepare_text_for_tts(tts, synth_text)
    retries = max(1, int(effective_pack.get("clipRetries", 3)))
    last_reason = "not rendered"
    scored_candidates: list[tuple[float, Path, dict]] = []
    select_best = bool(pack.get("clipQualitySelectBest", False)) and asr_model is not None
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries):
        temp, top_k = make_clip_variant(effective_pack, attempt)
        text_seed = int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:6], 16)
        seed = int(effective_pack.get("seedBase", 1729)) + text_seed + attempt
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        candidate = out_wav.with_name(f"{out_wav.stem}_try{attempt + 1}{out_wav.suffix}")
        use_batch_clip = (
            bool(pack.get("clipUseInferBatch", False))
            and hasattr(tts, "infer_batch")
            and len(text) <= int(pack.get("clipUseInferBatchMaxChars", 120))
        )
        if use_batch_clip:
            audios = tts.infer_batch(
                texts=[prepared],
                ref_codes=ref_codes,
                ref_text=pack.get("refText", ""),
                temperature=temp,
                top_k=top_k,
                skip_normalize=True,
                apply_watermark=False,
                max_new_tokens=int(effective_pack.get("clipMaxNewTokens", effective_pack.get("srtMaxNewTokens", 520))),
                min_new_tokens=int(effective_pack.get("clipMinNewTokens", effective_pack.get("srtMinNewTokens", 40))),
            )
            audio = audios[0]
        else:
            audio = tts.infer(
                text=prepared,
                ref_codes=ref_codes,
                ref_text=pack.get("refText", ""),
                max_chars=int(effective_pack.get("clipInferMaxChars", effective_pack.get("maxChars", 85))),
                temperature=temp,
                top_k=top_k,
                skip_normalize=True,
                apply_watermark=False,
            )
        tts.save(audio, candidate)
        if not bool(effective_pack.get("clipNoTrimGeneratedSilence", False)):
            trim_generated_silence(candidate)
        if not bool(effective_pack.get("clipNoEdgeTrim", False)):
            trim_edges(
                candidate,
                float(effective_pack.get("trimHeadSec", 0.08)),
                float(effective_pack.get("trimTailSec", 0.12)),
            )
        if not bool(effective_pack.get("clipNoHeadArtifactCleanup", False)):
            cleanup_head_artifact(candidate, effective_pack)
        if asr_model is not None:
            crop_pack = dict(pack)
            if not bool(pack.get("candidateUseWhisperXAlign", False)):
                crop_pack["useWhisperXAlign"] = False
            did_guard_crop = False
            if not bool(pack.get("clipNoAsrCrop", False)):
                did_guard_crop = cleanup_to_expected_word_bounds(candidate, asr_model, text, crop_pack)
            if not did_guard_crop and not bool(pack.get("clipNoAsrBoundaryCleanup", False)):
                cleanup_initial_noise_by_asr(candidate, asr_model, pack)
                cleanup_nonfinal_tail_by_asr(candidate, asr_model, pack)
        quality_pack = dict(effective_pack)
        if not bool(pack.get("candidateUseWhisperXAlign", False)):
            quality_pack["useWhisperXAlign"] = False
        quality = clip_quality_score(candidate, text, asr_model, quality_pack, target_sec)
        ok = bool(quality["ok"])
        reason = str(quality["reason"])
        duration = float(quality["duration"])
        if select_best:
            scored_candidates.append((float(quality["score"]), candidate, quality))
            last_reason = reason
            print(
                "VieNeu candidate: "
                f"{candidate.name} ok={ok} score={quality['score']} "
                f"head_sec={quality['headVoiceSec']} head_rms={quality['headRms']} "
                f"tail_rms={quality['tailRms']} final_word={quality['lastWordDuration']} "
                f"islands={len(quality['internalIslands'])} "
                f"artifacts={len(quality['artifact'].get('suspicious', []))} "
                f"asr={quality['asrText']}"
            )
            if ok and float(quality["score"]) <= float(pack.get("clipQualityEarlyAcceptScore", 0.001)):
                candidate.replace(out_wav)
                for old in out_wav.parent.glob(f"{out_wav.stem}_try*{out_wav.suffix}"):
                    if old.exists():
                        old.unlink()
                print(f"VieNeu candidate early accept: {out_wav.name} score={quality['score']}")
                return duration
            continue
        if ok:
            candidate.replace(out_wav)
            for old in out_wav.parent.glob(f"{out_wav.stem}_try*{out_wav.suffix}"):
                if old.exists():
                    old.unlink()
            return duration
        last_reason = reason
        if candidate.exists():
            candidate.unlink()
    if select_best and scored_candidates:
        ok_candidates = [item for item in scored_candidates if bool(item[2]["ok"])]
        pool = ok_candidates or scored_candidates
        pool.sort(key=lambda item: item[0])
        best_score, best_path, best_quality = pool[0]
        if not ok_candidates:
            reason_l = str(best_quality.get("reason", "")).lower()
            asr_cov = best_quality.get("asr", {}).get("coverage")
            try:
                asr_cov_f = float(asr_cov)
            except Exception:
                asr_cov_f = 0.0
            hard_reasons = ["filler", "artifact", "extra prefix", "first token", "voiced head"]
            allow_non_artifact = (
                bool(pack.get("clipQualityAllowNonArtifactBestEffort", False))
                and asr_cov_f >= float(pack.get("clipQualityBestEffortCoverageMin", 0.80))
                and not any(token in reason_l for token in hard_reasons)
            )
            if not bool(pack.get("clipQualityAllowBestEffort", False)) and not allow_non_artifact:
                for _score, old_path, _quality in scored_candidates:
                    if old_path.exists():
                        old_path.unlink()
                raise RuntimeError(f"VieNeu clip failed after {retries} tries: {best_quality['reason']} | text={text[:120]}")
            print(
                "VieNeu candidate best-effort: "
                f"{out_wav.name} score={best_score:.4f} reason={best_quality['reason']} "
                f"asr={best_quality['asrText']}"
            )
        best_path.replace(out_wav)
        for _score, old_path, _quality in scored_candidates:
            if old_path.exists():
                old_path.unlink()
        print(
            "VieNeu candidate selected: "
            f"{out_wav.name} score={best_score:.4f} "
            f"tail_rms={best_quality['tailRms']} final_word={best_quality['lastWordDuration']} "
            f"islands={len(best_quality['internalIslands'])} "
            f"artifacts={len(best_quality['artifact'].get('suspicious', []))}"
        )
        return float(best_quality["duration"])
    raise RuntimeError(f"VieNeu clip failed after {retries} tries: {last_reason} | text={text[:120]}")


def warmup_tts(tts, pack: dict, ref_codes) -> None:
    warmup_text = clean_text(pack.get("warmupText", ""))
    if not warmup_text:
        return
    _ = tts.infer(
        text=prepare_text_for_tts(tts, warmup_text),
        ref_codes=ref_codes,
        ref_text=pack.get("refText", ""),
        max_chars=int(pack.get("maxChars", 85)),
        temperature=float(pack.get("temperature", 0.38)),
        top_k=int(pack.get("topK", 16)),
        skip_normalize=True,
        apply_watermark=False,
    )


def parse_time(value: str) -> float:
    value = value.replace(".", ",")
    match = re.search(r"(\d{1,2}):([0-5]?\d):([0-5]?\d)[,.](\d{1,3})", value)
    if not match:
        raise ValueError(f"invalid SRT time: {value}")
    hours, minutes, seconds, millis = match.groups()
    millis = (millis + "000")[:3]
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


def parse_srt(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    timing_re = re.compile(
        r"^\s*(\d{1,2}:[0-5]?\d:[0-5]?\d[,.]\d{1,3})\s*-->\s*"
        r"(\d{1,2}:[0-5]?\d:[0-5]?\d[,.]\d{1,3}).*$",
        re.MULTILINE,
    )
    matches = list(timing_re.finditer(text))
    entries: list[dict] = []
    for idx, match in enumerate(matches):
        try:
            start = parse_time(match.group(1))
            end = parse_time(match.group(2))
        except Exception:
            continue
        if end <= start:
            continue
        body_start = match.end()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        lines = [line.strip() for line in text[body_start:body_end].split("\n") if line.strip()]
        while lines and lines[-1].isdigit():
            lines.pop()
        cue_text = clean_text(" ".join(lines))
        if cue_text:
            entries.append({"start": start, "end": end, "text": cue_text})

    entries.sort(key=lambda row: float(row["start"]))
    fixed: list[dict] = []
    last_end = 0.0
    for item in entries:
        start = float(item["start"])
        end = float(item["end"])
        if start < last_end:
            start = last_end
        if end <= start:
            continue
        fixed.append({"start": start, "end": end, "text": item["text"]})
        last_end = end
    return fixed


def group_timeline_entries(entries: list[dict], max_duration: float = 8.0, max_gap: float = 0.55) -> list[dict]:
    if max_duration <= 0 or max_gap <= 0:
        return entries
    groups: list[dict] = []
    cur: list[dict] = []

    def flush() -> None:
        nonlocal cur
        if not cur:
            return
        groups.append(
            {
                "start": cur[0]["start"],
                "end": cur[-1]["end"],
                "text": " ".join(item["text"] for item in cur),
            }
        )
        cur = []

    for item in entries:
        if not cur:
            cur = [item]
            continue
        gap = item["start"] - cur[-1]["end"]
        duration = item["end"] - cur[0]["start"]
        if 0 <= gap <= max_gap and duration <= max_duration:
            cur.append(item)
        else:
            flush()
            cur = [item]
    flush()
    return groups


def group_srt_smart_entries(entries: list[dict], pack: dict) -> list[dict]:
    if not bool(pack.get("srtSmartGrouping", False)):
        return entries
    max_duration = float(pack.get("srtSmartMaxDuration", 7.5))
    max_gap = float(pack.get("srtSmartMaxGap", 0.30))
    max_chars = int(pack.get("srtSmartMaxChars", 210))
    min_chars = int(pack.get("srtSmartMinChars", 70))
    strong_gap = float(pack.get("srtSmartStrongGap", 0.18))

    def punctuate(text: str, next_gap: float | None = None) -> str:
        text = clean_text(text)
        if not text:
            return ""
        if re.search(r"[.!?,;:\u3002\uff01\uff1f\uff0c\uff1b\uff1a]$", text):
            return text
        if next_gap is not None and next_gap >= strong_gap:
            return text + "."
        if len(text) >= min_chars:
            return text + "."
        return text + ","

    def joined(items: list[dict]) -> str:
        pieces: list[str] = []
        for idx, item in enumerate(items):
            next_gap = None
            if idx + 1 < len(items):
                next_gap = float(items[idx + 1]["start"]) - float(item["end"])
            pieces.append(punctuate(str(item["text"]), next_gap))
        text = clean_text(" ".join(piece for piece in pieces if piece))
        return re.sub(r",\s*$", ".", text)

    groups: list[dict] = []
    cur: list[dict] = []

    def flush() -> None:
        nonlocal cur
        if not cur:
            return
        groups.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": joined(cur), "sourceCount": len(cur)})
        cur = []

    for item in entries:
        if not cur:
            cur = [item]
            continue
        gap = float(item["start"]) - float(cur[-1]["end"])
        candidate = cur + [item]
        candidate_duration = float(candidate[-1]["end"]) - float(candidate[0]["start"])
        candidate_text = joined(candidate)
        current_text = joined(cur)
        can_join = (
            gap >= -0.08
            and gap <= max_gap
            and candidate_duration <= max_duration
            and len(candidate_text) <= max_chars
            and (len(current_text) < min_chars or not re.search(r"[.!?\u3002\uff01\uff1f]$", current_text))
        )
        if can_join:
            cur.append(item)
        else:
            flush()
            cur = [item]
    flush()
    return groups


def group_srt_natural_entries(entries: list[dict], pack: dict) -> list[dict]:
    if not bool(pack.get("srtNaturalGrouping", False)):
        return entries
    max_chars = int(pack.get("srtNaturalMaxChars", 95))
    max_duration = float(pack.get("srtNaturalMaxDuration", 4.2))
    max_gap = float(pack.get("srtNaturalMaxGap", 0.14))
    min_chars = int(pack.get("srtNaturalMinChars", 18))
    groups: list[dict] = []
    cur: list[dict] = []

    def join_srt_lines(items: list[dict]) -> str:
        pieces: list[str] = []
        for item in items:
            text = clean_text(item["text"])
            if not text:
                continue
            if pieces and not re.search(r"[.!?,;:\u3002\uff01\uff1f\uff0c\uff1b\uff1a]$", pieces[-1]):
                pieces[-1] = pieces[-1] + ","
            pieces.append(text)
        return clean_text(" ".join(pieces))

    def joined(items: list[dict]) -> str:
        return join_srt_lines(items)

    def ends_strong(text: str) -> bool:
        return bool(re.search(r"[.!?\u3002\uff01\uff1f]$", clean_text(text)))

    def flush() -> None:
        nonlocal cur
        if cur:
            groups.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": joined(cur)})
            cur = []

    for item in entries:
        if not cur:
            cur = [item]
            continue

        gap = item["start"] - cur[-1]["end"]
        candidate = cur + [item]
        candidate_text = joined(candidate)
        candidate_duration = candidate[-1]["end"] - candidate[0]["start"]
        current_text = joined(cur)

        should_join = (
            gap >= -0.05
            and gap <= max_gap
            and len(candidate_text) <= max_chars
            and candidate_duration <= max_duration
            and not (ends_strong(current_text) and len(current_text) >= min_chars)
        )
        if should_join:
            cur.append(item)
        else:
            flush()
            cur = [item]
    flush()
    return groups


def split_text_units(text: str, max_chars: int) -> list[str]:
    text = clean_text(text)
    if not text:
        return []
    parts = re.split(r"(?<=[.!?,;:\u3002\uff01\uff1f\uff0c\uff1b\uff1a])\s+", text)
    chunks: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        while len(part) > max_chars:
            cut = max(part.rfind(" ", 0, max_chars), max_chars)
            chunks.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            chunks.append(part)
    return chunks


VI_DIGITS = {
    0: "không",
    1: "một",
    2: "hai",
    3: "ba",
    4: "bốn",
    5: "năm",
    6: "sáu",
    7: "bảy",
    8: "tám",
    9: "chín",
}


def vietnamese_number_under_1000(value: int) -> str:
    if value < 10:
        return VI_DIGITS[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        if tens == 1:
            base = "mười"
        else:
            base = f"{VI_DIGITS[tens]} mươi"
        if ones == 0:
            return base
        if ones == 1 and tens > 1:
            return f"{base} mốt"
        if ones == 5:
            return f"{base} lăm"
        return f"{base} {VI_DIGITS[ones]}"
    hundreds, rest = divmod(value, 100)
    base = f"{VI_DIGITS[hundreds]} trăm"
    if rest == 0:
        return base
    if rest < 10:
        return f"{base} lẻ {VI_DIGITS[rest]}"
    return f"{base} {vietnamese_number_under_1000(rest)}"


def normalize_srt_reading_text(text: str) -> str:
    text = clean_text(text)

    def repl_number(match: re.Match) -> str:
        value = int(match.group(0))
        if 0 <= value < 1000:
            return vietnamese_number_under_1000(value)
        return match.group(0)

    text = re.sub(r"\b\d{1,3}\b", repl_number, text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = clean_text(text)
    if text and not re.search(r"[.!?\u3002\uff01\uff1f]$", text):
        text += "."
    return text


def normalize_srt_literal_pronunciation(text: str, pack: dict) -> str:
    text = clean_text(text)
    text = re.sub(r"\blàm\s*điêu\b", "làm điêu", text, flags=re.IGNORECASE)
    text = re.sub(r"\blam\s*dieu\b", "làm điêu", text, flags=re.IGNORECASE)
    text = re.sub(r"\blàm\s*điêu\b", "làm điêu", text, flags=re.IGNORECASE)
    if bool(pack.get("srtExpandNumbers", False)):
        def repl_number(match: re.Match) -> str:
            value = int(match.group(0))
            if 0 <= value < 1000:
                return vietnamese_number_under_1000(value)
            return match.group(0)

        text = re.sub(r"\b\d{1,3}\b", repl_number, text)

    for src, dst in (pack.get("srtPronunciationMap") or {}).items():
        src = clean_text(str(src))
        dst = clean_text(str(dst))
        if src and dst:
            text = re.sub(re.escape(src), dst, text, flags=re.IGNORECASE)
    return clean_text(text)


def split_text_timeline(text: str, max_chars: int) -> list[dict]:
    text = clean_text(text)
    if not text:
        return []
    text = re.sub(r",\s*nhưng\s+lúc\b", ". Nhưng khi", text, flags=re.IGNORECASE)
    text = re.sub(r",\s*nhưng\s+khi\b", ". Nhưng khi", text, flags=re.IGNORECASE)
    segments: list[dict] = []
    parts = re.split(r"(?<=[.!?\u3002\uff01\uff1f])\s+|\n+", text)
    for part in parts:
        part = clean_text(part)
        if not part:
            continue
        soft_conj = re.search(r"\bnhưng\b", part, flags=re.IGNORECASE)
        if soft_conj and len(part) > 65 and soft_conj.start() > 18:
            head = clean_text(part[:soft_conj.start()])
            tail = clean_text(part[soft_conj.start():])
            if head:
                segments.append({"text": head, "pause": 0.08})
            part = tail
        while len(part) > max_chars:
            if len(part) <= max_chars + 28 and not re.search(r"[,;:\u3002\uff01\uff1f\uff0c\uff1b\uff1a]", part[:max_chars]) and not re.search(r"\bnhưng\b", part, flags=re.IGNORECASE):
                break
            candidates = [part.rfind(mark, 0, max_chars) for mark in [",", ";", ":", "，", "；", "："]]
            used_conjunction_cut = False
            conj = re.search(r"\bnhưng\b", part, flags=re.IGNORECASE)
            if conj and max_chars * 0.30 <= conj.start() <= max_chars + 20:
                candidates.append(max(0, conj.start() - 1))
                used_conjunction_cut = True
            cut = max(candidates)
            pause = 0.16
            if cut < max_chars * 0.45 and not used_conjunction_cut:
                cut = part.rfind(" ", 0, max_chars)
                pause = 0.08
            if cut < max_chars * 0.45 and not used_conjunction_cut:
                cut = max_chars
            weak_end_tokens = {"co", "thi", "la", "vao", "va", "nhung", "khi", "lam", "cua", "mot", "nhieu"}
            chunk_tokens = compare_tokens(part[: cut + 1])
            if chunk_tokens and chunk_tokens[-1] in weak_end_tokens:
                next_punct = min([idx for idx in [part.find(mark, cut + 1) for mark in [",", ";", ":", ".", "?", "!"]] if idx != -1] or [-1])
                if next_punct != -1 and next_punct <= max_chars + 32:
                    cut = next_punct
                    pause = 0.16 if part[cut] in ",;:" else 0.24
                elif len(part) <= max_chars + 32:
                    break
            tail = clean_text(part[cut + 1 :])
            if tail and len(tail) < max(12, int(max_chars * 0.28)):
                break
            chunk = clean_text(part[: cut + 1])
            if chunk:
                segments.append({"text": chunk, "pause": pause})
            part = clean_text(part[cut + 1 :])
        if part:
            pause = 0.24 if re.search(r"[.!?\u3002\uff01\uff1f]$", part) else 0.14
            segments.append({"text": part, "pause": pause})
    if segments:
        segments[-1]["pause"] = 0.0
    return segments


def prepare_text_for_tts(tts, text: str) -> str:
    original = rewrite_for_vietnamese_tts(text)
    normalized = clean_text(tts.normalizer.normalize(original))
    if re.search(r"\bm\u00ecnh\b", original, flags=re.IGNORECASE):
        normalized = re.sub(r"\bb\u1ea3n th\u00e2n\b", "m\u00ecnh", normalized, flags=re.IGNORECASE)
    return normalized


def numpy_audio_duration(audio, sample_rate: int = 24000) -> float:
    try:
        return max(0.0, float(len(audio)) / float(sample_rate))
    except Exception:
        return 0.0


def audio_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / float(wav.getframerate())
    except wave.Error:
        proc = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
        if not match:
            raise
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def audio_peak_rms(path: Path, start_sec: float = 0.0, end_sec: float | None = None) -> tuple[float, float]:
    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            total = wav.getnframes()
            start_frame = max(0, min(total, int(start_sec * rate)))
            end_frame = total if end_sec is None else max(start_frame, min(total, int(end_sec * rate)))
            wav.setpos(start_frame)
            raw = wav.readframes(end_frame - start_frame)
    except wave.Error:
        duration_args = []
        if start_sec > 0:
            duration_args += ["-ss", f"{start_sec:.6f}"]
        if end_sec is not None and end_sec > start_sec:
            duration_args += ["-t", f"{end_sec - start_sec:.6f}"]
        raw = subprocess.check_output(
            [
                str(FFMPEG),
                "-hide_banner",
                "-loglevel",
                "error",
                *duration_args,
                "-i",
                str(path),
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                "24000",
                "-",
            ]
        )
        channels = 1
        width = 2
    if not raw:
        return 0.0, 0.0
    if width == 2:
        samples = array("h")
        samples.frombytes(raw)
        scale = 32768.0
    else:
        return 0.0, 0.0
    if channels > 1:
        samples = array("h", samples[::channels])
    if not samples:
        return 0.0, 0.0
    peak = max(abs(x) for x in samples) / scale
    rms = math.sqrt(sum((x / scale) ** 2 for x in samples) / len(samples))
    return peak, rms


def atempo_filter(speed: float) -> str:
    speed = max(0.25, min(4.0, speed))
    parts: list[str] = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.5f}")
    return ",".join(parts)


def fit_clip_to_duration(path: Path, target_sec: float) -> None:
    if target_sec <= 0:
        return
    current = audio_duration(path)
    if current <= 0:
        return
    tmp = path.with_name(path.stem + "_fit_tmp" + path.suffix)
    if current > target_sec * 1.03:
        filters = f"{atempo_filter(current / target_sec)},atrim=0:{target_sec:.3f},asetpts=N/SR/TB"
    else:
        filters = f"apad,atrim=0:{target_sec:.3f},asetpts=N/SR/TB"
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            filters,
            str(tmp),
        ],
        check=True,
    )
    tmp.replace(path)


def apply_tempo(path: Path, speed: float) -> None:
    if abs(speed - 1.0) < 0.01:
        return
    tmp = path.with_name(path.stem + "_tempo_tmp" + path.suffix)
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            atempo_filter(speed),
            str(tmp),
        ],
        check=True,
    )
    tmp.replace(path)


def remove_tail_artifact(path: Path, pack: dict, mode: str = "text", text: str = "") -> bool:
    """Remove a fixed generated tail artifact without touching the head.

    The filter mirrors the approved A/B test: reverse audio, trim a small
    amount from the generated tail, add a soft fade on the new edge, reverse
    back, then append a short silent pad so exported clips do not end abruptly.
    """
    remove_sec = float(pack.get(f"{mode}TailRemoveSec", pack.get("tailRemoveSec", 0.0)) or 0.0)
    if remove_sec <= 0:
        return False
    max_short_words = int(pack.get(f"{mode}TailRemoveSkipMaxWords", pack.get("tailRemoveSkipMaxWords", 0)) or 0)
    short_words = int(pack.get(f"{mode}TailRemoveShortMaxWords", pack.get("tailRemoveShortMaxWords", 0)) or 0)
    short_remove = pack.get(f"{mode}TailRemoveShortSec", pack.get("tailRemoveShortSec", None))
    skip_tokens_raw = str(pack.get(f"{mode}TailRemoveSkipFinalTokens", pack.get("tailRemoveSkipFinalTokens", "")) or "")
    skip_tokens = {clean_text(item).lower() for item in re.split(r"[,;|]+", skip_tokens_raw) if clean_text(item)}
    word_count = len(compare_tokens(text)) if text else 0
    if max_short_words > 0 and text:
        if word_count <= max_short_words:
            print(
                "VieNeu tail artifact cleanup skipped: "
                f"{path.name} short_text_words={word_count} limit={max_short_words}"
            )
            return False
    if skip_tokens and text:
        token = final_token(text).lower()
        if token in skip_tokens:
            print(
                "VieNeu tail artifact cleanup skipped: "
                f"{path.name} final_token={token}"
            )
            return False
    if short_words > 0 and word_count > 0 and word_count <= short_words and short_remove is not None:
        remove_sec = max(0.0, float(short_remove))
        if remove_sec <= 0:
            print(
                "VieNeu tail artifact cleanup skipped: "
                f"{path.name} short_text_words={word_count} short_remove=0"
            )
            return False
    duration = audio_duration(path)
    min_after = float(pack.get(f"{mode}TailRemoveMinDurationSec", pack.get("tailRemoveMinDurationSec", 0.75)))
    if duration <= remove_sec + min_after:
        print(
            "VieNeu tail artifact cleanup skipped: "
            f"{path.name} duration={duration:.3f}s remove={remove_sec:.3f}s"
        )
        return False
    fade_sec = float(pack.get(f"{mode}TailRemoveFadeSec", pack.get("tailRemoveFadeSec", 0.045)))
    pad_sec = float(pack.get(f"{mode}TailRemovePadSec", pack.get("tailRemovePadSec", 0.10)))
    tmp = path.with_name(path.stem + "_tailfix_tmp" + path.suffix)
    filters = (
        f"areverse,atrim=start={remove_sec:.3f},"
        f"afade=t=in:st=0:d={fade_sec:.3f},"
        f"areverse,apad=pad_dur={pad_sec:.3f},"
        "alimiter=limit=0.92"
    )
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            filters,
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
        print(
            "VieNeu tail artifact cleanup: "
            f"{path.name} remove={remove_sec:.3f}s fade={fade_sec:.3f}s pad={pad_sec:.3f}s"
        )
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def normalize_loudness(path: Path, pack: dict, mode: str = "text") -> bool:
    key = f"{mode}NormalizeLoudness"
    if not bool(pack.get(key, False)):
        return False
    target_i = float(pack.get(f"{mode}LoudnessI", pack.get("targetLoudnessI", -18.0)))
    target_tp = float(pack.get(f"{mode}LoudnessTP", pack.get("targetLoudnessTP", -2.0)))
    target_lra = float(pack.get(f"{mode}LoudnessLRA", pack.get("targetLoudnessLRA", 9.0)))
    tmp = path.with_name(path.stem + "_loud_tmp" + path.suffix)
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:linear=true,alimiter=limit=0.92",
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def make_silence(path: Path, duration: float = 0.12) -> None:
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-t",
            f"{duration:.3f}",
            str(path),
        ],
        check=True,
    )


def remove_midphrase_long_pauses(path: Path, max_pause: float = 0.35) -> None:
    if max_pause <= 0:
        return
    tmp = path.with_name(path.stem + "_pause_tmp" + path.suffix)
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            f"silenceremove=stop_periods=-1:stop_duration={max_pause:.3f}:stop_threshold=-45dB",
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
    elif tmp.exists():
        tmp.unlink()


def pause_for_text_segment(segment_text: str, pack: dict) -> float:
    text = clean_text(segment_text)
    if re.search(r"[.!?\u3002\uff01\uff1f]$", text):
        desired = float(pack.get("textPauseSentenceSec", 0.38))
    elif re.search(r"[,;:\uff0c\uff1b\uff1a]$", text):
        desired = float(pack.get("textPauseCommaSec", 0.20))
    else:
        desired = float(pack.get("textPauseWeakSec", 0.14))
    if bool(pack.get("textPauseNormalizeEdges", False)):
        desired -= float(pack.get("textPauseTailPadSec", 0.075))
        desired -= float(pack.get("textPauseHeadPadSec", 0.035))
    return max(0.0, desired)


def normalize_clip_edges_by_asr(path: Path, asr_model, pack: dict, is_final: bool = False) -> bool:
    if not bool(pack.get("textPauseNormalizeEdges", False)) or asr_model is None:
        return False
    first_start, last_end = word_edge_times(asr_model, path)
    if first_start is None or last_end is None:
        return False
    duration = audio_duration(path)
    head_pad = float(pack.get("textPauseHeadPadSec", 0.035))
    tail_pad = float(pack.get("textPauseTailPadSec", 0.075 if not is_final else 0.12))
    start = max(0.0, first_start - head_pad)
    end = min(duration, last_end + tail_pad)
    if end <= start + 0.35:
        return False
    changed = start > 0.035 or (duration - end) > 0.035
    if not changed:
        return False
    tmp = path.with_name(path.stem + "_edge_norm_tmp" + path.suffix)
    filters = [
        f"atrim=start={start:.3f}:end={end:.3f}",
        "asetpts=N/SR/TB",
        f"afade=t=in:st=0:d={float(pack.get('textPauseEdgeFadeSec', 0.025)):.3f}",
        f"afade=t=out:st={max(0.0, end - start - float(pack.get('textPauseEdgeFadeSec', 0.025))):.3f}:d={float(pack.get('textPauseEdgeFadeSec', 0.025)):.3f}",
    ]
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            ",".join(filters),
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
        print(
            "VieNeu pause normalize edges: "
            f"{path.name} start={start:.3f}s end={end:.3f}s duration={duration:.3f}s"
        )
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def final_token(text: str) -> str:
    tokens = compare_tokens(text)
    return tokens[-1] if tokens else ""


def adaptive_tail_pad(pack: dict, text: str, base_key: str, default: float, token_key: str | None = None) -> float:
    base = float(pack.get(base_key, default))
    if token_key:
        by_token = pack.get(token_key, {}) or {}
        token = final_token(text)
        if token in by_token:
            base = max(base, float(by_token[token]))
    return base


def effective_srt_tail_pack(pack: dict, target_sec: float | None = None, text: str = "") -> dict:
    """Return tail-cleanup settings adjusted for long SRT slots."""
    target = float(target_sec or 0.0)
    token_count = len(compare_tokens(text)) if text else 0
    has_punctuation_break = bool(re.search(r"[,;:\uff0c\uff1b\uff1a]", text or ""))
    long_by_target = target >= float(pack.get("longVariantMinTargetSec", 9999.0))
    long_by_text = token_count >= int(pack.get("longVariantMinWords", 999999))
    long_by_punct = has_punctuation_break and token_count >= int(pack.get("longVariantPunctuationMinWords", 999999))
    if not (long_by_target or long_by_text or long_by_punct):
        return pack
    adjusted = dict(pack)
    long_skip = str(pack.get("longVariantTailRemoveSkipFinalTokens", "") or "")
    if long_skip:
        adjusted["tailRemoveSkipFinalTokens"] = long_skip
        adjusted["textTailRemoveSkipFinalTokens"] = long_skip
        adjusted["srtTailRemoveSkipFinalTokens"] = long_skip
    if "longVariantTailRemoveSec" in pack:
        adjusted["tailRemoveSec"] = float(pack["longVariantTailRemoveSec"])
        adjusted["textTailRemoveSec"] = float(pack["longVariantTailRemoveSec"])
        adjusted["srtTailRemoveSec"] = float(pack["longVariantTailRemoveSec"])
    return adjusted


def render_pack_text(pack: dict, text: str, out_wav: Path, asr_model_name: str | None = None) -> None:
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if asr_model_name is None and bool(pack.get("boundaryCleanupAsr", False)):
        asr_model_name = str(pack.get("boundaryCleanupAsrModel", "small"))
    asr_model = load_asr_model(asr_model_name) if asr_model_name else None
    tts = Vieneu(
        mode="standard",
        backbone_repo=pack.get("model", "pnnbao-ump/VieNeu-TTS-0.3B"),
        gguf_filename=pack.get("ggufFilename", None),
        backbone_device=device,
        codec_repo=pack.get("codec", "neuphonic/distill-neucodec"),
        codec_device=device,
    )
    if pack.get("loraDir"):
        tts.load_lora_adapter(str(Path(pack["loraDir"])))
    dtype_name = clean_text(str(pack.get("backboneDtype", ""))).lower()
    if device == "cuda" and dtype_name in {"bf16", "bfloat16", "fp16", "float16"}:
        dtype = torch.bfloat16 if dtype_name in {"bf16", "bfloat16"} else torch.float16
        print(f"VieNeu optimize: backbone dtype={dtype_name}")
        tts.backbone.to(dtype=dtype)
    if pack.get("maxContext"):
        tts.max_context = max(256, int(pack.get("maxContext", tts.max_context)))
        print(f"VieNeu optimize: max_context={tts.max_context}")
    ref_codes = load_ref_codes_for_pack(tts, pack)
    warmup_tts(tts, pack, ref_codes)
    if bool(pack.get("textSingleUtteranceMode", False)):
        text_segments = [{"text": clean_text(text), "pause": 0.0}]
    else:
        text_segments = split_text_timeline(text, int(pack.get("maxChars", 70)))
    if bool(pack.get("textPauseNormalize", False)):
        for index, segment in enumerate(text_segments):
            segment["pause"] = 0.0 if index == len(text_segments) - 1 else pause_for_text_segment(segment["text"], pack)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if len(text_segments) <= 1:
        only_text = text_segments[0]["text"] if text_segments else text
        infer_one_clip(tts, pack, ref_codes, only_text, out_wav, asr_model=asr_model)
        apply_tempo(out_wav, float(pack.get("textSpeechSpeed", pack.get("speechSpeed", 1.0))))
        remove_tail_artifact(out_wav, effective_srt_tail_pack(pack, None, only_text), "text", only_text)
        normalize_loudness(out_wav, pack, "text")
        smooth_audio_file(out_wav, add_lead_silence=True)
        finalize_clip_edges(out_wav, pack, "text")
    else:
        with tempfile.TemporaryDirectory(prefix="vieneu_text_") as tmp:
            tmp_dir = Path(tmp)
            wav_files = []
            for i, segment in enumerate(text_segments, start=1):
                clip = tmp_dir / f"text_{i:03d}.wav"
                infer_one_clip(tts, pack, ref_codes, segment["text"], clip, asr_model=asr_model)
                smooth_audio_file(clip)
                if i > 1:
                    cleanup_initial_noise_by_asr(clip, asr_model, pack)
                if i < len(text_segments):
                    cleanup_nonfinal_tail_by_asr(clip, asr_model, pack)
                    trim_nonfinal_tail(
                        clip,
                        float(pack.get("midClipTrimTailSec", 0.0)),
                        float(pack.get("midClipTailFadeSec", 0.06)),
                    )
                remove_short_noise_islands(clip, pack)
                normalize_clip_silence_edges(clip, pack, is_final=i == len(text_segments))
                normalize_clip_edges_by_asr(clip, asr_model, pack, is_final=i == len(text_segments))
                wav_files.append(clip)
                if segment["pause"] > 0:
                    silence = tmp_dir / f"pause_{i:03d}.wav"
                    make_silence(silence, float(segment["pause"]))
                    wav_files.append(silence)
            list_path = tmp_dir / "inputs.txt"
            list_path.write_text("".join(f"file '{p.as_posix()}'\n" for p in wav_files), encoding="utf-8")
            subprocess.run(
                [
                    str(FFMPEG),
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_path),
                    str(out_wav),
                ],
                check=True,
            )
            remove_short_noise_islands(out_wav, pack)
            limit_internal_silences(out_wav, pack)
            apply_tempo(out_wav, float(pack.get("textSpeechSpeed", pack.get("speechSpeed", 1.0))))
            remove_tail_artifact(out_wav, effective_srt_tail_pack(pack, None, text), "text", text)
            normalize_loudness(out_wav, pack, "text")
            apply_post_declick(out_wav, pack)
            smooth_audio_file(out_wav, add_lead_silence=True)
            finalize_clip_edges(out_wav, pack, "text")
    tts.close()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()


def load_pack(pack_dir: Path) -> dict:
    pack_json = pack_dir / "pack.json"
    if not pack_json.exists():
        raise FileNotFoundError(pack_json)
    pack = json.loads(pack_json.read_text(encoding="utf-8-sig"))
    pack["_pack_dir"] = str(pack_dir.resolve())
    return pack


def resolve_pack_file(pack: dict, key: str, fallback_key: str | None = None) -> Path | None:
    value = pack.get(key)
    if not value and fallback_key:
        value = pack.get(fallback_key)
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        base = Path(str(pack.get("_pack_dir") or "."))
        path = base / path
    return path


def is_valid_lora_dir(path: Path | None) -> bool:
    if path is None or not path.exists() or not path.is_dir():
        return False
    has_config = (path / "adapter_config.json").exists()
    has_weights = (path / "adapter_model.safetensors").exists() or (path / "adapter_model.bin").exists()
    return has_config and has_weights


def resolve_lora_dir_for_render(pack: dict) -> Path | None:
    raw = clean_text(str(pack.get("loraDir", "")))
    primary = Path(raw) if raw else None
    if is_valid_lora_dir(primary):
        return primary

    candidates: list[Path] = []
    for value in pack.get("fallbackLoraDirs", []) or []:
        text = clean_text(str(value))
        if text:
            candidates.append(Path(text))
    env_fallback = clean_text(os.environ.get("VIENEU_FALLBACK_LORA", ""))
    if env_fallback:
        candidates.append(Path(env_fallback))

    root = Path(__file__).resolve().parents[1]
    known_recover = root / "vieneu_work" / "lora" / "thanh_thao_vieneu_lora_recover_20260525" / "checkpoint-1500"
    if "thanh" in clean_text(str(pack.get("id", pack.get("name", "")))).lower():
        candidates.append(known_recover)

    for candidate in candidates:
        if is_valid_lora_dir(candidate):
            if primary and primary != candidate:
                print(f"VieNeu lora fallback: primary invalid={primary} -> {candidate}")
            return candidate

    if primary:
        raise FileNotFoundError(
            f"LoRA adapter khong hop le: {primary}. Can adapter_config.json va adapter_model.safetensors/bin."
        )
    return None


def load_ref_codes_for_pack(tts, pack: dict):
    ref_text_path = resolve_pack_file(pack, "ref_text")
    if ref_text_path and ref_text_path.exists():
        pack["refText"] = ref_text_path.read_text(encoding="utf-8-sig", errors="replace").strip()

    ref_codes_path = resolve_pack_file(pack, "ref_codes")
    if ref_codes_path and ref_codes_path.exists():
        data = torch.load(str(ref_codes_path), map_location="cuda" if torch.cuda.is_available() else "cpu")
        codes = data.get("codes") if isinstance(data, dict) else data
        if torch.is_tensor(codes) and torch.cuda.is_available():
            codes = codes.cuda()
        if isinstance(data, dict) and data.get("ref_text") and not clean_text(str(pack.get("refText", ""))):
            pack["refText"] = str(data["ref_text"])
        print(f"VieNeu ref_codes: loaded precomputed {ref_codes_path}")
        return codes

    ref_audio = resolve_pack_file(pack, "refAudio", "ref_audio")
    if ref_audio is None:
        raise FileNotFoundError("Voice pack missing refAudio/ref_audio and ref_codes")
    print(f"VieNeu ref_codes: encode fallback {ref_audio}")
    return tts.encode_reference(str(ref_audio))


def atomic_copy_audio(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=dst.suffix or ".wav", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)
        if tmp.stat().st_size <= 1024:
            raise RuntimeError(f"audio cache temp file too small: {tmp}")
        os.replace(tmp, dst)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def mix_timeline_audio(items: list[tuple[Path, float]], out_path: Path, tmp_dir: Path, pack: dict) -> None:
    if not items:
        raise RuntimeError("timeline khong co audio de mix")
    chunk_size = max(1, int(pack.get("srtMixChunkSize", 80)))
    chunk_outputs: list[tuple[Path, float]] = []
    chunk_specs = list(enumerate(range(0, len(items), chunk_size), start=1))

    def render_mix_chunk(spec: tuple[int, int]) -> tuple[Path, float]:
        chunk_index, start_index = spec
        chunk = items[start_index : start_index + chunk_size]
        chunk_start = min(start for _path, start in chunk)
        chunk_out = tmp_dir / f"mix_chunk_{chunk_index:04d}.wav"
        cmd = [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error"]
        for path, _start in chunk:
            cmd += ["-i", str(path)]
        filters = []
        labels = []
        for input_index, (_path, start) in enumerate(chunk):
            delay_ms = max(0, int(round((start - chunk_start) * 1000)))
            label = f"a{input_index}"
            filters.append(f"[{input_index}:a]adelay={delay_ms}|{delay_ms}[{label}]")
            labels.append(f"[{label}]")
        if len(labels) == 1:
            filters.append(f"{labels[0]}anull[out]")
        else:
            filters.append(
                f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
                "alimiter=limit=0.92[out]"
            )
        cmd += ["-filter_complex", ";".join(filters), "-map", "[out]", str(chunk_out)]
        subprocess.run(cmd, check=True)
        return chunk_out, chunk_start

    max_mix_workers = max(1, int(pack.get("srtMixWorkers", min(2, _CPU_WORKERS))))
    if len(chunk_specs) > 1 and max_mix_workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_mix_workers) as pool:
            for result in pool.map(render_mix_chunk, chunk_specs):
                chunk_outputs.append(result)
        chunk_outputs.sort(key=lambda row: row[1])
    else:
        for spec in chunk_specs:
            chunk_outputs.append(render_mix_chunk(spec))

    if len(chunk_outputs) == 1 and chunk_outputs[0][1] <= 0.02:
        source = chunk_outputs[0][0]
        tmp_mp3 = out_path.with_name(f".{out_path.name}.tmp.mp3")
        subprocess.run(
            [
                str(FFMPEG),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                str(tmp_mp3),
            ],
            check=True,
        )
        os.replace(tmp_mp3, out_path)
        return

    cmd = [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error"]
    for path, _start in chunk_outputs:
        cmd += ["-i", str(path)]
    filters = []
    labels = []
    for input_index, (_path, start) in enumerate(chunk_outputs):
        delay_ms = max(0, int(round(start * 1000)))
        label = f"c{input_index}"
        filters.append(f"[{input_index}:a]adelay={delay_ms}|{delay_ms}[{label}]")
        labels.append(f"[{label}]")
    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
        "alimiter=limit=0.92[out]"
    )
    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[out]",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(out_path.with_name(f".{out_path.name}.tmp.mp3")),
    ]
    subprocess.run(cmd, check=True)
    os.replace(out_path.with_name(f".{out_path.name}.tmp.mp3"), out_path)


def prepare_srt_render_text(pack: dict, text: str) -> str:
    render_text = text
    if bool(pack.get("srtLiteralPronunciationFix", False)):
        render_text = normalize_srt_literal_pronunciation(render_text, pack)
    if bool(pack.get("srtNormalizeReadingText", True)):
        render_text = normalize_srt_reading_text(render_text)
    return render_text


def srt_cache_settings(pack: dict) -> dict:
    keys = [
        "loraDir",
        "refAudio",
        "refText",
        "temperature",
        "topK",
        "srtSpeechSpeed",
        "clipUseInferBatch",
        "srtUseTextClipPipeline",
        "srtFinalCropToExpected",
        "srtFinalCropHeadPadSec",
        "srtFinalCropTailPadSec",
        "srtFinalCropTailPadSecByToken",
        "srtTextPauseHeadPadSec",
        "srtTextPauseTailPadSec",
        "srtTextPauseTailPadSecByToken",
        "srtTrimHeadSec",
        "srtTrimTailSec",
        "srtFinalFadeInSec",
        "srtFinalFadeOutSec",
        "srtFinalTailSilenceSec",
        "srtRejectTailVoice",
        "srtFinalRejectShortFinalWord",
        "srtFinalAsrCoverageMin",
        "srtCacheVersion",
    ]
    return {key: pack.get(key) for key in keys if key in pack}


def cue_cache_signature(pack: dict, render_text: str) -> str:
    payload = {
        "version": pack.get("fastTtsCacheVersion", pack.get("srtCacheVersion", "v1")),
        "renderText": clean_text(render_text),
        "settings": srt_cache_settings(pack),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cue_cache_root(pack: dict) -> Path | None:
    configured = clean_text(str(pack.get("fastTtsCacheDir", "")))
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            root = Path(str(pack.get("_pack_dir") or ".")) / root
        return root / "wavs"
    pack_dir = clean_text(str(pack.get("_pack_dir", "")))
    if not pack_dir:
        return None
    return Path(pack_dir) / "_FAST_TTS_CACHE_DO_NOT_DELETE" / "cue_cache" / "wavs"


def cue_cache_paths(pack: dict, signature: str) -> tuple[Path, Path] | tuple[None, None]:
    root = cue_cache_root(pack)
    if root is None:
        return None, None
    folder = root / signature[:2]
    return folder / f"{signature}.wav", folder / f"{signature}.json"


def valid_cue_cached_clip(clip: Path | None, meta_path: Path | None, render_text: str, signature: str) -> bool:
    if clip is None or meta_path is None or not clip.exists() or clip.stat().st_size <= 1024:
        return False
    if not meta_path.exists():
        return True
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return (
        meta.get("signature") == signature
        and clean_text(str(meta.get("renderText", ""))) == clean_text(render_text)
        and float(meta.get("duration", 0.0) or 0.0) > 0.05
    )


def resolve_cue_cached_clip(pack: dict, render_text: str) -> Path | None:
    if not bool(pack.get("srtUsePersistentCueCache", True)):
        return None
    signature = cue_cache_signature(pack, render_text)
    clip, meta = cue_cache_paths(pack, signature)
    return clip if valid_cue_cached_clip(clip, meta, render_text, signature) else None


def write_cue_cached_clip(pack: dict, render_text: str, src: Path, report: dict | None) -> None:
    if not bool(pack.get("srtUsePersistentCueCache", True)) or not src.exists():
        return
    signature = cue_cache_signature(pack, render_text)
    clip, meta = cue_cache_paths(pack, signature)
    if clip is None or meta is None:
        return
    atomic_copy_audio(src, clip)
    meta.write_text(
        json.dumps(
            {
                "signature": signature,
                "renderText": clean_text(render_text),
                "duration": round(audio_duration(clip), 3),
                "asrOk": ((report or {}).get("asr") or {}).get("ok"),
                "asrCoverage": ((report or {}).get("asr") or {}).get("coverage"),
                "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def srt_cache_signature(pack: dict, item: dict, render_text: str) -> str:
    payload = {
        "version": pack.get("srtCacheVersion", "v1"),
        "start": round(float(item["start"]), 3),
        "end": round(float(item["end"]), 3),
        "text": clean_text(str(item["text"])),
        "renderText": clean_text(render_text),
        "settings": srt_cache_settings(pack),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def srt_cache_paths(clip_dir: Path, index: int, start: float, signature: str) -> tuple[Path, Path]:
    stem = f"clip_{index:05d}_{float(start):.3f}s_{signature}"
    return clip_dir / f"{stem}.wav", clip_dir / f"{stem}.json"


def srt_cache_meta(pack: dict, index: int, item: dict, render_text: str, signature: str, wav_path: Path, report: dict | None) -> dict:
    duration = audio_duration(wav_path) if wav_path.exists() else 0.0
    asr = (report or {}).get("asr") or {}
    return {
        "cacheVersion": pack.get("srtCacheVersion", "v1"),
        "signature": signature,
        "index": index,
        "start": round(float(item["start"]), 3),
        "end": round(float(item["end"]), 3),
        "text": clean_text(str(item["text"])),
        "renderText": clean_text(render_text),
        "duration": round(duration, 3),
        "asrCoverage": asr.get("coverage"),
        "asrOk": asr.get("ok"),
        "strictFinalQa": bool(pack.get("srtStrictFinalQa", False)),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def write_srt_cached_clip(pack: dict, clip: Path, meta_path: Path, index: int, item: dict, render_text: str, signature: str, src: Path, report: dict | None) -> None:
    atomic_copy_audio(src, clip)
    meta = srt_cache_meta(pack, index, item, render_text, signature, clip, report)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def valid_srt_cached_clip(pack: dict, clip: Path, meta_path: Path, index: int, item: dict, render_text: str, signature: str) -> bool:
    if not clip.exists() or clip.stat().st_size <= 1024:
        return False
    if bool(pack.get("srtAllowLegacyCache", False)) and not meta_path.exists():
        return True
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if meta.get("signature") != signature:
        return False
    if meta.get("cacheVersion") != pack.get("srtCacheVersion", "v1"):
        return False
    if int(meta.get("index", -1)) != int(index):
        return False
    if abs(float(meta.get("start", -999.0)) - float(item["start"])) > 0.002:
        return False
    if clean_text(str(meta.get("renderText", ""))) != clean_text(render_text):
        return False
    if float(meta.get("duration", 0.0) or 0.0) <= 0.05:
        return False
    if bool(pack.get("srtStrictCacheQa", True)) and meta.get("asrOk") is False:
        return False
    return True


def resolve_srt_cached_clip(pack: dict, clip_dir: Path, index: int, item: dict, render_text: str, signature: str) -> tuple[Path | None, Path | None]:
    cached, cached_meta = srt_cache_paths(clip_dir, index, float(item["start"]), signature)
    if valid_srt_cached_clip(pack, cached, cached_meta, index, item, render_text, signature):
        return cached, cached_meta
    if bool(pack.get("srtAllowLegacyCache", False)):
        legacy = clip_dir / f"clip_{index:05d}_{float(item['start']):.3f}s.wav"
        legacy_meta = legacy.with_suffix(".json")
        if valid_srt_cached_clip(pack, legacy, legacy_meta, index, item, render_text, signature):
            return legacy, legacy_meta
    return None, None


def render_srt(pack: dict, srt_path: Path, out_mp3: Path) -> None:
    started_at = time.perf_counter()
    entries = parse_srt(srt_path)
    if not entries:
        raise RuntimeError(f"SRT khong co subtitle hop le: {srt_path}")
    entries = group_timeline_entries(
        entries,
        max_duration=float(pack.get("maxGroupDuration", 8.0)),
        max_gap=float(pack.get("maxGroupGap", 0.55)),
    )
    before_smart = len(entries)
    entries = group_srt_smart_entries(entries, pack)
    if len(entries) != before_smart:
        print(f"VieNeu SRT smart grouping: {before_smart} -> {len(entries)}")
    before_natural = len(entries)
    entries = group_srt_natural_entries(entries, pack)
    if len(entries) != before_natural:
        print(f"VieNeu SRT natural grouping: {before_natural} -> {len(entries)}")
    if bool(pack.get("srtOmniBench", True)):
        print(
            "VieNeu bench setup: "
            f"entries={len(entries)} cache={bool(pack.get('srtUsePersistentCueCache', True))} "
            f"batch={bool(pack.get('srtBatchInfer', False))} quality={bool(pack.get('srtBatchQualityMode', False))}"
        )

    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    progress_path = out_mp3.with_suffix(".progress.json")
    configured_clip_dir = clean_text(str(pack.get("srtSaveClipDir", "")))
    reusable_clip_dir = Path(configured_clip_dir) if configured_clip_dir else out_mp3.with_name(out_mp3.stem + "_clips")
    if bool(pack.get("srtSaveDebugClips", False)) and bool(pack.get("srtReuseDebugClips", True)):
        cached_paths: list[Path] = []
        for index, item in enumerate(entries, start=1):
            render_text = prepare_srt_render_text(pack, str(item["text"]))
            signature = srt_cache_signature(pack, item, render_text)
            cached, _cached_meta = resolve_srt_cached_clip(pack, reusable_clip_dir, index, item, render_text, signature)
            if cached is None:
                cached_paths = []
                break
            cached_paths.append(cached)
        if cached_paths:
            print(f"VieNeu SRT cached: all {len(cached_paths)} clips available, skip model load")
            with tempfile.TemporaryDirectory(prefix="vieneu_srt_cached_") as tmp:
                tmp_dir = Path(tmp)
                timeline_items: list[tuple[Path, float]] = []
                for index, (item, cached) in enumerate(zip(entries, cached_paths), start=1):
                    clip = tmp_dir / f"clip_{index:05d}.wav"
                    shutil.copy2(cached, clip)
                    render_text = prepare_srt_render_text(pack, str(item["text"]))
                    write_cue_cached_clip(pack, render_text, cached, {"asr": {"ok": True, "coverage": None}})
                    timeline_items.append((clip, float(item["start"])))
                mix_timeline_audio(timeline_items, out_mp3, tmp_dir, pack)
            progress_path.write_text(
                json.dumps(
                    {
                        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "doneClips": len(entries),
                        "totalClips": len(entries),
                        "out": str(out_mp3),
                        "allCached": True,
                        "elapsedSec": round(time.perf_counter() - started_at, 3),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"VieNeu SRT cached done: {out_mp3}")
            return

    if bool(pack.get("srtUsePersistentCueCache", True)):
        cached_paths = []
        for item in entries:
            render_text = prepare_srt_render_text(pack, str(item["text"]))
            cached = resolve_cue_cached_clip(pack, render_text)
            if cached is None:
                cached_paths = []
                break
            cached_paths.append(cached)
        if cached_paths:
            print(f"VieNeu persistent cue cache: all {len(cached_paths)} clips available, skip model load")
            with tempfile.TemporaryDirectory(prefix="vieneu_srt_cue_cached_") as tmp:
                tmp_dir = Path(tmp)
                timeline_items: list[tuple[Path, float]] = []
                for index, (item, cached) in enumerate(zip(entries, cached_paths), start=1):
                    clip = tmp_dir / f"clip_{index:05d}.wav"
                    shutil.copy2(cached, clip)
                    timeline_items.append((clip, float(item["start"])))
                mix_timeline_audio(timeline_items, out_mp3, tmp_dir, pack)
            progress_path.write_text(
                json.dumps(
                    {
                        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "doneClips": len(entries),
                        "totalClips": len(entries),
                        "out": str(out_mp3),
                        "allPersistentCueCached": True,
                        "elapsedSec": round(time.perf_counter() - started_at, 3),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"VieNeu persistent cue cached done: {out_mp3}")
            return

    device = "cuda" if torch.cuda.is_available() else "cpu"
    asr_model = load_asr_model(str(pack.get("srtAsrModel", pack.get("boundaryCleanupAsrModel", "small")))) if bool(pack.get("srtQualityGate", True)) else None
    print(f"VieNeu local: device={device} entries={len(entries)}")
    vieneu_mode = clean_text(str(pack.get("vieneuMode", "standard"))) or "standard"
    if vieneu_mode in {"fast", "gpu"} and pack.get("lmdeployDtype"):
        os.environ["VIENEU_LMDEPLOY_DTYPE"] = clean_text(str(pack.get("lmdeployDtype", "float16"))) or "float16"
        print(f"VieNeu optimize: lmdeploy dtype={os.environ['VIENEU_LMDEPLOY_DTYPE']}")
    tts_kwargs = {
        "backbone_repo": pack.get("model", "pnnbao-ump/VieNeu-TTS-0.3B"),
        "backbone_device": device,
        "codec_repo": pack.get("codec", "neuphonic/distill-neucodec"),
        "codec_device": device,
    }
    if vieneu_mode == "standard":
        tts_kwargs["gguf_filename"] = pack.get("ggufFilename", None)
    if vieneu_mode in {"fast", "gpu"}:
        tts_kwargs["memory_util"] = float(pack.get("lmdeployMemoryUtil", 0.55))
        tts_kwargs["max_batch_size"] = int(pack.get("srtBatchSize", pack.get("batchSize", 4)))
    tts = Vieneu(mode=vieneu_mode, **tts_kwargs)
    if vieneu_mode == "standard":
        lora_dir = resolve_lora_dir_for_render(pack)
        if lora_dir is not None:
            pack["loraDir"] = str(lora_dir)
            tts.load_lora_adapter(str(lora_dir))
    dtype_name = clean_text(str(pack.get("backboneDtype", ""))).lower()
    if device == "cuda" and dtype_name in {"bf16", "bfloat16", "fp16", "float16"}:
        dtype = torch.bfloat16 if dtype_name in {"bf16", "bfloat16"} else torch.float16
        print(f"VieNeu optimize: backbone dtype={dtype_name}")
        tts.backbone.to(dtype=dtype)
    if pack.get("maxContext"):
        tts.max_context = max(256, int(pack.get("maxContext", tts.max_context)))
        print(f"VieNeu optimize: max_context={tts.max_context}")
    ref_codes = load_ref_codes_for_pack(tts, pack)
    warmup_tts(tts, pack, ref_codes)
    model_ready_sec = time.perf_counter() - started_at
    print(f"VieNeu timing: model_ready={model_ready_sec:.1f}s")

    with tempfile.TemporaryDirectory(prefix="vieneu_srt_") as tmp:
        tmp_dir = Path(tmp)
        timeline_items: list[tuple[Path, float]] = []
        clip_reports: list[dict] = []
        clip_save_dir: Path | None = None
        if bool(pack.get("srtSaveDebugClips", False)):
            configured = clean_text(str(pack.get("srtSaveClipDir", "")))
            clip_save_dir = Path(configured) if configured else out_mp3.with_name(out_mp3.stem + "_clips")
            clip_save_dir.mkdir(parents=True, exist_ok=True)
        srt_speed = float(pack.get("srtSpeechSpeed", pack.get("speechSpeed", 1.0)))
        overlap_mode = bool(pack.get("srtAllowOverlap", True))
        use_text_clip_pipeline = bool(pack.get("srtUseTextClipPipeline", False))
        ultra_fast = bool(pack.get("srtUltraFast", False))
        batch_infer = (
            bool(pack.get("srtBatchInfer", False))
            and hasattr(tts, "infer_batch")
        )
        render_loop_start = time.perf_counter()
        if batch_infer:
            batch_size = max(1, int(pack.get("srtBatchSize", pack.get("batchSize", 4))))
            sample_rate = int(getattr(tts, "sample_rate", 24000) or 24000)
            print(f"VieNeu SRT batch infer: batch_size={batch_size} speed={srt_speed}")
            pending: list[dict] = []
            for index, item in enumerate(entries, start=1):
                wav_path = tmp_dir / f"clip_{index:05d}.wav"
                target_sec = item["end"] - item["start"]
                render_text = prepare_srt_render_text(pack, str(item["text"]))
                saved_clip = None
                saved_meta = None
                if clip_save_dir is not None:
                    signature = srt_cache_signature(pack, item, render_text)
                    saved_clip, saved_meta = srt_cache_paths(clip_save_dir, index, float(item["start"]), signature)
                    cached_clip, cached_meta = resolve_srt_cached_clip(pack, clip_save_dir, index, item, render_text, signature)
                    if bool(pack.get("srtReuseDebugClips", True)) and cached_clip is not None:
                        shutil.copy2(cached_clip, wav_path)
                        duration = audio_duration(wav_path)
                        write_cue_cached_clip(pack, render_text, cached_clip, {"asr": {"ok": True, "coverage": None}})
                        clip_reports.append(
                            {
                                "index": index,
                                "start": round(float(item["start"]), 3),
                                "end": round(float(item["end"]), 3),
                                "targetDuration": round(float(target_sec), 3),
                                "audioDuration": round(duration, 3),
                                "overlapsNextBy": None,
                                "text": item["text"],
                                "renderText": render_text,
                                "asrText": "",
                                "asr": None,
                                "savedClip": str(cached_clip),
                                "reused": True,
                            }
                        )
                        timeline_items.append((wav_path, float(item["start"])))
                        pct = index * 100 / len(entries)
                        print(f"VieNeu render: {index}/{len(entries)} ({pct:.1f}%) reused")
                        continue
                pending.append(
                    {
                        "index": index,
                        "item": item,
                        "wav_path": wav_path,
                        "saved_clip": saved_clip,
                        "saved_meta": saved_meta,
                        "target_sec": target_sec,
                        "render_text": render_text,
                    }
                )

            pending_batches = pending
            if bool(pack.get("srtBatchSortByLength", True)):
                pending_batches = sorted(pending, key=lambda row: len(str(row["render_text"])))
            batch_render_start = time.perf_counter()
            rendered_count = 0
            for batch_start in range(0, len(pending_batches), batch_size):
                batch = pending_batches[batch_start : batch_start + batch_size]
                one_batch_start = time.perf_counter()
                seed = int(pack.get("seedBase", 1729)) + int(batch[0]["index"]) * 1009
                torch.manual_seed(seed)
                if torch.cuda.is_available():
                    torch.cuda.manual_seed_all(seed)
                prepared_texts = [prepare_text_for_tts(tts, str(row["render_text"])) for row in batch]
                try:
                    audios = tts.infer_batch(
                        texts=prepared_texts,
                        ref_codes=ref_codes,
                        ref_text=pack.get("refText", ""),
                        temperature=float(pack.get("temperature", 0.55)),
                        top_k=int(pack.get("topK", 25)),
                        skip_normalize=True,
                        apply_watermark=False,
                        max_new_tokens=int(pack.get("srtMaxNewTokens", 520)),
                        min_new_tokens=int(pack.get("srtMinNewTokens", 40)),
                    )
                except RuntimeError as exc:
                    if "out of memory" not in str(exc).lower():
                        raise
                    print(f"VieNeu batch OOM fallback: batch_size={len(batch)} error={str(exc)[:180]}")
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    audios = []
                    for row in batch:
                        fallback_path = Path(row["wav_path"])
                        fallback_pack = dict(pack)
                        fallback_pack["srtBatchInfer"] = False
                        fallback_pack["clipUseInferBatch"] = False
                        fallback_pack["clipRetries"] = int(pack.get("srtBatchAsrFallbackRetries", pack.get("srtClipRetries", 5)))
                        infer_one_clip(
                            tts,
                            fallback_pack,
                            ref_codes,
                            str(row["render_text"]),
                            fallback_path,
                            target_sec=None,
                            asr_model=asr_model,
                        )
                        audios.append(None)
                for row, audio in zip(batch, audios):
                    wav_path = Path(row["wav_path"])
                    if audio is not None:
                        tts.save(audio, wav_path)
                        duration = numpy_audio_duration(audio, sample_rate)
                    else:
                        duration = audio_duration(wav_path)
                    ok_duration, duration_reason, _raw_duration = validate_clip(
                        wav_path,
                        str(row["render_text"]),
                        None,
                        float(pack.get("srtBatchValidateMinRatio", 0.62)),
                    )
                    if (not ok_duration) and bool(pack.get("srtBatchFallbackInvalid", True)):
                        print(
                            "VieNeu batch fallback: "
                            f"clip={row['index']} reason={duration_reason}"
                        )
                        fallback_pack = dict(pack)
                        fallback_pack["clipRetries"] = int(pack.get("srtBatchFallbackRetries", pack.get("srtClipRetries", 3)))
                        fallback_pack["clipQualitySelectBest"] = False
                        infer_one_clip(
                            tts,
                            fallback_pack,
                            ref_codes,
                            str(row["render_text"]),
                            wav_path,
                            target_sec=None,
                            asr_model=None,
                        )
                        duration = audio_duration(wav_path)
                    if bool(pack.get("srtBatchTrimEdges", False)):
                        trim_edges(
                            wav_path,
                            float(pack.get("srtTrimHeadSec", pack.get("trimHeadSec", 0.0))),
                            float(pack.get("srtTrimTailSec", pack.get("trimTailSec", 0.0))),
                        )
                        duration = audio_duration(wav_path)
                    if bool(pack.get("srtBatchQualityMode", False)):
                        post_pack = dict(pack)
                        post_pack["textPauseTailPadSec"] = adaptive_tail_pad(
                            pack,
                            str(row["render_text"]),
                            "srtTextPauseTailPadSec",
                            float(pack.get("textPauseTailPadSec", 0.14)),
                            "srtTextPauseTailPadSecByToken",
                        )
                        post_pack["textPauseHeadPadSec"] = float(pack.get("srtTextPauseHeadPadSec", pack.get("textPauseHeadPadSec", 0.035)))
                        smooth_audio_file(wav_path)
                        remove_short_noise_islands(wav_path, post_pack)
                        normalize_clip_silence_edges(wav_path, post_pack, is_final=int(row["index"]) == len(entries))
                        if abs(srt_speed - 1.0) >= 0.01:
                            apply_tempo(wav_path, srt_speed)
                        remove_tail_artifact(wav_path, effective_srt_tail_pack(pack, float(row.get("target_sec", 0.0) or 0.0), str(row["render_text"])), "srt", str(row["render_text"]))
                        normalize_loudness(wav_path, post_pack, "srt")
                        apply_post_declick(wav_path, post_pack, "srt")
                        smooth_audio_file(wav_path)
                        duration = audio_duration(wav_path)
                    elif abs(srt_speed - 1.0) >= 0.01:
                        apply_tempo(wav_path, srt_speed)
                        duration = duration / max(0.01, srt_speed)
                    batch_asr_text = ""
                    batch_asr_match = None
                    if bool(pack.get("srtBatchAsrFallback", False)) and asr_model is not None:
                        batch_asr_text = transcribe_audio(asr_model, wav_path)
                        batch_asr_match = analyze_asr_match(str(row["render_text"]), batch_asr_text) if batch_asr_text else None
                        if bool(pack.get("srtBatchCleanupHeadByAsr", True)):
                            needs_head_cleanup = bool((batch_asr_match or {}).get("extraPrefix")) or not bool((batch_asr_match or {}).get("firstTokenMatch", True))
                            if needs_head_cleanup:
                                cleanup_pack = dict(pack)
                                cleanup_pack["useWhisperXAlign"] = False
                                if cleanup_to_expected_word_bounds(wav_path, asr_model, str(row["render_text"]), cleanup_pack):
                                    duration = audio_duration(wav_path)
                                    batch_asr_text = transcribe_audio(asr_model, wav_path)
                                    batch_asr_match = analyze_asr_match(str(row["render_text"]), batch_asr_text) if batch_asr_text else None
                        batch_cov = float((batch_asr_match or {}).get("coverage", 0.0) or 0.0)
                        batch_fillers = detect_filler_artifacts(str(row["render_text"]), batch_asr_text, pack)
                        if (
                            batch_cov < float(pack.get("srtBatchAsrCoverageMin", 0.84))
                            or batch_fillers
                        ):
                            print(
                                "VieNeu batch ASR fallback: "
                                f"clip={row['index']} coverage={batch_cov:.3f} fillers={batch_fillers} asr={batch_asr_text}"
                            )
                            fallback_pack = dict(pack)
                            fallback_pack["srtBatchInfer"] = False
                            fallback_pack["clipUseInferBatch"] = False
                            fallback_pack["clipRetries"] = int(pack.get("srtBatchAsrFallbackRetries", pack.get("srtClipRetries", 5)))
                            fallback_pack["clipQualitySelectBest"] = True
                            fallback_pack["clipQualityAllowBestEffort"] = bool(pack.get("srtBatchFallbackAllowBestEffort", True))
                            infer_one_clip(
                                tts,
                                fallback_pack,
                                ref_codes,
                                str(row["render_text"]),
                                wav_path,
                                target_sec=None,
                                asr_model=asr_model,
                            )
                            if abs(srt_speed - 1.0) >= 0.01:
                                apply_tempo(wav_path, srt_speed)
                            remove_tail_artifact(wav_path, effective_srt_tail_pack(pack, float(row.get("target_sec", 0.0) or 0.0), str(row["render_text"])), "srt", str(row["render_text"]))
                            normalize_loudness(wav_path, pack, "srt")
                            duration = audio_duration(wav_path)
                            batch_asr_text = transcribe_audio(asr_model, wav_path) if bool(pack.get("srtWriteClipQa", True)) else ""
                            batch_asr_match = analyze_asr_match(str(row["render_text"]), batch_asr_text) if batch_asr_text else None
                    if not overlap_mode:
                        fit_clip_to_duration(wav_path, float(row["target_sec"]))
                        duration = audio_duration(wav_path)
                    saved_clip = row["saved_clip"]
                    item = row["item"]
                    report = {
                        "index": int(row["index"]),
                        "start": round(float(item["start"]), 3),
                        "end": round(float(item["end"]), 3),
                        "targetDuration": round(float(row["target_sec"]), 3),
                        "audioDuration": round(duration, 3),
                        "overlapsNextBy": None,
                        "text": item["text"],
                        "renderText": row["render_text"],
                        "asrText": batch_asr_text,
                        "asr": batch_asr_match,
                        "savedClip": str(saved_clip) if saved_clip else None,
                        "batchInfer": True,
                    }
                    if clip_save_dir is not None and saved_clip is not None and row.get("saved_meta") is not None:
                        signature = srt_cache_signature(pack, item, str(row["render_text"]))
                        write_srt_cached_clip(pack, saved_clip, Path(row["saved_meta"]), int(row["index"]), item, str(row["render_text"]), signature, wav_path, report)
                    clip_reports.append(report)
                    timeline_items.append((wav_path, float(item["start"])))
                    rendered_count += 1
                done = len(clip_reports)
                pct = done * 100 / len(entries)
                elapsed = max(0.001, time.perf_counter() - batch_render_start)
                print(
                    f"VieNeu render: {done}/{len(entries)} ({pct:.1f}%) batch "
                    f"dt={time.perf_counter() - one_batch_start:.1f}s avg={elapsed / max(1, rendered_count):.2f}s/clip "
                    f"eta={(elapsed / max(1, rendered_count)) * max(0, len(entries) - done) / 60:.1f}m"
                )

            timeline_items.sort(key=lambda row: row[1])
            clip_reports.sort(key=lambda row: int(row["index"]))
        entries_to_render = [] if batch_infer else entries
        for index, item in enumerate(entries_to_render, start=1):
            wav_path = tmp_dir / f"clip_{index:05d}.wav"
            target_sec = item["end"] - item["start"]
            render_text = prepare_srt_render_text(pack, str(item["text"]))
            saved_clip = None
            saved_meta = None
            cache_signature = ""
            cue_cached_clip = resolve_cue_cached_clip(pack, render_text)
            if cue_cached_clip is not None:
                shutil.copy2(cue_cached_clip, wav_path)
                duration = audio_duration(wav_path)
                clip_reports.append(
                    {
                        "index": index,
                        "start": round(float(item["start"]), 3),
                        "end": round(float(item["end"]), 3),
                        "targetDuration": round(float(target_sec), 3),
                        "audioDuration": round(duration, 3),
                        "overlapsNextBy": None,
                        "text": item["text"],
                        "renderText": render_text,
                        "asrText": "",
                        "asr": None,
                        "savedClip": str(cue_cached_clip),
                        "reused": True,
                        "cache": "persistent_cue",
                    }
                )
                timeline_items.append((wav_path, float(item["start"])))
                done = index
                pct = done * 100 / len(entries)
                print(f"VieNeu render: {done}/{len(entries)} ({pct:.1f}%) reused persistent cue")
                continue
            if clip_save_dir is not None:
                cache_signature = srt_cache_signature(pack, item, render_text)
                saved_clip, saved_meta = srt_cache_paths(clip_save_dir, index, float(item["start"]), cache_signature)
                cached_clip, cached_meta = resolve_srt_cached_clip(pack, clip_save_dir, index, item, render_text, cache_signature)
                if bool(pack.get("srtReuseDebugClips", True)) and cached_clip is not None:
                    shutil.copy2(cached_clip, wav_path)
                    duration = audio_duration(wav_path)
                    write_cue_cached_clip(pack, render_text, cached_clip, {"asr": {"ok": True, "coverage": None}})
                    asr_text = transcribe_audio(asr_model, wav_path) if asr_model is not None and bool(pack.get("srtWriteClipQa", True)) else ""
                    asr_match = analyze_asr_match(render_text, asr_text) if asr_text else None
                    clip_reports.append(
                        {
                            "index": index,
                            "start": round(float(item["start"]), 3),
                            "end": round(float(item["end"]), 3),
                            "targetDuration": round(float(target_sec), 3),
                            "audioDuration": round(duration, 3),
                            "overlapsNextBy": None,
                            "text": item["text"],
                            "renderText": render_text,
                            "asrText": asr_text,
                            "asr": asr_match,
                            "savedClip": str(cached_clip),
                            "reused": True,
                        }
                    )
                    timeline_items.append((wav_path, float(item["start"])))
                    done = index
                    pct = done * 100 / len(entries)
                    print(f"VieNeu render: {done}/{len(entries)} ({pct:.1f}%) reused")
                    continue
            final_quality = None
            final_asr_text = ""
            final_asr_match = None
            final_rounds = max(0, int(pack.get("srtFinalQaRounds", 1)))
            failed_dir = tmp_dir / "failed_clips"
            if final_rounds <= 0:
                clip_pack = dict(pack)
                if "srtClipRetries" in pack:
                    clip_pack["clipRetries"] = int(pack["srtClipRetries"])
                clip_pack["trimHeadSec"] = float(pack.get("srtTrimHeadSec", pack.get("trimHeadSec", 0.08)))
                clip_pack["trimTailSec"] = float(pack.get("srtTrimTailSec", pack.get("trimTailSec", 0.12)))
                infer_target = None if use_text_clip_pipeline else target_sec
                infer_one_clip(tts, clip_pack, ref_codes, render_text, wav_path, target_sec=infer_target, asr_model=asr_model)
                apply_tempo(wav_path, srt_speed)
                remove_tail_artifact(wav_path, effective_srt_tail_pack(pack, target_sec, render_text), "srt", render_text)
                if bool(pack.get("srtRemoveLongInternalSilence", True)):
                    remove_midphrase_long_pauses(wav_path, float(pack.get("srtMaxInternalSilenceSec", 0.38)))
                remove_short_noise_islands(wav_path, pack)
                apply_post_declick(wav_path, pack, "srt")
                normalize_loudness(wav_path, pack, "srt")
                smooth_audio_file(wav_path)
                finalize_clip_edges(wav_path, pack, "srt")
                final_quality = {"ok": True, "score": 0.0, "reason": "qa_disabled"}
                print(f"VieNeu final QA skipped: clip={index}")
            for final_round in range(final_rounds):
                clip_pack = dict(pack)
                if "srtClipRetries" in pack:
                    clip_pack["clipRetries"] = int(pack["srtClipRetries"])
                clip_pack["seedBase"] = int(pack.get("seedBase", 1729)) + final_round * 100000
                clip_pack["trimHeadSec"] = float(pack.get("srtTrimHeadSec", pack.get("trimHeadSec", 0.08)))
                clip_pack["trimTailSec"] = float(pack.get("srtTrimTailSec", pack.get("trimTailSec", 0.12)))
                clip_pack["clipQualityTailRmsLimit"] = float(pack.get("srtTailRmsLimit", 0.018))
                clip_pack["clipGuardTailPadSec"] = float(pack.get("srtClipGuardTailPadSec", pack.get("clipGuardTailPadSec", 0.20)))
                clip_pack["artifactWordPadSec"] = float(pack.get("srtArtifactWordPadSec", pack.get("artifactWordPadSec", 0.13)))
                clip_pack["clipGuardSuffixText"] = "" if use_text_clip_pipeline else clean_text(str(pack.get("srtGuardSuffixText", pack.get("clipGuardSuffixText", ""))))
                clip_pack["clipQualityEarlyAcceptScore"] = float(pack.get("srtEarlyAcceptScore", pack.get("clipQualityEarlyAcceptScore", 0.001)))
                if bool(pack.get("srtQualityBestEffort", True)):
                    clip_pack["clipQualityAllowBestEffort"] = True
                infer_target = None if use_text_clip_pipeline else target_sec
                infer_one_clip(tts, clip_pack, ref_codes, render_text, wav_path, target_sec=infer_target, asr_model=asr_model)
                if ultra_fast:
                    apply_tempo(wav_path, srt_speed)
                    remove_tail_artifact(wav_path, effective_srt_tail_pack(pack, target_sec, render_text), "srt", render_text)
                elif use_text_clip_pipeline:
                    post_pack = dict(pack)
                    post_pack["textPauseTailPadSec"] = adaptive_tail_pad(
                        pack,
                        render_text,
                        "srtTextPauseTailPadSec",
                        float(pack.get("textPauseTailPadSec", 0.14)),
                        "srtTextPauseTailPadSecByToken",
                    )
                    post_pack["textPauseHeadPadSec"] = float(pack.get("srtTextPauseHeadPadSec", pack.get("textPauseHeadPadSec", 0.035)))
                    smooth_audio_file(wav_path)
                    remove_short_noise_islands(wav_path, post_pack)
                    normalize_clip_silence_edges(wav_path, post_pack, is_final=index == len(entries))
                    normalize_clip_edges_by_asr(wav_path, asr_model, post_pack, is_final=index == len(entries))
                    apply_tempo(wav_path, srt_speed)
                    remove_tail_artifact(wav_path, effective_srt_tail_pack(pack, target_sec, render_text), "srt", render_text)
                    normalize_loudness(wav_path, post_pack, "srt")
                    apply_post_declick(wav_path, post_pack, "srt")
                    smooth_audio_file(wav_path)
                else:
                    apply_tempo(wav_path, srt_speed)
                    remove_tail_artifact(wav_path, effective_srt_tail_pack(pack, target_sec, render_text), "srt", render_text)
                    if bool(pack.get("srtRemoveLongInternalSilence", True)):
                        remove_midphrase_long_pauses(wav_path, float(pack.get("srtMaxInternalSilenceSec", 0.38)))
                    remove_short_noise_islands(wav_path, pack)
                    apply_post_declick(wav_path, pack, "srt")
                    normalize_loudness(wav_path, pack, "srt")
                    smooth_audio_file(wav_path)
                if (not ultra_fast) and asr_model is not None and bool(pack.get("srtFinalCropToExpected", True)):
                    final_crop_pack = dict(pack)
                    final_crop_pack["useWhisperXAlign"] = False
                    final_crop_pack["clipGuardTailPadSec"] = adaptive_tail_pad(
                        pack,
                        render_text,
                        "srtFinalCropTailPadSec",
                        0.16,
                        "srtFinalCropTailPadSecByToken",
                    )
                    final_crop_pack["clipGuardHeadPadSec"] = float(pack.get("srtFinalCropHeadPadSec", pack.get("clipGuardHeadPadSec", 0.08)))
                    cleanup_to_expected_word_bounds(wav_path, asr_model, render_text, final_crop_pack)
                if not ultra_fast:
                    finalize_clip_edges(wav_path, pack, "srt")
                    final_pack = dict(clip_pack)
                    final_pack["clipQualityRejectTailVoice"] = bool(pack.get("srtRejectTailVoice", True))
                    final_pack["clipQualityRejectShortFinalWord"] = bool(pack.get("srtFinalRejectShortFinalWord", False))
                    final_pack["clipQualityAsrCoverageMin"] = float(pack.get("srtFinalAsrCoverageMin", 0.92))
                    final_pack["useWhisperXAlign"] = False
                    final_pack["artifactWordPadSec"] = float(pack.get("srtArtifactWordPadSec", pack.get("artifactWordPadSec", 0.13)))
                    final_quality = clip_quality_score(wav_path, render_text, asr_model, final_pack, target_sec)
                    final_asr_text = str(final_quality.get("asrText", ""))
                    final_asr_match = final_quality.get("asr")
                else:
                    final_quality = {"ok": True, "score": 0.0, "reason": "ultra_fast"}
                    final_asr_text = ""
                    final_asr_match = None
                if bool(final_quality.get("ok", False)):
                    print(f"VieNeu final QA pass: clip={index} round={final_round + 1} score={final_quality['score']}")
                    break
                print(
                    "VieNeu final QA fail: "
                    f"clip={index} round={final_round + 1}/{final_rounds} "
                    f"score={final_quality['score']} reason={final_quality['reason']}"
                )
                if bool(pack.get("srtSaveDebugClips", False)):
                    failed_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(wav_path, failed_dir / f"clip_{index:05d}_round_{final_round + 1}.wav")
            if final_quality is not None and not bool(final_quality.get("ok", False)) and bool(pack.get("srtStrictFinalQa", False)):
                raise RuntimeError(f"VieNeu SRT final QA failed clip {index}: {final_quality['reason']} | text={render_text}")
            if not overlap_mode:
                fit_clip_to_duration(wav_path, target_sec)
            duration = audio_duration(wav_path)
            asr_text = final_asr_text or (transcribe_audio(asr_model, wav_path) if asr_model is not None and bool(pack.get("srtWriteClipQa", True)) else "")
            asr_match = final_asr_match or (analyze_asr_match(render_text, asr_text) if asr_text else None)
            report = {
                "index": index,
                "start": round(float(item["start"]), 3),
                "end": round(float(item["end"]), 3),
                "targetDuration": round(float(target_sec), 3),
                "audioDuration": round(duration, 3),
                "overlapsNextBy": None,
                "text": item["text"],
                "renderText": render_text,
                "asrText": asr_text,
                "asr": asr_match,
                "savedClip": str(saved_clip) if saved_clip else None,
            }
            if clip_save_dir is not None and saved_clip is not None and saved_meta is not None:
                write_srt_cached_clip(pack, saved_clip, saved_meta, index, item, render_text, cache_signature, wav_path, report)
            write_cue_cached_clip(pack, render_text, wav_path, report)
            clip_reports.append(report)
            timeline_items.append((wav_path, float(item["start"])))
            done = index
            pct = done * 100 / len(entries)
            elapsed = max(0.001, time.perf_counter() - render_loop_start)
            print(
                f"VieNeu render: {done}/{len(entries)} ({pct:.1f}%) "
                f"avg={elapsed / max(1, done):.2f}s/clip eta={(elapsed / max(1, done)) * max(0, len(entries) - done) / 60:.1f}m"
            )

        for idx, report in enumerate(clip_reports[:-1]):
            report["overlapsNextBy"] = round(
                max(0.0, timeline_items[idx][1] + float(report["audioDuration"]) - timeline_items[idx + 1][1]),
                3,
            )
        if clip_reports:
            clip_reports[-1]["overlapsNextBy"] = 0.0

        out_mp3.parent.mkdir(parents=True, exist_ok=True)
        mix_start = time.perf_counter()
        if overlap_mode:
            mix_timeline_audio(timeline_items, out_mp3, tmp_dir, pack)
        else:
            list_path = tmp_dir / "inputs.txt"
            timeline_files: list[Path] = []
            cursor = 0.0
            for i, (item, wav_path) in enumerate(zip(entries, [p for p, _s in timeline_items]), start=1):
                gap = item["start"] - cursor
                if gap > 0.02:
                    silence = tmp_dir / f"silence_{i:05d}.wav"
                    make_silence(silence, gap)
                    timeline_files.append(silence)
                timeline_files.append(wav_path)
                cursor = max(cursor, item["end"])
            list_path.write_text("".join(f"file '{p.as_posix()}'\n" for p in timeline_files), encoding="utf-8")
            subprocess.run(
                [
                    str(FFMPEG),
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_path),
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "128k",
                    str(out_mp3),
                ],
                check=True,
            )
        mix_sec = time.perf_counter() - mix_start
        apply_post_declick(out_mp3, pack, "srt")
        total_sec = time.perf_counter() - started_at
        if bool(pack.get("srtOmniBench", True)):
            print(
                "VieNeu bench done: "
                f"total={total_sec:.1f}s model={model_ready_sec:.1f}s "
                f"render={time.perf_counter() - render_loop_start:.1f}s mix={mix_sec:.1f}s "
                f"clips={len(clip_reports)} out={out_mp3}"
            )
        normalize_loudness(out_mp3, pack, "srt")
        if clip_save_dir is not None:
            (clip_save_dir / "qa_report.json").write_text(
                json.dumps({"output": str(out_mp3), "speed": srt_speed, "clips": clip_reports}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    tts.close()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()
    print(f"VieNeu done: {out_mp3} (100.0%)")


def validate_pack(pack: dict, out_dir: Path, asr_model_name: str = "small") -> None:
    tests = pack.get("validationTexts") or [
        "Hôm nay mình sẽ cải tạo lại căn phòng nhỏ này.",
        "Đầu tiên là dọn sạch đồ cũ trong phòng.",
        "Nhìn thì đơn giản vậy thôi, nhưng lúc bắt tay vào làm thì có rất nhiều chi tiết.",
        "Tôi xuyên không về thập niên bảy mươi, thành một cô gái béo phì.",
        "Cố gắng hết sức là được, đừng tạo áp lực quá lớn cho bản thân.",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"VieNeu validate: device={device} tests={len(tests)}")
    asr_model = load_asr_model(asr_model_name)
    vieneu_mode = clean_text(str(pack.get("vieneuMode", "standard"))) or "standard"
    if vieneu_mode in {"fast", "gpu"} and pack.get("lmdeployDtype"):
        os.environ["VIENEU_LMDEPLOY_DTYPE"] = clean_text(str(pack.get("lmdeployDtype", "float16"))) or "float16"
        print(f"VieNeu optimize: lmdeploy dtype={os.environ['VIENEU_LMDEPLOY_DTYPE']}")
    tts_kwargs = {
        "backbone_repo": pack.get("model", "pnnbao-ump/VieNeu-TTS-0.3B"),
        "backbone_device": device,
        "codec_repo": pack.get("codec", "neuphonic/distill-neucodec"),
        "codec_device": device,
    }
    if vieneu_mode == "standard":
        tts_kwargs["gguf_filename"] = pack.get("ggufFilename", None)
    if vieneu_mode in {"fast", "gpu"}:
        tts_kwargs["memory_util"] = float(pack.get("lmdeployMemoryUtil", 0.55))
        tts_kwargs["max_batch_size"] = int(pack.get("srtBatchSize", pack.get("batchSize", 4)))
    tts = Vieneu(mode=vieneu_mode, **tts_kwargs)
    if vieneu_mode == "standard" and pack.get("loraDir"):
        tts.load_lora_adapter(str(Path(pack["loraDir"])))
    dtype_name = clean_text(str(pack.get("backboneDtype", ""))).lower()
    if device == "cuda" and dtype_name in {"bf16", "bfloat16", "fp16", "float16"}:
        dtype = torch.bfloat16 if dtype_name in {"bf16", "bfloat16"} else torch.float16
        print(f"VieNeu optimize: backbone dtype={dtype_name}")
        tts.backbone.to(dtype=dtype)
    ref_codes = load_ref_codes_for_pack(tts, pack)
    warmup_tts(tts, pack, ref_codes)
    report = []
    ok_all = True
    try:
        for index, text in enumerate(tests, start=1):
            wav_path = out_dir / f"validation_{index:02d}.wav"
            duration = infer_one_clip(tts, pack, ref_codes, text, wav_path, asr_model=asr_model)
            smooth_audio_file(wav_path)
            asr_text = transcribe_audio(asr_model, wav_path)
            asr_match = analyze_asr_match(text, asr_text) if asr_text else {"ok": True, "coverage": None, "extraPrefix": []}
            ok_all = ok_all and bool(asr_match["ok"])
            report.append({
                "index": index,
                "text": text,
                "duration": round(duration, 3),
                "asrText": asr_text,
                "asr": asr_match,
                "file": str(wav_path),
            })
            status = "ok" if asr_match["ok"] else "fail"
            print(f"VieNeu validate: {index}/{len(tests)} {status} duration={duration:.2f}s asr={asr_text}")
    finally:
        tts.close()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()
    (out_dir / "validation_report.json").write_text(
        json.dumps({"ok": ok_all, "tests": report}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not ok_all:
        raise RuntimeError(f"VieNeu pack validation failed: {out_dir / 'validation_report.json'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-dir", type=Path, required=True)
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--text")
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--asr-check", action="store_true")
    parser.add_argument("--asr-model", default="small")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    pack = load_pack(args.pack_dir.resolve())
    text_value = args.text
    if args.text_file:
        text_value = args.text_file.read_text(encoding="utf-8-sig", errors="replace").strip()
    if args.validate:
        validate_pack(pack, args.out.resolve(), args.asr_model)
    elif text_value:
        render_pack_text(pack, text_value, args.out.resolve(), args.asr_model if args.asr_check else None)
    elif args.srt:
        render_srt(pack, args.srt.resolve(), args.out.resolve())
    else:
        raise SystemExit("--srt hoac --text la bat buoc")


if __name__ == "__main__":
    main()
