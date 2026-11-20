import os
import re
import sys
import uuid
import json
import base64
import hashlib
import sqlite3
import subprocess
import time
import threading
import socket
import struct
import shutil
import secrets
import urllib.parse
from datetime import datetime, timedelta
from urllib.parse import urlsplit, quote
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, session

try:
    import telebot
except ImportError:
    telebot = None

app = Flask(__name__)

# پشت reverse proxy (Railway / Nginx / Cloudflare) تا request.scheme و host درست شوند
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["TEMPLATES_AUTO_RELOAD"] = True
def _resolve_secret_key():
    """SECRET_KEY: env → وگرنه یک کلید تصادفی که کنار داده ذخیره می‌شود.

    ذخیره‌کردنش یعنی سشن‌ها بعد از ری‌استارت نمی‌پرند و لازم نیست دستی متغیر بگذاری.
    """
    env = (os.environ.get("SECRET_KEY") or "").strip()
    if env:
        return env

    path = os.path.join(DATA_DIR, ".secret_key") if DATA_DIR not in (".", "" ) else ".secret_key"
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                saved = f.read().strip()
            if len(saved) >= 32:
                return saved
        key = os.urandom(32).hex()
        with open(path, "w", encoding="utf-8") as f:
            f.write(key)
        os.chmod(path, 0o600)
        return key
    except Exception:
        return os.urandom(32).hex()

ADMIN_USERNAME = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "admin")

try:
    import qrcode as _qrcode
except Exception:  # pragma: no cover - اگر کتابخانه نصب نباشد
    _qrcode = None


def _qr_svg(data, dark="#111a33", light="#ffffff", box=8, border=3):
    """SVG یک QR (بدون وابستگی به CDN و بدون Pillow) — برای ایمپورت آسان در کلاینت."""
    if _qrcode is None:
        return ""

    qr = _qrcode.QRCode(error_correction=_qrcode.constants.ERROR_CORRECT_M, border=border)
    qr.add_data(data)
    qr.make(fit=True)

    matrix = qr.get_matrix()
    n = len(matrix)

    parts = []
    for y, row in enumerate(matrix):
        x = 0
        while x < n:
            if not row[x]:
                x += 1
                continue
            run = 1
            while x + run < n and row[x + run]:
                run += 1
            parts.append(f"M{x},{y}h{run}v1h-{run}z")
            x += run

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" '
        f'shape-rendering="crispEdges" role="img" aria-label="QR">'
        f'<rect width="{n}" height="{n}" fill="{light}"/>'
        f'<path fill="{dark}" d="{"".join(parts)}"/></svg>'
    )


BRAND = "VEGA"  # نام برند به شکل ASCII (برای نام کانفیگ‌های سازگار)

# اسکین فعال: soft = تم روشن «Daylight» · pop = تم سفید/رنگی POP
SKIN = (os.environ.get("VEGA_SKIN") or "soft").strip().lower()
SKIN_CSS = "glass-pop.css" if SKIN in ("pop", "pop-art", "neubrutal") else "glass-soft.css"
BRAND_BOLD = "𝗩𝗘𝗚𝗔"  # فرم بولد یونیکد نام برند در remark کانفیگ‌ها

# هشت کانفیگ: VLESS، Trojan و VMess — همه پشت nginx روی همان دامنهٔ پنل.
# (Shadowsocks از پنل حذف شده است؛ جزئیات در README)
XRAY_PORT = 10000
TROJAN_PORT = 10001
VMESS_PORT = 10002
# VLESS روی XHTTP ترنسپورت (inbound چهارم)
XHTTP_PORT = 10004
XRAY_API_PORT = 10085
FLASK_PORT = 5000

# پورت‌هایی که داخل کانتینر توسط Xray/Flask گرفته می‌شوند؛ nginx هرگز نباید روی
# یکی از این‌ها بنشیند. اگر پلتفرم (مثلاً Railway با target port اشتباه) PORT را روی
# یکی از این‌ها بگذارد، به‌جای کرش کردن، پورت امن استفاده می‌شود.
_INTERNAL_PORTS = {FLASK_PORT, XRAY_PORT, TROJAN_PORT, VMESS_PORT, XHTTP_PORT, XRAY_API_PORT}


def _env_port(name, default):
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw.isdigit() and 0 < int(raw) < 65536 else default


# پورت HTTP پنل: پیش‌فرض 8080 و قابل تغییر با VEGA_HTTP_PORT.
# PORT پلتفرم هم به‌عنوان یک پورت اضافه در نظر گرفته می‌شود (اگر آزاد باشد)،
# چون بعضی پلتفرم‌ها همین را به دامنه وصل می‌کنند.
PUBLIC_PORT = _env_port("VEGA_HTTP_PORT", 8080)
PLATFORM_PORT = _env_port("PORT", PUBLIC_PORT)

if PUBLIC_PORT in _INTERNAL_PORTS:
    PUBLIC_PORT = 8080

if PLATFORM_PORT in _INTERNAL_PORTS and PLATFORM_PORT != PUBLIC_PORT:
    print(
        f"[vega] هشدار: PORT={PLATFORM_PORT} که پلتفرم تزریق کرده، پورت یکی از سرویس‌های "
        f"داخلی است (Xray/Flask) → روی همین پورت شنود نمی‌کنیم.\n"
        f"        اگر دامنه‌ات به این پورت وصل است، target port دامنه را {PUBLIC_PORT} بگذار.",
        flush=True,
    )
    PLATFORM_PORT = PUBLIC_PORT

# همهٔ فایل‌های وضعیت (دیتابیس، کانفیگ Xray، nginx) در یک پوشه نگه‌داری می‌شوند.
# روی PaaS یک volume را روی همین پوشه mount کنید و VEGA_DATA_DIR را ست کنید:
#   VEGA_DATA_DIR=/app/data
def _pick_data_dir():
    """پوشهٔ داده: env → وگرنه اگر /app/data وجود دارد (volume)، خودکار همان.

    یعنی روی Railway فقط mount کردن volume کافی است و لازم نیست متغیری تعریف کنی.
    """
    env = (os.environ.get("VEGA_DATA_DIR") or "").strip()
    if env:
        return env
    for candidate in ("/app/data", "./data"):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return "."


DATA_DIR = _pick_data_dir()

try:
    if DATA_DIR not in (".", ""):
        os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass


def _data_file(name):
    return name if DATA_DIR in (".", "") else os.path.join(DATA_DIR, name)


app.secret_key = _resolve_secret_key()

DB_PATH = _data_file("users.db")
XRAY_CONFIG_PATH = _data_file("xray_config.json")
NGINX_CONFIG_PATH = _data_file("nginx.conf")

# --------------------------------------------------------------- مبهم‌سازی مسیرها
# هر نصب مسیرهای خودش را می‌سازد؛ از روی URL نه پنل قابل تشخیص است نه نوع پروتکل.
# فایل: obfuscation.json کنار بقیهٔ داده‌ها (با volume پایدار می‌ماند).
OBF_FILE = _data_file("obfuscation.json")


def _obf_token(nbytes=4):
    """توکن هگز تصادفی (۸ کاراکتر برای ۴ بایت)."""
    return secrets.token_hex(nbytes)


# قالب‌های مسیر: «شکل» مسیر هم تصادفی است، نه فقط توکن داخلش.
# هر نصب یکی از این‌ها را برای هر ترنسپورت برمی‌دارد تا اثر انگشتِ قالب ثابت نماند.
_PATH_SHAPES = {
    "vless": (
        "/assets/js/chunk-{t}.js",
        "/static/js/{t}.bundle.js",
        "/cdn/{w}/js/app.{t}.js",
        "/js/vendor/{t}.min.js",
        "/dist/assets/index-{t}.js",
        "/build/{w}/main.{t}.js",
    ),
    "trojan": (
        "/media/hls/{t}.m3u8",
        "/live/stream/{w}/playlist-{t}.m3u8",
        "/vod/hls/{t}/index.m3u8",
        "/hls/{t}.m3u8",
        "/media/{w}/chunklist-{t}.m3u8",
    ),
    "vmess": (
        "/api/v2/feed/{t}.json",
        "/api/{v}/content/{t}.json",
        "/graphql/data/{t}.json",
        "/wp-json/api/v1/items/{t}.json",
        "/rest/{w}/v{v}/records/{t}.json",
    ),
    "xhttp": (
        "/api/v1/upload/{t}",
        "/api/v2/telemetry/{t}",
        "/collect/{t}",
        "/api/events/{t}",
        "/beacon/{w}/{t}",
    ),
}

_WORD_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"


def _obf_word(n=6):
    """بخش متنی تصادفیِ شبیه نام پوشه/برند (حروف کوچک و رقم)."""
    return "".join(secrets.choice(_WORD_CHARS) for _ in range(n))


def _default_paths():
    """مسیرهای پیش‌فرضِ شبیه به ترافیک وب معمولی (قالب + توکن، هر دو تصادفی)."""
    out = {}
    for key, shapes in _PATH_SHAPES.items():
        shape = secrets.choice(shapes)
        out[key] = shape.format(
            t=_obf_token(6),          # ۱۲ کاراکتر هگز
            w=_obf_word(6),
            v=secrets.randbelow(3) + 1,
        )
    out["sub"] = _obf_token(8)        # پیشوند ساب: ۱۶ کاراکتر
    out["panel"] = "/" + _obf_token(6)  # مسیر مخفی پنل: /<12hex>
    return out


def _clean_path(value):
    """مسیر معتبر (شروع با /، فقط کاراکترهای امن وب، بدون / انتهایی)."""
    value = (value or "").strip().split("?")[0].split("#")[0]
    if not value:
        return ""
    if not value.startswith("/"):
        value = "/" + value
    value = value.rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9/_.-]{2,90}", value):
        return ""
    return value


def _clean_slug(value):
    return re.sub(r"[^A-Za-z0-9_-]", "", (value or "").strip())[:40]


