"""
SoundCloud Platform — via yt-dlp
"""

import re
import time
import json
import urllib.request
import urllib.parse
import yt_dlp

_YDL_SEARCH_OPTS = {
    "quiet":        True,
    "no_warnings":  True,
    "extract_flat": True,
    "playlist_items": "1:20",
    "sleep_interval": 0.5,
}

_YDL_STREAM_OPTS = {
    "quiet":         True,
    "no_warnings":   True,
    "format":        "bestaudio[ext=mp3]/bestaudio[abr<=128]/bestaudio",
    "noplaylist":    True,
    "retries":       3,
    "sleep_interval": 1,
}

_sc_client_id = "Pb72ranhoyt6gw7hM7TkzUItXlMWSNSo"
_sc_client_id_exp = 0

def _get_sc_client_id() -> str:
    global _sc_client_id, _sc_client_id_exp
    if _sc_client_id and time.time() < _sc_client_id_exp:
        return _sc_client_id
    try:
        req = urllib.request.Request("https://soundcloud.com", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        scripts = re.findall(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html)
        for s in scripts[-6:]:
            try:
                with urllib.request.urlopen(s, timeout=5) as js_resp:
                    js = js_resp.read().decode("utf-8", errors="ignore")
                m = re.search(r'client_id:"([a-zA-Z0-9]{32})"', js)
                if m:
                    _sc_client_id = m.group(1)
                    _sc_client_id_exp = time.time() + 86400
                    return _sc_client_id
            except Exception:
                pass
    except Exception as ex:
        print(f"[SoundCloud] get client_id error: {ex}")
    return _sc_client_id or "Pb72ranhoyt6gw7hM7TkzUItXlMWSNSo"


class SoundCloudPlatform:
    """Nền tảng SoundCloud."""

    name  = "SoundCloud"
    color = 0xFC00  # Cam SoundCloud (RGB565)
    icon  = "S"

    # ── Search ────────────────────────────────────────────────
    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Tìm kiếm bài hát trên SoundCloud."""
        search_url = f"scsearch{limit}:{query}"
        results = []
        try:
            with yt_dlp.YoutubeDL(_YDL_SEARCH_OPTS) as ydl:
                info = ydl.extract_info(search_url, download=False)
                entries = info.get("entries", []) if info else []
                for e in entries:
                    if not e:
                        continue
                    # Lấy numeric track ID từ URL hoặc ID field
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
                try:
                    ids_param = ",".join(stub_ids)
                    tracks_url = f"https://api-v2.soundcloud.com/tracks?ids={ids_param}&client_id={cid}"
                    t_req = urllib.request.Request(tracks_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(t_req, timeout=6) as t_res:
                        hydrated_list = json.loads(t_res.read().decode("utf-8"))
                        for ht in hydrated_list:
                            if isinstance(ht, dict) and ht.get("id"):
                                hydrated_map[str(ht["id"])] = ht
                except Exception as ex_h:
                    print(f"[SoundCloud] Hydrate tracks error: {ex_h}")

            for t in tracks:
                if not t or not isinstance(t, dict):
                    continue
                tid = str(t.get("id", ""))
                if not tid:
                    continue
                # Nếu bài hát là stub, thay thế bằng dữ liệu đã nạp
                if "title" not in t and tid in hydrated_map:
                    t = hydrated_map[tid]

                title = t.get("title", "Unknown")
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
    def get_stream_url(self, song_id: str) -> str | None:
        """
        song_id: numeric track ID (e.g. '1581837499')
                 hoặc full URL (e.g. 'https://soundcloud.com/...')
        Trả về URL stream trực tiếp.
        """
        # Xây dựng URL từ numeric ID hoặc b64_ ID
        if song_id.startswith("b64_"):
            import base64
            pad = len(song_id[4:]) % 4
            b64 = song_id[4:] + ("=" * (4 - pad) if pad else "")
            url = base64.urlsafe_b64decode(b64).decode(errors="ignore")
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
