#!/usr/bin/env python3
"""
OBLIVION Finance — SEC EDGAR Financial Search
Port 3064 — Part of OBLIVION Search (oblivionsearch.com)
"""

import asyncio
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx
import asyncpg
from fastapi import FastAPI, Request, Query, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Config ──────────────────────────────────────────────────────────────────
DB_DSN = "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/oblivion_finance"
SEC_USER_AGENT = "OBLIVION Search admin@oblivionsearch.com"
SEC_HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
SEC_RATE_LIMIT = 10  # requests per second max
CACHE_TTL_COMPANY = 86400  # 24 hours
CACHE_TTL_FILINGS = 3600   # 1 hour

TRENDING_COMPANIES = [
    ("AAPL", "Apple Inc."), ("MSFT", "Microsoft Corporation"), ("GOOGL", "Alphabet Inc."),
    ("AMZN", "Amazon.com Inc."), ("TSLA", "Tesla Inc."), ("META", "Meta Platforms Inc."),
    ("NVDA", "NVIDIA Corporation"), ("BRK-B", "Berkshire Hathaway"), ("JPM", "JPMorgan Chase"),
    ("V", "Visa Inc."), ("JNJ", "Johnson & Johnson"), ("WMT", "Walmart Inc."),
    ("PG", "Procter & Gamble"), ("MA", "Mastercard Inc."), ("UNH", "UnitedHealth Group"),
    ("HD", "The Home Depot"), ("DIS", "Walt Disney Co."), ("NFLX", "Netflix Inc."),
    ("PYPL", "PayPal Holdings"), ("INTC", "Intel Corporation"),
]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("oblivion_finance")

# ── FastAPI App ─────────────────────────────────────────────────────────────
app = FastAPI(title="OBLIVION Finance", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://oblivionsearch.com", "https://www.oblivionsearch.com"],
    allow_methods=["GET"], allow_headers=["*"],
)

# ── Rate limiter for SEC EDGAR ──────────────────────────────────────────────
class SECRateLimiter:
    def __init__(self, max_per_sec: int = 10):
        self.max_per_sec = max_per_sec
        self.timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            self.timestamps = [t for t in self.timestamps if now - t < 1.0]
            if len(self.timestamps) >= self.max_per_sec:
                wait = 1.0 - (now - self.timestamps[0])
                if wait > 0:
                    await asyncio.sleep(wait)
            self.timestamps.append(time.monotonic())

rate_limiter = SECRateLimiter(SEC_RATE_LIMIT)

# ── Global state ────────────────────────────────────────────────────────────
db_pool: Optional[asyncpg.Pool] = None
http_client: Optional[httpx.AsyncClient] = None
company_tickers: dict = {}  # ticker -> {cik, name, ticker}

# ── Startup / Shutdown ──────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global db_pool, http_client, company_tickers
    # Database
    db_pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    await _init_db()
    # HTTP client
    http_client = httpx.AsyncClient(headers=SEC_HEADERS, timeout=30.0, follow_redirects=True)
    # Load company tickers
    company_tickers = await _load_company_tickers()
    log.info(f"Loaded {len(company_tickers)} company tickers")

@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()
    if http_client:
        await http_client.aclose()

