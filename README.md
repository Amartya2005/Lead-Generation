# Lead Outreach Agent (free stack)

A multi-agent pipeline that finds Indian SMEs and startups that need a website, automation, a CRM/ERP
or a custom app. It qualifies them, matches each one to a **real** portfolio project, drafts a short
cold email, and sends it over Gmail/Outlook SMTP with safety controls. Nothing goes out until a human
approves it.

**Stack:** Python · LangGraph · Gemini / Groq / Grok / Ollama (free tiers or local) · requests +
BeautifulSoup · Playwright (JS fallback) · DuckDuckGo (`ddgs`) · SQLite (optional Google Sheets export).
No paid APIs.

```
research ─▶ enrich ─▶ qualify ─▶ match ─▶ write ──▶ [human review] ──▶ send
   │           │          │         │        │                            │
 DDG/CSE,   site +     1-10 ICP   portfolio  ≤120 words,               limits, delays,
 IndiaMART/ contact    score +    project    validated,                suppression,
 Justdial   pages,     discard    (real      template                  window, logging
 titles,    robots.txt <6         work only) fallback
 directories
```

## Folder structure

```
Lead-Generation/
├── main.py                    # CLI: run / review / send / web / report / sync-inbox / ...
├── requirements.txt
├── .env.example               # copy to .env
├── config/
│   ├── settings.yaml          # LLM, crawl, limits, send window, storage
│   ├── team.yaml              # company identity + team members who sign/send
│   ├── portfolio.yaml         # the ONLY work emails may reference
│   └── icp.yaml               # cities and tiers, industries, query templates
├── app/
│   ├── config.py  db.py  llm.py  graph.py  review.py
│   ├── agents/
│   │   ├── research.py        # 1. Research Agent
│   │   ├── enrichment.py      # 2. Enrichment Agent
│   │   ├── qualification.py   # 3. Qualification Agent
│   │   ├── matching.py        # 4. Matching Agent
│   │   ├── email_writer.py    # 5. Email Agent
│   │   └── sender.py          # 6. Sender Agent
│   └── tools/
│       ├── http.py            # polite fetcher: robots.txt, per-host delay, cache, Playwright
│       ├── search.py          # DuckDuckGo + optional Google Programmable Search
│       ├── extract.py         # HTML → signals (contacts, tech, freshness, WhatsApp/Excel use)
│       ├── geo.py             # Indian city/state detection + Odisha/tier weighting
│       ├── email_rules.py     # hard rules every email must pass
│       ├── imap_sync.py       # replies/bounces → suppression list (read-only)
│       ├── report.py          # CSV / Google Sheets output
│       ├── locks.py           # stops two senders running at once
│       └── domains.py
│   └── web/                   # review page (Flask): templates/, static/
├── deploy/
│   ├── oracle/setup.sh        # one-command server install (idempotent)
│   ├── oracle/update.sh       # backup, pull, test, restart
│   ├── oracle/ORACLE.md       # step-by-step Oracle Cloud guide
│   └── systemd/               # web service + run/send/inbox/backup timers
├── data/leads.db              # created on first run
├── output/leads.csv           # per-lead report
├── logs/run.log
└── tests/                     # offline tests with fixture sites + fake SMTP
```

## Setup

```bash
cd lead-outreach-agent
```

```bash
python -m venv .venv && source .venv/bin/activate
```
(Windows: `.venv\Scripts\activate`)

```bash
pip install -r requirements.txt
```

```bash
playwright install chromium
```

```bash
cp .env.example .env
```

Then edit `.env` (secrets only; it's git-ignored):

1. **Gmail App Password:** Google won't accept the normal account password over SMTP/IMAP. On
   focustriggerautomation@gmail.com, turn on 2-Step Verification (myaccount.google.com → Security),
   then create an app password at <https://myaccount.google.com/apppasswords>. Put the 16-character code
   in both `SMTP_PASSWORD` and `IMAP_PASSWORD`.
2. **LLM:** put a free key from <https://aistudio.google.com/apikey> in `GEMINI_API_KEY` (or use
   Groq / Ollama, or set `LLM_PROVIDER=none`). If a Gemini model is busy or retired, the next model in
   `llm.default_models` is used automatically. Every agent still works without an LLM, using rules
   and templates.
3. Leave `DRY_RUN=true` for your first runs.

### Company and team

`config/team.yaml` holds the company identity and the people who sign emails:

```yaml
company:
  legal_name: "FocusTrigger Automation Pvt Ltd"   # signature
  short_name: "FocusTrigger Automation"           # "At FocusTrigger Automation, we build..."
  website: "https://focustriggerautomation.com"
  email: "focustriggerautomation@gmail.com"
from_name_format: "{name} | {company}"            # inbox shows: Amartya | FocusTrigger Automation
assignment: default                               # or round_robin across active members
default_member: amartya
members:
  - {id: amartya, name: "Amartya", title: "", email: "", phone: "", active: true}
  - {id: priya, name: "Priya Das", title: "Business Development", email: "", phone: "", active: true}
```

