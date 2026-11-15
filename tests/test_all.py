"""Tests de bout en bout avec un marché et un Telegram simulés.

Lancer : python3 -m unittest discover -s tests -v
"""
import base64
import copy
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

import logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from microinvest import broker as broker_mod  # noqa: E402
from microinvest import ed25519_pure  # noqa: E402
from microinvest.config import DEFAULTS, validate, _merge  # noqa: E402
from microinvest.engine import Engine  # noqa: E402
from microinvest.telegram_bot import TelegramBot  # noqa: E402

BASE_PRICES = {"BTC": 60000.0, "ETH": 3000.0, "SOL": 150.0, "XRP": 0.5, "ADA": 0.4, "LINK": 15.0}


class FakeMarket:
    quote = "EUR"

    def __init__(self):
        self.prices = dict(BASE_PRICES)
        self.avg = dict(BASE_PRICES)
        self.history = {}   # séries longues pour le radar
        self.volumes = {}
        self.extra_pairs = []

    def symbol(self, a):
        return a + "-EUR"

    def pairs(self):
        return {self.symbol(a): {"base": a, "quote": "EUR", "base_step": "0.00000001", "quote_step": "0.01",
                                 "min_order_size": "0.00000001", "min_order_size_quote": "1"}
                for a in list(BASE_PRICES) + self.extra_pairs}

    def pair_info(self, a):
        return self.pairs()[self.symbol(a)]

    def tickers(self, assets):
        return {a: {"bid": self.prices[a], "ask": self.prices[a], "last": self.prices[a]} for a in assets}

    def daily_closes(self, a, days=30):
        if days > 60 and a in self.history:
            return self.history[a]
        return [self.avg[a]] * days

    def all_tickers(self):
        out = {a: {"bid": p, "ask": p, "last": p, "volume": 1e9 / p} for a, p in self.prices.items()}
        for a, h in self.history.items():
            out.setdefault(a, {"bid": h[-1], "ask": h[-1], "last": h[-1], "volume": self.volumes.get(a, 1e9 / h[-1])})
        return out


def series(kind, n=365, start=100.0):
    """Séries de prix synthétiques pour tester le radar."""
    out, p = [], start
    for i in range(n):
        if kind == "up_pullback":
            p *= 1.004 if i < n - 12 else 0.991
        elif kind == "down":
            p *= 0.995
        elif kind == "hot":
            p *= 1.002 if i < n - 20 else 1.012
        else:
            p *= 1.0 + (0.001 if i % 2 else -0.001)
        out.append(p)
    return out


class FakeStocks:
    pause = 0

    def __init__(self, data=None, fail=False):
        self.data = data or {}
        self.fail = fail

    def closes(self, ticker):
        from microinvest.broker import BrokerError
        if self.fail or ticker not in self.data:
            raise BrokerError("indisponible")
        return self.data[ticker], "EUR"


class FakeNotifier:
    def __init__(self):
        self.msgs = []
        self.edits = []

    def notify(self, text, buttons=None):
        self.msgs.append((text, buttons))
        return len(self.msgs)

    def edit(self, mid, text):
        self.edits.append((mid, text))

    def last(self):
        return self.msgs[-1][0] if self.msgs else ""


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def monday_10h05():
    # lundi 2 novembre 2026 10:05 heure locale
    return time.mktime((2026, 11, 2, 10, 5, 0, 0, 0, -1))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.dir, "config.json")
        cfg = validate(_merge(DEFAULTS, {"paper_start_eur": 100}))
        with open(self.cfg_path, "w") as f:
            json.dump({"mode": "paper"}, f)
        self.market = FakeMarket()
        self.notifier = FakeNotifier()
        self.clock = Clock(monday_10h05())
        self.e = Engine(cfg, self.cfg_path, self.dir, market=self.market, notifier=self.notifier, clock=self.clock)
        from microinvest.radar import Radar
        self.stocks = FakeStocks()
        self.e.radar = Radar(self.e, stocks=self.stocks, clock=self.clock)
        self.e.cfg["radar"]["enabled"] = False  # activé seulement dans TestRadar

    def tearDown(self):
        r = getattr(self.e, "radar", None)
        if r is not None:  # attend la fin d'une analyse en arrière-plan avant d'effacer le dossier
            r.running.acquire(timeout=10)
        shutil.rmtree(self.dir, ignore_errors=True)

    def eur_balance(self):
        return self.e.broker.balances()["EUR"]


