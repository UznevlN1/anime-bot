"""
Kanal video-chatiga (jonli efirga) RTMP orqali ulanish uchun yordamchi modul.

MUHIM: bu bot tokeni bilan emas, balki alohida USERBOT (shaxsiy akkaunt,
telefon raqami orqali kirilgan sessiya) bilan ishlaydi — chunki Telegram
botlarga kanal video-chatini boshlashga ruxsat bermaydi, faqat oddiy
foydalanuvchi akkauntlariga (yoki kanal ma'muriga) ruxsat beradi.

Ishlatishdan oldin:
1) `generate_userbot_session.py` skriptini OʻZINGIZNING kompyuteringizda
   (bu bot ishlaydigan serverda emas) bir marta ishga tushirib, sessiya
   satrini (session string) oling.
2) Uni `USERBOT_SESSION_STRING` muhit o'zgaruvchisiga qo'ying.
3) Userbot akkaunti live-stream boshlanadigan kanalda ADMIN (kamida
   "Video chatlarni boshqarish" huquqi bilan) bo'lishi shart.
"""

import random
import time

from pyrogram.raw.functions.phone import (
    CreateGroupCall,
    GetGroupCallStreamRtmpUrl,
    DiscardGroupCall,
    GetGroupCall,
)
from pyrogram.raw.functions.channels import GetFullChannel
from pyrogram.raw import types as raw_types


async def _existing_call(client, channel):
    """Kanalning joriy faol group/video-chatini (bor bo'lsa) qaytaradi, aks holda None."""
    peer = await client.resolve_peer(channel)
    full = await client.invoke(GetFullChannel(channel=peer))
    return full.full_chat.call


async def start_rtmp(client, channel):
    """RTMP jonli efirni boshlaydi (kerak bo'lsa video-chat yaratadi) va
    (url, key) juftligini qaytaradi — buni OBS/ffmpeg'ga kiritish kerak.

    Agar oldingi video-chat to'g'ri yopilmasdan "eskirgan" (stale) holda
    qolib ketgan bo'lsa, Telegram RTMP url so'rovini xato bilan rad etishi
    mumkin. Shu holat uchun: xato chiqsa, eski chaqiruv (agar topilsa)
    tashlab yuboriladi (discard) va yangi video-chat yaratib bir marta
    qayta urinib ko'riladi."""
    peer = await client.resolve_peer(channel)
    call = await _existing_call(client, channel)
    if call is None:
        await client.invoke(CreateGroupCall(
            peer=peer,
            random_id=random.randrange(1, 2 ** 31 - 1),
            rtmp_stream=True,
        ))
    try:
        result = await client.invoke(GetGroupCallStreamRtmpUrl(peer=peer, revoke=False))
        return result.url, result.key
    except Exception:
        if call is not None:
            try:
                input_call = raw_types.InputGroupCall(id=call.id, access_hash=call.access_hash)
                await client.invoke(DiscardGroupCall(call=input_call))
            except Exception:
                pass
        await client.invoke(CreateGroupCall(
            peer=peer,
            random_id=random.randrange(1, 2 ** 31 - 1),
            rtmp_stream=True,
        ))
        result = await client.invoke(GetGroupCallStreamRtmpUrl(peer=peer, revoke=False))
        return result.url, result.key


async def stop_rtmp(client, channel):
    """Joriy video-chatni tugatadi. Faol video-chat topilmasa False qaytaradi."""
    call = await _existing_call(client, channel)
    if call is None:
        return False
    input_call = raw_types.InputGroupCall(id=call.id, access_hash=call.access_hash)
    await client.invoke(DiscardGroupCall(call=input_call))
    return True


async def rtmp_status(client, channel):
    """Kanalda hozir faol video-chat bor-yo'qligini tekshiradi."""
    call = await _existing_call(client, channel)
    return call is not None


async def get_call_info(client, channel):
    """Kanaldagi video-chat haqida qisqa ma'lumot qaytaradi:
    {"active": bool, "participants_count": int|None}.

    MUHIM: channels.getFullChannel orqali olinadigan 'call' maydoni faqat
    InputGroupCall (id + access_hash) — unda participants_count YO'Q. Haqiqiy
    tomoshabinlar sonini olish uchun alohida phone.GetGroupCall so'rovi kerak."""
    call = await _existing_call(client, channel)
    if call is None:
        return {"active": False, "participants_count": None}
    count = None
    try:
        input_call = raw_types.InputGroupCall(id=call.id, access_hash=call.access_hash)
        full_call = await client.invoke(GetGroupCall(call=input_call, limit=0))
        count = getattr(full_call.call, "participants_count", None)
    except Exception:
        pass
    return {"active": True, "participants_count": count}
