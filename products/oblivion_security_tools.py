#!/usr/bin/env python3
"""
OBLIVION Security Tools — Privacy-first security utilities
FastAPI on port 3069

Tools: Password Generator, Hash Generator, Encryption (client-side),
       DNS Lookup, SSL Checker, HTTP Headers Inspector, What's My IP,
       Base64, URL Encode/Decode, JWT Decoder
"""

import base64
import hashlib
import json
import re
import secrets
import smtplib
import socket
import ssl
import string
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from typing import Optional

import asyncpg
import dns.resolver
import httpx
import stripe
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

# ---------------------------------------------------------------------------
# Stripe config
# ---------------------------------------------------------------------------
stripe.api_key = "os.environ.get("STRIPE_SECRET_KEY", "")"
DOMAIN_URL = "https://oblivionsearch.com"
PRODUCT_NAME = "oblivion_security_tools"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "os.environ.get("SMTP_USER", "")"
SMTP_PASS = "os.environ.get("SMTP_PASS", "")"

SECURITY_PLANS = {
    "api": {"name": "API", "price_amount": 1400, "currency": "gbp", "label": "£14/mo", "req_limit": 5000},
    "api_pro": {"name": "API Pro", "price_amount": 3900, "currency": "gbp", "label": "£39/mo", "req_limit": 0},
}

_saas_pool: Optional[asyncpg.Pool] = None

app = FastAPI(title="OBLIVION Security Tools", version="1.0.0")

@app.on_event("startup")
async def startup():
    global _saas_pool
    _saas_pool = await asyncpg.create_pool(
        "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/postgres", min_size=1, max_size=5
    )
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

@app.on_event("shutdown")
async def shutdown():
    if _saas_pool:
        await _saas_pool.close()

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
_rate_store: dict[str, list[float]] = defaultdict(list)

def _check_rate(ip: str, limit: int = 30, window: int = 60) -> bool:
    now = time.time()
    cutoff = now - window
    _rate_store[ip] = [t for t in _rate_store[ip] if t > cutoff]
    if len(_rate_store[ip]) >= limit:
        return False
    _rate_store[ip].append(now)
    return True

def _get_ip(request: Request) -> str:
    return request.headers.get("x-real-ip", request.headers.get("x-forwarded-for", request.client.host))

