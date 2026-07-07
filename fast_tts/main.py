from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .batch_scheduler import BatchScheduler
from .cache_manager import CacheManager
from .ffmpeg_renderer import FFmpegRenderer
from .gpu_worker import GPUWorker, GPUWorkerConfig, lora_hash
from .logging_utils import logger
from .quality_gate import CheapQualityGate, HeavyQualityGate
from .report_generator import ReportGenerator
from .retry_manager import RetryManager
from .srt_parser import Cue, SRTParser
from .streaming_assembler import StreamingAssembler
from .text_normalizer import TextNormalizer
from .timeline_assembler import TimelineAssembler
from .voice_pack_manager import VoicePack, VoicePackManager


class FastTTSOrchestrator:
    """FAST-TTS ORCHESTRATOR 4.0 for VieNeu Case A-lite voice packs."""

    def __init__(
        self,
        srt: Path,
        voice: str,
        output: Path,
        cache_dir: Path | None = None,
        lora: str = "",
        model_version: str = "vieneu_case_a_lite_v4",
        no_cache: bool = False,
        warm_cache: bool = False,
        max_cues: int = 0,
        prepare_assets: bool = True,
    ) -> None:
        """Create the v4 orchestrator and prepare the voice pack first."""
        self.root = Path(__file__).resolve().parents[1]
        self.srt = srt
        self.voice_arg = voice
        self.output = output
        self.lora = lora
        self.model_version = model_version
        self.no_cache = no_cache
        self.warm_cache = warm_cache
        self.max_cues = max_cues
        self.normalizer = TextNormalizer()
        self.pack: VoicePack = VoicePackManager(self.root).prepare(voice, prepare_assets=prepare_assets)
        self.voice_settings = self.build_voice_settings()
        resolved_cache = cache_dir if cache_dir and str(cache_dir).strip() else self.pack.cache_dir
        self.cache = CacheManager(resolved_cache, self.voice_settings, self.pack.text_norm_version)
        quality = self.pack.data.get("quality_config", {})
        self.scheduler = BatchScheduler()
        self.retry = RetryManager(max_retry=int(quality.get("max_retry", 4)))
        self.cheap_gate = CheapQualityGate(
            tail_fade_threshold=float(quality.get("tail_fade_threshold", 0.85)),
            tail_abrupt_threshold=float(quality.get("tail_abrupt_threshold", 0.15)),
        )
        self.heavy_gate = HeavyQualityGate(
            str(self.pack.data.get("srtAsrModel") or "base"),
            threshold=float(quality.get("asr_threshold", 0.85)),
        )
        self.worker = GPUWorker(self.build_worker_config())

    def run(self) -> dict[str, Any]:
        """Run parse, normalize, cache preload, inference, QA, assembly, and report."""
        started = time.perf_counter()
        cues = SRTParser().parse(self.srt)
        if self.max_cues > 0:
            cues = cues[: self.max_cues]
        self.normalizer.normalize_cues(cues)
        logger.info(f"Parsed cues={len(cues)} from {self.srt}")

        for cue in cues:
            flags = self.normalizer.safety_flags(cue.normalized_text)
            if flags:
                logger.warning(f"cue={cue.id} safety flags={flags}")

        if self.no_cache:
            pending = cues
        else:
            preload = self.cache.preload_all(cues)
            pending = preload.misses

        if pending:
            self.worker.initialize()
            self.process_pending(pending)
            self.worker.close()
        self.cache.flush()

        if not self.warm_cache:
            items = TimelineAssembler().assemble(cues)
            renderer = FFmpegRenderer(self.root / "ffmpeg.exe")
            if len(cues) > 2000:
                StreamingAssembler(renderer).render(items, self.output)
            else:
                renderer.render(items, self.output)
        else:
            logger.info("Warm-cache mode: skipped final MP3 render")

        report_path = self.output.with_suffix(".report.json")
        report = ReportGenerator().write(
            report_path,
            cues,
            started,
            self.worker.vram_peak_gb,
            cache_tier_breakdown=self.cache.tier_breakdown,
            estimated_next_run_time_sec=self.estimate_next_run_time(cues),
        )
        self.cache.close()
        logger.info(f"Report written: {report_path}")
        return report

    def process_pending(self, pending: list[Cue]) -> None:
        """Render all cache misses; per-cue failures never abort the full job."""
        queue = list(pending)
        while queue:
            batches = self.scheduler.schedule(queue)
            queue = []
            for batch in batches:
                try:
                    self.worker.infer_batch(batch)
                except Exception as exc:
                    logger.error(f"TTS batch exception: {exc}")
                    for cue in batch:
                        if self.retry.next_retry(cue, f"tts_exception={exc}"):
                            queue.append(cue)
                    continue
                queue.extend(self.evaluate_batch(batch))
                self.cache.flush()

    def evaluate_batch(self, batch: list[Cue]) -> list[Cue]:
        """Run cheap gate, tail retry shortcut, optional ASR gate, and cache accepted clips."""
        retry_queue: list[Cue] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(self.cheap_gate.evaluate, cue, Path(cue.output_wav)): cue
                for cue in batch
                if cue.output_wav
            }
            cheap_results = {cue.id: future.result() for future, cue in futures.items()}

        for cue in batch:
            if cue.status == "degraded" or not cue.output_wav:
                continue
            result = cheap_results.get(cue.id)
            if result is None:
                if self.retry.next_retry(cue, "missing_cheap_gate_result"):
                    retry_queue.append(cue)
                continue
            if result.tail_issue:
                if self.retry.next_tail_retry(cue, result.tail_reason):
                    retry_queue.append(cue)
                else:
                    self.cache.put(cue, Path(cue.output_wav), {"tail_degraded": cue.degraded_reason})
                continue
            if result.passed:
                cue.status = "fast"
                self.cache.put(cue, Path(cue.output_wav), {"cheap_gate": result.reasons})
                continue

            logger.warning(f"cheap gate suspicious cue={cue.id}: {result.reasons}")
            heavy = self.heavy_gate.evaluate(cue, Path(cue.output_wav))
            if heavy.passed:
                cue.status = "heavy"
                self.cache.put(
                    cue,
                    Path(cue.output_wav),
                    {"cheap_gate": result.reasons, "asr_text": heavy.asr_text, "asr_score": heavy.asr_score},
                )
                continue

            if self.retry.next_retry(cue, "; ".join(result.reasons + heavy.reasons)):
                retry_queue.append(cue)
            else:
                self.cache.put(cue, Path(cue.output_wav), {"degraded": cue.degraded_reason})
        return retry_queue

    def build_voice_settings(self) -> dict[str, Any]:
        """Return cache-relevant voice settings, deliberately excluding all SRT timing."""
        render = self.pack.data.get("render_settings", {})
        lora = self.lora or (str(self.pack.lora_path) if self.pack.lora_path else "")
        return {
            "voice_id": self.pack.voice_id,
            "speed": render.get("speed", self.pack.data.get("srtSpeechSpeed", self.pack.data.get("speechSpeed", 1.0))),
            "pitch": render.get("pitch", self.pack.data.get("pitch", "")),
            "emotion": render.get("emotion", self.pack.data.get("emotion", "")),
            "model_version": self.model_version,
            "lora_hash": self.pack.data.get("lora_hash") or lora_hash(lora),
            "merged_model_hash": self.pack.merged_model_hash,
            "ref_codes_hash": self.pack.ref_codes_hash,
        }

    def build_worker_config(self) -> GPUWorkerConfig:
        """Build GPUWorkerConfig from the prepared v4 voice pack."""
        render = self.pack.data.get("render_settings", {})
        return GPUWorkerConfig(
            model=self.pack.model,
            codec=self.pack.codec,
            lora=self.lora or (str(self.pack.lora_path) if self.pack.lora_path else ""),
            merged_model_path=str(self.pack.merged_model_path or ""),
            ref_audio=str(self.pack.ref_audio_path or ""),
            ref_codes_path=str(self.pack.ref_codes_path or ""),
            ref_text=self.pack.ref_text,
            voice_id=self.pack.voice_id,
            temperature=float(self.pack.data.get("temperature", 0.55)),
            top_k=int(self.pack.data.get("topK", 25)),
            max_new_tokens=int(self.pack.data.get("srtMaxNewTokens", 520)),
            min_new_tokens=int(self.pack.data.get("srtMinNewTokens", 40)),
            work_dir=self.output.with_suffix("").parent / (self.output.stem + "_clips_tmp"),
            use_batch=bool(self.pack.data.get("fastTtsUseBatch", True)),
        )

    def estimate_next_run_time(self, cues: list[Cue]) -> float:
        """Estimate a next fully-cached run: mostly SQLite lookup plus FFmpeg assembly."""
        return round(max(1.0, len(cues) * 0.004), 3)


