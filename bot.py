import os
import sqlite3
import requests
from urllib.parse import quote
from datetime import datetime, timezone, timedelta
from threading import Thread
from flask import Flask

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

TOKEN = os.environ.get("BOT_TOKEN")
DB_FILE = "facebook_pro.db"
CHECK_SECONDS = 30
TRIAL_DAYS = 15
TRIAL_UID_LIMIT = 5
VIP_PRICE = 30000
VIP_DAYS = 30
VIP_UID_LIMIT = 50
VN_TZ = timezone(timedelta(hours=7))

# Trên Render, tạo ADMIN_ID = Telegram numeric user ID của chủ bot.
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)

# Thông tin nhận chuyển khoản. Cấu hình trên Render Environment.
# BANK_BIN: mã BIN ngân hàng, ví dụ MB = 970422, Vietcombank = 970436...
BANK_BIN = os.environ.get("BANK_BIN", "").strip()
BANK_ACCOUNT = os.environ.get("BANK_ACCOUNT", "").strip()
BANK_NAME = os.environ.get("BANK_NAME", "").strip()

BTN_ADD = "➕ Thêm UID"
BTN_LIST = "📋 Danh sách"
BTN_CHECK = "🔎 Kiểm tra ngay"
BTN_REMOVE = "❌ Xóa UID"
BTN_HISTORY = "📜 Lịch sử"
BTN_ACCOUNT = "👤 Tài khoản"
BTN_RENEW = "💳 Gia hạn"

web_app = Flask(__name__)
pending_changes = {}


@web_app.route("/")
def home():
    return "Laptinh FB Monitor PRO is running"


@web_app.route("/health")
def health():
    return "OK"


def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port, use_reloader=False)


def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            telegram_user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            uid_limit INTEGER NOT NULL DEFAULT 5,
            is_active INTEGER NOT NULL DEFAULT 1,
            plan TEXT NOT NULL DEFAULT 'TRIAL'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS monitored_accounts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER NOT NULL,
            fb_id TEXT NOT NULL,
            name TEXT,
            note TEXT,
            price TEXT,
            status TEXT,
            created_at TEXT NOT NULL,
            last_check TEXT,
            cycle_started_at TEXT,
            last_transition_at TEXT,
            UNIQUE(telegram_user_id, fb_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER NOT NULL,
            fb_id TEXT NOT NULL,
            old_status TEXT,
            new_status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            cycle_started_at TEXT,
            duration_seconds INTEGER DEFAULT 0
        )
    """)

    con.commit()
    con.close()


def now_dt():
    return datetime.now(VN_TZ)


def now_text():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(text):
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=VN_TZ)


def add_days_text(base_dt, days):
    return (base_dt + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def status_label(status):
    if status == "AVAILABLE":
        return "✅ LIVE"
    if status == "UNAVAILABLE":
        return "❌ DIE"
    return "🟡 CHƯA XÁC ĐỊNH"


def opposite_label(status):
    if status == "AVAILABLE":
        return "DIE ❌"
    if status == "UNAVAILABLE":
        return "LIVE ✅"
    return "trạng thái xác định"


def check_facebook(fb_id):
    """
    Dùng cùng tín hiệu public Graph profile-picture đã thử nghiệm:
    URL cuối có '100x100' => AVAILABLE.
    Lỗi request => UNKNOWN, không biến lỗi mạng thành DIE.
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
            return "AVAILABLE"

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
            [BTN_CHECK, BTN_REMOVE],
            [BTN_HISTORY, BTN_ACCOUNT],
            [BTN_RENEW],
        ],
        resize_keyboard=True
    )


