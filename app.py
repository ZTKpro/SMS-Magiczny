"""
SMSAPI 2-Way SMS Chat Application
==================================
Uruchomienie:
  pip install flask requests
  python app.py

Następnie otwórz http://localhost:5000 w przeglądarce.

Ustaw adres callback w panelu SMSAPI:
  http://TWÓJ_IP:5000/webhook/sms
"""

import json
import time
import threading
import os
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, Response, session, redirect, url_for

import requests
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# ─── KONFIGURACJA ────────────────────────────────────────────────────────────
SMSAPI_TOKEN  = os.environ.get("SMSAPI_TOKEN",  "")   # ← token OAuth
SMSAPI_SENDER = os.environ.get("SMSAPI_SENDER", "")              # ← pole nadawcy
APP_PASSWORD  = os.environ.get("APP_PASSWORD",  "Magiczni")          # ← hasło do aplikacji
app.secret_key = os.environ.get("SECRET_KEY",   "xK9#mPqL2@nRvT5$wYjH8&cBzA3!eUo")
# ─────────────────────────────────────────────────────────────────────────────

# Przechowywanie rozmów w pamięci (można podmienic na SQLite/baze danych)
conversations = {}   # { "48500000000": [ {id, from, to, text, time, direction} ] }
sse_clients = []
sse_lock = threading.Lock()

# ─── AUTORYZACJA ─────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def push_sse(data: dict):
    """Wysyła zdarzenie SSE do wszystkich podpiętych klientów."""
    payload = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.append(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)

def add_message(phone: str, text: str, direction: str, msg_id: str = None):
    """Dodaje wiadomość do rozmowy."""
    if phone not in conversations:
        conversations[phone] = []
    msg = {
        "id": msg_id or f"local_{int(time.time()*1000)}",
        "phone": phone,
        "text": text,
        "time": datetime.now().strftime("%H:%M"),
        "timestamp": time.time(),
        "direction": direction,  # "in" | "out"
    }
    conversations[phone].append(msg)
    return msg

# ─── WEBHOOK – odbiera SMS od SMSAPI ─────────────────────────────────────────
@app.route("/webhook/sms", methods=["POST", "GET"])
def webhook_sms():
    data = request.form if request.method == "POST" else request.args
    sms_from = data.get("sms_from", "")
    sms_to   = data.get("sms_to", "")
    sms_text = data.get("sms_text", "")
    msg_id   = data.get("MsgId", "")

    if not sms_from:
        return "OK"

    # Normalizuj numer (usuń +)
    phone = sms_from.lstrip("+")

    msg = add_message(phone, sms_text, "in", msg_id)

    # Powiadom klientów SSE
    push_sse({
        "type": "new_message",
        "phone": phone,
        "message": msg,
        "unread": True,
    })

    return "OK"

# ─── WYSYŁANIE SMS ────────────────────────────────────────────────────────────
@app.route("/api/send", methods=["POST"])
@login_required
def api_send():
    body = request.json or {}
    phone = body.get("phone", "").strip()
    text  = body.get("text", "").strip()

    if not phone or not text:
        return jsonify({"ok": False, "error": "Brak numeru lub treści"}), 400

    # Wyślij przez SMSAPI
    resp = requests.post(
        "https://api.smsapi.pl/sms.do",
        headers={"Authorization": f"Bearer {SMSAPI_TOKEN}"},
        params={
            "to": phone,
            "message": text,
            "from": "2way",
            "encoding": "utf-8",
            "format": "json",
        },
        timeout=10,
    )

    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.ok and "list" in data:
        msg_id = data["list"][0].get("id", "")
        msg = add_message(phone, text, "out", msg_id)
        push_sse({"type": "new_message", "phone": phone, "message": msg, "unread": False})
        return jsonify({"ok": True, "message": msg})
    else:
        err = data.get("message", resp.text)
        return jsonify({"ok": False, "error": err}), 500

