"""
AniFilm — «Texnik ishlar» rejimi.

Bu fayl bazaga (database.py) UMUMAN ulanmaydi, shuning uchun baza ishlamay
turgan paytda ham:
  • botda /start (yoki istalgan xabar) bosilganda texnik ishlar xabari chiqadi;
  • saytga (anifilm.uz) kirganda texnik ishlar sahifasi chiqadi.

QANDAY YOQILADI:
  Render → anime-bot → Settings → Start Command ni vaqtincha shunga almashtiring:
      python maintenance.py
  (Avvalgi Start Command'ni biror joyga yozib qoʻying!)

QANDAY OʻCHIRILADI:
  Ishlar tugagach Start Command'ni avvalgi holiga qaytaring
  (masalan: python anime_bot.py).
"""
import asyncio
import html
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ============ SHU YERNI TAHRIRLASH MUMKIN ============
UPDATED = "28-noyabr, 2026"               # «Yangilangan» sanasi
CHANNEL_URL = "https://t.me/anifilm_uz"   # yangiliklar kanali havolasi
CHANNEL_TAG = "@anifilm_uz"
# =====================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("maintenance")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------- BOT ----------------------------
BOT_TEXT = (
    "🛠 <b>Texnik ishlar olib borilmoqda</b>\n\n"
    "Hurmatli foydalanuvchi! Hozirda AniFilm boti va anifilm.uz saytida texnik "
    "ishlar amalga oshirilmoqda, shu sabab bot vaqtincha ishlamaydi.\n\n"
    f"🗓 Yangilangan: {html.escape(UPDATED)}\n\n"
    "📢 Ishlar yakunlangach, birinchi boʻlib kanalimizda eʼlon qilamiz. "
    f"Bot va saytga oid barcha yangiliklar: {html.escape(CHANNEL_TAG)}\n\n"
    "Sabringiz uchun rahmat! 💙"
)

KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="📢 Yangiliklar kanali", url=CHANNEL_URL)]]
)

dp = Dispatcher()


@dp.message(F.chat.type == "private")
async def notice(message: Message):
    """/start ham, boshqa har qanday xabar ham — shu xabarni oladi."""
    await message.answer(BOT_TEXT, parse_mode="HTML", reply_markup=KB)


@dp.callback_query()
async def notice_callback(call: CallbackQuery):
    await call.answer(
        f"🛠 Texnik ishlar olib borilmoqda. Yangiliklar: {CHANNEL_TAG}",
        show_alert=True,
    )


