#!/usr/bin/env python3
"""
OBLIVION Search Trends — Monetizable Data Product
FastAPI on port 3062

Public dashboard + API showing real-time search trends from OBLIVION Search.
Free tier: 100 API calls/day. Pro: £49/mo unlimited. Bulk export: £99/mo.
"""

import os, re, gzip, hashlib, time, json, math
from datetime import datetime, timedelta
from urllib.parse import unquote, urlparse, parse_qs
from collections import defaultdict, Counter
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ── Config ──────────────────────────────────────────────────────────────────
DB_NAME = "oblivion_trends"
DB_CFG = {"user": "postgres", "password": "os.environ.get("DB_PASSWORD", "change_me")", "host": "127.0.0.1", "port": 5432}
NGINX_LOG = "/var/log/nginx/access.log"
NGINX_LOG_ROTATED = "/var/log/nginx/access.log.1"
PORT = 3062

# Category keywords for auto-classification
CATEGORIES = {
    "tech": ["python", "javascript", "linux", "programming", "code", "api", "software",
             "ai", "machine learning", "docker", "kubernetes", "react", "node", "rust",
             "database", "sql", "github", "open source", "server", "cloud", "aws", "gpu"],
    "news": ["news", "breaking", "today", "latest", "update", "election", "politics",
             "government", "war", "president", "parliament", "economy", "crisis"],
    "science": ["science", "physics", "chemistry", "biology", "space", "nasa", "research",
                "climate", "quantum", "dna", "genome", "astronomy", "planet", "evolution"],
    "privacy": ["privacy", "vpn", "encryption", "tor", "anonymous", "surveillance",
                "tracking", "data breach", "secure", "pgp", "signal", "proton"],
    "finance": ["bitcoin", "crypto", "stock", "trading", "finance", "bank", "investment",
                "ethereum", "defi", "nft", "price", "market", "forex", "gold"],
    "health": ["health", "medical", "covid", "vaccine", "mental health", "fitness",
               "nutrition", "diet", "exercise", "doctor", "hospital", "symptoms"],
    "entertainment": ["movie", "music", "game", "gaming", "film", "tv", "series",
                      "anime", "spotify", "netflix", "youtube", "stream", "album"],
}

# ── Database ────────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(dbname=DB_NAME, **DB_CFG)

