import argparse
import base64
import hashlib
import json
import math
import os
import re
import random
import string
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List

try:
    import yt_dlp
except ModuleNotFoundError:
    print("[ERR] Thiếu yt-dlp. Mở Cài đặt và bấm Tải full tài nguyên thiếu.", flush=True)
    raise SystemExit(6)
import requests
from yt_dlp.extractor.bilibili import BilibiliSpaceVideoIE

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


ROOT = Path(__file__).resolve().parent.parent
FFMPEG = ROOT / "ffmpeg.exe"
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".flv", ".mov"}
WBI_MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}


def clean_name(value: str) -> str:
    value = repair_text(value)
    value = re.sub(r"[\r\n\t]+", " ", str(value or "video")).strip()
    value = re.sub(r'[\\/*?:"<>|]', "", value)
    return re.sub(r"\s{2,}", " ", value)[:110] or "video"


def repair_text(value: str) -> str:
    text = str(value or "")
    if any(marker in text for marker in ("ã", "æ", "å", "é", "ç", "ð")):
        try:
            repaired = text.encode("latin1").decode("utf-8")
            if repaired:
                return repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return text


def repair_text(value: str) -> str:
    text = str(value or "")
    markers = ("Ã", "ã", "Ä", "ä", "Å", "å", "Æ", "æ", "Ç", "ç", "È", "è", "É", "é", "Ê", "ê", "Ë", "ë", "¼", "½", "€")
    if any(marker in text for marker in markers):
        try:
            repaired = text.encode("latin1").decode("utf-8")
            if repaired and repaired != text:
                return repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return text


def read_links(raw: str) -> List[str]:
    raw = str(raw or "").strip()
    if os.path.isfile(raw):
        raw = Path(raw).read_text(encoding="utf-8-sig", errors="ignore")
    links = []
    for line in raw.splitlines():
        match = re.search(r"https?://[^\s]+", line)
        if match:
            links.append(match.group(0).rstrip("，。),]"))
    return list(dict.fromkeys(links))


def normalize_url(url: str, mode: str) -> str:
    url = str(url or "").strip()
    if mode == "channel" and re.fullmatch(r"https?://space\.bilibili\.com/\d+/?(?:\?.*)?", url):
        return url.split("?", 1)[0].rstrip("/") + "/video"
    return url


def flatten_entries(info: Dict) -> Iterable[Dict]:
    if not isinstance(info, dict):
        return
    entries = info.get("entries")
    if entries is not None:
        for entry in entries:
            if entry:
                yield from flatten_entries(entry)
        return
    yield info


def page_url(info: Dict) -> str:
    url = str(info.get("webpage_url") or info.get("original_url") or info.get("url") or "")
    video_id = str(info.get("id") or "")
    if video_id.startswith("BV") and "bilibili.com" not in url:
        return f"https://www.bilibili.com/video/{video_id}"
    return url


def channel_mid(url: str) -> str:
    match = re.search(r"space\.bilibili\.com/(\d+)", url)
    return match.group(1) if match else ""


def manifest_row(info: Dict, index: int) -> Dict:
    thumbnails = info.get("thumbnails") or []
    cover = str(info.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else "") or "")
    height = int(info.get("height") or 0)
    fps = info.get("fps")
    quality = f"{height}p" if height else "Tốt nhất khi tải"
    if height and fps:
        quality += f" {round(float(fps))}fps"
    return {
        "index": index,
        "title": clean_name(info.get("title") or info.get("fulltitle") or info.get("id") or f"Video {index}"),
        "url": page_url(info),
        "aweme_id": str(info.get("id") or index),
        "source": "bilibili",
        "no_watermark": True,
        "cover": cover,
        "duration": float(info.get("duration") or 0),
        "uploader": repair_text(info.get("uploader") or info.get("channel") or ""),
        "quality": quality,
    }


