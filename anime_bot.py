import asyncio
import base64
import hashlib
import hmac
import logging
import math
import re
import subprocess
import time
from urllib.parse import parse_qsl
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatMemberUpdated, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    WebAppInfo, FSInputFile
)
import json
from aiogram.filters import CommandStart, Command, ChatMemberUpdatedFilter, KICKED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.utils.callback_answer import CallbackAnswerMiddleware
from aiohttp import web
import aiohttp
import io
try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# MUHIM: Pyrogramning sync-yordamchi moduli import paytida asyncio.get_event_loop()
# ni chaqiradi va shu loop'ni ichkarida eslab qoladi. Agar keyinroq dastur boshqa
# (masalan asyncio.run() yaratgan) loop bilan ishga tushirilsa, Pyrogram xatolik beradi
# ("attached to a different loop"). Shu sabab BITTA loop'ni shu yerda yaratib,
# uni butun dastur davomida (pastda ham) ishlatamiz.
import asyncio as _asyncio_bootstrap
_MAIN_LOOP = _asyncio_bootstrap.new_event_loop()
_asyncio_bootstrap.set_event_loop(_MAIN_LOOP)

from pyrogram import Client as PyroClient

import database as db

# ===================== SOZLAMALAR =====================
import os

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable topilmadi! "
        "Render'da Environment > Add Environment Variable orqali BOT_TOKEN ni qoʻshing."
    )
ADMIN_ID_raw = os.environ.get("ADMIN_ID")
if not ADMIN_ID_raw:
    raise RuntimeError(
        "ADMIN_ID environment variable topilmadi! "
        "Render'da Environment > Add Environment Variable orqali o'zingizning "
        "Telegram ID'ingizni ADMIN_ID sifatida qo'shing (masalan @userinfobot orqali oling)."
    )
ADMIN_ID = int(ADMIN_ID_raw)
# /debug endpointi faqat shu tokenni bilgan kishi uchun ochiq (masalan
# ?token=... orqali). Agar o'rnatilmagan bo'lsa, endpoint butunlay o'chirilgan
# hisoblanadi — hech kim (hatto tasodifan URL topgan kishi ham) server fayl
# tuzilishini ko'ra olmaydi.
DEBUG_TOKEN = os.environ.get("DEBUG_TOKEN")
STORAGE_CHANNEL_raw = os.environ.get("STORAGE_CHANNEL")
if not STORAGE_CHANNEL_raw:
    raise RuntimeError(
        "STORAGE_CHANNEL environment variable topilmadi! "
        "Videolar saqlanadigan xom kanal ID'sini (masalan -100xxxxxxxxxx) "
        "STORAGE_CHANNEL sifatida qo'shing."
    )
STORAGE_CHANNEL = int(STORAGE_CHANNEL_raw)
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://anime-bot-fd8r.onrender.com/webapp")

# Qism videolari STORAGE_CHANNEL'ga saqlanganda avtomatik qo'yiladigan caption.
# Raqam (qism tartib raqami) har safar avtomatik hisoblanadi — admin qo'lda
# yozmaydi. Brend nomini o'zgartirish uchun faqat shu qatorni tahrirlang.
EPISODE_CREDIT_TAG = "@Ani_Max"

def episode_caption(ep_num: int) -> str:
    return f"{EPISODE_CREDIT_TAG} kanali uchun maxsus ({ep_num}-qism)"

# Yangi anime qo'shilganda karta (rasm + tavsif) avtomatik post qilinadigan ochiq
# reklama/e'lon kanali. Ixtiyoriy — o'rnatilmasa, bu funksiya oddiygina o'chiq
# hisoblanadi va botning boshqa ishiga ta'sir qilmaydi.
# Yangi anime qo'shilganda karta (rasm + tavsif) avtomatik post qilinadigan ochiq
# reklama/e'lon kanali. Ixtiyoriy — o'rnatilmasa, bu funksiya oddiygina o'chiq
# hisoblanadi va botning boshqa ishiga ta'sir qilmaydi. Raqamli ID (-100...)
# yoki @kanal_username shaklida yozilishi mumkin.
ANNOUNCE_CHANNEL_raw = os.environ.get("ANNOUNCE_CHANNEL")
if ANNOUNCE_CHANNEL_raw:
    ANNOUNCE_CHANNEL = int(ANNOUNCE_CHANNEL_raw) if ANNOUNCE_CHANNEL_raw.lstrip("-").isdigit() else ANNOUNCE_CHANNEL_raw
else:
    ANNOUNCE_CHANNEL = None

# Onlayn video striming uchun (my.telegram.org dan olinadi)
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
STREAM_ENABLED = bool(API_ID and API_HASH)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_extra_admin_cache = {"ids": set(), "loaded_at": 0}
_EXTRA_ADMIN_TTL = 60

async def is_admin_user(user_id):
    """Asosiy ADMIN_ID yoki DB'ga qo'shilgan qo'shimcha adminlardan biri bo'lsa True.
    Qo'shimcha adminlar ro'yxati DB'dan olinadi, tez-tez so'ralmasligi uchun
    qisqa muddat (60s) keshlanadi."""
    if user_id == ADMIN_ID:
        return True
    now = time.time()
    if now - _extra_admin_cache["loaded_at"] > _EXTRA_ADMIN_TTL:
        try:
            admins = await asyncio.to_thread(db.get_admins)
            _extra_admin_cache["ids"] = {a["user_id"] for a in admins}
            _extra_admin_cache["loaded_at"] = now
        except Exception:
            pass
    return user_id in _extra_admin_cache["ids"]

def _invalidate_extra_admin_cache():
    _extra_admin_cache["loaded_at"] = 0

async def log_admin_action(user, action, details=None):
    """Admin faoliyatini log qiladi (kim, nima qildi)."""
    try:
        name = f"@{user.username}" if getattr(user, "username", None) else getattr(user, "full_name", str(user.id))
        await asyncio.to_thread(db.log_admin_action, user.id, name, action, details)
    except Exception as e:
        logger.warning(f"Admin log yozilmadi: {e}")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
# Ko'pgina handlerlar (ayniqsa admin panelidagi 60+ tugma) o'zi call.answer()
# chaqirmaydi — natijada Telegram mijozi tugmani "yuklanmoqda" holatida
# ushlab turadi (matn tezda o'zgarsa ham). Bu middleware har bir callback
# so'rovga handler ishini tugatgach avtomatik javob beradi, agar handler
# allaqachon o'zi call.answer() chaqirgan bo'lsa — takror yubormaydi.
dp.callback_query.middleware(CallbackAnswerMiddleware())

# MUHIM (FLOOD_WAIT tuzatish): in_memory=True klient session_string bermasa, HAR
# safar server qayta ishga tushganda (Render'da bu tez-tez bo'ladi) botni Telegram'ga
# qaytadan "tanishtiradi" (auth.ImportBotAuthorization). Telegram buni shubhali/spam
# harakat deb hisoblab, FLOOD_WAIT (bir necha yuz-ming soniya) bilan bloklab qo'yadi.
# Yechim: birinchi muvaffaqiyatli autentifikatsiyadan keyin sessiya satrini (session
# string) bazaga saqlaymiz va keyingi ishga tushishlarda o'sha saqlangan sessiyadan
# foydalanamiz — bu holda qaytadan auth.ImportBotAuthorization chaqirilmaydi.
def _load_pyro_session(idx):
    try:
        return db.get_setting(f"pyro_session_{idx}")
    except Exception as e:
        # init_db() hali chaqirilmagan bo'lishi mumkin (masalan settings jadvali hali
        # yaratilmagan) — bu holatda shunchaki yangi sessiya bilan boshlanadi.
        logger.warning(f"Pyrogram sessiyasini yuklab bo'lmadi ({idx}): {e}")
        return None

# Pyrogram klienti — faqat katta video fayllarni brauzerga oqim (stream) qilish uchun.
# aiogram bilan bir xil bot tokenidan foydalanadi, foydalanuvchi login qilishi shart emas.
pyro = PyroClient(
    "stream_bot",
    api_id=int(API_ID) if API_ID else None,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    session_string=_load_pyro_session(1),
) if STREAM_ENABLED else None

# Bir nechta foydalanuvchi bir vaqtda video ko'rganda hammasi bitta MTProto ulanishi
# orqali ketmasin (aks holda hammasiga sekin yuklanadi) deb, streaming uchun bir nechta
# Pyrogram klient ("ishchi") yaratamiz va so'rovlarni ular orasida navbat bilan (round-robin)
# taqsimlaymiz. `pyro` o'zgaruvchisi avvalgidek boshqa joylarda (get_messages, get_chat)
# ishlatilaveradi — faqat og'ir qism (stream_media) bir nechta ulanishga bo'linadi.
STREAM_WORKERS = int(os.environ.get("STREAM_WORKERS", "3"))
_stream_clients = [pyro] if STREAM_ENABLED else []
if STREAM_ENABLED:
    for _i in range(2, STREAM_WORKERS + 1):
        _stream_clients.append(PyroClient(
            f"stream_bot_{_i}",
            api_id=int(API_ID),
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            in_memory=True,
            session_string=_load_pyro_session(_i),
        ))

_stream_client_counter = 0
def _next_stream_client():
    """Navbatdagi (round-robin) streaming klientini qaytaradi."""
    global _stream_client_counter
    if not _stream_clients:
        return pyro
    client = _stream_clients[_stream_client_counter % len(_stream_clients)]
    _stream_client_counter += 1
    return client

# ===================== STATES =====================
class RegState(StatesGroup):
    phone = State()

class AddAnime(StatesGroup):
    photo = State()
    title = State()
    year = State()
    country = State()
    genre = State()
    description = State()
    language = State()
    media_type = State()
    total_episodes = State()
    status = State()
    videos = State()

class AddEpisode(StatesGroup):
    choose_method = State()
    choose_anime = State()
    videos = State()

class ClipVideo(StatesGroup):
    search_query = State()
    waiting_video = State()

class EditAnime(StatesGroup):
    choose_method = State()
    search_query = State()
    choose_field = State()
    new_value = State()

class DeleteAnime(StatesGroup):
    search_query = State()
    confirm = State()

class AddBanner(StatesGroup):
    photo = State()
    title = State()
    subtitle = State()
    anime_link = State()

class EditEpisode(StatesGroup):
    search_query = State()
    choose_episode = State()
    new_video = State()

class DeleteEpisode(StatesGroup):
    search_query = State()
    choose_episode = State()

class BroadcastState(StatesGroup):
    choose_type = State()
    message = State()
    button_text = State()
    button_link = State()
    confirm = State()

class AddChannelState(StatesGroup):
    channel = State()

class LinksState(StatesGroup):
    choose_link = State()
    new_value = State()

class WordFilterState(StatesGroup):
    add_words = State()

class SponsorState(StatesGroup):
    photo = State()
    title = State()
    url = State()

class PremiumState(StatesGroup):
    waiting_screenshot = State()

class PremiumAdminState(StatesGroup):
    price_1m = State()
    price_3m = State()
    price_1y = State()
    card = State()
    early_hours = State()
    referral_bonus = State()
    promo_end = State()
    promo_note = State()

class SearchState(StatesGroup):
    query = State()

class BlockState(StatesGroup):
    user_id = State()

class UnblockState(StatesGroup):
    user_id = State()

class FindUserState(StatesGroup):
    query = State()

class AdminManageState(StatesGroup):
    add_id = State()

class AdminPremiumGiftState(StatesGroup):
    user_id = State()
    choosing_plan = State()
    custom_days = State()

class AdminPaymentAdjustState(StatesGroup):
    amount = State()

class VersionState(StatesGroup):
    version = State()
    changes = State()

async def _subscriptions_enabled():
    """Admin '🔔 Obuna bo'lish' funksiyasini sozlamalardan yoqib/o'chira oladi.
    Sozlama umuman kiritilmagan bo'lsa (birinchi marta ishga tushirilganda),
    funksiya standart holatda YOQILGAN deb hisoblanadi."""
    val = await asyncio.to_thread(db.get_setting, "subscriptions_enabled")
    return val != "0"


async def notify_anime_subscribers(anime_id, text, exclude_user_id=None):
    """Shu anime uchun '🔔 Obuna bo'lish' tugmasini bosgan barcha
    foydalanuvchilarga shaxsiy xabar yuboradi (masalan yangi qism yoki
    jonli efir boshlanganda). Botni bloklagan foydalanuvchilar jimgina
    o'tkazib yuboriladi."""
    if not await _subscriptions_enabled():
        return
    user_ids = await asyncio.to_thread(db.get_anime_subscribers, anime_id)
    for uid in user_ids:
        if uid == exclude_user_id:
            continue
        try:
            await bot.send_message(uid, text, parse_mode="HTML", disable_web_page_preview=True)
        except TelegramForbiddenError:
            await mark_user_left(uid)
        except Exception as e:
            logger.warning(f"[subscribers] {uid} ga xabar yuborilmadi: {e}")
        await asyncio.sleep(0.05)  # Telegram flood-limitiga tegib qolmaslik uchun


@dp.callback_query(F.data.regexp(r"^subq_\d+$"))
async def toggle_anime_subscription_cb(call: CallbackQuery):
    if not await _subscriptions_enabled():
        await call.answer("⚠️ Obuna funksiyasi hozircha admin tomonidan o'chirilgan.", show_alert=True)
        return
    anime_id = int(call.data.split("_")[1])
    anime = await asyncio.to_thread(db.get_anime, anime_id)
    if not anime:
        await call.answer("❌ Topilmadi", show_alert=True)
        return
    active = await asyncio.to_thread(db.toggle_anime_subscription, call.from_user.id, anime_id)
    await call.answer(
        f"🔔 \"{anime['title']}\" uchun obuna yoqildi! Yangi qism yoki jonli "
        f"efir boshlansa, shaxsiy xabar beramiz." if active
        else f"🔕 \"{anime['title']}\" uchun obuna bekor qilindi.",
        show_alert=True
    )


# ===================== YORDAMCHI =====================
def _invalidate_sub_cache(user_id):
    pass  # Kesh olib tashlandi — bu funksiya faqat eski chaqiruvlar buzilmasligi uchun qoldirildi

async def check_subscription(user_id):
    if not user_id:
        # user_id=0/None kelsa (masalan webappda Telegram foydalanuvchi ID'sini
        # bermagan holatda) — Telegramga so'rov yuborish shart emas, u baribir
        # "invalid user_id specified" xatosini qaytaradi. Sababni aniq logga
        # yozib, darhol rad etamiz.
        logger.warning("check_subscription: user_id bo'sh/0 keldi, tekshiruv o'tkazib yuborildi")
        return False

    premium = await asyncio.to_thread(db.get_premium_status, user_id)
    if premium["is_premium"]:
        return True
    channels = await asyncio.to_thread(db.get_channels)
    if not channels:
        return True

    async def _check_one(ch):
        try:
            member = await bot.get_chat_member(ch["channel_id"], user_id)
            return member.status not in ["left", "kicked", "banned"]
        except Exception as e:
            # MUHIM: ilgari bu yerda xatolik jim o'tkazib yuborilardi va
            # "obuna bor" deb hisoblanardi — natijada, agar bot kanalda
            # ADMIN qilib qo'yilmagan bo'lsa (get_chat_member shu sabab bilan
            # har doim xato qaytaradi), majburiy obuna HAMMA uchun butunlay
            # ishlamay qolardi (obuna bo'lgan-bo'lmaganidan qat'iy nazar).
            # Endi xato holatda kirish RAD ETILADI (xavfsizroq yo'l) va
            # sabab logga yoziladi — bu deyarli har doim: bot o'sha kanalga
            # ADMIN sifatida qo'shilmagan degani.
            logger.warning(
                f"check_subscription: {ch['channel_id']} kanali tekshirilmadi "
                f"(bot bu kanalda admin emasmi?): {e}"
            )
            return False

    # Kanallar parallel tekshiriladi (ketma-ket emas) — bu hech qanday xato
    # keltirib chiqarmaydi, faqat 2-3+ kanal bo'lganda tezroq javob beradi.
    # Natija KESHLANMAYDI — har safar Telegramdan jonli holat olinadi, shuning
    # uchun obunani hozirgina bekor qilgan/qilgan foydalanuvchi doim to'g'ri
    # natija ko'radi.
    results = await asyncio.gather(*[_check_one(ch) for ch in channels])
    return all(results)

async def sub_message_text():
    """Majburiy kanallar ekranida chiqadigan matn — kanallarga obuna bo'lish
    yoki Premium orqali cheklovsiz foydalanish haqida aniq imtiyozlar bilan."""
    prices = await premium_settings()
    return (
        "⚠️ Botdan to'liq foydalanish uchun quyidagi kanallarga obuna bo'ling!\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
        "✅ Yoki Premium sotib oling\n"
        "Kanallarga obuna bo'lmasdan cheklovlarsiz foydalaning:\n\n"
        "👑 Kanalsiz to'liq kirish\n"
        "👑 Reklama bannersiz\n"
        f"👑 Yangi qismlarga {prices['early_hours']} soat oldinroq kirish\n"
        "👑 Izohlaringiz yuqorida va 👑 belgi bilan\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
    )

async def sub_keyboard():
    channels = await asyncio.to_thread(db.get_channels)
    buttons = []
    for ch in channels:
        buttons.append([InlineKeyboardButton(
            text=f"📢 {ch['channel_name']}",
            url=f"https://t.me/{ch['channel_id'].lstrip('@')}"
        )])
    buttons.append([InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="check_sub", style="success")])
    buttons.append([InlineKeyboardButton(text="💎 Premium orqali kirish (kanalsiz)", callback_data="premium_menu", style="success")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def guard_access(event, is_callback=True):
    """Har qanday kontent ko'rsatishdan oldin bloklanganlik va majburiy obunani tekshiradi.
    True qaytarsa — davom etish mumkin, False bo'lsa — foydalanuvchiga xabar allaqachon yuborilgan."""
    user_id = event.from_user.id
    u = await asyncio.to_thread(db.get_user, user_id)
    if u and u.get("is_blocked"):
        msg = "🚫 Siz bloklandingiz."
        if is_callback:
            await event.answer(msg, show_alert=True)
        else:
            await event.answer(msg)
        return False
    subscribed = await check_subscription(user_id)
    if not subscribed:
        kb = await sub_keyboard()
        text = await sub_message_text()
        if is_callback:
            await event.answer("📢 Avval kanallarga obuna bo'ling!", show_alert=True)
            try:
                await event.message.answer(text, reply_markup=kb)
            except Exception:
                pass
        else:
            await event.answer(text, reply_markup=kb)
        return False
    return True


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Qidiruv", callback_data="search", style="primary")],
        [
            InlineKeyboardButton(text="🎬 Anime Film", callback_data="films_0", style="primary"),
            InlineKeyboardButton(text="📺 Anime Serial", callback_data="serials_0", style="primary"),
        ],
        [InlineKeyboardButton(text="🎲 Random", callback_data="random", style="success")],
        [InlineKeyboardButton(text="💎 Premium", callback_data="premium_menu", style="success")],
    ])

# ===================== PREMIUM =====================
PLAN_LABELS = {"1m": "1 oy", "3m": "3 oy", "1y": "1 yil"}
# Muddat tugashiga shuncha kun (yoki kamroq) qolganda ham foydalanuvchi yangi
# tarif sotib olishi (yangilashi) mumkin — yangi kunlar eskisining USTIGA qo'shiladi
# (extend_premium shu logikani allaqachon qo'llab-quvvatlaydi).
PREMIUM_RENEWAL_WINDOW_DAYS = 5
PLAN_DAYS = {"1m": 30, "3m": 90, "1y": 365}
BOT_USERNAME = None  # main() ichida to'ldiriladi

def fmt_som(n):
    return f"{n:,}".replace(",", " ") + " so'm"

_premium_settings_cache = {"data": None, "ts": 0.0}
_PREMIUM_SETTINGS_TTL = 20  # soniya — admin narx/karta o'zgartirsa, eng ko'pi bilan
                            # shuncha vaqtdan keyin ko'rinadi, lekin har bir epizod/
                            # foydalanuvchi so'rovida 11 tadan DB round-trip yo'qoladi

def _invalidate_premium_cache():
    _premium_settings_cache["data"] = None
    _premium_settings_cache["ts"] = 0.0

async def premium_settings():
    now = time.time()
    cached = _premium_settings_cache["data"]
    if cached is not None and (now - _premium_settings_cache["ts"]) < _PREMIUM_SETTINGS_TTL:
        return cached
    keys = [
        "premium_price_1m", "premium_price_3m", "premium_price_1y",
        "premium_card_number", "premium_card_holder", "premium_early_hours",
        "premium_referral_bonus_days", "premium_enabled",
        "premium_plan_1m_enabled", "premium_plan_3m_enabled", "premium_plan_1y_enabled",
    ]
    values = await asyncio.gather(*(asyncio.to_thread(db.get_setting, k) for k in keys))
    (p1, p3, p12, card, holder, early, ref_bonus, enabled_raw,
     plan_1m_raw, plan_3m_raw, plan_1y_raw) = values
    p1 = p1 or "15000"; p3 = p3 or "40000"; p12 = p12 or "120000"
    card = card or ""; holder = holder or ""
    early = early or "48"; ref_bonus = ref_bonus or "3"
    result = {
        "1m": int(p1), "3m": int(p3), "1y": int(p12),
        "card": card, "holder": holder,
        "early_hours": int(early), "ref_bonus": int(ref_bonus),
        "enabled": (enabled_raw or "1") == "1",
        "plan_1m_on": (plan_1m_raw or "1") == "1",
        "plan_3m_on": (plan_3m_raw or "1") == "1",
        "plan_1y_on": (plan_1y_raw or "1") == "1",
    }
    _premium_settings_cache["data"] = result
    _premium_settings_cache["ts"] = now
    return result

def premium_menu_keyboard(prices):
    rows = []
    if prices["plan_1m_on"]:
        rows.append([InlineKeyboardButton(text=f"1 oy — {fmt_som(prices['1m'])}", callback_data="premium_buy_1m", style="success")])
    if prices["plan_3m_on"]:
        rows.append([InlineKeyboardButton(text=f"3 oy — {fmt_som(prices['3m'])}", callback_data="premium_buy_3m", style="success")])
    if prices["plan_1y_on"]:
        rows.append([InlineKeyboardButton(text=f"1 yil — {fmt_som(prices['1y'])}", callback_data="premium_buy_1y", style="success")])
    rows.append([InlineKeyboardButton(text="🎁 Do'stlarni taklif qilish", callback_data="premium_referral")])
    rows.append([InlineKeyboardButton(text="🏠 Bosh menu", callback_data="main_menu", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def build_premium_menu(user_id):
    """Premium menyu matni va klaviaturasini qaytaradi (callback va deep-link uchun umumiy)."""
    status = await asyncio.to_thread(db.get_premium_status, user_id)
    prices = await premium_settings()
    if not prices["enabled"]:
        text = (
            "💎 <b>Premium</b>\n\n"
            "⏸ Premium xizmati hozircha vaqtincha o'chirilgan.\n"
            "Tez orada qayta yoqiladi, kuzatib boring!"
        )
        return text, InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Bosh menu", callback_data="main_menu", style="primary")],
        ])
    if status["is_premium"] and status["days_left"] > PREMIUM_RENEWAL_WINDOW_DAYS:
        text = (
            f"👑 <b>Siz allaqachon Premium foydalanuvchisiz!</b>\n\n"
            f"⏳ Amal qilish muddati: <b>{status['days_left']} kun</b> qoldi\n"
            f"📦 Joriy tarif: {PLAN_LABELS.get(status['plan'], status['plan'] or '—')}\n\n"
            f"Yangi tarif sotib olish hozircha kerak emas 😉"
        )
        # Premium foydalanuvchiga qayta sotib olish tugmalari ko'rsatilmaydi —
        # faqat referal va bosh menyu.
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Do'stlarni taklif qilish", callback_data="premium_referral")],
            [InlineKeyboardButton(text="🏠 Bosh menu", callback_data="main_menu", style="primary")],
        ])
        return text, kb
    if status["is_premium"]:
        # Muddat tugashiga PREMIUM_RENEWAL_WINDOW_DAYS yoki kamroq kun qoldi —
        # yangilashga ruxsat beramiz, yangi kunlar eskisining ustiga qo'shiladi.
        text = (
            f"👑 <b>Sizning Premium'ingiz tez orada tugaydi!</b>\n\n"
            f"⏳ Amal qilish muddati: <b>{status['days_left']} kun</b> qoldi\n"
            f"📦 Joriy tarif: {PLAN_LABELS.get(status['plan'], status['plan'] or '—')}\n\n"
            f"Hozir yangilasangiz, yangi kunlar qolgan muddatning ustiga qo'shiladi 👇"
        )
        return text, premium_menu_keyboard(prices)
    text = (
        "💎 <b>Premium imtiyozlari:</b>\n\n"
        "✅ Majburiy kanal obunasi shart emas\n"
        "✅ Reklama bannersiz\n"
        f"✅ Yangi qismlarga {prices['early_hours']} soat oldinroq kirish\n"
        "✅ Izohlaringiz yuqorida va 👑 belgi bilan chiqadi\n\n"
        "Tarifni tanlang:"
    )
    return text, premium_menu_keyboard(prices)

@dp.callback_query(F.data == "premium_menu")
async def premium_menu(call: CallbackQuery):
    await call.answer()
    text, kb = await build_premium_menu(call.from_user.id)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("premium_buy_"))
async def premium_buy(call: CallbackQuery, state: FSMContext):
    plan = call.data.replace("premium_buy_", "")
    status = await asyncio.to_thread(db.get_premium_status, call.from_user.id)
    if status["is_premium"] and status["days_left"] > PREMIUM_RENEWAL_WINDOW_DAYS:
        await call.answer(
            "👑 Siz allaqachon Premium foydalanuvchisiz!\n"
            f"Amal qilish muddati: {status['days_left']} kun qoldi.\n"
            "Yangi tarif sotib olish hozircha kerak emas 😉",
            show_alert=True
        )
        return
    prices = await premium_settings()
    if not prices["enabled"]:
        await call.answer("⏸ Premium xizmati hozircha vaqtincha o'chirilgan.", show_alert=True)
        return
    plan_flag = {"1m": "plan_1m_on", "3m": "plan_3m_on", "1y": "plan_1y_on"}.get(plan)
    if plan_flag and not prices[plan_flag]:
        await call.answer("⏸ Bu tarif hozircha vaqtincha yopiq.", show_alert=True)
        return
    amount = prices.get(plan)
    if not amount:
        await call.answer("Xatolik yuz berdi", show_alert=True)
        return
    if not prices["card"]:
        await call.answer("Hozircha to'lov qabul qilish sozlanmagan. Keyinroq urinib ko'ring.", show_alert=True)
        return
    await call.answer()
    await state.set_state(PremiumState.waiting_screenshot)
    await state.update_data(plan=plan, amount=amount)
    card_line = f"💳 <code>{prices['card']}</code>"
    holder_line = f"\n👤 {prices['holder']}" if prices["holder"] else ""
    await call.message.edit_text(
        f"💳 <b>{PLAN_LABELS[plan]} — {fmt_som(amount)}</b>\n"
        f"Quyidagi kartaga to'lovni amalga oshiring:\n\n"
        f"{card_line}{holder_line}\n\n"
        f"💰 Summa: <b>{fmt_som(amount)}</b>\n\n"
        f"To'lovni amalga oshirgach, chekni shu yerga yuboring.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="premium_menu")],
        ]),
        parse_mode="HTML"
    )