- Each lead is assigned one member when it's matched. That member's name and title go in the
  signature and the From name.
- By default everyone sends from the **shared company mailbox**, and the daily limit (25) is shared too.
  To give a member their own mailbox, with its own 25/day, fill their `email` and add
  `SMTP_PASSWORD_<ID>` (e.g. `SMTP_PASSWORD_PRIYA`) to `.env`.
- Replies go to the mailbox the email was sent from, so `sync-inbox` sees them.
- A personal portfolio (`owner:` in `portfolio.yaml`, e.g. amartya.focustriggerautomation.com) is only
  cited in its owner's emails. Other members get client work instead.

```bash
python main.py team
```
Shows each member, the From line recipients will see, the mailbox, whether credentials are set, and sends in the last 24h.

```bash
python main.py run --as priya
```
New drafts from this run are signed by Priya.

```bash
python main.py assign priya 12 15
```
Hands leads #12 and #15 to Priya. The signature is replaced (an approved draft goes back to review), or
the lead is re-matched if its project was someone else's personal portfolio.

```bash
python main.py send --as priya
```
Sends only Priya's approved leads.

### Confirm your portfolio first

Open `config/portfolio.yaml`. The descriptions were written from the categories you picked
(website, CRM/ERP, automation, custom app, event/community/registration). Make each one exact,
add the client's industry if you like, and set `confirmed: true`. **Emails that cite an unconfirmed
project are drafted but never sent.**

Check your setup:

```bash
python main.py test-llm
```

```bash
python main.py test-smtp
```
Add `--as <member>` to test a member's own mailbox.

```bash
python -m pytest -q
```

## How to run

```bash
python main.py run --max-leads 20
```
Discovers up to 20 new companies, enriches, scores, matches and drafts emails. Nothing is sent.

```bash
python main.py review
```
Shows each draft with its score, reasons and any rule problems. **a**pprove / **e**dit (opens `$EDITOR`) / **r**eject / **s**kip.

To review in a spreadsheet instead:

```bash
python main.py review --export output/review.csv
```
Fill the `decision` column with `approve` or `reject` (you can edit the subject and body), then:

```bash
python main.py review --import output/review.csv
```

Send approved emails (still a dry run while `DRY_RUN=true`):

```bash
python main.py send
```

Check replies and bounces, which also adds them to the suppression list:

```bash
python main.py sync-inbox
```

The output table, Company | Website | Location | Score | Matched Project | Email Draft | Status:

```bash
python main.py report
```
Add `--full` for whole drafts or `--status drafted` to filter. The CSV is always written to `output/leads.csv`.

Other commands:

```bash
python main.py add-lead https://somecompany.in --company "Some Co" --city Cuttack
```

```bash
python main.py show 12
```
Prints every field plus the full event log for one lead.

```bash
python main.py suppress someone@x.in otherdomain.in --reason unsubscribed
```

```bash
python main.py suppress --list
```

Individual stages: `research`, `enrich`, `qualify`, `match`, `draft`, or
`python main.py run --stages enrich,qualify`.

When you're happy with the drafts, set `DRY_RUN=false`. A daily routine is `run`, then `review`,
`send`, and `sync-inbox`. On the server these run on timers, and review happens on the web page.

## Review page

A mobile-friendly page where each team member signs in, reads their drafts with the score and the
reasons behind it, edits the email, and approves or rejects it. Approving re-runs the same email rules
the sender enforces. Members act on their own leads; admins can act on everyone's and reassign them.
There's also a do-not-email list and a team/portfolio status page.

Try it locally:

```bash
python main.py web-user add amartya --admin
```

```bash
python main.py web
```
Then open http://127.0.0.1:8000.

Security: hashed passwords, a CSRF token on every form, Secure/HttpOnly/SameSite session cookies,
lockout after 5 failed logins, a strict Content-Security-Policy, and an audit trail in each lead's
history (`web:<user> approved`).

## Deploy (Oracle Cloud Always Free)

See **[deploy/oracle/ORACLE.md](deploy/oracle/ORACLE.md)**. In short, on a fresh Ubuntu 24.04 VM:

```bash
git clone https://github.com/Amartya2005/Lead-Generation.git
```

```bash
sudo bash Lead-Generation/deploy/oracle/setup.sh --domain leads.focustriggerautomation.com
```

Then fill `/opt/leadgen/.env`, edit `/opt/leadgen/config/team.yaml` and `portfolio.yaml`, and create
logins with `leadgen web-user add <member>`. Server-specific config in `/opt/leadgen/config/` is
deep-merged over the repo's `config/`, so `git pull` never overwrites it.

## Safety controls

