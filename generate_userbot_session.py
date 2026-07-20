"""
Jonli efir (RTMP) uchun USERBOT sessiyasini bir marta yaratish skripti.

QOʻLLANMA:
1) Shu skriptni SIZNING O'Z KOMPYUTERINGIZDA ishga tushiring (bot server'ida
   EMAS) — chunki bu yerda telefon raqamingiz va Telegram yuboradigan SMS/
   ilova kodini kiritishingiz kerak bo'ladi.
2) O'rnating:  pip install pyrogram tgcrypto
3) my.telegram.org saytidan API_ID va API_HASH oling (agar hali olmagan
   bo'lsangiz): https://my.telegram.org -> API Development Tools
4) Quyida API_ID va API_HASH'ni pastda to'ldiring (yoki muhit
   o'zgaruvchisi sifatida bering) va skriptni ishga tushiring:
       python generate_userbot_session.py
5) Telefon raqamingizni (+998... formatida) va Telegram yuborgan kodni
   kiriting. Ikki bosqichli parolingiz (2FA) bo'lsa, uni ham so'raydi.
6) Oxirida chiqadigan uzun matn — SESSIYA SATRI (session string). Uni
   hech kimga bermang (bu akkauntingizga to'liq kirish huquqi beradi!),
   faqat bot serveridagi `USERBOT_SESSION_STRING` muhit o'zgaruvchisiga
   qo'ying.

ESLATMA: bu akkaunt (foydalanuvchi sifatida) jonli efir boshlanadigan
kanalda ADMIN bo'lishi shart (kamida "Video chatlarni boshqarish" huquqi
bilan) — aks holda RTMP boshlanmaydi.
"""

import os
from pyrogram import Client

API_ID = int(os.environ.get("API_ID") or input("API_ID: ").strip())
API_HASH = os.environ.get("API_HASH") or input("API_HASH: ").strip()

with Client("userbot_session", api_id=API_ID, api_hash=API_HASH, in_memory=True) as app:
    session_string = app.export_session_string()
    print("\n" + "=" * 60)
    print("✅ Sessiya yaratildi! Quyidagini USERBOT_SESSION_STRING")
    print("muhit o'zgaruvchisiga (Render/Environment) qo'ying:")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
