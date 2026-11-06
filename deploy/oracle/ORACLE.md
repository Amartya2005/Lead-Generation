# Deploy on Oracle Cloud (Always Free)

You get a free Ubuntu VM that runs the agents on a schedule, plus the review page at
`https://leads.focustriggerautomation.com`. It takes about 30 minutes, most of it in the Oracle console.

```
            Oracle VM (Ubuntu 24.04)
 ┌──────────────────────────────────────────────────┐
 │ Caddy :443 (auto HTTPS) ──▶ gunicorn :8000        │   team on phone/laptop
 │                              review page   ◀──────┼── https://leads.focustriggerautomation.com
 │ systemd timers                                    │
 │   08:40 IST  run        research → draft          │
 │   hourly     send       10:05–17:05 IST, Mon–Sat  │──▶ Gmail SMTP (587)
 │   every 2h   sync-inbox replies + bounces         │◀── Gmail IMAP
 │   23:50      backup     data/backups (14 days)    │
 │ /opt/leadgen/{app, venv, .env, config, data}      │
 └──────────────────────────────────────────────────┘
```

## 1. Create the account

1. Sign up at <https://www.oracle.com/cloud/free/>. A card is needed for verification; Always Free
   resources are not charged.
2. **Home region:** pick **India West (Mumbai)** or **India South (Hyderabad)**. This can't be changed
   later, and Always Free VMs only run in the home region.
3. **Recommended:** upgrade the account to *Pay As You Go* (Billing → Upgrade). Always Free resources
   stay free, and Oracle doesn't reclaim idle PAYG instances. Free-tier VMs that sit mostly idle for a
   week can be stopped and reclaimed, and this tool is idle most of the day. Add a budget alert of ₹100
   (Billing → Budgets) so nothing can cost money unnoticed.

## 2. Create the VM

Compute → Instances → **Create instance**:

| Setting | Value |
|---|---|
| Image | Canonical **Ubuntu 24.04** |
| Shape | **Ampere VM.Standard.A1.Flex**, 2 OCPU, 12 GB RAM (free up to 4 OCPU / 24 GB) |
| Networking | Create a new VCN with a **public subnet**, and tick *Assign a public IPv4 address* |
| SSH keys | *Generate a key pair* and **download the private key** (or paste your own public key) |
| Boot volume | default (≈47 GB, free) |

If you see **"Out of capacity"** for A1, try another availability domain or try again later. Otherwise
choose **VM.Standard.E2.1.Micro** (AMD, 1 GB, also free); the setup script adds swap automatically.

Note the instance's **public IP**.

## 3. Open ports 80 and 443

Instance → Primary VNIC → **Subnet** → Security List → **Add Ingress Rules**, twice:

| Source CIDR | Protocol | Destination port |
|---|---|---|
| 0.0.0.0/0 | TCP | 80 |
| 0.0.0.0/0 | TCP | 443 |

(The setup script opens the same ports in the VM's own firewall.)

## 4. Point a subdomain at the VM

Wherever focustriggerautomation.com's DNS is managed, add:

```
Type: A    Name: leads    Value: <public IP>    TTL: 300
```

Check it from your laptop (it should print the IP):

```bash
nslookup leads.focustriggerautomation.com
```

## 5. Install

```bash
ssh -i ~/Downloads/ssh-key.key ubuntu@<public IP>
```
(On Windows use PowerShell, or PuTTY with the key.)

On the VM:

```bash
git clone https://github.com/Amartya2005/Lead-Generation.git
```

```bash
sudo bash Lead-Generation/deploy/oracle/setup.sh --domain leads.focustriggerautomation.com
```

It installs Python, Caddy and Chromium, creates the `leadgen` service user, sets the timezone to IST,
installs the services and timers, and gets an HTTPS certificate once DNS resolves.

## 6. Configure

```bash
sudo nano /opt/leadgen/.env
```
Fill in `SMTP_PASSWORD` and `IMAP_PASSWORD` (the 16-character **Gmail app password** from
<https://myaccount.google.com/apppasswords>, not the account password) and `GEMINI_API_KEY`.
Keep `DRY_RUN=true` for the first day. Save with Ctrl+O, Enter, Ctrl+X.

```bash
sudo nano /opt/leadgen/config/team.yaml
```
Add each team member: id, name and title.

```bash
sudo nano /opt/leadgen/config/portfolio.yaml
```
Make each description exact, then set `confirmed: true`.

```bash
sudo systemctl restart leadgen-web
```

Create a review-page login for each member (it asks for a password, minimum 10 characters):

```bash
leadgen web-user add amartya --admin
```

```bash
leadgen web-user add priya
```

Admins can act on every lead. Members can act on the leads assigned to them.

## 7. Check and first run

```bash
leadgen test-llm
```

```bash
leadgen test-smtp
```

```bash
leadgen team
```

Start the first research run now instead of waiting for 08:40 tomorrow:

```bash
sudo systemctl start --no-block leadgen-run
```

```bash
journalctl -u leadgen-run -f
```

Then open **https://leads.focustriggerautomation.com**, sign in, and review the drafts.

## 8. Go live

Once a day of drafts looks right, set `DRY_RUN=false`:

```bash
sudo nano /opt/leadgen/.env
```

The next hourly send run (10:05–17:05 IST, Mon–Sat) sends approved drafts: at most 25 per mailbox per
24 hours, 90–300 s apart.

## Day to day

| What | How |
|---|---|
| Review drafts | the review page (works on a phone) |
| See schedules | `systemctl list-timers 'leadgen-*'` |
| Logs | `journalctl -u leadgen-run -n 100` (also `-send`, `-inbox`, `-web`), or `/opt/leadgen/data/logs/run.log` |
| Any CLI command | `leadgen <command>`, e.g. `leadgen report`, `leadgen show 12`, `leadgen send --dry-run` |
| Pause sending | `sudo systemctl stop leadgen-send.timer` (`start` to resume) |
| Change schedule | `sudo systemctl edit leadgen-run.timer` |
| Update the code | `sudo bash /opt/leadgen/app/deploy/oracle/update.sh` (backs up, pulls, runs tests, restarts) |
| Download a backup | `scp -i key ubuntu@<ip>:/opt/leadgen/data/backups/leads-*.db .` (needs `sudo chmod o+r` on the file) |

Changes to `team.yaml` and `portfolio.yaml` apply to the review page without a restart. The scheduled
runs pick them up on their next run. Changes to `.env` need `sudo systemctl restart leadgen-web`.

## Troubleshooting

- **Page doesn't load / certificate error:** check that DNS resolves to the VM and that ports 80/443 are
  open in the security list. Then run `journalctl -u caddy -n 50`. Caddy keeps retrying the certificate
  on its own.
- **`test-smtp` fails with 535:** you used the account password. Create a Gmail app password, which needs
  2-Step Verification turned on.
- **Research finds nothing:** DuckDuckGo sometimes rate-limits cloud IPs. The run stops searching and
  tries again tomorrow. You can add leads by hand with `leadgen add-lead https://company.in`, or add the
  free Google Programmable Search key (see README).
- **Out of memory on the 1 GB micro VM:** set `use_playwright_fallback: false` under `crawl:` in
  `/opt/leadgen/config/settings.yaml`.
- **Without a domain:** run setup without `--domain`. The page then only listens on the VM itself, so
  open it through an SSH tunnel (`ssh -L 8000:127.0.0.1:8000 ubuntu@<ip>`, then http://localhost:8000).
