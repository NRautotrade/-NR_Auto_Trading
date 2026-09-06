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
DERIV_REDIRECT_URI = os.getenv("DERIV_REDIRECT_URI", "http://localhost:8000/deriv/callback")
ALLOW_REAL_TRADING = os.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"

app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# In-memory demo bot workers. Each logged-in user gets an isolated task.
BOT_TASKS = {}
BOT_STATE = {}

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
        "allow_real": ALLOW_REAL_TRADING
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
    if mode == "real" and not ALLOW_REAL_TRADING:
        return templates.TemplateResponse("dashboard.html", {"request": request, "title": APP_NAME, "user": user,
            "settings": None, "connection": None, "allow_real": ALLOW_REAL_TRADING,
            "error": "Real trading is locked until the server is explicitly enabled for real mode."}, status_code=403)

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
    if not wanted:
        label = "real" if mode == "real" else "demo"
        return templates.TemplateResponse(
            "result.html",
            {"request": request, "title": APP_NAME,
             "message": f"No {label} Deriv trading account was returned for this authorization."},
            status_code=400
        )
    account = wanted[0]
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


async def deriv_ws_url(account_id: str, token: str):
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"https://api.derivws.com/trading/v1/options/accounts/{account_id}/otp",
            headers={"Authorization": f"Bearer {token}", "Deriv-App-ID": DERIV_CLIENT_ID},
        )
    if resp.status_code >= 400:
        raise RuntimeError("Deriv rejected WebSocket authentication.")
    url = resp.json().get("data", {}).get("url")
    if not url:
        raise RuntimeError("Deriv did not return a WebSocket URL.")
    return url

async def ws_request(ws, payload, req_id, timeout=15):
    payload = dict(payload)
    payload["req_id"] = req_id
    await ws.send(json.dumps(payload))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if msg.get("req_id") == req_id:
            if msg.get("error"):
                err = msg["error"].get("message", "Deriv API error.")
                raise RuntimeError(err)
            return msg

async def get_active_symbols(ws):
    msg = await ws_request(ws, {"active_symbols": "brief"}, 100)
    return msg.get("active_symbols", [])

def resolve_online_symbol(active_symbols, wanted):
    # Deriv's current API renamed symbol/display fields and some synthetic
    # indices may include variants such as "Volatility 25 (1s) Index".
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
    names = aliases.get(target, [target])

    def norm(value):
        value = str(value or "").lower()
        value = re.sub(r"\([^)]*\)", " ", value)
        value = re.sub(r"[^a-z0-9]+", " ", value)
        return " ".join(value.split())

    wanted_names = [norm(n) for n in names]
    for item in active_symbols:
        api_symbol = item.get("underlying_symbol") or item.get("symbol")
        if not api_symbol:
            continue
        fields = (
            item.get("underlying_symbol_name"),
            item.get("underlying_symbol"),
            item.get("display_name"),
            item.get("name"),
            item.get("symbol"),
        )
        normalized = [norm(v) for v in fields if v]
        if any(w and any(w in value for value in normalized) for w in wanted_names):
            return api_symbol
    return None

def abc_signal(candles, swing_len=2):
    if len(candles) < 20:
        return None
    hi = [float(c[2]) for c in candles]
    lo = [float(c[3]) for c in candles]
    highs, lows = [], []
    for i in range(swing_len, len(candles) - swing_len):
        if all(hi[i] > hi[i-j] and hi[i] > hi[i+j] for j in range(1, swing_len+1)):
            highs.append((i, hi[i]))
        if all(lo[i] < lo[i-j] and lo[i] < lo[i+j] for j in range(1, swing_len+1)):
            lows.append((i, lo[i]))
    if len(highs) >= 2 and len(lows) >= 1:
        ai, A = highs[-2]; ci, C = highs[-1]
        mids = [x for x in lows if ai < x[0] < ci]
        if mids and C < A:
            bi, B = mids[-1]
            return ("PUT", ai, bi, ci, A, B, C)
    if len(lows) >= 2 and len(highs) >= 1:
        ai, A = lows[-2]; ci, C = lows[-1]
        mids = [x for x in highs if ai < x[0] < ci]
        if mids and C > A:
            bi, B = mids[-1]
            return ("CALL", ai, bi, ci, A, B, C)
    return None

async def fetch_m5_candles(ws, symbol):
    msg = await ws_request(ws, {
        "ticks_history": symbol,
        "end": "latest",
        "count": 100,
        "style": "candles",
        "granularity": 300,
    }, 200 + hash(symbol) % 1000)
    return msg.get("candles", [])

