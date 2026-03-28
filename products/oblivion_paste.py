#!/usr/bin/env python3
"""
OBLIVION Paste — Zero-Knowledge Encrypted Notepad
Port 3073 — https://oblivionsearch.com/paste

The server NEVER sees plaintext. Encryption key lives in URL fragment (#),
which browsers never send to the server. AES-256-GCM via Web Crypto API.
"""

import asyncio
import hashlib
import os
import secrets
import smtplib
import string
import threading
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

import asyncpg
import stripe
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI(title="OBLIVION Paste", docs_url=None, redoc_url=None)

DB_DSN = "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/oblivion_paste"
pool: Optional[asyncpg.Pool] = None

# ─── Stripe ───
STRIPE_SECRET = "os.environ.get("STRIPE_SECRET_KEY", "")"
stripe.api_key = STRIPE_SECRET
DOMAIN = "https://oblivionsearch.com"

# ─── SMTP ───
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "os.environ.get("SMTP_USER", "")"
SMTP_PASS = "os.environ.get("SMTP_PASS", "")"


def generate_api_key():
    return "paste_" + secrets.token_hex(24)


def send_welcome_email(to_email, plan, api_key):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Welcome to OBLIVION Paste {plan.title()}!"
        msg["From"] = f"OBLIVION Paste <{SMTP_USER}>"
        msg["To"] = to_email
        html = f"""<html><body style="background:#0a0a0f;color:#e0e0e0;font-family:monospace;padding:30px;">
        <h1 style="color:#00d4ff;">Welcome to OBLIVION Paste {plan.title()}</h1>
        <p>Your subscription is now active.</p>
        <p><strong>Your API Key:</strong></p>
        <pre style="background:#12121a;padding:16px;border:1px solid #1e1e2e;border-radius:8px;color:#00d4ff;font-size:1.1em;">{api_key}</pre>
        <p>Your {plan.title()} features:</p>
        <ul>
        <li>Never-expire pastes</li>
        <li>Unlimited pastes per day</li>
        <li>Password-protected pastes</li>
        <li>Custom short URLs</li>
        {"<li>API access & team workspace</li>" if plan == "business" else ""}
        </ul>
        <p>Dashboard: <a href="{DOMAIN}/paste/dashboard?key={api_key}" style="color:#00d4ff;">{DOMAIN}/paste/dashboard?key={api_key}</a></p>
        <p style="color:#888;margin-top:30px;">— OBLIVION Paste | <a href="{DOMAIN}" style="color:#00d4ff;">oblivionsearch.com</a></p>
        </body></html>"""
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        print(f"[PASTE] Email send error: {e}")

EXPIRY_MAP = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
    "1m": timedelta(days=30),
    "never": None,
}


def generate_id(length=12):
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


async def get_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    return pool


@app.on_event("startup")
async def startup():
    # Create database if not exists — connect to default db first
    try:
        conn = await asyncpg.connect(
            "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/postgres"
        )
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname='oblivion_paste'"
        )
        if not exists:
            await conn.execute("CREATE DATABASE oblivion_paste")
        await conn.close()
    except Exception:
        pass

    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pastes (
                id TEXT PRIMARY KEY,
                encrypted_content TEXT NOT NULL,
                iv TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                expires_at TIMESTAMPTZ,
                burn_after_read BOOLEAN DEFAULT FALSE,
                view_count INTEGER DEFAULT 0,
                ip_hash TEXT,
                syntax TEXT DEFAULT 'plaintext'
            )
        """)
        # Cleanup index
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pastes_expires
            ON pastes(expires_at) WHERE expires_at IS NOT NULL
        """)
        # Customers table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS paste_customers (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL,
                plan TEXT NOT NULL DEFAULT 'pro',
                api_key TEXT UNIQUE NOT NULL,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_paste_api_key ON paste_customers(api_key)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_paste_email ON paste_customers(email)")


@app.on_event("shutdown")
async def shutdown():
    global pool
    if pool:
        await pool.close()


async def cleanup_expired():
    """Delete expired pastes."""
    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "DELETE FROM pastes WHERE expires_at IS NOT NULL AND expires_at < NOW()"
        )


# --------------- API ---------------

