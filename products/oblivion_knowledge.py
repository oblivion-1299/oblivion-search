#!/usr/bin/env python3
"""
OBLIVION -- Knowledge Panel Service (FastAPI on port 3045)

Searches the local oblivion-wikipedia Elasticsearch index to provide
knowledge panel data for the OBLIVION search frontend.
Enhanced with Wikidata structured facts for rich knowledge cards.

Endpoints:
  GET /api/knowledge?q=QUERY     — returns title, extract, categories, image, Wikipedia URL + Wikidata facts
  GET /api/knowledge/wikidata?q= — Wikidata-only structured facts lookup
  GET /health                    — health check

Designed to be called from oblivion_search.py or directly from the frontend.
"""

import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
from typing import Optional

import asyncpg
import httpx
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ES_HOST = os.getenv("ES_HOST", "http://localhost:9201")
INDEX_NAME = os.getenv("WIKI_INDEX", "oblivion-wikipedia")
PORT = int(os.getenv("KNOWLEDGE_PORT", "3045"))

# PostgreSQL for Wikidata cache
PG_DSN = os.getenv("KNOWLEDGE_PG_DSN", "postgresql://postgres:os.environ.get("DB_PASSWORD", "change_me")@127.0.0.1:5432/oblivion_knowledge")

# Wikipedia thumbnail API for images
WIKI_THUMB_API = "https://en.wikipedia.org/w/api.php"

# Wikidata API
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# Key Wikidata properties to extract
WIKIDATA_PROPERTIES = {
    "P31":   "instance_of",
    "P17":   "country",
    "P18":   "image",
    "P856":  "website",
    "P569":  "birth_date",
    "P570":  "death_date",
    "P6":    "head_of_government",
    "P1082": "population",
    "P112":  "founded_by",
    "P571":  "inception",
    "P169":  "ceo",
    "P36":   "capital",
    "P27":   "citizenship",
    "P106":  "occupation",
    "P19":   "place_of_birth",
    "P20":   "place_of_death",
    "P154":  "logo_image",
    "P1566": "geonames_id",
    "P625":  "coordinates",
    "P1082": "population",
    "P2046": "area",
    "P37":   "official_language",
    "P38":   "currency",
    "P41":   "flag_image",
    "P242":  "locator_map_image",
}

# Cache TTL: 7 days
WIKIDATA_CACHE_TTL = 7 * 86400

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("oblivion-knowledge")

# ---------------------------------------------------------------------------
# HTTP client & DB pool
# ---------------------------------------------------------------------------

_http_client: Optional[httpx.AsyncClient] = None
_pg_pool: Optional[asyncpg.Pool] = None

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager


