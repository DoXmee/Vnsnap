from __future__ import annotations

import gc
import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .logging_utils import logger


@dataclass
class VoicePack:
    """Validated VieNeu voice pack paths and render metadata for FAST-TTS v4."""

    pack_dir: Path
    pack_json_path: Path
    data: dict[str, Any]
    voice_id: str
    display_name: str
    model: str
    codec: str
    lora_path: Path | None
    merged_model_path: Path | None
    ref_audio_path: Path | None
    ref_text: str
    ref_text_path: Path | None
    ref_codes_path: Path | None
    cache_dir: Path
    merged_model_hash: str
    ref_codes_hash: str
    text_norm_version: str


class VoicePackManager:
    """Prepare VieNeu Case A-lite voice packs: LoRA plus precomputed ref codec codes."""

    def __init__(self, root: Path) -> None:
        """Create a manager rooted at the application directory."""
        self.root = root

    def prepare(self, voice: str | Path, prepare_assets: bool = True) -> VoicePack:
        """Resolve, validate, and optionally prepare a v4 voice pack."""
        pack_path = self.resolve_pack_json(voice)
        pack_dir = pack_path.parent
        data = json.loads(pack_path.read_text(encoding="utf-8-sig"))
        data = self._upgrade_legacy_pack(data, pack_dir)

        if prepare_assets:
            self._ensure_ref_files(data, pack_dir)
            self._ensure_merged_model(data, pack_dir)
            self._ensure_ref_codes(data, pack_dir)
            pack_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        cache_dir = self._resolve_path(
            data.get("fastTtsCacheDir")
            or data.get("cache_dir")
            or "_FAST_TTS_CACHE_DO_NOT_DELETE/v4_cache",
            pack_dir,
        )
        cache_dir.mkdir(parents=True, exist_ok=True)

        lora_path = self._optional_path(data.get("lora_path") or data.get("loraDir"), pack_dir)
        self._validate_lora_path(lora_path, data, pack_dir)
        ref_audio = self._optional_path(data.get("ref_audio") or data.get("refAudio"), pack_dir)
        ref_text_path = self._optional_path(data.get("ref_text"), pack_dir)
        ref_codes = self._optional_path(data.get("ref_codes"), pack_dir)
        merged = self._optional_path(data.get("merged_model_path"), pack_dir)

        ref_text = str(data.get("refText", ""))
        if ref_text_path and ref_text_path.exists():
            ref_text = ref_text_path.read_text(encoding="utf-8-sig", errors="replace").strip()

        return VoicePack(
            pack_dir=pack_dir,
            pack_json_path=pack_path,
            data=data,
            voice_id=str(data.get("voice_id") or data.get("id") or pack_dir.name),
            display_name=str(data.get("display_name") or data.get("name") or pack_dir.name),
            model=str(data.get("model") or data.get("backbone") or "pnnbao-ump/VieNeu-TTS-0.3B"),
            codec=str(data.get("codec") or "neuphonic/distill-neucodec"),
            lora_path=lora_path,
            merged_model_path=merged if merged and merged.exists() else None,
            ref_audio_path=ref_audio if ref_audio and ref_audio.exists() else None,
            ref_text=ref_text,
            ref_text_path=ref_text_path,
            ref_codes_path=ref_codes if ref_codes and ref_codes.exists() else None,
            cache_dir=cache_dir,
            merged_model_hash=str(data.get("merged_model_hash", "")),
            ref_codes_hash=str(data.get("ref_codes_hash", "")),
            text_norm_version=str(data.get("text_norm_version", "vi_norm_v1.3_tail")),
        )

    def resolve_pack_json(self, voice: str | Path) -> Path:
        """Resolve a voice id, pack directory, or pack.json path."""
        voice_path = Path(voice)
        candidates = [
            voice_path,
            self.root / voice_path,
            self.root / "voice_packs" / "vieneu" / str(voice),
            self.root / "voice_packs" / "vieneu" / str(voice) / "pack.json",
        ]
        for candidate in candidates:
            pack_json = candidate / "pack.json" if candidate.is_dir() else candidate
            if pack_json.exists():
                return pack_json.resolve()
        raise FileNotFoundError(f"Cannot find voice pack: {voice}")

    def _upgrade_legacy_pack(self, data: dict[str, Any], pack_dir: Path) -> dict[str, Any]:
        """Add v4 keys to older packs without removing legacy fields."""
        data.setdefault("voice_id", data.get("id", pack_dir.name))
        data.setdefault("display_name", data.get("name", pack_dir.name))
        data.setdefault("model_version", "vieneu_case_a_lite_v4")
        data.setdefault("render_settings", {})
        data["render_settings"].setdefault("speed", data.get("srtSpeechSpeed", data.get("speechSpeed", 1.0)))
        data["render_settings"].setdefault("pitch", data.get("pitch", 0.0))
        data["render_settings"].setdefault("emotion", data.get("emotion", "neutral"))
        data["render_settings"].setdefault("tail_pad_ms", int(data.get("tailPadMs", 80)))
        data["render_settings"].setdefault("max_stretch_ratio", 1.08)
        data.setdefault("quality_config", {})
        data["quality_config"].setdefault("cheap_gate_ratio_min", 0.5)
        data["quality_config"].setdefault("cheap_gate_ratio_max", 2.0)
        data["quality_config"].setdefault("chars_per_sec_min", 5)
        data["quality_config"].setdefault("chars_per_sec_max", 25)
        data["quality_config"].setdefault("tail_fade_threshold", 0.85)
        data["quality_config"].setdefault("tail_abrupt_threshold", 0.15)
        data["quality_config"].setdefault("asr_threshold", 0.85)
        data["quality_config"].setdefault("max_retry", 4)
        data.setdefault("text_norm_version", "vi_norm_v1.3_tail")
        data.setdefault("ref_codes", "ref_codes.pt")
        data.setdefault("ref_text", "ref_text.txt")
        data.setdefault("merged_model_path", "merged_model_fp16.pt")
        data.setdefault("fastTtsCacheDir", str(pack_dir / "_FAST_TTS_CACHE_DO_NOT_DELETE" / "v4_cache"))
        return data

    def _ensure_ref_files(self, data: dict[str, Any], pack_dir: Path) -> None:
        """Copy legacy ref audio/text into pack-local canonical files when possible."""
        ref_audio = self._optional_path(data.get("ref_audio") or data.get("refAudio"), pack_dir)
        canonical_audio = pack_dir / "ref_audio.wav"
        if ref_audio and ref_audio.exists() and not canonical_audio.exists():
            try:
                shutil.copy2(ref_audio, canonical_audio)
                data["ref_audio"] = "ref_audio.wav"
            except Exception as exc:
                logger.warning(f"Could not copy ref audio into pack: {exc}")
        elif canonical_audio.exists():
            data["ref_audio"] = "ref_audio.wav"

        ref_text_path = pack_dir / "ref_text.txt"
        ref_text = str(data.get("refText", "")).strip()
        if ref_text and not ref_text_path.exists():
            ref_text_path.write_text(ref_text, encoding="utf-8")
            data["ref_text"] = "ref_text.txt"
        elif ref_text_path.exists():
            data["ref_text"] = "ref_text.txt"

    def _ensure_merged_model(self, data: dict[str, Any], pack_dir: Path) -> None:
        """Validate or optionally build a pre-merged model artifact."""
        merged_path = self._resolve_path(data.get("merged_model_path", "merged_model_fp16.pt"), pack_dir)
        expected = str(data.get("merged_model_hash", ""))
        if merged_path.exists() and (not expected or self._compute_file_hash(merged_path) == expected):
            data["merged_model_hash"] = self._compute_file_hash(merged_path)
            return

        if not data.get("enable_premerge_lora", False):
            logger.info("Pre-merged LoRA disabled for this VieNeu pack; using runtime LoRA load")
            data["merged_model_hash"] = ""
            return

        raise NotImplementedError(
            "VieNeu standard loader does not expose a safe merged checkpoint load path yet. "
            "Set enable_premerge_lora=false or add a project-specific merge adapter."
        )

    def _ensure_ref_codes(self, data: dict[str, Any], pack_dir: Path) -> None:
        """Extract and store codec ref codes when missing or hash-invalid."""
        ref_codes = self._resolve_path(data.get("ref_codes", "ref_codes.pt"), pack_dir)
        expected = str(data.get("ref_codes_hash", ""))
        if ref_codes.exists() and (not expected or self._compute_file_hash(ref_codes) == expected):
            data["ref_codes_hash"] = self._compute_file_hash(ref_codes)
            return

        ref_audio = self._optional_path(data.get("ref_audio") or data.get("refAudio"), pack_dir)
        if not ref_audio or not ref_audio.exists():
            logger.warning("No ref audio found; ref_codes.pt cannot be extracted")
            return

        logger.info(f"Extracting VieNeu ref_codes once: {ref_audio}")
        from vieneu import Vieneu
        import torch

        tts = None
        try:
            device = "cuda" if torch.cuda.is_available() else "cpu"
            tts = Vieneu(
                mode="standard",
                backbone_repo=str(data.get("model") or "pnnbao-ump/VieNeu-TTS-0.3B"),
                backbone_device=device,
                codec_repo=str(data.get("codec") or "neuphonic/distill-neucodec"),
                codec_device=device,
                gguf_filename=None,
            )
            codes = tts.encode_reference(str(ref_audio))
            ref_text = self._read_ref_text(data, pack_dir)
            ref_codes.parent.mkdir(parents=True, exist_ok=True)
            torch.save({"codes": codes, "ref_text": ref_text, "source_ref_audio": str(ref_audio)}, ref_codes)
            data["ref_codes"] = str(ref_codes.relative_to(pack_dir)) if ref_codes.is_relative_to(pack_dir) else str(ref_codes)
            data["ref_codes_hash"] = self._compute_file_hash(ref_codes)
            data["ref_codes_extracted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            logger.info(f"ref_codes saved: {ref_codes}")
        finally:
            if tts is not None and callable(getattr(tts, "close", None)):
                tts.close()
            del tts
            gc.collect()
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass

    def _read_ref_text(self, data: dict[str, Any], pack_dir: Path) -> str:
        """Read ref text from canonical pack file or legacy pack field."""
        path = self._optional_path(data.get("ref_text"), pack_dir)
        if path and path.exists():
            return path.read_text(encoding="utf-8-sig", errors="replace").strip()
        return str(data.get("refText", "")).strip()

    def _validate_lora_path(self, lora_path: Path | None, data: dict[str, Any], pack_dir: Path) -> None:
        """Fail early when a pack points at an incomplete LoRA adapter directory."""
        if lora_path is None:
            return
        if not lora_path.exists():
            raise FileNotFoundError(
                f"Voice pack '{pack_dir.name}' points to a missing LoRA directory: {lora_path}"
            )
        if lora_path.is_dir():
            config = lora_path / "adapter_config.json"
            model_files = [
                lora_path / "adapter_model.safetensors",
                lora_path / "adapter_model.bin",
            ]
            if config.exists() and any(path.exists() for path in model_files):
                return
            raise FileNotFoundError(
                f"Voice pack '{pack_dir.name}' has an incomplete LoRA adapter: {lora_path}. "
                "Expected adapter_config.json plus adapter_model.safetensors or adapter_model.bin."
            )
        if lora_path.suffix.lower() in {".safetensors", ".bin", ".pt"} and lora_path.exists():
            return
        raise FileNotFoundError(f"Unsupported LoRA path in voice pack '{pack_dir.name}': {lora_path}")

    def _optional_path(self, value: Any, pack_dir: Path) -> Path | None:
        """Resolve a path-like value or return None for empty values."""
        text = str(value or "").strip()
        if not text:
            return None
        return self._resolve_path(text, pack_dir)

    def _resolve_path(self, value: Any, pack_dir: Path) -> Path:
        """Resolve relative pack paths and absolute Windows paths."""
        path = Path(str(value))
        return path if path.is_absolute() else (pack_dir / path)

    def _compute_file_hash(self, path: Path) -> str:
        """Return SHA256 hex digest for one file."""
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
