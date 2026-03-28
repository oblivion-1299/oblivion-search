#!/usr/bin/env python3
"""OBLIVION Nutrition — Food nutrition facts powered by USDA FoodData Central
Port 3067 | Free DEMO_KEY API
"""

import json
import time
from typing import Optional, List

import httpx
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Query, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="OBLIVION Nutrition", docs_url=None, redoc_url=None)

DB_CONFIG = dict(host="127.0.0.1", port=5432, user="postgres", password="os.environ.get("DB_PASSWORD", "change_me")", dbname="postgres")
CACHE_TTL = 86400  # 24 hours for nutrition data (doesn't change often)
USDA_API_KEY = "DEMO_KEY"
USDA_SEARCH = "https://api.nal.usda.gov/fdc/v1/foods/search"
USDA_DETAIL = "https://api.nal.usda.gov/fdc/v1/food"

# Key nutrients we want to display
KEY_NUTRIENTS = {
    "Energy": {"unit": "kcal", "group": "macros"},
    "Protein": {"unit": "g", "group": "macros"},
    "Total lipid (fat)": {"unit": "g", "group": "macros"},
    "Carbohydrate, by difference": {"unit": "g", "group": "macros"},
    "Fiber, total dietary": {"unit": "g", "group": "macros"},
    "Total Sugars": {"unit": "g", "group": "macros"},
    "Calcium, Ca": {"unit": "mg", "group": "minerals"},
    "Iron, Fe": {"unit": "mg", "group": "minerals"},
    "Magnesium, Mg": {"unit": "mg", "group": "minerals"},
    "Phosphorus, P": {"unit": "mg", "group": "minerals"},
    "Potassium, K": {"unit": "mg", "group": "minerals"},
    "Sodium, Na": {"unit": "mg", "group": "minerals"},
    "Zinc, Zn": {"unit": "mg", "group": "minerals"},
    "Vitamin C, total ascorbic acid": {"unit": "mg", "group": "vitamins"},
    "Thiamin": {"unit": "mg", "group": "vitamins"},
    "Riboflavin": {"unit": "mg", "group": "vitamins"},
    "Niacin": {"unit": "mg", "group": "vitamins"},
    "Vitamin B-6": {"unit": "mg", "group": "vitamins"},
    "Folate, total": {"unit": "\u00b5g", "group": "vitamins"},
    "Vitamin B-12": {"unit": "\u00b5g", "group": "vitamins"},
    "Vitamin A, RAE": {"unit": "\u00b5g", "group": "vitamins"},
    "Vitamin D (D2 + D3)": {"unit": "\u00b5g", "group": "vitamins"},
    "Vitamin E (alpha-tocopherol)": {"unit": "mg", "group": "vitamins"},
    "Vitamin K (phylloquinone)": {"unit": "\u00b5g", "group": "vitamins"},
    "Fatty acids, total saturated": {"unit": "g", "group": "macros"},
    "Fatty acids, total trans": {"unit": "g", "group": "macros"},
    "Cholesterol": {"unit": "mg", "group": "macros"},
}

# Daily values for % calculation (FDA reference)
DAILY_VALUES = {
    "Energy": 2000, "Total lipid (fat)": 78, "Fatty acids, total saturated": 20,
    "Cholesterol": 300, "Sodium, Na": 2300, "Carbohydrate, by difference": 275,
    "Fiber, total dietary": 28, "Total Sugars": 50, "Protein": 50,
    "Vitamin C, total ascorbic acid": 90, "Calcium, Ca": 1300, "Iron, Fe": 18,
    "Potassium, K": 4700, "Vitamin D (D2 + D3)": 20, "Vitamin A, RAE": 900,
    "Vitamin E (alpha-tocopherol)": 15, "Vitamin K (phylloquinone)": 120,
    "Thiamin": 1.2, "Riboflavin": 1.3, "Niacin": 16, "Vitamin B-6": 1.7,
    "Folate, total": 400, "Vitamin B-12": 2.4, "Magnesium, Mg": 420,
    "Zinc, Zn": 11, "Phosphorus, P": 1250,
}


