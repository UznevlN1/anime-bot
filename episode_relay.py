"""
Botda saqlangan anime epizodini Telegram kanal video-chatiga (jonli efirga)
RTMP orqali avtomatik uzatish uchun yordamchi modul.

Ishlash tartibi:
1) Epizod videosi Pyrogram orqali serverning vaqtinchalik papkasiga to'liq
   yuklab olinadi (fayl "seek qilinadigan" bo'lishi kerak — aks holda ffmpeg
   ba'zi MP4 fayllarning oxiridagi metama'lumotni (moov atom) oqib bo'lmay
   xatolik berishi mumkin, shu sabab to'g'ridan-to'g'ri oqim emas, avval
   fayl sifatida yuklanadi).
2) ffmpeg (imageio_ffmpeg paketi orqali, alohida o'rnatish shart emas) shu
   faylni RTMP orqali `userbot_stream.start_rtmp()` bergan url+key manziliga
   real vaqt tezligida (-re) uzatadi.
3) Efir tugagach yoki to'xtatilganda vaqtinchalik fayl o'chiriladi.
"""

import asyncio
import glob
import logging
import os
import shutil
import tempfile
import time

logger = logging.getLogger(__name__)

# ===================== OVERLAY (anime nomi + qism) UCHUN SHRIFT =====================
# Video ustiga matn "kuydirish" (ffmpeg drawtext) uchun .ttf shrift fayli kerak.
# Avval eng ko'p tarqalgan yo'llarni tekshiramiz, topilmasa tizimdagi istalgan
# botqalin (bold) shriftni qidiramiz. Hech narsa topilmasa, overlay funksiyasi
# ogohlantirish bilan o'chirib qo'yiladi (efir baribir davom etadi, faqat
# matnsiz) — Dockerfile'ga masalan "fonts-dejavu-core" paketini qo'shish tavsiya
# etiladi.
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def _find_overlay_font():
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    for pattern in ("/usr/share/fonts/**/*Bold*.ttf", "/usr/share/fonts/**/*bold*.ttf"):
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]
    return None


OVERLAY_FONT = _find_overlay_font()
if OVERLAY_FONT:
    logger.info(f"Overlay uchun shrift topildi: {OVERLAY_FONT}")
else:
    logger.warning(
        "Overlay uchun .ttf shrift fayli topilmadi — anime nomi/qism matni videoga "
        "yozilmaydi. Dockerfile'ga 'apt-get install -y fonts-dejavu-core' qo'shing."
    )


def _escape_drawtext(text):
    """ffmpeg drawtext filtri uchun matnni xavfsiz qiladi (filtr sintaksisini
    buzadigan belgilarni escape qiladi: backslash, ikki nuqta, foiz, qo'shtirnoq)."""
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("%", "\\%")
        .replace("'", "\u2019")  # to'g'ridan-to'g'ri qo'shtirnoq filtr ichida muammo qiladi
    )

# Avval TIZIM ffmpeg'ini qidiramiz (Dockerfile orqali apt-get bilan
# o'rnatilgan). Bu imageio_ffmpeg'ning statik build'idan ustun turadi,
# chunki statik build ba'zi konteyner muhitlarida (masalan Render.com)
# SIGSEGV (-11) bilan qulab tushishi kuzatilgan — sabab hali aniq
# emas, lekin tizim ffmpeg'i bunday muammo bermaydi. Agar tizim
# ffmpeg topilmasa (masalan lokal Windows/macOS muhitida ishlab
# chiqish paytida), imageio_ffmpeg'ga qaytamiz.
_system_ffmpeg = shutil.which("ffmpeg")
if _system_ffmpeg:
    FFMPEG_PATH = _system_ffmpeg
    logger.info(f"Tizim ffmpeg ishlatiladi: {FFMPEG_PATH}")
else:
    try:
        import imageio_ffmpeg
        FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
        logger.warning(
            f"Tizim ffmpeg topilmadi, imageio_ffmpeg'ning statik build'i "
            f"ishlatiladi ({FFMPEG_PATH}). Konteyner muhitida bu SIGSEGV "
            f"berishi mumkin — Dockerfile orqali 'apt-get install ffmpeg' "
            f"qilish tavsiya etiladi."
        )
    except Exception as e:
        logger.warning(f"imageio_ffmpeg ham topilmadi ({e}), 'ffmpeg' nomi bilan ishga tushirishga urinamiz.")
        FFMPEG_PATH = "ffmpeg"


async def download_episode(pyro_client, message, progress_cb=None):
    """Epizod videosini vaqtinchalik faylga yuklab oladi, fayl yo'lini qaytaradi."""
    tmp_dir = tempfile.gettempdir()
    file_name = f"live_relay_{message.chat.id}_{message.id}_{int(time.time())}.mp4"
    path = os.path.join(tmp_dir, file_name)

    async def _progress(current, total):
        if progress_cb:
            try:
                await progress_cb(current, total)
            except Exception:
                pass

    result = await pyro_client.download_media(message, file_name=path, progress=_progress)
    return result or path


