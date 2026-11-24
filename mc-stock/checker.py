#!/usr/bin/env python3
"""Micro Center per-store stock checker with Discord notifications."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
import yaml
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover - depends on environment
    cffi_requests = None

log = logging.getLogger("mc-stock")

MIN_INTERVAL_MINUTES = 5
FAILURE_ALERT_THRESHOLD = 3
MAX_DEBUG_FILES = 50
COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_YELLOW = 0xF1C40F

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

STOP = threading.Event()


# --------------------------------------------------------------------------- config


DEFAULT_STORE_ID = "095"  # Brentwood, MO (St. Louis)
MAX_ENV_ITEMS = 20


@dataclass
class Item:
    name: str
    url: str
    auto_name: bool = False  # no name given; replaced by the page title after the first check


@dataclass
class Config:
    store_id: str
    items: list[Item]
    interval_minutes: float = 15
    jitter_pct: float = 0.20
    summary_interval_hours: float = 6
    backoff_base_seconds: float = 30
    max_retries: int = 3
    webhook_url: str = ""
    mention_id: str = ""


class ConfigError(Exception):
    pass


def name_from_url(url: str) -> str:
    m = re.search(r"/product/\d+/([^/?#]+)", url)
    words = (m.group(1) if m else url).replace(",", "").replace("-", " ").split()
    return " ".join(words[:8]) or "item"


def parse_item_spec(spec: str, where: str) -> Item:
    """'Name|URL' or just 'URL'."""
    name, sep, url = spec.strip().rpartition("|")
    if not sep:
        name, url = "", spec.strip()
    name, url = name.strip(), url.strip()
    if "microcenter.com/product/" not in url:
        raise ConfigError(f"{where}: not a Micro Center product URL: {url!r}")
    return Item(name=name or name_from_url(url), url=url, auto_name=not name)


def load_config(path: Path | None, env: dict | None = None) -> Config:
    """Settings come from environment variables; an optional YAML file can add more.

    Env: STORE_ID (default 095 = Brentwood, MO), ITEM_1..ITEM_20 ("Name|URL" or "URL"),
    ITEMS (same format, one per line or ';'-separated), INTERVAL_MINUTES,
    SUMMARY_INTERVAL_HOURS, DISCORD_WEBHOOK_URL, DISCORD_MENTION_ID.
    Env values override the file.
    """
    env = os.environ if env is None else env
    raw: dict = {}
    if path and path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
        log.info("Loaded config file %s", path)

    def setting(env_key: str, file_key: str, default):
        val = env.get(env_key, "").strip()
        return val if val else raw.get(file_key, default)

    store_id = str(setting("STORE_ID", "store_id", DEFAULT_STORE_ID)).strip()
    if not store_id.isdigit():
        raise ConfigError(f"STORE_ID must be a number, got {store_id!r}")

    items: list[Item] = []
    for i, entry in enumerate(raw.get("items") or []):
        url = str((entry or {}).get("url", "")).strip()
        name = str((entry or {}).get("name", "")).strip()
        items.append(parse_item_spec(f"{name}|{url}" if name else url, f"config items[{i}]"))
    for n in range(1, MAX_ENV_ITEMS + 1):
        spec = env.get(f"ITEM_{n}", "").strip()
        if spec:
            items.append(parse_item_spec(spec, f"ITEM_{n}"))
    for j, spec in enumerate(re.split(r"[;\n]", env.get("ITEMS", ""))):
        if spec.strip():
            items.append(parse_item_spec(spec, f"ITEMS entry {j + 1}"))
    seen, unique = set(), []
    for it in items:
        if it.url not in seen:
            seen.add(it.url)
            unique.append(it)
    if not unique:
        raise ConfigError("no items to watch: set ITEM_1 (and ITEM_2, ...) to a Micro Center product URL, "
                          "optionally as 'Name|URL'")

    try:
        interval = float(setting("INTERVAL_MINUTES", "interval_minutes", 15))
        summary_hours = float(setting("SUMMARY_INTERVAL_HOURS", "summary_interval_hours", 6))
        jitter = float(setting("JITTER_PCT", "jitter_pct", 0.20))
        backoff = float(setting("BACKOFF_BASE_SECONDS", "backoff_base_seconds", 30))
        retries = int(setting("MAX_RETRIES", "max_retries", 3))
    except ValueError as e:
        raise ConfigError(f"bad number in settings: {e}") from e
    if interval < MIN_INTERVAL_MINUTES:
        log.warning("interval %s min is below the %s minute minimum; using %s",
                    interval, MIN_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES)
        interval = MIN_INTERVAL_MINUTES

    return Config(
        store_id=store_id,
        items=unique,
        interval_minutes=interval,
        jitter_pct=min(max(jitter, 0.0), 0.5),
        summary_interval_hours=max(summary_hours, 0.5),
        backoff_base_seconds=max(backoff, 1),
        max_retries=max(retries, 0),
        webhook_url=env.get("DISCORD_WEBHOOK_URL", "").strip(),
        mention_id=env.get("DISCORD_MENTION_ID", "").strip(),
    )


# --------------------------------------------------------------------------- parsing


class ParseError(Exception):
    pass


class FetchError(Exception):
    def __init__(self, msg: str, html: str | None = None):
        super().__init__(msg)
        self.html = html


@dataclass
class StockInfo:
    status: str  # "in_stock" | "sold_out"
    count: int | None = None  # numeric part, e.g. 25 for "25+"
    count_text: str | None = None  # as shown, e.g. "25+", "5", "LIMITED"
    price: float | None = None
    title: str | None = None
    store_name: str | None = None
    method: str = ""  # which parser path found the stock status


IN_STOCK_RE = re.compile(r"\b(\d+\+?)\s+(?:NEW\s+)?IN\s+STOCK\b")
IN_STOCK_NOCOUNT_RE = re.compile(r"\bIN\s+STOCK\b")
SOLD_OUT_RE = re.compile(r"\bSOLD\s+OUT\b")
LIMITED_RE = re.compile(r"\bLIMITED\s+AVAILABILITY\b", re.I)
STORE_RE = re.compile(r"\bat\s+(?:the\s+)?([A-Z][\w .'&-]{1,40}?)\s+Store\b", re.I)
PRICE_RE = re.compile(r"(\d[\d,]*\.?\d*)")
CHALLENGE_MARKERS = ("_cf_chl_opt", "challenge-platform", "<title>Just a moment...</title>",
                     "cf-browser-verification")

INVENTORY_SELECTORS = (
    "#pnlInventory .inventoryCnt",
    "span.inventoryCnt",
    ".inventoryCnt",
    "[class*=inventoryCnt]",
    "[id*=inventoryCnt]",
    "#pnlInventory",
    ".inventory",
)


def is_challenge_page(html: str) -> bool:
    head = html[:20000]
    return any(m in head for m in CHALLENGE_MARKERS)


def _stock_from_text(text: str, case_insensitive: bool = False) -> tuple[str, int | None, str | None] | None:
    """Return (status, count, count_text) from a snippet of text, or None.

    When both an in-stock and a sold-out phrase are present, the earliest one wins
    (the main product block comes before carousels / other-store lists).
    """
    t = " ".join(text.split())
    if case_insensitive:
        t = t.upper()
    hits = []
    m = IN_STOCK_RE.search(t)
    if m:
        hits.append((m.start(), ("in_stock", int(m.group(1).rstrip("+")), m.group(1))))
    m = SOLD_OUT_RE.search(t)
    if m:
        hits.append((m.start(), ("sold_out", 0, None)))
    if not hits:
        m = LIMITED_RE.search(t)
        if m:
            hits.append((m.start(), ("in_stock", None, "LIMITED")))
        m = IN_STOCK_NOCOUNT_RE.search(t)
        if m:
            hits.append((m.start(), ("in_stock", None, None)))
    if not hits:
        return None
    return min(hits, key=lambda h: h[0])[1]


def _iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                for key in ("@graph", "offers", "itemListElement"):
                    if key in node:
                        stack.append(node[key])


def _to_price(value) -> float | None:
    if value is None:
        return None
    m = PRICE_RE.search(str(value).replace("$", ""))
    if not m:
        return None
    try:
        p = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return round(p, 2) if p > 0 else None


def product_id_from_url(url: str) -> str | None:
    m = re.search(r"/product/(\d+)", url)
    return m.group(1) if m else None


def parse_price(soup: BeautifulSoup, product_id: str | None) -> float | None:
    # 1. microdata itemprop="price" (content attr preferred)
    for tag in soup.select('[itemprop="price"]'):
        p = _to_price(tag.get("content") or tag.get_text())
        if p:
            return p
    # 2. Micro Center's #pricing element
    tag = soup.select_one("#pricing")
    if tag:
        p = _to_price(tag.get("content") or tag.get_text())
        if p:
            return p
    # 3. JSON-LD offers.price
    for node in _iter_jsonld(soup):
        if "price" in node or "lowPrice" in node:
            p = _to_price(node.get("price") or node.get("lowPrice"))
            if p:
                return p
    # 4. data-price attribute, preferring the element tied to this product id
    if product_id:
        for tag in soup.select(f'[data-id="{product_id}"][data-price]'):
            p = _to_price(tag.get("data-price"))
            if p:
                return p
    tag = soup.select_one("[data-price]")
    if tag:
        return _to_price(tag.get("data-price"))
    return None


def parse_title(soup: BeautifulSoup) -> str | None:
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        title = og["content"]
    else:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else None
    if title:
        title = re.sub(r"\s*[-|]\s*Micro Center\s*$", "", " ".join(title.split()), flags=re.I)
    return title or None


def parse_product(html: str, url: str = "") -> StockInfo:
    """Parse a Micro Center product page. Raises ParseError if stock can't be determined."""
    if is_challenge_page(html):
        raise ParseError("got a Cloudflare challenge page instead of the product page")
    soup = BeautifulSoup(html, "html.parser")
    result = None
    method = ""
    store_name = None

    # 1. Inventory element
    for sel in INVENTORY_SELECTORS:
        for tag in soup.select(sel):
            text = tag.get_text(" ", strip=True)
            found = _stock_from_text(text, case_insensitive=True)
            if found:
                result, method = found, f"element:{sel}"
                ctx = tag.parent.get_text(" ", strip=True) if tag.parent else text
                sm = STORE_RE.search(ctx)
                store_name = sm.group(1).strip() if sm else None
                break
        if result:
            break

    # 2. Regex over the visible page text (case-sensitive: Micro Center uppercases status)
    if not result:
        for junk in soup(["script", "style", "noscript"]):
            junk.extract()
        found = _stock_from_text(soup.get_text(" ", strip=True))
        if found:
            result, method = found, "regex:page-text"
        soup = BeautifulSoup(html, "html.parser")  # restore scripts for JSON-LD below

    # 3. Structured data (may reflect online availability, not the store)
    if not result:
        avail = None
        tag = soup.select_one('[itemprop="availability"]')
        if tag:
            avail = tag.get("href") or tag.get("content") or tag.get_text()
        if not avail:
            for node in _iter_jsonld(soup):
                if node.get("availability"):
                    avail = str(node["availability"])
                    break
        if avail:
            a = avail.lower()
            if "instock" in a or "limitedavailability" in a or "instoreonly" in a:
                result, method = ("in_stock", None, None), "structured"
            elif "outofstock" in a or "soldout" in a or "discontinued" in a:
                result, method = ("sold_out", 0, None), "structured"

    if not result:
        raise ParseError("could not find stock status (inventory element, page text, structured data)")

    status, count, count_text = result
    return StockInfo(
        status=status,
        count=count,
        count_text=count_text,
        price=parse_price(soup, product_id_from_url(url)),
        title=parse_title(soup),
        store_name=store_name,
        method=method,
    )