class TestStrategy(Base):
    def test_weekly_dca_buys_base_amount_of_most_underweight(self):
        self.e.run_dca()
        t = self.e.store.trades()
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["asset"], "BTC")
        self.assertAlmostEqual(t[0]["eur"], round(35 * 12 / 52, 2), places=2)
        self.assertAlmostEqual(self.eur_balance(), 100 - t[0]["eur"], places=6)
        self.assertIn("Acheté", self.notifier.last())

    def test_rotation_follows_targets(self):
        for _ in range(4):
            self.e.run_dca()
        assets = [t["asset"] for t in reversed(self.e.store.trades())]
        self.assertEqual(assets[0], "BTC")
        self.assertIn("ETH", assets)

    def test_monthly_budget_is_hard_cap(self):
        for _ in range(10):
            self.e.run_dca(manual=True)
        spent = self.e.budget_status()["spent"]
        self.assertLessEqual(spent, 35 + 1e-9)
        self.assertGreater(spent, 34)
        n = len(self.e.store.trades())
        self.e.buy("BTC", 5, "test", automatic=False)
        self.assertEqual(len(self.e.store.trades()), n)
        self.assertIn("budget du mois épuisé", self.notifier.last())

    def test_never_spend_more_than_balance(self):
        self.e.store.set("paper_balances", {"EUR": 3.0})
        self.e.run_dca()
        self.assertEqual(self.e.store.trades(), [])
        self.assertIn("solde insuffisant", self.notifier.last())
        self.assertEqual(self.eur_balance(), 3.0)

    def test_reserve_is_respected(self):
        self.e.cfg["budget"]["keep_reserve_eur"] = 95
        self.e.run_dca()
        self.assertEqual(self.e.store.trades(), [])

    def test_max_order_cap(self):
        self.assertIsNotNone(self.e.check_buy("BTC", 25, automatic=False))

    def test_paused_blocks_automatic_only(self):
        self.e.store.set("paused", True)
        self.assertIn("pause", self.e.run_dca())
        self.assertEqual(self.e.store.trades(), [])
        self.e.run_dca(manual=True)
        self.assertEqual(len(self.e.store.trades()), 1)

    def test_dip_buy_requires_approval_then_executes(self):
        self.market.prices["SOL"] = 120.0  # -20 % vs moyenne 30 j
        msg = self.e.run_dca()
        self.assertIn("validation", msg)
        self.assertEqual(self.e.store.trades(), [])
        pend = self.e.store.pending_approvals()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["payload"]["asset"], "SOL")
        self.assertAlmostEqual(pend[0]["payload"]["amount"], round(35 * 12 / 52 * 2, 2), places=2)
        self.assertIsNotNone(self.notifier.msgs[-1][1])  # boutons présents
        r = self.e.resolve_approval(pend[0]["id"], True)
        self.assertIn("exécuté", r)
        self.assertEqual(self.e.store.trades()[0]["asset"], "SOL")
        self.assertIn("Déjà traitée", self.e.resolve_approval(pend[0]["id"], True))
        self.assertEqual(len(self.e.store.trades()), 1)

    def test_reject_does_nothing(self):
        self.market.prices["SOL"] = 120.0
        self.e.run_dca()
        aid = self.e.store.pending_approvals()[0]["id"]
        self.e.resolve_approval(aid, False)
        self.assertEqual(self.e.store.trades(), [])

    def test_approval_expires(self):
        self.market.prices["SOL"] = 120.0
        self.e.run_dca()
        aid = self.e.store.pending_approvals()[0]["id"]
        self.clock.t += 13 * 3600
        self.e.expire_approvals()
        self.assertEqual(self.e.store.approval(aid)["status"], "expired")
        self.assertIn("Déjà traitée", self.e.resolve_approval(aid, True))
        self.assertEqual(self.e.store.trades(), [])


