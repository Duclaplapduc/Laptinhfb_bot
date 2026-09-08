import os
import sqlite3
import requests
from datetime import datetime
from threading import Thread
from flask import Flask

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================================================
# CẤU HÌNH
# =========================================================

TOKEN = os.environ.get("BOT_TOKEN")
DB_FILE = "facebook.db"

BTN_ADD = "➕ Thêm ID"
BTN_LIST = "📋 Danh sách"
BTN_CHECK = "🔍 Kiểm tra ngay"
BTN_REMOVE = "🗑 Xóa ID"

# Xác nhận thay đổi 2 lần liên tiếp trước khi gửi cảnh báo
pending_changes = {}


# =========================================================
# WEB SERVER CHO RENDER
# =========================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Laptinh Facebook Bot is running"


@web_app.route("/health")
def health():
    return "OK"


def run_web():
    port = int(os.environ.get("PORT", 10000))

    web_app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False
    )


# =========================================================
# DATABASE
# =========================================================

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


# =========================================================
# BÀN PHÍM TELEGRAM
# =========================================================

def keyboard():

    return ReplyKeyboardMarkup(
        [
            [BTN_ADD, BTN_LIST],
            [BTN_CHECK, BTN_REMOVE],
        ],
        resize_keyboard=True
    )


# =========================================================
# KIỂM TRA FACEBOOK
# =========================================================

def check_facebook(fb_id):

    url = f"https://www.facebook.com/{fb_id}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
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


# =========================================================
# HIỂN THỊ TRẠNG THÁI
# =========================================================

def status_text(status):

    if status == "AVAILABLE":
        return "🟢 Có thể truy cập"

    if status == "UNAVAILABLE":
        return "🔴 Không khả dụng"

    return "🟡 Chưa xác định"


# =========================================================
# START BOT
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.application.bot_data[
        "chat_id"
    ] = update.effective_chat.id

    await update.message.reply_text(
        "🤖 LAPTINH FACEBOOK MONITOR\n\n"
        "Theo dõi trạng thái công khai của Facebook ID.\n\n"
        "Chọn chức năng bên dưới:",
        reply_markup=keyboard()
    )


# =========================================================
# DANH SÁCH
# =========================================================

async def show_list(update: Update):

    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    cur.execute(
        "SELECT fb_id, status, last_check "
        "FROM accounts"
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
            f"🆔 {fb_id}\n"
            f"{status_text(status)}\n"
            f"🕒 {last_check or 'Chưa kiểm tra'}\n\n"
        )

    await update.message.reply_text(
        msg,
        reply_markup=keyboard()
    )


# =========================================================
# KIỂM TRA TẤT CẢ
# =========================================================

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
            (
                status,
                now,
                fb_id
            )
        )

        msg += (
            f"🆔 {fb_id}\n"
            f"{status_text(status)}\n\n"
        )

    con.commit()
    con.close()

    await update.message.reply_text(
        msg,
        reply_markup=keyboard()
    )


