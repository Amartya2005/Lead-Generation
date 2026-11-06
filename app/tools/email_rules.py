"""Hard rules every outgoing email must pass. Used after LLM drafting AND after
human edits, so a draft that breaks a rule can't slip through unnoticed."""
from __future__ import annotations

import re

_TLDS = r"(?:com|in|co\.in|org|net|io|ai|app|dev|co|biz|info|xyz|online|site|tech|store|shop|me|us|uk|tv|ly|gl|link)"
URL_RE = re.compile(
    r"(?:https?://|www\.)((?:[a-z0-9-]+\.)+[a-z]{2,})|\b((?:[a-z0-9-]+\.)+" + _TLDS + r")\b(?![\w-])", re.I)
NUM_RE = re.compile(r"\b\d[\d,.]*\b")

# Phrases that usually signal invented results, hype, or spam triggers.
FORBIDDEN = [
    (r"\d+\s*%", "percentage claim"),
    (r"\b\d+\s*x\b", "multiplier claim"),
    (r"\b(increased|boosted|doubled|tripled|skyrocket\w*|grew)\b", "results claim"),
    (r"\bsaved\s+(?:them\s+|our clients\s+)?\d", "savings claim"),
    (r"\b(roi|guarantee\w*|case stud(y|ies)|testimonial)\b", "unsupported claim"),
    (r"\b(trusted by|clients like|hundreds of|thousands of|award[- ]winning|#1|number one|leading provider)\b", "unsupported claim"),
    (r"\b(act now|limited time|free trial|risk[- ]free|don't miss|exclusive offer|100% free|click here)\b", "salesy phrase"),
    (r"!{2,}", "multiple exclamation marks"),
    (r"\[[^\]]+\]|\{[^}]+\}", "unfilled placeholder"),
]
CTA_RE = re.compile(r"\?|\b(reply|call|chat|let me know|happy to (share|show)|worth a)\b", re.I)


def split_signature(body: str) -> tuple[str, str]:
    """Body = everything before the sign-off line ('Regards', 'Best', 'Thanks', 'Cheers')."""
    m = re.search(r"\n\s*(best regards|regards|best|thanks|thank you|cheers|warm regards|sincerely),?\s*\n", body, re.I)
    return (body[: m.start()], body[m.start():]) if m else (body, "")


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text))


def validate(subject: str, body: str, *, allowed_domains: set[str], required_domain: str,
             allowed_numbers: set[str], max_words: int = 120) -> list[str]:
    """Returns a list of problems. Empty list = OK to send."""
    problems: list[str] = []
    main, _sig = split_signature(body)
    wc = word_count(main)
    if wc > max_words:
        problems.append(f"too long: {wc} words (max {max_words})")
    if not subject.strip():
        problems.append("missing subject")
    elif len(subject) > 90:
        problems.append("subject longer than 90 characters")

    full = f"{subject}\n{body}"
    for d in {(m.group(1) or m.group(2)).lower().removeprefix("www.") for m in URL_RE.finditer(full)}:
        if "@" in full and re.search(r"@" + re.escape(d), full, re.I):
            continue  # part of an email address (sender's own)
        if not any(d == a or d.endswith("." + a) for a in allowed_domains):
            problems.append(f"links to non-portfolio domain: {d}")
    if required_domain.lower() not in full.lower():
        problems.append(f"does not reference the matched project ({required_domain})")

    for pattern, label in FORBIDDEN:
        if re.search(pattern, main, re.I):
            problems.append(f"{label}: '{re.search(pattern, main, re.I).group(0)}'")

    for n in NUM_RE.findall(main):
        n = n.strip(".,")
        if n and n not in allowed_numbers:
            problems.append(f"number not backed by lead data: {n}")

    if not CTA_RE.search(main.split("\n\n")[-1] if "\n\n" in main.strip() else main):
        problems.append("no clear call-to-action in the closing paragraph")
    if main.count("!") > 1:
        problems.append("too many exclamation marks")
    return problems
