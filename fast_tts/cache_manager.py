from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .logging_utils import logger
from .srt_parser import Cue

try:
    from cachetools import LRUCache
except Exception:  # pragma: no cover - optional dependency fallback
    LRUCache = dict  # type: ignore


@dataclass
class CacheMetadata:
    """Metadata stored in SQLite for one cached cue WAV."""

    fingerprint: str
    normalized_text: str
    voice_settings: dict[str, Any]
    sample_rate: int
    duration_sec: float
    status: str = "fast"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreloadResult:
    """Result of a batch cache lookup before inference."""

    hits: list[Cue] = field(default_factory=list)
    misses: list[Cue] = field(default_factory=list)
    tier_breakdown: dict[str, int] = field(
        default_factory=lambda: {"tier1_ram": 0, "tier2_sqlite": 0, "tier3_disk": 0}
    )


class CacheManager:
    """FAST-TTS v4 3-tier cache: RAM LRU, SQLite WAL index, and disk WAV files."""

    def __init__(self, cache_dir: Path, voice_settings: dict[str, Any], text_norm_version: str) -> None:
        """Create or open a persistent per-voice cache directory."""
        self.cache_dir = cache_dir
        self.voice_settings = voice_settings
        self.text_norm_version = text_norm_version
        self.wav_dir = self.cache_dir / "wavs"
        self.db_path = self.cache_dir / "index.db"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.wav_dir.mkdir(parents=True, exist_ok=True)
        self.ram: Any = LRUCache(maxsize=2048) if LRUCache is not dict else {}
        self.tier_breakdown = {"tier1_ram": 0, "tier2_sqlite": 0, "tier3_disk": 0}
        self._pending_rows: list[tuple[Any, ...]] = []
        self._connect()

    def _connect(self) -> None:
        """Open SQLite, enable WAL, and create schema."""
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_entries (
                fingerprint TEXT PRIMARY KEY,
                wav_path TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                voice_settings_json TEXT NOT NULL,
                sample_rate INTEGER NOT NULL,
                duration_sec REAL NOT NULL,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_used_at REAL NOT NULL
            )
            """
        )
        self.conn.commit()

    def build_fingerprint(self, cue: Cue, settings: dict[str, Any] | None = None) -> str:
        """Build SHA256 from text and voice settings only; timing is never included."""
        settings = settings or self.voice_settings
        payload = "|".join(
            [
                cue.normalized_text,
                str(settings.get("voice_id", "")),
                str(cue.retry_speed or settings.get("speed", 1.0)),
                str(settings.get("pitch", "")),
                str(settings.get("emotion", "")),
                str(settings.get("merged_model_hash", "")),
                str(settings.get("ref_codes_hash", "")),
                self.text_norm_version,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def fingerprint(self, cue: Cue) -> str:
        """Backward-compatible alias for build_fingerprint()."""
        return self.build_fingerprint(cue)

    def paths(self, fingerprint: str) -> tuple[Path, Path]:
        """Return WAV and sidecar JSON paths for a fingerprint."""
        folder = self.wav_dir / fingerprint[:2]
        return folder / f"{fingerprint}.wav", folder / f"{fingerprint}.json"

    def preload_all(self, cues: list[Cue]) -> PreloadResult:
        """Batch query SQLite once for all cues and mark cache hits in place."""
        result = PreloadResult(tier_breakdown=self.tier_breakdown.copy())
        for cue in cues:
            cue.fingerprint = cue.fingerprint or self.build_fingerprint(cue)
        wanted = [cue.fingerprint for cue in cues]
        row_by_fp: dict[str, tuple[str, str, float]] = {}
        if wanted:
            for start in range(0, len(wanted), 900):
                chunk = wanted[start : start + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows = self.conn.execute(
                    f"SELECT fingerprint,wav_path,duration_sec FROM cache_entries WHERE fingerprint IN ({placeholders})",
                    chunk,
                ).fetchall()
                row_by_fp.update({str(row[0]): (str(row[1]), str(row[0]), float(row[2])) for row in rows})

        now = time.time()
        for cue in cues:
            fp = cue.fingerprint
            wav = None
            if fp in self.ram:
                wav = self.paths(fp)[0]
                self.tier_breakdown["tier1_ram"] += 1
            elif fp in row_by_fp:
                wav = Path(row_by_fp[fp][0])
                if wav.exists() and wav.stat().st_size > 1024:
                    self.tier_breakdown["tier2_sqlite"] += 1
                    try:
                        data, sr = sf.read(str(wav), dtype="float32")
                        self.ram[fp] = (data, sr)
                    except Exception:
                        pass
                    self.conn.execute("UPDATE cache_entries SET last_used_at=? WHERE fingerprint=?", (now, fp))
                else:
                    wav = None
            if wav:
                cue.output_wav = str(wav)
                cue.status = "cached"
                result.hits.append(cue)
            else:
                result.misses.append(cue)
        self.conn.commit()
        result.tier_breakdown = self.tier_breakdown.copy()
        logger.info(f"Cache preload: hits={len(result.hits)} misses={len(result.misses)}")
        return result

    def get(self, cue_or_fp: Cue | str) -> Path | None:
        """Lookup one cue/fingerprint and return cached WAV path when valid."""
        if isinstance(cue_or_fp, Cue):
            cue = cue_or_fp
            cue.fingerprint = cue.fingerprint or self.build_fingerprint(cue)
            fp = cue.fingerprint
        else:
            cue = None
            fp = cue_or_fp
        if fp in self.ram:
            wav = self.paths(fp)[0]
            if wav.exists():
                if cue:
                    cue.output_wav = str(wav)
                    cue.status = "cached"
                self.tier_breakdown["tier1_ram"] += 1
                return wav
        row = self.conn.execute("SELECT wav_path FROM cache_entries WHERE fingerprint=?", (fp,)).fetchone()
        if not row:
            return None
        wav = Path(str(row[0]))
        if not wav.exists() or wav.stat().st_size <= 1024:
            return None
        try:
            data, sr = sf.read(str(wav), dtype="float32")
            self.ram[fp] = (data, sr)
        except Exception:
            pass
        if cue:
            cue.output_wav = str(wav)
            cue.status = "cached"
        self.tier_breakdown["tier2_sqlite"] += 1
        return wav

    def save(
        self,
        fingerprint: str,
        wav: np.ndarray,
        sample_rate: int,
        metadata: CacheMetadata,
    ) -> Path:
        """Save audio to WAV, SQLite, and RAM LRU."""
        wav_path, sidecar = self.paths(fingerprint)
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        audio = np.asarray(wav, dtype=np.float32)
        sf.write(str(wav_path), audio, sample_rate)
        self.ram[fingerprint] = (audio, sample_rate)
        duration = len(audio) / max(1, sample_rate)
        row = (
            fingerprint,
            str(wav_path),
            metadata.normalized_text,
            json.dumps(metadata.voice_settings, ensure_ascii=False),
            sample_rate,
            duration,
            metadata.status,
            json.dumps(asdict(metadata), ensure_ascii=False),
            time.time(),
            time.time(),
        )
        self._pending_rows.append(row)
        sidecar.write_text(json.dumps(asdict(metadata), ensure_ascii=False, indent=2), encoding="utf-8")
        if len(self._pending_rows) >= 100:
            self.flush()
        return wav_path

    def put(self, cue: Cue, wav_path: Path, extra: dict[str, Any] | None = None) -> Path:
        """Backward-compatible helper: cache an existing WAV file for a cue."""
        data, sr = sf.read(str(wav_path), dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        cue.fingerprint = cue.fingerprint or self.build_fingerprint(cue)
        metadata = CacheMetadata(
            fingerprint=cue.fingerprint,
            normalized_text=cue.normalized_text,
            voice_settings=self.voice_settings,
            sample_rate=sr,
            duration_sec=len(data) / max(1, sr),
            status=cue.status,
            extra=extra or {},
        )
        dst = self.save(cue.fingerprint, data, sr, metadata)
        self.flush()
        cue.output_wav = str(dst)
        return dst

    def flush(self) -> None:
        """Commit pending SQLite cache inserts."""
        if not self._pending_rows:
            return
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO cache_entries
            (fingerprint,wav_path,normalized_text,voice_settings_json,sample_rate,duration_sec,status,metadata_json,created_at,last_used_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            self._pending_rows,
        )
        self.conn.commit()
        self._pending_rows.clear()

    def close(self) -> None:
        """Flush and close SQLite resources."""
        self.flush()
        self.conn.close()
