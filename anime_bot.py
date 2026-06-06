import asyncio
import logging
import math
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatMemberUpdated
)
from aiogram.filters import CommandStart, Command, ChatMemberUpdatedFilter, KICKED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramForbiddenError

import database as db

# ===================== SOZLAMALAR =====================
BOT_TOKEN = "5757819990:AAGZZyehS1WK0eWLYlsxqpAmJeOFIfUQTLw"
ADMIN_ID = 5383321037

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ===================== STATES =====================
class AddAnime(StatesGroup):
    photo = State()
    title = State()
    year = State()
    country = State()
    genre = State()
    description = State()
    media_type = State()
    videos = State()

class EditAnime(StatesGroup):
    choose_field = State()
    new_value = State()

class AddEpisode(StatesGroup):
    choose_anime = State()
    videos = State()

class BroadcastState(StatesGroup):
    message = State()

class AddChannelState(StatesGroup):
    channel = State()

class SearchState(StatesGroup):
    query = State()

class BlockState(StatesGroup):
    user_id = State()

# ===================== YORDAMCHI =====================
async def check_subscription(user_id):
    channels = db.get_channels()
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

def sub_keyboard():
    channels = db.get_channels()
    buttons = []
    for ch in channels:
        buttons.append([InlineKeyboardButton(
            text=f"📢 {ch['channel_name']}",
            url=f"https://t.me/{ch['channel_id'].lstrip('@')}"
        )])
    buttons.append([InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Qidiruv", callback_data="search")],
        [
            InlineKeyboardButton(text="🎬 Anime Film", callback_data="films_0"),
            InlineKeyboardButton(text="📺 Anime Serial", callback_data="serials_0"),
        ],
        [InlineKeyboardButton(text="🎲 Random", callback_data="random")],
    ])

def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Bosh menu", callback_data="main_menu")]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Anime qo'shish", callback_data="admin_add"),
            InlineKeyboardButton(text="📋 Ro'yxat", callback_data="admin_list_0"),
        ],
        [
            InlineKeyboardButton(text="🎬 Qismlar", callback_data="admin_episodes"),
            InlineKeyboardButton(text="👥 Foydalanuvchi", callback_data="admin_users"),
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
        [InlineKeyboardButton(text="📖 Qo'llanma", callback_data="admin_help")],
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
    buttons.append([InlineKeyboardButton(text="🏠 Bosh menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def episodes_keyboard(episodes, anime_id, page=0):
    per_page = 6
    total_pages = math.ceil(len(episodes) / per_page) or 1
    start = page * per_page
    chunk = episodes[start:start + per_page]
    buttons = []
    row = []
    for i, ep in enumerate(chunk):
        row.append(InlineKeyboardButton(
            text=f"{ep['episode_number']}-qism",
            callback_data=f"ep_{ep['id']}"
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
    buttons.append([InlineKeyboardButton(text="🏠 Bosh menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def anime_card_text(anime):
    return (
        f"<b>{anime['title']}</b>\n\n"
        f"📅 Yil: {anime['year']}\n"
        f"🌍 Davlat: {anime['country']}\n"
        f"🎭 Janr: {anime['genre']}\n\n"
        f"📝 {anime['description']}"
    )

async def send_anime_card(chat_id, anime):
    protect = db.get_setting("content_protect") == "1"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬇️ Yuklab olish", callback_data=f"download_{anime['id']}_0")],
        [InlineKeyboardButton(text="🏠 Bosh menu", callback_data="main_menu")]
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

# ===================== /START =====================
@dp.message(CommandStart())
async def start_handler(message: Message):
    if db.get_setting("maintenance") == "1" and message.from_user.id != ADMIN_ID:
        await message.answer("🔧 Texnik ishlar olib borilmoqda.\nIltimos, kuting...")
        return

    user = message.from_user
    is_new = db.add_user(user.id, user.username, user.full_name)

    if is_new:
        u = db.get_user(user.id)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"👤 <b>Yangi foydalanuvchi!</b>\n\n"
                f"📌 Ism: {user.full_name}\n"
                f"🔢 Raqam: {u['join_number']}-chi\n"
                f"🆔 ID: <code>{user.id}</code>\n"
                f"👤 Username: @{user.username or 'yoq'}\n\n"
                f"📊 Jami: {u['join_number']} ta foydalanuvchi",
                parse_mode="HTML"
            )
        except Exception:
            pass

    u = db.get_user(user.id)
    if u and u.get("is_blocked"):
        await message.answer("🚫 Siz bloklandingiz.")
        return

    subscribed = await check_subscription(user.id)
    if not subscribed:
        await message.answer(
            "📢 Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
            reply_markup=sub_keyboard()
        )
        return

    await message.answer(
        "🌸 <b>AniFilm Bot</b> ga xush kelibsiz!\n\n"
        "⚠️ <b>Diqqat:</b> Botni bloklasangiz yoki chiqib ketsangiz — "
        "avtomatik bloklanasiz!\n\n"
        "Davom etish uchun tugmani bosing:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Qabul qilaman", callback_data="accept")]
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "accept")
async def accept_handler(call: CallbackQuery):
    await call.message.edit_text("📋 Bosh menu:", reply_markup=main_keyboard())

@dp.callback_query(F.data == "check_sub")
async def check_sub_handler(call: CallbackQuery):
    subscribed = await check_subscription(call.from_user.id)
    if subscribed:
        await call.message.edit_text(
            "🌸 <b>AniFilm Bot</b> ga xush kelibsiz!\n\n"
            "⚠️ <b>Diqqat:</b> Botni bloklasangiz — avtomatik bloklanasiz!\n\n"
            "Davom etish uchun tugmani bosing:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Qabul qilaman", callback_data="accept")]
            ]),
            parse_mode="HTML"
        )
    else:
        await call.answer("❌ Hali obuna bolmadingiz!", show_alert=True)

# ===================== FOYDALANUVCHI BOTNI BLOKLASA =====================
@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED))
async def user_blocked_bot(event: ChatMemberUpdated):
    user_id = event.from_user.id
    user = event.from_user
    db.set_user_inactive(user_id)
    u = db.get_user(user_id)
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

