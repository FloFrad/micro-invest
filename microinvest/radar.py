"""Radar : analyse et notation des cryptos et des actions/ETF.

Le radar ne prédit rien. Il applique des règles classiques et vérifiables,
puis explique chaque note en français :
  - Tendance (40 pts) : le prix est-il au-dessus de sa moyenne longue (200 j) ?
  - Élan (20 pts)     : performance sur 90 jours.
  - Point d'entrée (25 pts) : repli raisonnable sous la moyenne 30 j dans une
    tendance haussière (acheter les baisses d'un actif qui monte).
  - Risque (15 pts)   : volatilité annuelle (plus c'est calme, plus de points).
Les cryptos peu échangées (liquidité faible) et les stablecoins sont écartés.
"""
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .broker import BrokerError
from . import fmt as F

log = logging.getLogger("radar")

STABLECOINS = {"USDT", "USDC", "EURC", "DAI", "PYUSD", "RLUSD", "USDE", "FDUSD", "TUSD", "EURT", "EUROC",
               "USDP", "GUSD", "BUSD", "USDS", "FRAX", "LUSD", "EURI", "USD1", "USDG", "EURQ", "EURR"}
MIN_LIQUIDITY_EUR = 50000  # volume échangé sur 24 h en dessous duquel une crypto est jugée peu liquide

# Actions et ETF populaires disponibles chez Revolut (vérifie la disponibilité dans l'appli).
# (code Yahoo Finance, nom, catégorie, nom à chercher dans l'appli Revolut)
STOCKS = [
    ("IWDA.AS", "iShares Core MSCI World", "ETF Monde", "IWDA"),
    ("SXR8.DE", "iShares Core S&P 500", "ETF États-Unis", "CSPX / SXR8"),
    ("EQQQ.DE", "Invesco Nasdaq-100", "ETF Tech US", "EQQQ"),
    ("IS3N.DE", "iShares Core MSCI Emerging Markets IMI", "ETF Émergents", "EIMI"),
    ("MEUD.PA", "Amundi Core Stoxx Europe 600", "ETF Europe", "MEUD"),
    ("AAPL", "Apple", "Action US", "Apple"),
    ("MSFT", "Microsoft", "Action US", "Microsoft"),
    ("NVDA", "Nvidia", "Action US", "Nvidia"),
    ("GOOGL", "Alphabet (Google)", "Action US", "Alphabet"),
    ("AMZN", "Amazon", "Action US", "Amazon"),
    ("META", "Meta Platforms", "Action US", "Meta"),
    ("TSLA", "Tesla", "Action US", "Tesla"),
    ("AVGO", "Broadcom", "Action US", "Broadcom"),
    ("BRK-B", "Berkshire Hathaway B", "Action US", "Berkshire"),
    ("JPM", "JPMorgan Chase", "Action US", "JPMorgan"),
    ("V", "Visa", "Action US", "Visa"),
    ("LLY", "Eli Lilly", "Action US", "Eli Lilly"),
    ("COST", "Costco", "Action US", "Costco"),
    ("KO", "Coca-Cola", "Action US", "Coca-Cola"),
    ("ASML.AS", "ASML", "Action Europe", "ASML"),
    ("MC.PA", "LVMH", "Action France", "LVMH"),
    ("OR.PA", "L'Oréal", "Action France", "L'Oréal"),
    ("RMS.PA", "Hermès", "Action France", "Hermès"),
    ("TTE.PA", "TotalEnergies", "Action France", "TotalEnergies"),
    ("AIR.PA", "Airbus", "Action France", "Airbus"),
    ("SU.PA", "Schneider Electric", "Action France", "Schneider"),
    ("AI.PA", "Air Liquide", "Action France", "Air Liquide"),
    ("SAN.PA", "Sanofi", "Action France", "Sanofi"),
    ("SAF.PA", "Safran", "Action France", "Safran"),
    ("SAP.DE", "SAP", "Action Europe", "SAP"),
    ("SIE.DE", "Siemens", "Action Europe", "Siemens"),
    ("NOVO-B.CO", "Novo Nordisk", "Action Europe", "Novo Nordisk"),
    ("NESN.SW", "Nestlé", "Action Europe", "Nestlé"),
]
STOCK_INFO = {s[0]: {"name": s[1], "category": s[2], "search": s[3]} for s in STOCKS}


