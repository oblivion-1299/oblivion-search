#!/usr/bin/env python3
"""
OBLIVION Commento — Privacy-first comment system
FastAPI on port 3072

Inspired by the abandoned Commento project (MIT license).
No tracking, no cookies, no third-party requests.
Markdown support, spam filtering, embeddable via <script> tag.
"""

import hashlib
import html as html_mod
import json
import re
import secrets
import smtplib
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from typing import Optional

import asyncpg
import stripe
import uvicorn
from fastapi import FastAPI, Request, Query, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Stripe config
# ---------------------------------------------------------------------------
stripe.api_key = "os.environ.get("STRIPE_SECRET_KEY", "")"
DOMAIN_URL = "https://oblivionsearch.com"
PRODUCT_NAME = "oblivion_comments"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "os.environ.get("SMTP_USER", "")"
SMTP_PASS = "os.environ.get("SMTP_PASS", "")"

COMMENTS_PLANS = {
    "pro": {"name": "Pro", "price_amount": 700, "currency": "gbp", "label": "£7/mo", "sites": 5},
    "business": {"name": "Business", "price_amount": 1900, "currency": "gbp", "label": "£19/mo", "sites": 0},
}

_saas_pool: Optional[asyncpg.Pool] = None

app = FastAPI(title="OBLIVION Commento", version="1.0.0")

DB_DSN = "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/oblivion_comments"
pool: Optional[asyncpg.Pool] = None

ADMIN_PIN = os.environ.get("ADMIN_PIN", "000000")

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
_rate_store: dict[str, list[float]] = defaultdict(list)

def _check_rate(ip: str, limit: int = 5, window: int = 60) -> bool:
    now = time.time()
    cutoff = now - window
    _rate_store[ip] = [t for t in _rate_store[ip] if t > cutoff]
    if len(_rate_store[ip]) >= limit:
        return False
    _rate_store[ip].append(now)
    return True

def _get_ip(request: Request) -> str:
    return request.headers.get("x-real-ip", request.headers.get("x-forwarded-for", request.client.host))

# ---------------------------------------------------------------------------
# Spam filtering
# ---------------------------------------------------------------------------
SPAM_KEYWORDS = [
    "buy now", "click here", "free money", "make money fast", "casino",
    "viagra", "cialis", "porn", "xxx", "lottery", "winner", "congratulations",
    "nigerian prince", "wire transfer", "crypto giveaway", "double your",
    "act now", "limited time", "subscribe now", "earn extra", "work from home",
    "100% free", "no obligation", "risk-free", "miracle", "guaranteed",
]

SPAM_URL_PATTERN = re.compile(r'https?://\S+', re.IGNORECASE)

def is_spam(text: str, author: str) -> tuple[bool, str]:
    lower = text.lower()
    for kw in SPAM_KEYWORDS:
        if kw in lower:
            return True, f"Blocked keyword: {kw}"
    urls = SPAM_URL_PATTERN.findall(text)
    if len(urls) > 3:
        return True, "Too many URLs"
    if len(text) > 5000:
        return True, "Comment too long"
    if author and len(author) > 100:
        return True, "Author name too long"
    return False, ""

# ---------------------------------------------------------------------------
# Markdown (simple subset)
# ---------------------------------------------------------------------------
def simple_markdown(text: str) -> str:
    text = html_mod.escape(text)
    # Code blocks
    text = re.sub(r'```(.*?)```', r'<pre><code>\1</code></pre>', text, flags=re.DOTALL)
    # Inline code
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Italic
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    # Links
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" rel="nofollow noopener" target="_blank">\1</a>', text)
    # Line breaks
    text = text.replace('\n', '<br>')
    return text

# ---------------------------------------------------------------------------
# Theming
# ---------------------------------------------------------------------------
ACCENT = "#00d4ff"
BG = "#0a0a0f"
BG2 = "#12121a"
BG3 = "#1a1a2e"
TEXT = "#e0e0e0"
TEXT_DIM = "#888"

