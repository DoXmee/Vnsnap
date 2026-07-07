from __future__ import annotations

import argparse
import gc
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import torch
from vieneu import Vieneu

from render_vieneu_srt import (
    FFMPEG,
    apply_post_declick,
    apply_tempo,
    clean_text,
    effective_srt_tail_pack,
    finalize_clip_edges,
    infer_one_clip,
    load_asr_model,
    load_pack,
    load_ref_codes_for_pack,
    normalize_loudness,
    parse_srt,
    prepare_srt_render_text,
    remove_tail_artifact,
    resolve_lora_dir_for_render,
    smooth_audio_file,
    warmup_tts,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def text_units(text: str) -> int:
    """Count simple Vietnamese text units for short-cue stabilization."""
    return len([part for part in clean_text(text).replace(".", " ").replace(",", " ").split() if part])


def stabilize_clip_text(pack: dict, text: str) -> str:
    """Make short standalone cues less likely to lose their final syllable."""
    value = clean_text(text)
    if not bool(pack.get("textClipStabilizeShortCue", True)):
        return value
    tail_tokens = set(str(pack.get("textClipEllipsisTailTokens", "thôi,phòng,tiết,này")).split(","))
    normalized_tail = clean_text(value).rstrip(".!?…").split()
    last = normalized_tail[-1].lower() if normalized_tail else ""
    short = text_units(value) <= int(pack.get("textClipEllipsisMaxWords", 8))
    needs_tail_pad = last in {token.strip().lower() for token in tail_tokens if token.strip()}
    if (short or needs_tail_pad) and value.endswith("."):
        return value[:-1].rstrip() + "..."
    return value


def first_word(text: str) -> str:
    """Return the first simple word from text for contextual guard suffixes."""
    value = clean_text(text).replace(".", " ").replace(",", " ").replace("!", " ").replace("?", " ")
    return value.split()[0].lower() if value.split() else ""


def last_word(text: str) -> str:
    """Return the last simple word from text for contextual guard suffixes."""
    value = clean_text(text).rstrip(".!?…")
    parts = value.split()
    return parts[-1].lower() if parts else ""


def contextual_guard_suffix(pack: dict, entries: list[dict], index: int, text: str) -> str:
    """Use the next cue's first word as a natural guard suffix for risky final words."""
    if not bool(pack.get("textClipContextGuardSuffix", True)):
        return clean_text(str(pack.get("textClipGuardSuffixText", "")))
    risky = {token.strip().lower() for token in str(pack.get("textClipContextGuardTailTokens", "thôi,phòng,tiết")).split(",") if token.strip()}
    if last_word(text) not in risky:
        return ""
    if index < len(entries):
        nxt = first_word(str(entries[index]["text"]))
        if nxt:
            return nxt
    return clean_text(str(pack.get("textClipGuardSuffixText", "")))


def wav_to_mp3(wav_path: Path, mp3_path: Path, bitrate: str = "192k") -> None:
    """Encode one rendered WAV cue to MP3 for the existing SRT timing joiner."""
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(wav_path),
            "-c:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            str(mp3_path),
        ],
        check=True,
    )


def srt_timestamp(seconds: float) -> str:
    """Format seconds as SRT timestamp."""
    ms_total = max(0, int(round(float(seconds) * 1000)))
    ms = ms_total % 1000
    total = ms_total // 1000
    s = total % 60
    total //= 60
    m = total % 60
    h = total // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_group_srt(entries: list[dict], path: Path) -> None:
    """Write grouped voice-timing SRT matching the rendered clip count."""
    blocks = []
    for index, item in enumerate(entries, start=1):
        blocks.append(
            f"{index}\n"
            f"{srt_timestamp(float(item['start']))} --> {srt_timestamp(float(item['end']))}\n"
            f"{clean_text(str(item['text']))}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")


def join_group_text(left: str, right: str, pack: dict) -> str:
    """Join two subtitle cues into one natural TTS sentence."""
    left_text = clean_text(left)
    right_text = clean_text(right)
    if bool(pack.get("textClipGroupCommaJoin", True)):
        left_text = left_text.rstrip(".!?…")
        if right_text:
            right_text = right_text[0].lower() + right_text[1:]
        return clean_text(f"{left_text}, {right_text}")
    return clean_text(f"{left_text} {right_text}")