def _db():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    conn = _db()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS oblivion_nutrition (
            query TEXT PRIMARY KEY,
            data JSONB NOT NULL,
            cached_at DOUBLE PRECISION NOT NULL
        )
    """)
    conn.close()


def get_cached(query_key: str):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT data, cached_at FROM oblivion_nutrition WHERE query = %s", (query_key,))
    row = cur.fetchone()
    conn.close()
    if row and (time.time() - row[1]) < CACHE_TTL:
        return row[0]
    return None


def set_cached(query_key: str, data: dict):
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO oblivion_nutrition (query, data, cached_at) VALUES (%s, %s, %s)
        ON CONFLICT (query) DO UPDATE SET data = EXCLUDED.data, cached_at = EXCLUDED.cached_at
    """, (query_key, json.dumps(data), time.time()))
    conn.commit()
    conn.close()


def parse_nutrients(food_nutrients):
    """Extract key nutrients from USDA data."""
    nutrients = {}
    for n in food_nutrients:
        name = n.get("nutrientName", "")
        if name in KEY_NUTRIENTS:
            val = n.get("value", 0)
            unit = n.get("unitName", KEY_NUTRIENTS[name]["unit"])
            dv = DAILY_VALUES.get(name)
            pct = round((val / dv) * 100) if dv and val else None
            nutrients[name] = {
                "value": val,
                "unit": unit.lower() if unit else KEY_NUTRIENTS[name]["unit"],
                "group": KEY_NUTRIENTS[name]["group"],
                "daily_value_pct": pct,
            }
    return nutrients


async def search_food(query: str):
    key = f"search:{query.lower().strip()}"
    cached = get_cached(key)
    if cached:
        return cached

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(USDA_SEARCH, params={
            "query": query, "api_key": USDA_API_KEY, "pageSize": 8,
        })
        if r.status_code == 429:
            # Rate limited - don't cache, try again later
            return None
        if r.status_code != 200:
            return None
        data = r.json()
        if "error" in data:
            return None

    foods = data.get("foods", [])
    if not foods:
        return None

    results = []
    for f in foods[:8]:
        nutrients = parse_nutrients(f.get("foodNutrients", []))
        cal = nutrients.get("Energy", {}).get("value", "N/A")
        prot = nutrients.get("Protein", {}).get("value", "N/A")
        results.append({
            "fdcId": f.get("fdcId"),
            "description": f.get("description", "Unknown"),
            "category": f.get("foodCategory", ""),
            "calories": cal,
            "protein": prot,
            "nutrients": nutrients,
        })

    result = {"query": query, "foods": results}
    set_cached(key, result)
    return result


async def get_food_detail(food_query: str):
    """Search and return the top result with full nutrition."""
    key = f"detail:{food_query.lower().strip()}"
    cached = get_cached(key)
    if cached:
        return cached

    search = await search_food(food_query)
    if not search or not search.get("foods"):
        return None

    # Return the first (best match) food with full nutrients
    top = search["foods"][0]
    result = {
        "query": food_query,
        "food": top,
    }
    set_cached(key, result)
    return result


# ─── HTML Templates ───

HEADER = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — OBLIVION Nutrition</title>
<meta name="description" content="Free nutrition facts lookup powered by OBLIVION Search and USDA FoodData Central.">
<link rel="canonical" href="https://oblivionsearch.com/nutrition">
<meta property="og:title" content="OBLIVION Nutrition — Food Nutrition Facts">
<meta property="og:description" content="Free nutrition facts lookup powered by OBLIVION Search and USDA FoodData Central.">
<meta property="og:url" content="https://oblivionsearch.com/nutrition">
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

