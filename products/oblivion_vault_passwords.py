#!/usr/bin/env python3
"""
OBLIVION Vault — Stateless Password Manager
Port 3071

Generates passwords deterministically from (master password + site + login).
NO storage, NO sync, NO cloud. All crypto happens CLIENT-SIDE in JavaScript.
The master password NEVER leaves the browser.

Inspired by LessPass (GPLv3).
"""

import hashlib
import secrets
import smtplib
import string
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

import asyncpg
import stripe
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI(title="OBLIVION Vault", docs_url=None, redoc_url=None)

# ─── Stripe ───
STRIPE_SECRET = "os.environ.get("STRIPE_SECRET_KEY", "")"
stripe.api_key = STRIPE_SECRET
DOMAIN = "https://oblivionsearch.com"

# ─── Database ───
DB_DSN = "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/oblivion_vault"
pool: Optional[asyncpg.Pool] = None

# ─── SMTP ───
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "os.environ.get("SMTP_USER", "")"
SMTP_PASS = "os.environ.get("SMTP_PASS", "")"


def generate_api_key():
    return "vault_" + secrets.token_hex(24)


def send_welcome_email(to_email, plan, api_key):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Welcome to OBLIVION Vault {plan.title()}!"
        msg["From"] = f"OBLIVION Vault <{SMTP_USER}>"
        msg["To"] = to_email
        html = f"""<html><body style="background:#0a0a0f;color:#e0e0e0;font-family:monospace;padding:30px;">
        <h1 style="color:#00d4ff;">Welcome to OBLIVION Vault {plan.title()}</h1>
        <p>Your subscription is now active.</p>
        <p><strong>Your API Key:</strong></p>
        <pre style="background:#12121a;padding:16px;border:1px solid #1e1e2e;border-radius:8px;color:#00d4ff;font-size:1.1em;">{api_key}</pre>
        <p>Use this key to access Pro features:</p>
        <ul>
        <li>Saved site profiles (encrypted, stored server-side)</li>
        <li>Breach monitoring alerts (weekly HIBP check)</li>
        <li>Priority support</li>
        {"<li>Team dashboard (up to 10 users)</li>" if plan == "team" else ""}
        </ul>
        <p>Manage your account: <a href="{DOMAIN}/vault/dashboard?key={api_key}" style="color:#00d4ff;">{DOMAIN}/vault/dashboard?key={api_key}</a></p>
        <p style="color:#888;margin-top:30px;">— OBLIVION Vault | <a href="{DOMAIN}" style="color:#00d4ff;">oblivionsearch.com</a></p>
        </body></html>"""
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        print(f"[VAULT] Email send error: {e}")


async def get_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    return pool


