#!/usr/bin/env python3
"""
OBLIVION Retro Web Search — FastAPI on port 3068
Search archived GeoCities, BBS text files, and early web content
via the Internet Archive's CDX API.

Data cached in PostgreSQL `oblivion_retro` database.
"""

import os
import sys
import json
import hashlib
import logging
from datetime import datetime
from typing import Optional, List
from contextlib import asynccontextmanager

import httpx
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ── Config ─────────────────────────────────────────────────────────
PORT = 3068
PG_HOST = "127.0.0.1"
PG_PORT = 5432
PG_USER = "postgres"
PG_PASS = "os.environ.get("DB_PASSWORD", "change_me")"
PG_DB = "oblivion_retro"

IA_CDX_BASE = "https://web.archive.org/cdx/search/cdx"
IA_WAYBACK = "https://web.archive.org/web"

# GeoCities neighborhoods
GEOCITIES_NEIGHBORHOODS = {
    "Area51": "Sci-fi, fantasy, UFOs",
    "Heartland": "Family, religion, pets",
    "SiliconValley": "Computers, technology, programming",
    "Hollywood": "Movies, TV, celebrities",
    "Broadway": "Theater, music, performing arts",
    "CapitolHill": "Politics, government, activism",
    "CollegePark": "College life, education",
    "EnchantedForest": "Children's sites, fairy tales",
    "FashionAvenue": "Fashion, beauty, style",
    "MotorCity": "Cars, racing, automotive",
    "NapaValley": "Food, wine, cooking",
    "Nashville": "Country music",
    "Pentagon": "Military, veterans",
    "RainForest": "Environment, nature",
    "ResearchTriangle": "Science, research",
    "SoHo": "Art, poetry, writing",
    "SouthBeach": "Travel, beaches",
    "SunsetStrip": "Rock music, bands",
    "TimesSquare": "Games, humor",
    "Tokyo": "Anime, Japan, Asian culture",
    "WallStreet": "Finance, investing",
    "WestHollywood": "LGBTQ community",
    "Yosemite": "Outdoors, camping, hiking",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("retro")


# ── Database Setup ─────────────────────────────────────────────────
def get_pg_conn(dbname="postgres"):
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=dbname
    )


