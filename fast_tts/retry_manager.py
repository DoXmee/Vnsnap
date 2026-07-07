from __future__ import annotations

import re

from .logging_utils import logger
from .srt_parser import Cue


class RetryManager:
    """Apply deterministic retry mutations and mark irrecoverable cues degraded."""

    def __init__(self, max_retry: int = 4) -> None:
        """Create retry manager with a hard per-cue retry limit."""
        self.max_retry = max_retry

    def next_tail_retry(self, cue: Cue, reason: str) -> bool:
        """Retry a clipped final tail by increasing tail padding before any ASR gate."""
        cue.retry_count += 1
        cue.status = "tail_retry"
        cue.tail_issue = True
        cue.tail_reason = reason
        if cue.retry_count <= self.max_retry:
            cue.tail_pad_ms = max(cue.tail_pad_ms, 150)
            cue.force_single = True
            logger.warning(f"Tail retry cue={cue.id} tail_pad={cue.tail_pad_ms}ms reason={reason}")
            return True
        cue.status = "degraded"
        cue.degraded_reason = f"tail retry exhausted: {reason}"
        logger.warning(f"DEGRADED cue={cue.id}: {cue.degraded_reason}")
        return False

    def next_retry(self, cue: Cue, reason: str) -> bool:
        """Mutate cue for another render; return False when cue is degraded."""
        cue.retry_count += 1
        if cue.retry_count > self.max_retry:
            cue.status = "degraded"
            cue.degraded_reason = reason
            logger.warning(f"DEGRADED cue={cue.id}: {reason}")
            return False
        if cue.retry_count == 1:
            cue.normalized_text = self.stabilize_punctuation(cue.normalized_text)
            logger.warning(f"Retry cue={cue.id} punctuation reason={reason}")
            return True
        if cue.retry_count == 2:
            cue.retry_speed = 0.9
            logger.warning(f"Retry cue={cue.id} speed=0.9 reason={reason}")
            return True
        if cue.retry_count == 3:
            cue.force_single = True
            logger.warning(f"Retry cue={cue.id} force_single reason={reason}")
            return True
        cue.status = "degraded"
        cue.degraded_reason = reason
        logger.warning(f"DEGRADED cue={cue.id}: {reason}")
        return False

    def stabilize_punctuation(self, text: str) -> str:
        """Add stable punctuation to reduce TTS run-on failures."""
        text = re.sub(r"\s+", " ", text).strip()
        if text and not re.search(r"[.!?]$", text):
            text += "."
        return text
