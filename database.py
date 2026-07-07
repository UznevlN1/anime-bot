import psycopg2
import psycopg2.extras
import psycopg2.pool
import os
import time
from datetime import datetime, timedelta

DATABASE_URL = os.environ.get("DATABASE_URL")

# Connection pool: har safar yangi ulanish ochish/yopish oʻrniga
# tayyor ulanishlardan foydalaniladi (tezroq va samaraliroq).
# Neon (bepul reja) foydalanilmagan ulanishlarni bir necha daqiqadan
# keyin oʻzi yopib qoʻyadi ("SSL connection has been closed unexpectedly"),
# shuning uchun har bir ulanish pool'dan olinganda tekshiriladi va,
# agar oʻlik boʻlsa, avtomatik yangisi bilan almashtiriladi.
_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=25,  # 10 dan oshirildi: bot + webapp bir vaqtda ko'p so'rov yuborganda
                 # ulanish yetishmay ("pool exhausted") xatolik/sekinlashuv bo'lmasligi uchun
    dsn=DATABASE_URL,
)

# Ulanish tirikligini har safar tekshirish (SELECT 1) qoʻshimcha DB round-trip
# qoʻshadi va har bir soʻrovni ikki baravar sekinlashtiradi. Neon ulanishlarni
# faqat bir necha DAQIQA foydalanilmay qolgandagina yopadi, shuning uchun
# yaqinda ishlatilgan ulanishni qayta tekshirish ortiqcha. Har bir ulanish
# uchun oxirgi tekshirilgan vaqtni saqlab, qisqa muddat (15s) ichida qayta
# soʻralsa, tekshiruvni oʻtkazib yuboramiz.
_LIVENESS_TTL = 15
_last_checked = {}

def get_conn():
    conn = _pool.getconn()
    key = id(conn)
    now = time.time()
    last = _last_checked.get(key, 0)
    if now - last >= _LIVENESS_TTL:
        try:
            # Ulanish tirikligini tekshirish
            with conn.cursor() as c:
                c.execute("SELECT 1")
            _last_checked[key] = now
        except Exception:
            # Ulanish oʻlik — pool'dan butunlay chiqarib, yangisini ochamiz
            _last_checked.pop(key, None)
            try:
                _pool.putconn(conn, close=True)
            except Exception:
                pass
            conn = psycopg2.connect(DATABASE_URL)
            _last_checked[id(conn)] = now
    return conn

def put_conn(conn):
    # Agar biror joyda xatolik chiqib, commit() chaqirilmagan bo'lsa, ulanish
    # "aborted transaction" holatida qolishi mumkin — shu holatda pool'ga
    # qaytarilsa, uni keyin oladigan har qanday so'rov "current transaction
    # is aborted" xatosiga uchraydi, toki kimdir rollback() chaqirmaguncha.
    # Shuning uchun pool'ga qaytarishdan oldin har doim xavfsiz rollback qilamiz.
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        _pool.putconn(conn)
    except Exception:
        _last_checked.pop(id(conn), None)
        try:
            conn.close()
        except Exception:
            pass