def _base_css():
    return f"""
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ background:{BG}; color:{TEXT}; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height:1.6; }}
    a {{ color:{ACCENT}; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
    .container {{ max-width:900px; margin:0 auto; padding:2rem 1.5rem; }}
    .header {{ display:flex; align-items:center; gap:1rem; margin-bottom:2rem; padding-bottom:1rem; border-bottom:1px solid {BG3}; }}
    .header h1 {{ font-size:1.4rem; font-weight:600; }}
    .header h1 span {{ color:{ACCENT}; }}
    .card {{ background:{BG2}; border:1px solid {BG3}; border-radius:12px; padding:1.5rem; margin-bottom:1rem; }}
    input, textarea {{
        width:100%; padding:0.75rem 1rem; background:{BG}; border:1px solid {BG3};
        border-radius:8px; color:{TEXT}; font-size:0.95rem; margin-bottom:0.75rem;
        font-family:inherit;
    }}
    input:focus, textarea:focus {{ outline:none; border-color:{ACCENT}; }}
    button, .btn {{
        background:{ACCENT}; color:{BG}; border:none; padding:0.75rem 1.5rem;
        border-radius:8px; font-weight:600; cursor:pointer; font-size:0.95rem;
    }}
    button:hover {{ opacity:0.85; }}
    .btn-danger {{ background:#f44; }}
    code {{ background:{BG3}; padding:0.15em 0.4em; border-radius:4px; font-size:0.9em; }}
    pre {{ background:{BG}; border:1px solid {BG3}; border-radius:8px; padding:1rem; overflow-x:auto; margin:0.5rem 0; }}
    pre code {{ background:none; padding:0; }}
    table {{ width:100%; border-collapse:collapse; margin-top:1rem; }}
    th, td {{ padding:0.6rem 0.8rem; text-align:left; border-bottom:1px solid {BG3}; font-size:0.9rem; }}
    th {{ color:{ACCENT}; font-size:0.8rem; text-transform:uppercase; }}
    .tag {{ display:inline-block; padding:0.2rem 0.6rem; border-radius:4px; font-size:0.75rem; font-weight:600; }}
    .tag-green {{ background:#0f52; color:#0f5; }}
    .tag-red {{ background:#f002; color:#f44; }}
    footer {{ text-align:center; color:{TEXT_DIM}; font-size:0.8rem; margin-top:3rem; padding-top:1rem; border-top:1px solid {BG3}; }}
    @media(max-width:600px) {{ .container {{ padding:1rem; }} }}
    @media(max-width:480px) {{
        body {{ overflow-x:hidden; }}
        .container {{ padding:0.75rem; }}
        .header h1 {{ font-size:1.2rem; }}
        input, textarea {{ font-size:16px; }}
        button, .btn {{ min-height:44px; font-size:16px; }}
        .card {{ padding:1rem; }}
        table {{ font-size:0.8rem; }}
        th, td {{ padding:0.4rem 0.5rem; }}
        .tag {{ font-size:0.7rem; }}
        code {{ font-size:0.82em; }}
        pre {{ padding:0.75rem; }}
    }}
    @media(max-width:375px) {{
        .header {{ flex-direction:column; gap:0.5rem; }}
        .header h1 {{ font-size:1rem; }}
        button, .btn {{ width:100%; }}
        table {{ font-size:0.75rem; }}
        .card {{ padding:0.75rem; }}
    }}
    """

def _footer():
    return """<footer>
        <p>OBLIVION Commento &mdash; Privacy-first comments. No tracking.</p>
        <p style="margin-top:0.3rem;"><a href="https://oblivionsearch.com">oblivionsearch.com</a></p>
    </footer>"""

def _page(title, body, extra_head=""):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — OBLIVION Commento</title>
    <meta name="description" content="Privacy-first comment system. No tracking, no cookies, no third-party requests.">
    <link rel="canonical" href="https://oblivionsearch.com/comments">
    <meta property="og:title" content="{title} — OBLIVION Commento">
    <meta property="og:description" content="Privacy-first comment system. No tracking, no cookies, no third-party requests.">
    <meta property="og:url" content="https://oblivionsearch.com/comments">
    <meta property="og:type" content="website">
    <meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
    <link rel="icon" href="https://oblivionsearch.com/favicon.ico">
    <style>{_base_css()}</style>
    {extra_head}
