"""
SoundCloud Platform — via yt-dlp
"""

import re
import time
import json
import urllib.request
import urllib.parse
import yt_dlp

import os
from pathlib import Path

_YDL_SEARCH_OPTS = {
    "quiet":          True,
    "no_warnings":    True,
    "extract_flat":   True,
    "playlist_items": "1:20",
    "socket_timeout": 8,
    "retries":        2,
}

_YDL_STREAM_OPTS = {
    "quiet":          True,
    "no_warnings":    True,
    "format":         "bestaudio[ext=mp3]/bestaudio[abr<=128]/bestaudio",
    "noplaylist":     True,
    "retries":        3,
    "socket_timeout": 10,
}

_CID_FILE = Path(__file__).parent.parent / "sc_client_id.txt"

_SC_CLIENT_IDS = [
    "39AVoiZVrdvJEMFLH8T0c5WGshQaAPRJ",
    "iZIs9mchVcX5lhVR1hnZWMM9nx44Huao",
    "b40391d4e08216c0fae3b7b6e9c4d2d4",
    "2t9loNfh0ekOfbnFeO3wEiHGMIVk302f",
]

# Đọc client_id đã lưu từ file nếu có
if _CID_FILE.exists():
    try:
        saved_cid = _CID_FILE.read_text(encoding="utf-8").strip()
        if len(saved_cid) == 32 and saved_cid not in _SC_CLIENT_IDS:
            _SC_CLIENT_IDS.insert(0, saved_cid)
    except Exception:
        pass

_sc_cid_idx = 0
_sc_client_id = _SC_CLIENT_IDS[0]
_last_scrape_time = 0
_sc_cache = {}  # query: (timestamp, results)
_sc_track_transcodings = {}  # tid: transcodings list

def _save_client_id_to_file(cid: str):
    try:
        _CID_FILE.write_text(cid, encoding="utf-8")
    except Exception:
        pass

def _scrape_fresh_client_id(force: bool = False) -> str:
    global _sc_client_id, _last_scrape_time, _SC_CLIENT_IDS
    now = time.time()
    if not force and now - _last_scrape_time < 120 and _sc_client_id:
        return _sc_client_id
    _last_scrape_time = now
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    try:
        req = urllib.request.Request("https://soundcloud.com", headers=headers)
        with urllib.request.urlopen(req, timeout=3.5) as res:
            html = res.read().decode("utf-8", errors="ignore")
        scripts = re.findall(r'<script[^>]+src="([^">]+)"', html)
        js_urls = [s for s in scripts if s.endswith(".js")]
        for url in list(reversed(js_urls))[:2]:
            try:
                req_js = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req_js, timeout=2.5) as js_res:
                    js_text = js_res.read().decode("utf-8", errors="ignore")
                cids = re.findall(r'client_id[:=]["\']([a-zA-Z0-9]{32})["\']', js_text)
                if not cids:
                    cids = re.findall(r'client_id["\']\s*:\s*["\']([a-zA-Z0-9]{32})["\']', js_text)
                if not cids:
                    cids = re.findall(r'client_id=([a-zA-Z0-9]{32})', js_text)
                if cids:
                    fresh_cid = cids[0]
                    print(f"[SoundCloud] Auto-scraped fresh client_id: {fresh_cid}")
                    _sc_client_id = fresh_cid
                    _save_client_id_to_file(fresh_cid)
                    if fresh_cid not in _SC_CLIENT_IDS:
                        _SC_CLIENT_IDS.insert(0, fresh_cid)
                    return fresh_cid
            except Exception:
                continue
    except Exception as e:
        print(f"[SoundCloud] Scrape fresh client_id failed: {e}")
    return _sc_client_id

def _get_sc_client_id() -> str:
    global _sc_client_id
    if not _sc_client_id:
        return _SC_CLIENT_IDS[0]
    return _sc_client_id

def _rotate_sc_client_id():
    global _sc_client_id, _sc_cid_idx
    if len(_SC_CLIENT_IDS) > 1:
        _sc_cid_idx = (_sc_cid_idx + 1) % len(_SC_CLIENT_IDS)
        _sc_client_id = _SC_CLIENT_IDS[_sc_cid_idx]
    else:
        import threading
        threading.Thread(target=_scrape_fresh_client_id, args=(True,), daemon=True).start()
    return _sc_client_id