# --------------------------------------------------------------------------- fetching


def with_store_param(url: str, store_id: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "storeid"]
    query.append(("storeid", store_id))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class Fetcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if cffi_requests is not None:
            self.session = cffi_requests.Session(impersonate="chrome")
            self.client = "curl_cffi"
        else:
            self.session = requests.Session()
            self.session.headers.update(BROWSER_HEADERS)
            self.client = "requests"
        log.info("HTTP client: %s", self.client)

    def _get(self, url: str):
        return self.session.get(url, cookies={"storeid": self.cfg.store_id}, timeout=30,
                                allow_redirects=True)

    def fetch(self, url: str) -> str:
        target = with_store_param(url, self.cfg.store_id)
        last_html = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                resp = self._get(target)
            except Exception as e:  # network errors from either client
                reason = f"request error: {e}"
                status = None
            else:
                status = resp.status_code
                text = resp.text
                if status == 200 and not is_challenge_page(text):
                    return text
                last_html = text
                if status in (403, 429, 503) or is_challenge_page(text):
                    reason = f"HTTP {status}" + (" (Cloudflare challenge)" if is_challenge_page(text) else "")
                else:
                    raise FetchError(f"HTTP {status}", html=text)
            if attempt >= self.cfg.max_retries:
                raise FetchError(f"{reason} after {attempt + 1} attempts", html=last_html)
            delay = self.cfg.backoff_base_seconds * (2 ** attempt) * random.uniform(0.8, 1.2)
            log.warning("%s for %s; backing off %.0fs (attempt %d/%d)",
                        reason, target, delay, attempt + 1, self.cfg.max_retries + 1)
            if STOP.wait(delay):
                raise FetchError("shutting down")
        raise FetchError("unreachable")


