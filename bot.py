import os
import sqlite3
import requests
from datetime import datetime
from threading import Thread
from flask import Flask

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

TOKEN = os.environ.get("BOT_TOKEN")
DB_FILE = "facebook.db"
CHECK_SECONDS = 30

BTN_ADD = "➕ Thêm UID"
BTN_LIST = "📋 Danh sách"
BTN_CHECK = "🔎 Kiểm tra ngay"
BTN_REMOVE = "❌ Xóa UID"

web_app = Flask(__name__)
pending_changes = {}

@web_app.route("/")
def home():
    return "Laptinh Facebook Monitor is running"

@web_app.route("/health")
def health():
    return "OK"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port, use_reloader=False)

def db():
    return sqlite3.connect(DB_FILE)

def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts(
            fb_id TEXT PRIMARY KEY,
            name TEXT,
            note TEXT,
            price TEXT,
            status TEXT,
            created_at TEXT,
            last_check TEXT,
            completed_at TEXT
        )
    """)

    # Tự nâng cấp database cũ, không cần xóa facebook.db.
    cur.execute("PRAGMA table_info(accounts)")
    columns = {row[1] for row in cur.fetchall()}
    if "completed_at" not in columns:
        cur.execute("ALTER TABLE accounts ADD COLUMN completed_at TEXT")

    con.commit()
    con.close()

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def status_label(status):
    if status == "AVAILABLE":
        return "✅ LIVE"
    if status == "UNAVAILABLE":
        return "❌ DIE"
    return "🟡 CHƯA XÁC ĐỊNH"

def check_facebook(fb_id):
    """
    Kiểm tra UID theo tín hiệu public Graph profile picture.
    Logic tương tự trang Check Live UID đã phân tích:
    URL cuối chứa 100x100 => LIVE.
    Request lỗi => UNKNOWN để tránh báo DIE giả.
    """
    url = f"https://graph.facebook.com/{fb_id}/picture?type=normal"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        )
    }

    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        final_url = r.url

        print(
            "[FB_CHECK]",
            f"uid={fb_id}",
            f"http={r.status_code}",
            f"final_url={final_url}",
            flush=True
        )

        if "100x100" in final_url.lower():
            print("[FB_CHECK_RESULT]", f"uid={fb_id}", "result=AVAILABLE", flush=True)
            return "AVAILABLE"

        print("[FB_CHECK_RESULT]", f"uid={fb_id}", "result=UNAVAILABLE", flush=True)
        return "UNAVAILABLE"

    except requests.RequestException as e:
        print(
            "[FB_CHECK_ERROR]",
            f"uid={fb_id}",
            f"error_type={type(e).__name__}",
            f"error={str(e)[:200]}",
            flush=True
        )
        return "UNKNOWN"

def keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_ADD, BTN_LIST],
            [BTN_CHECK, BTN_REMOVE]
        ],
        resize_keyboard=True
    )

def get_account(fb_id):
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT fb_id,name,note,price,status,created_at,last_check,completed_at
        FROM accounts WHERE fb_id=?
    """, (fb_id,))
    row = cur.fetchone()
    con.close()
    return row

def format_duration(start_text, end_text=None):
    try:
        start = datetime.strptime(start_text, "%Y-%m-%d %H:%M:%S")
        end = (
            datetime.strptime(end_text, "%Y-%m-%d %H:%M:%S")
            if end_text
            else datetime.now()
        )
        total = max(0, int((end - start).total_seconds()))
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)

        parts = []
        if days:
            parts.append(f"{days} ngày")
        if hours:
            parts.append(f"{hours} giờ")
        if minutes:
            parts.append(f"{minutes} phút")
        parts.append(f"{seconds} giây")
        return " ".join(parts)
    except Exception:
        return "-"