# --------------------------------------------------------------------- calculs
def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def indicators(closes):
    """Indicateurs à partir des clôtures journalières (de la plus ancienne à la plus récente)."""
    closes = [c for c in closes if c and c > 0]
    if len(closes) < 60:
        return None
    price = closes[-1]
    long_n = min(200, len(closes))
    ma_long = _mean(closes[-long_n:])
    ma50 = _mean(closes[-50:])
    ma30 = _mean(closes[-30:])
    r30 = price / closes[-31] - 1 if len(closes) > 30 else 0.0
    r90 = price / closes[-91] - 1 if len(closes) > 90 else price / closes[0] - 1
    high = max(closes[-365:])
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(max(1, len(closes) - 90), len(closes))]
    m = _mean(rets)
    vol = math.sqrt(_mean([(r - m) ** 2 for r in rets])) * math.sqrt(365) if rets else 0.0
    return {
        "price": price, "ma_long": ma_long, "ma_long_days": long_n, "ma50": ma50, "ma30": ma30,
        "r30": r30, "r90": r90, "from_high": price / high - 1, "vs_ma30": price / ma30 - 1, "vol": vol,
        "days": len(closes),
    }


def score(ind, kind="crypto"):
    """Note sur 100 + raisons en français + étiquette."""
    reasons = []
    pts = 0.0
    up_long = ind["price"] > ind["ma_long"]
    up_mid = ind["ma50"] > ind["ma_long"]
    n = ind["ma_long_days"]
    # 1. Tendance
    if up_long and up_mid:
        pts += 40
        reasons.append("tendance haussière (au-dessus de sa moyenne %d j)" % n)
    elif up_long:
        pts += 20
        reasons.append("repasse au-dessus de sa moyenne %d j (tendance à confirmer)" % n)
    else:
        reasons.append("tendance baissière (sous sa moyenne %d j)" % n)
    # 2. Élan 90 jours : -30 % → 0 pt, +60 % → 20 pts
    pts += max(0.0, min(20.0, (ind["r90"] + 0.30) / 0.90 * 20))
    reasons.append("%+.0f %% sur 90 jours" % (ind["r90"] * 100))
    # 3. Point d'entrée
    d = ind["vs_ma30"]
    if up_long and -0.15 <= d <= -0.03:
        pts += 25 * (1 - abs(abs(d) - 0.09) / 0.09 * 0.5)
        reasons.append("repli de %.0f %% sous sa moyenne 30 j : point d'entrée intéressant" % (-d * 100))
    elif d > 0.15:
        reasons.append("en surchauffe (+%.0f %% au-dessus de sa moyenne 30 j) : attendre un repli" % (d * 100))
    elif d < -0.15:
        pts += 5
        reasons.append("chute de %.0f %% sous sa moyenne 30 j : très risqué" % (-d * 100))
    else:
        pts += 12
        reasons.append("prix proche de sa moyenne 30 j")
    # 4. Risque
    lo, hi = (0.40, 1.50) if kind == "crypto" else (0.15, 0.60)
    v = ind["vol"]
    pts += max(0.0, min(15.0, (hi - v) / (hi - lo) * 15))
    if v > hi * 0.8:
        reasons.append("très volatil (%.0f %%/an)" % (v * 100))
    elif v < lo * 1.3:
        reasons.append("peu volatil (%.0f %%/an)" % (v * 100))
    s = int(round(pts))
    if not up_long and ind["r90"] < -0.20:
        label = "⛔ À éviter"
    elif s >= 65:
        label = "⭐ Intéressant"
    elif s >= 45:
        label = "👀 À surveiller"
    else:
        label = "😐 Neutre"
    return s, label, reasons


def key_reason(r):
    """La raison la plus utile pour décider (point d'entrée, surchauffe, liquidité…), sinon la tendance."""
    for word in ("peu échangée", "point d'entrée", "surchauffe", "chute", "très volatil"):
        for x in r.get("reasons", []):
            if word in x:
                return x
    return (r.get("reasons") or [""])[0]


# ------------------------------------------------------------ données actions
class StockData:
    """Cours des actions/ETF via Yahoo Finance (gratuit, sans clé)."""

    URL = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=1y&interval=1d"

    def __init__(self, http=None, pause=0.5):
        self._http = http or self._get
        self.pause = pause

    @staticmethod
    def _get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise BrokerError("Yahoo Finance HTTP %s" % e.code)
        except urllib.error.URLError as e:
            raise BrokerError("Yahoo Finance injoignable : %s" % e.reason)

    def closes(self, ticker):
        data = self._http(self.URL % urllib.parse.quote(ticker))
        try:
            res = data["chart"]["result"][0]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            currency = res.get("meta", {}).get("currency", "")
        except (KeyError, IndexError, TypeError):
            raise BrokerError("données Yahoo illisibles pour %s" % ticker)
        return closes, currency


