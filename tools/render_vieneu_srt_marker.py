#!/usr/bin/env python3
"""Render SRT through long text blocks separated by audible pause markers.

This is an experimental path for VieNeu voices: short SRT cues are grouped into
larger text blocks, rendered like normal text, then split back into per-cue
clips at the marker pauses. The final mix still follows the original SRT
timeline.
"""

from __future__ import annotations

import argparse
import gc
import json
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

import torch
from vieneu import Vieneu

from render_vieneu_srt import (
    FFMPEG,
    apply_post_declick,
    apply_tempo,
    audio_peak_rms,
    audio_duration,
    clean_text,
    load_pack,
    load_asr_model,
    mix_timeline_audio,
    normalize_srt_literal_pronunciation,
    normalize_srt_reading_text,
    normalize_loudness,
    parse_srt,
    prepare_text_for_tts,
    smooth_audio_file,
    compare_tokens,
    analyze_asr_match,
    infer_one_clip,
    transcribe_audio,
    vad_speech_segments,
    word_timing_edges,
    warmup_tts,
)


def split_blocks(entries: list[dict], max_cues: int, max_chars: int) -> list[list[dict]]:
    blocks: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for entry in entries:
        text_len = len(str(entry["text"]))
        if cur and (len(cur) >= max_cues or cur_chars + text_len > max_chars):
            blocks.append(cur)
            cur = []
            cur_chars = 0
        cur.append(entry)
        cur_chars += text_len
    if cur:
        blocks.append(cur)
    return blocks


def marker_text_for_block(block: list[dict], marker: str, use_marker: bool = True) -> str:
    pieces = []
    for idx, entry in enumerate(block):
        text = clean_text(str(entry["text"]))
        if use_marker and idx < len(block) - 1:
            pieces.append(text + marker)
        else:
            pieces.append(text)
    return clean_text(" ".join(pieces))


def detect_silences(path: Path, noise_db: int, min_sec: float) -> list[tuple[float, float]]:
    proc = subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_sec:.3f}",
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
    pairs: list[tuple[float, float]] = []
    for line in (proc.stderr or "").splitlines():
        m_start = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m_start:
            starts.append(float(m_start.group(1)))
            continue
        m_end = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m_end and starts:
            start = starts.pop(0)
            end = float(m_end.group(1))
            if end > start:
                pairs.append((start, end))
    return pairs


def choose_marker_silences(
    silences: list[tuple[float, float]],
    needed: int,
    duration: float,
    min_gap_sec: float,
) -> list[tuple[float, float]]:
    usable = [
        (start, end)
        for start, end in silences
        if end - start >= min_gap_sec and start > 0.12 and end < duration - 0.12
    ]
    if len(usable) >= needed:
        return sorted(sorted(usable, key=lambda pair: pair[1] - pair[0], reverse=True)[:needed])
    # Retry with the longest detected pauses if the marker pause was shorter
    # than expected. This keeps the path usable while still logging the issue.
    ranked = sorted(
        [(start, end) for start, end in silences if start > 0.12 and end < duration - 0.12],
        key=lambda pair: pair[1] - pair[0],
        reverse=True,
    )
    chosen = sorted(ranked[:needed])
    if len(chosen) >= needed:
        return chosen
    raise RuntimeError(f"Khong tim du marker silence: can {needed}, thay {len(silences)}")


def estimate_marker_silences(block: list[dict], needed: int, duration: float) -> list[tuple[float, float]]:
    lengths = [max(8, len(clean_text(str(item["text"])))) for item in block]
    total = max(1, sum(lengths))
    markers: list[tuple[float, float]] = []
    acc = 0
    for idx in range(needed):
        acc += lengths[idx]
        center = duration * acc / total
        markers.append((max(0.12, center - 0.10), min(duration - 0.12, center + 0.10)))
    return markers


