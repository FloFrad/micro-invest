"""Cerveau de l'outil : stratégie DCA intelligente + garde-fous + planification."""
import logging
import os
import threading
import time

from . import config as config_mod
from . import fmt as F
from .broker import (BrokerError, InsufficientFunds, MarketData, PaperBroker,
                     RevolutXBroker, normalize_fill)
from .store import Store

log = logging.getLogger("engine")

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def eur(x):
    return F.eur(x)


def pct(x):
    return F.pct(x)


def hint_for(refus):
    """Conseil « que faire » pour une raison de refus."""
    r = refus.lower()
    if "solde insuffisant" in r:
        return "dépose des euros sur Revolut X (exchange.revolut.com → Déposer)."
    if "budget du mois" in r:
        return "rien : les achats reprendront le 1er du mois. (Ou /regler budget_crypto …)"
    if "gelés" in r:
        return "attendre, ou bouton « ▶️ Reprendre » si tu veux racheter quand même."
    if "pause" in r:
        return "bouton « ▶️ Reprendre »."
    if "minimum" in r:
        return "augmente le montant (/regler budget_crypto …)."
    return "rien de spécial : je réessaierai au prochain créneau."


def month_start(now=None):
    t = time.localtime(now or time.time())
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def month_key(now=None):
    return time.strftime("%Y-%m", time.localtime(now or time.time()))


