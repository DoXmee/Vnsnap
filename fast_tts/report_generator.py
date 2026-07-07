from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from .srt_parser import Cue


class ReportGenerator:
    """Write JSON render reports for audit and debugging."""

    def write(
        self,
        path: Path,
        cues: list[Cue],
        started_at: float,
        vram_peak_gb: float,
        cache_tier_breakdown: dict | None = None,
        estimated_next_run_time_sec: float | None = None,
    ) -> dict:
        """Write a JSON report and return the report dictionary."""
        render_time = time.perf_counter() - started_at
        total = len(cues)
        cache_hits = sum(1 for c in cues if c.status == "cached")
        fast_path = sum(1 for c in cues if c.status == "fast")
        heavy = sum(1 for c in cues if c.status == "heavy")
        tail_retries = sum(1 for c in cues if c.status == "tail_retry" or c.tail_issue)
        retried = sum(1 for c in cues if c.retry_count > 0)
        degraded = sum(1 for c in cues if c.status == "degraded")
        report = {
            "render_time_seconds": round(render_time, 3),
            "total_cues": total,
            "cache_hits": cache_hits,
            "cache_hit_rate": round(cache_hits / max(1, total), 3),
            "cache_tier_breakdown": cache_tier_breakdown or {},
            "fast_path": fast_path,
            "heavy_gate": heavy,
            "tail_retries": tail_retries,
            "retried": retried,
            "degraded": degraded,
            "vram_peak_gb": round(vram_peak_gb, 3),
            "throughput_cues_per_sec": round(total / max(0.001, render_time), 3),
            "estimated_next_run_time_sec": estimated_next_run_time_sec,
            "cues_detail": [asdict(cue) for cue in cues],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
