from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "vieneu_work" / "finetune_dataset" / "thanh_thao_vieneu_v4_combined_v2_hanhan"
DEFAULT_OUTPUT = ROOT / "vieneu_work" / "finetune_dataset" / "thanh_thao_reclone_clean_v1_20260605"


FILLER_RE = re.compile(
    r"(^|\s)(ừm|ưm|ờ|ừ|ừm+|ờm|um|uhm|uh|hm|hmm|à|ơ|ơm)(\s|,|\.|!|\?|$)",
    re.IGNORECASE,
)
LEADING_EXPRESSION_RE = re.compile(r"^\s*(á|oa|ha\s+ha|hahaha|hà\s+hà|ôi|ơ)\b", re.IGNORECASE)
BAD_RE = re.compile(r"https?://|www\.|@|#|[A-Za-z]{4,}")
ODD_PUNCT_RE = re.compile(r"([?!]){2,}|\.{4,}|[,;:]{3,}")
END_PUNCT_RE = re.compile(r"[.!?…]$")


def split_encoded_line(line: str) -> tuple[str, str, str] | None:
    parts = line.rstrip("\n").split("|", 2)
    if len(parts) != 3:
        return None
    filename, text, codes_json = parts
    try:
        codes = json.loads(codes_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(codes, list) or len(codes) < 30:
        return None
    return filename, normalize_spaces(text), json.dumps(codes, ensure_ascii=False)


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def reject_reason(text: str, min_chars: int, max_chars: int) -> str | None:
    if len(text) < min_chars:
        return "too_short"
    if len(text) > max_chars:
        return "too_long"
    if FILLER_RE.search(text):
        return "filler"
    if LEADING_EXPRESSION_RE.search(text):
        return "leading_expression"
    if BAD_RE.search(text):
        return "bad_token"
    if ODD_PUNCT_RE.search(text):
        return "odd_punct"
    digit_ratio = sum(ch.isdigit() for ch in text) / max(len(text), 1)
    if digit_ratio > 0.18:
        return "too_many_digits"
    if text.count(",") + text.count(".") + text.count("?") + text.count("!") > 6:
        return "too_many_clauses"
    if not END_PUNCT_RE.search(text):
        return "no_tail_punct"
    return None


def make_clean_dataset(source_dir: Path, output_dir: Path, min_chars: int, max_chars: int, max_samples: int | None) -> None:
    source_metadata = source_dir / "metadata_encoded.csv"
    if not source_metadata.exists():
        raise FileNotFoundError(source_metadata)

    output_dir.mkdir(parents=True, exist_ok=True)
    accepted: list[str] = []
    rejected = Counter()
    total = 0

    for line in source_metadata.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        total += 1
        parsed = split_encoded_line(line)
        if not parsed:
            rejected["parse_or_codes"] += 1
            continue
        filename, text, codes_json = parsed
        reason = reject_reason(text, min_chars=min_chars, max_chars=max_chars)
        if reason:
            rejected[reason] += 1
            continue
        accepted.append(f"{filename}|{text}|{codes_json}\n")
        if max_samples and len(accepted) >= max_samples:
            break

    if len(accepted) < 500:
        raise RuntimeError(f"Clean dataset too small: {len(accepted)} accepted from {total}")

    (output_dir / "metadata_encoded.csv").write_text("".join(accepted), encoding="utf-8")
    report_lines = [
        f"source_dir={source_dir.resolve()}",
        f"source_metadata={source_metadata.resolve()}",
        f"accepted={len(accepted)}",
        f"total_seen={total}",
        f"min_chars={min_chars}",
        f"max_chars={max_chars}",
        f"max_samples={max_samples or ''}",
        "rejected=" + json.dumps(dict(rejected), ensure_ascii=False, sort_keys=True),
        "note=Clean text-only encoded dataset for fresh Thanh Thao reclone. Raw audio is intentionally not copied.",
    ]
    (output_dir / "report.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"output={output_dir.resolve()}")
    print(f"accepted={len(accepted)} total_seen={total}")
    print(f"rejected={dict(rejected)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a strict clean encoded dataset for fresh VieNeu LoRA training.")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--min-chars", type=int, default=18)
    parser.add_argument("--max-chars", type=int, default=170)
    parser.add_argument("--max-samples", type=int, default=None)
    args = parser.parse_args()
    make_clean_dataset(args.source_dir.resolve(), args.output_dir.resolve(), args.min_chars, args.max_chars, args.max_samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
