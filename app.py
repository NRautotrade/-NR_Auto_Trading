
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

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=False,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
            access_token_encrypted TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

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

    conn.execute(
        """
        INSERT INTO settings
        (
            user_id,
            markets,
            strategies,
            risk_trade,
            reward_risk,
            daily_target,
            max_daily_profit,
            max_daily_loss,
            protect_tp,
            lock_profit_r,
            max_trades,
            stake_mode,
            martingale_multiplier,
            tp_adjust_percent
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(user_id) DO UPDATE SET
            markets=excluded.markets,
            strategies=excluded.strategies,
            risk_trade=excluded.risk_trade,
            reward_risk=excluded.reward_risk,
            daily_target=excluded.daily_target,
            max_daily_profit=excluded.max_daily_profit,
            max_daily_loss=excluded.max_daily_loss,
            protect_tp=excluded.protect_tp,
            lock_profit_r=excluded.lock_profit_r,
            max_trades=excluded.max_trades,
            stake_mode=excluded.stake_mode,
            martingale_multiplier=excluded.martingale_multiplier,
            tp_adjust_percent=excluded.tp_adjust_percent
        """,
        (
            user_id,
            json.dumps(form.getlist("markets")),
            json.dumps(form.getlist("strategies")),
            float(form.get("risk_trade", 50)),
            float(form.get("reward_risk", 2)),
            float(form.get("daily_target", 200)),
            min(200.0, max(100.0, float(form.get("max_daily_profit", 200)))),
            float(form.get("max_daily_loss", 50)),
            # Keep the user's requested 50% protection setting.
            float(form.get("protect_tp", 50)),
            float(form.get("lock_profit_r", 1)),
            min(15.0, max(1.0, float(form.get("max_trades", 15)))),
            ("Martingale" if "Martingale" in form.getlist("strategies") else "Flat Stake"),
            min(5.0, max(1.0, float(form.get("martingale_multiplier", 2)))),
            min(99.0, max(50.0, float(form.get("tp_adjust_percent", 90)))),
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
        SELECT account_id, account_type, updated_at
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

@app.get("/deriv/connect")
async def deriv_connect(
    request: Request,
    mode: str = "demo",
):
    user = current_user(request)

    if not user:
        return RedirectResponse("/", status_code=303)

    if not DERIV_CLIENT_ID:
        return RedirectResponse(
            "/dashboard?oauth_error=client",
            status_code=303,
        )

    if mode not in {"demo", "real"}:
        mode = "demo"

    if mode == "real" and not ALLOW_REAL_TRADING:
        return RedirectResponse(
            "/dashboard?oauth_error=real_locked",
            status_code=303,
        )

    verifier = secrets.token_urlsafe(64)

    challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(
                verifier.encode()
            ).digest()
        )
        .rstrip(b"=")
        .decode()
    )

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
        "https://auth.deriv.com/oauth2/auth?"
        + urlencode(params),
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
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "title": APP_NAME,
                "message": (
                    "Deriv authorization was not completed: "
                    f"{error}"
                ),
            },
        )

    saved_state = request.session.pop(
        "oauth_state",
        None,
    )

    if (
        not code
        or not state
        or state != saved_state
    ):
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "title": APP_NAME,
                "message": (
                    "OAuth security check failed. "
                    "Please start again."
                ),
            },
            status_code=400,
        )

    verifier = request.session.pop(
        "oauth_verifier",
        None,
    )

    if not verifier:
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "title": APP_NAME,
                "message": (
                    "OAuth session expired. "
                    "Please start again."
                ),
            },
            status_code=400,
        )

    async with httpx.AsyncClient(timeout=20) as client:
        token_resp = await client.post(
            "https://auth.deriv.com/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": DERIV_CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": DERIV_REDIRECT_URI,
            },
        )

    if token_resp.status_code >= 400:
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "title": APP_NAME,
                "message": (
                    "Deriv token exchange failed. "
                    "Check the registered redirect URI and App ID."
                ),
            },
            status_code=400,
        )

    token = token_resp.json().get(
        "access_token"
    )

    if not token:
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "title": APP_NAME,
                "message": (
                    "Deriv did not return an access token."
                ),
            },
            status_code=400,
        )

    async with httpx.AsyncClient(timeout=20) as client:
        acct_resp = await client.get(
            "https://api.derivws.com/trading/v1/options/accounts",
            headers={
                "Authorization": f"Bearer {token}",
                "Deriv-App-ID": DERIV_CLIENT_ID,
            },
        )

    if acct_resp.status_code >= 400:
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "title": APP_NAME,
                "message": (
                    "Could not retrieve the Deriv accounts "
                    "for this authorization."
                ),
            },
            status_code=400,
        )

    data = acct_resp.json().get(
        "data",
        [],
    )

    mode = request.session.pop(
        "oauth_mode",
        "demo",
    )

    wanted = [
        account
        for account in data
        if str(
            account.get(
                "account_type",
                "",
            )
        ).lower() == mode
    ]

    if not wanted:
        label = (
            "real"
            if mode == "real"
            else "demo"
        )

        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "title": APP_NAME,
                "message": (
                    f"No {label} Deriv trading account "
                    "was returned for this authorization."
                ),
            },
            status_code=400,
        )

    account = wanted[0]

    account_id = (
        account.get("id")
        or account.get("account_id")
    )

    if not account_id:
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "title": APP_NAME,
                "message": (
                    "Deriv returned an account without "
                    "a usable account ID."
                ),
            },
            status_code=400,
        )

    encrypted = protect_token(token)

    conn = db()

    conn.execute(
        """
        INSERT INTO deriv_connections
        (
            user_id,
            account_id,
            account_type,
            access_token_encrypted
        )
        VALUES (?, ?, ?, ?)

        ON CONFLICT(user_id) DO UPDATE SET
            account_id=excluded.account_id,
            account_type=excluded.account_type,
            access_token_encrypted=excluded.access_token_encrypted,
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            user["id"],
            account_id,
            mode,
            encrypted,
        ),
    )

    conn.commit()
    conn.close()

    return RedirectResponse(
        "/dashboard?connected=1",
        status_code=303,
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

    if "ABC Pattern" not in strategies:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "Select ABC Pattern for "
                    "the online worker."
                ),
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
        "trades": int(state.get("trades", 0) or 0),
        "activity": state.get("activity", []),
    }


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
