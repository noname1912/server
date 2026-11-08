"""
YouTube Platform — via yt-dlp
"""

import yt_dlp

_YDL_SEARCH_OPTS = {
    "quiet":          True,
    "no_warnings":    True,
    "extract_flat":   True,
    "playlist_items": "1:20",
    "socket_timeout": 8,
    "retries":        2,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"]
        }
    },
}

_YDL_STREAM_OPTS = {
    "quiet":          True,
    "no_warnings":    True,
    "format":         "ba/b",
    "noplaylist":     True,
    "socket_timeout": 8,
    "retries":        3,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios"]
        }
    },
}


import urllib.request
import json

def _innertube_search(query: str, limit: int = 20) -> list:
    """Tìm kiếm YouTube qua InnerTube API trực tiếp siêu nhanh (~0.2s, không qua yt-dlp)."""
    url = "https://www.youtube.com/youtubei/v1/search"
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20240401.01.00",
                "hl": "vi",
                "gl": "VN"
            }
        },
        "query": f"{query} audio"
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    )
    with urllib.request.urlopen(req, timeout=6.0) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))

    items = []
    def parse_dur(s):
        try:
            parts = list(map(int, s.split(":")))
            if len(parts) == 2: return parts[0]*60 + parts[1]
            if len(parts) == 3: return parts[0]*3600 + parts[1]*60 + parts[2]
        except Exception: pass
        return 0

    try:
        sections = res_data.get("contents", {}).get("twoColumnSearchResultsRenderer", {}).get("primaryContents", {}).get("sectionListRenderer", {}).get("contents", [])
        for section in sections:
            for it in section.get("itemSectionRenderer", {}).get("contents", []):
                vr = it.get("videoRenderer")
                if vr:
                    vid = vr.get("videoId")
                    title = "".join(r.get("text", "") for r in vr.get("title", {}).get("runs", []))
                    artist = "".join(r.get("text", "") for r in vr.get("ownerText", {}).get("runs", []))
                    dur_str = vr.get("lengthText", {}).get("simpleText", "")
                    if not dur_str and vr.get("lengthText", {}).get("runs"):
                        dur_str = "".join(r.get("text", "") for r in vr["lengthText"]["runs"])
                    dur = parse_dur(dur_str or "0:00")
                    if dur > 3600:
                        continue
                    if dur <= 0:
                        dur = 180
                    if vid and title:
                        items.append({
                            "id": vid,
                            "title": _clean_title(title),
                            "artist": artist or "YouTube",
                            "duration": dur,
                            "platform": "youtube",
                            "thumbnail": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg"
                        })
                    if len(items) >= limit:
                        return items
    except Exception as e:
        print(f"[YouTube-InnerTube] Parse error: {e}")
    return items