# ===================== /HELP =====================
@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer(
        "📖 <b>Qollanma</b>\n\n"
        "🔍 <b>Qidiruv</b> — anime qidirish\n"
        "🎬 <b>Anime Film</b> — filmlar royxati\n"
        "📺 <b>Anime Serial</b> — seriallar royxati\n"
        "🎲 <b>Random</b> — tasodifiy anime\n\n"
        "📥 <b>Yuklab olish:</b>\n"
        "• Film → video darhol\n"
        "• Serial → qismlar chiqadi\n\n"
        "📌 Muammo bolsa admin bilan bolaning.",
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )

# ===================== BOSH MENU =====================
@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    u = db.get_user(call.from_user.id)
    if u and u.get("is_blocked"):
        await call.answer("🚫 Siz bloklandingiz.", show_alert=True)
        return
    await call.message.edit_text("📋 Bosh menu:", reply_markup=main_keyboard())

@dp.callback_query(F.data == "noop")
async def noop_handler(call: CallbackQuery):
    await call.answer()

# ===================== QIDIRUV =====================
@dp.callback_query(F.data == "search")
async def search_callback(call: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.query)
    await call.message.edit_text("🔍 Anime nomini yozing:", reply_markup=back_keyboard())

@dp.message(SearchState.query)
async def search_result(message: Message, state: FSMContext):
    await state.clear()
    query = message.text.strip()
    results = db.search_anime(query)
    if not results:
        await message.answer("❌ Hech narsa topilmadi.", reply_markup=main_keyboard())
        return
    buttons = []
    for a in results[:10]:
        icon = "🎬" if a["media_type"] == "film" else "📺"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {a['title']}",
            callback_data=f"anime_{a['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="🏠 Bosh menu", callback_data="main_menu")])
    await message.answer(
        f"🔍 <b>{query}</b> natijalari:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )

# ===================== FILMLAR =====================
@dp.callback_query(F.data.startswith("films_"))
async def films_list(call: CallbackQuery):
    page = int(call.data.split("_")[1])
    animes = db.get_animes("film", page)
    total = db.get_anime_count("film")
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
    page = int(call.data.split("_")[1])
    animes = db.get_animes("serial", page)
    total = db.get_anime_count("serial")
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
    anime_id = int(call.data.split("_")[1])
    anime = db.get_anime(anime_id)
    if not anime:
        await call.answer("❌ Topilmadi", show_alert=True)
        return
    db.increment_views(anime_id)
    await call.message.delete()
    await send_anime_card(call.message.chat.id, anime)

# ===================== YUKLAB OLISH =====================
@dp.callback_query(F.data.startswith("download_"))
async def download_handler(call: CallbackQuery):
    parts = call.data.split("_")
    anime_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    anime = db.get_anime(anime_id)
    if not anime:
        await call.answer("❌ Topilmadi", show_alert=True)
        return
    protect = db.get_setting("content_protect") == "1"
    episodes = db.get_episodes(anime_id)
    if not episodes:
        await call.answer("❌ Video hali yuklanmagan!", show_alert=True)
        return
    if anime["media_type"] == "film":
        await bot.send_video(
            call.message.chat.id,
            video=episodes[0]["file_id"],
            caption=f"🎬 {anime['title']}",
            protect_content=protect
        )
    else:
        await call.message.edit_reply_markup(
            reply_markup=episodes_keyboard(episodes, anime_id, page)
        )

@dp.callback_query(F.data.startswith("eps_"))
async def episodes_page(call: CallbackQuery):
    parts = call.data.split("_")
    anime_id = int(parts[1])
    page = int(parts[2])
    episodes = db.get_episodes(anime_id)
    await call.message.edit_reply_markup(
        reply_markup=episodes_keyboard(episodes, anime_id, page)
    )

@dp.callback_query(F.data.startswith("ep_"))
async def episode_handler(call: CallbackQuery):
    episode_id = int(call.data.split("_")[1])
    ep = db.get_episode(episode_id)
    if not ep:
        await call.answer("❌ Topilmadi", show_alert=True)
        return
    protect = db.get_setting("content_protect") == "1"
    anime = db.get_anime(ep["anime_id"])
    await bot.send_video(
        call.message.chat.id,
        video=ep["file_id"],
        caption=f"📺 {anime['title']} — {ep['episode_number']}-qism",
        protect_content=protect
    )

# ===================== RANDOM =====================
@dp.callback_query(F.data == "random")
async def random_handler(call: CallbackQuery):
    anime = db.get_random_anime()
    if not anime:
        await call.answer("❌ Hozircha anime yoq!", show_alert=True)
        return
    db.increment_views(anime["id"])
    await call.message.delete()
    await send_anime_card(call.message.chat.id, anime)

# ===================== /ADMIN =====================
@dp.message(Command("admin"))
async def admin_handler(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Ruxsat yoq!")
        return
    await message.answer("👑 <b>Admin Panel</b>", reply_markup=admin_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_back")
async def admin_back(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await call.message.edit_text("👑 <b>Admin Panel</b>", reply_markup=admin_keyboard(), parse_mode="HTML")

# ---- STATISTIKA ----
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    s = db.get_stats()
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
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_back")]
        ]),
        parse_mode="HTML"
    )

# ---- ANIME QO'SHISH ----
@dp.callback_query(F.data == "admin_add")
async def admin_add(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AddAnime.photo)
    await call.message.edit_text(
        "🖼 Anime rasmini yuboring:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor", callback_data="admin_back")]
        ])
    )

