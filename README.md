# Server Shop Bot

A Telegram bot for selling Hetzner Cloud servers to customers in Iran. It bills from a wallet in IRR and comes with a web admin dashboard. The provider layer is pluggable, so more datacenters can be added later.

## Components

| Process | Command | Role |
|---|---|---|
| **bot** | `python -m app.bot` | aiogram 3 bot (Persian UI). Validates requests and queues jobs. It never waits on slow provider calls. |
| **worker** | `python -m app.worker` | Runs queued jobs (create, IP change, rebuild…) and periodic tasks (billing, balance enforcement, abuse detection, pool top-up, status sync). |
| **web** | `uvicorn app.web.main:app` | Admin dashboard (English) built with FastAPI and server-rendered pages. |
| **db** | PostgreSQL | All state, including the durable job queue (`jobs` table, `SKIP LOCKED`). |
| redis | optional | Holds bot conversation state, so it survives restarts. |

```
app/
  providers/      CloudProvider interface, Hetzner implementation, fake provider, registry
  services/       business logic: pricing, wallet, billing, purchase, IPs, top-ups, abuse, pool, stats
  bot/            handlers, keyboards, Persian texts, middlewares (DB session, ToS/channel gate, throttling)
  worker/         job handlers, job runner, periodic tasks
  web/            dashboard routes + Jinja templates
alembic/          migrations
docs/ADDING_A_PROVIDER.md
tests/            39 tests (purchase flows on the fake provider, mocked Hetzner API, dashboard)
```

## One-command install (Ubuntu / Debian server)

```bash
curl -fsSL https://raw.githubusercontent.com/MaxTeller95/hetzner-sales-bot/main/install.sh | sudo bash
```

The script installs Docker, clones the repo to `/opt/hetzner-sales-bot`, and asks for the bot token, admin ids, Hetzner token and an optional domain (which gets automatic HTTPS through Caddy). It then generates the secrets, starts everything and imports the Hetzner catalog. To manage it afterwards:

```bash
sudo bash /opt/hetzner-sales-bot/install.sh update     # pull latest code, rebuild, re-sync prices
sudo bash /opt/hetzner-sales-bot/install.sh backup     # pg_dump + .env copy into ./backups
sudo bash /opt/hetzner-sales-bot/install.sh logs bot   # follow logs (bot | worker | web | db)
sudo bash /opt/hetzner-sales-bot/install.sh status     # also: restart | stop | uninstall
```

## Manual quick start (Docker)

```bash
cp .env.example .env          # fill BOT_TOKEN, ADMIN_IDS, SECRET_KEY, HETZNER_API_TOKEN, ADMIN_PASSWORD
docker compose up -d --build  # runs migrations, then bot + worker + web
docker compose exec web python -m app.cli init   # create the Hetzner provider and import plans/prices/images
```

Then open the dashboard (port 8000 on localhost; put Caddy/nginx with TLS in front, and set `SESSION_HTTPS_ONLY=true`):

1. **Settings → Pricing**: set the **EUR → IRR exchange rate**. Nothing can be bought until it is set. The margin defaults to 35%.
2. **Settings → Wallet**: set the card details shown for top-ups and the low-balance threshold.
3. **Settings → Bot**: set the required channel (the bot must be an admin of it), the support username, and the tutorial and ToS texts.
4. **Plans & prices**: switch plans on or off, optionally override base prices, and set pool targets.
5. **Locations & OS**: rename locations (Persian names are pre-filled) and choose which OS images to offer.

## Local development without Docker or Postgres

```bash
pip install -r requirements-dev.txt
cp .env.example .env                 # DATABASE_URL=sqlite+aiosqlite:///./hetzbot.db
alembic upgrade head
python -m app.cli demo               # fake provider: creates no real servers and costs nothing
python -m app.bot & python -m app.worker & uvicorn app.web.main:app --reload
pytest
```

## Business rules, as implemented

**Pricing** (`app/services/pricing.py`)
- `hourly = ceil((plan_hourly + ipv4_hourly) × rate × (1 + margin))`. Hetzner bills the primary IPv4 separately, so it is included in the price.
- `monthly = hourly × 720 × (1 − monthly_discount)`.
- Base prices come from the provider's API on **Sync** and can be overridden per plan. Rounding is configurable (default: up to 100 IRR).
- Prices are snapshotted when a server is bought. Changing the rate later only affects new purchases.

