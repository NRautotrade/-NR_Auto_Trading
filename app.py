import os
import secrets
import sqlite3
import hashlib
import base64
import json
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

APP_NAME = "NR AUTO TRADING"
DB_PATH = os.getenv("NR_DB_PATH", "nr_users.db")
SECRET_KEY = os.getenv("NR_SESSION_SECRET", "CHANGE-ME-IN-PRODUCTION")
DERIV_CLIENT_ID = os.getenv("DERIV_CLIENT_ID", "")
DERIV_REDIRECT_URI = os.getenv("DERIV_REDIRECT_URI", "http://localhost:8000/deriv/callback")
ALLOW_REAL_TRADING = os.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"

app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS deriv_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        account_id TEXT,
        account_type TEXT,
        access_token_encrypted TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS settings (
        user_id INTEGER PRIMARY KEY,
        markets TEXT NOT NULL DEFAULT '["Volatility 25 Index"]',
        strategies TEXT NOT NULL DEFAULT '["ABC Pattern"]',
        risk_trade REAL NOT NULL DEFAULT 50,
        reward_risk REAL NOT NULL DEFAULT 2,
        daily_target REAL NOT NULL DEFAULT 200,
        max_daily_profit REAL NOT NULL DEFAULT 500,
        max_daily_loss REAL NOT NULL DEFAULT 50,
        protect_tp REAL NOT NULL DEFAULT 70,
        lock_profit_r REAL NOT NULL DEFAULT 1,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    conn.commit()
    conn.close()

def pw_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 210000)
    return base64.urlsafe_b64encode(salt + digest).decode()

def pw_check(password: str, stored: str) -> bool:
    raw = base64.urlsafe_b64decode(stored.encode())
    salt, digest = raw[:16], raw[16:]
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 210000)
    return secrets.compare_digest(digest, check)

# Temporary development encryption wrapper.
# Before public deployment, replace this with a managed secret/KMS-backed
# encryption service or a Fernet key stored only in the server environment.
def protect_token(token: str) -> str:
    key = os.getenv("NR_TOKEN_SECRET", "")
    if not key:
        raise RuntimeError("NR_TOKEN_SECRET is not configured.")
    # Lightweight authenticated envelope using HMAC-derived XOR is NOT production crypto.
    # It deliberately refuses to run unless a secret is configured.
    import hmac
    stream = b""
    counter = 0
    while len(stream) < len(token.encode()):
        counter_bytes = counter.to_bytes(4, "big")
        stream += hmac.new(key.encode(), counter_bytes, hashlib.sha256).digest()
        counter += 1
    cipher = bytes(a ^ b for a, b in zip(token.encode(), stream))
    mac = hmac.new(key.encode(), cipher, hashlib.sha256).hexdigest()
    return json.dumps({"cipher": base64.urlsafe_b64encode(cipher).decode(), "mac": mac})

def unprotect_token(value: str) -> str:
    key = os.getenv("NR_TOKEN_SECRET", "")
    if not key:
        raise RuntimeError("NR_TOKEN_SECRET is not configured.")
    import hmac
    obj = json.loads(value)
    cipher = base64.urlsafe_b64decode(obj["cipher"].encode())
    mac = hmac.new(key.encode(), cipher, hashlib.sha256).hexdigest()
    if not secrets.compare_digest(mac, obj["mac"]):
        raise RuntimeError("Stored Deriv token failed integrity check.")
    stream = b""
    counter = 0
    while len(stream) < len(cipher):
        stream += hmac.new(key.encode(), counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(cipher, stream)).decode()

def current_user(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        return None
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return user

def save_settings(user_id, form):
    conn = db()
    conn.execute("""INSERT INTO settings
        (user_id, markets, strategies, risk_trade, reward_risk, daily_target,
         max_daily_profit, max_daily_loss, protect_tp, lock_profit_r)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
        markets=excluded.markets, strategies=excluded.strategies,
        risk_trade=excluded.risk_trade, reward_risk=excluded.reward_risk,
        daily_target=excluded.daily_target, max_daily_profit=excluded.max_daily_profit,
        max_daily_loss=excluded.max_daily_loss, protect_tp=excluded.protect_tp,
        lock_profit_r=excluded.lock_profit_r""",
        (user_id, json.dumps(form.getlist("markets")),
         json.dumps(form.getlist("strategies")), float(form.get("risk_trade", 50)),
         float(form.get("reward_risk", 2)), float(form.get("daily_target", 200)),
         float(form.get("max_daily_profit", 500)), float(form.get("max_daily_loss", 50)),
         float(form.get("protect_tp", 70)), float(form.get("lock_profit_r", 1))))
    conn.commit()
    conn.close()

@app.on_event("startup")
def startup():
    init_db()

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = current_user(request)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "title": APP_NAME})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    conn.close()
    if not user or not pw_check(password, user["password_hash"]):
        return templates.TemplateResponse("login.html", {"request": request, "title": APP_NAME, "error": "Invalid username or password."}, status_code=401)
    request.session["user_id"] = user["id"]
    return RedirectResponse("/dashboard", status_code=303)

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "title": APP_NAME})

