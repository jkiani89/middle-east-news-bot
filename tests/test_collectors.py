"""Unit tests for collectors and normalizers."""

from app.collectors.rss_collector import canonicalize_url, make_hash


def test_canonicalize_url_strips_utm_and_tracking():
    raw = "https://WWW.AlJazeera.com/news/2026/11/10/event?utm_source=twitter&utm_medium=social&utm_campaign=breaking&fbclid=abc123xyz"
    canonical = canonicalize_url(raw)
    assert canonical == "https://www.aljazeera.com/news/2026/11/10/event"


def test_canonicalize_url_preserves_legitimate_query_params():
    raw = "https://news.un.org/en/story/2026/11/12345?id=99&lang=en&gclid=track"
    canonical = canonicalize_url(raw)
    assert "id=99" in canonical
    assert "lang=en" in canonical
    assert "gclid" not in canonical


def test_canonicalize_url_strips_trailing_slash():
    raw = "https://example.com/middle-east/news/"
    canonical = canonicalize_url(raw)
    assert canonical == "https://example.com/middle-east/news"


def test_make_hash_deterministic_and_whitespace_invariant():
    title = "Breaking: Diplomatic Talks in Geneva"
    text1 = "Delegates met on Tuesday to discuss regional security."
    text2 = "  Delegates  met\non Tuesday to discuss regional   security.  "
    
    hash1 = make_hash(title, text1)
    hash2 = make_hash(title, text2)
    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 length in hex


def test_make_hash_differentiates_distinct_content():
    title1 = "Ceasefire Agreement Signed"
    title2 = "Ceasefire Agreement Violated"
    text = "The parties made announcements regarding the ceasefire."
    
    assert make_hash(title1, text) != make_hash(title2, text)
