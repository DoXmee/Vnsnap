from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "local_vieneu" / "venv" / "Scripts" / "python.exe"
EXTRACT_SCRIPT = ROOT / "tools" / "extract_srt_from_video.py"

APP_MODEL_CACHE = ROOT / "model_cache"
os.environ.setdefault("HF_HOME", str(APP_MODEL_CACHE / "huggingface"))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(APP_MODEL_CACHE / "huggingface" / "hub"))
os.environ.setdefault("TRANSFORMERS_CACHE", str(APP_MODEL_CACHE / "huggingface" / "transformers"))
os.environ.setdefault("TORCH_HOME", str(APP_MODEL_CACHE / "torch"))
os.environ.setdefault("XDG_CACHE_HOME", str(APP_MODEL_CACHE))
os.environ.setdefault("MODELSCOPE_CACHE", str(APP_MODEL_CACHE / "modelscope"))
os.environ.setdefault("PADDLE_HOME", str(APP_MODEL_CACHE / "paddle"))
os.environ.setdefault("PADDLEOCR_HOME", str(APP_MODEL_CACHE / "paddleocr"))
for _cache_key in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE", "TORCH_HOME", "MODELSCOPE_CACHE", "PADDLE_HOME", "PADDLEOCR_HOME"):
    try:
        Path(os.environ[_cache_key]).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


BUILTIN_ZH_CORRECTIONS: dict[str, str] = {
    # Medical / hospital shorts.
    "急性蓝尾炎": "急性阑尾炎",
    "蓝尾炎": "阑尾炎",
    "截肠外科": "结肠外科",
    "血肠规": "血常规",
    "白细胞技术": "白细胞计数",
    "百样显影": "靶样显影",
    "真相": "征象",
    "卖试点": "麦氏点",
    "SC2座": "SCI二作",
    "SC二座": "SCI二作",
    "大一系统": "大医系统",
    "踏施级荷包缝合树": "大师级荷包缝合术",
    "大师级荷包缝合树": "大师级荷包缝合术",
    "机械剂": "机械记忆",
    "给许秋煮刀": "给许秋主刀",
    "低年资主之一": "低年资主治医",
    "低年资主治之一": "低年资主治医",
    "能力：弹舞": "能力：暂无",
    "能力:弹舞": "能力：暂无",
    "牵到": "签到",
    # Common Chinese short-video homophones.
    "一脸猛逼": "一脸懵逼",
    "导霉蛋": "倒霉蛋",
    "滑开了肚皮": "划开了肚皮",
    "爆汗被淘汰": "抱憾被淘汰",
    # Daoist / supernatural shorts.
    "我鸡尘借其安身": "一魄寄尘，借气安身",
    "音破": "魂魄",
    "师姑": "施孤",
    "受数": "寿数",
    "储物": "主顾",
    "请执行": "请直行",
}

# Extra terms are written with escapes to avoid Windows console/file-encoding drift.
BUILTIN_ZH_CORRECTIONS.update(
    {
        "\u80fd\u529b\u5927\u4e94": "\u80fd\u529b\u6682\u65e0",
        "\u5956\u52b1\u5434\u5609\u8bda": "\u5956\u52b1\u65e0\u52a0\u6210",
        "\u8e0f\u5e08\u7ea7": "\u5927\u5e08\u7ea7",
        "\u516b\u5341\u7ea7": "\u5927\u5e08\u7ea7",
        "\u65b0\u624b\u8e0f\u793c\u5305\u6210\u8863": "\u65b0\u624b\u5927\u793c\u5305*1",
        "\u526f\u4e3b\u4f55\u6d77": "\u526f\u4e3b\u4efb\u4f55\u6d77",
        "\u4e3b\u5bfc\u5012\u4e0b\u4e86": "\u4e3b\u5200\u5012\u4e0b\u4e86",
        "\u4e00\u6ce8\u76f4\u63a5\u8e72\u4e86\u4e0b\u6765": "\u4e00\u52a9\u76f4\u63a5\u8e72\u4e86\u4e0b\u6765",
        "\u65e0\u636e\u539f\u5219": "\u65e0\u83cc\u539f\u5219",
        "\u9888\u52a8\u8109\u6478\u6478": "\u9888\u52a8\u8109\u8109\u640f",
        "\u6237\u5916": "\u666e\u5916",
        "\u5343\u9053\u5730\u70b9": "\u7b7e\u5230\u5730\u70b9",
        "\u5343\u9053\u6210\u529f": "\u7b7e\u5230\u6210\u529f",
    }
)

SUMMARY_HALLUCINATION_MARKERS = [
    "\u4e3b\u89d2",  # main character
    "\u7a7f\u8d8a\u5230",  # transmigrated to
    "\u6545\u4e8b\u8bb2\u8ff0",
    "\u5267\u60c5",
    "\u8fd9\u90e8",
    "\u672c\u96c6",
    "\u666e\u901a\u533b\u751f\u8eab\u4e0a",
]

AI_INTERNAL_LEAK_MARKERS = [
    "\u53ef\u80fd\u4e3a\u8bef\u542c",  # likely misheard
    "\u6216\u9519\u8bd1",
    "\u5b9e\u9645\u5e94\u4e3a",
    "\u67d0\u4f4d\u89d2\u8272\u7684\u540d\u5b57",
    "\u7cfb\u7edf\u540d\u79f0",
    "\u4e0d\u786e\u5b9a",
    "\u9700\u8981\u6839\u636e\u4e0a\u4e0b\u6587",
]

BUILTIN_ZH_CORRECTIONS.update(
    {
        "\u817e\u6655\u8fc7\u53bb": "\u75bc\u6655\u8fc7\u53bb",
        "\u6253\u628a\u624b": "\u642d\u628a\u624b",
        "\u5e8a\u65c1\u8d85\u5347": "\u5e8a\u65c1\u8d85\u58f0",
        "\u81f3\u50f5\u786c": "\u8d28\u50f5\u786c",
        "\u540c\u6027\u539f\u5f0f": "\u540c\u5fc3\u5706\u4f3c",
        "\u59d7\u59d7\u6765\u5403": "\u59d7\u59d7\u6765\u8fdf",
        "\u4f34\u968f\u7740\u533b\u751f\u5de8\u54cd": "\u4f34\u968f\u7740\u4e00\u58f0\u5de8\u54cd",
        "\u5931\u620f\u751f": "\u5b9e\u4e60\u751f",
        "\u624b\u672f\u9002\u4e2d": "\u624b\u672f\u5ba4\u4e2d",
        "\u6362\u624b\u672f\u533b": "\u6362\u624b\u672f\u8863",
        "\u7981\u97f3": "\u6d78\u6deb",
        "\u65b0\u624b\u5927\u793c\u5305\u6210\u8863": "\u65b0\u624b\u5927\u793c\u5305*1",
        "\u6570\u524d\u6d88\u6bd2": "\u672f\u524d\u6d88\u6bd2",
        "\u6bd5\u7ade": "\u6bd5\u7adf",
        "\u8138\u82e6\u7b11": "\u4e00\u8138\u82e6\u7b11",
        "\u5de1\u53e3\u62a4\u58eb": "\u5de1\u56de\u62a4\u58eb",
        "\u7ef4 \u5feb\u9001\u53bb\u533b\u9662": "\u5feb\uff0c\u5feb\u9001\u53bb\u533b\u9662",
        "\u8fd9\u65f6\u5668\u68b0 \u4e86\u51fa\u6765": "\u8fd9\u65f6\u5668\u68b0\u62a4\u58eb\u7ad9\u4e86\u51fa\u6765",
        "\u52a9\u76f4\u63a5\u8e72\u4e86\u4e0b\u6765": "\u4e00\u52a9\u76f4\u63a5\u8e72\u4e86\u4e0b\u6765",
        "\u5927\u5e08\u7ea7\u9611\u5c3e\u708e\u5207\u9664\u672f*": "\u5927\u5e08\u7ea7\u9611\u5c3e\u708e\u5207\u9664\u672f*1",
        "\u9886\u53d6 \u968f\u540e\u8bb8\u79cb\u53c8\u9886\u53d6\u7684\u65b0\u624b\u4efb\u52a1": "\u968f\u540e\u8bb8\u79cb\u53c8\u9886\u53d6\u4e86\u65b0\u624b\u4efb\u52a1",
        "\u8d8a\u9ad8 \u5219\u5f71\u54cd\u56e0\u5b50\u8d8a\u9ad8": "\u5219\u5f71\u54cd\u56e0\u5b50\u8d8a\u9ad8",
        "\u8d8a\u9ad8 \u7b7e\u5230\u6240\u83b7\u5f97\u7684\u5956\u52b1\u5c31\u8d8a\u73cd\u7a00": "\u7b7e\u5230\u6240\u83b7\u5f97\u7684\u5956\u52b1\u5c31\u8d8a\u73cd\u7a00",
        "\u5934\u8111\u53d1\u70ed\u53d1\u80c0\u00b7": "\u5934\u8111\u53d1\u70ed\u53d1\u80c0",
        "\u4f60\u4e0d\u8981\u6363\u4e71\u00b7 \u00b7\u7b49\u6211\u8bf7\u793a\u4e0a\u7ea7\u533b\u751f": "\u4f60\u4e0d\u8981\u6363\u4e71...\u7b49\u6211\u8bf7\u793a\u4e0a\u7ea7\u533b\u751f",
        "\u80fd\u529b\uff1a\u6682\u65e0 \u80fd\u529b\uff1a\u6682\u5929": "\u80fd\u529b\uff1a\u6682\u65e0",
        "\u4efb\u52a1\u5217\u8868\uff1a \u5f85\u9886\u53d6\u4efb\u52a11": "\u4efb\u52a1\u5217\u8868\uff1a\u5f85\u9886\u53d6\u4efb\u52a11",
    }
)

