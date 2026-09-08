"""
SoundCloud Platform — via yt-dlp
"""

import re
import time
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
        """Tìm kiếm Sets / Mix / Playlist trên SoundCloud."""
        search_url = f"scsearch{limit}:{query} non stop viet mix"
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
                        "title":     e.get("title", "Nonstop Mix"),
                        "artist":    e.get("uploader") or "SoundCloud",
                        "duration":  int(e.get("duration") or 0),
                        "platform":  "soundcloud",
                        "thumbnail": _first_thumb(e.get("thumbnails", [])),
                        "type":      "playlist"
                    })
        except Exception as ex:
            print(f"[SoundCloud] Search playlists error: {ex}")
        return results[:limit]

    # ── Get Playlist Tracks ──────────────────────────────────
    def get_playlist_tracks(self, playlist_id: str, limit: int = 30) -> list[dict]:
        """Lấy bài hát trong playlist (hoặc phát mix đó)."""
        # Với SoundCloud mix, trả về chính track đó
        return [{
            "id": playlist_id,
            "title": "SoundCloud Mix Set",
            "artist": "SoundCloud",
            "duration": 1800,
            "platform": "soundcloud",
            "thumbnail": ""
        }]

    # ── Stream URL ────────────────────────────────────────────
    def get_stream_url(self, song_id: str) -> str | None:
        """
        song_id: numeric track ID (e.g. '1581837499')
                 hoặc full URL (e.g. 'https://soundcloud.com/...')
        Trả về URL stream trực tiếp.
        """
        # Xây dựng URL từ numeric ID
        if song_id.isdigit():
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

    # Fallback: dùng nguyên URL nếu là soundcloud.com
    url = entry.get("webpage_url") or entry.get("url", "")
    if "soundcloud.com" in url:
        return url  # server sẽ dùng trực tiếp

    return ""


def _first_thumb(thumbs: list) -> str:
    if not thumbs:
        return ""
    for t in thumbs:
        w = t.get("width", 0) or 0
        if 100 <= w <= 500:
            return t.get("url", "")
    return thumbs[0].get("url", "") if thumbs else ""
