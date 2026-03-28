#!/usr/bin/env python3
"""OBLIVION Weather — Free weather powered by Open-Meteo API
Port 3066 | No API key needed
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Query, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="OBLIVION Weather", docs_url=None, redoc_url=None)

DB_CONFIG = dict(host="127.0.0.1", port=5432, user="postgres", password="os.environ.get("DB_PASSWORD", "change_me")", dbname="postgres")
CACHE_TTL = 1800  # 30 minutes
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather code to description + icon mapping
WMO_CODES = {
    0: ("Clear sky", "\u2600\ufe0f"), 1: ("Mainly clear", "\U0001f324"), 2: ("Partly cloudy", "\u26c5"),
    3: ("Overcast", "\u2601\ufe0f"), 45: ("Foggy", "\U0001f32b\ufe0f"), 48: ("Rime fog", "\U0001f32b\ufe0f"),
    51: ("Light drizzle", "\U0001f326"), 53: ("Moderate drizzle", "\U0001f326"), 55: ("Dense drizzle", "\U0001f326"),
    56: ("Freezing drizzle", "\U0001f327\ufe0f"), 57: ("Heavy freezing drizzle", "\U0001f327\ufe0f"),
    61: ("Slight rain", "\U0001f327\ufe0f"), 63: ("Moderate rain", "\U0001f327\ufe0f"), 65: ("Heavy rain", "\U0001f327\ufe0f"),
    66: ("Freezing rain", "\U0001f327\ufe0f"), 67: ("Heavy freezing rain", "\U0001f327\ufe0f"),
    71: ("Slight snow", "\u2744\ufe0f"), 73: ("Moderate snow", "\u2744\ufe0f"), 75: ("Heavy snow", "\u2744\ufe0f"),
    77: ("Snow grains", "\u2744\ufe0f"), 80: ("Slight showers", "\U0001f326"), 81: ("Moderate showers", "\U0001f327\ufe0f"),
    82: ("Violent showers", "\U0001f327\ufe0f"), 85: ("Slight snow showers", "\U0001f328\ufe0f"),
    86: ("Heavy snow showers", "\U0001f328\ufe0f"), 95: ("Thunderstorm", "\U0001f329"),
    96: ("Thunderstorm + hail", "\U0001f329"), 99: ("Thunderstorm + heavy hail", "\U0001f329"),
}

def get_wmo(code):
    return WMO_CODES.get(code, ("Unknown", "\U0001f300"))


def _db():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    conn = _db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS oblivion_weather (
            query TEXT PRIMARY KEY,
            data JSONB NOT NULL,
            cached_at DOUBLE PRECISION NOT NULL
        )
    """)
    conn.close()


def get_cached(query_key: str):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT data, cached_at FROM oblivion_weather WHERE query = %s", (query_key,))
    row = cur.fetchone()
    conn.close()
    if row and (time.time() - row[1]) < CACHE_TTL:
        return row[0]
    return None