async def demo_bot_worker(user_id, account_id, token, markets, risk, rr):
    state = BOT_STATE[user_id]
    state.update({"running": True, "mode": "demo", "message": "Starting demo trading workerâ¦", "trades": 0})
    try:
        ws_url = await deriv_ws_url(account_id, token)
        async with websockets.connect(ws_url, open_timeout=15, close_timeout=5, ping_interval=20) as ws:
            await ws_request(ws, {"balance": 1}, 10)
            active = await get_active_symbols(ws)
            symbols = {m: resolve_online_symbol(active, m) for m in markets}
            symbols = {m: s for m, s in symbols.items() if s}
            if not symbols:
                raise RuntimeError("None of the selected markets are currently available on Deriv API.")
            state["symbols"] = symbols
            state["message"] = "Demo worker is running. Waiting for ABC setupsâ¦"
            last_setup = {}
            open_contracts = {}
            while not state.get("stop_requested"):
                # Refresh every open contract so the dashboard shows live P/L.
                for market, contract_id in list(open_contracts.items()):
                    try:
                        msg = await ws_request(ws, {
                            "proposal_open_contract": 1,
                            "contract_id": contract_id,
                            "subscribe": 1,
                        }, 6000 + len(open_contracts))
                        c = msg.get("proposal_open_contract", {})
                        lt = state.get("last_trade") or {}
                        lt.update({
                            "market": market,
                            "contract_id": contract_id,
                            "status": c.get("status", "open"),
                            "is_open": not bool(c.get("is_sold")),
                            "entry_price": c.get("buy_price", lt.get("stake", 0)),
                            "current_price": c.get("bid_price", c.get("current_spot", lt.get("stake", 0))),
                            "entry_spot": c.get("entry_spot"),
                            "current_spot": c.get("current_spot"),
                            "profit": float(c.get("profit", 0) or 0),
                            "payout": c.get("payout", lt.get("payout")),
                        })
                        state["last_trade"] = lt
                        if c.get("is_sold") or c.get("status") in {"won", "lost", "sold", "expired"}:
                            open_contracts.pop(market, None)
                            state["message"] = f"{market}: contract {c.get('status', 'closed').upper()} â P/L ${float(c.get('profit', 0) or 0):+.2f}"
                            continue
                    except Exception:
                        pass

                for market, symbol in symbols.items():
                    if market in open_contracts or state.get("stop_requested"):
                        continue
                    try:
                        candles = await fetch_m5_candles(ws, symbol)
                        signal = abc_signal(candles)
                        if not signal:
                            continue
                        direction, ai, bi, ci, A, B, C = signal
                        key = (direction, ai, bi, ci)
                        if last_setup.get(market) == key:
                            continue
                        last_setup[market] = key

                        # Demo-only stake cap: never risk more than 2% of balance per contract.
                        bal_msg = await ws_request(ws, {"balance": 1}, 300)
                        balance = float(bal_msg.get("balance", {}).get("balance", 0) or 0)
                        stake = min(float(risk), max(0.35, balance * 0.02))
                        if balance <= 0 or stake > balance:
                            continue

                        proposal = await ws_request(ws, {
                            "proposal": 1,
                            "amount": round(stake, 2),
                            "basis": "stake",
                            "contract_type": direction,
                            "currency": bal_msg.get("balance", {}).get("currency", "USD"),
                            "duration": 5,
                            "duration_unit": "m",
                            "underlying_symbol": symbol,
                        }, 400 + hash((market, key)) % 1000)
                        prop = proposal.get("proposal", {})
                        proposal_id = prop.get("id")
                        ask = float(prop.get("ask_price", stake) or stake)
                        payout = float(prop.get("payout", 0) or 0)
                        profit = payout - ask
                        if not proposal_id or profit < stake * float(rr):
                            state["message"] = f"{market}: ABC {direction} found; payout below selected RR, skipped."
                            continue

                        buy = await ws_request(ws, {"buy": proposal_id, "price": ask}, 500 + hash((market, key)) % 1000)
                        contract = buy.get("buy", {})
                        contract_id = contract.get("contract_id")
                        if not contract_id:
                            continue
                        open_contracts[market] = contract_id
                        state["trades"] = int(state.get("trades", 0)) + 1
                        state["message"] = f"DEMO TRADE OPEN: {market} {direction} ${ask:.2f} / 5m"
                        state["last_trade"] = {
                            "market": market,
                            "direction": direction,
                            "stake": ask,
                            "payout": payout,
                            "contract_id": contract_id,
                            "status": "open",
                            "is_open": True,
                            "entry_price": ask,
                            "current_price": ask,
                            "profit": 0.0,
                        }
                    except Exception as exc:
                        state["message"] = f"{market}: {type(exc).__name__} â waiting."
                await asyncio.sleep(10)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state["message"] = f"Worker stopped: {type(exc).__name__} â {exc}"
    finally:
        state["running"] = False
        state["stop_requested"] = False

