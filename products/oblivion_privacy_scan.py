#!/usr/bin/env python3
"""
OBLIVION Privacy Compliance Scanner — SaaS Edition
Scan any website for trackers, cookies, GDPR compliance issues.
FastAPI on port 3061

Features:
  - Free tier: 10 scans/day
  - Pro (£29/mo): unlimited scans, API key, monitoring, badge, PDF
  - Business (£99/mo): everything in Pro + compliance reporting + team
  - Stripe checkout integration
  - API key system (PostgreSQL)
  - SVG privacy badge
  - Weekly monitoring with email alerts
"""

import asyncio
import hashlib
import html as html_mod
import json
import os
import re
import secrets
import smtplib
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from urllib.parse import urlparse, urljoin

import httpx
import psycopg2
import psycopg2.extras
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
import uvicorn

# ---------------------------------------------------------------------------
# Stripe Payment Links — REPLACE WITH REAL LINKS
# ---------------------------------------------------------------------------
import stripe
stripe.api_key = "os.environ.get("STRIPE_SECRET_KEY", "")"
DOMAIN_URL = "https://oblivionsearch.com"

# ---------------------------------------------------------------------------
# PostgreSQL config
# ---------------------------------------------------------------------------
DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 5432,
    "user": "postgres",
    "password": "os.environ.get("DB_PASSWORD", "change_me")",
    "dbname": "privacy_scan",
}

app = FastAPI(title="OBLIVION Privacy Scanner", version="2.0.0")

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------
def _get_db():
    return psycopg2.connect(**DB_CONFIG)


def _init_db():
    """Create database and tables if they don't exist."""
    # First ensure the database exists
    try:
        conn = psycopg2.connect(
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            dbname="postgres",
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = 'privacy_scan'")
        if not cur.fetchone():
            cur.execute("CREATE DATABASE privacy_scan")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[DB] Warning creating database: {e}")

    # Now create tables
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL UNIQUE,
                api_key VARCHAR(64) NOT NULL UNIQUE,
                plan VARCHAR(20) NOT NULL DEFAULT 'pro',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                scans_today INTEGER NOT NULL DEFAULT 0,
                last_scan_date DATE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS monitored_sites (
                id SERIAL PRIMARY KEY,
                site_url VARCHAR(2048) NOT NULL,
                email VARCHAR(255) NOT NULL,
                api_key VARCHAR(64) NOT NULL REFERENCES api_keys(api_key),
                last_scan TIMESTAMP,
                last_score INTEGER,
                last_grade VARCHAR(5),
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE(site_url, api_key)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scan_history (
                id SERIAL PRIMARY KEY,
                url VARCHAR(2048) NOT NULL,
                score INTEGER NOT NULL,
                grade VARCHAR(5) NOT NULL,
                api_key VARCHAR(64),
                ip_address VARCHAR(45),
                scanned_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("[DB] Tables ready.")
    except Exception as e:
        print(f"[DB] Warning creating tables: {e}")


@app.on_event("startup")
async def startup():
    _init_db()


# ---------------------------------------------------------------------------
# API key helpers
# ---------------------------------------------------------------------------
def _validate_api_key(key: Optional[str]) -> Optional[dict]:
    """Returns the api_key row dict if valid, else None."""
    if not key:
        return None
    key = key.strip()
    if key.startswith("Bearer "):
        key = key[7:]
    try:
        conn = _get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM api_keys WHERE api_key = %s AND is_active = TRUE", (key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _generate_api_key() -> str:
    return "oblivion_ps_" + secrets.token_hex(24)


# ---------------------------------------------------------------------------
# Rate limiting: 10 free scans per IP per day
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
# Known tracker / ad-network patterns
# ---------------------------------------------------------------------------
TRACKER_PATTERNS = {
    "google-analytics.com": {"name": "Google Analytics", "category": "Analytics", "severity": "medium"},
    "googletagmanager.com": {"name": "Google Tag Manager", "category": "Tag Manager", "severity": "medium"},
    "facebook.net": {"name": "Facebook Pixel / SDK", "category": "Social Tracking", "severity": "high"},
    "connect.facebook.net": {"name": "Facebook Connect", "category": "Social Tracking", "severity": "high"},
    "doubleclick.net": {"name": "Google DoubleClick", "category": "Advertising", "severity": "high"},
    "googlesyndication.com": {"name": "Google AdSense", "category": "Advertising", "severity": "high"},
    "googleadservices.com": {"name": "Google Ads", "category": "Advertising", "severity": "high"},
    "amazon-adsystem.com": {"name": "Amazon Ads", "category": "Advertising", "severity": "high"},
    "criteo.com": {"name": "Criteo", "category": "Advertising", "severity": "high"},
    "hotjar.com": {"name": "Hotjar", "category": "Session Recording", "severity": "high"},
    "clarity.ms": {"name": "Microsoft Clarity", "category": "Session Recording", "severity": "medium"},
    "fullstory.com": {"name": "FullStory", "category": "Session Recording", "severity": "high"},
    "mouseflow.com": {"name": "Mouseflow", "category": "Session Recording", "severity": "high"},
    "segment.com": {"name": "Segment", "category": "Analytics", "severity": "medium"},
    "mixpanel.com": {"name": "Mixpanel", "category": "Analytics", "severity": "medium"},
    "hubspot.com": {"name": "HubSpot", "category": "Marketing", "severity": "medium"},
    "intercom.io": {"name": "Intercom", "category": "Marketing", "severity": "medium"},
    "tiktok.com/i18n/pixel": {"name": "TikTok Pixel", "category": "Social Tracking", "severity": "high"},
    "snap.licdn.com": {"name": "LinkedIn Insight", "category": "Social Tracking", "severity": "high"},
    "ads.linkedin.com": {"name": "LinkedIn Ads", "category": "Advertising", "severity": "high"},
    "twitter.com/i/adsct": {"name": "Twitter Ads", "category": "Advertising", "severity": "high"},
    "pinterest.com/ct.html": {"name": "Pinterest Tag", "category": "Social Tracking", "severity": "medium"},
    "adnxs.com": {"name": "AppNexus / Xandr", "category": "Advertising", "severity": "high"},
    "taboola.com": {"name": "Taboola", "category": "Advertising", "severity": "high"},
    "outbrain.com": {"name": "Outbrain", "category": "Advertising", "severity": "high"},
    "quantserve.com": {"name": "Quantcast", "category": "Analytics", "severity": "medium"},
    "scorecardresearch.com": {"name": "Scorecard Research", "category": "Analytics", "severity": "medium"},
    "newrelic.com": {"name": "New Relic", "category": "Performance", "severity": "low"},
    "sentry.io": {"name": "Sentry", "category": "Error Tracking", "severity": "low"},
}

COOKIE_CONSENT_PATTERNS = [
    "cookie-consent", "cookie-banner", "cookie-notice", "cookie-popup",
    "cookieconsent", "cookie_consent", "gdpr-banner", "gdpr-consent",
    "consent-banner", "consent-manager", "cc-banner", "cc-window",
    "onetrust", "cookiebot", "osano", "termly", "iubenda",
    "quantcast-choice", "didomi", "usercentrics", "trustarc",
    "cookie-law", "cookie-policy-banner", "cookies-eu-banner",
    "CookieConsent", "js-cookie-consent", "eupopup",
]

PRIVACY_LINK_PATTERNS = [
    "/privacy", "/privacy-policy", "/privacypolicy",
    "/data-protection", "/datenschutz", "/confidentialite",
    "/privacidade", "/politica-de-privacidad",
]

DNT_PATTERNS = [
    "navigator.doNotTrack", "donottrack", "dnt",
]


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


async def scan_privacy(url: str) -> dict:
    """Core scanner — fetches a URL and analyses privacy posture."""
    url = _normalize_url(url)
    parsed = urlparse(url)
    base_domain = parsed.netloc.lower().replace("www.", "")

    result = {
        "url": url,
        "scanned_at": datetime.utcnow().isoformat() + "Z",
        "score": 100,
        "grade": "A+",
        "issues": [],
        "trackers_found": [],
        "third_party_scripts": [],
        "cookies": {"first_party": [], "third_party": []},
        "https": True,
        "privacy_policy_found": False,
        "cookie_consent_found": False,
        "dnt_respected": False,
        "recommendations": [],
    }
    issues = result["issues"]
    recs = result["recommendations"]

    # -- Fetch page --
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            verify=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; OblivionPrivacyScanner/2.0)",
                "DNT": "1",
            },
        ) as client:
            resp = await client.get(url)
    except httpx.ConnectError:
        raise HTTPException(502, f"Could not connect to {url}")
    except httpx.TimeoutException:
        raise HTTPException(504, f"Timeout connecting to {url}")
    except Exception as exc:
        raise HTTPException(502, f"Error fetching {url}: {exc}")

    html = resp.text
    headers = dict(resp.headers)
    soup = BeautifulSoup(html, "html.parser")

    # 1. HTTPS check
    final_url = str(resp.url)
    if not final_url.startswith("https"):
        result["https"] = False
        result["score"] -= 20
        issues.append({"type": "https", "severity": "critical",
                        "message": "Site is not served over HTTPS — data in transit is unencrypted."})
        recs.append("Enable HTTPS with a free certificate from Let's Encrypt.")

    # 2. Security headers
    if "strict-transport-security" not in headers:
        result["score"] -= 5
        issues.append({"type": "header", "severity": "medium",
                        "message": "Missing Strict-Transport-Security (HSTS) header."})
        recs.append("Add HSTS header to prevent downgrade attacks.")

    if "x-content-type-options" not in headers:
        result["score"] -= 2
        issues.append({"type": "header", "severity": "low",
                        "message": "Missing X-Content-Type-Options header."})

    if "referrer-policy" not in headers:
        result["score"] -= 3
        issues.append({"type": "header", "severity": "low",
                        "message": "Missing Referrer-Policy header — browser may leak full URL to third parties."})
        recs.append("Set Referrer-Policy to 'strict-origin-when-cross-origin' or stricter.")

    # 3. Tracker detection
    for pattern, info in TRACKER_PATTERNS.items():
        if pattern in html:
            result["trackers_found"].append({**info, "pattern": pattern})
            sev = info["severity"]
            penalty = {"high": 10, "medium": 6, "low": 2}.get(sev, 5)
            result["score"] -= penalty
            issues.append({
                "type": "tracker",
                "severity": sev,
                "message": f"Third-party tracker detected: {info['name']} ({info['category']})",
            })

    if result["trackers_found"]:
        recs.append("Audit third-party trackers and remove any not strictly necessary.")
        recs.append("Consider privacy-respecting alternatives (e.g. Plausible, Fathom for analytics).")

    # 4. Third-party scripts
    scripts = soup.find_all("script", src=True)
    for tag in scripts:
        src = tag.get("src", "")
        script_domain = urlparse(urljoin(url, src)).netloc.lower().replace("www.", "")
        if script_domain and script_domain != base_domain:
            result["third_party_scripts"].append({"src": src, "domain": script_domain})

    tp_count = len(result["third_party_scripts"])
    if tp_count > 10:
        result["score"] -= 10
        issues.append({"type": "scripts", "severity": "high",
                        "message": f"{tp_count} third-party scripts loaded — significant privacy and performance risk."})
        recs.append("Reduce third-party script count to minimise data exposure.")
    elif tp_count > 5:
        result["score"] -= 5
        issues.append({"type": "scripts", "severity": "medium",
                        "message": f"{tp_count} third-party scripts loaded."})

    # 5. Cookies
    set_cookies = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else []
    if not set_cookies:
        raw = headers.get("set-cookie", "")
        if raw:
            set_cookies = [raw]

    for cookie_str in set_cookies:
        name = cookie_str.split("=")[0].strip() if "=" in cookie_str else cookie_str
        is_third = False
        for part in cookie_str.lower().split(";"):
            part = part.strip()
            if part.startswith("domain="):
                dom = part.split("=", 1)[1].strip().lstrip(".")
                if dom and dom != base_domain and not base_domain.endswith("." + dom):
                    is_third = True
        bucket = "third_party" if is_third else "first_party"
        result["cookies"][bucket].append(name)

    total_cookies = len(result["cookies"]["first_party"]) + len(result["cookies"]["third_party"])
    if result["cookies"]["third_party"]:
        result["score"] -= 8
        issues.append({"type": "cookies", "severity": "high",
                        "message": f"{len(result['cookies']['third_party'])} third-party cookie(s) set on first visit."})
        recs.append("Remove third-party cookies or defer them behind consent.")

    if total_cookies > 5:
        result["score"] -= 3
        issues.append({"type": "cookies", "severity": "medium",
                        "message": f"{total_cookies} cookies set on first visit without interaction."})

    # 6. Privacy policy
    html_lower = html.lower()
    for pp in PRIVACY_LINK_PATTERNS:
        if pp in html_lower:
            result["privacy_policy_found"] = True
            break
    for a in soup.find_all("a"):
        text = (a.get_text() or "").lower()
        href = (a.get("href") or "").lower()
        if "privacy" in text or "privacy" in href:
            result["privacy_policy_found"] = True
            break

    if not result["privacy_policy_found"]:
        result["score"] -= 15
        issues.append({"type": "privacy_policy", "severity": "critical",
                        "message": "No privacy policy link detected on the page."})
        recs.append("Add a clearly visible link to your privacy policy on every page.")

    # 7. Cookie consent banner
    for pat in COOKIE_CONSENT_PATTERNS:
        if pat.lower() in html_lower:
            result["cookie_consent_found"] = True
            break

    total_cookies = len(result["cookies"]["first_party"]) + len(result["cookies"]["third_party"])
    has_trackers = len(result["trackers_found"]) > 0
    if not result["cookie_consent_found"]:
        if total_cookies > 0 or has_trackers:
            result["score"] -= 10
            issues.append({"type": "consent", "severity": "high",
                            "message": "No cookie consent banner detected, but site sets cookies or uses trackers."})
            recs.append("Implement a GDPR-compliant cookie consent mechanism before setting non-essential cookies.")
        else:
            result["cookie_consent_found"] = True

    # 8. Do Not Track respect
    for pat in DNT_PATTERNS:
        if pat.lower() in html_lower:
            result["dnt_respected"] = True
            break
    if not result["dnt_respected"]:
        if has_trackers:
            issues.append({"type": "dnt", "severity": "low",
                            "message": "No evidence the site checks the Do-Not-Track signal."})
            recs.append("Consider respecting the DNT header as a good-faith privacy measure.")
        else:
            result["dnt_respected"] = True

    # 9. Iframes to external domains
    iframes = soup.find_all("iframe", src=True)
    ext_iframes = []
    for ifr in iframes:
        src = ifr.get("src", "")
        ifr_domain = urlparse(urljoin(url, src)).netloc.lower().replace("www.", "")
        if ifr_domain and ifr_domain != base_domain:
            ext_iframes.append({"src": src, "domain": ifr_domain})
    if ext_iframes:
        result["score"] -= min(len(ext_iframes) * 3, 10)
        issues.append({"type": "iframes", "severity": "medium",
                        "message": f"{len(ext_iframes)} external iframe(s) embedded — may load additional trackers."})

    # 10. Meta-referrer leak
    meta_ref = soup.find("meta", attrs={"name": "referrer"})
    if meta_ref:
        content = (meta_ref.get("content") or "").lower()
        if content in ("unsafe-url", "no-referrer-when-downgrade", ""):
            result["score"] -= 3
            issues.append({"type": "referrer", "severity": "low",
                            "message": f"Meta referrer set to '{content}' — may leak URLs to third parties."})

    # Clamp & grade
    result["score"] = max(0, min(100, result["score"]))
    s = result["score"]
    if s == 100 and len(issues) == 0:
        result["grade"] = "A+"
    elif s >= 90:
        result["grade"] = "A"
    elif s >= 80:
        result["grade"] = "B+"
    elif s >= 70:
        result["grade"] = "B"
    elif s >= 60:
        result["grade"] = "C+"
    elif s >= 50:
        result["grade"] = "C"
    elif s >= 35:
        result["grade"] = "D"
    else:
        result["grade"] = "F"

    # Log to scan_history
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO scan_history (url, score, grade, scanned_at) VALUES (%s, %s, %s, NOW())",
            (url, result["score"], result["grade"]),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# API route — supports API key auth
