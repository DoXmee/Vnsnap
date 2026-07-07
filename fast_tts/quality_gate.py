from __future__ import annotations

import gc
import math
import subprocess
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import soundfile as sf

from .gpu_worker import GPU_LOCK
from .logging_utils import logger
from .srt_parser import Cue


@dataclass
class GateResult:
    """Quality gate decision and detailed reasons."""

    passed: bool
    reasons: list[str] = field(default_factory=list)
    asr_score: float = 0.0
    duration_ratio: float = 0.0
    asr_text: str = ""
    tail_issue: bool = False
    tail_reason: str = ""


class CheapQualityGate:
    """CPU-only quality gate for silence, RMS, clipping, duration, CPS, and final-tail health."""

    def __init__(
        self,
        silence_db: float = -40.0,
        tail_fade_threshold: float = 0.85,
        tail_abrupt_threshold: float = 0.15,
    ) -> None:
        """Create cheap gate with silence threshold in dBFS."""
        self.silence_db = silence_db
        self.tail_fade_threshold = tail_fade_threshold
        self.tail_abrupt_threshold = tail_abrupt_threshold

    def evaluate(self, cue: Cue, wav_path: Path) -> GateResult:
        """Trim leading/trailing silence and return pass/fail reasons."""
        reasons: list[str] = []
        data, sr = sf.read(str(wav_path), dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        analysis = self.trim_silence(data)
        duration = max(0.001, len(data) / sr)
        rms = float(np.sqrt(np.mean(np.square(analysis))) + 1e-12)
        rms_db = 20 * math.log10(rms)
        peak = float(np.max(np.abs(analysis))) if len(analysis) else 0.0
        ratio = duration / max(0.001, cue.duration_ms / 1000.0)
        cps = len(cue.normalized_text) / duration
        if rms_db < -45:
            reasons.append(f"low_rms={rms_db:.1f}dB")
        if rms_db > -3 or peak > 0.98:
            reasons.append(f"clipping peak={peak:.3f} rms={rms_db:.1f}dB")
        if not (0.5 <= ratio <= 2.0):
            reasons.append(f"duration_ratio={ratio:.2f}")
        if not (5 <= cps <= 25):
            reasons.append(f"chars_per_sec={cps:.1f}")
        tail = self._check_tail(data, sr)
        tail_issue = not tail["ok"]
        if tail_issue:
            reasons.append(f"tail_issue={tail['reason']}")
            cue.tail_issue = True
            cue.tail_reason = str(tail["reason"])
        cue.duration_ratio = ratio
        cue.output_duration = duration
        return GateResult(not reasons, reasons, duration_ratio=ratio, tail_issue=tail_issue, tail_reason=str(tail["reason"]))

    def trim_silence(self, data: np.ndarray) -> np.ndarray:
        """Return audio with leading/trailing low-level regions removed."""
        if len(data) == 0:
            return data
        threshold = 10 ** (self.silence_db / 20)
        idx = np.flatnonzero(np.abs(data) > threshold)
        if len(idx) == 0:
            return data
        pad = 240
        start = max(0, int(idx[0]) - pad)
        end = min(len(data), int(idx[-1]) + pad)
        return data[start:end]

    def _check_tail(self, data: np.ndarray, sr: int) -> dict[str, object]:
        """Detect abrupt or unfaded final 150ms that often means the last word was cut."""
        if len(data) < int(0.18 * sr):
            return {"ok": True, "reason": "short_audio_skip"}
        tail_len = int(0.150 * sr)
        tail = data[-tail_len:]
        mid = max(1, len(tail) // 2)
        first_e = float(np.abs(tail[:mid]).mean())
        second_e = float(np.abs(tail[mid:]).mean())
        drop_ratio = second_e / (first_e + 1e-9)
        if drop_ratio > self.tail_fade_threshold and first_e > 0.003:
            return {"ok": False, "reason": "no_fade_out"}
        last_20 = data[-int(0.020 * sr) :]
        if len(last_20) and float(np.abs(last_20).max()) > self.tail_abrupt_threshold:
            return {"ok": False, "reason": "abrupt_end"}
        return {"ok": True, "reason": "ok"}


class HeavyQualityGate:
    """GPU ASR gate using faster-whisper base/small under GPU_LOCK."""

    def __init__(self, model_name: str = "base", threshold: float = 0.85) -> None:
        """Create heavy ASR gate; model is loaded only during evaluate()."""
        if model_name not in {"base", "small"}:
            raise ValueError("Heavy gate only allows faster-whisper base or small")
        self.model_name = model_name
        self.threshold = threshold

    def evaluate(self, cue: Cue, wav_path: Path) -> GateResult:
        """Transcribe WAV and fuzzy-match ASR text against normalized cue text."""
        with GPU_LOCK:
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass
            from faster_whisper import WhisperModel

            model = None
            try:
                model = WhisperModel(self.model_name, device="cuda", compute_type="float16")
                segments, _ = model.transcribe(
                    str(wav_path),
                    language="vi",
                    beam_size=5,
                    vad_filter=False,
                    condition_on_previous_text=False,
                )
                asr_text = " ".join(seg.text.strip() for seg in segments).strip()
                score = SequenceMatcher(None, cue.normalized_text.lower(), asr_text.lower()).ratio()
                cue.asr_score = score
                passed = score >= self.threshold
                reasons = [] if passed else [f"asr_score={score:.3f}", f"asr={asr_text}"]
                return GateResult(passed, reasons, asr_score=score, asr_text=asr_text)
            except Exception as exc:
                logger.error(f"Heavy gate failed cue={cue.id}: {exc}")
                return GateResult(False, [f"heavy_gate_exception={exc}"])
            finally:
                del model
                gc.collect()
                try:
                    import torch

                    torch.cuda.empty_cache()
                except Exception:
                    pass
