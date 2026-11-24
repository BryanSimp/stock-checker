"""Offline tests for the parser, change detection and payloads.

Fixtures are hand-written from the markup patterns Micro Center has used
(inventoryCnt span, itemprop price, JSON-LD). They are NOT captured pages;
verify against a live page with:  docker compose run --rm checker --once --dry-run
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checker as c  # noqa: E402

URL = "https://www.microcenter.com/product/687907/amd-ryzen-7-9800x3d"


def page(inventory_html: str, extra: str = "", price: str = '<span id="pricing" itemprop="price" content="479.99">$479.99</span>') -> str:
    return f"""<html><head>
<meta property="og:title" content="AMD Ryzen 7 9800X3D Granite Ridge AM5 - Micro Center">
<title>AMD Ryzen 7 9800X3D - Micro Center</title></head><body>
<h1>AMD Ryzen 7 9800X3D</h1>
<p class="big-price">{price}</p>
<div id="pnlInventory">{inventory_html}</div>
{extra}
</body></html>"""


class ParserTests(unittest.TestCase):
    def test_inventory_count(self):
        info = c.parse_product(page('<span class="inventoryCnt">5 NEW IN STOCK</span> <span class="msgInStock">at Tustin Store</span>'), URL)
        self.assertEqual((info.status, info.count, info.count_text), ("in_stock", 5, "5"))
        self.assertEqual(info.price, 479.99)
        self.assertEqual(info.title, "AMD Ryzen 7 9800X3D Granite Ridge AM5")
        self.assertEqual(info.store_name, "Tustin")
        self.assertTrue(info.method.startswith("element:"))

    def test_inventory_plus(self):
        info = c.parse_product(page('<span class="inventoryCnt">25+ NEW IN STOCK</span>'), URL)
        self.assertEqual((info.status, info.count, info.count_text), ("in_stock", 25, "25+"))

    def test_in_stock_mixed_case(self):
        info = c.parse_product(page('<span class="inventoryCnt">3 In Stock</span>'), URL)
        self.assertEqual((info.status, info.count_text), ("in_stock", "3"))

    def test_sold_out(self):
        info = c.parse_product(page('<span class="inventoryCnt">SOLD OUT</span> at Tustin Store'), URL)
        self.assertEqual((info.status, info.count), ("sold_out", 0))

    def test_regex_fallback_first_match_wins(self):
        html = page("", extra='<div class="x">SOLD OUT</div><div class="carousel">10 NEW IN STOCK</div>')
        info = c.parse_product(html, URL)
        self.assertEqual(info.status, "sold_out")
        self.assertEqual(info.method, "regex:page-text")

    def test_regex_ignores_scripts(self):
        html = page("", extra='<script>var s="99 NEW IN STOCK";</script><div>7 NEW IN STOCK</div>')
        self.assertEqual(c.parse_product(html, URL).count, 7)

    def test_jsonld_fallback(self):
        html = """<html><head><script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Product","name":"GPU",
         "offers":{"@type":"Offer","price":"599.99","availability":"https://schema.org/OutOfStock"}}
        </script></head><body><h1>GPU</h1></body></html>"""
        info = c.parse_product(html, URL)
        self.assertEqual((info.status, info.price, info.method), ("sold_out", 599.99, "structured"))

    def test_data_price_prefers_product_id(self):
        html = page('<span class="inventoryCnt">2 NEW IN STOCK</span>', price="",
                    extra='<a data-id="111" data-price="9.99"></a><a data-id="687907" data-price="1,299.00"></a>')
        self.assertEqual(c.parse_product(html, URL).price, 1299.0)

    def test_unparseable(self):
        with self.assertRaises(c.ParseError):
            c.parse_product("<html><body><h1>Something</h1></body></html>", URL)

    def test_challenge_page(self):
        html = "<html><head><title>Just a moment...</title></head><body><script>window._cf_chl_opt={}</script></body></html>"
        self.assertTrue(c.is_challenge_page(html))
        with self.assertRaises(c.ParseError):
            c.parse_product(html, URL)


class UrlTests(unittest.TestCase):
    def test_store_param(self):
        self.assertEqual(c.with_store_param("https://www.microcenter.com/product/1/x", "131"),
                         "https://www.microcenter.com/product/1/x?storeid=131")
        self.assertEqual(c.with_store_param("https://www.microcenter.com/product/1/x?a=1&storeid=999", "131"),
                         "https://www.microcenter.com/product/1/x?a=1&storeid=131")


def info(status, count_text=None, price=499.99):
    count = int(count_text.rstrip("+")) if count_text and count_text[0].isdigit() else (0 if status == "sold_out" else None)
    return c.StockInfo(status=status, count=count, count_text=count_text, price=price)


class EvaluateTests(unittest.TestCase):
    def test_first_observation_no_event(self):
        ev, _, st = c.evaluate(None, info("in_stock", "5"))
        self.assertIsNone(ev)
        self.assertEqual(st["status"], "in_stock")

    def test_back_in_stock_alert(self):
        _, _, prev = c.evaluate(None, info("sold_out"))
        ev, _, _ = c.evaluate(prev, info("in_stock", "3"))
        self.assertEqual(ev, "alert")

    def test_sells_out_change(self):
        _, _, prev = c.evaluate(None, info("in_stock", "3"))
        ev, changes, _ = c.evaluate(prev, info("sold_out"))
        self.assertEqual(ev, "change")
        self.assertIn("SOLD OUT", changes[0])

    def test_count_and_price_change(self):
        _, _, prev = c.evaluate(None, info("in_stock", "3", 499.99))
        ev, changes, _ = c.evaluate(prev, info("in_stock", "5", 479.99))
        self.assertEqual(ev, "change")
        self.assertEqual(len(changes), 2)

    def test_no_change(self):
        _, _, prev = c.evaluate(None, info("in_stock", "25+"))
        ev, _, _ = c.evaluate(prev, info("in_stock", "25+"))
        self.assertIsNone(ev)

    def test_missing_price_keeps_old_and_no_notice(self):
        _, _, prev = c.evaluate(None, info("in_stock", "3", 499.99))
        ev, _, st = c.evaluate(prev, info("in_stock", "3", None))
        self.assertIsNone(ev)
        self.assertEqual(st["price"], 499.99)

    def test_state_roundtrip_prevents_realert(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            _, _, prev = c.evaluate(None, info("sold_out"))
            ev, _, now = c.evaluate(prev, info("in_stock", "2"))
            self.assertEqual(ev, "alert")
            c.save_state(p, {"items": {URL: now}})
            loaded = c.load_state(p)["items"][URL]
            ev2, _, _ = c.evaluate(loaded, info("in_stock", "2"))
            self.assertIsNone(ev2)


class PayloadTests(unittest.TestCase):
    def test_alert_mentions(self):
        d = c.Discord("", "1234", dry_run=True)
        p = d.stock_alert(c.Item("GPU", URL), {"count_text": "5", "price": 1.0})
        self.assertIn("<@1234>", p["content"])
        self.assertEqual(p["allowed_mentions"], {"users": ["1234"]})
        self.assertEqual(p["embeds"][0]["color"], c.COLOR_GREEN)

    def test_change_no_mention(self):
        p = c.Discord.change_notice(c.Item("GPU", URL), {"status": "sold_out"}, ["Now **SOLD OUT**"])
        self.assertNotIn("content", p)
        self.assertEqual(p["embeds"][0]["color"], c.COLOR_RED)

    def test_summary(self):
        cfg = c.Config(store_id="131", items=[c.Item("A", URL), c.Item("B", URL + "b")])
        state = {"items": {URL: {"status": "sold_out", "price": 10.0}}}
        e = c.Discord.summary(cfg, state, "Startup")["embeds"][0]
        self.assertEqual(len(e["fields"]), 2)
        self.assertEqual(e["color"], c.COLOR_RED)
        self.assertIn("UNKNOWN", e["fields"][1]["value"])


class FlowTests(unittest.TestCase):
    """Drive check_all() through a page sequence with a fake fetcher and a recording Discord."""

    def test_sequence(self):
        import tempfile
        pages = {
            "sold": page('<span class="inventoryCnt">SOLD OUT</span>'),
            "in3": page('<span class="inventoryCnt">3 NEW IN STOCK</span>'),
            "in4": page('<span class="inventoryCnt">4 NEW IN STOCK</span>'),
            "bad": "<html><body>maintenance</body></html>",
        }

        class FakeFetcher:
            seq = []
            def fetch(self, url):
                return pages[self.seq.pop(0)]

        class Rec(c.Discord):
            def __init__(self):
                super().__init__("", "42", dry_run=True)
                self.sent = []
            def send(self, payload):
                self.sent.append(payload["embeds"][0]["title"])
                return True

        cfg = c.Config(store_id="131", items=[c.Item("CPU", URL)])
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            sp = d / "state.json"
            f = FakeFetcher()
            f.seq = ["sold", "bad", "bad", "bad", "bad", "in3", "in3", "in4", "sold"]
            r = Rec()
            for _ in range(9):
                # reload state from disk each time, as a restart would
                c.check_all(cfg, f, r, c.load_state(sp), d, sp, dry_run=False)
            titles = r.sent
            self.assertEqual(sum("Check failing" in t for t in titles), 1)  # error posted once
            self.assertEqual(sum("IN STOCK: CPU" in t for t in titles), 1)  # one alert, no repeat
            self.assertEqual(sum("Update: CPU" in t for t in titles), 1)    # 3 -> 4
            self.assertTrue(titles[-1].startswith("🔴 SOLD OUT"))
            self.assertTrue(any("recovered" in t for t in titles))
            self.assertEqual(len(list((d / "debug").glob("*.html"))), 4)


class ConfigTests(unittest.TestCase):
    def test_min_interval(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write('store_id: "131"\ninterval_minutes: 1\nitems:\n  - {name: A, url: "https://www.microcenter.com/product/1/a"}\n')
        cfg = c.load_config(Path(f.name))
        self.assertEqual(cfg.interval_minutes, 5)
        for _ in range(200):
            self.assertGreaterEqual(c.next_sleep_seconds(cfg), 300)


if __name__ == "__main__":
    unittest.main()
