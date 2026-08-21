# استفاده از نسخه سبک پایتون ۳.۱۳
FROM python:3.13-slim

# تنظیم دایرکتوری کاری داخل کانتینر
WORKDIR /app

# build-essential برای کامپایل کتابخانه‌هایی مثل TgCrypto لازم است.
# ffmpeg اختیاری است ولی بدون آن خروجی دستور .tts به جای «ویس» واقعی
# تلگرام، به صورت فایل WAV فرستاده می‌شود (utils/media.py → pcm_to_ogg).
# tzdata نصب می‌شود چون همه زمان‌های داخل پیام‌ها از منطقه زمانی خوانده
# می‌شوند (DISPLAY_TIMEZONE). بدون آن، کانتینر روی UTC می‌ماند و پیام ساعت
# ۲۱:۳۰ به‌صورت ۱۸:۰۰ گزارش می‌شد.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ffmpeg \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# کپی کردن لیست کتابخانه‌ها و نصب آن‌ها
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کپی کردن کل فایل‌های پروژه به داخل کانتینر
COPY . .

# دستوری که هنگام روشن شدن کانتینر اجرا می‌شود
CMD ["python", "main.py"]