def ensure_user(update: Update):
    user = update.effective_user
    chat_id = update.effective_chat.id
    uid = user.id

    con = db()
    cur = con.cursor()
    cur.execute(
        "SELECT * FROM users WHERE telegram_user_id=?",
        (uid,)
    )
    row = cur.fetchone()

    if row is None:
        created = now_dt()
        expires = add_days_text(created, TRIAL_DAYS)
        cur.execute("""
            INSERT INTO users(
                telegram_user_id,chat_id,username,full_name,
                created_at,expires_at,uid_limit,is_active,plan
            )
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (
            uid,
            chat_id,
            user.username or "",
            user.full_name or "",
            created.strftime("%Y-%m-%d %H:%M:%S"),
            expires,
            TRIAL_UID_LIMIT,
            1,
            "TRIAL"
        ))
        con.commit()
    else:
        cur.execute("""
            UPDATE users
            SET chat_id=?,username=?,full_name=?
            WHERE telegram_user_id=?
        """, (
            chat_id,
            user.username or "",
            user.full_name or "",
            uid
        ))
        con.commit()

    # ADMIN_ID luôn được nâng thành tài khoản quản trị, không hết hạn.
    if ADMIN_ID and uid == ADMIN_ID:
        admin_expiry = "2099-12-31 23:59:59"
        cur.execute("""
            UPDATE users
            SET plan='ADMIN', uid_limit=100, is_active=1, expires_at=?
            WHERE telegram_user_id=?
        """, (admin_expiry, uid))
        con.commit()

    cur.execute("SELECT * FROM users WHERE telegram_user_id=?", (uid,))
    row = cur.fetchone()
    con.close()
    return row


def subscription_ok(user_row):
    if not user_row or not user_row["is_active"]:
        return False
    expires = parse_dt(user_row["expires_at"])
    return bool(expires and now_dt() <= expires)


def remaining_text(user_row):
    if user_row and user_row["plan"] == "ADMIN":
        return "Không giới hạn"
    expires = parse_dt(user_row["expires_at"])
    if not expires:
        return "0 ngày"
    seconds = int((expires - now_dt()).total_seconds())
    if seconds <= 0:
        return "Đã hết hạn"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    return f"{days} ngày {hours} giờ"


def format_duration_seconds(total):
    total = max(0, int(total or 0))
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


def duration_between(start_text, end_text=None):
    try:
        start = parse_dt(start_text)
        end = parse_dt(end_text) if end_text else now_dt()
        return max(0, int((end - start).total_seconds()))
    except Exception:
        return 0


def get_account(user_id, fb_id):
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT * FROM monitored_accounts
        WHERE telegram_user_id=? AND fb_id=?
    """, (user_id, fb_id))
    row = cur.fetchone()
    con.close()
    return row


def format_ticket(row):
    status = row["status"]
    started = row["cycle_started_at"] or row["created_at"]
    elapsed = format_duration_seconds(duration_between(started))

    return (
        f"{status_label(status)}\n\n"
        f"🆔 UID: {row['fb_id']}\n"
        f"👤 Tên: {row['name'] or 'Chưa cập nhật'}\n"
        f"📝 Ghi chú: {row['note'] or '-'}\n"
        f"💵 Giá: {row['price'] or '0'}\n"
        f"🔄 Tiến trình: Đang theo dõi chờ {opposite_label(status)}\n"
        f"🕘 Bắt đầu chu kỳ: {started}\n"
        f"⏰ Cập nhật: {row['last_check'] or 'Chưa kiểm tra'}\n"
        f"⏳ Đã theo dõi chu kỳ: {elapsed}"
    )


async def require_active(update: Update):
    row = ensure_user(update)
    if subscription_ok(row):
        return row

    await update.message.reply_text(
        "⛔ Tài khoản đã hết hạn hoặc đang bị tạm khóa.\n\n"
        f"📅 Hết hạn: {row['expires_at']}\n"
        "Vui lòng liên hệ quản trị viên để gia hạn.",
        reply_markup=keyboard()
    )
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = ensure_user(update)
    text = (
        "🤖 LAPTINH FB MONITOR PRO\n\n"
        "Theo dõi tín hiệu khả dụng công khai của Facebook UID.\n"
        "LIVE/DIE ở đây không phải trạng thái online/offline riêng tư.\n\n"
        f"🎁 Gói: {row['plan']}\n"
        f"📦 Giới hạn: {row['uid_limit']} UID\n"
        f"📅 Hết hạn: {'Không giới hạn' if row['plan'] == 'ADMIN' else row['expires_at']}\n"
        f"⏳ Còn lại: {remaining_text(row)}\n\n"
        "Chọn chức năng:"
    )
    await update.message.reply_text(text, reply_markup=keyboard())


