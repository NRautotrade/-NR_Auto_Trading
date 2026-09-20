import smtplib
from email.message import EmailMessage
import os
import secrets
import sqlite3
import hashlib
import base64
import json
import asyncio
import time
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

APP_NAME = "NR AUTO TRADING"
DB_PATH = os.getenv("NR_DB_PATH", "nr_users.db")
SECRET_KEY = os.getenv("NR_SESSION_SECRET", "CHANGE-ME-IN-PRODUCTION")
DERIV_CLIENT_ID = os.getenv("DERIV_CLIENT_ID", "")
DERIV_REDIRECT_URI = os.getenv(
    "DERIV_REDIRECT_URI",
    "http://localhost:8000/deriv/callback",
)
ALLOW_REAL_TRADING = os.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"

SMTP_HOST = os.getenv("NR_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("NR_SMTP_PORT", "587"))
SMTP_USER = os.getenv("NR_SMTP_USER", "")
SMTP_PASSWORD = os.getenv("NR_SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("NR_SMTP_FROM", SMTP_USER)

app = FastAPI(title=APP_NAME)

os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=False,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Register the JSON filter used by dashboard.html:
# {{ settings.markets | from_json }}
templates.env.filters["from_json"] = json.loads

# Each logged-in user has isolated in-memory bot state.
BOT_TASKS = {}
BOT_STATE = {}


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            date_of_birth TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }

    if "first_name" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN first_name TEXT NOT NULL DEFAULT ''"
        )

    if "last_name" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN last_name TEXT NOT NULL DEFAULT ''"
        )

    if "date_of_birth" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN date_of_birth TEXT NOT NULL DEFAULT ''"
        )

    if "email" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''"
        )

    # Preserve existing accounts.
    rows = conn.execute(
        "SELECT id, username, email FROM users"
    ).fetchall()

    for row in rows:
        if not row["email"]:
            username = str(row["username"]).strip().lower()

            if "@" in username:
                email = username
            else:
                email = f"legacy-{row['id']}@invalid.local"

            conn.execute(
                "UPDATE users SET email=? WHERE id=?",
                (email, row["id"]),
            )

    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email
        ON users(email)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS deriv_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            account_id TEXT,
            account_type TEXT,
            account_currency TEXT NOT NULL DEFAULT 'USD',
            access_token_encrypted TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    deriv_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(deriv_connections)").fetchall()
    }

    if "account_currency" not in deriv_columns:
        conn.execute(
            "ALTER TABLE deriv_connections ADD COLUMN account_currency TEXT NOT NULL DEFAULT 'USD'"
        )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY,
            markets TEXT NOT NULL DEFAULT '["Volatility 25 Index"]',
            strategies TEXT NOT NULL DEFAULT '["ABC Pattern"]',
            risk_trade REAL NOT NULL DEFAULT 50,
            reward_risk REAL NOT NULL DEFAULT 2,
            daily_target REAL NOT NULL DEFAULT 200,
            max_daily_profit REAL NOT NULL DEFAULT 200,
            max_daily_loss REAL NOT NULL DEFAULT 50,
            protect_tp REAL NOT NULL DEFAULT 50,
            lock_profit_r REAL NOT NULL DEFAULT 1,
            max_trades REAL NOT NULL DEFAULT 15,
            stake_mode TEXT NOT NULL DEFAULT 'Flat Stake',
            martingale_multiplier REAL NOT NULL DEFAULT 2,
            tp_adjust_percent REAL NOT NULL DEFAULT 90,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    settings_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(settings)").fetchall()
    }

    if "markets" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN markets TEXT NOT NULL DEFAULT "
            "'[\"Volatility 25 Index\"]'"
        )

    if "strategies" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN strategies TEXT NOT NULL DEFAULT "
            "'[\"ABC Pattern\"]'"
        )

    if "risk_trade" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN risk_trade REAL NOT NULL DEFAULT 50"
        )

    if "reward_risk" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN reward_risk REAL NOT NULL DEFAULT 2"
        )

    if "daily_target" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN daily_target REAL NOT NULL DEFAULT 200"
        )

    if "max_daily_profit" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN max_daily_profit REAL NOT NULL DEFAULT 200"
        )

    if "max_daily_loss" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN max_daily_loss REAL NOT NULL DEFAULT 50"
        )

    if "protect_tp" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN protect_tp REAL NOT NULL DEFAULT 50"
        )

    if "max_trades" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN max_trades REAL NOT NULL DEFAULT 15"
        )

    if "stake_mode" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN stake_mode TEXT NOT NULL DEFAULT 'Flat Stake'"
        )

    if "martingale_multiplier" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN martingale_multiplier REAL NOT NULL DEFAULT 2"
        )

    if "tp_adjust_percent" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN tp_adjust_percent REAL NOT NULL DEFAULT 90"
        )

    # Reference-bot / live digit scanner settings. These are additive so
    # existing online-bot accounts and settings are preserved.
    digit_columns = {
        "digit_trade_type": "TEXT NOT NULL DEFAULT 'Over/Under'",
        "digit_barrier": "INTEGER NOT NULL DEFAULT 5",
        "digit_duration": "INTEGER NOT NULL DEFAULT 5",
        "digit_duration_unit": "TEXT NOT NULL DEFAULT 't'",
        "digit_min_confidence": "REAL NOT NULL DEFAULT 65",
        "magnet_stage1": "REAL NOT NULL DEFAULT 10",
        "magnet_lock1": "REAL NOT NULL DEFAULT 0",
        "magnet_stage2": "REAL NOT NULL DEFAULT 20",
        "magnet_lock2": "REAL NOT NULL DEFAULT 0.5",
        "magnet_stage3": "REAL NOT NULL DEFAULT 50",
        "magnet_lock3": "REAL NOT NULL DEFAULT 1",
        "magnet_stage4": "REAL NOT NULL DEFAULT 70",
        "magnet_lock4": "REAL NOT NULL DEFAULT 1.5",
    }
    for column, definition in digit_columns.items():
        if column not in settings_columns:
            conn.execute(
                f"ALTER TABLE settings ADD COLUMN {column} {definition}"
            )

    if "lock_profit_r" not in settings_columns:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN lock_profit_r REAL NOT NULL DEFAULT 1"
        )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            expires_at REAL NOT NULL,
            used_at REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# PASSWORDS / EMAIL
# ============================================================

def pw_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt,
        210000,
    )
    return base64.urlsafe_b64encode(salt + digest).decode()


def pw_check(password: str, stored: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(stored.encode())
        salt, digest = raw[:16], raw[16:]
        check = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            salt,
            210000,
        )
        return secrets.compare_digest(digest, check)
    except Exception:
        return False


def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def send_password_reset_email(email: str, reset_link: str):
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError(
            "Password reset email service is not configured."
        )

    def send():
        message = EmailMessage()
        message["Subject"] = "NR AUTO TRADING - Password Reset"
        message["From"] = SMTP_FROM
        message["To"] = email

        message.set_content(
            f"""NR AUTO TRADING

We received a request to reset your password.

Use this link to create a new password:

{reset_link}

This link expires in 30 minutes and can only be used once.

If you did not request this, you can safely ignore this email.
"""
        )

        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(
                SMTP_HOST,
                SMTP_PORT,
                timeout=20,
            ) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(message)
        else:
            with smtplib.SMTP(
                SMTP_HOST,
                SMTP_PORT,
                timeout=20,
            ) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(message)

    await asyncio.to_thread(send)


# ============================================================
# DERIV TOKEN PROTECTION
# ============================================================

def protect_token(token: str) -> str:
    key = os.getenv("NR_TOKEN_SECRET", "")

    if not key:
        raise RuntimeError("NR_TOKEN_SECRET is not configured.")

    import hmac

    stream = b""
    counter = 0
    token_bytes = token.encode()

    while len(stream) < len(token_bytes):
        stream += hmac.new(
            key.encode(),
            counter.to_bytes(4, "big"),
            hashlib.sha256,
        ).digest()
        counter += 1

    cipher = bytes(
        a ^ b
        for a, b in zip(token_bytes, stream)
    )

    mac = hmac.new(
        key.encode(),
        cipher,
        hashlib.sha256,
    ).hexdigest()

    return json.dumps({
        "cipher": base64.urlsafe_b64encode(cipher).decode(),
        "mac": mac,
    })


def unprotect_token(value: str) -> str:
    key = os.getenv("NR_TOKEN_SECRET", "")

    if not key:
        raise RuntimeError("NR_TOKEN_SECRET is not configured.")

    import hmac

    obj = json.loads(value)
    cipher = base64.urlsafe_b64decode(obj["cipher"].encode())

    mac = hmac.new(
        key.encode(),
        cipher,
        hashlib.sha256,
    ).hexdigest()

    if not secrets.compare_digest(mac, obj["mac"]):
        raise RuntimeError(
            "Stored Deriv token failed integrity check."
        )

    stream = b""
    counter = 0

    while len(stream) < len(cipher):
        stream += hmac.new(
            key.encode(),
            counter.to_bytes(4, "big"),
            hashlib.sha256,
        ).digest()
        counter += 1

    return bytes(
        a ^ b
        for a, b in zip(cipher, stream)
    ).decode()


# ============================================================
# USER / SETTINGS HELPERS
# ============================================================

def current_user(request: Request):
    uid = request.session.get("user_id")

    if not uid:
        return None

    conn = db()

    user = conn.execute(
        "SELECT * FROM users WHERE id=?",
        (uid,),
    ).fetchone()

    conn.close()

    return user


