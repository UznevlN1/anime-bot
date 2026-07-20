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
import logging
import os
import tempfile
import time

logger = logging.getLogger(__name__)

try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as e:
    logger.warning(f"imageio_ffmpeg topilmadi ({e}), tizimdagi 'ffmpeg' ishlatiladi.")
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


def _build_relay_cmd(local_path, rtmp_url, reencode_video=True):
    """ffmpeg buyrug'ini quradi.

    reencode_video=False bo'lsa video "-c copy" bilan uzatiladi (tezkor, CPU
    tejaydi). Ammo ba'zi MP4 fayllarda kadr balandligi 16 ga karrali bo'lmasa
    (masalan 1280x694 -> ichkarida 1280x704 ga to'ldiriladi) "-c copy" bilan
    to'g'ridan-to'g'ri FLV'ga yozish johnvansickle statik ffmpeg build'larida
    SIGSEGV (-11) bilan qulashi ma'lum muammo — sabab FLV muxer shu "g'alati"
    o'lchamli oqim uchun original SPS/extradata'ni to'g'ri yoza olmasligi.
    Yechim: video qayta kodlash (re-encode) — bu toza SPS/PPS bilan yangi
    extradata yaratadi va muxer qulamaydi.
    """
    video_codec = (
        ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
        if reencode_video else ["-c:v", "copy"]
    )
    return [
        FFMPEG_PATH, "-nostdin", "-loglevel", "verbose",
        "-re", "-i", local_path,
        *video_codec,
        "-c:a", "aac", "-b:a", "128k", "-bsf:a", "aac_adtstoasc",
        "-avoid_negative_ts", "make_zero",
        "-f", "flv", rtmp_url,
    ]


async def start_ffmpeg_relay(local_path, rtmp_url, reencode_video=True):
    """ffmpeg jarayonini fon rejimida ishga tushiradi, subprocess obyektini qaytaradi.

    reencode_video=True (standart) — video qayta kodlanadi, CPU'ni ko'proq band
    qiladi, lekin g'alati o'lchamli/metadata'li fayllarda SIGSEGV'ni oldini
    oladi. Agar barcha fayllaringiz "toza" (standart, 16 ga karrali) o'lchamda
    bo'lsa, tezroq ishlashi uchun reencode_video=False berib stream-copy
    rejimiga o'tishingiz mumkin.
    """
    cmd = _build_relay_cmd(local_path, rtmp_url, reencode_video=reencode_video)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return proc


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
