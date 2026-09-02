"""
ESP32 Music Player — Python Flask Server v2
Hỗ trợ: ZingMP3 | SoundCloud | YouTube
"""

import os
import json
import struct
import threading
import urllib.request
from io import BytesIO
from pathlib import Path
import subprocess
from urllib.parse import unquote

from flask import (Flask, jsonify, request, render_template,
                   Response, stream_with_context, abort)
from flask_cors import CORS
from PIL import Image

from platforms import PLATFORMS

app = Flask(__name__)
CORS(app)

# ─── Cache đơn giản cho stream URLs (tránh gọi API nhiều lần) ──
_stream_cache: dict[str, str] = {}
_cache_lock = threading.Lock()

# ───────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


# ─── Tìm kiếm ──────────────────────────────────────────────────

@app.route("/api/search", methods=["GET", "POST"])
def search():
    """
    GET  /api/search?q=<query>&platform=<zing|soundcloud|youtube>&limit=<n>
    POST /api/search  body: {"q":"...","platform":"...","limit":20}
    Trả về JSON danh sách bài hát.
    """
    if request.method == "POST":
        data     = request.get_json(force=True, silent=True) or {}
        q        = str(data.get("q", "")).strip()
        platform = str(data.get("platform", "zing")).lower()
        limit    = int(data.get("limit", 20))
    else:
        q        = request.args.get("q", "").strip()
        platform = request.args.get("platform", "zing").lower()
        limit    = int(request.args.get("limit", 20))

    if not q:
        return jsonify({"error": "Missing query parameter 'q'"}), 400
    if platform not in PLATFORMS:
        return jsonify({"error": f"Unknown platform '{platform}'"}), 400

    print(f"[Search] q={q!r} platform={platform} limit={limit}")
    try:
        results = PLATFORMS[platform].search(q, limit=limit)
        return jsonify(results)
    except Exception as e:
        print(f"[Search] Error: {e}")
        return jsonify({"error": str(e)}), 500


# ─── Trending / Home ────────────────────────────────────────────

@app.route("/api/trending")
def trending():
    """
    GET /api/trending?platform=<zing|soundcloud|youtube>
    Trả về danh sách bài trending.
    """
    platform = request.args.get("platform", "zing").lower()
    if platform not in PLATFORMS:
        return jsonify({"error": "Unknown platform"}), 400
    try:
        results = PLATFORMS[platform].get_trending(limit=20)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Stream Audio ───────────────────────────────────────────────

@app.route("/api/stream/<platform>/<path:song_id>")
def stream_audio(platform: str, song_id: str):
    """
    GET /api/stream/<platform>/<song_id>
    Proxy stream audio về cho ESP32.
    - ZingMP3 song_id: encodeId (e.g. ZW9AAZOW)
    - SoundCloud song_id: URL encode của track URL
    - YouTube song_id: video ID (e.g. dQw4w9WgXcQ)

    Hỗ trợ HTTP Range requests để ESP32-audioI2S hoạt động đúng.
    """
    if platform not in PLATFORMS:
        abort(400, "Unknown platform")

    song_id = unquote(song_id)

    # Kiểm tra cache
    cache_key = f"{platform}:{song_id}"
    with _cache_lock:
        audio_url = _stream_cache.get(cache_key)

    if not audio_url:
        print(f"[Stream] Fetching URL for {platform}:{song_id}")
        audio_url = PLATFORMS[platform].get_stream_url(song_id)
        if not audio_url:
            abort(404, "Cannot get stream URL")
        with _cache_lock:
            # Giới hạn cache 100 entries
            if len(_stream_cache) > 100:
                _stream_cache.clear()
            _stream_cache[cache_key] = audio_url

    print(f"[Stream] Proxying: {audio_url[:80]}...")

    # ── Chuyển đổi trực tiếp sang chuẩn MP3 (128kbps) bằng ffmpeg ──
    # Giúp YouTube/ZingMP3/SoundCloud phát ngay lập tức trên ESP32 mà không bị lỗi header MP4
    try:
        cmd = [
            "ffmpeg",
            "-loglevel", "quiet",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-headers", f"Referer: {_get_referer(platform)}\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n",
            "-i", audio_url,
            "-vn",
            "-acodec", "libmp3lame",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-f", "mp3",
            "pipe:1"
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=8192)

        def generate_ffmpeg():
            try:
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass

        return Response(
            stream_with_context(generate_ffmpeg()),
            status=200,
            headers={
                "Content-Type": "audio/mpeg",
                "Cache-Control": "no-cache",
                "Accept-Ranges": "none",
                "Access-Control-Allow-Origin": "*",
            },
            direct_passthrough=True,
        )

    except Exception as ex:
        print(f"[Stream] FFmpeg error: {ex}, fallback to direct proxy...")

    # ── Fallback trực tiếp nếu không gọi được ffmpeg ─────────────
    range_header = request.headers.get("Range")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ESP32AudioPlayer/1.0)",
        "Referer": _get_referer(platform),
    }
    if range_header:
        headers["Range"] = range_header

    try:
        req = urllib.request.Request(audio_url, headers=headers)
        remote = urllib.request.urlopen(req, timeout=15)
        content_type = remote.headers.get("Content-Type", "audio/mpeg")

        def generate():
            try:
                while True:
                    chunk = remote.read(8192)
                    if not chunk:
                        break
                    yield chunk
            finally:
                remote.close()

        return Response(
            stream_with_context(generate()),
            status=200,
            headers={
                "Content-Type": content_type,
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            },
            direct_passthrough=True,
        )
    except Exception as e:
        print(f"[Stream] Error: {e}")
        abort(502, str(e))