def set_cached(query_key: str, data: dict):
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO oblivion_weather (query, data, cached_at) VALUES (%s, %s, %s)
        ON CONFLICT (query) DO UPDATE SET data = EXCLUDED.data, cached_at = EXCLUDED.cached_at
    """, (query_key, json.dumps(data), time.time()))
    conn.commit()
    conn.close()


async def geocode(city: str):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(GEOCODE_URL, params={"name": city, "count": 5, "language": "en"})
        data = r.json()
        results = data.get("results", [])
        if not results:
            return None
        loc = results[0]
        return {
            "name": loc.get("name", city),
            "country": loc.get("country", ""),
            "admin1": loc.get("admin1", ""),
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "timezone": loc.get("timezone", "auto"),
        }


async def fetch_weather(lat: float, lon: float, tz: str = "auto"):
    params = {
        "latitude": lat, "longitude": lon,
        "current_weather": "true",
        "hourly": "temperature_2m,relativehumidity_2m,apparent_temperature,weathercode,windspeed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode,sunrise,sunset,windspeed_10m_max",
        "timezone": tz,
        "forecast_days": 7,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(FORECAST_URL, params=params)
        return r.json()


async def get_weather_data(city: str):
    key = city.lower().strip()
    cached = get_cached(key)
    if cached:
        return cached

    geo = await geocode(city)
    if not geo:
        return None

    forecast = await fetch_weather(geo["latitude"], geo["longitude"], geo.get("timezone", "auto"))
    if "error" in forecast:
        return None

    cw = forecast.get("current_weather", {})
    daily = forecast.get("daily", {})
    hourly = forecast.get("hourly", {})

    # Get current hour index for humidity
    current_humidity = None
    if hourly.get("time") and hourly.get("relativehumidity_2m"):
        now_str = cw.get("time", "")
        for i, t in enumerate(hourly["time"]):
            if t.startswith(now_str[:13]):
                current_humidity = hourly["relativehumidity_2m"][i]
                break
        if current_humidity is None and hourly["relativehumidity_2m"]:
            current_humidity = hourly["relativehumidity_2m"][0]

    desc, icon = get_wmo(cw.get("weathercode", 0))

    result = {
        "location": geo,
        "current": {
            "temperature": cw.get("temperature"),
            "windspeed": cw.get("windspeed"),
            "winddirection": cw.get("winddirection"),
            "weathercode": cw.get("weathercode", 0),
            "description": desc,
            "icon": icon,
            "humidity": current_humidity,
            "is_day": cw.get("is_day", 1),
            "time": cw.get("time", ""),
        },
        "daily": [],
    }

    if daily.get("time"):
        for i in range(len(daily["time"])):
            d_desc, d_icon = get_wmo(daily["weathercode"][i] if daily.get("weathercode") and i < len(daily.get("weathercode", [])) else 0)
            result["daily"].append({
                "date": daily["time"][i],
                "temp_max": daily["temperature_2m_max"][i] if daily.get("temperature_2m_max") else None,
                "temp_min": daily["temperature_2m_min"][i] if daily.get("temperature_2m_min") else None,
                "precipitation": daily["precipitation_sum"][i] if daily.get("precipitation_sum") else 0,
                "weathercode": daily["weathercode"][i] if daily.get("weathercode") else 0,
                "description": d_desc,
                "icon": d_icon,
                "windspeed_max": daily["windspeed_10m_max"][i] if daily.get("windspeed_10m_max") else None,
                "sunrise": daily["sunrise"][i] if daily.get("sunrise") else None,
                "sunset": daily["sunset"][i] if daily.get("sunset") else None,
            })

    set_cached(key, result)
    return result


# ─── HTML Templates ───

HEADER = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — OBLIVION Weather</title>
<meta name="description" content="Free weather forecasts powered by OBLIVION Search. Current conditions, 7-day forecast, no tracking.">
<link rel="canonical" href="https://oblivionsearch.com/weather">
<meta property="og:title" content="OBLIVION Weather — Free Weather Forecasts">
<meta property="og:description" content="Free weather forecasts powered by OBLIVION Search. Current conditions, 7-day forecast, no tracking.">
<meta property="og:url" content="https://oblivionsearch.com/weather">
<meta property="og:type" content="website">
<meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0f;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh}}
a{{color:#00d4ff;text-decoration:none}} a:hover{{text-decoration:underline}}
.container{{max-width:1100px;margin:0 auto;padding:20px}}
.nav{{display:flex;align-items:center;justify-content:space-between;padding:16px 0;border-bottom:1px solid #1a1a2e;margin-bottom:30px}}
.nav .logo{{font-size:1.3rem;font-weight:700;color:#fff}}
.nav .logo span{{color:#00d4ff}}
.nav a.back{{color:#888;font-size:.9rem}}
.search-box{{text-align:center;margin:30px 0}}
.search-box form{{display:inline-flex;gap:8px;width:100%;max-width:500px}}
.search-box input{{flex:1;padding:14px 20px;border-radius:12px;border:1px solid #1a1a2e;background:#12121a;color:#fff;font-size:1rem;outline:none}}
.search-box input:focus{{border-color:#00d4ff}}
.search-box button{{padding:14px 28px;border-radius:12px;border:none;background:#00d4ff;color:#0a0a0f;font-weight:700;font-size:1rem;cursor:pointer}}
.search-box button:hover{{background:#00b8d9}}
.current-card{{background:linear-gradient(135deg,#12121a 0%,#1a1a2e 100%);border:1px solid #1a1a2e;border-radius:20px;padding:40px;text-align:center;margin:30px 0}}
.current-card .icon{{font-size:5rem;margin-bottom:10px}}
.current-card .temp{{font-size:4.5rem;font-weight:700;color:#fff}}
.current-card .desc{{font-size:1.3rem;color:#00d4ff;margin:8px 0}}
.current-card .location-name{{font-size:1.5rem;color:#aaa;margin-bottom:20px}}
.current-details{{display:flex;justify-content:center;gap:40px;margin-top:20px;flex-wrap:wrap}}
.current-details .detail{{text-align:center}}
.current-details .detail .val{{font-size:1.4rem;font-weight:600;color:#fff}}
.current-details .detail .lbl{{font-size:.8rem;color:#888;margin-top:4px}}
.forecast-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px;margin:30px 0}}
.day-card{{background:#12121a;border:1px solid #1a1a2e;border-radius:16px;padding:20px;text-align:center;transition:border-color .2s}}
.day-card:hover{{border-color:#00d4ff}}
.day-card .day-name{{font-size:.9rem;color:#888;margin-bottom:8px}}
.day-card .day-icon{{font-size:2.2rem;margin:8px 0}}
.day-card .day-temps{{font-size:1rem}}
.day-card .day-temps .hi{{color:#fff;font-weight:600}}
.day-card .day-temps .lo{{color:#666}}
.day-card .day-precip{{font-size:.8rem;color:#00d4ff;margin-top:6px}}
.section-title{{font-size:1.3rem;font-weight:600;color:#fff;margin:30px 0 15px;padding-left:4px}}
.footer{{text-align:center;color:#555;font-size:.8rem;margin-top:50px;padding:20px 0;border-top:1px solid #1a1a2e}}
.error-msg{{text-align:center;padding:60px 20px;color:#ff4757}}
.error-msg h2{{font-size:2rem;margin-bottom:10px}}
.popular{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:20px 0}}
.popular a{{display:block;background:#12121a;border:1px solid #1a1a2e;border-radius:12px;padding:16px;text-align:center;color:#e0e0e0;transition:border-color .2s}}
.popular a:hover{{border-color:#00d4ff;text-decoration:none}}
.popular a .city-icon{{font-size:1.5rem}}
@media(max-width:600px){{.current-card .temp{{font-size:3rem}}.current-details{{gap:20px}}.forecast-grid{{grid-template-columns:repeat(auto-fit,minmax(100px,1fr))}}}}
@media(max-width:480px){{
body{{overflow-x:hidden}}
.container{{padding:12px}}
.nav{{flex-direction:column;gap:8px;text-align:center}}
.search-box input{{font-size:16px}}
.search-box button{{min-height:44px;font-size:16px}}
.current-card{{padding:20px}}
.current-card .icon{{font-size:3.5rem}}
.current-card .temp{{font-size:2.5rem}}
.current-card .desc{{font-size:1.1rem}}
.current-card .location-name{{font-size:1.1rem}}
.current-details{{gap:16px}}
.current-details .detail .val{{font-size:1.1rem}}
.forecast-grid{{grid-template-columns:repeat(auto-fit,minmax(90px,1fr));gap:10px}}
.day-card{{padding:12px;border-radius:12px}}
.day-card .day-icon{{font-size:1.6rem}}
.section-title{{font-size:1.1rem}}
.popular{{grid-template-columns:1fr 1fr;gap:8px}}
h1{{font-size:1.6rem !important}}
}}
@media(max-width:375px){{
.current-card .temp{{font-size:2rem}}
.current-card .desc{{font-size:1rem}}
.current-details{{flex-direction:column;gap:12px}}
.forecast-grid{{grid-template-columns:1fr 1fr;gap:8px}}
.day-card .day-name{{font-size:.75rem}}
.popular{{grid-template-columns:1fr}}
.search-box form{{flex-direction:column}}
.search-box button{{width:100%}}
h1{{font-size:1.3rem !important}}
}}
</style></head><body><div class="container">
<nav class="nav"><a href="/weather" class="logo"><span>OBLIVION</span> Weather</a><a href="https://oblivionsearch.com" class="back">oblivionsearch.com</a></nav>
"""