async def _init_db():
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS company_cache (
                cik TEXT PRIMARY KEY,
                ticker TEXT,
                name TEXT,
                sic TEXT,
                sic_description TEXT,
                state TEXT,
                address TEXT,
                data JSONB,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_company_ticker ON company_cache(ticker);
            CREATE INDEX IF NOT EXISTS idx_company_name ON company_cache USING gin(to_tsvector('english', name));

            CREATE TABLE IF NOT EXISTS filings_cache (
                cik TEXT,
                filing_type TEXT,
                data JSONB,
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (cik, filing_type)
            );

            CREATE TABLE IF NOT EXISTS search_log (
                id SERIAL PRIMARY KEY,
                query TEXT,
                results_count INT,
                searched_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

async def _load_company_tickers() -> dict:
    """Load SEC company tickers JSON into memory."""
    tickers = {}
    try:
        await rate_limiter.acquire()
        resp = await http_client.get("https://www.sec.gov/files/company_tickers.json")
        if resp.status_code == 200:
            data = resp.json()
            for entry in data.values():
                ticker = entry.get("ticker", "").upper()
                if ticker:
                    tickers[ticker] = {
                        "cik": str(entry.get("cik_str", "")),
                        "name": entry.get("title", ""),
                        "ticker": ticker,
                    }
    except Exception as e:
        log.error(f"Failed to load company tickers: {e}")
    return tickers

# ── SEC EDGAR API helpers ───────────────────────────────────────────────────
async def sec_get(url: str) -> Optional[dict]:
    """Rate-limited GET to SEC EDGAR, returns JSON or None."""
    await rate_limiter.acquire()
    try:
        resp = await http_client.get(url)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        log.error(f"SEC GET {url}: {e}")
    return None

def pad_cik(cik: str) -> str:
    return cik.zfill(10)

async def get_company_submissions(cik: str) -> Optional[dict]:
    """Fetch company submissions from SEC EDGAR."""
    padded = pad_cik(cik)
    url = f"https://data.sec.gov/submissions/CIK{padded}.json"
    return await sec_get(url)

async def search_edgar_fulltext(query: str, start: int = 0) -> Optional[dict]:
    """Full-text search of EDGAR filings."""
    await rate_limiter.acquire()
    try:
        url = "https://efts.sec.gov/LATEST/search-index"
        params = {"q": query, "dateRange": "custom", "startdt": "2020-01-01",
                  "enddt": datetime.now().strftime("%Y-%m-%d"), "forms": "10-K,10-Q,8-K",
                  "from": start}
        resp = await http_client.get(url, params=params)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        log.error(f"EDGAR search error: {e}")
    return None

# ── Database cache helpers ──────────────────────────────────────────────────
async def cache_get_company(cik: str) -> Optional[dict]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data, updated_at FROM company_cache WHERE cik=$1", cik)
        if row and (datetime.now(row['updated_at'].tzinfo) - row['updated_at']).total_seconds() < CACHE_TTL_COMPANY:
            return json.loads(row['data'])
    return None

async def cache_set_company(cik: str, ticker: str, name: str, data: dict):
    sic = data.get("sic", "")
    sic_desc = data.get("sicDescription", "")
    state = ""
    address_parts = []
    if "addresses" in data and "business" in data["addresses"]:
        addr = data["addresses"]["business"]
        state = addr.get("stateOrCountry", "")
        for f in ["street1", "street2", "city", "stateOrCountry", "zipCode"]:
            if addr.get(f):
                address_parts.append(addr[f])
    address = ", ".join(address_parts)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO company_cache (cik, ticker, name, sic, sic_description, state, address, data, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())
            ON CONFLICT (cik) DO UPDATE SET ticker=$2, name=$3, sic=$4, sic_description=$5,
                state=$6, address=$7, data=$8, updated_at=NOW()
        """, cik, ticker, name, sic, sic_desc, state, address, json.dumps(data))

async def cache_get_filings(cik: str, filing_type: str = "all") -> Optional[dict]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data, updated_at FROM filings_cache WHERE cik=$1 AND filing_type=$2", cik, filing_type)
        if row and (datetime.now(row['updated_at'].tzinfo) - row['updated_at']).total_seconds() < CACHE_TTL_FILINGS:
            return json.loads(row['data'])
    return None

async def cache_set_filings(cik: str, filing_type: str, data: dict):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO filings_cache (cik, filing_type, data, updated_at) VALUES ($1,$2,$3,NOW())
            ON CONFLICT (cik, filing_type) DO UPDATE SET data=$3, updated_at=NOW()
        """, cik, filing_type, json.dumps(data))

# ── Business logic ──────────────────────────────────────────────────────────
def search_tickers(query: str, limit: int = 20) -> list[dict]:
    """Search in-memory tickers by name or ticker symbol."""
    q = query.upper().strip()
    results = []
    # Exact ticker match first
    if q in company_tickers:
        results.append(company_tickers[q])
    # Partial matches
    ql = query.lower()
    for ticker, info in company_tickers.items():
        if info in results:
            continue
        if q in ticker or ql in info["name"].lower():
            results.append(info)
        if len(results) >= limit:
            break
    return results

async def get_company_data(ticker_or_cik: str) -> Optional[dict]:
    """Get full company data by ticker or CIK."""
    ticker_or_cik = ticker_or_cik.strip().upper()
    # Resolve to CIK
    cik = None
    ticker = ticker_or_cik
    if ticker_or_cik in company_tickers:
        cik = company_tickers[ticker_or_cik]["cik"]
        ticker = ticker_or_cik
    elif ticker_or_cik.isdigit():
        cik = ticker_or_cik
    else:
        # Search for partial match
        matches = search_tickers(ticker_or_cik, limit=1)
        if matches:
            cik = matches[0]["cik"]
            ticker = matches[0]["ticker"]

    if not cik:
        return None

    # Check cache
    cached = await cache_get_company(cik)
    if cached:
        return cached

    # Fetch from SEC
    data = await get_company_submissions(cik)
    if data:
        await cache_set_company(cik, ticker, data.get("name", ""), data)
        return data
    return None

def extract_filings(data: dict, filing_types: list = None, limit: int = 50) -> list[dict]:
    """Extract filings from submissions data."""
    filings = []
    recent = data.get("filings", {}).get("recent", {})
    if not recent:
        return filings
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])
    cik = str(data.get("cik", ""))

    for i in range(min(len(forms), len(dates), len(accessions))):
        form = forms[i]
        if filing_types and form not in filing_types:
            continue
        acc = accessions[i].replace("-", "")
        acc_display = accessions[i]
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""
        desc = descriptions[i] if i < len(descriptions) else ""
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{primary_doc}" if primary_doc else ""
        index_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

        filings.append({
            "form": form,
            "filingDate": dates[i],
            "accessionNumber": acc_display,
            "primaryDocument": primary_doc,
            "description": desc,
            "filingUrl": filing_url,
            "indexUrl": index_url,
        })
        if len(filings) >= limit:
            break
    return filings

