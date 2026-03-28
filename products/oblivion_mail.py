#!/usr/bin/env python3
"""
OBLIVION Mail — Encrypted Email Service
Port 3055 — Waitlist / Coming Soon page
Powered by Lavabit/Magma technology
"""

import json
import os
import datetime
from pathlib import Path
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

app = FastAPI(title="OBLIVION Mail", docs_url=None, redoc_url=None)

DATA_DIR = Path("/opt/oblivionzone/data")
WAITLIST_FILE = DATA_DIR / "mail_waitlist.json"

# Ensure data dir exists
DATA_DIR.mkdir(parents=True, exist_ok=True)
if not WAITLIST_FILE.exists():
    WAITLIST_FILE.write_text("[]")


def load_waitlist():
    try:
        return json.loads(WAITLIST_FILE.read_text())
    except Exception:
        return []


def save_waitlist(entries):
    WAITLIST_FILE.write_text(json.dumps(entries, indent=2))


PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OBLIVION Mail — Encrypted Email</title>
<meta name="description" content="OBLIVION Mail: End-to-end encrypted email that can't be read by anyone but you. Zero-access encryption, no tracking. Powered by Lavabit technology.">
<meta name="robots" content="index, follow">
<link rel="canonical" href="https://oblivionsearch.com/mail">
<meta property="og:title" content="OBLIVION Mail — Encrypted Email">
<meta property="og:description" content="End-to-end encrypted email. Zero-access encryption. No tracking.">
<meta property="og:type" content="website">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
    --bg: #0a0a0f;
    --bg2: #12121a;
    --bg3: #1a1a2e;
    --accent: #00d4ff;
    --accent2: #7c3aed;
    --accent3: #06b6d4;
    --text: #e2e8f0;
    --text-dim: #94a3b8;
    --border: #1e293b;
    --success: #10b981;
    --glow: rgba(0, 212, 255, 0.15);
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    overflow-x: hidden;
}