</head>
<body>
<div class="container">
{body}
{_footer()}
</div>
</body>
</html>"""


# =========================================================================
# Database setup
# =========================================================================
@app.on_event("startup")
async def startup():
    global pool, _saas_pool
    # Create database if not exists
    sys_pool = await asyncpg.create_pool("postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/postgres", min_size=1, max_size=2)
    async with sys_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname='oblivion_comments'")
        if not exists:
            await conn.execute("CREATE DATABASE oblivion_comments")
    await sys_pool.close()

    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                page_url TEXT NOT NULL,
                page_domain TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT 'Anonymous',
                body TEXT NOT NULL,
                body_html TEXT NOT NULL,
                parent_id UUID REFERENCES comments(id) ON DELETE CASCADE,
                ip_hash TEXT NOT NULL,
                is_spam BOOLEAN DEFAULT FALSE,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_comments_page_url ON comments(page_url);
            CREATE INDEX IF NOT EXISTS idx_comments_domain ON comments(page_domain);
            CREATE INDEX IF NOT EXISTS idx_comments_created ON comments(created_at DESC);

            CREATE TABLE IF NOT EXISTS allowed_domains (
                domain TEXT PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

    _saas_pool = await asyncpg.create_pool(
        "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/postgres", min_size=1, max_size=5
    )
    async with _saas_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS saas_customers (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                product TEXT NOT NULL,
                plan TEXT NOT NULL,
                api_key TEXT NOT NULL UNIQUE,
                requests_today INT DEFAULT 0,
                requests_reset_date DATE DEFAULT CURRENT_DATE,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_saas_apikey ON saas_customers(api_key)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_saas_stripe_sub ON saas_customers(stripe_subscription_id)")

@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()
    if _saas_pool:
        await _saas_pool.close()


# =========================================================================
# LANDING PAGE — /comments
# =========================================================================
@app.get("/comments", response_class=HTMLResponse)
async def landing():
    body = f"""
    <div class="header">
        <div>
            <h1><span>OBLIVION</span> Commento</h1>
            <p style="color:{TEXT_DIM};font-size:0.9rem;">Privacy-first comment system for any website</p>
        </div>
    </div>

    <div class="card">
        <h3 style="color:{ACCENT};margin-bottom:0.5rem;">What is OBLIVION Commento?</h3>
        <p>A lightweight, privacy-first comment system that any website can embed. Unlike Disqus or other services, OBLIVION Commento:</p>
        <ul style="margin:0.75rem 0 0 1.5rem;color:{TEXT_DIM};">
            <li>No tracking cookies or fingerprinting</li>
            <li>No third-party requests</li>
            <li>No personal data collection</li>
            <li>Markdown support</li>
            <li>Threaded replies</li>
            <li>Built-in spam filtering</li>
            <li>Dark theme by default</li>
        </ul>
    </div>

    <div class="card">
        <h3 style="color:{ACCENT};margin-bottom:0.5rem;">Quick Start</h3>
        <p style="margin-bottom:0.75rem;">Add this single line to your HTML:</p>
        <pre><code>&lt;script src="https://oblivionsearch.com/comments/embed.js"
        data-oblivion-comments&gt;&lt;/script&gt;</code></pre>
        <p style="margin-top:0.75rem;color:{TEXT_DIM};font-size:0.9rem;">That's it. Comments will appear automatically on each page.</p>
    </div>

    <div class="card">
        <h3 style="color:{ACCENT};margin-bottom:0.5rem;">API</h3>
        <table>
            <tr><th>Method</th><th>Endpoint</th><th>Description</th></tr>
            <tr><td>GET</td><td><code>/api/comments?url=PAGE_URL</code></td><td>Get comments for a page</td></tr>
            <tr><td>POST</td><td><code>/api/comments</code></td><td>Submit a comment (JSON body)</td></tr>
        </table>
        <p style="margin-top:0.75rem;color:{TEXT_DIM};font-size:0.85rem;">POST body: <code>{{"url": "...", "author": "...", "body": "...", "parent_id": null}}</code></p>
    </div>

    <div class="card">
        <h3 style="color:{ACCENT};margin-bottom:0.5rem;">Live Demo</h3>
        <div id="oblivion-comments" data-page-url="https://oblivionsearch.com/comments"></div>
    </div>
    <script src="/comments/embed.js" data-oblivion-comments></script>
    """
    return _page("Privacy-First Comments", body)


# =========================================================================
# EMBEDDABLE JS — /comments/embed.js
# =========================================================================
EMBED_JS = """
(function() {
    'use strict';
    const ACCENT = '#00d4ff';
    const BG = '#0a0a0f';
    const BG2 = '#12121a';
    const BG3 = '#1a1a2e';
    const TEXT = '#e0e0e0';
    const TEXT_DIM = '#888';

    // Find the container
    let container = document.getElementById('oblivion-comments');
    if (!container) {
        const script = document.querySelector('script[data-oblivion-comments]');
        if (!script) return;
        container = document.createElement('div');
        container.id = 'oblivion-comments';
        script.parentNode.insertBefore(container, script);
    }

    const pageUrl = container.getAttribute('data-page-url') || window.location.href.split('#')[0].split('?')[0];
    const baseUrl = (function() {
        const scripts = document.querySelectorAll('script[data-oblivion-comments]');
        for (const s of scripts) {
            if (s.src) {
                const u = new URL(s.src);
                return u.origin;
            }
        }
        return '';
    })();
    const apiBase = baseUrl + '/api/comments';

    // Inject styles
    const style = document.createElement('style');
    style.textContent = `
        #oblivion-comments { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: ${TEXT}; line-height: 1.6; }
        .obc-form { background: ${BG2}; border: 1px solid ${BG3}; border-radius: 12px; padding: 1rem; margin-bottom: 1.5rem; }
        .obc-input { width: 100%; padding: 0.6rem 0.8rem; background: ${BG}; border: 1px solid ${BG3}; border-radius: 8px; color: ${TEXT}; font-size: 0.9rem; margin-bottom: 0.5rem; font-family: inherit; box-sizing: border-box; }
        .obc-input:focus { outline: none; border-color: ${ACCENT}; }
        .obc-textarea { min-height: 80px; resize: vertical; }
        .obc-btn { background: ${ACCENT}; color: ${BG}; border: none; padding: 0.6rem 1.2rem; border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 0.9rem; }
        .obc-btn:hover { opacity: 0.85; }
        .obc-btn-sm { padding: 0.3rem 0.8rem; font-size: 0.8rem; }
        .obc-btn-ghost { background: transparent; color: ${ACCENT}; border: 1px solid ${BG3}; }
        .obc-comment { background: ${BG2}; border: 1px solid ${BG3}; border-radius: 12px; padding: 1rem; margin-bottom: 0.75rem; }
        .obc-comment .obc-replies { margin-left: 1.5rem; margin-top: 0.75rem; }
        .obc-meta { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
        .obc-author { font-weight: 600; color: ${ACCENT}; font-size: 0.9rem; }
        .obc-time { font-size: 0.8rem; color: ${TEXT_DIM}; }
        .obc-body { font-size: 0.9rem; }
        .obc-body a { color: ${ACCENT}; }
        .obc-body code { background: ${BG3}; padding: 0.1em 0.3em; border-radius: 3px; font-size: 0.85em; }
        .obc-body pre { background: ${BG}; border: 1px solid ${BG3}; border-radius: 8px; padding: 0.75rem; overflow-x: auto; }
        .obc-body pre code { background: none; padding: 0; }
        .obc-actions { margin-top: 0.5rem; }
        .obc-count { font-size: 0.9rem; color: ${TEXT_DIM}; margin-bottom: 1rem; }
        .obc-error { color: #f44; font-size: 0.85rem; margin-top: 0.5rem; }
        .obc-powered { text-align: center; margin-top: 1rem; font-size: 0.75rem; color: ${TEXT_DIM}; }
        .obc-powered a { color: ${ACCENT}; text-decoration: none; }
        @media(max-width: 600px) { .obc-comment .obc-replies { margin-left: 0.75rem; } }
    `;
    container.appendChild(style);

    function timeAgo(dateStr) {
        const d = new Date(dateStr);
        const now = new Date();
        const s = Math.floor((now - d) / 1000);
        if (s < 60) return 'just now';
        if (s < 3600) return Math.floor(s/60) + 'm ago';
        if (s < 86400) return Math.floor(s/3600) + 'h ago';
        if (s < 2592000) return Math.floor(s/86400) + 'd ago';
        return d.toLocaleDateString();
    }

    function renderComment(c, depth) {
        depth = depth || 0;
        let html = '<div class="obc-comment">';
        html += '<div class="obc-meta"><span class="obc-author">' + escHtml(c.author) + '</span><span class="obc-time">' + timeAgo(c.created_at) + '</span></div>';
        html += '<div class="obc-body">' + c.body_html + '</div>';
        if (depth < 3) {
            html += '<div class="obc-actions"><button class="obc-btn obc-btn-sm obc-btn-ghost" onclick="obcReply(\\''+c.id+'\\')">Reply</button></div>';
            html += '<div id="obc-reply-'+c.id+'" style="display:none;margin-top:0.5rem;"></div>';
        }
        if (c.replies && c.replies.length) {
            html += '<div class="obc-replies">';
            for (const r of c.replies) {
                html += renderComment(r, depth + 1);
            }
            html += '</div>';
        }
        html += '</div>';
        return html;
    }

    function escHtml(s) {
        const d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    async function loadComments() {
        try {
            const res = await fetch(apiBase + '?url=' + encodeURIComponent(pageUrl));
            const data = await res.json();
            const comments = data.comments || [];
            let html = '<div class="obc-count">' + data.total + ' comment' + (data.total !== 1 ? 's' : '') + '</div>';

            // New comment form
            html += '<div class="obc-form">';
            html += '<input class="obc-input" id="obc-author" placeholder="Name (optional)">';
            html += '<textarea class="obc-input obc-textarea" id="obc-body" placeholder="Write a comment... (Markdown supported)"></textarea>';
            html += '<button class="obc-btn" onclick="obcSubmit()">Post Comment</button>';
            html += '<div id="obc-error" class="obc-error"></div>';
            html += '</div>';

            for (const c of comments) {
                html += renderComment(c, 0);
            }

            html += '<div class="obc-powered">Powered by <a href="https://oblivionsearch.com/comments">OBLIVION Commento</a></div>';
            container.innerHTML = '<style>' + style.textContent + '</style>' + html;
        } catch(e) {
            container.innerHTML = '<p style="color:#f44;">Could not load comments.</p>';
        }
    }

    window.obcSubmit = async function(parentId) {
        const authorEl = parentId ? document.getElementById('obc-reply-author-'+parentId) : document.getElementById('obc-author');
        const bodyEl = parentId ? document.getElementById('obc-reply-body-'+parentId) : document.getElementById('obc-body');
        const errorEl = parentId ? document.getElementById('obc-reply-error-'+parentId) : document.getElementById('obc-error');
        const author = (authorEl && authorEl.value.trim()) || 'Anonymous';
        const body = bodyEl ? bodyEl.value.trim() : '';
        if (!body) { if(errorEl) errorEl.textContent = 'Comment cannot be empty.'; return; }
        try {
            const res = await fetch(apiBase, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ url: pageUrl, author, body, parent_id: parentId || null })
            });
            const data = await res.json();
            if (data.error) { if(errorEl) errorEl.textContent = data.error; return; }
            loadComments();
        } catch(e) {
            if(errorEl) errorEl.textContent = 'Failed to post comment.';
        }
    };

    window.obcReply = function(parentId) {
        const el = document.getElementById('obc-reply-'+parentId);
        if (!el) return;
        if (el.style.display === 'none') {
            el.style.display = 'block';
            el.innerHTML = '<input class="obc-input" id="obc-reply-author-'+parentId+'" placeholder="Name (optional)">'
                + '<textarea class="obc-input obc-textarea" id="obc-reply-body-'+parentId+'" placeholder="Reply... (Markdown supported)"></textarea>'
                + '<button class="obc-btn obc-btn-sm" onclick="obcSubmit(\\''+parentId+'\\')">Reply</button>'
                + '<div id="obc-reply-error-'+parentId+'" class="obc-error"></div>';
        } else {
            el.style.display = 'none';
        }
    };

    loadComments();
})();
""".strip()

@app.get("/comments/embed.js")
async def embed_js():
    return Response(content=EMBED_JS, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"})


# =========================================================================
# API — Submit comment
# =========================================================================
class CommentInput(BaseModel):
    url: str
    author: str = "Anonymous"
    body: str
    parent_id: Optional[str] = None

@app.post("/api/comments")
async def submit_comment(request: Request, comment: CommentInput):
    ip = _get_ip(request)
    if not _check_rate(ip, limit=5, window=60):
        return JSONResponse({"error": "Rate limited — please wait before posting again."}, status_code=429)

    url = comment.url.strip()
    if not url:
        return JSONResponse({"error": "URL is required."}, status_code=400)

    author = comment.author.strip() or "Anonymous"
    body = comment.body.strip()
    if not body:
        return JSONResponse({"error": "Comment cannot be empty."}, status_code=400)
    if len(body) > 5000:
        return JSONResponse({"error": "Comment too long (max 5000 chars)."}, status_code=400)

    spam, reason = is_spam(body, author)
    body_html = simple_markdown(body)
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]

    # Extract domain
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc or url
    except Exception:
        domain = url

    parent_uuid = None
    if comment.parent_id:
        try:
            parent_uuid = uuid.UUID(comment.parent_id)
        except ValueError:
            return JSONResponse({"error": "Invalid parent_id."}, status_code=400)

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO comments (page_url, page_domain, author, body, body_html, parent_id, ip_hash, is_spam)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id, created_at
        """, url, domain, author, body, body_html, parent_uuid, ip_hash, spam)

    if spam:
        return JSONResponse({"error": "Your comment has been flagged for review."}, status_code=200)

    return {
        "id": str(row["id"]),
        "author": author,
        "body_html": body_html,
        "created_at": row["created_at"].isoformat(),
    }