@app.post("/api/trading/start")
async def start_trading(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not logged in."}, status_code=401)
    uid = user["id"]
    existing = BOT_TASKS.get(uid)
    if existing and not existing.done():
        return {"ok": True, "running": True, "message": "Bot is already running."}
    conn = db()
    connection = conn.execute("SELECT account_id, account_type, access_token_encrypted FROM deriv_connections WHERE user_id=?", (uid,)).fetchone()
    settings = conn.execute("SELECT * FROM settings WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    if not connection:
        return JSONResponse({"ok": False, "error": "Connect a Deriv account first."}, status_code=400)
    if connection["account_type"] != "demo":
        return JSONResponse({"ok": False, "error": "Online bot execution is DEMO-ONLY right now. Connect the Demo account."}, status_code=403)
    markets = json.loads(settings["markets"]) if settings else ["Volatility 25 Index"]
    strategies = json.loads(settings["strategies"]) if settings else ["ABC Pattern"]
    if "ABC Pattern" not in strategies:
        return JSONResponse({"ok": False, "error": "Select ABC Pattern for the online worker."}, status_code=400)
    if not markets:
        return JSONResponse({"ok": False, "error": "Select at least one market."}, status_code=400)
    BOT_STATE[uid] = {"running": False, "stop_requested": False, "mode": "demo", "message": "Startingâ¦", "trades": 0}
    token = unprotect_token(connection["access_token_encrypted"])
    task = asyncio.create_task(demo_bot_worker(uid, connection["account_id"], token, markets, float(settings["risk_trade"]), float(settings["reward_risk"])))
    BOT_TASKS[uid] = task
    return {"ok": True, "running": True, "mode": "demo", "message": "Demo trading worker started."}

@app.post("/api/trading/stop")
async def stop_trading(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not logged in."}, status_code=401)
    uid = user["id"]
    state = BOT_STATE.setdefault(uid, {})
    state["stop_requested"] = True
    task = BOT_TASKS.get(uid)
    if task and not task.done():
        task.cancel()
    state["running"] = False
    state["message"] = "Bot stopped. Existing demo contracts are left to Deriv to settle."
    return {"ok": True, "running": False, "message": state["message"]}

@app.get("/api/trading/state")
async def trading_state(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    uid = user["id"]
    state = BOT_STATE.get(uid, {"running": False, "message": "Bot stopped.", "trades": 0})
    return {"ok": True, **state}

@app.get("/api/trading/test-connection")
async def test_trading_connection(request: Request):
    """Open the authenticated Deriv WebSocket and read balance only.
    This endpoint NEVER sends proposal/buy/sell/contract_update commands.
    """
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not logged in."}, status_code=401)

    conn = db()
    row = conn.execute(
        "SELECT account_id, account_type, access_token_encrypted FROM deriv_connections WHERE user_id=?",
        (user["id"],),
    ).fetchone()
    conn.close()
    if not row:
        return JSONResponse({"ok": False, "error": "Connect a Deriv account first."}, status_code=400)

    try:
        token = unprotect_token(row["access_token_encrypted"])
        account_id = row["account_id"]
        async with httpx.AsyncClient(timeout=20) as client:
            otp_resp = await client.post(
                f"https://api.derivws.com/trading/v1/options/accounts/{account_id}/otp",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Deriv-App-ID": DERIV_CLIENT_ID,
                },
            )
        if otp_resp.status_code >= 400:
            return JSONResponse({"ok": False, "error": "Deriv rejected the WebSocket authentication request."}, status_code=400)

        otp_data = otp_resp.json().get("data", {})
        ws_url = otp_data.get("url")
        if not ws_url:
            return JSONResponse({"ok": False, "error": "Deriv did not return a WebSocket URL."}, status_code=400)

        async with websockets.connect(ws_url, open_timeout=15, close_timeout=5) as ws:
            await ws.send(json.dumps({"balance": 1, "req_id": 1}))
            for _ in range(10):
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("msg_type") == "balance":
                    bal = msg.get("balance", {})
                    return {
                        "ok": True,
                        "websocket_connected": True,
                        "account_id": account_id,
                        "account_type": row["account_type"],
                        "balance": bal.get("balance"),
                        "currency": bal.get("currency"),
                        "message": "Authenticated Deriv WebSocket connection is working."
                    }
                if msg.get("error"):
                    return JSONResponse({"ok": False, "error": msg["error"].get("message", "Deriv WebSocket error.")}, status_code=400)

        return JSONResponse({"ok": False, "error": "Connected, but Deriv did not return a balance response."}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Trading connection test failed: {type(exc).__name__}."}, status_code=500)


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