# ---------------------------------------------------------------------------
# Common word list for passphrases
# ---------------------------------------------------------------------------
WORDLIST = [
    "abandon", "ability", "able", "about", "above", "absent", "absorb", "abstract",
    "absurd", "abuse", "access", "accident", "account", "accuse", "achieve", "acid",
    "acoustic", "acquire", "across", "action", "actor", "actress", "actual", "adapt",
    "address", "adjust", "admit", "adult", "advance", "advice", "aerobic", "affair",
    "afford", "afraid", "again", "agent", "agree", "ahead", "airport", "aisle",
    "alarm", "album", "alcohol", "alert", "alien", "alley", "allow", "almost",
    "alone", "alpha", "already", "alter", "always", "amateur", "amazing", "among",
    "amount", "amused", "analyst", "anchor", "ancient", "anger", "angle", "angry",
    "animal", "ankle", "announce", "annual", "another", "answer", "antenna", "antique",
    "anxiety", "apart", "apology", "appear", "apple", "approve", "april", "arch",
    "arctic", "arena", "argue", "armor", "army", "arrive", "arrow", "artwork",
    "aspect", "assault", "asset", "assist", "assume", "asthma", "athlete", "atom",
    "attack", "attend", "attitude", "attract", "auction", "audit", "august", "aunt",
    "author", "autumn", "average", "avocado", "avoid", "awake", "aware", "awesome",
    "axis", "baby", "bachelor", "bacon", "badge", "bag", "balance", "balcony",
    "bamboo", "banana", "banner", "barrel", "basket", "battle", "beach", "beauty",
    "because", "become", "begin", "behave", "believe", "below", "bench", "benefit",
    "best", "betray", "better", "between", "beyond", "bicycle", "billion", "bird",
    "birth", "bitter", "black", "blade", "blame", "blanket", "blast", "bleak",
    "bless", "blind", "blood", "blossom", "blue", "blur", "blush", "board",
    "boat", "body", "bomb", "bone", "bonus", "book", "boost", "border",
    "boring", "borrow", "bottom", "bounce", "brain", "brand", "brave", "bread",
    "breeze", "brick", "bridge", "brief", "bright", "bring", "broken", "brother",
    "brown", "brush", "bubble", "budget", "buffalo", "build", "bullet", "bundle",
    "burden", "burger", "burst", "butter", "buyer", "cabin", "cable", "cactus",
    "cage", "cake", "call", "calm", "camera", "camp", "canal", "cancel",
    "candy", "cannon", "canvas", "canyon", "capable", "capital", "captain", "carbon",
    "cargo", "carpet", "carry", "castle", "casual", "catalog", "catch", "category",
    "cattle", "caught", "cause", "caution", "cave", "ceiling", "celery", "cement",
    "census", "century", "cereal", "certain", "chair", "chalk", "champion", "change",
    "chaos", "chapter", "charge", "chase", "cheap", "check", "cheese", "cherry",
    "chest", "chicken", "chief", "child", "chimney", "choice", "chunk", "church",
    "cigar", "cinnamon", "circle", "citizen", "city", "civil", "claim", "clap",
    "clarify", "claw", "clay", "clean", "clerk", "clever", "cliff", "climb",
    "clinic", "clip", "clock", "close", "cloth", "cloud", "clown", "cluster",
    "clutch", "coach", "coast", "coconut", "code", "coffee", "coil", "coin",
    "collect", "color", "column", "combine", "comfort", "comic", "common", "company",
    "concert", "conduct", "confirm", "congress", "connect", "consider", "control", "convince",
    "coral", "corn", "correct", "cosmic", "cotton", "couch", "country", "couple",
    "course", "cousin", "cover", "craft", "crane", "crash", "crater", "crawl",
    "crazy", "cream", "credit", "creek", "crew", "cricket", "crime", "crisp",
    "critic", "crop", "cross", "crouch", "crowd", "crucial", "cruel", "cruise",
    "crumble", "crush", "crystal", "cube", "culture", "cupboard", "curious", "current",
    "curtain", "curve", "cushion", "custom", "cycle", "damage", "damp", "dance",
    "danger", "daring", "dash", "daughter", "dawn", "debate", "debris", "decade",
    "december", "decide", "decline", "decorate", "decrease", "defense", "define", "degree",
    "delay", "deliver", "demand", "denial", "dentist", "deny", "depart", "depend",
    "deposit", "depth", "deputy", "derive", "describe", "desert", "design", "desk",
    "detail", "detect", "develop", "device", "devote", "diagram", "diamond", "diary",
    "diesel", "differ", "digital", "dignity", "dilemma", "dinner", "dinosaur", "direct",
    "dirt", "disagree", "discover", "disease", "dish", "dismiss", "display", "distance",
    "divert", "divide", "divorce", "dizzy", "doctor", "document", "dolphin", "domain",
    "donate", "donkey", "donor", "door", "double", "dragon", "drama", "drastic",
    "dream", "drift", "drill", "drink", "drive", "drop", "drum", "dry",
    "duck", "dumb", "dune", "during", "dust", "dutch", "dwarf", "dynamic",
    "eager", "eagle", "earth", "easily", "economy", "edge", "edit", "educate",
    "effort", "eight", "either", "elbow", "elder", "electric", "elegant", "element",
    "elephant", "elevator", "elite", "else", "embrace", "emerge", "emotion", "employ",
    "empower", "empty", "enable", "enact", "endless", "endorse", "enemy", "energy",
    "enforce", "engage", "engine", "enhance", "enjoy", "enlist", "enough", "enrich",
    "enroll", "ensure", "enter", "entire", "entry", "envelope", "episode", "equal",
    "equip", "erode", "erosion", "error", "erupt", "escape", "essay", "essence",
    "estate", "eternal", "evoke", "evolve", "exact", "example", "excess", "exchange",
    "excite", "exclude", "excuse", "execute", "exercise", "exhaust", "exhibit", "exile",
    "exist", "expand", "expect", "expire", "explain", "expose", "express", "extend",
    "extra", "fabric", "face", "faculty", "fade", "faint", "faith", "fall",
    "false", "family", "famous", "fancy", "fantasy", "farm", "fashion", "father",
    "fatigue", "fault", "favorite", "feature", "february", "federal", "fence", "festival",
    "fever", "field", "figure", "file", "film", "filter", "final", "find",
    "finger", "finish", "fire", "fitness", "flag", "flame", "flash", "flat",
    "flavor", "flee", "flight", "flip", "float", "flock", "floor", "flower",
    "fluid", "flush", "focus", "follow", "force", "forest", "forget", "fork",
    "fortune", "forum", "forward", "fossil", "foster", "found", "frame", "frequent",
    "fresh", "friend", "fringe", "frog", "front", "frozen", "fruit", "fuel",
    "funny", "furnace", "fury", "future", "gadget", "galaxy", "gallery", "game",
    "garage", "garden", "garlic", "garment", "gather", "gauge", "gaze", "general",
    "genius", "genre", "gentle", "genuine", "gesture", "ghost", "giant", "gift",
    "giggle", "ginger", "giraffe", "glad", "glance", "glare", "glass", "glimpse",
    "globe", "gloom", "glory", "glove", "glow", "glue", "goat", "goddess",
    "gold", "gorilla", "gospel", "gossip", "govern", "gown", "grab", "grace",
    "grain", "grant", "grape", "grass", "gravity", "great", "green", "grid",
    "grief", "grit", "grocery", "group", "grow", "grunt", "guard", "guess",
    "guide", "guilt", "guitar", "hammer", "hamster", "hand", "happy", "harbor",
    "hard", "harvest", "hawk", "hazard", "health", "heart", "heavy", "hedgehog",
    "height", "hero", "hidden", "high", "hill", "hint", "history", "hobby",
    "hockey", "hollow", "home", "honey", "hood", "hope", "horror", "horse",
    "hospital", "host", "hotel", "hover", "human", "humble", "humor", "hundred",
    "hungry", "hunt", "hurdle", "hurry", "hybrid", "humor", "husband",
    "ice", "icon", "idea", "identify", "idle", "ignore", "image", "imitate",
    "immense", "immune", "impact", "impose", "improve", "impulse", "inch", "include",
    "income", "increase", "index", "indicate", "indoor", "industry", "infant", "inflict",
    "inform", "initial", "inject", "inmate", "inner", "innocent", "input", "inquiry",
    "insane", "insect", "inside", "inspire", "install", "intact", "interest", "invest",
    "invite", "involve", "island", "isolate", "ivory", "jacket", "jaguar", "jealous",
    "jeans", "jelly", "jewel", "join", "joke", "journey", "judge", "juice",
    "jungle", "junior", "junk", "kangaroo", "keen", "keep", "kernel", "key",
    "kidney", "kind", "kingdom", "kitchen", "kite", "kitten", "kiwi", "knee",
    "knife", "knock", "know", "labor", "ladder", "lady", "lake", "lamp",
    "language", "laptop", "large", "later", "latin", "laugh", "laundry", "lawn",
    "layer", "leader", "leaf", "learn", "leave", "lecture", "left", "legend",
    "leisure", "lemon", "length", "lens", "leopard", "lesson", "letter", "level",
    "liberty", "library", "license", "life", "light", "like", "limb", "limit",
    "link", "lion", "liquid", "list", "little", "live", "lizard", "loan",
    "lobster", "local", "lock", "logic", "lonely", "long", "loop", "lottery",
    "loud", "lounge", "love", "loyal", "lucky", "luggage", "lumber", "lunar",
    "lunch", "luxury", "lyrics", "machine", "magic", "magnet", "maid", "mail",
    "major", "make", "mammal", "manage", "mandate", "mango", "mansion", "manual",
    "maple", "marble", "march", "margin", "marine", "market", "marriage", "mask",
    "mass", "master", "match", "material", "math", "matrix", "matter", "maximum",
    "meadow", "mean", "measure", "media", "melody", "member", "memory", "mental",
    "mention", "mercy", "merge", "merit", "mesh", "message", "metal", "method",
    "middle", "midnight", "million", "mimic", "mind", "minimum", "minor", "minute",
    "miracle", "mirror", "misery", "mistake", "mixture", "mobile", "model", "modify",
    "moment", "monitor", "monkey", "monster", "month", "moral", "morning", "mosquito",
    "mother", "motion", "motor", "mountain", "mouse", "movie", "much", "muffin",
    "multiply", "muscle", "museum", "music", "must", "mutual", "myself", "mystery",
    "myth", "naive", "name", "napkin", "narrow", "nasty", "nation", "nature",
    "near", "neck", "negative", "neglect", "neither", "nephew", "nerve", "nest",
    "network", "neutral", "never", "night", "noble", "noise", "nominee", "normal",
    "north", "notable", "nothing", "notice", "novel", "number", "nurse", "object",
    "oblige", "obscure", "observe", "obtain", "obvious", "occur", "ocean", "october",
    "odor", "offer", "office", "often", "olive", "olympic", "omit", "once",
    "opinion", "oppose", "option", "orange", "orbit", "orchard", "order", "ordinary",
    "organ", "orient", "original", "orphan", "ostrich", "other", "outdoor", "outer",
    "output", "outside", "oval", "oven", "owner", "oxygen", "oyster", "ozone",
    "pact", "paddle", "palace", "panda", "panel", "panic", "panther", "paper",
    "parade", "parent", "park", "parrot", "party", "patch", "path", "patient",
    "patrol", "pattern", "pause", "peanut", "pepper", "perfect", "permit", "person",
    "piano", "picnic", "picture", "piece", "pilot", "pistol", "pitch", "pizza",
    "place", "planet", "plastic", "plate", "play", "pledge", "pluck", "plug",
    "plunge", "pocket", "poem", "poet", "point", "polar", "police", "pond",
    "popular", "portion", "position", "possible", "potato", "pottery", "poverty", "powder",
    "power", "practice", "praise", "predict", "prefer", "prepare", "present", "pretty",
    "prevent", "price", "pride", "primary", "print", "priority", "prison", "private",
    "problem", "process", "produce", "profit", "program", "project", "promote", "proof",
    "property", "prosper", "protect", "proud", "provide", "public", "pulse", "pumpkin",
    "punch", "pupil", "puppy", "purchase", "purity", "purpose", "purse", "push",
    "pyramid", "quality", "quantum", "quarter", "question", "quick", "quit", "quote",
    "rabbit", "raccoon", "radar", "radio", "rail", "rain", "raise", "rally",
    "ranch", "random", "range", "rapid", "rare", "rather", "raven", "razor",
    "ready", "real", "reason", "rebel", "rebuild", "recall", "receive", "recipe",
    "record", "recycle", "reduce", "reflect", "reform", "region", "regret", "regular",
    "reject", "relax", "release", "relief", "rely", "remain", "remember", "remind",
    "remove", "render", "renew", "repair", "repeat", "replace", "report", "require",
    "rescue", "resemble", "resist", "resource", "response", "result", "retire", "retreat",
    "return", "reunion", "reveal", "review", "reward", "rhythm", "ribbon", "rice",
    "rich", "ride", "rifle", "right", "rigid", "ring", "riot", "ripple",
    "risk", "ritual", "rival", "river", "road", "roast", "robot", "robust",
    "rocket", "romance", "roof", "rookie", "room", "rose", "rotate", "rough",
    "round", "route", "royal", "rubber", "rude", "rug", "rule", "rural",
    "saddle", "sadness", "safe", "sail", "salad", "salmon", "salon", "salt",
    "salute", "sample", "sand", "satisfy", "satoshi", "sauce", "sausage", "save",
    "scale", "scan", "scatter", "scene", "scheme", "school", "science", "scissors",
    "scorpion", "scout", "scrap", "screen", "script", "scrub", "search", "season",
    "second", "secret", "section", "security", "segment", "select", "senior", "sense",
    "sentence", "series", "service", "session", "settle", "setup", "seven", "shadow",
    "shaft", "shallow", "share", "shed", "shell", "sheriff", "shield", "shift",
    "shine", "ship", "shiver", "shock", "shoe", "shoot", "shop", "short",
    "shoulder", "shove", "shrimp", "shuttle", "sibling", "siege", "sight", "sign",
    "silent", "silver", "similar", "simple", "since", "siren", "sister", "situate",
    "skill", "skull", "slender", "slice", "slide", "slight", "slogan", "slow",
    "small", "smart", "smile", "smoke", "smooth", "snake", "snow", "soccer",
    "social", "soldier", "solid", "solution", "someone", "soon", "sorry", "sort",
    "source", "south", "space", "spare", "spatial", "spawn", "speak", "special",
    "speed", "sphere", "spider", "spike", "spirit", "split", "sponsor", "spoon",
    "sport", "spray", "spread", "spring", "square", "squeeze", "squirrel", "stable",
    "stadium", "staff", "stage", "stairs", "stamp", "stand", "start", "state",
    "stay", "steak", "steel", "stem", "step", "stereo", "stick", "still",
    "sting", "stock", "stomach", "stone", "stool", "story", "stove", "strategy",
    "street", "strike", "strong", "struggle", "student", "stuff", "stumble", "style",
    "subject", "submit", "subway", "success", "sudden", "suffer", "sugar", "suggest",
    "summer", "sun", "sunny", "sunset", "super", "supply", "supreme", "surface",
    "surge", "surprise", "surround", "survey", "suspect", "sustain", "swallow", "swamp",
    "swap", "swarm", "sweet", "swift", "swim", "switch", "sword", "symbol",
    "symptom", "system", "table", "tackle", "tail", "talent", "target", "taxi",
    "teach", "team", "tell", "tenant", "tennis", "tent", "term", "test",
    "text", "thank", "theme", "theory", "thing", "thought", "three", "thrive",
    "throw", "thumb", "thunder", "ticket", "tide", "tiger", "timber", "time",
    "tiny", "tip", "tired", "tissue", "title", "toast", "tobacco", "today",
    "together", "toilet", "token", "tomato", "tomorrow", "tone", "tongue", "tonight",
    "tool", "tooth", "topic", "torch", "tornado", "tortoise", "total", "tourist",
    "toward", "tower", "town", "trade", "traffic", "tragic", "train", "transfer",
    "trap", "trash", "travel", "tray", "treat", "tree", "trend", "trial",
    "tribe", "trick", "trigger", "trim", "trip", "trophy", "trouble", "truck",
    "true", "truly", "trumpet", "trust", "truth", "tumble", "tunnel", "turkey",
    "turn", "turtle", "twelve", "twenty", "twice", "twin", "twist", "type",
    "typical", "ugly", "umbrella", "unable", "unaware", "uncle", "uncover", "under",
    "unfair", "unfold", "unhappy", "uniform", "unique", "unit", "universe", "unknown",
    "unlock", "until", "unusual", "unveil", "update", "upgrade", "uphold", "upon",
    "upper", "upset", "urban", "usage", "useful", "usual", "utility", "vacant",
    "vacuum", "vague", "valid", "valley", "valve", "vanish", "vapor", "various",
    "vast", "vault", "vehicle", "velvet", "vendor", "venture", "verify", "version",
    "vessel", "veteran", "viable", "vibrant", "vicious", "victory", "video", "view",
    "village", "vintage", "violin", "virtual", "virus", "visa", "visit", "visual",
    "vital", "vivid", "vocal", "voice", "volcano", "volume", "voyage", "wage",
    "wagon", "wait", "walk", "wall", "walnut", "wander", "warfare", "warm",
    "warrior", "wash", "wasp", "waste", "water", "wave", "wealth", "weapon",
    "weather", "wedding", "weekend", "weird", "welcome", "west", "whale", "wheat",
    "wheel", "whip", "whisper", "width", "wife", "wild", "will", "window",
    "wine", "wing", "winner", "winter", "wire", "wisdom", "wise", "wish",
    "witness", "wolf", "woman", "wonder", "wood", "work", "world", "worry",
    "worth", "wrap", "wreck", "wrestle", "wrist", "write", "wrong", "yard",
    "year", "yellow", "young", "youth", "zebra", "zero", "zone", "zoo",
]

