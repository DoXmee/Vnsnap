from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from render_vieneu_srt import analyze_asr_match, clean_text, load_asr_model, transcribe_audio


ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "local_vieneu" / "venv" / "Scripts" / "python.exe"
RENDERER = ROOT / "tools" / "render_vieneu_srt.py"
FFMPEG = ROOT / "ffmpeg.exe"


def load_pipe_metadata(dataset_dir: Path) -> list[tuple[Path, str]]:
    rows: list[tuple[Path, str]] = []
    meta = dataset_dir / "metadata.csv"
    for raw in meta.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if "|" not in raw:
            continue
        audio_name, text = raw.split("|", 1)
        audio_path = dataset_dir / "raw_audio" / audio_name
        if audio_path.exists() and clean_text(text):
            rows.append((audio_path, clean_text(text)))
    return rows


def make_temp_pack(base_pack: dict, ref_audio: Path, ref_text: str, root: Path) -> Path:
    pack = dict(base_pack)
    pack["refAudio"] = str(ref_audio)
    pack["refText"] = ref_text
    pack["maxGroupDuration"] = 0
    pack["maxGroupGap"] = 0
    pack["batchSize"] = 1
    pack["clipRetries"] = 2
    pack["temperature"] = float(pack.get("temperature", 0.38))
    pack["topK"] = int(pack.get("topK", 16))
    pack["maxChars"] = min(int(pack.get("maxChars", 85)), 85)
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.json").write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def render_text(pack_dir: Path, text: str, out_wav: Path) -> None:
    subprocess.run(
        [
            str(PY),
            str(RENDERER),
            "--pack-dir",
            str(pack_dir),
            "--text",
            text,
            "--out",
            str(out_wav),
        ],
        cwd=str(ROOT),
        check=True,
    )


def wav_to_mp3(wav_path: Path, mp3_path: Path) -> None:
    subprocess.run(
        [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav_path), "-c:a", "libmp3lame", "-b:a", "128k", str(mp3_path)],
        check=True,
    )


def candidate_score(ref_text: str) -> int:
    text = ref_text.lower()
    penalty = 0
    for bad in ["bản thân", "mình", "hôm nay", "đầu tiên"]:
        if bad in text:
            penalty += 4
    return penalty + abs(len(ref_text) - 75) // 20


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-pack", type=Path, required=True)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--text", default="Hôm nay mình sẽ cải tạo lại căn phòng nhỏ này.")
    parser.add_argument("--asr-model", default="small")
    args = parser.parse_args()

    base_pack = json.loads((args.base_pack / "pack.json").read_text(encoding="utf-8-sig"))
    rows = load_pipe_metadata(args.dataset_dir)
    candidates = sorted(rows, key=lambda item: candidate_score(item[1]))[: args.limit]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    asr_model = load_asr_model(args.asr_model)
    results = []

    with tempfile.TemporaryDirectory(prefix="vieneu_ref_score_") as tmp:
        tmp_dir = Path(tmp)
        for index, (ref_audio, ref_text) in enumerate(candidates, start=1):
            pack_dir = make_temp_pack(base_pack, ref_audio, ref_text, tmp_dir / f"pack_{index:02d}")
            wav_path = args.out_dir / f"candidate_{index:02d}.wav"
            mp3_path = args.out_dir / f"candidate_{index:02d}.mp3"
            try:
                render_text(pack_dir, args.text, wav_path)
                wav_to_mp3(wav_path, mp3_path)
                asr_text = transcribe_audio(asr_model, mp3_path)
                match = analyze_asr_match(args.text, asr_text)
                score = match["coverage"] - len(match["extraPrefix"]) * 0.25
                ok = bool(match["ok"])
            except Exception as exc:
                asr_text = ""
                match = {"ok": False, "coverage": 0, "extraPrefix": [], "error": str(exc)}
                score = -1
                ok = False
            result = {
                "index": index,
                "ok": ok,
                "score": score,
                "refAudio": str(ref_audio),
                "refText": ref_text,
                "demo": str(mp3_path),
                "asrText": asr_text,
                "asr": match,
            }
            results.append(result)
            print(json.dumps(result, ensure_ascii=False))

    best = sorted(results, key=lambda item: (item["ok"], item["score"]), reverse=True)[0] if results else None
    (args.out_dir / "ref_score_report.json").write_text(
        json.dumps({"best": best, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not best or not best["ok"]:
        raise SystemExit("No reference candidate passed ASR validation")


if __name__ == "__main__":
    main()
