import os
import sqlite3
import requests
import re
import html
from urllib.parse import quote
from datetime import datetime, timezone, timedelta
from threading import Thread
from flask import Flask

from telegram import Update, ReplyKeyboardMarkup, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

TOKEN = os.environ.get("BOT_TOKEN")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "facebook_pro.db")
CHECK_SECONDS = 30
TRIAL_DAYS = 30
TRIAL_UID_LIMIT = 5
VIP_PRICE = 20000
VIP_DAYS = 30
VIP_UID_LIMIT = 1000
VN_TZ = timezone(timedelta(hours=7))

# ADMIN_ID = Telegram numeric user ID của chủ bot.
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)

# Thông tin nhận chuyển khoản. Cấu hình bằng biến môi trường/.env.
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
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS expiry_reminders(
            telegram_user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            expiry_key TEXT NOT NULL,
            reminder_key TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (telegram_user_id, plan, expiry_key, reminder_key)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_user_id INTEGER NOT NULL,
            admin_user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
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


def _looks_like_default_avatar(response):
    """
    Lọc ảnh mặc định/placeholder theo URL, header và kích thước dữ liệu.
    Mục tiêu: thà bỏ ảnh còn hơn gửi avatar trắng/default.
    """
    url = (response.url or "").lower()
    ctype = (response.headers.get("content-type") or "").lower()
    clen = response.headers.get("content-length")

    default_markers = (
        "static.xx.fbcdn.net",
        "silhouette",
        "default",
        "unknown",
        "anon",
        "blank",
        "placeholder",
    )

    if any(marker in url for marker in default_markers):
        return True

    if not ctype.startswith("image/"):
        return True

    try:
        if clen and int(clen) < 2500:
            return True
    except (TypeError, ValueError):
        pass

    return False


def _decode_fb_url(text):
    if not text:
        return None

    value = html.unescape(str(text))

    # Facebook JSON/HTML thường escape URL theo nhiều lớp.
    for _ in range(3):
        old = value
        value = (
            value
            .replace("\\u0025", "%")
            .replace("\\u0026", "&")
            .replace("\\u003D", "=")
            .replace("\\u003d", "=")
            .replace("\\u002F", "/")
            .replace("\\u002f", "/")
            .replace("\\/", "/")
            .replace("\\\\/", "/")
            .replace("\\\\u0026", "&")
            .replace("\\\\u003d", "=")
        )
        if value == old:
            break

    return value


def _avatar_score(url):
    """Ưu tiên URL có dấu hiệu là ảnh profile Facebook."""
    low = (url or "").lower()
    score = 0
    if "fbcdn.net" in low:
        score += 10
    if "t39.30808-1" in low:
        score += 30
    if "profile" in low:
        score += 20
    if "scontent" in low:
        score += 5
    if "p120x120" in low or "s120x120" in low:
        score += 5
    return score


def _extract_profile_picture_from_public_page(fb_id):
    """
    V9: tìm profile_picture.uri trong dữ liệu HTML/JSON công khai.
    Không dùng cookie, access token hay phiên đăng nhập.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }

    public_urls = (
        f"https://www.facebook.com/{fb_id}",
        f"https://m.facebook.com/{fb_id}",
    )

    for page_url in public_urls:
        try:
            r = requests.get(
                page_url,
                headers=headers,
                timeout=20,
                allow_redirects=True
            )
            body = r.text or ""
            final_url = (r.url or "").lower()

            print(
                "[PUBLIC_PROFILE]",
                f"uid={fb_id}",
                f"http={r.status_code}",
                f"final_url={r.url}",
                f"body_len={len(body)}",
                flush=True
            )

            if any(x in final_url for x in ("/checkpoint", "/challenge")):
                print("[PUBLIC_PROFILE_BLOCKED]", fb_id, r.url, flush=True)
                continue

            candidates = []

            # 1. Bám trực tiếp vào profile_picture rồi tìm uri gần đó.
            for m in re.finditer(r'profile_picture', body, flags=re.I):
                chunk = body[m.start():m.start() + 2500]

                uri_patterns = (
                    r'["\\]uri["\\]\s*:\s*["\\]([^"\\]{20,})',
                    r'"uri"\s*:\s*"([^"]{20,})"',
                    r'\\"uri\\"\s*:\s*\\"([^"]{20,})',
                )

                for pat in uri_patterns:
                    for raw in re.findall(pat, chunk, flags=re.I | re.S):
                        candidate = _decode_fb_url(raw)
                        if candidate and "fbcdn.net" in candidate.lower():
                            candidates.append(candidate)

            # 2. Fallback: tìm trực tiếp URL CDN loại ảnh profile.
            cdn_patterns = (
                r'https?:\\?/\\?/[^"\'<>\s]*fbcdn\.net[^"\'<>\s]*t39\.30808-1[^"\'<>\s]+',
                r'https?://[^"\'<>\s]*fbcdn\.net[^"\'<>\s]*t39\.30808-1[^"\'<>\s]+',
            )

            for pat in cdn_patterns:
                for raw in re.findall(pat, body, flags=re.I):
                    candidate = _decode_fb_url(raw)
                    if candidate:
                        candidates.append(candidate)

            # Loại trùng và ưu tiên ứng viên giống ảnh profile nhất.
            unique = []
            seen = set()
            for candidate in candidates:
                candidate = candidate.rstrip("\\")
                if candidate not in seen:
                    seen.add(candidate)
                    unique.append(candidate)

            unique.sort(key=_avatar_score, reverse=True)

            print(
                "[PUBLIC_AVATAR_FOUND]",
                f"uid={fb_id}",
                f"count={len(unique)}",
                flush=True
            )

            for candidate in unique[:10]:
                validated = _validate_avatar_url(candidate)
                if validated:
                    print(
                        "[AVATAR_OK_PUBLIC]",
                        f"uid={fb_id}",
                        f"url={validated}",
                        flush=True
                    )
                    return validated

        except requests.RequestException as e:
            print(
                "[PUBLIC_PROFILE_ERROR]",
                f"uid={fb_id}",
                type(e).__name__,
                str(e)[:180],
                flush=True
            )

    return None


def _validate_avatar_url(url):
    if not url:
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        )
    }

    try:
        r = requests.get(
            url,
            headers=headers,
            timeout=15,
            allow_redirects=True,
            stream=True
        )

        if r.status_code != 200:
            return None

        if _looks_like_default_avatar(r):
            print("[AVATAR_REJECTED]", r.url, flush=True)
            return None

        return r.url

    except requests.RequestException as e:
        print("[AVATAR_VALIDATE_ERROR]", type(e).__name__, str(e)[:180], flush=True)
        return None


def get_live_avatar_url(fb_id):
    """
    V8:
    1) Xác nhận UID LIVE bằng Graph như logic cũ.
    2) Ưu tiên avatar thật từ dữ liệu public profile.
    3) Nếu không lấy được, fallback sang Graph picture nhiều kích thước.
    4) Nếu vẫn là ảnh trắng/default thì không gửi ảnh.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        )
    }

    try:
        probe = requests.get(
            f"https://graph.facebook.com/{fb_id}/picture?type=normal",
            headers=headers,
            timeout=15,
            allow_redirects=True
        )

        if "100x100" not in (probe.url or "").lower():
            return None

    except requests.RequestException as e:
        print("[AVATAR_LIVE_PROBE_ERROR]", fb_id, type(e).__name__, flush=True)
        return None

    # Nguồn 1: dữ liệu public profile thật.
    public_avatar = _extract_profile_picture_from_public_page(fb_id)
    if public_avatar:
        return public_avatar

    # Nguồn 2: Graph fallback.
    candidates = (
        f"https://graph.facebook.com/{fb_id}/picture?width=800&height=800",
        f"https://graph.facebook.com/{fb_id}/picture?width=500&height=500",
        f"https://graph.facebook.com/{fb_id}/picture?type=large",
        f"https://graph.facebook.com/{fb_id}/picture?type=normal",
    )

    for candidate in candidates:
        validated = _validate_avatar_url(candidate)
        if validated:
            print("[AVATAR_OK_GRAPH]", f"uid={fb_id}", f"url={validated}", flush=True)
            return validated

    print("[AVATAR_NONE]", f"uid={fb_id}", flush=True)
    return None


