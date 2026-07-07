from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
FFMPEG = ROOT / "ffmpeg.exe"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from extract_srt_from_video import (  # noqa: E402
    audio_duration_seconds,
    clean_caption_text,
    format_srt_time,
    split_caption_text,
    transcribe_faster_whisper,
    write_srt_from_omni_segments,
    write_srt_from_segments,
    write_srt_from_words,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


ZH_PROMPT = (
    "以下是中文短剧、动画、网文解说对白。请按原始普通话转写，不要翻译，不要改写。"
    "视频可能包含医学、系统、穿越、都市爽文、玄学、修仙等题材。"
    "请特别注意同音词和专业术语。"
)

MEDICAL_HOTWORDS = (
    "急性阑尾炎 血常规 白细胞计数 麦氏点 结肠外科 主刀 主治医 副主任 "
    "SCI二作 大医系统 签到 暂无 大师级荷包缝合术 机械记忆 征象 靶样显影 "
    "无菌原则 床旁超声 颈动脉脉搏 普外 实习生 手术室 手术衣 术前消毒"
)


def log(message: str) -> None:
    print(message, flush=True)


def extract_wav(video_path: Path, wav_path: Path) -> None:
    cmd = [
        str(FFMPEG),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(wav_path),
    ]
    subprocess.run(cmd, check=True)


def sanitize_funasr_text(text: str) -> str:
    text = re.sub(r"<\|[^|]*\|>", "", text or "")
    text = re.sub(r"\s+", "", text)
    return text.strip()


def write_segments_to_srt(segments: list[dict[str, Any]], out_path: Path, max_chars: int = 28) -> int:
    lines: list[str] = []
    count = 0
    last_end = 0.0
    for seg in segments:
        text = clean_caption_text(str(seg.get("text", "")))
        start = float(seg.get("start", 0.0) or 0.0)
        end = float(seg.get("end", 0.0) or 0.0)
        if not text or end <= start:
            continue
        if start < last_end:
            start = last_end
        if end <= start:
            continue
        parts = split_caption_text(text, max_chars=max_chars)
        total_chars = sum(max(1, len(part)) for part in parts) or 1
        cursor = start
        duration = end - start
        for idx, part in enumerate(parts):
            part_end = end if idx == len(parts) - 1 else min(end, cursor + max(0.35, duration * (len(part) / total_chars)))
            if part_end <= cursor:
                continue
            count += 1
            lines.extend([str(count), f"{format_srt_time(cursor)} --> {format_srt_time(part_end)}", part, ""])
            cursor = part_end
        last_end = end
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return count


def funasr_result_to_segments(result: Any, duration: float) -> list[dict[str, Any]]:
    rows = result if isinstance(result, list) else [result]
    segments: list[dict[str, Any]] = []
    cursor = 0.0

    for row in rows:
        if not isinstance(row, dict):
            continue
        sentence_info = row.get("sentence_info") or row.get("sentences") or []
        for sentence in sentence_info:
            text = sanitize_funasr_text(str(sentence.get("text", "")))
            start = float(sentence.get("start", sentence.get("start_time", 0)) or 0) / 1000.0
            end = float(sentence.get("end", sentence.get("end_time", 0)) or 0) / 1000.0
            if text and end > start:
                segments.append({"start": start, "end": end, "text": text})

        if segments:
            continue

        timestamps = row.get("timestamp") or row.get("timestamps")
        text = sanitize_funasr_text(str(row.get("text", "")))
        if text and isinstance(timestamps, list) and timestamps:
            chars = list(text)
            if len(timestamps) >= len(chars):
                buf = ""
                start_ms = None
                end_ms = None
                for ch, ts in zip(chars, timestamps):
                    if not isinstance(ts, (list, tuple)) or len(ts) < 2:
                        continue
                    if start_ms is None:
                        start_ms = float(ts[0])
                    end_ms = float(ts[1])
                    buf += ch
                    if ch in "。！？!?，、；;：" or len(buf) >= 28:
                        if buf and start_ms is not None and end_ms is not None:
                            segments.append({"start": start_ms / 1000.0, "end": end_ms / 1000.0, "text": buf})
                        buf = ""
                        start_ms = None
                        end_ms = None
                if buf and start_ms is not None and end_ms is not None:
                    segments.append({"start": start_ms / 1000.0, "end": end_ms / 1000.0, "text": buf})
            continue

        if text:
            parts = split_caption_text(text, max_chars=28)
            total_chars = sum(max(1, len(part)) for part in parts) or 1
            remaining = max(0.5, duration - cursor)
            for part in parts:
                span = max(0.45, remaining * (len(part) / total_chars))
                segments.append({"start": cursor, "end": min(duration, cursor + span), "text": part})
                cursor = min(duration, cursor + span)

    return segments


def run_faster(wav: Path, out_path: Path, device: str) -> dict[str, Any]:
    start = time.time()
    result = transcribe_faster_whisper(wav, "large-v3-turbo", "zh", device, "float16", ZH_PROMPT)
    count = write_srt_from_omni_segments(result, out_path)
    if count == 0:
        count = write_srt_from_words(result.get("segments", []), out_path, max_chars=28, min_duration=0.35)
    if count == 0:
        count = write_srt_from_segments(result.get("segments", []), out_path, max_chars=28)
    return {
        "engine": "faster-whisper-large-v3-turbo",
        "output": str(out_path),
        "cues": count,
        "seconds": round(time.time() - start, 2),
        "language": result.get("language", "unknown"),
        "segments": len(result.get("segments", [])),
    }


def run_funasr(wav: Path, out_path: Path, model_name: str, device: str, hotword: str = "") -> dict[str, Any]:
    from funasr import AutoModel

    start = time.time()
    local_sensevoice = ROOT / "models" / "SenseVoiceSmall_hf"
    resolved_model_name = str(local_sensevoice) if model_name == "iic/SenseVoiceSmall" and local_sensevoice.exists() else model_name
    init_kwargs: dict[str, Any] = {
        "model": resolved_model_name,
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
        "device": "cuda:0" if device == "cuda" else "cpu",
        "disable_update": True,
        "ncpu": 8,
    }
    if "SenseVoice" in model_name or "SenseVoice" in resolved_model_name:
        init_kwargs["trust_remote_code"] = True
    else:
        init_kwargs["punc_model"] = "ct-punc"
    try:
        model = AutoModel(**init_kwargs)
    except Exception as exc:
        if device != "cuda":
            raise
        log(f"WARNING: {model_name} CUDA load failed, fallback CPU: {exc}")
        init_kwargs["device"] = "cpu"
        model = AutoModel(**init_kwargs)

    gen_kwargs: dict[str, Any] = {
        "input": str(wav),
        "batch_size": 1,
    }
    if "SenseVoice" in model_name or "SenseVoice" in resolved_model_name:
        gen_kwargs["language"] = "zh"
        gen_kwargs["use_itn"] = True
    if hotword:
        gen_kwargs["hotword"] = hotword

    result = model.generate(**gen_kwargs)
    duration = audio_duration_seconds(wav)
    segments = funasr_result_to_segments(result, duration)
    count = write_segments_to_srt(segments, out_path, max_chars=28)
    raw_path = out_path.with_suffix(".raw.json")
    raw_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "engine": model_name + ("+hotwords" if hotword else ""),
        "resolved_model": resolved_model_name,
        "output": str(out_path),
        "raw": str(raw_path),
        "cues": count,
        "seconds": round(time.time() - start, 2),
        "segments": len(segments),
    }