@dp.message(AddAnime.photo, F.photo)
async def add_photo(message: Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddAnime.title)
    await message.answer("📌 Anime nomini yozing:")

@dp.message(AddAnime.title)
async def add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(AddAnime.year)
    await message.answer("📅 Yilini yozing (masalan: 2002):")

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
    video_ids.append(message.video.file_id)
    await state.update_data(video_ids=video_ids)
    await message.answer(f"✅ {len(video_ids)}-video qabul qilindi. /done yozing yoki davom eting.")

@dp.message(AddAnime.videos, Command("done"))
async def add_done(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("video_ids"):
        await message.answer("❌ Video yuklanmadi!")
        return
    anime_id = db.add_anime(
        data["title"], data["year"], data["country"],
        data["genre"], data["description"], data["photo_id"], data["media_type"]
    )
    for i, file_id in enumerate(data["video_ids"], 1):
        db.add_episode(anime_id, i, file_id)
    await state.clear()
    await message.answer(
        f"✅ <b>{data['title']}</b> qoshildi!\n📹 {len(data['video_ids'])} ta video",
        reply_markup=admin_keyboard(), parse_mode="HTML"
    )

# ---- ANIME RO'YXATI ADMIN ----
@dp.callback_query(F.data.startswith("admin_list_"))
async def admin_list(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    page = int(call.data.split("_")[2])
    animes = db.get_animes(page=page)
    total = db.get_anime_count()
    per_page = 10
    total_pages = math.ceil(total / per_page) or 1
    buttons = []
    for a in animes:
        icon = "🎬" if a["media_type"] == "film" else "📺"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {a['title']}", callback_data=f"admin_anime_{a['id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin_list_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin_list_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_back")])
    await call.message.edit_text(
        "📋 <b>Anime royxati</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("admin_anime_"))
async def admin_anime_detail(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    anime_id = int(call.data.split("_")[2])
    anime = db.get_anime(anime_id)
    if not anime:
        await call.answer("❌ Topilmadi")
        return
    await call.message.edit_text(
        f"<b>{anime['title']}</b>\n"
        f"📅 {anime['year']} | 🌍 {anime['country']}\n"
        f"🎭 {anime['genre']}\n👁 Korishlar: {anime['views']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Tahrirlash", callback_data=f"edit_anime_{anime_id}"),
                InlineKeyboardButton(text="🗑 Ochirish", callback_data=f"del_anime_{anime_id}"),
            ],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_list_0")]
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("del_anime_"))
async def delete_anime(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    anime_id = int(call.data.split("_")[2])
    anime = db.get_anime(anime_id)
    db.delete_anime(anime_id)
    await call.message.edit_text(
        f"🗑 <b>{anime['title']}</b> ochirildi!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_list_0")]
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("edit_anime_"))
async def edit_anime(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    anime_id = int(call.data.split("_")[2])
    await state.update_data(edit_anime_id=anime_id)
    await state.set_state(EditAnime.choose_field)
    await call.message.edit_text(
        "✏️ Qaysi maydonni tahrirlaysiz?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📌 Nomi", callback_data="efield_title")],
            [InlineKeyboardButton(text="📅 Yili", callback_data="efield_year")],
            [InlineKeyboardButton(text="🌍 Davlat", callback_data="efield_country")],
            [InlineKeyboardButton(text="🎭 Janr", callback_data="efield_genre")],
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
    db.update_anime(data["edit_anime_id"], data["edit_field"], message.text)
    await state.clear()
    await message.answer("✅ Yangilandi!", reply_markup=admin_keyboard())

# ---- QISMLAR ----
@dp.callback_query(F.data == "admin_episodes")
async def admin_episodes(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.message.edit_text(
        "🎬 <b>Qism boshqaruvi</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Qism qoshish", callback_data="add_episode")],
            [InlineKeyboardButton(text="🗑 Qism ochirish", callback_data="del_episode_list")],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "add_episode")
async def add_episode_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    animes = db.get_animes("serial", 0, 50)
    if not animes:
        await call.answer("❌ Serial yoq!", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=a["title"], callback_data=f"addepto_{a['id']}")] for a in animes]
    buttons.append([InlineKeyboardButton(text="❌ Bekor", callback_data="admin_back")])
    await state.set_state(AddEpisode.choose_anime)
    await call.message.edit_text(
        "📺 Qaysi serialga qism qoshamiz?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("addepto_"))
async def add_episode_chosen(call: CallbackQuery, state: FSMContext):
    anime_id = int(call.data.split("_")[1])
    episodes = db.get_episodes(anime_id)
    next_ep = len(episodes) + 1
    await state.update_data(episode_anime_id=anime_id, episode_videos=[], next_ep=next_ep)
    await state.set_state(AddEpisode.videos)
    await call.message.edit_text(
        f"🎬 Videolarni yuboring ({next_ep}-qismdan boshlanadi).\nTugagach /done yozing:"
    )

@dp.message(AddEpisode.videos, F.video)
async def add_episode_video(message: Message, state: FSMContext):
    data = await state.get_data()
    videos = data.get("episode_videos", [])
    videos.append(message.video.file_id)
    await state.update_data(episode_videos=videos)
    ep_num = data["next_ep"] + len(videos) - 1
    await message.answer(f"✅ {ep_num}-qism qabul qilindi.")

@dp.message(AddEpisode.videos, Command("done"))
async def add_episode_done(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("episode_videos"):
        await message.answer("❌ Video yuklanmadi!")
        return
    for i, file_id in enumerate(data["episode_videos"]):
        db.add_episode(data["episode_anime_id"], data["next_ep"] + i, file_id)
    await state.clear()
    await message.answer(
        f"✅ {len(data['episode_videos'])} ta qism qoshildi!",
        reply_markup=admin_keyboard()
    )

# ---- FOYDALANUVCHILAR ----
@dp.callback_query(F.data == "admin_users")
async def admin_users(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    s = db.get_stats()
    await call.message.edit_text(
        f"👥 <b>Foydalanuvchilar</b>\n\n"
        f"Jami: {s['total']}\nFaol: {s['active']}\nBloklangan: {s['blocked']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_back")]
        ]),
        parse_mode="HTML"
    )

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
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "do_block")
async def do_block(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(BlockState.user_id)
    await state.update_data(block_action="block")
    await call.message.edit_text("🚫 Foydalanuvchi ID sini yozing:")

@dp.callback_query(F.data == "do_unblock")
async def do_unblock(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(BlockState.user_id)
    await state.update_data(block_action="unblock")
    await call.message.edit_text("✅ Blokdan chiqarish uchun ID yozing:")

@dp.message(BlockState.user_id)
async def block_action(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Notogri ID!")
        return
    if data["block_action"] == "block":
        db.block_user(user_id)
        await message.answer(f"🚫 {user_id} bloklandi!", reply_markup=admin_keyboard())
    else:
        db.unblock_user(user_id)
        await message.answer(f"✅ {user_id} blokdan chiqarildi!", reply_markup=admin_keyboard())
    await state.clear()

# ---- XABAR YUBORISH ----
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(BroadcastState.message)
    await call.message.edit_text(
        "📨 Barcha foydalanuvchilarga xabarni yozing:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor", callback_data="admin_back")]
        ])
    )

