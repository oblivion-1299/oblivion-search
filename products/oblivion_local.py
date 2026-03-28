#!/usr/bin/env python3
"""
OBLIVION Local Search — GeoNames-powered local/geographic search (port 3065)

Provides geographic search, nearby places, and city information pages
using a PostgreSQL database loaded from GeoNames data (12M+ places).

Endpoints:
  GET /local                          — landing page with map/search
  GET /api/local/search?q=london      — search places by name
  GET /api/local/nearby?lat=51.5&lon=-0.1&radius=50 — nearby places
  GET /api/local/city/{name}          — city info page (HTML)
  GET /api/local/city/{name}/json     — city info (JSON)
  GET /api/local/countries             — list countries with counts
  GET /api/local/stats                 — database statistics
  GET /health                          — health check
"""

import asyncio
import json
import logging
import math
import os
import secrets
import smtplib
from email.mime.text import MIMEText
from datetime import date
from typing import Optional

import asyncpg
import stripe
import uvicorn
from fastapi import FastAPI, Query, Request, Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# ---------------------------------------------------------------------------
# Stripe config
# ---------------------------------------------------------------------------
stripe.api_key = "os.environ.get("STRIPE_SECRET_KEY", "")"
DOMAIN = "https://oblivionsearch.com"
PRODUCT_NAME = "oblivion_local"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "os.environ.get("SMTP_USER", "")"
SMTP_PASS = "os.environ.get("SMTP_PASS", "")"

LOCAL_PLANS = {
    "api": {"name": "API", "price_amount": 1400, "currency": "gbp", "label": "£14/mo", "req_limit": 10000},
    "api_pro": {"name": "API Pro", "price_amount": 3900, "currency": "gbp", "label": "£39/mo", "req_limit": 0},
}

_saas_pool: Optional[asyncpg.Pool] = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.getenv("LOCAL_PORT", "3065"))
PG_DSN = os.getenv(
    "LOCAL_PG_DSN",
    "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/oblivion_geonames"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("oblivion-local")

# ---------------------------------------------------------------------------
# Database pool
# ---------------------------------------------------------------------------

_pool: Optional[asyncpg.Pool] = None

# ---------------------------------------------------------------------------
# Country code to name mapping (common ones)
# ---------------------------------------------------------------------------

COUNTRY_NAMES = {
    "US": "United States", "GB": "United Kingdom", "CA": "Canada",
    "AU": "Australia", "DE": "Germany", "FR": "France", "JP": "Japan",
    "CN": "China", "IN": "India", "BR": "Brazil", "RU": "Russia",
    "IT": "Italy", "ES": "Spain", "MX": "Mexico", "KR": "South Korea",
    "NL": "Netherlands", "SE": "Sweden", "NO": "Norway", "DK": "Denmark",
    "FI": "Finland", "PL": "Poland", "PT": "Portugal", "AT": "Austria",
    "CH": "Switzerland", "BE": "Belgium", "IE": "Ireland", "NZ": "New Zealand",
    "ZA": "South Africa", "AR": "Argentina", "CL": "Chile", "CO": "Colombia",
    "EG": "Egypt", "NG": "Nigeria", "KE": "Kenya", "IL": "Israel",
    "AE": "United Arab Emirates", "SA": "Saudi Arabia", "TH": "Thailand",
    "VN": "Vietnam", "PH": "Philippines", "ID": "Indonesia", "MY": "Malaysia",
    "SG": "Singapore", "TW": "Taiwan", "HK": "Hong Kong", "TR": "Turkey",
    "GR": "Greece", "CZ": "Czech Republic", "HU": "Hungary", "RO": "Romania",
    "UA": "Ukraine", "PK": "Pakistan", "BD": "Bangladesh", "LK": "Sri Lanka",
    "PE": "Peru", "VE": "Venezuela", "EC": "Ecuador", "UY": "Uruguay",
}

FEATURE_DESCRIPTIONS = {
    "PPL": "populated place", "PPLA": "seat of first-order admin division",
    "PPLA2": "seat of second-order admin division", "PPLC": "capital of a country",
    "PPLA3": "seat of third-order admin division", "PPLA4": "seat of fourth-order admin division",
    "PPLX": "section of populated place", "PPLS": "populated places",
    "ADM1": "first-order admin division", "ADM2": "second-order admin division",
    "PCLI": "independent political entity", "MT": "mountain", "MTS": "mountains",
    "LK": "lake", "STM": "stream", "ISL": "island", "AIRP": "airport",
    "PRK": "park", "UNIV": "university", "CH": "church", "MUS": "museum",
}


def country_name(code: str) -> str:
    return COUNTRY_NAMES.get(code, code)


def feature_desc(code: str) -> str:
    return FEATURE_DESCRIPTIONS.get(code, code)


def format_population(pop: int) -> str:
    if pop >= 1_000_000_000:
        return f"{pop / 1_000_000_000:.1f}B"
    if pop >= 1_000_000:
        return f"{pop / 1_000_000:.1f}M"
    if pop >= 1_000:
        return f"{pop / 1_000:.1f}K"
    return str(pop) if pop else "N/A"


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool, _saas_pool
    _pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=20)
    _saas_pool = await asyncpg.create_pool(
        "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/postgres", min_size=1, max_size=5
    )
    log.info("OBLIVION Local Search starting on port %d", PORT)

    async with _pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM geonames")
        log.info("GeoNames database: %s places loaded", f"{count:,}")

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

    yield
    await _pool.close()
    await _saas_pool.close()