async def send_account_ticket(message, row):
    """LIVE: ưu tiên gửi avatar + ticket. DIE/UNKNOWN: gửi ticket chữ như cũ."""
    ticket = format_ticket(row)

    if row["status"] == "AVAILABLE":
        avatar_url = get_live_avatar_url(row["fb_id"])
        if avatar_url:
            try:
                await message.reply_photo(
                    photo=avatar_url,
                    caption=ticket,
                    parse_mode="HTML",
                    reply_markup=keyboard()
                )
                return
            except Exception as e:
                print("[AVATAR_SEND_ERROR]", row["fb_id"], type(e).__name__, flush=True)

    await message.reply_text(ticket, parse_mode="HTML", reply_markup=keyboard())


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



def fb_profile_url(fb_id):
    """Tạo link profile Facebook công khai theo UID."""
    return f"https://www.facebook.com/profile.php?id={str(fb_id).strip()}"


def fb_uid_link(fb_id):
    """UID màu xanh, bấm được trong Telegram HTML."""
    uid = html.escape(str(fb_id).strip())
    url = html.escape(fb_profile_url(fb_id), quote=True)
    return f'<a href="{url}">{uid}</a>'


def format_ticket(row):
    status = row["status"]
    started = row["cycle_started_at"] or row["created_at"]
    elapsed = format_duration_seconds(duration_between(started))

    name = html.escape(str(row["name"] or "Chưa cập nhật"))
    note = html.escape(str(row["note"] or "-"))
    price = html.escape(str(row["price"] or "0"))
    started_text = html.escape(str(started))
    last_check = html.escape(str(row["last_check"] or "Chưa kiểm tra"))

    return (
        f"{status_label(status)}\n\n"
        f"🆔 UID: {fb_uid_link(row['fb_id'])}\n"
        f"👤 Tên: {name}\n"
        f"📝 Ghi chú: {note}\n"
        f"💵 Giá: {price}\n"
        f"🔄 Tiến trình: Đang theo dõi chờ {opposite_label(status)}\n"
        f"🕘 Bắt đầu chu kỳ: {started_text}\n"
        f"⏰ Cập nhật: {last_check}\n"
        f"⏳ Đã theo dõi chu kỳ: {elapsed}\n"
        f"🔗 <a href=\"{html.escape(fb_profile_url(row['fb_id']), quote=True)}\">Mở Facebook</a>"
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



def reminder_already_sent(user_id, plan, expiry_key, reminder_key):
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT 1 FROM expiry_reminders
        WHERE telegram_user_id=? AND plan=? AND expiry_key=? AND reminder_key=?
        LIMIT 1
    """, (user_id, plan, expiry_key, reminder_key))
    found = cur.fetchone() is not None
    con.close()
    return found


def mark_reminder_sent(user_id, plan, expiry_key, reminder_key):
    con = db()
    cur = con.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO expiry_reminders(
            telegram_user_id, plan, expiry_key, reminder_key, sent_at
        )
        VALUES(?,?,?,?,?)
    """, (user_id, plan, expiry_key, reminder_key, now_text()))
    con.commit()
    con.close()