@dp.message(BroadcastState.message)
async def broadcast_send(message: Message, state: FSMContext):
    await state.clear()
    users = db.get_all_active_users()
    sent = 0
    failed = 0
    for user_id in users:
        try:
            await bot.copy_message(user_id, message.chat.id, message.message_id)
            sent += 1
        except TelegramForbiddenError:
            db.set_user_inactive(user_id)
            failed += 1
        except Exception:
            failed += 1
    await message.answer(
        f"📨 Xabar yuborildi!\n✅ {sent} ta\n❌ {failed} ta",
        reply_markup=admin_keyboard()
    )

# ---- KANALLAR ----
@dp.callback_query(F.data == "admin_channels")
async def admin_channels(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    channels = db.get_channels()
    text = "📢 <b>Majburiy kanallar</b>\n\n"
    if channels:
        for ch in channels:
            text += f"• {ch['channel_name']} ({ch['channel_id']})\n"
    else:
        text += "Hozircha kanal yoq."
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Qoshish", callback_data="add_channel")],
            [InlineKeyboardButton(text="🗑 Ochirish", callback_data="del_channel")],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "add_channel")
async def add_channel_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AddChannelState.channel)
    await state.update_data(channel_action="add")
    await call.message.edit_text(
        "📢 Format: @kanalnom | Kanal nomi\nMasalan: @anime_uz | Anime UZ"
    )