def format_ticket(row):
    fb_id, name, note, price, status, created_at, last_check, completed_at = row
    name = name or "Chưa cập nhật"
    note = note or "-"
    price = price or "0đ"

    if status == "AVAILABLE":
        progress = "Đang theo dõi chờ DIE ❌"
        time_line = f"⏳ Thời gian đã theo dõi: {format_duration(created_at)}"
        completed_line = ""
    elif status == "UNAVAILABLE":
        progress = "HOÀN THÀNH ✅"
        completed_line = f"\n🏁 Hoàn thành: {completed_at or last_check or '-'}"
        time_line = (
            f"⏳ Thời gian xử lý: "
            f"{format_duration(created_at, completed_at or last_check)}"
        )
    else:
        progress = "Chưa xác định trạng thái 🟡"
        completed_line = ""
        time_line = f"⏳ Thời gian đã theo dõi: {format_duration(created_at)}"

    return (
        f"{status_label(status)}\n\n"
        f"🆔 UID: {fb_id}\n"
        f"👤 Tên: {name}\n"
        f"📝 Ghi chú: {note}\n"
        f"💵 Giá: {price}\n"
        f"🔄 Tiến trình: {progress}\n"
        f"🕘 Khởi tạo: {created_at}\n"
        f"⏰ Cập nhật: {last_check or 'Chưa kiểm tra'}"
        f"{completed_line}\n"
        f"{time_line}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "🤖 LAPTINH FACEBOOK MONITOR\n\n"
        "Theo dõi trạng thái truy cập công khai của Facebook UID.\n"
        "Trạng thái dùng: LIVE / DIE / CHƯA XÁC ĐỊNH.\n\n"
        "Chọn chức năng:",
        reply_markup=keyboard()
    )

async def list_accounts(update: Update):
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT fb_id,name,note,price,status,created_at,last_check,completed_at
        FROM accounts
        ORDER BY created_at DESC
    """)
    rows = cur.fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(
            "📭 Chưa có UID nào đang theo dõi.",
            reply_markup=keyboard()
        )
        return

    for row in rows:
        await update.message.reply_text(
            format_ticket(row),
            reply_markup=keyboard()
        )

async def check_all(update: Update):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT fb_id FROM accounts ORDER BY created_at DESC")
    rows = cur.fetchall()

    if not rows:
        con.close()
        await update.message.reply_text(
            "📭 Chưa có UID nào.",
            reply_markup=keyboard()
        )
        return

    await update.message.reply_text("⏳ Đang kiểm tra...")

    for (fb_id,) in rows:
        status = check_facebook(fb_id)
        now = now_text()

        if status != "UNKNOWN":
            cur.execute("""
                UPDATE accounts
                SET status=?, last_check=?
                WHERE fb_id=?
            """, (status, now, fb_id))
        else:
            cur.execute("""
                UPDATE accounts
                SET last_check=?
                WHERE fb_id=?
            """, (now, fb_id))

        con.commit()
        row = get_account(fb_id)

        await update.message.reply_text(
            format_ticket(row),
            reply_markup=keyboard()
        )

    con.close()

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["chat_id"] = update.effective_chat.id
    text = update.message.text.strip()

    if text == BTN_ADD:
        context.user_data["mode"] = "add_uid"
        await update.message.reply_text(
            "➕ Gửi UID Facebook dạng số."
        )
        return

    if text == BTN_LIST:
        await list_accounts(update)
        return

    if text == BTN_CHECK:
        await check_all(update)
        return

    if text == BTN_REMOVE:
        context.user_data["mode"] = "remove_uid"
        await update.message.reply_text(
            "❌ Gửi UID cần xóa."
        )
        return

    mode = context.user_data.get("mode")

    # Nếu Render/bot vừa khởi động lại làm mất trạng thái hội thoại,
    # người dùng vẫn có thể gửi UID dạng số và bot tiếp tục quy trình thêm UID.
    if mode is None and text.isdigit():
        context.user_data["new_uid"] = text
        context.user_data["mode"] = "add_name"
        await update.message.reply_text(
            "👤 Nhập tên hiển thị cho UID.\n"
            "Nếu không cần, gửi dấu -"
        )
        return

    if mode == "add_uid":
        if not text.isdigit():
            await update.message.reply_text(
                "⚠️ UID phải là dãy số."
            )
            return

        context.user_data["new_uid"] = text
        context.user_data["mode"] = "add_name"
        await update.message.reply_text(
            "👤 Nhập tên hiển thị cho UID.\n"
            "Nếu không cần, gửi dấu -"
        )
        return

    if mode == "add_name":
        context.user_data["new_name"] = "" if text == "-" else text
        context.user_data["mode"] = "add_note"
        await update.message.reply_text(
            "📝 Nhập ghi chú.\n"
            "Nếu không cần, gửi dấu -"
        )
        return

    if mode == "add_note":
        context.user_data["new_note"] = "" if text == "-" else text
        context.user_data["mode"] = "add_price"
        await update.message.reply_text(
            "💵 Nhập giá, ví dụ: 99.999đ\n"
            "Nếu không cần, gửi 0"
        )
        return

    if mode == "add_price":
        fb_id = context.user_data["new_uid"]
        name = context.user_data.get("new_name", "")
        note = context.user_data.get("new_note", "")
        price = text

        await update.message.reply_text("⏳ Đang kiểm tra UID...")

        status = check_facebook(fb_id)
        created = now_text()

        con = db()
        cur = con.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO accounts
            (fb_id,name,note,price,status,created_at,last_check,completed_at)
            VALUES(?,?,?,?,?,?,?,?)
        """, (
            fb_id,
            name,
            note,
            price,
            status,
            created,
            created,
            created if status == "UNAVAILABLE" else None
        ))
        con.commit()
        con.close()

        row = get_account(fb_id)

        context.user_data.clear()

        await update.message.reply_text(
            format_ticket(row),
            reply_markup=keyboard()
        )
        return

    if mode == "remove_uid":
        con = db()
        cur = con.cursor()
        cur.execute(
            "DELETE FROM accounts WHERE fb_id=?",
            (text,)
        )
        deleted = cur.rowcount
        con.commit()
        con.close()

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Đã xóa UID."
            if deleted
            else "⚠️ Không tìm thấy UID này.",
            reply_markup=keyboard()
        )
        return

    await update.message.reply_text(
        "👇 Chọn chức năng bên dưới.",
        reply_markup=keyboard()
    )