FOOTER = """
<div class="footer">
  <p>Powered by <a href="https://oblivionsearch.com">OBLIVION Search</a> &middot; Weather data from <a href="https://open-meteo.com/" target="_blank">Open-Meteo</a></p>
  <p style="margin-top:6px">Free, private, no tracking. &copy; 2026 OBLIVION</p>
</div></div></body></html>"""

SEARCH_FORM = """<div class="search-box"><form action="/weather" method="get">
<input type="text" name="q" placeholder="Search any city..." value="{val}" autofocus>
<button type="submit">\U0001f50d Search</button></form></div>"""


def render_weather_page(data: dict, query: str = ""):
    loc = data["location"]
    cur = data["current"]
    location_str = loc["name"]
    if loc.get("admin1"):
        location_str += f", {loc['admin1']}"
    if loc.get("country"):
        location_str += f", {loc['country']}"

    html = HEADER.format(title=loc["name"])
    html += SEARCH_FORM.format(val=query)

    # Current weather card
    html += f"""<div class="current-card">
    <div class="icon">{cur['icon']}</div>
    <div class="temp">{cur['temperature']}\u00b0C</div>
    <div class="desc">{cur['description']}</div>
    <div class="location-name">{location_str}</div>
    <div class="current-details">
        <div class="detail"><div class="val">{cur['windspeed']} km/h</div><div class="lbl">\U0001f4a8 Wind Speed</div></div>
        <div class="detail"><div class="val">{cur.get('humidity', 'N/A')}{'%' if cur.get('humidity') is not None else ''}</div><div class="lbl">\U0001f4a7 Humidity</div></div>
        <div class="detail"><div class="val">{cur.get('winddirection', 'N/A')}\u00b0</div><div class="lbl">\U0001f9ed Wind Direction</div></div>
    </div></div>"""

    # 7-day forecast
    html += '<div class="section-title">\U0001f4c5 7-Day Forecast</div><div class="forecast-grid">'
    for day in data.get("daily", []):
        try:
            dt = datetime.strptime(day["date"], "%Y-%m-%d")
            day_name = dt.strftime("%a %b %d")
        except Exception:
            day_name = day["date"]
        precip = day.get("precipitation", 0) or 0
        precip_str = f"\U0001f4a7 {precip:.1f}mm" if precip > 0 else ""
        hi = day.get("temp_max", "?")
        lo = day.get("temp_min", "?")
        html += f"""<div class="day-card">
            <div class="day-name">{day_name}</div>
            <div class="day-icon">{day['icon']}</div>
            <div class="day-temps"><span class="hi">{hi}\u00b0</span> / <span class="lo">{lo}\u00b0</span></div>
            <div class="day-precip">{precip_str}</div>
        </div>"""
    html += '</div>'

    html += FOOTER
    return html