# =========================================================
# XỬ LÝ NÚT BẤM
# =========================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text.strip()

    # -------------------------
    # THÊM ID
    # -------------------------

    if text == BTN_ADD:

        context.user_data["mode"] = "add"

        await update.message.reply_text(
            "➕ Gửi Facebook ID cần theo dõi.\n\n"
            "Ví dụ:\n"
            "1000123456789"
        )

        return

    # -------------------------
    # XÓA ID
    # -------------------------

    if text == BTN_REMOVE:

        context.user_data["mode"] = "remove"

        await update.message.reply_text(
            "🗑 Gửi Facebook ID cần xóa."
        )

        return

    # -------------------------
    # DANH SÁCH
    # -------------------------

    if text == BTN_LIST:

        await show_list(update)

        return

    # -------------------------
    # KIỂM TRA
    # -------------------------

    if text == BTN_CHECK:

        await check_all(update)

        return

    mode = context.user_data.get("mode")

    # =====================================================
    # NHẬN ID MỚI
    # =====================================================

    if mode == "add":

        fb_id = text.strip()

        if not fb_id.isdigit():

            await update.message.reply_text(
                "⚠️ Facebook ID phải là dãy số.\n\n"
                "Ví dụ:\n"
                "1000123456789"
            )

            return

        await update.message.reply_text(
            "⏳ Đang kiểm tra Facebook ID..."
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
            (
                fb_id,
                status,
                last_check
            )
            VALUES (?, ?, ?)
            """,
            (
                fb_id,
                status,
                now
            )
        )

        con.commit()
        con.close()

        context.user_data["mode"] = None

        await update.message.reply_text(
            "✅ ĐÃ THÊM FACEBOOK ID\n\n"
            f"🆔 {fb_id}\n"
            f"{status_text(status)}",
            reply_markup=keyboard()
        )

        return

    # =====================================================
    # XÓA ID
    # =====================================================

    if mode == "remove":

        fb_id = text.strip()

        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()

        cur.execute(
            "DELETE FROM accounts "
            "WHERE fb_id = ?",
            (fb_id,)
        )

        deleted = cur.rowcount

        con.commit()
        con.close()

        context.user_data["mode"] = None

        if deleted:

            await update.message.reply_text(
                "🗑 ĐÃ XÓA\n\n"
                f"ID: {fb_id}",
                reply_markup=keyboard()
            )

        else:

            await update.message.reply_text(
                "⚠️ Không tìm thấy ID này.",
                reply_markup=keyboard()
            )

        return

    await update.message.reply_text(
        "👇 Hãy chọn chức năng bên dưới.",
        reply_markup=keyboard()
    )


# =========================================================
# TỰ ĐỘNG KIỂM TRA
# =========================================================

async def auto_monitor(
    context: ContextTypes.DEFAULT_TYPE
):

    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    cur.execute(
        "SELECT fb_id, status "
        "FROM accounts"
    )

    rows = cur.fetchall()

    for fb_id, old_status in rows:

        new_status = check_facebook(fb_id)
        now = datetime.now().strftime("%d/%m/%Y %H:%M")

        # UNKNOWN thường là lỗi mạng/chặn tạm thời:
        # chỉ cập nhật thời gian, không đổi trạng thái và không cảnh báo.
        if new_status == "UNKNOWN":
            cur.execute(
                """
                UPDATE accounts
                SET last_check = ?
                WHERE fb_id = ?
                """,
                (now, fb_id)
            )
            pending_changes.pop(fb_id, None)
            continue

        # Không thay đổi: xóa bộ đếm xác nhận và chỉ cập nhật thời gian.
        if not old_status or new_status == old_status:
            pending_changes.pop(fb_id, None)

            cur.execute(
                """
                UPDATE accounts
                SET status = ?, last_check = ?
                WHERE fb_id = ?
                """,
                (new_status, now, fb_id)
            )
            continue

        # Có thay đổi: phải thấy cùng trạng thái mới 2 lần liên tiếp.
        pending_status, count = pending_changes.get(
            fb_id, (None, 0)
        )

        if pending_status == new_status:
            count += 1
        else:
            pending_status = new_status
            count = 1

        pending_changes[fb_id] = (pending_status, count)

        # Lần đầu chỉ ghi nhận nghi ngờ, chưa đổi trạng thái chính thức.
        if count < 2:
            cur.execute(
                """
                UPDATE accounts
                SET last_check = ?
                WHERE fb_id = ?
                """,
                (now, fb_id)
            )
            continue

        # Xác nhận lần 2: đổi trạng thái chính thức và gửi cảnh báo.
        cur.execute(
            """
            UPDATE accounts
            SET status = ?, last_check = ?
            WHERE fb_id = ?
            """,
            (new_status, now, fb_id)
        )

        pending_changes.pop(fb_id, None)

        chat_id = context.application.bot_data.get("chat_id")

        if chat_id:
            if new_status == "UNAVAILABLE":
                message = (
                    "🚨 FACEBOOK KHÔNG CÒN TRUY CẬP ĐƯỢC\n\n"
                    f"🆔 ID: {fb_id}\n"
                    "🔴 Link/profile hiện không truy cập được."
                )
            else:
                message = (
                    "✅ FACEBOOK HOẠT ĐỘNG TRỞ LẠI\n\n"
                    f"🆔 ID: {fb_id}\n"
                    "🟢 Link/profile đã truy cập được trở lại."
                )

            await context.bot.send_message(
                chat_id=chat_id,
                text=message
            )

    con.commit()
    con.close()


# =========================================================
# MAIN
# =========================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "Chưa thiết lập BOT_TOKEN trên Render."
        )

    init_db()

    # Chạy web server cho Render
    Thread(
        target=run_web,
        daemon=True
    ).start()

    # Khởi tạo Telegram Bot
    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler
        )
    )

    # Kiểm tra Facebook mỗi 30 giây
    app.job_queue.run_repeating(
        auto_monitor,
        interval=30,
        first=10
    )

    print(
        "Laptinh Facebook Monitor đang hoạt động..."
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
