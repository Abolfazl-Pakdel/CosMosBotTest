import logging
import os
import secrets
import sqlite3

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
# کانال‌های اسپانسر، مثلا: @sponsor1,@sponsor2
SPONSOR_CHANNELS = [c.strip() for c in os.getenv("SPONSOR_CHANNELS", "").split(",") if c.strip()]
DB_PATH = os.getenv("DB_PATH", "bot.db")
PROTECT_CONTENT = os.getenv("PROTECT_CONTENT", "1") == "1"  # جلوگیری از فوروارد/ذخیره

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("movie_bot")


# ---------------------------- Database ----------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS videos (
                code TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                caption TEXT DEFAULT '',
                downloads INTEGER DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )


def save_user(user):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user.id, user.username),
        )


def get_video(code: str):
    with db() as conn:
        return conn.execute("SELECT * FROM videos WHERE code = ?", (code,)).fetchone()


def add_video(file_id: str, caption: str) -> str:
    code = secrets.token_urlsafe(6)
    with db() as conn:
        conn.execute(
            "INSERT INTO videos (code, file_id, caption) VALUES (?, ?, ?)",
            (code, file_id, caption),
        )
    return code


def inc_downloads(code: str):
    with db() as conn:
        conn.execute("UPDATE videos SET downloads = downloads + 1 WHERE code = ?", (code,))


# ------------------------ Membership check ------------------------
async def get_missing_channels(bot, user_id: int) -> list[str]:
    """کانال‌هایی که کاربر هنوز عضو آن‌ها نیست را برمی‌گرداند."""
    missing = []
    for channel in SPONSOR_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            joined = member.status in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
            ) or (member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False))
            if not joined:
                missing.append(channel)
        except TelegramError as e:
            # معمولا یعنی ربات در کانال ادمین نیست یا آیدی کانال اشتباه است
            log.error("Cannot check membership in %s: %s", channel, e)
            missing.append(channel)
    return missing


def join_keyboard(channels: list[str], code: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"📢 عضویت در کانال {i}", url=f"https://t.me/{ch.lstrip('@')}")]
        for i, ch in enumerate(channels, start=1)
    ]
    rows.append([InlineKeyboardButton("✅ عضو شدم", callback_data=f"check:{code}")])
    return InlineKeyboardMarkup(rows)


async def deliver_video(bot, chat_id: int, code: str):
    video = get_video(code)
    if not video:
        await bot.send_message(chat_id, "❌ این ویدیو پیدا نشد یا حذف شده است.")
        return
    await bot.send_video(
        chat_id=chat_id,
        video=video["file_id"],
        caption=video["caption"] or None,
        protect_content=PROTECT_CONTENT,
    )
    inc_downloads(code)


# --------------------------- Handlers -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)

    if not context.args:
        await update.message.reply_text(
            "سلام 👋\nبرای دریافت فیلم، روی لینک داخل کانال اصلی کلیک کنید."
        )
        return

    code = context.args[0]
    if not get_video(code):
        await update.message.reply_text("❌ لینک نامعتبر است یا ویدیو حذف شده است.")
        return

    missing = await get_missing_channels(context.bot, user.id)
    if missing:
        await update.message.reply_text(
            "🔒 برای دریافت ویدیو ابتدا در کانال‌های زیر عضو شوید و سپس دکمه «عضو شدم» را بزنید:",
            reply_markup=join_keyboard(missing, code),
        )
        return

    await deliver_video(context.bot, update.effective_chat.id, code)


async def check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    code = query.data.split(":", 1)[1]

    missing = await get_missing_channels(context.bot, query.from_user.id)
    if missing:
        await query.answer("هنوز در همه کانال‌ها عضو نشده‌اید ❗", show_alert=True)
        return

    await query.answer("عضویت تایید شد ✅")
    try:
        await query.message.delete()
    except TelegramError:
        pass
    await deliver_video(context.bot, query.message.chat_id, code)


async def admin_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ادمین ویدیو را برای ربات می‌فرستد و لینک اختصاصی می‌گیرد."""
    msg = update.message
    media = msg.video or msg.document
    code = add_video(media.file_id, msg.caption or "")
    link = f"https://t.me/{context.bot.username}?start={code}"
    await msg.reply_text(
        f"✅ ویدیو ذخیره شد.\n\nلینک برای گذاشتن در کانال اصلی:\n{link}",
        disable_web_page_preview=True,
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        sent = conn.execute("SELECT COALESCE(SUM(downloads),0) FROM videos").fetchone()[0]
    await update.message.reply_text(f"👥 کاربران: {users}\n🎬 ویدیوها: {videos}\n📥 ارسال‌ها: {sent}")


async def list_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute("SELECT code, caption, downloads FROM videos ORDER BY rowid DESC LIMIT 20").fetchall()
    if not rows:
        await update.message.reply_text("هنوز ویدیویی اضافه نشده.")
        return
    text = "\n\n".join(
        f"🎬 {(r['caption'] or 'بدون عنوان')[:40]}\n"
        f"https://t.me/{context.bot.username}?start={r['code']}  ({r['downloads']} بار)"
        for r in rows
    )
    await update.message.reply_text(text, disable_web_page_preview=True)


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    admin_filter = filters.User(user_id=list(ADMIN_IDS)) & filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(check_callback, pattern=r"^check:"))
    app.add_handler(CommandHandler("stats", stats, filters=admin_filter))
    app.add_handler(CommandHandler("list", list_videos, filters=admin_filter))
    app.add_handler(MessageHandler(admin_filter & (filters.VIDEO | filters.Document.VIDEO), admin_video))

    log.info("Bot started. Sponsors: %s", SPONSOR_CHANNELS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
