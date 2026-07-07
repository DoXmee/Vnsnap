from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta
from pathlib import Path


CAPCUT_DRAFT_DIR = Path(os.path.expanduser(
    r"~\AppData\Local\CapCut\User Data\Projects\com.lveditor.draft"
))


def format_time(seconds: float) -> str:
    total_ms = int(seconds * 1000 + 0.5)
    td = timedelta(milliseconds=total_ms)
    h = td.seconds // 3600
    m = (td.seconds % 3600) // 60
    s = td.seconds % 60
    ms = td.microseconds // 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def find_latest_json() -> Path | None:
    base = CAPCUT_DRAFT_DIR
    if not base.exists():
        return None
    projects = [
        p
        for p in base.iterdir()
        if p.is_dir()
    ]
    if not projects:
        return None
    latest = max(projects, key=lambda p: p.stat().st_mtime)
    json_file = latest / "draft_content.json"
    if json_file.exists():
        return json_file
    return None


def extract_subtitles(json_path: Path, use_translation: bool) -> list[str]:
    """
    Extract SRT from CapCut draft_content.json using the exact core rules from
    D:\\tool lay srt\\capcutsrt.py. This file only removes the Tkinter UI and
    adds CLI arguments so Electron can call it.
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    mat_map: dict[str, dict] = {
        m["id"]: m for m in data.get("materials", {}).get("texts", [])
    }

    tracks = data.get("tracks", [])
    text_tracks = [t for t in tracks if t.get("type") == "text"]

    if not text_tracks:
        return []

    def detect_lang(track) -> str:
        for seg in track.get("segments", [])[:20]:
            mat = mat_map.get(seg.get("material_id", ""), {})
            lang = mat.get("language", "")
            if lang:
                return lang
        return ""

    target_lang = "vi-VN" if use_translation else "zh-CN"
    chosen_track = None

    for t in text_tracks:
        if detect_lang(t) == target_lang:
            chosen_track = t
            break

    if chosen_track is None:
        chosen_track = text_tracks[0] if use_translation else text_tracks[-1]

    segments = chosen_track.get("segments", [])
    entries: list[tuple[int, int, str]] = []

    for seg in segments:
        tr = seg.get("target_timerange") or {}
        start_us: int = tr.get("start", 0)
        dur_us: int = tr.get("duration", 0)

        if dur_us <= 0:
            continue

        mat = mat_map.get(seg.get("material_id", ""), {})
        text = mat.get("recognize_text", "").strip()

        if not text:
            continue

        entries.append((start_us, dur_us, text))

    entries.sort(key=lambda x: x[0])

    subtitles: list[str] = []
    for idx, (start_us, dur_us, text) in enumerate(entries, 1):
        t_start = format_time(start_us / 1_000_000)
        t_end = format_time((start_us + dur_us) / 1_000_000)
        subtitles.append(f"{idx}\n{t_start} --> {t_end}\n{text}\n")

    return subtitles


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="Path to CapCut draft_content.json. If omitted, latest project is used.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--language", choices=["zh", "vi"], default="zh")
    args = parser.parse_args()

    json_path = args.input or find_latest_json()
    if not json_path or not json_path.exists():
        raise SystemExit(f"Khong tim thay draft_content.json cua CapCut trong {CAPCUT_DRAFT_DIR}")

    subtitles = extract_subtitles(json_path, use_translation=args.language == "vi")
    if not subtitles:
        raise SystemExit("Khong tim thay phu de. File co the khong chua text track cho ngon ngu nay.")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(subtitles))

    print(json.dumps({
        "ok": True,
        "input": str(json_path),
        "output": str(args.output),
        "language": args.language,
        "cues": len(subtitles),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