@dp.message(PremiumState.waiting_screenshot, F.photo | F.document)
async def premium_screenshot_received(message: Message, state: FSMContext):
    data = await state.get_data()
    plan = data.get("plan")
    amount = data.get("amount")
    if not plan:
        await state.clear()
        return

    is_document = message.document is not None
    if is_document:
        # Fayl sifatida yuborilgan chek — faqat rasm fayllarini qabul qilamiz
        # (masalan, hujjat yoki arxiv chek sifatida yuborilishining oldini olamiz).
        mime = (message.document.mime_type or "")
        if not mime.startswith("image/"):
            await message.answer("📸 Iltimos, chekni rasm (screenshot) yoki rasm fayli sifatida yuboring.")
            return
        file_id = message.document.file_id
    else:
        file_id = message.photo[-1].file_id

    payment_id = await asyncio.to_thread(
        db.create_payment_request, message.from_user.id, plan, amount, file_id
    )
    await state.clear()
    await message.answer("✅ Chek qabul qilindi! Admin tomonidan tekshirilib, tez orada tasdiqlanadi.")
    u = message.from_user
    uname = f"@{u.username}" if u.username else u.full_name
    caption = (
        f"💎 <b>Yangi Premium to'lovi</b>\n\n"
        f"👤 {uname} (ID: <code>{u.id}</code>)\n"
        f"📦 Tarif: {PLAN_LABELS.get(plan, plan)}\n"
        f"💰 Summa: {fmt_som(amount)}"
    )
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"pay_ok_{payment_id}", style="success"),
            InlineKeyboardButton(text="❌ Rad etish", callback_data=f"pay_no_{payment_id}", style="danger"),
        ],
        [InlineKeyboardButton(text="💰 Summani tuzatib tasdiqlash", callback_data=f"pay_editamt_{payment_id}", style="primary")],
    ])
    try:
        if is_document:
            await bot.send_document(ADMIN_ID, file_id, caption=caption, reply_markup=admin_kb, parse_mode="HTML")
        else:
            await bot.send_photo(ADMIN_ID, file_id, caption=caption, reply_markup=admin_kb, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Admin'ga to'lov xabari yuborilmadi: {e}")

@dp.message(PremiumState.waiting_screenshot)
async def premium_screenshot_wrong(message: Message):
    await message.answer("📸 Iltimos, chekni rasm (screenshot) yoki fayl sifatida yuboring.")

@dp.callback_query(F.data.startswith("pay_ok_"))
async def premium_approve(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    payment_id = int(call.data.split("_")[-1])
    payment = await asyncio.to_thread(db.get_payment_request, payment_id)
    if not payment or payment["status"] != "pending":
        await call.answer("Bu so'rov allaqachon ko'rib chiqilgan", show_alert=True)
        return
    days = PLAN_DAYS.get(payment["plan"], 30)
    new_until = await asyncio.to_thread(db.extend_premium, payment["user_id"], days, payment["plan"])
    await asyncio.to_thread(db.set_payment_status, payment_id, "approved")
    _invalidate_sub_cache(payment["user_id"])  # Premium bo'ldi — majburiy obuna talabidan darhol ozod bo'lsin
    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n✅ <b>Tasdiqlandi</b>", parse_mode="HTML")
    except Exception:
        pass
    try:
        await bot.send_message(
            payment["user_id"],
            f"🎉 Tabriklaymiz! Premium yoqildi.\n\n📅 Amal qilish muddati: <b>{new_until.strftime('%d.%m.%Y')}</b> gacha",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await call.answer("✅ Tasdiqlandi")

@dp.callback_query(F.data.startswith("pay_no_"))
async def premium_reject(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    payment_id = int(call.data.split("_")[-1])
    payment = await asyncio.to_thread(db.get_payment_request, payment_id)
    if not payment or payment["status"] != "pending":
        await call.answer("Bu so'rov allaqachon ko'rib chiqilgan", show_alert=True)
        return
    await asyncio.to_thread(db.set_payment_status, payment_id, "rejected")
    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n❌ <b>Rad etildi</b>", parse_mode="HTML")
    except Exception:
        pass
    try:
        await bot.send_message(
            payment["user_id"],
            "❌ To'lovingiz tasdiqlanmadi. Chekni tekshirib qayta yuborishga urinib ko'ring yoki admin bilan bog'laning."
        )
    except Exception:
        pass
    await call.answer("❌ Rad etildi")

# ---- To'lov summasini TUZATIB tasdiqlash (foydalanuvchi so'ralganidan kam/ko'p pul yuborgan bo'lsa) ----
@dp.callback_query(F.data.startswith("pay_editamt_"))
async def premium_edit_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    payment_id = int(call.data.split("_")[-1])
    payment = await asyncio.to_thread(db.get_payment_request, payment_id)
    if not payment or payment["status"] != "pending":
        await call.answer("Bu so'rov allaqachon ko'rib chiqilgan", show_alert=True)
        return
    await call.answer()
    await state.set_state(AdminPaymentAdjustState.amount)
    await state.update_data(payment_id=payment_id, chat_id=call.message.chat.id, message_id=call.message.message_id)
    await call.message.answer(
        f"💰 To'lov #{payment_id} — so'ralgan summa: <b>{fmt_som(payment['amount'])}</b>.\n"
        f"Foydalanuvchi haqiqatda qancha yuborgan bo'lsa, o'sha summani raqam bilan yuboring "
        f"(masalan: <code>7000</code>). Premium muddati shu summaga mos ravishda "
        f"(qisman) qo'shiladi.",
        parse_mode="HTML"
    )

@dp.message(AdminPaymentAdjustState.amount)
async def premium_edit_amount(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        await state.clear()
        return
    try:
        real_amount = int(re.sub(r"[^\d]", "", message.text.strip()))
        if real_amount <= 0:
            raise ValueError
    except Exception:
        await message.answer("❌ Iltimos, musbat butun son kiriting (masalan: 7000).")
        return
    data = await state.get_data()
    payment_id = data.get("payment_id")
    chat_id = data.get("chat_id")
    message_id = data.get("message_id")
    await state.clear()

    payment = await asyncio.to_thread(db.get_payment_request, payment_id)
    if not payment or payment["status"] != "pending":
        await message.answer("Bu so'rov allaqachon ko'rib chiqilgan.")
        return

    requested_amount = payment["amount"] or 1
    full_days = PLAN_DAYS.get(payment["plan"], 30)
    # Haqiqatda tushgan summaga proporsional kun beriladi (masalan 10 000 o'rniga
    # 7 000 tushsa, 30 kundan 21 kun beriladi). Kamida 1 kun beriladi.
    days = max(1, round(full_days * real_amount / requested_amount))

    await asyncio.to_thread(db.set_payment_amount, payment_id, real_amount)
    new_until = await asyncio.to_thread(db.extend_premium, payment["user_id"], days, payment["plan"])
    await asyncio.to_thread(db.set_payment_status, payment_id, "approved")
    _invalidate_sub_cache(payment["user_id"])  # Premium bo'ldi — majburiy obuna talabidan darhol ozod bo'lsin

    if chat_id and message_id:
        try:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id,
                caption=(
                    f"💰 <b>Summa tuzatildi va tasdiqlandi</b>\n"
                    f"So'ralgan: {fmt_som(requested_amount)} → Tushgan: {fmt_som(real_amount)}\n"
                    f"📅 Berilgan muddat: {days} kun"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
    try:
        await bot.send_message(
            payment["user_id"],
            f"🎉 To'lovingiz tasdiqlandi va Premium yoqildi ({days} kunlik).\n\n"
            f"📅 Amal qilish muddati: <b>{new_until.strftime('%d.%m.%Y')}</b> gacha",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await message.answer(
        f"✅ To'lov #{payment_id} tasdiqlandi — summa {fmt_som(real_amount)} deb tuzatildi, "
        f"foydalanuvchiga {days} kunlik Premium berildi."
    )

@dp.callback_query(F.data == "premium_referral")
async def premium_referral(call: CallbackQuery):
    await call.answer()
    prices = await premium_settings()
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{call.from_user.id}"
    stats = await asyncio.to_thread(db.get_referral_stats, call.from_user.id)
    await call.message.edit_text(
        f"🎁 <b>Do'stlaringizni taklif qiling!</b>\n\n"
        f"Har bir do'stingiz sizning havolangiz orqali botga birinchi marta kirsa, "
        f"Premium muddatingizga <b>+{prices['ref_bonus']} kun</b> qo'shiladi.\n\n"
        f"🔗 Sizning shaxsiy havolangiz:\n<code>{link}</code>\n\n"
        f"📊 <b>Statistikangiz:</b>\n"
        f"👥 Taklif qilinganlar: <b>{stats['total']}</b>\n"
        f"👑 Ulardan Premium bo'lganlar: <b>{stats['premium_count']}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="premium_menu")],
        ]),
        parse_mode="HTML"
    )

# ---- ADMIN: PREMIUM SOZLAMALARI ----
async def _premium_admin_text():
    p = await premium_settings()
    card = p["card"] or "—"
    holder = p["holder"] or "—"
    sys_state = "🟢 Yoqilgan" if p["enabled"] else "🔴 O'chirilgan"
    p1_state = "🟢 Yoqilgan" if p["plan_1m_on"] else "🔴 O'chirilgan"
    p3_state = "🟢 Yoqilgan" if p["plan_3m_on"] else "🔴 O'chirilgan"
    p12_state = "🟢 Yoqilgan" if p["plan_1y_on"] else "🔴 O'chirilgan"
    promo_active, promo_end, promo_note = await asyncio.gather(
        asyncio.to_thread(db.get_setting, "premium_promo_active"),
        asyncio.to_thread(db.get_setting, "premium_promo_end"),
        asyncio.to_thread(db.get_setting, "premium_promo_note"),
    )
    promo_on = (promo_active or "0") == "1"
    if promo_on and promo_end:
        try:
            end_dt = datetime.fromisoformat(promo_end)
            promo_line = f"🔥 Chegirma: <b>🟢 Faol</b> — tugash: {end_dt.strftime('%d.%m.%Y %H:%M')}"
            if promo_note:
                promo_line += f"\n📝 Izoh: {promo_note}"
        except Exception:
            promo_line = "🔥 Chegirma: <b>🟢 Faol</b> (sana noto'g'ri formatda)"
    else:
        promo_line = "🔥 Chegirma: 🔴 O'chirilgan"
    return (
        f"💎 <b>Premium sozlamalari</b>\n\n"
        f"⚙️ Tizim holati: <b>{sys_state}</b>\n\n"
        f"1 oy: {fmt_som(p['1m'])} — {p1_state}\n"
        f"3 oy: {fmt_som(p['3m'])} — {p3_state}\n"
        f"1 yil: {fmt_som(p['1y'])} — {p12_state}\n\n"
        f"💳 Karta: <code>{card}</code>\n"
        f"👤 Karta egasi: {holder}\n\n"
        f"⏱ Oldinroq kirish: {p['early_hours']} soat\n"
        f"🎁 Referal bonusi: {p['ref_bonus']} kun\n\n"
        f"{promo_line}"
    )

def _premium_admin_kb():
    """Premium sozlamalarining bosh menyusi — endi tekis roʻyxat emas, mavzu boʻyicha
    kichik boʻlimlarga ajratilgan (Narxlar, Umumiy, Qulflar, Toʻlovlar)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Narxlar va rejalar", callback_data="padm_cat_pricing", style="primary")],
        [InlineKeyboardButton(text="⚙️ Umumiy sozlamalar", callback_data="padm_cat_general", style="primary")],
        [InlineKeyboardButton(text="🔥 Chegirma / Countdown (WebApp)", callback_data="padm_cat_promo", style="primary")],
        [InlineKeyboardButton(text="🔓 Qulflarni ochish", callback_data="padm_cat_unlock", style="danger")],
        [InlineKeyboardButton(text="👑 Premium animelar", callback_data="padm_premium_animes", style="primary")],
        [InlineKeyboardButton(text="🎁 Foydalanuvchiga Premium berish", callback_data="padm_gift_start", style="success")],
        [InlineKeyboardButton(text="📋 To'lov so'rovlari", callback_data="padm_pending", style="success")],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ])

def _padm_pricing_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 oy yoq/o'ch", callback_data="padm_toggle_1m"),
            InlineKeyboardButton(text="3 oy yoq/o'ch", callback_data="padm_toggle_3m"),
            InlineKeyboardButton(text="1 yil yoq/o'ch", callback_data="padm_toggle_1y"),
        ],
        [
            InlineKeyboardButton(text="✏️ 1 oy narxi", callback_data="padm_price_1m", style="success"),
            InlineKeyboardButton(text="✏️ 3 oy narxi", callback_data="padm_price_3m", style="success"),
        ],
        [InlineKeyboardButton(text="✏️ 1 yil narxi", callback_data="padm_price_1y", style="success")],
        [InlineKeyboardButton(text="🔙 Premium sozlamalari", callback_data="admin_premium")],
    ])

def _padm_general_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Premium tizimini yoqish/o'chirish", callback_data="padm_toggle_enabled", style="danger")],
        [
            InlineKeyboardButton(text="💳 Karta", callback_data="padm_card", style="success"),
            InlineKeyboardButton(text="⏱ Oldinroq kirish", callback_data="padm_early", style="success"),
        ],
        [InlineKeyboardButton(text="🎁 Referal bonusi", callback_data="padm_ref", style="success")],
        [InlineKeyboardButton(text="🔙 Premium sozlamalari", callback_data="admin_premium")],
    ])

def _padm_unlock_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔓 Eski qismlar qulfini ochish", callback_data="padm_unlock_old", style="danger")],
        [InlineKeyboardButton(text="🔙 Premium sozlamalari", callback_data="admin_premium")],
    ])

def _padm_promo_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏱ 24 soat", callback_data="padm_promo_quick_24"),
            InlineKeyboardButton(text="⏱ 48 soat", callback_data="padm_promo_quick_48"),
            InlineKeyboardButton(text="⏱ 3 kun", callback_data="padm_promo_quick_72"),
        ],
        [InlineKeyboardButton(text="✏️ Sana/vaqtni qo'lda kiritish", callback_data="padm_promo_end", style="success")],
        [InlineKeyboardButton(text="📝 Izoh matnini o'zgartirish", callback_data="padm_promo_note", style="success")],
        [InlineKeyboardButton(text="🛑 Chegirmani to'xtatish", callback_data="padm_promo_off", style="danger")],
        [InlineKeyboardButton(text="🔙 Premium sozlamalari", callback_data="admin_premium")],
    ])

# Har bir padm_* callback qaysi kichik boʻlimga tegishli ekanini bilib, amaldan
# keyin foydalanuvchini bosh menyuga emas, oʻsha boʻlimga qaytarish uchun.
_PADM_PRICING_KEYS = {"padm_toggle_1m", "padm_toggle_3m", "padm_toggle_1y", "padm_price_1m", "padm_price_3m", "padm_price_1y"}
_PADM_GENERAL_KEYS = {"padm_toggle_enabled", "padm_card", "padm_early", "padm_ref"}

def _padm_kb_for(callback_data):
    if callback_data in _PADM_PRICING_KEYS:
        return _padm_pricing_kb()
    if callback_data in _PADM_GENERAL_KEYS:
        return _padm_general_kb()
    return _premium_admin_kb()

@dp.callback_query(F.data == "padm_cat_pricing")
async def padm_cat_pricing(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text(await _premium_admin_text(), reply_markup=_padm_pricing_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "padm_cat_general")
async def padm_cat_general(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text(await _premium_admin_text(), reply_markup=_padm_general_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "padm_cat_unlock")
async def padm_cat_unlock(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text(await _premium_admin_text(), reply_markup=_padm_unlock_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "padm_cat_promo")
async def padm_cat_promo(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text(await _premium_admin_text(), reply_markup=_padm_promo_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("padm_promo_quick_"))
async def padm_promo_quick(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    hours = int(call.data.replace("padm_promo_quick_", ""))
    end_dt = datetime.now() + timedelta(hours=hours)
    await asyncio.gather(
        asyncio.to_thread(db.set_setting, "premium_promo_active", "1"),
        asyncio.to_thread(db.set_setting, "premium_promo_end", end_dt.isoformat()),
    )
    await call.answer(f"🔥 Chegirma yoqildi — {hours} soatlik countdown ishga tushdi!")
    await call.message.edit_text(await _premium_admin_text(), reply_markup=_padm_promo_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "padm_promo_off")
async def padm_promo_off(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await asyncio.to_thread(db.set_setting, "premium_promo_active", "0")
    await call.answer("🛑 Chegirma to'xtatildi")
    await call.message.edit_text(await _premium_admin_text(), reply_markup=_padm_promo_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "padm_promo_end")
async def padm_promo_end_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(PremiumAdminState.promo_end)
    await call.message.edit_text(
        "🗓 Chegirma tugash sanasi va vaqtini yuboring.\n\n"
        "Format: <code>25.12.2026 20:00</code>\n"
        "(WebApp'dagi Premium sahifasida shu vaqtgacha countdown ko'rsatiladi)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="padm_cat_promo")],
        ]),
        parse_mode="HTML"
    )

@dp.message(PremiumAdminState.promo_end)
async def padm_promo_end_save(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    text = (message.text or "").strip()
    try:
        end_dt = datetime.strptime(text, "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer("❌ Format noto'g'ri. Masalan: 25.12.2026 20:00 shaklida yuboring.")
        return
    if end_dt <= datetime.now():
        await message.answer("❌ Sana kelajakda bo'lishi kerak. Qaytadan yuboring.")
        return
    await asyncio.gather(
        asyncio.to_thread(db.set_setting, "premium_promo_active", "1"),
        asyncio.to_thread(db.set_setting, "premium_promo_end", end_dt.isoformat()),
    )
    await state.clear()
    await message.answer("✅ Saqlandi! Chegirma countdown yoqildi.")
    await message.answer(await _premium_admin_text(), reply_markup=_padm_promo_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "padm_promo_note")
async def padm_promo_note_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(PremiumAdminState.promo_note)
    await call.message.edit_text(
        "📝 Chegirma uchun qisqa izoh matnini yuboring (masalan: <code>-20% chegirma faqat bugun!</code>):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="padm_cat_promo")],
        ]),
        parse_mode="HTML"
    )

@dp.message(PremiumAdminState.promo_note)
async def padm_promo_note_save(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    note = (message.text or "").strip()[:120]
    await asyncio.to_thread(db.set_setting, "premium_promo_note", note)
    await state.clear()
    await message.answer("✅ Saqlandi!")
    await message.answer(await _premium_admin_text(), reply_markup=_padm_promo_kb(), parse_mode="HTML")

_PADM_TOGGLE_MAP = {
    "padm_toggle_enabled": "premium_enabled",
    "padm_toggle_1m": "premium_plan_1m_enabled",
    "padm_toggle_3m": "premium_plan_3m_enabled",
    "padm_toggle_1y": "premium_plan_1y_enabled",
}

@dp.callback_query(F.data.in_(list(_PADM_TOGGLE_MAP.keys())))
async def padm_toggle(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    key = _PADM_TOGGLE_MAP[call.data]
    current = await asyncio.to_thread(db.get_setting, key)
    current_on = (current or "1") == "1"
    new_val = "0" if current_on else "1"
    await asyncio.to_thread(db.set_setting, key, new_val)
    _invalidate_premium_cache()
    await call.answer("🟢 Yoqildi" if new_val == "1" else "🔴 O'chirildi")
    await call.message.edit_text(await _premium_admin_text(), reply_markup=_padm_kb_for(call.data), parse_mode="HTML")

@dp.callback_query(F.data == "padm_unlock_old")
async def padm_unlock_old(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.answer("Bajarilmoqda...")
    count = await asyncio.to_thread(db.unlock_all_old_episodes)
    await call.message.edit_text(
        f"✅ {count} ta qism qulfdan chiqarildi. Yangi qo'shiladigan qismlar odatdagidek "
        f"'oldinroq kirish' muddatiga tushaveradi.",
        reply_markup=_padm_unlock_kb()
    )

@dp.callback_query(F.data == "admin_premium")
async def admin_premium(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.clear()
    await call.message.edit_text(await _premium_admin_text(), reply_markup=_premium_admin_kb(), parse_mode="HTML")

# ---- ADMIN: FOYDALANUVCHIGA PREMIUM SOVG'A QILISH ----
# Do'stlar bir-biriga sovg'a qilgani kabi, admin ham istalgan foydalanuvchiga
# to'g'ridan-to'g'ri (to'lovsiz) Premium bera oladi.
def _admgift_plan_kb():
    rows = []
    for key in ("1m", "3m", "1y"):
        rows.append([InlineKeyboardButton(
            text=f"{PLAN_LABELS[key]} — {PLAN_DAYS[key]} kun",
            callback_data=f"admgift_plan_{key}", style="success"
        )])
    rows.append([InlineKeyboardButton(text="✏️ Boshqa muddat (kun)", callback_data="admgift_custom", style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_premium")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _admgift_grant(admin_id, target_id, plan, days):
    """extend_premium + sovg'a yozuvi + kesh tozalash + foydalanuvchiga xabar."""
    new_until = await asyncio.to_thread(db.extend_premium, target_id, days, plan)
    await asyncio.to_thread(db.record_premium_gift, admin_id, target_id, plan, days)
    _invalidate_sub_cache(target_id)  # Premium bo'ldi — majburiy obuna talabidan darhol ozod bo'lsin
    try:
        await bot.send_message(
            target_id,
            f"🎁 Sizga Premium sovg'a qilindi!\n\n📅 Amal qilish muddati: <b>{new_until.strftime('%d.%m.%Y')}</b> gacha",
            parse_mode="HTML"
        )
    except Exception:
        pass
    return new_until

@dp.callback_query(F.data == "padm_gift_start")
async def padm_gift_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(AdminPremiumGiftState.user_id)
    await call.message.edit_text(
        "🎁 Premium bermoqchi bo'lgan foydalanuvchining ID yoki @username'ini yozing:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_premium")],
        ])
    )

@dp.message(AdminPremiumGiftState.user_id)
async def padm_gift_target(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    query = message.text.strip()
    if query.startswith("@"):
        u = await asyncio.to_thread(db.get_user_by_username, query)
    else:
        try:
            u = await asyncio.to_thread(db.get_user, int(query))
        except Exception:
            u = None
    if not u:
        await message.answer("❌ Bunday foydalanuvchi topilmadi. Foydalanuvchi avval botdan foydalangan bo'lishi kerak.")
        return
    await state.update_data(admgift_to=u["user_id"])
    await state.set_state(AdminPremiumGiftState.choosing_plan)
    await message.answer(
        f"🎁 <b>{u['full_name']}</b> (<code>{u['user_id']}</code>) uchun muddatni tanlang:",
        reply_markup=_admgift_plan_kb(),
        parse_mode="HTML"
    )

@dp.message(AdminPremiumGiftState.choosing_plan)
async def padm_gift_choosing_plan_wrong(message: Message):
    await message.answer("Iltimos, yuqoridagi tugmalardan birini tanlang 👆")

@dp.callback_query(F.data.startswith("admgift_plan_"))
async def padm_gift_plan(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    data = await state.get_data()
    target_id = data.get("admgift_to")
    if not target_id:
        await call.answer("❌ Foydalanuvchi tanlanmagan, qaytadan boshlang.", show_alert=True)
        await state.clear()
        return
    plan = call.data.replace("admgift_plan_", "")
    days = PLAN_DAYS.get(plan, 30)
    new_until = await _admgift_grant(call.from_user.id, target_id, plan, days)
    await log_admin_action(call.from_user, "Premium sovg'a qildi", f"ID: {target_id}, {PLAN_LABELS.get(plan, plan)}")
    await state.clear()
    await call.message.edit_text(
        f"✅ Premium berildi!\n\n🆔 ID: <code>{target_id}</code>\n📅 Muddati: <b>{new_until.strftime('%d.%m.%Y')}</b> gacha",
        reply_markup=admin_back(),
        parse_mode="HTML"
    )
    await call.answer("✅ Berildi")

@dp.callback_query(F.data == "admgift_custom")
async def padm_gift_custom_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    data = await state.get_data()
    if not data.get("admgift_to"):
        await call.answer("❌ Foydalanuvchi tanlanmagan, qaytadan boshlang.", show_alert=True)
        await state.clear()
        return
    await state.set_state(AdminPremiumGiftState.custom_days)
    await call.message.edit_text(
        "✏️ Necha kunlik Premium berilsin? Faqat raqam yuboring (masalan: 7):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_premium")],
        ])
    )

@dp.message(AdminPremiumGiftState.custom_days)
async def padm_gift_custom_save(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    data = await state.get_data()
    target_id = data.get("admgift_to")
    if not target_id:
        await state.clear()
        await message.answer("❌ Xatolik: foydalanuvchi topilmadi. Qaytadan boshlang.", reply_markup=admin_back())
        return
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("❌ Faqat musbat raqam yuboring. Qaytadan urinib ko'ring.")
        return
    days = int(text)
    new_until = await _admgift_grant(message.from_user.id, target_id, "admin_gift", days)
    await log_admin_action(message.from_user, "Premium sovg'a qildi", f"ID: {target_id}, {days} kun")
    await state.clear()
    await message.answer(
        f"✅ Premium berildi!\n\n🆔 ID: <code>{target_id}</code>\n📅 Muddati: <b>{new_until.strftime('%d.%m.%Y')}</b> gacha",
        reply_markup=admin_back(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("admgift_direct_"))
async def padm_gift_direct(call: CallbackQuery, state: FSMContext):
    """Foydalanuvchi qidiruv kartasidagi '🎁 Premium berish' tugmasi — ID qayta
    kiritilmasdan to'g'ridan-to'g'ri muddat tanlash bosqichiga o'tadi."""
    if not await is_admin_user(call.from_user.id):
        return
    try:
        target_id = int(call.data.replace("admgift_direct_", ""))
    except Exception:
        await call.answer("❌ Xatolik", show_alert=True)
        return
    await state.update_data(admgift_to=target_id)
    await state.set_state(AdminPremiumGiftState.choosing_plan)
    await call.message.answer(
        f"🎁 <code>{target_id}</code> uchun muddatni tanlang:",
        reply_markup=_admgift_plan_kb(),
        parse_mode="HTML"
    )
    await call.answer()

_PADM_FIELD_MAP = {
    "padm_price_1m": ("premium_price_1m", PremiumAdminState.price_1m, "1 oylik narxni faqat raqam bilan yuboring (masalan: 15000):"),
    "padm_price_3m": ("premium_price_3m", PremiumAdminState.price_3m, "3 oylik narxni faqat raqam bilan yuboring (masalan: 40000):"),
    "padm_price_1y": ("premium_price_1y", PremiumAdminState.price_1y, "1 yillik narxni faqat raqam bilan yuboring (masalan: 120000):"),
    "padm_early": ("premium_early_hours", PremiumAdminState.early_hours, "Oldinroq kirish necha soat bo'lsin? (masalan: 48):"),
    "padm_ref": ("premium_referral_bonus_days", PremiumAdminState.referral_bonus, "Referal bonusi necha kun bo'lsin? (masalan: 3):"),
}

@dp.callback_query(F.data.in_(list(_PADM_FIELD_MAP.keys())))
async def padm_field_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    key, st, prompt = _PADM_FIELD_MAP[call.data]
    await state.set_state(st)
    back_cb="padm_cat_pricing" if call.data in _PADM_PRICING_KEYS else "padm_cat_general"
    await state.update_data(setting_key=key, padm_back=back_cb)
    await call.message.edit_text(prompt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data=back_cb)],
    ]))

@dp.message(PremiumAdminState.price_1m)
@dp.message(PremiumAdminState.price_3m)
@dp.message(PremiumAdminState.price_1y)
@dp.message(PremiumAdminState.early_hours)
@dp.message(PremiumAdminState.referral_bonus)
async def padm_field_save(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("setting_key")
    padm_back = data.get("padm_back", "admin_premium")
    value = (message.text or "").strip()
    if not value.isdigit():
        await message.answer("❌ Faqat raqam yuboring. Qaytadan urinib ko'ring.")
        return
    await asyncio.to_thread(db.set_setting, key, value)
    _invalidate_premium_cache()
    await state.clear()
    await message.answer("✅ Saqlandi!")
    kb = _padm_pricing_kb() if padm_back == "padm_cat_pricing" else _padm_general_kb()
    await message.answer(await _premium_admin_text(), reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "padm_card")
async def padm_card_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(PremiumAdminState.card)
    await call.message.edit_text(
        "💳 Karta raqami va (ixtiyoriy) egasining ismini yuboring.\n\n"
        "Format: <code>8600 1234 5678 9012 - Ism Familiya</code>\n"
        "(faqat karta raqamini ham yuborsangiz bo'ladi)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="padm_cat_general")],
        ]),
        parse_mode="HTML"
    )