def render_landing():
    html = HEADER.format(title="Weather")
    html += """<div style="text-align:center;margin:40px 0">
    <div style="font-size:4rem">\U0001f326\ufe0f</div>
    <h1 style="font-size:2.2rem;font-weight:700;color:#fff;margin:10px 0">OBLIVION Weather</h1>
    <p style="color:#888;font-size:1.1rem;max-width:500px;margin:0 auto">Free weather forecasts. No tracking. Just weather.</p>
    </div>"""
    html += SEARCH_FORM.format(val="")
    html += '<div class="section-title">\U0001f30d Popular Cities</div><div class="popular">'
    cities = [
        ("\U0001f1fa\U0001f1f8", "New York"), ("\U0001f1ec\U0001f1e7", "London"), ("\U0001f1ef\U0001f1f5", "Tokyo"),
        ("\U0001f1e6\U0001f1fa", "Sydney"), ("\U0001f1e9\U0001f1ea", "Berlin"), ("\U0001f1e7\U0001f1f7", "Sao Paulo"),
        ("\U0001f1ee\U0001f1f3", "Mumbai"), ("\U0001f1e8\U0001f1e6", "Toronto"), ("\U0001f1eb\U0001f1f7", "Paris"),
        ("\U0001f1f0\U0001f1f7", "Seoul"), ("\U0001f1f2\U0001f1fd", "Mexico City"), ("\U0001f1ff\U0001f1e6", "Cape Town"),
    ]
    for flag, city in cities:
        html += f'<a href="/weather/{city}"><span class="city-icon">{flag}</span> {city}</a>'
    html += '</div>'
    html += FOOTER
    return html


def render_error(msg: str, query: str = ""):
    html = HEADER.format(title="Not Found")
    html += SEARCH_FORM.format(val=query)
    html += f'<div class="error-msg"><h2>\U0001f50d No Results</h2><p>{msg}</p></div>'
    html += FOOTER
    return html


# ─── Routes ───

@app.on_event("startup")
async def startup():
    init_db()


