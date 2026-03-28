#!/usr/bin/env python3
"""
OBLIVION WebTech Profiler — BuiltWith-style Technology Detection
Port 3060 | /webtech routes
Detects 60+ web technologies via HTTP headers, HTML patterns, script URLs, CSS classes.
"""

import os, json, time, hashlib, re, asyncio
from datetime import datetime, timedelta
from urllib.parse import urlparse
from typing import Optional
from collections import defaultdict

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import psycopg2
import psycopg2.extras

# ─── Config ───────────────────────────────────────────────────────────────────
PORT = 3060
DB_DSN = "host=127.0.0.1 dbname=oblivionzone user=postgres password=os.environ.get("DB_PASSWORD", "change_me")"
FREE_LIMIT = 5  # scans per day per IP
USER_AGENT = "OBLIVION-WebTech/1.0 (+https://oblivionzone.com/webtech)"
SCAN_TIMEOUT = 15

app = FastAPI(title="OBLIVION WebTech Profiler", docs_url=None, redoc_url=None)

# ─── Technology Signatures (60+) ─────────────────────────────────────────────
SIGNATURES = {
    # === CMS ===
    "WordPress": {
        "category": "CMS",
        "icon": "W",
        "color": "#21759b",
        "html": ["wp-content", "wp-includes", "wp-json", "/wp-admin"],
        "headers": {},
        "meta": {"generator": "wordpress"},
    },
    "Drupal": {
        "category": "CMS",
        "icon": "D",
        "color": "#0678be",
        "html": ["drupal.js", "Drupal.settings", "/sites/default/files"],
        "headers": {"x-generator": "drupal"},
        "meta": {"generator": "drupal"},
    },
    "Joomla": {
        "category": "CMS",
        "icon": "J",
        "color": "#5091cd",
        "html": ["/media/jui/", "Joomla!", "/administrator/"],
        "headers": {},
        "meta": {"generator": "joomla"},
    },
    "Shopify": {
        "category": "CMS / E-commerce",
        "icon": "S",
        "color": "#96bf48",
        "html": ["cdn.shopify.com", "Shopify.theme", "shopify-section"],
        "headers": {"x-shopify-stage": ""},
        "meta": {},
    },
    "Wix": {
        "category": "CMS",
        "icon": "Wx",
        "color": "#0c6efc",
        "html": ["wix.com", "wixstatic.com", "X-Wix-"],
        "headers": {"x-wix-request-id": ""},
        "meta": {"generator": "wix"},
    },
    "Squarespace": {
        "category": "CMS",
        "icon": "Sq",
        "color": "#222222",
        "html": ["squarespace.com", "squarespace-cdn", "sqs-block"],
        "headers": {},
        "meta": {"generator": "squarespace"},
    },
    "Ghost": {
        "category": "CMS",
        "icon": "Gh",
        "color": "#738a94",
        "html": ["ghost-", "ghost.js"],
        "headers": {"x-ghost-cache-status": ""},
        "meta": {"generator": "ghost"},
    },
    "Webflow": {
        "category": "CMS",
        "icon": "Wf",
        "color": "#4353ff",
        "html": ["webflow.com", "w-nav", "w-container", "wf-page"],
        "headers": {},
        "meta": {"generator": "webflow"},
    },
    "Hugo": {
        "category": "CMS",
        "icon": "Hu",
        "color": "#ff4088",
        "html": [],
        "headers": {},
        "meta": {"generator": "hugo"},
    },
    "Gatsby": {
        "category": "CMS / Framework",
        "icon": "Ga",
        "color": "#663399",
        "html": ["gatsby-", "___gatsby", "gatsby-image"],
        "headers": {"x-powered-by": "gatsby"},
        "meta": {"generator": "gatsby"},
    },

    # === JavaScript Frameworks ===
    "React": {
        "category": "JavaScript Framework",
        "icon": "Re",
        "color": "#61dafb",
        "html": ["_reactRootContainer", "react-root", "data-reactroot", "__REACT_DEVTOOLS"],
        "headers": {},
        "meta": {},
    },
    "Next.js": {
        "category": "JavaScript Framework",
        "icon": "Nx",
        "color": "#000000",
        "html": ["__NEXT_DATA__", "_next/static", "next/dist"],
        "headers": {"x-powered-by": "next.js"},
        "meta": {},
    },
    "Vue.js": {
        "category": "JavaScript Framework",
        "icon": "Vu",
        "color": "#42b883",
        "html": ["vue.js", "vue.min.js", "vue-router", "data-v-", "__vue__", "vue.runtime"],
        "headers": {},
        "meta": {},
    },
    "Nuxt.js": {
        "category": "JavaScript Framework",
        "icon": "Nu",
        "color": "#00dc82",
        "html": ["__NUXT__", "_nuxt/", "nuxt.js"],
        "headers": {},
        "meta": {},
    },
    "Angular": {
        "category": "JavaScript Framework",
        "icon": "Ng",
        "color": "#dd0031",
        "html": ["ng-version", "ng-app", "angular.js", "angular.min.js", "ng-controller"],
        "headers": {},
        "meta": {},
    },
    "Svelte": {
        "category": "JavaScript Framework",
        "icon": "Sv",
        "color": "#ff3e00",
        "html": ["svelte-", "__svelte", "svelte.dev"],
        "headers": {},
        "meta": {},
    },
    "jQuery": {
        "category": "JavaScript Library",
        "icon": "jQ",
        "color": "#0769ad",
        "html": ["jquery.min.js", "jquery.js", "jquery-", "ajax.googleapis.com/ajax/libs/jquery"],
        "headers": {},
        "meta": {},
    },
    "Bootstrap": {
        "category": "CSS Framework",
        "icon": "Bs",
        "color": "#7952b3",
        "html": ["bootstrap.min.css", "bootstrap.min.js", "bootstrap.css", "class=\"container"],
        "headers": {},
        "meta": {},
    },
    "Tailwind CSS": {
        "category": "CSS Framework",
        "icon": "Tw",
        "color": "#38bdf8",
        "html": ["tailwindcss", "tailwind.min.css"],
        "headers": {},
        "meta": {},
        "css_classes": ["flex", "items-center", "justify-between", "bg-white", "px-4", "py-2", "rounded-lg"],
    },
    "TypeScript": {
        "category": "Language",
        "icon": "TS",
        "color": "#3178c6",
        "html": [".ts", "typescript"],
        "headers": {},
        "meta": {},
    },

    # === Server / Infrastructure ===
    "Nginx": {
        "category": "Web Server",
        "icon": "Nx",
        "color": "#009639",
        "html": [],
        "headers": {"server": "nginx"},
        "meta": {},
    },
    "Apache": {
        "category": "Web Server",
        "icon": "Ap",
        "color": "#d22128",
        "html": [],
        "headers": {"server": "apache"},
        "meta": {},
    },
    "Cloudflare": {
        "category": "CDN / Security",
        "icon": "CF",
        "color": "#f38020",
        "html": [],
        "headers": {"cf-ray": "", "cf-cache-status": "", "server": "cloudflare"},
        "meta": {},
    },
    "AWS": {
        "category": "Cloud Hosting",
        "icon": "AW",
        "color": "#ff9900",
        "html": ["amazonaws.com", "aws-sdk"],
        "headers": {"x-amz-cf-id": "", "x-amz-request-id": "", "server": "amazons3"},
        "meta": {},
    },
    "Google Cloud": {
        "category": "Cloud Hosting",
        "icon": "GC",
        "color": "#4285f4",
        "html": ["googleapis.com/storage", "storage.googleapis.com"],
        "headers": {"x-goog-": "", "server": "gse"},
        "meta": {},
    },
    "Vercel": {
        "category": "Hosting",
        "icon": "Vc",
        "color": "#000000",
        "html": [],
        "headers": {"x-vercel-id": "", "server": "vercel"},
        "meta": {},
    },
    "Netlify": {
        "category": "Hosting",
        "icon": "Nt",
        "color": "#00c7b7",
        "html": [],
        "headers": {"x-nf-request-id": "", "server": "netlify"},
        "meta": {},
    },
    "Heroku": {
        "category": "Hosting",
        "icon": "He",
        "color": "#430098",
        "html": [],
        "headers": {"via": "heroku"},
        "meta": {},
    },
    "PHP": {
        "category": "Language",
        "icon": "PH",
        "color": "#777bb4",
        "html": [".php"],
        "headers": {"x-powered-by": "php"},
        "meta": {},
    },
    "ASP.NET": {
        "category": "Framework",
        "icon": "AS",
        "color": "#512bd4",
        "html": ["__VIEWSTATE", "__EVENTVALIDATION", "aspnetcdn.com"],
        "headers": {"x-powered-by": "asp.net", "x-aspnet-version": ""},
        "meta": {},
    },
    "Express": {
        "category": "Framework",
        "icon": "Ex",
        "color": "#000000",
        "html": [],
        "headers": {"x-powered-by": "express"},
        "meta": {},
    },
    "Ruby on Rails": {
        "category": "Framework",
        "icon": "Rb",
        "color": "#cc0000",
        "html": ["csrf-token", "turbolinks", "action_cable"],
        "headers": {"x-powered-by": "phusion passenger"},
        "meta": {},
    },
    "Laravel": {
        "category": "Framework",
        "icon": "Lv",
        "color": "#ff2d20",
        "html": ["laravel", "csrf-token"],
        "headers": {},
        "meta": {},
        "cookies": ["laravel_session", "XSRF-TOKEN"],
    },
    "Django": {
        "category": "Framework",
        "icon": "Dj",
        "color": "#092e20",
        "html": ["csrfmiddlewaretoken", "django"],
        "headers": {},
        "meta": {},
        "cookies": ["csrftoken", "sessionid"],
    },

    # === Analytics ===
    "Google Analytics": {
        "category": "Analytics",
        "icon": "GA",
        "color": "#e37400",
        "html": ["google-analytics.com", "gtag/js", "ga.js", "analytics.js", "googletagmanager.com/gtag"],
        "headers": {},
        "meta": {},
    },
    "Google Tag Manager": {
        "category": "Tag Manager",
        "icon": "GT",
        "color": "#246fdb",
        "html": ["googletagmanager.com/gtm.js", "GTM-", "google_tag_manager"],
        "headers": {},
        "meta": {},
    },
    "Plausible": {
        "category": "Analytics",
        "icon": "Pl",
        "color": "#5850ec",
        "html": ["plausible.io/js", "plausible.js"],
        "headers": {},
        "meta": {},
    },
    "Matomo": {
        "category": "Analytics",
        "icon": "Ma",
        "color": "#3152a0",
        "html": ["matomo.js", "matomo.php", "piwik.js"],
        "headers": {},
        "meta": {},
    },
    "Hotjar": {
        "category": "Analytics",
        "icon": "Hj",
        "color": "#fd3a5c",
        "html": ["hotjar.com", "static.hotjar.com", "_hjSettings"],
        "headers": {},
        "meta": {},
    },
    "Mixpanel": {
        "category": "Analytics",
        "icon": "Mp",
        "color": "#7856ff",
        "html": ["mixpanel.com", "mixpanel.init"],
        "headers": {},
        "meta": {},
    },
    "Segment": {
        "category": "Analytics",
        "icon": "Sg",
        "color": "#52bd94",
        "html": ["segment.com/analytics.js", "analytics.segment.com"],
        "headers": {},
        "meta": {},
    },
    "Heap": {
        "category": "Analytics",
        "icon": "Hp",
        "color": "#5f46f0",
        "html": ["heap-", "heapanalytics.com"],
        "headers": {},
        "meta": {},
    },
    "Amplitude": {
        "category": "Analytics",
        "icon": "Am",
        "color": "#1e61f0",
        "html": ["amplitude.com", "cdn.amplitude.com"],
        "headers": {},
        "meta": {},
    },

    # === Advertising ===
    "Google Ads": {
        "category": "Advertising",
        "icon": "Ad",
        "color": "#4285f4",
        "html": ["googlesyndication.com", "googleadservices.com", "adsbygoogle", "google_ads"],
        "headers": {},
        "meta": {},
    },
    "Facebook Pixel": {
        "category": "Advertising",
        "icon": "FB",
        "color": "#1877f2",
        "html": ["connect.facebook.net", "fbevents.js", "facebook.com/tr"],
        "headers": {},
        "meta": {},
    },
    "LinkedIn Insight": {
        "category": "Advertising",
        "icon": "LI",
        "color": "#0a66c2",
        "html": ["snap.licdn.com", "linkedin.com/px"],
        "headers": {},
        "meta": {},
    },
    "Twitter Pixel": {
        "category": "Advertising",
        "icon": "Tw",
        "color": "#1da1f2",
        "html": ["static.ads-twitter.com", "t.co/i/adsct"],
        "headers": {},
        "meta": {},
    },
    "TikTok Pixel": {
        "category": "Advertising",
        "icon": "TT",
        "color": "#000000",
        "html": ["analytics.tiktok.com", "tiktok.com/i18n"],
        "headers": {},
        "meta": {},
    },

    # === E-commerce ===
    "WooCommerce": {
        "category": "E-commerce",
        "icon": "Wc",
        "color": "#96588a",
        "html": ["woocommerce", "wc-cart", "wc-ajax"],
        "headers": {},
        "meta": {},
    },
    "Magento": {
        "category": "E-commerce",
        "icon": "Mg",
        "color": "#f46f25",
        "html": ["magento", "mage-init", "Magento_"],
        "headers": {"x-magento-": ""},
        "meta": {},
    },
    "BigCommerce": {
        "category": "E-commerce",
        "icon": "BC",
        "color": "#34313f",
        "html": ["bigcommerce.com", "stencil-"],
        "headers": {},
        "meta": {},
    },
    "Stripe": {
        "category": "Payments",
        "icon": "St",
        "color": "#635bff",
        "html": ["js.stripe.com", "stripe.js", "Stripe("],
        "headers": {},
        "meta": {},
    },
    "PayPal": {
        "category": "Payments",
        "icon": "PP",
        "color": "#003087",
        "html": ["paypal.com/sdk", "paypalobjects.com", "paypal-button"],
        "headers": {},
        "meta": {},
    },

    # === Marketing / Chat / CRM ===
    "HubSpot": {
        "category": "Marketing",
        "icon": "HS",
        "color": "#ff5c35",
        "html": ["hubspot.com", "hs-scripts.com", "hbspt.forms"],
        "headers": {},
        "meta": {},
    },
    "Intercom": {
        "category": "Chat / Support",
        "icon": "IC",
        "color": "#1f8ded",
        "html": ["intercom.io", "intercomSettings", "widget.intercom.io"],
        "headers": {},
        "meta": {},
    },
    "Zendesk": {
        "category": "Chat / Support",
        "icon": "ZD",
        "color": "#03363d",
        "html": ["zendesk.com", "zdassets.com", "zE("],
        "headers": {},
        "meta": {},
    },
    "Drift": {
        "category": "Chat / Support",
        "icon": "Dr",
        "color": "#0176ff",
        "html": ["drift.com", "js.driftt.com"],
        "headers": {},
        "meta": {},
    },
    "Crisp": {
        "category": "Chat / Support",
        "icon": "Cr",
        "color": "#4b75ff",
        "html": ["crisp.chat", "client.crisp.chat"],
        "headers": {},
        "meta": {},
    },
    "Mailchimp": {
        "category": "Email Marketing",
        "icon": "MC",
        "color": "#ffe01b",
        "html": ["mailchimp.com", "list-manage.com", "mc.us"],
        "headers": {},
        "meta": {},
    },
    "Cloudflare Turnstile": {
        "category": "Security",
        "icon": "CT",
        "color": "#f38020",
        "html": ["challenges.cloudflare.com/turnstile", "cf-turnstile"],
        "headers": {},
        "meta": {},
    },
    "reCAPTCHA": {
        "category": "Security",
        "icon": "rC",
        "color": "#4285f4",
        "html": ["google.com/recaptcha", "g-recaptcha", "grecaptcha"],
        "headers": {},
        "meta": {},
    },
    "hCaptcha": {
        "category": "Security",
        "icon": "hC",
        "color": "#0074bf",
        "html": ["hcaptcha.com", "h-captcha"],
        "headers": {},
        "meta": {},
    },
    "Font Awesome": {
        "category": "Font / Icon",
        "icon": "FA",
        "color": "#528dd7",
        "html": ["fontawesome", "font-awesome", "fa-solid", "fa-brands"],
        "headers": {},
        "meta": {},
    },
    "Google Fonts": {
        "category": "Font",
        "icon": "GF",
        "color": "#4285f4",
        "html": ["fonts.googleapis.com", "fonts.gstatic.com"],
        "headers": {},
        "meta": {},
    },
}