@dp.callback_query(F.data == "del_channel")
async def del_channel_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AddChannelState.channel)
    await state.update_data(channel_action="del")
    await call.message.edit_text("🗑 Ochirish uchun kanal username yozing (@kanalnom):")

@dp.message(AddChannelState.channel)
async def channel_action_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if data["channel_action"] == "add":
        parts = message.text.split("|")
        if len(parts) != 2:
            await message.answer("❌ Format notogri!\nMasalan: @anime_uz | Anime UZ")
            return
        db.add_channel(parts[0].strip(), parts[1].strip())
        await message.answer(f"✅ Kanal qoshildi!", reply_markup=admin_keyboard())
    else:
        db.delete_channel(message.text.strip())
        await message.answer("🗑 Kanal ochirildi!", reply_markup=admin_keyboard())

# ---- TEXNIK ISHLAR ----
@dp.callback_query(F.data == "admin_maintenance")
async def admin_maintenance(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    current = db.get_setting("maintenance")
    status = "✅ Yoqiq" if current == "1" else "❌ Ochiq"
    await call.message.edit_text(
        f"🔧 <b>Texnik ishlar</b>\nHolat: {status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yoqish", callback_data="maintenance_on"),
                InlineKeyboardButton(text="❌ Ochirish", callback_data="maintenance_off"),
            ],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.in_(["maintenance_on", "maintenance_off"]))