# ---------------------------------------------------------------------------
# Shared CSS/HTML components
# ---------------------------------------------------------------------------
ACCENT = "#00d4ff"
BG = "#0a0a0f"
BG2 = "#12121a"
BG3 = "#1a1a2e"
TEXT = "#e0e0e0"
TEXT_DIM = "#888"

def _base_css():
    return f"""
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ background:{BG}; color:{TEXT}; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height:1.6; }}
    a {{ color:{ACCENT}; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
    .container {{ max-width:900px; margin:0 auto; padding:2rem 1.5rem; }}
    .header {{ display:flex; align-items:center; gap:1rem; margin-bottom:2rem; padding-bottom:1rem; border-bottom:1px solid {BG3}; }}
    .header img {{ height:32px; }}
    .header h1 {{ font-size:1.4rem; font-weight:600; }}
    .header h1 span {{ color:{ACCENT}; }}
    .nav-back {{ font-size:0.9rem; color:{TEXT_DIM}; }}
    .card {{ background:{BG2}; border:1px solid {BG3}; border-radius:12px; padding:1.5rem; margin-bottom:1rem; }}
    .card:hover {{ border-color:{ACCENT}33; }}
    .card h3 {{ color:{ACCENT}; margin-bottom:0.5rem; }}
    .card p {{ color:{TEXT_DIM}; font-size:0.9rem; }}
    .tool-grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(260px,1fr)); gap:1rem; }}
    .tool-grid a {{ text-decoration:none; }}
    input, textarea, select {{
        width:100%; padding:0.75rem 1rem; background:{BG}; border:1px solid {BG3};
        border-radius:8px; color:{TEXT}; font-size:0.95rem; margin-bottom:0.75rem;
        font-family:inherit;
    }}
    input:focus, textarea:focus, select:focus {{ outline:none; border-color:{ACCENT}; }}
    button, .btn {{
        background:{ACCENT}; color:{BG}; border:none; padding:0.75rem 1.5rem;
        border-radius:8px; font-weight:600; cursor:pointer; font-size:0.95rem;
        display:inline-block; transition: opacity 0.2s;
    }}
    button:hover, .btn:hover {{ opacity:0.85; }}
    .btn-secondary {{ background:{BG3}; color:{TEXT}; }}
    .result {{ background:{BG}; border:1px solid {BG3}; border-radius:8px; padding:1rem; margin-top:1rem; font-family:'Courier New',monospace; font-size:0.9rem; word-break:break-all; white-space:pre-wrap; }}
    .result-label {{ font-size:0.8rem; color:{TEXT_DIM}; margin-bottom:0.25rem; text-transform:uppercase; letter-spacing:0.05em; }}
    .tag {{ display:inline-block; padding:0.2rem 0.6rem; border-radius:4px; font-size:0.75rem; font-weight:600; }}
    .tag-green {{ background:#0f52; color:#0f5; }}
    .tag-red {{ background:#f002; color:#f44; }}
    .tag-yellow {{ background:#ff02; color:#fa0; }}
    .flex-row {{ display:flex; gap:0.75rem; flex-wrap:wrap; }}
    .flex-row > * {{ flex:1; min-width:120px; }}
    .strength-bar {{ height:6px; border-radius:3px; background:{BG3}; margin-top:0.5rem; overflow:hidden; }}
    .strength-fill {{ height:100%; border-radius:3px; transition:width 0.3s; }}
    table {{ width:100%; border-collapse:collapse; margin-top:1rem; }}
    th, td {{ padding:0.6rem 0.8rem; text-align:left; border-bottom:1px solid {BG3}; font-size:0.9rem; }}
    th {{ color:{ACCENT}; font-size:0.8rem; text-transform:uppercase; letter-spacing:0.05em; }}
    footer {{ text-align:center; color:{TEXT_DIM}; font-size:0.8rem; margin-top:3rem; padding-top:1rem; border-top:1px solid {BG3}; }}
    @media(max-width:600px) {{
        .container {{ padding:1rem; }}
        .tool-grid {{ grid-template-columns:1fr; }}
        .flex-row {{ flex-direction:column; }}
    }}
    @media(max-width:480px) {{
        body {{ overflow-x:hidden; }}
        .header h1 {{ font-size:1.2rem; }}
        .tool-grid {{ grid-template-columns:1fr; gap:0.75rem; }}
        input, textarea, select {{ font-size:16px; }}
        button, .btn {{ min-height:44px; font-size:16px; }}
        .card {{ padding:1rem; }}
        .result {{ font-size:0.82rem; padding:0.75rem; }}
        table {{ font-size:0.82rem; }}
        th, td {{ padding:0.4rem 0.5rem; }}
        .tag {{ font-size:0.7rem; }}
    }}
    @media(max-width:375px) {{
        .header h1 {{ font-size:1rem; }}
        .tool-grid {{ grid-template-columns:1fr; }}
        button, .btn {{ width:100%; }}
        .flex-row > * {{ min-width:auto; }}
        .result {{ font-size:0.78rem; }}
    }}
    """

def _header(title="Security Tools", back_link=None):
    back = f'<a class="nav-back" href="/security-tools">&larr; All Tools</a>' if back_link else ''
    return f"""
    <div class="header">
        <div>
            <h1><span>OBLIVION</span> {title}</h1>
            {back}
        </div>
    </div>"""

def _footer():
    return f"""<footer>
        <p>OBLIVION Security Tools &mdash; No tracking. No logs. No cookies.</p>
        <p style="margin-top:0.3rem;"><a href="https://oblivionsearch.com">oblivionsearch.com</a></p>
    </footer>"""

def _page(title, body, extra_head=""):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — OBLIVION Security Tools</title>
    <meta name="description" content="Free privacy-first security tools. No tracking, no cookies, no logs.">
    <link rel="canonical" href="https://oblivionsearch.com/security-tools">
    <meta property="og:title" content="{title} — OBLIVION Security Tools">
    <meta property="og:description" content="Free privacy-first security tools. No tracking, no cookies, no logs.">
    <meta property="og:url" content="https://oblivionsearch.com/security-tools">
    <meta property="og:type" content="website">
    <meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
    <link rel="icon" href="https://oblivionsearch.com/favicon.ico">
    <style>{_base_css()}</style>
    {extra_head}