# ─── POBIERANIE ROZMÓW ────────────────────────────────────────────────────────
@app.route("/api/conversations")
@login_required
def api_conversations():
    result = []
    for phone, msgs in conversations.items():
        last = msgs[-1] if msgs else None
        result.append({
            "phone": phone,
            "last_text": last["text"] if last else "",
            "last_time": last["time"] if last else "",
            "count": len(msgs),
        })
    result.sort(key=lambda x: -conversations[x["phone"]][-1]["timestamp"] if conversations[x["phone"]] else 0)
    return jsonify(result)

@app.route("/api/messages/<phone>")
@login_required
def api_messages(phone):
    return jsonify(conversations.get(phone, []))

@app.route("/reply")
@login_required
def reply_page():
    folder = "replies"
    replies = {}

    if os.path.exists(folder):
        for file in os.listdir(folder):
            if file.endswith(".txt"):
                with open(os.path.join(folder, file), "r", encoding="utf-8") as f:
                    replies[file] = f.read()

    html = "<h2>Gotowe odpowiedzi SMS</h2>"

    for name, text in replies.items():
        html += f"""
        <div style="margin-bottom:20px">
            <h3>{name}</h3>
            <textarea style="width:500px;height:120px">{text}</textarea>
        </div>
        """

    return html

# ─── SSE – strumień zdarzeń w czasie rzeczywistym ────────────────────────────
@app.route("/api/events")
@login_required
def api_events():
    def stream():
        buf = []
        with sse_lock:
            sse_clients.append(buf)
        try:
            yield ": connected\n\n"
            while True:
                if buf:
                    payload = buf.pop(0)
                    yield payload
                else:
                    time.sleep(0.1)
        except GeneratorExit:
            with sse_lock:
                if buf in sse_clients:
                    sse_clients.remove(buf)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ─── SYMULATOR (tylko do testów) ──────────────────────────────────────────────
@app.route("/api/simulate", methods=["POST"])
@login_required
def simulate():
    body = request.json or {}
    phone = body.get("phone", "48500000000")
    text  = body.get("text", "Testowa wiadomość")
    msg = add_message(phone, text, "in", f"sim_{int(time.time())}")
    push_sse({"type": "new_message", "phone": phone, "message": msg, "unread": True})
    return jsonify({"ok": True})

# ─── LOGIN PAGE ───────────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>SMS Chat · Logowanie</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Space+Grotesk:wght@400;600;700&display=swap" rel="stylesheet"/>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0d0f14; --panel: #131620; --surface: #1a1e2e;
    --border: #252a3d; --accent: #4f6ef7; --accent2: #7c5bfc;
    --text: #e2e6f3; --muted: #6b7399; --danger: #f74f6e;
  }
  body {
    background: var(--bg); color: var(--text);
    font-family: 'Space Grotesk', sans-serif;
    height: 100vh; display: flex; align-items: center; justify-content: center;
  }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 20px; padding: 44px 40px; width: 360px;
    display: flex; flex-direction: column; gap: 24px;
    box-shadow: 0 24px 64px rgba(0,0,0,.6);
  }
  .card-logo { display: flex; align-items: center; gap: 12px; }
  .logo-icon {
    width: 44px; height: 44px; border-radius: 12px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    display: flex; align-items: center; justify-content: center; font-size: 20px;
  }
  .logo-name { font-size: 18px; font-weight: 700; }
  .logo-sub  { font-size: 11px; color: var(--muted); font-family: 'IBM Plex Mono', monospace; letter-spacing: 1px; margin-top: 2px; }
  h2 { font-size: 22px; font-weight: 700; }
  .sub { font-size: 13px; color: var(--muted); margin-top: -16px; }
  .field { display: flex; flex-direction: column; gap: 8px; }
  label { font-size: 12px; color: var(--muted); letter-spacing: .5px; text-transform: uppercase; font-family: 'IBM Plex Mono', monospace; }
  input[type=password] {
    padding: 12px 16px; border-radius: 10px;
    background: var(--surface); border: 1px solid var(--border);
    color: var(--text); font-family: 'Space Grotesk', sans-serif; font-size: 15px;
    outline: none; transition: border-color .2s; letter-spacing: 2px;
  }
  input[type=password]:focus { border-color: var(--accent); }
  .btn {
    padding: 13px; border-radius: 11px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    border: none; color: #fff; font-family: 'Space Grotesk', sans-serif;
    font-size: 15px; font-weight: 600; cursor: pointer;
    transition: opacity .15s, transform .1s;
  }
  .btn:hover { opacity: .9; }
  .btn:active { transform: scale(.98); }
  .error {
    background: rgba(247,79,110,.12); border: 1px solid rgba(247,79,110,.3);
    border-radius: 9px; padding: 10px 14px;
    color: var(--danger); font-size: 13px;
  }