class NullNotifier:
    def notify(self, text, buttons=None):
        log.info("[notification] %s", text.replace("\n", " | "))
        return None

    def send_photo(self, png, caption="", buttons=None):
        log.info("[image] %s", caption.replace("\n", " | "))

    def send_document(self, filename, content, caption="", mime=""):
        log.info("[fichier] %s (%d octets)", filename, len(content))

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

    def send_photo(self, png, caption, buttons=None):
        """Envoie une image ; si ça échoue, envoie au moins le texte."""
        try:
            return self.notifier.send_photo(png, caption, buttons)
        except Exception as e:
            log.warning("Image non envoyée : %s", e)
            return self.notify(caption, buttons)

    def send_document(self, filename, content, caption="", mime="application/octet-stream"):
        try:
            return self.notifier.send_document(filename, content, caption, mime)
        except Exception as e:
            log.warning("Fichier non envoyé : %s", e)
            self.notify("⚠️ Envoi du fichier %s impossible : %s" % (F.esc(filename), F.esc(e)))
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
                self.notify("⛔ <b>Achat annulé</b> — %s de %s\n<b>Pourquoi</b> : %s\n➡️ <b>Que faire</b> : %s" % (
                    eur(amount), F.esc(asset), F.esc(refus), F.esc(hint_for(refus))))
                return None
            try:
                o = self.broker.market_order(asset, "buy", quote_eur=amount)
            except InsufficientFunds as e:
                self.notify("⛔ <b>Achat refusé par Revolut X</b> : solde insuffisant.\n➡️ <b>Que faire</b> : dépose des euros "
                            "sur Revolut X.\n<i>%s</i>" % F.esc(e))
                self.log("warn", "Fonds insuffisants : %s" % e)
                return None
            except BrokerError as e:
                self.notify("⚠️ <b>Échec de l'achat %s</b>\n<i>%s</i>\n➡️ Rien n'a été dépensé. Je réessaierai au prochain créneau." % (
                    F.esc(asset), F.esc(e)))
                self.log("error", "Échec achat %s : %s" % (asset, e))
                return None
            qty, spent = normalize_fill(o, asset, "buy", self.cfg["quote"])
            if qty <= 0:
                self.notify("⚠️ <b>Ordre %s non exécuté</b> (statut : %s). Rien n'a été dépensé." % (F.esc(asset), F.esc(o["status"])))
                self.log("error", "Ordre non exécuté %s : %s" % (asset, o))
                return None
            self.store.add_trade(asset, "buy", qty, spent, o["price"], reason, o["id"])
            bs = self.budget_status()
            tag = " · 🧪 simulation" if self.mode == "paper" else ""
            msg = ("✅ <b>Achat effectué</b>%s\n<b>%s de %s</b>\n%s %s à %s\n💡 %s\n\n💶 Budget de %s\n%s %s / %s" % (
                tag, eur(spent), F.esc(asset), F.num(qty, 8), F.esc(asset), F.price(o["price"]), F.esc(reason),
                F.month_name(self.clock()), F.bar(bs["spent"] / bs["monthly"] if bs["monthly"] else 1),
                eur(bs["spent"]), eur(bs["monthly"])))
            self.log("info", "Acheté %s de %s (%s)" % (eur(spent), asset, reason))
            self.notify(msg)
            return o

    # -------------------------------------------------------------------- vente
    def sell(self, asset, fraction_pct, reason):
        with self.lock:
            pos = self.store.positions().get(asset)
            if not pos or pos["qty"] <= 0:
                self.notify("⛔ <b>Vente %s annulée</b> : aucune position détenue par l'outil." % F.esc(asset))
                return None
            try:
                price = self.prices(max_age=0).get(asset, {}).get("bid") or 0
                avail = self.broker.balances().get(asset, 0.0)
            except BrokerError as e:
                self.notify("⚠️ <b>Vente %s impossible</b>\n<i>%s</i>" % (F.esc(asset), F.esc(e)))
                return None
            if price < pos["avg"] * 1.005:
                self.notify("⛔ <b>Vente %s annulée</b>\nLe prix (%s) est repassé sous ton prix moyen (%s).\n"
                            "🛡 L'outil ne vend jamais à perte." % (F.esc(asset), F.price(price), F.price(pos["avg"])))
                self.log("warn", "Vente %s refusée : prix sous le coût moyen" % asset)
                return None
            qty = min(pos["qty"] * fraction_pct / 100.0, avail)
            if qty <= 0:
                self.notify("⛔ <b>Vente %s annulée</b> : solde %s introuvable sur Revolut X." % (F.esc(asset), F.esc(asset)))
                return None
            try:
                o = self.broker.market_order(asset, "sell", base_qty=qty)
            except BrokerError as e:
                self.notify("⚠️ <b>Échec de la vente %s</b>\n<i>%s</i>" % (F.esc(asset), F.esc(e)))
                self.log("error", "Échec vente %s : %s" % (asset, e))
                return None
            q, received = normalize_fill(o, asset, "sell", self.cfg["quote"])
            if q <= 0:
                self.notify("⚠️ <b>Vente %s non exécutée</b> (statut : %s)" % (F.esc(asset), F.esc(o["status"])))
                return None
            try:
                global_value = sum(r["value"] for r in self.portfolio()["rows"])  # valeur globale avant la cession
            except Exception:
                global_value = None
            self.store.add_trade(asset, "sell", q, received, o["price"], reason, o["id"], portfolio_value=global_value)
            gain = received - pos["avg"] * q
            tag = " · 🧪 simulation" if self.mode == "paper" else ""
            msg = "💰 <b>Vente effectuée</b>%s\n%s %s → <b>%s</b>\nGain encaissé ≈ <b>%s</b>\n💡 %s" % (
                tag, F.num(q, 8), F.esc(asset), eur(received), F.signed_eur(gain), F.esc(reason))
            self.log("info", "Vendu %s %s pour %s (gain %s)" % (q, asset, eur(received), eur(gain)))
            self.notify(msg)
            return o

    # ------------------------------------------------------------- validations
    def request_approval(self, kind, payload, text, hours=None):
        """Pose une question avec boutons. `text` est du HTML Telegram (valeurs déjà échappées)."""
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
        footer = "\n\n⏳ <i>Sans réponse d'ici %s, rien ne sera fait.</i>" % F.when(self.clock() + hours * 3600, self.clock())
        msg_id = self.notify(text + footer, buttons)
        if msg_id:
            self.store.set_approval(aid, tg_msg=msg_id)
        self.log("info", "Question posée #%d : %s" % (aid, F.strip_tags(text.split("\n")[0])))
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
                self.notifier.edit(a["tg_msg"], "%s\n\n➡️ <b>%s</b> (via %s)" % (a["text"], "Accepté" if yes else "Refusé", source))
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
                        self.notifier.edit(a["tg_msg"], a["text"] + "\n\n⌛ <i>Expiré sans réponse : rien n'a été fait.</i>")
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
            reason = "DCA hebdo renforcé : %s est %s %% sous sa moyenne 30 j" % (asset, F.num(dips[asset], 1))
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
                self.notify("ℹ️ <b>Pas d'achat cette semaine</b> : le budget crypto de %s est déjà utilisé.\n"
                            "Les achats reprennent le 1er." % F.month_name(self.clock()))
                return "Budget du mois épuisé."
            if plan["amount"] > self.cfg["budget"]["approval_above_eur"]:
                self.request_approval("buy", {"asset": plan["asset"], "amount": plan["amount"], "reason": plan["reason"]},
                                      "🛒 <b>Achat renforcé proposé</b>\n<b>%s de %s</b>\n💡 %s\n\n"
                                      "<i>C'est plus que l'achat habituel (≈ %s), donc je te demande.</i>" % (
                                          eur(plan["amount"]), F.esc(plan["asset"]), F.esc(plan["reason"]),
                                          eur(self.budget_status()["weekly_base"])))
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
            self.notify("🧊 <b>Achats gelés</b> : plafond de perte atteint\nPortefeuille crypto : <b>%s</b> (limite −%s %%)\n"
                        "Valeur %s pour %s investis.\n\n🛡 Rien n'est vendu. Les achats reprendront quand tu le décideras, "
                        "ou je te préviendrai quand le portefeuille sera remonté." % (
                            pct(p), s["max_drawdown_pct"], eur(pf["value"]), eur(pf["cost"])),
                        [[("▶️ Reprendre quand même", "cmd:resume_freeze")]])
        elif frozen and not override and p >= -s["resume_above_pct"] and not self.flag("recovery_notified"):
            self.store.set("recovery_notified", True)
            self.notify("📈 <b>Le portefeuille est remonté</b> à %s.\nReprendre les achats automatiques ?" % pct(p),
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
                                  "💰 <b>Prise de bénéfices ?</b>\n%s est à <b>%s</b> au-dessus de ton prix moyen (%s).\n"
                                  "Vendre %s %% de la position, soit ≈ <b>%s</b> ?\n"
                                  "<i>🛡 Je ne vends jamais sous ton prix moyen.</i>" % (
                                      F.esc(r["asset"]), pct(gain), F.price(r["avg"]), tp["sell_fraction_pct"],
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
            self.request_approval("buy", {"asset": a, "amount": amount, "reason": "opportunité : −%s %% vs moyenne 30 j" % F.num(dip, 1)},
                                  "📉 <b>Forte baisse sur %s</b>\n%s sous sa moyenne des 30 derniers jours.\n"
                                  "Acheter <b>%s</b> en plus ?\n<i>Pris sur le budget du mois (reste %s).</i>" % (
                                      F.esc(a), F.pct(-dip), eur(amount), eur(bs["remaining"])))
            break

    # ------------------------------------------------------------------- ETF
    # ------------------------------------------------------ plan actions/ETF
    def stock_targets(self):
        return dict(self.cfg["stocks"]["targets"])

    def stock_label(self, ticker, html=False):
        from .radar import STOCK_INFO
        i = STOCK_INFO.get(ticker)
        if i:
            if html:
                return "<b>%s</b> (cherche « %s »)" % (F.esc(i["name"]), F.esc(i["search"]))
            return "%s (cherche « %s »)" % (i["name"], i["search"])
        return F.esc(ticker) if html else ticker

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
        detail = "\n".join("• %s : <b>%s</b>" % (self.stock_label(k, html=True), eur(a)) for k, a in lines)
        self.request_approval("etf", {"asset": "ETF", "amount": total, "lines": lines},
                              "📈 <b>Achats actions/ETF de %s</b> — total %s\n\n"
                              "Dans l'appli Revolut → <b>Investir</b>, achète :\n%s\n\n"
                              "Puis appuie sur « ✅ Fait ».\n<i>Autre montant : /etf 12.50</i>" % (
                                  F.month_name(self.clock()), eur(total), detail), hours=72)

    def resolve_stock(self, sym):
        """Code d'une ligne actions/ETF à partir de « NVDA », « iwda », « IWDA.AS »… (ligne du plan ou liste suivie)."""
        from .radar import STOCK_INFO
        sym = (sym or "").strip().upper()
        if not sym:
            return None
        for pool in (list(self.stock_targets()), list(STOCK_INFO)):
            for k in pool:
                if k.upper() == sym or k.split(".")[0].upper() == sym:
                    return k
        return None

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
        self.notify("✅ <b>Achat actions/ETF enregistré</b> : %s%s\n\n❓ Quelle est la valeur totale actuelle de tes "
                    "actions/ETF dans l'appli Revolut ?\nRéponds par exemple : <code>/etfvaleur 123.45</code>" % (eur(amount), warn))

    def set_etf_value(self, value):
        self.store.set("etf_value", {"value": float(value), "ts": self.clock()})
        invested = sum(t["eur"] for t in self.store.trades(10000, kind="etf"))
        p = (value / invested - 1) * 100 if invested > 0 else 0
        return "📈 <b>Actions/ETF</b> : valeur %s pour %s investis (<b>%s</b>)." % (eur(value), eur(invested), pct(p))

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
                self.check_low_balance()
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
                    self.notify("⚠️ <b>Achat de la semaine retardé</b> : Revolut X injoignable.\n"
                                "➡️ Rien à faire : je réessaie toutes les 15 min.\n<i>%s</i>" % F.esc(e))

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
                        self.notify("⏰ <b>Rappel</b> : les achats actions/ETF du mois attendent toujours.\n"
                                    "Bouton « ❓ Questions » pour les retrouver.")

        day = time.strftime("%Y-%m-%d", t)
        # Message du matin (preuve que l'outil tourne)
        dl = self.cfg["daily"]
        if dl["enabled"] and t.tm_hour >= dl["hour"] and self.store.get("daily_day") != day:
            self.store.set("daily_day", day)
            try:
                self.notify(self.daily_text())
            except BrokerError as e:
                self.notify("☀️ <b>Bonjour</b> — Micro-invest tourne ✅\n⚠️ Prix indisponibles : <i>%s</i>" % F.esc(e))

        # Sauvegarde de nuit
        bk = self.cfg["backup"]
        if bk["enabled"] and t.tm_hour >= bk["hour"] and self.store.get("backup_day") != day:
            self.store.set("backup_day", day)
            self.do_backup()

        # Réconciliation quotidienne avec Revolut X
        rc = self.cfg["reconcile"]
        if rc["enabled"] and t.tm_hour >= rc["hour"] and self.store.get("reconcile_day") != day:
            self.store.set("reconcile_day", day)
            try:
                self.reconcile(notify_only_if_problem=True)
            except BrokerError as e:
                self.log("warn", "Réconciliation impossible : %s" % e)

        # Alertes de prix (toutes les 5 minutes)
        if self.store.get("price_alerts") and now - self.store.get("alerts_checked", 0) >= 300:
            self.store.set("alerts_checked", now)
            try:
                self.check_price_alerts()
            except BrokerError as e:
                log.info("Alertes de prix : %s", e)

        # Radar quotidien (en arrière-plan : 2 à 4 min)
        r = self.cfg["radar"]
        if r["enabled"] and t.tm_hour >= r["hour"] and self.store.get("radar_day") != day:
            self.store.set("radar_day", day)
            self.get_radar().run_async(notify=False)

        # Rapport hebdomadaire
        w = self.cfg["weekly_report"]
        if t.tm_wday == w["weekday"] and t.tm_hour >= w["hour"] and self.store.get("report_week") != week:
            self.store.set("report_week", week)
            report = self.weekly_report_text()
            png = self.portfolio_chart_png(90)
            if png and len(F.strip_tags(report)) <= 1000:
                self.send_photo(png, report)
            else:
                if png:
                    self.send_photo(png, "📉 <b>Ton portefeuille sur 90 jours</b>")
                self.notify(report)
            rd = self.get_radar().latest()
            if r["enabled"] and rd:
                self.notify(self.get_radar().summary_text(rd), self.get_radar().summary_buttons(rd))

    # ------------------------------------------------------ fiabilité (1-3)
    def daily_text(self):
        now = self.clock()
        t = time.localtime(now)
        pf = self.portfolio()
        lines = ["☀️ <b>Bonjour</b> — Micro-invest tourne ✅%s" % (" · 🧪 simulation" if self.mode == "paper" else "")]
        if pf["cost"] > 0:
            lines.append("💼 Crypto : <b>%s</b> (%s)" % (eur(pf["value"]), pct(pf["pnl_pct"])))
        today = []
        nd = self.next_dca_ts()
        if time.strftime("%Y-%m-%d", time.localtime(nd)) == time.strftime("%Y-%m-%d", t):
            today.append("🛒 achat crypto à %d h (≈ %s)" % (time.localtime(nd).tm_hour, eur(self.budget_status()["weekly_base"])))
        ne = self.next_etf_ts()
        if time.strftime("%Y-%m-%d", time.localtime(ne)) == time.strftime("%Y-%m-%d", t) and self.cfg["budget"]["monthly_etf_eur"] > 0:
            today.append("📈 rappel actions/ETF à %d h" % time.localtime(ne).tm_hour)
        lines.append("🗓 Aujourd'hui : " + (" · ".join(today) if today else "rien de prévu"))
        g = self.goal_status()
        if g:
            lines.append("🎯 Objectif : %s %.0f %%" % (F.bar(g["progress"]), g["progress"] * 100))
        n = len(self.store.pending_approvals())
        if n:
            lines.append("❓ <b>%d question%s en attente</b> → « ❓ Questions »" % (n, "s" if n > 1 else ""))
        if self.flag("paused"):
            lines.append("⏸️ Achats en pause")
        return "\n".join(lines)

    def check_low_balance(self):
        bs = self.budget_status()
        weeks = self.cfg["safety"]["low_balance_weeks"]
        need = bs["weekly_base"] * weeks
        if need <= 0 or bs["remaining"] <= 0 or self.flag("paused"):
            return
        try:
            avail = self.broker.balances().get(self.cfg["quote"], 0.0) - self.cfg["budget"]["keep_reserve_eur"]
        except BrokerError:
            return
        if avail >= need or self.clock() - self.store.get("low_balance_last", 0) < 3 * 86400:
            return
        self.store.set("low_balance_last", self.clock())
        weeks_left = max(0, int(avail // bs["weekly_base"])) if bs["weekly_base"] else 0
        self.log("warn", "Solde bas : %s" % eur(avail))
        self.notify("🏦 <b>Solde bas sur Revolut X</b>%s\n%s disponibles = <b>%d semaine%s</b> d'achats.\n"
                    "➡️ <b>Que faire</b> : dépose au moins %s sur Revolut X (exchange.revolut.com → Déposer)." % (
                        " (simulation)" if self.mode == "paper" else "", eur(max(avail, 0)), weeks_left,
                        "s" if weeks_left > 1 else "", eur(max(bs["monthly"], need))))

    def do_backup(self):
        from . import backup
        try:
            path, size = backup.save_local(self.base_dir, self.clock())
            self.log("info", "Sauvegarde : %s (%d Ko)" % (os.path.basename(path), size // 1024))
            return path
        except Exception as e:
            self.log("error", "Sauvegarde impossible : %s" % e)
            self.notify("⚠️ <b>Sauvegarde de nuit impossible</b>\n<i>%s</i>" % F.esc(e))
            return None

    def reconcile_data(self):
        """Compare les quantités suivies par l'outil avec les avoirs réels sur Revolut X."""
        pos = self.store.positions()
        hold = self.broker.holdings()
        rows = []
        for a, p in sorted(pos.items()):
            if p["qty"] <= 1e-12:
                continue
            ex = hold.get(a, 0.0)
            missing = p["qty"] - ex
            ok = missing <= max(p["qty"] * 0.005, 1e-10)
            rows.append({"asset": a, "bot": p["qty"], "exchange": ex, "missing": max(0.0, missing), "ok": ok,
                         "avg": p["avg"], "extra": max(0.0, ex - p["qty"])})
        return rows, hold.get(self.cfg["quote"], 0.0)

    def reconcile(self, notify_only_if_problem=False):
        rows, eur_bal = self.reconcile_data()
        bad = [r for r in rows if not r["ok"]]
        self.store.set("reconcile_last", {"ts": self.clock(), "ok": not bad, "issues": [r["asset"] for r in bad]})
        if bad:
            self.log("warn", "Écart avec Revolut X : %s" % ", ".join(r["asset"] for r in bad))
        if notify_only_if_problem and not bad:
            return None
        table = [["Actif", "Outil", "Revolut X", ""]] + [
            [r["asset"], F.num(r["bot"], 6).replace(F.NBSP, " "), F.num(r["exchange"], 6).replace(F.NBSP, " "), "OK" if r["ok"] else "ÉCART"]
            for r in rows]
        if bad:
            text = ("🔍 <b>Écart avec Revolut X</b>\nL'outil pense détenir plus que ce qu'il y a réellement sur Revolut X "
                    "(vente ou retrait manuel ?).\n%s\n➡️ <b>Que faire</b> : si c'est toi qui as vendu ou retiré, aligne l'outil "
                    "sur Revolut X. Sinon, vérifie ton compte Revolut X." % F.pre(F.table(table)))
            buttons = [[("✔️ Aligner l'outil sur Revolut X", "cmd:reconcile_fix")]]
        else:
            text = "🔍 <b>Revolut X : tout correspond</b> ✅\n%s\nSolde EUR : %s" % (
                F.pre(F.table(table)) if rows else "(aucune position suivie)", eur(eur_bal))
            buttons = None
        if notify_only_if_problem:
            self.notify(text, buttons)
        return text, buttons

    def reconcile_fix(self):
        rows, _ = self.reconcile_data()
        fixed = []
        for r in rows:
            if not r["ok"]:
                self.store.add_trade(r["asset"], "sell", r["missing"], r["avg"] * r["missing"], r["avg"],
                                     "ajustement (écart Revolut X)", "adjust")
                fixed.append("%s −%s" % (r["asset"], F.num(r["missing"], 8)))
        if not fixed:
            return "Rien à aligner : tout correspond déjà."
        self.log("info", "Positions alignées sur Revolut X : %s" % ", ".join(fixed))
        return "✔️ <b>Outil aligné sur Revolut X</b> : %s" % F.esc(", ".join(fixed))

    # ----------------------------------------------------- alertes de prix (6)
    def price_alerts(self):
        return list(self.store.get("price_alerts", []))

    def add_price_alert(self, asset, op, price):
        asset = asset.upper()
        self.market.pair_info(asset)  # lève une erreur si la crypto n'existe pas sur Revolut X
        alerts = self.price_alerts()
        if len(alerts) >= 20:
            raise ValueError("20 alertes maximum")
        aid = max([a["id"] for a in alerts] + [0]) + 1
        alerts.append({"id": aid, "asset": asset, "op": op, "price": float(price), "ts": self.clock()})
        self.store.set("price_alerts", alerts)
        return aid

    def remove_price_alert(self, aid):
        alerts = [a for a in self.price_alerts() if a["id"] != aid]
        self.store.set("price_alerts", alerts)

    def check_price_alerts(self):
        alerts = self.price_alerts()
        if not alerts:
            return
        prices = self.market.tickers(sorted(set(a["asset"] for a in alerts)))
        keep = []
        for a in alerts:
            last = prices.get(a["asset"], {}).get("last")
            hit = last and ((a["op"] == "<" and last <= a["price"]) or (a["op"] == ">" and last >= a["price"]))
            if not hit:
                keep.append(a)
                continue
            self.log("info", "Alerte prix : %s %s %s" % (a["asset"], a["op"], a["price"]))
            self.notify("🔔 <b>Alerte prix</b> : %s est passé %s %s\nPrix actuel : <b>%s</b>" % (
                F.esc(a["asset"]), "sous" if a["op"] == "<" else "au-dessus de", F.price(a["price"]), F.price(last)),
                [[("🔁 Réactiver", "cmd:alert_re:%s:%s:%s" % (a["asset"], "lt" if a["op"] == "<" else "gt", a["price"])),
                  ("🔎 Analyse", "m:/info %s" % a["asset"])]])
        self.store.set("price_alerts", keep)

    # --------------------------------------------------------- objectif (8)
    def goal_status(self):
        g = self.cfg.get("goal") or {}
        if not g.get("amount"):
            return None
        pf = self.portfolio()
        ev = self.store.get("etf_value")
        stocks = ev["value"] if ev else sum(self.stock_invested().values())
        current = pf["value"] + stocks
        out = {"amount": float(g["amount"]), "date": g.get("date", ""), "current": current,
               "progress": max(0.0, min(1.0, current / g["amount"]))}
        if g.get("date"):
            y, m = int(g["date"][:4]), int(g["date"][5:7])
            t = time.localtime(self.clock())
            months = (y - t.tm_year) * 12 + (m - t.tm_mon)
            monthly = self.cfg["budget"]["monthly_crypto_eur"] + self.cfg["budget"]["monthly_etf_eur"]
            out.update(months=max(0, months), monthly=monthly, projected=current + monthly * max(0, months),
                       needed=((g["amount"] - current) / months) if months > 0 else None)
        return out

    def goal_text(self):
        g = self.goal_status()
        if not g:
            return ("🎯 <b>Aucun objectif d'épargne</b>\nExemple : <code>/objectif 1000 2027-06</code> "
                    "(1 000 € d'ici juin 2027).")
        lines = ["🎯 <b>Objectif : %s</b>%s" % (eur(g["amount"]), (" d'ici %s %s" % (F.MOIS[int(g["date"][5:7]) - 1], g["date"][:4]))
                                                if g["date"] else ""),
                 "%s <b>%s</b> (%.0f %%)" % (F.bar(g["progress"]), eur(g["current"]), g["progress"] * 100)]
        if g["progress"] >= 1:
            lines.append("🎉 Objectif atteint !")
        elif g.get("months") is not None:
            if g["months"] <= 0:
                lines.append("⌛ La date est passée. Change-la : <code>/objectif %d 2027-12</code>" % g["amount"])
            elif g["projected"] >= g["amount"]:
                lines.append("✅ En bonne voie : à %s/mois, tu auras versé de quoi l'atteindre (≈ %s, hors gains et pertes)." % (
                    eur(g["monthly"], ), eur(g["projected"])))
            else:
                lines.append("📏 À %s/mois tu arriverais à ≈ %s (hors gains et pertes).\nIl faudrait ≈ <b>%s/mois</b> pour l'atteindre." % (
                    eur(g["monthly"]), eur(g["projected"]), eur(g["needed"])))
        lines.append("<i>Changer : /objectif 1500 2027-12 · Supprimer : /objectif 0</i>")
        return "\n".join(lines)

    def set_goal(self, amount, date=""):
        config_mod.update_file(self.cfg_path, "goal", {"amount": float(amount), "date": date})
        self.reload_config()

    # --------------------------------------------------------- graphique (7)
    def portfolio_chart_png(self, days=90):
        from . import png
        snaps = self.store.snapshots(self.clock() - days * 86400 if days else 0)
        if len(snaps) < 2:
            return None
        lab = lambda ts: time.strftime("%d/%m", time.localtime(ts))
        return png.line_chart([
            {"label": "Valeur", "color": "accent", "points": [(x["ts"], x["value"]) for x in snaps]},
            {"label": "Investi", "color": "series2", "points": [(x["ts"], x["cost"]) for x in snaps], "dash": True}],
            title="Portefeuille crypto - %s" % ("%d jours" % days if days else "depuis le debut"),
            x_labels=[lab(snaps[0]["ts"]), lab(snaps[-1]["ts"])])

    # ------------------------------------------------ gestion de la simulation
    def reset_paper(self, amount):
        """Repart de zéro en simulation (jamais en réel)."""
        if self.mode != "paper":
            raise ValueError("réservé au mode simulation")
        amount = float(amount)
        if not 0 <= amount <= 1000000:
            raise ValueError("solde entre 0 et 1 000 000 €")
        with self.lock:
            for table in ("trades", "approvals", "snapshots"):
                self.store.x("DELETE FROM %s" % table)
            for k, v in (("frozen", False), ("freeze_override", False), ("recovery_notified", False), ("tp_last", {}),
                         ("opp_last", {}), ("low_balance_last", 0), ("reconcile_last", None), ("etf_value", None),
                         ("etf_month", None), ("dca_week", None)):
                self.store.set(k, v)
            self.store.set("paper_balances", {self.cfg["quote"]: amount})
        self.log("info", "Simulation remise à zéro avec %s" % eur(amount))
        return "🔄 <b>Simulation remise à zéro</b> : %s fictifs, aucune opération.\nProchain achat : %s." % (
            eur(amount), F.when(self.next_dca_ts(), self.clock()))

    def set_paper_balance(self, amount):
        if self.mode != "paper":
            raise ValueError("réservé au mode simulation")
        amount = float(amount)
        if not 0 <= amount <= 1000000:
            raise ValueError("solde entre 0 et 1 000 000 €")
        b = self.broker.balances()
        b[self.cfg["quote"]] = amount
        self.store.set("paper_balances", b)
        self.log("info", "Solde simulé fixé à %s" % eur(amount))
        return "💶 Solde simulé : <b>%s</b> (tes positions simulées ne changent pas)." % eur(amount)

    def trade(self, tid):
        r = self.store.q("SELECT * FROM trades WHERE id=?", (tid,))
        return r[0] if r else None

    def can_undo(self, t):
        if t["kind"] == "etf":
            return True, ""
        if self.mode != "paper":
            return False, "en mode réel, une opération crypto a vraiment eu lieu sur Revolut X : elle ne peut pas être effacée"
        return True, ""

    def undo_trade(self, tid):
        """Annule une opération : achat/vente crypto simulé (soldes fictifs rétablis) ou déclaration d'achat d'actions."""
        with self.lock:
            t = self.trade(tid)
            if not t:
                raise ValueError("opération introuvable")
            ok, why = self.can_undo(t)
            if not ok:
                raise ValueError(why)
            if t["kind"] == "crypto":
                b = self.broker.balances()
                q, quote = self.cfg["quote"], t["asset"]
                if t["side"] == "buy":
                    if b.get(quote, 0.0) < t["qty"] - 1e-12:
                        raise ValueError("une partie de ce %s a déjà été vendue : annule d'abord la vente" % quote)
                    b[quote] = b.get(quote, 0.0) - t["qty"]
                    b[q] = b.get(q, 0.0) + t["eur"]
                else:
                    b[quote] = b.get(quote, 0.0) + t["qty"]
                    b[q] = b.get(q, 0.0) - t["eur"]
                self.store.set("paper_balances", b)
            self.store.x("DELETE FROM trades WHERE id=?", (tid,))
        what = "%s %s %s" % ("Achat" if t["side"] == "buy" else "Vente", t["asset"], eur(t["eur"]))
        self.log("info", "Opération annulée : %s" % what)
        return "↩️ <b>Opération annulée</b> : %s%s." % (F.esc(what), " (soldes simulés rétablis)" if t["kind"] == "crypto" else "")

    def simulation_text(self):
        if self.mode != "paper":
            return "🧪 Tu es en mode <b>réel</b> : la gestion de la simulation n'est disponible qu'en mode simulation."
        b = self.broker.balances()
        n = len(self.store.trades(100000, kind="crypto"))
        pf = self.portfolio()
        return ("🧪 <b>Simulation</b>\nSolde fictif : <b>%s</b> · crypto simulée : %s · %d opération%s\n\n"
                "<i>Remettre à zéro efface les opérations, l'historique et les questions de la simulation (jamais celles du réel).</i>\n"
                "Commandes : <code>/simulation reset 100</code> · <code>/simulation solde 200</code> · "
                "annuler une opération : bouton 🗑 de l'historique (web) ou <code>/annuler N°</code>" % (
                    eur(b.get(self.cfg["quote"], 0.0)), eur(pf["value"]), n, "s" if n > 1 else ""))

    # ------------------------------------------------- test sur 12 mois (5)
    def run_backtest(self, params=None):
        """Test sur le passé. `params` (facultatif) : réglages à essayer sans toucher à la configuration."""
        from . import backtest, png
        p = backtest.check_params(params) if params else backtest.params_from_cfg(self.cfg)
        res = backtest.run(self.market, self.cfg, params=p)
        n0 = res["series"][0]["i"] if res["series"] else 0
        total_days = res["days"]
        ts_of = lambda i: self.clock() - (total_days - (i - n0)) * 86400
        lab = lambda i: time.strftime("%d/%m", time.localtime(ts_of(i)))
        ser = res["series"]
        image = png.line_chart([
            {"label": "DCA intelligent", "color": "accent", "points": [(x["i"], x["smart"]) for x in ser]},
            {"label": "DCA simple", "color": "orange", "points": [(x["i"], x["simple"]) for x in ser]},
            {"label": "Investi", "color": "series2", "points": [(x["i"], x["inv_simple"]) for x in ser], "dash": True}],
            title="Test sur %d mois (prix reels)" % p["months"], x_labels=[lab(ser[0]["i"]), lab(ser[-1]["i"])] if ser else None)
        try:
            with open(os.path.join(self.base_dir, "data", "backtest.png"), "wb") as f:
                f.write(image)
        except OSError:
            pass
        summary = {k: res[k] for k in ("days", "assets", "dip_buys", "weeks", "smart", "simple")}
        summary["ts"] = self.clock()
        summary["id"] = max([h.get("id", 0) for h in self.store.get("backtests", [])] + [self.store.get("backtest_seq", 0)]) + 1
        self.store.set("backtest_seq", summary["id"])
        summary["params"] = p
        summary["current"] = p == backtest.params_from_cfg(self.cfg, p["months"])
        self.store.set("backtest", summary)
        # un même jeu de réglages n'apparaît qu'une fois (le test le plus récent remplace l'ancien)
        hist = [h for h in self.store.get("backtests", []) if h.get("params") != p]
        self.store.set("backtests", ([summary] + hist)[:8])
        self.log("info", "Test sur %d mois : intelligent %s, simple %s" % (
            p["months"], pct(res["smart"]["gain_pct"] * 100), pct(res["simple"]["gain_pct"] * 100)))
        return summary, image

    def backtest_params_text(self, p):
        return "%s/mois · max %s/ordre · baisse %s %% × %s · %s" % (
            eur(p["budget"]), eur(p["max_order"]), F.num(p["dip"], 0), F.num(p["mult"], 1).replace(",0", ""),
            " ".join("%s %g" % kv for kv in p["targets"].items()))

    def find_backtest(self, bid):
        for h in self.store.get("backtests", []):
            if h.get("id") == bid:
                return h
        return None

    def apply_backtest_params(self, bid):
        h = self.find_backtest(bid)
        if not h:
            raise ValueError("test introuvable (relance-le)")
        p = h["params"]
        for key, val in (("budget.monthly_crypto_eur", p["budget"]), ("budget.max_order_eur", p["max_order"]),
                         ("dca.dip_threshold_pct", p["dip"]), ("dca.dip_multiplier", p["mult"]), ("dca.targets", p["targets"])):
            config_mod.update_file(self.cfg_path, key, val)
        if p["max_order"] < self.cfg["budget"]["approval_above_eur"]:
            config_mod.update_file(self.cfg_path, "budget.approval_above_eur", p["max_order"])
        self.reload_config()
        self.log("info", "Réglages du test appliqués : %s" % self.backtest_params_text(p))
        return p

    def backtests_text(self):
        hist = self.store.get("backtests", [])
        if not hist:
            return "🧪 Aucun test pour l'instant. Lance <code>/test</code>."
        rows = [["#", "Mois", "Budget", "Baisse", "Intellig.", "Simple"]]
        for k, h in enumerate(hist, 1):
            p = h["params"]
            rows.append([str(k), str(p["months"]), F.teur(p["budget"], 0), "%g%%x%g" % (p["dip"], p["mult"]),
                         F.tpct(h["smart"]["gain_pct"] * 100), F.tpct(h["simple"]["gain_pct"] * 100)])
        return "🧪 <b>Derniers tests</b> (gain final, hors prises de bénéfices)\n%s\n<i>« Intellig. » = stratégie de l'outil avec ces réglages.</i>" % (
            F.pre(F.table(rows)))

    def backtest_text(self, r):
        sm, si = r["smart"], r["simple"]
        diff = sm["value"] / sm["invested"] - si["value"] / si["invested"] if sm["invested"] and si["invested"] else 0
        verdict = ("✅ Sur cette période, la stratégie de l'outil a fait <b>mieux</b> que l'achat fixe." if diff > 0.005 else
                   "➖ Sur cette période, la stratégie de l'outil a fait <b>moins bien</b> que l'achat fixe." if diff < -0.005 else
                   "➖ Sur cette période, les deux méthodes font <b>à peu près pareil</b>.")
        return ("🧪 <b>Test sur %d jours</b> (vrais prix Revolut X, %s)\n<i>Réglages : %s%s</i>\n\n"
                "🧠 <b>DCA intelligent</b> (l'outil)\n   investi %s → <b>%s</b> (%s)\n   pire moment : %s · %d achats renforcés\n"
                "📏 <b>DCA simple</b> (montant fixe réparti)\n   investi %s → <b>%s</b> (%s)\n   pire moment : %s\n\n%s\n"
                "<i>Prises de bénéfices non simulées. Les performances passées ne préjugent pas des performances futures.</i>" % (
                    r["days"], F.esc(", ".join(r["assets"])), F.esc(self.backtest_params_text(r["params"])) if r.get("params") else "actuels",
                    " (tes réglages actuels)" if r.get("current") else "",
                    eur(sm["invested"]), eur(sm["value"]), pct(sm["gain_pct"] * 100), pct(sm["worst_pct"] * 100), r["dip_buys"],
                    eur(si["invested"]), eur(si["value"]), pct(si["gain_pct"] * 100), pct(si["worst_pct"] * 100), verdict))

    # ---------------------------------------------------------------- textes
    def status_text(self):
        pf = self.portfolio()
        bs = self.budget_status()
        if self.flag("frozen") and not self.flag("freeze_override"):
            etat = "🧊 Gelé (plafond de perte)"
        elif self.flag("paused"):
            etat = "⏸️ En pause"
        else:
            etat = "▶️ Actif"
        mode = "🧪 Simulation" if self.mode == "paper" else "💶 Réel"
        lines = ["📊 <b>Statut</b> · %s · %s" % (mode, etat), ""]
        if pf["cost"] > 0:
            lines.append("💼 <b>Crypto</b> : <b>%s</b> (%s)" % (eur(pf["value"]), pct(pf["pnl_pct"])))
            lines.append("      investi %s · %s %s" % (eur(pf["cost"]), "gain" if pf["pnl"] >= 0 else "perte",
                                                     F.signed_eur(pf["pnl"])))
        else:
            lines.append("💼 <b>Crypto</b> : pas encore d'achat")
        inv = sum(self.stock_invested().values())
        ev = self.store.get("etf_value")
        if inv or ev:
            lines.append("📈 <b>Actions/ETF</b> : %s investis%s" % (eur(inv), (" · valeur déclarée %s" % eur(ev["value"])) if ev else ""))
        m = F.month_name(self.clock())
        lines += ["", "💶 <b>Budget de %s</b>" % m,
                  "<code>Crypto  </code> %s %s / %s" % (F.bar(bs["spent"] / bs["monthly"] if bs["monthly"] else 0),
                                                       eur(bs["spent"]), eur(bs["monthly"])),
                  "<code>Actions </code> %s %s / %s" % (F.bar(bs["etf_spent"] / bs["etf_monthly"] if bs["etf_monthly"] else 0),
                                                       eur(bs["etf_spent"]), eur(bs["etf_monthly"])), ""]
        g = self.goal_status()
        if g:
            lines.append("🎯 <b>Objectif</b> %s : %s %s (%.0f %%)" % (eur(g["amount"]), F.bar(g["progress"]), eur(g["current"]),
                                                                 g["progress"] * 100))
        lines.append("🗓 <b>Prochain achat</b> : %s (≈ %s)" % (F.when(self.next_dca_ts(), self.clock()), eur(bs["weekly_base"])))
        try:
            lines.append("🏦 <b>Solde Revolut X</b> : %s" % eur(self.broker.balances().get(self.cfg["quote"], 0)))
        except BrokerError as e:
            lines.append("🏦 Solde Revolut X illisible : <i>%s</i>" % F.esc(e))
        n = len(self.store.pending_approvals())
        if n:
            lines.append("\n❓ <b>%d question%s en attente</b> → bouton « ❓ Questions »" % (n, "s" if n > 1 else ""))
        return "\n".join(lines)

    def portfolio_text(self):
        pf = self.portfolio()
        rows = [["Actif", "Valeur", "Gain", "Poids/cible"]]
        for r in pf["rows"]:
            if r["qty"] <= 0 and r["target_pct"] <= 0:
                continue
            rows.append([r["asset"], F.teur(r["value"]), F.tpct(r["pnl_pct"]) if r["cost"] else "—",
                         "%.0f/%.0f%%" % (r["weight_pct"], r["target_pct"])])
        lines = ["💼 <b>Portefeuille crypto</b> — <b>%s</b>%s" % (
            eur(pf["value"]), (" (%s)" % pct(pf["pnl_pct"])) if pf["cost"] else "")]
        lines.append(F.pre(F.table(rows)))
        if pf["realized"]:
            lines.append("Gains déjà encaissés : <b>%s</b>" % F.signed_eur(pf["realized"]))
        inv = self.stock_invested()
        if inv:
            srows = [[k.split(".")[0], F.teur(v)] for k, v in sorted(inv.items(), key=lambda kv: -kv[1])]
            lines.append("\n📈 <b>Actions/ETF</b> (achats manuels, montants investis)")
            lines.append(F.pre(F.table(srows)))
        ev = self.store.get("etf_value")
        if ev:
            lines.append("Valeur déclarée : <b>%s</b> (le %s)" % (eur(ev["value"]), time.strftime("%d/%m", time.localtime(ev["ts"]))))
        return "\n".join(lines)

    def history_text(self, n=12):
        rows = self.store.trades(n)
        if not rows:
            return "📜 Aucune opération pour l'instant."
        table = []
        for t in rows:
            d = time.strftime("%d/%m %Hh%M", time.localtime(t["ts"]))
            if t["kind"] == "etf":
                table.append(["#%d" % t["id"], d, "Action", t["asset"].split(".")[0][:6], F.teur(t["eur"])])
            else:
                table.append(["#%d" % t["id"], d, "Achat" if t["side"] == "buy" else "Vente", t["asset"][:6], F.teur(t["eur"])])
        return "📜 <b>Dernières opérations</b>%s\n%s%s" % (" · 🧪 simulation" if self.mode == "paper" else "",
                                                         F.pre(F.table(table, ["<", "<", "<", "<", ">"])),
                                                         "<i>Annuler une opération simulée : /annuler N°</i>" if self.mode == "paper" else "")

    def weekly_report_text(self):
        now = self.clock()
        pf = self.portfolio()
        week_ago = now - 7 * 86400
        snaps = self.store.snapshots(week_ago - 3600)
        lines = ["🗓 <b>Ta semaine</b> (%s → %s)" % (time.strftime("%d/%m", time.localtime(week_ago)),
                                                   time.strftime("%d/%m", time.localtime(now))), ""]
        if pf["cost"] > 0:
            lines.append("💼 Crypto : <b>%s</b> (%s depuis le début)" % (eur(pf["value"]), pct(pf["pnl_pct"])))
            if snaps:
                first = snaps[0]
                # variation hors nouveaux achats : (valeur - investi) maintenant vs il y a 7 jours
                delta = (pf["value"] - pf["cost"]) - (first["value"] - first["cost"])
                lines.append("      cette semaine : <b>%s</b> (hors achats)" % F.signed_eur(delta))
        buys = [t for t in self.store.trades(200) if t["ts"] >= week_ago]
        if buys:
            spent = sum(t["eur"] for t in buys if t["side"] == "buy")
            lines.append("🛒 %d opération%s · %s investis (%s)" % (
                len(buys), "s" if len(buys) > 1 else "", eur(spent),
                ", ".join(sorted(set(t["asset"].split(".")[0] for t in buys)))))
        else:
            lines.append("🛒 Aucune opération cette semaine")
        moves = []
        for a in self.assets:
            try:
                c = self.market.daily_closes(a, 30)
                if len(c) >= 8 and c[-8]:
                    moves.append((c[-1] / c[-8] - 1, a))
            except BrokerError:
                pass
        if moves:
            moves.sort()
            best, worst = moves[-1], moves[0]
            lines.append("🏆 Meilleure : <b>%s</b> %s · 📉 Moins bonne : <b>%s</b> %s (sur 7 jours)" % (
                best[1], pct(best[0] * 100), worst[1], pct(worst[0] * 100)))
        bs = self.budget_status()
        lines += ["", "💶 Budget de %s : %s %s / %s" % (F.month_name(now), F.bar(bs["spent"] / bs["monthly"] if bs["monthly"] else 0),
                                                      eur(bs["spent"]), eur(bs["monthly"])),
                  "🗓 Prochain achat : %s" % F.when(self.next_dca_ts(), now)]
        n = len(self.store.pending_approvals())
        if n:
            lines.append("❓ %d question%s en attente" % (n, "s" if n > 1 else ""))
        return "\n".join(lines)

    def _radar_state(self):
        from .radar import key_reason
        rd = self.store.get("radar")
        if not rd:
            return None

        def slim(rows):
            return [{"symbol": r["symbol"], "name": r["name"], "score": r["score"], "label": r["label"],
                     "reason": key_reason(r), "reasons": r["reasons"], "kind": r["kind"],
                     "category": r.get("category", "Crypto"), "search": r.get("search", ""),
                     "r30": r["ind"]["r30"], "r90": r["ind"]["r90"], "from_high": r["ind"]["from_high"],
                     "vol": r["ind"]["vol"]} for r in rows[:40]]
        return {"ts": rd["ts"], "crypto": slim(rd["crypto"]), "stocks": slim(rd["stocks"]),
                "n_crypto": len(rd["crypto"]), "n_stocks": len(rd["stocks"])}

    def next_etf_ts(self):
        """Date du prochain rappel actions/ETF."""
        e = self.cfg["etf"]
        now = self.clock()
        t = time.localtime(now)
        if self.store.get("etf_month") == month_key(now):  # déjà envoyé ce mois-ci → mois prochain
            return time.mktime((t.tm_year, t.tm_mon + 1, e["day_of_month"], e["hour"], 0, 0, 0, 0, -1))
        cand = time.mktime((t.tm_year, t.tm_mon, e["day_of_month"], e["hour"], 0, 0, 0, 0, -1))
        return max(cand, now)  # date passée mais rappel pas encore envoyé : il part tout de suite

    def version_info(self):
        try:
            import json
            with open(os.path.join(self.base_dir, "data", "version.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _settings_state(self):
        from .telegram_bot import SETTINGS, JOURS as JRS, _get
        out = []
        for name, (key, conv, lo, hi, desc) in SETTINGS.items():
            v = _get(self.cfg, key)
            kind = "bool" if isinstance(v, bool) else ("day" if key == "dca.weekday" else ("text" if conv is str else "number"))
            out.append({"name": name, "value": (JRS[v] if kind == "day" else v), "desc": desc, "kind": kind, "min": lo, "max": hi})
        return out

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
            "approvals": [{"id": a["id"], "kind": a["kind"], "text": F.strip_tags(a["text"]), "expires": a["expires"]}
                          for a in self.store.pending_approvals()],
            "trades": self.store.trades(30),
            "n_crypto_trades": len(self.store.trades(1000000, kind="crypto")),
            "events": self.store.events(20),
            "snapshots": self.store.snapshots(self.clock() - 90 * 86400),
            "next_dca": self.next_dca_ts(),
            "etf": {"name": ", ".join(self.stock_targets()), "value": self.store.get("etf_value"),
                    "invested": sum(t["eur"] for t in self.store.trades(10000, kind="etf")),
                    "targets": self.stock_targets(), "by_line": self.stock_invested()},
            "radar": self._radar_state(),
            "next_etf": self.next_etf_ts(),
            "goal": self.goal_status(),
            "alerts": self.price_alerts(),
            "backtest": self.store.get("backtest"),
            "backtests": self.store.get("backtests", []),
            "lab_defaults": __import__("microinvest.backtest", fromlist=["params_from_cfg"]).params_from_cfg(self.cfg),
            "settings": self._settings_state(),
            "reconcile": self.store.get("reconcile_last"),
            "backups": __import__("microinvest.backup", fromlist=["list_local"]).list_local(self.base_dir)[:5],
            "version": self.version_info(),
            "stock_plan": self.stock_plan(),
            "stock_labels": {k: self.stock_label(k) for k in self.stock_targets()},
            "stock_universe": [{"ticker": t, "name": n, "search": q} for t, n, c, q in __import__("microinvest.radar", fromlist=["STOCKS"]).STOCKS],
            "config": {
                "budget": self.cfg["budget"], "targets": self.cfg["dca"]["targets"],
                "dca_day": JOURS[self.cfg["dca"]["weekday"]], "dca_hour": self.cfg["dca"]["hour"],
                "max_drawdown_pct": self.cfg["safety"]["max_drawdown_pct"],
                "take_profit": self.cfg["take_profit"],
                "dip_threshold_pct": self.cfg["dca"]["dip_threshold_pct"],
                "etf_day": self.cfg["etf"]["day_of_month"], "radar_hour": self.cfg["radar"]["hour"],
                "weekly_base": self.budget_status()["weekly_base"],
            },
        }
