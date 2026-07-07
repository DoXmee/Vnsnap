from __future__ import annotations

import argparse
import html
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STRICT_META = (
    ROOT
    / "Release_App"
    / "TikTokVoiceStudio-win32-x64"
    / "resources"
    / "app"
    / "datasets"
    / "thanh-thao-1-qc-strict-1779139429883"
    / "metadata.csv"
)
SOURCE_AUDIO = Path(r"D:\hanhan\1\voice_fulvid9ieng.mp3")
SOURCE_SRT = Path(r"D:\hanhan\1\[VI]_hanhan1.srt")
DEFAULT_OUT_DIR = ROOT / "vieneu_work" / "finetune_dataset" / "thanh_thao_vieneu_v1"
FFMPEG = ROOT / "ffmpeg.exe"


@dataclass
class Entry:
    index: int
    start: float
    end: float
    text: str


def fix_mojibake(text: str) -> str:
    if not any(marker in text for marker in ("Ã", "Ä", "Æ", "á»", "áº")):
        return text
    try:
        return text.encode("latin1").decode("utf-8")
    except UnicodeError:
        return text


def clean_text(text: str) -> str:
    text = fix_mojibake(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_time(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 1000.0
    )


def parse_srt(path: Path) -> list[Entry]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\r?\n\r?\n+", raw.strip())
    entries: list[Entry] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        try:
            index = int(lines[0])
            left, right = [part.strip() for part in lines[1].split("-->")]
            text = clean_text(" ".join(lines[2:]))
        except Exception:
            continue
        if text:
            entries.append(Entry(index=index, start=parse_time(left), end=parse_time(right), text=text))
    return entries


def strict_indexes(path: Path) -> set[int]:
    indexes: set[int] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "|" not in line:
            continue
        wav_path, _ = line.split("|", 1)
        match = re.search(r"(\d{6})\.wav$", wav_path)
        if match:
            indexes.add(int(match.group(1)))
    return indexes


def bad_text(text: str) -> bool:
    if len(text) < 12:
        return True
    if re.search(r"https?://|www\.|@", text, flags=re.I):
        return True
    if text.count("?") + text.count("!") > 2:
        return True
    if re.search(r"[A-Za-z]{4,}", text):
        return True
    return False


def build_chunks(
    entries: list[Entry],
    keep: set[int],
    min_dur: float,
    max_dur: float,
    max_gap: float,
    strict_only: bool,
) -> list[list[Entry]]:
    usable = [
        entry
        for entry in entries
        if (not strict_only or entry.index in keep) and not bad_text(entry.text)
    ]
    chunks: list[list[Entry]] = []
    cur: list[Entry] = []

    def flush() -> None:
        nonlocal cur
        if cur and min_dur <= cur[-1].end - cur[0].start <= max_dur:
            chunks.append(cur)
        cur = []

    for entry in usable:
        if not cur:
            cur = [entry]
            continue

        gap = entry.start - cur[-1].end
        candidate_dur = entry.end - cur[0].start
        if 0 <= gap <= max_gap and candidate_dur <= max_dur:
            cur.append(entry)
            if candidate_dur >= min_dur and len(" ".join(e.text for e in cur)) >= 35:
                flush()
        else:
            flush()
            cur = [entry]

    flush()
    return chunks


def cut_chunk(source: Path, out_wav: Path, start: float, end: float) -> None:
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-chunks", type=int, default=2200)
    parser.add_argument("--min-dur", type=float, default=3.0)
    parser.add_argument("--max-dur", type=float, default=8.5)
    parser.add_argument("--max-gap", type=float, default=0.35)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--source-audio", type=Path, default=SOURCE_AUDIO)
    parser.add_argument("--source-srt", type=Path, default=SOURCE_SRT)
    parser.add_argument("--use-all-srt", action="store_true")
    args = parser.parse_args()

    out_dir = args.out_dir.resolve()
    raw_dir = out_dir / "raw_audio"
    raw_dir.mkdir(parents=True, exist_ok=True)

    source_audio = args.source_audio.resolve()
    source_srt = args.source_srt.resolve()
    entries = parse_srt(source_srt)
    keep = strict_indexes(STRICT_META)
    chunks = build_chunks(
        entries,
        keep,
        args.min_dur,
        args.max_dur,
        args.max_gap,
        strict_only=not args.use_all_srt,
    )
    chunks = chunks[: args.max_chunks]

    metadata_lines: list[str] = []
    total_duration = 0.0
    for i, chunk in enumerate(chunks, start=1):
        start = chunk[0].start
        end = chunk[-1].end
        duration = end - start
        text = " ".join(entry.text for entry in chunk)
        filename = f"thanh_thao_{i:05d}.wav"
        out_wav = raw_dir / filename
        if not out_wav.exists():
            cut_chunk(source_audio, out_wav, start, end)
        metadata_lines.append(f"{filename}|{text}\n")
        total_duration += duration

    (out_dir / "metadata.csv").write_text("".join(metadata_lines), encoding="utf-8")
    (out_dir / "report.txt").write_text(
        "\n".join(
            [
                f"source_audio={source_audio}",
                f"source_srt={source_srt}",
                f"strict_metadata={STRICT_META}",
                f"chunks={len(chunks)}",
                f"total_hours={total_duration / 3600:.3f}",
                f"min_dur={args.min_dur}",
                f"max_dur={args.max_dur}",
                f"max_gap={args.max_gap}",
                f"use_all_srt={args.use_all_srt}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"out_dir={out_dir}")
    print(f"chunks={len(chunks)} total_hours={total_duration / 3600:.3f}")


if __name__ == "__main__":
    main()
