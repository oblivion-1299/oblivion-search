#!/usr/bin/env python3
"""
OBLIVION Privacy Report Card -- Visual Privacy Grader
FastAPI on port 3072

Different from the Privacy Scanner (port 3061) which is a compliance checker.
This is a shareable "report card" with letter grades, SVG badges, and a
visually rich breakdown designed for sharing and embedding.

Inspired by CaliOpen's privacy scoring concept.
"""

import asyncio
import hashlib
import html as html_mod
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse, urljoin

import asyncpg
import httpx
from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
import uvicorn

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_DSN = "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/postgres"
_pool: Optional[asyncpg.Pool] = None

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    return _pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS oblivion_privacy_reports (
                id SERIAL PRIMARY KEY,
                domain TEXT NOT NULL,
                report JSONB NOT NULL,
                grade TEXT NOT NULL,
                score INTEGER NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                expires_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '24 hours')
            );
            CREATE INDEX IF NOT EXISTS idx_privacy_reports_domain ON oblivion_privacy_reports(domain);
            CREATE INDEX IF NOT EXISTS idx_privacy_reports_expires ON oblivion_privacy_reports(expires_at);
        """)

async def get_cached_report(domain: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT report FROM oblivion_privacy_reports WHERE domain=$1 AND expires_at > NOW() ORDER BY created_at DESC LIMIT 1",
            domain
        )
        if row:
            return json.loads(row["report"]) if isinstance(row["report"], str) else row["report"]
    return None

async def cache_report(domain: str, report: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oblivion_privacy_reports (domain, report, grade, score) VALUES ($1, $2, $3, $4)",
            domain, json.dumps(report), report["grade"], report["score"]
        )

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    await init_db()
    yield
    if _pool:
        await _pool.close()

app = FastAPI(title="OBLIVION Privacy Report Card", version="1.0.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
_rate_store: dict[str, list[float]] = defaultdict(list)
FREE_DAILY_LIMIT = 10

def _check_rate(ip: str) -> bool:
    now = time.time()
    cutoff = now - 86400
    _rate_store[ip] = [t for t in _rate_store[ip] if t > cutoff]
    if len(_rate_store[ip]) >= FREE_DAILY_LIMIT:
        return False
    _rate_store[ip].append(now)
    return True

# ---------------------------------------------------------------------------
# Known trackers (comprehensive list)
# ---------------------------------------------------------------------------
TRACKER_DB = {
    "google-analytics.com": {"name": "Google Analytics", "category": "Analytics", "severity": "high", "icon": "GA"},
    "googletagmanager.com": {"name": "Google Tag Manager", "category": "Tag Manager", "severity": "medium", "icon": "GTM"},
    "facebook.net": {"name": "Facebook Pixel", "category": "Social Tracking", "severity": "critical", "icon": "FB"},
    "connect.facebook.net": {"name": "Facebook Connect", "category": "Social Tracking", "severity": "critical", "icon": "FB"},
    "doubleclick.net": {"name": "DoubleClick", "category": "Advertising", "severity": "critical", "icon": "DC"},
    "googlesyndication.com": {"name": "Google AdSense", "category": "Advertising", "severity": "high", "icon": "AS"},
    "googleadservices.com": {"name": "Google Ads", "category": "Advertising", "severity": "high", "icon": "GA"},
    "amazon-adsystem.com": {"name": "Amazon Ads", "category": "Advertising", "severity": "high", "icon": "AM"},
    "criteo.com": {"name": "Criteo", "category": "Retargeting", "severity": "critical", "icon": "CR"},
    "hotjar.com": {"name": "Hotjar", "category": "Session Recording", "severity": "critical", "icon": "HJ"},
    "clarity.ms": {"name": "Microsoft Clarity", "category": "Session Recording", "severity": "high", "icon": "MC"},
    "fullstory.com": {"name": "FullStory", "category": "Session Recording", "severity": "critical", "icon": "FS"},
    "mouseflow.com": {"name": "Mouseflow", "category": "Session Recording", "severity": "critical", "icon": "MF"},
    "segment.com": {"name": "Segment", "category": "Analytics", "severity": "medium", "icon": "SG"},
    "segment.io": {"name": "Segment", "category": "Analytics", "severity": "medium", "icon": "SG"},
    "mixpanel.com": {"name": "Mixpanel", "category": "Analytics", "severity": "high", "icon": "MP"},
    "hubspot.com": {"name": "HubSpot", "category": "Marketing", "severity": "medium", "icon": "HS"},
    "intercom.io": {"name": "Intercom", "category": "Marketing", "severity": "medium", "icon": "IC"},
    "tiktok.com": {"name": "TikTok Pixel", "category": "Social Tracking", "severity": "critical", "icon": "TT"},
    "snap.licdn.com": {"name": "LinkedIn Insight", "category": "Social Tracking", "severity": "high", "icon": "LI"},
    "ads.linkedin.com": {"name": "LinkedIn Ads", "category": "Advertising", "severity": "high", "icon": "LI"},
    "twitter.com/i/adsct": {"name": "Twitter/X Ads", "category": "Advertising", "severity": "high", "icon": "TW"},
    "pinterest.com/ct.html": {"name": "Pinterest Tag", "category": "Social Tracking", "severity": "medium", "icon": "PT"},
    "adnxs.com": {"name": "AppNexus/Xandr", "category": "Advertising", "severity": "critical", "icon": "AN"},
    "taboola.com": {"name": "Taboola", "category": "Content Ads", "severity": "high", "icon": "TB"},
    "outbrain.com": {"name": "Outbrain", "category": "Content Ads", "severity": "high", "icon": "OB"},
    "quantserve.com": {"name": "Quantcast", "category": "Analytics", "severity": "medium", "icon": "QC"},
    "scorecardresearch.com": {"name": "Scorecard Research", "category": "Analytics", "severity": "medium", "icon": "SC"},
    "newrelic.com": {"name": "New Relic", "category": "Performance", "severity": "low", "icon": "NR"},
    "sentry.io": {"name": "Sentry", "category": "Error Tracking", "severity": "low", "icon": "SN"},
    "amplitude.com": {"name": "Amplitude", "category": "Analytics", "severity": "high", "icon": "AM"},
    "heap.io": {"name": "Heap", "category": "Analytics", "severity": "high", "icon": "HP"},
    "heapanalytics.com": {"name": "Heap Analytics", "category": "Analytics", "severity": "high", "icon": "HP"},
    "optimizely.com": {"name": "Optimizely", "category": "A/B Testing", "severity": "medium", "icon": "OP"},
    "launchdarkly.com": {"name": "LaunchDarkly", "category": "Feature Flags", "severity": "low", "icon": "LD"},
    "rubiconproject.com": {"name": "Rubicon Project", "category": "Advertising", "severity": "critical", "icon": "RP"},
    "pubmatic.com": {"name": "PubMatic", "category": "Advertising", "severity": "critical", "icon": "PM"},
    "openx.net": {"name": "OpenX", "category": "Advertising", "severity": "critical", "icon": "OX"},
    "casalemedia.com": {"name": "Index Exchange", "category": "Advertising", "severity": "high", "icon": "IX"},
    "adsrvr.org": {"name": "The Trade Desk", "category": "Advertising", "severity": "critical", "icon": "TD"},
    "demdex.net": {"name": "Adobe Audience Manager", "category": "DMP", "severity": "critical", "icon": "AD"},
    "omtrdc.net": {"name": "Adobe Analytics", "category": "Analytics", "severity": "high", "icon": "AD"},
    "pardot.com": {"name": "Salesforce Pardot", "category": "Marketing", "severity": "medium", "icon": "SF"},
    "crazyegg.com": {"name": "Crazy Egg", "category": "Session Recording", "severity": "high", "icon": "CE"},
    "inspectlet.com": {"name": "Inspectlet", "category": "Session Recording", "severity": "critical", "icon": "IL"},
    "smartlook.com": {"name": "Smartlook", "category": "Session Recording", "severity": "high", "icon": "SL"},
    "drift.com": {"name": "Drift", "category": "Marketing", "severity": "medium", "icon": "DR"},
    "zopim.com": {"name": "Zendesk Chat", "category": "Support", "severity": "low", "icon": "ZD"},
    "freshchat.com": {"name": "Freshchat", "category": "Support", "severity": "low", "icon": "FC"},
}

COOKIE_CONSENT_PATTERNS = [
    "cookie-consent", "cookie-banner", "cookie-notice", "cookieconsent",
    "gdpr-banner", "gdpr-consent", "consent-banner", "consent-manager",
    "cc-banner", "cc-window", "onetrust", "cookiebot", "osano", "termly",
    "iubenda", "quantcast-choice", "didomi", "usercentrics", "trustarc",
    "cookie-law", "CookieConsent", "js-cookie-consent", "eupopup",
]

PRIVACY_LINK_PATTERNS = [
    "/privacy", "/privacy-policy", "/privacypolicy",
    "/data-protection", "/datenschutz", "/confidentialite",
]

SECURITY_HEADERS = {
    "strict-transport-security": {"name": "HSTS", "weight": 10, "description": "Enforces HTTPS connections"},
    "content-security-policy": {"name": "CSP", "weight": 15, "description": "Controls resource loading"},
    "x-frame-options": {"name": "X-Frame-Options", "weight": 5, "description": "Prevents clickjacking"},
    "x-content-type-options": {"name": "X-Content-Type-Options", "weight": 5, "description": "Prevents MIME sniffing"},
    "referrer-policy": {"name": "Referrer-Policy", "weight": 8, "description": "Controls referer leakage"},
    "permissions-policy": {"name": "Permissions-Policy", "weight": 7, "description": "Controls browser features"},
}


def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _extract_domain(url: str) -> str:
    return urlparse(url).netloc.lower().replace("www.", "")


# ---------------------------------------------------------------------------
# Core Scanner
# ---------------------------------------------------------------------------
async def generate_report(url: str) -> dict:
    url = _normalize_url(url)
    domain = _extract_domain(url)

    # Check cache first
    cached = await get_cached_report(domain)
    if cached:
        cached["cached"] = True
        return cached

    report = {
        "url": url,
        "domain": domain,
        "scanned_at": datetime.utcnow().isoformat() + "Z",
        "cached": False,
        "categories": {},
        "trackers": [],
        "cookies": {"first_party": 0, "third_party": 0, "total": 0},
        "security_headers": {},
        "third_party_domains": [],
        "privacy_policy": False,
        "cookie_banner": False,
        "https": {"enabled": False, "hsts": False},
        "score": 0,
        "grade": "F",
        "summary": "",
    }

    # Fetch the page
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=20.0, verify=False,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "DNT": "1",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        ) as client:
            resp = await client.get(url)
    except httpx.ConnectError:
        raise HTTPException(502, f"Could not connect to {domain}")
    except httpx.TimeoutException:
        raise HTTPException(504, f"Timeout connecting to {domain}")
    except Exception as exc:
        raise HTTPException(502, f"Error fetching {domain}: {exc}")

    html = resp.text
    html_lower = html.lower()
    headers = {k.lower(): v for k, v in resp.headers.items()}

    # Use BeautifulSoup for parsing
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    # --- SCORING SYSTEM (out of 100) ---
    # Start at 100, deduct for issues. Bonus points possible.
    score = 100
    cat_scores = {}

    # ========== 1. HTTPS & Transport Security (20 points max) ==========
    https_score = 20
    final_url = str(resp.url)
    if final_url.startswith("https"):
        report["https"]["enabled"] = True
    else:
        report["https"]["enabled"] = False
        https_score -= 15

    if "strict-transport-security" in headers:
        report["https"]["hsts"] = True
        hsts_val = headers["strict-transport-security"]
        # Check for good max-age
        if "max-age=" in hsts_val:
            try:
                ma = int(re.search(r"max-age=(\d+)", hsts_val).group(1))
                if ma < 31536000:
                    https_score -= 2
            except:
                pass
    else:
        https_score -= 5

    cat_scores["https"] = {"score": max(0, https_score), "max": 20, "label": "HTTPS & Transport"}

    # ========== 2. Security Headers (20 points max) ==========
    sec_score = 20
    header_deductions = 0
    for hdr, info in SECURITY_HEADERS.items():
        present = hdr in headers
        report["security_headers"][hdr] = {
            "present": present,
            "name": info["name"],
            "description": info["description"],
            "value": headers.get(hdr, ""),
        }
        if not present:
            deduction = info["weight"] * 20 / sum(h["weight"] for h in SECURITY_HEADERS.values())
            header_deductions += deduction

    sec_score = max(0, int(sec_score - header_deductions))
    cat_scores["headers"] = {"score": sec_score, "max": 20, "label": "Security Headers"}

    # ========== 3. Trackers (25 points max) ==========
    tracker_score = 25
    trackers_found = []
    for pattern, info in TRACKER_DB.items():
        if pattern in html_lower or pattern in html:
            trackers_found.append({**info, "pattern": pattern})
            sev_penalty = {"critical": 6, "high": 4, "medium": 2, "low": 1}.get(info["severity"], 3)
            tracker_score -= sev_penalty

    report["trackers"] = trackers_found
    tracker_score = max(0, tracker_score)
    cat_scores["trackers"] = {"score": tracker_score, "max": 25, "label": "Tracker Freedom"}

    # ========== 4. Cookies (10 points max) ==========
    cookie_score = 10
    fp_cookies = 0
    tp_cookies = 0

    set_cookies = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else []
    if not set_cookies:
        raw_sc = headers.get("set-cookie", "")
        if raw_sc:
            set_cookies = [raw_sc]

    for cookie_str in set_cookies:
        is_third = False
        for part in cookie_str.lower().split(";"):
            part = part.strip()
            if part.startswith("domain="):
                dom = part.split("=", 1)[1].strip().lstrip(".")
                if dom and dom != domain and not domain.endswith("." + dom):
                    is_third = True
        if is_third:
            tp_cookies += 1
        else:
            fp_cookies += 1

    report["cookies"] = {"first_party": fp_cookies, "third_party": tp_cookies, "total": fp_cookies + tp_cookies}

    if tp_cookies > 0:
        cookie_score -= min(tp_cookies * 3, 7)
    if fp_cookies + tp_cookies > 10:
        cookie_score -= 3

    cookie_score = max(0, cookie_score)
    cat_scores["cookies"] = {"score": cookie_score, "max": 10, "label": "Cookie Hygiene"}

    # ========== 5. Third-party requests (10 points max) ==========
    tp_score = 10
    tp_domains = set()

    for tag in soup.find_all("script", src=True):
        src = tag.get("src", "")
        d = _extract_domain(urljoin(url, src))
        if d and d != domain:
            tp_domains.add(d)

    for tag in soup.find_all("link", href=True):
        href = tag.get("href", "")
        d = _extract_domain(urljoin(url, href))
        if d and d != domain:
            tp_domains.add(d)

    for tag in soup.find_all("img", src=True):
        src = tag.get("src", "")
        if src.startswith("data:"):
            continue
        d = _extract_domain(urljoin(url, src))
        if d and d != domain:
            tp_domains.add(d)

    for tag in soup.find_all("iframe", src=True):
        src = tag.get("src", "")
        d = _extract_domain(urljoin(url, src))
        if d and d != domain:
            tp_domains.add(d)

    report["third_party_domains"] = sorted(tp_domains)
    tp_count = len(tp_domains)

    if tp_count > 20:
        tp_score -= 10
    elif tp_count > 10:
        tp_score -= 7
    elif tp_count > 5:
        tp_score -= 4
    elif tp_count > 2:
        tp_score -= 2

    cat_scores["third_party"] = {"score": max(0, tp_score), "max": 10, "label": "Third-Party Requests"}

    # ========== 6. Privacy Policy (8 points max) ==========
    pp_score = 8
    pp_found = False
    for pp in PRIVACY_LINK_PATTERNS:
        if pp in html_lower:
            pp_found = True
            break
    if not pp_found:
        for a in soup.find_all("a"):
            text = (a.get_text() or "").lower()
            href = (a.get("href") or "").lower()
            if "privacy" in text or "privacy" in href:
                pp_found = True
                break

    report["privacy_policy"] = pp_found
    if not pp_found:
        pp_score = 0

    cat_scores["privacy_policy"] = {"score": pp_score, "max": 8, "label": "Privacy Policy"}

    # ========== 7. Cookie Banner (7 points max) ==========
    cb_score = 7
    cb_found = False
    for pat in COOKIE_CONSENT_PATTERNS:
        if pat.lower() in html_lower:
            cb_found = True
            break

    report["cookie_banner"] = cb_found
    if not cb_found:
        cb_score = 0

    cat_scores["cookie_banner"] = {"score": cb_score, "max": 7, "label": "Cookie Consent"}

    # ========== Calculate final score and grade ==========
    total_score = sum(c["score"] for c in cat_scores.values())
    total_max = sum(c["max"] for c in cat_scores.values())
    report["score"] = int(total_score * 100 / total_max) if total_max > 0 else 0
    report["categories"] = cat_scores

    s = report["score"]
    if s >= 95:
        report["grade"] = "A+"
    elif s >= 90:
        report["grade"] = "A"
    elif s >= 85:
        report["grade"] = "A-"
    elif s >= 80:
        report["grade"] = "B+"
    elif s >= 75:
        report["grade"] = "B"
    elif s >= 70:
        report["grade"] = "B-"
    elif s >= 65:
        report["grade"] = "C+"
    elif s >= 60:
        report["grade"] = "C"
    elif s >= 50:
        report["grade"] = "D"
    else:
        report["grade"] = "F"

    # Summary
    if s >= 90:
        report["summary"] = f"{domain} has excellent privacy practices. Minimal tracking, strong security headers, and good transparency."
    elif s >= 75:
        report["summary"] = f"{domain} has good privacy practices with room for improvement. Some third-party tracking detected."
    elif s >= 60:
        report["summary"] = f"{domain} has average privacy practices. Multiple trackers and missing security features detected."
    elif s >= 40:
        report["summary"] = f"{domain} has poor privacy practices. Significant tracking, weak security headers, and privacy gaps."
    else:
        report["summary"] = f"{domain} has serious privacy issues. Extensive tracking, missing security basics, and poor transparency."

    # Cache the report
    try:
        await cache_report(domain, report)
    except Exception:
        pass  # Don't fail if caching fails

    return report


# ---------------------------------------------------------------------------
# SVG Badge Generator
# ---------------------------------------------------------------------------
def generate_badge_svg(grade: str, score: int, domain: str = "") -> str:
    colors = {
        "A+": "#00b894", "A": "#00b894", "A-": "#00cec9",
        "B+": "#0984e3", "B": "#0984e3", "B-": "#6c5ce7",
        "C+": "#fdcb6e", "C": "#f39c12",
        "D": "#e17055", "F": "#d63031",
    }
    color = colors.get(grade, "#636e72")

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="28" role="img" aria-label="Privacy Score: {grade}">
  <title>Privacy Score: {grade} ({score}/100) | OBLIVION</title>
  <linearGradient id="a" x2="0" y2="100%">
    <stop offset="0" stop-color="#555" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="c"><rect width="240" height="28" rx="5" fill="#fff"/></clipPath>
  <g clip-path="url(#c)">
    <rect width="130" height="28" fill="#1a1a2e"/>
    <rect x="130" width="110" height="28" fill="{color}"/>
    <rect width="240" height="28" fill="url(#a)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,sans-serif" font-size="11">
    <text x="65" y="18" fill="#e8e8f0">Privacy Score</text>
    <text x="185" y="18" font-weight="bold">{grade} ({score}/100)</text>
  </g>
  <text x="234" y="10" font-family="Verdana" font-size="6" fill="#aaa" text-anchor="end">OBLIVION</text>
</svg>'''


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------
@app.get("/api/privacy-report")
async def api_report(request: Request, url: str = Query(..., description="Domain or URL to grade")):
    ip = request.client.host if request.client else "unknown"
    if not _check_rate(ip):
        return JSONResponse(status_code=429, content={
            "error": "Rate limit reached (10/day). Upgrade to OBLIVION Pro for unlimited.",
            "upgrade_url": "https://oblivionsearch.com/privacy-report#pro",
        })
    report = await generate_report(url)
    return JSONResponse(content=report)


@app.get("/api/privacy-report/badge")
async def api_badge(url: str = Query(..., description="Domain to generate badge for")):
    try:
        report = await generate_report(url)
        svg = generate_badge_svg(report["grade"], report["score"], report.get("domain", ""))
        return Response(content=svg, media_type="image/svg+xml",
                       headers={"Cache-Control": "public, max-age=3600"})
    except HTTPException:
        svg = generate_badge_svg("?", 0)
        return Response(content=svg, media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# Visual Report Page
# ---------------------------------------------------------------------------
@app.get("/privacy-report/{domain:path}", response_class=HTMLResponse)
async def report_page(request: Request, domain: str):
    ip = request.client.host if request.client else "unknown"
    if not _check_rate(ip):
        return HTMLResponse(content="<h1>Rate limit reached</h1><p>Try again tomorrow or upgrade to Pro.</p>", status_code=429)

    try:
        report = await generate_report(domain)
    except HTTPException as e:
        return HTMLResponse(content=f"<h1>Error</h1><p>{e.detail}</p>", status_code=e.status_code)

    return HTMLResponse(content=render_report_html(report))


def _grade_color(grade: str) -> str:
    colors = {
        "A+": "#00b894", "A": "#00b894", "A-": "#00cec9",
        "B+": "#0984e3", "B": "#0984e3", "B-": "#6c5ce7",
        "C+": "#fdcb6e", "C": "#f39c12",
        "D": "#e17055", "F": "#d63031",
    }
    return colors.get(grade, "#636e72")


def _cat_bar(label: str, score: int, maxs: int) -> str:
    pct = int(score * 100 / maxs) if maxs else 0
    if pct >= 80:
        color = "#00b894"
    elif pct >= 60:
        color = "#f39c12"
    else:
        color = "#e74c3c"
    return f'''<div class="cat-row">
      <div class="cat-label">{html_mod.escape(label)}</div>
      <div class="cat-bar-bg"><div class="cat-bar-fill" style="width:{pct}%;background:{color}"></div></div>
      <div class="cat-val">{score}/{maxs}</div>
    </div>'''


def render_report_html(r: dict) -> str:
    domain = html_mod.escape(r.get("domain", ""))
    grade = html_mod.escape(r.get("grade", "?"))
    score = r.get("score", 0)
    gc = _grade_color(r.get("grade", "F"))
    summary = html_mod.escape(r.get("summary", ""))

    # Category bars
    cat_bars = ""
    for key in ["https", "headers", "trackers", "cookies", "third_party", "privacy_policy", "cookie_banner"]:
        cat = r.get("categories", {}).get(key, {})
        if cat:
            cat_bars += _cat_bar(cat.get("label", key), cat.get("score", 0), cat.get("max", 1))

    # Trackers list
    tracker_html = ""
    trackers = r.get("trackers", [])
    if trackers:
        tracker_html = '<div class="section"><h3>Trackers Detected (' + str(len(trackers)) + ')</h3><div class="tracker-grid">'
        for t in trackers:
            sev = t.get("severity", "medium")
            sev_cls = {"critical": "sev-critical", "high": "sev-high", "medium": "sev-medium", "low": "sev-low"}.get(sev, "sev-medium")
            tracker_html += f'<div class="tracker-chip {sev_cls}"><strong>{html_mod.escape(t["name"])}</strong><span class="tcat">{html_mod.escape(t["category"])}</span><span class="tsev">{sev.upper()}</span></div>'
        tracker_html += '</div></div>'
    else:
        tracker_html = '<div class="section good-section"><h3>No Trackers Detected</h3><p>This site does not load any known tracking scripts. Excellent!</p></div>'

    # Security headers table
    sec_html = '<div class="section"><h3>Security Headers</h3><table class="hdr-table"><tr><th>Header</th><th>Status</th><th>Purpose</th></tr>'
    for hdr, info in r.get("security_headers", {}).items():
        status_icon = '<span class="check">Present</span>' if info.get("present") else '<span class="miss">Missing</span>'
        sec_html += f'<tr><td><code>{html_mod.escape(info.get("name", hdr))}</code></td><td>{status_icon}</td><td>{html_mod.escape(info.get("description", ""))}</td></tr>'
    sec_html += '</table></div>'

    # Third party domains
    tp_domains = r.get("third_party_domains", [])
    tp_html = ""
    if tp_domains:
        tp_html = f'<div class="section"><h3>Third-Party Domains ({len(tp_domains)})</h3><div class="tp-list">'
        for d in tp_domains[:30]:
            tp_html += f'<span class="tp-chip">{html_mod.escape(d)}</span>'
        if len(tp_domains) > 30:
            tp_html += f'<span class="tp-chip tp-more">+{len(tp_domains) - 30} more</span>'
        tp_html += '</div></div>'

    # Quick facts
    https_icon = "Yes" if r.get("https", {}).get("enabled") else "No"
    hsts_icon = "Yes" if r.get("https", {}).get("hsts") else "No"
    pp_icon = "Found" if r.get("privacy_policy") else "Not Found"
    cb_icon = "Found" if r.get("cookie_banner") else "Not Found"
    ck = r.get("cookies", {})

    badge_url = f"/api/privacy-report/badge?url={domain}"

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Report Card: {domain} | OBLIVION</title>
<meta name="description" content="Privacy report card for {domain}. Grade: {grade}, Score: {score}/100. Powered by OBLIVION.">
<meta property="og:title" content="Privacy Report: {domain} — Grade {grade}">
<meta property="og:description" content="{summary}">
<meta property="og:type" content="website">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#x1F4CB;</text></svg>">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0a0a0f;--card:#13131f;--border:#23233a;--accent:#6c5ce7;--text:#e8e8f0;--muted:#888}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}}
a{{color:#00cec9;text-decoration:none}}
.container{{max-width:860px;margin:0 auto;padding:24px 20px}}

.header{{text-align:center;padding:32px 0 16px}}
.header .logo{{font-size:1.6rem;font-weight:800;letter-spacing:-1px}}
.header .logo span{{color:var(--accent)}}

/* Hero card */
.hero{{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:36px;margin:24px 0;display:flex;align-items:center;gap:36px;flex-wrap:wrap;justify-content:center}}
.grade-ring{{position:relative;width:180px;height:180px;flex-shrink:0}}
.grade-ring svg{{width:180px;height:180px;transform:rotate(-90deg)}}
.grade-ring circle{{fill:none;stroke-width:12;stroke-linecap:round}}
.grade-ring .bg{{stroke:#23233a}}
.grade-ring .fg{{stroke:{gc};stroke-dasharray:{2*3.14159*72:.1f};stroke-dashoffset:{2*3.14159*72*(1-score/100):.1f};transition:stroke-dashoffset 1s ease}}
.grade-center{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center}}
.grade-letter{{font-size:3rem;font-weight:900;color:{gc};line-height:1}}
.grade-score{{font-size:.9rem;color:var(--muted);margin-top:2px}}
.hero-info{{flex:1;min-width:260px}}
.hero-info h2{{font-size:1.5rem;margin-bottom:8px}}
.hero-info .domain-name{{color:var(--muted);font-size:1rem;margin-bottom:12px}}
.hero-info .summary-text{{line-height:1.6;font-size:.95rem;color:#ccc}}
.quick-facts{{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}}
.fact{{padding:6px 14px;border-radius:20px;font-size:.82rem;font-weight:600;border:1px solid var(--border);background:#1a1a2e}}
.fact.good{{color:#00b894;border-color:#00b894}}
.fact.bad{{color:#e74c3c;border-color:#e74c3c}}
.fact.warn{{color:#f39c12;border-color:#f39c12}}

/* Category bars */
.section{{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:24px;margin-bottom:20px}}
.section h3{{font-size:1.1rem;margin-bottom:16px}}
.cat-row{{display:flex;align-items:center;gap:12px;margin-bottom:10px}}
.cat-label{{width:160px;font-size:.88rem;color:#ccc;flex-shrink:0}}
.cat-bar-bg{{flex:1;height:10px;background:#1a1a2e;border-radius:5px;overflow:hidden}}
.cat-bar-fill{{height:100%;border-radius:5px;transition:width .6s ease}}
.cat-val{{width:50px;text-align:right;font-size:.82rem;color:var(--muted);flex-shrink:0}}

/* Trackers */
.tracker-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}}
.tracker-chip{{padding:10px 14px;border-radius:10px;background:#1a1a2e;border-left:3px solid;font-size:.85rem}}
.tracker-chip strong{{display:block;margin-bottom:2px}}
.tracker-chip .tcat{{color:var(--muted);font-size:.78rem}}
.tracker-chip .tsev{{display:inline-block;margin-top:4px;padding:2px 8px;border-radius:10px;font-size:.7rem;font-weight:700}}
.sev-critical{{border-color:#d63031}}.sev-critical .tsev{{background:#d63031;color:#fff}}
.sev-high{{border-color:#e17055}}.sev-high .tsev{{background:#e17055;color:#fff}}
.sev-medium{{border-color:#f39c12}}.sev-medium .tsev{{background:#f39c12;color:#111}}
.sev-low{{border-color:#636e72}}.sev-low .tsev{{background:#636e72;color:#fff}}
.good-section{{border-color:#00b894}}
.good-section h3{{color:#00b894}}

/* Headers table */
.hdr-table{{width:100%;border-collapse:collapse;font-size:.88rem}}
.hdr-table th{{text-align:left;padding:8px;border-bottom:1px solid var(--border);color:var(--muted);font-weight:600}}
.hdr-table td{{padding:8px;border-bottom:1px solid var(--border)}}
.hdr-table code{{background:#1a1a2e;padding:2px 8px;border-radius:4px;font-size:.82rem}}
.check{{color:#00b894;font-weight:600}}
.miss{{color:#e74c3c;font-weight:600}}

/* Third party */
.tp-list{{display:flex;flex-wrap:wrap;gap:8px}}
.tp-chip{{padding:4px 12px;border-radius:16px;background:#1a1a2e;font-size:.8rem;color:#aaa;border:1px solid var(--border)}}
.tp-more{{color:var(--accent)}}

/* Badge / share */
.share-section{{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:24px;margin-bottom:20px;text-align:center}}
.share-section h3{{margin-bottom:12px}}
.badge-preview{{margin:16px 0}}
.embed-code{{background:#1a1a2e;border:1px solid var(--border);border-radius:8px;padding:12px;font-family:monospace;font-size:.8rem;color:#aaa;word-break:break-all;text-align:left;margin-top:12px;cursor:pointer;position:relative}}
.embed-code:hover::after{{content:'Click to copy';position:absolute;top:-24px;right:0;font-size:.7rem;color:var(--accent)}}
.copy-btn{{margin-top:8px;padding:8px 20px;border-radius:8px;border:1px solid var(--accent);background:transparent;color:var(--accent);cursor:pointer;font-size:.85rem}}
.copy-btn:hover{{background:var(--accent);color:#fff}}

.footer{{text-align:center;padding:32px 0;color:var(--muted);font-size:.82rem;border-top:1px solid var(--border);margin-top:32px}}

@media(max-width:600px){{
  .hero{{flex-direction:column;text-align:center}}
  .cat-label{{width:100px}}
  .quick-facts{{justify-content:center}}
}}
@media(max-width:480px){{
  body{{overflow-x:hidden}}
  .container{{padding:12px}}
  .header .logo{{font-size:1.2rem}}
  .hero{{padding:20px;gap:20px}}
  .grade-ring{{width:130px;height:130px}}
  .grade-ring svg{{width:130px;height:130px}}
  .grade-letter{{font-size:2.2rem}}
  .hero-info h2{{font-size:1.2rem}}
  .hero-info .summary-text{{font-size:.85rem}}
  .section{{padding:16px}}
  .section h3{{font-size:1rem}}
  .cat-label{{width:80px;font-size:.8rem}}
  .tracker-grid{{grid-template-columns:1fr}}
  .hdr-table{{font-size:.8rem}}
  .hdr-table th,.hdr-table td{{padding:6px}}
  .tp-chip{{font-size:.75rem}}
  .share-section{{padding:16px}}
  .embed-code{{font-size:.72rem}}
  .fact{{font-size:.75rem;padding:4px 10px}}
  .footer{{font-size:.75rem}}
}}
@media(max-width:375px){{
  .header .logo{{font-size:1rem}}
  .grade-ring{{width:100px;height:100px}}
  .grade-ring svg{{width:100px;height:100px}}
  .grade-letter{{font-size:1.8rem}}
  .hero-info h2{{font-size:1rem}}
  .cat-row{{flex-direction:column;gap:4px}}
  .cat-label{{width:100%}}
  .quick-facts{{gap:6px}}
  .tracker-grid{{grid-template-columns:1fr}}
}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="logo">OBLIVION <span>Privacy Report Card</span></div>
  </div>

  <div class="hero">
    <div class="grade-ring">
      <svg viewBox="0 0 180 180">
        <circle class="bg" cx="90" cy="90" r="72"/>
        <circle class="fg" cx="90" cy="90" r="72"/>
      </svg>
      <div class="grade-center">
        <div class="grade-letter">{grade}</div>
        <div class="grade-score">{score}/100</div>
      </div>
    </div>
    <div class="hero-info">
      <h2>Privacy Report Card</h2>
      <div class="domain-name">{domain}</div>
      <div class="summary-text">{summary}</div>
      <div class="quick-facts">
        <span class="fact {'good' if r.get('https',{}).get('enabled') else 'bad'}">HTTPS: {https_icon}</span>
        <span class="fact {'good' if r.get('https',{}).get('hsts') else 'warn'}">HSTS: {hsts_icon}</span>
        <span class="fact {'good' if not trackers else 'bad'}">{len(trackers)} Trackers</span>
        <span class="fact {'good' if ck.get('third_party',0)==0 else 'bad'}">{ck.get('total',0)} Cookies ({ck.get('third_party',0)} 3P)</span>
        <span class="fact {'good' if r.get('privacy_policy') else 'bad'}">Privacy Policy: {pp_icon}</span>
        <span class="fact {'good' if r.get('cookie_banner') else 'warn'}">Cookie Banner: {cb_icon}</span>
        <span class="fact warn">{len(tp_domains)} Third-Party Domains</span>
      </div>
    </div>
  </div>

  <div class="section">
    <h3>Score Breakdown</h3>
    {cat_bars}
  </div>

  {tracker_html}
  {sec_html}
  {tp_html}

  <div class="share-section">
    <h3>Share This Report</h3>
    <div class="badge-preview">
      <img src="{badge_url}" alt="Privacy Score Badge" width="240" height="28">
    </div>
    <p style="color:var(--muted);font-size:.88rem;margin-bottom:8px">Embed this badge on your site:</p>
    <div class="embed-code" id="embedCode" onclick="copyEmbed()">&lt;a href="https://oblivionsearch.com/privacy-report/{domain}"&gt;&lt;img src="https://oblivionsearch.com/api/privacy-report/badge?url={domain}" alt="Privacy Score" width="240" height="28"&gt;&lt;/a&gt;</div>
    <button class="copy-btn" onclick="copyEmbed()">Copy Embed Code</button>
    <p style="color:var(--muted);font-size:.8rem;margin-top:12px">
      Report generated at {html_mod.escape(r.get('scanned_at',''))} &middot; {'Cached result' if r.get('cached') else 'Fresh scan'} &middot; Results cached for 24 hours
    </p>
  </div>

  <div class="footer">
    Powered by <strong>OBLIVION Privacy Report Card</strong> &mdash; <a href="https://oblivionsearch.com">OblivionSearch.com</a><br>
    <a href="/privacy-report">Scan another website</a> &middot; <a href="https://oblivionsearch.com/privacy-scan">Full Privacy Scanner</a><br>
    &copy; 2026 OBLIVION. Privacy-first by design.
  </div>
</div>
<script>
function copyEmbed(){{
  var code=document.getElementById('embedCode').textContent;
  navigator.clipboard.writeText(code).then(function(){{
    var btn=document.querySelector('.copy-btn');
    btn.textContent='Copied!';
    setTimeout(function(){{btn.textContent='Copy Embed Code'}},2000);
  }});
}}
</script>
</body>
</html>'''


# ---------------------------------------------------------------------------
# Landing Page
# ---------------------------------------------------------------------------
LANDING_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OBLIVION Privacy Report Card -- Grade Any Website's Privacy | Free</title>
<meta name="description" content="Get a privacy report card for any website. Letter grade A+ to F. Check trackers, cookies, security headers, privacy policy. Shareable badge. Free.">
<link rel="canonical" href="https://oblivionsearch.com/privacy-report">
<meta property="og:title" content="OBLIVION Privacy Report Card — Grade Any Website's Privacy">
<meta property="og:description" content="Get a privacy report card for any website. Letter grade A+ to F. Check trackers, cookies, security headers, privacy policy. Shareable badge. Free.">
<meta property="og:url" content="https://oblivionsearch.com/privacy-report">
<meta property="og:type" content="website">
<meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#x1F4CB;</text></svg>">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0a0f;--card:#13131f;--border:#23233a;--accent:#6c5ce7;--accent2:#00cec9;--text:#e8e8f0;--muted:#888}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:var(--accent2);text-decoration:none}
.container{max-width:800px;margin:0 auto;padding:24px 20px}
.header{text-align:center;padding:48px 0 24px}
.header .logo{font-size:2rem;font-weight:800;letter-spacing:-1px}
.header .logo span{color:var(--accent)}
.header p{color:var(--muted);margin-top:8px;font-size:1.05rem}

.scan-box{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:36px;margin:32px 0;text-align:center}
.scan-box h2{font-size:1.4rem;margin-bottom:6px}
.scan-box p{color:var(--muted);margin-bottom:20px;font-size:.92rem}
.input-row{display:flex;gap:12px;max-width:600px;margin:0 auto}
.input-row input{flex:1;padding:14px 18px;border-radius:10px;border:1px solid var(--border);background:#1a1a2e;color:var(--text);font-size:1rem;outline:none}
.input-row input:focus{border-color:var(--accent)}
.input-row button{padding:14px 28px;border-radius:10px;border:none;background:linear-gradient(135deg,var(--accent),#8b5cf6);color:#fff;font-size:1rem;font-weight:600;cursor:pointer;white-space:nowrap}
.input-row button:hover{box-shadow:0 4px 20px rgba(108,92,231,.4)}
.scanning{display:none;margin-top:16px;color:var(--muted);font-size:.9rem}
.scanning.active{display:block}
.scanning .dots::after{content:'';animation:dots 1.4s steps(4,end) infinite}
@keyframes dots{0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'...'}}

.features{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;margin:32px 0}
.feat-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;text-align:center}
.feat-card .feat-icon{font-size:2rem;margin-bottom:8px}
.feat-card h4{font-size:.95rem;margin-bottom:4px}
.feat-card p{font-size:.82rem;color:var(--muted);line-height:1.4}

.how-it-works{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:28px;margin:24px 0}
.how-it-works h3{font-size:1.2rem;margin-bottom:16px;text-align:center}
.steps{display:flex;gap:20px;flex-wrap:wrap;justify-content:center}
.step{flex:1;min-width:180px;text-align:center}
.step .step-num{width:36px;height:36px;border-radius:50%;background:var(--accent);color:#fff;display:inline-flex;align-items:center;justify-content:center;font-weight:700;margin-bottom:8px}
.step p{font-size:.88rem;color:#ccc}

.examples{margin:24px 0;text-align:center}
.examples h3{margin-bottom:12px;font-size:1.1rem}
.example-btns{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
.example-btn{padding:8px 16px;border-radius:20px;background:#1a1a2e;border:1px solid var(--border);color:var(--text);font-size:.85rem;cursor:pointer;text-decoration:none}
.example-btn:hover{border-color:var(--accent);color:var(--accent)}

.footer{text-align:center;padding:32px 0;color:var(--muted);font-size:.82rem;border-top:1px solid var(--border);margin-top:32px}
@media(max-width:600px){.input-row{flex-direction:column}}
@media(max-width:480px){
body{overflow-x:hidden}
.container{padding:12px}
.header .logo{font-size:1.4rem}
.header p{font-size:.9rem}
.scan-box{padding:20px;margin:20px 0}
.scan-box h2{font-size:1.1rem}
.input-row input{font-size:16px}
.input-row button{min-height:44px;font-size:16px}
.features{grid-template-columns:1fr 1fr}
.feat-card{padding:14px}
.feat-card h4{font-size:.88rem}
.feat-card p{font-size:.78rem}
.steps{flex-direction:column;gap:12px}
.how-it-works{padding:18px}
.example-btn{font-size:.8rem;padding:6px 12px}
}
@media(max-width:375px){
.header .logo{font-size:1.1rem}
.features{grid-template-columns:1fr}
.scan-box h2{font-size:1rem}
}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="logo">OBLIVION <span>Privacy Report Card</span></div>
    <p>Grade any website's privacy practices. Get a shareable report card.</p>
  </div>

  <div class="scan-box">
    <h2>Enter a website to grade</h2>
    <p>We check trackers, cookies, security headers, privacy policy, and more.</p>
    <div class="input-row">
      <input type="text" id="urlInput" placeholder="e.g. example.com" autofocus>
      <button id="scanBtn" onclick="doGrade()">Grade It</button>
    </div>
    <div class="scanning" id="scanning">Generating privacy report card<span class="dots"></span></div>
  </div>

  <div class="features">
    <div class="feat-card"><div class="feat-icon">&#x1F50D;</div><h4>Tracker Detection</h4><p>Scans for 50+ known trackers including Google Analytics, Facebook Pixel, Hotjar, and more.</p></div>
    <div class="feat-card"><div class="feat-icon">&#x1F36A;</div><h4>Cookie Analysis</h4><p>Counts first-party vs third-party cookies set on first visit.</p></div>
    <div class="feat-card"><div class="feat-icon">&#x1F512;</div><h4>Security Headers</h4><p>Checks CSP, HSTS, X-Frame-Options, Referrer-Policy, and more.</p></div>
    <div class="feat-card"><div class="feat-icon">&#x1F4DC;</div><h4>Privacy Policy</h4><p>Detects whether a privacy policy is linked on the page.</p></div>
    <div class="feat-card"><div class="feat-icon">&#x1F3F7;</div><h4>Letter Grade</h4><p>A+ to F grade based on 7 privacy categories, 100-point scale.</p></div>
    <div class="feat-card"><div class="feat-icon">&#x1F396;</div><h4>Shareable Badge</h4><p>Get an SVG badge to embed on your site. Show visitors you care about privacy.</p></div>
  </div>

  <div class="how-it-works">
    <h3>How It Works</h3>
    <div class="steps">
      <div class="step"><div class="step-num">1</div><p>Enter any website URL</p></div>
      <div class="step"><div class="step-num">2</div><p>We fetch and analyse the page</p></div>
      <div class="step"><div class="step-num">3</div><p>Get a detailed report with grade</p></div>
      <div class="step"><div class="step-num">4</div><p>Share or embed the badge</p></div>
    </div>
  </div>

  <div class="examples">
    <h3>Try These Examples</h3>
    <div class="example-btns">
      <a class="example-btn" href="/privacy-report/google.com">google.com</a>
      <a class="example-btn" href="/privacy-report/wikipedia.org">wikipedia.org</a>
      <a class="example-btn" href="/privacy-report/github.com">github.com</a>
      <a class="example-btn" href="/privacy-report/duckduckgo.com">duckduckgo.com</a>
      <a class="example-btn" href="/privacy-report/oblivionsearch.com">oblivionsearch.com</a>
    </div>
  </div>

  <div class="footer">
    Powered by <strong>OBLIVION</strong> &mdash; <a href="https://oblivionsearch.com">OblivionSearch.com</a><br>
    <a href="https://oblivionsearch.com/privacy-scan">Full Privacy Scanner</a> &middot; <a href="https://oblivionsearch.com">Search Engine</a><br>
    &copy; 2026 OBLIVION. Privacy-first by design.
  </div>
</div>
<script>
function doGrade(){
  var url=document.getElementById('urlInput').value.trim();
  if(!url) return;
  // Clean up the URL to just domain
  url=url.replace(/^https?:\/\//,'').replace(/\/.*$/,'');
  document.getElementById('scanning').classList.add('active');
  document.getElementById('scanBtn').disabled=true;
  window.location.href='/privacy-report/'+encodeURIComponent(url);
}
document.getElementById('urlInput').addEventListener('keydown',function(e){
  if(e.key==='Enter') doGrade();
});
</script>
</body>
</html>'''

@app.get("/privacy-report", response_class=HTMLResponse)
async def landing():
    return HTMLResponse(content=LANDING_HTML)

@app.get("/", response_class=HTMLResponse)
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/privacy-report")

@app.get("/health")
async def health():
    return {"status": "ok", "service": "oblivion-privacy-report"}




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

_SAAS_DB = "oblivion_privacy_reports"
_SAAS_NAME = "OBLIVION Privacy Report"
_SAAS_PATH = "/privacy-report"
_SAAS_PREFIX = "oblivion_pr"
_SAAS_TIERS = [('Free', '£0', ['3 reports/day', 'Basic privacy grade', 'Issue list'], '', False), ('Pro', '£24/mo', ['Unlimited reports', 'REST API', 'Scheduled weekly reports', 'PDF export', 'Privacy badge'], '/privacy-report/checkout/pro', True), ('Business', '£89/mo', ['Everything in Pro', 'GDPR/CCPA compliance reports', 'Team dashboard', 'Custom branding', 'Priority support'], '/privacy-report/checkout/enterprise', False)]
_SAAS_PRO_PRICE = 2400
_SAAS_BIZ_PRICE = 8900

# Initialize DB on import
ensure_db(_SAAS_DB)

@app.get("/privacy-report/pricing")
async def _saas_pricing():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(pricing_page_html(_SAAS_NAME, _SAAS_PATH, _SAAS_TIERS))

@app.get("/privacy-report/checkout/pro")
async def _saas_checkout_pro():
    from fastapi.responses import RedirectResponse
    url = create_checkout_session(f"{_SAAS_NAME} Pro", _SAAS_PRO_PRICE, "gbp",
        f"{_SAAS_NAME} Pro subscription", f"{_SAAS_PATH}/success?plan=pro", f"{_SAAS_PATH}/pricing")
    return RedirectResponse(url, status_code=303)

@app.get("/privacy-report/checkout/enterprise")
async def _saas_checkout_biz():
    from fastapi.responses import RedirectResponse
    url = create_checkout_session(f"{_SAAS_NAME} Business", _SAAS_BIZ_PRICE, "gbp",
        f"{_SAAS_NAME} Business subscription", f"{_SAAS_PATH}/success?plan=business", f"{_SAAS_PATH}/pricing")
    return RedirectResponse(url, status_code=303)

@app.get("/privacy-report/success")
async def _saas_success(session_id: str = "", plan: str = "pro"):
    from fastapi.responses import HTMLResponse
    email, api_key = handle_success(session_id, plan, _SAAS_DB, _SAAS_PREFIX)
    plan_name = "Pro" if plan == "pro" else "Business"
    if email:
        send_welcome_email(email, api_key, plan_name, _SAAS_NAME, f"https://oblivionsearch.com{_SAAS_PATH}/dashboard?key={api_key}")
    return HTMLResponse(success_page_html(_SAAS_NAME, email, api_key, plan_name, f"{_SAAS_PATH}/dashboard"))

@app.get("/privacy-report/dashboard")
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

@app.post("/privacy-report/webhook")
async def _saas_webhook(request):
    body = await request.body()
    handle_webhook(body, _SAAS_DB)
    return {"received": True}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3075)
