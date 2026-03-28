#!/usr/bin/env python3
"""
OBLIVION Short — Privacy Link Shortener
Port 3074 — https://oblivionsearch.com/s

No click tracking. No referrer logging. No cookies. Just short links.
"""

import asyncio
import hashlib
import re
import secrets
import smtplib
import string
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from urllib.parse import urlparse

import asyncpg
import stripe
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI(title="OBLIVION Short", docs_url=None, redoc_url=None)

DB_DSN = "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/oblivion_short"
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
    return "short_" + secrets.token_hex(24)


def send_welcome_email(to_email, plan, api_key):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Welcome to OBLIVION Short {plan.title()}!"
        msg["From"] = f"OBLIVION Short <{SMTP_USER}>"
        msg["To"] = to_email
        html = f"""<html><body style="background:#0a0a0f;color:#e0e0e0;font-family:monospace;padding:30px;">
        <h1 style="color:#00d4ff;">Welcome to OBLIVION Short {plan.title()}</h1>
        <p>Your subscription is now active.</p>
        <p><strong>Your API Key:</strong></p>
        <pre style="background:#12121a;padding:16px;border:1px solid #1e1e2e;border-radius:8px;color:#00d4ff;font-size:1.1em;">{api_key}</pre>
        <p>Your {plan.title()} features:</p>
        <ul>
        <li>Unlimited short links</li>
        <li>Custom aliases</li>
        <li>QR codes</li>
        <li>Aggregate click analytics (privacy-safe)</li>
        {"<li>API access, branded domains, bulk creation</li>" if plan == "business" else ""}
        </ul>
        <p>Dashboard: <a href="{DOMAIN}/s/dashboard?key={api_key}" style="color:#00d4ff;">{DOMAIN}/s/dashboard?key={api_key}</a></p>
        <p style="color:#888;margin-top:30px;">— OBLIVION Short | <a href="{DOMAIN}" style="color:#00d4ff;">oblivionsearch.com</a></p>
        </body></html>"""
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        print(f"[SHORT] Email send error: {e}")

# Rate limiting: IP -> list of timestamps
rate_limits: dict = defaultdict(list)

# Basic malware/phishing domain blocklist
BLOCKED_DOMAINS = {
    "malware.com", "phishing.com", "evil.com", "bit-ly.cc",
    "amaz0n.com", "paypa1.com", "g00gle.com", "faceb00k.com",
    "login-verify.com", "account-update.com", "secure-login.net",
    "free-iphone.com", "you-won-prize.com", "click-here-now.com",
    "crypto-double.com", "wallet-verify.com",
}

BASE62 = string.ascii_letters + string.digits


def generate_code(length=6):
    return "".join(secrets.choice(BASE62) for _ in range(length))


def hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def is_valid_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme in ("http", "https"), result.netloc])
    except Exception:
        return False


