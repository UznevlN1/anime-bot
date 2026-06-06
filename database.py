import sqlite3
import os

DB_PATH = "anime.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()

    # Foydalanuvchilar
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        user_id INTEGER UNIQUE,
        username TEXT,
        full_name TEXT,
        join_number INTEGER,
        is_blocked INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        joined_at TEXT DEFAULT (datetime('now'))
    )""")

    # Animlar
    c.execute("""
    CREATE TABLE IF NOT EXISTS animes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        year TEXT,
        country TEXT,
        genre TEXT,
        description TEXT,
        photo_id TEXT,
        media_type TEXT,
        views INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )""")

    # Qismlar
    c.execute("""
    CREATE TABLE IF NOT EXISTS episodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anime_id INTEGER,
        episode_number INTEGER,
        file_id TEXT,
        FOREIGN KEY (anime_id) REFERENCES animes(id)
    )""")

    # Majburiy kanallar
    c.execute("""
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT,
        channel_name TEXT
    )""")

    # Sozlamalar
    c.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")

    # Default sozlamalar
    c.execute("INSERT OR IGNORE INTO settings VALUES ('maintenance', '0')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('content_protect', '1')")

    conn.commit()
    conn.close()

# ===== FOYDALANUVCHILAR =====
def add_user(user_id, username, full_name):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    count = c.fetchone()[0]
    try:
        c.execute("""INSERT OR IGNORE INTO users 
            (user_id, username, full_name, join_number) 
            VALUES (?, ?, ?, ?)""",
            (user_id, username, full_name, count + 1))
        conn.commit()
        inserted = c.rowcount > 0
    except:
        inserted = False
    conn.close()
    return inserted

def get_user(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def block_user(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET is_blocked=1, is_active=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def unblock_user(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET is_blocked=0, is_active=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def set_user_inactive(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET is_active=0, is_blocked=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def get_all_active_users():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE is_active=1 AND is_blocked=0")
    rows = c.fetchall()
    conn.close()
    return [r["user_id"] for r in rows]

def get_stats():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_active=1 AND is_blocked=0")
    active = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1")
    blocked = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM animes")
    total_animes = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM animes WHERE media_type='film'")
    films = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM animes WHERE media_type='serial'")
    serials = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE date(joined_at)=date('now')")
    today = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE date(joined_at)>=date('now','-7 days')")
    week = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE date(joined_at)>=date('now','-30 days')")
    month = c.fetchone()[0]
    c.execute("SELECT id, title, views FROM animes ORDER BY views DESC LIMIT 5")
    top = [dict(r) for r in c.fetchall()]
    conn.close()
    return {
        "total": total, "active": active, "blocked": blocked,
        "total_animes": total_animes, "films": films, "serials": serials,
        "today": today, "week": week, "month": month, "top": top
    }

# ===== ANIMLAR =====
def add_anime(title, year, country, genre, description, photo_id, media_type):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""INSERT INTO animes (title, year, country, genre, description, photo_id, media_type)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (title, year, country, genre, description, photo_id, media_type))
    anime_id = c.lastrowid
    conn.commit()
    conn.close()
    return anime_id

def get_animes(media_type=None, page=0, per_page=10):
    conn = get_conn()
    c = conn.cursor()
    offset = page * per_page
    if media_type:
        c.execute("SELECT * FROM animes WHERE media_type=? ORDER BY id DESC LIMIT ? OFFSET ?",
                  (media_type, per_page, offset))
    else:
        c.execute("SELECT * FROM animes ORDER BY id DESC LIMIT ? OFFSET ?",
                  (per_page, offset))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_anime_count(media_type=None):
    conn = get_conn()
    c = conn.cursor()
    if media_type:
        c.execute("SELECT COUNT(*) FROM animes WHERE media_type=?", (media_type,))
    else:
        c.execute("SELECT COUNT(*) FROM animes")
    count = c.fetchone()[0]
    conn.close()
    return count

def get_anime(anime_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM animes WHERE id=?", (anime_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def search_anime(query):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM animes WHERE LOWER(title) LIKE ? ORDER BY views DESC LIMIT 20",
              (f"%{query.lower()}%",))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def delete_anime(anime_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM episodes WHERE anime_id=?", (anime_id,))
    c.execute("DELETE FROM animes WHERE id=?", (anime_id,))
    conn.commit()
    conn.close()

def update_anime(anime_id, field, value):
    conn = get_conn()
    c = conn.cursor()
    c.execute(f"UPDATE animes SET {field}=? WHERE id=?", (value, anime_id))
    conn.commit()
    conn.close()

def increment_views(anime_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE animes SET views=views+1 WHERE id=?", (anime_id,))
    conn.commit()
    conn.close()

def get_random_anime():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM animes ORDER BY RANDOM() LIMIT 1")
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

# ===== QISMLAR =====
def add_episode(anime_id, episode_number, file_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO episodes (anime_id, episode_number, file_id) VALUES (?, ?, ?)",
              (anime_id, episode_number, file_id))
    conn.commit()
    conn.close()

def get_episodes(anime_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM episodes WHERE anime_id=? ORDER BY episode_number", (anime_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def delete_episode(episode_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
    conn.commit()
    conn.close()

def get_episode(episode_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM episodes WHERE id=?", (episode_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

# ===== KANALLAR =====
def add_channel(channel_id, channel_name):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO channels (channel_id, channel_name) VALUES (?, ?)",
              (channel_id, channel_name))
    conn.commit()
    conn.close()

def delete_channel(channel_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))
    conn.commit()
    conn.close()

def get_channels():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM channels")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

# ===== SOZLAMALAR =====
def get_setting(key):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row["value"] if row else None

def set_setting(key, value):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()