@app.post("/register")
async def register(request: Request, username: str = Form(...), password: str = Form(...), confirm: str = Form(...)):
    username = username.strip()
    if len(username) < 3 or len(password) < 8:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Username must be 3+ characters and password 8+ characters."}, status_code=400)
    if password != confirm:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Passwords do not match."}, status_code=400)
    conn = db()
    try:
        cur = conn.execute("INSERT INTO users(username,password_hash) VALUES (?,?)", (username, pw_hash(password)))
        uid = cur.lastrowid
        conn.execute("INSERT INTO settings(user_id) VALUES (?)", (uid,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return templates.TemplateResponse("register.html", {"request": request, "error": "That username already exists."}, status_code=400)
    conn.close()
    request.session["user_id"] = uid
    return RedirectResponse("/dashboard", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    conn = db()
    settings = conn.execute("SELECT * FROM settings WHERE user_id=?", (user["id"],)).fetchone()
    connection = conn.execute("SELECT account_id, account_type, updated_at FROM deriv_connections WHERE user_id=?", (user["id"],)).fetchone()
    conn.close()
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "title": APP_NAME, "user": user,
        "settings": settings, "connection": connection,
        "allow_real": ALLOW_REAL_TRADING,
        "real_connection_enabled": True
    })

@app.post("/settings")
async def update_settings(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    save_settings(user["id"], form)
    return RedirectResponse("/dashboard", status_code=303)

@app.get("/deriv/connect")
async def deriv_connect(request: Request, mode: str = "demo"):
    user = current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    if not DERIV_CLIENT_ID:
        return templates.TemplateResponse("dashboard.html", {"request": request, "title": APP_NAME, "user": user,
            "settings": None, "connection": None, "allow_real": ALLOW_REAL_TRADING,
            "error": "DERIV_CLIENT_ID is not configured yet."}, status_code=500)
    if mode not in {"demo", "real"}:
        mode = "demo"
        verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
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
    return RedirectResponse("https://auth.deriv.com/oauth2/auth?" + urlencode(params), status_code=303)

@app.get("/deriv/callback")
async def deriv_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    user = current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    if error:
        return templates.TemplateResponse("result.html", {"request": request, "title": APP_NAME, "message": f"Deriv authorization was not completed: {error}"})
    if not code or not state or state != request.session.pop("oauth_state", None):
        return templates.TemplateResponse("result.html", {"request": request, "title": APP_NAME, "message": "OAuth security check failed. Please start again."}, status_code=400)
    verifier = request.session.pop("oauth_verifier", None)
    if not verifier:
        return templates.TemplateResponse("result.html", {"request": request, "title": APP_NAME, "message": "OAuth session expired. Please start again."}, status_code=400)

    async with httpx.AsyncClient(timeout=20) as client:
        token_resp = await client.post("https://auth.deriv.com/oauth2/token", data={
            "grant_type": "authorization_code",
            "client_id": DERIV_CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": DERIV_REDIRECT_URI,
        })
    if token_resp.status_code >= 400:
        return templates.TemplateResponse("result.html", {"request": request, "title": APP_NAME, "message": "Deriv token exchange failed. Check the registered redirect URI and App ID."}, status_code=400)
    token = token_resp.json().get("access_token")
    if not token:
        return templates.TemplateResponse("result.html", {"request": request, "title": APP_NAME, "message": "Deriv did not return an access token."}, status_code=400)

    async with httpx.AsyncClient(timeout=20) as client:
        acct_resp = await client.get("https://api.derivws.com/trading/v1/options/accounts",
            headers={"Authorization": f"Bearer {token}", "Deriv-App-ID": DERIV_CLIENT_ID})
    if acct_resp.status_code >= 400:
        return templates.TemplateResponse("result.html", {"request": request, "title": APP_NAME, "message": "Could not retrieve the Deriv accounts for this authorization."}, status_code=400)
    data = acct_resp.json().get("data", [])
    mode = request.session.pop("oauth_mode", "demo")
    wanted = [a for a in data if str(a.get("account_type", "")).lower() == mode]
    account = wanted[0] if wanted else (data[0] if data else None)
    if not account:
        return templates.TemplateResponse("result.html", {"request": request, "title": APP_NAME, "message": "No Deriv trading account was returned."}, status_code=400)

    encrypted = protect_token(token)
    conn = db()
    conn.execute("""INSERT INTO deriv_connections(user_id, account_id, account_type, access_token_encrypted)
                    VALUES(?,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET account_id=excluded.account_id,
                    account_type=excluded.account_type, access_token_encrypted=excluded.access_token_encrypted,
                    updated_at=CURRENT_TIMESTAMP""",
                 (user["id"], account.get("id") or account.get("account_id"), mode, encrypted))
    conn.commit()
    conn.close()
    return RedirectResponse("/dashboard?connected=1", status_code=303)

@app.get("/api/status")
async def api_status(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    conn = db()
    row = conn.execute("SELECT account_id, account_type FROM deriv_connections WHERE user_id=?", (user["id"],)).fetchone()
    conn.close()
    return {"ok": True, "connected": bool(row), "account_id": row["account_id"] if row else None,
            "account_type": row["account_type"] if row else None,
            "real_enabled": ALLOW_REAL_TRADING}
