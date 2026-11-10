import hashlib
import feedparser
import httpx
from dateutil import parser as date_parser
from datetime import timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}

def canonicalize_url(url: str) -> str:
    p = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACKING]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), urlencode(query), ""))

def make_hash(title: str, text: str) -> str:
    normalized = " ".join((title + " " + text).lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()

async def fetch_feed(feed_url: str):
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        response = await client.get(feed_url, headers={"User-Agent": "MiddleEastNewsBot/0.1"})
        response.raise_for_status()
    parsed = feedparser.parse(response.text)
    results = []
    for entry in parsed.entries:
        url, title = entry.get("link"), entry.get("title", "").strip()
        if not url or not title:
            continue
        raw_date = entry.get("published") or entry.get("updated")
        try:
            published = date_parser.parse(raw_date) if raw_date else None
            if published and not published.tzinfo:
                published = published.replace(tzinfo=timezone.utc)
        except Exception:
            published = None
        summary = entry.get("summary", "") or ""
        results.append({"title": title, "url": url, "canonical_url": canonicalize_url(url),
                        "published_at": published, "text": summary, "content_hash": make_hash(title, summary)})
    return results