# =========================================================================
# API — Get comments
# =========================================================================
@app.get("/api/comments")
async def get_comments(url: str = Query(...)):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, author, body_html, parent_id, created_at
            FROM comments
            WHERE page_url = $1 AND is_spam = FALSE AND is_deleted = FALSE
            ORDER BY created_at ASC
        """, url)

    # Build tree
    by_id = {}
    roots = []
    for r in rows:
        c = {
            "id": str(r["id"]),
            "author": r["author"],
            "body_html": r["body_html"],
            "parent_id": str(r["parent_id"]) if r["parent_id"] else None,
            "created_at": r["created_at"].isoformat(),
            "replies": [],
        }
        by_id[c["id"]] = c

    for c in by_id.values():
        if c["parent_id"] and c["parent_id"] in by_id:
            by_id[c["parent_id"]]["replies"].append(c)
        else:
            roots.append(c)

    return {"url": url, "total": len(by_id), "comments": roots}


# =========================================================================
# ADMIN — /comments/admin (PIN protected)
# =========================================================================
@app.get("/comments/admin", response_class=HTMLResponse)
async def admin_page(pin: Optional[str] = Query(None)):
    if pin != ADMIN_PIN:
        body = f"""
        <div class="header"><h1><span>OBLIVION</span> Commento Admin</h1></div>
        <div class="card">
            <h3 style="color:{ACCENT};">Enter Admin PIN</h3>
            <form method="GET" action="/comments/admin">
                <input type="password" name="pin" placeholder="PIN" style="max-width:200px;">
                <button type="submit">Login</button>
            </form>
        </div>"""
        return _page("Admin Login", body)

    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM comments")
        spam_count = await conn.fetchval("SELECT COUNT(*) FROM comments WHERE is_spam = TRUE")
        recent = await conn.fetch("""
            SELECT id, page_url, page_domain, author, body, is_spam, is_deleted, created_at
            FROM comments ORDER BY created_at DESC LIMIT 50
        """)
        domains = await conn.fetch("SELECT domain, created_at FROM allowed_domains ORDER BY domain")

    rows_html = ""
    for r in recent:
        status = ""
        if r["is_spam"]:
            status = '<span class="tag tag-red">Spam</span>'
        elif r["is_deleted"]:
            status = '<span class="tag tag-red">Deleted</span>'
        else:
            status = '<span class="tag tag-green">OK</span>'
        body_preview = html_mod.escape(r["body"][:80]) + ("..." if len(r["body"]) > 80 else "")
        rows_html += f"""<tr>
            <td>{status}</td>
            <td>{html_mod.escape(r['author'])}</td>
            <td style="font-size:0.8rem;">{body_preview}</td>
            <td style="font-size:0.8rem;">{html_mod.escape(r['page_domain'])}</td>
            <td style="font-size:0.8rem;">{r['created_at'].strftime('%Y-%m-%d %H:%M')}</td>
            <td>
                <a href="/comments/admin/delete?id={r['id']}&pin={ADMIN_PIN}" style="color:#f44;font-size:0.8rem;">Delete</a>
                {' | <a href="/comments/admin/approve?id=' + str(r['id']) + '&pin=' + ADMIN_PIN + '" style="color:#0f5;font-size:0.8rem;">Approve</a>' if r['is_spam'] else ''}
            </td>
        </tr>"""

    body = f"""
    <div class="header"><h1><span>OBLIVION</span> Commento Admin</h1></div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-bottom:1.5rem;">
        <div class="card"><h3 style="color:{ACCENT};font-size:2rem;">{total}</h3><p style="color:{TEXT_DIM};">Total Comments</p></div>
        <div class="card"><h3 style="color:#f44;font-size:2rem;">{spam_count}</h3><p style="color:{TEXT_DIM};">Spam Blocked</p></div>
        <div class="card"><h3 style="color:#0f5;font-size:2rem;">{total - spam_count}</h3><p style="color:{TEXT_DIM};">Published</p></div>
    </div>
    <div class="card">
        <h3 style="color:{ACCENT};margin-bottom:0.5rem;">Recent Comments</h3>
        <table>
            <tr><th>Status</th><th>Author</th><th>Comment</th><th>Domain</th><th>Date</th><th>Actions</th></tr>
            {rows_html}
        </table>
    </div>"""
    return _page("Admin", body)


@app.get("/comments/admin/delete")
async def admin_delete(id: str, pin: str):
    if pin != ADMIN_PIN:
        raise HTTPException(403, "Invalid PIN")
    try:
        uid = uuid.UUID(id)
    except ValueError:
        raise HTTPException(400, "Invalid ID")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE comments SET is_deleted = TRUE WHERE id = $1", uid)
    return HTMLResponse(f'<script>window.location="/comments/admin?pin={ADMIN_PIN}";</script>')


@app.get("/comments/admin/approve")
async def admin_approve(id: str, pin: str):
    if pin != ADMIN_PIN:
        raise HTTPException(403, "Invalid PIN")
    try:
        uid = uuid.UUID(id)
    except ValueError:
        raise HTTPException(400, "Invalid ID")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE comments SET is_spam = FALSE WHERE id = $1", uid)
    return HTMLResponse(f'<script>window.location="/comments/admin?pin={ADMIN_PIN}";</script>')


# =========================================================================
# SaaS Helpers
# =========================================================================

def _generate_api_key():
    return "obcom_" + secrets.token_hex(24)

def _send_welcome_email(email: str, api_key: str, plan: str):
    try:
        body = f"""Welcome to OBLIVION Commento ({plan} plan)!

