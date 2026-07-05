import asyncio
import logging
import math
import re
import time
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatMemberUpdated, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    WebAppInfo
)
import json
from aiogram.filters import CommandStart, Command, ChatMemberUpdatedFilter, KICKED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramForbiddenError
from aiohttp import web
import aiohttp

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
ADMIN_ID = int(os.environ.get("ADMIN_ID", "5383321037"))
STORAGE_CHANNEL = int(os.environ.get("STORAGE_CHANNEL", "-1002195410889"))
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://anime-bot-fd8r.onrender.com/webapp")

# Onlayn video striming uchun (my.telegram.org dan olinadi)
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
STREAM_ENABLED = bool(API_ID and API_HASH)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Pyrogram klienti — faqat katta video fayllarni brauzerga oqim (stream) qilish uchun.
# aiogram bilan bir xil bot tokenidan foydalanadi, foydalanuvchi login qilishi shart emas.
pyro = PyroClient(
    "stream_bot",
    api_id=int(API_ID) if API_ID else None,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
) if STREAM_ENABLED else None


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
    videos = State()

class AddEpisode(StatesGroup):
    choose_method = State()
    choose_anime = State()
    videos = State()

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

class SearchState(StatesGroup):
    query = State()

class BlockState(StatesGroup):
    user_id = State()

class UnblockState(StatesGroup):
    user_id = State()

class FindUserState(StatesGroup):
    query = State()

class VersionState(StatesGroup):
    version = State()
    changes = State()

# ===================== YORDAMCHI =====================
async def check_subscription(user_id):
    premium = await asyncio.to_thread(db.get_premium_status, user_id)
    if premium["is_premium"]:
        return True
    channels = await asyncio.to_thread(db.get_channels)
    if not channels:
        return True
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["channel_id"], user_id)
            if member.status in ["left", "kicked", "banned"]:
                return False
        except Exception:
            pass
    return True

async def sub_keyboard():
    channels = await asyncio.to_thread(db.get_channels)
    buttons = []
    for ch in channels:
        buttons.append([InlineKeyboardButton(
            text=f"📢 {ch['channel_name']}",
            url=f"https://t.me/{ch['channel_id'].lstrip('@')}"
        )])
    buttons.append([InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="check_sub", style="success")])
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
        text = "📢 Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:"
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
PLAN_DAYS = {"1m": 30, "3m": 90, "1y": 365}
BOT_USERNAME = None  # main() ichida to'ldiriladi

def fmt_som(n):
    return f"{n:,}".replace(",", " ") + " so'm"

async def premium_settings():
    p1 = await asyncio.to_thread(db.get_setting, "premium_price_1m") or "15000"
    p3 = await asyncio.to_thread(db.get_setting, "premium_price_3m") or "40000"
    p12 = await asyncio.to_thread(db.get_setting, "premium_price_1y") or "120000"
    card = await asyncio.to_thread(db.get_setting, "premium_card_number") or ""
    holder = await asyncio.to_thread(db.get_setting, "premium_card_holder") or ""
    early = await asyncio.to_thread(db.get_setting, "premium_early_hours") or "48"
    ref_bonus = await asyncio.to_thread(db.get_setting, "premium_referral_bonus_days") or "3"
    enabled = (await asyncio.to_thread(db.get_setting, "premium_enabled") or "1") == "1"
    plan_1m_on = (await asyncio.to_thread(db.get_setting, "premium_plan_1m_enabled") or "1") == "1"
    plan_3m_on = (await asyncio.to_thread(db.get_setting, "premium_plan_3m_enabled") or "1") == "1"
    plan_1y_on = (await asyncio.to_thread(db.get_setting, "premium_plan_1y_enabled") or "1") == "1"
    return {
        "1m": int(p1), "3m": int(p3), "1y": int(p12),
        "card": card, "holder": holder,
        "early_hours": int(early), "ref_bonus": int(ref_bonus),
        "enabled": enabled,
        "plan_1m_on": plan_1m_on, "plan_3m_on": plan_3m_on, "plan_1y_on": plan_1y_on,
    }

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
    if status["is_premium"]:
        text = (
            f"👑 <b>Siz Premium foydalanuvchisiz!</b>\n\n"
            f"⏳ Amal qilish muddati: <b>{status['days_left']} kun</b> qoldi\n"
            f"📦 Joriy tarif: {PLAN_LABELS.get(status['plan'], status['plan'] or '—')}\n\n"
            f"Muddatni uzaytirish uchun tarif tanlang:"
        )
    else:
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
        f"💳 <b>{PLAN_LABELS[plan]} — {fmt_som(amount)}</b>\n\n"
        f"Quyidagi kartaga to'lovni amalga oshiring:\n\n"
        f"{card_line}{holder_line}\n\n"
        f"💰 Summa: <b>{fmt_som(amount)}</b>\n\n"
        f"To'lovni amalga oshirgach, chek skrinshotini shu yerga rasm qilib yuboring 📸",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="premium_menu")],
        ]),
        parse_mode="HTML"
    )