def save_settings(user_id, form):
    conn = db()

    strategies = form.getlist("strategies")
    if "Flat Stake" in strategies or "Martingale" in strategies:
        stake_mode = "Martingale" if "Martingale" in strategies else "Flat Stake"
    else:
        stake_mode = "Flat Stake"

    conn.execute(
        """
        INSERT INTO settings
        (
            user_id, markets, strategies, risk_trade, reward_risk,
            daily_target, max_daily_profit, max_daily_loss, protect_tp,
            lock_profit_r, max_trades, stake_mode, martingale_multiplier,
            tp_adjust_percent, digit_trade_type, digit_barrier,
            digit_duration, digit_duration_unit, digit_min_confidence,
            magnet_stage1, magnet_lock1, magnet_stage2, magnet_lock2,
            magnet_stage3, magnet_lock3, magnet_stage4, magnet_lock4
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(user_id) DO UPDATE SET
            markets=excluded.markets, strategies=excluded.strategies,
            risk_trade=excluded.risk_trade, reward_risk=excluded.reward_risk,
            daily_target=excluded.daily_target, max_daily_profit=excluded.max_daily_profit,
            max_daily_loss=excluded.max_daily_loss, protect_tp=excluded.protect_tp,
            lock_profit_r=excluded.lock_profit_r, max_trades=excluded.max_trades,
            stake_mode=excluded.stake_mode, martingale_multiplier=excluded.martingale_multiplier,
            tp_adjust_percent=excluded.tp_adjust_percent, digit_trade_type=excluded.digit_trade_type,
            digit_barrier=excluded.digit_barrier, digit_duration=excluded.digit_duration,
            digit_duration_unit=excluded.digit_duration_unit, digit_min_confidence=excluded.digit_min_confidence,
            magnet_stage1=excluded.magnet_stage1, magnet_lock1=excluded.magnet_lock1,
            magnet_stage2=excluded.magnet_stage2, magnet_lock2=excluded.magnet_lock2,
            magnet_stage3=excluded.magnet_stage3, magnet_lock3=excluded.magnet_lock3,
            magnet_stage4=excluded.magnet_stage4, magnet_lock4=excluded.magnet_lock4
        """,
        (
            user_id,
            json.dumps(form.getlist("markets")),
            json.dumps(strategies),
            float(form.get("risk_trade", 50)),
            float(form.get("reward_risk", 2)),
            float(form.get("daily_target", 200)),
            min(200.0, max(100.0, float(form.get("max_daily_profit", 200)))),
            float(form.get("max_daily_loss", 50)),
            float(form.get("protect_tp", 50)),
            float(form.get("lock_profit_r", 1)),
            min(15.0, max(1.0, float(form.get("max_trades", 15)))),
            stake_mode,
            min(5.0, max(1.0, float(form.get("martingale_multiplier", 2)))),
            min(99.0, max(50.0, float(form.get("tp_adjust_percent", 90)))),
            str(form.get("digit_trade_type", "Over/Under")),
            min(8, max(1, int(float(form.get("digit_barrier", 5))))),
            min(10, max(1, int(float(form.get("digit_duration", 5))))),
            str(form.get("digit_duration_unit", "t")),
            min(95.0, max(50.0, float(form.get("digit_min_confidence", 65)))),
            float(form.get("magnet_stage1", 10)),
            float(form.get("magnet_lock1", 0)),
            float(form.get("magnet_stage2", 20)),
            float(form.get("magnet_lock2", 0.5)),
            float(form.get("magnet_stage3", 50)),
            float(form.get("magnet_lock3", 1)),
            float(form.get("magnet_stage4", 70)),
            float(form.get("magnet_lock4", 1.5)),
        ),
    )

    conn.commit()
    conn.close()


def dashboard_context(request: Request, user, error=None, message=None):
    conn = db()

    settings = conn.execute(
        "SELECT * FROM settings WHERE user_id=?",
        (user["id"],),
    ).fetchone()

    connection = conn.execute(
        """
        SELECT account_id, account_type, account_currency, updated_at
        FROM deriv_connections
        WHERE user_id=?
        """,
        (user["id"],),
    ).fetchone()

    conn.close()

    return {
        "request": request,
        "title": APP_NAME,
        "user": user,
        "settings": settings,
        "connection": connection,
        "allow_real": ALLOW_REAL_TRADING,
        "error": error,
        "message": message,
    }


# ============================================================
# STARTUP / LOGIN
# ============================================================

@app.on_event("startup")
def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = current_user(request)

    if user:
        return RedirectResponse(
            "/dashboard",
            status_code=303,
        )

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "title": APP_NAME,
        },
    )


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    login_value = username.strip().lower()

    conn = db()

    user = conn.execute(
        """
        SELECT *
        FROM users
        WHERE LOWER(username)=?
           OR LOWER(email)=?
        LIMIT 1
        """,
        (login_value, login_value),
    ).fetchone()

    conn.close()

    if not user or not pw_check(
        password,
        user["password_hash"],
    ):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "title": APP_NAME,
                "error": "Invalid email/username or password.",
            },
            status_code=401,
        )

    request.session["user_id"] = user["id"]

    return RedirectResponse(
        "/dashboard",
        status_code=303,
    )


# ============================================================
# REGISTRATION
# ============================================================

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "title": APP_NAME,
        },
    )