def simple_energy_vad(wav_path: Path, min_speech_ms: int = 260, min_silence_ms: int = 360, pad_ms: int = 120) -> tuple[int, list[tuple[float, float, Any]]]:
    """Return speech chunks using a conservative RMS gate for engines without timestamps."""
    import numpy as np
    import soundfile as sf

    samples, sr = sf.read(str(wav_path), dtype="float32")
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    frame = max(1, int(sr * 0.03))
    hop = frame
    rms = []
    for start in range(0, len(samples), hop):
        chunk = samples[start : start + frame]
        if len(chunk) == 0:
            continue
        rms.append(float(np.sqrt(np.mean(chunk * chunk) + 1e-12)))
    if not rms:
        return sr, []
    rms_arr = np.asarray(rms, dtype=np.float32)
    threshold = max(0.006, float(np.percentile(rms_arr, 72)) * 0.45)
    voiced = rms_arr >= threshold
    min_speech_frames = max(1, int(min_speech_ms / 30))
    min_silence_frames = max(1, int(min_silence_ms / 30))
    pad_frames = max(0, int(pad_ms / 30))
    chunks: list[tuple[int, int]] = []
    active = False
    start_frame = 0
    silence = 0
    for i, is_voiced in enumerate(voiced):
        if is_voiced:
            if not active:
                active = True
                start_frame = i
            silence = 0
        elif active:
            silence += 1
            if silence >= min_silence_frames:
                end_frame = i - silence + 1
                if end_frame - start_frame >= min_speech_frames:
                    chunks.append((max(0, start_frame - pad_frames), min(len(voiced), end_frame + pad_frames)))
                active = False
                silence = 0
    if active and len(voiced) - start_frame >= min_speech_frames:
        chunks.append((max(0, start_frame - pad_frames), len(voiced)))

    merged: list[tuple[int, int]] = []
    for start_frame, end_frame in chunks:
        if merged and start_frame - merged[-1][1] < int(0.18 / 0.03):
            merged[-1] = (merged[-1][0], end_frame)
        else:
            merged.append((start_frame, end_frame))

    out = []
    for start_frame, end_frame in merged:
        start_sample = int(start_frame * hop)
        end_sample = min(len(samples), int(end_frame * hop + frame))
        out.append((start_sample / sr, end_sample / sr, samples[start_sample:end_sample].copy()))
    return sr, out


