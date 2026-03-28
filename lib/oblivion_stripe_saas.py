#!/usr/bin/env python3
"""
OBLIVION Stripe SaaS Module — Shared by all products
Provides: pricing page, Stripe checkout, webhook, dashboard, API keys, welcome email
"""

import hashlib
import json
import os
import secrets
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

import stripe
import psycopg2
import psycopg2.extras

# Stripe config
stripe.api_key = "os.environ.get("STRIPE_SECRET_KEY", "")"
DOMAIN_URL = "https://oblivionsearch.com"

# DB config
DB_CFG = dict(host="127.0.0.1", port=5432, user="postgres", password="os.environ.get("DB_PASSWORD", "change_me")")

# Email config
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "os.environ.get("SMTP_USER", "")"
SMTP_PASS = "os.environ.get("SMTP_PASS", "")"


def get_db(dbname):
    return psycopg2.connect(**DB_CFG, dbname=dbname)


def ensure_db(dbname):
    """Create database and api_keys table if needed."""
    try:
        conn = psycopg2.connect(**DB_CFG, dbname="postgres")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"SELECT 1 FROM pg_database WHERE datname='{dbname}'")
        if not cur.fetchone():
            cur.execute(f"CREATE DATABASE {dbname}")
        cur.close()
        conn.close()
    except:
        pass

    try:
        conn = get_db(dbname)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS saas_api_keys (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL UNIQUE,
                api_key VARCHAR(64) NOT NULL UNIQUE,
                plan VARCHAR(20) NOT NULL DEFAULT 'pro',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                stripe_customer_id VARCHAR(255),
                stripe_subscription_id VARCHAR(255),
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[SaaS DB] Error: {e}")


def create_checkout_session(plan_name, price_cents, currency, description, success_path, cancel_path):
    """Create a real Stripe checkout session."""
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{
            "price_data": {
                "currency": currency,
                "unit_amount": price_cents,
                "recurring": {"interval": "month"},
                "product_data": {"name": plan_name, "description": description},
            },
            "quantity": 1,
        }],
        success_url=f"{DOMAIN_URL}{success_path}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{DOMAIN_URL}{cancel_path}",
    )
    return session.url


def handle_success(session_id, plan, dbname, product_prefix):
    """Handle post-payment: create API key, send email."""
    email = ""
    api_key = ""
    try:
        if session_id and session_id != "test":
            session = stripe.checkout.Session.retrieve(session_id)
            email = session.customer_details.email if session.customer_details else ""
            stripe_cid = session.customer or ""
            stripe_sid = session.subscription or ""
        else:
            email = "test@example.com"
            stripe_cid = ""
            stripe_sid = ""

        if email:
            api_key = f"{product_prefix}_{secrets.token_hex(16)}"
            conn = get_db(dbname)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO saas_api_keys (email, api_key, plan, stripe_customer_id, stripe_subscription_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET
                    plan = EXCLUDED.plan, stripe_customer_id = EXCLUDED.stripe_customer_id,
                    stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                    is_active = TRUE, updated_at = NOW()
                RETURNING api_key
            """, (email, api_key, plan, stripe_cid, stripe_sid))
            row = cur.fetchone()
            api_key = row[0] if row else api_key
            conn.commit()
            cur.close()
            conn.close()
    except Exception as e:
        print(f"[SaaS] Success error: {e}")

    return email, api_key


def handle_webhook(payload_bytes, dbname):
    """Handle Stripe webhook events."""
    try:
        event = json.loads(payload_bytes)
    except:
        return False

    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    if etype == "checkout.session.completed":
        email = obj.get("customer_details", {}).get("email", "")
        if email:
            api_key = f"oblivion_{secrets.token_hex(16)}"
            amount = obj.get("amount_total", 0)
            plan = "business" if amount >= 5000 else "pro"
            try:
                conn = get_db(dbname)
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO saas_api_keys (email, api_key, plan, stripe_customer_id, stripe_subscription_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (email) DO UPDATE SET plan=EXCLUDED.plan, is_active=TRUE, updated_at=NOW()
                    RETURNING api_key
                """, (email, api_key, plan, obj.get("customer", ""), obj.get("subscription", "")))
                conn.commit()
                cur.close()
                conn.close()
            except:
                pass

    elif etype == "customer.subscription.deleted":
        sub_id = obj.get("id", "")
        if sub_id:
            try:
                conn = get_db(dbname)
                cur = conn.cursor()
                cur.execute("UPDATE saas_api_keys SET is_active=FALSE WHERE stripe_subscription_id=%s", (sub_id,))
                conn.commit()
                cur.close()
                conn.close()
            except:
                pass

    return True


