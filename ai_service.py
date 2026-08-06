"""
===================== AI XIZMATI (Google Gemini, bepul tarif) =====================
Bu modul botga sun'iy intellekt qo'shish uchun kerakli barcha funksiyalarni
o'z ichiga oladi:
  - ask_ai()                    -> foydalanuvchi bilan erkin suhbat (chatbot)
  - moderate_comment()          -> izohlarni spam/haqorat uchun tekshirish
  - generate_anime_description()-> admin uchun anime tavsifini AI bilan yozish
  - recommend_anime_ids()       -> foydalanuvchi tarixiga qarab anime tavsiyasi

Ishlatish uchun Render (yoki boshqa) muhitida GEMINI_API_KEY environment
variable'ni qo'shish kerak. Kalitni https://aistudio.google.com/apikey
sahifasidan BEPUL olish mumkin.

GEMINI_API_KEY o'rnatilmagan bo'lsa, bu modul o'zini o'chirilgan deb
hisoblaydi (AI_ENABLED=False) va barcha funksiyalar xavfsiz "bo'sh" natija
qaytaradi — botning qolgan qismi bunga bog'liq bo'lmagani uchun hech narsa
buzilmaydi.
"""
import os
import json
import logging
import asyncio
import aiohttp

logger = logging.getLogger("ai_service")

# Bitta umumiy aiohttp session butun bot hayoti davomida qayta ishlatiladi.
# Avval har chaqiriqda "async with aiohttp.ClientSession()" bilan YANGI
# session ochilar edi — bu har bir AI so'roviga qo'shimcha TCP/TLS handshake
# vaqtini (~yuzlab ms) qo'shib, botni sekinlashtirar edi.
_session = None
_session_lock = asyncio.Lock()


async def _get_session():
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession()
    return _session


async def close_ai_session():
    """Bot to'xtaganda chaqirish uchun (ixtiyoriy) — ochiq connection'larni tozalaydi."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# "gemini-3.6-flash" — Google'ning 2026-yil iyul oyida chiqargan eng yangi
# va bepul tarifda mavjud boʻlgan ENG KUCHLI Flash modeli (fikrlash/reasoning
# qobiliyati "Pro" darajasiga yaqinroq, lekin bepul). Faqat "Pro" modellari
# (masalan gemini-3.1-pro-preview) pullik — ular bepul tarifda mavjud emas.
# Agar kelajakda kvota/limit muammosi chiqsa, GEMINI_MODEL environment
# variable orqali "gemini-3.5-flash" yoki "gemini-2.5-flash"ga tushirish
# mumkin (ular ozroq "aqlli", lekin yengilroq va tezroq).
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

# ===================== GROQ — ZAXIRA (FALLBACK) XIZMATI =====================
# Gemini (asosiy model + barcha FALLBACK_MODELS) muvaffaqiyatsiz bo'lsa —
# masalan kvota tugasa, Google tomonidan xizmat vaqtincha to'xtatilsa yoki
# tarmoq xatosi bo'lsa — so'rov avtomatik ravishda Groq'ga (boshqa provayder,
# boshqa infratuzilma) yuboriladi. Ikkalasi ham ishlamasa, funksiya baribir
# xavfsiz "bo'sh" natija qaytaradi.
#
# Kalitni https://console.groq.com/keys sahifasidan BEPUL olish mumkin.
# GROQ_API_KEY o'rnatilmagan bo'lsa, Groq zaxirasi shunchaki chetlab
# o'tiladi — hech narsa buzilmaydi.
#
# Model ID'lar 2026-yil avgust holatiga ko'ra: "llama-3.3-70b-versatile"
# 2026-08-16'da butunlay o'chiriladi (Groq'ning rasmiy deprecation sahifasi),
# shu sabab undan foydalanilmadi. O'rniga:
#   - matn uchun: "openai/gpt-oss-120b" (Groq tavsiya qilgan joriy model)
#   - rasm (vision, masalan to'lov cheki) uchun: "qwen/qwen3.6-27b"
#     (hozircha Groq'dagi yagona vision-qo'llovchi model, "preview" holatida —
#     kelajakda Groq buni ham almashtirishi mumkin, shu sabab environment
#     variable orqali osongina yangilash imkoni qoldirildi)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_ENABLED = bool(GROQ_API_KEY)
GROQ_TEXT_MODEL = os.environ.get("GROQ_TEXT_MODEL", "openai/gpt-oss-120b")
GROQ_VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

AI_ENABLED = bool(GEMINI_API_KEY) or GROQ_ENABLED

if not AI_ENABLED:
    logger.warning(
        "GEMINI_API_KEY ham, GROQ_API_KEY ham o'rnatilmagan — AI funksiyalari "
        "o'chirilgan holda ishlaydi. Yoqish uchun Render > Environment'ga "
        "kamida bittasini qo'shing."
    )
elif not GEMINI_API_KEY:
    logger.warning(
        "GEMINI_API_KEY o'rnatilmagan — faqat Groq orqali ishlaydi "
        "(asosiy AI xizmati emas, faqat zaxira sifatida mo'ljallangan)."
    )
elif not GROQ_ENABLED:
    logger.info(
        "GROQ_API_KEY o'rnatilmagan — Gemini butunlay ishlamay qolsa, "
        "zaxira xizmati bo'lmaydi. Qo'shish uchun: console.groq.com/keys"
    )


# Asosiy model band/xato bersa, urinib ko'riladigan zaxira modellar (eng
# kuchlisidan kamroq kuchlisiga qarab). Asosiy model FALLBACK_MODELS ichida
# takrorlanmasin deb tekshiriladi.
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-3.5-flash"]


def _model_url(model_name):
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"


def _is_gemini3(model_name):
    """Gemini 3.x modellari thinkingConfig.thinkingLevel qabul qiladi;
    undan oldingi modellar (masalan gemini-2.5-flash) buni tushunmaydi va
    o'rniga eski thinkingBudget parametrini kutadi."""
    return model_name.startswith("gemini-3")


def _apply_thinking_config(generation_config, model_name, thinking_level):
    """MUHIM: gemini-3.6-flash standart holatda thinking_level='medium' bilan
    ishlaydi — ya'ni HAR bir so'rovda (hatto oddiy 'OK'/'BAD' javob kerak
    bo'lganda ham) model avval ichida fikrlaydi, bu esa javobni sezilarli
    sekinlashtiradi. Shu sabab har bir chaqiruv o'ziga mos thinking darajasini
    aniq ko'rsatishi kerak (murakkab vazifalarga ko'proq, oddiylariga kamroq)."""
    if not thinking_level:
        return
    if _is_gemini3(model_name):
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level.upper()}
    elif model_name == "gemini-2.5-flash":
        # Gemini 3'dan oldingi modellarda thinking token-budjet bilan
        # boshqariladi, level bilan emas. minimal/low -> eng kam budjet.
        budget = 0 if thinking_level in ("minimal", "low") else -1  # -1 = avtomatik
        generation_config["thinkingConfig"] = {"thinkingBudget": budget}
    # gemini-3.5-flash va shunga o'xshash boshqa modellar uchun ham
    # thinkingLevel ishlaydi (_is_gemini3 True qaytaradi).