async def expiry_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Nhắc hạn 7 ngày, 3 ngày, 1 ngày và khi hết hạn; mỗi mốc chỉ gửi 1 lần."""
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT telegram_user_id, chat_id, plan, expires_at, is_active
        FROM users
        WHERE plan IN ('TRIAL', 'VIP')
    """)
    users = cur.fetchall()
    con.close()

    current = now_dt()

    for row in users:
        user_id = row["telegram_user_id"]
        chat_id = row["chat_id"]
        plan = row["plan"]
        expires = parse_dt(row["expires_at"])

        if not expires:
            continue

        expiry_key = row["expires_at"]
        seconds_left = int((expires - current).total_seconds())

        if seconds_left <= 0:
            reminder_key = "expired"
            if reminder_already_sent(user_id, plan, expiry_key, reminder_key):
                continue

            if plan == "TRIAL":
                text = (
                    "⛔ <b>GÓI DÙNG THỬ ĐÃ HẾT HẠN</b>\n\n"
                    "🎁 Thời gian dùng thử của bạn đã kết thúc.\n"
                    f"💎 Nâng cấp VIP chỉ <b>{VIP_PRICE:,}đ/tháng</b>, "
                    f"được theo dõi tối đa <b>{VIP_UID_LIMIT} UID</b>.\n"
                    "❤️ Việc nâng cấp cũng giúp ủng hộ chi phí duy trì hệ thống.\n\n"
                    "👉 Bấm <b>💳 Gia hạn</b> để xem thông tin nâng cấp."
                ).replace(",", ".")
            else:
                text = (
                    "⛔ <b>GÓI VIP ĐÃ HẾT HẠN</b>\n\n"
                    "💎 Gói VIP của bạn đã hết hạn.\n"
                    "🔔 Việc theo dõi UID sẽ tạm dừng cho đến khi tài khoản được gia hạn.\n"
                    "Xin vui lòng gia hạn để không ảnh hưởng đến công việc.\n\n"
                    "👉 Bấm <b>💳 Gia hạn</b> để xem thông tin gia hạn."
                )

            try:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                mark_reminder_sent(user_id, plan, expiry_key, reminder_key)
            except Exception as e:
                print("[EXPIRY_REMINDER_SEND_ERROR]", user_id, reminder_key, type(e).__name__, flush=True)
            continue

        days_left = max(1, (seconds_left + 86399) // 86400)
        if days_left not in (7, 3, 1):
            continue

        reminder_key = f"{days_left}d"
        if reminder_already_sent(user_id, plan, expiry_key, reminder_key):
            continue

        if plan == "TRIAL":
            text = (
                "⏰ <b>THÔNG BÁO GÓI DÙNG THỬ</b>\n\n"
                f"🎁 Bạn còn <b>{days_left} ngày dùng thử</b> LAPTINH FB MONITOR PRO.\n"
                f"💎 Hãy nâng cấp gói VIP chỉ <b>{VIP_PRICE:,}đ/tháng</b>, "
                f"được dùng tối đa <b>{VIP_UID_LIMIT} UID</b> "
                "để tiếp tục sử dụng và ủng hộ chi phí duy trì hệ thống.\n\n"
                "👉 Bấm <b>💳 Gia hạn</b> để xem thông tin nâng cấp."
            ).replace(",", ".")
        else:
            text = (
                "⚠️ <b>GÓI VIP SẮP HẾT HẠN</b>\n\n"
                f"💎 Gói VIP của bạn còn <b>{days_left} ngày nữa là hết hạn</b>.\n"
                f"📅 Ngày hết hạn: <b>{html.escape(str(row['expires_at']))}</b>\n"
                "🔔 Xin vui lòng gia hạn để không ảnh hưởng đến công việc.\n\n"
                "👉 Bấm <b>💳 Gia hạn</b> để xem thông tin gia hạn."
            )

        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            mark_reminder_sent(user_id, plan, expiry_key, reminder_key)
        except Exception as e:
            print("[EXPIRY_REMINDER_SEND_ERROR]", user_id, reminder_key, type(e).__name__, flush=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = ensure_user(update)
    plan = row["plan"]

    if plan == "ADMIN":
        text = (
            "👑 <b>LAPTINH FB MONITOR PRO - ADMIN</b>\n\n"
            "✅ Tài khoản quản trị viên đang hoạt động.\n"
            f"📦 Giới hạn hiện tại: <b>{row['uid_limit']} UID</b>\n"
            "📅 Thời hạn: <b>Không giới hạn</b>\n\n"
            "👇 Chọn chức năng bên dưới để bắt đầu."
        )
    elif plan == "VIP":
        text = (
            "💎 <b>CHÀO MỪNG THÀNH VIÊN VIP</b>\n\n"
            "✅ Tài khoản của bạn đang sử dụng <b>gói VIP</b>.\n"
            f"📦 Theo dõi tối đa: <b>{row['uid_limit']} UID</b>\n"
            f"📅 Hết hạn: <b>{html.escape(str(row['expires_at']))}</b>\n"
            f"⏳ Còn lại: <b>{html.escape(remaining_text(row))}</b>\n\n"
            "❤️ Cảm ơn bạn đã sử dụng <b>LAPTINH FB MONITOR PRO</b>.\n"
            "👇 Chọn chức năng bên dưới để bắt đầu."
        )
    else:
        text = (
            "👋 <b>Chào mừng bạn đến với LAPTINH FB MONITOR PRO</b>\n\n"
            f"🎁 Bạn được dùng thử: <b>{TRIAL_DAYS} ngày – tối đa {TRIAL_UID_LIMIT} UID</b>.\n"
            f"💎 Nâng cấp chỉ với giá <b>{VIP_PRICE:,}đ/tháng</b>, "
            f"được dùng tối đa <b>{VIP_UID_LIMIT} UID</b>.\n\n"
            f"📅 Hết hạn dùng thử: <b>{html.escape(str(row['expires_at']))}</b>\n"
            f"⏳ Còn lại: <b>{html.escape(remaining_text(row))}</b>\n\n"
            "🚀 <b>BẮT ĐẦU SỬ DỤNG</b>\n"
            "1️⃣ Bấm <b>➕ Thêm UID</b>\n"
            "2️⃣ Gửi UID hoặc link Facebook có UID số\n"
            "3️⃣ Nhập tên, ghi chú và giá nếu cần\n"
            "4️⃣ Bot sẽ tự động theo dõi LIVE/DIE\n\n"
            "💡 Dùng /help để xem thêm hướng dẫn."
        ).replace(",", ".")

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=keyboard()
    )


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
        await send_account_ticket(update.message, row)


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
            f"🆔 {fb_uid_link(row['fb_id'])}\n"
            f"{status_label(row['old_status'])} → {status_label(row['new_status'])}\n"
            f"🕘 {row['changed_at']}\n"
            f"⏱ {format_duration_seconds(row['duration_seconds'])}\n"
        )

    await update.message.reply_text("\n".join(parts), parse_mode="HTML", reply_markup=keyboard())


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
        await send_account_ticket(update.message, row)