def ensure_db():
    """Create database and tables if they don't exist."""
    try:
        conn = psycopg2.connect(dbname="postgres", **DB_CFG)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,))
        if not cur.fetchone():
            cur.execute(f"CREATE DATABASE {DB_NAME}")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[WARN] DB creation check: {e}")

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS search_trends (
                id SERIAL PRIMARY KEY,
                query VARCHAR(500) NOT NULL,
                count INT NOT NULL DEFAULT 1,
                hour TIMESTAMP NOT NULL,
                category VARCHAR(50) DEFAULT 'general',
                UNIQUE(query, hour)
            );
            CREATE INDEX IF NOT EXISTS idx_trends_hour ON search_trends(hour);
            CREATE INDEX IF NOT EXISTS idx_trends_query ON search_trends(query);
            CREATE INDEX IF NOT EXISTS idx_trends_category ON search_trends(category);
            CREATE INDEX IF NOT EXISTS idx_trends_count ON search_trends(count DESC);

            CREATE TABLE IF NOT EXISTS trends_api_keys (
                id SERIAL PRIMARY KEY,
                api_key VARCHAR(64) UNIQUE NOT NULL,
                email VARCHAR(255),
                tier VARCHAR(20) DEFAULT 'free',
                daily_calls INT DEFAULT 0,
                daily_reset DATE DEFAULT CURRENT_DATE,
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS trends_collection_log (
                id SERIAL PRIMARY KEY,
                collected_at TIMESTAMP DEFAULT NOW(),
                log_file VARCHAR(255),
                lines_parsed INT DEFAULT 0,
                queries_found INT DEFAULT 0,
                hour_bucket TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("[OK] Database and tables ready")
    except Exception as e:
        print(f"[ERROR] Table creation: {e}")

# ── Log Parsing ─────────────────────────────────────────────────────────────

# Nginx combined log format regex
LOG_RE = re.compile(
    r'^[\d.]+ - - \[([^\]]+)\] "(\w+) ([^ ]+) [^"]*" (\d+) \d+'
)

def classify_query(q: str) -> str:
    """Auto-classify a search query into a category."""
    q_lower = q.lower()
    scores = {}
    for cat, keywords in CATEGORIES.items():
        score = sum(1 for kw in keywords if kw in q_lower)
        if score > 0:
            scores[cat] = score
    if scores:
        return max(scores, key=scores.get)
    return "general"

def parse_search_queries(log_path: str, since: Optional[datetime] = None):
    """
    Parse nginx access log for search queries.
    Returns dict of {(query, hour_bucket): count}
    Only extracts aggregated data — NO IP addresses or user agents stored.
    """
    results = defaultdict(int)

    def process_line(line):
        m = LOG_RE.match(line)
        if not m:
            return
        timestamp_str, method, path, status = m.groups()
        if method != "GET" or status == "404":
            return

        # Match search endpoints
        query = None
        if "/search" in path and "q=" in path:
            try:
                parsed = urlparse(path)
                params = parse_qs(parsed.query)
                if "q" in params:
                    query = unquote(params["q"][0]).strip()
            except Exception:
                return
        elif "/api/search" in path and "q=" in path:
            try:
                parsed = urlparse(path)
                params = parse_qs(parsed.query)
                if "q" in params:
                    query = unquote(params["q"][0]).strip()
            except Exception:
                return

        if not query or len(query) < 2 or len(query) > 500:
            return

        # Normalize: lowercase, collapse whitespace
        query = re.sub(r'\s+', ' ', query.lower()).strip()

        # Filter out obvious bot/garbage queries
        if re.match(r'^[\d./:]+$', query) or '<script' in query or 'SELECT' in query.upper():
            return
        if any(c in query for c in ['│', '|', '{', '}', '<', '>']):
            return
        if not re.match(r'^[a-zA-Z0-9\s\-\'.,:;!?@#&()+/]+$', query):
            return

        # Parse timestamp and bucket to hour
        try:
            dt = datetime.strptime(timestamp_str.split()[0], "%d/%b/%Y:%H:%M:%S")
            hour_bucket = dt.replace(minute=0, second=0, microsecond=0)
        except Exception:
            return

        if since and dt < since:
            return

        results[(query, hour_bucket)] += 1

    # Read current log
    try:
        with open(log_path, 'r', errors='replace') as f:
            for line in f:
                process_line(line)
    except FileNotFoundError:
        pass

    return results

def collect_and_store(hours_back: int = 1):
    """Collect search queries from last N hours and store aggregated trends."""
    since = datetime.now() - timedelta(hours=hours_back)
    all_results = defaultdict(int)

    # Parse current and rotated log
    for log_path in [NGINX_LOG, NGINX_LOG_ROTATED]:
        results = parse_search_queries(log_path, since=since)
        for key, count in results.items():
            all_results[key] += count

    if not all_results:
        print(f"[INFO] No search queries found in last {hours_back} hour(s)")
        return 0

    conn = get_conn()
    cur = conn.cursor()
    inserted = 0

    for (query, hour_bucket), count in all_results.items():
        category = classify_query(query)
        try:
            cur.execute("""
                INSERT INTO search_trends (query, count, hour, category)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (query, hour)
                DO UPDATE SET count = GREATEST(search_trends.count, EXCLUDED.count)
            """, (query, count, hour_bucket, category))
            inserted += 1
        except Exception as e:
            print(f"[WARN] Insert error: {e}")
            conn.rollback()

    # Log collection run
    cur.execute("""
        INSERT INTO trends_collection_log (log_file, lines_parsed, queries_found, hour_bucket)
        VALUES (%s, %s, %s, %s)
    """, (NGINX_LOG, 0, inserted, since.replace(minute=0, second=0, microsecond=0)))

    conn.commit()
    cur.close()
    conn.close()
    print(f"[OK] Stored {inserted} trend entries")
    return inserted

# ── FastAPI App ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="OBLIVION Search Trends",
    description="Real-time search trend data from OBLIVION Search Engine",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Rate Limiting (simple in-memory) ────────────────────────────────────────
rate_limits = defaultdict(lambda: {"count": 0, "reset": time.time() + 86400})
FREE_DAILY_LIMIT = 100

def check_rate_limit(request: Request, api_key: Optional[str] = None):
    """Check API rate limit. Returns True if allowed."""
    if api_key:
        # Check DB for pro keys
        try:
            conn = get_conn()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT tier FROM trends_api_keys WHERE api_key=%s", (api_key,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row and row["tier"] in ("pro", "bulk"):
                return True
        except Exception:
            pass

    # IP-based free tier
    client_ip = request.headers.get("x-real-ip", request.client.host)
    ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:16]

    bucket = rate_limits[ip_hash]
    if time.time() > bucket["reset"]:
        bucket["count"] = 0
        bucket["reset"] = time.time() + 86400

    bucket["count"] += 1
    if bucket["count"] > FREE_DAILY_LIMIT:
        return False
    return True

# ── API Routes ──────────────────────────────────────────────────────────────

@app.get("/api/trends", response_class=JSONResponse)
async def get_trending(
    request: Request,
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(50, ge=1, le=200),
    api_key: Optional[str] = Query(None)
):
    """Top trending queries over the last N hours."""
    if not check_rate_limit(request, api_key):
        raise HTTPException(429, detail="Rate limit exceeded. Upgrade to Pro: £49/mo for unlimited access.")

    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        since = datetime.now() - timedelta(hours=hours)
        cur.execute("""
            SELECT query, category, SUM(count) as total_count,
                   COUNT(DISTINCT hour) as active_hours,
                   MAX(hour) as last_seen
            FROM search_trends
            WHERE hour >= %s
            GROUP BY query, category
            ORDER BY total_count DESC
            LIMIT %s
        """, (since, limit))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        trends = []
        for i, r in enumerate(rows):
            trends.append({
                "rank": i + 1,
                "query": r["query"],
                "category": r["category"],
                "search_count": r["total_count"],
                "active_hours": r["active_hours"],
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            })

        return {
            "status": "ok",
            "period_hours": hours,
            "count": len(trends),
            "trends": trends,
            "generated_at": datetime.now().isoformat(),
            "source": "OBLIVION Search Engine",
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "trends": []}

@app.get("/api/trends/query", response_class=JSONResponse)
async def get_query_trend(
    request: Request,
    q: str = Query(..., min_length=1),
    days: int = Query(7, ge=1, le=30),
    api_key: Optional[str] = Query(None)
):
    """Hourly trend data for a specific search term."""
    if not check_rate_limit(request, api_key):
        raise HTTPException(429, detail="Rate limit exceeded. Upgrade to Pro: £49/mo.")

    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        since = datetime.now() - timedelta(days=days)
        cur.execute("""
            SELECT hour, count, category
            FROM search_trends
            WHERE query = %s AND hour >= %s
            ORDER BY hour ASC
        """, (q.lower().strip(), since))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        data_points = [
            {"hour": r["hour"].isoformat(), "count": r["count"]}
            for r in rows
        ]
        total = sum(r["count"] for r in rows)

        return {
            "status": "ok",
            "query": q,
            "category": rows[0]["category"] if rows else classify_query(q),
            "period_days": days,
            "total_searches": total,
            "data_points": data_points,
            "generated_at": datetime.now().isoformat(),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/trends/rising", response_class=JSONResponse)
async def get_rising_trends(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    api_key: Optional[str] = Query(None)
):
    """Fastest rising queries — comparing last 6h vs previous 6h."""
    if not check_rate_limit(request, api_key):
        raise HTTPException(429, detail="Rate limit exceeded.")

    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        now = datetime.now()
        recent_start = now - timedelta(hours=6)
        prev_start = now - timedelta(hours=12)

        cur.execute("""
            WITH recent AS (
                SELECT query, category, SUM(count) as recent_count
                FROM search_trends
                WHERE hour >= %s
                GROUP BY query, category
            ),
            previous AS (
                SELECT query, SUM(count) as prev_count
                FROM search_trends
                WHERE hour >= %s AND hour < %s
                GROUP BY query
            )
            SELECT r.query, r.category, r.recent_count,
                   COALESCE(p.prev_count, 0) as prev_count,
                   CASE
                       WHEN COALESCE(p.prev_count, 0) = 0 THEN r.recent_count * 100
                       ELSE ROUND(((r.recent_count::float - p.prev_count) / p.prev_count * 100)::numeric, 1)
                   END as growth_pct
            FROM recent r
            LEFT JOIN previous p ON r.query = p.query
            WHERE r.recent_count >= 2
            ORDER BY growth_pct DESC
            LIMIT %s
        """, (recent_start, prev_start, recent_start, limit))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        rising = []
        for i, r in enumerate(rows):
            rising.append({
                "rank": i + 1,
                "query": r["query"],
                "category": r["category"],
                "recent_count": r["recent_count"],
                "previous_count": r["prev_count"],
                "growth_percent": float(r["growth_pct"]),
            })

        return {
            "status": "ok",
            "comparison": "last 6h vs previous 6h",
            "count": len(rising),
            "rising": rising,
            "generated_at": datetime.now().isoformat(),
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "rising": []}

@app.get("/api/trends/categories", response_class=JSONResponse)
async def get_category_trends(
    request: Request,
    hours: int = Query(24, ge=1, le=168),
    api_key: Optional[str] = Query(None)
):
    """Trending queries grouped by category."""
    if not check_rate_limit(request, api_key):
        raise HTTPException(429, detail="Rate limit exceeded.")

    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        since = datetime.now() - timedelta(hours=hours)

        cur.execute("""
            SELECT category, query, SUM(count) as total_count
            FROM search_trends
            WHERE hour >= %s
            GROUP BY category, query
            ORDER BY category, total_count DESC
        """, (since,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        categories = defaultdict(list)
        cat_totals = defaultdict(int)
        for r in rows:
            cat = r["category"]
            cat_totals[cat] += r["total_count"]
            if len(categories[cat]) < 10:  # Top 10 per category
                categories[cat].append({
                    "query": r["query"],
                    "count": r["total_count"],
                })

        result = []
        for cat in sorted(cat_totals, key=cat_totals.get, reverse=True):
            result.append({
                "category": cat,
                "total_searches": cat_totals[cat],
                "top_queries": categories[cat],
            })

        return {
            "status": "ok",
            "period_hours": hours,
            "categories": result,
            "generated_at": datetime.now().isoformat(),
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "categories": []}

@app.get("/api/trends/export", response_class=JSONResponse)
async def export_trends(
    request: Request,
    days: int = Query(7, ge=1, le=30),
    api_key: str = Query(...)
):
    """Bulk data export (Pro/Bulk tier only)."""
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT tier FROM trends_api_keys WHERE api_key=%s", (api_key,))
        row = cur.fetchone()
        if not row or row["tier"] not in ("pro", "bulk"):
            cur.close()
            conn.close()
            raise HTTPException(403, detail="Bulk export requires Pro (£49/mo) or Bulk (£99/mo) plan.")

        since = datetime.now() - timedelta(days=days)
        cur.execute("""
            SELECT query, category, hour, count
            FROM search_trends
            WHERE hour >= %s
            ORDER BY hour DESC, count DESC
        """, (since,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        data = []
        for r in rows:
            data.append({
                "query": r["query"],
                "category": r["category"],
                "hour": r["hour"].isoformat(),
                "count": r["count"],
            })

        return {
            "status": "ok",
            "period_days": days,
            "total_records": len(data),
            "data": data,
        }
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ── Dashboard HTML ──────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OBLIVION Search Trends — Real-Time Search Intelligence</title>
<meta name="description" content="See what the world is searching for on OBLIVION. Real-time search trends, rising queries, and category insights.">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0f;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh}
a{color:#7c5cff;text-decoration:none}a:hover{text-decoration:underline}

.header{background:linear-gradient(135deg,#0d0d1a 0%,#1a0a2e 100%);border-bottom:1px solid #1a1a2e;padding:20px 0}
.header-inner{max-width:1200px;margin:0 auto;padding:0 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:16px}
.logo{font-size:24px;font-weight:800;background:linear-gradient(135deg,#7c5cff,#00d4ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.logo span{font-weight:400;font-size:16px;color:#888}
.header-actions{display:flex;gap:12px;align-items:center}
.btn{padding:8px 20px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;border:none;transition:all 0.2s}
.btn-primary{background:linear-gradient(135deg,#7c5cff,#5a3fd4);color:#fff}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 4px 15px rgba(124,92,255,0.3)}
.btn-outline{background:transparent;border:1px solid #333;color:#ccc}
.btn-outline:hover{border-color:#7c5cff;color:#7c5cff}

.container{max-width:1200px;margin:0 auto;padding:24px}

.search-box{margin:24px 0;position:relative}
.search-box input{width:100%;padding:16px 20px 16px 50px;background:#12121f;border:1px solid #1a1a2e;border-radius:12px;color:#fff;font-size:16px;outline:none;transition:border 0.2s}
.search-box input:focus{border-color:#7c5cff}
.search-box .icon{position:absolute;left:18px;top:50%;transform:translateY(-50%);color:#555;font-size:18px}

.stats-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:32px}
.stat-card{background:#12121f;border:1px solid #1a1a2e;border-radius:12px;padding:20px;text-align:center}
.stat-card .value{font-size:32px;font-weight:800;background:linear-gradient(135deg,#7c5cff,#00d4ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stat-card .label{color:#888;font-size:13px;margin-top:4px}

.section-title{font-size:20px;font-weight:700;margin-bottom:16px;display:flex;align-items:center;gap:10px}
.section-title .badge{background:#7c5cff22;color:#7c5cff;font-size:11px;padding:4px 10px;border-radius:20px;font-weight:600}

.trends-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:40px}
@media(max-width:768px){.trends-grid{grid-template-columns:1fr}}

.trend-card{background:#12121f;border:1px solid #1a1a2e;border-radius:12px;padding:16px 20px;display:flex;align-items:center;gap:16px;transition:border 0.2s;cursor:pointer}
.trend-card:hover{border-color:#7c5cff}
.trend-rank{font-size:18px;font-weight:800;color:#333;min-width:30px;text-align:center}
.trend-info{flex:1;min-width:0}
.trend-query{font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trend-meta{font-size:12px;color:#666;margin-top:2px;display:flex;gap:12px;align-items:center}
.trend-cat{background:#7c5cff15;color:#7c5cff;padding:2px 8px;border-radius:4px;font-size:11px}
.trend-sparkline{width:80px;height:30px}
.trend-sparkline svg{width:100%;height:100%}
.trend-count{font-size:14px;font-weight:700;color:#00d4ff;min-width:50px;text-align:right}

.rising-tag{color:#00ff88;font-size:12px;font-weight:700}
.rising-tag::before{content:"▲ "}

.categories-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-bottom:40px}
.cat-card{background:#12121f;border:1px solid #1a1a2e;border-radius:12px;padding:20px;transition:border 0.2s}
.cat-card:hover{border-color:#7c5cff}
.cat-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.cat-name{font-size:16px;font-weight:700;text-transform:capitalize}
.cat-total{color:#888;font-size:13px}
.cat-queries{list-style:none}
.cat-queries li{padding:6px 0;border-bottom:1px solid #1a1a2e;font-size:14px;display:flex;justify-content:space-between}
.cat-queries li:last-child{border:none}
.cat-queries .cnt{color:#7c5cff;font-weight:600}

.query-detail{background:#12121f;border:1px solid #1a1a2e;border-radius:12px;padding:24px;margin-bottom:32px;display:none}
.query-detail.active{display:block}
.query-detail h3{font-size:22px;margin-bottom:8px}
.query-chart{width:100%;height:200px;margin-top:16px}

.pricing{background:#12121f;border:1px solid #1a1a2e;border-radius:16px;padding:32px;margin:40px 0}
.pricing h2{font-size:22px;font-weight:700;margin-bottom:8px}
.pricing p{color:#888;margin-bottom:24px}
.pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px}
.price-card{background:#0a0a15;border:1px solid #1a1a2e;border-radius:12px;padding:24px;text-align:center}
.price-card.featured{border-color:#7c5cff;position:relative}
.price-card.featured::before{content:"POPULAR";position:absolute;top:-10px;left:50%;transform:translateX(-50%);background:#7c5cff;color:#fff;font-size:11px;padding:2px 12px;border-radius:10px;font-weight:700}
.price-name{font-size:18px;font-weight:700;margin-bottom:8px}
.price-amount{font-size:36px;font-weight:800;margin-bottom:4px}
.price-amount span{font-size:16px;font-weight:400;color:#888}
.price-features{list-style:none;text-align:left;margin:16px 0;font-size:14px;color:#aaa}
.price-features li{padding:6px 0}
.price-features li::before{content:"✓ ";color:#00d4ff}

.footer{text-align:center;padding:40px 24px;color:#555;font-size:13px;border-top:1px solid #1a1a2e}
.auto-refresh{color:#444;font-size:12px;margin-top:8px}
</style>
</head>
<body>

<div class="header">
<div class="header-inner">
  <div class="logo">OBLIVION <span>Search Trends</span></div>
  <div class="header-actions">
    <a href="https://oblivionsearch.com" class="btn btn-outline">Back to Search</a>
    <a href="#pricing" class="btn btn-primary">Get API Access</a>
  </div>
</div>
</div>

<div class="container">

<div class="search-box">
  <span class="icon">&#128269;</span>
  <input type="text" id="searchInput" placeholder="Look up trend for any search term..." autocomplete="off">
</div>

<div class="query-detail" id="queryDetail">
  <h3 id="queryDetailTitle"></h3>
  <div style="display:flex;gap:20px;flex-wrap:wrap">
    <div><span style="color:#888">Category:</span> <span id="queryDetailCat" class="trend-cat"></span></div>
    <div><span style="color:#888">Total searches:</span> <strong id="queryDetailTotal"></strong></div>
    <div><span style="color:#888">Period:</span> Last 7 days</div>
  </div>
  <div class="query-chart"><canvas id="queryChart"></canvas></div>
</div>

<div class="stats-row" id="statsRow">
  <div class="stat-card"><div class="value" id="statTotal">—</div><div class="label">Searches Tracked (24h)</div></div>
  <div class="stat-card"><div class="value" id="statUnique">—</div><div class="label">Unique Queries</div></div>
  <div class="stat-card"><div class="value" id="statRising">—</div><div class="label">Rising Trends</div></div>
  <div class="stat-card"><div class="value" id="statCats">—</div><div class="label">Categories</div></div>
</div>

<div class="section-title">Top Trending Searches <span class="badge">LIVE — Last 24h</span></div>
<div class="trends-grid" id="trendsGrid"></div>

<div class="section-title">Fastest Rising <span class="badge">6h comparison</span></div>
<div class="trends-grid" id="risingGrid"></div>

<div class="section-title">Trending by Category</div>
<div class="categories-grid" id="categoriesGrid"></div>

<div class="pricing" id="pricing">
  <h2>OBLIVION Trends API</h2>
  <p>Integrate real-time search trend data into your applications, research, or trading algorithms.</p>
  <div class="pricing-grid">
    <div class="price-card">
      <div class="price-name">Free</div>
      <div class="price-amount">£0<span>/mo</span></div>
      <ul class="price-features">
        <li>100 API calls/day</li>
        <li>Top 50 trending queries</li>
        <li>Rising trends</li>
        <li>Category breakdown</li>
        <li>JSON responses</li>
      </ul>
      <a href="/api/trends" class="btn btn-outline" style="display:block;text-align:center;margin-top:12px">Try Now</a>
    </div>
    <div class="price-card featured">
      <div class="price-name">Pro</div>
      <div class="price-amount">£49<span>/mo</span></div>
      <ul class="price-features">
        <li>Unlimited API calls</li>
        <li>Hourly granularity</li>
        <li>Historical data (30 days)</li>
        <li>Query-level trends</li>
        <li>Priority support</li>
      </ul>
      <a href="mailto:api@oblivionsearch.com?subject=Trends%20Pro%20Access" class="btn btn-primary" style="display:block;text-align:center;margin-top:12px">Get Pro</a>
    </div>
    <div class="price-card">
      <div class="price-name">Bulk Export</div>
      <div class="price-amount">£99<span>/mo</span></div>
      <ul class="price-features">
        <li>Everything in Pro</li>
        <li>Full data export (JSON/CSV)</li>
        <li>30-day historical dump</li>
        <li>Webhook notifications</li>
        <li>Dedicated support</li>
      </ul>
      <a href="mailto:api@oblivionsearch.com?subject=Trends%20Bulk%20Export" class="btn btn-outline" style="display:block;text-align:center;margin-top:12px">Contact Us</a>
    </div>
  </div>
</div>

</div>

<div class="footer">
  <div>&copy; 2026 OBLIVION Search — Search Trends Data Product</div>
  <div class="auto-refresh">Dashboard auto-refreshes every 5 minutes &bull; Data collected hourly</div>
</div>

<script>
const API = '';

function genSparkline(count, maxCount) {
    // Generate a simple pseudo-sparkline based on count
    const bars = 8;
    const h = 30;
    const w = 80;
    const barW = w / bars - 1;
    let svg = `<svg viewBox="0 0 ${w} ${h}" xmlns="http://www.w3.org/2000/svg">`;
    const ratio = Math.min(count / Math.max(maxCount, 1), 1);
    for (let i = 0; i < bars; i++) {
        const frac = (0.3 + Math.random() * 0.4) * ratio + (i / bars) * ratio * 0.5;
        const bh = Math.max(2, frac * h);
        svg += `<rect x="${i*(barW+1)}" y="${h-bh}" width="${barW}" height="${bh}" rx="1" fill="url(#g)"/>`;
    }
    svg += `<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#7c5cff"/><stop offset="100%" stop-color="#00d4ff"/></linearGradient></defs></svg>`;
    return svg;
}

async function loadTrends() {
    try {
        const res = await fetch(API + '/api/trends?hours=24&limit=20');
        const data = await res.json();
        const grid = document.getElementById('trendsGrid');
        const maxCount = data.trends.length > 0 ? data.trends[0].search_count : 1;

        if (data.trends.length === 0) {
            grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:#555;padding:40px">No trend data yet. Data collection runs hourly — trends will appear as searches are made.</div>';
        } else {
            grid.innerHTML = data.trends.map(t => `
                <div class="trend-card" onclick="lookupTerm('${t.query.replace(/'/g,"\\\\'")}')">
                    <div class="trend-rank">${t.rank}</div>
                    <div class="trend-info">
                        <div class="trend-query">${escHtml(t.query)}</div>
                        <div class="trend-meta">
                            <span class="trend-cat">${t.category}</span>
                            <span>${t.active_hours}h active</span>
                        </div>
                    </div>
                    <div class="trend-sparkline">${genSparkline(t.search_count, maxCount)}</div>
                    <div class="trend-count">${formatNum(t.search_count)}</div>
                </div>
            `).join('');
        }

        // Stats
        const totalSearches = data.trends.reduce((a, t) => a + t.search_count, 0);
        document.getElementById('statTotal').textContent = formatNum(totalSearches);
        document.getElementById('statUnique').textContent = formatNum(data.count);
    } catch(e) { console.error('Trends load error:', e); }
}

async function loadRising() {
    try {
        const res = await fetch(API + '/api/trends/rising?limit=10');
        const data = await res.json();
        const grid = document.getElementById('risingGrid');

        if (!data.rising || data.rising.length === 0) {
            grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:#555;padding:40px">Rising trends will appear after sufficient data collection.</div>';
            document.getElementById('statRising').textContent = '0';
        } else {
            grid.innerHTML = data.rising.map(t => `
                <div class="trend-card" onclick="lookupTerm('${t.query.replace(/'/g,"\\\\'")}')">
                    <div class="trend-rank">${t.rank}</div>
                    <div class="trend-info">
                        <div class="trend-query">${escHtml(t.query)}</div>
                        <div class="trend-meta">
                            <span class="trend-cat">${t.category}</span>
                            <span class="rising-tag">${t.growth_percent > 999 ? 'NEW' : t.growth_percent + '%'}</span>
                        </div>
                    </div>
                    <div class="trend-count">${formatNum(t.recent_count)}</div>
                </div>
            `).join('');
            document.getElementById('statRising').textContent = data.rising.length;
        }
    } catch(e) { console.error('Rising load error:', e); }
}

async function loadCategories() {
    try {
        const res = await fetch(API + '/api/trends/categories?hours=24');
        const data = await res.json();
        const grid = document.getElementById('categoriesGrid');

        if (!data.categories || data.categories.length === 0) {
            grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:#555;padding:40px">Category data will appear after initial data collection.</div>';
            document.getElementById('statCats').textContent = '0';
        } else {
            grid.innerHTML = data.categories.map(c => `
                <div class="cat-card">
                    <div class="cat-header">
                        <div class="cat-name">${c.category}</div>
                        <div class="cat-total">${formatNum(c.total_searches)} searches</div>
                    </div>
                    <ul class="cat-queries">
                        ${c.top_queries.slice(0, 5).map(q => `
                            <li><span onclick="lookupTerm('${q.query.replace(/'/g,"\\\\'")}');return false" style="cursor:pointer;color:#e0e0e0">${escHtml(q.query)}</span><span class="cnt">${formatNum(q.count)}</span></li>
                        `).join('')}
                    </ul>
                </div>
            `).join('');
            document.getElementById('statCats').textContent = data.categories.length;
        }
    } catch(e) { console.error('Categories load error:', e); }
}

async function lookupTerm(q) {
    const detail = document.getElementById('queryDetail');
    detail.classList.add('active');
    document.getElementById('queryDetailTitle').textContent = '"' + q + '"';
    document.getElementById('queryDetailCat').textContent = 'loading...';
    document.getElementById('queryDetailTotal').textContent = '...';
    detail.scrollIntoView({behavior:'smooth', block:'start'});

    try {
        const res = await fetch(API + '/api/trends/query?q=' + encodeURIComponent(q) + '&days=7');
        const data = await res.json();
        document.getElementById('queryDetailCat').textContent = data.category;
        document.getElementById('queryDetailTotal').textContent = formatNum(data.total_searches);

        // Draw chart
        const canvas = document.getElementById('queryChart');
        const ctx = canvas.getContext('2d');
        canvas.width = canvas.parentElement.offsetWidth;
        canvas.height = 200;
        ctx.clearRect(0, 0, canvas.width, canvas.height);

        if (data.data_points && data.data_points.length > 0) {
            const pts = data.data_points;
            const maxV = Math.max(...pts.map(p=>p.count), 1);
            const stepX = canvas.width / Math.max(pts.length - 1, 1);

            // Grid
            ctx.strokeStyle = '#1a1a2e';
            ctx.lineWidth = 1;
            for (let y = 0; y < 5; y++) {
                const yp = 20 + (y * (canvas.height - 40) / 4);
                ctx.beginPath(); ctx.moveTo(0, yp); ctx.lineTo(canvas.width, yp); ctx.stroke();
            }

            // Gradient fill
            const grad = ctx.createLinearGradient(0, 0, 0, canvas.height);
            grad.addColorStop(0, 'rgba(124,92,255,0.3)');
            grad.addColorStop(1, 'rgba(124,92,255,0)');

            ctx.beginPath();
            ctx.moveTo(0, canvas.height - 20);
            pts.forEach((p, i) => {
                const x = i * stepX;
                const y = 20 + (1 - p.count / maxV) * (canvas.height - 40);
                if (i === 0) ctx.lineTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.lineTo((pts.length-1)*stepX, canvas.height-20);
            ctx.fillStyle = grad; ctx.fill();

            // Line
            ctx.beginPath();
            const lineGrad = ctx.createLinearGradient(0,0,canvas.width,0);
            lineGrad.addColorStop(0, '#7c5cff');
            lineGrad.addColorStop(1, '#00d4ff');
            ctx.strokeStyle = lineGrad;
            ctx.lineWidth = 2;
            pts.forEach((p, i) => {
                const x = i * stepX;
                const y = 20 + (1 - p.count / maxV) * (canvas.height - 40);
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.stroke();
        } else {
            ctx.fillStyle = '#555';
            ctx.font = '14px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('No data points yet for this query', canvas.width/2, 100);
        }
    } catch(e) {
        console.error('Query lookup error:', e);
        document.getElementById('queryDetailCat').textContent = 'error';
    }
}

function formatNum(n) {
    if (n >= 1000000) return (n/1000000).toFixed(1)+'M';
    if (n >= 1000) return (n/1000).toFixed(1)+'K';
    return String(n);
}

function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Search input
const searchInput = document.getElementById('searchInput');
let debounce;
searchInput.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
        const q = searchInput.value.trim();
        if (q.length >= 2) lookupTerm(q);
    }, 500);
});
searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        const q = searchInput.value.trim();
        if (q.length >= 2) lookupTerm(q);
    }
});

// Initial load
loadTrends();
loadRising();
loadCategories();

// Auto-refresh every 5 minutes
setInterval(() => { loadTrends(); loadRising(); loadCategories(); }, 300000);
</script>
</body>
</html>"""

@app.get("/trends", response_class=HTMLResponse)
async def trends_dashboard():
    """Public trends dashboard."""
    return DASHBOARD_HTML

# ── CLI: Data Collection Mode ───────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    ensure_db()

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "collect":
        # Run as data collection script
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        ensure_db()
        count = collect_and_store(hours_back=hours)
        print(f"Collection complete: {count} entries")
        # Also do initial backfill from current log if DB is empty
        if hours == 1:
            try:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM search_trends")
                total = cur.fetchone()[0]
                cur.close()
                conn.close()
                if total == 0:
                    print("DB empty, running 168h backfill...")
                    collect_and_store(hours_back=168)
            except Exception:
                pass
    else:
        uvicorn.run(app, host="0.0.0.0", port=PORT)