def init_database():
    """Create the oblivion_retro database and tables."""
    # Create database if needed
    conn = get_pg_conn("postgres")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (PG_DB,))
    if not cur.fetchone():
        cur.execute(f"CREATE DATABASE {PG_DB}")
        log.info(f"Created database: {PG_DB}")
    cur.close()
    conn.close()

    # Create tables
    conn = get_pg_conn(PG_DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS retro_cache (
            id SERIAL PRIMARY KEY,
            query_hash VARCHAR(64) UNIQUE NOT NULL,
            query_text TEXT NOT NULL,
            source VARCHAR(50) NOT NULL,
            results JSONB NOT NULL,
            result_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            expires_at TIMESTAMP DEFAULT (NOW() + INTERVAL '7 days')
        );
        CREATE INDEX IF NOT EXISTS idx_retro_cache_hash ON retro_cache(query_hash);
        CREATE INDEX IF NOT EXISTS idx_retro_cache_source ON retro_cache(source);

        CREATE TABLE IF NOT EXISTS retro_searches (
            id SERIAL PRIMARY KEY,
            query TEXT NOT NULL,
            source VARCHAR(50),
            result_count INTEGER DEFAULT 0,
            ip_address VARCHAR(45),
            searched_at TIMESTAMP DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_retro_searches_time ON retro_searches(searched_at);
    """)
    conn.commit()
    cur.close()
    conn.close()
    log.info("Database tables initialized")


def get_cached(query_text, source):
    """Check if we have cached results."""
    qhash = hashlib.sha256(f"{source}:{query_text}".encode()).hexdigest()
    try:
        conn = get_pg_conn(PG_DB)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT results, result_count FROM retro_cache
            WHERE query_hash = %s AND expires_at > NOW()
        """, (qhash,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return row["results"]
    except Exception as e:
        log.debug(f"Cache miss: {e}")
    return None


def set_cached(query_text, source, results):
    """Cache search results."""
    qhash = hashlib.sha256(f"{source}:{query_text}".encode()).hexdigest()
    try:
        conn = get_pg_conn(PG_DB)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO retro_cache (query_hash, query_text, source, results, result_count)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (query_hash) DO UPDATE SET
                results = EXCLUDED.results,
                result_count = EXCLUDED.result_count,
                expires_at = NOW() + INTERVAL '7 days'
        """, (qhash, query_text, source, json.dumps(results), len(results)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log.warning(f"Cache write failed: {e}")


def log_search(query, source, count, ip=""):
    try:
        conn = get_pg_conn(PG_DB)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO retro_searches (query, source, result_count, ip_address)
            VALUES (%s, %s, %s, %s)
        """, (query, source, count, ip))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass


# ── Internet Archive CDX Search ────────────────────────────────────
async def search_ia_cdx(query: str, domain: str = "geocities.com",
                        limit: int = 50) -> List[dict]:
    """Search Internet Archive's CDX API for archived pages."""
    # Check cache first
    cache_key = f"{domain}/{query}"
    cached = get_cached(cache_key, "ia_cdx")
    if cached:
        return cached

    # GeoCities neighborhoods are URL paths, not search terms
    # Try multiple URL patterns for best results
    is_neighborhood = query.lower() in [h.lower() for h in GEOCITIES_NEIGHBORHOODS]

    if domain == "geocities.com" and is_neighborhood:
        url_pattern = f"geocities.com/{query}/*"
    elif domain == "geocities.com":
        url_pattern = f"geocities.com/*{query}*"
    else:
        url_pattern = f"{domain}/*{query}*" if query else f"{domain}/*"

    params = {
        "url": url_pattern,
        "output": "json",
        "limit": limit,
        "fl": "urlkey,timestamp,original,mimetype,statuscode,length",
        "filter": "mimetype:text/html",
        "collapse": "urlkey",
    }

    results = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(IA_CDX_BASE, params=params)
            if resp.status_code != 200:
                log.warning(f"IA CDX returned {resp.status_code}")
                return results

            data = resp.json()
            if len(data) < 2:
                return results

            headers = data[0]
            for row in data[1:]:
                record = dict(zip(headers, row))
                ts = record.get("timestamp", "")
                original_url = record.get("original", "")

                # Parse timestamp
                try:
                    date_str = datetime.strptime(ts[:8], "%Y%m%d").strftime("%B %d, %Y")
                    year = ts[:4]
                except (ValueError, IndexError):
                    date_str = "Unknown date"
                    year = "?"

                wayback_url = f"{IA_WAYBACK}/{ts}/{original_url}"

                # Detect GeoCities neighborhood
                neighborhood = detect_neighborhood(original_url)

                results.append({
                    "url": original_url,
                    "wayback_url": wayback_url,
                    "timestamp": ts,
                    "date": date_str,
                    "year": year,
                    "status": record.get("statuscode", ""),
                    "size": record.get("length", "0"),
                    "neighborhood": neighborhood,
                })

    except Exception as e:
        log.error(f"IA CDX search failed: {e}")

    if results:
        set_cached(cache_key, "ia_cdx", results)

    return results


def detect_neighborhood(url: str) -> Optional[str]:
    """Detect which GeoCities neighborhood a URL belongs to."""
    url_lower = url.lower()
    for hood in GEOCITIES_NEIGHBORHOODS:
        if hood.lower() in url_lower:
            return hood
    return None


# ── BBS Text Files Search ──────────────────────────────────────────
async def search_textfiles(query: str, limit: int = 30) -> List[dict]:
    """Search textfiles.com archive via IA CDX."""
    cached = get_cached(query, "textfiles")
    if cached:
        return cached

    params = {
        "url": f"textfiles.com/*{query}*",
        "output": "json",
        "limit": limit,
        "fl": "urlkey,timestamp,original,mimetype,statuscode",
        "filter": "mimetype:text/plain|mimetype:text/html",
        "collapse": "urlkey",
    }

    results = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(IA_CDX_BASE, params=params)
            if resp.status_code != 200:
                return results

            data = resp.json()
            if len(data) < 2:
                return results

            headers = data[0]
            for row in data[1:]:
                record = dict(zip(headers, row))
                ts = record.get("timestamp", "")
                original_url = record.get("original", "")

                # Categorize BBS content
                bbs_category = categorize_bbs(original_url)

                results.append({
                    "url": original_url,
                    "wayback_url": f"{IA_WAYBACK}/{ts}/{original_url}",
                    "timestamp": ts,
                    "date": ts[:4] if len(ts) >= 4 else "?",
                    "category": bbs_category,
                    "type": "bbs_textfile",
                })

    except Exception as e:
        log.error(f"Textfiles search failed: {e}")

    if results:
        set_cached(query, "textfiles", results)

    return results


def categorize_bbs(url: str) -> str:
    categories = {
        "hacking": "Hacking & Phreaking",
        "anarchy": "Anarchy Files",
        "humor": "Humor & Comedy",
        "science": "Science & Tech",
        "games": "Games & Entertainment",
        "apple": "Apple II",
        "bbs": "BBS Culture",
        "art": "ASCII Art",
        "music": "Music & Bands",
        "politics": "Politics",
    }
    url_lower = url.lower()
    for key, label in categories.items():
        if key in url_lower:
            return label
    return "General"


# ── FastAPI App ────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    log.info(f"OBLIVION Retro Search running on port {PORT}")
    yield

app = FastAPI(
    title="OBLIVION Retro Web Search",
    description="Search the archived web — GeoCities, BBS text files, and more",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── HTML Templates ─────────────────────────────────────────────────
RETRO_CSS = """
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        background: #0a0a0f;
        color: #00ff41;
        font-family: 'Courier New', monospace;
        min-height: 100vh;
    }
    .container { max-width: 900px; margin: 0 auto; padding: 20px; }
    .header {
        text-align: center;
        padding: 30px 0;
        border-bottom: 2px dashed #333;
        margin-bottom: 30px;
    }
    .header h1 {
        font-size: 2.5em;
        color: #ff6600;
        text-shadow: 0 0 20px rgba(255,102,0,0.5);
        letter-spacing: 3px;
    }
    .header .subtitle {
        color: #888;
        margin-top: 8px;
        font-size: 0.9em;
    }
    .blink { animation: blink 1s step-end infinite; }
    @keyframes blink { 50% { opacity: 0; } }
    .search-box {
        display: flex;
        gap: 10px;
        margin: 20px 0;
    }
    .search-box input {
        flex: 1;
        padding: 12px 16px;
        background: #111;
        border: 1px solid #333;
        color: #00ff41;
        font-family: 'Courier New', monospace;
        font-size: 1.1em;
        outline: none;
    }
    .search-box input:focus { border-color: #ff6600; }
    .search-box button {
        padding: 12px 24px;
        background: #ff6600;
        color: #000;
        border: none;
        font-family: 'Courier New', monospace;
        font-weight: bold;
        font-size: 1.1em;
        cursor: pointer;
    }
    .search-box button:hover { background: #ff8833; }
    .filters {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        margin: 15px 0;
    }
    .filter-btn {
        padding: 4px 12px;
        background: transparent;
        border: 1px solid #333;
        color: #888;
        font-family: 'Courier New', monospace;
        font-size: 0.8em;
        cursor: pointer;
    }
    .filter-btn:hover, .filter-btn.active { border-color: #ff6600; color: #ff6600; }
    .result {
        border: 1px solid #222;
        padding: 15px;
        margin: 10px 0;
        background: #0d0d12;
    }
    .result:hover { border-color: #444; }
    .result .url {
        color: #ff6600;
        text-decoration: none;
        word-break: break-all;
        font-size: 0.9em;
    }
    .result .url:hover { text-decoration: underline; }
    .result .meta {
        color: #555;
        font-size: 0.8em;
        margin-top: 5px;
    }
    .result .neighborhood {
        color: #00aaff;
        font-size: 0.8em;
    }
    .hoods {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
        gap: 8px;
        margin: 20px 0;
    }
    .hood {
        padding: 8px 12px;
        border: 1px solid #222;
        background: #0d0d12;
        text-decoration: none;
    }
    .hood:hover { border-color: #ff6600; }
    .hood .name { color: #ff6600; font-weight: bold; font-size: 0.9em; }
    .hood .desc { color: #555; font-size: 0.75em; }
    .marquee-bar {
        background: #111;
        padding: 6px;
        overflow: hidden;
        border: 1px solid #222;
        margin: 10px 0;
    }
    .marquee-text {
        display: inline-block;
        animation: marquee 20s linear infinite;
        color: #ff6600;
        white-space: nowrap;
    }
    @keyframes marquee {
        0% { transform: translateX(100%); }
        100% { transform: translateX(-100%); }
    }
    .stats { color: #555; font-size: 0.85em; margin: 10px 0; }
    .footer {
        text-align: center;
        padding: 30px 0;
        border-top: 1px dashed #222;
        margin-top: 30px;
        color: #333;
        font-size: 0.8em;
    }
    .construction {
        text-align: center;
        padding: 5px;
        font-size: 0.9em;
        color: #ffff00;
    }
    .guestbook-link {
        display: inline-block;
        margin: 10px;
        padding: 6px 16px;
        border: 2px outset #888;
        background: #333;
        color: #00ff41;
        text-decoration: none;
        font-size: 0.85em;
    }
    .counter {
        text-align: center;
        padding: 5px;
        font-size: 0.8em;
        color: #888;
        border: 1px inset #444;
        display: inline-block;
        background: #000;
    }
    @media(max-width:768px){
      .header h1{font-size:1.8em}
      .hoods{grid-template-columns:repeat(2,1fr)}
      .search-box{flex-direction:column;gap:8px}
      .search-box input{width:100%}
      .search-box button{min-height:44px;width:100%}
      .filter-btn{min-height:44px;padding:8px 16px}
      .container{padding:16px}
    }
    @media(max-width:480px){
      .header h1{font-size:1.5em}
      .hoods{grid-template-columns:1fr}
      .marquee-text{animation:none;white-space:normal}
      .result .url{font-size:0.8em}
      body{font-size:14px}
    }
    @media(max-width:375px){
      .header h1{font-size:1.3em}
      .container{padding:10px}
      .result{padding:10px}
    }
</style>
"""


def render_landing_page():
    hoods_html = ""
    for name, desc in GEOCITIES_NEIGHBORHOODS.items():
        hoods_html += f'''
            <a class="hood" href="/retro/search?q={name.lower()}&source=geocities">
                <div class="name">{name}</div>
                <div class="desc">{desc}</div>
            </a>'''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OBLIVION Retro Search - The Lost Web</title>
    <meta name="description" content="Search the lost web — GeoCities, Angelfire, Tripod, BBS archives from the 1990s and 2000s. Explore 23 GeoCities neighborhoods and forgotten internet history.">
    <link rel="canonical" href="https://oblivionsearch.com/retro">
    <meta property="og:title" content="OBLIVION Retro Search — The Lost Web">
    <meta property="og:description" content="Search the lost web — GeoCities, Angelfire, Tripod, BBS archives from the 1990s and 2000s. Explore 23 GeoCities neighborhoods and forgotten internet history.">
    <meta property="og:url" content="https://oblivionsearch.com/retro">
    <meta property="og:type" content="website">
    <meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
    {RETRO_CSS}
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="construction">* * * UNDER CONSTRUCTION * * *</div>
            <h1>OBLIVION<br>RETRO SEARCH</h1>
            <div class="subtitle">Explore the lost web. GeoCities, BBS files, and forgotten homepages.</div>
        </div>

        <div class="marquee-bar">
            <span class="marquee-text">
                Welcome to OBLIVION Retro Search! Explore millions of archived web pages from the 90s and 2000s.
                Powered by the Internet Archive Wayback Machine CDX API.
                Search GeoCities neighborhoods, BBS text files, and more!
            </span>
        </div>

        <form class="search-box" action="/retro/search" method="get">
            <input type="text" name="q" placeholder="Search the archived web..." autofocus>
            <button type="submit">SEARCH</button>
        </form>

        <div class="filters">
            <button class="filter-btn active" onclick="setSource('geocities')">GeoCities</button>
            <button class="filter-btn" onclick="setSource('textfiles')">BBS Text Files</button>
            <button class="filter-btn" onclick="setSource('all')">All Archives</button>
        </div>

        <h2 style="color:#ff6600; margin:25px 0 10px;">GeoCities Neighborhoods</h2>
        <div class="hoods">{hoods_html}</div>

        <div style="text-align:center; margin:30px 0;">
            <span class="counter">You are visitor #<span id="counter">{hash(datetime.now().isoformat()) % 99999 + 10000}</span></span>
        </div>

        <div class="footer">
            <p>OBLIVION Retro Search &mdash; Because the old web was weird and wonderful</p>
            <p style="margin-top:5px;">Powered by <a href="https://oblivionsearch.com" style="color:#ff6600;">OBLIVION Search</a> &amp; Internet Archive</p>
            <p style="margin-top:5px; color:#222;">Best viewed in Netscape Navigator 4.0</p>
        </div>
    </div>

    <script>
        let currentSource = 'geocities';
        function setSource(s) {{
            currentSource = s;
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            event.target.classList.add('active');
        }}
        document.querySelector('form').addEventListener('submit', function(e) {{
            if (currentSource !== 'geocities') {{
                let input = document.createElement('input');
                input.type = 'hidden';
                input.name = 'source';
                input.value = currentSource;
                this.appendChild(input);
            }}
        }});
    </script>
</body>
</html>"""


def render_results_page(query, results, source, elapsed_ms):
    results_html = ""
    if not results:
        results_html = '<div style="color:#888; padding:20px; text-align:center;">No archived pages found. Try a different search term.</div>'
    else:
        for r in results:
            hood = ""
            if r.get("neighborhood"):
                hood = f' <span class="neighborhood">[{r["neighborhood"]}]</span>'
            rtype = r.get("type", "webpage")
            if rtype == "bbs_textfile":
                cat = r.get("category", "General")
                results_html += f'''
                    <div class="result">
                        <a class="url" href="{r['wayback_url']}" target="_blank">{r['url']}</a>
                        <div class="meta">BBS Text File | Category: {cat} | Archived: {r.get('date', '?')}</div>
                    </div>'''
            else:
                results_html += f'''
                    <div class="result">
                        <a class="url" href="{r['wayback_url']}" target="_blank">{r['url']}</a>
                        <div class="meta">
                            Archived: {r.get('date', 'Unknown')} | Year: {r.get('year', '?')} | Status: {r.get('status', '?')}{hood}
                        </div>
                    </div>'''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>"{query}" - OBLIVION Retro Search</title>
    {RETRO_CSS}
</head>
<body>
    <div class="container">
        <div style="display:flex; align-items:center; gap:15px; margin-bottom:20px;">
            <a href="/retro" style="color:#ff6600; text-decoration:none; font-size:1.5em; font-weight:bold;">OBLIVION RETRO</a>
        </div>

        <form class="search-box" action="/retro/search" method="get">
            <input type="text" name="q" value="{query}" autofocus>
            <input type="hidden" name="source" value="{source}">
            <button type="submit">SEARCH</button>
        </form>

        <div class="stats">
            Found {len(results)} archived pages for "{query}" ({source}) in {elapsed_ms:.0f}ms
        </div>

        {results_html}

        <div class="footer">
            <a href="/retro" style="color:#ff6600;">Back to Retro Search</a> |
            <a href="https://oblivionsearch.com" style="color:#ff6600;">OBLIVION Search</a>
        </div>
    </div>
</body>
</html>"""


# ── Routes ─────────────────────────────────────────────────────────
@app.get("/retro", response_class=HTMLResponse)
async def retro_landing():
    return render_landing_page()


@app.get("/retro/search", response_class=HTMLResponse)
async def retro_search_html(
    q: str = Query("", description="Search query"),
    source: str = Query("geocities", description="Source: geocities, textfiles, all"),
    limit: int = Query(50, ge=1, le=200),
    request: Request = None,
):
    if not q.strip():
        return render_landing_page()

    start = datetime.utcnow()
    results = await _do_search(q, source, limit)
    elapsed = (datetime.utcnow() - start).total_seconds() * 1000

    ip = request.client.host if request and request.client else ""
    log_search(q, source, len(results), ip)

    return render_results_page(q, results, source, elapsed)


@app.get("/api/retro/search")
async def retro_search_api(
    q: str = Query(..., description="Search query"),
    source: str = Query("geocities", description="Source: geocities, textfiles, all"),
    limit: int = Query(50, ge=1, le=200),
    request: Request = None,
):
    start = datetime.utcnow()
    results = await _do_search(q, source, limit)
    elapsed = (datetime.utcnow() - start).total_seconds() * 1000

    ip = request.client.host if request and request.client else ""
    log_search(q, source, len(results), ip)

    return {
        "query": q,
        "source": source,
        "count": len(results),
        "elapsed_ms": round(elapsed, 1),
        "results": results,
    }


@app.get("/api/retro/neighborhoods")
async def retro_neighborhoods():
    return {
        "neighborhoods": [
            {"name": k, "description": v, "search_url": f"/retro/search?q={k.lower()}&source=geocities"}
            for k, v in GEOCITIES_NEIGHBORHOODS.items()
        ]
    }


@app.get("/api/retro/stats")
async def retro_stats():
    try:
        conn = get_pg_conn(PG_DB)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT COUNT(*) as total_searches FROM retro_searches")
        total = cur.fetchone()["total_searches"]
        cur.execute("SELECT COUNT(*) as cached FROM retro_cache")
        cached = cur.fetchone()["cached"]
        cur.execute("""
            SELECT query, COUNT(*) as cnt FROM retro_searches
            GROUP BY query ORDER BY cnt DESC LIMIT 10
        """)
        top = cur.fetchall()
        cur.close()
        conn.close()
        return {"total_searches": total, "cached_queries": cached, "top_queries": top}
    except Exception as e:
        return {"error": str(e)}


@app.get("/retro/health")
async def health():
    return {"status": "ok", "service": "oblivion-retro", "port": PORT}


async def _do_search(q: str, source: str, limit: int) -> list:
    results = []
    if source in ("geocities", "all"):
        results.extend(await search_ia_cdx(q, "geocities.com", limit))
    if source in ("textfiles", "all"):
        results.extend(await search_textfiles(q, limit))
    if source == "all":
        # Also search general early web
        results.extend(await search_ia_cdx(q, "angelfire.com", min(limit, 20)))
        results.extend(await search_ia_cdx(q, "tripod.com", min(limit, 20)))
    return results


# ── Main ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