app = FastAPI(
    title="OBLIVION Local Search",
    description="GeoNames-powered geographic search for OBLIVION",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# HTML Templates
# ---------------------------------------------------------------------------

DARK_CSS = """
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
        background: #0a0a0a; color: #e0e0e0; min-height: 100vh;
    }
    a { color: #8ab4f8; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .container { max-width: 1100px; margin: 0 auto; padding: 20px; }
    .header {
        text-align: center; padding: 40px 20px 20px;
    }
    .header h1 {
        font-size: 2.5rem; font-weight: 700;
        background: linear-gradient(135deg, #8ab4f8, #c084fc);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        margin-bottom: 8px;
    }
    .header p { color: #888; font-size: 1.1rem; }
    .search-box {
        display: flex; gap: 8px; max-width: 600px; margin: 30px auto;
    }
    .search-box input {
        flex: 1; padding: 14px 20px; border-radius: 12px;
        border: 1px solid #333; background: #1a1a1a; color: #e0e0e0;
        font-size: 1rem; outline: none; transition: border-color 0.2s;
    }
    .search-box input:focus { border-color: #8ab4f8; }
    .search-box button {
        padding: 14px 28px; border-radius: 12px; border: none;
        background: linear-gradient(135deg, #8ab4f8, #c084fc);
        color: #000; font-weight: 600; cursor: pointer; font-size: 1rem;
        transition: opacity 0.2s;
    }
    .search-box button:hover { opacity: 0.85; }
    .results { margin-top: 20px; }
    .place-card {
        background: #1a1a1a; border-radius: 12px; padding: 20px;
        margin-bottom: 12px; border: 1px solid #222;
        transition: border-color 0.2s;
    }
    .place-card:hover { border-color: #444; }
    .place-card h3 { font-size: 1.2rem; margin-bottom: 4px; }
    .place-card .meta { color: #888; font-size: 0.9rem; margin-bottom: 8px; }
    .place-card .stats {
        display: flex; gap: 20px; font-size: 0.85rem; color: #aaa;
    }
    .place-card .stats span { display: flex; align-items: center; gap: 4px; }
    .badge {
        display: inline-block; padding: 2px 8px; border-radius: 4px;
        font-size: 0.75rem; font-weight: 600; background: #2a2a3a; color: #8ab4f8;
    }
    .city-header {
        background: #1a1a1a; border-radius: 16px; padding: 30px;
        margin-bottom: 20px; border: 1px solid #222;
    }
    .city-header h1 {
        font-size: 2rem; margin-bottom: 8px;
        background: linear-gradient(135deg, #8ab4f8, #c084fc);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }
    .city-header .desc { color: #aaa; font-size: 1.1rem; margin-bottom: 15px; }
    .fact-grid {
        display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
        gap: 12px; margin-top: 15px;
    }
    .fact-item {
        background: #222; border-radius: 8px; padding: 12px;
    }
    .fact-item .label { color: #888; font-size: 0.8rem; text-transform: uppercase; }
    .fact-item .value { font-size: 1.1rem; font-weight: 600; margin-top: 4px; }
    .nearby-section { margin-top: 30px; }
    .nearby-section h2 { font-size: 1.3rem; margin-bottom: 15px; color: #ccc; }
    .map-container {
        width: 100%; height: 300px; border-radius: 12px; overflow: hidden;
        margin: 20px 0; border: 1px solid #333;
    }
    .map-container iframe { width: 100%; height: 100%; border: none; }
    .stats-bar {
        display: flex; justify-content: center; gap: 30px; padding: 15px;
        background: #111; border-radius: 10px; margin: 20px 0;
    }
    .stats-bar .stat { text-align: center; }
    .stats-bar .stat .num { font-size: 1.5rem; font-weight: 700; color: #8ab4f8; }
    .stats-bar .stat .lbl { font-size: 0.8rem; color: #888; }
    .footer {
        text-align: center; padding: 30px; color: #555; font-size: 0.85rem;
        border-top: 1px solid #222; margin-top: 40px;
    }
    .popular-grid {
        display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
        gap: 10px; margin-top: 15px;
    }
    .popular-item {
        background: #1a1a1a; border-radius: 8px; padding: 12px 16px;
        border: 1px solid #222; display: flex; justify-content: space-between;
        align-items: center;
    }
    .popular-item .pop { color: #888; font-size: 0.85rem; }
    @media (max-width: 600px) {
        .header h1 { font-size: 1.8rem; }
        .fact-grid { grid-template-columns: 1fr 1fr; }
        .stats-bar { flex-wrap: wrap; gap: 15px; }
    }
    @media (max-width: 480px) {
        body { overflow-x: hidden; }
        .container { padding: 12px; }
        .header { padding: 20px 10px 10px; }
        .header h1 { font-size: 1.5rem; }
        .header p { font-size: 0.9rem; }
        .search-box { flex-direction: column; }
        .search-box input { font-size: 16px; }
        .search-box button { min-height: 44px; font-size: 16px; width: 100%; }
        .popular-grid { grid-template-columns: 1fr; }
        .fact-grid { grid-template-columns: 1fr; }
        .map-container { height: 220px; width: 100%; }
        .stats-bar { flex-direction: column; gap: 10px; }
        .stats-bar .stat .num { font-size: 1.2rem; }
        .place-card h3 { font-size: 1rem; }
        .place-card .stats { flex-wrap: wrap; gap: 10px; font-size: 0.8rem; }
        .city-header { padding: 16px; }
        .city-header h1 { font-size: 1.5rem; }
    }
    @media (max-width: 375px) {
        .header h1 { font-size: 1.3rem; }
        .popular-grid { grid-template-columns: 1fr; }
        .fact-grid { grid-template-columns: 1fr; }
        .map-container { height: 180px; }
        .city-header h1 { font-size: 1.3rem; }
        .city-header .desc { font-size: 0.9rem; }
    }
</style>
"""

NAV_HTML = """
<div style="text-align:center; padding:12px; background:#111; border-bottom:1px solid #222;">
    <a href="https://oblivionsearch.com" style="color:#888; margin:0 12px;">OBLIVION Search</a>
    <a href="/local" style="color:#8ab4f8; margin:0 12px; font-weight:600;">Local</a>
    <a href="https://oblivionsearch.com/trends" style="color:#888; margin:0 12px;">Trends</a>
    <a href="https://oblivionsearch.com/tools" style="color:#888; margin:0 12px;">Tools</a>
</div>
"""


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    try:
        async with _pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM geonames")
            cities = await conn.fetchval(
                "SELECT COUNT(*) FROM geonames WHERE feature_class='P' AND population > 15000"
            )
        return {
            "status": "ok",
            "service": "oblivion-local",
            "total_places": count,
            "major_cities": cities,
        }
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=503)


