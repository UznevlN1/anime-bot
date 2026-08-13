"""
===================== ANIME MA'LUMOT XIZMATI (AniList) =====================
Bu modul AniList (https://anilist.co) dan anime haqida HAQIQIY ma'lumot
(yil, mamlakat, janr, tavsif, poster) olib beradi. AniList API'si bepul,
API kalit talab qilmaydi.

Bu yerdagi ma'lumotlar (tavsifdan tashqari) INGLIZ tilida keladi va
qo'shimcha tarjima kerak emas — faqat "description" (tavsif) maydoni
ingliz tilida keladi, uni o'zbek tiliga tarjima qilish uchun
ai_service.translate_description_to_uz() funksiyasidan foydalaning.

Asosiy funksiya:
    search_anilist(title) -> dict | None
"""
import logging
import re
import time
import aiohttp
import asyncio

logger = logging.getLogger("anime_api")

ANILIST_URL = "https://graphql.anilist.co"

_session = None
_session_lock = asyncio.Lock()

# Bir xil nom qayta-qayta qidirilganda (masalan admin anime qo'shishda bir
# necha marta orqaga qaytib qayta urinsa, yoki bir xil serial nomi turli
# vaqtda qidirilsa) har safar tashqi AniList API'ga so'rov ketmasligi uchun
# oddiy xotiradagi TTL kesh. Kalit — normallashtirilgan (lower+strip) nom.
_CACHE_TTL = 6 * 3600  # 6 soat — anime metama'lumotlari kamdan-kam o'zgaradi
_cache: dict[str, tuple] = {}  # normalized_title -> (expires_at, result)


async def _get_session():
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession()
    return _session


# AniList "countryOfOrigin" ISO kod qaytaradi (JP, KR, CN, TW, US va h.k.).
# Botda mamlakat nomi o'zbekcha ko'rsatilishi uchun eng ko'p uchraydigan
# kodlarni o'zbekcha nomlarga moslaymiz. Ro'yxatda yo'q kod kelsa, xom
# kod (masalan "FR") qaytariladi — bo'sh qolgandan ko'ra shu ma'qul.
_COUNTRY_MAP = {
    "JP": "Yaponiya",
    "KR": "Janubiy Koreya",
    "CN": "Xitoy",
    "TW": "Tayvan",
    "US": "AQSH",
    "FR": "Fransiya",
}

_ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    title { romaji english native }
    description(asHtml: false)
    genres
    seasonYear
    startDate { year }
    episodes
    countryOfOrigin
    averageScore
    coverImage { large extraLarge }
    siteUrl
  }
}
"""

# AniList tavsiflari <br> (qator ko'chirish) va <i> kabi teglar bilan keladi.
# <br> -> "\n" ga aylantiriladi, qolgan teglar esa BO'SH emas, BO'SHLIQ bilan
# almashtiriladi — aks holda "so'z.<i>Izoh</i>" kabi joylarda teglar olib
# tashlanganda "so'z.Izoh" deb qo'shilib qolar edi (bo'sh joy yo'qolib ketib).
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


async def search_anilist(title: str) -> dict | None:
    """AniList'dan nomi bo'yicha ENG MOS bitta anime'ni topib qaytaradi.
    Topilmasa yoki xato bo'lsa None qaytaradi (chaqiruvchi tomon bunga
    tayyor turishi, ya'ni foydalanuvchiga xatolik ko'rsatib, qo'lda
    kiritish imkonini qoldirishi kerak)."""
    title = (title or "").strip()
    if not title:
        return None

    cache_key = title.lower()
    cached = _cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    try:
        session = await _get_session()
        async with session.post(
            ANILIST_URL,
            json={"query": _ANILIST_QUERY, "variables": {"search": title}},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"AniList xato qaytardi: {resp.status}")
                return None
            data = await resp.json()
    except Exception as e:
        logger.warning(f"AniList so'rovida xato: {e}")
        return None

    media = (data or {}).get("data", {}).get("Media")
    if not media:
        return None

    desc_raw = media.get("description") or ""
    desc_clean = _BR_RE.sub("\n", desc_raw)
    desc_clean = _TAG_RE.sub(" ", desc_clean)
    desc_clean = re.sub(r"[ \t]+", " ", desc_clean)
    desc_clean = re.sub(r"\n{3,}", "\n\n", desc_clean).strip()

    country_code = media.get("countryOfOrigin") or ""
    country = _COUNTRY_MAP.get(country_code, country_code or "")

    year = media.get("seasonYear") or (media.get("startDate") or {}).get("year")

    titles = media.get("title") or {}
    best_title = titles.get("english") or titles.get("romaji") or titles.get("native") or title

    cover = media.get("coverImage") or {}

    result = {
        "id": media.get("id"),
        "title": best_title,
        "title_romaji": titles.get("romaji"),
        "title_native": titles.get("native"),
        "description_en": desc_clean,
        "genres": media.get("genres") or [],
        "year": year,
        "country": country,
        "episodes": media.get("episodes"),
        "score": media.get("averageScore"),
        "cover_url": cover.get("extraLarge") or cover.get("large"),
        "site_url": media.get("siteUrl"),
    }
    _cache[cache_key] = (time.monotonic() + _CACHE_TTL, result)
    return result
