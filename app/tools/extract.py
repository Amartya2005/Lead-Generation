"""Turn a company web page into structured, *observable* signals.

Every signal here is something we can point at on the page (a meta tag, a link,
a footer year). The Email Agent may only mention problems derived from these.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+91[\s-]?|\b0)?[6-9]\d{4}[\s-]?\d{5}\b")
YEAR_RE = re.compile(r"(?:©|&copy;|copyright)\s*(?:[^0-9]{0,20})((?:19|20)\d{2})(?:\s*[-–]\s*((?:19|20)\d{2}))?", re.I)
JQUERY_RE = re.compile(r"jquery[.-]?(\d)\.(\d+)(?:\.\d+)?(?:\.min)?\.js", re.I)
EMPLOYEES_RE = re.compile(r"(\d{2,6})\s*\+?\s*(?:employees|team members|staff members|professionals)", re.I)
FOUNDED_RE = re.compile(r"(?:since|established in|estd\.?|founded in)\s*((?:19|20)\d{2})", re.I)

BAD_EMAIL_PARTS = ("example.", "domain.com", "sentry", "wixpress", "@2x", ".png", ".jpg", ".jpeg", ".gif",
                   ".webp", ".svg", "u003e", "yourname", "email.com", "test@", "noreply", "no-reply")
ROLE_PREFIXES = ("info", "contact", "hello", "sales", "enquiry", "enquiries", "inquiry", "admin", "office",
                 "support", "business", "care", "mail")

PAGE_KEYWORDS = {
    "contact": ("contact", "reach-us", "get-in-touch", "enquiry", "location"),
    "about": ("about", "who-we-are", "company", "profile"),
    "careers": ("career", "jobs", "join-us", "hiring", "work-with-us"),
}

MANUAL_PROCESS_PHRASES = (
    "whatsapp us", "order on whatsapp", "order via whatsapp", "message us on whatsapp", "chat on whatsapp",
    "send your requirement", "call us to order", "call to order", "call for price", "call for booking",
    "download the form", "download form", "fill the form and", "submit the form at", "visit our office to",
    "price list pdf", "send us a message on whatsapp",
)
GROWTH_PHRASES = (
    "we are hiring", "we're hiring", "join our team", "current openings", "new branch", "now open",
    "expanding", "launching soon", "coming soon", "opening soon", "new outlet", "franchise",
)
ONLINE_TOOLS = {
    "payments": ("razorpay", "paytm", "payu", "instamojo", "cashfree", "stripe.com", "ccavenue", "phonepe"),
    "booking": ("calendly", "book now", "book an appointment", "book appointment", "schedule a call", "setmore", "zoho bookings"),
    "chat": ("tawk.to", "crisp.chat", "intercom", "freshchat", "zopim", "livechat"),
    "analytics": ("googletagmanager", "gtag(", "google-analytics", "clarity.ms"),
}


def _clean_emails(candidates: set[str], site_domain: str) -> list[str]:
    good = []
    for e in candidates:
        e = e.strip(".").lower()
        if any(b in e for b in BAD_EMAIL_PARTS) or len(e) > 80:
            continue
        good.append(e)

    def rank(e: str) -> tuple:
        local, _, dom = e.partition("@")
        same = dom.endswith(site_domain) if site_domain else False
        return (not same, not local.startswith(ROLE_PREFIXES), len(e))

    return sorted(set(good), key=rank)


def extract(html: str, url: str, site_domain: str = "") -> dict:
    soup = BeautifulSoup(html, "lxml")
    lower_html = html.lower()

    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    meta = {m.get("name", m.get("property", "")).lower(): m.get("content", "") for m in soup.find_all("meta")}
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    text = " ".join(soup.get_text(" ").split())
    text_lower = text.lower()

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        links.append((href, " ".join(a.get_text(" ").split())[:80]))

    # ---- contacts
    emails = set(EMAIL_RE.findall(text))
    emails |= {h.split(":", 1)[1].split("?")[0] for h, _ in links if h.lower().startswith("mailto:")}
    phones = sorted({re.sub(r"[\s-]", "", p) for p in PHONE_RE.findall(text)})[:3]
    phones += [h.split(":", 1)[1] for h, _ in links if h.lower().startswith("tel:")][:2]

    whatsapp_links = [h for h, _ in links if re.search(r"wa\.me/|api\.whatsapp\.com|whatsapp://|web\.whatsapp", h, re.I)]
    linkedin = next((h for h, _ in links if "linkedin.com/company" in h.lower()), None) or next(
        (h for h, _ in links if "linkedin.com/" in h.lower()), None)
    socials = sorted({urlparse(h).hostname for h, _ in links
                      if re.search(r"facebook\.com|instagram\.com|youtube\.com|twitter\.com|x\.com/", h, re.I)} - {None})

    # ---- internal pages worth visiting
    base_host = (urlparse(url).hostname or "").removeprefix("www.")
    internal: dict[str, str] = {}
    for href, label in links:
        full = urljoin(url, href)
        h = (urlparse(full).hostname or "").removeprefix("www.")
        if h != base_host or full.split("#")[0].rstrip("/") == url.rstrip("/"):
            continue
        key = (urlparse(full).path + " " + label).lower()
        for kind, words in PAGE_KEYWORDS.items():
            if kind not in internal and any(w in key for w in words):
                internal[kind] = full.split("#")[0]

    # ---- tech & freshness
    generator = meta.get("generator", "")
    tech = []
    checks = {
        "wordpress": "wp-content" in lower_html or "wordpress" in generator.lower(),
        "wix": "wixstatic.com" in lower_html or "wix.com" in generator.lower(),
        "shopify": "cdn.shopify.com" in lower_html,
        "squarespace": "squarespace" in lower_html,
        "joomla": "joomla" in generator.lower() or "/media/jui/" in lower_html,
        "drupal": "drupal" in generator.lower() or "/sites/default/files" in lower_html,
        "blogger": "blogger.com" in lower_html or "blogspot" in lower_html,
        "google_sites": "sites.google.com" in lower_html,
        "nextjs": "/_next/" in lower_html or "__next_data__" in lower_html,
        "react": "data-reactroot" in lower_html or "react-dom" in lower_html,
        "bootstrap": "bootstrap" in lower_html,
        "flash": ".swf" in lower_html,
    }
    tech = [k for k, v in checks.items() if v]
    jq = JQUERY_RE.search(html)
    jquery_version = f"{jq.group(1)}.{jq.group(2)}" if jq else None

    years = []
    for m in YEAR_RE.finditer(text + " " + html[-6000:]):
        years += [int(y) for y in m.groups() if y]
    this_year = date.today().year
    years = [y for y in years if 1995 <= y <= this_year]
    copyright_year = max(years) if years else None

    has_viewport = "viewport" in meta
    layout_tables = len(soup.find_all("table", attrs={"width": True})) + len(soup.find_all("font"))

    online = {k: any(w in lower_html for w in words) for k, words in ONLINE_TOOLS.items()}
    google_forms = "docs.google.com/forms" in lower_html or "forms.gle" in lower_html
    spreadsheet_links = [h for h, _ in links if re.search(r"\.(xlsx?|csv)(\?|$)", h, re.I)]
    manual_phrases = [p for p in MANUAL_PROCESS_PHRASES if p in text_lower]
    growth_phrases = [p for p in GROWTH_PHRASES if p in text_lower]
    emp = EMPLOYEES_RE.search(text)
    founded = FOUNDED_RE.search(text)

    return {
        "url": url,
        "title": title[:200],
        "description": (meta.get("description") or meta.get("og:description") or "")[:300],
        "text": text[:6000],
        "emails": _clean_emails(emails, site_domain),
        "phones": phones[:4],
        "whatsapp_links": len(whatsapp_links),
        "whatsapp_mentions": text_lower.count("whatsapp"),
        "linkedin": linkedin,
        "socials": socials,
        "pages": internal,
        "tech": tech,
        "generator": generator[:80],
        "jquery_version": jquery_version,
        "copyright_year": copyright_year,
        "has_viewport": has_viewport,
        "https": url.lower().startswith("https://"),
        "layout_tables": layout_tables,
        "online": online,
        "google_forms": google_forms,
        "spreadsheet_links": len(spreadsheet_links),
        "manual_phrases": manual_phrases,
        "growth_phrases": growth_phrases,
        "has_careers": "careers" in internal or any(p in text_lower for p in ("careers", "we are hiring", "current openings")),
        "employees": int(emp.group(1)) if emp else None,
        "founded": int(founded.group(1)) if founded else None,
        "listed_company": bool(re.search(r"\b(NSE|BSE)\s*(?:listed|:)|listed on (the )?(NSE|BSE)", text)),
        "word_count": len(text.split()),
    }


def merge(signals: list[dict]) -> dict:
    """Merge homepage + contact/about/careers page signals (homepage wins for page-level facts)."""
    if not signals:
        return {}
    base = dict(signals[0])
    for s in signals[1:]:
        base["emails"] = list(dict.fromkeys(base["emails"] + s["emails"]))
        base["phones"] = list(dict.fromkeys(base["phones"] + s["phones"]))[:4]
        base["whatsapp_links"] += s["whatsapp_links"]
        base["whatsapp_mentions"] += s["whatsapp_mentions"]
        base["linkedin"] = base["linkedin"] or s["linkedin"]
        base["socials"] = sorted(set(base["socials"]) | set(s["socials"]))
        base["google_forms"] = base["google_forms"] or s["google_forms"]
        base["spreadsheet_links"] += s["spreadsheet_links"]
        base["manual_phrases"] = sorted(set(base["manual_phrases"]) | set(s["manual_phrases"]))
        base["growth_phrases"] = sorted(set(base["growth_phrases"]) | set(s["growth_phrases"]))
        base["has_careers"] = base["has_careers"] or s["has_careers"]
        base["employees"] = base["employees"] or s["employees"]
        base["founded"] = base["founded"] or s["founded"]
        base["listed_company"] = base["listed_company"] or s["listed_company"]
        base["online"] = {k: base["online"].get(k) or s["online"].get(k) for k in base["online"]}
        base["text"] = (base["text"] + " " + s["text"])[:9000]
    return base
