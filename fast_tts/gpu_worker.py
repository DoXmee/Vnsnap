from __future__ import annotations

import gc
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .logging_utils import logger
from .srt_parser import Cue

GPU_LOCK = threading.Lock()
VRAM_LIMIT_BYTES = int(5.5 * 1e9)


@dataclass
class GPUWorkerConfig:
    """VieNeu model and voice configuration used by GPUWorker."""

    model: str
    codec: str
    lora: str = ""
    merged_model_path: str = ""
    ref_audio: str = ""
    ref_codes_path: str = ""
    ref_text: str = ""
    voice_id: str = "voice"
    temperature: float = 0.55
    top_k: int = 25
    max_new_tokens: int = 520
    min_new_tokens: int = 40
    work_dir: Path = Path("fast_tts_work")
    sample_rate: int = 24000
    use_batch: bool = True


class GPUWorker:
    """Singleton-style VieNeu worker that keeps model and ref_codes hot during one session."""

    def __init__(self, config: GPUWorkerConfig) -> None:
        """Create a worker; model is loaded lazily by load_model()."""
        self.config = config
        self.tts = None
        self.ref_codes = None
        self.vram_peak_gb = 0.0
        self.config.work_dir.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        """Load VieNeu, optional runtime LoRA, precomputed ref_codes, and warm up once."""
        self.load_model()
        self.warmup()

    def load_model(self) -> None:
        """Load VieNeu and ref_codes once; use runtime LoRA when merged loading is unavailable."""
        if self.tts is not None:
            return
        with GPU_LOCK:
            self._wait_vram_budget()
            from vieneu import Vieneu
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
            if self.config.merged_model_path and Path(self.config.merged_model_path).exists():
                logger.info(
                    "merged_model_fp16.pt present, but VieNeu standard API has no safe direct loader; "
                    "falling back to base model + runtime LoRA"
                )
            logger.info(f"Loading VieNeu model on {device}: {self.config.model}")
            self.tts = Vieneu(
                mode="standard",
                backbone_repo=self.config.model,
                backbone_device=device,
                codec_repo=self.config.codec,
                codec_device=device,
                gguf_filename=None,
            )
            if self.config.lora:
                self._validate_lora_adapter(Path(self.config.lora))
                self.tts.load_lora_adapter(str(Path(self.config.lora)))
            if device == "cuda":
                try:
                    self.tts.backbone.half().eval()
                except Exception as exc:
                    logger.warning(f"Could not force fp16/eval on backbone: {exc}")
            if self.config.ref_codes_path and Path(self.config.ref_codes_path).exists():
                self.ref_codes = self._load_ref_codes(Path(self.config.ref_codes_path), device)
            elif self.config.ref_audio:
                self.ref_codes = self.tts.encode_reference(str(Path(self.config.ref_audio)))
            self._update_vram_peak()
            logger.info(f"VieNeu loaded, VRAM={self.vram_peak_gb:.2f} GB")

    def _load_ref_codes(self, path: Path, device: str):
        """Load pack-local ref_codes.pt and move tensor codes to the active device when possible."""
        import torch

        data = torch.load(str(path), map_location=device)
        codes = data.get("codes", data) if isinstance(data, dict) else data
        if hasattr(codes, "to"):
            codes = codes.to(device)
            if device == "cuda" and hasattr(codes, "half") and getattr(codes, "is_floating_point", lambda: False)():
                codes = codes.half()
        if isinstance(data, dict) and data.get("ref_text") and not self.config.ref_text:
            self.config.ref_text = str(data["ref_text"])
        logger.info(f"Loaded precomputed ref_codes: {path}")
        return codes

    def warmup(self) -> None:
        """Run a tiny forward pass to initialize kernels and caches."""
        if self.tts is None:
            self.load_model()
        with GPU_LOCK:
            try:
                _ = self.tts.infer(
                    text="xin chào",
                    ref_codes=self.ref_codes,
                    ref_text=self.config.ref_text,
                    max_chars=16,
                    temperature=self.config.temperature,
                    top_k=self.config.top_k,
                    skip_normalize=True,
                    apply_watermark=False,
                )
                logger.info("VieNeu warmup done")
            except Exception as exc:
                logger.warning(f"VieNeu warmup skipped: {exc}")

    def infer_batch(self, cues: list[Cue]) -> list[Path]:
        """Infer audio for each cue independently and return temporary WAV paths."""
        if self.tts is None:
            self.load_model()
        out_paths: list[Path] = []
        with GPU_LOCK:
            self._wait_vram_budget()
            start = time.perf_counter()
            try:
                texts = [cue.normalized_text for cue in cues]
                if self.config.use_batch and hasattr(self.tts, "infer_batch") and len(cues) > 1:
                    audios = self.tts.infer_batch(
                        texts=texts,
                        ref_codes=self.ref_codes,
                        ref_text=self.config.ref_text,
                        temperature=self.config.temperature,
                        top_k=self.config.top_k,
                        skip_normalize=True,
                        apply_watermark=False,
                        max_new_tokens=self.config.max_new_tokens,
                        min_new_tokens=self.config.min_new_tokens,
                    )
                else:
                    audios = [
                        self.tts.infer(
                            text=text,
                            ref_codes=self.ref_codes,
                            ref_text=self.config.ref_text,
                            max_chars=120,
                            temperature=self.config.temperature,
                            top_k=self.config.top_k,
                            skip_normalize=True,
                            apply_watermark=False,
                        )
                        for text in texts
                    ]
                for cue, audio in zip(cues, audios):
                    path = self.config.work_dir / f"cue_{cue.id:06d}_try{cue.retry_count}.wav"
                    self.save_audio(audio, path)
                    cue.output_duration = self._audio_duration_sec(audio)
                    cue.output_wav = str(path)
                    out_paths.append(path)
                self._update_vram_peak()
                logger.info(
                    f"TTS batch done cues={len(cues)} dt={time.perf_counter() - start:.2f}s "
                    f"vram={self.vram_peak_gb:.2f}GB"
                )
            finally:
                gc.collect()
                try:
                    import torch

                    torch.cuda.empty_cache()
                except Exception:
                    pass
        return out_paths

    def save_audio(self, audio: np.ndarray, path: Path) -> None:
        """Save model audio numpy array to WAV."""
        sr = int(getattr(self.tts, "sample_rate", self.config.sample_rate) or self.config.sample_rate)
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(path), np.asarray(audio, dtype=np.float32), sr)

    def _audio_duration_sec(self, audio: np.ndarray) -> float:
        """Return duration of raw model audio at the active sample rate."""
        sr = int(getattr(self.tts, "sample_rate", self.config.sample_rate) or self.config.sample_rate)
        return float(len(np.asarray(audio)) / max(1, sr))

    def close(self) -> None:
        """Release model and clear CUDA cache."""
        with GPU_LOCK:
            if self.tts is not None:
                close = getattr(self.tts, "close", None)
                if callable(close):
                    close()
            self.tts = None
            self.ref_codes = None
            gc.collect()
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass

    def _wait_vram_budget(self) -> None:
        """Wait until current CUDA allocation is under the configured budget."""
        try:
            import torch

            while torch.cuda.is_available() and torch.cuda.memory_allocated() > VRAM_LIMIT_BYTES:
                logger.warning("VRAM above 5.5GB, waiting before next GPU task")
                time.sleep(0.5)
        except Exception:
            return

    def _update_vram_peak(self) -> None:
        """Update peak VRAM metric."""
        try:
            import torch

            if torch.cuda.is_available():
                self.vram_peak_gb = max(self.vram_peak_gb, torch.cuda.memory_allocated() / 1e9)
        except Exception:
            pass

    def _validate_lora_adapter(self, path: Path) -> None:
        """Raise a clear error if the configured LoRA adapter is missing core files."""
        if not path.exists():
            raise FileNotFoundError(f"Missing LoRA adapter directory: {path}")
        if path.is_dir():
            has_config = (path / "adapter_config.json").exists()
            has_weights = (path / "adapter_model.safetensors").exists() or (path / "adapter_model.bin").exists()
            if has_config and has_weights:
                return
            raise FileNotFoundError(
                f"Incomplete LoRA adapter: {path}. "
                "Expected adapter_config.json plus adapter_model.safetensors or adapter_model.bin."
            )
        if path.suffix.lower() not in {".safetensors", ".bin", ".pt"}:
            raise FileNotFoundError(f"Unsupported LoRA adapter path: {path}")


def lora_hash(path: str) -> str:
    """Return a stable hash from LoRA adapter files and mtimes."""
    import hashlib

    if not path:
        return ""
    root = Path(path)
    h = hashlib.sha256()
    for file in sorted(root.glob("adapter_*")):
        h.update(file.name.encode("utf-8"))
        h.update(str(file.stat().st_size).encode())
        h.update(str(int(file.stat().st_mtime)).encode())
    return h.hexdigest()[:16]