async def _call_gemini(contents, system_instruction=None, temperature=0.7,
                        max_output_tokens=800, json_mode=False, timeout=25,
                        thinking_level="minimal", is_admin=True):
    """Gemini API'ga xom so'rov yuboradi. Asosiy model xato bersa (band,
    topilmadi va h.k.), zaxira modellar bilan qayta urinadi. Hammasi
    muvaffaqiyatsiz bo'lsa None qaytaradi.

    thinking_level: "minimal" | "low" | "medium" | "high" | None
      Vazifa qanchalik oddiy bo'lsa, shuncha past daraja tanlanishi kerak —
      bu javob tezligiga TO'G'RIDAN-TO'G'RI ta'sir qiladi. Masalan oddiy
      OK/BAD moderatsiya yoki JSON ID ro'yxati tanlashda "minimal" yetarli;
      faqat erkin, chuqurroq fikr talab qiladigan suhbatlarda "low"/"medium"
      ishlatilsin.

    is_admin: True (standart) — bu so'rov admin uchun (yoki admin panel/
      backend jarayoni uchun) ekanini bildiradi, bunday holda Gemini
      ASOSIY model bo'lib qoladi (Groq faqat Gemini butunlay ishlamasa
      zaxira sifatida ishlatiladi).
      False — bu oddiy foydalanuvchiga real vaqtda ko'rsatiladigan AI
      javobi ekanini bildiradi. Bunday holda, agar Groq ulangan bo'lsa,
      SO'ROV AVVAL GROQ'GA yuboriladi (Gemini kvotasi/resursi admin
      funksiyalari uchun asrab qolinishi uchun) — Groq ishlamasa yoki
      bo'sh javob qaytarsa, xavfsizlik uchun baribir Gemini'ga
      o'tiladi (pastdagi odatiy yo'l)."""
    if not AI_ENABLED:
        return None

    if not is_admin and GROQ_ENABLED:
        # Oddiy foydalanuvchi so'rovi — Gemini o'rniga birinchi navbatda
        # Groq sinaladi. Groq muvaffaqiyatsiz bo'lsa, pastdagi odatiy
        # Gemini yo'liga (zaxira sifatida) o'tiladi.
        groq_first_result = await _call_groq(
            contents, system_instruction=system_instruction,
            temperature=temperature, max_output_tokens=max_output_tokens,
            json_mode=json_mode, timeout=timeout,
        )
        if groq_first_result:
            return groq_first_result
        logger.info(
            "Oddiy foydalanuvchi so'rovi uchun Groq ishlamadi — "
            "zaxira sifatida Gemini'ga o'tilyapti"
        )

    if not GEMINI_API_KEY:
        # Gemini kaliti umuman yo'q — Gemini modellarini sinab ko'rishning
        # ma'nosi yo'q, to'g'ridan-to'g'ri Groq'ga o'tamiz.
        return await _call_groq(
            contents, system_instruction=system_instruction,
            temperature=temperature, max_output_tokens=max_output_tokens,
            json_mode=json_mode, timeout=timeout,
        )

    base_generation_config = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }
    if json_mode:
        base_generation_config["response_mime_type"] = "application/json"

    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    models_to_try = [GEMINI_MODEL] + [m for m in FALLBACK_MODELS if m != GEMINI_MODEL]
    session = await _get_session()

    for model_name in models_to_try:
        generation_config = dict(base_generation_config)
        _apply_thinking_config(generation_config, model_name, thinking_level)

        # MUHIM: Gemini 3.x modellarida maxOutputTokens — javob matni UCHUN
        # emas, balki "thinking" (ichki fikrlash) tokenlari + javob matni
        # BIRGALIKDA sarflaydigan umumiy byudjet. Agar thinking budjetning
        # katta qismini yeb qo'ysa, javob matni yarim gapda kesilib qoladi
        # (finishReason="MAX_TOKENS"). Shu sabab, shunday holat sodir bo'lsa,
        # shu model bilan BIR MARTA kattaroq byudjet bilan qayta urinamiz —
        # bekorga darhol keyingi (kuchsizroq) modelga o'tavermaslik uchun.
        max_attempts_for_model = 2 if _is_gemini3(model_name) else 1

        for attempt in range(max_attempts_for_model):
            payload = {"contents": contents, "generationConfig": generation_config}
            if system_instruction:
                payload["system_instruction"] = {"parts": [{"text": system_instruction}]}

            try:
                async with session.post(
                    _model_url(model_name), json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        logger.warning("Gemini (%s) xato javob: %s - %s", model_name, resp.status, data)
                        break  # keyingi zaxira modelni sinab ko'ramiz
                    candidates = data.get("candidates") or []
                    if not candidates:
                        break
                    finish_reason = candidates[0].get("finishReason")
                    parts = candidates[0].get("content", {}).get("parts", []) or []
                    text = "".join(p.get("text", "") for p in parts).strip()

                    if finish_reason == "MAX_TOKENS" and attempt == 0 and max_attempts_for_model > 1:
                        # Thinking budjetni yeb qo'ygan bo'lishi mumkin — javob
                        # matniga ko'proq joy qoldirib qayta urinamiz.
                        logger.info(
                            "Gemini (%s) MAX_TOKENS bilan kesildi (thinking token'lar "
                            "byudjetni yegan bo'lishi mumkin) — kattaroq byudjet bilan qayta urinilyapti",
                            model_name,
                        )
                        generation_config = dict(generation_config)
                        generation_config["maxOutputTokens"] = min(
                            8192, int(generation_config["maxOutputTokens"] * 2.5) + 500
                        )
                        continue  # shu model bilan yana bir bor urinib ko'ramiz

                    if text:
                        return text
                    break  # bo'sh javob — keyingi zaxira modelga o'tamiz
            except Exception:
                logger.exception("Gemini (%s) so'rovida xatolik yuz berdi", model_name)
                break  # keyingi zaxira modelni sinab ko'ramiz

    # Gemini (asosiy + barcha zaxira modellari) muvaffaqiyatsiz bo'ldi —
    # oxirgi chora sifatida Groq'ni sinab ko'ramiz (agar ulangan bo'lsa).
    if GROQ_ENABLED:
        logger.warning(
            "Gemini'ning barcha modellari ishlamadi — Groq (%s) zaxira "
            "sifatida sinalyapti", GROQ_TEXT_MODEL,
        )
        groq_result = await _call_groq(
            contents, system_instruction=system_instruction,
            temperature=temperature, max_output_tokens=max_output_tokens,
            json_mode=json_mode, timeout=timeout,
        )
        if groq_result:
            return groq_result

    return None  # Gemini va Groq — ikkalasi ham muvaffaqiyatsiz bo'ldi


def _contents_has_image(contents):
    """contents ichida rasm (inline_data) bor-yo'qligini tekshiradi — Groq'da
    matn va vision modellari alohida bo'lgani uchun to'g'ri modelni tanlash
    kerak."""
    for item in contents:
        for part in item.get("parts", []):
            if "inline_data" in part:
                return True
    return False


def _gemini_contents_to_groq_messages(contents, system_instruction=None):
    """Gemini formatidagi contents ({"role", "parts": [...]})ni Groq/OpenAI
    formatidagi messages ({"role", "content": ...})ga o'giradi. Rasm bo'lsa,
    OpenAI-uslubidagi image_url (data URI) blokiga aylantiradi."""
    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})

    for item in contents:
        # Gemini'da AI javobi "model" deb ataladi, OpenAI/Groq'da "assistant"
        role = "assistant" if item.get("role") == "model" else "user"
        parts = item.get("parts", []) or []

        if len(parts) == 1 and "text" in parts[0] and "inline_data" not in parts[0]:
            messages.append({"role": role, "content": parts[0]["text"]})
            continue

        content_blocks = []
        for part in parts:
            if "text" in part:
                content_blocks.append({"type": "text", "text": part["text"]})
            elif "inline_data" in part:
                mime = part["inline_data"].get("mime_type", "image/jpeg")
                data = part["inline_data"].get("data", "")
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{data}"},
                })
        messages.append({"role": role, "content": content_blocks})

    return messages


