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
                   Response, stream_with_context, abort, send_file)
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


# ─── Disk Audio Cache (Chống giật tuyệt đối cho bài dài YouTube / Zing) ─
AUDIO_CACHE_DIR = Path(__file__).parent / "audio_cache"
AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_transcode_lock = threading.Lock()
_active_transcodes: dict[str, subprocess.Popen] = {}

def _cleanup_old_cache(max_files: int = 60):
    """Giữ lại 60 bài hát gần nhất trong cache, tự động xóa bài cũ."""
    try:
        files = list(AUDIO_CACHE_DIR.glob("*.mp3"))
        if len(files) > max_files:
            files.sort(key=lambda f: f.stat().st_mtime)
            for f in files[:-max_files]:
                try:
                    f.unlink()
                except Exception:
                    pass
    except Exception as ex:
        print(f"[Cache] Cleanup error: {ex}")

def _get_cache_paths(platform: str, song_id: str):
    import hashlib
    clean_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in song_id)[:40]
    hash_id  = hashlib.md5(f"{platform}:{song_id}".encode()).hexdigest()[:10]
    base_name = f"{platform}_{clean_id}_{hash_id}"
    final_mp3 = AUDIO_CACHE_DIR / f"{base_name}.mp3"
    tmp_mp3   = AUDIO_CACHE_DIR / f"{base_name}.tmp.mp3"
    return final_mp3, tmp_mp3


# ─── Stream Audio ───────────────────────────────────────────────

@app.route("/api/stream/<platform>/<path:song_id>")
def stream_audio(platform: str, song_id: str):
    """
    GET /api/stream/<platform>/<song_id>
    Proxy stream audio về cho ESP32.
    Tự động đệm vào ổ cứng SSD cục bộ (audio_cache) để bài dài (10p, 30p, 1h)
    không bao giờ bị Google Video throttle hay đứt gãy kết nối!
    """
    if platform not in PLATFORMS:
        abort(400, "Unknown platform")

    song_id = unquote(song_id)
    final_mp3, tmp_mp3 = _get_cache_paths(platform, song_id)

    # 1. NẾU ĐÃ CÓ TRONG CACHE HOÀN CHỈNH -> PHÁT NGAY LẬP TỨC 100% MƯỢT
    if final_mp3.exists() and final_mp3.stat().st_size > 20000:
        print(f"[Stream] Serving cached: {final_mp3.name} ({final_mp3.stat().st_size // 1024} KB)")
        return send_file(str(final_mp3), mimetype="audio/mpeg", conditional=True)

    # 2. NẾU CHƯA CÓ TRONG CACHE:
    # Lấy stream URL gốc
    cache_key = f"{platform}:{song_id}"
    with _cache_lock:
        audio_url = _stream_cache.get(cache_key)

    if not audio_url:
        print(f"[Stream] Fetching URL for {platform}:{song_id}")
        audio_url = PLATFORMS[platform].get_stream_url(song_id)
        if not audio_url:
            abort(404, "Cannot get stream URL")
        with _cache_lock:
            if len(_stream_cache) > 100:
                _stream_cache.clear()
            _stream_cache[cache_key] = audio_url

    print(f"[Stream] Caching & streaming: {platform}:{song_id} -> {tmp_mp3.name}")

    # Bắt đầu transcode ghi vào tmp_mp3 với tốc độ tối đa (unthrottled)
    with _transcode_lock:
        proc = _active_transcodes.get(cache_key)
        if proc is None or proc.poll() is not None:
            # Xóa tmp cũ nếu có
            if tmp_mp3.exists():
                try: tmp_mp3.unlink()
                except Exception: pass

            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel", "quiet",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-headers", f"Referer: {_get_referer(platform)}\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n",
                "-thread_queue_size", "4096",
                "-i", audio_url,
                "-vn",
                "-af", "volume=0.80",
                "-acodec", "libmp3lame",
                "-b:a", "128k",
                "-ar", "44100",
                "-ac", "2",
                "-f", "mp3",
                "-write_xing", "0",
                str(tmp_mp3)
            ]
            proc = subprocess.Popen(cmd)
            _active_transcodes[cache_key] = proc

            # Thread giám sát khi transcode xong -> đổi tên thành final_mp3
            def _watch_transcode(p=proc, tmp_p=tmp_mp3, final_p=final_mp3, ckey=cache_key):
                try:
                    p.wait()
                    if p.returncode == 0 and tmp_p.exists() and tmp_p.stat().st_size > 20000:
                        if final_p.exists():
                            try: final_p.unlink()
                            except Exception: pass
                        tmp_p.rename(final_p)
                        print(f"[Stream-Cache] Hoàn tất cache: {final_p.name} ({final_p.stat().st_size // 1024} KB)")
                        _cleanup_old_cache(60)
                    else:
                        print(f"[Stream-Cache] Transcode finished/aborted for {ckey}")
                except Exception as ex:
                    print(f"[Stream-Cache] Watch error: {ex}")
                finally:
                    with _transcode_lock:
                        _active_transcodes.pop(ckey, None)

            threading.Thread(target=_watch_transcode, daemon=True).start()

    # 3. STREAM PROGRESSIVE: Đọc từ file tmp_mp3 đang lớn dần
    # Chờ file có ít nhất 16KB dữ liệu ban đầu (~1 giây nhạc)
    t_wait = time.time()
    while not tmp_mp3.exists() or tmp_mp3.stat().st_size < 16384:
        if proc.poll() is not None and (not tmp_mp3.exists() or tmp_mp3.stat().st_size == 0):
            print("[Stream] FFmpeg khởi động thất bại!")
            abort(502, "Transcode startup failed")
        time.sleep(0.04)
        if time.time() - t_wait > 6:
            break

    def generate_progressive(tmp_p=tmp_mp3, p=proc, final_p=final_mp3):
        curr_file = final_p if final_p.exists() else tmp_p
        try:
            with open(curr_file, "rb") as f:
                while True:
                    chunk = f.read(4096)
                    if chunk:
                        yield chunk
                    else:
                        # Kiểm tra xem transcode đã hoàn tất chưa
                        if p.poll() is not None:
                            rem = f.read()
                            if rem:
                                yield rem
                            break
                        time.sleep(0.04)
        except Exception:
            # Client ngắt kết nối
            pass

    return Response(
        stream_with_context(generate_progressive()),
        status=200,
        headers={
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-cache",
            "Accept-Ranges": "none",
            "Access-Control-Allow-Origin": "*",
        },
        direct_passthrough=True,
    )


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
    "volume": 11,            # 0–21 (Mức 11: Chuẩn an toàn cho PAM8403, chống méo clipping và rè)
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

    # Xóa cache của bài tiếp theo để luôn lấy stream URL tươi mới
    with _cache_lock:
        _stream_cache.pop(f"{platform}:{song_id}", None)
        _stream_cache.pop(f"{platform}:{safe_id}", None)

    # Pre-warm stream URL của bài tiếp theo
    def _prewarm_next(plat=platform, sid=song_id, sfid=safe_id):
        try:
            p_obj = PLATFORMS.get(plat)
            if p_obj and hasattr(p_obj, "get_stream_url"):
                fresh = p_obj.get_stream_url(sid)
                if fresh:
                    with _cache_lock:
                        _stream_cache[f"{plat}:{sid}"] = fresh
                        _stream_cache[f"{plat}:{sfid}"] = fresh
                    print(f"[PlayerHub] Pre-warmed next URL for {plat}:{sid}")
        except Exception as ex_pw:
            print(f"[PlayerHub] Pre-warm next error: {ex_pw}")

    threading.Thread(target=_prewarm_next, daemon=True).start()

    _player_state["stream_url"] = f"/api/stream/{platform}/{safe_id}?t={_player_state['seq']}"
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

    # Xóa cache cũ của bài hát này để ép buộc lấy link stream mới 100% (tránh lỗi 403 link hết hạn khi nghe lại bài cũ)
    with _cache_lock:
        _stream_cache.pop(f"{platform}:{song_id}", None)
        _stream_cache.pop(f"{platform}:{safe_id}", None)

    # Pre-warm: Trích xuất ngay stream URL mới vào cache để khi Loa ESP32 kết nối có link ngay lập tức
    def _prewarm_play(plat=platform, sid=song_id, sfid=safe_id):
        try:
            p_obj = PLATFORMS.get(plat)
            if p_obj and hasattr(p_obj, "get_stream_url"):
                fresh = p_obj.get_stream_url(sid)
                if fresh:
                    with _cache_lock:
                        _stream_cache[f"{plat}:{sid}"] = fresh
                        _stream_cache[f"{plat}:{sfid}"] = fresh
                    print(f"[PlayerHub] Pre-warmed play URL for {plat}:{sid}")
        except Exception as ex_pw:
            print(f"[PlayerHub] Pre-warm play error: {ex_pw}")

    threading.Thread(target=_prewarm_play, daemon=True).start()

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
        _player_state["stream_url"] = f"/api/stream/{platform}/{safe_id}?t={_player_state['seq']}"
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


