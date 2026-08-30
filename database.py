import psycopg2
import psycopg2.extras
import psycopg2.pool
import os
import json
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

# O'zbekiston vaqt zonasi. Bazadagi barcha ulanishlar shu zonada ochiladi
# (pastga qarang: options="-c timezone=..."), shuning uchun Postgres'ning
# NOW() / CURRENT_DATE funksiyalari va to_char(NOW(),...) orqali saqlangan
# barcha vaqt ustunlari endi to'g'ridan-to'g'ri Toshkent mahalliy vaqtida
# bo'ladi (server qayerda joylashganidan qat'iy nazar — Render/Neon odatda
# UTC ishlatadi). Python tomonida shu bazaviy qiymatlar bilan solishtiriladigan
# "hozir" kerak bo'lsa, oddiy datetime.now() emas, shu quyidagi now_tz()
# funksiyasi ishlatilishi kerak — aks holda ikki taraf orasida ~5 soatlik
# nomuvofiqlik paydo bo'ladi.
TASHKENT_TZ = ZoneInfo("Asia/Tashkent")

def now_tz():
    """Bazadagi vaqt ustunlari bilan bir xil zonadagi (Asia/Tashkent), lekin
    tzinfo'siz ("naive") datetime qaytaradi — shu bilan bazadan o'qilgan
    "YYYY-MM-DD HH:MI:SS" satrlarini strptime qilib to'g'ridan-to'g'ri
    solishtirish mumkin."""
    return datetime.now(TASHKENT_TZ).replace(tzinfo=None)

# Connection pool: har safar yangi ulanish ochish/yopish oʻrniga
# tayyor ulanishlardan foydalaniladi (tezroq va samaraliroq).
# Neon (bepul reja) foydalanilmagan ulanishlarni bir necha daqiqadan
# keyin oʻzi yopib qoʻyadi ("SSL connection has been closed unexpectedly"),
# shuning uchun har bir ulanish pool'dan olinganda tekshiriladi va,
# agar oʻlik boʻlsa, avtomatik yangisi bilan almashtiriladi.
# options="-c timezone=..." — ulanish ochilishidayoq sessiya vaqt zonasini
# belgilaydi (har bir so'rovda qo'shimcha "SET TIME ZONE" round-trip shart
# emas), shu bilan NOW()/CURRENT_DATE doim Toshkent vaqtida ishlaydi.
_CONN_OPTIONS = "-c timezone=Asia/Tashkent"
_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=25,  # 10 dan oshirildi: bot + webapp bir vaqtda ko'p so'rov yuborganda
                 # ulanish yetishmay ("pool exhausted") xatolik/sekinlashuv bo'lmasligi uchun
    dsn=DATABASE_URL,
    options=_CONN_OPTIONS,
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
            conn = psycopg2.connect(DATABASE_URL, options=_CONN_OPTIONS)
            # Bu ulanish _pool.getconn() orqali chiqmagan — pool uni "tanimaydi".
            # Belgilab qo'yamiz, shunda put_conn() uni _pool.putconn()ga
            # yubormaydi (aks holda psycopg2 "trying to put unkeyed connection"
            # xatosini beradi; eski kodda bu try/except bilan tutilgani uchun
            # ko'rinmas edi, lekin shu oraliqda jismoniy ulanishlar soni
            # maxconn=25'dan qisqa muddatga oshib ketishi mumkin edi).
            conn._uznev_standalone = True
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
    if getattr(conn, "_uznev_standalone", False):
        # get_conn() o'lik ulanishni almashtirganda pool'dan tashqarida ochgan —
        # pool.putconn() bunga "notanish" deb xato beradi, shuning uchun
        # to'g'ridan-to'g'ri yopamiz.
        _last_checked.pop(id(conn), None)
        try:
            conn.close()
        except Exception:
            pass
        return
    try:
        _pool.putconn(conn)
    except Exception:
        _last_checked.pop(id(conn), None)
        try:
            conn.close()
        except Exception:
            pass

# DIQQAT: quyidagi 100+ funksiyaning deyarli barchasi hozircha
# `conn = get_conn(); ... ; put_conn(conn)` shaklida yozilgan — ORASIDA
# xatolik chiqsa (masalan execute() constraint xatosi, kutilmagan None
# qiymat va h.k.), put_conn(conn) HECH QACHON chaqirilmaydi va shu ulanish
# pool'dan (jami 25 tadan) abadiy "yo'qoladi". Vaqt o'tishi bilan (ayniqsa
# xato tez-tez chiqadigan joylarda) pool butunlay tugab, BARCHA baza
# amallari to'xtab qolishi mumkin — buni faqat process qayta ishga
# tushirish tuzatadi. Quyidagi context manager shu muammoni butunlay yo'q
# qiladi (finally: orqali put_conn() har doim chaqirilishini kafolatlaydi).
# Yangi funksiyalarda shuni ishlating; eskilarini ham asta-sekin shunga
# o'tkazish tavsiya etiladi:
#
#   def some_func(...):
#       with db_conn() as conn:
#           c = conn.cursor()
#           c.execute(...)
#           conn.commit()
#           return ...
from contextlib import contextmanager

@contextmanager
def db_conn():
    conn = get_conn()
    try:
        yield conn
    finally:
        put_conn(conn)