@dp.message(PremiumState.waiting_screenshot, F.photo)
async def premium_screenshot_received(message: Message, state: FSMContext):
    data = await state.get_data()
    plan = data.get("plan")
    amount = data.get("amount")
    if not plan:
        await state.clear()
        return
    payment_id = await asyncio.to_thread(
        db.create_payment_request, message.from_user.id, plan, amount, message.photo[-1].file_id
    )
    await state.clear()
    await message.answer("✅ Chek qabul qilindi! Admin tomonidan tekshirilib, tez orada tasdiqlanadi.")
    u = message.from_user
    uname = f"@{u.username}" if u.username else u.full_name
    try:
        await bot.send_photo(
            ADMIN_ID,
            message.photo[-1].file_id,
            caption=(
                f"💎 <b>Yangi Premium to'lovi</b>\n\n"
                f"👤 {uname} (ID: <code>{u.id}</code>)\n"
                f"📦 Tarif: {PLAN_LABELS.get(plan, plan)}\n"
                f"💰 Summa: {fmt_som(amount)}"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"pay_ok_{payment_id}", style="success"),
                    InlineKeyboardButton(text="❌ Rad etish", callback_data=f"pay_no_{payment_id}", style="danger"),
                ]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Admin'ga to'lov xabari yuborilmadi: {e}")

@dp.message(PremiumState.waiting_screenshot)
async def premium_screenshot_wrong(message: Message):
    await message.answer("📸 Iltimos, chek skrinshotini rasm sifatida yuboring.")

@dp.callback_query(F.data.startswith("pay_ok_"))
async def premium_approve(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    payment_id = int(call.data.split("_")[-1])
    payment = await asyncio.to_thread(db.get_payment_request, payment_id)
    if not payment or payment["status"] != "pending":
        await call.answer("Bu so'rov allaqachon ko'rib chiqilgan", show_alert=True)
        return
    days = PLAN_DAYS.get(payment["plan"], 30)
    new_until = await asyncio.to_thread(db.extend_premium, payment["user_id"], days, payment["plan"])
    await asyncio.to_thread(db.set_payment_status, payment_id, "approved")
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
    if call.from_user.id != ADMIN_ID:
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

@dp.callback_query(F.data == "premium_referral")
async def premium_referral(call: CallbackQuery):
    await call.answer()
    prices = await premium_settings()
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{call.from_user.id}"
    await call.message.edit_text(
        f"🎁 <b>Do'stlaringizni taklif qiling!</b>\n\n"
        f"Har bir do'stingiz sizning havolangiz orqali botga birinchi marta kirsa, "
        f"Premium muddatingizga <b>+{prices['ref_bonus']} kun</b> qo'shiladi.\n\n"
        f"🔗 Sizning shaxsiy havolangiz:\n<code>{link}</code>",
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
    return (
        f"💎 <b>Premium sozlamalari</b>\n\n"
        f"⚙️ Tizim holati: <b>{sys_state}</b>\n\n"
        f"1 oy: {fmt_som(p['1m'])} — {p1_state}\n"
        f"3 oy: {fmt_som(p['3m'])} — {p3_state}\n"
        f"1 yil: {fmt_som(p['1y'])} — {p12_state}\n\n"
        f"💳 Karta: <code>{card}</code>\n"
        f"👤 Karta egasi: {holder}\n\n"
        f"⏱ Oldinroq kirish: {p['early_hours']} soat\n"
        f"🎁 Referal bonusi: {p['ref_bonus']} kun"
    )

def _premium_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Premium tizimini yoqish/o'chirish", callback_data="padm_toggle_enabled", style="danger")],
        [
            InlineKeyboardButton(text="1 oy yoq/o'ch", callback_data="padm_toggle_1m"),
            InlineKeyboardButton(text="3 oy yoq/o'ch", callback_data="padm_toggle_3m"),
            InlineKeyboardButton(text="1 yil yoq/o'ch", callback_data="padm_toggle_1y"),
        ],
        [
            InlineKeyboardButton(text="✏️ 1 oy narxi", callback_data="padm_price_1m"),
            InlineKeyboardButton(text="✏️ 3 oy narxi", callback_data="padm_price_3m"),
        ],
        [
            InlineKeyboardButton(text="✏️ 1 yil narxi", callback_data="padm_price_1y"),
            InlineKeyboardButton(text="💳 Karta", callback_data="padm_card"),
        ],
        [
            InlineKeyboardButton(text="⏱ Oldinroq kirish", callback_data="padm_early"),
            InlineKeyboardButton(text="🎁 Referal bonusi", callback_data="padm_ref"),
        ],
        [InlineKeyboardButton(text="🔓 Eski qismlar qulfini ochish", callback_data="padm_unlock_old", style="danger")],
        [InlineKeyboardButton(text="📋 To'lov so'rovlari", callback_data="padm_pending")],
        [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")],
    ])

_PADM_TOGGLE_MAP = {
    "padm_toggle_enabled": "premium_enabled",
    "padm_toggle_1m": "premium_plan_1m_enabled",
    "padm_toggle_3m": "premium_plan_3m_enabled",
    "padm_toggle_1y": "premium_plan_1y_enabled",
}

@dp.callback_query(F.data.in_(list(_PADM_TOGGLE_MAP.keys())))
async def padm_toggle(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    key = _PADM_TOGGLE_MAP[call.data]
    current = await asyncio.to_thread(db.get_setting, key)
    current_on = (current or "1") == "1"
    new_val = "0" if current_on else "1"
    await asyncio.to_thread(db.set_setting, key, new_val)
    await call.answer("🟢 Yoqildi" if new_val == "1" else "🔴 O'chirildi")
    await call.message.edit_text(await _premium_admin_text(), reply_markup=_premium_admin_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "padm_unlock_old")
async def padm_unlock_old(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer("Bajarilmoqda...")
    count = await asyncio.to_thread(db.unlock_all_old_episodes)
    await call.message.edit_text(
        f"✅ {count} ta qism qulfdan chiqarildi. Yangi qo'shiladigan qismlar odatdagidek "
        f"'oldinroq kirish' muddatiga tushaveradi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Premium sozlamalari", callback_data="admin_premium")],
        ])
    )

