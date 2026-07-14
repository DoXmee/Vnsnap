import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


APP_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(APP_DIR, "videos")
DEFAULT_DOWNLOAD_THREADS = 6
MAX_THREADS = 8
REQUEST_TIMEOUT = 35

DOUYIN_HOME = "https://www.douyin.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}


def default_session_dir() -> str:
    """Pick an existing Douyin browser profile when possible, otherwise create one next to this script."""
    env_dir = os.environ.get("DOUYIN_SESSION_DIR", "").strip()
    candidates = [
        env_dir,
        os.path.join(APP_DIR, "douyin_session"),
        os.path.join(os.path.dirname(APP_DIR), "douyin_session"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return os.path.join(APP_DIR, "douyin_session")


SESSION_DIR = default_session_dir()
CHANNEL_SCAN_INCOMPLETE = False


@dataclass
class VideoItem:
    title: str
    url: str
    aweme_id: str = ""
    source: str = ""
    no_watermark: bool = True
    cover: str = ""


def clean_name(name: str) -> str:
    name = re.sub(r"[\r\n\t]+", " ", str(name or "video")).strip()
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s{2,}", " ", name)
    return (name or "video")[:100]


def unique_path(directory: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    path = os.path.join(directory, filename)
    counter = 1
    while os.path.exists(path):
        path = os.path.join(directory, f"{base} ({counter}){ext}")
        counter += 1
    return path


def ensure_output(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def find_ffmpeg() -> str:
    candidates = [
        os.environ.get("FFMPEG_BIN", ""),
        os.path.join(APP_DIR, "ffmpeg.exe"),
        os.path.join(os.path.dirname(APP_DIR), "ffmpeg.exe"),
        os.path.join(os.path.dirname(os.path.dirname(APP_DIR)), "ffmpeg.exe"),
        shutil.which("ffmpeg") or "",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


def session_status() -> Dict:
    exists = os.path.exists(SESSION_DIR)
    files = 0
    size = 0
    if exists:
        for root, _, names in os.walk(SESSION_DIR):
            files += len(names)
            for name in names:
                try:
                    size += os.path.getsize(os.path.join(root, name))
                except Exception:
                    pass
    return {"session_dir": SESSION_DIR, "exists": exists, "files": files, "size_mb": round(size / 1024 / 1024, 1)}


def clear_session() -> None:
    if os.path.exists(SESSION_DIR):
        shutil.rmtree(SESSION_DIR, ignore_errors=True)
    print(f"[SESSION] Đã xóa session: {SESSION_DIR}")


def install_deps() -> int:
    """Install downloader dependencies for the current Python interpreter."""
    cmds = [
        [sys.executable, "-m", "pip", "install", "--upgrade", "requests", "playwright"],
        [sys.executable, "-m", "playwright", "install", "chromium"],
    ]
    for cmd in cmds:
        print("[SETUP]", " ".join(cmd))
        code = subprocess.call(cmd)
        if code != 0:
            return code
    print("[SETUP] Douyin downloader dependencies OK")
    return 0


def normalize_url(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r"https?://[^\s]+", text)
    if m:
        text = m.group(0)
    return text.rstrip("，。),]")


def read_links_from_text_or_file(raw: str) -> List[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    if os.path.exists(raw):
        with open(raw, "r", encoding="utf-8-sig", errors="ignore") as f:
            raw = f.read()
    links = []
    for line in re.split(r"[\r\n]+", raw):
        url = normalize_url(line)
        if url:
            links.append(url)
    return list(dict.fromkeys(links))


def request_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def resolve_url(url: str, session: Optional[requests.Session] = None) -> str:
    session = session or request_session()
    try:
        r = session.get(url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        return r.url or url
    except Exception:
        return url


def extract_aweme_id(url: str) -> str:
    url = normalize_url(url)
    patterns = [
        r"/video/(\d+)",
        r"/note/(\d+)",
        r"aweme_id=(\d+)",
        r"modal_id=(\d+)",
        r"video_id=(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return ""


def extract_mix_id(url: str) -> str:
    for pat in [r"/collection/(\d+)", r"mix_id=(\d+)"]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return ""


def extract_sec_uid(url: str) -> str:
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    for key in ["sec_user_id", "sec_uid"]:
        if q.get(key):
            return q[key][0]
    m = re.search(r"/user/([^/?#]+)", parsed.path)
    if m:
        return unquote(m.group(1))
    return ""


def best_video_url(aweme: Dict) -> Tuple[str, bool]:
    video = aweme.get("video") or {}
    play_uri = str(
        (video.get("play_addr_h264") or {}).get("uri")
        or (video.get("play_addr") or {}).get("uri")
        or ""
    ).strip()
    if play_uri:
        return (
            f"https://aweme.snssdk.com/aweme/v1/play/?video_id={quote(play_uri)}&ratio=1080p&line=0",
            True,
        )
    candidates = []
    for key in ["play_addr_h264", "play_addr_265", "play_addr"]:
        value = video.get(key) or {}
        urls = value.get("url_list") or []
        if urls:
            width = int(value.get("width") or video.get("width") or 0)
            height = int(value.get("height") or video.get("height") or 0)
            size = int(value.get("data_size") or 0)
            codec_bonus = 1 if key == "play_addr_h264" else 0
            candidates.append(((width * height, size, codec_bonus), urls[0]))
    for row in video.get("bit_rate") or []:
        value = row.get("play_addr") or {}
        urls = value.get("url_list") or []
        if urls:
            width = int(value.get("width") or 0)
            height = int(value.get("height") or 0)
            bitrate = int(row.get("bit_rate") or 0)
            candidates.append(((width * height, bitrate, 0), urls[0]))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1], True
    urls = ((video.get("download_addr") or {}).get("url_list")) or []
    if urls:
        return urls[0], False
    return "", False


def aweme_to_video(aweme: Dict, source: str) -> Optional[VideoItem]:
    url, no_watermark = best_video_url(aweme)
    if not url:
        return None
    title = aweme.get("desc") or aweme.get("share_info", {}).get("share_title") or aweme.get("aweme_id") or "video"
    cover = ""
    video = aweme.get("video") or {}
    for key in ["cover", "origin_cover", "dynamic_cover"]:
        urls = ((video.get(key) or {}).get("url_list")) or []
        if urls:
            cover = urls[0]
            break
    return VideoItem(
        title=clean_name(title),
        url=url,
        aweme_id=str(aweme.get("aweme_id") or ""),
        source=source,
        no_watermark=no_watermark,
        cover=cover,
    )


def parse_render_data(html: str) -> Dict:
    for marker in ["RENDER_DATA", "ROUTER_DATA"]:
        m = re.search(rf'<script id="{marker}" type="application/json">(.*?)</script>', html, re.S)
        if not m:
            continue
        try:
            return json.loads(unquote(m.group(1)))
        except Exception:
            pass
    return {}


def walk_dict(obj) -> Iterable[Dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_dict(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_dict(v)


def find_awemes_in_json(data: Dict) -> List[Dict]:
    found = []
    seen = set()
    for node in walk_dict(data):
        if "aweme_id" in node and "video" in node:
            aid = str(node.get("aweme_id") or "")
            if aid and aid not in seen:
                seen.add(aid)
                found.append(node)
    return found


def direct_detail(aweme_id: str, session: Optional[requests.Session] = None) -> Optional[VideoItem]:
    session = session or request_session()
    api_urls = [
        f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}",
        f"https://www.iesdouyin.com/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}",
    ]
    for api in api_urls:
        try:
            r = session.get(api, timeout=REQUEST_TIMEOUT)
            data = r.json()
            aweme = data.get("aweme_detail") or data.get("aweme") or {}
            item = aweme_to_video(aweme, "direct_detail")
            if item:
                return item
        except Exception:
            continue
    return None


def direct_html_video(url: str, session: Optional[requests.Session] = None) -> Optional[VideoItem]:
    session = session or request_session()
    resolved = resolve_url(url, session)
    aweme_id = extract_aweme_id(resolved)
    if aweme_id:
        item = direct_detail(aweme_id, session)
        if item:
            return item
    try:
        html = session.get(resolved, timeout=REQUEST_TIMEOUT).text
        data = parse_render_data(html)
        for aweme in find_awemes_in_json(data):
            item = aweme_to_video(aweme, "direct_html")
            if item:
                return item
    except Exception:
        return None
    return None


def browser_context(headless: bool = True):
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError("Thiếu Playwright. Bấm nút Cài/repair dependency trước.") from exc
    p = sync_playwright().start()
    context = p.chromium.launch_persistent_context(
        user_data_dir=SESSION_DIR,
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
    )
    context.set_default_timeout(90_000)
    page = context.new_page()
    return p, context, page


def ensure_login_if_needed(page, context, headless: bool):
    is_first_login = not os.path.exists(SESSION_DIR) or not os.listdir(SESSION_DIR)
    if headless and not is_first_login:
        return
    try:
        page.goto(DOUYIN_HOME, wait_until="domcontentloaded", timeout=90_000)
    except Exception as e:
        print(f"[WARN] Mở Douyin lỗi: {e}")
    if is_first_login:
        print("[LOGIN] Lần đầu cần đăng nhập Douyin trong browser để lưu session.")
        if headless:
            context.close()
            raise RuntimeError("Chưa có session. Chạy lại với --show-browser để login lần đầu.")
        input("Login xong nhấn ENTER để lưu session...")
    else:
        print("[SESSION] Dùng session đã lưu.")


def login_only() -> int:
    p = context = page = None
    try:
        p, context, page = browser_context(headless=False)
        try:
            page.goto(DOUYIN_HOME, wait_until="domcontentloaded", timeout=90_000)
        except Exception as e:
            print(f"[WARN] Mở Douyin lỗi: {e}")
        print("[LOGIN] Đăng nhập Douyin trong browser. Tool sẽ tự lưu và đóng khi nhận được session.")
        login_cookie_names = {"sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt"}
        deadline = time.time() + 240
        while time.time() < deadline:
            cookies = context.cookies()
            names = {str(cookie.get("name") or "") for cookie in cookies}
            if names & login_cookie_names:
                print(f"[LOGIN] Đã nhận session Douyin ({len(cookies)} cookie).")
                break
            time.sleep(2)
        else:
            print("[WARN] Hết thời gian chờ đăng nhập; profile trình duyệt vẫn được giữ lại.")
        print(json.dumps(session_status(), ensure_ascii=False, indent=2))
        return 0
    finally:
        try:
            if context:
                context.close()
        finally:
            if p:
                p.stop()


def browser_fetch_json(page, url: str) -> Dict:
    return page.evaluate(
        """
        async (url) => {
            const res = await fetch(url, { credentials: 'include' });
            const text = await res.text();
            try { return JSON.parse(text); } catch (e) { return { _raw: text, _error: String(e) }; }
        }
        """,
        url,
    )


def browser_collection(page, mix_id: str) -> List[VideoItem]:
    print(f"[API] Collection mix_id={mix_id}")
    items: List[VideoItem] = []
    cursor = 0
    has_more = 1
    while has_more == 1:
        api = f"https://www-hj.douyin.com/aweme/v1/web/mix/aweme/?mix_id={mix_id}&cursor={cursor}&count=20"
        data = browser_fetch_json(page, api)
        awemes = data.get("aweme_list") or []
        for aweme in awemes:
            item = aweme_to_video(aweme, "browser_collection")
            if item:
                items.append(item)
        has_more = int(data.get("has_more") or 0)
        cursor = int(data.get("cursor") or 0)
        if not awemes:
            break
    return dedupe_items(items)


def browser_user_dom(page, profile_url: str) -> List[VideoItem]:
    global CHANNEL_SCAN_INCOMPLETE
    print("[FALLBACK] API trực tiếp bị chặn, bắt phân trang thật khi cuộn trang cá nhân...")
    api_items: Dict[str, VideoItem] = {}
    pagination = {"has_more": None, "cursor": None}

    def capture_post_response(response) -> None:
        if "/aweme/v1/web/aweme/post/" not in response.url:
            return
        try:
            data = response.json()
            pagination["has_more"] = data.get("has_more")
            pagination["cursor"] = data.get("max_cursor") or data.get("cursor")
            for aweme in data.get("aweme_list") or []:
                item = aweme_to_video(aweme, "browser_user_response")
                if item and item.aweme_id:
                    api_items[item.aweme_id] = item
        except Exception:
            pass

    page.on("response", capture_post_response)
    page.goto(profile_url, wait_until="domcontentloaded", timeout=90_000)
    time.sleep(2)
    found: Dict[str, VideoItem] = {}
    stagnant_rounds = 0
    last_height = 0
    for _ in range(200):
        rows = page.evaluate(
            """
            () => {
              const scoped = document.querySelectorAll(
                '[data-e2e="user-post-list"] a[href*="/video/"],'
                + '[data-e2e="user-post-item"] a[href*="/video/"]'
              );
              const anchors = scoped.length ? [...scoped] : [...document.querySelectorAll('a[href*="/video/"]')];
              return anchors.map(a => {
                const img = a.querySelector('img');
                const href = a.href || a.getAttribute('href') || '';
                const title = a.getAttribute('aria-label') || a.getAttribute('title')
                    || (img && (img.alt || img.getAttribute('aria-label'))) || a.innerText || '';
                const cover = img ? (img.currentSrc || img.src || '') : '';
                return { href, title: String(title).trim(), cover };
              });
            }
            """
        )
        before = len(found) + len(api_items)
        for row in rows or []:
            url = normalize_url(str(row.get("href") or ""))
            aweme_id = extract_aweme_id(url)
            if not url or not aweme_id:
                continue
            found[aweme_id] = VideoItem(
                title=clean_name(row.get("title") or f"Douyin {aweme_id}"),
                url=url,
                aweme_id=aweme_id,
                source="browser_user_dom",
                no_watermark=True,
                cover=str(row.get("cover") or ""),
            )
        height = int(page.evaluate("document.documentElement.scrollHeight") or 0)
        current = len(found) + len(api_items)
        stagnant_rounds = stagnant_rounds + 1 if current == before and height == last_height else 0
        last_height = height
        if stagnant_rounds >= 8:
            break
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        time.sleep(1.0)
    page.remove_listener("response", capture_post_response)
    if api_items:
        CHANNEL_SCAN_INCOMPLETE = str(pagination["has_more"]).lower() in {"1", "true"}
        print(
            f"[FALLBACK] Bắt được {len(api_items)} video từ phân trang của chính kênh "
            f"| has_more={pagination['has_more']} | cursor={pagination['cursor']}."
        )
        return list(api_items.values())
    print(f"[FALLBACK] Không bắt được response phân trang; DOM thấy {len(found)} video.")
    CHANNEL_SCAN_INCOMPLETE = True
    return list(found.values())


def browser_user(page, sec_uid: str, profile_url: str) -> List[VideoItem]:
    print(f"[API] User sec_uid={sec_uid[:16]}...")
    items: List[VideoItem] = []
    cursor = 0
    has_more = 1
    try:
        while has_more == 1:
            api = f"https://www.douyin.com/aweme/v1/web/aweme/post/?sec_user_id={quote(sec_uid)}&max_cursor={cursor}&count=18&locate_query=false&show_live_replay_strategy=1"
            data = browser_fetch_json(page, api)
            awemes = data.get("aweme_list") or []
            for aweme in awemes:
                item = aweme_to_video(aweme, "browser_user")
                if item:
                    items.append(item)
            has_more = int(data.get("has_more") or 0)
            cursor = int(data.get("max_cursor") or data.get("cursor") or 0)
            if not awemes:
                break
    except Exception as exc:
        print(f"[WARN] API danh sách Douyin không dùng được: {exc}")
    if not items:
        return browser_user_dom(page, profile_url)
    return dedupe_items(items)


def browser_video(page, url: str) -> Optional[VideoItem]:
    resolved = url
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        time.sleep(1)
        resolved = page.url
    except Exception:
        pass
    aweme_id = extract_aweme_id(resolved) or extract_aweme_id(url)
    if aweme_id:
        data = browser_fetch_json(page, f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}")
        item = aweme_to_video(data.get("aweme_detail") or {}, "browser_detail")
        if item:
            return item
    data = page.evaluate(
        """
        () => {
            const scripts = [...document.querySelectorAll('script[type="application/json"]')].map(s => s.textContent || '');
            return scripts.join('\\n---SCRIPT---\\n');
        }
        """
    )
    parsed = parse_render_data(data)
    for aweme in find_awemes_in_json(parsed):
        item = aweme_to_video(aweme, "browser_html")
        if item:
            return item
    return None


def dedupe_items(items: List[VideoItem]) -> List[VideoItem]:
    seen = set()
    out = []
    for item in items:
        key = item.aweme_id or item.url
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def get_items(mode: str, links: List[str], headless: bool, prefer_direct: bool) -> List[VideoItem]:
    session = request_session()
    items: List[VideoItem] = []

    if prefer_direct and mode == "videos":
        print("[DIRECT] Thử tải video lẻ không mở browser...")
        for url in links:
            item = direct_html_video(url, session)
            if item:
                print(f"[DIRECT OK] {item.title}")
                items.append(item)
            else:
                print(f"[DIRECT MISS] {url}")
        if len(items) == len(links):
            return dedupe_items(items)

    need_browser = mode in ("collections", "channel") or len(items) < len(links)
    if not need_browser:
        return dedupe_items(items)

    p = context = page = None
    try:
        p, context, page = browser_context(headless=headless)
        ensure_login_if_needed(page, context, headless=headless)
        if mode == "collections":
            for url in links:
                resolved = resolve_url(url, session)
                mix_id = extract_mix_id(resolved)
                if not mix_id:
                    print(f"[SKIP] Không thấy collection ID: {url}")
                    continue
                try:
                    page.goto(resolved, wait_until="domcontentloaded", timeout=90_000)
                except Exception:
                    pass
                items.extend(browser_collection(page, mix_id))
        elif mode == "channel":
            for url in links:
                resolved = resolve_url(url, session)
                sec_uid = extract_sec_uid(resolved)
                if not sec_uid:
                    try:
                        page.goto(resolved, wait_until="domcontentloaded", timeout=90_000)
                        sec_uid = extract_sec_uid(page.url)
                    except Exception:
                        sec_uid = ""
                if not sec_uid:
                    print(f"[SKIP] Không thấy sec_user_id: {url}")
                    continue
                items.extend(browser_user(page, sec_uid, resolved))
        else:
            done = {x.aweme_id or x.url for x in items}
            for url in links:
                if any((extract_aweme_id(url) and extract_aweme_id(url) == x.aweme_id) or x.url == url for x in items):
                    continue
                item = browser_video(page, url)
                if item and (item.aweme_id or item.url) not in done:
                    items.append(item)
                    done.add(item.aweme_id or item.url)
                else:
                    print(f"[FAIL META] {url}")
    finally:
        try:
            if context:
                context.close()
        finally:
            if p:
                p.stop()
    return dedupe_items(items)


def get_channel_items_anonymous_first(links: List[str], headless: bool, prefer_direct: bool) -> List[VideoItem]:
    global SESSION_DIR, CHANNEL_SCAN_INCOMPLETE
    saved_session_dir = SESSION_DIR
    anonymous_dir = tempfile.mkdtemp(prefix="vf_douyin_anonymous_")
    try:
        SESSION_DIR = anonymous_dir
        CHANNEL_SCAN_INCOMPLETE = False
        print("[ANON] Thử quét trang cá nhân không đăng nhập trước...", flush=True)
        anonymous_items = get_items("channel", links, headless=headless, prefer_direct=prefer_direct)
    finally:
        SESSION_DIR = saved_session_dir
        shutil.rmtree(anonymous_dir, ignore_errors=True)

    if not CHANNEL_SCAN_INCOMPLETE:
        print(f"[ANON] Đã quét hết kênh không cần đăng nhập: {len(anonymous_items)} video.", flush=True)
        return anonymous_items

    print(
        f"[ANON] Douyin dừng phân trang ở {len(anonymous_items)} video. "
        "Tự chuyển sang session đã lưu để tránh thiếu video.",
        flush=True,
    )
    if not os.path.exists(saved_session_dir) or not os.listdir(saved_session_dir):
        print("[SESSION REQUIRED] Chưa có session Douyin. Mở Cài đặt -> Douyin -> Đăng nhập Douyin.", flush=True)
        return anonymous_items

    CHANNEL_SCAN_INCOMPLETE = False
    session_items = get_items("channel", links, headless=headless, prefer_direct=prefer_direct)
    if CHANNEL_SCAN_INCOMPLETE:
        print("[SESSION REQUIRED] Session hiện tại vẫn bị giới hạn phân trang; hãy đăng nhập/làm mới session.", flush=True)
    else:
        print(f"[SESSION] Đã quét hết kênh bằng session: {len(session_items)} video.", flush=True)
    return session_items


def item_to_manifest(item: VideoItem, index: int, ok: Optional[bool] = None) -> Dict:
    data = item.__dict__.copy()
    data["index"] = index
    if ok is not None:
        data["ok"] = ok
    return data


def write_items_manifest(items: List[VideoItem], output_dir: str, filename: str = "videos_manifest.json") -> str:
    ensure_output(output_dir)
    path_out = os.path.join(output_dir, filename)
    with open(path_out, "w", encoding="utf-8") as f:
        json.dump([item_to_manifest(item, i) for i, item in enumerate(items, start=1)], f, ensure_ascii=False, indent=2)
    return path_out


def load_manifest_items(manifest_path: str) -> List[VideoItem]:
    with open(manifest_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        data = json.load(f)
    items = []
    for row in data:
        if not row.get("url"):
            continue
        items.append(VideoItem(
            title=clean_name(row.get("title") or row.get("aweme_id") or "video"),
            url=row["url"],
            aweme_id=str(row.get("aweme_id") or ""),
            source=row.get("source") or "manifest",
            no_watermark=bool(row.get("no_watermark", True)),
            cover=row.get("cover") or row.get("thumbnail") or "",
        ))
    return dedupe_items(items)


def refresh_manifest_media(items: List[VideoItem], headless: bool = True) -> List[VideoItem]:
    """Resolve profile-preview page URLs to actual no-watermark media URLs."""
    page_items = [item for item in items if "douyin.com/video/" in item.url]
    if not page_items:
        return items
    print(f"[REFRESH] Làm mới link media cho {len(page_items)} video đã chọn...", flush=True)
    resolved = get_items("videos", [item.url for item in page_items], headless=headless, prefer_direct=True)
    by_id = {item.aweme_id: item for item in resolved if item.aweme_id}
    refreshed: List[VideoItem] = []
    for original in items:
        replacement = by_id.get(original.aweme_id)
        if replacement:
            refreshed.append(replacement)
        elif "douyin.com/video/" in original.url:
            print(f"[REFRESH FAIL] Không lấy được link media: {original.title}", flush=True)
        else:
            refreshed.append(original)
    print(f"[REFRESH] Sẵn sàng {len(refreshed)}/{len(items)} video.", flush=True)
    return refreshed


def extract_audio_mp3(video_path: str, mp3_path: str) -> None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("Không thấy ffmpeg để tách audio MP3")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", video_path, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", mp3_path]
    code = subprocess.call(cmd)
    if code != 0 or not os.path.exists(mp3_path):
        raise RuntimeError(f"ffmpeg extract audio lỗi code={code}")


def download_one(video: VideoItem, index: int, total: int, output_dir: str, output_mode: str = "video", strict_no_watermark: bool = False) -> Tuple[bool, str]:
    print(f"[PROGRESS] Đang tải video {index}/{total}: {video.title}", flush=True)
    if strict_no_watermark and not video.no_watermark:
        return False, f"[SKIP WATERMARK] {index:04d} - {video.title}"
    if output_mode == "metadata":
        return True, f"[META ONLY] {index:04d} - {video.title}"
    ext = ".mp3" if output_mode == "audio" else ".mp4"
    filename = f"{index:04d} - {clean_name(video.title)}{ext}"
    path = os.path.join(output_dir, filename)
    if os.path.exists(path) and os.path.getsize(path) > 1024 * 256:
        return True, f"[SKIP EXISTS] {filename}"
    tmp_video_path = unique_path(output_dir, f"._tmp_{index:04d}_{int(time.time())}.mp4") if output_mode == "audio" else path
    headers = dict(HEADERS)
    headers["Range"] = "bytes=0-"
    for attempt in range(1, 6):
        try:
            with requests.get(video.url, headers=headers, stream=True, timeout=60) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}")
                content_type = str(r.headers.get("content-type") or "").lower()
                if "text/html" in content_type or "application/json" in content_type:
                    raise RuntimeError(f"link media trả về {content_type or 'dữ liệu không hợp lệ'}")
                total = int(r.headers.get("content-length") or 0)
                written = 0
                with open(tmp_video_path, "wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                if total and written < total * 0.98:
                    raise RuntimeError(f"file thiếu dữ liệu {written}/{total}")
            if output_mode == "audio":
                extract_audio_mp3(tmp_video_path, path)
                try:
                    os.remove(tmp_video_path)
                except Exception:
                    pass
            wm = "" if video.no_watermark else " [fallback watermark]"
            return True, f"[OK] {filename}{wm}"
        except Exception as e:
            try:
                if os.path.exists(path):
                    os.remove(path)
                if output_mode == "audio" and os.path.exists(tmp_video_path):
                    os.remove(tmp_video_path)
            except Exception:
                pass
            if attempt < 5:
                time.sleep(1.5 * attempt)
            else:
                return False, f"[FAIL] {filename}: {e}"
    return False, f"[FAIL] {filename}"


def download_all(items: List[VideoItem], output_dir: str, threads: int, output_mode: str = "video", strict_no_watermark: bool = False) -> bool:
    ensure_output(output_dir)
    manifest = []
    ok = 0
    completed = 0
    total = len(items)
    worker_count = min(MAX_THREADS, max(1, int(threads or DEFAULT_DOWNLOAD_THREADS)))
    print(f"[DOWNLOAD] {total} video -> {output_dir} | {worker_count} luồng song song", flush=True)
    with ThreadPoolExecutor(max_workers=worker_count) as ex:
        futures = {
            ex.submit(download_one, item, i, total, output_dir, output_mode, strict_no_watermark): (i, item)
            for i, item in enumerate(items, start=1)
        }
        for fut in as_completed(futures):
            i, item = futures[fut]
            try:
                success, msg = fut.result()
            except Exception as exc:
                success, msg = False, f"[FAIL] {i:04d} - {item.title}: {exc}"
            ok += 1 if success else 0
            completed += 1
            print(msg, flush=True)
            print(f"[PROGRESS] Đã xử lý {completed}/{total} video | thành công {ok}", flush=True)
            manifest.append(item_to_manifest(item, i, success))
    with open(os.path.join(output_dir, "download_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[DONE] Thành công {ok}/{total} video", flush=True)
    return ok == total


def interactive_args() -> argparse.Namespace:
    print("=== Douyin no-logo downloader ===")
    print("1. Tải collection/danh sách collection")
    print("2. Tải nhiều link video lẻ")
    print("3. Tải toàn bộ video từ kênh")
    choice = input("Chọn chế độ (1/2/3): ").strip() or "1"
    mode = {"1": "collections", "2": "videos", "3": "channel"}.get(choice, "collections")
    raw = input("Dán link hoặc đường dẫn file .txt chứa link: ").strip()
    out = input(f"Thư mục lưu (ENTER = {OUTPUT_DIR}): ").strip() or OUTPUT_DIR
    show = input("Mở browser nếu cần login/fallback? (y/N): ").strip().lower() == "y"
    return argparse.Namespace(mode=mode, links=raw, manifest="", output=out, threads=MAX_THREADS, show_browser=show, no_direct=False, list_only=False, output_mode="video", strict_no_watermark=False, clean_output_metadata=False, install_deps=False, session_status=False, clear_session=False, login_only=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Douyin downloader: collection, many videos, or whole channel.")
    parser.add_argument("--mode", choices=["collections", "videos", "channel"], help="Download mode.")
    parser.add_argument("--links", help="One URL, many newline-separated URLs, or a .txt file path.")
    parser.add_argument("--manifest", help="Download from a previously scanned videos_manifest.json.")
    parser.add_argument("--output", default=OUTPUT_DIR, help="Output folder.")
    parser.add_argument("--threads", type=int, default=DEFAULT_DOWNLOAD_THREADS, help="Parallel download threads.")
    parser.add_argument("--show-browser", action="store_true", help="Show Chromium browser for login/fallback.")
    parser.add_argument("--no-direct", action="store_true", help="Skip browserless direct attempt.")
    parser.add_argument("--list-only", action="store_true", help="Only collect metadata and write videos_manifest.json.")
    parser.add_argument("--output-mode", choices=["video", "audio", "metadata"], default="video", help="Download full video, extract MP3 audio, or metadata only.")
    parser.add_argument("--strict-no-watermark", action="store_true", help="Skip items that only have watermark fallback URL.")
    parser.add_argument("--clean-output-metadata", action="store_true", help="Remove internal manifest JSON files after download.")
    parser.add_argument("--install-deps", action="store_true", help="Install requests/playwright and Chromium for this Python.")
    parser.add_argument("--session-status", action="store_true", help="Print Douyin session status and exit.")
    parser.add_argument("--clear-session", action="store_true", help="Delete saved Douyin browser session and exit.")
    parser.add_argument("--login-only", action="store_true", help="Open browser only for Douyin login/session refresh.")
    parser.add_argument("--anonymous-first", action="store_true", help="Try a clean anonymous profile, then fallback to saved session if pagination is incomplete.")
    args = parser.parse_args()
    if args.install_deps or args.session_status or args.clear_session or args.login_only:
        return args
    if args.manifest:
        return args
    if not args.mode or not args.links:
        return interactive_args()
    return args


def main() -> int:
    args = parse_args()
    if getattr(args, "install_deps", False):
        return install_deps()
    if getattr(args, "session_status", False):
        print(json.dumps(session_status(), ensure_ascii=False, indent=2))
        return 0
    if getattr(args, "clear_session", False):
        clear_session()
        return 0
    if getattr(args, "login_only", False):
        return login_only()
    output = os.path.abspath(args.output)
    if getattr(args, "manifest", ""):
        items = load_manifest_items(args.manifest)
        if not args.list_only and args.output_mode != "metadata":
            items = refresh_manifest_media(items, headless=not args.show_browser)
    else:
        links = read_links_from_text_or_file(args.links)
        if not links:
            print("[ERR] Chưa có link hợp lệ.")
            return 2
        if args.mode == "channel" and getattr(args, "anonymous_first", False):
            items = get_channel_items_anonymous_first(links, headless=not args.show_browser, prefer_direct=not args.no_direct)
        else:
            items = get_items(args.mode, links, headless=not args.show_browser, prefer_direct=not args.no_direct)
    items = dedupe_items(items)
    if not items:
        print("[ERR] Không lấy được video nào. Nếu chưa có session, chạy lại với --show-browser để login.")
        return 3
    ensure_output(output)
    manifest_path = write_items_manifest(items, output)
    print(f"[META] Lấy được {len(items)} video. Manifest: {manifest_path}")
    if args.mode == "channel" and getattr(args, "anonymous_first", False) and CHANNEL_SCAN_INCOMPLETE:
        print("[ERR] Danh sách kênh chưa đầy đủ nên tool không cho tải để tránh thiếu video.", flush=True)
        return 5
    if args.list_only or args.output_mode == "metadata":
        return 0
    all_ok = download_all(items, output, args.threads, output_mode=args.output_mode, strict_no_watermark=args.strict_no_watermark)
    if args.clean_output_metadata:
        for filename in ["videos_manifest.json", "download_manifest.json"]:
            try:
                os.remove(os.path.join(output, filename))
            except FileNotFoundError:
                pass
    return 0 if all_ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