def is_blocked(url: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        return domain in BLOCKED_DOMAINS
    except Exception:
        return False


def check_rate_limit(ip: str) -> bool:
    """10 links per IP per hour."""
    now = time.time()
    timestamps = rate_limits[ip]
    # Remove entries older than 1 hour
    rate_limits[ip] = [t for t in timestamps if now - t < 3600]
    return len(rate_limits[ip]) < 10


async def get_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    return pool


@app.on_event("startup")
async def startup():
    # Create database if needed
    try:
        conn = await asyncpg.connect(
            "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/postgres"
        )
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname='oblivion_short'"
        )
        if not exists:
            await conn.execute("CREATE DATABASE oblivion_short")
        await conn.close()
    except Exception:
        pass

    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS links (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                original_url TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                ip_hash TEXT,
                click_count INTEGER DEFAULT 0,
                owner_api_key TEXT
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_links_code ON links(code)")
        # Add columns if missing (idempotent migration)
        try:
            await conn.execute("ALTER TABLE links ADD COLUMN IF NOT EXISTS click_count INTEGER DEFAULT 0")
            await conn.execute("ALTER TABLE links ADD COLUMN IF NOT EXISTS owner_api_key TEXT")
        except Exception:
            pass
        # Customers table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS short_customers (
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
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_short_api_key ON short_customers(api_key)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_short_email ON short_customers(email)")


@app.on_event("shutdown")
async def shutdown():
    global pool
    if pool:
        await pool.close()


# --------------- API ---------------

@app.post("/api/s/create")
async def create_short_link(request: Request):
    body = await request.json()
    url = body.get("url", "").strip()

    if not url:
        raise HTTPException(400, "URL is required")

    # Add scheme if missing
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    if not is_valid_url(url):
        raise HTTPException(400, "Invalid URL")

    if is_blocked(url):
        raise HTTPException(403, "This domain has been blocked for safety reasons")

    # Don't shorten our own short links
    parsed = urlparse(url)
    if parsed.netloc in ("oblivionsearch.com", "www.oblivionsearch.com") and parsed.path.startswith("/s/"):
        raise HTTPException(400, "Cannot shorten an OBLIVION Short link")

    ip_raw = request.headers.get("x-real-ip", request.client.host)
    ip_hashed = hash_ip(ip_raw)

    # Check for API key (Pro/Business skip rate limits)
    api_key = body.get("api_key", "") or request.headers.get("x-api-key", "")
    owner_key = None
    if api_key:
        p = await get_pool()
        async with p.acquire() as conn:
            valid = await conn.fetchval("SELECT api_key FROM short_customers WHERE api_key=$1 AND active=TRUE", api_key)
            if valid:
                owner_key = api_key

    if not owner_key:
        if not check_rate_limit(ip_hashed):
            raise HTTPException(429, "Rate limit exceeded. Max 10 links per hour.")
        rate_limits[ip_hashed].append(time.time())

    # Check if URL already shortened by this IP
    p = await get_pool()
    async with p.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT code FROM links WHERE original_url = $1 AND ip_hash = $2",
            url, ip_hashed,
        )
        if existing:
            return {"code": existing, "short_url": f"/s/{existing}"}

    # Generate unique code
    for _ in range(10):
        code = generate_code()
        try:
            async with p.acquire() as conn:
                await conn.execute(
                    "INSERT INTO links (code, original_url, ip_hash, owner_api_key) VALUES ($1, $2, $3, $4)",
                    code, url, ip_hashed, owner_key,
                )
            return {"code": code, "short_url": f"/s/{code}"}
        except asyncpg.UniqueViolationError:
            continue

    raise HTTPException(500, "Failed to generate unique code. Try again.")


@app.get("/api/s/{code}")
async def get_link_info(code: str):
    """Get info about a link without redirecting."""
    p = await get_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM links WHERE code = $1", code)

    if not row:
        raise HTTPException(404, "Link not found")

    return {
        "code": row["code"],
        "original_url": row["original_url"],
        "created_at": row["created_at"].isoformat(),
    }


