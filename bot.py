import os
import sqlite3
import requests
from datetime import datetime

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ.get("BOT_TOKEN")
DB_FILE = "facebook.db"

BTN_ADD = "➕ Thêm ID"
BTN_LIST = "📋 Danh sách"
BTN_CHECK = "🔍 Kiểm tra ngay"
BTN_REMOVE = "🗑 Xóa ID"


def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            fb_id TEXT PRIMARY KEY,
            status TEXT,
            last_check TEXT
        )
    """)

    con.commit()
    con.close()


def keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_ADD, BTN_LIST],
            [BTN_CHECK, BTN_REMOVE],
        ],
        resize_keyboard=True
    )


def check_facebook(fb_id):
    url = f"https://www.facebook.com/{fb_id}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        )
    }

    try:
        r = requests.get(
            url,
            headers=headers,
            timeout=15,
            allow_redirects=True
        )

        text = r.text.lower()

        unavailable = [
            "this content isn't available",
            "this content isn’t available",
            "page isn't available",
            "page isn’t available",
            "content not found",
        ]

        if r.status_code == 404:
            return "UNAVAILABLE"

        if any(x in text for x in unavailable):
            return "UNAVAILABLE"

        if r.status_code == 200:
            return "AVAILABLE"

        return "UNKNOWN"

    except requests.RequestException:
        return "UNKNOWN"


def status_text(status):
    if status == "AVAILABLE":
        return "🟢 Có thể truy cập"

    if status == "UNAVAILABLE":
        return "🔴 Không khả dụng"

    return "🟡 Chưa xác định"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🤖 LAPTINH FACEBOOK MONITOR\n\n"
        "Theo dõi trạng thái công khai của Facebook ID.\n\n"
        "Chọn chức năng bên dưới:",
        reply_markup=keyboard()
    )


async def show_list(update: Update):

    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    cur.execute(
        "SELECT fb_id, status, last_check FROM accounts"
    )

    rows = cur.fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(
            "📭 Chưa có Facebook ID nào.",
            reply_markup=keyboard()
        )
        return

    msg = "📋 DANH SÁCH THEO DÕI\n\n"

    for fb_id, status, last_check in rows:

        msg += (
            f"ID: {fb_id}\n"
            f"{status_text(status)}\n"
            f"🕒 {last_check or 'Chưa kiểm tra'}\n\n"
        )

    await update.message.reply_text(
        msg,
        reply_markup=keyboard()
    )


async def check_all(update: Update):

    await update.message.reply_text(
        "⏳ Đang kiểm tra..."
    )

    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    cur.execute(
        "SELECT fb_id FROM accounts"
    )

    rows = cur.fetchall()

    if not rows:
        con.close()

        await update.message.reply_text(
            "📭 Chưa có ID nào.",
            reply_markup=keyboard()
        )
        return

    msg = "🔍 KẾT QUẢ KIỂM TRA\n\n"

    for (fb_id,) in rows:

        status = check_facebook(fb_id)

        now = datetime.now().strftime(
            "%d/%m/%Y %H:%M"
        )

        cur.execute(
            """
            UPDATE accounts
            SET status = ?, last_check = ?
            WHERE fb_id = ?
            """,
            (status, now, fb_id)
        )

        msg += (
            f"{fb_id}\n"
            f"{status_text(status)}\n\n"
        )

    con.commit()
    con.close()

    await update.message.reply_text(
        msg,
        reply_markup=keyboard()
    )


async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text.strip()

    if text == BTN_ADD:

        context.user_data["mode"] = "add"

        await update.message.reply_text(
            "➕ Gửi Facebook ID cần thêm.\n\n"
            "Ví dụ:\n1000123456789"
        )
        return

    if text == BTN_REMOVE:

        context.user_data["mode"] = "remove"

        await update.message.reply_text(
            "🗑 Gửi Facebook ID cần xóa."
        )
        return

    if text == BTN_LIST:

        await show_list(update)
        return

    if text == BTN_CHECK:

        await check_all(update)
        return

    mode = context.user_data.get("mode")

    if mode == "add":

        fb_id = text

        if not fb_id.isdigit():

            await update.message.reply_text(
                "⚠️ ID phải là dãy số."
            )
            return

        await update.message.reply_text(
            "⏳ Đang kiểm tra ID..."
        )

        status = check_facebook(fb_id)

        now = datetime.now().strftime(
            "%d/%m/%Y %H:%M"
        )

        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()

        cur.execute(
            """
            INSERT OR REPLACE INTO accounts
            (fb_id, status, last_check)
            VALUES (?, ?, ?)
            """,
            (fb_id, status, now)
        )

        con.commit()
        con.close()

        context.user_data["mode"] = None

        await update.message.reply_text(
            f"✅ Đã thêm ID:\n{fb_id}\n\n"
            f"{status_text(status)}",
            reply_markup=keyboard()
        )

        return

    if mode == "remove":

        fb_id = text

        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()

        cur.execute(
            "DELETE FROM accounts WHERE fb_id = ?",
            (fb_id,)
        )

        con.commit()
        con.close()

        context.user_data["mode"] = None

        await update.message.reply_text(
            f"🗑 Đã xóa ID:\n{fb_id}",
            reply_markup=keyboard()
        )

        return

    await update.message.reply_text(
        "Chọn một chức năng bên dưới.",
        reply_markup=keyboard()
    )


async def auto_monitor(
    context: ContextTypes.DEFAULT_TYPE
):

    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    cur.execute(
        "SELECT fb_id, status FROM accounts"
    )

    rows = cur.fetchall()

    for fb_id, old_status in rows:

        new_status = check_facebook(fb_id)

        now = datetime.now().strftime(
            "%d/%m/%Y %H:%M"
        )

        cur.execute(
            """
            UPDATE accounts
            SET status = ?, last_check = ?
            WHERE fb_id = ?
            """,
            (new_status, now, fb_id)
        )

        if (
            old_status
            and new_status != old_status
            and new_status != "UNKNOWN"
        ):

            chat_id = context.application.bot_data.get(
                "chat_id"
            )

            if chat_id:

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🚨 FACEBOOK THAY ĐỔI TRẠNG THÁI\n\n"
                        f"ID: {fb_id}\n\n"
                        f"{status_text(old_status)}\n"
                        "⬇️\n"
                        f"{status_text(new_status)}"
                    )
                )

    con.commit()
    con.close()


async def remember_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.application.bot_data[
        "chat_id"
    ] = update.effective_chat.id

    await start(update, context)


def main():

    if not TOKEN:
        raise RuntimeError(
            "Chưa thiết lập BOT_TOKEN"
        )

    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", remember_chat)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler
        )
    )

    app.job_queue.run_repeating(
        auto_monitor,
        interval=900,
        first=30
    )

    print("Bot đang hoạt động...")

    app.run_polling()


if __name__ == "__main__":
    main()
