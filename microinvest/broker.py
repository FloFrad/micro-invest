"""Accès au marché : client Revolut X (réel) et courtier simulé (paper).

Les deux exposent la même interface, ce qui permet de tester toute la
stratégie en simulation avec les vrais prix avant de passer en réel.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, ROUND_DOWN

from . import ed25519_pure

log = logging.getLogger("broker")

BASE_URL = "https://revx.revolut.com"
TAKER_FEE = Decimal("0.0009")  # 0,09 % (frais "taker" Revolut X)


class BrokerError(Exception):
    pass


class InsufficientFunds(BrokerError):
    pass


def D(x):
    return x if isinstance(x, Decimal) else Decimal(str(x))


def round_down(value, step):
    value, step = D(value), D(step)
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


MIN_INTERVAL = 1.2  # secondes minimum entre deux requêtes Revolut X (limite de débit)
_rate_lock = threading.Lock()
_last_call = [0.0]


def _throttle():
    with _rate_lock:
        wait = _last_call[0] + MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _http(method, url, headers=None, body=None, timeout=20, retries=4):
    data = body.encode() if isinstance(body, str) else body
    for attempt in range(retries + 1):
        _throttle()
        req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
        req.add_header("User-Agent", "microinvest/1.0")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode() or "null"
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")[:500]
            if e.code == 429 and attempt < retries:
                delay = 3 * (2 ** attempt)  # 3, 6, 12, 24 s
                log.info("Limite de débit Revolut X atteinte, nouvel essai dans %d s", delay)
                time.sleep(delay)
                continue
            if "insufficient" in txt.lower():
                raise InsufficientFunds(txt)
            raise BrokerError("HTTP %s sur %s : %s" % (e.code, url.split("?")[0], txt))
        except urllib.error.URLError as e:
            raise BrokerError("Connexion impossible à %s : %s" % (url.split("?")[0], e.reason))


class MarketData:
    """Données publiques Revolut X (aucune clé nécessaire)."""

    def __init__(self, quote="EUR"):
        self.quote = quote
        self._pairs = None
        self._candle_cache = {}

    def pairs(self):
        if self._pairs is None:
            data = _http("GET", BASE_URL + "/api/1.0/public/configuration/pairs")
            data = data.get("data", data) if isinstance(data, dict) else {}
            out = {}
            for _k, v in data.items():
                if isinstance(v, dict) and v.get("base") and v.get("quote"):
                    out["%s-%s" % (v["base"], v["quote"])] = v
            self._pairs = out
        return self._pairs

    def pair_info(self, asset):
        info = self.pairs().get(self.symbol(asset))
        if not info:
            raise BrokerError("La paire %s n'existe pas sur Revolut X" % self.symbol(asset))
        return info

    def symbol(self, asset):
        return "%s-%s" % (asset, self.quote)

    def tickers(self, assets):
        syms = ",".join(self.symbol(a) for a in assets)
        data = _http("GET", BASE_URL + "/api/1.0/public/tickers?" + urllib.parse.urlencode({"symbols": syms}))
        out = {}
        for t in (data or {}).get("data", []):
            base = t.get("symbol", "").replace("-", "/").split("/")[0]
            if base in assets:
                out[base] = {
                    "bid": float(t.get("bid") or 0),
                    "ask": float(t.get("ask") or 0),
                    "last": float(t.get("last_price") or t.get("mid") or 0),
                }
        return out

    def all_tickers(self):
        """Prix et volume 24 h de toutes les paires en euros (une seule requête)."""
        data = _http("GET", BASE_URL + "/api/1.0/public/tickers")
        out = {}
        for t in (data or {}).get("data", []):
            parts = t.get("symbol", "").replace("-", "/").split("/")
            if len(parts) == 2 and parts[1] == self.quote:
                out[parts[0]] = {
                    "bid": float(t.get("bid") or 0), "ask": float(t.get("ask") or 0),
                    "last": float(t.get("last_price") or t.get("mid") or 0),
                    "volume": float(t.get("volume_24h") or 0),
                }
        return out

    def daily_closes(self, asset, days=30):
        """Clôtures journalières (cache 6 h)."""
        key = (asset, days)
        cached = self._candle_cache.get(key)
        if cached and time.time() - cached[0] < 6 * 3600:
            return cached[1]
        now = int(time.time() * 1000)
        q = urllib.parse.urlencode({"interval": 1440, "since": now - days * 86400000, "until": now})
        data = _http("GET", BASE_URL + "/api/1.0/public/candles/%s?%s" % (self.symbol(asset), q))
        closes = [float(c["close"]) for c in (data or {}).get("data", []) if c.get("close")]
        self._candle_cache[key] = (time.time(), closes)
        return closes


class RevolutXBroker:
    """Ordres réels sur Revolut X (signature Ed25519)."""

    mode = "live"

    def __init__(self, api_key, private_key_pem, market):
        if not api_key:
            raise BrokerError("Clé API Revolut X manquante (config.json → revolutx.api_key)")
        self.api_key = api_key.strip()
        self.seed = ed25519_pure.load_private_pem(private_key_pem)
        self.pub = ed25519_pure.public_key(self.seed)
        self.market = market

    def _private(self, method, path, query=None, body=None):
        qs = urllib.parse.urlencode(query) if query else ""
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        ts = str(int(time.time() * 1000))
        msg = ts + method + "/api/" + path + qs + body_str
        import base64
        sig = base64.b64encode(ed25519_pure.sign(self.seed, msg.encode(), self.pub)).decode()
        headers = {"X-Revx-API-Key": self.api_key, "X-Revx-Timestamp": ts, "X-Revx-Signature": sig}
        if body is not None:
            headers["Content-Type"] = "application/json"
        url = BASE_URL + "/api/" + path + ("?" + qs if qs else "")
        return _http(method, url, headers, body_str if body is not None else None)

    def balances(self):
        data = self._private("GET", "1.0/balances")
        rows = data if isinstance(data, list) else (data or {}).get("data", [])
        return {r["currency"]: float(r.get("available") or 0) for r in rows}

    def market_order(self, asset, side, quote_eur=None, base_qty=None):
        """Place un ordre au marché et attend son exécution (max ~20 s)."""
        info = self.market.pair_info(asset)
        cfg = {}
        if quote_eur is not None:
            q = round_down(quote_eur, info.get("quote_step", "0.01"))
            if q < D(info.get("min_order_size_quote", "0")):
                raise BrokerError("Montant %s € sous le minimum Revolut X pour %s" % (q, asset))
            cfg["quote_size"] = str(q)
        else:
            b = round_down(base_qty, info.get("base_step", "0.00000001"))
            if b <= 0 or b < D(info.get("min_order_size", "0")):
                raise BrokerError("Quantité %s %s sous le minimum Revolut X" % (b, asset))
            cfg["base_size"] = str(b)
        body = {
            "client_order_id": str(uuid.uuid4()),
            "symbol": self.market.symbol(asset),
            "side": side,
            "order_configuration": {"market": cfg},
        }
        res = self._private("POST", "1.0/orders", body=body)
        data = res.get("data") if isinstance(res, dict) else None
        if isinstance(data, list):
            data = data[0] if data else {}
        order_id = (data or {}).get("venue_order_id")
        if not order_id:
            raise BrokerError("Réponse inattendue de Revolut X : %s" % str(res)[:300])
        for _ in range(10):
            time.sleep(2)
            o = self.order(order_id)
            if o["status"] in ("filled", "cancelled", "canceled", "rejected"):
                return o
        return self.order(order_id)

    def order(self, order_id):
        res = self._private("GET", "1.0/orders/%s" % order_id)
        o = res.get("data", res) if isinstance(res, dict) else {}
        return {
            "id": order_id,
            "status": o.get("status", "unknown"),
            "qty": float(o.get("filled_quantity") or 0),
            "price": float(o.get("average_fill_price") or 0),
            "cost": float(o.get("filled_amount") or 0),
            "fee": float(o.get("total_fee") or 0),
            "fee_currency": o.get("fee_currency", ""),
        }


class PaperBroker:
    """Simulation : vrais prix Revolut X, faux argent, mêmes règles."""

    mode = "paper"

    def __init__(self, market, store, start_eur=100.0):
        self.market = market
        self.store = store
        if self.store.get("paper_balances") is None:
            self.store.set("paper_balances", {market.quote: float(start_eur)})

    def balances(self):
        return dict(self.store.get("paper_balances") or {})

    def _save(self, b):
        self.store.set("paper_balances", b)

    def market_order(self, asset, side, quote_eur=None, base_qty=None):
        info = self.market.pair_info(asset)
        t = self.market.tickers([asset]).get(asset)
        if not t:
            raise BrokerError("Prix indisponible pour %s" % asset)
        b = self.balances()
        quote = self.market.quote
        if side == "buy":
            price = D(t["ask"] or t["last"])
            spend = round_down(quote_eur, info.get("quote_step", "0.01"))
            if spend < D(info.get("min_order_size_quote", "0")):
                raise BrokerError("Montant %s € sous le minimum Revolut X pour %s" % (spend, asset))
            if D(b.get(quote, 0)) < spend:
                raise InsufficientFunds("Solde simulé insuffisant")
            fee_q = spend * TAKER_FEE
            qty = round_down((spend - fee_q) / price, info.get("base_step", "0.00000001"))
            b[quote] = float(D(b.get(quote, 0)) - spend)
            b[asset] = float(D(b.get(asset, 0)) + qty)
            cost, fee = spend - fee_q, fee_q
        else:
            price = D(t["bid"] or t["last"])
            qty = round_down(base_qty, info.get("base_step", "0.00000001"))
            if qty <= 0:
                raise BrokerError("Quantité nulle")
            if D(b.get(asset, 0)) < qty:
                raise InsufficientFunds("Quantité simulée insuffisante")
            gross = qty * price
            fee = gross * TAKER_FEE
            b[asset] = float(D(b.get(asset, 0)) - qty)
            b[quote] = float(D(b.get(quote, 0)) + gross - fee)
            cost = gross
        self._save(b)
        return {
            "id": "paper-" + uuid.uuid4().hex[:12],
            "status": "filled",
            "qty": float(qty),
            "price": float(price),
            "cost": float(cost),
            "fee": float(fee),
            "fee_currency": quote,
        }

    def order(self, order_id):
        raise BrokerError("Ordre simulé introuvable")


def normalize_fill(order, asset, side, quote="EUR"):
    """Convertit un ordre exécuté en (quantité nette d'actif, euros nets).

    Achat : euros dépensés frais compris. Vente : euros reçus frais déduits.
    """
    qty, cost, fee = float(order["qty"]), float(order["cost"]), float(order["fee"])
    fc = (order.get("fee_currency") or quote).upper()
    if fc == asset.upper():
        qty_net = qty - fee if side == "buy" else qty + fee
        eur_net = cost
    else:
        qty_net = qty
        eur_net = cost + fee if side == "buy" else cost - fee
    return qty_net, eur_net