# --------------------------------------------------------------------------- discord


def fmt_price(p: float | None) -> str:
    return f"${p:,.2f}" if p is not None else "n/a"


def fmt_count(status: str, count_text: str | None) -> str:
    if status != "in_stock":
        return "0"
    return count_text or "unknown"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Discord:
    def __init__(self, webhook_url: str, mention_id: str, dry_run: bool):
        self.webhook_url = webhook_url
        self.mention_id = mention_id
        self.dry_run = dry_run

    def send(self, payload: dict) -> bool:
        payload.setdefault("username", "Micro Center Stock")
        payload.setdefault("allowed_mentions", {"parse": []})
        if self.dry_run:
            print("---- DISCORD PAYLOAD (dry run) ----")
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return True
        if not self.webhook_url:
            log.error("DISCORD_WEBHOOK_URL is not set; dropping message")
            return False
        for attempt in range(4):
            try:
                r = requests.post(self.webhook_url, json=payload, timeout=20)
            except requests.RequestException as e:
                log.warning("Discord post failed: %s", e)
                STOP.wait(2 ** attempt)
                continue
            if r.status_code == 429:
                try:
                    retry = float(r.json().get("retry_after", 2))
                except ValueError:
                    retry = 2
                STOP.wait(min(retry, 60) + 0.5)
                continue
            if r.status_code >= 400:
                log.error("Discord rejected message: HTTP %s %s", r.status_code, r.text[:300])
                return False
            return True
        log.error("Discord post failed after retries")
        return False

    # -- payload builders (pure, testable) --

    def stock_alert(self, item: Item, info: dict) -> dict:
        mention = f"<@{self.mention_id}> " if self.mention_id else ""
        payload = {
            "content": f"{mention}**{item.name}** is IN STOCK!",
            "embeds": [{
                "title": f"🟢 IN STOCK: {item.name}"[:256],
                "url": item.url,
                "color": COLOR_GREEN,
                "fields": [
                    {"name": "Stock", "value": fmt_count("in_stock", info.get("count_text")), "inline": True},
                    {"name": "Price", "value": fmt_price(info.get("price")), "inline": True},
                ] + ([{"name": "Store", "value": info["store_name"], "inline": True}]
                     if info.get("store_name") else []),
                "description": info.get("title") or "",
                "timestamp": now_iso(),
            }],
        }
        if self.mention_id:
            payload["allowed_mentions"] = {"users": [self.mention_id]}
        return payload

    @staticmethod
    def change_notice(item: Item, info: dict, changes: list[str]) -> dict:
        sold_out = info["status"] == "sold_out"
        return {
            "embeds": [{
                "title": (f"🔴 SOLD OUT: {item.name}" if sold_out else f"🔄 Update: {item.name}")[:256],
                "url": item.url,
                "color": COLOR_RED if sold_out else COLOR_GREEN,
                "description": "\n".join(changes)[:4000],
                "fields": [
                    {"name": "Stock", "value": fmt_count(info["status"], info.get("count_text")), "inline": True},
                    {"name": "Price", "value": fmt_price(info.get("price")), "inline": True},
                ],
                "timestamp": now_iso(),
            }],
        }

    @staticmethod
    def error_notice(item: Item, error: str, failures: int, debug_path: str | None) -> dict:
        desc = f"Failed {failures} checks in a row.\n**Last error:** {error}"
        if debug_path:
            desc += f"\nRaw HTML saved to `{debug_path}`"
        return {
            "embeds": [{
                "title": f"⚠️ Check failing: {item.name}"[:256],
                "url": item.url,
                "color": COLOR_YELLOW,
                "description": desc[:4000],
                "timestamp": now_iso(),
            }],
        }

    @staticmethod
    def recovered_notice(item: Item) -> dict:
        return {"embeds": [{"title": f"✅ Check recovered: {item.name}"[:256], "url": item.url,
                            "color": COLOR_GREEN, "timestamp": now_iso()}]}

    @staticmethod
    def summary(cfg: Config, state: dict, reason: str) -> dict:
        fields = []
        any_in, any_ok = False, False
        for item in cfg.items:
            s = state["items"].get(item.url) or {}
            status = s.get("status")
            failing = s.get("failures", 0) > 0
            if status == "in_stock":
                any_in = any_ok = True
                line = f"🟢 **IN STOCK** ({fmt_count(status, s.get('count_text'))}) · {fmt_price(s.get('price'))}"
            elif status == "sold_out":
                any_ok = True
                line = f"🔴 **SOLD OUT** · {fmt_price(s.get('price'))}"
            else:
                line = "⚠️ **UNKNOWN** (no successful check yet)"
            if failing:
                line += f"\n⚠️ last {s['failures']} check(s) failed: {(s.get('last_error') or '')[:150]}"
            line += f"\n[Product page]({item.url})"
            fields.append({"name": item.name[:256], "value": line[:1024], "inline": False})
        color = COLOR_GREEN if any_in else (COLOR_RED if any_ok else COLOR_YELLOW)
        store = next((s.get("store_name") for s in state["items"].values() if s.get("store_name")), None)
        store_label = f"{store} (store {cfg.store_id})" if store else f"store {cfg.store_id}"
        return {
            "embeds": [{
                "title": f"📦 Micro Center stock summary — {store_label}"[:256],
                "color": color,
                "fields": fields[:25],
                "footer": {"text": f"{reason} · checks every ~{cfg.interval_minutes:g} min"},
                "timestamp": now_iso(),
            }],
        }