async def _call_groq(contents, system_instruction=None, temperature=0.7,
                      max_output_tokens=800, json_mode=False, timeout=25):
    """Groq (OpenAI-uslubidagi chat completions) API'ga so'rov yuboradi.
    _call_gemini ichidan ZAXIRA sifatida (yoki GEMINI_API_KEY yo'q bo'lsa
    to'g'ridan-to'g'ri) chaqiriladi. Xatolik bo'lsa None qaytaradi — bu holda
    chaqiruvchi funksiyalar (ask_ai, moderate_comment va h.k.) allaqachon
    xavfsiz "bo'sh natija" bilan ishlashga moslashtirilgan."""
    if not GROQ_ENABLED:
        return None

    has_image = _contents_has_image(contents)
    model_name = GROQ_VISION_MODEL if has_image else GROQ_TEXT_MODEL
    messages = _gemini_contents_to_groq_messages(contents, system_instruction)

    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_output_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
    }

    session = await _get_session()
    try:
        async with session.post(
            GROQ_API_URL, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                logger.warning("Groq (%s) xato javob: %s - %s", model_name, resp.status, data)
                return None
            choices = data.get("choices") or []
            if not choices:
                return None
            text = (choices[0].get("message", {}).get("content") or "").strip()
            return text or None
    except Exception:
        logger.exception("Groq (%s) so'rovida xatolik yuz berdi", model_name)
        return None


async def ask_ai(user_text, history=None, system_instruction=None, catalog_text=None,
                  bot_info_text=None):
    """Erkin suhbat uchun javob qaytaradi.
    history: [("user"|"model", matn), ...] — oldingi xabarlar (ixtiyoriy).
    catalog_text: botdagi haqiqiy animelar ro'yxati (ixtiyoriy) — berilsa,
    AI 'qanaqa anime bor' kabi savollarga OʻYLAB TOPMASDAN, shu roʻyxatga
    asoslanib javob beradi.
    bot_info_text: botning oʻzi haqidagi haqiqiy maʼlumot (boʻlimlar,
    Premium narxlari va h.k.) — berilsa, AI bot haqida soʻralganda
    OʻYLAB TOPMASDAN, shu maʼlumotga asoslanib javob beradi."""
    contents = []
    for role, text in (history or []):
        contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    sys_prompt = system_instruction or (
        "Sen umumiy chatbot emassan — sen aynan 'AniFilm Bot' telegram "
        "botining oʻziga tegishli, shu botning ichida ishlaydigan shaxsiy "
        "AI-yordamchisan. Har bir javobingda oʻzingni shu botning bir "
        "qismi sifatida tut: kerak boʻlganda botning oʻzi (uning "
        "katalogi, boʻlimlari, imkoniyatlari — 🔍 Qidiruv, 📚 Katalog, "
        "tavsiyalar va h.k.) bilan bogʻlab gapir.\n\n"
        "Anime, kino, serial yoki botning oʻzi haqidagi savollarga "
        "oʻzbek tilida toʻliq, mazmunli va foydali javob ber — kerak "
        "boʻlsa misollar, tafsilotlar va tushuntirishlar bilan boy javob "
        "yoz, faqat bir-ikki gap bilan cheklanma. Sayoz yoki umumiy "
        "javoblardan qoch — aniq faktlar, nomlar va tavsiyalar bilan "
        "javob ber.\n\n"
        "Agar savol anime/kino/serial va botning oʻzi bilan UMUMAN "
        "bogʻliq boʻlmasa (masalan matematika, siyosat, boshqa aloqasiz "
        "mavzu), unga qisqagina, xushmuomalalik bilan javob ber-da, "
        "keyin suhbatni botning asosiy mavzusiga — anime/kino tavsiyalari "
        "va katalogiga — qaytar. Bunday aloqasiz savollarga anime "
        "savollari kabi chuqur va batafsil yozib oʻtirma."
    )
    if bot_info_text:
        sys_prompt += (
            "\n\nBot haqida HAQIQIY maʼlumot (bot qanday ishlashi, "
            "boʻlimlari, Premium narxlari va h.k. haqida savol berilsa, "
            "FAQAT shu maʼlumotga tayan — boshqa narsa oʻylab topma, "
            "eskirgan yoki taxminiy narx aytma):\n" + bot_info_text
        )
    if catalog_text:
        sys_prompt += (
            "\n\nBotning HAQIQIY kataloridagi animelar roʻyxati (FAQAT "
            "shular botda mavjud — bu roʻyxatdan tashqari hech qanday "
            "animeni \"bizda bor\" deb aytma, oʻylab ham topma):\n"
            + catalog_text +
            "\n\nAgar foydalanuvchi \"qanaqa anime bor\", \"nima koʻrsam "
            "boʻladi\", \"roʻyxat\" kabi savol bersa — shu roʻyxatdan mavzuga "
            "yoki janrga mos bir nechta nomni aniq aytib ber (ixtiyoriy "
            "ravishda yil/janrini ham qoʻsh), va 🔍 Qidiruv yoki 📚 Katalog "
            "tugmasidan toʻliq roʻyxatni koʻrishni maslahat ber. Agar roʻyxat "
            "boʻsh boʻlsa yoki mos nom topa olmasang, buni rostgoʻylik bilan "
            "ayt — hech qachon mavjud boʻlmagan sarlavha oʻylab topma."
        )

    return await _call_gemini(contents, system_instruction=sys_prompt,
                               temperature=0.85, max_output_tokens=1800,
                               thinking_level="low", is_admin=False)


async def moderate_comment(text):
    """True -> izoh xavfsiz. False -> spam/haqorat/nomaqbul kontent deb topildi.
    AI ishlamasa yoki xatolik bersa, xavfsiz deb hisoblanadi (mavjud
    taqiqlangan-soʻzlar filtri baribir alohida ishlayveradi)."""
    if not text:
        return True
    prompt = (
        "Quyidagi matn — anime saytidagi foydalanuvchi izohi. Agar unda "
        "haqorat, kamsitish, spam, reklama/havola chaqiruvi, nafrat nutqi "
        "yoki boshqa nomaqbul kontent boʻlsa, faqat bitta soʻz bilan \"BAD\" "
        "deb javob ber. Aks holda faqat \"OK\" deb javob ber. Boshqa hech "
        "narsa yozma.\n\nIzoh: " + text[:300]
    )
    result = await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.0, max_output_tokens=20, is_admin=False,
    )
    if result is None:
        return True
    return "BAD" not in result.upper()


async def generate_anime_description(title, genre="", year="", country=""):
    """Admin anime qo'shayotganda tavsifni AI yordamida yaratadi."""
    details = []
    if genre:
        details.append(f"Janr: {genre}")
    if year:
        details.append(f"Yil: {year}")
    if country:
        details.append(f"Davlat: {country}")
    details_text = (" " + " ".join(details) + ".") if details else ""

    prompt = (
        f"'{title}' nomli anime uchun jozibali, 2-3 gapdan iborat qisqa "
        f"tavsif yoz (o'zbek tilida).{details_text} Faqat tavsif matnini "
        f"yoz, sarlavha yoki izoh qo'shma."
    )
    return await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.9, max_output_tokens=450,
    )