@app.get("/api/local/stats")
async def stats():
    """Database statistics."""
    async with _pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM geonames")
        countries = await conn.fetchval("SELECT COUNT(DISTINCT country_code) FROM geonames")
        cities = await conn.fetchval(
            "SELECT COUNT(*) FROM geonames WHERE feature_class='P' AND population > 0"
        )
        biggest = await conn.fetch(
            "SELECT name, country_code, population FROM geonames "
            "WHERE feature_class='P' ORDER BY population DESC LIMIT 10"
        )
    return {
        "total_places": total,
        "countries": countries,
        "populated_places": cities,
        "largest_cities": [
            {"name": r["name"], "country": country_name(r["country_code"]),
             "population": r["population"]}
            for r in biggest
        ],
    }


@app.get("/api/local/search")
async def search_places(
    q: str = Query(..., min_length=1, max_length=200),
    country: Optional[str] = Query(None, max_length=2),
    limit: int = Query(20, ge=1, le=100),
):
    """Search places by name. Optionally filter by country code."""
    query = q.strip()
    async with _pool.acquire() as conn:
        if country:
            rows = await conn.fetch("""
                SELECT geonameid, name, asciiname, latitude, longitude,
                       feature_class, feature_code, country_code, population, timezone
                FROM geonames
                WHERE (lower(name) = $1 OR lower(asciiname) = $1)
                  AND country_code = $3
                ORDER BY population DESC
                LIMIT $2
            """, query.lower(), limit, country.upper())

            if len(rows) < limit:
                extra = await conn.fetch("""
                    SELECT geonameid, name, asciiname, latitude, longitude,
                           feature_class, feature_code, country_code, population, timezone
                    FROM geonames
                    WHERE (lower(name) LIKE $1 OR lower(asciiname) LIKE $1)
                      AND country_code = $3
                      AND geonameid NOT IN (SELECT unnest($4::int[]))
                    ORDER BY population DESC
                    LIMIT $2
                """, f"%{query.lower()}%", limit - len(rows), country.upper(),
                    [r["geonameid"] for r in rows])
                rows = list(rows) + list(extra)
        else:
            # Exact match first, then prefix, then contains
            rows = await conn.fetch("""
                SELECT geonameid, name, asciiname, latitude, longitude,
                       feature_class, feature_code, country_code, population, timezone
                FROM geonames
                WHERE lower(name) = $1 OR lower(asciiname) = $1
                ORDER BY population DESC
                LIMIT $2
            """, query.lower(), limit)

            if len(rows) < limit:
                extra = await conn.fetch("""
                    SELECT geonameid, name, asciiname, latitude, longitude,
                           feature_class, feature_code, country_code, population, timezone
                    FROM geonames
                    WHERE (lower(name) LIKE $1 OR lower(asciiname) LIKE $1)
                      AND geonameid NOT IN (SELECT unnest($3::int[]))
                    ORDER BY population DESC
                    LIMIT $2
                """, f"{query.lower()}%", limit - len(rows),
                    [r["geonameid"] for r in rows])
                rows = list(rows) + list(extra)

            if len(rows) < limit:
                extra2 = await conn.fetch("""
                    SELECT geonameid, name, asciiname, latitude, longitude,
                           feature_class, feature_code, country_code, population, timezone
                    FROM geonames
                    WHERE (lower(name) LIKE $1 OR lower(asciiname) LIKE $1)
                      AND geonameid NOT IN (SELECT unnest($3::int[]))
                    ORDER BY population DESC
                    LIMIT $2
                """, f"%{query.lower()}%", limit - len(rows),
                    [r["geonameid"] for r in rows])
                rows = list(rows) + list(extra2)

    results = []
    for r in rows:
        results.append({
            "geonameid": r["geonameid"],
            "name": r["name"],
            "asciiname": r["asciiname"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "feature_class": r["feature_class"],
            "feature_code": r["feature_code"],
            "feature_description": feature_desc(r["feature_code"]),
            "country_code": r["country_code"],
            "country": country_name(r["country_code"]),
            "population": r["population"],
            "population_formatted": format_population(r["population"]),
            "timezone": r["timezone"],
        })

    return {
        "query": query,
        "count": len(results),
        "results": results,
    }


@app.get("/api/local/nearby")
async def nearby_places(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius: float = Query(50, ge=1, le=500, description="Radius in km"),
    limit: int = Query(20, ge=1, le=100),
    min_population: int = Query(0, ge=0),
):
    """Find places near a given coordinate within a radius (km)."""
    # Convert radius from km to degrees (approximate)
    # 1 degree latitude ~ 111 km
    lat_range = radius / 111.0
    lon_range = radius / (111.0 * max(math.cos(math.radians(lat)), 0.01))

    async with _pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT geonameid, name, asciiname, latitude, longitude,
                   feature_class, feature_code, country_code, population, timezone,
                   (
                       6371 * acos(
                           LEAST(1.0, GREATEST(-1.0,
                               cos(radians($1)) * cos(radians(latitude)) *
                               cos(radians(longitude) - radians($2)) +
                               sin(radians($1)) * sin(radians(latitude))
                           ))
                       )
                   ) AS distance_km
            FROM geonames
            WHERE latitude BETWEEN $1 - $3 AND $1 + $3
              AND longitude BETWEEN $2 - $4 AND $2 + $4
              AND population >= $6
            ORDER BY distance_km ASC
            LIMIT $5
        """, lat, lon, lat_range, lon_range, limit, min_population)

    results = []
    for r in rows:
        results.append({
            "geonameid": r["geonameid"],
            "name": r["name"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "feature_code": r["feature_code"],
            "feature_description": feature_desc(r["feature_code"]),
            "country_code": r["country_code"],
            "country": country_name(r["country_code"]),
            "population": r["population"],
            "population_formatted": format_population(r["population"]),
            "timezone": r["timezone"],
            "distance_km": round(r["distance_km"], 1),
        })

    return {
        "center": {"lat": lat, "lon": lon},
        "radius_km": radius,
        "count": len(results),
        "results": results,
    }


@app.get("/api/local/city/{name}/json")
async def city_json(name: str = Path(..., min_length=1, max_length=200)):
    """Get city information as JSON."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT geonameid, name, asciiname, latitude, longitude,
                   feature_class, feature_code, country_code, population,
                   elevation, timezone, admin1_code
            FROM geonames
            WHERE (lower(name) = $1 OR lower(asciiname) = $1)
              AND feature_class = 'P'
            ORDER BY population DESC
            LIMIT 1
        """, name.lower().replace("-", " "))

    if not row:
        return JSONResponse({"found": False, "name": name}, status_code=404)

    return {
        "found": True,
        "geonameid": row["geonameid"],
        "name": row["name"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "country_code": row["country_code"],
        "country": country_name(row["country_code"]),
        "population": row["population"],
        "population_formatted": format_population(row["population"]),
        "elevation": row["elevation"],
        "timezone": row["timezone"],
        "feature_code": row["feature_code"],
        "feature_description": feature_desc(row["feature_code"]),
        "admin1": row["admin1_code"],
    }


@app.get("/api/local/countries")
async def list_countries(limit: int = Query(50, ge=1, le=250)):
    """List countries with place counts."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT country_code, COUNT(*) as cnt,
                   SUM(CASE WHEN feature_class='P' AND population > 15000 THEN 1 ELSE 0 END) as cities
            FROM geonames
            WHERE country_code IS NOT NULL AND country_code != ''
            GROUP BY country_code
            ORDER BY cnt DESC
            LIMIT $1
        """, limit)

    return {
        "count": len(rows),
        "countries": [
            {
                "code": r["country_code"],
                "name": country_name(r["country_code"]),
                "total_places": r["cnt"],
                "major_cities": r["cities"],
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

@app.get("/local", response_class=HTMLResponse)
async def landing_page():
    """Landing page with search and popular cities."""
    async with _pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM geonames")
        countries = await conn.fetchval("SELECT COUNT(DISTINCT country_code) FROM geonames")
        top_cities = await conn.fetch("""
            SELECT name, country_code, population, latitude, longitude
            FROM geonames
            WHERE feature_class='P' AND population > 500000
            ORDER BY population DESC
            LIMIT 24
        """)

    cities_html = ""
    for c in top_cities:
        pop = format_population(c["population"])
        cname = country_name(c["country_code"])
        slug = c["name"].lower().replace(" ", "-")
        cities_html += f"""
        <a href="/api/local/city/{slug}" class="popular-item" style="text-decoration:none; color:#e0e0e0;">
            <div>
                <strong>{c["name"]}</strong>
                <span style="color:#888; font-size:0.85rem;"> &mdash; {cname}</span>
            </div>
            <span class="pop">{pop}</span>
        </a>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OBLIVION Local Search — Explore Places Worldwide</title>
    <meta name="description" content="Search {total:,} places across {countries} countries. Geographic search powered by OBLIVION.">
    <link rel="canonical" href="https://oblivionsearch.com/local">
    <meta property="og:title" content="OBLIVION Local Search — Explore Places Worldwide">
    <meta property="og:description" content="Search {total:,} places across {countries} countries. Geographic search powered by OBLIVION.">
    <meta property="og:url" content="https://oblivionsearch.com/local">
    <meta property="og:type" content="website">
    <meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
    <link rel="icon" href="https://oblivionsearch.com/favicon.ico">
    {DARK_CSS}
</head>
<body>
    {NAV_HTML}
    <div class="container">
        <div class="header">
            <h1>OBLIVION Local</h1>
            <p>Explore {total:,} places across {countries} countries</p>
        </div>

        <div class="search-box">
            <input type="text" id="searchInput" placeholder="Search cities, places, landmarks..."
                   autofocus onkeydown="if(event.key==='Enter')doSearch()">
            <button onclick="doSearch()">Search</button>
        </div>

        <div class="stats-bar">
            <div class="stat">
                <div class="num">{total:,}</div>
                <div class="lbl">Places</div>
            </div>
            <div class="stat">
                <div class="num">{countries}</div>
                <div class="lbl">Countries</div>
            </div>
            <div class="stat">
                <div class="num">{len(top_cities)}</div>
                <div class="lbl">Major Cities</div>
            </div>
        </div>

        <div id="results" class="results"></div>

        <div class="nearby-section">
            <h2>Largest Cities in the World</h2>
            <div class="popular-grid">
                {cities_html}
            </div>
        </div>
    </div>

    <div class="footer">
        OBLIVION Local Search &mdash; Powered by GeoNames open data<br>
        <a href="https://oblivionsearch.com">oblivionsearch.com</a>
    </div>

    <script>
    function doSearch() {{
        const q = document.getElementById('searchInput').value.trim();
        if (!q) return;
        fetch('/api/local/search?q=' + encodeURIComponent(q) + '&limit=20')
            .then(r => r.json())
            .then(data => {{
                const el = document.getElementById('results');
                if (!data.results || data.results.length === 0) {{
                    el.innerHTML = '<div class="place-card"><p>No places found for "' + q + '"</p></div>';
                    return;
                }}
                let html = '<p style="color:#888; margin-bottom:12px;">' + data.count + ' results for "' + q + '"</p>';
                data.results.forEach(p => {{
                    const slug = p.name.toLowerCase().replace(/ /g, '-');
                    html += '<div class="place-card">' +
                        '<h3><a href="/api/local/city/' + slug + '">' + p.name + '</a></h3>' +
                        '<div class="meta">' + p.country + ' &middot; ' + (p.feature_description || p.feature_code) + '</div>' +
                        '<div class="stats">' +
                        '<span>Pop: ' + p.population_formatted + '</span>' +
                        '<span>Lat: ' + (p.latitude ? p.latitude.toFixed(2) : 'N/A') + '</span>' +
                        '<span>Lon: ' + (p.longitude ? p.longitude.toFixed(2) : 'N/A') + '</span>' +
                        (p.timezone ? '<span>TZ: ' + p.timezone + '</span>' : '') +
                        '</div></div>';
                }});
                el.innerHTML = html;
            }})
            .catch(err => {{
                document.getElementById('results').innerHTML =
                    '<div class="place-card"><p style="color:#f87171;">Search error: ' + err + '</p></div>';
            }});
    }}
    </script>
</body>
</html>"""


@app.get("/api/local/city/{name}", response_class=HTMLResponse)
async def city_page(name: str = Path(..., min_length=1, max_length=200)):
    """City information page with map and nearby places."""
    clean_name = name.replace("-", " ")

    async with _pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT geonameid, name, asciiname, latitude, longitude,
                   feature_class, feature_code, country_code, population,
                   elevation, timezone, admin1_code
            FROM geonames
            WHERE (lower(name) = $1 OR lower(asciiname) = $1)
              AND feature_class = 'P'
            ORDER BY population DESC
            LIMIT 1
        """, clean_name.lower())

        if not row:
            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Not Found — OBLIVION Local</title>{DARK_CSS}</head>