# --------------------------------------------------------------------------- state


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            data.setdefault("items", {})
            return data
        except (json.JSONDecodeError, OSError) as e:
            log.error("Could not read %s (%s); starting with empty state", path, e)
    return {"version": 1, "items": {}, "last_summary_at": None}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


def evaluate(prev: dict | None, info: StockInfo) -> tuple[str | None, list[str], dict]:
    """Compare a successful check with saved state.

    Returns (event, changes, new_state) where event is "alert", "change" or None.
    """
    new = dict(prev or {})
    price = info.price if info.price is not None else new.get("price")
    new.update({
        "status": info.status,
        "count": info.count,
        "count_text": info.count_text,
        "price": price,
        "title": info.title or new.get("title"),
        "store_name": info.store_name or new.get("store_name"),
        "last_ok_at": now_iso(),
        "failures": 0,
        "error_notified": False,
        "last_error": None,
    })
    if not prev or prev.get("status") not in ("in_stock", "sold_out"):
        return None, [], new  # first observation: record only

    changes = []
    old_status = prev["status"]
    if old_status == "sold_out" and info.status == "in_stock":
        return "alert", ["Back in stock"], new
    if old_status == "in_stock" and info.status == "sold_out":
        changes.append("Now **SOLD OUT**")
    elif info.status == "in_stock" and prev.get("count_text") != info.count_text:
        changes.append(f"Stock: {fmt_count('in_stock', prev.get('count_text'))} → "
                       f"{fmt_count('in_stock', info.count_text)}")
    old_price = prev.get("price")
    if info.price is not None and old_price is not None and round(old_price, 2) != round(info.price, 2):
        changes.append(f"Price: {fmt_price(old_price)} → {fmt_price(info.price)}")
    return ("change" if changes else None), changes, new


