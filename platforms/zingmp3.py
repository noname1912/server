"""
ZingMP3 Platform
ZingMP3 hiện chặn bot bằng Cloudflare nên dùng YouTube Music
để tìm nhạc Việt thay thế (kết quả tương đương).
"""

import yt_dlp

_YDL_SEARCH_OPTS = {
    "quiet":          True,
    "no_warnings":    True,
    "extract_flat":   True,
    "playlist_items": "1:20",
}

_YDL_STREAM_OPTS = {
    "quiet":       True,
    "no_warnings": True,
    "format":      "bestaudio[ext=m4a]/140/bestaudio[acodec^=mp4a]/bestaudio[ext=mp3]/bestaudio",
    "noplaylist":  True,
}


class ZingMP3Platform:
    """Nền tảng ZingMP3."""

    name  = "ZingMP3"
    color = 0xFF60   # Cam ZingMP3 (RGB565)
    icon  = "Z"

    # ── Search ────────────────────────────────────────────────
    def search(self, query: str, limit: int = 20) -> list[dict]:
        """
        Tìm kiếm nhạc Việt qua YouTube Music.
        (ZingMP3 chặn bot nên dùng YouTube làm backend thay thế)
        """
        # Thêm từ khóa VN để ưu tiên nhạc Việt
        search_url = f"ytsearch{limit}:{query} nhac viet"
        results = []
        try:
            with yt_dlp.YoutubeDL(_YDL_SEARCH_OPTS) as ydl:
                info = ydl.extract_info(search_url, download=False)
                entries = info.get("entries", []) if info else []
                for e in entries:
                    if not e:
                        continue
                    dur = int(e.get("duration") or 0)
                    if dur > 3600:
                        continue
                    vid_id = e.get("id", "")
                    if not vid_id:
                        continue
                    results.append({
                        "id":        vid_id,
                        "title":     _clean_title(e.get("title", "Unknown")),
                        "artist":    e.get("channel") or e.get("uploader", "Unknown"),
                        "duration":  dur,
                        "platform":  "zing",
                        "thumbnail": f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg",
                    })
        except Exception as ex:
            print(f"[ZingMP3] Search error: {ex}")
        return results[:limit]

    # ── Search Playlists ─────────────────────────────────────
    def search_playlists(self, query: str, limit: int = 10) -> list[dict]:
        """Tìm kiếm Playlist / Album nhạc Việt."""
        import urllib.parse
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query + ' playlist nhac viet')}&sp=EgIQAw%253D%253D"
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
                    if not str(thumb).startswith("http"):
                        thumb = ""
                    results.append({
                        "id": pid,
                        "title": _clean_title(e.get("title", "Album Nhac Viet")),
                        "artist": e.get("channel") or e.get("uploader") or "ZingMP3",
                        "thumbnail": thumb,
                        "duration": 0,
                        "platform": "zing",
                        "type": "playlist"
                    })
                    if len(results) >= limit:
                        break
        except Exception as ex:
            print(f"[ZingMP3] Search playlists error: {ex}")
        return results[:limit]

    # ── Get Playlist Tracks ──────────────────────────────────
    def get_playlist_tracks(self, playlist_id: str, limit: int = 30) -> list[dict]:
        """Lấy danh sách bài hát trong Playlist."""
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
                        "artist": e.get("channel") or e.get("uploader", "ZingMP3"),
                        "duration": dur,
                        "platform": "zing",
                        "thumbnail": f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg",
                    })
        except Exception as ex:
            print(f"[ZingMP3] Get playlist tracks error: {ex}")
        return results[:limit]

    # ── Stream URL ────────────────────────────────────────────
    def get_stream_url(self, song_id: str) -> str | None:
        """
        song_id: YouTube video ID (vì ZingMP3 dùng YouTube làm backend)
        """
        if song_id.startswith("http"):
            url = song_id
        else:
            url = f"https://www.youtube.com/watch?v={song_id}"

        try:
            with yt_dlp.YoutubeDL(_YDL_STREAM_OPTS) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                direct = info.get("url")
                if direct:
                    return direct
                fmts = info.get("formats", [])
                audio = [
                    f for f in fmts
                    if f.get("url") and f.get("vcodec") == "none" and (
                        f.get("ext") in ("m4a", "mp3", "aac") or
                        "mp4a" in str(f.get("acodec", "")).lower()
                    )
                ]
                if audio:
                    audio.sort(key=lambda f: f.get("abr") or 0, reverse=True)
                    return audio[0]["url"]

                any_audio = [f for f in fmts if f.get("url") and f.get("vcodec") == "none"]
                if any_audio:
                    return any_audio[0]["url"]

                for f in reversed(fmts):
                    if f.get("url"):
                        return f["url"]
        except Exception as ex:
            print(f"[ZingMP3] Stream error: {ex}")
        return None

    # ── Trending ──────────────────────────────────────────────
    def get_trending(self, limit: int = 20) -> list[dict]:
        """Bài đang hot nhạc Việt."""
        return self.search("nhac hot viet nam 2024", limit)


def _clean_title(title: str) -> str:
    """Bỏ các suffix thừa."""
    for s in ["(Official MV)", "(Official Video)", "(Lyric Video)",
              "(Audio)", "(MV)", "(Official)", "| Official MV",
              "[Official MV]", "[MV]"]:
        title = title.replace(s, "").strip()
    return title
