#!/usr/bin/env python3
"""OBLIVION Community Platform — Port 3056
Saved searches, annotations, trending, leaderboard, instant answers.
"""

import os, secrets, hashlib, json, html as html_module
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Request, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncpg
import uvicorn

app = FastAPI(title="OBLIVION Community")
DB_URL = "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/oblivion_community"
pool = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Startup / Shutdown ──────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10)
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS community_users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(64) UNIQUE NOT NULL,
            email VARCHAR(255) UNIQUE,
            password_hash VARCHAR(128),
            points INT DEFAULT 0,
            badges TEXT[] DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS collections (
            id SERIAL PRIMARY KEY,
            user_id INT REFERENCES community_users(id),
            name VARCHAR(255) NOT NULL,
            description TEXT DEFAULT '',
            is_public BOOLEAN DEFAULT FALSE,
            share_code VARCHAR(16) UNIQUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS collection_items (
            id SERIAL PRIMARY KEY,
            collection_id INT REFERENCES collections(id) ON DELETE CASCADE,
            title VARCHAR(512) NOT NULL,
            url TEXT NOT NULL,
            snippet TEXT DEFAULT '',
            added_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS annotations (
            id SERIAL PRIMARY KEY,
            user_id INT REFERENCES community_users(id),
            url TEXT NOT NULL,
            note TEXT NOT NULL,
            sentiment VARCHAR(16) DEFAULT 'neutral',
            upvotes INT DEFAULT 0,
            downvotes INT DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS trending_queries (
            id SERIAL PRIMARY KEY,
            query VARCHAR(512) NOT NULL,
            search_count INT DEFAULT 1,
            last_seen TIMESTAMPTZ DEFAULT NOW(),
            hour_bucket TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS instant_answers (
            id SERIAL PRIMARY KEY,
            user_id INT REFERENCES community_users(id),
            query_pattern VARCHAR(512) NOT NULL,
            title VARCHAR(255) NOT NULL,
            content TEXT NOT NULL,
            source_url TEXT DEFAULT '',
            status VARCHAR(16) DEFAULT 'pending',
            upvotes INT DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            approved_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_annotations_url ON annotations(url);
        CREATE INDEX IF NOT EXISTS idx_trending_hour ON trending_queries(hour_bucket);
        CREATE INDEX IF NOT EXISTS idx_instant_query ON instant_answers(query_pattern);
        CREATE INDEX IF NOT EXISTS idx_collections_public ON collections(is_public) WHERE is_public = TRUE;
        """)

@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()

# ── Helpers ──────────────────────────────────────────────────────────────────

def esc(s):
    return html_module.escape(str(s)) if s else ""

def gen_share_code():
    return secrets.token_urlsafe(8)[:12]

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

# ── Dark theme CSS ───────────────────────────────────────────────────────────

DARK_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:#0a0a0f;color:#e2e8f0;line-height:1.6;min-height:100vh}
a{color:#818cf8;text-decoration:none}a:hover{text-decoration:underline;color:#a5b4fc}
.nav{background:linear-gradient(135deg,#0f0f1a 0%,#1a1a2e 100%);border-bottom:1px solid #2d2d44;padding:12px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.nav-logo{font-size:20px;font-weight:800;background:linear-gradient(135deg,#7c3aed,#3b82f6,#ec4899);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.nav-links{display:flex;gap:20px}
.nav-links a{color:#94a3b8;font-size:14px;font-weight:500;transition:color 0.2s}
.nav-links a:hover{color:#e2e8f0;text-decoration:none}
.container{max-width:960px;margin:0 auto;padding:32px 20px}
h1{font-size:clamp(24px,4vw,36px);font-weight:800;margin-bottom:8px}
h2{font-size:22px;font-weight:700;margin-bottom:16px;color:#f1f5f9}
.grad{background:linear-gradient(135deg,#7c3aed,#3b82f6,#ec4899);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.subtitle{color:#94a3b8;font-size:16px;margin-bottom:32px}
.card{background:#12121f;border:1px solid #2d2d44;border-radius:12px;padding:24px;margin-bottom:16px;transition:border-color 0.2s}
.card:hover{border-color:#7c3aed}
.card h3{font-size:18px;font-weight:700;margin-bottom:8px}
.card p{color:#94a3b8;font-size:14px}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:11px;font-weight:600;margin-right:6px}
.badge-public{background:#065f46;color:#6ee7b7}
.badge-private{background:#713f12;color:#fcd34d}
.badge-pending{background:#3b3b00;color:#fde68a}
.badge-approved{background:#064e3b;color:#6ee7b7}
.badge-rejected{background:#7f1d1d;color:#fca5a5}
.badge-scam{background:#7f1d1d;color:#fca5a5}
.badge-great{background:#064e3b;color:#6ee7b7}
.badge-neutral{background:#1e293b;color:#94a3b8}
.btn{display:inline-block;padding:10px 24px;border-radius:8px;font-size:14px;font-weight:600;border:none;cursor:pointer;transition:all 0.2s}
.btn-primary{background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff}
.btn-primary:hover{opacity:0.9;text-decoration:none}
.btn-sm{padding:6px 14px;font-size:12px;border-radius:6px}
.btn-outline{border:1px solid #7c3aed;color:#7c3aed;background:transparent}
.btn-outline:hover{background:#7c3aed;color:#fff;text-decoration:none}
input,textarea,select{background:#1a1a2e;border:1px solid #2d2d44;color:#e2e8f0;padding:10px 14px;border-radius:8px;font-size:14px;width:100%;margin-bottom:12px;font-family:inherit}
input:focus,textarea:focus,select:focus{outline:none;border-color:#7c3aed}
textarea{min-height:80px;resize:vertical}
.grid-2{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.trending-num{font-size:28px;font-weight:800;color:#7c3aed;min-width:48px;text-align:right;margin-right:16px}
.trending-row{display:flex;align-items:center;padding:14px 0;border-bottom:1px solid #1e1e2f}
.trending-row:last-child{border-bottom:none}
.leaderboard-row{display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid #1e1e2f}
.leaderboard-rank{font-size:24px;font-weight:800;min-width:48px;text-align:center;margin-right:16px}
.leaderboard-rank.gold{color:#fbbf24}
.leaderboard-rank.silver{color:#94a3b8}
.leaderboard-rank.bronze{color:#d97706}
.points-pill{background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff;padding:4px 12px;border-radius:99px;font-size:12px;font-weight:700;margin-left:auto}
.answer-card{background:linear-gradient(135deg,#1a1a2e,#12121f);border:1px solid #3b82f6;border-radius:12px;padding:20px;margin-bottom:16px}
.answer-card .query{color:#818cf8;font-size:13px;font-weight:600;margin-bottom:6px}
.answer-card h3{font-size:18px;margin-bottom:10px}
.answer-card .content{color:#cbd5e1;font-size:14px;line-height:1.7}
.tab-bar{display:flex;gap:0;margin-bottom:24px;border-bottom:2px solid #2d2d44}
.tab{padding:10px 20px;font-size:14px;font-weight:600;color:#64748b;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px;transition:all 0.2s}
.tab.active{color:#7c3aed;border-bottom-color:#7c3aed}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px;margin-bottom:32px}
.stat-card{background:#12121f;border:1px solid #2d2d44;border-radius:12px;padding:20px;text-align:center}
.stat-num{font-size:32px;font-weight:800;background:linear-gradient(135deg,#7c3aed,#3b82f6);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stat-label{color:#64748b;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:1px;margin-top:4px}
.empty{text-align:center;padding:60px 20px;color:#475569}
.empty p{font-size:16px;margin-bottom:16px}
footer{text-align:center;padding:40px 20px;color:#475569;font-size:13px;border-top:1px solid #1e1e2f;margin-top:60px}
@media(max-width:640px){.nav-links{gap:12px}.container{padding:20px 12px}.stats-grid{grid-template-columns:1fr 1fr}}
"""

NAV_HTML = """
<nav class="nav">
  <a href="/community" class="nav-logo">OBLIVION Community</a>
  <div class="nav-links">
    <a href="/community">Home</a>
    <a href="/community/collections">Collections</a>
    <a href="/community/annotations">Annotations</a>
    <a href="/trending">Trending</a>
    <a href="/community/leaderboard">Leaderboard</a>
    <a href="/community/answers">Instant Answers</a>
    <a href="https://oblivionsearch.com">Search</a>
  </div>
</nav>
"""

def page_wrap(title, body, extra_head=""):
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — OBLIVION Community</title>
<meta name="description" content="OBLIVION Community — save searches, annotate results, see trends, earn badges.">
<link rel="icon" href="https://oblivionsearch.com/favicon.ico">
<style>{DARK_CSS}</style>{extra_head}
</head><body>{NAV_HTML}<div class="container">{body}</div>
<footer>OBLIVION Community &middot; Part of <a href="https://oblivionsearch.com">OBLIVION Search</a> &middot; Privacy-first, community-powered.</footer>
</body></html>"""

# ═══════════════════════════════════════════════════════════════════════════
#  1. COMMUNITY HOME
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/community", response_class=HTMLResponse)
async def community_home():
    stats = {}
    async with pool.acquire() as c:
        stats["users"] = await c.fetchval("SELECT COUNT(*) FROM community_users") or 0
        stats["collections"] = await c.fetchval("SELECT COUNT(*) FROM collections WHERE is_public=TRUE") or 0
        stats["annotations"] = await c.fetchval("SELECT COUNT(*) FROM annotations") or 0
        stats["answers"] = await c.fetchval("SELECT COUNT(*) FROM instant_answers WHERE status='approved'") or 0

    body = f"""
    <div style="text-align:center;padding:40px 0 20px">
      <h1><span class="grad">OBLIVION Community</span></h1>
      <p class="subtitle">More than a search engine. A community-powered knowledge platform.</p>
    </div>
    <div class="stats-grid">
      <div class="stat-card"><div class="stat-num">{stats['users']}</div><div class="stat-label">Members</div></div>
      <div class="stat-card"><div class="stat-num">{stats['collections']}</div><div class="stat-label">Public Collections</div></div>
      <div class="stat-card"><div class="stat-num">{stats['annotations']}</div><div class="stat-label">Annotations</div></div>
      <div class="stat-card"><div class="stat-num">{stats['answers']}</div><div class="stat-label">Instant Answers</div></div>
    </div>
    <div class="grid-2">
      <div class="card">
        <h3>Saved Collections</h3>
        <p>Save search results into named, shareable collections. Curate the best links on any topic.</p>
        <br><a href="/community/collections" class="btn btn-primary btn-sm">Browse Collections</a>
      </div>
      <div class="card">
        <h3>Annotations</h3>
        <p>Add notes to search results — flag scams, highlight gems, help the community search smarter.</p>
        <br><a href="/community/annotations" class="btn btn-primary btn-sm">View Annotations</a>
      </div>
      <div class="card">
        <h3>Trending Searches</h3>
        <p>See what the community is searching for right now. Anonymized, updated hourly.</p>
        <br><a href="/trending" class="btn btn-primary btn-sm">See Trends</a>
      </div>
      <div class="card">
        <h3>Instant Answers</h3>
        <p>Community-written answer cards that appear at the top of search results. Like DuckDuckGo Instant Answers, but by you.</p>
        <br><a href="/community/answers" class="btn btn-primary btn-sm">Contribute Answers</a>
      </div>
    </div>
    """
    return HTMLResponse(page_wrap("Home", body))

# ═══════════════════════════════════════════════════════════════════════════
#  2. COLLECTIONS — Saved Searches
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/community/collections", response_class=HTMLResponse)
async def list_collections():
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT cl.id, cl.name, cl.description, cl.share_code, cl.created_at, cl.is_public,
                   cu.username, COUNT(ci.id) as item_count
            FROM collections cl
            JOIN community_users cu ON cl.user_id = cu.id
            LEFT JOIN collection_items ci ON ci.collection_id = cl.id
            WHERE cl.is_public = TRUE
            GROUP BY cl.id, cu.username
            ORDER BY cl.created_at DESC LIMIT 50
        """)

    cards = ""
    for r in rows:
        vis = '<span class="badge badge-public">Public</span>'
        cards += f"""<div class="card">
            <h3><a href="/community/collection/{r['share_code']}">{esc(r['name'])}</a></h3>
            <p>{esc(r['description'][:200])}</p>
            <div style="margin-top:10px;display:flex;align-items:center;gap:8px">
                {vis}
                <span style="color:#64748b;font-size:12px">{r['item_count']} items &middot; by {esc(r['username'])} &middot; {r['created_at'].strftime('%b %d, %Y')}</span>
            </div>
        </div>"""

    if not cards:
        cards = '<div class="empty"><p>No public collections yet.</p><p>Be the first to curate a collection!</p></div>'

    body = f"""
    <h1><span class="grad">Collections</span></h1>
    <p class="subtitle">Curated link collections from the OBLIVION community.</p>
    <div style="margin-bottom:24px"><a href="/community/collections/new" class="btn btn-primary">Create Collection</a></div>
    {cards}
    """
    return HTMLResponse(page_wrap("Collections", body))

@app.get("/community/collection/{share_code}", response_class=HTMLResponse)
async def view_collection(share_code: str):
    async with pool.acquire() as c:
        cl = await c.fetchrow("""
            SELECT cl.*, cu.username FROM collections cl
            JOIN community_users cu ON cl.user_id = cu.id
            WHERE cl.share_code = $1
        """, share_code)
        if not cl:
            raise HTTPException(404, "Collection not found")
        items = await c.fetch("""
            SELECT * FROM collection_items WHERE collection_id = $1 ORDER BY added_at DESC
        """, cl["id"])

    items_html = ""
    for i, item in enumerate(items, 1):
        items_html += f"""<div class="card" style="display:flex;align-items:flex-start;gap:16px">
            <div style="color:#7c3aed;font-weight:800;font-size:18px;min-width:32px;text-align:right">{i}</div>
            <div>
                <h3 style="font-size:16px"><a href="{esc(item['url'])}" target="_blank" rel="noopener">{esc(item['title'])}</a></h3>
                <p style="font-size:12px;color:#64748b;margin-top:2px">{esc(item['url'][:80])}</p>
                <p style="margin-top:6px">{esc(item['snippet'][:300])}</p>
            </div>
        </div>"""

    if not items_html:
        items_html = '<div class="empty"><p>This collection is empty.</p></div>'

    vis_badge = '<span class="badge badge-public">Public</span>' if cl["is_public"] else '<span class="badge badge-private">Private</span>'
    share_url = f"https://oblivionsearch.com/community/collection/{share_code}"

    body = f"""
    <div style="margin-bottom:24px">
        <a href="/community/collections" style="color:#64748b;font-size:13px">&larr; All Collections</a>
    </div>
    <h1>{esc(cl['name'])}</h1>
    <div style="display:flex;align-items:center;gap:10px;margin:8px 0 16px">
        {vis_badge}
        <span style="color:#64748b;font-size:13px">by {esc(cl['username'])} &middot; {cl['created_at'].strftime('%b %d, %Y')}</span>
    </div>
    <p style="color:#94a3b8;margin-bottom:8px">{esc(cl['description'])}</p>
    <p style="font-size:12px;color:#64748b;margin-bottom:24px">Share: <code style="background:#1a1a2e;padding:2px 8px;border-radius:4px">{share_url}</code></p>
    <h2>{len(items)} Items</h2>
    {items_html}
    """
    return HTMLResponse(page_wrap(cl["name"], body))

@app.get("/community/collections/new", response_class=HTMLResponse)
async def new_collection_form():
    body = """
    <h1><span class="grad">Create Collection</span></h1>
    <p class="subtitle">Save and share curated search results.</p>
    <form method="POST" action="/api/collections" class="card" style="max-width:600px">
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Your Username</label>
        <input name="username" placeholder="e.g. privacy_guru" required>
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Collection Name</label>
        <input name="name" placeholder="e.g. Best Privacy Tools 2026" required>
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Description</label>
        <textarea name="description" placeholder="What is this collection about?"></textarea>
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Visibility</label>
        <select name="is_public"><option value="1">Public — anyone can view</option><option value="0">Private — only via link</option></select>
        <br><button type="submit" class="btn btn-primary">Create Collection</button>
    </form>
    """
    return HTMLResponse(page_wrap("Create Collection", body))

# ── Collections API ──────────────────────────────────────────────────────────

@app.post("/api/collections")
async def create_collection(
    username: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    is_public: str = Form("1"),
):
    public = is_public == "1"
    share_code = gen_share_code()
    async with pool.acquire() as c:
        user = await c.fetchrow("SELECT id FROM community_users WHERE username=$1", username)
        if not user:
            user_id = await c.fetchval(
                "INSERT INTO community_users (username) VALUES ($1) RETURNING id", username
            )
        else:
            user_id = user["id"]
        await c.execute(
            "INSERT INTO collections (user_id, name, description, is_public, share_code) VALUES ($1,$2,$3,$4,$5)",
            user_id, name, description, public, share_code,
        )
        await c.execute("UPDATE community_users SET points = points + 10 WHERE id=$1", user_id)
    return RedirectResponse(f"/community/collection/{share_code}", status_code=303)

@app.post("/api/collections/{share_code}/items")
async def add_collection_item(
    share_code: str,
    title: str = Form(...),
    url: str = Form(...),
    snippet: str = Form(""),
):
    async with pool.acquire() as c:
        cl = await c.fetchrow("SELECT id, user_id FROM collections WHERE share_code=$1", share_code)
        if not cl:
            raise HTTPException(404, "Collection not found")
        await c.execute(
            "INSERT INTO collection_items (collection_id, title, url, snippet) VALUES ($1,$2,$3,$4)",
            cl["id"], title, url, snippet,
        )
        await c.execute("UPDATE community_users SET points = points + 2 WHERE id=$1", cl["user_id"])
    return RedirectResponse(f"/community/collection/{share_code}", status_code=303)

@app.get("/api/collections/{share_code}/add", response_class=HTMLResponse)
async def add_item_form(share_code: str):
    async with pool.acquire() as c:
        cl = await c.fetchrow("SELECT name FROM collections WHERE share_code=$1", share_code)
        if not cl:
            raise HTTPException(404)
    body = f"""
    <h1>Add Item to <span class="grad">{esc(cl['name'])}</span></h1>
    <form method="POST" action="/api/collections/{esc(share_code)}/items" class="card" style="max-width:600px">
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Title</label>
        <input name="title" placeholder="Page title" required>
        <label style="font-size:13px;color:#94a3b8;font-weight:600">URL</label>
        <input name="url" type="url" placeholder="https://example.com" required>
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Snippet / Note</label>
        <textarea name="snippet" placeholder="Why is this link useful?"></textarea>
        <br><button type="submit" class="btn btn-primary">Add to Collection</button>
    </form>
    """
    return HTMLResponse(page_wrap("Add Item", body))

# ═══════════════════════════════════════════════════════════════════════════
#  3. ANNOTATIONS
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/community/annotations", response_class=HTMLResponse)
async def list_annotations():
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT a.*, cu.username FROM annotations a
            JOIN community_users cu ON a.user_id = cu.id
            ORDER BY a.created_at DESC LIMIT 50
        """)

    items = ""
    for r in rows:
        s = r["sentiment"]
        badge_cls = "badge-scam" if s == "scam" else ("badge-great" if s == "great" else "badge-neutral")
        label = s.capitalize()
        items += f"""<div class="card">
            <div style="display:flex;justify-content:space-between;align-items:flex-start">
                <div>
                    <p style="font-size:12px;color:#64748b;margin-bottom:4px">{esc(r['url'][:80])}</p>
                    <p style="font-size:15px;color:#e2e8f0">{esc(r['note'])}</p>
                    <p style="font-size:12px;color:#475569;margin-top:6px">by {esc(r['username'])} &middot; {r['created_at'].strftime('%b %d, %Y')}</p>
                </div>
                <div style="text-align:right">
                    <span class="badge {badge_cls}">{label}</span>
                    <div style="font-size:12px;color:#64748b;margin-top:6px">{r['upvotes']} up / {r['downvotes']} down</div>
                </div>
            </div>
        </div>"""

    if not items:
        items = '<div class="empty"><p>No annotations yet. Be the first to review a search result!</p></div>'

    body = f"""
    <h1><span class="grad">Annotations</span></h1>
    <p class="subtitle">Community notes on search results. Flag scams, highlight gems.</p>
    <div style="margin-bottom:24px"><a href="/community/annotations/new" class="btn btn-primary">Add Annotation</a></div>
    {items}
    """
    return HTMLResponse(page_wrap("Annotations", body))

@app.get("/community/annotations/new", response_class=HTMLResponse)
async def new_annotation_form():
    body = """
    <h1><span class="grad">Add Annotation</span></h1>
    <p class="subtitle">Help the community by reviewing a website or search result.</p>
    <form method="POST" action="/api/annotations" class="card" style="max-width:600px">
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Your Username</label>
        <input name="username" placeholder="e.g. privacy_guru" required>
        <label style="font-size:13px;color:#94a3b8;font-weight:600">URL</label>
        <input name="url" type="url" placeholder="https://example.com/page" required>
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Your Note</label>
        <textarea name="note" placeholder="What should others know about this site?" required></textarea>
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Sentiment</label>
        <select name="sentiment">
            <option value="great">Great resource</option>
            <option value="neutral" selected>Neutral</option>
            <option value="scam">Scam / Harmful</option>
        </select>
        <br><button type="submit" class="btn btn-primary">Submit Annotation</button>
    </form>
    """
    return HTMLResponse(page_wrap("Add Annotation", body))

@app.post("/api/annotations")
async def create_annotation(
    username: str = Form(...),
    url: str = Form(...),
    note: str = Form(...),
    sentiment: str = Form("neutral"),
):
    if sentiment not in ("great", "neutral", "scam"):
        sentiment = "neutral"
    async with pool.acquire() as c:
        user = await c.fetchrow("SELECT id FROM community_users WHERE username=$1", username)
        if not user:
            user_id = await c.fetchval(
                "INSERT INTO community_users (username) VALUES ($1) RETURNING id", username
            )
        else:
            user_id = user["id"]
        await c.execute(
            "INSERT INTO annotations (user_id, url, note, sentiment) VALUES ($1,$2,$3,$4)",
            user_id, url, note, sentiment,
        )
        await c.execute("UPDATE community_users SET points = points + 5 WHERE id=$1", user_id)
    return RedirectResponse("/community/annotations", status_code=303)

@app.get("/api/annotations/lookup")
async def lookup_annotations(url: str = Query(...)):
    """API: get annotations for a URL — called by OBLIVION Search to show community notes."""
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT a.note, a.sentiment, a.upvotes, a.downvotes, cu.username, a.created_at
            FROM annotations a
            JOIN community_users cu ON a.user_id = cu.id
            WHERE a.url = $1
            ORDER BY a.upvotes DESC LIMIT 10
        """, url)
    return JSONResponse([{
        "note": r["note"], "sentiment": r["sentiment"],
        "upvotes": r["upvotes"], "downvotes": r["downvotes"],
        "username": r["username"], "created_at": r["created_at"].isoformat(),
    } for r in rows])

@app.post("/api/annotations/{ann_id}/vote")
async def vote_annotation(ann_id: int, direction: str = Form("up")):
    col = "upvotes" if direction == "up" else "downvotes"
    async with pool.acquire() as c:
        await c.execute(f"UPDATE annotations SET {col} = {col} + 1 WHERE id=$1", ann_id)
    return JSONResponse({"ok": True})

# ═══════════════════════════════════════════════════════════════════════════
#  4. TRENDING SEARCHES (public dashboard)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/trending", response_class=HTMLResponse)
async def trending_page():
    async with pool.acquire() as c:
        # Current hour bucket
        rows = await c.fetch("""
            SELECT query, SUM(search_count) as total
            FROM trending_queries
            WHERE hour_bucket >= NOW() - INTERVAL '24 hours'
            GROUP BY query
            ORDER BY total DESC LIMIT 20
        """)

    items = ""
    for i, r in enumerate(rows, 1):
        items += f"""<div class="trending-row">
            <div class="trending-num">{i}</div>
            <div>
                <a href="https://oblivionsearch.com/search?q={esc(r['query'])}" style="font-size:16px;font-weight:600;color:#e2e8f0">{esc(r['query'])}</a>
                <p style="color:#64748b;font-size:12px;margin-top:2px">{r['total']} searches in 24h</p>
            </div>
        </div>"""

    if not items:
        items = '<div class="empty"><p>No trending data yet. Searches will appear here as people use OBLIVION.</p></div>'

    body = f"""
    <h1><span class="grad">Trending Searches</span></h1>
    <p class="subtitle">What the world is searching for on OBLIVION. Anonymized, updated hourly.</p>
    <div class="card">
        {items}
    </div>
    """
    return HTMLResponse(page_wrap("Trending Searches", body))

@app.post("/api/trending/log")
async def log_trending(query: str = Form(...)):
    """Called by the search engine to log a query (anonymized)."""
    q = query.strip().lower()[:200]
    if len(q) < 2:
        return JSONResponse({"ok": False})
    from datetime import timezone
    now = datetime.now(timezone.utc)
    bucket = now.replace(minute=0, second=0, microsecond=0)
    async with pool.acquire() as c:
        existing = await c.fetchrow(
            "SELECT id FROM trending_queries WHERE query=$1 AND hour_bucket=$2", q, bucket
        )
        if existing:
            await c.execute(
                "UPDATE trending_queries SET search_count = search_count + 1, last_seen = NOW() WHERE id=$1",
                existing["id"],
            )
        else:
            await c.execute(
                "INSERT INTO trending_queries (query, hour_bucket) VALUES ($1, $2)", q, bucket
            )
    return JSONResponse({"ok": True})

# ═══════════════════════════════════════════════════════════════════════════
#  5. LEADERBOARD
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/community/leaderboard", response_class=HTMLResponse)
async def leaderboard():
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT cu.username, cu.points, cu.badges,
                   COUNT(DISTINCT cl.id) as collections_count,
                   COUNT(DISTINCT a.id) as annotations_count,
                   COUNT(DISTINCT ia.id) as answers_count
            FROM community_users cu
            LEFT JOIN collections cl ON cl.user_id = cu.id
            LEFT JOIN annotations a ON a.user_id = cu.id
            LEFT JOIN instant_answers ia ON ia.user_id = cu.id
            GROUP BY cu.id
            ORDER BY cu.points DESC LIMIT 50
        """)

    items = ""
    for i, r in enumerate(rows, 1):
        rank_cls = "gold" if i == 1 else ("silver" if i == 2 else ("bronze" if i == 3 else ""))
        badge_list = ""
        if r["badges"]:
            for b in r["badges"]:
                badge_list += f'<span class="badge badge-neutral">{esc(b)}</span>'
        items += f"""<div class="leaderboard-row">
            <div class="leaderboard-rank {rank_cls}">{i}</div>
            <div style="flex:1">
                <div style="font-weight:700;font-size:16px">{esc(r['username'])}</div>
                <div style="font-size:12px;color:#64748b;margin-top:2px">
                    {r['collections_count']} collections &middot; {r['annotations_count']} annotations &middot; {r['answers_count']} answers
                </div>
                <div style="margin-top:4px">{badge_list}</div>
            </div>
            <div class="points-pill">{r['points']} pts</div>
        </div>"""

    if not items:
        items = '<div class="empty"><p>No community members yet. Start contributing to climb the leaderboard!</p></div>'

    body = f"""
    <h1><span class="grad">Community Leaderboard</span></h1>
    <p class="subtitle">Top contributors who make OBLIVION better for everyone.</p>
    <div class="card" style="padding:0;overflow:hidden">
        {items}
    </div>
    """
    return HTMLResponse(page_wrap("Leaderboard", body))

# ═══════════════════════════════════════════════════════════════════════════
#  6. INSTANT ANSWERS (Community-driven)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/community/answers", response_class=HTMLResponse)
async def list_answers():
    async with pool.acquire() as c:
        approved = await c.fetch("""
            SELECT ia.*, cu.username FROM instant_answers ia
            JOIN community_users cu ON ia.user_id = cu.id
            WHERE ia.status = 'approved'
            ORDER BY ia.upvotes DESC, ia.created_at DESC LIMIT 50
        """)
        pending = await c.fetch("""
            SELECT ia.*, cu.username FROM instant_answers ia
            JOIN community_users cu ON ia.user_id = cu.id
            WHERE ia.status = 'pending'
            ORDER BY ia.created_at DESC LIMIT 20
        """)

    approved_html = ""
    for r in approved:
        approved_html += f"""<div class="answer-card">
            <div class="query">Query: "{esc(r['query_pattern'])}"</div>
            <h3>{esc(r['title'])}</h3>
            <div class="content">{esc(r['content'][:500])}</div>
            <div style="margin-top:10px;display:flex;align-items:center;gap:12px">
                <span class="badge badge-approved">Approved</span>
                <span style="font-size:12px;color:#64748b">by {esc(r['username'])} &middot; {r['upvotes']} upvotes</span>
                {f'<a href="{esc(r["source_url"])}" target="_blank" style="font-size:12px">Source</a>' if r["source_url"] else ""}
            </div>
        </div>"""

    pending_html = ""
    for r in pending:
        pending_html += f"""<div class="answer-card" style="border-color:#fbbf24;opacity:0.8">
            <div class="query">Query: "{esc(r['query_pattern'])}"</div>
            <h3>{esc(r['title'])}</h3>
            <div class="content">{esc(r['content'][:300])}</div>
            <div style="margin-top:10px">
                <span class="badge badge-pending">Pending Review</span>
                <span style="font-size:12px;color:#64748b;margin-left:8px">by {esc(r['username'])}</span>
            </div>
        </div>"""

    if not approved_html and not pending_html:
        approved_html = '<div class="empty"><p>No instant answers yet. Be the first to contribute!</p></div>'

    body = f"""
    <h1><span class="grad">Instant Answers</span></h1>
    <p class="subtitle">Community-written answer cards that appear at the top of OBLIVION search results.</p>
    <div style="margin-bottom:24px"><a href="/community/answers/new" class="btn btn-primary">Submit an Answer</a></div>
    <h2>Approved Answers</h2>
    {approved_html if approved_html else '<p style="color:#475569;margin-bottom:24px">None yet.</p>'}
    {"<h2>Pending Review</h2>" + pending_html if pending_html else ""}
    """
    return HTMLResponse(page_wrap("Instant Answers", body))

@app.get("/community/answers/new", response_class=HTMLResponse)
async def new_answer_form():
    body = """
    <h1><span class="grad">Submit Instant Answer</span></h1>
    <p class="subtitle">Write an answer card that helps people searching for a specific query.</p>
    <form method="POST" action="/api/answers" class="card" style="max-width:600px">
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Your Username</label>
        <input name="username" placeholder="e.g. privacy_guru" required>
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Query Pattern</label>
        <input name="query_pattern" placeholder='e.g. "What is OBLIVION?" or "best VPN 2026"' required>
        <p style="font-size:11px;color:#475569;margin:-8px 0 12px">The search query this answer should appear for.</p>
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Answer Title</label>
        <input name="title" placeholder="e.g. OBLIVION Search Engine" required>
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Answer Content</label>
        <textarea name="content" style="min-height:120px" placeholder="Write a clear, factual answer. Include key facts and details." required></textarea>
        <label style="font-size:13px;color:#94a3b8;font-weight:600">Source URL (optional)</label>
        <input name="source_url" type="url" placeholder="https://en.wikipedia.org/wiki/...">
        <br><button type="submit" class="btn btn-primary">Submit for Review</button>
    </form>
    """
    return HTMLResponse(page_wrap("Submit Instant Answer", body))

@app.post("/api/answers")
async def create_answer(
    username: str = Form(...),
    query_pattern: str = Form(...),
    title: str = Form(...),
    content: str = Form(...),
    source_url: str = Form(""),
):
    async with pool.acquire() as c:
        user = await c.fetchrow("SELECT id FROM community_users WHERE username=$1", username)
        if not user:
            user_id = await c.fetchval(
                "INSERT INTO community_users (username) VALUES ($1) RETURNING id", username
            )
        else:
            user_id = user["id"]
        await c.execute(
            """INSERT INTO instant_answers (user_id, query_pattern, title, content, source_url)
               VALUES ($1, $2, $3, $4, $5)""",
            user_id, query_pattern.strip().lower(), title, content, source_url,
        )
        await c.execute("UPDATE community_users SET points = points + 15 WHERE id=$1", user_id)
    return RedirectResponse("/community/answers", status_code=303)

@app.get("/api/answers/lookup")
async def lookup_answer(q: str = Query(...)):
    """API: called by OBLIVION Search to get instant answer for a query."""
    async with pool.acquire() as c:
        row = await c.fetchrow("""
            SELECT ia.title, ia.content, ia.source_url, ia.upvotes, cu.username
            FROM instant_answers ia
            JOIN community_users cu ON ia.user_id = cu.id
            WHERE ia.status = 'approved' AND ia.query_pattern = $1
            ORDER BY ia.upvotes DESC LIMIT 1
        """, q.strip().lower())
    if not row:
        return JSONResponse(None)
    return JSONResponse({
        "title": row["title"], "content": row["content"],
        "source_url": row["source_url"], "upvotes": row["upvotes"],
        "username": row["username"],
    })

@app.post("/api/answers/{answer_id}/approve")
async def approve_answer(answer_id: int, pin: str = Form(...)):
    """Admin: approve an instant answer. Requires admin PIN."""
    if pin != os.environ.get("ADMIN_PIN", "000000"):
        raise HTTPException(403, "Invalid PIN")
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE instant_answers SET status='approved', approved_at=NOW() WHERE id=$1",
            answer_id,
        )
        row = await c.fetchrow("SELECT user_id FROM instant_answers WHERE id=$1", answer_id)
        if row:
            await c.execute("UPDATE community_users SET points = points + 25 WHERE id=$1", row["user_id"])
    return JSONResponse({"ok": True, "status": "approved"})

@app.post("/api/answers/{answer_id}/reject")
async def reject_answer(answer_id: int, pin: str = Form(...)):
    if pin != os.environ.get("ADMIN_PIN", "000000"):
        raise HTTPException(403, "Invalid PIN")
    async with pool.acquire() as c:
        await c.execute("UPDATE instant_answers SET status='rejected' WHERE id=$1", answer_id)
    return JSONResponse({"ok": True, "status": "rejected"})

# ═══════════════════════════════════════════════════════════════════════════
#  7. JSON API ENDPOINTS (for integration with OBLIVION Search)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/stats")
async def api_stats():
    """Public stats for embedding in search UI."""
    async with pool.acquire() as c:
        return JSONResponse({
            "users": await c.fetchval("SELECT COUNT(*) FROM community_users") or 0,
            "collections": await c.fetchval("SELECT COUNT(*) FROM collections WHERE is_public=TRUE") or 0,
            "annotations": await c.fetchval("SELECT COUNT(*) FROM annotations") or 0,
            "answers_approved": await c.fetchval("SELECT COUNT(*) FROM instant_answers WHERE status='approved'") or 0,
            "trending_queries_24h": await c.fetchval(
                "SELECT COUNT(DISTINCT query) FROM trending_queries WHERE hour_bucket >= NOW() - INTERVAL '24 hours'"
            ) or 0,
        })

@app.get("/api/trending")
async def api_trending():
    """JSON API for trending queries."""
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT query, SUM(search_count) as total
            FROM trending_queries
            WHERE hour_bucket >= NOW() - INTERVAL '24 hours'
            GROUP BY query ORDER BY total DESC LIMIT 20
        """)
    return JSONResponse([{"query": r["query"], "count": r["total"]} for r in rows])

@app.get("/api/leaderboard")
async def api_leaderboard():
    """JSON API for leaderboard."""
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT username, points, badges FROM community_users
            ORDER BY points DESC LIMIT 20
        """)
    return JSONResponse([{
        "username": r["username"], "points": r["points"],
        "badges": list(r["badges"]) if r["badges"] else [],
    } for r in rows])

# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "service": "oblivion-community", "port": 3056})

# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3056)