async def translate_description_to_uz(text, anime_title=""):
    """AniList/Jikan kabi tashqi manbadan kelgan INGLIZCHA anime tavsifini
    o'zbek tiliga ANIQ (so'zma-so'zga yaqin) tarjima qiladi.

    MUHIM: bu funksiya generate_anime_description'dan farqli — u yangi
    tavsif "ijod qiladi", bu esa mavjud matnni FAQAT tarjima qiladi.
    Shu sabab temperature juda past (0.2) qo'yilgan — AI o'zidan gap
    qo'shib yubormasligi, faktlarni o'zgartirmasligi uchun."""
    if not text or not text.strip():
        return ""
    prompt = (
        "Quyidagi matn anime tavsifi (ingliz tilida). Uni o'zbek tiliga "
        "ANIQ va TO'G'RI tarjima qil. Qoidalar:\n"
        "1. Hech qanday yangi ma'lumot qo'shma, hech narsani o'zgartirma "
        "yoki qisqartirma — faqat asl matnni tarjima qil.\n"
        "2. Anime, personaj, joy nomlarini (masalan shaxs ismlari) "
        "tarjima qilma — asl holida qoldir.\n"
        "3. HTML teglari (<br>, <i>, <b> va h.k.) bo'lsa, ularni olib "
        "tashla, faqat toza matn qoldir.\n"
        "4. Faqat tarjima qilingan matnni yoz — kirish so'zi, izoh yoki "
        "\"Mana tarjima:\" kabi qo'shimcha yozma.\n\n"
        f"Anime nomi (kontekst uchun, tarjima qilma): {anime_title or '—'}\n\n"
        f"Tavsif:\n{text[:2000]}"
    )
    result = await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.2, max_output_tokens=900, thinking_level="low",
    )
    return (result or "").strip()


async def generate_recommendation_reason(anime_title, user_context):
    """Webapp'da 'Nega tavsiya qilindi?' uchun bitta qisqa (5-10 so'zli)
    sabab yozadi. AI ishlamasa yoki xato bersa, None qaytaradi — chaqiruvchi
    tomon bu holda umumiy sabab ko'rsatishi kerak."""
    prompt = (
        f"Foydalanuvchi haqida: {user_context}\n\n"
        f"'{anime_title}' nomli anime shu foydalanuvchiga nega tavsiya "
        "qilinganini FAQAT 5-10 so'zdan iborat, o'zbek tilida, jozibali "
        "qisqa jumla bilan tushuntir. Faqat shu jumlani yoz, boshqa hech "
        "narsa qo'shma (tirnoq, sarlavha yo'q)."
    )
    return await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.7, max_output_tokens=180, is_admin=False,
    )


async def generate_episode_announcement(anime_title, episode_number, genre=""):
    """Yangi qism chiqqanda kanalga/foydalanuvchilarga yuboriladigan qisqa,
    qiziqarli e'lon matnini yozadi (emoji bilan, 2-3 gap)."""
    genre_text = f" Janr: {genre}." if genre else ""
    prompt = (
        f"'{anime_title}' anime'sining {episode_number}-qismi endigina "
        f"qo'shildi.{genre_text} Buni e'lon qiluvchi qisqa (2-3 gap), "
        "qiziqtiruvchi, mos joylarda emoji ishlatilgan xabar matnini "
        "o'zbek tilida yoz. Faqat xabar matnini yoz, izoh qo'shma."
    )
    return await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.9, max_output_tokens=450,
    )


async def generate_ai_invite_message():
    """Botning AI-yordamchisidan hali foydalanmagan userlarga yuboriladigan
    taklif xabarini yozadi — ularni /ai chatga yoki tavsiya funksiyasini
    sinab ko'rishga undaydi."""
    prompt = (
        "Anime Telegram botida 'AI-yordamchi' funksiyasi bor: foydalanuvchi "
        "u bilan suhbatlashishi va o'ziga mos anime tavsiyalarini so'rashi "
        "mumkin. Bu funksiyadan hali foydalanmagan foydalanuvchilarga "
        "yuboriladigan, uni sinab ko'rishga undovchi qisqa (3-4 gap), "
        "samimiy va qiziqtiruvchi xabar yoz (o'zbek tilida, mos joyda "
        "emoji bilan). Faqat xabar matnini yoz."
    )
    return await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.9, max_output_tokens=450,
    )


async def answer_comment_question(question_text, anime_title, anime_description, total_episodes, available_episodes, status):
    """Foydalanuvchi izohda savol bersa (masalan 'qachon davomi chiqadi'),
    shu anime haqidagi mavjud ma'lumotlarga asoslanib qisqa javob yozadi.
    AI FAQAT berilgan ma'lumotlar asosida javob berishi, o'zidan foydalanuvchi
    bilmaydigan narsa o'ylab topmasligi kerak (masalan aniq sana)."""
    prompt = (
        f"Anime: {anime_title}\nHolati: {status}\nTavsif: {anime_description or 'yoq'}\n"
        f"Jami rejalashtirilgan qism: {total_episodes or 'nomaʼlum'}\n"
        f"Hozircha yuklangan qismlar: {available_episodes}\n\n"
        f"Foydalanuvchi izohi (savol): \"{question_text}\"\n\n"
        "Shu ma'lumotlarga asoslanib, foydalanuvchiga qisqa (1-2 gap), o'zbek "
        "tilida javob yoz. Agar javob uchun yetarli ma'lumot bo'lmasa (masalan "
        "aniq chiqish sanasi berilmagan), aniq sana o'ylab topma — buning "
        "o'rniga 'hozircha aniq sana e'lon qilinmagan' kabi rostgo'y javob "
        "ber. Faqat javob matnini yoz."
    )
    return await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.4, max_output_tokens=350, is_admin=False,
    )


def looks_like_question(text):
    """Izoh savolga o'xshaydimi — oddiy kalit so'z/belgi tekshiruvi (AI
    chaqirishdan oldingi arzon filtr, bekorga API so'rov yubormaslik uchun)."""
    if not text:
        return False
    t = text.lower()
    if "?" in t:
        return True
    keywords = ["qachon", "nechanchi", "necha qism", "davomi", "yangi qism",
                "qolgani", "tugaydimi", "bormi", "chiqadimi"]
    return any(k in t for k in keywords)


async def analyze_stats(stats_context):
    """Admin uchun statistika raqamlarini so'z bilan izohlaydi — sabab va
    tavsiya bilan qisqa tahlil (3-5 gap)."""
    prompt = (
        "Quyida anime Telegram bot/webapp uchun statistik ma'lumotlar berilgan:\n\n"
        f"{stats_context}\n\n"
        "Shu raqamlarga asoslanib, admin uchun qisqa (3-5 gap) tahlil yoz: "
        "nima yaxshi ketyapti, nimaga e'tibor berish kerak, va bitta amaliy "
        "tavsiya ber. O'zbek tilida, aniq va foydali yoz — umumiy gaplardan "
        "qoch. Faqat tahlil matnini yoz."
    )
    return await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.6, max_output_tokens=700,
    )


async def suggest_anime_metadata(title):
    """Admin faqat nom kiritganda, janr/davlat/tavsifni AI taxmin qiladi.
    Natija topilmasa yoki noaniq bo'lsa bo'sh qiymatlar qaytariladi — admin
    baribir qo'lda tuzatishi mumkin."""
    prompt = (
        f"'{title}' nomli anime/film haqida bilganingcha ma'lumot ber. "
        'FAQAT quyidagi JSON formatda javob ber, boshqa hech narsa yozma: '
        '{"genre": "...", "country": "...", "year": "...", "description": "..."} '
        "description 2-3 gapdan iborat, o'zbek tilida bo'lsin. Agar animeni "
        "aniq bilmasang yoki noaniq bo'lsa, mos maydonlarni bo'sh (\"\") qoldir "
        "— hech narsa o'ylab topma."
    )
    result = await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.3, max_output_tokens=550, json_mode=True,
    )
    if not result:
        return {}
    try:
        data = json.loads(result)
        return {
            "genre": str(data.get("genre") or "").strip(),
            "country": str(data.get("country") or "").strip(),
            "year": str(data.get("year") or "").strip(),
            "description": str(data.get("description") or "").strip(),
        }
    except Exception:
        return {}