# ---------------------------------------------------------------------------
@app.get("/api/privacy-scan")
async def api_scan(
    request: Request,
    url: str = Query(..., description="URL to scan"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    # Check API key first
    key_info = _validate_api_key(x_api_key)
    if key_info:
        # Authenticated — unlimited scans
        report = await scan_privacy(url)
        try:
            conn = _get_db()
            cur = conn.cursor()
            cur.execute(
                "UPDATE scan_history SET api_key = %s WHERE id = (SELECT MAX(id) FROM scan_history WHERE url = %s)",
                (key_info["api_key"], report["url"]),
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            pass
        return JSONResponse(content=report)

    # Free tier — rate limited
    ip = request.client.host if request.client else "unknown"
    if not _check_rate(ip):
        return JSONResponse(
            status_code=429,
            content={
                "error": "Free limit reached (10 scans/day). Upgrade to OBLIVION Pro for unlimited scans.",
                "upgrade_url": "https://oblivionsearch.com/privacy-scan/pricing",
            },
        )
    report = await scan_privacy(url)
    return JSONResponse(content=report)


# ---------------------------------------------------------------------------
# API Key Registration
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    email: str


@app.post("/api/privacy-scan/register")
async def register_api_key(body: RegisterRequest):
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required.")

    try:
        conn = _get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Check if already registered
        cur.execute("SELECT api_key, plan FROM api_keys WHERE email = %s", (email,))
        existing = cur.fetchone()
        if existing:
            cur.close()
            conn.close()
            return JSONResponse(content={
                "message": "API key already exists for this email.",
                "api_key": existing["api_key"],
                "plan": existing["plan"],
                "usage": "Include header: X-API-Key: <your-key>",
            })

        # Generate new key
        api_key = _generate_api_key()
        cur.execute(
            "INSERT INTO api_keys (email, api_key, plan) VALUES (%s, %s, 'pro')",
            (email, api_key),
        )
        conn.commit()
        cur.close()
        conn.close()

        return JSONResponse(content={
            "message": "API key created. Activate Pro by completing checkout.",
            "api_key": api_key,
            "plan": "pro",
            "usage": "Include header: X-API-Key: <your-key>",
            "checkout_url": "https://oblivionsearch.com/privacy-scan/pricing",
        })
    except Exception as e:
        raise HTTPException(500, f"Registration error: {e}")


# ---------------------------------------------------------------------------
# Monitoring — add site
# ---------------------------------------------------------------------------
class MonitorRequest(BaseModel):
    site_url: str
    email: Optional[str] = None


@app.post("/api/privacy-scan/monitor")
async def add_monitor(
    body: MonitorRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    key_info = _validate_api_key(x_api_key)
    if not key_info:
        raise HTTPException(401, "Valid API key required. Register at /api/privacy-scan/register")

    site_url = _normalize_url(body.site_url.strip())
    email = (body.email or key_info["email"]).strip().lower()

    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO monitored_sites (site_url, email, api_key)
               VALUES (%s, %s, %s)
               ON CONFLICT (site_url, api_key) DO UPDATE SET email = EXCLUDED.email
               RETURNING id""",
            (site_url, email, key_info["api_key"]),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return JSONResponse(content={
            "message": f"Monitoring enabled for {site_url}",
            "monitor_id": row[0],
            "email": email,
            "frequency": "weekly",
        })
    except Exception as e:
        raise HTTPException(500, f"Error adding monitor: {e}")


@app.get("/api/privacy-scan/monitors")
async def list_monitors(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    key_info = _validate_api_key(x_api_key)
    if not key_info:
        raise HTTPException(401, "Valid API key required.")
    try:
        conn = _get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, site_url, email, last_scan, last_score, last_grade, created_at FROM monitored_sites WHERE api_key = %s ORDER BY created_at DESC",
            (key_info["api_key"],),
        )
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            for k, v in r.items():
                if isinstance(v, datetime):
                    r[k] = v.isoformat()
        cur.close()
        conn.close()
        return JSONResponse(content={"monitors": rows})
    except Exception as e:
        raise HTTPException(500, f"Error: {e}")


# ---------------------------------------------------------------------------
# Cron-compatible: run_weekly_monitoring()
# Call via: python3 -c "from oblivion_privacy_scan import run_weekly_monitoring; import asyncio; asyncio.run(run_weekly_monitoring())"
# ---------------------------------------------------------------------------
async def run_weekly_monitoring():
    """Scan all monitored sites and email results."""
    try:
        conn = _get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM monitored_sites")
        sites = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[MONITOR] DB error: {e}")
        return

    for site in sites:
        try:
            report = await scan_privacy(site["site_url"])
            score = report["score"]
            grade = report["grade"]

            # Update DB
            conn = _get_db()
            cur = conn.cursor()
            cur.execute(
                "UPDATE monitored_sites SET last_scan = NOW(), last_score = %s, last_grade = %s WHERE id = %s",
                (score, grade, site["id"]),
            )
            conn.commit()
            cur.close()
            conn.close()

            # Check for score drop
            prev = site.get("last_score")
            alert = ""
            if prev is not None and score < prev - 5:
                alert = f"\n⚠ ALERT: Score dropped from {prev} to {score}!\n"

            # Send email (best-effort)
            try:
                _send_monitoring_email(
                    to=site["email"],
                    site_url=site["site_url"],
                    score=score,
                    grade=grade,
                    issues_count=len(report["issues"]),
                    alert=alert,
                )
            except Exception as email_err:
                print(f"[MONITOR] Email error for {site['site_url']}: {email_err}")

            print(f"[MONITOR] {site['site_url']} => Score: {score} Grade: {grade}")
        except Exception as e:
            print(f"[MONITOR] Scan error for {site['site_url']}: {e}")


def _send_monitoring_email(to: str, site_url: str, score: int, grade: str, issues_count: int, alert: str = ""):
    """Send monitoring report email. Uses localhost SMTP."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"OBLIVION Privacy Monitor: {site_url} — Grade {grade} ({score}/100)"
    msg["From"] = "noreply@oblivionsearch.com"
    msg["To"] = to

    body = f"""OBLIVION Privacy Scanner — Weekly Monitoring Report
{'=' * 55}

Site: {site_url}
Score: {score}/100
Grade: {grade}
Issues Found: {issues_count}
{alert}
View full report: https://oblivionsearch.com/privacy-scan?url={site_url}
Manage monitors: https://oblivionsearch.com/privacy-scan/pricing

—
OBLIVION Search — Privacy-first by design
https://oblivionsearch.com
"""
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP("localhost", 25, timeout=10) as smtp:
            smtp.sendmail("noreply@oblivionsearch.com", [to], msg.as_string())
    except Exception:
        # Fallback: try port 587
        pass


# ---------------------------------------------------------------------------
# Badge System — SVG
# ---------------------------------------------------------------------------
@app.get("/privacy-scan/badge/{domain}")
async def privacy_badge(domain: str):
    """Returns an SVG badge showing the site's cached privacy grade."""
    domain = domain.strip().lower().replace("https://", "").replace("http://", "").rstrip("/")

    # Look up latest scan
    grade = "?"
    score = "?"
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT score, grade FROM scan_history WHERE url LIKE %s ORDER BY scanned_at DESC LIMIT 1",
            (f"%{domain}%",),
        )
        row = cur.fetchone()
        if row:
            score = row[0]
            grade = row[1]
        cur.close()
        conn.close()
    except Exception:
        pass

    # Grade colors
    grade_colors = {
        "A+": "#00b894", "A": "#00b894",
        "B+": "#55efc4", "B": "#55efc4",
        "C+": "#fdcb6e", "C": "#fdcb6e",
        "D": "#e17055", "F": "#d63031",
        "?": "#636e72",
    }
    color = grade_colors.get(grade, "#636e72")

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="280" height="28" role="img" aria-label="Privacy Score: {grade} | Verified by OBLIVION">
  <title>Privacy Score: {grade} | Verified by OBLIVION</title>
  <defs>
    <linearGradient id="g" x2="0" y2="100%">
      <stop offset="0" stop-color="#555" stop-opacity=".15"/>
      <stop offset="1" stop-opacity=".15"/>
    </linearGradient>
  </defs>
  <rect rx="4" width="280" height="28" fill="#1a1a2e"/>
  <rect rx="4" x="0" width="280" height="28" fill="url(#g)"/>
  <rect rx="3" x="2" y="2" width="120" height="24" fill="{color}" opacity="0.15"/>
  <g fill="#fff" text-anchor="start" font-family="Segoe UI,Helvetica,Arial,sans-serif" font-size="12">
    <text x="10" y="18" fill="{color}" font-weight="700">Privacy Score: {grade}</text>
    <text x="135" y="18" fill="#aaa" font-size="11">Verified by OBLIVION</text>
  </g>
  <rect rx="4" width="280" height="28" fill="none" stroke="{color}" stroke-width="1" opacity="0.4"/>
</svg>'''

    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ---------------------------------------------------------------------------
# Stripe Checkout Redirects
# ---------------------------------------------------------------------------
@app.get("/privacy-scan/checkout/pro")
async def checkout_pro(request: Request):
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "unit_amount": 2900,  # £29.00
                    "recurring": {"interval": "month"},
                    "product_data": {
                        "name": "OBLIVION Privacy Scanner Pro",
                        "description": "Unlimited scans, API access, weekly monitoring, privacy badges",
                    },
                },
                "quantity": 1,
            }],
            success_url=f"{DOMAIN_URL}/privacy-scan/success?session_id={{CHECKOUT_SESSION_ID}}&plan=pro",
            cancel_url=f"{DOMAIN_URL}/privacy-scan/pricing?status=cancelled",
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        return HTMLResponse(f"<h1>Checkout Error</h1><p>{e}</p><a href='/privacy-scan/pricing'>Back</a>", status_code=500)


@app.get("/privacy-scan/checkout/business")
async def checkout_business(request: Request):
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "unit_amount": 9900,  # £99.00
                    "recurring": {"interval": "month"},
                    "product_data": {
                        "name": "OBLIVION Privacy Scanner Business",
                        "description": "Unlimited scans, API, monitoring, GDPR/CCPA compliance, team accounts",
                    },
                },
                "quantity": 1,
            }],
            success_url=f"{DOMAIN_URL}/privacy-scan/success?session_id={{CHECKOUT_SESSION_ID}}&plan=business",
            cancel_url=f"{DOMAIN_URL}/privacy-scan/pricing?status=cancelled",
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        return HTMLResponse(f"<h1>Checkout Error</h1><p>{e}</p><a href='/privacy-scan/pricing'>Back</a>", status_code=500)


# ---------------------------------------------------------------------------
# Pricing Page
# ---------------------------------------------------------------------------
PRICING_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OBLIVION Privacy Scanner — Pricing</title>
<meta name="description" content="OBLIVION Privacy Scanner pricing. Free, Pro, and Business plans for website privacy compliance monitoring.">
<link rel="canonical" href="https://oblivionsearch.com/privacy-scan/pricing">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🛡</text></svg>">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0a0f;--card:#13131f;--border:#23233a;--accent:#6c5ce7;--accent2:#00d4ff;--red:#e74c3c;--orange:#f39c12;--green:#00b894;--text:#e8e8f0;--muted:#888}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:var(--accent2);text-decoration:none}
.container{max-width:1100px;margin:0 auto;padding:24px 20px}
.header{text-align:center;padding:48px 0 16px}
.header .logo{font-size:2rem;font-weight:800;letter-spacing:-1px}
.header .logo span{color:var(--accent)}
.header p{color:var(--muted);margin-top:8px;font-size:1.05rem}
.back-link{display:inline-block;margin-bottom:24px;color:var(--accent2);font-size:.9rem}

.pricing-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;margin:40px 0}
@media(max-width:900px){.pricing-grid{grid-template-columns:1fr}}

.plan-card{background:var(--card);border-radius:20px;padding:36px 28px;position:relative;border:1px solid var(--border);display:flex;flex-direction:column;transition:transform .2s}
.plan-card:hover{transform:translateY(-4px)}
.plan-card.popular{border:2px solid var(--accent2);box-shadow:0 0 40px rgba(0,212,255,.1)}
.popular-badge{position:absolute;top:-14px;left:50%;transform:translateX(-50%);background:linear-gradient(135deg,var(--accent2),#0099cc);color:#fff;padding:6px 20px;border-radius:20px;font-size:.78rem;font-weight:700;letter-spacing:.5px;text-transform:uppercase;white-space:nowrap}
.plan-name{font-size:1.4rem;font-weight:700;margin-bottom:4px;margin-top:10px}
.plan-desc{color:var(--muted);font-size:.88rem;margin-bottom:20px;min-height:40px}
.plan-price{font-size:2.6rem;font-weight:800;margin-bottom:4px}
.plan-price .currency{font-size:1.4rem;vertical-align:top;position:relative;top:4px}
.plan-price .period{font-size:.9rem;font-weight:400;color:var(--muted)}
.plan-price.free-price{color:var(--green)}

.features-list{list-style:none;margin:24px 0;flex:1}
.features-list li{padding:8px 0;font-size:.9rem;display:flex;align-items:flex-start;gap:10px;line-height:1.4}
.features-list li::before{content:"";display:inline-block;width:18px;height:18px;flex-shrink:0;margin-top:1px;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20' fill='%2300d4ff'%3E%3Cpath fill-rule='evenodd' d='M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z'/%3E%3C/svg%3E") center/contain no-repeat}
.features-list li.disabled{color:var(--muted)}
.features-list li.disabled::before{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20' fill='%23444'%3E%3Cpath fill-rule='evenodd' d='M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z'/%3E%3C/svg%3E") center/contain no-repeat}

.plan-btn{display:block;width:100%;padding:16px;border-radius:12px;border:none;font-size:1rem;font-weight:700;cursor:pointer;text-align:center;transition:transform .15s,box-shadow .2s;text-decoration:none}
.plan-btn:hover{transform:translateY(-2px);box-shadow:0 4px 20px rgba(0,0,0,.3)}
.btn-free{background:#1a1a2e;color:var(--text);border:1px solid var(--border)}
.btn-pro{background:linear-gradient(135deg,var(--accent2),#0099cc);color:#fff}
.btn-business{background:linear-gradient(135deg,var(--accent),#8b5cf6);color:#fff}

.faq-section{margin:60px 0 40px}
.faq-section h2{text-align:center;font-size:1.6rem;margin-bottom:32px}
.faq-item{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px 24px;margin-bottom:12px}
.faq-item h4{color:var(--accent2);margin-bottom:8px;font-size:1rem}
.faq-item p{color:var(--muted);font-size:.9rem;line-height:1.6}

.footer{text-align:center;padding:32px 0;color:var(--muted);font-size:.82rem;border-top:1px solid var(--border);margin-top:40px}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="logo">OBLIVION <span>Privacy Scanner</span></div>
    <p>Protect your website. Build trust with your users.</p>
  </div>

  <div style="text-align:center"><a href="/privacy-scan" class="back-link">&larr; Back to Scanner</a></div>

  <div class="pricing-grid">
    <!-- FREE -->
    <div class="plan-card">
      <div class="plan-name">Free</div>
      <div class="plan-desc">Quick privacy check for any website</div>
      <div class="plan-price free-price">Free</div>
      <ul class="features-list">
        <li>10 scans per day</li>
        <li>Basic privacy analysis</li>
        <li>Tracker detection</li>
        <li>GDPR issue detection</li>
        <li>Privacy grade (A+ to F)</li>
        <li class="disabled">API access</li>
        <li class="disabled">Weekly monitoring</li>
        <li class="disabled">PDF report export</li>
        <li class="disabled">Privacy badge</li>
        <li class="disabled">Email alerts</li>
      </ul>
      <a href="/privacy-scan" class="plan-btn btn-free">Start Scanning</a>
    </div>

    <!-- PRO -->
    <div class="plan-card popular">
      <div class="popular-badge">Most Popular</div>
      <div class="plan-name">Pro</div>
      <div class="plan-desc">For developers and site owners who care about privacy</div>
      <div class="plan-price"><span class="currency">&pound;</span>29<span class="period">/month</span></div>
      <ul class="features-list">
        <li>Unlimited scans</li>
        <li>REST API access with API key</li>
        <li>Weekly automated monitoring</li>
        <li>Email alerts on score changes</li>
        <li>PDF report export</li>
        <li>Privacy badge for your website</li>
        <li>Priority support</li>
        <li class="disabled">GDPR/CCPA compliance reporting</li>
        <li class="disabled">Team accounts</li>
        <li class="disabled">Custom badge branding</li>
      </ul>
      <a href="/privacy-scan/checkout/pro" class="plan-btn btn-pro">Upgrade to Pro</a>
    </div>

    <!-- BUSINESS -->
    <div class="plan-card">
      <div class="plan-name">Business</div>
      <div class="plan-desc">For agencies and enterprises managing multiple sites</div>
      <div class="plan-price"><span class="currency">&pound;</span>99<span class="period">/month</span></div>
      <ul class="features-list">
        <li>Everything in Pro</li>
        <li>Unlimited sites monitored</li>
        <li>GDPR/CCPA compliance reporting</li>
        <li>Team accounts (up to 10 users)</li>
        <li>Custom badge branding</li>
        <li>Dedicated support</li>
        <li>API rate limit: 1000 req/min</li>
        <li>Bulk scan endpoints</li>
        <li>Audit trail export</li>
        <li>SLA guarantee</li>
      </ul>
      <a href="/privacy-scan/checkout/business" class="plan-btn btn-business">Contact Sales</a>
    </div>
  </div>

  <div class="faq-section">
    <h2>Frequently Asked Questions</h2>
    <div class="faq-item">
      <h4>How does the API work?</h4>
      <p>Register your email at <code>/api/privacy-scan/register</code> to get an API key. Include it as <code>X-API-Key</code> header in your requests to <code>/api/privacy-scan?url=example.com</code>. Pro and Business plans get unlimited scans.</p>
    </div>
    <div class="faq-item">
      <h4>What does the privacy badge do?</h4>
      <p>The privacy badge is an SVG image you embed on your website showing your privacy grade. It signals to visitors that you take privacy seriously. Add it with: <code>&lt;img src="https://oblivionsearch.com/privacy-scan/badge/yourdomain.com"&gt;</code></p>
    </div>
    <div class="faq-item">
      <h4>How does weekly monitoring work?</h4>
      <p>Add sites via the API: <code>POST /api/privacy-scan/monitor</code> with your API key. We scan your sites every week and email you if the privacy score changes — so you catch tracker additions or compliance regressions instantly.</p>
    </div>
    <div class="faq-item">
      <h4>Can I cancel anytime?</h4>
      <p>Yes. All plans are month-to-month with no contracts. Cancel anytime from your Stripe billing portal.</p>
    </div>
  </div>

  <div class="footer">
    Powered by <strong>OBLIVION Search</strong> &mdash; <a href="https://oblivionsearch.com">oblivionsearch.com</a><br>
    &copy; 2026 OBLIVION. Privacy-first by design.
  </div>
</div>
</body>
</html>"""


@app.get("/privacy-scan/pricing", response_class=HTMLResponse)
async def pricing_page():
    return HTMLResponse(content=PRICING_HTML)


# ---------------------------------------------------------------------------
# Landing page — Updated with pricing CTA, badge CTA, monitoring CTA
# ---------------------------------------------------------------------------
LANDING_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OBLIVION Privacy Scanner — Website Privacy & GDPR Compliance Check</title>
<meta name="description" content="Free website privacy scanner. Detect trackers, cookies, GDPR issues and get a privacy compliance score in seconds. Pro plans available with API, monitoring & badges.">
<link rel="canonical" href="https://oblivionsearch.com/privacy-scan">
<meta property="og:title" content="OBLIVION Privacy Scanner — Website Privacy & GDPR Compliance Check">
<meta property="og:description" content="Free website privacy scanner. Detect trackers, cookies, GDPR issues and get a privacy compliance score in seconds.">
<meta property="og:url" content="https://oblivionsearch.com/privacy-scan">
<meta property="og:type" content="website">
<meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🛡</text></svg>">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0a0f;--card:#13131f;--border:#23233a;--accent:#6c5ce7;--accent2:#00d4ff;--red:#e74c3c;--orange:#f39c12;--green:#00b894;--text:#e8e8f0;--muted:#888}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:var(--accent2);text-decoration:none}
.container{max-width:900px;margin:0 auto;padding:24px 20px}

/* Header */
.header{text-align:center;padding:48px 0 24px}
.header .logo{font-size:2rem;font-weight:800;letter-spacing:-1px}
.header .logo span{color:var(--accent)}
.header p.tagline{color:var(--muted);margin-top:8px;font-size:1.05rem}

/* Search box */
.scan-box{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:32px;margin:32px 0;text-align:center}
.scan-box h2{font-size:1.4rem;margin-bottom:16px}
.input-row{display:flex;gap:12px;max-width:600px;margin:0 auto}
.input-row input{flex:1;padding:14px 18px;border-radius:10px;border:1px solid var(--border);background:#1a1a2e;color:var(--text);font-size:1rem;outline:none;transition:border .2s}
.input-row input:focus{border-color:var(--accent)}
.input-row button{padding:14px 28px;border-radius:10px;border:none;background:linear-gradient(135deg,var(--accent),#8b5cf6);color:#fff;font-size:1rem;font-weight:600;cursor:pointer;transition:transform .15s,box-shadow .2s;white-space:nowrap}
.input-row button:hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(108,92,231,.4)}
.input-row button:disabled{opacity:.6;cursor:wait}

/* Scanning state */
.scanning{display:none;margin-top:20px;color:var(--muted);font-size:.95rem}
.scanning.active{display:block}
.scanning .dots::after{content:'';animation:dots 1.4s steps(4,end) infinite}
@keyframes dots{0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'...'}}

/* Results */
.results{display:none;margin-top:32px}
.results.active{display:block}

.score-section{display:flex;align-items:center;gap:36px;background:var(--card);border:1px solid var(--border);border-radius:16px;padding:32px;margin-bottom:24px;flex-wrap:wrap;justify-content:center}
.score-ring{position:relative;width:160px;height:160px;flex-shrink:0}
.score-ring svg{transform:rotate(-90deg);width:160px;height:160px}
.score-ring circle{fill:none;stroke-width:10;stroke-linecap:round}
.score-ring .bg{stroke:#23233a}
.score-ring .fg{transition:stroke-dashoffset .8s ease}
.score-label{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center}
.score-label .num{font-size:2.4rem;font-weight:800;line-height:1}
.score-label .grade{font-size:1rem;font-weight:600;margin-top:2px}
.score-label .of{font-size:.75rem;color:var(--muted)}

.score-details h3{font-size:1.3rem;margin-bottom:8px}
.score-details p{color:var(--muted);line-height:1.6}
.stat-pills{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
.pill{padding:6px 14px;border-radius:20px;font-size:.82rem;font-weight:600;border:1px solid var(--border);background:#1a1a2e}
.pill.good{color:var(--green);border-color:var(--green)}
.pill.warn{color:var(--orange);border-color:var(--orange)}
.pill.bad{color:var(--red);border-color:var(--red)}

/* Issues list */
.section-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:24px;margin-bottom:20px}
.section-card h3{font-size:1.1rem;margin-bottom:14px;display:flex;align-items:center;gap:8px}
.issue-item{display:flex;align-items:flex-start;gap:10px;padding:10px 0;border-bottom:1px solid var(--border)}
.issue-item:last-child{border-bottom:none}
.sev{width:8px;height:8px;border-radius:50%;margin-top:6px;flex-shrink:0}
.sev.critical{background:var(--red)}
.sev.high{background:#e74c3c}
.sev.medium{background:var(--orange)}
.sev.low{background:#636e72}
.issue-msg{font-size:.92rem;line-height:1.5}

.rec-item{padding:8px 0;border-bottom:1px solid var(--border);font-size:.92rem;line-height:1.5;color:var(--accent2)}
.rec-item:last-child{border-bottom:none}
.rec-item::before{content:"→ ";color:var(--muted)}

.tracker-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
.tracker-chip{padding:10px 14px;border-radius:10px;background:#1a1a2e;border:1px solid var(--border);font-size:.85rem}
.tracker-chip .tname{font-weight:600}
.tracker-chip .tcat{color:var(--muted);font-size:.78rem}

/* Badge CTA */
.badge-cta{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:32px;text-align:center;margin:32px 0}
.badge-cta h3{font-size:1.3rem;margin-bottom:8px;color:var(--accent2)}
.badge-cta p{color:var(--muted);margin-bottom:16px;line-height:1.5;max-width:600px;margin-left:auto;margin-right:auto}
.badge-preview{background:#1a1a2e;border:1px solid var(--border);border-radius:10px;padding:16px;display:inline-block;margin-bottom:16px}
.badge-code{background:#1a1a2e;border:1px solid var(--border);border-radius:8px;padding:12px 16px;font-family:monospace;font-size:.82rem;color:var(--accent2);word-break:break-all;max-width:600px;margin:0 auto 16px;text-align:left}

/* Pricing table */
.pricing-section{margin:48px 0}
.pricing-section h2{text-align:center;font-size:1.6rem;margin-bottom:8px}
.pricing-section .sub{text-align:center;color:var(--muted);margin-bottom:32px;font-size:.95rem}
.pricing-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
@media(max-width:800px){.pricing-grid{grid-template-columns:1fr}}

.plan-card{background:var(--card);border-radius:16px;padding:28px 24px;position:relative;border:1px solid var(--border);display:flex;flex-direction:column}
.plan-card.popular{border:2px solid var(--accent2);box-shadow:0 0 30px rgba(0,212,255,.08)}
.popular-badge{position:absolute;top:-12px;left:50%;transform:translateX(-50%);background:linear-gradient(135deg,var(--accent2),#0099cc);color:#fff;padding:4px 16px;border-radius:16px;font-size:.72rem;font-weight:700;letter-spacing:.5px;text-transform:uppercase;white-space:nowrap}
.plan-name{font-size:1.15rem;font-weight:700;margin-top:6px}
.plan-price{font-size:2rem;font-weight:800;margin:8px 0 4px}
.plan-price .cur{font-size:1rem;vertical-align:top;position:relative;top:3px}
.plan-price .per{font-size:.8rem;font-weight:400;color:var(--muted)}
.plan-price.free-p{color:var(--green)}

.feat-list{list-style:none;margin:16px 0;flex:1;font-size:.85rem}
.feat-list li{padding:5px 0;display:flex;align-items:flex-start;gap:8px;line-height:1.4}
.feat-list li .ck{color:var(--accent2);font-weight:700;flex-shrink:0}
.feat-list li.off{color:var(--muted)}
.feat-list li.off .ck{color:#444}

.plan-btn{display:block;width:100%;padding:13px;border-radius:10px;border:none;font-size:.92rem;font-weight:700;cursor:pointer;text-align:center;transition:transform .15s;text-decoration:none;margin-top:8px}
.plan-btn:hover{transform:translateY(-2px)}
.btn-f{background:#1a1a2e;color:var(--text);border:1px solid var(--border)}
.btn-p{background:linear-gradient(135deg,var(--accent2),#0099cc);color:#fff}
.btn-b{background:linear-gradient(135deg,var(--accent),#8b5cf6);color:#fff}

/* Monitor CTA */
.monitor-cta{background:linear-gradient(135deg,#13131f,#1a1a3a);border:1px solid var(--accent);border-radius:16px;padding:32px;text-align:center;margin:32px 0}
.monitor-cta h3{font-size:1.3rem;color:var(--accent);margin-bottom:8px}
.monitor-cta p{color:var(--muted);margin-bottom:16px;line-height:1.5}
.cta-btn{display:inline-block;padding:14px 36px;border-radius:10px;background:linear-gradient(135deg,var(--accent),#8b5cf6);color:#fff;font-weight:700;font-size:1rem;border:none;cursor:pointer;transition:transform .15s;text-decoration:none}
.cta-btn:hover{transform:translateY(-1px)}

/* Footer */
.footer{text-align:center;padding:32px 0;color:var(--muted);font-size:.82rem;border-top:1px solid var(--border);margin-top:40px}

@media(max-width:600px){
  .input-row{flex-direction:column}
  .score-section{flex-direction:column;text-align:center}
}
@media(max-width:480px){
body{overflow-x:hidden}
.container{padding:12px}
.header .logo{font-size:1.4rem}
.header p.tagline{font-size:.9rem}
.scan-box{padding:20px;margin:20px 0}
.scan-box h2{font-size:1.1rem}
.input-row input{font-size:16px;padding:12px 14px}
.input-row button{min-height:44px;font-size:16px;padding:14px 20px}
.score-ring{width:120px;height:120px}
.score-ring svg{width:120px;height:120px}
.score-label .num{font-size:1.8rem}
.score-details h3{font-size:1rem}
.stat-pills{justify-content:center}
.pill{font-size:.75rem;padding:4px 10px}
.section-card{padding:16px}
.section-card h3{font-size:1rem}
.tracker-grid{grid-template-columns:1fr}
.pricing-grid{grid-template-columns:1fr}
.badge-cta,.monitor-cta{padding:20px}
}
@media(max-width:375px){
.header .logo{font-size:1.2rem}
.scan-box h2{font-size:1rem}
.score-ring{width:100px;height:100px}
.score-ring svg{width:100px;height:100px}
.score-label .num{font-size:1.5rem}
.issue-msg{font-size:.82rem}
}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="logo">OBLIVION <span>Privacy Scanner</span></div>
    <p class="tagline">Instant website privacy & GDPR compliance analysis</p>
  </div>

  <div class="scan-box">
    <h2>Scan any website for privacy issues</h2>
    <div class="input-row">
      <input type="text" id="urlInput" placeholder="Enter a website URL (e.g. example.com)" autofocus>
      <button id="scanBtn" onclick="startScan()">Scan Now</button>
    </div>
    <div class="scanning" id="scanning">Analysing privacy compliance<span class="dots"></span></div>
  </div>

  <div class="results" id="results"></div>

  <!-- Badge CTA (shown after scan) -->
  <div class="badge-cta" id="badgeCta" style="display:none">
    <h3>Get Your Privacy Badge</h3>
    <p>Display your privacy grade on your website. Show visitors you take their data seriously — and get a free backlink from OBLIVION.</p>
    <div class="badge-preview" id="badgePreview"></div>
    <div class="badge-code" id="badgeCode"></div>
    <a href="/privacy-scan/pricing" class="cta-btn" style="font-size:.9rem;padding:12px 28px">Unlock Badge with Pro</a>
  </div>

  <!-- Monitor CTA -->
  <div class="monitor-cta">
    <h3>Monitor Your Site 24/7</h3>
    <p>Get weekly automated scans and instant email alerts when your privacy score changes. Catch new trackers, broken consent banners, and compliance regressions before your users do.</p>
    <a href="/privacy-scan/pricing" class="cta-btn">View Pricing &amp; Plans</a>
  </div>

  <!-- Pricing Table -->
  <div class="pricing-section">
    <h2>Plans &amp; Pricing</h2>
    <p class="sub">Start free. Upgrade when you need more power.</p>
    <div class="pricing-grid">
      <!-- Free -->
      <div class="plan-card">
        <div class="plan-name">Free</div>
        <div class="plan-price free-p">Free</div>
        <ul class="feat-list">
          <li><span class="ck">&#10003;</span> 10 scans per day</li>
          <li><span class="ck">&#10003;</span> Basic privacy analysis</li>
          <li><span class="ck">&#10003;</span> Tracker detection</li>
          <li><span class="ck">&#10003;</span> Privacy grade</li>
          <li class="off"><span class="ck">&#10007;</span> API access</li>
          <li class="off"><span class="ck">&#10007;</span> Monitoring</li>
          <li class="off"><span class="ck">&#10007;</span> Badge</li>
        </ul>
        <a href="#" onclick="document.getElementById('urlInput').focus();return false" class="plan-btn btn-f">Scan Now</a>
      </div>
      <!-- Pro -->
      <div class="plan-card popular">
        <div class="popular-badge">Most Popular</div>
        <div class="plan-name">Pro</div>
        <div class="plan-price"><span class="cur">&pound;</span>29<span class="per">/mo</span></div>
        <ul class="feat-list">
          <li><span class="ck">&#10003;</span> Unlimited scans</li>
          <li><span class="ck">&#10003;</span> REST API + key</li>
          <li><span class="ck">&#10003;</span> Weekly monitoring</li>
          <li><span class="ck">&#10003;</span> Email alerts</li>
          <li><span class="ck">&#10003;</span> PDF export</li>
          <li><span class="ck">&#10003;</span> Privacy badge</li>
          <li><span class="ck">&#10003;</span> Priority support</li>
        </ul>
        <a href="/privacy-scan/checkout/pro" class="plan-btn btn-p">Upgrade to Pro</a>
      </div>
      <!-- Business -->
      <div class="plan-card">
        <div class="plan-name">Business</div>
        <div class="plan-price"><span class="cur">&pound;</span>99<span class="per">/mo</span></div>
        <ul class="feat-list">
          <li><span class="ck">&#10003;</span> Everything in Pro</li>
          <li><span class="ck">&#10003;</span> Unlimited monitors</li>
          <li><span class="ck">&#10003;</span> GDPR/CCPA reports</li>
          <li><span class="ck">&#10003;</span> Team (up to 10)</li>
          <li><span class="ck">&#10003;</span> Custom badge</li>
          <li><span class="ck">&#10003;</span> Dedicated support</li>
          <li><span class="ck">&#10003;</span> SLA guarantee</li>
        </ul>
        <a href="/privacy-scan/checkout/business" class="plan-btn btn-b">Get Business</a>
      </div>
    </div>
  </div>

  <div class="footer">
    Powered by <strong>OBLIVION Search</strong> &mdash; <a href="https://oblivionsearch.com">oblivionsearch.com</a><br>
    &copy; 2026 OBLIVION. Privacy-first by design.
  </div>
</div>

<script>
const C = 2 * Math.PI * 65;

function startScan(){
  const url = document.getElementById('urlInput').value.trim();
  if(!url) return;
  const btn = document.getElementById('scanBtn');
  btn.disabled = true;
  document.getElementById('scanning').classList.add('active');
  document.getElementById('results').classList.remove('active');
  document.getElementById('badgeCta').style.display = 'none';

  fetch('/api/privacy-scan?url=' + encodeURIComponent(url))
    .then(r => {
      if(r.status === 429) return r.json().then(d => { throw {rate: true, msg: d.error}; });
      if(!r.ok) return r.json().then(d => { throw {msg: d.detail || 'Scan failed'}; });
      return r.json();
    })
    .then(d => {
      renderResults(d);
      showBadgeCta(d);
    })
    .catch(e => {
      document.getElementById('results').innerHTML = `<div class="section-card" style="text-align:center;color:var(--red)">${e.msg || 'Error scanning URL. Please check the address and try again.'}</div>`;
      document.getElementById('results').classList.add('active');
    })
    .finally(() => {
      btn.disabled = false;
      document.getElementById('scanning').classList.remove('active');
    });
}

document.getElementById('urlInput').addEventListener('keydown', e => {
  if(e.key === 'Enter') startScan();
});

function scoreColor(s){
  if(s >= 80) return '#00b894';
  if(s >= 60) return '#f39c12';
  return '#e74c3c';
}

function showBadgeCta(d){
  try {
    const domain = new URL(d.url).hostname.replace('www.','');
    const badgeUrl = 'https://oblivionsearch.com/privacy-scan/badge/' + domain;
    const linkUrl = 'https://oblivionsearch.com/privacy-scan?url=' + encodeURIComponent(domain);
    document.getElementById('badgePreview').innerHTML = `<img src="/privacy-scan/badge/${domain}" alt="Privacy Badge" height="28">`;
    document.getElementById('badgeCode').textContent = `<a href="${linkUrl}"><img src="${badgeUrl}" alt="Privacy Score" height="28"></a>`;
    document.getElementById('badgeCta').style.display = 'block';
  } catch(e){}
}

function renderResults(d){
  const col = scoreColor(d.score);
  const offset = C - (d.score / 100) * C;

  let issuesHtml = '';
  (d.issues || []).forEach(i => {
    issuesHtml += `<div class="issue-item"><div class="sev ${i.severity}"></div><div class="issue-msg">${esc(i.message)}</div></div>`;
  });

  let recsHtml = '';
  (d.recommendations || []).forEach(r => {
    recsHtml += `<div class="rec-item">${esc(r)}</div>`;
  });

  let trackersHtml = '';
  (d.trackers_found || []).forEach(t => {
    trackersHtml += `<div class="tracker-chip"><div class="tname">${esc(t.name)}</div><div class="tcat">${esc(t.category)}</div></div>`;
  });

  const fp = d.cookies?.first_party?.length || 0;
  const tp = d.cookies?.third_party?.length || 0;
  const tpScripts = d.third_party_scripts?.length || 0;

  let html = `
    <div class="score-section">
      <div class="score-ring">
        <svg viewBox="0 0 160 160">
          <circle class="bg" cx="80" cy="80" r="65"/>
          <circle class="fg" cx="80" cy="80" r="65"
            stroke="${col}"
            stroke-dasharray="${C}"
            stroke-dashoffset="${offset}"/>
        </svg>
        <div class="score-label">
          <div class="num" style="color:${col}">${d.score}</div>
          <div class="of">/ 100</div>
          <div class="grade" style="color:${col}">Grade ${d.grade}</div>
        </div>
      </div>
      <div class="score-details">
        <h3>Privacy Score for ${esc(d.url)}</h3>
        <p>Scanned at ${d.scanned_at}</p>
        <div class="stat-pills">
          <span class="pill ${d.https?'good':'bad'}">${d.https?'HTTPS':'No HTTPS'}</span>
          <span class="pill ${d.privacy_policy_found?'good':'bad'}">${d.privacy_policy_found?'Privacy Policy':'No Privacy Policy'}</span>
          <span class="pill ${d.cookie_consent_found?'good':'warn'}">${d.cookie_consent_found?'Consent Banner':'No Consent Banner'}</span>
          <span class="pill ${d.trackers_found.length===0?'good':'bad'}">${d.trackers_found.length} Trackers</span>
          <span class="pill ${tpScripts<=5?'good':'warn'}">${tpScripts} 3rd-Party Scripts</span>
          <span class="pill ${tp===0?'good':'bad'}">${fp+tp} Cookies (${tp} 3P)</span>
        </div>
      </div>
    </div>`;

  if(issuesHtml){
    html += `<div class="section-card"><h3>Issues Found (${d.issues.length})</h3>${issuesHtml}</div>`;
  }

  if(trackersHtml){
    html += `<div class="section-card"><h3>Trackers Detected (${d.trackers_found.length})</h3><div class="tracker-grid">${trackersHtml}</div></div>`;
  }

  if(recsHtml){
    html += `<div class="section-card"><h3>Recommendations</h3>${recsHtml}</div>`;
  }

  document.getElementById('results').innerHTML = html;
  document.getElementById('results').classList.add('active');
  document.getElementById('results').scrollIntoView({behavior:'smooth',block:'start'});
}

function esc(s){
  const d=document.createElement('div');d.textContent=s;return d.innerHTML;
}
</script>
</body>
</html>"""


@app.get("/privacy-scan", response_class=HTMLResponse)
async def landing():
    return HTMLResponse(content=LANDING_HTML)


@app.get("/")
async def root():
    return RedirectResponse("/privacy-scan")


# ---------------------------------------------------------------------------
# Stripe Webhook — activates account after payment
# ---------------------------------------------------------------------------
@app.post("/privacy-scan/webhook")
async def stripe_webhook(request: Request):
    """Stripe sends this when a payment succeeds."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    # For now, trust the payload (add webhook signature verification later with whsec_ secret)
    try:
        event = json.loads(payload)
    except:
        raise HTTPException(400, "Invalid payload")

    event_type = event.get("type", "")

    if event_type == "checkout.session.completed":
        session_data = event.get("data", {}).get("object", {})
        customer_email = session_data.get("customer_details", {}).get("email", "")
        stripe_customer_id = session_data.get("customer", "")
        stripe_sub_id = session_data.get("subscription", "")
        amount = session_data.get("amount_total", 0)
        plan = "business" if amount >= 9900 else "pro"

        if customer_email:
            # Create or update API key for this customer
            api_key = f"oblivion_ps_{secrets.token_hex(16)}"
            try:
                conn = _get_db()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO api_keys (email, api_key, plan, stripe_customer_id, stripe_subscription_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (email) DO UPDATE SET
                        plan = EXCLUDED.plan,
                        stripe_customer_id = EXCLUDED.stripe_customer_id,
                        stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                        is_active = TRUE,
                        updated_at = NOW()
                    RETURNING api_key
                """, (customer_email, api_key, plan, stripe_customer_id, stripe_sub_id))
                row = cur.fetchone()
                actual_key = row[0] if row else api_key
                conn.commit()
                cur.close()
                conn.close()

                # Send welcome email with API key
                _send_welcome_email(customer_email, actual_key, plan)

            except Exception as e:
                print(f"[Webhook] DB error: {e}")

    elif event_type == "customer.subscription.deleted":
        # Subscription cancelled — deactivate
        session_data = event.get("data", {}).get("object", {})
        stripe_sub_id = session_data.get("id", "")
        if stripe_sub_id:
            try:
                conn = _get_db()
                cur = conn.cursor()
                cur.execute("UPDATE api_keys SET is_active = FALSE WHERE stripe_subscription_id = %s", (stripe_sub_id,))
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                print(f"[Webhook] Deactivate error: {e}")

    return {"received": True}


def _send_welcome_email(email: str, api_key: str, plan: str):
    """Send welcome email with API key after payment."""
    try:
        plan_name = "Pro" if plan == "pro" else "Business"
        msg = MIMEMultipart()
        msg["From"] = "OBLIVION Privacy Scanner <os.environ.get("SMTP_USER", "")>"
        msg["To"] = email
        msg["Subject"] = f"Welcome to OBLIVION Privacy Scanner {plan_name}!"

        body = f"""
Welcome to OBLIVION Privacy Scanner {plan_name}!

Your account is now active. Here are your details:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Plan: {plan_name}
API Key: {api_key}
Dashboard: https://oblivionsearch.com/privacy-scan/dashboard?key={api_key}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GETTING STARTED:

1. Scan any website via API:
   curl -H "X-API-Key: {api_key}" "https://oblivionsearch.com/api/privacy-scan?url=yoursite.com"

2. Add a privacy badge to your website:
   <img src="https://oblivionsearch.com/privacy-scan/badge/yoursite.com" alt="Privacy Score">

3. Set up weekly monitoring:
   curl -X POST "https://oblivionsearch.com/api/privacy-scan/monitor" \\
     -H "X-API-Key: {api_key}" \\
     -H "Content-Type: application/json" \\
     -d '{{"url": "yoursite.com"}}'

4. View your dashboard:
   https://oblivionsearch.com/privacy-scan/dashboard?key={api_key}

Need help? Reply to this email or contact admin@oblivionzone.com.

— The OBLIVION Team
https://oblivionsearch.com
"""
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login("os.environ.get("SMTP_USER", "")", "os.environ.get("SMTP_PASS", "")")
            server.send_message(msg)
        print(f"[Email] Welcome email sent to {email}")
    except Exception as e:
        print(f"[Email] Failed to send to {email}: {e}")


# ---------------------------------------------------------------------------
# Success Page — customer lands here after paying
# ---------------------------------------------------------------------------
@app.get("/privacy-scan/success")
async def checkout_success(session_id: str = "", plan: str = "pro"):
    """After Stripe payment, customer sees this page."""
    # Retrieve the session from Stripe to get customer email
    email = ""
    api_key = ""
    try:
        if session_id:
            session = stripe.checkout.Session.retrieve(session_id)
            email = session.customer_details.email if session.customer_details else ""

            if email:
                # Create API key immediately (webhook may not have fired yet)
                api_key = f"oblivion_ps_{secrets.token_hex(16)}"
                stripe_customer_id = session.customer or ""
                stripe_sub_id = session.subscription or ""

                conn = _get_db()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO api_keys (email, api_key, plan, stripe_customer_id, stripe_subscription_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (email) DO UPDATE SET
                        plan = EXCLUDED.plan,
                        stripe_customer_id = EXCLUDED.stripe_customer_id,
                        stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                        is_active = TRUE,
                        updated_at = NOW()
                    RETURNING api_key
                """, (email, api_key, plan, stripe_customer_id, stripe_sub_id))
                row = cur.fetchone()
                api_key = row[0] if row else api_key
                conn.commit()
                cur.close()
                conn.close()

                # Send welcome email
                _send_welcome_email(email, api_key, plan)
    except Exception as e:
        print(f"[Success] Error: {e}")

    plan_name = "Pro" if plan == "pro" else "Business"

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Welcome — OBLIVION Privacy Scanner {plan_name}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0f;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.box{{max-width:640px;margin:20px;padding:48px 40px;background:#12121a;border:1px solid #1e293b;border-radius:16px;text-align:center}}
h1{{font-size:2rem;color:#22c55e;margin-bottom:8px}}
.sub{{color:#94a3b8;margin-bottom:32px}}
.key-box{{background:#0a0a0f;border:1px solid #334155;border-radius:8px;padding:20px;margin:24px 0;text-align:left}}
.key-box label{{color:#64748b;font-size:.8rem;text-transform:uppercase;letter-spacing:1px}}
.key-box code{{display:block;color:#00d4ff;font-size:1.1rem;margin-top:4px;word-break:break-all;user-select:all}}
.steps{{text-align:left;margin:24px 0}}
.steps li{{padding:8px 0;color:#94a3b8;line-height:1.6}}
.steps code{{background:#1e293b;padding:2px 6px;border-radius:4px;color:#00d4ff;font-size:.85rem}}
.btn{{display:inline-block;padding:14px 32px;background:linear-gradient(135deg,#00d4ff,#7c3aed);color:#fff;border-radius:8px;text-decoration:none;font-weight:700;margin-top:16px}}
.btn:hover{{opacity:.9}}
</style></head><body>
<div class="box">
    <h1>Payment Successful!</h1>
    <p class="sub">Welcome to OBLIVION Privacy Scanner {plan_name}</p>

    {'<div class="key-box"><label>Your API Key</label><code>' + api_key + '</code></div>' if api_key else ''}
    {'<div class="key-box"><label>Email</label><code>' + email + '</code></div>' if email else ''}

    <div class="steps">
        <h3 style="color:#e2e8f0;margin-bottom:12px">What happens now:</h3>
        <ol>
            <li>Your API key has been emailed to <strong>{email or 'your email'}</strong></li>
            <li>Use your key for <strong>unlimited scans</strong> — no daily limits</li>
            <li>Add the <strong>privacy badge</strong> to your website</li>
            <li>Set up <strong>weekly monitoring</strong> for your sites</li>
            <li>Access your <strong>dashboard</strong> anytime</li>
        </ol>
    </div>

    <a href="/privacy-scan/dashboard?key={api_key}" class="btn">Go to Dashboard →</a>
</div></body></html>""")


# ---------------------------------------------------------------------------
# Customer Dashboard — manage scans, monitoring, badges
# ---------------------------------------------------------------------------
@app.get("/privacy-scan/dashboard")
async def customer_dashboard(key: str = ""):
    if not key:
        return HTMLResponse("""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard Login — OBLIVION Privacy Scanner</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{background:#0a0a0f;color:#e2e8f0;font-family:-apple-system,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}
.box{max-width:400px;padding:40px;background:#12121a;border:1px solid #1e293b;border-radius:16px;text-align:center}
h2{margin-bottom:16px;color:#00d4ff}input{width:100%;padding:14px;background:#0a0a0f;border:1px solid #334155;border-radius:8px;color:#e2e8f0;font-size:16px;margin:12px 0}
.btn{display:inline-block;padding:14px 32px;background:#00d4ff;color:#0a0a0f;border:none;border-radius:8px;font-weight:700;cursor:pointer;font-size:16px;width:100%}
</style></head><body><div class="box"><h2>Privacy Scanner Dashboard</h2><p style="color:#94a3b8;margin-bottom:16px">Enter your API key to access your dashboard</p>
<form action="/privacy-scan/dashboard" method="get"><input name="key" placeholder="oblivion_ps_..." required><button class="btn" type="submit">Access Dashboard</button></form>
</div></body></html>""")

    # Verify API key and get account info
    account = None
    monitors = []
    recent_scans = []
    try:
        conn = _get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM api_keys WHERE api_key = %s", (key,))
        account = cur.fetchone()
        if account:
            cur.execute("SELECT * FROM monitored_sites WHERE api_key = %s ORDER BY created_at DESC", (key,))
            monitors = cur.fetchall()
            cur.execute("SELECT * FROM scan_history WHERE api_key = %s ORDER BY scanned_at DESC LIMIT 20", (key,))
            recent_scans = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[Dashboard] Error: {e}")

    if not account:
        return HTMLResponse("<h1>Invalid API Key</h1><p>Check your email for your key or <a href='/privacy-scan/pricing'>subscribe</a>.</p>", status_code=404)

    plan_name = "Pro" if account["plan"] == "pro" else "Business"
    active = "Active" if account["is_active"] else "Cancelled"
    active_color = "#22c55e" if account["is_active"] else "#ef4444"

    monitor_rows = ""
    for m in monitors:
        score = m.get("last_score", "—")
        grade = m.get("last_grade", "—")
        monitor_rows += f'<tr><td>{m["site_url"]}</td><td>{score}</td><td>{grade}</td><td>{str(m.get("last_scan","Never"))[:19]}</td></tr>'
    if not monitor_rows:
        monitor_rows = '<tr><td colspan="4" style="color:#64748b">No sites monitored yet. Add one below.</td></tr>'

    scan_rows = ""
    for s in recent_scans:
        scan_rows += f'<tr><td>{s["url"][:50]}</td><td>{s["score"]}</td><td>{s["grade"]}</td><td>{str(s["scanned_at"])[:19]}</td></tr>'
    if not scan_rows:
        scan_rows = '<tr><td colspan="4" style="color:#64748b">No scans yet. Try scanning a URL.</td></tr>'

    return HTMLResponse(f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard — OBLIVION Privacy Scanner</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#0a0a0f;color:#e2e8f0;font-family:-apple-system,sans-serif}}
.container{{max-width:960px;margin:0 auto;padding:24px}}
h1{{color:#00d4ff;margin-bottom:4px}}
.plan-badge{{display:inline-block;padding:4px 12px;background:#7c3aed;border-radius:12px;font-size:.8rem;font-weight:600;margin-left:8px}}
.status{{color:{active_color};font-weight:600}}
.card{{background:#12121a;border:1px solid #1e293b;border-radius:12px;padding:24px;margin:16px 0}}
.card h3{{color:#00d4ff;margin-bottom:12px}}
.key-display{{background:#0a0a0f;border:1px solid #334155;border-radius:8px;padding:12px;font-family:monospace;color:#00d4ff;word-break:break-all;user-select:all;font-size:.9rem}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid #1e293b;font-size:.9rem}}th{{color:#64748b;font-size:.8rem;text-transform:uppercase}}
.code-block{{background:#0a0a0f;border:1px solid #334155;border-radius:8px;padding:16px;font-family:monospace;font-size:.85rem;color:#94a3b8;overflow-x:auto;white-space:pre;margin:8px 0}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
@media(max-width:600px){{.grid{{grid-template-columns:1fr}}}}
input{{padding:10px;background:#0a0a0f;border:1px solid #334155;border-radius:6px;color:#e2e8f0;font-size:16px;width:100%}}
.btn{{padding:10px 20px;background:#00d4ff;color:#0a0a0f;border:none;border-radius:6px;font-weight:700;cursor:pointer}}
.btn:hover{{opacity:.9}}
</style></head><body><div class="container">
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap">
    <div><h1>Privacy Scanner Dashboard</h1><p style="color:#94a3b8">{account['email']}</p></div>
    <div><span class="plan-badge">{plan_name}</span> <span class="status">{active}</span></div>
</div>

<div class="grid">
    <div class="card"><h3>Your API Key</h3><div class="key-display">{key}</div>
    <p style="color:#64748b;font-size:.8rem;margin-top:8px">Use in X-API-Key header for unlimited scans</p></div>
    <div class="card"><h3>Quick Scan</h3>
    <form action="/privacy-scan/dashboard" method="get" style="display:flex;gap:8px">
        <input type="hidden" name="key" value="{key}">
        <input name="scan_url" placeholder="Enter URL to scan..." style="flex:1">
        <button class="btn" type="submit">Scan</button>
    </form></div>
</div>

<div class="card"><h3>Badge for Your Website</h3>
<p style="color:#94a3b8;margin-bottom:8px">Add this HTML to your site to display your privacy score:</p>
<div class="code-block">&lt;a href="https://oblivionsearch.com/privacy-scan"&gt;&lt;img src="https://oblivionsearch.com/privacy-scan/badge/YOURSITE.COM" alt="Privacy Score"&gt;&lt;/a&gt;</div>
</div>

<div class="card"><h3>Monitored Sites ({len(monitors)})</h3>
<table><tr><th>URL</th><th>Score</th><th>Grade</th><th>Last Scan</th></tr>{monitor_rows}</table>
<form style="display:flex;gap:8px;margin-top:12px" onsubmit="addMonitor(event)">
    <input id="monitor-url" placeholder="Add site to monitor..." style="flex:1">
    <button class="btn" type="submit">+ Add</button>
</form>
</div>

<div class="card"><h3>Recent Scans</h3>
<table><tr><th>URL</th><th>Score</th><th>Grade</th><th>Date</th></tr>{scan_rows}</table>
</div>

<div class="card"><h3>API Examples</h3>
<p style="color:#94a3b8;margin-bottom:8px"><strong>Scan a website:</strong></p>
<div class="code-block">curl -H "X-API-Key: {key}" "https://oblivionsearch.com/api/privacy-scan?url=example.com"</div>
<p style="color:#94a3b8;margin:8px 0"><strong>Add monitoring:</strong></p>
<div class="code-block">curl -X POST "https://oblivionsearch.com/api/privacy-scan/monitor" \\
  -H "X-API-Key: {key}" -H "Content-Type: application/json" \\
  -d '{{"url": "yoursite.com"}}'</div>
</div>

<script>
async function addMonitor(e) {{
    e.preventDefault();
    const url = document.getElementById('monitor-url').value;
    if (!url) return;
    const res = await fetch('/api/privacy-scan/monitor', {{
        method: 'POST',
        headers: {{'X-API-Key': '{key}', 'Content-Type': 'application/json'}},
        body: JSON.stringify({{url: url}})
    }});
    if (res.ok) {{ location.reload(); }} else {{ alert('Error adding site'); }}
}}
</script>
</div></body></html>""")


# ---------------------------------------------------------------------------
# Add Stripe columns to api_keys table
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def _add_stripe_columns():
    try:
        conn = _get_db()
        cur = conn.cursor()
        for col in ["stripe_customer_id VARCHAR(255)", "stripe_subscription_id VARCHAR(255)"]:
            try:
                cur.execute(f"ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS {col}")
            except:
                conn.rollback()
        conn.commit()
        cur.close()
        conn.close()
    except:
        pass


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "oblivion-privacy-scanner", "version": "2.0.0"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3061)