| Control | Where | Default |
|---|---|---|
| Human approval: only `approved` drafts are sent | `HUMAN_APPROVAL` / `sending.human_approval` | **ON** |
| Daily limit per mailbox (rolling 24h, counted from actual sends) | `DAILY_SEND_LIMIT` | **25** |
| Random delay between sends | `sending.min/max_delay_seconds` | 90–300 s |
| Send window | `sending.send_window` | Mon–Sat 10:00–18:00 IST |
| Suppression list: emailed / bounced / replied / manual, by email and domain | `suppression` table | always on |
| One email per company domain | `sending.max_per_domain` | 1 |
| Re-validation right before sending (catches bad manual edits) | `sender.check()` | always on |
| Unconfirmed portfolio project blocks sending | `portfolio.yaml` → `confirmed` | always on |
| Personal portfolio only cited by its owner | `portfolio.yaml` → `owner` | always on |
| Inactive or unknown team member blocks sending | `team.yaml` → `active` | always on |
| Opt-out line + `List-Unsubscribe` header | signature | always on |
| Dry run | `DRY_RUN` | true in `.env.example` |

**Email rules** (`app/tools/email_rules.py`) reject any draft that:
- has more than 120 words (greeting to call-to-action; the signature and opt-out line aren't counted)
- links to any domain outside `portfolio.yaml` and your own site
- doesn't reference the matched project
- contains percentages, multipliers, "increased/boosted/doubled", "ROI", "case study", "trusted by",
  "clients like" and similar claims
- contains salesy phrases, `!!`, or leftover placeholders
- contains a number that isn't backed by lead data (e.g. the footer year actually seen on their site)
- has no question or call-to-action at the end

If an LLM draft fails, it's retried once with the problems listed. If it fails again, a deterministic
template that always passes is used instead. The LLM only gets structured facts, not raw page text,
which limits prompt injection from scraped sites.

## Crawling etiquette

- Every company-site request checks **robots.txt** first, for both the original and the redirected host,
  following RFC 9309: no robots.txt means allowed; a server error or unreachable robots.txt means disallowed.
  `Crawl-delay` is honoured, the per-host gap defaults to 6 s, and 429/503 responses trigger a back-off.
- An honest User-Agent with your URL (`crawl.user_agent`). Pages are cached for 72 h, so re-runs don't re-crawl.
- **Google:** `google.com/search` is never scraped, because its robots.txt disallows it. Use the optional
  Programmable Search JSON API (100 free queries/day) by adding `google_cse` to `research.sources`.
  Google has limited this API for new sign-ups, so it may not be available to you.
- **IndiaMART / Justdial:** only the titles that appear in search results are read. The pipeline then
  searches for the company's own site. It does not crawl those directories.
- **DuckDuckGo** is queried through the `ddgs` library, which uses DDG's HTML endpoint and isn't covered by
  our robots.txt checker. Keep volume low (default 8 queries per run, 6–12 s apart). If you get
  rate-limited, the run stops searching and continues with the leads it already has.
- **LinkedIn** is only taken from links on the company's own website. LinkedIn is never crawled.
- **Startup directories:** add public listing pages you're allowed to crawl under
  `research.directory_pages`. robots.txt is still checked.

## Scoring (Qualification Agent)

`score = location (0–3) + need evidence (0–4) + growth (0–2) + has email (0–1) − penalties`, clamped to 1–10.
If an LLM is configured, its score is blended in (`qualification.llm_weight`, default 0.4).

- **Location:** Odisha 3 · Tier-2/Tier-3 2 · metro 1 · India (city unknown) 0.5 · outside India means discard
- **Need evidence:** footer year ≥3 years old, no mobile viewport, no HTTPS, old jQuery, table/Flash layout,
  WhatsApp ordering, Google Forms, Excel downloads, "call to order"/download-the-form flows,
  no online booking or payment, plus an industry prior from `icp.yaml`
- **Growth:** hiring, careers page, new branch/launching, founded in the last 8 years, startup
- **Penalties:** already modern with online booking/payments (−2); listed company or 1000+ employees (−3)

Leads scoring below `qualification.min_score` (6) are marked `discarded`. The reasons for every score are
stored and shown in `review` and `show`.

## Lead statuses

`discovered → enriched → qualified → matched → drafted → approved → sent → replied / bounced`,
plus `no_website` (directory listing only, kept for manual follow-up), `unreachable`, `blocked_robots`,
`discarded`, `needs_contact` (good fit but no public email address), `rejected`, `failed` and `suppressed`.
Every step is written to the `events` table (`python main.py show <id>`).

## Notes

- Free tiers change. If a model name stops working, change `llm.default_models` in `settings.yaml`
  or set `LLM_MODEL`.
- Never commit `.env` or `data/leads.db`. The repository is public, and the database holds every lead's
  email address and status.
- Outlook / Microsoft 365 is retiring password-based SMTP, so it may not accept an app password.
  Gmail is the reliable free option.
- Cold email in India: only email business addresses published on the company's own site, keep the
  opt-out line, and honour every "no" (replies are suppressed automatically by `sync-inbox`). Also
  check Gmail's sender guidelines; 25/day from a warmed-up account is a sensible ceiling.
- Google Sheets: `pip install gspread google-auth`, create a free service account, share the sheet with
  its email address, set `storage.google_sheets.enabled: true`, then run `python main.py report --sheets`.