def write_manifest(rows: List[Dict], output: Path, source_label: str) -> int:
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "videos_manifest.json"
    manifest.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if not rows:
        print("[ERR] Không lấy được video Bilibili công khai nào.", flush=True)
        return 3
    print(f"[META] Lấy được {len(rows)} video Bilibili ({source_label}). Manifest: {manifest}", flush=True)
    return 0


def flat_channel_rows(url: str) -> List[Dict]:
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "ignoreerrors": True,
        "playlistend": None,
        "socket_timeout": 45,
        "retries": 4,
        "http_headers": HEADERS,
    }
    rows: List[Dict] = []
    seen = set()
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(normalize_url(url, "channel"), download=False)
        for entry in flatten_entries(info):
            row = manifest_row(entry, len(rows) + 1)
            key = row["aweme_id"] or row["url"]
            if not row["url"] or key in seen:
                continue
            row["source"] = "bilibili_channel_flat"
            row["quality"] = "Tốt nhất khi tải"
            seen.add(key)
            rows.append(row)
            if len(rows) % 50 == 0:
                print(f"[PROGRESS] Fallback đã quét {len(rows)} video Bilibili...", flush=True)
    return rows


def wbi_key(session: requests.Session) -> str:
    data = session.get("https://api.bilibili.com/x/web-interface/nav", timeout=30).json()["data"]["wbi_img"]
    raw = "".join(Path(urllib.parse.urlparse(data[key]).path).stem for key in ("img_url", "sub_url"))
    return "".join(raw[index] for index in WBI_MIXIN_TABLE)[:32]


def sign_wbi(params: Dict, key: str) -> Dict:
    params = {**params, "wts": round(time.time())}
    params = {
        name: "".join(char for char in str(value) if char not in "!'()*")
        for name, value in sorted(params.items())
    }
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5(f"{query}{key}".encode()).hexdigest()
    return params


def scan_channel_wbi(url: str, output: Path) -> int:
    match = re.search(r"space\.bilibili\.com/(\d+)", url)
    if not match:
        print(f"[ERR] Link trang cá nhân Bilibili không hợp lệ: {url}", flush=True)
        return 2
    mid = match.group(1)
    rows: List[Dict] = []
    page = 1
    page_count = 1
    options = {"quiet": True, "no_warnings": True, "socket_timeout": 45, "retries": 4, "http_headers": HEADERS}
    with yt_dlp.YoutubeDL(options) as ydl:
        extractor = BilibiliSpaceVideoIE(ydl)
        while page <= page_count:
            random_text = lambda low, high: "".join(random.choices(string.printable, k=random.randint(low, high)))
            query = {
                "keyword": "",
                "mid": mid,
                "order": "pubdate",
                "order_avoided": "true",
                "platform": "web",
                "pn": page,
                "ps": 30,
                "tid": 0,
                "web_location": 1550101,
                "dm_img_list": "[]",
                "dm_img_str": base64.b64encode(random_text(16, 64).encode())[:-2].decode(),
                "dm_cover_img_str": base64.b64encode(random_text(32, 128).encode())[:-2].decode(),
                "dm_img_inter": '{"ds":[],"wh":[6093,6631,31],"of":[430,760,380]}',
            }
            try:
                response = extractor._download_json(
                    "https://api.bilibili.com/x/space/wbi/arc/search",
                    mid,
                    query=extractor._sign_wbi(query, mid),
                    headers={"Referer": url},
                    note=f"Quét trang Bilibili {page}",
                )
            except Exception as exc:
                print(f"[ERR] Bilibili chặn trang {page}: {exc}", flush=True)
                return 5
            if int(response.get("code") or 0) != 0:
                print(f"[ERR] Bilibili chặn trang {page}: code={response.get('code')} {response.get('message')}", flush=True)
                return 5
            payload = response.get("data") or {}
            page_info = payload.get("page") or {}
            total = int(page_info.get("count") or 0)
            page_count = max(1, math.ceil(total / 30))
            for item in ((payload.get("list") or {}).get("vlist") or []):
                bvid = str(item.get("bvid") or "")
                if not bvid:
                    continue
                rows.append({
                    "index": len(rows) + 1,
                    "title": clean_name(item.get("title") or bvid),
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "aweme_id": bvid,
                    "source": "bilibili_channel_wbi",
                    "no_watermark": True,
                    "cover": str(item.get("pic") or ""),
                    "duration": 0,
                    "uploader": repair_text(item.get("author") or ""),
                    "quality": "Tốt nhất khi tải",
                })
            print(f"[PROGRESS] Đã quét {len(rows)}/{total} video Bilibili", flush=True)
            page += 1
            if page <= page_count:
                time.sleep(1.2)
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "videos_manifest.json"
    manifest.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if not rows:
        print("[ERR] Trang cá nhân Bilibili không có video công khai.", flush=True)
        return 3
    print(f"[META] Lấy đủ {len(rows)}/{len(rows)} video Bilibili. Manifest: {manifest}", flush=True)
    return 0