@dp.message(PremiumAdminState.card)
async def padm_card_save(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    text = (message.text or "").strip()
    if "-" in text:
        card, holder = text.split("-", 1)
        card, holder = card.strip(), holder.strip()
    else:
        card, holder = text, ""
    await asyncio.to_thread(db.set_setting, "premium_card_number", card)
    await asyncio.to_thread(db.set_setting, "premium_card_holder", holder)
    _invalidate_premium_cache()
    await state.clear()
    await message.answer("✅ Saqlandi!")
    await message.answer(await _premium_admin_text(), reply_markup=_padm_general_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "padm_pending")
async def padm_pending(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    pending = await asyncio.to_thread(db.get_pending_payments)
    if not pending:
        await call.answer("Kutilayotgan to'lovlar yo'q", show_alert=True)
        return
    await call.answer()
    text = "📋 <b>Kutilayotgan to'lovlar:</b>\n\n" + "\n".join(
        f"#{p['id']} — ID <code>{p['user_id']}</code> — {PLAN_LABELS.get(p['plan'], p['plan'])} — {fmt_som(p['amount'])}"
        for p in pending
    )
    await call.message.answer(text, parse_mode="HTML")

def _premium_anime_kb(animes, page, total, per_page=10):
    total_pages = math.ceil(total / per_page) or 1
    buttons = []
    for a in animes:
        mark = "👑" if a.get("is_premium_only") else "⚪"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {a['title']}", callback_data=f"padm_pa_sel_{a['id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"padm_pa_page_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"padm_pa_page_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Premium sozlamalari", callback_data="admin_premium")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(F.data == "padm_premium_animes")
async def padm_premium_animes(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.clear()
    animes = await asyncio.to_thread(db.get_animes, None, 0, 10)
    total = await asyncio.to_thread(db.get_anime_count)
    await call.message.edit_text(
        "👑 <b>Premium animelar</b>\n\nAnimeni tanlang — butun anime yoki alohida qismlarini "
        "doimiy Premium-only qilib belgilashingiz mumkin.\n\n👑 = Premium-only, ⚪ = oddiy",
        reply_markup=_premium_anime_kb(animes, 0, total),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("padm_pa_page_"))
async def padm_premium_animes_page(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    page = int(call.data.split("_")[3])
    animes = await asyncio.to_thread(db.get_animes, None, page, 10)
    total = await asyncio.to_thread(db.get_anime_count)
    await call.message.edit_reply_markup(reply_markup=_premium_anime_kb(animes, page, total))

def _premium_anime_detail_kb(anime, episodes):
    mark = "🔓 Oddiy qilish" if anime.get("is_premium_only") else "👑 Premium-only qilish"
    buttons = [
        [InlineKeyboardButton(text=f"{mark} (butun anime)", callback_data=f"padm_pa_toggle_{anime['id']}", style="danger" if anime.get("is_premium_only") else "success")],
    ]
    if episodes:
        buttons.append([InlineKeyboardButton(text="🎬 Qismlarni alohida boshqarish", callback_data=f"padm_pa_eps_{anime['id']}_0")])
    buttons.append([InlineKeyboardButton(text="🔙 Ro'yxatga qaytish", callback_data="padm_premium_animes")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(F.data.startswith("padm_pa_sel_"))
async def padm_premium_anime_detail(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    anime_id = int(call.data.split("_")[3])
    anime = await asyncio.to_thread(db.get_anime, anime_id)
    if not anime:
        await call.answer("❌ Topilmadi", show_alert=True)
        return
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    status = "👑 Premium-only" if anime.get("is_premium_only") else "⚪ Oddiy"
    await call.message.edit_text(
        f"📌 <b>{anime['title']}</b>\n🆔 Kod: <code>{anime['id']}</code>\nHolati: {status}\n"
        f"🎬 Qismlar soni: {len(episodes)}",
        reply_markup=_premium_anime_detail_kb(anime, episodes),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("padm_pa_toggle_"))
async def padm_premium_anime_toggle(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    anime_id = int(call.data.split("_")[3])
    anime = await asyncio.to_thread(db.get_anime, anime_id)
    if not anime:
        await call.answer("❌ Topilmadi", show_alert=True)
        return
    new_val = not bool(anime.get("is_premium_only"))
    await asyncio.to_thread(db.set_anime_premium_only, anime_id, new_val)
    await call.answer("✅ Yangilandi")
    anime["is_premium_only"] = 1 if new_val else 0
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    status = "👑 Premium-only" if new_val else "⚪ Oddiy"
    await call.message.edit_text(
        f"📌 <b>{anime['title']}</b>\n🆔 Kod: <code>{anime['id']}</code>\nHolati: {status}\n"
        f"🎬 Qismlar soni: {len(episodes)}",
        reply_markup=_premium_anime_detail_kb(anime, episodes),
        parse_mode="HTML"
    )

def _premium_episodes_kb(anime_id, episodes, page, per_page=15):
    total_pages = math.ceil(len(episodes) / per_page) or 1
    start = page * per_page
    chunk = episodes[start:start + per_page]
    buttons = []
    row = []
    for ep in chunk:
        mark = "👑" if ep.get("is_premium_only") else "⚪"
        row.append(InlineKeyboardButton(text=f"{mark}{ep['episode_number']}", callback_data=f"padm_pa_ept_{ep['id']}_{anime_id}_{page}"))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"padm_pa_eps_{anime_id}_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"padm_pa_eps_{anime_id}_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Anime sahifasiga", callback_data=f"padm_pa_sel_{anime_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(F.data.startswith("padm_pa_eps_"))
async def padm_premium_episodes_list(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    parts = call.data.split("_")
    anime_id, page = int(parts[3]), int(parts[4])
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    await call.message.edit_text(
        "🎬 Qismni bosib, alohida Premium-only holatini almashtiring (👑/⚪):",
        reply_markup=_premium_episodes_kb(anime_id, episodes, page)
    )

@dp.callback_query(F.data.startswith("padm_pa_ept_"))
async def padm_premium_episode_toggle(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    parts = call.data.split("_")
    episode_id, anime_id, page = int(parts[3]), int(parts[4]), int(parts[5])
    ep = await asyncio.to_thread(db.get_episode, episode_id)
    if not ep:
        await call.answer("❌ Topilmadi", show_alert=True)
        return
    new_val = not bool(ep.get("is_premium_only"))
    await asyncio.to_thread(db.set_episode_premium_only, episode_id, new_val)
    await call.answer("👑 Premium-only qilindi" if new_val else "⚪ Oddiy qilindi")
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    await call.message.edit_reply_markup(reply_markup=_premium_episodes_kb(anime_id, episodes, page))

def main_reply_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎌 Animelarni ko'rish", web_app=WebAppInfo(url=WEBAPP_URL))],
    ], resize_keyboard=True)

def back_to_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Bosh menu", callback_data="main_menu", style="primary")]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📚 Kontent boshqaruvi", callback_data="admin_cat_content", style="primary"),
            InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="admin_cat_users", style="primary"),
        ],
        [
            InlineKeyboardButton(text="📊 Statistika", callback_data="admin_cat_stats", style="primary"),
            InlineKeyboardButton(text="📨 Muloqot", callback_data="admin_cat_comm", style="primary"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Sozlamalar", callback_data="admin_cat_settings", style="primary"),
            InlineKeyboardButton(text="💎 Premium", callback_data="admin_premium", style="success"),
        ],
        [
            InlineKeyboardButton(text="📖 Qo'llanma", callback_data="admin_help", style="primary"),
        ],
    ])

def admin_cat_content_keyboard():
    """Kontent boshqaruvi — endi 7 ta tugma bitta tekis roʻyxatda emas,
    2 kichik guruhga (Animelar / Qismlar) va Bannerlarga ajratilgan."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📀 Animelar", callback_data="admin_cat_content_anime", style="primary")],
        [InlineKeyboardButton(text="🎬 Qismlar", callback_data="admin_cat_content_episodes", style="primary")],
        [InlineKeyboardButton(text="🖼 Bannerlar", callback_data="admin_banners", style="success")],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ])

def admin_cat_content_anime_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Anime qo'shish", callback_data="admin_add", style="success"),
            InlineKeyboardButton(text="📋 Ro'yxat", callback_data="admin_list_0", style="primary"),
        ],
        [
            InlineKeyboardButton(text="✏️ Tahrirlash", callback_data="admin_edit", style="primary"),
            InlineKeyboardButton(text="🗑 O'chirish", callback_data="admin_delete", style="danger"),
        ],
        [InlineKeyboardButton(text="🔙 Kontent boshqaruvi", callback_data="admin_cat_content")],
    ])

def admin_cat_content_episodes_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Davom qo'shish", callback_data="admin_add_episode", style="success"),
            InlineKeyboardButton(text="✏️ Qismlarni tahrirlash", callback_data="admin_episodes", style="primary"),
        ],
        [InlineKeyboardButton(text="✂️ Qiziqarli joy kesish", callback_data="clip_start", style="success")],
        [InlineKeyboardButton(text="🔍 Videolarni tekshirish", callback_data="admin_check_videos", style="primary")],
        [InlineKeyboardButton(text="🔙 Kontent boshqaruvi", callback_data="admin_cat_content")],
    ])

def admin_cat_users_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔍 Foydalanuvchi", callback_data="admin_find_user", style="primary"),
            InlineKeyboardButton(text="👑 Admin qo'shish", callback_data="admin_add_admin", style="success"),
        ],
        [
            InlineKeyboardButton(text="🗑 Admin o'chirish", callback_data="admin_list_admins", style="danger"),
            InlineKeyboardButton(text="🚫 Bloklash", callback_data="admin_block", style="danger"),
        ],
        [InlineKeyboardButton(text="📜 Admin faoliyati", callback_data="admin_activity_log", style="primary")],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ])

def admin_cat_stats_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Statistika", callback_data="admin_stats", style="primary"),
            InlineKeyboardButton(text="📅 Hisobot", callback_data="admin_report", style="primary"),
        ],
        [InlineKeyboardButton(text="💰 Daromad", callback_data="admin_revenue", style="primary")],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ])

def admin_cat_comm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📨 Xabar yuborish", callback_data="admin_broadcast", style="success"),
            InlineKeyboardButton(text="💬 Izohlar", callback_data="admin_comments_anime", style="primary"),
        ],
        [
            InlineKeyboardButton(text="📢 Sponsor baner", callback_data="admin_sponsor", style="primary"),
        ],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ])

def admin_cat_settings_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📢 Kanallar", callback_data="admin_channels", style="primary"),
            InlineKeyboardButton(text="🔗 Havolalar", callback_data="admin_links", style="primary"),
        ],
        [
            InlineKeyboardButton(text="🔒 Kontent himoyasi", callback_data="admin_content", style="primary"),
            InlineKeyboardButton(text="🚫 So'z filtri", callback_data="admin_wordfilter", style="danger"),
        ],
        [
            InlineKeyboardButton(text="🔧 Texnik ishlar", callback_data="admin_maintenance", style="danger"),
        ],
        [
            InlineKeyboardButton(text="🎬 Avtomatik klip", callback_data="admin_autoclip", style="primary"),
        ],
        [
            InlineKeyboardButton(text="👤 Profil bo'limi (bepul)", callback_data="admin_profile_lock", style="danger"),
        ],
        [
            InlineKeyboardButton(text="🔒 Avtomatik bloklash", callback_data="admin_autoblock", style="danger"),
        ],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ])

def admin_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")]
    ])

def anime_list_keyboard(animes, media_type, page, total):
    per_page = 10
    total_pages = math.ceil(total / per_page) or 1
    buttons = []
    for a in animes:
        buttons.append([InlineKeyboardButton(
            text=a["title"], callback_data=f"anime_{a['id']}"
        )])
    nav = []
    prefix = "films" if media_type == "film" else "serials"
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🏠 Bosh menu", callback_data="main_menu", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def episodes_keyboard(episodes, anime_id, page=0, highlight_id=None):
    per_page = 6
    total_pages = math.ceil(len(episodes) / per_page) or 1
    start = page * per_page
    chunk = episodes[start:start + per_page]
    buttons = []
    row = []
    for ep in chunk:
        row.append(InlineKeyboardButton(
            text=f"{ep['episode_number']}-qism",
            callback_data=f"ep_{ep['id']}",
            style="success" if ep["id"] == highlight_id else None
        ))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"eps_{anime_id}_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"eps_{anime_id}_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Orqaga qaytish", callback_data=f"backcard_{anime_id}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

ANIME_STATUS_LABELS = {
    "ongoing": "🟢 Davom etmoqda",
    "finished": "✅ Tugagan",
}

def anime_card_text(anime):
    title = anime['title']
    status_text = ANIME_STATUS_LABELS.get(anime.get("status") or "ongoing", ANIME_STATUS_LABELS["ongoing"])
    return (
        f"🎬 {title}\n\n"
        f"📅 Yil     ➤ {anime['year']}\n"
        f"🌍 Davlat  ➤ {anime['country']}\n"
        f"🎭 Janr    ➤ {anime['genre']}\n"
        f"🌐 Til     ➤ {anime.get('language', 'Nomalum')}\n"
        f"📊 Holat   ➤ {status_text}\n\n"
        f"📖 Qisqacha:\n{anime['description']}"
    )

async def send_anime_card(chat_id, anime):
    protect = await asyncio.to_thread(db.get_setting, "content_protect") == "1"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬇️ Yuklab olish", callback_data=f"download_{anime['id']}_0", style="success"),
            InlineKeyboardButton(text="🎲 Random", callback_data="random", style="primary"),
        ],
        [InlineKeyboardButton(text="🏠 Bosh menu", callback_data="main_menu", style="primary")]
    ])
    try:
        await bot.send_photo(
            chat_id,
            photo=anime["photo_id"],
            caption=anime_card_text(anime),
            reply_markup=kb,
            parse_mode="HTML",
            protect_content=protect
        )
    except Exception:
        await bot.send_message(
            chat_id,
            anime_card_text(anime),
            reply_markup=kb,
            parse_mode="HTML"
        )

def announce_caption_text(anime):
    """E'lon kanali uchun qisqa post matni — botning ichidagi to'liq karta
    (anime_card_text)dan farqli, kanalda avvaldan qo'llanilgan qisqa uslubga
    mos: nom, yil, janr va kod, tavsifsiz."""
    return (
        f"🆕 Yangi anime qo'shildi!\n\n"
        f"📌 {anime['title']}\n"
        f"📅 {anime['year']} • 🎭 {anime['genre']}\n"
        f"🆔 Kod: {anime['id']}"
    )

async def post_anime_to_announce_channel(anime):
    """Yangi qo'shilgan animining qisqa e'loni (rasm + nom/yil/janr/kod)ni
    ochiq e'lon kanaliga post qiladi. Tugma bosilganda foydalanuvchi botga
    o'tib, deep-link (?start=anime_<id>) orqali to'g'ridan-to'g'ri 1-qismni
    oladi."""
    if not ANNOUNCE_CHANNEL:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="▶️ Tomosha qilish",
            url=f"https://t.me/{BOT_USERNAME}?start=anime_{anime['id']}",
            style="success"
        )]
    ])
    try:
        await bot.send_photo(
            ANNOUNCE_CHANNEL,
            photo=anime["photo_id"],
            caption=announce_caption_text(anime),
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"[post_anime_to_announce_channel] e'lon kanaliga post qilinmadi (anime_id={anime['id']}): {e}")

def search_method_keyboard(prefix):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Ro'yxatdan tanlash", callback_data=f"{prefix}_list")],
        [InlineKeyboardButton(text="🔍 Nomi orqali qidirish", callback_data=f"{prefix}_search")],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ])

def admin_anime_list_keyboard(animes, page, total, prefix):
    per_page = 10
    total_pages = math.ceil(total / per_page) or 1
    buttons = []
    for a in animes:
        icon = "🎬" if a["media_type"] == "film" else "📺"
        uploaded = a.get("episode_count") or 0
        if a["media_type"] == "film":
            label = f"{icon} {a['title']}"
        else:
            planned = a.get("total_episodes")
            label = f"{icon} {a['title']}  ·  {uploaded}/{planned if planned else uploaded} qism"
        buttons.append([InlineKeyboardButton(
            text=label, callback_data=f"{prefix}_{a['id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}_page_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}_page_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ===================== /HELP =====================
@dp.message(Command("help"))
async def help_handler(message: Message):
    support_url = await asyncio.to_thread(db.get_setting, "profile_support_url")
    text = (
        "🆘 <b>Yordam — botdan qanday foydalanish kerak</b>\n\n"
        "🔍 <b>Qidiruv</b> — anime nomini yozib qidiring\n\n"
        "🎬 <b>Anime Film</b> / 📺 <b>Anime Serial</b> — turlari bo'yicha roʻyxatni koʻring\n\n"
        "🎲 <b>Random</b> — tasodifiy anime tavsiya qiladi\n\n"
        "🎌 <b>«Animelarni koʻrish»</b> tugmasi mini-ilovani ochadi. U yerda:\n"
        "   • Barcha animelar va kategoriyalar\n"
        "   • ❤️ Sevimlilar — yoqqan animelaringizni saqlang\n"
        "   • 🕒 Tomosha tarixi — qaysi joyida toʻxtaganingiz eslab qolinadi\n"
        "   • 💬 Har bir anime ostiga izoh qoldirish\n"
        "   • 👤 Profil — statistika, obunalar va hisob sozlamalari\n\n"
        "🔔 <b>«Xabardor qil»</b> — anime sahifasida bosilsa, shu animega yangi "
        "qism yoki jonli efir qoʻshilganda sizga shaxsiy xabar keladi\n\n"
        "💎 <b>Premium</b> — reklamasiz tomosha, oldindan chiqqan qismlar va "
        "premium-only kontentga kirish imkonini beradi. Doʻstingizni referal "
        "havolangiz orqali taklif qilsangiz, u roʻyxatdan oʻtganda sizga "
        "bonus kunlar qoʻshiladi (Premium boʻlimida havolangizni topasiz).\n\n"
        "⚙️ <b>Buyruqlar:</b>\n"
        "/start — botni qayta ishga tushirish / bosh menu\n"
        "/help — shu yordam xabari\n"
    )
    if support_url:
        text += f"\n❓ Savol yoki muammo boʻlsa: {support_url}"
    await message.answer(text, parse_mode="HTML")

# ===================== /START =====================
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()

    # Deep link: /start ep_123
    args = message.text.split()

    # Deep link: /start premium (WebApp profildagi "Premium sotib olish" tugmasidan)
    if len(args) > 1 and args[1] == 'premium':
        u = await asyncio.to_thread(db.get_user, message.from_user.id)
        if u and u.get("is_blocked"):
            await message.answer("🚫 Siz bloklandingiz.")
            return
        text, kb = await build_premium_menu(message.from_user.id)
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    # Deep link: /start premium_1m / premium_3m / premium_1y (WebApp Premium
    # sahifasida foydalanuvchi tarifni tanlaganda) — to'g'ridan-to'g'ri o'sha
    # tarif uchun to'lov ko'rsatmasini ochamiz.
    if len(args) > 1 and args[1].startswith('premium_'):
        plan = args[1].replace('premium_', '', 1)
        if plan in PLAN_LABELS:
            u = await asyncio.to_thread(db.get_user, message.from_user.id)
            if u and u.get("is_blocked"):
                await message.answer("🚫 Siz bloklandingiz.")
                return
            status = await asyncio.to_thread(db.get_premium_status, message.from_user.id)
            if status["is_premium"] and status["days_left"] > PREMIUM_RENEWAL_WINDOW_DAYS:
                text, kb = await build_premium_menu(message.from_user.id)
                await message.answer(text, reply_markup=kb, parse_mode="HTML")
                return
            prices = await premium_settings()
            plan_flag = {"1m": "plan_1m_on", "3m": "plan_3m_on", "1y": "plan_1y_on"}.get(plan)
            if not prices["enabled"] or (plan_flag and not prices[plan_flag]) or not prices["card"]:
                text, kb = await build_premium_menu(message.from_user.id)
                await message.answer(text, reply_markup=kb, parse_mode="HTML")
                return
            amount = prices.get(plan)
            await state.set_state(PremiumState.waiting_screenshot)
            await state.update_data(plan=plan, amount=amount)
            card_line = f"💳 <code>{prices['card']}</code>"
            holder_line = f"\n👤 {prices['holder']}" if prices["holder"] else ""
            await message.answer(
                f"💳 <b>{PLAN_LABELS[plan]} — {fmt_som(amount)}</b>\n"
                f"Quyidagi kartaga to'lovni amalga oshiring:\n\n"
                f"{card_line}{holder_line}\n\n"
                f"💰 Summa: <b>{fmt_som(amount)}</b>\n\n"
                f"To'lovni amalga oshirgach, chekni shu yerga yuboring.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="premium_menu")],
                ]),
                parse_mode="HTML"
            )
            return

    # Deep link: /start anime_45 (kanal e'lonidagi "Tomosha qilish" tugmasidan) —
    # shu animening BIRINCHI qismini avtomatik yuboradi.
    if len(args) > 1 and args[1].startswith('anime_'):
        try:
            anime_id = int(args[1].split('_')[1])
        except Exception:
            anime_id = None
        if anime_id is not None:
            u = await asyncio.to_thread(db.get_user, message.from_user.id)
            if u and u.get("is_blocked"):
                await message.answer("🚫 Siz bloklandingiz.")
                return
            subscribed = await check_subscription(message.from_user.id)
            if not subscribed:
                await message.answer(
                    await sub_message_text(),
                    reply_markup=await sub_keyboard()
                )
                return
            episodes = await asyncio.to_thread(db.get_episodes, anime_id)
            if not episodes:
                await message.answer("❌ Bu anime uchun hali qism yuklanmagan.")
                return
            ep = episodes[0]
            if await is_episode_locked_for_user(ep, message.from_user.id):
                text, kb = await locked_episode_message(ep)
                await message.answer(text, reply_markup=kb, parse_mode="HTML")
                return
            protect = await asyncio.to_thread(db.get_setting, "content_protect") == "1"
            try:
                await bot.copy_message(
                    chat_id=message.chat.id,
                    from_chat_id=STORAGE_CHANNEL,
                    message_id=ep["channel_message_id"],
                    protect_content=protect
                )
            except Exception as e:
                logger.error(f"[start anime_ deep-link] video yuborilmadi (anime_id={anime_id}): {e}")
                await message.answer("❌ Videoni yuborishda xatolik yuz berdi. Keyinroq qayta urinib ko'ring.")
            return

    if len(args) > 1 and args[1].startswith('ep_'):
        try:
            episode_id = int(args[1].split('_')[1])
        except Exception:
            episode_id = None
        if episode_id is not None:
            u = await asyncio.to_thread(db.get_user, message.from_user.id)
            if u and u.get("is_blocked"):
                await message.answer("🚫 Siz bloklandingiz.")
                return
            subscribed = await check_subscription(message.from_user.id)
            if not subscribed:
                await message.answer(
                    await sub_message_text(),
                    reply_markup=await sub_keyboard()
                )
                return
            ep = await asyncio.to_thread(db.get_episode, episode_id)
            if ep:
                if await is_episode_locked_for_user(ep, message.from_user.id):
                    text, kb = await locked_episode_message(ep)
                    await message.answer(text, reply_markup=kb, parse_mode="HTML")
                    return
                protect = await asyncio.to_thread(db.get_setting, "content_protect") == "1"
                try:
                    await bot.copy_message(
                        chat_id=message.chat.id,
                        from_chat_id=STORAGE_CHANNEL,
                        message_id=ep["channel_message_id"],
                        protect_content=protect
                    )
                except Exception as e:
                    logger.error(f"[start ep_ deep-link] video yuborilmadi (ep_id={episode_id}, channel_message_id={ep['channel_message_id']}): {e}")
                    await message.answer(
                        "❌ Videoni yuborishda xatolik yuz berdi. Bu qism kanaldan o'chirilgan yoki botga ruxsat yo'q bo'lishi mumkin.\n\n"
                        "Admin bilan bog'laning yoki keyinroq qayta urinib ko'ring."
                    )
            else:
                await message.answer("❌ Epizod topilmadi.")
            return

    if await asyncio.to_thread(db.get_setting, "maintenance") == "1" and not await is_admin_user(message.from_user.id):
        await message.answer("🔧 Texnik ishlar olib borilmoqda.\nIltimos, kuting...")
        return

    user = message.from_user
    u = await asyncio.to_thread(db.get_user, user.id)

    if u and u.get("is_blocked"):
        await message.answer("🚫 Siz bloklandingiz.")
        return

    # Referal havola: /start ref_123456789
    referred_by = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_id = int(args[1].split("_")[1])
            if ref_id != user.id:
                referred_by = ref_id
        except Exception:
            pass

    if not u:
        if referred_by:
            await state.update_data(referred_by=referred_by)
        await message.answer(
            "🌸 <b>AniFilm Bot</b> ga xush kelibsiz!\n\n"
            "⚠️ <b>Diqqat:</b> Botni bloklasangiz yoki chiqib ketsangiz — "
            "avtomatik bloklanasiz va botdan foydalana olmaysiz!\n\n"
            "📌 Iltimos, quyidagi qoidalarni o'qib chiqing va qabul qiling.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Qabul qilaman", callback_data="accept_rules")]
            ]),
            parse_mode="HTML"
        )
    else:
        subscribed = await check_subscription(user.id)
        if not subscribed:
            await message.answer(
                await sub_message_text(),
                reply_markup=await sub_keyboard()
            )
            return
        await message.answer(
            f"👋 Salom, {user.full_name}!\n"
            f"🎌 AniFilm Bot ga xush kelibsiz\n\n"
            f"👇 Nimani qidiryapsiz?",
            reply_markup=main_keyboard()
        )

# Qabul qilaman bosilganda
async def _request_phone(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text(
        "📱 Botdan foydalanish uchun telefon raqamingizni yuboring:"
    )
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await call.message.answer("👇 Tugmani bosing:", reply_markup=kb)
    await state.set_state(RegState.phone)

@dp.callback_query(F.data == "accept_rules")
async def accept_rules(call: CallbackQuery, state: FSMContext):
    # Avval majburiy kanallarga obunani tekshiramiz — faqat obuna bo'lgandan
    # keyin telefon raqami so'raladi (ro'yxatdan o'tish shu bilan yakunlanadi).
    subscribed = await check_subscription(call.from_user.id)
    if not subscribed:
        await call.message.edit_text(
            await sub_message_text(),
            reply_markup=await sub_keyboard()
        )
        return
    await _request_phone(call, state)

# Raqam yuborilganda
@dp.message(RegState.phone, F.contact)
async def reg_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    referred_by = data.get("referred_by")
    await state.clear()
    user = message.from_user
    phone = message.contact.phone_number
    is_new = await asyncio.to_thread(db.add_user, user.id, user.username, user.full_name, phone, referred_by)

    if is_new and referred_by:
        prices = await premium_settings()
        try:
            await asyncio.to_thread(db.process_referral_bonus, referred_by, prices["ref_bonus"])
            await bot.send_message(
                referred_by,
                f"🎁 Sizning havolangiz orqali yangi foydalanuvchi qo'shildi!\n"
                f"Premium muddatingizga <b>+{prices['ref_bonus']} kun</b> qo'shildi.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    if is_new:
        u = await asyncio.to_thread(db.get_user, user.id)
        new_user_text = (
            f"👤 <b>Yangi foydalanuvchi!</b>\n\n"
            f"📌 Ism: {user.full_name}\n"
            f"🔢 Raqam: {u['join_number']}-chi\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"👤 Username: @{user.username or 'yoq'}\n"
            f"📱 Telefon: {phone}\n"
            f"📅 Sana: {u['joined_at'][:10]}\n\n"
            f"📊 Jami: {u['join_number']} ta foydalanuvchi"
        )
        new_user_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Profilni ko'rish", url=f"tg://user?id={user.id}")]
        ])
        for attempt in range(2):
            try:
                await bot.send_message(
                    ADMIN_ID, new_user_text, reply_markup=new_user_kb, parse_mode="HTML"
                )
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                continue
            except Exception as e:
                logger.warning(f"[reg_phone] yangi foydalanuvchi xabari yuborilmadi (user {user.id}): {e}")
                break

    # Klaviaturani yopish
    await message.answer("✅ Ro'yxatdan o'tdingiz!", reply_markup=ReplyKeyboardRemove())

    # Obuna tekshirish
    subscribed = await check_subscription(user.id)
    if not subscribed:
        await message.answer(
            await sub_message_text(),
            reply_markup=await sub_keyboard()
        )
        return

    await message.answer(
        f"👋 Salom, {user.full_name}!\n"
        f"🎌 AniFilm Bot ga xush kelibsiz\n\n"
        f"👇 Nimani qidiryapsiz?",
        reply_markup=main_keyboard()
    )

@dp.callback_query(F.data == "check_sub")
async def check_sub_handler(call: CallbackQuery, state: FSMContext):
    # Foydalanuvchi endigina kanalga obuna bo'lgan bo'lishi mumkin — eski
    # keshlangan (obuna emas) natijaga ishonmasdan, majburiy yangilab tekshiramiz.
    _invalidate_sub_cache(call.from_user.id)
    subscribed = await check_subscription(call.from_user.id)
    if not subscribed:
        await call.answer("❌ Hali obuna bolmadingiz!", show_alert=True)
        return

    await call.answer()
    u = await asyncio.to_thread(db.get_user, call.from_user.id)
    if not u:
        # Hali ro'yxatdan o'tmagan — obuna endi tasdiqlandi, navbatdagi qadam
        # telefon raqamini so'rash (ro'yxatdan o'tish shu bilan tugaydi).
        await _request_phone(call, state)
        return

    await call.message.edit_text(
        f"👋 Salom, {call.from_user.full_name}!\n"
        f"🎌 AniFilm Bot ga xush kelibsiz\n\n"
        f"👇 Nimani qidiryapsiz?",
        reply_markup=main_keyboard()
    )

# ===================== BOT BLOKLANSA =====================
@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED))
async def user_blocked_bot(event: ChatMemberUpdated):
    await mark_user_left(event.from_user.id, tg_user=event.from_user)

# ===================== BOSH MENU =====================
@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await guard_access(call):
        return
    await call.answer()
    text = (
        f"👋 Salom, {call.from_user.full_name}!\n"
        f"🎌 AniFilm Bot ga xush kelibsiz\n\n"
        f"👇 Nimani qidiryapsiz?"
    )
    # Foto xabar bo'lsa edit_text ishlamaydi — delete qilib yangi yuborish
    try:
        await call.message.edit_text(text, reply_markup=main_keyboard())
    except Exception:
        try:
            await call.message.delete()
        except Exception:
            pass
        await bot.send_message(call.message.chat.id, text, reply_markup=main_keyboard())

@dp.callback_query(F.data == "noop")
async def noop_handler(call: CallbackQuery):
    await call.answer()

# ===================== QIDIRUV =====================
@dp.callback_query(F.data == "search")
async def search_callback(call: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.query)
    await call.message.edit_text(
        "🔍 Anime nomini yozing (to'liq nom):",
        reply_markup=back_to_main()
    )

@dp.message(SearchState.query)
async def search_result(message: Message, state: FSMContext):
    await state.clear()
    if not await guard_access(message, is_callback=False):
        return
    query = message.text.strip()
    results = await asyncio.to_thread(db.search_anime, query)
    if not results:
        await message.answer(
            f"❌ <b>{query}</b> topilmadi.",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
        return
    buttons = []
    for a in results[:10]:
        icon = "🎬" if a["media_type"] == "film" else "📺"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {a['title']}",
            callback_data=f"anime_{a['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="🏠 Bosh menu", callback_data="main_menu", style="primary")])
    await message.answer(
        f"🔍 <b>{query}</b> natijalari:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )

# ===================== FILMLAR =====================
@dp.callback_query(F.data.startswith("films_"))
async def films_list(call: CallbackQuery):
    if not await guard_access(call):
        return
    await call.answer()
    page = int(call.data.split("_")[1])
    animes = await asyncio.to_thread(db.get_animes, "film", page)
    total = await asyncio.to_thread(db.get_anime_count, "film")
    if not animes:
        await call.answer("🎬 Hozircha film yoq!", show_alert=True)
        return
    await call.message.edit_text(
        "🎬 <b>Anime Filmlar</b>",
        reply_markup=anime_list_keyboard(animes, "film", page, total),
        parse_mode="HTML"
    )

# ===================== SERIALLAR =====================
@dp.callback_query(F.data.startswith("serials_"))
async def serials_list(call: CallbackQuery):
    if not await guard_access(call):
        return
    await call.answer()
    page = int(call.data.split("_")[1])
    animes = await asyncio.to_thread(db.get_animes, "serial", page)
    total = await asyncio.to_thread(db.get_anime_count, "serial")
    if not animes:
        await call.answer("📺 Hozircha serial yoq!", show_alert=True)
        return
    await call.message.edit_text(
        "📺 <b>Anime Seriallar</b>",
        reply_markup=anime_list_keyboard(animes, "serial", page, total),
        parse_mode="HTML"
    )

# ===================== ANIME KARTOCHKASI =====================
@dp.callback_query(F.data.startswith("anime_"))
async def anime_detail(call: CallbackQuery):
    if not await guard_access(call):
        return
    await call.answer()
    parts = call.data.split("_")
    if len(parts) < 2:
        return
    try:
        anime_id = int(parts[1])
    except Exception:
        return
    anime = await asyncio.to_thread(db.get_anime, anime_id)
    if not anime:
        await call.answer("❌ Topilmadi", show_alert=True)
        return
    await asyncio.to_thread(db.increment_views, anime_id)
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_anime_card(call.message.chat.id, anime)

# ===================== YUKLAB OLISH =====================
@dp.callback_query(F.data.startswith("download_"))
async def download_handler(call: CallbackQuery):
    if not await guard_access(call):
        return
    await call.answer()
    parts = call.data.split("_")
    anime_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    anime = await asyncio.to_thread(db.get_anime, anime_id)
    if not anime:
        await call.answer("❌ Topilmadi", show_alert=True)
        return
    protect = await asyncio.to_thread(db.get_setting, "content_protect") == "1"
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    if not episodes:
        await call.answer("❌ Video hali yuklanmagan!", show_alert=True)
        return
    if anime["media_type"] == "film":
        ep = episodes[0]
        if await is_episode_locked_for_user(ep, call.from_user.id):
            await call.answer("👑 Bu film faqat Premium foydalanuvchilar uchun ochiq", show_alert=True)
            text, kb = await locked_episode_message(ep, anime)
            try:
                await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            return
        video_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data=f"backcard_{anime_id}", style="primary")],
        ])
        try:
            await bot.copy_message(
                call.message.chat.id,
                STORAGE_CHANNEL,
                ep["channel_message_id"],
                protect_content=protect,
                reply_markup=video_kb
            )
        except Exception as e:
            logger.error(f"[download_handler] film yuborilmadi (anime_id={anime_id}, channel_message_id={ep['channel_message_id']}): {e}")
            await call.answer(
                "❌ Videoni yuborishda xatolik yuz berdi. Bu film kanaldan o'chirilgan yoki botga ruxsat yo'q bo'lishi mumkin.",
                show_alert=True
            )
            return
        try:
            await call.message.delete()
        except Exception:
            pass
    else:
        await call.message.edit_reply_markup(
            reply_markup=episodes_keyboard(episodes, anime_id, page)
        )

@dp.callback_query(F.data.startswith("backcard_"))
async def backcard_handler(call: CallbackQuery):
    if not await guard_access(call):
        return
    await call.answer()
    anime_id = int(call.data.split("_")[1])
    anime = await asyncio.to_thread(db.get_anime, anime_id)
    if not anime:
        await call.answer("❌ Topilmadi", show_alert=True)
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_anime_card(call.message.chat.id, anime)

@dp.callback_query(F.data.startswith("eps_"))
async def episodes_page(call: CallbackQuery):
    parts = call.data.split("_")
    anime_id = int(parts[1])
    page = int(parts[2])
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    await call.message.edit_reply_markup(
        reply_markup=episodes_keyboard(episodes, anime_id, page)
    )

def video_episodes_keyboard(episodes, anime_id, page=0, highlight_id=None):
    """episodes_keyboard bilan bir xil, lekin video xabari ostida ishlatiladi:
    sahifalash tugmalari alohida 'epv_' prefiksi bilan (video kontekstida ekanini bildirish uchun),
    joriy qism ID'si ham callback ichida saqlanadi — shu orqali sahifa almashtirilganda ham
    joriy tomosha qilinayotgan qism yashil rangda qolaveradi."""
    kb = episodes_keyboard(episodes, anime_id, page, highlight_id)
    hid = highlight_id or 0
    new_rows = []
    for row in kb.inline_keyboard:
        new_row = []
        for btn in row:
            if btn.callback_data and btn.callback_data.startswith("eps_"):
                _, aid, pg = btn.callback_data.split("_")
                new_row.append(InlineKeyboardButton(text=btn.text, callback_data=f"epv_{aid}_{pg}_{hid}"))
            else:
                new_row.append(btn)
        new_rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=new_rows)

@dp.callback_query(F.data.startswith("epv_"))
async def episodes_page_video(call: CallbackQuery):
    parts = call.data.split("_")
    anime_id = int(parts[1])
    page = int(parts[2])
    highlight_id = int(parts[3]) if len(parts) > 3 and int(parts[3]) else None
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    await call.message.edit_reply_markup(
        reply_markup=video_episodes_keyboard(episodes, anime_id, page, highlight_id)
    )

def _episode_locked(episode, user_id, prices, is_premium, anime=None):
    """is_episode_locked_for_user bilan bir xil mantiq, lekin tayyor
    prices/is_premium qiymatlari bilan — har bir epizod uchun qayta
    DB'ga bormaydi (webapp_anime_detail'dagi tsiklda ishlatiladi)."""
    if user_id == ADMIN_ID:
        return False
    # Doimiy Premium-only cheklov (vaqtdan qat'iy nazar) — anime yoki aynan shu qism
    if episode.get("is_premium_only") or (anime and anime.get("is_premium_only")):
        return not is_premium
    if not prices["enabled"]:
        return False
    early_hours = prices["early_hours"]
    if early_hours <= 0:
        return False
    created_at = episode.get("created_at")
    if not created_at:
        return False
    try:
        created_dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    if datetime.now() - created_dt >= timedelta(hours=early_hours):
        return False
    return not is_premium

async def is_episode_locked_for_user(episode, user_id):
    """Agar epizod (yoki uning animesi) doimiy Premium-only qilib belgilangan bo'lsa,
    yoki hali 'oldinroq kirish' muddatida bo'lsa va foydalanuvchi Premium bo'lmasa, True qaytaradi."""
    if user_id == ADMIN_ID:
        return False
    status = await asyncio.to_thread(db.get_premium_status, user_id)
    is_premium = status["is_premium"]

    # Doimiy Premium-only cheklov — vaqtdan qat'iy nazar
    if episode.get("is_premium_only"):
        return not is_premium
    anime_id = episode.get("anime_id")
    if anime_id:
        anime = await asyncio.to_thread(db.get_anime, anime_id)
        if anime and anime.get("is_premium_only"):
            return not is_premium

    prices = await premium_settings()
    if not prices["enabled"]:
        return False
    early_hours = prices["early_hours"]
    if early_hours <= 0:
        return False
    created_at = episode.get("created_at")
    if not created_at:
        return False
    try:
        created_dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    if datetime.now() - created_dt >= timedelta(hours=early_hours):
        return False
    return not is_premium

async def locked_episode_message(episode, anime=None):
    """Qulflangan qism uchun foydalanuvchiga ko'rsatiladigan matn va
    '💎 Premium sotib olish' tugmasi bilan klaviaturani qaytaradi."""
    permanent = bool(episode.get("is_premium_only")) or bool(anime and anime.get("is_premium_only"))
    if not permanent and episode.get("anime_id") and not anime:
        a = await asyncio.to_thread(db.get_anime, episode["anime_id"])
        if a and a.get("is_premium_only"):
            permanent = True
    if permanent:
        text = (
            "👑 <b>Bu qism faqat Premium foydalanuvchilar uchun mavjud.</b>\n\n"
            "Cheklovsiz tomosha qilish uchun Premium sotib oling 👇"
        )
    else:
        prices = await premium_settings()
        text = (
            f"👑 <b>Bu qism hozircha faqat Premium foydalanuvchilar uchun ochiq</b>\n\n"
            f"({prices['early_hours']} soatdan keyin hammaga ochiladi)\n"
            f"Hoziroq ko'rish uchun Premium sotib oling 👇"
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Premium sotib olish", callback_data="premium_menu", style="success")],
    ])
    return text, kb

@dp.callback_query(F.data.regexp(r"^ep_\d+$"))
async def episode_handler(call: CallbackQuery):
    if not await guard_access(call):
        return
    await call.answer()
    episode_id = int(call.data.split("_")[1])
    ep = await asyncio.to_thread(db.get_episode, episode_id)
    if not ep:
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    if await is_episode_locked_for_user(ep, call.from_user.id):
        text, kb = await locked_episode_message(ep)
        try:
            await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        return

    protect = await asyncio.to_thread(db.get_setting, "content_protect") == "1"

    all_eps = await asyncio.to_thread(db.get_episodes, ep["anime_id"])
    all_eps_sorted = sorted(all_eps, key=lambda x: x["episode_number"])
    idx = next((i for i, e in enumerate(all_eps_sorted) if e["id"] == episode_id), 0)
    page = idx // 6
    video_kb = video_episodes_keyboard(all_eps_sorted, ep["anime_id"], page, episode_id)

    # Avvalgi xabarni (kartochka yoki oldingi video) o'chirib, o'rniga yangisini yuboramiz.
    try:
        await call.message.delete()
    except Exception:
        pass

    try:
        await bot.copy_message(
            call.message.chat.id,
            STORAGE_CHANNEL,
            ep["channel_message_id"],
            protect_content=protect,
            reply_markup=video_kb
        )
    except Exception as e:
        logger.error(f"[episode_handler] video yuborilmadi (ep_id={episode_id}, channel_message_id={ep['channel_message_id']}): {e}")
        await bot.send_message(
            call.message.chat.id,
            "❌ Videoni yuborishda xatolik yuz berdi. Bu qism kanaldan o'chirilgan yoki botga ruxsat yo'q bo'lishi mumkin.\n\n"
            "Admin bilan bog'laning yoki keyinroq qayta urinib ko'ring."
        )

# ===================== RANDOM =====================
@dp.callback_query(F.data == "random")
async def random_handler(call: CallbackQuery):
    if not await guard_access(call):
        return
    await call.answer()
    anime = await asyncio.to_thread(db.get_random_anime)
    if not anime:
        await call.answer("❌ Hozircha anime yoq!", show_alert=True)
        return
    await asyncio.to_thread(db.increment_views, anime["id"])
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_anime_card(call.message.chat.id, anime)

# ===================== /ADMIN =====================
@dp.message(Command("fixstatus"))
async def fixstatus_handler(message: Message):
    """Bir martalik tuzatish: barcha animelarni 'Tugagan' holatiga o'tkazadi."""
    if not await is_admin_user(message.from_user.id):
        await message.answer("❌ Ruxsat yoq!")
        return
    updated = await asyncio.to_thread(db.finish_all_animes)
    await message.answer(f"✅ {updated} ta anime 'Tugagan' holatiga o'tkazildi.")

@dp.message(Command("admin"))
async def admin_handler(message: Message):
    if not await is_admin_user(message.from_user.id):
        await message.answer("❌ Ruxsat yoq!")
        return
    await message.answer("👑 <b>Admin Panel</b>", reply_markup=admin_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_back")
async def admin_back_handler(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.clear()
    await call.message.edit_text("👑 <b>Admin Panel</b>", reply_markup=admin_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_cat_content")
async def admin_cat_content(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text("📚 <b>Kontent boshqaruvi</b>", reply_markup=admin_cat_content_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_cat_content_anime")
async def admin_cat_content_anime(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text("📀 <b>Animelar</b>", reply_markup=admin_cat_content_anime_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_cat_content_episodes")
async def admin_cat_content_episodes(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text("🎬 <b>Qismlar</b>", reply_markup=admin_cat_content_episodes_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_cat_users")
async def admin_cat_users(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text("👥 <b>Foydalanuvchilar</b>", reply_markup=admin_cat_users_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_cat_stats")
async def admin_cat_stats(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text("📊 <b>Statistika</b>", reply_markup=admin_cat_stats_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_cat_comm")
async def admin_cat_comm(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text("📨 <b>Muloqot</b>", reply_markup=admin_cat_comm_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_cat_settings")
async def admin_cat_settings(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text("⚙️ <b>Sozlamalar</b>", reply_markup=admin_cat_settings_keyboard(), parse_mode="HTML")

# ---- ANIME QO'SHISH ----
@dp.callback_query(F.data == "admin_add")
async def admin_add(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(AddAnime.photo)
    await call.message.edit_text("🖼 Anime rasmini yuboring:", reply_markup=admin_back())

@dp.message(AddAnime.photo, F.photo)
async def add_photo(message: Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddAnime.title)
    await message.answer("📌 Anime nomini yozing:")

@dp.message(AddAnime.title)
async def add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(AddAnime.year)
    await message.answer("📅 Yilini yozing:")

@dp.message(AddAnime.year)
async def add_year(message: Message, state: FSMContext):
    await state.update_data(year=message.text)
    await state.set_state(AddAnime.country)
    await message.answer("🌍 Davlatini yozing:")

@dp.message(AddAnime.country)
async def add_country(message: Message, state: FSMContext):
    await state.update_data(country=message.text)
    await state.set_state(AddAnime.genre)
    await message.answer("🎭 Janrini yozing:")

@dp.message(AddAnime.genre)
async def add_genre(message: Message, state: FSMContext):
    await state.update_data(genre=message.text)
    await state.set_state(AddAnime.description)
    await message.answer("📝 Qisqa malumot yozing:")

@dp.message(AddAnime.description)
async def add_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(AddAnime.language)
    await message.answer("🗣 Tilini yozing (masalan: O'zbek, Rus, Yapon):")

@dp.message(AddAnime.language)
async def add_language(message: Message, state: FSMContext):
    await state.update_data(language=message.text)
    await state.set_state(AddAnime.media_type)
    await message.answer(
        "🎬 Turi qanday?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 Film", callback_data="set_type_film"),
                InlineKeyboardButton(text="📺 Serial", callback_data="set_type_serial"),
            ]
        ])
    )

def _status_choice_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Davom etmoqda", callback_data="addstatus_ongoing")],
        [InlineKeyboardButton(text="✅ Tugagan", callback_data="addstatus_finished")],
    ])

@dp.callback_query(F.data.in_(["set_type_film", "set_type_serial"]))
async def set_type(call: CallbackQuery, state: FSMContext):
    media_type = "film" if call.data == "set_type_film" else "serial"
    await state.update_data(media_type=media_type, video_ids=[])
    if media_type == "serial":
        await state.set_state(AddAnime.total_episodes)
        await call.message.edit_text(
            "🔢 Jami nechta qism bo'ladi? (hali aniq bo'lmasa /skip yozing)"
        )
    else:
        await state.update_data(total_episodes=None)
        await state.set_state(AddAnime.status)
        await call.message.edit_text("📊 Holatini tanlang:", reply_markup=_status_choice_keyboard())

@dp.message(AddAnime.total_episodes, Command("skip"))
async def add_total_episodes_skip(message: Message, state: FSMContext):
    await state.update_data(total_episodes=None)
    await state.set_state(AddAnime.status)
    await message.answer("📊 Holatini tanlang:", reply_markup=_status_choice_keyboard())

@dp.message(AddAnime.total_episodes)
async def add_total_episodes(message: Message, state: FSMContext):
    try:
        total = int(message.text.strip())
        if total <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Iltimos, musbat butun son kiriting (masalan: 24) yoki /skip yozing.")
        return
    await state.update_data(total_episodes=total)
    await state.set_state(AddAnime.status)
    await message.answer("📊 Holatini tanlang:", reply_markup=_status_choice_keyboard())

@dp.callback_query(AddAnime.status, F.data.in_(["addstatus_ongoing", "addstatus_finished"]))
async def add_status(call: CallbackQuery, state: FSMContext):
    status = call.data.replace("addstatus_", "")
    await state.update_data(status=status)
    await state.set_state(AddAnime.videos)
    await call.message.edit_text("🎬 Videolarni yuboring. Tugagach /done yozing:")

@dp.message(AddAnime.videos, F.video)
async def add_video(message: Message, state: FSMContext):
    data = await state.get_data()
    video_ids = data.get("video_ids", [])
    ep_num = len(video_ids) + 1
    sent = await bot.copy_message(
        STORAGE_CHANNEL, message.chat.id, message.message_id,
        caption=episode_caption(ep_num), parse_mode=None
    )
    video_ids.append(sent.message_id)
    await state.update_data(video_ids=video_ids)
    await message.answer(f"✅ {len(video_ids)}-video kanalga saqlandi. /done yozing yoki davom eting.")

@dp.message(AddAnime.videos, Command("done"))
async def add_done(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("video_ids"):
        await message.answer("❌ Video yuklanmadi!")
        return
    anime_id = await asyncio.to_thread(db.add_anime, data["title"], data["year"], data["country"],
        data["genre"], data["description"], data.get("language", "Nomalum"), data["photo_id"], data["media_type"],
        data.get("total_episodes"), data.get("status", "ongoing"))
    for i, msg_id in enumerate(data["video_ids"], 1):
        await asyncio.to_thread(db.add_episode, anime_id, i, msg_id)
    await state.clear()

    # Yangi anime kartasini ochiq e'lon kanaliga post qilish (ANNOUNCE_CHANNEL
    # o'rnatilgan bo'lsa). anime dict'ini db'dan qayta olamiz — chunki
    # add_anime natijasi faqat ID, to'liq maydonlar emas.
    if ANNOUNCE_CHANNEL:
        new_anime = await asyncio.to_thread(db.get_anime, anime_id)
        if new_anime:
            await post_anime_to_announce_channel(new_anime)

    # Reklama klipi — 1-qismdan avtomatik yaratiladi (fonda, javobni bloklamaydi)
    if await is_auto_clip_enabled() and data["video_ids"]:
        asyncio.create_task(_auto_generate_highlight_clip(message, data["video_ids"][0]))

    # Faqat BLOKLNMAGAN foydalanuvchilarga xabar
    users = await asyncio.to_thread(db.get_all_active_users)
    for user_id in users:
        try:
            await bot.send_message(
                user_id,
                f"🆕 Yangi anime qo'shildi!\n\n📌 {data['title']}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="👁 Ko'rish", callback_data=f"anime_{anime_id}")]
                ])
            )
            await asyncio.sleep(0.05)  # flood limiti uchun
        except Exception:
            pass

    total = data.get("total_episodes")
    progress_line = f"\n📦 Yuklandi: {len(data['video_ids'])}/{total} qism" if total else f"\n📹 {len(data['video_ids'])} ta video"
    await log_admin_action(message.from_user, "Anime qo'shdi", f"{data['title']} ({len(data['video_ids'])} qism)")
    # Webapp 🔔 paneli uchun bildirishnoma
    await asyncio.to_thread(
        db.create_notification, "anime",
        f"Yangi anime qo'shildi: {data['title']}",
        anime_id=anime_id
    )
    await message.answer(
        f"✅ <b>{data['title']}</b> qoshildi!{progress_line}",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )

# ---- DAVOM QO'SHISH ----
@dp.callback_query(F.data == "admin_add_episode")
async def admin_add_episode(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text(
        "➕ Davom qo'shish — serial tanlash usuli:",
        reply_markup=search_method_keyboard("addepi")
    )

@dp.callback_query(F.data == "addepi_list")
async def addepi_list(call: CallbackQuery, state: FSMContext):
    animes = await asyncio.to_thread(db.get_animes, "serial", 0)
    total = await asyncio.to_thread(db.get_anime_count, "serial")
    if not animes:
        await call.answer("📺 Hozircha serial yoq!", show_alert=True)
        return
    await state.set_state(AddEpisode.choose_anime)
    await call.message.edit_text(
        "📺 Serialni tanlang:",
        reply_markup=admin_anime_list_keyboard(animes, 0, total, "addepi_sel")
    )

@dp.callback_query(F.data.startswith("addepi_sel_page_"))
async def addepi_sel_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split("_")[-1])
    animes = await asyncio.to_thread(db.get_animes, "serial", page)
    total = await asyncio.to_thread(db.get_anime_count, "serial")
    await call.message.edit_text(
        "📺 Serialni tanlang:",
        reply_markup=admin_anime_list_keyboard(animes, page, total, "addepi_sel")
    )

@dp.callback_query(F.data == "addepi_search")
async def addepi_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddEpisode.choose_method)
    await call.message.edit_text("🔍 Serial nomini yozing:")

@dp.message(AddEpisode.choose_method)
async def addepi_search_result(message: Message, state: FSMContext):
    results = await asyncio.to_thread(db.search_anime, message.text.strip())
    serials = [a for a in results if a["media_type"] == "serial"]
    if not serials:
        await message.answer("❌ Topilmadi!")
        return
    await state.set_state(AddEpisode.choose_anime)
    buttons = [[InlineKeyboardButton(text=a["title"], callback_data=f"addepi_sel_{a['id']}")] for a in serials]
    await message.answer("Tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("addepi_sel_"))
async def addepi_selected(call: CallbackQuery, state: FSMContext):
    anime_id = int(call.data.split("_")[2])
    anime = await asyncio.to_thread(db.get_anime, anime_id)
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    next_ep = len(episodes) + 1
    await state.update_data(episode_anime_id=anime_id, episode_msg_ids=[], next_ep=next_ep)
    await state.set_state(AddEpisode.videos)
    total = anime.get("total_episodes") if anime else None
    progress_line = f"\n📦 Hozircha: {len(episodes)}/{total} qism yuklangan." if total else f"\n📦 Hozircha: {len(episodes)} qism yuklangan."
    await call.message.edit_text(
        f"🎬 Videolarni yuboring ({next_ep}-qismdan boshlanadi).{progress_line}\nTugagach /done yozing:"
    )

@dp.message(AddEpisode.videos, F.video)
async def addepi_video(message: Message, state: FSMContext):
    data = await state.get_data()
    msg_ids = data.get("episode_msg_ids", [])
    ep_num = data["next_ep"] + len(msg_ids)
    new_msg_id = await _upload_episode_to_storage(
        message.chat.id, message.message_id,
        episode_caption(ep_num), status_message=message
    )
    msg_ids.append(new_msg_id)
    await state.update_data(episode_msg_ids=msg_ids)
    await message.answer(f"✅ {ep_num}-qism kanalga saqlandi.")

@dp.message(AddEpisode.videos, Command("done"))
async def addepi_done(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("episode_msg_ids"):
        await message.answer("❌ Video yuklanmadi!")
        return
    for i, msg_id in enumerate(data["episode_msg_ids"]):
        await asyncio.to_thread(db.add_episode, data["episode_anime_id"], data["next_ep"] + i, msg_id)
    await state.clear()

    # Reklama klipi — shu safar yuklangan birinchi (eng yangi) qismdan avtomatik
    # yaratiladi (fonda, javobni bloklamaydi)
    if await is_auto_clip_enabled() and data["episode_msg_ids"]:
        asyncio.create_task(_auto_generate_highlight_clip(message, data["episode_msg_ids"][0]))

    # Obunachilarga shaxsiy xabar — yangi qism chiqqani haqida
    anime_row = await asyncio.to_thread(db.get_anime, data["episode_anime_id"])
    if anime_row:
        eps = await asyncio.to_thread(db.get_episodes, data["episode_anime_id"])
        new_ep = next((e for e in eps if e["episode_number"] == data["next_ep"]), None)
        if new_ep:
            asyncio.create_task(notify_anime_subscribers(
                data["episode_anime_id"],
                f"🎬 <b>{anime_row['title']}</b> — {data['next_ep']}-qism chiqdi!\n\n"
                f"👉 https://t.me/{BOT_USERNAME}?start=ep_{new_ep['id']}"
            ))
        # Webapp 🔔 paneli uchun bildirishnoma
        await asyncio.to_thread(
            db.create_notification, "episode",
            f"{anime_row['title']} — {data['next_ep']}-qism chiqdi!",
            anime_id=data["episode_anime_id"]
        )

    await message.answer(
        f"✅ {len(data['episode_msg_ids'])} ta qism qoshildi!",
        reply_markup=admin_keyboard()
    )

# ---- ANIME RO'YXATI ADMIN ----
@dp.callback_query(F.data.regexp(r"^admin_list_\d+$"))
async def admin_list(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    page = int(call.data.split("_")[2])
    animes = await asyncio.to_thread(db.get_animes, page=page)
    total = await asyncio.to_thread(db.get_anime_count)
    per_page = 10
    total_pages = math.ceil(total / per_page) or 1
    buttons = []
    for a in animes:
        icon = "🎬" if a["media_type"] == "film" else "📺"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {a['title']}", callback_data=f"alist_{a['id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin_list_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin_list_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")])
    await call.message.edit_text(
        "📋 <b>Anime royxati</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("alist_"))
async def alist_detail(call: CallbackQuery):
    anime_id = int(call.data.split("_")[1])
    anime = await asyncio.to_thread(db.get_anime, anime_id)
    if not anime:
        await call.answer("❌ Topilmadi")
        return
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    await call.message.edit_text(
        f"<b>{anime['title']}</b>\n"
        f"📅 {anime['year']} | 🌍 {anime['country']}\n"
        f"🎭 {anime['genre']}\n"
        f"🎬 Qismlar: {len(episodes)}\n"
        f"👁 Korishlar: {anime['views']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔓 Ushbu anime qulfini ochish", callback_data=f"unlockanime_{anime_id}")],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_list_0")]
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("unlockanime_"))
async def unlockanime(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    anime_id = int(call.data.split("_")[1])
    anime = await asyncio.to_thread(db.get_anime, anime_id)
    if not anime:
        await call.answer("❌ Topilmadi")
        return
    count = await asyncio.to_thread(db.unlock_anime_episodes, anime_id)
    await call.answer(f"✅ {count} ta qism qulfdan chiqarildi")
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    await call.message.edit_text(
        f"<b>{anime['title']}</b>\n"
        f"📅 {anime['year']} | 🌍 {anime['country']}\n"
        f"🎭 {anime['genre']}\n"
        f"🎬 Qismlar: {len(episodes)}\n"
        f"👁 Korishlar: {anime['views']}\n\n"
        f"🔓 Barcha qismlar qulfdan chiqarildi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔓 Ushbu anime qulfini ochish", callback_data=f"unlockanime_{anime_id}")],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_list_0")]
        ]),
        parse_mode="HTML"
    )

# ---- TAHRIRLASH ----
@dp.callback_query(F.data == "admin_edit")
async def admin_edit(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text(
        "✏️ Tahrirlash — anime tanlash usuli:",
        reply_markup=search_method_keyboard("edit")
    )

@dp.callback_query(F.data == "edit_list")
async def edit_list(call: CallbackQuery):
    animes = await asyncio.to_thread(db.get_animes, page=0)
    total = await asyncio.to_thread(db.get_anime_count)
    await call.message.edit_text(
        "Animeni tanlang:",
        reply_markup=admin_anime_list_keyboard(animes, 0, total, "editsel")
    )

@dp.callback_query(F.data.startswith("editsel_page_"))
async def editsel_page(call: CallbackQuery):
    page = int(call.data.split("_")[-1])
    animes = await asyncio.to_thread(db.get_animes, page=page)
    total = await asyncio.to_thread(db.get_anime_count)
    await call.message.edit_text(
        "Animeni tanlang:",
        reply_markup=admin_anime_list_keyboard(animes, page, total, "editsel")
    )

@dp.callback_query(F.data == "edit_search")
async def edit_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditAnime.search_query)
    await call.message.edit_text("🔍 Anime nomini yozing:")

@dp.message(EditAnime.search_query)
async def edit_search_result(message: Message, state: FSMContext):
    results = await asyncio.to_thread(db.search_anime, message.text.strip())
    if not results:
        await message.answer("❌ Topilmadi!")
        return
    buttons = [[InlineKeyboardButton(text=a["title"], callback_data=f"editsel_{a['id']}")] for a in results]
    await message.answer("Tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("editsel_"))
async def editsel(call: CallbackQuery, state: FSMContext):
    anime_id = int(call.data.split("_")[1])
    await state.update_data(edit_anime_id=anime_id)
    await state.set_state(EditAnime.choose_field)
    await call.message.edit_text(
        "✏️ Qaysi maydonni tahrirlaysiz?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📌 Nomi", callback_data="efield_title")],
            [InlineKeyboardButton(text="📅 Yili", callback_data="efield_year")],
            [InlineKeyboardButton(text="🌍 Davlat", callback_data="efield_country")],
            [InlineKeyboardButton(text="🎭 Janr", callback_data="efield_genre")],
            [InlineKeyboardButton(text="🏷 Kategoriya", callback_data="efield_category")],
            [InlineKeyboardButton(text="📝 Malumot", callback_data="efield_description")],
            [InlineKeyboardButton(text="🔢 Jami qism soni", callback_data="efield_total_episodes")],
            [InlineKeyboardButton(text="📊 Holat", callback_data="efield_status")],
            [InlineKeyboardButton(text="❌ Bekor", callback_data="admin_back")],
        ])
    )

@dp.callback_query(F.data.startswith("efield_"))
async def edit_field(call: CallbackQuery, state: FSMContext):
    field = call.data.replace("efield_", "")
    await state.update_data(edit_field=field)
    if field == "status":
        await call.message.edit_text(
            "📊 Holatni tanlang:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🟢 Davom etmoqda", callback_data="setstatus_ongoing")],
                [InlineKeyboardButton(text="✅ Tugagan", callback_data="setstatus_finished")],
                [InlineKeyboardButton(text="❌ Bekor", callback_data="admin_back")],
            ])
        )
        return
    await state.set_state(EditAnime.new_value)
    await call.message.edit_text("✏️ Yangi qiymatni yozing:")

@dp.callback_query(F.data.startswith("setstatus_"))
async def set_anime_status(call: CallbackQuery, state: FSMContext):
    status = call.data.replace("setstatus_", "")
    data = await state.get_data()
    anime_id = data.get("edit_anime_id")
    if not anime_id:
        await call.answer("❌ Xatolik: anime tanlanmagan.", show_alert=True)
        return
    await asyncio.to_thread(db.update_anime, anime_id, "status", status)
    await state.clear()
    await log_admin_action(call.from_user, "Animeni tahrirladi", f"maydon: status, anime_id: {anime_id}")
    await call.message.edit_text("✅ Yangilandi!")

@dp.message(EditAnime.new_value)
async def edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    value = message.text
    if data["edit_field"] == "total_episodes":
        try:
            value = int(value.strip())
            if value <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Iltimos, musbat butun son kiriting (masalan: 24).")
            return
    await asyncio.to_thread(db.update_anime, data["edit_anime_id"], data["edit_field"], value)
    await state.clear()
    await log_admin_action(message.from_user, "Animeni tahrirladi", f"maydon: {data['edit_field']}, anime_id: {data['edit_anime_id']}")
    await message.answer("✅ Yangilandi!", reply_markup=admin_keyboard())

# ---- BANNERLAR ----
def banner_list_keyboard(banners):
    buttons = []
    for b in banners:
        status = "✅" if b["is_active"] else "🚫"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {b['title']}", callback_data=f"bview_{b['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="➕ Banner qo'shish", callback_data="banner_add")])
    buttons.append([InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(F.data == "admin_banners")
async def admin_banners(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.clear()
    banners = await asyncio.to_thread(db.get_banners, False)
    text = "🖼 <b>Bannerlar</b>\n\nWebapp bosh sahifasidagi aylanuvchi bannerlarni shu yerdan boshqarasiz." if banners else "🖼 <b>Bannerlar</b>\n\nHozircha banner qo'shilmagan."
    await call.message.edit_text(text, reply_markup=banner_list_keyboard(banners), parse_mode="HTML")

@dp.callback_query(F.data == "banner_add")
async def banner_add(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(AddBanner.photo)
    await call.message.edit_text("🖼 Banner rasmini yuboring:", reply_markup=admin_back())

@dp.message(AddBanner.photo, F.photo)
async def banner_photo(message: Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddBanner.title)
    await message.answer("📌 Banner sarlavhasini yozing:")

@dp.message(AddBanner.title)
async def banner_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(AddBanner.subtitle)
    await message.answer("📝 Kichik matn (subtitle) yozing, yoki /skip:")

@dp.message(AddBanner.subtitle, Command("skip"))
async def banner_subtitle_skip(message: Message, state: FSMContext):
    await state.update_data(subtitle="")
    await state.set_state(AddBanner.anime_link)
    await message.answer("🔗 Ushbu bannerni qaysi anime'ga bog'laymiz? Anime nomini yozing yoki /skip:")

@dp.message(AddBanner.subtitle)
async def banner_subtitle(message: Message, state: FSMContext):
    await state.update_data(subtitle=message.text)
    await state.set_state(AddBanner.anime_link)
    await message.answer("🔗 Ushbu bannerni qaysi anime'ga bog'laymiz? Anime nomini yozing yoki /skip:")

@dp.message(AddBanner.anime_link, Command("skip"))
async def banner_link_skip(message: Message, state: FSMContext):
    data = await state.get_data()
    await asyncio.to_thread(db.add_banner, data["photo_id"], data["title"], data.get("subtitle", ""), None, 0)
    await state.clear()
    await message.answer("✅ Banner qo'shildi!", reply_markup=admin_keyboard())

@dp.message(AddBanner.anime_link)
async def banner_link(message: Message, state: FSMContext):
    results = await asyncio.to_thread(db.search_anime, message.text.strip())
    if not results:
        await message.answer("❌ Topilmadi, qaytadan yozing yoki /skip bosing:")
        return
    data = await state.get_data()
    anime_id = results[0]["id"]
    await asyncio.to_thread(db.add_banner, data["photo_id"], data["title"], data.get("subtitle", ""), anime_id, 0)
    await state.clear()
    await message.answer(f"✅ Banner qo'shildi va \"{results[0]['title']}\" bilan bog'landi!", reply_markup=admin_keyboard())

@dp.callback_query(F.data.startswith("bview_"))
async def banner_view(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    banner_id = int(call.data.split("_")[1])
    banners = await asyncio.to_thread(db.get_banners, False)
    b = next((x for x in banners if x["id"] == banner_id), None)
    if not b:
        await call.answer("Topilmadi", show_alert=True)
        return
    toggle_text = "🚫 O'chirib qo'yish" if b["is_active"] else "✅ Yoqish"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=f"btoggle_{b['id']}")],
        [InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"bdel_{b['id']}")],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_banners")],
    ])
    await call.message.edit_text(
        f"🖼 <b>{b['title']}</b>\n{b.get('subtitle') or ''}\nHolati: {'✅ Faol' if b['is_active'] else '🚫 Oʻchirilgan'}",
        reply_markup=kb, parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("btoggle_"))
async def banner_toggle(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    banner_id = int(call.data.split("_")[1])
    banners = await asyncio.to_thread(db.get_banners, False)
    b = next((x for x in banners if x["id"] == banner_id), None)
    if b:
        await asyncio.to_thread(db.set_banner_active, banner_id, not b["is_active"])
    banners = await asyncio.to_thread(db.get_banners, False)
    await call.message.edit_text("🖼 <b>Bannerlar</b>", reply_markup=banner_list_keyboard(banners), parse_mode="HTML")

@dp.callback_query(F.data.startswith("bdel_"))
async def banner_delete(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    banner_id = int(call.data.split("_")[1])
    await asyncio.to_thread(db.delete_banner, banner_id)
    banners = await asyncio.to_thread(db.get_banners, False)
    await call.message.edit_text("✅ Banner o'chirildi.\n\n🖼 <b>Bannerlar</b>", reply_markup=banner_list_keyboard(banners), parse_mode="HTML")

# ---- IZOHLAR MODERATSIYASI ----
class ModerateComment(StatesGroup):
    search_query = State()

@dp.callback_query(F.data == "admin_comments_anime")
async def admin_comments_anime(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(ModerateComment.search_query)
    await call.message.edit_text("🔍 Izohlarini ko'rmoqchi bo'lgan anime nomini yozing:", reply_markup=admin_back())

@dp.message(ModerateComment.search_query)
async def admin_comments_result(message: Message, state: FSMContext):
    results = await asyncio.to_thread(db.search_anime, message.text.strip())
    if not results:
        await message.answer("❌ Topilmadi!")
        return
    anime = results[0]
    comments = await asyncio.to_thread(db.get_comments, anime["id"], 20)
    if not comments:
        await message.answer(f"💬 \"{anime['title']}\" uchun izohlar yo'q.")
        await state.clear()
        return
    buttons = [[InlineKeyboardButton(
        text=f"🗑 {(c['username'] or c['user_id'])}: {c['text'][:25]}", callback_data=f"cdel_{c['id']}"
    )] for c in comments]
    buttons.append([InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")])
    await message.answer(f"💬 \"{anime['title']}\" izohlari (o'chirish uchun bosing):", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.clear()

@dp.callback_query(F.data.startswith("cdel_"))
async def comment_delete(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    comment_id = int(call.data.split("_")[1])
    await asyncio.to_thread(db.delete_comment, comment_id)
    await call.answer("✅ Izoh o'chirildi")

# ---- O'CHIRISH ----
@dp.callback_query(F.data == "admin_delete")
async def admin_delete(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text(
        "🗑 O'chirish — anime tanlash usuli:",
        reply_markup=search_method_keyboard("del")
    )

@dp.callback_query(F.data == "del_list")
async def del_list(call: CallbackQuery):
    animes = await asyncio.to_thread(db.get_animes, page=0)
    total = await asyncio.to_thread(db.get_anime_count)
    await call.message.edit_text(
        "Animeni tanlang:",
        reply_markup=admin_anime_list_keyboard(animes, 0, total, "delsel")
    )

@dp.callback_query(F.data.startswith("delsel_page_"))
async def delsel_page(call: CallbackQuery):
    page = int(call.data.split("_")[-1])
    animes = await asyncio.to_thread(db.get_animes, page=page)
    total = await asyncio.to_thread(db.get_anime_count)
    await call.message.edit_text(
        "Animeni tanlang:",
        reply_markup=admin_anime_list_keyboard(animes, page, total, "delsel")
    )

@dp.callback_query(F.data == "del_search")
async def del_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(DeleteAnime.search_query)
    await call.message.edit_text("🔍 Anime nomini yozing:")

@dp.message(DeleteAnime.search_query)
async def del_search_result(message: Message, state: FSMContext):
    results = await asyncio.to_thread(db.search_anime, message.text.strip())
    if not results:
        await message.answer("❌ Topilmadi!")
        return
    buttons = [[InlineKeyboardButton(text=a["title"], callback_data=f"delsel_{a['id']}")] for a in results]
    await message.answer("Tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("delsel_"))
async def delsel(call: CallbackQuery, state: FSMContext):
    anime_id = int(call.data.split("_")[1])
    anime = await asyncio.to_thread(db.get_anime, anime_id)
    await state.update_data(del_anime_id=anime_id)
    await state.set_state(DeleteAnime.confirm)
    await call.message.edit_text(
        f"⚠️ <b>{anime['title']}</b> ni ochirasizmi?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Ha", callback_data="del_confirm_yes", style="danger"),
                InlineKeyboardButton(text="❌ Yoq", callback_data="admin_back", style="primary"),
            ]
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "del_confirm_yes")
async def del_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    anime = await asyncio.to_thread(db.get_anime, data["del_anime_id"])
    await asyncio.to_thread(db.delete_anime, data["del_anime_id"])
    await state.clear()
    await log_admin_action(call.from_user, "Anime o'chirdi", anime["title"] if anime else str(data["del_anime_id"]))
    await call.message.edit_text(
        f"🗑 <b>{anime['title']}</b> ochirildi!",
        reply_markup=admin_back(),
        parse_mode="HTML"
    )

# ---- QISMLAR ----
@dp.callback_query(F.data == "admin_episodes")
async def admin_episodes(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text(
        "🎬 <b>Qism boshqaruvi</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Qism o'chirish", callback_data="ep_del")],
            [InlineKeyboardButton(text="✏️ Qism tahrirlash", callback_data="ep_edit")],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

# ---- VIDEOLARNI TEKSHIRISH (kanaldan o'chirilgan xabarlarni topish) ----
# Videolar STORAGE_CHANNEL'dagi xabarlarga (channel_message_id) havola qilib
# saqlanadi. Kimdir kanaldan o'sha xabarni o'chirib qo'ysa, epizod jim-jit
# buziladi — foydalanuvchi "video topilmadi" xatosiga duch kelmaguncha admin
# bundan bexabar qoladi. Bu tugma barcha qismlarni Telegram'dan (Pyrogram
# orqali, ommaviy so'rovlar bilan) qayta tekshirib, buzilganlarini ro'yxatlaydi.
_VIDEO_CHECK_CHUNK = 100

@dp.callback_query(F.data == "admin_check_videos")
async def admin_check_videos(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    if not STREAM_ENABLED or not pyro:
        await call.answer(
            "Bu funksiya uchun API_ID/API_HASH sozlanmagan (onlayn striming o'chirilgan).",
            show_alert=True
        )
        return

    episodes = await asyncio.to_thread(db.get_all_episodes_with_anime)
    if not episodes:
        await call.message.edit_text("Hozircha hech qanday qism yo'q.", reply_markup=admin_back())
        return

    total = len(episodes)
    await call.message.edit_text(f"🔍 Tekshirilmoqda... (0/{total}) — 0%")

    if not await _ensure_pyro_ready(pyro):
        await call.message.edit_text(
            "Onlayn ko'rish vaqtincha ishlamayapti, birozdan so'ng qayta urinib ko'ring.",
            reply_markup=admin_back()
        )
        return

    broken = []
    checked = 0
    for i in range(0, total, _VIDEO_CHECK_CHUNK):
        chunk = episodes[i:i + _VIDEO_CHECK_CHUNK]
        ids = [ep["channel_message_id"] for ep in chunk]
        try:
            msgs = await pyro.get_messages(STORAGE_CHANNEL, ids)
            if not isinstance(msgs, list):
                msgs = [msgs]
        except Exception as e:
            logger.error(f"[admin_check_videos] get_messages xato: {e}")
            msgs = [None] * len(ids)

        for ep, msg in zip(chunk, msgs):
            checked += 1
            is_broken = (
                msg is None
                or getattr(msg, "empty", False)
                or not (getattr(msg, "video", None) or getattr(msg, "document", None) or getattr(msg, "animation", None))
            )
            if is_broken:
                broken.append(ep)

        try:
            percent = round(checked / total * 100)
            await call.message.edit_text(f"🔍 Tekshirilmoqda... ({checked}/{total}) — {percent}%")
        except Exception:
            pass

    if not broken:
        await call.message.edit_text(
            f"✅ Tekshiruv tugadi.\n\nBarcha {total} ta video havolasi ishlayapti — buzilgan qism topilmadi.",
            reply_markup=admin_back(),
            parse_mode="HTML"
        )
        return

    lines = [
        f"❌ <b>{ep['anime_title']}</b> — {ep['episode_number']}-qism (ID: {ep['id']})"
        for ep in broken[:50]
    ]
    extra = f"\n\n...va yana {len(broken) - 50} ta" if len(broken) > 50 else ""
    await call.message.edit_text(
        f"🔍 Tekshiruv tugadi: {total} ta qismdan <b>{len(broken)}</b> tasi buzilgan "
        f"(kanal xabari o'chirilgan yoki topilmagan):\n\n"
        + "\n".join(lines) + extra +
        "\n\nBuzilgan qismni tuzatish uchun ✏️ Qismlarni tahrirlash bo'limidan foydalaning.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Qismlarni tahrirlash", callback_data="admin_episodes")],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "ep_del")
async def ep_del(call: CallbackQuery, state: FSMContext):
    await state.update_data(ep_action="del")
    await call.message.edit_text(
        "Qism o'chirish — serial tanlash usuli:",
        reply_markup=search_method_keyboard("epact")
    )

@dp.callback_query(F.data == "ep_edit")
async def ep_edit(call: CallbackQuery, state: FSMContext):
    await state.update_data(ep_action="edit")
    await call.message.edit_text(
        "Qism tahrirlash — serial tanlash usuli:",
        reply_markup=search_method_keyboard("epact")
    )

@dp.callback_query(F.data == "epact_list")
async def epact_list(call: CallbackQuery, state: FSMContext):
    animes = await asyncio.to_thread(db.get_animes, "serial", 0)
    total = await asyncio.to_thread(db.get_anime_count, "serial")
    await state.set_state(EditEpisode.choose_episode)
    await call.message.edit_text(
        "Serial tanlang:",
        reply_markup=admin_anime_list_keyboard(animes, 0, total, "epact_sel")
    )

@dp.callback_query(F.data.startswith("epact_sel_page_"))
async def epact_sel_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split("_")[-1])
    animes = await asyncio.to_thread(db.get_animes, "serial", page)
    total = await asyncio.to_thread(db.get_anime_count, "serial")
    await call.message.edit_text(
        "Serial tanlang:",
        reply_markup=admin_anime_list_keyboard(animes, page, total, "epact_sel")
    )

@dp.callback_query(F.data == "epact_search")
async def epact_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditEpisode.search_query)
    await call.message.edit_text("🔍 Serial nomini yozing:")

@dp.message(EditEpisode.search_query)
async def epact_search_result(message: Message, state: FSMContext):
    results = await asyncio.to_thread(db.search_anime, message.text.strip())
    serials = [a for a in results if a["media_type"] == "serial"]
    if not serials:
        await message.answer("❌ Topilmadi!")
        return
    buttons = [[InlineKeyboardButton(text=a["title"], callback_data=f"epact_sel_{a['id']}")] for a in serials]
    await message.answer("Tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("epact_sel_"))
async def epact_sel(call: CallbackQuery, state: FSMContext):
    anime_id = int(call.data.split("_")[2])
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    await state.update_data(epact_anime_id=anime_id)
    await state.set_state(EditEpisode.choose_episode)
    buttons = []
    row = []
    for ep in episodes:
        row.append(InlineKeyboardButton(
            text=f"{ep['episode_number']}-qism",
            callback_data=f"epact_ep_{ep['id']}"
        ))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")])
    await call.message.edit_text(
        "Qismni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("epact_ep_"))
async def epact_ep(call: CallbackQuery, state: FSMContext):
    ep_id = int(call.data.split("_")[2])
    data = await state.get_data()
    action = data.get("ep_action")
    if action == "del":
        await asyncio.to_thread(db.delete_episode, ep_id)
        await state.clear()
        await call.message.edit_text("🗑 Qism ochirildi!", reply_markup=admin_back())
    elif action == "edit":
        await state.update_data(edit_ep_id=ep_id)
        await state.set_state(EditEpisode.new_video)
        await call.message.edit_text("🎬 Yangi videoni yuboring:")

@dp.message(EditEpisode.new_video, F.video)
async def epact_new_video(message: Message, state: FSMContext):
    data = await state.get_data()
    old_ep = await asyncio.to_thread(db.get_episode, data["edit_ep_id"])
    ep_num = old_ep["episode_number"] if old_ep else None
    new_msg_id = await _upload_episode_to_storage(
        message.chat.id, message.message_id,
        episode_caption(ep_num) if ep_num else None, status_message=message
    )
    await asyncio.to_thread(db.update_episode, data["edit_ep_id"], new_msg_id)
    await state.clear()
    await message.answer("✅ Qism yangilandi!", reply_markup=admin_keyboard())

# ===================== QIZIQARLI JOY KESISH (AUTO-HIGHLIGHT) =====================
# Ovoz balandligi (RMS) tahliliga asoslanib videoning eng "qiziqarli" (energiyaga
# eng boy — jang/kulgi/musiqa avjida odatda ovoz balandroq bo'ladi) qismini avtomatik
# topib, 15/30 soniyalik klip qilib kesib beradi. Chinakam video-tushunish (nima
# tasvirlanganini "ko'rish") emas — bu audio energiyasiga asoslangan amaliy evristika,
# lekin jang/kulgi/musiqiy avj kabi joylarni yetarlicha yaxshi topadi.
#
# MUHIM: bu funksiya ishlashi uchun serverda `ffmpeg` va `ffprobe` o'rnatilgan bo'lishi
# SHART (Render'da standart Python image'ida ular yo'q — Aptfile yoki Dockerfile orqali
# qo'shish kerak bo'ladi, aks holda quyidagi funksiyalar xato beradi).

CLIP_TMP_DIR = "/tmp/anime_clips"
EPISODE_TMP_DIR = "/tmp/anime_episodes"
# job_id -> asyncio.Task — hozir ishlayotgan klip jarayonlari, "Bekor qilish"
# tugmasi bosilganda mos Task topilib .cancel() qilinishi uchun.
_clip_jobs = {}
_CLIP_WINDOW_SEC = 5     # tahlil oynasi — video shu uzunlikdagi bo'laklarga bo'linadi
_CLIP_SAMPLE_RATE = 44100

# Yangi qism yuklanganda klip avtomatik yaratilishi kerakmi (admin buyruq bermasdan).
# O'chirish uchun Environment'da AUTO_CLIP_ENABLED=0 qiling.
AUTO_CLIP_ENABLED = os.environ.get("AUTO_CLIP_ENABLED", "1") == "1"
AUTO_CLIP_DURATION = int(os.environ.get("AUTO_CLIP_DURATION", "30"))

async def is_auto_clip_enabled():
    """Avtomatik klip yoqilganmi — DB'dagi 'auto_clip_enabled' sozlamasi asosiy
    manba (admin panel orqali o'zgartiriladi). Agar DB'da hali sozlanmagan
    bo'lsa (birinchi marta), Environment o'zgaruvchisi (AUTO_CLIP_ENABLED)
    standart qiymat sifatida ishlatiladi."""
    val = await asyncio.to_thread(db.get_setting, "auto_clip_enabled")
    if val is None:
        return AUTO_CLIP_ENABLED
    return val == "1"

def _ffprobe_duration(path):
    """Video faylining umumiy davomiyligini (soniyalarda) qaytaradi."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True
    )
    try:
        return float(out.stdout.strip())
    except Exception:
        return None

async def _probe_video_codec(path):
    """Faylning birinchi video oqimi kodek nomini qaytaradi (masalan 'h264',
    'hevc'). Aniqlab bo'lmasa (fayl buzilgan/ffprobe topa olmadi) None qaytaradi
    va SABABINI logga yozadi (jim qolib ketmaslik uchun)."""
    try:
        out = await asyncio.to_thread(
            subprocess.run,
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True
        )
    except Exception as e:
        logger.warning(f"[epizod-format] ffprobe (video) ishga tushmadi: {e!r}")
        return None
    if out.returncode != 0:
        logger.warning(f"[epizod-format] ffprobe (video) xato qaytardi: {out.stderr.strip()[:300]}")
    codec = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
    return codec or None

async def _probe_audio_codec(path):
    """Faylning birinchi audio oqimi kodek nomini qaytaradi (masalan 'aac',
    'ac3'). Audio oqim umuman bo'lmasa yoki aniqlab bo'lmasa None qaytaradi."""
    try:
        out = await asyncio.to_thread(
            subprocess.run,
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True
        )
    except Exception as e:
        logger.warning(f"[epizod-format] ffprobe (audio) ishga tushmadi: {e!r}")
        return None
    codec = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
    return codec or None

# Brauzerning <video> tegi ishonchli o'ynata oladigan video/audio kodeklar.
# Agar yuklangan qismning video YOKI audio oqimi shulardan birida bo'lmasa
# (eng ko'p uchraydigani: video HEVC/H.265, yoki audio AC3/EAC3/DTS — bularning
# barchasi Telegram ilovasida yaxshi o'ynaydi, lekin Chrome kabi brauzerlarda
# odatda ishlamaydi), webapp pleer "Videoni yuklab bo'lmadi" xatosini beradi.
# Shu sabab bunday holatda avtomatik H.264/AAC'ga o'tkazamiz.
_BROWSER_SAFE_VIDEO_CODECS = {"h264", "vp9", "vp8", "av1"}
_BROWSER_SAFE_AUDIO_CODECS = {"aac", "mp3"}

async def _upload_episode_to_storage(orig_chat_id, orig_message_id, caption, status_message=None):
    """Admin yuborgan qism videosini STORAGE_CHANNEL'ga saqlaydi. Video yoki
    audio kodeki brauzer bilan mos kelmasa (masalan video HEVC yoki audio AC3),
    avtomatik ravishda H.264/AAC'ga o'tkazadi, shundan keyingina saqlaydi —
    aks holda webapp pleer videoni umuman ochira olmaydi (garchi Telegram
    ilovasining o'zida muammosiz o'ynasa ham).
    Har doim STORAGE_CHANNEL'dagi YAKUNIY xabar message_id'sini qaytaradi.
    Tekshirish/o'tkazish jarayonida kutilmagan xato yuz bersa, asl (o'tkazilmagan)
    video saqlab qolinadi — admin oqimi hech qachon butunlay to'xtamaydi.
    Har bir bosqich [epizod-format] prefiksi bilan logga yoziladi — Render
    log qidiruvida shu so'z bo'yicha butun jarayonni kuzatish mumkin."""
    sent = await bot.copy_message(
        STORAGE_CHANNEL, orig_chat_id, orig_message_id,
        caption=caption, parse_mode=None
    )
    logger.info(f"[epizod-format] boshlandi: STORAGE_CHANNEL xabari={sent.message_id}")

    await asyncio.to_thread(os.makedirs, EPISODE_TMP_DIR, exist_ok=True)
    ts = int(time.time() * 1000)
    src_path = os.path.join(EPISODE_TMP_DIR, f"ep_{sent.message_id}_{ts}_src.mp4")
    out_path = os.path.join(EPISODE_TMP_DIR, f"ep_{sent.message_id}_{ts}_h264.mp4")

    try:
        channel_msg = await pyro.get_messages(STORAGE_CHANNEL, sent.message_id)
        media = channel_msg.video or channel_msg.document or channel_msg.animation
        if not media:
            logger.warning(f"[epizod-format] xabar {sent.message_id}'da media topilmadi — o'zgarishsiz qoldirildi.")
            return sent.message_id

        logger.info(f"[epizod-format] yuklab olinmoqda (hajm={getattr(media, 'file_size', '?')} bayt)...")
        dl_client = _next_stream_client() if _stream_clients else pyro
        await dl_client.download_media(channel_msg, file_name=src_path)
        logger.info(f"[epizod-format] yuklab olindi: {src_path}")

        v_codec = await _probe_video_codec(src_path)
        a_codec = await _probe_audio_codec(src_path)
        logger.info(f"[epizod-format] aniqlangan kodeklar: video={v_codec!r}, audio={a_codec!r}")

        video_ok = v_codec is not None and v_codec in _BROWSER_SAFE_VIDEO_CODECS
        audio_ok = a_codec is None or a_codec in _BROWSER_SAFE_AUDIO_CODECS
        if video_ok and audio_ok:
            logger.info(f"[epizod-format] kodeklar brauzer bilan mos — o'tkazish shart emas.")
            return sent.message_id
        if v_codec is None:
            # ffprobe video oqimini aniqlay olmadi (masalan ffmpeg/ffprobe
            # o'rnatilmagan yoki fayl buzilgan) — xavfsiz tomondan qolib,
            # o'zgartirmaymiz, lekin buni ANIQ logga yozamiz.
            logger.warning(f"[epizod-format] video kodekini aniqlab bo'lmadi — o'tkazishdan voz kechildi (o'zgarishsiz qoldirildi).")
            return sent.message_id

        if status_message:
            try:
                await status_message.answer(
                    f"🎞 Format (video={v_codec}, audio={a_codec}) ba'zi brauzerlarda "
                    f"ishlamasligi mumkin — avtomatik H.264/AAC'ga o'tkazilmoqda. "
                    f"Fayl hajmiga qarab bu bir necha daqiqa vaqt olishi mumkin, iltimos kuting..."
                )
            except Exception:
                pass

        total_dur = await asyncio.to_thread(_ffprobe_duration, src_path)
        logger.info(f"[epizod-format] transkodlash boshlandi (davomiylik={total_dur}s)...")
        await _run_ffmpeg_progress(
            ["ffmpeg", "-y", "-i", src_path,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", out_path],
            total_dur or 0, stall_timeout=180
        )
        logger.info(f"[epizod-format] transkodlash tugadi: {out_path}")

        new_sent = await pyro.send_video(STORAGE_CHANNEL, out_path, caption=caption or "")
        logger.info(f"[epizod-format] yangi H.264 fayl saqlandi: msg_id={new_sent.id}")
        try:
            await bot.delete_message(STORAGE_CHANNEL, sent.message_id)
        except Exception:
            logger.warning(f"Eski (transkodlanmagan) xabar {sent.message_id} o'chirilmadi — qo'lda tozalash kerak bo'lishi mumkin.")
        return new_sent.id
    except Exception:
        logger.exception(
            "[epizod-format] tekshirish/o'tkazishda kutilmagan xato — "
            f"asl video (msg_id={sent.message_id}) o'zgarishsiz saqlab qolindi."
        )
        return sent.message_id
    finally:
        for p in (src_path, out_path):
            try:
                if os.path.exists(p):
                    await asyncio.to_thread(os.remove, p)
            except Exception:
                pass

async def _analyze_loudness(path, total_duration=None, progress_cb=None, highpass_hz=None):
    """Videoni _CLIP_WINDOW_SEC bo'laklarga bo'lib, har bir bo'lak uchun ovoz
    balandligi (RMS, dB) darajasini o'lchaydi. Natija: [(vaqt_soniya, rms_db), ...]
    `highpass_hz` berilsa (masalan 2000), past chastotalar (musiqa/bass) kesib
    tashlanadi va faqat YUQORI chastotali tovushlar (qilich-zarba, portlash,
    shovqin-siyosat kabi keskin effektlar) o'lchanadi — bu jang sahnalarini
    fon musiqasidan ko'proq ajratib topishga yordam beradi.
    `total_duration` va `progress_cb(pct)` berilsa, ffmpeg'ning o'z `-progress`
    chiqishidan foydalanib, tahlil davomida foizni (0-100) real vaqtda xabar qiladi
    — aks holda jarayon tugagunicha ekranda hech narsa yangilanmaydi."""
    n_samples = _CLIP_SAMPLE_RATE * _CLIP_WINDOW_SEC
    af_chain = f"highpass=f={highpass_hz}," if highpass_hz else ""
    af_chain += (
        f"aresample={_CLIP_SAMPLE_RATE},asetnsamples=n={n_samples},"
        f"astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
    )
    cmd = [
        "ffmpeg", "-i", path,
        "-af", af_chain,
        "-progress", "pipe:2", "-nostats", "-loglevel", "error",
        "-f", "null", "-"
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_chunks = []

    async def _read_stdout():
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            stdout_chunks.append(chunk)

    async def _read_stderr():
        last_pct = -1
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            if not (progress_cb and total_duration):
                continue
            text = line.decode(errors="ignore").strip()
            if text.startswith("out_time_us=") or text.startswith("out_time_ms="):
                try:
                    us = int(text.split("=", 1)[1])
                    pct = max(0, min(100, int(us / 1_000_000 / total_duration * 100)))
                except Exception:
                    continue
                if pct != last_pct:
                    last_pct = pct
                    await progress_cb(pct)

    await asyncio.gather(_read_stdout(), _read_stderr(), proc.wait())

    frames = []
    pts_time = None
    output = b"".join(stdout_chunks).decode(errors="ignore")
    for line in output.splitlines():
        m_pts = re.search(r"pts_time:([\d.]+)", line)
        if m_pts:
            pts_time = float(m_pts.group(1))
            continue
        m_rms = re.search(r"RMS_level=(-?[\d.]+|-inf)", line)
        if m_rms and pts_time is not None:
            val = m_rms.group(1)
            rms = -120.0 if val == "-inf" else float(val)
            frames.append((pts_time, rms))
            pts_time = None
    return frames

async def _analyze_motion(path, total_duration=None, progress_cb=None):
    """BEPUL usul (faqat ffmpeg'ning ichki `signalstats` filtri, tashqi
    kutubxona kerak emas): har bir kadrning oldingi kadrdan qanchalik farq
    qilishini (YDIF — harakat/o'zgarish kuchi) o'lchaydi. Yuqori qiymat —
    kuchli harakat (jang, tez yugurish), past qiymat — deyarli statik kadr
    (tinch dialog). CPU tejash uchun video avval soniyasiga 2 kadrga
    siyraklashtiriladi (fps=2). Natija: [(vaqt_soniya, ydif_qiymati), ...]"""
    cmd = [
        "ffmpeg", "-i", path,
        "-vf", "fps=2,signalstats,metadata=print:key=lavfi.signalstats.YDIF:file=-",
        "-an", "-f", "null",
        "-progress", "pipe:2", "-nostats", "-loglevel", "error", "-"
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_chunks = []

    async def _read_stdout():
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            stdout_chunks.append(chunk)

    async def _read_stderr():
        last_pct = -1
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            if not (progress_cb and total_duration):
                continue
            text = line.decode(errors="ignore").strip()
            if text.startswith("out_time_us=") or text.startswith("out_time_ms="):
                try:
                    us = int(text.split("=", 1)[1])
                    pct = max(0, min(100, int(us / 1_000_000 / total_duration * 100)))
                except Exception:
                    continue
                if pct != last_pct:
                    last_pct = pct
                    await progress_cb(pct)

    await asyncio.gather(_read_stdout(), _read_stderr(), proc.wait())

    frames = []
    pts_time = None
    output = b"".join(stdout_chunks).decode(errors="ignore")
    for line in output.splitlines():
        m_pts = re.search(r"pts_time:([\d.]+)", line)
        if m_pts:
            pts_time = float(m_pts.group(1))
            continue
        m_ydif = re.search(r"YDIF=([\d.]+)", line)
        if m_ydif and pts_time is not None:
            frames.append((pts_time, float(m_ydif.group(1))))
            pts_time = None
    return frames

async def _analyze_silence(path, total_duration=None, progress_cb=None, noise_db=-30, min_silence=0.3):
    """BEPUL usul (`silencedetect` filtri): videoning qaysi qismlarida ovoz
    (nutq/tovush) bor, qaysi qismida jim ekanini aniqlaydi. Natija: jimlik
    oraliqlari ro'yxati [(boshlanish, tugash), ...]. Bundan foydalanib har
    bir oynada "nutq zichligi"ni hisoblash mumkin — ko'p gaplashuv (kam
    jimlik) odatda suhbatga boy (romantik/kulgili) sahnalarga xos."""
    cmd = [
        "ffmpeg", "-i", path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-progress", "pipe:2", "-nostats", "-loglevel", "info",
        "-f", "null", "-"
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    intervals = []
    open_start = None
    last_pct = -1
    async for raw in proc.stderr:
        line = raw.decode(errors="ignore").strip()
        m_start = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m_start:
            open_start = max(0.0, float(m_start.group(1)))
            continue
        m_end = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if m_end and open_start is not None:
            intervals.append((open_start, float(m_end.group(1))))
            open_start = None
            continue
        if progress_cb and total_duration and line.startswith("out_time_ms="):
            try:
                us = int(line.split("=", 1)[1])
                pct = max(0, min(100, int(us / 1_000_000 / total_duration * 100)))
            except Exception:
                continue
            if pct != last_pct:
                last_pct = pct
                await progress_cb(pct)
    await proc.wait()
    if open_start is not None and total_duration:
        intervals.append((open_start, total_duration))
    return intervals

async def _analyze_scene_cuts(path, total_duration=None, progress_cb=None):
    """BEPUL usul (faqat ffmpeg, tashqi API kerak emas): videodagi sahna
    almashinuvlarini (kadr keskin o'zgargan lahzalarni) aniqlaydi. Tez-tez
    o'zgaruvchi kadrlar odatda harakatli/jang sahnalarga xos, kam o'zgaruvchi
    uzoq kadrlar esa dialog/tinch sahnalarga xos. Natija: sahna o'zgargan
    vaqtlar ro'yxati [soniya, soniya, ...]."""
    cmd = [
        "ffmpeg", "-i", path,
        "-vf", "select='gt(scene,0.28)',showinfo",
        "-an", "-f", "null",
        "-progress", "pipe:2", "-loglevel", "info", "-"
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    cuts = []
    last_pct = -1
    async for raw in proc.stderr:
        line = raw.decode(errors="ignore").strip()
        if not line:
            continue
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            try:
                cuts.append(float(m.group(1)))
            except ValueError:
                pass
            continue
        if progress_cb and total_duration and line.startswith("out_time_ms="):
            try:
                us = int(line.split("=", 1)[1])
                pct = max(0, min(100, int(us / 1_000_000 / total_duration * 100)))
            except Exception:
                continue
            if pct != last_pct:
                last_pct = pct
                await progress_cb(pct)
    await proc.wait()
    return cuts

def _pick_best_window(frames, target_duration, total_duration, edge_margin_ratio=0.05,
                       cuts=None, high_freq_frames=None, motion_frames=None, silence_intervals=None):
    """`target_duration` soniyalik eng "qiziqarli" uzluksiz oraliqni topadi.
    Beshta BEPUL signal birlashtiriladi (qaysi biri berilgan bo'lsa, o'shanisi
    hisobga olinadi, vaznlar shunga qarab qayta taqsimlanadi):
      1. `frames`            — umumiy ovoz balandligi (RMS)
      2. `cuts`               — sahna almashinuv tezligi (jang/tez montaj belgisi)
      3. `high_freq_frames`   — yuqori chastotali "zarba" tovushlari (qilich, portlash)
      4. `motion_frames`      — kadr ichidagi harakat kuchi (YDIF)
      5. `silence_intervals`  — nutq zichligi (suhbatga boy sahnalarni topish uchun)
    Intro/outro/kredit qismlarini chetlab o'tish uchun boshi va oxiridan
    ozgina joy (~5%) hisobga olinmaydi."""
    n_windows = max(1, round(target_duration / _CLIP_WINDOW_SEC))
    edge_margin = max(0, total_duration * edge_margin_ratio)
    usable = [(t, r) for (t, r) in frames if edge_margin <= t <= max(edge_margin, total_duration - edge_margin)]
    if len(usable) < n_windows:
        usable = frames
    if not usable:
        return max(0, (total_duration - target_duration) / 2)

    def _normalize(vals):
        m = max(vals) if vals else 0.0
        return [v / m for v in vals] if m > 0 else [0.0] * len(vals)

    # 1) Ovoz balandligi (RMS, dB -> chiziqli energiya)
    energy_scores = _normalize([10 ** (r / 10.0) for (_, r) in usable])

    # 2) Sahna-kesish zichligi — har bir oynadagi kesishlar soni
    cut_scores = None
    if cuts:
        cut_scores = _normalize([
            sum(1 for c in cuts if t <= c < t + _CLIP_WINDOW_SEC) for (t, _) in usable
        ])

    # 3) Yuqori chastotali "zarba" energiyasi
    hf_scores = None
    if high_freq_frames:
        hf_lookup = {round(t): r for (t, r) in high_freq_frames}
        hf_scores = _normalize([
            10 ** (hf_lookup.get(round(t), -120.0) / 10.0) for (t, _) in usable
        ])

    # 4) Harakat kuchi — oynadagi namunalarning o'rtachasi
    motion_scores = None
    if motion_frames:
        motion_scores = _normalize([
            (lambda pts: sum(pts) / len(pts) if pts else 0.0)(
                [v for (mt, v) in motion_frames if t <= mt < t + _CLIP_WINDOW_SEC]
            )
            for (t, _) in usable
        ])

    # 5) Nutq zichligi — oynaning jimlik bilan qoplanmagan qismi ulushi
    speech_scores = None
    if silence_intervals is not None:
        def _speech_ratio(t):
            win_end = t + _CLIP_WINDOW_SEC
            silent = 0.0
            for s, e in silence_intervals:
                overlap = min(win_end, e) - max(t, s)
                if overlap > 0:
                    silent += overlap
            return max(0.0, 1.0 - silent / _CLIP_WINDOW_SEC)
        speech_scores = [_speech_ratio(t) for (t, _) in usable]  # allaqachon 0..1

    # Faqat mavjud signallar bo'yicha vaznlarni qayta taqsimlaymiz.
    weighted = [("energy", energy_scores, 0.30)]
    if cut_scores is not None:
        weighted.append(("cuts", cut_scores, 0.25))
    if hf_scores is not None:
        weighted.append(("hf", hf_scores, 0.20))
    if motion_scores is not None:
        weighted.append(("motion", motion_scores, 0.15))
    if speech_scores is not None:
        weighted.append(("speech", speech_scores, 0.10))
    total_w = sum(w for _, _, w in weighted)

    combined = [0.0] * len(usable)
    for _, scores, w in weighted:
        norm_w = w / total_w
        for i, s in enumerate(scores):
            combined[i] += norm_w * s

    best_idx, best_sum = 0, -1.0
    for i in range(0, len(usable) - n_windows + 1):
        s = sum(combined[i:i + n_windows])
        if s > best_sum:
            best_sum = s
            best_idx = i
    start_time = usable[best_idx][0]
    return min(start_time, max(0, total_duration - target_duration))


# ---- AI (VISION) ORQALI ENG QIZIQARLI JOYNI TOPISH ----
# Yuqoridagi ovoz-balandligi (RMS) usuli faqat audio energiyasiga qaraydi — u nima
# tasvirlanayotganini bilmaydi. Bu yerda Claude'ning vision qobiliyatidan foydalanib,
# videodan bir nechta nomzod lahza (kadr) olinadi va Claude'dan ENG jonli/qiziqarli
# lahzani (jang, kulgi, drama cho'qqisi va h.k.) tanlashi so'raladi.
#
# ISHLASHI UCHUN: serverga ANTHROPIC_API_KEY environment o'zgaruvchisi kerak
# (https://console.anthropic.com dan olinadi). O'rnatilmagan yoki so'rov muvaffaqiyatsiz
# bo'lsa, bot xato bermaydi — avtomatik ravishda pastdagi ovoz-balandligi usuliga qaytadi.

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
_AI_VISION_MODEL = "claude-haiku-4-5-20251001"  # vision uchun tez va arzon model
_AI_CANDIDATE_COUNT = 8  # tahlil qilinadigan namunaviy kadrlar soni (ko'p bo'lsa — xarajat oshadi)

def _extract_frame(path, timestamp, out_path):
    """Videoning bitta lahzasidan kichik JPEG kadr oladi (xarajat/tokenni tejash
    uchun kichraytirilgan o'lchamda — 480px kengida)."""
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(max(0, timestamp)), "-i", path,
        "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4",
        out_path, "-loglevel", "error"
    ], check=True)

def _candidate_start_times(target_duration, total_duration, edge_margin_ratio=0.05):
    """Klip boshlanishi mumkin bo'lgan nomzod vaqtlarni video bo'ylab bir tekis
    taqsimlab qaytaradi (intro/outro chetlab o'tiladi, xuddi RMS usulidagi kabi)."""
    edge_margin = total_duration * edge_margin_ratio
    lo = edge_margin
    hi = max(lo, total_duration - edge_margin - target_duration)
    if hi <= lo:
        return [max(0, (total_duration - target_duration) / 2)]
    n = min(_AI_CANDIDATE_COUNT, max(3, int((hi - lo) // max(5, target_duration))))
    if n <= 1:
        return [lo]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]

async def _ai_pick_best_window(path, target_duration, total_duration, progress_cb=None):
    """Nomzod lahzalardan kadr oladi, Claude'ga (vision) yuboradi va eng
    qiziqarlisini tanlashini so'raydi. Muvaffaqiyatsiz bo'lsa (API kaliti yo'q,
    tarmoq xatosi, formatlanmagan javob va h.k.) None qaytaradi — bu holda
    chaqiruvchi RMS (ovoz-balandligi) usuliga qaytadi.
    `progress_cb(text, force=False)` — agar berilsa, kadr olish va AI so'rovi
    bosqichlarida chaqiriladi (jarayon "qotib qolgandek" ko'rinmasligi uchun)."""
    if not ANTHROPIC_API_KEY:
        return None
    candidates = _candidate_start_times(target_duration, total_duration)
    tmp_dir = os.path.join(CLIP_TMP_DIR, f"frames_{int(time.time() * 1000)}")
    try:
        await asyncio.to_thread(os.makedirs, tmp_dir, exist_ok=True)
        content = [{
            "type": "text",
            "text": (
                f"Quyida bitta anime epizodidan {len(candidates)} ta turli lahzada olingan "
                f"kadrlar bor, har biri raqamlangan (1 dan {len(candidates)} gacha). Ijtimoiy "
                f"tarmoq (Instagram Reels/Stories) uchun qisqa reklama klipi shu lahzalardan "
                f"BIRIDAN boshlanadi — shuning uchun eng \"qiziqarli\"/jonli ko'ringan kadrni "
                f"tanla. Jang, kulgili moment, kuchli hissiyot yoki chiroyli vizual sahnalarga "
                f"ustunlik ber. FAQAT quyidagi JSON formatida javob ber, boshqa hech qanday "
                f"matn (izoh, tushuntirish) yozma: {{\"index\": <raqam>}}"
            )
        }]
        for i, ts in enumerate(candidates, 1):
            frame_path = os.path.join(tmp_dir, f"f{i}.jpg")
            await asyncio.to_thread(_extract_frame, path, ts, frame_path)
            if progress_cb:
                pct = int(i / len(candidates) * 100)
                await progress_cb(
                    f"🤖 AI ishlamoqda: kadrlar tayyorlanmoqda\n\n"
                    f"{_progress_bar(pct)} {i}/{len(candidates)}"
                )
            if not os.path.exists(frame_path):
                continue
            with open(frame_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            content.append({"type": "text", "text": f"Kadr #{i}:"})
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
            })

        if progress_cb:
            await progress_cb(f"🤖 AI ishlamoqda: {len(candidates)} ta kadr yuborildi, javob kutilmoqda...", True)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": _AI_VISION_MODEL,
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": content}],
                },
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json()

        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        m = re.search(r'"index"\s*:\s*(\d+)', text)
        if not m:
            logger.error(f"[ai-highlight] javobni ajratib bo'lmadi: {text[:200]}")
            return None
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]
        return None
    except Exception as e:
        logger.error(f"[ai-highlight] xato: {e}")
        return None
    finally:
        try:
            if os.path.exists(tmp_dir):
                for fn in os.listdir(tmp_dir):
                    os.remove(os.path.join(tmp_dir, fn))
                os.rmdir(tmp_dir)
        except Exception:
            pass

async def _find_highlight_start(path, target_duration, total_duration, progress_cb=None):
    """Avval AI (vision) usulini sinaydi; muvaffaqiyatsiz bo'lsa yoki
    ANTHROPIC_API_KEY o'rnatilmagan bo'lsa, BEPUL evristikaga qaytadi — bu
    ovoz balandligi (RMS) VA sahna-almashinuv tezligini birlashtiradi (ikkalasi
    ham faqat ffmpeg orqali, hech qanday tashqi/pullik xizmat kerak emas).
    Qaytaradi: (start_soniya, usul_nomi — status xabarida ko'rsatish uchun).
    `progress_cb(text, force=False)` — AI ishlayaptimi, muvaffaqiyatsizmi yoki
    umuman o'chirilganmi — bu holatlar aniq ajratib xabar qilinadi, shunda admin
    jarayon "qotib qolmaganini", balki AI haqiqatan ishlayotganini ko'radi."""
    async def _free_heuristic():
        """Beshta bepul signalni (ovoz, kesish, yuqori chastota, harakat,
        nutq zichligi) birlashtirgan usul — barchasi faqat ffmpeg orqali."""
        def _cb(label):
            if not progress_cb:
                return None
            async def _inner(pct):
                await progress_cb(f"{label}\n\n{_progress_bar(pct)} {pct}%")
            return _inner

        frames = await _analyze_loudness(path, total_duration, progress_cb=_cb("🔊 Ovoz balandligi tahlili..."))

        try:
            cuts = await _analyze_scene_cuts(path, total_duration, progress_cb=_cb("🎬 Sahna almashinuvi tahlili..."))
        except Exception as e:
            logger.warning(f"[scene-cuts] tahlil qilinmadi: {e}")
            cuts = None

        try:
            hf_frames = await _analyze_loudness(
                path, total_duration, progress_cb=_cb("💥 Zarba tovushlari tahlili..."), highpass_hz=2000
            )
        except Exception as e:
            logger.warning(f"[high-freq] tahlil qilinmadi: {e}")
            hf_frames = None

        try:
            motion_frames = await _analyze_motion(path, total_duration, progress_cb=_cb("🏃 Harakat kuchi tahlili..."))
        except Exception as e:
            logger.warning(f"[motion] tahlil qilinmadi: {e}")
            motion_frames = None

        try:
            silence_intervals = await _analyze_silence(path, total_duration, progress_cb=_cb("🗣 Nutq zichligi tahlili..."))
        except Exception as e:
            logger.warning(f"[silence] tahlil qilinmadi: {e}")
            silence_intervals = None

        start = (
            _pick_best_window(
                frames, target_duration, total_duration,
                cuts=cuts, high_freq_frames=hf_frames,
                motion_frames=motion_frames, silence_intervals=silence_intervals,
            )
            if frames else max(0, (total_duration - target_duration) / 2)
        )
        method = "🆓 kombinatsiyalangan tahlil" if frames else "🔊 ovoz tahlili"
        return start, method

    if not ANTHROPIC_API_KEY:
        if progress_cb:
            await progress_cb("🆓 Bepul tahlil: ovoz + sahna + harakat + nutq tahlili ishlatilmoqda...", True)
        return await _free_heuristic()

    if progress_cb:
        await progress_cb("🤖 AI ishlamoqda: kadrlar tanlanmoqda...", True)
    ai_start = await _ai_pick_best_window(path, target_duration, total_duration, progress_cb=progress_cb)
    if ai_start is not None:
        if progress_cb:
            await progress_cb("✅ AI tanladi — klip tayyorlanmoqda...", True)
        return ai_start, "🤖 AI tahlili"
    if progress_cb:
        await progress_cb("⚠️ AI javob bermadi — bepul tahlilga o'tilmoqda...", True)
    return await _free_heuristic()


# ---- SUV BELGISI (WATERMARK) ----
WATERMARK_LINE1 = "Telegram: @anifilm_bot"
WATERMARK_LINE2 = "Instagram: @anifilm_bot"
_WATERMARK_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]

def _find_watermark_font():
    """Server'da mavjud shrift faylini topadi. Hech biri topilmasa None qaytaradi —
    bu holda ffmpeg fontconfig orqali standart shriftni ishlatishga urinadi (agar
    fontconfig o'rnatilmagan bo'lsa, drawtext xato beradi — shu sabab serverga
    `fonts-dejavu-core` paketini o'rnatish tavsiya etiladi, pastdagi izohga qarang)."""
    for p in _WATERMARK_FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None

def _drawtext_filter(text, font_path, fontsize, y_expr):
    escaped = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    font_part = f"fontfile={font_path}:" if font_path else ""
    return (
        f"drawtext={font_part}text='{escaped}':fontcolor=white:fontsize={fontsize}:"
        f"box=1:boxcolor=black@0.5:boxborderw={max(4, fontsize // 4)}:"
        f"x=(w-text_w)/2:y={y_expr}"
    )

async def _run_ffmpeg_progress(cmd, total_seconds, on_progress=None, stall_timeout=90):
    """`cmd` (ffmpeg buyrug'i, ro'yxat) ni -progress pipe:1 bilan ishga tushiradi.
    ffmpeg joriy pozitsiyasini out_time_ms sifatida chiqarib turadi (bu maydon nomi
    chalg'ituvchi — aslida MIKROsoniya, ffmpeg'ning eski moslik uchun saqlab
    qolingan xatosi). Shundan foizni hisoblab on_progress(percent) callback'iga
    (sync yoki async bo'lishi mumkin) uzatib boradi.
    MUHIM (qotib qolishning oldini olish): agar `stall_timeout` soniya davomida
    progress foizi umuman o'zgarmasa (ffmpeg qotib qolgan/juda sekinlashgan
    hisoblanadi), jarayon majburan o'chiriladi va xatolik qaytariladi — shu
    orqali admin abadiy "qotib qolgan" progress-barni kutib qolmaydi."""
    full_cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    proc = await asyncio.create_subprocess_exec(
        *full_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    last_pct = -1
    last_progress_at = time.monotonic()
    error_tail = []

    async def _stall_watchdog():
        while True:
            await asyncio.sleep(5)
            if time.monotonic() - last_progress_at > stall_timeout:
                if proc.returncode is None:
                    proc.kill()
                return

    watchdog_task = asyncio.create_task(_stall_watchdog())
    try:
        async for raw in proc.stdout:
            line = raw.decode(errors="ignore").strip()
            if not line:
                continue
            if line.startswith("out_time_ms="):
                try:
                    out_us = int(line.split("=", 1)[1])
                    pct = max(0, min(99, int((out_us / 1_000_000) / max(0.1, total_seconds) * 100)))
                    if pct != last_pct:
                        last_pct = pct
                        last_progress_at = time.monotonic()
                        if on_progress:
                            result = on_progress(pct)
                            if asyncio.iscoroutine(result):
                                await result
                except (ValueError, IndexError):
                    pass
            elif "=" not in line:
                error_tail.append(line)
                del error_tail[:-10]
        ret = await proc.wait()
    except asyncio.CancelledError:
        # Bekor qilindi — orfan (egasiz) ffmpeg jarayoni serverda ishlab
        # qolib ketmasligi uchun majburan o'chiramiz.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    finally:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except (asyncio.CancelledError, Exception):
            pass
    if proc.returncode == -9 or proc.returncode == 137:
        raise RuntimeError(
            f"ffmpeg {stall_timeout}s davomida ilgarilamadi (qotib qoldi) — majburan to'xtatildi."
        )
    if ret != 0:
        raise RuntimeError("ffmpeg xato bilan tugadi: " + " / ".join(error_tail[-3:]))
    if on_progress:
        result = on_progress(100)
        if asyncio.iscoroutine(result):
            await result


async def _render_horizontal(path, start, duration, out_path, font_path, progress_cb=None):
    """16:9 (1280x720) format — kerak bo'lsa qora chiziqlar (pad) bilan aniq 16:9
    ga keltiriladi, ustiga suv belgisi qo'yiladi."""
    fs = 28
    vf = (
        "scale=1280:720:force_original_aspect_ratio=decrease,"
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,"
        + _drawtext_filter(WATERMARK_LINE1, font_path, fs, f"h-th-{fs+36}") + ","
        + _drawtext_filter(WATERMARK_LINE2, font_path, fs, f"h-th-{fs}")
    )
    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-i", path, "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        out_path, "-loglevel", "error"
    ]
    await _run_ffmpeg_progress(cmd, duration, progress_cb)

def _has_audio_stream(path):
    """Faylda audio trek bor-yo'qligini tekshiradi (ba'zi kutilmagan hollarda,
    masalan buzilgan/ovozsiz video yuborilsa, xato bermasligi uchun)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    return bool(out.stdout.strip())

async def _render_vertical(path, start, duration, out_path, font_path, has_audio=True, progress_cb=None):
    """9:16 (1080x1920, Instagram Stories/Reels) format.
    MUHIM: avvalgi versiyada fon uchun xiralashtirilgan (blur) kattalashtirilgan
    nusxa + overlay ishlatilar edi — bu ancha og'ir hisoblash bo'lib, serverning
    kuchsiz CPU'sida amalda to'xtab qolar edi (foiz bir joyda "qotib" qolardi).
    Shu sabab endi 16:9 bilan bir xil, YENGIL usul qo'llaniladi: video 1080 kenlikka
    moslab kichraytiriladi va yuqori-pastiga oddiy qora chiziqlar (pad) qo'yiladi.
    Bu overlay/blur'siz, bitta oddiy filtr zanjiri bo'lgani uchun ancha tezroq va
    barqaror ishlaydi."""
    fs = 34
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
        + _drawtext_filter(WATERMARK_LINE1, font_path, fs, f"h-th-{fs+56}") + ","
        + _drawtext_filter(WATERMARK_LINE2, font_path, fs, f"h-th-{fs+6}")
    )
    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-i", path, "-t", str(duration),
        "-vf", vf,
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-movflags", "+faststart", out_path, "-loglevel", "error"
    ]
    await _run_ffmpeg_progress(cmd, duration, progress_cb)

async def _get_channel_message_for_clip(channel_msg_id):
    """STORAGE_CHANNEL'dagi xabarni Pyrogram orqali oladi. Birinchi urinish
    muvaffaqiyatsiz bo'lsa (masalan peer keshi bo'sh bo'lsa), sinxronlash signali
    yuborib qayta uriniladi — xuddi onlayn striming funksiyasidagidek."""
    if not STREAM_ENABLED or not pyro:
        raise RuntimeError(
            "Bu funksiya uchun API_ID/API_HASH sozlanmagan (onlayn striming o'chirilgan). "
            "Render Environment'ga API_ID va API_HASH qo'shing."
        )
    try:
        return await pyro.get_messages(STORAGE_CHANNEL, channel_msg_id)
    except Exception:
        try:
            sync_msg = await bot.send_message(STORAGE_CHANNEL, "🔄")
            await asyncio.sleep(2)
            try:
                await sync_msg.delete()
            except Exception:
                pass
        except Exception:
            pass
        return await pyro.get_messages(STORAGE_CHANNEL, channel_msg_id)

def _progress_bar(pct, width=10):
    """0-100 foizni ▰/▱ dan iborat vizual progress-barga aylantiradi."""
    filled = max(0, min(width, int(round(pct / 100 * width))))
    return "▰" * filled + "▱" * (width - filled)

def _fmt_mmss(seconds):
    """Soniyani MM:SS formatiga o'giradi."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"

async def process_highlight_clip(admin_chat_id, channel_msg_id, duration, status_message):
    """Video yuklab olinadi -> eng qiziqarli joy topiladi -> suv belgisi bilan
    16:9 va 9:16 formatlarda kesiladi -> ikkalasi ham adminga yuboriladi.
    Butun jarayon davomida BITTA xabar tahrirlanib, foiz (%) va o'tgan/qolgan
    vaqt shu yerda ko'rsatib boriladi (haddan tashqari tez-tez tahrirlab,
    Telegram flood-limitiga tegib qolmaslik uchun ~2 soniyada bir marta)."""
    await asyncio.to_thread(os.makedirs, CLIP_TMP_DIR, exist_ok=True)
    ts = int(time.time() * 1000)
    src_path = os.path.join(CLIP_TMP_DIR, f"src_{channel_msg_id}_{ts}.mp4")
    out_h_path = os.path.join(CLIP_TMP_DIR, f"clip16x9_{channel_msg_id}_{ts}.mp4")
    out_v_path = os.path.join(CLIP_TMP_DIR, f"clip9x16_{channel_msg_id}_{ts}.mp4")

    pipeline_start = time.monotonic()
    job_id = f"{status_message.chat.id}_{int(pipeline_start * 1000)}"
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"clipcancel_{job_id}")]
    ])
    progress_msg = await status_message.answer("⏳ Boshlanmoqda...", reply_markup=cancel_kb)
    _clip_jobs[job_id] = asyncio.current_task()
    _edit_state = {"t": 0.0, "text": ""}

    async def set_progress(text, force=False, show_cancel=True):
        """progress_msg'ni tahrirlaydi. `force=False` bo'lsa, matn o'zgarmagan yoki
        oxirgi tahrirdan 2 soniya o'tmagan bo'lsa — o'tkazib yuboriladi (flood-limit).
        `show_cancel=False` — jarayon tugagach (muvaffaqiyatli/xato/bekor qilingan)
        "Bekor qilish" tugmasini xabardan olib tashlash uchun."""
        now = time.monotonic()
        if not force and (text == _edit_state["text"] or now - _edit_state["t"] < 2.0):
            return
        _edit_state["t"] = now
        _edit_state["text"] = text
        try:
            await progress_msg.edit_text(text, reply_markup=cancel_kb if show_cancel else None)
        except Exception:
            pass  # masalan "message is not modified" — bemalol e'tiborsiz qoldiriladi

    def _stage_progress_text(label, pct, elapsed):
        return (
            f"✂️ {label} tayyorlanmoqda...\n\n"
            f"{_progress_bar(pct)} {pct}%\n\n"
            f"📊 Progress : {pct}%\n"
            f"⏱ O'tdi     : {_fmt_mmss(elapsed)}"
        )

    def make_stage_cb(label):
        async def cb(pct):
            elapsed = int(time.monotonic() - pipeline_start)
            await set_progress(_stage_progress_text(label, pct, elapsed))
        return cb

    try:
        msg = await _get_channel_message_for_clip(channel_msg_id)
        media = msg.video or msg.document or msg.animation
        if not media:
            await set_progress("❌ Bu xabarda video topilmadi.", force=True, show_cancel=False)
            await status_message.answer("❌ Bu xabarda video topilmadi.", reply_markup=admin_keyboard())
            return

        def _dl_progress_text(current, total, elapsed):
            pct = int(current / total * 100) if total else 0
            speed = current / elapsed if elapsed > 0.5 else 0
            remain = (total - current) / speed if speed > 0 else None
            cur_mb, tot_mb = current / 1_048_576, (total or 0) / 1_048_576
            remain_mb = max(0.0, tot_mb - cur_mb)
            speed_txt = f"{speed / 1_048_576:.1f} MB/s" if speed > 0 else "—"
            remain_txt = _fmt_mmss(remain) if remain is not None else "—"
            return (
                "📥 Yuklanmoqda...\n\n"
                f"{_progress_bar(pct)} {pct}%\n\n"
                f"📊 Progress : {pct}%\n"
                f"⚡ Tezlik   : {speed_txt}\n"
                f"⏱ O'tdi     : {_fmt_mmss(elapsed)}\n"
                f"⏳ Qoldi    : {remain_txt}\n"
                f"💾 Qoldi    : {remain_mb:.1f} MB\n"
                f"📦 Jami     : {tot_mb:.1f} MB"
            )

        async def dl_progress(current, total):
            elapsed = time.monotonic() - pipeline_start
            await set_progress(_dl_progress_text(current, total, elapsed))

        await set_progress("📥 Yuklanmoqda...", force=True)
        client = _next_stream_client() if _stream_clients else pyro
        await client.download_media(msg, file_name=src_path, progress=dl_progress)

        total_dur = await asyncio.to_thread(_ffprobe_duration, src_path)
        if not total_dur or total_dur <= 0:
            await set_progress("❌ Video davomiyligini aniqlab bo'lmadi (fayl buzilgan bo'lishi mumkin).", force=True, show_cancel=False)
            await status_message.answer("❌ Video davomiyligini aniqlab bo'lmadi.", reply_markup=admin_keyboard())
            return

        if total_dur <= duration:
            start, clip_len, method = 0.0, total_dur, None
        else:
            async def _highlight_progress(text, force=False):
                elapsed = int(time.monotonic() - pipeline_start)
                await set_progress(f"{text} — jami o'tgan vaqt: {elapsed}s", force=force)

            await _highlight_progress("🔎 Qiziqarli joy qidirilmoqda...", force=True)
            start, method = await _find_highlight_start(
                src_path, duration, total_dur, progress_cb=_highlight_progress
            )
            clip_len = float(duration)

        font_path = await asyncio.to_thread(_find_watermark_font)
        has_audio = await asyncio.to_thread(_has_audio_stream, src_path)

        mm, ss = int(start // 60), int(start % 60)
        method_line = f" ({method})" if method else ""
        base_caption = f"✂️ Qiziqarli joy{method_line} — {mm}:{ss:02d} dan {int(clip_len)} soniya"

        # MUHIM: har bir format tayyor bo'lishi bilan DARHOL yuboriladi (ikkalasini
        # ham kutib turilmaydi). Avval bu ikkalasi tugagunicha hech narsa yuborilmas
        # edi — shu sabab 9:16 bosqichi qotib qolsa yoki xato bersa, 16:9 muvaffaqiyatli
        # tayyor bo'lgan bo'lsa ham adminga umuman yetib bormas edi.
        await set_progress(_stage_progress_text("16:9", 0, int(time.monotonic() - pipeline_start)), force=True)
        await _render_horizontal(src_path, start, clip_len, out_h_path, font_path,
                                  progress_cb=make_stage_cb("16:9"))
        await bot.send_video(admin_chat_id, FSInputFile(out_h_path), caption=f"{base_caption}\n📐 16:9")

        await set_progress(_stage_progress_text("9:16", 0, int(time.monotonic() - pipeline_start)), force=True)
        await _render_vertical(src_path, start, clip_len, out_v_path, font_path, has_audio,
                                progress_cb=make_stage_cb("9:16"))
        await bot.send_video(admin_chat_id, FSInputFile(out_v_path), caption=f"{base_caption}\n📱 9:16 (Stories/Reels)")

        total_elapsed = int(time.monotonic() - pipeline_start)
        await set_progress(f"✅ Tayyor! (jami {total_elapsed}s ketdi)", force=True, show_cancel=False)
        await status_message.answer("✅ Tayyor!", reply_markup=admin_keyboard())
    except asyncio.CancelledError:
        await set_progress("❌ Bekor qilindi.", force=True, show_cancel=False)
        raise
    except Exception:
        await set_progress("❌ Xatolik yuz berdi.", force=True, show_cancel=False)
        raise
    finally:
        _clip_jobs.pop(job_id, None)
        for p in (src_path, out_h_path, out_v_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

async def _auto_generate_highlight_clip(message, channel_msg_id):
    """Yangi yuklangan qism uchun klipni FONDA (background) yaratadi — admin
    /done javobini kutmaydi, tayyor bo'lgach natija o'sha adminga yuboriladi.
    process_highlight_clip statusni message.answer(...) orqali shu chatga yozadi."""
    try:
        await message.answer("🎬 Fonda avtomatik reklama klipi tayyorlanmoqda...")
        await process_highlight_clip(message.from_user.id, channel_msg_id, AUTO_CLIP_DURATION, message)
    except Exception as e:
        logger.error(f"[auto-clip] xato: {e}")
        try:
            await message.answer(f"⚠️ Avtomatik klip yaratishda xatolik: {e}")
        except Exception:
            pass

def _clip_duration_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="15 soniya", callback_data="clipdur_15"),
            InlineKeyboardButton(text="30 soniya", callback_data="clipdur_30"),
        ],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ])

@dp.callback_query(F.data == "clip_start")
async def clip_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.clear()
    await call.message.edit_text(
        "✂️ <b>Qiziqarli video kesish</b>\n\n"
        "Bot videoning eng \"qiziqarli\" (ovozi eng baland/energiyaga boy — jang, "
        "kulgi, musiqa avji kabi) joyini avtomatik topib, 15 yoki 30 soniyalik klip "
        "qilib sizga yuboradi.\n\n"
        "Video qayerdan olinsin?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📂 Bazadagi epizod", callback_data="clip_source_db")],
            [InlineKeyboardButton(text="📤 Video yuborish", callback_data="clip_source_upload")],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "clip_source_db")
async def clip_source_db(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text("🎬 Anime/filmni tanlash usuli:", reply_markup=search_method_keyboard("clipa"))

@dp.callback_query(F.data == "clipa_list")
async def clipa_list(call: CallbackQuery, state: FSMContext):
    animes = await asyncio.to_thread(db.get_animes, None, 0)
    total = await asyncio.to_thread(db.get_anime_count)
    if not animes:
        await call.answer("📺 Hozircha kontent yo'q!", show_alert=True)
        return
    await call.message.edit_text(
        "Anime/filmni tanlang:",
        reply_markup=admin_anime_list_keyboard(animes, 0, total, "clipa_sel")
    )

@dp.callback_query(F.data == "clipa_search")
async def clipa_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(ClipVideo.search_query)
    await call.message.edit_text("🔍 Nomini yozing:")

@dp.message(ClipVideo.search_query)
async def clipa_search_result(message: Message, state: FSMContext):
    results = await asyncio.to_thread(db.search_anime, message.text.strip())
    if not results:
        await message.answer("❌ Topilmadi!")
        return
    await state.clear()
    buttons = [[InlineKeyboardButton(text=a["title"], callback_data=f"clipa_sel_{a['id']}")] for a in results]
    await message.answer("Tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("clipa_sel_page_"))
async def clipa_sel_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split("_")[-1])
    animes = await asyncio.to_thread(db.get_animes, None, page)
    total = await asyncio.to_thread(db.get_anime_count)
    await call.message.edit_text(
        "Anime/filmni tanlang:",
        reply_markup=admin_anime_list_keyboard(animes, page, total, "clipa_sel")
    )

@dp.callback_query(F.data.regexp(r"^clipa_sel_(\d+)$"))
async def clipa_sel(call: CallbackQuery, state: FSMContext):
    anime_id = int(call.data.split("_")[-1])
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    if not episodes:
        await call.answer("❌ Bu anime uchun hali video yuklanmagan!", show_alert=True)
        return
    buttons = []
    row = []
    for ep in episodes:
        row.append(InlineKeyboardButton(text=f"{ep['episode_number']}-qism", callback_data=f"clipep_{ep['id']}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")])
    await call.message.edit_text("Qismni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.regexp(r"^clipep_(\d+)$"))
async def clipep_selected(call: CallbackQuery, state: FSMContext):
    ep_id = int(call.data.split("_")[1])
    ep = await asyncio.to_thread(db.get_episode, ep_id)
    if not ep:
        await call.answer("❌ Topilmadi!", show_alert=True)
        return
    await state.update_data(clip_channel_msg_id=ep["channel_message_id"])
    await call.message.edit_text("⏱ Klip davomiyligini tanlang:", reply_markup=_clip_duration_keyboard())

@dp.callback_query(F.data == "clip_source_upload")
async def clip_source_upload(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(ClipVideo.waiting_video)
    await call.message.edit_text("🎬 Kesish uchun videoni yuboring:")

@dp.message(ClipVideo.waiting_video, F.video)
async def clip_upload_video(message: Message, state: FSMContext):
    sent = await bot.forward_message(STORAGE_CHANNEL, message.chat.id, message.message_id)
    await state.update_data(clip_channel_msg_id=sent.message_id)
    await state.set_state(None)
    await message.answer("⏱ Klip davomiyligini tanlang:", reply_markup=_clip_duration_keyboard())

@dp.callback_query(F.data.regexp(r"^clipdur_(15|30)$"))
async def clipdur_selected(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    duration = int(call.data.split("_")[1])
    data = await state.get_data()
    channel_msg_id = data.get("clip_channel_msg_id")
    if not channel_msg_id:
        await call.answer("❌ Video topilmadi, qaytadan boshlang.", show_alert=True)
        return
    await state.clear()
    await call.message.edit_text("⏳ Boshlanmoqda...")
    try:
        await process_highlight_clip(call.from_user.id, channel_msg_id, duration, call.message)
    except asyncio.CancelledError:
        pass  # foydalanuvchi "❌ Bekor qilish" tugmasini bosgan — bu holat kutilgan
    except Exception as e:
        logger.error(f"[clip] xato: {e}")
        await call.message.answer(f"❌ Xatolik yuz berdi: {e}", reply_markup=admin_keyboard())

@dp.callback_query(F.data.startswith("clipcancel_"))
async def clip_cancel_handler(call: CallbackQuery):
    """Klip yaratish jarayonidagi "❌ Bekor qilish" tugmasi. Mos job_id bo'yicha
    ishlayotgan Task topilib .cancel() qilinadi — bu process_highlight_clip
    ichida joriy await nuqtasida (yuklab olish yoki ffmpeg) CancelledError
    sifatida ko'tariladi va u yerda tozalab, xabarni yangilaydi."""
    if not await is_admin_user(call.from_user.id):
        return
    job_id = call.data[len("clipcancel_"):]
    task = _clip_jobs.get(job_id)
    if task and not task.done():
        task.cancel()
        await call.answer("Bekor qilinmoqda...")
    else:
        await call.answer("Bu jarayon allaqachon tugagan.", show_alert=True)

# ---- STATISTIKA ----
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    s = await asyncio.to_thread(db.get_stats)
    top_text = ""
    for i, a in enumerate(s["top"], 1):
        top_text += f"{i}. {a['title']} — {a['views']} marta\n"
    await call.message.edit_text(
        f"📊 <b>Statistika</b>\n\n"
        f"👥 Jami: {s['total']}\n"
        f"✅ Faol: {s['active']}\n"
        f"🚫 Bloklangan: {s['blocked']}\n\n"
        f"📺 Jami animlar: {s['total_animes']}\n"
        f"🎬 Filmlar: {s['films']}\n"
        f"📺 Seriallar: {s['serials']}\n\n"
        f"📈 Bugun: {s['today']}\n"
        f"📈 Hafta: {s['week']}\n"
        f"📈 Oy: {s['month']}\n\n"
        f"🔥 <b>Eng kop korilgan:</b>\n{top_text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📈 O'sish grafigi", callback_data="admin_growth_chart", style="primary")],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

def _sparkline(values):
    """Qiymatlar ro'yxatini Unicode blok-grafik (▁▂▃▄▅▆▇█) satriga aylantiradi."""
    blocks = "▁▂▃▄▅▆▇█"
    vmax = max(values) if values and max(values) > 0 else 1
    return "".join(blocks[min(7, int((v / vmax) * 7))] for v in values)

@dp.callback_query(F.data == "admin_growth_chart")
async def admin_growth_chart(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    growth = await asyncio.to_thread(db.get_growth_stats, 7)
    users_vals = [g["new_users"] for g in growth]
    views_vals = [g["views"] for g in growth]
    days_labels = " ".join(g["date"][5:] for g in growth)  # MM-DD
    lines = "\n".join(
        f"{g['date'][5:]}   👥 {g['new_users']:<4} 👁 {g['views']}" for g in growth
    )
    await call.message.edit_text(
        f"📈 <b>Oxirgi 7 kunlik o'sish</b>\n\n"
        f"👥 Yangi foydalanuvchilar:\n<code>{_sparkline(users_vals)}</code>\n\n"
        f"👁 Ko'rishlar:\n<code>{_sparkline(views_vals)}</code>\n\n"
        f"📅 <b>Kunlik tafsilot:</b>\n<code>{lines}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Statistika", callback_data="admin_stats")],
        ]),
        parse_mode="HTML"
    )

# ---- ADMIN FAOLIYATI LOGI ----
@dp.callback_query(F.data == "admin_activity_log")
async def admin_activity_log(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    logs = await asyncio.to_thread(db.get_admin_logs, 20)
    if not logs:
        text = "📜 <b>Admin faoliyati</b>\n\nHozircha yozuv yo'q."
    else:
        lines = []
        for lg in logs:
            when = (lg.get("created_at") or "")[5:16].replace("-", ".")
            details = f" — {lg['details']}" if lg.get("details") else ""
            lines.append(f"🕒 {when} | {lg['admin_name']}\n   {lg['action']}{details}")
        text = "📜 <b>Admin faoliyati (oxirgi 20 ta)</b>\n\n" + "\n\n".join(lines)
    await call.message.edit_text(text, reply_markup=admin_back(), parse_mode="HTML")

# ---- XABAR YUBORISH ----
# Telegram Bot API'ning umumiy flood-limitidan (~30 xabar/soniya) xavfsiz
# pastroq tezlik — broadcast paytida FLOOD_WAIT xatolariga uchramaslik uchun.
BROADCAST_DELAY = 0.05  # ~20 xabar/soniya

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(BroadcastState.choose_type)
    await call.message.edit_text(
        "📨 Xabar turi:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Oddiy xabar", callback_data="bc_simple")],
            [InlineKeyboardButton(text="🔘 Inline tugmali xabar", callback_data="bc_inline")],
            [InlineKeyboardButton(text="❌ Bekor", callback_data="admin_back")],
        ])
    )

@dp.callback_query(F.data == "bc_simple")
async def bc_simple(call: CallbackQuery, state: FSMContext):
    await state.update_data(bc_type="simple")
    await state.set_state(BroadcastState.message)
    await call.message.edit_text("📝 Xabarni yozing:")

@dp.callback_query(F.data == "bc_inline")
async def bc_inline(call: CallbackQuery, state: FSMContext):
    await state.update_data(bc_type="inline")
    await state.set_state(BroadcastState.message)
    await call.message.edit_text("📝 Xabar matnini yozing:")

@dp.message(BroadcastState.message)
async def bc_message(message: Message, state: FSMContext):
    await state.update_data(
        bc_message_id=message.message_id, bc_chat_id=message.chat.id,
        bc_text=message.text or message.caption or ""
    )
    data = await state.get_data()
    if data["bc_type"] == "inline":
        await state.set_state(BroadcastState.button_text)
        await message.answer("🔘 Tugma nomini yozing:")
    else:
        users = await asyncio.to_thread(db.get_all_active_users)
        await state.set_state(BroadcastState.confirm)
        await message.answer(
            f"⚠️ {len(users)} ta foydalanuvchiga yuborasizmi?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Ha", callback_data="bc_send"),
                    InlineKeyboardButton(text="❌ Bekor", callback_data="admin_back"),
                ]
            ])
        )