@app.post("/register")
async def register(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    date_of_birth: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
):
    first_name = first_name.strip()
    last_name = last_name.strip()
    date_of_birth = date_of_birth.strip()
    email = email.strip().lower()

    if not first_name or not last_name:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "title": APP_NAME,
                "error": "First name and last name are required.",
            },
            status_code=400,
        )

    if not date_of_birth:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "title": APP_NAME,
                "error": "Date of birth is required.",
            },
            status_code=400,
        )

    if "@" not in email or "." not in email.split("@")[-1]:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "title": APP_NAME,
                "error": "Enter a valid email address.",
            },
            status_code=400,
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "title": APP_NAME,
                "error": "Password must be at least 8 characters.",
            },
            status_code=400,
        )

    if password != confirm:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "title": APP_NAME,
                "error": "Passwords do not match.",
            },
            status_code=400,
        )

    # New accounts use email as the internal username.
    username = email

    conn = db()

    try:
        cur = conn.execute(
            """
            INSERT INTO users
            (
                username,
                first_name,
                last_name,
                date_of_birth,
                email,
                password_hash
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                first_name,
                last_name,
                date_of_birth,
                email,
                pw_hash(password),
            ),
        )

        uid = cur.lastrowid

        conn.execute(
            "INSERT INTO settings(user_id) VALUES (?)",
            (uid,),
        )

        conn.commit()

    except sqlite3.IntegrityError:
        conn.close()

        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "title": APP_NAME,
                "error": "An account with that email already exists.",
            },
            status_code=400,
        )

    conn.close()

    request.session["user_id"] = uid

    return RedirectResponse(
        "/dashboard",
        status_code=303,
    )


# ============================================================
# FORGOT / RESET PASSWORD
# ============================================================

@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(
        "forgot_password.html",
        {
            "request": request,
            "title": APP_NAME,
        },
    )


@app.post("/forgot-password")
async def forgot_password(
    request: Request,
    email: str = Form(...),
):
    email = email.strip().lower()

    conn = db()

    user = conn.execute(
        """
        SELECT *
        FROM users
        WHERE LOWER(email)=?
        LIMIT 1
        """,
        (email,),
    ).fetchone()

    if user:
        conn.execute(
            """
            UPDATE password_reset_tokens
            SET used_at=?
            WHERE user_id=?
              AND used_at IS NULL
            """,
            (time.time(), user["id"]),
        )

        token = secrets.token_urlsafe(48)

        conn.execute(
            """
            INSERT INTO password_reset_tokens
            (
                user_id,
                token_hash,
                expires_at
            )
            VALUES (?, ?, ?)
            """,
            (
                user["id"],
                hash_reset_token(token),
                time.time() + (30 * 60),
            ),
        )

        conn.commit()
        conn.close()

        base_url = str(request.base_url).rstrip("/")
        reset_link = (
            f"{base_url}/reset-password?token={token}"
        )

        try:
            await send_password_reset_email(
                email,
                reset_link,
            )
        except Exception:
            pass
    else:
        conn.commit()
        conn.close()

    # Same response whether account exists or not.
    return templates.TemplateResponse(
        "forgot_password.html",
        {
            "request": request,
            "title": APP_NAME,
            "message": (
                "If an account exists for that email, "
                "a password reset link has been sent."
            ),
        },
    )


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(
    request: Request,
    token: str = "",
):
    if not token:
        return templates.TemplateResponse(
            "reset_password.html",
            {
                "request": request,
                "title": APP_NAME,
                "error": "Invalid or expired reset link.",
            },
            status_code=400,
        )

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM password_reset_tokens
        WHERE token_hash=?
          AND used_at IS NULL
          AND expires_at>?
        LIMIT 1
        """,
        (
            hash_reset_token(token),
            time.time(),
        ),
    ).fetchone()

    conn.close()

    if not row:
        return templates.TemplateResponse(
            "reset_password.html",
            {
                "request": request,
                "title": APP_NAME,
                "error": "Invalid or expired reset link.",
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        "reset_password.html",
        {
            "request": request,
            "title": APP_NAME,
            "token": token,
        },
    )


@app.post("/reset-password")
async def reset_password(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
):
    if len(password) < 8:
        return templates.TemplateResponse(
            "reset_password.html",
            {
                "request": request,
                "title": APP_NAME,
                "token": token,
                "error": "Password must be at least 8 characters.",
            },
            status_code=400,
        )

    if password != confirm:
        return templates.TemplateResponse(
            "reset_password.html",
            {
                "request": request,
                "title": APP_NAME,
                "token": token,
                "error": "Passwords do not match.",
            },
            status_code=400,
        )

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM password_reset_tokens
        WHERE token_hash=?
          AND used_at IS NULL
          AND expires_at>?
        LIMIT 1
        """,
        (
            hash_reset_token(token),
            time.time(),
        ),
    ).fetchone()

    if not row:
        conn.close()

        return templates.TemplateResponse(
            "reset_password.html",
            {
                "request": request,
                "title": APP_NAME,
                "error": "Invalid or expired reset link.",
            },
            status_code=400,
        )

    conn.execute(
        """
        UPDATE users
        SET password_hash=?
        WHERE id=?
        """,
        (
            pw_hash(password),
            row["user_id"],
        ),
    )

    conn.execute(
        """
        UPDATE password_reset_tokens
        SET used_at=?
        WHERE id=?
        """,
        (
            time.time(),
            row["id"],
        ),
    )

    conn.commit()
    conn.close()

    return RedirectResponse(
        "/?reset=1",
        status_code=303,
    )


# ============================================================
# PROFILE
# ============================================================

@app.post("/profile")
async def update_profile(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    date_of_birth: str = Form(...),
    email: str = Form(...),
):
    user = current_user(request)

    if not user:
        return RedirectResponse("/", status_code=303)

    first_name = first_name.strip()
    last_name = last_name.strip()
    date_of_birth = date_of_birth.strip()
    email = email.strip().lower()

    if not first_name or not last_name or not date_of_birth:
        return RedirectResponse(
            "/dashboard?profile_error=missing",
            status_code=303,
        )

    if "@" not in email:
        return RedirectResponse(
            "/dashboard?profile_error=email",
            status_code=303,
        )

    conn = db()

    duplicate = conn.execute(
        """
        SELECT id
        FROM users
        WHERE LOWER(email)=?
          AND id<>?
        LIMIT 1
        """,
        (email, user["id"]),
    ).fetchone()

    if duplicate:
        conn.close()

        return RedirectResponse(
            "/dashboard?profile_error=duplicate",
            status_code=303,
        )

    conn.execute(
        """
        UPDATE users
        SET first_name=?,
            last_name=?,
            date_of_birth=?,
            email=?,
            username=?
        WHERE id=?
        """,
        (
            first_name,
            last_name,
            date_of_birth,
            email,
            email,
            user["id"],
        ),
    )

    conn.commit()
    conn.close()

    return RedirectResponse(
        "/dashboard?profile_updated=1",
        status_code=303,
    )


# ============================================================
# LOGOUT / DASHBOARD / SETTINGS
# ============================================================

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = current_user(request)

    if not user:
        return RedirectResponse("/", status_code=303)

    error = None
    message = None

    if request.query_params.get("profile_updated") == "1":
        message = "Profile updated successfully."

    profile_error = request.query_params.get("profile_error")

    if profile_error == "missing":
        error = "First name, last name and date of birth are required."
    elif profile_error == "email":
        error = "Enter a valid email address."
    elif profile_error == "duplicate":
        error = "That email address is already in use."

    return templates.TemplateResponse(
        "dashboard.html",
        dashboard_context(
            request,
            user,
            error=error,
            message=message,
        ),
    )


@app.post("/settings")
async def update_settings(request: Request):
    user = current_user(request)

    if not user:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    save_settings(user["id"], form)

    return RedirectResponse(
        "/dashboard",
        status_code=303,
    )


# ============================================================
# DERIV OAUTH
# ============================================================

def render_deriv_result(request: Request, message: str, status_code: int = 400):
    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "title": APP_NAME,
            "message": message,
        },
        status_code=status_code,
    )


def deriv_account_id(account: dict) -> str:
    return str(
        account.get("id")
        or account.get("account_id")
        or account.get("loginid")
        or account.get("login_id")
        or ""
    ).strip()


def deriv_account_type(account: dict) -> str:
    """Normalize Deriv account records into demo or real."""
    account_id = deriv_account_id(account).upper()

    if account.get("is_virtual") is True:
        return "demo"

    raw_type = str(
        account.get("account_type")
        or account.get("type")
        or account.get("accountType")
        or ""
    ).lower().strip()

    if raw_type in {"demo", "virtual", "practice", "paper"}:
        return "demo"
    if raw_type in {"real", "live", "cash"}:
        return "real"

    company = str(
        account.get("landing_company_name")
        or account.get("landing_company")
        or account.get("landingCompany")
        or ""
    ).lower()

    if "virtual" in company or "demo" in company:
        return "demo"

    if account_id.startswith(("VRTC", "VR")):
        return "demo"

    if account_id.startswith(("CR", "MF", "SVG")):
        return "real"

    return "real"


def extract_deriv_accounts(payload: dict) -> list[dict]:
    """Accept the account-list response formats used by Deriv."""
    if not isinstance(payload, dict):
        return []

    candidates = [payload.get("data"), payload.get("accounts"), payload]

    for data in candidates:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]

        if isinstance(data, dict):
            for key in ("accounts", "items", "results", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]

            if deriv_account_id(data):
                return [data]

    return []


@app.get("/deriv/connect")
async def deriv_connect(request: Request, mode: str = "demo"):
    user = current_user(request)

    if not user:
        return RedirectResponse("/", status_code=303)

    if not DERIV_CLIENT_ID:
        return RedirectResponse("/dashboard?oauth_error=client", status_code=303)

    mode = str(mode or "demo").lower().strip()
    if mode not in {"demo", "real"}:
        mode = "demo"

    if mode == "real" and not ALLOW_REAL_TRADING:
        return RedirectResponse("/dashboard?oauth_error=real_locked", status_code=303)

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    ).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)

    request.session["oauth_verifier"] = verifier
    request.session["oauth_state"] = state
    request.session["oauth_mode"] = mode

    params = {
        "response_type": "code",
        "client_id": DERIV_CLIENT_ID,
        "redirect_uri": DERIV_REDIRECT_URI,
        "scope": "trade",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }

    if request.query_params.get("signup") == "1":
        params["prompt"] = "registration"

    return RedirectResponse(
        "https://auth.deriv.com/oauth2/auth?" + urlencode(params),
        status_code=303,
    )


@app.get("/deriv/callback")
async def deriv_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    user = current_user(request)

    if not user:
        return RedirectResponse("/", status_code=303)

    if error:
        return render_deriv_result(
            request,
            f"Deriv authorization was not completed: {error}",
        )

    saved_state = request.session.pop("oauth_state", None)
    verifier = request.session.pop("oauth_verifier", None)
    mode = str(request.session.pop("oauth_mode", "demo")).lower()
    mode = mode if mode in {"demo", "real"} else "demo"

    if not code:
        return render_deriv_result(request, "Deriv did not return an authorization code.")

    if not state or state != saved_state:
        return render_deriv_result(
            request,
            "OAuth security check failed. Please start again.",
        )

    if not verifier:
        return render_deriv_result(
            request,
            "OAuth session expired. Please start again.",
        )

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            token_resp = await client.post(
                "https://auth.deriv.com/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": DERIV_CLIENT_ID,
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": DERIV_REDIRECT_URI,
                },
                headers={"Accept": "application/json"},
            )

        if token_resp.status_code >= 400:
            return render_deriv_result(
                request,
                "Deriv token exchange failed. "
                f"HTTP {token_resp.status_code}: {token_resp.text[:700]}",
            )

        token_payload = token_resp.json()
        token = token_payload.get("access_token")
        if not token:
            return render_deriv_result(
                request,
                f"Deriv did not return an access token: {token_payload}",
            )

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            account_resp = await client.get(
                "https://api.derivws.com/trading/v1/options/accounts",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Deriv-App-ID": str(DERIV_CLIENT_ID),
                    "Accept": "application/json",
                },
            )

        if account_resp.status_code >= 400:
            return render_deriv_result(
                request,
                "Deriv authorization succeeded, but the account list "
                f"could not be retrieved. HTTP {account_resp.status_code}: "
                f"{account_resp.text[:700]}",
            )

        try:
            account_payload = account_resp.json()
        except ValueError:
            return render_deriv_result(
                request,
                "Deriv returned an invalid account-list response.",
            )

        accounts = extract_deriv_accounts(account_payload)
        matching = [
            account for account in accounts
            if deriv_account_id(account)
            and deriv_account_type(account) == mode
        ]

        # Some Deriv responses omit the account-type fields. For demo mode,
        # safely accept the first returned account when no type metadata exists.
        if not matching and mode == "demo":
            unknown_accounts = [
                account for account in accounts
                if deriv_account_id(account)
                and not any(
                    key in account
                    for key in (
                        "is_virtual",
                        "account_type",
                        "type",
                        "accountType",
                        "landing_company_name",
                        "landing_company",
                    )
                )
            ]
            if len(unknown_accounts) == 1:
                matching = unknown_accounts

        if not matching:
            available = [
                f"{deriv_account_id(account)} ({deriv_account_type(account)})"
                for account in accounts
                if deriv_account_id(account)
            ]
            return render_deriv_result(
                request,
                f"No {mode} Deriv account was found. "
                f"Accounts returned: {', '.join(available) or 'none'}",
            )

        selected = matching[0]
        account_id = deriv_account_id(selected)
        account_currency = str(
            selected.get("currency")
            or selected.get("account_currency")
            or "USD"
        ).upper().strip() or "USD"
        encrypted_token = protect_token(token)

        conn = db()
        try:
            conn.execute(
                """
                INSERT INTO deriv_connections
                (
                    user_id,
                    account_id,
                    account_type,
                    account_currency,
                    access_token_encrypted,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    account_id=excluded.account_id,
                    account_type=excluded.account_type,
                    account_currency=excluded.account_currency,
                    access_token_encrypted=excluded.access_token_encrypted,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (user["id"], account_id, mode, account_currency, encrypted_token),
            )
            conn.commit()
        finally:
            conn.close()

        request.session["deriv_connected"] = True
        request.session["deriv_account_id"] = account_id
        request.session["deriv_account_type"] = mode

        return RedirectResponse("/dashboard?connected=1", status_code=303)

    except httpx.RequestError as exc:
        return render_deriv_result(
            request,
            f"Could not reach Deriv: {type(exc).__name__}: {exc}",
        )
    except Exception as exc:
        return render_deriv_result(
            request,
            f"Deriv connection failed: {type(exc).__name__}: {exc}",
        )

# ============================================================
# DERIV WEBSOCKET
# ============================================================

async def deriv_ws_url(
    account_id: str,
    token: str,
):
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            (
                "https://api.derivws.com/trading/v1/options/"
                f"accounts/{account_id}/otp"
            ),
            headers={
                "Authorization": f"Bearer {token}",
                "Deriv-App-ID": DERIV_CLIENT_ID,
            },
        )

    if resp.status_code >= 400:
        raise RuntimeError(
            "Deriv rejected WebSocket authentication."
        )

    url = resp.json().get(
        "data",
        {},
    ).get("url")

    if not url:
        raise RuntimeError(
            "Deriv did not return a WebSocket URL."
        )

    return url


async def ws_request(
    ws,
    payload,
    req_id,
    timeout=15,
):
    payload = dict(payload)
    payload["req_id"] = req_id

    await ws.send(
        json.dumps(payload)
    )

    while True:
        msg = json.loads(
            await asyncio.wait_for(
                ws.recv(),
                timeout=timeout,
            )
        )

        if msg.get("req_id") != req_id:
            continue

        if msg.get("error"):
            raise RuntimeError(
                msg["error"].get(
                    "message",
                    "Deriv API error.",
                )
            )

        return msg


async def get_active_symbols(ws):
    msg = await ws_request(
        ws,
        {"active_symbols": "brief"},
        100,
    )

    return msg.get(
        "active_symbols",
        [],
    )

async def fetch_live_balance(account_id, token):
    ws_url = await deriv_ws_url(account_id, token)

    async with websockets.connect(
        ws_url,
        open_timeout=15,
        close_timeout=5,
        ping_interval=20,
    ) as ws:
        msg = await ws_request(
            ws,
            {"balance": 1},
            901,
            timeout=10,
        )

    data = msg.get("balance", {})
    return (
        float(data.get("balance", 0) or 0),
        data.get("currency", "USD"),
    )