async def suggest_comment_replies(comment_text, anime_title="", anime_description=""):
    """Admin izohga javob yozayotganda tanlashi uchun 2-3 ta tayyor javob
    varianti taklif qiladi. AI ishlamasa yoki xato bersa, bo'sh ro'yxat
    qaytariladi — admin baribir qo'lda yozishi mumkin."""
    context = f"Anime: {anime_title}." if anime_title else ""
    if anime_description:
        context += f" Tavsif: {anime_description}"
    prompt = (
        f"{context}\n\nFoydalanuvchi anime ostiga shu izohni qoldirdi: "
        f"\"{comment_text}\"\n\n"
        "Anime kanali admini nomidan shu izohga javob sifatida yozish "
        "mumkin bo'lgan 2-3 ta QISQA (1-2 gap), turli ohangdagi (masalan: "
        "samimiy/rasmiy/hazil-mutoyiba aralash) javob variantini o'zbek "
        "tilida taklif qil. FAQAT quyidagi JSON formatda javob ber, boshqa "
        "hech narsa yozma: {\"replies\": [\"...\", \"...\", \"...\"]}"
    )
    result = await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.8, max_output_tokens=500, json_mode=True,
        thinking_level="low",
    )
    if not result:
        return []
    try:
        data = json.loads(result)
        replies = data.get("replies") or []
        return [str(r).strip() for r in replies if str(r).strip()][:3]
    except Exception:
        return []


async def generate_comeback_message(user_context):
    """Uzoq vaqtdan beri botga kirmagan foydalanuvchiga yuboriladigan
    shaxsiylashtirilgan 'qaytib kel' xabarini yozadi. user_context —
    foydalanuvchining sevimli animelari haqida qisqa matn (bo'lishi
    shart emas)."""
    fav_text = (
        f"Foydalanuvchining sevimli animelari: {user_context}."
        if user_context else
        "Foydalanuvchining sevimli animelari haqida ma'lumot yo'q."
    )
    prompt = (
        "Anime Telegram botida foydalanuvchi ancha vaqtdan beri ko'rinmayapti. "
        f"{fav_text}\n\n"
        "Uni botga qaytishga undaydigan, qisqa (2-3 gap), samimiy va "
        "sog'inch ohangidagi xabar yoz (o'zbek tilida, mos joyda emoji "
        "bilan). Agar sevimli animelari berilgan bo'lsa, ularga ishora "
        "qil (masalan yangi qismlar chiqqan bo'lishi mumkinligini "
        "eslat), lekin aniq raqam yoki sana o'ylab topma. Faqat xabar "
        "matnini yoz."
    )
    return await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.9, max_output_tokens=350, thinking_level="low",
    )


async def generate_premium_pitch_message(user_context, plan_summary=""):
    """FAOL, lekin hali Premium sotib olmagan foydalanuvchiga yuboriladigan
    shaxsiylashtirilgan sotuv taklifini yozadi. generate_comeback_message'dan
    farqi — bu foydalanuvchi allaqachon botdan faol foydalanmoqda, shuning
    uchun ohang "qaytib kel" emas, balki "sen bunga loyiqsan, keyingi qadam
    shu" ruhida bo'lishi kerak. `plan_summary` — narxlar/imkoniyatlar haqida
    qisqa matn (masalan "1 oy - 15000 so'm, reklamasiz + eksklyuziv
    qismlar"), bo'lmasa umumiy tarzda yozadi."""
    fav_text = (
        f"Foydalanuvchining sevimli animelari: {user_context}."
        if user_context else
        "Foydalanuvchining sevimli animelari haqida ma'lumot yo'q."
    )
    plan_text = f"\n\nPremium imkoniyatlari: {plan_summary}." if plan_summary else ""
    prompt = (
        "Anime Telegram botida foydalanuvchi FAOL (tez-tez kirib turadi), "
        f"lekin hali Premium sotib olmagan. {fav_text}{plan_text}\n\n"
        "Unga Premiumga o'tishni taklif qiluvchi, qisqa (2-3 gap), "
        "do'stona va suhbatdosh ohangdagi xabar yoz (o'zbek tilida, mos "
        "joyda emoji bilan). Bosim o'tkazma yoki 'chegirma tugayapti' kabi "
        "yolg'on shoshilinchlik yaratma — buning o'rniga uning faolligini "
        "e'tirof et va Premium unga qanday qo'shimcha qulaylik berishini "
        "(masalan reklamasiz tomosha, eksklyuziv qismlarga tezroq kirish) "
        "tabiiy tarzda eslat. Agar sevimli animelari berilgan bo'lsa, "
        "ularga ishora qil. Faqat xabar matnini yoz, aniq raqam/muddat "
        "o'ylab topma (plan_summary'da berilmagan bo'lsa)."
    )
    return await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.9, max_output_tokens=350, thinking_level="low",
    )


async def generate_payment_rejection_message(reason, suggest_retry=True):
    """Admin to'lovni rad etganda foydalanuvchiga yuboriladigan xabarni
    yozadi. `reason` — admin tanlagan/yozgan rad etish sababi (masalan
    "summa mos emas", "chek soxta/tahrirlangan", "sifatsiz rasm — o'qib
    bo'lmadi", yoki admin o'z so'zi bilan yozgan erkin matn). AI shu
    sababni foydalanuvchiga xafa qilmaydigan, xushmuomala tilga o'giradi
    — sababni o'zgartirmaydi yoki yumshatib "yashirmaydi", faqat ohangini
    silliqlaydi. `suggest_retry=True` bo'lsa, xabar oxirida to'g'ri
    chek bilan qayta urinishga taklif qo'shiladi (masalan sabab "soxta
    chek" bo'lsa buni False qilib chaqirish tavsiya etiladi — bunday holda
    qayta urinish o'rniga admin bilan bog'lanishga yo'naltirilgan matn
    yaxshiroq)."""
    retry_text = (
        "Xabar oxirida foydalanuvchini TO'G'RI/ANIQ chek skrinshoti bilan "
        "qayta urinib ko'rishga xushmuomalalik bilan taklif qil."
        if suggest_retry else
        "Qayta urinishni taklif qilma — buning o'rniga admin bilan "
        "to'g'ridan-to'g'ri bog'lanishni taklif qil."
    )
    prompt = (
        "Anime Telegram bot/webapp'ida foydalanuvchining Premium uchun "
        f"yuborgan to'lov chekini admin rad etdi. Rad etish sababi: "
        f"\"{reason}\".\n\n"
        "Shu sababga asoslanib, foydalanuvchiga yuboriladigan QISQA "
        "(2-3 gap, o'zbek tilida) xabar yoz. Ohang: xushmuomala, "
        "hurmatli, lekin ayblovchi yoki qattiqqo'l emas — sababni ochiq "
        "va tushunarli qilib ayt (yumshatib, noaniq qilib yozma), lekin "
        "foydalanuvchini past ko'rmagan, tushunuvchan tilda. "
        f"{retry_text} Faqat xabar matnini yoz, sarlavha yoki tirnoq "
        "belgisiz."
    )
    result = await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.6, max_output_tokens=300, thinking_level="low",
    )
    if result:
        return result
    # AI ishlamasa — xavfsiz zaxira matn (sababni baribir ko'rsatadi)
    fallback = f"❌ To'lovingiz tasdiqlanmadi. Sabab: {reason}."
    if suggest_retry:
        fallback += " Iltimos, to'g'ri chek bilan qayta urinib ko'ring."
    else:
        fallback += " Savol bo'lsa, admin bilan bog'laning."
    return fallback


