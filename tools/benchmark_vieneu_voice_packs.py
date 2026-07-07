from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

from faster_whisper import WhisperModel


ROOT = Path(__file__).resolve().parents[1]
LOCAL_VIENEU = ROOT / "Release_App" / "TikTokVoiceStudio-win32-x64" / "resources" / "app" / "local_vieneu.js"


def transcribe_folder(folder: Path, language: str = "vi") -> list[dict]:
    """Transcribe all MP3 clips in a folder with faster-whisper small."""
    model = WhisperModel("small", device="cuda", compute_type="float16")
    rows = []
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8")) if (folder / "manifest.json").exists() else {}
    clips_meta = {Path(item.get("mp3", "")).name: item for item in manifest.get("clips", [])}
    for path in sorted(folder.glob("*.mp3")):
        segments, _info = model.transcribe(str(path), language=language, vad_filter=False, condition_on_previous_text=False)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        expected = str(clips_meta.get(path.name, {}).get("text", ""))
        score = SequenceMatcher(None, expected.lower(), text.lower()).ratio() if expected else 0.0
        rows.append({"file": path.name, "expected": expected, "asr": text, "score": round(score, 3), "size": path.stat().st_size})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--packs", nargs="+", required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    args.out_root.mkdir(parents=True, exist_ok=True)
    summary = []
    for pack_id in args.packs:
        out_dir = args.out_root / pack_id
        if out_dir.exists():
            shutil.rmtree(out_dir)
        payload = {"packId": pack_id, "srt": str(args.srt.resolve()), "outDir": str(out_dir.resolve())}
        started = time.perf_counter()
        print(f"BENCH render pack={pack_id}")
        proc = subprocess.run(
            ["node", str(LOCAL_VIENEU), "render-srt-clips", json.dumps(payload, ensure_ascii=True)],
            cwd=str(ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=args.timeout,
        )
        elapsed = round(time.perf_counter() - started, 3)
        pack_result = {"pack": pack_id, "ok": proc.returncode == 0, "elapsedSec": elapsed, "outDir": str(out_dir)}
        if proc.returncode != 0:
            pack_result["error"] = (proc.stdout + "\n" + proc.stderr)[-4000:]
            summary.append(pack_result)
            print(json.dumps(pack_result, ensure_ascii=False, indent=2))
            continue
        rows = transcribe_folder(out_dir)
        avg = sum(row["score"] for row in rows) / max(1, len(rows))
        fails = [row for row in rows if row["score"] < 0.82]
        pack_result.update({"avgScore": round(avg, 3), "fails": len(fails), "clips": rows})
        (out_dir / "asr_report.json").write_text(json.dumps(pack_result, ensure_ascii=False, indent=2), encoding="utf-8")
        summary.append(pack_result)
        print(json.dumps({"pack": pack_id, "avgScore": pack_result["avgScore"], "fails": len(fails), "elapsedSec": elapsed}, ensure_ascii=False))
    (args.out_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": str(args.out_root / "summary.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