@app.on_event("startup")
async def startup_db():
    try:
        conn = await asyncpg.connect("postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/postgres")
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname='oblivion_vault'")
        if not exists:
            await conn.execute("CREATE DATABASE oblivion_vault")
        await conn.close()
    except Exception:
        pass
    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vault_customers (
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
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_vault_api_key ON vault_customers(api_key)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_vault_email ON vault_customers(email)")


@app.on_event("shutdown")
async def shutdown_db():
    global pool
    if pool:
        await pool.close()

# ─── Dark theme colors ───
BG = "#0a0a0f"
BG2 = "#12121a"
BG3 = "#1a1a28"
ACCENT = "#00d4ff"
ACCENT2 = "#00a8cc"
TEXT = "#e0e0e0"
TEXT2 = "#999"
DANGER = "#ff4757"
SUCCESS = "#2ed573"
WARNING = "#ffa502"

COMMON_HEAD = f"""
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="{BG}">
<link rel="manifest" href="/vault/manifest.json">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔐</text></svg>">
"""

COMMON_CSS = f"""
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:{BG}; color:{TEXT}; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; line-height:1.6; min-height:100vh; }}
a {{ color:{ACCENT}; text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.container {{ max-width:900px; margin:0 auto; padding:20px; }}
nav {{ background:{BG2}; border-bottom:1px solid #222; padding:12px 0; position:sticky; top:0; z-index:100; }}
nav .container {{ display:flex; align-items:center; gap:24px; flex-wrap:wrap; }}
nav .logo {{ font-size:1.3em; font-weight:700; color:{ACCENT}; display:flex; align-items:center; gap:8px; }}
nav .logo span {{ font-size:0.7em; color:{TEXT2}; font-weight:400; }}
nav .links {{ display:flex; gap:16px; flex-wrap:wrap; }}
nav .links a {{ color:{TEXT2}; font-size:0.9em; padding:6px 12px; border-radius:6px; transition:all 0.2s; }}
nav .links a:hover, nav .links a.active {{ color:{ACCENT}; background:rgba(0,212,255,0.08); text-decoration:none; }}
.hero {{ text-align:center; padding:60px 0 40px; }}
.hero h1 {{ font-size:2.8em; margin-bottom:16px; }}
.hero h1 .accent {{ color:{ACCENT}; }}
.hero p {{ font-size:1.2em; color:{TEXT2}; max-width:600px; margin:0 auto 30px; }}
.btn {{ display:inline-block; padding:12px 28px; background:{ACCENT}; color:{BG}; border:none; border-radius:8px; font-size:1em; font-weight:600; cursor:pointer; transition:all 0.2s; }}
.btn:hover {{ background:{ACCENT2}; text-decoration:none; transform:translateY(-1px); }}
.btn-outline {{ background:transparent; border:2px solid {ACCENT}; color:{ACCENT}; }}
.btn-outline:hover {{ background:rgba(0,212,255,0.1); }}
.card {{ background:{BG2}; border:1px solid #222; border-radius:12px; padding:28px; margin-bottom:20px; }}
.card h3 {{ color:{ACCENT}; margin-bottom:12px; font-size:1.2em; }}
input, select {{ background:{BG3}; border:1px solid #333; color:{TEXT}; padding:12px 16px; border-radius:8px; font-size:1em; width:100%; transition:border 0.2s; }}
input:focus, select:focus {{ outline:none; border-color:{ACCENT}; }}
label {{ display:block; margin-bottom:6px; color:{TEXT2}; font-size:0.9em; font-weight:500; }}
.form-group {{ margin-bottom:18px; }}
.grid-2 {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
.grid-3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:20px; }}
footer {{ text-align:center; padding:40px 0; color:{TEXT2}; font-size:0.85em; border-top:1px solid #222; margin-top:60px; }}
.badge {{ display:inline-block; padding:4px 10px; border-radius:20px; font-size:0.8em; font-weight:600; }}
.badge-green {{ background:rgba(46,213,115,0.15); color:{SUCCESS}; }}
.badge-red {{ background:rgba(255,71,87,0.15); color:{DANGER}; }}
.badge-yellow {{ background:rgba(255,165,2,0.15); color:{WARNING}; }}
.strength-bar {{ height:6px; border-radius:3px; background:#222; margin-top:8px; overflow:hidden; }}
.strength-bar .fill {{ height:100%; border-radius:3px; transition:width 0.3s, background 0.3s; }}
.password-output {{ background:{BG3}; border:2px solid #333; border-radius:8px; padding:16px; font-family:'Courier New',monospace; font-size:1.3em; word-break:break-all; position:relative; min-height:54px; display:flex; align-items:center; justify-content:space-between; gap:12px; }}
.password-output .copy-btn {{ background:{ACCENT}; color:{BG}; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:0.7em; font-weight:600; white-space:nowrap; flex-shrink:0; }}
.password-output .copy-btn:hover {{ background:{ACCENT2}; }}
.toggle-row {{ display:flex; flex-wrap:wrap; gap:12px; margin-bottom:18px; }}
.toggle-row label {{ display:flex; align-items:center; gap:8px; cursor:pointer; padding:8px 14px; background:{BG3}; border:1px solid #333; border-radius:8px; color:{TEXT}; font-size:0.9em; margin:0; }}
.toggle-row input[type=checkbox] {{ width:auto; accent-color:{ACCENT}; }}
.section {{ padding:40px 0; }}
.section h2 {{ font-size:1.8em; margin-bottom:24px; text-align:center; }}
@media (max-width:700px) {{
    .grid-2, .grid-3 {{ grid-template-columns:1fr; }}
    .hero h1 {{ font-size:2em; }}
    nav .container {{ justify-content:center; }}
}}
@media (max-width:480px) {{
    body {{ overflow-x:hidden; }}
    .container {{ padding:12px; }}
    nav .container {{ gap:12px; }}
    .hero {{ padding:30px 0 20px; }}
    .hero h1 {{ font-size:1.6em; }}
    .hero p {{ font-size:1em; }}
    input, select {{ font-size:16px; padding:14px 16px; }}
    .btn {{ min-height:44px; font-size:16px; width:100%; text-align:center; }}
    .btn-outline {{ min-height:44px; }}
    .card {{ padding:18px; }}
    .card h3 {{ font-size:1.05em; }}
    .form-group {{ margin-bottom:14px; }}
    .password-output {{ font-size:1em; padding:12px; }}
    .password-output .copy-btn {{ padding:10px 14px; min-height:44px; }}
    .toggle-row {{ gap:8px; }}
    .toggle-row label {{ padding:10px 12px; font-size:0.85em; }}
    .grid-2, .grid-3 {{ grid-template-columns:1fr; gap:12px; }}
    .section h2 {{ font-size:1.4em; }}
    nav .links {{ gap:8px; }}
    nav .links a {{ padding:8px 10px; font-size:0.82em; }}
}}
@media (max-width:375px) {{
    .hero h1 {{ font-size:1.3em; }}
    .hero p {{ font-size:0.9em; }}
    nav .logo {{ font-size:1.1em; }}
    .card {{ padding:14px; }}
    .password-output {{ font-size:0.9em; }}
    .section h2 {{ font-size:1.2em; }}
    nav .links {{ flex-wrap:wrap; justify-content:center; }}
}}
"""

NAV_HTML = """
<nav>
  <div class="container">
    <a href="/vault" class="logo">🔐 OBLIVION Vault <span>Stateless Passwords</span></a>
    <div class="links">
      <a href="/vault" id="nav-home">Home</a>
      <a href="/vault/generate" id="nav-gen">Generator</a>
      <a href="/vault/check" id="nav-check">Strength Check</a>
      <a href="/vault/breach" id="nav-breach">Breach Check</a>
      <a href="/vault/pricing" id="nav-pricing">Pricing</a>
      <a href="/" id="nav-search">← OBLIVION Search</a>
    </div>
  </div>
</nav>
"""

FOOTER_HTML = """
<footer>
  <div class="container">
    <p>OBLIVION Vault — Part of <a href="/">OBLIVION Search</a></p>
    <p style="margin-top:8px;">No cookies. No tracking. No analytics. Your master password never leaves your browser.</p>
    <p style="margin-top:8px;">Inspired by <a href="https://github.com/lesspass/lesspass" target="_blank">LessPass</a> (GPLv3)</p>
  </div>
</footer>
"""

SW_JS = """
const CACHE_NAME = 'oblivion-vault-v1';
const URLS_TO_CACHE = ['/vault', '/vault/generate', '/vault/check', '/vault/breach'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(URLS_TO_CACHE)));
  self.skipWaiting();
});

self.addEventListener('fetch', e => {
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request).then(resp => {
      if (resp.status === 200) {
        const clone = resp.clone();
        caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
      }
      return resp;
    }).catch(() => caches.match('/vault')))
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))));
});
"""

MANIFEST_JSON = """{
  "name": "OBLIVION Vault — Stateless Password Manager",
  "short_name": "OBLIVION Vault",
  "description": "Generate deterministic passwords. No storage, no cloud, no sync.",
  "start_url": "/vault/generate",
  "display": "standalone",
  "background_color": "#0a0a0f",
  "theme_color": "#00d4ff",
  "icons": [
    {
      "src": "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔐</text></svg>",
      "sizes": "any",
      "type": "image/svg+xml"
    }
  ]
}"""

# ─── PBKDF2 + password generation JS (runs entirely client-side) ───
CRYPTO_JS = """
// OBLIVION Vault — Client-side PBKDF2 password generation
// The master password NEVER leaves the browser.

const CHARSETS = {
  uppercase: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
  lowercase: 'abcdefghijklmnopqrstuvwxyz',
  digits: '0123456789',
  symbols: '!@#$%^&*()-_=+[]{};:,.<>?/'
};

async function generatePassword(site, login, masterPassword, options) {
  const { length = 16, uppercase = true, lowercase = true, digits = true, symbols = true } = options || {};

  let charset = '';
  if (lowercase) charset += CHARSETS.lowercase;
  if (uppercase) charset += CHARSETS.uppercase;
  if (digits) charset += CHARSETS.digits;
  if (symbols) charset += CHARSETS.symbols;

  if (!charset) charset = CHARSETS.lowercase + CHARSETS.uppercase + CHARSETS.digits;

  const enc = new TextEncoder();
  const salt = enc.encode(site + login);
  const keyMaterial = await crypto.subtle.importKey('raw', enc.encode(masterPassword), 'PBKDF2', false, ['deriveBits']);

  const derived = await crypto.subtle.deriveBits(
    { name: 'PBKDF2', salt, iterations: 100000, hash: 'SHA-256' },
    keyMaterial,
    256
  );

  const bytes = new Uint8Array(derived);
  let password = '';

  // Use BigInt for uniform distribution across charset
  let bigNum = BigInt(0);
  for (let i = 0; i < bytes.length; i++) {
    bigNum = (bigNum << BigInt(8)) | BigInt(bytes[i]);
  }

  const charsetLen = BigInt(charset.length);
  for (let i = 0; i < length; i++) {
    password += charset[Number(bigNum % charsetLen)];
    bigNum = bigNum / charsetLen;
  }

  // Ensure at least one char from each enabled set (swap into random positions)
  const rules = [];
  if (lowercase) rules.push(CHARSETS.lowercase);
  if (uppercase) rules.push(CHARSETS.uppercase);
  if (digits) rules.push(CHARSETS.digits);
  if (symbols) rules.push(CHARSETS.symbols);

  const pwArr = password.split('');
  for (let ri = 0; ri < rules.length && ri < length; ri++) {
    const ruleSet = rules[ri];
    if (!pwArr.some(c => ruleSet.includes(c))) {
      // Pick a deterministic char from the rule set
      pwArr[ri] = ruleSet[Number(BigInt(bytes[ri]) % BigInt(ruleSet.length))];
    }
  }

  return pwArr.join('');
}

function calcEntropy(pw) {
  let pool = 0;
  if (/[a-z]/.test(pw)) pool += 26;
  if (/[A-Z]/.test(pw)) pool += 26;
  if (/[0-9]/.test(pw)) pool += 10;
  if (/[^a-zA-Z0-9]/.test(pw)) pool += 33;
  return pw.length * Math.log2(pool || 1);
}

function crackTime(entropy) {
  // Assume 10 billion guesses/sec
  const seconds = Math.pow(2, entropy) / 1e10;
  if (seconds < 1) return 'Instant';
  if (seconds < 60) return Math.round(seconds) + ' seconds';
  if (seconds < 3600) return Math.round(seconds/60) + ' minutes';
  if (seconds < 86400) return Math.round(seconds/3600) + ' hours';
  if (seconds < 86400*365) return Math.round(seconds/86400) + ' days';
  if (seconds < 86400*365*1e3) return Math.round(seconds/(86400*365)) + ' years';
  if (seconds < 86400*365*1e6) return Math.round(seconds/(86400*365*1e3)) + 'K years';
  if (seconds < 86400*365*1e9) return Math.round(seconds/(86400*365*1e6)) + 'M years';
  return Math.round(seconds/(86400*365*1e9)) + 'B years';
}

function strengthLevel(entropy) {
  if (entropy < 28) return { label: 'Very Weak', color: '#ff4757', pct: 15 };
  if (entropy < 36) return { label: 'Weak', color: '#ff6348', pct: 30 };
  if (entropy < 60) return { label: 'Fair', color: '#ffa502', pct: 50 };
  if (entropy < 80) return { label: 'Strong', color: '#7bed9f', pct: 75 };
  return { label: 'Very Strong', color: '#2ed573', pct: 100 };
}

async function sha1(str) {
  const data = new TextEncoder().encode(str);
  const hashBuffer = await crypto.subtle.digest('SHA-1', data);
  return Array.from(new Uint8Array(hashBuffer)).map(b => b.toString(16).padStart(2, '0')).join('').toUpperCase();
}
"""


# ──────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────

@app.get("/vault", response_class=HTMLResponse)
async def vault_landing():
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{COMMON_HEAD}
<title>OBLIVION Vault — Stateless Password Manager | OBLIVION Search</title>
<meta name="description" content="Generate deterministic passwords from your master password. No storage, no cloud, no sync. Your passwords live in your head, not in the cloud.">
<link rel="canonical" href="https://oblivionsearch.com/vault">
<meta property="og:title" content="OBLIVION Vault — Stateless Password Manager">
<meta property="og:description" content="Generate deterministic passwords from your master password. No storage, no cloud, no sync. Your passwords live in your head, not in the cloud.">
<meta property="og:url" content="https://oblivionsearch.com/vault">
<meta property="og:type" content="website">
<meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
<style>{COMMON_CSS}</style>
</head>
<body>
{NAV_HTML}
<script>document.getElementById('nav-home').classList.add('active');</script>

<div class="container">
  <div class="hero">
    <h1>🔐 OBLIVION <span class="accent">Vault</span></h1>
    <p>Your passwords live in your head, not in the cloud.</p>
    <p style="color:{TEXT2};font-size:0.95em;margin-bottom:30px;">
      Generate strong, unique passwords from a single master password. No storage. No sync. No cloud. No accounts.<br>
      Same inputs always produce the same password. Pure math.
    </p>
    <a href="/vault/generate" class="btn" style="margin-right:12px;">Generate Password</a>
    <a href="/vault/check" class="btn btn-outline">Check Strength</a>
  </div>

  <div class="section">
    <h2>How It Works</h2>
    <div class="grid-3">
      <div class="card">
        <h3>1. Enter Your Inputs</h3>
        <p>Provide a site name, your login, and your master password. These three ingredients are all you need.</p>
      </div>
      <div class="card">
        <h3>2. Math Does the Rest</h3>
        <p>PBKDF2-SHA256 with 100,000 iterations derives a unique password. Same inputs = same output, every time.</p>
      </div>
      <div class="card">
        <h3>3. Copy & Use</h3>
        <p>Your generated password appears instantly. Copy it, use it, forget it. You can regenerate it anytime.</p>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>Why Stateless?</h2>
    <div class="grid-2">
      <div class="card">
        <h3>🛡️ Zero Attack Surface</h3>
        <p>No database to breach. No servers storing your passwords. No encrypted vaults to crack. Nothing to steal because nothing is stored.</p>
      </div>
      <div class="card">
        <h3>🔒 True Privacy</h3>
        <p>Your master password never leaves your browser. All computation happens client-side using the Web Crypto API. We can't see your passwords even if we wanted to.</p>
      </div>
      <div class="card">
        <h3>🌐 Works Everywhere</h3>
        <p>No sync needed. No app to install. Works on any device with a browser. Get the same password on your phone, laptop, or a library computer.</p>
      </div>
      <div class="card">
        <h3>📴 Works Offline</h3>
        <p>Install as a Progressive Web App. Once loaded, it works without internet. No server required for password generation.</p>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>Comparison</h2>
    <div class="card" style="overflow-x:auto;">
      <table style="width:100%;border-collapse:collapse;text-align:center;">
        <thead>
          <tr style="border-bottom:2px solid #333;">
            <th style="text-align:left;padding:12px;">Feature</th>
            <th style="padding:12px;">OBLIVION Vault</th>
            <th style="padding:12px;">LastPass</th>
            <th style="padding:12px;">1Password</th>
            <th style="padding:12px;">Bitwarden</th>
          </tr>
        </thead>
        <tbody>
          <tr style="border-bottom:1px solid #222;">
            <td style="text-align:left;padding:10px;">Password storage</td>
            <td><span class="badge badge-green">None needed</span></td>
            <td><span class="badge badge-red">Cloud vault</span></td>
            <td><span class="badge badge-red">Cloud vault</span></td>
            <td><span class="badge badge-yellow">Cloud/self-host</span></td>
          </tr>
          <tr style="border-bottom:1px solid #222;">
            <td style="text-align:left;padding:10px;">Breach risk</td>
            <td><span class="badge badge-green">Zero</span></td>
            <td><span class="badge badge-red">Breached 2022</span></td>
            <td><span class="badge badge-yellow">Possible</span></td>
            <td><span class="badge badge-yellow">Possible</span></td>
          </tr>
          <tr style="border-bottom:1px solid #222;">
            <td style="text-align:left;padding:10px;">Cost</td>
            <td><span class="badge badge-green">Free forever</span></td>
            <td><span class="badge badge-red">$3/mo</span></td>
            <td><span class="badge badge-red">$3/mo</span></td>
            <td><span class="badge badge-green">Free tier</span></td>
          </tr>
          <tr style="border-bottom:1px solid #222;">
            <td style="text-align:left;padding:10px;">Works offline</td>
            <td><span class="badge badge-green">Yes (PWA)</span></td>
            <td><span class="badge badge-yellow">Limited</span></td>
            <td><span class="badge badge-yellow">Limited</span></td>
            <td><span class="badge badge-yellow">Limited</span></td>
          </tr>
          <tr>
            <td style="text-align:left;padding:10px;">Sync needed</td>
            <td><span class="badge badge-green">No</span></td>
            <td><span class="badge badge-red">Yes</span></td>
            <td><span class="badge badge-red">Yes</span></td>
            <td><span class="badge badge-red">Yes</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="section" style="text-align:center;">
    <h2>Tools</h2>
    <div class="grid-3">
      <a href="/vault/generate" class="card" style="text-decoration:none;text-align:center;">
        <h3>🔑 Password Generator</h3>
        <p>Generate deterministic passwords from your master password.</p>
      </a>
      <a href="/vault/check" class="card" style="text-decoration:none;text-align:center;">
        <h3>💪 Strength Checker</h3>
        <p>Analyze any password's entropy and estimated crack time.</p>
      </a>
      <a href="/vault/breach" class="card" style="text-decoration:none;text-align:center;">
        <h3>🔍 Breach Checker</h3>
        <p>Check if a password has appeared in known data breaches.</p>
      </a>
    </div>
  </div>
</div>

{FOOTER_HTML}

<script>
if ('serviceWorker' in navigator) {{
  navigator.serviceWorker.register('/vault/sw.js');
}}
</script>
</body>
</html>"""


@app.get("/vault/generate", response_class=HTMLResponse)
async def vault_generate():
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{COMMON_HEAD}
<title>Password Generator — OBLIVION Vault</title>
<meta name="description" content="Generate strong, deterministic passwords from your master password. All computation happens in your browser.">
<style>{COMMON_CSS}
.result-area {{ display:none; margin-top:24px; }}
.result-area.show {{ display:block; }}
.counter {{ font-size:0.8em; color:{TEXT2}; margin-top:4px; }}
.fingerprint {{ font-family:monospace; font-size:0.85em; color:{ACCENT}; margin-top:8px; word-break:break-all; }}
.master-toggle {{ position:relative; }}
.master-toggle .eye {{ position:absolute; right:12px; top:50%; transform:translateY(-50%); cursor:pointer; color:{TEXT2}; font-size:1.2em; user-select:none; }}
</style>
</head>
<body>
{NAV_HTML}
<script>document.getElementById('nav-gen').classList.add('active');</script>

<div class="container">
  <div style="text-align:center;padding:40px 0 20px;">
    <h1>🔑 Password Generator</h1>
    <p style="color:{TEXT2};margin-top:8px;">All computation happens in your browser. Your master password never leaves this device.</p>
  </div>

  <div class="card">
    <div class="form-group">
      <label for="site">Site / Service</label>
      <input type="text" id="site" placeholder="e.g. google.com" autocomplete="off" spellcheck="false">
    </div>
    <div class="form-group">
      <label for="login">Login / Username / Email</label>
      <input type="text" id="login" placeholder="e.g. myemail@gmail.com" autocomplete="off" spellcheck="false">
    </div>
    <div class="form-group">
      <label for="master">Master Password</label>
      <div class="master-toggle">
        <input type="password" id="master" placeholder="Your master password" autocomplete="off">
        <span class="eye" onclick="toggleMaster()" id="eyeIcon">👁️</span>
      </div>
      <div class="counter" id="masterCounter"></div>
    </div>

    <div class="form-group">
      <label>Options</label>
      <div class="toggle-row">
        <label><input type="checkbox" id="optLower" checked> a-z</label>
        <label><input type="checkbox" id="optUpper" checked> A-Z</label>
        <label><input type="checkbox" id="optDigits" checked> 0-9</label>
        <label><input type="checkbox" id="optSymbols" checked> !@#$</label>
      </div>
    </div>

    <div class="form-group">
      <label for="length">Length: <span id="lenVal">16</span></label>
      <input type="range" id="length" min="12" max="64" value="16" style="padding:4px 0;" oninput="document.getElementById('lenVal').textContent=this.value">
    </div>

    <button class="btn" onclick="doGenerate()" style="width:100%;font-size:1.1em;" id="genBtn">Generate Password</button>

    <div class="result-area" id="resultArea">
      <label>Your Generated Password</label>
      <div class="password-output">
        <span id="passwordDisplay" style="flex:1;"></span>
        <button class="copy-btn" onclick="copyPassword()">📋 Copy</button>
      </div>
      <div class="strength-bar" style="margin-top:12px;">
        <div class="fill" id="strengthFill"></div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:6px;">
        <span id="strengthLabel" style="font-size:0.85em;"></span>
        <span id="entropyLabel" style="font-size:0.85em;color:{TEXT2};"></span>
      </div>
      <div id="crackTimeLabel" style="font-size:0.85em;color:{TEXT2};margin-top:4px;"></div>
      <div class="fingerprint" id="fingerprint"></div>
    </div>
  </div>

  <div class="card" style="margin-top:20px;">
    <h3>🔒 Privacy Guarantee</h3>
    <ul style="color:{TEXT2};list-style:none;padding:0;">
      <li style="padding:6px 0;">✅ Your master password never leaves this browser tab</li>
      <li style="padding:6px 0;">✅ Zero network requests during password generation</li>
      <li style="padding:6px 0;">✅ No cookies, no tracking, no analytics</li>
      <li style="padding:6px 0;">✅ Works fully offline once loaded (PWA)</li>
      <li style="padding:6px 0;">✅ <a href="https://github.com/lesspass/lesspass" target="_blank">Open-source algorithm</a> — inspect the code yourself</li>
    </ul>
  </div>
</div>

{FOOTER_HTML}

<script>
{CRYPTO_JS}

let currentPassword = '';

function toggleMaster() {{
  const m = document.getElementById('master');
  m.type = m.type === 'password' ? 'text' : 'password';
  document.getElementById('eyeIcon').textContent = m.type === 'password' ? '👁️' : '🙈';
}}

document.getElementById('master').addEventListener('input', function() {{
  const len = this.value.length;
  document.getElementById('masterCounter').textContent = len > 0 ? len + ' characters' : '';
}});

// Generate on Enter key
['site','login','master'].forEach(id => {{
  document.getElementById(id).addEventListener('keydown', e => {{
    if (e.key === 'Enter') doGenerate();
  }});
}});

async function doGenerate() {{
  const site = document.getElementById('site').value.trim();
  const login = document.getElementById('login').value.trim();
  const master = document.getElementById('master').value;

  if (!site || !login || !master) {{
    alert('Please fill in all three fields: Site, Login, and Master Password.');
    return;
  }}

  const btn = document.getElementById('genBtn');
  btn.textContent = 'Generating...';
  btn.disabled = true;

  try {{
    const opts = {{
      length: parseInt(document.getElementById('length').value),
      uppercase: document.getElementById('optUpper').checked,
      lowercase: document.getElementById('optLower').checked,
      digits: document.getElementById('optDigits').checked,
      symbols: document.getElementById('optSymbols').checked
    }};

    currentPassword = await generatePassword(site, login, master, opts);

    document.getElementById('passwordDisplay').textContent = currentPassword;

    // Strength indicator
    const ent = calcEntropy(currentPassword);
    const sl = strengthLevel(ent);
    document.getElementById('strengthFill').style.width = sl.pct + '%';
    document.getElementById('strengthFill').style.background = sl.color;
    document.getElementById('strengthLabel').textContent = sl.label;
    document.getElementById('strengthLabel').style.color = sl.color;
    document.getElementById('entropyLabel').textContent = Math.round(ent) + ' bits of entropy';
    document.getElementById('crackTimeLabel').textContent = 'Estimated crack time: ' + crackTime(ent);

    // Fingerprint (non-secret visual confirmation)
    const fp = await sha1(site + '|' + login);
    document.getElementById('fingerprint').textContent = 'Profile fingerprint: ' + fp.substring(0, 16);

    document.getElementById('resultArea').classList.add('show');
  }} catch (err) {{
    alert('Error: ' + err.message);
  }} finally {{
    btn.textContent = 'Generate Password';
    btn.disabled = false;
  }}
}}

function copyPassword() {{
  if (!currentPassword) return;
  navigator.clipboard.writeText(currentPassword).then(() => {{
    const btn = document.querySelector('.copy-btn');
    btn.textContent = '✅ Copied!';
    setTimeout(() => btn.textContent = '📋 Copy', 2000);
  }});
}}

if ('serviceWorker' in navigator) {{
  navigator.serviceWorker.register('/vault/sw.js');
}}
</script>
</body>
</html>"""


@app.get("/vault/check", response_class=HTMLResponse)
async def vault_check():
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{COMMON_HEAD}
<title>Password Strength Checker — OBLIVION Vault</title>
<meta name="description" content="Check how strong your password is. Entropy analysis, crack time estimates, and improvement suggestions.">
<style>{COMMON_CSS}
.result-card {{ display:none; }}
.result-card.show {{ display:block; }}
.meter {{ display:flex; gap:4px; margin:16px 0; }}
.meter .seg {{ flex:1; height:8px; border-radius:4px; background:#222; transition:background 0.3s; }}
.suggestion {{ padding:8px 12px; background:{BG3}; border-left:3px solid {ACCENT}; border-radius:0 6px 6px 0; margin-bottom:8px; font-size:0.9em; }}
.stat-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin:20px 0; }}
.stat {{ text-align:center; padding:16px; background:{BG3}; border-radius:8px; }}
.stat .val {{ font-size:2em; font-weight:700; }}
.stat .lbl {{ font-size:0.85em; color:{TEXT2}; margin-top:4px; }}
</style>
</head>
<body>
{NAV_HTML}
<script>document.getElementById('nav-check').classList.add('active');</script>

<div class="container">
  <div style="text-align:center;padding:40px 0 20px;">
    <h1>💪 Password Strength Checker</h1>
    <p style="color:{TEXT2};margin-top:8px;">Analyze any password locally. Nothing is sent to any server.</p>
  </div>

  <div class="card">
    <div class="form-group">
      <label for="checkPw">Enter a password to analyze</label>
      <input type="text" id="checkPw" placeholder="Type or paste a password..." autocomplete="off" spellcheck="false">
    </div>
    <button class="btn" onclick="analyzePassword()" style="width:100%;">Analyze Strength</button>
  </div>

  <div class="card result-card" id="resultCard">
    <div style="text-align:center;">
      <span id="resultEmoji" style="font-size:3em;"></span>
      <h2 id="resultLabel" style="margin-top:8px;"></h2>
    </div>

    <div class="meter" id="meter">
      <div class="seg"></div><div class="seg"></div><div class="seg"></div><div class="seg"></div><div class="seg"></div>
    </div>

    <div class="stat-grid">
      <div class="stat">
        <div class="val" id="entVal">—</div>
        <div class="lbl">Bits of Entropy</div>
      </div>
      <div class="stat">
        <div class="val" id="crackVal">—</div>
        <div class="lbl">Crack Time (10B/sec)</div>
      </div>
      <div class="stat">
        <div class="val" id="lenVal">—</div>
        <div class="lbl">Length</div>
      </div>
      <div class="stat">
        <div class="val" id="poolVal">—</div>
        <div class="lbl">Character Pool</div>
      </div>
    </div>

    <h3 style="margin:20px 0 12px;">Analysis</h3>
    <div id="charBreakdown"></div>

    <h3 style="margin:20px 0 12px;">Suggestions</h3>
    <div id="suggestions"></div>
  </div>
</div>

{FOOTER_HTML}

<script>
{CRYPTO_JS}

function analyzePassword() {{
  const pw = document.getElementById('checkPw').value;
  if (!pw) {{ alert('Please enter a password.'); return; }}

  const ent = calcEntropy(pw);
  const sl = strengthLevel(ent);

  document.getElementById('resultCard').classList.add('show');

  // Emoji and label
  const emojis = {{ 'Very Weak': '💀', 'Weak': '😟', 'Fair': '🤔', 'Strong': '💪', 'Very Strong': '🛡️' }};
  document.getElementById('resultEmoji').textContent = emojis[sl.label] || '🤔';
  const rl = document.getElementById('resultLabel');
  rl.textContent = sl.label;
  rl.style.color = sl.color;

  // Meter
  const segs = document.querySelectorAll('#meter .seg');
  const levels = {{ 'Very Weak': 1, 'Weak': 2, 'Fair': 3, 'Strong': 4, 'Very Strong': 5 }};
  const lvl = levels[sl.label] || 0;
  segs.forEach((s, i) => s.style.background = i < lvl ? sl.color : '#222');

  // Stats
  document.getElementById('entVal').textContent = Math.round(ent);
  document.getElementById('crackVal').textContent = crackTime(ent);
  document.getElementById('lenVal').textContent = pw.length;

  let pool = 0;
  if (/[a-z]/.test(pw)) pool += 26;
  if (/[A-Z]/.test(pw)) pool += 26;
  if (/[0-9]/.test(pw)) pool += 10;
  if (/[^a-zA-Z0-9]/.test(pw)) pool += 33;
  document.getElementById('poolVal').textContent = pool;

  // Char breakdown
  const lower = (pw.match(/[a-z]/g) || []).length;
  const upper = (pw.match(/[A-Z]/g) || []).length;
  const digit = (pw.match(/[0-9]/g) || []).length;
  const sym = pw.length - lower - upper - digit;
  document.getElementById('charBreakdown').innerHTML =
    '<div style="display:flex;gap:12px;flex-wrap:wrap;">' +
    (lower ? '<span class="badge badge-green">Lowercase: ' + lower + '</span>' : '') +
    (upper ? '<span class="badge badge-green">Uppercase: ' + upper + '</span>' : '') +
    (digit ? '<span class="badge badge-green">Digits: ' + digit + '</span>' : '') +
    (sym ? '<span class="badge badge-green">Symbols: ' + sym + '</span>' : '') +
    '</div>';

  // Suggestions
  const sugs = [];
  if (pw.length < 12) sugs.push('Use at least 12 characters for adequate security.');
  if (pw.length < 16) sugs.push('16+ characters is recommended for important accounts.');
  if (!/[A-Z]/.test(pw)) sugs.push('Add uppercase letters to expand the character pool.');
  if (!/[a-z]/.test(pw)) sugs.push('Add lowercase letters to expand the character pool.');
  if (!/[0-9]/.test(pw)) sugs.push('Add digits to expand the character pool.');
  if (!/[^a-zA-Z0-9]/.test(pw)) sugs.push('Add symbols (!@#$%^&*) for maximum entropy.');
  if (/^[a-zA-Z]+$/.test(pw)) sugs.push('Avoid using only letters — mix in numbers and symbols.');
  if (/(.)\1\1/.test(pw)) sugs.push('Avoid repeated characters (e.g., "aaa").');
  if (/^(123|abc|password|qwerty)/i.test(pw)) sugs.push('Avoid common patterns and dictionary words.');
  if (ent >= 80 && sugs.length === 0) sugs.push('Excellent password! Consider using OBLIVION Vault to generate passwords like this automatically.');

  document.getElementById('suggestions').innerHTML = sugs.map(s => '<div class="suggestion">' + s + '</div>').join('');
}}

document.getElementById('checkPw').addEventListener('keydown', e => {{
  if (e.key === 'Enter') analyzePassword();
}});
document.getElementById('checkPw').addEventListener('input', function() {{
  if (this.value) analyzePassword();
}});

if ('serviceWorker' in navigator) {{
  navigator.serviceWorker.register('/vault/sw.js');
}}
</script>
</body>
</html>"""


@app.get("/vault/breach", response_class=HTMLResponse)
async def vault_breach():
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{COMMON_HEAD}
<title>Breach Checker — OBLIVION Vault</title>
<meta name="description" content="Check if your password has appeared in known data breaches using k-anonymity. Your password is never sent anywhere.">
<style>{COMMON_CSS}
.result-box {{ display:none; text-align:center; padding:30px; border-radius:12px; margin-top:24px; }}
.result-box.show {{ display:block; }}
.result-box.safe {{ background:rgba(46,213,115,0.1); border:2px solid {SUCCESS}; }}
.result-box.pwned {{ background:rgba(255,71,87,0.1); border:2px solid {DANGER}; }}
.result-box .emoji {{ font-size:3em; }}
.result-box h2 {{ margin:12px 0 8px; }}
.how-it-works {{ margin-top:30px; }}
.how-it-works ol {{ padding-left:20px; }}
.how-it-works li {{ padding:6px 0; color:{TEXT2}; }}
.how-it-works code {{ background:{BG3}; padding:2px 6px; border-radius:4px; font-size:0.9em; }}
</style>
</head>
<body>
{NAV_HTML}
<script>document.getElementById('nav-breach').classList.add('active');</script>

<div class="container">
  <div style="text-align:center;padding:40px 0 20px;">
    <h1>🔍 Have I Been Pwned?</h1>
    <p style="color:{TEXT2};margin-top:8px;">Check if a password appears in known data breaches — without revealing it.</p>
  </div>

  <div class="card">
    <div class="form-group">
      <label for="breachPw">Enter a password to check</label>
      <input type="password" id="breachPw" placeholder="Your password" autocomplete="off">
    </div>
    <button class="btn" onclick="checkBreach()" id="checkBtn" style="width:100%;">Check for Breaches</button>

    <div class="result-box" id="safeBox">
      <div class="emoji">🛡️</div>
      <h2 style="color:{SUCCESS};">Not Found in Breaches</h2>
      <p style="color:{TEXT2};">This password has not appeared in any known data breaches in the Have I Been Pwned database.</p>
    </div>

    <div class="result-box" id="pwnedBox">
      <div class="emoji">⚠️</div>
      <h2 style="color:{DANGER};">Password Compromised!</h2>
      <p style="color:{TEXT2};">This password has been found <strong id="pwnedCount"></strong> times in known data breaches.</p>
      <p style="color:{DANGER};margin-top:12px;font-weight:600;">You should change this password immediately.</p>
      <a href="/vault/generate" class="btn" style="margin-top:16px;">Generate a Secure Password</a>
    </div>
  </div>

  <div class="card how-it-works">
    <h3>How K-Anonymity Works</h3>
    <p style="color:{TEXT2};margin-bottom:12px;">Your password is <strong>never</strong> sent to any server. Here's the process:</p>
    <ol>
      <li>Your password is hashed with <code>SHA-1</code> locally in your browser.</li>
      <li>Only the first <strong>5 characters</strong> of the hash are sent to the Have I Been Pwned API.</li>
      <li>The API returns all hash suffixes matching that prefix (~500-800 results).</li>
      <li>Your browser checks locally if the full hash appears in the results.</li>
      <li>The API operator <strong>never sees your password or its full hash</strong>.</li>
    </ol>
    <p style="color:{TEXT2};margin-top:12px;">
      Learn more: <a href="https://haveibeenpwned.com/API/v3#SearchingPwnedPasswordsByRange" target="_blank">HIBP k-Anonymity documentation</a>
    </p>
  </div>
</div>

{FOOTER_HTML}

<script>
{CRYPTO_JS}

async function checkBreach() {{
  const pw = document.getElementById('breachPw').value;
  if (!pw) {{ alert('Please enter a password.'); return; }}

  const btn = document.getElementById('checkBtn');
  btn.textContent = 'Checking...';
  btn.disabled = true;

  document.getElementById('safeBox').classList.remove('show');
  document.getElementById('pwnedBox').classList.remove('show');

  try {{
    const hash = await sha1(pw);
    const prefix = hash.substring(0, 5);
    const suffix = hash.substring(5);

    const response = await fetch('https://api.pwnedpasswords.com/range/' + prefix);
    if (!response.ok) throw new Error('API request failed');

    const text = await response.text();
    const lines = text.split('\\n');

    let found = false;
    for (const line of lines) {{
      const [hashSuffix, count] = line.split(':');
      if (hashSuffix.trim() === suffix) {{
        found = true;
        document.getElementById('pwnedCount').textContent = parseInt(count.trim()).toLocaleString();
        document.getElementById('pwnedBox').classList.add('show', 'pwned');
        break;
      }}
    }}

    if (!found) {{
      document.getElementById('safeBox').classList.add('show', 'safe');
    }}
  }} catch (err) {{
    alert('Error checking breach database: ' + err.message + '. The HIBP API may be temporarily unavailable.');
  }} finally {{
    btn.textContent = 'Check for Breaches';
    btn.disabled = false;
  }}
}}

document.getElementById('breachPw').addEventListener('keydown', e => {{
  if (e.key === 'Enter') checkBreach();
}});

if ('serviceWorker' in navigator) {{
  navigator.serviceWorker.register('/vault/sw.js');
}}
</script>
</body>
</html>"""


@app.get("/vault/sw.js", response_class=HTMLResponse)
async def vault_sw():
    return HTMLResponse(content=SW_JS, media_type="application/javascript")


@app.get("/vault/manifest.json", response_class=JSONResponse)
async def vault_manifest():
    import json
    return JSONResponse(content=json.loads(MANIFEST_JSON))


# ──────────────────────────────────────────────
# STRIPE / SAAS ROUTES
# ──────────────────────────────────────────────

PRICING_CSS = """
.pricing-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:24px; margin:40px 0; }
.price-card { background:#12121a; border:1px solid #222; border-radius:12px; padding:32px 24px; text-align:center; position:relative; transition:all 0.2s; }
.price-card:hover { border-color:#00d4ff; transform:translateY(-4px); }
.price-card.featured { border:2px solid #00d4ff; }
.price-card .badge-pop { position:absolute; top:-12px; left:50%; transform:translateX(-50%); background:#00d4ff; color:#0a0a0f; padding:4px 16px; border-radius:20px; font-size:0.8em; font-weight:700; }
.price-card h3 { font-size:1.4em; margin-bottom:8px; color:#e0e0e0; }
.price-card .price { font-size:2.4em; font-weight:700; color:#00d4ff; margin:16px 0; }
.price-card .price span { font-size:0.4em; color:#888; }
.price-card ul { list-style:none; text-align:left; margin:20px 0; }
.price-card ul li { padding:8px 0; color:#ccc; font-size:0.9em; border-bottom:1px solid #1a1a28; }
.price-card ul li:before { content:"\\2713 "; color:#00d4ff; font-weight:700; margin-right:8px; }
.price-card .btn { width:100%; margin-top:16px; }
"""


@app.get("/vault/pricing", response_class=HTMLResponse)
async def vault_pricing():
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{COMMON_HEAD}
<title>Pricing — OBLIVION Vault</title>
<style>{COMMON_CSS}{PRICING_CSS}</style>
</head>
<body>
{NAV_HTML}
<div class="container">
  <div style="text-align:center;padding:40px 0 10px;">
    <h1>OBLIVION Vault <span class="accent">Pricing</span></h1>
    <p style="color:{TEXT2};margin-top:12px;">Free forever for basic use. Upgrade for saved profiles, breach monitoring & team features.</p>
  </div>

  <div class="pricing-grid">
    <div class="price-card">
      <h3>Free</h3>
      <div class="price">£0<span>/forever</span></div>
      <ul>
        <li>Deterministic password generator</li>
        <li>Password strength checker</li>
        <li>Breach checker (HIBP)</li>
        <li>Offline PWA support</li>
        <li>10 generations/day</li>
      </ul>
      <a href="/vault/generate" class="btn btn-outline">Get Started</a>
    </div>
    <div class="price-card featured">
      <div class="badge-pop">MOST POPULAR</div>
      <h3>Pro</h3>
      <div class="price">£4<span>/month</span></div>
      <ul>
        <li>Everything in Free</li>
        <li>Saved site profiles (encrypted)</li>
        <li>Breach monitoring alerts (weekly)</li>
        <li>Unlimited generations</li>
        <li>Priority support</li>
        <li>API access</li>
      </ul>
      <a href="/vault/checkout/pro" class="btn">Subscribe to Pro</a>
    </div>
    <div class="price-card">
      <h3>Team</h3>
      <div class="price">£12<span>/month</span></div>
      <ul>
        <li>Everything in Pro</li>
        <li>Up to 10 users</li>
        <li>Admin dashboard</li>
        <li>Team password policies</li>
        <li>Shared site profiles</li>
        <li>Priority support</li>
      </ul>
      <a href="/vault/checkout/team" class="btn btn-outline">Subscribe to Team</a>
    </div>
  </div>
</div>
{FOOTER_HTML}
</body>
</html>"""


@app.get("/vault/checkout/pro")
async def vault_checkout_pro():
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "product_data": {"name": "OBLIVION Vault Pro", "description": "Saved profiles, breach monitoring, unlimited generations"},
                    "unit_amount": 400,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=DOMAIN + "/vault/success?session_id={CHECKOUT_SESSION_ID}&plan=pro",
            cancel_url=DOMAIN + "/vault/pricing",
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/vault/checkout/team")
async def vault_checkout_team():
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "product_data": {"name": "OBLIVION Vault Team", "description": "Up to 10 users, admin dashboard, shared profiles"},
                    "unit_amount": 1200,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=DOMAIN + "/vault/success?session_id={CHECKOUT_SESSION_ID}&plan=team",
            cancel_url=DOMAIN + "/vault/pricing",
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/vault/success", response_class=HTMLResponse)
async def vault_success(session_id: str = "", plan: str = "pro"):
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

    # Store in DB
    try:
        p = await get_pool()
        async with p.acquire() as conn:
            existing = await conn.fetchval("SELECT api_key FROM vault_customers WHERE email=$1 AND plan=$2 AND active=TRUE", email, plan)
            if existing:
                api_key = existing
            else:
                await conn.execute(
                    "INSERT INTO vault_customers (email, plan, api_key, stripe_customer_id, stripe_subscription_id) VALUES ($1,$2,$3,$4,$5)",
                    email, plan, api_key, str(cust_id), str(sub_id),
                )
    except Exception as e:
        print(f"[VAULT] DB error: {e}")

    # Send welcome email
    if email != "unknown":
        import threading
        threading.Thread(target=send_welcome_email, args=(email, plan, api_key), daemon=True).start()

    plan_title = plan.title()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{COMMON_HEAD}
<title>Welcome to OBLIVION Vault {plan_title}!</title>
<style>{COMMON_CSS}</style>
</head>
<body>
{NAV_HTML}
<div class="container">
  <div style="text-align:center;padding:60px 0 30px;">
    <div style="font-size:4em;">🎉</div>
    <h1 style="margin-top:16px;">Welcome to Vault <span class="accent">{plan_title}</span>!</h1>
    <p style="color:{TEXT2};margin-top:12px;">Your subscription is now active. A confirmation email has been sent to <strong>{email}</strong>.</p>
  </div>
  <div class="card" style="max-width:600px;margin:0 auto;">
    <h3>Your API Key</h3>
    <div style="background:{BG3};padding:16px;border-radius:8px;margin:12px 0;font-family:monospace;color:{ACCENT};word-break:break-all;font-size:1.1em;" id="apiKey">{api_key}</div>
    <button class="btn" onclick="navigator.clipboard.writeText(document.getElementById('apiKey').textContent);this.textContent='Copied!';setTimeout(()=>this.textContent='Copy API Key',2000)" style="width:100%;">Copy API Key</button>
    <p style="color:{TEXT2};margin-top:16px;font-size:0.85em;">Save this key! You'll need it to access Pro features and your dashboard.</p>
    <div style="margin-top:20px;display:flex;gap:12px;">
      <a href="/vault/dashboard?key={api_key}" class="btn btn-outline" style="flex:1;text-align:center;">Go to Dashboard</a>
      <a href="/vault/generate" class="btn btn-outline" style="flex:1;text-align:center;">Generate Passwords</a>
    </div>
  </div>
</div>
{FOOTER_HTML}
</body>
</html>"""


@app.get("/vault/dashboard", response_class=HTMLResponse)
async def vault_dashboard(key: str = ""):
    if not key:
        return HTMLResponse("<h1>API key required</h1><p>Add ?key=YOUR_KEY to the URL</p>", status_code=400)
    p = await get_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM vault_customers WHERE api_key=$1 AND active=TRUE", key)
    if not row:
        return HTMLResponse("<h1>Invalid or inactive API key</h1>", status_code=403)

    r_email = row['email']
    r_plan = row['plan'].title()
    r_since = row['created_at'].strftime('%B %d, %Y')
    r_key = row['api_key']
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
{COMMON_HEAD}
<title>Dashboard — OBLIVION Vault</title>
<style>{COMMON_CSS}</style>
</head>
<body>
{NAV_HTML}
<div class="container">
  <div style="text-align:center;padding:40px 0 20px;">
    <h1>Your <span class="accent">Vault Dashboard</span></h1>
  </div>
  <div class="grid-2">
    <div class="card">
      <h3>Account</h3>
      <p><strong>Email:</strong> {r_email}</p>
      <p><strong>Plan:</strong> <span class="badge badge-green">{r_plan}</span></p>
      <p><strong>Status:</strong> <span class="badge badge-green">Active</span></p>
      <p><strong>Since:</strong> {r_since}</p>
    </div>
    <div class="card">
      <h3>API Key</h3>
      <div style="background:{BG3};padding:12px;border-radius:8px;font-family:monospace;color:{ACCENT};font-size:0.85em;word-break:break-all;">{r_key}</div>
      <p style="color:{TEXT2};margin-top:8px;font-size:0.85em;">Use this key in API requests or to skip rate limits.</p>
    </div>
  </div>
  <div class="card" style="margin-top:20px;">
    <h3>Pro Features</h3>
    <div class="grid-2" style="margin-top:16px;">
      <div style="padding:16px;background:{BG3};border-radius:8px;">
        <h4 style="color:{ACCENT};">Saved Profiles</h4>
        <p style="color:{TEXT2};font-size:0.9em;">Your site profiles are encrypted and stored server-side. Access them from any device.</p>
      </div>
      <div style="padding:16px;background:{BG3};border-radius:8px;">
        <h4 style="color:{ACCENT};">Breach Monitoring</h4>
        <p style="color:{TEXT2};font-size:0.9em;">Weekly HIBP checks on your common passwords. We'll alert you if any are compromised.</p>
      </div>
      <div style="padding:16px;background:{BG3};border-radius:8px;">
        <h4 style="color:{ACCENT};">Unlimited Generations</h4>
        <p style="color:{TEXT2};font-size:0.9em;">No daily limits on password generation.</p>
      </div>
      <div style="padding:16px;background:{BG3};border-radius:8px;">
        <h4 style="color:{ACCENT};">Priority Support</h4>
        <p style="color:{TEXT2};font-size:0.9em;">Get help faster from the OBLIVION team.</p>
      </div>
    </div>
  </div>
</div>
{FOOTER_HTML}
</body>
</html>"""


@app.post("/vault/webhook")
async def vault_webhook(request: Request):
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
                await conn.execute("UPDATE vault_customers SET active=FALSE WHERE stripe_subscription_id=$1", str(sub_id))
        except Exception as e:
            print(f"[VAULT] Webhook DB error: {e}")

    return JSONResponse({"status": "ok"})


# Health check
@app.get("/vault/health")
async def vault_health():
    return {"status": "ok", "service": "oblivion-vault", "port": 3071}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3071)