<body>{NAV_HTML}<div class="container">
<div class="header"><h1>Place Not Found</h1>
<p>No city found matching "{name}". <a href="/local">Try searching</a>.</p>
</div></div></body></html>""", status_code=404)

        # Get nearby cities
        lat, lon = row["latitude"], row["longitude"]
        lat_range = 50 / 111.0
        lon_range = 50 / (111.0 * max(math.cos(math.radians(lat)), 0.01))

        nearby = await conn.fetch("""
            SELECT name, country_code, population, latitude, longitude,
                   (6371 * acos(
                       LEAST(1.0, GREATEST(-1.0,
                           cos(radians($1)) * cos(radians(latitude)) *
                           cos(radians(longitude) - radians($2)) +
                           sin(radians($1)) * sin(radians(latitude))
                       ))
                   )) AS distance_km
            FROM geonames
            WHERE latitude BETWEEN $1 - $3 AND $1 + $3
              AND longitude BETWEEN $2 - $4 AND $2 + $4
              AND feature_class = 'P'
              AND population > 5000
              AND geonameid != $5
            ORDER BY distance_km ASC
            LIMIT 12
        """, lat, lon, lat_range, lon_range, row["geonameid"])

    cname = country_name(row["country_code"])
    pop = format_population(row["population"])
    fdesc = feature_desc(row["feature_code"])

    # Nearby cities HTML
    nearby_html = ""
    for n in nearby:
        slug = n["name"].lower().replace(" ", "-")
        dist = round(n["distance_km"], 1)
        npop = format_population(n["population"])
        nearby_html += f"""
        <a href="/api/local/city/{slug}" class="popular-item" style="text-decoration:none; color:#e0e0e0;">
            <div><strong>{n["name"]}</strong> <span style="color:#888;">({npop})</span></div>
            <span class="pop">{dist} km</span>
        </a>
        """

    # Facts grid
    facts = []
    facts.append(("Country", cname))
    facts.append(("Population", f"{row['population']:,}" if row['population'] else "N/A"))
    facts.append(("Coordinates", f"{row['latitude']:.4f}, {row['longitude']:.4f}"))
    if row["elevation"]:
        facts.append(("Elevation", f"{row['elevation']:,} m"))
    facts.append(("Timezone", row["timezone"] or "N/A"))
    facts.append(("Type", fdesc))
    if row["admin1_code"]:
        facts.append(("Admin Region", row["admin1_code"]))

    facts_html = ""
    for label, value in facts:
        facts_html += f"""
        <div class="fact-item">
            <div class="label">{label}</div>
            <div class="value">{value}</div>
        </div>
        """

    osm_url = f"https://www.openstreetmap.org/export/embed.html?bbox={lon-0.05},{lat-0.03},{lon+0.05},{lat+0.03}&layer=mapnik&marker={lat},{lon}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{row["name"]}, {cname} — OBLIVION Local</title>
    <meta name="description" content="{row["name"]} is a {fdesc} in {cname} with a population of {pop}. Explore on OBLIVION Local Search.">
    {DARK_CSS}
</head>
<body>
    {NAV_HTML}
    <div class="container">
        <div class="city-header">
            <h1>{row["name"]}</h1>
            <div class="desc">{fdesc.title()} in {cname}</div>
            <div class="fact-grid">
                {facts_html}
            </div>
        </div>

        <div class="map-container">
            <iframe src="{osm_url}" loading="lazy"></iframe>
        </div>

        <div class="search-box">
            <input type="text" id="searchInput" placeholder="Search another place..."
                   onkeydown="if(event.key==='Enter')location.href='/api/local/city/'+this.value.toLowerCase().replace(/ /g,'-')">
            <button onclick="location.href='/api/local/city/'+document.getElementById('searchInput').value.toLowerCase().replace(/ /g,'-')">Go</button>
        </div>

        <div class="nearby-section">
            <h2>Nearby Cities (within 50 km)</h2>
            <div class="popular-grid">
                {nearby_html if nearby_html else '<p style="color:#888;">No nearby cities with population > 5,000 found.</p>'}
            </div>
        </div>

        <div style="margin-top:20px; text-align:center;">
            <a href="/api/local/nearby?lat={lat}&lon={lon}&radius=100&min_population=10000&limit=50"
               style="color:#8ab4f8;">View all nearby places (JSON API)</a>
            &nbsp;&middot;&nbsp;
            <a href="/local" style="color:#888;">Back to Local Search</a>
        </div>
    </div>

    <div class="footer">
        OBLIVION Local Search &mdash; Powered by GeoNames open data<br>
        <a href="https://oblivionsearch.com">oblivionsearch.com</a>
    </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# SaaS Helpers
# ---------------------------------------------------------------------------

def _generate_api_key():
    return "obloc_" + secrets.token_hex(24)

def _send_welcome_email(email: str, api_key: str, plan: str):
    try:
        body = f"""Welcome to OBLIVION Local Search API ({plan} plan)!

