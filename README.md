# OBLIVION Search

![License](https://img.shields.io/badge/license-Source--Available-blue)
![Products](https://img.shields.io/badge/products-25%2B-brightgreen)
![Engines](https://img.shields.io/badge/search%20engines-246-00d4ff)
![AI Models](https://img.shields.io/badge/AI%20models-15-7c3aed)
![Privacy Score](https://img.shields.io/badge/privacy%20score-A%2B%20100%2F100-22c55e)
![Uptime](https://img.shields.io/badge/uptime-99.9%25-success)

**Privacy-first AI-powered search engine querying 246 engines simultaneously.**

[Live Demo](https://oblivionsearch.com) | [Privacy Policy](https://oblivionsearch.com/privacy) | [About](https://oblivionsearch.com/about) | [GitHub Issues](https://github.com/oblivion-1299/oblivion-search/issues)

<p align="center">
  <strong>Zero tracking. Zero cookies. Zero user profiles. 25+ products. All on one server.</strong>
</p>

## What is OBLIVION?

OBLIVION is a meta-search engine that queries 246 independent search engines at once and returns aggregated results — without tracking users, setting cookies, or building profiles.

- **246 search engines** queried simultaneously via SearXNG
- **21 search verticals** (Web, Images, Videos, News, Academic, Finance, Weather, Nutrition, Medical, Legal, Patents, Local, Music, Books, Code...)
- **15 AI models** running locally on our own hardware (queries never leave our servers)
- **Scam Shield** — proprietary safety scoring on every result
- **Zero tracking** — no cookies, no fingerprinting, no user profiles, no search history
- **7 access protocols** — HTTPS, Tor (.onion), IPFS, Gemini, Yggdrasil, GUN.js, Blockstream Satellite

## Products

| Product | Description | Path |
|---------|-------------|------|
| Search Engine | Core 246-engine search | `/` |
| Finance | SEC EDGAR company & filing search | `/finance` |
| Weather | Global weather forecasts | `/weather` |
| Nutrition | FDA nutrition facts | `/nutrition` |
| Instant Answers | 20 utility tools (calculator, converter, hash, QR...) | `/instant` |
| Security Tools | DNS, SSL, headers, encrypt, IP lookup | `/security-tools` |
| Privacy Scanner | Website privacy & GDPR grading (A+ to F) | `/privacy-scan` |
| Privacy Report | Visual privacy report cards | `/privacy-report` |
| Password Vault | Stateless PBKDF2 password manager (client-side) | `/vault` |
| Encrypted Paste | Zero-knowledge AES-256-GCM encrypted notepad | `/paste` |
| Link Shortener | Privacy-first URL shortener (no tracking) | `/s` |
| Local Search | 12M+ places worldwide from GeoNames | `/local` |
| Retro Search | GeoCities, Angelfire, BBS archives | `/retro` |
| WebTech | Website technology profiler | `/webtech` |
| Comments | Privacy-first embeddable comment system | `/comments` |
| Trends | Search trends dashboard | `/trends` |

## Tech Stack

- **Backend:** Python 3, FastAPI, aiohttp
- **Search:** SearXNG (246 engines), Elasticsearch, Meilisearch
- **Databases:** PostgreSQL, ClickHouse, Qdrant (vectors)
- **AI:** Ollama (Qwen3 235B, LLaMA 3.3 70B, DeepSeek-R1, Phi-4, + 11 more)
- **Infrastructure:** Docker, systemd, Nginx, Cloudflare
- **Privacy:** Tor, IPFS, Gemini, Yggdrasil, GUN.js

## Quick Start

```bash
# Clone
git clone https://github.com/AtriumArchitectLLC/oblivion-search.git
cd oblivion-search

# Set environment variables
cp .env.example .env
# Edit .env with your credentials

# Install dependencies
pip install -r requirements.txt

# Run the search engine
python search/oblivion_search.py
```

## Environment Variables

Copy `.env.example` and fill in your values:

```
DB_HOST=localhost
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=your_password
SEARXNG_URL=http://localhost:8890
STRIPE_SECRET_KEY=sk_live_your_key
SMTP_USER=your_email@gmail.com
SMTP_PASS=your_app_password
ADMIN_PIN=your_pin
```

## Infrastructure Requirements

Minimum:
- 4 CPU cores, 8GB RAM, 50GB storage
- PostgreSQL 14+
- SearXNG instance

Recommended (what we run):
- 64 CPU cores, 393GB RAM, Tesla T4 GPU, 7.7TB storage
- Full Ollama AI stack
- Elasticsearch, ClickHouse, Qdrant, Meilisearch

## License

**Source-Available** — See [LICENSE](LICENSE)

This code is published for transparency, security auditing, and educational purposes. You may read, inspect, and audit the code freely.

**You may NOT** use this code commercially, host it as a service, or redistribute it without written permission from Oblivion Technologies LLC.

For commercial licensing inquiries: admin@oblivionzone.com

The "OBLIVION" name and logo are trademarks of Oblivion Technologies LLC.

## Company

**Oblivion Technologies LLC**
30 N Gould St Ste R, Sheridan, WY 82801, USA
https://oblivionsearch.com
admin@oblivionzone.com
