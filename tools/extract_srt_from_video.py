from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / "ffmpeg.exe"
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

APP_MODEL_CACHE = ROOT / "model_cache"
os.environ.setdefault("HF_HOME", str(APP_MODEL_CACHE / "huggingface"))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(APP_MODEL_CACHE / "huggingface" / "hub"))
os.environ.setdefault("TRANSFORMERS_CACHE", str(APP_MODEL_CACHE / "huggingface" / "transformers"))
os.environ.setdefault("TORCH_HOME", str(APP_MODEL_CACHE / "torch"))
os.environ.setdefault("XDG_CACHE_HOME", str(APP_MODEL_CACHE))
os.environ.setdefault("MODELSCOPE_CACHE", str(APP_MODEL_CACHE / "modelscope"))
os.environ.setdefault("PADDLE_HOME", str(APP_MODEL_CACHE / "paddle"))
os.environ.setdefault("PADDLEOCR_HOME", str(APP_MODEL_CACHE / "paddleocr"))
for _cache_key in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE", "TORCH_HOME", "MODELSCOPE_CACHE", "PADDLE_HOME", "PADDLEOCR_HOME"):
    try:
        Path(os.environ[_cache_key]).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log(message: str) -> None:
    print(message, flush=True)


def progress(percent: float, message: str) -> None:
    print(f"PROGRESS:{max(0, min(100, percent)):.1f}:{message}", flush=True)


def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        s += 1
        ms -= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def clean_caption_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def split_caption_text(text: str, max_chars: int = 72) -> list[str]:
    text = clean_caption_text(text)
    if not text:
        return []
    sentence_pieces = [p.strip() for p in re.split(r"(?<=[.!?\u3002\uff01\uff1f])\s*", text) if p.strip()]
    pieces: list[str] = []
    for sentence in sentence_pieces or [text]:
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        clause_pieces = [p.strip() for p in re.split(r"(?<=[,;:\uff0c\uff1b\uff1a\u3001])\s*", sentence) if p.strip()]
        pieces.extend(clause_pieces or [sentence])

    out: list[str] = []
    for piece in pieces or [text]:
        if len(piece) <= max_chars:
            out.append(piece)
            continue
        if contains_cjk(piece):
            out.extend(piece[start : start + max_chars] for start in range(0, len(piece), max_chars))
            continue
        words = piece.split()
        buf: list[str] = []
        for word in words:
            candidate = " ".join(buf + [word])
            if buf and len(candidate) > max_chars:
                out.append(" ".join(buf))
                buf = [word]
            else:
                buf.append(word)
        if buf:
            out.append(" ".join(buf))
    return [p for p in out if p]


def join_word_text(prev: str, token: str) -> str:
    token = clean_caption_text(token)
    if not token:
        return prev
    if not prev:
        return token
    if contains_cjk(prev[-1] + token[:1]) or re.match(r"^[,.;:!?，。！？；：、)]", token):
        return prev + token
    return prev + " " + token


def should_cut_text(text: str, duration: float, max_chars: int, min_duration: float) -> bool:
    text = clean_caption_text(text)
    if not text or duration < min_duration:
        return False
    if re.search(r"[.!?\u3002\uff01\uff1f]$", text):
        return True
    if re.search(r"[,;:\uff0c\uff1b\uff1a\u3001]$", text) and duration >= max(0.8, min_duration):
        return True
    if len(text) >= max_chars and re.search(r"[,;:\uff0c\uff1b\uff1a\u3001]$", text):
        return True
    return len(text) >= max_chars


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


def default_prompt(language: str) -> str:
    if (language or "").lower().startswith("zh"):
        return (
            "以下是中文短剧对白，可能包含玄学、道教、招魂、符咒、魂魄、施孤、寿数、主顾、"
            "一魄寄尘、借气安身、请直行、鬼打墙、封印等术语。请按原始普通话转写，不要翻译。"
        )
    if (language or "").lower().startswith("vi"):
        return (
            "Đây là lời thoại tiếng Việt cho video ngắn. Các cụm có thể xuất hiện: "
            "hôm nay, cải tạo, căn phòng nhỏ này, đầu tiên, dọn sạch đồ cũ trong phòng, "
            "nhìn thì đơn giản vậy thôi, khi bắt tay vào làm, rất nhiều chi tiết. "
            "Hãy chép lại đúng lời thoại, không dịch."
        )
    return ""


