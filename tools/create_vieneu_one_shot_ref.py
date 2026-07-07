from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from omni_speaker_clone import extract_speaker_clones


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / "ffmpeg.exe"


def srt_time_to_sec(value: str) -> float:
    hours, minutes, rest = value.strip().split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


def parse_srt(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\n\s*\n", text.strip())
    segments: list[dict] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        left, right = [part.strip() for part in lines[timing_index].split("-->", 1)]
        caption = " ".join(lines[timing_index + 1 :]).strip()
        if not caption:
            continue
        segments.append(
            {
                "speaker_id": "Speaker 1",
                "start": srt_time_to_sec(left),
                "end": srt_time_to_sec(right),
                "text": caption,
            }
        )
    return segments


def convert_to_wav(src: Path, dst: Path, max_duration: float | None = None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(FFMPEG),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-ac",
        "1",
        "-ar",
        "24000",
    ]
    if max_duration and max_duration > 0:
        cmd += ["-t", f"{max_duration:.3f}"]
    cmd.append(str(dst))
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a VieNeu one-shot reference using Omni speaker clone selection.")
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--ref-text", default="")
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    if not args.audio.exists():
        raise FileNotFoundError(args.audio)
    if args.srt and not args.srt.exists():
        raise FileNotFoundError(args.srt)
    if not FFMPEG.exists():
        raise FileNotFoundError(FFMPEG)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    final_audio = args.out_dir / "ref_audio.wav"
    final_text = args.out_dir / "ref_text.txt"

    if args.srt:
        segments = parse_srt(args.srt)
        if not segments:
            raise RuntimeError(f"SRT khong co timing hop le: {args.srt}")
        with tempfile.TemporaryDirectory(prefix="vieneu_one_shot_") as tmp:
            source_wav = Path(tmp) / "source.wav"
            convert_to_wav(args.audio, source_wav)
            clones = extract_speaker_clones(str(source_wav), segments, str(args.out_dir))
        first = next(iter(clones.values()), None)
        if not first:
            raise RuntimeError("Khong tim duoc doan reference 5-15s sach theo co che Omni.")
        Path(first["ref_audio"]).replace(final_audio)
        ref_text = str(first.get("ref_text") or "").strip()
        final_text.write_text(ref_text, encoding="utf-8")
        result = {
            "refAudio": str(final_audio),
            "refText": ref_text,
            "duration": first.get("duration"),
            "sourceCount": first.get("source_count"),
            "method": "omni_speaker_clone_srt",
        }
    else:
        ref_text = args.ref_text.strip()
        if not ref_text:
            raise RuntimeError("Can ref-text neu tao one-shot pack khong co SRT.")
        convert_to_wav(args.audio, final_audio, max_duration=15.0)
        final_text.write_text(ref_text, encoding="utf-8")
        result = {
            "refAudio": str(final_audio),
            "refText": ref_text,
            "duration": None,
            "sourceCount": 1,
            "method": "direct_ref_audio",
        }

    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
