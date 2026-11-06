"""Email Agent: writes a short, personalised, non-salesy cold email.

Inputs are ONLY: company name, city, industry, one observed problem (from site
signals), the matched portfolio project's domain + description, and the company /
team-member identity from config/team.yaml (the signature names the assigned member).
The LLM never sees raw website text here, which limits prompt injection from scraped pages.

Every draft (LLM or template) goes through tools.email_rules.validate().
If the LLM draft fails, we retry once with the problems listed, then fall back
to a deterministic template that is known to pass.
"""
from __future__ import annotations

import logging
import re

from app.config import Config
from app.db import DB, loads
from app.llm import LLM
from app.tools.email_rules import split_signature, validate, word_count

log = logging.getLogger("email")

NEED_PHRASE = {
    "website": "fast, mobile-friendly websites",
    "automation": "simple automations for enquiries, follow-ups and orders",
    "crm": "lightweight CRMs to track enquiries and customers",
    "erp": "ERP-style systems for stock, orders and billing",
    "custom_app": "custom web apps built around how a team actually works",
    "registration": "event and community websites with online registration",
}
SUBJECT_TOPIC = {
    "website": "website", "automation": "enquiry handling", "crm": "customer follow-ups",
    "erp": "order and stock tracking", "custom_app": "internal tools", "registration": "online registrations",
}
INDUSTRY_PHRASE = {
    "education": "coaching and education businesses", "healthcare": "clinics and healthcare providers",
    "real_estate": "real estate firms", "manufacturing": "manufacturers", "distribution": "distributors and traders",
    "hospitality": "hotels and restaurants", "retail": "retail businesses", "logistics": "logistics companies",
    "professional": "professional services firms", "interiors": "interior and construction firms",
    "travel": "travel businesses", "events": "event organisers", "startup": "startups",
}

SYSTEM = """You write B2B cold emails for a small Indian software studio. Hard rules:
- Body (greeting to call-to-action, excluding signature) is at most {max_words} words. Aim for 70-100.
- Mention exactly ONE portfolio project: {domain}, described only as: "{description}".
  Do not add any results, numbers, metrics, client names, testimonials or features not in that description.
- Personalise only with the facts given. If observed_problem is empty, don't invent one.
- Professional, warm, plain English. No hype, no exclamation marks, no "I hope this finds you well".
- End with one clear, low-pressure call-to-action question (e.g. a 15-minute call).
- No links other than {domain}. No placeholders like [Name].
- Do NOT write a signature or sign-off; it is added automatically."""


def allowed_numbers(lead: dict) -> set[str]:
    """Numbers an email may contain: facts seen on the lead's site, digits in its name, '15' (minute call)."""
    sig = loads(lead.get("signals"), {})
    return ({str(n) for n in (sig.get("copyright_year"), sig.get("founded")) if n}
            | set(re.findall(r"\d+", lead.get("company") or "")) | {"15"})


def signature(cfg: Config, member: dict) -> str:
    """Sign-off + opt-out line for a team member, from config/team.yaml."""
    c = cfg.company
    site = c["website"].replace("https://", "").replace("http://", "").rstrip("/")
    lines = ["Regards,", member["name"]]
    lines.append(f"{member['title']} | {c['legal_name']}" if member.get("title") else c["legal_name"])
    lines.append(" | ".join(x for x in (site, member.get("phone") or c.get("phone")) if x))
    lines += ["", "Not relevant? Just reply \"no\" and I won't email again."]
    return "\n".join(lines)


def apply_signature(cfg: Config, body: str, member: dict) -> str:
    """Replace whatever signature a draft has with the given member's."""
    main, _ = split_signature(body or "")
    return main.rstrip() + "\n\n" + signature(cfg, member)


def check_draft(cfg: Config, lead: dict, subject: str, body: str) -> list[str]:
    """Validate a (possibly human-edited) draft against the matched project and lead facts."""
    proj = cfg.project(lead.get("matched_project") or "")
    if not proj:
        return ["no matched portfolio project"]
    if not cfg.project_allowed_for(proj, lead.get("sender_id")):
        return [f"{proj['domain']} is {proj['owner']}'s personal portfolio; only they can cite it"]
    return validate(subject or "", body or "", allowed_domains=cfg.allowed_email_domains,
                    required_domain=proj["domain"], allowed_numbers=allowed_numbers(lead),
                    max_words=int(cfg.settings["email"].get("max_words", 120)))


def _short_company(name: str) -> str:
    n = re.sub(r"\b(pvt\.?|private|ltd\.?|limited|llp|inc\.?)\b", "", name or "", flags=re.I)
    n = re.sub(r"\s{2,}", " ", n).strip(" ,.-|")
    return n if 2 <= len(n) <= 45 else (n[:45].rsplit(" ", 1)[0] if n else "your team")


