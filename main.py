import logging
import mimetypes
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Optional
from urllib.parse import quote

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("atiq-image-host")

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].strip().rstrip("/")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"].strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram-webhook").strip().strip("/")
S3_BUCKET = os.environ["S3_BUCKET"].strip()
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1").strip()
URL_EXPIRY_SECONDS = int(os.getenv("URL_EXPIRY_SECONDS", "604800"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "20"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET must be 16-256 characters: letters, numbers, _ or -")
if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", WEBHOOK_PATH):
    raise RuntimeError("WEBHOOK_PATH must be 8-100 characters: letters, numbers, _ or -")
if not (60 <= URL_EXPIRY_SECONDS <= 604800):
    raise RuntimeError("URL_EXPIRY_SECONDS must be between 60 and 604800 (7 days).")
if not (1 <= MAX_FILE_MB <= 20):
    raise RuntimeError("MAX_FILE_MB must be between 1 and 20 MB for Telegram cloud Bot API downloads.")

s3 = boto3.client("s3", region_name=AWS_REGION)

telegram_app = (
    Application.builder()
    .token(BOT_TOKEN)
    .concurrent_updates(True)
    .build()
)


def safe_filename(name: Optional[str], fallback: str) -> str:
    name = (name or fallback).replace("\\", "_").replace("/", "_")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return (name[:120] or fallback)


def get_attachment(message):
    """Return (Telegram file object, original filename, MIME type, kind)."""
    if message.photo:
        return message.photo[-1], "photo.jpg", "image/jpeg", "Photo"

    candidates = [
        ("document", message.document, "Document"),
        ("video", message.video, "Video"),
        ("animation", message.animation, "Animation"),
        ("audio", message.audio, "Audio"),
        ("voice", message.voice, "Voice"),
        ("video_note", message.video_note, "Video note"),
    ]
    for attr, obj, label in candidates:
        if obj:
            filename = getattr(obj, "file_name", None)
            mime = getattr(obj, "mime_type", None)
            if not filename:
                ext = mimetypes.guess_extension(mime or "") or ".bin"
                filename = f"{attr}{ext}"
            return obj, filename, mime or mimetypes.guess_type(filename)[0] or "application/octet-stream", label
    return None


def make_s3_key(filename: str) -> str:
    # Random prefix prevents collisions and avoids exposing Telegram/user IDs in object keys.
    return f"uploads/{secrets.token_hex(16)}-{filename}"


def create_presigned_links(key: str, filename: str, content_type: str):
    encoded_name = quote(filename, safe="")
    inline_disposition = f"inline; filename*=UTF-8''{encoded_name}"
    attachment_disposition = f"attachment; filename*=UTF-8''{encoded_name}"

    common = {
        "Bucket": S3_BUCKET,
        "Key": key,
    }
    view_url = s3.generate_presigned_url(
        "get_object",
        Params={**common, "ResponseContentType": content_type,
                "ResponseContentDisposition": inline_disposition},
        ExpiresIn=URL_EXPIRY_SECONDS,
    )
    download_url = s3.generate_presigned_url(
        "get_object",
        Params={**common, "ResponseContentType": content_type,
                "ResponseContentDisposition": attachment_disposition},
        ExpiresIn=URL_EXPIRY_SECONDS,
    )
    # For image files, direct and view URLs intentionally have the same inline behavior.
    direct_url = view_url
    return direct_url, download_url, view_url


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 স্বাগতম ATIQ IMAGE HOST-এ!\n\n"
        "এখানে ছবি বা সাধারণ ফাইল পাঠান। আমি সেটি Amazon S3-তে আপলোড করে "
        "Direct, Download এবং View link তৈরি করে দেব।\n\n"
        f"সর্বোচ্চ ফাইল সাইজ: {MAX_FILE_MB} MB\n"
        f"লিংক সচল থাকবে: {URL_EXPIRY_SECONDS // 86400} দিন পর্যন্ত\n\n"
        "ব্যবহার: শুধু একটি ছবি বা ফাইল পাঠান।"
    )
    await update.effective_message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📌 ATIQ IMAGE HOST ব্যবহার\n\n"
        "• ছবি, PDF, ZIP, অডিও, ভিডিও বা Telegram document পাঠান।\n"
        "• সর্বোচ্চ ফাইল সাইজ 20 MB (কনফিগারেশন অনুযায়ী কম হতে পারে)।\n"
        "• আপলোড শেষে Direct, Download ও View link পাবেন।\n"
        "• নিরাপত্তার জন্য লিংক নির্দিষ্ট সময় পর মেয়াদোত্তীর্ণ হবে।"
    )


