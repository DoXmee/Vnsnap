#!/usr/bin/env python3
"""Join pre-rendered audio clips onto an SRT timeline.

This tool is intentionally simple: it does not synthesize, trim, denoise, or
time-stretch clips. Each clip is placed at the matching SRT cue start time.
If a clip is longer than its cue, following clips may overlap it.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


def natural_key(path: Path) -> list[object]:
    parts = re.split(r"(\d+)", path.name.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def parse_time(value: str) -> int:
    value = value.strip().replace(".", ",")
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(millis[:3].ljust(3, "0"))
    )


def parse_srt(path: Path) -> list[dict[str, int | str]]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\r?\n\r?\n+", raw.strip())
    entries: list[dict[str, int | str]] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing = next((line for line in lines if "-->" in line), "")
        if not timing:
            continue
        left, right = [part.strip().split()[0] for part in timing.split("-->", 1)]
        text_lines = lines[lines.index(timing) + 1 :]
        entries.append(
            {
                "start_ms": parse_time(left),
                "end_ms": parse_time(right),
                "text": " ".join(text_lines),
            }
        )
    return entries


def find_audio_files(path: Path) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() == ".txt":
            return [
                Path(line.strip().strip('"'))
                for line in path.read_text(encoding="utf-8-sig").splitlines()
                if line.strip()
            ]
        return [path]
    files = sorted(
        [item for item in path.iterdir() if item.is_file() and item.suffix.lower() in AUDIO_EXTS],
        key=natural_key,
    )
    numbered = [item for item in files if re.match(r"^cau[_-]?\d+", item.stem, flags=re.IGNORECASE)]
    if numbered:
        ignored = len(files) - len(numbered)
        if ignored:
            print(f"Bo qua {ignored} file khong phai clip cau_#### trong folder voice.", flush=True)
        return numbered
    return files


def ffmpeg_binary(explicit: str | None) -> str:
    if explicit:
        return explicit
    local = Path.cwd() / "ffmpeg.exe"
    if local.exists():
        return str(local)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError("Khong tim thay ffmpeg.exe")


def run(cmd: list[str], label: str) -> None:
    print(label, flush=True)
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-12:])
        raise RuntimeError(f"{label} failed\n{tail}")


def join_audio_by_srt(
    *,
    srt_path: Path,
    clips: list[Path],
    out_path: Path,
    ffmpeg: str,
    batch_size: int = 80,
    tail_pad_ms: int = 750,
    limiter: bool = True,
    limit: int = 0,
) -> None:
    entries = parse_srt(srt_path)
    if not entries:
        raise RuntimeError(f"SRT khong co timing hop le: {srt_path}")
    if limit > 0:
        entries = entries[:limit]
        clips = clips[:limit]
        print(f"Test limit: dung {limit} dong dau.", flush=True)
    if len(clips) < len(entries):
        raise RuntimeError(f"Thieu clip: SRT co {len(entries)} dong nhung chi co {len(clips)} file audio")
    if len(clips) > len(entries):
        print(f"Co {len(clips)} clip, chi dung {len(entries)} clip dau theo SRT.", flush=True)
        clips = clips[: len(entries)]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    mix_tail = ",alimiter=limit=0.95" if limiter else ""

    with tempfile.TemporaryDirectory(prefix="join_srt_audio_") as tmp_name:
        tmp = Path(tmp_name)
        batch_outputs: list[tuple[Path, int]] = []
        total_batches = (len(entries) + batch_size - 1) // batch_size

        for batch_index, start in enumerate(range(0, len(entries), batch_size), start=1):
            chunk = entries[start : start + batch_size]
            chunk_clips = clips[start : start + len(chunk)]
            batch_start = int(chunk[0]["start_ms"])
            batch_end = int(chunk[-1]["end_ms"]) + tail_pad_ms
            batch_out = tmp / f"batch_{batch_index:05d}.wav"
            filter_path = tmp / f"batch_{batch_index:05d}.ffscript"

            cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
            filters: list[str] = []
            labels: list[str] = []
            for idx, (entry, clip) in enumerate(zip(chunk, chunk_clips)):
                cmd += ["-i", str(clip)]
                delay = max(0, int(entry["start_ms"]) - batch_start)
                label = f"a{idx}"
                filters.append(f"[{idx}:a]aresample=48000,asetpts=PTS-STARTPTS,adelay={delay}|{delay}[{label}]")
                labels.append(f"[{label}]")

            filters.append(
                f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
                f"dropout_transition=0:normalize=0{mix_tail}[out]"
            )
            filter_path.write_text(";".join(filters), encoding="utf-8")
            cmd += [
                "-filter_complex_script",
                str(filter_path),
                "-map",
                "[out]",
                "-t",
                f"{(batch_end - batch_start) / 1000:.3f}",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(batch_out),
            ]
            run(cmd, f"Batch {batch_index}/{total_batches}")
            batch_outputs.append((batch_out, batch_start))

        final_filter = tmp / "final.ffscript"
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        filters = []
        labels = []
        for idx, (batch_path, batch_start) in enumerate(batch_outputs):
            cmd += ["-i", str(batch_path)]
            label = f"b{idx}"
            filters.append(f"[{idx}:a]adelay={batch_start}|{batch_start}[{label}]")
            labels.append(f"[{label}]")

        filters.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
            f"dropout_transition=0:normalize=0{mix_tail}[out]"
        )
        final_filter.write_text(";".join(filters), encoding="utf-8")
        final_end = int(entries[-1]["end_ms"]) + tail_pad_ms
        cmd += [
            "-filter_complex_script",
            str(final_filter),
            "-map",
            "[out]",
            "-t",
            f"{final_end / 1000:.3f}",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(out_path),
        ]
        run(cmd, "Encode output")

    if not out_path.exists() or out_path.stat().st_size < 1000:
        raise RuntimeError(f"Output khong hop le: {out_path}")
    print(f"OK: {out_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Join existing audio clips by SRT timing.")
    parser.add_argument("--srt", required=True, type=Path)
    parser.add_argument("--clips", required=True, type=Path, help="Folder audio, one audio file, or txt file list.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--ffmpeg", default=None)
    parser.add_argument("--batch-size", type=int, default=80)
    parser.add_argument("--tail-pad-ms", type=int, default=750)
    parser.add_argument("--limit", type=int, default=0, help="Only join the first N cues for a quick test.")
    parser.add_argument("--no-limiter", action="store_true")
    args = parser.parse_args()

    clips = find_audio_files(args.clips)
    join_audio_by_srt(
        srt_path=args.srt,
        clips=clips,
        out_path=args.out,
        ffmpeg=ffmpeg_binary(args.ffmpeg),
        batch_size=max(1, args.batch_size),
        tail_pad_ms=max(0, args.tail_pad_ms),
        limiter=not args.no_limiter,
        limit=max(0, args.limit),
    )


if __name__ == "__main__":
    main()