</style>
</head>
<body>
<div class="card">
  <div class="card-logo">
    <div class="logo-icon">💬</div>
    <div>
      <div class="logo-name">SMS Chat</div>
      <div class="logo-sub">SMSAPI · 2WAY</div>
    </div>
  </div>
  <div>
    <h2>Zaloguj się</h2>
    <p class="sub">Podaj hasło aby uzyskać dostęp</p>
  </div>
  {{ERROR_BLOCK}}
  <form method="POST" style="display:flex; flex-direction:column; gap:16px;">
    <div class="field">
      <label>Hasło</label>
      <input type="password" name="password" placeholder="••••••••" autofocus/>
    </div>
    <button type="submit" class="btn">Wejdź →</button>
  </form>
</div>
</body>
</html>"""

def make_login_html(error=""):
    block = f'<div class="error">🔒 {error}</div>' if error else ""
    return LOGIN_HTML.replace("{{ERROR_BLOCK}}", block)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# Patch login route to use helper
@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Nieprawidłowe hasło. Spróbuj ponownie."
    return make_login_html(error)

# ─── FRONTEND ─────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>SMS Chat · SMSAPI</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet"/>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:       #0d0f14;
    --panel:    #131620;
    --surface:  #1a1e2e;
    --border:   #252a3d;
    --accent:   #4f6ef7;
    --accent2:  #7c5bfc;
    --green:    #22d3a5;
    --text:     #e2e6f3;
    --muted:    #6b7399;
    --bubble-in:  #1e2540;
    --bubble-out: #2d3a7c;
    --danger:   #f74f6e;
    --radius:   14px;
    --font-main: 'Space Grotesk', sans-serif;
    --font-mono: 'IBM Plex Mono', monospace;
  }

  html, body { height: 100%; overflow: hidden; background: var(--bg); color: var(--text); font-family: var(--font-main); }

  /* ── LAYOUT ── */
  .app { display: flex; height: 100vh; }

  /* ── SIDEBAR ── */
  .sidebar {
    width: 300px; min-width: 260px; max-width: 340px;
    background: var(--panel);
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column;
    flex-shrink: 0;
  }

  .sidebar-header {
    padding: 22px 20px 16px;
    border-bottom: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 12px;
  }

  .sidebar-logo {
    display: flex; align-items: center; gap: 10px;
  }
  .logo-icon {
    width: 34px; height: 34px; border-radius: 10px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    display: flex; align-items: center; justify-content: center;
    font-size: 16px;
  }
  .logo-text { font-size: 15px; font-weight: 700; letter-spacing: .5px; }
  .logo-sub  { font-size: 10px; color: var(--muted); font-family: var(--font-mono); letter-spacing: 1px; }

  .search-box {
    display: flex; align-items: center; gap: 8px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 9px; padding: 8px 12px;
  }
  .search-box input {
    background: none; border: none; outline: none;
    color: var(--text); font-family: var(--font-main); font-size: 13px; width: 100%;
  }
  .search-box input::placeholder { color: var(--muted); }
  .search-icon { color: var(--muted); font-size: 14px; }

  .conv-list { flex: 1; overflow-y: auto; padding: 8px 0; }
  .conv-list::-webkit-scrollbar { width: 4px; }
  .conv-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  .conv-item {
    padding: 12px 18px;
    cursor: pointer;
    border-left: 3px solid transparent;
    transition: background .15s, border-color .15s;
    position: relative;
  }
  .conv-item:hover { background: var(--surface); }
  .conv-item.active {
    background: var(--surface);
    border-left-color: var(--accent);
  }
  .conv-item.unread .conv-name { color: #fff; font-weight: 600; }

  .conv-row1 { display: flex; justify-content: space-between; align-items: center; }
  .conv-name { font-size: 13.5px; font-weight: 500; font-family: var(--font-mono); color: var(--text); }
  .conv-time { font-size: 11px; color: var(--muted); }

  .conv-preview { font-size: 12px; color: var(--muted); margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 220px; }

  .unread-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--accent); position: absolute; right: 16px; top: 50%; transform: translateY(-50%);
  }

  .conv-empty { padding: 30px 20px; text-align: center; color: var(--muted); font-size: 13px; line-height: 1.7; }

  .sidebar-footer {
    padding: 12px 16px;
    border-top: 1px solid var(--border);
  }
  .sim-btn {
    width: 100%; padding: 9px; border-radius: 9px;
    background: var(--surface); border: 1px solid var(--border);
    color: var(--muted); font-family: var(--font-main); font-size: 12px;
    cursor: pointer; transition: all .15s;
  }
  .sim-btn:hover { border-color: var(--accent); color: var(--accent); }

  /* ── MAIN CHAT ── */
  .chat-area {
    flex: 1; display: flex; flex-direction: column; min-width: 0;
    background: var(--bg);
  }

  .chat-header {
    padding: 16px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--panel);
    display: flex; align-items: center; gap: 16px;
    min-height: 64px;
  }

  .chat-avatar {
    width: 38px; height: 38px; border-radius: 50%;
    background: linear-gradient(135deg, var(--accent), var(--green));
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; font-weight: 700; flex-shrink: 0;
  }

  .chat-info { flex: 1; }
  .chat-phone { font-size: 14px; font-weight: 600; font-family: var(--font-mono); }
  .chat-status { font-size: 11px; color: var(--green); margin-top: 1px; }
  .chat-status.offline { color: var(--muted); }

  .status-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: currentColor; margin-right: 5px; }

  .webhook-badge {
    font-family: var(--font-mono); font-size: 10px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; padding: 4px 10px; color: var(--muted);
  }

  /* messages */
  .messages {
    flex: 1; overflow-y: auto; padding: 20px 24px;
    display: flex; flex-direction: column; gap: 10px;
  }
  .messages::-webkit-scrollbar { width: 4px; }
  .messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  .msg-group { display: flex; flex-direction: column; gap: 3px; }

  .msg-row { display: flex; align-items: flex-end; gap: 8px; }
  .msg-row.out { flex-direction: row-reverse; }

  .bubble {
    max-width: 62%; padding: 10px 14px;
    border-radius: 16px;
    font-size: 14px; line-height: 1.5;
    word-break: break-word;
    animation: popIn .18s ease;
  }
  @keyframes popIn {
    from { opacity: 0; transform: scale(.94) translateY(4px); }
    to   { opacity: 1; transform: scale(1)  translateY(0); }
  }

  .bubble.in  { background: var(--bubble-in);  border-bottom-left-radius:  4px; }
  .bubble.out { background: var(--bubble-out); border-bottom-right-radius: 4px; color: #dde4ff; }

  .msg-time { font-size: 10px; color: var(--muted); padding-bottom: 2px; white-space: nowrap; }

  .date-divider {
    text-align: center; font-size: 11px; color: var(--muted);
    font-family: var(--font-mono); letter-spacing: .5px;
    margin: 8px 0; position: relative;
  }
  .date-divider::before, .date-divider::after {
    content: ''; position: absolute; top: 50%;
    width: 35%; height: 1px; background: var(--border);
  }
  .date-divider::before { left: 0; }
  .date-divider::after  { right: 0; }

  /* empty state */
  .chat-empty {
    flex: 1; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 16px;
    color: var(--muted); text-align: center; padding: 40px;
  }
  .chat-empty-icon { font-size: 52px; opacity: .25; }
  .chat-empty h2 { font-size: 18px; font-weight: 600; color: var(--text); opacity: .4; }
  .chat-empty p  { font-size: 13px; line-height: 1.7; max-width: 320px; }

  /* compose */
  .compose {
    padding: 16px 20px;
    border-top: 1px solid var(--border);
    background: var(--panel);
    display: flex; gap: 10px; align-items: flex-end;
  }

  .compose textarea {
    flex: 1; background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 12px 16px;
    color: var(--text); font-family: var(--font-main); font-size: 14px;
    resize: none; outline: none; line-height: 1.5;
    max-height: 140px; min-height: 48px;
    transition: border-color .2s;
  }
  .compose textarea:focus { border-color: var(--accent); }
  .compose textarea::placeholder { color: var(--muted); }

  .send-btn {
    width: 48px; height: 48px; border-radius: 12px; flex-shrink: 0;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    border: none; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px; transition: opacity .15s, transform .1s;
    color: #fff;
  }
  .send-btn:hover { opacity: .9; }
  .send-btn:active { transform: scale(.94); }
  .send-btn:disabled { opacity: .4; cursor: default; }

  /* ── TOAST ── */
  .toast-container {
    position: fixed; top: 20px; right: 20px;
    display: flex; flex-direction: column; gap: 10px;
    z-index: 9999; pointer-events: none;
  }
  .toast {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 18px;
    min-width: 280px; max-width: 360px;
    box-shadow: 0 8px 32px rgba(0,0,0,.5);
    animation: slideIn .25s ease;
    pointer-events: all;
    display: flex; gap: 12px; align-items: flex-start;
  }
  .toast.hide { animation: slideOut .3s ease forwards; }
  @keyframes slideIn  { from { opacity:0; transform: translateX(40px); } to { opacity:1; transform: translateX(0); } }
  @keyframes slideOut { to   { opacity:0; transform: translateX(40px); } }
  .toast-icon { font-size: 20px; flex-shrink: 0; margin-top: 1px; }
  .toast-body { flex: 1; }
  .toast-title { font-size: 13px; font-weight: 600; margin-bottom: 3px; }
  .toast-text  { font-size: 12px; color: var(--muted); line-height: 1.4; }

  /* ── NOTIFICATION BANNER ── */
  .notif-bar {
    background: #1a1e2e; border-bottom: 1px solid var(--border);
    padding: 9px 20px;
    font-size: 12px; color: var(--muted);
    display: flex; align-items: center; gap: 10px;
    cursor: pointer;
  }
  .notif-bar .allow-btn {
    margin-left: auto; padding: 4px 12px;
    background: var(--accent); color: #fff;
    border: none; border-radius: 6px; font-size: 11px;
    cursor: pointer; font-family: var(--font-main);
  }
</style>
</head>
<body>

<div class="toast-container" id="toastContainer"></div>

<div class="app">

  <!-- SIDEBAR -->
  <aside class="sidebar">
    <div class="sidebar-header">
      <div class="sidebar-logo">
        <div class="logo-icon">💬</div>
        <div>
          <div class="logo-text">SMS Chat</div>
          <div class="logo-sub">SMSAPI · 2WAY</div>
        </div>
      </div>
      <div class="search-box">
        <span class="search-icon">🔍</span>
        <input type="text" id="searchInput" placeholder="Szukaj numeru..."/>
      </div>
    </div>

    <div class="conv-list" id="convList">
      <div class="conv-empty">
        Brak rozmów.<br/>Czekam na pierwsze SMS-y…
      </div>
    </div>

    <div class="sidebar-footer">
      <button class="sim-btn" onclick="openSimulator()">⚡ Symuluj przychodzący SMS</button>
      <a href="/logout" style="display:block; margin-top:8px; text-align:center;
         font-size:12px; color:var(--muted); text-decoration:none; padding:6px;
         border-radius:8px; transition:color .15s;"
         onmouseover="this.style.color='var(--danger)'"
         onmouseout="this.style.color='var(--muted)'">
        🔒 Wyloguj
      </a>
    </div>
  </aside>

  <!-- CHAT -->
  <main class="chat-area">
    <div id="notifBar" class="notif-bar" style="display:none" onclick="requestNotifPermission()">
      🔔 Włącz powiadomienia push aby być powiadamianym o nowych SMS-ach
      <button class="allow-btn">Zezwól</button>
    </div>

    <!-- empty state -->
    <div class="chat-empty" id="emptyState">
      <div class="chat-empty-icon">📱</div>
      <h2>Wybierz rozmowę</h2>
      <p>Kliknij kontakt po lewej lub poczekaj na nowy SMS.<br/>
         Webhook: <code style="font-family:var(--font-mono);color:var(--accent)">/webhook/sms</code></p>
    </div>

    <!-- active chat -->
    <div id="chatView" style="display:none; flex-direction:column; flex:1; min-height:0;">
      <div class="chat-header">
        <div class="chat-avatar" id="chatAvatar">?</div>
        <div class="chat-info">
          <div class="chat-phone" id="chatPhone">—</div>
          <div class="chat-status" id="chatStatus">
            <span class="status-dot"></span>Aktywny
          </div>
        </div>
        <div class="webhook-badge" id="webhookBadge">webhook: /webhook/sms</div>
      </div>

      <div class="messages" id="messages"></div>

      <div class="compose">
        <textarea id="composeInput" placeholder="Napisz odpowiedź…" rows="1"
          onkeydown="handleKey(event)" oninput="autoResize(this)"></textarea>
        <button class="send-btn" id="sendBtn" onclick="sendMessage()" title="Wyślij (Enter)">➤</button>
      </div>
    </div>
  </main>

</div>

<!-- SIMULATOR MODAL -->
<div id="simModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,.7);
  z-index:1000; align-items:center; justify-content:center;">
  <div style="background:var(--panel); border:1px solid var(--border); border-radius:16px;
    padding:28px; width:360px; display:flex; flex-direction:column; gap:14px;">
    <div style="font-size:16px; font-weight:700;">⚡ Symulator SMS</div>
    <input id="simPhone" type="text" placeholder="Numer nadawcy (np. 48500000000)"
      value="48500123456"
      style="padding:10px 14px; border-radius:10px; background:var(--surface);
             border:1px solid var(--border); color:var(--text);
             font-family:var(--font-mono); font-size:13px; outline:none;"/>
    <textarea id="simText" placeholder="Treść wiadomości"
      style="padding:10px 14px; border-radius:10px; background:var(--surface);
             border:1px solid var(--border); color:var(--text);
             font-family:var(--font-main); font-size:13px; outline:none; resize:none; height:80px;"
      >Hej, potrzebuję pomocy!</textarea>
    <div style="display:flex; gap:10px; justify-content:flex-end;">
      <button onclick="closeSimulator()"
        style="padding:9px 20px; border-radius:9px; background:var(--surface);
               border:1px solid var(--border); color:var(--muted);
               font-family:var(--font-main); cursor:pointer;">Anuluj</button>
      <button onclick="sendSimulated()"
        style="padding:9px 20px; border-radius:9px;
               background:linear-gradient(135deg,var(--accent),var(--accent2));
               border:none; color:#fff; font-family:var(--font-main);
               font-weight:600; cursor:pointer;">Wyślij symulację</button>
    </div>
  </div>
</div>

<script>
// ─── STATE ────────────────────────────────────────────────────────────────────
let activePhone = null;
let allConvs    = {};   // phone → [{...}]
let unreadSet   = new Set();
let searchQ     = "";

// ─── INIT ────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  checkNotifPermission();
  loadConversations();
  connectSSE();
  document.getElementById("searchInput").addEventListener("input", e => {
    searchQ = e.target.value.toLowerCase();
    renderSidebar();
  });
});

// ─── NOTIFICATIONS ────────────────────────────────────────────────────────────
function checkNotifPermission() {
  if (!("Notification" in window)) return;
  if (Notification.permission === "default") {
    document.getElementById("notifBar").style.display = "flex";
  }
}

function requestNotifPermission() {
  Notification.requestPermission().then(p => {
    document.getElementById("notifBar").style.display = "none";
    if (p === "granted") showToast("✅", "Powiadomienia aktywne", "Będziesz informowany o nowych SMS-ach.");
  });
}

function pushNotification(phone, text) {
  if (Notification.permission === "granted" && document.hidden) {
    new Notification(`📩 SMS od ${phone}`, {
      body: text,
      icon: "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>💬</text></svg>"
    });
  }
}

// ─── SSE ─────────────────────────────────────────────────────────────────────
function connectSSE() {
  const es = new EventSource("/api/events");
  es.onmessage = e => {
    const data = JSON.parse(e.data);
    if (data.type === "new_message") {
      const { phone, message, unread } = data;

      if (!allConvs[phone]) allConvs[phone] = [];
      allConvs[phone].push(message);

      if (unread) {
        unreadSet.add(phone);
        showToast("📩", `SMS od ${phone}`, message.text);
        pushNotification(phone, message.text);
      }

      renderSidebar();
      if (phone === activePhone) {
        appendBubble(message);
        scrollToBottom();
      }
    }
  };
  es.onerror = () => setTimeout(connectSSE, 3000);
}

// ─── LOAD CONVERSATIONS ───────────────────────────────────────────────────────
async function loadConversations() {
  const res = await fetch("/api/conversations");
  const list = await res.json();
  for (const c of list) {
    const msgs = await fetch(`/api/messages/${c.phone}`).then(r => r.json());
    allConvs[c.phone] = msgs;
  }
  renderSidebar();
}

// ─── SIDEBAR ─────────────────────────────────────────────────────────────────
function renderSidebar() {
  const phones = Object.keys(allConvs).sort((a, b) => {
    const la = allConvs[a].at(-1)?.timestamp || 0;
    const lb = allConvs[b].at(-1)?.timestamp || 0;
    return lb - la;
  });

  const filtered = phones.filter(p => p.includes(searchQ));
  const list = document.getElementById("convList");

  if (!filtered.length) {
    list.innerHTML = `<div class="conv-empty">Brak rozmów.<br/>Czekam na pierwsze SMS-y…</div>`;
    return;
  }

  list.innerHTML = filtered.map(phone => {
    const msgs = allConvs[phone] || [];
    const last = msgs.at(-1);
    const isUnread = unreadSet.has(phone);
    const isActive = phone === activePhone;
    const preview = last ? (last.direction === "out" ? "Ty: " : "") + last.text : "";
    const initials = phone.slice(-2);
    return `
      <div class="conv-item${isActive?" active":""}${isUnread?" unread":""}"
           onclick="openChat('${phone}')">
        <div class="conv-row1">
          <span class="conv-name">${phone}</span>
          <span class="conv-time">${last?.time||""}</span>
        </div>
        <div class="conv-preview">${escHtml(preview)}</div>
        ${isUnread ? '<div class="unread-dot"></div>' : ""}
      </div>`;
  }).join("");
}

// ─── CHAT VIEW ────────────────────────────────────────────────────────────────
function openChat(phone) {
  activePhone = phone;
  unreadSet.delete(phone);

  document.getElementById("emptyState").style.display = "none";
  const cv = document.getElementById("chatView");
  cv.style.display = "flex";

  document.getElementById("chatPhone").textContent = phone;
  document.getElementById("chatAvatar").textContent = phone.slice(-2);

  const msgs = document.getElementById("messages");
  msgs.innerHTML = "";

  const list = allConvs[phone] || [];
  list.forEach(appendBubble);
  scrollToBottom();
  renderSidebar();
  document.getElementById("composeInput").focus();
}

function appendBubble(msg) {
  const msgs = document.getElementById("messages");
  const row = document.createElement("div");
  row.className = "msg-row " + msg.direction;
  row.innerHTML = `
    <div class="bubble ${msg.direction}">${escHtml(msg.text)}</div>
    <div class="msg-time">${msg.time}</div>`;
  msgs.appendChild(row);
}

function scrollToBottom() {
  const m = document.getElementById("messages");
  m.scrollTop = m.scrollHeight;
}

// ─── SEND ─────────────────────────────────────────────────────────────────────
async function sendMessage() {
  if (!activePhone) return;
  const ta = document.getElementById("composeInput");
  const text = ta.value.trim();
  if (!text) return;

  ta.value = "";
  ta.style.height = "";
  document.getElementById("sendBtn").disabled = true;

  try {
    const res = await fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone: activePhone, text })
    });
    const data = await res.json();
    if (!data.ok) showToast("❌", "Błąd wysyłki", data.error || "Nieznany błąd");
  } catch(e) {
    showToast("❌", "Błąd sieci", e.message);
  }

  document.getElementById("sendBtn").disabled = false;
  document.getElementById("composeInput").focus();
}

function handleKey(e) {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

function autoResize(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 140) + "px";
}

// ─── SIMULATOR ────────────────────────────────────────────────────────────────
function openSimulator()  { document.getElementById("simModal").style.display = "flex"; }
function closeSimulator() { document.getElementById("simModal").style.display = "none"; }

async function sendSimulated() {
  const phone = document.getElementById("simPhone").value.trim();
  const text  = document.getElementById("simText").value.trim();
  if (!phone || !text) return;
  await fetch("/api/simulate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phone, text })
  });
  closeSimulator();
}

// ─── TOAST ────────────────────────────────────────────────────────────────────
function showToast(icon, title, text) {
  const c = document.getElementById("toastContainer");
  const t = document.createElement("div");
  t.className = "toast";
  t.innerHTML = `
    <div class="toast-icon">${icon}</div>
    <div class="toast-body">
      <div class="toast-title">${escHtml(title)}</div>
      <div class="toast-text">${escHtml(text)}</div>
    </div>`;
  c.appendChild(t);
  setTimeout(() => {
    t.classList.add("hide");
    setTimeout(() => t.remove(), 300);
  }, 4000);
}

// ─── UTILS ────────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
</script>
</body>
</html>"""

@app.route("/")
@login_required
def index():
    return HTML

if __name__ == "__main__":
    print("=" * 56)
    print("  💬  SMS Chat · SMSAPI 2-Way")
    print("=" * 56)
    print(f"  Interfejs:  http://localhost:5000")
    print(f"  Webhook:    http://TWÓJ_IP:5000/webhook/sms")
    print(f"  Symulator:  http://localhost:5000/api/simulate")
    print("=" * 56)
    print("  ⚠  Ustaw SMSAPI_TOKEN i SMSAPI_SENDER w app.py")
    print("  ⚠  Wpisz adres webhook w panelu SMSAPI")
    print("=" * 56)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