@dp.message(BroadcastState.button_text)
async def bc_button_text(message: Message, state: FSMContext):
    await state.update_data(bc_button_text=message.text)
    await state.set_state(BroadcastState.button_link)
    await message.answer("🔗 Tugma linkini yozing:")

@dp.message(BroadcastState.button_link)
async def bc_button_link(message: Message, state: FSMContext):
    await state.update_data(bc_button_link=message.text)
    users = await asyncio.to_thread(db.get_all_active_users)
    await state.set_state(BroadcastState.confirm)
    await message.answer(
        f"⚠️ {len(users)} ta foydalanuvchiga yuborasizmi?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Ha", callback_data="bc_send"),
                InlineKeyboardButton(text="❌ Bekor", callback_data="admin_back"),
            ]
        ])
    )

@dp.callback_query(F.data == "bc_send")
async def bc_send(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    users = await asyncio.to_thread(db.get_all_active_users)
    sent = 0
    failed = 0
    kb = None
    if data.get("bc_type") == "inline" and data.get("bc_button_text"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=data["bc_button_text"], url=data["bc_button_link"])]
        ])
    for user_id in users:
        # FLOOD_WAIT bo'lsa (Telegram vaqtincha to'xtatsa), bir marta kutib
        # qayta uriniladi — avval bu holat oddiy "failed" deb hisoblanardi va
        # xabar shu foydalanuvchiga umuman yetib bormasdi.
        for attempt in range(2):
            try:
                await bot.copy_message(
                    user_id,
                    data["bc_chat_id"],
                    data["bc_message_id"],
                    reply_markup=kb
                )
                sent += 1
                break
            except TelegramForbiddenError:
                await mark_user_left(user_id)
                failed += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                continue
            except Exception:
                failed += 1
                break
        # Har xabardan keyin qisqa pauza — Telegramning flood-limitiga
        # (~30 xabar/soniya) uchramaslik uchun.
        await asyncio.sleep(BROADCAST_DELAY)
    # Webapp 🔔 paneli uchun e'lon bildirishnomasi (faqat matnli/caption'li xabar bo'lsa)
    if data.get("bc_text"):
        title = data["bc_text"].strip().splitlines()[0][:120]
        await asyncio.to_thread(db.create_notification, "announcement", title, body=data["bc_text"])
    await call.message.edit_text(
        f"📨 Yuborildi!\n✅ {sent} ta\n❌ {failed} ta",
        reply_markup=admin_back()
    )