async def moderate_image(image_base64, mime_type="image/jpeg"):
    """Admin yuklagan rasmni (anime posteri, banner, sponsor rasmi va h.k.)
    ochiq foydalanuvchilarga ko'rsatishdan OLDIN NSFW/nomaqbul kontentga
    tekshiradi. True -> rasm xavfsiz. False -> nomaqbul (yalang'ochlik,
    haddan tashqari zo'ravonlik/qon, ekstremistik ramzlar va h.k.) deb
    topildi. AI ishlamasa yoki javob bera olmasa, XAVFSIZ deb hisoblanadi
    (mavjud admin nazorati baribir asosiy himoya bo'lib qoladi — bu faqat
    qo'shimcha filtr, admin oqimini butunlay to'xtatib qo'ymasligi kerak)."""
    if not AI_ENABLED:
        return True, ""
    prompt = (
        "Bu rasm anime saytida OMMAVIY ko'rsatiladi (poster yoki banner "
        "sifatida). Rasmda quyidagilardan biri bor-yo'qligini tekshir: "
        "yalang'ochlik yoki jinsiy kontent, haddan tashqari qon/zo'ravonlik, "
        "ekstremistik/nafrat ramzlari, yoki boshqa umuman nomaqbul kontent "
        "(oddiy anime uslubidagi jang sahnalari yoki illyustratsiyalar "
        "MUAMMO EMAS — faqat ANIQ nomaqbul bo'lsa belgila). FAQAT quyidagi "
        'JSON formatda javob ber: {"safe": true/false, "reason": "..."} '
        'safe=true bo\'lsa reason bo\'sh ("") qoldirilsin.'
    )
    contents = [{
        "role": "user",
        "parts": [
            {"inline_data": {"mime_type": mime_type, "data": image_base64}},
            {"text": prompt},
        ],
    }]
    result = await _call_gemini(contents, temperature=0.0, max_output_tokens=150, json_mode=True)
    if not result:
        return True, ""
    try:
        data = json.loads(result)
        if data.get("safe") is False:
            return False, str(data.get("reason") or "").strip()
        return True, ""
    except Exception:
        return True, ""


async def verify_anime_photo_title(image_base64, title, mime_type="image/jpeg"):
    """Admin yangi anime qo'shayotganda AniList'dan avtomatik to'ldirishdan
    OLDIN chaqiriladi: admin yuborgan POSTER RASM bilan admin yozgan NOM
    (title) haqiqatan bir xil animega tegishlimi — shuni AI'ning ko'rish
    (vision) qobiliyati bilan tekshiradi. Bu AniList qidiruvi noto'g'ri/
    boshqa animeni topib qo'yishining oldini olish uchun kerak (masalan
    nom noto'g'ri yozilgan yoki bir nechta anime bir xil nomga ega bo'lsa).

    Qaytaradi: {"matches": bool, "detected_title": str, "confidence": str}
      - matches=True  -> rasm va nom mos, AniList qidiruvini "title" bilan
        davom ettirish xavfsiz.
      - matches=False -> AI rasmda boshqa anime ko'rgandek va bu haqda
        "detected_title" orqali o'z taxminini beradi (bo'sh bo'lishi ham
        mumkin — rasmni umuman tanimasa).
      - AI ishlamasa/xato bersa yoki aniq xulosaga kela olmasa,
        matches=True qaytariladi (ya'ni oqim to'xtatilmaydi — bu faqat
        QO'SHIMCHA ogohlantirish, admin baribir yakuniy qarorni o'zi
        qabul qiladi)."""
    if not AI_ENABLED:
        return {"matches": True, "detected_title": "", "confidence": ""}
    prompt = (
        f"Admin bu anime posterini '{title}' nomi bilan qo'shmoqchi.\n\n"
        "Rasmga qarab, bu haqiqatan ham shu nomdagi anime posteri/kadri "
        "ekanligini tekshir. Agar rasmda personajlar yoki uslub aynan shu "
        "animega mos kelmasa (masalan boshqa animening posteri "
        "yuborilgan bo'lsa), buni aniq belgila.\n\n"
        "Agar rasmni aniq tanimasang yoki ishonchli xulosa qila olmasang, "
        '"matches": true qaytar (shubhali holatda ogohlantirmaslik '
        "yaxshiroq, chunki noto'g'ri ogohlantirish adminni chalg'itadi).\n\n"
        'FAQAT quyidagi JSON formatda javob ber: {"matches": true/false, '
        '"detected_title": "agar matches=false bo\'lsa, rasmda ko\'ringan '
        'haqiqiy anime nomi (bilmasang bo\'sh \\"\\")", '
        '"confidence": "low/medium/high"}'
    )
    contents = [{
        "role": "user",
        "parts": [
            {"inline_data": {"mime_type": mime_type, "data": image_base64}},
            {"text": prompt},
        ],
    }]
    result = await _call_gemini(
        contents, temperature=0.0, max_output_tokens=200, json_mode=True,
        thinking_level="low",
    )
    if not result:
        return {"matches": True, "detected_title": "", "confidence": ""}
    try:
        data = json.loads(result)
        return {
            "matches": bool(data.get("matches", True)),
            "detected_title": str(data.get("detected_title") or "").strip(),
            "confidence": str(data.get("confidence") or "").strip(),
        }
    except Exception:
        return {"matches": True, "detected_title": "", "confidence": ""}