async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    attachment = get_attachment(message)
    if not attachment:
        await message.reply_text("একটি ছবি বা ফাইল পাঠান। /help লিখলে নিয়ম দেখতে পারবেন।")
        return

    tg_file_obj, original_name, content_type, kind = attachment
    original_name = safe_filename(original_name, "upload.bin")

    try:
        await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        tg_file = await tg_file_obj.get_file()
        # Download to memory only; enforce limit before and after download.
        if tg_file.file_size and tg_file.file_size > MAX_FILE_BYTES:
            await message.reply_text(f"❌ ফাইলটি {MAX_FILE_MB} MB সীমার চেয়ে বড়।")
            return

        data = await tg_file.download_as_bytearray()
        if len(data) > MAX_FILE_BYTES:
            await message.reply_text(f"❌ ফাইলটি {MAX_FILE_MB} MB সীমার চেয়ে বড়।")
            return

        key = make_s3_key(original_name)
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=bytes(data),
            ContentType=content_type,
            # Do not make objects public. Access is provided by time-limited signed URLs.
            ServerSideEncryption="AES256",
            Metadata={"source": "telegram-bot"},
        )

        direct_url, download_url, view_url = create_presigned_links(key, original_name, content_type)
        expiry_days = URL_EXPIRY_SECONDS / 86400
        result = (
            "✅ আপলোড সফল!\n\n"
            f"📄 নাম: {original_name}\n"
            f"📦 ধরন: {kind}\n"
            f"📏 সাইজ: {len(data) / 1024:.1f} KB\n\n"
            f"🔗 Direct Link:\n{direct_url}\n\n"
            f"⬇️ Download Link:\n{download_url}\n\n"
            f"🖼️ View Link:\n{view_url}\n\n"
            f"⏳ লিংকের মেয়াদ: প্রায় {expiry_days:g} দিন"
        )
        await message.reply_text(result, disable_web_page_preview=True)
        logger.info("Upload completed: key=%s bytes=%d", key, len(data))

    except (BotoCoreError, ClientError):
        logger.exception("S3 upload failed")
        await message.reply_text("❌ S3-তে আপলোড করা যায়নি। কিছুক্ষণ পর আবার চেষ্টা করুন।")
    except Exception:
        logger.exception("Unexpected upload error")
        await message.reply_text("❌ একটি সমস্যা হয়েছে। পরে আবার চেষ্টা করুন।")


telegram_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r"^/start(?:@\w+)?$"), start_command))
telegram_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r"^/help(?:@\w+)?$"), help_command))
telegram_app.add_handler(
    MessageHandler(
        filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO |
        filters.VOICE | filters.ANIMATION | filters.VIDEO_NOTE,
        handle_upload,
    )
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    webhook_url = f"{PUBLIC_BASE_URL}/{WEBHOOK_PATH}"
    await telegram_app.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    logger.info("Webhook configured: %s", webhook_url)
    yield
    try:
        await telegram_app.bot.delete_webhook()
    finally:
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(title="ATIQ IMAGE HOST", version="1.0.0", lifespan=lifespan)


@app.get("/")
async def root():
    return {"service": "ATIQ IMAGE HOST", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post(f"/{WEBHOOK_PATH}")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    if not x_telegram_bot_api_secret_token or not secrets.compare_digest(
        x_telegram_bot_api_secret_token, WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    try:
        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
        await telegram_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception:
        logger.exception("Failed to process Telegram update")
        raise HTTPException(status_code=500, detail="Update processing failed")