Your API key: {api_key}

Usage examples:
  curl -H "X-API-Key: {api_key}" "{DOMAIN}/api/local/search?q=london"
  curl -H "X-API-Key: {api_key}" "{DOMAIN}/api/local/nearby?lat=51.5&lon=-0.1&radius=50"
  curl -H "X-API-Key: {api_key}" "{DOMAIN}/api/local/city/london/json"

Dashboard: {DOMAIN}/local/dashboard?key={api_key}

Thank you for choosing OBLIVION Local Search.
"""
        msg = MIMEText(body)
        msg["Subject"] = f"OBLIVION Local Search — Your API Key ({plan})"
        msg["From"] = SMTP_USER
        msg["To"] = email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        log.error("Failed to send welcome email to %s: %s", email, e)


# ---------------------------------------------------------------------------
# SaaS Routes — Pricing / Checkout / Success / Dashboard / Webhook
# ---------------------------------------------------------------------------

LOCAL_PRICING_CSS = """
.pricing-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:1.5rem; margin:2rem 0; }
.plan-card { background:#1a1a1a; border:1px solid #222; border-radius:16px; padding:2rem; text-align:center; transition:border-color 0.3s; }
.plan-card:hover { border-color:#8ab4f8; }
.plan-card.featured { border-color:#c084fc; box-shadow:0 0 30px rgba(192,132,252,0.15); }
.plan-card h3 { font-size:1.3rem; margin-bottom:0.5rem; }
.plan-price { font-size:2.5rem; font-weight:700; background:linear-gradient(135deg,#8ab4f8,#c084fc); -webkit-background-clip:text; -webkit-text-fill-color:transparent; margin:1rem 0; }
.plan-price span { font-size:1rem; -webkit-text-fill-color:#888; }
.plan-features { list-style:none; text-align:left; margin:1.5rem 0; }
.plan-features li { padding:0.4rem 0; color:#aaa; font-size:0.95rem; }
.plan-features li::before { content:"\\2713 "; color:#8ab4f8; font-weight:bold; margin-right:0.5rem; }
.plan-btn { display:inline-block; padding:12px 32px; border-radius:10px; font-weight:600; font-size:1rem; text-decoration:none; transition:opacity 0.2s; border:none; cursor:pointer; }
.plan-btn-primary { background:linear-gradient(135deg,#8ab4f8,#c084fc); color:#000; }
.plan-btn-outline { background:transparent; border:1px solid #444; color:#e0e0e0; }
.plan-btn:hover { opacity:0.85; }
"""

@app.get("/local/pricing", response_class=HTMLResponse)
async def local_pricing():
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Pricing — OBLIVION Local Search</title>
<link rel="icon" href="https://oblivionsearch.com/favicon.ico">
{DARK_CSS}
<style>{LOCAL_PRICING_CSS}</style>
</head><body>
{NAV_HTML}
<div class="container">
<div class="header"><h1>OBLIVION Local API Pricing</h1><p>Geographic search API for your applications</p></div>
<div class="pricing-grid">
  <div class="plan-card">
    <h3>Free</h3>
    <div class="plan-price">£0<span>/mo</span></div>
    <ul class="plan-features">
      <li>Search on website</li>
      <li>City pages</li>
      <li>No API access</li>
    </ul>
    <a href="/local" class="plan-btn plan-btn-outline">Use Free</a>
  </div>
  <div class="plan-card featured">
    <h3>API</h3>
    <div class="plan-price">£14<span>/mo</span></div>
    <ul class="plan-features">
      <li>Geocoding API access</li>
      <li>10,000 requests/day</li>
      <li>Nearby search</li>
      <li>City data JSON</li>
      <li>Country listings</li>
    </ul>
    <a href="/local/checkout/api" class="plan-btn plan-btn-primary">Subscribe</a>
  </div>
  <div class="plan-card">
    <h3>API Pro</h3>
    <div class="plan-price">£39<span>/mo</span></div>
    <ul class="plan-features">
      <li>Everything in API</li>
      <li>Unlimited requests</li>
      <li>Bulk geocoding</li>
      <li>Data export</li>
      <li>Priority support</li>
    </ul>
    <a href="/local/checkout/api_pro" class="plan-btn plan-btn-primary">Subscribe</a>
  </div>
</div>
</div>
<div class="footer">OBLIVION Local Search &mdash; <a href="https://oblivionsearch.com">oblivionsearch.com</a></div>
</body></html>"""


@app.get("/local/checkout/{plan}")
async def local_checkout(plan: str):
    if plan not in LOCAL_PLANS:
        return HTMLResponse("<h1>Invalid plan</h1>", status_code=400)
    p = LOCAL_PLANS[plan]
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": p["currency"],
                    "product_data": {"name": f"OBLIVION Local Search — {p['name']}"},
                    "unit_amount": p["price_amount"],
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=DOMAIN + "/local/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=DOMAIN + "/local/pricing",
            metadata={"product": PRODUCT_NAME, "plan": plan},
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        log.error("Stripe checkout error: %s", e)
        return HTMLResponse(f"<h1>Checkout error</h1><p>{e}</p>", status_code=500)


@app.get("/local/success", response_class=HTMLResponse)
async def local_success(session_id: str = Query(...)):
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        email = session.customer_details.email or session.customer_email or "unknown"
        plan = session.metadata.get("plan", "api")
        p = LOCAL_PLANS.get(plan, LOCAL_PLANS["api"])
        api_key = _generate_api_key()

        async with _saas_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO saas_customers (email, stripe_customer_id, stripe_subscription_id, product, plan, api_key)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, email, session.customer, session.subscription, PRODUCT_NAME, plan, api_key)

        _send_welcome_email(email, api_key, p["name"])

        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Welcome — OBLIVION Local Search</title><link rel="icon" href="https://oblivionsearch.com/favicon.ico">
{DARK_CSS}</head><body>{NAV_HTML}
<div class="container">
<div class="header"><h1>Welcome to OBLIVION Local API!</h1><p>Your subscription is active</p></div>
<div style="background:#1a1a1a;border:1px solid #222;border-radius:16px;padding:2rem;max-width:600px;margin:2rem auto;">
  <p style="color:#888;margin-bottom:0.5rem;">Your API Key:</p>
  <div style="background:#111;border:1px solid #333;border-radius:8px;padding:1rem;font-family:monospace;font-size:1.1rem;word-break:break-all;color:#8ab4f8;">{api_key}</div>
  <p style="color:#888;margin-top:1rem;font-size:0.9rem;">Plan: <strong style="color:#c084fc;">{p['name']}</strong> &mdash; {p['label']}</p>
  <p style="color:#888;font-size:0.9rem;">Email: {email}</p>
  <p style="color:#f87171;margin-top:1rem;font-size:0.85rem;">Save this key! It has also been sent to your email.</p>
  <a href="/local/dashboard?key={api_key}" class="plan-btn plan-btn-primary" style="display:inline-block;margin-top:1.5rem;text-decoration:none;">Open Dashboard</a>
</div></div>
<div class="footer">OBLIVION Local Search &mdash; <a href="https://oblivionsearch.com">oblivionsearch.com</a></div>
</body></html>"""
    except Exception as e:
        log.error("Success page error: %s", e)
        return HTMLResponse(f"<h1>Error</h1><p>{e}</p>", status_code=500)


@app.get("/local/dashboard", response_class=HTMLResponse)
async def local_dashboard(key: str = Query(...)):
    async with _saas_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM saas_customers WHERE api_key=$1 AND product=$2", key, PRODUCT_NAME
        )
    if not row:
        return HTMLResponse("<h1>Invalid API key</h1>", status_code=404)

    p = LOCAL_PLANS.get(row["plan"], LOCAL_PLANS["api"])
    status_tag = '<span style="color:#0f5;font-weight:600;">Active</span>' if row["active"] else '<span style="color:#f44;font-weight:600;">Inactive</span>'
    req_limit_str = str(p["req_limit"]) if p["req_limit"] else "Unlimited"
    reqs_today = row["requests_today"] if row["requests_reset_date"] == date.today() else 0

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Dashboard — OBLIVION Local Search</title><link rel="icon" href="https://oblivionsearch.com/favicon.ico">
{DARK_CSS}</head><body>{NAV_HTML}
<div class="container">
<div class="header"><h1>API Dashboard</h1><p>OBLIVION Local Search</p></div>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-bottom:2rem;">
  <div style="background:#1a1a1a;border:1px solid #222;border-radius:12px;padding:1.5rem;">
    <div style="color:#888;font-size:0.8rem;text-transform:uppercase;">Status</div>
    <div style="font-size:1.3rem;margin-top:0.5rem;">{status_tag}</div>
  </div>
  <div style="background:#1a1a1a;border:1px solid #222;border-radius:12px;padding:1.5rem;">
    <div style="color:#888;font-size:0.8rem;text-transform:uppercase;">Plan</div>
    <div style="font-size:1.3rem;margin-top:0.5rem;color:#c084fc;">{p['name']}</div>
  </div>
  <div style="background:#1a1a1a;border:1px solid #222;border-radius:12px;padding:1.5rem;">
    <div style="color:#888;font-size:0.8rem;text-transform:uppercase;">Requests Today</div>
    <div style="font-size:1.3rem;margin-top:0.5rem;">{reqs_today} / {req_limit_str}</div>
  </div>
</div>
<div style="background:#1a1a1a;border:1px solid #222;border-radius:16px;padding:2rem;">
  <h3 style="color:#8ab4f8;margin-bottom:1rem;">API Key</h3>
  <div style="background:#111;border:1px solid #333;border-radius:8px;padding:1rem;font-family:monospace;font-size:0.95rem;word-break:break-all;color:#8ab4f8;">{row['api_key']}</div>
  <p style="color:#888;margin-top:1rem;font-size:0.9rem;">Email: {row['email']}</p>
  <p style="color:#888;font-size:0.9rem;">Subscribed: {row['created_at'].strftime('%Y-%m-%d')}</p>
  <h3 style="color:#8ab4f8;margin-top:2rem;margin-bottom:0.5rem;">Quick Start</h3>
  <pre style="background:#111;border:1px solid #333;border-radius:8px;padding:1rem;overflow-x:auto;font-size:0.85rem;color:#aaa;">curl -H "X-API-Key: {row['api_key']}" \\
  "{DOMAIN}/api/local/search?q=london"

curl -H "X-API-Key: {row['api_key']}" \\
  "{DOMAIN}/api/local/nearby?lat=51.5&lon=-0.1&radius=50"

curl -H "X-API-Key: {row['api_key']}" \\
  "{DOMAIN}/api/local/city/london/json"</pre>
</div></div>
<div class="footer">OBLIVION Local Search &mdash; <a href="https://oblivionsearch.com">oblivionsearch.com</a></div>
</body></html>"""


@app.post("/local/webhook")
async def local_webhook(request: Request):
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