def group_entries_for_text_clips(entries: list[dict], pack: dict) -> list[dict]:
    """Group fragile short SRT cues into natural text clips before TTS.

    Subtitle timing can stay line-by-line for display, but local TTS is more stable
    when very short cues with fragile final syllables are read with their following
    context. The generated voice_groups.srt should be used by the MP3/SRT joiner.
    """
    if not bool(pack.get("textClipAutoGroup", False)):
        return entries
    risky = {
        token.strip().lower()
        for token in str(pack.get("textClipGroupTailTokens", "thôi,phòng,tiết")).split(",")
        if token.strip()
    }
    connectors = {
        token.strip().lower()
        for token in str(pack.get("textClipGroupNextWords", "nhưng,khi,và,rồi,sau,tiếp")).split(",")
        if token.strip()
    }
    max_gap = float(pack.get("textClipGroupMaxGapSec", 0.65))
    max_words = int(pack.get("textClipGroupMaxWords", 22))
    grouped: list[dict] = []
    i = 0
    while i < len(entries):
        current = dict(entries[i])
        if i + 1 < len(entries):
            nxt = entries[i + 1]
            gap = float(nxt["start"]) - float(current["end"])
            current_last = last_word(str(current["text"]))
            next_first = first_word(str(nxt["text"]))
            word_total = text_units(str(current["text"])) + text_units(str(nxt["text"]))
            should_group = (
                gap <= max_gap
                and word_total <= max_words
                and (
                    current_last in risky
                    or next_first in connectors
                )
            )
            if should_group:
                current["end"] = nxt["end"]
                current["text"] = join_group_text(str(current["text"]), str(nxt["text"]), pack)
                grouped.append(current)
                i += 2
                continue
        grouped.append(current)
        i += 1
    if len(grouped) != len(entries):
        print(f"VieNeu clips: grouped {len(entries)} SRT cues -> {len(grouped)} voice clips")
    return grouped