async def auto_monitor(context: ContextTypes.DEFAULT_TYPE):
    con = db()
    cur = con.cursor()

    cur.execute("""
        SELECT fb_id,status
        FROM accounts
    """)
    rows = cur.fetchall()

    for fb_id, old_status in rows:
        new_status = check_facebook(fb_id)
        now = now_text()

        if new_status == "UNKNOWN":
            pending_changes.pop(fb_id, None)

            cur.execute("""
                UPDATE accounts
                SET last_check=?
                WHERE fb_id=?
            """, (now, fb_id))
            continue

        if not old_status or new_status == old_status:
            pending_changes.pop(fb_id, None)

            cur.execute("""
                UPDATE accounts
                SET status=?, last_check=?
                WHERE fb_id=?
            """, (
                new_status,
                now,
                fb_id
            ))
            continue

        pending_status, count = pending_changes.get(
            fb_id,
            (None, 0)
        )

        if pending_status == new_status:
            count += 1
        else:
            pending_status = new_status
            count = 1

        pending_changes[fb_id] = (
            pending_status,
            count
        )

        if count < 2:
            continue

        completed_at = now if new_status == "UNAVAILABLE" else None

        cur.execute("""
            UPDATE accounts
            SET status=?, last_check=?, completed_at=?
            WHERE fb_id=?
        """, (
            new_status,
            now,
            completed_at,
            fb_id
        ))

        pending_changes.pop(fb_id, None)
        con.commit()

        row = get_account(fb_id)

        chat_id = context.application.bot_data.get("chat_id")

        if chat_id:
            if new_status == "UNAVAILABLE":
                title = "🚨 UID ĐÃ CHUYỂN TỪ LIVE → DIE"
            else:
                title = "✅ UID ĐÃ CHUYỂN TỪ DIE → LIVE"

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{title}\n\n{format_ticket(row)}"
            )

    con.commit()
    con.close()


def main():
    if not TOKEN:
        raise RuntimeError("Chưa thiết lập BOT_TOKEN trên Render.")

    init_db()

    Thread(
        target=run_web,
        daemon=True
    ).start()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler
        ),
        group=0
    )

    app.job_queue.run_repeating(
        auto_monitor,
        interval=CHECK_SECONDS,
        first=10
    )

    print("Laptinh Facebook Monitor đang hoạt động...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