# ---- KANALLAR ----
@dp.callback_query(F.data == "admin_channels")
async def admin_channels(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    channels = await asyncio.to_thread(db.get_channels)
    text = "📢 <b>Majburiy kanallar</b>\n\n"
    if channels:
        for ch in channels:
            text += f"• {ch['channel_name']} ({ch['channel_id']})\n"
    else:
        text += "Hozircha kanal yoq."
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Qoshish", callback_data="ch_add")],
            [InlineKeyboardButton(text="🗑 Ochirish", callback_data="ch_del")],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "ch_add")
async def ch_add(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(AddChannelState.channel)
    await call.message.edit_text(
        "📢 Format: @kanalnom | Kanal nomi\nMasalan: @anime_uz | Anime UZ"
    )

@dp.message(AddChannelState.channel)
async def ch_add_done(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    await state.clear()
    parts = message.text.split("|")
    if len(parts) != 2:
        await message.answer("❌ Format notogri!\nMasalan: @anime_uz | Anime UZ")
        return
    await asyncio.to_thread(db.add_channel, parts[0].strip(), parts[1].strip())
    await message.answer("✅ Kanal qoshildi!", reply_markup=admin_keyboard())

@dp.callback_query(F.data == "ch_del")
async def ch_del(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    channels = await asyncio.to_thread(db.get_channels)
    if not channels:
        await call.answer("Kanal yoq!", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(
        text=ch["channel_name"],
        callback_data=f"ch_del_{ch['channel_id']}"
    )] for ch in channels]
    buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_channels")])
    await call.message.edit_text(
        "O'chirish uchun kanalni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("ch_del_"))
async def ch_del_done(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    channel_id = call.data.replace("ch_del_", "")
    await asyncio.to_thread(db.delete_channel, channel_id)
    await call.answer("🗑 Ochirildi!", show_alert=True)
    await admin_channels(call)

# ---- BLOKLASH ----
@dp.callback_query(F.data == "admin_block")
async def admin_block_menu(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text(
        "🚫 <b>Bloklash</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Bloklash", callback_data="do_block")],
            [InlineKeyboardButton(text="✅ Blokdan chiqarish", callback_data="do_unblock")],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "do_block")
async def do_block(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(BlockState.user_id)
    await call.message.edit_text("🚫 Foydalanuvchi ID yoki @username yozing:")

@dp.message(BlockState.user_id)
async def block_action(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    await state.clear()
    query = message.text.strip()
    if query.startswith("@"):
        u = await asyncio.to_thread(db.get_user_by_username, query)
    else:
        try:
            u = await asyncio.to_thread(db.get_user, int(query))
        except Exception:
            u = None
    if not u:
        await message.answer("❌ Topilmadi!", reply_markup=admin_keyboard())
        return
    await asyncio.to_thread(db.block_user, u["user_id"])
    await log_admin_action(message.from_user, "Foydalanuvchini bloklandi", f"{u['full_name']} (ID: {u['user_id']})")
    await message.answer(
        f"🚫 <b>{u['full_name']}</b> bloklandi!",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "do_unblock")
async def do_unblock(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(UnblockState.user_id)
    await call.message.edit_text("✅ Blokdan chiqarish uchun ID yoki @username yozing:")

@dp.message(UnblockState.user_id)
async def unblock_action(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    await state.clear()
    query = message.text.strip()
    if query.startswith("@"):
        u = await asyncio.to_thread(db.get_user_by_username, query)
    else:
        try:
            u = await asyncio.to_thread(db.get_user, int(query))
        except Exception:
            u = None
    if not u:
        await message.answer("❌ Topilmadi!", reply_markup=admin_keyboard())
        return
    await asyncio.to_thread(db.unblock_user, u["user_id"])
    await log_admin_action(message.from_user, "Foydalanuvchini blokdan chiqardi", f"{u['full_name']} (ID: {u['user_id']})")
    await message.answer(
        f"✅ <b>{u['full_name']}</b> blokdan chiqarildi!",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )

# ---- TEXNIK ISHLAR ----
@dp.callback_query(F.data == "admin_maintenance")
async def admin_maintenance(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    current = await asyncio.to_thread(db.get_setting, "maintenance")
    status = "✅ Yoqiq" if current == "1" else "❌ Ochiq"
    await call.message.edit_text(
        f"🔧 <b>Texnik ishlar</b>\nHolat: {status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yoqish", callback_data="maint_on"),
                InlineKeyboardButton(text="❌ Ochirish", callback_data="maint_off"),
            ],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.in_(["maint_on", "maint_off"]))
async def set_maintenance(call: CallbackQuery):
    value = "1" if call.data == "maint_on" else "0"
    await asyncio.to_thread(db.set_setting, "maintenance", value)
    status = "✅ Yoqildi" if value == "1" else "❌ Ochirildi"
    await log_admin_action(call.from_user, "Texnik ishlar rejimi", status)
    await call.answer(f"🔧 {status}", show_alert=True)
    await admin_maintenance(call)

# ---- PROFIL BO'LIMI (bepul foydalanuvchilar uchun vaqtincha yopish) ----
@dp.callback_query(F.data == "admin_profile_lock")
async def admin_profile_lock(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    current = await asyncio.to_thread(db.get_setting, "profile_disabled_for_free")
    status = "✅ Yopiq (faqat Premium kira oladi)" if current == "1" else "❌ Ochiq (hammaga)"
    await call.message.edit_text(
        f"👤 <b>Profil bo'limi (bepul foydalanuvchilar uchun)</b>\n"
        f"Holat: {status}\n\n"
        f"Yoqilsa — Premium bo'lmagan foydalanuvchilar Webappdagi Profil "
        f"bo'limini ocholmaydi, oʻrniga Premium sotib olish taklifini koʻradi. "
        f"Premium foydalanuvchilar va admin har doim kira oladi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yopish", callback_data="proflock_on"),
                InlineKeyboardButton(text="❌ Ochish", callback_data="proflock_off"),
            ],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.in_(["proflock_on", "proflock_off"]))
async def set_profile_lock(call: CallbackQuery):
    value = "1" if call.data == "proflock_on" else "0"
    await asyncio.to_thread(db.set_setting, "profile_disabled_for_free", value)
    status = "✅ Yopildi" if value == "1" else "❌ Ochildi"
    await log_admin_action(call.from_user, "Profil bo'limi (bepul foydalanuvchilar)", status)
    await call.answer(f"👤 {status}", show_alert=True)
    await admin_profile_lock(call)

# ---- KONTENT HIMOYASI ----
@dp.callback_query(F.data == "admin_content")
async def admin_content(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    current = await asyncio.to_thread(db.get_setting, "content_protect")
    status = "✅ Yoqiq" if current == "1" else "❌ Ochiq"
    await call.message.edit_text(
        f"🔒 <b>Kontent himoyasi</b>\nHolat: {status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yoqish", callback_data="cont_on"),
                InlineKeyboardButton(text="❌ Ochirish", callback_data="cont_off"),
            ],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.in_(["cont_on", "cont_off"]))
async def set_content(call: CallbackQuery):
    value = "1" if call.data == "cont_on" else "0"
    await asyncio.to_thread(db.set_setting, "content_protect", value)
    await call.answer("✅ Saqlandi!", show_alert=True)
    await admin_content(call)

# ---- AVTOMATIK BLOKLASH ----
# Foydalanuvchi botni bloklab/chiqib ketganda, tizim uni avtomatik ravishda
# "bloklangan" deb belgilashi kerakmi? Yoqiq bo'lsa — qaytib kelib blokdan
# chiqarsa ham, admin qo'lda blokdan chiqarmaguncha botdan foydalana olmaydi.
# O'chiq bo'lsa — faqat "nofaol" deb belgilanadi, qaytib kelsa erkin foydalanadi.
@dp.callback_query(F.data == "admin_autoblock")
async def admin_autoblock(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    current = await asyncio.to_thread(db.get_setting, "auto_block_on_leave")
    enabled = current != "0"  # standart holat — yoqiq
    status = "✅ Yoqiq" if enabled else "❌ O'chirilgan"
    await call.message.edit_text(
        f"🔒 <b>Avtomatik bloklash</b>\n\n"
        f"Foydalanuvchi botni bloklab/chiqib ketsa, tizim uni avtomatik "
        f"bloklaydi (qaytib kelsa ham, admin blokdan chiqarmaguncha botdan "
        f"foydalana olmaydi).\n\nHolat: {status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yoqish", callback_data="autoblk_on"),
                InlineKeyboardButton(text="❌ Ochirish", callback_data="autoblk_off"),
            ],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.in_(["autoblk_on", "autoblk_off"]))
async def set_autoblock(call: CallbackQuery):
    value = "1" if call.data == "autoblk_on" else "0"
    await asyncio.to_thread(db.set_setting, "auto_block_on_leave", value)
    await call.answer("✅ Saqlandi!", show_alert=True)
    await admin_autoblock(call)

async def _notify_admin_user_left(user_id, full_name, username, join_number, auto_block):
    status_line = "🔒 Avtomatik bloklandi." if auto_block else "⚪️ Nofaol deb belgilandi (avtomatik bloklash o'chirilgan)."
    text = (
        f"🚫 <b>Foydalanuvchi chiqib ketdi!</b>\n\n"
        f"📌 Ism: {full_name}\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"👤 Username: @{username or 'yoq'}\n"
        f"🔢 Raqam: {join_number if join_number else '?'}-chi\n\n"
        f"{status_line}"
    )
    for attempt in range(2):
        try:
            await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            continue
        except Exception as e:
            logger.warning(f"[mark_user_left] adminga xabar yuborilmadi (user {user_id}): {e}")
            return

async def mark_user_left(user_id, tg_user=None):
    """Foydalanuvchi botni bloklab/chiqib ketganda chaqiriladi — bir nechta joydan
    (my_chat_member KICKED hodisasi, broadcast, obunachilarga xabar yuborish va h.k.)
    chaqirilishi mumkin, chunki Telegramning 'bloklandi' hodisasi yolg'iz o'zi
    yetarlicha tez/ishonchli emas. 'Avtomatik bloklash' sozlamasiga qarab, foydalanuvchini
    to'liq bloklaydi yoki faqat nofaol deb belgilaydi — va FAQAT u ilgari faol bo'lgan
    bo'lsa admiga xabar yuboradi, shu bilan bir xil foydalanuvchi uchun bir nechta
    joydan takroriy xabar kelishining oldi olinadi."""
    u = await asyncio.to_thread(db.get_user, user_id)
    was_active = bool(u and u.get("is_active") and not u.get("is_blocked"))

    auto_block = await asyncio.to_thread(db.get_setting, "auto_block_on_leave") != "0"
    if auto_block:
        await asyncio.to_thread(db.set_user_inactive, user_id)
    else:
        await asyncio.to_thread(db.set_user_left_only, user_id)

    if not was_active:
        return  # allaqachon nofaol/bloklangan edi — qayta xabar bermaymiz

    full_name = (tg_user.full_name if tg_user else None) or (u.get("full_name") if u else None) or "Nomalum"
    username = (tg_user.username if tg_user else None) or (u.get("username") if u else None)
    join_number = u.get("join_number") if u else None
    await _notify_admin_user_left(user_id, full_name, username, join_number, auto_block)

@dp.callback_query(F.data == "admin_autoclip")
async def admin_autoclip(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    enabled = await is_auto_clip_enabled()
    status = "✅ Yoqiq" if enabled else "❌ O'chirilgan"
    await call.message.edit_text(
        "🎬 <b>Avtomatik reklama klipi</b>\n\n"
        "Yangi qism/film yuklab, <code>/done</code> yozilgach, bot fonda "
        "o'zi \"eng qiziqarli joy\" klipini (16:9 va 9:16) tayyorlab, "
        "adminga yuborishi kerakmi?\n\n"
        f"Holat: {status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yoqish", callback_data="autoclip_on"),
                InlineKeyboardButton(text="❌ Ochirish", callback_data="autoclip_off"),
            ],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.in_(["autoclip_on", "autoclip_off"]))
async def set_autoclip(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    value = "1" if call.data == "autoclip_on" else "0"
    await asyncio.to_thread(db.set_setting, "auto_clip_enabled", value)
    await call.answer("✅ Saqlandi!", show_alert=True)
    await admin_autoclip(call)

# ---- WEBAPP HAVOLALARI (Kanal / Support) ----
async def _links_text():
    channel = await asyncio.to_thread(db.get_setting, "profile_channel_url") or "❌ Sozlanmagan"
    support = await asyncio.to_thread(db.get_setting, "profile_support_url") or "❌ Sozlanmagan"
    return f"🔗 <b>Webapp Profil havolalari</b>\n\n📢 Kanal: {channel}\n❓ Support: {support}"

@dp.callback_query(F.data == "admin_links")
async def admin_links(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.clear()
    await call.message.edit_text(
        await _links_text(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Kanalni o'zgartirish", callback_data="link_set_channel")],
            [InlineKeyboardButton(text="❓ Supportni o'zgartirish", callback_data="link_set_support")],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.in_(["link_set_channel", "link_set_support"]))
async def link_set_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    key = "profile_channel_url" if call.data == "link_set_channel" else "profile_support_url"
    label = "Telegram kanal" if key == "profile_channel_url" else "Support (foydalanuvchi/kanal)"
    await state.update_data(link_key=key)
    await state.set_state(LinksState.new_value)
    await call.message.edit_text(
        f"✏️ <b>{label}</b> havolasini yuboring.\n\nMasalan: <code>https://t.me/anifilm_kanal</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_links")],
        ]),
        parse_mode="HTML"
    )

@dp.message(LinksState.new_value)
async def link_set_save(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not value.startswith("http"):
        await message.answer("❌ Havola https:// bilan boshlanishi kerak. Qaytadan yuboring.")
        return
    data = await state.get_data()
    key = data.get("link_key")
    await asyncio.to_thread(db.set_setting, key, value)
    await state.clear()
    await message.answer("✅ Saqlandi!", reply_markup=admin_back())
    await message.answer(await _links_text(), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalni o'zgartirish", callback_data="link_set_channel")],
        [InlineKeyboardButton(text="❓ Supportni o'zgartirish", callback_data="link_set_support")],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ]), parse_mode="HTML")

