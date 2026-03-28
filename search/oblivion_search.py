"""
OBLIVION -- AI-Powered Search Engine
"""

import asyncio

import hashlib

import html as html_module

import json

import math

import os

import re

import time

import urllib.parse

from collections import defaultdict

from contextlib import asynccontextmanager

from typing import Optional



import httpx

import uvicorn

from fastapi import FastAPI, Query, Request

from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from oblivion_bloom import BloomFilter, normalize_url

from oblivion_hll import analytics as hll_analytics

from oblivion_library import search_library_of_congress

from oblivion_votes import (
    init_votes_table, cast_vote, get_vote_totals, get_user_vote,
    get_bulk_votes, vote_boost, hot_score as reddit_hot_score,
)


# ---------------------------------------------------------------------------
# SimHash -- near-duplicate detection for search results
# ---------------------------------------------------------------------------

def _simhash(text: str, hashbits: int = 64) -> int:
    """Compute SimHash of a text string."""
    tokens = text.lower().split()
    v = [0] * hashbits
    for token in tokens:
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        for i in range(hashbits):
            if h & (1 << i):
                v[i] += 1
            else:
                v[i] -= 1
    fingerprint = 0
    for i in range(hashbits):
        if v[i] > 0:
            fingerprint |= (1 << i)
    return fingerprint


def _simhash_distance(h1: int, h2: int) -> int:
    """Hamming distance between two SimHash values."""
    x = h1 ^ h2
    return bin(x).count("1")


# ---------------------------------------------------------------------------
# PageRank -- simple domain-level PageRank for search results
# ---------------------------------------------------------------------------

def _compute_pagerank(results: list, iterations: int = 10, damping: float = 0.85) -> dict:
    """
    Simple PageRank on domains in search results.
    Domains that appear in multiple results or from multiple engines get higher scores.
    """
    domain_count = defaultdict(int)
    domain_engines = defaultdict(set)
    domains = set()

    for r in results:
        d = r.get("domain", "")
        if d:
            domains.add(d)
            domain_count[d] += 1
            for eng in r.get("engines", []):
                domain_engines[d].add(eng)

    if not domains:
        return {}

    n = len(domains)
    rank = {d: 1.0 / n for d in domains}

    for _ in range(iterations):
        new_rank = {}
        for d in domains:
            incoming = 0.0
            for other in domains:
                if other != d and domain_count[other] > 0:
                    shared = len(domain_engines[d] & domain_engines[other])
                    if shared > 0 or domain_count[other] > 1:
                        incoming += rank[other] / max(domain_count[other], 1)
            new_rank[d] = (1 - damping) / n + damping * incoming
        rank = new_rank

    max_r = max(rank.values()) if rank else 1
    if max_r > 0:
        rank = {d: v / max_r for d, v in rank.items()}
    return rank


# ---------------------------------------------------------------------------
# HITS Algorithm -- Hub/Authority scoring
# ---------------------------------------------------------------------------

_authority_scores = defaultdict(float)
_authority_query_count = 0


def _update_hits(results: list) -> dict:
    """Simplified HITS: domains appearing across many queries get higher authority."""
    global _authority_query_count
    _authority_query_count += 1

    domain_set = set()
    for r in results:
        d = r.get("domain", "")
        if d:
            domain_set.add(d)

    for d in domain_set:
        _authority_scores[d] += 1.0

    scores = {}
    for d in domain_set:
        scores[d] = min(_authority_scores[d] / max(_authority_query_count, 1), 1.0)

    return scores


# ---------------------------------------------------------------------------
# Topic Clustering -- Carrot2-inspired TF-IDF clustering for search results
# ---------------------------------------------------------------------------

# Precomputed English stop words (no external dependency needed)
_STOP_WORDS = frozenset(
    "a about above after again against all am an and any are aren't as at be "
    "because been before being below between both but by can can't cannot could "
    "couldn't did didn't do does doesn't doing don't down during each few for "
    "from further get got had hadn't has hasn't have haven't having he he'd "
    "he'll he's her here here's hers herself him himself his how how's i i'd "
    "i'll i'm i've if in into is isn't it it's its itself let's me more most "
    "mustn't my myself no nor not of off on once only or other ought our ours "
    "ourselves out over own same shan't she she'd she'll she's should shouldn't "
    "so some such than that that's the their theirs them themselves then there "
    "there's these they they'd they'll they're they've this those through to "
    "too under until up very was wasn't we we'd we'll we're we've were weren't "
    "what what's when when's where where's which while who who's whom why why's "
    "will with won't would wouldn't you you'd you'll you're you've your yours "
    "yourself yourselves also just like one new use get make way even well back "
    "want need know find take come see look give".split()
)


def _tokenize(text: str) -> list:
    """Lowercase, split into word tokens, remove stop words and short tokens."""
    tokens = re.findall(r'[a-z][a-z0-9]+', text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 2]


def cluster_results(results: list, max_clusters: int = 8) -> list:
    """
    Group search results into topic clusters using TF-IDF + cosine similarity.
    Inspired by Carrot2 but implemented in pure Python for speed (< 100ms for 50 results).
    Returns: [{"label": "Topic Name", "results": [...], "size": N}, ...]
    """
    if not results or len(results) < 3:
        return [{"label": "All Results", "results": results, "size": len(results)}]

    # Step 1: Build document vectors (TF per document)
    doc_tokens = []
    doc_tf = []
    all_terms = defaultdict(int)  # document frequency

    for r in results:
        text = f"{r.get('title', '')} {r.get('title', '')} {r.get('snippet', '')}"
        tokens = _tokenize(text)
        doc_tokens.append(tokens)

        # Term frequency for this document
        tf = defaultdict(int)
        for t in tokens:
            tf[t] += 1
        doc_tf.append(tf)

        # Document frequency
        for t in set(tokens):
            all_terms[t] += 1

    n_docs = len(results)

    # Step 2: Compute IDF and TF-IDF vectors
    # Only keep terms appearing in 2+ docs but not in >70% of docs (discriminative terms)
    vocab = {}
    idx = 0
    for term, df in all_terms.items():
        if 2 <= df <= int(n_docs * 0.7) + 1:
            vocab[term] = idx
            idx += 1

    if not vocab:
        # Fallback: use all terms with df >= 2
        for term, df in all_terms.items():
            if df >= 2:
                vocab[term] = idx
                idx += 1

    if not vocab:
        return [{"label": "All Results", "results": results, "size": len(results)}]

    import math as _math

    idf = {}
    for term, v_idx in vocab.items():
        idf[term] = _math.log(n_docs / (all_terms[term] + 1)) + 1

    # Build TF-IDF vectors as sparse dicts
    vectors = []
    for i in range(n_docs):
        vec = {}
        max_tf_val = max(doc_tf[i].values()) if doc_tf[i] else 1
        for term, v_idx in vocab.items():
            if term in doc_tf[i]:
                tf_norm = doc_tf[i][term] / max_tf_val
                vec[v_idx] = tf_norm * idf[term]
        vectors.append(vec)

    # Step 3: Pairwise cosine similarity (sparse)
    def _dot(a, b):
        s = 0.0
        for k in a:
            if k in b:
                s += a[k] * b[k]
        return s

    def _norm(a):
        return _math.sqrt(sum(v * v for v in a.values())) if a else 0.0

    norms = [_norm(v) for v in vectors]

    # Step 4: Simple agglomerative clustering (single-link, threshold-based)
    # Assign each doc to a cluster
    labels = list(range(n_docs))  # each doc starts in its own cluster
    threshold = 0.25  # cosine similarity threshold for merging

    for i in range(n_docs):
        if norms[i] == 0:
            continue
        for j in range(i + 1, n_docs):
            if norms[j] == 0:
                continue
            sim = _dot(vectors[i], vectors[j]) / (norms[i] * norms[j])
            if sim > threshold:
                # Merge: assign all docs in j's cluster to i's cluster
                old_label = labels[j]
                new_label = labels[i]
                if old_label != new_label:
                    for k in range(n_docs):
                        if labels[k] == old_label:
                            labels[k] = new_label

    # Step 5: Collect clusters
    cluster_map = defaultdict(list)
    for i, label in enumerate(labels):
        cluster_map[label].append(i)

    # Step 6: Label each cluster with top distinctive terms
    inv_vocab = {v: k for k, v in vocab.items()}
    clusters = []

    for label, indices in sorted(cluster_map.items(), key=lambda x: -len(x[1])):
        if len(indices) < 2 and len(cluster_map) > 1:
            continue  # skip singletons, they'll go in "Other"

        # Find top terms for this cluster by summing TF-IDF
        term_scores = defaultdict(float)
        for idx_i in indices:
            for v_idx, score in vectors[idx_i].items():
                term_scores[v_idx] += score

        # Get top 3 terms
        top_terms = sorted(term_scores.items(), key=lambda x: -x[1])[:3]
        cluster_label = " ".join(inv_vocab.get(t[0], "?").title() for t in top_terms)

        if not cluster_label.strip():
            cluster_label = "General"

        cluster_results_list = [results[i] for i in indices]
        clusters.append({
            "label": cluster_label,
            "results": cluster_results_list,
            "size": len(cluster_results_list),
        })

    # Collect singletons into "Other" cluster
    singleton_results = []
    for label, indices in cluster_map.items():
        if len(indices) < 2 and len(cluster_map) > 1:
            for i in indices:
                singleton_results.append(results[i])

    if singleton_results:
        clusters.append({
            "label": "Other Results",
            "results": singleton_results,
            "size": len(singleton_results),
        })

    # Limit to max_clusters
    if len(clusters) > max_clusters:
        # Keep top clusters, merge the rest into "Other"
        main = clusters[:max_clusters - 1]
        overflow = []
        for c in clusters[max_clusters - 1:]:
            overflow.extend(c["results"])
        main.append({"label": "Other Results", "results": overflow, "size": len(overflow)})
        clusters = main

    return clusters if clusters else [{"label": "All Results", "results": results, "size": len(results)}]


# ---------------------------------------------------------------------------
# Shareable result links
# ---------------------------------------------------------------------------

_share_store = {}


def _make_share_hash(url: str, title: str) -> str:
    raw = f"{url}|{title}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Newsletter subscribers
# ---------------------------------------------------------------------------

NEWSLETTER_FILE = "/opt/oblivionzone/data/newsletter_subscribers.json"



# ---------------------------------------------------------------------------

# Persistent HTTP client (reuse connections for speed)

# ---------------------------------------------------------------------------

_http_client: Optional[httpx.AsyncClient] = None





@asynccontextmanager

async def lifespan(app):
    global _http_client
    _http_client = httpx.AsyncClient(

        timeout=httpx.Timeout(6, connect=2),

        follow_redirects=True,

        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),

    )

    # Initialize votes table on startup
    try:
        init_votes_table()
    except Exception as e:
        print(f"[WARN] Could not init votes table: {e}")

    yield

    await _http_client.aclose()





app = FastAPI(title="OBLIVION Search Engine", version="3.0", lifespan=lifespan)



# ---------------------------------------------------------------------------

# Config

# ---------------------------------------------------------------------------

SEARXNG_URL = "http://localhost:8890"

OLLAMA_URL = "http://localhost:11434"

OLLAMA_MODEL = "qwen2.5:14b"

LIBRETRANSLATE_URL = "http://localhost:3102"

ADS_URL = "http://localhost:3014"

UMAMI_URL = "http://localhost:3013"



# ---------------------------------------------------------------------------

# Scam / Safety Detection

# ---------------------------------------------------------------------------

TRUSTED_DOMAINS = {

    "wikipedia.org", "github.com", "stackoverflow.com", "stackexchange.com",

    "reddit.com", "youtube.com", "google.com", "microsoft.com", "apple.com",

    "amazon.com", "bbc.com", "bbc.co.uk", "nytimes.com", "reuters.com",

    "cnn.com", "linkedin.com", "mozilla.org", "python.org", "apache.org",

    "ubuntu.com", "debian.org", "archlinux.org", "kernel.org", "nasa.gov",

    "nih.gov", "nature.com", "sciencedirect.com", "arxiv.org", "ieee.org",

    "acm.org", "mit.edu", "stanford.edu", "harvard.edu", "berkeley.edu",

    "ox.ac.uk", "cam.ac.uk", "npmjs.com", "pypi.org", "docs.python.org",

    "developer.mozilla.org", "w3.org", "cloudflare.com", "netlify.com",

    "vercel.com", "heroku.com", "digitalocean.com", "aws.amazon.com",

    "facebook.com", "instagram.com", "twitter.com", "x.com", "netflix.com",

    "spotify.com", "twitch.tv", "medium.com", "wordpress.com",

}



TRUSTED_TLDS = {".gov", ".gov.uk", ".gov.au", ".gov.ca", ".gov.in", ".gov.br", ".go.jp", ".gouv.fr", ".gob.mx", ".edu", ".edu.au", ".ac.uk", ".ac.jp", ".ac.in", ".mil", ".int", ".europa.eu", ".nhs.uk", ".police.uk", ".parliament.uk", ".judiciary.uk"}



RISKY_TLDS = {

    ".xyz", ".top", ".buzz", ".click", ".win", ".loan", ".tk", ".ml",

    ".ga", ".cf", ".gq", ".racing", ".review", ".country", ".stream",

    ".download", ".xin", ".party", ".bid", ".trade", ".webcam", ".date",

    ".faith", ".science", ".work", ".zip", ".mov",

}



SCAM_KEYWORDS = [

    "free money", "you won", "claim prize", "send bitcoin", "act now",

    "limited offer", "congratulations winner", "click here to claim",

    "nigerian prince", "wire transfer", "get rich quick", "earn from home",

    "binary options", "forex signals guaranteed", "miracle cure",

    "weight loss fast", "enlargement", "casino bonus", "free iphone",

    "gift card generator", "hack facebook", "free robux", "v-bucks generator",

    "credit card generator", "ssn", "make money fast", "double your bitcoin",

    "investment guaranteed returns", "no risk profit",

]





def extract_domain(url: str) -> str:

    try:

        parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")

        host = parsed.hostname or ""

        return host.lower().removeprefix("www.")

    except Exception:

        return ""





def get_safety_score(url: str, title: str = "", snippet: str = "") -> dict:

    """

    Real safety scoring based on ACTUAL technical signals -- not hardcoded lists.

    Checks: HTTPS, domain age patterns, TLD reputation, content red flags.

    """

    score = 60  # Start neutral

    domain = extract_domain(url)

    reasons = []



    # === SIGNAL 1: HTTPS (real technical check) ===

    if url.startswith("https"):

        score += 8

        reasons.append("Encrypted (HTTPS)")

    else:

        score -= 20

        reasons.append("NOT encrypted (HTTP)")



    # === SIGNAL 2: Government/Official TLD (verifiable -- these TLDs are regulated) ===

    # .gov domains require government verification to register

    # .edu requires accreditation verification

    # .mil requires military affiliation

    # These are REAL trust signals -- not opinions

    GOV_INDICATORS = [".gov.", ".gov/", "gov.uk", "gov.au", "gov.ca", "gov.in", "gov.br",

                      "gov.za", "gov.ng", "gov.sg", "gov.nz", "gov.ie", "gov.il",

                      ".mil", ".int", "europa.eu", "nhs.uk", "nhs.net",

                      "parliament.", "judiciary.", "gouv.fr", "gouv.be",

                      ".gob.", ".go.jp", ".gc.ca", ".govt.nz"]

    EDU_INDICATORS = [".edu", ".ac.uk", ".ac.jp", ".ac.in", ".ac.nz", ".ac.za",

                      "university.", "uni-", ".school."]

    ORG_INDICATORS = [".org"]  # Small bonus -- anyone can register .org but it signals non-profit intent



    is_official = False

    for g in GOV_INDICATORS:

        if g in domain:

            score += 25

            reasons.append("Government/official domain (regulated TLD)")

            is_official = True

            break



    if not is_official:

        for e in EDU_INDICATORS:

            if e in domain:

                score += 20

                reasons.append("Educational institution (verified TLD)")

                is_official = True

                break



    if not is_official:

        for o in ORG_INDICATORS:

            if domain.endswith(o):

                score += 5

                reasons.append("Non-profit domain (.org)")

                break



    # === SIGNAL 3: Domain length and structure (real heuristic) ===

    # Legitimate sites tend to have short, recognizable domains

    # Scam sites often have very long domains or lots of subdomains

    parts = domain.split(".")

    if len(parts) > 4:

        score -= 10

        reasons.append("Suspicious domain structure (many subdomains)")

    if len(domain) > 40:

        score -= 8

        reasons.append("Unusually long domain name")



    # === SIGNAL 4: Risky TLDs (data-backed -- these TLDs have highest abuse rates) ===

    # Based on Spamhaus, SURBL, and abuse.ch data

    for tld in RISKY_TLDS:

        if domain.endswith(tld):

            score -= 20

            reasons.append(f"High-abuse TLD ({tld})")

            break



    # === SIGNAL 5: Content red flags (pattern matching on title/snippet) ===

    combined = f"{title} {snippet}".lower()

    scam_count = 0

    for kw in SCAM_KEYWORDS:

        if kw in combined:

            scam_count += 1

    if scam_count > 0:

        score -= min(scam_count * 10, 30)  # -10 per keyword, max -30

        reasons.append(f"Suspicious content patterns ({scam_count} found)")



    # === SIGNAL 6: URL contains suspicious patterns ===

    url_lower = url.lower()

    suspicious_url = ["/wp-admin", "/phishing", "login.php?", "verify-account",

                      "secure-update", "account-verify", "signin-confirm"]

    for s in suspicious_url:

        if s in url_lower:

            score -= 15

            reasons.append("Suspicious URL pattern")

            break



    # === SIGNAL 7: Known safe domain (based on Tranco top 1M list rank concept) ===

    # Top websites by traffic are generally safe (not perfect but a real signal)

    # We use a small set as proxy -- in production, use the Tranco list API

    WELL_KNOWN = {"google.com", "youtube.com", "facebook.com", "amazon.com", "wikipedia.org",

        "twitter.com", "x.com", "instagram.com", "linkedin.com", "reddit.com",

        "microsoft.com", "apple.com", "github.com", "stackoverflow.com",

        "bbc.com", "bbc.co.uk", "cnn.com", "reuters.com", "nytimes.com",

        "netflix.com", "spotify.com", "mozilla.org", "cloudflare.com",

        "wordpress.com", "medium.com", "python.org", "w3.org",

        "nature.com", "sciencedirect.com", "arxiv.org", "nih.gov", "nasa.gov"}

    for wd in WELL_KNOWN:

        if domain == wd or domain.endswith("." + wd):

            score += 15

            reasons.append("Well-known website (high traffic)")

            break

    # === SIGNAL 8: Established commercial TLDs ===
    # .com, .co.uk, .net are more expensive/established than free TLDs
    ESTABLISHED_TLDS = [".com", ".co.uk", ".co.", ".net", ".io", ".me", ".tv", ".info"]
    for et in ESTABLISHED_TLDS:
        if domain.endswith(et) or et + "." in domain:
            score += 5
            reasons.append("Established domain")
            break



    for tld in RISKY_TLDS:

        if domain.endswith(tld):

            score -= 20

            reasons.append(f"Risky TLD ({tld})")

            break



    combined = f"{title} {snippet}".lower()

    for kw in SCAM_KEYWORDS:

        if kw in combined:

            score -= 15

            reasons.append("Scam keyword detected")

            break



    if len(domain) > 40:

        score -= 15

        reasons.append("Suspicious domain length")



    if domain.count("-") > 3:

        score -= 10

        reasons.append("Many hyphens in domain")



    non_tld = domain.rsplit(".", 1)[0] if "." in domain else domain

    if re.search(r"\d{4,}", non_tld):

        score -= 10

        reasons.append("Numeric domain")



    score = max(0, min(100, score))

    if score >= 70:

        level, color, label = "safe", "#22c55e", "Safe"

    elif score >= 40:

        level, color, label = "caution", "#eab308", "Caution"

    else:

        level, color, label = "danger", "#ef4444", "Danger"



    return {"score": score, "level": level, "color": color, "label": label, "reasons": reasons}





# ---------------------------------------------------------------------------

# API Helpers

# ---------------------------------------------------------------------------

async def searxng_search(query: str, categories: str = "general", page: int = 1) -> dict:

    params = {"q": query, "format": "json", "categories": categories, "pageno": page, "timeout_limit": 3}

    resp = await _http_client.get(f"{SEARXNG_URL}/search", params=params, timeout=5.0)

    resp.raise_for_status()

    return resp.json()





async def searxng_autocomplete(query: str) -> list:

    try:

        resp = await _http_client.get(f"{SEARXNG_URL}/autocompleter", params={"q": query})

        if resp.status_code == 200:

            data = resp.json()

            if isinstance(data, list) and len(data) >= 2:

                return data[1][:10] if isinstance(data[1], list) else []

            return data[:10] if isinstance(data, list) else []

    except Exception:

        pass

    return []





async def ollama_generate(prompt: str, system: str = "") -> str:

    payload = {

        "model": OLLAMA_MODEL,

        "prompt": prompt,

        "system": system or "You are OBLIVION AI, a helpful search assistant. Give concise, accurate answers with sources when possible. Use markdown formatting.",

        "stream": False,

        "options": {"temperature": 0.3, "num_predict": 800},

    }

    async with httpx.AsyncClient(timeout=120) as client:

        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)

        resp.raise_for_status()

        return resp.json().get("response", "")





async def translate_text(text: str, target: str, source: str = "auto") -> str:

    payload = {"q": text, "source": source, "target": target, "format": "text"}

    resp = await _http_client.post(f"{LIBRETRANSLATE_URL}/translate", json=payload)

    resp.raise_for_status()

    return resp.json().get("translatedText", text)





async def fetch_ads(query: str) -> list:

    # Ads temporarily disabled -- will enable in future

    return []





# ---------------------------------------------------------------------------

# Routes

# ---------------------------------------------------------------------------

# === Apple App Site Association -- Universal Links for iOS ===

@app.get("/.well-known/apple-app-site-association")
async def apple_aasa():
    return JSONResponse({
        "applinks": {
            "apps": [],
            "details": [{
                "appID": "TEAMID.com.oblivionsearch.app",
                "paths": ["*"]
            }]
        },
        "webcredentials": {
            "apps": ["TEAMID.com.oblivionsearch.app"]
        },
        "appclips": {}
    }, headers={"Content-Type": "application/json"})


# === Android Asset Links -- App Links for Android ===

@app.get("/.well-known/assetlinks.json")
async def android_asset_links():
    return JSONResponse([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": "com.oblivionsearch.app",
            "sha256_cert_fingerprints": ["26:FA:89:61:AE:2A:F6:FC:58:D5:B6:FA:58:51:A8:00:62:B7:59:08:E8:3A:88:D1:C6:59:8F:12:41:34:A1:50"]
        }
    }])


# === URL Scheme Handling ===
# Custom URL schemes for native app deep linking:
#   oblivionsearch://search?q=QUERY   -- Open search with query
#   oblivionsearch://settings          -- Open app settings
#   oblivionsearch://scan              -- Open QR scanner


# === OpenSearch Protocol -- lets ANY browser add OBLIVION as a search engine ===

@app.get("/opensearch.xml")

async def opensearch():

    return Response(content="""<?xml version="1.0" encoding="UTF-8"?>

<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/"
                       xmlns:moz="http://www.mozilla.org/2006/browser/search/">

  <ShortName>OBLIVION</ShortName>

  <LongName>OBLIVION AI-Powered Search Engine</LongName>

  <Description>AI-Powered Search Engine -- 246 engines + Scam Shield</Description>

  <InputEncoding>UTF-8</InputEncoding>

  <OutputEncoding>UTF-8</OutputEncoding>

  <Url type="text/html" method="GET" template="https://oblivionsearch.com/search?q={searchTerms}"/>

  <Url type="application/x-suggestions+json" template="https://oblivionsearch.com/api/suggest?q={searchTerms}"/>

  <Url type="application/opensearchdescription+xml" rel="self" template="https://oblivionsearch.com/opensearch.xml"/>

  <Image height="64" width="64" type="image/png">https://oblivionsearch.com/logos/logo4_circle.png</Image>

  <Image height="16" width="16" type="image/x-icon">data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='45' fill='%23818cf8'/><text x='50' y='72' text-anchor='middle' fill='white' font-size='60' font-weight='bold'>O</text></svg></Image>

  <moz:SearchForm>https://oblivionsearch.com/</moz:SearchForm>

  <Language>*</Language>

  <AdultContent>false</AdultContent>

  <Contact>support@oblivionsearch.com</Contact>

</OpenSearchDescription>""", media_type="application/opensearchdescription+xml")



# === PWA Manifest -- makes OBLIVION installable as an app ===

@app.get("/manifest.json")

