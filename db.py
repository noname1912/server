"""
Database module for ESP32 Music Player Server.
Sử dụng SQLite3 để lưu trữ:
- Danh sách bài hát yêu thích (favorites)
- Lịch sử tìm kiếm gần đây (search_history)
"""

import sqlite3
import os
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "music.db"


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Khởi tạo các bảng nếu chưa có."""
    conn = get_db()
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT DEFAULT '',
                duration INTEGER DEFAULT 0,
                thumbnail TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL UNIQUE,
                platform TEXT DEFAULT 'youtube',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    conn.close()
    print(f"[DB] SQLite database initialized at: {DB_PATH}")


# ─── Search History ──────────────────────────────────────────

def add_search_history(query: str, platform: str = "youtube"):
    """Lưu từ khóa tìm kiếm mới, đưa lên đầu, giữ tối đa 30 mục."""
    q = query.strip()
    if not q or len(q) < 2:
        return
    conn = get_db()
    try:
        with conn:
            conn.execute("""
                INSERT INTO search_history (query, platform, created_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(query) DO UPDATE SET
                    created_at = CURRENT_TIMESTAMP,
                    platform = excluded.platform
            """, (q, platform))
            # Xóa các mục cũ nếu vượt quá 30
            conn.execute("""
                DELETE FROM search_history WHERE id NOT IN (
                    SELECT id FROM search_history ORDER BY created_at DESC LIMIT 30
                )
            """)
    finally:
        conn.close()


def get_recent_searches(limit: int = 5) -> list[str]:
    """Lấy N từ khóa tìm kiếm gần đây nhất."""
    conn = get_db()
    try:
        cursor = conn.execute("""
            SELECT query FROM search_history
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))
        return [row["query"] for row in cursor.fetchall()]
    finally:
        conn.close()


def get_suggestions(prefix: str, limit: int = 5) -> list[str]:
    """
    Gợi ý từ khóa:
    1. Tìm trong lịch sử tìm kiếm khớp tiền tố
    2. Nếu không đủ, tìm trong danh sách bài hát yêu thích khớp tiền tố
    """
    p = prefix.strip().lower()
    if not p:
        return get_recent_searches(limit)

    results = []
    conn = get_db()
    try:
        cursor = conn.execute("""
            SELECT query FROM search_history
            WHERE LOWER(query) LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (f"{p}%", limit))
        for row in cursor.fetchall():
            if row["query"] not in results:
                results.append(row["query"])

        if len(results) < limit:
            cursor = conn.execute("""
                SELECT title FROM favorites
                WHERE LOWER(title) LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (f"%{p}%", limit - len(results)))
            for row in cursor.fetchall():
                if row["title"] not in results:
                    results.append(row["title"])

        return results[:limit]
    finally:
        conn.close()


# ─── Favorites ───────────────────────────────────────────────

def toggle_favorite(song: dict) -> bool:
    """
    Thêm vào yêu thích nếu chưa có, hoặc xóa nếu đã có.
    Trả về True nếu bài hát được thêm vào yêu thích, False nếu đã xóa.
    """
    song_id = str(song.get("id", "")).strip()
    if not song_id:
        return False

    conn = get_db()
    try:
        cursor = conn.execute("SELECT id FROM favorites WHERE id = ?", (song_id,))
        exists = cursor.fetchone() is not None

        with conn:
            if exists:
                conn.execute("DELETE FROM favorites WHERE id = ?", (song_id,))
                print(f"[DB] Removed favorite: {song_id}")
                return False
            else:
                conn.execute("""
                    INSERT INTO favorites (id, platform, title, artist, duration, thumbnail)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    song_id,
                    song.get("platform", "youtube"),
                    song.get("title", "Unknown"),
                    song.get("artist", "Unknown"),
                    int(song.get("duration", 0)),
                    song.get("thumbnail", ""),
                ))
                print(f"[DB] Added favorite: {song_id} - {song.get('title')}")
                return True
    finally:
        conn.close()


def get_favorites() -> list[dict]:
    """Lấy danh sách toàn bộ bài hát yêu thích (sắp xếp mới nhất trước)."""
    conn = get_db()
    try:
        cursor = conn.execute("""
            SELECT id, platform, title, artist, duration, thumbnail
            FROM favorites
            ORDER BY created_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_favorite_ids() -> list[str]:
    """Lấy danh sách ID các bài yêu thích."""
    conn = get_db()
    try:
        cursor = conn.execute("SELECT id FROM favorites")
        return [row["id"] for row in cursor.fetchall()]
    finally:
        conn.close()