# ── API Endpoints ───────────────────────────────────────────────────────────
def _check_finance_key(key):
    """Validate API key against shared SaaS module."""
    if not key:
        return None
    try:
        import sys
        sys.path.insert(0, "/opt/oblivionzone")
        from oblivion_stripe_saas import check_api_key
        return check_api_key(key, "oblivion_finance")
    except Exception:
        return None

@app.get("/api/finance/search")
async def api_search(q: str = Query(..., min_length=1, max_length=100), x_api_key: Optional[str] = Header(None)):
    """Search companies by name or ticker."""
    if x_api_key:
        plan = _check_finance_key(x_api_key)
        if not plan:
            return JSONResponse({"error": "Invalid API key"}, status_code=401)
    results = search_tickers(q, limit=20)
    # Log search
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO search_log(query, results_count) VALUES ($1,$2)", q, len(results))
    except Exception:
        pass
    return {"query": q, "results": results, "count": len(results)}

@app.get("/api/finance/company/{ticker}")
async def api_company(ticker: str, x_api_key: Optional[str] = Header(None)):
    """Get company profile and recent filings."""
    if x_api_key:
        plan = _check_finance_key(x_api_key)
        if not plan:
            return JSONResponse({"error": "Invalid API key"}, status_code=401)
    data = await get_company_data(ticker)
    if not data:
        return JSONResponse({"error": "Company not found"}, status_code=404)
    filings = extract_filings(data, limit=20)
    cik = str(data.get("cik", ""))
    return {
        "cik": cik,
        "name": data.get("name", ""),
        "ticker": data.get("tickers", [ticker.upper()])[0] if data.get("tickers") else ticker.upper(),
        "sic": data.get("sic", ""),
        "sicDescription": data.get("sicDescription", ""),
        "stateOfIncorporation": data.get("stateOfIncorporation", ""),
        "fiscalYearEnd": data.get("fiscalYearEnd", ""),
        "addresses": data.get("addresses", {}),
        "website": data.get("website", ""),
        "phone": data.get("phone", ""),
        "filings": filings,
        "totalFilings": data.get("filings", {}).get("recent", {}).get("form", []).__len__() if data.get("filings") else 0,
    }