def init_db():
    conn = get_conn()
    try:
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
        # Taklif qilingan foydalanuvchi tufayli uning taklif qilgan odamiga necha kun
        # bonus berilgani — agar bu foydalanuvchi keyinchalik botni tark etsa
        # (bloklasa), revoke_referral_bonus() aynan shu qadar kunni taklif qilgan
        # odamning Premium muddatidan qaytarib oladi (suiiste'molning oldini olish).
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_bonus_days INTEGER DEFAULT 0")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_renew_notified INTEGER DEFAULT 0")
        # Webapp 🔔 bildirishnoma paneli — foydalanuvchi oxirgi ko'rgan bildirishnoma
        # id'si (shundan katta id'li bildirishnomalar "o'qilmagan" hisoblanadi).
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_notification_id INTEGER DEFAULT 0")
        # AI funksiyalaridan (chat, tavsiya) hech boʻlmasa bir marta foydalanganmi
        # — foydalanmaganlarga maxsus taklif xabari yuborish uchun.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_used INTEGER DEFAULT 0")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_used_at TEXT")

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
        # Anime butunlay (barcha qismlari) doimiy Premium-only qilib belgilanishi mumkin
        c.execute("ALTER TABLE animes ADD COLUMN IF NOT EXISTS is_premium_only INTEGER DEFAULT 0")
        # Anime holati: 'ongoing' (davom etmoqda) yoki 'finished' (tugagan)
        c.execute("ALTER TABLE animes ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'ongoing'")

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
        # AI orqali spoiler deb aniqlangan izohlar — webapp'da "⚠️ Spoiler" deb
        # yashirilgan holda ko'rsatilishi mumkin (bosilganda ochiladi)
        c.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS is_spoiler INTEGER DEFAULT 0")

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
        # Alohida qismni ham doimiy Premium-only qilish mumkin (anime umumiy premium bo'lmasa ham)
        c.execute("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS is_premium_only INTEGER DEFAULT 0")

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

        # ===== Sayt foydalanuvchilari (anifilm.uz landing sahifasi uchun,
        # Telegramdan mustaqil ro'yxatdan o'tish/kirish: email yoki telefon + parol) =====
        c.execute("""
        CREATE TABLE IF NOT EXISTS site_users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE,
            phone TEXT UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )""")

        # Sayt foydalanuvchisining sevimlilari (Telegram favorites'dan alohida —
        # chunki bu yerdagi foydalanuvchi ID'lari Telegram user_id bilan bog'liq emas)
        c.execute("""
        CREATE TABLE IF NOT EXISTS site_favorites (
            site_user_id INTEGER NOT NULL REFERENCES site_users(id) ON DELETE CASCADE,
            anime_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY (site_user_id, anime_id)
        )""")

        # Parolni tiklash uchun bir martalik tokenlar (faqat email bilan ro'yxatdan
        # o'tgan sayt foydalanuvchilari uchun — SMTP orqali yuboriladi).
        c.execute("""
        CREATE TABLE IF NOT EXISTS site_password_resets (
            token TEXT PRIMARY KEY,
            site_user_id INTEGER NOT NULL REFERENCES site_users(id) ON DELETE CASCADE,
            created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')),
            used INTEGER DEFAULT 0
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_site_pwreset_user ON site_password_resets(site_user_id)")

        # ===== QO'SHIMCHA ADMINLAR (asosiy ADMIN_ID'dan tashqari) =====
        c.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            added_by BIGINT,
            added_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )""")
        # Admin darajalari: 'moderator' (kontent/jamoat — anime/qism qo'shish,
        # tahrirlash, banner, izohlar moderatsiyasi, foydalanuvchi bloklash),
        # 'moliya' (to'lovlarni tasdiqlash/rad etish, Premium narxlari/sovg'a,
        # daromad statistikasi), 'super' (hammasi, shu jumladan admin
        # boshqaruvi, xabar yuborish, texnik sozlamalar, DB backup).
        # DEFAULT 'super' — bu ustun qo'shilishidan OLDIN qo'shilgan barcha
        # adminlar hech narsa yo'qotmasligi uchun (ular ilgari to'liq huquqli
        # edi). Yangi qo'shiladigan adminlarga rolni add_admin() chaqiruvchi
        # tomon aniq beradi (pastga qarang, standart 'moderator').
        c.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'super'")

        # ===== ADMIN FAOLIYATI LOGI =====
        c.execute("""
        CREATE TABLE IF NOT EXISTS admin_logs (
            id SERIAL PRIMARY KEY,
            admin_id BIGINT,
            admin_name TEXT,
            action TEXT,
            details TEXT,
            created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_admin_logs_created ON admin_logs(created_at DESC)")

        # ===== PREMIUM SOVG'ALARI (kim kimga sovg'a qildi) =====
        c.execute("""
        CREATE TABLE IF NOT EXISTS premium_gifts (
            id SERIAL PRIMARY KEY,
            from_user_id BIGINT,
            to_user_id BIGINT,
            plan TEXT,
            days INTEGER,
            created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )""")

        # Sayt foydalanuvchisi ko'rgan animelar tarixi ("Ko'rilganlar")
        c.execute("""
        CREATE TABLE IF NOT EXISTS site_history (
            site_user_id INTEGER NOT NULL REFERENCES site_users(id) ON DELETE CASCADE,
            anime_id INTEGER NOT NULL,
            viewed_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY (site_user_id, anime_id)
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

        # ===== ANIME OBUNALARI (foydalanuvchi "🔔 Xabardor qil" bossa, shu anime
        # bo'yicha yangi qism/jonli efir boshlanganda shaxsiy xabar oladi) =====
        c.execute("""
        CREATE TABLE IF NOT EXISTS anime_subscriptions (
            user_id BIGINT NOT NULL,
            anime_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY (user_id, anime_id)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_anime_subs_anime_id ON anime_subscriptions(anime_id)")

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

        # AI suhbat tarixi (ask_ai uchun) — foydalanuvchi bilan oldingi
        # xabarlarni saqlab, kontekstli suhbat berish uchun. Har foydalanuvchi
        # uchun faqat oxirgi bir necha xabar saqlanadi (eskilari tozalanadi).
        c.execute("""
        CREATE TABLE IF NOT EXISTS ai_chat_history (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ai_chat_history_user ON ai_chat_history(user_id, id DESC)")

        # AI'ga yozilgan savollarning DOIMIY jurnali (admin ko'rib chiqishi
        # uchun, botni yaxshilash maqsadida) — yuqoridagi ai_chat_history'dan
        # farqli o'laroq, bu yerda eski yozuvlar hech qachon tozalanmaydi.
        c.execute("""
        CREATE TABLE IF NOT EXISTS ai_questions_log (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ai_questions_log_id ON ai_questions_log(id DESC)")

        # Webapp 🔔 bildirishnoma paneli — yangi qism/anime va admin e'lonlari
        # shu jadvalga yoziladi, webapp uni /api/notifications orqali o'qiydi.
        c.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            ntype TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            anime_id INTEGER,
            created_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_notifications_id_desc ON notifications(id DESC)")

        # Foydalanuvchi bitta bildirishnomani "o'qildi" deb belgilab, oʻz roʻyxatidan
        # yashirishi mumkin (masalan svayp/× tugmasi orqali). Bildirishnomalar jadvali
        # barcha foydalanuvchilar uchun umumiy boʻlgani sabab, bu yerda faqat shu
        # foydalanuvchi uchun "yashirilgan" deb belgilanadi — boshqalarga taʼsir qilmaydi.
        c.execute("""
        CREATE TABLE IF NOT EXISTS notification_dismissals (
            user_id BIGINT NOT NULL,
            notification_id INTEGER NOT NULL,
            dismissed_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')),
            PRIMARY KEY (user_id, notification_id)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_notif_dismissals_user ON notification_dismissals(user_id)")

        # --- Tezlik uchun indekslar (mavjud ma'lumot yoki xulq-atvorni o'zgartirmaydi,
        # faqat qidiruv/filtrlashni tezlashtiradi) ---
        c.execute("CREATE INDEX IF NOT EXISTS idx_animes_media_type ON animes(media_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_animes_id_desc ON animes(id DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_favorites_user_id ON favorites(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_watch_positions_user_id ON watch_positions(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_watch_activity_user_id ON watch_activity(user_id)")
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

        # FSM holati (ro'yxatdan o'tish, admin oqimlari, AI suhbat jarayoni va
        # h.k.) — ilgari faqat RAM'da (aiogram MemoryStorage) saqlanardi va
        # Render qayta ishga tushganda butunlay yo'qolardi. Endi shu jadvalda
        # saqlanadi (pastga, "FSM STORAGE" bo'limiga qarang), shu bilan restart
        # bo'lsa ham foydalanuvchi jarayoni uzilib qolmaydi.
        c.execute("""
        CREATE TABLE IF NOT EXISTS fsm_storage (
            storage_key TEXT PRIMARY KEY,
            state TEXT,
            data TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        )""")

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
        # Eskirgan sozlama: Profil bo'limini bepul foydalanuvchilar uchun yopish
        # funksiyasi olib tashlandi — allaqachon deploy qilingan bazalarda qolib
        # ketgan qatorni tozalaymiz.
        c.execute("DELETE FROM settings WHERE key = 'profile_disabled_for_free'")

        conn.commit()
    finally:
        put_conn(conn)

# ===== FSM STORAGE (aiogram holati; klass o'zi anime_bot.py'da — PostgresStorage) =====
def fsm_get_state(storage_key):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT state FROM fsm_storage WHERE storage_key=%s", (storage_key,))
        row = c.fetchone()
        return row[0] if row else None
    finally:
        put_conn(conn)

def fsm_set_state(storage_key, state):
    conn = get_conn()
    try:
        c = conn.cursor()
        try:
            c.execute("""
                INSERT INTO fsm_storage (storage_key, state, updated_at)
                VALUES (%s, %s, to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
                ON CONFLICT (storage_key) DO UPDATE SET
                    state = EXCLUDED.state, updated_at = EXCLUDED.updated_at
            """, (storage_key, state))
            conn.commit()
        except psycopg2.Error as e:
            conn.rollback()
            logger.error(f"fsm_set_state({storage_key}): xato: {e}")
    finally:
        put_conn(conn)

def fsm_get_data(storage_key):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT data FROM fsm_storage WHERE storage_key=%s", (storage_key,))
        row = c.fetchone()
        if not row or not row[0]:
            return {}
        try:
            return json.loads(row[0])
        except Exception:
            return {}
    finally:
        put_conn(conn)

def fsm_set_data(storage_key, data):
    payload = json.dumps(data)
    conn = get_conn()
    try:
        c = conn.cursor()
        try:
            c.execute("""
                INSERT INTO fsm_storage (storage_key, data, updated_at)
                VALUES (%s, %s, to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
                ON CONFLICT (storage_key) DO UPDATE SET
                    data = EXCLUDED.data, updated_at = EXCLUDED.updated_at
            """, (storage_key, payload))
            conn.commit()
        except psycopg2.Error as e:
            conn.rollback()
            logger.error(f"fsm_set_data({storage_key}): xato: {e}")
    finally:
        put_conn(conn)

def cleanup_stale_fsm_rows(older_than_days=3):
    """Holati bo'sh (None) va uzoq vaqtdan beri yangilanmagan FSM qatorlarini
    o'chiradi (masalan foydalanuvchi bir jarayonni tugatib/tashlab ketgach) —
    aks holda jadval asossiz o'sib boradi. Faol holati bor qatorlarga
    tegilmaydi, qancha eski bo'lishidan qat'iy nazar."""
    cutoff = (now_tz() - timedelta(days=older_than_days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    try:
        c = conn.cursor()
        try:
            c.execute(
                "DELETE FROM fsm_storage WHERE state IS NULL AND updated_at < %s",
                (cutoff,)
            )
            conn.commit()
            deleted = c.rowcount
        except psycopg2.Error as e:
            conn.rollback()
            logger.error(f"cleanup_stale_fsm_rows: xato: {e}")
            deleted = 0
        return deleted
    finally:
        put_conn(conn)

# ===== FOYDALANUVCHILAR =====
def add_user(user_id, username, full_name, phone=None, referred_by=None):
    conn = get_conn()
    try:
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
        except psycopg2.Error as e:
            conn.rollback()  # muhim: aks holda pool'ga qaytgan ulanish "aborted transaction" holatida qolib, keyingi so'rovlar ham xato beradi
            logger.error(f"add_user({user_id}): foydalanuvchi qo'shishda xato: {e}")
            inserted = False
        return inserted
    finally:
        put_conn(conn)

def get_user(user_id):
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)

def get_user_by_username(username):
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        username = username.lstrip('@')
        c.execute("SELECT * FROM users WHERE username=%s", (username,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)

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
    now = now_tz()
    if until <= now:
        set_premium(user_id, None, active=False)
        return {"is_premium": False, "until": None, "days_left": 0, "plan": None}
    days_left = max(1, (until - now).days + (1 if (until - now).seconds > 0 else 0))
    return {"is_premium": True, "until": u["premium_until"], "days_left": days_left, "plan": u.get("premium_plan")}

def set_premium(user_id, until_dt, plan=None, active=True):
    """until_dt — datetime obyekti yoki None. active=False qilib o'chirib qo'yish uchun ham ishlatiladi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        if active and until_dt:
            c.execute(
                "UPDATE users SET is_premium=1, premium_until=%s, premium_plan=%s, premium_renew_notified=0 WHERE user_id=%s",
                (until_dt.strftime("%Y-%m-%d %H:%M:%S"), plan, user_id)
            )
        else:
            c.execute("UPDATE users SET is_premium=0 WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        put_conn(conn)

def extend_premium(user_id, days, plan=None):
    """Mavjud muddatga kun qo'shadi (agar hali faol bo'lsa), aks holda hozirdan boshlab hisoblaydi."""
    status = get_premium_status(user_id)
    base = datetime.strptime(status["until"], "%Y-%m-%d %H:%M:%S") if status["is_premium"] else now_tz()
    new_until = base + timedelta(days=days)
    set_premium(user_id, new_until, plan or status.get("plan"))
    return new_until

def get_expiring_premium_users(within_days=2):
    """Muddati within_days ichida tugaydigan, hali ogohlantirilmagan foydalanuvchilar."""
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        cutoff = (now_tz() + timedelta(days=within_days)).strftime("%Y-%m-%d %H:%M:%S")
        now = now_tz().strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            """SELECT * FROM users WHERE is_premium=1 AND premium_until IS NOT NULL
               AND premium_until <= %s AND premium_until > %s AND premium_renew_notified=0""",
            (cutoff, now)
        )
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

def mark_renew_notified(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE users SET premium_renew_notified=1 WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        put_conn(conn)

def expire_premiums():
    """Muddati o'tgan barcha Premium'larni o'chiradi. Kunlik fon vazifasi uchun."""
    conn = get_conn()
    try:
        c = conn.cursor()
        now = now_tz().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("UPDATE users SET is_premium=0 WHERE is_premium=1 AND premium_until IS NOT NULL AND premium_until <= %s", (now,))
        affected = c.rowcount
        conn.commit()
        return affected
    finally:
        put_conn(conn)

# ---- Premium to'lov so'rovlari ----
def create_payment_request(user_id, plan, amount, screenshot_file_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO premium_payments (user_id, plan, amount, screenshot_file_id) VALUES (%s,%s,%s,%s) RETURNING id",
            (user_id, plan, amount, screenshot_file_id)
        )
        new_id = c.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        put_conn(conn)

def get_payment_request(payment_id):
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM premium_payments WHERE id=%s", (payment_id,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)

def get_pending_payments():
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM premium_payments WHERE status='pending' ORDER BY id DESC")
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

def set_payment_status(payment_id, status):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE premium_payments SET status=%s, processed_at=to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS') WHERE id=%s",
            (status, payment_id)
        )
        conn.commit()
    finally:
        put_conn(conn)

def try_claim_pending_payment(payment_id, new_status):
    """set_payment_status'dan farqli o'laroq, "hali 'pending'mi?" tekshiruvi va
    holatni o'zgartirish BITTA atomik so'rovda bajariladi (WHERE status='pending').
    Shu bilan admin "✅ Tasdiqlash" tugmasini tasodifan/tez-tez ikki marta bossa
    yoki ikkita callback deyarli bir vaqtda kelib qolsa ham, Premium faqat BIR
    marta beriladi — chunki UPDATE faqat hali chindan ham 'pending' bo'lgan
    qatorga tegadi, shu bazaviy operatsiya PostgreSQL darajasida qulflanadi.
    Muvaffaqiyatli bo'lsa yangilangan qatorni, aks holda (allaqachon boshqa
    holatga o'tgan bo'lsa) None qaytaradi."""
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute(
            "UPDATE premium_payments SET status=%s, processed_at=to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS') "
            "WHERE id=%s AND status='pending' RETURNING *",
            (new_status, payment_id)
        )
        row = c.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        put_conn(conn)

def set_payment_amount(payment_id, amount):
    """Admin chekni ko'rib, haqiqatda tushgan summa so'ralgandan kam/ko'p bo'lsa,
    to'lov yozuvidagi summani shu haqiqiy summaga to'g'irlaydi (masalan 10 000 so'm
    kerak bo'lib, foydalanuvchi 7 000 so'm yuborgan bo'lsa)."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE premium_payments SET amount=%s WHERE id=%s", (amount, payment_id))
        conn.commit()
    finally:
        put_conn(conn)

def clear_all_payments():
    """BARCHA to'lov yozuvlarini (test yoki eski ma'lumotlarni) o'chiradi —
    Daromad statistikasini nolga qaytaradi. Foydalanuvchilarning aktiv
    Premium muddatiga TA'SIR QILMAYDI (bu faqat to'lov tarixi jadvali,
    users jadvalidagi premium_until'ga tegmaydi). QAYTARIB BO'LMAYDI —
    admin panelda tasdiqlashdan keyin chaqirilishi kerak."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM premium_payments")
        affected = c.rowcount
        conn.commit()
        return affected
    finally:
        put_conn(conn)

# ---- Referral bonus ----
def get_revenue_stats():
    """Tasdiqlangan (approved) to'lovlar bo'yicha daromad statistikasi: umumiy summa,
    reja bo'yicha taqsimot (1m/3m/1y — soni va summasi) va oxirgi 6 oylik oylik tushum."""
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)

        c.execute("SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS cnt FROM premium_payments WHERE status='approved'")
        totals = dict(c.fetchone())

        c.execute("""
            SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS cnt
            FROM premium_payments
            WHERE status='approved' AND created_at >= to_char(NOW(), 'YYYY-MM-DD')
        """)
        today = dict(c.fetchone())

        c.execute("""
            SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS cnt
            FROM premium_payments
            WHERE status='approved' AND created_at >= to_char(NOW() - INTERVAL '30 days', 'YYYY-MM-DD')
        """)
        last30 = dict(c.fetchone())

        c.execute("""
            SELECT plan, COALESCE(SUM(amount),0) AS total, COUNT(*) AS cnt
            FROM premium_payments
            WHERE status='approved'
            GROUP BY plan
        """)
        by_plan = {r["plan"]: {"total": r["total"], "cnt": r["cnt"]} for r in c.fetchall()}

        c.execute("""
            SELECT to_char(to_date(created_at, 'YYYY-MM-DD HH24:MI:SS'), 'YYYY-MM') AS month,
                   COALESCE(SUM(amount),0) AS total,
                   COUNT(*) AS cnt
            FROM premium_payments
            WHERE status='approved'
            GROUP BY month
            ORDER BY month DESC
            LIMIT 6
        """)
        by_month = [dict(r) for r in c.fetchall()]
        by_month.reverse()

        return {
            "total": totals["total"], "total_cnt": totals["cnt"],
            "today": today["total"], "today_cnt": today["cnt"],
            "last30": last30["total"], "last30_cnt": last30["cnt"],
            "by_plan": by_plan,
            "by_month": by_month,
        }
    finally:
        put_conn(conn)

def process_referral_bonus(referrer_id, bonus_days, referred_user_id=None):
    """Yangi foydalanuvchi kimningdir taklifi bilan kelganda, taklif qilgan odamga bonus kun qo'shadi.
    referred_user_id berilsa, taklif qilingan foydalanuvchining o'z yozuviga ham
    'unga necha kun bonus berilgani' saqlanadi (referral_bonus_days) — bu, agar
    o'sha foydalanuvchi keyinchalik botni tark etsa, revoke_referral_bonus() aynan
    shu qadar kunni taklif qilgan odamdan qaytarib olishi uchun kerak."""
    if not referrer_id:
        return
    referrer = get_user(referrer_id)
    if not referrer:
        return
    extend_premium(referrer_id, bonus_days, plan=referrer.get("premium_plan") or "referral")
    if referred_user_id:
        conn = get_conn()
        try:
            c = conn.cursor()
            c.execute("UPDATE users SET referral_bonus_days=%s WHERE user_id=%s", (bonus_days, referred_user_id))
            conn.commit()
        finally:
            put_conn(conn)

def revoke_referral_bonus(user_id):
    """Taklif qilingan foydalanuvchi botni bloklab/tark etganda chaqiriladi.
    Agar bu foydalanuvchi kimningdir referal havolasi orqali qo'shilgan bo'lsa va
    o'sha taklif uchun bonus kun berilgan bo'lsa (referral_bonus_days > 0) —
    aynan o'sha kunlarni taklif qilgan odamning Premium muddatidan ayirib
    tashlaydi (soxta/bir martalik akkountlar bilan bonus "termash"ning oldini
    olish uchun). Har bir bonus faqat BIR MARTA qaytarib olinadi — shu sabab
    ayirib bo'lgach referral_bonus_days darhol 0 ga tushiriladi, shu bilan
    funksiya bir xil foydalanuvchi uchun tasodifan qayta chaqirilsa ham ikkinchi
    marta ayirib qo'ymaydi. Haqiqatda qaytarib olingan kunlar sonini qaytaradi
    (hech narsa qaytarilmagan bo'lsa 0 — masalan referal orqali kelmagan yoki
    bonusi allaqachon qaytarib olingan foydalanuvchi uchun)."""
    u = get_user(user_id)
    if not u:
        return 0
    referrer_id = u.get("referred_by")
    bonus_days = u.get("referral_bonus_days") or 0
    if not referrer_id or bonus_days <= 0:
        return 0
    extend_premium(referrer_id, -bonus_days)
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE users SET referral_bonus_days=0 WHERE user_id=%s", (user_id,))
        conn.commit()
        return bonus_days
    finally:
        put_conn(conn)

def block_user(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE users SET is_blocked=1, is_active=0 WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        put_conn(conn)

def unblock_user(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE users SET is_blocked=0, is_active=1 WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        put_conn(conn)

def set_user_inactive(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE users SET is_active=0, is_blocked=1 WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        put_conn(conn)

def set_user_left_only(user_id):
    """Botni bloklab/chiqib ketgan foydalanuvchini faqat NOFAOL qiladi —
    is_blocked'ga tegmaydi. 'Avtomatik bloklash' sozlamasi O'CHIQ bo'lganda
    ishlatiladi: foydalanuvchi qaytib kelsa (blokdan chiqarsa), qayta bloklanmagan
    holda botdan foydalana oladi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE users SET is_active=0 WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        put_conn(conn)

def update_user_info(user_id, username=None, full_name=None):
    """Telegramdan kelgan joriy ism/username bilan foydalanuvchi yozuvini
    yangilaydi ("Hisobni yangilash" tugmasi uchun)."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE users SET username=COALESCE(%s, username), full_name=COALESCE(%s, full_name) WHERE user_id=%s",
            (username, full_name, user_id),
        )
        conn.commit()
    finally:
        put_conn(conn)

def delete_user_data(user_id):
    """Foydalanuvchi "Hisobni o'chirish"ni tanlaganda chaqiriladi.

    To'lovlar/referral tarixini (audit uchun) saqlab qolamiz, lekin shaxsiy
    ma'lumotlarni (sevimlilar, tomosha tarixi, pozitsiyalar, izohlar matni,
    ism/username) tozalaymiz va hisobni bloklaymiz — u endi botdan
    foydalana olmaydi."""
    conn = get_conn()
    try:
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
    finally:
        put_conn(conn)

def get_all_active_users():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE is_active=1 AND is_blocked=0")
        rows = c.fetchall()
        return [r[0] for r in rows]
    finally:
        put_conn(conn)

def get_stats():
    # Ilgari bu yerda 9 ta ALOHIDA so'rov ketma-ket yuborilardi (har biri
    # oʻzining DB round-trip'iga ega, masofaviy Neon'da bu sekinlik sabab
    # boʻlardi — ayniqsa admin "Statistika" tugmasi). Endi FILTER (WHERE ...)
    # yordamida bitta so'rovda barcha users hisoblari, bitta so'rovda barcha
    # animes hisoblari olinadi — jami 9 emas, 3 ta round-trip (2 hisob + top-5).
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE is_active=1 AND is_blocked=0) AS active,
                COUNT(*) FILTER (WHERE is_blocked=1) AS blocked,
                COUNT(*) FILTER (WHERE date(joined_at)=CURRENT_DATE) AS today,
                COUNT(*) FILTER (WHERE date(joined_at)>=CURRENT_DATE - INTERVAL '7 days') AS week,
                COUNT(*) FILTER (WHERE date(joined_at)>=CURRENT_DATE - INTERVAL '30 days') AS month
            FROM users
        """)
        total, active, blocked, today, week, month = c.fetchone()

        c.execute("""
            SELECT
                COUNT(*) AS total_animes,
                COUNT(*) FILTER (WHERE media_type='film') AS films,
                COUNT(*) FILTER (WHERE media_type='serial') AS serials
            FROM animes
        """)
        total_animes, films, serials = c.fetchone()

        cd = psycopg2.extras.RealDictCursor(conn)
        cd.execute("SELECT id, title, views FROM animes ORDER BY views DESC LIMIT 5")
        top = [dict(r) for r in cd.fetchall()]
        return {
            "total": total, "active": active, "blocked": blocked,
            "total_animes": total_animes, "films": films, "serials": serials,
            "today": today, "week": week, "month": month, "top": top
        }
    finally:
        put_conn(conn)

def get_daily_stats():
    # 4 ta alohida so'rov o'rniga: users va animes bo'yicha bittadan
    # umumiy so'rov (ikkalasini bitta so'rovga birlashtirib bo'lmaydi,
    # chunki jadvallar boshqa, lekin 4 dan 2 ga tushirildi).
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT
                COUNT(*) FILTER (WHERE date(joined_at)=CURRENT_DATE) AS new_users,
                COUNT(*) FILTER (WHERE date(joined_at)=CURRENT_DATE AND is_blocked=1) AS left_users
            FROM users
        """)
        new_users, left_users = c.fetchone()

        c.execute("""
            SELECT
                COUNT(*) FILTER (WHERE date(created_at)=CURRENT_DATE) AS new_animes,
                COALESCE(SUM(views), 0) AS total_views
            FROM animes
        """)
        new_animes, total_views = c.fetchone()

        return {
            "new_users": new_users,
            "left_users": left_users,
            "new_animes": new_animes,
            "total_views": total_views
        }
    finally:
        put_conn(conn)

# ===== ANIMLAR =====
def add_anime(title, year, country, genre, description, language, photo_id, media_type, total_episodes=None, status="ongoing"):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""INSERT INTO animes (title, year, country, genre, description, language, photo_id, media_type, total_episodes, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (title, year, country, genre, description, language, photo_id, media_type, total_episodes, status))
        anime_id = c.fetchone()[0]
        conn.commit()
        _invalidate_animes_cache()
        return anime_id
    finally:
        put_conn(conn)

def get_animes(media_type=None, page=0, per_page=10):
    conn = get_conn()
    try:
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
        return rows
    finally:
        put_conn(conn)

def get_anime_count(media_type=None):
    conn = get_conn()
    try:
        c = conn.cursor()
        if media_type:
            c.execute("SELECT COUNT(*) FROM animes WHERE media_type=%s", (media_type,))
        else:
            c.execute("SELECT COUNT(*) FROM animes")
        count = c.fetchone()[0]
        return count
    finally:
        put_conn(conn)

def get_anime(anime_id):
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM animes WHERE id=%s", (anime_id,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)

def search_anime(query):
    """Anime qidiradi uch bosqichda:
    1) Agar so'rov faqat raqamlardan iborat bo'lsa — bu 'kod' (animening DB id'si)
       deb qaraladi va aynan o'sha anime birinchi o'ringa chiqariladi.
    2) Sarlavha ICHIDA qism sifatida uchraydigan mos kelishlar (substring, katta-
       kichik harflarga befarq) — masalan 'naru' -> 'Naruto'.
    3) Agar aniq/qismli mos kelish kam yoki umuman topilmasa — yozuv xatolariga
       chidamli (fuzzy) qidiruv orqali eng o'xshash nomlar ham qo'shiladi
       (masalan 'narito' -> 'Naruto').
    Natijalar: aniq kod mosligi -> substring mosligi -> fuzzy mosligi tartibida,
    dublikatsiz qaytariladi."""
    import difflib

    query = (query or "").strip()
    if not query:
        return []

    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)

        results = []
        seen_ids = set()

        def _add(rows):
            for r in rows:
                d = dict(r)
                if d["id"] not in seen_ids:
                    seen_ids.add(d["id"])
                    results.append(d)

        # 1) Kod (ID) bo'yicha qidiruv — masalan foydalanuvchi "106" deb yozsa
        if query.isdigit():
            c.execute("SELECT * FROM animes WHERE id=%s", (int(query),))
            _add(c.fetchall())

        # 2) Sarlavha ichida qism sifatida uchrashi (substring)
        escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        c.execute(
            "SELECT * FROM animes WHERE title ILIKE %s ESCAPE '\\' ORDER BY views DESC LIMIT 30",
            ('%' + escaped + '%',)
        )
        _add(c.fetchall())

        # 3) Fuzzy (o'xshashlik) qidiruv — agar natija kam bo'lsa yoki umuman
        # aniq mos kelish topilmasa, yozuv xatolariga chidamli qidiruvni ham qo'shamiz
        if len(results) < 10:
            c.execute("SELECT * FROM animes")
            all_rows = [dict(r) for r in c.fetchall()]
            q_low = query.lower()
            scored = []
            for r in all_rows:
                if r["id"] in seen_ids:
                    continue
                title_low = (r.get("title") or "").lower()
                ratio = difflib.SequenceMatcher(None, q_low, title_low).ratio()
                # so'z darajasida ham tekshiramiz, chunki uzun sarlavhalarda
                # butun matnni solishtirish o'xshashlikni pasaytirib yuboradi
                best_word_ratio = 0.0
                for word in title_low.split():
                    wr = difflib.SequenceMatcher(None, q_low, word).ratio()
                    if wr > best_word_ratio:
                        best_word_ratio = wr
                score = max(ratio, best_word_ratio)
                if score >= 0.6:
                    scored.append((score, r))
            scored.sort(key=lambda x: x[0], reverse=True)
            _add([r for _, r in scored[:20]])

        return results[:30]
    finally:
        put_conn(conn)

def delete_anime(anime_id):
    """Animeni va u bilan bog'liq BARCHA ma'lumotlarni o'chiradi. Ilgari faqat
    episodes+animes o'chirilar, qolgan jadvallarda (favorites, watch_log,
    watch_activity, comments/comment_likes, anime_subscriptions, banners,
    notifications, site_favorites, site_history, watch_positions) o'sha
    anime_id'ga ishora qiluvchi "yetim" qatorlar abadiy qolib ketardi —
    masalan o'chirilgan animega ishora qiluvchi banner yoki bildirishnoma
    bosilganda xatolikka olib kelardi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        # watch_positions episode_id orqali bog'langan — anime o'chirilishidan
        # oldin shu anime'ning barcha qism ID'larini olib qo'yamiz.
        c.execute("SELECT id FROM episodes WHERE anime_id=%s", (anime_id,))
        episode_ids = [r[0] for r in c.fetchall()]
        if episode_ids:
            c.execute("DELETE FROM watch_positions WHERE episode_id=ANY(%s)", (episode_ids,))
        c.execute("DELETE FROM comment_likes WHERE comment_id IN (SELECT id FROM comments WHERE anime_id=%s)", (anime_id,))
        c.execute("DELETE FROM comments WHERE anime_id=%s", (anime_id,))
        c.execute("DELETE FROM favorites WHERE anime_id=%s", (anime_id,))
        c.execute("DELETE FROM watch_log WHERE anime_id=%s", (anime_id,))
        c.execute("DELETE FROM watch_activity WHERE anime_id=%s", (anime_id,))
        c.execute("DELETE FROM anime_subscriptions WHERE anime_id=%s", (anime_id,))
        c.execute("DELETE FROM banners WHERE anime_id=%s", (anime_id,))
        c.execute("DELETE FROM notifications WHERE anime_id=%s", (anime_id,))
        c.execute("DELETE FROM site_favorites WHERE anime_id=%s", (anime_id,))
        c.execute("DELETE FROM site_history WHERE anime_id=%s", (anime_id,))
        c.execute("DELETE FROM episodes WHERE anime_id=%s", (anime_id,))
        c.execute("DELETE FROM animes WHERE id=%s", (anime_id,))
        conn.commit()
        _invalidate_animes_cache()
    finally:
        put_conn(conn)

# update_anime orqali oʻzgartirish mumkin boʻlgan ustunlar roʻyxati.
# Bu SQL Injection'dan himoya qiladi — faqat shu roʻyxatdagi
# ustun nomlariga yozish ruxsat etiladi.
ALLOWED_ANIME_FIELDS = {
    "title", "year", "country", "genre",
    "description", "language", "photo_id", "media_type", "category",
    "total_episodes", "status",
}

def update_anime(anime_id, field, value):
    if field not in ALLOWED_ANIME_FIELDS:
        raise ValueError(f"Notogri maydon nomi: {field}")
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(f"UPDATE animes SET {field}=%s WHERE id=%s", (value, anime_id))
        conn.commit()
        _invalidate_animes_cache()
    finally:
        put_conn(conn)

def finish_all_animes():
    """Bir martalik tuzatish: barcha animelarni 'Tugagan' (finished) holatiga
    o'tkazadi. Nechta qator o'zgartirilganini qaytaradi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE animes SET status='finished' WHERE status IS DISTINCT FROM 'finished'")
        updated = c.rowcount
        conn.commit()
        _invalidate_animes_cache()
        return updated
    finally:
        put_conn(conn)

def increment_views(anime_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE animes SET views=views+1 WHERE id=%s", (anime_id,))
        conn.commit()
    finally:
        put_conn(conn)

def set_anime_premium_only(anime_id, is_premium_only: bool):
    """Butun animeni (barcha qismlarini) doimiy Premium-only qilib belgilaydi/bekor qiladi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE animes SET is_premium_only=%s WHERE id=%s", (1 if is_premium_only else 0, anime_id))
        conn.commit()
        _invalidate_animes_cache()
    finally:
        put_conn(conn)

def get_random_anime():
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM animes ORDER BY RANDOM() LIMIT 1")
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)

# ===== QISMLAR =====
def add_episode(anime_id, episode_number, channel_message_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO episodes (anime_id, episode_number, channel_message_id) VALUES (%s, %s, %s) RETURNING id",
                  (anime_id, episode_number, channel_message_id))
        new_id = c.fetchone()[0]
        conn.commit()
        _invalidate_animes_cache()
        return new_id
    finally:
        put_conn(conn)

def get_episodes(anime_id):
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM episodes WHERE anime_id=%s ORDER BY episode_number", (anime_id,))
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

def delete_episode(episode_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        # Xuddi delete_anime'dagidek — watch_positions shu episode_id'ga bog'liq
        # bo'lgani uchun avval tozalanadi, aks holda yetim qator qolib ketadi.
        c.execute("DELETE FROM watch_positions WHERE episode_id=%s", (episode_id,))
        c.execute("DELETE FROM episodes WHERE id=%s", (episode_id,))
        conn.commit()
        _invalidate_animes_cache()
    finally:
        put_conn(conn)

def get_episode(episode_id):
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM episodes WHERE id=%s", (episode_id,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)

def set_episode_premium_only(episode_id, is_premium_only: bool):
    """Alohida qismni doimiy Premium-only qilib belgilaydi/bekor qiladi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE episodes SET is_premium_only=%s WHERE id=%s", (1 if is_premium_only else 0, episode_id))
        conn.commit()
        _invalidate_animes_cache()
    finally:
        put_conn(conn)

def update_episode(episode_id, channel_message_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE episodes SET channel_message_id=%s WHERE id=%s", (channel_message_id, episode_id))
        conn.commit()
        _invalidate_animes_cache()
    finally:
        put_conn(conn)

def get_all_episodes_with_anime():
    """Barcha qismlarni tegishli anime nomi bilan birga qaytaradi (video havolalarini
    ommaviy tekshirish uchun — anime nomi/qism raqami darrov ko'rinishi uchun)."""
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("""
            SELECT e.id, e.anime_id, e.episode_number, e.channel_message_id, a.title AS anime_title
            FROM episodes e
            JOIN animes a ON a.id = e.anime_id
            ORDER BY a.title, e.episode_number
        """)
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

def get_watch_position(user_id, episode_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT position_seconds FROM watch_positions WHERE user_id=%s AND episode_id=%s", (user_id, episode_id))
        row = c.fetchone()
        return row[0] if row else 0
    finally:
        put_conn(conn)

def set_watch_position(user_id, episode_id, seconds):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO watch_positions (user_id, episode_id, position_seconds, updated_at)
            VALUES (%s, %s, %s, to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT (user_id, episode_id)
            DO UPDATE SET position_seconds=EXCLUDED.position_seconds, updated_at=EXCLUDED.updated_at
        """, (user_id, episode_id, int(seconds)))
        conn.commit()
    finally:
        put_conn(conn)

def toggle_favorite(user_id, anime_id):
    """Sevimlilarga qo'shadi/olib tashlaydi. Yangi holatni (True=sevimli) qaytaradi."""
    conn = get_conn()
    try:
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
        return active
    finally:
        put_conn(conn)

def get_favorite_ids(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT anime_id FROM favorites WHERE user_id=%s ORDER BY created_at DESC", (user_id,))
        ids = [r[0] for r in c.fetchall()]
        return ids
    finally:
        put_conn(conn)

def get_favorite_titles(user_id, limit=3):
    """Foydalanuvchining eng oxirgi qo'shgan sevimli animelari nomlari —
    AI'ga shaxsiylashtirilgan xabar (masalan 'qaytib kel') yozdirish uchun
    kontekst sifatida ishlatiladi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT a.title FROM favorites f
            JOIN animes a ON a.id = f.anime_id
            WHERE f.user_id=%s
            ORDER BY f.created_at DESC
            LIMIT %s
            """,
            (user_id, limit)
        )
        titles = [r[0] for r in c.fetchall()]
        return titles
    finally:
        put_conn(conn)

def get_active_non_premium_users(days=7, limit=150):
    """So'nggi `days` kun ichida tomosha faolligi (watch_activity) qayd
    etilgan, hali Premium bo'lmagan (yoki muddati o'tgan) foydalanuvchilarni
    qaytaradi — AI Premium-taklif xabari uchun. Eng yaqinda faol bo'lganlar
    birinchi bo'lib qaytariladi (eng "issiq" auditoriya)."""
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute(
            """
            SELECT u.user_id, u.username, u.full_name,
                   MAX(wa.activity_date) AS last_seen
            FROM users u
            JOIN watch_activity wa ON wa.user_id = u.user_id
            WHERE u.is_active=1 AND u.is_blocked=0
              AND (u.is_premium=0 OR u.is_premium IS NULL
                   OR u.premium_until IS NULL OR u.premium_until < to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
            GROUP BY u.user_id, u.username, u.full_name
            HAVING MAX(wa.activity_date) >= (CURRENT_DATE - %s::int)
            ORDER BY last_seen DESC
            LIMIT %s
            """,
            (days, limit)
        )
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

def get_inactive_users(days, limit=200):
    """Kamida `days` kundan beri hech qanday tomosha faolligi (watch_activity)
    qayd etilmagan, hali bloklanmagan/tark etmagan foydalanuvchilarni
    qaytaradi. Faollik bo'lmagan userlar uchun ro'yxatdan o'tgan sana
    (joined_at) oxirgi faollik sifatida hisoblanadi. Eng uzoq vaqt
    ko'rinmaganlar birinchi bo'lib qaytariladi."""
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute(
            """
            SELECT u.user_id, u.username, u.full_name,
                   COALESCE(MAX(wa.activity_date), u.joined_at::date) AS last_seen
            FROM users u
            LEFT JOIN watch_activity wa ON wa.user_id = u.user_id
            WHERE u.is_active=1 AND u.is_blocked=0
            GROUP BY u.user_id, u.username, u.full_name, u.joined_at
            HAVING COALESCE(MAX(wa.activity_date), u.joined_at::date) <= (CURRENT_DATE - %s::int)
            ORDER BY last_seen ASC
            LIMIT %s
            """,
            (days, limit)
        )
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

def record_watch_activity(user_id, anime_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO watch_activity (user_id, activity_date, anime_id) VALUES (%s, CURRENT_DATE, %s) ON CONFLICT DO NOTHING",
            (user_id, anime_id)
        )
        conn.commit()
    finally:
        put_conn(conn)

def get_profile_stats(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM favorites WHERE user_id=%s", (user_id,))
        favorites = c.fetchone()[0]
        c.execute("SELECT COUNT(DISTINCT anime_id) FROM watch_activity WHERE user_id=%s", (user_id,))
        watched = c.fetchone()[0]
        c.execute("SELECT COALESCE(SUM(position_seconds),0) FROM watch_positions WHERE user_id=%s", (user_id,))
        total_seconds = c.fetchone()[0]
        c.execute("SELECT DISTINCT activity_date FROM watch_activity WHERE user_id=%s ORDER BY activity_date DESC", (user_id,))
        dates = [r[0] for r in c.fetchall()]
        # Streak (ketma-ket kunlar) — bugun yoki kechadan boshlab hisoblanadi
        streak = 0
        if dates:
            today = now_tz().date()
            cur = today if dates[0] == today else (today - timedelta(days=1))
            date_set = set(dates)
            while cur in date_set:
                streak += 1
                cur = cur - timedelta(days=1)
        return {
            "favorites": favorites,
            "watched": watched,
            "watch_hours": round(total_seconds / 3600, 1),
            "watch_seconds": int(total_seconds),
            "streak": streak,
        }
    finally:
        put_conn(conn)

def clear_watch_history(user_id):
    """"Tarixni tozalash" tugmasi uchun: foydalanuvchining tomosha faoliyati
    (watch_activity) va pozitsiyalari (watch_positions) bazadan o'chiriladi.
    Natijada profildagi "Ko'rilgan"/"Tomosha vaqti"/"Davom etishda" statistikasi
    va "Oxirgi ko'rilganlar" ro'yxati 0/bo'sh holatga qaytadi.
    Sevimlilar (favorites) bu funksiyaga tegilmaydi — u alohida narsa."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM watch_positions WHERE user_id=%s", (user_id,))
        c.execute("DELETE FROM watch_activity WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        put_conn(conn)

def get_recent_anime_ids(user_id, limit=8):
    """Foydalanuvchi oxirgi marta tomosha qilgan animelar roʻyxati (eng soʻnggisi birinchi).

    MUHIM: bu avval `watch_positions` jadvaliga (episode_id orqali) asoslangan edi,
    lekin agar biror qism admin tomonidan oʻchirilib qayta yuklansa (episode_id
    oʻzgaradi), eski yozuv "yetim" boʻlib qolib, JOIN uni butunlay chetlab
    oʻtardi — natijada "Koʻrilgan"/"Davom etishda" statistikasi mavjud boʻlsa ham
    "Oxirgi koʻrilganlar" roʻyxati boʻsh koʻrinardi. Shuning uchun endi asosiy
    manba sifatida `watch_activity` (anime_id'ni toʻgʻridan-toʻgʻri, episode'ga
    bogʻliq boʻlmagan holda saqlaydi) ishlatiladi; `watch_positions` esa faqat
    bir xil kunda qaysi anime yangiroq ekanini aniqlashtirish uchun (best-effort)
    qoʻshimcha tartiblash mezoni sifatida ishlatiladi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT wa.anime_id,
                   MAX(wa.activity_date) AS last_date,
                   MAX(COALESCE(wp.updated_at, '')) AS last_time
            FROM watch_activity wa
            LEFT JOIN episodes e ON e.anime_id = wa.anime_id
            LEFT JOIN watch_positions wp ON wp.episode_id = e.id AND wp.user_id = wa.user_id
            WHERE wa.user_id=%s
            GROUP BY wa.anime_id
            ORDER BY last_date DESC, last_time DESC
            LIMIT %s
        """, (user_id, limit))
        ids = [r[0] for r in c.fetchall()]
        return ids
    finally:
        put_conn(conn)

def get_recent_watch_details(user_id, limit=8):
    """`get_recent_anime_ids` bilan bir xil roʻyxatni qaytaradi, lekin har bir
    anime uchun foydalanuvchi ENG OXIRGI tomosha qilgan qism raqamini ("N-qism"
    ko'rinishida frontendda chiqarish uchun) ham qo'shib beradi.

    `episode_number` — foydalanuvchi shu anime bo'yicha oxirgi pozitsiyasini
    saqlagan qismning raqami (agar mavjud bo'lsa), aks holda None."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            WITH last_pos AS (
                SELECT e.anime_id, e.episode_number, wp.updated_at,
                       ROW_NUMBER() OVER (PARTITION BY e.anime_id ORDER BY wp.updated_at DESC) AS rn
                FROM watch_positions wp
                JOIN episodes e ON e.id = wp.episode_id
                WHERE wp.user_id=%s
            )
            SELECT wa.anime_id,
                   lp.episode_number,
                   MAX(wa.activity_date) AS last_date,
                   MAX(COALESCE(lp.updated_at, '')) AS last_time
            FROM watch_activity wa
            LEFT JOIN last_pos lp ON lp.anime_id = wa.anime_id AND lp.rn = 1
            WHERE wa.user_id=%s
            GROUP BY wa.anime_id, lp.episode_number
            ORDER BY last_date DESC, last_time DESC
            LIMIT %s
        """, (user_id, user_id, limit))
        rows = c.fetchall()
        return [{"anime_id": r[0], "episode_number": r[1]} for r in rows]
    finally:
        put_conn(conn)

def unlock_anime_episodes(anime_id):
    """Bitta animening barcha qismlarini to'liq qulfdan chiqaradi:
    1) 'oldinroq kirish' vaqtinchalik qulfi — created_at'ni uzoq o'tmishga suradi
       (kelajakda shu animega YANGI qism qo'shilsa, u odatdagidek qayta qulflanadi).
    2) Doimiy 'Premium-only' belgisi — animening o'zida ham, uning barcha
       qismlarida ham is_premium_only=0 qilib tozalanadi (aks holda 'qulfdan
       chiqarish' tugmasi bosilsa ham anime hamon Premium-only bo'lib qolar edi)."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE episodes SET created_at='2000-01-01 00:00:00', is_premium_only=0 WHERE anime_id=%s", (anime_id,))
        affected = c.rowcount
        c.execute("UPDATE animes SET is_premium_only=0 WHERE id=%s", (anime_id,))
        conn.commit()
        _invalidate_animes_cache()
        return affected
    finally:
        put_conn(conn)

def unlock_all_old_episodes():
    """Bug tuzatish: bir vaqtlar migratsiya (ALTER TABLE ... DEFAULT NOW()) barcha eski
    qismlarning created_at ustunini o'sha migratsiya vaqtiga o'rnatib qo'ygan edi, natijada
    ular 'yangi qo'shilgan' deb hisoblanib, Premium 'oldinroq kirish' muddati davomida
    hammaga qulflanib qolgan. Bu funksiya barcha mavjud qismlarning created_at'ini uzoq
    o'tmishga suradi — shu bilan ular hech kimga qulflanmay qoladi. Bundan keyin YANGI
    qo'shiladigan qismlar odatdagidek haqiqiy vaqt bilan saqlanadi va Premium erta-kirish
    cheklovi ular uchun normal ishlayveradi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE episodes SET created_at='2000-01-01 00:00:00'")
        affected = c.rowcount
        conn.commit()
        return affected
    finally:
        put_conn(conn)

# ===== KANALLAR =====
def add_channel(channel_id, channel_name):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO channels (channel_id, channel_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                  (channel_id, channel_name))
        conn.commit()
    finally:
        put_conn(conn)

def delete_channel(channel_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM channels WHERE channel_id=%s", (channel_id,))
        conn.commit()
    finally:
        put_conn(conn)

def get_channels():
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM channels")
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

# ===== SOZLAMALAR =====
def get_setting(key):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key=%s", (key,))
        row = c.fetchone()
        return row[0] if row else None
    finally:
        put_conn(conn)

def get_settings(keys):
    """Bir nechta sozlamani BITTA so'rovda oladi — get_setting()ni bir necha
    marta ketma-ket chaqirib, ortiqcha DB round-trip qilmaslik uchun (masalan
    AI-yordamchiga bot haqida kontekst tayyorlashda bir nechta sozlama
    kerak bo'lganda). Natija: {key: value} lug'at — bazada topilmagan
    kalitlar lug'atda umuman bo'lmaydi (get_setting'dagi kabi None emas)."""
    if not keys:
        return {}
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT key, value FROM settings WHERE key = ANY(%s)", (list(keys),))
        rows = c.fetchall()
        return {k: v for k, v in rows}
    finally:
        put_conn(conn)

def set_setting(key, value):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=%s",
                  (key, value, value))
        conn.commit()
    finally:
        put_conn(conn)

def delete_setting(key):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM settings WHERE key=%s", (key,))
        conn.commit()
    finally:
        put_conn(conn)

def get_settings_prefix(prefix):
    """`prefix` bilan boshlanadigan barcha sozlamalarni {key: value} lug'at
    sifatida qaytaradi. Masalan faol klip-job'larni (clipjob_...) bot qayta
    ishga tushganda topib, ularni tozalash/xabarlarni yangilash uchun."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT key, value FROM settings WHERE key LIKE %s", (prefix + "%",))
        rows = c.fetchall()
        return {k: v for k, v in rows}
    finally:
        put_conn(conn)

# ===== WEBAPP UCHUN =====
# Webapp bosh sahifasi HAR SAFAR ochilganda shu funksiya chaqiriladi va u
# butun animes+episodes jadvalini JOIN qilib qayta hisoblardi — anime soni
# ko'paygan sari va foydalanuvchilar ko'p bo'lgan sari sekinlashuvning asosiy
# sababi shu edi. Endi natija qisqa muddat (10s) xotirada keshlanadi: shu
# oraliqda kelgan barcha so'rovlar DB'ga umuman bormaydi. Yangi anime/qism
# qo'shilsa/o'zgarsa/o'chirilsa kesh darhol bekor qilinadi (_invalidate_
# animes_cache), shuning uchun eskirgan ma'lumot ko'rsatilmaydi.
_ANIMES_WEBAPP_TTL = 10  # soniya
_animes_webapp_cache = {"data": None, "ts": 0.0}

def _invalidate_animes_cache():
    _animes_webapp_cache["data"] = None
    _animes_webapp_cache["ts"] = 0.0

def get_animes_for_webapp():
    now = time.time()
    cached = _animes_webapp_cache["data"]
    if cached is not None and (now - _animes_webapp_cache["ts"]) < _ANIMES_WEBAPP_TTL:
        return cached
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("""
            SELECT a.id, a.title, a.year, a.genre, a.category, a.description, a.photo_id,
                   a.media_type, a.views, a.total_episodes, a.is_premium_only,
                   COUNT(e.id) AS episode_count,
                   GREATEST(a.created_at, COALESCE(MAX(e.created_at), a.created_at)) AS last_activity_at
            FROM animes a
            LEFT JOIN episodes e ON e.anime_id = a.id
            GROUP BY a.id
            ORDER BY a.id DESC
        """)
        rows = [dict(r) for r in c.fetchall()]
        _animes_webapp_cache["data"] = rows
        _animes_webapp_cache["ts"] = now
        return rows
    finally:
        put_conn(conn)

def get_anime_detail_for_webapp(anime_id):
    anime = get_anime(anime_id)
    if not anime:
        return None
    anime["episodes"] = get_episodes(anime_id)
    anime["watched_today"] = get_watch_count_today(anime_id)
    return anime

def get_animes_by_ids(ids):
    """Berilgan id'lar ro'yxati bo'yicha animelarni (faqat ommaviy ko'rsatsa
    bo'ladigan maydonlar bilan) bitta so'rovda qaytaradi — Sevimlilar/Tarix
    bo'limlari uchun. Natija `ids` tartibida qaytariladi."""
    ids = [i for i in (ids or []) if i is not None]
    if not ids:
        return []
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("""
            SELECT a.id, a.title, a.year, a.genre, a.category, a.description, a.photo_id,
                   a.media_type, a.views, a.total_episodes,
                   COUNT(e.id) AS episode_count
            FROM animes a
            LEFT JOIN episodes e ON e.anime_id = a.id
            WHERE a.id = ANY(%s)
            GROUP BY a.id
        """, (ids,))
        by_id = {r["id"]: dict(r) for r in c.fetchall()}
        return [by_id[i] for i in ids if i in by_id]
    finally:
        put_conn(conn)

# ===== BANNERLAR =====
def add_banner(photo_id, title, subtitle, anime_id, position=0):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO banners (photo_id, title, subtitle, anime_id, position) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (photo_id, title, subtitle, anime_id, position)
        )
        new_id = c.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        put_conn(conn)

def get_banners(active_only=True):
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        if active_only:
            c.execute("SELECT * FROM banners WHERE is_active=1 ORDER BY position ASC, id DESC")
        else:
            c.execute("SELECT * FROM banners ORDER BY position ASC, id DESC")
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

def delete_banner(banner_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM banners WHERE id=%s", (banner_id,))
        conn.commit()
    finally:
        put_conn(conn)

def set_banner_active(banner_id, active):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE banners SET is_active=%s WHERE id=%s", (1 if active else 0, banner_id))
        conn.commit()
    finally:
        put_conn(conn)

# ===== KATEGORIYALAR =====
def get_categories():
    conn = get_conn()
    try:
        c = conn.cursor()
        # LOWER(TRIM(...)) bo'yicha guruhlanadi — shunda faqat katta-kichik harf
        # yoki bo'sh joy bilan farq qiladigan janrlar (masalan "Komediya" va
        # "komediya ") ro'yxatda ikki marta chiqmaydi. Har bir guruhdan bitta
        # (alifbo bo'yicha birinchi) yozuv tanlanadi.
        c.execute("""
            SELECT category FROM (
                SELECT DISTINCT ON (LOWER(TRIM(category))) TRIM(category) AS category
                FROM animes
                WHERE category IS NOT NULL AND TRIM(category) <> ''
                ORDER BY LOWER(TRIM(category)), category ASC
            ) t
            ORDER BY category ASC
        """)
        rows = [r[0] for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

# ===== IZOHLAR =====
def add_comment(anime_id, user_id, username, text, parent_id=None, is_spoiler=False):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO comments (anime_id, user_id, username, text, parent_id, is_spoiler) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (anime_id, user_id, username, text, parent_id, 1 if is_spoiler else 0)
        )
        new_id = c.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        put_conn(conn)

def get_comments(anime_id, limit=50, viewer_id=None):
    conn = get_conn()
    try:
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
        return rows
    finally:
        put_conn(conn)

def get_comment_trend_data(days=7, max_animes=15, max_comments_per_anime=12):
    """Oxirgi `days` kun ichida eng ko'p izoh olgan `max_animes` ta animeni
    va har biridan (eng yangi) `max_comments_per_anime` tagacha izoh
    matnini qaytaradi — AI'ga haftalik "trend" tahlili (qaysi animega
    shikoyat/maqtov ko'p) uchun material sifatida. Natija: 
    [{"anime_id":, "title":, "comment_count":, "comments": [...]}], eng ko'p
    izoh olgandan kamiga qarab tartiblangan."""
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute(
            """
            WITH recent AS (
                SELECT c.anime_id, c.text,
                       ROW_NUMBER() OVER (PARTITION BY c.anime_id ORDER BY c.id DESC) AS rn
                FROM comments c
                WHERE c.is_deleted = 0
                  AND c.created_at >= to_char(NOW() - (%s || ' days')::interval, 'YYYY-MM-DD')
            ),
            counts AS (
                SELECT anime_id, COUNT(*) AS cnt FROM recent GROUP BY anime_id
                ORDER BY cnt DESC LIMIT %s
            )
            SELECT r.anime_id, a.title, counts.cnt AS comment_count, r.text
            FROM recent r
            JOIN counts ON counts.anime_id = r.anime_id
            JOIN animes a ON a.id = r.anime_id
            WHERE r.rn <= %s
            ORDER BY counts.cnt DESC, r.anime_id, r.rn
            """,
            (str(days), max_animes, max_comments_per_anime)
        )
        rows = c.fetchall()
        grouped = {}
        order = []
        for r in rows:
            aid = r["anime_id"]
            if aid not in grouped:
                grouped[aid] = {
                    "anime_id": aid, "title": r["title"],
                    "comment_count": r["comment_count"], "comments": [],
                }
                order.append(aid)
            grouped[aid]["comments"].append(r["text"])
        return [grouped[aid] for aid in order]
    finally:
        put_conn(conn)

def toggle_comment_like(comment_id, user_id):
    """Like bosilgan/bosilmagan holatini almashtiradi. Yangi holatni (True=liked) qaytaradi."""
    conn = get_conn()
    try:
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
        return liked, count
    finally:
        put_conn(conn)

def get_comment_by_id(comment_id):
    """Bitta izohni (anime nomi bilan birga) qaytaradi — admin AI-javob
    takliflarini generatsiya qilishda izoh matni/kontekstiga kerak bo'ladi."""
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute(
            """
            SELECT c.*, a.title AS anime_title
            FROM comments c
            LEFT JOIN animes a ON a.id = c.anime_id
            WHERE c.id=%s
            """,
            (comment_id,)
        )
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)

def get_last_comment_at(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT created_at FROM comments WHERE user_id=%s ORDER BY id DESC LIMIT 1", (user_id,))
        row = c.fetchone()
        return row[0] if row else None
    finally:
        put_conn(conn)

def delete_comment(comment_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE comments SET is_deleted=1 WHERE id=%s", (comment_id,))
        conn.commit()
    finally:
        put_conn(conn)

# ===== TOMOSHA JURNALI =====
def log_watch(anime_id, user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO watch_log (anime_id, user_id) VALUES (%s,%s)", (anime_id, user_id))
        conn.commit()
    finally:
        put_conn(conn)

def get_watch_count_today(anime_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM watch_log WHERE anime_id=%s AND watched_at >= to_char(NOW(), 'YYYY-MM-DD')",
            (anime_id,)
        )
        count = c.fetchone()[0]
        return count
    finally:
        put_conn(conn)

# ===== SAYT FOYDALANUVCHILARI (email/telefon + parol bilan roʻyxatdan oʻtish) =====
def create_site_user(email, phone, password_hash, display_name):
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute(
            "INSERT INTO site_users (email, phone, password_hash, display_name) VALUES (%s,%s,%s,%s) RETURNING *",
            (email, phone, password_hash, display_name)
        )
        row = c.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        put_conn(conn)

def get_site_user_by_login(identifier):
    """Email yoki telefon boʻyicha foydalanuvchini topadi (kirish uchun)."""
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM site_users WHERE email=%s OR phone=%s", (identifier, identifier))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)

def get_site_user_by_id(site_user_id):
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM site_users WHERE id=%s", (site_user_id,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)

def get_site_user_by_email(email):
    """Faqat email bo'yicha qidiradi (parol tiklash faqat email orqali ishlaydi —
    SMS xizmati ulanmagan). Telefon bilan ro'yxatdan o'tgan, emaili bo'lmagan
    hisoblar uchun None qaytadi."""
    if not email:
        return None
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM site_users WHERE email=%s", (email,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)

def update_site_user_password(site_user_id, password_hash):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE site_users SET password_hash=%s WHERE id=%s", (password_hash, site_user_id))
        conn.commit()
    finally:
        put_conn(conn)

def create_password_reset(token, site_user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO site_password_resets (token, site_user_id) VALUES (%s,%s)",
            (token, site_user_id)
        )
        conn.commit()
    finally:
        put_conn(conn)

def get_password_reset(token):
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM site_password_resets WHERE token=%s", (token,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)

def mark_password_reset_used(token):
    """set_payment_status singari emas, bu yerda ham bitta tokenning ikki marta
    ishlatilishining oldini olish uchun atomik qilib qilingan: faqat hali
    used=0 bo'lgan qatorga tegadi va shu holatni RETURNING bilan tasdiqlaydi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE site_password_resets SET used=1 WHERE token=%s AND used=0 RETURNING site_user_id",
            (token,)
        )
        row = c.fetchone()
        conn.commit()
        return row[0] if row else None
    finally:
        put_conn(conn)

def cleanup_expired_reset_tokens(older_than_hours=24):
    """Ishlatib bo'lingan (used=1) yoki muddati o'tgan parol-tiklash
    tokenlarini o'chiradi — aks holda site_password_resets jadvali vaqt
    o'tishi bilan cheksiz o'sib boradi. Tokenlar aslida 1 soatdan keyin
    funksional jihatdan eskiradi (webapp_site_reset_password'ga qarang),
    24 soatlik standart shunchaki xavfsiz zaxira."""
    cutoff = (now_tz() - timedelta(hours=older_than_hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    try:
        c = conn.cursor()
        try:
            c.execute(
                "DELETE FROM site_password_resets WHERE used=1 OR created_at < %s",
                (cutoff,)
            )
            conn.commit()
            deleted = c.rowcount
        except psycopg2.Error as e:
            conn.rollback()
            logger.error(f"cleanup_expired_reset_tokens: xato: {e}")
            deleted = 0
        return deleted
    finally:
        put_conn(conn)

def toggle_site_favorite(site_user_id, anime_id):
    """Sevimlilarga qo'shadi/olib tashlaydi. Yangi holatni (True=sevimli) qaytaradi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM site_favorites WHERE site_user_id=%s AND anime_id=%s", (site_user_id, anime_id))
        exists = c.fetchone()
        if exists:
            c.execute("DELETE FROM site_favorites WHERE site_user_id=%s AND anime_id=%s", (site_user_id, anime_id))
            active = False
        else:
            c.execute("INSERT INTO site_favorites (site_user_id, anime_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (site_user_id, anime_id))
            active = True
        conn.commit()
        return active
    finally:
        put_conn(conn)

def get_site_favorite_ids(site_user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT anime_id FROM site_favorites WHERE site_user_id=%s ORDER BY created_at DESC", (site_user_id,))
        ids = [r[0] for r in c.fetchall()]
        return ids
    finally:
        put_conn(conn)

def record_site_history(site_user_id, anime_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO site_history (site_user_id, anime_id, viewed_at) VALUES (%s,%s,to_char(NOW(),'YYYY-MM-DD HH24:MI:SS')) "
            "ON CONFLICT (site_user_id, anime_id) DO UPDATE SET viewed_at=EXCLUDED.viewed_at",
            (site_user_id, anime_id)
        )
        conn.commit()
    finally:
        put_conn(conn)

def get_site_history_ids(site_user_id, limit=30):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT anime_id FROM site_history WHERE site_user_id=%s ORDER BY viewed_at DESC LIMIT %s",
            (site_user_id, limit)
        )
        ids = [r[0] for r in c.fetchall()]
        return ids
    finally:
        put_conn(conn)

def clear_site_history(site_user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM site_history WHERE site_user_id=%s", (site_user_id,))
        conn.commit()
    finally:
        put_conn(conn)

# ===== QO'SHIMCHA ADMINLAR =====
ADMIN_ROLES = {"moderator", "moliya", "super"}

def add_admin(user_id, username, added_by, role="moderator"):
    """Yangi qo'shimcha admin qo'shadi. role — 'moderator' (standart,
    kontent/jamoat), 'moliya' (to'lovlar) yoki 'super' (hammasi). Notogri
    qiymat kelsa xavfsiz standart 'moderator'ga tushiriladi."""
    if role not in ADMIN_ROLES:
        role = "moderator"
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO admins (user_id, username, added_by, role) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username, role=EXCLUDED.role",
            (user_id, username, added_by, role)
        )
        conn.commit()
    finally:
        put_conn(conn)

def remove_admin(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM admins WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        put_conn(conn)

def get_admins():
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute("SELECT * FROM admins ORDER BY added_at DESC")
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

def set_admin_role(user_id, role):
    if role not in ADMIN_ROLES:
        raise ValueError(f"Notogri admin rol: {role}")
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE admins SET role=%s WHERE user_id=%s", (role, user_id))
        updated = c.rowcount
        conn.commit()
        return bool(updated)
    finally:
        put_conn(conn)

# ===== ADMIN FAOLIYATI LOGI =====
def log_admin_action(admin_id, admin_name, action, details=None):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO admin_logs (admin_id, admin_name, action, details) VALUES (%s,%s,%s,%s)",
            (admin_id, admin_name, action, details)
        )
        conn.commit()
    finally:
        put_conn(conn)

def get_admin_logs(limit=30, admin_id=None):
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        if admin_id:
            c.execute("SELECT * FROM admin_logs WHERE admin_id=%s ORDER BY id DESC LIMIT %s", (admin_id, limit))
        else:
            c.execute("SELECT * FROM admin_logs ORDER BY id DESC LIMIT %s", (limit,))
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

# ===== O'SISH STATISTIKASI (grafik uchun) =====
def get_growth_stats(days=7):
    """Oxirgi `days` kun uchun har kunlik yangi foydalanuvchilar va ko'rishlar sonini qaytaradi.
    Natija: [{"date": "YYYY-MM-DD", "new_users": N, "views": N}, ...] (eskidan yangiga qarab tartiblangan)."""
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        c.execute(
            """
            SELECT to_char(d, 'YYYY-MM-DD') AS date
            FROM generate_series(CURRENT_DATE - (%s::int - 1), CURRENT_DATE, interval '1 day') AS d
            """,
            (days,)
        )
        date_rows = [r["date"] for r in c.fetchall()]

        c.execute(
            """
            SELECT date(joined_at) AS d, COUNT(*) AS cnt
            FROM users
            WHERE joined_at >= to_char(CURRENT_DATE - (%s::int - 1), 'YYYY-MM-DD')
            GROUP BY date(joined_at)
            """,
            (days,)
        )
        users_map = {str(r["d"]): r["cnt"] for r in c.fetchall()}

        c.execute(
            """
            SELECT date(watched_at) AS d, COUNT(*) AS cnt
            FROM watch_log
            WHERE watched_at >= to_char(CURRENT_DATE - (%s::int - 1), 'YYYY-MM-DD')
            GROUP BY date(watched_at)
            """,
            (days,)
        )
        views_map = {str(r["d"]): r["cnt"] for r in c.fetchall()}

        return [
            {"date": d, "new_users": users_map.get(d, 0), "views": views_map.get(d, 0)}
            for d in date_rows
        ]
    finally:
        put_conn(conn)

# ===== PREMIUM SOVG'ASI =====
def record_premium_gift(from_user_id, to_user_id, plan, days):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO premium_gifts (from_user_id, to_user_id, plan, days) VALUES (%s,%s,%s,%s)",
            (from_user_id, to_user_id, plan, days)
        )
        conn.commit()
    finally:
        put_conn(conn)

# ===== REFERAL STATISTIKASI =====
def get_referral_stats(user_id):
    """Foydalanuvchi taklif qilgan do'stlar soni va ulardan nechtasi
    Premium sotib olganini qaytaradi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users WHERE referred_by=%s", (user_id,))
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE referred_by=%s AND is_premium=1", (user_id,))
        premium_count = c.fetchone()[0]
        return {"total": total, "premium_count": premium_count}
    finally:
        put_conn(conn)

# ===== ANIME OBUNALARI (shaxsiy eslatmalar) =====
def toggle_anime_subscription(user_id, anime_id):
    """Obunani yoqadi/o'chiradi. Yangi holatni (True=obuna bo'ldi) qaytaradi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM anime_subscriptions WHERE user_id=%s AND anime_id=%s", (user_id, anime_id))
        exists = c.fetchone()
        if exists:
            c.execute("DELETE FROM anime_subscriptions WHERE user_id=%s AND anime_id=%s", (user_id, anime_id))
            active = False
        else:
            c.execute(
                "INSERT INTO anime_subscriptions (user_id, anime_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (user_id, anime_id)
            )
            active = True
        conn.commit()
        return active
    finally:
        put_conn(conn)

def get_anime_subscribers(anime_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT user_id FROM anime_subscriptions WHERE anime_id=%s", (anime_id,))
        ids = [r[0] for r in c.fetchall()]
        return ids
    finally:
        put_conn(conn)

# ===== BILDIRISHNOMALAR (webapp 🔔 paneli) =====
def create_notification(ntype, title, body=None, anime_id=None):
    """Yangi bildirishnoma yozadi: ntype 'episode' | 'anime' | 'announcement'."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO notifications (ntype, title, body, anime_id) VALUES (%s,%s,%s,%s)",
            (ntype, title, body, anime_id)
        )
        conn.commit()
    finally:
        put_conn(conn)

def get_notifications(limit=30, user_id=None):
    """user_id berilsa, shu foydalanuvchi allaqachon yashirgan (dismiss qilgan)
    bildirishnomalar ro'yxatdan chiqarib tashlanadi."""
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        if user_id:
            c.execute("""
                SELECT n.* FROM notifications n
                WHERE NOT EXISTS (
                    SELECT 1 FROM notification_dismissals d
                    WHERE d.notification_id = n.id AND d.user_id = %s
                )
                ORDER BY n.id DESC LIMIT %s
            """, (user_id, limit))
        else:
            c.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT %s", (limit,))
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

def dismiss_notification(user_id, notification_id):
    """Foydalanuvchi bitta bildirishnomani o'qildi deb belgilaydi va uni oʻz
    roʻyxatidan butunlay yashiradi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO notification_dismissals (user_id, notification_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (user_id, notification_id)
        )
        conn.commit()
    finally:
        put_conn(conn)

def get_unread_notification_count(user_id):
    """Foydalanuvchi hali ko'rmagan bildirishnomalar soni. Mehmon (user_id=0/None)
    uchun har doim 0 qaytaradi — Telegram orqali tasdiqlanmagan foydalanuvchiga
    o'qilgan/o'qilmagan holatini kuzatib bo'lmaydi."""
    if not user_id:
        return 0
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT COALESCE(last_seen_notification_id,0) FROM users WHERE user_id=%s", (user_id,))
        row = c.fetchone()
        last_seen = row[0] if row else 0
        c.execute("""
            SELECT COUNT(*) FROM notifications n
            WHERE n.id > %s AND NOT EXISTS (
                SELECT 1 FROM notification_dismissals d
                WHERE d.notification_id = n.id AND d.user_id = %s
            )
        """, (last_seen, user_id))
        count = c.fetchone()[0]
        return count
    finally:
        put_conn(conn)

def mark_notifications_seen(user_id):
    if not user_id:
        return
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT COALESCE(MAX(id),0) FROM notifications")
        max_id = c.fetchone()[0]
        c.execute("UPDATE users SET last_seen_notification_id=%s WHERE user_id=%s", (max_id, user_id))
        conn.commit()
    finally:
        put_conn(conn)

# ===== AVTOMATIK BACKUP =====
def get_all_table_names():
    """public sxemasidagi barcha jadval nomlarini qaytaradi. Yangi jadval
    qo'shilsa ham, backup avtomatik uni ham qamrab oladi (hardcode qilinmagan)."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE'
            ORDER BY table_name
        """)
        tables = [r[0] for r in c.fetchall()]
        return tables
    finally:
        put_conn(conn)

def export_backup():
    """Barcha jadvallardagi hozirgi ma'lumotlarni {jadval_nomi: [qatorlar...]}
    ko'rinishida qaytaradi. Natija JSON'ga to'g'ridan-to'g'ri serializatsiya
    qilinishi mumkin (sana/vaqt kabi maxsus tiplar chaqiruvchi tomonda
    json.dumps(..., default=str) bilan matnga aylantiriladi)."""
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        tables = get_all_table_names()
        data = {}
        for t in tables:
            c.execute(f'SELECT * FROM "{t}"')
            data[t] = [dict(row) for row in c.fetchall()]
        return data
    finally:
        put_conn(conn)

# ===== AI FUNKSIYALARI =====
def mark_ai_used(user_id):
    """Foydalanuvchi AI funksiyasidan (chat yoki tavsiya) birinchi/keyingi
    marta foydalanganda chaqiriladi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE users SET ai_used=1, ai_used_at=to_char(NOW(),'YYYY-MM-DD HH24:MI:SS') WHERE user_id=%s",
            (user_id,)
        )
        conn.commit()
    finally:
        put_conn(conn)

def get_users_never_used_ai():
    """AI funksiyalaridan hech qachon foydalanmagan, hozir faol (bloklamagan/
    tark etmagan) foydalanuvchilar ro'yxati — maxsus taklif xabari uchun."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT user_id FROM users
            WHERE COALESCE(ai_used,0)=0 AND is_blocked=0 AND is_active=1
        """)
        rows = [r[0] for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)

def add_ai_chat_message(user_id, role, text):
    """AI suhbat tarixiga bitta xabar qo'shadi va shu foydalanuvchi uchun
    faqat oxirgi 12 ta xabarni (6 juftlik) saqlab qoladi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO ai_chat_history (user_id, role, text) VALUES (%s,%s,%s)",
            (user_id, role, text[:4000])
        )
        c.execute("""
            DELETE FROM ai_chat_history WHERE user_id=%s AND id NOT IN (
                SELECT id FROM ai_chat_history WHERE user_id=%s
                ORDER BY id DESC LIMIT 12
            )
        """, (user_id, user_id))
        conn.commit()
    finally:
        put_conn(conn)


def log_ai_question(user_id, text):
    """Foydalanuvchi AI'ga yozgan savolni DOIMIY jurnalga qo'shadi (admin
    keyinchalik "AI savollari" bo'limida ko'rib chiqishi uchun) —
    add_ai_chat_message'dan farqli o'laroq, bu yerdagi yozuvlar hech qachon
    avtomatik o'chirilmaydi."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO ai_questions_log (user_id, text) VALUES (%s,%s)",
            (user_id, text[:4000])
        )
        conn.commit()
    finally:
        put_conn(conn)


def get_ai_questions(page=0, per_page=10):
    """AI'ga yozilgan savollarni eng yangisidan boshlab sahifalab qaytaradi,
    har biriga (agar mavjud bo'lsa) foydalanuvchi ismi/username'ini ham
    qo'shib — admin panelidagi "AI savollari" ro'yxati uchun."""
    conn = get_conn()
    try:
        c = psycopg2.extras.RealDictCursor(conn)
        offset = page * per_page
        c.execute("""
            SELECT q.id, q.user_id, q.text, q.created_at, u.full_name, u.username
            FROM ai_questions_log q
            LEFT JOIN users u ON u.user_id = q.user_id
            ORDER BY q.id DESC LIMIT %s OFFSET %s
        """, (per_page, offset))
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        put_conn(conn)


def get_ai_questions_count():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM ai_questions_log")
        count = c.fetchone()[0]
        return count
    finally:
        put_conn(conn)