# --------------------------------------------------------------------------- main loop


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "item"


def save_debug_html(data_dir: Path, item: Item, html: str | None) -> str | None:
    if not html:
        return None
    debug_dir = data_dir / "debug"
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = debug_dir / f"{slug(item.name)}-{ts}.html"
        path.write_text(html)
        files = sorted(debug_dir.glob("*.html"), key=lambda p: p.stat().st_mtime)
        for old in files[:-MAX_DEBUG_FILES]:
            old.unlink(missing_ok=True)
        return str(path)
    except OSError as e:
        log.error("Could not save debug HTML: %s", e)
        return None


def check_all(cfg: Config, fetcher: Fetcher, discord: Discord, state: dict, data_dir: Path,
              state_path: Path, dry_run: bool) -> None:
    for i, item in enumerate(cfg.items):
        if STOP.is_set():
            return
        if i > 0 and STOP.wait(random.uniform(2, 5)):
            return
        prev = state["items"].get(item.url)
        html = None
        try:
            html = fetcher.fetch(item.url)
            info = parse_product(html, item.url)
        except (FetchError, ParseError) as e:
            if isinstance(e, FetchError):
                html = e.html
            if STOP.is_set():
                return
            entry = dict(prev or {})
            entry["failures"] = entry.get("failures", 0) + 1
            entry["last_error"] = str(e)
            debug_path = save_debug_html(data_dir, item, html)
            log.error("%s | ERROR | %s | failures=%d%s", item.name, e, entry["failures"],
                      f" | html={debug_path}" if debug_path else "")
            if entry["failures"] >= FAILURE_ALERT_THRESHOLD and not entry.get("error_notified"):
                if discord.send(discord.error_notice(item, str(e), entry["failures"], debug_path)):
                    entry["error_notified"] = True
            state["items"][item.url] = entry
        else:
            log.info("%s | %s | count=%s | price=%s | store=%s | via %s",
                     item.name, "IN STOCK" if info.status == "in_stock" else "SOLD OUT",
                     fmt_count(info.status, info.count_text), fmt_price(info.price),
                     info.store_name or "?", info.method)
            if info.method == "structured":
                log.warning("%s: stock came from structured data only; it may not be store-specific",
                            item.name)
            if item.auto_name and info.title:
                item.name = info.title[:80]
            was_notified = bool(prev and prev.get("error_notified"))
            event, changes, entry = evaluate(prev, info)
            entry["name"] = item.name
            if was_notified:
                discord.send(discord.recovered_notice(item))
            if event == "alert":
                discord.send(discord.stock_alert(item, entry))
            elif event == "change":
                discord.send(discord.change_notice(item, entry, changes))
            state["items"][item.url] = entry
        if not dry_run:
            save_state(state_path, state)