@app.get("/api/finance/filings/{cik}")
async def api_filings(cik: str, form: Optional[str] = None, limit: int = 50, x_api_key: Optional[str] = Header(None)):
    """Get filings for a company by CIK."""
    if x_api_key:
        plan = _check_finance_key(x_api_key)
        if not plan:
            return JSONResponse({"error": "Invalid API key"}, status_code=401)
    # Check cache
    cache_key = form or "all"
    cached = await cache_get_filings(cik, cache_key)
    if cached:
        return cached

    data = await get_company_submissions(cik)
    if not data:
        return JSONResponse({"error": "CIK not found"}, status_code=404)

    filing_types = [f.strip() for f in form.split(",")] if form else None
    filings = extract_filings(data, filing_types=filing_types, limit=limit)
    result = {
        "cik": cik,
        "name": data.get("name", ""),
        "filings": filings,
        "count": len(filings),
    }
    await cache_set_filings(cik, cache_key, result)
    return result

# ── HTML Templates ──────────────────────────────────────────────────────────
def _base_head(title: str = "OBLIVION Finance") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — OBLIVION Search</title>
<meta name="description" content="Search SEC EDGAR filings, company profiles, 10-K, 10-Q, 8-K reports. Free financial data powered by OBLIVION Search.">
<link rel="canonical" href="https://oblivionsearch.com/finance">
<meta property="og:title" content="OBLIVION Finance — SEC EDGAR Financial Search">
<meta property="og:description" content="Search SEC EDGAR filings, company profiles, 10-K, 10-Q, 8-K reports. Free financial data powered by OBLIVION Search.">
<meta property="og:url" content="https://oblivionsearch.com/finance">
<meta property="og:type" content="website">
<meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: #0a0a0f; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; min-height: 100vh; }}
a {{ color: #00d4ff; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.container {{ max-width: 1100px; margin: 0 auto; padding: 0 20px; }}
header {{ padding: 24px 0; border-bottom: 1px solid #1a1a2e; }}
header .logo {{ display: flex; align-items: center; gap: 12px; }}
header .logo svg {{ width: 36px; height: 36px; }}
header .logo span {{ font-size: 1.4rem; font-weight: 700; color: #fff; }}
header .logo .sub {{ color: #00d4ff; font-weight: 400; font-size: 1rem; margin-left: 4px; }}
header nav {{ display: flex; gap: 20px; margin-top: 10px; }}
header nav a {{ color: #888; font-size: 0.9rem; }}
header nav a:hover, header nav a.active {{ color: #00d4ff; }}
.hero {{ text-align: center; padding: 60px 0 40px; }}
.hero h1 {{ font-size: 2.4rem; font-weight: 700; color: #fff; margin-bottom: 8px; }}
.hero h1 .accent {{ color: #00d4ff; }}
.hero p {{ color: #888; font-size: 1.1rem; max-width: 600px; margin: 0 auto 30px; }}
.search-box {{ position: relative; max-width: 640px; margin: 0 auto 20px; }}
.search-box input {{ width: 100%; padding: 16px 20px 16px 48px; background: #12121a; border: 1px solid #2a2a3e; border-radius: 12px; color: #fff; font-size: 1.05rem; outline: none; transition: border-color 0.2s; }}
.search-box input:focus {{ border-color: #00d4ff; }}
.search-box input::placeholder {{ color: #555; }}
.search-box .icon {{ position: absolute; left: 16px; top: 50%; transform: translateY(-50%); color: #555; }}
.results {{ max-width: 640px; margin: 0 auto; background: #12121a; border: 1px solid #2a2a3e; border-radius: 12px; display: none; max-height: 400px; overflow-y: auto; }}
.results.show {{ display: block; }}
.result-item {{ padding: 12px 20px; border-bottom: 1px solid #1a1a2e; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }}
.result-item:hover {{ background: #1a1a2e; }}
.result-item:last-child {{ border-bottom: none; }}
.result-item .ticker {{ color: #00d4ff; font-weight: 700; font-size: 1rem; min-width: 80px; }}
.result-item .name {{ color: #ccc; font-size: 0.95rem; flex: 1; margin-left: 12px; }}
.section {{ margin: 40px 0; }}
.section h2 {{ font-size: 1.3rem; font-weight: 600; color: #fff; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 1px solid #1a1a2e; }}
.trending-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }}
.trending-card {{ background: #12121a; border: 1px solid #1a1a2e; border-radius: 10px; padding: 14px 16px; transition: border-color 0.2s, transform 0.15s; }}
.trending-card:hover {{ border-color: #00d4ff; transform: translateY(-2px); }}
.trending-card .t {{ color: #00d4ff; font-weight: 700; font-size: 1.05rem; }}
.trending-card .n {{ color: #888; font-size: 0.82rem; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.company-header {{ padding: 40px 0 30px; }}
.company-header h1 {{ font-size: 2rem; color: #fff; }}
.company-header .meta {{ display: flex; gap: 20px; flex-wrap: wrap; margin-top: 10px; color: #888; font-size: 0.9rem; }}
.company-header .meta span {{ display: flex; align-items: center; gap: 6px; }}
.company-header .badge {{ background: #00d4ff20; color: #00d4ff; padding: 2px 10px; border-radius: 6px; font-weight: 600; font-size: 0.85rem; }}
.filings-table {{ width: 100%; border-collapse: collapse; }}
.filings-table th {{ text-align: left; padding: 10px 12px; color: #888; font-weight: 500; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #2a2a3e; }}
.filings-table td {{ padding: 10px 12px; border-bottom: 1px solid #1a1a2e; font-size: 0.95rem; }}
.filings-table tr:hover td {{ background: #12121a; }}
.form-badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }}
.form-10k {{ background: #00d4ff20; color: #00d4ff; }}
.form-10q {{ background: #8b5cf620; color: #8b5cf6; }}
.form-8k {{ background: #f59e0b20; color: #f59e0b; }}
.form-other {{ background: #33333a; color: #aaa; }}
.info-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; margin-bottom: 30px; }}
.info-card {{ background: #12121a; border: 1px solid #1a1a2e; border-radius: 10px; padding: 18px; }}
.info-card .label {{ color: #666; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }}
.info-card .value {{ color: #fff; font-size: 1rem; }}
footer {{ padding: 30px 0; margin-top: 60px; border-top: 1px solid #1a1a2e; text-align: center; color: #555; font-size: 0.85rem; }}
.filter-pills {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; }}
.filter-pill {{ padding: 6px 14px; background: #12121a; border: 1px solid #2a2a3e; border-radius: 20px; color: #888; font-size: 0.85rem; cursor: pointer; transition: all 0.2s; }}
.filter-pill:hover, .filter-pill.active {{ background: #00d4ff20; border-color: #00d4ff; color: #00d4ff; }}
.back-link {{ display: inline-flex; align-items: center; gap: 6px; color: #888; font-size: 0.9rem; margin-bottom: 10px; }}
.back-link:hover {{ color: #00d4ff; }}
.empty {{ text-align: center; padding: 40px; color: #555; }}
.spinner {{ display: inline-block; width: 20px; height: 20px; border: 2px solid #333; border-top-color: #00d4ff; border-radius: 50%; animation: spin 0.6s linear infinite; }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
@media (max-width: 640px) {{
    .hero h1 {{ font-size: 1.6rem; }}
    .trending-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .info-grid {{ grid-template-columns: 1fr; }}
}}
@media(max-width:480px){{
  .hero h1{{font-size:1.8rem}}
  .hero p{{font-size:0.9rem}}
  .trending-grid{{grid-template-columns:repeat(2,1fr)}}
  .search-box{{padding:12px}}
  .search-box input{{font-size:16px}}
  input,select{{font-size:16px}}
  body{{font-size:14px}}
}}
@media(max-width:375px){{
  .hero h1{{font-size:1.5rem}}
  .trending-grid{{grid-template-columns:1fr 1fr}}
  .container{{padding:10px}}
}}
</style>
</head>
<body>"""

FOOTER_HTML = """
<footer class="container">
    <p>Data provided by <a href="https://www.sec.gov/edgar" target="_blank">SEC EDGAR</a>.
    OBLIVION Finance is not affiliated with the SEC. Not financial advice.</p>
    <p style="margin-top:6px;">&copy; 2026 <a href="https://oblivionsearch.com">OBLIVION Search</a></p>
</footer>
"""

HEADER_HTML = """
<header class="container">
    <div class="logo">
        <svg viewBox="0 0 36 36" fill="none"><circle cx="18" cy="18" r="17" stroke="#00d4ff" stroke-width="2"/><circle cx="18" cy="18" r="8" fill="#00d4ff" opacity="0.3"/><circle cx="18" cy="18" r="3" fill="#00d4ff"/></svg>
        <span>OBLIVION<span class="sub">Finance</span></span>
    </div>
    <nav>
        <a href="/finance" class="active">Search</a>
        <a href="/">Web Search</a>
        <a href="/trends">Trends</a>
    </nav>
</header>
"""

# ── Landing page ────────────────────────────────────────────────────────────
@app.get("/finance", response_class=HTMLResponse)
async def finance_landing():
    trending_html = ""
    for ticker, name in TRENDING_COMPANIES:
        trending_html += f'<a href="/finance/{ticker}" class="trending-card"><div class="t">{ticker}</div><div class="n">{name}</div></a>\n'

    return f"""{_base_head("OBLIVION Finance — SEC EDGAR Search")}
{HEADER_HTML}
<main class="container">
    <div class="hero">
        <h1>Search <span class="accent">SEC EDGAR</span></h1>
        <p>Explore public company filings, financials, and regulatory documents from the SEC EDGAR database.</p>
        <div class="search-box">
            <span class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg></span>
            <input type="text" id="searchInput" placeholder="Search by company name or ticker (e.g. Apple, TSLA)..." autocomplete="off" autofocus>
        </div>
        <div class="results" id="searchResults"></div>
    </div>

    <div class="section">
        <h2>Trending Companies</h2>
        <div class="trending-grid">
            {trending_html}
        </div>
    </div>
</main>
{FOOTER_HTML}
<script>
const input = document.getElementById('searchInput');
const results = document.getElementById('searchResults');
let debounce = null;

input.addEventListener('input', () => {{
    clearTimeout(debounce);
    const q = input.value.trim();
    if (q.length < 1) {{ results.classList.remove('show'); return; }}
    debounce = setTimeout(async () => {{
        try {{
            const resp = await fetch('/api/finance/search?q=' + encodeURIComponent(q));
            const data = await resp.json();
            if (data.results && data.results.length > 0) {{
                results.innerHTML = data.results.map(r =>
                    `<a href="/finance/${{r.ticker}}" class="result-item">
                        <span class="ticker">${{r.ticker}}</span>
                        <span class="name">${{r.name}}</span>
                    </a>`
                ).join('');
                results.classList.add('show');
            }} else {{
                results.innerHTML = '<div class="empty">No companies found</div>';
                results.classList.add('show');
            }}
        }} catch(e) {{ results.classList.remove('show'); }}
    }}, 200);
}});

input.addEventListener('keydown', (e) => {{
    if (e.key === 'Enter') {{
        const first = results.querySelector('a');
        if (first) window.location.href = first.href;
    }}
}});

document.addEventListener('click', (e) => {{
    if (!results.contains(e.target) && e.target !== input) results.classList.remove('show');
}});
</script>
</body></html>"""

# ── Health check ────────────────────────────────────────────────────────────
@app.get("/api/finance/health")
async def health():
    db_ok = False
    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        pass
    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "companies_loaded": len(company_tickers),
        "timestamp": datetime.utcnow().isoformat(),
    }

# ── Run ─────────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════
# STRIPE SAAS ROUTES (auto-generated by add_stripe_to_all.py)
# ═══════════════════════════════════════════════════════════════════
import sys
sys.path.insert(0, "/opt/oblivionzone")
from oblivion_stripe_saas import (
    ensure_db, create_checkout_session, handle_success, handle_webhook,
    check_api_key, send_welcome_email, pricing_page_html, success_page_html,
    dashboard_page_html, get_db
)

_SAAS_DB = "oblivion_finance"
_SAAS_NAME = "OBLIVION Finance"
_SAAS_PATH = "/finance"
_SAAS_PREFIX = "oblivion_fin"
_SAAS_TIERS = [('Free', '£0', ['Search companies on website', 'View SEC filings', 'Basic company data'], '', False), ('API Pro', '£29/mo', ['REST API access', '1,000 requests/day', 'Company + filing data', 'CSV export', 'Email support'], '/finance/checkout/pro', True), ('API Enterprise', '£99/mo', ['Unlimited API requests', 'Bulk data export', 'Webhook notifications', 'Priority support', 'Custom integrations'], '/finance/checkout/enterprise', False)]
_SAAS_PRO_PRICE = 2900
_SAAS_BIZ_PRICE = 9900

# Initialize DB on import
ensure_db(_SAAS_DB)

@app.get("/finance/pricing")
async def _saas_pricing():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(pricing_page_html(_SAAS_NAME, _SAAS_PATH, _SAAS_TIERS))

@app.get("/finance/checkout/pro")
async def _saas_checkout_pro():
    from fastapi.responses import RedirectResponse
    url = create_checkout_session(f"{_SAAS_NAME} Pro", _SAAS_PRO_PRICE, "gbp",
        f"{_SAAS_NAME} Pro subscription", f"{_SAAS_PATH}/success?plan=pro", f"{_SAAS_PATH}/pricing")
    return RedirectResponse(url, status_code=303)

@app.get("/finance/checkout/enterprise")
async def _saas_checkout_biz():
    from fastapi.responses import RedirectResponse
    url = create_checkout_session(f"{_SAAS_NAME} Business", _SAAS_BIZ_PRICE, "gbp",
        f"{_SAAS_NAME} Business subscription", f"{_SAAS_PATH}/success?plan=business", f"{_SAAS_PATH}/pricing")
    return RedirectResponse(url, status_code=303)

@app.get("/finance/success")
async def _saas_success(session_id: str = "", plan: str = "pro"):
    from fastapi.responses import HTMLResponse
    email, api_key = handle_success(session_id, plan, _SAAS_DB, _SAAS_PREFIX)
    plan_name = "Pro" if plan == "pro" else "Business"
    if email:
        send_welcome_email(email, api_key, plan_name, _SAAS_NAME, f"https://oblivionsearch.com{_SAAS_PATH}/dashboard?key={api_key}")
    return HTMLResponse(success_page_html(_SAAS_NAME, email, api_key, plan_name, f"{_SAAS_PATH}/dashboard"))

@app.get("/finance/dashboard")
async def _saas_dashboard(key: str = ""):
    from fastapi.responses import HTMLResponse
    import psycopg2.extras
    account = None
    if key:
        try:
            conn = get_db(_SAAS_DB)
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM saas_api_keys WHERE api_key=%s", (key,))
            account = cur.fetchone()
            cur.close(); conn.close()
        except: pass
    return HTMLResponse(dashboard_page_html(_SAAS_NAME, _SAAS_PATH, account, key))

@app.post("/finance/webhook")
async def _saas_webhook(request: Request):
    body = await request.body()
    handle_webhook(body, _SAAS_DB)
    return {"received": True}

# ── Company profile page (wildcard route - MUST be after all specific /finance/* routes) ──
@app.get("/finance/{ticker}", response_class=HTMLResponse)
async def finance_company(ticker: str, form: Optional[str] = None):
    data = await get_company_data(ticker)
    if not data:
        return HTMLResponse(f"""{_base_head("Not Found")}
{HEADER_HTML}
<main class="container">
    <div class="company-header">
        <a href="/finance" class="back-link">&larr; Back to search</a>
        <h1>Company Not Found</h1>
        <p style="color:#888;margin-top:10px;">Could not find "{ticker}" in SEC EDGAR. Try searching by full company name.</p>
    </div>
</main>
{FOOTER_HTML}
</body></html>""", status_code=404)

    name = data.get("name", "Unknown")
    cik = str(data.get("cik", ""))
    sic = data.get("sic", "N/A")
    sic_desc = data.get("sicDescription", "")
    state = data.get("stateOfIncorporation", "N/A")
    fiscal_end = data.get("fiscalYearEnd", "N/A")
    tickers_list = data.get("tickers", [ticker.upper()])
    display_ticker = tickers_list[0] if tickers_list else ticker.upper()
    website_val = ""
    phone_val = ""
    address_html = ""
    if "addresses" in data and "business" in data["addresses"]:
        addr = data["addresses"]["business"]
        parts = [addr.get("street1", ""), addr.get("street2", ""), addr.get("city", ""),
                 addr.get("stateOrCountry", ""), addr.get("zipCode", "")]
        address_html = ", ".join(p for p in parts if p)
        phone_val = addr.get("phone", "")

    if "website" in data:
        website_val = data["website"]

    # Extract filings
    filter_types = None
    if form:
        filter_types = [f.strip() for f in form.split(",")]
    filings = extract_filings(data, filing_types=filter_types, limit=50)

    filings_html = ""
    if filings:
        rows = ""
        for f in filings:
            ft = f["form"]
            badge_class = "form-10k" if "10-K" in ft else ("form-10q" if "10-Q" in ft else ("form-8k" if "8-K" in ft else "form-other"))
            link = f'<a href="{f["filingUrl"]}" target="_blank">{f["description"] or ft}</a>' if f["filingUrl"] else (f["description"] or ft)
            rows += f"""<tr>
                <td><span class="form-badge {badge_class}">{ft}</span></td>
                <td>{f["filingDate"]}</td>
                <td>{link}</td>
                <td><a href="{f["indexUrl"]}" target="_blank" style="color:#888;font-size:0.85rem;">Index</a></td>
            </tr>"""
        filings_html = f"""
        <div class="filter-pills">
            <a href="/finance/{display_ticker}" class="filter-pill {"active" if not form else ""}">All</a>
            <a href="/finance/{display_ticker}?form=10-K" class="filter-pill {"active" if form=="10-K" else ""}">10-K</a>
            <a href="/finance/{display_ticker}?form=10-Q" class="filter-pill {"active" if form=="10-Q" else ""}">10-Q</a>
            <a href="/finance/{display_ticker}?form=8-K" class="filter-pill {"active" if form=="8-K" else ""}">8-K</a>
            <a href="/finance/{display_ticker}?form=4" class="filter-pill {"active" if form=="4" else ""}">Insider (Form 4)</a>
        </div>
        <table class="filings-table">
            <thead><tr><th>Form</th><th>Filed</th><th>Document</th><th></th></tr></thead>
            <tbody>{rows}</tbody>
        </table>"""
    else:
        filings_html = '<div class="empty">No filings found for this filter.</div>'

    info_cards = f"""
    <div class="info-grid">
        <div class="info-card"><div class="label">CIK</div><div class="value">{cik}</div></div>
        <div class="info-card"><div class="label">SIC Code</div><div class="value">{sic} — {sic_desc}</div></div>
        <div class="info-card"><div class="label">State of Incorporation</div><div class="value">{state}</div></div>
        <div class="info-card"><div class="label">Fiscal Year End</div><div class="value">{fiscal_end}</div></div>
        {"<div class='info-card'><div class='label'>Address</div><div class='value'>" + address_html + "</div></div>" if address_html else ""}
        {"<div class='info-card'><div class='label'>Phone</div><div class='value'>" + phone_val + "</div></div>" if phone_val else ""}
    </div>"""

    return f"""{_base_head(f"{display_ticker} — {name}")}
{HEADER_HTML}
<main class="container">
    <div class="company-header">
        <a href="/finance" class="back-link">&larr; Back to search</a>
        <h1>{name} <span class="badge">{display_ticker}</span></h1>
        <div class="meta">
            <span>CIK: {cik}</span>
            <span>{sic_desc}</span>
            <span>{state}</span>
        </div>
    </div>

    {info_cards}

    <div class="section">
        <h2>SEC Filings</h2>
        {filings_html}
    </div>
</main>
{FOOTER_HTML}
</body></html>"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=3064, log_level="info")