async def _init_pg():
    """Initialize PostgreSQL connection pool and create cache table."""
    global _pg_pool
    try:
        _pg_pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=10)
        async with _pg_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS wikidata_cache (
                    qid         TEXT PRIMARY KEY,
                    query       TEXT,
                    label       TEXT,
                    description TEXT,
                    facts       JSONB,
                    image_url   TEXT,
                    fetched_at  BIGINT
                );
                CREATE INDEX IF NOT EXISTS idx_wikidata_query ON wikidata_cache(lower(query));
                CREATE INDEX IF NOT EXISTS idx_wikidata_label ON wikidata_cache(lower(label));
            """)
        log.info("Wikidata cache table ready in PostgreSQL")
    except Exception as e:
        log.warning("PostgreSQL not available for Wikidata cache: %s (will work without cache)", e)
        _pg_pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    await _init_pg()
    log.info("OBLIVION Knowledge Panel service starting on port %d (with Wikidata)", PORT)
    yield
    await _http_client.aclose()
    if _pg_pool:
        await _pg_pool.close()

app = FastAPI(
    title="OBLIVION Knowledge Panel",
    description="Wikipedia-powered knowledge panels for OBLIVION Search",
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
# Elasticsearch query helpers
# ---------------------------------------------------------------------------

def build_knowledge_query(query: str) -> dict:
    """Build an Elasticsearch query optimized for knowledge panel lookup.

    Uses multi_match with cross_fields + phrase matching + fuzzy for typos.
    Prioritizes exact title matches heavily.
    """
    return {
        "size": 5,
        "_source": [
            "title", "opening_text", "text", "category", "heading",
            "incoming_links", "popularity_score", "wikibase_item",
            "redirect", "coordinates", "namespace",
        ],
        "query": {
            "bool": {
                "should": [
                    # Exact title match (highest boost)
                    {
                        "term": {
                            "title.keyword": {
                                "value": query,
                                "boost": 100,
                            }
                        }
                    },
                    # Case-insensitive title match
                    {
                        "match_phrase": {
                            "title": {
                                "query": query,
                                "boost": 50,
                            }
                        }
                    },
                    # Fuzzy title match (for misspellings)
                    {
                        "match": {
                            "title": {
                                "query": query,
                                "fuzziness": "AUTO",
                                "boost": 20,
                            }
                        }
                    },
                    # Redirect match (alternative names)
                    {
                        "nested": {
                            "path": "redirect",
                            "query": {
                                "match": {
                                    "redirect.title": {
                                        "query": query,
                                        "fuzziness": "AUTO",
                                        "boost": 15,
                                    }
                                }
                            },
                        }
                    },
                    # Full-text match in opening paragraph
                    {
                        "match_phrase": {
                            "opening_text": {
                                "query": query,
                                "boost": 5,
                            }
                        }
                    },
                ],
                "minimum_should_match": 1,
                # Only main namespace articles
                "filter": [
                    {"term": {"namespace": 0}},
                ],
            }
        },
        "sort": [
            "_score",
            {"incoming_links": {"order": "desc", "missing": "_last"}},
        ],
    }


def extract_first_paragraph(text: str, max_chars: int = 500) -> str:
    """Extract a clean first paragraph from Wikipedia article text."""
    if not text:
        return ""

    # Remove common wiki markup artifacts
    text = re.sub(r'\{\{[^}]*\}\}', '', text)
    text = re.sub(r'\[\[(?:[^|\]]*\|)?([^\]]*)\]\]', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()

    # Take first paragraph or first N chars
    paragraphs = text.split('\n')
    for p in paragraphs:
        p = p.strip()
        if len(p) > 50:
            if len(p) > max_chars:
                # Cut at last sentence boundary within limit
                cut = p[:max_chars]
                last_period = cut.rfind('.')
                if last_period > max_chars // 2:
                    return cut[:last_period + 1]
                return cut + "..."
            return p

    return text[:max_chars] + ("..." if len(text) > max_chars else "")


async def fetch_wikipedia_image(title: str) -> str:
    """Fetch the main image URL for a Wikipedia article via the API."""
    try:
        params = {
            "action": "query",
            "titles": title,
            "prop": "pageimages",
            "format": "json",
            "pithumbsize": 300,
            "pilicense": "any",
        }
        resp = await _http_client.get(
            WIKI_THUMB_API,
            params=params,
            headers={"User-Agent": "OblivionSearch/3.0 (admin@oblivionzone.com)"},
        )
        if resp.status_code == 200:
            data = resp.json()
            pages = data.get("query", {}).get("pages", {})
            for page_id, page_data in pages.items():
                thumb = page_data.get("thumbnail", {})
                if thumb.get("source"):
                    return thumb["source"]
    except Exception as e:
        log.debug("Failed to fetch image for '%s': %s", title, e)
    return ""


# ---------------------------------------------------------------------------
# Wikidata helpers
# ---------------------------------------------------------------------------

async def wikidata_search_entity(query: str, limit: int = 3) -> list:
    """Search Wikidata for entities matching a query string."""
    try:
        params = {
            "action": "wbsearchentities",
            "search": query,
            "language": "en",
            "limit": limit,
            "format": "json",
        }
        resp = await _http_client.get(
            WIKIDATA_API, params=params,
            headers={"User-Agent": "OblivionSearch/3.0 (admin@oblivionzone.com)"},
            timeout=6.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("search", [])
    except Exception as e:
        log.debug("Wikidata search failed for '%s': %s", query, e)
    return []


def _extract_claim_value(claim: dict) -> str:
    """Extract a human-readable value from a Wikidata claim."""
    mainsnak = claim.get("mainsnak", {})
    datavalue = mainsnak.get("datavalue", {})
    vtype = datavalue.get("type", "")
    value = datavalue.get("value", "")

    if vtype == "string":
        return value
    elif vtype == "wikibase-entityid":
        # Return the Q-id; we resolve labels separately
        return value.get("id", "")
    elif vtype == "time":
        # Parse +1955-10-28T00:00:00Z to 1955-10-28
        t = value.get("time", "")
        # Strip leading + and trailing time part
        t = re.sub(r'^\+', '', t)
        t = re.sub(r'T.*$', '', t)
        return t
    elif vtype == "quantity":
        amount = value.get("amount", "")
        # Strip leading +
        amount = amount.lstrip("+")
        # Format with commas for readability
        try:
            num = float(amount)
            if num == int(num):
                return f"{int(num):,}"
            return f"{num:,.2f}"
        except (ValueError, TypeError):
            return amount
    elif vtype == "globecoordinate":
        lat = value.get("latitude", 0)
        lon = value.get("longitude", 0)
        return f"{lat:.4f}, {lon:.4f}"
    elif vtype == "monolingualtext":
        return value.get("text", "")
    return str(value) if value else ""


async def _resolve_entity_labels(qids: list) -> dict:
    """Resolve a batch of Q-IDs to their English labels."""
    if not qids:
        return {}
    # Wikidata supports up to 50 IDs per request
    labels = {}
    for i in range(0, len(qids), 50):
        batch = qids[i:i+50]
        try:
            params = {
                "action": "wbgetentities",
                "ids": "|".join(batch),
                "props": "labels",
                "languages": "en",
                "format": "json",
            }
            resp = await _http_client.get(
                WIKIDATA_API, params=params,
                headers={"User-Agent": "OblivionSearch/3.0 (admin@oblivionzone.com)"},
                timeout=8.0,
            )
            if resp.status_code == 200:
                entities = resp.json().get("entities", {})
                for qid, ent in entities.items():
                    lbl = ent.get("labels", {}).get("en", {}).get("value", qid)
                    labels[qid] = lbl
        except Exception as e:
            log.debug("Failed to resolve labels for %s: %s", batch, e)
    return labels


async def fetch_wikidata_facts(qid: str) -> dict:
    """Fetch structured facts for a Wikidata entity by Q-ID.

    Returns dict with resolved human-readable property names and values.
    """
    # Check cache first
    if _pg_pool:
        try:
            async with _pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT facts, fetched_at FROM wikidata_cache WHERE qid=$1", qid
                )
                if row and (time.time() - row["fetched_at"]) < WIKIDATA_CACHE_TTL:
                    return json.loads(row["facts"])
        except Exception:
            pass

    try:
        params = {
            "action": "wbgetentities",
            "ids": qid,
            "format": "json",
            "languages": "en",
            "props": "labels|descriptions|claims|sitelinks",
        }
        resp = await _http_client.get(
            WIKIDATA_API, params=params,
            headers={"User-Agent": "OblivionSearch/3.0 (admin@oblivionzone.com)"},
            timeout=8.0,
        )
        if resp.status_code != 200:
            return {}

        data = resp.json()
        entity = data.get("entities", {}).get(qid, {})
        if not entity or "missing" in entity:
            return {}

        label = entity.get("labels", {}).get("en", {}).get("value", "")
        description = entity.get("descriptions", {}).get("en", {}).get("value", "")
        claims = entity.get("claims", {})

        # Extract facts for our key properties
        raw_facts = {}
        entity_refs = []  # Q-IDs we need to resolve to labels
        for pid, prop_name in WIKIDATA_PROPERTIES.items():
            if pid not in claims:
                continue
            claim_list = claims[pid]
            values = []
            for claim in claim_list[:3]:  # max 3 values per property
                val = _extract_claim_value(claim)
                if val:
                    values.append(val)
                    if val.startswith("Q") and val[1:].isdigit():
                        entity_refs.append(val)
            if values:
                raw_facts[prop_name] = values

        # Resolve entity references to labels
        if entity_refs:
            label_map = await _resolve_entity_labels(entity_refs)
            for prop_name, values in raw_facts.items():
                raw_facts[prop_name] = [
                    label_map.get(v, v) if (v.startswith("Q") and v[1:].isdigit()) else v
                    for v in values
                ]

        # Build image URL from Wikidata filename
        image_url = ""
        if "image" in raw_facts:
            fname = raw_facts["image"][0]
            if not fname.startswith("http"):
                fname_encoded = urllib.parse.quote(fname.replace(" ", "_"))
                md5 = hashlib.md5(fname.replace(" ", "_").encode()).hexdigest()
                image_url = f"https://upload.wikimedia.org/wikipedia/commons/thumb/{md5[0]}/{md5[:2]}/{fname_encoded}/300px-{fname_encoded}"

        # Flatten single-value lists
        facts = {}
        for k, v in raw_facts.items():
            if k in ("image", "logo_image", "flag_image", "locator_map_image"):
                continue  # Skip image filenames from facts display
            facts[k] = v[0] if len(v) == 1 else v

        result = {
            "qid": qid,
            "label": label,
            "description": description,
            "facts": facts,
        }
        if image_url:
            result["wikidata_image"] = image_url

        # Store in cache
        if _pg_pool:
            try:
                async with _pg_pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO wikidata_cache (qid, query, label, description, facts, image_url, fetched_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (qid) DO UPDATE SET
                            facts = EXCLUDED.facts,
                            image_url = EXCLUDED.image_url,
                            fetched_at = EXCLUDED.fetched_at
                    """, qid, label.lower(), label, description,
                        json.dumps(facts), image_url, int(time.time()))
            except Exception as e:
                log.debug("Cache write failed: %s", e)

        return result

    except Exception as e:
        log.warning("Wikidata fetch failed for %s: %s", qid, e)
        return {}