@dp.callback_query(F.data == "admin_premium")
async def admin_premium(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await call.message.edit_text(await _premium_admin_text(), reply_markup=_premium_admin_kb(), parse_mode="HTML")

_PADM_FIELD_MAP = {
    "padm_price_1m": ("premium_price_1m", PremiumAdminState.price_1m, "1 oylik narxni faqat raqam bilan yuboring (masalan: 15000):"),
    "padm_price_3m": ("premium_price_3m", PremiumAdminState.price_3m, "3 oylik narxni faqat raqam bilan yuboring (masalan: 40000):"),
    "padm_price_1y": ("premium_price_1y", PremiumAdminState.price_1y, "1 yillik narxni faqat raqam bilan yuboring (masalan: 120000):"),
    "padm_early": ("premium_early_hours", PremiumAdminState.early_hours, "Oldinroq kirish necha soat bo'lsin? (masalan: 48):"),
    "padm_ref": ("premium_referral_bonus_days", PremiumAdminState.referral_bonus, "Referal bonusi necha kun bo'lsin? (masalan: 3):"),
}

@dp.callback_query(F.data.in_(list(_PADM_FIELD_MAP.keys())))
async def padm_field_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key, st, prompt = _PADM_FIELD_MAP[call.data]
    await state.set_state(st)
    await state.update_data(setting_key=key)
    await call.message.edit_text(prompt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_premium")],
    ]))

@dp.message(PremiumAdminState.price_1m)
@dp.message(PremiumAdminState.price_3m)
@dp.message(PremiumAdminState.price_1y)
@dp.message(PremiumAdminState.early_hours)
@dp.message(PremiumAdminState.referral_bonus)
async def padm_field_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    key = data.get("setting_key")
    value = (message.text or "").strip()
    if not value.isdigit():
        await message.answer("❌ Faqat raqam yuboring. Qaytadan urinib ko'ring.")
        return
    await asyncio.to_thread(db.set_setting, key, value)
    await state.clear()
    await message.answer("✅ Saqlandi!")
    await message.answer(await _premium_admin_text(), reply_markup=_premium_admin_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "padm_card")
async def padm_card_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(PremiumAdminState.card)
    await call.message.edit_text(
        "💳 Karta raqami va (ixtiyoriy) egasining ismini yuboring.\n\n"
        "Format: <code>8600 1234 5678 9012 - Ism Familiya</code>\n"
        "(faqat karta raqamini ham yuborsangiz bo'ladi)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_premium")],
        ]),
        parse_mode="HTML"
    )

@dp.message(PremiumAdminState.card)
async def padm_card_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = (message.text or "").strip()
    if "-" in text:
        card, holder = text.split("-", 1)
        card, holder = card.strip(), holder.strip()
    else:
        card, holder = text, ""
    await asyncio.to_thread(db.set_setting, "premium_card_number", card)
    await asyncio.to_thread(db.set_setting, "premium_card_holder", holder)
    await state.clear()
    await message.answer("✅ Saqlandi!")
    await message.answer(await _premium_admin_text(), reply_markup=_premium_admin_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "padm_pending")
async def padm_pending(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
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
            InlineKeyboardButton(text="➕ Anime qo'shish", callback_data="admin_add"),
            InlineKeyboardButton(text="📋 Ro'yxat", callback_data="admin_list_0"),
        ],
        [
            InlineKeyboardButton(text="➕ Davom qo'shish", callback_data="admin_add_episode"),
            InlineKeyboardButton(text="✏️ Tahrirlash", callback_data="admin_edit"),
        ],
        [
            InlineKeyboardButton(text="🗑 Anime o'chirish", callback_data="admin_delete"),
            InlineKeyboardButton(text="🎬 Qismlar", callback_data="admin_episodes"),
        ],
        [
            InlineKeyboardButton(text="📊 Statistika", callback_data="admin_stats"),
            InlineKeyboardButton(text="📨 Xabar", callback_data="admin_broadcast"),
        ],
        [
            InlineKeyboardButton(text="📢 Kanallar", callback_data="admin_channels"),
            InlineKeyboardButton(text="🚫 Bloklash", callback_data="admin_block"),
        ],
        [
            InlineKeyboardButton(text="🔧 Texnik ishlar", callback_data="admin_maintenance"),
            InlineKeyboardButton(text="🔒 Kontent", callback_data="admin_content"),
        ],
        [
            InlineKeyboardButton(text="🔍 Foydalanuvchi", callback_data="admin_find_user"),
            InlineKeyboardButton(text="📅 Hisobot", callback_data="admin_report"),
        ],
        [
            InlineKeyboardButton(text="🖼 Bannerlar", callback_data="admin_banners"),
            InlineKeyboardButton(text="💬 Izohlar", callback_data="admin_comments_anime"),
        ],
        [
            InlineKeyboardButton(text="🔗 Havolalar", callback_data="admin_links"),
            InlineKeyboardButton(text="🚫 Soʻz filtri", callback_data="admin_wordfilter"),
        ],
        [
            InlineKeyboardButton(text="📢 Sponsor baner", callback_data="admin_sponsor"),
            InlineKeyboardButton(text="💎 Premium", callback_data="admin_premium"),
        ],
        [
            InlineKeyboardButton(text="👑 Admin qo'shish", callback_data="admin_add_admin"),
            InlineKeyboardButton(text="📖 Qo'llanma", callback_data="admin_help"),
        ],
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

