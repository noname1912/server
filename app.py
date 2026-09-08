"""
ESP32 Music Player — Python Flask Server v2
Hỗ trợ: ZingMP3 | SoundCloud | YouTube
"""

import os
import time
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
import db

app = Flask(__name__)
CORS(app)
db.init_db()

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
        stype    = str(data.get("type", "song")).lower()
    else:
        q        = request.args.get("q", "").strip()
        platform = request.args.get("platform", "zing").lower()
        limit    = int(request.args.get("limit", 20))
        stype    = str(request.args.get("type", "song")).lower()

    if not q:
        return jsonify({"error": "Missing query parameter 'q'"}), 400
    if platform not in PLATFORMS:
        return jsonify({"error": f"Unknown platform '{platform}'"}), 400

    print(f"[Search] q={q!r} platform={platform} type={stype} limit={limit}")
    # Lưu từ khóa vào lịch sử tìm kiếm SQLite
    try:
        db.add_search_history(q, platform)
    except Exception as dbe:
        print(f"[Search] DB history error: {dbe}")

    try:
        plat_obj = PLATFORMS[platform]
        if stype == "playlist" and hasattr(plat_obj, "search_playlists"):
            results = plat_obj.search_playlists(q, limit=limit)
            for r in results:
                r["title"] = db.to_ascii_simple(r.get("title", ""))
                r["artist"] = db.to_ascii_simple(r.get("artist", ""))
            return jsonify(results)

        results = plat_obj.search(q, limit=limit)
        # Lưu kết quả tìm kiếm vào danh sách phát (Playlist) để tự động Next bài
        global _playlist, _playlist_idx
        with _player_lock:
            _playlist = list(results)
            _playlist_idx = 0
        return jsonify(results)
    except Exception as e:
        print(f"[Search] Error: {e}")
        return jsonify({"error": str(e)}), 500


# ─── Lấy danh sách bài hát trong Playlist ──────────────────────

@app.route("/api/playlist/tracks", methods=["GET"])
def playlist_tracks():
    """
    GET /api/playlist/tracks?id=<playlist_id>&platform=<youtube|zing|soundcloud>&limit=30
    Lấy danh sách các bài hát trong Playlist và tự động nạp vào playlist phát của server.
    """
    playlist_id = request.args.get("id", "").strip()
    platform    = request.args.get("platform", "youtube").lower()
    limit       = int(request.args.get("limit", 30))

    if not playlist_id:
        return jsonify({"error": "Missing 'id'"}), 400
    if platform not in PLATFORMS:
        return jsonify({"error": f"Unknown platform '{platform}'"}), 400

    try:
        plat_obj = PLATFORMS[platform]
        if hasattr(plat_obj, "get_playlist_tracks"):
            tracks = plat_obj.get_playlist_tracks(playlist_id, limit=limit)
            for t in tracks:
                t["title"] = db.to_ascii_simple(t.get("title", ""))
                t["artist"] = db.to_ascii_simple(t.get("artist", ""))
        else:
            tracks = []

        # Tự động nạp vào hàng đợi phát (Playlist) để tự động Next bài
        global _playlist, _playlist_idx
        with _player_lock:
            if tracks:
                _playlist = list(tracks)
                _playlist_idx = 0

        return jsonify(tracks)
    except Exception as e:
        print(f"[PlaylistTracks] Error: {e}")
        return jsonify({"error": str(e)}), 500


# ─── Lịch sử tìm kiếm & Gợi ý (Search History & Suggestions) ──

@app.route("/api/history", methods=["GET", "POST"])
def search_history():
    """
    GET  /api/history?limit=5  -> Trả về danh sách [str] các tìm kiếm gần nhất
    POST /api/history          -> Thêm từ khóa {"q": "..."}
    """
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        q = str(data.get("q", "")).strip()
        p = str(data.get("platform", "youtube")).lower()
        if q:
            db.add_search_history(q, p)
        return jsonify({"status": "ok"})
    else:
        limit = int(request.args.get("limit", 5))
        platform = request.args.get("platform", "").strip().lower()
        history = db.get_recent_searches(limit=limit, platform=platform or None)
        return jsonify(history)


@app.route("/api/suggest", methods=["GET"])
def search_suggest():
    """
    GET /api/suggest?q=<prefix>&limit=5&platform=zing|soundcloud|youtube
    Trả về danh sách gợi ý phù hợp với từ khóa đang gõ theo từng nền tảng
    """
    prefix = request.args.get("q", "").strip()
    platform = request.args.get("platform", "youtube").strip().lower()
    limit = int(request.args.get("limit", 5))
    suggestions = db.get_suggestions(prefix, limit=limit, platform=platform)
    return jsonify(suggestions)


# ─── Bài hát yêu thích (Favorites — SQLite3) ──────────────────

@app.route("/api/favorites", methods=["GET"])
def get_favorites():
    """
    GET /api/favorites
    Trả về danh sách các bài hát yêu thích đã lưu trong CSDL SQLite
    """
    favs = db.get_favorites()
    return jsonify(favs)


@app.route("/api/favorites/ids", methods=["GET"])
def get_favorite_ids():
    """
    GET /api/favorites/ids
    Trả về mảng ID các bài hát yêu thích để ESP32 hiển thị tim đỏ nhanh chóng
    """
    ids = db.get_favorite_ids()
    return jsonify(ids)