def resolve_online_symbol(
    active_symbols,
    wanted,
):
    import re

    target = wanted.lower().strip()

    aliases = {
        "step index": ["step index"],
        "volatility 5 index": ["volatility 5"],
        "volatility 10 index": ["volatility 10"],
        "volatility 15 index": ["volatility 15"],
        "volatility 25 index": ["volatility 25"],
        "volatility 30 index": ["volatility 30"],
        "volatility 50 index": ["volatility 50"],
        "volatility 75 index": ["volatility 75"],
        "volatility 100 index": ["volatility 100"],
    }

    names = aliases.get(
        target,
        [target],
    )

    def norm(value):
        value = str(value or "").lower()
        value = re.sub(
            r"\([^)]*\)",
            " ",
            value,
        )
        value = re.sub(
            r"[^a-z0-9]+",
            " ",
            value,
        )
        return " ".join(value.split())

    wanted_names = [
        norm(name)
        for name in names
    ]

    for item in active_symbols:
        api_symbol = (
            item.get("underlying_symbol")
            or item.get("symbol")
        )

        if not api_symbol:
            continue

        fields = (
            item.get("underlying_symbol_name"),
            item.get("underlying_symbol"),
            item.get("display_name"),
            item.get("name"),
            item.get("symbol"),
        )

        normalized = [
            norm(value)
            for value in fields
            if value
        ]

        if any(
            wanted_name
            and any(
                wanted_name in value
                for value in normalized
            )
            for wanted_name in wanted_names
        ):
            return api_symbol

    return None


# ============================================================
# ABC STRATEGY
# ============================================================

def abc_signal(
    candles,
    swing_len=2,
):
    if len(candles) < 20:
        return None

    def candle_high(c):
        if isinstance(c, dict):
            return float(c["high"])
        return float(c[2])

    def candle_low(c):
        if isinstance(c, dict):
            return float(c["low"])
        return float(c[3])

    hi = [
        candle_high(c)
        for c in candles
    ]

    lo = [
        candle_low(c)
        for c in candles
    ]

    highs = []
    lows = []

    for i in range(
        swing_len,
        len(candles) - swing_len,
    ):
        if all(
            hi[i] > hi[i - j]
            and hi[i] > hi[i + j]
            for j in range(
                1,
                swing_len + 1,
            )
        ):
            highs.append(
                (i, hi[i])
            )

        if all(
            lo[i] < lo[i - j]
            and lo[i] < lo[i + j]
            for j in range(
                1,
                swing_len + 1,
            )
        ):
            lows.append(
                (i, lo[i])
            )

    # Bearish ABC
    if len(highs) >= 2 and len(lows) >= 1:
        ai, A = highs[-2]
        ci, C = highs[-1]

        mids = [
            x
            for x in lows
            if ai < x[0] < ci
        ]

        if mids and C < A:
            bi, B = mids[-1]

            return (
                "PUT",
                ai,
                bi,
                ci,
                A,
                B,
                C,
            )

    # Bullish ABC
    if len(lows) >= 2 and len(highs) >= 1:
        ai, A = lows[-2]
        ci, C = lows[-1]

        mids = [
            x
            for x in highs
            if ai < x[0] < ci
        ]

        if mids and C > A:
            bi, B = mids[-1]

            return (
                "CALL",
                ai,
                bi,
                ci,
                A,
                B,
                C,
            )

    return None


async def fetch_m5_candles(
    ws,
    symbol,
):
    msg = await ws_request(
        ws,
        {
            "ticks_history": symbol,
            "end": "latest",
            "count": 100,
            "style": "candles",
            "granularity": 300,
        },
        200 + abs(hash(symbol)) % 1000,
    )

    return msg.get(
        "candles",
        [],
    )


# ============================================================
# LIVE DEMO BOT WORKER
# ============================================================