OCR_NOISE_EXACT_PHRASES = {
    "\u62a4\u58eb\u7ad9",
    "\u6d3b\u5783\u573e",
    "\u6c49\u56de\u53ef",
    "\u5904\u56de\u53ef",
}

OCR_UI_ONLY_PATTERNS = [
    re.compile(r"^\s*(?:\u6bcf\u65e5\u7b7e\u5230[:\uff1a]\s*\u672a\u8fdb\u884c\s*)+$"),
    re.compile(r"^\s*(?:\u4efb\u52a1\u5217\u8868[:\uff1a]\s*\u5f85\u9886\u53d6\u4efb\u52a1\d+\s*)+$"),
    re.compile(r"^\s*(?:\u7ecf\u9a8c\u503c[:\uff1a]\s*\u96f6|\u80fd\u529b[:\uff1a]\s*(?:\u6682\u65e0|\u6682\u5929))\s*$"),
]

OCR_UI_INLINE_PATTERNS = [
    re.compile(r"\u6bcf\u65e5\u7b7e\u5230\s*[:\uff1a]\s*\u672a\u8fdb\u884c"),
    re.compile(r"\u4efb\u52a1\u5217\u8868\s*[:\uff1a]\s*\u5f85\u9886\u53d6\u4efb\u52a1\d+"),
    re.compile(r"\u7ecf\u9a8c\u503c\s*[:\uff1a]\s*\u96f6"),
    re.compile(r"\u80fd\u529b\s*[:\uff1a]\s*(?:\u6682\u65e0|\u6682\u5929)"),
]

OCR_CODE_PATTERNS = [
    re.compile(r"\d{1,3}\s*[:\uff1a.\-]\s*\d{2,5}"),
    re.compile(r"[01]{8,}(?:\s*\d{3,5})?"),
    re.compile(r"(?<![A-Za-z])\d{5,}(?![A-Za-z])"),
    re.compile(r"\s+\d{3,5}(?=\s|$)"),
    re.compile(r"(?<=\s)\d{1,4}(?=\s|$)"),
]


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def progress(percent: float, message: str) -> None:
    print(f"PROGRESS:{max(0, min(100, percent)):.1f}:{message}", flush=True)


def log(message: str) -> None:
    print(message, flush=True)


def fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def run_streaming(cmd: list[str], log_path: Path, start_pct: float, end_pct: float, stage: str) -> int:
    started = time.time()
    last_emit = 0.0
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] START {stage}\n")
        fh.write(" ".join(f'"{x}"' if " " in x else x for x in cmd) + "\n")
        extra_path = str(PY.parent) if PY.exists() else ""
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        if extra_path:
            env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
        env.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            fh.write(line + "\n")
            fh.flush()
            if line.startswith("PROGRESS:"):
                parts = line.split(":", 2)
                try:
                    inner = float(parts[1])
                except Exception:
                    inner = 0.0
                msg = parts[2] if len(parts) > 2 else stage
                pct = start_pct + (end_pct - start_pct) * (inner / 100.0)
                progress(pct, f"{stage}: {msg} | đã chạy {fmt(time.time() - started)}")
            elif time.time() - last_emit > 45:
                last_emit = time.time()
                progress(start_pct, f"{stage}: đang chạy | đã chạy {fmt(time.time() - started)}")
        code = proc.wait()
        fh.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] EXIT {stage}={code}\n")
        return code


def parse_srt_count(path: Path) -> int:
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8", errors="replace")
    return sum(1 for line in text.splitlines() if line.strip().isdigit())


