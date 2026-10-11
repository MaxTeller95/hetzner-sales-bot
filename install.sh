#!/usr/bin/env bash
# Hetzner Sales Bot — installer & management script for Ubuntu/Debian servers.
#
#   Install:   curl -fsSL https://raw.githubusercontent.com/MaxTeller95/hetzner-sales-bot/main/install.sh | sudo bash
#   Manage:    sudo bash /opt/hetzner-sales-bot/install.sh <command>
#
# Commands: install (default) | update | status | logs [service] | backup | restart | stop | uninstall
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/MaxTeller95/hetzner-sales-bot.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/hetzner-sales-bot}"

c_green=$'\e[32m'; c_yellow=$'\e[33m'; c_red=$'\e[31m'; c_bold=$'\e[1m'; c_off=$'\e[0m'
info()  { echo "${c_green}==>${c_off} $*"; }
warn()  { echo "${c_yellow}!!${c_off} $*"; }
die()   { echo "${c_red}xx${c_off} $*" >&2; exit 1; }

# Prompts read from the terminal so the script also works via `curl | bash`.
ask() {  # ask VAR "Question" [default] [secret]
  local __var=$1 __q=$2 __def=${3:-} __secret=${4:-} __ans=""
  local __prompt="$__q"; [[ -n $__def ]] && __prompt+=" [$__def]"
  if [[ -n $__secret ]]; then read -r -s -p "$__prompt: " __ans </dev/tty; echo
  else read -r -p "$__prompt: " __ans </dev/tty; fi
  printf -v "$__var" '%s' "${__ans:-$__def}"
}

rand() { tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-48}" || true; }

compose() { (cd "$APP_DIR" && docker compose "$@"); }

require_root() { [[ $EUID -eq 0 ]] || die "Run as root (use sudo)."; }

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    info "Docker already installed: $(docker --version)"
    return
  fi
  info "Installing Docker..."
  apt-get update -y
  apt-get install -y ca-certificates curl git
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
}

fetch_code() {
  command -v git >/dev/null 2>&1 || { apt-get update -y && apt-get install -y git; }
  if [[ -d $APP_DIR/.git ]]; then
    info "Updating code in $APP_DIR"
    git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
  else
    info "Cloning $REPO_URL into $APP_DIR"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  fi
}

write_env() {
  local env_file="$APP_DIR/.env"
  if [[ -f $env_file ]]; then
    warn ".env already exists — keeping it. Edit $env_file manually to change settings."
    return
  fi
  echo
  echo "${c_bold}Configuration${c_off} (press Enter to accept defaults)"
  local BOT_TOKEN ADMIN_IDS HETZNER_API_TOKEN ADMIN_USER ADMIN_PASS RATE_EUR DOMAIN
  ask BOT_TOKEN "Telegram bot token (from @BotFather)"
  [[ -n $BOT_TOKEN ]] || die "Bot token is required."
  ask ADMIN_IDS "Admin Telegram user id(s), comma-separated"
  ask HETZNER_API_TOKEN "Hetzner Cloud API token (Read & Write)" "" secret
  ask ADMIN_USER "Dashboard username" "admin"
  ask ADMIN_PASS "Dashboard password (empty = generate)" "" secret
  [[ -n $ADMIN_PASS ]] || { ADMIN_PASS=$(rand 20); GENERATED_PASS=$ADMIN_PASS; }
  ask RATE_EUR "EUR → IRR exchange rate (can be changed later in the dashboard)" "0"
  ask DOMAIN "Domain for the dashboard with automatic HTTPS (empty = local only)"

  local https=false base_url="http://127.0.0.1:8000"
  if [[ -n $DOMAIN ]]; then https=true; base_url="https://$DOMAIN"; fi

  umask 077
  cat >"$env_file" <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
SECRET_KEY=$(rand 64)
POSTGRES_PASSWORD=$(rand 32)
HETZNER_API_TOKEN=$HETZNER_API_TOKEN
ADMIN_USERNAME=$ADMIN_USER
ADMIN_PASSWORD=$ADMIN_PASS
WEB_BASE_URL=$base_url
SESSION_HTTPS_ONLY=$https
DEFAULT_EXCHANGE_RATE_EUR=$RATE_EUR
DEFAULT_EXCHANGE_RATE_USD=0
DEFAULT_PROFIT_MARGIN_PERCENT=35
DEFAULT_LOW_BALANCE_THRESHOLD=500000
ABUSE_IMAP_HOST=
ABUSE_IMAP_PORT=993
ABUSE_IMAP_USER=
ABUSE_IMAP_PASSWORD=
ABUSE_IMAP_FOLDER=INBOX
ABUSE_SENDER_FILTER=hetzner
WORKER_CONCURRENCY=5
LOG_LEVEL=INFO
EOF
  chmod 600 "$env_file"
  info "Wrote $env_file"

  if [[ -n $DOMAIN ]]; then
    cat >"$APP_DIR/Caddyfile" <<EOF
$DOMAIN {
	encode gzip
	reverse_proxy web:8000
}
EOF
    cat >"$APP_DIR/docker-compose.override.yml" <<'EOF'
services:
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    depends_on:
      - web
volumes:
  caddy_data:
EOF
    info "HTTPS reverse proxy (Caddy) configured for $DOMAIN — point its DNS A record to this server."
  fi
}

