from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from typing import Any

import requests


for stream_name in ("stdout", "stderr"):
    stream = getattr(__import__("sys"), stream_name)
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


API_HOST = "https://lv-pc-api-sinfonlinec.ulikecam.com"
APP_VERSION = "8.8.0"
USER_AGENT = "Cronet/TTNetVersion:01594da2 2023-03-14 QuicVersion:46688bb4 2022-11-28"
BLOCK_MS = 10 * 60 * 1000
SIGN_SALT_1 = "9e2c"
SIGN_SALT_2 = "11ac"
AAC_TIMELINE_SECONDS_PER_HOUR = 72.0
AAC_RECOGNIZE_SECONDS_PER_HOUR = 36.0
SESSION_SPLIT_TRIGGER_MS = 3 * 60 * 60 * 1000
MAX_SESSION_MS = 2 * 60 * 60 * 1000
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


def log(message: str) -> None:
    print(message, flush=True)


def progress(percent: float, message: str) -> None:
    print(f"PROGRESS:{max(0, min(99.9, percent)):.1f}:{message}", flush=True)


def request_with_retries(
    method: str,
    url: str,
    *,
    label: str,
    max_attempts: int = 5,
    base_delay: float = 5.0,
    **kwargs: Any,
) -> requests.Response:
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code in RETRYABLE_HTTP_STATUS:
                raise requests.HTTPError(
                    f"{response.status_code} retryable server response",
                    response=response,
                )
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = (
                isinstance(exc, (requests.Timeout, requests.ConnectionError))
                or status in RETRYABLE_HTTP_STATUS
            )
            if not retryable or attempt >= max_attempts:
                raise
            delay = min(60.0, base_delay * attempt)
            if status:
                log(f"{label} tạm lỗi HTTP {status}; thử lại {attempt + 1}/{max_attempts} sau {delay:.0f} giây.")
            else:
                log(f"{label} tạm gián đoạn ({exc.__class__.__name__}); thử lại {attempt + 1}/{max_attempts} sau {delay:.0f} giây.")
            time.sleep(delay)
    raise RuntimeError(f"{label} lỗi không xác định.")


def friendly_duration(seconds: float) -> str:
    value = max(0, round(seconds))
    hours, value = divmod(value, 3600)
    minutes, secs = divmod(value, 60)
    if hours:
        return f"{hours} giờ {minutes} phút"
    if minutes:
        return f"{minutes} phút {secs} giây"
    return f"{secs} giây"


def estimate_job_seconds(media_files: list[Path], duration_ms: int) -> float:
    hours = duration_ms / 3_600_000
    return max(15, hours * AAC_TIMELINE_SECONDS_PER_HOUR + 3)


def run_checked(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"Command failed: {args[0]}")
    return proc.stdout


def media_duration_ms(ffprobe: Path, media: Path) -> int:
    raw = run_checked([
        str(ffprobe), "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(media),
    ])
    duration = float(json.loads(raw)["format"]["duration"])
    return max(1, round(duration * 1000))


def render_timeline_audio(ffmpeg: Path, media_files: list[Path], output: Path) -> None:
    log(
        f"[1/5] CapCut detach audio timeline từ {len(media_files)} video "
        "(không nối hoặc render video)..."
    )
    args = [str(ffmpeg), "-y"]
    for media in media_files:
        args.extend(["-i", str(media)])
    if len(media_files) == 1:
        args.extend([
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "48000",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),
        ])
    else:
        normalized = []
        for index in range(len(media_files)):
            args.extend([])
            normalized.append(
                f"[{index}:a:0]aresample=48000,"
                "aformat=sample_fmts=fltp:channel_layouts=mono"
                f"[a{index}]"
            )
        joined = "".join(f"[a{i}]" for i in range(len(media_files)))
        filter_graph = ";".join(normalized) + f";{joined}concat=n={len(media_files)}:v=0:a=1[aout]"
        args.extend([
            "-filter_complex", filter_graph, "-map", "[aout]", "-vn",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),
        ])
    run_checked(args)