def load_obfuscation():
    """خواندن (یا ساخت) مسیرهای مبهم این نصب.

    اولویت: env → فایل obfuscation.json → توکن تازه.
    """
    stored = {}
    try:
        if os.path.exists(OBF_FILE):
            with open(OBF_FILE, "r", encoding="utf-8") as f:
                stored = json.load(f) or {}
    except Exception as e:
        print(f"[vega] obfuscation.json خوانده نشد ({e}) → مسیر تازه ساخته می‌شود", flush=True)

    base = _default_paths()
    data, changed = {}, False

    for key in ("vless", "trojan", "vmess", "xhttp"):
        env_val = _clean_path(os.environ.get(f"VEGA_PATH_{key.upper()}", ""))
        file_val = _clean_path(stored.get(key, ""))
        if env_val:
            data[key] = env_val
        elif file_val:
            data[key] = file_val
        else:
            data[key] = base[key]
            changed = True

    # مسیر مخفی پنل: env خالی/0 ⇒ غیرفعال (پنل مثل قبل روی / می‌ماند)
    env_panel = (os.environ.get("VEGA_PANEL_PATH", "") or "").strip()
    if env_panel.lower() in ("0", "off", "no"):
        data["panel"] = ""
    elif env_panel:
        data["panel"] = _clean_path(env_panel)
    elif _clean_path(stored.get("panel", "")):
        data["panel"] = _clean_path(stored.get("panel", ""))
    else:
        data["panel"] = base["panel"]
        changed = True

    env_sub = _clean_slug(os.environ.get("VEGA_SUB_PREFIX", ""))
    file_sub = _clean_slug(stored.get("sub", ""))
    if env_sub and len(env_sub) >= 6:
        data["sub"] = env_sub
    elif file_sub and len(file_sub) >= 6:
        data["sub"] = file_sub
    else:
        data["sub"] = base["sub"]
        changed = True

    # توکن‌های تکراری/خالی هم بازسازی می‌شوند و مسیرها هرگز یکی نیستند
    keys = ("vless", "trojan", "vmess", "xhttp")
    if len({data[k] for k in keys}) != len(keys):
        data.update({k: base[k] for k in keys})
        changed = True

    if os.environ.get("VEGA_REGEN_PATHS", "") == "1":
        data = base
        changed = True

    if changed or not os.path.exists(OBF_FILE):
        try:
            with open(OBF_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[vega] obfuscation.json ذخیره نشد: {e}", flush=True)

    return data


OBF = load_obfuscation()

VLESS_PATH = OBF["vless"]          # مسیر وب‌سوکت VLESS  (پیش‌فرض: /assets/js/chunk-xxxxxxxx.js)
TROJAN_PATH = OBF["trojan"]        # مسیر وب‌سوکت Trojan (پیش‌فرض: /media/hls/xxxxxxxx.m3u8)
VMESS_PATH = OBF["vmess"]          # مسیر وب‌سوکت VMess  (پیش‌فرض: /api/v2/feed/xxxxxxxx.json)
XHTTP_PATH = OBF["xhttp"]          # مسیر XHTTP       (پیش‌فرض: /api/v1/upload/xxxxxxxx)
XHTTP_MODE = (os.environ.get("VEGA_XHTTP_MODE") or "auto").strip().lower()  # auto | packet-up | stream-up
# الگوریتم کنترل ازدحام سوکت‌های خروجی؛ bbr روی مسیرهای پرتأخیر/پرتلفات چند برابر بهتر از cubic است
# (خالی گذاشتن = دست‌نزدن به تنظیم کرنل)
TCP_CONGESTION = (os.environ.get("VEGA_TCP_CONGESTION") or "bbr").strip().lower()
SUB_PREFIX = "/" + OBF["sub"]      # پیشوند لینک ساب (پیش‌فرض: /xxxxxxxxxxxx)

# مسیر مخفی پنل (خالی = غیرفعال). با VEGA_PANEL_PATH=0 هم می‌شود خاموشش کرد.
PANEL_PREFIX = (OBF.get("panel") or "").rstrip("/")

# نام برند داخل remark کانفیگ‌ها؛ خالی = بدون نام (سخت‌تر برای اثرانگشت‌گیری)
REMARK_TAG = os.environ.get("VEGA_REMARK_TAG", "").strip()

print(f"[vega] مسیرهای مبهم: vless={VLESS_PATH} trojan={TROJAN_PATH} "
      f"vmess={VMESS_PATH} xhttp={XHTTP_PATH} sub={SUB_PREFIX}/<uuid> "
      f"panel={PANEL_PREFIX or '(روی ریشه)'}", flush=True)

ONLINE_USERS = {}
ONLINE_THRESHOLD = 90

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            uuid TEXT UNIQUE NOT NULL,
            quota_gb REAL DEFAULT 0,
            used_bytes INTEGER DEFAULT 0,
            expire_days INTEGER DEFAULT 30,
            created_at TEXT,
            enabled INTEGER DEFAULT 1
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS panel_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            actor TEXT,
            message TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

def get_setting(key, default=""):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default

def set_setting(key, value):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print("Error saving setting:", e)
        return False

def get_admin_credentials():
    env_user = os.environ.get("ADMIN_USER")
    env_pass = os.environ.get("ADMIN_PASS")

    if env_user is not None and env_pass is not None:
        return env_user, env_pass

    settings_file = _data_file("panel_settings.json")

    if os.path.exists(settings_file):
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            username = data.get("username", "admin")
            password = data.get("password", "admin")

            return username, password

        except Exception:
            pass

    return "admin", "admin"

def save_admin_credentials(username, password):
    settings_file = _data_file("panel_settings.json")

    data = {
        "username": username,
        "password": password
    }

    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _panel_log(message, level="info", actor=None):
    """ثبت یک رویداد پنل در جدول panel_log (بدون شکستن روت در صورت خطا)."""
    try:
        who = actor or session.get("admin_user") or "admin"
    except Exception:
        who = actor or "system"

    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO panel_log (ts, level, actor, message) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), level, who, str(message)[:500]),
        )
        # فقط ۱۰۰۰ ردیف آخر نگه داشته می‌شود
        c.execute("DELETE FROM panel_log WHERE id NOT IN "
                  "(SELECT id FROM panel_log ORDER BY id DESC LIMIT 1000)")
        conn.commit()
        conn.close()
    except Exception:
        pass


# مسیرهای رایج لاگ Xray / nginx؛ اولین فایل موجود خوانده می‌شود
RUNTIME_LOG_PATHS = {
    "access": [
        "/var/log/xray/access.log",
        "/usr/local/etc/xray/access.log",
        "/var/log/nginx/access.log",
        "xray-access.log",
    ],
    "error": [
        "/var/log/xray/error.log",
        "/usr/local/etc/xray/error.log",
        "/var/log/nginx/error.log",
        "xray-error.log",
    ],
}


def _log_level_of(line):
    """سطح خط لاگ را تعیین می‌کند؛ تگ صریح Xray یعنی [Error]/[Warning] بر کلمات کلیدی اولویت دارد."""
    low = line.lower()

    if any(k in low for k in ("[error]", "[fatal]", "[panic]")):
        return "error"
    if any(k in low for k in ("[warning]", "[warn]")):
        return "warn"

    if any(k in low for k in ("error", "fatal", "panic")):
        return "error"
    if any(k in low for k in ("rejected", "refused", "failed", "failure")):
        return "error"
    if any(k in low for k in ("warn", "warning", "timeout", "retry")):
        return "warn"
    return "info"


def _tail_runtime_log(kind, limit=200):
    """آخرین خطوط لاگ اجرا (access/error) را برمی‌گرداند: (path, entries, searched)."""
    searched = RUNTIME_LOG_PATHS.get(kind, [])
    for path in searched:
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                block = min(size, 96 * 1024)
                f.seek(size - block)
                raw = f.read().decode("utf-8", "replace")
        except Exception:
            continue

        lines = [ln for ln in raw.splitlines() if ln.strip()][-limit:]
        entries = [{
            "ts": "",
            "level": _log_level_of(ln),
            "actor": path.rsplit("/", 1)[-1],
            "message": ln,
        } for ln in lines]
        return path, entries, searched

    return None, [], searched


def get_all_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY id DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def is_user_active(user):
    """بررسی فعال بودن کاربر بر اساس ترافیک، زمان و وضعیت کلی"""
    if user["enabled"] == 0:
        return False, "disabled"

    try:
        created_dt = datetime.fromisoformat(user["created_at"])
        elapsed_days = (datetime.now() - created_dt).days
        if elapsed_days >= user["expire_days"]:
            return False, "expired"
    except Exception:
        pass

    used_gb = user["used_bytes"] / (1024 ** 3)
    if user["quota_gb"] > 0 and used_gb >= user["quota_gb"]:
        return False, "limit_exceeded"

    return True, "active"

def enrich_user(u):
    try:
        created_dt = datetime.fromisoformat(u["created_at"])
        elapsed_days = (datetime.now() - created_dt).days
        days_left = max(0, u["expire_days"] - elapsed_days)
    except Exception:
        days_left = u["expire_days"]

    used_gb = round(u["used_bytes"] / (1024 ** 3), 2)
    quota_gb = round(float(u["quota_gb"]), 2)

    if quota_gb > 0:
        percent = min(100, round((used_gb / quota_gb) * 100, 1))
    else:
        percent = 0

    active_state, reason = is_user_active(u)

    u["used_gb"] = used_gb
    u["days_left"] = days_left
    u["percent"] = percent
    u["is_expired"] = days_left <= 0 or reason == "expired"
    u["is_limit_exceeded"] = reason == "limit_exceeded"
    u["is_active"] = active_state
    u["created_date"] = u["created_at"][:10] if u.get("created_at") else ""
    u["is_online"] = is_user_online(u["name"])

    return u

def is_user_online(name):
    last_seen = ONLINE_USERS.get(name, 0)
    return (time.time() - last_seen) < ONLINE_THRESHOLD

# وضعیت سلامت هسته (برای /api/diag و پیام‌های استارتاپ)
XHTTP_AVAILABLE = True
XRAY_CONFIG_OK = True
XRAY_CONFIG_ERROR = ""
Nginx = None  # (بی‌استفاده؛ فقط برای جلوگیری از نام‌های تکراری)


def xray_bin_path():
    """مسیر باینری Xray. اولویت: env → مسیر استاندارد ایمیج → PATH."""
    env = (os.environ.get("VEGA_XRAY_BIN") or "").strip()
    if env:
        return env
    for candidate in ("/usr/local/bin/xray/xray", "/usr/bin/xray", "xray"):
        if candidate == "xray":
            found = shutil.which("xray")
            return found or "xray"
        if os.path.exists(candidate):
            return candidate
    return "xray"


def xray_version():
    """نسخهٔ Xray به‌صورت رشته؛ '' اگر اجرا نشود."""
    try:
        r = subprocess.run([xray_bin_path(), "-version"], capture_output=True, text=True, timeout=10)
        first = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        return first[0] if first else ""
    except Exception:
        return ""


def _xray_config_test():
    """(ok, خروجی) از `xray run -test`."""
    try:
        r = subprocess.run([xray_bin_path(), "run", "-test", "-c", XRAY_CONFIG_PATH],
                           capture_output=True, text=True, timeout=30)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, out
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def validate_xray_config():
    """کانفیگ را قبل از اجرا چک می‌کند و در صورت پشتیبانی‌نشدن XHTTP، آن را حذف می‌کند.

    اگر باینری Xray قدیمی باشد (بدون ترنسپورت xhttp) کل کانفیگ رد می‌شود و
    هسته بالا نمی‌آید؛ این تابع جلوی آن را می‌گیرد تا ۷ کانفیگ دیگر سالم بمانند.
    """
    global XHTTP_AVAILABLE, XRAY_CONFIG_OK, XRAY_CONFIG_ERROR

    ok, out = _xray_config_test()
    if ok:
        XHTTP_AVAILABLE, XRAY_CONFIG_OK, XRAY_CONFIG_ERROR = True, True, ""
        return True

    XRAY_CONFIG_ERROR = out[-900:]
    print(f"[vega] ⚠ Xray config نامعتبر است:\n{out[-600:]}", flush=True)

    # تلاش نجات: حذف inbound XHTTP و تست دوباره
    if XHTTP_AVAILABLE:
        try:
            cfg = json.load(open(XRAY_CONFIG_PATH, encoding="utf-8"))
            cfg["inbounds"] = [i for i in cfg["inbounds"] if i.get("tag") != "vless-xhttp-in"]
            with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            ok2, out2 = _xray_config_test()
            if ok2:
                XHTTP_AVAILABLE, XRAY_CONFIG_OK, XRAY_CONFIG_ERROR = False, True, ""
                print("[vega] ⚠ XHTTP غیرفعال شد (نسخهٔ Xray از ترنسپورت xhttp پشتیبانی نمی‌کند) "
                      "→ ۷ کانفیگ دیگر فعال‌اند. ایمیج را با Xray v25+ بساز.", flush=True)
                return True
            XRAY_CONFIG_ERROR = out2[-900:]
        except Exception as e:
            XRAY_CONFIG_ERROR = f"{type(e).__name__}: {e}"

    XRAY_CONFIG_OK = False
    print(f"[vega] ✗ Xray با این کانفیگ بالا نمی‌آید: {XRAY_CONFIG_ERROR[-300:]}", flush=True)
    return False


