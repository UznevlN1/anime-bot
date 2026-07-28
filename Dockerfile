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

# Botni ishga tushirish — asosiy fayl nomingizga qarab moslang
# (agar fayl nomi boshqacha bo'lsa, shu qatorni o'zgartiring)
CMD ["python", "anime_bot.py"]