def build_parser() -> argparse.ArgumentParser:
    """Create CLI parser for FAST-TTS ORCHESTRATOR 4.0."""
    parser = argparse.ArgumentParser(description="FAST-TTS ORCHESTRATOR 4.0")
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--voice", default="")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache-dir", default=None, type=Path)
    parser.add_argument("--lora", default="")
    parser.add_argument("--model-version", default="vieneu_case_a_lite_v4")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--warm-cache", action="store_true")
    parser.add_argument("--max-cues", type=int, default=0)
    parser.add_argument("--prepare-voice", default="")
    parser.add_argument("--voice-dir", default=None, type=Path)
    parser.add_argument("--skip-prepare-assets", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns process exit code."""
    if hasattr(logger, "remove") and hasattr(logger, "add"):
        logger.remove()
        logger.add(sys.stderr, level="INFO")
    args = build_parser().parse_args(argv)
    try:
        if args.prepare_voice:
            voice_target = args.voice_dir if args.voice_dir is not None else args.prepare_voice
            pack = VoicePackManager(Path(__file__).resolve().parents[1]).prepare(voice_target, prepare_assets=True)
            logger.info(f"Voice prepared: {pack.display_name} cache={pack.cache_dir}")
            return 0

        if not args.srt:
            raise ValueError("--srt is required unless --prepare-voice is used")
        if not args.voice:
            raise ValueError("--voice is required")
        output = args.output or args.srt.with_suffix(".fast_tts_v4.mp3")
        orchestrator = FastTTSOrchestrator(
            srt=args.srt,
            voice=args.voice,
            output=output,
            cache_dir=args.cache_dir,
            lora=args.lora,
            model_version=args.model_version,
            no_cache=args.no_cache,
            warm_cache=args.warm_cache,
            max_cues=args.max_cues,
            prepare_assets=not args.skip_prepare_assets,
        )
        report = orchestrator.run()
        logger.info(f"Done total={report['total_cues']} cache_hit_rate={report['cache_hit_rate']}")
        return 0
    except Exception as exc:
        logger.exception(f"FAST-TTS failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