class SoundCloudPlatform:
    """Nền tảng SoundCloud."""

    name  = "SoundCloud"
    color = 0xFC00  # Cam SoundCloud (RGB565)
    icon  = "S"

    # ── Search ────────────────────────────────────────────────
    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Tìm kiếm bài hát trên SoundCloud (API v2 trực tiếp ~0.2s, fallback yt-dlp)."""
        cache_key = f"{query.strip().lower()}:{limit}"
        now = time.time()
        if cache_key in _sc_cache:
            ts, cached_res = _sc_cache[cache_key]
            if now - ts < 300:  # 5 phút cache
                return list(cached_res)

        # 1. Thử gọi trực tiếp SoundCloud API v2 với danh sách client_id sẵn sàng
        for attempt in range(len(_SC_CLIENT_IDS)):
            cid = _get_sc_client_id()
            try:
                url = f"https://api-v2.soundcloud.com/search/tracks?q={urllib.parse.quote(query)}&client_id={cid}&limit={limit}"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                with urllib.request.urlopen(req, timeout=3.0) as res:
                    data = json.loads(res.read().decode("utf-8"))
                tracks = data.get("collection", [])
                results = []
                for t in tracks:
                    if not t or not isinstance(t, dict):
                        continue
                    tid = str(t.get("id", ""))
                    if not tid:
                        continue
                    # Cache transcodings để lấy stream URL tức thì (<150ms) không cần gọi tracks/{id}
                    tr_list = t.get("media", {}).get("transcodings", [])
                    if tr_list:
                        _sc_track_transcodings[tid] = tr_list
                    title = t.get("title") or t.get("permalink") or "SoundCloud Track"
                    user = t.get("user") or {}
                    artist = user.get("username") or "SoundCloud"
                    dur_ms = t.get("duration") or 0
                    dur_sec = int(dur_ms // 1000)
                    artwork = t.get("artwork_url") or user.get("avatar_url") or ""
                    if artwork and "-large" in artwork:
                        artwork = artwork.replace("-large", "-t300x300")
                    results.append({
                        "id":        tid,
                        "title":     title,
                        "artist":    artist,
                        "duration":  dur_sec,
                        "platform":  "soundcloud",
                        "thumbnail": artwork,
                    })
                if results:
                    _sc_cache[cache_key] = (now, results[:limit])
                    return results[:limit]
            except urllib.error.HTTPError as he:
                if he.code in (401, 403):
                    _rotate_sc_client_id()
                    continue
                break
            except Exception as ex_v2:
                print(f"[SoundCloud] Direct API v2 search error ({ex_v2})")
                break

        # 2. Fallback sang yt-dlp nếu API v2 bị lỗi
        search_url = f"scsearch{limit}:{query}"
        results = []
        try:
            with yt_dlp.YoutubeDL(_YDL_SEARCH_OPTS) as ydl:
                info = ydl.extract_info(search_url, download=False)
                entries = info.get("entries", []) if info else []
                for e in entries:
                    if not e:
                        continue
                    track_id = _extract_sc_id(e)
                    if not track_id:
                        continue
                    results.append({
                        "id":        track_id,
                        "title":     e.get("title", "Unknown"),
                        "artist":    e.get("uploader") or e.get("artist", "Unknown"),
                        "duration":  int(e.get("duration") or 0),
                        "platform":  "soundcloud",
                        "thumbnail": _first_thumb(e.get("thumbnails", [])),
                    })
        except Exception as ex:
            print(f"[SoundCloud] Search error: {ex}")
        return results[:limit]

    # ── Search Playlists ─────────────────────────────────────
    def search_playlists(self, query: str, limit: int = 10) -> list[dict]:
        """Tìm kiếm Playlist thực sự trên SoundCloud bằng API v2."""
        cid = _get_sc_client_id()
        url = f"https://api-v2.soundcloud.com/search/playlists_without_albums?q={urllib.parse.quote(query)}&client_id={cid}&limit={limit}"
        results = []
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as res:
                data = json.loads(res.read().decode("utf-8"))
            for p in data.get("collection", []):
                if not p:
                    continue
                pid = str(p.get("id", ""))
                if not pid:
                    continue
                title = p.get("title", "SoundCloud Playlist")
                user = p.get("user") or {}
                artist = user.get("username") or "SoundCloud"
                artwork = p.get("artwork_url") or ""
                if not artwork and p.get("tracks"):
                    artwork = p["tracks"][0].get("artwork_url") or ""
                if not artwork:
                    artwork = user.get("avatar_url") or ""
                if artwork and "-large" in artwork:
                    artwork = artwork.replace("-large", "-t300x300")

                results.append({
                    "id": pid,
                    "title": title,
                    "artist": artist,
                    "duration": 0,
                    "platform": "soundcloud",
                    "thumbnail": artwork,
                    "type": "playlist",
                    "track_count": p.get("track_count", len(p.get("tracks", [])))
                })
        except Exception as ex:
            print(f"[SoundCloud] Search playlists error: {ex}")
        return results[:limit]

    # ── Get Playlist Tracks ──────────────────────────────────
    def get_playlist_tracks(self, playlist_id: str, limit: int = 30) -> list[dict]:
        """Lấy danh sách các bài hát trong Playlist SoundCloud."""
        cid = _get_sc_client_id()
        url = f"https://api-v2.soundcloud.com/playlists/{playlist_id}?client_id={cid}"
        results = []
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as res:
                data = json.loads(res.read().decode("utf-8"))
            tracks = data.get("tracks", [])[:limit]

            # Bổ sung thông tin cho các bài hát từ vị trí thứ 5 trở đi (SoundCloud API v2 chỉ trả về stub ID)
            stub_ids = [str(t.get("id")) for t in tracks if isinstance(t, dict) and "title" not in t and t.get("id")]
            hydrated_map = {}
            if stub_ids:
                # Chia thành các chunk 15 IDs để tránh URL quá dài và timeout
                for i in range(0, len(stub_ids), 15):
                    chunk = stub_ids[i:i + 15]
                    try:
                        ids_param = ",".join(chunk)
                        tracks_url = f"https://api-v2.soundcloud.com/tracks?ids={ids_param}&client_id={cid}"
                        t_req = urllib.request.Request(tracks_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(t_req, timeout=10) as t_res:
                            hydrated_list = json.loads(t_res.read().decode("utf-8"))
                            for ht in hydrated_list:
                                if isinstance(ht, dict) and ht.get("id"):
                                    hydrated_map[str(ht["id"])] = ht
                    except Exception as ex_h:
                        print(f"[SoundCloud] Hydrate tracks chunk error: {ex_h}")

            for t in tracks:
                if not t or not isinstance(t, dict):
                    continue
                tid = str(t.get("id", ""))
                if not tid:
                    continue
                # Nếu bài hát là stub, thay thế bằng dữ liệu đã nạp
                if "title" not in t and tid in hydrated_map:
                    t = hydrated_map[tid]

                title = t.get("title") or t.get("permalink") or "SoundCloud Track"
                user = t.get("user") or {}
                artist = user.get("username") or "SoundCloud"
                dur_ms = t.get("duration") or 0
                dur_sec = int(dur_ms // 1000)
                artwork = t.get("artwork_url") or user.get("avatar_url") or ""
                if artwork and "-large" in artwork:
                    artwork = artwork.replace("-large", "-t300x300")
                results.append({
                    "id": tid,
                    "title": title,
                    "artist": artist,
                    "duration": dur_sec,
                    "platform": "soundcloud",
                    "thumbnail": artwork
                })
                if len(results) >= limit:
                    break
        except Exception as ex:
            print(f"[SoundCloud] Get tracks error: {ex}")
        return results

    # ── Stream URL ────────────────────────────────────────────
    def get_stream_url(self, song_id: str):
        """
        song_id: numeric track ID (e.g. '1581837499')
                 hoặc full URL (e.g. 'https://soundcloud.com/...')
        Trả về URL stream trực tiếp.
        """
        cid = _get_sc_client_id()

        # 1. Trích xuất track_id dạng số (nếu có) để gọi trực tiếp SoundCloud API v2 (siêu nhanh ~0.3s)
        numeric_id = None
        raw_url = None
        if song_id.isdigit():
            numeric_id = song_id
        elif song_id.startswith("b64_"):
            import base64
            pad = len(song_id[4:]) % 4
            b64 = song_id[4:] + ("=" * (4 - pad) if pad else "")
            raw_url = base64.urlsafe_b64decode(b64).decode(errors="ignore")
            m = re.search(r'/tracks/(\d+)', raw_url)
            if m:
                numeric_id = m.group(1)
        elif "/tracks/" in song_id:
            m = re.search(r'/tracks/(\d+)', song_id)
            if m:
                numeric_id = m.group(1)

        if numeric_id:
            trans = _sc_track_transcodings.get(str(numeric_id))
            for sc_attempt in range(2):
                try:
                    cid = _get_sc_client_id()
                    if not trans:
                        t_url = f"https://api-v2.soundcloud.com/tracks/{numeric_id}?client_id={cid}"
                        t_req = urllib.request.Request(t_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                        with urllib.request.urlopen(t_req, timeout=3.5) as t_res:
                            t_data = json.loads(t_res.read().decode("utf-8"))
                        trans = t_data.get("media", {}).get("transcodings", [])
                        if trans:
                            _sc_track_transcodings[str(numeric_id)] = trans
                    progs = [x for x in trans if x.get("format", {}).get("protocol") == "progressive"]
                    hls_mp3 = [x for x in trans if x.get("format", {}).get("protocol") == "hls" and ("mpeg" in str(x.get("format", {}).get("mime_type", "")) or "mp3" in str(x.get("format", {}).get("mime_type", "")))]
                    target = progs[0] if progs else (hls_mp3[0] if hls_mp3 else (trans[0] if trans else None))
                    if target and target.get("url"):
                        m_url = target["url"] + f"?client_id={cid}"
                        m_req = urllib.request.Request(m_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                        with urllib.request.urlopen(m_req, timeout=3.5) as m_res:
                            m_data = json.loads(m_res.read().decode("utf-8"))
                            direct = m_data.get("url")
                            if direct:
                                print(f"[SoundCloud] Direct API v2 stream URL obtained: {direct[:60]}...")
                                return direct
                except Exception as ex_v2:
                    trans = None  # Reset để thử lại nếu lỗi
                    if sc_attempt == 0:
                        print(f"[SoundCloud] Direct API v2 failed with cid={cid} ({ex_v2}), scraping fresh client_id...")
                        _scrape_fresh_client_id(force=True)
                    else:
                        print(f"[SoundCloud] Direct API v2 stream retry failed ({ex_v2}), falling back to yt-dlp...")

        # 2. Fallback sang yt-dlp nếu không lấy được bằng API v2
        if raw_url:
            url = raw_url
        elif song_id.isdigit():
            url = f"https://api.soundcloud.com/tracks/{song_id}"
        elif song_id.startswith("http"):
            url = song_id
        else:
            url = f"https://soundcloud.com/{song_id}"

        for attempt in range(3):
            try:
                with yt_dlp.YoutubeDL(_YDL_STREAM_OPTS) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if not info:
                        return None
                    # Ưu tiên url trực tiếp
                    direct = info.get("url")
                    if direct and "manifest" not in direct:
                        return direct
                    # Duyệt formats, bỏ qua HLS manifest
                    fmts = info.get("formats", [])
                    audio_fmts = [
                        f for f in fmts
                        if f.get("url")
                        and "manifest" not in f.get("url", "")
                        and f.get("acodec", "none") != "none"
                    ]
                    if audio_fmts:
                        audio_fmts.sort(key=lambda f: f.get("abr") or 0, reverse=True)
                        return audio_fmts[0]["url"]
                    # Fallback
                    for f in reversed(fmts):
                        if f.get("url") and "manifest" not in f.get("url", ""):
                            return f["url"]
            except Exception as ex:
                msg = str(ex)
                if "429" in msg or "Too Many Requests" in msg:
                    wait = (attempt + 1) * 3
                    print(f"[SoundCloud] 429 rate limit, retry in {wait}s...")
                    time.sleep(wait)
                    continue
                print(f"[SoundCloud] Stream error: {ex}")
                return None
        print("[SoundCloud] Stream failed after 3 retries")
        return None

    # ── Trending ─────────────────────────────────────────────
    def get_trending(self, limit: int = 20) -> list[dict]:
        """Lấy top charts SoundCloud."""
        return self.search("trending music 2024", limit)


def _extract_sc_id(entry: dict) -> str:
    """
    Lấy numeric track ID từ entry yt-dlp SoundCloud.
    Ưu tiên: entry['id'] → parse từ URL/webpage_url → fallback URL đầy đủ.
    """
    # entry['id'] thường là 'soundcloud:tracks:1581837499'
    raw_id = str(entry.get("id", ""))
    m = re.search(r":(\d+)$", raw_id)
    if m:
        return m.group(1)

    # Parse từ URL API: https://api.soundcloud.com/tracks/1581837499
    for key in ("url", "webpage_url"):
        u = entry.get(key, "")
        m = re.search(r"/tracks/(\d+)", u)
        if m:
            return m.group(1)

    # Fallback: giải quyết URL thành numeric ID sạch
    url = entry.get("webpage_url") or entry.get("url", "")
    if "soundcloud.com" in url:
        cid = _get_sc_client_id()
        try:
            r_url = f"https://api-v2.soundcloud.com/resolve?url={urllib.parse.quote(url)}&client_id={cid}"
            req = urllib.request.Request(r_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data and data.get("id"):
                return str(data["id"])
        except Exception:
            pass
        import base64
        return "b64_" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")

    return ""


def _first_thumb(thumbs: list) -> str:
    if not thumbs:
        return ""
    for t in thumbs:
        w = t.get("width", 0) or 0
        if 100 <= w <= 500:
            return t.get("url", "")
    return thumbs[0].get("url", "") if thumbs else ""
