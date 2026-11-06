# Deploy on Vercel + Supabase (free tiers)

```
 Supabase pg_cron + pg_net (every 10 min, 07:30–20:20 IST)   Vercel Cron (daily backup)
              │  GET /api/tick          (+ optional GitHub Actions schedule)  │
              ▼                                                               ▼
 ┌───────────────────── Vercel (region bom1) ──────────────────────┐
 │  api/index.py  = Flask review page + /api/tick                  │   team: https://leads.focustriggerautomation.com
 │  each tick (≤ 4 min): 1 email, inbox sync, qualify/match/draft, │
 │  enrich a few sites, 1 research query                           │──▶ Gmail SMTP/IMAP
 └──────────────────────────────┬──────────────────────────────────┘
                                ▼
              Supabase Postgres (ap-south-1), schema "leadgen"
```

Vercel has no long-running processes or persistent disk, so the design differs from the VM:

* **Database:** Postgres via `DATABASE_URL`. Same tables as SQLite, created automatically, in a
  private `leadgen` schema that Supabase's public REST API can't reach.
* **Scheduling:** the database itself is the clock. A `pg_cron` job (`leadgen-tick`, UTC
  `*/10 2-14 * * 1-6`) calls `/api/tick` through `pg_net`. Check it in Supabase → Integrations → Cron,
  or `select * from cron.job_run_details order by start_time desc limit 5;`. The GitHub Actions
  workflow is a spare clock; extra calls are harmless. `/api/tick` does at most about 4 minutes of work per call. The database enforces
  spacing between ticks (4 minutes) and the random 90–300 s gap between emails, so extra calls change nothing.
* **Sending:** at most one email per tick. Each lead is atomically claimed (`sending`) before the
  mail server is contacted, so it can never be sent twice. A send interrupted half-way is marked
  `failed` and never retried automatically.
* **No Playwright** on Vercel (too large). JavaScript-only sites are read without rendering.

## Set up the database (any Supabase account)

1. In Supabase: **New project** → name `focustrigger-leads`, region **South Asia (Mumbai)**. Use
   *Generate a password* (letters and digits only, so it needs no URL-encoding) and save it.
2. When the project is ready: **Connect** → **Transaction pooler** → copy the URI, e.g.
   `postgresql://postgres.abcd1234:[YOUR-PASSWORD]@aws-1-ap-south-1.pooler.supabase.com:6543/postgres`,
   and put your password in place of `[YOUR-PASSWORD]`.
3. In Vercel → `focustrigger-leads` → Settings → Environment Variables, add `DATABASE_URL` with that
   URI (type *Sensitive*, Production + Preview). Then Deployments → ⋯ → **Redeploy**.

The app creates its tables on first start, turns on row-level security and revokes Supabase's API
roles, so nothing is readable through Supabase's REST API.

## First admin login

While no logins exist, the app writes a one-time **SETUP CODE** to the function log (Vercel →
Project → **Logs**, search `SETUP CODE`). Open `https://leads.focustriggerautomation.com/setup`,
enter the code, pick yourself and choose a password. Add everyone else under **Logins**: each gets a
temporary password and must change it at first sign-in.

## Environment variables (Vercel → Project → Settings → Environment Variables)

| Name | Value |
|---|---|
| `DATABASE_URL` | Supabase pooler URL (transaction mode, port 6543), app role |
| `WEB_SECRET_KEY` | optional; if unset, a random key is generated once and kept in the database |
| `CRON_SECRET` | optional; if set, Vercel Cron sends it and only authorized calls can force a tick |
| `GEMINI_API_KEY` | Google AI Studio key |
| `LLM_PROVIDER` | `gemini` |
| `SMTP_USER`, `IMAP_USER` | `focustriggerautomation@gmail.com` |
| `SMTP_PASSWORD`, `IMAP_PASSWORD` | **Gmail app password** (myaccount.google.com/apppasswords) |
| `DRY_RUN` | `true` until the drafts look right, then `false` |
| `HUMAN_APPROVAL` | `true` |

After changing a variable, redeploy (Deployments → ⋯ → Redeploy) so it takes effect.

## Team and portfolio

Edit `config/team.yaml` and `config/portfolio.yaml` on GitHub (pencil icon → Commit). Vercel
redeploys in about a minute. Logins are managed on the site under **Logins** (admins only).

## Free-tier notes

* **Vercel Hobby** is for personal, non-commercial projects. A company tool should move to Pro
  to stay within Vercel's terms.
* **GitHub** disables scheduled workflows in public repos after 60 days without a commit. That's
  why the main clock is pg_cron in Supabase, with the daily Vercel Cron as a backup.
* **Supabase free** projects pause after a week without activity. The ticks prevent that.
* To pause everything: `select cron.alter_job(1, active := false);` (and `true` to resume).
* DuckDuckGo may rate-limit Vercel's cloud IPs. Research then skips that tick and tries again later.