def split_points_by_asr_words(asr_model, block_wav: Path, block: list[dict], duration: float) -> list[tuple[float, float]]:
    words = word_timing_edges(asr_model, block_wav)
    if not words:
        raise RuntimeError("ASR khong tra ve word timing")
    starts: list[float] = []
    ends: list[float] = []
    cursor = 0
    head_pad = 0.05
    tail_pad = 0.60
    for cue_index, entry in enumerate(block):
        expected = compare_tokens(str(entry["text"]))
        count = max(1, len(expected))
        start_word = min(cursor, len(words) - 1)
        end_word = min(len(words) - 1, cursor + count - 1)
        starts.append(max(0.0, float(words[start_word][1]) - head_pad))
        ends.append(min(duration, float(words[end_word][2]) + tail_pad))
        cursor = end_word + 1
        if cue_index == len(block) - 1:
            ends[-1] = min(duration, max(ends[-1], float(words[-1][2]) + tail_pad))
    return list(zip(starts, ends))


def cut_audio(path: Path, out: Path, start: float, end: float) -> None:
    if end <= start + 0.12:
        raise RuntimeError(f"Clip qua ngan: {path.name} {start:.3f}-{end:.3f}")
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-to",
            f"{end:.3f}",
            "-i",
            str(path),
            "-af",
            "afade=t=in:st=0:d=0.010,alimiter=limit=0.92",
            "-ac",
            "1",
            "-ar",
            "48000",
            str(out),
        ],
        check=True,
    )
    if not out.exists() or out.stat().st_size < 512:
        raise RuntimeError(f"Cat clip that bai: {out}")


def make_silence_clip(path: Path, duration: float = 0.45) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            "anullsrc=channel_layout=mono:sample_rate=48000",
            "-t",
            f"{max(0.05, duration):.3f}",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
    )


def trim_tail_artifact_by_vad(path: Path, text: str, asr_model, pack: dict) -> bool:
    if asr_model is None or not bool(pack.get("markerBlockTrimTailArtifact", True)):
        return False
    words = word_timing_edges(asr_model, path)
    if not words:
        return False
    duration = audio_duration(path)
    last_end = float(words[-1][2])
    safe_tail = float(pack.get("markerBlockCleanTailKeepSec", 0.42))
    scan_start = min(duration, last_end + safe_tail)
    vad_pack = dict(pack)
    vad_pack["artifactVadThreshold"] = float(pack.get("markerBlockTailVadThreshold", pack.get("artifactVadThreshold", 0.32)))
    segments = vad_speech_segments(path, vad_pack)
    artifact = next((seg for seg in segments if seg[0] >= scan_start and seg[1] - seg[0] >= 0.055), None)
    tail_peak, tail_rms = audio_peak_rms(path, min(duration, last_end + 0.045), duration)
    tail_dirty = tail_rms > float(pack.get("markerBlockTailEnergyRmsLimit", 0.030))
    if artifact is None and not tail_dirty:
        return False
    if artifact is not None:
        cut_end = max(last_end + safe_tail, artifact[0] - 0.04)
    else:
        cut_end = last_end + float(pack.get("markerBlockDirtyTailWordPadSec", 0.07))
    cut_end = min(duration, cut_end)
    if duration - cut_end < 0.04 or cut_end <= last_end + 0.015:
        return False
    tmp = path.with_name(path.stem + "_tail_clean_tmp" + path.suffix)
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
            f"atrim=0:{cut_end:.3f},asetpts=N/SR/TB,apad=pad_dur={float(pack.get('markerBlockCleanTailSilenceSec', 0.300)):.3f},alimiter=limit=0.92",
            "-ac",
            "1",
            "-ar",
            "48000",
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 512:
        tmp.replace(path)
        print(
            "VieNeu marker-block tail artifact trim: "
            f"{path.name} last_word={last_end:.3f}s "
            f"artifact={artifact[0]:.3f}-{artifact[1]:.3f}s " if artifact else ""
            f"tail_rms={tail_rms:.4f} cut={cut_end:.3f}s"
        )
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def trim_head_artifact_by_vad(path: Path, text: str, pack: dict) -> bool:
    if not bool(pack.get("markerBlockTrimHeadArtifact", True)):
        return False
    vad_pack = dict(pack)
    vad_pack["artifactVadThreshold"] = float(pack.get("markerBlockHeadVadThreshold", pack.get("artifactVadThreshold", 0.32)))
    segments = vad_speech_segments(path, vad_pack)
    if len(segments) < 2:
        return False
    first_start, first_end = segments[0]
    second_start, _second_end = segments[1]
    first_len = first_end - first_start
    gap = second_start - first_end
    if first_start > 0.08 or first_len > float(pack.get("markerBlockHeadArtifactMaxSec", 0.12)) or gap < float(pack.get("markerBlockHeadArtifactMinGapSec", 0.10)):
        return False
    trim_to = max(0.0, second_start - float(pack.get("markerBlockHeadKeepSec", 0.045)))
    if trim_to <= 0.04:
        return False
    tmp = path.with_name(path.stem + "_head_clean_tmp" + path.suffix)
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{trim_to:.3f}",
            "-i",
            str(path),
            "-af",
            "afade=t=in:st=0:d=0.015,alimiter=limit=0.92",
            "-ac",
            "1",
            "-ar",
            "48000",
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 512:
        tmp.replace(path)
        print(
            "VieNeu marker-block head artifact trim: "
            f"{path.name} artifact={first_start:.3f}-{first_end:.3f}s next={second_start:.3f}s trim={trim_to:.3f}s"
        )
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def trim_block_head_silence(path: Path) -> None:
    tmp = path.with_name(path.stem + "_head_trim_tmp" + path.suffix)
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
            "silenceremove=start_periods=1:start_duration=0.03:start_threshold=-50dB",
            str(tmp),
        ],
        check=True,
    )
    if tmp.exists() and tmp.stat().st_size > 1024:
        tmp.replace(path)
    elif tmp.exists():
        tmp.unlink()