wait_for_web() {
  info "Waiting for the dashboard to come up..."
  for _ in $(seq 1 60); do
    if compose exec -T web python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  warn "Dashboard did not answer yet; check: sudo bash $APP_DIR/install.sh logs web"
}

cmd_install() {
  require_root
  install_docker
  fetch_code
  write_env
  info "Building and starting containers (first build takes a few minutes)..."
  compose up -d --build
  wait_for_web
  info "Importing Hetzner locations, plans, prices and OS images..."
  compose exec -T web python -m app.cli init || warn "Catalog import failed — check HETZNER_API_TOKEN, then run: sudo bash $APP_DIR/install.sh update"

  local url; url=$(grep '^WEB_BASE_URL=' "$APP_DIR/.env" | cut -d= -f2-)
  echo
  echo "${c_bold}${c_green}Installed.${c_off}"
  echo "  Dashboard: $url"
  [[ $url == http://127.0.0.1* ]] && echo "  (local only — from your PC: ssh -L 8000:127.0.0.1:8000 root@<server-ip>, then open http://127.0.0.1:8000)"
  echo "  Username:  $(grep '^ADMIN_USERNAME=' "$APP_DIR/.env" | cut -d= -f2-)"
  [[ -n ${GENERATED_PASS:-} ]] && echo "  Password:  $GENERATED_PASS   ${c_yellow}(save it now — stored only in $APP_DIR/.env)${c_off}"
  echo
  echo "Next steps in the dashboard: Settings → exchange rate, card info, required channel; Plans → enable plans."
  echo "Manage later with: sudo bash $APP_DIR/install.sh {update|status|logs|backup|restart|stop|uninstall}"
}

cmd_update() {
  require_root
  fetch_code
  compose up -d --build
  wait_for_web
  compose exec -T web python -m app.cli sync || warn "Catalog sync failed."
  info "Updated."
}

cmd_backup() {
  require_root
  mkdir -p "$APP_DIR/backups"
  local file="$APP_DIR/backups/db-$(date +%Y%m%d-%H%M%S).sql.gz"
  compose exec -T db pg_dump -U hetzbot hetzbot | gzip >"$file"
  cp "$APP_DIR/.env" "$APP_DIR/backups/env-$(date +%Y%m%d-%H%M%S)"
  chmod 600 "$APP_DIR"/backups/*
  info "Backup written: $file (plus a copy of .env — SECRET_KEY is needed to read stored passwords)"
}

cmd_uninstall() {
  require_root
  local confirm
  ask confirm "This stops the bot and DELETES the database volume. Type 'delete' to continue"
  [[ $confirm == delete ]] || die "Aborted."
  compose down -v
  info "Containers and volumes removed. Code and .env remain in $APP_DIR (remove manually if wanted)."
}

case "${1:-install}" in
  install)   cmd_install ;;
  update)    cmd_update ;;
  status)    compose ps ;;
  logs)      compose logs -f --tail=200 ${2:-} ;;
  backup)    cmd_backup ;;
  restart)   require_root; compose restart ;;
  stop)      require_root; compose stop ;;
  uninstall) cmd_uninstall ;;
  *) die "Unknown command: $1 (use install|update|status|logs|backup|restart|stop|uninstall)" ;;
esac