def init_db():
    conn = get_conn()
    c = conn.cursor()

    # Foydalanuvchilar
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        user_id BIGINT UNIQUE,
        username TEXT,
        full_name TEXT,
        phone TEXT,
        join_number INTEGER,
        is_blocked INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        version TEXT DEFAULT '1.0.0',
        joined_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
    )""")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_premium INTEGER DEFAULT 0")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until TEXT")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_plan TEXT")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_renew_notified INTEGER DEFAULT 0")

    # Animlar
    c.execute("""
    CREATE TABLE IF NOT EXISTS animes (
        id SERIAL PRIMARY KEY,
        title TEXT,
        year TEXT,
        country TEXT,
        genre TEXT,
        description TEXT,
        language TEXT DEFAULT 'Nomalum',
        photo_id TEXT,
        media_type TEXT,
        views INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
    )""")
    c.execute("ALTER TABLE animes ADD COLUMN IF NOT EXISTS category TEXT")
    c.execute("ALTER TABLE animes ADD COLUMN IF NOT EXISTS total_episodes INTEGER")

    # Bannerlar (webapp bosh sahifasidagi reklama/eʼlon suratlari)
    c.execute("""
    CREATE TABLE IF NOT EXISTS banners (
        id SERIAL PRIMARY KEY,
        photo_id TEXT,
        title TEXT,
        subtitle TEXT,
        anime_id INTEGER,
        position INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
    )""")

    # Izohlar
    c.execute("""
    CREATE TABLE IF NOT EXISTS comments (
        id SERIAL PRIMARY KEY,
        anime_id INTEGER,
        user_id BIGINT,
        username TEXT,
        text TEXT,
        is_deleted INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
    )""")
    c.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS parent_id INTEGER")

    # Izohlarga bosilgan "like"lar
    c.execute("""
    CREATE TABLE IF NOT EXISTS comment_likes (
        comment_id INTEGER,
        user_id BIGINT,
        created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')),
        PRIMARY KEY (comment_id, user_id)
    )""")

    # Tomosha jurnali (haqiqiy "bugun necha marta koʻrildi" statistikasi uchun)
    c.execute("""
    CREATE TABLE IF NOT EXISTS watch_log (
        id SERIAL PRIMARY KEY,
        anime_id INTEGER,
        user_id BIGINT,
        watched_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
    )""")

    # Qismlar
    c.execute("""
    CREATE TABLE IF NOT EXISTS episodes (
        id SERIAL PRIMARY KEY,
        anime_id INTEGER,
        episode_number INTEGER,
        channel_message_id INTEGER,
        FOREIGN KEY (anime_id) REFERENCES animes(id)
    )""")
    c.execute("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))")

    # Video koʻrish pozitsiyasi (davomidan koʻrish uchun, localStorage'ga
    # tayanmasdan — Telegram mini-ilova ba'zi qurilmalarda WebView xotirasini
    # har safar tozalab yuborishi mumkin, shuning uchun serverda saqlaymiz)
    c.execute("""
    CREATE TABLE IF NOT EXISTS watch_positions (
        user_id BIGINT NOT NULL,
        episode_id INTEGER NOT NULL,
        position_seconds INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')),
        PRIMARY KEY (user_id, episode_id)
    )""")

    # Sevimlilar (serverda — profil statistikasi va Sevimlilar bo'limi
    # har doim toʻgʻri ishlashi uchun, localStorage'ga tayanmasdan)
    c.execute("""
    CREATE TABLE IF NOT EXISTS favorites (
        user_id BIGINT NOT NULL,
        anime_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')),
        PRIMARY KEY (user_id, anime_id)
    )""")

    # Tomosha faolligi (profil statistikasi: necha xil anime koʻrilgan va
    # necha kun ketma-ket tomosha qilingan — "davom etishda" uchun)
    c.execute("""
    CREATE TABLE IF NOT EXISTS watch_activity (
        user_id BIGINT NOT NULL,
        activity_date DATE NOT NULL,
        anime_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, activity_date, anime_id)
    )""")

    # Majburiy kanallar
    c.execute("""
    CREATE TABLE IF NOT EXISTS channels (
        id SERIAL PRIMARY KEY,
        channel_id TEXT UNIQUE,
        channel_name TEXT
    )""")

    # Sozlamalar
    c.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")

    # Premium to'lov so'rovlari (qo'lda tasdiqlash uchun)
    c.execute("""
    CREATE TABLE IF NOT EXISTS premium_payments (
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        plan TEXT,
        amount INTEGER,
        screenshot_file_id TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')),
        processed_at TEXT
    )""")

    # --- Tezlik uchun indekslar (mavjud ma'lumot yoki xulq-atvorni o'zgartirmaydi,
    # faqat qidiruv/filtrlashni tezlashtiradi) ---
    c.execute("CREATE INDEX IF NOT EXISTS idx_episodes_anime_id ON episodes(anime_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_comments_anime_id ON comments(anime_id, is_deleted)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_comments_parent_id ON comments(parent_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_watch_log_anime_watched ON watch_log(anime_id, watched_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_premium_payments_status ON premium_payments(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_premium_payments_user_id ON premium_payments(user_id)")

    # --- join_number uchun sequence: har bir ro'yxatdan o'tishda "SELECT COUNT(*) FROM users"
    # ishlatish o'rniga (bu foydalanuvchilar ko'paygani sari sekinlashadi), Postgres SEQUENCE
    # ishlatiladi — natija (ketma-ket raqamlash) bir xil, lekin har doim tezkor (O(1)). ---
    c.execute("CREATE SEQUENCE IF NOT EXISTS user_join_seq")
    c.execute("""
        SELECT setval('user_join_seq',
            GREATEST(
                (SELECT COALESCE(MAX(join_number), 0) FROM users),
                (SELECT last_value FROM user_join_seq)
            )
        )
    """)

    # Default sozlamalar
    c.execute("INSERT INTO settings VALUES ('maintenance', '0') ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO settings VALUES ('content_protect', '1') ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO settings VALUES ('bot_version', '1.0.0') ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO settings VALUES ('storage_channel', '') ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO settings VALUES ('premium_price_1m', '15000') ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO settings VALUES ('premium_price_3m', '40000') ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO settings VALUES ('premium_price_1y', '120000') ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO settings VALUES ('premium_early_hours', '48') ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO settings VALUES ('premium_referral_bonus_days', '3') ON CONFLICT DO NOTHING")

    conn.commit()
    put_conn(conn)

# ===== FOYDALANUVCHILAR =====
def add_user(user_id, username, full_name, phone=None, referred_by=None):
    conn = get_conn()
    c = conn.cursor()
    # Eslatma: ilgari bu yerda "SELECT COUNT(*) FROM users" ishlatilardi — foydalanuvchilar
    # ko'paygani sari sekinlashib borardi (har safar butun jadval sanalardi). Endi
    # join_number Postgres SEQUENCE'dan olinadi — natija bir xil (ketma-ket raqamlar),
    # lekin har doim bir zumda (O(1)) ishlaydi.
    c.execute("SELECT nextval('user_join_seq')")
    next_number = c.fetchone()[0]
    try:
        c.execute("""INSERT INTO users 
            (user_id, username, full_name, phone, join_number, referred_by) 
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING""",
            (user_id, username, full_name, phone, next_number, referred_by))
        conn.commit()
        inserted = c.rowcount > 0
    except:
        inserted = False
    put_conn(conn)
    return inserted

def update_phone(user_id, phone):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET phone=%s WHERE user_id=%s", (phone, user_id))
    conn.commit()
    put_conn(conn)

def get_user(user_id):
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    c.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
    row = c.fetchone()
    put_conn(conn)
    return dict(row) if row else None

def get_user_by_username(username):
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    username = username.lstrip('@')
    c.execute("SELECT * FROM users WHERE username=%s", (username,))
    row = c.fetchone()
    put_conn(conn)
    return dict(row) if row else None

# ===== PREMIUM =====
from datetime import timedelta

def get_premium_status(user_id):
    """Foydalanuvchining Premium holatini qaytaradi: {is_premium, until, days_left, plan}.
    Muddati o'tgan bo'lsa avtomatik ravishda is_premium=0 ga tushiriladi."""
    u = get_user(user_id)
    if not u or not u.get("is_premium") or not u.get("premium_until"):
        return {"is_premium": False, "until": None, "days_left": 0, "plan": None}
    try:
        until = datetime.strptime(u["premium_until"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return {"is_premium": False, "until": None, "days_left": 0, "plan": None}
    now = datetime.now()
    if until <= now:
        set_premium(user_id, None, active=False)
        return {"is_premium": False, "until": None, "days_left": 0, "plan": None}
    days_left = max(1, (until - now).days + (1 if (until - now).seconds > 0 else 0))
    return {"is_premium": True, "until": u["premium_until"], "days_left": days_left, "plan": u.get("premium_plan")}

def set_premium(user_id, until_dt, plan=None, active=True):
    """until_dt — datetime obyekti yoki None. active=False qilib o'chirib qo'yish uchun ham ishlatiladi."""
    conn = get_conn()
    c = conn.cursor()
    if active and until_dt:
        c.execute(
            "UPDATE users SET is_premium=1, premium_until=%s, premium_plan=%s, premium_renew_notified=0 WHERE user_id=%s",
            (until_dt.strftime("%Y-%m-%d %H:%M:%S"), plan, user_id)
        )
    else:
        c.execute("UPDATE users SET is_premium=0 WHERE user_id=%s", (user_id,))
    conn.commit()
    put_conn(conn)

def extend_premium(user_id, days, plan=None):
    """Mavjud muddatga kun qo'shadi (agar hali faol bo'lsa), aks holda hozirdan boshlab hisoblaydi."""
    status = get_premium_status(user_id)
    base = datetime.strptime(status["until"], "%Y-%m-%d %H:%M:%S") if status["is_premium"] else datetime.now()
    new_until = base + timedelta(days=days)
    set_premium(user_id, new_until, plan or status.get("plan"))
    return new_until

def get_expiring_premium_users(within_days=2):
    """Muddati within_days ichida tugaydigan, hali ogohlantirilmagan foydalanuvchilar."""
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    cutoff = (datetime.now() + timedelta(days=within_days)).strftime("%Y-%m-%d %H:%M:%S")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        """SELECT * FROM users WHERE is_premium=1 AND premium_until IS NOT NULL
           AND premium_until <= %s AND premium_until > %s AND premium_renew_notified=0""",
        (cutoff, now)
    )
    rows = [dict(r) for r in c.fetchall()]
    put_conn(conn)
    return rows

def mark_renew_notified(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET premium_renew_notified=1 WHERE user_id=%s", (user_id,))
    conn.commit()
    put_conn(conn)

def expire_premiums():
    """Muddati o'tgan barcha Premium'larni o'chiradi. Kunlik fon vazifasi uchun."""
    conn = get_conn()
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("UPDATE users SET is_premium=0 WHERE is_premium=1 AND premium_until IS NOT NULL AND premium_until <= %s", (now,))
    affected = c.rowcount
    conn.commit()
    put_conn(conn)
    return affected