def anime_card_text(anime):
    return (
        f"<b>{anime['title']}</b>\n\n"
        f"📅 Yil: {anime['year']}\n"
        f"🌍 Davlat: {anime['country']}\n"
        f"🗣 Til: {anime.get('language', 'Nomalum')}\n"
        f"🎭 Janr: {anime['genre']}\n\n"
        f"📝 {anime['description']}"
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
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {a['title']}", callback_data=f"{prefix}_{a['id']}"
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
            ep = await asyncio.to_thread(db.get_episode, episode_id)
            if ep:
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

    if await asyncio.to_thread(db.get_setting, "maintenance") == "1" and message.from_user.id != ADMIN_ID:
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
                "📢 Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
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
@dp.callback_query(F.data == "accept_rules")
async def accept_rules(call: CallbackQuery, state: FSMContext):
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
        try:
            await bot.send_message(
                ADMIN_ID,
                f"👤 <b>Yangi foydalanuvchi!</b>\n\n"
                f"📌 Ism: {user.full_name}\n"
                f"🔢 Raqam: {u['join_number']}-chi\n"
                f"🆔 ID: <code>{user.id}</code>\n"
                f"👤 Username: @{user.username or 'yoq'}\n"
                f"📱 Telefon: {phone}\n"
                f"📅 Sana: {u['joined_at'][:10]}\n\n"
                f"📊 Jami: {u['join_number']} ta foydalanuvchi",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="👤 Profilni ko'rish", url=f"tg://user?id={user.id}")]
                ]),
                parse_mode="HTML"
            )
        except Exception:
            pass

    # Klaviaturani yopish
    await message.answer("✅ Ro'yxatdan o'tdingiz!", reply_markup=ReplyKeyboardRemove())

    # Obuna tekshirish
    subscribed = await check_subscription(user.id)
    if not subscribed:
        await message.answer(
            "📢 Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
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
    await call.answer()
    subscribed = await check_subscription(call.from_user.id)
    if subscribed:
        await call.message.edit_text(
            f"👋 Salom, {call.from_user.full_name}!\n"
            f"🎌 AniFilm Bot ga xush kelibsiz\n\n"
            f"👇 Nimani qidiryapsiz?",
            reply_markup=main_keyboard()
        )
    else:
        await call.answer("❌ Hali obuna bolmadingiz!", show_alert=True)

# ===================== BOT BLOKLANSA =====================
@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED))
async def user_blocked_bot(event: ChatMemberUpdated):
    user_id = event.from_user.id
    user = event.from_user
    await asyncio.to_thread(db.set_user_inactive, user_id)
    u = await asyncio.to_thread(db.get_user, user_id)
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🚫 <b>Foydalanuvchi chiqib ketdi!</b>\n\n"
            f"📌 Ism: {user.full_name}\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"👤 Username: @{user.username or 'yoq'}\n"
            f"🔢 Raqam: {u['join_number'] if u else '?'}-chi\n\n"
            f"🔒 Avtomatik bloklandi.",
            parse_mode="HTML"
        )
    except Exception:
        pass

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