def run_sherpa_paraformer(wav: Path, out_path: Path) -> dict[str, Any]:
    import sherpa_onnx

    start = time.time()
    model_dir = ROOT / "models" / "sherpa-onnx-paraformer-zh-2024-03-09"
    model_path = model_dir / "model.int8.onnx"
    tokens_path = model_dir / "tokens.txt"
    recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
        paraformer=str(model_path),
        tokens=str(tokens_path),
        num_threads=8,
        provider="cpu",
    )
    sr, chunks = simple_energy_vad(wav)
    segments: list[dict[str, Any]] = []
    for chunk_start, chunk_end, samples in chunks:
        stream = recognizer.create_stream()
        stream.accept_waveform(sr, samples)
        recognizer.decode_stream(stream)
        text = clean_caption_text(getattr(stream.result, "text", ""))
        text = re.sub(r"\s+", "", text)
        if text:
            segments.append({"start": chunk_start, "end": chunk_end, "text": text})
    count = write_segments_to_srt(segments, out_path, max_chars=28)
    return {
        "engine": "sherpa-onnx-paraformer-zh-int8",
        "output": str(out_path),
        "cues": count,
        "seconds": round(time.time() - start, 2),
        "segments": len(segments),
        "note": "VAD chunk timing; text compare engine.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Render comparable Chinese SRT files with multiple local ASR engines.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument(
        "--engines",
        default="faster-whisper,sensevoice,paraformer,paraformer-hotwords",
        help="Comma list: faster-whisper,sensevoice,paraformer,paraformer-hotwords,sherpa-paraformer",
    )
    args = parser.parse_args()

    src = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        raise FileNotFoundError(src)
    if not FFMPEG.exists():
        raise FileNotFoundError(FFMPEG)

    summary: list[dict[str, Any]] = []
    total_start = time.time()
    with tempfile.TemporaryDirectory(prefix="asr_compare_") as tmp:
        wav = Path(tmp) / "audio.wav"
        log(f"[1/5] Extract audio 16k mono: {src.name}")
        extract_wav(src, wav)
        for engine in [e.strip() for e in args.engines.split(",") if e.strip()]:
            log(f"RUN:{engine}")
            try:
                if engine == "faster-whisper":
                    summary.append(run_faster(wav, out_dir / "01_faster_whisper_large_v3_turbo.srt", args.device))
                elif engine == "sensevoice":
                    summary.append(run_funasr(wav, out_dir / "02_funasr_sensevoice_small.srt", "iic/SenseVoiceSmall", args.device))
                elif engine == "paraformer":
                    summary.append(run_funasr(wav, out_dir / "03_funasr_paraformer_zh.srt", "paraformer-zh", args.device))
                elif engine == "paraformer-hotwords":
                    summary.append(run_funasr(wav, out_dir / "04_funasr_paraformer_zh_hotwords.srt", "paraformer-zh", args.device, hotword=MEDICAL_HOTWORDS))
                elif engine == "sherpa-paraformer":
                    summary.append(run_sherpa_paraformer(wav, out_dir / "05_sherpa_onnx_paraformer_zh_int8.srt"))
                else:
                    raise ValueError(f"Unknown engine: {engine}")
            except Exception as exc:
                log(f"ERROR:{engine}:{exc}")
                summary.append({"engine": engine, "error": str(exc)})

    report = {
        "input": str(src),
        "output_dir": str(out_dir),
        "total_seconds": round(time.time() - total_start, 2),
        "results": summary,
    }
    report_path = out_dir / "asr_compare_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"REPORT:{report_path}")
    for item in summary:
        if item.get("output"):
            log(f"OUTPUT:{item['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
