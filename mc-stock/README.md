# mc-stock: Micro Center per-store stock checker → Discord

Checks Micro Center product pages (bundles count as products) for stock **at your store** and posts to a Discord webhook.

| Discord post | When | Color | Mention |
|---|---|---|---|
| 🟢 Stock alert | item goes SOLD OUT → IN STOCK | green | yes (`DISCORD_MENTION_ID`) |
| 🔄 / 🔴 Change notice | count changes, price changes, or item sells out | green / red | no |
| 📦 Summary | startup, then every `summary_interval_hours` (default 6) | green if anything in stock, else red (yellow if nothing could be checked) | no |
| ⚠️ Error notice | 3 failed checks in a row for an item (posted once until it recovers) | yellow | no |
| ✅ Recovered | first good check after an error notice | green | no |

State lives in `./data/state.json`, so restarts don't repeat alerts.

## Setup with Portainer (prebuilt image)

GitHub Actions (`.github/workflows/docker-publish.yml`) runs the tests, then builds and pushes `ghcr.io/bryansimp/stock-checker` for amd64 and arm64. It runs on every push to `main` that touches `mc-stock/`, and you can also start it by hand from the Actions tab. Tags: `latest`, `sha-<short>`, and `X.Y.Z` when you push a `vX.Y.Z` git tag.

1. **Host folder (once):**
   ```bash
   sudo mkdir -p /opt/mc-stock
   sudo curl -o /opt/mc-stock/config.yaml https://raw.githubusercontent.com/BryanSimp/stock-checker/main/mc-stock/config.example.yaml
   sudo nano /opt/mc-stock/config.yaml          # store_id + items
   sudo chown -R 1000:1000 /opt/mc-stock        # container runs as uid 1000
   ```
   `state.json` and `debug/` are written to the same folder.
2. **Portainer → Stacks → Add stack.** Either paste in `mc-stock/portainer-stack.yml` (Web editor), or choose **Repository** with URL `https://github.com/BryanSimp/stock-checker`, reference `refs/heads/main`, and compose path `mc-stock/portainer-stack.yml`.
3. **Environment variables** on the stack: `DISCORD_WEBHOOK_URL` (required), `DISCORD_MENTION_ID`, `TZ`, and `MC_STOCK_DIR` if you used a folder other than `/opt/mc-stock`.
4. Deploy. The container logs show one line per item, and Discord gets the startup summary.
5. **Verify parsing (once):** in Portainer, open the container's console (or run `docker exec -it mc-stock python /app/checker.py --once --dry-run` on the host). It prints each item's status, count, price and store without posting to Discord.

**Updating:**
- New code: Stacks → mc-stock → **Update the stack** with **Re-pull image** on. A Repository stack can also use automatic updates or a webhook.
- Config only: edit `/opt/mc-stock/config.yaml`, then restart the container.

## Setup with local build (docker compose)

```bash
cd mc-stock
cp config.example.yaml config.yaml   # set store_id + items
cp .env.example .env                 # set DISCORD_WEBHOOK_URL (+ optional DISCORD_MENTION_ID)
mkdir -p data && sudo chown 1000:1000 data   # container runs as uid 1000

docker compose build
docker compose run --rm checker --test-discord        # webhook check
docker compose run --rm checker --once --dry-run      # verify stock parsing, sends nothing, saves no state
docker compose up -d                                  # start; posts the startup summary
docker compose logs -f
```

After changing `config.yaml`, restart: `docker compose restart`. After changing `checker.py`: `docker compose up -d --build`.

### Discord webhook
Server Settings → Integrations → Webhooks → New Webhook → pick the channel → Copy Webhook URL → paste into `.env`.

### Mention ID (optional)
Discord → User Settings → Advanced → Developer Mode on. Then right-click your name → Copy User ID → `DISCORD_MENTION_ID=` in `.env`.

### Finding your store ID
1. Open microcenter.com in a desktop browser and pick your store (the store selector at the top of the page).
2. Do either of these:
   - Open any product page and look at the URL. If it has `?storeid=NNN`, that number is your ID.
   - DevTools (F12) → Application/Storage → Cookies → `https://www.microcenter.com` → the `storeid` cookie value.
3. Put it in `config.yaml` as `store_id: "NNN"`.

The checker sends the ID as a `storeid` cookie **and** as a `?storeid=` URL param on every request. Each `--once --dry-run` log line shows `store=<name>` when the page names the store. Use it to confirm the ID is the store you expect.

### Adding items
Add an entry under `items:` with any `name` you like and the product page URL (`https://www.microcenter.com/product/<id>/...`). Bundles are product pages, so they work the same way. The URL is the state key: changing a URL resets that item's history, and renaming it does not.

## Config reference (`config.yaml`)

| Key | Default | Notes |
|---|---|---|
| `store_id` | required | numeric string |
| `interval_minutes` | 15 | ±`jitter_pct` random jitter; values below 5 are raised to 5 |
| `jitter_pct` | 0.20 | 0–0.5 |
| `summary_interval_hours` | 6 | |
| `backoff_base_seconds` | 30 | on 403/429/503/Cloudflare challenge: 30s → 60s → 120s (×0.8–1.2) |
| `max_retries` | 3 | retries per item per check before it counts as a failure |
| `items` | required | list of `{name, url}` |

Env (`.env`): `DISCORD_WEBHOOK_URL` (required except with `--dry-run`), `DISCORD_MENTION_ID`, `TZ`, `LOG_LEVEL`.

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

Each log line shows which path was used, for example `GPU | IN STOCK | count=5 | price=$599.99 | store=Tustin | via element:span.inventoryCnt`. If a page can't be parsed (or Cloudflare serves a challenge), the raw HTML is saved to `data/debug/<item>-<timestamp>.html`. The newest 50 are kept.

## Known limits
- **Cloudflare**: Micro Center sits behind Cloudflare. From a datacenter IP, every request (including curl_cffi's Chrome impersonation) gets a "Just a moment..." challenge. That is what happened in the cloud sandbox this was built in. From a home or residential IP it usually works. If it doesn't, you'll get the ⚠️ error notice after 3 checks, and the challenge HTML will be in `data/debug/`. This tool does not try to solve or evade bot challenges.
- **`!stock` command**: not implemented. A webhook can only send messages. Reading `!stock` needs a separate Discord bot with a token and a gateway connection. The scheduled and startup summaries cover this.
- The parser was tested against hand-written fixtures (`tests/`), not a live page (see above). Run `--once --dry-run` once on your server and check that each line shows the right status, count, price and store.

## Tests
```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```
