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
import aiohttp

logger = logging.getLogger("ai_service")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# "gemini-flash-latest" — Google'ning doim eng so'nggi (bepul tarifga kiruvchi)
# Flash modeliga ishora qiluvchi alias. Google modelni yangilasa ham, bu nom
# o'zgarmaydi va kod qayta yozilmasa ham ishlashda davom etadi.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

AI_ENABLED = bool(GEMINI_API_KEY)

if not AI_ENABLED:
    logger.warning(
        "GEMINI_API_KEY o'rnatilmagan — AI funksiyalari o'chirilgan holda ishlaydi. "
        "Yoqish uchun Render > Environment > GEMINI_API_KEY qo'shing."
    )


async def _call_gemini(contents, system_instruction=None, temperature=0.7,
                        max_output_tokens=800, json_mode=False, timeout=25):
    """Gemini API'ga xom so'rov yuboradi. Xatolik/o'chirilgan holatda None qaytaradi."""
    if not AI_ENABLED:
        return None

    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        },
    }
    if system_instruction:
        payload["system_instruction"] = {"parts": [{"text": system_instruction}]}
    if json_mode:
        payload["generationConfig"]["response_mime_type"] = "application/json"

    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GEMINI_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.warning("Gemini xato javob: %s - %s", resp.status, data)
                    return None
                candidates = data.get("candidates") or []
                if not candidates:
                    return None
                parts = candidates[0].get("content", {}).get("parts", []) or []
                text = "".join(p.get("text", "") for p in parts).strip()
                return text or None
    except Exception:
        logger.exception("Gemini so'rovida xatolik yuz berdi")
        return None


async def ask_ai(user_text, history=None, system_instruction=None):
    """Erkin suhbat uchun javob qaytaradi.
    history: [("user"|"model", matn), ...] — oldingi xabarlar (ixtiyoriy)."""
    contents = []
    for role, text in (history or []):
        contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    sys_prompt = system_instruction or (
        "Sen 'AniFilm Bot' telegram botidagi doʻstona AI-yordamchisan. "
        "Anime, kino va seriallar haqidagi savollarga oʻzbek tilida, qisqa, "
        "aniq va samimiy javob ber (odatda 2-5 gap). Anime bilan bogʻliq "
        "boʻlmagan savollarga ham xushmuomalalik bilan yordam ber, lekin "
        "javoblaringni oʻta uzun qilma, chunki bu Telegram chatida oʻqiladi."
    )
    return await _call_gemini(contents, system_instruction=sys_prompt,
                               temperature=0.8, max_output_tokens=500)


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