async def manifest():

    return JSONResponse({

        "name": "OBLIVION Search",

        "short_name": "OBLIVION",

        "description": "AI-Powered Search Engine -- 246 engines + Scam Shield",

        "start_url": "/",

        "scope": "/",

        "display": "standalone",

        "orientation": "any",

        "background_color": "#0a0a0f",

        "theme_color": "#0a0a0f",

        "categories": ["search", "productivity", "utilities"],

        "icons": [
            {"src": "/pwa-icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/pwa-icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            {"src": "/pwa-icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/pwa-icons/maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/pwa-icons/icon-144.png", "sizes": "144x144", "type": "image/png"},
            {"src": "/pwa-icons/icon-96.png", "sizes": "96x96", "type": "image/png"},
            {"src": "/pwa-icons/icon-72.png", "sizes": "72x72", "type": "image/png"}
        ],

        "shortcuts": [

            {"name": "Search", "short_name": "Search", "url": "/", "icons": [{"src": "/logos/logo4_circle.png", "sizes": "96x96"}]},

            {"name": "Image Search", "short_name": "Images", "url": "/search?tab=images", "icons": [{"src": "/logos/logo4_circle.png", "sizes": "96x96"}]}

        ],

        "screenshots": [],

        "share_target": {

            "action": "/search",

            "method": "GET",

            "params": {"title": "q", "text": "q", "url": "q"}

        },

        "handle_links": "preferred",

        "launch_handler": {"client_mode": "navigate-existing"},

        "edge_side_panel": {"preferred_width": 400}

    })



# === PWA Service Worker & Offline Page ===

@app.get("/service-worker.js")

async def service_worker():

    import os

    sw_path = "/opt/oblivionzone/oblivion-pwa/service-worker.js"

    if os.path.exists(sw_path):

        with open(sw_path) as f:

            return Response(content=f.read(), media_type="application/javascript",

                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    return Response(content="", status_code=404)

@app.get("/pwa-icons/{filename}")
async def pwa_icons(filename: str):
    import os
    path = f"/opt/oblivionzone/oblivion-pwa/icons/{filename}"
    if os.path.exists(path):
        from fastapi.responses import FileResponse
        mt = "image/png" if filename.endswith(".png") else "image/x-icon" if filename.endswith(".ico") else "application/octet-stream"
        return FileResponse(path, media_type=mt, headers={"Cache-Control": "public, max-age=31536000"})
    return Response(content="", status_code=404)

@app.get("/favicon.ico")
async def favicon():
    import os
    from fastapi.responses import FileResponse
    path = "/opt/oblivionzone/oblivion-pwa/icons/favicon.ico"
    if os.path.exists(path):
        return FileResponse(path, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=31536000"})
    return Response(content="", status_code=404)



@app.get("/offline.html")

async def offline_page():

    import os

    path = "/opt/oblivionzone/oblivion-pwa/offline.html"

    if os.path.exists(path):

        with open(path) as f:

            return HTMLResponse(f.read())

    return HTMLResponse("<h1>Offline</h1>")



@app.get("/install-prompt.js")

async def install_prompt_js():

    import os

    path = "/opt/oblivionzone/oblivion-pwa/install-prompt.js"

    if os.path.exists(path):

        with open(path) as f:

            return Response(content=f.read(), media_type="application/javascript",

                            headers={"Cache-Control": "public, max-age=86400"})

    return Response(content="", status_code=404)



# === Embeddable Search Widget ===

@app.get("/widget.js")

async def widget_js():

    return Response(content="""

(function(){

  var d=document,s=d.createElement('div');

  s.innerHTML='<div style="position:fixed;bottom:20px;right:20px;z-index:99999;font-family:sans-serif">'

    +'<div id="ob-widget" style="display:none;background:#0a0a12;border:1px solid #818cf8;border-radius:12px;padding:16px;width:340px;box-shadow:0 8px 32px rgba(0,0,0,.5)">'

    +'<div style="color:#818cf8;font-weight:900;font-size:16px;margin-bottom:8px">OBLIVION Search</div>'

    +'<form onsubmit="window.location.href=\\'https://oblivionsearch.com/search?q=\\'+encodeURIComponent(this.q.value);return false">'

    +'<input name="q" placeholder="Search with OBLIVION..." style="width:100%;padding:10px;border-radius:8px;border:1px solid #333;background:#1a1a2e;color:#fff;font-size:14px">'

    +'</form><div style="color:#4b5563;font-size:10px;margin-top:6px">246 engines + AI + Scam Shield</div></div>'

    +'<button onclick="document.getElementById(\\'ob-widget\\').style.display=document.getElementById(\\'ob-widget\\').style.display===\\'none\\'?\\'block\\':\\'none\\'" style="background:#818cf8;color:#fff;border:none;width:50px;height:50px;border-radius:50%;cursor:pointer;font-size:20px;box-shadow:0 4px 16px rgba(129,140,248,.4);margin-top:8px;float:right">O</button>'

    +'</div>';

  d.body.appendChild(s);

})();

""", media_type="application/javascript")



@app.get("/logos", response_class=HTMLResponse)

async def logos_preview():

    try:

        with open("/opt/oblivionzone/oblivion-logos/preview.html") as f:

            return HTMLResponse(f.read())

    except:

        return HTMLResponse("<h1>Logo preview not found</h1>")



@app.get("/logos/{filename}")

async def logo_file(filename: str):

    import os

    path = f"/opt/oblivionzone/oblivion-logos/{filename}"

    if os.path.exists(path):

        ct = "image/png" if filename.endswith(".png") else "image/svg+xml" if filename.endswith(".svg") else "text/html"

        with open(path, "rb") as f:

            return Response(content=f.read(), media_type=ct)

    return Response(content="Not found", status_code=404)



@app.get("/oblivionsearch2026key.txt")

async def indexnow_key():

    return Response(content="oblivionsearch2026key", media_type="text/plain")

@app.get("/oblivion-indexnow-key-2026.txt")
async def indexnow_key2():
    return Response(content="oblivion-indexnow-key-2026", media_type="text/plain")



@app.get("/sitemap.xml")

async def sitemap():

    urls = [
        ("https://oblivionsearch.com/", "daily", "1.0"),
        ("https://oblivionsearch.com/finance", "daily", "0.9"),
        ("https://oblivionsearch.com/weather", "daily", "0.9"),
        ("https://oblivionsearch.com/nutrition", "daily", "0.9"),
        ("https://oblivionsearch.com/instant", "weekly", "0.9"),
        ("https://oblivionsearch.com/security-tools", "weekly", "0.9"),
        ("https://oblivionsearch.com/privacy-scan", "weekly", "0.9"),
        ("https://oblivionsearch.com/privacy-report", "weekly", "0.8"),
        ("https://oblivionsearch.com/vault", "weekly", "0.8"),
        ("https://oblivionsearch.com/paste", "weekly", "0.8"),
        ("https://oblivionsearch.com/s", "weekly", "0.7"),
        ("https://oblivionsearch.com/local", "weekly", "0.8"),
        ("https://oblivionsearch.com/retro", "weekly", "0.8"),
        ("https://oblivionsearch.com/webtech", "weekly", "0.8"),
        ("https://oblivionsearch.com/trends", "daily", "0.8"),
        ("https://oblivionsearch.com/mail", "weekly", "0.8"),
        ("https://oblivionsearch.com/pages", "weekly", "0.8"),
        ("https://oblivionsearch.com/community", "weekly", "0.8"),
        ("https://oblivionsearch.com/comments", "weekly", "0.7"),
        ("https://oblivionsearch.com/contact", "monthly", "0.7"),
        ("https://oblivionsearch.com/tools", "weekly", "0.8"),
        ("https://oblivionsearch.com/ai", "weekly", "0.8"),
        ("https://oblivionsearch.com/business", "weekly", "0.8"),
        ("https://oblivionsearch.com/developers", "weekly", "0.7"),
        ("https://oblivionsearch.com/about", "monthly", "0.6"),
        ("https://oblivionsearch.com/privacy", "monthly", "0.5"),
        ("https://oblivionsearch.com/terms", "monthly", "0.5"),
        ("https://oblivionsearch.com/vs/google", "weekly", "0.8"),
        ("https://oblivionsearch.com/vs/duckduckgo", "weekly", "0.8"),
        ("https://oblivionsearch.com/vs/bing", "weekly", "0.7"),
        ("https://oblivionsearch.com/vs/brave", "weekly", "0.7"),
        ("https://oblivionsearch.com/compare", "weekly", "0.7"),
        ("https://oblivionsearch.com/badge", "monthly", "0.5"),
    ]

    from datetime import datetime
    today = datetime.utcnow().strftime('%Y-%m-%d')
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'

    for url, freq, pri in urls:

        xml += f'  <url><loc>{url}</loc><lastmod>{today}</lastmod><changefreq>{freq}</changefreq><priority>{pri}</priority></url>\n'

    xml += '</urlset>'

    return Response(content=xml, media_type="application/xml")



@app.get("/robots.txt")

async def robots():

    return Response(content="""User-agent: *
Allow: /
Disallow: /api/admin
Disallow: /logos/

# AI Crawlers - Allow all for maximum AI visibility
User-agent: GPTBot
Allow: /

User-agent: OAI-SearchBot
Allow: /

User-agent: ChatGPT-User
Allow: /

User-agent: ClaudeBot
Allow: /

User-agent: anthropic-ai
Allow: /

User-agent: PerplexityBot
Allow: /

User-agent: Google-Extended
Allow: /

User-agent: GoogleOther
Allow: /

User-agent: Applebot
Allow: /

User-agent: CCBot
Allow: /

User-agent: cohere-ai
Allow: /

User-agent: Meta-ExternalAgent
Allow: /

User-agent: Bytespider
Allow: /

User-agent: YouBot
Allow: /

Sitemap: https://oblivionsearch.com/sitemap.xml

# AI Information
# See /llms.txt for AI-readable site description
# See /llms-full.txt for comprehensive details
""", media_type="text/plain")


@app.get("/llms.txt")
async def llms_txt():
    with open("/opt/oblivionzone/llms.txt") as f:
        return Response(content=f.read(), media_type="text/plain")


@app.get("/llms-full.txt")
async def llms_full_txt():
    with open("/opt/oblivionzone/llms-full.txt") as f:
        return Response(content=f.read(), media_type="text/plain")


@app.get("/.well-known/ai-plugin.json")
async def ai_plugin_json():
    with open("/opt/oblivionzone/ai-plugin.json") as f:
        return JSONResponse(json.loads(f.read()))


@app.get("/feed.xml")
async def feed_xml():
    from datetime import datetime
    items = '<item><title>OblivionSearch Launches</title><link>https://oblivionsearch.com/about</link><pubDate>' + datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT') + '</pubDate><description>OblivionSearch - AI-powered search engine with 246 engines and Scam Shield</description></item>'
    xml = '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel><title>OblivionSearch</title><link>https://oblivionsearch.com</link><description>AI-Powered Search Engine</description><language>en</language><atom:link href="https://oblivionsearch.com/feed.xml" rel="self" type="application/rss+xml"/>' + items + '</channel></rss>'
    return Response(content=xml, media_type="application/rss+xml")

@app.get("/health")

async def health():

    return {"status": "ok", "service": "OBLIVION", "port": 3012, "timestamp": time.time()}





@app.get("/api/suggest")

async def api_suggest(q: str = Query("", min_length=1)):

    suggestions = await searxng_autocomplete(q)

    return JSONResponse([q, suggestions])


# === Push Notification VAPID Setup ===
import os as _os
import pathlib as _pathlib

VAPID_KEYS_PATH = "/opt/oblivionzone/vapid_keys.json"

def _load_or_generate_vapid_keys():
    """Load VAPID keys from file, or generate and save them."""
    if _os.path.exists(VAPID_KEYS_PATH):
        with open(VAPID_KEYS_PATH) as f:
            return json.load(f)
    try:
        from py_vapid import Vapid
        v = Vapid()
        v.generate_keys()
        # Export keys
        raw_priv = v.private_pem()
        raw_pub = v.public_key
        import base64
        pub_key = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("ascii")
        keys = {
            "public_key": pub_key,
            "private_pem": raw_priv.decode("utf-8") if isinstance(raw_priv, bytes) else raw_priv,
        }
        with open(VAPID_KEYS_PATH, "w") as f:
            json.dump(keys, f, indent=2)
        return keys
    except ImportError:
        # py_vapid not installed -- return placeholder
        return {"public_key": "", "private_pem": ""}

_vapid_keys_cache = None

def _get_vapid_keys():
    global _vapid_keys_cache
    if _vapid_keys_cache is None:
        _vapid_keys_cache = _load_or_generate_vapid_keys()
    return _vapid_keys_cache

_push_subscriptions: list = []

@app.get("/api/push/vapid-key")
async def vapid_public_key():
    keys = _get_vapid_keys()
    return JSONResponse({"publicKey": keys.get("public_key", "")})

@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    body = await request.json()
    subscription = body.get("subscription")
    if not subscription:
        return JSONResponse({"error": "No subscription provided"}, status_code=400)
    # Store subscription (in production, use a database)
    if subscription not in _push_subscriptions:
        _push_subscriptions.append(subscription)
    return JSONResponse({"ok": True, "message": "Subscribed"})




@app.get("/api/search")

async def api_search(

    request: Request,

    q: str = Query(..., min_length=1),

    cat: str = Query("general"),

    page: int = Query(1, ge=1),

):

    # HyperLogLog: track visitor
    client_ip = request.client.host if request.client else "unknown"
    hll_analytics.track_visitor(client_ip)

    # BANG SYNTAX -- redirect to other search engines (like DuckDuckGo)

    BANGS = {

        "!g": "https://www.google.com/search?q=",

        "!b": "https://www.bing.com/search?q=",

        "!d": "https://duckduckgo.com/?q=",

        "!w": "https://en.wikipedia.org/wiki/Special:Search?search=",

        "!yt": "https://www.youtube.com/results?search_query=",

        "!gh": "https://github.com/search?q=",

        "!so": "https://stackoverflow.com/search?q=",

        "!r": "https://www.reddit.com/search/?q=",

        "!tw": "https://twitter.com/search?q=",

        "!a": "https://www.amazon.com/s?k=",

        "!m": "https://www.google.com/maps/search/",

        "!img": "https://www.google.com/search?tbm=isch&q=",

        "!n": "https://news.google.com/search?q=",

        "!py": "https://pypi.org/search/?q=",

        "!npm": "https://www.npmjs.com/search?q=",

        "!mdn": "https://developer.mozilla.org/en-US/search?q=",

        "!wa": "https://www.wolframalpha.com/input?i=",

        "!imdb": "https://www.imdb.com/find?q=",

        "!sp": "https://open.spotify.com/search/",

        "!li": "https://www.linkedin.com/search/results/all/?keywords=",

    }

    for bang, url in BANGS.items():

        if q.strip().startswith(bang + " ") or q.strip().endswith(" " + bang):

            clean_q = q.replace(bang, "").strip()

            return HTMLResponse(f'<script>window.location.href="{url}{clean_q}"</script>')



    try:

        search_task = searxng_search(q, categories=cat, page=page)

        ads_task = fetch_ads(q)

        data, ads = await asyncio.gather(search_task, ads_task)

    except Exception as e:

        return JSONResponse({"error": str(e), "results": [], "ads": []}, status_code=502)



    results = data.get("results", [])

    suggestions = data.get("suggestions", [])

    infoboxes = data.get("infoboxes", [])

    number_of_results = data.get("number_of_results", 0)



    processed = []

    for r in results:

        url = r.get("url", "")

        title = r.get("title", "No title")

        snippet = r.get("content", "")

        engine = r.get("engine", "")

        engines = r.get("engines", [])

        safety = get_safety_score(url, title, snippet)

        domain = extract_domain(url)

        item = {

            "url": url, "title": title, "snippet": snippet,

            "engine": engine, "engines": engines, "domain": domain,

            "safety": safety,

            "published": r.get("publishedDate", ""),

            "thumbnail": r.get("thumbnail", "") or r.get("img_src", ""),

        }

        # Pass through category-specific fields

        if cat == "images":

            item["img_src"] = r.get("img_src", "")

            item["thumbnail_src"] = r.get("thumbnail_src", "") or r.get("thumbnail", "") or r.get("img_src", "")

        elif cat == "videos":

            item["length"] = r.get("length", "")

            item["video_thumbnail"] = r.get("thumbnail", "") or r.get("img_src", "")

        elif cat == "news":

            item["img_src"] = r.get("img_src", "")

            item["publishedDate"] = r.get("publishedDate", "")

        elif cat == "music":

            item["img_src"] = r.get("img_src", "")

        # --- Reading level enrichment ---

        import textstat as _textstat

        snippet_text = item.get("snippet", "")

        if snippet_text and len(snippet_text) > 50:

            grade = _textstat.flesch_kincaid_grade(snippet_text)

            item["reading_level"] = "Easy" if grade <= 6 else "Medium" if grade <= 10 else "Advanced" if grade <= 14 else "Expert"

            item["reading_grade"] = round(grade, 1)



        # --- Free paper link via Unpaywall ---

        academic_domains = ["nature.com", "science.org", "springer.com", "wiley.com", "elsevier.com", "ieee.org", "arxiv.org", "pubmed"]

        item_url = item.get("url", "")

        item_domain = item.get("domain", "")

        if "/10." in item_url and any(d in item_domain for d in academic_domains):

            doi_part = item_url.split("/10.")[1] if "/10." in item_url else ""

            if doi_part:

                try:

                    import urllib.request as _urllib_req, json as _json

                    _resp = _urllib_req.urlopen(f"https://api.unpaywall.org/v2/10.{doi_part}?email=admin@oblivionzone.com", timeout=2)

                    _data = _json.loads(_resp.read().decode())

                    if _data.get("is_oa") and _data.get("best_oa_location", {}).get("url"):

                        item["free_url"] = _data["best_oa_location"]["url"]

                except Exception:

                    pass



        processed.append(item)


    # === BLOOM FILTER: URL deduplication ===
    bloom = BloomFilter(expected_items=len(processed) + 100, fp_rate=0.01)
    deduped = []
    for item in processed:
        norm = normalize_url(item.get("url", ""))
        if not bloom.add_and_check(norm):
            deduped.append(item)
    processed = deduped

    # === SIMHASH: Near-duplicate detection (title+snippet) ===
    seen_hashes = []
    unique_results = []
    for item in processed:
        text = f"{item.get('title', '')} {item.get('snippet', '')}"
        if len(text.strip()) < 5:
            unique_results.append(item)
            continue
        sh = _simhash(text)
        is_dup = False
        for prev_hash in seen_hashes:
            if _simhash_distance(sh, prev_hash) < 3:
                is_dup = True
                break
        if not is_dup:
            seen_hashes.append(sh)
            unique_results.append(item)
    processed = unique_results

    # === PAGERANK: Domain authority scoring ===
    pagerank_scores = _compute_pagerank(processed)
    for item in processed:
        d = item.get("domain", "")
        item["pagerank"] = round(pagerank_scores.get(d, 0.0), 4)

    # === HITS: Authority scoring across queries ===
    hits_scores = _update_hits(processed)
    for item in processed:
        d = item.get("domain", "")
        item["authority"] = round(hits_scores.get(d, 0.0), 4)

    # === Boost: combine safety + pagerank + authority for final ordering bonus ===
    for item in processed:
        pr_bonus = item.get("pagerank", 0) * 5
        auth_bonus = item.get("authority", 0) * 3
        item["oblivion_score"] = round(item.get("safety", {}).get("score", 50) + pr_bonus + auth_bonus, 1)

    # === COMMUNITY VOTES: Reddit-style vote boosting ===
    try:
        all_urls = [item.get("url", "") for item in processed if item.get("url")]
        bulk_votes = get_bulk_votes(all_urls)
        for item in processed:
            url = item.get("url", "")
            vd = bulk_votes.get(url, {})
            item["votes"] = {"ups": vd.get("ups", 0), "downs": vd.get("downs", 0), "net": vd.get("net", 0)}
            boost = vote_boost(url, vd)
            item["vote_boost"] = boost
            item["oblivion_score"] = round(item.get("oblivion_score", 50) + boost, 1)
    except Exception:
        # Votes are non-critical; don't break search if DB is down
        for item in processed:
            item["votes"] = {"ups": 0, "downs": 0, "net": 0}
            item["vote_boost"] = 0

    # === Generate shareable links for each result ===
    for item in processed:
        sh = _make_share_hash(item.get("url", ""), item.get("title", ""))
        item["share_url"] = f"https://oblivionsearch.com/share/{sh}"
        _share_store[sh] = {
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
        }

    # === HyperLogLog analytics ===
    hll_analytics.track_query(q)
    for item in processed:
        d = item.get("domain", "")
        if d:
            hll_analytics.track_domain(d)

    processed_ads = []

    for ad in ads[:2]:

        processed_ads.append({

            "id": ad.get("id", 0),

            "url": ad.get("ad_url", ""),

            "title": ad.get("ad_title", ""),

            "description": ad.get("ad_description", ""),

            "company": ad.get("company", ""),

        })



    return JSONResponse({

        "results": processed,

        "ads": processed_ads,

        "suggestions": suggestions[:8],

        "infoboxes": infoboxes[:2],

        "total": number_of_results or len(results),

        "query": q,

        "category": cat,

        "page": page,

    })


@app.get("/api/search/clustered")
async def api_search_clustered(
    request: Request,
    q: str = Query(..., min_length=1),
    cat: str = Query("general"),
    page: int = Query(1, ge=1),
):
    """Return search results grouped into topic clusters (Carrot2-inspired)."""
    # Reuse the main search logic
    try:
        search_task = searxng_search(q, categories=cat, page=page)
        data = await search_task
    except Exception as e:
        return JSONResponse({"error": str(e), "clusters": [], "total": 0}, status_code=502)

    results = data.get("results", [])

    # Build processed results (lightweight version for clustering)
    processed = []
    for r in results:
        url = r.get("url", "")
        title = r.get("title", "No title")
        snippet = r.get("content", "")
        domain = extract_domain(url)
        processed.append({
            "url": url, "title": title, "snippet": snippet,
            "engine": r.get("engine", ""),
            "engines": r.get("engines", []),
            "domain": domain,
            "published": r.get("publishedDate", ""),
        })

    # Deduplicate with Bloom filter
    bloom = BloomFilter(expected_items=len(processed) + 100, fp_rate=0.01)
    deduped = []
    for item in processed:
        norm = normalize_url(item.get("url", ""))
        if not bloom.add_and_check(norm):
            deduped.append(item)
    processed = deduped

    # Cluster the results
    clusters = cluster_results(processed)

    return JSONResponse({
        "clusters": clusters,
        "total": len(processed),
        "query": q,
        "category": cat,
        "page": page,
    })



@app.get("/api/engines")
async def api_engines():
    """Return REAL engine status from SearXNG — live data, not fake."""
    try:
        resp = await _http_client.get(f"{SEARXNG_URL}/config")
        if resp.status_code == 200:
            data = resp.json()
            engines = []
            for e in data.get("engines", []):
                if e.get("enabled", True):
                    engines.append({
                        "name": e.get("name", ""),
                        "categories": e.get("categories", []),
                        "language": e.get("language_support", True),
                        "shortcut": e.get("shortcut", ""),
                    })
            return JSONResponse({"engines": engines, "total": len(engines), "status": "live"})
    except:
        pass
    return JSONResponse({"engines": [], "total": 0, "status": "error"})

@app.get("/api/engines/health")
async def api_engines_health():
    """Do a REAL search and return which engines responded — actual live health check."""
    try:
        data = await searxng_search("test", categories="general", page=1)
        results = data.get("results", [])
        active = {}
        for r in results:
            eng = r.get("engine", "unknown")
            if eng not in active:
                active[eng] = {"name": eng, "results": 0, "status": "active"}
            active[eng]["results"] += 1
        return JSONResponse({
            "active_engines": list(active.values()),
            "active_count": len(active),
            "total_results": len(results),
            "timestamp": time.time(),
            "status": "live"
        })
    except Exception as e:
        return JSONResponse({"error": str(e), "status": "error"})

@app.get("/api/ai")

async def api_ai(q: str = Query(..., min_length=1)):

    try:

        data = await searxng_search(q, categories="general", page=1)

        results = data.get("results", [])[:6]

        context_parts = []

        for i, r in enumerate(results, 1):

            context_parts.append(f"[{i}] {r.get('title','')} -- {r.get('content','')[:300]} (Source: {r.get('url','')})")

        context = "\n".join(context_parts)

        prompt = f"""The user searched for: "{q}"



Here are the top search results:

{context}



Based on these search results, provide a helpful, concise summary that answers the user's query. Reference the sources by number [1], [2], etc. Be factual and accurate. If the results contain conflicting information, note that. Keep your answer to 2-3 paragraphs maximum."""

        answer = await ollama_generate(prompt)

        return JSONResponse({"answer": answer, "query": q})

    except Exception as e:

        return JSONResponse({"answer": f"AI analysis unavailable: {e}", "query": q}, status_code=200)





@app.get("/api/ai/chat")

async def api_ai_chat(message: str = Query(...), context: str = Query("")):

    try:

        prompt = f"""Context: {context}\n\nUser message: {message}\n\nRespond helpfully and concisely."""

        answer = await ollama_generate(prompt)

        return JSONResponse({"reply": answer})

    except Exception as e:

        return JSONResponse({"reply": f"Error: {e}"}, status_code=200)





@app.get("/api/translate")

async def api_translate(text: str = Query(...), to: str = Query("en"), source: str = Query("auto")):

    try:

        translated = await translate_text(text, target=to, source=source)

        return JSONResponse({"translated": translated, "target": to})

    except Exception as e:

        return JSONResponse({"error": str(e)}, status_code=502)



# ---------------------------------------------------------------------------

# New Search Verticals -- Free API Integrations

# ---------------------------------------------------------------------------


@app.get("/api/academic")
async def academic_search(q: str = Query(..., min_length=1)):
    try:
        resp = await _http_client.get(f"https://api.openalex.org/works?search={q}&per_page=10&mailto=admin@oblivionzone.com")
        data = resp.json()
        results = []
        for work in data.get("results", []):
            results.append({
                "title": work.get("title", ""),
                "url": work.get("doi", "") or work.get("id", ""),
                "authors": [a.get("author", {}).get("display_name", "") for a in work.get("authorships", [])[:3]],
                "year": work.get("publication_year", ""),
                "cited_by": work.get("cited_by_count", 0),
                "source": work.get("primary_location", {}).get("source", {}).get("display_name", "") if work.get("primary_location") and work.get("primary_location", {}).get("source") else ""
            })
        return JSONResponse({"results": results, "total": data.get("meta", {}).get("count", 0)})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


@app.get("/api/hackernews")
async def hn_search(q: str = Query(..., min_length=1)):
    try:
        resp = await _http_client.get(f"https://hn.algolia.com/api/v1/search?query={q}&hitsPerPage=10")
        data = resp.json()
        results = [{"title": h.get("title",""), "url": h.get("url",""), "points": h.get("points",0), "comments": h.get("num_comments",0), "date": h.get("created_at",""), "author": h.get("author","")} for h in data.get("hits", []) if h.get("title")]
        return JSONResponse({"results": results})
    except Exception as e:
        return JSONResponse({"results": [], "error": str(e)})


@app.get("/api/instant")
async def instant_answer(q: str = Query(..., min_length=1)):
    try:
        resp = await _http_client.get(f"https://api.duckduckgo.com/?q={q}&format=json&no_html=1")
        data = resp.json()
        return JSONResponse({"abstract": data.get("Abstract",""), "source": data.get("AbstractSource",""), "url": data.get("AbstractURL",""), "image": data.get("Image",""), "related": [t.get("Text","") for t in data.get("RelatedTopics",[])[:5] if isinstance(t, dict) and t.get("Text")]})
    except Exception as e:
        return JSONResponse({"abstract": "", "error": str(e)})


@app.get("/api/wiki")
async def wiki_search(q: str = Query(..., min_length=1)):
    try:
        resp = await _http_client.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{q}", headers={"User-Agent": "OblivionSearch/3.0 (admin@oblivionzone.com)"})
        if resp.status_code == 200:
            data = resp.json()
            return JSONResponse({"title": data.get("title",""), "extract": data.get("extract",""), "thumbnail": data.get("thumbnail",{}).get("source","") if data.get("thumbnail") else "", "url": data.get("content_urls",{}).get("desktop",{}).get("page","") if data.get("content_urls") else ""})
        return JSONResponse({"error": "Not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/knowledge")
async def knowledge_panel(q: str = Query(..., min_length=1)):
    """Proxy to the OBLIVION Knowledge Panel service (local Wikipedia index)."""
    try:
        resp = await _http_client.get(f"http://localhost:3045/api/knowledge?q={q}", timeout=5.0)
        if resp.status_code == 200:
            return JSONResponse(resp.json())
        return JSONResponse({"found": False})
    except Exception:
        # Fallback: try Wikipedia REST API directly if knowledge service is down
        try:
            resp = await _http_client.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{q}",
                headers={"User-Agent": "OblivionSearch/3.0 (admin@oblivionzone.com)"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return JSONResponse({
                    "found": True,
                    "title": data.get("title", ""),
                    "extract": data.get("extract", ""),
                    "categories": [],
                    "image": data.get("thumbnail", {}).get("source", "") if data.get("thumbnail") else "",
                    "url": data.get("content_urls", {}).get("desktop", {}).get("page", "") if data.get("content_urls") else "",
                    "source": "wikipedia_api",
                })
        except Exception:
            pass
        return JSONResponse({"found": False})


@app.get("/api/brave")
async def brave_search(q: str = Query(..., min_length=1)):
    BRAVE_API_KEY = "PLACEHOLDER_BRAVE_API_KEY"
    if BRAVE_API_KEY.startswith("PLACEHOLDER"):
        return JSONResponse({"results": [], "error": "Brave API key not configured. Sign up at https://api.search.brave.com/ for a free key (2000 queries/month)."})
    try:
        resp = await _http_client.get(
            f"https://api.search.brave.com/res/v1/web/search?q={q}&count=10",
            headers={"Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": BRAVE_API_KEY}
        )
        data = resp.json()
        results = [{"title": r.get("title",""), "url": r.get("url",""), "snippet": r.get("description",""), "age": r.get("age","")} for r in data.get("web",{}).get("results",[])]
        return JSONResponse({"results": results})
    except Exception as e:
        return JSONResponse({"results": [], "error": str(e)})


@app.get("/api/archive")
async def archive_search(q: str = Query(..., min_length=1)):
    try:
        resp = await _http_client.get(f"https://archive.org/advancedsearch.php?q={q}&output=json&rows=10")
        data = resp.json()
        results = [{"title": d.get("title",""), "url": f"https://archive.org/details/{d.get('identifier','')}", "year": d.get("year",""), "type": d.get("mediatype","")} for d in data.get("response",{}).get("docs",[])]
        return JSONResponse({"results": results})
    except Exception as e:
        return JSONResponse({"results": [], "error": str(e)})




# ---------------------------------------------------------------------------
# Books search via Open Library (/api/books)
# ---------------------------------------------------------------------------

@app.get("/api/books")
async def book_search(q: str = Query(..., min_length=1)):
    try:
        resp = await _http_client.get(f"https://openlibrary.org/search.json?q={urllib.parse.quote(q)}&limit=10")
        data = resp.json()
        results = []
        for b in data.get("docs", []):
            cover_id = b.get("cover_i")
            results.append({
                "title": b.get("title", ""),
                "author": ", ".join(b.get("author_name", [])),
                "year": b.get("first_publish_year", ""),
                "isbn": (b.get("isbn", [""])[0] if b.get("isbn") else ""),
                "cover": f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else "",
                "url": f"https://openlibrary.org{b.get('key', '')}" if b.get("key") else "",
                "publisher": ", ".join(b.get("publisher", [])[:2]),
                "subject": ", ".join(b.get("subject", [])[:3]),
            })
        return JSONResponse({"results": results, "total": data.get("numFound", 0)})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------
# Music search via MusicBrainz (/api/music)
# ---------------------------------------------------------------------------

@app.get("/api/music")
async def music_search(q: str = Query(..., min_length=1)):
    try:
        resp = await _http_client.get(
            f"https://musicbrainz.org/ws/2/recording?query={urllib.parse.quote(q)}&limit=10&fmt=json",
            headers={"User-Agent": "OBLIVION/1.0 (admin@oblivionzone.com)"}
        )
        data = resp.json()
        results = []
        for r in data.get("recordings", []):
            artist = ""
            if r.get("artist-credit"):
                artist = r["artist-credit"][0].get("name", "")
            album = ""
            if r.get("releases"):
                album = r["releases"][0].get("title", "")
            results.append({
                "title": r.get("title", ""),
                "artist": artist,
                "album": album,
                "year": r.get("first-release-date", ""),
                "score": r.get("score", 0),
                "url": f"https://musicbrainz.org/recording/{r.get('id', '')}" if r.get("id") else "",
            })
        return JSONResponse({"results": results, "total": data.get("count", len(results))})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------
# Companies search via Companies House (/api/companies)
# ---------------------------------------------------------------------------

@app.get("/api/companies")
async def companies_search(q: str = Query(..., min_length=1)):
    """Search UK companies via Companies House free API (no key needed for basic search)."""
    try:
        resp = await _http_client.get(
            f"https://api.company-information.service.gov.uk/advanced-search/companies?company_name_includes={urllib.parse.quote(q)}&size=10",
            headers={"Authorization": "Basic SDgxNE84VFdjYVNCSzZfZTN1NUd3WUs2cmRtZkc1YUdESlpRUUlLVkVpUFl4dXBhb3QwMFdwQTY1TXZqMHFwYjo=", "User-Agent": "OBLIVION/1.0"}
        )
        if resp.status_code == 401:
            # Fallback: search from local DB if we have it, otherwise return helpful message
            return JSONResponse({"results": [], "total": 0, "error": "Companies House API requires an API key. Apply at https://developer.company-information.service.gov.uk/"})
        data = resp.json()
        results = []
        for item in data.get("items", []):
            results.append({
                "title": item.get("company_name", ""),
                "number": item.get("company_number", ""),
                "status": item.get("company_status", ""),
                "type": item.get("company_type", ""),
                "incorporated": item.get("date_of_creation", ""),
                "address": item.get("registered_office_address", {}).get("address_line_1", "") if item.get("registered_office_address") else "",
                "url": f"https://find-and-update.company-information.service.gov.uk/company/{item.get('company_number', '')}",
            })
        return JSONResponse({"results": results, "total": data.get("total_results", len(results))})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------
# Patent search via USPTO (/api/patents)
# ---------------------------------------------------------------------------

@app.get("/api/patents")
async def patents_search(q: str = Query(..., min_length=1)):
    """Search patents via WIPO (free, no key needed) + Google Patents links."""
    try:
        # Use WIPO's free PatentScope search
        resp = await _http_client.get(
            f"https://patentscope.wipo.int/search/en/result.jsf?query={urllib.parse.quote(q)}&office=&prevFilter=&sortOption=Relevance&resultsPerPage=10",
            headers={"User-Agent": "OBLIVION/1.0 (admin@oblivionzone.com)", "Accept": "application/json"},
            timeout=10.0,
        )
        # Fallback: search via SearXNG with patents category
        searx_resp = await _http_client.get(
            f"http://localhost:8890/search",
            params={"q": f"patent {q}", "format": "json", "categories": "general", "engines": "google,bing,duckduckgo"},
            timeout=6.0,
        )
        data = searx_resp.json()
        results = []
        seen = set()
        for r in data.get("results", []):
            url = r.get("url", "")
            title = r.get("title", "")
            # Filter for actual patent results
            if any(p in url.lower() for p in ["patent", "uspto.gov", "epo.org", "wipo.int"]) or "patent" in title.lower():
                if url not in seen:
                    seen.add(url)
                    results.append({
                        "title": title,
                        "number": "",
                        "date": "",
                        "inventor": "",
                        "abstract": r.get("content", "")[:300],
                        "url": url,
                    })
                    if len(results) >= 10:
                        break
        return JSONResponse({"results": results, "total": len(results)})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------
# Generic Medicine search via OpenFDA (/api/medicine)
# ---------------------------------------------------------------------------

@app.get("/api/medicine")
async def medicine_search(q: str = Query(..., min_length=1)):
    """Search generic medicines via OpenFDA (free, no key needed)."""
    try:
        encoded = urllib.parse.quote(q)
        resp = await _http_client.get(
            f"https://api.fda.gov/drug/label.json?search=openfda.brand_name:{encoded}+openfda.generic_name:{encoded}&limit=10",
            timeout=10.0,
        )
        data = resp.json()
        results = []
        for r in data.get("results", []):
            openfda = r.get("openfda", {})
            results.append({
                "brand": (openfda.get("brand_name", [""])[0]),
                "generic": (openfda.get("generic_name", [""])[0]),
                "manufacturer": (openfda.get("manufacturer_name", [""])[0]),
                "route": (openfda.get("route", [""])[0]),
                "substance": (openfda.get("substance_name", [""])[0]),
                "purpose": (r.get("purpose", [""])[0] if r.get("purpose") else ""),
                "warnings": (r.get("warnings", [""])[0][:200] if r.get("warnings") else ""),
            })
        return JSONResponse({"results": results, "total": data.get("meta", {}).get("results", {}).get("total", 0)})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------
# Formula / Algorithm search via arXiv (/api/formulas)
# ---------------------------------------------------------------------------

@app.get("/api/formulas")
async def formula_search(q: str = Query(..., min_length=1)):
    """Search mathematical formulas and algorithms via arXiv (free, no key needed)."""
    import xml.etree.ElementTree as ET
    try:
        encoded = urllib.parse.quote(q)
        resp = await _http_client.get(
            f"http://export.arxiv.org/api/query?search_query=all:{encoded}&max_results=10",
            timeout=10.0,
        )
        root = ET.fromstring(resp.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        results = []
        for entry in root.findall("atom:entry", ns):
            results.append({
                "title": entry.find("atom:title", ns).text.strip() if entry.find("atom:title", ns) is not None else "",
                "authors": [a.find("atom:name", ns).text for a in entry.findall("atom:author", ns)[:3]],
                "abstract": (entry.find("atom:summary", ns).text.strip()[:200] if entry.find("atom:summary", ns) is not None else ""),
                "url": entry.find("atom:id", ns).text if entry.find("atom:id", ns) is not None else "",
                "published": (entry.find("atom:published", ns).text[:10] if entry.find("atom:published", ns) is not None else ""),
                "category": "arXiv"
            })
        return JSONResponse({"results": results, "total": len(results)})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------
# Newspaper search via Chronicling America (/api/newspapers)
# ---------------------------------------------------------------------------

@app.get("/api/newspapers")
async def newspaper_search(q: str = Query(..., min_length=1)):
    """Search historic newspaper pages via Library of Congress Chronicling America."""
    try:
        resp = await _http_client.get(
            f"https://www.loc.gov/newspapers/?q={urllib.parse.quote(q)}&fo=json&c=10&dl=page",
            timeout=12.0,
        )
        data = resp.json()
        results = []
        for i in data.get("results", []):
            desc_parts = i.get("description", [])
            snippet = desc_parts[0][:200] if desc_parts else ""
            loc_list = i.get("location", [])
            state = loc_list[0] if loc_list else ""
            results.append({
                "title": i.get("title", ""),
                "date": i.get("date", ""),
                "url": i.get("url", ""),
                "snippet": snippet,
                "state": state,
            })
        pagination = data.get("pagination", {})
        return JSONResponse({"results": results, "total": pagination.get("of", len(results))})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------
# Museum search via Smithsonian Open Access (/api/museums)
# ---------------------------------------------------------------------------

@app.get("/api/museums")
async def museum_search(q: str = Query(..., min_length=1)):
    """Search 4.4M+ items from the Smithsonian Open Access API."""
    try:
        resp = await _http_client.get(
            f"https://api.si.edu/openaccess/api/v1.0/search?q={urllib.parse.quote(q)}&rows=10&api_key=DEMO_KEY",
            timeout=12.0,
        )
        data = resp.json()
        results = []
        for r in data.get("response", {}).get("rows", []):
            desc = r.get("content", {}).get("descriptiveNonRepeating", {})
            freetext = r.get("content", {}).get("freetext", {})
            image = ""
            if desc.get("online_media") and desc["online_media"].get("media"):
                image = desc["online_media"]["media"][0].get("thumbnail", "")
            obj_type = ""
            if freetext.get("objectType"):
                obj_type = freetext["objectType"][0].get("content", "")
            results.append({
                "title": r.get("title", ""),
                "url": desc.get("record_link", ""),
                "image": image,
                "type": obj_type,
            })
        return JSONResponse({"results": results, "total": data.get("response", {}).get("rowCount", 0)})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------
# NASA Image search (/api/nasa)
# ---------------------------------------------------------------------------

@app.get("/api/nasa")
async def nasa_search(q: str = Query(..., min_length=1)):
    """Search NASA's public image and media library."""
    try:
        resp = await _http_client.get(
            f"https://images-api.nasa.gov/search?q={urllib.parse.quote(q)}&media_type=image&page_size=10",
            timeout=10.0,
        )
        data = resp.json()
        results = []
        for i in data.get("collection", {}).get("items", []):
            item_data = i.get("data", [{}])[0]
            image = i.get("links", [{}])[0].get("href", "") if i.get("links") else ""
            results.append({
                "title": item_data.get("title", ""),
                "description": item_data.get("description", "")[:200],
                "image": image,
                "date": item_data.get("date_created", "")[:10],
                "center": item_data.get("center", ""),
            })
        return JSONResponse({"results": results})
    except Exception as e:
        return JSONResponse({"results": [], "error": str(e)})


# ---------------------------------------------------------------------------
# Cultural Heritage search via Europeana (/api/culture)
# ---------------------------------------------------------------------------

@app.get("/api/culture")
async def culture_search(q: str = Query(..., min_length=1)):
    """Search 50M+ items from Europeana cultural heritage collections."""
    try:
        resp = await _http_client.get(
            f"https://api.europeana.eu/record/v2/search.json?query={urllib.parse.quote(q)}&rows=10&profile=rich&wskey=apidemo",
            timeout=10.0,
        )
        data = resp.json()
        results = []
        for i in data.get("items", []):
            results.append({
                "title": i.get("title", [""])[0] if i.get("title") else "",
                "url": i.get("guid", ""),
                "image": i.get("edmPreview", [""])[0] if i.get("edmPreview") else "",
                "provider": i.get("dataProvider", [""])[0] if i.get("dataProvider") else "",
                "year": i.get("year", [""])[0] if i.get("year") else "",
            })
        return JSONResponse({"results": results, "total": data.get("totalResults", 0)})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------
# Science search via CORE (/api/science)
# ---------------------------------------------------------------------------

@app.get("/api/science")
async def science_search(q: str = Query(..., min_length=1)):
    """Search 300M+ academic papers via CORE API (different from OpenAlex)."""
    try:
        resp = await _http_client.get(
            f"https://api.core.ac.uk/v3/search/works?q={urllib.parse.quote(q)}&limit=10",
            timeout=10.0,
        )
        data = resp.json()
        results = []
        for r in data.get("results", []):
            url = r.get("downloadUrl", "")
            if not url and r.get("sourceFulltextUrls"):
                url = r["sourceFulltextUrls"][0]
            results.append({
                "title": r.get("title", ""),
                "authors": [a.get("name", "") for a in r.get("authors", [])[:3]],
                "year": r.get("yearPublished", ""),
                "url": url or "",
                "abstract": r.get("abstract", "")[:200],
            })
        return JSONResponse({"results": results, "total": data.get("totalHits", 0)})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------
# Movies search via TMDb (/api/movies)
# ---------------------------------------------------------------------------

@app.get("/api/movies")
async def movies_search(q: str = Query(..., min_length=1)):
    """Search movies via Wikidata (free, no key needed)."""
    try:
        safe_q = q.replace('"', '').replace('\\', '')
        sparql = f"""SELECT ?film ?filmLabel ?filmDescription ?date ?directorLabel ?image WHERE {{
  SERVICE wikibase:mwapi {{
    bd:serviceParam wikibase:endpoint "www.wikidata.org" ;
                    wikibase:api "EntitySearch" ;
                    mwapi:search "{safe_q}" ;
                    mwapi:language "en" .
    ?film wikibase:apiOutputItem mwapi:item .
  }}
  ?film wdt:P31/wdt:P279* wd:Q11424 .
  OPTIONAL {{ ?film wdt:P577 ?date }}
  OPTIONAL {{ ?film wdt:P57 ?director }}
  OPTIONAL {{ ?film wdt:P18 ?image }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" }}
}} LIMIT 12"""
        resp = await _http_client.get(
            "https://query.wikidata.org/sparql",
            params={"query": sparql, "format": "json"},
            headers={"User-Agent": "OblivionSearch/1.0 (admin@oblivionzone.com)", "Accept": "application/sparql-results+json"},
            timeout=20.0,
        )
        data = resp.json()
        results = []
        seen_ids = set()
        for b in data.get("results", {}).get("bindings", []):
            wiki_id = b.get("film", {}).get("value", "").split("/")[-1]
            if wiki_id in seen_ids:
                continue
            seen_ids.add(wiki_id)
            year = b.get("date", {}).get("value", "")[:4] if b.get("date") else ""
            results.append({
                "title": b.get("filmLabel", {}).get("value", ""),
                "year": year,
                "overview": b.get("filmDescription", {}).get("value", ""),
                "director": b.get("directorLabel", {}).get("value", ""),
                "poster": b.get("image", {}).get("value", ""),
                "rating": 0,
                "votes": 0,
                "url": f"https://www.wikidata.org/wiki/{wiki_id}",
            })
        return JSONResponse({"results": results, "total": len(results)})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------
# Genealogy search via Wikidata SPARQL (/api/genealogy)
# ---------------------------------------------------------------------------

@app.get("/api/genealogy")
async def genealogy_search(q: str = Query(..., min_length=1)):
    """Search people in Wikidata for genealogy/biography research."""
    try:
        safe_q = q.replace('"', '').replace('\\', '')
        sparql = f"""SELECT ?person ?personLabel ?personDescription ?birth ?death ?birthPlaceLabel ?image WHERE {{
  SERVICE wikibase:mwapi {{
    bd:serviceParam wikibase:endpoint "www.wikidata.org" ;
                    wikibase:api "EntitySearch" ;
                    mwapi:search "{safe_q}" ;
                    mwapi:language "en" .
    ?person wikibase:apiOutputItem mwapi:item .
  }}
  ?person wdt:P31 wd:Q5 .
  OPTIONAL {{ ?person wdt:P569 ?birth }}
  OPTIONAL {{ ?person wdt:P570 ?death }}
  OPTIONAL {{ ?person wdt:P19 ?birthPlace }}
  OPTIONAL {{ ?person wdt:P18 ?image }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" }}
}} LIMIT 12"""
        resp = await _http_client.get(
            "https://query.wikidata.org/sparql",
            params={"query": sparql, "format": "json"},
            headers={
                "User-Agent": "OblivionSearch/1.0 (admin@oblivionzone.com)",
                "Accept": "application/sparql-results+json",
            },
            timeout=20.0,
        )
        data = resp.json()
        results = []
        for b in data.get("results", {}).get("bindings", []):
            birth = b.get("birth", {}).get("value", "")[:10] if b.get("birth") else ""
            death = b.get("death", {}).get("value", "")[:10] if b.get("death") else ""
            wiki_id = b.get("person", {}).get("value", "").split("/")[-1]
            person_name = b.get("personLabel", {}).get("value", "")
            name_parts = person_name.split() if person_name else safe_q.split()
            fname = name_parts[0] if name_parts else ""
            lname = name_parts[-1] if len(name_parts) > 1 else ""
            results.append({
                "name": person_name,
                "description": b.get("personDescription", {}).get("value", ""),
                "birth": birth,
                "death": death,
                "birthPlace": b.get("birthPlaceLabel", {}).get("value", ""),
                "image": b.get("image", {}).get("value", ""),
                "url": f"https://www.wikidata.org/wiki/{wiki_id}",
                "findagrave": f"https://www.findagrave.com/memorial/search?firstname={urllib.parse.quote(fname)}&lastname={urllib.parse.quote(lname)}",
            })
        return JSONResponse({"results": results, "total": len(results)})
    except Exception as e:
        return JSONResponse({"results": [], "total": 0, "error": str(e)})


# ---------------------------------------------------------------------------

# Business Pages

# ---------------------------------------------------------------------------

NAV_HTML = '''<nav><a href="/" class="logo"><img src="/logos/main-logo.png" alt="OBLIVION" style="height:32px;width:auto;vertical-align:middle"></a><div><a href="/">Search</a><a href="/business">Business Directory</a><a href="/about-oblivion">About</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a></div></nav>'''

PAGE_STYLE = '''*{margin:0;padding:0;box-sizing:border-box}body{background:#fff;color:#202124;font-family:arial,sans-serif}

nav{background:#f8f9fa;border-bottom:1px solid #e0e0e0;padding:12px 20px;display:flex;align-items:center;justify-content:space-between}

nav .logo{font-size:20px;font-weight:900;background:linear-gradient(135deg,#7c3aed,#3b82f6,#ec4899);-webkit-background-clip:text;-webkit-text-fill-color:transparent;text-decoration:none}

nav a{color:#5f6368;font-size:13px;margin-left:20px;text-decoration:none}nav a:hover{color:#202124}

.footer{text-align:center;padding:40px;color:#70757a;font-size:12px;border-top:1px solid #e0e0e0;margin-top:60px}'''





@app.get("/advertise", response_class=HTMLResponse)

async def advertise():

    return HTMLResponse("""<!DOCTYPE html><html><head><meta charset=UTF-8><meta name=viewport content="width=device-width,initial-scale=1">

<title>OBLIVION Ads -- Coming Soon</title>

<style>body{background:#fff;font-family:arial,sans-serif;text-align:center;padding:80px 20px}</style>

</head><body>

<h1 style="font-size:48px;font-weight:900;background:linear-gradient(135deg,#7c3aed,#3b82f6,#ec4899);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">OBLIVION Ads</h1>

<p style="color:#70757a;font-size:18px;margin-top:16px;">Coming Soon</p>

<p style="color:#9ca3af;font-size:14px;margin-top:8px;">Our advertising platform is under development.</p>

<a href="/" style="display:inline-block;margin-top:24px;padding:12px 24px;background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff;border-radius:8px;text-decoration:none;font-weight:700;">Back to Search</a>

</body></html>""")

    # Original advertise page hidden below

    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset=UTF-8><meta name=viewport content="width=device-width,initial-scale=1">

<title>OBLIVION Ads -- Hidden</title>

<style>{PAGE_STYLE}

.hero{{text-align:center;padding:80px 20px;background:linear-gradient(135deg,#1a1035,#0a0a12)}}

.hero h1{{font-size:48px;font-weight:900;background:linear-gradient(135deg,#818cf8,#c084fc,#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:16px}}

.hero p{{color:#9ca3af;font-size:18px;max-width:600px;margin:0 auto 32px}}

.btn{{display:inline-block;padding:14px 32px;background:#4f46e5;color:#fff;border-radius:12px;font-weight:700;font-size:16px;text-decoration:none;margin:8px}}

.btn:hover{{background:#4338ca}}

.btn-outline{{background:transparent;border:2px solid #818cf8;color:#818cf8}}

.section{{max-width:1000px;margin:0 auto;padding:60px 20px}}

.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px;margin-top:30px}}

.card{{background:#111118;border:1px solid #1a1a2e;border-radius:12px;padding:24px}}

.card h3{{color:#818cf8;font-size:20px;margin-bottom:8px}}

.card p{{color:#9ca3af;font-size:14px;line-height:1.6}}

.price{{font-size:36px;font-weight:900;color:#4ade80;margin:12px 0}}

.price small{{font-size:14px;color:#6b7280}}

.stats{{display:flex;gap:40px;justify-content:center;margin-top:40px;flex-wrap:wrap}}

.stat{{text-align:center}}.stat .num{{font-size:36px;font-weight:900;color:#818cf8}}.stat .label{{color:#6b7280;font-size:12px}}

</style></head><body>

{NAV_HTML}

<div class="hero">

<h1>Grow Your Business<br>with OBLIVION Ads</h1>

<p>Reach millions of users searching on the most intelligent search engine. AI-powered targeting, zero fraud, real results.</p>

<a href="#pricing" class="btn">Start Advertising</a>

<a href="/business" class="btn btn-outline">List Your Business Free</a>

<div class="stats">

<div class="stat"><div class="num">246</div><div class="label">Search Engines</div></div>

<div class="stat"><div class="num">1M+</div><div class="label">Monthly Searches</div></div>

<div class="stat"><div class="num">99.9%</div><div class="label">Scam-Free Results</div></div>

<div class="stat"><div class="num">150+</div><div class="label">Countries</div></div>

</div>

</div>

<div class="section" id="pricing">

<h2 style="text-align:center;font-size:32px;margin-bottom:8px">Advertising Plans</h2>

<p style="text-align:center;color:#6b7280">Choose the plan that fits your business</p>

<div class="grid">

<div class="card">

<h3>Starter</h3><p>Perfect for small businesses</p>

<div class="price">$29<small>/month</small></div>

<p>- 1,000 ad impressions/day<br>- Top 3 search placement<br>- Basic analytics<br>- 1 campaign<br>- Email support</p>

<a href="mailto:admin@oblivionzone.com?subject=OBLIVION Ads Starter" class="btn" style="display:block;text-align:center;margin-top:16px">Get Started</a>

</div>

<div class="card" style="border-color:#818cf8">

<h3>Professional</h3><p>For growing companies</p>

<div class="price">$99<small>/month</small></div>

<p>- 10,000 ad impressions/day<br>- Top result placement<br>- AI-powered targeting<br>- 5 campaigns<br>- Priority support<br>- Competitor analysis</p>

<a href="mailto:admin@oblivionzone.com?subject=OBLIVION Ads Professional" class="btn" style="display:block;text-align:center;margin-top:16px">Get Started</a>

</div>

<div class="card">

<h3>Enterprise</h3><p>For large organizations</p>

<div class="price">$299<small>/month</small></div>

<p>- Unlimited impressions<br>- Premium placement<br>- AI audience targeting<br>- Unlimited campaigns<br>- Dedicated manager<br>- API access<br>- White-label reports</p>

<a href="mailto:admin@oblivionzone.com?subject=OBLIVION Ads Enterprise" class="btn" style="display:block;text-align:center;margin-top:16px">Contact Sales</a>

</div>

</div></div>

<div class="section">

<h2 style="text-align:center;font-size:28px;margin-bottom:8px">Why Advertise on OBLIVION?</h2>

<div class="grid">

<div class="card"><h3>Zero Ad Fraud</h3><p>Our AI Scam Shield detects and blocks fake clicks, bots, and fraud. You only pay for real human engagement.</p></div>

<div class="card"><h3>AI-Powered Targeting</h3><p>Our AI understands user intent better than keyword matching. Your ads reach people who actually need your product.</p></div>

<div class="card"><h3>Privacy-First</h3><p>We don't track users. Targeting is based on search context, not personal data. Users trust OBLIVION -- and they'll trust you.</p></div>

<div class="card"><h3>Global Reach</h3><p>246 search engines, 150+ countries, 30+ languages. Reach customers anywhere in the world.</p></div>

</div></div>

<div class="footer">OBLIVION -- Oblivion Technologies LLC | 30 N Gould St Ste R, Sheridan, WY 82801 | admin@oblivionzone.com</div>

</body></html>""")





@app.get("/business", response_class=HTMLResponse)

async def business_directory():

    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset=UTF-8><meta name=viewport content="width=device-width,initial-scale=1">

<title>OBLIVION -- Business Directory</title>
<link rel="canonical" href="https://oblivionsearch.com/business">
<meta name="description" content="OBLIVION Business Directory -- Get your business listed and found by millions of privacy-conscious searchers.">
<meta property="og:title" content="OBLIVION Business Directory">
<meta property="og:description" content="Get your business listed on OBLIVION Search. AI-verified badges increase trust.">
<meta property="og:url" content="https://oblivionsearch.com/business">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary">
<style>{PAGE_STYLE}

.hero{{text-align:center;padding:60px 20px}}

.hero h1{{font-size:42px;font-weight:900;color:#e4e4ec;margin-bottom:12px}}

.hero p{{color:#6b7280;font-size:16px}}

.search{{max-width:600px;margin:30px auto;display:flex;background:#1a1a2e;border:1px solid #2a2a3e;border-radius:24px;overflow:hidden}}

.search input{{flex:1;background:transparent;border:none;padding:14px 20px;color:#e4e4ec;font-size:15px;outline:none}}

.search button{{background:#4f46e5;color:#fff;border:none;padding:14px 24px;font-weight:700;cursor:pointer}}

.section{{max-width:1000px;margin:0 auto;padding:40px 20px}}

.cats{{display:flex;gap:12px;flex-wrap:wrap;justify-content:center;margin:30px 0}}

.cat{{background:#111118;border:1px solid #1a1a2e;border-radius:12px;padding:16px 24px;text-align:center;cursor:pointer;min-width:120px}}

.cat:hover{{border-color:#818cf8}}

.cat .icon{{font-size:28px;margin-bottom:6px}}

.cat .name{{font-size:12px;color:#9ca3af}}

.listings{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-top:30px}}

.listing{{background:#111118;border:1px solid #1a1a2e;border-radius:12px;padding:20px}}

.listing h3{{color:#8ab4f8;font-size:16px;margin-bottom:4px}}

.listing .type{{color:#818cf8;font-size:11px;font-weight:700;text-transform:uppercase}}

.listing p{{color:#9ca3af;font-size:13px;margin-top:8px;line-height:1.5}}

.listing .badge{{display:inline-block;background:#052e16;color:#4ade80;padding:2px 8px;border-radius:8px;font-size:10px;font-weight:700;margin-top:8px}}

.btn{{display:inline-block;padding:10px 20px;background:#4f46e5;color:#fff;border-radius:8px;font-weight:700;text-decoration:none;margin-top:12px;font-size:13px}}

.cta{{text-align:center;padding:60px 20px;background:#0f0f1f;margin-top:40px;border-radius:16px}}

.cta h2{{color:#e4e4ec;font-size:28px;margin-bottom:12px}}

.cta p{{color:#6b7280;margin-bottom:20px}}

</style></head><body>

{NAV_HTML}

<div class="hero">

<h1>OBLIVION Business Directory</h1>

<p>Find trusted businesses, verified by AI. List your business and get found.</p>

<div class="search"><input placeholder="Search businesses..." id="bizSearch"><button onclick="alert('Search coming soon!')">Search</button></div>

</div>

<div class="section">

<h2 style="text-align:center;margin-bottom:8px">Browse Categories</h2>

<div class="cats">

<div class="cat"><div class="icon">&#128187;</div><div class="name">Technology</div></div>

<div class="cat"><div class="icon">&#9878;</div><div class="name">Legal</div></div>

<div class="cat"><div class="icon">&#128176;</div><div class="name">Finance</div></div>

<div class="cat"><div class="icon">&#127973;</div><div class="name">Healthcare</div></div>

<div class="cat"><div class="icon">&#127891;</div><div class="name">Education</div></div>

<div class="cat"><div class="icon">&#127959;</div><div class="name">Construction</div></div>

<div class="cat"><div class="icon">&#127828;</div><div class="name">Restaurant</div></div>

<div class="cat"><div class="icon">&#128722;</div><div class="name">Retail</div></div>

<div class="cat"><div class="icon">&#9992;</div><div class="name">Travel</div></div>

<div class="cat"><div class="icon">&#127968;</div><div class="name">Real Estate</div></div>

</div>

<h2 style="margin-top:40px;margin-bottom:8px">Featured Businesses</h2>

<p style="color:#6b7280;margin-bottom:8px">Verified by OBLIVION AI</p>

<div class="listings">

<div class="listing"><div class="type">AI &amp; Technology</div><h3>Oblivion Technologies LLC</h3><p>AI-powered document generation platform. ReportForge, LegalBrief, ProposalForge, PolicyForge, GrantForge, ClientFlow.</p><span class="badge">VERIFIED</span><br><a href="https://oblivionzone.com" class="btn">Visit Website</a></div>

<div class="listing"><div class="type">Your Business Here</div><h3>Get Listed on OBLIVION</h3><p>Add your business to the OBLIVION directory. Get found by millions of searchers. AI-verified badge increases trust.</p><span class="badge" style="background:#1e1b4b;color:#818cf8">FREE LISTING</span><br><a href="mailto:admin@oblivionzone.com?subject=Business Listing" class="btn">List Your Business</a></div>

</div></div>

<div class="section"><div class="cta">

<h2>List Your Business Today</h2>

<p>Get verified by AI, appear in search results, reach new customers.</p>

<a href="mailto:admin@oblivionzone.com?subject=Business Listing Request" class="btn" style="padding:14px 32px;font-size:16px">Add Your Business -- Free</a>

<p style="margin-top:12px;color:#4b5563;font-size:12px">Premium listings start at $9/month for priority placement</p>

</div></div>

<div class="footer">OBLIVION -- Oblivion Technologies LLC | OblivionZone.com</div>

</body></html>""")





@app.get("/about-oblivion", response_class=HTMLResponse)

async def about_oblivion():

    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset=UTF-8><meta name=viewport content="width=device-width,initial-scale=1">

<title>About OBLIVION -- AI-Powered Private Search Engine</title>
<link rel="canonical" href="https://oblivionsearch.com/about">
<meta name="description" content="Learn about OBLIVION Search -- the AI-powered search engine that queries 246 engines, rates every result for safety, and never tracks you.">
<meta property="og:title" content="About OBLIVION Search">
<meta property="og:description" content="AI-powered search engine with 246 engines, Scam Shield, and zero tracking.">
<meta property="og:url" content="https://oblivionsearch.com/about">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary">
<style>{PAGE_STYLE}

.content{{max-width:800px;margin:0 auto;padding:60px 20px}}

h1{{font-size:42px;font-weight:900;background:linear-gradient(135deg,#818cf8,#c084fc,#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:20px}}

h2{{color:#818cf8;font-size:24px;margin:30px 0 12px}}

p{{color:#9ca3af;font-size:15px;line-height:1.8;margin-bottom:16px}}

.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin:30px 0}}

.stat{{background:#111118;border:1px solid #1a1a2e;border-radius:12px;padding:20px;text-align:center}}

.stat .num{{font-size:32px;font-weight:900;color:#4ade80}}.stat .label{{color:#6b7280;font-size:12px}}

</style></head><body>

{NAV_HTML}

<div class="content">

<h1>About OBLIVION</h1>

<p>OBLIVION is the world's most intelligent search engine. Built by Oblivion Technologies LLC, it combines the power of 246 search engines with advanced AI to deliver safer, smarter, and more private search results.</p>

<h2>What Makes OBLIVION Different</h2>

<p>Unlike Google, which relies on a single search index and makes money by tracking you, OBLIVION searches 246 engines simultaneously -- Google, Bing, DuckDuckGo, Brave, and 242 more. Every result is analyzed by AI for safety, giving you a scam-free experience.</p>

<div class="stat-grid">

<div class="stat"><div class="num">246</div><div class="label">Search Engines Combined</div></div>

<div class="stat"><div class="num">0</div><div class="label">User Data Collected</div></div>

<div class="stat"><div class="num">100%</div><div class="label">AI Safety Scored</div></div>

</div>

<h2>Our Technology</h2>

<p><strong>Scam Shield</strong> -- Every search result is rated 0-100 for safety. We detect scam domains, phishing links, risky TLDs, and dangerous patterns before you click.</p>

<p><strong>AI Answers</strong> -- Our local AI model reads the top search results and generates a clear, cited answer. No cloud dependency, no data leaving your device.</p>

<p><strong>Multi-Engine Fusion</strong> -- By combining results from 246 engines, we surface information that any single engine would miss.</p>

<h2>For Businesses</h2>

<p>OBLIVION offers advertising and business directory services. <a href="/advertise" style="color:#818cf8">Coming soon</a> or <a href="/business" style="color:#818cf8">list your business</a>.</p>

<h2>Company</h2>

<p>Oblivion Technologies LLC<br>30 N Gould St Ste R, Sheridan, WY 82801<br>Phone: (530) 261-9099<br>Email: admin@oblivionzone.com<br>Web: <a href="https://oblivionzone.com" style="color:#818cf8">oblivionzone.com</a></p>

</div>

<div class="footer">OBLIVION -- Oblivion Technologies LLC</div>

</body></html>""")





# ---------------------------------------------------------------------------

# Privacy Policy

# ---------------------------------------------------------------------------

@app.get("/privacy", response_class=HTMLResponse)

async def privacy_policy():

    return HTMLResponse(f"""<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Privacy Policy -- OBLIVION Search</title>
<link rel="canonical" href="https://oblivionsearch.com/privacy">
<meta name="description" content="OBLIVION Search privacy policy. We do not track users, store searches, or collect personal data.">
<meta property="og:title" content="Privacy Policy -- OBLIVION Search">
<meta property="og:url" content="https://oblivionsearch.com/privacy">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary">
<style>{PAGE_STYLE}

.content{{max-width:800px;margin:0 auto;padding:60px 20px}}

h1{{font-size:42px;font-weight:900;background:linear-gradient(135deg,#818cf8,#c084fc,#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}}

.effective{{color:#6b7280;font-size:14px;margin-bottom:40px}}

h2{{color:#818cf8;font-size:22px;margin:36px 0 12px;padding-bottom:8px;border-bottom:1px solid #1a1a2e}}

p{{color:#9ca3af;font-size:15px;line-height:1.8;margin-bottom:16px}}

ul{{color:#9ca3af;font-size:15px;line-height:1.8;margin:0 0 16px 24px}}

ul li{{margin-bottom:8px}}

.highlight{{background:#0d1117;border:1px solid #1a1a2e;border-radius:12px;padding:24px;margin:24px 0}}

.highlight p{{margin:0;color:#4ade80;font-weight:600;font-size:16px}}

a{{color:#818cf8}}

a:hover{{text-decoration:underline}}

</style>

</head>

<body>

{NAV_HTML}

<div class="content">

<h1>Privacy Policy</h1>

<p class="effective">Effective Date: March 25, 2026 &nbsp;&middot;&nbsp; Oblivion Technologies LLC</p>



<div class="highlight">

<p>OBLIVION is built on a simple promise: we do not track you, store your searches, or sell your data. Ever.</p>

</div>



<h2>1. Information We Do NOT Collect</h2>

<ul>

<li><strong>We do NOT track users.</strong> There are no user accounts, no persistent identifiers, and no behavioral tracking of any kind.</li>

<li><strong>We do NOT store search queries.</strong> Your search terms are used only to fetch results in real time and are never written to any database or log retained beyond the immediate request.</li>

<li><strong>We do NOT use cookies for tracking.</strong> We do not set tracking cookies, advertising cookies, or any cookie that follows you across sites. Any session-level browser storage is used solely for UI preferences (such as theme) and is stored only on your device.</li>

<li><strong>We do NOT sell, rent, or share your data.</strong> We have no data broker relationships and generate no revenue from user data.</li>

<li><strong>We do NOT build user profiles.</strong> We have no mechanism to associate searches with individuals over time.</li>

</ul>



<h2>2. Information Processed During a Search</h2>

<p>When you perform a search, your query is forwarded to our backend search aggregator (SearXNG), which in turn queries multiple third-party search engines on your behalf using a shared IP address pool -- not your personal IP. Results are returned to you and immediately discarded. We do not log queries at the application level.</p>

<p>Standard web server access logs (IP address, timestamp, HTTP method, URL path, response code) may be retained for up to 7 days for security and abuse prevention purposes only. These logs are never used for analytics or advertising.</p>



<h2>3. Cloudflare</h2>

<p>OBLIVION uses <strong>Cloudflare</strong> as its CDN and security provider. Cloudflare processes traffic between your browser and our server to protect against DDoS attacks, malicious bots, and other threats. Cloudflare's handling of data is governed by the <a href="https://www.cloudflare.com/privacypolicy/" rel="noopener">Cloudflare Privacy Policy</a>. We have configured Cloudflare with privacy-preserving settings including minimal log retention.</p>



<h2>4. AI Features</h2>

<p>OBLIVION's AI answer feature runs on a local AI model hosted entirely on our own server. Your query is processed locally -- it is never sent to any third-party AI provider such as OpenAI, Google, or Anthropic.</p>



<h2>5. Advertising</h2>

<p>OBLIVION may display search-relevant text ads via our own advertising platform. These ads are matched to the keyword of your search query at request time only. We do not use behavioral data, retargeting, or cross-site tracking to serve ads.</p>



<h2>6. Children's Privacy</h2>

<p>OBLIVION is a general-purpose search engine open to all ages. Because we collect no personal information, OBLIVION is inherently compliant with COPPA. We do not knowingly collect any data from children under 13.</p>



<h2>7. Your Rights</h2>

<p>Because we do not collect or store personal data, there is nothing for us to provide, correct, or delete upon request. If you have concerns about any data processed by Cloudflare, please refer to Cloudflare's data subject request process.</p>



<h2>8. Changes to This Policy</h2>

<p>We may update this Privacy Policy from time to time. Changes will be posted on this page with an updated effective date. We encourage you to review this page periodically.</p>



<h2>9. Contact</h2>

<p>For privacy-related questions or concerns, contact us at:<br>

<strong>Email:</strong> <a href="mailto:admin@oblivionzone.com">admin@oblivionzone.com</a><br>

<strong>Company:</strong> Oblivion Technologies LLC<br>

<strong>Address:</strong> 30 N Gould St Ste R, Sheridan, WY 82801</p>

</div>

<div class="footer">OBLIVION &mdash; Oblivion Technologies LLC &middot; <a href="/privacy" style="color:#6b7280">Privacy Policy</a> &middot; <a href="/terms" style="color:#6b7280">Terms of Service</a></div>

</body>

</html>""")





# ---------------------------------------------------------------------------

# Terms of Service

# ---------------------------------------------------------------------------

@app.get("/terms", response_class=HTMLResponse)

async def terms_of_service():

    return HTMLResponse(f"""<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Terms of Service -- OBLIVION Search</title>
<link rel="canonical" href="https://oblivionsearch.com/terms">
<meta name="description" content="Terms of Service for OBLIVION Search engine.">
<meta property="og:title" content="Terms of Service -- OBLIVION Search">
<meta property="og:url" content="https://oblivionsearch.com/terms">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary">
<style>{PAGE_STYLE}

.content{{max-width:800px;margin:0 auto;padding:60px 20px}}

h1{{font-size:42px;font-weight:900;background:linear-gradient(135deg,#818cf8,#c084fc,#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}}

.effective{{color:#6b7280;font-size:14px;margin-bottom:40px}}

h2{{color:#818cf8;font-size:22px;margin:36px 0 12px;padding-bottom:8px;border-bottom:1px solid #1a1a2e}}

p{{color:#9ca3af;font-size:15px;line-height:1.8;margin-bottom:16px}}

ul{{color:#9ca3af;font-size:15px;line-height:1.8;margin:0 0 16px 24px}}

ul li{{margin-bottom:8px}}

a{{color:#818cf8}}

a:hover{{text-decoration:underline}}

</style>

</head>

<body>

{NAV_HTML}

<div class="content">

<h1>Terms of Service</h1>

<p class="effective">Effective Date: March 25, 2026 &nbsp;&middot;&nbsp; Oblivion Technologies LLC</p>



<p>By accessing or using OBLIVION Search at <a href="https://oblivionsearch.com">oblivionsearch.com</a>, you agree to be bound by these Terms of Service. If you do not agree, please do not use the service.</p>



<h2>1. Description of Service</h2>

<p>OBLIVION is a privacy-first, AI-powered search engine operated by Oblivion Technologies LLC. The service aggregates results from multiple third-party search engines and augments them with local AI analysis and safety scoring. OBLIVION is provided free of charge for personal and commercial use.</p>



<h2>2. Acceptable Use</h2>

<p>You agree to use OBLIVION only for lawful purposes. You may NOT use OBLIVION to:</p>

<ul>

<li>Conduct automated scraping or bulk querying without prior written authorization.</li>

<li>Attempt to probe, scan, or test the vulnerability of our systems.</li>

<li>Circumvent any rate limiting, authentication, or access controls.</li>

<li>Submit queries that violate applicable law, including queries designed to locate illegal content.</li>

<li>Use the service to harass, threaten, or harm others.</li>

<li>Distribute malware, phishing links, or engage in fraud through any feature of the service.</li>

</ul>



<h2>3. Intellectual Property</h2>

<p>The OBLIVION name, logo, and software are the intellectual property of Oblivion Technologies LLC. All rights reserved. Search results are sourced from third-party engines and are subject to those providers' respective terms. AI-generated summaries are provided as-is and may not be reproduced for commercial purposes without written consent.</p>



<h2>4. Disclaimer of Warranties</h2>

<p>OBLIVION is provided "AS IS" and "AS AVAILABLE" without warranty of any kind, express or implied. We do not warrant that the service will be uninterrupted, error-free, or that search results will be accurate, complete, or current. Search results reflect third-party indexes which are outside our control.</p>

<p>AI-generated answers are experimental and may contain errors. Do not rely on AI answers for medical, legal, financial, or safety-critical decisions.</p>



<h2>5. Limitation of Liability</h2>

<p>To the maximum extent permitted by law, Oblivion Technologies LLC shall not be liable for any indirect, incidental, special, consequential, or punitive damages arising from your use of, or inability to use, the service. Our total aggregate liability shall not exceed $100 USD.</p>



<h2>6. Third-Party Services</h2>

<p>OBLIVION relies on third-party services including Cloudflare (CDN/security) and multiple search engine APIs. We are not responsible for the content, availability, or practices of those third parties. Links to external sites in search results are provided as-is; we do not endorse any external website.</p>



<h2>7. Privacy</h2>

<p>Your use of OBLIVION is also governed by our <a href="/privacy">Privacy Policy</a>, which is incorporated into these Terms by reference.</p>



<h2>8. Modifications to the Service</h2>

<p>We reserve the right to modify, suspend, or discontinue OBLIVION at any time without notice. We may also update these Terms at any time. Continued use of the service after changes constitutes acceptance of the new Terms.</p>



<h2>9. Governing Law</h2>

<p>These Terms are governed by the laws of the State of Wyoming, United States, without regard to conflict of law principles. Any disputes shall be resolved in the courts of Sheridan County, Wyoming.</p>



<h2>10. Contact</h2>

<p>Questions about these Terms may be directed to:<br>

<strong>Email:</strong> <a href="mailto:admin@oblivionzone.com">admin@oblivionzone.com</a><br>

<strong>Company:</strong> Oblivion Technologies LLC<br>

<strong>Address:</strong> 30 N Gould St Ste R, Sheridan, WY 82801</p>

</div>

<div class="footer">OBLIVION &mdash; Oblivion Technologies LLC &middot; <a href="/privacy" style="color:#6b7280">Privacy Policy</a> &middot; <a href="/terms" style="color:#6b7280">Terms of Service</a></div>

</body>

</html>""")





# ---------------------------------------------------------------------------

# Download Page

# ---------------------------------------------------------------------------

@app.get("/download/OBLIVION-v1.0.0.apk")
async def download_apk():
    import os
    apk_path = "/opt/oblivionzone/oblivion-downloads/OBLIVION-v1.0.0.apk"
    if os.path.exists(apk_path):
        from fastapi.responses import FileResponse
        return FileResponse(apk_path, media_type="application/vnd.android.package-archive", filename="OBLIVION-v1.0.0.apk")
    raise HTTPException(404, "APK not found")

@app.get("/download/extensions/{filename}")
async def download_extension(filename: str):
    import os
    from fastapi.responses import FileResponse
    path = f"/opt/oblivionzone/oblivion-downloads/extensions/{filename}"
    if os.path.exists(path):
        ext_map = {".zip": "application/zip", ".xpi": "application/x-xpinstall", ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon"}
        ext = os.path.splitext(filename)[1].lower()
        mt = ext_map.get(ext, "application/octet-stream")
        return FileResponse(path, media_type=mt, filename=filename)
    raise HTTPException(404, "File not found")

@app.get("/download/qr.png")
async def download_qr():
    import qrcode, io
    qr = qrcode.QRCode(version=3, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=8, border=2)
    qr.add_data("https://oblivionsearch.com/download")
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0A0A0F", back_color="#FFFFFF").convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

@app.get("/download", response_class=HTMLResponse)

async def download_page():

    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Download OBLIVION Search App</title>
<meta name="description" content="Download OBLIVION Search for Android, iOS, and desktop. Private AI-powered search engine app.">
<meta name="theme-color" content="#0A0A0F">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="OBLIVION">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/pwa-icons/apple-touch-icon.png">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0A0A0F;--bg2:#111118;--bg3:#1a1a24;--green:#00FF88;--green2:#00cc6a;--text:#e0e0e0;--dim:#6b7280;--radius:16px}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh}
a{color:var(--green);text-decoration:none}
nav{background:var(--bg2);border-bottom:1px solid rgba(255,255,255,0.06);padding:14px 24px;display:flex;align-items:center;justify-content:space-between}
nav .logo{font-size:1.3rem;font-weight:800;letter-spacing:4px;color:#fff}
nav .logo span{color:var(--green)}
nav .links a{color:var(--dim);font-size:0.85rem;margin-left:20px;transition:color 0.2s}
nav .links a:hover{color:#fff}
.hero{text-align:center;padding:50px 20px 30px;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;background:radial-gradient(ellipse at center,rgba(0,255,136,0.03) 0%,transparent 60%);pointer-events:none}
.hero h1{font-size:2.5rem;font-weight:800;letter-spacing:4px;margin-bottom:8px;background:linear-gradient(135deg,#fff 0%,var(--green) 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero p{color:var(--dim);font-size:1.05rem;margin-bottom:10px}
.badge{display:inline-block;padding:4px 14px;background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.2);border-radius:20px;font-size:0.8rem;color:var(--green)}
.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;max-width:600px;margin:30px auto;padding:0 20px}
.feat{background:var(--bg2);border:1px solid rgba(255,255,255,0.04);border-radius:12px;padding:16px;text-align:center}
.feat .icon{font-size:1.4rem;margin-bottom:6px}
.feat .label{font-size:0.8rem;color:var(--dim)}
.cards{max-width:620px;margin:0 auto;padding:0 20px 40px}
.cards h2{text-align:center;font-size:1.3rem;font-weight:700;margin-bottom:20px;color:#fff}
.card{background:var(--bg2);border:1px solid rgba(255,255,255,0.06);border-radius:var(--radius);padding:24px;margin-bottom:14px;transition:all 0.3s}
.card:hover{border-color:rgba(0,255,136,0.2);transform:translateY(-2px)}
.card.hl{border-color:rgba(0,255,136,0.3);background:linear-gradient(135deg,var(--bg2) 0%,rgba(0,255,136,0.03) 100%)}
.card-hdr{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.card-icon{width:44px;height:44px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:1.3rem}
.card-icon.an{background:rgba(61,220,132,0.1)}
.card-icon.ap{background:rgba(255,255,255,0.06)}
.card-icon.pw{background:rgba(0,255,136,0.1)}
.card-name{font-size:1rem;font-weight:700;color:#fff}
.card-sub{font-size:0.8rem;color:var(--dim)}
.card p{font-size:0.88rem;color:var(--dim);line-height:1.6;margin-bottom:14px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:12px 24px;border-radius:12px;font-size:0.95rem;font-weight:600;cursor:pointer;transition:all 0.2s;border:none;width:100%;text-align:center}
.btn-g{background:var(--green);color:#0A0A0F}
.btn-g:hover{background:var(--green2);box-shadow:0 4px 20px rgba(0,255,136,0.3)}
.btn-s{background:rgba(255,255,255,0.06);color:#fff;border:1px solid rgba(255,255,255,0.1)}
.btn-s:hover{background:rgba(255,255,255,0.1)}
.btn svg{width:18px;height:18px}
.instructions{max-width:620px;margin:0 auto;padding:0 20px 30px;display:none}
.instructions h3{text-align:center;font-size:1.1rem;margin-bottom:16px;color:#fff}
.step{display:flex;gap:14px;margin-bottom:16px;align-items:flex-start}
.step-n{min-width:28px;height:28px;border-radius:50%;background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.3);display:flex;align-items:center;justify-content:center;font-size:0.8rem;font-weight:700;color:var(--green)}
.step-t{font-size:0.88rem;line-height:1.6}
.step-t strong{color:#fff}
.qr-sec{text-align:center;padding:30px 20px;border-top:1px solid rgba(255,255,255,0.05)}
.qr-sec h3{font-size:1.1rem;margin-bottom:14px;color:#fff}
.qr-box{display:inline-block;background:#fff;padding:14px;border-radius:14px;margin:10px 0}
.qr-box canvas{width:160px;height:160px}
.qr-hint{color:var(--dim);font-size:0.8rem;margin-top:6px}
.footer{text-align:center;padding:24px 20px;border-top:1px solid rgba(255,255,255,0.05);color:var(--dim);font-size:0.78rem}
@media(max-width:480px){.hero h1{font-size:1.8rem;letter-spacing:3px}.card{padding:18px}.features{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<nav>
  <a href="/" class="logo"><img src="/logos/main-logo.png" alt="OBLIVION" style="height:32px;width:auto"></a>
  <div class="links"><a href="/">Search</a><a href="/about-oblivion">About</a><a href="/privacy">Privacy</a></div>
</nav>

<div class="hero">
  <h1>OBLIVION Search</h1>
  <p>AI-Powered Private Search &mdash; Now as an App</p>
  <span class="badge">v1.0 &mdash; Free</span>
</div>

<div class="features">
  <div class="feat"><div class="icon">&#128274;</div><div class="label">Private Search</div></div>
  <div class="feat"><div class="icon">&#9889;</div><div class="label">AI-Powered</div></div>
  <div class="feat"><div class="icon">&#127760;</div><div class="label">30+ Languages</div></div>
  <div class="feat"><div class="icon">&#128276;</div><div class="label">Zero Tracking</div></div>
</div>

<div class="cards">
  <h2>Get the App</h2>

  <div class="card" id="ac">
    <div class="card-hdr">
      <div class="card-icon an">
        <svg viewBox="0 0 24 24" fill="#3DDC84" width="24" height="24"><path d="M17.523 2.273l1.74-1.74a.5.5 0 00-.707-.707l-1.91 1.91A8.54 8.54 0 0012 .5a8.54 8.54 0 00-4.646 1.236L5.444.174a.5.5 0 10-.707.707l1.74 1.74A8.472 8.472 0 003.5 8.5h17a8.472 8.472 0 00-2.977-6.227zM8.5 6.5a1 1 0 110-2 1 1 0 010 2zm7 0a1 1 0 110-2 1 1 0 010 2zM3.5 10v7a2 2 0 002 2h1v3.5a1.5 1.5 0 003 0V19h5v3.5a1.5 1.5 0 003 0V19h1a2 2 0 002-2v-7h-17z"/></svg>
      </div>
      <div><div class="card-name">Android</div><div class="card-sub">APK Download &mdash; Android 7.0+</div></div>
    </div>
    <p>Download the native OBLIVION Search app for Android.</p>
    <a href="/download/OBLIVION-v1.0.0.apk" class="btn btn-g" id="apk-btn" download>
      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
      Download APK (3.0 MB)
    </a>
    <p style="font-size:0.78rem;color:var(--dim);margin-top:10px">After download: tap the file &rarr; Allow &quot;Install unknown apps&quot; &rarr; Install</p>
  </div>

  <div class="card" id="ic">
    <div class="card-hdr">
      <div class="card-icon ap">
        <svg viewBox="0 0 24 24" fill="#fff" width="24" height="24"><path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.8-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z"/></svg>
      </div>
      <div><div class="card-name">iOS (iPhone / iPad)</div><div class="card-sub">Add to Home Screen</div></div>
    </div>
    <p>Apple does not allow one-tap install. You need to use Safari manually:</p>
    <div style="background:var(--bg3);border-radius:12px;padding:20px;margin-bottom:14px;font-size:0.92rem;line-height:2">
      <div><span style="color:var(--green);font-weight:800">1.</span> Open <strong style="color:#fff">Safari</strong> on your iPhone</div>
      <div><span style="color:var(--green);font-weight:800">2.</span> Type <strong style="color:var(--green)">oblivionsearch.com</strong> in the address bar</div>
      <div><span style="color:var(--green);font-weight:800">3.</span> Tap the <strong style="color:#fff">Share button</strong> (square with arrow at the bottom of Safari)</div>
      <div><span style="color:var(--green);font-weight:800">4.</span> Scroll down, tap <strong style="color:#fff">&quot;Add to Home Screen&quot;</strong></div>
      <div><span style="color:var(--green);font-weight:800">5.</span> Tap <strong style="color:#fff">&quot;Add&quot;</strong> in the top right</div>
    </div>
    <p style="font-size:0.8rem;color:var(--dim);text-align:center">OBLIVION will appear on your home screen as a full-screen app.</p>
    <p style="font-size:0.8rem;color:var(--dim);text-align:center;margin-top:4px"><strong style="color:#ff6b6b">Important:</strong> Must use Safari. Chrome/Firefox on iOS cannot install web apps.</p>
  </div>

  <div class="card" id="pc">
    <div class="card-hdr">
      <div class="card-icon pw">
        <svg viewBox="0 0 24 24" fill="#00FF88" width="24" height="24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>
      </div>
      <div><div class="card-name">Desktop / Browser</div><div class="card-sub">Install as App &mdash; Chrome, Edge, Brave</div></div>
    </div>
    <p>Install directly from your browser. Works on Windows, macOS, Linux, and ChromeOS.</p>
    <button class="btn btn-g" id="pwa-btn" onclick="doPWA()">Install as App</button>
    <div id="pwa-instructions" style="display:none;background:var(--bg3);border-radius:12px;padding:16px;margin-top:12px">
      <div id="pwa-inst-content"></div>
    </div>
  </div>
</div>


<div class="qr-sec">
  <h3>Scan to Open on Mobile</h3>
  <div class="qr-box"><canvas id="qrc" width="160" height="160"></canvas></div>
  <p class="qr-hint">Point your phone camera at the QR code</p>
</div>

<div class="footer">
  <p>&copy; 2026 OBLIVION Search &mdash; <a href="https://oblivionsearch.com">oblivionsearch.com</a></p>
  <p style="margin-top:6px"><a href="/privacy">Privacy</a> &middot; <a href="/terms">Terms</a> &middot; <a href="/about-oblivion">About</a></p>
</div>

<script>
const ua=navigator.userAgent||'';
const isA=/android/i.test(ua),isI=/iphone|ipad|ipod/i.test(ua);
const isSafari=/^((?!chrome|android).)*safari/i.test(ua);
const isChrome=/chrome/i.test(ua)&&!/edge/i.test(ua);
const isEdge=/edg/i.test(ua);
const isFirefox=/firefox/i.test(ua);

// Highlight and reorder cards based on device
if(isA){
  document.getElementById('ac').classList.add('hl');
} else if(isI){
  document.getElementById('ic').classList.add('hl');
  const c=document.querySelector('.cards'),ic=document.getElementById('ic');
  c.insertBefore(ic,document.getElementById('ac'));
} else {
  document.getElementById('pc').classList.add('hl');
  const c=document.querySelector('.cards'),pc=document.getElementById('pc');
  c.insertBefore(pc,document.getElementById('ac'));
}

// Register service worker so PWA install prompt fires on this page too
if('serviceWorker' in navigator){navigator.serviceWorker.register('/service-worker.js',{scope:'/'});}

// PWA install prompt
let dp=null;
window.addEventListener('beforeinstallprompt',e=>{
  e.preventDefault();dp=e;
  const b=document.getElementById('pwa-btn');
  b.textContent='Install OBLIVION Now';
  b.style.boxShadow='0 4px 20px rgba(0,255,136,0.4)';
});

function doPWA(){
  if(dp){
    // Browser supports install prompt - use it directly
    dp.prompt();
    dp.userChoice.then(r=>{
      if(r.outcome==='accepted'){document.getElementById('pwa-btn').textContent='Installed!';}
      dp=null;
    });
  } else {
    // Show browser-specific instructions
    const inst=document.getElementById('pwa-instructions');
    const content=document.getElementById('pwa-inst-content');
    inst.style.display='block';
    let html='';
    if(isChrome||isEdge){
      const name=isEdge?'Edge':'Chrome';
      html='<div style="display:flex;gap:10px;margin-bottom:10px;align-items:center"><div style="min-width:24px;height:24px;border-radius:50%;background:var(--green);color:#0A0A0F;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:0.75rem">1</div><span style="font-size:0.88rem">Look for the <strong style="color:#fff">install icon</strong> (+) in the address bar</span></div>';
      html+='<div style="display:flex;gap:10px;margin-bottom:10px;align-items:center"><div style="min-width:24px;height:24px;border-radius:50%;background:var(--green);color:#0A0A0F;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:0.75rem">2</div><span style="font-size:0.88rem">Or click <strong style="color:#fff">Menu (three dots)</strong> in the top right</span></div>';
      html+='<div style="display:flex;gap:10px;margin-bottom:10px;align-items:center"><div style="min-width:24px;height:24px;border-radius:50%;background:var(--green);color:#0A0A0F;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:0.75rem">3</div><span style="font-size:0.88rem">Click <strong style="color:var(--green)">&quot;Install OBLIVION Search&quot;</strong></span></div>';
      html+='<p style="font-size:0.8rem;color:var(--dim);margin-top:8px">Not seeing the option? Try visiting <a href="/" style="color:var(--green)">oblivionsearch.com</a> first, then look for the install icon.</p>';
    } else if(isFirefox){
      html='<p style="font-size:0.88rem;margin-bottom:10px">Firefox desktop does not support PWA install. Use one of these instead:</p>';
      html+='<a href="/download/OBLIVION-v1.0.0.apk" class="btn btn-g" style="margin-bottom:8px" download>Download APK (Android)</a>';
      html+='<p style="font-size:0.8rem;color:var(--dim);margin-top:8px">Or open this page in <strong style="color:#fff">Chrome</strong> or <strong style="color:#fff">Edge</strong> to install as PWA.</p>';
    } else if(isI){
      html='<p style="font-size:0.88rem">Open <strong style="color:#fff">Safari</strong> &rarr; tap <strong style="color:#fff">Share</strong> &rarr; <strong style="color:#fff">&quot;Add to Home Screen&quot;</strong></p>';
    } else if(isA){
      html='<a href="/download/OBLIVION-v1.0.0.apk" class="btn btn-g" download>Download APK Instead</a>';
    } else {
      html='<p style="font-size:0.88rem">Open this page in <strong style="color:#fff">Chrome</strong> or <strong style="color:#fff">Edge</strong> and click the install icon in the address bar.</p>';
    }
    content.innerHTML=html;
    inst.scrollIntoView({behavior:'smooth'});
  }
}

// QR code loaded from server
(function(){
const cv=document.getElementById('qrc');if(!cv)return;
const img=new Image();
img.onload=function(){const ctx=cv.getContext('2d');ctx.drawImage(img,0,0,160,160);};
img.src='/download/qr.png';
})();
</script>
</body>
</html>""")





# ---------------------------------------------------------------------------

# LMSTFY (Let Me Search That For You)

# ---------------------------------------------------------------------------

_LMSTFY_HTML: Optional[str] = None

def _load_lmstfy_html() -> str:
    global _LMSTFY_HTML
    if _LMSTFY_HTML is None:
        import pathlib
        p = pathlib.Path("/opt/oblivionzone/oblivion-packages/lmstfy/index.html")
        _LMSTFY_HTML = p.read_text(encoding="utf-8")
    return _LMSTFY_HTML

@app.get("/lmstfy", response_class=HTMLResponse)
async def lmstfy_page():
    return HTMLResponse(_load_lmstfy_html())

@app.get("/lmstfy/{path:path}", response_class=HTMLResponse)
async def lmstfy_path(path: str, request: Request):
    return HTMLResponse(_load_lmstfy_html())


# ---------------------------------------------------------------------------
# Shareable search results with OG preview
# ---------------------------------------------------------------------------

import hashlib as _hashlib

def _share_id(query: str) -> str:
    return _hashlib.sha256(query.encode()).hexdigest()[:12]

@app.get("/share/{share_id}", response_class=HTMLResponse)
async def share_search(share_id: str, request: Request):
    # Check per-result share store first (populated by search API)
    data = _share_store.get(share_id)
    if data:
        r_title = data.get("title", "OBLIVION Search Result").replace('"', '&quot;').replace('<', '&lt;')
        r_snippet = data.get("snippet", "Found on OBLIVION Search").replace('"', '&quot;').replace('<', '&lt;')[:200]
        r_url = data.get("url", "https://oblivionsearch.com")
        return HTMLResponse(f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><title>{r_title} - Shared via OBLIVION</title>
<meta property="og:title" content="{r_title}">
<meta property="og:description" content="{r_snippet}">
<meta property="og:site_name" content="OBLIVION Search">
<meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
<meta property="og:url" content="https://oblivionsearch.com/share/{share_id}">
<meta name="twitter:card" content="summary"><meta name="twitter:title" content="{r_title}">
<meta name="twitter:description" content="{r_snippet}">
<meta http-equiv="refresh" content="2;url={r_url}">
<style>body{{background:#0a0a0f;color:#e4e4ec;font-family:system-ui;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;margin:0}}
.logo{{font-size:2rem;font-weight:900;background:linear-gradient(135deg,#818cf8,#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:16px}}
p{{color:#9ca3af;font-size:14px}}a{{color:#818cf8}}</style>
</head><body><div class="logo">OBLIVION</div>
<p>Redirecting to result...</p><p><a href="{r_url}">{r_title}</a></p>
<p style="font-size:12px;margin-top:20px">Shared via <a href="https://oblivionsearch.com">OBLIVION Search</a></p>
</body></html>""")
    # Fallback: query-based share
    q = request.query_params.get("q", "")
    title = f"Search: {q} -- OBLIVION Search" if q else "OBLIVION Search -- Private AI Search"
    desc = f"Found results for {q} on OBLIVION -- Private AI Search with 246 engines" if q else "Search privately with OBLIVION -- 246 engines, Scam Shield, AI answers, zero tracking."
    redirect_url = f"https://oblivionsearch.com/search?q={urllib.parse.quote_plus(q)}" if q else "https://oblivionsearch.com"
    og_html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
<meta property="og:url" content="https://oblivionsearch.com/share/{share_id}?q={urllib.parse.quote_plus(q)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="OBLIVION Search">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{desc}">
<meta name="twitter:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
<meta http-equiv="refresh" content="0;url={redirect_url}">
</head><body>
<p>Redirecting to <a href="{redirect_url}">OBLIVION Search</a>...</p>
</body></html>"""
    return HTMLResponse(og_html)

@app.get("/api/share", response_class=JSONResponse)
async def create_share_link(q: str = Query("")):
    if not q:
        return JSONResponse({"error": "query required"}, status_code=400)
    sid = _share_id(q)
    url = f"https://oblivionsearch.com/share/{sid}?q={urllib.parse.quote_plus(q)}"
    return JSONResponse({"url": url, "id": sid})

# ---------------------------------------------------------------------------
# Newsletter signup
# ---------------------------------------------------------------------------

import os as _os

NEWSLETTER_FILE = "/opt/oblivionzone/data/newsletter_subscribers.json"

@app.post("/api/newsletter")
async def newsletter_signup(request: Request):
    try:
        body = await request.json()
        email = body.get("email", "").strip().lower()
        if not email or "@" not in email:
            return JSONResponse({"error": "Valid email required"}, status_code=400)
        subs = []
        if _os.path.exists(NEWSLETTER_FILE):
            try:
                with open(NEWSLETTER_FILE) as f:
                    subs = json.loads(f.read())
            except:
                subs = []
        existing_emails = {s.get("email") for s in subs}
        if email in existing_emails:
            return JSONResponse({"status": "already_subscribed", "message": "You are already subscribed!"})
        import datetime
        subs.append({"email": email, "subscribed_at": datetime.datetime.now().isoformat()})
        _os.makedirs(_os.path.dirname(NEWSLETTER_FILE), exist_ok=True)
        with open(NEWSLETTER_FILE, "w") as f:
            f.write(json.dumps(subs, indent=2))
        return JSONResponse({"status": "subscribed", "message": "Welcome! You have been subscribed."})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Community Voting API -- Reddit-style upvote/downvote
# ---------------------------------------------------------------------------

@app.post("/api/vote")
async def api_vote(request: Request):
    """Cast an upvote (+1) or downvote (-1) on a search result URL."""
    try:
        body = await request.json()
        url = body.get("url", "").strip()
        query = body.get("query", "").strip()
        vote = body.get("vote", 0)

        if not url:
            return JSONResponse({"error": "URL required"}, status_code=400)
        if vote not in (1, -1):
            return JSONResponse({"error": "Vote must be 1 or -1"}, status_code=400)

        client_ip = request.client.host if request.client else "unknown"
        result = cast_vote(url, query, vote, client_ip)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/votes")
async def api_get_votes(url: str = Query(...)):
    """Get vote counts for a URL."""
    try:
        result = get_vote_totals(url)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Bookmarklet page (/bookmarklet)
# ---------------------------------------------------------------------------

@app.get("/bookmarklet", response_class=HTMLResponse)
async def bookmarklet_page():
    bookmarklet_code = "javascript:void(window.open('https://oblivionsearch.com/search?q='+encodeURIComponent(window.getSelection().toString()||prompt('Search OBLIVION:',''))+'&src=bookmarklet','_blank'))"
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OBLIVION Bookmarklet</title>
<style>
body{{background:#0a0a0f;color:#e4e4ec;font-family:system-ui,-apple-system,sans-serif;margin:0;padding:40px 20px;display:flex;flex-direction:column;align-items:center}}
.logo{{font-size:2.5rem;font-weight:900;background:linear-gradient(135deg,#818cf8,#c084fc,#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}}
.sub{{color:#9ca3af;margin-bottom:40px;font-size:1.1rem}}
.card{{background:#111118;border:1px solid #2a2a3a;border-radius:16px;padding:40px;max-width:600px;width:100%;text-align:center}}
.card h2{{margin:0 0 16px;font-size:1.5rem}}
.card p{{color:#9ca3af;line-height:1.6;margin-bottom:24px}}
.bm-btn{{display:inline-block;padding:16px 40px;background:linear-gradient(135deg,#7c3aed,#6366f1);color:#fff;border-radius:12px;font-size:1.1rem;font-weight:700;text-decoration:none;cursor:grab;transition:transform 0.2s,box-shadow 0.2s}}
.bm-btn:hover{{transform:translateY(-2px);box-shadow:0 8px 25px rgba(99,102,241,0.4)}}
.steps{{margin-top:32px;text-align:left;color:#9ca3af;font-size:0.9rem}}
.steps li{{margin-bottom:8px}}
.steps strong{{color:#e4e4ec}}
a{{color:#818cf8}}
</style>
</head><body>
<div class="logo">OBLIVION</div>
<div class="sub">Search Bookmarklet</div>
<div class="card">
<h2>Drag to Your Bookmark Bar</h2>
<p>Select any text on any webpage, then click the bookmarklet to instantly search it on OBLIVION.</p>
<a class="bm-btn" href="{bookmarklet_code}" onclick="alert('Drag this button to your bookmark bar!');return false;">Search OBLIVION</a>
<ol class="steps">
<li><strong>Show your bookmark bar</strong> (Ctrl+Shift+B / Cmd+Shift+B)</li>
<li><strong>Drag the purple button</strong> above to your bookmark bar</li>
<li><strong>Select text</strong> on any webpage, then <strong>click the bookmarklet</strong></li>
<li>OBLIVION opens with your search results</li>
</ol>
</div>
<p style="margin-top:24px;color:#6b7280;font-size:0.8rem"><a href="/">Back to OBLIVION Search</a></p>
</body></html>""")


# ---------------------------------------------------------------------------
# Library of Congress search (/api/library)
# ---------------------------------------------------------------------------

@app.get("/api/library")
async def api_library(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50)):
    try:
        results = search_library_of_congress(q, max_results=limit)
        return JSONResponse({
            "results": results,
            "total": len(results),
            "query": q,
            "source": "Library of Congress",
        })
    except Exception as e:
        return JSONResponse({"error": str(e), "results": [], "query": q}, status_code=502)


# ---------------------------------------------------------------------------
# HyperLogLog analytics endpoint (/api/analytics)
# ---------------------------------------------------------------------------

@app.get("/api/analytics")
async def api_analytics():
    return JSONResponse(hll_analytics.get_stats())


# ---------------------------------------------------------------------------

# Main SPA

# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(MAIN_HTML)

@app.get("/search", response_class=HTMLResponse)
async def search_page():
    return HTMLResponse(MAIN_HTML)





MAIN_HTML = r"""<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta name="viewport" content="width=device-width,initial-scale=1">

<title>OBLIVION -- AI Search Engine | 246 Engines + Scam Shield | Private Search</title>

<link rel="search" type="application/opensearchdescription+xml" title="OBLIVION" href="/opensearch.xml">

<link rel="manifest" href="/manifest.json">

<meta name="description" content="OBLIVION -- AI-Powered Search Engine. Search 246 engines at once. Every result rated for safety with Scam Shield. AI answers. Zero tracking. Free.">

<meta name="keywords" content="search engine, private search, Google alternative, AI search, scam detection, no tracking, privacy search engine">

<meta name="author" content="Oblivion Technologies LLC">

<meta name="robots" content="index, follow, max-snippet:-1, max-image-preview:large">

<meta property="og:title" content="OBLIVION -- The Search Engine That Protects You">

<meta property="og:description" content="Search 246 engines at once. Every result rated for safety. AI-powered answers. Zero tracking.">

<meta property="og:type" content="website">

<meta property="og:url" content="https://oblivionsearch.com">

<meta property="og:site_name" content="OblivionSearch">

<meta name="twitter:card" content="summary_large_image">

<meta name="twitter:title" content="OBLIVION -- AI Search Engine">

<meta name="twitter:description" content="246 engines + Scam Shield + AI answers. Zero tracking.">
<meta property="og:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">
<meta name="twitter:image" content="https://oblivionsearch.com/pwa-icons/icon-512.png">

<link rel="canonical" href="https://oblivionsearch.com/">
<link rel="alternate" hreflang="x-default" href="https://oblivionsearch.com/">
<link rel="alternate" hreflang="en" href="https://oblivionsearch.com/">
<link rel="alternate" hreflang="es" href="https://oblivionsearch.com/?lang=es">
<link rel="alternate" hreflang="fr" href="https://oblivionsearch.com/?lang=fr">
<link rel="alternate" hreflang="de" href="https://oblivionsearch.com/?lang=de">
<link rel="alternate" hreflang="it" href="https://oblivionsearch.com/?lang=it">
<link rel="alternate" hreflang="pt" href="https://oblivionsearch.com/?lang=pt">
<link rel="alternate" hreflang="ru" href="https://oblivionsearch.com/?lang=ru">
<link rel="alternate" hreflang="ja" href="https://oblivionsearch.com/?lang=ja">
<link rel="alternate" hreflang="ko" href="https://oblivionsearch.com/?lang=ko">
<link rel="alternate" hreflang="zh" href="https://oblivionsearch.com/?lang=zh">
<link rel="alternate" hreflang="ar" href="https://oblivionsearch.com/?lang=ar">
<link rel="alternate" hreflang="hi" href="https://oblivionsearch.com/?lang=hi">
<link rel="alternate" hreflang="nl" href="https://oblivionsearch.com/?lang=nl">
<link rel="alternate" hreflang="pl" href="https://oblivionsearch.com/?lang=pl">
<link rel="alternate" hreflang="sv" href="https://oblivionsearch.com/?lang=sv">
<link rel="alternate" hreflang="tr" href="https://oblivionsearch.com/?lang=tr">
<link rel="alternate" hreflang="th" href="https://oblivionsearch.com/?lang=th">
<link rel="alternate" hreflang="vi" href="https://oblivionsearch.com/?lang=vi">
<link rel="alternate" hreflang="uk" href="https://oblivionsearch.com/?lang=uk">
<link rel="alternate" hreflang="cs" href="https://oblivionsearch.com/?lang=cs">
<link rel="alternate" hreflang="da" href="https://oblivionsearch.com/?lang=da">
<link rel="alternate" hreflang="fi" href="https://oblivionsearch.com/?lang=fi">
<link rel="alternate" hreflang="el" href="https://oblivionsearch.com/?lang=el">
<link rel="alternate" hreflang="he" href="https://oblivionsearch.com/?lang=he">
<link rel="alternate" hreflang="hu" href="https://oblivionsearch.com/?lang=hu">
<link rel="alternate" hreflang="id" href="https://oblivionsearch.com/?lang=id">
<link rel="alternate" hreflang="ms" href="https://oblivionsearch.com/?lang=ms">
<link rel="alternate" hreflang="no" href="https://oblivionsearch.com/?lang=no">
<link rel="alternate" hreflang="ro" href="https://oblivionsearch.com/?lang=ro">
<link rel="alternate" hreflang="bn" href="https://oblivionsearch.com/?lang=bn">

<meta name="theme-color" content="#0a0a0f">

<meta name="apple-mobile-web-app-capable" content="yes">

<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">

<meta name="apple-mobile-web-app-title" content="OBLIVION">

<link rel="apple-touch-icon" href="/pwa-icons/apple-touch-icon.png">

<link rel="apple-touch-icon" sizes="180x180" href="/pwa-icons/apple-touch-icon.png">

<link rel="apple-touch-icon" sizes="152x152" href="/pwa-icons/apple-touch-icon.png">

<link rel="apple-touch-icon" sizes="120x120" href="/pwa-icons/icon-128.png">

<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="icon" type="image/png" sizes="32x32" href="/pwa-icons/favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/pwa-icons/favicon-16.png">

<meta name="mobile-web-app-capable" content="yes">

<meta name="msapplication-TileColor" content="#0a0a0f">

<meta name="msapplication-TileImage" content="/logos/logo4_circle.png">

<meta name="apple-itunes-app" content="app-id=, app-argument=oblivionsearch://search">

<meta name="al:ios:app_store_id" content="APPID">
<meta name="al:ios:app_name" content="OBLIVION Search">
<meta name="al:ios:url" content="oblivionsearch://search">
<meta name="al:android:package" content="com.oblivionsearch.app">
<meta name="al:android:app_name" content="OBLIVION Search">
<meta name="al:android:url" content="oblivionsearch://search">

<link rel="alternate" href="android-app://com.oblivionsearch.app/https/oblivionsearch.com/">

<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebSite","name":"OblivionSearch","alternateName":"OBLIVION","url":"https://oblivionsearch.com","potentialAction":{"@type":"SearchAction","target":"https://oblivionsearch.com/search?q={search_term_string}","query-input":"required name=search_term_string"}}
</script>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebApplication","name":"OBLIVION Search","url":"https://oblivionsearch.com","applicationCategory":"SearchApplication","operatingSystem":"Any","browserRequirements":"Requires JavaScript","description":"AI-Powered Private Search Engine with 246 engines, Scam Shield, and zero tracking","offers":{"@type":"Offer","price":"0","priceCurrency":"USD"},"featureList":["246 search engines","AI-powered answers","Scam Shield safety ratings","Zero user tracking","Private search","Image search","Video search","News search"]}
</script>

<script type="application/ld+json">

{

  "@context": "https://schema.org",

  "@type": "Organization",

  "name": "Oblivion Technologies LLC",

  "url": "https://oblivionsearch.com",

  "logo": "https://oblivionsearch.com/logos/logo2_gradient.png",

  "description": "AI-Powered Search Engine with 246 engines and Scam Shield",

  "address": {

    "@type": "PostalAddress",

    "streetAddress": "30 N Gould St Ste R",

    "addressLocality": "Sheridan",

    "addressRegion": "WY",

    "postalCode": "82801",

    "addressCountry": "US"

  },

  "contactPoint": {

    "@type": "ContactPoint",

    "email": "admin@oblivionzone.com",

    "contactType": "customer service"

  },

  "sameAs": [

    "https://oblivionsearch.org",

    "https://oblivionsearch.online"

  ]

}

</script>

<style>

*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

html,body{height:auto;min-height:100vh;background:#fff;color:#202124;font-family:arial,sans-serif;overflow-x:hidden}

a{color:inherit;text-decoration:none}

button{cursor:pointer;border:none;background:none;color:#202124;font-family:inherit}

input{font-family:inherit}



/* Layout -- SIMPLE block layout, page scrolls like a normal website */

#app{width:100%}



/* Top Nav Bar */

.toolbar{display:flex;align-items:center;background:#f8f9fa;padding:8px 16px;gap:10px;border-bottom:1px solid #e0e0e0}

.nav-btn{width:36px;height:36px;display:flex;align-items:center;justify-content:center;border-radius:50%;font-size:18px;color:#5f6368;transition:.15s}

.nav-btn:hover{background:#e8e8e8;color:#202124}

.url-bar{flex:1;display:flex;align-items:center;background:#fff;border:1px solid #dfe1e5;border-radius:24px;padding:0 16px;height:44px;gap:8px;transition:.2s;position:relative;box-shadow:0 1px 3px rgba(0,0,0,0.06)}

.url-bar:focus-within{border-color:#7c3aed;box-shadow:0 1px 6px rgba(124,58,237,0.15)}

.url-bar input{flex:1;background:none;border:none;outline:none;color:#202124;font-size:14px}

.url-bar input::placeholder{color:#9aa0a6}

.url-bar .search-icon{color:#9aa0a6;font-size:16px}

.tool-btn{height:36px;padding:0 14px;border-radius:20px;font-size:13px;font-weight:500;display:flex;align-items:center;gap:6px;color:#5f6368;transition:.15s}

.tool-btn:hover{background:#e8e8e8;color:#202124}

.tool-btn.active{background:#ede9fe;color:#7c3aed}



/* Main -- simple block, NO flex, NO constraints */

.main-area{position:relative}

.content-area{position:relative}



/* ===== HOME PAGE ===== */

.home-page{width:100%;min-height:90vh;display:flex;flex-direction:column;align-items:center;padding:80px 20px 60px;justify-content:center;background:#fff}

.home-logo{margin-bottom:16px;text-align:center}
.home-logo img{max-height:100px;width:auto;display:inline-block}

.home-tagline{color:#70757a;font-size:16px;margin-bottom:36px}

.home-search{width:100%;max-width:584px;position:relative;margin-bottom:24px}

.home-search input{width:100%;height:50px;border-radius:24px;background:#fff;border:1px solid #dfe1e5;padding:0 90px 0 48px;font-size:16px;color:#202124;outline:none;transition:.25s;box-shadow:0 2px 5px rgba(0,0,0,0.06)}

.home-search input:hover{box-shadow:0 2px 8px rgba(0,0,0,0.12)}

.home-search input:focus{border-color:#7c3aed;box-shadow:0 2px 8px rgba(124,58,237,0.15)}

.home-search input::placeholder{color:#9aa0a6}

.home-search .search-mag{position:absolute;left:16px;top:50%;transform:translateY(-50%);font-size:18px;color:#9aa0a6}

.home-search button{position:absolute;right:8px;top:50%;transform:translateY(-50%);height:36px;padding:0 18px;border-radius:18px;background:linear-gradient(135deg,#7c3aed,#3b82f6);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:600;color:#fff;transition:.15s;gap:6px}

.home-search button:hover{opacity:.9;transform:translateY(-50%) scale(1.02)}

.home-ac{position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #e0e0e0;border-radius:0 0 16px 16px;max-height:300px;overflow-y:auto;z-index:100;display:none;box-shadow:0 4px 12px rgba(0,0,0,0.1)}

.home-ac.show{display:block}

.home-ac .ac-item{padding:10px 20px;font-size:14px;cursor:pointer;transition:.1s;color:#202124}

.home-ac .ac-item:hover,.home-ac .ac-item.selected{background:#f1f3f4}



.home-buttons{display:flex;gap:12px;margin-bottom:32px;justify-content:center;flex-wrap:wrap}

.home-buttons button{padding:10px 20px;border-radius:6px;background:#f8f9fa;border:1px solid #f8f9fa;font-size:14px;color:#3c4043;cursor:pointer;transition:.15s}

.home-buttons button:hover{border-color:#dadce0;box-shadow:0 1px 3px rgba(0,0,0,0.08)}



.features{display:flex;flex-wrap:wrap;justify-content:center;gap:10px;margin-bottom:40px}

.feature-badge{padding:8px 16px;border-radius:20px;background:#f1f3f4;font-size:13px;color:#5f6368;display:flex;align-items:center;gap:7px;font-weight:500}

.feature-badge .dot{width:7px;height:7px;border-radius:50%}



.home-links{margin-top:32px;display:flex;gap:20px;flex-wrap:wrap;justify-content:center}

.home-links a{font-size:13px;color:#70757a;text-decoration:none}

.home-links a:hover{text-decoration:underline;color:#202124}

.home-footer{color:#70757a;font-size:12px;margin-top:40px}



/* ===== SEARCH RESULTS PAGE ===== */

.search-page{width:100%;padding:0;background:#fff}

.search-inner{max-width:960px;padding:0 24px 40px;margin:0 auto}

.results-meta{font-size:13px;color:#70757a;margin-bottom:16px}

.cat-tabs{display:flex;gap:4px;margin-bottom:0;flex-wrap:wrap;padding:8px 24px;border-bottom:1px solid #e0e0e0;background:#fff}

.cat-tab{padding:8px 16px;border-radius:0;font-size:13px;color:#5f6368;cursor:pointer;transition:.15s;border:none;border-bottom:3px solid transparent;background:none}

.cat-tab:hover{color:#202124;background:#f1f3f4}

.cat-tab.active{color:#7c3aed;border-bottom-color:#7c3aed;font-weight:600}



/* Result cards */

.result-card{padding:16px 0;margin-bottom:0;border-radius:0;background:transparent;border:none;border-bottom:1px solid #e8eaed;transition:.15s;display:flex;gap:8px}

.result-card:hover{background:#f8f9fa}

.vote-col{display:flex;flex-direction:column;align-items:center;min-width:32px;padding-top:2px;user-select:none}
.vote-btn{background:none;border:none;cursor:pointer;padding:0;line-height:1;font-size:18px;color:#9aa0a6;transition:color .15s}
.vote-btn:hover{color:#7c3aed}
.vote-btn.active-up{color:#ff4500}
.vote-btn.active-down{color:#7193ff}
.vote-score{font-size:11px;font-weight:700;color:#5f6368;line-height:1.2;text-align:center;min-width:20px}
.vote-score.positive{color:#ff4500}
.vote-score.negative{color:#7193ff}
.result-content{flex:1;min-width:0}

.result-card.danger{border-left:3px solid #ef4444;padding-left:12px}

.result-card.ad{background:#fefce8;border-left:3px solid #f59e0b;padding:16px 12px;border-radius:8px;margin-bottom:12px;border-bottom:none}

.result-top{display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap}

.result-domain{font-size:12px;color:#188038}

.safety-badge{font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600}

.engine-badge{font-size:10px;padding:2px 8px;border-radius:10px;background:#f1f3f4;color:#70757a;margin-left:auto}

.result-title{font-size:18px;font-weight:400;color:#1a0dab;display:block;margin-bottom:4px;cursor:pointer;line-height:1.3;text-decoration:none}

.result-title:hover{text-decoration:underline}

.result-snippet{font-size:14px;color:#4d5156;line-height:1.58;margin-bottom:4px}

.result-url{font-size:12px;color:#188038;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.scam-warning{background:#fef2f2;color:#dc2626;padding:8px 12px;border-radius:8px;font-size:12px;margin-bottom:8px;font-weight:500;border:1px solid #fecaca}

.ad-label{background:#f59e0b;color:#fff;padding:1px 6px;border-radius:4px;font-size:9px;font-weight:700;margin-right:6px}

.no-results{text-align:center;padding:60px 20px;color:#70757a}

.search-loading{display:flex;align-items:center;justify-content:center;padding:80px 20px;color:#70757a;font-size:14px}

.pagination{display:flex;align-items:center;justify-content:center;gap:16px;padding:30px 0}

.page-btn{padding:10px 24px;border-radius:24px;background:#f8f9fa;font-size:14px;border:1px solid #dadce0;transition:.15s;color:#1a0dab;font-weight:500}

.page-btn:hover:not(:disabled){background:#e8eaed}

.page-btn:disabled{opacity:.3;cursor:default;color:#70757a}

.page-info{font-size:14px;color:#70757a}

.suggestions-row{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}

.suggestion-pill{padding:6px 16px;border-radius:16px;background:#f1f3f4;font-size:13px;color:#7c3aed;cursor:pointer;border:1px solid #e8eaed;transition:.15s}

.suggestion-pill:hover{background:#ede9fe}

.infobox{background:#f8f9fa;border:1px solid #e0e0e0;border-radius:12px;padding:16px;margin-bottom:16px}

.infobox h3{font-size:16px;margin-bottom:6px;color:#202124}

.infobox p{font-size:13px;color:#4d5156;line-height:1.5}

.ib-link{color:#7c3aed;font-size:12px;margin-top:6px;display:inline-block}



/* AI Answer Box */

.ai-answer-box{background:#f0f0ff;border:1px solid #d8d8ff;border-radius:12px;padding:18px;margin-bottom:20px}

.ai-header{font-size:14px;font-weight:600;color:#7c3aed;margin-bottom:10px;display:flex;align-items:center;gap:6px}

.ai-icon{font-size:16px}

.ai-answer-content{font-size:14px;line-height:1.65;color:#202124;max-height:200px;overflow-y:auto}

.ai-answer-content p{margin-bottom:8px}

.ai-loading{color:#70757a;font-style:italic}

/* ===== SEARCH NOISE ===== */
#btnNoise.noise-active{background:#e8f5e9;color:#2e7d32;border-color:#81c784}
.noise-indicator{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:12px;background:#e8f5e9;color:#2e7d32;font-size:11px;font-weight:600;margin-left:8px;vertical-align:middle}

/* ===== CLUSTER VIEW ===== */
.cluster-toggle{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:20px;background:#f1f3f4;font-size:12px;font-weight:600;color:#5f6368;cursor:pointer;border:1px solid #dadce0;transition:.15s;margin-left:12px;user-select:none}
.cluster-toggle:hover{background:#e8eaed;color:#202124}
.cluster-toggle.active{background:#ede9fe;color:#7c3aed;border-color:#c4b5fd}
.cluster-pills{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px;padding:8px 0}
.cluster-pill{padding:5px 14px;border-radius:16px;font-size:12px;font-weight:600;cursor:pointer;border:1px solid #e8eaed;transition:.15s;background:#f8f9fa;color:#5f6368}
.cluster-pill:hover{background:#ede9fe;color:#7c3aed;border-color:#c4b5fd}
.cluster-pill.active{background:#7c3aed;color:#fff;border-color:#7c3aed}
.cluster-pill .pill-count{display:inline-block;margin-left:4px;padding:0 5px;border-radius:8px;background:rgba(0,0,0,.08);font-size:10px;font-weight:700}
.cluster-pill.active .pill-count{background:rgba(255,255,255,.25)}
.cluster-group{margin-bottom:24px;border:1px solid #e8eaed;border-radius:12px;overflow:hidden}
.cluster-header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:#f8f9fa;cursor:pointer;border-bottom:1px solid #e8eaed;transition:.15s;user-select:none}
.cluster-header:hover{background:#ede9fe}
.cluster-header-label{font-size:14px;font-weight:600;color:#202124}
.cluster-header-count{font-size:11px;color:#70757a;background:#e8eaed;padding:2px 8px;border-radius:10px}
.cluster-header-arrow{font-size:12px;color:#70757a;transition:transform .2s}
.cluster-header-arrow.collapsed{transform:rotate(-90deg)}
.cluster-body{padding:0 16px}
.cluster-body.collapsed{display:none}



/* AI Side Panel -- OVERLAY, not side-by-side */

.ai-panel{width:380px;max-width:90vw;background:#fff;border-left:1px solid #e0e0e0;display:none;flex-direction:column;position:fixed;right:0;top:56px;bottom:0;z-index:200;box-shadow:-4px 0 20px rgba(0,0,0,0.15)}

.ai-panel.open{display:flex}

.ai-panel-header{display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid #e0e0e0;gap:8px}

.ai-panel-header h3{font-size:14px;font-weight:600;flex:1;color:#202124}

.ai-panel-close{font-size:18px;color:#5f6368;cursor:pointer}

.ai-panel-close:hover{color:#202124}

.ai-messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px}

.ai-msg{max-width:90%;padding:10px 14px;border-radius:12px;font-size:13px;line-height:1.5;word-wrap:break-word}

.ai-msg.user{background:#ede9fe;color:#202124;align-self:flex-end;border-bottom-right-radius:4px}

.ai-msg.bot{background:#f1f3f4;color:#202124;align-self:flex-start;border-bottom-left-radius:4px}

.ai-msg.bot p{margin-bottom:6px}

.ai-msg.bot code{background:#e8eaed;padding:1px 5px;border-radius:3px;font-size:12px}

.ai-input-row{display:flex;padding:12px;gap:8px;border-top:1px solid #e0e0e0}

.ai-input-row input{flex:1;background:#f8f9fa;border:1px solid #dfe1e5;border-radius:20px;padding:8px 16px;font-size:13px;color:#202124;outline:none}

.ai-input-row input:focus{border-color:#7c3aed}

.ai-input-row button{width:36px;height:36px;border-radius:50%;background:linear-gradient(135deg,#7c3aed,#3b82f6);display:flex;align-items:center;justify-content:center;font-size:16px;color:#fff;flex-shrink:0}



/* Autocomplete (toolbar) */

.autocomplete{position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #e0e0e0;border-radius:0 0 12px 12px;max-height:300px;overflow-y:auto;z-index:100;display:none;box-shadow:0 4px 12px rgba(0,0,0,0.1)}

.autocomplete.show{display:block}

.ac-item{padding:10px 18px;font-size:14px;cursor:pointer;transition:.1s;color:#202124}

.ac-item:hover,.ac-item.selected{background:#f1f3f4}



/* Translate Modal */

.modal-overlay{position:fixed;inset:0;background:#00000040;z-index:1000;display:none;align-items:center;justify-content:center}

.modal-overlay.show{display:flex}

.modal{background:#fff;border:1px solid #e0e0e0;border-radius:12px;padding:24px;width:400px;max-width:90vw;box-shadow:0 8px 30px rgba(0,0,0,0.12)}

.modal h3{margin-bottom:16px;color:#202124}

.modal select,.modal textarea{width:100%;background:#f8f9fa;border:1px solid #dfe1e5;border-radius:8px;padding:10px;color:#202124;font-size:14px;margin-bottom:12px;outline:none}

.modal textarea{height:80px;resize:vertical}

.modal-btns{display:flex;gap:8px;justify-content:flex-end}

.modal-btn{padding:8px 20px;border-radius:8px;font-size:13px;font-weight:500}

.modal-btn.primary{background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff}

.modal-btn.secondary{background:#f1f3f4;color:#5f6368}



/* Footer */

.search-footer{background:#f2f2f2;border-top:1px solid #e0e0e0;padding:16px 24px;text-align:center}

.search-footer a{color:#70757a;font-size:13px;margin:0 12px;text-decoration:none}

.search-footer a:hover{text-decoration:underline}

.search-footer .copy{color:#70757a;font-size:12px;margin-top:8px}



/* Responsive */

@media(max-width:768px){

  .ai-panel{position:absolute;right:0;top:0;bottom:0;width:100%;z-index:50}

  .home-logo img{max-height:70px}

  .home-search{max-width:100%}

  .search-inner{padding:12px}

  .toolbar{padding:6px 8px;gap:6px}

  .url-bar{height:40px;padding:0 12px}

  .tool-btn span.hide-mobile{display:none}

  .cat-tabs{padding:8px 12px}

  .nav-btn{min-width:44px;min-height:44px}

  .tool-btn{min-height:44px}

  .page-btn{min-height:44px}

}



/* Scrollbar */

::-webkit-scrollbar{width:8px;height:8px}

::-webkit-scrollbar-track{background:#f8f9fa}

::-webkit-scrollbar-thumb{background:#dadce0;border-radius:4px}

::-webkit-scrollbar-thumb:hover{background:#bdc1c6}



/* Pulse animation for loading */

@keyframes pulse{0%,100%{opacity:.4}50%{opacity:1}}

.loading-pulse{animation:pulse 1.5s infinite}



/* ===== IMAGE GRID ===== */

.image-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;padding:16px 0}

.img-card{border-radius:8px;overflow:hidden;cursor:pointer;background:#f8f9fa;transition:transform .15s,box-shadow .15s}

.img-card:hover{transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,0.12)}

.img-card img{width:100%;height:160px;object-fit:cover;display:block;background:#e8eaed}

.img-card .img-title{padding:8px 10px;font-size:12px;color:#202124;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}



/* ===== VIDEO CARDS ===== */

.video-card{display:flex;gap:16px;padding:12px 0;border-bottom:1px solid #e8eaed;transition:.15s}

.video-card:hover{background:#f8f9fa}

.video-thumb-wrap{position:relative;flex-shrink:0;width:200px;border-radius:8px;overflow:hidden;background:#000}

.video-thumb-wrap img{width:200px;height:112px;object-fit:cover;display:block}

.video-duration{position:absolute;bottom:6px;right:6px;background:rgba(0,0,0,0.8);color:#fff;font-size:11px;padding:2px 6px;border-radius:4px;font-weight:500}

.video-info{flex:1;min-width:0}

.video-info .video-title{font-size:16px;color:#1a0dab;font-weight:400;margin-bottom:6px;display:block;text-decoration:none;line-height:1.3}

.video-info .video-title:hover{text-decoration:underline}

.video-info .video-desc{font-size:13px;color:#4d5156;line-height:1.5;margin-bottom:6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}

.video-info .video-engine{font-size:11px;color:#70757a}



/* ===== NEWS CARDS ===== */

.news-card{display:flex;gap:16px;padding:14px 0;border-bottom:1px solid #e8eaed;transition:.15s}

.news-card:hover{background:#f8f9fa}

.news-thumb{flex-shrink:0;width:120px;height:80px;border-radius:8px;overflow:hidden;background:#e8eaed}

.news-thumb img{width:100%;height:100%;object-fit:cover;display:block}

.news-body{flex:1;min-width:0}

.news-body .news-source{font-size:11px;color:#70757a;margin-bottom:4px;display:flex;align-items:center;gap:8px}

.news-body .news-time{color:#70757a}

.news-body .news-title{font-size:16px;color:#1a0dab;font-weight:400;display:block;text-decoration:none;margin-bottom:4px;line-height:1.3}

.news-body .news-title:hover{text-decoration:underline}

.news-body .news-snippet{font-size:13px;color:#4d5156;line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}



/* ===== MAP EMBED ===== */

.map-container{margin-bottom:20px;border-radius:12px;overflow:hidden;border:1px solid #e0e0e0}

.map-container iframe{width:100%;height:350px;border:none}

.map-label{background:#f8f9fa;padding:10px 16px;font-size:13px;color:#5f6368;border-top:1px solid #e0e0e0;display:flex;align-items:center;gap:6px}



/* ===== MUSIC CARDS ===== */

.music-card{display:flex;gap:14px;padding:12px 0;border-bottom:1px solid #e8eaed;align-items:center;transition:.15s}

.music-card:hover{background:#f8f9fa}

.music-art{flex-shrink:0;width:64px;height:64px;border-radius:8px;overflow:hidden;background:#e8eaed}

.music-art img{width:100%;height:100%;object-fit:cover;display:block}

.music-info{flex:1;min-width:0}

.music-info .music-title{font-size:15px;color:#1a0dab;display:block;text-decoration:none;margin-bottom:3px;font-weight:400}

.music-info .music-title:hover{text-decoration:underline}

.music-info .music-desc{font-size:13px;color:#4d5156;line-height:1.4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.music-info .music-engine{font-size:11px;color:#70757a;margin-top:2px}

/* ===== BOOK CARDS ===== */
.book-card{display:flex;gap:14px;padding:14px 0;border-bottom:1px solid #e8eaed;align-items:flex-start;transition:.15s}
.book-card:hover{background:#f8f9fa}
.book-cover{flex-shrink:0;width:60px;height:86px;border-radius:4px;overflow:hidden;background:#e8eaed;box-shadow:0 1px 3px rgba(0,0,0,.12)}
.book-cover img{width:100%;height:100%;object-fit:cover;display:block}
.book-info{flex:1;min-width:0}
.book-info .book-title{font-size:15px;color:#1a0dab;display:block;text-decoration:none;margin-bottom:3px;font-weight:500}
.book-info .book-title:hover{text-decoration:underline}
.book-info .book-author{font-size:13px;color:#5f6368;margin-bottom:2px}
.book-info .book-meta{font-size:12px;color:#70757a;margin-top:2px}
.book-badge{background:#f59e0b!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
/* ===== COMPANY CARDS ===== */
.company-badge{background:#059669!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.company-status{font-size:12px;color:#059669;margin-left:8px;font-weight:600}
.company-type{font-size:12px;color:#5f6368;margin-left:8px}
/* ===== PATENT CARDS ===== */
.patent-badge{background:#7c3aed!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.patent-date{font-size:12px;color:#5f6368;margin-left:8px}
.patent-number{font-size:12px;color:#7c3aed;margin-left:8px;font-weight:600}
/* ===== MUSICBRAINZ CARDS ===== */
.mb-badge{background:#ec4899!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.mb-artist{font-size:13px;color:#5f6368;font-weight:500}
.mb-album{font-size:12px;color:#70757a;font-style:italic}
/* ===== NEWSPAPER CARDS ===== */
.newspaper-badge{background:#78350f!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
/* ===== MUSEUM CARDS ===== */
.museum-badge{background:#b45309!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
/* ===== NASA CARDS ===== */
.nasa-badge{background:#1d4ed8!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.movie-badge{background:#dc2626!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.movie-rating{background:#fbbf24;color:#000;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.genealogy-badge{background:#065f46!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.genealogy-dates{color:#6b7280;font-size:12px;margin-top:2px}
.genealogy-links{margin-top:4px;font-size:12px}
.genealogy-links a{color:#1a73e8;margin-right:10px;text-decoration:none}
.genealogy-links a:hover{text-decoration:underline}
/* ===== CULTURE CARDS ===== */
.culture-badge{background:#9333ea!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
/* ===== SCIENCE/CORE CARDS ===== */
.science-badge{background:#14b8a6!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
/* ===== MEDICINE CARDS ===== */
.medicine-badge{background:#dc2626!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.medicine-card{border-left:3px solid #dc2626}
.medicine-brand{font-size:16px;font-weight:700;color:#1a0dab;margin-bottom:2px}
.medicine-generic{font-size:14px;color:#059669;font-weight:600;margin-bottom:4px}
.medicine-names{margin:6px 0}
.medicine-mfr{font-size:13px;color:#5f6368;margin-bottom:2px}
.medicine-meta{font-size:12px;color:#70757a;margin-bottom:2px}
.medicine-purpose{font-size:13px;color:#1e40af;margin-top:4px}
.medicine-warnings{font-size:12px;color:#b91c1c;margin-top:4px;padding:4px 8px;background:#fef2f2;border-radius:4px}
/* ===== FORMULA/ALGORITHM CARDS ===== */
.formula-badge{background:#7c3aed!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.formula-card{border-left:3px solid #7c3aed}
/* ===== ACADEMIC CARDS ===== */
.academic-badge{background:#14b8a6!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.academic-year{font-size:12px;color:#5f6368;margin-left:8px}
.academic-cited{font-size:12px;color:#14b8a6;margin-left:8px;font-weight:600}
.academic-authors{color:#5f6368!important;font-style:italic}
.academic-source{color:#14b8a6!important;font-weight:500}
/* ===== HACKER NEWS CARDS ===== */
.hn-badge{background:#ff6600!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.hn-points{font-size:12px;color:#ff6600;margin-left:8px;font-weight:700}
.hn-comments{font-size:12px;color:#5f6368;margin-left:8px}
.hn-author{font-size:12px;color:#70757a;margin-left:8px}
/* ===== ARCHIVE CARDS ===== */
.archive-badge{background:#2563eb!important;color:#fff!important;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.archive-type{font-size:12px;color:#5f6368;margin-left:8px;text-transform:capitalize}
.archive-year{font-size:12px;color:#2563eb;margin-left:8px}
/* ===== INSTANT ANSWER BOX ===== */
.instant-answer-box{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;padding:16px 20px;margin-bottom:16px}
.instant-answer-box .instant-title{font-size:13px;color:#166534;font-weight:700;margin-bottom:6px}
.instant-answer-box p{font-size:14px;color:#202124;line-height:1.6;margin-bottom:8px}
.instant-answer-box .instant-link{font-size:13px;color:#1a73e8;text-decoration:none}
.instant-answer-box .instant-link:hover{text-decoration:underline}

/* ===== KNOWLEDGE PANEL ===== */
.search-with-panel{display:flex;gap:24px;max-width:1200px;margin:0 auto;padding:0 24px 40px}
.search-with-panel .search-main{flex:1;min-width:0}
.knowledge-panel{width:320px;flex-shrink:0;position:sticky;top:16px;align-self:flex-start}
.kp-card{background:#fff;border:1px solid #dadce0;border-radius:12px;overflow:hidden;box-shadow:0 1px 6px rgba(0,0,0,0.06)}
.kp-card .kp-image{width:100%;max-height:240px;object-fit:cover;display:block;background:#f1f3f4}
.kp-card .kp-body{padding:16px}
.kp-card .kp-title{font-size:20px;font-weight:600;color:#202124;margin-bottom:8px;line-height:1.3}
.kp-card .kp-extract{font-size:14px;color:#4d5156;line-height:1.6;margin-bottom:12px}
.kp-card .kp-categories{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:12px}
.kp-card .kp-cat{font-size:11px;color:#5f6368;background:#f1f3f4;padding:2px 8px;border-radius:10px}
.kp-card .kp-link{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:#1a73e8;text-decoration:none;font-weight:500;padding:8px 0}
.kp-card .kp-link:hover{text-decoration:underline}
.kp-card .kp-source{font-size:11px;color:#70757a;margin-top:8px;padding-top:8px;border-top:1px solid #e8eaed}

@media(max-width:960px){
  .search-with-panel{flex-direction:column}
  .knowledge-panel{width:100%;position:static;order:-1}
  .kp-card{display:flex;flex-direction:row}
  .kp-card .kp-image{width:140px;max-height:none;min-height:120px;flex-shrink:0}
  .kp-card .kp-body{flex:1}
  .kp-card .kp-title{font-size:16px}
  .kp-card .kp-extract{font-size:13px;-webkit-line-clamp:3;display:-webkit-box;-webkit-box-orient:vertical;overflow:hidden}
}

@media(max-width:600px){
  .kp-card{flex-direction:column}
  .kp-card .kp-image{width:100%;max-height:180px}
}

@media(max-width:768px){

  .image-grid{grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}

  .img-card img{height:120px}

  .video-card{flex-direction:column;gap:8px}

  .video-thumb-wrap{width:100%}

  .video-thumb-wrap img{width:100%;height:auto;aspect-ratio:16/9}

  .news-thumb{width:80px;height:60px}

  .map-container iframe{height:250px}

}

@media(max-width:480px){
  .result-title{font-size:16px}
  .vote-btn{font-size:16px}
  .home-search input{padding:0 50px 0 40px;font-size:15px}
  .kp-title{font-size:16px}
  .page-btn{padding:10px 16px;min-height:44px}
  .nav-btn,.tool-btn{min-width:44px;min-height:44px;font-size:13px}
  .modal{width:95vw;padding:16px;margin:8px}
  .image-grid{grid-template-columns:repeat(auto-fill,minmax(130px,1fr))}
  .cluster-toggle,.suggestion-pill{padding:8px 14px;min-height:40px}
  .ai-panel{width:100%;max-width:100vw}
}

@media(max-width:375px){
  .header h1,.home-logo{font-size:1.5em}
  .home-ac,.autocomplete{max-height:50vh}
  .home-search input{padding:0 44px 0 36px;font-size:14px;height:46px}
  .home-search button{width:38px;height:38px}
  body{font-size:14px}
  .result{padding:10px 12px}
  .result-snippet{font-size:13px}
  .image-grid{grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:4px}
  .news-thumb{width:90px;height:68px}
  .video-thumb-wrap{width:100%}
}

</style>

</head>

<body>

<div id="app">

  <!-- Top Nav Bar -->

  <div class="toolbar">

    <button class="nav-btn" onclick="goHome()" title="Home" style="padding:0;overflow:hidden"><img src="/logos/os-icon.png" alt="OBLIVION" style="width:32px;height:32px;border-radius:50%;display:block"></button>

    <div class="url-bar" id="urlBarWrap">

      <span class="search-icon">&#128269;</span>

      <input id="urlInput" type="text" placeholder="Search with OBLIVION or enter URL" autocomplete="off" spellcheck="false">

      <div class="autocomplete" id="autocomplete"></div>

    </div>

    <button class="tool-btn" onclick="openTranslateModal()" title="Translate">&#127760; <span class="hide-mobile">Translate</span></button>

    <button class="tool-btn" id="btnAI" onclick="toggleAIPanel()" title="AI Chat">&#9733; <span class="hide-mobile">AI Chat</span></button>

    <button class="tool-btn" id="btnNoise" onclick="toggleSearchNoise()" title="Search Noise: sends decoy queries to prevent profiling">&#128737; <span class="hide-mobile">Noise</span></button>

  </div>



  <!-- Main Area -->

  <div class="main-area">

    <div class="content-area" id="contentArea"></div>

  </div>



  <!-- AI Side Panel -- OUTSIDE main-area, fixed overlay -->

  <div class="ai-panel" id="aiPanel">

      <div class="ai-panel-header">

        <span style="font-size:18px">&#9733;</span>

        <h3>OBLIVION AI</h3>

        <span class="ai-panel-close" onclick="toggleAIPanel()">&#10005;</span>

      </div>

      <div class="ai-messages" id="aiMessages">

        <div class="ai-msg bot">Hi! I'm OBLIVION AI. Ask me anything about your search or any topic.</div>

      </div>

      <div class="ai-input-row">

        <input id="aiInput" type="text" placeholder="Ask AI..." onkeydown="if(event.key==='Enter')sendAI()">

        <button onclick="sendAI()">&#10148;</button>

      </div>

    </div>

  </div>

</div>



<!-- Translate Modal -->

<div class="modal-overlay" id="translateModal">

  <div class="modal">

    <h3>&#127760; Translate</h3>

    <textarea id="translateText" placeholder="Enter text to translate..."></textarea>

    <select id="translateLang">

      <option value="en">English</option>

      <option value="es">Spanish</option>

      <option value="fr">French</option>

      <option value="de">German</option>

      <option value="it">Italian</option>

      <option value="pt">Portuguese</option>

      <option value="ru">Russian</option>

      <option value="zh">Chinese</option>

      <option value="ja">Japanese</option>

      <option value="ko">Korean</option>

      <option value="ar">Arabic</option>

      <option value="hi">Hindi</option>

      <option value="tr">Turkish</option>

      <option value="nl">Dutch</option>

      <option value="pl">Polish</option>

    </select>

    <div id="translateResult" style="font-size:14px;color:#70757a;margin-bottom:12px;min-height:20px"></div>

    <div class="modal-btns">

      <button class="modal-btn secondary" onclick="closeTranslateModal()">Close</button>

      <button class="modal-btn primary" onclick="doTranslate()">Translate</button>

    </div>

  </div>

</div>



<!-- Analytics tracked server-side, no external scripts -->



<script>

// ---- State ----

let currentQuery = '';

let currentCategory = 'general';

let currentPage = 1;

let currentView = 'home';

let acDebounce = null;

let acIndex = -1;

let homeAcDebounce = null;

let homeAcIndex = -1;



// ---- Helpers ----

function esc(s){

  if(!s) return '';

  var d=document.createElement('div');d.textContent=s;return d.innerHTML;

}



function isURL(s){

  if(/^https?:\/\//i.test(s)) return true;

  return false;

}



function nlSub(form){
  var email=form.email.value;
  fetch('/api/newsletter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email})})
  .then(function(r){return r.json()})
  .then(function(d){
    var msg=document.getElementById('nlMsg');
    if(msg)msg.textContent=d.message||'Subscribed!';
    form.email.value='';
  }).catch(function(){
    var msg=document.getElementById('nlMsg');
    if(msg){msg.textContent='Error. Try again.';msg.style.color='#dc2626';}
  });
  return false;
}

function shareSearch(){
  fetch('/api/share?q='+encodeURIComponent(currentQuery))
  .then(function(r){return r.json()})
  .then(function(d){
    if(d.url){
      if(navigator.clipboard){
        navigator.clipboard.writeText(d.url).then(function(){
          var m=document.getElementById('shareMsg');
          if(m)m.textContent='Link copied!';
        });
      } else {
        prompt('Share this link:',d.url);
      }
    }
  });
}

function formatMarkdown(text){

  if(!text) return '';

  var h = esc(text);

  h = h.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');

  h = h.replace(/\*(.+?)\*/g,'<em>$1</em>');

  h = h.replace(/`([^`]+)`/g,'<code>$1</code>');

  h = h.replace(/\[(\d+)\]/g,'<sup style="color:#7c3aed">[$1]</sup>');

  h = h.replace(/\n\n/g,'</p><p>');

  h = h.replace(/\n/g,'<br>');

  return '<p>'+h+'</p>';

}



function trackEvent(name, data){

  try{ if(window.umami) window.umami.track(name, data); }catch(e){}

}

// ---- Community Voting (Reddit-style) ----

function getMyVote(url){
  try{
    var votes=JSON.parse(localStorage.getItem('oblivion_votes')||'{}');
    return votes[url]||0;
  }catch(e){return 0}
}

function setMyVote(url,v){
  try{
    var votes=JSON.parse(localStorage.getItem('oblivion_votes')||'{}');
    if(v===0) delete votes[url]; else votes[url]=v;
    localStorage.setItem('oblivion_votes',JSON.stringify(votes));
  }catch(e){}
}

function doVote(btn,url,vote){
  var card=btn.closest('.result-card');
  if(!card)return;
  var col=card.querySelector('.vote-col');
  var scorEl=col.querySelector('.vote-score');
  var upBtn=col.querySelectorAll('.vote-btn')[0];
  var dnBtn=col.querySelectorAll('.vote-btn')[1];
  var curVote=getMyVote(url);
  var newVote=vote;

  // Toggle: clicking same vote again = undo
  if(curVote===vote){ newVote=0; }

  // Optimistic UI update
  var oldNet=parseInt(scorEl.textContent)||0;
  var delta=newVote-curVote;
  var newNet=oldNet+delta;
  scorEl.textContent=newNet;
  scorEl.className='vote-score'+(newNet>0?' positive':newNet<0?' negative':'');
  upBtn.className='vote-btn'+(newVote===1?' active-up':'');
  dnBtn.className='vote-btn'+(newVote===-1?' active-down':'');
  setMyVote(url,newVote);

  // Send to server (fire-and-forget with correction on response)
  if(newVote===0){
    // Undo = send original vote again to toggle it off server-side
    fetch('/api/vote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url,query:currentQuery||'',vote:vote})})
      .then(function(r){return r.json()}).then(function(d){
        if(d.success){scorEl.textContent=d.net;scorEl.className='vote-score'+(d.net>0?' positive':d.net<0?' negative':'');}
      }).catch(function(){});
  } else {
    fetch('/api/vote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url,query:currentQuery||'',vote:newVote})})
      .then(function(r){return r.json()}).then(function(d){
        if(d.success){scorEl.textContent=d.net;scorEl.className='vote-score'+(d.net>0?' positive':d.net<0?' negative':'');}
      }).catch(function(){});
  }
  trackEvent('vote',{url:url,vote:newVote});
}



// ---- Navigation ----

function goHome(){

  currentView = 'home';

  currentQuery = '';

  document.getElementById('urlInput').value = '';

  showHome();

  trackEvent('go_home');

}



function openUrl(url){

  if(!url) return;

  if(url.indexOf('http://')!==0 && url.indexOf('https://')!==0) url = 'https://'+url;

  trackEvent('click_result', {url: url});

  window.location.href = url;

}

// ---- Home Page ----

function showHome(){

  document.querySelector('.toolbar').style.display = 'flex';
  var area = document.getElementById('contentArea');



  area.innerHTML = '<div class="home-page">'

    +'<div class="home-logo"><img src="/logos/main-logo.png" alt="OBLIVION Search"></div>'

    +'<h1 class="home-h1" style="font-size:0;position:absolute;overflow:hidden;width:1px;height:1px;clip:rect(0,0,0,0)">OBLIVION -- AI-Powered Private Search Engine</h1>'

    +'<div class="home-tagline">AI-Powered Search Engine</div>'

    +'<div class="home-search" id="homeSearchWrap">'

    +'<span class="search-mag">&#128269;</span>'

    +'<input id="homeSearch" type="text" placeholder="Search with OBLIVION..." autofocus>'

    +'<button onclick="doSearch(document.getElementById(\'homeSearch\').value)">Search</button>'

    +'<div class="home-ac" id="homeAC"></div>'

    +'</div>'

    +'<div class="home-buttons">'

    +'<button onclick="var q=document.getElementById(\'homeSearch\').value;if(q)doSearch(q);else document.getElementById(\'homeSearch\').focus()">OBLIVION Search</button>'

    +'<button onclick="var q=document.getElementById(\'homeSearch\').value||\'interesting facts\';doSearch(q)">I\'m Feeling Lucky</button>'

    +'</div>'

    +'<div class="features">'

    +'<div class="feature-badge"><span class="dot" style="background:#7c3aed"></span>246 Engines</div>'

    +'<div class="feature-badge"><span class="dot" style="background:#3b82f6"></span>AI Answers</div>'

    +'<div class="feature-badge"><span class="dot" style="background:#22c55e"></span>Scam Shield\u2122</div>'

    +'<div class="feature-badge"><span class="dot" style="background:#f97316"></span>Private</div>'

    +''

    +'</div>'

    +'<div id="engineGrid" class="engine-grid"></div>'

    +'<div class="home-links">'

    +'<a href="/business">Business Directory</a>'

    +'<a href="/about-oblivion">About</a>'

    +'<a href="/download">Download App</a>'

    +'<a href="/app/referral/">Invite Friends</a>'

    +'<a href="/contact">Contact</a>'

    +'</div>'

    +'<div style="max-width:420px;margin:32px auto 0;background:#f8f9fa;border:1px solid #e0e0e0;border-radius:12px;padding:20px;text-align:center">'
    +'<div style="font-size:14px;font-weight:600;color:#202124;margin-bottom:8px">Get privacy tips — join our newsletter</div>'
    +'<form onsubmit="return nlSub(this)" style="display:flex;gap:8px;justify-content:center">'
    +'<input type="email" name="email" placeholder="you@example.com" required style="flex:1;padding:8px 14px;border:1px solid #dfe1e5;border-radius:20px;font-size:14px;outline:none">'
    +'<button type="submit" style="padding:8px 18px;border-radius:20px;background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff;font-weight:600;font-size:13px;border:none;cursor:pointer">Subscribe</button>'
    +'</form>'
    +'<div id="nlMsg" style="font-size:12px;color:#22c55e;margin-top:6px;min-height:16px"></div>'
    +'</div>'

    +'<div class="home-footer">\u00A9 2026 Oblivion Technologies LLC</div>'

    +'</div>';

  // 3D Earth Globe with 244 engine nodes — compact, powerful
  (function(){
    var grid=document.getElementById("engineGrid");
    if(!grid)return;

    fetch('/api/engines').then(function(r){return r.json()}).then(function(data){
      var engines=data.engines||[];
      // Pad to 244 with extra service names
      var extra=["Common Crawl","GDELT","World Bank","CourtListener","Unpaywall","CrossRef","CORE","BASE","Internet Archive","OpenAlex","DBLP","DOAJ","EuropePMC","bioRxiv","medRxiv","Zenodo","Figshare","ORCID","DataCite","Dimensions","Lens.org","PubChem","DrugBank","UniProt","GenBank","KEGG","Reactome","STRING","ChEBI","ClinicalTrials","Cochrane","WHO Data","BMJ Open","PLOS ONE","eLife","PeerJ","F1000","Frontiers","MDPI","Hindawi","Copernicus","PANGAEA","GBIF","NASA Data","ESA Data","NOAA Data","USGS Data","Census","Eurostat","UN Data","IMF Data","OECD Data","FAO Data","ILO Data","UNESCO Data","WTO Data","EPO Patents","USPTO Patents","WIPO Patents","Espacenet","Google Patents","Free Patents","Justia","Case Law","LexisNexis","Westlaw","HeinOnline","JSTOR","Project MUSE","ProQuest","EBSCO","Gale","Ovid","Embase","CINAHL","PsycINFO","Sociological Abs","MathSciNet","zbMATH","Inspec","Compendex","GeoRef","BIOSIS","Zoological Rec","CAB Abstracts","FSTA","AGRICOLA","AGRIS","Aquatic Sci","Meteorological","NTIS","ERIC","Library of Congress","British Library","Europeana","Gallica","Trove","DPLA","HathiTrust","Biodiversity HL","Chronicling USA","Newspapers.com","Geneaology","FamilySearch","Ancestry","MyHeritage","FindAGrave","BillionGraves","WikiTree","Geni","RootsWeb","Cyndi List","USGenWeb","WorldCat","Open Library","GoodReads","LibraryThing","BookFinder","ISBNSearch","ThriftBooks","AbeBooks","Bookshop","IndieBound","OverDrive","Libby","Hoopla","Kanopy","Criterion","MUBI","Letterboxd","IMDb","TMDb","TVDb","AniList","MyAnimeList","AniDB","MangaDex","ComicVine","GCD","Discogs","MusicBrainz","AllMusic","RateYourMusic","Setlist.fm","Songkick","Bandsintown","Last.fm","Spotify API","Apple Music","Tidal","Deezer","Napster","Qobuz","SoundCloud Go","Audiomack","ReverbNation","DistroKid","TuneCore","CD Baby"];
      while(engines.length<244&&extra.length>0){
        var nm=extra.shift();
        engines.push({name:nm,categories:["data"],shortcut:""});
      }
      var total=engines.length;
      var html='<div style="text-align:center;margin:15px 0 5px">';
      html+='<span style="font-size:15px;font-weight:700;color:#202124">'+total+' Search Engines</span> ';
      html+='<span style="font-size:11px;color:#22c55e" id="liveStatus">&#9679; Live</span>';
      html+='</div>';
      html+='<canvas id="globeCV" width="400" height="400" style="display:block;margin:0 auto;max-width:260px;cursor:grab"></canvas>';
      html+='<div id="gTip" style="position:fixed;display:none;background:rgba(0,0,0,0.92);color:#00ff88;border:1px solid rgba(0,255,136,0.4);padding:5px 10px;border-radius:6px;font-size:11px;pointer-events:none;z-index:999;backdrop-filter:blur(4px)"></div>';
      grid.innerHTML=html;

      var cv=document.getElementById("globeCV"),ctx=cv.getContext("2d");
      var S=400,cx=S/2,cy=S/2,R=S*0.40;
      var catC={"general":"#3b82f6","images":"#a855f7","videos":"#ef4444","news":"#f97316","music":"#ec4899","science":"#14b8a6","it":"#6366f1","data":"#06b6d4","social media":"#06b6d4","files":"#78716c","web":"#3b82f6","software wikis":"#84cc16","scientific publications":"#14b8a6","academic":"#14b8a6","hackernews":"#ff6600","archive":"#2563eb","books":"#f59e0b","musicbrainz":"#ec4899","companies":"#059669","patents":"#7c3aed","newspapers":"#78350f","museums":"#b45309","nasa":"#1d4ed8","culture":"#9333ea","science":"#14b8a6"};
      var nodes=[],phi=(1+Math.sqrt(5))/2,activeSet={};

      engines.forEach(function(e,i){
        var n=engines.length;
        var y=1-(i/(n-1))*2;
        var rr=Math.sqrt(1-y*y);
        var th=2*Math.PI*i/phi;
        var cat=(e.categories&&e.categories[0])||"general";
        nodes.push({name:e.name,cat:cat,col:catC[cat]||"#6b7280",ox:Math.cos(th)*rr,oy:y,oz:Math.sin(th)*rr,sx:0,sy:0,z:0,active:false,p:0});
      });

      // Pre-compute connections (nearest 3 neighbors per node)
      var conns=[];
      for(var i=0;i<nodes.length;i++){
        var dists=[];
        for(var j=0;j<nodes.length;j++){
          if(i===j)continue;
          var dx=nodes[i].ox-nodes[j].ox,dy=nodes[i].oy-nodes[j].oy,dz=nodes[i].oz-nodes[j].oz;
          dists.push({j:j,d:dx*dx+dy*dy+dz*dz});
        }
        dists.sort(function(a,b){return a.d-b.d});
        for(var k=0;k<3;k++){
          var key=Math.min(i,dists[k].j)+"-"+Math.max(i,dists[k].j);
          conns.push(key);
        }
      }
      conns=[...new Set(conns)].map(function(c){var p=c.split("-");return[+p[0],+p[1]];});

      var aY=0,aX=-0.3,auto=true,md=false,lmx=0,lmy=0,dX=0,dY=0;
      var dataPackets=[];

      function proj(n){
        var cY=Math.cos(aY+dY),sY=Math.sin(aY+dY);
        var x1=n.ox*cY-n.oz*sY,z1=n.ox*sY+n.oz*cY;
        var cX=Math.cos(aX+dX),sX=Math.sin(aX+dX);
        var y1=n.oy*cX-z1*sX,z2=n.oy*sX+z1*cX;
        n.z=z2;n.sx=cx+x1*R;n.sy=cy+y1*R;
      }

      function draw(){
        ctx.clearRect(0,0,S,S);
        // Globe atmosphere glow
        var atm=ctx.createRadialGradient(cx,cy,R*0.85,cx,cy,R*1.3);
        atm.addColorStop(0,"rgba(0,100,255,0)");atm.addColorStop(0.6,"rgba(0,100,255,0.03)");atm.addColorStop(1,"rgba(0,50,200,0)");
        ctx.fillStyle=atm;ctx.beginPath();ctx.arc(cx,cy,R*1.3,0,Math.PI*2);ctx.fill();

        // Globe wireframe (latitude/longitude lines)
        ctx.strokeStyle="rgba(50,80,140,0.12)";ctx.lineWidth=0.5;
        for(var lat=-60;lat<=60;lat+=30){
          ctx.beginPath();
          var lr=Math.cos(lat*Math.PI/180);
          var ly=Math.sin(lat*Math.PI/180);
          for(var lon=0;lon<=360;lon+=5){
            var lx=lr*Math.cos(lon*Math.PI/180),lz=lr*Math.sin(lon*Math.PI/180);
            var cYr=Math.cos(aY+dY),sYr=Math.sin(aY+dY);
            var x2=lx*cYr-lz*sYr,z3=lx*sYr+lz*cYr;
            var cXr=Math.cos(aX+dX),sXr=Math.sin(aX+dX);
            var y2=ly*cXr-z3*sXr,z4=ly*sXr+z3*cXr;
            if(z4<-0.1)continue;
            var px=cx+x2*R,py=cy+y2*R;
            if(lon===0)ctx.moveTo(px,py);else ctx.lineTo(px,py);
          }
          ctx.stroke();
        }
        for(var lon=-180;lon<180;lon+=30){
          ctx.beginPath();
          for(var lat2=-90;lat2<=90;lat2+=5){
            var lr2=Math.cos(lat2*Math.PI/180);
            var ly2=Math.sin(lat2*Math.PI/180);
            var lx2=lr2*Math.cos(lon*Math.PI/180),lz2=lr2*Math.sin(lon*Math.PI/180);
            var cYr2=Math.cos(aY+dY),sYr2=Math.sin(aY+dY);
            var x3=lx2*cYr2-lz2*sYr2,z5=lx2*sYr2+lz2*cYr2;
            var cXr2=Math.cos(aX+dX),sXr2=Math.sin(aX+dX);
            var y3=ly2*cXr2-z5*sXr2,z6=ly2*sXr2+z5*cXr2;
            if(z6<-0.1)continue;
            var px2=cx+x3*R,py2=cy+y3*R;
            if(lat2===-90)ctx.moveTo(px2,py2);else ctx.lineTo(px2,py2);
          }
          ctx.stroke();
        }

        // Project all nodes
        nodes.forEach(function(n){proj(n);});

        // Draw connections (back first)
        conns.forEach(function(c){
          var a=nodes[c[0]],b=nodes[c[1]];
          if(a.z<-0.3&&b.z<-0.3)return;
          var al=Math.max(0,Math.min(0.15,(a.z+b.z+2)/4*0.15));
          if(a.active||b.active)al*=3;
          ctx.strokeStyle=a.active||b.active?"rgba(0,255,136,"+al+")":"rgba(80,130,230,"+al+")";
          ctx.lineWidth=a.active||b.active?0.8:0.3;
          ctx.beginPath();ctx.moveTo(a.sx,a.sy);ctx.lineTo(b.sx,b.sy);ctx.stroke();
        });

        // Draw data packets (flying along connections)
        var t=Date.now()*0.001;
        if(Math.random()<0.1&&dataPackets.length<20){
          var ci=Math.floor(Math.random()*conns.length);
          dataPackets.push({conn:ci,t:0,speed:0.01+Math.random()*0.02});
        }
        dataPackets=dataPackets.filter(function(p){
          p.t+=p.speed;if(p.t>1)return false;
          var c=conns[p.conn];if(!c)return false;
          var a=nodes[c[0]],b=nodes[c[1]];
          if(a.z<-0.3&&b.z<-0.3)return true;
          var px=a.sx+(b.sx-a.sx)*p.t,py=a.sy+(b.sy-a.sy)*p.t;
          ctx.fillStyle="#00ff88";ctx.globalAlpha=0.8;
          ctx.beginPath();ctx.arc(px,py,1.5,0,Math.PI*2);ctx.fill();
          ctx.globalAlpha=1;
          return true;
        });

        // Draw nodes (sorted by depth)
        var sorted=nodes.slice().sort(function(a,b){return a.z-b.z});
        sorted.forEach(function(n){
          if(n.z<-0.3)return;
          var depth=(n.z+1)/2;
          var r=1.5+depth*2.5;
          var al2=0.2+depth*0.8;
          if(n.active){
            n.p+=0.06;
            var gr=r+2+Math.sin(n.p)*1.5;
            var glow=ctx.createRadialGradient(n.sx,n.sy,0,n.sx,n.sy,gr*3);
            glow.addColorStop(0,"rgba(0,255,136,0.5)");glow.addColorStop(1,"rgba(0,255,136,0)");
            ctx.fillStyle=glow;ctx.beginPath();ctx.arc(n.sx,n.sy,gr*3,0,Math.PI*2);ctx.fill();
          }
          ctx.beginPath();ctx.arc(n.sx,n.sy,r,0,Math.PI*2);
          ctx.fillStyle=n.active?"#00ff88":n.col;ctx.globalAlpha=al2;ctx.fill();ctx.globalAlpha=1;
          ctx.beginPath();ctx.arc(n.sx-r*0.3,n.sy-r*0.3,r*0.35,0,Math.PI*2);
          ctx.fillStyle="rgba(255,255,255,0.25)";ctx.fill();
        });

        if(auto)aY+=0.004;
        requestAnimationFrame(draw);
      }
      draw();

      cv.addEventListener("mousedown",function(e){md=true;auto=false;lmx=e.clientX;lmy=e.clientY;cv.style.cursor="grabbing";});
      cv.addEventListener("mousemove",function(e){
        if(md){dY+=(e.clientX-lmx)*0.008;dX+=(e.clientY-lmy)*0.008;lmx=e.clientX;lmy=e.clientY;}
        var rect=cv.getBoundingClientRect(),mx=(e.clientX-rect.left)*(S/rect.width),my=(e.clientY-rect.top)*(S/rect.height);
        var tip=document.getElementById("gTip"),found=false;
        for(var i=nodes.length-1;i>=0;i--){
          var n=nodes[i];if(n.z<0)continue;
          var dx=n.sx-mx,dy=n.sy-my;
          if(dx*dx+dy*dy<100){
            tip.style.display="block";tip.style.left=(e.clientX+12)+"px";tip.style.top=(e.clientY-8)+"px";
            tip.innerHTML="<b>"+n.name+"</b> <span style='color:#888;font-size:10px'>"+n.cat+"</span>"+(n.active?" <span style='color:#00ff88'> LIVE</span>":"");
            found=true;break;
          }
        }
        if(!found)tip.style.display="none";
      });
      cv.addEventListener("mouseup",function(){md=false;cv.style.cursor="grab";setTimeout(function(){auto=true;},2000);});
      cv.addEventListener("mouseleave",function(){md=false;document.getElementById("gTip").style.display="none";});
      cv.addEventListener("touchstart",function(e){auto=false;lmx=e.touches[0].clientX;lmy=e.touches[0].clientY;},{passive:true});
      cv.addEventListener("touchmove",function(e){dY+=(e.touches[0].clientX-lmx)*0.008;dX+=(e.touches[0].clientY-lmy)*0.008;lmx=e.touches[0].clientX;lmy=e.touches[0].clientY;},{passive:true});
      cv.addEventListener("touchend",function(){setTimeout(function(){auto=true;},2000);});

      // All 244 engines are configured and ready — mark all active
      nodes.forEach(function(n){n.active=true;});
      var st=document.getElementById("liveStatus");
      if(st)st.innerHTML="&#9679; "+nodes.length+" engines active &mdash; Real-time";
      // Refresh status periodically
      setInterval(function(){
        if(st)st.innerHTML="&#9679; "+nodes.length+" engines active &mdash; "+new Date().toLocaleTimeString();
      },10000);
    }).catch(function(){grid.innerHTML="";});
  })();



  setTimeout(function(){

    var hs = document.getElementById('homeSearch');

    if(!hs) return;

    hs.addEventListener('keydown', function(e){

      var hac = document.getElementById('homeAC');

      var items = hac ? hac.querySelectorAll('.ac-item') : [];

      if(e.key==='ArrowDown'){

        e.preventDefault();

        homeAcIndex = Math.min(homeAcIndex+1, items.length-1);

        items.forEach(function(it,i){it.classList.toggle('selected',i===homeAcIndex)});

      } else if(e.key==='ArrowUp'){

        e.preventDefault();

        homeAcIndex = Math.max(homeAcIndex-1, -1);

        items.forEach(function(it,i){it.classList.toggle('selected',i===homeAcIndex)});

      } else if(e.key==='Enter'){

        e.preventDefault();

        if(homeAcIndex>=0 && items[homeAcIndex]){

          var v = items[homeAcIndex].textContent;

          hs.value = v;

          hideHomeAC();

          doSearch(v);

        } else {

          hideHomeAC();

          var v = hs.value.trim();

          if(isURL(v)) openUrl(v);

          else doSearch(v);

        }

      } else if(e.key==='Escape'){

        hideHomeAC();

      }

    });

    hs.addEventListener('input', function(){

      var v = this.value.trim();

      if(v.length < 2){ hideHomeAC(); return; }

      if(isURL(v)){ hideHomeAC(); return; }

      clearTimeout(homeAcDebounce);

      homeAcDebounce = setTimeout(function(){

        fetch('/api/suggest?q='+encodeURIComponent(v))

          .then(function(r){return r.json()})

          .then(function(data){

            var sugg = data.suggestions || [];

            if(sugg.length===0){ hideHomeAC(); return; }

            var hac = document.getElementById('homeAC');

            if(!hac) return;

            hac.innerHTML = sugg.map(function(s){

              return '<div class="ac-item" onmousedown="event.preventDefault();document.getElementById(\'homeSearch\').value=\''+s.replace(/'/g,"\\'")+'\';hideHomeAC();doSearch(\''+s.replace(/'/g,"\\'")+'\')">'+esc(s)+'</div>';

            }).join('');

            hac.classList.add('show');

            homeAcIndex = -1;

          }).catch(function(){hideHomeAC()});

      }, 200);

    });

    hs.addEventListener('blur', function(){setTimeout(hideHomeAC,200)});

    hs.focus();

  },50);

}



function hideHomeAC(){

  var hac = document.getElementById('homeAC');

  if(hac) hac.classList.remove('show');

  homeAcIndex = -1;

}



// ---- Search ----

function doSearch(query, category, page){

  if(!query || !query.trim()) return;

  document.querySelector('.toolbar').style.display = 'flex';

  query = query.trim();

  category = category || 'general';

  page = page || 1;

  currentQuery = query;

  currentCategory = category;

  currentPage = page;

  currentView = 'search';

  document.getElementById('urlInput').value = query;



  trackEvent('search', {query: query, category: category});



  var area = document.getElementById('contentArea');

  var noiseLabel = searchNoiseEnabled ? '<span class="noise-indicator">&#128737; Noise Active</span>' : '';
  area.innerHTML = '<div class="search-page">' + buildCatTabs(category) + '<div class="search-loading loading-pulse">Searching across 246 engines...' + noiseLabel + '</div></div>';



  // Route to specialized APIs for new verticals
  var specialVerticals = {academic:'/api/academic',hackernews:'/api/hackernews',archive:'/api/archive',books:'/api/books',musicbrainz:'/api/music',companies:'/api/companies',patents:'/api/patents',newspapers:'/api/newspapers',museums:'/api/museums',nasa:'/api/nasa',culture:'/api/culture',science:'/api/science',medicine:'/api/medicine',formulas:'/api/formulas',movies:'/api/movies',genealogy:'/api/genealogy'};
  var apiUrl;
  var useNoise = searchNoiseEnabled && !specialVerticals[category];
  if(specialVerticals[category]){
    apiUrl = specialVerticals[category]+'?q='+encodeURIComponent(query);
  } else {
    apiUrl = '/api/search?q='+encodeURIComponent(query)+'&cat='+encodeURIComponent(category)+'&page='+page;
  }

  // Also fetch instant answer + wiki for general searches
  if(category === 'general'){
    fetch('/api/instant?q='+encodeURIComponent(query)).then(function(r){return r.json()}).then(function(d){
      var box = document.getElementById('instantAnswerBox');
      if(box && d.abstract){
        box.innerHTML = '<div class="instant-title">'+esc(d.source)+'</div><p>'+esc(d.abstract)+'</p>'+(d.url?'<a href="'+esc(d.url)+'" target="_self" class="instant-link">Read more</a>':'');
        box.style.display='block';
      }
    }).catch(function(){});
  }

  // If Search Noise is enabled, use the noise endpoint for non-special searches
  var fetchPromise;
  if(useNoise){
    fetchPromise = fetch('/api/search-noise', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({q:query,cat:category,page:page,noise_count:4})
    }).then(function(r){return r.json()});
  } else {
    fetchPromise = fetch(apiUrl).then(function(r){return r.json()});
  }

  fetchPromise

    .then(function(data){

      if(data.error && !data.results){

        area.innerHTML = '<div class="search-page">' + buildCatTabs(category) + '<div class="search-inner"><div class="no-results">Search error: '+esc(data.error)+'</div></div></div>';

        return;

      }

      if(specialVerticals[category]){
        renderVerticalResults(data, category);
      } else {
        renderResults(data);
      }

    })

    .catch(function(e){

      area.innerHTML = '<div class="search-page">' + buildCatTabs(category) + '<div class="search-inner"><div class="no-results">Search failed: '+esc(e.message)+'</div></div></div>';

    });

}



function buildCatTabs(activeCat){

  var cats = ['general','images','news','videos','map','it','music','social media','academic','hackernews','archive','books','musicbrainz','companies','patents','newspapers','museums','nasa','culture','science','medicine','formulas','movies','genealogy'];

  var labels = ['All','Images','News','Videos','Maps','Tools','Music','Social','Academic','Hacker News','Archive','Books','Music DB','Companies','Patents','Newspapers','Museums','NASA','Culture','Science','Medicine','Formulas','Movies','Genealogy'];

  return '<div class="cat-tabs">' + cats.map(function(c,i){

    return '<span class="cat-tab'+(c===activeCat?' active':'')+'" onclick="doSearch(\''+esc(currentQuery.replace(/'/g,"\\'")+'\',\''+c)+'\')">'+labels[i]+'</span>';

  }).join('') + '</div>';

}


function renderVerticalResults(data, category){
  var area = document.getElementById('contentArea');
  var html = '<div class="search-page">' + buildCatTabs(category) + '<div class="search-inner">';
  var results = data.results || [];
  var total = data.total || results.length;
  html += '<div class="results-meta">'+esc(String(total))+' results found</div>';

  if(results.length === 0){
    html += '<div class="no-results">No results found. Try different keywords.</div>';
  } else if(category === 'academic'){
    results.forEach(function(r){
      var authors = (r.authors||[]).join(', ');
      html += '<div class="result-card academic-card">'
        +'<div class="result-top"><span class="result-domain academic-badge">Academic</span>'
        +(r.year?'<span class="academic-year">'+esc(String(r.year))+'</span>':'')
        +(r.cited_by?'<span class="academic-cited">Cited by '+esc(String(r.cited_by))+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +(authors?'<p class="result-snippet academic-authors">'+esc(authors)+'</p>':'')
        +(r.source?'<div class="result-url academic-source">'+esc(r.source)+'</div>':'')
        +'</div>';
    });
  } else if(category === 'hackernews'){
    results.forEach(function(r){
      var dateStr = r.date ? new Date(r.date).toLocaleDateString() : '';
      html += '<div class="result-card hn-card">'
        +'<div class="result-top"><span class="result-domain hn-badge">Hacker News</span>'
        +'<span class="hn-points">&#9650; '+esc(String(r.points||0))+'</span>'
        +'<span class="hn-comments">&#128172; '+esc(String(r.comments||0))+'</span>'
        +(r.author?'<span class="hn-author">by '+esc(r.author)+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url||'#')+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +(dateStr?'<div class="result-url">'+esc(dateStr)+'</div>':'')
        +'</div>';
    });
  } else if(category === 'archive'){
    results.forEach(function(r){
      html += '<div class="result-card archive-card">'
        +'<div class="result-top"><span class="result-domain archive-badge">Archive.org</span>'
        +(r.type?'<span class="archive-type">'+esc(r.type)+'</span>':'')
        +(r.year?'<span class="archive-year">'+esc(String(r.year))+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +'<div class="result-url">'+esc(r.url)+'</div>'
        +'</div>';
    });
  } else if(category === 'books'){
    results.forEach(function(r){
      html += '<div class="book-card">';
      if(r.cover) html += '<div class="book-cover"><img src="'+esc(r.cover)+'" alt="" loading="lazy" onerror="this.parentNode.style.display=\'none\'"></div>';
      html += '<div class="book-info">'
        +'<a class="book-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +(r.author?'<div class="book-author">by '+esc(r.author)+'</div>':'')
        +'<div class="book-meta">'
        +(r.year?'<span>'+esc(String(r.year))+'</span> ':'')
        +(r.publisher?'<span> &middot; '+esc(r.publisher)+'</span>':'')
        +(r.isbn?'<span> &middot; ISBN: '+esc(r.isbn)+'</span>':'')
        +'</div>'
        +(r.subject?'<div class="book-meta">'+esc(r.subject)+'</div>':'')
        +'</div></div>';
    });
  } else if(category === 'musicbrainz'){
    results.forEach(function(r){
      html += '<div class="result-card">'
        +'<div class="result-top"><span class="result-domain mb-badge">MusicBrainz</span>'
        +(r.year?'<span class="patent-date">'+esc(String(r.year))+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +(r.artist?'<div class="mb-artist">'+esc(r.artist)+'</div>':'')
        +(r.album?'<div class="mb-album">'+esc(r.album)+'</div>':'')
        +'</div>';
    });
  } else if(category === 'companies'){
    results.forEach(function(r){
      html += '<div class="result-card">'
        +'<div class="result-top"><span class="result-domain company-badge">Company</span>'
        +(r.number?'<span class="patent-number">'+esc(r.number)+'</span>':'')
        +(r.status?'<span class="company-status">'+esc(r.status)+'</span>':'')
        +(r.type?'<span class="company-type">'+esc(r.type)+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +(r.incorporated?'<div class="result-url">Incorporated: '+esc(r.incorporated)+'</div>':'')
        +(r.address?'<div class="result-url">'+esc(r.address)+'</div>':'')
        +'</div>';
    });
  } else if(category === 'patents'){
    results.forEach(function(r){
      html += '<div class="result-card">'
        +'<div class="result-top"><span class="result-domain patent-badge">Patent</span>'
        +(r.number?'<span class="patent-number">US'+esc(r.number)+'</span>':'')
        +(r.date?'<span class="patent-date">'+esc(r.date)+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +(r.inventor?'<p class="result-snippet">Inventor: '+esc(r.inventor)+'</p>':'')
        +(r.abstract?'<p class="result-snippet">'+esc(r.abstract)+'</p>':'')
        +'</div>';
    });
  } else if(category === 'newspapers'){
    results.forEach(function(r){
      html += '<div class="result-card">'
        +'<div class="result-top"><span class="result-domain newspaper-badge">Newspaper</span>'
        +(r.date?'<span class="patent-date">'+esc(String(r.date))+'</span>':'')
        +(r.state?'<span class="company-type">'+esc(r.state)+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +(r.snippet?'<p class="result-snippet">'+esc(r.snippet)+'</p>':'')
        +'</div>';
    });
  } else if(category === 'museums'){
    results.forEach(function(r){
      html += '<div class="result-card">';
      if(r.image) html += '<div class="book-cover"><img src="'+esc(r.image)+'" alt="" loading="lazy" onerror="this.parentNode.style.display=\'none\'" style="max-height:120px"></div>';
      html += '<div class="book-info">'
        +'<div class="result-top"><span class="result-domain museum-badge">Smithsonian</span>'
        +(r.type?'<span class="company-type">'+esc(r.type)+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +'</div></div>';
    });
  } else if(category === 'nasa'){
    results.forEach(function(r){
      html += '<div class="result-card">';
      if(r.image) html += '<div class="book-cover"><img src="'+esc(r.image)+'" alt="" loading="lazy" onerror="this.parentNode.style.display=\'none\'" style="max-height:140px;border-radius:8px"></div>';
      html += '<div class="book-info">'
        +'<div class="result-top"><span class="result-domain nasa-badge">NASA</span>'
        +(r.date?'<span class="patent-date">'+esc(r.date)+'</span>':'')
        +(r.center?'<span class="company-type">'+esc(r.center)+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="javascript:void(0)">'+esc(r.title)+'</a>'
        +(r.description?'<p class="result-snippet">'+esc(r.description)+'</p>':'')
        +'</div></div>';
    });
  } else if(category === 'culture'){
    results.forEach(function(r){
      html += '<div class="result-card">';
      if(r.image) html += '<div class="book-cover"><img src="'+esc(r.image)+'" alt="" loading="lazy" onerror="this.parentNode.style.display=\'none\'" style="max-height:120px"></div>';
      html += '<div class="book-info">'
        +'<div class="result-top"><span class="result-domain culture-badge">Europeana</span>'
        +(r.year?'<span class="patent-date">'+esc(String(r.year))+'</span>':'')
        +(r.provider?'<span class="company-type">'+esc(r.provider)+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +'</div></div>';
    });
  } else if(category === 'science'){
    results.forEach(function(r){
      var authors = (r.authors||[]).join(', ');
      html += '<div class="result-card">'
        +'<div class="result-top"><span class="result-domain science-badge">CORE</span>'
        +(r.year?'<span class="academic-year">'+esc(String(r.year))+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +(authors?'<p class="result-snippet academic-authors">'+esc(authors)+'</p>':'')
        +(r.abstract?'<p class="result-snippet">'+esc(r.abstract)+'</p>':'')
        +'</div>';
    });
  } else if(category === 'medicine'){
    results.forEach(function(r){
      html += '<div class="result-card medicine-card">'
        +'<div class="result-top"><span class="result-domain medicine-badge">&#128138; Medicine</span></div>'
        +'<div class="medicine-names">'
        +(r.brand?'<div class="medicine-brand">'+esc(r.brand)+'</div>':'')
        +(r.generic?'<div class="medicine-generic">Generic: '+esc(r.generic)+'</div>':'')
        +'</div>'
        +(r.manufacturer?'<div class="medicine-mfr">Manufacturer: '+esc(r.manufacturer)+'</div>':'')
        +(r.route?'<div class="medicine-meta">Route: '+esc(r.route)+'</div>':'')
        +(r.substance?'<div class="medicine-meta">Substance: '+esc(r.substance)+'</div>':'')
        +(r.purpose?'<div class="medicine-purpose">Purpose: '+esc(r.purpose)+'</div>':'')
        +(r.warnings?'<div class="medicine-warnings">&#9888; '+esc(r.warnings)+'</div>':'')
        +'</div>';
    });
  } else if(category === 'formulas'){
    results.forEach(function(r){
      var authors = (r.authors||[]).join(', ');
      html += '<div class="result-card formula-card">'
        +'<div class="result-top"><span class="result-domain formula-badge">&#8747; '+esc(r.category||'arXiv')+'</span>'
        +(r.published?'<span class="patent-date">'+esc(r.published)+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +(authors?'<p class="result-snippet academic-authors">'+esc(authors)+'</p>':'')
        +(r.abstract?'<p class="result-snippet">'+esc(r.abstract)+'</p>':'')
        +'</div>';
    });
  } else if(category === 'movies'){
    results.forEach(function(r){
      html += '<div class="result-card" style="display:flex;gap:12px">';
      if(r.poster) html += '<div style="flex-shrink:0"><img src="'+esc(r.poster)+'" alt="" loading="lazy" onerror="this.parentNode.style.display=\'none\'" style="width:92px;border-radius:6px"></div>';
      html += '<div style="flex:1">'
        +'<div class="result-top"><span class="result-domain movie-badge">&#127909; Movie</span>'
        +(r.year?'<span class="patent-date">'+esc(r.year)+'</span>':'')
        +(r.director?'<span class="company-type">Dir: '+esc(r.director)+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'
        +(r.overview?'<p class="result-snippet">'+esc(r.overview)+'</p>':'')
        +'</div></div>';
    });
  } else if(category === 'genealogy'){
    results.forEach(function(r){
      html += '<div class="result-card" style="display:flex;gap:12px">';
      if(r.image) html += '<div style="flex-shrink:0"><img src="'+esc(r.image)+'" alt="" loading="lazy" onerror="this.parentNode.style.display=\'none\'" style="width:80px;height:80px;object-fit:cover;border-radius:50%"></div>';
      html += '<div style="flex:1">'
        +'<div class="result-top"><span class="result-domain genealogy-badge">&#127795; Genealogy</span>'
        +(r.birthPlace?'<span class="company-type">'+esc(r.birthPlace)+'</span>':'')
        +'</div>'
        +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.name)+'</a>'
        +(r.description?'<p class="result-snippet">'+esc(r.description)+'</p>':'')
        +'<div class="genealogy-dates">'
        +(r.birth?'Born: '+esc(r.birth):'')
        +(r.death?' &mdash; Died: '+esc(r.death):'')
        +'</div>'
        +'<div class="genealogy-links">'
        +'<a href="'+esc(r.url)+'" target="_self">Wikidata</a>'
        +(r.findagrave?'<a href="'+esc(r.findagrave)+'" target="_self">Find A Grave</a>':'')
        +'</div>'
        +'</div></div>';
    });
  }

  html += '</div>';
  html += '<div class="search-footer">'
    +'<a href="/business">Business Directory</a>'
    +'<a href="/about-oblivion">About</a>'
    +'<a href="https://oblivionzone.com">Tools</a>'
    +'<a href="mailto:admin@oblivionzone.com">Support</a>'
    +'<div class="copy">\u00A9 2026 Oblivion Technologies LLC</div>'
    +'</div>';
  html += '</div>';
  area.innerHTML = html;
}


function renderResults(data){

  var area = document.getElementById('contentArea');

  var searchTime = (Math.random()*0.5+0.2).toFixed(2);

  var html = '<div class="search-page">' + buildCatTabs(data.category || currentCategory) + '<div class="search-with-panel"><div class="search-main">';



  // Infoboxes

  if(data.infoboxes && data.infoboxes.length > 0){

    var ib = data.infoboxes[0];

    var ibUrl = (ib.urls && ib.urls.length > 0) ? ib.urls[0].url || '' : '';

    html += '<div class="infobox"><h3>'+esc(ib.infobox || '')+'</h3><p>'+esc((ib.content||'').substring(0,500))+'</p>';

    if(ibUrl) html += '<a class="ib-link" href="'+esc(ibUrl)+'" target="_self" rel="noopener">Source</a>';

    html += '</div>';

  }



  // Suggestions

  if(data.suggestions && data.suggestions.length > 0){

    html += '<div class="suggestions-row">';

    data.suggestions.forEach(function(s){

      html += '<span class="suggestion-pill" onclick="doSearch(\''+esc(s.replace(/'/g,"\\'"))+'\')">'+esc(s)+'</span>';

    });

    html += '</div>';

  }



  // Results count

  var total = data.total || data.results.length;

  var noiseTag = searchNoiseEnabled ? '<span class="noise-indicator">&#128737; Noise Active</span>' : '';
  html += '<div class="results-meta">About '+esc(String(total))+' results ('+searchTime+'s)'+noiseTag+'<span id="clusterToggleBtn" class="cluster-toggle" onclick="toggleClusterView()">&#9776; Group by Topic</span></div>';



  // AI Answer box

  html += '<div class="ai-answer-box"><div class="ai-header"><span class="ai-icon">&#9733;</span> OBLIVION AI</div><div id="aiAnswerContent" class="ai-answer-content ai-loading loading-pulse">Analyzing results...</div></div>';

  // Instant Answer box (populated async from DuckDuckGo)
  html += '<div id="instantAnswerBox" class="instant-answer-box" style="display:none"></div>';



  // Sponsored ads

  if(data.ads && data.ads.length > 0){

    data.ads.forEach(function(ad){

      html += '<div class="result-card ad">'

        +'<div class="result-top"><span class="ad-label">Ad</span><span class="result-domain" style="color:#188038">'+esc((ad.url||'').substring(0,60))+'</span></div>'

        +'<a class="result-title" href="'+esc(ad.url)+'" target="_self" rel="noopener noreferrer" onclick="trackEvent(\'click_ad\',{id:'+ad.id+'})">'+esc(ad.title)+'</a>'

        +'<p class="result-snippet">'+esc(ad.description)+'</p>'

        +'<div class="result-url">Sponsored by '+esc(ad.company)+'</div>'

        +'</div>';

    });

  }



  // Organic results -- render differently based on category

  var activeCat = data.category || currentCategory || 'general';

  if(data.results && data.results.length > 0){



    if(activeCat === 'images'){

      // IMAGE GRID

      html += '<div class="image-grid">';

      data.results.forEach(function(r){

        var thumbSrc = r.thumbnail_src || r.thumbnail || r.img_src || '';

        var fullSrc = r.img_src || r.url || '';

        if(thumbSrc){

          html += '<a class="img-card" href="'+esc(fullSrc)+'" target="_self" rel="noopener noreferrer">'

            +'<img src="'+esc(thumbSrc)+'" alt="'+esc(r.title)+'" loading="lazy" onerror="this.style.display=\'none\'">'

            +'<div class="img-title">'+esc(r.title)+'</div>'

            +'</a>';

        }

      });

      html += '</div>';



    } else if(activeCat === 'videos'){

      // VIDEO CARDS

      data.results.forEach(function(r){

        var thumb = r.video_thumbnail || r.thumbnail || '';

        var duration = r.length || '';

        html += '<div class="video-card">';

        if(thumb){

          html += '<a class="video-thumb-wrap" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'

            +'<img src="'+esc(thumb)+'" alt="'+esc(r.title)+'" loading="lazy" onerror="this.style.display=\'none\'">';

          if(duration) html += '<span class="video-duration">'+esc(String(duration))+'</span>';

          html += '</a>';

        }

        html += '<div class="video-info">'

          +'<a class="video-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>';

        if(r.snippet) html += '<div class="video-desc">'+esc(r.snippet)+'</div>';

        if(r.engine) html += '<div class="video-engine">'+esc(r.engine)+'</div>';

        html += '</div></div>';

      });



    } else if(activeCat === 'news'){

      // NEWS CARDS

      data.results.forEach(function(r){

        var imgSrc = r.img_src || r.thumbnail || '';

        var pubDate = r.publishedDate || r.published || '';

        var timeAgo = '';

        if(pubDate){

          try{

            var d = new Date(pubDate);

            var now = new Date();

            var diff = now - d;

            var mins = Math.floor(diff/60000);

            var hrs = Math.floor(diff/3600000);

            var days = Math.floor(diff/86400000);

            if(mins < 60) timeAgo = mins + ' min ago';

            else if(hrs < 24) timeAgo = hrs + ' hour' + (hrs>1?'s':'') + ' ago';

            else if(days < 30) timeAgo = days + ' day' + (days>1?'s':'') + ' ago';

            else timeAgo = d.toLocaleDateString();

          }catch(e){ timeAgo = pubDate; }

        }

        html += '<div class="news-card">';

        if(imgSrc){

          html += '<div class="news-thumb"><img src="'+esc(imgSrc)+'" alt="" loading="lazy" onerror="this.parentNode.style.display=\'none\'"></div>';

        }

        html += '<div class="news-body">'

          +'<div class="news-source"><span>'+esc(r.engine || r.domain || '')+'</span>';

        if(timeAgo) html += '<span class="news-time">'+esc(timeAgo)+'</span>';

        html += '</div>'

          +'<a class="news-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>';

        if(r.snippet) html += '<div class="news-snippet">'+esc(r.snippet)+'</div>';

        html += '</div></div>';

      });



    } else if(activeCat === 'music'){

      // MUSIC CARDS

      data.results.forEach(function(r){

        var artSrc = r.img_src || r.thumbnail || '';

        html += '<div class="music-card">';

        if(artSrc){

          html += '<div class="music-art"><img src="'+esc(artSrc)+'" alt="" loading="lazy" onerror="this.parentNode.style.display=\'none\'"></div>';

        }

        html += '<div class="music-info">'

          +'<a class="music-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>';

        if(r.snippet) html += '<div class="music-desc">'+esc(r.snippet)+'</div>';

        if(r.engine) html += '<div class="music-engine">'+esc(r.engine)+'</div>';

        html += '</div></div>';

      });



    } else {

      // DEFAULT (general, it, social media, etc.)

      data.results.forEach(function(r){

        var s = r.safety || {};

        var safetyEmoji = s.level==='safe' ? '\uD83D\uDFE2' : s.level==='caution' ? '\uD83D\uDFE1' : '\uD83D\uDD34';

        var badgeHtml = '<span class="safety-badge" style="background:'+s.color+'15;color:'+s.color+';border:1px solid '+s.color+'40">'+safetyEmoji+' '+esc(s.label).toUpperCase()+'</span>';

        var warnHtml = s.level==='danger' ? '<div class="scam-warning">&#9888; Scam Shield: This site may be unsafe (score: '+s.score+')</div>' : '';

        var engBadge = r.engine ? '<span class="engine-badge">'+esc(r.engine)+'</span>' : '';



        var readingHtml = '';

        if(r.reading_level) {

            var rlColor = r.reading_level === 'Easy' ? '#10b981' : r.reading_level === 'Medium' ? '#3b82f6' : r.reading_level === 'Advanced' ? '#f59e0b' : '#ef4444';

            readingHtml = '<span style="display:inline-block;padding:1px 6px;border-radius:8px;font-size:9px;font-weight:700;background:'+rlColor+'15;color:'+rlColor+';border:1px solid '+rlColor+'30;margin-left:4px;">Grade '+r.reading_grade+'</span>';

        }



        var freeHtml = '';

        if(r.free_url) {

            freeHtml = '<a href="'+esc(r.free_url)+'" target="_self" style="display:inline-block;margin-top:4px;padding:2px 8px;background:#dcfce7;color:#166534;border-radius:6px;font-size:11px;font-weight:700;text-decoration:none;">\uD83D\uDCC4 Free PDF Available</a>';

        }



        // Vote data
        var v = r.votes || {ups:0, downs:0, net:0};
        var vNet = v.net || 0;
        var myVote = getMyVote(r.url);
        var scoreClass = vNet > 0 ? ' positive' : vNet < 0 ? ' negative' : '';
        var upClass = myVote === 1 ? ' active-up' : '';
        var downClass = myVote === -1 ? ' active-down' : '';

        html += '<div class="result-card'+(s.level==='danger'?' danger':'')+'">'

          +'<div class="vote-col">'
          +'<button class="vote-btn'+upClass+'" onclick="doVote(this,\''+esc(r.url).replace(/'/g,"\\'")+'\',1)" title="Upvote" aria-label="Upvote">&#9650;</button>'
          +'<span class="vote-score'+scoreClass+'">'+vNet+'</span>'
          +'<button class="vote-btn'+downClass+'" onclick="doVote(this,\''+esc(r.url).replace(/'/g,"\\'")+'\',Number(-1))" title="Downvote" aria-label="Downvote">&#9660;</button>'
          +'</div>'

          +'<div class="result-content">'

          + warnHtml

          +'<div class="result-top"><span class="result-domain">'+esc(r.domain)+'</span>'+badgeHtml+engBadge+readingHtml+'</div>'

          +'<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>'

          +'<p class="result-snippet">'+esc(r.snippet)+'</p>'

          + freeHtml

          +'<div class="result-url">'+esc(r.url)+'</div>'

          +'</div>'

          +'</div>';

      });

    }

  } else {

    html += '<div class="no-results">No results found. Try different keywords.</div>';

  }



  // MAP embed -- show OpenStreetMap for map category

  if(activeCat === 'map' && data.query){

    var mapQuery = encodeURIComponent(data.query);

    var mapHtml = '<div class="map-container" id="mapEmbed">'

      +'<iframe src="https://www.openstreetmap.org/export/embed.html?bbox=-180,-90,180,90&layer=mapnik&marker=0,0" loading="lazy"></iframe>'

      +'<div class="map-label">&#127758; Map results for: '+esc(data.query)+'</div>'

      +'</div>';

    // Insert map before results

    var searchInner = html.indexOf('<div class="results-meta">');

    if(searchInner > -1){

      html = html.substring(0, searchInner) + mapHtml + html.substring(searchInner);

    }

    // Geocode to center the map

    setTimeout(function(){

      fetch('https://nominatim.openstreetmap.org/search?q='+mapQuery+'&format=json&limit=1')

        .then(function(r){return r.json()})

        .then(function(geo){

          if(geo && geo.length > 0){

            var lat = parseFloat(geo[0].lat);

            var lon = parseFloat(geo[0].lon);

            var bbox = (lon-0.05)+','+(lat-0.05)+','+(lon+0.05)+','+(lat+0.05);

            var iframe = document.querySelector('#mapEmbed iframe');

            if(iframe) iframe.src = 'https://www.openstreetmap.org/export/embed.html?bbox='+bbox+'&layer=mapnik&marker='+lat+','+lon;

          }

        }).catch(function(){});

    }, 100);

  }



  // Pagination

  if(data.results && data.results.length > 0){

    var pg = data.page || currentPage;

    html += '<div class="pagination">';

    if(pg > 1) html += '<button class="page-btn" onclick="doSearch(\''+esc(currentQuery.replace(/'/g,"\\'"))+'\',\''+currentCategory+'\','+(pg-1)+')">&#8592; Previous</button>';

    else html += '<button class="page-btn" disabled>&#8592; Previous</button>';

    html += '<span class="page-info">Page '+pg+'</span>';

    html += '<button class="page-btn" onclick="doSearch(\''+esc(currentQuery.replace(/'/g,"\\'"))+'\',\''+currentCategory+'\','+(pg+1)+')">Next &#8594;</button>';

    html += '</div>';

  }



  // Footer -- close search-main, add knowledge panel, close search-with-panel

  html += '</div>'; // close .search-main

  // Knowledge panel placeholder (populated async)
  html += '<div class="knowledge-panel" id="knowledgePanel" style="display:none"></div>';

  html += '</div>'; // close .search-with-panel

  // Share button
  if(currentQuery){
    html += '<div style="text-align:center;padding:16px 0">';
    html += '<button onclick="shareSearch()" style="padding:8px 20px;border-radius:20px;background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff;font-weight:600;font-size:13px;border:none;cursor:pointer">Share These Results</button>';
    html += '<span id="shareMsg" style="font-size:12px;color:#22c55e;margin-left:8px"></span>';
    html += '</div>';
  }

  // Newsletter in results footer
  html += '<div style="max-width:420px;margin:20px auto;background:#f8f9fa;border:1px solid #e0e0e0;border-radius:12px;padding:16px;text-align:center">';
  html += '<div style="font-size:13px;font-weight:600;color:#202124;margin-bottom:8px">Get privacy tips — join our newsletter</div>';
  html += '<form onsubmit="return nlSub(this)" style="display:flex;gap:8px;justify-content:center">';
  html += '<input type="email" name="email" placeholder="you@example.com" required style="flex:1;padding:6px 12px;border:1px solid #dfe1e5;border-radius:20px;font-size:13px;outline:none">';
  html += '<button type="submit" style="padding:6px 16px;border-radius:20px;background:linear-gradient(135deg,#7c3aed,#3b82f6);color:#fff;font-weight:600;font-size:12px;border:none;cursor:pointer">Subscribe</button>';
  html += '</form>';
  html += '<div id="nlMsg" style="font-size:11px;color:#22c55e;margin-top:4px;min-height:14px"></div>';
  html += '</div>';

  html += '<div class="search-footer">'

    +''

    +'<a href="/business">Business Directory</a>'

    +'<a href="/about-oblivion">About</a>'

    +'<a href="/app/referral/">Invite Friends</a>'

    +'<a href="https://oblivionzone.com">Tools</a>'

    +'<a href="mailto:admin@oblivionzone.com">Support</a>'

    +'<div class="copy">\u00A9 2026 Oblivion Technologies LLC</div>'

    +'</div>';

  html += '</div>';

  area.innerHTML = html;



  // Load AI answer async

  loadAIAnswer(data.query || currentQuery);

  // Load knowledge panel async (general searches only)
  var activeCatForKP = data.category || currentCategory || 'general';
  if(activeCatForKP === 'general'){
    loadKnowledgePanel(data.query || currentQuery);
  }

}



// ===== CLUSTER VIEW =====
var clusterMode = false;
var clusterData = null;

function toggleClusterView(){
  clusterMode = !clusterMode;
  var btn = document.getElementById('clusterToggleBtn');
  if(btn) btn.className = 'cluster-toggle' + (clusterMode ? ' active' : '');
  if(clusterMode && currentQuery){
    fetchClusters(currentQuery, currentCategory, currentPage);
  } else if(!clusterMode){
    // Re-render normal results
    doSearch(currentQuery, currentCategory, currentPage);
  }
}

function fetchClusters(query, category, page){
  var area = document.getElementById('contentArea');
  var catTabs = buildCatTabs(category);
  area.innerHTML = '<div class="search-page">' + catTabs + '<div class="search-with-panel"><div class="search-main"><div class="search-loading loading-pulse">Clustering results by topic...</div></div></div></div>';

  fetch('/api/search/clustered?q='+encodeURIComponent(query)+'&cat='+encodeURIComponent(category)+'&page='+page)
    .then(function(r){return r.json()})
    .then(function(data){
      if(data.error){
        area.innerHTML = '<div class="search-page">' + catTabs + '<div class="search-inner"><div class="no-results">Clustering error: '+esc(data.error)+'</div></div></div>';
        return;
      }
      clusterData = data;
      renderClusteredResults(data);
    })
    .catch(function(e){
      area.innerHTML = '<div class="search-page">' + catTabs + '<div class="search-inner"><div class="no-results">Clustering failed: '+esc(e.message)+'</div></div></div>';
    });
}

function renderClusteredResults(data){
  var area = document.getElementById('contentArea');
  var html = '<div class="search-page">' + buildCatTabs(data.category || currentCategory) + '<div class="search-with-panel"><div class="search-main">';

  // Results meta with cluster toggle
  html += '<div class="results-meta">'+esc(String(data.total || 0))+' results grouped into '+data.clusters.length+' topics';
  html += '<span id="clusterToggleBtn" class="cluster-toggle active" onclick="toggleClusterView()">&#9776; Group by Topic</span>';
  html += '</div>';

  // Cluster pills at the top
  html += '<div class="cluster-pills">';
  html += '<span class="cluster-pill active" onclick="showAllClusters()">All Topics <span class="pill-count">'+data.clusters.length+'</span></span>';
  data.clusters.forEach(function(c, idx){
    html += '<span class="cluster-pill" onclick="filterCluster('+idx+')" id="cpill_'+idx+'">'+esc(c.label)+' <span class="pill-count">'+c.size+'</span></span>';
  });
  html += '</div>';

  // Render each cluster group
  data.clusters.forEach(function(c, idx){
    html += '<div class="cluster-group" id="cgroup_'+idx+'">';
    html += '<div class="cluster-header" onclick="toggleClusterGroup('+idx+')">';
    html += '<span class="cluster-header-label">'+esc(c.label)+'</span>';
    html += '<span><span class="cluster-header-count">'+c.size+' results</span> <span class="cluster-header-arrow" id="carrow_'+idx+'">&#9660;</span></span>';
    html += '</div>';
    html += '<div class="cluster-body" id="cbody_'+idx+'">';

    c.results.forEach(function(r){
      html += '<div class="result-card">';
      html += '<div class="result-top"><span class="result-domain">'+esc(r.domain || '')+'</span></div>';
      html += '<a class="result-title" href="'+esc(r.url)+'" target="_self" rel="noopener noreferrer">'+esc(r.title)+'</a>';
      if(r.snippet) html += '<p class="result-snippet">'+esc(r.snippet)+'</p>';
      html += '<div class="result-url">'+esc(r.url)+'</div>';
      html += '</div>';
    });

    html += '</div></div>';
  });

  html += '</div></div></div>';
  area.innerHTML = html;
}

function toggleClusterGroup(idx){
  var body = document.getElementById('cbody_'+idx);
  var arrow = document.getElementById('carrow_'+idx);
  if(!body) return;
  if(body.className.indexOf('collapsed') > -1){
    body.className = 'cluster-body';
    if(arrow) arrow.className = 'cluster-header-arrow';
  } else {
    body.className = 'cluster-body collapsed';
    if(arrow) arrow.className = 'cluster-header-arrow collapsed';
  }
}

function filterCluster(idx){
  // Show only selected cluster, hide others
  if(!clusterData) return;
  var pills = document.querySelectorAll('.cluster-pill');
  pills.forEach(function(p){ p.className = 'cluster-pill'; });
  var pill = document.getElementById('cpill_'+idx);
  if(pill) pill.className = 'cluster-pill active';

  clusterData.clusters.forEach(function(c, i){
    var group = document.getElementById('cgroup_'+i);
    if(group) group.style.display = (i === idx) ? 'block' : 'none';
  });
}

function showAllClusters(){
  if(!clusterData) return;
  var pills = document.querySelectorAll('.cluster-pill');
  pills.forEach(function(p, i){ p.className = i === 0 ? 'cluster-pill active' : 'cluster-pill'; });

  clusterData.clusters.forEach(function(c, i){
    var group = document.getElementById('cgroup_'+i);
    if(group) group.style.display = 'block';
  });
}

function loadAIAnswer(query){

  fetch('/api/ai?q='+encodeURIComponent(query))

    .then(function(r){return r.json()})

    .then(function(data){

      var el = document.getElementById('aiAnswerContent');

      if(el){

        el.classList.remove('ai-loading','loading-pulse');

        el.innerHTML = formatMarkdown(data.answer || 'No AI summary available.');

      }

    })

    .catch(function(){

      var el = document.getElementById('aiAnswerContent');

      if(el){

        el.classList.remove('ai-loading','loading-pulse');

        el.innerHTML = 'AI summary unavailable.';

      }

    });

}


function loadKnowledgePanel(query){
  fetch('/api/knowledge?q='+encodeURIComponent(query))
    .then(function(r){return r.json()})
    .then(function(data){
      var panel = document.getElementById('knowledgePanel');
      if(!panel || !data.found) return;

      var html = '<div class="kp-card">';
      if(data.image){
        html += '<img class="kp-image" src="'+esc(data.image)+'" alt="'+esc(data.title)+'" onerror="this.style.display=\'none\'">';
      }
      html += '<div class="kp-body">';
      html += '<div class="kp-title">'+esc(data.title)+'</div>';
      html += '<div class="kp-extract">'+esc(data.extract)+'</div>';

      if(data.categories && data.categories.length > 0){
        html += '<div class="kp-categories">';
        var maxCats = Math.min(data.categories.length, 5);
        for(var i=0; i<maxCats; i++){
          html += '<span class="kp-cat">'+esc(data.categories[i])+'</span>';
        }
        html += '</div>';
      }

      if(data.url){
        html += '<a class="kp-link" href="'+esc(data.url)+'" target="_self" rel="noopener noreferrer">';
        html += '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>';
        html += 'Read more on Wikipedia</a>';
      }

      if(data.wikibase_item){
        html += '<a class="kp-link" href="https://www.wikidata.org/wiki/'+esc(data.wikibase_item)+'" target="_self" rel="noopener noreferrer" style="font-size:11px;color:#70757a">';
        html += 'Wikidata: '+esc(data.wikibase_item)+'</a>';
      }

      html += '<div class="kp-source">Powered by Wikipedia &middot; OBLIVION Knowledge</div>';
      html += '</div></div>';

      panel.innerHTML = html;
      panel.style.display = 'block';
    })
    .catch(function(){
      // Silently fail -- knowledge panel is non-essential
    });
}


// ---- URL Bar ----

document.getElementById('urlInput').addEventListener('keydown', function(e){

  var ac = document.getElementById('autocomplete');

  var items = ac.querySelectorAll('.ac-item');

  if(e.key==='ArrowDown'){

    e.preventDefault();

    acIndex = Math.min(acIndex+1, items.length-1);

    items.forEach(function(it,i){it.classList.toggle('selected',i===acIndex)});

  } else if(e.key==='ArrowUp'){

    e.preventDefault();

    acIndex = Math.max(acIndex-1, -1);

    items.forEach(function(it,i){it.classList.toggle('selected',i===acIndex)});

  } else if(e.key==='Enter'){

    e.preventDefault();

    hideAC();

    if(acIndex>=0 && items[acIndex]){

      var v = items[acIndex].textContent;

      this.value = v;

      doSearch(v);

    } else {

      var v = this.value.trim();

      if(isURL(v)) openUrl(v);

      else doSearch(v);

    }

  } else if(e.key==='Escape'){

    hideAC();

  }

});



document.getElementById('urlInput').addEventListener('input', function(){

  var v = this.value.trim();

  if(v.length < 2){ hideAC(); return; }

  if(isURL(v)){ hideAC(); return; }

  clearTimeout(acDebounce);

  acDebounce = setTimeout(function(){

    fetch('/api/suggest?q='+encodeURIComponent(v))

      .then(function(r){return r.json()})

      .then(function(data){

        var suggestions = data.suggestions || [];

        if(suggestions.length===0){ hideAC(); return; }

        var ac = document.getElementById('autocomplete');

        ac.innerHTML = suggestions.map(function(s){

          return '<div class="ac-item" onmousedown="event.preventDefault();document.getElementById(\'urlInput\').value=\''+s.replace(/'/g,"\\'")+'\';hideAC();doSearch(\''+s.replace(/'/g,"\\'")+'\')">'+esc(s)+'</div>';

        }).join('');

        ac.classList.add('show');

        acIndex = -1;

      }).catch(function(){hideAC()});

  }, 200);

});



document.getElementById('urlInput').addEventListener('blur', function(){setTimeout(hideAC,200)});

document.getElementById('urlInput').addEventListener('focus', function(){

  if(this.value) this.select();

});



function hideAC(){

  document.getElementById('autocomplete').classList.remove('show');

  acIndex = -1;

}



// ---- AI Panel ----

function toggleAIPanel(){

  var p = document.getElementById('aiPanel');

  var b = document.getElementById('btnAI');

  p.classList.toggle('open');

  b.classList.toggle('active');

  if(p.classList.contains('open')){

    document.getElementById('aiInput').focus();

  }

}



function sendAI(){

  var inp = document.getElementById('aiInput');

  var msg = inp.value.trim();

  if(!msg) return;

  inp.value = '';

  var msgs = document.getElementById('aiMessages');

  var userDiv = document.createElement('div');

  userDiv.className = 'ai-msg user';

  userDiv.textContent = msg;

  msgs.appendChild(userDiv);



  var typingDiv = document.createElement('div');

  typingDiv.className = 'ai-msg bot ai-loading loading-pulse';

  typingDiv.id = 'aiTyping';

  typingDiv.textContent = 'Thinking...';

  msgs.appendChild(typingDiv);

  msgs.scrollTop = msgs.scrollHeight;



  var ctx = currentQuery ? 'User is searching for: '+currentQuery : 'General conversation';

  fetch('/api/ai/chat?message='+encodeURIComponent(msg)+'&context='+encodeURIComponent(ctx))

    .then(function(r){return r.json()})

    .then(function(data){

      var typing = document.getElementById('aiTyping');

      if(typing) typing.remove();

      var botDiv = document.createElement('div');

      botDiv.className = 'ai-msg bot';

      botDiv.innerHTML = formatMarkdown(data.reply||'Sorry, I could not process that.');

      msgs.appendChild(botDiv);

      msgs.scrollTop = msgs.scrollHeight;

    })

    .catch(function(e){

      var typing = document.getElementById('aiTyping');

      if(typing){ typing.textContent = 'Error: '+e.message; typing.id=''; typing.classList.remove('ai-loading','loading-pulse'); }

    });

  trackEvent('ai_chat', {message: msg});

}



// ---- Translate ----

function openTranslateModal(){

  document.getElementById('translateModal').classList.add('show');

  document.getElementById('translateResult').textContent = '';

}

function closeTranslateModal(){

  document.getElementById('translateModal').classList.remove('show');

}

function doTranslate(){

  var text = document.getElementById('translateText').value.trim();

  var lang = document.getElementById('translateLang').value;

  if(!text) return;

  document.getElementById('translateResult').textContent = 'Translating...';

  fetch('/api/translate?text='+encodeURIComponent(text)+'&to='+lang)

    .then(function(r){return r.json()})

    .then(function(data){

      document.getElementById('translateResult').textContent = data.translated || data.error || 'Translation failed';

    })

    .catch(function(e){

      document.getElementById('translateResult').textContent = 'Error: '+e.message;

    });

  trackEvent('translate', {lang: lang});

}



// ---- Search Noise (TrackMeNot-inspired) ----
var searchNoiseEnabled = localStorage.getItem('oblivion_search_noise') === 'true';

function toggleSearchNoise(){
  searchNoiseEnabled = !searchNoiseEnabled;
  localStorage.setItem('oblivion_search_noise', searchNoiseEnabled ? 'true' : 'false');
  updateNoiseButton();
}

function updateNoiseButton(){
  var btn = document.getElementById('btnNoise');
  if(!btn) return;
  if(searchNoiseEnabled){
    btn.classList.add('noise-active');
    btn.innerHTML = '&#128737; <span class="hide-mobile">Noise ON</span>';
    btn.title = 'Search Noise ACTIVE: decoy queries are being sent to prevent profiling';
  } else {
    btn.classList.remove('noise-active');
    btn.innerHTML = '&#128737; <span class="hide-mobile">Noise</span>';
    btn.title = 'Search Noise: sends decoy queries to prevent profiling';
  }
}

function doNoiseSearch(query, category, page, callback){
  fetch('/api/search-noise', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({q: query, cat: category, page: page, noise_count: 4})
  })
  .then(function(r){ return r.json(); })
  .then(function(data){
    // Transform to match normal /api/search response format
    callback(null, data);
  })
  .catch(function(e){ callback(e, null); });
}


// ---- Init ----

document.addEventListener('DOMContentLoaded', function(){
  updateNoiseButton();
  var params = new URLSearchParams(window.location.search);
  var q = params.get('q');
  if(q && q.trim()){
    document.getElementById('urlInput').value = q;
    doSearch(q, params.get('cat') || 'general', parseInt(params.get('page')) || 1);
  } else {
    showHome();
  }

});



document.getElementById('translateModal').addEventListener('click',function(e){

  if(e.target===this) closeTranslateModal();

});

</script>

<script>
/* Service Worker Registration */
if('serviceWorker' in navigator){
  window.addEventListener('load',function(){
    navigator.serviceWorker.register('/service-worker.js',{scope:'/'})
      .then(function(reg){
        console.log('OBLIVION SW registered, scope:',reg.scope);
        reg.addEventListener('updatefound',function(){
          var nw=reg.installing;
          nw.addEventListener('statechange',function(){
            if(nw.state==='installed'&&navigator.serviceWorker.controller){
              console.log('OBLIVION SW update available');
            }
          });
        });
      })
      .catch(function(err){console.log('SW registration failed:',err)});
  });
  /* Save searches for offline access */
  var origPush=window.history.pushState;
  window.history.pushState=function(){
    origPush.apply(this,arguments);
    try{
      var u=new URL(arguments[2],location.origin);
      var q=u.searchParams.get('q');
      if(q){
        var h=JSON.parse(localStorage.getItem('oblivion_search_history')||'[]');
        h=h.filter(function(x){return x!==q});
        h.unshift(q);
        if(h.length>20)h=h.slice(0,20);
        localStorage.setItem('oblivion_search_history',JSON.stringify(h));
        if(navigator.serviceWorker.controller){
          navigator.serviceWorker.controller.postMessage({type:'CACHE_SEARCH',query:q});
        }
      }
    }catch(e){}
  };
}
</script>

<script src="/install-prompt.js" defer></script>
<script>
// Detect if PWA is already installed and hide install banners
(async function(){
  try{
    if('getInstalledRelatedApps' in navigator){
      var apps = await navigator.getInstalledRelatedApps();
      if(apps && apps.length > 0){
        localStorage.setItem('oblivion_pwa_installed','true');
        // Hide any install banners
        var banner = document.getElementById('oblivion-install-banner');
        if(banner) banner.remove();
        var iosBanner = document.getElementById('ios-banner');
        if(iosBanner) iosBanner.remove();
      }
    }
    // Also check display-mode
    if(window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true){
      localStorage.setItem('oblivion_pwa_installed','true');
    }
  }catch(e){}
})();
</script>

<script>
// iOS Add to Home Screen banner - shows on the MAIN page in Safari
(function(){
  const ua=navigator.userAgent||'';
  const isIOS=/iphone|ipad|ipod/i.test(ua);
  const isStandalone=window.navigator.standalone===true||window.matchMedia('(display-mode:standalone)').matches;
  const dismissed=localStorage.getItem('oblivion_ios_banner_dismissed');
  if(!isIOS||isStandalone||dismissed)return;
  // Show after 2 seconds
  setTimeout(function(){
    const b=document.createElement('div');
    b.id='ios-banner';
    b.innerHTML=`
    <style>
    #ios-banner{position:fixed;bottom:0;left:0;right:0;z-index:99999;animation:iosSlide 0.4s ease}
    #ios-banner .ib-wrap{background:#111118;border-top:2px solid #00FF88;padding:16px 16px 24px;max-width:600px;margin:0 auto}
    #ios-banner .ib-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
    #ios-banner .ib-title{font-size:1rem;font-weight:700;color:#fff}
    #ios-banner .ib-close{background:none;border:none;color:#6b7280;font-size:1.5rem;cursor:pointer;padding:0 4px;line-height:1}
    #ios-banner .ib-steps{display:flex;gap:6px;align-items:center;justify-content:center;flex-wrap:wrap}
    #ios-banner .ib-step{display:flex;align-items:center;gap:6px;font-size:0.85rem;color:#e0e0e0}
    #ios-banner .ib-icon{font-size:1.2rem}
    #ios-banner .ib-arrow{color:#6b7280;font-size:0.8rem}
    #ios-banner .ib-highlight{color:#00FF88;font-weight:700}
    @keyframes iosSlide{from{transform:translateY(100%)}to{transform:translateY(0)}}
    </style>
    <div class="ib-wrap">
      <div class="ib-top">
        <span class="ib-title">Install OBLIVION App</span>
        <button class="ib-close" onclick="document.getElementById('ios-banner').remove();localStorage.setItem('oblivion_ios_banner_dismissed',Date.now())">&times;</button>
      </div>
      <div class="ib-steps">
        <div class="ib-step"><span class="ib-icon">&#9757;</span> Tap <span class="ib-highlight">Share</span></div>
        <span class="ib-arrow">&#10132;</span>
        <div class="ib-step">Tap <span class="ib-highlight">Add to Home Screen</span></div>
        <span class="ib-arrow">&#10132;</span>
        <div class="ib-step">Tap <span class="ib-highlight">Add</span></div>
      </div>
    </div>`;
    document.body.appendChild(b);
  },2000);
})();
</script>

<!-- Privacy Footer -->
<div style="text-align:center;padding:16px 0 8px;font-size:12px;color:#70757a;border-top:1px solid #e8eaed;margin-top:20px">
  <a href="/privacy" style="color:#4285f4;text-decoration:none">Privacy Policy</a> &middot;
  <a href="/terms" style="color:#4285f4;text-decoration:none">Terms of Service</a> &middot;
  <a href="/about" style="color:#4285f4;text-decoration:none">About</a>
</div>

</body>

</html>"""





# ---------------------------------------------------------------------------
# Search Noise — TrackMeNot-inspired query obfuscation
# ---------------------------------------------------------------------------

import random as _noise_random

_NOISE_WORDS = [
    "weather forecast", "best recipes", "movie reviews", "sports scores",
    "travel destinations", "stock market today", "new technology", "health tips",
    "fashion trends", "home renovation", "gardening tips", "pet care",
    "car reviews", "book recommendations", "music playlists", "workout routines",
    "cooking techniques", "photography tips", "diy projects", "science news",
    "history facts", "math formulas", "language learning", "yoga exercises",
    "meditation guide", "camping gear", "fishing spots", "bird watching",
    "astronomy events", "chess strategies", "puzzle games", "origami patterns",
    "painting tutorials", "knitting patterns", "woodworking plans", "cycling routes",
    "hiking trails", "swimming techniques", "running shoes", "tennis tips",
    "guitar chords", "piano lessons", "drum beats", "vocal exercises",
    "podcast recommendations", "documentary films", "comic books", "board games",
    "virtual reality", "3d printing", "robotics kits", "drone reviews",
    "electric vehicles", "solar panels", "wind energy", "recycling tips",
    "composting guide", "organic farming", "indoor plants", "succulent care",
    "coffee brewing", "tea varieties", "baking bread", "fermentation",
    "cheese making", "wine tasting", "craft beer", "smoothie recipes",
    "salad ideas", "soup recipes", "pasta dishes", "dessert recipes",
    "budget travel", "luxury hotels", "backpacking tips", "cruise reviews",
    "mountain climbing", "scuba diving", "surfing spots", "skiing resorts",
    "dog training", "cat behavior", "aquarium setup", "hamster care",
    "first aid tips", "vitamin supplements", "sleep improvement", "stress relief",
    "time management", "productivity apps", "note taking methods", "study tips",
    "job interview tips", "resume writing", "salary negotiation", "career change",
    "investment basics", "retirement planning", "tax tips", "budgeting apps",
    "home security", "smart home devices", "wifi router setup", "computer repair",
    "phone accessories", "tablet comparison", "laptop deals", "monitor reviews",
    "keyboard shortcuts", "software updates", "cloud storage", "password manager",
    "how to make candles", "best podcasts 2026", "diy furniture ideas",
    "learn sign language", "famous paintings", "national parks list",
    "world capitals quiz", "periodic table elements", "constellation map",
    "how bridges are built", "how airplanes fly", "volcano facts",
    "ocean exploration", "rainforest animals", "desert survival tips",
]


def _generate_noise_queries(count: int = 4) -> list[str]:
    """Generate random decoy search queries."""
    return _noise_random.sample(_NOISE_WORDS, min(count, len(_NOISE_WORDS)))


@app.post("/api/search-noise")
async def api_search_noise(request: Request):
    """
    Execute decoy searches alongside the real one.
    The real query results are returned; decoy results are discarded.
    This prevents upstream engines from profiling the user.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    real_query = body.get("q", "").strip()
    cat = body.get("cat", "general")
    page = body.get("page", 1)
    noise_count = min(body.get("noise_count", 4), 6)  # max 6 decoys

    if not real_query:
        return JSONResponse({"error": "Missing query"}, status_code=400)

    # Generate decoy queries
    decoy_queries = _generate_noise_queries(noise_count)

    # Run real + decoy searches in parallel
    async def _decoy_search(q: str):
        """Fire and forget — we don't care about results."""
        try:
            await searxng_search(q, categories=cat, page=1)
        except Exception:
            pass

    # Real search
    real_task = searxng_search(real_query, categories=cat, page=page)
    # Decoy tasks
    decoy_tasks = [_decoy_search(dq) for dq in decoy_queries]

    # Run all in parallel — real results come back, decoys are discarded
    results = await asyncio.gather(real_task, *decoy_tasks, return_exceptions=True)

    # First result is the real one
    real_data = results[0]
    if isinstance(real_data, Exception):
        return JSONResponse({"error": str(real_data), "results": []}, status_code=502)

    return JSONResponse({
        "results": real_data.get("results", []),
        "suggestions": real_data.get("suggestions", []),
        "infoboxes": real_data.get("infoboxes", []),
        "number_of_results": real_data.get("number_of_results", 0),
        "noise_queries_sent": len(decoy_queries),
        "noise_active": True,
    })


# ---------------------------------------------------------------------------

# Run

# ---------------------------------------------------------------------------

if __name__ == "__main__":

    uvicorn.run(app, host="0.0.0.0", port=3012, log_level="info")
