#!/usr/bin/env bash
# Install or update review-analyzer on the Oracle server. Safe to re-run.
#   bash deploy/install.sh
# Does: Python venv + deps, .env (first run only), systemd service, Caddy + site config,
# and opens TCP 80/443 in the instance's iptables (Oracle's Ubuntu image rejects them).
# The Oracle Cloud *security list* must also allow 80/443; that is done in the web console.
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR=$(pwd)

echo "== Python environment"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p data

if [ ! -f .env ]; then
  echo "== Creating .env from .env.example (add your GEMINI_API_KEY to it)"
  cp .env.example .env
  chmod 600 .env
fi

echo "== systemd service"
sudo install -m 644 deploy/review-analyzer.service /etc/systemd/system/review-analyzer.service
sudo systemctl daemon-reload
sudo systemctl enable --now review-analyzer
sudo systemctl restart review-analyzer

echo "== Caddy"
if ! command -v caddy >/dev/null; then
  sudo apt-get update -q
  sudo apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -q
  sudo apt-get install -y -q caddy
fi
sudo mkdir -p /etc/caddy/sites /var/log/caddy
sudo chown caddy:caddy /var/log/caddy
# Keep each app in its own file so other sites can be added later.
if ! grep -q '^import sites/\*.caddy' /etc/caddy/Caddyfile 2>/dev/null; then
  if grep -q 'The Caddyfile is an easy way' /etc/caddy/Caddyfile 2>/dev/null; then
    # Replace the package's stock welcome-page config.
    echo 'import sites/*.caddy' | sudo tee /etc/caddy/Caddyfile >/dev/null
  else
    echo 'import sites/*.caddy' | sudo tee -a /etc/caddy/Caddyfile >/dev/null
  fi
fi
sudo install -m 644 deploy/review-analyzer.caddy /etc/caddy/sites/review-analyzer.caddy
# Validate as the caddy user so any log files it creates have the right owner.
sudo -u caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
sudo chown -R caddy:caddy /var/log/caddy
sudo systemctl enable caddy
sudo systemctl reload caddy || sudo systemctl restart caddy

echo "== Firewall (iptables)"
# Insert ACCEPT rules just before Oracle's catch-all REJECT, if not already there.
for port in 80 443; do
  if ! sudo iptables -C INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null; then
    reject_line=$(sudo iptables -L INPUT --line-numbers -n | awk '$2=="REJECT"{print $1; exit}')
    sudo iptables -I INPUT "${reject_line:-1}" -p tcp -m state --state NEW --dport "$port" -j ACCEPT
  fi
done
sudo netfilter-persistent save >/dev/null

echo "== Done. Local health check:"
sleep 2
curl -fsS http://127.0.0.1:8010/healthz && echo
echo "App dir: $APP_DIR"