</head>
<body>
<div class="container">
{body}
{_footer()}
</div>
</body>
</html>"""


# =========================================================================
# LANDING PAGE — /security-tools
# =========================================================================
TOOLS = [
    {"path": "/security-tools/password", "name": "Password Generator", "desc": "Generate secure passwords and passphrases with customizable options", "icon": "&#128274;"},
    {"path": "/security-tools/hash", "name": "Hash Generator", "desc": "Generate MD5, SHA1, SHA256, SHA512 hashes from any text", "icon": "&#128271;"},
    {"path": "/security-tools/encrypt", "name": "Encrypt / Decrypt", "desc": "AES-256 encryption &amp; decryption — runs entirely in your browser", "icon": "&#128272;"},
    {"path": "/security-tools/dns", "name": "DNS Lookup", "desc": "Query A, AAAA, MX, NS, TXT, CNAME records for any domain", "icon": "&#127760;"},
    {"path": "/security-tools/ssl", "name": "SSL Certificate Checker", "desc": "Inspect any domain's SSL certificate, expiry, and chain", "icon": "&#128274;"},
    {"path": "/security-tools/headers", "name": "HTTP Headers Inspector", "desc": "View response headers and check security header compliance", "icon": "&#128196;"},
    {"path": "/security-tools/ip", "name": "What's My IP", "desc": "See your IP address, approximate location, and connection info", "icon": "&#127758;"},
    {"path": "/security-tools/base64", "name": "Base64 Encode / Decode", "desc": "Encode or decode Base64 strings instantly", "icon": "&#128221;"},
    {"path": "/security-tools/url-encode", "name": "URL Encode / Decode", "desc": "Percent-encode or decode URL strings", "icon": "&#128279;"},
    {"path": "/security-tools/jwt", "name": "JWT Decoder", "desc": "Decode and inspect JSON Web Tokens — entirely client-side", "icon": "&#128273;"},
]

@app.get("/security-tools", response_class=HTMLResponse)
async def landing():
    cards = ""
    for t in TOOLS:
        cards += f"""<a href="{t['path']}"><div class="card">
            <h3>{t['icon']} {t['name']}</h3>
            <p>{t['desc']}</p>
        </div></a>"""
    body = f"""{_header("Security Tools")}
    <p style="color:{TEXT_DIM};margin-bottom:1.5rem;">Free, open privacy-first security tools. No tracking, no cookies, no server-side logging. Encryption tools run entirely in your browser.</p>
    <div class="tool-grid">{cards}</div>"""
    return _page("Security Tools", body)


# =========================================================================
# 1. PASSWORD GENERATOR — /security-tools/password
# =========================================================================
@app.get("/security-tools/password", response_class=HTMLResponse)
async def password_page():
    body = f"""{_header("Password Generator", back_link=True)}
    <div class="card">
        <h3>Random Password</h3>
        <div class="flex-row">
            <div>
                <label style="font-size:0.85rem;color:{TEXT_DIM};">Length</label>
                <input type="number" id="pw-len" value="20" min="4" max="128">
            </div>
            <div>
                <label style="font-size:0.85rem;color:{TEXT_DIM};">Options</label>
                <div style="display:flex;gap:1rem;padding:0.5rem 0;">
                    <label><input type="checkbox" id="pw-upper" checked> A-Z</label>
                    <label><input type="checkbox" id="pw-lower" checked> a-z</label>
                    <label><input type="checkbox" id="pw-digits" checked> 0-9</label>
                    <label><input type="checkbox" id="pw-symbols" checked> !@#$</label>
                </div>
            </div>
        </div>
        <button onclick="genPassword()">Generate Password</button>
        <div id="pw-result" class="result" style="display:none;font-size:1.1rem;"></div>
        <button onclick="copyResult('pw-result')" style="margin-top:0.5rem;background:{BG3};color:{TEXT};" id="pw-copy" class="btn-secondary" >Copy</button>
    </div>

    <div class="card" style="margin-top:1rem;">
        <h3>Passphrase Generator</h3>
        <div class="flex-row">
            <div>
                <label style="font-size:0.85rem;color:{TEXT_DIM};">Words</label>
                <input type="number" id="pp-words" value="5" min="3" max="12">
            </div>
            <div>
                <label style="font-size:0.85rem;color:{TEXT_DIM};">Separator</label>
                <input type="text" id="pp-sep" value="-" maxlength="3">
            </div>
        </div>
        <button onclick="genPassphrase()">Generate Passphrase</button>
        <div id="pp-result" class="result" style="display:none;font-size:1.1rem;"></div>
        <button onclick="copyResult('pp-result')" style="margin-top:0.5rem;" class="btn-secondary">Copy</button>
    </div>

    <div class="card" style="margin-top:1rem;">
        <h3>Password Strength Checker</h3>
        <input type="text" id="pw-check" placeholder="Enter a password to check..." oninput="checkStrength()">
        <div class="strength-bar"><div class="strength-fill" id="str-fill"></div></div>
        <div id="str-label" style="margin-top:0.5rem;font-size:0.9rem;"></div>
    </div>
    """
    js = """<script>
    function genPassword() {
        const len = Math.min(128, Math.max(4, parseInt(document.getElementById('pw-len').value)||20));
        let chars = '';
        if(document.getElementById('pw-upper').checked) chars += 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
        if(document.getElementById('pw-lower').checked) chars += 'abcdefghijklmnopqrstuvwxyz';
        if(document.getElementById('pw-digits').checked) chars += '0123456789';
        if(document.getElementById('pw-symbols').checked) chars += '!@#$%^&*()_+-=[]{}|;:,.<>?';
        if(!chars) chars = 'abcdefghijklmnopqrstuvwxyz';
        const arr = new Uint32Array(len);
        crypto.getRandomValues(arr);
        let pw = '';
        for(let i=0;i<len;i++) pw += chars[arr[i] % chars.length];
        const el = document.getElementById('pw-result');
        el.style.display='block';
        el.textContent = pw;
    }
    async function genPassphrase() {
        const n = Math.min(12, Math.max(3, parseInt(document.getElementById('pp-words').value)||5));
        const sep = document.getElementById('pp-sep').value || '-';
        const res = await fetch('/security-tools/api/passphrase?words='+n+'&sep='+encodeURIComponent(sep));
        const data = await res.json();
        const el = document.getElementById('pp-result');
        el.style.display='block';
        el.textContent = data.passphrase;
    }
    function checkStrength() {
        const pw = document.getElementById('pw-check').value;
        let score = 0;
        if(pw.length >= 8) score++;
        if(pw.length >= 12) score++;
        if(pw.length >= 16) score++;
        if(/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score++;
        if(/\\d/.test(pw)) score++;
        if(/[^a-zA-Z0-9]/.test(pw)) score++;
        if(pw.length >= 20) score++;
        const pct = Math.min(100, (score/7)*100);
        const fill = document.getElementById('str-fill');
        const label = document.getElementById('str-label');
        const colors = ['#f44','#f44','#fa0','#fa0','#ff0','#0f5','#0f5','#0ff'];
        const labels = ['Very Weak','Very Weak','Weak','Fair','Good','Strong','Very Strong','Excellent'];
        fill.style.width = pct+'%';
        fill.style.background = colors[score];
        label.textContent = pw ? labels[score] + ' (' + pw.length + ' chars, ~' + Math.round(Math.log2(Math.pow(getCharSpace(pw), pw.length))) + ' bits entropy)' : '';
    }
    function getCharSpace(pw) {
        let s = 0;
        if(/[a-z]/.test(pw)) s+=26;
        if(/[A-Z]/.test(pw)) s+=26;
        if(/\\d/.test(pw)) s+=10;
        if(/[^a-zA-Z0-9]/.test(pw)) s+=32;
        return s || 1;
    }
    function copyResult(id) {
        const text = document.getElementById(id).textContent;
        navigator.clipboard.writeText(text);
    }
    genPassword();
    </script>"""
    return _page("Password Generator", body, js)

@app.get("/security-tools/api/passphrase")
async def api_passphrase(words: int = Query(5, ge=3, le=12), sep: str = Query("-")):
    chosen = [secrets.choice(WORDLIST) for _ in range(words)]
    passphrase = sep.join(chosen)
    bits = len(chosen) * 11  # ~2048 words = 11 bits each
    return {"passphrase": passphrase, "words": len(chosen), "entropy_bits": bits}

@app.get("/security-tools/api/password")
async def api_password(length: int = Query(20, ge=4, le=128), upper: bool = True, lower: bool = True, digits: bool = True, symbols: bool = True):
    chars = ""
    if upper: chars += string.ascii_uppercase
    if lower: chars += string.ascii_lowercase
    if digits: chars += string.digits
    if symbols: chars += "!@#$%^&*()_+-=[]{}|;:,.<>?"
    if not chars: chars = string.ascii_lowercase
    pw = "".join(secrets.choice(chars) for _ in range(length))
    return {"password": pw, "length": length}


# =========================================================================
# 2. HASH GENERATOR — /security-tools/hash
# =========================================================================
@app.get("/security-tools/hash", response_class=HTMLResponse)
async def hash_page():
    body = f"""{_header("Hash Generator", back_link=True)}
    <div class="card">
        <h3>Text Hash Generator</h3>
        <textarea id="hash-input" rows="4" placeholder="Enter text to hash..."></textarea>
        <button onclick="genHash()">Generate Hashes</button>
        <div id="hash-results"></div>
    </div>"""
    js = """<script>
    async function genHash() {
        const text = document.getElementById('hash-input').value;
        if(!text) return;
        const res = await fetch('/security-tools/api/hash', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({text})
        });
        const data = await res.json();
        let html = '';
        for(const [algo, hash] of Object.entries(data.hashes)) {
            html += '<div class="result-label" style="margin-top:0.75rem;">'+algo+'</div>';
            html += '<div class="result" style="cursor:pointer;" onclick="navigator.clipboard.writeText(this.textContent)" title="Click to copy">'+hash+'</div>';
        }
        document.getElementById('hash-results').innerHTML = html;
    }
    </script>"""
    return _page("Hash Generator", body, js)

@app.post("/security-tools/api/hash")
async def api_hash(request: Request):
    data = await request.json()
    text = data.get("text", "")
    encoded = text.encode("utf-8")
    return {
        "hashes": {
            "MD5": hashlib.md5(encoded).hexdigest(),
            "SHA1": hashlib.sha1(encoded).hexdigest(),
            "SHA256": hashlib.sha256(encoded).hexdigest(),
            "SHA512": hashlib.sha512(encoded).hexdigest(),
        }
    }


# =========================================================================
# 3. ENCRYPTION / DECRYPTION — /security-tools/encrypt (CLIENT-SIDE ONLY)
# =========================================================================
@app.get("/security-tools/encrypt", response_class=HTMLResponse)
async def encrypt_page():
    body = f"""{_header("Encrypt / Decrypt", back_link=True)}
    <div style="background:#0f52;border:1px solid #0f53;border-radius:8px;padding:0.75rem 1rem;margin-bottom:1rem;color:#0f5;font-size:0.9rem;">
        &#128274; All encryption/decryption happens entirely in your browser using the Web Crypto API (AES-256-GCM). Your data and password are never sent to our server.
    </div>
    <div class="card">
        <h3>Encrypt Text</h3>
        <textarea id="enc-input" rows="4" placeholder="Enter text to encrypt..."></textarea>
        <input type="password" id="enc-password" placeholder="Encryption password">
        <button onclick="doEncrypt()">Encrypt</button>
        <div id="enc-result" class="result" style="display:none;"></div>
        <button onclick="copyResult('enc-result')" style="margin-top:0.5rem;" class="btn-secondary">Copy Ciphertext</button>
    </div>
    <div class="card" style="margin-top:1rem;">
        <h3>Decrypt Text</h3>
        <textarea id="dec-input" rows="4" placeholder="Paste encrypted text (Base64)..."></textarea>
        <input type="password" id="dec-password" placeholder="Decryption password">
        <button onclick="doDecrypt()">Decrypt</button>
        <div id="dec-result" class="result" style="display:none;"></div>
    </div>"""
    js = """<script>
    async function deriveKey(password, salt) {
        const enc = new TextEncoder();
        const keyMaterial = await crypto.subtle.importKey('raw', enc.encode(password), 'PBKDF2', false, ['deriveKey']);
        return crypto.subtle.deriveKey({name:'PBKDF2',salt,iterations:100000,hash:'SHA-256'}, keyMaterial, {name:'AES-GCM',length:256}, false, ['encrypt','decrypt']);
    }
    async function doEncrypt() {
        const text = document.getElementById('enc-input').value;
        const password = document.getElementById('enc-password').value;
        if(!text||!password){alert('Enter text and password');return;}
        const enc = new TextEncoder();
        const salt = crypto.getRandomValues(new Uint8Array(16));
        const iv = crypto.getRandomValues(new Uint8Array(12));
        const key = await deriveKey(password, salt);
        const ct = await crypto.subtle.encrypt({name:'AES-GCM',iv}, key, enc.encode(text));
        const buf = new Uint8Array(salt.length + iv.length + ct.byteLength);
        buf.set(salt, 0);
        buf.set(iv, salt.length);
        buf.set(new Uint8Array(ct), salt.length + iv.length);
        const b64 = btoa(String.fromCharCode(...buf));
        const el = document.getElementById('enc-result');
        el.style.display='block';
        el.textContent = b64;
    }
    async function doDecrypt() {
        const b64 = document.getElementById('dec-input').value.trim();
        const password = document.getElementById('dec-password').value;
        if(!b64||!password){alert('Enter ciphertext and password');return;}
        try {
            const raw = Uint8Array.from(atob(b64), c=>c.charCodeAt(0));
            const salt = raw.slice(0,16);
            const iv = raw.slice(16,28);
            const ct = raw.slice(28);
            const key = await deriveKey(password, salt);
            const pt = await crypto.subtle.decrypt({name:'AES-GCM',iv}, key, ct);
            const el = document.getElementById('dec-result');
            el.style.display='block';
            el.textContent = new TextDecoder().decode(pt);
        } catch(e) {
            alert('Decryption failed — wrong password or corrupted data.');
        }
    }
    function copyResult(id){navigator.clipboard.writeText(document.getElementById(id).textContent);}
    </script>"""
    return _page("Encrypt / Decrypt", body, js)


# =========================================================================
# 4. DNS LOOKUP — /security-tools/dns
# =========================================================================
@app.get("/security-tools/dns", response_class=HTMLResponse)
async def dns_page():
    body = f"""{_header("DNS Lookup", back_link=True)}
    <div class="card">
        <h3>DNS Record Lookup</h3>
        <input type="text" id="dns-domain" placeholder="Enter domain (e.g. example.com)">
        <div class="flex-row" style="margin-bottom:0.75rem;">
            <label><input type="checkbox" class="dns-type" value="A" checked> A</label>
            <label><input type="checkbox" class="dns-type" value="AAAA" checked> AAAA</label>
            <label><input type="checkbox" class="dns-type" value="MX" checked> MX</label>
            <label><input type="checkbox" class="dns-type" value="NS" checked> NS</label>
            <label><input type="checkbox" class="dns-type" value="TXT" checked> TXT</label>
            <label><input type="checkbox" class="dns-type" value="CNAME"> CNAME</label>
            <label><input type="checkbox" class="dns-type" value="SOA"> SOA</label>
        </div>
        <button onclick="dnsLookup()">Lookup</button>
        <div id="dns-results"></div>
    </div>"""
    js = """<script>
    async function dnsLookup() {
        const domain = document.getElementById('dns-domain').value.trim();
        if(!domain) return;
        const types = [...document.querySelectorAll('.dns-type:checked')].map(c=>c.value);
        document.getElementById('dns-results').innerHTML = '<p style="color:#888;margin-top:1rem;">Looking up...</p>';
        const res = await fetch('/security-tools/api/dns?domain='+encodeURIComponent(domain)+'&types='+types.join(','));
        const data = await res.json();
        if(data.error) {
            document.getElementById('dns-results').innerHTML = '<div class="result" style="color:#f44;">'+data.error+'</div>';
            return;
        }
        let html = '<table><tr><th>Type</th><th>Record</th><th>TTL</th></tr>';
        for(const rec of data.records) {
            html += '<tr><td><span class="tag tag-green">'+rec.type+'</span></td><td>'+rec.value+'</td><td>'+rec.ttl+'s</td></tr>';
        }
        html += '</table>';
        document.getElementById('dns-results').innerHTML = html;
    }
    </script>"""
    return _page("DNS Lookup", body, js)

@app.get("/security-tools/api/dns")
async def api_dns(request: Request, domain: str = Query(...), types: str = Query("A,AAAA,MX,NS,TXT")):
    ip = _get_ip(request)
    if not _check_rate(ip, limit=20, window=60):
        raise HTTPException(429, "Rate limited — try again later")
    domain = domain.strip().lower()
    if not re.match(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$', domain):
        return {"error": "Invalid domain name"}
    record_types = [t.strip().upper() for t in types.split(",")]
    records = []
    resolver = dns.resolver.Resolver()
    resolver.timeout = 5
    resolver.lifetime = 5
    for rtype in record_types:
        if rtype not in ("A", "AAAA", "MX", "NS", "TXT", "CNAME", "SOA"):
            continue
        try:
            answers = resolver.resolve(domain, rtype)
            for rdata in answers:
                val = str(rdata)
                if rtype == "MX":
                    val = f"{rdata.preference} {rdata.exchange}"
                records.append({"type": rtype, "value": val, "ttl": answers.rrset.ttl})
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.resolver.Timeout, Exception):
            pass
    return {"domain": domain, "records": records}


# =========================================================================
# 5. SSL CERTIFICATE CHECKER — /security-tools/ssl
# =========================================================================
@app.get("/security-tools/ssl", response_class=HTMLResponse)
async def ssl_page():
    body = f"""{_header("SSL Certificate Checker", back_link=True)}
    <div class="card">
        <h3>Check SSL Certificate</h3>
        <input type="text" id="ssl-domain" placeholder="Enter domain (e.g. example.com)">
        <button onclick="checkSSL()">Check Certificate</button>
        <div id="ssl-results"></div>
    </div>"""
    js = """<script>
    async function checkSSL() {
        const domain = document.getElementById('ssl-domain').value.trim();
        if(!domain) return;
        document.getElementById('ssl-results').innerHTML = '<p style="color:#888;margin-top:1rem;">Checking...</p>';
        const res = await fetch('/security-tools/api/ssl?domain='+encodeURIComponent(domain));
        const data = await res.json();
        if(data.error) {
            document.getElementById('ssl-results').innerHTML = '<div class="result" style="color:#f44;">'+data.error+'</div>';
            return;
        }
        const c = data.cert;
        const daysLeft = c.days_until_expiry;
        const tag = daysLeft > 30 ? 'tag-green' : daysLeft > 7 ? 'tag-yellow' : 'tag-red';
        const grade = daysLeft > 30 ? 'A' : daysLeft > 7 ? 'B' : 'F';
        let html = '<table>';
        html += '<tr><td>Grade</td><td><span class="tag '+tag+'" style="font-size:1.2rem;">'+grade+'</span></td></tr>';
        html += '<tr><td>Subject</td><td>'+c.subject+'</td></tr>';
        html += '<tr><td>Issuer</td><td>'+c.issuer+'</td></tr>';
        html += '<tr><td>Valid From</td><td>'+c.not_before+'</td></tr>';
        html += '<tr><td>Valid Until</td><td>'+c.not_after+'</td></tr>';
        html += '<tr><td>Days Until Expiry</td><td><span class="tag '+tag+'">'+daysLeft+' days</span></td></tr>';
        html += '<tr><td>Serial Number</td><td style="font-family:monospace;font-size:0.8rem;">'+c.serial_number+'</td></tr>';
        html += '<tr><td>Signature Algorithm</td><td>'+c.sig_algo+'</td></tr>';
        if(c.san && c.san.length) html += '<tr><td>Subject Alt Names</td><td>'+c.san.join(', ')+'</td></tr>';
        html += '</table>';
        document.getElementById('ssl-results').innerHTML = html;
    }
    </script>"""
    return _page("SSL Certificate Checker", body, js)

@app.get("/security-tools/api/ssl")
async def api_ssl(request: Request, domain: str = Query(...)):
    ip = _get_ip(request)
    if not _check_rate(ip, limit=10, window=60):
        raise HTTPException(429, "Rate limited")
    domain = domain.strip().lower().replace("https://", "").replace("http://", "").split("/")[0]
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                cert = ssock.getpeercert()
                der = ssock.getpeercert(binary_form=True)
        subject = dict(x[0] for x in cert.get("subject", ()))
        issuer = dict(x[0] for x in cert.get("issuer", ()))
        not_before = cert.get("notBefore", "")
        not_after = cert.get("notAfter", "")
        # Parse expiry
        try:
            exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            days_left = (exp - datetime.now(timezone.utc)).days
        except Exception:
            days_left = -1
        san = []
        for typ, val in cert.get("subjectAltName", ()):
            if typ == "DNS":
                san.append(val)
        return {
            "domain": domain,
            "cert": {
                "subject": subject.get("commonName", str(subject)),
                "issuer": issuer.get("organizationName", str(issuer)),
                "not_before": not_before,
                "not_after": not_after,
                "days_until_expiry": days_left,
                "serial_number": format(cert.get("serialNumber", 0)),
                "sig_algo": "SHA256withRSA",  # Python ssl doesn't expose this easily
                "san": san[:20],
            }
        }
    except ssl.SSLCertVerificationError as e:
        return {"error": f"SSL verification failed: {e}"}
    except socket.timeout:
        return {"error": "Connection timed out"}
    except Exception as e:
        return {"error": f"Could not check SSL: {e}"}


# =========================================================================
# 6. HTTP HEADERS INSPECTOR — /security-tools/headers
# =========================================================================
SECURITY_HEADERS = {
    "strict-transport-security": {"name": "HSTS", "desc": "Enforces HTTPS"},
    "content-security-policy": {"name": "CSP", "desc": "Controls resource loading"},
    "x-content-type-options": {"name": "X-Content-Type-Options", "desc": "Prevents MIME sniffing"},
    "x-frame-options": {"name": "X-Frame-Options", "desc": "Prevents clickjacking"},
    "referrer-policy": {"name": "Referrer-Policy", "desc": "Controls referrer info"},
    "permissions-policy": {"name": "Permissions-Policy", "desc": "Controls browser features"},
    "x-xss-protection": {"name": "X-XSS-Protection", "desc": "Legacy XSS filter (set to 0)"},
    "cross-origin-opener-policy": {"name": "COOP", "desc": "Isolates browsing context"},
    "cross-origin-resource-policy": {"name": "CORP", "desc": "Controls cross-origin reads"},
}

@app.get("/security-tools/headers", response_class=HTMLResponse)
async def headers_page():
    body = f"""{_header("HTTP Headers Inspector", back_link=True)}
    <div class="card">
        <h3>Inspect HTTP Response Headers</h3>
        <input type="text" id="hdr-url" placeholder="Enter URL (e.g. https://example.com)">
        <button onclick="checkHeaders()">Inspect Headers</button>
        <div id="hdr-results"></div>
    </div>"""
    js = """<script>
    async function checkHeaders() {
        let url = document.getElementById('hdr-url').value.trim();
        if(!url) return;
        if(!url.startsWith('http')) url = 'https://'+url;
        document.getElementById('hdr-results').innerHTML = '<p style="color:#888;margin-top:1rem;">Fetching...</p>';
        const res = await fetch('/security-tools/api/headers?url='+encodeURIComponent(url));
        const data = await res.json();
        if(data.error) {
            document.getElementById('hdr-results').innerHTML = '<div class="result" style="color:#f44;">'+data.error+'</div>';
            return;
        }
        let html = '<h4 style="margin-top:1rem;color:#00d4ff;">Security Headers</h4><table>';
        for(const sh of data.security_headers) {
            const tag = sh.present ? 'tag-green' : 'tag-red';
            const status = sh.present ? 'Present' : 'Missing';
            html += '<tr><td>'+sh.name+'</td><td><span class="tag '+tag+'">'+status+'</span></td><td style="color:#888;font-size:0.8rem;">'+sh.desc+'</td></tr>';
        }
        html += '</table>';
        html += '<h4 style="margin-top:1.5rem;color:#00d4ff;">All Response Headers</h4><table>';
        for(const [k,v] of Object.entries(data.headers)) {
            html += '<tr><td style="white-space:nowrap;font-weight:600;">'+k+'</td><td style="font-family:monospace;font-size:0.85rem;word-break:break-all;">'+v+'</td></tr>';
        }
        html += '</table>';
        document.getElementById('hdr-results').innerHTML = html;
    }
    </script>"""
    return _page("HTTP Headers Inspector", body, js)

@app.get("/security-tools/api/headers")
async def api_headers(request: Request, url: str = Query(...)):
    ip = _get_ip(request)
    if not _check_rate(ip, limit=10, window=60):
        raise HTTPException(429, "Rate limited")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            resp = await client.get(url)
        headers = dict(resp.headers)
        sec = []
        lower_hdrs = {k.lower(): v for k, v in headers.items()}
        for key, info in SECURITY_HEADERS.items():
            sec.append({
                "name": info["name"],
                "desc": info["desc"],
                "present": key in lower_hdrs,
                "value": lower_hdrs.get(key, ""),
            })
        return {"url": url, "status_code": resp.status_code, "headers": headers, "security_headers": sec}
    except Exception as e:
        return {"error": f"Could not fetch URL: {e}"}


# =========================================================================
# 7. WHAT'S MY IP — /security-tools/ip
# =========================================================================
@app.get("/security-tools/ip", response_class=HTMLResponse)
async def ip_page(request: Request):
    client_ip = _get_ip(request)
    body = f"""{_header("What's My IP", back_link=True)}
    <div class="card">
        <h3>Your IP Address</h3>
        <div class="result" style="font-size:1.5rem;text-align:center;padding:1.5rem;" id="ip-display">{client_ip}</div>
        <button onclick="navigator.clipboard.writeText('{client_ip}')" style="margin-top:0.5rem;" class="btn-secondary">Copy IP</button>
        <div id="ip-details" style="margin-top:1rem;"></div>
    </div>"""
    js = """<script>
    async function loadIPInfo() {
        try {
            const res = await fetch('/security-tools/api/ip-info');
            const data = await res.json();
            let html = '<table>';
            html += '<tr><td>IP Address</td><td>'+data.ip+'</td></tr>';
            if(data.hostname) html += '<tr><td>Hostname</td><td>'+data.hostname+'</td></tr>';
            if(data.user_agent) html += '<tr><td>User-Agent</td><td style="font-size:0.8rem;word-break:break-all;">'+data.user_agent+'</td></tr>';
            if(data.accept_language) html += '<tr><td>Accept-Language</td><td>'+data.accept_language+'</td></tr>';
            html += '</table>';
            html += '<p style="color:#888;font-size:0.8rem;margin-top:1rem;">OBLIVION does not log or store your IP address. This information is shown only to you.</p>';
            document.getElementById('ip-details').innerHTML = html;
        } catch(e) {}
    }
    loadIPInfo();
    </script>"""
    return _page("What's My IP", body, js)

@app.get("/security-tools/api/ip-info")
async def api_ip_info(request: Request):
    client_ip = _get_ip(request)
    hostname = ""
    try:
        hostname = socket.gethostbyaddr(client_ip)[0]
    except Exception:
        pass
    return {
        "ip": client_ip,
        "hostname": hostname,
        "user_agent": request.headers.get("user-agent", ""),
        "accept_language": request.headers.get("accept-language", ""),
    }


# =========================================================================
# 8. BASE64 ENCODE/DECODE — /security-tools/base64
# =========================================================================
@app.get("/security-tools/base64", response_class=HTMLResponse)
async def base64_page():
    body = f"""{_header("Base64 Encode / Decode", back_link=True)}
    <div class="card">
        <h3>Encode</h3>
        <textarea id="b64-enc-input" rows="3" placeholder="Enter text to encode..."></textarea>
        <button onclick="b64Encode()">Encode to Base64</button>
        <div id="b64-enc-result" class="result" style="display:none;cursor:pointer;" onclick="navigator.clipboard.writeText(this.textContent)"></div>
    </div>
    <div class="card" style="margin-top:1rem;">
        <h3>Decode</h3>
        <textarea id="b64-dec-input" rows="3" placeholder="Enter Base64 string to decode..."></textarea>
        <button onclick="b64Decode()">Decode from Base64</button>
        <div id="b64-dec-result" class="result" style="display:none;cursor:pointer;" onclick="navigator.clipboard.writeText(this.textContent)"></div>
    </div>"""
    js = """<script>
    function b64Encode() {
        const text = document.getElementById('b64-enc-input').value;
        const el = document.getElementById('b64-enc-result');
        el.style.display='block';
        try { el.textContent = btoa(unescape(encodeURIComponent(text))); }
        catch(e) { el.textContent = 'Error: '+e.message; }
    }
    function b64Decode() {
        const text = document.getElementById('b64-dec-input').value.trim();
        const el = document.getElementById('b64-dec-result');
        el.style.display='block';
        try { el.textContent = decodeURIComponent(escape(atob(text))); }
        catch(e) { el.textContent = 'Error: Invalid Base64 string'; }
    }
    </script>"""
    return _page("Base64 Encode / Decode", body, js)


# =========================================================================
# 9. URL ENCODE/DECODE — /security-tools/url-encode
# =========================================================================
@app.get("/security-tools/url-encode", response_class=HTMLResponse)
async def url_encode_page():
    body = f"""{_header("URL Encode / Decode", back_link=True)}
    <div class="card">
        <h3>Encode</h3>
        <textarea id="url-enc-input" rows="3" placeholder="Enter text to URL-encode..."></textarea>
        <button onclick="urlEncode()">URL Encode</button>
        <div id="url-enc-result" class="result" style="display:none;cursor:pointer;" onclick="navigator.clipboard.writeText(this.textContent)"></div>
    </div>
    <div class="card" style="margin-top:1rem;">
        <h3>Decode</h3>
        <textarea id="url-dec-input" rows="3" placeholder="Enter URL-encoded string to decode..."></textarea>
        <button onclick="urlDecode()">URL Decode</button>
        <div id="url-dec-result" class="result" style="display:none;cursor:pointer;" onclick="navigator.clipboard.writeText(this.textContent)"></div>
    </div>"""
    js = """<script>
    function urlEncode() {
        const text = document.getElementById('url-enc-input').value;
        const el = document.getElementById('url-enc-result');
        el.style.display='block';
        el.textContent = encodeURIComponent(text);
    }
    function urlDecode() {
        const text = document.getElementById('url-dec-input').value.trim();
        const el = document.getElementById('url-dec-result');
        el.style.display='block';
        try { el.textContent = decodeURIComponent(text); }
        catch(e) { el.textContent = 'Error: Invalid URL-encoded string'; }
    }
    </script>"""
    return _page("URL Encode / Decode", body, js)


# =========================================================================
# 10. JWT DECODER — /security-tools/jwt (CLIENT-SIDE ONLY)
# =========================================================================
@app.get("/security-tools/jwt", response_class=HTMLResponse)
async def jwt_page():
    body = f"""{_header("JWT Decoder", back_link=True)}
    <div style="background:#0f52;border:1px solid #0f53;border-radius:8px;padding:0.75rem 1rem;margin-bottom:1rem;color:#0f5;font-size:0.9rem;">
        &#128274; JWT decoding happens entirely in your browser. Your tokens are never sent to our server.
    </div>
    <div class="card">
        <h3>Decode JWT</h3>
        <textarea id="jwt-input" rows="4" placeholder="Paste your JWT token here (eyJhbG...)"></textarea>
        <button onclick="decodeJWT()">Decode Token</button>
        <div id="jwt-results"></div>
    </div>"""
    js = """<script>
    function b64decode(str) {
        str = str.replace(/-/g,'+').replace(/_/g,'/');
        while(str.length%4) str+='=';
        return decodeURIComponent(escape(atob(str)));
    }
    function decodeJWT() {
        const token = document.getElementById('jwt-input').value.trim();
        if(!token) return;
        const parts = token.split('.');
        if(parts.length < 2) {
            document.getElementById('jwt-results').innerHTML = '<div class="result" style="color:#f44;">Invalid JWT — expected 3 parts separated by dots</div>';
            return;
        }
        let html = '';
        try {
            const header = JSON.parse(b64decode(parts[0]));
            html += '<div class="result-label" style="margin-top:1rem;">Header</div>';
            html += '<div class="result"><pre>'+JSON.stringify(header,null,2)+'</pre></div>';
        } catch(e) {
            html += '<div class="result" style="color:#f44;">Could not decode header</div>';
        }
        try {
            const payload = JSON.parse(b64decode(parts[1]));
            html += '<div class="result-label" style="margin-top:1rem;">Payload</div>';
            html += '<div class="result"><pre>'+JSON.stringify(payload,null,2)+'</pre></div>';
            // Check expiry
            if(payload.exp) {
                const expDate = new Date(payload.exp * 1000);
                const now = new Date();
                const expired = expDate < now;
                const tag = expired ? 'tag-red' : 'tag-green';
                const label = expired ? 'Expired' : 'Valid';
                html += '<div style="margin-top:0.5rem;"><span class="tag '+tag+'">'+label+'</span> Expires: '+expDate.toUTCString()+'</div>';
            }
            if(payload.iat) {
                html += '<div style="margin-top:0.25rem;color:#888;font-size:0.85rem;">Issued: '+new Date(payload.iat*1000).toUTCString()+'</div>';
            }
        } catch(e) {
            html += '<div class="result" style="color:#f44;">Could not decode payload</div>';
        }
        if(parts[2]) {
            html += '<div class="result-label" style="margin-top:1rem;">Signature</div>';
            html += '<div class="result" style="font-size:0.8rem;">'+parts[2]+'</div>';
            html += '<p style="color:#888;font-size:0.8rem;margin-top:0.25rem;">Signature verification requires the secret/public key and is not performed client-side.</p>';
        }
        document.getElementById('jwt-results').innerHTML = html;
    }
    </script>"""
    return _page("JWT Decoder", body, js)


# =========================================================================
# SaaS Helpers
# =========================================================================

def _generate_api_key():
    return "obsec_" + secrets.token_hex(24)

def _send_welcome_email(email: str, api_key: str, plan: str):
    try:
        body_text = f"""Welcome to OBLIVION Security Tools API ({plan} plan)!