# ---- Premium to'lov so'rovlari ----
def create_payment_request(user_id, plan, amount, screenshot_file_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO premium_payments (user_id, plan, amount, screenshot_file_id) VALUES (%s,%s,%s,%s) RETURNING id",
        (user_id, plan, amount, screenshot_file_id)
    )
    new_id = c.fetchone()[0]
    conn.commit()
    put_conn(conn)
    return new_id

def get_payment_request(payment_id):
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    c.execute("SELECT * FROM premium_payments WHERE id=%s", (payment_id,))
    row = c.fetchone()
    put_conn(conn)
    return dict(row) if row else None

def get_pending_payments():
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    c.execute("SELECT * FROM premium_payments WHERE status='pending' ORDER BY id DESC")
    rows = [dict(r) for r in c.fetchall()]
    put_conn(conn)
    return rows

def set_payment_status(payment_id, status):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE premium_payments SET status=%s, processed_at=to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS') WHERE id=%s",
        (status, payment_id)
    )
    conn.commit()
    put_conn(conn)

# ---- Referral bonus ----
def process_referral_bonus(referrer_id, bonus_days):
    """Yangi foydalanuvchi kimningdir taklifi bilan kelganda, taklif qilgan odamga bonus kun qo'shadi."""
    if not referrer_id:
        return
    referrer = get_user(referrer_id)
    if not referrer:
        return
    extend_premium(referrer_id, bonus_days, plan=referrer.get("premium_plan") or "referral")