async def identify_anime_from_image_tracemoe(image_bytes):
    """trace.moe orqali rasmdan (poster/kadr) anime nomini aniqlaydi.
    Bepul, API kalit talab qilmaydi. Ishonch darajasi past bo'lsa
    None qaytaradi — shunda chaqiruvchi tomon Gemini vision'ga
    (taxminga) o'tishi mumkin."""
    session = await _get_session()
    try:
        async with session.post(
            "https://api.trace.moe/search?anilistInfo",
            data=image_bytes,
            headers={"Content-Type": "image/jpeg"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        logger.exception("[identify_anime_from_image_tracemoe] so'rov xato")
        return None

    results = data.get("result") or []
    if not results:
        return None
    best = results[0]
    similarity = best.get("similarity", 0)
    if similarity < 0.87:  # past ishonch — AniList taxminini rad etamiz
        return None

    anilist = best.get("anilist") or {}
    title = (anilist.get("title") or {}).get("romaji") or \
            (anilist.get("title") or {}).get("english") or ""
    if not title:
        return None
    return {
        "title": title,
        "anilist_id": anilist.get("id"),
        "similarity": round(similarity * 100, 1),
    }


async def guess_anime_title_from_image(image_base64, mime_type="image/jpeg"):
    """trace.moe rasmni tanimaganda (past ishonch yoki umuman topilmaganda)
    ZAXIRA sifatida chaqiriladi: AI'ning vision qobiliyati bilan posterga
    qarab anime nomini TAXMIN qiladi. Bu — trace.moe kabi aniq bazaga
    tayanmagani uchun — noto'g'ri yoki o'ylab topilgan nom qaytarishi
    mumkin, shu sabab natija HAR DOIM admin'ga "AI taxmini, tekshiring"
    deb aniq belgilab ko'rsatilishi SHART (chaqiruvchi tomon).

    Qaytaradi: {"title": str, "year": str, "confidence": "low/medium/high"}
      Hech narsa aniqlay olmasa — barcha maydonlar bo'sh, confidence="low"."""
    empty = {"title": "", "year": "", "confidence": "low"}
    if not AI_ENABLED:
        return empty

    prompt = (
        "Bu rasm — anime posteri yoki kadri. Rasmga diqqat bilan qarab, "
        "bu qaysi anime ekanini aniqlashga harakat qil (personajlar "
        "dizayni, uslub, matn/logotip agar rasmda ko'rinsa — shularga "
        "tayan).\n\n"
        "MUHIM: Agar ANIQ ishonchli bo'lmasang yoki bu anime senga "
        "notanish bo'lsa, nom o'ylab topma — bo'sh (\"\") qoldir. "
        "Noto'g'ri taxmin qilishdan ko'ra \"bilmayman\" deyish "
        "YAXSHIROQ.\n\n"
        'FAQAT quyidagi JSON formatda javob ber: {"title": "aniq bo\'lsa '
        'anime nomi, aks holda \\"\\"", "year": "chiqqan yili bilsang, '
        'aks holda \\"\\"", "confidence": "low/medium/high"}'
    )
    contents = [{
        "role": "user",
        "parts": [
            {"inline_data": {"mime_type": mime_type, "data": image_base64}},
            {"text": prompt},
        ],
    }]
    result = await _call_gemini(
        contents, temperature=0.0, max_output_tokens=150, json_mode=True,
        thinking_level="low",
    )
    if not result:
        return empty
    try:
        data = json.loads(result)
        title = str(data.get("title") or "").strip()
        if not title:
            return empty
        return {
            "title": title,
            "year": str(data.get("year") or "").strip(),
            "confidence": str(data.get("confidence") or "low").strip().lower(),
        }
    except Exception:
        return empty


async def find_duplicate_anime(new_title, candidate_titles):
    """Admin yangi anime qo'shayotganda, DB'da nomi o'xshash animelar
    topilsa, shulardan qay biri HAQIQATDA bir xil anime ekanini (masalan
    faqat yozilishi boshqacha yoki fasl/qism raqami qo'shilgan) AI orqali
    tekshiradi. Aniq bo'lmasa yoki AI ishlamasa, dublikat topilmagan deb
    hisoblanadi (admin baribir ro'yxatni o'zi ko'radi)."""
    if not candidate_titles:
        return None
    candidates_text = "\n".join(f"- {t}" for t in candidate_titles)
    prompt = (
        f"Botga yangi anime qo'shilmoqchi: \"{new_title}\"\n\n"
        f"Bazada allaqachon mavjud, nomi o'xshash animelar:\n{candidates_text}\n\n"
        "Shulardan biri yangi qo'shilayotgan anime bilan AYNAN BIR XIL "
        "(masalan faqat imlo farqi, translit farqi yoki bo'sh joy/tinish "
        "belgisi farqi bor) ekanligini tekshir. Fasllari yoki qismlari "
        "boshqa bo'lsa (masalan '2-fasl', 'Season 2'), bu dublikat "
        "HISOBLANMAYDI. FAQAT quyidagi JSON formatda javob ber, boshqa "
        "hech narsa yozma: {\"is_duplicate\": true/false, \"matched_title\": "
        "\"...\" yoki \"\"}"
    )
    result = await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.0, max_output_tokens=200, json_mode=True,
        thinking_level="minimal",
    )
    if not result:
        return None
    try:
        data = json.loads(result)
        if data.get("is_duplicate") and str(data.get("matched_title") or "").strip():
            return str(data["matched_title"]).strip()
        return None
    except Exception:
        return None


async def extract_payment_amount(image_base64, mime_type="image/jpeg"):
    """To'lov skrinshotidan summani (va agar ko'rinsa, karta oxirgi 4 raqamini)
    o'qiydi — adminga tasdiqlashni tezlashtirish uchun, YAKUNIY qaror baribir
    admin tomonidan qabul qilinadi (bu faqat yordamchi taxmin).
    Shu bir xil (qo'shimcha so'rov yubormasdan, tezlik uchun) vizual
    tekshiruvda skrinshot QALBAKI/TAHRIRLANGAN bo'lishi mumkinligini
    ko'rsatuvchi belgilarni ham baholaydi (mos kelmagan shrift, notekis
    joylashuv/piksellashuv, bank ilovasi dizayniga mos kelmaslik va h.k.)
    va natijaga `suspicious`/`suspicion_reason` maydonlarini qo'shadi.
    MUHIM: bu ham faqat YORDAMCHI signal — yakuniy qaror admin qo'lida
    qoladi, chunki AI xato taxmin qilishi (ayniqsa siqilgan/pastroq
    sifatli skrinshotlarda) mumkin."""
    if not AI_ENABLED:
        return None
    prompt = (
        "Bu to'lov cheki/skrinshoti. Undagi PUL SUMMASINI (raqam, valyuta "
        "bilan) top. Agar ko'rinib tursa, karta raqamining oxirgi 4 "
        "raqamini ham top.\n\n"
        "Shuningdek, skrinshot QALBAKI/TAHRIRLANGAN (masalan Photoshop yoki "
        "boshqa tahrirlash ilovasida raqamlar/matn o'zgartirilgan) bo'lishi "
        "mumkinligini ko'rsatuvchi ANIQ vizual belgilarni tekshir: matn "
        "atrofidagi mos kelmagan shrift/o'lcham/rang, notekis fon yoki "
        "piksellashuv chegaralari, ustma-ust qo'yilgan/joylashuvi noto'g'ri "
        "matn qatorlari, bank ilovasi interfeysiga (tugmalar, joylashuv, "
        "shrift uslubi) mos kelmaydigan elementlar, yoki mantiqsiz "
        "sana/vaqt/raqam formati. E'tibor bering: siqilgan, pastroq "
        "sifatli yoki qorong'i skrinshot BU YOLG'ON belgi EMAS — faqat "
        "ANIQ tahrirlash izlari ko'rinsa shubhali deb belgila, taxmin "
        "qilma.\n\n"
        "FAQAT quyidagi JSON formatda javob ber: "
        '{"amount": "...", "card_last4": "...", "suspicious": true/false, '
        '"suspicion_reason": "..."} Aniq o\'qiy olmasangan maydonni bo\'sh '
        '("") qoldir. suspicious=false bo\'lsa suspicion_reason ham bo\'sh '
        'qoldirilsin.'
    )
    contents = [{
        "role": "user",
        "parts": [
            {"inline_data": {"mime_type": mime_type, "data": image_base64}},
            {"text": prompt},
        ],
    }]
    result = await _call_gemini(contents, temperature=0.0, max_output_tokens=250, json_mode=True)
    if not result:
        return None
    try:
        data = json.loads(result)
        return {
            "amount": str(data.get("amount") or "").strip(),
            "card_last4": str(data.get("card_last4") or "").strip(),
            "suspicious": bool(data.get("suspicious")),
            "suspicion_reason": str(data.get("suspicion_reason") or "").strip(),
        }
    except Exception:
        return None


async def is_spoiler(text, anime_title=""):
    """True -> izoh spoiler (syujetni ochib beruvchi) deb topildi."""
    if not text:
        return False
    prompt = (
        f"Quyidagi matn '{anime_title}' anime'siga yozilgan izoh. Agar unda "
        "syujetni ochib beruvchi (kimdir o'ladi, kim kim ekanligi, oxiri "
        "qanday tugashi va h.k.) spoiler bo'lsa, faqat 'SPOILER' deb javob "
        "ber. Aks holda faqat 'OK' deb javob ber. Boshqa hech narsa yozma.\n\n"
        f"Izoh: {text[:300]}"
    )
    result = await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.0, max_output_tokens=20, is_admin=False,
    )
    if result is None:
        return False
    return "SPOILER" in result.upper()