def render_block(tts, pack: dict, ref_codes, text: str, out_wav: Path, asr_model=None) -> None:
    block_pack = dict(pack)
    allow_non_artifact_best = bool(pack.get("markerBlockAllowBestEffort", False))
    block_pack["maxChars"] = int(pack.get("markerBlockInferMaxChars", pack.get("maxChars", 180)))
    block_pack["clipRetries"] = int(pack.get("markerBlockRetries", pack.get("clipRetries", 10)))
    block_pack["clipQualitySelectBest"] = bool(pack.get("markerBlockSelectBest", True))
    block_pack["clipQualityAllowBestEffort"] = bool(pack.get("markerBlockAllowAnyBestEffort", False))
    block_pack["clipQualityAllowNonArtifactBestEffort"] = allow_non_artifact_best
    block_pack["clipQualityBestEffortCoverageMin"] = float(
        pack.get("markerBlockBestEffortCoverageMin", pack.get("clipQualityBestEffortCoverageMin", 0.80))
    )
    block_pack["clipQualityAsrCoverageMin"] = float(pack.get("markerBlockCoverageMin", 0.82))
    block_pack["clipQualityRejectFirstTokenMismatch"] = True
    block_pack["clipQualityRejectExtraPrefix"] = True
    block_pack["clipQualityRejectFiller"] = True
    block_pack["clipQualityRejectTailVoice"] = bool(pack.get("markerBlockRejectTailVoice", True))
    block_pack["clipQualityTailRmsLimit"] = float(pack.get("markerBlockTailRmsLimit", 0.018))
    block_pack["artifactReject"] = bool(pack.get("markerBlockArtifactReject", True))
    block_pack["artifactWordPadSec"] = float(pack.get("markerBlockArtifactWordPadSec", pack.get("artifactWordPadSec", 0.10)))
    infer_one_clip(tts, block_pack, ref_codes, text, out_wav, asr_model=asr_model)
    trim_block_head_silence(out_wav)
    apply_tempo(out_wav, float(pack.get("srtSpeechSpeed", pack.get("speechSpeed", 1.0))))
    normalize_loudness(out_wav, pack, "srt")
    apply_post_declick(out_wav, pack, "srt")
    smooth_audio_file(out_wav)


def block_has_enough_text(asr_model, path: Path, text: str, pack: dict) -> bool:
    if asr_model is None:
        return True
    actual = transcribe_audio(asr_model, path)
    match = analyze_asr_match(text, actual)
    min_cov = float(pack.get("markerBlockCoverageMin", 0.82))
    ok = float(match.get("coverage") or 0.0) >= min_cov and len(match.get("extraPrefix") or []) <= 1
    print(f"VieNeu marker-block ASR: ok={ok} coverage={match.get('coverage')} asr={actual}")
    return ok