# ---- IZOHLAR SOʻZ FILTRI ----
async def _wordfilter_text():
    raw = await asyncio.to_thread(db.get_setting, "banned_words") or ""
    words = [w.strip() for w in raw.split(",") if w.strip()]
    if not words:
        return "🚫 <b>Taqiqlangan soʻzlar filtri</b>\n\nHozircha roʻyxat boʻsh."
    return "🚫 <b>Taqiqlangan soʻzlar filtri</b>\n\n" + ", ".join(f"<code>{w}</code>" for w in words)

def _wordfilter_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Soʻz qoʻshish", callback_data="wf_add")],
        [InlineKeyboardButton(text="🗑 Roʻyxatni tozalash", callback_data="wf_clear")],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ])

@dp.callback_query(F.data == "admin_wordfilter")
async def admin_wordfilter(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.clear()
    await call.message.edit_text(await _wordfilter_text(), reply_markup=_wordfilter_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "wf_add")
async def wf_add_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(WordFilterState.add_words)
    await call.message.edit_text(
        "✏️ Taqiqlanadigan soʻz(lar)ni yuboring.\n\nBir nechtasini vergul bilan ajratib yozing:\n<code>soʻz1, soʻz2, soʻz3</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_wordfilter")],
        ]),
        parse_mode="HTML"
    )

