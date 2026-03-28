# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in OBLIVION Search, please report it responsibly.

**Email:** admin@oblivionzone.com

**What to include:**
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

**Response time:**
- Acknowledgment within 24 hours
- Assessment within 72 hours
- Fix deployed within 7 days for critical issues

**We will:**
- Acknowledge your report publicly (with your permission)
- Credit you as the discoverer
- Not take legal action against responsible disclosure

## Scope

In scope:
- oblivionsearch.com and all subpaths
- Browser extensions (Chrome, Firefox, Edge)
- API endpoints
- Client-side encryption (vault, paste)

Out of scope:
- Social engineering attacks
- DDoS attacks
- Issues in third-party software (SearXNG, Elasticsearch, etc.)

## Known Security Measures

- All client-side crypto uses Web Crypto API (AES-256-GCM, PBKDF2-SHA256)
- Server never receives plaintext passwords (vault) or paste content (paste)
- HTTPS with HSTS, CSP, X-Content-Type-Options, Referrer-Policy
- Cloudflare WAF + DDoS protection
- Tor .onion access for anonymous usage