def check_api_key(key, dbname):
    """Check if API key is valid and active. Returns plan or None."""
    if not key:
        return None
    try:
        conn = get_db(dbname)
        cur = conn.cursor()
        cur.execute("SELECT plan, is_active FROM saas_api_keys WHERE api_key=%s", (key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[1]:
            return row[0]
    except:
        pass
    return None


def send_welcome_email(email, api_key, plan_name, product_name, dashboard_url):
    """Send welcome email with API key."""
    try:
        msg = MIMEMultipart()
        msg["From"] = f"OBLIVION {product_name} <{SMTP_USER}>"
        msg["To"] = email
        msg["Subject"] = f"Welcome to OBLIVION {product_name} {plan_name}!"

        body = f"""Welcome to OBLIVION {product_name} {plan_name}!

Your account is active. Here are your details:

Plan: {plan_name}
API Key: {api_key}
Dashboard: {dashboard_url}

Use your API key in the X-API-Key header:
  curl -H "X-API-Key: {api_key}" "{DOMAIN_URL}/api/..."

Need help? Reply to this email or contact admin@oblivionzone.com.

— The OBLIVION Team
{DOMAIN_URL}
"""
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
    except Exception as e:
        print(f"[Email] Failed: {e}")


def pricing_page_html(product_name, product_path, tiers):
    """Generate a pricing page HTML. tiers = [(name, price_display, features_list, checkout_path, is_popular)]"""
    cards = ""
    for name, price, features, checkout, popular in tiers:
        border = "border:2px solid #00d4ff" if popular else "border:1px solid #1e293b"
        badge = '<div style="background:#00d4ff;color:#0a0a0f;padding:4px 12px;border-radius:12px;font-size:.75rem;font-weight:700;position:absolute;top:-12px;left:50%;transform:translateX(-50%)">MOST POPULAR</div>' if popular else ""
        feat_html = "".join(f'<li style="padding:6px 0;color:#94a3b8;font-size:.9rem">✓ {f}</li>' for f in features)
        btn = f'<a href="{checkout}" style="display:block;text-align:center;padding:14px;background:{"#00d4ff" if popular else "#1e293b"};color:{"#0a0a0f" if popular else "#e2e8f0"};border-radius:8px;text-decoration:none;font-weight:700;margin-top:auto">Get {name}</a>' if checkout else '<div style="text-align:center;padding:14px;background:#1e293b;border-radius:8px;color:#64748b">Current Plan</div>'
        cards += f'<div style="background:#12121a;{border};border-radius:16px;padding:32px 24px;position:relative;display:flex;flex-direction:column">{badge}<h3 style="color:#e2e8f0;font-size:1.2rem;margin-bottom:8px">{name}</h3><div style="font-size:2rem;font-weight:800;color:#00d4ff;margin-bottom:4px">{price}</div><ul style="list-style:none;padding:0;margin:16px 0;flex:1">{feat_html}</ul>{btn}</div>'

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{product_name} Pricing — OBLIVION</title>
<meta name="description" content="{product_name} pricing plans. Free tier available. Pro and Business plans with API access.">
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#0a0a0f;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;min-height:100vh}}
.container{{max-width:960px;margin:0 auto;padding:40px 20px}}
h1{{text-align:center;font-size:2rem;color:#00d4ff;margin-bottom:8px}}
.sub{{text-align:center;color:#94a3b8;margin-bottom:40px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:24px}}
a.back{{display:inline-block;margin-bottom:24px;color:#00d4ff;text-decoration:none}}
@media(max-width:480px){{h1{{font-size:1.5rem}}.grid{{grid-template-columns:1fr}}}}
</style></head><body><div class="container">
<a class="back" href="{product_path}">← Back to {product_name}</a>
<h1>{product_name} Pricing</h1>
<p class="sub">Choose the plan that fits your needs. Upgrade or cancel anytime.</p>
<div class="grid">{cards}</div>
<p style="text-align:center;color:#64748b;margin-top:32px;font-size:.85rem">All plans billed monthly. Cancel anytime. Powered by Stripe.</p>
</div></body></html>"""


def success_page_html(product_name, email, api_key, plan_name, dashboard_path):
    """Generate success page after payment."""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Welcome — {product_name}</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#0a0a0f;color:#e2e8f0;font-family:-apple-system,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.box{{max-width:600px;margin:20px;padding:40px;background:#12121a;border:1px solid #1e293b;border-radius:16px;text-align:center}}
h1{{color:#22c55e;margin-bottom:8px;font-size:1.8rem}}
.key{{background:#0a0a0f;border:1px solid #334155;border-radius:8px;padding:16px;margin:20px 0;text-align:left}}
.key label{{color:#64748b;font-size:.8rem;text-transform:uppercase}}
.key code{{display:block;color:#00d4ff;margin-top:4px;word-break:break-all;user-select:all}}
.btn{{display:inline-block;padding:14px 32px;background:#00d4ff;color:#0a0a0f;border-radius:8px;text-decoration:none;font-weight:700;margin-top:16px}}
</style></head><body><div class="box">
<h1>Payment Successful!</h1>
<p style="color:#94a3b8">Welcome to {product_name} {plan_name}</p>
<div class="key"><label>Your API Key</label><code>{api_key}</code></div>
<div class="key"><label>Email</label><code>{email}</code></div>
<p style="color:#94a3b8;margin:16px 0">A welcome email has been sent with your API key and setup instructions.</p>
<a href="{dashboard_path}?key={api_key}" class="btn">Go to Dashboard →</a>
</div></body></html>"""


def dashboard_page_html(product_name, product_path, account, api_key):
    """Generate customer dashboard."""
    if not account:
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard — {product_name}</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#0a0a0f;color:#e2e8f0;font-family:-apple-system,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.box{{max-width:400px;padding:40px;background:#12121a;border:1px solid #1e293b;border-radius:16px;text-align:center}}
h2{{color:#00d4ff;margin-bottom:16px}}
input{{width:100%;padding:14px;background:#0a0a0f;border:1px solid #334155;border-radius:8px;color:#e2e8f0;font-size:16px;margin:12px 0}}
.btn{{display:block;padding:14px;background:#00d4ff;color:#0a0a0f;border:none;border-radius:8px;font-weight:700;cursor:pointer;font-size:16px;width:100%}}
</style></head><body><div class="box"><h2>{product_name} Dashboard</h2>
<p style="color:#94a3b8;margin-bottom:16px">Enter your API key</p>
<form action="{product_path}/dashboard" method="get"><input name="key" placeholder="Your API key..." required><button class="btn" type="submit">Access Dashboard</button></form>
<p style="color:#64748b;margin-top:16px;font-size:.85rem">Don't have a key? <a href="{product_path}/pricing" style="color:#00d4ff">Subscribe</a></p>
</div></body></html>"""

    plan = account.get("plan", "pro")
    email = account.get("email", "")
    active = account.get("is_active", True)
    status_color = "#22c55e" if active else "#ef4444"
    status_text = "Active" if active else "Cancelled"
    created = str(account.get("created_at", ""))[:10]

    # Product-specific curl examples
    api_examples = {
        "/finance": f'curl -H "X-API-Key: {api_key}" "{DOMAIN_URL}/api/finance/search?q=Apple"\ncurl -H "X-API-Key: {api_key}" "{DOMAIN_URL}/api/finance/company/AAPL"',
        "/weather": f'curl -H "X-API-Key: {api_key}" "{DOMAIN_URL}/api/weather?q=London"',
        "/nutrition": f'curl -H "X-API-Key: {api_key}" "{DOMAIN_URL}/api/nutrition?q=chicken+breast"',
        "/instant": f'curl -H "X-API-Key: {api_key}" "{DOMAIN_URL}/api/instant/calculator?input=2%2B2"\ncurl -H "X-API-Key: {api_key}" "{DOMAIN_URL}/api/instant/hash-generator?input=hello"',
    }
    curl_example = api_examples.get(product_path, f'curl -H "X-API-Key: {api_key}" "{DOMAIN_URL}{product_path}/api/..."')

    python_examples = {
        "/finance": f'import requests\nr = requests.get("{DOMAIN_URL}/api/finance/company/TSLA",\n    headers={{"X-API-Key": "{api_key}"}})\nprint(r.json())',
        "/weather": f'import requests\nr = requests.get("{DOMAIN_URL}/api/weather",\n    params={{"q": "London"}},\n    headers={{"X-API-Key": "{api_key}"}})\nprint(r.json())',
        "/nutrition": f'import requests\nr = requests.get("{DOMAIN_URL}/api/nutrition",\n    params={{"q": "chicken breast"}},\n    headers={{"X-API-Key": "{api_key}"}})\nprint(r.json())',
        "/instant": f'import requests\nr = requests.get("{DOMAIN_URL}/api/instant/calculator",\n    params={{"input": "sqrt(144)"}},\n    headers={{"X-API-Key": "{api_key}"}})\nprint(r.json())',
    }
    python_example = python_examples.get(product_path, f'import requests\nr = requests.get("{DOMAIN_URL}{product_path}/api/...",\n    headers={{"X-API-Key": "{api_key}"}})\nprint(r.json())')

    rate_limits = {
        "/finance": {"pro": "1,000 req/day", "business": "Unlimited"},
        "/weather": {"pro": "5,000 req/day", "business": "Unlimited"},
        "/nutrition": {"pro": "5,000 req/day", "business": "Unlimited"},
        "/instant": {"pro": "10,000 req/day", "business": "Unlimited"},
    }
    limit_info = rate_limits.get(product_path, {}).get(plan, "Standard")

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard — {product_name}</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#0a0a0f;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}}
.c{{max-width:800px;margin:0 auto;padding:24px}}
h1{{color:#00d4ff;margin-bottom:4px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin:20px 0}}
.stat{{background:#12121a;border:1px solid #1e293b;border-radius:14px;padding:24px;text-align:center}}
.stat .val{{font-size:1.8rem;font-weight:700;color:#00d4ff}}
.stat .lbl{{font-size:.8rem;color:#64748b;margin-top:4px}}
.card{{background:#12121a;border:1px solid #1e293b;border-radius:12px;padding:24px;margin:16px 0}}
.card h3{{color:#00d4ff;margin-bottom:12px;font-size:1.1rem}}
.key-box{{background:#0a0a0f;border:1px solid #00d4ff40;border-radius:8px;padding:16px;font-family:'Fira Code',monospace;color:#00d4ff;word-break:break-all;user-select:all;font-size:1rem}}
.code{{background:#0a0a0f;border:1px solid #334155;border-radius:8px;padding:14px;font-family:'Fira Code',monospace;font-size:.85rem;color:#94a3b8;white-space:pre-wrap;margin:8px 0;overflow-x:auto}}
.badge{{display:inline-block;padding:4px 12px;background:#00d4ff20;color:#00d4ff;border-radius:12px;font-size:.85rem;font-weight:600}}
.code-label{{color:#64748b;font-size:.8rem;margin:12px 0 4px;text-transform:uppercase;letter-spacing:.5px}}
@media(max-width:480px){{.c{{padding:12px}}.stats{{grid-template-columns:1fr 1fr}}.stat .val{{font-size:1.3rem}}.key-box{{font-size:.85rem}}}}
</style></head><body><div class="c">
<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;margin-bottom:20px;gap:12px">
<div><h1>{product_name} Dashboard</h1><p style="color:#94a3b8">{email}</p></div>
<div><span class="badge">{plan.title()}</span> <span style="color:{status_color};font-weight:600;margin-left:8px">{status_text}</span></div>
</div>
<div class="stats">
    <div class="stat"><div class="val">{plan.title()}</div><div class="lbl">Current Plan</div></div>
    <div class="stat"><div class="val">{limit_info}</div><div class="lbl">Rate Limit</div></div>
    <div class="stat"><div class="val">{created}</div><div class="lbl">Member Since</div></div>
</div>
<div class="card"><h3>Your API Key</h3><div class="key-box">{api_key}</div>
<p style="color:#64748b;font-size:.8rem;margin-top:8px">Include as <code style="color:#00d4ff">X-API-Key</code> header in all API requests</p></div>
<div class="card"><h3>Code Examples</h3>
<div class="code-label">cURL</div>
<div class="code">{curl_example}</div>
<div class="code-label">Python</div>
<div class="code">{python_example}</div>
<div class="code-label">JavaScript</div>
<div class="code">fetch("{DOMAIN_URL}/api{product_path}/...", {{
  headers: {{ "X-API-Key": "{api_key}" }}
}}).then(r => r.json()).then(console.log)</div>
</div>
<div class="card"><h3>Need Help?</h3>
<p style="color:#94a3b8">Email us at admin@oblivionzone.com</p>
</div>
<p style="text-align:center;margin-top:24px"><a href="{product_path}" style="color:#00d4ff;text-decoration:none">&#8592; Back to {product_name}</a></p>
</div></body></html>"""