/* Nutrition Facts Label */
.nf-label{{background:#fff;color:#000;border:2px solid #000;border-radius:4px;padding:8px 12px;max-width:380px;margin:20px auto;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif}}
.nf-label .nf-title{{font-size:2.2rem;font-weight:900;border-bottom:1px solid #000;padding-bottom:2px;margin-bottom:2px}}
.nf-label .nf-serving{{font-size:.85rem;border-bottom:8px solid #000;padding-bottom:6px;margin-bottom:4px}}
.nf-label .nf-cal-row{{display:flex;justify-content:space-between;font-size:.85rem;border-bottom:4px solid #000;padding:4px 0}}
.nf-label .nf-cal-row .cal-val{{font-size:2rem;font-weight:900}}
.nf-label .nf-dv-header{{text-align:right;font-size:.75rem;font-weight:600;border-bottom:1px solid #000;padding:2px 0}}
.nf-row{{display:flex;justify-content:space-between;font-size:.85rem;padding:3px 0;border-bottom:1px solid #ccc}}
.nf-row.thick{{border-bottom:4px solid #000}}
.nf-row.medium{{border-bottom:2px solid #000}}
.nf-row .nm{{font-weight:700}}
.nf-row .sub{{padding-left:20px;font-weight:400}}
.nf-row .pct{{font-weight:700}}
.nf-footer{{font-size:.7rem;border-top:4px solid #000;padding-top:6px;margin-top:4px;line-height:1.4}}

/* Results grid */
.food-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin:20px 0}}
.food-card{{background:#12121a;border:1px solid #1a1a2e;border-radius:16px;padding:24px;transition:border-color .2s}}
.food-card:hover{{border-color:#00d4ff}}
.food-card .food-name{{font-size:1.1rem;font-weight:700;color:#fff;margin-bottom:6px}}
.food-card .food-cat{{font-size:.8rem;color:#888;margin-bottom:12px}}
.food-card .food-macros{{display:flex;gap:16px;flex-wrap:wrap}}
.food-card .macro{{text-align:center}}
.food-card .macro .mval{{font-size:1.3rem;font-weight:700;color:#00d4ff}}
.food-card .macro .mlbl{{font-size:.7rem;color:#888}}

/* Compare */
.compare-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:20px;margin:20px 0}}
.compare-item{{background:#12121a;border:1px solid #1a1a2e;border-radius:16px;padding:20px}}
.compare-item h3{{color:#fff;margin-bottom:16px;text-align:center;font-size:1.1rem}}

.section-title{{font-size:1.3rem;font-weight:600;color:#fff;margin:30px 0 15px;padding-left:4px}}
.popular{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:20px 0}}
.popular a{{display:block;background:#12121a;border:1px solid #1a1a2e;border-radius:12px;padding:16px;text-align:center;color:#e0e0e0;transition:border-color .2s}}
.popular a:hover{{border-color:#00d4ff;text-decoration:none}}
.error-msg{{text-align:center;padding:60px 20px;color:#ff4757}}
.error-msg h2{{font-size:2rem;margin-bottom:10px}}
.footer{{text-align:center;color:#555;font-size:.8rem;margin-top:50px;padding:20px 0;border-top:1px solid #1a1a2e}}
@media(max-width:600px){{.nf-label{{margin:20px 10px}}.compare-grid{{grid-template-columns:1fr}}}}
@media(max-width:480px){{
body{{overflow-x:hidden}}
.container{{padding:12px}}
.nav{{flex-direction:column;gap:8px;text-align:center}}
.search-box input{{font-size:16px}}
.search-box button{{min-height:44px;font-size:16px}}
.nf-label{{max-width:100%;padding:6px 8px}}
.nf-label .nf-title{{font-size:1.6rem}}
.food-grid{{grid-template-columns:1fr}}
.food-card{{padding:16px}}
.food-card .food-macros{{gap:10px}}
.compare-grid{{grid-template-columns:1fr}}
.popular{{grid-template-columns:1fr 1fr;gap:8px}}
.section-title{{font-size:1.1rem}}
h1,h2{{font-size:1.4rem !important}}
}}
@media(max-width:375px){{
.nf-label .nf-title{{font-size:1.3rem}}
.nf-row{{font-size:.78rem}}
.food-grid{{grid-template-columns:1fr}}
.popular{{grid-template-columns:1fr}}
.search-box form{{flex-direction:column}}
.search-box button{{width:100%}}
h1,h2{{font-size:1.2rem !important}}
}}
</style></head><body><div class="container">
<nav class="nav"><a href="/nutrition" class="logo"><span>OBLIVION</span> Nutrition</a><a href="https://oblivionsearch.com" class="back">oblivionsearch.com</a></nav>
"""

FOOTER = """
<div class="footer">
  <p>Powered by <a href="https://oblivionsearch.com">OBLIVION Search</a> &middot; Data from <a href="https://fdc.nal.usda.gov/" target="_blank">USDA FoodData Central</a></p>
  <p style="margin-top:6px">Free, private, no tracking. &copy; 2026 OBLIVION</p>
</div></div></body></html>"""

SEARCH_FORM = """<div class="search-box"><form action="/nutrition" method="get">
<input type="text" name="q" placeholder="Search any food..." value="{val}" autofocus>
<button type="submit">\U0001f50d Search</button></form></div>"""


def render_nutrition_label(food: dict):
    """Render an FDA-style Nutrition Facts label."""
    n = food.get("nutrients", {})
    cal = n.get("Energy", {}).get("value", 0)
    fat = n.get("Total lipid (fat)", {}).get("value", 0)
    sat = n.get("Fatty acids, total saturated", {}).get("value", 0)
    trans = n.get("Fatty acids, total trans", {}).get("value", 0)
    chol = n.get("Cholesterol", {}).get("value", 0)
    sodium = n.get("Sodium, Na", {}).get("value", 0)
    carb = n.get("Carbohydrate, by difference", {}).get("value", 0)
    fiber = n.get("Fiber, total dietary", {}).get("value", 0)
    sugar = n.get("Total Sugars", {}).get("value", 0)
    protein = n.get("Protein", {}).get("value", 0)

    def pct(name):
        info = n.get(name, {})
        p = info.get("daily_value_pct")
        return f"{p}%" if p is not None else ""

    html = f"""<div class="nf-label">
    <div class="nf-title">Nutrition Facts</div>
    <div class="nf-serving">Per 100g serving</div>
    <div class="nf-cal-row"><div>Calories<br><span class="cal-val">{cal:.0f}</span></div><div></div></div>
    <div class="nf-dv-header">% Daily Value*</div>
    <div class="nf-row thick"><span class="nm">Total Fat</span> {fat:.1f}g<span class="pct">{pct('Total lipid (fat)')}</span></div>
    <div class="nf-row"><span class="sub">Saturated Fat</span> {sat:.1f}g<span class="pct">{pct('Fatty acids, total saturated')}</span></div>
    <div class="nf-row"><span class="sub">Trans Fat</span> {trans:.1f}g</div>
    <div class="nf-row thick"><span class="nm">Cholesterol</span> {chol:.0f}mg<span class="pct">{pct('Cholesterol')}</span></div>
    <div class="nf-row thick"><span class="nm">Sodium</span> {sodium:.0f}mg<span class="pct">{pct('Sodium, Na')}</span></div>
    <div class="nf-row thick"><span class="nm">Total Carbohydrate</span> {carb:.1f}g<span class="pct">{pct('Carbohydrate, by difference')}</span></div>
    <div class="nf-row"><span class="sub">Dietary Fiber</span> {fiber:.1f}g<span class="pct">{pct('Fiber, total dietary')}</span></div>
    <div class="nf-row"><span class="sub">Total Sugars</span> {sugar:.1f}g</div>
    <div class="nf-row medium"><span class="nm">Protein</span> {protein:.1f}g<span class="pct">{pct('Protein')}</span></div>"""

    # Vitamins & minerals
    vit_minerals = [
        ("Vitamin C, total ascorbic acid", "Vitamin C"),
        ("Vitamin A, RAE", "Vitamin A"),
        ("Vitamin D (D2 + D3)", "Vitamin D"),
        ("Calcium, Ca", "Calcium"),
        ("Iron, Fe", "Iron"),
        ("Potassium, K", "Potassium"),
        ("Magnesium, Mg", "Magnesium"),
        ("Zinc, Zn", "Zinc"),
        ("Phosphorus, P", "Phosphorus"),
    ]
    for key, label in vit_minerals:
        info = n.get(key, {})
        if info:
            val = info.get("value", 0)
            unit = info.get("unit", "")
            p = pct(key)
            html += f'<div class="nf-row"><span>{label}</span> {val:.1f}{unit}<span class="pct">{p}</span></div>'

    html += """<div class="nf-footer">* Percent Daily Values are based on a 2,000 calorie diet.</div></div>"""
    return html


def render_food_card(food: dict):
    n = food.get("nutrients", {})
    cal = n.get("Energy", {}).get("value", "N/A")
    prot = n.get("Protein", {}).get("value", "N/A")
    carb = n.get("Carbohydrate, by difference", {}).get("value", "N/A")
    fat = n.get("Total lipid (fat)", {}).get("value", "N/A")

    desc = food.get("description", "Unknown").title()
    cat = food.get("category", "")
    safe_desc = desc.replace(" ", "+")

    return f"""<a href="/nutrition/{safe_desc}" style="text-decoration:none;color:inherit">
    <div class="food-card">
        <div class="food-name">{desc}</div>
        <div class="food-cat">{cat}</div>
        <div class="food-macros">
            <div class="macro"><div class="mval">{cal}</div><div class="mlbl">kcal</div></div>
            <div class="macro"><div class="mval">{prot}g</div><div class="mlbl">Protein</div></div>
            <div class="macro"><div class="mval">{carb}g</div><div class="mlbl">Carbs</div></div>
            <div class="macro"><div class="mval">{fat}g</div><div class="mlbl">Fat</div></div>
        </div>
    </div></a>"""


def render_search_results(data: dict, query: str):
    html = HEADER.format(title=f"{query} nutrition")
    html += SEARCH_FORM.format(val=query)
    html += f'<div class="section-title">\U0001f50e Results for "{query}"</div>'
    html += '<div class="food-grid">'
    for food in data.get("foods", []):
        html += render_food_card(food)
    html += '</div>'
    html += FOOTER
    return html


def render_food_page(data: dict, query: str):
    food = data.get("food", {})
    desc = food.get("description", query).title()
    html = HEADER.format(title=desc)
    html += SEARCH_FORM.format(val="")
    html += f'<h2 style="text-align:center;color:#fff;font-size:1.8rem;margin:20px 0">\U0001f34e {desc}</h2>'
    if food.get("category"):
        html += f'<p style="text-align:center;color:#888;margin-bottom:20px">{food["category"]}</p>'

    # Macros summary bar
    n = food.get("nutrients", {})
    cal = n.get("Energy", {}).get("value", 0)
    prot = n.get("Protein", {}).get("value", 0)
    carb = n.get("Carbohydrate, by difference", {}).get("value", 0)
    fat = n.get("Total lipid (fat)", {}).get("value", 0)
    total_macro = prot + carb + fat if (prot + carb + fat) > 0 else 1
    prot_pct = (prot / total_macro) * 100
    carb_pct = (carb / total_macro) * 100
    fat_pct = (fat / total_macro) * 100

    html += f"""<div style="display:flex;justify-content:center;gap:30px;margin:20px 0;flex-wrap:wrap">
        <div style="text-align:center"><div style="font-size:2.5rem;font-weight:700;color:#00d4ff">{cal:.0f}</div><div style="color:#888;font-size:.85rem">Calories</div></div>
        <div style="text-align:center"><div style="font-size:2.5rem;font-weight:700;color:#ff6b6b">{prot:.1f}g</div><div style="color:#888;font-size:.85rem">Protein ({prot_pct:.0f}%)</div></div>
        <div style="text-align:center"><div style="font-size:2.5rem;font-weight:700;color:#ffd93d">{carb:.1f}g</div><div style="color:#888;font-size:.85rem">Carbs ({carb_pct:.0f}%)</div></div>
        <div style="text-align:center"><div style="font-size:2.5rem;font-weight:700;color:#6bcb77">{fat:.1f}g</div><div style="color:#888;font-size:.85rem">Fat ({fat_pct:.0f}%)</div></div>
    </div>"""

    # Macro bar
    html += f"""<div style="max-width:400px;margin:10px auto 30px;height:12px;border-radius:6px;overflow:hidden;display:flex">
        <div style="width:{prot_pct}%;background:#ff6b6b"></div>
        <div style="width:{carb_pct}%;background:#ffd93d"></div>
        <div style="width:{fat_pct}%;background:#6bcb77"></div>
    </div>"""

    html += render_nutrition_label(food)

    # Compare link
    html += f"""<div style="text-align:center;margin:30px 0">
        <p style="color:#888;font-size:.9rem">Compare with another food:</p>
        <form action="/nutrition/compare" method="get" style="display:inline-flex;gap:8px;margin-top:8px">
            <input type="hidden" name="foods" value="{query}">
            <input type="text" name="foods" placeholder="Enter another food..." style="padding:10px 16px;border-radius:10px;border:1px solid #1a1a2e;background:#12121a;color:#fff;font-size:.9rem;outline:none">
            <button type="submit" style="padding:10px 20px;border-radius:10px;border:none;background:#00d4ff;color:#0a0a0f;font-weight:700;cursor:pointer">Compare</button>
        </form></div>"""

    html += FOOTER
    return html


def render_compare_page(foods_data: list, food_names: list):
    title = " vs ".join(food_names)
    html = HEADER.format(title=f"Compare: {title}")
    html += SEARCH_FORM.format(val="")
    html += f'<h2 style="text-align:center;color:#fff;font-size:1.5rem;margin:20px 0">\u2696\ufe0f Compare: {title}</h2>'

    html += '<div class="compare-grid">'
    for i, fd in enumerate(foods_data):
        if fd is None:
            html += f'<div class="compare-item"><h3>{food_names[i]}</h3><p style="color:#ff4757">Not found</p></div>'
            continue
        food = fd.get("food", {})
        desc = food.get("description", food_names[i]).title()
        html += f'<div class="compare-item"><h3>{desc}</h3>'
        html += render_nutrition_label(food)
        html += '</div>'
    html += '</div>'

    # Side-by-side macro comparison table
    html += '<div style="max-width:700px;margin:30px auto">'
    html += '<h3 style="color:#fff;text-align:center;margin-bottom:16px">\U0001f4ca Macro Comparison (per 100g)</h3>'
    html += '<table style="width:100%;border-collapse:collapse;font-size:.9rem">'
    html += '<tr style="border-bottom:2px solid #1a1a2e"><th style="text-align:left;padding:8px;color:#888">Nutrient</th>'
    for name in food_names:
        html += f'<th style="text-align:right;padding:8px;color:#00d4ff">{name.title()}</th>'
    html += '</tr>'

    compare_nutrients = [
        ("Energy", "Calories", "kcal"), ("Protein", "Protein", "g"),
        ("Carbohydrate, by difference", "Carbs", "g"), ("Total lipid (fat)", "Fat", "g"),
        ("Fiber, total dietary", "Fiber", "g"), ("Total Sugars", "Sugar", "g"),
        ("Sodium, Na", "Sodium", "mg"), ("Calcium, Ca", "Calcium", "mg"),
        ("Iron, Fe", "Iron", "mg"), ("Vitamin C, total ascorbic acid", "Vitamin C", "mg"),
    ]
    for key, label, unit in compare_nutrients:
        html += f'<tr style="border-bottom:1px solid #1a1a2e"><td style="padding:8px;color:#e0e0e0">{label}</td>'
        vals = []
        for fd in foods_data:
            if fd and fd.get("food", {}).get("nutrients", {}).get(key):
                vals.append(fd["food"]["nutrients"][key].get("value", 0))
            else:
                vals.append(0)
        max_val = max(vals) if vals else 0
        for v in vals:
            color = "#00d4ff" if v == max_val and max_val > 0 and len([x for x in vals if x == max_val]) == 1 else "#e0e0e0"
            html += f'<td style="text-align:right;padding:8px;color:{color};font-weight:600">{v:.1f} {unit}</td>'
        html += '</tr>'
    html += '</table></div>'

    html += FOOTER
    return html


def render_landing():
    html = HEADER.format(title="Nutrition")
    html += """<div style="text-align:center;margin:40px 0">
    <div style="font-size:4rem">\U0001f34e</div>
    <h1 style="font-size:2.2rem;font-weight:700;color:#fff;margin:10px 0">OBLIVION Nutrition</h1>
    <p style="color:#888;font-size:1.1rem;max-width:500px;margin:0 auto">Free nutrition facts for any food. USDA data. No tracking.</p>
    </div>"""
    html += SEARCH_FORM.format(val="")
    html += '<div class="section-title">\U0001f525 Popular Foods</div><div class="popular">'
    foods = [
        ("\U0001f34c", "Banana"), ("\U0001f34e", "Apple"), ("\U0001f95a", "Egg"),
        ("\U0001f357", "Chicken Breast"), ("\U0001f35a", "Rice"), ("\U0001f954", "Potato"),
        ("\U0001f951", "Avocado"), ("\U0001f95b", "Milk"), ("\U0001f96c", "Broccoli"),
        ("\U0001f96a", "Oatmeal"), ("\U0001f36b", "Dark Chocolate"), ("\U0001f96d", "Mango"),
    ]
    for icon, food in foods:
        html += f'<a href="/nutrition/{food}">{icon} {food}</a>'
    html += '</div>'

    html += """<div style="text-align:center;margin:30px 0">
        <p style="color:#888;margin-bottom:10px">\u2696\ufe0f Compare two foods:</p>
        <form action="/nutrition/compare" method="get" style="display:inline-flex;gap:8px;flex-wrap:wrap;justify-content:center">
            <input type="text" name="foods" placeholder="First food (e.g. banana)" style="padding:10px 16px;border-radius:10px;border:1px solid #1a1a2e;background:#12121a;color:#fff;outline:none">
            <input type="text" name="foods" placeholder="Second food (e.g. apple)" style="padding:10px 16px;border-radius:10px;border:1px solid #1a1a2e;background:#12121a;color:#fff;outline:none">
            <button type="submit" style="padding:10px 20px;border-radius:10px;border:none;background:#00d4ff;color:#0a0a0f;font-weight:700;cursor:pointer">Compare</button>
        </form></div>"""

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


@app.get("/nutrition", response_class=HTMLResponse)
async def nutrition_landing(q: Optional[str] = Query(None)):
    if q and q.strip():
        data = await search_food(q.strip())
        if data and data.get("foods"):
            # If only one result, go directly to detail
            if len(data["foods"]) == 1:
                detail = await get_food_detail(q.strip())
                if detail:
                    return HTMLResponse(render_food_page(detail, q.strip()))
            return HTMLResponse(render_search_results(data, q.strip()))
        return HTMLResponse(render_error(f'Could not find nutrition data for "{q}". Try a different food.', q))
    return HTMLResponse(render_landing())


@app.get("/nutrition/compare", response_class=HTMLResponse)
async def nutrition_compare(foods: List[str] = Query(...)):
    # foods can come as comma-separated or multiple params
    all_foods = []
    for f in foods:
        for part in f.split(","):
            part = part.strip()
            if part:
                all_foods.append(part)

    if len(all_foods) < 2:
        return HTMLResponse(render_error("Please provide at least two foods to compare.", ""))

    results = []
    for food_name in all_foods[:4]:  # Max 4 comparisons
        detail = await get_food_detail(food_name)
        results.append(detail)

    return HTMLResponse(render_compare_page(results, all_foods[:4]))


@app.get("/nutrition/health")
async def health():
    return {"status": "ok", "service": "oblivion-nutrition"}


@app.get("/api/nutrition")
async def api_nutrition(q: str = Query(..., description="Food name"), x_api_key: Optional[str] = Header(None)):
    if x_api_key:
        import sys
        sys.path.insert(0, "/opt/oblivionzone")
        from oblivion_stripe_saas import check_api_key
        plan = check_api_key(x_api_key, "oblivion_nutrition")
        if not plan:
            return JSONResponse({"error": "Invalid API key"}, status_code=401)
    detail = await get_food_detail(q)
    if not detail:
        return JSONResponse({"error": "Food not found", "query": q}, status_code=404)
    return JSONResponse({"status": "ok", **detail})




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

_SAAS_DB = "oblivion_nutrition"
_SAAS_NAME = "OBLIVION Nutrition"
_SAAS_PATH = "/nutrition"
_SAAS_PREFIX = "oblivion_nut"
_SAAS_TIERS = [('Free', '£0', ['Search foods on website', 'View nutrition facts', 'Compare 2 foods'], '', False), ('API', '£9/mo', ['REST API access', '5,000 requests/day', 'Full nutrition data', 'Food comparison API', 'Meal planning data'], '/nutrition/checkout/pro', True), ('API Pro', '£29/mo', ['Unlimited requests', 'Bulk food queries', 'Recipe nutrition calculator', 'Barcode lookup', 'Priority support'], '/nutrition/checkout/enterprise', False)]
_SAAS_PRO_PRICE = 900
_SAAS_BIZ_PRICE = 2900

# Initialize DB on import
ensure_db(_SAAS_DB)

@app.get("/nutrition/pricing")
async def _saas_pricing():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(pricing_page_html(_SAAS_NAME, _SAAS_PATH, _SAAS_TIERS))

@app.get("/nutrition/checkout/pro")
async def _saas_checkout_pro():
    from fastapi.responses import RedirectResponse
    url = create_checkout_session(f"{_SAAS_NAME} Pro", _SAAS_PRO_PRICE, "gbp",
        f"{_SAAS_NAME} Pro subscription", f"{_SAAS_PATH}/success?plan=pro", f"{_SAAS_PATH}/pricing")
    return RedirectResponse(url, status_code=303)

@app.get("/nutrition/checkout/enterprise")
async def _saas_checkout_biz():
    from fastapi.responses import RedirectResponse
    url = create_checkout_session(f"{_SAAS_NAME} Business", _SAAS_BIZ_PRICE, "gbp",
        f"{_SAAS_NAME} Business subscription", f"{_SAAS_PATH}/success?plan=business", f"{_SAAS_PATH}/pricing")
    return RedirectResponse(url, status_code=303)

@app.get("/nutrition/success")
async def _saas_success(session_id: str = "", plan: str = "pro"):
    from fastapi.responses import HTMLResponse
    email, api_key = handle_success(session_id, plan, _SAAS_DB, _SAAS_PREFIX)
    plan_name = "Pro" if plan == "pro" else "Business"
    if email:
        send_welcome_email(email, api_key, plan_name, _SAAS_NAME, f"https://oblivionsearch.com{_SAAS_PATH}/dashboard?key={api_key}")
    return HTMLResponse(success_page_html(_SAAS_NAME, email, api_key, plan_name, f"{_SAAS_PATH}/dashboard"))

@app.get("/nutrition/dashboard")
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

@app.post("/nutrition/webhook")
async def _saas_webhook(request: Request):
    body = await request.body()
    handle_webhook(body, _SAAS_DB)
    return {"received": True}

# Wildcard food route MUST be after all specific /nutrition/* routes
@app.get("/nutrition/{food}", response_class=HTMLResponse)
async def nutrition_food(food: str):
    food_query = food.replace("+", " ").replace("-", " ").replace("_", " ")
    detail = await get_food_detail(food_query)
    if detail:
        return HTMLResponse(render_food_page(detail, food_query))
    return HTMLResponse(render_error(f'Could not find nutrition data for "{food_query}". Try a different food.', food_query), status_code=404)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3067)