async def account_info(update: Update):
    row = ensure_user(update)
    con = db()
    cur = con.cursor()
    cur.execute(
        "SELECT COUNT(*) AS c FROM monitored_accounts WHERE telegram_user_id=?",
        (update.effective_user.id,)
    )
    count = cur.fetchone()["c"]
    con.close()

    await update.message.reply_text(
        "👤 TÀI KHOẢN\n\n"
        f"🆔 Telegram ID: {row['telegram_user_id']}\n"
        f"🎫 Gói: {row['plan']}\n"
        f"📦 UID: {count}/{row['uid_limit']}\n"
        f"📅 Bắt đầu: {row['created_at']}\n"
        f"📅 Hết hạn: {'Không giới hạn' if row['plan'] == 'ADMIN' else row['expires_at']}\n"
        f"⏳ Còn lại: {remaining_text(row)}\n"
        f"🔐 Trạng thái: {'Quản trị viên' if row['plan'] == 'ADMIN' else ('Hoạt động' if subscription_ok(row) else 'Tạm dừng')}",
        reply_markup=keyboard()
    )



async def renew_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = ensure_user(update)
    user_id = update.effective_user.id

    if row["plan"] == "ADMIN":
        await update.message.reply_text(
            "👑 Bạn là ADMIN nên tài khoản không cần gia hạn.",
            reply_markup=keyboard()
        )
        return

    transfer_content = f"VIP {user_id}"

    if not BANK_BIN or not BANK_ACCOUNT:
        await update.message.reply_text(
            "⚠️ Quản trị viên chưa cấu hình tài khoản nhận chuyển khoản.\n\n"
            f"🆔 Telegram ID của bạn: {user_id}\n"
            f"📝 Nội dung chuyển khoản: {transfer_content}\n\n"
            "Vui lòng liên hệ quản trị viên.",
            reply_markup=keyboard()
        )
        return

    qr_url = (
        f"https://img.vietqr.io/image/{quote(BANK_BIN)}-"
        f"{quote(BANK_ACCOUNT)}-compact2.png"
        f"?amount={VIP_PRICE}"
        f"&addInfo={quote(transfer_content)}"
        f"&accountName={quote(BANK_NAME)}"
    )

    caption = (
        "💎 NÂNG CẤP / GIA HẠN VIP\n\n"
        f"💰 Giá: {VIP_PRICE:,}đ / {VIP_DAYS} ngày\n".replace(",", ".")
        + f"📦 Giới hạn: {VIP_UID_LIMIT} UID\n"
        + f"🏦 Tài khoản nhận: {BANK_ACCOUNT}\n"
        + f"👤 Chủ tài khoản: {BANK_NAME or 'Chưa cập nhật'}\n"
        + f"🆔 Telegram ID: {user_id}\n"
        + f"📝 Nội dung chuyển khoản: {transfer_content}\n\n"
        "⚠️ Vui lòng giữ nguyên nội dung chuyển khoản để quản trị viên "
        "xác định đúng tài khoản cần gia hạn.\n\n"
        "Sau khi chuyển khoản, gửi ảnh giao dịch cho quản trị viên. "
        "Quản trị viên sẽ gia hạn tài khoản bằng Telegram ID của bạn."
    )

    try:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=qr_url,
            caption=caption
        )
    except Exception as e:
        print("[QR_SEND_ERROR]", type(e).__name__, str(e)[:200], flush=True)
        await update.message.reply_text(
            "⚠️ Không tải được ảnh QR lúc này.\n\n"
            f"🏦 Số tài khoản: {BANK_ACCOUNT}\n"
            f"👤 Chủ tài khoản: {BANK_NAME or 'Chưa cập nhật'}\n"
            f"📝 Nội dung chuyển khoản: {transfer_content}",
            reply_markup=keyboard()
        )