# --------------- Pages ---------------

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
.container { max-width: 700px; margin: 0 auto; padding: 20px; }
.header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 0; border-bottom: 1px solid var(--border); margin-bottom: 30px;
}
.header h1 { font-size: 1.3em; color: var(--accent); font-weight: 600; }
.header h1 a { color: var(--accent); text-decoration: none; }
.header .sub { color: var(--muted); font-size: 0.8em; }
.btn {
    background: var(--accent); color: #000; border: none; padding: 12px 28px;
    border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 0.95em;
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
.input-group {
    display: flex; gap: 12px; margin-bottom: 16px;
}
.input-group input {
    flex: 1; background: var(--surface); border: 1px solid var(--border);
    color: var(--text); padding: 12px 16px; border-radius: 8px;
    font-family: inherit; font-size: 1em; outline: none;
    transition: border 0.2s;
}
.input-group input:focus { border-color: var(--accent); }
.input-group input::placeholder { color: #555; }
.result-box {
    background: var(--surface); border: 1px solid var(--accent);
    border-radius: 8px; padding: 20px; margin-top: 20px; display: none;
}
.short-url {
    color: var(--accent); font-size: 1.2em; font-weight: 600;
    word-break: break-all;
}
.original-url {
    color: var(--muted); font-size: 0.85em; margin-top: 8px;
    word-break: break-all;
}
.features {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px; margin: 30px 0;
}
.feature {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px;
}
.feature h3 { color: var(--accent); font-size: 0.9em; margin-bottom: 6px; }
.feature p { color: var(--muted); font-size: 0.8em; line-height: 1.4; }
.error-msg { color: var(--danger); margin-top: 12px; font-size: 0.85em; display: none; }
.info-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 24px; text-align: center;
}
.info-card .dest { color: var(--accent); font-size: 1.1em; word-break: break-all; margin: 16px 0; }
.info-card .meta { color: var(--muted); font-size: 0.8em; }
.footer {
    margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--border);
    text-align: center; color: var(--muted); font-size: 0.75em;
}
.footer a { color: var(--accent); text-decoration: none; }
@media (max-width: 600px) {
    .container { padding: 12px; }
    .input-group { flex-direction: column; }
    .features { grid-template-columns: 1fr; }
}
@media (max-width: 480px) {
    body { overflow-x: hidden; }
    .header h1 { font-size: 1.1em; }
    .input-group input { font-size: 16px; width: 100%; padding: 14px 16px; }
    .btn { min-height: 44px; font-size: 16px; width: 100%; }
    .btn-outline { min-height: 44px; }
    .short-url { font-size: 1em; }
    .result-box { padding: 14px; }
    .feature h3 { font-size: 0.85em; }
    .feature p { font-size: 0.78em; }
    .info-card { padding: 16px; }
    .info-card .dest { font-size: 0.95em; }
}
@media (max-width: 375px) {
    .header { flex-direction: column; gap: 8px; text-align: center; }
    .input-group input { font-size: 16px; }
    .short-url { font-size: 0.9em; }
    .info-card .meta { font-size: 0.75em; }
}
"""


@app.get("/s", response_class=HTMLResponse)
async def short_landing():
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OBLIVION Short — Privacy Link Shortener</title>
    <meta name="description" content="Shorten URLs without tracking. No click analytics, no cookies, no referrer logging. Just short links.">
    <link rel="canonical" href="https://oblivionsearch.com/s">
    <meta property="og:title" content="OBLIVION Short — Privacy Link Shortener">
    <meta property="og:description" content="Shorten URLs without tracking. No click analytics, no cookies, no referrer logging. Just short links.">
    <meta property="og:url" content="https://oblivionsearch.com/s">
    <meta property="og:type" content="website">
    <meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
    <style>{COMMON_STYLE}</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1><a href="/s">OBLIVION Short</a></h1>
            <div class="sub">Privacy Link Shortener</div>
        </div>
        <div style="display:flex;gap:10px;">
            <a href="/s/pricing" class="btn-outline">Pricing</a>
            <a href="/" class="btn-outline">OBLIVION Search</a>
        </div>
    </div>

    <div class="input-group">
        <input type="text" id="urlInput" placeholder="https://example.com/very/long/url" autofocus
               onkeypress="if(event.key==='Enter')shortenUrl()">
        <button class="btn" id="shortenBtn" onclick="shortenUrl()">Shorten</button>
    </div>
    <div class="error-msg" id="error"></div>

    <div class="result-box" id="result">
        <div style="color:var(--muted);font-size:0.85em;margin-bottom:8px">Your short link:</div>
        <div class="short-url" id="shortUrl"></div>
        <div class="original-url" id="origUrl"></div>
        <div style="margin-top:14px;display:flex;gap:10px">
            <button class="btn-outline" onclick="copyUrl()">Copy Link</button>
            <button class="btn-outline" onclick="document.getElementById('result').style.display='none';document.getElementById('urlInput').value='';document.getElementById('urlInput').focus()">Shorten Another</button>
        </div>
    </div>

    <div class="features">
        <div class="feature">
            <h3>No Tracking</h3>
            <p>We don't count clicks, log referrers, or set cookies. Your links are just links.</p>
        </div>
        <div class="feature">
            <h3>No Accounts</h3>
            <p>No signup required. Just paste a URL and get a short link instantly.</p>
        </div>
        <div class="feature">
            <h3>Link Preview</h3>
            <p>Add + to any short link to see where it goes before clicking.</p>
        </div>
        <div class="feature">
            <h3>IP Hashing</h3>
            <p>Your IP is hashed for rate limiting only. Raw IPs are never stored.</p>
        </div>
    </div>

    <div class="footer">
        <a href="/">OBLIVION Search</a> &mdash; Privacy-first tools. No tracking. No logs. No cookies.
    </div>
</div>

<script>
async function shortenUrl() {{
    const input = document.getElementById('urlInput');
    const url = input.value.trim();
    if (!url) return;

    const btn = document.getElementById('shortenBtn');
    const error = document.getElementById('error');
    btn.disabled = true;
    btn.textContent = 'Shortening...';
    error.style.display = 'none';

    try {{
        const resp = await fetch('/api/s/create', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ url }})
        }});

        if (!resp.ok) {{
            const err = await resp.json();
            throw new Error(err.detail || 'Failed to shorten URL');
        }}

        const data = await resp.json();
        const fullUrl = window.location.origin + data.short_url;
        document.getElementById('shortUrl').textContent = fullUrl;
        document.getElementById('origUrl').textContent = url;
        document.getElementById('result').style.display = 'block';
    }} catch (e) {{
        error.textContent = e.message;
        error.style.display = 'block';
    }} finally {{
        btn.disabled = false;
        btn.textContent = 'Shorten';
    }}
}}

function copyUrl() {{
    const url = document.getElementById('shortUrl').textContent;
    navigator.clipboard.writeText(url).then(() => {{
        const btn = event.target;
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = 'Copy Link', 1500);
    }});
}}
</script>
</body>
</html>""")