def _get_referer(platform: str) -> str:
    return {
        "zing":       "https://zingmp3.vn/",
        "soundcloud": "https://soundcloud.com/",
        "youtube":    "https://www.youtube.com/",
    }.get(platform, "")


# ─── Thumbnail proxy — trả về raw RGB565 để ESP32 vẽ trực tiếp ─

_thumb_cache: dict[str, bytes] = {}
_thumb_lock  = threading.Lock()

THUMB_SIZE = 36   # 36×36 px → 2592 bytes RGB565

@app.route("/api/thumbnail")
def thumbnail():
    """
    GET /api/thumbnail?url=<encoded_url>&size=<N>
    Tải ảnh, resize về NxN px, trả về raw RGB565 big-endian bytes.
    ESP32 đọc từng cặp byte và gọi tft.pushColor() để vẽ.
    Header X-Width / X-Height cho ESP32 biết kích thước.
    """
    url  = request.args.get("url", "").strip()
    size = int(request.args.get("size", THUMB_SIZE))
    size = max(16, min(size, 80))  # clamp 16..80

    if not url:
        abort(400, "Missing url")

    cache_key = f"{url}:{size}"
    with _thumb_lock:
        cached = _thumb_cache.get(cache_key)
    if cached:
        return Response(cached, status=200, mimetype="application/octet-stream",
                        headers={"X-Width": str(size), "X-Height": str(size),
                                 "Content-Length": str(len(cached))})

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (ESP32MusicPlayer)",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()

        img = Image.open(BytesIO(raw)).convert("RGB")
        # Center-crop vuông trước khi resize để ảnh không bị méo (đặc biệt là thumbnail 16:9 của YouTube)
        w, h = img.size
        min_dim = min(w, h)
        left = (w - min_dim) // 2
        top = (h - min_dim) // 2
        img = img.crop((left, top, left + min_dim, top + min_dim))

        # Tăng nhẹ độ tương phản & độ nét để nổi bật trên màn hình TFT 2.8"
        from PIL import ImageEnhance, ImageFilter
        img = ImageEnhance.Contrast(img).enhance(1.15)
        img = ImageEnhance.Color(img).enhance(1.1)
        img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=120, threshold=3))

        img = img.resize((size, size), Image.Resampling.LANCZOS)

        # Chuyển sang RGB565 big-endian với color dithering
        pixels = img.load()
        buf = bytearray(size * size * 2)
        idx = 0
        for y in range(size):
            for x in range(size):
                r, g, b = pixels[x, y]
                rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                buf[idx]     = (rgb565 >> 8) & 0xFF
                buf[idx + 1] = rgb565 & 0xFF
                idx += 2

        data = bytes(buf)
        with _thumb_lock:
            if len(_thumb_cache) > 200:
                _thumb_cache.clear()
            _thumb_cache[cache_key] = data

        return Response(data, status=200, mimetype="application/octet-stream",
                        headers={"X-Width": str(size), "X-Height": str(size),
                                 "Content-Length": str(len(data))})

    except Exception as e:
        print(f"[Thumbnail] Error: {e}")
        # Trả về ảnh placeholder màu xám
        placeholder = bytes([0x42, 0x08] * (size * size))  # #808080
        return Response(placeholder, status=200, mimetype="application/octet-stream",
                        headers={"X-Width": str(size), "X-Height": str(size),
                                 "Content-Length": str(len(placeholder))})


# ─── Platforms Info ──────────────────────────────────────────────

@app.route("/api/platforms")
def platforms_info():
    """Trả về thông tin các platform cho ESP32."""
    return jsonify([
        {"id": "zing",       "name": "ZingMP3",    "color": 0xFF60},
        {"id": "soundcloud", "name": "SoundCloud",  "color": 0xFC00},
        {"id": "youtube",    "name": "YouTube",     "color": 0xF800},
    ])


# ─── Player Hub (Điều phối ESP32 Màn hình & ESP32 Loa) ─────────