/* Animated background */
.bg-grid {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background-image:
        linear-gradient(rgba(0, 212, 255, 0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(0, 212, 255, 0.03) 1px, transparent 1px);
    background-size: 60px 60px;
    z-index: 0;
}

.bg-glow {
    position: fixed;
    top: -200px; left: 50%;
    transform: translateX(-50%);
    width: 800px; height: 800px;
    background: radial-gradient(circle, rgba(0, 212, 255, 0.08) 0%, transparent 70%);
    z-index: 0;
    animation: pulse 8s ease-in-out infinite;
}

@keyframes pulse {
    0%, 100% { opacity: 0.5; transform: translateX(-50%) scale(1); }
    50% { opacity: 1; transform: translateX(-50%) scale(1.1); }
}

.container {
    position: relative;
    z-index: 1;
    max-width: 960px;
    margin: 0 auto;
    padding: 0 24px;
}

/* Nav */
nav {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 24px 0;
    border-bottom: 1px solid var(--border);
}

.logo {
    font-size: 1.3rem;
    font-weight: 800;
    letter-spacing: 3px;
    text-transform: uppercase;
    color: #fff;
}
.logo span { color: var(--accent); }

.nav-links a {
    color: var(--text-dim);
    text-decoration: none;
    font-size: 0.9rem;
    margin-left: 24px;
    transition: color 0.2s;
}
.nav-links a:hover { color: var(--accent); }

/* Hero */
.hero {
    text-align: center;
    padding: 100px 0 60px;
}

.badge {
    display: inline-block;
    background: rgba(0, 212, 255, 0.1);
    border: 1px solid rgba(0, 212, 255, 0.25);
    color: var(--accent);
    padding: 8px 20px;
    border-radius: 100px;
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 32px;
}

h1 {
    font-size: clamp(2.5rem, 6vw, 4rem);
    font-weight: 800;
    line-height: 1.1;
    margin-bottom: 24px;
    background: linear-gradient(135deg, #fff 0%, var(--accent) 50%, var(--accent2) 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}

.hero p {
    font-size: 1.25rem;
    color: var(--text-dim);
    max-width: 600px;
    margin: 0 auto 48px;
    line-height: 1.7;
}

/* Signup form */
.signup-box {
    max-width: 520px;
    margin: 0 auto 32px;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 40px;
}

.signup-box h2 {
    font-size: 1.1rem;
    margin-bottom: 8px;
    color: #fff;
}
.signup-box .sub {
    color: var(--text-dim);
    font-size: 0.9rem;
    margin-bottom: 24px;
}

.form-row {
    display: flex;
    gap: 12px;
}

.form-row input[type="email"] {
    flex: 1;
    padding: 14px 18px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: #fff;
    font-size: 1rem;
    outline: none;
    transition: border-color 0.2s;
}
.form-row input[type="email"]:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--glow);
}

.btn-primary {
    padding: 14px 28px;
    border-radius: 10px;
    border: none;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    color: #fff;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    transition: transform 0.15s, box-shadow 0.15s;
}
.btn-primary:hover {
    transform: translateY(-1px);
    box-shadow: 0 8px 30px rgba(0, 212, 255, 0.3);
}

.privacy-note {
    font-size: 0.8rem;
    color: var(--text-dim);
    margin-top: 14px;
    text-align: center;
}

.counter {
    text-align: center;
    color: var(--text-dim);
    font-size: 0.9rem;
    margin-bottom: 80px;
}
.counter strong { color: var(--accent); }

/* Success message */
.msg-success {
    display: none;
    text-align: center;
    padding: 20px;
    color: var(--success);
    font-weight: 600;
}

/* Features */
.features {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 24px;
    margin-bottom: 80px;
}

.feature {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 32px;
    transition: border-color 0.3s, transform 0.2s;
}
.feature:hover {
    border-color: rgba(0, 212, 255, 0.3);
    transform: translateY(-3px);
}

.feature-icon {
    width: 48px; height: 48px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.4rem;
    margin-bottom: 20px;
}
.fi-1 { background: rgba(0, 212, 255, 0.1); color: var(--accent); }
.fi-2 { background: rgba(124, 58, 237, 0.1); color: var(--accent2); }
.fi-3 { background: rgba(16, 185, 129, 0.1); color: var(--success); }
.fi-4 { background: rgba(251, 191, 36, 0.1); color: #fbbf24; }
.fi-5 { background: rgba(239, 68, 68, 0.1); color: #ef4444; }
.fi-6 { background: rgba(6, 182, 212, 0.1); color: var(--accent3); }

.feature h3 {
    font-size: 1.05rem;
    margin-bottom: 10px;
    color: #fff;
}
.feature p {
    font-size: 0.9rem;
    color: var(--text-dim);
    line-height: 1.6;
}

/* Tech section */
.tech-section {
    text-align: center;
    padding: 60px 0;
    border-top: 1px solid var(--border);
    margin-bottom: 60px;
}

.tech-section h2 {
    font-size: 1.8rem;
    margin-bottom: 16px;
    color: #fff;
}
.tech-section .desc {
    color: var(--text-dim);
    max-width: 600px;
    margin: 0 auto 40px;
    line-height: 1.7;
}

.tech-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    max-width: 700px;
    margin: 0 auto;
}

.tech-item {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
    font-size: 0.9rem;
    color: var(--text-dim);
}
.tech-item strong {
    display: block;
    color: #fff;
    margin-bottom: 4px;
}

/* Comparison */
.comparison {
    text-align: center;
    padding: 60px 0;
    border-top: 1px solid var(--border);
    margin-bottom: 60px;
}
.comparison h2 {
    font-size: 1.8rem;
    margin-bottom: 32px;
    color: #fff;
}

.comp-table {
    max-width: 700px;
    margin: 0 auto;
    border-collapse: collapse;
    width: 100%;
    font-size: 0.9rem;
}
.comp-table th, .comp-table td {
    padding: 14px 18px;
    text-align: left;
    border-bottom: 1px solid var(--border);
}
.comp-table th {
    color: var(--text-dim);
    font-weight: 600;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 1px;
}
.comp-table td:first-child { color: #fff; font-weight: 500; }
.yes { color: var(--success); }
.no { color: #ef4444; }
.partial { color: #fbbf24; }
.highlight-row td { background: rgba(0, 212, 255, 0.04); }

/* Footer */
footer {
    text-align: center;
    padding: 40px 0;
    border-top: 1px solid var(--border);
    color: var(--text-dim);
    font-size: 0.85rem;
}
footer a {
    color: var(--accent);
    text-decoration: none;
}

@media (max-width: 600px) {
    .form-row { flex-direction: column; }
    .btn-primary { width: 100%; }
    .nav-links { display: none; }
}
</style>
</head>
<body>
<div class="bg-grid"></div>
<div class="bg-glow"></div>

<div class="container">
<nav>
    <div class="logo">OBLIVION <span>MAIL</span></div>
    <div class="nav-links">
        <a href="https://oblivionsearch.com">Search</a>
        <a href="#features">Features</a>
        <a href="#technology">Technology</a>
        <a href="#compare">Compare</a>
    </div>
</nav>

<section class="hero">
    <div class="badge">Coming Soon</div>
    <h1>Email That Only<br>You Can Read</h1>
    <p>OBLIVION Mail uses zero-access encryption so your messages can never be read by anyone but you. Not us. Not governments. Not hackers. Nobody.</p>

    <div class="signup-box">
        <h2>Join the Waitlist</h2>
        <p class="sub">Be the first to get an @oblivionmail.com address</p>
        <form id="waitlistForm">
            <div class="form-row">
                <input type="email" id="emailInput" placeholder="you@example.com" required>
                <button type="submit" class="btn-primary">Join Waitlist</button>
            </div>
        </form>
        <div class="msg-success" id="successMsg">You're on the list! We'll notify you at launch.</div>
        <p class="privacy-note">No spam. We'll only email you when OBLIVION Mail launches.</p>
    </div>

    <div class="counter"><strong id="waitlistCount">COUNTER</strong> people on the waitlist</div>
</section>

<section id="features">
<div class="features">
    <div class="feature">
        <div class="feature-icon fi-1">&#128274;</div>
        <h3>End-to-End Encryption</h3>
        <p>Every message is encrypted on your device before it leaves. Even we cannot read your emails. Your keys, your data.</p>
    </div>
    <div class="feature">
        <div class="feature-icon fi-2">&#128683;</div>
        <h3>Zero-Access Encryption</h3>
        <p>Your mailbox is encrypted with your password. We physically cannot access your stored messages, even with a court order.</p>
    </div>
    <div class="feature">
        <div class="feature-icon fi-3">&#9989;</div>
        <h3>No Tracking, No Scanning</h3>
        <p>We don't scan your emails. We don't track you. Privacy is the product, not you.</p>
    </div>
    <div class="feature">
        <div class="feature-icon fi-4">&#128296;</div>
        <h3>Open Source</h3>
        <p>Built on the Lavabit/Magma codebase. Fully auditable. Transparency builds trust.</p>
    </div>
    <div class="feature">
        <div class="feature-icon fi-5">&#128165;</div>
        <h3>Self-Destructing Messages</h3>
        <p>Set messages to auto-delete after being read. No traces left behind. Perfect for sensitive communications.</p>
    </div>
    <div class="feature">
        <div class="feature-icon fi-6">&#127760;</div>
        <h3>Tor & Onion Access</h3>
        <p>Access OBLIVION Mail over Tor for complete anonymity. Your IP address never touches our servers.</p>
    </div>
</div>
</section>

<section id="technology" class="tech-section">
    <h2>Built on Proven Technology</h2>
    <p class="desc">OBLIVION Mail is built on the Magma codebase originally created by Lavabit — the encrypted email service famously used by Edward Snowden. We're continuing the mission of private communication.</p>
    <div class="tech-grid">
        <div class="tech-item">
            <strong>DIME Protocol</strong>
            Dark Internet Mail Environment — next-gen encrypted email
        </div>
        <div class="tech-item">
            <strong>AES-256 + RSA</strong>
            Military-grade encryption at rest and in transit
        </div>
        <div class="tech-item">
            <strong>Perfect Forward Secrecy</strong>
            Each session uses unique keys
        </div>
        <div class="tech-item">
            <strong>No Metadata Leaks</strong>
            Headers and subjects encrypted too
        </div>
        <div class="tech-item">
            <strong>SMTP/IMAP/POP3</strong>
            Works with any standard email client
        </div>
        <div class="tech-item">
            <strong>Onion Routing</strong>
            Full Tor hidden service support
        </div>
    </div>
</section>

<section id="compare" class="comparison">
    <h2>How We Compare</h2>
    <table class="comp-table">
        <thead>
            <tr>
                <th>Feature</th>
                <th>OBLIVION Mail</th>
                <th>Gmail</th>
                <th>ProtonMail</th>
            </tr>
        </thead>
        <tbody>
            <tr class="highlight-row">
                <td>End-to-End Encryption</td>
                <td class="yes">Yes</td>
                <td class="no">No</td>
                <td class="yes">Yes</td>
            </tr>
            <tr>
                <td>Zero-Access Encryption</td>
                <td class="yes">Yes</td>
                <td class="no">No</td>
                <td class="yes">Yes</td>
            </tr>
            <tr class="highlight-row">
                <td>No Ad Tracking</td>
                <td class="yes">Yes</td>
                <td class="no">No</td>
                <td class="yes">Yes</td>
            </tr>
            <tr>
                <td>Open Source</td>
                <td class="yes">Yes</td>
                <td class="no">No</td>
                <td class="partial">Partial</td>
            </tr>
            <tr class="highlight-row">
                <td>Tor / Onion Access</td>
                <td class="yes">Yes</td>
                <td class="no">No</td>
                <td class="yes">Yes</td>
            </tr>
            <tr>
                <td>Self-Destructing Messages</td>
                <td class="yes">Yes</td>
                <td class="partial">Limited</td>
                <td class="yes">Yes</td>
            </tr>
            <tr class="highlight-row">
                <td>Encrypted Metadata</td>
                <td class="yes">Yes (DIME)</td>
                <td class="no">No</td>
                <td class="no">No</td>
            </tr>
            <tr>
                <td>Free Tier</td>
                <td class="yes">Yes</td>
                <td class="yes">Yes</td>
                <td class="yes">Yes</td>
            </tr>
            <tr class="highlight-row">
                <td>Based in Privacy Jurisdiction</td>
                <td class="yes">Yes</td>
                <td class="no">No (US)</td>
                <td class="yes">Yes (CH)</td>
            </tr>
        </tbody>
    </table>
</section>

<footer>
    <p>OBLIVION Mail — A product of <a href="https://oblivionsearch.com">OBLIVION Search</a></p>
    <p style="margin-top: 8px;">Powered by <a href="https://github.com/lavabit/magma" target="_blank" rel="noopener">Lavabit/Magma</a> technology</p>
</footer>
</div>

<script>
document.addEventListener('DOMContentLoaded', function() {
    // Load count
    fetch('/api/waitlist/count')
        .then(r => r.json())
        .then(d => {
            document.getElementById('waitlistCount').textContent = d.count.toLocaleString();
        })
        .catch(() => {
            document.getElementById('waitlistCount').textContent = '0';
        });

    // Form submit
    document.getElementById('waitlistForm').addEventListener('submit', function(e) {
        e.preventDefault();
        const email = document.getElementById('emailInput').value.trim();
        if (!email) return;

        fetch('/api/waitlist', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({email: email})
        })
        .then(r => r.json())
        .then(d => {
            if (d.status === 'ok' || d.status === 'exists') {
                document.getElementById('waitlistForm').style.display = 'none';
                document.getElementById('successMsg').style.display = 'block';
                // Update counter
                fetch('/api/waitlist/count')
                    .then(r => r.json())
                    .then(d => {
                        document.getElementById('waitlistCount').textContent = d.count.toLocaleString();
                    });
            }
        })
        .catch(err => {
            alert('Something went wrong. Please try again.');
        });
    });
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def landing():
    return PAGE_HTML


@app.get("/api/waitlist/count")
async def waitlist_count():
    entries = load_waitlist()
    return {"count": len(entries)}


@app.post("/api/waitlist")
async def waitlist_signup(request: Request):
    try:
        data = await request.json()
        email = data.get("email", "").strip().lower()
    except Exception:
        return JSONResponse({"status": "error", "message": "Invalid request"}, 400)

    if not email or "@" not in email or "." not in email:
        return JSONResponse({"status": "error", "message": "Invalid email"}, 400)

    entries = load_waitlist()

    # Check for duplicate
    existing_emails = {e["email"] for e in entries}
    if email in existing_emails:
        return {"status": "exists", "message": "Already on the waitlist"}

    entries.append({
        "email": email,
        "signed_up": datetime.datetime.utcnow().isoformat(),
        "ip": request.client.host if request.client else "unknown"
    })
    save_waitlist(entries)

    return {"status": "ok", "message": "Added to waitlist"}


@app.get("/api/waitlist/export")
async def waitlist_export(key: str = ""):
    """Simple admin export — requires key"""
    if key != "oblivion-mail-admin-2026":
        return JSONResponse({"error": "unauthorized"}, 403)
    return load_waitlist()


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "oblivion-mail", "port": 3055}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3055)
