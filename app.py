import re
import os
import asyncio
import logging
import time
import uuid
import json
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs, quote
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as cffi_requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("terabox-bypass")

app = FastAPI(title="TeraBox Bypass Tool")

DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "TeraBox")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

download_tasks: dict[str, dict] = {}
CHUNK_THREADS = 2

TERABOX_DOMAINS = [
    "terabox.com", "1024tera.com", "terasharelink.com", "nephobox.com",
    "1024terabox.com", "4funbox.com", "mirrobox.com", "momerybox.com",
    "teraboxapp.com", "terabox.app", "terafileshare.com",
    "dm.terabox.com", "dm.terabox.app",
]

SHORTLINK_DOMAINS = [
    "teraboxlinks.com", "teralink.me", "terasharelinks.com",
    "teraboxlink.com", "teraboxurl.com",
]

REDIRECT_DOMAINS = [
    "finance.carrnissan.com",
]

ALL_KNOWN_DOMAINS = TERABOX_DOMAINS + SHORTLINK_DOMAINS + REDIRECT_DOMAINS

NDUS_COOKIE = "YuKhSgnteHui5BaYYbhVrKsMJCFvrs0KDlPi5DTZ"


def extract_short_code(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    for domain in SHORTLINK_DOMAINS:
        if host.endswith(domain):
            path = parsed.path.strip("/")
            if path:
                return path
    return None


def extract_surl(url: str) -> Optional[str]:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "surl" in qs:
        return qs["surl"][0]
    path = parsed.path
    match = re.search(r'/s/1?([A-Za-z0-9_-]+)', path)
    if match:
        raw = match.group(0).split("/s/")[1]
        if raw.startswith("1") and len(raw) > 10:
            return raw[1:]
        return raw
    return None


def classify_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if any(host.endswith(d) for d in SHORTLINK_DOMAINS):
        return "shortlink"
    if any(host.endswith(d) for d in REDIRECT_DOMAINS):
        return "redirect"
    if any(host.endswith(d) for d in TERABOX_DOMAINS):
        return "terabox"
    return "unknown"


def resolve_shortlink(url: str) -> dict:
    code = extract_short_code(url)
    if not code:
        return {"type": "shortlink", "success": False, "error": "Invalid shortlink", "original": url}

    try:
        session = cffi_requests.Session(impersonate="chrome110")
        resp = session.get(url, timeout=15, allow_redirects=False)

        if resp.status_code in (301, 302, 303, 307, 308):
            redirect_url = resp.headers.get("Location", "")
            return {"type": "shortlink", "success": True, "intermediate_url": redirect_url,
                    "original": url, "code": code}

        content = resp.text
        redirect_match = re.search(
            r'(?:window\.location\.href|location\.href)\s*=\s*["\']([^"\']+)["\']',
            content,
        )
        if redirect_match:
            intermediate = redirect_match.group(1)
            if intermediate.startswith("intent://"):
                parts = intermediate.split("intent://")[1].split("#")[0]
                intermediate = f"https://{parts}"
            return {"type": "shortlink", "success": True, "intermediate_url": intermediate,
                    "original": url, "code": code}

        return {"type": "shortlink", "success": False, "error": "No redirect found",
                "original": url, "code": code}

    except Exception as e:
        return {"type": "shortlink", "success": False, "error": str(e),
                "original": url, "code": code}


def get_terabox_file_info(surl: str, ndus: str = "") -> dict:
    """Get file info from TeraBox API using curl_cffi (bypasses bot detection)."""
    try:
        session = cffi_requests.Session(impersonate="chrome110")

        if ndus:
            session.cookies.update({"ndus": ndus})
        elif NDUS_COOKIE:
            session.cookies.update({"ndus": NDUS_COOKIE})

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36"
        }

        page_url = f"https://www.terabox.app/sharing/link?surl={surl}"
        resp = session.get(page_url, headers=headers, timeout=15)

        match = re.search(r'fn%28%22(.*?)%22%29', resp.text)
        if not match:
            return {"success": False, "error": "Could not extract jsToken from page", "surl": surl}

        jsToken = match.group(1)

        api_url = "https://www.terabox.app/share/list"
        params = {
            "app_id": "250528",
            "jsToken": jsToken,
            "shorturl": surl,
            "root": "1"
        }

        api_headers = {
            "User-Agent": headers["User-Agent"],
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://www.terabox.app/sharing/link?surl={surl}&clearCache=1",
            "X-Requested-With": "XMLHttpRequest",
        }

        api_resp = session.get(api_url, params=params, headers=api_headers, timeout=15)
        data = api_resp.json()

        if data.get("errno") != 0:
            return {"success": False, "error": f"TeraBox API error: {data.get('errmsg', 'Unknown')}",
                    "surl": surl, "errno": data.get("errno")}

        files = []
        for item in data.get("list", []):
            f = {
                "name": item.get("server_filename", "unknown"),
                "size": item.get("size", 0),
                "size_human": format_size(item.get("size", 0)),
                "is_dir": str(item.get("isdir", "0")) == "1",
                "fs_id": item.get("fs_id"),
                "md5": item.get("md5", ""),
                "category": item.get("category", 0),
            }
            dlink = item.get("dlink", "")
            if dlink:
                f["dlink"] = dlink
            thumbs = item.get("thumbs", {})
            if thumbs:
                f["thumbnail"] = thumbs.get("url3") or thumbs.get("url2") or thumbs.get("url1", "")
            if item.get("duration"):
                f["duration"] = item["duration"]
            files.append(f)

        return {"success": True, "surl": surl, "files": files,
                "share_url": f"https://www.terabox.app/s/1{surl}",
                "has_dlink": any(f.get("dlink") for f in files)}

    except Exception as e:
        logger.error(f"Error getting TeraBox info for surl={surl}: {e}")
        return {"success": False, "error": str(e), "surl": surl}