class EmailAgent:
    name = "email"

    def __init__(self, cfg: Config, db: DB, llm: LLM):
        self.cfg, self.db, self.llm = cfg, db, llm
        self.max_words = int(cfg.settings["email"].get("max_words", 120))
        self.use_llm = bool(cfg.settings["email"].get("use_llm", True))

    # ------------------------------------------------------------- pieces
    def facts(self, lead: dict) -> dict:
        proj = self.cfg.project(lead["matched_project"])
        needs = loads(lead["needs"], []) or ["website"]
        return {
            "company": _short_company(lead["company"]),
            "city": lead["city"] or lead["city_hint"] or "",
            "industry": INDUSTRY_PHRASE.get(lead["industry"] or "", "local businesses"),
            "need": needs[0],
            "need_phrase": NEED_PHRASE.get(needs[0], NEED_PHRASE["website"]),
            "observed_problem": lead["observed_problem"] or "",
            "project": proj,
            "sender_company": self.cfg.company["short_name"],
        }

    def template(self, f: dict) -> tuple[str, str]:
        proj = f["project"]
        where = f" in {f['city']}" if f["city"] else ""
        opener = f"I was looking at {f['industry']}{where} and came across {f['company']}."
        if f["observed_problem"]:
            opener += f" I noticed {f['observed_problem']}."
        if proj["kind"] == "client":
            proof = f"One recent project of ours is {proj['domain']}: {proj['description']}."
        else:
            proof = f"You can see examples of our work at {proj['domain']}: {proj['description']}."
        body = (
            f"Hi {f['company']} team,\n\n{opener}\n\n"
            f"At {f['sender_company']}, we build {f['need_phrase']} for growing businesses. {proof}\n\n"
            f"Would you be open to a 15-minute call next week? I can share two or three specific ideas for {f['company']}."
        )
        subject = f"Quick idea for {f['company']}'s {SUBJECT_TOPIC.get(f['need'], 'website')}"
        return subject[:90], body

    def _llm_draft(self, f: dict, feedback: list[str] | None = None) -> tuple[str, str] | None:
        if not (self.use_llm and self.llm.enabled):
            return None
        proj = f["project"]
        system = SYSTEM.format(max_words=self.max_words - 10, domain=proj["domain"], description=proj["description"])
        prompt = (
            f"Facts:\n- company: {f['company']}\n- city: {f['city'] or 'unknown'}\n- industry: {f['industry']}\n"
            f"- observed_problem (from their website, may be empty): {f['observed_problem']}\n"
            f"- what we offer for their main need: {f['need_phrase']}\n- our company: {f['sender_company']}\n"
            f"- project kind: {'client project' if proj['kind'] == 'client' else 'our own site / portfolio'}\n\n"
            'Return JSON: {"subject": "<= 8 words, no clickbait", "body": "greeting + 2-3 short paragraphs + CTA question"}'
        )
        if feedback:
            prompt += "\n\nYour previous draft broke these rules, fix them: " + "; ".join(feedback)
        out = self.llm.json(system, prompt)
        if not out or not out.get("body") or not out.get("subject"):
            return None
        body = re.sub(r"\n\s*(best regards|regards|best|thanks|cheers|sincerely)[\s\S]*$", "", out["body"].strip(), flags=re.I)
        return str(out["subject"]).strip()[:90], body.strip()


    # ---------------------------------------------------------------- api
    def draft(self, lead: dict) -> tuple[str, str, list[str], str]:
        f = self.facts(lead)
        member = self.cfg.member(lead.get("sender_id")) or self.cfg.member(None)
        sig = signature(self.cfg, member)
        check = lambda subj, body: check_draft(self.cfg, lead, subj, body)  # noqa: E731
        source = "template"
        subject, body = self.template(f)
        feedback = None
        for _ in range(2):
            d = self._llm_draft(f, feedback)
            if not d:
                break
            problems = check(d[0], d[1] + "\n\n" + sig)
            if not problems:
                subject, body, source = d[0], d[1], "llm"
                break
            feedback = problems
            log.debug("LLM draft rejected: %s", problems)
        full = body + "\n\n" + sig
        return subject, full, check(subject, full), source

    def draft_one(self, lead) -> tuple[str, list[str]]:
        lead = dict(lead)
        subject, body, problems, source = self.draft(lead)
        status = "drafted" if lead["email"] else "needs_contact"
        self.db.update_lead(lead["id"], email_subject=subject, email_body=body, validation=problems, status=status)
        self.db.log(self.name, "drafted", lead["id"],
                    {"source": source, "sender": lead.get("sender_id"),
                     "words": word_count(split_signature(body)[0]), "problems": problems})
        log.info("Drafted %-35s as %s (%s, %s)", lead["company"][:35], lead.get("sender_id"), source,
                 "OK" if not problems else problems)
        return status, problems

    def run(self, limit: int = 100) -> dict:
        counts = {"drafted": 0, "needs_contact": 0, "invalid": 0}
        for lead in self.db.leads_by_status("matched", limit=limit):
            status, problems = self.draft_one(lead)
            counts[status] += 1
            counts["invalid"] += bool(problems)
        return counts