class TestSafety(Base):
    def _invest(self, n=3):
        for _ in range(n):
            self.e.run_dca(manual=True)

    def test_drawdown_freezes_all_buys(self):
        self._invest(3)
        for a in self.market.prices:
            self.market.prices[a] *= 0.7
            self.market.avg[a] *= 0.7  # pas de signal "baisse"
        self.e._prices_ts = 0
        self.e.check_drawdown()
        self.assertTrue(self.e.flag("frozen"))
        self.assertIn("GELÉS", self.notifier.last())
        n = len(self.e.store.trades())
        self.e.run_dca(manual=True)
        self.assertEqual(len(self.e.store.trades()), n)
        self.e.resume_freeze()
        self.e.run_dca(manual=True)
        self.assertEqual(len(self.e.store.trades()), n + 1)

    def test_take_profit_never_sells_at_loss(self):
        self.e.cfg["take_profit"]["min_position_eur"] = 1
        self.e.run_dca(manual=True)  # BTC
        self.market.prices["BTC"] *= 1.5
        self.e._prices_ts = 0
        self.e.check_take_profit()
        pend = [a for a in self.e.store.pending_approvals() if a["kind"] == "sell"]
        self.assertEqual(len(pend), 1)
        self.market.prices["BTC"] = BASE_PRICES["BTC"] * 0.9  # krach avant le clic
        self.e._prices_ts = 0
        self.e.resolve_approval(pend[0]["id"], True)
        self.assertEqual([t for t in self.e.store.trades() if t["side"] == "sell"], [])
        self.assertIn("jamais à perte", self.notifier.last())

    def test_take_profit_sells_fraction(self):
        self.e.cfg["take_profit"]["min_position_eur"] = 1
        self.e.run_dca(manual=True)
        qty = self.e.store.positions()["BTC"]["qty"]
        self.market.prices["BTC"] *= 1.5
        self.e._prices_ts = 0
        self.e.check_take_profit()
        aid = self.e.store.pending_approvals()[0]["id"]
        self.e.resolve_approval(aid, True)
        sells = [t for t in self.e.store.trades() if t["side"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertAlmostEqual(sells[0]["qty"], qty * 0.25, places=7)
        self.assertGreater(self.e.store.positions()["BTC"]["realized"], 0)
        # pas de nouvelle proposition avant 7 jours
        self.e.check_take_profit()
        self.assertEqual(len(self.e.store.pending_approvals()), 0)

    def test_sell_never_exceeds_bot_position(self):
        self.e.store.set("paper_balances", {"EUR": 100, "BTC": 1.0})  # BTC perso de l'utilisateur
        self.e.run_dca(manual=True)
        bot_qty = self.e.store.positions()["BTC"]["qty"]
        self.market.prices["BTC"] *= 2
        self.e._prices_ts = 0
        self.e.sell("BTC", 100, "test")
        self.assertAlmostEqual(self.e.broker.balances()["BTC"], 1.0, places=7)
        self.assertAlmostEqual(self.e.store.positions()["BTC"]["qty"], 0, places=9)
        self.assertGreater(bot_qty, 0)

    def test_opportunity_question(self):
        self.market.prices["ETH"] = 2400.0  # -20 %
        self.e.check_opportunity()
        pend = self.e.store.pending_approvals()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["payload"]["asset"], "ETH")
        self.e.check_opportunity()
        self.assertEqual(len(self.e.store.pending_approvals()), 1)


class TestSchedule(Base):
    def test_tick_runs_dca_once_per_week(self):
        self.e.tick()
        self.assertEqual(len(self.e.store.trades()), 1)
        self.clock.t += 3600
        self.e.tick()
        self.assertEqual(len(self.e.store.trades()), 1)
        self.clock.t += 7 * 86400
        self.e.tick()
        self.assertEqual(len(self.e.store.trades()), 2)

    def test_dca_retries_when_exchange_down(self):
        orig = self.market.tickers
        def down(assets):
            raise __import__("microinvest.broker", fromlist=["x"]).BrokerError("panne")
        self.market.tickers = down
        self.e.tick()
        self.assertEqual(self.e.store.trades(), [])
        self.assertIn("retardé", self.notifier.last())
        self.market.tickers = orig
        self.clock.t += 600
        self.e.tick()
        self.assertEqual(self.e.store.trades(), [])  # attend les 15 min
        self.clock.t += 400
        self.e.tick()
        self.assertEqual(len(self.e.store.trades()), 1)

    def test_no_dca_before_hour(self):
        self.clock.t = time.mktime((2026, 11, 2, 9, 0, 0, 0, 0, -1))
        self.e.tick()
        self.assertEqual(self.e.store.trades(), [])

    def test_etf_monthly_question_and_record(self):
        self.clock.t = time.mktime((2026, 11, 5, 18, 10, 0, 0, 0, -1))
        self.e.tick()
        pend = [a for a in self.e.store.pending_approvals() if a["kind"] == "etf"]
        self.assertEqual(len(pend), 1)
        self.assertIn("iShares Core MSCI World", pend[0]["text"])
        self.e.tick()
        self.assertEqual(len([a for a in self.e.store.pending_approvals() if a["kind"] == "etf"]), 1)
        self.e.resolve_approval(pend[0]["id"], True)
        etf = self.e.store.trades(kind="etf")
        self.assertEqual(len(etf), 1)
        self.assertEqual(etf[0]["eur"], 15)
        self.assertIn("/etfvaleur", self.notifier.last())
        self.assertIn("ETF", self.e.set_etf_value(16.2))

    def test_status_and_state_render(self):
        self.e.run_dca()
        self.assertIn("SIMULATION", self.e.status_text())
        self.assertIn("BTC", self.e.portfolio_text())
        st = self.e.state()
        json.dumps(st, default=str)
        self.assertEqual(len(st["trades"]), 1)

    def test_live_mode_refused_without_key(self):
        from microinvest.broker import BrokerError
        with self.assertRaises(BrokerError):
            self.e.set_mode("live")
        self.assertEqual(self.e.mode, "paper")
        self.e.run_dca()
        self.assertEqual(len(self.e.store.trades()), 1)

    def test_paper_and_live_db_are_separate(self):
        self.e.run_dca()
        self.assertTrue(os.path.exists(os.path.join(self.dir, "data", "paper.db")))


class TestTelegram(Base):
    def setUp(self):
        super().setUp()
        self.sent = []

        def api(token, method, params=None, timeout=20):
            self.sent.append((method, params))
            return {"message_id": len(self.sent)}

        self.bot = TelegramBot("T", "42", self.e, api=api)
        self.e.notifier = self.bot

    def msg(self, text, chat=42):
        self.bot.handle({"update_id": 1, "message": {"chat": {"id": chat}, "text": text}})

    def texts(self):
        return [p.get("text", "") for m, p in self.sent if m == "sendMessage"]

    def test_ignores_strangers(self):
        self.msg("/pause", chat=999)
        self.assertFalse(self.e.flag("paused"))
        self.assertEqual(self.sent, [])

    def test_commands(self):
        for c in ("/aide", "/statut", "/portefeuille", "/historique", "/budget", "/questions", "/mode"):
            self.msg(c)
        self.assertEqual(len(self.texts()), 7)
        self.msg("/pause")
        self.assertTrue(self.e.flag("paused"))
        self.msg("/reprendre")
        self.assertFalse(self.e.flag("paused"))
        self.msg("/etf 12,5")
        self.assertEqual(self.e.store.trades(kind="etf")[0]["eur"], 12.5)
        self.msg("/etfvaleur 13")
        self.assertIn("ETF", self.texts()[-1])

    def test_buy_button_flow(self):
        self.msg("/acheter")
        last = self.sent[-1][1]
        self.assertIn("reply_markup", last)
        self.bot.handle({"update_id": 2, "callback_query": {"id": "c", "data": "cmd:dca_now", "message": {"chat": {"id": 42}}}})
        self.assertEqual(len(self.e.store.trades()), 1)

    def test_approval_button(self):
        self.market.prices["SOL"] = 120.0
        self.e.run_dca()
        aid = self.e.store.pending_approvals()[0]["id"]
        self.bot.handle({"update_id": 3, "callback_query": {"id": "c", "data": "ap:%d:y" % aid, "message": {"chat": {"id": 42}}}})
        self.assertEqual(self.e.store.trades()[0]["asset"], "SOL")
        self.assertTrue(any(m == "editMessageText" for m, _ in self.sent))


class TestWeb(Base):
    def test_auth_csrf_and_actions(self):
        from microinvest import web
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        srv = web.start(self.e, "127.0.0.1", port, "secret")
        try:
            url = "http://127.0.0.1:%d" % port
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(url + "/api/state")
            self.assertEqual(cm.exception.code, 401)
            auth = "Basic " + base64.b64encode(b"admin:secret").decode()
            r = urllib.request.urlopen(urllib.request.Request(url + "/api/state", headers={"Authorization": auth}))
            st = json.loads(r.read())
            self.assertEqual(st["mode"], "paper")
            r = urllib.request.urlopen(urllib.request.Request(url + "/", headers={"Authorization": auth}))
            self.assertIn(b"Micro-invest", r.read())
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(url + "/api/pause", data=b"{}", headers={"Authorization": auth}))
            self.assertEqual(cm.exception.code, 403)
            h = {"Authorization": auth, "X-Requested-With": "microinvest", "Content-Type": "application/json"}
            urllib.request.urlopen(urllib.request.Request(url + "/api/pause", data=b"{}", headers=h))
            self.assertTrue(self.e.flag("paused"))
            urllib.request.urlopen(urllib.request.Request(url + "/api/dca_now", data=b"{}", headers=h))
            self.assertEqual(len(self.e.store.trades()), 1)
        finally:
            srv.shutdown()


