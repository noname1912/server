"""
YouTube Platform — via yt-dlp
"""

import yt_dlp

_YDL_SEARCH_OPTS = {
    "quiet":        True,
    "no_warnings":  True,
    "extract_flat": True,
    "playlist_items": "1:20",
}

_YDL_STREAM_OPTS = {
    "quiet":       True,
    "no_warnings": True,
    # Ưu tiên m4a/aac/mp3 để ESP32-audioI2S giải mã được (không dùng opus/webm)
    "format":      "bestaudio[ext=m4a]/140/bestaudio[acodec^=mp4a]/bestaudio[ext=mp3]/bestaudio",
    "noplaylist":  True,
}


class YouTubePlatform:
    """Nền tảng YouTube Music."""

    name  = "YouTube"
    color = 0xF800  # Đỏ YouTube (RGB565)
    icon  = "Y"

    # ── Search ────────────────────────────────────────────────
    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Tìm kiếm bài hát trên YouTube."""
        # Thêm "music" để ưu tiên kết quả âm nhạc
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
                    if dur > 3600:
                        continue
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
    def get_stream_url(self, song_id: str) -> str | None:
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