class YouTubePlatform:
    """Nền tảng YouTube Music."""

    name  = "YouTube"
    color = 0xF800  # Đỏ YouTube (RGB565)
    icon  = "Y"

    # ── Search ────────────────────────────────────────────────
    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Tìm kiếm bài hát trên YouTube (Ưu tiên InnerTube API ~0.2s, fallback yt-dlp)."""
        # 1. Thử InnerTube API trực tiếp siêu tốc
        try:
            fast_res = _innertube_search(query, limit=limit)
            if fast_res and len(fast_res) > 0:
                return fast_res[:limit]
        except Exception as ex_it:
            print(f"[YouTube] Fast search failed: {ex_it}, falling back to yt-dlp...")

        # 2. Fallback sang yt-dlp nếu InnerTube lỗi
        search_url = f"ytsearch{limit}:{query} music"
        results = []
        try:
            with yt_dlp.YoutubeDL(_YDL_SEARCH_OPTS) as ydl:
                info = ydl.extract_info(search_url, download=False)
                entries = info.get("entries", []) if info else []
                for e in entries:
                    if not e:
                        continue
                    # Bỏ qua video quá dài (> 1 tiếng)
                    dur = int(e.get("duration") or 0)
                    if dur <= 0:
                        dur = 180
                    if dur > 3600:
                        continue
                    if dur <= 0:
                        dur = 180
                    results.append({
                        "id":       e.get("id", ""),
                        "title":    _clean_title(e.get("title", "Unknown")),
                        "artist":   e.get("channel") or e.get("uploader", "Unknown"),
                        "duration": dur,
                        "platform": "youtube",
                        "thumbnail": f"https://img.youtube.com/vi/{e.get('id', '')}/mqdefault.jpg",
                    })
        except Exception as ex:
            print(f"[YouTube] Search error: {ex}")
        return results[:limit]

    # ── Search Playlists ─────────────────────────────────────
    def search_playlists(self, query: str, limit: int = 10) -> list[dict]:
        """Tìm kiếm Playlist / Album trên YouTube."""
        import urllib.parse
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}&sp=EgIQAw%253D%253D"
        results = []
        try:
            with yt_dlp.YoutubeDL(_YDL_SEARCH_OPTS) as ydl:
                info = ydl.extract_info(url, download=False)
                entries = info.get("entries", []) if info else []
                for e in entries:
                    if not e:
                        continue
                    pid = e.get("id", "")
                    if not pid or not pid.startswith("PL"):
                        continue
                    thumb = e.get("thumbnail") or ""
                    if not thumb and e.get("thumbnails"):
                        thumb = e.get("thumbnails")[-1].get("url", "")
                    if not str(thumb).startswith("http"):
                        thumb = ""
                    results.append({
                        "id": pid,
                        "title": _clean_title(e.get("title", "Unknown Playlist")),
                        "artist": e.get("channel") or e.get("uploader") or "YouTube",
                        "thumbnail": thumb,
                        "duration": 0,
                        "platform": "youtube",
                        "type": "playlist"
                    })
                    if len(results) >= limit:
                        break
        except Exception as ex:
            print(f"[YouTube] Search playlists error: {ex}")
        return results[:limit]

    # ── Get Playlist Tracks ──────────────────────────────────
    def get_playlist_tracks(self, playlist_id: str, limit: int = 30) -> list[dict]:
        """Trích xuất danh sách bài hát từ YouTube Playlist."""
        url = f"https://www.youtube.com/playlist?list={playlist_id}"
        results = []
        try:
            opts = dict(_YDL_SEARCH_OPTS)
            opts["playlist_items"] = f"1:{limit}"
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                entries = info.get("entries", []) if info else []
                for e in entries:
                    if not e:
                        continue
                    vid_id = e.get("id", "")
                    if not vid_id:
                        continue
                    dur = int(e.get("duration") or 0)
                    if dur <= 0:
                        dur = 180
                    results.append({
                        "id": vid_id,
                        "title": _clean_title(e.get("title", "Unknown")),
                        "artist": e.get("channel") or e.get("uploader", "YouTube"),
                        "duration": dur,
                        "platform": "youtube",
                        "thumbnail": f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg",
                    })
        except Exception as ex:
            print(f"[YouTube] Get playlist tracks error: {ex}")
        return results[:limit]

    # ── Stream URL ────────────────────────────────────────────
    def get_stream_url(self, song_id: str):
        """
        song_id là YouTube video ID (e.g. 'dQw4w9WgXcQ').
        Trả về URL audio stream.
        """
        url = f"https://www.youtube.com/watch?v={song_id}"
        try:
            with yt_dlp.YoutubeDL(_YDL_STREAM_OPTS) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                # url trực tiếp
                direct = info.get("url")
                if direct:
                    return direct
                # Duyệt formats
                fmts = info.get("formats", [])
                # Ưu tiên m4a / aac / mp3 formats (ESP32-audioI2S giải mã được)
                audio_fmts = [
                    f for f in fmts
                    if f.get("vcodec") == "none" and f.get("url") and (
                        f.get("ext") in ("m4a", "mp3", "aac") or
                        "mp4a" in str(f.get("acodec", "")).lower()
                    )
                ]
                if audio_fmts:
                    # Ưu tiên bitrate quanh 128kbps (format 140)
                    audio_fmts.sort(key=lambda f: f.get("abr") or 0, reverse=True)
                    return audio_fmts[0]["url"]

                # Fallback: audio formats khác
                any_audio = [f for f in fmts if f.get("vcodec") == "none" and f.get("url")]
                if any_audio:
                    return any_audio[0]["url"]

                # Fallback cuối
                for f in reversed(fmts):
                    if f.get("url"):
                        return f["url"]
        except Exception as ex:
            print(f"[YouTube] Stream error: {ex}")
        return None

    # ── Trending ──────────────────────────────────────────────
    def get_trending(self, limit: int = 20) -> list[dict]:
        """Top nhạc thịnh hành."""
        return self.search("nhạc hot 2024 việt nam", limit)


def _clean_title(title: str) -> str:
    """Loại bỏ các suffix thừa như '(Official MV)', '(Lyric Video)'..."""
    for suffix in ["(Official MV)", "(Official Video)", "(Lyric Video)",
                   "(Audio)", "(MV)", "(Official)", "| Official MV"]:
        title = title.replace(suffix, "").strip()
    return title