class TestWebLockout(Base):
    def test_blocks_after_5_wrong_passwords(self):
        from microinvest import web
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        srv = web.start(self.e, "127.0.0.1", port, "secret")
        url = "http://127.0.0.1:%d/api/state" % port

        def get(pw, ip="192.168.1.77"):
            h = {"Authorization": "Basic " + base64.b64encode(("admin:" + pw).encode()).decode(), "X-Real-IP": ip}
            try:
                return urllib.request.urlopen(urllib.request.Request(url, headers=h)).status
            except urllib.error.HTTPError as e:
                return e.code
        try:
            self.assertEqual(get("secret"), 200)
            for _ in range(5):
                self.assertEqual(get("faux"), 401)
            self.assertEqual(get("secret"), 429)  # même le bon mot de passe est bloqué 15 min
            self.assertIn("bloquée", self.notifier.last())
            self.assertEqual(get("secret", ip="192.168.1.9"), 200)  # une autre adresse passe
            for i in range(15):
                get("faux", ip="10.0.0.%d" % i)
            self.assertEqual(get("secret", ip="192.168.1.9"), 429)  # plafond global atteint
        finally:
            srv.shutdown()

    def test_refuses_public_ips(self):
        from microinvest import web
        self.assertTrue(web.is_local("192.168.1.20"))
        self.assertTrue(web.is_local("10.1.2.3"))
        self.assertTrue(web.is_local("127.0.0.1"))
        self.assertTrue(web.is_local("::ffff:192.168.1.5"))
        self.assertFalse(web.is_local("8.8.8.8"))
        self.assertFalse(web.is_local("2a01:cb00::1"))
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        srv = web.start(self.e, "127.0.0.1", port, "secret")
        try:
            h = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode(), "X-Forwarded-For": "8.8.8.8"}
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d/api/state" % port, headers=h))
            self.assertEqual(cm.exception.code, 403)
        finally:
            srv.shutdown()


