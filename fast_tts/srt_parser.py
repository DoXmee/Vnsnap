from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Cue:
    """One SRT cue and all render metadata carried through the pipeline."""

    id: int
    start_ms: int
    end_ms: int
    duration_ms: int
    raw_text: str
    normalized_text: str = ""
    fingerprint: str = ""
    status: str = "pending"
    retry_count: int = 0
    retry_speed: float = 1.0
    tail_pad_ms: int = 80
    degraded_reason: str = ""
    asr_score: float = 0.0
    duration_ratio: float = 0.0
    tail_issue: bool = False
    tail_reason: str = ""
    output_wav: str = ""
    output_duration: float = 0.0
    force_single: bool = False


class SRTParser:
    """Parse an SRT file into Cue objects with millisecond timing."""

    TIME_RE = re.compile(
        r"(?P<h>\d{1,2}):(?P<m>[0-5]?\d):(?P<s>[0-5]?\d)[,.](?P<ms>\d{1,3})"
    )
    TIMING_RE = re.compile(
        r"^\s*(?P<left>\d{1,2}:[0-5]?\d:[0-5]?\d[,.]\d{1,3})\s*-->\s*"
        r"(?P<right>\d{1,2}:[0-5]?\d:[0-5]?\d[,.]\d{1,3}).*$",
        re.MULTILINE,
    )

    def parse(self, path: Path) -> list[Cue]:
        """Read `path` and return cues; malformed blocks are skipped."""
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
        text = raw.replace("\r\n", "\n").replace("\r", "\n")
        matches = list(self.TIMING_RE.finditer(text))
        rows: list[tuple[int, int, str]] = []
        for index, match in enumerate(matches):
            try:
                start_ms = self.parse_time_ms(match.group("left"))
                end_ms = self.parse_time_ms(match.group("right"))
            except ValueError:
                continue
            if end_ms <= start_ms:
                continue
            body_start = match.end()
            body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            lines = [line.strip() for line in text[body_start:body_end].split("\n") if line.strip()]
            while lines and lines[-1].isdigit():
                lines.pop()
            cue_text = re.sub(r"\s+", " ", " ".join(lines)).strip()
            if cue_text:
                rows.append((start_ms, end_ms, cue_text))

        rows.sort(key=lambda row: row[0])
        cues: list[Cue] = []
        last_end = 0
        for idx, (start_ms, end_ms, cue_text) in enumerate(rows, start=1):
            if start_ms < last_end:
                start_ms = last_end
            if end_ms <= start_ms:
                continue
            cues.append(Cue(idx, start_ms, end_ms, end_ms - start_ms, cue_text))
            last_end = end_ms
        return cues

    def parse_time_ms(self, value: str) -> int:
        """Convert SRT timestamp text into milliseconds."""
        match = self.TIME_RE.search(value)
        if not match:
            raise ValueError(f"Invalid SRT timestamp: {value}")
        h = int(match.group("h"))
        m = int(match.group("m"))
        s = int(match.group("s"))
        ms = int((match.group("ms") + "000")[:3])
        return ((h * 60 + m) * 60 + s) * 1000 + ms