# ─── Quản lý Startup Chime khi Loa mở nguồn ─────────────────────
_last_speaker_poll = 0

@app.route("/api/stream/startup")
def stream_startup():
    """Phát file âm thanh khởi động MP3"""
    startup_path = Path(__file__).parent / "static" / "startup.mp3"
    if not startup_path.exists():
        abort(404, "Startup sound not found")
    return send_file(str(startup_path), mimetype="audio/mpeg", conditional=True)


@app.route("/api/player/startup", methods=["GET", "POST"])
def trigger_startup_sound():
    """Trigger phát chuông khởi động thủ công hoặc từ giao diện web"""
    with _player_lock:
        now = time.time()
        _player_state["seq"] = int(now * 1000) % 2000000000
        _player_state["state"] = "playing"
        _player_state["stream_url"] = "/api/stream/startup"
        _player_state["song"] = {
            "id": "startup_jingle",
            "platform": "system",
            "title": "Startup Chime (Khởi Động)",
            "artist": "ESP32 Music Box",
            "thumbnail": "",
            "duration": 3,
        }
        _player_state["elapsed"] = 0
    print("[PlayerHub] Triggered startup chime!")
    return jsonify({"status": "ok", "message": "Startup sound triggered"})


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
            # Nếu là bài khởi động vừa phát xong -> chuyển về trạng thái chờ
            if _player_state.get("song") and _player_state["song"].get("id") == "startup_jingle":
                print("[PlayerHub] >>> Nhạc khởi động đã phát xong! Chuyển trạng thái sang sẵn sàng.")
                _player_state["state"] = "stopped"
                _player_state["stream_url"] = ""
                _player_state["song"] = None
                return jsonify({"status": "ok", "action": "standby"})

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
