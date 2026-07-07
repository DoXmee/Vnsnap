from __future__ import annotations

import argparse
import html
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / "ffmpeg.exe"
DEFAULT_OUT = ROOT / "vieneu_work" / "finetune_dataset" / "thanh_thao_sentence_clean_v1_20260605"


@dataclass(frozen=True)
class Source:
    label: str
    audio: Path
    srt: Path


@dataclass
class Entry:
    index: int
    start: float
    end: float
    text: str


FILLER_RE = re.compile(r"(^|\s)(ừm|ưm|ờ|ừ|ờm|um|uhm|uh|hm|hmm|ơ|ơm)(\s|,|\.|!|\?|$)", re.IGNORECASE)
LEADING_EXPRESSION_RE = re.compile(r"^\s*(á|oa|ha\s+ha|hahaha|hà\s+hà|ôi|ơ|úi|trời)\b", re.IGNORECASE)
BAD_RE = re.compile(r"https?://|www\.|@|#|[A-Za-z]{4,}")
END_PUNCT_RE = re.compile(r"[.!?…]$")


def fix_mojibake(text: str) -> str:
    markers = ("Ãƒ", "Ã„", "Ã†", "Ã¡Âº", "Ã¡Â»", "Ã‚")
    if not any(marker in text for marker in markers):
        return text
    try:
        fixed = text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except Exception:
        return text
    before = sum(text.count(marker) for marker in markers)
    after = sum(fixed.count(marker) for marker in markers)
    return fixed if after < before else text


def clean_text(text: str) -> str:
    text = fix_mojibake(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("…", ".")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_time(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


def parse_srt(path: Path) -> list[Entry]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\r?\n\r?\n+", raw.strip())
    entries: list[Entry] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        try:
            index = int(re.sub(r"\D+", "", lines[0]) or "0")
            left, right = [part.strip() for part in lines[1].split("-->", 1)]
            text = clean_text(" ".join(lines[2:]))
        except Exception:
            continue
        if text:
            entries.append(Entry(index=index, start=parse_time(left), end=parse_time(right), text=text))
    return entries


def reject_reason(entry: Entry, min_dur: float, max_dur: float, min_chars: int, max_chars: int) -> str | None:
    text = entry.text
    dur = entry.end - entry.start
    if dur < min_dur:
        return "too_short_duration"
    if dur > max_dur:
        return "too_long_duration"
    if len(text) < min_chars:
        return "too_short_text"
    if len(text) > max_chars:
        return "too_long_text"
    if not END_PUNCT_RE.search(text):
        return "no_tail_punct"
    if FILLER_RE.search(text):
        return "filler"
    if LEADING_EXPRESSION_RE.search(text):
        return "leading_expression"
    if BAD_RE.search(text):
        return "bad_token"
    if text.count(".") + text.count("?") + text.count("!") > 2:
        return "too_many_sentences"
    if text.count(",") > 3:
        return "too_many_commas"
    if sum(ch.isdigit() for ch in text) / max(1, len(text)) > 0.15:
        return "too_many_digits"
    return None


def cut_audio(source: Path, out_wav: Path, start: float, end: float, head_pad: float, tail_pad: float) -> None:
    start = max(0.0, start - head_pad)
    end = max(start + 0.25, end + tail_pad)
    cmd = [
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
        str(source),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
        str(out_wav),
    ]
    subprocess.run(cmd, check=True)


def build_dataset(args: argparse.Namespace) -> None:
    sources = [
        Source("s1", Path(args.audio1), Path(args.srt1)),
        Source("s2", Path(args.audio2), Path(args.srt2)),
    ]
    out_dir = Path(args.out_dir).resolve()
    raw_dir = out_dir / "raw_audio"
    raw_dir.mkdir(parents=True, exist_ok=True)
    metadata: list[str] = []
    rejected: dict[str, int] = {}
    total_seen = 0
    total_seconds = 0.0

    for source in sources:
        if not source.audio.exists():
            raise FileNotFoundError(source.audio)
        if not source.srt.exists():
            raise FileNotFoundError(source.srt)
        for entry in parse_srt(source.srt):
            total_seen += 1
            reason = reject_reason(entry, args.min_dur, args.max_dur, args.min_chars, args.max_chars)
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            filename = f"{source.label}_sentence_{len(metadata) + 1:05d}.wav"
            out_wav = raw_dir / filename
            if not out_wav.exists():
                cut_audio(source.audio, out_wav, entry.start, entry.end, args.head_pad, args.tail_pad)
            metadata.append(f"{filename}|{entry.text}\n")
            total_seconds += max(0.0, entry.end - entry.start)
            if args.max_samples and len(metadata) >= args.max_samples:
                break
        if args.max_samples and len(metadata) >= args.max_samples:
            break

    if len(metadata) < args.min_samples:
        raise RuntimeError(f"Only {len(metadata)} accepted samples; refusing to build too-small dataset.")

    (out_dir / "metadata.csv").write_text("".join(metadata), encoding="utf-8")
    report = [
        f"accepted={len(metadata)}",
        f"total_seen={total_seen}",
        f"total_hours_without_pad={total_seconds / 3600:.3f}",
        f"min_dur={args.min_dur}",
        f"max_dur={args.max_dur}",
        f"min_chars={args.min_chars}",
        f"max_chars={args.max_chars}",
        f"head_pad={args.head_pad}",
        f"tail_pad={args.tail_pad}",
        "rejected=" + repr(dict(sorted(rejected.items()))),
        "mode=single_sentence_one_audio_per_sample",
    ]
    (out_dir / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"out_dir={out_dir}")
    print(f"accepted={len(metadata)} total_seen={total_seen} hours={total_seconds / 3600:.3f}")
    print(f"rejected={dict(sorted(rejected.items()))}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one-sentence clean VieNeu finetune dataset.")
    parser.add_argument("--audio1", default=r"D:\hanhan\1\voice_fulvid9ieng.mp3")
    parser.add_argument("--srt1", default=r"D:\hanhan\1\[VI]_hanhan1.srt")
    parser.add_argument("--audio2", default=str(ROOT / "voice_hanhan.mp3"))
    parser.add_argument("--srt2", default=str(ROOT / "[VI]hanhan_srt 1.srt"))
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--min-dur", type=float, default=1.05)
    parser.add_argument("--max-dur", type=float, default=5.8)
    parser.add_argument("--min-chars", type=int, default=10)
    parser.add_argument("--max-chars", type=int, default=95)
    parser.add_argument("--head-pad", type=float, default=0.04)
    parser.add_argument("--tail-pad", type=float, default=0.28)
    parser.add_argument("--max-samples", type=int, default=2600)
    parser.add_argument("--min-samples", type=int, default=700)
    args = parser.parse_args()
    build_dataset(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