class TestRevolutXClient(unittest.TestCase):
    def test_signed_request_and_order_body(self):
        priv, pub = ed25519_pure.generate_keypair()
        calls = []

        def fake_http(method, url, headers=None, body=None, timeout=20):
            calls.append((method, url, headers, body))
            if url.endswith("/api/1.0/orders") and method == "POST":
                return {"data": [{"venue_order_id": "abc", "state": "new"}]}
            if "/api/1.0/orders/abc" in url:
                return {"data": {"status": "filled", "filled_quantity": "0.0001", "average_fill_price": "60000",
                                 "filled_amount": "6.00", "total_fee": "0.0054", "fee_currency": "EUR"}}
            if url.endswith("/api/1.0/balances"):
                return [{"currency": "EUR", "available": "42.5", "total": "42.5"}]
            raise AssertionError(url)

        orig_http, orig_sleep = broker_mod._http, broker_mod.time.sleep
        broker_mod._http = fake_http
        broker_mod.time.sleep = lambda s: None
        try:
            b = broker_mod.RevolutXBroker("KEY", priv, FakeMarket())
            self.assertEqual(b.balances()["EUR"], 42.5)
            o = b.market_order("BTC", "buy", quote_eur=6.009)
        finally:
            broker_mod._http, broker_mod.time.sleep = orig_http, orig_sleep
        method, url, headers, body = calls[1]
        sent = json.loads(body)
        self.assertEqual(sent["symbol"], "BTC-EUR")
        self.assertEqual(sent["side"], "buy")
        self.assertEqual(sent["order_configuration"], {"market": {"quote_size": "6.00"}})
        msg = (headers["X-Revx-Timestamp"] + "POST/api/1.0/orders" + body).encode()
        sig = base64.b64decode(headers["X-Revx-Signature"])
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
            load_pem_public_key(pub.encode()).verify(sig, msg)
        except ImportError:
            seed = ed25519_pure.load_private_pem(priv)
            self.assertEqual(sig, ed25519_pure.sign(seed, msg))
        self.assertEqual(headers["X-Revx-API-Key"], "KEY")
        qty, spent = broker_mod.normalize_fill(o, "BTC", "buy")
        self.assertAlmostEqual(spent, 6.0054)
        self.assertAlmostEqual(qty, 0.0001)

    def test_retries_on_rate_limit(self):
        import io
        calls = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=20):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, io.BytesIO(b'{"message":"Rate limit exceeded"}'))
            return Resp(b'{"data": []}')

        orig_open, orig_sleep = broker_mod.urllib.request.urlopen, broker_mod.time.sleep
        sleeps = []
        broker_mod.urllib.request.urlopen = fake_urlopen
        broker_mod.time.sleep = sleeps.append
        try:
            self.assertEqual(broker_mod._http("GET", "https://revx.revolut.com/api/x"), {"data": []})
        finally:
            broker_mod.urllib.request.urlopen, broker_mod.time.sleep = orig_open, orig_sleep
        self.assertEqual(len(calls), 3)
        self.assertIn(3, sleeps)
        self.assertIn(6, sleeps)

    def test_fee_in_base_currency(self):
        q, e = broker_mod.normalize_fill({"qty": 1.0, "cost": 10, "fee": 0.01, "fee_currency": "SOL"}, "SOL", "buy")
        self.assertAlmostEqual(q, 0.99)
        self.assertAlmostEqual(e, 10)


class TestRadar(Base):
    def setUp(self):
        super().setUp()
        m = self.market
        m.extra_pairs = ["DOGE", "USDC", "TINY", "DOWN"]
        m.history = {"BTC": series("up_pullback", start=40000), "DOGE": series("up_pullback", start=0.1),
                     "USDC": series("flat", start=1.0), "TINY": series("up_pullback", start=1.0),
                     "DOWN": series("down", start=10.0), "ETH": series("hot", start=2000)}
        m.volumes = {"TINY": 10.0}
        self.e.cfg["radar"]["enabled"] = True
        self.stocks.data = {"IWDA.AS": series("up_pullback", start=80), "NVDA": series("hot", start=100),
                            "TSLA": series("down", start=300)}

    def test_indicators_and_scores(self):
        from microinvest.radar import indicators, score
        s1, l1, r1 = score(indicators(series("up_pullback")), "crypto")
        s2, l2, r2 = score(indicators(series("down")), "crypto")
        s3, l3, r3 = score(indicators(series("hot")), "crypto")
        self.assertGreater(s1, s2)
        self.assertGreater(s1, s3)
        self.assertEqual(l2, "⛔ À éviter")
        self.assertTrue(any("point d'entrée" in r for r in r1))
        self.assertTrue(any("surchauffe" in r for r in r3))
        self.assertIsNone(indicators([1.0] * 10))  # trop peu d'historique

    def test_full_scan(self):
        data = self.e.get_radar().run()
        syms = [r["symbol"] for r in data["crypto"]]
        self.assertNotIn("USDC", syms)                       # stablecoin écarté
        self.assertIn("DOGE", syms)
        tiny = [r for r in data["crypto"] if r["symbol"] == "TINY"][0]
        self.assertEqual(tiny["label"], "💧 Peu liquide")
        self.assertLessEqual(tiny["score"], 40)
        self.assertEqual([r["symbol"] for r in data["stocks"]][0], "IWDA.AS")
        self.assertEqual(len(data["stocks"]), 3)              # les autres : cours indisponibles, ignorés
        txt = self.e.get_radar().summary_text(data)
        self.assertIn("DOGE", txt)
        self.assertIn("DOWN", txt)                            # dans la liste « à éviter »
        self.assertIn("pas une prédiction", txt)
        btn = [b[1] for row in self.e.get_radar().summary_buttons(data) for b in row]
        self.assertIn("add:c:DOGE", btn)
        self.assertNotIn("add:c:BTC", btn)                    # déjà dans le plan
        self.assertNotIn("add:s:IWDA.AS", btn)
        info, r = self.e.get_radar().info_text(data, "doge")
        self.assertIn("Note", info)
        info, r = self.e.get_radar().info_text(data, "NVDA")
        self.assertIn("Nvidia", info)
        st = self.e.state()["radar"]
        self.assertEqual(st["n_stocks"], 3)
        json.dumps(st)

    def test_scan_survives_stock_outage(self):
        self.stocks.fail = True
        data = self.e.get_radar().run()
        self.assertEqual(data["stocks"], [])
        self.assertIn("indisponibles", self.e.get_radar().summary_text(data))

    def test_daily_radar_in_tick(self):
        self.e.tick()
        for _ in range(100):
            if self.e.store.get("radar"):
                break
            time.sleep(0.02)
        self.assertIsNotNone(self.e.store.get("radar"))

    def test_telegram_radar_and_add(self):
        sent = []
        bot = TelegramBot("T", "42", self.e, api=lambda t, m, p=None, timeout=20: sent.append((m, p)) or {"message_id": 1},
                          base_dir=self.dir)
        self.e.notifier = bot
        click = lambda d: bot.handle({"update_id": 2, "callback_query": {"id": "c", "data": d, "message": {"chat": {"id": 42}}}})
        texts = lambda: [p.get("text", "") for m, p in sent if m == "sendMessage"]
        bot.handle({"update_id": 1, "message": {"chat": {"id": 42}, "text": "/radar"}})
        self.assertIn("lancée", texts()[-1])
        for _ in range(100):
            if self.e.store.get("radar"):
                break
            time.sleep(0.02)
        time.sleep(0.1)
        self.assertTrue(any("🔭 Radar du" in t for t in texts()))
        click("add:c:DOGE")
        self.assertIn("Ajouter DOGE", texts()[-1])
        click("addok:c:DOGE:7.5")
        self.assertEqual(self.e.cfg["dca"]["targets"]["DOGE"], 7.5)
        click("add:s:NVDA")
        click("addok:s:NVDA:20")
        self.assertEqual(self.e.stock_targets(), {"IWDA.AS": 100, "NVDA": 20.0})
        bot.handle({"update_id": 3, "message": {"chat": {"id": 42}, "text": "/radar crypto"}})
        self.assertIn("Classement crypto", texts()[-1])
        bot.handle({"update_id": 4, "message": {"chat": {"id": 42}, "text": "/info DOWN"}})
        self.assertIn("À éviter", texts()[-1])
        bot.handle({"update_id": 5, "message": {"chat": {"id": 42}, "text": "/actions"}})
        self.assertIn("Nvidia", texts()[-1])
        bot.handle({"update_id": 6, "message": {"chat": {"id": 42}, "text": "/cible_action NVDA 0"}})
        self.assertEqual(self.e.stock_targets(), {"IWDA.AS": 100})
        bot.handle({"update_id": 7, "message": {"chat": {"id": 42}, "text": "/cible_action XYZ 10"}})
        self.assertIn("pas dans la liste", texts()[-1])