def build_rtmp_url(url, key):
    """RTMP server url va stream key'ni to'liq manzilga birlashtiradi."""
    base = url if url.endswith("/") else url + "/"
    return base + key


def _build_relay_cmd(local_path, rtmp_url, reencode_video=True, fast_preset=False, scale_720p=False, overlay_text=None):
    """ffmpeg buyrug'ini quradi.

    overlay_text berilsa (va shrift topilgan bo'lsa), matn (masalan anime nomi
    va qism raqami) videoning chap-pastki burchagiga fon (qora, yarim shaffof)
    bilan "kuydiriladi". DIQQAT: matnni kuydirish uchun video albatta qayta
    kodlanishi kerak (drawtext filtri "-c copy" bilan ishlamaydi) — shu sabab
    overlay_text berilganda reencode_video har doim True bo'lishi kerak
    (chaqiruvchi tomonda ta'minlanadi, start_relay_auto'ga qarang).

    reencode_video=False bo'lsa video "-c copy" bilan uzatiladi (tezkor, CPU
    tejaydi). Ammo ba'zi MP4 fayllarda kadr balandligi 16 ga karrali bo'lmasa
    (masalan 1280x694 -> ichkarida 1280x704 ga to'ldiriladi) "-c copy" bilan
    to'g'ridan-to'g'ri FLV'ga yozish johnvansickle statik ffmpeg build'larida
    SIGSEGV (-11) bilan qulashi ma'lum muammo — sabab FLV muxer shu "g'alati"
    o'lchamli oqim uchun original SPS/extradata'ni to'g'ri yoza olmasligi.
    Yechim: video qayta kodlash (re-encode) — bu toza SPS/PPS bilan yangi
    extradata yaratadi va muxer qulamaydi.

    fast_preset=True bo'lsa "ultrafast" preset ishlatiladi (CPU kuchsiz
    serverlar, masalan Render bepul tarifi uchun — "veryfast" ham real
    vaqtdan orqada qolib, efirni "qotirib" qo'yishi mumkin).

    scale_720p=True bo'lsa video balandligi 720px dan oshmasligi uchun
    pastga masshtablanadi (kattalashtirilmaydi) — CPU yukini yanada
    kamaytiradi.

    Har doim (reencode bo'lsa) "-g"/"-keyint_min" bilan har ~2 soniyada
    (30fps'da 60 kadr) majburiy keyframe qo'yiladi — Telegram video-chat
    RTMP qabul qiluvchisi buni kutadi; aks holda tarmoqda ozgina uzilish
    bo'lganda ekran keyingi keyframegacha "qotib" qoladi. "-maxrate"/
    "-bufsize" esa bitrate sakrashlarini cheklab, bufer to'lib "jamming"
    bo'lib qolishining oldini oladi.
    """
    if reencode_video:
        preset = "ultrafast" if fast_preset else "veryfast"
        maxrate, bufsize = ("2000k", "4000k") if scale_720p else ("3500k", "7000k")
        video_codec = [
            "-c:v", "libx264", "-preset", preset, "-pix_fmt", "yuv420p",
            "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
            "-maxrate", maxrate, "-bufsize", bufsize,
        ]
        vf_filters = []
        if scale_720p:
            vf_filters.append("scale=-2:min(720\\,ih)")
        if overlay_text and OVERLAY_FONT:
            escaped = _escape_drawtext(overlay_text)
            vf_filters.append(
                f"drawtext=fontfile={OVERLAY_FONT}:text='{escaped}':"
                "fontsize=28:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=12:"
                "x=24:y=h-th-24"
            )
        vf = ["-vf", ",".join(vf_filters)] if vf_filters else []
    else:
        video_codec = ["-c:v", "copy"]
        vf = []
    return [
        FFMPEG_PATH, "-nostdin", "-loglevel", "error",
        "-re", "-i", local_path,
        *vf,
        *video_codec,
        "-c:a", "aac", "-b:a", "128k", "-bsf:a", "aac_adtstoasc",
        "-avoid_negative_ts", "make_zero",
        "-f", "flv", rtmp_url,
    ]


async def _drain_stderr(proc, tail_holder, max_tail=4000):
    """ffmpeg'ning stderr oqimini jarayon davomida UZLUKSIZ o'qib turadi.

    Bu shart, chunki OS pipe buferi cheklangan (odatda ~64KB): agar hech kim
    o'qimasa va ffmpeg yoza-yoza bufer to'lib qolsa, ffmpeg yozishda bloklanib
    (deadlock) osilib qoladi — bu uzoq davom etgan efirlarda kutilmagan
    qulash/to'xtashlarning yashirin sababi bo'lishi mumkin. Oxirgi ~4KB log
    xato bo'lganda ko'rsatish uchun tail_holder'da saqlanadi.
    """
    buf = b""
    try:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > max_tail:
                buf = buf[-max_tail:]
    except Exception:
        pass
    tail_holder["tail"] = buf