def normalize_uid_input(text):
    """
    Chấp nhận:
    - UID số
    - facebook.com/UID
    - https://facebook.com/UID
    - www.facebook.com/UID
    Chưa tự phân giải username -> UID nếu URL không chứa UID số.
    """
    raw = (text or "").strip()

    if raw.isdigit():
        return raw

    m = re.search(
        r'(?:https?://)?(?:www\.|m\.)?facebook\.com/(\d{5,})',
        raw,
        flags=re.I
    )
    if m:
        return m.group(1)

    return None


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    mode = context.user_data.get("mode")

    # Các bước nhập liệu riêng cho ADMIN.
    if is_admin(update) and mode == "admin_search_customer":
        q = text.lstrip("@").strip()
        like = f"%{q}%"
        con = db()
        cur = con.cursor()
        if q.isdigit():
            cur.execute("""
                SELECT u.*,
                       (SELECT COUNT(*) FROM monitored_accounts a
                        WHERE a.telegram_user_id=u.telegram_user_id) AS uid_count
                FROM users u
                WHERE CAST(u.telegram_user_id AS TEXT)=?
                   OR COALESCE(u.full_name,'') LIKE ? COLLATE NOCASE
                   OR COALESCE(u.username,'') LIKE ? COLLATE NOCASE
                ORDER BY u.created_at DESC
                LIMIT 10
            """, (q, like, like))
        else:
            cur.execute("""
                SELECT u.*,
                       (SELECT COUNT(*) FROM monitored_accounts a
                        WHERE a.telegram_user_id=u.telegram_user_id) AS uid_count
                FROM users u
                WHERE COALESCE(u.full_name,'') LIKE ? COLLATE NOCASE
                   OR COALESCE(u.username,'') LIKE ? COLLATE NOCASE
                ORDER BY u.created_at DESC
                LIMIT 10
            """, (like, like))
        rows = cur.fetchall()
        con.close()
        context.user_data.clear()

        if not rows:
            await update.message.reply_text(
                "📭 Không tìm thấy khách phù hợp.",
                reply_markup=admin_menu_markup()
            )
            return

        buttons = []
        for r in rows:
            name = (r["full_name"] or r["username"] or str(r["telegram_user_id"]))[:28]
            buttons.append([
                InlineKeyboardButton(
                    f"👤 {name} • {r['plan']} • {r['uid_count']}/{r['uid_limit']}",
                    callback_data=f"adm:user:{r['telegram_user_id']}"
                )
            ])
        buttons.append([InlineKeyboardButton("🏠 Menu ADMIN", callback_data="adm:home")])
        await update.message.reply_text(
            f"🔎 Tìm thấy <b>{len(rows)}</b> khách:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if is_admin(update) and mode == "admin_extend_days":
        target = context.user_data.get("admin_target")
        if not text.isdigit() or int(text) < 1 or int(text) > 3650:
            await update.message.reply_text("⚠️ Hãy gửi số ngày từ 1 đến 3650.")
            return

        days = int(text)
        row = admin_customer_row(target)
        if not row:
            context.user_data.clear()
            await update.message.reply_text("⚠️ Không tìm thấy khách.", reply_markup=admin_menu_markup())
            return

        old_exp = parse_dt(row["expires_at"])
        base = old_exp if old_exp and old_exp > now_dt() else now_dt()
        new_exp = add_days_text(base, days)

        con = db()
        cur = con.cursor()
        cur.execute("""
            UPDATE users
            SET expires_at=?, is_active=1, plan='VIP'
            WHERE telegram_user_id=?
        """, (new_exp, target))
        con.commit()
        con.close()

        admin_log(target, user_id, "EXTEND", f"+{days} ngày; hết hạn={new_exp}")
        context.user_data.clear()
        row = admin_customer_row(target)
        await update.message.reply_text(
            "✅ <b>ĐÃ GIA HẠN</b>\n\n" + customer_card_text(row),
            parse_mode="HTML",
            reply_markup=customer_actions_markup(row)
        )
        return

    if is_admin(update) and mode == "admin_set_limit":
        target = context.user_data.get("admin_target")
        if not text.isdigit() or int(text) < 1 or int(text) > 100000:
            await update.message.reply_text("⚠️ Hãy gửi giới hạn UID từ 1 đến 100000.")
            return

        limit_n = int(text)
        con = db()
        cur = con.cursor()
        cur.execute("UPDATE users SET uid_limit=? WHERE telegram_user_id=?", (limit_n, target))
        changed = cur.rowcount
        con.commit()
        con.close()

        context.user_data.clear()
        if not changed:
            await update.message.reply_text("⚠️ Không tìm thấy khách.", reply_markup=admin_menu_markup())
            return

        admin_log(target, user_id, "LIMIT", f"uid_limit={limit_n}")
        row = admin_customer_row(target)
        await update.message.reply_text(
            "✅ <b>ĐÃ ĐỔI GIỚI HẠN UID</b>\n\n" + customer_card_text(row),
            parse_mode="HTML",
            reply_markup=customer_actions_markup(row)
        )
        return

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
        await update.message.reply_text("➕ Gửi UID hoặc link Facebook có UID số.\nVí dụ:\n100003606221946\nhttps://facebook.com/100003606221946")
        return

    if text == BTN_REMOVE:
        context.user_data["mode"] = "remove_uid"
        await update.message.reply_text("❌ Gửi UID cần xóa.")
        return

    mode = context.user_data.get("mode")

    if mode == "add_uid":
        parsed_uid = normalize_uid_input(text)
        if not parsed_uid:
            await update.message.reply_text(
                "⚠️ Chưa lấy được UID. Hãy gửi UID số hoặc link Facebook có UID số."
            )
            return

        text = parsed_uid

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
        await send_account_ticket(
            update.message,
            get_account(user_id, fb_id)
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
            f"🆔 UID: {fb_uid_link(fb_id)}\n"
            f"👤 Tên: {html.escape(str(account['name'] or 'Chưa cập nhật'))}\n"
            f"📝 Ghi chú: {html.escape(str(account['note'] or '-'))}\n"
            f"💵 Giá: {html.escape(str(account['price'] or '0'))}\n"
            f"🕘 Hoàn thành: {html.escape(str(now))}\n"
            f"⏱ Thời gian chu kỳ: {format_duration_seconds(duration)}\n\n"
            f"🔄 Chu kỳ mới: Đang theo dõi chờ {opposite_label(new_status)}\n"
            f"🔗 <a href=\"{html.escape(fb_profile_url(fb_id), quote=True)}\">Mở Facebook</a>"
        )

        try:
            if new_status == "AVAILABLE":
                avatar_url = get_live_avatar_url(fb_id)
                if avatar_url:
                    try:
                        await context.bot.send_photo(
                            chat_id=row["chat_id"],
                            photo=avatar_url,
                            caption=message,
                            parse_mode="HTML"
                        )
                    except Exception:
                        await context.bot.send_message(chat_id=row["chat_id"], text=message, parse_mode="HTML")
                else:
                    await context.bot.send_message(chat_id=row["chat_id"], text=message, parse_mode="HTML")
            else:
                await context.bot.send_message(chat_id=row["chat_id"], text=message, parse_mode="HTML")
        except Exception as e:
            print("[TG_SEND_ERROR]", user_id, fb_id, type(e).__name__, flush=True)



async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    text = (
        "📚 HƯỚNG DẪN SỬ DỤNG\n\n"
        "/start - Khởi động bot\n"
        "/help - Hướng dẫn sử dụng\n"
        "/add - Thêm Facebook UID\n"
        "/list - Danh sách UID đang theo dõi\n"
        "/search TỪ_KHÓA - Tìm theo UID / tên / ghi chú\n"
        "/check - Kiểm tra ngay toàn bộ UID\n"
        "/history - Lịch sử chuyển trạng thái\n"
        "/remove - Xóa UID\n"
        "/account - Thông tin tài khoản\n"
        "/renew - Nâng cấp / gia hạn VIP\n\n"
        "ℹ️ LIVE/DIE là tín hiệu khả dụng công khai của UID, "
        "không phải trạng thái online/offline riêng tư."
    )
    if is_admin(update):
        text += "\n\n🛠 Quản trị viên: dùng /admin để xem lệnh quản trị."
    await update.message.reply_text(text, reply_markup=keyboard())


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = await require_active(update)
    if not row:
        return

    user_id = update.effective_user.id
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

    context.user_data.clear()
    context.user_data["mode"] = "add_uid"
    await update.message.reply_text(
        "➕ Gửi UID hoặc link Facebook có UID số.\n"
        "Ví dụ:\n100003606221946\n"
        "https://facebook.com/100003606221946"
    )


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    context.user_data.clear()
    context.user_data["mode"] = "remove_uid"
    await update.message.reply_text("❌ Gửi UID cần xóa.", reply_markup=keyboard())


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    await list_accounts(update)


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await check_all(update)


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    await show_history(update)


async def account_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await account_info(update)


async def renew_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await renew_account(update, context)


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    query = " ".join(context.args).strip()

    if not query:
        await update.message.reply_text(
            "🔎 Dùng: /search TỪ_KHÓA\n"
            "Có thể tìm theo UID, tên hoặc ghi chú.",
            reply_markup=keyboard()
        )
        return

    user_id = update.effective_user.id
    like = f"%{query}%"

    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT * FROM monitored_accounts
        WHERE telegram_user_id=?
          AND (
              fb_id LIKE ? COLLATE NOCASE
              OR COALESCE(name, '') LIKE ? COLLATE NOCASE
              OR COALESCE(note, '') LIKE ? COLLATE NOCASE
          )
        ORDER BY created_at DESC
        LIMIT 20
    """, (user_id, like, like, like))
    rows = cur.fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(
            f"📭 Không tìm thấy kết quả cho: {query}",
            reply_markup=keyboard()
        )
        return

    await update.message.reply_text(
        f"🔎 Tìm thấy {len(rows)} kết quả cho: {query}",
        reply_markup=keyboard()
    )
    for row in rows:
        await send_account_ticket(update.message, row)


async def setup_commands(app):
    commands = [
        BotCommand("start", "🚀 Khởi động bot"),
        BotCommand("help", "📚 Hướng dẫn sử dụng"),
        BotCommand("add", "➕ Thêm tài khoản Facebook"),
        BotCommand("list", "📋 Danh sách Facebook"),
        BotCommand("search", "🔎 Tìm UID theo tên/note"),
        BotCommand("check", "🔍 Kiểm tra ngay"),
        BotCommand("history", "📜 Lịch sử chuyển trạng thái"),
        BotCommand("remove", "❌ Xóa tài khoản"),
        BotCommand("account", "👤 Thông tin tài khoản"),
        BotCommand("renew", "💳 Nâng cấp / gia hạn VIP"),
    ]
    await app.bot.set_my_commands(commands)



def admin_log(target_user_id, admin_user_id, action, details=""):
    con = db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO admin_audit(target_user_id, admin_user_id, action, details, created_at)
        VALUES(?,?,?,?,?)
    """, (target_user_id, admin_user_id, action, details, now_text()))
    con.commit()
    con.close()