async def is_episode_locked_for_user(episode, user_id):
    """Agar epizod hali 'oldinroq kirish' muddatida bo'lsa va foydalanuvchi Premium bo'lmasa, True qaytaradi."""
    if user_id == ADMIN_ID:
        return False
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
    status = await asyncio.to_thread(db.get_premium_status, user_id)
    return not status["is_premium"]

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
        prices = await premium_settings()
        await call.answer(
            f"👑 Bu qism hozircha faqat Premium foydalanuvchilar uchun ochiq "
            f"({prices['early_hours']} soatdan keyin hammaga ochiladi).",
            show_alert=True
        )
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
@dp.message(Command("admin"))
async def admin_handler(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Ruxsat yoq!")
        return
    await message.answer("👑 <b>Admin Panel</b>", reply_markup=admin_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_back")
async def admin_back_handler(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await call.message.edit_text("👑 <b>Admin Panel</b>", reply_markup=admin_keyboard(), parse_mode="HTML")

# ---- ANIME QO'SHISH ----
@dp.callback_query(F.data == "admin_add")
async def admin_add(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
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

@dp.callback_query(F.data.in_(["set_type_film", "set_type_serial"]))
async def set_type(call: CallbackQuery, state: FSMContext):
    media_type = "film" if call.data == "set_type_film" else "serial"
    await state.update_data(media_type=media_type, video_ids=[])
    await state.set_state(AddAnime.videos)
    await call.message.edit_text("🎬 Videolarni yuboring. Tugagach /done yozing:")

@dp.message(AddAnime.videos, F.video)
async def add_video(message: Message, state: FSMContext):
    data = await state.get_data()
    video_ids = data.get("video_ids", [])
    sent = await bot.forward_message(STORAGE_CHANNEL, message.chat.id, message.message_id)
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
        data["genre"], data["description"], data.get("language", "Nomalum"), data["photo_id"], data["media_type"])
    for i, msg_id in enumerate(data["video_ids"], 1):
        await asyncio.to_thread(db.add_episode, anime_id, i, msg_id)
    await state.clear()

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

    await message.answer(
        f"✅ <b>{data['title']}</b> qoshildi!\n📹 {len(data['video_ids'])} ta video",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )

# ---- DAVOM QO'SHISH ----
@dp.callback_query(F.data == "admin_add_episode")
async def admin_add_episode(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
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
    episodes = await asyncio.to_thread(db.get_episodes, anime_id)
    next_ep = len(episodes) + 1
    await state.update_data(episode_anime_id=anime_id, episode_msg_ids=[], next_ep=next_ep)
    await state.set_state(AddEpisode.videos)
    await call.message.edit_text(
        f"🎬 Videolarni yuboring ({next_ep}-qismdan boshlanadi).\nTugagach /done yozing:"
    )

@dp.message(AddEpisode.videos, F.video)
async def addepi_video(message: Message, state: FSMContext):
    data = await state.get_data()
    msg_ids = data.get("episode_msg_ids", [])
    sent = await bot.forward_message(STORAGE_CHANNEL, message.chat.id, message.message_id)
    msg_ids.append(sent.message_id)
    await state.update_data(episode_msg_ids=msg_ids)
    ep_num = data["next_ep"] + len(msg_ids) - 1
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
    await message.answer(
        f"✅ {len(data['episode_msg_ids'])} ta qism qoshildi!",
        reply_markup=admin_keyboard()
    )

# ---- ANIME RO'YXATI ADMIN ----
@dp.callback_query(F.data.startswith("admin_list_"))
async def admin_list(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
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
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_list_0")]
        ]),
        parse_mode="HTML"
    )

# ---- TAHRIRLASH ----
@dp.callback_query(F.data == "admin_edit")
async def admin_edit(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
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
            [InlineKeyboardButton(text="❌ Bekor", callback_data="admin_back")],
        ])
    )

@dp.callback_query(F.data.startswith("efield_"))
async def edit_field(call: CallbackQuery, state: FSMContext):
    field = call.data.replace("efield_", "")
    await state.update_data(edit_field=field)
    await state.set_state(EditAnime.new_value)
    await call.message.edit_text("✏️ Yangi qiymatni yozing:")

@dp.message(EditAnime.new_value)
async def edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    await asyncio.to_thread(db.update_anime, data["edit_anime_id"], data["edit_field"], message.text)
    await state.clear()
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
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    banners = await asyncio.to_thread(db.get_banners, False)
    text = "🖼 <b>Bannerlar</b>\n\nWebapp bosh sahifasidagi aylanuvchi bannerlarni shu yerdan boshqarasiz." if banners else "🖼 <b>Bannerlar</b>\n\nHozircha banner qo'shilmagan."
    await call.message.edit_text(text, reply_markup=banner_list_keyboard(banners), parse_mode="HTML")