def build_xray_config():
    users = get_all_users()

    vless_clients = []
    trojan_clients = []
    vmess_clients = []

    for u in users:
        # فقط کاربرانی که واقعاً فعال هستند داخل کانفیگ Xray ریخته می‌شوند
        active, _ = is_user_active(u)
        if not active:
            continue

        vless_clients.append({"id": u["uuid"], "email": u["name"], "level": 0})
        # در تروجان رمز عبور همان UUID کاربر است تا همه پروتکل‌ها یک هویت داشته باشند
        trojan_clients.append({"password": u["uuid"], "email": u["name"], "level": 0})
        vmess_clients.append({"id": u["uuid"], "email": u["name"], "level": 0})

    if not vless_clients:
        fallback_id = str(uuid.uuid4())
        vless_clients.append({"id": fallback_id, "email": "default_user", "level": 0})
        trojan_clients.append({"password": fallback_id, "email": "default_user", "level": 0})
        vmess_clients.append({"id": fallback_id, "email": "default_user", "level": 0})

    config = {
        "log": {
            "loglevel": "warning"
        },
        "stats": {},
        "api": {
            "tag": "api",
            "services": ["StatsService"]
        },
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True
                }
            },
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True
            }
        },
        "inbounds": [
            {
                "tag": "api",
                "port": XRAY_API_PORT,
                "listen": "127.0.0.1",
                "protocol": "dokodemo-door",
                "settings": {
                    "address": "127.0.0.1"
                }
            },
            {
                "tag": "vless-in",
                "port": XRAY_PORT,
                "listen": "127.0.0.1",
                "protocol": "vless",
                "settings": {
                    "clients": vless_clients,
                    "decryption": "none"
                },
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {
                        "path": VLESS_PATH
                    }
                }
            },
            {
                "tag": "trojan-in",
                "port": TROJAN_PORT,
                "listen": "127.0.0.1",
                "protocol": "trojan",
                "settings": {
                    "clients": trojan_clients
                },
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {
                        "path": TROJAN_PATH
                    }
                }
            },
            {
                "tag": "vless-xhttp-in",
                "port": XHTTP_PORT,
                "listen": "127.0.0.1",
                "protocol": "vless",
                "settings": {
                    "clients": vless_clients,
                    "decryption": "none"
                },
                "streamSettings": {
                    "network": "xhttp",
                    "security": "none",
                    "xhttpSettings": {
                        "path": XHTTP_PATH,
                        "mode": XHTTP_MODE
                    }
                }
            },
            {
                "tag": "vmess-in",
                "port": VMESS_PORT,
                "listen": "127.0.0.1",
                "protocol": "vmess",
                "settings": {
                    "clients": vmess_clients
                },
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {
                        "path": VMESS_PATH
                    }
                }
            },

        ],
        "outbounds": [
            {
                "protocol": "freedom",
                "tag": "direct",
                # سوکت خروجی: BBR + Fast Open + keepalive (تأخیر کمتر روی مسیرهای پرتلفات)
                "streamSettings": {
                    "sockopt": {
                        "tcpFastOpen": True,
                        "tcpNoDelay": True,
                        "tcpKeepAliveInterval": 15,
                        "tcpUserTimeout": 10000,
                        **({"tcpCongestion": TCP_CONGESTION} if TCP_CONGESTION else {}),
                    }
                },
            }
        ],
        "routing": {
            "rules": [
                {
                    "type": "field",
                    "inboundTag": ["api"],
                    "outboundTag": "api"
                }
            ]
        }
    }

    with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    validate_xray_config()


def restart_xray():
    build_xray_config()
    try:
        subprocess.run(["pkill", "-9", "-f", "xray"], check=False)
        time.sleep(0.5)
    except Exception:
        pass

    try:
        xray_bin = "/usr/local/bin/xray/xray"
        if not os.path.exists(xray_bin):
            xray_bin = "xray"

        subprocess.Popen(
            [xray_bin, "run", "-c", XRAY_CONFIG_PATH],
            stdout=sys.stdout,
            stderr=sys.stderr
        )
    except Exception as e:
        print("Xray start error:", e)