# ---------------------------- SAYT ----------------------------
PAGE_TEMPLATE = """<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0e1330">
<meta name="color-scheme" content="dark">
<title>AniFilm — texnik ishlar</title>
<link rel="icon" href="/favicon.ico">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Dela+Gothic+One&family=Onest:wght@400;600;700&display=swap">
<style>
:root{color-scheme:dark;--bg:#0e1330;--p2:#1f2759;--ln:#2c3572;--tq:#2dd4bf;--tx:#f3f5ff;--dm:#9aa3d6}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;min-height:100dvh;display:grid;place-items:center;padding:28px 22px;background:var(--bg);color:var(--tx);font:16px/1.6 Onest,system-ui,sans-serif}
main{width:100%;max-width:480px}
.logo{display:block;height:44px;width:auto;margin:0 0 40px}
h1{margin:0 0 20px;font:400 clamp(27px,7.4vw,40px)/1.15 'Dela Gothic One',Onest,system-ui,sans-serif;text-wrap:balance}
p{margin:0 0 14px;color:var(--dm)}
.strip{display:flex;gap:6px;margin:30px 0}
.strip i{width:24px;height:16px;border-radius:4px;background:var(--p2);border:1px solid var(--ln);animation:frame 2.7s ease-in-out infinite;animation-delay:calc(var(--i)*.22s)}
@keyframes frame{0%,60%,100%{background:var(--p2);border-color:var(--ln)}25%{background:var(--tq);border-color:var(--tq)}}
.date{color:var(--tx);font-weight:600}
.btn{display:inline-flex;align-items:center;gap:9px;margin:8px 0 18px;padding:13px 22px;border-radius:12px;background:var(--tq);color:#06231f;font-weight:700;text-decoration:none}
.btn svg{width:18px;height:18px}
.btn:focus-visible{outline:3px solid var(--tx);outline-offset:3px}
.thanks{font-size:14px;margin:0}
@media (prefers-reduced-motion:reduce){.strip i{animation:none}.strip i:nth-child(5){background:var(--tq);border-color:var(--tq)}}
</style>
</head>
<body>
<main>
<img class="logo" src="/logo.png" alt="AniFilm" onerror="this.remove()">
<h1>Texnik ishlar olib borilmoqda</h1>
<p>Hurmatli foydalanuvchilar! Hozirda AniFilm boti va anifilm.uz saytida texnik ishlar amalga oshirilmoqda. Shu sabab bot va sayt vaqtincha ishlamasligi mumkin.</p>
<div class="strip" aria-hidden="true"><i style="--i:0"></i><i style="--i:1"></i><i style="--i:2"></i><i style="--i:3"></i><i style="--i:4"></i><i style="--i:5"></i><i style="--i:6"></i><i style="--i:7"></i><i style="--i:8"></i></div>
<p class="date">Yangilangan: %%UPDATED%%</p>
<p>Ishlar yakunlangach, birinchi boʻlib Telegram kanalimizda eʼlon qilamiz. Sayt va botga oid barcha yangiliklar shu yerda chiqadi.</p>
<a class="btn" href="%%CHANNEL_URL%%" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/></svg>Telegram kanalimiz</a>
<p class="thanks">Sabringiz uchun rahmat.</p>
</main>
</body>
</html>"""

PAGE = (
    PAGE_TEMPLATE
    .replace("%%UPDATED%%", html.escape(UPDATED))
    .replace("%%CHANNEL_URL%%", html.escape(CHANNEL_URL, quote=True))
)


def _file(name: str) -> web.StreamResponse:
    path = os.path.join(BASE_DIR, name)
    if os.path.isfile(path):
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})
    return web.Response(status=404)


async def logo(request: web.Request):
    return _file("logo.png")


async def favicon(request: web.Request):
    return _file("favicon.ico")


async def catch_all(request: web.Request):
    path = request.path
    # Sayt ochiq turgan brauzerlar uchun API/striming soʻrovlariga aniq javob
    if path.startswith(("/api/", "/stream/")):
        return web.json_response(
            {"ok": False, "error": "maintenance", "message": "Texnik ishlar olib borilmoqda"},
            status=503,
            headers={"Cache-Control": "no-store", "Retry-After": "3600"},
        )
    # "/" — 200 (Render yoki ping-xizmat tekshiruvi buzilmasin);
    # qolgan sahifalar — 503 (qidiruv tizimlari saytni indeksdan oʻchirib yubormasin)
    if path == "/":
        status, headers = 200, {"Cache-Control": "no-store"}
    else:
        status, headers = 503, {"Cache-Control": "no-store", "Retry-After": "3600"}
    return web.Response(
        text=PAGE, content_type="text/html", charset="utf-8", status=status, headers=headers
    )


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/logo.png", logo)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_route("*", "/{tail:.*}", catch_all)
    return app


# ---------------------------- ISHGA TUSHIRISH ----------------------------
async def main():
    runner = web.AppRunner(make_app())
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Texnik ishlar sahifasi ishga tushdi (port: %s)", port)

    if not BOT_TOKEN:
        log.warning("BOT_TOKEN topilmadi — faqat sayt sahifasi ishlayapti.")
        await asyncio.Event().wait()
        return

    bot = Bot(token=BOT_TOKEN)
    try:
        # Bot oʻchiq turganda yigʻilib qolgan eski xabarlarga javob yozib
        # Telegram limitiga tushib qolmaslik uchun ularni tashlab yuboramiz.
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Bot ishga tushdi (texnik ishlar xabari yoqilgan).")
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