def render_srt_to_text_clips(pack: dict, srt_path: Path, out_dir: Path, max_cues: int = 0, asr_model_name: str | None = None) -> dict:
    """Render each SRT cue as an independent text-mode voice clip.

    This intentionally does not fit, trim, or crop clips to SRT timing. The existing
    MP3-by-SRT joiner should place these clips on the timeline afterward.
    """
    started = time.perf_counter()
    entries = parse_srt(srt_path)
    if max_cues > 0:
        entries = entries[:max_cues]
    if not entries:
        raise RuntimeError(f"SRT has no valid cue: {srt_path}")
    original_entries = entries
    entries = group_entries_for_text_clips(entries, pack)

    out_dir.mkdir(parents=True, exist_ok=True)
    wav_dir = out_dir / "_wav"
    wav_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    group_srt_path = out_dir / "voice_groups.srt"
    write_group_srt(entries, group_srt_path)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    pure_text_mode = bool(pack.get("textClipPureTextMode", True))
    use_asr_gate = bool(pack.get("textClipAsrGate", False)) and not pure_text_mode
    if use_asr_gate and not asr_model_name:
        asr_model_name = str(pack.get("textClipAsrModel", pack.get("srtAsrModel", "small")))
    asr_model = load_asr_model(asr_model_name) if asr_model_name else None
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
        pack["loraDir"] = str(lora_dir)
        tts.load_lora_adapter(str(lora_dir))
    dtype_name = clean_text(str(pack.get("backboneDtype", ""))).lower()
    if device == "cuda" and dtype_name in {"bf16", "bfloat16", "fp16", "float16"}:
        dtype = torch.bfloat16 if dtype_name in {"bf16", "bfloat16"} else torch.float16
        print(f"VieNeu clips: backbone dtype={dtype_name}")
        tts.backbone.to(dtype=dtype)
    if pack.get("maxContext"):
        tts.max_context = max(256, int(pack.get("maxContext", tts.max_context)))
    ref_codes = load_ref_codes_for_pack(tts, pack)
    warmup_tts(tts, pack, ref_codes)

    clip_pack = dict(pack)
    clip_pack["trimTailSec"] = float(pack.get("textClipTrimTailSec", 0.0))
    clip_pack["trimHeadSec"] = float(pack.get("textClipTrimHeadSec", pack.get("trimHeadSec", 0.08)))
    clip_pack["clipGuardSuffixText"] = ""
    clip_pack["clipGuardCropToExpected"] = False if pure_text_mode else bool(pack.get("textClipGuardCropToExpected", True))
    clip_pack["clipGuardAllowTailFallback"] = False
    clip_pack["clipGuardTailPadSec"] = float(pack.get("textClipGuardTailPadSec", 0.35))
    clip_pack["clipGuardHeadPadSec"] = float(pack.get("textClipGuardHeadPadSec", 0.08))
    clip_pack["clipRetries"] = int(pack.get("textClipRetries", max(1, int(pack.get("clipRetries", 1)))))
    clip_pack["clipQualitySelectBest"] = True
    clip_pack["clipQualityAllowBestEffort"] = True if pure_text_mode else bool(pack.get("textClipAllowBestEffort", False))
    clip_pack["clipQualityAllowNonArtifactBestEffort"] = True if pure_text_mode else bool(pack.get("textClipAllowBestEffort", False))
    clip_pack["clipValidateMinRatio"] = float(pack.get("textClipValidateMinRatio", 0.30 if pure_text_mode else 0.82))
    clip_pack["clipQualityAsrCoverageMin"] = float(pack.get("textClipAsrCoverageMin", 0.90))
    clip_pack["clipQualityRejectShortFinalWord"] = False if pure_text_mode else bool(pack.get("textClipRejectShortFinalWord", False))
    clip_pack["clipQualityRejectFiller"] = False if pure_text_mode else bool(pack.get("textClipRejectFiller", True))
    clip_pack["clipQualityRejectExtraPrefix"] = False if pure_text_mode else bool(pack.get("textClipRejectExtraPrefix", True))
    clip_pack["clipQualityRejectFirstTokenMismatch"] = False if pure_text_mode else bool(pack.get("textClipRejectFirstTokenMismatch", True))

    rendered = []
    for index, item in enumerate(entries, start=1):
        raw_text = str(item["text"])
        base_render_text = prepare_srt_render_text(pack, raw_text)
        suffix = "" if pure_text_mode else contextual_guard_suffix(pack, entries, index - 1, base_render_text)
        render_text = base_render_text if (pure_text_mode or suffix) else stabilize_clip_text(pack, base_render_text)
        per_clip_pack = dict(clip_pack)
        per_clip_pack["clipGuardSuffixText"] = suffix
        wav_path = wav_dir / f"cau_{index:04d}.wav"
        mp3_path = out_dir / f"cau_{index:04d}.mp3"
        print(f"VieNeu clips: render {index}/{len(entries)} {render_text[:80]}")
        text_tail_pack = effective_srt_tail_pack(per_clip_pack, None, render_text)
        infer_one_clip(tts, text_tail_pack, ref_codes, render_text, wav_path, target_sec=None, asr_model=asr_model)
        speed = float(pack.get("textSpeechSpeed", pack.get("speechSpeed", 1.0)))
        if abs(speed - 1.0) >= 0.01:
            apply_tempo(wav_path, speed)
        remove_tail_artifact(wav_path, text_tail_pack, "text", render_text)
        normalize_loudness(wav_path, pack, "text")
        apply_post_declick(wav_path, pack, "text")
        smooth_audio_file(wav_path)
        finalize_clip_edges(wav_path, pack, "text")
        wav_to_mp3(wav_path, mp3_path, str(pack.get("textClipMp3Bitrate", "192k")))
        rendered.append(
            {
                "index": index,
                "start": item["start"],
                "end": item["end"],
                "text": raw_text,
                "renderText": render_text,
                "wav": str(wav_path),
                "mp3": str(mp3_path),
            }
        )

    try:
        tts.close()
    finally:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()

    report = {
        "srt": str(srt_path),
        "voiceGroupsSrt": str(group_srt_path),
        "originalCueCount": len(original_entries),
        "outDir": str(out_dir),
        "total": len(rendered),
        "elapsedSec": round(time.perf_counter() - started, 3),
        "clips": rendered,
    }
    manifest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-dir", type=Path, required=True)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--max-cues", type=int, default=0)
    parser.add_argument("--asr-check", action="store_true")
    parser.add_argument("--asr-model", default="small")
    args = parser.parse_args()
    pack = load_pack(args.pack_dir.resolve())
    report = render_srt_to_text_clips(
        pack,
        args.srt.resolve(),
        args.out_dir.resolve(),
        max_cues=args.max_cues,
        asr_model_name=args.asr_model if args.asr_check else None,
    )
    print(json.dumps({"ok": True, "outDir": report["outDir"], "total": report["total"], "elapsedSec": report["elapsedSec"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