def xray_query_stats():
    stats = {}
    xray_bin = "/usr/local/bin/xray/xray"
    if not os.path.exists(xray_bin):
        xray_bin = "xray"

    try:
        result = subprocess.run(
            [
                xray_bin,
                "api",
                "statsquery",
                f"--server=127.0.0.1:{XRAY_API_PORT}",
                "-pattern=user>>>",
                "-reset=true"
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode != 0:
            return stats

        data = json.loads(result.stdout)
        stat_list = data.get("stat", [])

        for item in stat_list:
            name = item.get("name", "")
            value = int(item.get("value", 0))

            parts = name.split(">>>")
            if len(parts) >= 4 and parts[0] == "user":
                user_email = parts[1]

                if user_email not in stats:
                    stats[user_email] = 0

                stats[user_email] += value

    except Exception:
        pass

    return stats

def xray_reset_user_stats(user_email):
    xray_bin = "/usr/local/bin/xray/xray"
    if not os.path.exists(xray_bin):
        xray_bin = "xray"

    try:
        for direction in ["uplink", "downlink"]:
            subprocess.run(
                [
                    xray_bin,
                    "api",
                    "statsquery",
                    f"--server=127.0.0.1:{XRAY_API_PORT}",
                    "-reset=true",
                    f"-pattern=user>>>{user_email}>>>traffic>>>{direction}"
                ],
                capture_output=True,
                timeout=3
            )
    except Exception:
        pass

PREVIOUS_STATS = {}

def stats_collector():
    global PREVIOUS_STATS

    while True:
        try:
            time.sleep(15)

            stats = xray_query_stats()

            if not stats:
                continue

            conn = get_db()
            c = conn.cursor()

            need_restart = False

            for user_email, total_bytes in stats.items():
                if total_bytes <= 0:
                    continue

                c.execute(
                    "UPDATE users SET used_bytes = used_bytes + ? WHERE name = ?",
                    (total_bytes, user_email)
                )

                ONLINE_USERS[user_email] = time.time()

                c.execute(
                    "SELECT id, quota_gb, used_bytes, enabled, created_at, expire_days FROM users WHERE name = ?",
                    (user_email,)
                )

                row = c.fetchone()

                if row and row["enabled"] == 1:
                    # دیگر کاربر را در دیتابیس کلاً غیرفعال نمی‌کنیم (تا سابسکریپشن قطع نشود)
                    # فقط وضعیت فعال بودن واقعی او را می‌سنجیم و در صورت لزوم Xray را ریستارت می‌کنیم تا قطع شود
                    active, reason = is_user_active(dict(row))
                    if not active:
                        need_restart = True
                        print(f"[QUOTA/TIME] User '{user_email}' is no longer active ({reason}). Disabling from Xray.")

            conn.commit()
            conn.close()

            if need_restart:
                restart_xray()

        except Exception as e:
            print("Stats collector error:", e)

def start_stats_collector():
    t = threading.Thread(target=stats_collector, daemon=True)
    t.start()

def http_listen_ports():
    """پورت‌هایی که nginx باید روی آن‌ها گوش بدهد.

    همیشه PUBLIC_PORT؛ و اگر PORT پلتفرم پورت دیگری بود و آزاد است، آن هم اضافه
    می‌شود تا دامنهٔ پلتفرم در هر حالت به پنل برسد.
    """
    ports = [PUBLIC_PORT]
    if PLATFORM_PORT != PUBLIC_PORT and not _port_open(PLATFORM_PORT):
        ports.append(PLATFORM_PORT)
    return ports


def _port_open(port, host="127.0.0.1", timeout=0.6):
    """آیا روی این پورت چیزی گوش می‌دهد؟"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


# ---------------------------------------------------------------- طعمه‌ها
# اگر کسی مسیر مبهم را با یک GET ساده (بدون WebSocket/XHTTP) صدا بزند،
# Xray خطا می‌دهد؛ nginx آن خطا را با یک فایل واقعی و کد ۲۰۰ جواب می‌دهد
# تا از یک سرور معمولی قابل تشخیص نباشد.
DECOY_DIR = os.path.join(DATA_DIR, "decoy")

_DECOYS = {
    1: ("application/javascript; charset=utf-8",
        """/*! chunk bundle */
(function(){"use strict";
var e=[],t=function(n){return e.indexOf(n)>-1};
window.__chunk=function(n){return t(n)?null:{id:n,loaded:!0}};
})();
"""),
    2: ("application/vnd.apple.mpegurl",
        """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:6.000,
seg-0.ts
#EXTINF:6.000,
seg-1.ts
#EXT-X-ENDLIST
"""),
    3: ("application/json; charset=utf-8",
        """{"status":"ok","items":[],"page":1,"next":null}
"""),
    4: ("application/json; charset=utf-8",
        """{"success":true,"received":0,"id":null}
"""),
}


def write_decoys():
    """فایل‌های طعمه را می‌سازد (بدون extension ⇒ default_type تعیین‌کننده است).

    ساختار: <DECOY_DIR>/_vx/<n>  چون nginx با root، کل URI را به مسیر اضافه می‌کند.
    """
    decoy_web_dir = os.path.join(DECOY_DIR, "_vx")
    os.makedirs(decoy_web_dir, exist_ok=True)
    for name, (_ctype, body) in _DECOYS.items():
        path = os.path.join(decoy_web_dir, str(name))
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
    return DECOY_DIR


def _decoy_locations():
    """لوکیشن‌های داخلیِ طعمه؛ فقط از راه error_page قابل دسترس‌اند."""
    out = ""
    for name, (ctype, _body) in _DECOYS.items():
        out += f"""
        location = /_vx/{name} {{
            internal;
            root {DECOY_DIR};
            default_type "{ctype}";
        }}
"""
    return out


def _obf_location(path, upstream, label, websocket=True, decoy=None):
    """بلوک nginx برای مسیر مبهم (بدون rewrite، مسیر عیناً حفظ می‌شود).

    websocket=False برای XHTTP: هدرهای Upgrade لازم نیست و بافر باید خاموش باشد
    تا جریان دوطرفهٔ POST/GET قطع نشود.
    """
    if websocket:
        upgrade = """            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
"""
        buffering = """            proxy_buffering off;
"""
    else:
        upgrade = ""
        buffering = """            proxy_buffering off;
            proxy_request_buffering off;
"""

    return f"""
        # {label} — مسیر این نصب: {path}
        location ^~ {path} {{
            proxy_redirect off;
            proxy_pass http://127.0.0.1:{upstream};
            proxy_http_version 1.1;
{upgrade}{buffering}            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_socket_keepalive on;
            proxy_read_timeout 86400s;
            proxy_send_timeout 86400s;
            error_page 400 403 404 405 =200 /_vx/{decoy};
        }}
"""


def start_nginx():
    ports = http_listen_ports()
    port = ports[0]
    listen_lines = "\n        ".join(f"listen {p};" for p in ports)

    ws_locations = (
        _obf_location(VLESS_PATH, XRAY_PORT, "VLESS · WS", decoy=1)
        + _obf_location(TROJAN_PATH, TROJAN_PORT, "TROJAN · WS", decoy=2)
        + _obf_location(VMESS_PATH, VMESS_PORT, "VMESS · WS", decoy=3)
        + _obf_location(XHTTP_PATH, XHTTP_PORT, "VLESS · XHTTP",
                        websocket=False, decoy=4)
    )

    write_decoys()
    decoy_locations = _decoy_locations()

    nginx_conf = f"""
pid /run/nginx.pid;
error_log /dev/stderr warn;
# سرعت: یک worker روی کانتینر چند‌هسته‌ای گلوگاه اصلی بود
worker_processes auto;
worker_rlimit_nofile 65535;
events {{
    worker_connections 8192;
    multi_accept on;
}}
http {{
    access_log /dev/stdout;
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    client_body_timeout 60s;
    client_header_timeout 60s;
    send_timeout 120s;
    server_tokens off;
    proxy_hide_header X-Powered-By;
    proxy_hide_header Server;
    add_header Server "nginx" always;
    map $http_upgrade $connection_upgrade {{
        default upgrade;
        '' close;
    }}
    server {{
        {listen_lines}
        server_name _;
        {ws_locations}{decoy_locations}
        location / {{
            proxy_pass http://127.0.0.1:{FLASK_PORT};
            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }}
    }}
}}
"""

    with open(NGINX_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(nginx_conf)

    nginx_bin = shutil.which("nginx")
    if not nginx_bin:
        print("[vega] nginx پیدا نشد → پنل مستقیم روی PORT اجرا می‌شود "
              "(کانفیگ‌های WS/TLS در این حالت کار نمی‌کنند)", flush=True)
        return False

    try:
        subprocess.run(["pkill", "-9", "-f", "nginx"], check=False)
        time.sleep(0.3)
    except Exception:
        pass

    # اعتبارسنجی کانفیگ قبل از اجرا؛ کانفیگ خراب نباید پنل را زمین بزند
    test = subprocess.run(
        [nginx_bin, "-t", "-c", os.path.abspath(NGINX_CONFIG_PATH)],
        capture_output=True, text=True,
    )
    if test.returncode != 0:
        print("[vega] nginx config نامعتبر است → پنل مستقیم اجرا می‌شود\n"
              + (test.stderr or "").strip(), flush=True)
        return False

    try:
        subprocess.Popen(
            [nginx_bin, "-c", os.path.abspath(NGINX_CONFIG_PATH), "-g", "daemon off;"],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    except Exception as e:
        print(f"[vega] اجرای nginx ناموفق بود: {e}", flush=True)
        return False

    # مطمئن شو پورت واقعاً باز شده؛ وگرنه پلتفرم سرویس را «بالا نیامده» می‌بیند
    for _ in range(20):
        time.sleep(0.5)
        if _port_open(int(port)):
            print(f"[vega] nginx روی پورت(های) {', '.join(map(str, ports))} بالا آمد", flush=True)
            return True

    print(f"[vega] nginx بالا آمد ولی پورت {port} باز نشد — پنل مستقیم اجرا می‌شود", flush=True)
    return False

def _enc(value):
    return urllib.parse.quote(str(value), safe="")


def _vless_link(uuid_str, host, port, path, remark, tls=True, alpn=None, fp=None,
                transport="ws", mode=None):
    """لینک VLESS در قالب استاندارد و «محافظه‌کارانه».

    نکته: پارامترهای insecure / allowInsecure / alpn عمداً حذف شده‌اند.
    روی وب‌سوکت (WS) تعیین alpn=h2 اشتباه است و بعضی پارسرهای سخت‌گیر
    (v2rayNG) کل ساب را به‌خاطر یک پارامتر غیرمنتظره رد می‌کنند.
    """
    params = [f"type={transport}"]

    if tls:
        params += ["security=tls", "encryption=none"]
    else:
        params += ["security=none", "encryption=none"]

    params.append(f"path={_enc(path)}")
    params.append(f"host={_enc(host)}")

    # XHTTP: حالت انتقال هم باید در لینک باشد (auto | packet-up | stream-up)
    if transport == "xhttp" and mode:
        params.append(f"mode={_enc(mode)}")

    if tls:
        params.append(f"sni={_enc(host)}")
        if fp:
            params.append(f"fp={_enc(fp)}")

    return f"vless://{uuid_str}@{host}:{port}?" + "&".join(params) + f"#{_enc(remark)}"


def _trojan_link(uuid_str, host, port, path, remark, alpn=None, fp=None, tls=True):
    """لینک تروجان، همان قالب محافظه‌کارانه (بدون alpn و allowInsecure)."""
    params = ["type=ws"]

    if tls:
        params += ["security=tls"]
    else:
        params += ["security=none"]

    params.append(f"path={_enc(path)}")
    params.append(f"host={_enc(host)}")

    if tls:
        params.append(f"sni={_enc(host)}")
        if fp:
            params.append(f"fp={_enc(fp)}")

    return f"trojan://{uuid_str}@{host}:{port}?" + "&".join(params) + f"#{_enc(remark)}"


def _vmess_link(uuid_str, host, port, path, remark, alpn=None, fp=None, tls=True):
    """VMess فقط با کلیدهای پایه؛ کلیدهای اضافی (alpn) حذف شده‌اند."""
    obj = {
        "v": "2",
        "ps": remark,
        "add": host,
        "port": str(port),
        "id": uuid_str,
        "aid": "0",
        "scy": "auto",
        "net": "ws",
        "type": "none",
        "host": host,
        "path": path,
        "tls": "tls" if tls else "",
        "sni": host if tls else "",
    }

    if tls and fp:
        obj["fp"] = fp

    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return "vmess://" + base64.b64encode(raw.encode("utf-8")).decode()


def make_all_configs(user, host, port=443, tls=True, ascii_names=False):
    """۵ کانفیگ برای هر کاربر: ۳ عدد VLESS (WS/XHTTP/ED)، ۱ عدد Trojan و ۱ عدد VMess.

    هر سه پروتکل سمت سرور inbound جدا دارند (XRAY_PORT / TROJAN_PORT / VMESS_PORT)
    و از طریق nginx روی مسیرهای /ws ، /tr و /vm به بیرون سرو می‌شوند.
    """
    u_uuid = user["uuid"]
    name = user["name"]

    def remark(number):
        # نام کانفیگ: ایموجی + (برند اختیاری) + شماره + نام کاربر
        # REMARK_TAG خالی است مگر این‌که VEGA_REMARK_TAG ست شود؛ بدون نام برند
        # هیچ اثری از پنل داخل لینک‌ها نمی‌ماند.
        mid = f"{REMARK_TAG} | " if REMARK_TAG else ""
        return f"⚡ {mid}{number} | {name}"

    # (شماره، تگ، نوع، پارامترها) — ۵ کانفیگ: کمترین سطح حمله، پوشش همهٔ نیازها
    plan = [
        (1, "VLESS · WS", "vless",
         {"port": port, "path": VLESS_PATH, "fp": "chrome"}),
        (2, "VLESS · XHTTP", "vless",
         {"port": port, "path": XHTTP_PATH, "fp": "chrome", "transport": "xhttp", "mode": XHTTP_MODE}),
        (3, "VLESS · ED2560", "vless",
         {"port": port, "path": f"{VLESS_PATH}?ed=2560", "fp": "firefox"}),
        (4, "TROJAN · WS", "trojan",
         {"port": port, "path": TROJAN_PATH, "fp": "chrome"}),
        (5, "VMESS · WS", "vmess",
         {"port": port, "path": VMESS_PATH, "fp": "chrome"}),
    ]

    desc = {
        "vless": "اتصال پایدار روی هسته VLESS با وب‌سوکت و TLS",
        "trojan": "اتصال روی پروتکل تروجان با وب‌سوکت و TLS، مناسب شبکه‌های سخت‌گیر",
        "vmess": "اتصال روی پروتکل VMess با وب‌سوکت و TLS، گزینه جایگزین",
        "xhttp": "اتصال VLESS روی ترنسپورت XHTTP — سبک، سریع و مقاوم‌تر در برابر فیلترینگ",
    }

    configs = []
    for number, tag, kind, p in plan:
        # اگر هستهٔ Xray قدیمی باشد و ترنسپورت xhttp را نشناسد، این کانفیگ ساخته نمی‌شود
        if p.get("transport") == "xhttp" and not XHTTP_AVAILABLE:
            continue
        text = remark(number)
        # اثر انگشت (fp) فقط در حالت TLS معنا دارد
        alpn = None
        fp = p.get("fp") if tls else None
        if kind == "vless":
            link = _vless_link(
                u_uuid, host, p["port"], p["path"], text,
                tls=tls, alpn=alpn, fp=fp,
                transport=p.get("transport", "ws"), mode=p.get("mode"),
            )
        elif kind == "trojan":
            link = _trojan_link(
                u_uuid, host, p["port"], p["path"], text,
                alpn=alpn, fp=fp, tls=tls,
            )
        else:
            link = _vmess_link(
                u_uuid, host, p["port"], p["path"], text,
                alpn=alpn, fp=fp, tls=tls,
            )

        configs.append({
            "title": f"⚡ {(REMARK_TAG + ' | ') if REMARK_TAG else ''}{number}",
            "desc": desc["xhttp"] if p.get("transport") == "xhttp" else desc[kind],
            "tag": tag,
            "protocol": kind,
            "config": link,
        })

    return configs


# نام قدیمی برای سازگاری با کدهای قبلی
make_all_vless_configs = make_all_configs


def make_fake_config(reason, user, host, port=443, tls=True):
    """تولید کانکشن غیرفعال (نمایشی) جهت اطلاع‌رسانی به کاربر نهایی در نرم‌افزارش"""
    u_uuid = str(uuid.uuid4())
    name = user["name"]
    
    if reason == "limit_exceeded":
        remark = f"⛔️ Traffic Limit Exceeded | {name}"
        desc = "حجم ترافیک مجاز شما به اتمام رسیده است. جهت تمدید اقدام کنید."
    elif reason == "expired":
        remark = f"⛔️ Subscription Expired | {name}"
        desc = "زمان اشتراک شما به پایان رسیده است. جهت تمدید اقدام کنید."
    else:
        remark = f"⛔️ Service Disabled | {name}"
        desc = "سرویس شما توسط ادمین غیرفعال شده است."
        
    remark_encoded = urllib.parse.quote(remark)
    security = "tls" if tls else "none"
    sni = f"&sni={host}" if tls else ""

    config_str = (
        f"vless://{u_uuid}@{host}:{port}"
        f"?path={urllib.parse.quote(VLESS_PATH, safe='')}"
        f"&security={security}"
        f"&encryption=none"
        f"&host={host}"
        f"&fp=chrome"
        f"&type=ws"
        f"{sni}"
        f"#{remark_encoded}"
    )
    return [{
        "title": remark,
        "desc": desc,
        "tag": "Alert",
        "config": config_str
    }]

def get_subscription_header(user):
    """تولید هدر استاندارد مانیتورینگ حجم برای نمایش بالای نرم‌افزار کاربر"""
    used = int(user["used_bytes"])
    total = int(float(user["quota_gb"]) * (1024 ** 3))
    try:
        created_dt = datetime.fromisoformat(user["created_at"])
        expire_dt = created_dt + timedelta(days=int(user["expire_days"]))
        expire_ts = int(expire_dt.timestamp())
    except Exception:
        expire_ts = 0
    return f"upload=0; download={used}; total={total}; expire={expire_ts}"


# ------------------------------------------------------------------
# SYSTEM METRICS (CPU / RAM / DISK)
# ------------------------------------------------------------------

_CPU_LOCK = threading.Lock()
_CPU_PREV = {"total": None, "idle": None}


def _cpu_times():
    """(total, idle) jiffies از /proc/stat"""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return sum(vals), idle
    except Exception:
        return None, None


def cpu_percent():
    """درصد مصرف CPU بر اساس اختلاف دو نمونه"""
    total, idle = _cpu_times()
    if total is None:
        return None, None

    with _CPU_LOCK:
        prev_total, prev_idle = _CPU_PREV["total"], _CPU_PREV["idle"]
        _CPU_PREV["total"], _CPU_PREV["idle"] = total, idle

    if prev_total is None or total <= prev_total:
        # اولین فراخوانی: یک نمونه‌برداری کوتاه
        time.sleep(0.15)
        total2, idle2 = _cpu_times()
        if total2 is None or total2 <= total:
            return 0.0, os.cpu_count()
        pct = 100.0 * (1 - (idle2 - idle) / float(total2 - total))
        with _CPU_LOCK:
            _CPU_PREV["total"], _CPU_PREV["idle"] = total2, idle2
        return round(max(0.0, min(100.0, pct)), 1), os.cpu_count()

    pct = 100.0 * (1 - (idle - prev_idle) / float(total - prev_total))
    return round(max(0.0, min(100.0, pct)), 1), os.cpu_count()


def memory_info():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.split()[0]) * 1024  # kB -> B
    except Exception:
        return None

    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable") or (
        info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0)
    )
    used = max(0, total - avail)
    pct = (used / total * 100.0) if total else 0.0
    return {
        "percent": round(pct, 1),
        "used_gb": round(used / (1024 ** 3), 2),
        "total_gb": round(total / (1024 ** 3), 2),
    }


def disk_info():
    try:
        du = shutil.disk_usage("/")
    except Exception:
        return None
    pct = (du.used / du.total * 100.0) if du.total else 0.0
    return {
        "percent": round(pct, 1),
        "used_gb": round(du.used / (1024 ** 3), 1),
        "total_gb": round(du.total / (1024 ** 3), 1),
    }


def uptime_seconds():
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return 0



# ------------------------------------------------------------------
# آدرس عمومی اشتراک (هاست / پورت / TLS)
# ------------------------------------------------------------------

TLS_PORTS = {443, 8443, 2053, 2083, 2087, 2096}


def resolve_public_endpoint():
    """هاست، پورت و وضعیت TLS عمومیِ لینک‌های ساب را تعیین می‌کند.

    پیش‌فرض عمداً «443 + TLS روشن» است، چون تقریباً همهٔ نصب‌های واقعی پشت
    یک reverse proxy یا CDN روی 443 سرو می‌شوند؛ حتی اگر خودِ پنل روی
    http://…:8080 باز شود، لینک کلاینت نباید روی پورت 80 و بدون TLS ساخته شود.

    ترتیب اولویت:
      1) SUB_TLS  (env) یا تنظیمات sub_tls   → روشن/خاموشِ TLS
      2) SUB_PORT (env) یا تنظیمات sub_port  → پورت عمومی
      3) هدرهای X-Forwarded-Proto/Host       → اگر پشت پروکسی با TLS باشید
      4) پیش‌فرض: پورت 443 و TLS روشن
    """
    host = request.host.split(":")[0]

    def _clean(value):
        return (value or "").strip()

    forced_tls = _clean(os.environ.get("SUB_TLS") or get_setting("sub_tls", "")).lower()
    forced_port = _clean(os.environ.get("SUB_PORT") or get_setting("sub_port", ""))

    # --- 1) وضعیت TLS -------------------------------------------------
    if forced_tls in ("1", "true", "yes", "on", "tls"):
        tls = True
    elif forced_tls in ("0", "false", "no", "off", "none", "notls"):
        tls = False
    else:
        tls = None  # خودکار

    # --- 2) پورت عمومی ------------------------------------------------
    port = int(forced_port) if forced_port.isdigit() else None

    scheme = (
        request.headers.get("X-Forwarded-Proto")
        or request.headers.get("X-Forwarded-Scheme")
        or request.scheme
        or "http"
    ).split(",")[0].strip().lower()

    req_port = request.host.split(":")[1] if ":" in request.host else ""
    req_port = req_port if req_port.isdigit() else None

    if port is not None:
        # پورت دستی تعیین شده؛ اگر TLS هم دستی نیامده باشد از روی پورت حدس می‌زنیم
        if tls is None:
            tls = port in TLS_PORTS
    elif tls is False:
        # صریحاً «بدون TLS» خواسته شده → همان پورتی که پنل رویش باز است
        port = int(req_port) if req_port else 80
    else:
        # خودکار یا TLS روشن → 443 (مگر اینکه درخواست روی یک پورت TLS غیراستاندارد باشد)
        if tls is None and req_port and int(req_port) in TLS_PORTS and scheme == "https":
            port = int(req_port)
        else:
            port = 443
        tls = True

    return host, port, tls


def _public_scheme(host):
    """http یا https برای لینک‌های عمومی (ساب و ...).

    ترتیب تصمیم:
    1. SUB_SCHEME (env)  → اجبار دستی
    2. تنظیمات sub_tls = 0  → http  (یعنی عمداً TLS خاموش است)
    3. X-Forwarded-Proto / request.is_secure → https
    4. دامنهٔ عمومی (نه localhost و نه IP خصوصی) → https
    5. در غیر این صورت → همان scheme درخواست
    """
    forced = (os.environ.get("SUB_SCHEME") or "").strip().lower()
    if forced in ("http", "https"):
        return forced

    try:
        tls_setting = str(os.environ.get("SUB_TLS") or get_setting("sub_tls", "") or "").strip().lower()
    except Exception:
        tls_setting = ""
    if tls_setting == "0":
        return "http"

    proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if request.is_secure or proto in ("https", "wss"):
        return "https"

    hostname = (host or "").split(":")[0].strip().lower()
    if (
        not hostname
        or hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
        or hostname.startswith(("10.", "192.168.", "169.254.", "127."))
        or hostname.endswith(".local")
        or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", hostname)
    ):
        return request.scheme

    return "https"


def build_sub_url(user_uuid):
    """لینک اشتراک عمومی (چیزی که باید در v2rayNG وارد شود)"""
    host = request.host.split(",")[0].strip()
    scheme = _public_scheme(host)
    return f"{scheme}://{host}{SUB_PREFIX}/{user_uuid}"


@app.route("/")
def home():
    if "admin" not in session:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))

@app.context_processor
def _inject_skin():
    # P = پیشوند مسیر پنل برای لینک‌های داخل قالب‌ها ("" یعنی پیشوندی نیست)
    prefix = request.environ.get("SCRIPT_NAME", "") or ""
    return {"skin_css": SKIN_CSS, "skin_name": SKIN, "sub_prefix": SUB_PREFIX,
            "P": prefix}


@app.after_request
def _hide_fingerprint(resp):
    """اثر انگشتِ فریم‌ورک را از پاسخ‌ها برمی‌دارد.

    هدر Server اینجا ست نمی‌شود؛ nginx خودش با server_tokens off آن را می‌نویسد.
    """
    for header in ("X-Powered-By", "X-Runtime", "X-Vega", "Werkzeug"):
        try:
            del resp.headers[header]
        except KeyError:
            pass
    return resp


@app.route("/healthz")
def healthz():
    """چک سلامت عمومی: فقط status (بی‌آزار برای اسکنرها).

    جزئیات محیطی (مسیر باینری‌ها، دیتابیس، اسکین) فقط برای نشستِ لاگین‌شده است،
    تا یک درخواست ساده نتواند هویت پنل را تشخیص دهد.
    """
    if not session.get("admin"):
        return jsonify({"status": "ok"})
    return jsonify({
        "status": "ok",
        "port": PUBLIC_PORT,
        "http_ports": http_listen_ports(),
        "env_port": os.environ.get("PORT", ""),
        "data_dir": os.path.abspath(DATA_DIR),
        "db": os.path.abspath(DB_PATH),
        "xray_bin": shutil.which("xray") or "/usr/local/bin/xray/xray",
        "nginx_bin": shutil.which("nginx") or "",
        "skin": SKIN_CSS,
        "obfuscated_paths": True,
    })


@app.route("/robots.txt")
def _robots_txt():
    """دامنه باید مثل یک سایت معمولی رفتار کند، نه یک پنل."""
    return Response("User-agent: *\nDisallow: /api/\n", mimetype="text/plain")


@app.route("/manifest.json")
def _manifest_json():
    return jsonify({
        "name": "site", "short_name": "site", "start_url": "/",
        "display": "standalone", "background_color": "#ffffff", "theme_color": "#ffffff",
    })


@app.route("/favicon.ico")
def _favicon():
    return redirect(url_for("static", filename="logo.png"))


@app.route("/api/diag")
def api_diag():
    """خودبررسی کامل: هسته، کانفیگ، پورت‌ها، nginx. فقط برای نشستِ لاگین‌شده."""
    if not session.get("admin"):
        return jsonify({"error": "auth"}), 401

    ports = {
        "vless": _port_open(XRAY_PORT),
        "trojan": _port_open(TROJAN_PORT),
        "vmess": _port_open(VMESS_PORT),
        "xhttp": _port_open(XHTTP_PORT),
        "api": _port_open(XRAY_API_PORT),
        "flask": _port_open(FLASK_PORT),
        "public": _port_open(PUBLIC_PORT),
    }
    nginx_bin = shutil.which("nginx") or ""
    nginx_test = ""
    if nginx_bin and os.path.exists(NGINX_CONFIG_PATH):
        try:
            r = subprocess.run([nginx_bin, "-t", "-c", os.path.abspath(NGINX_CONFIG_PATH)],
                               capture_output=True, text=True, timeout=20)
            nginx_test = ((r.stdout or "") + (r.stderr or "")).strip()[-300:]
        except Exception as e:
            nginx_test = f"{type(e).__name__}: {e}"

    ver = xray_version()
    hinted = ""
    if ver and not XHTTP_AVAILABLE:
        hinted = "نسخهٔ Xray از xhttp پشتیبانی نمی‌کند → کانفیگ ۴ حذف شده؛ ایمیج را با v25+ بساز"
    if not XRAY_CONFIG_OK:
        hinted = "Xray با کانفیگ فعلی بالا نمی‌آید (جزئیات در config_error)"

    return jsonify({
        "xray_version": ver,
        "xray_bin": xray_bin_path(),
        "config_ok": XRAY_CONFIG_OK,
        "config_error": XRAY_CONFIG_ERROR[-600:],
        "xhttp_available": XHTTP_AVAILABLE,
        "configs": len(make_all_configs({"uuid": "00000000-0000-0000-0000-000000000000",
                                         "name": "diag"}, "127.0.0.1", 443, True)),
        "ports_listening": ports,
        "nginx_bin": nginx_bin,
        "nginx_test": nginx_test,
        "public_port": PUBLIC_PORT,
        "platform_port": PLATFORM_PORT,
        "paths": {"vless": VLESS_PATH, "trojan": TROJAN_PATH,
                  "vmess": VMESS_PATH, "xhttp": XHTTP_PATH, "sub": SUB_PREFIX},
        "telegram": {
            "telebot_installed": bool(telebot),
            "token_set": bool(get_setting("telegram_token", "")),
            "admin_id_set": bool(get_setting("telegram_admin_id", "")),
            "bot_running": _TG_BOT is not None,
        },
        "hint": hinted,
    })


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        current_username, current_password = get_admin_credentials()

        if username == current_username and password == current_password:
            session["admin"] = True
            session["admin_user"] = username
            _panel_log(f"ورود موفق مدیر · {username}", "info", actor=username)
            return redirect(url_for("dashboard"))

        _panel_log(f"تلاش ناموفق برای ورود · {username or '—'} از {request.remote_addr}",
                   "warn", actor=username or "unknown")

        ep_host, ep_port, ep_tls = _login_endpoint()

        return render_template(
            "login.html",
            error="نام کاربری یا رمز عبور اشتباه است!",
            endpoint_host=ep_host,
            endpoint_port=ep_port,
            endpoint_tls=ep_tls,
        )

    ep_host, ep_port, ep_tls = _login_endpoint()

    return render_template(
        "login.html",
        error=None,
        endpoint_host=ep_host,
        endpoint_port=ep_port,
        endpoint_tls=ep_tls,
    )


def _login_endpoint():
    """مقصد عمومی را برای نمایش در صفحهٔ ورود برمی‌گرداند (بدون خطا در نبودِ تنظیمات)."""
    try:
        return resolve_public_endpoint()
    except Exception:
        host = (request.host or "").split(":")[0]
        return host, 443, True

@app.route("/logout")
def logout():
    _panel_log("خروج از پنل", "info")
    session.pop("admin", None)
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if "admin" not in session:
        return redirect(url_for("login"))

    users = get_all_users()

    total_gb = sum(u["quota_gb"] for u in users)
    total_used = sum(u["used_bytes"] for u in users) / (1024 ** 3)
    active_count = sum(1 for u in users if is_user_active(u)[0])

    used_pct = round((total_used / total_gb) * 100) if total_gb else 0
    used_pct = max(0, min(100, used_pct))
    remaining_gb = round(max(total_gb - total_used, 0), 2)

    # سه باند فشار سهمیه: >70% ، 40-70% ، <40%
    bands = {
        "high": {"n": 0, "gb": 0.0},
        "mid":  {"n": 0, "gb": 0.0},
        "low":  {"n": 0, "gb": 0.0},
    }

    enriched = []

    for u in users:
        quota = u["quota_gb"] or 0
        used = u["used_bytes"] / (1024 ** 3)
        pct = (used / quota * 100) if quota else 0

        key = "high" if pct >= 70 else ("mid" if pct >= 40 else "low")
        bands[key]["n"] += 1
        bands[key]["gb"] += used

        enriched.append({
            "name": u["name"],
            "used_gb": round(used, 2),
            "quota_gb": quota,
            "pct": max(0, min(100, round(pct))),
        })

    band_gb_total = sum(b["gb"] for b in bands.values())
    band_n_total = sum(b["n"] for b in bands.values()) or 1

    for key, band in bands.items():
        band["gb"] = round(band["gb"], 2)

        if band_gb_total > 0:
            band["pct"] = round(band["gb"] / band_gb_total * 100)
        else:
            band["pct"] = round(band["n"] / band_n_total * 100)

    top_users = sorted(enriched, key=lambda x: x["used_gb"], reverse=True)[:5]

    return render_template(
        "dashboard.html",
        users=users,
        total_users=len(users),
        active_users=active_count,
        total_gb=round(total_gb, 2),
        total_used=round(total_used, 2),
        used_pct=used_pct,
        remaining_gb=remaining_gb,
        bands=bands,
        top_users=top_users,
        more_users=max(len(users) - len(top_users), 0),
        avg_used=round(total_used / len(users), 2) if users else 0,
    )


@app.route("/logs")
def logs_page():
    if "admin" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM panel_log")
    total_events = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM panel_log WHERE level='error'")
    error_events = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM panel_log WHERE level='warn'")
    warn_events = c.fetchone()[0]
    conn.close()

    runtime = {}
    for kind in ("access", "error"):
        path, entries, searched = _tail_runtime_log(kind, limit=1)
        runtime[kind] = {"available": bool(path), "path": path or "", "searched": searched}

    return render_template(
        "logs.html",
        total_events=total_events,
        error_events=error_events,
        warn_events=warn_events,
        runtime=runtime,
    )


@app.route("/api/logs")
def api_logs():
    if "admin" not in session:
        return jsonify({"status": "error", "message": "unauthorized"}), 401

    src = (request.args.get("src") or "panel").lower()
    if src not in ("panel", "access", "error"):
        src = "panel"

    try:
        limit = int(request.args.get("limit", 200))
    except ValueError:
        limit = 200
    limit = max(20, min(limit, 1000))

    query = (request.args.get("q") or "").strip().lower()

    entries = []
    source = ""
    searched = []

    if src == "panel":
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT ts, level, actor, message FROM panel_log ORDER BY id DESC LIMIT ?", (limit * 3,))
        rows = c.fetchall()
        conn.close()

        for row in rows:
            item = {
                "ts": row["ts"] or "",
                "level": row["level"] or "info",
                "actor": row["actor"] or "",
                "message": row["message"] or "",
            }
            if query and query not in (item["message"] + " " + item["actor"]).lower():
                continue
            entries.append(item)
            if len(entries) >= limit:
                break

        source = "panel_log.sqlite"
        entries.reverse()  # قدیمی → جدید
    else:
        source, entries, searched = _tail_runtime_log(src, limit=limit * 3)
        if query:
            entries = [e for e in entries if query in e["message"].lower()]
        entries = entries[-limit:]

    counts = {
        "total": len(entries),
        "error": sum(1 for e in entries if e["level"] == "error"),
        "warn": sum(1 for e in entries if e["level"] == "warn"),
    }

    # آمار کل جدول رویدادهای پنل (مستقل از منبع انتخاب‌شده)
    totals = {"total": 0, "warn": 0, "error": 0}
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM panel_log")
        totals["total"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM panel_log WHERE level='warn'")
        totals["warn"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM panel_log WHERE level='error'")
        totals["error"] = c.fetchone()[0]
        conn.close()
    except Exception:
        pass

    return jsonify({
        "status": "ok",
        "src": src,
        "source": source,
        "available": bool(entries) or bool(source),
        "searched": searched,
        "counts": counts,
        "totals": totals,
        "entries": entries,
    })


@app.route("/api/system")
def api_system():
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    cpu_pct, cores = cpu_percent()
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        load = [0, 0, 0]

    return jsonify({
        "status": "success",
        "cpu": {
            "percent": cpu_pct if cpu_pct is not None else 0,
            "cores": cores or 1,
            "load": load,
        },
        "ram": memory_info() or {"percent": 0, "used_gb": 0, "total_gb": 0},
        "disk": disk_info() or {"percent": 0, "used_gb": 0, "total_gb": 0},
        "uptime": uptime_seconds(),
        "host": socket.gethostname(),
    })


@app.route("/users")
def users_page():
    if "admin" not in session:
        return redirect(url_for("login"))

    raw_users = get_all_users()
    users = [enrich_user(u) for u in raw_users]

    total_gb = sum(u["quota_gb"] for u in raw_users)
    total_used = sum(u["used_bytes"] for u in raw_users) / (1024 ** 3)
    active_count = sum(1 for u in users if u["is_active"])
    disabled_count = sum(1 for u in users if u["enabled"] == 0)
    expired_count = sum(1 for u in users if u["is_expired"] or u["is_limit_exceeded"])
    online_count = sum(1 for u in users if u["is_online"])

    return render_template(
        "users.html",
        users=users,
        total_users=len(users),
        active_users=active_count,
        disabled_users=disabled_count,
        expired_users=expired_count,
        online_users=online_count,
        total_gb=round(total_gb, 2),
        total_used=round(total_used, 2)
    )

@app.route("/api/online_users")
def api_online_users():
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    raw_users = get_all_users()
    online = []

    for u in raw_users:
        if is_user_online(u["name"]):
            last_seen = ONLINE_USERS.get(u["name"], 0)
            seconds_ago = int(time.time() - last_seen)

            online.append({
                "id": u["id"],
                "name": u["name"],
                "used_gb": round(u["used_bytes"] / (1024 ** 3), 2),
                "quota_gb": u["quota_gb"],
                "seconds_ago": seconds_ago
            })

    return jsonify({
        "status": "success",
        "count": len(online),
        "users": online
    })

@app.route("/settings")
def settings():
    if "admin" not in session:
        return redirect(url_for("login"))

    username, _ = get_admin_credentials()
    tg_token = get_setting("telegram_token", "")
    tg_admin_id = get_setting("telegram_admin_id", "")

    sub_port = get_setting("sub_port", "")
    sub_tls = get_setting("sub_tls", "")

    try:
        det_host, det_port, det_tls = resolve_public_endpoint()
    except Exception:
        det_host, det_port, det_tls = "", 443, True

    return render_template(
        "settings.html",
        current_username=username,
        telegram_token=tg_token,
        telegram_admin_id=tg_admin_id,
        sub_port=sub_port,
        sub_tls=sub_tls,
        det_host=det_host,
        det_port=det_port,
        det_tls=det_tls
    )

@app.route("/api/settings", methods=["POST"])
def update_settings():
    if "admin" not in session:
        return jsonify({"status": "error", "message": "دسترسی غیرمجاز"}), 401

    data = request.get_json(silent=True) or {}
    action = data.get("action", "")

    if action == "telegram":
        tg_token = data.get("telegram_token", "").strip()
        tg_admin_id = data.get("telegram_admin_id", "").strip()

        # فاصله‌های اضافی/خط جدید از توکن پاک می‌شوند (کپی‌پیست رایج است)
        tg_token = "".join(tg_token.split())
        tg_admin_id = "".join(tg_admin_id.split())

        if tg_token and not re.match(r"^\d{6,}:[A-Za-z0-9_-]{20,}$", tg_token):
            return jsonify({
                "status": "error",
                "message": "قالب توکن درست نیست — باید شبیه 123456789:AA... باشد (از @BotFather)",
            }), 400

        try:
            set_setting("telegram_token", tg_token)
            set_setting("telegram_admin_id", tg_admin_id)
            # اعمال فوری بدون ری‌استارت کانتینر
            reload_telegram_bot()
            return jsonify({"status": "success", "message": "تنظیمات ربات تلگرام با موفقیت ثبت شد"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    elif action == "endpoint":
        sub_port = str(data.get("sub_port", "")).strip()
        sub_tls = str(data.get("sub_tls", "")).strip().lower()

        if sub_port and not sub_port.isdigit():
            return jsonify({"status": "error", "message": "پورت باید عدد باشد"}), 400

        if sub_port and not (0 < int(sub_port) < 65536):
            return jsonify({"status": "error", "message": "پورت باید بین ۱ و ۶۵۵۳۵ باشد"}), 400

        if sub_tls not in ("", "1", "0"):
            return jsonify({"status": "error", "message": "مقدار TLS نامعتبر است"}), 400

        try:
            set_setting("sub_port", sub_port)
            set_setting("sub_tls", sub_tls)
            return jsonify({"status": "success", "message": "تنظیمات پورت و TLS لینک ساب ذخیره شد"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    elif action == "security":
        new_username = data.get("username", "").strip()
        new_password = data.get("password", "")
        current_password = data.get("current_password", "")

        if not new_username:
            return jsonify({"status": "error", "message": "نام کاربری جدید الزامی است"}), 400

        if not new_password:
            return jsonify({"status": "error", "message": "رمز عبور جدید الزامی است"}), 400

        username, password = get_admin_credentials()

        if current_password != password:
            return jsonify({"status": "error", "message": "رمز عبور فعلی اشتباه است"}), 400

        if len(new_username) < 3:
            return jsonify({"status": "error", "message": "نام کاربری حداقل باید ۳ کاراکتر باشد"}), 400

        if len(new_password) < 4:
            return jsonify({"status": "error", "message": "رمز عبور حداقل باید ۴ کاراکتر باشد"}), 400

        try:
            _panel_log(f"نام کاربری مدیر به «{new_username}» تغییر کرد", "warn", actor=username)
            save_admin_credentials(new_username, new_password)
            session.pop("admin", None)
            return jsonify({"status": "success", "message": "اطلاعات ورود تغییر کرد"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "error", "message": "درخواست نامعتبر"}), 400

@app.route("/api/add_user", methods=["POST"])
def add_user():
    if "admin" not in session:
        return jsonify({"status": "error", "message": "دسترسی غیرمجاز"}), 401

    data = request.json or {}

    name = data.get("name", "").strip()

    try:
        quota = float(data.get("quota", 30))
        days = int(data.get("days", 30))
    except Exception:
        return jsonify({"status": "error", "message": "حجم یا تعداد روز نامعتبر است"}), 400

    if not name:
        return jsonify({"status": "error", "message": "نام کاربر الزامی است"}), 400

    if quota <= 0:
        return jsonify({"status": "error", "message": "حجم باید بیشتر از صفر باشد"}), 400

    if days <= 0:
        return jsonify({"status": "error", "message": "تعداد روز باید بیشتر از صفر باشد"}), 400

    user_uuid = str(uuid.uuid4())

    try:
        conn = get_db()
        c = conn.cursor()

        c.execute(
            """
            INSERT INTO users
            (name, uuid, quota_gb, expire_days, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, user_uuid, quota, days, datetime.now().isoformat())
        )

        conn.commit()
        conn.close()

        restart_xray()

        _panel_log(f"کاربر ساخته شد · {name} · {quota:g}GB · {days} روز", "info")
        return jsonify({"status": "success", "message": "کاربر با موفقیت ساخته شد"})

    except sqlite3.IntegrityError:
        return jsonify({"status": "error", "message": "این نام کاربری قبلاً وجود دارد"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/edit_user/<int:user_id>", methods=["POST"])
def edit_user(user_id):
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    data = request.get_json(silent=True) or {}

    try:
        new_quota = float(data.get("quota_gb", 0))
        new_days = int(data.get("expire_days", 0))
    except Exception:
        return jsonify({"status": "error", "message": "مقادیر نامعتبر"}), 400

    if new_quota <= 0 or new_days <= 0:
        return jsonify({"status": "error", "message": "حجم و روز باید بیشتر از صفر باشند"}), 400

    try:
        conn = get_db()
        c = conn.cursor()

        c.execute(
            "UPDATE users SET quota_gb=?, expire_days=? WHERE id=?",
            (new_quota, new_days, user_id)
        )

        conn.commit()
        conn.close()

        # بازسازی هسته چون امکان دارد کاربر ویرایش‌شده تمدید حجم شده باشد و باید دوباره فعال شود
        restart_xray()

        _panel_log(f"کاربر ویرایش شد · #{user_id} · {new_quota:g}GB · {new_days} روز", "info")
        return jsonify({"status": "success", "message": "کاربر با موفقیت ویرایش شد"})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/reset_user/<int:user_id>", methods=["POST"])
def reset_user(user_id):
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    try:
        conn = get_db()
        c = conn.cursor()

        c.execute("SELECT name FROM users WHERE id=?", (user_id,))
        row = c.fetchone()

        if row:
            xray_reset_user_stats(row["name"])

        c.execute(
            "UPDATE users SET used_bytes=0, created_at=? WHERE id=?",
            (datetime.now().isoformat(), user_id)
        )

        conn.commit()
        conn.close()

        # بازسازی فایل کانفیگ برای فعال شدن مجدد کاربر قطع شده در Xray
        restart_xray()

        _panel_log(f"ترافیک کاربر صفر شد · #{user_id}", "info")
        return jsonify({"status": "success", "message": "ترافیک کاربر صفر شد"})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/delete_user/<int:user_id>", methods=["POST"])
def delete_user(user_id):
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT name FROM users WHERE id=?", (user_id,))
    gone = c.fetchone()

    c.execute("DELETE FROM users WHERE id=?", (user_id,))

    conn.commit()
    conn.close()

    restart_xray()

    _panel_log(f"کاربر حذف شد · {gone['name'] if gone else '#' + str(user_id)}", "warn")
    return jsonify({"status": "success", "message": "کاربر با موفقیت حذف شد"})

@app.route("/api/toggle_user/<int:user_id>", methods=["POST"])
def toggle_user(user_id):
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT enabled FROM users WHERE id=?", (user_id,))

    row = c.fetchone()

    if not row:
        conn.close()
        return jsonify({"status": "error", "message": "کاربر یافت نشد"}), 404

    new_val = 0 if row[0] == 1 else 1

    c.execute("UPDATE users SET enabled=? WHERE id=?", (new_val, user_id))

    conn.commit()
    conn.close()

    restart_xray()

    _panel_log(f"کاربر #{user_id} {'فعال' if new_val else 'غیرفعال'} شد", "info")
    return jsonify({"status": "success", "new_state": new_val})

@app.route("/api/user_config/<int:user_id>")
def user_config(user_id):
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM users WHERE id=?", (user_id,))
    user = c.fetchone()

    conn.close()

    if not user:
        return jsonify({"status": "error", "message": "کاربر یافت نشد"}), 404

    host, port, tls = resolve_public_endpoint()

    configs = make_all_configs(dict(user), host, port=port, tls=tls)

    sub_link = build_sub_url(user["uuid"])

    return jsonify({
        "status": "success",
        "configs": configs,
        "sub": sub_link,
        "user": dict(user)
    })

CLIENT_UA_KEYWORDS = [
    "v2ray", "v2rayng", "xray", "clash", "sing-box", "singbox", "hiddify",
    "nekobox", "nekoray", "streisand", "foxray", "shadowrocket", "v2box",
    "quantumult", "surfboard", "kitsunebi", "okhttp", "dart", "go-http",
    "python-requests", "curl", "wget", "axios",
]

BROWSER_UA_KEYWORDS = ["mozilla", "chrome", "safari", "firefox", "edg/", "opr/", "opera"]


def _is_browser_ua(ua):
    """فقط مرورگر واقعی: UA مرورگری داشته باشد و کلیدواژهٔ کلاینت نداشته باشد."""
    ua = (ua or "").lower()
    if any(k in ua for k in CLIENT_UA_KEYWORDS):
        return False
    return any(k in ua for k in BROWSER_UA_KEYWORDS)


def _is_client_ua(ua):
    ua = (ua or "").lower()
    if any(k in ua for k in CLIENT_UA_KEYWORDS):
        return True
    return not any(k in ua for k in BROWSER_UA_KEYWORDS)


def _build_subscription_payload(user_dict, host, port, tls, ascii_names=False):
    active, reason = is_user_active(user_dict)
    if active:
        configs = make_all_configs(user_dict, host, port=port, tls=tls, ascii_names=ascii_names)
    else:
        configs = make_fake_config(reason, user_dict, host, port=port, tls=tls)

    links = []

    for item in configs:
        link = (item.get("config") or "").strip()

        # اعتبارسنجی سخت‌گیرانه: بعضی کلاینت‌ها (v2rayNG) با یک خط خراب
        # کل ساب را رد می‌کنند، پس خط‌های ناسالم اصلاً فرستاده نمی‌شوند.
        try:
            parts = urlsplit(link)
            ok = parts.scheme in ("vless", "vmess", "trojan", "ss") and bool(parts.netloc)
        except Exception:
            ok = False

        if not ok:
            app.logger.warning("sub: config line skipped (invalid URI): %s", link[:80])
            continue

        if any(ch.isspace() for ch in link.split("#")[0]):
            link = link.replace(" ", "%20")
            item["config"] = link

        links.append(link)

    raw_text = "\n".join(links)
    encoded = base64.b64encode(raw_text.encode("utf-8")).decode()
    user_dict["_raw_text"] = raw_text
    return configs, encoded


def _raw_subscription_response(user_dict, encoded, plain_text=None):
    body = encoded if plain_text is None else plain_text
    resp = Response(body, content_type="text/plain; charset=utf-8")
    resp.headers["Subscription-Userinfo"] = get_subscription_header(user_dict)
    resp.headers["Profile-Title"] = "base64:" + base64.b64encode(
        "⚡ Vega".encode("utf-8")
    ).decode()
    resp.headers["Profile-Update-Interval"] = "12"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Expose-Headers"] = "Subscription-Userinfo, Profile-Title"
    return resp


def _load_user(user_uuid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE uuid=?", (user_uuid,))
    user = c.fetchone()
    conn.close()
    return dict(user) if user else None


@app.route(f"{SUB_PREFIX}/<user_uuid>/stats")
def subscription_stats(user_uuid):
    """آمار زندهٔ یک ساب برای نمودار دایره‌ای صفحه (polling سمت کلاینت)."""
    user = _load_user(user_uuid)
    if not user:
        return jsonify({"status": "error", "message": "not found"}), 404

    quota = float(user.get("quota_gb") or 0)
    used_bytes = int(user.get("used_bytes") or 0)
    used_gb = round(used_bytes / (1024 ** 3), 2)
    remaining_gb = round(max(quota - used_gb, 0), 2)
    percent = round((used_gb / quota) * 100, 1) if quota > 0 else 0
    percent = min(percent, 100)

    active, reason = is_user_active(user)

    try:
        created = datetime.fromisoformat(user.get("created_at") or "")
        days_left = max((created + timedelta(days=int(user.get("expire_days") or 0)) - datetime.now()).days, 0)
    except Exception:
        days_left = 0

    resp = jsonify({
        "status": "ok",
        "quota_gb": quota,
        "used_gb": used_gb,
        "remaining_gb": remaining_gb,
        "percent": percent,
        "days_left": days_left,
        "active": bool(active),
        "reason": None if active else reason,
        "updated": datetime.now().strftime("%H:%M:%S"),
    })
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route(f"{SUB_PREFIX}/<user_uuid>/qr.svg")
def subscription_qr(user_uuid):
    """QR لینک اشتراک (برای ایمپورت سریع در کلاینت)."""
    user = _load_user(user_uuid)
    if not user:
        return ("User not found", 404)

    svg = _qr_svg(build_sub_url(user_uuid))
    if not svg:
        return ("QR generator unavailable", 503)

    resp = Response(svg, content_type="image/svg+xml; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route(f"{SUB_PREFIX}/<user_uuid>/qr/<int:index>.svg")
def subscription_config_qr(user_uuid, index):
    """QR یک کانفیگ مشخص (index از ۱)."""
    user = _load_user(user_uuid)
    if not user:
        return ("User not found", 404)

    host, port, tls = resolve_public_endpoint()
    configs, _ = _build_subscription_payload(dict(user), host, port, tls)

    if index < 1 or index > len(configs):
        return ("Config not found", 404)

    svg = _qr_svg(configs[index - 1]["config"])
    if not svg:
        return ("QR generator unavailable", 503)

    resp = Response(svg, content_type="image/svg+xml; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route(f"{SUB_PREFIX}/<user_uuid>/raw")
def subscription_raw(user_uuid):
    """همیشه خروجی خام base64 (برای کلاینت‌ها و تست دستی)"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE uuid=?", (user_uuid,))
    user = c.fetchone()
    conn.close()

    if not user:
        return ("User not found", 404)

    user_dict = dict(user)
    host, port, tls = resolve_public_endpoint()
    _, encoded = _build_subscription_payload(user_dict, host, port, tls)
    return _raw_subscription_response(user_dict, encoded)


@app.route(f"{SUB_PREFIX}/<user_uuid>")
def subscription(user_uuid):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM users WHERE uuid=?", (user_uuid,))
    user = c.fetchone()

    conn.close()

    if not user:
        return ("User not found", 404)

    user_dict = dict(user)
    ua = request.headers.get("User-Agent", "")

    wants_raw = str(request.args.get("raw", "")).lower() in ("1", "true", "yes", "on", "b64", "base64")
    wants_page = str(request.args.get("view", "")).lower() in ("1", "true", "yes", "on", "page")
    wants_plain = str(request.args.get("plain", "")).lower() in ("1", "true", "yes", "on", "text")

    host, port, tls = resolve_public_endpoint()
    all_configs, encoded = _build_subscription_payload(user_dict, host, port, tls)

    # کلاینت‌ها (v2rayNG، Clash، Hiddify، ...) همیشه خروجی خام می‌گیرند
    if wants_plain:
        # خروجی متنی (بدون Base64) — برای دیباگ و کلاینت‌هایی که Base64 دوست ندارند
        return _raw_subscription_response(
            user_dict, encoded, user_dict.get("_raw_text", "")
        )

    if wants_raw:
        return _raw_subscription_response(user_dict, encoded)

    # صفحهٔ HTML فقط وقتی که واقعاً یک مرورگر است: هم UA مرورگری باشد و هم
    # هدر Accept صریحاً text/html بخواهد. هر کلاینت دیگر (v2rayNG، Clash،
    # اپ‌های ناشناس، Dart، okhttp، حتی UA خالی) Base64 می‌گیرد — چون برگرداندن
    # HTML به کلاینت یعنی «Invalid subscription format».
    accept = (request.headers.get("Accept") or "").lower()
    if wants_page or (_is_browser_ua(ua) and "text/html" in accept):
        pass  # ادامه → رندر صفحهٔ وب
    else:
        return _raw_subscription_response(user_dict, encoded)

    # ---------- صفحهٔ وب (مرورگر) ----------
    created_dt = datetime.fromisoformat(user_dict["created_at"])
    elapsed_days = (datetime.now() - created_dt).days
    days_left = max(0, user_dict["expire_days"] - elapsed_days)

    used_gb = round(user_dict["used_bytes"] / (1024 ** 3), 2)
    remaining_gb = max(0.0, round(user_dict["quota_gb"] - used_gb, 2))

    percent = (
        used_gb / user_dict["quota_gb"] * 100
        if user_dict["quota_gb"] > 0 else 0
    )

    resp = Response(render_template(
        "subscription.html",
        user_name=user_dict["name"],
        used_gb=used_gb,
        quota_gb=user_dict["quota_gb"],
        remaining_gb=remaining_gb,
        days_left=days_left,
        percent=round(percent, 1),
        configs=all_configs,
        sub_raw=encoded,
        user_uuid=user_uuid,
        sub_url=build_sub_url(user_uuid),
        sub_qr_svg=_qr_svg(build_sub_url(user_uuid)),
        sub_page_url=request.url.split("?")[0] + "?view=1",
        public_port=port,
        public_tls=tls,
    ))
    resp.headers["Subscription-Userinfo"] = get_subscription_header(user_dict)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- ربات تلگرام
_TG_BOT = None          # نمونهٔ فعال (برای توقف/ری‌لود بدون ری‌استارت کانتینر)
_TG_TOKEN_MASK = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")


def _mask_secret(text):
    """توکن ربات نباید در لاگ‌ها لو برود."""
    return _TG_TOKEN_MASK.sub("***:***", str(text or ""))


def reload_telegram_bot():
    """توکن تازه را بدون ری‌استارت کانتینر اعمال می‌کند.

    stop_polling باعث خروج infinity_polling می‌شود و حلقه دوباره توکن را از
    تنظیمات می‌خواند (خالی ⇒ ربات خاموش، تازه ⇒ ربات با توکن جدید).
    """
    bot = _TG_BOT
    if bot is None:
        return False
    try:
        bot.stop_polling()
    except Exception as e:
        print(f"[Telegram Bot] توقف ربات ناموفق: {_mask_secret(e)}", flush=True)
    return True


def run_telegram_bot_thread():
    global _TG_BOT
    if not telebot:
        print("[Telegram Bot] ماژول telebot نصب نیست (pyTelegramBotAPI) — ربات اجرا نمی‌شود.", flush=True)
        return

    while True:
        try:
            tg_token = get_setting("telegram_token", "").strip()
            tg_admin_id = get_setting("telegram_admin_id", "").strip()

            if not tg_token:
                time.sleep(10)
                continue

            bot = telebot.TeleBot(tg_token)
            _TG_BOT = bot

            @bot.message_handler(commands=['start'])
            def cmd_start(message):
                chat_id = str(message.chat.id)
                if chat_id == tg_admin_id:
                    markup = telebot.types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
                    btn_status = telebot.types.KeyboardButton('📊 وضعیت پنل')
                    btn_users = telebot.types.KeyboardButton('👥 لیست کاربران')
                    markup.add(btn_status, btn_users)
                    
                    bot.send_message(
                        message.chat.id,
                        "⚡ **سلام ادمین گرامی!**\nبه پنل مدیریت تلگرامی Vega خوش آمدید.\nیکی از دکمه‌های زیر را انتخاب کنید:",
                        reply_markup=markup,
                        parse_mode="Markdown"
                    )
                else:
                    bot.send_message(
                        message.chat.id,
                        "👋 **سلام کاربر عزیز!**\nبرای استعلام مشخصات کانکشن خود، لطفاً **نام کاربری** یا **UUID** خود را ارسال کنید:"
                    )

            @bot.message_handler(func=lambda m: True)
            def handle_messages(message):
                text = message.text.strip()
                chat_id = str(message.chat.id)

                if chat_id == tg_admin_id:
                    if text == '📊 وضعیت پنل':
                        users = get_all_users()
                        total = len(users)
                        active = sum(1 for u in users if is_user_active(u)[0])
                        total_bytes = sum(u["used_bytes"] for u in users)
                        total_gb = round(total_bytes / (1024 ** 3), 2)

                        msg = (
                            f"🌐 **وضعیت سرور Vega**\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"👥 کل کاربران: {total} کاربر\n"
                            f"🟢 کاربران فعال: {active} کاربر\n"
                            f"📊 مصرف کل دیتابیس: {total_gb} GB\n"
                            f"━━━━━━━━━━━━━━━━━━"
                        )
                        bot.send_message(chat_id, msg, parse_mode="Markdown")

                    elif text == '👥 لیست کاربران':
                        users = get_all_users()
                        if not users:
                            bot.send_message(chat_id, "هیچ کاربری یافت نشد.")
                            return

                        msg = "👥 **لیست کاربران پنل (نمایش ۱۵ کاربر آخر):**\n\n"
                        for u in users[:15]:
                            active, _ = is_user_active(u)
                            status = "🟢" if active else "🔴"
                            used = round(u["used_bytes"] / (1024 ** 3), 2)
                            msg += f"{status} `{u['name']}` | {used}/{u['quota_gb']} GB\n"

                        if len(users) > 15:
                            msg += f"\nو {len(users) - 15} کاربر دیگر..."

                        bot.send_message(chat_id, msg, parse_mode="Markdown")

                else:
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("SELECT * FROM users WHERE name = ? OR uuid = ?", (text, text))
                    user = c.fetchone()
                    conn.close()

                    if user:
                        u = dict(user)
                        used_gb = round(u["used_bytes"] / (1024 ** 3), 2)
                        
                        active, reason = is_user_active(u)
                        if active:
                            status = "فعال 🟢"
                        else:
                            if reason == "limit_exceeded":
                                status = "اتمام حجم 🔴"
                            elif reason == "expired":
                                status = "پایان اعتبار زمانی 🔴"
                            else:
                                status = "غیرفعال 🔴"
                        
                        try:
                            created_dt = datetime.fromisoformat(u["created_at"])
                            elapsed_days = (datetime.now() - created_dt).days
                            days_left = max(0, u["expire_days"] - elapsed_days)
                        except Exception:
                            days_left = u["expire_days"]

                        msg = (
                            f"👤 **مشخصات اشتراک شما**\n"
                            f"━━━━━━━━━━━━━\n"
                            f"🆔 نام کاربری: `{u['name']}`\n"
                            f"⚡ وضعیت اکانت: {status}\n"
                            f"📊 ترافیک مصرفی: {used_gb} GB\n"
                            f"💾 سقف حجم کل: {u['quota_gb']} GB\n"
                            f"📅 اعتبار باقی‌مانده: {days_left} روز\n"
                            f"━━━━━━━━━━━━━"
                        )
                        bot.send_message(chat_id, msg, parse_mode="Markdown")
                    else:
                        bot.send_message(chat_id, "❌ کاربری با این نام یا UUID یافت نشد.")

            print("[Telegram Bot] polling شروع شد (توکن …" + tg_token[-4:] + ")", flush=True)
            try:
                bot.infinity_polling(timeout=10, long_polling_timeout=5)
            finally:
                if _TG_BOT is bot:
                    _TG_BOT = None

        except Exception as e:
            print("[Telegram Bot] خطا، تلاش دوباره در ۱۰ ثانیه:", _mask_secret(e), flush=True)
            time.sleep(10)

def start_telegram_bot():
    t = threading.Thread(target=run_telegram_bot_thread, daemon=True)
    t.start()


# ---------------------------------------------------------------- دروازهٔ پنل
# پنل پشت یک مسیر تصادفی می‌رود و روی ریشهٔ دامنه یک «سایت بی‌ضرر» سرو می‌شود.
# هدف: کسی که دامنه را باز می‌کند، پنل را نبیند (دلیل اصلی بلاک شدن دامنه‌ها همین است).

_SITE_PAGES = {
    "/": ("Northbeam Analytics", """
        <p>Northbeam Analytics helps small teams turn raw product data into
        decisions. We build lightweight dashboards, scheduled reports and
        alerting that run quietly in the background.</p>
        <h2>What we do</h2>
        <ul>
          <li>Product analytics and cohort reporting</li>
          <li>Scheduled exports and warehouse sync</li>
          <li>Uptime and latency monitoring for small services</li>
        </ul>
        <p>We are a small distributed team. Most of our work is delivered as a
        managed service, so there is nothing to install.</p>"""),
    "/about": ("About — Northbeam Analytics", """
        <p>Northbeam Analytics was founded in 2021 and works with early-stage
        teams across Europe and the Middle East. We keep our stack boring on
        purpose: static sites, a small API and scheduled jobs.</p>
        <h2>How we work</h2>
        <p>Every engagement starts with a two-week measurement sprint. If the
        numbers do not move, we say so and stop there.</p>"""),
    "/contact": ("Contact — Northbeam Analytics", """
        <p>Write to us at <b>hello@northbeam.example</b> and we usually reply
        within one business day.</p>
        <p>For support on an existing engagement, include your workspace name
        and a short description of the issue.</p>"""),
    "/privacy": ("Privacy — Northbeam Analytics", """
        <p>We collect the minimum needed to run the service: account email,
        workspace name and aggregate usage counters. We do not sell or share
        data with third parties.</p>
        <p>Logs are retained for 30 days and then deleted automatically.</p>"""),
}


def _site_html(title, body):
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{color-scheme:light}}
*{{box-sizing:border-box}}
body{{margin:0;font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
color:#1f2430;background:#fbfbfd}}
header{{border-bottom:1px solid #e8e8ee;background:#fff}}
.wrap{{max-width:760px;margin:0 auto;padding:0 20px}}
header .wrap{{display:flex;align-items:center;gap:18px;height:62px}}
.logo{{font-weight:700;letter-spacing:-.2px}}
nav a{{color:#5a6273;text-decoration:none;margin-left:16px;font-size:14px}}
nav a:hover{{color:#1f2430}}
main{{padding:48px 0 64px}}
h1{{font-size:26px;letter-spacing:-.3px;margin:0 0 14px}}
h2{{font-size:17px;margin:28px 0 8px}}
p,li{{color:#3d4351}}
footer{{border-top:1px solid #e8e8ee;padding:20px 0;color:#8a90a0;font-size:13px}}
</style></head>
<body>
<header><div class="wrap">
<span class="logo">Northbeam</span>
<nav><a href="/">Home</a><a href="/about">About</a><a href="/contact">Contact</a><a href="/privacy">Privacy</a></nav>
</div></header>
<main><div class="wrap"><h1>{title.split(' — ')[0]}</h1>{body}</div></main>
<footer><div class="wrap">© 2026 Northbeam Analytics · All rights reserved</div></footer>
</body></html>"""


class _PanelGate:
    """WSGI middleware: پنل فقط زیر PANEL_PREFIX، ریشه برای سایت طعمه."""

    def __init__(self, wsgi_app, panel_prefix, sub_prefix):
        self.wsgi_app = wsgi_app
        self.panel_prefix = (panel_prefix or "").rstrip("/")
        self.sub_prefix = sub_prefix

    def _respond(self, start_response, status, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8")
        headers = [("Content-Type", ctype), ("Content-Length", str(len(data)))]
        headers += (extra or [])
        start_response(status, headers)
        return [data]

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO") or "/"
        method = environ.get("REQUEST_METHOD", "GET")

        # بدون مسیر مخفی ⇒ رفتار قبلی (پنل روی ریشه)
        if not self.panel_prefix:
            return self.wsgi_app(environ, start_response)

        # دارایی‌های استاتیک و لینک ساب همیشه روی ریشه می‌مانند
        if path.startswith("/static/") or path == "/healthz" \
                or path == self.sub_prefix or path.startswith(self.sub_prefix + "/"):
            return self.wsgi_app(environ, start_response)

        # داخل مسیر مخفی ⇒ همان اپ Flask، با SCRIPT_NAME تا url_for درست بسازد
        if path == self.panel_prefix or path.startswith(self.panel_prefix + "/"):
            environ["SCRIPT_NAME"] = self.panel_prefix
            environ["PATH_INFO"] = path[len(self.panel_prefix):] or "/"
            return self.wsgi_app(environ, start_response)

        # بیرون از پنل ⇒ سایت طعمه
        if path in _SITE_PAGES:
            title, body = _SITE_PAGES[path]
            return self._respond(start_response, "200 OK",
                                 _site_html(title, body),
                                 extra=[("Cache-Control", "public, max-age=600")])
        if path == "/robots.txt":
            return self._respond(start_response, "200 OK",
                                 "User-agent: *\nAllow: /\n", "text/plain; charset=utf-8")
        if path == "/sitemap.xml":
            urls = "".join(f"<url><loc>https://{environ.get('HTTP_HOST','')}{p}</loc></url>"
                           for p in _SITE_PAGES)
            return self._respond(start_response, "200 OK",
                                 f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                                 "application/xml; charset=utf-8")
        if path == "/manifest.json":
            return self._respond(start_response, "200 OK",
                                 '{"name":"Northbeam Analytics","short_name":"Northbeam",'
                                 '"start_url":"/","display":"standalone","background_color":"#ffffff",'
                                 '"theme_color":"#ffffff"}', "application/json; charset=utf-8")
        if path == "/favicon.ico":
            return self._respond(start_response, "204 No Content", "", "image/x-icon")

        return self._respond(start_response, "404 Not Found",
                             _site_html("Page not found — Northbeam Analytics",
                                        "<p>This page does not exist. Try the "
                                        "navigation above.</p>"))


app.wsgi_app = _PanelGate(app.wsgi_app, PANEL_PREFIX, SUB_PREFIX)


def main():
    public_port = PUBLIC_PORT

    print(f"[vega] شروع: HTTP={http_listen_ports()} (PORT پلتفرم={os.environ.get('PORT', '-')}) "
          f"DATA_DIR={os.path.abspath(DATA_DIR)} configs=5", flush=True)

    for step, fn in (
        ("init_db", init_db),
        ("restart_xray", restart_xray),
        ("stats_collector", start_stats_collector),
        ("telegram_bot", start_telegram_bot),
    ):
        try:
            fn()
        except Exception as e:
            print(f"[vega] {step} با خطا متوقف شد: {e}", flush=True)

    try:
        admin_user, admin_pass = get_admin_credentials()
        if admin_user == "admin" and admin_pass == "admin":
            print("[vega] هشدار امنیتی: نام کاربری/رمز هنوز admin/admin است — "
                  "از صفحهٔ Settings عوضش کن (در volume ذخیره می‌شود).", flush=True)
    except Exception:
        pass

    try:
        nginx_ok = start_nginx()
    except Exception as e:
        print(f"[vega] start_nginx با خطا متوقف شد: {e}", flush=True)
        nginx_ok = False

    if nginx_ok:
        # nginx ترافیک را روی PORT می‌گیرد و Flask پشتش روی 127.0.0.1 است
        app.run(host="127.0.0.1", port=FLASK_PORT, threaded=True)
    else:
        # بدون nginx: خودِ Flask روی پورت عمومی می‌نشیند تا پنل بالا بیاید.
        # اگر پلتفرم پورت آزاد و غیرِداخلی داده باشد، همان اولویت دارد.
        fallback_port = (
            PLATFORM_PORT if PLATFORM_PORT not in _INTERNAL_PORTS else PUBLIC_PORT
        )
        print(f"[vega] Flask مستقیم روی 0.0.0.0:{fallback_port}", flush=True)
        app.run(host="0.0.0.0", port=fallback_port, threaded=True)


if __name__ == "__main__":
    main()