def proxy_download(dlink: str, ndus: str = "") -> cffi_requests.Response:
    """Download file from TeraBox dlink via proxy."""
    session = cffi_requests.Session(impersonate="chrome110")
    if ndus:
        session.cookies.update({"ndus": ndus})
    elif NDUS_COOKIE:
        session.cookies.update({"ndus": NDUS_COOKIE})

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Referer": "https://www.terabox.app/",
    }

    resp = session.get(dlink, headers=headers, timeout=60, allow_redirects=True, stream=True)
    return resp


def format_size(size_bytes) -> str:
    try:
        size_bytes = int(size_bytes)
    except (ValueError, TypeError):
        return "Unknown"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def parse_links(text: str) -> list[str]:
    url_re = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
    urls = url_re.findall(text)
    valid = []
    for u in urls:
        u = u.rstrip(".,;:!?)")
        parsed = urlparse(u)
        host = parsed.hostname or ""
        if any(host.endswith(d) for d in ALL_KNOWN_DOMAINS):
            valid.append(u)
    return list(dict.fromkeys(valid))


@app.post("/api/resolve")
async def resolve_links(request: Request):
    body = await request.json()
    raw_text = body.get("links", "")
    ndus = body.get("cookie", "")

    urls = parse_links(raw_text)
    if not urls:
        return JSONResponse({"success": False, "error": "No valid links found. Paste teraboxlinks.com or terabox.com links."})

    results = []
    for url in urls:
        url_type = classify_url(url)
        if url_type == "shortlink":
            result = await asyncio.to_thread(resolve_shortlink, url)
            results.append(result)
        elif url_type == "redirect":
            qs = parse_qs(urlparse(url).query)
            code = qs.get("link", [None])[0]
            if code:
                shortlink = f"https://teraboxlinks.com/{code}"
                result = await asyncio.to_thread(resolve_shortlink, shortlink)
                result["original"] = url
                results.append(result)
            else:
                results.append({"type": "redirect", "success": True,
                                "intermediate_url": url, "original": url})
        elif url_type == "terabox":
            surl = extract_surl(url)
            if surl:
                result = await asyncio.to_thread(get_terabox_file_info, surl, ndus)
                result["type"] = "terabox"
                result["original"] = url
                results.append(result)
            else:
                results.append({"type": "terabox", "success": False,
                                "error": "Could not extract share URL", "original": url})
        else:
            results.append({"type": "unknown", "success": False,
                            "error": "Unknown domain", "original": url})

    return JSONResponse({"success": True, "results": results, "total": len(results)})


@app.post("/api/terabox-info")
async def terabox_info(request: Request):
    body = await request.json()
    surls = body.get("surls", [])
    ndus = body.get("cookie", "")

    if not surls:
        return JSONResponse({"success": False, "error": "No surls provided"})

    results = []
    for surl in surls:
        result = await asyncio.to_thread(get_terabox_file_info, surl, ndus)
        results.append(result)

    return JSONResponse({"success": True, "results": results})


@app.get("/api/download")
async def download_proxy(request: Request):
    dlink = request.query_params.get("dlink", "")
    ndus = request.query_params.get("cookie", "")
    fname = request.query_params.get("fname", "download")

    if not dlink:
        return JSONResponse({"error": "No dlink provided"}, status_code=400)

    try:
        resp = await asyncio.to_thread(proxy_download, dlink, ndus)
        if resp.status_code != 200:
            return JSONResponse({"error": f"TeraBox returned {resp.status_code}"}, status_code=502)

        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        content_length = resp.headers.get("Content-Length")

        headers = {
            "Content-Disposition": f'attachment; filename="{quote(fname)}"',
            "Content-Type": content_type,
        }
        if content_length:
            headers["Content-Length"] = content_length

        def iterdata():
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    yield chunk

        return StreamingResponse(iterdata(), headers=headers, media_type=content_type)

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/set-cookie")
async def set_cookie(request: Request):
    global NDUS_COOKIE
    body = await request.json()
    cookie = body.get("cookie", "").strip()
    if cookie:
        NDUS_COOKIE = cookie
        return JSONResponse({"success": True, "message": "Cookie saved for this session"})
    return JSONResponse({"success": False, "error": "No cookie provided"})


