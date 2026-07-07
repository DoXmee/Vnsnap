from __future__ import annotations

from collections.abc import Iterable

from .srt_parser import Cue


class BatchScheduler:
    """Build dynamic batches sorted by text length to reduce tensor padding."""

    def schedule(self, cues: Iterable[Cue]) -> list[list[Cue]]:
        """Return batches sorted by normalized text length."""
        pending = sorted(cues, key=lambda cue: len(cue.normalized_text))
        batches: list[list[Cue]] = []
        current: list[Cue] = []
        for cue in pending:
            candidate = current + [cue]
            if len(candidate) > self.max_batch_size(candidate) or (current and cue.force_single):
                batches.append(current)
                current = [cue]
            else:
                current = candidate
            if cue.force_single:
                batches.append(current)
                current = []
        if current:
            batches.append(current)
        return batches

    def max_batch_size(self, cues: list[Cue]) -> int:
        """Return max batch size from average normalized text length."""
        if not cues:
            return 1
        avg_len = sum(len(cue.normalized_text) for cue in cues) / len(cues)
        if avg_len <= 50:
            return 8
        if avg_len <= 100:
            return 6
        if avg_len <= 200:
            return 4
        return 2

