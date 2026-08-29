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
import difflib
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
_MAX_CACHE_ENTRIES = 300  # cheksiz o'sishning oldini olish uchun oddiy chegara


async def _get_session():
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession()
    return _session


async def close_session():
    """Bot to'xtayotganda chaqirilishi kerak — ochiq aiohttp session'ni yopadi."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _prune_cache():
    """Muddati o'tgan yozuvlarni tozalaydi; agar kesh baribir juda katta
    bo'lib qolsa (juda ko'p turli nom qidirilgan bo'lsa), eng eski
    yozuvlarni chiqarib, xotira cheksiz o'sib ketishining oldini oladi."""
    now = time.monotonic()
    for k in [k for k, (exp, _) in _cache.items() if exp <= now]:
        _cache.pop(k, None)
    if len(_cache) > _MAX_CACHE_ENTRIES:
        overflow = len(_cache) - _MAX_CACHE_ENTRIES
        for k in list(_cache.keys())[:overflow]:  # dict tartibi = qo'shilish tartibi
            _cache.pop(k, None)


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
  Page(perPage: 8) {
    media(search: $search, type: ANIME) {
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
}
"""


def _pick_best_match(query: str, candidates: list[dict]) -> dict:
    """AniList so'ralgan nomga eng mosini har doim ham birinchi qaytarmaydi
    (ayniqsa nom qisqartma, boshqacha yozilgan yoki lotin/original
    romanizatsiyasi farq qilsa). Shu sabab bir nechta nomzod so'rab, har
    birining uchala nom varianti (english/romaji/native) bilan so'ralgan
    nomni solishtirib, ENG YAQININI o'zimiz tanlaymiz."""
    q = query.strip().lower()
    best, best_score = candidates[0], -1.0
    for c in candidates:
        titles = c.get("title") or {}
        for t in (titles.get("english"), titles.get("romaji"), titles.get("native")):
            if not t:
                continue
            score = difflib.SequenceMatcher(None, q, t.strip().lower()).ratio()
            if score > best_score:
                best_score = score
                best = c
    return best

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

    cache_key = re.sub(r"\s+", " ", title.lower())
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

    media_list = (data or {}).get("data", {}).get("Page", {}).get("media") or []
    if not media_list:
        return None
    media = _pick_best_match(title, media_list)

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
    _prune_cache()
    _cache[cache_key] = (time.monotonic() + _CACHE_TTL, result)
    return result
