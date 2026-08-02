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

AI_ENABLED = bool(GEMINI_API_KEY)

if not AI_ENABLED:
    logger.warning(
        "GEMINI_API_KEY o'rnatilmagan — AI funksiyalari o'chirilgan holda ishlaydi. "
        "Yoqish uchun Render > Environment > GEMINI_API_KEY qo'shing."
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
                        thinking_level="minimal"):
    """Gemini API'ga xom so'rov yuboradi. Asosiy model xato bersa (band,
    topilmadi va h.k.), zaxira modellar bilan qayta urinadi. Hammasi
    muvaffaqiyatsiz bo'lsa None qaytaradi.

    thinking_level: "minimal" | "low" | "medium" | "high" | None
      Vazifa qanchalik oddiy bo'lsa, shuncha past daraja tanlanishi kerak —
      bu javob tezligiga TO'G'RIDAN-TO'G'RI ta'sir qiladi. Masalan oddiy
      OK/BAD moderatsiya yoki JSON ID ro'yxati tanlashda "minimal" yetarli;
      faqat erkin, chuqurroq fikr talab qiladigan suhbatlarda "low"/"medium"
      ishlatilsin."""
    if not AI_ENABLED:
        return None

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
                    continue  # keyingi zaxira modelni sinab ko'ramiz
                candidates = data.get("candidates") or []
                if not candidates:
                    continue
                parts = candidates[0].get("content", {}).get("parts", []) or []
                text = "".join(p.get("text", "") for p in parts).strip()
                if text:
                    return text
        except Exception:
            logger.exception("Gemini (%s) so'rovida xatolik yuz berdi", model_name)
            continue  # keyingi zaxira modelni sinab ko'ramiz

    return None  # barcha modellar muvaffaqiyatsiz bo'ldi


async def ask_ai(user_text, history=None, system_instruction=None):
    """Erkin suhbat uchun javob qaytaradi.
    history: [("user"|"model", matn), ...] — oldingi xabarlar (ixtiyoriy)."""
    contents = []
    for role, text in (history or []):
        contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    sys_prompt = system_instruction or (
        "Sen 'AniFilm Bot' telegram botidagi bilimdon AI-yordamchisan. "
        "Anime, kino va seriallar haqidagi savollarga oʻzbek tilida "
        "toʻliq, mazmunli va foydali javob ber — kerak boʻlsa misollar, "
        "tafsilotlar va tushuntirishlar bilan boy javob yoz, faqat bir-ikki "
        "gap bilan cheklanma. Sayoz yoki umumiy javoblardan qoch — aniq "
        "faktlar, nomlar va tavsiyalar bilan javob ber. Anime bilan bogʻliq "
        "boʻlmagan savollarga ham xuddi shunday chuqur va foydali javob ber."
    )
    return await _call_gemini(contents, system_instruction=sys_prompt,
                               temperature=0.85, max_output_tokens=1000,
                               thinking_level="low")


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
        temperature=0.0, max_output_tokens=5,
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
        temperature=0.9, max_output_tokens=220,
    )


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
        temperature=0.7, max_output_tokens=60,
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
        temperature=0.9, max_output_tokens=200,
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
        temperature=0.9, max_output_tokens=200,
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
        temperature=0.4, max_output_tokens=150,
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
        temperature=0.6, max_output_tokens=350,
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
        temperature=0.3, max_output_tokens=300, json_mode=True,
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


async def extract_payment_amount(image_base64, mime_type="image/jpeg"):
    """To'lov skrinshotidan summani (va agar ko'rinsa, karta oxirgi 4 raqamini)
    o'qiydi — adminga tasdiqlashni tezlashtirish uchun, YAKUNIY qaror baribir
    admin tomonidan qabul qilinadi (bu faqat yordamchi taxmin)."""
    if not AI_ENABLED:
        return None
    prompt = (
        "Bu to'lov cheki/skrinshoti. Undagi PUL SUMMASINI (raqam, valyuta "
        "bilan) top. Agar ko'rinib tursa, karta raqamining oxirgi 4 "
        "raqamini ham top. FAQAT quyidagi JSON formatda javob ber: "
        '{"amount": "...", "card_last4": "..."} Aniq o\'qiy olmasangan '
        'maydonni bo\'sh ("") qoldir, hech narsa o\'ylab topma.'
    )
    contents = [{
        "role": "user",
        "parts": [
            {"inline_data": {"mime_type": mime_type, "data": image_base64}},
            {"text": prompt},
        ],
    }]
    result = await _call_gemini(contents, temperature=0.0, max_output_tokens=100, json_mode=True)
    if not result:
        return None
    try:
        data = json.loads(result)
        return {
            "amount": str(data.get("amount") or "").strip(),
            "card_last4": str(data.get("card_last4") or "").strip(),
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
        temperature=0.0, max_output_tokens=5,
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
        temperature=0.6, max_output_tokens=400,
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
        temperature=0.6, max_output_tokens=200, json_mode=True,
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
        temperature=0.4, max_output_tokens=250, json_mode=True,
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
                               temperature=0.7, max_output_tokens=400,
                               thinking_level="low")


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
        temperature=0.4, max_output_tokens=200, json_mode=True,
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