class TestStockPlan(Base):
    def test_default_single_line(self):
        self.assertEqual(self.e.stock_plan(), [["IWDA.AS", 15]])

    def test_multi_lines_rotation(self):
        self.e.cfg["stocks"]["targets"] = {"IWDA.AS": 60, "NVDA": 20, "MC.PA": 20}
        plan = self.e.stock_plan()
        self.assertAlmostEqual(sum(a for _, a in plan), 15)
        self.assertTrue(all(a >= 5 for _, a in plan))
        self.assertLessEqual(len(plan), 3)
        self.assertEqual(plan[0][0], "IWDA.AS")
        self.e.record_etf(15, plan)
        for _ in range(5):  # sur plusieurs mois, chaque ligne converge vers sa cible
            p = self.e.stock_plan()
            self.e.record_etf(sum(a for _, a in p), p)
        inv = self.e.stock_invested()
        total = sum(inv.values())
        self.assertAlmostEqual(total, 90)
        self.assertAlmostEqual(inv["IWDA.AS"] / total, 0.6, delta=0.08)
        self.assertGreater(inv.get("NVDA", 0), 10)
        self.assertGreater(inv.get("MC.PA", 0), 10)

    def test_monthly_prompt_lists_lines_and_records_them(self):
        self.e.cfg["stocks"]["targets"] = {"IWDA.AS": 50, "NVDA": 50}
        self.clock.t = time.mktime((2026, 11, 5, 18, 10, 0, 0, 0, -1))
        self.e.tick()
        a = [x for x in self.e.store.pending_approvals() if x["kind"] == "etf"][0]
        self.assertIn("Nvidia", a["text"])
        self.assertIn("iShares Core MSCI World", a["text"])
        self.e.resolve_approval(a["id"], True)
        inv = self.e.stock_invested()
        self.assertEqual(set(inv), {"IWDA.AS", "NVDA"})
        self.assertAlmostEqual(sum(inv.values()), 15)

    def test_legacy_etf_trades_count_for_main_line(self):
        self.e.store.add_trade("ETF", "buy", 0, 15, 0, "ancien", kind="etf")
        self.assertEqual(self.e.stock_invested(), {"IWDA.AS": 15})


PROJECT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def make_zipball(src, sha, extra=None, top="owner-repo-"):
    """Fabrique une archive comme celle que renvoie GitHub (dossier racine owner-repo-sha/)."""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for dp, dns, fns in os.walk(src):
            dns[:] = [d for d in dns if d not in ("data", "keys", "logs", "__pycache__")]
            for f in fns:
                if f in ("config.json", "microinvest.zip") or f.endswith(".pyc"):
                    continue
                p = os.path.join(dp, f)
                z.write(p, top + sha[:7] + "/" + os.path.relpath(p, src))
        for name, content in (extra or {}).items():
            z.writestr(top + sha[:7] + "/" + name, content)
    return buf.getvalue()


