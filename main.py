import re
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import getproxies

import imageio_ffmpeg
from flask import Flask, jsonify, render_template, request
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = Flask(__name__)

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# پورت‌های رایج پروکسی محلی در برنامه‌های VPN (v2ray, clash, nekoray, psiphon, ...)
COMMON_PROXY_PORTS = [10808, 10809, 7890, 7891, 7897, 1080, 1081, 2080, 2081, 8080, 8118]
NETWORK_ERROR = re.compile(
    r"unable to download|timed out|refused|connection|transporterror|network|"
    r"getaddrinfo|ssl|proxy|unreachable|reset|certificate|remote end closed",
    re.I,
)
ANSI = re.compile(r"\x1b\[[0-9;]*m")

jobs = {}  # شناسه کار -> وضعیت پیشرفت

# بهترین ویدیو و صدا جدا دانلود و با ffmpeg در یک mp4 ترکیب می‌شوند
FORMAT_OPTS = {
    "format": "bv*[vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
    "merge_output_format": "mp4",
    "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
}

_last_working = None  # آخرین مسیر اتصالی که جواب داده، دفعه بعد اول امتحان می‌شود


def port_open(host, port, timeout=0.7):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def proxy_alive(proxy_url):
    u = urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    return bool(u.hostname and u.port and port_open(u.hostname, u.port))


def candidate_proxies(manual):
    """لیست مسیرهای اتصال به ترتیب اولویت. None یعنی اتصال مستقیم (مناسب VPN های تونلی)."""
    out = []

    def add(p):
        if p not in out:
            out.append(p)

    if manual:
        add(manual if "://" in manual else "http://" + manual)
    if _last_working is not None:
        add(_last_working)
    # پروکسی سیستم/محیط (فقط اگر واقعاً روشن باشد، نه تنظیم قدیمی و مرده)
    for key in ("https", "http", "socks", "all"):
        p = getproxies().get(key)
        if p and proxy_alive(p):
            add(p if "://" in p else "http://" + p)
    add(None)
    # اسکن پورت‌های رایج روی لوکال
    for port in COMMON_PROXY_PORTS:
        if port_open("127.0.0.1", port):
            add(f"http://127.0.0.1:{port}")
            add(f"socks5h://127.0.0.1:{port}")
    return out


class SilentLogger:
    """خطاهای مسیرهای ناموفق را در کنسول چاپ نمی‌کند؛ مسیر بعدی امتحان می‌شود."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def base_opts(proxy):
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": SilentLogger(),
        "noplaylist": True,
        "socket_timeout": 10,
        "retries": 2,
        # رشته خالی یعنی اتصال مستقیم و نادیده گرفتن پروکسی سیستم
        "proxy": proxy if proxy else "",
    }


def clean(msg):
    return ANSI.sub("", str(msg))


def find_working_route(url, manual, extra=None):
    """اولین مسیری که یوتیوب را باز کند پیدا می‌کند. (proxy, info) برمی‌گرداند."""
    global _last_working
    last_err = None
    for proxy in candidate_proxies(manual):
        try:
            with YoutubeDL(base_opts(proxy) | (extra or {})) as ydl:
                info = ydl.extract_info(url, download=False)
            _last_working = proxy
            return proxy, info
        except DownloadError as e:
            msg = clean(e)
            if not NETWORK_ERROR.search(msg):
                raise  # خطای واقعی (مثلاً ویدیو خصوصی یا لینک اشتباه) نه مشکل شبکه
            last_err = msg
    raise ConnectionError(last_err or "اتصال برقرار نشد")


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/download")
def download():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify(error="لینک وارد نشده است"), 400

    folder = Path(body.get("folder", "").strip() or DOWNLOAD_DIR).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return jsonify(error=f"مسیر ذخیره نامعتبر است: {e}"), 400

    job_id = uuid.uuid4().hex
    jobs[job_id] = {"phase": "connecting", "percent": 0}
    threading.Thread(
        target=run_job, args=(job_id, url, folder, body.get("proxy", "").strip()), daemon=True
    ).start()
    return jsonify(job=job_id)


@app.get("/progress/<job_id>")
def progress(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify(phase="error", error="کار پیدا نشد"), 404
    return jsonify(job)


def run_job(job_id, url, folder, manual_proxy):
    job = jobs[job_id]
    try:
        proxy, info = find_working_route(url, manual_proxy, FORMAT_OPTS)
        streams = info.get("requested_formats") or [info]
        expected = sum(s.get("filesize") or s.get("filesize_approx") or 0 for s in streams)
        job.update(phase="downloading", title=info.get("title", ""))

        per_file = {}  # نام فایل -> بایت دانلودشده

        def on_progress(d):
            if d["status"] not in ("downloading", "finished"):
                return
            done = d.get("downloaded_bytes") or 0
            per_file[d["filename"]] = done
            downloaded = sum(per_file.values())
            total = max(expected, downloaded)
            speed = d.get("speed") or 0
            job.update(
                downloaded=downloaded,
                total=total,
                speed=speed,
                eta=int((total - downloaded) / speed) if speed else None,
                percent=min(99.0, downloaded * 100 / total) if total else 0,
            )

        def on_postprocess(d):
            if d["status"] == "started":
                job.update(phase="merging", percent=99.0, speed=0, eta=None)

        opts = base_opts(proxy) | FORMAT_OPTS | {
            "outtmpl": str(folder / "%(title)s [%(id)s].%(ext)s"),
            "socket_timeout": 30,
            "retries": 10,
            "progress_hooks": [on_progress],
            "postprocessor_hooks": [on_postprocess],
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
        job.update(phase="done", percent=100, path=str(path), eta=0, speed=0)
    except ConnectionError:
        job.update(
            phase="error",
            error="اتصال به یوتیوب برقرار نشد. مطمئن شوید VPN شما روشن و وصل است. "
            "اگر VPN شما پروکسی محلی دارد، آدرس آن را در «تنظیمات پیشرفته» وارد کنید.",
        )
    except DownloadError as e:
        job.update(phase="error", error=f"خطا در دانلود: {clean(e)}")
    except Exception as e:  # noqa: BLE001
        job.update(phase="error", error=f"خطای غیرمنتظره: {e}")


@app.get("/choose-folder")
def choose_folder():
    # دیالوگ انتخاب پوشه در یک پروسه جدا اجرا می‌شود (tkinter در ترد Flask مشکل دارد)
    script = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "print(filedialog.askdirectory(title='پوشه ذخیره را انتخاب کنید'))\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, encoding="utf-8")
    return jsonify(folder=out.stdout.strip())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
