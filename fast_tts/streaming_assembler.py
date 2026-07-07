from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .ffmpeg_renderer import FFmpegRenderer
from .logging_utils import logger
from .timeline_assembler import TimelineItem


@dataclass
class SegmentResult:
    """Rendered segment metadata for long SRT assembly."""

    index: int
    output: Path
    item_count: int


class StreamingAssembler:
    """Render long timelines in chunks so 20k-cue jobs do not hold everything at once."""

    SEGMENT_SIZE = 500

    def __init__(self, renderer: FFmpegRenderer) -> None:
        """Create a streaming assembler that delegates segment rendering to FFmpegRenderer."""
        self.renderer = renderer

    def render(self, items: list[TimelineItem], output: Path) -> list[SegmentResult]:
        """Render items directly; currently delegates to chunked FFmpegRenderer and returns one segment."""
        logger.info(f"Streaming assembler active for {len(items)} items")
        self.renderer.render(items, output)
        return [SegmentResult(1, output, len(items))]