# ------------------------------------------------------------------- radar
class Radar:
    def __init__(self, engine, stocks=None, clock=time.time):
        self.engine = engine
        self.stocks = stocks or StockData()
        self.clock = clock
        self.running = threading.Lock()

    def crypto_universe(self):
        """Toutes les cryptos échangeables en euros sur Revolut X (hors stablecoins)."""
        m = self.engine.market
        out = []
        for sym, info in m.pairs().items():
            base, quote = info.get("base"), info.get("quote")
            if quote != m.quote or not base or base.upper() in STABLECOINS:
                continue
            if info.get("status", "active") not in ("active", "ACTIVE"):
                continue
            out.append(base)
        return sorted(out)

    def scan_crypto(self):
        m = self.engine.market
        assets = self.crypto_universe()
        try:
            tick = m.all_tickers()
        except BrokerError as e:
            log.warning("Tickers indisponibles : %s", e)
            tick = {}
        results = []
        for a in assets:
            t = tick.get(a, {})
            liq = (t.get("volume") or 0) * (t.get("last") or 0)
            try:
                closes = m.daily_closes(a, 365)
            except BrokerError as e:
                log.info("Historique %s indisponible : %s", a, e)
                continue
            ind = indicators(closes)
            if not ind:
                continue
            s, label, reasons = score(ind, "crypto")
            illiquid = bool(tick) and liq < MIN_LIQUIDITY_EUR
            if illiquid:
                s = min(s, 40)
                label = "💧 Peu liquide"
                reasons.append("peu échangée (%.0f € sur 24 h) : prix facilement manipulable" % liq)
            results.append({"symbol": a, "name": a, "kind": "crypto", "score": s, "label": label,
                            "reasons": reasons, "ind": ind, "liquidity": liq})
        results.sort(key=lambda r: -r["score"])
        return results

    def scan_stocks(self):
        results = []
        for ticker, name, cat, search in STOCKS:
            try:
                closes, cur = self.stocks.closes(ticker)
            except BrokerError as e:
                log.info("Cours %s indisponible : %s", ticker, e)
                continue
            finally:
                time.sleep(self.stocks.pause)
            ind = indicators(closes)
            if not ind:
                continue
            kind = "etf" if cat.startswith("ETF") else "stock"
            s, label, reasons = score(ind, "stock")
            if kind == "etf":
                reasons.append("diversifié : un seul achat = des centaines d'entreprises")
            results.append({"symbol": ticker, "name": name, "kind": kind, "category": cat, "search": search,
                            "currency": cur, "score": s, "label": label, "reasons": reasons, "ind": ind})
        results.sort(key=lambda r: -r["score"])
        return results

    def run(self, notify=False):
        """Analyse complète (2 à 4 min). Ne fait rien si une analyse tourne déjà."""
        if not self.running.acquire(blocking=False):
            return None
        try:
            t0 = self.clock()
            crypto = self.scan_crypto()
            stocks = self.scan_stocks()
            data = {"ts": self.clock(), "crypto": crypto, "stocks": stocks, "duration": self.clock() - t0}
            self.engine.store.set("radar", data)
            self.engine.log("info", "Radar : %d cryptos et %d actions/ETF analysées" % (len(crypto), len(stocks)))
            if notify:
                self.engine.notify(self.summary_text(data), self.summary_buttons(data))
            return data
        except Exception as e:
            log.exception("Radar")
            self.engine.log("error", "Radar impossible : %s" % e)
            if notify:
                self.engine.notify("⚠️ Radar impossible : %s" % e)
            return None
        finally:
            self.running.release()

    def run_async(self, notify=True):
        if self.running.locked():
            return False
        threading.Thread(target=self.run, kwargs={"notify": notify}, name="radar", daemon=True).start()
        return True

    def latest(self):
        return self.engine.store.get("radar")

    # ------------------------------------------------------------ textes
    def _in_plan(self, r):
        if r["kind"] == "crypto":
            return r["symbol"] in self.engine.cfg["dca"]["targets"]
        return r["symbol"] in self.engine.stock_targets()

    def picks(self, data, kind, n=5):
        rows = data["crypto"] if kind == "crypto" else data["stocks"]
        return [r for r in rows if r["label"] not in ("⛔ À éviter", "💧 Peu liquide")][:n]

    def summary_text(self, data):
        if not data:
            return "Pas encore d'analyse."
        d = time.strftime("%d/%m à %Hh%M", time.localtime(data["ts"]))
        lines = ["🔭 <b>Radar du %s</b>" % d,
                 "<i>%d cryptos et %d actions/ETF notées sur 100</i>" % (len(data["crypto"]), len(data["stocks"])), "",
                 "🪙 <b>Cryptos les mieux notées</b>"]
        for r in self.picks(data, "crypto"):
            lines.append("%s <b>%s</b> · %d/100 %s%s\n      <i>%s</i>" % (
                r["label"].split()[0], F.esc(r["symbol"]), r["score"], F.bar(r["score"] / 100.0, 5),
                "  ✅ dans ton plan" if self._in_plan(r) else "", F.esc(key_reason(r))))
        if not data["crypto"]:
            lines.append("   (données indisponibles)")
        lines += ["", "📈 <b>Actions &amp; ETF les mieux notés</b>"]
        for r in self.picks(data, "stocks"):
            lines.append("%s <b>%s</b> <i>(%s)</i> · %d/100 %s%s\n      <i>%s</i>" % (
                r["label"].split()[0], F.esc(r["name"]), F.esc(r["category"]), r["score"], F.bar(r["score"] / 100.0, 5),
                "  ✅ dans ton plan" if self._in_plan(r) else "", F.esc(key_reason(r))))
        if not data["stocks"]:
            lines.append("   (cours indisponibles pour l'instant)")
        avoid = [r["symbol"] for r in data["crypto"] if r["label"] == "⛔ À éviter"][:8]
        if avoid:
            lines += ["", "⛔ <b>À éviter pour l'instant</b> (forte baisse) : " + F.esc(", ".join(avoid))]
        lines += ["", "<i>Règles chiffrées (tendance, élan, point d'entrée, risque) : ce n'est pas une prédiction. "
                  "Détail d'un actif : /info SYMBOLE</i>"]
        return "\n".join(lines)

    def summary_buttons(self, data):
        if not data:
            return None
        rows = []
        adds = [("➕ %s" % r["symbol"], "add:c:%s" % r["symbol"]) for r in self.picks(data, "crypto") if not self._in_plan(r)][:3]
        if adds:
            rows.append(adds)
        adds = [("➕ %s" % r["search"].split(" /")[0], "add:s:%s" % r["symbol"]) for r in self.picks(data, "stocks")
                if not self._in_plan(r)][:3]
        if adds:
            rows.append(adds)
        rows.append([("🪙 Classement crypto", "m:/radar crypto"), ("📈 Classement actions", "m:/radar actions")])
        return rows

    def ranking_text(self, data, kind, n=15):
        rows = data["crypto"] if kind == "crypto" else data["stocks"]
        title = ("🪙 <b>Classement crypto</b> (%d)" if kind == "crypto" else "📈 <b>Classement actions &amp; ETF</b> (%d)") % len(rows)
        lines = [title, ""]
        for i, r in enumerate(rows[:n], 1):
            name = r["symbol"] if kind == "crypto" else r["name"]
            lines.append("%d. %s <b>%s</b> · %d/100%s" % (i, r["label"].split()[0], F.esc(name), r["score"],
                                                       " ✅" if self._in_plan(r) else ""))
        if len(rows) > n:
            lines.append("<i>… et %d autres.</i>" % (len(rows) - n))
        lines.append("\n<i>Légende : ⭐ intéressant · 👀 à surveiller · 😐 neutre · ⛔ à éviter · 💧 peu liquide · ✅ dans ton plan. "
                     "Détail : /info SYMBOLE</i>")
        return "\n".join(lines)

    def info_text(self, data, symbol):
        symbol = symbol.upper()
        for r in (data or {}).get("crypto", []) + (data or {}).get("stocks", []):
            if r["symbol"].upper() == symbol or (r["kind"] != "crypto" and r["symbol"].split(".")[0].upper() == symbol):
                i = r["ind"]
                name = (F.b(r["symbol"]) if r["kind"] == "crypto" else
                        "%s <i>(%s · %s)</i>" % (F.b(r["name"]), F.esc(r["symbol"]), F.esc(r["category"])))
                lines = ["🔎 %s" % name, "%s · <b>%d/100</b> %s" % (F.esc(r["label"]), r["score"], F.bar(r["score"] / 100.0)), ""]
                lines += ["• " + F.esc(x) for x in r["reasons"]]
                lines += ["", "💶 Prix : <b>%s</b>" % (F.price(i["price"]) if not r.get("currency") or r.get("currency") == "EUR"
                                                       else "%s %s" % (_fmt(i["price"]), F.esc(r["currency"]))),
                          "📅 30 jours : <b>%s</b> · 90 jours : <b>%s</b>" % (F.pct(i["r30"] * 100), F.pct(i["r90"] * 100)),
                          "⛰ Depuis son plus haut sur 1 an : %s" % F.pct(i["from_high"] * 100),
                          "🌊 Volatilité : %.0f %%/an" % (i["vol"] * 100)]
                if r["kind"] != "crypto":
                    lines.append("📱 Dans l'appli Revolut, cherche : <b>%s</b>" % F.esc(r["search"]))
                lines.append("Dans ton plan : %s" % ("oui ✅" if self._in_plan(r) else "non"))
                return "\n".join(lines), r
        return "🤔 %s n'est pas dans la dernière analyse du radar." % F.esc(symbol), None


def _fmt(x):
    if x >= 100:
        return ("%.2f" % x).replace(".", ",")
    if x >= 1:
        return ("%.3f" % x).replace(".", ",")
    return ("%.6f" % x).replace(".", ",")
