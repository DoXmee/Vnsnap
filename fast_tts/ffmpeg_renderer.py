from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .logging_utils import logger
from .timeline_assembler import TimelineItem


class FFmpegRenderer:
    """Render timeline items into a final MP3 using FFmpeg filter_complex."""

    def __init__(self, ffmpeg: Path, chunk_size: int = 80) -> None:
        """Create renderer with FFmpeg executable and chunk size for Windows safety."""
        self.ffmpeg = ffmpeg
        self.chunk_size = chunk_size

    def render(self, items: list[TimelineItem], output: Path, bitrate: str = "320k") -> None:
        """Render all timeline items to `output`; long jobs are mixed in chunks."""
        output.parent.mkdir(parents=True, exist_ok=True)
        if not items:
            raise RuntimeError("No timeline items to render")
        with tempfile.TemporaryDirectory(prefix="fast_tts_mix_") as td:
            tmp = Path(td)
            chunk_outputs: list[Path] = []
            for chunk_index, start in enumerate(range(0, len(items), self.chunk_size), start=1):
                chunk = items[start : start + self.chunk_size]
                chunk_out = tmp / f"chunk_{chunk_index:05d}.wav"
                self._render_chunk(chunk, chunk_out)
                chunk_outputs.append(chunk_out)
            if len(chunk_outputs) == 1:
                self._post_process(chunk_outputs[0], output, bitrate)
            else:
                merged = tmp / "merged.wav"
                self._render_chunk(
                    [
                        TimelineItem(item.cue, path, 0, item.duration_ms)
                        for item, path in zip(items[: len(chunk_outputs)], chunk_outputs)
                    ],
                    merged,
                )
                self._post_process(merged, output, bitrate)
        logger.info(f"FFmpeg render done: {output}")

    def _render_chunk(self, items: list[TimelineItem], output: Path) -> None:
        """Mix one chunk to WAV with adelay and amix."""
        cmd = [str(self.ffmpeg), "-y", "-hide_banner", "-loglevel", "error"]
        for item in items:
            cmd += ["-i", str(item.wav_path)]
        filters: list[str] = []
        labels: list[str] = []
        for i, item in enumerate(items):
            label = f"a{i}"
            delay = max(0, int(item.start_ms))
            chain = f"[{i}:a]"
            if 0.92 <= item.stretch_ratio <= 1.08 and abs(item.stretch_ratio - 1.0) > 0.01:
                tempo = 1.0 / item.stretch_ratio
                chain += f"atempo={tempo:.5f},"
            chain += f"adelay={delay}|{delay}[{label}]"
            filters.append(chain)
            labels.append(f"[{label}]")
        filters.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.95[out]"
        )
        cmd += ["-filter_complex", ";".join(filters), "-map", "[out]", "-c:a", "pcm_s16le", str(output)]
        subprocess.run(cmd, check=True)

    def _post_process(self, wav: Path, output: Path, bitrate: str) -> None:
        """Apply loudnorm and true peak limiting before MP3 export."""
        cmd = [
            str(self.ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(wav),
            "-af",
            "loudnorm=I=-16:TP=-1:LRA=11,alimiter=limit=0.95",
            "-b:a",
            bitrate,
            str(output),
        ]
        subprocess.run(cmd, check=True)