@dp.callback_query(F.data == "banner_add")
async def banner_add(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
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
    if call.from_user.id != ADMIN_ID:
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
    if call.from_user.id != ADMIN_ID:
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
    if call.from_user.id != ADMIN_ID:
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
    if call.from_user.id != ADMIN_ID:
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
    if call.from_user.id != ADMIN_ID:
        return
    comment_id = int(call.data.split("_")[1])
    await asyncio.to_thread(db.delete_comment, comment_id)
    await call.answer("✅ Izoh o'chirildi")

# ---- O'CHIRISH ----
@dp.callback_query(F.data == "admin_delete")
async def admin_delete(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
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
    await call.message.edit_text(
        f"🗑 <b>{anime['title']}</b> ochirildi!",
        reply_markup=admin_back(),
        parse_mode="HTML"
    )

# ---- QISMLAR ----
@dp.callback_query(F.data == "admin_episodes")
async def admin_episodes(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
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
    sent = await bot.forward_message(STORAGE_CHANNEL, message.chat.id, message.message_id)
    await asyncio.to_thread(db.update_episode, data["edit_ep_id"], sent.message_id)
    await state.clear()
    await message.answer("✅ Qism yangilandi!", reply_markup=admin_keyboard())

# ---- STATISTIKA ----
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
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
        reply_markup=admin_back(),
        parse_mode="HTML"
    )

# ---- XABAR YUBORISH ----
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
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
    await state.update_data(bc_message_id=message.message_id, bc_chat_id=message.chat.id)
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
        try:
            await bot.copy_message(
                user_id,
                data["bc_chat_id"],
                data["bc_message_id"],
                reply_markup=kb
            )
            sent += 1
        except TelegramForbiddenError:
            await asyncio.to_thread(db.set_user_inactive, user_id)
            failed += 1
        except Exception:
            failed += 1
    await call.message.edit_text(
        f"📨 Yuborildi!\n✅ {sent} ta\n❌ {failed} ta",
        reply_markup=admin_back()
    )

# ---- KANALLAR ----
@dp.callback_query(F.data == "admin_channels")
async def admin_channels(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
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
    await state.set_state(AddChannelState.channel)
    await call.message.edit_text(
        "📢 Format: @kanalnom | Kanal nomi\nMasalan: @anime_uz | Anime UZ"
    )

@dp.message(AddChannelState.channel)
async def ch_add_done(message: Message, state: FSMContext):
    await state.clear()
    parts = message.text.split("|")
    if len(parts) != 2:
        await message.answer("❌ Format notogri!\nMasalan: @anime_uz | Anime UZ")
        return
    await asyncio.to_thread(db.add_channel, parts[0].strip(), parts[1].strip())
    await message.answer("✅ Kanal qoshildi!", reply_markup=admin_keyboard())

@dp.callback_query(F.data == "ch_del")
async def ch_del(call: CallbackQuery):
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
    channel_id = call.data.replace("ch_del_", "")
    await asyncio.to_thread(db.delete_channel, channel_id)
    await call.answer("🗑 Ochirildi!", show_alert=True)
    await admin_channels(call)

# ---- BLOKLASH ----
@dp.callback_query(F.data == "admin_block")
async def admin_block_menu(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
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
    await state.set_state(BlockState.user_id)
    await call.message.edit_text("🚫 Foydalanuvchi ID yoki @username yozing:")

@dp.message(BlockState.user_id)
async def block_action(message: Message, state: FSMContext):
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
    await message.answer(
        f"🚫 <b>{u['full_name']}</b> bloklandi!",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "do_unblock")
async def do_unblock(call: CallbackQuery, state: FSMContext):
    await state.set_state(UnblockState.user_id)
    await call.message.edit_text("✅ Blokdan chiqarish uchun ID yoki @username yozing:")

@dp.message(UnblockState.user_id)
async def unblock_action(message: Message, state: FSMContext):
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
    await message.answer(
        f"✅ <b>{u['full_name']}</b> blokdan chiqarildi!",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )

# ---- TEXNIK ISHLAR ----
@dp.callback_query(F.data == "admin_maintenance")
async def admin_maintenance(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
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
    await call.answer(f"🔧 {status}", show_alert=True)
    await admin_maintenance(call)

# ---- KONTENT HIMOYASI ----
@dp.callback_query(F.data == "admin_content")
async def admin_content(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
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

# ---- WEBAPP HAVOLALARI (Kanal / Support) ----
async def _links_text():
    channel = await asyncio.to_thread(db.get_setting, "profile_channel_url") or "❌ Sozlanmagan"
    support = await asyncio.to_thread(db.get_setting, "profile_support_url") or "❌ Sozlanmagan"
    return f"🔗 <b>Webapp Profil havolalari</b>\n\n📢 Kanal: {channel}\n❓ Support: {support}"

@dp.callback_query(F.data == "admin_links")
async def admin_links(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
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
    if call.from_user.id != ADMIN_ID:
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
    if message.from_user.id != ADMIN_ID:
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
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await call.message.edit_text(await _wordfilter_text(), reply_markup=_wordfilter_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "wf_add")
async def wf_add_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
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
    if message.from_user.id != ADMIN_ID:
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
    if call.from_user.id != ADMIN_ID:
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
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await call.message.edit_text(await _sponsor_text(), reply_markup=_sponsor_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "sp_photo")
async def sp_photo_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(SponsorState.photo)
    await call.message.edit_text("🖼 Sponsor baner rasmini yuboring:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_sponsor")],
    ]))

@dp.message(SponsorState.photo, F.photo)
async def sp_photo_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await asyncio.to_thread(db.set_setting, "sponsor_photo_id", message.photo[-1].file_id)
    await state.clear()
    await message.answer("✅ Rasm saqlandi!")
    await message.answer(await _sponsor_text(), reply_markup=_sponsor_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "sp_title")
async def sp_title_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(SponsorState.title)
    await call.message.edit_text("✏️ Sponsor baner sarlavhasini yuboring (masalan: \"Bizning boshqa botimiz\"):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_sponsor")],
    ]))

@dp.message(SponsorState.title)
async def sp_title_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await asyncio.to_thread(db.set_setting, "sponsor_title", (message.text or "").strip()[:80])
    await state.clear()
    await message.answer("✅ Sarlavha saqlandi!")
    await message.answer(await _sponsor_text(), reply_markup=_sponsor_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "sp_url")
async def sp_url_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(SponsorState.url)
    await call.message.edit_text("🔗 Bosilganda ochiladigan havolani yuboring (https:// bilan):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin_sponsor")],
    ]))

@dp.message(SponsorState.url)
async def sp_url_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
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
    if call.from_user.id != ADMIN_ID:
        return
    await asyncio.to_thread(db.set_setting, "sponsor_photo_id", "")
    await asyncio.to_thread(db.set_setting, "sponsor_title", "")
    await asyncio.to_thread(db.set_setting, "sponsor_url", "")
    await call.answer("✅ Baner oʻchirildi", show_alert=True)
    await call.message.edit_text(await _sponsor_text(), reply_markup=_sponsor_kb(), parse_mode="HTML")

