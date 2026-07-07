from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from render_vieneu_srt import clean_text, parse_srt, prepare_srt_render_text, render_pack_text, load_pack


def read_text_lines(path: Path) -> list[str]:
    """Read non-empty text lines from a plain text file."""
    return [clean_text(line) for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if clean_text(line)]


def split_free_text(text: str) -> list[str]:
    """Split pasted text into standalone sentences while preserving readable punctuation."""
    text = clean_text(text)
    if not text:
        return []
    parts = re.split(r"(?<=[.!?。！？])\s+", text)
    return [clean_text(part) for part in parts if clean_text(part)]


def load_lines(args: argparse.Namespace) -> list[str]:
    """Load render lines from SRT, text-file, or direct text."""
    if args.srt:
        return [clean_text(str(item["text"])) for item in parse_srt(args.srt) if clean_text(str(item["text"]))]
    if args.text_file:
        raw = args.text_file.read_text(encoding="utf-8-sig", errors="replace")
        lines = read_text_lines(args.text_file)
        return lines if len(lines) > 1 else split_free_text(raw)
    if args.text:
        return split_free_text(args.text)
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-dir", type=Path, required=True)
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--text")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--max-cues", type=int, default=0)
    args = parser.parse_args()

    started = time.perf_counter()
    pack = load_pack(args.pack_dir.resolve())
    lines = load_lines(args)
    if args.max_cues:
        lines = lines[: args.max_cues]
    if not lines:
        raise SystemExit("Khong co cau text hop le de render.")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index, text in enumerate(lines, start=1):
        text = prepare_srt_render_text(pack, text)
        out_file = args.out_dir / f"cau_{index:04d}.mp3"
        print(f"VieNeu text clips: render {index}/{len(lines)} {text[:90]}")
        render_pack_text(pack, text, out_file, None)
        manifest.append({"index": index, "text": text, "mp3": str(out_file)})
        elapsed = time.perf_counter() - started
        print(f"VieNeu text clips: done {index}/{len(lines)} elapsed={elapsed:.1f}s")

    report = {
        "ok": True,
        "outDir": str(args.out_dir.resolve()),
        "total": len(manifest),
        "elapsedSec": round(time.perf_counter() - started, 3),
        "clips": manifest,
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "outDir": report["outDir"], "total": report["total"], "elapsedSec": report["elapsedSec"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