Your API key: {api_key}

Embed on your sites:
  <script src="https://oblivionsearch.com/comments/embed.js"
          data-oblivion-comments data-api-key="{api_key}"></script>

Dashboard: {DOMAIN_URL}/comments/dashboard?key={api_key}

Thank you for choosing OBLIVION Commento.
"""
        msg = MIMEText(body)
        msg["Subject"] = f"OBLIVION Commento — Your API Key ({plan})"
        msg["From"] = SMTP_USER
        msg["To"] = email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        import logging
        logging.error("Failed to send welcome email to %s: %s", email, e)


# =========================================================================
# SaaS Routes — Pricing / Checkout / Success / Dashboard / Webhook
# =========================================================================

@app.get("/comments/pricing", response_class=HTMLResponse)
async def comments_pricing():
    body = f"""
    <div class="header">
        <div><h1><span>OBLIVION</span> Commento Pricing</h1>
        <p style="color:{TEXT_DIM};font-size:0.9rem;">Privacy-first comments for your website</p></div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1.5rem;margin:1.5rem 0;">
      <div class="card" style="text-align:center;">
        <h3>Free</h3>
        <div style="font-size:2.2rem;font-weight:700;color:{ACCENT};margin:1rem 0;">£0<span style="font-size:0.9rem;color:{TEXT_DIM};">/mo</span></div>
        <ul style="list-style:none;text-align:left;margin:1rem 0;">
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; 1 site</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; 100 comments/month</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; Basic spam filter</li>
        </ul>
        <a href="/comments" class="btn" style="background:{BG3};color:{TEXT};text-decoration:none;">Use Free</a>
      </div>
      <div class="card" style="text-align:center;border-color:{ACCENT};">
        <h3>Pro</h3>
        <div style="font-size:2.2rem;font-weight:700;color:{ACCENT};margin:1rem 0;">£7<span style="font-size:0.9rem;color:{TEXT_DIM};">/mo</span></div>
        <ul style="list-style:none;text-align:left;margin:1rem 0;">
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; 5 sites</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; Unlimited comments</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; Custom styling</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; Moderation tools</li>
        </ul>
        <a href="/comments/checkout/pro" class="btn" style="text-decoration:none;">Subscribe</a>
      </div>
      <div class="card" style="text-align:center;">
        <h3>Business</h3>
        <div style="font-size:2.2rem;font-weight:700;color:{ACCENT};margin:1rem 0;">£19<span style="font-size:0.9rem;color:{TEXT_DIM};">/mo</span></div>
        <ul style="list-style:none;text-align:left;margin:1rem 0;">
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; Unlimited sites</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; REST API access</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; Spam AI</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; White-label</li>
        </ul>
        <a href="/comments/checkout/business" class="btn" style="text-decoration:none;">Subscribe</a>
      </div>
    </div>
    """
    return _page("Pricing", body)


@app.get("/comments/checkout/{plan}")
async def comments_checkout(plan: str):
    if plan not in COMMENTS_PLANS:
        return HTMLResponse("<h1>Invalid plan</h1>", status_code=400)
    p = COMMENTS_PLANS[plan]
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": p["currency"],
                    "product_data": {"name": f"OBLIVION Commento — {p['name']}"},
                    "unit_amount": p["price_amount"],
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=DOMAIN_URL + "/comments/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=DOMAIN_URL + "/comments/pricing",
            metadata={"product": PRODUCT_NAME, "plan": plan},
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        return HTMLResponse(f"<h1>Checkout error</h1><p>{e}</p>", status_code=500)


@app.get("/comments/success", response_class=HTMLResponse)
async def comments_success(session_id: str = Query(...)):
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        email = session.customer_details.email or session.customer_email or "unknown"
        plan = session.metadata.get("plan", "pro")
        p = COMMENTS_PLANS.get(plan, COMMENTS_PLANS["pro"])
        api_key = _generate_api_key()

        async with _saas_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO saas_customers (email, stripe_customer_id, stripe_subscription_id, product, plan, api_key)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, email, session.customer, session.subscription, PRODUCT_NAME, plan, api_key)

        _send_welcome_email(email, api_key, p["name"])

        body = f"""
        <div class="header"><div><h1><span>OBLIVION</span> Commento</h1><p style="color:{TEXT_DIM};">Welcome! Your subscription is active.</p></div></div>
        <div class="card" style="max-width:600px;margin:0 auto;">
          <p style="color:{TEXT_DIM};margin-bottom:0.5rem;">Your API Key:</p>
          <div style="background:{BG};border:1px solid {BG3};border-radius:8px;padding:1rem;font-family:monospace;font-size:1.05rem;word-break:break-all;color:{ACCENT};">{api_key}</div>
          <p style="color:{TEXT_DIM};margin-top:1rem;font-size:0.9rem;">Plan: <strong style="color:{ACCENT};">{p['name']}</strong> &mdash; {p['label']}</p>
          <p style="color:{TEXT_DIM};font-size:0.9rem;">Email: {email}</p>
          <p style="color:#f44;margin-top:1rem;font-size:0.85rem;">Save this key! It has also been sent to your email.</p>
          <a href="/comments/dashboard?key={api_key}" class="btn" style="display:inline-block;margin-top:1.5rem;text-decoration:none;">Open Dashboard</a>
        </div>"""
        return _page("Welcome", body)
    except Exception as e:
        return HTMLResponse(f"<h1>Error</h1><p>{e}</p>", status_code=500)


@app.get("/comments/dashboard", response_class=HTMLResponse)
async def comments_dashboard(key: str = Query(...)):
    async with _saas_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM saas_customers WHERE api_key=$1 AND product=$2", key, PRODUCT_NAME
        )
    if not row:
        return HTMLResponse("<h1>Invalid API key</h1>", status_code=404)

    p = COMMENTS_PLANS.get(row["plan"], COMMENTS_PLANS["pro"])
    status_tag = f'<span style="color:#0f5;font-weight:600;">Active</span>' if row["active"] else '<span style="color:#f44;font-weight:600;">Inactive</span>'
    sites_str = str(p["sites"]) if p["sites"] else "Unlimited"

    body = f"""
    <div class="header"><div><h1><span>OBLIVION</span> Commento Dashboard</h1></div></div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-bottom:1.5rem;">
      <div class="card"><p style="color:{TEXT_DIM};font-size:0.8rem;text-transform:uppercase;">Status</p><p style="font-size:1.2rem;margin-top:0.3rem;">{status_tag}</p></div>
      <div class="card"><p style="color:{TEXT_DIM};font-size:0.8rem;text-transform:uppercase;">Plan</p><p style="font-size:1.2rem;margin-top:0.3rem;color:{ACCENT};">{p['name']}</p></div>
      <div class="card"><p style="color:{TEXT_DIM};font-size:0.8rem;text-transform:uppercase;">Sites Allowed</p><p style="font-size:1.2rem;margin-top:0.3rem;">{sites_str}</p></div>
    </div>
    <div class="card">
      <h3>Your API Key</h3>
      <div style="background:{BG};border:1px solid {BG3};border-radius:8px;padding:1rem;font-family:monospace;font-size:0.95rem;word-break:break-all;color:{ACCENT};margin-top:0.5rem;">{row['api_key']}</div>
      <p style="color:{TEXT_DIM};margin-top:1rem;font-size:0.9rem;">Email: {row['email']}</p>
      <p style="color:{TEXT_DIM};font-size:0.9rem;">Subscribed: {row['created_at'].strftime('%Y-%m-%d')}</p>
    </div>
    <div class="card">
      <h3>Embed Code</h3>
      <pre style="background:{BG};border:1px solid {BG3};border-radius:8px;padding:1rem;overflow-x:auto;font-size:0.85rem;color:{TEXT_DIM};margin-top:0.5rem;">&lt;script src="{DOMAIN_URL}/comments/embed.js"
        data-oblivion-comments
        data-api-key="{row['api_key']}"&gt;&lt;/script&gt;</pre>
    </div>"""
    return _page("Dashboard", body)


@app.post("/comments/webhook")
async def comments_webhook(request: Request):
    payload = await request.body()
    try:
        event = stripe.Event.construct_from(json.loads(payload), stripe.api_key)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if event.type == "customer.subscription.deleted":
        sub_id = event.data.object.id
        async with _saas_pool.acquire() as conn:
            await conn.execute(
                "UPDATE saas_customers SET active=FALSE WHERE stripe_subscription_id=$1", sub_id
            )
    elif event.type == "customer.subscription.updated":
        sub_id = event.data.object.id
        active = event.data.object.status == "active"
        async with _saas_pool.acquire() as conn:
            await conn.execute(
                "UPDATE saas_customers SET active=$1 WHERE stripe_subscription_id=$2", active, sub_id
            )

    return {"received": True}


# =========================================================================
# CORS middleware for embed.js API calls
# =========================================================================
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# =========================================================================
# STARTUP
# =========================================================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3072)