async def list_accounts(update: Update):
    user_id = update.effective_user.id
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT * FROM monitored_accounts
        WHERE telegram_user_id=?
        ORDER BY created_at DESC
    """, (user_id,))
    rows = cur.fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(
            "📭 Bạn chưa theo dõi UID nào.",
            reply_markup=keyboard()
        )
        return

    for row in rows:
        await update.message.reply_text(format_ticket(row), reply_markup=keyboard())


async def show_history(update: Update):
    user_id = update.effective_user.id
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT * FROM history
        WHERE telegram_user_id=?
        ORDER BY id DESC
        LIMIT 20
    """, (user_id,))
    rows = cur.fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(
            "📭 Chưa có lần chuyển trạng thái nào.",
            reply_markup=keyboard()
        )
        return

    parts = ["📜 20 LẦN CHUYỂN TRẠNG THÁI GẦN NHẤT\n"]
    for row in rows:
        parts.append(
            f"🆔 {row['fb_id']}\n"
            f"{status_label(row['old_status'])} → {status_label(row['new_status'])}\n"
            f"🕘 {row['changed_at']}\n"
            f"⏱ {format_duration_seconds(row['duration_seconds'])}\n"
        )

    await update.message.reply_text("\n".join(parts), reply_markup=keyboard())


async def check_all(update: Update):
    user_row = await require_active(update)
    if not user_row:
        return

    user_id = update.effective_user.id
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT fb_id FROM monitored_accounts
        WHERE telegram_user_id=?
        ORDER BY created_at DESC
    """, (user_id,))
    rows = cur.fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("📭 Bạn chưa có UID nào.", reply_markup=keyboard())
        return

    await update.message.reply_text("⏳ Đang kiểm tra...")

    for item in rows:
        fb_id = item["fb_id"]
        status = check_facebook(fb_id)
        now = now_text()

        con = db()
        cur = con.cursor()
        if status != "UNKNOWN":
            cur.execute("""
                UPDATE monitored_accounts
                SET status=?,last_check=?
                WHERE telegram_user_id=? AND fb_id=?
            """, (status, now, user_id, fb_id))
        else:
            cur.execute("""
                UPDATE monitored_accounts
                SET last_check=?
                WHERE telegram_user_id=? AND fb_id=?
            """, (now, user_id, fb_id))
        con.commit()
        con.close()

        row = get_account(user_id, fb_id)
        await update.message.reply_text(format_ticket(row), reply_markup=keyboard())


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if text == BTN_ACCOUNT:
        await account_info(update)
        return

    if text == BTN_RENEW:
        await renew_account(update, context)
        return

    if text == BTN_LIST:
        await list_accounts(update)
        return

    if text == BTN_HISTORY:
        await show_history(update)
        return

    if text == BTN_CHECK:
        await check_all(update)
        return

    if text == BTN_ADD:
        row = await require_active(update)
        if not row:
            return

        con = db()
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*) AS c FROM monitored_accounts WHERE telegram_user_id=?",
            (user_id,)
        )
        count = cur.fetchone()["c"]
        con.close()

        if count >= row["uid_limit"]:
            await update.message.reply_text(
                f"⚠️ Bạn đã dùng đủ {row['uid_limit']} UID của gói hiện tại.",
                reply_markup=keyboard()
            )
            return

        context.user_data["mode"] = "add_uid"
        await update.message.reply_text("➕ Gửi UID Facebook dạng số.")
        return

    if text == BTN_REMOVE:
        context.user_data["mode"] = "remove_uid"
        await update.message.reply_text("❌ Gửi UID cần xóa.")
        return

    mode = context.user_data.get("mode")

    if mode == "add_uid":
        if not text.isdigit():
            await update.message.reply_text("⚠️ UID phải là dãy số.")
            return

        if get_account(user_id, text):
            await update.message.reply_text(
                "⚠️ UID này đã có trong danh sách của bạn.",
                reply_markup=keyboard()
            )
            context.user_data.clear()
            return

        context.user_data["new_uid"] = text
        context.user_data["mode"] = "add_name"
        await update.message.reply_text("👤 Nhập tên hiển thị.\nNếu không cần, gửi dấu -")
        return

    if mode == "add_name":
        context.user_data["new_name"] = "" if text == "-" else text
        context.user_data["mode"] = "add_note"
        await update.message.reply_text("📝 Nhập ghi chú.\nNếu không cần, gửi dấu -")
        return

    if mode == "add_note":
        context.user_data["new_note"] = "" if text == "-" else text
        context.user_data["mode"] = "add_price"
        await update.message.reply_text("💵 Nhập giá, ví dụ: 99.999đ\nNếu không cần, gửi 0")
        return

    if mode == "add_price":
        row = await require_active(update)
        if not row:
            context.user_data.clear()
            return

        fb_id = context.user_data["new_uid"]
        name = context.user_data.get("new_name", "")
        note = context.user_data.get("new_note", "")
        price = text

        await update.message.reply_text("⏳ Đang kiểm tra UID...")
        status = check_facebook(fb_id)
        created = now_text()

        con = db()
        cur = con.cursor()
        try:
            cur.execute("""
                INSERT INTO monitored_accounts(
                    telegram_user_id,fb_id,name,note,price,status,
                    created_at,last_check,cycle_started_at,last_transition_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (
                user_id, fb_id, name, note, price, status,
                created, created, created, None
            ))
            con.commit()
        except sqlite3.IntegrityError:
            con.close()
            context.user_data.clear()
            await update.message.reply_text(
                "⚠️ UID này đã có trong danh sách.",
                reply_markup=keyboard()
            )
            return
        con.close()

        context.user_data.clear()
        await update.message.reply_text(
            format_ticket(get_account(user_id, fb_id)),
            reply_markup=keyboard()
        )
        return

    if mode == "remove_uid":
        con = db()
        cur = con.cursor()
        cur.execute("""
            DELETE FROM monitored_accounts
            WHERE telegram_user_id=? AND fb_id=?
        """, (user_id, text))
        deleted = cur.rowcount
        con.commit()
        con.close()

        pending_changes.pop((user_id, text), None)
        context.user_data.clear()
        await update.message.reply_text(
            "✅ Đã xóa UID." if deleted else "⚠️ Không tìm thấy UID này.",
            reply_markup=keyboard()
        )
        return

    await update.message.reply_text("👇 Chọn chức năng bên dưới.", reply_markup=keyboard())