async def demo_bot_worker(
    user_id,
    account_id,
    token,
    markets,
    risk,
    rr,
    max_daily_profit=200.0,
    max_trades=15,
    stake_mode="Flat Stake",
    martingale_multiplier=2.0,
    tp_adjust_percent=90.0,
):
    state = BOT_STATE[user_id]

    reconnecting = bool(state.pop("_reconnecting", False))

    if not reconnecting:
        state.update({
            "trades": 0,
            "balance": 0.0,
            "equity": 0.0,
            "today_pl": 0.0,
            "wins": 0,
            "losses": 0,
            "positions": [],
            "last_trade": None,
            "activity": [],
            "_position_map": {},
            "_market_restart_at": {},
            "_stake_level": 0,
            "paused": False,
        })

    state.update({
        "running": True,
        "mode": "demo",
        "message": (
            "Reconnecting to Deriv..."
            if reconnecting
            else "Starting demo trading worker..."
        ),
    })

    try:
        ws_url = await deriv_ws_url(
            account_id,
            token,
        )

        async with websockets.connect(
            ws_url,
            open_timeout=15,
            close_timeout=5,
            ping_interval=20,
        ) as ws:

            active = await get_active_symbols(ws)

            balance_msg = await ws_request(
                ws,
                {"balance": 1},
                11,
            )

            account_balance = float(
                balance_msg.get(
                    "balance",
                    {},
                ).get(
                    "balance",
                    0,
                )
                or 0
            )

            state["balance"] = account_balance
            state["equity"] = account_balance

            symbols = {
                market: resolve_online_symbol(
                    active,
                    market,
                )
                for market in markets
            }

            symbols = {
                market: symbol
                for market, symbol in symbols.items()
                if symbol
            }

            if not symbols:
                raise RuntimeError(
                    "None of the selected markets are currently "
                    "available on Deriv API."
                )

            state["symbols"] = symbols

            state["message"] = (
                "Demo worker is running. "
                "Waiting for ABC setups..."
            )

            last_setup = {}
            open_contracts = {}
            request_counter = 6000

            while not state.get("stop_requested"):

                # PAUSE means keep the Deriv connection alive and monitor
                # existing contracts, but do not open new contracts.
                if state.get("paused"):
                    state["message"] = "Bot paused. Existing demo trades are still monitored."

                # --------------------------------------------------------
                # UPDATE ALL OPEN CONTRACTS
                # --------------------------------------------------------
                open_profit = 0.0

                for market, contract_id in list(
                    open_contracts.items()
                ):
                    request_counter += 1

                    try:
                        msg = await ws_request(
                            ws,
                            {
                                "proposal_open_contract": 1,
                                "contract_id": contract_id,
                            },
                            request_counter,
                        )

                        c = msg.get(
                            "proposal_open_contract",
                            {},
                        )

                        position = (
                            state["_position_map"].get(market)
                            or {}
                        )

                        profit = float(
                            c.get("profit", 0)
                            or 0
                        )

                        status = str(
                            c.get(
                                "status",
                                "open",
                            )
                        ).lower()

                        position.update({
                            "symbol": market,
                            "direction": position.get(
                                "direction",
                                "",
                            ),
                            "entry": c.get(
                                "buy_price",
                                position.get(
                                    "entry",
                                    0,
                                ),
                            ),
                            "current": c.get(
                                "bid_price",
                                c.get(
                                    "current_spot",
                                    position.get(
                                        "current",
                                        0,
                                    ),
                                ),
                            ),
                            "profit": profit,
                            "status": (
                                "OPEN"
                                if not c.get("is_sold")
                                else status.upper()
                            ),
                            "contract_id": contract_id,
                            "entry_spot": c.get(
                                "entry_spot"
                            ),
                            "current_spot": c.get(
                                "current_spot"
                            ),
                        })

                        state["_position_map"][market] = position

                        # Fixed-duration CALL/PUT contracts do not have a
                        # CFD-style price TP. Instead, optionally request an
                        # early cash-out when the live profit reaches the
                        # configured percentage of the maximum contract profit.
                        if (
                            not c.get("is_sold")
                            and max(0.0, profit) > 0
                            and float(position.get("max_profit", 0) or 0) > 0
                            and profit >= float(position.get("max_profit", 0)) * (float(tp_adjust_percent) / 100.0)
                            and not position.get("tp_requested")
                        ):
                            try:
                                request_counter += 1
                                await ws_request(
                                    ws,
                                    {"sell": contract_id, "price": 0},
                                    request_counter,
                                )
                                position["tp_requested"] = True
                                state["message"] = (
                                    f"{market}: early TP requested at "
                                    f"{float(tp_adjust_percent):.0f}% of max profit."
                                )
                            except Exception:
                                # If early selling is unavailable for the contract,
                                # leave it running to normal settlement.
                                pass

                        settled = (
                            bool(c.get("is_sold"))
                            or status in {
                                "won",
                                "lost",
                                "sold",
                                "expired",
                            }
                        )

                        if not settled:
                            open_profit += profit
                            continue

                        # ------------------------------------------------
                        # SETTLED TRADE
                        # ------------------------------------------------
                        if status == "won":
                            state["wins"] = int(
                                state.get("wins", 0)
                            ) + 1

                        elif status in {
                            "lost",
                            "expired",
                        }:
                            state["losses"] = int(
                                state.get("losses", 0)
                            ) + 1

                        state["today_pl"] = float(
                            state.get(
                                "today_pl",
                                0,
                            )
                            or 0
                        ) + profit

                        closed_trade = {
                            **position,
                            "profit": profit,
                            "status": status.upper(),
                            "is_open": False,
                        }

                        state["last_trade"] = closed_trade

                        activity = state.setdefault(
                            "activity",
                            [],
                        )

                        activity.insert(
                            0,
                            (
                                f"{market}: "
                                f"{status.upper()} "
                                f"P/L ${profit:+.2f}"
                            ),
                        )

                        state["activity"] = activity[:20]

                        state["_position_map"].pop(
                            market,
                            None,
                        )

                        # Restart this market only on the next 10-minute mark.
                        now_ts = time.time()
                        next_mark = (int(now_ts) // 600 + 1) * 600
                        state.setdefault("_market_restart_at", {})[market] = next_mark

                        if profit < 0 and stake_mode == "Martingale":
                            state["_stake_level"] = min(6, int(state.get("_stake_level", 0)) + 1)
                        elif profit >= 0:
                            state["_stake_level"] = 0

                        open_contracts.pop(
                            market,
                            None,
                        )

                        state["message"] = (
                            f"{market}: contract "
                            f"{status.upper()} - "
                            f"P/L ${profit:+.2f}"
                        )

                    except websockets.exceptions.ConnectionClosed:
                        raise
                    except Exception:
                        continue

                # Build the complete open-position list.
                state["positions"] = list(
                    state["_position_map"].values()
                )

                # Equity = balance + live open P/L.
                state["equity"] = (
                    float(
                        state.get(
                            "balance",
                            0,
                        )
                        or 0
                    )
                    + float(open_profit or 0)
                )

                # --------------------------------------------------------
                # DAILY PROFIT CAP / MAX TRADES
                # --------------------------------------------------------
                daily_cap = min(200.0, max(100.0, float(max_daily_profit or 200)))
                trade_cap = min(15, max(1, int(max_trades or 15)))
                if int(state.get("trades", 0) or 0) >= trade_cap:
                    state["message"] = (
                        f"Maximum {trade_cap} trades reached for today. "
                        "No new trades until the next day."
                    )
                    await asyncio.sleep(10)
                    continue
                if float(state.get("today_pl", 0) or 0) >= daily_cap:
                    state["message"] = (
                        f"Daily profit limit ${daily_cap:.0f} reached. "
                        "No new trades until the next day."
                    )
                    await asyncio.sleep(10)
                    continue

                # --------------------------------------------------------
                # SCAN SELECTED MARKETS
                # --------------------------------------------------------
                for market, symbol in symbols.items():
                    if state.get("paused"):
                        break
                    if int(state.get("trades", 0) or 0) >= trade_cap:
                        state["message"] = (
                            f"Maximum {trade_cap} trades reached for today. "
                            "No new trades until the next day."
                        )
                        break
                    if float(state.get("today_pl", 0) or 0) >= daily_cap:
                        state["message"] = (
                            f"Daily profit limit ${daily_cap:.0f} reached. "
                            "No new trades until the next day."
                        )
                        break

                    restart_at = float(state.get("_market_restart_at", {}).get(market, 0) or 0)
                    if time.time() < restart_at:
                        continue

                    if (
                        market in open_contracts
                        or state.get("stop_requested")
                    ):
                        continue

                    try:
                        candles = await fetch_m5_candles(
                            ws,
                            symbol,
                        )

                        signal = abc_signal(candles)

                        if not signal:
                            continue

                        (
                            direction,
                            ai,
                            bi,
                            ci,
                            A,
                            B,
                            C,
                        ) = signal

                        key = (
                            direction,
                            ai,
                            bi,
                            ci,
                        )

                        if last_setup.get(market) == key:
                            continue

                        last_setup[market] = key

                        # Refresh live balance before proposal.
                        request_counter += 1

                        bal_msg = await ws_request(
                            ws,
                            {"balance": 1},
                            request_counter,
                        )

                        balance_data = bal_msg.get(
                            "balance",
                            {},
                        )

                        balance = float(
                            balance_data.get(
                                "balance",
                                0,
                            )
                            or 0
                        )

                        state["balance"] = balance

                        # Base stake is the configured risk, capped at 2% of
                        # the member's live demo balance for safety.
                        base_stake = min(
                            float(risk),
                            max(0.35, balance * 0.02),
                        )
                        if stake_mode == "Martingale":
                            level = int(state.get("_stake_level", 0) or 0)
                            stake = base_stake * (float(martingale_multiplier) ** level)
                            stake = min(stake, balance * 0.10)
                        else:
                            stake = base_stake

                        stake = round(max(0.35, stake), 2)

                        if balance <= 0 or stake > balance:
                            continue

                        request_counter += 1

                        proposal = await ws_request(
                            ws,
                            {
                                "proposal": 1,
                                "amount": round(
                                    stake,
                                    2,
                                ),
                                "basis": "stake",
                                "contract_type": direction,
                                "currency": balance_data.get(
                                    "currency",
                                    "USD",
                                ),
                                "duration": 5,
                                "duration_unit": "m",
                                "underlying_symbol": symbol,
                            },
                            request_counter,
                        )

                        prop = proposal.get(
                            "proposal",
                            {},
                        )

                        proposal_id = prop.get("id")

                        ask = float(
                            prop.get(
                                "ask_price",
                                stake,
                            )
                            or stake
                        )

                        payout = float(
                            prop.get(
                                "payout",
                                0,
                            )
                            or 0
                        )

                        expected_profit = (
                            payout - ask
                        )

                        # IMPORTANT: For Deriv fixed-payout CALL/PUT contracts,
                        # the dashboard RR setting is not a CFD stop-loss/TP
                        # ratio. Do not reject valid signals because payout
                        # does not equal 2R. The contract itself controls the
                        # fixed payout/loss.
                        if not proposal_id:
                            state["message"] = (
                                f"{market}: {direction} proposal was not returned."
                            )
                            continue

                        state["message"] = (
                            f"{market}: ABC {direction} confirmed; buying demo contract..."
                        )

                        request_counter += 1

                        buy = await ws_request(
                            ws,
                            {
                                "buy": proposal_id,
                                "price": ask,
                            },
                            request_counter,
                        )

                        contract = buy.get(
                            "buy",
                            {},
                        )

                        contract_id = contract.get(
                            "contract_id"
                        )

                        if not contract_id:
                            continue

                        open_contracts[market] = contract_id

                        max_profit = max(0.0, payout - ask)
                        position = {
                            "symbol": market,
                            "direction": direction,
                            "entry": ask,
                            "current": ask,
                            "profit": 0.0,
                            "max_profit": max_profit,
                            "tp_adjust_percent": float(tp_adjust_percent),
                            "status": "OPEN",
                            "contract_id": contract_id,
                        }

                        state["_position_map"][market] = position

                        state["positions"] = list(
                            state["_position_map"].values()
                        )

                        state["trades"] = int(
                            state.get(
                                "trades",
                                0,
                            )
                        ) + 1

                        state["message"] = (
                            f"DEMO TRADE OPEN: "
                            f"{market} {direction} "
                            f"${ask:.2f} / 5m"
                        )

                        activity = state.setdefault(
                            "activity",
                            [],
                        )

                        activity.insert(
                            0,
                            (
                                f"OPEN: {market} "
                                f"{direction} "
                                f"${ask:.2f}"
                            ),
                        )

                        state["activity"] = activity[:20]

                    except websockets.exceptions.ConnectionClosed:
                        raise
                    except Exception as exc:
                        state["message"] = (
                            f"{market}: trade execution error - {exc}"
                        )
                        activity = state.setdefault("activity", [])
                        activity.insert(0, f"{market}: ERROR - {exc}")
                        state["activity"] = activity[:20]

                await asyncio.sleep(10)

    except asyncio.CancelledError:
        raise

    except websockets.exceptions.ConnectionClosed as exc:
        if state.get("stop_requested"):
            state["message"] = "Bot stopped."
            return

        state["message"] = (
            "Deriv connection closed. Reconnecting automatically..."
        )
        state["_reconnecting"] = True
        state["_handoff"] = True

        task = asyncio.create_task(
            demo_bot_worker(
                user_id,
                account_id,
                token,
                markets,
                risk,
                rr,
                max_daily_profit,
                max_trades,
                stake_mode,
                martingale_multiplier,
                tp_adjust_percent,
            )
        )
        BOT_TASKS[user_id] = task
        return

    except (asyncio.TimeoutError, OSError) as exc:
        if state.get("stop_requested"):
            state["message"] = "Bot stopped."
            return

        state["message"] = (
            f"Connection problem ({type(exc).__name__}). "
            "Reconnecting automatically..."
        )
        state["_reconnecting"] = True
        state["_handoff"] = True

        task = asyncio.create_task(
            demo_bot_worker(
                user_id,
                account_id,
                token,
                markets,
                risk,
                rr,
                max_daily_profit,
                max_trades,
                stake_mode,
                martingale_multiplier,
                tp_adjust_percent,
            )
        )
        BOT_TASKS[user_id] = task
        return

    except Exception as exc:
        state["message"] = (
            f"Worker stopped: "
            f"{type(exc).__name__} - {exc}"
        )

    finally:
        if not state.pop("_handoff", False):
            state["running"] = False
            state["stop_requested"] = False


# ============================================================
# LIVE DIGIT SCANNER / REFERENCE-BOT ENGINE
# ============================================================

def extract_last_digit(quote):
    """Extract the final displayed decimal digit without losing trailing zeroes."""
    try:
        from decimal import Decimal
        text = format(Decimal(str(quote)), "f")
        digits = [c for c in text if c.isdigit()]
        return int(digits[-1]) if digits else None
    except Exception:
        try:
            return int(str(quote).replace(".", "")[-1])
        except Exception:
            return None


def digit_percentages(digits):
    counts = [0] * 10
    for digit in digits:
        if 0 <= int(digit) <= 9:
            counts[int(digit)] += 1
    total = sum(counts)
    if not total:
        return counts, [0.0] * 10
    return counts, [round((n / total) * 100, 2) for n in counts]


def digit_signal(digits, trade_type="Over/Under", fixed_barrier=5, min_confidence=65):
    """Return the strongest recent Over/Under read from the live tick window."""
    if len(digits) < 20:
        return None

    recent = list(digits[-50:])
    candidates = []

    if trade_type in {"Over/Under", "Auto"}:
        barriers = [fixed_barrier] if trade_type == "Over/Under" else list(range(1, 9))
        for barrier in barriers:
            over = sum(d > barrier for d in recent) / len(recent) * 100
            under = sum(d < barrier for d in recent) / len(recent) * 100
            if over >= under:
                candidates.append((over, "DIGITOVER", barrier, over))
            else:
                candidates.append((under, "DIGITUNDER", barrier, under))

    if not candidates:
        return None

    confidence, contract_type, barrier, probability = max(candidates, key=lambda x: x[0])
    if confidence < float(min_confidence):
        return None

    return {
        "contract_type": contract_type,
        "direction": "OVER" if contract_type == "DIGITOVER" else "UNDER",
        "barrier": int(barrier),
        "confidence": round(float(confidence), 2),
        "probability": round(float(probability), 2),
        "sample": len(recent),
    }


def magnet_lock_floor(stake, max_profit, peak_profit, stage_locks, stage_triggers, reached_stage):
    """Calculate a progressive profit floor for a fixed-payout contract."""
    floor = 0.0
    stage = 0
    for i, _trigger in enumerate(stage_triggers, start=1):
        if reached_stage >= i:
            stage = i
            lock_r = float(stage_locks[i - 1])
            desired = max(0.0, float(stake) * lock_r)
            cap = max(0.0, float(peak_profit) * 0.90)
            floor = max(floor, min(desired, cap))
    return stage, round(floor, 6)


async def digit_bot_worker(
    user_id,
    account_id,
    token,
    markets,
    risk,
    max_trades=15,
    stake_mode="Flat Stake",
    martingale_multiplier=2.0,
    trade_type="Over/Under",
    barrier=5,
    duration=5,
    duration_unit="t",
    min_confidence=65,
    magnet_stages=(10, 20, 50, 70),
    magnet_locks=(0, 0.5, 1, 1.5),
    max_daily_profit=200.0,
    max_daily_loss=50.0,
):
    state = BOT_STATE[user_id]
    state.update({
        "running": True,
        "mode": "demo",
        "engine": "DIGIT_OVER_UNDER",
        "message": "Connecting live market scanner...",
        "market_data": {},
        "signals": [],
        "positions": [],
        "_position_map": {},
        "_stake_level": 0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "today_pl": 0.0,
        "balance": 0.0,
        "equity": 0.0,
        "last_trade": None,
        "trade_history": [],
        "activity": [],
        "paused": False,
    })

    req = 12000
    open_contracts = {}
    last_trade_tick = {}

    try:
        ws_url = await deriv_ws_url(account_id, token)
        async with websockets.connect(ws_url, open_timeout=15, close_timeout=5, ping_interval=20) as market_ws, \
                   websockets.connect(ws_url, open_timeout=15, close_timeout=5, ping_interval=20) as trade_ws:

            active = await get_active_symbols(market_ws)
            symbols = {m: resolve_online_symbol(active, m) for m in markets}
            symbols = {m: s for m, s in symbols.items() if s}
            if not symbols:
                raise RuntimeError("None of the selected markets are available on Deriv.")

            for market, symbol in symbols.items():
                req += 1
                hist = await ws_request(market_ws, {
                    "ticks_history": symbol,
                    "count": 80,
                    "end": "latest",
                    "style": "ticks",
                }, req)
                prices = hist.get("history", {}).get("prices", [])
                seeded = [d for d in (extract_last_digit(q) for q in prices) if d is not None]
                counts, pcts = digit_percentages(seeded[-50:])
                state["market_data"][market] = {
                    "symbol": symbol,
                    "quote": prices[-1] if prices else None,
                    "last_digit": seeded[-1] if seeded else None,
                    "digits": seeded[-50:],
                    "counts": counts,
                    "percentages": pcts,
                    "signal": None,
                    "ticks": len(seeded),
                }
                req += 1
                await market_ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": req}))

            req += 1
            balance_msg = await ws_request(trade_ws, {"balance": 1}, req)
            balance_data = balance_msg.get("balance", {})
            state["balance"] = float(balance_data.get("balance", 0) or 0)
            state["equity"] = state["balance"]
            state["currency"] = balance_data.get("currency", "USD")
            state["symbols"] = symbols
            state["message"] = "LIVE SCANNER RUNNING â waiting for a valid signal."

            async def request_contract_updates(contract_id):
                nonlocal req
                req += 1
                await trade_ws.send(json.dumps({
                    "proposal_open_contract": 1,
                    "contract_id": contract_id,
                    "subscribe": 1,
                    "req_id": req,
                }))

            async def execute_signal(market, symbol, signal):
                nonlocal req
                if market in open_contracts or state.get("paused"):
                    return
                if int(state.get("trades", 0) or 0) >= min(15, max(1, int(max_trades))):
                    return
                if float(state.get("today_pl", 0) or 0) >= min(200.0, max(100.0, float(max_daily_profit))):
                    return
                if float(state.get("today_pl", 0) or 0) <= -abs(float(max_daily_loss or 0)):
                    return

                now = time.time()
                if now - float(last_trade_tick.get(market, 0) or 0) < 8:
                    return

                balance = float(state.get("balance", 0) or 0)
                base_stake = min(float(risk), max(0.35, balance * 0.02))
                if stake_mode == "Martingale":
                    level = int(state.get("_stake_level", 0) or 0)
                    stake = min(base_stake * (float(martingale_multiplier) ** level), balance * 0.10)
                else:
                    stake = base_stake
                stake = round(max(0.35, stake), 2)
                if balance <= 0 or stake > balance:
                    return

                req += 1
                proposal_req = req
                proposal_payload = {
                    "proposal": 1,
                    "amount": stake,
                    "basis": "stake",
                    "contract_type": signal["contract_type"],
                    "currency": state.get("currency", "USD"),
                    "duration": int(duration),
                    "duration_unit": duration_unit,
                    "underlying_symbol": symbol,
                    "barrier": str(signal["barrier"]),
                }
                await trade_ws.send(json.dumps({**proposal_payload, "req_id": proposal_req}))
                proposal = None
                deadline = time.time() + 10
                while time.time() < deadline:
                    raw = await asyncio.wait_for(trade_ws.recv(), timeout=3)
                    msg = json.loads(raw)
                    if msg.get("req_id") == proposal_req:
                        if msg.get("error"):
                            raise RuntimeError(msg["error"].get("message", "Proposal rejected."))
                        proposal = msg.get("proposal", {})
                        break
                if not proposal or not proposal.get("id"):
                    return

                ask = float(proposal.get("ask_price", stake) or stake)
                payout = float(proposal.get("payout", 0) or 0)
                req += 1
                buy_req = req
                await trade_ws.send(json.dumps({"buy": proposal["id"], "price": ask, "req_id": buy_req}))
                buy = None
                deadline = time.time() + 10
                while time.time() < deadline:
                    raw = await asyncio.wait_for(trade_ws.recv(), timeout=3)
                    msg = json.loads(raw)
                    if msg.get("req_id") == buy_req:
                        if msg.get("error"):
                            raise RuntimeError(msg["error"].get("message", "Buy rejected."))
                        buy = msg.get("buy", {})
                        break
                contract_id = buy.get("contract_id") if buy else None
                if not contract_id:
                    return

                max_profit = max(0.0, payout - ask)
                position = {
                    "symbol": market,
                    "underlying_symbol": symbol,
                    "direction": signal["direction"],
                    "contract_type": signal["contract_type"],
                    "barrier": signal["barrier"],
                    "confidence": signal["confidence"],
                    "entry": ask,
                    "current": ask,
                    "profit": 0.0,
                    "max_profit": max_profit,
                    "peak_profit": 0.0,
                    "magnet_stage": 0,
                    "magnet_progress": 0.0,
                    "profit_floor": 0.0,
                    "locked_profit": 0.0,
                    "magnet_active": False,
                    "status": "OPEN",
                    "contract_id": contract_id,
                    "stake": stake,
                    "opened_at": time.time(),
                }
                open_contracts[market] = contract_id
                state["_position_map"][market] = position
                state["positions"] = list(state["_position_map"].values())
                state["trades"] = int(state.get("trades", 0) or 0) + 1
                last_trade_tick[market] = now
                state["message"] = f"AUTO ENTRY: {market} {signal['direction']} {signal['barrier']} @ {signal['confidence']:.1f}%"
                activity = state.setdefault("activity", [])
                activity.insert(0, f"OPEN {market} {signal['direction']} {signal['barrier']} â¢ {signal['confidence']:.1f}%")
                state["activity"] = activity[:30]
                await request_contract_updates(contract_id)

            while not state.get("stop_requested"):
                try:
                    raw = await asyncio.wait_for(market_ws.recv(), timeout=1.0)
                    msg = json.loads(raw)
                except asyncio.TimeoutError:
                    msg = None

                if msg and msg.get("msg_type") == "tick":
                    tick = msg.get("tick", {})
                    symbol = tick.get("symbol")
                    market = next((m for m, s in symbols.items() if s == symbol), None)
                    if market:
                        quote = tick.get("quote")
                        digit = extract_last_digit(quote)
                        data = state["market_data"].setdefault(market, {"symbol": symbol, "digits": []})
                        if digit is not None:
                            data.setdefault("digits", []).append(digit)
                            data["digits"] = data["digits"][-50:]
                            counts, pcts = digit_percentages(data["digits"])
                            data.update({
                                "quote": quote,
                                "last_digit": digit,
                                "counts": counts,
                                "percentages": pcts,
                                "ticks": int(data.get("ticks", 0) or 0) + 1,
                            })
                            signal = digit_signal(data["digits"], trade_type, barrier, min_confidence)
                            data["signal"] = signal
                            if signal:
                                state["signals"] = [{"market": market, **signal, "quote": quote, "last_digit": digit}]
                                await execute_signal(market, symbol, signal)

                for _ in range(8):
                    try:
                        raw = await asyncio.wait_for(trade_ws.recv(), timeout=0.02)
                    except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                        break
                    msg = json.loads(raw)
                    if msg.get("msg_type") != "proposal_open_contract":
                        continue
                    c = msg.get("proposal_open_contract", {})
                    contract_id = str(c.get("contract_id", ""))
                    market = next((m for m, cid in open_contracts.items() if str(cid) == contract_id), None)
                    if not market:
                        continue
                    position = state["_position_map"].get(market, {})
                    profit = float(c.get("profit", 0) or 0)
                    status = str(c.get("status", "open")).lower()
                    position.update({
                        "entry": float(c.get("buy_price", position.get("entry", 0)) or 0),
                        "current": float(c.get("bid_price", c.get("current_spot", position.get("current", 0))) or 0),
                        "profit": profit,
                        "status": "OPEN" if not c.get("is_sold") else status.upper(),
                        "current_spot": c.get("current_spot"),
                        "exit_spot": c.get("exit_spot"),
                    })
                    position["peak_profit"] = max(float(position.get("peak_profit", 0) or 0), profit)
                    max_profit = float(position.get("max_profit", 0) or 0)
                    if max_profit <= 0:
                        max_profit = max(0.0, float(c.get("payout", 0) or 0) - float(position.get("entry", 0) or 0))
                        position["max_profit"] = max_profit
                    progress = (position["peak_profit"] / max_profit * 100) if max_profit else 0
                    reached = sum(progress >= float(t) for t in magnet_stages)
                    stage, floor = magnet_lock_floor(
                        position.get("stake", 0), max_profit, position["peak_profit"],
                        magnet_locks, magnet_stages, reached,
                    )
                    # Magnet protection is monotonic: once a stage is reached,
                    # its floor can never move backwards.  This is deliberately
                    # based on the contract's live profit/payout, not a fixed TP.
                    if stage > int(position.get("magnet_stage", 0) or 0):
                        position["magnet_stage"] = stage
                        position["magnet_active"] = True

                    if stage > 0:
                        position["profit_floor"] = max(
                            float(position.get("profit_floor", 0) or 0),
                            float(floor),
                        )

                    position["locked_profit"] = float(position.get("profit_floor", 0) or 0)
                    position["magnet_progress"] = round(float(progress), 2)

                    floor = float(position.get("profit_floor", 0) or 0)
                    if (
                        not c.get("is_sold")
                        and profit > 0
                        and stage > 0
                        and profit <= floor
                        and not position.get("sell_requested")
                    ):
                        position["sell_requested"] = True
                        req += 1
                        await trade_ws.send(json.dumps({"sell": contract_id, "price": 0, "req_id": req}))
                        state["message"] = f"{market}: magnet protected ${floor:.2f}; closing trade."

                    settled = bool(c.get("is_sold")) or status in {"won", "lost", "sold", "expired"}
                    if settled:
                        if status == "won" or profit > 0:
                            state["wins"] = int(state.get("wins", 0) or 0) + 1
                        elif status in {"lost", "expired"} or profit < 0:
                            state["losses"] = int(state.get("losses", 0) or 0) + 1
                        state["today_pl"] = float(state.get("today_pl", 0) or 0) + profit
                        closed = {**position, "profit": profit, "status": status.upper(), "is_open": False}
                        state["last_trade"] = closed
                        history = state.setdefault("trade_history", [])
                        history.insert(0, closed)
                        state["trade_history"] = history[:50]
                        activity = state.setdefault("activity", [])
                        activity.insert(0, f"CLOSE {market} {status.upper()} â¢ P/L ${profit:+.2f}")
                        state["activity"] = activity[:30]
                        if profit < 0 and stake_mode == "Martingale":
                            state["_stake_level"] = min(6, int(state.get("_stake_level", 0)) + 1)
                        elif profit >= 0:
                            state["_stake_level"] = 0
                        state["_position_map"].pop(market, None)
                        open_contracts.pop(market, None)

                state["positions"] = list(state["_position_map"].values())
                open_profit = sum(float(p.get("profit", 0) or 0) for p in state["positions"])
                state["equity"] = float(state.get("balance", 0) or 0) + open_profit

                # Non-blocking balance refresh every ~5 seconds.
                if time.time() - float(state.get("_last_balance_request", 0) or 0) >= 5:
                    state["_last_balance_request"] = time.time()
                    req += 1
                    await trade_ws.send(json.dumps({"balance": 1, "req_id": req}))

                if float(state.get("today_pl", 0) or 0) >= min(200.0, max(100.0, float(max_daily_profit))):
                    state["message"] = "Daily profit limit reached â scanner remains live, new entries are paused."
                elif float(state.get("today_pl", 0) or 0) <= -abs(float(max_daily_loss or 0)):
                    state["message"] = "Daily loss limit reached â scanner remains live, new entries are paused."
                elif int(state.get("trades", 0) or 0) >= min(15, max(1, int(max_trades))):
                    state["message"] = "Maximum daily trades reached â scanner remains live, new entries are paused."

                # Drain balance messages without blocking.
                for _ in range(3):
                    try:
                        raw = await asyncio.wait_for(trade_ws.recv(), timeout=0.01)
                    except asyncio.TimeoutError:
                        break
                    msg2 = json.loads(raw)
                    if msg2.get("msg_type") == "balance":
                        bd = msg2.get("balance", {})
                        state["balance"] = float(bd.get("balance", state.get("balance", 0)) or 0)
                        state["currency"] = bd.get("currency", state.get("currency", "USD"))

            state["message"] = "Bot stopped."

    except asyncio.CancelledError:
        raise
    except websockets.exceptions.ConnectionClosed:
        if not state.get("stop_requested"):
            state["message"] = "Deriv connection closed. Restart the bot to reconnect."
    except Exception as exc:
        state["message"] = f"Digit engine stopped: {type(exc).__name__} - {exc}"
    finally:
        if not state.pop("_handoff", False):
            state["running"] = False
            state["stop_requested"] = False


# ============================================================
# BOT START / STOP / STATE
# ============================================================

@app.post("/api/trading/start")
async def start_trading(request: Request):
    user = current_user(request)

    if not user:
        return JSONResponse(
            {
                "ok": False,
                "error": "Not logged in.",
            },
            status_code=401,
        )

    uid = user["id"]

    existing = BOT_TASKS.get(uid)

    if existing and not existing.done():
        return {
            "ok": True,
            "running": True,
            "paused": bool(BOT_STATE.get(uid, {}).get("paused", False)),
            "message": "Bot is already running.",
        }

    conn = db()

    connection = conn.execute(
        """
        SELECT
            account_id,
            account_type,
            access_token_encrypted
        FROM deriv_connections
        WHERE user_id=?
        """,
        (uid,),
    ).fetchone()

    settings = conn.execute(
        "SELECT * FROM settings WHERE user_id=?",
        (uid,),
    ).fetchone()

    conn.close()

    if not connection:
        return JSONResponse(
            {
                "ok": False,
                "error": "Connect a Deriv account first.",
            },
            status_code=400,
        )

    if connection["account_type"] != "demo":
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "Online bot execution is DEMO-ONLY right now. "
                    "Connect the Demo account."
                ),
            },
            status_code=403,
        )

    markets = (
        json.loads(settings["markets"])
        if settings
        else ["Volatility 25 Index"]
    )

    strategies = (
        json.loads(settings["strategies"])
        if settings
        else ["ABC Pattern"]
    )

    supported = {"ABC Pattern", "Digit Over/Under"}
    if not any(strategy in supported for strategy in strategies):
        return JSONResponse(
            {
                "ok": False,
                "error": "Select ABC Pattern or Digit Over/Under for the online worker.",
            },
            status_code=400,
        )

    if not markets:
        return JSONResponse(
            {
                "ok": False,
                "error": "Select at least one market.",
            },
            status_code=400,
        )

    BOT_STATE[uid] = {
        "running": False,
        "stop_requested": False,
        "mode": "demo",
        "message": "Starting...",
        "trades": 0,
        "balance": 0.0,
        "equity": 0.0,
        "today_pl": 0.0,
        "wins": 0,
        "losses": 0,
        "positions": [],
        "last_trade": None,
        "activity": [],
        "_position_map": {},
        "_market_restart_at": {},
        "_stake_level": 0,
        "paused": False,
        "market_data": {},
        "signals": [],
        "engine": "ABC",
    }

    try:
        token = unprotect_token(
            connection["access_token_encrypted"]
        )
    except Exception:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "The stored Deriv connection could not "
                    "be unlocked. Please reconnect your "
                    "Deriv Demo account."
                ),
            },
            status_code=400,
        )

    if "Digit Over/Under" in strategies:
        task = asyncio.create_task(
            digit_bot_worker(
                uid,
                connection["account_id"],
                token,
                markets,
                float(settings["risk_trade"]),
                int(settings["max_trades"]),
                str(settings["stake_mode"] or "Flat Stake"),
                float(settings["martingale_multiplier"]),
                str(settings["digit_trade_type"] or "Over/Under"),
                int(settings["digit_barrier"] or 5),
                int(settings["digit_duration"] or 5),
                str(settings["digit_duration_unit"] or "t"),
                float(settings["digit_min_confidence"] or 65),
                (
                    float(settings["magnet_stage1"]),
                    float(settings["magnet_stage2"]),
                    float(settings["magnet_stage3"]),
                    float(settings["magnet_stage4"]),
                ),
                (
                    float(settings["magnet_lock1"]),
                    float(settings["magnet_lock2"]),
                    float(settings["magnet_lock3"]),
                    float(settings["magnet_lock4"]),
                ),
                min(200.0, max(100.0, float(settings["max_daily_profit"]))),
                float(settings["max_daily_loss"]),
            )
        )
    else:
        task = asyncio.create_task(
            demo_bot_worker(
                uid,
                connection["account_id"],
                token,
                markets,
                float(settings["risk_trade"]),
                float(settings["reward_risk"]),
                min(200.0, max(100.0, float(settings["max_daily_profit"]))),
                int(settings["max_trades"]),
                str(settings["stake_mode"] or "Flat Stake"),
                float(settings["martingale_multiplier"]),
                float(settings["tp_adjust_percent"]),
            )
        )

    BOT_TASKS[uid] = task

    return {
        "ok": True,
        "running": True,
        "mode": "demo",
        "message": "Demo trading worker started.",
    }