@unittest.skipIf(os.environ.get("MICROINVEST_UPDATING"), "déjà dans un test de mise à jour")
class TestUpdater(unittest.TestCase):
    def setUp(self):
        from microinvest.updater import Updater
        self.dir = tempfile.mkdtemp()
        for f in ("main.py", "run.sh", "stop.sh"):
            shutil.copy(os.path.join(PROJECT, f), self.dir)
        shutil.copytree(os.path.join(PROJECT, "microinvest"), os.path.join(self.dir, "microinvest"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(self.dir, "data"))
        with open(os.path.join(self.dir, "config.json"), "w") as f:
            f.write('{"mode": "paper", "secret": "a-moi"}')
        with open(os.path.join(self.dir, "data", "paper.db"), "w") as f:
            f.write("mes données")
        self.sha = "abcdef1234567890"
        self.zip = make_zipball(PROJECT, self.sha, {"microinvest/marker.txt": "nouvelle version"})
        commit = json.dumps({"sha": self.sha, "commit": {"message": "Ajoute un marqueur\n\ndétails",
                                                           "committer": {"date": "2026-11-16T10:00:00Z"}}}).encode()

        def http(url, accept):
            self.urls.append(url)
            return self.zip if "/zipball/" in url else commit

        self.urls = []
        self.up = Updater(self.dir, {"update": {"repo": "owner/repo", "branch": "main"}}, http=http)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self, *p):
        with open(os.path.join(self.dir, *p)) as f:
            return f.read()

    def test_latest(self):
        last = self.up.latest()
        self.assertEqual(last["sha"], self.sha)
        self.assertEqual(last["message"], "Ajoute un marqueur")
        self.assertIn("/repos/owner/repo/commits/main", self.urls[0])

    def test_install_then_rollback(self):
        self.assertIsNone(self.up.installed())
        msg = self.up.install(self.sha, "Ajoute un marqueur")
        self.assertIn("abcdef1", msg)
        self.assertEqual(self.read("microinvest", "marker.txt"), "nouvelle version")
        self.assertIn("a-moi", self.read("config.json"))            # config intacte
        self.assertEqual(self.read("data", "paper.db"), "mes données")  # données intactes
        self.assertEqual(self.up.installed()["sha"], self.sha)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "data", "update_pending")))
        self.assertTrue(self.up.can_rollback())
        self.up.rollback()
        self.assertIsNone(self.up.installed())
        self.assertFalse(os.path.exists(os.path.join(self.dir, "data", "update_pending")))
        self.assertIn("a-moi", self.read("config.json"))

    def test_failing_tests_block_install(self):
        from microinvest.updater import UpdateError
        self.zip = make_zipball(PROJECT, self.sha, {
            "tests/test_casse.py": "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.fail('cassé')\n",
            "microinvest/marker.txt": "ne doit pas arriver"})
        with self.assertRaises(UpdateError) as cm:
            self.up.install(self.sha)
        self.assertIn("échouent", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "microinvest", "marker.txt")))
        self.assertIsNone(self.up.installed())

    def test_syntax_error_blocks_install(self):
        from microinvest.updater import UpdateError
        self.zip = make_zipball(PROJECT, self.sha, {"microinvest/casse.py": "x = 'non fermée\n"})
        with self.assertRaises(UpdateError) as cm:
            self.up.install(self.sha)
        self.assertIn("syntaxe", str(cm.exception))

    def test_rejects_path_traversal(self):
        import io
        import zipfile
        from microinvest.updater import UpdateError
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("x/../../evil.txt", "boom")
        self.zip = buf.getvalue()
        with self.assertRaises(UpdateError):
            self.up.install(self.sha)

    def test_no_repo(self):
        from microinvest.updater import Updater, UpdateError
        with self.assertRaises(UpdateError):
            Updater(self.dir, {}).latest()