@app.route("/api/favorites/toggle", methods=["POST"])
def toggle_favorite():
    """
    POST /api/favorites/toggle
    Body JSON: {"id": "...", "platform": "...", "title": "...", "artist": "...", "duration": 180, "thumbnail": "..."}
    Thêm/Xóa bài hát khỏi danh sách yêu thích
    """
    data = request.get_json(force=True, silent=True) or {}
    song_id = str(data.get("id", "")).strip()
    if not song_id:
        return jsonify({"error": "Missing song id"}), 400

    is_fav = db.toggle_favorite(data)
    return jsonify({
        "status": "ok",
        "id": song_id,
        "is_favorite": is_fav
    })


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
                    chunk = proc.stdout.read(16384)
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
                    chunk = remote.read(16384)
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
_playlist: list[dict] = []
_playlist_idx: int = 0
_player_state = {
    "state": "stopped",      # "playing", "paused", "stopped"
    "seq": int(time.time()), # Dùng timestamp để luôn tăng và không bao giờ bị trùng
    "song": None,            # Thông tin SongInfo hiện tại
    "stream_url": "",        # URL stream cho ESP32 loa
    "volume": 15,            # 0–21
    "elapsed": 0,            # Giây đã phát
    "last_update": 0,
}

def _auto_play_next() -> bool:
    global _playlist, _playlist_idx
    if not _playlist:
        return False
    _playlist_idx = (_playlist_idx + 1) % len(_playlist)
    next_song = _playlist[_playlist_idx]
    platform = str(next_song.get("platform", "youtube")).lower()
    song_id  = str(next_song.get("id", "")).strip()
    if not song_id:
        return False
    _player_state["seq"] = int(time.time() * 1000) % 2000000000
    _player_state["state"] = "playing"
    if song_id.startswith("http"):
        import base64
        safe_id = "b64_" + base64.urlsafe_b64encode(song_id.encode()).decode().rstrip("=")
    else:
        safe_id = song_id
    _player_state["stream_url"] = f"/api/stream/{platform}/{safe_id}"
    _player_state["song"] = next_song
    _player_state["elapsed"] = 0
    print(f"[PlayerHub-AutoNext] Auto-play #{_player_state['seq']}: {next_song.get('title')} ({platform})")
    return True


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

    # Lấy stream url an toàn cho ESP32 Audio parser (tránh chứa http:// trong path)
    if song_id.startswith("http"):
        import base64
        safe_id = "b64_" + base64.urlsafe_b64encode(song_id.encode()).decode().rstrip("=")
    else:
        safe_id = song_id
    stream_url = f"/api/stream/{platform}/{safe_id}"

    with _player_lock:
        # Cập nhật vị trí bài trong playlist nếu có
        global _playlist, _playlist_idx
        for i, s in enumerate(_playlist):
            if str(s.get("id")) == song_id:
                _playlist_idx = i
                break
        else:
            _playlist.append({
                "id": song_id,
                "platform": platform,
                "title": data.get("title", "Unknown"),
                "artist": data.get("artist", "Unknown"),
                "thumbnail": data.get("thumbnail", ""),
                "duration": int(data.get("duration", 0)),
            })
            _playlist_idx = len(_playlist) - 1

        _player_state["seq"] = int(time.time() * 1000) % 2000000000
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
            _player_state["seq"] = int(time.time() * 1000) % 2000000000
            _player_state["state"] = "playing"
        elif action == "stop":
            _player_state["state"] = "stopped"
            _player_state["stream_url"] = ""
            _player_state["song"] = None
        elif action == "vol_up":
            _player_state["volume"] = min(21, _player_state["volume"] + 2)
        elif action == "vol_down":
            _player_state["volume"] = max(0, _player_state["volume"] - 2)
        elif action == "set_vol":
            _player_state["volume"] = max(0, min(21, int(data.get("value", 15))))
        elif action == "next":
            if not _auto_play_next():
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
    import time as _time
    data = request.get_json(force=True, silent=True) or {}
    elapsed = data.get("elapsed")
    event   = data.get("event")

    with _player_lock:
        if elapsed is not None:
            _player_state["elapsed"] = int(elapsed)

        if event == "eof":
            cur_elapsed = int(elapsed or 0)
            # Bỏ qua EOF nếu bài hát vừa bắt đầu < 5 giây (false EOF khi ESP32 mất kết nối tạm thời)
            if cur_elapsed < 5:
                print(f"[PlayerHub] Bỏ qua EOF giả (elapsed={cur_elapsed}s < 5s)")
            else:
                # Cooldown 4 giây giữa 2 lần auto-next liên tiếp
                last_next = _player_state.get("_last_auto_next_ts", 0)
                now_ts = _time.time()
                if now_ts - last_next < 4:
                    print(f"[PlayerHub] Bỏ qua EOF (cooldown còn {4-(now_ts-last_next):.1f}s)")
                else:
                    _player_state["_last_auto_next_ts"] = now_ts
                    print(f"[PlayerHub] Song finished (EOF, elapsed={cur_elapsed}s) -> Auto-Next...")
                    if not _auto_play_next():
                        _player_state["state"] = "next_requested"

        elif event == "error":
            print(f"[PlayerHub] Loa bao loi stream -> Giu nguyen bai hien tai")

    return jsonify({"status": "ok"})


# ─── Health check ──────────────────────────────────────────────


@app.route("/api/ping")
def ping():
    return jsonify({"status": "ok", "version": "2.0"})


@app.route("/api/debug/log")
def debug_log():
    try:
        with open("server.log", "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return Response("".join(lines[-100:]), mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return str(e), 500


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