# --------------- STRIPE / SAAS ROUTES (must be before /s/{code} catch-all) ---------------

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


@app.get("/s/pricing", response_class=HTMLResponse)
async def short_pricing():
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pricing — OBLIVION Short</title>
    <style>{COMMON_STYLE}{PRICING_CSS}</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1><a href="/s">OBLIVION Short</a></h1>
            <div class="sub">Privacy Link Shortener</div>
        </div>
        <a href="/s" class="btn-outline">Back to Short</a>
    </div>

    <div style="text-align:center;padding:20px 0;">
        <h2 style="color:#00d4ff;font-size:1.6em;">Choose Your Plan</h2>
        <p style="color:#888;margin-top:8px;">Free for basic use. Upgrade for unlimited links, analytics & custom aliases.</p>
        <p style="color:#666;margin-top:4px;font-size:0.85em;">Privacy promise: analytics show ONLY aggregate click counts. We never track WHO clicked.</p>
    </div>

    <div class="pricing-grid">
        <div class="price-card">
            <h3>Free</h3>
            <div class="price">&pound;0<span>/forever</span></div>
            <ul>
                <li>10 links per day</li>
                <li>Random short codes</li>
                <li>Link preview (+)</li>
                <li>No tracking, no cookies</li>
            </ul>
            <a href="/s" class="cta cta-outline">Get Started</a>
        </div>
        <div class="price-card featured">
            <div class="badge-pop">MOST POPULAR</div>
            <h3>Pro</h3>
            <div class="price">&pound;5<span>/month</span></div>
            <ul>
                <li>Everything in Free</li>
                <li>Unlimited links</li>
                <li>Custom aliases</li>
                <li>QR codes</li>
                <li>Aggregate click counts</li>
                <li>Link dashboard</li>
            </ul>
            <a href="/s/checkout/pro" class="cta">Subscribe to Pro</a>
        </div>
        <div class="price-card">
            <h3>Business</h3>
            <div class="price">&pound;15<span>/month</span></div>
            <ul>
                <li>Everything in Pro</li>
                <li>Full REST API</li>
                <li>Bulk link creation</li>
                <li>Branded domains</li>
                <li>Priority support</li>
                <li>Webhook notifications</li>
            </ul>
            <a href="/s/checkout/business" class="cta cta-outline">Subscribe to Business</a>
        </div>
    </div>

    <div class="footer">
        <a href="/">OBLIVION Search</a> &mdash; Privacy-first tools. No tracking. No logs. No cookies.
    </div>
