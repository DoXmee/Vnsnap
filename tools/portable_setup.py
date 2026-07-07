from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "resources_manifest.json"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def progress(percent: float, message: str) -> None:
    print(f"PROGRESS:{max(0, min(100, percent)):.1f}:{message}", flush=True)


def log(message: str) -> None:
    print(message, flush=True)


def load_manifest() -> dict:
    if not MANIFEST.exists():
        raise FileNotFoundError(MANIFEST)
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def setup_cache_env(manifest: dict) -> dict:
    cache_dir = ROOT / manifest.get("cache_dir", "model_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["HF_HOME"] = str(cache_dir / "huggingface")
    env["HUGGINGFACE_HUB_CACHE"] = str(cache_dir / "huggingface" / "hub")
    env["TRANSFORMERS_CACHE"] = str(cache_dir / "huggingface" / "transformers")
    env["TORCH_HOME"] = str(cache_dir / "torch")
    env["XDG_CACHE_HOME"] = str(cache_dir)
    env["MODELSCOPE_CACHE"] = str(cache_dir / "modelscope")
    env["PADDLE_HOME"] = str(cache_dir / "paddle")
    env["PADDLEOCR_HOME"] = str(cache_dir / "paddleocr")
    env["PADDLE_PDX_CACHE_HOME"] = str(cache_dir / "paddlex")
    env["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    for key in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE", "TORCH_HOME", "MODELSCOPE_CACHE", "PADDLE_HOME", "PADDLEOCR_HOME"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    Path(env["PADDLE_PDX_CACHE_HOME"]).mkdir(parents=True, exist_ok=True)
    return env


def run(cmd: list[str], env: dict, label: str, timeout: int | None = None) -> int:
    log(f"RUN:{label}:{' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    started = time.time()
    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                lines.put(raw)
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    while True:
        if timeout and time.time() - started > timeout:
            log(f"WARNING:{label}: timeout after {timeout}s, killing process tree")
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            return 124
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            if proc.poll() is not None:
                return proc.returncode or 0
            continue
        if line is None:
            return proc.wait()
        line = line.rstrip()
        if line:
            if line.startswith(("PROGRESS:", "WARNING:", "HF_DOWNLOAD_", "FUNASR_DOWNLOAD_", "PADDLEOCR_")):
                log(line[-500:])
            else:
                log(f"[{label}] {line[-500:]}")


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".download")
    last_pct = -1

    def hook(count: int, block_size: int, total_size: int) -> None:
        nonlocal last_pct
        if total_size <= 0:
            return
        pct = int(min(100, count * block_size * 100 / total_size))
        if pct >= last_pct + 5:
            last_pct = pct
            progress(8 + pct * 0.12, f"Downloading FFmpeg package {pct}%")

    urllib.request.urlretrieve(url, tmp, hook)
    tmp.replace(dest)


def ensure_ffmpeg(manifest: dict) -> None:
    ff = manifest.get("ffmpeg", {})
    required = [ROOT / x for x in ff.get("required_files", [])]
    if all(p.exists() for p in required):
        progress(8, "FFmpeg/FFprobe OK")
        return
    url = ff.get("zip_url")
    if not url:
        raise RuntimeError("ffmpeg.zip_url missing in resources_manifest.json")
    work = ROOT / "_portable_setup_tmp"
    zip_path = work / "ffmpeg.zip"
    extract_dir = work / "ffmpeg_extract"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    download(url, zip_path)
    progress(22, "Extracting FFmpeg package")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)
    for name in ff.get("required_files", []):
        found = next(extract_dir.rglob(Path(name).name), None)
        if not found:
            raise RuntimeError(f"{name} not found in FFmpeg package")
        shutil.copy2(found, ROOT / name)
        log(f"Installed {name}")
    shutil.rmtree(work, ignore_errors=True)
    progress(25, "FFmpeg/FFprobe installed")


def install_requirements(manifest: dict, env: dict, skip_pip: bool) -> None:
    py = ROOT / manifest.get("python", {}).get("venv_python", "local_vieneu/venv/Scripts/python.exe")
    if not py.exists():
        raise FileNotFoundError(f"Local Python missing: {py}")
    progress(30, "Python portable OK")
    if skip_pip:
        return
    reqs = [ROOT / r for r in manifest.get("python", {}).get("requirements", []) if (ROOT / r).exists()]
    for idx, req in enumerate(reqs, 1):
        progress(30 + idx * 3, f"Installing Python requirements: {req.name}")
        code = run([str(py), "-m", "pip", "install", "-r", str(req)], env, f"pip:{req.name}")
        if code != 0:
            raise RuntimeError(f"pip install failed for {req}: exit {code}")

def ensure_playwright_browser(manifest: dict, env: dict) -> None:
    py = ROOT / manifest.get("python", {}).get("venv_python", "local_vieneu/venv/Scripts/python.exe")
    portable_data = Path(env.get("VF_PORTABLE_DATA_DIR") or (ROOT.parent.parent / "portable_data"))
    browser_dir = Path(env.get("PLAYWRIGHT_BROWSERS_PATH") or (portable_data / "playwright-browsers"))
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_dir)
    existing = next(browser_dir.rglob("chrome.exe"), None) if browser_dir.exists() else None
    if existing:
        progress(46, "Portable Chromium OK")
        return
    progress(42, "Installing portable Chromium for Gemini Web")
    code = run([str(py), "-m", "playwright", "install", "chromium"], env, "playwright:chromium", timeout=1800)
    if code != 0:
        log(f"WARNING: Portable Chromium install failed: exit {code}")
    else:
        progress(46, "Portable Chromium installed")