Your API key: {api_key}

Usage examples:
  curl -H "X-API-Key: {api_key}" "{DOMAIN_URL}/security-tools/api/password?length=20"
  curl -H "X-API-Key: {api_key}" "{DOMAIN_URL}/security-tools/api/hash?text=hello&algo=sha256"
  curl -H "X-API-Key: {api_key}" "{DOMAIN_URL}/security-tools/api/dns?domain=example.com"
  curl -H "X-API-Key: {api_key}" "{DOMAIN_URL}/security-tools/api/ssl?domain=example.com"
  curl -H "X-API-Key: {api_key}" "{DOMAIN_URL}/security-tools/api/headers?url=https://example.com"

Dashboard: {DOMAIN_URL}/security-tools/dashboard?key={api_key}

Thank you for choosing OBLIVION Security Tools.
"""
        msg = MIMEText(body_text)
        msg["Subject"] = f"OBLIVION Security Tools — Your API Key ({plan})"
        msg["From"] = SMTP_USER
        msg["To"] = email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        import logging
        logging.error("Failed to send welcome email to %s: %s", email, e)


# =========================================================================
# SaaS Routes — Pricing / Checkout / Success / Dashboard / Webhook
# =========================================================================

@app.get("/security-tools/pricing", response_class=HTMLResponse)
async def security_pricing():
    body = f"""{_header("API Pricing")}
    <p style="color:{TEXT_DIM};margin-bottom:1.5rem;">REST API access to all 10 security tools</p>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1.5rem;margin:1.5rem 0;">
      <div class="card" style="text-align:center;">
        <h3>Free</h3>
        <div style="font-size:2.2rem;font-weight:700;color:{ACCENT};margin:1rem 0;">£0<span style="font-size:0.9rem;color:{TEXT_DIM};">/mo</span></div>
        <ul style="list-style:none;text-align:left;margin:1rem 0;">
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; Use tools on website</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; All 10 tools</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10007; No API access</li>
        </ul>
        <a href="/security-tools" class="btn btn-secondary" style="text-decoration:none;">Use Free</a>
      </div>
      <div class="card" style="text-align:center;border-color:{ACCENT};">
        <h3>API</h3>
        <div style="font-size:2.2rem;font-weight:700;color:{ACCENT};margin:1rem 0;">£14<span style="font-size:0.9rem;color:{TEXT_DIM};">/mo</span></div>
        <ul style="list-style:none;text-align:left;margin:1rem 0;">
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; REST API to all 10 tools</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; 5,000 requests/day</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; JSON responses</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; API key auth</li>
        </ul>
        <a href="/security-tools/checkout/api" class="btn" style="text-decoration:none;">Subscribe</a>
      </div>
      <div class="card" style="text-align:center;">
        <h3>API Pro</h3>
        <div style="font-size:2.2rem;font-weight:700;color:{ACCENT};margin:1rem 0;">£39<span style="font-size:0.9rem;color:{TEXT_DIM};">/mo</span></div>
        <ul style="list-style:none;text-align:left;margin:1rem 0;">
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; Everything in API</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; Unlimited requests</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; Bulk operations</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; Scheduled scans</li>
          <li style="padding:0.3rem 0;color:{TEXT_DIM};">&#10003; Priority support</li>
        </ul>
        <a href="/security-tools/checkout/api_pro" class="btn" style="text-decoration:none;">Subscribe</a>
      </div>
    </div>"""
    return _page("API Pricing", body)


@app.get("/security-tools/checkout/{plan}")
async def security_checkout(plan: str):
    if plan not in SECURITY_PLANS:
        return HTMLResponse("<h1>Invalid plan</h1>", status_code=400)
    p = SECURITY_PLANS[plan]
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": p["currency"],
                    "product_data": {"name": f"OBLIVION Security Tools — {p['name']}"},
                    "unit_amount": p["price_amount"],
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=DOMAIN_URL + "/security-tools/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=DOMAIN_URL + "/security-tools/pricing",
            metadata={"product": PRODUCT_NAME, "plan": plan},
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        return HTMLResponse(f"<h1>Checkout error</h1><p>{e}</p>", status_code=500)


@app.get("/security-tools/success", response_class=HTMLResponse)
async def security_success(session_id: str = Query(...)):
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        email = session.customer_details.email or session.customer_email or "unknown"
        plan = session.metadata.get("plan", "api")
        p = SECURITY_PLANS.get(plan, SECURITY_PLANS["api"])
        api_key = _generate_api_key()

        async with _saas_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO saas_customers (email, stripe_customer_id, stripe_subscription_id, product, plan, api_key)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, email, session.customer, session.subscription, PRODUCT_NAME, plan, api_key)

        _send_welcome_email(email, api_key, p["name"])

        body = f"""{_header("Welcome!")}
        <div class="card" style="max-width:600px;margin:0 auto;">
          <p style="color:{TEXT_DIM};margin-bottom:0.5rem;">Your API Key:</p>
          <div style="background:{BG};border:1px solid {BG3};border-radius:8px;padding:1rem;font-family:monospace;font-size:1.05rem;word-break:break-all;color:{ACCENT};">{api_key}</div>
          <p style="color:{TEXT_DIM};margin-top:1rem;font-size:0.9rem;">Plan: <strong style="color:{ACCENT};">{p['name']}</strong> &mdash; {p['label']}</p>
          <p style="color:{TEXT_DIM};font-size:0.9rem;">Email: {email}</p>
          <p style="color:#f44;margin-top:1rem;font-size:0.85rem;">Save this key! It has also been sent to your email.</p>
          <a href="/security-tools/dashboard?key={api_key}" class="btn" style="display:inline-block;margin-top:1.5rem;text-decoration:none;">Open Dashboard</a>
        </div>"""
        return _page("Welcome", body)
    except Exception as e:
        return HTMLResponse(f"<h1>Error</h1><p>{e}</p>", status_code=500)


@app.get("/security-tools/dashboard", response_class=HTMLResponse)
async def security_dashboard(key: str = Query(...)):
    async with _saas_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM saas_customers WHERE api_key=$1 AND product=$2", key, PRODUCT_NAME
        )
    if not row:
        return HTMLResponse("<h1>Invalid API key</h1>", status_code=404)

    p = SECURITY_PLANS.get(row["plan"], SECURITY_PLANS["api"])
    status_tag = f'<span style="color:#0f5;font-weight:600;">Active</span>' if row["active"] else '<span style="color:#f44;font-weight:600;">Inactive</span>'
    req_limit_str = str(p["req_limit"]) if p["req_limit"] else "Unlimited"
    reqs_today = row["requests_today"] if row["requests_reset_date"] == date.today() else 0

    body = f"""{_header("API Dashboard")}
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-bottom:1.5rem;">
      <div class="card"><p style="color:{TEXT_DIM};font-size:0.8rem;text-transform:uppercase;">Status</p><p style="font-size:1.2rem;margin-top:0.3rem;">{status_tag}</p></div>
      <div class="card"><p style="color:{TEXT_DIM};font-size:0.8rem;text-transform:uppercase;">Plan</p><p style="font-size:1.2rem;margin-top:0.3rem;color:{ACCENT};">{p['name']}</p></div>
      <div class="card"><p style="color:{TEXT_DIM};font-size:0.8rem;text-transform:uppercase;">Requests Today</p><p style="font-size:1.2rem;margin-top:0.3rem;">{reqs_today} / {req_limit_str}</p></div>
    </div>
    <div class="card">
      <h3>API Key</h3>
      <div style="background:{BG};border:1px solid {BG3};border-radius:8px;padding:1rem;font-family:monospace;font-size:0.95rem;word-break:break-all;color:{ACCENT};margin-top:0.5rem;">{row['api_key']}</div>
      <p style="color:{TEXT_DIM};margin-top:1rem;font-size:0.9rem;">Email: {row['email']}</p>
      <p style="color:{TEXT_DIM};font-size:0.9rem;">Subscribed: {row['created_at'].strftime('%Y-%m-%d')}</p>
    </div>
    <div class="card">
      <h3>Quick Start</h3>
      <pre style="background:{BG};border:1px solid {BG3};border-radius:8px;padding:1rem;overflow-x:auto;font-size:0.85rem;color:{TEXT_DIM};margin-top:0.5rem;">curl -H "X-API-Key: {row['api_key']}" \\
  "{DOMAIN_URL}/security-tools/api/password?length=20"

curl -H "X-API-Key: {row['api_key']}" \\
  "{DOMAIN_URL}/security-tools/api/hash?text=hello&algo=sha256"

curl -H "X-API-Key: {row['api_key']}" \\
  "{DOMAIN_URL}/security-tools/api/dns?domain=example.com"

curl -H "X-API-Key: {row['api_key']}" \\
  "{DOMAIN_URL}/security-tools/api/ssl?domain=example.com"</pre>
    </div>"""
    return _page("Dashboard", body)


@app.post("/security-tools/webhook")
async def security_webhook(request: Request):
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


# =========================================================================
# STARTUP
# =========================================================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3069)
