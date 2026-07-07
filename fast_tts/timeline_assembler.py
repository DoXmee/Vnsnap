from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .logging_utils import logger
from .srt_parser import Cue

TAIL_PAD_MS = 80
STRETCH_MAX = 1.08
STRETCH_RETRY = 1.20
CROSSFADE_MS = 15
GAP_BORROW_MIN = 200


@dataclass
class TimelineItem:
    """A rendered audio item placed on the SRT timeline."""

    cue: Cue
    wav_path: Path
    start_ms: int
    duration_ms: int
    stretch_ratio: float = 1.0
    needs_retry: bool = False
    pad_ms: int = 0


class TimelineAssembler:
    """Prepare cue WAVs for timeline placement while never trimming final audio."""

    def assemble(self, cues: list[Cue]) -> list[TimelineItem]:
        """Return timeline items with mandatory tail pad and retry flags for risky cues."""
        items: list[TimelineItem] = []
        ordered = sorted(cues, key=lambda c: c.start_ms)
        for idx, cue in enumerate(ordered):
            wav = Path(cue.output_wav)
            if not wav.exists():
                cue.status = "degraded"
                cue.degraded_reason = cue.degraded_reason or "missing output wav"
                continue
            padded = self.add_tail_pad_file(wav, cue.tail_pad_ms or TAIL_PAD_MS)
            audio_ms = self.audio_duration_ms(wav)
            slot_ms = max(1, cue.duration_ms)
            ratio = audio_ms / slot_ms
            item = TimelineItem(cue, padded, cue.start_ms, audio_ms + (cue.tail_pad_ms or TAIL_PAD_MS), ratio)
            cue.duration_ratio = ratio
            if ratio <= STRETCH_MAX:
                item.stretch_ratio = ratio
                if ratio < 1.0:
                    item.pad_ms = max(0, slot_ms - audio_ms) + (cue.tail_pad_ms or TAIL_PAD_MS)
            elif ratio <= STRETCH_RETRY:
                next_cue = ordered[idx + 1] if idx + 1 < len(ordered) else None
                gap_ms = max(0, next_cue.start_ms - cue.end_ms) if next_cue else 0
                if gap_ms >= GAP_BORROW_MIN:
                    item.pad_ms = cue.tail_pad_ms or TAIL_PAD_MS
                else:
                    item.needs_retry = True
                    logger.warning(f"cue={cue.id} needs speed retry ratio={ratio:.2f} gap={gap_ms}ms")
            else:
                item.needs_retry = True
                logger.warning(f"cue={cue.id} too long for slot ratio={ratio:.2f}")
            items.append(item)
        return items

    def audio_duration_ms(self, path: Path) -> int:
        """Return WAV duration in milliseconds."""
        info = sf.info(str(path))
        return int(round(info.frames * 1000 / info.samplerate))

    def add_tail_pad_file(self, path: Path, pad_ms: int = TAIL_PAD_MS) -> Path:
        """Create a padded WAV beside the source; this never removes source audio."""
        out = path.with_name(path.stem + f"_tailpad{pad_ms}.wav")
        if out.exists() and out.stat().st_mtime >= path.stat().st_mtime:
            return out
        data, sr = sf.read(str(path), dtype="float32")
        if data.ndim > 1:
            pad_shape = (int(pad_ms * sr / 1000), data.shape[1])
        else:
            pad_shape = (int(pad_ms * sr / 1000),)
        padded = np.concatenate([data, np.zeros(pad_shape, dtype=np.float32)])
        sf.write(str(out), padded, sr)
        return out
