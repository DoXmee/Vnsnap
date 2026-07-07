from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .srt_parser import Cue


@dataclass
class TextSafetyResult:
    """Safety status for one normalized text string."""

    ok: bool
    flags: list[str] = field(default_factory=list)


class TextNormalizer:
    """Normalize Vietnamese cue text for stable TTS and cache identity."""

    version = "vi_norm_v1.3_tail"

    DIGITS = {
        0: "không",
        1: "một",
        2: "hai",
        3: "ba",
        4: "bốn",
        5: "năm",
        6: "sáu",
        7: "bảy",
        8: "tám",
        9: "chín",
    }

    def normalize_cues(self, cues: list[Cue]) -> list[Cue]:
        """Normalize all cue texts in place and return the same list."""
        for cue in cues:
            cue.normalized_text = self.enforce_text_tail(self.normalize(cue.raw_text))
        return cues

    def normalize(self, text: str) -> str:
        """Return normalized Vietnamese text with safe spacing and punctuation."""
        text = self.repair_mojibake(text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"\blàm\s*điêu\b", "làm điêu", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+([,.!?;:])", r"\1", text)
        text = re.sub(r"([,.!?;:])(?=\S)", r"\1 ", text)
        text = re.sub(r"\b\d{1,3}\b", lambda m: self.number_under_1000(int(m.group(0))), text)
        return unicodedata.normalize("NFC", text).strip()

    def enforce_text_tail(self, text: str) -> str:
        """Add only ending punctuation hints so VieNeu finishes the final word cleanly."""
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return text
        word_count = len(re.findall(r"\w+", text, flags=re.UNICODE))
        if not re.search(r"[.!?…]$", text):
            text += "."
        if word_count <= 3 and text.endswith(".") and not text.endswith("..."):
            text = text[:-1].rstrip() + "..."
        return text

    def safety_flags(self, text: str) -> list[str]:
        """Return warnings for very short, very long, or suspicious text."""
        return self.check_text_safety(text).flags

    def check_text_safety(self, text: str) -> TextSafetyResult:
        """Return structured flags for text that may be risky for TTS."""
        flags: list[str] = []
        if len(text.strip()) < 3:
            flags.append("too_short")
        if len(text) > 300:
            flags.append("too_long")
        if re.search(r"[\uFFFD\x00-\x08\x0b\x0c\x0e-\x1f]", text):
            flags.append("bad_chars")
        return TextSafetyResult(not flags, flags)

    def repair_mojibake(self, text: str) -> str:
        """Fix common UTF-8-as-Latin mojibake when recoverable."""
        markers = ("Ã", "Ä", "Æ", "áº", "á»")
        if not any(marker in text for marker in markers):
            return text
        for encoding in ("latin1", "cp1252"):
            try:
                fixed = text.encode(encoding, errors="strict").decode("utf-8", errors="strict")
            except Exception:
                continue
            if sum(fixed.count(marker) for marker in markers) < sum(text.count(marker) for marker in markers):
                return fixed
        return text

    def number_under_1000(self, value: int) -> str:
        """Speak a non-negative integer under 1000 in Vietnamese."""
        if value < 10:
            return self.DIGITS[value]
        if value < 100:
            tens, ones = divmod(value, 10)
            base = "mười" if tens == 1 else f"{self.DIGITS[tens]} mươi"
            if ones == 0:
                return base
            if ones == 1 and tens > 1:
                return f"{base} mốt"
            if ones == 5:
                return f"{base} lăm"
            return f"{base} {self.DIGITS[ones]}"
        hundreds, rest = divmod(value, 100)
        base = f"{self.DIGITS[hundreds]} trăm"
        if rest == 0:
            return base
        if rest < 10:
            return f"{base} lẻ {self.DIGITS[rest]}"
        return f"{base} {self.number_under_1000(rest)}"