def send_summary(cfg: Config, discord: Discord, state: dict, state_path: Path, reason: str,
                 dry_run: bool) -> None:
    discord.send(discord.summary(cfg, state, reason))
    # Advance the timer even if the post failed, so a Discord outage doesn't cause a retry every cycle.
    state["last_summary_at"] = time.time()
    if not dry_run:
        save_state(state_path, state)


def next_sleep_seconds(cfg: Config) -> float:
    base = cfg.interval_minutes * 60
    return max(base * random.uniform(1 - cfg.jitter_pct, 1 + cfg.jitter_pct), MIN_INTERVAL_MINUTES * 60)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="run one check (plus summary) and exit")
    ap.add_argument("--dry-run", action="store_true", help="print Discord payloads instead of sending; don't save state")
    ap.add_argument("--test-discord", action="store_true", help="send one test message and exit")
    ap.add_argument("--config", default=os.environ.get("CONFIG_PATH", "/data/config.yaml"),
                    help="optional YAML config; environment variables work without it")
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "/data"))
    args = ap.parse_args(argv)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())

    one_shot = args.once or args.dry_run or args.test_discord

    def fail(msg: str) -> int:
        """One-shot runs exit; the service waits instead of crash-looping under restart: unless-stopped."""
        log.error(msg)
        if one_shot:
            return 2
        log.error("Waiting. Fix the stack's environment variables and redeploy.")
        while not STOP.wait(3600):
            log.error(msg)
        return 0

    try:
        cfg = load_config(Path(args.config))
    except ConfigError as e:
        return fail(f"Config error: {e}")
    data_dir = Path(args.data_dir)
    state_path = data_dir / "state.json"
    discord = Discord(cfg.webhook_url, cfg.mention_id, args.dry_run)

    if not args.dry_run and not cfg.webhook_url:
        return fail("DISCORD_WEBHOOK_URL is not set. Use --dry-run to test without it.")

    if args.test_discord:
        ok = discord.send({"embeds": [{
            "title": "✅ Micro Center stock checker: test message",
            "description": f"Webhook works. Watching {len(cfg.items)} item(s) at store {cfg.store_id}.",
            "color": COLOR_GREEN, "timestamp": now_iso()}]})
        return 0 if ok else 1

    if not args.dry_run:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / ".write-test").write_text("ok")
            (data_dir / ".write-test").unlink()
        except OSError as e:
            return fail(f"Data dir {data_dir} is not writable ({e}). Use a named volume, "
                        "or chown a bind-mounted folder to 1000:1000.")

    state = load_state(state_path)
    for item in cfg.items:  # show the real product title right away for unnamed items
        saved = state["items"].get(item.url) or {}
        if item.auto_name and saved.get("title"):
            item.name = saved["title"][:80]
    fetcher = Fetcher(cfg)
    for item in cfg.items:
        log.info("Watching: %s — %s", item.name, item.url)
    log.info("Store %s · %d item(s) · every %g min ±%d%% · summary every %g h%s",
             cfg.store_id, len(cfg.items), cfg.interval_minutes, cfg.jitter_pct * 100,
             cfg.summary_interval_hours, " · DRY RUN" if args.dry_run else "")

    check_all(cfg, fetcher, discord, state, data_dir, state_path, args.dry_run)
    if STOP.is_set():
        return 0
    send_summary(cfg, discord, state, state_path, "Startup" if not args.once else "One-off check",
                 args.dry_run)
    if args.once:
        return 0

    while not STOP.is_set():
        delay = next_sleep_seconds(cfg)
        log.info("Next check in %.1f min", delay / 60)
        if STOP.wait(delay):
            break
        check_all(cfg, fetcher, discord, state, data_dir, state_path, args.dry_run)
        last = state.get("last_summary_at") or 0
        if not STOP.is_set() and time.time() - last >= cfg.summary_interval_hours * 3600:
            send_summary(cfg, discord, state, state_path, "Scheduled", args.dry_run)
    log.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