def default_prompt(language: str) -> str:
    """Clean ASR prompt. This overrides the legacy mojibake prompt above."""
    lang = (language or "").lower()
    if lang.startswith("zh"):
        return (
            "以下是中文短剧、动画、网文解说对白。请按原始普通话转写，不要翻译，不要改写。"
            "视频可能包含医学、玄学、道教、修仙、系统、穿越、重生、家庭伦理、都市爽文、军嫂、总裁等题材。"
            "请特别注意同音词和专业术语，例如：急性阑尾炎、血常规、白细胞计数、麦氏点、结肠外科、主刀、主治医、"
            "SCI二作、大医系统、大师级荷包缝合术、机械记忆、征象、靶样显影、签到、暂无、懵逼、倒霉蛋、划开肚皮、"
            "一魄寄尘、借气安身、魂魄、施孤、寿数、主顾、请直行、鬼打墙、封印。"
            "请优先输出语义正确的中文专有名词。"
        )
    if lang.startswith("vi"):
        return (
            "Đây là lời thoại tiếng Việt cho video ngắn. Các cụm có thể xuất hiện: "
            "hôm nay, cải tạo, căn phòng nhỏ này, đầu tiên, dọn sạch đồ cũ trong phòng, "
            "nhìn thì đơn giản vậy thôi, khi bắt tay vào làm, rất nhiều chi tiết. "
            "Hãy chép lại đúng lời thoại, không dịch."
        )
    return ""


def audio_duration_seconds(audio_path: Path) -> float:
    try:
        import soundfile as sf

        info = sf.info(str(audio_path))
        return float(info.frames) / float(info.samplerate or 16000)
    except Exception:
        return 0.0


def normalize_omni_result(result: dict, duration: float) -> dict:
    segments = result.get("segments", []) if isinstance(result, dict) else []
    chunks = [
        {"text": seg.get("text", ""), "timestamp": (seg.get("start"), seg.get("end"))}
        for seg in segments
    ]
    return {
        "chunks": chunks,
        "segments": segments,
        "language": result.get("language", "unknown"),
        "duration": result.get("duration", duration),
    }


def result_timing_suspicious(result: dict, audio_path: Path) -> bool:
    duration = audio_duration_seconds(audio_path)
    segments = result.get("segments", []) if isinstance(result, dict) else []
    if duration <= 5.0 or not segments:
        return False
    last_end = max([float(seg.get("end") or 0.0) for seg in segments] or [0.0])
    return last_end < duration * 0.60


def transcribe_whisperx(audio_path: Path, model_size: str, language: str, device: str, compute_type: str, prompt: str) -> dict:
    import whisperx

    if device == "cpu" and compute_type in {"float16", "fp16", "auto"}:
        compute_type = "int8"

    progress(18, f"WhisperX load model {model_size} on {device}/{compute_type}")
    model = whisperx.load_model(model_size, device=device, compute_type=compute_type)
    audio = whisperx.load_audio(str(audio_path))

    kwargs = {}
    if language and language.lower() not in {"auto", "detect", "none"}:
        kwargs["language"] = language
    initial_prompt = prompt or default_prompt(language)

    try:
        result = model.transcribe(audio, batch_size=16, initial_prompt=initial_prompt, **kwargs)
    except TypeError:
        result = model.transcribe(audio, batch_size=16, **kwargs)
    except IndexError:
        result = {"segments": [], "language": language if language not in {"auto", ""} else "unknown"}

    lang = result.get("language") or language or "unknown"
    raw_result = {
        "segments": [dict(seg) for seg in result.get("segments", [])],
        "language": lang,
        "duration": audio_duration_seconds(audio_path),
    }
    progress(58, f"WhisperX ASR xong: language={lang}, align word timing")

    try:
        align_model, metadata = whisperx.load_align_model(language_code=lang, device=device)
        result = whisperx.align(result.get("segments", []), align_model, metadata, audio, device, return_char_alignments=False)
        result["language"] = lang
        duration = audio_duration_seconds(audio_path)
        last_end = max([float(seg.get("end") or 0.0) for seg in result.get("segments", [])] or [0.0])
        if duration > 5.0 and last_end < duration * 0.60:
            log(f"WARNING: WhisperX align timing suspicious ({last_end:.2f}s/{duration:.2f}s), using raw ASR segments")
            result = raw_result
    except Exception as exc:
        log(f"WARNING: WhisperX align failed, fallback raw segments: {exc}")
        result = raw_result

    return normalize_omni_result(result, audio_duration_seconds(audio_path))


