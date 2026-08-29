FROM python:3.11-slim

WORKDIR /app

# ffmpeg — video kesish/blur/rendering uchun
# fonts-dejavu-core — DejaVuSans-Bold.ttf shriftini beradi, drawtext suv belgisi shu shriftni qidiradi
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Avval faqat requirements.txt'ni ko'chiramiz — Docker keshini
# samarali ishlatish uchun (kod o'zgarsa ham, agar requirements
# o'zgarmasa, pip install qayta bajarilmaydi).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Qolgan barcha loyihani ko'chiramiz
COPY . .

# Xavfsizlik: konteyner root emas, cheklangan huquqli foydalanuvchi sifatida
# ishlaydi. Runtime'da yoziladigan yagona joylar /tmp/anime_clips va
# /tmp/anime_episodes (kodga qarang) — /tmp esa standart holatda barcha
# foydalanuvchilar uchun yozish huquqiga ega, shuning uchun /app'ga qo'shimcha
# egalik/chmod berish shart emas.
RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

# Botni ishga tushirish — asosiy fayl nomingizga qarab moslang
# (agar fayl nomi boshqacha bo'lsa, shu qatorni o'zgartiring)
CMD ["python", "anime_bot.py"]
