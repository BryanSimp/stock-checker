# mc-stock: Micro Center per-store stock checker → Discord

Checks Micro Center product pages (bundles count as products) for stock **at your store** (default: Brentwood, MO, store `095`) and posts to a Discord webhook.

| Discord post | When | Color | Mention |
|---|---|---|---|
| 🟢 Stock alert | item goes SOLD OUT → IN STOCK | green | yes (`DISCORD_MENTION_ID`) |
| 🔄 / 🔴 Change notice | count changes, price changes, or item sells out | green / red | no |
| 📦 Summary | startup, then every `summary_interval_hours` (default 6) | green if anything in stock, else red (yellow if nothing could be checked) | no |
| ⚠️ Error notice | 3 failed checks in a row for an item (posted once until it recovers) | yellow | no |
| ✅ Recovered | first good check after an error notice | green | no |

State lives in the `mc-stock-data` volume, so restarts and redeploys don't repeat alerts.

## Setup (Portainer)

No files or folders to create on the server. Everything is set in Portainer.

1. **Stacks → Add stack.** Either:
   - **Repository**: URL `https://github.com/BryanSimp/stock-checker`, reference `refs/heads/main`, compose path `docker-compose.yml` (the default; `mc-stock/docker-compose.yml` also works), or
   - **Web editor**: paste in `docker-compose.yml`.

   No `.env` file is used. Portainer passes the stack's environment variables in.
2. **Environment variables** on the stack:

   | Variable | Required | Example / default |
   |---|---|---|
   | `DISCORD_WEBHOOK_URL` | yes | `https://discord.com/api/webhooks/...` |
   | `ITEM_1`, `ITEM_2`, ... `ITEM_10` | at least one | `https://www.microcenter.com/product/...` or `GPU\|https://www.microcenter.com/product/...` |
   | `DISCORD_MENTION_ID` | no | your Discord user ID (pinged on in-stock alerts) |
   | `STORE_ID` | no | `095` = **Brentwood, MO** (default) |
   | `INTERVAL_MINUTES` | no | `15` (minimum 5, ±20% jitter) |
   | `SUMMARY_INTERVAL_HOURS` | no | `6` |
   | `TZ` | no | `America/Chicago` |

   An item name is optional. Without one, the item uses the product's page title.
3. **Deploy.** The logs show one line per item, and Discord gets the startup summary.

If the webhook or items are missing, the container logs what's wrong and waits instead of restarting over and over. Fix the variables and click **Update the stack**.

**Updating:**
- Code: **Update the stack** with **Re-pull image** on.
- Items or settings: change the variables, then **Update the stack**.

**Image:** GitHub Actions (`.github/workflows/docker-publish.yml`) runs the tests, then publishes `ghcr.io/bryansimp/stock-checker` for amd64 and arm64. It runs on every push to `main` that touches `mc-stock/`. Tags: `latest`, `sha-<short>`, and `X.Y.Z` for `vX.Y.Z` git tags.

**Storage:** state (`state.json`) and failed-page HTML (`debug/`) live in the Docker named volume `mc-stock-data`, which survives redeploys. To copy the debug files out: `docker cp mc-stock:/data/debug ./debug`.

**Verify parsing once** (container console in Portainer, or on the host):
`docker exec mc-stock python /app/checker.py --once --dry-run`

### Discord webhook
Server Settings → Integrations → Webhooks → New Webhook → pick the channel → Copy Webhook URL.

### Mention ID (optional)
Discord → User Settings → Advanced → Developer Mode on. Then right-click your name → Copy User ID.

### Other stores
The default is Brentwood, MO (`095`). For another store, pick it on microcenter.com and read `storeid=` from the URL, or the `storeid` cookie in DevTools. The checker sends the ID as a `storeid` cookie and as a `?storeid=` URL param on every request. Each log line shows `store=<name>` when the page names the store.

## Without Portainer (plain docker compose)
The same compose file works. Compose reads variables from your shell, or from a `.env` file next to the compose file if one exists (see `.env.example`):
```bash
git clone https://github.com/BryanSimp/stock-checker && cd stock-checker
cp mc-stock/.env.example .env   # fill in DISCORD_WEBHOOK_URL and ITEM_1...
docker compose up -d && docker compose logs -f
```
To run your own build: `docker build -t ghcr.io/bryansimp/stock-checker:latest mc-stock`, then run `docker compose up -d`.

## Advanced settings
These also work as environment variables (in Portainer, add them to the `environment:` block first): `JITTER_PCT` (0.20), `BACKOFF_BASE_SECONDS` (30: on 403/429/503/Cloudflare challenge it waits 30s → 60s → 120s), `MAX_RETRIES` (3), `ITEMS` (several `Name|URL` entries, one per line or separated by `;`), `LOG_LEVEL`.
An optional YAML file at `/data/config.yaml` is also read (see `config.example.yaml`). Environment variables override it, and items from both are combined.

## CLI flags
- `--once`: run one check (sends alerts and a summary), then exit.
- `--dry-run`: print the Discord payloads instead of sending them. State is not saved.
- `--test-discord`: send one test message and exit.

## How parsing works
Each item is fetched with `curl_cffi` impersonating Chrome. If `curl_cffi` isn't installed, it uses `requests` with browser headers. Requests are spaced 2–5 s apart. Stock status is found in this order:
1. **Inventory element**: `.inventoryCnt` (for example `5 NEW IN STOCK`, `25+ NEW IN STOCK`, `SOLD OUT`), then `#pnlInventory` / `.inventory`.
2. **Regex on the visible page text**: `(\d+\+?) (NEW )?IN STOCK` or `SOLD OUT`. The first match on the page wins.
3. **Structured data**: `itemprop="availability"` or JSON-LD `offers.availability`. The log warns when this path is used, because the value may be online availability rather than your store's stock.

Price comes from `itemprop="price"`, then `#pricing`, then JSON-LD `offers.price`, then `data-price` (preferring the element whose `data-id` matches the product ID). The name comes from `og:title`, then `<h1>`.

Each log line shows which path was used, for example `GPU | IN STOCK | count=5 | price=$599.99 | store=Tustin | via element:span.inventoryCnt`. If a page can't be parsed (or Cloudflare serves a challenge), the raw HTML is saved to `/data/debug/<item>-<timestamp>.html` in the volume. The newest 50 are kept.

## Known limits
- **Cloudflare**: Micro Center sits behind Cloudflare. From a datacenter IP, every request (including curl_cffi's Chrome impersonation) gets a "Just a moment..." challenge. That is what happened in the cloud sandbox this was built in. From a home or residential IP it usually works. If it doesn't, you'll get the ⚠️ error notice after 3 checks, and the challenge HTML will be in `/data/debug/`. This tool does not try to solve or evade bot challenges.
- **`!stock` command**: not implemented. A webhook can only send messages. Reading `!stock` needs a separate Discord bot with a token and a gateway connection. The scheduled and startup summaries cover this.
- The parser was tested against hand-written fixtures (`tests/`), not a live page (see above). Run `--once --dry-run` once on your server and check that each line shows the right status, count, price and store.

## Tests
```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```