async def wikidata_lookup(query: str) -> dict:
    """Full Wikidata lookup: search for entity, then fetch its facts."""
    # Check cache by query
    if _pg_pool:
        try:
            async with _pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT qid, label, description, facts, image_url, fetched_at "
                    "FROM wikidata_cache WHERE lower(query)=$1 OR lower(label)=$1",
                    query.lower()
                )
                if row and (time.time() - row["fetched_at"]) < WIKIDATA_CACHE_TTL:
                    result = {
                        "qid": row["qid"],
                        "label": row["label"],
                        "description": row["description"],
                        "facts": json.loads(row["facts"]),
                    }
                    if row["image_url"]:
                        result["wikidata_image"] = row["image_url"]
                    return result
        except Exception:
            pass

    # Search for entity
    results = await wikidata_search_entity(query, limit=1)
    if not results:
        return {}

    qid = results[0].get("id", "")
    if not qid:
        return {}

    return await fetch_wikidata_facts(qid)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Health check — verifies Elasticsearch + PostgreSQL connectivity."""
    result = {"status": "ok", "service": "oblivion-knowledge", "version": "2.0.0"}
    try:
        resp = await _http_client.get(f"{ES_HOST}/{INDEX_NAME}/_count")
        count = resp.json().get("count", 0)
        result["elasticsearch"] = {"status": "ok", "index": INDEX_NAME, "doc_count": count}
    except Exception as e:
        result["elasticsearch"] = {"status": "error", "error": str(e)}

    if _pg_pool:
        try:
            async with _pg_pool.acquire() as conn:
                cache_count = await conn.fetchval("SELECT COUNT(*) FROM wikidata_cache")
                result["wikidata_cache"] = {"status": "ok", "cached_entities": cache_count}
        except Exception as e:
            result["wikidata_cache"] = {"status": "error", "error": str(e)}
    else:
        result["wikidata_cache"] = {"status": "disabled"}

    return result


@app.get("/api/knowledge")
async def knowledge_panel(q: str = Query(..., min_length=1, max_length=200)):
    """
    Search for a knowledge panel match in the local Wikipedia index.

    Returns the best-matching article with:
    - title: Article title
    - extract: First 500 chars of the article
    - categories: List of article categories
    - image: Thumbnail URL from Wikipedia
    - url: Link to the Wikipedia article
    - wikibase_item: Wikidata ID (e.g. Q42)
    - score: Elasticsearch relevance score
    """
    query = q.strip()
    if not query:
        return JSONResponse({"error": "Empty query"}, status_code=400)

    try:
        es_query = build_knowledge_query(query)
        resp = await _http_client.post(
            f"{ES_HOST}/{INDEX_NAME}/_search",
            json=es_query,
            headers={"Content-Type": "application/json"},
            timeout=5.0,
        )

        if resp.status_code != 200:
            log.warning("ES returned %d: %s", resp.status_code, resp.text[:200])
            return JSONResponse({"error": "Search failed"}, status_code=502)

        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])

        if not hits:
            return JSONResponse({"found": False, "query": query})

        # Take the best hit
        best = hits[0]
        source = best.get("_source", {})
        score = best.get("_score", 0)

        title = source.get("title", "")
        opening_text = source.get("opening_text", "")
        full_text = source.get("text", "")
        categories = source.get("category", [])
        wikibase_item = source.get("wikibase_item", "")
        incoming_links = source.get("incoming_links", 0)
        coordinates = source.get("coordinates", None)

        # Build extract from opening_text or full text
        extract = extract_first_paragraph(opening_text or full_text)

        # Build Wikipedia URL from title
        wiki_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"

        # Fetch image asynchronously
        image_url = await fetch_wikipedia_image(title)

        # Only return if score is decent (avoid random low-quality matches)
        if score < 5 and incoming_links < 100:
            return JSONResponse({"found": False, "query": query, "reason": "low_relevance"})

        result = {
            "found": True,
            "title": title,
            "extract": extract,
            "categories": categories[:10] if categories else [],
            "image": image_url,
            "url": wiki_url,
            "wikibase_item": wikibase_item,
            "incoming_links": incoming_links,
            "score": round(score, 2),
            "query": query,
        }

        if coordinates:
            result["coordinates"] = coordinates

        # --- Wikidata enrichment ---
        wikidata_facts = {}
        try:
            if wikibase_item:
                wikidata_facts = await fetch_wikidata_facts(wikibase_item)
            else:
                wikidata_facts = await wikidata_lookup(title)

            if wikidata_facts:
                result["wikidata"] = wikidata_facts
                # Use Wikidata image if Wikipedia image is missing
                if not image_url and wikidata_facts.get("wikidata_image"):
                    result["image"] = wikidata_facts["wikidata_image"]
        except Exception as e:
            log.debug("Wikidata enrichment failed for '%s': %s", title, e)

        return JSONResponse(result)

    except httpx.TimeoutException:
        return JSONResponse({"error": "Search timeout"}, status_code=504)
    except Exception as e:
        log.error("Knowledge panel error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/knowledge/wikidata")
async def wikidata_panel(q: str = Query(..., min_length=1, max_length=200)):
    """
    Wikidata-only structured facts lookup.
    Returns structured data from Wikidata for any entity (person, place, org, etc.).
    Results are cached in PostgreSQL for 7 days.
    """
    query = q.strip()
    if not query:
        return JSONResponse({"error": "Empty query"}, status_code=400)

    try:
        # If query looks like a Q-ID, fetch directly
        if re.match(r'^Q\d+$', query, re.IGNORECASE):
            facts = await fetch_wikidata_facts(query.upper())
        else:
            facts = await wikidata_lookup(query)

        if not facts:
            return JSONResponse({"found": False, "query": query})

        return JSONResponse({"found": True, "query": query, **facts})

    except Exception as e:
        log.error("Wikidata lookup error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/knowledge/suggest")
async def knowledge_suggest(q: str = Query(..., min_length=1, max_length=100)):
    """Autocomplete suggestions from Wikipedia titles."""
    try:
        es_query = {
            "suggest": {
                "title-suggest": {
                    "prefix": q,
                    "completion": {
                        "field": "title.suggest",
                        "size": 5,
                        "fuzzy": {"fuzziness": "AUTO"},
                    },
                }
            },
        }
        resp = await _http_client.post(
            f"{ES_HOST}/{INDEX_NAME}/_search",
            json=es_query,
            headers={"Content-Type": "application/json"},
            timeout=3.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            suggestions = []
            for opt in data.get("suggest", {}).get("title-suggest", [{}])[0].get("options", []):
                suggestions.append(opt.get("text", ""))
            return JSONResponse({"suggestions": suggestions})
        return JSONResponse({"suggestions": []})
    except Exception:
        return JSONResponse({"suggestions": []})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