async def auto_monitor(context: ContextTypes.DEFAULT_TYPE):
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT
            a.id,a.telegram_user_id,a.fb_id,a.status,
            a.cycle_started_at,a.created_at,
            u.chat_id,u.expires_at,u.is_active
        FROM monitored_accounts a
        JOIN users u ON u.telegram_user_id=a.telegram_user_id
    """)
    rows = cur.fetchall()
    con.close()

    for row in rows:
        expires = parse_dt(row["expires_at"])
        if not row["is_active"] or not expires or now_dt() > expires:
            continue

        user_id = row["telegram_user_id"]
        fb_id = row["fb_id"]
        old_status = row["status"]
        new_status = check_facebook(fb_id)
        now = now_text()
        key = (user_id, fb_id)

        if new_status == "UNKNOWN":
            pending_changes.pop(key, None)
            con = db()
            con.execute(
                "UPDATE monitored_accounts SET last_check=? WHERE id=?",
                (now, row["id"])
            )
            con.commit()
            con.close()
            continue

        # Nếu trạng thái ban đầu là UNKNOWN, nhận trạng thái xác định đầu tiên
        # làm mốc bắt đầu chu kỳ, không gửi thông báo chuyển trạng thái.
        if not old_status or old_status == "UNKNOWN":
            pending_changes.pop(key, None)
            con = db()
            con.execute("""
                UPDATE monitored_accounts
                SET status=?,last_check=?,cycle_started_at=?
                WHERE id=?
            """, (new_status, now, now, row["id"]))
            con.commit()
            con.close()
            continue

        if new_status == old_status:
            pending_changes.pop(key, None)
            con = db()
            con.execute(
                "UPDATE monitored_accounts SET last_check=? WHERE id=?",
                (now, row["id"])
            )
            con.commit()
            con.close()
            continue

        pending_status, count = pending_changes.get(key, (None, 0))
        if pending_status == new_status:
            count += 1
        else:
            pending_status, count = new_status, 1

        pending_changes[key] = (pending_status, count)

        # Xác nhận 2 lần liên tiếp để hạn chế báo nhầm.
        if count < 2:
            continue

        cycle_started = row["cycle_started_at"] or row["created_at"]
        duration = duration_between(cycle_started, now)

        con = db()
        cur = con.cursor()
        cur.execute("""
            UPDATE monitored_accounts
            SET status=?,last_check=?,last_transition_at=?,cycle_started_at=?
            WHERE id=?
        """, (new_status, now, now, now, row["id"]))

        cur.execute("""
            INSERT INTO history(
                telegram_user_id,fb_id,old_status,new_status,
                changed_at,cycle_started_at,duration_seconds
            )
            VALUES(?,?,?,?,?,?,?)
        """, (
            user_id, fb_id, old_status, new_status,
            now, cycle_started, duration
        ))
        con.commit()
        con.close()

        pending_changes.pop(key, None)
        account = get_account(user_id, fb_id)

        if new_status == "UNAVAILABLE":
            title = "🚨 HOÀN THÀNH CHU KỲ LIVE → DIE"
        else:
            title = "✅ HOÀN THÀNH CHU KỲ DIE → LIVE"

        message = (
            f"{title}\n\n"
            f"🆔 UID: {fb_id}\n"
            f"👤 Tên: {account['name'] or 'Chưa cập nhật'}\n"
            f"📝 Ghi chú: {account['note'] or '-'}\n"
            f"💵 Giá: {account['price'] or '0'}\n"
            f"🕘 Hoàn thành: {now}\n"
            f"⏱ Thời gian chu kỳ: {format_duration_seconds(duration)}\n\n"
            f"🔄 Chu kỳ mới: Đang theo dõi chờ {opposite_label(new_status)}"
        )

        try:
            await context.bot.send_message(chat_id=row["chat_id"], text=message)
        except Exception as e:
            print("[TG_SEND_ERROR]", user_id, fb_id, type(e).__name__, flush=True)


def is_admin(update: Update):
    return bool(ADMIN_ID and update.effective_user.id == ADMIN_ID)


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Bạn không có quyền quản trị.")
        return

    con = db()
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users")
    users = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM monitored_accounts")
    accounts = cur.fetchone()["c"]
    cur.execute("""
        SELECT COUNT(*) AS c FROM users
        WHERE is_active=1 AND expires_at>=?
    """, (now_text(),))
    active = cur.fetchone()["c"]
    con.close()

    await update.message.reply_text(
        "🛠 ADMIN - LAPTINH FB MONITOR PRO\n\n"
        f"👥 Tổng khách: {users}\n"
        f"✅ Khách còn hạn: {active}\n"
        f"🆔 Tổng UID: {accounts}\n\n"
        "Lệnh quản trị:\n"
        "/users - danh sách khách\n"
        "/vip TELEGRAM_ID - VIP 30 ngày / 50 UID\n"
        "/extend TELEGRAM_ID DAYS - gia hạn tùy số ngày\n"
        "/limit TELEGRAM_ID NUMBER - đổi giới hạn UID\n"
        "/lock TELEGRAM_ID - khóa\n"
        "/unlock TELEGRAM_ID - mở khóa"
    )


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT u.*,
               (SELECT COUNT(*) FROM monitored_accounts a
                WHERE a.telegram_user_id=u.telegram_user_id) AS uid_count
        FROM users u
        ORDER BY u.created_at DESC
        LIMIT 50
    """)
    rows = cur.fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("Chưa có khách.")
        return

    text = ["👥 DANH SÁCH KHÁCH\n"]
    for r in rows:
        text.append(
            f"ID: {r['telegram_user_id']}\n"
            f"Tên: {r['full_name'] or '-'}\n"
            f"Gói: {r['plan']} | UID: {r['uid_count']}/{r['uid_limit']}\n"
            f"Hết hạn: {r['expires_at']}\n"
            f"Trạng thái: {'ON' if r['is_active'] else 'LOCK'}\n"
        )
    await update.message.reply_text("\n".join(text)[:4000])