class TestTelegramControls(Base):
    def setUp(self):
        super().setUp()
        self.sent = []

        def api(token, method, params=None, timeout=20):
            self.sent.append((method, params))
            return {"message_id": len(self.sent)}

        class FakeUpdater:
            cfg = {}
            installed_v = None
            restored = False

            def latest(s):
                return {"sha": "1234567abc", "message": "Nouvelle fonction", "date": ""}

            def installed(s):
                return s.installed_v

            def install(s, sha, message=""):
                s.installed_v = {"sha": sha, "message": message, "ts": 0}
                return "version %s installée" % sha[:7]

            def can_rollback(s):
                return True

            def rollback(s):
                s.restored = True
                return "version précédente remise en place"

        self.updater = FakeUpdater()
        self.restarts = []
        self.bot = TelegramBot("T", "42", self.e, api=api, updater=self.updater,
                               restart=lambda: self.restarts.append(1), base_dir=self.dir)
        self.e.notifier = self.bot

    def msg(self, text, mid=7):
        self.bot.handle({"update_id": 1, "message": {"message_id": mid, "chat": {"id": 42}, "text": text}})

    def click(self, data):
        self.bot.handle({"update_id": 2, "callback_query": {"id": "c", "data": data, "message": {"chat": {"id": 42}}}})

    def texts(self):
        return [p.get("text", "") for m, p in self.sent if m == "sendMessage"]

    def test_keyboard_buttons_and_menu(self):
        self.msg("/start")
        self.assertIn("keyboard", self.sent[-1][1]["reply_markup"])
        self.msg("📊 Statut")
        self.assertIn("Mode", self.texts()[-1])
        self.msg("⚙️ Menu")
        menu = self.sent[-1][1]["reply_markup"]["inline_keyboard"]
        datas = [b["callback_data"] for row in menu for b in row]
        for d in datas:  # chaque bouton du menu répond quelque chose sans erreur
            n = len(self.sent)
            self.click(d)
            self.assertGreater(len(self.sent), n, d)
            self.assertNotIn("Erreur", self.texts()[-1])

    def test_settings(self):
        self.msg("/regler budget_crypto 40")
        self.assertIn("✅", self.texts()[-1])
        self.assertEqual(self.e.cfg["budget"]["monthly_crypto_eur"], 40.0)
        with open(self.cfg_path) as f:
            self.assertEqual(json.load(f)["budget"]["monthly_crypto_eur"], 40.0)
        self.msg("/regler jour_achat mercredi")
        self.assertEqual(self.e.cfg["dca"]["weekday"], 2)
        self.msg("/regler prise_benefices 2")
        self.assertIn("❌", self.texts()[-1])
        self.assertEqual(self.e.cfg["take_profit"]["gain_pct"], 40)
        self.msg("/regler prise_benefices_active non")
        self.assertFalse(self.e.cfg["take_profit"]["enabled"])
        self.msg("/regler inconnu 3")
        self.assertIn("Noms possibles", self.texts()[-1])
        self.msg("/cible SOL 20")
        self.assertEqual(self.e.cfg["dca"]["targets"]["SOL"], 20)
        self.msg("/cible LINK 0")
        self.assertNotIn("LINK", self.e.cfg["dca"]["targets"])
        self.msg("/reglages")
        self.assertIn("budget_crypto = 40", self.texts()[-1])

    def test_update_flow(self):
        self.msg("/maj")
        self.assertIn("1234567", self.texts()[-1])
        self.click("cmd:update")
        for _ in range(50):
            if self.restarts:
                break
            time.sleep(0.05)
        self.assertEqual(self.restarts, [1])
        self.assertTrue(any("installée" in t for t in self.texts()))
        self.msg("/maj")
        self.assertIn("déjà la dernière version", self.texts()[-1])
        self.msg("/retour")
        self.click("cmd:rollback")
        self.assertTrue(self.updater.restored)
        self.assertEqual(len(self.restarts), 2)
        self.click("cmd:restart")
        self.assertEqual(len(self.restarts), 3)

    def test_api_key_message_is_deleted_and_validated(self):
        self.msg("/cle")
        self.assertIn("BEGIN PUBLIC KEY", self.texts()[-1])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "keys", "private.pem")))
        orig = broker_mod._http
        broker_mod._http = lambda *a, **k: [{"currency": "EUR", "available": "70", "total": "70"}]
        try:
            self.msg("/cleapi CLE_API_TRES_LONGUE_1234567890", mid=99)
        finally:
            broker_mod._http = orig
        self.assertIn(("deleteMessage", {"chat_id": "42", "message_id": 99}), self.sent)
        self.assertIn("70,00", self.texts()[-1].replace(".", ","))
        self.assertEqual(self.e.cfg["revolutx"]["api_key"], "CLE_API_TRES_LONGUE_1234567890")
        self.click("cmd:mode_live")
        self.assertEqual(self.e.mode, "live")

    def test_repo_setting(self):
        self.msg("/depot moi/microinvest ghp_secret", mid=55)
        self.assertIn(("deleteMessage", {"chat_id": "42", "message_id": 55}), self.sent)
        self.assertEqual(self.e.cfg["update"]["repo"], "moi/microinvest")
        self.assertEqual(self.e.cfg["update"]["token"], "ghp_secret")
        self.assertIn("1234567", self.texts()[-1])


class TestMainScript(unittest.TestCase):
    def test_main_compiles_and_help_runs(self):
        import subprocess
        main = os.path.join(PROJECT, "main.py")
        with open(main, "rb") as f:
            compile(f.read(), main, "exec")
        r = subprocess.run([sys.executable, main], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout.decode())
        self.assertIn(b"setup", r.stdout)

    def test_shell_scripts_syntax(self):
        import subprocess
        for f in ("run.sh", "stop.sh"):
            r = subprocess.run(["sh", "-n", os.path.join(PROJECT, f)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(r.returncode, 0, r.stdout.decode())


class TestConfig(unittest.TestCase):
    def test_rejects_bad_values(self):
        from microinvest.config import ConfigError
        bad = copy.deepcopy(DEFAULTS)
        bad["take_profit"]["gain_pct"] = 1
        with self.assertRaises(ConfigError):
            validate(bad)

    def test_example_config_is_valid(self):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.example.json")
        with open(p) as f:
            validate(_merge(DEFAULTS, json.load(f)))


if __name__ == "__main__":
    unittest.main()
