"""URL / domain helpers."""
from __future__ import annotations

from urllib.parse import urlparse

import tldextract

# Offline: use the bundled public-suffix snapshot, never download at runtime.
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc.lower()}/"


def registered_domain(url: str) -> str:
    """'https://www.shop.abc.co.in/x' -> 'abc.co.in'."""
    if "://" not in url:
        url = "https://" + url
    ext = _extract(url)
    if not ext.domain:
        return ""
    return f"{ext.domain}.{ext.suffix}".lower() if ext.suffix else ext.domain.lower()


def host(url: str) -> str:
    if "://" not in url:
        url = "https://" + url
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_skipped(url: str, skip_domains: list[str], skip_tlds: list[str]) -> bool:
    dom = registered_domain(url)
    h = host(url)
    if not dom:
        return True
    if any(dom == s or h.endswith("." + s) or h == s for s in skip_domains):
        return True
    return any(h.endswith("." + t) for t in skip_tlds)