def render_srt_marker_blocks(pack: dict, srt_path: Path, out_mp3: Path) -> None:
    started = time.perf_counter()
    entries = parse_srt(srt_path)
    if not entries:
        raise RuntimeError(f"SRT khong co subtitle hop le: {srt_path}")
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    progress_path = out_mp3.with_suffix(".progress.json")
    prepared_entries = []
    for entry in entries:
        render_text = str(entry["text"])
        if bool(pack.get("srtLiteralPronunciationFix", False)):
            render_text = normalize_srt_literal_pronunciation(render_text, pack)
        if bool(pack.get("srtNormalizeReadingText", False)):
            render_text = normalize_srt_reading_text(render_text)
        prepared = dict(entry)
        prepared["text"] = render_text
        prepared_entries.append(prepared)
    entries = prepared_entries
    max_cues = int(pack.get("markerBlockMaxCues", 10))
    max_chars = int(pack.get("markerBlockMaxChars", 620))
    marker = str(pack.get("markerBlockText", ",,,     "))
    blocks = split_blocks(entries, max_cues=max_cues, max_chars=max_chars)
    save_dir = Path(str(pack.get("markerBlockSaveClipDir", out_mp3.with_name(out_mp3.stem + "_marker_clips"))))
    save_dir.mkdir(parents=True, exist_ok=True)
    if bool(pack.get("markerBlockReuseClips", True)) and entries:
        cached_paths: list[Path] = []
        for index, entry in enumerate(entries, start=1):
            cached_path = save_dir / f"clip_{index:05d}_{float(entry['start']):.3f}s.wav"
            if not cached_path.exists() or cached_path.stat().st_size < 512:
                cached_paths = []
                break
            cached_paths.append(cached_path)
        if cached_paths:
            print(f"VieNeu marker-block: all {len(cached_paths)} clips cached, skip model load")
            with tempfile.TemporaryDirectory(prefix="vieneu_marker_cached_") as tmp_name:
                tmp = Path(tmp_name)
                timeline_items: list[tuple[Path, float]] = []
                report = []
                for index, (entry, cached_path) in enumerate(zip(entries, cached_paths), start=1):
                    clip = tmp / f"clip_{index:05d}.wav"
                    shutil.copy2(cached_path, clip)
                    timeline_items.append((clip, float(entry["start"])))
                    report.append(
                        {
                            "index": index,
                            "start": round(float(entry["start"]), 3),
                            "end": round(float(entry["end"]), 3),
                            "text": entry["text"],
                            "savedClip": str(cached_path),
                            "reused": True,
                        }
                    )
                mix_timeline_audio(timeline_items, out_mp3, tmp, pack)
            (save_dir / "marker_report.json").write_text(
                json.dumps(
                    {
                        "createdAt": datetime.now().isoformat(timespec="seconds"),
                        "entries": len(entries),
                        "blocks": len(blocks),
                        "okClips": len(report),
                        "failedClips": 0,
                        "clips": report,
                        "failed": [],
                        "allCached": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            progress_path.write_text(
                json.dumps(
                    {
                        "updatedAt": datetime.now().isoformat(timespec="seconds"),
                        "doneBlocks": len(blocks),
                        "totalBlocks": len(blocks),
                        "doneClips": len(entries),
                        "failedClips": 0,
                        "out": str(out_mp3),
                        "allCached": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"VieNeu marker-block cached done: {out_mp3}")
            return
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"VieNeu marker-block: device={device} entries={len(entries)} blocks={len(blocks)}")

    vieneu_mode = clean_text(str(pack.get("vieneuMode", "standard"))) or "standard"
    tts_kwargs = {
        "backbone_repo": pack.get("model", "pnnbao-ump/VieNeu-TTS-0.3B"),
        "backbone_device": device,
        "codec_repo": pack.get("codec", "neuphonic/distill-neucodec"),
        "codec_device": device,
    }
    if vieneu_mode == "standard":
        tts_kwargs["gguf_filename"] = pack.get("ggufFilename", None)
    tts = Vieneu(mode=vieneu_mode, **tts_kwargs)
    if vieneu_mode == "standard" and pack.get("loraDir"):
        tts.load_lora_adapter(str(Path(pack["loraDir"])))
    dtype_name = clean_text(str(pack.get("backboneDtype", ""))).lower()
    if device == "cuda" and dtype_name in {"bf16", "bfloat16", "fp16", "float16"}:
        dtype = torch.bfloat16 if dtype_name in {"bf16", "bfloat16"} else torch.float16
        tts.backbone.to(dtype=dtype)
    ref_codes = tts.encode_reference(str(Path(pack["refAudio"])))
    warmup_tts(tts, pack, ref_codes)
    print(f"VieNeu marker-block: model_ready={time.perf_counter() - started:.1f}s")
    asr_model = (
        load_asr_model(str(pack.get("markerBlockAsrModel", pack.get("srtAsrModel", "small"))))
        if bool(pack.get("markerBlockAsrSplit", False)) or bool(pack.get("markerBlockQaAsr", True))
        else None
    )

    report: list[dict] = []
    failed_report: list[dict] = []
    timeline_items: list[tuple[Path, float]] = []
    noise_db = int(pack.get("markerBlockSilenceDb", -38))
    min_silence = float(pack.get("markerBlockSilenceMinSec", 0.22))
    min_gap = float(pack.get("markerBlockMarkerMinSec", 0.20))
    tail_keep = float(pack.get("markerBlockTailKeepSec", 0.60))
    head_keep = float(pack.get("markerBlockHeadKeepSec", 0.04))

    def render_one_block(block_index: int, block: list[dict], tmp: Path, entry_index: int) -> int:
        if bool(pack.get("markerBlockReuseClips", True)):
            cached: list[tuple[Path, dict, int]] = []
            for local_index, entry in enumerate(block, start=1):
                cached_index = entry_index + local_index
                cached_path = save_dir / f"clip_{cached_index:05d}_{float(entry['start']):.3f}s.wav"
                if not cached_path.exists() or cached_path.stat().st_size < 512:
                    cached = []
                    break
                cached.append((cached_path, entry, cached_index))
            if cached:
                for cached_path, entry, cached_index in cached:
                    clip = tmp / f"clip_{cached_index:05d}.wav"
                    shutil.copy2(cached_path, clip)
                    timeline_items.append((clip, float(entry["start"])))
                    report.append(
                        {
                            "index": cached_index,
                            "start": round(float(entry["start"]), 3),
                            "end": round(float(entry["end"]), 3),
                            "text": entry["text"],
                            "block": block_index,
                            "clipStartInBlock": None,
                            "clipEndInBlock": None,
                            "savedClip": str(cached_path),
                            "reused": True,
                        }
                    )
                print(f"VieNeu marker-block: block {block_index}/{len(blocks)} reused {len(cached)} clips")
                return entry_index + len(cached)

        split_with_asr = bool(pack.get("markerBlockAsrSplit", False)) and asr_model is not None
        block_text = marker_text_for_block(block, marker, use_marker=not split_with_asr)
        block_wav = tmp / f"block_{block_index:04d}.wav"
        try:
            render_block(tts, pack, ref_codes, block_text, block_wav, asr_model=asr_model)
        except RuntimeError as exc:
            if len(block) > 1:
                print(f"VieNeu marker-block render fallback to single cues: block={block_index} reason={exc}")
                for single in block:
                    entry_index = render_one_block(block_index * 1000 + entry_index + 1, [single], tmp, entry_index)
                return entry_index
            if bool(pack.get("markerBlockSingleBestEffort", True)):
                single_pack = dict(pack)
                single_pack["markerBlockAllowBestEffort"] = True
                single_pack["clipQualityAllowBestEffort"] = True
                single_pack["clipQualityAllowNonArtifactBestEffort"] = True
                try:
                    render_block(tts, single_pack, ref_codes, block_text, block_wav, asr_model=asr_model)
                except RuntimeError as single_exc:
                    if not bool(pack.get("markerBlockContinueOnFail", True)):
                        raise
                    exc = single_exc
                else:
                    exc = None
            if exc is not None:
                entry_index += 1
                clip = tmp / f"clip_{entry_index:05d}.wav"
                saved_clip = save_dir / f"clip_{entry_index:05d}_{float(block[0]['start']):.3f}s_FAILED.wav"
                if bool(pack.get("markerBlockContinueOnFail", True)):
                    make_silence_clip(clip, float(pack.get("markerBlockFailSilenceSec", 0.45)))
                    shutil.copy2(clip, saved_clip)
                    timeline_items.append((clip, float(block[0]["start"])))
                    item = {
                        "index": entry_index,
                        "start": round(float(block[0]["start"]), 3),
                        "end": round(float(block[0]["end"]), 3),
                        "text": block[0]["text"],
                        "block": block_index,
                        "failed": True,
                        "reason": str(exc),
                        "savedClip": str(saved_clip),
                    }
                    failed_report.append(item)
                    report.append(item)
                    print(f"VieNeu marker-block FAILED clip={entry_index} saved silence placeholder reason={exc}")
                    return entry_index
                raise
        if len(block) > 1 and not block_has_enough_text(asr_model, block_wav, block_text, pack):
            print(f"VieNeu marker-block fallback to single cues: block={block_index}")
            for single in block:
                entry_index = render_one_block(block_index * 1000 + entry_index + 1, [single], tmp, entry_index)
            return entry_index
        shutil.copy2(block_wav, save_dir / f"block_{block_index:04d}.wav")
        duration = audio_duration(block_wav)
        needed = max(0, len(block) - 1)
        if split_with_asr:
            ranges = split_points_by_asr_words(asr_model, block_wav, block, duration)
            silences = []
            marker_silences = []
        else:
            silences = detect_silences(block_wav, noise_db, min_silence)
            if needed:
                try:
                    marker_silences = choose_marker_silences(silences, needed, duration, min_gap)
                except RuntimeError as exc:
                    marker_silences = estimate_marker_silences(block, needed, duration)
                    print(f"VieNeu marker-block fallback split: block={block_index} reason={exc}")
            else:
                marker_silences = []
            starts = [0.0]
            ends: list[float] = []
            for silence_start, silence_end in marker_silences:
                ends.append(min(duration, silence_start + tail_keep))
                starts.append(max(0.0, silence_end - head_keep))
            ends.append(duration)
            ranges = list(zip(starts, ends))
        for local_index, entry in enumerate(block):
            entry_index += 1
            clip = tmp / f"clip_{entry_index:05d}.wav"
            saved_clip = save_dir / f"clip_{entry_index:05d}_{float(entry['start']):.3f}s.wav"
            clip_start, clip_end = ranges[local_index]
            cut_audio(block_wav, clip, clip_start, clip_end)
            trim_head_artifact_by_vad(clip, str(entry["text"]), pack)
            trim_tail_artifact_by_vad(clip, str(entry["text"]), asr_model, pack)
            if asr_model is not None and not block_has_enough_text(asr_model, clip, str(entry["text"]), pack):
                print(f"VieNeu marker-block clip fallback rerender: clip={entry_index}")
                try:
                    render_block(tts, pack, ref_codes, str(entry["text"]), clip, asr_model=asr_model)
                    trim_head_artifact_by_vad(clip, str(entry["text"]), pack)
                    trim_tail_artifact_by_vad(clip, str(entry["text"]), asr_model, pack)
                except RuntimeError as exc:
                    if not bool(pack.get("markerBlockContinueOnFail", True)):
                        raise
                    failed_clip = save_dir / f"clip_{entry_index:05d}_{float(entry['start']):.3f}s_FAILED.wav"
                    make_silence_clip(clip, float(pack.get("markerBlockFailSilenceSec", 0.45)))
                    shutil.copy2(clip, failed_clip)
                    failed_report.append(
                        {
                            "index": entry_index,
                            "start": round(float(entry["start"]), 3),
                            "end": round(float(entry["end"]), 3),
                            "text": entry["text"],
                            "block": block_index,
                            "failed": True,
                            "reason": str(exc),
                            "savedClip": str(failed_clip),
                        }
                    )
            shutil.copy2(clip, saved_clip)
            timeline_items.append((clip, float(entry["start"])))
            report.append(
                {
                    "index": entry_index,
                    "start": round(float(entry["start"]), 3),
                    "end": round(float(entry["end"]), 3),
                    "text": entry["text"],
                    "block": block_index,
                    "clipStartInBlock": round(clip_start, 3),
                    "clipEndInBlock": round(clip_end, 3),
                    "savedClip": str(saved_clip),
                }
            )
        print(
            f"VieNeu marker-block: block {block_index}/{len(blocks)} "
            f"cues={len(block)} silences={len(silences)} markers={len(marker_silences)}"
        )
        return entry_index

    try:
        with tempfile.TemporaryDirectory(prefix="vieneu_marker_srt_") as tmp_name:
            tmp = Path(tmp_name)
            entry_index = 0
            for block_index, block in enumerate(blocks, start=1):
                entry_index = render_one_block(block_index, block, tmp, entry_index)
                elapsed = max(0.001, time.perf_counter() - started)
                avg_block_sec = elapsed / max(1, block_index)
                eta_sec = avg_block_sec * max(0, len(blocks) - block_index)
                avg_clip_sec = elapsed / max(1, entry_index)
                progress_path.write_text(
                    json.dumps(
                        {
                            "updatedAt": datetime.now().isoformat(timespec="seconds"),
                            "doneBlocks": block_index,
                            "totalBlocks": len(blocks),
                            "doneClips": entry_index,
                            "failedClips": len(failed_report),
                            "out": str(out_mp3),
                            "elapsedSec": round(elapsed, 2),
                            "avgBlockSec": round(avg_block_sec, 2),
                            "avgClipSec": round(avg_clip_sec, 2),
                            "etaSec": round(eta_sec, 2),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(
                    f"VieNeu marker-block progress: {block_index}/{len(blocks)} blocks "
                    f"clips={entry_index}/{len(entries)} avg={avg_clip_sec:.2f}s/clip eta={eta_sec/60:.1f}m"
                )
            mix_timeline_audio(timeline_items, out_mp3, tmp, pack)
        (save_dir / "marker_report.json").write_text(
            json.dumps(
                {
                    "createdAt": datetime.now().isoformat(timespec="seconds"),
                    "entries": len(entries),
                    "blocks": len(blocks),
                    "okClips": len([item for item in report if not item.get("failed")]),
                    "failedClips": len(failed_report),
                    "clips": report,
                    "failed": failed_report,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if failed_report:
            (save_dir / "failed_clips.json").write_text(json.dumps(failed_report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"VieNeu marker-block done: {out_mp3}")
    finally:
        tts.close()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-dir", type=Path, required=True)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", type=int, default=1, help="1-based first SRT cue to render.")
    parser.add_argument("--limit", type=int, default=0, help="Render only the first N SRT cues for testing.")
    args = parser.parse_args()
    pack = load_pack(args.pack_dir.resolve())
    if args.limit > 0 or args.start > 1:
        source_srt = args.srt.resolve()
        all_entries = parse_srt(source_srt)
        start_index = max(1, args.start) - 1
        end_index = None if args.limit <= 0 else start_index + args.limit
        entries = all_entries[start_index:end_index]
        limited = args.out.resolve().with_suffix(".limit.srt")
        lines = []
        for index, entry in enumerate(entries, start=max(1, args.start)):
            def fmt(sec: float) -> str:
                ms = int(round(sec * 1000))
                h, rem = divmod(ms, 3600000)
                m, rem = divmod(rem, 60000)
                s, milli = divmod(rem, 1000)
                return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"
            lines.append(f"{index}\n{fmt(float(entry['start']))} --> {fmt(float(entry['end']))}\n{entry['text']}\n")
        limited.write_text("\n".join(lines), encoding="utf-8")
        args.srt = limited
    render_srt_marker_blocks(pack, args.srt.resolve(), args.out.resolve())


if __name__ == "__main__":
    main()