**Billing** (`app/services/billing.py`, recorded in `billing_events` + `transactions`)
- Each unit is charged **in advance**: hourly servers 1 h, monthly servers 720 h, reserve IPs 1 h. This gives the 1-hour minimum. Deleting a server only stops future charges.
- Every IP change (during purchase or later) charges one hour of IP price for the discarded IP.
- Buying requires `first hour × (1 + purchase_buffer_hours)` in the wallet for hourly billing, or the monthly price for monthly billing.
- Low-balance warning: sent when the balance drops below the threshold, and repeated every N hours.
- Negative balance: servers are powered off (**suspended**), and the user is warned. After `suspend_grace_hours` still negative, servers and reserve IPs are **deleted**. A top-up that brings the balance back to ≥ 0 powers suspended servers back on automatically.

**Purchase flow**: Provider (skipped when only one) → Location → plans ("All" or "Cheapest" tab) → hourly or monthly → OS → summary → create.
1. The order is charged and a job is queued. A pooled server for that plan is used if one is available (reinstalled with the chosen OS).
2. **Iran connectivity check** via check-host.net: 4 Iranian nodes ping the IP, and by default each node must return 4/4 replies (all configurable). If it fails, the IP is swapped automatically and the check runs again, up to 5 times. If it still fails, the order is cancelled and refunded.
3. Once the check passes, the user sees the IP and can **change it (up to 5 times total)** or confirm. Credentials are delivered with copy buttons. Without confirmation the server is auto-delivered after 15 min.
4. After delivery the IP can be changed **once per 24 h**.
- If check-host.net itself is unreachable, admins are alerted and the user can still confirm, so a third-party outage doesn't block sales. Turn off `connectivity_enabled` to skip the check entirely.

**Reserve IPs**: up to 2 per user, priced at 2× the normal IP price per hour. A reserve IP can be **swapped** onto any of the user's servers in the same location. The server's old IP becomes the reserve, so the count stays the same. Swaps count toward the daily IP-change limit.

**Abuse**: a server is locked (user actions blocked, optionally powered off, user and admins notified) when:
- Hetzner reports `locked: true` on the server for more than 10 minutes with no bot operation running, or
- an e-mail from the abuse mailbox (`ABUSE_IMAP_*`) mentions the server's IP.

Admins review reports on **Abuse** and choose *resolve & unlock*, *resolve (keep locked)* or *dismiss*. Admins can also lock a server manually.

**Pool**: set `Pool` on a plan, or create pool servers manually on **Server pool**. The worker keeps that many pre-created. Purchases of the plan take from the pool, so customers still get a server even when the plan is sold out at Hetzner. Pool servers cost you money while they wait.

**Onboarding gate**: on first `/start` the user gets the tutorial and ToS in expandable blocks. The *I agree* button stays locked for 10 s; this is enforced on the server too. After that the user must join the required channel, if one is set.

## Operations notes
- **Hetzner API changes (2026):** the code uses `location` instead of the removed `datacenter` fields and reads plan availability from `server_types[].locations[]`. `/datacenters` returns 410 since 2026-10-01 and is not used.
- Hetzner rate limits (3600 req/h) are respected: `429` responses are retried after `RateLimit-Reset`, and concurrency is capped.
- Job retries use backoff. Jobs left `running` after a crash are re-queued on worker start. Failed jobs appear under **Jobs** with a Retry button and alert admins on Telegram.
- Root passwords and provider API tokens are encrypted at rest with a key derived from `SECRET_KEY`.
- Dashboard: session login, CSRF tokens on every form, login throttling. Keep it behind HTTPS and ideally an IP allow-list.
- Top-ups use manual card-to-card transfers: the user uploads a receipt, and admins approve in Telegram or on the dashboard. A payment gateway (e.g. Zarinpal) can be added as another top-up method that calls `services.topups.approve` from its callback.
- Back up PostgreSQL (`pgdata` volume) regularly.

## Tests
`pytest` runs 39 tests on SQLite with the fake provider. They cover purchase, billing, IP rotation, refunds, out-of-stock, suspension and deletion, top-up resume, reserve IP swap, pool, provider-lock detection, the mocked Hetzner HTTP API (request shapes, 429 retry, IP-change call order), and dashboard auth and pages.
They do **not** call the real Hetzner or check-host.net APIs. Before going live, do one real purchase with a cheap plan.