# ---- FOYDALANUVCHI QIDIRISH ----
@dp.callback_query(F.data == "admin_find_user")
async def admin_find_user(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
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
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_back")]
        ]),
        parse_mode="HTML"
    )

# ---- KUNLIK HISOBOT ----
@dp.callback_query(F.data == "admin_report")
async def admin_report(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
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

# ---- ADMIN QO'LLANMA ----
@dp.callback_query(F.data == "admin_help")
async def admin_help(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.message.edit_text(
        "📖 <b>Admin Qollanma</b>\n\n"
        "➕ <b>Anime qoshish:</b>\n"
        "Rasm → Nom → Yil → Davlat → Janr → Malumot → "
        "Tur (film/serial) → Videolarni yuboring → "
        "/done yozing → Kanalga saqlanadi\n\n"
        "➕ <b>Davom qoshish:</b>\n"
        "Royxat yoki nom → Serial tanlang → "
        "Videolar yuboring → /done\n\n"
        "✏️ <b>Tahrirlash:</b>\n"
        "Royxat yoki nom → Maydon tanlang → Yangi qiymat\n\n"
        "🗑 <b>Ochirish:</b>\n"
        "Royxat yoki nom → Tasdiqlang\n\n"
        "📨 <b>Xabar:</b>\n"
        "Oddiy — matn/rasm/video\n"
        "Inline — xabar + tugma + link\n\n"
        "📢 <b>Kanal formati:</b>\n"
        "@kanalnom | Kanal nomi\n\n"
        "🔧 <b>Texnik ishlar:</b>\n"
        "Yoqilsa foydalanuvchilar kira olmaydi\n\n"
        "🔒 <b>Kontent himoyasi:</b>\n"
        "Yoqilsa video forward/save bloklanadi",
        reply_markup=admin_back(),
        parse_mode="HTML"
    )


# ---- ADMIN QO'SHISH ----
@dp.callback_query(F.data == "admin_add_admin")
async def admin_add_admin(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.message.edit_text(
        "👑 Yangi admin ID sini yozing:",
        reply_markup=admin_back()
    )
    await state.set_state(FindUserState.query)
    await state.update_data(action="add_admin")

# ===================== KUNLIK HISOBOT TIMER =====================
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
    return web.Response(text="AniFilm Bot ishlayapti!")

def _webapp_user_id(request):
    """Query yoki path'dan user_id ni xavfsiz oʻqib olish."""
    try:
        return int(request.query.get("user_id", "0"))
    except Exception:
        return 0

async def webapp_access_status(user_id: int):
    """Webapp uchun kirish holatini tekshiradi: texnik ishlar va majburiy obuna."""
    is_admin = user_id == ADMIN_ID

    maintenance = await asyncio.to_thread(db.get_setting, "maintenance") == "1"
    if maintenance and not is_admin:
        return {"maintenance": True, "subscribed": True, "channels": []}

    if is_admin:
        return {"maintenance": False, "subscribed": True, "channels": []}

    subscribed = await check_subscription(user_id)
    channels_out = []
    if not subscribed:
        channels = await asyncio.to_thread(db.get_channels)
        for ch in channels:
            channels_out.append({
                "name": ch["channel_name"],
                "url": f"https://t.me/{ch['channel_id'].lstrip('@')}"
            })
    return {"maintenance": False, "subscribed": subscribed, "channels": channels_out}

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
    u = await asyncio.to_thread(db.get_user, user_id)
    channel_url = await asyncio.to_thread(db.get_setting, "profile_channel_url")
    support_url = await asyncio.to_thread(db.get_setting, "profile_support_url")
    premium = await asyncio.to_thread(db.get_premium_status, user_id)
    prices = await premium_settings()
    return web.json_response({
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
    })

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
    for ep in data.get("episodes", []):
        ep["is_locked"] = await is_episode_locked_for_user(ep, user_id)
    return web.json_response(data)

async def webapp_send_episode(request):
    try:
        data = await request.json()
        user_id = int(data.get("user_id"))
        episode_id = int(data.get("episode_id"))
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)

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
        user_id = int(data.get("user_id"))
        anime_id = int(data.get("anime_id"))
        text = str(data.get("text", "")).strip()
        username = str(data.get("username") or "")[:64]
        parent_id = data.get("parent_id")
        parent_id = int(parent_id) if parent_id else None
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)

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
        user_id = int(data.get("user_id"))
        comment_id = int(data.get("comment_id"))
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    status = await webapp_access_status(user_id)
    if not status["subscribed"]:
        return web.json_response({"error": "not_subscribed"}, status=403)
    liked, count = await asyncio.to_thread(db.toggle_comment_like, comment_id, user_id)
    return web.json_response({"ok": True, "liked": liked, "likes": count})

async def webapp_get_position(request):
    try:
        user_id = int(request.query.get("user_id"))
        episode_id = int(request.match_info["episode_id"])
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    seconds = await asyncio.to_thread(db.get_watch_position, user_id, episode_id)
    return web.json_response({"position": seconds})

async def webapp_save_position(request):
    try:
        data = await request.json()
        user_id = int(data.get("user_id"))
        episode_id = int(data.get("episode_id"))
        seconds = int(data.get("position", 0))
    except Exception:
        return web.json_response({"error": "notogri sorov"}, status=400)
    await asyncio.to_thread(db.set_watch_position, user_id, episode_id, seconds)
    return web.json_response({"ok": True})

async def debug_path(request):
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

    async def serve_webapp_file(request):
        filename = request.match_info["filename"]
        filepath = os.path.join(webapp_dir, filename)
        if not os.path.exists(filepath):
            return web.Response(text="Topilmadi", status=404)
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

        try:
            msg = await pyro.get_messages(STORAGE_CHANNEL, ep["channel_message_id"])
        except Exception as e:
            logger.warning(f"[stream] birinchi urinish muvaffaqiyatsiz ({e}), sinxronlash signali orqali qayta urinilmoqda...")
            try:
                sync_msg = await bot.send_message(STORAGE_CHANNEL, "🔄")
                await asyncio.sleep(2)
                try:
                    await sync_msg.delete()
                except Exception:
                    pass
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
        max_retries = 4
        retries = 0
        try:
            while sent < length:
                cur_offset_bytes = start + sent
                offset_chunk = cur_offset_bytes // STREAM_CHUNK_SIZE
                cut = cur_offset_bytes % STREAM_CHUNK_SIZE
                try:
                    first_piece = True
                    async for chunk in pyro.stream_media(msg, offset=offset_chunk):
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
                    if retries > max_retries or sent >= length:
                        logger.error(f"[stream] uzatishda uzilish, qayta urinishlar tugadi: {e}")
                        break
                    logger.warning(f"[stream] uzilish ({e}), {cur_offset_bytes} baytdan qayta ulanilmoqda ({retries}/{max_retries})...")
                    await asyncio.sleep(0.5)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.error(f"[stream] kutilmagan xato: {e}")
        return resp

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/debug", debug_path)
    app.router.add_get("/webapp", serve_webapp_index)
    app.router.add_get("/webapp/", serve_webapp_index)
    app.router.add_get("/webapp/{filename}", serve_webapp_file)
    app.router.add_get("/api/check_access", webapp_check_access)
    app.router.add_get("/api/profile", webapp_profile)
    app.router.add_get("/api/animes", webapp_animes_list)
    app.router.add_get("/api/animes/{anime_id}", webapp_anime_detail)
    app.router.add_get("/api/photo/{photo_id}", webapp_photo)
    app.router.add_get("/api/sponsor", webapp_sponsor)
    app.router.add_post("/api/send_episode", webapp_send_episode)
    app.router.add_get("/api/banners", webapp_banners)
    app.router.add_get("/api/categories", webapp_categories)
    app.router.add_get("/api/comments/{anime_id}", webapp_get_comments)
    app.router.add_post("/api/comments", webapp_add_comment)
    app.router.add_post("/api/comments/like", webapp_toggle_like)
    app.router.add_get("/api/position/{episode_id}", webapp_get_position)
    app.router.add_post("/api/position", webapp_save_position)
    app.router.add_get("/stream/{episode_id}", webapp_stream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("Web server ishga tushdi!")

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
    if STREAM_ENABLED:
        try:
            await pyro.start()
            # Pyrogram (MTProto) botning a'zo bo'lgan kanalni "tanishi" uchun kamida bitta
            # yangi hodisani (update) shu sessiya orqali ko'rishi kerak — aks holda ID orqali
            # xabar olishga uringanda "Peer id invalid" xato beradi. Shuning uchun kanalga
            # bitta signal xabar yuboramiz (aiogram orqali) — bu Pyrogram sessiyasiga ham
            # keladi va kanalning toʻliq maʼlumotini (access_hash) keshga yozadi.
            try:
                await pyro.get_chat(STORAGE_CHANNEL)
                logger.info("Pyrogram: kanal peer keshi allaqachon mavjud.")
            except Exception:
                logger.info("Pyrogram: kanal peer keshi boʻsh, sinxronlash signali yuborilmoqda...")
                try:
                    sync_msg = await bot.send_message(STORAGE_CHANNEL, "🔄")
                    await asyncio.sleep(2)
                    try:
                        await sync_msg.delete()
                    except Exception:
                        pass
                    await pyro.get_chat(STORAGE_CHANNEL)
                    logger.info("Pyrogram: kanal peer keshi muvaffaqiyatli toʻldirildi.")
                except Exception as e:
                    logger.warning(f"Pyrogram: sinxronlash signali yuborilmadi/xato: {e}")
            logger.info("Pyrogram (onlayn striming) ishga tushdi!")
        except Exception as e:
            logger.error(f"Pyrogram ishga tushmadi: {e}")
    else:
        logger.warning("API_ID/API_HASH topilmadi — onlayn striming o'chirilgan.")
    await start_web_server()
    asyncio.create_task(daily_report_task())
    asyncio.create_task(premium_maintenance_task())
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "my_chat_member", "web_app_data"]
        )
    finally:
        if STREAM_ENABLED and pyro:
            await pyro.stop()

if __name__ == "__main__":
    # asyncio.run() emas — importda yaratilgan _MAIN_LOOP'ning aynan oʻzida ishga tushiramiz,
    # aks holda Pyrogram "attached to a different loop" xatosini beradi.
    try:
        _MAIN_LOOP.run_until_complete(main())
    finally:
        _MAIN_LOOP.close()
