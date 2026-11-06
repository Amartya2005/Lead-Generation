# Deploy on Vercel + Supabase (free tiers)

```
 GitHub Actions (every 10 min, 07:30–20:20 IST)      Vercel Cron (daily backup trigger)
              │  GET /api/tick                              │
              ▼                                             ▼
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
* **Scheduling:** `/api/tick` does at most about 4 minutes of work per call. The database enforces
  spacing between ticks (4 minutes) and the random 90–300 s gap between emails, so extra calls change nothing.
* **Sending:** at most one email per tick. Each lead is atomically claimed (`sending`) before the
  mail server is contacted, so it can never be sent twice. A send interrupted half-way is marked
  `failed` and never retried automatically.
* **No Playwright** on Vercel (too large). JavaScript-only sites are read without rendering.

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
* **GitHub** disables scheduled workflows in public repos after 60 days without a commit. The daily
  Vercel Cron keeps the pipeline going, slowly, and any commit re-enables the workflow.
* **Supabase free** projects pause after a week without activity. The ticks prevent that.
* DuckDuckGo may rate-limit Vercel's cloud IPs. Research then skips that tick and tries again later.