def videocr_available() -> str:
    for name in ("videocr-cli.exe", "videocr-cli"):
        found = shutil.which(name)
        if found:
            return found

    roots: list[Path] = []
    for base in (ROOT, Path.cwd()):
        roots.append(base)
        roots.extend(base.parents)
    seen: set[Path] = set()
    for base in roots:
        if base in seen:
            continue
        seen.add(base)
        candidates = [
            base / "_repo_inspect" / "VideOCR" / "CLI" / "videocr_cli.py",
            base / "VideOCR" / "CLI" / "videocr_cli.py",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    return ""


def cjk_ratio(text: str) -> float:
    """Return ratio of CJK characters over non-space chars."""
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact:
        return 0.0
    cjk = len(re.findall(r"[\u3400-\u9fff]", compact))
    return cjk / max(1, len(compact))


def srt_quality_stats(path: Path) -> dict:
    """Compute lightweight SRT quality stats for source selection."""
    cues = parse_srt(path) if path.exists() else []
    durations: list[int] = []
    texts: list[str] = []
    leaks = 0
    summary_like = 0
    too_long_duration = 0
    too_long_text = 0
    for cue in cues:
        text = str(cue.get("text", "")).strip()
        start_ms, end_ms = cue_time_bounds(cue)
        dur_ms = max(0, end_ms - start_ms)
        durations.append(dur_ms)
        texts.append(text)
        if contains_ai_internal_leak(text):
            leaks += 1
        if any(marker in text for marker in SUMMARY_HALLUCINATION_MARKERS):
            summary_like += 1
        if dur_ms > 15_000:
            too_long_duration += 1
        if len(text) > 90:
            too_long_text += 1
    full_text = "".join(texts)
    return {
        "cues": len(cues),
        "chars": len(full_text),
        "cjk_ratio": round(cjk_ratio(full_text), 4),
        "leaks": leaks,
        "summary_like": summary_like,
        "too_long_duration": too_long_duration,
        "too_long_text": too_long_text,
        "avg_duration_ms": round(sum(durations) / max(1, len(durations)), 2),
    }


def is_usable_ocr_srt(path: Path, asr_stats: dict | None = None) -> tuple[bool, str, dict]:
    """Decide whether hard-sub OCR is strong enough to become final source."""
    stats = srt_quality_stats(path)
    if stats["cues"] < 3:
        return False, "too_few_cues", stats
    if stats["cjk_ratio"] < 0.55:
        return False, "low_cjk_ratio", stats
    if stats["leaks"]:
        return False, "unsafe_text", stats
    if stats["too_long_duration"] > max(2, stats["cues"] // 20):
        return False, "many_long_cues", stats
    if asr_stats and asr_stats.get("cues", 0) >= 20 and stats["cues"] < max(5, int(asr_stats["cues"] * 0.25)):
        return False, "too_sparse_vs_asr", stats
    return True, "ok", stats


def write_sanitized_final(input_srt: Path, out_srt: Path) -> dict:
    """Write a final SRT after deterministic correction and safety cleanup."""
    cues = parse_srt(input_srt)
    cues, map_changed = apply_phrase_corrections(cues, BUILTIN_ZH_CORRECTIONS)
    cues, safety_stats = safety_finalize_cues(cues)
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    write_srt(cues, out_srt)
    return {
        "source": str(input_srt),
        "map_changed": map_changed,
        "safety": safety_stats,
        "cues": len(cues),
    }


def time_overlap_ratio(a: dict, b: dict) -> float:
    """Return overlap ratio against the shorter cue duration."""
    a0, a1 = cue_time_bounds(a)
    b0, b1 = cue_time_bounds(b)
    inter = max(0, min(a1, b1) - max(a0, b0))
    short = max(1, min(max(1, a1 - a0), max(1, b1 - b0)))
    return inter / short


def is_suspicious_final_gap(prev_cue: dict | None, cue: dict, next_cue: dict | None) -> bool:
    """Detect a cue from source that likely disappeared from final."""
    start_ms, end_ms = cue_time_bounds(cue)
    dur_ms = end_ms - start_ms
    text = str(cue.get("text", "")).strip()
    if not text or dur_ms < 120 or dur_ms > 10_000:
        return False
    if cjk_ratio(text) < 0.45:
        return False
    if contains_ai_internal_leak(text):
        return False
    if compact_visible_text(text) in {compact_visible_text(str((prev_cue or {}).get("text", ""))), compact_visible_text(str((next_cue or {}).get("text", "")))}:
        return False
    return True


def has_nearby_text_duplicate(cue: dict, text: str, final_cues: list[dict], window_ms: int = 2600) -> bool:
    """Return true if nearby final cues already contain essentially the same text."""
    start_ms, end_ms = cue_time_bounds(cue)
    key = compact_visible_text(text)
    if not key:
        return True
    for final in final_cues:
        f0, f1 = cue_time_bounds(final)
        if f1 < start_ms - window_ms or f0 > end_ms + window_ms:
            continue
        fkey = compact_visible_text(str(final.get("text", "")))
        if not fkey:
            continue
        if key == fkey:
            return True
        shorter = min(len(key), len(fkey))
        longer = max(len(key), len(fkey))
        if shorter >= 5 and shorter / max(1, longer) >= 0.55 and (key in fkey or fkey in key):
            return True
    return False


def final_qa_repair(final_srt: Path, source_srt_paths: list[Path], report_path: Path) -> dict:
    """Lightweight final QA: restore high-confidence missing cues and report suspicious spots."""
    final_cues = parse_srt(final_srt)
    inserted: list[dict] = []
    suspicious: list[dict] = []
    source_reports: list[dict] = []

    for source_path in source_srt_paths:
        if not source_path or not source_path.exists():
            continue
        source_cues = parse_srt(source_path)
        source_name = source_path.name.lower()
        # OCR-cleaned/corrected sources are allowed to restore missing hard-sub cues.
        # ASR is useful for suspicious reporting, but it often merges several short
        # visual subtitle cues into one long spoken segment, so it must not insert.
        allow_insert = "cleaned" in source_name or "corrected" in source_name
        source_reports.append({"source": str(source_path), "cues": len(source_cues)})
        for idx, cue in enumerate(source_cues):
            text = str(cue.get("text", "")).strip()
            if not text:
                continue
            candidate_text = text
            if "ocr" in source_name:
                candidate_text, _ = cleanup_ocr_visual_noise_text(candidate_text)
            candidate_cues, _ = apply_phrase_corrections([{**cue, "text": candidate_text}], BUILTIN_ZH_CORRECTIONS)
            candidate_text = str(candidate_cues[0].get("text", "")).strip() if candidate_cues else ""
            overlaps = [f for f in final_cues if time_overlap_ratio(cue, f) >= 0.55]
            source_key = compact_visible_text(candidate_text)
            if overlaps:
                best_text = max((str(f.get("text", "")) for f in overlaps), key=lambda x: SequenceMatcher(None, source_key, compact_visible_text(x)).ratio(), default="")
                sim = SequenceMatcher(None, source_key, compact_visible_text(best_text)).ratio() if source_key else 0
                if source_key and sim < 0.38 and len(source_key) >= 8:
                    suspicious.append({
                        "type": "low_text_match_same_time",
                        "source": str(source_path),
                        "time": cue.get("time", ""),
                        "source_text": candidate_text,
                        "final_text": best_text,
                        "similarity": round(sim, 3),
                    })
                continue
            if not allow_insert:
                if candidate_text:
                    suspicious.append({
                        "type": "missing_in_final_report_only",
                        "source": str(source_path),
                        "time": cue.get("time", ""),
                        "source_text": candidate_text,
                    })
                continue
            if not candidate_text or "@" in candidate_text or contains_ai_internal_leak(candidate_text):
                continue
            if re.search(r"[01]{8,}|\d{1,3}\s*[:\uff1a.\-]\s*\d{2,5}|(?<![A-Za-z])\d{5,}(?![A-Za-z])", candidate_text):
                continue
            if has_nearby_text_duplicate(cue, candidate_text, final_cues):
                suspicious.append({
                    "type": "nearby_duplicate_skipped",
                    "source": str(source_path),
                    "time": cue.get("time", ""),
                    "source_text": candidate_text,
                })
                continue
            prev_cue = source_cues[idx - 1] if idx > 0 else None
            next_cue = source_cues[idx + 1] if idx + 1 < len(source_cues) else None
            if not is_suspicious_final_gap(prev_cue, {**cue, "text": candidate_text}, next_cue):
                continue
            repaired = dict(cue)
            repaired["text"] = candidate_text
            final_cues.append(repaired)
            inserted.append({
                "source": str(source_path),
                "time": cue.get("time", ""),
                "text": candidate_text,
                "reason": "missing_time_gap_recovered",
            })

    candidate_inserted = list(inserted)
    final_cues.sort(key=lambda item: cue_time_bounds(item)[0])
    final_cues, safety = safety_finalize_cues(final_cues)
    restored_after_safety: list[dict] = []
    for item in candidate_inserted:
        text = str(item.get("text", "")).strip()
        time_line = str(item.get("time", "")).strip()
        if not text or not time_line:
            continue
        probe = {"time": time_line, "text": text}
        survived = any(
            time_overlap_ratio(probe, final) >= 0.55
            and compact_visible_text(text) == compact_visible_text(str(final.get("text", "")))
            for final in final_cues
        )
        if survived:
            continue
        if any(time_overlap_ratio(probe, final) >= 0.80 and compact_visible_text(text) in compact_visible_text(str(final.get("text", ""))) for final in final_cues):
            continue
        final_cues.append({"id": 0, "time": time_line, "text": text})
        restored_after_safety.append(item)
    if restored_after_safety:
        final_cues.sort(key=lambda item: cue_time_bounds(item)[0])
        for idx, cue in enumerate(final_cues, 1):
            cue["id"] = idx
    write_srt(final_cues, final_srt)
    survived_inserted = []
    for item in candidate_inserted:
        probe = {"time": item.get("time", ""), "text": item.get("text", "")}
        if any(
            time_overlap_ratio(probe, final) >= 0.55
            and compact_visible_text(str(item.get("text", ""))) == compact_visible_text(str(final.get("text", "")))
            for final in final_cues
        ):
            survived_inserted.append(item)
    report = {
        "status": "ok",
        "source_reports": source_reports,
        "candidate_inserted_count": len(candidate_inserted),
        "inserted_count": len(survived_inserted),
        "inserted": survived_inserted[:200],
        "restored_after_safety_count": len(restored_after_safety),
        "restored_after_safety": restored_after_safety[:100],
        "suspicious_count": len(suspicious),
        "suspicious": suspicious[:300],
        "post_safety": safety,
        "final_cues": len(final_cues),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def parse_srt(path: Path) -> list[dict]:
    """Parse SRT into cue dicts while preserving cue id and timestamp line."""
    text = path.read_text(encoding="utf-8", errors="replace")
    cues: list[dict] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip("\ufeff").rstrip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        try:
            cue_id = int(lines[0].strip())
        except ValueError:
            continue
        cues.append({"id": cue_id, "time": lines[1].strip(), "text": " ".join(line.strip() for line in lines[2:]).strip()})
    return cues


def write_srt(cues: list[dict], path: Path) -> None:
    """Write cue dicts back to SRT without changing timing."""
    lines: list[str] = []
    for cue in cues:
        text = str(cue.get("text", "")).strip()
        if text:
            lines.extend([str(int(cue["id"])), str(cue["time"]), text, ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_srt_time_ms(value: str) -> int:
    """Parse SRT timestamp into milliseconds."""
    m = re.match(r"(\d+):(\d+):(\d+),(\d+)", str(value).strip())
    if not m:
        return 0
    h, mi, s, ms = [int(x) for x in m.groups()]
    return ((h * 60 + mi) * 60 + s) * 1000 + ms


def format_srt_time_ms(ms: int) -> str:
    """Format milliseconds as SRT timestamp."""
    ms = max(0, int(ms))
    h = ms // 3_600_000
    ms %= 3_600_000
    mi = ms // 60_000
    ms %= 60_000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{mi:02d}:{s:02d},{ms:03d}"


def cue_time_bounds(cue: dict) -> tuple[int, int]:
    """Return cue start/end milliseconds."""
    parts = str(cue.get("time", "")).split("-->")
    if len(parts) != 2:
        return 0, 0
    return parse_srt_time_ms(parts[0]), parse_srt_time_ms(parts[1])


def compact_for_similarity(text: str) -> str:
    """Remove whitespace and punctuation-ish chars for similarity checks."""
    return re.sub(r"[\s,.;:!?，。！？；：、\"'“”‘’（）()\[\]{}<>《》]", "", str(text or ""))


def contains_ai_internal_leak(text: str) -> bool:
    """Return true if model leaked analysis/debug text into subtitle content."""
    text = str(text or "")
    return any(marker in text for marker in AI_INTERNAL_LEAK_MARKERS)


def is_safe_ai_rewrite(original: str, candidate: str) -> tuple[bool, str]:
    """Reject hallucinated or too-aggressive AI rewrites."""
    old = str(original or "").strip()
    new = str(candidate or "").strip()
    if not new:
        return False, "empty"
    if "\n" in new or "\r" in new:
        return False, "multiline"
    if len(new) > max(len(old) + 28, int(len(old) * 1.75) + 8):
        return False, "too_long"
    old_compact = compact_for_similarity(old)
    new_compact = compact_for_similarity(new)
    if len(old_compact) >= 8 and len(new_compact) >= 8:
        ratio = SequenceMatcher(None, old_compact, new_compact).ratio()
        if ratio < 0.42:
            return False, f"too_different:{ratio:.2f}"
    if any(marker in new and marker not in old for marker in SUMMARY_HALLUCINATION_MARKERS):
        return False, "summary_marker"
    if contains_ai_internal_leak(new):
        return False, "ai_internal_leak"
    return True, "ok"


def split_text_for_duration(text: str, max_chars: int = 42) -> list[str]:
    """Split long Chinese subtitle text into readable pieces."""
    text = re.sub(r"\s+", "", str(text or "").strip())
    if not text:
        return []
    parts = [p for p in re.split(r"(?<=[，。！？；：、,.!?;:])", text) if p]
    out: list[str] = []
    for part in parts or [text]:
        while len(part) > max_chars:
            out.append(part[:max_chars])
            part = part[max_chars:]
        if part:
            out.append(part)
    return out or [text]


def safety_finalize_cues(cues: list[dict], max_cue_seconds: float = 7.0, max_chars: int = 48) -> tuple[list[dict], dict]:
    """Remove nearby duplicates and split unreadably long cues without changing total timing span."""
    cues = sorted((dict(cue) for cue in cues), key=lambda item: cue_time_bounds(item)[0])
    deduped: list[dict] = []
    duplicate_removed = 0
    leak_removed = 0
    summary_removed = 0
    overlap_trimmed = 0
    for cue in cues:
        text = str(cue.get("text", "")).strip()
        start_ms, end_ms = cue_time_bounds(cue)
        if contains_ai_internal_leak(text):
            leak_removed += 1
            continue
        if len(text) > 80 and (end_ms - start_ms) > 15_000 and any(marker in text for marker in SUMMARY_HALLUCINATION_MARKERS):
            summary_removed += 1
            continue
        if deduped and text:
            prev_text = str(deduped[-1].get("text", "")).strip()
            if (
                (
                    re.search(r"\u6bcf\u65e5\u7b7e\u5230\s*[:\uff1a]\s*\u672a\u8fdb\u884c", prev_text)
                    or re.search(r"\u4efb\u52a1\u5217\u8868\s*[:\uff1a]\s*\u5f85\u9886\u53d6\u4efb\u52a1\d+", prev_text)
                )
                and re.search(r"\u4efb\u52a1\u5217\u8868\s*[:\uff1a]\s*\u5f85\u9886\u53d6\u4efb\u52a1\d+", text)
            ):
                next_text = re.sub(r"\u6bcf\u65e5\u7b7e\u5230\s*[:\uff1a]\s*\u672a\u8fdb\u884c", " ", text)
                next_text = re.sub(r"\s+", " ", next_text).strip(" ，,、")
                if next_text:
                    text = next_text
                    cue = dict(cue)
                    cue["text"] = text
                    overlap_trimmed += 1
            prev_key = compact_visible_text(prev_text)
            text_key = compact_visible_text(text)
            best_overlap = ""
            for size in range(min(len(prev_key), len(text_key)), 5, -1):
                candidate = prev_key[-size:]
                if text_key.startswith(candidate):
                    best_overlap = candidate
                    break
            if best_overlap:
                next_text = re.sub(re.escape(best_overlap), "", text, count=1).strip(" ，,、")
                if next_text and compact_visible_text(next_text) != text_key:
                    text = next_text
                    cue = dict(cue)
                    cue["text"] = text
                    overlap_trimmed += 1
        if text:
            replaced_duplicate = False
            for offset, prev in enumerate(deduped[-4:], 1):
                if text != str(prev.get("text", "")).strip():
                    continue
                prev_start, prev_end = cue_time_bounds(prev)
                if (end_ms - start_ms) > (prev_end - prev_start):
                    replacement = dict(cue)
                    replacement["text"] = text
                    deduped[len(deduped) - offset] = replacement
                    replaced_duplicate = True
                duplicate_removed += 1
                break
            if replaced_duplicate or any(text == str(prev.get("text", "")).strip() for prev in deduped[-4:]):
                continue
        deduped.append(dict(cue))

    deduped.sort(key=lambda item: cue_time_bounds(item)[0])
    ordered: list[dict] = []
    for cue in deduped:
        item = dict(cue)
        text = str(item.get("text", "")).strip()
        if ordered:
            prev = ordered[-1]
            prev_text = str(prev.get("text", "")).strip()
            prev_key = compact_visible_text(prev_text)
            text_key = compact_visible_text(text)
            if prev_key and prev_key in text_key and prev_key != text_key:
                if re.search(r"^\s*\d+|\u7406\u89e3\u548c\u914d\u5408|[·]{1,}", text):
                    duplicate_removed += 1
                    continue
            if (
                re.search(r"\u6bcf\u65e5\u7b7e\u5230\s*[:\uff1a]\s*\u672a\u8fdb\u884c", prev_text)
                and re.search(r"\u4efb\u52a1\u5217\u8868\s*[:\uff1a]\s*\u5f85\u9886\u53d6\u4efb\u52a1\d+", text)
            ):
                text = re.sub(r"\u6bcf\u65e5\u7b7e\u5230\s*[:\uff1a]\s*\u672a\u8fdb\u884c", " ", text)
                text = re.sub(r"\s+", " ", text).strip(" ，,、")
                item["text"] = text
                overlap_trimmed += 1
            if compact_visible_text(text) == compact_visible_text(prev_text):
                prev_start, prev_end = cue_time_bounds(prev)
                cur_start, cur_end = cue_time_bounds(item)
                if (cur_end - cur_start) > (prev_end - prev_start):
                    ordered[-1] = item
                duplicate_removed += 1
                continue
        ordered.append(item)
    deduped = ordered

    final: list[dict] = []
    split_count = 0
    for cue in deduped:
        text = str(cue.get("text", "")).strip()
        start_ms, end_ms = cue_time_bounds(cue)
        dur_ms = max(0, end_ms - start_ms)
        if text and dur_ms > int(max_cue_seconds * 1000) and (len(text) > max_chars or (dur_ms > 12_000 and len(text) > 18)):
            pieces = split_text_for_duration(text, max_chars=max_chars)
            if len(pieces) > 1:
                weights = [max(1, len(p)) for p in pieces]
                total = sum(weights)
                cursor = start_ms
                for index, piece in enumerate(pieces):
                    part_end = end_ms if index == len(pieces) - 1 else cursor + max(600, int(dur_ms * weights[index] / total))
                    final.append({"id": len(final) + 1, "time": f"{format_srt_time_ms(cursor)} --> {format_srt_time_ms(part_end)}", "text": piece})
                    cursor = part_end
                split_count += 1
                continue
        item = dict(cue)
        item["id"] = len(final) + 1
        final.append(item)
    return final, {
        "duplicates_removed": duplicate_removed,
        "ai_internal_leaks_removed": leak_removed,
        "summary_hallucinations_removed": summary_removed,
        "overlap_trimmed": overlap_trimmed,
        "long_cues_split": split_count,
    }


def normalize_phrase_map(data) -> dict[str, str]:
    """Normalize AI/builtin phrase maps into {wrong: correct} with safe CJK strings."""
    out: dict[str, str] = {}
    if isinstance(data, dict):
        items = data.items()
    elif isinstance(data, list):
        items = []
        for item in data:
            if isinstance(item, dict):
                wrong = item.get("wrong") or item.get("asr") or item.get("source") or item.get("from")
                right = item.get("correct") or item.get("target") or item.get("to")
                if wrong and right:
                    items.append((wrong, right))
    else:
        items = []
    for wrong, right in items:
        wrong_s = str(wrong or "").strip()
        right_s = str(right or "").strip()
        if not wrong_s or not right_s or wrong_s == right_s:
            continue
        if contains_ai_internal_leak(wrong_s) or contains_ai_internal_leak(right_s):
            continue
        if len(wrong_s) > 24 or len(right_s) > 32:
            continue
        if not re.search(r"[\u3400-\u9fffA-Za-z0-9]", wrong_s + right_s):
            continue
        out[wrong_s] = right_s
    return out


def correction_map_from_glossary(glossary: dict) -> dict[str, str]:
    """Extract likely ASR correction phrases from a glossary object."""
    merged: dict[str, str] = {}
    for key in ("likely_asr_errors", "homophone_errors", "correction_map", "term_corrections"):
        merged.update(normalize_phrase_map(glossary.get(key)))
    return merged


def apply_phrase_corrections(cues: list[dict], phrase_map: dict[str, str]) -> tuple[list[dict], int]:
    """Apply deterministic high-confidence phrase corrections without touching timing."""
    if not phrase_map:
        return [dict(cue) for cue in cues], 0
    ordered = sorted(phrase_map.items(), key=lambda kv: len(kv[0]), reverse=True)
    changed = 0
    out: list[dict] = []
    for cue in cues:
        item = dict(cue)
        text = str(item.get("text", ""))
        next_text = text
        for wrong, right in ordered:
            if wrong in next_text:
                next_text = next_text.replace(wrong, right)
        if next_text != text:
            changed += 1
            item["text"] = next_text
        out.append(item)
    return out, changed


def nearby_asr_text(cue: dict, asr_cues: list[dict], window_ms: int = 1600) -> str:
    """Return ASR text near an OCR cue for artifact filtering."""
    start_ms, end_ms = cue_time_bounds(cue)
    parts: list[str] = []
    for asr in asr_cues:
        asr_start, asr_end = cue_time_bounds(asr)
        if asr_end >= start_ms - window_ms and asr_start <= end_ms + window_ms:
            parts.append(str(asr.get("text", "")))
    return "".join(parts)


def compact_visible_text(text: str) -> str:
    """Compact text for duplicate/noise checks while preserving CJK content."""
    return re.sub(r"[\s,，。.!！?？:：;；、·\-\[\]【】()（）]+", "", str(text or ""))


def is_ui_only_text(text: str) -> bool:
    """Return true when text is a pure system/dashboard OCR label."""
    return any(p.match(str(text or "").strip()) for p in OCR_UI_ONLY_PATTERNS)


def has_system_ui_context(cue: dict, cues: list[dict], window_ms: int = 9000) -> bool:
    """Keep system UI captions when they appear near explicit system/interface text."""
    start_ms, end_ms = cue_time_bounds(cue)
    context_terms = (
        "\u5927\u533b\u7cfb\u7edf",
        "\u754c\u9762",
        "\u91d1\u624b\u6307",
        "\u7b7e\u5230",
        "\u4efb\u52a1",
        "\u7cfb\u7edf\u58f0\u97f3",
    )
    for other in cues:
        other_start, other_end = cue_time_bounds(other)
        if other_end < start_ms - window_ms or other_start > end_ms + window_ms:
            continue
        text = str(other.get("text", ""))
        if any(term in text for term in context_terms):
            return True
    return False


def cleanup_ocr_visual_noise_text(text: str) -> tuple[str, dict[str, int]]:
    """Remove OCR visual overlays: watermarks, dashboard labels, and numeric codes."""
    stats: dict[str, int] = {}

    def bump(name: str, before: str, after: str) -> str:
        if after != before:
            stats[name] = stats.get(name, 0) + 1
        return after

    def clean_line(line: str) -> str:
        line = str(line or "").strip()
        if not line:
            return ""
        before = line
        line = re.sub(r"@\s*[\u3400-\u9fffA-Za-z0-9_]{1,12}", " ", line)
        line = bump("watermark_or_code", before, line)
        line = re.sub(r"\s+", " ", line).strip(" ，,、:：.-")
        if line and is_ui_only_text(line):
            return line
        ui_hits: list[str] = []
        for pattern in OCR_UI_INLINE_PATTERNS:
            ui_hits.extend(match.group(0).strip() for match in pattern.finditer(line))
        for pattern in OCR_CODE_PATTERNS:
            before = line
            line = pattern.sub(" ", line)
            line = bump("watermark_or_code", before, line)
        for pattern in OCR_UI_INLINE_PATTERNS:
            before = line
            line = pattern.sub(" ", line)
            line = bump("ui_inline", before, line)
        if ui_hits and not re.search(r"[\u3400-\u9fff]", line):
            unique_hits: list[str] = []
            seen_hits: set[str] = set()
            for hit in ui_hits:
                key = compact_visible_text(hit)
                if key and key not in seen_hits:
                    seen_hits.add(key)
                    unique_hits.append(hit)
            return " ".join(unique_hits)
        for phrase in OCR_NOISE_EXACT_PHRASES:
            if phrase in line:
                line = line.replace(phrase, " ")
                stats[phrase] = stats.get(phrase, 0) + 1
        line = re.sub(r"\s+", " ", line).strip(" ，,、:：.-")
        if not line:
            return ""
        if re.fullmatch(r"[\d\s:：.\-]+", line):
            stats["watermark_or_code"] = stats.get("watermark_or_code", 0) + 1
            return ""
        return line

    lines = [clean_line(line) for line in re.split(r"[\r\n]+", str(text or ""))]
    cleaned_lines: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = compact_visible_text(line)
        if not key:
            continue
        if key in seen:
            stats["in_cue_duplicate"] = stats.get("in_cue_duplicate", 0) + 1
            continue
        seen.add(key)
        cleaned_lines.append(line)
    cleaned = " ".join(cleaned_lines)
    for _ in range(4):
        next_cleaned = re.sub(r"(.{4,24})\s+\1", r"\1", cleaned)
        if next_cleaned == cleaned:
            break
        stats["in_cue_duplicate"] = stats.get("in_cue_duplicate", 0) + 1
        cleaned = next_cleaned
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,、:：.-")
    return cleaned, stats


def remove_repeated_ocr_artifacts(ocr_cues: list[dict], asr_cues: list[dict]) -> tuple[list[dict], dict]:
    """Remove short repeated OCR-only labels such as scene UI text before fusion."""
    prefix_counts: dict[str, int] = {}
    for cue in ocr_cues:
        text = str(cue.get("text", "")).strip()
        m = re.match(r"^([\u3400-\u9fffA-Za-z0-9]{1,6})[\s:：,，]+(.{2,})$", text)
        if m:
            prefix_counts[m.group(1)] = prefix_counts.get(m.group(1), 0) + 1
    repeated_prefixes = {k for k, v in prefix_counts.items() if v >= 2}

    changed = 0
    dropped = 0
    removed_tokens: dict[str, int] = {}
    out: list[dict] = []
    for cue in ocr_cues:
        item = dict(cue)
        text = str(item.get("text", "")).strip()
        original_text = text

        def remove_token(token: str, replacement: str = "") -> None:
            nonlocal text, changed
            if token in text:
                text = text.replace(token, replacement)
                changed += 1
                removed_tokens[token] = removed_tokens.get(token, 0) + 1

        next_text, line_stats = cleanup_ocr_visual_noise_text(text)
        if next_text != text:
            changed += 1
            text = next_text
        for key, value in line_stats.items():
            removed_tokens[key] = removed_tokens.get(key, 0) + value

        for phrase in OCR_NOISE_EXACT_PHRASES:
            remove_token(phrase)

        text = re.sub(r"\s+", " ", text).strip(" ，,、")
        asr_text = nearby_asr_text(item, asr_cues)
        asr_compact = compact_for_similarity(asr_text)
        for prefix in sorted(repeated_prefixes, key=len, reverse=True):
            pattern = rf"^{re.escape(prefix)}[\s:：,，]+(.+)$"
            m = re.match(pattern, text)
            if not m:
                continue
            candidate = m.group(1).strip()
            if not candidate:
                continue
            old_score = SequenceMatcher(None, compact_for_similarity(text), asr_compact).ratio() if asr_compact else 0.0
            new_score = SequenceMatcher(None, compact_for_similarity(candidate), asr_compact).ratio() if asr_compact else 0.0
            prefix_in_asr = prefix in asr_text
            if prefix in {"\u7b80\u5386", "\u7b7e\u5230"} and (not prefix_in_asr or new_score >= old_score):
                text = candidate
                changed += 1
                removed_tokens[prefix] = removed_tokens.get(prefix, 0) + 1
                break
        item["text"] = text
        item["text"] = re.sub(r"\s+", " ", str(item["text"])).strip(" ，,、")
        if not item["text"]:
            dropped += 1
            continue
        if is_ui_only_text(item["text"]) and not has_system_ui_context(item, ocr_cues):
            dropped += 1
            removed_tokens["ui_only_cue"] = removed_tokens.get("ui_only_cue", 0) + 1
            continue
        if item["text"] != original_text:
            changed += 0
        out.append(item)
    return out, {"changed": changed, "dropped": dropped, "removed_tokens": removed_tokens}


def clean_ocr_srt_with_asr(ocr_srt: Path, asr_srt: Path, out_srt: Path) -> dict:
    """Clean OCR subtitle artifacts using the ASR transcript as secondary evidence."""
    ocr_cues = parse_srt(ocr_srt)
    asr_cues = parse_srt(asr_srt) if asr_srt.exists() else []
    cleaned, stats = remove_repeated_ocr_artifacts(ocr_cues, asr_cues)
    cleaned, safety = safety_finalize_cues(cleaned)
    write_srt(cleaned, out_srt)
    stats["safety"] = safety
    stats["cues"] = len(cleaned)
    stats["source"] = str(ocr_srt)
    stats["output"] = str(out_srt)
    return stats


def copy_reference_srt(reference_srt: Path, out_srt: Path) -> dict:
    """Use a trusted external SRT as final text/timing source."""
    if not reference_srt.exists():
        raise FileNotFoundError(f"Reference SRT not found: {reference_srt}")
    cues = parse_srt(reference_srt)
    if not cues:
        raise ValueError(f"Reference SRT has no valid cues: {reference_srt}")
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(reference_srt, out_srt)
    return {
        "status": "ok",
        "source": str(reference_srt),
        "cues": len(cues),
    }


def openai_chat(endpoint: str, model: str, messages: list[dict], temperature: float, max_tokens: int, timeout: int) -> str:
    """Call a local OpenAI-compatible chat endpoint such as LM Studio or Ollama."""
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    content = str(data["choices"][0]["message"].get("content", ""))
    if not content.strip() and data["choices"][0]["message"].get("reasoning"):
        raise RuntimeError(
            f"Model {model} returned empty content and reasoning-only output. "
            "Use qwen2:7b/qwen2.5 or configure this model to return normal content."
        )
    return content


def resolve_ai_endpoint(endpoint: str, logs: Path, timeout: int = 8) -> str:
    """Return the first reachable OpenAI-compatible endpoint, trying Ollama if LM Studio is down."""
    candidates = [endpoint]
    ollama_endpoint = "http://localhost:11434/v1"
    if endpoint.rstrip("/") != ollama_endpoint.rstrip("/"):
        candidates.append(ollama_endpoint)
    last_error = ""
    for item in candidates:
        try:
            req = urllib.request.Request(item.rstrip("/") + "/models", method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read(2048)
            if item != endpoint:
                with logs.open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write(f"[AI] endpoint fallback: {endpoint} -> {item}\n")
            return item
        except Exception as exc:
            last_error = str(exc)
            with logs.open("a", encoding="utf-8", errors="replace") as fh:
                fh.write(f"[AI] endpoint unavailable {item}: {exc}\n")
            if item.rstrip("/") == ollama_endpoint.rstrip("/"):
                started = try_start_ollama(logs)
                if started:
                    try:
                        req = urllib.request.Request(item.rstrip("/") + "/models", method="GET")
                        with urllib.request.urlopen(req, timeout=timeout) as resp:
                            resp.read(2048)
                        return item
                    except Exception as retry_exc:
                        last_error = str(retry_exc)
                        with logs.open("a", encoding="utf-8", errors="replace") as fh:
                            fh.write(f"[AI] endpoint unavailable after Ollama start {item}: {retry_exc}\n")
    raise RuntimeError(
        "No local AI endpoint reachable. Start LM Studio server at http://localhost:1234/v1 "
        "or Ollama at http://localhost:11434/v1, then load a Chinese-capable Qwen model. "
        f"Last error: {last_error}"
    )


def try_start_ollama(logs: Path) -> bool:
    """Start Ollama server on Windows if ollama.exe is installed and not already serving."""
    exe = shutil.which("ollama")
    if not exe:
        local = Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"
        exe = str(local) if local.exists() else ""
    if not exe:
        return False
    try:
        with logs.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"[AI] starting Ollama server: {exe}\n")
        subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        time.sleep(5)
        return True
    except Exception as exc:
        with logs.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"[AI] failed to start Ollama: {exc}\n")
        return False


def extract_json(text: str):
    """Extract JSON from strict or fenced model output."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    spans = [(cleaned.find("{"), cleaned.rfind("}")), (cleaned.find("["), cleaned.rfind("]"))]
    for start, end in spans:
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("AI response is not valid JSON")


def openai_json(
    endpoint: str,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    timeout: int,
):
    """Call chat model and retry once if it ignores the JSON-only instruction."""
    last_content = ""
    last_error: Exception | None = None
    for attempt in range(2):
        call_messages = list(messages)
        if attempt:
            call_messages = [
                {
                    "role": "system",
                    "content": (
                        "CRITICAL OUTPUT FORMAT: return raw valid JSON only. "
                        "No markdown, no explanation, no English commentary, no code fence."
                    ),
                },
                *messages,
                {"role": "user", "content": "Return the corrected result again as RAW VALID JSON ONLY."},
            ]
        last_content = openai_chat(endpoint, model, call_messages, temperature if attempt == 0 else 0.0, max_tokens, timeout)
        try:
            return extract_json(last_content)
        except Exception as exc:
            last_error = exc
            continue
    raise ValueError(f"AI response is not valid JSON after retry: {last_error}. Raw: {last_content[:500]}")


def build_context_sample(cues: list[dict], max_chars: int = 14000) -> str:
    """Sample transcript across the video so the local LLM can infer genre and terms."""
    parts: list[str] = []
    indices = list(range(min(100, len(cues))))
    if len(cues) > 160:
        step = max(1, len(cues) // 14)
        for i in range(100, len(cues), step):
            indices.extend(range(i, min(len(cues), i + 8)))
    seen = set()
    for i in indices:
        if i in seen or i >= len(cues):
            continue
        seen.add(i)
        line = f"{cues[i]['id']}. {cues[i]['text']}"
        if sum(len(x) for x in parts) + len(line) > max_chars:
            break
        parts.append(line)
    return "\n".join(parts)


def build_secondary_context(cues: list[dict], max_chars: int = 7000) -> str:
    """Build compact secondary ASR context for OCR-first correction."""
    parts: list[str] = []
    for cue in cues:
        start_ms, end_ms = cue_time_bounds(cue)
        line = f"{format_srt_time_ms(start_ms)}-{format_srt_time_ms(end_ms)} {cue.get('text', '')}"
        if sum(len(x) for x in parts) + len(line) > max_chars:
            break
        parts.append(line)
    return "\n".join(parts)


def ai_build_glossary(cues: list[dict], endpoint: str, model: str, logs: Path, timeout: int, secondary_cues: list[dict] | None = None) -> dict:
    """Build a per-video Chinese glossary and likely homophone correction map."""
    sample = build_context_sample(cues)
    system = (
        "You are a Chinese ASR correction analyst for subtitles. Produce a dynamic glossary for this video. "
        "Do not translate. Infer genre/context, character names, professional terms, and repeated homophone ASR errors. "
        "Only include corrections when the wrong phrase is likely present in the transcript sample. "
        "Never output uncertainty notes like 可能为误听, 错译, 实际应为, or 系统名称 as subtitle text or corrections."
    )
    user = (
        "Analyze this Chinese ASR transcript sample. Return STRICT JSON only with keys: "
        "genre, context_summary, characters, locations, domain_terms, likely_asr_errors. "
        "likely_asr_errors must be an object mapping exact wrong ASR phrases to corrected Chinese phrases. "
        "Prefer short exact phrase corrections, e.g. {\"急性蓝尾炎\":\"急性阑尾炎\"}. "
        "Never include timing. Never include Vietnamese translation.\n\n"
        f"PRIMARY_SOURCE_SAMPLE:\n{sample}\n\n"
        f"SECONDARY_ASR_CONTEXT_FOR_CROSS_CHECK:\n{build_secondary_context(secondary_cues or [])}"
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    data = openai_json(endpoint, model, messages, 0.1, 3000, timeout)
    with logs.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write("\n[AI glossary parsed]\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    if not isinstance(data, dict):
        raise ValueError("AI glossary must be a JSON object")
    return data


def ai_correct_block(block: list[dict], glossary: dict, endpoint: str, model: str, timeout: int, secondary_context: str = "") -> list[dict]:
    """Correct one cue block with the local LLM while preserving cue ids."""
    compact = [{"id": cue["id"], "text": cue["text"]} for cue in block]
    system = (
        "You are a high-precision Chinese ASR subtitle corrector. Rules: do not translate, do not rewrite style, "
        "do not add information, do not split or merge cues, do not change cue ids. "
        "Only fix ASR/OCR homophones, wrong terms, names, punctuation, and obvious typos. "
        "If PRIMARY is OCR hard-sub, trust PRIMARY over ASR unless OCR is clearly broken/noisy. "
        "Preserve cue count exactly. Keep text concise and spoken, not literary. "
        "Never output uncertainty notes such as 可能为误听或错译, 实际应为某位角色的名字, or 系统名称. "
        "If unsure, keep the original text unchanged. "
        "Return STRICT JSON array only: [{\"id\": number, \"text\": \"corrected Chinese text\", \"confidence\": 0-1, \"reason\": \"short\"}]."
    )
    user = (
        f"Dynamic glossary/context:\n{json.dumps(glossary, ensure_ascii=False)}\n\n"
        f"Secondary ASR context near this block, for cross-check only. Do not copy it blindly:\n{secondary_context}\n\n"
        f"Correct these PRIMARY cues and preserve every id:\n{json.dumps(compact, ensure_ascii=False)}"
    )
    data = openai_json(endpoint, model, [{"role": "system", "content": system}, {"role": "user", "content": user}], 0.05, 6000, timeout)
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if not isinstance(data, list):
        raise ValueError("AI correction must be a JSON array")
    return [item for item in data if isinstance(item, dict) and "id" in item and str(item.get("text", "")).strip()]


def run_ai_correction(
    asr_srt: Path,
    corrected_srt: Path,
    glossary_path: Path,
    correction_path: Path,
    logs: Path,
    endpoint: str,
    model: str,
    block_size: int,
    timeout: int,
    secondary_srt: Path | None = None,
    progress_cb=None,
) -> dict:
    """Run local LLM glossary + correction pass and return status metadata."""
    def emit(pct: float, msg: str) -> None:
        if progress_cb:
            progress_cb(pct, msg)
        with logs.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"[AI] {msg}\n")

    started = time.time()
    cues = parse_srt(asr_srt)
    secondary_cues = parse_srt(secondary_srt) if secondary_srt and secondary_srt.exists() else []
    emit(79, f"AI correction: read {len(cues)} cues, applying built-in term map")
    cues, builtin_changed = apply_phrase_corrections(cues, BUILTIN_ZH_CORRECTIONS)
    emit(79.5, f"AI correction: built-in map changed={builtin_changed}, building glossary")
    endpoint = resolve_ai_endpoint(endpoint, logs)
    glossary = ai_build_glossary(cues, endpoint, model, logs, timeout, secondary_cues=secondary_cues)
    glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8")
    dynamic_map = correction_map_from_glossary(glossary)
    phrase_map = dict(BUILTIN_ZH_CORRECTIONS)
    phrase_map.update(dynamic_map)
    cues, map_changed = apply_phrase_corrections(cues, phrase_map)
    emit(80, f"AI correction: dynamic map={len(dynamic_map)} items, map changed={map_changed}")
    corrections: list[dict] = []
    blocks = [cues[i : i + max(10, block_size)] for i in range(0, len(cues), max(10, block_size))]
    for idx, block in enumerate(blocks, 1):
        emit(81 + 14 * ((idx - 1) / max(1, len(blocks))), f"AI correction: block {idx}/{len(blocks)}")
        try:
            block_start, block_end = cue_time_bounds(block[0])[0], cue_time_bounds(block[-1])[1]
            nearby_secondary = []
            for sec in secondary_cues:
                sec_start, sec_end = cue_time_bounds(sec)
                if sec_end >= block_start - 2000 and sec_start <= block_end + 2000:
                    nearby_secondary.append(sec)
            corrections.extend(ai_correct_block(block, glossary, endpoint, model, timeout, build_secondary_context(nearby_secondary, max_chars=4500)))
        except Exception as exc:
            corrections.append({"block": idx, "error": str(exc), "ids": [cue["id"] for cue in block]})
    by_id = {int(item["id"]): str(item["text"]).strip() for item in corrections if "id" in item and str(item.get("text", "")).strip()}
    changed = 0
    rejected = 0
    reject_reasons: dict[str, int] = {}
    corrected = []
    for cue in cues:
        item = dict(cue)
        next_text = by_id.get(int(cue["id"]))
        if next_text and next_text != cue["text"]:
            ok, reason = is_safe_ai_rewrite(str(cue["text"]), next_text)
            if ok:
                item["text"] = next_text
                changed += 1
            else:
                rejected += 1
                reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
        corrected.append(item)
    corrected, final_map_changed = apply_phrase_corrections(corrected, phrase_map)
    corrected, safety_stats = safety_finalize_cues(corrected)
    write_srt(corrected, corrected_srt)
    correction_path.write_text(
        json.dumps(
            {
                "changed": changed,
                "ai_rejected": rejected,
                "reject_reasons": reject_reasons,
                "builtin_map_changed": builtin_changed,
                "dynamic_map_changed": map_changed,
                "final_map_changed": final_map_changed,
                "safety": safety_stats,
                "total_cues": len(cues),
                "dynamic_map": dynamic_map,
                "items": corrections,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "changed": changed,
        "ai_rejected": rejected,
        "reject_reasons": reject_reasons,
        "builtin_map_changed": builtin_changed,
        "dynamic_map_changed": map_changed,
        "final_map_changed": final_map_changed,
        "safety": safety_stats,
        "total_cues": len(cues),
        "elapsed_seconds": round(time.time() - started, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto Hybrid SRT: OCR-first hard-sub extraction, ASR secondary cross-check, guarded AI correction.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--model", default="large-v3-turbo", choices=["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"])
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument("--engine", default="sensevoice", choices=["auto", "whisperx", "faster-whisper", "sensevoice"])
    parser.add_argument("--debug-dir", type=Path, default=None)
    parser.add_argument("--enable-ocr", action="store_true")
    parser.add_argument("--ai-correct", action="store_true", help="Run local OpenAI-compatible AI correction after ASR/OCR.")
    parser.add_argument("--ai-endpoint", default="http://localhost:1234/v1", help="OpenAI-compatible endpoint, e.g. LM Studio.")
    parser.add_argument("--ai-model", default="qwen2:7b", help="Local Chinese-capable correction model name.")
    parser.add_argument("--ai-block-size", type=int, default=40, help="Cue count per correction request.")
    parser.add_argument("--ai-timeout", type=int, default=180, help="Seconds per local AI request.")
    parser.add_argument("--require-ai", action="store_true", help="Fail the job instead of exporting raw ASR if AI correction fails.")
    parser.add_argument("--reference-srt", type=Path, default=None, help="Trusted SRT to use as final after ASR debug is generated.")
    parser.add_argument("--crop", default="auto")
    args = parser.parse_args()

    started = time.time()
    video = args.input.resolve()
    out = args.output.resolve()
    if not video.exists():
        raise FileNotFoundError(video)
    if not PY.exists():
        raise FileNotFoundError(PY)
    if not EXTRACT_SCRIPT.exists():
        raise FileNotFoundError(EXTRACT_SCRIPT)

    debug_dir = (args.debug_dir or out.with_suffix("").with_name(out.stem + "_debug")).resolve()
    debug_dir.mkdir(parents=True, exist_ok=True)
    logs = debug_dir / "logs.txt"
    report_path = debug_dir / "report.json"
    asr_srt = debug_dir / "debug_asr.srt"
    ocr_srt = debug_dir / "debug_ocr.srt"
    ocr_cleaned_srt = debug_dir / "debug_ocr_cleaned.srt"
    fusion_json = debug_dir / "debug_fusion.json"
    corrected_srt = debug_dir / "debug_ai_corrected.srt"
    reference_final_srt = debug_dir / "debug_reference_final.srt"
    final_qa_json = debug_dir / "debug_final_qa.json"
    glossary_json = debug_dir / "debug_context_glossary.json"
    correction_json = debug_dir / "debug_correction.json"

    progress(1, f"Setup Auto Hybrid SRT: video={video.name}, output={out.name}, debug={debug_dir.name}")

    cmd = [
        str(PY),
        str(EXTRACT_SCRIPT),
        "--input",
        str(video),
        "--output",
        str(asr_srt),
        "--model",
        args.model,
        "--language",
        args.language,
        "--device",
        args.device,
        "--engine",
        args.engine,
    ]
    code = run_streaming(cmd, logs, 5, 72, "ASR voice")
    if code != 0:
        raise RuntimeError(f"ASR failed, xem log: {logs}")

    ocr_status = "skipped"
    ocr_quality = {"status": "not_run"}
    ocr_cleanup = {"status": "not_run"}
    ocr_tool = videocr_available()
    if args.enable_ocr and ocr_tool:
        progress(73, "OCR hard-sub: phát hiện VideOCR, chuẩn bị chạy debug OCR")
        # Current integration keeps OCR optional/debug. Full fusion will use this output once
        # VideOCR runtime is installed and crop parameters are validated in the app.
        if ocr_tool.endswith(".py"):
            ocr_cmd = [
                str(PY),
                ocr_tool,
                "--video_path",
                str(video),
                "--output",
                str(ocr_srt),
                "--ocr_engine",
                "paddleocr",
                "--lang",
                "ch",
                "--frames_to_skip",
                "1",
                "--conf_threshold",
                "70",
                "--sim_threshold",
                "82",
                "--ssim_threshold",
                "88",
                "--max_merge_gap",
                "0.12",
                "--subtitle_position",
                "center",
                "--normalize_to_simplified_chinese",
                "true",
                "--post_processing",
                "true",
                "--min_subtitle_duration",
                "0.15",
                "--ocr_image_max_width",
                "960",
            ]
        else:
            ocr_cmd = [
                ocr_tool,
                "--video_path",
                str(video),
                "--output",
                str(ocr_srt),
                "--ocr_engine",
                "paddleocr",
                "--lang",
                "ch",
                "--frames_to_skip",
                "1",
                "--conf_threshold",
                "70",
                "--sim_threshold",
                "82",
                "--ssim_threshold",
                "88",
                "--max_merge_gap",
                "0.12",
                "--subtitle_position",
                "center",
                "--normalize_to_simplified_chinese",
                "true",
                "--post_processing",
                "true",
                "--min_subtitle_duration",
                "0.15",
                "--ocr_image_max_width",
                "960",
            ]
        ocr_code = run_streaming(ocr_cmd, logs, 73, 90, "OCR hard-sub")
        ocr_status = "ok" if ocr_code == 0 and ocr_srt.exists() else f"failed:{ocr_code}"
        if ocr_srt.exists() and asr_srt.exists():
            ocr_cleanup = clean_ocr_srt_with_asr(ocr_srt, asr_srt, ocr_cleaned_srt)
            if ocr_cleaned_srt.exists() and ocr_cleaned_srt.stat().st_size > 0:
                ocr_srt = ocr_cleaned_srt
                progress(90, f"OCR cleanup: removed={ocr_cleanup.get('changed', 0)} artifact cues")
        ocr_quality = srt_quality_stats(ocr_srt) if ocr_srt.exists() else {"status": ocr_status}
    elif args.enable_ocr:
        ocr_status = "unavailable"
        progress(78, "OCR hard-sub: chưa thấy videocr-cli, bỏ qua OCR và dùng ASR làm final")
    else:
        progress(78, "OCR hard-sub: tạm tắt, dùng ASR làm final")

    progress(91, "Fusion: tạo final.srt từ nguồn tốt nhất hiện có")
    ai_status = {"status": "disabled"}
    asr_quality = srt_quality_stats(asr_srt)
    ocr_usable, ocr_reason, ocr_quality = is_usable_ocr_srt(ocr_srt, asr_quality) if ocr_srt.exists() else (False, "missing", ocr_quality)
    final_input = ocr_srt if ocr_usable else asr_srt
    final_source = "asr"
    text_source = "asr"
    timing_source = "asr_word_timestamps"
    if ocr_usable:
        final_source = "ocr_hardsub"
        text_source = "ocr_hardsub"
        timing_source = "videocr_frame_timestamps"
        progress(90, f"OCR hard-sub: promoted to final, cues={ocr_quality.get('cues')}")
    elif args.enable_ocr:
        progress(89, f"OCR hard-sub: rejected ({ocr_reason}), using ASR/AI fallback")
    if args.ai_correct and args.reference_srt:
        ai_status = {"status": "skipped", "reason": "reference_srt_selected"}
    elif args.ai_correct:
        try:
            correction_source = final_source
            ai_status = run_ai_correction(
                asr_srt=final_input,
                corrected_srt=corrected_srt,
                glossary_path=glossary_json,
                correction_path=correction_json,
                logs=logs,
                endpoint=args.ai_endpoint,
                model=args.ai_model,
                block_size=args.ai_block_size,
                timeout=args.ai_timeout,
                secondary_srt=asr_srt if correction_source == "ocr_hardsub" else None,
                progress_cb=progress,
            )
            if corrected_srt.exists() and corrected_srt.stat().st_size > 0:
                final_input = corrected_srt
                final_source = "ai_corrected_ocr" if correction_source == "ocr_hardsub" else "ai_corrected_asr"
                text_source = "local_ai_context_correction_over_ocr" if correction_source == "ocr_hardsub" else "local_ai_context_correction"
                progress(90, f"AI correction: ok, changed={ai_status.get('changed', 0)}")
        except Exception as exc:
            ai_status = {"status": "failed", "error": str(exc)}
            progress(88, f"AI correction failed, fallback ASR: {exc}")
            if args.require_ai:
                raise RuntimeError(f"AI correction is required but failed: {exc}") from exc

    reference_status = {"status": "disabled"}
    if args.reference_srt:
        try:
            reference_status = copy_reference_srt(args.reference_srt.resolve(), reference_final_srt)
            final_input = reference_final_srt
            final_source = "reference_srt"
            text_source = "trusted_reference_srt"
            timing_source = "trusted_reference_srt"
            progress(90, f"Reference SRT: ok, cues={reference_status.get('cues')}")
        except Exception as exc:
            reference_status = {"status": "failed", "error": str(exc)}
            progress(88, f"Reference SRT failed, fallback current final: {exc}")

    final_safety = write_sanitized_final(final_input, out)
    qa_sources = []
    if ocr_cleaned_srt.exists():
        qa_sources.append(ocr_cleaned_srt)
    if asr_srt.exists():
        qa_sources.append(asr_srt)
    if (debug_dir / "debug_ocr.srt").exists():
        qa_sources.append(debug_dir / "debug_ocr.srt")
    final_qa = final_qa_repair(out, qa_sources, final_qa_json)
    if final_qa.get("inserted_count", 0) or final_qa.get("suspicious_count", 0):
        progress(92, f"Final QA: restored={final_qa.get('inserted_count', 0)}, suspicious={final_qa.get('suspicious_count', 0)}")
    fusion = {
        "mode": "auto_hybrid_v2_ocr_first_guarded_ai",
        "final_source": final_source,
        "timing_source": timing_source,
        "text_source": text_source,
        "ocr_status": ocr_status,
        "ocr_reason": ocr_reason,
        "asr_quality": asr_quality,
        "ocr_quality": ocr_quality,
        "ocr_cleanup": ocr_cleanup,
        "ai_status": ai_status,
        "reference_status": reference_status,
        "final_safety": final_safety,
        "final_qa": final_qa,
        "notes": [
            "OCR hard-sub is promoted to final when quality checks pass.",
            "Local AI correction is guarded; unsafe rewrites, summaries, and internal notes are rejected.",
            "Final SRT always runs deterministic cleanup and QA repair before export.",
        ],
    }
    fusion_json.write_text(json.dumps(fusion, ensure_ascii=False, indent=2), encoding="utf-8")

    final_count = parse_srt_count(out)
    report = {
        "input": str(video),
        "output": str(out),
        "debug_dir": str(debug_dir),
        "language": args.language,
        "asr_model": args.model,
        "asr_engine": args.engine,
        "device": args.device,
        "ocr_status": ocr_status,
        "ocr_reason": ocr_reason,
        "ocr_quality": ocr_quality,
        "ocr_cleanup": ocr_cleanup,
        "asr_quality": asr_quality,
        "ai_status": ai_status,
        "reference_status": reference_status,
        "final_safety": final_safety,
        "final_qa": final_qa,
        "final_cues": final_count,
        "elapsed_seconds": round(time.time() - started, 2),
        "files": {
            "final": str(out),
            "debug_asr": str(asr_srt),
            "debug_ocr": str(ocr_srt) if ocr_srt.exists() else "",
            "debug_ocr_raw": str(debug_dir / "debug_ocr.srt") if (debug_dir / "debug_ocr.srt").exists() else "",
            "debug_ocr_cleaned": str(ocr_cleaned_srt) if ocr_cleaned_srt.exists() else "",
            "debug_ai_corrected": str(corrected_srt) if corrected_srt.exists() else "",
            "debug_reference_final": str(reference_final_srt) if reference_final_srt.exists() else "",
            "debug_final_qa": str(final_qa_json) if final_qa_json.exists() else "",
            "debug_context_glossary": str(glossary_json) if glossary_json.exists() else "",
            "debug_correction": str(correction_json) if correction_json.exists() else "",
            "debug_fusion": str(fusion_json),
            "logs": str(logs),
        },
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    progress(100, f"Done: final={out.name}, cues={final_count}, elapsed={fmt(time.time() - started)}")
    log(f"FINAL_SRT:{out}")
    log(f"DEBUG_DIR:{debug_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR:{exc}", file=sys.stderr, flush=True)
        raise