@dp.message(WordFilterState.add_words)
async def wf_add_save(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    new_words = [w.strip().lower() for w in (message.text or "").split(",") if w.strip()]
    if not new_words:
        await message.answer("❌ Boʻsh yuborildi. Qaytadan urinib koʻring.")
        return
    raw = await asyncio.to_thread(db.get_setting, "banned_words") or ""
    existing = [w.strip().lower() for w in raw.split(",") if w.strip()]
    combined = sorted(set(existing + new_words))
    await asyncio.to_thread(db.set_setting, "banned_words", ", ".join(combined))
    await state.clear()
    await message.answer(f"✅ Qoʻshildi! Jami: {len(combined)} ta soʻz.")
    await message.answer(await _wordfilter_text(), reply_markup=_wordfilter_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "wf_clear")
async def wf_clear(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await asyncio.to_thread(db.set_setting, "banned_words", "")
    await call.answer("✅ Roʻyxat tozalandi", show_alert=True)
    await call.message.edit_text(await _wordfilter_text(), reply_markup=_wordfilter_kb(), parse_mode="HTML")

# ---- SPONSOR BANER (webapp bosh sahifasi) ----
async def _sponsor_text():
    title = await asyncio.to_thread(db.get_setting, "sponsor_title")
    url = await asyncio.to_thread(db.get_setting, "sponsor_url")
    photo = await asyncio.to_thread(db.get_setting, "sponsor_photo_id")
    if not photo:
        return "📢 <b>Sponsor baner</b>\n\nHozircha sozlanmagan. Webapp bosh sahifasida koʻrinmaydi."
    return (
        f"📢 <b>Sponsor baner</b>\n\n"
        f"🖼 Rasm: ✅ yuklangan\n"
        f"📝 Sarlavha: {title or '—'}\n"
        f"🔗 Havola: {url or '—'}"
    )

def _sponsor_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 Rasmni oʻrnatish", callback_data="sp_photo")],
        [InlineKeyboardButton(text="✏️ Sarlavhani oʻrnatish", callback_data="sp_title")],
        [InlineKeyboardButton(text="🔗 Havolani oʻrnatish", callback_data="sp_url")],
        [InlineKeyboardButton(text="🗑 Banerni oʻchirish", callback_data="sp_delete")],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ])

@dp.callback_query(F.data == "admin_sponsor")
async def admin_sponsor(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.clear()
    await call.message.edit_text(await _sponsor_text(), reply_markup=_sponsor_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "sp_photo")
async def sp_photo_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(SponsorState.photo)
    await call.message.edit_text("🖼 Sponsor baner rasmini yuboring:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_sponsor")],
    ]))

@dp.message(SponsorState.photo, F.photo)
async def sp_photo_save(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    await asyncio.to_thread(db.set_setting, "sponsor_photo_id", message.photo[-1].file_id)
    await state.clear()
    await message.answer("✅ Rasm saqlandi!")
    await message.answer(await _sponsor_text(), reply_markup=_sponsor_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "sp_title")
async def sp_title_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(SponsorState.title)
    await call.message.edit_text("✏️ Sponsor baner sarlavhasini yuboring (masalan: \"Bizning boshqa botimiz\"):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_sponsor")],
    ]))

@dp.message(SponsorState.title)
async def sp_title_save(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    await asyncio.to_thread(db.set_setting, "sponsor_title", (message.text or "").strip()[:80])
    await state.clear()
    await message.answer("✅ Sarlavha saqlandi!")
    await message.answer(await _sponsor_text(), reply_markup=_sponsor_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "sp_url")
async def sp_url_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(SponsorState.url)
    await call.message.edit_text("🔗 Bosilganda ochiladigan havolani yuboring (https:// bilan):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_sponsor")],
    ]))

@dp.message(SponsorState.url)
async def sp_url_save(message: Message, state: FSMContext):
    if not await is_admin_user(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not value.startswith("http"):
        await message.answer("❌ Havola https:// bilan boshlanishi kerak. Qaytadan yuboring.")
        return
    await asyncio.to_thread(db.set_setting, "sponsor_url", value)
    await state.clear()
    await message.answer("✅ Havola saqlandi!")
    await message.answer(await _sponsor_text(), reply_markup=_sponsor_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "sp_delete")
async def sp_delete(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await asyncio.to_thread(db.set_setting, "sponsor_photo_id", "")
    await asyncio.to_thread(db.set_setting, "sponsor_title", "")
    await asyncio.to_thread(db.set_setting, "sponsor_url", "")
    await call.answer("✅ Baner oʻchirildi", show_alert=True)
    await call.message.edit_text(await _sponsor_text(), reply_markup=_sponsor_kb(), parse_mode="HTML")

# ---- FOYDALANUVCHI QIDIRISH ----
@dp.callback_query(F.data == "admin_find_user")
async def admin_find_user(call: CallbackQuery, state: FSMContext):
    if not await is_admin_user(call.from_user.id):
        return
    await state.set_state(FindUserState.query)
    await call.message.edit_text("🔍 Foydalanuvchi ID yoki @username yozing:")

@dp.message(FindUserState.query)
async def find_user_result(message: Message, state: FSMContext):
    await state.clear()
    query = message.text.strip()
    if query.startswith("@"):
        u = await asyncio.to_thread(db.get_user_by_username, query)
    else:
        try:
            u = await asyncio.to_thread(db.get_user, int(query))
        except Exception:
            u = None
    if not u:
        await message.answer("❌ Topilmadi!", reply_markup=admin_keyboard())
        return
    status = "🚫 Bloklangan" if u.get("is_blocked") else "✅ Faol"
    await message.answer(
        f"👤 <b>Foydalanuvchi</b>\n\n"
        f"📌 Ism: {u['full_name']}\n"
        f"🔢 Raqam: {u['join_number']}-chi\n"
        f"🆔 ID: <code>{u['user_id']}</code>\n"
        f"👤 Username: @{u['username'] or 'yoq'}\n"
        f"📱 Telefon: {u['phone'] or 'yoq'}\n"
        f"📅 Qoshilgan: {u['joined_at'][:10]}\n"
        f"📊 Holat: {status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Profilni ko'rish", url=f"tg://user?id={u['user_id']}")],
            [InlineKeyboardButton(text="🎁 Premium berish", callback_data=f"admgift_direct_{u['user_id']}", style="success")],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")]
        ]),
        parse_mode="HTML"
    )

# ---- KUNLIK HISOBOT ----
@dp.callback_query(F.data == "admin_report")
async def admin_report(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    from datetime import datetime
    s = await asyncio.to_thread(db.get_daily_stats)
    today = datetime.now().strftime("%d.%m.%Y")
    await call.message.edit_text(
        f"📅 <b>Kunlik hisobot — {today}</b>\n\n"
        f"👥 Bugun qoshildi: {s['new_users']}\n"
        f"🚫 Bugun chiqib ketdi: {s['left_users']}\n"
        f"🆕 Bugun qoshilgan anime: {s['new_animes']}\n"
        f"📺 Jami korishlar: {s['total_views']}",
        reply_markup=admin_back(),
        parse_mode="HTML"
    )

# ---- DAROMAD STATISTIKASI ----
_PLAN_LABELS = {"1m": "1 oy", "3m": "3 oy", "1y": "1 yil"}

@dp.callback_query(F.data == "admin_revenue")
async def admin_revenue(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    r = await asyncio.to_thread(db.get_revenue_stats)

    plan_lines = ""
    for plan_key, label in _PLAN_LABELS.items():
        info = r["by_plan"].get(plan_key)
        if info:
            plan_lines += f"  • {label}: {info['cnt']} ta — {fmt_som(info['total'])}\n"
        else:
            plan_lines += f"  • {label}: 0 ta — {fmt_som(0)}\n"

    if r["by_month"]:
        month_lines = "\n".join(
            f"  • {m['month']}: {fmt_som(m['total'])} ({m['cnt']} ta)" for m in r["by_month"]
        )
    else:
        month_lines = "  Hozircha maʼlumot yoʻq"

    await call.message.edit_text(
        f"💰 <b>Daromad statistikasi</b>\n\n"
        f"💵 Jami daromad: <b>{fmt_som(r['total'])}</b> ({r['total_cnt']} ta toʻlov)\n"
        f"📆 Bugun: {fmt_som(r['today'])} ({r['today_cnt']} ta)\n"
        f"🗓 Oxirgi 30 kun: {fmt_som(r['last30'])} ({r['last30_cnt']} ta)\n\n"
        f"📦 <b>Reja boʻyicha taqsimot:</b>\n{plan_lines}\n"
        f"📈 <b>Oylik tushum (oxirgi 6 oy):</b>\n{month_lines}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Statistika", callback_data="admin_cat_stats")],
        ]),
        parse_mode="HTML"
    )

# ---- ADMIN QO'LLANMA ----
@dp.callback_query(F.data == "admin_help")
async def admin_help(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    await call.message.edit_text(
        "📖 <b>Admin Qoʻllanma</b>\n\n"

        "📚 <b>Kontent boshqaruvi</b>\n"
        "➕ Anime qoʻshish: Rasm → Nom → Yil → Davlat → Janr → Malumot → "
        "Til → Tur (film/serial) → Videolarni yuboring → /done\n"
        "➕ Davom (qism) qoʻshish: Roʻyxat yoki nom → Serial tanlang → "
        "Videolar yuboring → /done\n"
        "✏️ Tahrirlash: Roʻyxat yoki nom → Maydon tanlang → Yangi qiymat\n"
        "🗑 Oʻchirish: Roʻyxat yoki nom → Tasdiqlang\n"
        "🖼 Bannerlar: Webapp bosh sahifasidagi reklama suratlarini "
        "qoʻshish/oʻchirish\n"
        "🔍 Videolarni tekshirish — barcha qismlarni kanaldagi original xabar "
        "bilan solishtirib, oʻchirilgan/buzilgan video havolalarini topib beradi\n\n"

        "👥 <b>Foydalanuvchilar</b>\n"
        "🔍 ID yoki @username orqali foydalanuvchini topish, maʼlumotlarini koʻrish\n"
        "🚫 Bloklash / blokdan chiqarish\n"
        "👑 Admin qoʻshish — <i>faqat asosiy admin (ADMIN_ID)</i> yangi qoʻshimcha "
        "admin qoʻsha oladi; qoʻshimcha adminlar bu huquqqa ega emas\n"
        "🗑 Admin oʻchirish — qoʻshimcha adminlar roʻyxatidan olib tashlash\n"
        "📜 Admin faoliyati — barcha adminlarning oxirgi harakatlari logi\n\n"

        "📊 <b>Statistika</b>\n"
        "📊 Umumiy statistika — foydalanuvchilar (jami/faol/bloklangan/bugun/"
        "hafta/oy), animelar soni, eng koʻp koʻrilgan 5 ta anime\n"
        "📅 Hisobot — bugungi yangi foydalanuvchilar, tark etganlar, yangi "
        "animelar, umumiy koʻrishlar\n"
        "💰 Daromad — umumiy/bugungi/oxirgi 30 kunlik tushum, reja (1 oy/3 oy/"
        "1 yil) boʻyicha taqsimot, oxirgi 6 oylik oylik tushum\n\n"

        "📨 <b>Muloqot</b>\n"
        "📨 Xabar yuborish — barcha faol foydalanuvchilarga: Oddiy (matn/rasm/"
        "video) yoki Inline (xabar + tugma + link)\n"
        "💬 Izohlar — foydalanuvchilarning anime ostidagi izohlarini koʻrish "
        "va oʻchirish\n"
        "📢 Sponsor baner — webapp'da koʻrsatiladigan reklama banerini sozlash\n\n"

        "⚙️ <b>Sozlamalar</b>\n"
        "📢 Kanallar — majburiy obuna kanallari roʻyxati (qoʻshish/oʻchirish)\n"
        "🔗 Havolalar — webapp Profil boʻlimidagi kanal va qoʻllab-quvvatlash "
        "havolalarini sozlash\n"
        "🔒 Kontent himoyasi — yoqilsa, yuborilgan videolarni forward/saqlab "
        "olish bloklanadi\n"
        "🚫 Soʻz filtri — taqiqlangan soʻzlar roʻyxati (izohlarda avtomatik "
        "filtrlanadi)\n"
        "🔧 Texnik ishlar — yoqilsa, oddiy foydalanuvchilar botga kira olmaydi "
        "(adminlar bundan mustasno)\n"
        "👤 Profil boʻlimi (bepul) — Premium boʻlmagan foydalanuvchilar uchun "
        "webapp Profil boʻlimini vaqtincha yopish (Premium sotishni "
        "ragʻbatlantirish kampaniyasi uchun)\n"
        "📣 Eʼlon kanali — yangi anime/qism qoʻshilganda avtomatik eʼlon "
        "yuboriladigan kanal\n\n"

        "💎 <b>Premium</b>\n"
        "Tariflar va narxlarni (1 oy/3 oy/1 yil) hamda toʻlov kartasini sozlash\n"
        "Foydalanuvchilardan kelgan toʻlov soʻrovlarini (skrinshot bilan) "
        "koʻrib, tasdiqlash yoki rad etish\n"
        "🎁 Istalgan foydalanuvchiga toʻgʻridan-toʻgʻri (toʻlovsiz) Premium "
        "sovgʻa qilish\n\n"

        "ℹ️ Foydalanuvchilar uchun /help buyrugʻi ham mavjud — botdan qanday "
        "foydalanish boʻyicha qisqa qoʻllanma.",
        reply_markup=admin_back(),
        parse_mode="HTML"
    )


def admin_manage_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Admin qo'shish", callback_data="admin_add_admin", style="success")],
        [InlineKeyboardButton(text="📋 Adminlar ro'yxati", callback_data="admin_list_admins", style="primary")],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ])

# ---- ADMIN QO'SHISH ----
# FAQAT asosiy egasi (ADMIN_ID) yangi admin qo'sha oladi — qo'shimcha adminlar
# o'zlari boshqa admin qo'sha olmaydi, aks holda nazoratdan chiqib ketishi mumkin.
@dp.callback_query(F.data == "admin_add_admin")
async def admin_add_admin(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Faqat asosiy admin yangi admin qo'sha oladi", show_alert=True)
        return
    await call.message.edit_text(
        "👑 Yangi admin ID sini yozing:",
        reply_markup=admin_back()
    )
    await state.set_state(AdminManageState.add_id)

@dp.message(AdminManageState.add_id)
async def admin_add_admin_save(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != ADMIN_ID:
        return
    try:
        new_admin_id = int(message.text.strip())
    except Exception:
        await message.answer("❌ Noto'g'ri ID. Faqat raqam yuboring.", reply_markup=admin_back())
        return
    if new_admin_id == ADMIN_ID:
        await message.answer("❌ Bu ID allaqachon asosiy admin.", reply_markup=admin_back())
        return
    u = await asyncio.to_thread(db.get_user, new_admin_id)
    username = f"@{u['username']}" if u and u.get("username") else None
    await asyncio.to_thread(db.add_admin, new_admin_id, username, message.from_user.id)
    _invalidate_extra_admin_cache()
    await log_admin_action(message.from_user, "Admin qo'shdi", f"ID: {new_admin_id}")
    await message.answer(
        f"✅ Yangi admin qo'shildi!\n🆔 ID: <code>{new_admin_id}</code>" + (f"\n👤 {username}" if username else ""),
        reply_markup=admin_manage_kb(),
        parse_mode="HTML"
    )
    try:
        await bot.send_message(new_admin_id, "👑 Sizga botda admin huquqi berildi!")
    except Exception:
        pass

# ---- ADMINLAR RO'YXATI / O'CHIRISH ----
@dp.callback_query(F.data == "admin_list_admins")
async def admin_list_admins(call: CallbackQuery):
    if not await is_admin_user(call.from_user.id):
        return
    admins = await asyncio.to_thread(db.get_admins)
    rows = [[InlineKeyboardButton(
        text=f"🗑 {(a['username'] or a['user_id'])}",
        callback_data=f"admin_remove_admin_{a['user_id']}",
        style="danger"
    )] for a in admins]
    rows.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_cat_users")])
    text = "📋 <b>Qo'shimcha adminlar:</b>\n\n" + (
        "\n".join(f"🆔 <code>{a['user_id']}</code> — {a['username'] or 'username yoq'}" for a in admins)
        if admins else "Hozircha qo'shimcha admin yo'q."
    )
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")

@dp.callback_query(F.data.startswith("admin_remove_admin_"))
async def admin_remove_admin(call: CallbackQuery):
    # Qo'shimcha admin o'chirishni ham faqat asosiy admin qila oladi.
    if call.from_user.id != ADMIN_ID:
        await call.answer("Faqat asosiy admin adminlikdan chiqara oladi", show_alert=True)
        return
    target_id = int(call.data.replace("admin_remove_admin_", ""))
    await asyncio.to_thread(db.remove_admin, target_id)
    _invalidate_extra_admin_cache()
    await log_admin_action(call.from_user, "Adminlikdan chiqardi", f"ID: {target_id}")
    await call.answer("✅ Admin o'chirildi", show_alert=True)
    try:
        await bot.send_message(target_id, "❗️ Sizning admin huquqingiz bekor qilindi.")
    except Exception:
        pass
    admins = await asyncio.to_thread(db.get_admins)
    rows = [[InlineKeyboardButton(
        text=f"🗑 {(a['username'] or a['user_id'])}",
        callback_data=f"admin_remove_admin_{a['user_id']}",
        style="danger"
    )] for a in admins]
    rows.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_cat_users")])
    text = "📋 <b>Qo'shimcha adminlar:</b>\n\n" + (
        "\n".join(f"🆔 <code>{a['user_id']}</code> — {a['username'] or 'username yoq'}" for a in admins)
        if admins else "Hozircha qo'shimcha admin yo'q."
    )
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")

# ===================== DB "UYG'OQ" TUTISH =====================
async def db_keepalive_task():
    """Neon (bepul reja) baza 5 daqiqa foydalanilmasa avtomatik "uxlab qoladi" va
    keyingi so'rov 1-3+ soniya "uyg'onish" uchun kutadi — bu holatda BARCHA
    buyruqlar (shu jumladan admin panel) bir martalik sekinlanib qoladi.
    Shuning uchun har 4 daqiqada yengil so'rov yuborib, bazani doim tirik tutamiz."""
    while True:
        await asyncio.sleep(240)
        try:
            await asyncio.to_thread(db.get_setting, "premium_enabled")
        except Exception as e:
            logger.warning(f"DB keep-alive xatosi: {e}")

async def premium_maintenance_task():
    """Har 6 soatda: muddati o'tgan Premium'larni tozalaydi va tugashiga
    yaqin qolganlarga uzaytirish eslatmasi yuboradi."""
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            expired_count = await asyncio.to_thread(db.expire_premiums)
            if expired_count:
                logger.info(f"Premium muddati tugagan foydalanuvchilar: {expired_count}")
        except Exception as e:
            logger.error(f"Premium tozalash xatosi: {e}")
        try:
            expiring = await asyncio.to_thread(db.get_expiring_premium_users, 2)
            for u in expiring:
                try:
                    await bot.send_message(
                        u["user_id"],
                        "⏳ <b>Premium muddatingiz tugayapti!</b>\n\n"
                        "Imtiyozlaringizni yo'qotmaslik uchun uzaytirib qo'ying 👇",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💎 Premium'ni uzaytirish", callback_data="premium_menu", style="success")],
                        ]),
                        parse_mode="HTML"
                    )
                    await asyncio.to_thread(db.mark_renew_notified, u["user_id"])
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Premium eslatma xatosi: {e}")

async def daily_report_task():
    while True:
        await asyncio.sleep(86400)
        from datetime import datetime
        s = await asyncio.to_thread(db.get_daily_stats)
        today = datetime.now().strftime("%d.%m.%Y")
        try:
            await bot.send_message(
                ADMIN_ID,
                f"📅 <b>Kunlik hisobot — {today}</b>\n\n"
                f"👥 Bugun qoshildi: {s['new_users']}\n"
                f"🚫 Bugun chiqib ketdi: {s['left_users']}\n"
                f"🆕 Bugun qoshilgan anime: {s['new_animes']}\n"
                f"📺 Jami korishlar: {s['total_views']}",
                parse_mode="HTML"
            )
        except Exception:
            pass

# ===================== WEB SERVER =====================
async def health_check(request):
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, "landing.html")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return web.Response(text=content, content_type="text/html", charset="utf-8")
    except Exception:
        return web.Response(text="AniFilm Bot ishlayapti!")

async def serve_favicon(request):
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return web.FileResponse(os.path.join(base_dir, "favicon.ico"))

async def serve_sitemap(request):
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(base_dir, "sitemap.xml"), "r", encoding="utf-8") as f:
            content = f.read()
        return web.Response(text=content, content_type="application/xml", charset="utf-8")
    except Exception:
        return web.Response(status=404)

async def serve_robots(request):
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(base_dir, "robots.txt"), "r", encoding="utf-8") as f:
            content = f.read()
        return web.Response(text=content, content_type="text/plain", charset="utf-8")
    except Exception:
        return web.Response(status=404)

def verify_init_data(init_data: str, max_age_seconds: int = 86400):
    """Telegram WebApp initData imzosini tekshiradi (rasmiy Telegram algoritmi:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app).
    MUHIM: bundan oldin server foydalanuvchi ID'sini mijoz yuborgan oddiy
    `user_id` parametridan olar edi — buni istalgan kishi DevTools'da
    o'zgartirib, boshqa birovning nomidan so'rov yuborishi (hisobni o'chirish,
    Premium tekshiruvidan "o'tish" va h.k.) mumkin edi. Endi faqat BOT_TOKEN
    bilan HMAC-SHA256 imzolangan, Telegram tomonidan yuborilgan initData'gagina
    ishonamiz — uni soxtalashtirib bo'lmaydi.
    Muvaffaqiyatli bo'lsa {"user": {...}, "auth_date": int} qaytaradi, aks holda None."""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except Exception:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except Exception:
        return None
    if max_age_seconds and (time.time() - auth_date) > max_age_seconds:
        # Juda eski (masalan qayta yuborilgan/keshlangan) initData — rad etamiz.
        return None
    user = None
    if "user" in pairs:
        try:
            user = json.loads(pairs["user"])
        except Exception:
            user = None
    if not user or not user.get("id"):
        return None
    return {"user": user, "auth_date": auth_date}

def _verify_init_data_str(init_data: str):
    """Qulaylik uchun: tekshiruvdan oʻtsa Telegram user dict'ini, aks holda None qaytaradi."""
    result = verify_init_data(init_data)
    return result["user"] if result else None

def _webapp_user_id(request):
    """GET soʻrovlar uchun: query'dagi `init_data`ni tekshirib, tasdiqlangan
    user_id'ni qaytaradi. Tekshiruvdan oʻtmasa 0 (mehmon/ruxsatsiz) qaytaradi."""
    user = _verify_init_data_str(request.query.get("init_data", ""))
    try:
        return int(user["id"]) if user else 0
    except Exception:
        return 0

def _verified_post_user(data):
    """POST body'dagi `init_data`ni tekshiradi. Tasdiqlangan Telegram user
    dict'ini yoki None qaytaradi (agar tekshiruvdan oʻtmasa)."""
    if not isinstance(data, dict):
        return None
    return _verify_init_data_str(data.get("init_data", ""))

# ===================== SAYT AUTENTIFIKATSIYASI (email/telefon + parol) =====================
# Bu Telegram Mini App'dagi initData tekshiruvidan butunlay alohida tizim —
# anifilm.uz saytiga Telegramsiz ham roʻyxatdan oʻtish/kirish imkonini beradi.
SITE_AUTH_SECRET = os.environ.get("SITE_AUTH_SECRET", BOT_TOKEN)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?\d{9,15}$")

def _hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"

def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
        return hmac.compare_digest(calc, digest)
    except Exception:
        return False

def _make_site_token(site_user_id: int) -> str:
    expires = int(time.time()) + 60 * 60 * 24 * 30  # 30 kun
    payload = f"{site_user_id}.{expires}"
    sig = hmac.new(SITE_AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def _verify_site_token(token: str):
    """Tokenni tekshiradi, toʻgʻri boʻlsa site_user_id (int) qaytaradi, aks holda None."""
    if not token:
        return None
    try:
        uid_str, expires_str, sig = token.split(".")
        payload = f"{uid_str}.{expires_str}"
        expected = hmac.new(SITE_AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        if int(expires_str) < time.time():
            return None
        return int(uid_str)
    except Exception:
        return None

def _site_user_id(request):
    """So'rovdagi Authorization: Bearer <token> sarlavhasidan tasdiqlangan
    sayt foydalanuvchisi id'sini qaytaradi, aks holda 0."""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    uid = _verify_site_token(token)
    return uid or 0

def _public_site_user(u: dict) -> dict:
    return {
        "id": u["id"],
        "email": u.get("email"),
        "phone": u.get("phone"),
        "display_name": u.get("display_name"),
    }

async def webapp_site_register(request):
    try:
        data = await request.json()
        email = (data.get("email") or "").strip().lower() or None
        phone = re.sub(r"[\s\-\(\)]", "", (data.get("phone") or "").strip()) or None
        password = str(data.get("password") or "")
        display_name = str(data.get("display_name") or "").strip()[:64] or None
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)

    if not email and not phone:
        return web.json_response({"error": "email yoki telefon kiritilishi shart"}, status=400)
    if email and not _EMAIL_RE.match(email):
        return web.json_response({"error": "email notoʻgʻri formatda"}, status=400)
    if phone and not _PHONE_RE.match(phone):
        return web.json_response({"error": "telefon raqami notoʻgʻri formatda"}, status=400)
    if len(password) < 6:
        return web.json_response({"error": "parol kamida 6 ta belgidan iborat boʻlishi kerak"}, status=400)

    if email and await asyncio.to_thread(db.get_site_user_by_login, email):
        return web.json_response({"error": "bunday foydalanuvchi allaqachon mavjud"}, status=409)
    if phone and await asyncio.to_thread(db.get_site_user_by_login, phone):
        return web.json_response({"error": "bunday foydalanuvchi allaqachon mavjud"}, status=409)

    password_hash = _hash_password(password)
    try:
        user = await asyncio.to_thread(db.create_site_user, email, phone, password_hash, display_name)
    except Exception:
        return web.json_response({"error": "bunday foydalanuvchi allaqachon mavjud"}, status=409)
    token = _make_site_token(user["id"])
    return web.json_response({"token": token, "user": _public_site_user(user)})

async def webapp_site_login(request):
    try:
        data = await request.json()
        identifier = str(data.get("identifier") or "").strip()
        password = str(data.get("password") or "")
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    if "@" in identifier:
        identifier = identifier.lower()
    else:
        identifier = re.sub(r"[\s\-\(\)]", "", identifier)
    user = await asyncio.to_thread(db.get_site_user_by_login, identifier)
    if not user or not _verify_password(password, user["password_hash"]):
        return web.json_response({"error": "email/telefon yoki parol notoʻgʻri"}, status=401)
    token = _make_site_token(user["id"])
    return web.json_response({"token": token, "user": _public_site_user(user)})

async def webapp_site_me(request):
    uid = _site_user_id(request)
    if not uid:
        return web.json_response({"error": "ruxsat yoq"}, status=401)
    user = await asyncio.to_thread(db.get_site_user_by_id, uid)
    if not user:
        return web.json_response({"error": "ruxsat yoq"}, status=401)
    return web.json_response({"user": _public_site_user(user)})

async def webapp_site_favorites(request):
    uid = _site_user_id(request)
    if not uid:
        return web.json_response({"error": "ruxsat yoq"}, status=401)
    ids = await asyncio.to_thread(db.get_site_favorite_ids, uid)
    animes = await asyncio.to_thread(db.get_animes_by_ids, ids)
    return web.json_response(animes)

async def webapp_site_toggle_favorite(request):
    uid = _site_user_id(request)
    if not uid:
        return web.json_response({"error": "ruxsat yoq"}, status=401)
    try:
        data = await request.json()
        anime_id = int(data.get("anime_id"))
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    active = await asyncio.to_thread(db.toggle_site_favorite, uid, anime_id)
    return web.json_response({"ok": True, "active": active})

async def webapp_site_history(request):
    uid = _site_user_id(request)
    if not uid:
        return web.json_response({"error": "ruxsat yoq"}, status=401)
    ids = await asyncio.to_thread(db.get_site_history_ids, uid)
    animes = await asyncio.to_thread(db.get_animes_by_ids, ids)
    return web.json_response(animes)

async def webapp_site_record_history(request):
    uid = _site_user_id(request)
    if not uid:
        return web.json_response({"error": "ruxsat yoq"}, status=401)
    try:
        data = await request.json()
        anime_id = int(data.get("anime_id"))
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    await asyncio.to_thread(db.record_site_history, uid, anime_id)
    return web.json_response({"ok": True})

async def webapp_site_clear_history(request):
    uid = _site_user_id(request)
    if not uid:
        return web.json_response({"error": "ruxsat yoq"}, status=401)
    await asyncio.to_thread(db.clear_site_history, uid)
    return web.json_response({"ok": True})


async def webapp_access_status(user_id: int):
    """Webapp uchun kirish holatini tekshiradi: texnik ishlar va majburiy obuna."""
    is_admin = user_id == ADMIN_ID

    maintenance = await asyncio.to_thread(db.get_setting, "maintenance") == "1"
    if maintenance and not is_admin:
        return {"maintenance": True, "subscribed": True, "channels": []}

    if is_admin:
        return {"maintenance": False, "subscribed": True, "channels": []}

    if not user_id:
        # Webapp Telegram foydalanuvchi ID'sini yubormadi — bu "obuna yo'q"dan
        # boshqa holat, frontend buni alohida ko'rsatishi uchun belgilaymiz.
        return {"maintenance": False, "subscribed": False, "channels": [], "invalid_session": True}

    # Botda bloklangan foydalanuvchi webapp'ga ham kira olmasligi kerak.
    # Mavjud "subscribed" tekshiruvidan foydalanamiz — shu orqali bu holat
    # webapp_access_status'ni chaqiruvchi HAMMA endpoint'larda (animes ro'yxati,
    # anime tafsiloti, video yuborish, izohlar va h.k.) avtomatik qo'llanadi.
    u = await asyncio.to_thread(db.get_user, user_id)
    if u and u.get("is_blocked"):
        return {"maintenance": False, "subscribed": False, "channels": [], "blocked": True}

    subscribed = await check_subscription(user_id)
    channels_out = []
    if not subscribed:
        channels = await asyncio.to_thread(db.get_channels)
        for ch in channels:
            channels_out.append({
                "name": ch["channel_name"],
                "url": f"https://t.me/{ch['channel_id'].lstrip('@')}"
            })
    return {
        "maintenance": False,
        "subscribed": subscribed,
        "channels": channels_out,
        "bot_username": BOT_USERNAME or "",
    }

async def webapp_check_access(request):
    user_id = _webapp_user_id(request)
    status = await webapp_access_status(user_id)
    return web.json_response(status)

async def webapp_sponsor(request):
    user_id = _webapp_user_id(request)
    premium = await asyncio.to_thread(db.get_premium_status, user_id)
    if premium["is_premium"]:
        return web.json_response({"enabled": False})
    photo_id = await asyncio.to_thread(db.get_setting, "sponsor_photo_id")
    if not photo_id:
        return web.json_response({"enabled": False})
    title = await asyncio.to_thread(db.get_setting, "sponsor_title") or ""
    url = await asyncio.to_thread(db.get_setting, "sponsor_url") or ""
    return web.json_response({
        "enabled": True,
        "photo_url": f"/api/photo/{photo_id}",
        "title": title,
        "url": url,
    })

async def webapp_profile(request):
    user_id = _webapp_user_id(request)
    u, channel_url, support_url, premium, prices, app_version, profile_disabled = await asyncio.gather(
        asyncio.to_thread(db.get_user, user_id),
        asyncio.to_thread(db.get_setting, "profile_channel_url"),
        asyncio.to_thread(db.get_setting, "profile_support_url"),
        asyncio.to_thread(db.get_premium_status, user_id),
        premium_settings(),
        asyncio.to_thread(db.get_setting, "bot_version"),
        asyncio.to_thread(db.get_setting, "profile_disabled_for_free"),
    )
    is_admin = await is_admin_user(user_id)

    # Kampaniya: bepul (Premium bo'lmagan) foydalanuvchilar uchun Profil
    # bo'limi vaqtincha yopilgan bo'lishi mumkin — Premium/admin har doim kiradi.
    if profile_disabled == "1" and not premium["is_premium"] and not is_admin:
        return web.json_response({
            "disabled": True,
            "is_premium": False,
            "channel_url": channel_url or "",
            "support_url": support_url or "",
            "bot_username": BOT_USERNAME or "",
        })

    return web.json_response({
        "disabled": False,
        "joined_at": u.get("joined_at") if u else None,
        "is_premium": premium["is_premium"],
        "premium_days_left": premium["days_left"],
        "premium_until": premium["until"],
        "premium_plan": PLAN_LABELS.get(premium["plan"], premium["plan"]) if premium["plan"] else None,
        "premium_early_hours": prices["early_hours"],
        "premium_ref_bonus": prices["ref_bonus"],
        "channel_url": channel_url or "",
        "support_url": support_url or "",
        "bot_username": BOT_USERNAME or "",
        "app_version": app_version or "1.0.0",
    })

async def webapp_premium_info(request):
    """Webapp ichidagi Premium sahifasi uchun: tarif narxlari, imtiyozlar
    solishtiruvi va (agar admin yoqqan bo'lsa) chegirma countdown ma'lumoti."""
    user_id = _webapp_user_id(request)
    prices, premium, promo_active, promo_end, promo_note = await asyncio.gather(
        premium_settings(),
        asyncio.to_thread(db.get_premium_status, user_id) if user_id else asyncio.sleep(0, result={"is_premium": False, "days_left": 0, "plan": None}),
        asyncio.to_thread(db.get_setting, "premium_promo_active"),
        asyncio.to_thread(db.get_setting, "premium_promo_end"),
        asyncio.to_thread(db.get_setting, "premium_promo_note"),
    )

    plans = []
    if prices["plan_1m_on"]:
        plans.append({"code": "1m", "label": "1 oy", "days": 30, "price": prices["1m"]})
    if prices["plan_3m_on"]:
        plans.append({"code": "3m", "label": "3 oy", "days": 90, "price": prices["3m"]})
    if prices["plan_1y_on"]:
        plans.append({"code": "1y", "label": "1 yil", "days": 365, "price": prices["1y"]})

    return web.json_response({
        "enabled": prices["enabled"],
        "plans": plans,
        "early_hours": prices["early_hours"],
        "ref_bonus": prices["ref_bonus"],
        "is_premium": premium.get("is_premium", False),
        "days_left": premium.get("days_left", 0),
        "bot_username": BOT_USERNAME or "",
        "promo": {
            "active": (promo_active or "0") == "1",
            "end": promo_end or None,
            "note": promo_note or "",
        },
    })

async def webapp_account_refresh(request):
    """"Hisobni yangilash" — Telegramdan kelgan joriy ism/username bilan
    foydalanuvchi yozuvini sinxronlaydi va yangilangan profilni qaytaradi."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    user = _verified_post_user(data)
    if not user:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    user_id = int(user["id"])
    # Ism/username Telegram tomonidan imzolangan initData'dan olinadi, mijoz
    # yuborgan qiymatlarga endi ishonilmaydi (spoofing'ning oldini oladi).
    username = user.get("username")
    full_name = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or None
    await asyncio.to_thread(db.update_user_info, user_id, username, full_name)
    return web.json_response({"success": True})

async def webapp_account_delete(request):
    """"Hisobni o'chirish" — shaxsiy ma'lumotlarni tozalab, hisobni
    bloklaydi. Qaytarib bo'lmaydi, shuning uchun frontendda ikki marta
    tasdiqlash talab qilinadi."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    user = _verified_post_user(data)
    if not user:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    user_id = int(user["id"])
    await asyncio.to_thread(db.delete_user_data, user_id)
    return web.json_response({"success": True})

async def webapp_animes_list(request):
    status = await webapp_access_status(_webapp_user_id(request))
    if status["maintenance"]:
        return web.json_response({"error": "maintenance"}, status=503)
    if not status["subscribed"]:
        return web.json_response({"error": "not_subscribed", "channels": status["channels"]}, status=403)
    animes = await asyncio.to_thread(db.get_animes_for_webapp)
    return web.json_response(animes)

async def webapp_anime_detail(request):
    user_id = _webapp_user_id(request)
    status = await webapp_access_status(user_id)
    if status["maintenance"]:
        return web.json_response({"error": "maintenance"}, status=503)
    if not status["subscribed"]:
        return web.json_response({"error": "not_subscribed", "channels": status["channels"]}, status=403)
    anime_id = int(request.match_info["anime_id"])
    data = await asyncio.to_thread(db.get_anime_detail_for_webapp, anime_id)
    if not data:
        return web.json_response({"error": "topilmadi"}, status=404)
    episodes = data.get("episodes", [])
    if episodes:
        prices = await premium_settings()
        is_premium = False
        if user_id != ADMIN_ID:
            premium_status = await asyncio.to_thread(db.get_premium_status, user_id)
            is_premium = premium_status["is_premium"]
        for ep in episodes:
            ep["is_locked"] = _episode_locked(ep, user_id, prices, is_premium, anime=data)
    return web.json_response(data)

async def webapp_send_episode(request):
    try:
        data = await request.json()
        episode_id = int(data.get("episode_id"))
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    user = _verified_post_user(data)
    if not user:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    user_id = int(user["id"])

    status = await webapp_access_status(user_id)
    if status["maintenance"]:
        return web.json_response({"error": "maintenance"}, status=503)
    if not status["subscribed"]:
        return web.json_response({"error": "not_subscribed", "channels": status["channels"]}, status=403)

    u = await asyncio.to_thread(db.get_user, user_id)
    if u and u.get("is_blocked"):
        return web.json_response({"error": "bloklangan"}, status=403)

    ep = await asyncio.to_thread(db.get_episode, episode_id)
    if not ep:
        return web.json_response({"error": "topilmadi"}, status=404)
    if await is_episode_locked_for_user(ep, user_id):
        return web.json_response({"error": "premium_only"}, status=403)

    protect = await asyncio.to_thread(db.get_setting, "content_protect") == "1"
    try:
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=STORAGE_CHANNEL,
            message_id=ep["channel_message_id"],
            protect_content=protect
        )
        await asyncio.to_thread(db.log_watch, ep["anime_id"], user_id)
        return web.json_response({"ok": True})
    except Exception as e:
        logger.error(
            f"send_episode xato: user_id={user_id} episode_id={episode_id} "
            f"channel_message_id={ep.get('channel_message_id')} xato={e}"
        )
        return web.json_response({"error": str(e)}, status=500)

