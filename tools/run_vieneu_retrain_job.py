from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "local_vieneu" / "venv" / "Scripts" / "python.exe"


def append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(text)
        if text and not text.endswith("\n"):
            fh.write("\n")


def adapter_ok(path: Path) -> bool:
    return (
        path.exists()
        and (path / "adapter_config.json").exists()
        and ((path / "adapter_model.safetensors").exists() or (path / "adapter_model.bin").exists())
    )


def copy_final_snapshot(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    ignore = shutil.ignore_patterns("optimizer.pt", "scheduler.pt", "trainer_state.json", "rng_state.pth")
    shutil.copytree(src, dst, ignore=ignore)
    if not adapter_ok(dst):
        raise RuntimeError(f"Final snapshot invalid after copy: {dst}")


def update_pack(pack_json: Path, lora_dir: Path, note: str) -> None:
    data = json.loads(pack_json.read_text(encoding="utf-8-sig"))
    data["loraDir"] = str(lora_dir.resolve())
    data["fallbackLoraDirs"] = [str(lora_dir.resolve())]
    data["loraStatusNote"] = note
    data["status"] = "ready"
    data["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    pack_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume VieNeu LoRA training and atomically publish a validated final adapter.")
    parser.add_argument("--dataset-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--resume-from-checkpoint", required=True, type=Path)
    parser.add_argument("--init-lora-only", action="store_true")
    parser.add_argument("--final-dir", required=True, type=Path)
    parser.add_argument("--pack-json", required=True, type=Path)
    parser.add_argument("--log-file", required=True, type=Path)
    parser.add_argument("--max-steps", type=int, default=10000)
    parser.add_argument("--save-steps", type=int, default=500)
    parser.add_argument("--save-total-limit", type=int, default=20)
    parser.add_argument("--lr", type=float, default=0.00008)
    parser.add_argument("--ref-codes", type=Path, default=None)
    parser.add_argument("--ref-text-file", type=Path, default=None)
    args = parser.parse_args()

    if not PY.exists():
        raise FileNotFoundError(PY)
    if not adapter_ok(args.resume_from_checkpoint):
        raise RuntimeError(f"Resume checkpoint invalid: {args.resume_from_checkpoint}")

    cmd = [
        str(PY),
        str(ROOT / "tools" / "train_vieneu_lora.py"),
        "--dataset-dir",
        str(args.dataset_dir.resolve()),
        "--output-dir",
        str(args.output_dir.resolve()),
        "--max-steps",
        str(args.max_steps),
        "--save-steps",
        str(args.save_steps),
        "--save-total-limit",
        str(args.save_total_limit),
        "--lr",
        str(args.lr),
        "--max-len",
        "1024",
        "--batch-size",
        "1",
        "--grad-accum",
        "4",
    ]
    if args.init_lora_only:
        cmd += ["--init-lora", str(args.resume_from_checkpoint.resolve())]
    else:
        cmd += ["--resume-from-checkpoint", str(args.resume_from_checkpoint.resolve())]
    if args.ref_codes:
        cmd += ["--ref-codes", str(args.ref_codes.resolve())]
    if args.ref_text_file:
        cmd += ["--ref-text-file", str(args.ref_text_file.resolve())]

    append_log(args.log_file, f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] train start")
    append_log(args.log_file, " ".join(f'"{part}"' if " " in part else part for part in cmd))
    with args.log_file.open("a", encoding="utf-8", errors="replace") as log:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**dict(**__import__("os").environ), "PYTHONIOENCODING": "utf-8"},
        )
    append_log(args.log_file, f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] train exit={proc.returncode}")
    if proc.returncode != 0:
        return proc.returncode

    if not adapter_ok(args.output_dir):
        raise RuntimeError(f"Training finished but output adapter invalid: {args.output_dir}")
    copy_final_snapshot(args.output_dir, args.final_dir)
    update_pack(
        args.pack_json,
        args.final_dir,
        f"Retrained and validated from {args.resume_from_checkpoint.name} to step {args.max_steps}.",
    )
    append_log(args.log_file, f"published_final={args.final_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