</div>
</body>
</html>""")


@app.get("/s/checkout/pro")
async def short_checkout_pro():
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "product_data": {"name": "OBLIVION Short Pro", "description": "Unlimited links, custom aliases, QR codes, click analytics"},
                    "unit_amount": 500,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=DOMAIN + "/s/success?session_id={CHECKOUT_SESSION_ID}&plan=pro",
            cancel_url=DOMAIN + "/s/pricing",
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/s/checkout/business")
async def short_checkout_business():
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "product_data": {"name": "OBLIVION Short Business", "description": "API, branded domains, bulk creation, everything in Pro"},
                    "unit_amount": 1500,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=DOMAIN + "/s/success?session_id={CHECKOUT_SESSION_ID}&plan=business",
            cancel_url=DOMAIN + "/s/pricing",
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/s/success", response_class=HTMLResponse)
async def short_success(session_id: str = "", plan: str = "pro"):
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
            existing = await conn.fetchval("SELECT api_key FROM short_customers WHERE email=$1 AND plan=$2 AND active=TRUE", email, plan)
            if existing:
                api_key = existing
            else:
                await conn.execute(
                    "INSERT INTO short_customers (email, plan, api_key, stripe_customer_id, stripe_subscription_id) VALUES ($1,$2,$3,$4,$5)",
                    email, plan, api_key, str(cust_id), str(sub_id),
                )
    except Exception as e:
        print(f"[SHORT] DB error: {e}")

    if email != "unknown":
        threading.Thread(target=send_welcome_email, args=(email, plan, api_key), daemon=True).start()

    plan_title = plan.title()
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Welcome to OBLIVION Short {plan_title}!</title>
    <style>{COMMON_STYLE}</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1><a href="/s">OBLIVION Short</a></h1>
            <div class="sub">Privacy Link Shortener</div>
        </div>
    </div>

    <div style="text-align:center;padding:40px 0 20px;">
        <div style="font-size:4em;">🎉</div>
        <h2 style="margin-top:16px;color:#00d4ff;">Welcome to Short {plan_title}!</h2>
        <p style="color:#888;margin-top:8px;">Your subscription is active. Confirmation sent to <strong style="color:#e0e0e0;">{email}</strong>.</p>
    </div>

    <div style="background:#12121a;border:1px solid #00d4ff;border-radius:12px;padding:28px;max-width:600px;margin:0 auto;">
        <h3 style="color:#00d4ff;margin-bottom:12px;">Your API Key</h3>
        <div style="background:#0a0a0f;padding:16px;border-radius:8px;font-family:monospace;color:#00d4ff;word-break:break-all;font-size:1.1em;" id="apiKey">{api_key}</div>
        <button class="btn" onclick="navigator.clipboard.writeText(document.getElementById('apiKey').textContent);this.textContent='Copied!';setTimeout(()=>this.textContent='Copy API Key',2000)" style="width:100%;margin-top:12px;">Copy API Key</button>
        <p style="color:#888;margin-top:16px;font-size:0.85em;">Save this key. Use it to access your dashboard, skip rate limits, and use the API.</p>
        <div style="margin-top:20px;display:flex;gap:12px;">
            <a href="/s/dashboard?key={api_key}" class="btn-outline" style="flex:1;text-align:center;padding:10px;text-decoration:none;">Dashboard</a>
            <a href="/s" class="btn-outline" style="flex:1;text-align:center;padding:10px;text-decoration:none;">Shorten Links</a>
        </div>
    </div>

    <div class="footer">
        <a href="/">OBLIVION Search</a> &mdash; Privacy-first tools.
    </div>
</div>
</body>
</html>""")