async def start_ffmpeg_relay(local_path, rtmp_url, reencode_video=True, fast_preset=False, scale_720p=False, overlay_text=None):
    """ffmpeg jarayonini fon rejimida ishga tushiradi, subprocess obyektini qaytaradi.

    reencode_video=True (standart) — video qayta kodlanadi, CPU'ni ko'proq band
    qiladi, lekin g'alati o'lchamli/metadata'li fayllarda SIGSEGV'ni oldini
    oladi. Agar barcha fayllaringiz "toza" (standart, 16 ga karrali) o'lchamda
    bo'lsa, tezroq ishlashi uchun reencode_video=False berib stream-copy
    rejimiga o'tishingiz mumkin.

    fast_preset va scale_720p — qarang: _build_relay_cmd. Kuchsiz serverlarda
    (masalan Render bepul tarifi) qotishlarni kamaytirish uchun ishlatiladi.

    overlay_text berilsa, matn (anime nomi + qism raqami) videoga kuydiriladi
    — bu reencode_video=True talab qiladi (chaqiruvchida ta'minlanishi kerak).

    Qaytaradi: (proc, stderr_tail_holder). stderr_tail_holder — jarayon
    tugagach ["tail"] kaliti orqali oxirgi log matnini o'z ichiga oladigan dict
    (proc.stderr endi to'g'ridan-to'g'ri o'qilmaydi, chunki uni fon vazifasi
    allaqachon uzluksiz iste'mol qilib turadi).
    """
    cmd = _build_relay_cmd(
        local_path, rtmp_url,
        reencode_video=reencode_video, fast_preset=fast_preset, scale_720p=scale_720p,
        overlay_text=overlay_text,
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    tail_holder = {"tail": b""}
    asyncio.create_task(_drain_stderr(proc, tail_holder))
    return proc, tail_holder


async def start_relay_auto(local_path, rtmp_url, startup_check_secs=4, overlay_text=None):
    """Kuchsiz CPU'li serverlar (masalan Render bepul tarifi) uchun avtomatik
    rejim tanlovchi ishga tushirish funksiyasi.

    1) Avval CPU deyarli ishlatmaydigan stream-copy ("-c copy") rejimida
       urinadi — bu Render bepul tarifidagi "qotish" muammosining eng
       keng tarqalgan sababi (real vaqt kodlash CPU'dan orqada qolishi)
       uchun eng samarali yechim.
    2) `startup_check_secs` soniya kutib turadi: agar shu vaqt ichida ffmpeg
       xato kod bilan tezda tugasa (odatda "g'alati" o'lchamli video sababli
       FLV muxer rad etganda yuz beradi), avtomatik ravishda qayta kodlash
       (reencode, "ultrafast" preset + 720p'ga pasaytirish) rejimiga o'tadi.
    3) Agar startup_check_secs ichida jarayon davom etayotgan bo'lsa —
       demak copy rejimi ishladi, shu holicha davom ettiriladi.

    overlay_text berilsa (anime nomi + qism raqami), matnni videoga kuydirish
    "-c copy" bilan mumkin emasligi sababli, stream-copy urinishi butunlay
    o'tkazib yuboriladi va to'g'ridan-to'g'ri reencode (ultrafast, 720p)
    rejimida ishga tushiriladi.

    Qaytaradi: (proc, stderr_tail_holder, used_mode) — used_mode "copy" yoki
    "reencode".
    """
    if overlay_text:
        proc, tail = await start_ffmpeg_relay(
            local_path, rtmp_url, reencode_video=True, fast_preset=True, scale_720p=True,
            overlay_text=overlay_text,
        )
        return proc, tail, "reencode"

    proc, tail = await start_ffmpeg_relay(local_path, rtmp_url, reencode_video=False)
    try:
        await asyncio.wait_for(proc.wait(), timeout=startup_check_secs)
    except asyncio.TimeoutError:
        # startup_check_secs dan ko'proq ishlayapti -> copy rejimi barqaror ishlayapti
        return proc, tail, "copy"

    # Jarayon tezda tugadi
    if proc.returncode == 0:
        # Juda qisqa video muvaffaqiyatli tugagan bo'lishi mumkin
        return proc, tail, "copy"

    logger.warning(
        f"[relay] stream-copy rejimi xato bilan tez tugadi (kod {proc.returncode}), "
        f"reencode (ultrafast, 720p) rejimiga avtomatik o'tilmoqda. "
        f"Log: {tail.get('tail', b'')[-500:]}"
    )
    proc, tail = await start_ffmpeg_relay(
        local_path, rtmp_url, reencode_video=True, fast_preset=True, scale_720p=True,
    )
    return proc, tail, "reencode"


async def stop_ffmpeg_relay(proc):
    """ffmpeg jarayonini to'xtatadi."""
    if proc is None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def cleanup_file(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