def _download_chunk(dlink: str, ndus: str, start: int, end: int, filepath: str, chunk_idx: int, task_id: str):
    """Download a single chunk using range request with retry."""
    for attempt in range(3):
        try:
            session = cffi_requests.Session(impersonate="chrome110")
            if ndus:
                session.cookies.update({"ndus": ndus})
            elif NDUS_COOKIE:
                session.cookies.update({"ndus": NDUS_COOKIE})

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36",
                "Referer": "https://www.terabox.app/",
                "Range": f"bytes={start}-{end}",
            }

            resp = session.get(dlink, headers=headers, timeout=120, allow_redirects=True)
            chunk_file = f"{filepath}.part{chunk_idx}"
            with open(chunk_file, "wb") as f:
                f.write(resp.content)

            if task_id in download_tasks:
                download_tasks[task_id]["downloaded"] += len(resp.content)

            return chunk_file
        except Exception as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
            else:
                raise e
    return None


SLOW_SPEED_THRESHOLD = 50 * 1024  # 50 KB/s
SLOW_CHECK_AFTER = 10  # seconds before checking speed
MAX_CDN_RETRIES = 3


def _get_fresh_dlink(surl: str, ndus: str, fs_id: str = "") -> str:
    """Get a fresh dlink from TeraBox API (may hit different CDN)."""
    result = get_terabox_file_info(surl, ndus)
    if result.get("success") and result.get("files"):
        for f in result["files"]:
            if f.get("dlink"):
                if fs_id and str(f.get("fs_id")) == str(fs_id):
                    return f["dlink"]
                elif not fs_id:
                    return f["dlink"]
        if result["files"][0].get("dlink"):
            return result["files"][0]["dlink"]
    return ""


def _fast_download_file(dlink: str, ndus: str, filename: str, task_id: str,
                        surl: str = "", fs_id: str = ""):
    """Download with auto-retry on slow CDN."""
    task = download_tasks[task_id]
    task["status"] = "downloading"
    task["started"] = time.time()

    try:
        task["filename"] = filename

        safe_name = re.sub(r'[<>:"/\\|?*]', '_', filename)
        filepath = os.path.join(DOWNLOAD_DIR, safe_name)

        base, ext = os.path.splitext(filepath)
        counter = 1
        while os.path.exists(filepath):
            filepath = f"{base} ({counter}){ext}"
            counter += 1

        task["filepath"] = filepath
        task["threads"] = 1
        task["retries"] = 0

        current_dlink = dlink

        for retry in range(MAX_CDN_RETRIES + 1):
            task["downloaded"] = 0
            task["retries"] = retry

            session = cffi_requests.Session(impersonate="chrome110")
            if ndus:
                session.cookies.update({"ndus": ndus})
            elif NDUS_COOKIE:
                session.cookies.update({"ndus": NDUS_COOKIE})

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36",
                "Referer": "https://www.terabox.app/",
            }

            if retry > 0:
                task["status"] = "retrying"
                logger.info(f"[{task_id}] Slow speed detected, getting fresh CDN link (retry {retry}/{MAX_CDN_RETRIES})")
                time.sleep(1)

            resp = session.get(current_dlink, headers=headers, timeout=600,
                             allow_redirects=True, stream=True)
            total_size = int(resp.headers.get("Content-Length", 0))
            task["total"] = total_size
            task["status"] = "downloading"

            slow_detected = False
            speed_check_start = time.time()
            speed_check_bytes = 0

            with open(filepath, "wb") as out:
                for data in resp.iter_content(chunk_size=512 * 1024):
                    if data:
                        out.write(data)
                        task["downloaded"] += len(data)
                        speed_check_bytes += len(data)

                        elapsed_since_check = time.time() - speed_check_start
                        if elapsed_since_check >= SLOW_CHECK_AFTER:
                            current_speed = speed_check_bytes / elapsed_since_check
                            if (current_speed < SLOW_SPEED_THRESHOLD
                                    and surl
                                    and retry < MAX_CDN_RETRIES
                                    and task["downloaded"] < total_size * 0.5):
                                logger.info(f"[{task_id}] Speed {current_speed/1024:.1f} KB/s < {SLOW_SPEED_THRESHOLD/1024:.0f} KB/s threshold")
                                slow_detected = True
                                break
                            speed_check_start = time.time()
                            speed_check_bytes = 0

            if slow_detected:
                try:
                    os.remove(filepath)
                except OSError:
                    pass
                if surl:
                    new_dlink = _get_fresh_dlink(surl, ndus, fs_id)
                    if new_dlink and new_dlink != current_dlink:
                        current_dlink = new_dlink
                        logger.info(f"[{task_id}] Got new CDN link, retrying...")
                        continue
                    elif new_dlink:
                        current_dlink = new_dlink
                        continue
                break

            actual_size = os.path.getsize(filepath)
            task["downloaded"] = actual_size
            task["total"] = actual_size
            task["status"] = "completed"
            task["speed"] = 0
            elapsed = time.time() - task["started"]
            if elapsed > 0:
                task["avg_speed"] = actual_size / elapsed
            return

        if task["status"] != "completed":
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                actual_size = os.path.getsize(filepath)
                task["downloaded"] = actual_size
                task["total"] = actual_size
                task["status"] = "completed"
                task["speed"] = 0
                elapsed = time.time() - task["started"]
                if elapsed > 0:
                    task["avg_speed"] = actual_size / elapsed
            else:
                task["status"] = "error"
                task["error"] = "Download too slow, all CDN retries exhausted"

    except Exception as e:
        task["status"] = "error"
        task["error"] = str(e)
        logger.error(f"Download error for {filename}: {e}")


