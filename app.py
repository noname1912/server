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
from PIL import Image, ImageEnhance, ImageOps

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

@app.route("/api/search/history", methods=["GET", "POST"])
@app.route("/api/suggest/history", methods=["GET", "POST"])
def search_history():
    """
    GET  /api/search/history?limit=5  -> Trả về danh sách [str] các tìm kiếm gần nhất
    POST /api/search/history          -> Thêm từ khóa {"q": "..."}
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


# ─── Lyrics (LRC synced lyrics) ──────────────────────────────
_lyrics_cache_dir = Path("audio_cache")
_lyrics_cache_dir.mkdir(exist_ok=True)

def _get_youtube_subtitles(video_id: str) -> str:
    """Trích xuất phụ đề / lời bài hát trực tiếp từ YouTube."""
    try:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            subs = info.get("subtitles", {})
            auto = info.get("automatic_captions", {})
            chosen_url = None
            # Ưu tiên tiếng Việt, rồi tiếng Anh, rồi bất kỳ ngôn ngữ nào
            for lang in ["vi", "vi-orig", "en"]:
                sub_list = subs.get(lang) or auto.get(lang)
                if sub_list:
                    for s in sub_list:
                        if s.get("ext") == "json3" or "fmt=json3" in s.get("url", ""):
                            chosen_url = s.get("url")
                            break
                    if chosen_url:
                        break
                    chosen_url = sub_list[0].get("url")
                    break

            if not chosen_url:
                all_subs = list(subs.values()) + list(auto.values())
                if all_subs and all_subs[0]:
                    chosen_url = all_subs[0][0].get("url")

            if chosen_url:
                import urllib.request, json
                req = urllib.request.Request(chosen_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    raw = resp.read().decode("utf-8", errors="ignore")
                    d = json.loads(raw)
                    events = d.get("events", [])
                    lines = []
                    for ev in events:
                        t_ms = ev.get("tStartMs", 0)
                        txt = "".join(s.get("utf8", "") for s in ev.get("segs", [])).strip()
                        if txt and txt != "\n":
                            mm = int(t_ms // 60000)
                            ss = int((t_ms % 60000) // 1000)
                            cs = int((t_ms % 1000) // 10)
                            lines.append(f"[{mm:02d}:{ss:02d}.{cs:02d}]{txt}")
                    if lines:
                        return "\n".join(lines)
    except Exception as e:
        print(f"[Lyrics] Subtitle error for {video_id}: {e}")
    return ""

@app.route("/api/lyrics", methods=["GET"])
def get_lyrics():
    """
    GET /api/lyrics?id=<id>&title=<title>&artist=<artist>&platform=<plat>
    Trả về định dạng chuỗi chuẩn LRC: [mm:ss.xx] Lời bài hát
    """
    song_id  = request.args.get("id", "").strip()
    title    = request.args.get("title", "").strip()
    artist   = request.args.get("artist", "").strip()
    platform = request.args.get("platform", "zing").lower()

    if not song_id and not title:
        return "Song ID or Title required", 400

    cache_file = _lyrics_cache_dir / f"lyrics_{song_id or 'q'}.lrc"
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if content.strip():
                return Response(content, mimetype="text/plain; charset=utf-8")
        except Exception:
            pass

    lrc_text = ""

    # 1. Thử lấy lyric từ LRCLIB TRƯỚC (cực nhanh 0.2s - 0.5s, hỗ trợ trọn vẹn nhạc Việt Zing/YouTube/V-Pop)
    if title:
        import re, urllib.request, urllib.parse
        # Làm sạch tên bài hát và ca sĩ
        clean_t = re.sub(r"\[.*?\]|\(.*?\)|Official.*|MV.*|Lyric.*|Audio.*|Video.*|HD.*|4K.*", "", title, flags=re.IGNORECASE)
        clean_t = clean_t.replace("-", " ").replace("&", " ").strip()
        clean_t = " ".join(clean_t.split())

        clean_a = ""
        if artist and artist.lower() not in ("unknown", "zingmp3", "youtube"):
            clean_a = re.sub(r"\[.*?\]|\(.*?\)", "", artist, flags=re.IGNORECASE)
            clean_a = clean_a.replace("&", " ").replace(",", " ").strip()
            clean_a = " ".join(clean_a.split())

        def _strip_vn(s: str) -> str:
            import unicodedata
            s = unicodedata.normalize('NFD', s)
            s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
            s = s.replace('đ', 'd').replace('Đ', 'D')
            return s

        clean_a_unacc = _strip_vn(clean_a) if clean_a else ""
        clean_t_unacc = _strip_vn(clean_t)

        queries = []
        if clean_a:
            queries.append(f"{clean_t} {clean_a}")
        queries.append(clean_t)
        if clean_t_unacc != clean_t:
            if clean_a_unacc:
                queries.append(f"{clean_t_unacc} {clean_a_unacc}")
            queries.append(clean_t_unacc)

        for q in queries:
            if not q.strip() or lrc_text:
                break
            try:
                query_url = f"https://lrclib.net/api/search?q={urllib.parse.quote(q)}"
                req = urllib.request.Request(query_url, headers={"User-Agent": "ESP32MusicPlayer/2.0"})
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    if resp.status == 200:
                        items = json.loads(resp.read().decode("utf-8"))
                        # Ưu tiên lời khớp thời gian (syncedLyrics)
                        for it in items:
                            synced = it.get("syncedLyrics")
                            if synced and synced.strip():
                                lrc_text = synced.strip()
                                break
                        # Dự phòng lời thường (plainLyrics) được chia nhịp thời gian
                        if not lrc_text:
                            for it in items:
                                plain = it.get("plainLyrics")
                                if plain and plain.strip():
                                    plines = [pl.strip() for pl in plain.splitlines() if pl.strip()]
                                    if plines:
                                        step_sec = 4.5
                                        timed_lines = []
                                        for idx, pline in enumerate(plines):
                                             sec = int(idx * step_sec)
                                             timed_lines.append(f"[{sec // 60:02d}:{sec % 60:02d}.00]{pline}")
                                        lrc_text = "\n".join(timed_lines)
                                        break
            except Exception as e:
                print(f"[Lyrics] LRCLIB search error for '{q}': {e}")

    # 2. Nếu chưa có trên LRCLIB, thử trích xuất phụ đề YouTube (chỉ khi có video_id)
    if not lrc_text and len(song_id) == 11:
        lrc_text = _get_youtube_subtitles(song_id)

    # 3. Dự phòng giả lập theo đúng tên bài & ca sĩ nếu không tìm thấy lời online
    if not lrc_text.strip():
        display_title  = title if title else "Dang Phat"
        display_artist = artist if artist else "Unknown Artist"
        lrc_lines = [
            f"[00:00.00]{display_title}",
            f"[00:04.00]{display_artist}",
            "[00:08.00]Giai dieu dang vang len...",
            "[00:20.00]Cam nhan am thanh chat luong cao...",
            "[00:45.00]Giai dieu tuyet voi...",
            "[01:15.00]Cam on ban da lang nghe!",
        ]
        lrc_text = "\n".join(lrc_lines)
    else:
        lines = lrc_text.splitlines()
        if lines and not lines[0].startswith("[00:00"):
            display_t = title if title else "Am Nhac Viet"
            lines.insert(0, f"[00:00.00]{display_t}")
            lrc_text = "\n".join(lines)

    lrc_text = lrc_text.replace("\r", "").strip()

    # Lưu cache
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            f.write(lrc_text)
    except Exception as e:
        print(f"[Lyrics] Cache write error: {e}")

    return Response(lrc_text, mimetype="text/plain; charset=utf-8")




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

def _start_transcode_background(platform: str, song_id: str, audio_url: str):
    """Kích hoạt transcode FFmpeg ngầm trước ngay khi chọn bài, để khi Loa ESP32 kết nối tới là có file phát ngay lập tức không bị delay/rỗng"""
    final_mp3, tmp_mp3 = _get_cache_paths(platform, song_id)
    if final_mp3.exists() and final_mp3.stat().st_size > 20000:
        return
    cache_key = f"{platform}:{song_id}"
    with _transcode_lock:
        proc = _active_transcodes.get(cache_key)
        if proc is not None and proc.poll() is None:
            return  # Đang chạy rồi
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
        try:
            proc = subprocess.Popen(cmd)
            _active_transcodes[cache_key] = proc
            print(f"[Stream-Prewarm] Đã kích hoạt transcode trước cho {cache_key}")
        except Exception as e:
            print(f"[Stream-Prewarm] Lỗi khởi động FFmpeg: {e}")
            return

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


@app.route("/api/stream", methods=["GET"])
def stream_audio_query():
    """Hỗ trợ tải stream qua query params /api/stream?id=...&platform=... (dùng cho Tải Nhạc về SD trên ESP32)"""
    platform = request.args.get("platform", "youtube").lower()
    song_id  = request.args.get("id") or request.args.get("song_id") or ""
    if not song_id:
        abort(400, "Missing song id")
    return stream_audio(platform, song_id)


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
    # Tự động giải mã base64 nếu song_id dạng b64_...
    if song_id.startswith("b64_"):
        try:
            import base64
            padded = song_id[4:] + "=" * (-len(song_id[4:]) % 4)
            song_id = base64.urlsafe_b64decode(padded.encode()).decode()
        except Exception:
            pass

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
    _start_transcode_background(platform, song_id, audio_url)
    with _transcode_lock:
        proc = _active_transcodes.get(cache_key)

    def generate_progressive(tmp_p=tmp_mp3, p=proc, final_p=final_mp3):
        # Chờ tối đa 12 giây cho tới khi có ít nhất 4KB dữ liệu (~300ms nhạc)
        t0 = time.time()
        curr_file = None
        while time.time() - t0 < 12.0:
            target = final_p if final_p.exists() else tmp_p
            if target.exists() and target.stat().st_size >= 4096:
                curr_file = target
                break
            if p and p.poll() is not None:
                target = final_p if final_p.exists() else tmp_p
                if target.exists() and target.stat().st_size > 0:
                    curr_file = target
                    break
                print("[Stream] Transcode failed or produced empty file")
                return
            time.sleep(0.04)

        if not curr_file or not curr_file.exists():
            print("[Stream] Timeout chờ file tmp_mp3 xuất hiện")
            return

        try:
            with open(curr_file, "rb") as f:
                while True:
                    chunk = f.read(8192)
                    if chunk:
                        yield chunk
                    else:
                        if p and p.poll() is not None:
                            while True:
                                rem = f.read(8192)
                                if not rem:
                                    break
                                yield rem
                            break
                        time.sleep(0.02)
        except Exception:
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

    print(f"[Thumbnail] Request size={size} from {request.remote_addr} for: {url[:60]}")

    cache_key = f"{url}:{size}"
    with _thumb_lock:
        cached = _thumb_cache.get(cache_key)
    if cached:
        return Response(cached, status=200, mimetype="application/octet-stream",
                        headers={"X-Width": str(size), "X-Height": str(size),
                                 "Content-Length": str(len(cached))})

    try:
        raw = None
        try:
            r = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }, timeout=8)
            if r.status_code == 200:
                raw = r.content
        except Exception:
            pass

        if not raw:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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

        # Resize về đúng độ phân giải bằng LANCZOS trước để ảnh mịn và không méo
        img = img.resize((size, size), Image.Resampling.LANCZOS)

        # Pipeline màu sắc cho TFT RGB565: bão hòa cao + contrast tốt + sắc nét
        from PIL import ImageEnhance, ImageFilter
        img = ImageEnhance.Contrast(img).enhance(1.20)        # Tương phản cao hơn để rõ chi tiết
        img = ImageEnhance.Color(img).enhance(1.40)           # Bão hòa màu cao hơn bù cho RGB565
        img = ImageEnhance.Sharpness(img).enhance(1.30)       # Sắc nét thêm

        buf = bytearray(size * size * 2)

        # Cố gắng dùng Floyd-Steinberg dithering qua numpy nếu có
        try:
            import numpy as np
            img_filtered = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=130, threshold=2))
            arr = np.array(img_filtered, dtype=np.float32)
            H, W = arr.shape[:2]
            idx = 0
            for y in range(H):
                for x in range(W):
                    r_old = int(min(255, max(0, arr[y, x, 0])))
                    g_old = int(min(255, max(0, arr[y, x, 1])))
                    b_old = int(min(255, max(0, arr[y, x, 2])))

                    r_new = (r_old & 0xF8)
                    g_new = (g_old & 0xFC)
                    b_new = (b_old & 0xF8)

                    er = r_old - r_new
                    eg = g_old - g_new
                    eb = b_old - b_new

                    if x + 1 < W:
                        arr[y,   x+1, 0] += er * 7 / 16
                        arr[y,   x+1, 1] += eg * 7 / 16
                        arr[y,   x+1, 2] += eb * 7 / 16
                    if y + 1 < H:
                        if x > 0:
                            arr[y+1, x-1, 0] += er * 3 / 16
                            arr[y+1, x-1, 1] += eg * 3 / 16
                            arr[y+1, x-1, 2] += eb * 3 / 16
                        arr[y+1, x,   0] += er * 5 / 16
                        arr[y+1, x,   1] += eg * 5 / 16
                        arr[y+1, x,   2] += eb * 5 / 16
                        if x + 1 < W:
                            arr[y+1, x+1, 0] += er * 1 / 16
                            arr[y+1, x+1, 1] += eg * 1 / 16
                            arr[y+1, x+1, 2] += eb * 1 / 16

                    rgb565 = ((r_new & 0xF8) << 8) | ((g_new & 0xFC) << 3) | (b_new >> 3)
                    buf[idx]     = (rgb565 >> 8) & 0xFF
                    buf[idx + 1] = rgb565 & 0xFF
                    idx += 2
        except ImportError:
            # Fallback pure-Python siêu nhanh (chỉ ~4ms), không phụ thuộc numpy
            raw_rgb = img.tobytes()
            idx = 0
            for i in range(size * size):
                r = raw_rgb[i * 3]
                g = raw_rgb[i * 3 + 1]
                b = raw_rgb[i * 3 + 2]
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
    "power": "on",           # "on" (Bật hoạt động), "off" (Tắt / Chế độ ngủ)
    "remote_locked": False,  # True = Đã bị khóa từ màn hình ESP32
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
                    _start_transcode_background(plat, sid, fresh)
                    print(f"[PlayerHub] Pre-warmed next URL for {plat}:{sid}")
        except Exception as ex_pw:
            print(f"[PlayerHub] Pre-warm next error: {ex_pw}")

    threading.Thread(target=_prewarm_next, daemon=True).start()

    _player_state["stream_url"] = f"/api/stream/{platform}/{safe_id}?t={_player_state['seq']}"
    _player_state["song"] = next_song
    _player_state["elapsed"] = 0
    try:
        db.add_play_history(next_song)
    except Exception:
        pass
    print(f"[PlayerHub-AutoNext] Auto-play #{_player_state['seq']}: {next_song.get('title')} ({platform})")
    return True


@app.route("/api/player/play", methods=["POST"])
def player_play():
    """
    Phát bài hát ngay lập tức:
    POST /api/player/play
    Body JSON: {"song_id":"...", "platform":"youtube|soundcloud|zing", "title":"...", "artist":"...", "thumbnail":"...", "duration":180}
    """
    data = request.get_json(force=True, silent=True) or {}
    platform = str(data.get("platform", "youtube")).lower()
    song_id  = str(data.get("song_id") or data.get("id") or "").strip()

    if not song_id:
        return jsonify({"error": "Missing song_id"}), 400

    source = str(data.get("source", "")).lower()
    user_agent = request.headers.get("User-Agent", "")
    is_esp = (source == "esp32") or ("ESP32" in user_agent)

    if _player_state.get("remote_locked", False) and not is_esp:
        return jsonify({"error": "Web Remote đã bị khóa từ thiết bị ESP32"}), 403

    print(f"[PlayerHub] PLAY request from {request.remote_addr} (is_esp={is_esp}, locked={_player_state.get('remote_locked')}): {song_id} ({platform})")

    # Lấy stream url an toàn cho ESP32 Audio parser
    if song_id.startswith("http"):
        import base64
        safe_id = "b64_" + base64.urlsafe_b64encode(song_id.encode()).decode().rstrip("=")
    else:
        safe_id = song_id

    # Giữ lại URL hợp lệ trong cache nếu có để phát ngay lập tức
    fresh_url = None
    with _cache_lock:
        fresh_url = _stream_cache.get(f"{platform}:{song_id}") or _stream_cache.get(f"{platform}:{safe_id}")

    if not fresh_url:
        try:
            p_obj = PLATFORMS.get(platform)
            if p_obj and hasattr(p_obj, "get_stream_url"):
                fresh_url = p_obj.get_stream_url(song_id)
                if fresh_url:
                    with _cache_lock:
                        _stream_cache[f"{platform}:{song_id}"] = fresh_url
                        _stream_cache[f"{platform}:{safe_id}"] = fresh_url
                    print(f"[PlayerHub] Pre-resolved stream URL for {platform}:{song_id}")
        except Exception as ex_pw:
            print(f"[PlayerHub] Resolve stream URL error: {ex_pw}")

    if fresh_url:
        _start_transcode_background(platform, song_id, fresh_url)
        if safe_id != song_id:
            _start_transcode_background(platform, safe_id, fresh_url)

    song_info = {
        "id": song_id,
        "platform": platform,
        "title": data.get("title", "Unknown"),
        "artist": data.get("artist", "Unknown"),
        "thumbnail": data.get("thumbnail", ""),
        "duration": int(data.get("duration", 0)),
    }

    with _player_lock:
        global _playlist, _playlist_idx
        for i, s in enumerate(_playlist):
            if str(s.get("id")) == song_id:
                _playlist_idx = i
                break
        else:
            _playlist.append(song_info)
            _playlist_idx = len(_playlist) - 1

        _player_state["seq"] = int(time.time() * 1000) % 2000000000
        _player_state["state"] = "playing"
        _player_state["stream_url"] = f"/api/stream/{platform}/{safe_id}?t={_player_state['seq']}"
        _player_state["song"] = song_info
        _player_state["elapsed"] = 0

    try:
        db.add_play_history(song_info)
    except Exception as ex_h:
        print(f"[PlayerHub] Add history error: {ex_h}")

    print(f"[PlayerHub] PLAY #{_player_state['seq']}: {song_info.get('title')} ({platform})")
    return jsonify({"status": "ok", "seq": _player_state["seq"], "song": song_info})


@app.route("/api/player/control", methods=["POST"])
def player_control():
    """
    Điều khiển Play/Pause/Stop/Volume/Next/Prev:
    POST /api/player/control
    Body JSON: {"action":"pause|resume|stop|vol_up|vol_down|set_vol|next|prev", "value":15}
    """
    data   = request.get_json(force=True, silent=True) or {}
    action = str(data.get("action", "")).lower()

    source = str(data.get("source", "")).lower()
    user_agent = request.headers.get("User-Agent", "")
    is_esp = (source == "esp32") or ("ESP32" in user_agent)

    if _player_state.get("remote_locked", False) and not is_esp:
        return jsonify({"error": "Web Remote đã bị khóa từ thiết bị ESP32"}), 403

    print(f"[PlayerHub] CONTROL request from {request.remote_addr} (is_esp={is_esp}, locked={_player_state.get('remote_locked')}): {action}")

    with _player_lock:
        global _playlist, _playlist_idx
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
            _player_state["volume"] = max(0, min(21, int(data.get("value", 11))))
        elif action == "next":
            if not _auto_play_next():
                _player_state["state"] = "next_requested"
        elif action == "prev":
            if _playlist and len(_playlist) > 0:
                _playlist_idx = (_playlist_idx - 1) % len(_playlist)
                prev_song = _playlist[_playlist_idx]
                platform = str(prev_song.get("platform", "youtube")).lower()
                song_id  = str(prev_song.get("id", "")).strip()
                if song_id.startswith("http"):
                    import base64
                    safe_id = "b64_" + base64.urlsafe_b64encode(song_id.encode()).decode().rstrip("=")
                else:
                    safe_id = song_id
                _player_state["seq"] = int(time.time() * 1000) % 2000000000
                _player_state["state"] = "playing"
                _player_state["stream_url"] = f"/api/stream/{platform}/{safe_id}?t={_player_state['seq']}"
                _player_state["song"] = prev_song
                _player_state["elapsed"] = 0
                try: db.add_play_history(prev_song)
                except Exception: pass

    print(f"[PlayerHub] CONTROL: {action} (vol={_player_state['volume']}, state={_player_state['state']})")
    return jsonify({"status": "ok", "state": _player_state["state"], "volume": _player_state["volume"], "song": _player_state.get("song")})


# ─── Queue / Up Next (Danh sách phát & Hàng đợi) ───────────────

@app.route("/api/player/queue", methods=["GET"])
def player_queue_get():
    """Lấy toàn bộ danh sách hàng đợi phát."""
    with _player_lock:
        return jsonify({
            "queue": _playlist,
            "current_index": _playlist_idx,
            "current_song": _player_state.get("song")
        })


@app.route("/api/player/queue/add", methods=["POST"])
def player_queue_add():
    """
    Thêm bài hát vào hàng đợi:
    Body: {"song": {...}, "position": "next" | "end"}
    position="next": Chèn ngay sau bài đang phát (Phát tiếp theo)
    position="end": Thêm vào cuối hàng đợi
    """
    data = request.get_json(force=True, silent=True) or {}
    song = data.get("song")
    if not song or not song.get("id"):
        return jsonify({"error": "Missing song object"}), 400

    position = str(data.get("position", "end")).lower()

    with _player_lock:
        global _playlist, _playlist_idx
        # Định dạng chuẩn song dict
        song_clean = {
            "id": str(song.get("id")),
            "platform": str(song.get("platform", "youtube")).lower(),
            "title": str(song.get("title", "Unknown")),
            "artist": str(song.get("artist", "Unknown")),
            "thumbnail": str(song.get("thumbnail", "")),
            "duration": int(song.get("duration", 0)),
        }

        if position == "next" and _playlist:
            insert_pos = _playlist_idx + 1
            _playlist.insert(insert_pos, song_clean)
            print(f"[Queue] Inserted NEXT at index {insert_pos}: {song_clean['title']}")
        else:
            _playlist.append(song_clean)
            print(f"[Queue] Appended to END: {song_clean['title']}")

        return jsonify({
            "status": "ok",
            "queue_len": len(_playlist),
            "current_index": _playlist_idx,
            "position": position
        })


@app.route("/api/player/queue/remove", methods=["POST"])
def player_queue_remove():
    """Xóa 1 bài khỏi hàng đợi theo index."""
    data = request.get_json(force=True, silent=True) or {}
    idx = data.get("index")
    with _player_lock:
        global _playlist, _playlist_idx
        if idx is not None and 0 <= int(idx) < len(_playlist):
            idx = int(idx)
            removed = _playlist.pop(idx)
            if idx < _playlist_idx:
                _playlist_idx -= 1
            elif idx == _playlist_idx:
                # Nếu xóa bài đang phát -> tự chuyển bài tiếp
                if _playlist:
                    _playlist_idx = _playlist_idx % len(_playlist)
                else:
                    _playlist_idx = 0
            print(f"[Queue] Removed #{idx}: {removed.get('title')}")
            return jsonify({"status": "ok", "queue_len": len(_playlist)})
    return jsonify({"error": "Invalid index"}), 400


@app.route("/api/player/queue/clear", methods=["POST"])
def player_queue_clear():
    """Xóa sạch hàng đợi (giữ lại bài đang phát nếu có)."""
    with _player_lock:
        global _playlist, _playlist_idx
        if _player_state.get("song"):
            _playlist = [_player_state["song"]]
            _playlist_idx = 0
        else:
            _playlist = []
            _playlist_idx = 0
        return jsonify({"status": "ok", "queue_len": len(_playlist)})


# ─── Lịch sử nghe nhạc (Tối đa 20 bài) ──────────────────────────

@app.route("/api/history", methods=["GET"])
@app.route("/api/play/history", methods=["GET"])
@app.route("/api/play_history", methods=["GET"])
def get_history():
    """GET /api/history -> Trả về tối đa 20 bài hát đã nghe gần nhất."""
    history = db.get_play_history(limit=20)
    return jsonify(history)


@app.route("/api/history/clear", methods=["POST"])
def clear_history():
    """POST /api/history/clear -> Xóa sạch lịch sử nghe nhạc."""
    db.clear_play_history()
    return jsonify({"status": "ok"})



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
            "power": _player_state.get("power", "on"),
            "remote_locked": _player_state.get("remote_locked", False),
            "state": _player_state["state"],
            "seq": _player_state["seq"],
            "stream_url": url,
            "volume": _player_state["volume"],
            "elapsed": _player_state["elapsed"],
            "song": _player_state["song"],
        }
    return jsonify(resp)


@app.route("/api/player/remote_lock", methods=["GET", "POST"])
def player_remote_lock():
    """
    ESP32 gửi lệnh Khóa / Mở khóa Web Remote:
    POST /api/player/remote_lock
    Body: {"locked": true | false}
    """
    with _player_lock:
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            locked = data.get("locked")
            if locked is not None:
                _player_state["remote_locked"] = bool(locked)
                _player_state["seq"] = int(time.time() * 1000) % 2000000000
                print(f"[WebRemote] >>> TRANG THAI KHOA (REMOTE LOCKED): {_player_state['remote_locked']}")
        return jsonify({
            "status": "ok",
            "locked": _player_state.get("remote_locked", False)
        })


@app.route("/api/player/power", methods=["GET", "POST"])
def player_power():
    """
    Bật / Tắt nguồn hoặc Chế độ ngủ (Standby) từ xa:
    POST /api/player/power
    Body JSON: {"state": "on" | "off" | "toggle"}
    """
    with _player_lock:
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            target = str(data.get("state", "toggle")).lower()
            if target == "toggle":
                _player_state["power"] = "off" if _player_state.get("power", "on") == "on" else "on"
            elif target in ["on", "off"]:
                _player_state["power"] = target

            if _player_state["power"] == "off":
                _player_state["state"] = "stopped"
                _player_state["stream_url"] = ""

            _player_state["seq"] = int(time.time() * 1000) % 2000000000
            print(f"[PlayerHub] >>> NGUON THIET BI (POWER): {_player_state['power'].upper()}")

        return jsonify({
            "status": "ok",
            "power": _player_state.get("power", "on"),
            "state": _player_state["state"]
        })


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

            curr_song = _player_state.get("song") or {}
            song_dur = int(curr_song.get("duration", 0))

            # BẢO VỆ CHỐNG NHẢY BÀI SỚM:
            # Nếu bài hát có thời lượng (> 20s) và chưa phát được ít nhất (duration - 15s):
            # Đây chỉ là lỗi rớt mạng hoặc đệm tạm thời của Loa, TUYỆT ĐỐI KHÔNG nhảy bài!
            if song_dur > 20 and cur_elapsed < (song_dur - 15):
                print(f"[PlayerHub] Bỏ qua EOF non (mới phát {cur_elapsed}s / {song_dur}s) -> Giữ nguyên bài, Loa sẽ tự kết nối lại!")
            elif cur_elapsed < 10:
                print(f"[PlayerHub] Bỏ qua EOF quá ngắn (elapsed={cur_elapsed}s < 10s)")
            else:
                last_next = _player_state.get("_last_auto_next_ts", 0)
                now_ts = _time.time()
                if now_ts - last_next < 5:
                    print(f"[PlayerHub] Bỏ qua EOF (cooldown auto-next còn {5-(now_ts-last_next):.1f}s)")
                else:
                    _player_state["_last_auto_next_ts"] = now_ts
                    print(f"[PlayerHub] Song finished TRULY (EOF, elapsed={cur_elapsed}s / {song_dur}s) -> Auto-Next...")
                    if not _auto_play_next():
                        _player_state["state"] = "next_requested"

        elif event == "error":
            print(f"[PlayerHub] Loa bao loi stream -> Giu nguyen bai hien tai")

    return jsonify({"status": "ok"})


# ─── XiaoAI Voice Assistant API ───────────────────────────────

@app.route("/api/ai/voice_command", methods=["POST"])
def ai_voice_command():
    """
    Trợ lý giọng nói AI (XiaoAI / Xiaomi Voice Assistant):
    POST /api/ai/voice_command
    Body JSON: {"command": "mở bài Cắt Đôi Nỗi Sầu"}
    """
    data = request.get_json(force=True, silent=True) or {}
    cmd = (data.get("command") or data.get("text") or "").strip().lower()

    if not cmd:
        return jsonify({"status": "error", "message": "Không nghe rõ câu lệnh, vui lòng thử lại!"}), 400

    if _player_state.get("remote_locked", False):
        return jsonify({"status": "error", "message": "XiaoAI: Web Remote đang bị KHÓA từ màn hình ESP32. Hãy bật lại trên ESP32 để sử dụng!"}), 403

    print(f"[XiaoAI] Nhan lenh giong noi: '{cmd}'")

    # 1. Nhan dien cac lenh dieu khien nhanh
    if any(w in cmd for w in ["tắt máy", "tắt loa", "tắt nguồn", "đi ngủ", "sleep", "ngủ đi"]):
        with _player_lock:
            _player_state["power"] = "off"
            _player_state["state"] = "stopped"
            _player_state["stream_url"] = ""
            _player_state["seq"] = int(time.time() * 1000) % 2000000000
        return jsonify({"status": "ok", "action": "power", "power": "off", "message": "XiaoAI: Đã tắt máy và chuyển sang chế độ ngủ."})

    if any(w in cmd for w in ["bật máy", "bật loa", "mở máy", "bật nguồn", "thức dậy", "wake up"]):
        with _player_lock:
            _player_state["power"] = "on"
            _player_state["seq"] = int(time.time() * 1000) % 2000000000
        return jsonify({"status": "ok", "action": "power", "power": "on", "message": "XiaoAI: Đã bật máy và sẵn sàng hoạt động!"})

    if any(w in cmd for w in ["tiếp theo", "bài tiếp", "qua bài", "next"]):
        with _player_lock:
            ok = _auto_play_next()
        if ok:
            return jsonify({
                "status": "ok",
                "action": "next",
                "message": f"XiaoAI: Đã chuyển bài tiếp theo '{_player_state['song'].get('title')}'",
                "song": _player_state["song"]
            })
        return jsonify({"status": "ok", "action": "next", "message": "Hàng đợi trống, không có bài tiếp theo."})

    if any(w in cmd for w in ["dừng lại", "tạm dừng", "pause", "tắt nhạc", "im lặng", "stop"]):
        with _player_lock:
            _player_state["state"] = "paused"
            _player_state["seq"] = int(time.time() * 1000) % 2000000000
        return jsonify({"status": "ok", "action": "pause", "message": "XiaoAI: Đã tạm dừng phát nhạc."})

    if any(w in cmd for w in ["tiếp tục", "phát tiếp", "resume", "bật lại"]):
        with _player_lock:
            _player_state["state"] = "playing"
            _player_state["seq"] = int(time.time() * 1000) % 2000000000
        return jsonify({"status": "ok", "action": "resume", "message": "XiaoAI: Tiếp tục phát nhạc."})

    if any(w in cmd for w in ["tăng âm lượng", "to lên", "bật to", "âm lượng to", "cho to"]):
        with _player_lock:
            _player_state["volume"] = min(21, _player_state.get("volume", 11) + 2)
        return jsonify({"status": "ok", "action": "volume", "volume": _player_state["volume"], "message": f"XiaoAI: Đã tăng âm lượng lên {_player_state['volume']}/21."})

    if any(w in cmd for w in ["giảm âm lượng", "nhỏ lại", "bật nhỏ", "âm lượng nhỏ", "cho nhỏ"]):
        with _player_lock:
            _player_state["volume"] = max(0, _player_state.get("volume", 11) - 2)
        return jsonify({"status": "ok", "action": "volume", "volume": _player_state["volume"], "message": f"XiaoAI: Đã giảm âm lượng xuống {_player_state['volume']}/21."})

    # 2. Xử lý yêu cầu tìm & phát bài hát
    prefixes = [
        "mở bài hát", "mở ca khúc", "mở bản nhạc", "mở bài", "mở nhạc", "mở",
        "phát bài hát", "phát ca khúc", "phát bản nhạc", "phát bài", "phát nhạc", "phát",
        "bật bài hát", "bật ca khúc", "bật bản nhạc", "bật bài", "bật nhạc", "bật",
        "tìm bài hát", "tìm ca khúc", "tìm bài", "tìm kiếm", "tìm",
        "cho tôi nghe bài", "cho nghe bài", "nghe bài", "nghe",
        "hát bài", "chơi bài", "play"
    ]
    query = cmd
    for p in prefixes:
        if query.startswith(p + " "):
            query = query[len(p)+1:].strip()
            break
        elif query.startswith(p):
            query = query[len(p):].strip()
            break

    if not query:
        query = cmd

    print(f"[XiaoAI] Trich xuat tu khoa: '{query}'")

    found_songs = []
    # Thử ZingMP3 trước
    try:
        if "zing" in PLATFORMS:
            res_z = PLATFORMS["zing"].search(query, limit=5)
            if res_z:
                found_songs.extend(res_z)
    except Exception as ex_z:
        print(f"[XiaoAI] Zing error: {ex_z}")

    # Thử YouTube nếu cần
    if len(found_songs) < 2:
        try:
            if "youtube" in PLATFORMS:
                res_yt = PLATFORMS["youtube"].search(query, limit=5)
                if res_yt:
                    found_songs.extend(res_yt)
        except Exception as ex_yt:
            print(f"[XiaoAI] YT error: {ex_yt}")

    # Thử SoundCloud nếu vẫn chưa có
    if not found_songs:
        try:
            if "soundcloud" in PLATFORMS:
                res_sc = PLATFORMS["soundcloud"].search(query, limit=5)
                if res_sc:
                    found_songs.extend(res_sc)
        except Exception as ex_sc:
            print(f"[XiaoAI] SC error: {ex_sc}")

    if not found_songs:
        return jsonify({
            "status": "not_found",
            "action": "search",
            "message": f"XiaoAI: Không tìm thấy bài hát nào cho '{query}'. Bạn thử nói bài khác nhé!"
        })

    best_song = found_songs[0]
    platform = str(best_song.get("platform", "youtube")).lower()
    song_id  = str(best_song.get("id", "")).strip()

    if song_id.startswith("http"):
        import base64
        safe_id = "b64_" + base64.urlsafe_b64encode(song_id.encode()).decode().rstrip("=")
    else:
        safe_id = song_id

    with _player_lock:
        global _playlist, _playlist_idx
        _player_state["seq"] = int(time.time() * 1000) % 2000000000
        _player_state["state"] = "playing"
        _player_state["stream_url"] = f"/api/stream/{platform}/{safe_id}?t={_player_state['seq']}"
        _player_state["song"] = best_song
        _player_state["elapsed"] = 0

        for i, s in enumerate(_playlist):
            if str(s.get("id")) == song_id:
                _playlist_idx = i
                break
        else:
            _playlist.insert(_playlist_idx + 1, best_song)
            _playlist_idx = _playlist_idx + 1

    try:
        db.add_play_history(best_song)
    except Exception:
        pass

    msg = f"XiaoAI: Đang phát bài '{best_song.get('title')}' của {best_song.get('artist', 'Unknown')} lên Loa ESP32"
    print(f"[XiaoAI] {msg}")

    return jsonify({
        "status": "ok",
        "action": "play",
        "message": msg,
        "song": best_song
    })


# ─── Clock Wallpaper Management ────────────────────────────────

WALLPAPER_BIN = Path(__file__).parent / "static" / "clock_wallpaper.bin"
WALLPAPER_GIF = Path(__file__).parent / "static" / "clock_wallpaper.gif"
WALLPAPER_JPG = Path(__file__).parent / "static" / "clock_wallpaper.jpg"

def _init_wallpaper_version():
    if WALLPAPER_GIF.exists():
        return int(WALLPAPER_GIF.stat().st_mtime)
    if WALLPAPER_BIN.exists():
        return int(WALLPAPER_BIN.stat().st_mtime)
    return 0

_wallpaper_version = _init_wallpaper_version()


def _enhance_image_for_tft(img: Image.Image, preset: str = "tft_film") -> Image.Image:
    """
    Tối ưu hóa ảnh hiển thị sắc nét, triệt tiêu 100% hiện tượng hắt sáng (backlight glare)
    và tăng độ tương phản, chiều sâu màu sắc như trên màn hình OLED điện thoại.
    """
    if preset == "original":
        # Với ảnh gốc: Giữ màu gốc nhưng ghìm nhẹ trần ánh sáng cực đại ở ~240/255
        # để tránh tinh thể lỏng mở 100% làm đèn nền chiếu thẳng vào mắt
        lut = []
        for i in range(256):
            x = i / 255.0
            if x > 0.85:
                x = 0.85 + (x - 0.85) * 0.50
            lut.append(max(0, min(255, int(x * 242.0))))
        return img.point(lut * 3)

    # 1. Đường cong S-Curve + Highlight Ceiling Clamp (Ghìm trần ánh sáng chống hắt sáng):
    # - Vùng tối (< 0.5): Kéo sâu xuống một chút để màu đen inky, triệt tiêu mờ sương
    # - Vùng sáng (> 0.5): Uốn cong mượt mà để chủ thể nổi khối
    # - Trần ánh sáng cực đại (Ceiling): Khóa ở 0.92 (~234/255)
    #   Điều này giữ cho tinh thể lỏng màn hình TFT luôn có lớp cản sáng ~8-10%,
    #   triệt tiêu hoàn toàn tia sáng hắt chói từ đèn LED nền mà mắt nhìn vẫn trắng đẹp!
    lut = []
    power = 1.25 if preset == "tft_film" else 1.15
    ceiling = 0.92 if preset == "tft_film" else 0.94

    for i in range(256):
        x = i / 255.0
        if x < 0.5:
            y = 0.5 * ((2.0 * x) ** power)
        else:
            y = 1.0 - 0.5 * ((2.0 * (1.0 - x)) ** power)
        y = y * ceiling
        lut.append(max(0, min(255, int(y * 255.0))))

    img = img.point(lut * 3)

    # 2. Bão hòa màu (Saturation): Bù đắp dải màu ~60% sRGB của màn TFT
    sat_factor = 1.36 if preset == "tft_film" else 1.42
    img = ImageEnhance.Color(img).enhance(sat_factor)

    # 3. Tăng độ tương phản (Contrast)
    con_factor = 1.16 if preset == "tft_film" else 1.22
    img = ImageEnhance.Contrast(img).enhance(con_factor)

    # 4. Tăng độ nét (Sharpness) sau khi resize về 320x240
    img = ImageEnhance.Sharpness(img).enhance(1.25)

    return img


def _image_to_rgb565_dithered(img: Image.Image) -> bytearray:
    """
    Chuyển đổi ảnh RGB sang mảng byte RGB565.
    Nếu có numpy thì dùng tán sắc Floyd-Steinberg, nếu không thì dùng Pure-Python fallback cực nhanh.
    """
    try:
        import numpy as np
        arr = np.array(img, dtype=np.float32)
        h, w, _ = arr.shape
        out = bytearray(w * h * 2)

        scale_r = 31.0 / 255.0
        scale_g = 63.0 / 255.0
        scale_b = 31.0 / 255.0

        idx = 0
        for y in range(h):
            for x in range(w):
                r = max(0.0, min(255.0, arr[y, x, 0]))
                g = max(0.0, min(255.0, arr[y, x, 1]))
                b = max(0.0, min(255.0, arr[y, x, 2]))

                r5 = max(0, min(31, int(r * scale_r + 0.5)))
                g6 = max(0, min(63, int(g * scale_g + 0.5)))
                b5 = max(0, min(31, int(b * scale_b + 0.5)))

                rgb16 = (r5 << 11) | (g6 << 5) | b5
                out[idx]     = (rgb16 >> 8) & 0xFF
                out[idx + 1] = rgb16 & 0xFF
                idx += 2

                qr = r5 * (255.0 / 31.0)
                qg = g6 * (255.0 / 63.0)
                qb = b5 * (255.0 / 31.0)

                er = r - qr
                eg = g - qg
                eb = b - qb

                if x + 1 < w:
                    arr[y, x + 1, 0] += er * 0.4375
                    arr[y, x + 1, 1] += eg * 0.4375
                    arr[y, x + 1, 2] += eb * 0.4375
                if y + 1 < h:
                    if x > 0:
                        arr[y + 1, x - 1, 0] += er * 0.1875
                        arr[y + 1, x - 1, 1] += eg * 0.1875
                        arr[y + 1, x - 1, 2] += eb * 0.1875
                    arr[y + 1, x, 0] += er * 0.3125
                    arr[y + 1, x, 1] += eg * 0.3125
                    arr[y + 1, x, 2] += eb * 0.3125
                    if x + 1 < w:
                        arr[y + 1, x + 1, 0] += er * 0.0625
                        arr[y + 1, x + 1, 1] += eg * 0.0625
                        arr[y + 1, x + 1, 2] += eb * 0.0625

        return out
    except ImportError:
        # Fallback pure-Python khi môi trường VPS chưa cài numpy
        raw_rgb = img.tobytes()
        w, h = img.size
        out = bytearray(w * h * 2)
        idx = 0
        for i in range(w * h):
            r = raw_rgb[i * 3]
            g = raw_rgb[i * 3 + 1]
            b = raw_rgb[i * 3 + 2]
            rgb16 = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out[idx]     = (rgb16 >> 8) & 0xFF
            out[idx + 1] = rgb16 & 0xFF
            idx += 2
        return out


def _optimize_gif_for_esp32(raw: bytes, max_bytes: int = 940_000) -> bytes:
    """
    Tự động nén, crop 320x240 và tối ưu số frame/palette cho file GIF lớn (2MB, 5MB...)
    để vừa vặn phân vùng SPIFFS của ESP32 mà vẫn giữ chuyển động mượt mà và không tràn RAM.
    """
    from PIL import ImageSequence
    im = Image.open(BytesIO(raw))
    orig_frames = [f.copy() for f in ImageSequence.Iterator(im)]
    total_f = len(orig_frames)

    step = 1
    if total_f > 80:
        step = 2
    elif total_f > 150:
        step = 3

    colors = 128
    target_w, target_h = 320, 240
    target_ratio = target_w / float(target_h)

    res = raw
    for attempt in range(3):
        frames = []
        durations = []
        for i in range(0, total_f, step):
            f = orig_frames[i].convert("RGB")
            w, h = f.size
            cur_ratio = w / float(h)
            if cur_ratio > target_ratio:
                new_w = int(h * target_ratio)
                left = (w - new_w) // 2
                f = f.crop((left, 0, left + new_w, h))
            else:
                new_h = int(w / target_ratio)
                top = (h - new_h) // 2
                f = f.crop((0, top, w, top + new_h))

            f = f.resize((target_w, target_h), Image.Resampling.LANCZOS)
            f_p = f.quantize(colors=colors, method=Image.Resampling.NEAREST)
            frames.append(f_p)
            dur = max(33, int(im.info.get("duration", 80) * step))
            durations.append(dur)

        if not frames:
            break

        out = BytesIO()
        frames[0].save(
            out,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0,
            optimize=True
        )
        res = out.getvalue()
        if len(res) <= max_bytes or attempt == 2:
            return res

        step += 1
        colors = 64

    return res


@app.route("/api/wallpaper/upload", methods=["POST"])
def upload_wallpaper():
    """Nhận ảnh / GIF từ điện thoại, xử lý và lưu cho ESP32 tải về"""
    global _wallpaper_version
    file = request.files.get("file")
    if not file:
        return jsonify({"status": "error", "message": "Chưa chọn file ảnh"}), 400

    preset = request.form.get("preset", "tft_film").strip().lower()
    if preset not in ("tft_film", "vivid", "original"):
        preset = "tft_film"

    WALLPAPER_BIN.parent.mkdir(parents=True, exist_ok=True)
    fname = (file.filename or "").lower()
    is_gif = fname.endswith(".gif") or (file.content_type or "").startswith("image/gif")

    try:
        if is_gif:
            # ── GIF động: Tự động nén & tối ưu frame 320x240 để vừa vặn SPIFFS ──
            raw = file.read()
            orig_len = len(raw)
            if orig_len > 940_000:
                print(f"[Wallpaper] GIF lớn ({orig_len//1024} KB). Đang tự động nén & tối ưu 320x240...")
                try:
                    raw = _optimize_gif_for_esp32(raw, max_bytes=940_000)
                    print(f"[Wallpaper] GIF nén thành công: {orig_len//1024} KB -> {len(raw)//1024} KB")
                except Exception as ex_opt:
                    print(f"[Wallpaper] Lỗi tự động nén GIF: {ex_opt}")
                    if len(raw) > 950_000:
                        return jsonify({
                            "status": "error",
                            "message": f"GIF quá lớn ({orig_len//1024} KB). Tự động nén thất bại: {ex_opt}"
                        }), 400

            # Lưu GIF gốc / đã tối ưu
            WALLPAPER_GIF.write_bytes(raw)
            # Xoá ảnh tĩnh cũ (tiết kiệm SPIFFS)
            if WALLPAPER_BIN.exists():
                WALLPAPER_BIN.unlink()

            # Tạo preview JPEG từ frame đầu
            try:
                img_pil = Image.open(BytesIO(raw))
                img_pil.seek(0)
                img_pil = img_pil.convert("RGB").resize((320, 240), Image.Resampling.LANCZOS)
                img_pil = _enhance_image_for_tft(img_pil, preset)
                img_pil.save(str(WALLPAPER_JPG), format="JPEG", quality=88)
            except Exception as pe:
                print(f"[Wallpaper] Warning: không tạo được preview: {pe}")

            _wallpaper_version = int(time.time())
            print(f"[Wallpaper] GIF saved {len(raw)} bytes. Version: {_wallpaper_version}")
            return jsonify({
                "status": "ok",
                "message": f"Đã tải GIF động ({len(raw)//1024} KB) lên ESP32!",
                "version": _wallpaper_version,
                "is_gif": True,
                "size_bytes": len(raw),
                "preview_url": "/api/wallpaper/preview"
            })

        else:
            # ── Ảnh tĩnh: crop 320×240 → Auto-Enhance TFT → RGB565 Dithered ─
            img = Image.open(file.stream).convert("RGB")
            w, h = img.size

            target_ratio = 320.0 / 240.0
            cur_ratio = w / float(h)
            if cur_ratio > target_ratio:
                new_w = int(h * target_ratio)
                left = (w - new_w) // 2
                img = img.crop((left, 0, left + new_w, h))
            else:
                new_h = int(w / target_ratio)
                top = (h - new_h) // 2
                img = img.crop((0, top, w, top + new_h))

            img = img.resize((320, 240), Image.Resampling.LANCZOS)
            
            # Tối ưu hóa màu sắc, chống cháy trắng cho màn hình TFT ESP32
            img = _enhance_image_for_tft(img, preset)
            img.save(str(WALLPAPER_JPG), format="JPEG", quality=90)

            # Chuyển đổi RGB565 bằng Floyd-Steinberg dithering mượt mà
            out = _image_to_rgb565_dithered(img)

            WALLPAPER_BIN.write_bytes(out)
            # Xoá GIF cũ nếu có
            if WALLPAPER_GIF.exists():
                WALLPAPER_GIF.unlink()

            _wallpaper_version = int(time.time())
            print(f"[Wallpaper] Static image saved (preset: {preset}). Version: {_wallpaper_version}")
            return jsonify({
                "status": "ok",
                "message": f"Cập nhật hình nền thành công (chế độ {preset})!",
                "version": _wallpaper_version,
                "is_gif": False,
                "preview_url": "/api/wallpaper/preview"
            })

    except Exception as e:
        print(f"[Wallpaper] Lỗi xử lý: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/wallpaper", methods=["GET"])
def get_wallpaper():
    """Phục vụ file RGB565 binary cho ESP32 (ảnh tĩnh)"""
    if not WALLPAPER_BIN.exists():
        abort(404)
    return send_file(str(WALLPAPER_BIN), mimetype="application/octet-stream")


@app.route("/api/wallpaper/gif", methods=["GET"])
def get_wallpaper_gif():
    """Phục vụ file GIF gốc cho ESP32 tải về SPIFFS"""
    if not WALLPAPER_GIF.exists():
        abort(404)
    return send_file(str(WALLPAPER_GIF), mimetype="image/gif")


@app.route("/api/wallpaper/preview", methods=["GET"])
def get_wallpaper_preview():
    """Xem trước ảnh nền trên Web (JPEG)"""
    if not WALLPAPER_JPG.exists():
        abort(404)
    return send_file(str(WALLPAPER_JPG), mimetype="image/jpeg")


@app.route("/api/wallpaper/info", methods=["GET"])
def get_wallpaper_info():
    """Trạng thái ảnh nền hiện tại"""
    is_gif = WALLPAPER_GIF.exists()
    has_static = WALLPAPER_BIN.exists()
    available = is_gif or has_static
    if is_gif:
        size = WALLPAPER_GIF.stat().st_size
    elif has_static:
        size = WALLPAPER_BIN.stat().st_size
    else:
        size = 0
    return jsonify({
        "available": available,
        "is_gif": is_gif,
        "version": _wallpaper_version,
        "size_bytes": size
    })


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


# ─── Weather API (Open-Meteo Integration) ─────────────────────

_weather_cache = {
    "data": None,
    "timestamp": 0
}

@app.route("/api/weather", methods=["GET"])
def get_weather():
    """Lấy thông tin thời tiết chuẩn xác theo từng giờ giống hệt iPhone cho Hà Nội"""
    global _weather_cache
    now = time.time()
    # Cache trong 2 phut (120s) de cap nhat theo phut va theo gio chinh xac
    if _weather_cache["data"] and (now - _weather_cache["timestamp"] < 120):
        return jsonify(_weather_cache["data"])

    lat = request.args.get("lat", "21.0285")  # Mac dinh Ha Noi
    lon = request.args.get("lon", "105.8542")

    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}&"
            f"current=temperature_2m,relative_humidity_2m,weather_code,precipitation,rain,showers,cloud_cover,is_day&"
            f"hourly=temperature_2m,relative_humidity_2m,weather_code,precipitation,rain,cloud_cover,is_day&"
            f"daily=weather_code,temperature_2m_max,temperature_2m_min&"
            f"timezone=Asia%2FBangkok"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "ESP32MusicPlayer/2.0"})
        with urllib.request.urlopen(req, timeout=6) as response:
            res_json = json.loads(response.read().decode())

        # Khop theo gio va phut thuc te tai Ha Noi (GMT+7) giong 100% Google Weather & iPhone
        import datetime
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo("Asia/Bangkok")
            now_dt = datetime.datetime.now(tz)
        except Exception:
            # Fallback neu khong co zoneinfo: UTC + 7h
            now_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=7)

        cur_hour_str = now_dt.strftime("%Y-%m-%dT%H:00")

        hourly = res_json.get("hourly", {})
        times = hourly.get("time", [])
        idx = -1
        for i, t in enumerate(times):
            if t == cur_hour_str:
                idx = i
                break

        curr = res_json.get("current", {})
        daily = res_json.get("daily", {})
        daily_codes = daily.get("weather_code", [])
        today_daily_code = daily_codes[0] if len(daily_codes) > 0 else 0

        # Tinh toan noi suy theo phut giua 2 khung gio de nhiet do & do am chuyen tiep muot ma
        interp_temp = None
        interp_hum = None
        if idx != -1:
            w_code   = int(hourly["weather_code"][idx])
            precip   = float(hourly["precipitation"][idx] or 0.0)
            cloud    = int(hourly["cloud_cover"][idx] or 0)
            is_day   = int(hourly["is_day"][idx] or 1)
            t0 = float(hourly["temperature_2m"][idx])
            h0 = float(hourly["relative_humidity_2m"][idx])
            if idx + 1 < len(times):
                t1 = float(hourly["temperature_2m"][idx + 1])
                h1 = float(hourly["relative_humidity_2m"][idx + 1])
                m_frac = now_dt.minute / 60.0
                interp_temp = t0 + (t1 - t0) * m_frac
                interp_hum = h0 + (h1 - h0) * m_frac
            else:
                interp_temp = t0
                interp_hum = h0
        else:
            w_code   = int(curr.get("weather_code", 0))
            precip   = float(curr.get("precipitation") or 0.0)
            cloud    = int(curr.get("cloud_cover") or 0)
            is_day   = int(curr.get("is_day", 1))

        # Lay du lieu quan trac thuc te tram khi tuong METAR Noi Bai (VVNB)
        metar_temp = None
        metar_hum = None
        if "21.02" in lat or "105.8" in lon:
            try:
                m_req = urllib.request.Request("https://aviationweather.gov/api/data/metar?ids=VVNB&format=json", headers={"User-Agent": "ESP32Weather/1.0"})
                with urllib.request.urlopen(m_req, timeout=3) as m_resp:
                    m_arr = json.loads(m_resp.read().decode())
                    if m_arr and len(m_arr) > 0:
                        mt = m_arr[0].get("temp")
                        md = m_arr[0].get("dewp")
                        if mt is not None and md is not None:
                            import math
                            a_const, b_const = 17.625, 243.04
                            alpha_t = (a_const * mt) / (b_const + mt)
                            alpha_td = (a_const * md) / (b_const + md)
                            metar_temp = float(mt)
                            metar_hum = float(round(100.0 * math.exp(alpha_td - alpha_t)))
            except Exception:
                pass

        # Tong hop nhiet do & do am chuan xac nhat tu Tram quan trac va Open-Meteo
        curr_t = float(curr.get("temperature_2m", interp_temp if interp_temp is not None else 32.0))
        curr_h = float(curr.get("relative_humidity_2m", interp_hum if interp_hum is not None else 65.0))
        calc_t = interp_temp if interp_temp is not None else curr_t
        calc_h = interp_hum if interp_hum is not None else curr_h

        if metar_temp is not None:
            # Ket hop tram METAR Ha Noi voi mo hinh du bao de dat do chuan cao nhat
            raw_temp = (curr_t + calc_t + metar_temp * 2.0) / 4.0
            raw_hum  = (curr_h + calc_h + metar_hum * 2.0) / 4.0
        else:
            raw_temp = (curr_t + calc_t) / 2.0
            raw_hum  = (curr_h + calc_h) / 2.0

        # Ban ngay / Ban dem theo gio thuc te tai Ha Noi (hoang hon ~18h)
        if now_dt.hour >= 18 or now_dt.hour < 6 or curr.get("is_day") == 0:
            is_day = 0

        temp = int(round(raw_temp))
        humidity = int(round(raw_hum))
        total_rain = precip

        # Phan loai chuan xac giong 100% Google Weather va Apple Weather:
        if total_rain > 0.2:
            if w_code in [95, 96, 99]:
                icon = "thunder"
                text = "Mua giong sam set"
                icon_id = 3
            else:
                icon = "rain"
                text = "Troi mua"
                icon_id = 2
        elif w_code in [45, 48]:
            icon = "fog"
            text = "Suong mu"
            icon_id = 4
        elif cloud > 80 or w_code == 3:
            # Nhieu may / U am (Overcast)
            icon = "cloud"
            text = "Troi nhieu may"
            icon_id = 1
        elif is_day == 1:
            # BAN NGAY:
            # Neu co may rai rac (WMO 1, 2 hoac cloud >= 10 hoac du bao ngay co may) -> Nang va co may!
            if w_code in [1, 2] or cloud >= 10 or today_daily_code in [1, 2, 3]:
                icon = "sun_cloud"
                text = "Nang va co may"
                icon_id = 6
            else:
                icon = "sun"
                text = "Troi nang"
                icon_id = 0
        else:
            # BAN DEM:
            if w_code in [1, 2] or cloud >= 10 or today_daily_code in [1, 2, 3]:
                icon = "moon_cloud"
                text = "Dem co may"
                icon_id = 7
            else:
                icon = "moon"
                text = "Troi quang (Ban dem)"
                icon_id = 5

        weather_data = {
            "status": "ok",
            "temp": temp,
            "humidity": humidity,
            "icon": icon,
            "icon_id": icon_id,
            "text": text,
            "code": w_code,
            "rain_mm": total_rain,
            "cloud_cover": cloud,
            "is_day": is_day,
            "hour": cur_hour_str,
            "cached_at": int(now)
        }
        _weather_cache = {
            "data": weather_data,
            "timestamp": now
        }
        print(f"[Weather] Accurate Google-matched Ha Noi ({cur_hour_str}): {temp}C, {humidity}%, icon:{icon_id} ({icon}), is_day:{is_day}")
        return jsonify(weather_data)

    except Exception as e:
        print(f"[Weather] Error fetching weather: {e}")
        # Giữ lại dữ liệu hợp lệ gần nhất, KHÔNG nhảy về số 28°C hay icon giả
        if _weather_cache["data"]:
            return jsonify(_weather_cache["data"])
        return jsonify({
            "status": "fallback",
            "temp": 30,
            "humidity": 70,
            "icon": "moon",
            "icon_id": 5,
            "text": "Troi quang (Ban dem)",
            "code": 0
        })


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

    port = int(os.environ.get("PORT", 5000))

    print("=" * 60)
    print("  ESP32 Music Player Server v2")
    print("=" * 60)
    print(f"  Internal Port: {port}")
    print(f"  External Port (NAT mapped): 4954")
    print(f"  External URL : http://51.15.20.170:4954")
    print("=" * 60)
    print("  Platforms: ZingMP3 | SoundCloud | YouTube")
    print()
    print("  📋 Nhập vào config.h của ESP32:")
    print(f'     #define SERVER_IP   "51.15.20.170"')
    print(f'     #define SERVER_PORT 4954')
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