def transcribe_faster_whisper(audio_path: Path, model_size: str, language: str, device: str, compute_type: str, prompt: str) -> dict:
    from faster_whisper import WhisperModel

    if device == "cpu" and compute_type in {"float16", "fp16", "auto"}:
        compute_type = "int8"

    kwargs = {}
    if language and language.lower() not in {"auto", "detect", "none"}:
        kwargs["language"] = language
    initial_prompt = prompt or default_prompt(language)
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt

    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as exc:
        if device == "cuda":
            log(f"WARNING: CUDA ASR load failed, fallback CPU int8: {exc}")
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
        else:
            raise

    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        best_of=5,
        patience=1.0,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 220,
            "speech_pad_ms": 120,
            "threshold": 0.35,
        },
        word_timestamps=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.35,
        compression_ratio_threshold=2.6,
        **kwargs,
    )
    segments = list(segments)
    return {
        "chunks": [{"text": seg.text, "timestamp": (seg.start, seg.end)} for seg in segments],
        "segments": [
            {
                "text": seg.text,
                "start": seg.start,
                "end": seg.end,
                "words": [
                    {"word": w.word, "start": w.start, "end": w.end, "probability": w.probability}
                    for w in (seg.words or [])
                ],
            }
            for seg in segments
        ],
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
    }


def sanitize_funasr_text(text: str) -> str:
    text = re.sub(r"<\|[^|]*\|>", "", text or "")
    text = re.sub(r"\s+", "", text)
    return text.strip()


def find_sensevoice_model() -> str:
    """Find a pre-downloaded SenseVoiceSmall model near the app/repo roots."""
    candidates: list[Path] = []
    for base in [ROOT, *ROOT.parents]:
        candidates.append(base / "model_cache" / "modelscope" / "models" / "iic" / "SenseVoiceSmall")
        candidates.append(base / "models" / "SenseVoiceSmall_hf")
    for candidate in candidates:
        if (candidate / "model.pt").exists() or (candidate / "model.bin").exists():
            return str(candidate)
    return "iic/SenseVoiceSmall"


def funasr_result_to_segments(result, duration: float) -> list[dict]:
    rows = result if isinstance(result, list) else [result]
    segments: list[dict] = []
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
                segments.append({"start": start, "end": end, "text": text, "words": []})
        if segments:
            continue

        timestamps = row.get("timestamp") or row.get("timestamps")
        text = sanitize_funasr_text(str(row.get("text", "")))
        if text and isinstance(timestamps, list) and timestamps:
            buf = ""
            start_ms = None
            end_ms = None
            for ch, ts in zip(list(text), timestamps):
                if not isinstance(ts, (list, tuple)) or len(ts) < 2:
                    continue
                if start_ms is None:
                    start_ms = float(ts[0])
                end_ms = float(ts[1])
                buf += ch
                if ch in "。！？!?，、；;：" or len(buf) >= 28:
                    if buf and start_ms is not None and end_ms is not None:
                        segments.append({"start": start_ms / 1000.0, "end": end_ms / 1000.0, "text": buf, "words": []})
                    buf = ""
                    start_ms = None
                    end_ms = None
            if buf and start_ms is not None and end_ms is not None:
                segments.append({"start": start_ms / 1000.0, "end": end_ms / 1000.0, "text": buf, "words": []})
            continue

        if text:
            parts = split_caption_text(text, max_chars=28)
            total_chars = sum(max(1, len(part)) for part in parts) or 1
            remaining = max(0.5, duration - cursor)
            for part in parts:
                span = max(0.45, remaining * (len(part) / total_chars))
                segments.append({"start": cursor, "end": min(duration, cursor + span), "text": part, "words": []})
                cursor = min(duration, cursor + span)
    return segments


