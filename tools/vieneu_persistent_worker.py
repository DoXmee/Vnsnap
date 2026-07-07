from __future__ import annotations

import argparse
import gc
import hashlib
import json
import socket
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import torch
from vieneu import Vieneu

from render_vieneu_candidate_profiles import (
    fast_profile_for_text,
    load_lines,
    render_candidate,
)
from render_vieneu_srt import (
    clean_text,
    load_pack,
    load_ref_codes_for_pack,
    prepare_text_for_tts,
    resolve_lora_dir_for_render,
)


HOST = "127.0.0.1"


def default_port(pack_dir: Path) -> int:
    """Return a stable localhost port for a voice pack."""
    digest = hashlib.sha256(str(pack_dir.resolve()).encode("utf-8", errors="ignore")).hexdigest()
    return 49300 + (int(digest[:6], 16) % 700)


def read_json_line(sock_file) -> dict:
    """Read one JSON request from a socket file."""
    line = sock_file.readline()
    if not line:
        return {}
    return json.loads(line.decode("utf-8", errors="replace"))


def write_json_line(sock_file, value: dict) -> None:
    """Write one JSON response to a socket file."""
    sock_file.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
    sock_file.flush()


class VieneuWorkerState:
    """Persistent VieNeu runtime that keeps model, LoRA, ref_codes, and warmup alive."""

    def __init__(self, pack_dir: Path):
        self.pack_dir = pack_dir.resolve()
        self.pack = {}
        self.lock = threading.Lock()
        self.ready = False
        self.error = ""
        self.started = time.perf_counter()
        self.tts = None
        self.ref_codes = None
        self.load_thread = threading.Thread(target=self._load_guarded, daemon=True)
        self.load_thread.start()

    def _load_guarded(self) -> None:
        """Load the model in the background so the server port opens immediately."""
        try:
            self._load()
            self.ready = True
        except Exception as exc:
            self.error = str(exc)
            print(f"[worker] load failed: {exc}", file=sys.stderr, flush=True)

    def _load(self) -> None:
        """Load VieNeu, LoRA, ref_codes once and run a warmup utterance."""
        self.pack = load_pack(self.pack_dir)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self.tts = Vieneu(
            mode=clean_text(str(self.pack.get("vieneuMode", "standard"))) or "standard",
            backbone_repo=self.pack.get("model", "pnnbao-ump/VieNeu-TTS-0.3B"),
            gguf_filename=self.pack.get("ggufFilename", None),
            backbone_device=device,
            codec_repo=self.pack.get("codec", "neuphonic/distill-neucodec"),
            codec_device=device,
        )
        lora_dir = resolve_lora_dir_for_render(self.pack)
        if lora_dir is not None:
            self.tts.load_lora_adapter(str(lora_dir))
        self.ref_codes = load_ref_codes_for_pack(self.tts, self.pack)

        warmup_text = str(self.pack.get("candidateWarmupText") or self.pack.get("warmupText") or "Xin chào.")
        try:
            prepared = prepare_text_for_tts(self.tts, warmup_text)
            _ = self.tts.infer(
                text=prepared,
                ref_codes=self.ref_codes,
                ref_text=self.pack.get("refText", ""),
                max_chars=80,
                temperature=0.55,
                top_k=25,
                skip_normalize=True,
                apply_watermark=False,
            )
            if torch.cuda.is_available():
                torch.cuda.synchronize()
        except Exception as exc:
            print(f"[worker] warmup warning: {exc}", file=sys.stderr, flush=True)
        gc.collect()

    def wait_ready(self, timeout: float = 600.0) -> None:
        """Wait until model is ready or fail with the load error."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.ready:
                return
            if self.error:
                raise RuntimeError(self.error)
            time.sleep(0.5)
        raise RuntimeError("VieNeu worker model load timed out")

    def render_lines(self, lines: list[str], out_dir: Path, max_cues: int = 0, force_profile: str = "") -> dict:
        """Render text lines one by one using the approved original tts.infer path."""
        self.wait_ready()
        with self.lock:
            started = time.perf_counter()
            out_dir.mkdir(parents=True, exist_ok=True)
            lines = [clean_text(str(line)) for line in lines if clean_text(str(line))]
            if max_cues:
                lines = lines[:max_cues]
            report = []
            for index, text in enumerate(lines, start=1):
                profile = fast_profile_for_text(text, self.pack, force_profile)
                cue_dir = out_dir / f"cau_{index:04d}_candidates"
                out_file = cue_dir / f"{profile.name}.mp3"
                item_started = time.perf_counter()
                duration = render_candidate(self.tts, self.ref_codes, self.pack, text, profile, out_file)
                best = {
                    "index": index,
                    "text": text,
                    "profile": profile.name,
                    "file": str(out_file),
                    "duration": round(duration, 3),
                    "settings": asdict(profile),
                    "batchSize": 1,
                    "inferMode": "persistent_worker_single_tts_infer",
                    "inferSec": round(time.perf_counter() - item_started, 3),
                    "maxNewTokens": int(self.pack.get("candidateSingleMaxNewTokens", 0) or 0) if bool(self.pack.get("candidateUseSingleInferBatch", False)) else "unbounded",
                    "limitedTokens": bool(self.pack.get("candidateUseSingleInferBatch", False)),
                }
                best_out = out_dir / f"cau_{index:04d}.mp3"
                best_out.parent.mkdir(parents=True, exist_ok=True)
                best_out.write_bytes(out_file.read_bytes())
                report.append({"index": index, "text": text, "best": best, "bestFile": str(best_out), "candidates": [best]})
            manifest = {
                "ok": True,
                "worker": "persistent",
                "packDir": str(self.pack_dir),
                "outDir": str(out_dir.resolve()),
                "elapsedSec": round(time.perf_counter() - started, 3),
                "total": len(report),
                "mode": "fast-single-infer",
                "clips": report,
            }
            (out_dir / "candidate_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            return manifest


class WorkerHandler(socketserver.StreamRequestHandler):
    """Line-delimited JSON socket handler."""

    state: VieneuWorkerState

    def handle(self) -> None:
        try:
            req = read_json_line(self.rfile)
            cmd = req.get("cmd")
            if cmd == "ping":
                write_json_line(self.wfile, {
                    "ok": True,
                    "ready": bool(self.state.ready),
                    "status": "ready" if self.state.ready else ("error" if self.state.error else "loading"),
                    "error": self.state.error,
                    "uptimeSec": round(time.perf_counter() - self.state.started, 3),
                })
                return
            if cmd == "render":
                out_dir = Path(req["outDir"])
                result = self.state.render_lines(
                    lines=[str(x).strip() for x in req.get("lines", []) if str(x).strip()],
                    out_dir=out_dir,
                    max_cues=int(req.get("maxCues") or 0),
                    force_profile=str(req.get("forceProfile") or ""),
                )
                write_json_line(self.wfile, {"ok": True, "result": {"outDir": result["outDir"], "manifest": result}})
                return
            write_json_line(self.wfile, {"ok": False, "error": f"unknown cmd: {cmd}"})
        except Exception as exc:
            write_json_line(self.wfile, {"ok": False, "error": str(exc)})


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """TCP server that allows quick repeated render requests."""

    allow_reuse_address = True
    daemon_threads = True


def send_request(port: int, payload: dict, timeout: float = 30.0) -> dict:
    """Send a JSON request to the worker."""
    with socket.create_connection((HOST, port), timeout=timeout) as sock:
        file = sock.makefile("rwb")
        write_json_line(file, payload)
        return read_json_line(file)


def start_server_process(pack_dir: Path, port: int) -> None:
    """Start a detached worker server process."""
    creationflags = 0
    executable = Path(sys.executable)
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            executable = pythonw
    root = Path(__file__).resolve().parents[1]
    log_dir = root / "vieneu_work" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"vieneu_worker_{port}.log"
    log_fh = log_file.open("a", encoding="utf-8", errors="replace")
    log_fh.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} pack={pack_dir} port={port} ---\n")
    log_fh.flush()
    subprocess.Popen(
        [str(executable), str(Path(__file__).resolve()), "--server", "--pack-dir", str(pack_dir), "--port", str(port)],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=log_fh,
        creationflags=creationflags,
        close_fds=True,
    )


def wait_ready(port: int, timeout: float) -> dict:
    """Wait until worker responds to ping."""
    deadline = time.perf_counter() + timeout
    last_error = ""
    while time.perf_counter() < deadline:
        try:
            response = send_request(port, {"cmd": "ping"}, timeout=3.0)
            if response.get("ready"):
                return response
            if response.get("status") == "error":
                raise RuntimeError(response.get("error") or "worker load error")
        except Exception as exc:
            last_error = str(exc)
            time.sleep(1.0)
    raise RuntimeError(f"worker not ready after {timeout:.0f}s: {last_error}")


def run_server(args: argparse.Namespace) -> int:
    """Run the persistent worker server."""
    state = VieneuWorkerState(args.pack_dir)
    WorkerHandler.state = state
    with ThreadedTCPServer((HOST, args.port), WorkerHandler) as server:
        print(json.dumps({"ok": True, "server": True, "port": args.port, "packDir": str(args.pack_dir.resolve())}), flush=True)
        server.serve_forever()
    return 0


def run_client(args: argparse.Namespace) -> int:
    """Start/connect worker and ask it to render lines."""
    lines = load_lines(args)
    if args.max_cues:
        lines = lines[:args.max_cues]
    if not lines:
        raise SystemExit("Khong co cau text hop le de render.")
    port = args.port or default_port(args.pack_dir)
    try:
        ping = send_request(port, {"cmd": "ping"}, timeout=2.0)
        if not ping.get("ok"):
            raise RuntimeError(str(ping))
    except Exception:
        start_server_process(args.pack_dir, port)
        wait_ready(port, args.startup_timeout)
    response = send_request(
        port,
        {
            "cmd": "render",
            "lines": lines,
            "outDir": str(args.out_dir),
            "maxCues": args.max_cues,
            "forceProfile": args.force_profile or "",
        },
        timeout=max(60.0, float(args.render_timeout)),
    )
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "worker render failed")
    print(json.dumps(response["result"], ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--pack-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--text")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--max-cues", type=int, default=0)
    parser.add_argument("--force-profile", default="")
    parser.add_argument("--startup-timeout", type=float, default=240.0)
    parser.add_argument("--render-timeout", type=float, default=7200.0)
    args = parser.parse_args()
    args.pack_dir = args.pack_dir.resolve()
    args.port = args.port or default_port(args.pack_dir)
    if args.server:
        return run_server(args)
    if args.out_dir is None:
        raise SystemExit("--out-dir is required in client mode")
    return run_client(args)


if __name__ == "__main__":
    raise SystemExit(main())