def download_hf_models(manifest: dict, env: dict, skip_models: bool) -> None:
    if skip_models:
        return
    py = ROOT / manifest.get("python", {}).get("venv_python", "local_vieneu/venv/Scripts/python.exe")
    script = ROOT / "_portable_setup_hf_download.py"
    models = manifest.get("huggingface_models", [])
    script.write_text(
        "from huggingface_hub import snapshot_download\n"
        "import json, os\n"
        "from pathlib import Path\n"
        f"models = {json.dumps(models, ensure_ascii=False)!r}\n"
        "models = json.loads(models)\n"
        "total = max(1, len(models))\n"
        "for idx, m in enumerate(models, 1):\n"
        "    pct = 48 + ((idx - 1) / total) * 20\n"
        "    local_dir = Path(m['local_dir'])\n"
        "    model_bin = local_dir / 'model.bin'\n"
        "    if model_bin.exists() and model_bin.stat().st_size > 1024 * 1024:\n"
        "        print(f'PROGRESS:{pct + 8:.1f}:HuggingFace OK: ' + m['name'], flush=True)\n"
        "        print('HF_DOWNLOAD_SKIP:' + m['name'], flush=True)\n"
        "        continue\n"
        "    print(f'PROGRESS:{pct:.1f}:Downloading HuggingFace model: ' + m['name'], flush=True)\n"
        "    print('HF_DOWNLOAD_START:' + m['name'], flush=True)\n"
        "    try:\n"
        "        snapshot_download(repo_id=m['repo_id'], local_dir=m['local_dir'], local_dir_use_symlinks=False, resume_download=True)\n"
        "        print(f'PROGRESS:{pct + 8:.1f}:HuggingFace done: ' + m['name'], flush=True)\n"
        "        print('HF_DOWNLOAD_DONE:' + m['name'], flush=True)\n"
        "    except Exception as exc:\n"
        "        print('HF_DOWNLOAD_FAILED:' + m['name'] + ':' + str(exc), flush=True)\n",
        encoding="utf-8",
    )
    try:
        progress(48, "Downloading HuggingFace ASR models")
        code = run([str(py), str(script)], env, "huggingface")
        if code != 0:
            log(f"WARNING: HuggingFace model download step failed: exit {code}")
    finally:
        try:
            script.unlink()
        except OSError:
            pass
    progress(70, "HuggingFace ASR models OK")


def download_funasr_models(manifest: dict, env: dict, skip_models: bool) -> None:
    if skip_models:
        return
    py = ROOT / manifest.get("python", {}).get("venv_python", "local_vieneu/venv/Scripts/python.exe")
    models = manifest.get("funasr_models", [])
    if not models:
        return
    script = ROOT / "_portable_setup_funasr_download.py"
    script.write_text(
        "from funasr import AutoModel\n"
        "import json, shutil\n"
        "from pathlib import Path\n"
        f"models = {json.dumps(models, ensure_ascii=False)!r}\n"
        "models = json.loads(models)\n"
        "total = max(1, len(models))\n"
        "for idx, m in enumerate(models, 1):\n"
        "    pct = 74 + ((idx - 1) / total) * 8\n"
        "    print(f'PROGRESS:{pct:.1f}:Downloading FunASR/SenseVoice: ' + m['name'], flush=True)\n"
        "    print('FUNASR_DOWNLOAD_START:' + m['name'], flush=True)\n"
        "    model = AutoModel(model=m['model_id'], vad_model='fsmn-vad', device='cpu', disable_update=True, trust_remote_code=True)\n"
        "    target = Path(m['local_dir'])\n"
        "    target.parent.mkdir(parents=True, exist_ok=True)\n"
        "    print(f'PROGRESS:{pct + 6:.1f}:FunASR/SenseVoice done: ' + m['name'], flush=True)\n"
        "    print('FUNASR_DOWNLOAD_DONE:' + m['name'], flush=True)\n",
        encoding="utf-8",
    )
    try:
        progress(74, "Downloading FunASR/SenseVoice models")
        code = run([str(py), str(script)], env, "funasr", timeout=1800)
        if code != 0:
            log(f"WARNING: FunASR model predownload failed: exit {code}")
    finally:
        try:
            script.unlink()
        except OSError:
            pass
    progress(84, "FunASR predownload step done")