def block_user(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET is_blocked=1, is_active=0 WHERE user_id=%s", (user_id,))
    conn.commit()
    put_conn(conn)

def unblock_user(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET is_blocked=0, is_active=1 WHERE user_id=%s", (user_id,))
    conn.commit()
    put_conn(conn)

def set_user_inactive(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET is_active=0, is_blocked=1 WHERE user_id=%s", (user_id,))
    conn.commit()
    put_conn(conn)

def update_user_info(user_id, username=None, full_name=None):
    """Telegramdan kelgan joriy ism/username bilan foydalanuvchi yozuvini
    yangilaydi ("Hisobni yangilash" tugmasi uchun)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE users SET username=COALESCE(%s, username), full_name=COALESCE(%s, full_name) WHERE user_id=%s",
        (username, full_name, user_id),
    )
    conn.commit()
    put_conn(conn)

def delete_user_data(user_id):
    """Foydalanuvchi "Hisobni o'chirish"ni tanlaganda chaqiriladi.

    To'lovlar/referral tarixini (audit uchun) saqlab qolamiz, lekin shaxsiy
    ma'lumotlarni (sevimlilar, tomosha tarixi, pozitsiyalar, izohlar matni,
    ism/username) tozalaymiz va hisobni bloklaymiz — u endi botdan
    foydalana olmaydi."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM favorites WHERE user_id=%s", (user_id,))
    c.execute("DELETE FROM watch_positions WHERE user_id=%s", (user_id,))
    c.execute("DELETE FROM watch_activity WHERE user_id=%s", (user_id,))
    c.execute("DELETE FROM comment_likes WHERE user_id=%s", (user_id,))
    c.execute(
        "UPDATE comments SET is_deleted=1, text='[o''chirilgan]' WHERE user_id=%s",
        (user_id,),
    )
    c.execute(
        "UPDATE users SET username=NULL, full_name='O''chirilgan foydalanuvchi', phone=NULL, "
        "is_active=0, is_blocked=1 WHERE user_id=%s",
        (user_id,),
    )
    conn.commit()
    put_conn(conn)

def get_all_active_users():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE is_active=1 AND is_blocked=0")
    rows = c.fetchall()
    put_conn(conn)
    return [r[0] for r in rows]

def update_user_version(user_id, version):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET version=%s WHERE user_id=%s", (version, user_id))
    conn.commit()
    put_conn(conn)

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
    c.execute("SELECT COUNT(*) FROM users WHERE date(joined_at)=CURRENT_DATE")
    today = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE date(joined_at)>=CURRENT_DATE - INTERVAL '7 days'")
    week = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE date(joined_at)>=CURRENT_DATE - INTERVAL '30 days'")
    month = c.fetchone()[0]
    cd = psycopg2.extras.RealDictCursor(conn)
    cd.execute("SELECT id, title, views FROM animes ORDER BY views DESC LIMIT 5")
    top = [dict(r) for r in cd.fetchall()]
    put_conn(conn)
    return {
        "total": total, "active": active, "blocked": blocked,
        "total_animes": total_animes, "films": films, "serials": serials,
        "today": today, "week": week, "month": month, "top": top
    }

def get_daily_stats():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE date(joined_at)=CURRENT_DATE")
    new_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE date(joined_at)=CURRENT_DATE AND is_blocked=1")
    left_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM animes WHERE date(created_at)=CURRENT_DATE")
    new_animes = c.fetchone()[0]
    c.execute("SELECT SUM(views) FROM animes")
    total_views = c.fetchone()[0] or 0
    put_conn(conn)
    return {
        "new_users": new_users,
        "left_users": left_users,
        "new_animes": new_animes,
        "total_views": total_views
    }

# ===== ANIMLAR =====
def add_anime(title, year, country, genre, description, language, photo_id, media_type, total_episodes=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""INSERT INTO animes (title, year, country, genre, description, language, photo_id, media_type, total_episodes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (title, year, country, genre, description, language, photo_id, media_type, total_episodes))
    anime_id = c.fetchone()[0]
    conn.commit()
    put_conn(conn)
    return anime_id

def get_animes(media_type=None, page=0, per_page=10):
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    offset = page * per_page
    if media_type:
        c.execute("""SELECT a.*, (SELECT COUNT(*) FROM episodes e WHERE e.anime_id=a.id) AS episode_count
                     FROM animes a WHERE media_type=%s ORDER BY id DESC LIMIT %s OFFSET %s""",
                  (media_type, per_page, offset))
    else:
        c.execute("""SELECT a.*, (SELECT COUNT(*) FROM episodes e WHERE e.anime_id=a.id) AS episode_count
                     FROM animes a ORDER BY id DESC LIMIT %s OFFSET %s""",
                  (per_page, offset))
    rows = [dict(r) for r in c.fetchall()]
    put_conn(conn)
    return rows

def get_anime_count(media_type=None):
    conn = get_conn()
    c = conn.cursor()
    if media_type:
        c.execute("SELECT COUNT(*) FROM animes WHERE media_type=%s", (media_type,))
    else:
        c.execute("SELECT COUNT(*) FROM animes")
    count = c.fetchone()[0]
    put_conn(conn)
    return count

def get_anime(anime_id):
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    c.execute("SELECT * FROM animes WHERE id=%s", (anime_id,))
    row = c.fetchone()
    put_conn(conn)
    return dict(row) if row else None

def search_anime(query):
    """Sarlavha ichidan qidiradi (harflar katta-kichikligiga qaramasdan).
    Eslatma: ilgari bu yerda '=' (aniq mos kelish) ishlatilardi — foydalanuvchi
    sarlavhani harfma-harf to'liq yozmaguncha hech narsa topilmasdi. Endi
    qismli qidiruv ishlaydi (masalan 'naruto' yozsa 'Naruto Shippuden' ham chiqadi)."""
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    c.execute(
        "SELECT * FROM animes WHERE title ILIKE %s ESCAPE '\\' ORDER BY views DESC LIMIT 20",
        ('%' + escaped + '%',)
    )
    rows = [dict(r) for r in c.fetchall()]
    put_conn(conn)
    return rows

def delete_anime(anime_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM episodes WHERE anime_id=%s", (anime_id,))
    c.execute("DELETE FROM animes WHERE id=%s", (anime_id,))
    conn.commit()
    put_conn(conn)

# update_anime orqali oʻzgartirish mumkin boʻlgan ustunlar roʻyxati.
# Bu SQL Injection'dan himoya qiladi — faqat shu roʻyxatdagi
# ustun nomlariga yozish ruxsat etiladi.
ALLOWED_ANIME_FIELDS = {
    "title", "year", "country", "genre",
    "description", "language", "photo_id", "media_type", "category",
    "total_episodes",
}

def update_anime(anime_id, field, value):
    if field not in ALLOWED_ANIME_FIELDS:
        raise ValueError(f"Notogri maydon nomi: {field}")
    conn = get_conn()
    c = conn.cursor()
    c.execute(f"UPDATE animes SET {field}=%s WHERE id=%s", (value, anime_id))
    conn.commit()
    put_conn(conn)

def increment_views(anime_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE animes SET views=views+1 WHERE id=%s", (anime_id,))
    conn.commit()
    put_conn(conn)

def get_random_anime():
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    c.execute("SELECT * FROM animes ORDER BY RANDOM() LIMIT 1")
    row = c.fetchone()
    put_conn(conn)
    return dict(row) if row else None

# ===== QISMLAR =====
def add_episode(anime_id, episode_number, channel_message_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO episodes (anime_id, episode_number, channel_message_id) VALUES (%s, %s, %s)",
              (anime_id, episode_number, channel_message_id))
    conn.commit()
    put_conn(conn)

def get_episodes(anime_id):
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    c.execute("SELECT * FROM episodes WHERE anime_id=%s ORDER BY episode_number", (anime_id,))
    rows = [dict(r) for r in c.fetchall()]
    put_conn(conn)
    return rows

def delete_episode(episode_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM episodes WHERE id=%s", (episode_id,))
    conn.commit()
    put_conn(conn)

def get_episode(episode_id):
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    c.execute("SELECT * FROM episodes WHERE id=%s", (episode_id,))
    row = c.fetchone()
    put_conn(conn)
    return dict(row) if row else None

def update_episode(episode_id, channel_message_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE episodes SET channel_message_id=%s WHERE id=%s", (channel_message_id, episode_id))
    conn.commit()
    put_conn(conn)

def get_watch_position(user_id, episode_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT position_seconds FROM watch_positions WHERE user_id=%s AND episode_id=%s", (user_id, episode_id))
    row = c.fetchone()
    put_conn(conn)
    return row[0] if row else 0

def set_watch_position(user_id, episode_id, seconds):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO watch_positions (user_id, episode_id, position_seconds, updated_at)
        VALUES (%s, %s, %s, to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        ON CONFLICT (user_id, episode_id)
        DO UPDATE SET position_seconds=EXCLUDED.position_seconds, updated_at=EXCLUDED.updated_at
    """, (user_id, episode_id, int(seconds)))
    conn.commit()
    put_conn(conn)

def toggle_favorite(user_id, anime_id):
    """Sevimlilarga qo'shadi/olib tashlaydi. Yangi holatni (True=sevimli) qaytaradi."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM favorites WHERE user_id=%s AND anime_id=%s", (user_id, anime_id))
    exists = c.fetchone()
    if exists:
        c.execute("DELETE FROM favorites WHERE user_id=%s AND anime_id=%s", (user_id, anime_id))
        active = False
    else:
        c.execute("INSERT INTO favorites (user_id, anime_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (user_id, anime_id))
        active = True
    conn.commit()
    put_conn(conn)
    return active

def get_favorite_ids(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT anime_id FROM favorites WHERE user_id=%s ORDER BY created_at DESC", (user_id,))
    ids = [r[0] for r in c.fetchall()]
    put_conn(conn)
    return ids

def record_watch_activity(user_id, anime_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO watch_activity (user_id, activity_date, anime_id) VALUES (%s, CURRENT_DATE, %s) ON CONFLICT DO NOTHING",
        (user_id, anime_id)
    )
    conn.commit()
    put_conn(conn)

def get_profile_stats(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM favorites WHERE user_id=%s", (user_id,))
    favorites = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT anime_id) FROM watch_activity WHERE user_id=%s", (user_id,))
    watched = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(position_seconds),0) FROM watch_positions WHERE user_id=%s", (user_id,))
    total_seconds = c.fetchone()[0]
    c.execute("SELECT DISTINCT activity_date FROM watch_activity WHERE user_id=%s ORDER BY activity_date DESC", (user_id,))
    dates = [r[0] for r in c.fetchall()]
    put_conn(conn)
    # Streak (ketma-ket kunlar) — bugun yoki kechadan boshlab hisoblanadi
    streak = 0
    if dates:
        today = datetime.now().date()
        cur = today if dates[0] == today else (today - timedelta(days=1))
        date_set = set(dates)
        while cur in date_set:
            streak += 1
            cur = cur - timedelta(days=1)
    return {
        "favorites": favorites,
        "watched": watched,
        "watch_hours": round(total_seconds / 3600, 1),
        "streak": streak,
    }

def get_recent_anime_ids(user_id, limit=8):
    """Foydalanuvchi oxirgi marta tomosha qilgan animelar roʻyxati (eng soʻnggisi birinchi),
    watch_positions jadvalidagi haqiqiy saqlash vaqtlariga asoslanadi — localStorage'ga
    tayanmasdan, Telegram mini-ilova qayta ochilganda ham ishonchli ishlaydi."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT e.anime_id, MAX(wp.updated_at) AS last_watched
        FROM watch_positions wp
        JOIN episodes e ON e.id = wp.episode_id
        WHERE wp.user_id=%s
        GROUP BY e.anime_id
        ORDER BY last_watched DESC
        LIMIT %s
    """, (user_id, limit))
    ids = [r[0] for r in c.fetchall()]
    put_conn(conn)
    return ids

def unlock_all_old_episodes():
    """Bug tuzatish: bir vaqtlar migratsiya (ALTER TABLE ... DEFAULT NOW()) barcha eski
    qismlarning created_at ustunini o'sha migratsiya vaqtiga o'rnatib qo'ygan edi, natijada
    ular 'yangi qo'shilgan' deb hisoblanib, Premium 'oldinroq kirish' muddati davomida
    hammaga qulflanib qolgan. Bu funksiya barcha mavjud qismlarning created_at'ini uzoq
    o'tmishga suradi — shu bilan ular hech kimga qulflanmay qoladi. Bundan keyin YANGI
    qo'shiladigan qismlar odatdagidek haqiqiy vaqt bilan saqlanadi va Premium erta-kirish
    cheklovi ular uchun normal ishlayveradi."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE episodes SET created_at='2000-01-01 00:00:00'")
    affected = c.rowcount
    conn.commit()
    put_conn(conn)
    return affected

# ===== KANALLAR =====
def add_channel(channel_id, channel_name):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO channels (channel_id, channel_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
              (channel_id, channel_name))
    conn.commit()
    put_conn(conn)

def delete_channel(channel_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM channels WHERE channel_id=%s", (channel_id,))
    conn.commit()
    put_conn(conn)

def get_channels():
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    c.execute("SELECT * FROM channels")
    rows = [dict(r) for r in c.fetchall()]
    put_conn(conn)
    return rows

# ===== SOZLAMALAR =====
def get_setting(key):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=%s", (key,))
    row = c.fetchone()
    put_conn(conn)
    return row[0] if row else None

def set_setting(key, value):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=%s",
              (key, value, value))
    conn.commit()
    put_conn(conn)

# ===== WEBAPP UCHUN =====
def get_animes_for_webapp():
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    c.execute("""
        SELECT a.id, a.title, a.year, a.genre, a.category, a.description, a.photo_id,
               a.media_type, a.views, a.total_episodes,
               COUNT(e.id) AS episode_count
        FROM animes a
        LEFT JOIN episodes e ON e.anime_id = a.id
        GROUP BY a.id
        ORDER BY a.id DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    put_conn(conn)
    return rows

def get_anime_detail_for_webapp(anime_id):
    anime = get_anime(anime_id)
    if not anime:
        return None
    anime["episodes"] = get_episodes(anime_id)
    anime["watched_today"] = get_watch_count_today(anime_id)
    return anime

# ===== BANNERLAR =====
def add_banner(photo_id, title, subtitle, anime_id, position=0):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO banners (photo_id, title, subtitle, anime_id, position) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (photo_id, title, subtitle, anime_id, position)
    )
    new_id = c.fetchone()[0]
    conn.commit()
    put_conn(conn)
    return new_id

def get_banners(active_only=True):
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    if active_only:
        c.execute("SELECT * FROM banners WHERE is_active=1 ORDER BY position ASC, id DESC")
    else:
        c.execute("SELECT * FROM banners ORDER BY position ASC, id DESC")
    rows = [dict(r) for r in c.fetchall()]
    put_conn(conn)
    return rows

def delete_banner(banner_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM banners WHERE id=%s", (banner_id,))
    conn.commit()
    put_conn(conn)

def set_banner_active(banner_id, active):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE banners SET is_active=%s WHERE id=%s", (1 if active else 0, banner_id))
    conn.commit()
    put_conn(conn)

# ===== KATEGORIYALAR =====
def get_categories():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT DISTINCT category FROM animes WHERE category IS NOT NULL AND category <> '' ORDER BY category ASC")
    rows = [r[0] for r in c.fetchall()]
    put_conn(conn)
    return rows

# ===== IZOHLAR =====
def add_comment(anime_id, user_id, username, text, parent_id=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO comments (anime_id, user_id, username, text, parent_id) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (anime_id, user_id, username, text, parent_id)
    )
    new_id = c.fetchone()[0]
    conn.commit()
    put_conn(conn)
    return new_id

def get_comments(anime_id, limit=50, viewer_id=None):
    conn = get_conn()
    c = psycopg2.extras.RealDictCursor(conn)
    c.execute(
        """
        SELECT c.*,
               COALESCE(lc.likes, 0) AS likes,
               CASE WHEN vl.user_id IS NULL THEN false ELSE true END AS liked_by_me,
               COALESCE(u.is_premium, 0) AS is_premium
        FROM comments c
        LEFT JOIN (SELECT comment_id, COUNT(*) AS likes FROM comment_likes GROUP BY comment_id) lc
               ON lc.comment_id = c.id
        LEFT JOIN comment_likes vl ON vl.comment_id = c.id AND vl.user_id = %s
        LEFT JOIN users u ON u.user_id = c.user_id
        WHERE c.anime_id=%s AND c.is_deleted=0
        ORDER BY (CASE WHEN c.parent_id IS NULL THEN COALESCE(u.is_premium,0) ELSE 0 END) DESC, c.id DESC
        LIMIT %s
        """,
        (viewer_id, anime_id, limit)
    )
    rows = [dict(r) for r in c.fetchall()]
    put_conn(conn)
    return rows

def toggle_comment_like(comment_id, user_id):
    """Like bosilgan/bosilmagan holatini almashtiradi. Yangi holatni (True=liked) qaytaradi."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM comment_likes WHERE comment_id=%s AND user_id=%s", (comment_id, user_id))
    exists = c.fetchone()
    if exists:
        c.execute("DELETE FROM comment_likes WHERE comment_id=%s AND user_id=%s", (comment_id, user_id))
        liked = False
    else:
        c.execute("INSERT INTO comment_likes (comment_id, user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (comment_id, user_id))
        liked = True
    conn.commit()
    c.execute("SELECT COUNT(*) FROM comment_likes WHERE comment_id=%s", (comment_id,))
    count = c.fetchone()[0]
    put_conn(conn)
    return liked, count

def get_last_comment_at(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT created_at FROM comments WHERE user_id=%s ORDER BY id DESC LIMIT 1", (user_id,))
    row = c.fetchone()
    put_conn(conn)
    return row[0] if row else None

def delete_comment(comment_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE comments SET is_deleted=1 WHERE id=%s", (comment_id,))
    conn.commit()
    put_conn(conn)

# ===== TOMOSHA JURNALI =====
def log_watch(anime_id, user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO watch_log (anime_id, user_id) VALUES (%s,%s)", (anime_id, user_id))
    conn.commit()
    put_conn(conn)

def get_watch_count_today(anime_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM watch_log WHERE anime_id=%s AND watched_at >= to_char(NOW(), 'YYYY-MM-DD')",
        (anime_id,)
    )
    count = c.fetchone()[0]
    put_conn(conn)
    return count