_player_lock = threading.Lock()
_player_state = {
    "state": "stopped",      # "playing", "paused", "stopped"
    "seq": 0,                # Tăng mỗi khi bài đổi để ESP32 loa nhận diện bài mới
    "song": None,            # Thông tin SongInfo hiện tại
    "stream_url": "",        # URL stream cho ESP32 loa
    "volume": 15,            # 0–21
    "elapsed": 0,            # Giây đã phát
    "last_update": 0,
}


@app.route("/api/player/play", methods=["POST"])
def player_play():
    """
    ESP32 Màn hình gọi khi chọn bài:
    POST /api/player/play
    Body JSON: {"song_id":"...", "platform":"youtube|soundcloud|zing", "title":"...", "artist":"...", "thumbnail":"...", "duration":180}
    """
    data = request.get_json(force=True, silent=True) or {}
    platform = str(data.get("platform", "youtube")).lower()
    song_id  = str(data.get("song_id", "")).strip()

    if not song_id:
        return jsonify({"error": "Missing song_id"}), 400

    # Lấy stream url
    stream_url = f"/api/stream/{platform}/{song_id}"

    with _player_lock:
        _player_state["seq"] += 1
        _player_state["state"] = "playing"
        _player_state["stream_url"] = stream_url
        _player_state["song"] = {
            "id": song_id,
            "platform": platform,
            "title": data.get("title", "Unknown"),
            "artist": data.get("artist", "Unknown"),
            "thumbnail": data.get("thumbnail", ""),
            "duration": int(data.get("duration", 0)),
        }
        _player_state["elapsed"] = 0

    print(f"[PlayerHub] PLAY #{_player_state['seq']}: {data.get('title')} ({platform})")
    return jsonify({"status": "ok", "seq": _player_state["seq"]})


@app.route("/api/player/control", methods=["POST"])
def player_control():
    """
    Điều khiển Play/Pause/Stop/Volume/Next:
    POST /api/player/control
    Body JSON: {"action":"pause|resume|stop|vol_up|vol_down|set_vol|next", "value":15}
    """
    data   = request.get_json(force=True, silent=True) or {}
    action = str(data.get("action", "")).lower()

    with _player_lock:
        if action == "pause":
            _player_state["state"] = "paused"
        elif action == "resume":
            _player_state["state"] = "playing"
        elif action == "stop":
            _player_state["state"] = "stopped"
            _player_state["stream_url"] = ""
            _player_state["song"] = None
        elif action == "vol_up":
            _player_state["volume"] = min(21, _player_state["volume"] + 1)
        elif action == "vol_down":
            _player_state["volume"] = max(0, _player_state["volume"] - 1)
        elif action == "set_vol":
            _player_state["volume"] = max(0, min(21, int(data.get("value", 15))))
        elif action == "next":
            _player_state["state"] = "next_requested"

    print(f"[PlayerHub] CONTROL: {action} (vol={_player_state['volume']}, state={_player_state['state']})")
    return jsonify({"status": "ok", "state": _player_state["state"], "volume": _player_state["volume"]})


@app.route("/api/player/status", methods=["GET"])
def player_status():
    """
    Cả ESP32 Màn hình và ESP32 Loa đều gọi để đồng bộ:
    GET /api/player/status
    """
    with _player_lock:
        # Nếu stream_url là relative -> chuyển thành absolute URL cho ESP32 loa
        url = _player_state["stream_url"]
        if url.startswith("/"):
            host = request.host
            url = f"http://{host}{url}"

        resp = {
            "status": "ok",
            "state": _player_state["state"],
            "seq": _player_state["seq"],
            "stream_url": url,
            "volume": _player_state["volume"],
            "elapsed": _player_state["elapsed"],
            "song": _player_state["song"],
        }
    return jsonify(resp)


@app.route("/api/player/report", methods=["POST"])
def player_report():
    """
    ESP32 Loa báo cáo tiến độ và sự kiện:
    POST /api/player/report
    Body: {"elapsed": 45, "event": "eof|error|playing"}
    """
    data = request.get_json(force=True, silent=True) or {}
    elapsed = data.get("elapsed")
    event   = data.get("event")

    with _player_lock:
        if elapsed is not None:
            _player_state["elapsed"] = int(elapsed)
        if event == "eof":
            print(f"[PlayerHub] Song finished (EOF) -> Requesting Next Song")
            _player_state["state"] = "next_requested"

    return jsonify({"status": "ok"})


# ─── Health check ──────────────────────────────────────────────


@app.route("/api/ping")
def ping():
    return jsonify({"status": "ok", "version": "2.0"})


# ─── Entry Point ───────────────────────────────────────────────

if __name__ == "__main__":
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    print("=" * 60)
    print("  ESP32 Music Player Server v2")
    print("=" * 60)
    print(f"  Local URL  : http://localhost:5000")
    print(f"  Network URL: http://{local_ip}:5000")
    print("=" * 60)
    print("  Platforms: ZingMP3 | SoundCloud | YouTube")
    print()
    print("  📋 Nhập vào config.h của ESP32:")
    print(f'     #define SERVER_IP  "{local_ip}"')
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