async def set_maintenance(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    value = "1" if call.data == "maintenance_on" else "0"
    db.set_setting("maintenance", value)
    status = "✅ Yoqildi" if value == "1" else "❌ Ochirildi"
    await call.answer(f"🔧 {status}", show_alert=True)
    await admin_maintenance(call)

# ---- KONTENT HIMOYASI ----
@dp.callback_query(F.data == "admin_content")
async def admin_content(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    current = db.get_setting("content_protect")
    status = "✅ Yoqiq" if current == "1" else "❌ Ochiq"
    await call.message.edit_text(
        f"🔒 <b>Kontent himoyasi</b>\n(Forward va saqlash bloklash)\nHolat: {status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yoqish", callback_data="content_on"),
                InlineKeyboardButton(text="❌ Ochirish", callback_data="content_off"),
            ],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_back")],
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.in_(["content_on", "content_off"]))
async def set_content(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    value = "1" if call.data == "content_on" else "0"
    db.set_setting("content_protect", value)
    await call.answer("✅ Saqlandi!", show_alert=True)
    await admin_content(call)

# ---- ADMIN QO'LLANMA ----
@dp.callback_query(F.data == "admin_help")
async def admin_help(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.message.edit_text(
        "📖 <b>Admin Qollanma</b>\n\n"
        "➕ <b>Anime qoshish:</b>\n"
        "Rasm → Nom → Yil → Davlat → Janr → Malumot → Tur → Video → /done\n\n"
        "🎬 <b>Qismlar:</b>\n"
        "Serialga yangi qism qoshish\n\n"
        "📨 <b>Xabar:</b>\n"
        "Barcha faol foydalanuvchilarga xabar\n\n"
        "📢 <b>Kanal qoshish formati:</b>\n"
        "@kanalnom | Kanal nomi\n\n"
        "🔧 <b>Texnik ishlar:</b>\n"
        "Yoqilsa — foydalanuvchilar kira olmaydi\n\n"
        "🔒 <b>Kontent himoyasi:</b>\n"
        "Yoqilsa — video forward/save bloklanadi",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_back")]
        ]),
        parse_mode="HTML"
    )

# ===================== ISHGA TUSHIRISH =====================
async def main():
    db.init_db()
    logger.info("Bot ishga tushmoqda...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
