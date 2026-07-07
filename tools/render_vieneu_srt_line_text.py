import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / "tools" / "ffmpeg.exe"
if not FFMPEG.exists():
    FFMPEG = ROOT / "Release_App" / "TikTokVoiceStudio-win32-x64" / "resources" / "app" / "ffmpeg.exe"
PY = ROOT / "local_vieneu" / "venv" / "Scripts" / "python.exe"
RENDERER = ROOT / "tools" / "render_vieneu_srt.py"


def parse_time(value: str) -> float:
    m = re.match(r"(\d+):(\d+):(\d+),(\d+)", value.strip())
    if not m:
        return 0.0
    h, mi, s, ms = [int(x) for x in m.groups()]
    return h * 3600 + mi * 60 + s + ms / 1000.0


def parse_srt(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_text, end_text = [x.strip() for x in lines[1].split("-->", 1)]
        raw_text = " ".join(lines[2:]).strip()
        if raw_text:
            cues.append({
                "index": len(cues) + 1,
                "time": lines[1],
                "start": parse_time(start_text),
                "end": parse_time(end_text),
                "text": raw_text,
            })
    return cues


def cache_key(pack_dir: Path, text: str) -> str:
    pack_file = pack_dir / "pack.json"
    pack = json.loads(pack_file.read_text(encoding="utf-8-sig"))
    data = {
        "text": text,
        "voice": pack.get("id") or pack_dir.name,
        "lora": pack.get("lora_hash") or pack.get("loraDir"),
        "ref": pack.get("ref_codes_hash") or pack.get("refText"),
        "speed": pack.get("textSpeechSpeed", pack.get("speechSpeed", 1.0)),
        "version": pack.get("srtCacheVersion", "line_text_v1"),
    }
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def render_text_clip(pack_dir: Path, text: str, out_file: Path, timeout_sec: int) -> bool:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vieneu_line_text_") as tmp:
        text_file = Path(tmp) / "text.txt"
        text_file.write_text(text, encoding="utf-8")
        cmd = [
            str(PY),
            str(RENDERER),
            "--pack-dir",
            str(pack_dir),
            "--text-file",
            str(text_file),
            "--out",
            str(out_file),
        ]
        try:
            subprocess.run(
                cmd,
                cwd=str(ROOT),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
                check=True,
            )
            return out_file.exists() and out_file.stat().st_size > 1024
        except subprocess.TimeoutExpired:
            return False
        except subprocess.CalledProcessError:
            return False


def make_silence(path: Path, duration: float) -> None:
    duration = max(0.05, duration)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t",
            f"{duration:.3f}",
            "-q:a",
            "4",
            str(path),
        ],
        check=True,
    )


def ffprobe_duration(path: Path) -> float:
    ffprobe = FFMPEG.with_name("ffprobe.exe")
    if not ffprobe.exists():
        return 0.0
    r = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def assemble_timeline(clips: list[dict], out_file: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="vieneu_line_mix_") as tmp:
        tmp_dir = Path(tmp)
        inputs = []
        filters = []
        labels = []
        for i, item in enumerate(clips):
            inputs.extend(["-i", str(item["path"])])
            delay_ms = int(max(0.0, item["start"]) * 1000)
            labels.append(f"a{i}")
            filters.append(f"[{i}:a]adelay={delay_ms}|{delay_ms}[a{i}]")
        if not labels:
            silence = tmp_dir / "empty.mp3"
            make_silence(silence, 0.5)
            shutil.copy2(silence, out_file)
            return
        filter_complex = ";".join(filters) + ";" + "".join(f"[{label}]" for label in labels)
        filter_complex += f"amix=inputs={len(labels)}:normalize=0,loudnorm=I=-18:TP=-2:LRA=9[aout]"
        subprocess.run(
            [
                str(FFMPEG),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                *inputs,
                "-filter_complex",
                filter_complex,
                "-map",
                "[aout]",
                "-b:a",
                "192k",
                str(out_file),
            ],
            check=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-dir", type=Path, required=True)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout-sec", type=int, default=180)
    parser.add_argument("--max-cues", type=int, default=0)
    args = parser.parse_args()

    started = time.perf_counter()
    cues = parse_srt(args.srt)
    if args.max_cues:
        cues = cues[: args.max_cues]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    cache_root = args.pack_dir / "_FAST_TTS_CACHE_DO_NOT_DELETE" / "line_text_cache"
    clip_items = []
    report = []
    for cue in cues:
        key = cache_key(args.pack_dir, cue["text"])
        clip = cache_root / key[:2] / f"{key}.mp3"
        ok = clip.exists() and clip.stat().st_size > 1024
        status = "cache" if ok else "render"
        if not ok:
            ok = render_text_clip(args.pack_dir, cue["text"], clip, args.timeout_sec)
            status = "rendered" if ok else "timeout_or_failed"
        if not ok:
            clip = cache_root / "fallback" / f"silence_{cue['index']:05d}.mp3"
            make_silence(clip, max(0.2, cue["end"] - cue["start"]))
        clip_items.append({"path": clip, "start": cue["start"], "index": cue["index"]})
        report.append({
            "index": cue["index"],
            "status": status,
            "text": cue["text"],
            "clip": str(clip),
            "duration": round(ffprobe_duration(clip), 3),
        })
        elapsed = time.perf_counter() - started
        print(f"Line text render: {cue['index']}/{len(cues)} {status} elapsed={elapsed:.1f}s")
    assemble_timeline(clip_items, args.out)
    report_path = args.out.with_suffix(".line_text_report.json")
    report_path.write_text(
        json.dumps({"out": str(args.out), "elapsed": round(time.perf_counter() - started, 2), "cues": report}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Line text done: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