@app.post("/api/fast-download")
async def fast_download(request: Request):
    body = await request.json()
    dlink = body.get("dlink", "")
    filename = body.get("filename", "download")
    ndus = body.get("cookie", "")
    surl = body.get("surl", "")
    fs_id = body.get("fs_id", "")

    if not dlink:
        return JSONResponse({"error": "No dlink provided"}, status_code=400)

    task_id = str(uuid.uuid4())[:8]
    download_tasks[task_id] = {
        "id": task_id,
        "filename": filename,
        "status": "starting",
        "downloaded": 0,
        "total": 0,
        "threads": 0,
        "speed": 0,
        "started": 0,
        "filepath": "",
        "retries": 0,
    }

    thread = threading.Thread(
        target=_fast_download_file,
        args=(dlink, ndus, filename, task_id, surl, fs_id),
        daemon=True
    )
    thread.start()

    return JSONResponse({"success": True, "task_id": task_id})


@app.get("/api/download-status")
async def download_status(request: Request):
    task_id = request.query_params.get("task_id", "")

    if task_id:
        task = download_tasks.get(task_id)
        if not task:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        elapsed = time.time() - task.get("started", time.time()) if task.get("started") else 0
        speed = task["downloaded"] / elapsed if elapsed > 0.5 else 0
        return JSONResponse({
            "id": task["id"],
            "filename": task["filename"],
            "status": task["status"],
            "downloaded": task["downloaded"],
            "total": task["total"],
            "threads": task["threads"],
            "speed": speed,
            "filepath": task.get("filepath", ""),
            "error": task.get("error", ""),
            "retries": task.get("retries", 0),
        })

    all_tasks = []
    for tid, task in list(download_tasks.items()):
        elapsed = time.time() - task.get("started", time.time()) if task.get("started") else 0
        speed = task["downloaded"] / elapsed if elapsed > 0.5 else 0
        all_tasks.append({
            "id": task["id"],
            "filename": task["filename"],
            "status": task["status"],
            "downloaded": task["downloaded"],
            "total": task["total"],
            "threads": task["threads"],
            "speed": speed,
            "filepath": task.get("filepath", ""),
            "error": task.get("error", ""),
            "retries": task.get("retries", 0),
        })
    return JSONResponse({"tasks": all_tasks})


@app.post("/api/download-all")
async def download_all(request: Request):
    body = await request.json()
    files = body.get("files", [])
    ndus = body.get("cookie", "")

    task_ids = []
    for f in files:
        dlink = f.get("dlink", "")
        filename = f.get("name", "download")
        surl = f.get("surl", "")
        fs_id = f.get("fs_id", "")
        if not dlink:
            continue
        task_id = str(uuid.uuid4())[:8]
        download_tasks[task_id] = {
            "id": task_id,
            "filename": filename,
            "status": "queued",
            "downloaded": 0,
            "total": 0,
            "threads": 0,
            "speed": 0,
            "started": 0,
            "filepath": "",
            "retries": 0,
        }
        thread = threading.Thread(
            target=_fast_download_file,
            args=(dlink, ndus, filename, task_id, surl, fs_id),
            daemon=True
        )
        thread.start()
        task_ids.append(task_id)

    return JSONResponse({"success": True, "task_ids": task_ids, "download_dir": DOWNLOAD_DIR})


@app.get("/api/download-dir")
async def get_download_dir():
    return JSONResponse({"dir": DOWNLOAD_DIR})


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


def main():
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