async def extend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if len(context.args) != 2 or not all(x.isdigit() for x in context.args):
        await update.message.reply_text("Dùng: /extend TELEGRAM_ID DAYS")
        return

    target = int(context.args[0])
    days = int(context.args[1])

    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_user_id=?", (target,))
    row = cur.fetchone()
    if not row:
        con.close()
        await update.message.reply_text("⚠️ Không tìm thấy khách.")
        return

    old_exp = parse_dt(row["expires_at"])
    base = old_exp if old_exp and old_exp > now_dt() else now_dt()
    new_exp = add_days_text(base, days)

    cur.execute("""
        UPDATE users
        SET expires_at=?,is_active=1,plan='VIP',uid_limit=?
        WHERE telegram_user_id=?
    """, (new_exp, VIP_UID_LIMIT, target))
    con.commit()
    con.close()

    await update.message.reply_text(f"✅ Đã gia hạn {days} ngày.\nHết hạn mới: {new_exp}")



async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kích hoạt/gia hạn đúng gói VIP mặc định: 30.000đ / 30 ngày / 50 UID."""
    if not is_admin(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Dùng: /vip TELEGRAM_ID")
        return

    target = int(context.args[0])
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_user_id=?", (target,))
    row = cur.fetchone()
    if not row:
        con.close()
        await update.message.reply_text("⚠️ Không tìm thấy khách.")
        return

    old_exp = parse_dt(row["expires_at"])
    base = old_exp if old_exp and old_exp > now_dt() else now_dt()
    new_exp = add_days_text(base, VIP_DAYS)

    cur.execute("""
        UPDATE users
        SET expires_at=?,is_active=1,plan='VIP',uid_limit=?
        WHERE telegram_user_id=?
    """, (new_exp, VIP_UID_LIMIT, target))
    con.commit()
    con.close()

    await update.message.reply_text(
        f"💎 Đã kích hoạt/gia hạn VIP cho {target}\n"
        f"📦 Giới hạn: {VIP_UID_LIMIT} UID\n"
        f"📅 Cộng: {VIP_DAYS} ngày\n"
        f"⏰ Hết hạn mới: {new_exp}"
    )


async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if len(context.args) != 2 or not all(x.isdigit() for x in context.args):
        await update.message.reply_text("Dùng: /limit TELEGRAM_ID NUMBER")
        return

    target, limit_n = int(context.args[0]), int(context.args[1])
    con = db()
    cur = con.cursor()
    cur.execute(
        "UPDATE users SET uid_limit=? WHERE telegram_user_id=?",
        (limit_n, target)
    )
    changed = cur.rowcount
    con.commit()
    con.close()
    await update.message.reply_text(
        f"✅ Đã đặt giới hạn {limit_n} UID." if changed else "⚠️ Không tìm thấy khách."
    )


async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Dùng: /lock TELEGRAM_ID")
        return
    target = int(context.args[0])
    con = db()
    cur = con.cursor()
    cur.execute("UPDATE users SET is_active=0 WHERE telegram_user_id=?", (target,))
    changed = cur.rowcount
    con.commit()
    con.close()
    await update.message.reply_text("🔒 Đã khóa." if changed else "⚠️ Không tìm thấy khách.")


async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Dùng: /unlock TELEGRAM_ID")
        return
    target = int(context.args[0])
    con = db()
    cur = con.cursor()
    cur.execute("UPDATE users SET is_active=1 WHERE telegram_user_id=?", (target,))
    changed = cur.rowcount
    con.commit()
    con.close()
    await update.message.reply_text("🔓 Đã mở khóa." if changed else "⚠️ Không tìm thấy khách.")


def main():
    if not TOKEN:
        raise RuntimeError("Chưa thiết lập BOT_TOKEN trên Render.")

    init_db()
    Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("extend", extend_cmd))
    app.add_handler(CommandHandler("vip", vip_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler),
        group=0
    )

    app.job_queue.run_repeating(
        auto_monitor,
        interval=CHECK_SECONDS,
        first=10
    )

    print("Laptinh FB Monitor PRO đang hoạt động...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