@app.get("/weather", response_class=HTMLResponse)
async def weather_landing(q: Optional[str] = Query(None)):
    if q and q.strip():
        data = await get_weather_data(q.strip())
        if data:
            return HTMLResponse(render_weather_page(data, q.strip()))
        return HTMLResponse(render_error(f"Could not find weather for \"{q}\". Try a different city name.", q))
    return HTMLResponse(render_landing())


@app.get("/api/weather")
async def api_weather(q: str = Query(..., description="City name"), x_api_key: Optional[str] = Header(None)):
    # Check API key for paid access
    if x_api_key:
        import sys
        sys.path.insert(0, "/opt/oblivionzone")
        from oblivion_stripe_saas import check_api_key
        plan = check_api_key(x_api_key, "oblivion_weather")
        if not plan:
            return JSONResponse({"error": "Invalid API key"}, status_code=401)
    data = await get_weather_data(q)
    if not data:
        return JSONResponse({"error": "City not found", "query": q}, status_code=404)
    return JSONResponse({"status": "ok", "query": q, **data})


# Health check
@app.get("/weather/health")
async def health():
    return {"status": "ok", "service": "oblivion-weather"}




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

_SAAS_DB = "oblivion_weather"
_SAAS_NAME = "OBLIVION Weather"
_SAAS_PATH = "/weather"
_SAAS_PREFIX = "oblivion_wx"
_SAAS_TIERS = [('Free', '£0', ['View weather on website', 'Current conditions', '7-day forecast'], '', False), ('API', '£9/mo', ['REST API access', '5,000 requests/day', 'Historical weather data', '7-day forecast API', 'Multiple locations'], '/weather/checkout/pro', True), ('API Pro', '£29/mo', ['Unlimited requests', 'Bulk location queries', '30-day forecast', 'Weather alerts API', 'Priority support'], '/weather/checkout/enterprise', False)]
_SAAS_PRO_PRICE = 900
_SAAS_BIZ_PRICE = 2900

# Initialize DB on import
ensure_db(_SAAS_DB)

@app.get("/weather/pricing")
async def _saas_pricing():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(pricing_page_html(_SAAS_NAME, _SAAS_PATH, _SAAS_TIERS))

@app.get("/weather/checkout/pro")
async def _saas_checkout_pro():
    from fastapi.responses import RedirectResponse
    url = create_checkout_session(f"{_SAAS_NAME} Pro", _SAAS_PRO_PRICE, "gbp",
        f"{_SAAS_NAME} Pro subscription", f"{_SAAS_PATH}/success?plan=pro", f"{_SAAS_PATH}/pricing")
    return RedirectResponse(url, status_code=303)

@app.get("/weather/checkout/enterprise")
async def _saas_checkout_biz():
    from fastapi.responses import RedirectResponse
    url = create_checkout_session(f"{_SAAS_NAME} Business", _SAAS_BIZ_PRICE, "gbp",
        f"{_SAAS_NAME} Business subscription", f"{_SAAS_PATH}/success?plan=business", f"{_SAAS_PATH}/pricing")
    return RedirectResponse(url, status_code=303)

@app.get("/weather/success")
async def _saas_success(session_id: str = "", plan: str = "pro"):
    from fastapi.responses import HTMLResponse
    email, api_key = handle_success(session_id, plan, _SAAS_DB, _SAAS_PREFIX)
    plan_name = "Pro" if plan == "pro" else "Business"
    if email:
        send_welcome_email(email, api_key, plan_name, _SAAS_NAME, f"https://oblivionsearch.com{_SAAS_PATH}/dashboard?key={api_key}")
    return HTMLResponse(success_page_html(_SAAS_NAME, email, api_key, plan_name, f"{_SAAS_PATH}/dashboard"))

@app.get("/weather/dashboard")
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

@app.post("/weather/webhook")
async def _saas_webhook(request: Request):
    body = await request.body()
    handle_webhook(body, _SAAS_DB)
    return {"received": True}

# Wildcard city route MUST be after all specific /weather/* routes
@app.get("/weather/{city}", response_class=HTMLResponse)
async def weather_city(city: str):
    data = await get_weather_data(city)
    if data:
        return HTMLResponse(render_weather_page(data, city))
    return HTMLResponse(render_error(f"Could not find weather for \"{city}\". Try a different city name.", city), status_code=404)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3066)