def admin_customer_row(target_user_id):
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT u.*,
               (SELECT COUNT(*) FROM monitored_accounts a
                WHERE a.telegram_user_id=u.telegram_user_id) AS uid_count
        FROM users u
        WHERE u.telegram_user_id=?
    """, (target_user_id,))
    row = cur.fetchone()
    con.close()
    return row


def admin_menu_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Khách hàng", callback_data="adm:list:0"),
            InlineKeyboardButton("🔎 Tìm khách", callback_data="adm:search")
        ],
        [
            InlineKeyboardButton("⏳ Sắp hết hạn", callback_data="adm:expiring:0"),
            InlineKeyboardButton("🔒 Đang khóa", callback_data="adm:locked:0")
        ],
        [
            InlineKeyboardButton("📊 Thống kê", callback_data="adm:stats")
        ]
    ])


def customer_actions_markup(row):
    uid = row["telegram_user_id"]
    lock_label = "🔓 Mở khóa" if not row["is_active"] else "🔒 Khóa"
    lock_action = "unlock" if not row["is_active"] else "lock"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💎 VIP +30 ngày", callback_data=f"adm:vip:{uid}"),
            InlineKeyboardButton("📅 Gia hạn khác", callback_data=f"adm:extend:{uid}")
        ],
        [
            InlineKeyboardButton("📦 Đổi giới hạn", callback_data=f"adm:limit:{uid}"),
            InlineKeyboardButton(lock_label, callback_data=f"adm:{lock_action}:{uid}")
        ],
        [
            InlineKeyboardButton("📋 Xem UID", callback_data=f"adm:uids:{uid}"),
            InlineKeyboardButton("🧾 Lịch sử", callback_data=f"adm:audit:{uid}")
        ],
        [
            InlineKeyboardButton("🔄 Làm mới", callback_data=f"adm:user:{uid}"),
            InlineKeyboardButton("⬅️ Danh sách", callback_data="adm:list:0")
        ]
    ])


def customer_card_text(row):
    if not row:
        return "⚠️ Không tìm thấy khách hàng."

    expires = parse_dt(row["expires_at"])
    if row["plan"] == "ADMIN":
        remain = "Không giới hạn"
    elif not row["is_active"]:
        remain = "Đang khóa"
    elif not expires or expires < now_dt():
        remain = "Đã hết hạn"
    else:
        remain = remaining_text(row)

    username = f"@{row['username']}" if row["username"] else "-"
    status = "🟢 Hoạt động" if row["is_active"] else "🔴 Đã khóa"

    return (
        "👤 <b>THÔNG TIN KHÁCH HÀNG</b>\n\n"
        f"🆔 Telegram ID: <code>{row['telegram_user_id']}</code>\n"
        f"👤 Tên: <b>{html.escape(row['full_name'] or '-')}</b>\n"
        f"🔗 Username: <b>{html.escape(username)}</b>\n"
        f"💎 Gói: <b>{html.escape(row['plan'])}</b>\n"
        f"📊 UID: <b>{row['uid_count']}/{row['uid_limit']}</b>\n"
        f"📅 Hết hạn: <b>{html.escape(str(row['expires_at']))}</b>\n"
        f"⏳ Còn lại: <b>{html.escape(remain)}</b>\n"
        f"🔐 Trạng thái: <b>{status}</b>\n"
        f"🗓 Ngày tạo: <b>{html.escape(str(row['created_at']))}</b>"
    )


def admin_customer_list(page=0, mode="all"):
    per_page = 8
    page = max(0, int(page or 0))
    offset = page * per_page

    where = ""
    args = []
    title = "👥 <b>KHÁCH HÀNG</b>"

    if mode == "locked":
        where = "WHERE u.is_active=0"
        title = "🔒 <b>KHÁCH ĐANG KHÓA</b>"
    elif mode == "expiring":
        deadline = add_days_text(now_dt(), 7)
        where = "WHERE u.plan IN ('TRIAL','VIP') AND u.is_active=1 AND u.expires_at>=? AND u.expires_at<=?"
        args = [now_text(), deadline]
        title = "⏳ <b>KHÁCH SẮP HẾT HẠN ≤ 7 NGÀY</b>"

    con = db()
    cur = con.cursor()
    cur.execute(f"SELECT COUNT(*) AS c FROM users u {where}", args)
    total = cur.fetchone()["c"]

    cur.execute(f"""
        SELECT u.*,
               (SELECT COUNT(*) FROM monitored_accounts a
                WHERE a.telegram_user_id=u.telegram_user_id) AS uid_count
        FROM users u
        {where}
        ORDER BY u.created_at DESC
        LIMIT ? OFFSET ?
    """, args + [per_page, offset])
    rows = cur.fetchall()
    con.close()

    buttons = []
    for r in rows:
        name = (r["full_name"] or r["username"] or str(r["telegram_user_id"]))[:24]
        icon = "🔴" if not r["is_active"] else ("💎" if r["plan"] == "VIP" else "🎁")
        buttons.append([
            InlineKeyboardButton(
                f"{icon} {name} • {r['uid_count']}/{r['uid_limit']}",
                callback_data=f"adm:user:{r['telegram_user_id']}"
            )
        ])

    nav = []
    prefix = "list" if mode == "all" else mode
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Trước", callback_data=f"adm:{prefix}:{page-1}"))
    if offset + per_page < total:
        nav.append(InlineKeyboardButton("Sau ➡️", callback_data=f"adm:{prefix}:{page+1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🏠 Menu ADMIN", callback_data="adm:home")])

    text = (
        f"{title}\n\n"
        f"📊 Tổng: <b>{total}</b>\n"
        f"📄 Trang: <b>{page + 1}</b>\n\n"
        "👇 Bấm vào khách để quản lý."
    )
    return text, InlineKeyboardMarkup(buttons)


async def admin_send_stats(target_message):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users")
    total = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE plan='TRIAL'")
    trial = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE plan='VIP'")
    vip = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE is_active=0")
    locked = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM monitored_accounts")
    uids = cur.fetchone()["c"]
    cur.execute("""
        SELECT COUNT(*) AS c FROM users
        WHERE plan IN ('TRIAL','VIP') AND is_active=1
          AND expires_at>=? AND expires_at<=?
    """, (now_text(), add_days_text(now_dt(), 7)))
    expiring = cur.fetchone()["c"]
    con.close()

    text = (
        "📊 <b>THỐNG KÊ HỆ THỐNG</b>\n\n"
        f"👥 Tổng tài khoản: <b>{total}</b>\n"
        f"🎁 TRIAL: <b>{trial}</b>\n"
        f"💎 VIP: <b>{vip}</b>\n"
        f"⏳ Sắp hết hạn ≤ 7 ngày: <b>{expiring}</b>\n"
        f"🔒 Đang khóa: <b>{locked}</b>\n"
        f"🆔 Tổng UID theo dõi: <b>{uids}</b>"
    )
    await target_message.reply_text(text, parse_mode="HTML", reply_markup=admin_menu_markup())


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.answer("Bạn không có quyền quản trị.", show_alert=True)
        return

    await query.answer()
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "adm":
        return

    action = parts[1]

    if action == "home":
        await query.edit_message_text(
            "🛠 <b>ADMIN - LAPTINH FB MONITOR PRO</b>\n\n"
            "Quản lý khách hàng bằng nút bấm:",
            parse_mode="HTML",
            reply_markup=admin_menu_markup()
        )
        return

    if action in ("list", "locked", "expiring"):
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        mode = "all" if action == "list" else action
        text, markup = admin_customer_list(page, mode)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    if action == "stats":
        con = db()
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM users")
        total = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM users WHERE plan='TRIAL'")
        trial = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM users WHERE plan='VIP'")
        vip = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM users WHERE is_active=0")
        locked = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM monitored_accounts")
        uids = cur.fetchone()["c"]
        cur.execute("""
            SELECT COUNT(*) AS c FROM users
            WHERE plan IN ('TRIAL','VIP') AND is_active=1
              AND expires_at>=? AND expires_at<=?
        """, (now_text(), add_days_text(now_dt(), 7)))
        expiring = cur.fetchone()["c"]
        con.close()

        text = (
            "📊 <b>THỐNG KÊ HỆ THỐNG</b>\n\n"
            f"👥 Tổng tài khoản: <b>{total}</b>\n"
            f"🎁 TRIAL: <b>{trial}</b>\n"
            f"💎 VIP: <b>{vip}</b>\n"
            f"⏳ Sắp hết hạn ≤ 7 ngày: <b>{expiring}</b>\n"
            f"🔒 Đang khóa: <b>{locked}</b>\n"
            f"🆔 Tổng UID theo dõi: <b>{uids}</b>"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=admin_menu_markup())
        return

    if action == "search":
        context.user_data.clear()
        context.user_data["mode"] = "admin_search_customer"
        await query.edit_message_text(
            "🔎 <b>TÌM KHÁCH HÀNG</b>\n\n"
            "Gửi Telegram ID, tên hoặc @username của khách.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Menu ADMIN", callback_data="adm:home")]
            ])
        )
        return

    if len(parts) < 3 or not parts[2].isdigit():
        return

    target = int(parts[2])
    row = admin_customer_row(target)
    if not row:
        await query.edit_message_text(
            "⚠️ Không tìm thấy khách hàng.",
            reply_markup=admin_menu_markup()
        )
        return

    if action == "user":
        await query.edit_message_text(
            customer_card_text(row),
            parse_mode="HTML",
            reply_markup=customer_actions_markup(row)
        )
        return

    if action == "vip":
        old_exp = parse_dt(row["expires_at"])
        base = old_exp if old_exp and old_exp > now_dt() else now_dt()
        new_exp = add_days_text(base, VIP_DAYS)

        con = db()
        cur = con.cursor()
        cur.execute("""
            UPDATE users
            SET expires_at=?, is_active=1, plan='VIP', uid_limit=?
            WHERE telegram_user_id=?
        """, (new_exp, VIP_UID_LIMIT, target))
        con.commit()
        con.close()

        admin_log(target, query.from_user.id, "VIP", f"+{VIP_DAYS} ngày; limit={VIP_UID_LIMIT}; hết hạn={new_exp}")
        row = admin_customer_row(target)
        await query.edit_message_text(
            "✅ <b>ĐÃ KÍCH HOẠT/GIA HẠN VIP</b>\n\n" + customer_card_text(row),
            parse_mode="HTML",
            reply_markup=customer_actions_markup(row)
        )
        return

    if action == "extend":
        context.user_data.clear()
        context.user_data["mode"] = "admin_extend_days"
        context.user_data["admin_target"] = target
        await query.edit_message_text(
            f"📅 <b>GIA HẠN TÙY CHỌN</b>\n\n"
            f"Khách: <code>{target}</code>\n"
            "Gửi số ngày muốn cộng, ví dụ: <code>15</code>, <code>30</code>, <code>90</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Quay lại", callback_data=f"adm:user:{target}")]
            ])
        )
        return

    if action == "limit":
        context.user_data.clear()
        context.user_data["mode"] = "admin_set_limit"
        context.user_data["admin_target"] = target
        await query.edit_message_text(
            f"📦 <b>ĐỔI GIỚI HẠN UID</b>\n\n"
            f"Khách: <code>{target}</code>\n"
            f"Giới hạn hiện tại: <b>{row['uid_limit']} UID</b>\n\n"
            "Gửi giới hạn mới, ví dụ: <code>100</code> hoặc <code>1000</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Quay lại", callback_data=f"adm:user:{target}")]
            ])
        )
        return

    if action in ("lock", "unlock"):
        new_active = 0 if action == "lock" else 1
        con = db()
        cur = con.cursor()
        cur.execute("UPDATE users SET is_active=? WHERE telegram_user_id=?", (new_active, target))
        con.commit()
        con.close()

        admin_log(target, query.from_user.id, action.upper(), "Khóa tài khoản" if not new_active else "Mở khóa tài khoản")
        row = admin_customer_row(target)
        await query.edit_message_text(
            ("🔒 <b>ĐÃ KHÓA TÀI KHOẢN</b>\n\n" if not new_active else "🔓 <b>ĐÃ MỞ KHÓA TÀI KHOẢN</b>\n\n")
            + customer_card_text(row),
            parse_mode="HTML",
            reply_markup=customer_actions_markup(row)
        )
        return

    if action == "uids":
        con = db()
        cur = con.cursor()
        cur.execute("""
            SELECT fb_id, name, note, last_status
            FROM monitored_accounts
            WHERE telegram_user_id=?
            ORDER BY created_at DESC
            LIMIT 30
        """, (target,))
        rows = cur.fetchall()
        con.close()

        if not rows:
            text = "📋 <b>DANH SÁCH UID</b>\n\nKhách này chưa có UID nào."
        else:
            lines = [f"📋 <b>DANH SÁCH UID</b> • {html.escape(row['full_name'] or str(target))}\n"]
            for i, a in enumerate(rows, 1):
                label = html.escape(a["name"] or "-")
                status = html.escape(a["last_status"] or "UNKNOWN")
                link = html.escape(fb_profile_url(a["fb_id"]), quote=True)
                lines.append(f'{i}. <a href="{link}">{a["fb_id"]}</a> • {label} • {status}')
            text = "\n".join(lines)

        await query.edit_message_text(
            text[:4000],
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Quay lại khách", callback_data=f"adm:user:{target}")]
            ])
        )
        return

    if action == "audit":
        con = db()
        cur = con.cursor()
        cur.execute("""
            SELECT action, details, created_at
            FROM admin_audit
            WHERE target_user_id=?
            ORDER BY id DESC
            LIMIT 15
        """, (target,))
        logs = cur.fetchall()
        con.close()

        lines = ["🧾 <b>LỊCH SỬ QUẢN TRỊ</b>\n"]
        if not logs:
            lines.append("Chưa có thao tác quản trị nào được ghi nhận.")
        else:
            for x in logs:
                lines.append(
                    f"• <b>{html.escape(x['action'])}</b> — {html.escape(x['created_at'])}\n"
                    f"  {html.escape(x['details'] or '-')}"
                )

        await query.edit_message_text(
            "\n".join(lines)[:4000],
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Quay lại khách", callback_data=f"adm:user:{target}")]
            ])
        )
        return



def is_admin(update: Update):
    return bool(ADMIN_ID and update.effective_user.id == ADMIN_ID)


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Bạn không có quyền quản trị.")
        return

    await update.message.reply_text(
        "🛠 <b>ADMIN - LAPTINH FB MONITOR PRO</b>\n\n"
        "Quản lý khách hàng bằng nút bấm.\n"
        "Bạn không cần nhớ các lệnh /vip, /limit, /lock nữa.",
        parse_mode="HTML",
        reply_markup=admin_menu_markup()
    )


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    text, markup = admin_customer_list(0, "all")
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


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

    admin_log(target, update.effective_user.id, "EXTEND", f"+{days} ngày; hết hạn={new_exp}")
    await update.message.reply_text(f"✅ Đã gia hạn {days} ngày.\nHết hạn mới: {new_exp}")



async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kích hoạt/gia hạn gói VIP theo cấu hình hiện tại."""
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

    admin_log(target, update.effective_user.id, "VIP", f"+{VIP_DAYS} ngày; limit={VIP_UID_LIMIT}; hết hạn={new_exp}")
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
    if changed:
        admin_log(target, update.effective_user.id, "LIMIT", f"uid_limit={limit_n}")
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
    if changed:
        admin_log(target, update.effective_user.id, "LOCK", "Khóa tài khoản")
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
    if changed:
        admin_log(target, update.effective_user.id, "UNLOCK", "Mở khóa tài khoản")
    await update.message.reply_text("🔓 Đã mở khóa." if changed else "⚠️ Không tìm thấy khách.")


def main():
    if not TOKEN:
        raise RuntimeError("Chưa thiết lập BOT_TOKEN trong biến môi trường.")

    init_db()
    Thread(target=run_web, daemon=True).start()

    async def post_init(application):
        await setup_commands(application)
        print("[COMMAND_MENU] Telegram command menu updated", flush=True)

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("account", account_cmd))
    app.add_handler(CommandHandler("renew", renew_cmd))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("extend", extend_cmd))
    app.add_handler(CommandHandler("vip", vip_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))

    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler),
        group=0
    )

    app.job_queue.run_repeating(
        auto_monitor,
        interval=CHECK_SECONDS,
        first=10
    )

    app.job_queue.run_repeating(
        expiry_reminder_job,
        interval=3600,
        first=20
    )


    print(f"Laptinh FB Monitor PRO V14 đang hoạt động | DB={DB_FILE}", flush=True)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
