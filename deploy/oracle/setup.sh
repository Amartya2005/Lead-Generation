#!/usr/bin/env bash
# One-command install / update on an Oracle Cloud (or any Ubuntu 22.04/24.04) VM.
#
#   git clone https://github.com/Amartya2005/Lead-Generation.git
#   sudo bash Lead-Generation/deploy/oracle/setup.sh --domain leads.focustriggerautomation.com
#
# Safe to re-run: it never overwrites your .env, config/ or data/.
#
# Layout
#   /opt/leadgen/app      code (git checkout; `git pull` safe)
#   /opt/leadgen/venv     Python virtualenv
#   /opt/leadgen/.env     secrets (chmod 600)
#   /opt/leadgen/config   your team.yaml / portfolio.yaml / settings.yaml overrides
#   /opt/leadgen/data     database, backups, CSV report, logs
set -euo pipefail

PREFIX=/opt/leadgen
APP_USER=leadgen
DOMAIN=""
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BRANCH=""
REPO=""

usage() { echo "usage: sudo bash $0 [--domain leads.example.com] [--branch main] [--repo URL]"; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1"; usage ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

# Remember the domain between runs (update.sh re-runs this script).
CONF="$PREFIX/.deploy.conf"
if [[ -z "$DOMAIN" && -f "$CONF" ]]; then
  # shellcheck disable=SC1090
  source "$CONF"
fi

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

step "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq python3 python3-venv python3-pip git sqlite3 curl ca-certificates gnupg \
  apt-transport-https logrotate >/dev/null
timedatectl set-timezone Asia/Kolkata || true

if [[ -n "$DOMAIN" ]] && ! command -v caddy >/dev/null; then
  step "Caddy (automatic HTTPS)"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y -qq && apt-get install -y -qq caddy >/dev/null
fi

step "Swap (only on small VMs)"
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [[ $MEM_MB -lt 2048 && ! -f /swapfile ]]; then
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "added 2 GB swap (RAM ${MEM_MB} MB)"
else
  echo "RAM ${MEM_MB} MB, no swap needed"
fi

step "Service user and folders"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$PREFIX" "$PREFIX/config" "$PREFIX/data" \
  "$PREFIX/data/logs" "$PREFIX/data/backups"

step "Code"
# root runs git on checkouts owned by other users; tell git that's expected.
for d in "$PREFIX/app" "$SRC_DIR"; do
  git config --system --get-all safe.directory 2>/dev/null | grep -qxF "$d" || git config --system --add safe.directory "$d"
done
if [[ ! -d "$PREFIX/app/.git" ]]; then
  ORIGIN="${REPO:-$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null || true)}"
  if [[ "$ORIGIN" =~ ^(https|git|ssh):// || "$ORIGIN" =~ ^git@ ]]; then
    git clone --quiet "$ORIGIN" "$PREFIX/app"
  else
    git clone --quiet --no-local "${ORIGIN:-$SRC_DIR}" "$PREFIX/app"
  fi
fi
if [[ -n "$BRANCH" ]]; then git -C "$PREFIX/app" checkout --quiet "$BRANCH"; fi
chown -R "$APP_USER:$APP_USER" "$PREFIX/app"
echo "at $(git -C "$PREFIX/app" log --oneline -1)"

step "Python environment"
[[ -x "$PREFIX/venv/bin/python" ]] || sudo -u "$APP_USER" python3 -m venv "$PREFIX/venv"
sudo -u "$APP_USER" "$PREFIX/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/app/requirements-full.txt"

step "Playwright Chromium (JS-site fallback; optional)"
if "$PREFIX/venv/bin/playwright" install-deps chromium >/dev/null 2>&1 \
   && sudo -u "$APP_USER" HOME="$PREFIX" "$PREFIX/venv/bin/playwright" install chromium >/dev/null 2>&1; then
  echo "installed"
else
  warn "Playwright browser not installed; JavaScript-only sites will be read without rendering (everything else works)."
fi

step "Config (kept if it already exists)"
if [[ ! -f "$PREFIX/config/settings.yaml" ]]; then
  cat > "$PREFIX/config/settings.yaml" <<EOF
# Server overrides, deep-merged over $PREFIX/app/config/settings.yaml.
# Only put the keys you want to change here.
storage:
  db_path: $PREFIX/data/leads.db
  csv_report: $PREFIX/data/leads.csv
  log_file: $PREFIX/data/logs/run.log
  backup_dir: $PREFIX/data/backups
EOF
fi
for f in team.yaml portfolio.yaml; do
  [[ -f "$PREFIX/config/$f" ]] || cp "$PREFIX/app/config/$f" "$PREFIX/config/$f"
done
chown -R "$APP_USER:$APP_USER" "$PREFIX/config"

if [[ ! -f "$PREFIX/.env" ]]; then
  cp "$PREFIX/app/.env.example" "$PREFIX/.env"
  echo "created $PREFIX/.env from .env.example: fill in the secrets"
fi
grep -q '^WEB_SECRET_KEY=' "$PREFIX/.env" || \
  echo "WEB_SECRET_KEY=\"$(python3 -c 'import secrets; print(secrets.token_hex(32))')\"" >> "$PREFIX/.env"
if [[ -z "$DOMAIN" ]]; then
  # No HTTPS: the page is only reachable through an SSH tunnel, so cookies can't be Secure-only.
  grep -q '^WEB_COOKIE_SECURE=' "$PREFIX/.env" || echo 'WEB_COOKIE_SECURE=false' >> "$PREFIX/.env"
else
  sed -i '/^WEB_COOKIE_SECURE=false/d' "$PREFIX/.env"
fi
chown "$APP_USER:$APP_USER" "$PREFIX/.env"
chmod 600 "$PREFIX/.env"

step "systemd services and timers"
for f in "$PREFIX"/app/deploy/systemd/*; do
  name="$(basename "$f" .in)"
  sed -e "s#@PREFIX@#$PREFIX#g" -e "s#@USER@#$APP_USER#g" "$f" > "/etc/systemd/system/$name"
done
systemctl daemon-reload
systemctl enable --quiet --now leadgen-web.service
systemctl restart leadgen-web.service
for t in run send inbox backup; do systemctl enable --quiet --now "leadgen-$t.timer"; done

step "Command-line helper: leadgen"
cat > /usr/local/bin/leadgen <<EOF
#!/usr/bin/env bash
# Run any main.py command as the service user with the server config, e.g. \`leadgen team\`.
run() { exec env LEADGEN_ENV_FILE=$PREFIX/.env LEADGEN_LOCAL_CONFIG_DIR=$PREFIX/config \\
        $PREFIX/venv/bin/python $PREFIX/app/main.py "\$@"; }
if [[ "\$(id -un)" == "$APP_USER" ]]; then run "\$@"; else exec sudo -u $APP_USER -- "\$0" "\$@"; fi
EOF
chmod 755 /usr/local/bin/leadgen

cat > /etc/logrotate.d/leadgen <<EOF
$PREFIX/data/logs/*.log {
  weekly
  rotate 8
  compress
  missingok
  notifempty
  copytruncate
}
EOF

step "Firewall (OS level)"
open_port() {
  local p=$1
  if iptables -C INPUT -p tcp -m state --state NEW --dport "$p" -j ACCEPT 2>/dev/null; then return; fi
  # Oracle's Ubuntu images end the INPUT chain with a REJECT rule: insert before it.
  local pos
  pos=$(iptables -L INPUT --line-numbers -n | awk '$2=="REJECT" {print $1; exit}')
  if [[ -n "$pos" ]]; then
    iptables -I INPUT "$pos" -p tcp -m state --state NEW --dport "$p" -j ACCEPT
  else
    iptables -A INPUT -p tcp -m state --state NEW --dport "$p" -j ACCEPT
  fi
}
if [[ -n "$DOMAIN" ]] && command -v iptables >/dev/null; then
  open_port 80; open_port 443
  command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null 2>&1 || true
  echo "ports 80 and 443 open (also open them in the Oracle VCN security list)"
fi

if [[ -n "$DOMAIN" ]]; then
  step "HTTPS for $DOMAIN"
  cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:8000
	header {
		Strict-Transport-Security "max-age=31536000"
		-Server
	}
}
EOF
  systemctl enable --quiet caddy
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
  echo "DOMAIN=\"$DOMAIN\"" > "$CONF"
fi

step "Checks"
sleep 2
if curl -fsS http://127.0.0.1:8000/healthz >/dev/null; then echo "review page: running"; else warn "review page not answering; see: journalctl -u leadgen-web -n 50"; fi
systemctl list-timers 'leadgen-*' --no-pager | head -n 7

PUBLIC_IP=$(curl -fsS -m 5 https://ifconfig.me 2>/dev/null || echo "<this VM's public IP>")
cat <<EOF

Done. Next steps:
  1. Secrets:   sudo nano $PREFIX/.env        (Gmail app password, GEMINI_API_KEY; keep DRY_RUN=true at first)
  2. Team:      sudo nano $PREFIX/config/team.yaml
     Portfolio: sudo nano $PREFIX/config/portfolio.yaml   (exact descriptions, then confirmed: true)
  3. Restart:   sudo systemctl restart leadgen-web
  4. Logins:    leadgen web-user add amartya --admin
  5. Check:     leadgen test-llm && leadgen test-smtp && leadgen team
  6. First run: sudo systemctl start leadgen-run   (watch: journalctl -u leadgen-run -f)
EOF
if [[ -n "$DOMAIN" ]]; then
  echo "  7. Open:      https://$DOMAIN   (DNS A record $DOMAIN -> $PUBLIC_IP must exist)"
else
  echo "  7. Open:      ssh -L 8000:127.0.0.1:8000 ubuntu@$PUBLIC_IP   then http://localhost:8000"
fi