def transcribe_sensevoice(audio_path: Path, language: str, device: str) -> dict:
    """Transcribe Chinese audio with FunASR SenseVoiceSmall."""
    from funasr import AutoModel

    model_name = find_sensevoice_model()
    init_kwargs = {
        "model": model_name,
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
        "device": "cuda:0" if device == "cuda" else "cpu",
        "disable_update": True,
        "ncpu": 8,
        "trust_remote_code": True,
    }
    try:
        model = AutoModel(**init_kwargs)
    except Exception as exc:
        if device != "cuda":
            raise
        log(f"WARNING: SenseVoice CUDA load failed, fallback CPU: {exc}")
        init_kwargs["device"] = "cpu"
        model = AutoModel(**init_kwargs)

    gen_kwargs = {"input": str(audio_path), "batch_size": 1, "use_itn": True}
    if language and language.lower() not in {"auto", "detect", "none"}:
        gen_kwargs["language"] = "zh" if language.lower().startswith("zh") else language
    result = model.generate(**gen_kwargs)
    duration = audio_duration_seconds(audio_path)
    segments = funasr_result_to_segments(result, duration)
    return {
        "chunks": [{"text": seg["text"], "timestamp": (seg["start"], seg["end"])} for seg in segments],
        "segments": segments,
        "language": language if language and language != "auto" else "zh",
        "duration": duration,
        "raw": result,
    }


def transcribe(audio_path: Path, model_size: str, language: str, device: str, compute_type: str, prompt: str, engine: str) -> dict:
    if engine == "sensevoice":
        progress(16, "FunASR SenseVoiceSmall direct")
        return transcribe_sensevoice(audio_path, language, device)
    if engine == "faster-whisper":
        progress(16, f"Local faster-whisper direct model={model_size}")
        return transcribe_faster_whisper(audio_path, model_size, language, device, compute_type, prompt)
    if engine in {"auto", "whisperx"}:
        try:
            os.environ["ASR_MODEL_WHISPERX"] = model_size
            os.environ["ASR_MODEL_FASTER"] = model_size
            from omni_asr_backend import WhisperXBackend

            progress(16, f"Omni ASR backend: WhisperXBackend model={model_size}")
            backend = WhisperXBackend()
            result = backend.transcribe(str(audio_path), word_timestamps=True)
            if result_timing_suspicious(result, audio_path):
                raise RuntimeError("Omni WhisperX returned suspicious compressed timing")
            return result
        except Exception as exc:
            if engine == "whisperx":
                log(f"WARNING: Omni WhisperX backend failed, trying local WhisperX wrapper: {exc}")
            else:
                log(f"WARNING: Omni WhisperX backend failed, fallback wrapper: {exc}")
        try:
            return transcribe_whisperx(audio_path, model_size, language, device, compute_type, prompt)
        except Exception as exc:
            if engine == "whisperx":
                raise
            log(f"WARNING: WhisperX unavailable/failed, fallback faster-whisper: {exc}")
    if engine in {"auto", "faster-whisper"}:
        try:
            os.environ["ASR_MODEL_FASTER"] = model_size
            from omni_asr_backend import FasterWhisperBackend

            progress(16, f"Omni ASR backend: FasterWhisperBackend model={model_size}")
            backend = FasterWhisperBackend()
            return backend.transcribe(str(audio_path), word_timestamps=True)
        except Exception as exc:
            log(f"WARNING: Omni FasterWhisper backend failed, fallback local wrapper: {exc}")
    return transcribe_faster_whisper(audio_path, model_size, language, device, compute_type, prompt)