async def generate_weekly_report(stats_context):
    """Haftalik AI-tahlil xabari uchun — analyze_stats bilan bir xil, lekin
    haftalik davrga mos so'zlash bilan (kengroq, 4-6 gap)."""
    prompt = (
        "Quyida anime Telegram bot/webapp uchun oxirgi haftalik statistik "
        f"ma'lumotlar berilgan:\n\n{stats_context}\n\n"
        "Admin uchun haftalik qisqa hisobot yoz (4-6 gap, o'zbek tilida): "
        "asosiy trendlar, nima yaxshi/yomon ketyapti, va keyingi hafta uchun "
        "1-2 amaliy tavsiya. Faqat hisobot matnini yoz."
    )
    return await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.6, max_output_tokens=900,
    )


async def analyze_comment_trends(comments_context):
    """Haftalik hisobotga QO'SHIMCHA bo'lim — oxirgi hafta izohlariga qarab
    qaysi anime(lar) haqida eng ko'p SHIKOYAT (past sifat, tarjima xatosi,
    reklama ko'pligi va h.k.) va qaysi(lar) haqida eng ko'p MAQTOV borligini
    aniqlaydi. `comments_context` — har bir anime nomi va unga yozilgan
    izohlar namunasidan iborat matn (tayyorlovchi tomon bazadan yig'ib
    beradi). Izohlar yetarli bo'lmasa yoki AI aniq xulosa chiqara olmasa,
    None qaytaradi (bo'sh/asossiz hisobot yozib chalkashtirmaslik uchun)."""
    if not (comments_context or "").strip():
        return None
    prompt = (
        "Quyida anime Telegram bot/webapp'ida oxirgi hafta ichida turli "
        f"animelarga yozilgan foydalanuvchi izohlari berilgan (anime nomi "
        f"bo'yicha guruhlangan):\n\n{comments_context}\n\n"
        "Shu izohlarga asoslanib admin uchun QISQA (3-5 gap, o'zbek tilida) "
        "tahlil yoz: qaysi anime(lar) haqida eng ko'p SHIKOYAT/salbiy fikr "
        "bor va NIMA sababdan (masalan past video sifat, tarjima xatosi, "
        "qismlar yetishmasligi, reklama ko'pligi), qaysi anime(lar) haqida "
        "eng ko'p MAQTOV/ijobiy fikr bor. FAQAT izohlarda HAQIQATDA "
        "ko'ringan naqllarga tayan — hech narsa o'ylab topma. Agar aniq "
        "trend ko'rinmasa (izohlar juda kam yoki neytral), shuni ochiq "
        "yoz. Faqat tahlil matnini yoz, sarlavha qo'ymasdan."
    )
    return await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.3, max_output_tokens=500, thinking_level="low",
    )


async def pick_anime_by_mood(mood_text, catalog_text, max_picks=3):
    """Foydalanuvchi kayfiyati/xohishini erkin matn bilan yozganda (masalan
    'kayfiyatim yoq, kulgili narsa korgim kelyapti'), katalogdan mos
    animelarni tanlaydi. recommend_anime_ids bilan bir xil qat'iy qoidaga
    amal qiladi: FAQAT berilgan ID'lardan tanlaydi."""
    prompt = (
        "Quyida anime katalogi berilgan:\n\n" + catalog_text + "\n\n"
        f"Foydalanuvchining hozirgi kayfiyati/xohishi: \"{mood_text}\"\n\n"
        f"Shu kayfiyatga eng mos keladigan {max_picks} tagacha anime tanla. "
        "FAQAT yuqoridagi ro'yxatda mavjud ID'lardan foydalan, hech qanday "
        "yangi ID o'ylab topma. Faqat quyidagi JSON formatda javob ber: "
        '{"ids": [id1, id2, ...]}'
    )
    result = await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.6, max_output_tokens=400, json_mode=True, is_admin=False,
    )
    if not result:
        return []
    try:
        data = json.loads(result)
        ids = data.get("ids", [])
        out = []
        for i in ids:
            try:
                out.append(int(i))
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return []


async def search_anime_ids(query, catalog_text, max_picks=10):
    """Webapp qidiruv qutisi uchun — foydalanuvchi nom bo'yicha emas, erkin
    tavsif/janr bo'yicha yozsa ham ('kulgili, qisqa, 2024-yilgi') mos
    animelarni topadi. FAQAT berilgan ID'lardan tanlaydi."""
    prompt = (
        "Quyida anime katalogi berilgan:\n\n" + catalog_text + "\n\n"
        f"Foydalanuvchi qidiruv soʻrovi: \"{query}\"\n\n"
        f"Shu soʻrovga eng mos keladigan {max_picks} tagacha anime tanla — "
        "nom mos kelmasa ham, tavsif/janr/yil boʻyicha moslikka qara. FAQAT "
        "yuqoridagi roʻyxatda mavjud ID'lardan foydalan, yangi ID oʻylab "
        "topma. Faqat quyidagi JSON formatda javob ber: "
        '{"ids": [id1, id2, ...]}'
    )
    result = await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.4, max_output_tokens=450, json_mode=True, is_admin=False,
    )
    if not result:
        return []
    try:
        data = json.loads(result)
        ids = data.get("ids", [])
        out = []
        for i in ids:
            try:
                out.append(int(i))
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return []


async def chat_about_anime(question, anime_title, anime_description, history=None):
    """Webapp'dagi anime detail sahifasidagi mini-chat uchun — shu anime
    haqida SPOYLERSIZ javob beradi (syujet oxiri/muhim burilishlarni
    ochmaydi)."""
    contents = []
    for role, text in (history or []):
        contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": question}]})
    sys_prompt = (
        f"Sen '{anime_title}' anime'si haqidagi AI-yordamchisan. "
        f"Tavsif: {anime_description or 'yoq'}\n\n"
        "Foydalanuvchi savollariga o'zbek tilida, qisqa va foydali javob "
        "ber. MUHIM: syujetning muhim burilishlari, oxiri yoki kim "
        "o'lishi/kim kim ekanligi kabi SPOYLER bo'lishi mumkin bo'lgan "
        "narsalarni OCHIB BERMA — agar foydalanuvchi aynan shuni so'rasa, "
        "spoyler berishdan xushmuomalalik bilan bosh tort va buning o'rniga "
        "umumiy, spoylersiz tavsif ber."
    )
    return await _call_gemini(contents, system_instruction=sys_prompt,
                               temperature=0.7, max_output_tokens=700,
                               thinking_level="low", is_admin=False)


async def recommend_anime_ids(catalog_text, user_context, max_picks=4):
    """Berilgan katalog matnidan (ID: N | Nom: ... formatida) foydalanuvchiga
    eng mos anime ID'larini tanlaydi. Faqat berilgan ID'lar orasidan tanlashi
    SHART qilib so'raladi — shu bilan AI mavjud bo'lmagan anime o'ylab
    chiqarishining oldi olinadi (natijalar baribir qo'ng'iroq qiluvchi
    tomonidan haqiqiy ID'lar bilan tekshiriladi)."""
    prompt = (
        "Quyida anime katalogi berilgan:\n\n" + catalog_text + "\n\n"
        f"Foydalanuvchi haqida ma'lumot: {user_context}\n\n"
        f"Shu foydalanuvchiga eng mos keladigan {max_picks} tagacha anime "
        "tanla. FAQAT yuqoridagi ro'yxatda mavjud ID'lardan foydalan, "
        "hech qanday yangi ID o'ylab topma. Faqat quyidagi JSON formatda "
        'javob ber, boshqa hech narsa yozma: {"ids": [id1, id2, ...]}'
    )
    result = await _call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        temperature=0.4, max_output_tokens=400, json_mode=True, is_admin=False,
    )
    if not result:
        return []
    try:
        data = json.loads(result)
        ids = data.get("ids", [])
        out = []
        for i in ids:
            try:
                out.append(int(i))
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return []