# ─── DB Setup ─────────────────────────────────────────────────────────────────
def db_init():
    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS webtech_scans (
                id SERIAL PRIMARY KEY,
                domain TEXT NOT NULL,
                url TEXT NOT NULL,
                technologies JSONB NOT NULL DEFAULT '[]',
                headers JSONB DEFAULT '{}',
                scan_time FLOAT DEFAULT 0,
                scanned_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_webtech_domain ON webtech_scans(domain);
            CREATE INDEX IF NOT EXISTS idx_webtech_scanned ON webtech_scans(scanned_at);

            CREATE TABLE IF NOT EXISTS webtech_rate_limits (
                ip TEXT NOT NULL,
                day DATE NOT NULL DEFAULT CURRENT_DATE,
                count INT DEFAULT 1,
                PRIMARY KEY (ip, day)
            );
        """)
        conn.close()
    except Exception as e:
        print(f"[WebTech] DB init warning: {e}")

def db_exec(query, params=None, fetch=True):
    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, params)
        if fetch:
            rows = [dict(r) for r in cur.fetchall()]
        else:
            rows = []
        conn.close()
        return rows
    except Exception as e:
        print(f"[WebTech] DB error: {e}")
        return []

def check_rate_limit(ip: str) -> bool:
    rows = db_exec(
        "SELECT count FROM webtech_rate_limits WHERE ip=%s AND day=CURRENT_DATE", (ip,)
    )
    if not rows:
        return True
    return rows[0]["count"] < FREE_LIMIT

def increment_rate(ip: str):
    db_exec(
        """INSERT INTO webtech_rate_limits (ip, day, count) VALUES (%s, CURRENT_DATE, 1)
           ON CONFLICT (ip, day) DO UPDATE SET count = webtech_rate_limits.count + 1""",
        (ip,), fetch=False,
    )

def save_scan(domain, url, techs, headers_dict, scan_time):
    db_exec(
        "INSERT INTO webtech_scans (domain, url, technologies, headers, scan_time) VALUES (%s,%s,%s,%s,%s)",
        (domain, url, json.dumps(techs), json.dumps(headers_dict), scan_time),
        fetch=False,
    )

def get_cached_report(domain):
    rows = db_exec(
        "SELECT * FROM webtech_scans WHERE domain=%s ORDER BY scanned_at DESC LIMIT 1",
        (domain,),
    )
    if rows:
        r = rows[0]
        age = (datetime.now(r["scanned_at"].tzinfo) - r["scanned_at"]).total_seconds()
        if age < 86400:
            return r
    return None

# ─── Detection Engine ─────────────────────────────────────────────────────────
async def scan_url(url: str) -> dict:
    if not url.startswith("http"):
        url = f"https://{url}"

    parsed = urlparse(url)
    domain = parsed.netloc.lower().lstrip("www.")

    t0 = time.time()
    detected = []

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=SCAN_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            verify=False,
        ) as client:
            resp = await client.get(url)
    except Exception as e:
        return {"error": f"Could not reach {url}: {str(e)}", "domain": domain, "url": url}

    html = resp.text.lower() if resp.text else ""
    headers = {k.lower(): v.lower() for k, v in resp.headers.items()}
    cookies = {k.lower(): v for k, v in resp.cookies.items()} if resp.cookies else {}

    # Check each technology
    for tech_name, sig in SIGNATURES.items():
        confidence = 0
        evidence = []

        # HTML pattern matching
        for pattern in sig.get("html", []):
            if pattern.lower() in html:
                confidence += 30
                evidence.append(f"HTML: '{pattern}'")

        # Header matching
        for hdr_key, hdr_val in sig.get("headers", {}).items():
            hdr_key_l = hdr_key.lower()
            if hdr_val:
                # Match header key AND value substring
                for rk, rv in headers.items():
                    if hdr_key_l in rk and hdr_val.lower() in rv:
                        confidence += 40
                        evidence.append(f"Header: {rk}={rv}")
            else:
                # Just match header key existence
                for rk in headers:
                    if hdr_key_l in rk:
                        confidence += 40
                        evidence.append(f"Header: {rk}")
                        break

        # Meta tag matching
        for meta_name, meta_val in sig.get("meta", {}).items():
            if meta_val.lower() in html and meta_name.lower() in html:
                confidence += 25
                evidence.append(f"Meta: {meta_name}")

        # Cookie matching
        for cookie_name in sig.get("cookies", []):
            if cookie_name.lower() in cookies:
                confidence += 35
                evidence.append(f"Cookie: {cookie_name}")

        # Tailwind heuristic: check for 5+ utility classes
        if tech_name == "Tailwind CSS":
            tw_patterns = sig.get("css_classes", [])
            matches = sum(1 for p in tw_patterns if f'class="' in html and p in html)
            if matches >= 5:
                confidence += 30
                evidence.append(f"CSS utility classes ({matches} patterns)")

        confidence = min(confidence, 100)
        if confidence >= 25:
            detected.append({
                "name": tech_name,
                "category": sig["category"],
                "confidence": confidence,
                "icon": sig.get("icon", "?"),
                "color": sig.get("color", "#666"),
                "evidence": evidence[:3],
            })

    # Sort by confidence descending
    detected.sort(key=lambda x: x["confidence"], reverse=True)

    scan_time = round(time.time() - t0, 2)

    # Save to DB
    headers_clean = {k: v for k, v in list(headers.items())[:30]}
    save_scan(domain, url, detected, headers_clean, scan_time)

    return {
        "domain": domain,
        "url": str(resp.url),
        "status_code": resp.status_code,
        "technologies": detected,
        "tech_count": len(detected),
        "scan_time": scan_time,
        "scanned_at": datetime.utcnow().isoformat(),
    }


# ─── API Routes ───────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    db_init()

@app.get("/api/webtech")
async def api_scan(request: Request, url: str = Query(..., description="URL to scan")):
    ip = request.headers.get("x-real-ip", request.client.host)
    if not check_rate_limit(ip):
        return JSONResponse(
            {"error": "Rate limit exceeded. Free tier: 5 scans/day. Upgrade to Pro for unlimited.", "upgrade_url": "/webtech#pricing"},
            status_code=429,
        )
    increment_rate(ip)
    result = await scan_url(url)
    return result

@app.get("/api/webtech/recent")
async def api_recent():
    rows = db_exec(
        "SELECT domain, technologies, scan_time, scanned_at FROM webtech_scans ORDER BY scanned_at DESC LIMIT 20"
    )
    for r in rows:
        if isinstance(r.get("scanned_at"), datetime):
            r["scanned_at"] = r["scanned_at"].isoformat()
        if isinstance(r.get("technologies"), str):
            r["technologies"] = json.loads(r["technologies"])
    return rows

@app.get("/webtech/report/{domain:path}", response_class=HTMLResponse)
async def report_page(domain: str):
    cached = get_cached_report(domain)
    if not cached:
        return HTMLResponse(report_html(domain, None), status_code=404)
    return HTMLResponse(report_html(domain, cached))

@app.get("/webtech", response_class=HTMLResponse)
async def landing_page(request: Request):
    return HTMLResponse(LANDING_HTML)

@app.get("/api/webtech/health")
async def health():
    return {"status": "ok", "service": "OBLIVION WebTech Profiler", "signatures": len(SIGNATURES)}


# ─── HTML Templates ───────────────────────────────────────────────────────────
def report_html(domain, data):
    if not data:
        techs_html = '<p style="color:#94a3b8;text-align:center;padding:40px">No report found. <a href="/webtech" style="color:#818cf8">Scan this domain</a>.</p>'
    else:
        techs = data.get("technologies", [])
        if isinstance(techs, str):
            techs = json.loads(techs)
        rows = ""
        for t in techs:
            ev = ", ".join(t.get("evidence", [])[:2])
            conf = t["confidence"]
            bar_color = "#10b981" if conf >= 70 else "#f59e0b" if conf >= 40 else "#94a3b8"
            rows += f'''
            <div style="display:flex;align-items:center;gap:16px;padding:14px 0;border-bottom:1px solid rgba(255,255,255,0.06)">
                <div style="width:36px;height:36px;border-radius:8px;background:{t.get("color","#666")};display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;color:#fff;flex-shrink:0">{t.get("icon","?")}</div>
                <div style="flex:1;min-width:0">
                    <div style="font-weight:600;color:#f1f5f9">{t["name"]}</div>
                    <div style="font-size:12px;color:#64748b">{t["category"]} &middot; {ev}</div>
                </div>
                <div style="width:120px;flex-shrink:0">
                    <div style="background:rgba(255,255,255,0.06);border-radius:4px;height:6px;overflow:hidden">
                        <div style="width:{conf}%;height:100%;background:{bar_color};border-radius:4px"></div>
                    </div>
                    <div style="font-size:11px;color:#94a3b8;text-align:right;margin-top:2px">{conf}%</div>
                </div>
            </div>'''
        scan_time = data.get("scan_time", 0)
        scanned = data.get("scanned_at", "")
        if isinstance(scanned, datetime):
            scanned = scanned.strftime("%Y-%m-%d %H:%M UTC")
        techs_html = f'''
        <div style="display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap">
            <div style="background:rgba(129,140,248,0.1);border:1px solid rgba(129,140,248,0.2);border-radius:10px;padding:12px 20px">
                <div style="font-size:28px;font-weight:700;color:#818cf8">{len(techs)}</div>
                <div style="font-size:12px;color:#94a3b8">Technologies</div>
            </div>
            <div style="background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.2);border-radius:10px;padding:12px 20px">
                <div style="font-size:28px;font-weight:700;color:#10b981">{scan_time}s</div>
                <div style="font-size:12px;color:#94a3b8">Scan Time</div>
            </div>
            <div style="background:rgba(245,158,11,0.1);border:1px solid rgba(245,158,11,0.2);border-radius:10px;padding:12px 20px">
                <div style="font-size:14px;font-weight:600;color:#f59e0b;margin-top:6px">{scanned}</div>
                <div style="font-size:12px;color:#94a3b8">Scanned</div>
            </div>
        </div>
        {rows}'''

    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{domain} - Technology Profile | OBLIVION WebTech</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a1a;color:#e2e8f0;min-height:100vh}}</style></head><body>
<div style="max-width:800px;margin:0 auto;padding:32px 20px">
    <div style="margin-bottom:32px">
        <a href="/webtech" style="color:#818cf8;text-decoration:none;font-size:14px">&#8592; Back to Scanner</a>
        <h1 style="font-size:28px;font-weight:800;margin-top:12px;background:linear-gradient(135deg,#818cf8,#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent">{domain}</h1>
        <p style="color:#64748b;font-size:14px">Technology Profile Report</p>
    </div>
    <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:16px;padding:24px">
        {techs_html}
    </div>
    <div style="text-align:center;margin-top:40px;padding:20px;color:#475569;font-size:13px">
        Powered by <a href="/webtech" style="color:#818cf8">OBLIVION WebTech Profiler</a> &middot; <a href="https://oblivionzone.com" style="color:#818cf8">OblivionZone.com</a>
    </div>
</div></body></html>'''


LANDING_HTML = '''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OBLIVION WebTech Profiler - Discover What Websites Are Built With</title>
<meta name="description" content="Free web technology profiler. Detect CMS, frameworks, analytics, hosting, and 60+ technologies used by any website.">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a1a;color:#e2e8f0;min-height:100vh}
.hero{text-align:center;padding:60px 20px 40px}
.hero h1{font-size:clamp(32px,5vw,52px);font-weight:800;background:linear-gradient(135deg,#818cf8,#c084fc,#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:12px}
.hero p{color:#94a3b8;font-size:18px;max-width:600px;margin:0 auto}
.scan-box{max-width:640px;margin:32px auto;display:flex;gap:0;border-radius:14px;overflow:hidden;border:1px solid rgba(129,140,248,0.3);background:rgba(255,255,255,0.03)}
.scan-box input{flex:1;padding:16px 20px;background:transparent;border:none;color:#f1f5f9;font-size:16px;outline:none}
.scan-box input::placeholder{color:#475569}
.scan-box button{padding:16px 32px;background:linear-gradient(135deg,#818cf8,#6366f1);color:#fff;border:none;font-weight:700;font-size:15px;cursor:pointer;transition:opacity .2s}
.scan-box button:hover{opacity:0.85}
.results{max-width:800px;margin:0 auto;padding:0 20px}
.result-card{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:16px;padding:24px;margin-top:24px}
.tech-item{display:flex;align-items:center;gap:16px;padding:14px 0;border-bottom:1px solid rgba(255,255,255,0.06)}
.tech-item:last-child{border:none}
.tech-icon{width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;color:#fff;flex-shrink:0}
.tech-info{flex:1;min-width:0}
.tech-name{font-weight:600;color:#f1f5f9}
.tech-cat{font-size:12px;color:#64748b}
.conf-bar{width:120px;flex-shrink:0}
.conf-track{background:rgba(255,255,255,0.06);border-radius:4px;height:6px;overflow:hidden}
.conf-fill{height:100%;border-radius:4px}
.conf-pct{font-size:11px;color:#94a3b8;text-align:right;margin-top:2px}
.stats{display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap}
.stat{background:rgba(129,140,248,0.1);border:1px solid rgba(129,140,248,0.2);border-radius:10px;padding:12px 20px;text-align:center}
.stat-val{font-size:28px;font-weight:700;color:#818cf8}
.stat-label{font-size:12px;color:#94a3b8}
.features{max-width:900px;margin:60px auto;padding:0 20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px}
.feat{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:14px;padding:24px}
.feat h3{font-size:16px;color:#f1f5f9;margin-bottom:8px}
.feat p{font-size:14px;color:#94a3b8;line-height:1.5}
.pricing{max-width:900px;margin:40px auto;padding:0 20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px}
.plan{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:16px;padding:32px;text-align:center}
.plan.pro{border-color:rgba(129,140,248,0.4);background:rgba(129,140,248,0.05)}
.plan h3{font-size:20px;margin-bottom:4px}
.plan .price{font-size:36px;font-weight:800;margin:12px 0;color:#818cf8}
.plan .price small{font-size:14px;color:#94a3b8;font-weight:400}
.plan ul{list-style:none;text-align:left;margin:16px 0}
.plan li{padding:6px 0;font-size:14px;color:#cbd5e1}
.plan li::before{content:"\\2713 ";color:#10b981;font-weight:700}
.loading{display:none;text-align:center;padding:40px}
.spinner{width:40px;height:40px;border:3px solid rgba(129,140,248,0.2);border-top-color:#818cf8;border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto 16px}
@keyframes spin{to{transform:rotate(360deg)}}
footer{text-align:center;padding:40px 20px;color:#475569;font-size:13px}
footer a{color:#818cf8}
.error{color:#f87171;text-align:center;padding:20px}
#recent{max-width:800px;margin:40px auto;padding:0 20px}
.recent-item{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-size:14px}
.recent-item a{color:#818cf8;text-decoration:none}
.recent-techs{color:#94a3b8;font-size:12px}
</style></head><body>

<div class="hero">
    <h1>OBLIVION WebTech Profiler</h1>
    <p>Discover the technology stack behind any website. Detect CMS, frameworks, analytics, hosting, and 60+ technologies instantly.</p>
</div>

<form class="scan-box" onsubmit="doScan(event)">
    <input type="text" id="urlInput" placeholder="Enter a website URL (e.g., stripe.com)" autocomplete="off" required>
    <button type="submit" id="scanBtn">Scan</button>
</form>

<div class="loading" id="loading">
    <div class="spinner"></div>
    <div style="color:#94a3b8">Scanning website technologies...</div>
</div>

<div class="results" id="results"></div>

<div class="features">
    <div class="feat">
        <h3>60+ Technologies</h3>
        <p>Detect CMS platforms, JavaScript frameworks, analytics tools, advertising pixels, e-commerce, hosting, and more.</p>
    </div>
    <div class="feat">
        <h3>Instant Results</h3>
        <p>Real-time scanning with confidence scores. Know exactly what powers any website in seconds.</p>
    </div>
    <div class="feat">
        <h3>API Access</h3>
        <p>Integrate technology detection into your workflow. RESTful JSON API with comprehensive results.</p>
    </div>
    <div class="feat">
        <h3>Trend Analysis</h3>
        <p>Track technology changes over time. See when websites add or remove tools from their stack.</p>
    </div>
</div>

<div id="pricing" style="max-width:900px;margin:20px auto;padding:0 20px">
    <h2 style="text-align:center;font-size:28px;font-weight:700;margin-bottom:24px;color:#f1f5f9">Pricing</h2>
    <div class="pricing" style="margin:0">
        <div class="plan">
            <h3>Free</h3>
            <div class="price">&pound;0<small>/mo</small></div>
            <ul>
                <li>5 scans per day</li>
                <li>60+ technology signatures</li>
                <li>Confidence scoring</li>
                <li>Shareable report links</li>
            </ul>
        </div>
        <div class="plan pro">
            <h3>Pro</h3>
            <div class="price">&pound;29<small>/mo</small></div>
            <ul>
                <li>Unlimited scans</li>
                <li>Full API access</li>
                <li>Bulk scanning (CSV upload)</li>
                <li>Historical trend data</li>
                <li>Priority support</li>
                <li>Custom webhook alerts</li>
            </ul>
            <button style="margin-top:12px;padding:12px 32px;background:linear-gradient(135deg,#818cf8,#6366f1);color:#fff;border:none;border-radius:10px;font-weight:700;cursor:pointer;font-size:15px" onclick="alert('Coming soon! Contact sales@oblivionzone.com')">Start Pro Trial</button>
        </div>
    </div>
</div>

<div id="recent">
    <h2 style="font-size:20px;font-weight:700;margin-bottom:16px;color:#f1f5f9">Recent Scans</h2>
    <div id="recentList" style="color:#64748b;font-size:14px">Loading...</div>
</div>

<footer>
    Powered by <a href="https://oblivionzone.com">OBLIVION</a> &middot; Detecting 60+ web technologies &middot; <a href="/api/webtech/health">API Status</a>
</footer>

<script>
async function doScan(e) {
    e.preventDefault();
    const url = document.getElementById('urlInput').value.trim();
    if (!url) return;
    document.getElementById('loading').style.display = 'block';
    document.getElementById('results').innerHTML = '';
    document.getElementById('scanBtn').disabled = true;
    try {
        const resp = await fetch('/api/webtech?url=' + encodeURIComponent(url));
        const data = await resp.json();
        if (data.error) {
            document.getElementById('results').innerHTML = '<div class="error">' + data.error + '</div>';
        } else {
            renderResults(data);
        }
    } catch(e) {
        document.getElementById('results').innerHTML = '<div class="error">Scan failed. Please try again.</div>';
    }
    document.getElementById('loading').style.display = 'none';
    document.getElementById('scanBtn').disabled = false;
}

function renderResults(data) {
    let techRows = '';
    for (const t of data.technologies) {
        const barColor = t.confidence >= 70 ? '#10b981' : t.confidence >= 40 ? '#f59e0b' : '#94a3b8';
        const ev = (t.evidence || []).slice(0, 2).join(', ');
        techRows += '<div class="tech-item">' +
            '<div class="tech-icon" style="background:' + t.color + '">' + t.icon + '</div>' +
            '<div class="tech-info"><div class="tech-name">' + t.name + '</div>' +
            '<div class="tech-cat">' + t.category + (ev ? ' &middot; ' + ev : '') + '</div></div>' +
            '<div class="conf-bar"><div class="conf-track"><div class="conf-fill" style="width:' + t.confidence + '%;background:' + barColor + '"></div></div>' +
            '<div class="conf-pct">' + t.confidence + '%</div></div></div>';
    }
    const html = '<div class="result-card">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">' +
        '<div><h2 style="font-size:22px;font-weight:700;color:#f1f5f9">' + data.domain + '</h2>' +
        '<div style="font-size:13px;color:#64748b">' + data.url + '</div></div>' +
        '<a href="/webtech/report/' + data.domain + '" style="color:#818cf8;font-size:13px;text-decoration:none">Permalink &#8594;</a></div>' +
        '<div class="stats">' +
        '<div class="stat"><div class="stat-val">' + data.tech_count + '</div><div class="stat-label">Technologies</div></div>' +
        '<div class="stat"><div class="stat-val">' + data.scan_time + 's</div><div class="stat-label">Scan Time</div></div>' +
        '<div class="stat"><div class="stat-val" style="font-size:18px;color:#10b981">' + data.status_code + '</div><div class="stat-label">HTTP Status</div></div>' +
        '</div>' + techRows + '</div>';
    document.getElementById('results').innerHTML = html;
}

// Load recent scans
(async function() {
    try {
        const resp = await fetch('/api/webtech/recent');
        const data = await resp.json();
        if (data.length === 0) {
            document.getElementById('recentList').textContent = 'No scans yet. Be the first!';
            return;
        }
        let html = '';
        for (const r of data.slice(0, 10)) {
            const techs = (r.technologies || []);
            const names = techs.slice(0, 4).map(t => t.name).join(', ');
            const extra = techs.length > 4 ? ' +' + (techs.length - 4) + ' more' : '';
            html += '<div class="recent-item"><a href="/webtech/report/' + r.domain + '">' + r.domain + '</a><span class="recent-techs">' + names + extra + '</span></div>';
        }
        document.getElementById('recentList').innerHTML = html;
    } catch(e) {}
})();
</script>
</body></html>'''


# ─── Run ──────────────────────────────────────────────────────────────────────


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

_SAAS_DB = "oblivion_webtech"
_SAAS_NAME = "OBLIVION WebTech"
_SAAS_PATH = "/webtech"
_SAAS_PREFIX = "oblivion_wt"
_SAAS_TIERS = [('Free', '£0', ['5 scans/day', 'Basic tech detection', '64 technologies'], '', False), ('Pro', '£19/mo', ['Unlimited scans', 'REST API', 'Tech monitoring', 'Change alerts', 'Export CSV'], '/webtech/checkout/pro', True), ('Agency', '£79/mo', ['Everything in Pro', 'White-label reports', 'Bulk scanning', 'Team accounts', 'Priority support'], '/webtech/checkout/enterprise', False)]
_SAAS_PRO_PRICE = 1900
_SAAS_BIZ_PRICE = 7900

# Initialize DB on import
ensure_db(_SAAS_DB)

@app.get("/webtech/pricing")
async def _saas_pricing():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(pricing_page_html(_SAAS_NAME, _SAAS_PATH, _SAAS_TIERS))

@app.get("/webtech/checkout/pro")
async def _saas_checkout_pro():
    from fastapi.responses import RedirectResponse
    url = create_checkout_session(f"{_SAAS_NAME} Pro", _SAAS_PRO_PRICE, "gbp",
        f"{_SAAS_NAME} Pro subscription", f"{_SAAS_PATH}/success?plan=pro", f"{_SAAS_PATH}/pricing")
    return RedirectResponse(url, status_code=303)

@app.get("/webtech/checkout/enterprise")
async def _saas_checkout_biz():
    from fastapi.responses import RedirectResponse
    url = create_checkout_session(f"{_SAAS_NAME} Business", _SAAS_BIZ_PRICE, "gbp",
        f"{_SAAS_NAME} Business subscription", f"{_SAAS_PATH}/success?plan=business", f"{_SAAS_PATH}/pricing")
    return RedirectResponse(url, status_code=303)

@app.get("/webtech/success")
async def _saas_success(session_id: str = "", plan: str = "pro"):
    from fastapi.responses import HTMLResponse
    email, api_key = handle_success(session_id, plan, _SAAS_DB, _SAAS_PREFIX)
    plan_name = "Pro" if plan == "pro" else "Business"
    if email:
        send_welcome_email(email, api_key, plan_name, _SAAS_NAME, f"https://oblivionsearch.com{_SAAS_PATH}/dashboard?key={api_key}")
    return HTMLResponse(success_page_html(_SAAS_NAME, email, api_key, plan_name, f"{_SAAS_PATH}/dashboard"))

@app.get("/webtech/dashboard")
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

@app.post("/webtech/webhook")
async def _saas_webhook(request):
    body = await request.body()
    handle_webhook(body, _SAAS_DB)
    return {"received": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