def scan_channel_stable(url: str, output: Path) -> int:
    mid = channel_mid(url)
    if not mid:
        print(f"[ERR] Link trang cá nhân Bilibili không hợp lệ: {url}", flush=True)
        return 2
    rows: List[Dict] = []
    page = 1
    page_count = 1
    options = {"quiet": True, "no_warnings": True, "socket_timeout": 45, "retries": 4, "http_headers": HEADERS}
    with yt_dlp.YoutubeDL(options) as ydl:
        extractor = BilibiliSpaceVideoIE(ydl)
        while page <= page_count:
            random_text = lambda low, high: "".join(random.choices(string.printable, k=random.randint(low, high)))
            query = {
                "keyword": "",
                "mid": mid,
                "order": "pubdate",
                "order_avoided": "true",
                "platform": "web",
                "pn": page,
                "ps": 30,
                "tid": 0,
                "web_location": 1550101,
                "dm_img_list": "[]",
                "dm_img_str": base64.b64encode(random_text(16, 64).encode())[:-2].decode(),
                "dm_cover_img_str": base64.b64encode(random_text(32, 128).encode())[:-2].decode(),
                "dm_img_inter": '{"ds":[],"wh":[6093,6631,31],"of":[430,760,380]}',
            }
            response = None
            last_error = None
            for attempt in range(1, 4):
                try:
                    response = extractor._download_json(
                        "https://api.bilibili.com/x/space/wbi/arc/search",
                        mid,
                        query=extractor._sign_wbi(query, mid),
                        headers={**HEADERS, "Referer": url},
                        note=f"Quét trang Bilibili {page}",
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    delay = 4 * attempt + random.random() * 2
                    print(f"[WARN] Bilibili chặn metadata trang {page}, thử lại {attempt}/3 sau {delay:.1f}s: {exc}", flush=True)
                    time.sleep(delay)
            if response is None:
                print(f"[WARN] Không lấy được metadata đẹp trang {page}: {last_error}", flush=True)
                break
            if int(response.get("code") or 0) != 0:
                print(f"[WARN] Bilibili chặn metadata trang {page}: code={response.get('code')} {response.get('message')}", flush=True)
                break
            payload = response.get("data") or {}
            page_info = payload.get("page") or {}
            total = int(page_info.get("count") or 0)
            page_count = max(1, math.ceil(total / 30))
            for item in ((payload.get("list") or {}).get("vlist") or []):
                bvid = str(item.get("bvid") or "")
                if not bvid:
                    continue
                rows.append({
                    "index": len(rows) + 1,
                    "title": clean_name(item.get("title") or bvid),
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "aweme_id": bvid,
                    "source": "bilibili_channel_wbi",
                    "no_watermark": True,
                    "cover": str(item.get("pic") or ""),
                    "duration": 0,
                    "uploader": repair_text(item.get("author") or ""),
                    "quality": "Tốt nhất khi tải",
                })
            print(f"[PROGRESS] Đã quét {len(rows)}/{total} video Bilibili", flush=True)
            page += 1
            if page <= page_count:
                time.sleep(2.0 + random.random())
    if rows and page > page_count:
        return write_manifest(rows, output, "metadata đầy đủ")

    print("[WARN] Bilibili đang giới hạn API metadata trang cá nhân. Chuyển sang quét fallback để không bị kẹt.", flush=True)
    try:
        fallback_rows = flat_channel_rows(url)
    except Exception as exc:
        fallback_rows = []
        print(f"[WARN] Fallback trang cá nhân cũng bị chặn: {exc}", flush=True)
    if fallback_rows:
        rich_by_url = {row["url"]: row for row in rows}
        merged = []
        seen_urls = set()
        for row in fallback_rows:
            chosen = rich_by_url.get(row["url"], row)
            if chosen["url"] in seen_urls:
                continue
            seen_urls.add(chosen["url"])
            chosen["index"] = len(merged) + 1
            merged.append(chosen)
        for row in rows:
            if row["url"] in seen_urls:
                continue
            seen_urls.add(row["url"])
            row["index"] = len(merged) + 1
            merged.append(row)
        return write_manifest(merged, output, "fallback đầy đủ, preview có thể hạn chế")
    if rows:
        print("[WARN] Chỉ lấy được một phần metadata. Tool vẫn ghi manifest phần đã lấy để người dùng kiểm tra.", flush=True)
        return write_manifest(rows, output, "metadata một phần")
    print("[ERR] Bilibili chặn cả metadata lẫn fallback trang cá nhân. Thử lại sau vài phút hoặc dùng link playlist/video lẻ.", flush=True)
    return 5


def scan(links: List[str], mode: str, output: Path) -> int:
    if mode == "channel" and len(links) == 1:
        return scan_channel_stable(normalize_url(links[0], mode), output)
    options = {
        "quiet": True,
        "no_warnings": False,
        "skip_download": True,
        "extract_flat": False,
        "ignoreerrors": True,
        "noplaylist": mode == "videos",
        "playlistend": None,
        "socket_timeout": 45,
        "retries": 4,
        "http_headers": HEADERS,
    }
    rows: List[Dict] = []
    seen = set()
    with yt_dlp.YoutubeDL(options) as ydl:
        for raw_url in links:
            url = normalize_url(raw_url, mode)
            print(f"[SCAN] {url}", flush=True)
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as exc:
                print(f"[WARN] Không quét được {url}: {exc}", flush=True)
                continue
            for entry in flatten_entries(info):
                row = manifest_row(entry, len(rows) + 1)
                key = row["aweme_id"] or row["url"]
                if not row["url"] or key in seen:
                    continue
                seen.add(key)
                rows.append(row)
                if len(rows) % 30 == 0:
                    print(f"[PROGRESS] Đã quét {len(rows)} video...", flush=True)
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "videos_manifest.json"
    manifest.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if not rows:
        print("[ERR] Không lấy được video Bilibili công khai nào.", flush=True)
        return 3
    print(f"[META] Lấy được {len(rows)} video Bilibili. Manifest: {manifest}", flush=True)
    return 0


def load_manifest(path: Path) -> List[Dict]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return [row for row in data if isinstance(row, dict) and row.get("url")]


def download_one(row: Dict, index: int, total: int, output: Path) -> tuple:
    title = clean_name(row.get("title") or row.get("aweme_id") or f"Video {index}")
    video_id = clean_name(row.get("aweme_id") or "")
    stem = f"{index:04d} - {title}" + (f" [{video_id}]" if video_id else "")
    before = {path.resolve() for path in output.glob(f"{index:04d} - *") if path.is_file()}

    progress_state = {"key": "", "percent": -10}

    def progress_hook(data: Dict) -> None:
        status = data.get("status")
        if status == "downloading":
            key = str(data.get("filename") or data.get("tmpfilename") or "")
            total_bytes = float(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            downloaded = float(data.get("downloaded_bytes") or 0)
            numeric_percent = int(downloaded * 100 / total_bytes) if total_bytes > 0 else -1
            if key != progress_state["key"]:
                progress_state.update({"key": key, "percent": -10})
            if numeric_percent >= 0 and numeric_percent < 100 and numeric_percent - progress_state["percent"] < 5:
                return
            if numeric_percent >= 0:
                progress_state["percent"] = numeric_percent
            percent = str(data.get("_percent_str") or "").strip()
            speed = str(data.get("_speed_str") or "").strip()
            eta = str(data.get("_eta_str") or "").strip()
            print(f"[PROGRESS] {index}/{total} {percent} | {speed} | ETA {eta}", flush=True)
        elif status == "finished":
            print(f"[MUX] {index}/{total} Đã tải stream, đang ghép video + audio...", flush=True)

    options = {
        "format": "bestvideo*+bestaudio/best",
        "format_sort": ["res", "fps", "hdr:12", "vcodec", "br"],
        "outtmpl": str(output / f"{stem}.%(ext)s"),
        "merge_output_format": "mp4",
        "ffmpeg_location": str(FFMPEG) if FFMPEG.exists() else None,
        "noplaylist": True,
        "playlist_items": "1",
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 60,
        "continuedl": True,
        "overwrites": False,
        "windowsfilenames": True,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "noprogress": True,
        "no_warnings": False,
    }
    print(f"[DOWNLOAD] {index}/{total}: {title}", flush=True)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.extract_info(str(row["url"]), download=True)
        after = [
            path for path in output.glob(f"{index:04d} - *")
            if path.is_file() and path.resolve() not in before and path.suffix.lower() in VIDEO_EXTENSIONS
        ]
        if not after:
            after = [path for path in output.glob(f"{index:04d} - *") if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS]
        if not after:
            raise RuntimeError("yt-dlp hoàn tất nhưng không thấy file video")
        best = max(after, key=lambda path: path.stat().st_size)
        height = int((result or {}).get("height") or 0)
        return True, f"[OK] {index}/{total}: {best.name}" + (f" | {height}p" if height else "")
    except Exception as exc:
        return False, f"[FAIL] {index}/{total}: {title}: {exc}"


def download(rows: List[Dict], output: Path, threads: int) -> int:
    output.mkdir(parents=True, exist_ok=True)
    total = len(rows)
    workers = max(1, min(4, int(threads or 3)))
    print(f"[START] Tải {total} video Bilibili | {workers} luồng | chất lượng tốt nhất khả dụng", flush=True)
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download_one, row, index, total, output): index for index, row in enumerate(rows, 1)}
        for future in as_completed(futures):
            success, message = future.result()
            print(message, flush=True)
            ok += int(success)
            print(f"[TOTAL] Hoàn thành {ok}/{total} video", flush=True)
    print(f"[DONE] Thành công {ok}/{total} video Bilibili", flush=True)
    return 0 if ok == total else 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bilibili downloader using yt-dlp.")
    parser.add_argument("--mode", choices=["videos", "collections", "channel"], default="videos")
    parser.add_argument("--links")
    parser.add_argument("--manifest")
    parser.add_argument("--output", required=True)
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--list-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output).resolve()
    if args.manifest:
        rows = load_manifest(Path(args.manifest))
        if not rows:
            print("[ERR] Manifest Bilibili không có video.", flush=True)
            return 3
        return 0 if args.list_only else download(rows, output, args.threads)
    links = read_links(args.links or "")
    if not links:
        print("[ERR] Chưa có link Bilibili hợp lệ.", flush=True)
        return 2
    return scan(links, args.mode, output)


if __name__ == "__main__":
    raise SystemExit(main())