@app.post("/api/trading/stop")
async def stop_trading(request: Request):
    user = current_user(request)

    if not user:
        return JSONResponse(
            {
                "ok": False,
                "error": "Not logged in.",
            },
            status_code=401,
        )

    uid = user["id"]

    state = BOT_STATE.setdefault(
        uid,
        {
            "running": False,
            "positions": [],
        },
    )

    state["stop_requested"] = True

    task = BOT_TASKS.get(uid)

    if task and not task.done():
        task.cancel()

    state["running"] = False

    state["message"] = (
        "Bot stopped. Existing demo contracts are "
        "left to Deriv to settle."
    )

    return {
        "ok": True,
        "running": False,
        "message": state["message"],
    }


@app.post("/api/trading/pause")
async def pause_trading(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not logged in."}, status_code=401)
    uid = user["id"]
    state = BOT_STATE.setdefault(uid, {"running": False, "positions": []})
    if not state.get("running"):
        return JSONResponse({"ok": False, "error": "Bot is not running."}, status_code=400)
    state["paused"] = True
    state["message"] = "Bot paused. Existing demo trades are still monitored."
    return {"ok": True, "paused": True, "message": state["message"]}


@app.post("/api/trading/resume")
async def resume_trading(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not logged in."}, status_code=401)
    uid = user["id"]
    state = BOT_STATE.setdefault(uid, {"running": False, "positions": []})
    if not state.get("running"):
        return JSONResponse({"ok": False, "error": "Bot is not running."}, status_code=400)
    state["paused"] = False
    state["message"] = "Bot resumed. Waiting for the next valid setup."
    return {"ok": True, "paused": False, "message": state["message"]}


@app.get("/api/trading/state")
async def trading_state(request: Request):
    user = current_user(request)

    if not user:
        return JSONResponse({"ok": False}, status_code=401)

    uid = user["id"]

    state = BOT_STATE.setdefault(
        uid,
        {
            "running": False,
            "message": "Bot stopped.",
            "trades": 0,
            "balance": 0.0,
            "equity": 0.0,
            "today_pl": 0.0,
            "wins": 0,
            "losses": 0,
            "positions": [],
            "last_trade": None,
            "activity": [],
            "paused": False,
        },
    )

    # When the bot is stopped, keep this member's dashboard synced
    # directly to the Deriv account they connected.
    if not state.get("running"):
        now = time.time()
        last_refresh = float(
            state.get("_account_refresh_at", 0) or 0
        )

        if now - last_refresh >= 8:
            conn = db()
            connection = conn.execute(
                """SELECT account_id, account_type, access_token_encrypted
                   FROM deriv_connections
                   WHERE user_id=?""",
                (uid,),
            ).fetchone()
            conn.close()

            if connection:
                try:
                    token = unprotect_token(
                        connection["access_token_encrypted"]
                    )

                    balance, currency = await fetch_live_balance(
                        connection["account_id"],
                        token,
                    )

                    state["balance"] = balance

                    if not state.get("positions"):
                        state["equity"] = balance

                    state["currency"] = currency
                    state["_account_refresh_at"] = now
                    state.pop("account_error", None)

                except Exception as exc:
                    state["account_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )

    return {
        "ok": True,
        "running": bool(state.get("running", False)),
        "paused": bool(state.get("paused", False)),
        "mode": state.get("mode", "demo"),
        "message": state.get("message", "Bot stopped."),
        "balance": float(state.get("balance", 0.0) or 0.0),
        "equity": float(
            state.get(
                "equity",
                state.get("balance", 0.0),
            ) or 0.0
        ),
        "today_pl": float(state.get("today_pl", 0.0) or 0.0),
        "open_trades": len(state.get("positions", [])),
        "wins": int(state.get("wins", 0) or 0),
        "losses": int(state.get("losses", 0) or 0),
        "positions": state.get("positions", []),
        "last_trade": state.get("last_trade"),
        "trade_history": state.get("trade_history", []),
        "trades": int(state.get("trades", 0) or 0),
        "activity": state.get("activity", []),
        "engine": state.get("engine", "ABC"),
        "market_data": state.get("market_data", {}),
        "signals": state.get("signals", []),
        "magnet": {
            "stage": max([int(p.get("magnet_stage", 0) or 0) for p in state.get("positions", [])] or [0]),
            "open_locked_profit": round(sum(float(p.get("locked_profit", 0) or 0) for p in state.get("positions", [])), 2),
            "open_peak_profit": round(sum(float(p.get("peak_profit", 0) or 0) for p in state.get("positions", [])), 2),
            "positions": [
                {
                    "market": p.get("symbol"),
                    "stage": int(p.get("magnet_stage", 0) or 0),
                    "progress": float(p.get("magnet_progress", 0) or 0),
                    "peak_profit": float(p.get("peak_profit", 0) or 0),
                    "locked_profit": float(p.get("locked_profit", 0) or 0),
                    "profit_floor": float(p.get("profit_floor", 0) or 0),
                }
                for p in state.get("positions", [])
            ],
        },
    }


@app.post("/api/trading/scan")
async def scan_trading(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not logged in."}, status_code=401)
    uid = user["id"]
    state = BOT_STATE.get(uid, {})
    data = state.get("market_data", {})
    signals = []
    for market, item in data.items():
        signal = item.get("signal")
        if signal:
            signals.append({"market": market, **signal, "quote": item.get("quote"), "last_digit": item.get("last_digit")})
    state["signals"] = signals[:20]
    state["message"] = f"AI scan complete â {len(signals)} live signal(s)." if signals else "AI scan complete â no signal currently meets the confidence threshold."
    return {"ok": True, "signals": signals, "message": state["message"]}


@app.get("/api/trading/test-connection")
async def test_trading_connection(
    request: Request,
):
    user = current_user(request)

    if not user:
        return JSONResponse(
            {
                "ok": False,
                "error": "Not logged in.",
            },
            status_code=401,
        )

    conn = db()

    row = conn.execute(
        """
        SELECT
            account_id,
            account_type,
            access_token_encrypted
        FROM deriv_connections
        WHERE user_id=?
        """,
        (user["id"],),
    ).fetchone()

    conn.close()

    if not row:
        return JSONResponse(
            {
                "ok": False,
                "error": "Connect a Deriv account first.",
            },
            status_code=400,
        )

    try:
        token = unprotect_token(
            row["access_token_encrypted"]
        )

        account_id = row["account_id"]

        async with httpx.AsyncClient(
            timeout=20
        ) as client:

            otp_resp = await client.post(
                (
                    "https://api.derivws.com/trading/v1/"
                    f"options/accounts/{account_id}/otp"
                ),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Deriv-App-ID": DERIV_CLIENT_ID,
                },
            )

        if otp_resp.status_code >= 400:
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "Deriv rejected the WebSocket "
                        "authentication request."
                    ),
                },
                status_code=400,
            )

        otp_data = otp_resp.json().get(
            "data",
            {},
        )

        ws_url = otp_data.get("url")

        if not ws_url:
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "Deriv did not return "
                        "a WebSocket URL."
                    ),
                },
                status_code=400,
            )

        async with websockets.connect(
            ws_url,
            open_timeout=15,
            close_timeout=5,
        ) as ws:

            await ws.send(
                json.dumps(
                    {
                        "balance": 1,
                        "req_id": 1,
                    }
                )
            )

            for _ in range(10):
                raw = await ws.recv()
                msg = json.loads(raw)

                if msg.get("msg_type") == "balance":
                    bal = msg.get(
                        "balance",
                        {},
                    )

                    return {
                        "ok": True,
                        "websocket_connected": True,
                        "account_id": account_id,
                        "account_type": row[
                            "account_type"
                        ],
                        "balance": bal.get(
                            "balance"
                        ),
                        "currency": bal.get(
                            "currency"
                        ),
                        "message": (
                            "Authenticated Deriv "
                            "WebSocket connection "
                            "is working."
                        ),
                    }

                if msg.get("error"):
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": msg["error"].get(
                                "message",
                                "Deriv WebSocket error.",
                            ),
                        },
                        status_code=400,
                    )

        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "Connected, but Deriv did not "
                    "return a balance response."
                ),
            },
            status_code=400,
        )

    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "Trading connection test failed: "
                    f"{type(exc).__name__}."
                ),
            },
            status_code=500,
        )