def split_timeline_audio(
    ffmpeg: Path,
    audio: Path,
    duration_ms: int,
    temp_dir: Path,
) -> list[tuple[Path, int, int]]:
    if duration_ms <= SESSION_SPLIT_TRIGGER_MS:
        return [(audio, 0, duration_ms)]

    chunks: list[tuple[Path, int, int]] = []
    for index, start_ms in enumerate(range(0, duration_ms, MAX_SESSION_MS), 1):
        chunk_ms = min(MAX_SESSION_MS, duration_ms - start_ms)
        chunk = temp_dir / f"timeline_{index:03d}.m4a"
        run_checked([
            str(ffmpeg), "-y",
            "-ss", f"{start_ms / 1000:.3f}",
            "-t", f"{chunk_ms / 1000:.3f}",
            "-i", str(audio),
            "-map", "0:a:0", "-c", "copy", "-movflags", "+faststart",
            str(chunk),
        ])
        chunks.append((chunk, start_ms, chunk_ms))
    return chunks


def offset_utterances(
    utterances: list[dict[str, Any]],
    offset_ms: int,
) -> list[dict[str, Any]]:
    if not offset_ms:
        return utterances
    adjusted = []
    for source in utterances:
        item = dict(source)
        for key in ("start_time", "end_time"):
            if item.get(key) is not None:
                item[key] = float(item[key]) + offset_ms
        words = []
        for source_word in item.get("words") or []:
            word = dict(source_word)
            for key in ("start_time", "end_time"):
                if word.get(key) is not None:
                    word[key] = float(word[key]) + offset_ms
            words.append(word)
        if words:
            item["words"] = words
        adjusted.append(item)
    return adjusted


def crc32_file(file_path: Path) -> str:
    value = 0
    with file_path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value = zlib.crc32(chunk, value)
    return f"{value & 0xFFFFFFFF:08x}"


def stable_device_id() -> str:
    seed = f"{socket.gethostname()}:{uuid.getnode()}".encode("utf-8")
    return str(int(hashlib.sha256(seed).hexdigest()[:15], 16)).zfill(16)[:16]


def request_sign(route: str, device_id: str) -> tuple[str, str]:
    device_time = str(int(time.time()))
    signature_source = "|".join([
        SIGN_SALT_1,
        route[-7:],
        "4",
        APP_VERSION,
        device_time,
        device_id,
        SIGN_SALT_2,
    ])
    return hashlib.md5(signature_source.encode("utf-8")).hexdigest(), device_time


def api_headers(route: str, device_id: str) -> dict[str, str]:
    sign, device_time = request_sign(route, device_id)
    return {
        "User-Agent": USER_AGENT,
        "appvr": APP_VERSION,
        "device-time": device_time,
        "pf": "4",
        "sign": sign,
        "sign-ver": "1",
        "tdid": device_id,
    }