def finalize_modelscope_sensevoice() -> bool:
    """Promote partially downloaded ModelScope SenseVoice files into the usable app cache."""
    final_dir = ROOT / "model_cache" / "modelscope" / "models" / "iic" / "SenseVoiceSmall"
    temp_dir = ROOT / "model_cache" / "modelscope" / "models" / "._____temp" / "iic" / "SenseVoiceSmall"
    final_dir.mkdir(parents=True, exist_ok=True)
    promoted = False
    if temp_dir.exists():
        for item in temp_dir.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(temp_dir)
            target = final_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or item.stat().st_size > target.stat().st_size:
                shutil.copy2(item, target)
                promoted = True
    ok = (final_dir / "model.pt").exists() or (final_dir / "model.bin").exists()
    if ok and promoted:
        log("Promoted SenseVoiceSmall model files from ModelScope temp cache")
    return ok


def warm_paddleocr(manifest: dict, env: dict, skip_models: bool) -> None:
    if skip_models or not manifest.get("paddleocr", {}).get("enabled", False):
        return
    py = ROOT / manifest.get("python", {}).get("venv_python", "local_vieneu/venv/Scripts/python.exe")
    script = ROOT / "_portable_setup_paddleocr.py"
    lang = manifest.get("paddleocr", {}).get("lang", "ch")
    script.write_text(
        "from paddleocr import PaddleOCR\n"
        f"lang = {lang!r}\n"
        "try:\n"
        "    ocr = PaddleOCR(lang=lang, use_textline_orientation=True)\n"
        "except (TypeError, ValueError):\n"
        "    ocr = PaddleOCR(lang=lang, use_angle_cls=True)\n"
        "print('PADDLEOCR_READY', flush=True)\n",
        encoding="utf-8",
    )
    try:
        progress(86, "Downloading/warming PaddleOCR models")
        code = run([str(py), str(script)], env, "paddleocr")
        if code != 0:
            log(f"WARNING: PaddleOCR warmup failed: exit {code}")
    finally:
        try:
            script.unlink()
        except OSError:
            pass
    progress(94, "PaddleOCR setup step done")


def write_report(manifest: dict, env: dict) -> None:
    sensevoice_ok = finalize_modelscope_sensevoice()
    hf_turbo = ROOT / "model_cache" / "huggingface" / "mobiuslabsgmbh" / "faster-whisper-large-v3-turbo" / "model.bin"
    hf_small = ROOT / "model_cache" / "huggingface" / "Systran" / "faster-whisper-small" / "model.bin"
    report = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "complete" if sensevoice_ok and hf_turbo.exists() and hf_small.exists() else "partial_usable",
        "root": str(ROOT),
        "cache": {k: env[k] for k in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE", "TORCH_HOME", "MODELSCOPE_CACHE", "PADDLE_HOME", "PADDLEOCR_HOME", "PADDLE_PDX_CACHE_HOME") if k in env},
        "files": {
            "ffmpeg": (ROOT / "ffmpeg.exe").exists(),
            "ffprobe": (ROOT / "ffprobe.exe").exists(),
            "python": (ROOT / manifest.get("python", {}).get("venv_python", "local_vieneu/venv/Scripts/python.exe")).exists(),
            "faster_whisper_large_v3_turbo": hf_turbo.exists(),
            "faster_whisper_small": hf_small.exists(),
            "sensevoice_small": sensevoice_ok,
        },
    }
    (ROOT / "portable_setup_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare portable resources for VnSnap Studio.")
    parser.add_argument("--skip-pip", action="store_true")
    parser.add_argument("--skip-models", action="store_true")
    args = parser.parse_args()
    manifest = load_manifest()
    env = setup_cache_env(manifest)
    progress(1, "Portable setup started")
    ensure_ffmpeg(manifest)
    install_requirements(manifest, env, args.skip_pip)
    ensure_playwright_browser(manifest, env)
    download_hf_models(manifest, env, args.skip_models)
    download_funasr_models(manifest, env, args.skip_models)
    warm_paddleocr(manifest, env, args.skip_models)
    write_report(manifest, env)
    progress(100, "Portable setup done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