# ============================================================
# ACCOUNT STATUS
# ============================================================

@app.post("/deriv/disconnect")
async def deriv_disconnect(request: Request):
    user = current_user(request)

    if not user:
        return RedirectResponse("/", status_code=303)

    uid = user["id"]

    state = BOT_STATE.get(uid, {})
    state["stop_requested"] = True

    task = BOT_TASKS.get(uid)

    if task and not task.done():
        task.cancel()

    conn = db()
    conn.execute(
        "DELETE FROM deriv_connections WHERE user_id=?",
        (uid,),
    )
    conn.commit()
    conn.close()

    BOT_STATE[uid] = {
        "running": False,
        "message": "Deriv account disconnected.",
        "trades": 0,
        "balance": 0.0,
        "equity": 0.0,
        "today_pl": 0.0,
        "wins": 0,
        "losses": 0,
        "positions": [],
        "last_trade": None,
        "activity": [],
        "paused": False,
    }

    BOT_TASKS.pop(uid, None)

    return RedirectResponse(
        "/dashboard?deriv_disconnected=1",
        status_code=303,
    )


@app.get("/api/status")
async def api_status(request: Request):
    user = current_user(request)

    if not user:
        return JSONResponse(
            {"ok": False},
            status_code=401,
        )

    conn = db()

    row = conn.execute(
        """
        SELECT account_id, account_type
        FROM deriv_connections
        WHERE user_id=?
        """,
        (user["id"],),
    ).fetchone()

    conn.close()

    return {
        "ok": True,
        "connected": bool(row),
        "account_id": (
            row["account_id"]
            if row
            else None
        ),
        "account_type": (
            row["account_type"]
            if row
            else None
        ),
        "real_enabled": ALLOW_REAL_TRADING,
    }