# Rasm keshi — bir xil poster/baner qayta-qayta Telegramdan yuklab olinmasligi uchun (tezlik + trafik tejash)
_PHOTO_CACHE = {}
_PHOTO_CACHE_TTL = 6 * 3600  # 6 soat
_PHOTO_CACHE_MAX = 400  # xotirada saqlanadigan maksimal rasm soni
_PHOTO_MAX_DIM = 720  # webapp'da rasm bundan kattaroq ko'rsatilmaydi — Telegramning original
                       # (ko'pincha 1280px+, yuzlab KB) rasmini shu o'lchamgacha kichraytiramiz.
                       # Kartalar sahifasida bir vaqtda 15-20 ta rasm yuklanadi — asl hajmda
                       # bu sekinlik/"qotib qolish" hissining asosiy sababi edi.

def _shrink_image_bytes(data, max_dim=_PHOTO_MAX_DIM, quality=82):
    """Rasmni max_dim'gacha kichraytirib, JPEG'ga siqib qaytaradi. Xato bo'lsa,
    asl baytlarni (None content_type bilan — 'kichraytirilmadi' belgisi) qaytaradi."""
    try:
        img = _PILImage.open(io.BytesIO(data))
        if img.mode in ("RGBA", "LA", "P"):
            bg = _PILImage.new("RGB", img.size, (10, 10, 15))
            bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), _PILImage.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception as e:
        logger.warning(f"[photo] rasm kichraytirilmadi, asl holida yuboriladi: {e}")
        return data, None

async def webapp_photo(request):
    photo_id = request.match_info["photo_id"]
    now = time.time()
    cached = _PHOTO_CACHE.get(photo_id)
    if cached and (now - cached[2]) < _PHOTO_CACHE_TTL:
        data, content_type, _ = cached
        return web.Response(body=data, content_type=content_type, headers={
            "Cache-Control": "public, max-age=86400",
        })
    try:
        file = await bot.get_file(photo_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    raise web.HTTPNotFound()
                data = await resp.read()
                content_type = resp.headers.get("Content-Type", "image/jpeg")
        if _PIL_AVAILABLE:
            resized_data, resized_type = await asyncio.to_thread(_shrink_image_bytes, data)
            if resized_type:
                data, content_type = resized_data, resized_type
        _PHOTO_CACHE[photo_id] = (data, content_type, now)
        if len(_PHOTO_CACHE) > _PHOTO_CACHE_MAX:
            oldest = min(_PHOTO_CACHE, key=lambda k: _PHOTO_CACHE[k][2])
            del _PHOTO_CACHE[oldest]
        return web.Response(body=data, content_type=content_type, headers={
            "Cache-Control": "public, max-age=86400",
        })
    except web.HTTPNotFound:
        raise
    except Exception:
        raise web.HTTPNotFound()

# ===== 🔔 BILDIRISHNOMALAR (webapp bell paneli) =====
_NOTIF_ICON = {"episode": "🎬", "anime": "🆕", "announcement": "📣"}

async def webapp_notifications(request):
    user_id = _webapp_user_id(request)
    rows, unread = await asyncio.gather(
        asyncio.to_thread(db.get_notifications, 30),
        asyncio.to_thread(db.get_unread_notification_count, user_id),
    )
    items = [{
        "id": n["id"],
        "type": n["ntype"],
        "icon": _NOTIF_ICON.get(n["ntype"], "🔔"),
        "title": n["title"],
        "body": n.get("body"),
        "anime_id": n.get("anime_id"),
        "created_at": n.get("created_at"),
    } for n in rows]
    return web.json_response({"items": items, "unread": unread})

async def webapp_notifications_seen(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    user = _verified_post_user(data)
    if not user:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    await asyncio.to_thread(db.mark_notifications_seen, int(user["id"]))
    return web.json_response({"ok": True})

async def webapp_banners(request):
    status = await webapp_access_status(_webapp_user_id(request))
    if status["maintenance"]:
        return web.json_response({"error": "maintenance"}, status=503)
    if not status["subscribed"]:
        return web.json_response({"error": "not_subscribed", "channels": status["channels"]}, status=403)
    banners = await asyncio.to_thread(db.get_banners, True)
    return web.json_response(banners)

async def webapp_categories(request):
    status = await webapp_access_status(_webapp_user_id(request))
    if status["maintenance"]:
        return web.json_response({"error": "maintenance"}, status=503)
    if not status["subscribed"]:
        return web.json_response({"error": "not_subscribed", "channels": status["channels"]}, status=403)
    cats = await asyncio.to_thread(db.get_categories)
    return web.json_response(cats)

# ===== PUBLIC (auth talab qilmaydigan) endpointlar — landing.html uchun =====
# Bular Telegram init_data yoki kanalga obuna tekshiruvisiz ishlaydi, chunki
# faqat reklama/marketing sahifasida (anifilm.uz) ko'rsatiladigan ochiq
# ma'lumotlarni qaytaradi (nomi, poster, yil, tur — yuklab olish havolasiz).
async def webapp_public_animes(request):
    try:
        animes = await asyncio.to_thread(db.get_animes_for_webapp)
    except Exception:
        return web.json_response({"error": "unavailable"}, status=503)
    safe = [{
        "id": a.get("id"),
        "title": a.get("title"),
        "year": a.get("year"),
        "genre": a.get("genre"),
        "category": a.get("category"),
        "description": a.get("description"),
        "photo_id": a.get("photo_id"),
        "media_type": a.get("media_type"),
        "views": a.get("views"),
        "total_episodes": a.get("total_episodes"),
        "episode_count": a.get("episode_count"),
    } for a in animes]
    return web.json_response(safe, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=120"})

async def webapp_public_categories(request):
    try:
        cats = await asyncio.to_thread(db.get_categories)
    except Exception:
        return web.json_response({"error": "unavailable"}, status=503)
    return web.json_response(cats, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"})

async def webapp_get_comments(request):
    viewer_id = _webapp_user_id(request)
    status = await webapp_access_status(viewer_id)
    if status["maintenance"]:
        return web.json_response({"error": "maintenance"}, status=503)
    if not status["subscribed"]:
        return web.json_response({"error": "not_subscribed", "channels": status["channels"]}, status=403)
    anime_id = int(request.match_info["anime_id"])
    comments = await asyncio.to_thread(db.get_comments, anime_id, 50, viewer_id)
    return web.json_response(comments)

async def webapp_add_comment(request):
    try:
        data = await request.json()
        anime_id = int(data.get("anime_id"))
        text = str(data.get("text", "")).strip()
        parent_id = data.get("parent_id")
        parent_id = int(parent_id) if parent_id else None
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    user = _verified_post_user(data)
    if not user:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    user_id = int(user["id"])
    username = str(user.get("username") or "")[:64]

    if not text:
        return web.json_response({"error": "boʻsh izoh"}, status=400)
    if len(text) > 300:
        return web.json_response({"error": "izoh juda uzun (max 300)"}, status=400)

    status = await webapp_access_status(user_id)
    if status["maintenance"]:
        return web.json_response({"error": "maintenance"}, status=503)
    if not status["subscribed"]:
        return web.json_response({"error": "not_subscribed", "channels": status["channels"]}, status=403)

    u = await asyncio.to_thread(db.get_user, user_id)
    if u and u.get("is_blocked"):
        return web.json_response({"error": "bloklangan"}, status=403)

    # spam himoyasi: 20 soniyada 1 tadan koʻp izoh yozib boʻlmaydi
    last_at = await asyncio.to_thread(db.get_last_comment_at, user_id)
    if last_at:
        try:
            from datetime import datetime
            delta = (datetime.now() - datetime.strptime(last_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
            if delta < 20:
                return web.json_response({"error": "juda tez, biroz kuting"}, status=429)
        except Exception:
            pass

    # ---- Izohlar moderatsiyasi: taqiqlangan so'zlar va spam himoyasi ----
    banned_raw = await asyncio.to_thread(db.get_setting, "banned_words") or ""
    banned_words = [w.strip().lower() for w in banned_raw.split(",") if w.strip()]
    lowered = text.lower()
    if any(w in lowered for w in banned_words):
        return web.json_response({"error": "izoh taqiqlangan soʻz(lar) boʻlgani uchun yuborilmadi"}, status=400)
    if re.search(r"https?://|t\.me/|@\w{4,}", lowered):
        return web.json_response({"error": "izohda havola/reklama boʻlishi mumkin emas"}, status=400)

    new_id = await asyncio.to_thread(db.add_comment, anime_id, user_id, username, text, parent_id)
    return web.json_response({"ok": True, "id": new_id})

async def webapp_toggle_like(request):
    try:
        data = await request.json()
        comment_id = int(data.get("comment_id"))
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    user = _verified_post_user(data)
    if not user:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    user_id = int(user["id"])
    status = await webapp_access_status(user_id)
    if not status["subscribed"]:
        return web.json_response({"error": "not_subscribed"}, status=403)
    liked, count = await asyncio.to_thread(db.toggle_comment_like, comment_id, user_id)
    return web.json_response({"ok": True, "liked": liked, "likes": count})

async def webapp_get_favorites(request):
    user_id = _webapp_user_id(request)
    if not user_id:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    ids = await asyncio.to_thread(db.get_favorite_ids, user_id)
    return web.json_response({"ids": ids})

async def webapp_toggle_favorite(request):
    try:
        data = await request.json()
        anime_id = int(data.get("anime_id"))
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    user = _verified_post_user(data)
    if not user:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    user_id = int(user["id"])
    active = await asyncio.to_thread(db.toggle_favorite, user_id, anime_id)
    return web.json_response({"ok": True, "active": active})

async def webapp_get_stats(request):
    user_id = _webapp_user_id(request)
    if not user_id:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    stats = await asyncio.to_thread(db.get_profile_stats, user_id)
    return web.json_response(stats)

async def webapp_get_history(request):
    user_id = _webapp_user_id(request)
    if not user_id:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    # Profildagi "Oxirgi ko'rilganlar" qatori uchun standart 8 ta yetarli, lekin
    # "Ko'rilgan"/"Davom etishda" statistikasi bosilganda to'liq ro'yxat (10
    # tagacha) ko'rsatiladi — shuning uchun ?limit= orqali moslashuvchan qilindi.
    try:
        limit = int(request.query.get("limit", 8))
    except Exception:
        limit = 8
    limit = max(1, min(limit, 20))
    items = await asyncio.to_thread(db.get_recent_watch_details, user_id, limit)
    # `ids` maydoni eski frontend versiyalari bilan moslik uchun saqlab qolinadi.
    return web.json_response({"ids": [it["anime_id"] for it in items], "items": items})

async def webapp_clear_history(request):
    """"Tarixni tozalash" tugmasi bosilganda chaqiriladi — foydalanuvchining
    serverdagi tomosha tarixi/statistikasini butunlay o'chiradi."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    user = _verified_post_user(data)
    if not user:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    user_id = int(user["id"])
    await asyncio.to_thread(db.clear_watch_history, user_id)
    return web.json_response({"ok": True})

async def webapp_get_position(request):
    user_id = _webapp_user_id(request)
    if not user_id:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    try:
        episode_id = int(request.match_info["episode_id"])
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    seconds = await asyncio.to_thread(db.get_watch_position, user_id, episode_id)
    return web.json_response({"position": seconds})

async def webapp_save_position(request):
    try:
        data = await request.json()
        episode_id = int(data.get("episode_id"))
        seconds = int(data.get("position", 0))
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    user = _verified_post_user(data)
    if not user:
        return web.json_response({"error": "ruxsat yoq"}, status=403)
    user_id = int(user["id"])
    await asyncio.to_thread(db.set_watch_position, user_id, episode_id, seconds)
    if seconds >= 5:
        ep = await asyncio.to_thread(db.get_episode, episode_id)
        if ep:
            await asyncio.to_thread(db.record_watch_activity, user_id, ep["anime_id"])
    return web.json_response({"ok": True})

async def debug_path(request):
    # XAVFSIZLIK: bu diagnostika endpointi ilgari hech kim tekshirmasdan
    # ochiq edi — istalgan kishi server fayl tuzilishini ko'rishi mumkin edi.
    # Endi faqat DEBUG_TOKEN muhit o'zgaruvchisi o'rnatilgan bo'lsa va
    # so'rovda to'g'ri ?token=... berilgan bo'lsagina javob qaytaradi.
    if not DEBUG_TOKEN or request.query.get("token") != DEBUG_TOKEN:
        return web.Response(status=404)
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    webapp_dir = os.path.join(base_dir, "webapp")
    index_file = os.path.join(webapp_dir, "index.html")
    files_in_base = os.listdir(base_dir) if os.path.exists(base_dir) else []
    files_in_webapp = os.listdir(webapp_dir) if os.path.exists(webapp_dir) else ["webapp papka topilmadi"]
    return web.json_response({
        "base_dir": base_dir,
        "index_exists": os.path.exists(index_file),
        "files_in_base": files_in_base,
        "files_in_webapp": files_in_webapp,
    })

async def serve_webapp_index(request):
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, "webapp", "index.html")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return web.Response(
            text=content,
            content_type="text/html",
            charset="utf-8",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    except Exception as e:
        return web.Response(text=f"Xato: {e} | path: {filepath}", status=500)

async def start_web_server():
    import os
    import mimetypes
    base_dir = os.path.dirname(os.path.abspath(__file__))
    webapp_dir = os.path.join(base_dir, "webapp")

    webapp_dir_real = os.path.realpath(webapp_dir)

    async def serve_webapp_file(request):
        filename = request.match_info["filename"]
        filepath = os.path.join(webapp_dir, filename)
        # XAVFSIZLIK: filename URL'dan to'g'ridan-to'g'ri kelgani uchun
        # (masalan "..%2f..%2fdatabase.py" kabi) webapp_dir'dan tashqariga
        # chiqishga urinishi mumkin edi. Haqiqiy (realpath) yo'l webapp_dir
        # ichida qolishini tekshiramiz — aks holda rad etamiz.
        real_filepath = os.path.realpath(filepath)
        if real_filepath != webapp_dir_real and not real_filepath.startswith(webapp_dir_real + os.sep):
            return web.Response(text="Ruxsat yoq", status=403)
        if not os.path.isfile(real_filepath):
            return web.Response(text="Topilmadi", status=404)
        filepath = real_filepath
        mime, _ = mimetypes.guess_type(filepath)
        with open(filepath, "rb") as f:
            content = f.read()
        return web.Response(
            body=content,
            content_type=mime or "application/octet-stream",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    STREAM_CHUNK_SIZE = 1024 * 1024  # pyrogramning ichki chunk hajmi
    STREAM_CHUNK_TIMEOUT = 20  # soniya — shuncha vaqt ichida Telegramdan keyingi parcha kelmasa, ulanish "osilib qolgan" deb hisoblanadi

    # Bir nechta foydalanuvchi bir vaqtda video ochib, hammasi birdek muvaffaqiyatsiz
    # bo'lsa (masalan qayta ishga tushgandan keyin), har biri alohida sinxronlash
    # signali ("🔄") yubormasin deb, bu signalni bosim ostida faqat bir marta (cooldown
    # davomida) yuboramiz — aks holda kanalga bir zumda o'nlab xabar tushib, yana
    # keraksiz flood xatariga olib kelishi mumkin edi.
    _sync_signal_lock = asyncio.Lock()
    _last_sync_signal_ts = [0.0]
    _SYNC_SIGNAL_COOLDOWN = 5  # soniya

    # Klient bo'yicha: FLOOD_WAIT tugaguncha qayta client.start() chaqirmaslik uchun.
    # MUHIM: FLOOD_WAIT paytida har bir /stream so'rovi qayta start() chaqiraversa,
    # Telegram buni yana shubhali harakat deb hisoblab, kutish muddatini yanada
    # uzaytirib yuboradi — aynan shu sabab avvalgi 880s -> 876s -> ... ketma-ket
    # o'sib borgan edi. Endi bitta klient flood-wait holatida bo'lsa, muddat
    # tugaguniga qadar boshqa urinishlar shu yerda to'xtatiladi.
    _pyro_flood_until = {}

    async def _ensure_pyro_ready(client):
        """Pyrogram klienti ishga tushirilmagan yoki uzilib qolgan bo'lsa, uni qayta ishga
        tushirishga urinadi. Bu ilgari uchragan xatoni bartaraf qiladi: agar dastur ishga
        tushishida (masalan ikkita nusxa bir vaqtda ishlab ketganda) Pyrogram vaqtincha
        ulanolmay qolsa, server baribir ishga tushib ketardi va /stream so'rovlari
        abadiy 'Client has not been started yet' xatosi bilan tugardi — hech qachon
        o'zi tuzalmasdi."""
        if client is None:
            return False
        if getattr(client, "is_connected", False):
            return True
        now = time.time()
        wait_until = _pyro_flood_until.get(id(client), 0)
        if now < wait_until:
            return False
        try:
            await client.start()
            _pyro_flood_until.pop(id(client), None)
            # Muvaffaqiyatli ulanishdan keyin sessiyani saqlab qo'yamiz (agar hali
            # saqlanmagan yoki o'zgargan bo'lsa) — keyingi qayta ishga tushishda
            # qaytadan auth.ImportBotAuthorization chaqirilmasligi uchun.
            try:
                idx = _stream_clients.index(client) + 1
                sess = await client.export_session_string()
                await asyncio.to_thread(db.set_setting, f"pyro_session_{idx}", sess)
            except Exception:
                pass
            return True
        except ConnectionError:
            # Pyrogram "Client is already started" holatini ConnectionError qilib ko'taradi
            return True
        except Exception as e:
            msg = str(e)
            m = re.search(r"wait of (\d+) seconds", msg)
            if m:
                wait_s = int(m.group(1))
                _pyro_flood_until[id(client)] = now + wait_s
                logger.error(
                    f"[stream] FLOOD_WAIT: {wait_s}s kutish talab qilinadi. "
                    f"Shu muddat tugagunicha bu klient uchun qayta urinilmaydi."
                )
            else:
                logger.error(f"[stream] klientni ishga tushirib bo'lmadi: {e}")
            return False

    async def webapp_stream(request):
        """Videoni Telegramdan (Pyrogram/MTProto orqali) to'g'ridan-to'g'ri brauzerga oqim qiladi.
        HTTP Range so'rovlarini qo'llab-quvvatlaydi — shu tufayli pleerda oldinga/orqaga surish (seek) ishlaydi."""
        if not STREAM_ENABLED or not pyro:
            return web.Response(text="Onlayn ko'rish hozircha sozlanmagan", status=503)

        user_id = _webapp_user_id(request)
        status = await webapp_access_status(user_id)
        if status["maintenance"] or not status["subscribed"]:
            return web.Response(text="Ruxsat yo'q", status=403)

        try:
            episode_id = int(request.match_info["episode_id"])
        except Exception:
            return web.Response(text="Notoʻgʻri soʻrov", status=400)

        ep = await asyncio.to_thread(db.get_episode, episode_id)
        if not ep:
            return web.Response(text="Epizod topilmadi", status=404)
        if await is_episode_locked_for_user(ep, user_id):
            return web.Response(text="Bu qism hozircha faqat Premium foydalanuvchilar uchun ochiq", status=403)

        if not await _ensure_pyro_ready(pyro):
            return web.Response(text="Onlayn ko'rish vaqtincha ishlamayapti, birozdan so'ng qayta urinib ko'ring", status=503)

        try:
            msg = await pyro.get_messages(STORAGE_CHANNEL, ep["channel_message_id"])
        except Exception as e:
            logger.warning(f"[stream] birinchi urinish muvaffaqiyatsiz ({e}), sinxronlash signali orqali qayta urinilmoqda...")
            try:
                async with _sync_signal_lock:
                    now = time.time()
                    if now - _last_sync_signal_ts[0] > _SYNC_SIGNAL_COOLDOWN:
                        sync_msg = await bot.send_message(STORAGE_CHANNEL, "🔄")
                        _last_sync_signal_ts[0] = now
                        await asyncio.sleep(2)
                        try:
                            await sync_msg.delete()
                        except Exception:
                            pass
                    else:
                        # Yaqinda boshqa so'rov allaqachon sinxronlash signali yuborgan —
                        # takror yubormaymiz, faqat qisqa kutib qayta urinamiz.
                        await asyncio.sleep(0.5)
                msg = await pyro.get_messages(STORAGE_CHANNEL, ep["channel_message_id"])
            except Exception as e2:
                logger.error(f"[stream] get_messages xato: {e2}")
                return web.Response(text="Video topilmadi", status=404)

        media = msg.video or msg.document or msg.animation
        if not media:
            return web.Response(text="Bu xabarda video yoʻq", status=404)

        file_size = media.file_size
        mime_type = getattr(media, "mime_type", None) or "video/mp4"

        start, end = 0, file_size - 1
        status_code = 200
        range_header = request.headers.get("Range")
        if range_header:
            try:
                rng = range_header.replace("bytes=", "").split("-")
                if rng[0]:
                    start = int(rng[0])
                if len(rng) > 1 and rng[1]:
                    end = int(rng[1])
                status_code = 206
            except Exception:
                start, end = 0, file_size - 1
        end = min(end, file_size - 1)
        if start > end:
            start = 0
        length = end - start + 1

        resp = web.StreamResponse(status=status_code, headers={
            "Content-Type": mime_type,
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
        })
        if status_code == 206:
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        await resp.prepare(request)

        sent = 0
        max_retries = 6
        retries = 0
        give_up = False
        try:
            while sent < length:
                cur_offset_bytes = start + sent
                offset_chunk = cur_offset_bytes // STREAM_CHUNK_SIZE
                cut = cur_offset_bytes % STREAM_CHUNK_SIZE
                try:
                    first_piece = True
                    stream_client = _next_stream_client()
                    if not await _ensure_pyro_ready(stream_client):
                        raise RuntimeError("stream worker ishga tushmagan")
                    media_iter = stream_client.stream_media(msg, offset=offset_chunk)
                    # MUHIM (qotib qolish muammosi shu yerda edi): oddiy `async for`
                    # ishlatilganda, Telegram tomoni javob bermay qolsa (MTProto
                    # ulanishi vaqtincha "osilib qolsa") bu yerda HECH QANDAY
                    # xatolik chiqmasdan abadiy kutib turilar edi — shuning uchun
                    # pastdagi `except`/qayta urinish kodi umuman ishga tushmas,
                    # foydalanuvchi esa internet yaxshi bo'lsa ham brauzerda
                    # "Yuklanmoqda..." abadiy aylanib qolganini ko'rar edi. Endi
                    # har bir keyingi parcha timeout bilan kutiladi: agar
                    # STREAM_CHUNK_TIMEOUT ichida kelmasa, bu oddiy uzilish kabi
                    # quyidagi `except Exception` bloki tomonidan ushlanadi va
                    # avtomatik ravishda boshqa klient bilan qayta ulanadi.
                    while True:
                        try:
                            chunk = await asyncio.wait_for(media_iter.__anext__(), timeout=STREAM_CHUNK_TIMEOUT)
                        except StopAsyncIteration:
                            break
                        if first_piece and cut:
                            chunk = chunk[cut:]
                            first_piece = False
                        remaining = length - sent
                        if len(chunk) > remaining:
                            chunk = chunk[:remaining]
                        if not chunk:
                            break
                        await resp.write(chunk)
                        sent += len(chunk)
                        retries = 0  # muvaffaqiyatli yozilgach hisoblagichni tiklaymiz
                        if sent >= length:
                            break
                    break  # toʻliq tugadi (yoki uzilishsiz yakunlandi)
                except (ConnectionResetError, asyncio.CancelledError):
                    raise  # bular foydalanuvchi tomonidan yopilgani uchun qayta urinishning hojati yoʻq
                except Exception as e:
                    retries += 1
                    if sent >= length:
                        break
                    if retries > max_retries:
                        logger.error(f"[stream] uzatishda uzilish, qayta urinishlar tugadi: {e}")
                        give_up = True
                        break
                    backoff = min(0.5 * retries, 3)
                    logger.warning(f"[stream] uzilish ({e}), {cur_offset_bytes} baytdan qayta ulanilmoqda ({retries}/{max_retries})...")
                    await asyncio.sleep(backoff)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.error(f"[stream] kutilmagan xato: {e}")
        if give_up:
            # Javobni Content-Length'da va'da qilingandan kamroq bayt bilan "tinch"
            # tugatish o'rniga ulanishni ataylab keskin uzamiz. Aks holda ba'zi
            # brauzer/WebView'lar (jumladan Telegram ichidagi) buni xato deb
            # tanimay, pleerni abadiy "yuklanmoqda" holatida qoldirib qo'yishi
            # mumkin edi. Ulanish keskin uzilganda video elementining `error`
            # hodisasi ishga tushadi va frontend buni avtomatik qayta urinish
            # bilan to'g'ri qayta tiklaydi (openPlayer/pv error handler'iga qarang).
            try:
                request.transport.close()
            except Exception:
                pass
        return resp

    @web.middleware
    async def compress_middleware(request, handler):
        """API javoblarini (anime ro'yxati va h.k. — ba'zida yuzlab KB JSON)
        gzip bilan siqib yuboradi. Rasm/video kabi allaqachon siqilgan
        formatlarga tegmaydi — ularni qayta siqish foyda bermaydi, faqat
        protsessor vaqtini yeydi."""
        resp = await handler(request)
        try:
            if (
                isinstance(resp, web.Response) and resp.body
                and len(resp.body) > 1024
                and "gzip" in request.headers.get("Accept-Encoding", "")
                and (resp.content_type or "").startswith(("application/json", "text/"))
            ):
                resp.enable_compression()
        except Exception:
            pass
        return resp

    app = web.Application(middlewares=[compress_middleware])
    app.router.add_get("/", health_check)
    app.router.add_get("/favicon.ico", serve_favicon)
    app.router.add_get("/sitemap.xml", serve_sitemap)
    app.router.add_get("/robots.txt", serve_robots)
    app.router.add_get("/debug", debug_path)
    app.router.add_get("/webapp", serve_webapp_index)
    app.router.add_get("/webapp/", serve_webapp_index)
    app.router.add_get("/webapp/{filename}", serve_webapp_file)
    app.router.add_get("/api/check_access", webapp_check_access)
    app.router.add_get("/api/profile", webapp_profile)
    app.router.add_get("/api/premium-info", webapp_premium_info)
    app.router.add_post("/api/account/refresh", webapp_account_refresh)
    app.router.add_post("/api/account/delete", webapp_account_delete)
    app.router.add_get("/api/animes", webapp_animes_list)
    app.router.add_get("/api/animes/{anime_id}", webapp_anime_detail)
    app.router.add_get("/api/photo/{photo_id}", webapp_photo)
    app.router.add_get("/api/sponsor", webapp_sponsor)
    app.router.add_post("/api/send_episode", webapp_send_episode)
    app.router.add_get("/api/banners", webapp_banners)
    app.router.add_get("/api/categories", webapp_categories)
    app.router.add_get("/api/notifications", webapp_notifications)
    app.router.add_post("/api/notifications/seen", webapp_notifications_seen)
    app.router.add_get("/api/public/animes", webapp_public_animes)
    app.router.add_get("/api/public/categories", webapp_public_categories)
    app.router.add_post("/api/site/register", webapp_site_register)
    app.router.add_post("/api/site/login", webapp_site_login)
    app.router.add_get("/api/site/me", webapp_site_me)
    app.router.add_get("/api/site/favorites", webapp_site_favorites)
    app.router.add_post("/api/site/favorite", webapp_site_toggle_favorite)
    app.router.add_get("/api/site/history", webapp_site_history)
    app.router.add_post("/api/site/history", webapp_site_record_history)
    app.router.add_post("/api/site/history/clear", webapp_site_clear_history)
    app.router.add_get("/api/comments/{anime_id}", webapp_get_comments)
    app.router.add_post("/api/comments", webapp_add_comment)
    app.router.add_post("/api/comments/like", webapp_toggle_like)
    app.router.add_get("/api/favorites", webapp_get_favorites)
    app.router.add_post("/api/favorite", webapp_toggle_favorite)
    app.router.add_get("/api/stats", webapp_get_stats)
    app.router.add_get("/api/history", webapp_get_history)
    app.router.add_post("/api/history/clear", webapp_clear_history)
    app.router.add_get("/api/position/{episode_id}", webapp_get_position)
    app.router.add_post("/api/position", webapp_save_position)
    app.router.add_get("/stream/{episode_id}", webapp_stream)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server ishga tushdi! (port: {port})")

# ===================== WEBAPP HANDLER =====================
@dp.message(F.web_app_data)
async def handle_webapp_data(message: Message):
    try:
        data = json.loads(message.web_app_data.data)
    except Exception:
        return

    if data.get("action") == "open_episode":
        u = await asyncio.to_thread(db.get_user, message.from_user.id)
        if u and u.get("is_blocked"):
            await message.answer("🚫 Siz bloklandingiz.")
            return

        episode_id = data.get("episode_id")
        ep = await asyncio.to_thread(db.get_episode, episode_id)
        if not ep:
            await message.answer("❌ Epizod topilmadi.")
            return
        if await is_episode_locked_for_user(ep, message.from_user.id):
            text, kb = await locked_episode_message(ep)
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
            return

        protect = await asyncio.to_thread(db.get_setting, "content_protect") == "1"
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=STORAGE_CHANNEL,
            message_id=ep["channel_message_id"],
            protect_content=protect
        )

# ===================== ISHGA TUSHIRISH =====================
async def main():
    global BOT_USERNAME
    await asyncio.to_thread(db.init_db)
    logger.info("Bot ishga tushmoqda...")
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
    except Exception as e:
        logger.error(f"Bot username olinmadi: {e}")
    try:
        from aiogram.types import BotCommand
        await bot.set_my_commands([
            BotCommand(command="start", description="🏠 Bosh menu"),
            BotCommand(command="help", description="🆘 Yordam — qanday foydalanish kerak"),
        ])
    except Exception as e:
        logger.warning(f"Bot buyruqlari (/-menyu) ornatilmadi: {e}")
    if STREAM_ENABLED:
        async def _warm_peer_cache(client, label):
            # Pyrogram (MTProto) botning a'zo bo'lgan kanalni "tanishi" uchun kamida bitta
            # yangi hodisani (update) shu sessiya orqali ko'rishi kerak — aks holda ID orqali
            # xabar olishga uringanda "Peer id invalid" xato beradi. Har bir klientning
            # peer keshi alohida bo'lgani uchun bu tekshiruv HAR BIR ishchi uchun bajariladi.
            try:
                await client.get_chat(STORAGE_CHANNEL)
                logger.info(f"Pyrogram [{label}]: kanal peer keshi allaqachon mavjud.")
            except Exception:
                logger.info(f"Pyrogram [{label}]: kanal peer keshi boʻsh, sinxronlash signali yuborilmoqda...")
                try:
                    sync_msg = await bot.send_message(STORAGE_CHANNEL, "🔄")
                    await asyncio.sleep(2)
                    try:
                        await sync_msg.delete()
                    except Exception:
                        pass
                    await client.get_chat(STORAGE_CHANNEL)
                    logger.info(f"Pyrogram [{label}]: kanal peer keshi muvaffaqiyatli toʻldirildi.")
                except Exception as e:
                    logger.warning(f"Pyrogram [{label}]: sinxronlash signali yuborilmadi/xato: {e}")

        try:
            for _idx, _client in enumerate(_stream_clients, start=1):
                await _client.start()
                try:
                    sess = await _client.export_session_string()
                    await asyncio.to_thread(db.set_setting, f"pyro_session_{_idx}", sess)
                except Exception as e:
                    logger.warning(f"Pyrogram [worker-{_idx}] sessiyasini saqlab bo'lmadi: {e}")
                await _warm_peer_cache(_client, f"worker-{_idx}")
            logger.info(f"Pyrogram (onlayn striming) ishga tushdi! ({len(_stream_clients)} ta ishchi ulanish)")
        except Exception as e:
            logger.error(f"Pyrogram ishga tushmadi: {e}")
    else:
        logger.warning("API_ID/API_HASH topilmadi — onlayn striming o'chirilgan.")

    await start_web_server()
    asyncio.create_task(daily_report_task())
    asyncio.create_task(premium_maintenance_task())
    asyncio.create_task(db_keepalive_task())
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "my_chat_member", "web_app_data"]
        )
    finally:
        if STREAM_ENABLED:
            for _client in _stream_clients:
                try:
                    await _client.stop()
                except Exception:
                    pass

if __name__ == "__main__":
    # asyncio.run() emas — importda yaratilgan _MAIN_LOOP'ning aynan oʻzida ishga tushiramiz,
    # aks holda Pyrogram "attached to a different loop" xatosini beradi.
    try:
        _MAIN_LOOP.run_until_complete(main())
    finally:
        _MAIN_LOOP.close()