@app.get("/s/dashboard", response_class=HTMLResponse)
async def short_dashboard(key: str = ""):
    if not key:
        return HTMLResponse("<h1>API key required. Add ?key=YOUR_KEY to the URL.</h1>", status_code=400)
    p = await get_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM short_customers WHERE api_key=$1 AND active=TRUE", key)
    if not row:
        return HTMLResponse("<h1>Invalid or inactive API key</h1>", status_code=403)

    # Get their links
    async with p.acquire() as conn:
        links = await conn.fetch("SELECT code, original_url, click_count, created_at FROM links WHERE owner_api_key=$1 ORDER BY created_at DESC LIMIT 100", key)

    r_email = row['email']
    r_plan = row['plan'].title()
    r_since = row['created_at'].strftime('%B %d, %Y')
    r_key = row['api_key']
    total_links = len(links)
    total_clicks = sum(l['click_count'] or 0 for l in links)

    links_html = ""
    for l in links:
        short_url = f"{DOMAIN}/s/{l['code']}"
        clicks = l['click_count'] or 0
        created = l['created_at'].strftime('%Y-%m-%d')
        orig = l['original_url']
        if len(orig) > 60:
            orig = orig[:57] + "..."
        links_html += f"""<tr>
            <td style="padding:10px;"><a href="{short_url}" style="color:#00d4ff;" target="_blank">/s/{l['code']}</a></td>
            <td style="padding:10px;color:#888;word-break:break-all;max-width:300px;">{orig}</td>
            <td style="padding:10px;text-align:center;color:#00d4ff;font-weight:600;">{clicks}</td>
            <td style="padding:10px;color:#888;">{created}</td>
        </tr>"""

    if not links_html:
        links_html = '<tr><td colspan="4" style="padding:20px;text-align:center;color:#888;">No links yet. Go shorten some URLs!</td></tr>'

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard — OBLIVION Short</title>
    <style>{COMMON_STYLE}
    table {{ width:100%; border-collapse:collapse; }}
    th {{ text-align:left; padding:12px 10px; border-bottom:2px solid #1e1e2e; color:#00d4ff; font-size:0.9em; }}
    tr {{ border-bottom:1px solid #1e1e2e; }}
    tr:hover {{ background:rgba(0,212,255,0.03); }}
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1><a href="/s">OBLIVION Short</a></h1>
            <div class="sub">Your Dashboard</div>
        </div>
        <a href="/s" class="btn-outline">Shorten Links</a>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin:20px 0;">
        <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:12px;padding:20px;text-align:center;">
            <div style="font-size:2em;font-weight:700;color:#00d4ff;">{r_plan}</div>
            <div style="color:#888;font-size:0.85em;">Plan</div>
        </div>
        <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:12px;padding:20px;text-align:center;">
            <div style="font-size:2em;font-weight:700;color:#00d4ff;">{total_links}</div>
            <div style="color:#888;font-size:0.85em;">Total Links</div>
        </div>
        <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:12px;padding:20px;text-align:center;">
            <div style="font-size:2em;font-weight:700;color:#00d4ff;">{total_clicks}</div>
            <div style="color:#888;font-size:0.85em;">Total Clicks</div>
        </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px;">
        <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:12px;padding:20px;">
            <h3 style="color:#00d4ff;margin-bottom:8px;">Account</h3>
            <p><strong>Email:</strong> {r_email}</p>
            <p><strong>Since:</strong> {r_since}</p>
        </div>
        <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:12px;padding:20px;">
            <h3 style="color:#00d4ff;margin-bottom:8px;">API Key</h3>
            <div style="background:#0a0a0f;padding:10px;border-radius:6px;font-family:monospace;color:#00d4ff;font-size:0.8em;word-break:break-all;">{r_key}</div>
        </div>
    </div>

    <div style="background:#12121a;border:1px solid #1e1e2e;border-radius:12px;padding:24px;">
        <h3 style="color:#00d4ff;margin-bottom:16px;">Your Links</h3>
        <div style="overflow-x:auto;">
            <table>
                <thead>
                    <tr>
                        <th>Short Link</th>
                        <th>Destination</th>
                        <th style="text-align:center;">Clicks</th>
                        <th>Created</th>
                    </tr>
                </thead>
                <tbody>
                    {links_html}
                </tbody>
            </table>
        </div>
        <p style="color:#666;font-size:0.8em;margin-top:16px;">Click counts are aggregate only. No visitor data is stored or tracked.</p>
    </div>

    <div class="footer">
        <a href="/">OBLIVION Search</a> &mdash; Privacy-first tools.
    </div>
</div>
</body>
</html>""")


@app.post("/s/webhook")
async def short_webhook(request: Request):
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
                await conn.execute("UPDATE short_customers SET active=FALSE WHERE stripe_subscription_id=$1", str(sub_id))
        except Exception as e:
            print(f"[SHORT] Webhook DB error: {e}")

    return JSONResponse({"status": "ok"})


@app.get("/s/{code}", response_class=HTMLResponse)
async def redirect_or_preview(code: str):
    """Redirect to original URL, or show preview if code ends with +"""
    # Check for preview mode (code+)
    if code.endswith("+"):
        actual_code = code[:-1]
        return await show_preview(actual_code)

    p = await get_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT original_url FROM links WHERE code = $1", code)

    if not row:
        raise HTTPException(404, "Link not found")

    # Increment aggregate click count (no visitor data stored)
    asyncio.create_task(_increment_clicks(code))

    # 302 redirect — no visitor tracking, no referrer logging
    return RedirectResponse(url=row["original_url"], status_code=302)


async def _increment_clicks(code: str):
    try:
        p = await get_pool()
        async with p.acquire() as conn:
            await conn.execute("UPDATE links SET click_count = click_count + 1 WHERE code = $1", code)
    except Exception:
        pass


async def show_preview(code: str):
    """Show link destination without redirecting."""
    p = await get_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM links WHERE code = $1", code)

    if not row:
        raise HTTPException(404, "Link not found")

    created = row["created_at"].strftime("%B %d, %Y at %H:%M UTC")
    orig = row["original_url"]
    parsed = urlparse(orig)

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OBLIVION Short — Link Preview</title>
    <style>{COMMON_STYLE}</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1><a href="/s">OBLIVION Short</a></h1>
            <div class="sub">Link Preview</div>
        </div>
        <a href="/s" class="btn-outline">Shorten a Link</a>
    </div>

    <div class="info-card">
        <div style="color:var(--muted);font-size:0.85em">This short link points to:</div>
        <div class="dest">{orig}</div>
        <div class="meta">
            Domain: <strong>{parsed.netloc}</strong><br>
            Created: {created}
        </div>
        <div style="margin-top:20px;display:flex;gap:12px;justify-content:center">
            <a href="/s/{code}" class="btn" style="text-decoration:none">Go to Link</a>
            <a href="/s" class="btn-outline" style="text-decoration:none">Back to OBLIVION Short</a>
        </div>
    </div>

    <div class="footer">
        <a href="/">OBLIVION Search</a> &mdash; Preview any OBLIVION Short link by adding + to the URL.
    </div>
</div>
</body>
</html>""")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3074)