def api_post(route: str, payload: dict[str, Any], device_id: str, timeout: int = 60) -> dict[str, Any]:
    response = requests.post(
        API_HOST + route,
        json=payload,
        headers=api_headers(route, device_id),
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if str(data.get("ret")) != "0":
        raise RuntimeError(f"CapCut {route} lỗi {data.get('ret')}: {data.get('errmsg')}")
    return data


def aws_sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def aws_signature(secret: str, query: str, headers: dict[str, str]) -> str:
    amz_date = headers["x-amz-date"]
    date_stamp = amz_date.split("T", 1)[0]
    canonical_headers = "\n".join(f"{key}:{value}" for key, value in headers.items()) + "\n"
    signed_headers = ";".join(headers)
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical = f"GET\n/\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    scope = f"{date_stamp}/cn/vod/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
        f"{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    )
    key_date = aws_sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    key_region = aws_sign(key_date, "cn")
    key_service = aws_sign(key_region, "vod")
    signing_key = aws_sign(key_service, "aws4_request")
    return hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


def upload_media(media: Path, device_id: str, label: str = "media") -> str:
    log(f"[2/5] Đang tải {label} lên CapCut ({media.stat().st_size / 1024 / 1024:.1f} MB)...")
    credentials = api_post("/lv/v1/upload_sign", {"biz": "pc-recognition"}, device_id)["data"]
    file_size = media.stat().st_size
    query = (
        f"Action=ApplyUploadInner&FileSize={file_size}&FileType=object&IsInner=1"
        "&SpaceName=lv-mac-recognition&Version=2020-11-19&s=5y0udbjapi"
    )
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    auth_headers = {
        "x-amz-date": amz_date,
        "x-amz-security-token": credentials["session_token"],
    }
    signature = aws_signature(credentials["secret_access_key"], query, auth_headers)
    auth_headers["authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={credentials['access_key_id']}/{date_stamp}/cn/vod/aws4_request, "
        f"SignedHeaders=x-amz-date;x-amz-security-token, Signature={signature}"
    )
    apply_response = request_with_retries(
        "get",
        f"https://vod.bytedanceapi.com/?{query}",
        headers=auth_headers,
        timeout=60,
        label="CapCut xin phiên upload",
    )
    address = apply_response.json()["Result"]["UploadAddress"]
    store = address["StoreInfos"][0]
    host = address["UploadHosts"][0]
    store_uri = store["StoreUri"]
    upload_id = address.get("UploadID") or store.get("UploadID") or store.get("UploadId")
    if not upload_id:
        raise RuntimeError(
            "CapCut upload không trả UploadID; "
            f"UploadAddress keys={sorted(address)}, StoreInfo keys={sorted(store)}"
        )
    crc = crc32_file(media)
    upload_headers = {
        "User-Agent": "Mozilla/5.0 Thea/1.0.1",
        "Authorization": store["Auth"],
        "Content-CRC32": crc,
    }
    upload_url = f"https://{host}/{store_uri}?partNumber=1&uploadID={upload_id}"
    for attempt in range(1, 7):
        try:
            with media.open("rb") as stream:
                part = requests.put(
                    upload_url,
                    data=stream,
                    headers=upload_headers,
                    timeout=1800,
                )
            if part.status_code in RETRYABLE_HTTP_STATUS:
                raise requests.HTTPError(
                    f"{part.status_code} retryable server response",
                    response=part,
                )
            part.raise_for_status()
            break
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = (
                isinstance(exc, (requests.Timeout, requests.ConnectionError))
                or status in RETRYABLE_HTTP_STATUS
            )
            if not retryable or attempt >= 6:
                raise
            delay = min(90.0, 10.0 * attempt)
            if status:
                log(f"CapCut upload audio tạm lỗi HTTP {status}; thử lại {attempt + 1}/6 sau {delay:.0f} giây.")
            else:
                log(f"CapCut upload audio tạm gián đoạn ({exc.__class__.__name__}); thử lại {attempt + 1}/6 sau {delay:.0f} giây.")
            time.sleep(delay)
    if part.json().get("success") != 0:
        raise RuntimeError(f"CapCut upload part lỗi: {part.text[:500]}")
    checked = request_with_retries(
        "post",
        f"https://{host}/{store_uri}?uploadID={upload_id}",
        data=f"1:{crc}",
        headers=upload_headers,
        timeout=60,
        label="CapCut xác nhận upload",
    )
    check_data = checked.json()
    if check_data.get("success") not in (None, 0):
        raise RuntimeError(f"CapCut upload check lỗi: {checked.text[:500]}")
    return store_uri


def timeline_blocks(duration_ms: int) -> list[dict[str, Any]]:
    blocks = []
    for start in range(0, duration_ms, BLOCK_MS):
        blocks.append({
            "start_time": start,
            "end_time": min(duration_ms, start + BLOCK_MS),
            "id": str(uuid.uuid4()),
        })
    return blocks


def submit_recognition(store_uri: str, duration_ms: int, device_id: str) -> str:
    blocks = timeline_blocks(duration_ms)
    log(f"[3/5] Gửi nhận diện: {len(blocks)} đoạn timeline, tối đa 10 phút/đoạn...")
    data = api_post(
        "/lv/v1/audio_subtitle/submit",
        {
            "adjust_endtime": 200,
            "audio": store_uri,
            "caption_type": 2,
            "client_request_id": str(uuid.uuid4()),
            "max_lines": 1,
            "songs_info": blocks,
            "words_per_line": 16,
        },
        device_id,
    )
    task_id = data.get("data", {}).get("id")
    if not task_id:
        raise RuntimeError("CapCut không trả task ID nhận diện.")
    return str(task_id)


def poll_result(
    task_id: str,
    device_id: str,
    timeout_seconds: int,
    progress_callback=None,
) -> list[dict[str, Any]]:
    log("[4/5] CapCut đang nhận diện phụ đề...")
    deadline = time.time() + timeout_seconds
    attempt = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        while time.time() < deadline:
            attempt += 1
            query_started = time.time()
            future = executor.submit(
                api_post,
                "/lv/v1/audio_subtitle/query",
                {"id": task_id, "pack_options": {"need_attribute": True}},
                device_id,
            )
            while not future.done():
                time.sleep(5)
                if progress_callback:
                    progress_callback(time.time() - (deadline - timeout_seconds))
            try:
                data = future.result()
            except (requests.Timeout, requests.ConnectionError) as error:
                log(
                    f"CapCut query tạm thời gián đoạn ({type(error).__name__}); "
                    "tự thử lại sau 5 giây."
                )
                if progress_callback:
                    progress_callback(time.time() - (deadline - timeout_seconds))
                time.sleep(5)
                continue
            utterances = data.get("data", {}).get("utterances") or []
            if utterances:
                log(f"CapCut đã trả {len(utterances)} câu.")
                return utterances
            if progress_callback:
                progress_callback(time.time() - (deadline - timeout_seconds))
            if attempt == 1 or attempt % 6 == 0:
                log(f"Đang chờ kết quả CapCut... ({attempt})")
            time.sleep(max(0, 5 - (time.time() - query_started)))
    raise TimeoutError(f"CapCut chưa trả kết quả sau {timeout_seconds // 60} phút.")


def srt_timestamp(milliseconds: int | float) -> str:
    value = max(0, round(float(milliseconds)))
    hours, value = divmod(value, 3_600_000)
    minutes, value = divmod(value, 60_000)
    seconds, millis = divmod(value, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def write_srt(utterances: list[dict[str, Any]], output: Path) -> int:
    entries = []
    for item in utterances:
        text = str(item.get("text") or "").strip()
        start = item.get("start_time")
        end = item.get("end_time")
        if not text or start is None or end is None or float(end) <= float(start):
            continue
        entries.append((float(start), float(end), text))
    entries.sort(key=lambda row: (row[0], row[1]))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="\n") as stream:
        for index, (start, end, text) in enumerate(entries, 1):
            stream.write(
                f"{index}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n\n"
            )
    return len(entries)


def process_timeline(
    media_files: list[Path],
    output: Path,
    ffmpeg: Path,
    ffprobe: Path,
    timeout_seconds: int,
    progress_start: float,
    progress_span: float,
) -> int:
    job_started = time.time()
    durations = [media_duration_ms(ffprobe, media) for media in media_files]
    duration_ms = sum(durations)
    estimated_seconds = estimate_job_seconds(media_files, duration_ms)

    def report(local_ratio: float, message: str) -> None:
        remaining = max(0, estimated_seconds - (time.time() - job_started))
        progress(
            progress_start + progress_span * local_ratio,
            f"{message} | ETA khoảng {friendly_duration(remaining)}",
        )

    report(0.03, f"Đã đọc {len(media_files)} video, tổng {duration_ms / 60000:.2f} phút")
    log(
        f"Ước tính hoàn thành khoảng {friendly_duration(estimated_seconds)} "
        "(dựa trên benchmark video 1 giờ)."
    )
    log(
        f"Timeline gồm {len(media_files)} video, dài {duration_ms / 60000:.2f} phút; "
        f"CapCut chia {len(timeline_blocks(duration_ms))} đoạn nhận diện."
    )
    temp_dir: Path | None = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="voiceforge_capcut_"))
        temp_audio = temp_dir / "timeline.m4a"
        render_timeline_audio(ffmpeg, media_files, temp_audio)
        report(0.20, "Đã detach audio AAC 48 kHz · mono · 128 kbps")
        chunks = split_timeline_audio(ffmpeg, temp_audio, duration_ms, temp_dir)
        if len(chunks) > 1:
            log(
                f"Timeline vượt 3 giờ: tự chia thành {len(chunks)} phiên CapCut "
                "(tối đa 2 giờ/phiên), sau đó ghép lại timestamp."
            )
            report(0.24, f"Đã chia audio thành {len(chunks)} phiên an toàn")

        device_id = stable_device_id()
        all_utterances: list[dict[str, Any]] = []
        session_span = 0.72 / len(chunks)
        for session_index, (upload_source, offset_ms, chunk_ms) in enumerate(chunks, 1):
            session_start = 0.24 + (session_index - 1) * session_span
            upload_label = (
                f"audio AAC phiên {session_index}/{len(chunks)}"
                if len(chunks) > 1 else "audio AAC 48 kHz"
            )
            store_uri = upload_media(upload_source, device_id, upload_label)
            report(
                session_start + session_span * 0.38,
                f"Đã tải phiên {session_index}/{len(chunks)} lên CapCut",
            )
            task_id = submit_recognition(store_uri, chunk_ms, device_id)
            report(
                session_start + session_span * 0.48,
                f"CapCut đã nhận phiên {session_index}/{len(chunks)}",
            )
            expected_recognition = max(
                15,
                chunk_ms / 3_600_000 * AAC_RECOGNIZE_SECONDS_PER_HOUR,
            )
            utterances = poll_result(
                task_id,
                device_id,
                timeout_seconds,
                lambda elapsed, base=session_start, span=session_span, current=session_index: report(
                    base + span * (0.48 + min(0.95, elapsed / expected_recognition) * 0.48),
                    f"CapCut đang nhận diện phiên {current}/{len(chunks)}",
                ),
            )
            all_utterances.extend(offset_utterances(utterances, offset_ms))

        count = write_srt(all_utterances, output)
        if not count:
            raise RuntimeError("CapCut hoàn thành nhưng không có câu phụ đề hợp lệ.")
        progress(progress_start + progress_span, f"Hoàn thành {count} câu: {output.name}")
        log(f"[5/5] Hoàn thành {count} câu: {output}")
        return count
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate SRT with CapCut's audio subtitle service.")
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--base-name", default="capcut_srt")
    parser.add_argument("--mode", choices=["combined", "separate"], default="combined")
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--timeout-minutes", type=int, default=90)
    args = parser.parse_args()

    for file_path, label in (
        (args.ffmpeg, "FFmpeg"),
        (args.ffprobe, "FFprobe"),
    ):
        if not file_path.exists():
            raise SystemExit(f"Không tìm thấy {label}: {file_path}")
    for media in args.input:
        if not media.exists():
            raise SystemExit(f"Không tìm thấy video: {media}")

    timeout_seconds = max(60, args.timeout_minutes * 60)
    if args.mode == "combined":
        if not args.output:
            raise SystemExit("--output là bắt buộc với mode combined")
        process_timeline(
            args.input, args.output, args.ffmpeg, args.ffprobe,
            timeout_seconds, 0, 99.8,
        )
    else:
        if not args.output_dir:
            raise SystemExit("--output-dir là bắt buộc với mode separate")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        total = len(args.input)
        for index, media in enumerate(args.input, 1):
            suffix = f"_{index:03d}" if total > 1 else ""
            output = args.output_dir / f"{args.base_name}{suffix}.srt"
            if output.exists():
                counter = 1
                while output.exists():
                    output = args.output_dir / f"{args.base_name}{suffix} ({counter}).srt"
                    counter += 1
            log(f"Video {index}/{total}: {media.name}")
            process_timeline(
                [media], output, args.ffmpeg, args.ffprobe, timeout_seconds,
                (index - 1) * 99.8 / total, 99.8 / total,
            )
    print("PROGRESS:100:Hoàn thành CapCut SRT", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