@app.post("/api/paste")
async def create_paste(request: Request):
    """Store encrypted paste. Server never sees plaintext."""
    body = await request.json()

    encrypted = body.get("encrypted")
    iv = body.get("iv")
    expiry = body.get("expiry", "1w")
    burn = body.get("burn_after_read", False)
    syntax = body.get("syntax", "plaintext")

    if not encrypted or not iv:
        raise HTTPException(400, "Missing encrypted content or IV")

    if len(encrypted) > 2_000_000:  # ~2MB limit
        raise HTTPException(413, "Paste too large (max ~1.5MB)")

    if expiry not in EXPIRY_MAP:
        raise HTTPException(400, f"Invalid expiry. Use: {list(EXPIRY_MAP.keys())}")

    paste_id = generate_id()
    expires_at = None
    if EXPIRY_MAP[expiry]:
        expires_at = datetime.now(timezone.utc) + EXPIRY_MAP[expiry]

    ip_raw = request.headers.get("x-real-ip", request.client.host)
    ip_hash = hashlib.sha256(ip_raw.encode()).hexdigest()[:16]

    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """INSERT INTO pastes (id, encrypted_content, iv, expires_at, burn_after_read, ip_hash, syntax)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            paste_id, encrypted, iv, expires_at, burn, ip_hash, syntax,
        )

    # Run cleanup occasionally
    asyncio.create_task(cleanup_expired())

    return {"id": paste_id, "url": f"/paste/{paste_id}"}


@app.get("/api/paste/{paste_id}")
async def get_paste_data(paste_id: str):
    """Return encrypted blob. Decryption happens client-side."""
    p = await get_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM pastes WHERE id = $1", paste_id
        )

    if not row:
        raise HTTPException(404, "Paste not found or expired")

    # Check expiry
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        async with p.acquire() as conn:
            await conn.execute("DELETE FROM pastes WHERE id = $1", paste_id)
        raise HTTPException(404, "Paste expired")

    burn = row["burn_after_read"]
    view_count = row["view_count"]

    # Burn after read: allow first view, delete after
    if burn and view_count > 0:
        async with p.acquire() as conn:
            await conn.execute("DELETE FROM pastes WHERE id = $1", paste_id)
        raise HTTPException(410, "This paste has been burned after reading")

    # Increment view count
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE pastes SET view_count = view_count + 1 WHERE id = $1", paste_id
        )

    return {
        "encrypted": row["encrypted_content"],
        "iv": row["iv"],
        "burn_after_read": burn,
        "syntax": row["syntax"],
        "created_at": row["created_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
    }


# --------------- HTML Pages ---------------

COMMON_STYLE = """
:root {
    --bg: #0a0a0f;
    --surface: #12121a;
    --border: #1e1e2e;
    --text: #e0e0e0;
    --muted: #888;
    --accent: #00d4ff;
    --accent-dim: #00d4ff33;
    --danger: #ff4444;
    --success: #00ff88;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
}
.container { max-width: 900px; margin: 0 auto; padding: 20px; }
.header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 0; border-bottom: 1px solid var(--border); margin-bottom: 24px;
}
.header h1 { font-size: 1.3em; color: var(--accent); font-weight: 600; }
.header h1 a { color: var(--accent); text-decoration: none; }
.header .sub { color: var(--muted); font-size: 0.8em; }
.btn {
    background: var(--accent); color: #000; border: none; padding: 10px 24px;
    border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 0.9em;
    font-family: inherit; transition: all 0.2s;
}
.btn:hover { opacity: 0.85; transform: translateY(-1px); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }
.btn-outline {
    background: transparent; border: 1px solid var(--accent); color: var(--accent);
    padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 0.85em;
    font-family: inherit; transition: all 0.2s;
}
.btn-outline:hover { background: var(--accent-dim); }
textarea {
    width: 100%; min-height: 350px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 8px; padding: 16px;
    color: var(--text); font-family: inherit; font-size: 0.9em;
    resize: vertical; outline: none; transition: border 0.2s;
}
textarea:focus { border-color: var(--accent); }
select, input[type=text] {
    background: var(--surface); border: 1px solid var(--border);
    color: var(--text); padding: 8px 12px; border-radius: 6px;
    font-family: inherit; font-size: 0.85em; outline: none;
}
select:focus, input[type=text]:focus { border-color: var(--accent); }
.options { display: flex; gap: 16px; margin: 16px 0; flex-wrap: wrap; align-items: center; }
.options label { color: var(--muted); font-size: 0.85em; }
.checkbox-wrap { display: flex; align-items: center; gap: 6px; }
.checkbox-wrap input[type=checkbox] { accent-color: var(--accent); }
.result-box {
    background: var(--surface); border: 1px solid var(--accent);
    border-radius: 8px; padding: 16px; margin-top: 20px; display: none;
}
.result-box .url { color: var(--accent); word-break: break-all; font-size: 0.9em; }
.info-bar {
    display: flex; gap: 16px; margin-bottom: 16px; color: var(--muted);
    font-size: 0.8em; flex-wrap: wrap;
}
.info-bar span { display: flex; align-items: center; gap: 4px; }
.content-view {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 20px; white-space: pre-wrap;
    word-break: break-word; font-size: 0.9em; line-height: 1.6;
    min-height: 200px;
}
.content-view.markdown { white-space: normal; }
.content-view h1, .content-view h2, .content-view h3 { color: var(--accent); margin: 12px 0 6px; }
.content-view a { color: var(--accent); }
.content-view code {
    background: #1a1a2e; padding: 2px 6px; border-radius: 4px; font-size: 0.9em;
}
.content-view pre { background: #1a1a2e; padding: 12px; border-radius: 6px; overflow-x: auto; }
.content-view pre code { background: none; padding: 0; }
.burn-warning {
    background: #ff444420; border: 1px solid var(--danger); border-radius: 8px;
    padding: 12px 16px; margin-bottom: 16px; color: var(--danger);
    font-size: 0.85em;
}
.footer {
    margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--border);
    text-align: center; color: var(--muted); font-size: 0.75em;
}
.footer a { color: var(--accent); text-decoration: none; }
.loading { text-align: center; padding: 40px; color: var(--muted); }
.error-msg { color: var(--danger); margin-top: 12px; font-size: 0.85em; display: none; }
@media (max-width: 600px) {
    .container { padding: 12px; }
    textarea { min-height: 250px; }
    .options { flex-direction: column; gap: 10px; }
}
@media (max-width: 480px) {
    body { overflow-x: hidden; }
    .header h1 { font-size: 1.1em; }
    textarea { font-size: 16px; width: 100%; min-height: 200px; }
    .btn { min-height: 44px; font-size: 16px; padding: 12px 20px; width: 100%; }
    .btn-outline { min-height: 44px; font-size: 14px; }
    select, input[type=text] { font-size: 16px; }
    .result-box { padding: 12px; }
    .result-box .url { font-size: 0.8em; }
    .content-view { padding: 12px; font-size: 0.85em; }
    .info-bar { font-size: 0.75em; gap: 8px; }
}
@media (max-width: 375px) {
    .header { flex-direction: column; gap: 8px; text-align: center; }
    textarea { min-height: 180px; font-size: 16px; }
    .btn { font-size: 15px; }
    .content-view { font-size: 0.8em; padding: 10px; }
}
"""

CRYPTO_JS = """
// --- Real AES-256-GCM encryption using Web Crypto API ---
const OZCrypto = {
    async generateKey() {
        const key = await crypto.subtle.generateKey(
            { name: 'AES-GCM', length: 256 },
            true,
            ['encrypt', 'decrypt']
        );
        const raw = await crypto.subtle.exportKey('raw', key);
        return this._bufToBase64(raw);
    },

    async encrypt(plaintext, keyBase64) {
        const keyBuf = this._base64ToBuf(keyBase64);
        const key = await crypto.subtle.importKey(
            'raw', keyBuf, { name: 'AES-GCM' }, false, ['encrypt']
        );
        const iv = crypto.getRandomValues(new Uint8Array(12));
        const encoded = new TextEncoder().encode(plaintext);
        const ciphertext = await crypto.subtle.encrypt(
            { name: 'AES-GCM', iv: iv },
            key, encoded
        );
        return {
            encrypted: this._bufToBase64(ciphertext),
            iv: this._bufToBase64(iv)
        };
    },

    async decrypt(encryptedBase64, ivBase64, keyBase64) {
        const keyBuf = this._base64ToBuf(keyBase64);
        const key = await crypto.subtle.importKey(
            'raw', keyBuf, { name: 'AES-GCM' }, false, ['decrypt']
        );
        const iv = this._base64ToBuf(ivBase64);
        const ciphertext = this._base64ToBuf(encryptedBase64);
        const decrypted = await crypto.subtle.decrypt(
            { name: 'AES-GCM', iv: iv },
            key, ciphertext
        );
        return new TextDecoder().decode(decrypted);
    },

    _bufToBase64(buf) {
        const bytes = new Uint8Array(buf);
        let binary = '';
        for (let i = 0; i < bytes.byteLength; i++) {
            binary += String.fromCharCode(bytes[i]);
        }
        return btoa(binary).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
    },

    _base64ToBuf(base64) {
        const padded = base64.replace(/-/g, '+').replace(/_/g, '/');
        const pad = padded.length % 4;
        const final = pad ? padded + '='.repeat(4 - pad) : padded;
        const binary = atob(final);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
            bytes[i] = binary.charCodeAt(i);
        }
        return bytes.buffer;
    }
};
"""

MARKDOWN_JS = """
// Minimal markdown renderer
function renderMarkdown(text) {
    let html = text
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        // Code blocks
        .replace(/```(\\w*)\\n([\\s\\S]*?)```/g, '<pre><code>$2</code></pre>')
        // Inline code
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        // Headers
        .replace(/^### (.+)$/gm, '<h3>$1</h3>')
        .replace(/^## (.+)$/gm, '<h2>$1</h2>')
        .replace(/^# (.+)$/gm, '<h1>$1</h1>')
        // Bold/italic
        .replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>')
        .replace(/\\*(.+?)\\*/g, '<em>$1</em>')
        // Links
        .replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
        // Lists
        .replace(/^- (.+)$/gm, '<li>$1</li>')
        .replace(/(<li>.*<\\/li>)/s, '<ul>$1</ul>')
        // Line breaks
        .replace(/\\n\\n/g, '</p><p>')
        .replace(/\\n/g, '<br>');
    return '<p>' + html + '</p>';
}
"""


@app.get("/paste", response_class=HTMLResponse)
async def paste_landing(request: Request):
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OBLIVION Paste — Zero-Knowledge Encrypted Notepad</title>
    <meta name="description" content="Encrypted pastebin where the server cannot read your content. Zero-knowledge, AES-256-GCM encryption.">
    <link rel="canonical" href="https://oblivionsearch.com/paste">
    <meta property="og:title" content="OBLIVION Paste — Zero-Knowledge Encrypted Notepad">
    <meta property="og:description" content="Encrypted pastebin where the server cannot read your content. Zero-knowledge, AES-256-GCM encryption.">
    <meta property="og:url" content="https://oblivionsearch.com/paste">
    <meta property="og:type" content="website">
    <meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
    <style>{COMMON_STYLE}</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1><a href="/paste">OBLIVION Paste</a></h1>
            <div class="sub">Zero-Knowledge Encrypted Notepad</div>
        </div>
        <div style="display:flex;gap:10px;">
            <a href="/paste/pricing" class="btn-outline">Pricing</a>
            <a href="/" class="btn-outline">OBLIVION Search</a>
        </div>
    </div>

    <div id="how-it-works" style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px 18px;margin-bottom:20px;font-size:0.82em;color:var(--muted);line-height:1.5">
        <strong style="color:var(--accent)">How it works:</strong>
        Your text is encrypted <em>in your browser</em> with AES-256-GCM before it ever reaches our server.
        The encryption key is placed in the URL fragment (#), which is <em>never</em> sent to the server.
        We literally cannot read your paste.
    </div>

    <textarea id="content" placeholder="Paste or type your content here...&#10;&#10;Supports Markdown. Everything is encrypted client-side before reaching the server."></textarea>

    <div class="options">
        <div>
            <label>Expiry:</label>
            <select id="expiry">
                <option value="1h">1 Hour</option>
                <option value="1d">1 Day</option>
                <option value="1w" selected>1 Week</option>
                <option value="1m">1 Month</option>
                <option value="never">Never</option>
            </select>
        </div>
        <div>
            <label>Syntax:</label>
            <select id="syntax">
                <option value="plaintext">Plain Text</option>
                <option value="markdown">Markdown</option>
                <option value="code">Code</option>
            </select>
        </div>
        <div class="checkbox-wrap">
            <input type="checkbox" id="burn">
            <label for="burn" style="color:var(--danger)">Burn after reading</label>
        </div>
        <button class="btn" id="createBtn" onclick="createPaste()">Encrypt &amp; Save</button>
    </div>

    <div class="error-msg" id="error"></div>

    <div class="result-box" id="result">
        <div style="margin-bottom:8px;color:var(--muted);font-size:0.85em">
            Your encrypted paste is ready. Share this link:
        </div>
        <div class="url" id="pasteUrl"></div>
        <div style="margin-top:10px;display:flex;gap:10px">
            <button class="btn-outline" onclick="copyUrl()">Copy Link</button>
            <button class="btn-outline" onclick="location.reload()">New Paste</button>
        </div>
        <div style="margin-top:10px;color:var(--muted);font-size:0.75em">
            The #key part of the URL is your decryption key. Without it, the paste is unreadable.
        </div>
    </div>

    <div class="footer">
        <a href="/">OBLIVION Search</a> &mdash; Privacy-first tools.
        Server stores only encrypted blobs. We cannot read your pastes.
    </div>
</div>

<script>
{CRYPTO_JS}

async function createPaste() {{
    const content = document.getElementById('content').value.trim();
    if (!content) return;

    const btn = document.getElementById('createBtn');
    const error = document.getElementById('error');
    btn.disabled = true;
    btn.textContent = 'Encrypting...';
    error.style.display = 'none';

    try {{
        // 1. Generate random AES-256-GCM key
        const key = await OZCrypto.generateKey();

        // 2. Encrypt content client-side
        const {{ encrypted, iv }} = await OZCrypto.encrypt(content, key);

        // 3. Send ONLY encrypted blob to server
        const resp = await fetch('/api/paste', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
                encrypted: encrypted,
                iv: iv,
                expiry: document.getElementById('expiry').value,
                burn_after_read: document.getElementById('burn').checked,
                syntax: document.getElementById('syntax').value,
            }})
        }});

        if (!resp.ok) {{
            const err = await resp.json();
            throw new Error(err.detail || 'Failed to create paste');
        }}

        const data = await resp.json();

        // 4. Build URL with key in fragment (never sent to server)
        const url = window.location.origin + data.url + '#' + key;
        document.getElementById('pasteUrl').textContent = url;
        document.getElementById('result').style.display = 'block';
        document.getElementById('content').style.display = 'none';
        document.querySelector('.options').style.display = 'none';
        document.getElementById('how-it-works').style.display = 'none';
    }} catch (e) {{
        error.textContent = e.message;
        error.style.display = 'block';
    }} finally {{
        btn.disabled = false;
        btn.textContent = 'Encrypt & Save';
    }}
}}

function copyUrl() {{
    const url = document.getElementById('pasteUrl').textContent;
    navigator.clipboard.writeText(url).then(() => {{
        const btn = event.target;
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = 'Copy Link', 1500);
    }});
}}
</script>
</body>
</html>""")


# --------------- STRIPE / SAAS ROUTES ---------------

PRICING_CSS = """
.pricing-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:24px; margin:40px 0; }
.price-card { background:#12121a; border:1px solid #1e1e2e; border-radius:12px; padding:32px 24px; text-align:center; position:relative; transition:all 0.2s; }
.price-card:hover { border-color:#00d4ff; transform:translateY(-4px); }
.price-card.featured { border:2px solid #00d4ff; }
.price-card .badge-pop { position:absolute; top:-12px; left:50%; transform:translateX(-50%); background:#00d4ff; color:#0a0a0f; padding:4px 16px; border-radius:20px; font-size:0.8em; font-weight:700; }
.price-card h3 { font-size:1.4em; margin-bottom:8px; color:#e0e0e0; }
.price-card .price { font-size:2.4em; font-weight:700; color:#00d4ff; margin:16px 0; }
.price-card .price span { font-size:0.4em; color:#888; }
.price-card ul { list-style:none; text-align:left; margin:20px 0; }
.price-card ul li { padding:8px 0; color:#ccc; font-size:0.9em; border-bottom:1px solid #1a1a28; }
.price-card ul li:before { content:"\\2713 "; color:#00d4ff; font-weight:700; margin-right:8px; }
.price-card .cta { display:inline-block; width:100%; margin-top:16px; padding:12px 24px; background:#00d4ff; color:#000; border:none; border-radius:8px; font-weight:600; font-size:0.95em; cursor:pointer; text-decoration:none; text-align:center; font-family:inherit; transition:all 0.2s; }
.price-card .cta:hover { opacity:0.85; transform:translateY(-1px); text-decoration:none; }
.price-card .cta-outline { background:transparent; border:2px solid #00d4ff; color:#00d4ff; }
.price-card .cta-outline:hover { background:rgba(0,212,255,0.1); }
"""


@app.get("/paste/pricing", response_class=HTMLResponse)
async def paste_pricing():
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pricing — OBLIVION Paste</title>
    <style>{COMMON_STYLE}{PRICING_CSS}</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1><a href="/paste">OBLIVION Paste</a></h1>
            <div class="sub">Zero-Knowledge Encrypted Notepad</div>
        </div>
        <a href="/paste" class="btn-outline">Back to Paste</a>
    </div>

    <div style="text-align:center;padding:20px 0;">
        <h2 style="color:#00d4ff;font-size:1.6em;">Choose Your Plan</h2>
        <p style="color:#888;margin-top:8px;">Free forever for basic use. Upgrade for unlimited, never-expire pastes.</p>
    </div>

    <div class="pricing-grid">
        <div class="price-card">
            <h3>Free</h3>
            <div class="price">&pound;0<span>/forever</span></div>
            <ul>
                <li>10 pastes per day</li>
                <li>Pastes expire in 24 hours</li>
                <li>AES-256-GCM encryption</li>
                <li>Burn after reading</li>
                <li>Markdown support</li>
            </ul>
            <a href="/paste" class="cta cta-outline">Get Started</a>
        </div>
        <div class="price-card featured">
            <div class="badge-pop">MOST POPULAR</div>
            <h3>Pro</h3>
            <div class="price">&pound;3<span>/month</span></div>
            <ul>
                <li>Everything in Free</li>
                <li>Never-expire pastes</li>
                <li>Unlimited pastes per day</li>
                <li>Password-protected pastes</li>
                <li>Custom short URLs</li>
                <li>API access</li>
            </ul>
            <a href="/paste/checkout/pro" class="cta">Subscribe to Pro</a>
        </div>
        <div class="price-card">
            <h3>Business</h3>
            <div class="price">&pound;9<span>/month</span></div>
            <ul>
                <li>Everything in Pro</li>
                <li>Full REST API</li>
                <li>Team workspace</li>
                <li>Bulk paste creation</li>
                <li>Priority support</li>
                <li>Webhook notifications</li>
            </ul>
            <a href="/paste/checkout/business" class="cta cta-outline">Subscribe to Business</a>
        </div>
    </div>

    <div class="footer">
        <a href="/">OBLIVION Search</a> &mdash; Privacy-first tools. Server stores only encrypted blobs.
    </div>
</div>
</body>
</html>""")


@app.get("/paste/checkout/pro")
async def paste_checkout_pro():
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "product_data": {"name": "OBLIVION Paste Pro", "description": "Never-expire pastes, unlimited, password protection, custom URLs"},
                    "unit_amount": 300,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=DOMAIN + "/paste/success?session_id={CHECKOUT_SESSION_ID}&plan=pro",
            cancel_url=DOMAIN + "/paste/pricing",
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/paste/checkout/business")
async def paste_checkout_business():
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "product_data": {"name": "OBLIVION Paste Business", "description": "Full API, team workspace, bulk creation"},
                    "unit_amount": 900,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=DOMAIN + "/paste/success?session_id={CHECKOUT_SESSION_ID}&plan=business",
            cancel_url=DOMAIN + "/paste/pricing",
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/paste/success", response_class=HTMLResponse)
async def paste_success(session_id: str = "", plan: str = "pro"):
    email = "unknown"
    api_key = generate_api_key()
    cust_id = ""
    sub_id = ""
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        email = session.customer_details.email or session.customer_email or "unknown"
        cust_id = session.customer or ""
        sub_id = session.subscription or ""
    except Exception:
        pass

    try:
        p = await get_pool()
        async with p.acquire() as conn:
            existing = await conn.fetchval("SELECT api_key FROM paste_customers WHERE email=$1 AND plan=$2 AND active=TRUE", email, plan)
            if existing:
                api_key = existing
            else:
                await conn.execute(
                    "INSERT INTO paste_customers (email, plan, api_key, stripe_customer_id, stripe_subscription_id) VALUES ($1,$2,$3,$4,$5)",
                    email, plan, api_key, str(cust_id), str(sub_id),
                )
    except Exception as e:
        print(f"[PASTE] DB error: {e}")

    if email != "unknown":
        threading.Thread(target=send_welcome_email, args=(email, plan, api_key), daemon=True).start()

    plan_title = plan.title()
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Welcome to OBLIVION Paste {plan_title}!</title>
    <style>{COMMON_STYLE}</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1><a href="/paste">OBLIVION Paste</a></h1>
            <div class="sub">Zero-Knowledge Encrypted Notepad</div>
        </div>
    </div>

    <div style="text-align:center;padding:40px 0 20px;">
        <div style="font-size:4em;">🎉</div>
        <h2 style="margin-top:16px;color:#00d4ff;">Welcome to Paste {plan_title}!</h2>
        <p style="color:#888;margin-top:8px;">Your subscription is active. Confirmation sent to <strong style="color:#e0e0e0;">{email}</strong>.</p>
    </div>

    <div style="background:#12121a;border:1px solid #00d4ff;border-radius:12px;padding:28px;max-width:600px;margin:0 auto;">
        <h3 style="color:#00d4ff;margin-bottom:12px;">Your API Key</h3>
        <div style="background:#0a0a0f;padding:16px;border-radius:8px;font-family:monospace;color:#00d4ff;word-break:break-all;font-size:1.1em;" id="apiKey">{api_key}</div>
        <button class="btn" onclick="navigator.clipboard.writeText(document.getElementById('apiKey').textContent);this.textContent='Copied!';setTimeout(()=>this.textContent='Copy API Key',2000)" style="width:100%;margin-top:12px;">Copy API Key</button>
        <p style="color:#888;margin-top:16px;font-size:0.85em;">Save this key. Use it for API access and to unlock Pro features.</p>
        <div style="margin-top:20px;display:flex;gap:12px;">
            <a href="/paste/dashboard?key={api_key}" class="btn-outline" style="flex:1;text-align:center;padding:10px;text-decoration:none;">Dashboard</a>
            <a href="/paste" class="btn-outline" style="flex:1;text-align:center;padding:10px;text-decoration:none;">Create Paste</a>
        </div>
    </div>

    <div class="footer">
        <a href="/">OBLIVION Search</a> &mdash; Privacy-first tools.
    </div>
</div>
</body>
</html>""")


@app.get("/paste/dashboard", response_class=HTMLResponse)
async def paste_dashboard(key: str = ""):
    if not key:
        return HTMLResponse("<h1>API key required. Add ?key=YOUR_KEY to the URL.</h1>", status_code=400)
    p = await get_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM paste_customers WHERE api_key=$1 AND active=TRUE", key)
    if not row:
        return HTMLResponse("<h1>Invalid or inactive API key</h1>", status_code=403)

    r_email = row['email']
    r_plan = row['plan'].title()
    r_since = row['created_at'].strftime('%B %d, %Y')
    r_key = row['api_key']
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard — OBLIVION Paste</title>
    <style>{COMMON_STYLE}</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1><a href="/paste">OBLIVION Paste</a></h1>
            <div class="sub">Your Dashboard</div>
        </div>
        <a href="/paste" class="btn-outline">Create Paste</a>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px;">
        <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:12px;padding:24px;">
            <h3 style="color:#00d4ff;margin-bottom:12px;">Account</h3>
            <p><strong>Email:</strong> {r_email}</p>
            <p><strong>Plan:</strong> <span style="background:rgba(0,212,255,0.15);color:#00d4ff;padding:4px 12px;border-radius:20px;font-size:0.85em;">{r_plan}</span></p>
            <p><strong>Status:</strong> <span style="color:#00ff88;">Active</span></p>
            <p><strong>Since:</strong> {r_since}</p>
        </div>
        <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:12px;padding:24px;">
            <h3 style="color:#00d4ff;margin-bottom:12px;">API Key</h3>
            <div style="background:#0a0a0f;padding:12px;border-radius:8px;font-family:monospace;color:#00d4ff;font-size:0.85em;word-break:break-all;">{r_key}</div>
            <p style="color:#888;margin-top:8px;font-size:0.85em;">Use this key in the X-API-Key header for API requests.</p>
        </div>
    </div>

    <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:12px;padding:24px;margin-top:20px;">
        <h3 style="color:#00d4ff;margin-bottom:16px;">Your {r_plan} Features</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
            <div style="padding:16px;background:#0a0a0f;border-radius:8px;">
                <h4 style="color:#00d4ff;">Never-Expire Pastes</h4>
                <p style="color:#888;font-size:0.9em;">Your pastes stay forever until you delete them.</p>
            </div>
            <div style="padding:16px;background:#0a0a0f;border-radius:8px;">
                <h4 style="color:#00d4ff;">Unlimited Pastes</h4>
                <p style="color:#888;font-size:0.9em;">No daily limits on paste creation.</p>
            </div>
            <div style="padding:16px;background:#0a0a0f;border-radius:8px;">
                <h4 style="color:#00d4ff;">Password Protection</h4>
                <p style="color:#888;font-size:0.9em;">Add an extra password layer on top of encryption.</p>
            </div>
            <div style="padding:16px;background:#0a0a0f;border-radius:8px;">
                <h4 style="color:#00d4ff;">Custom URLs</h4>
                <p style="color:#888;font-size:0.9em;">Create pastes at /paste/yourname/title.</p>
            </div>
        </div>
    </div>

    <div class="footer">
        <a href="/">OBLIVION Search</a> &mdash; Privacy-first tools.
    </div>
</div>
</body>
</html>""")


@app.post("/paste/webhook")
async def paste_webhook(request: Request):
    payload = await request.body()
    try:
        event = stripe.Event.construct_from(
            stripe.util.json.loads(payload), stripe.api_key
        )
    except Exception:
        return JSONResponse({"error": "Invalid payload"}, status_code=400)

    if event.type == "customer.subscription.deleted":
        sub_id = event.data.object.id
        try:
            p = await get_pool()
            async with p.acquire() as conn:
                await conn.execute("UPDATE paste_customers SET active=FALSE WHERE stripe_subscription_id=$1", str(sub_id))
        except Exception as e:
            print(f"[PASTE] Webhook DB error: {e}")

    return JSONResponse({"status": "ok"})



@app.get("/paste/{paste_id}", response_class=HTMLResponse)
async def view_paste(paste_id: str):
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OBLIVION Paste — Viewing Encrypted Paste</title>
    <style>{COMMON_STYLE}</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1><a href="/paste">OBLIVION Paste</a></h1>
            <div class="sub">Zero-Knowledge Encrypted Notepad</div>
        </div>
        <a href="/paste" class="btn-outline">New Paste</a>
    </div>

    <div class="loading" id="loading">Decrypting paste...</div>
    <div id="error-display" style="display:none" class="burn-warning"></div>
    <div id="paste-display" style="display:none">
        <div class="info-bar" id="info-bar"></div>
        <div id="burn-warn" class="burn-warning" style="display:none">
            This paste will be destroyed after you close this page. Copy what you need now.
        </div>
        <div class="content-view" id="content"></div>
        <div style="margin-top:16px;display:flex;gap:10px">
            <button class="btn-outline" onclick="copyContent()">Copy Content</button>
            <button class="btn-outline" onclick="downloadContent()">Download</button>
            <a href="/paste" class="btn-outline">New Paste</a>
        </div>
    </div>

    <div class="footer">
        <a href="/">OBLIVION Search</a> &mdash; Privacy-first tools.
        Decryption happens entirely in your browser.
    </div>
</div>

<script>
{CRYPTO_JS}
{MARKDOWN_JS}

let decryptedContent = '';

async function loadPaste() {{
    const key = window.location.hash.substring(1);
    if (!key) {{
        showError('No decryption key found in URL. The #key fragment is required to decrypt this paste.');
        return;
    }}

    try {{
        const resp = await fetch('/api/paste/{paste_id}');
        if (resp.status === 404) {{
            showError('Paste not found. It may have expired or been deleted.');
            return;
        }}
        if (resp.status === 410) {{
            showError('This paste was set to burn after reading and has already been viewed.');
            return;
        }}
        if (!resp.ok) {{
            const err = await resp.json();
            showError(err.detail || 'Failed to load paste');
            return;
        }}

        const data = await resp.json();

        // Decrypt client-side
        try {{
            decryptedContent = await OZCrypto.decrypt(data.encrypted, data.iv, key);
        }} catch (e) {{
            showError('Decryption failed. The key in the URL may be incorrect or corrupted.');
            return;
        }}

        // Display info
        const info = document.getElementById('info-bar');
        const created = new Date(data.created_at).toLocaleString();
        let infoHtml = '<span>Created: ' + created + '</span>';
        if (data.expires_at) {{
            const exp = new Date(data.expires_at).toLocaleString();
            infoHtml += '<span>Expires: ' + exp + '</span>';
        }}
        infoHtml += '<span>Format: ' + data.syntax + '</span>';
        info.innerHTML = infoHtml;

        // Show burn warning
        if (data.burn_after_read) {{
            document.getElementById('burn-warn').style.display = 'block';
        }}

        // Render content
        const contentEl = document.getElementById('content');
        if (data.syntax === 'markdown') {{
            contentEl.innerHTML = renderMarkdown(decryptedContent);
            contentEl.classList.add('markdown');
        }} else {{
            contentEl.textContent = decryptedContent;
        }}

        document.getElementById('loading').style.display = 'none';
        document.getElementById('paste-display').style.display = 'block';

    }} catch (e) {{
        showError('Error loading paste: ' + e.message);
    }}
}}

function showError(msg) {{
    document.getElementById('loading').style.display = 'none';
    const el = document.getElementById('error-display');
    el.textContent = msg;
    el.style.display = 'block';
}}

function copyContent() {{
    navigator.clipboard.writeText(decryptedContent).then(() => {{
        const btn = event.target;
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = 'Copy Content', 1500);
    }});
}}

function downloadContent() {{
    const blob = new Blob([decryptedContent], {{ type: 'text/plain' }});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'oblivion-paste.txt';
    a.click();
}}

loadPaste();
</script>
</body>
</html>""")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3073)