# ============================================================
# LIVE DIGIT FEED (DEMO-ONLY, NO CONTRACT PURCHASES)
# ============================================================
DIGIT_TASKS = {}
DIGIT_STATE = {}

async def digit_feed_worker(uid, account_id, token, symbol):
    state = DIGIT_STATE.setdefault(uid, {})
    state.update({"running": True, "symbol": symbol, "digits": [0] * 10,
                  "history": [], "last_digit": None,
                  "message": "Connected to live Deriv tick feed.", "mode": "demo"})
    try:
        ws_url = await deriv_ws_url(account_id, token)
        async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": 9101}))
            while state.get("running"):
                raw = await asyncio.wait_for(ws.recv(), timeout=35)
                msg = json.loads(raw)
                tick = msg.get("tick") or {}
                quote = tick.get("quote")
                if quote is None:
                    if msg.get("error"):
                        state["message"] = msg["error"].get("message", "Deriv tick error")
                    continue
                text = f"{float(quote):.2f}".replace(".", "")
                digit = int(text[-1])
                state["last_digit"] = digit
                state["digits"][digit] += 1
                state["history"].append(digit)
                state["history"] = state["history"][-100:]
                total = sum(state["digits"])
                state["percentages"] = [round((n / total) * 100, 2) for n in state["digits"]] if total else [0] * 10
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state["message"] = f"Digit feed stopped: {exc}"
    finally:
        state["running"] = False

@app.post("/api/digits/start")
async def start_digits(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not logged in."}, status_code=401)
    uid = user["id"]
    existing = DIGIT_TASKS.get(uid)
    if existing and not existing.done():
        return {"ok": True, "running": True, "message": "Digit feed already running."}
    conn = db()
    connection = conn.execute("SELECT account_id, account_type, access_token_encrypted FROM deriv_connections WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    if not connection or connection["account_type"] != "demo":
        return JSONResponse({"ok": False, "error": "Connect a Deriv Demo account first."}, status_code=403)
    try:
        token = unprotect_token(connection["access_token_encrypted"])
    except Exception:
        return JSONResponse({"ok": False, "error": "Reconnect your Deriv Demo account."}, status_code=400)
    symbol = "R_25"
    DIGIT_TASKS[uid] = asyncio.create_task(digit_feed_worker(uid, connection["account_id"], token, symbol))
    return {"ok": True, "running": True, "mode": "demo", "symbol": symbol, "message": "Live digit feed started. No contracts are purchased."}

@app.post("/api/digits/stop")
async def stop_digits(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not logged in."}, status_code=401)
    uid = user["id"]
    state = DIGIT_STATE.setdefault(uid, {})
    state["running"] = False
    task = DIGIT_TASKS.get(uid)
    if task and not task.done():
        task.cancel()
    state["message"] = "Digit feed stopped."
    return {"ok": True, "running": False, "message": state["message"]}

@app.get("/api/digits/state")
async def digits_state(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    state = DIGIT_STATE.get(user["id"], {})
    return {"ok": True, "running": bool(state.get("running")), "mode": "demo",
            "symbol": state.get("symbol", "R_25"), "last_digit": state.get("last_digit"),
            "digits": state.get("digits", [0] * 10),
            "percentages": state.get("percentages", [0] * 10),
            "history": state.get("history", []),
            "message": state.get("message", "Digit feed is stopped."),
            "contracts_enabled": False}
