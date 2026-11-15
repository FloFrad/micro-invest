"""Cerveau de l'outil : stratégie DCA intelligente + garde-fous + planification."""
import logging
import os
import threading
import time

from . import config as config_mod
from .broker import (BrokerError, InsufficientFunds, MarketData, PaperBroker,
                     RevolutXBroker, normalize_fill)
from .store import Store

log = logging.getLogger("engine")

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def eur(x):
    return ("%.2f €" % x).replace(".", ",")


def pct(x):
    return ("%+.1f %%" % x).replace(".", ",")


def month_start(now=None):
    t = time.localtime(now or time.time())
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def month_key(now=None):
    return time.strftime("%Y-%m", time.localtime(now or time.time()))


class NullNotifier:
    def notify(self, text, buttons=None):
        log.info("[notification] %s", text.replace("\n", " | "))
        return None

    def edit(self, msg_id, text):
        pass


class Engine:
    def __init__(self, cfg, cfg_path, base_dir, market=None, broker=None, notifier=None, clock=time.time):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.base_dir = base_dir
        self.clock = clock
        self.lock = threading.RLock()
        self.notifier = notifier or NullNotifier()
        self.market = market or MarketData(cfg["quote"])
        self._prices = {}
        self._prices_ts = 0
        self._custom_broker = broker
        self._open(cfg["mode"])

    # ------------------------------------------------------------------ setup
    def _open(self, mode):
        os.makedirs(os.path.join(self.base_dir, "data"), exist_ok=True)
        self.store = Store(os.path.join(self.base_dir, "data", "%s.db" % mode), self.clock)
        if self._custom_broker is not None:
            self.broker = self._custom_broker
        elif mode == "live":
            rx = self.cfg["revolutx"]
            key_path = rx["private_key_file"]
            if not os.path.isabs(key_path):
                key_path = os.path.join(self.base_dir, key_path)
            if not os.path.exists(key_path):
                raise BrokerError("Clé privée introuvable : %s (lance : python3 main.py genkey)" % key_path)
            with open(key_path) as f:
                self.broker = RevolutXBroker(rx["api_key"], f.read(), self.market)
        else:
            self.broker = PaperBroker(self.market, self.store, self.cfg["paper_start_eur"])
        self.mode = mode

    def set_mode(self, mode):
        with self.lock:
            old = self.cfg["mode"]
            try:  # relit config.json (ex. clé API ajoutée pendant que l'outil tourne)
                self.reload_config()
            except config_mod.ConfigError as e:
                log.warning("config.json non relu : %s", e)
            self.cfg["mode"] = mode
            try:
                self._custom_broker = None
                self._open(mode)
            except Exception:
                self.cfg["mode"] = old
                self._open(old)
                raise
            raw = {}
            try:
                import json
                with open(self.cfg_path, encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, ValueError):
                pass
            raw["mode"] = mode
            config_mod.save(self.cfg_path, raw)
            self.log("info", "Mode changé : %s" % self.mode_label())

    def get_radar(self):
        if getattr(self, "radar", None) is None:
            from .radar import Radar
            self.radar = Radar(self, clock=self.clock)
        return self.radar

    def reload_config(self):
        """Relit config.json et applique les réglages sans redémarrer (le mode actuel est conservé)."""
        with self.lock:
            fresh = config_mod.load(self.cfg_path)
            fresh["mode"] = self.cfg["mode"]
            self.cfg.clear()
            self.cfg.update(fresh)
            self._prices_ts = 0

    def mode_label(self):
        return "RÉEL 💶" if self.mode == "live" else "SIMULATION 🧪"

    # ------------------------------------------------------------ utilitaires
    def log(self, level, text):
        getattr(log, "warning" if level == "warn" else level, log.info)(text)
        self.store.event(level, text)

    def notify(self, text, buttons=None):
        try:
            return self.notifier.notify(text, buttons)
        except Exception as e:  # une panne Telegram ne doit jamais bloquer le moteur
            log.warning("Notification impossible : %s", e)
            return None

    @property
    def assets(self):
        return list(self.cfg["dca"]["targets"].keys())

    def prices(self, max_age=60):
        if self.clock() - self._prices_ts > max_age or not self._prices:
            self._prices = self.market.tickers(self.assets)
            self._prices_ts = self.clock()
        return self._prices

    def flag(self, k, default=False):
        return self.store.get(k, default)

    # -------------------------------------------------------------- portefeuille
    def portfolio(self):
        pos = self.store.positions()
        try:
            prices = self.prices()
        except BrokerError as e:
            log.warning("Prix indisponibles : %s", e)
            prices = self._prices or {}
        targets = self.cfg["dca"]["targets"]
        tsum = float(sum(targets.values()))
        rows, value, cost, realized = [], 0.0, 0.0, 0.0
        for a in sorted(set(list(targets.keys()) + list(pos.keys()))):
            p = pos.get(a, {"qty": 0.0, "cost": 0.0, "avg": 0.0, "realized": 0.0})
            price = prices.get(a, {}).get("bid") or prices.get(a, {}).get("last") or 0.0
            v = p["qty"] * price
            value += v
            cost += p["cost"]
            realized += p["realized"]
            rows.append({
                "asset": a, "qty": p["qty"], "avg": p["avg"], "price": price, "value": v,
                "cost": p["cost"], "pnl_pct": (v / p["cost"] - 1) * 100 if p["cost"] > 0 else 0.0,
                "target_pct": targets.get(a, 0) / tsum * 100,
            })
        for r in rows:
            r["weight_pct"] = r["value"] / value * 100 if value > 0 else 0.0
        return {
            "rows": rows, "value": value, "cost": cost, "realized": realized,
            "pnl": value - cost, "pnl_pct": (value / cost - 1) * 100 if cost > 0 else 0.0,
        }

    def budget_status(self):
        b = self.cfg["budget"]
        ms = month_start(self.clock())
        spent = self.store.spent_since(ms, "crypto")
        etf = self.store.spent_since(ms, "etf")
        return {
            "monthly": b["monthly_crypto_eur"], "spent": spent,
            "remaining": max(0.0, b["monthly_crypto_eur"] - spent),
            "etf_monthly": b["monthly_etf_eur"], "etf_spent": etf,
            "weekly_base": b["monthly_crypto_eur"] * 12.0 / 52.0,
        }

    def dips(self):
        """Écart (en %) entre le prix actuel et la moyenne 30 jours, par actif."""
        out = {}
        prices = self.prices()
        for a in self.assets:
            try:
                closes = self.market.daily_closes(a, 30)
            except BrokerError as e:
                log.warning("Historique %s indisponible : %s", a, e)
                closes = []
            price = prices.get(a, {}).get("last")
            if closes and price:
                avg = sum(closes) / len(closes)
                out[a] = (avg - price) / avg * 100
        return out

    # ---------------------------------------------------------------- garde-fous
    def check_buy(self, asset, amount, automatic=True):
        """Retourne None si l'achat est autorisé, sinon la raison du refus.

        C'est ICI qu'est garanti « jamais en négatif » : aucun achat ne peut
        dépasser le solde disponible, le budget mensuel ou le plafond par ordre.
        """
        b = self.cfg["budget"]
        if automatic and self.flag("paused"):
            return "outil en pause (/reprendre pour relancer)"
        if self.flag("frozen") and not self.flag("freeze_override"):
            return "achats gelés : plafond de perte de -%s %% atteint" % self.cfg["safety"]["max_drawdown_pct"]
        if amount <= 0:
            return "montant nul"
        if amount > b["max_order_eur"] + 1e-9:
            return "montant %s supérieur au maximum par ordre (%s)" % (eur(amount), eur(b["max_order_eur"]))
        bs = self.budget_status()
        if amount > bs["remaining"] + 1e-9:
            return "budget du mois épuisé (reste %s sur %s)" % (eur(bs["remaining"]), eur(bs["monthly"]))
        try:
            info = self.market.pair_info(asset)
            mini = float(info.get("min_order_size_quote") or 0)
            if amount < mini:
                return "montant sous le minimum Revolut X (%s)" % eur(mini)
        except BrokerError as e:
            return str(e)
        try:
            avail = self.broker.balances().get(self.cfg["quote"], 0.0)
        except BrokerError as e:
            return "solde Revolut X illisible (%s)" % e
        if avail - b["keep_reserve_eur"] < amount - 1e-9:
            return "solde insuffisant sur Revolut X : %s disponibles (réserve %s). Alimente ton compte Revolut X." % (
                eur(avail), eur(b["keep_reserve_eur"]))
        return None

    # -------------------------------------------------------------------- achat
    def buy(self, asset, amount, reason, automatic=True):
        with self.lock:
            refus = self.check_buy(asset, amount, automatic)
            if refus:
                self.log("warn", "Achat %s %s refusé : %s" % (eur(amount), asset, refus))
                self.notify("⛔ Achat de %s de %s annulé\nRaison : %s" % (eur(amount), asset, refus))
                return None
            try:
                o = self.broker.market_order(asset, "buy", quote_eur=amount)
            except InsufficientFunds as e:
                self.notify("⛔ Revolut X refuse l'achat : solde insuffisant.\n%s" % e)
                self.log("warn", "Fonds insuffisants : %s" % e)
                return None
            except BrokerError as e:
                self.notify("⚠️ Échec de l'achat %s : %s" % (asset, e))
                self.log("error", "Échec achat %s : %s" % (asset, e))
                return None
            qty, spent = normalize_fill(o, asset, "buy", self.cfg["quote"])
            if qty <= 0:
                self.notify("⚠️ Ordre %s non exécuté (statut : %s)" % (asset, o["status"]))
                self.log("error", "Ordre non exécuté %s : %s" % (asset, o))
                return None
            self.store.add_trade(asset, "buy", qty, spent, o["price"], reason, o["id"])
            bs = self.budget_status()
            msg = "✅ Acheté %s de %s (%.8f %s à %s)\nMotif : %s\nBudget du mois : %s / %s" % (
                eur(spent), asset, qty, asset, eur(o["price"]), reason, eur(bs["spent"]), eur(bs["monthly"]))
            self.log("info", msg.replace("\n", " | "))
            self.notify(msg)
            return o

    # -------------------------------------------------------------------- vente
    def sell(self, asset, fraction_pct, reason):
        with self.lock:
            pos = self.store.positions().get(asset)
            if not pos or pos["qty"] <= 0:
                self.notify("⛔ Vente %s annulée : aucune position détenue par l'outil." % asset)
                return None
            try:
                price = self.prices(max_age=0).get(asset, {}).get("bid") or 0
                avail = self.broker.balances().get(asset, 0.0)
            except BrokerError as e:
                self.notify("⚠️ Vente %s impossible : %s" % (asset, e))
                return None
            if price < pos["avg"] * 1.005:
                self.notify("⛔ Vente %s annulée : le prix (%s) est repassé sous ton prix moyen (%s). "
                            "L'outil ne vend jamais à perte." % (asset, eur(price), eur(pos["avg"])))
                self.log("warn", "Vente %s refusée : prix sous le coût moyen" % asset)
                return None
            qty = min(pos["qty"] * fraction_pct / 100.0, avail)
            if qty <= 0:
                self.notify("⛔ Vente %s annulée : solde %s introuvable sur Revolut X." % (asset, asset))
                return None
            try:
                o = self.broker.market_order(asset, "sell", base_qty=qty)
            except BrokerError as e:
                self.notify("⚠️ Échec de la vente %s : %s" % (asset, e))
                self.log("error", "Échec vente %s : %s" % (asset, e))
                return None
            q, received = normalize_fill(o, asset, "sell", self.cfg["quote"])
            if q <= 0:
                self.notify("⚠️ Vente %s non exécutée (statut : %s)" % (asset, o["status"]))
                return None
            self.store.add_trade(asset, "sell", q, received, o["price"], reason, o["id"])
            gain = received - pos["avg"] * q
            msg = "💰 Vendu %.8f %s pour %s (gain ≈ %s)\nMotif : %s" % (q, asset, eur(received), eur(gain), reason)
            self.log("info", msg.replace("\n", " | "))
            self.notify(msg)
            return o

    # ------------------------------------------------------------- validations
    def request_approval(self, kind, payload, text, hours=None):
        hours = hours or self.cfg["approval_timeout_hours"]
        for a in self.store.pending_approvals():
            if a["kind"] == kind and a["payload"].get("asset") == payload.get("asset"):
                return a["id"]  # déjà une question en attente pour ça
        aid = self.store.add_approval(kind, payload, text, hours)
        if kind == "etf":
            amt = payload["amount"]
            buttons = [[("✅ Fait (%s)" % eur(amt), "ap:%d:y" % aid), ("⏭ Sauter ce mois", "ap:%d:n" % aid)]]
        else:
            buttons = [[("✅ Oui", "ap:%d:y" % aid), ("❌ Non", "ap:%d:n" % aid)]]
        msg_id = self.notify("❓ %s\n\n(expire dans %d h)" % (text, hours), buttons)
        if msg_id:
            self.store.set_approval(aid, tg_msg=msg_id)
        self.log("info", "Question posée #%d : %s" % (aid, text.split("\n")[0]))
        return aid

    def resolve_approval(self, aid, yes, source="telegram"):
        a = self.store.approval(aid)
        if not a:
            return "Demande introuvable."
        if not self.store.claim_approval(aid, "approved" if yes else "rejected"):
            return "Déjà traitée (%s)." % a["status"]
        p = a["payload"]
        if a["tg_msg"]:
            try:
                self.notifier.edit(a["tg_msg"], "%s\n\n➡️ %s (%s)" % (a["text"], "Accepté" if yes else "Refusé", source))
            except Exception:
                pass
        if not yes:
            if a["kind"] == "etf":
                self.log("info", "Achat ETF du mois sauté")
            return "Refusé : rien n'a été fait."
        if a["kind"] == "buy":
            o = self.buy(p["asset"], p["amount"], p["reason"], automatic=False)
            return "Achat exécuté." if o else "Achat refusé par les garde-fous (voir message)."
        if a["kind"] == "sell":
            o = self.sell(p["asset"], p["fraction_pct"], p["reason"])
            return "Vente exécutée." if o else "Vente annulée (voir message)."
        if a["kind"] == "etf":
            self.record_etf(p["amount"], p.get("lines"))
            return "Achat actions/ETF enregistré."
        return "OK"

    def expire_approvals(self):
        now = self.clock()
        for a in self.store.pending_approvals():
            if a["expires"] < now and self.store.claim_approval(a["id"], "expired"):
                if a["tg_msg"]:
                    try:
                        self.notifier.edit(a["tg_msg"], a["text"] + "\n\n⌛ Expiré sans réponse : rien n'a été fait.")
                    except Exception:
                        pass
                self.log("info", "Question #%d expirée" % a["id"])

    # ------------------------------------------------------------------- DCA
    def plan_dca(self):
        """Choisit l'actif et le montant du prochain achat. Retourne un dict."""
        d = self.cfg["dca"]
        bs = self.budget_status()
        pf = self.portfolio()
        targets = d["targets"]
        tsum = float(sum(targets.values()))
        base = bs["weekly_base"]
        try:
            dips = self.dips()
        except BrokerError:
            dips = {}
        values = {r["asset"]: r["value"] for r in pf["rows"]}
        total_after = pf["value"] + base
        deficits = {a: targets[a] / tsum * total_after - values.get(a, 0.0) for a in targets}
        # Achat "baisse" : l'actif le plus sous sa moyenne 30 j, s'il n'est pas déjà trop lourd
        dip_cands = [a for a in targets if dips.get(a, 0) >= d["dip_threshold_pct"]
                     and values.get(a, 0) <= targets[a] / tsum * total_after * 1.5]
        if dip_cands:
            asset = max(dip_cands, key=lambda a: dips[a])
            amount = base * d["dip_multiplier"]
            reason = "DCA hebdo renforcé : %s est %.1f %% sous sa moyenne 30 j" % (asset, dips[asset])
        else:
            asset = max(deficits, key=lambda a: deficits[a])
            amount = base
            reason = "DCA hebdo : %s est le plus en retard sur sa cible" % asset
        amount = round(min(amount, self.cfg["budget"]["max_order_eur"], bs["remaining"]), 2)
        return {"asset": asset, "amount": amount, "reason": reason, "dips": dips}

    def run_dca(self, manual=False):
        with self.lock:
            if self.flag("paused") and not manual:
                self.log("info", "DCA sauté : outil en pause")
                return "DCA sauté : outil en pause."
            plan = self.plan_dca()
            if plan["amount"] <= 0:
                self.notify("ℹ️ DCA de la semaine sauté : budget du mois déjà utilisé.")
                return "Budget du mois épuisé."
            if plan["amount"] > self.cfg["budget"]["approval_above_eur"]:
                self.request_approval("buy", {"asset": plan["asset"], "amount": plan["amount"], "reason": plan["reason"]},
                                      "Achat exceptionnel proposé : %s de %s\n%s" % (eur(plan["amount"]), plan["asset"], plan["reason"]))
                return "Achat exceptionnel : validation demandée sur Telegram."
            o = self.buy(plan["asset"], plan["amount"], plan["reason"], automatic=not manual)
            return "Achat exécuté." if o else "Achat refusé (voir message)."

    # -------------------------------------------------------- surveillances
    def check_drawdown(self):
        s = self.cfg["safety"]
        pf = self.portfolio()
        if pf["cost"] < 10:
            return
        p = pf["pnl_pct"]
        frozen, override = self.flag("frozen"), self.flag("freeze_override")
        if override and p >= -s["resume_above_pct"]:
            self.store.set("freeze_override", False)
            self.store.set("frozen", False)
        elif not frozen and not override and p <= -s["max_drawdown_pct"]:
            self.store.set("frozen", True)
            self.store.set("recovery_notified", False)
            self.log("warn", "Plafond de perte atteint (%s) : achats gelés" % pct(p))
            self.notify("🧊 Plafond de perte atteint : %s (limite -%s %%).\nTous les achats sont GELÉS. "
                        "Rien n'est vendu.\nValeur : %s pour %s investis." % (
                            pct(p), s["max_drawdown_pct"], eur(pf["value"]), eur(pf["cost"])),
                        [[("▶️ Reprendre quand même", "cmd:resume_freeze")]])
        elif frozen and not override and p >= -s["resume_above_pct"] and not self.flag("recovery_notified"):
            self.store.set("recovery_notified", True)
            self.notify("📈 Le portefeuille est remonté à %s. Reprendre les achats ?" % pct(p),
                        [[("▶️ Reprendre", "cmd:resume_freeze")]])

    def resume_freeze(self):
        self.store.set("frozen", False)
        self.store.set("freeze_override", True)
        self.log("info", "Gel des achats levé manuellement")
        return "Achats réactivés. (Le gel se réarmera quand le portefeuille sera remonté au-dessus de -%s %%.)" % (
            self.cfg["safety"]["resume_above_pct"])

    def check_take_profit(self):
        tp = self.cfg["take_profit"]
        if not tp["enabled"]:
            return
        last = self.store.get("tp_last", {})
        for r in self.portfolio()["rows"]:
            if r["qty"] <= 0 or r["avg"] <= 0 or r["value"] < tp["min_position_eur"]:
                continue
            if r["price"] < r["avg"] * (1 + tp["gain_pct"] / 100.0):
                continue
            if self.clock() - last.get(r["asset"], 0) < 7 * 86400:
                continue
            last[r["asset"]] = self.clock()
            self.store.set("tp_last", last)
            gain = (r["price"] / r["avg"] - 1) * 100
            self.request_approval("sell", {"asset": r["asset"], "fraction_pct": tp["sell_fraction_pct"],
                                           "reason": "prise de bénéfices (+%.0f %%)" % gain},
                                  "Prise de bénéfices : %s est à %s au-dessus de ton prix moyen.\n"
                                  "Vendre %s %% de la position (≈ %s) ?" % (
                                      r["asset"], pct(gain), tp["sell_fraction_pct"],
                                      eur(r["value"] * tp["sell_fraction_pct"] / 100.0)))

    def check_opportunity(self):
        d = self.cfg["dca"]
        if self.flag("paused") or (self.flag("frozen") and not self.flag("freeze_override")):
            return
        bs = self.budget_status()
        amount = round(min(bs["weekly_base"], bs["remaining"], self.cfg["budget"]["max_order_eur"]), 2)
        if amount <= 0:
            return
        last = self.store.get("opp_last", {})
        try:
            dips = self.dips()
        except BrokerError:
            return
        for a, dip in sorted(dips.items(), key=lambda kv: -kv[1]):
            if dip < d["opportunity_threshold_pct"] or self.clock() - last.get(a, 0) < 3 * 86400:
                continue
            last[a] = self.clock()
            self.store.set("opp_last", last)
            self.request_approval("buy", {"asset": a, "amount": amount, "reason": "opportunité : -%.1f %% vs moyenne 30 j" % dip},
                                  "Forte baisse : %s est %.1f %% sous sa moyenne 30 jours.\n"
                                  "Acheter %s en plus ? (pris sur le budget du mois : reste %s)" % (
                                      a, dip, eur(amount), eur(bs["remaining"])))
            break

    # ------------------------------------------------------------------- ETF
    # ------------------------------------------------------ plan actions/ETF
    def stock_targets(self):
        return dict(self.cfg["stocks"]["targets"])

    def stock_label(self, ticker):
        from .radar import STOCK_INFO
        i = STOCK_INFO.get(ticker)
        if i:
            return "%s (cherche « %s »)" % (i["name"], i["search"])
        return ticker

    def stock_invested(self):
        """Montant investi par ligne du plan actions (anciens achats « ETF » rattachés à la ligne principale)."""
        targets = self.stock_targets()
        main = max(targets, key=lambda k: targets[k]) if targets else "ETF"
        out = {}
        for t in self.store.trades(100000, kind="etf"):
            k = main if t["asset"] == "ETF" else t["asset"]
            out[k] = out.get(k, 0.0) + t["eur"]
        return out

    def stock_plan(self, budget=None):
        """Répartit le budget du mois entre les lignes les plus en retard sur leur cible.

        Au maximum `max_lines` lignes, chacune d'au moins `min_line_eur` (sinon trop de petits ordres)."""
        sc = self.cfg["stocks"]
        budget = self.cfg["budget"]["monthly_etf_eur"] if budget is None else budget
        targets = self.stock_targets()
        if budget <= 0 or not targets:
            return []
        inv = self.stock_invested()
        tsum = float(sum(targets.values()))
        total_after = sum(inv.get(k, 0.0) for k in targets) + budget
        deficits = sorted(((targets[k] / tsum * total_after - inv.get(k, 0.0), k) for k in targets), reverse=True)
        lines, left = [], budget
        for d, k in deficits:
            if len(lines) >= sc["max_lines"] or left < sc["min_line_eur"] - 1e-9:
                break
            amt = max(sc["min_line_eur"], min(d, left))
            amt = round(min(amt, left), 2)
            lines.append([k, amt])
            left = round(left - amt, 2)
        if left > 0 and lines:  # le reste va à la ligne la plus en retard
            lines[0][1] = round(lines[0][1] + left, 2)
        return lines

    def etf_prompt(self):
        lines = self.stock_plan()
        if not lines:
            return
        total = round(sum(a for _, a in lines), 2)
        self.store.set("etf_month", month_key(self.clock()))
        self.store.set("etf_reminded", False)
        detail = "\n".join("   • %s : %s" % (self.stock_label(k), eur(a)) for k, a in lines)
        self.request_approval("etf", {"asset": "ETF", "amount": total, "lines": lines},
                              "📈 Achats actions/ETF du mois (semi-auto) — total %s\n"
                              "1. Ouvre l'appli Revolut → Investir\n"
                              "2. Achète :\n%s\n"
                              "3. Reviens ici et appuie sur « Fait »\n"
                              "(Autre montant ? Envoie /etf 12.50)" % (eur(total), detail), hours=72)

    def record_etf(self, amount, lines=None):
        if not lines:
            targets = self.stock_targets()
            main = max(targets, key=lambda k: targets[k]) if targets else "ETF"
            lines = [[main, float(amount)]]
        for k, a in lines:
            self.store.add_trade(k, "buy", 0, float(a), 0, self.stock_label(k), kind="etf")
        amount = sum(a for _, a in lines)
        for a in self.store.pending_approvals():
            if a["kind"] == "etf":
                self.store.claim_approval(a["id"], "approved")
        bs = self.budget_status()
        warn = ""
        if bs["etf_spent"] > bs["etf_monthly"] + 1e-9:
            warn = "\n⚠️ Au-dessus du budget actions du mois (%s)." % eur(bs["etf_monthly"])
        self.log("info", "Achat actions/ETF enregistré : %s" % eur(amount))
        self.notify("✅ Achat actions/ETF de %s enregistré.%s\n\nQuestion : quelle est la valeur totale actuelle de tes "
                    "actions/ETF dans Revolut ? Réponds par exemple : /etfvaleur 123.45" % (eur(amount), warn))

    def set_etf_value(self, value):
        self.store.set("etf_value", {"value": float(value), "ts": self.clock()})
        invested = sum(t["eur"] for t in self.store.trades(10000, kind="etf"))
        p = (value / invested - 1) * 100 if invested > 0 else 0
        return "ETF : valeur %s pour %s investis (%s)." % (eur(value), eur(invested), pct(p))

    # ------------------------------------------------------------- planning
    def next_dca_ts(self):
        d = self.cfg["dca"]
        now = self.clock()
        t = time.localtime(now)
        days = (d["weekday"] - t.tm_wday) % 7
        cand = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + days, d["hour"], 0, 0, 0, 0, -1))
        if cand <= now and self.store.get("dca_week") == time.strftime("%G-%V", t):
            cand = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + days + 7, d["hour"], 0, 0, 0, 0, -1))
        return cand

    def tick(self):
        """Appelé toutes les 30 s par la boucle principale."""
        now = self.clock()
        t = time.localtime(now)
        self.expire_approvals()

        # Surveillance horaire
        if now - self.store.get("last_hourly", 0) >= 3600:
            self.store.set("last_hourly", now)
            try:
                pf = self.portfolio()
                if pf["cost"] > 0:
                    self.store.snapshot(pf["value"], pf["cost"])
                self.check_drawdown()
                self.check_take_profit()
                self.check_opportunity()
            except BrokerError as e:
                self.log("warn", "Surveillance horaire : %s" % e)

        # DCA hebdomadaire
        d = self.cfg["dca"]
        week = time.strftime("%G-%V", t)
        if (t.tm_wday == d["weekday"] and t.tm_hour >= d["hour"] and self.store.get("dca_week") != week
                and now >= self.store.get("dca_retry_at", 0)):
            try:
                self.prices(max_age=0)  # Revolut X joignable ? Sinon on réessaie plus tard
                self.store.set("dca_week", week)
                self.store.set("dca_retry_at", 0)
                self.run_dca()
            except BrokerError as e:
                self.store.set("dca_retry_at", now + 900)
                self.log("error", "DCA impossible (nouvel essai dans 15 min) : %s" % e)
                if not self.store.get("dca_fail_notified") == week:
                    self.store.set("dca_fail_notified", week)
                    self.notify("⚠️ Achat de la semaine retardé : Revolut X injoignable. Nouvel essai toutes les 15 min.\n%s" % e)

        # ETF mensuel (semi-auto)
        e = self.cfg["etf"]
        if e["enabled"] and self.cfg["budget"]["monthly_etf_eur"] > 0:
            if (t.tm_mday >= e["day_of_month"] and t.tm_hour >= e["hour"]
                    and self.store.get("etf_month") != month_key(now)):
                self.etf_prompt()
            elif not self.store.get("etf_reminded"):
                for a in self.store.pending_approvals():
                    if a["kind"] == "etf" and now - a["ts"] > 24 * 3600:
                        self.store.set("etf_reminded", True)
                        self.notify("⏰ Rappel : l'achat ETF du mois attend toujours (message ci-dessus).")

        # Radar quotidien (en arrière-plan : 2 à 4 min)
        r = self.cfg["radar"]
        day = time.strftime("%Y-%m-%d", t)
        if r["enabled"] and t.tm_hour >= r["hour"] and self.store.get("radar_day") != day:
            self.store.set("radar_day", day)
            self.get_radar().run_async(notify=False)

        # Rapport hebdomadaire
        w = self.cfg["weekly_report"]
        if t.tm_wday == w["weekday"] and t.tm_hour >= w["hour"] and self.store.get("report_week") != week:
            self.store.set("report_week", week)
            self.notify("🗓 Rapport de la semaine\n\n" + self.status_text())
            rd = self.get_radar().latest()
            if r["enabled"] and rd:
                self.notify(self.get_radar().summary_text(rd), self.get_radar().summary_buttons(rd))

    # ---------------------------------------------------------------- textes
    def status_text(self):
        pf = self.portfolio()
        bs = self.budget_status()
        if self.flag("frozen") and not self.flag("freeze_override"):
            etat = "🧊 GELÉ (plafond de perte)"
        elif self.flag("paused"):
            etat = "⏸ EN PAUSE"
        else:
            etat = "▶️ ACTIF"
        nd = time.localtime(self.next_dca_ts())
        lines = [
            "Mode : %s — État : %s" % (self.mode_label(), etat),
            "Crypto : %s pour %s investis (%s)" % (eur(pf["value"]), eur(pf["cost"]), pct(pf["pnl_pct"])),
            "Budget crypto du mois : %s / %s" % (eur(bs["spent"]), eur(bs["monthly"])),
            "Budget ETF du mois : %s / %s" % (eur(bs["etf_spent"]), eur(bs["etf_monthly"])),
            "Prochain DCA : %s %s à %dh" % (JOURS[nd.tm_wday], time.strftime("%d/%m", nd), nd.tm_hour),
        ]
        try:
            lines.append("Solde %s dispo sur Revolut X : %s" % (self.cfg["quote"], eur(self.broker.balances().get(self.cfg["quote"], 0))))
        except BrokerError as e:
            lines.append("Solde Revolut X illisible : %s" % e)
        n = len(self.store.pending_approvals())
        if n:
            lines.append("❓ %d question(s) en attente" % n)
        return "\n".join(lines)

    def portfolio_text(self):
        pf = self.portfolio()
        lines = ["Actif | Valeur | P/L | Poids / cible"]
        for r in pf["rows"]:
            if r["qty"] <= 0 and r["target_pct"] <= 0:
                continue
            lines.append("%s | %s | %s | %.0f %% / %.0f %%" % (
                r["asset"], eur(r["value"]), pct(r["pnl_pct"]) if r["cost"] else "—", r["weight_pct"], r["target_pct"]))
        lines.append("Total : %s (%s)" % (eur(pf["value"]), pct(pf["pnl_pct"])))
        inv = self.stock_invested()
        if inv:
            lines.append("\nActions/ETF (achats manuels) :")
            for k, v in sorted(inv.items(), key=lambda kv: -kv[1]):
                lines.append("%s | %s investis" % (k, eur(v)))
        ev = self.store.get("etf_value")
        if ev:
            lines.append("Valeur actions/ETF déclarée : %s" % eur(ev["value"]))
        return "\n".join(lines)

    def history_text(self, n=10):
        rows = self.store.trades(n)
        if not rows:
            return "Aucune opération pour l'instant."
        out = []
        for t in rows:
            d = time.strftime("%d/%m %H:%M", time.localtime(t["ts"]))
            if t["kind"] == "etf":
                out.append("%s ETF achat %s" % (d, eur(t["eur"])))
            else:
                out.append("%s %s %s %s (%s)" % (d, "Achat" if t["side"] == "buy" else "Vente", t["asset"], eur(t["eur"]), t["reason"][:40]))
        return "\n".join(out)

    def _radar_state(self):
        from .radar import key_reason
        rd = self.store.get("radar")
        if not rd:
            return None
        slim = lambda rows: [{"symbol": r["symbol"], "name": r["name"], "score": r["score"], "label": r["label"],
                              "reason": key_reason(r), "kind": r["kind"],
                              "r90": r["ind"]["r90"], "vol": r["ind"]["vol"]} for r in rows[:12]]
        return {"ts": rd["ts"], "crypto": slim(rd["crypto"]), "stocks": slim(rd["stocks"]),
                "n_crypto": len(rd["crypto"]), "n_stocks": len(rd["stocks"])}

    def state(self):
        """Toutes les données pour l'interface web."""
        pf = self.portfolio()
        try:
            bal = self.broker.balances()
        except BrokerError as e:
            bal = {"erreur": str(e)}
        return {
            "mode": self.mode, "mode_label": self.mode_label(),
            "paused": bool(self.flag("paused")),
            "frozen": bool(self.flag("frozen")) and not self.flag("freeze_override"),
            "portfolio": pf, "budget": self.budget_status(), "balances": bal,
            "approvals": [{"id": a["id"], "kind": a["kind"], "text": a["text"], "expires": a["expires"]}
                          for a in self.store.pending_approvals()],
            "trades": self.store.trades(30),
            "events": self.store.events(20),
            "snapshots": self.store.snapshots(self.clock() - 90 * 86400),
            "next_dca": self.next_dca_ts(),
            "etf": {"name": ", ".join(self.stock_targets()), "value": self.store.get("etf_value"),
                    "invested": sum(t["eur"] for t in self.store.trades(10000, kind="etf")),
                    "targets": self.stock_targets(), "by_line": self.stock_invested()},
            "radar": self._radar_state(),
            "config": {
                "budget": self.cfg["budget"], "targets": self.cfg["dca"]["targets"],
                "dca_day": JOURS[self.cfg["dca"]["weekday"]], "dca_hour": self.cfg["dca"]["hour"],
                "max_drawdown_pct": self.cfg["safety"]["max_drawdown_pct"],
                "take_profit": self.cfg["take_profit"],
            },
        }