def write_srt_from_omni_segments(result: dict, out_path: Path) -> int:
    from omni_subtitle_segmenter import segment_for_subtitles

    raw_segments = result.get("segments", []) if isinstance(result, dict) else []
    segments = segment_for_subtitles(raw_segments)
    if segments:
        tiny_count = sum(1 for seg in segments if float(seg.get("end", 0.0)) - float(seg.get("start", 0.0)) < 0.30)
        if tiny_count > max(3, len(segments) // 2):
            log("WARNING: Omni subtitle split produced many tiny cues, merging tiny subtitle fragments")
            segments = merge_tiny_subtitle_segments(segments)
    lines: list[str] = []
    for idx, seg in enumerate(segments, 1):
        text = clean_caption_text(str(seg.get("text", "")))
        if not text:
            continue
        lines.append(str(idx))
        lines.append(f"{format_srt_time(float(seg.get('start', 0.0)))} --> {format_srt_time(float(seg.get('end', 0.0)))}")
        lines.append(text)
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return len(segments)


def merge_tiny_subtitle_segments(segments: list[dict], min_duration: float = 0.65, min_chars: int = 8, max_chars: int = 84) -> list[dict]:
    """Repair over-split Omni subtitle fragments without changing word order."""
    out: list[dict] = []
    for seg in segments:
        text = clean_caption_text(str(seg.get("text", "")))
        if not text:
            continue
        cur = {**seg, "text": text, "start": float(seg.get("start", 0.0)), "end": float(seg.get("end", 0.0))}
        dur = cur["end"] - cur["start"]
        if out and (dur < min_duration or len(text) < min_chars):
            prev = out[-1]
            joined = clean_caption_text(f"{prev.get('text', '')} {text}")
            combined_dur = cur["end"] - float(prev.get("start", 0.0))
            if (len(joined) <= max_chars or dur < 0.30 or len(text) <= 4) and combined_dur <= 6.0:
                prev["text"] = joined
                prev["end"] = max(float(prev.get("end", 0.0)), cur["end"])
                continue
        out.append(cur)
    return out


def write_srt_from_words(segments, out_path: Path, max_chars: int, min_duration: float) -> int:
    lines: list[str] = []
    count = 0
    last_end = 0.0

    def flush(start: float, end: float, text: str) -> None:
        nonlocal count, last_end
        text = clean_caption_text(text)
        if not text or end <= start:
            return
        if start < last_end:
            start = last_end
        if end <= start:
            return
        count += 1
        lines.append(str(count))
        lines.append(f"{format_srt_time(start)} --> {format_srt_time(end)}")
        lines.append(text)
        lines.append("")
        last_end = end

    def seg_value(seg, key, default=None):
        return seg.get(key, default) if isinstance(seg, dict) else getattr(seg, key, default)

    def word_value(word, key, default=None):
        return word.get(key, default) if isinstance(word, dict) else getattr(word, key, default)

    for seg in segments:
        words = [w for w in (seg_value(seg, "words", None) or []) if clean_caption_text(word_value(w, "word", word_value(w, "text", "")))]
        if not words:
            continue
        cue_text = ""
        cue_start = None
        cue_end = None
        for word in words:
            token = clean_caption_text(word_value(word, "word", word_value(word, "text", "")))
            start = float(word_value(word, "start", seg_value(seg, "start", 0.0)) or seg_value(seg, "start", 0.0))
            end = float(word_value(word, "end", start + 0.08) or (start + 0.08))
            if cue_start is None:
                cue_start = start
            cue_text = join_word_text(cue_text, token)
            cue_end = end
            if should_cut_text(cue_text, cue_end - cue_start, max_chars, min_duration):
                flush(cue_start, cue_end, cue_text)
                cue_text = ""
                cue_start = None
                cue_end = None
        if cue_text and cue_start is not None and cue_end is not None:
            flush(cue_start, cue_end, cue_text)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return count


def write_srt_from_segments(segments, out_path: Path, max_chars: int) -> int:
    lines: list[str] = []
    count = 0
    last_end = 0.0
    for seg in segments:
        start = float(seg.get("start", 0.0) if isinstance(seg, dict) else seg.start)
        end = float(seg.get("end", 0.0) if isinstance(seg, dict) else seg.end)
        text = clean_caption_text(seg.get("text", "") if isinstance(seg, dict) else seg.text)
        if not text or end <= start:
            continue
        if start < last_end:
            start = last_end
        if end <= start:
            continue
        parts = split_caption_text(text, max_chars=max_chars)
        if not parts:
            continue
        total_chars = sum(max(1, len(part)) for part in parts)
        cursor = start
        duration = end - start
        for part_index, part in enumerate(parts):
            part_end = end if part_index == len(parts) - 1 else min(end, cursor + max(0.45, duration * (len(part) / total_chars)))
            if part_end <= cursor:
                continue
            count += 1
            lines.append(str(count))
            lines.append(f"{format_srt_time(cursor)} --> {format_srt_time(part_end)}")
            lines.append(part)
            lines.append("")
            cursor = part_end
        last_end = end
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract SRT subtitles from a video/audio file using faster-whisper.")
    parser.add_argument("--input", required=True, help="Input video/audio path")
    parser.add_argument("--output", required=True, help="Output .srt path")
    parser.add_argument("--model", default="small", choices=["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"], help="Whisper model size")
    parser.add_argument("--language", default="auto", help="Language code, e.g. vi/zh/en, or auto")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"], help="ASR device")
    parser.add_argument("--compute-type", default="float16", help="faster-whisper compute type")
    parser.add_argument("--engine", default="auto", choices=["auto", "whisperx", "faster-whisper", "sensevoice"], help="ASR engine. sensevoice is recommended as Chinese ASR secondary source.")
    parser.add_argument("--max-chars", type=int, default=72, help="Split long ASR segments near this many chars")
    parser.add_argument("--min-duration", type=float, default=0.45, help="Minimum cue duration before punctuation split")
    parser.add_argument("--prompt", default="", help="Optional ASR initial prompt / glossary context")
    parser.add_argument("--no-word-timestamps", action="store_true", help="Fallback to raw Whisper segments")
    args = parser.parse_args()

    src = Path(args.input)
    out = Path(args.output)
    if not src.exists():
        raise FileNotFoundError(src)
    if not FFMPEG.exists():
        raise FileNotFoundError(FFMPEG)

    language = (args.language or "auto").lower()
    max_chars = max(12, args.max_chars)
    if language.startswith("zh") and args.max_chars == 72:
        max_chars = 28

    progress(2, "Tach audio 16k mono bang FFmpeg")
    with tempfile.TemporaryDirectory(prefix="extract_srt_") as tmp:
        wav = Path(tmp) / "audio.wav"
        extract_wav(src, wav)
        progress(12, f"Dang load ASR engine={args.engine} model={args.model}")
        result = transcribe(wav, args.model, args.language, args.device, args.compute_type, args.prompt, args.engine)
        detected = str(result.get("language", "unknown"))
        if detected.lower().startswith("zh") and args.max_chars == 72:
            max_chars = 28
        segments = result.get("segments", [])
        progress(82, f"ASR xong: language={detected} segments={len(segments)}")
        if args.no_word_timestamps:
            count = write_srt_from_segments(segments, out, max_chars=max_chars)
        else:
            count = write_srt_from_omni_segments(result, out)
            if count == 0:
                count = write_srt_from_words(segments, out, max_chars=max_chars, min_duration=max(0.2, args.min_duration))
            if count == 0:
                count = write_srt_from_segments(segments, out, max_chars=max_chars)
    progress(100, f"Xuat SRT thanh cong: {count} dong -> {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR:{exc}", file=sys.stderr, flush=True)
        raise
