"""Bot Telegram : pilotage complet, alertes, questions, réglages et mises à jour."""
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request

from . import config as config_mod
from . import ed25519_pure
from . import fmt as F

log = logging.getLogger("telegram")

# Clavier permanent en bas de l'écran
KEYBOARD = [["📊 Statut", "💼 Portefeuille"], ["🛒 Acheter", "❓ Questions"], ["⏸️ Pause", "▶️ Reprendre"], ["⚙️ Menu"]]
KEY_TO_CMD = {"📊 Statut": "/statut", "💼 Portefeuille": "/portefeuille", "🛒 Acheter": "/acheter",
              "❓ Questions": "/questions", "⏸ Pause": "/pause", "⏸️ Pause": "/pause", "▶️ Reprendre": "/reprendre", "⚙️ Menu": "/menu"}

MENU = [
    [("🔭 Radar (conseils)", "m:/radar"), ("📈 Plan actions/ETF", "m:/actions")],
    [("📜 Historique", "m:/historique"), ("📒 Journal", "m:/journal")],
    [("🧾 Déclarer achat actions", "m:/etf"), ("💶 Budget", "m:/budget")],
    [("🔧 Réglages", "m:/reglages"), ("🔁 Mode simu/réel", "m:/mode")],
    [("⬆️ Mise à jour", "m:/maj"), ("↩️ Version précédente", "m:/retour")],
    [("🔄 Redémarrer", "m:/redemarrer"), ("🌐 Interface web", "m:/web")],
    [("🔑 Clé Revolut X", "m:/cle"), ("❔ Aide", "m:/aide")],
]

BOT_COMMANDS = [
    ("menu", "Tous les boutons"), ("statut", "État, budget, prochain achat"), ("portefeuille", "Positions et gains"),
    ("acheter", "Achat de la semaine maintenant"), ("questions", "Questions en attente"), ("pause", "Suspendre les achats"),
    ("reprendre", "Relancer les achats"), ("historique", "Dernières opérations"), ("journal", "Journal des événements"),
    ("radar", "Conseils : cryptos et actions les mieux notées"), ("actions", "Plan actions/ETF"),
    ("info", "Détail d'un actif (ex. /info SOL)"), ("etf", "Déclarer un achat actions/ETF"), ("reglages", "Voir / modifier les réglages"), ("maj", "Mettre à jour l'outil"),
    ("version", "Version installée"), ("mode", "Simulation ou réel"), ("aide", "Aide"),
]

AIDE = """🤖 <b>Micro-invest — aide</b>

Les boutons en bas de l'écran font l'essentiel. « ⚙️ Menu » donne accès à tout le reste.

<b>Commandes avec une valeur</b>
<code>/etf 15</code> — déclarer 15 € d'achats actions/ETF
<code>/etfvaleur 230.50</code> — valeur actuelle de tes actions/ETF
<code>/regler budget_crypto 40</code> — changer un réglage (liste : /reglages)
<code>/cible SOL 12</code> — poids d'une crypto (0 = la retirer)
<code>/cible_action NVDA 20</code> — poids d'une action/ETF (0 = la retirer)
<code>/info SOL</code> — analyse détaillée d'un actif
<code>/radar crypto</code> · <code>/radar actions</code> · <code>/radar maj</code> — classements, nouvelle analyse
<code>/cleapi TA_CLE</code> — clé API Revolut X (message effacé)
<code>/depot proprietaire/depot</code> — dépôt GitHub des mises à jour
<code>/mode reel</code> · <code>/mode simu</code> — changer de mode"""

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _parse_bool(v):
    v = v.lower()
    if v in ("oui", "on", "1", "true", "vrai", "actif", "active"):
        return True
    if v in ("non", "off", "0", "false", "faux", "inactif", "desactive", "désactivé"):
        return False
    raise ValueError("réponds oui ou non")


def _parse_day(v):
    v = v.lower()
    if v in JOURS:
        return JOURS.index(v)
    n = int(v)
    if not 0 <= n <= 6:
        raise ValueError("0 = lundi … 6 = dimanche")
    return n


# nom court -> (clé config, conversion, min, max, description)
SETTINGS = {
    "budget_crypto": ("budget.monthly_crypto_eur", float, 0, 1000, "Budget crypto par mois (€)"),
    "budget_etf": ("budget.monthly_etf_eur", float, 0, 1000, "Budget actions/ETF par mois (€)"),
    "max_ordre": ("budget.max_order_eur", float, 1, 1000, "Montant maximum d'un ordre (€)"),
    "validation_au_dela": ("budget.approval_above_eur", float, 0, 1000, "Au-delà, je te demande ton accord (€)"),
    "reserve": ("budget.keep_reserve_eur", float, 0, 100000, "Euros jamais utilisés sur Revolut X"),
    "jour_achat": ("dca.weekday", _parse_day, 0, 6, "Jour de l'achat hebdo (lundi … dimanche)"),
    "heure_achat": ("dca.hour", int, 0, 23, "Heure de l'achat hebdo"),
    "seuil_baisse": ("dca.dip_threshold_pct", float, 1, 90, "Baisse (%) qui déclenche un achat renforcé"),
    "plafond_perte": ("safety.max_drawdown_pct", float, 1, 90, "Perte (%) qui gèle les achats"),
    "prise_benefices": ("take_profit.gain_pct", float, 5, 1000, "Gain (%) qui déclenche une proposition de vente"),
    "prise_benefices_active": ("take_profit.enabled", _parse_bool, None, None, "Propositions de vente (oui/non)"),
    "jour_etf": ("etf.day_of_month", int, 1, 28, "Jour du mois du rappel actions/ETF"),
    "radar_heure": ("radar.hour", int, 0, 23, "Heure de l'analyse quotidienne du radar"),
}


class TelegramError(Exception):
    pass


def api_call(token, method, params=None, timeout=20):
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read().decode())
        except ValueError:
            raise TelegramError("HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise TelegramError("Connexion à Telegram impossible : %s" % e.reason)
    if not res.get("ok"):
        raise TelegramError(res.get("description", "erreur inconnue"))
    return res.get("result")


def _inline(buttons):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}


def _get(cfg, dotted):
    node = cfg
    for k in dotted.split("."):
        node = node[k]
    return node


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # aucun paquet n'est envoyé
        return s.getsockname()[0]
    except OSError:
        return "IP_DU_NAS"
    finally:
        s.close()


class TelegramBot:
    def __init__(self, token, chat_id, engine=None, api=api_call, updater=None, restart=None, base_dir=None):
        self.token = token
        self.chat_id = str(chat_id)
        self.engine = engine
        self.api = api
        self.updater = updater
        self.restart = restart or (lambda: None)
        self.base_dir = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.offset = None
        self.stop = threading.Event()
        self._pending_update = None
        self._busy = threading.Lock()

    # ------------------------------------------------------- notifier (moteur)
    def _send_html(self, method, params):
        """Envoie en HTML ; si Telegram refuse la mise en forme, renvoie le même message en texte simple."""
        params = dict(params, parse_mode="HTML")
        try:
            return self.api(self.token, method, params)
        except TelegramError as e:
            if "parse" not in str(e).lower() and "entit" not in str(e).lower():
                raise
            log.warning("Mise en forme refusée par Telegram (%s) : envoi en texte simple", e)
            params.pop("parse_mode")
            params["text"] = F.strip_tags(params["text"])
            return self.api(self.token, method, params)

    def notify(self, text, buttons=None, keyboard=False):
        params = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = _inline(buttons)
        elif keyboard:
            params["reply_markup"] = {"keyboard": [[{"text": t} for t in row] for row in KEYBOARD],
                                      "resize_keyboard": True, "is_persistent": True}
        res = self._send_html("sendMessage", params)
        return res.get("message_id") if isinstance(res, dict) else None

    def edit(self, msg_id, text):
        self._send_html("editMessageText", {"chat_id": self.chat_id, "message_id": msg_id, "text": text[:4000]})

    def register_commands(self):
        try:
            self.api(self.token, "setMyCommands", {"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]})
        except TelegramError as e:
            log.warning("Menu des commandes non enregistré : %s", e)

    def send(self, reply):
        if not reply:
            return
        if isinstance(reply, tuple):
            self.notify(reply[0], reply[1])
        elif isinstance(reply, dict):
            self.notify(reply["text"], keyboard=reply.get("keyboard", False))
        else:
            self.notify(reply)

    # ---------------------------------------------------------------- boucle
    def run_forever(self):
        delay = 5
        while not self.stop.is_set():
            try:
                params = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
                if self.offset is not None:
                    params["offset"] = self.offset
                updates = self.api(self.token, "getUpdates", params, timeout=65)
                delay = 5
                for u in updates or []:
                    self.offset = u["update_id"] + 1
                    try:
                        self.handle(u)
                    except Exception as e:
                        log.exception("Erreur en traitant un message Telegram")
                        try:
                            self.notify("⚠️ Erreur interne : %s" % F.esc(e))
                        except Exception:
                            pass
            except TelegramError as e:
                log.warning("Telegram : %s (nouvel essai dans %d s)", e, delay)
                self.stop.wait(delay)
                delay = min(delay * 2, 300)
            except Exception:
                log.exception("Erreur inattendue Telegram")
                self.stop.wait(delay)

    def handle(self, u):
        if "callback_query" in u:
            cq = u["callback_query"]
            chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
            if chat != self.chat_id:
                return
            data = cq.get("data", "")
            if data.startswith("ap:"):  # réponse à une question : petite bulle de confirmation
                answer = self.on_button(data)
                try:
                    self.api(self.token, "answerCallbackQuery", {"callback_query_id": cq["id"], "text": (answer or "")[:190]})
                except TelegramError:
                    pass
                return
            try:
                self.api(self.token, "answerCallbackQuery", {"callback_query_id": cq["id"]})
            except TelegramError:
                pass
            self.send(self.on_button(data))
            return
        msg = u.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        if chat != self.chat_id:
            log.warning("Message ignoré d'un chat inconnu (%s)", chat)
            return
        text = (msg.get("text") or "").strip()
        if not text:
            return
        text = KEY_TO_CMD.get(text, text)
        cmd = text.split()[0].lower().split("@")[0]
        if cmd in ("/cleapi", "/depot") and msg.get("message_id"):
            try:  # le message contient un secret : on l'efface de la conversation
                self.api(self.token, "deleteMessage", {"chat_id": self.chat_id, "message_id": msg["message_id"]})
            except TelegramError:
                pass
        self.send(self.on_command(text))

    # ------------------------------------------------------------- commandes
    def on_command(self, text):
        e = self.engine
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]
        if cmd in ("/start", "/aide", "/help"):
            return {"text": AIDE, "keyboard": True}
        if cmd == "/menu":
            return ("⚙️ <b>Menu</b> — que veux-tu faire ?", MENU)
        if cmd in ("/statut", "/status"):
            return e.status_text()
        if cmd in ("/portefeuille", "/portfolio"):
            return e.portfolio_text()
        if cmd in ("/historique", "/history"):
            return e.history_text()
        if cmd == "/journal":
            ev = e.store.events(12)
            if not ev:
                return "Journal vide."
            icon = {"error": "🔴", "warn": "🟠"}
            return "📒 <b>Journal</b>\n\n" + "\n".join("%s <code>%s</code> %s" % (
                icon.get(x["level"], "▫️"), time.strftime("%d/%m %Hh%M", time.localtime(x["ts"])), F.esc(x["text"][:150]))
                for x in ev)
        if cmd == "/budget":
            bs = e.budget_status()
            return ("💶 <b>Budget de %s</b>\n<code>Crypto  </code> %s %s / %s\n<code>Actions </code> %s %s / %s\n\n"
                    "Achat crypto hebdo de base : ≈ %s\nChanger : <code>/regler budget_crypto 40</code> · <code>/regler budget_etf 10</code>" % (
                        F.month_name(e.clock()), F.bar(bs["spent"] / bs["monthly"] if bs["monthly"] else 0), F.eur(bs["spent"]),
                        F.eur(bs["monthly"]), F.bar(bs["etf_spent"] / bs["etf_monthly"] if bs["etf_monthly"] else 0),
                        F.eur(bs["etf_spent"]), F.eur(bs["etf_monthly"]), F.eur(bs["weekly_base"])))
        if cmd in ("/acheter", "/buy"):
            plan = e.plan_dca()
            if plan["amount"] <= 0:
                return "Budget du mois épuisé : aucun achat possible avant le 1er."
            return ("🛒 <b>Acheter maintenant ?</b>\nPrévu : <b>%s de %s</b>\n💡 %s" % (
                F.eur(plan["amount"]), F.esc(plan["asset"]), F.esc(plan["reason"])),
                [[("✅ Oui, acheter", "cmd:dca_now"), ("❌ Non", "cmd:noop")]])
        if cmd == "/pause":
            e.store.set("paused", True)
            e.log("info", "Pause demandée depuis Telegram")
            return "⏸️ <b>Achats automatiques suspendus.</b>\nLes alertes continuent. Bouton « ▶️ Reprendre » pour relancer."
        if cmd in ("/reprendre", "/resume"):
            e.store.set("paused", False)
            e.log("info", "Reprise demandée depuis Telegram")
            if e.flag("frozen") and not e.flag("freeze_override"):
                return ("▶️ Pause levée, MAIS les achats sont gelés (plafond de perte atteint). Les réactiver ?",
                        [[("▶️ Réactiver les achats", "cmd:resume_freeze")]])
            return "▶️ <b>Achats automatiques relancés.</b> Prochain achat : %s." % F.when(e.next_dca_ts(), e.clock())
        if cmd in ("/questions", "/q"):
            pend = e.store.pending_approvals()
            if not pend:
                return "Aucune question en attente."
            for a in pend:
                label = "✅ Fait" if a["kind"] == "etf" else "✅ Oui"
                no = "⏭ Sauter" if a["kind"] == "etf" else "❌ Non"
                self.notify(a["text"], [[(label, "ap:%d:y" % a["id"]), (no, "ap:%d:n" % a["id"])]])
            return None
        if cmd == "/etf":
            if not args:
                amt = e.cfg["budget"]["monthly_etf_eur"]
                plan = e.stock_plan()
                detail = "\n".join("• %s : <b>%s</b>" % (e.stock_label(k, html=True), F.eur(a)) for k, a in plan) or "(plan vide)"
                return ("🧾 <b>Achats actions/ETF prévus ce mois-ci</b>\n%s\n\nAchète-les dans l'appli Revolut (Investir), "
                        "puis déclare-les ici.\n<i>Autre montant : /etf 12.50 · Valeur totale : /etfvaleur 230</i>" % detail,
                        [[("✅ J'ai acheté ces %s" % F.eur(sum(a for _, a in plan)), "cmd:etf_default")]])
            amt = _num(args)
            if amt is None or amt <= 0 or amt > 10000:
                return "Usage : /etf 15  (montant en euros acheté dans l'appli Revolut)"
            e.record_etf(amt)
            return None
        if cmd == "/etfvaleur":
            v = _num(args)
            if v is None or v < 0:
                return "Usage : /etfvaleur 230.50  (valeur totale de ta ligne ETF dans Revolut)"
            return e.set_etf_value(v)
        if cmd == "/mode":
            if args and args[0].lower() in ("reel", "réel", "live"):
                return ("⚠️ Passer en mode RÉEL ? Les prochains ordres utiliseront ton vrai argent sur Revolut X.",
                        [[("💶 Oui, passer en réel", "cmd:mode_live"), ("❌ Annuler", "cmd:noop")]])
            if args and args[0].lower() in ("simu", "simulation", "paper"):
                return ("Repasser en SIMULATION ?", [[("🧪 Oui", "cmd:mode_paper"), ("❌ Annuler", "cmd:noop")]])
            other = ("💶 Passer en réel", "cmd:ask_live") if e.mode == "paper" else ("🧪 Passer en simulation", "cmd:ask_paper")
            return ("🔁 <b>Mode actuel</b> : %s\n%s" % (e.mode_label(), "<i>Simulation : vrais prix, argent fictif.</i>" if e.mode == "paper"
                                                           else "<i>Réel : les ordres utilisent ton argent sur Revolut X.</i>"), [[other]])
        if cmd == "/reglages":
            return self.settings_text()
        if cmd == "/regler":
            return self.set_setting(args)
        if cmd == "/cible":
            return self.set_target(args)
        if cmd == "/radar":
            return self.radar_cmd(args)
        if cmd == "/info":
            if not args:
                return "Usage : /info SOL  ou  /info NVDA"
            rd = e.get_radar().latest()
            text, r = e.get_radar().info_text(rd, args[0])
            if r and not e.get_radar()._in_plan(r):
                k = "c" if r["kind"] == "crypto" else "s"
                return (text, [[("➕ Ajouter à mon plan", "add:%s:%s" % (k, r["symbol"]))]])
            return text
        if cmd == "/actions":
            return self.stock_plan_text()
        if cmd == "/cible_action":
            return self.set_stock_target(args)
        if cmd == "/web":
            port = e.cfg["web"]["port"]
            return ("🌐 <b>Interface web</b> (seulement depuis le Wi-Fi de la maison)\nhttp://%s:%s\n"
                    "Identifiant : <code>admin</code> · Mot de passe : celui du setup" % (local_ip(), port))
        if cmd == "/cle":
            return self.key_info()
        if cmd == "/cleapi":
            return self.set_api_key(args)
        if cmd == "/depot":
            return self.set_repo(args)
        if cmd == "/version":
            return self.version_text()
        if cmd in ("/maj", "/update"):
            return self.check_update()
        if cmd in ("/retour", "/rollback"):
            if not self.updater or not self.updater.can_rollback():
                return "Aucune version précédente sauvegardée (elle apparaît après la première mise à jour)."
            return ("↩️ Remettre la version précédente puis redémarrer ?",
                    [[("↩️ Oui", "cmd:rollback"), ("❌ Annuler", "cmd:noop")]])
        if cmd in ("/redemarrer", "/restart"):
            return ("🔄 Redémarrer Micro-invest ?", [[("🔄 Oui", "cmd:restart"), ("❌ Annuler", "cmd:noop")]])
        return "🤔 Je n'ai pas compris. Utilise les boutons en bas, « ⚙️ Menu », ou /aide."

    def on_button(self, data):
        e = self.engine
        if data.startswith("ap:"):
            try:
                _, aid, yn = data.split(":")
                return e.resolve_approval(int(aid), yn == "y", "Telegram")
            except ValueError:
                return "Bouton invalide."
        if data.startswith("m:"):
            return self.on_command(data[2:])
        if data == "cmd:dca_now":
            return e.run_dca(manual=True)
        if data == "cmd:resume_freeze":
            return e.resume_freeze()
        if data == "cmd:etf_default":
            plan = e.stock_plan()
            e.record_etf(sum(a for _, a in plan), plan)
            return None
        if data.startswith("add:"):
            _, k, sym = data.split(":", 2)
            return self.confirm_add(k, sym)
        if data.startswith("addok:"):
            _, k, sym, w = data.split(":", 3)
            return self.set_target([sym, w]) if k == "c" else self.set_stock_target([sym, w])
        if data == "cmd:radar_run":
            return self.radar_cmd(["maj"])
        if data == "cmd:ask_live":
            return self.on_command("/mode reel")
        if data == "cmd:ask_paper":
            return self.on_command("/mode simu")
        if data in ("cmd:mode_live", "cmd:mode_paper"):
            try:
                e.set_mode("live" if data == "cmd:mode_live" else "paper")
                return "🔁 Mode : <b>%s</b>\n\n%s" % (e.mode_label(), e.status_text())
            except Exception as ex:
                return "❌ Changement de mode impossible : %s" % F.esc(ex)
        if data == "cmd:update":
            return self.start_update()
        if data == "cmd:rollback":
            try:
                msg = self.updater.rollback()
            except Exception as ex:
                return "❌ Retour impossible : %s" % F.esc(ex)
            e.log("info", "Retour à la version précédente demandé depuis Telegram")
            self.notify("↩️ %s. Redémarrage…" % msg)
            self.restart()
            return None
        if data == "cmd:restart":
            e.log("info", "Redémarrage demandé depuis Telegram")
            self.notify("🔄 Redémarrage… (message de démarrage dans ~10 s)")
            self.restart()
            return None
        if data == "cmd:noop":
            return "Annulé."
        return None

    # -------------------------------------------------------------- réglages
    def settings_text(self):
        c = self.engine.cfg
        lines = ["🔧 <b>Réglages</b>", ""]
        for name, (key, conv, lo, hi, desc) in SETTINGS.items():
            v = _get(c, key)
            if key == "dca.weekday":
                v = JOURS[v]
            elif isinstance(v, bool):
                v = "oui" if v else "non"
            elif isinstance(v, float) and v.is_integer():
                v = int(v)
            lines.append("<b>%s</b> · <code>%s</code>\n   <i>%s</i>" % (F.esc(v), F.esc(name), F.esc(desc)))
        t = c["dca"]["targets"]
        lines.append("\n🎯 <b>Cibles crypto</b> : " + " · ".join("%s %g" % kv for kv in t.items()))
        lines.append("\nChanger : <code>/regler nom valeur</code> (ex. <code>/regler budget_crypto 40</code>)\n"
                     "Cibles : <code>/cible SOL 12</code> (0 = retirer)")
        return "\n".join(lines)

    def set_setting(self, args):
        if len(args) < 2 or args[0].lower() not in SETTINGS:
            return "Usage : /regler nom valeur\nNoms possibles : " + ", ".join(SETTINGS) + "\nVoir les valeurs : /reglages"
        name = args[0].lower()
        key, conv, lo, hi, desc = SETTINGS[name]
        raw = " ".join(args[1:])
        try:
            val = conv(raw.replace(",", ".")) if conv in (float, int) else conv(raw)
            if lo is not None and not lo <= val <= hi:
                return "❌ %s : valeur entre %s et %s" % (name, lo, hi)
            config_mod.update_file(self.engine.cfg_path, key, val)
            self.engine.reload_config()
        except (ValueError, config_mod.ConfigError) as ex:
            return "❌ Valeur refusée : %s" % F.esc(ex)
        self.engine.log("info", "Réglage modifié depuis Telegram : %s = %s" % (name, val))
        shown = JOURS[val] if key == "dca.weekday" else ("oui" if val is True else "non" if val is False else val)
        return "✅ <b>%s</b> = %s <i>(appliqué immédiatement)</i>" % (F.esc(name), F.esc(shown))

    def set_target(self, args):
        e = self.engine
        if len(args) != 2:
            return "Usage : /cible SOL 12  (poids relatif ; 0 = retirer la crypto)"
        asset = args[0].upper()
        try:
            w = float(args[1].replace(",", "."))
        except ValueError:
            return "Usage : /cible SOL 12"
        targets = dict(e.cfg["dca"]["targets"])
        if w <= 0:
            if asset not in targets:
                return "%s n'est pas dans les cibles." % asset
            if len(targets) == 1:
                return "❌ Il faut garder au moins une crypto."
            targets.pop(asset)
        else:
            if asset not in targets:
                try:
                    e.market.pair_info(asset)
                except Exception as ex:
                    return "❌ %s" % ex
            targets[asset] = w
        try:
            config_mod.update_file(e.cfg_path, "dca.targets", targets)
            e.reload_config()
        except config_mod.ConfigError as ex:
            return "❌ %s" % ex
        e.log("info", "Cibles modifiées : %s" % targets)
        return "✅ Cibles : " + " · ".join("%s %g" % kv for kv in targets.items())

    # ------------------------------------------------------------- radar
    def radar_cmd(self, args):
        radar = self.engine.get_radar()
        sub = args[0].lower() if args else ""
        rd = radar.latest()
        if sub in ("maj", "update", "nouveau"):
            if radar.run_async(notify=True):
                return "🔭 Analyse lancée : toutes les cryptos Revolut X + %d actions/ETF. Résultat dans 2 à 4 min." % len(
                    __import__("microinvest.radar", fromlist=["STOCKS"]).STOCKS)
            return "🔭 Une analyse est déjà en cours, le résultat arrive."
        if not rd:
            radar.run_async(notify=True)
            return "🔭 Première analyse lancée (2 à 4 min). Je t'envoie le résultat dès qu'il est prêt."
        if sub in ("crypto", "cryptos"):
            return radar.ranking_text(rd, "crypto")
        if sub in ("actions", "action", "etf", "stocks"):
            return radar.ranking_text(rd, "stocks")
        text = radar.summary_text(rd)
        if self.engine.clock() - rd["ts"] > 26 * 3600:
            text += "\n\n(Analyse de plus d'un jour.)"
        buttons = radar.summary_buttons(rd) or []
        buttons.append([("🔄 Nouvelle analyse", "cmd:radar_run")])
        return (text, buttons)

    def confirm_add(self, k, sym):
        e = self.engine
        if k == "c":
            t = e.cfg["dca"]["targets"]
            w = max(1.0, round(min(t.values()) if t else 5.0, 1))
            share = w / (sum(t.values()) + w) * 100
            return ("➕ <b>Ajouter %s à ton plan crypto ?</b>\nPoids %g → environ <b>%.0f %%</b> de tes achats crypto.\n"
                    "<i>Le budget mensuel ne change pas : les autres cryptos auront un peu moins.</i>" % (F.esc(sym), w, share),
                    [[("✅ Ajouter", "addok:c:%s:%g" % (sym, w)), ("❌ Non", "cmd:noop")]])
        t = e.stock_targets()
        w = 20.0
        share = w / (sum(t.values()) + w) * 100
        return ("➕ <b>Ajouter à ton plan actions/ETF ?</b>\n%s\nPoids %g → environ <b>%.0f %%</b> de ton budget actions (%s/mois).\n"
                "<i>Tu l'achèteras dans l'appli Revolut lors du rappel mensuel.</i>" % (
                    e.stock_label(sym, html=True), w, share, F.eur(e.cfg["budget"]["monthly_etf_eur"])),
                [[("✅ Ajouter", "addok:s:%s:%g" % (sym, w)), ("❌ Non", "cmd:noop")]])

    def stock_plan_text(self):
        e = self.engine
        t = e.stock_targets()
        tsum = float(sum(t.values())) or 1
        inv = e.stock_invested()
        lines = ["📈 <b>Plan actions/ETF</b> — %s/mois <i>(achats guidés dans l'appli Revolut)</i>" % F.eur(
            e.cfg["budget"]["monthly_etf_eur"]), ""]
        for k, w in sorted(t.items(), key=lambda kv: -kv[1]):
            lines.append("• %s\n   cible <b>%.0f %%</b> · investi %s" % (e.stock_label(k, html=True), w / tsum * 100, F.eur(inv.get(k, 0.0))))
        plan = e.stock_plan()
        if plan:
            lines += ["", "🗓 <b>Prochain rappel</b> (le %d) :" % e.cfg["etf"]["day_of_month"]]
            lines += ["   %s : %s" % (F.esc(k.split(".")[0]), F.eur(a)) for k, a in plan]
        lines += ["", "<i>Ajouter/retirer : /cible_action NVDA 20 (0 = retirer) · Idées : « 🔭 Radar »</i>"]
        return "\n".join(lines)

    def set_stock_target(self, args):
        e = self.engine
        if len(args) != 2:
            return "Usage : /cible_action NVDA 20  (poids relatif ; 0 = retirer)"
        from .radar import STOCK_INFO
        sym = args[0].upper()
        match = [k for k in STOCK_INFO if k.upper() == sym or k.split(".")[0].upper() == sym]
        targets = e.stock_targets()
        if not match and sym not in targets:
            return "❌ %s n'est pas dans la liste suivie. Disponibles : %s" % (
                sym, ", ".join(k.split(".")[0] for k in STOCK_INFO))
        key = match[0] if match else sym
        try:
            w = float(args[1].replace(",", "."))
        except ValueError:
            return "Usage : /cible_action NVDA 20"
        if w <= 0:
            if key not in targets:
                return "%s n'est pas dans ton plan." % key
            if len(targets) == 1:
                return "❌ Il faut garder au moins une ligne (ou mets /regler budget_etf 0)."
            targets.pop(key)
        else:
            targets[key] = w
        try:
            config_mod.update_file(e.cfg_path, "stocks.targets", targets)
            e.reload_config()
        except config_mod.ConfigError as ex:
            return "❌ %s" % ex
        e.log("info", "Plan actions modifié : %s" % targets)
        return "✅ Plan actions : " + " · ".join("%s %g" % (k.split(".")[0], v) for k, v in targets.items())

    # --------------------------------------------------------- clés et dépôt
    def key_info(self):
        keys = os.path.join(self.base_dir, "keys")
        priv_path = os.path.join(keys, "private.pem")
        os.makedirs(keys, exist_ok=True)
        if os.path.exists(priv_path):
            with open(priv_path) as f:
                pub = ed25519_pure.public_pem_from_seed(ed25519_pure.load_private_pem(f.read()))
            intro = "🔑 Ta clé publique (déjà créée) :"
        else:
            priv, pub = ed25519_pure.generate_keypair()
            fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(priv)
            intro = "🔑 Clé créée sur le NAS. Voici la clé publique (elle peut être partagée sans risque) :"
        has_api = bool(self.engine.cfg["revolutx"].get("api_key"))
        return ("%s\n\n<pre>%s</pre>\n1. Copie tout le bloc (BEGIN et END compris)\n"
                "2. Revolut X (exchange.revolut.com) → Profil → API keys → Créer → colle-la → autorise le trading\n"
                "3. Envoie ici : /cleapi LA_CLE_API_AFFICHEE\n\nClé API enregistrée : %s" % (
                    intro, F.esc(pub.strip()), "oui ✅" if has_api else "pas encore"))

    def set_api_key(self, args):
        if len(args) != 1 or len(args[0]) < 16:
            return "Usage : /cleapi LA_CLE_API (copiée depuis Revolut X). Ton message sera effacé."
        key = args[0].strip()
        e = self.engine
        priv_path = os.path.join(self.base_dir, "keys", "private.pem")
        if not os.path.exists(priv_path):
            return "❌ Crée d'abord la clé de signature : bouton « 🔑 Clé Revolut X »."
        from .broker import BrokerError, RevolutXBroker
        try:
            with open(priv_path) as f:
                bal = RevolutXBroker(key, f.read(), e.market).balances()
        except BrokerError as ex:
            return "❌ Revolut X refuse cette clé : %s\n(Rien n'a été enregistré.)" % F.esc(ex)
        config_mod.update_file(e.cfg_path, "revolutx.api_key", key)
        e.reload_config()
        e.log("info", "Clé API Revolut X enregistrée depuis Telegram")
        return ("✅ Clé API valide et enregistrée (ton message a été effacé).\nSolde EUR sur Revolut X : %.2f €\n\n"
                "Pour passer en réel : bouton « 🔁 Mode simu/réel »." % bal.get(e.cfg["quote"], 0))

    def set_repo(self, args):
        if not args or args[0].count("/") != 1:
            return "Usage : /depot proprietaire/nom-du-depot [jeton]  (ton message sera effacé)"
        e = self.engine
        old_repo = e.cfg.get("update", {}).get("repo", "")
        old_tok = e.cfg.get("update", {}).get("token", "")
        try:
            config_mod.update_file(e.cfg_path, "update.repo", args[0])
            config_mod.update_file(e.cfg_path, "update.token", args[1] if len(args) > 1 else "")
            e.reload_config()
        except config_mod.ConfigError as ex:
            return "❌ %s" % ex
        if self.updater:
            self.updater.cfg = e.cfg
            try:
                last = self.updater.latest()
            except Exception as ex:
                config_mod.update_file(e.cfg_path, "update.repo", old_repo)
                config_mod.update_file(e.cfg_path, "update.token", old_tok)
                e.reload_config()
                self.updater.cfg = e.cfg
                return "❌ Dépôt inaccessible : %s\n(Ancien réglage conservé.)" % F.esc(ex)
            return "✅ Dépôt enregistré : %s\nDernière version disponible : %s — %s\n\nInstaller : bouton « ⬆️ Mise à jour »." % (
                args[0], last["sha"][:7], last["message"])
        return "✅ Dépôt enregistré : %s" % args[0]

    # ---------------------------------------------------------- mises à jour
    def version_text(self):
        v = self.updater.installed() if self.updater else None
        repo = self.engine.cfg.get("update", {}).get("repo") or "aucun (/depot proprietaire/depot)"
        if v:
            return "📦 <b>Version installée</b> : <code>%s</code>\n%s\nInstallée le %s · dépôt %s" % (
                v["sha"][:7], F.esc(v.get("message", "")), time.strftime("%d/%m/%Y à %Hh%M", time.localtime(v["ts"])), F.esc(repo))
        return "📦 <b>Version installée</b> : manuelle (zip) · dépôt %s" % F.esc(repo)

    def check_update(self):
        if not self.updater:
            return "Mises à jour indisponibles."
        self.updater.cfg = self.engine.cfg
        try:
            last = self.updater.latest()
        except Exception as ex:
            return "❌ Vérification impossible : %s" % F.esc(ex)
        cur = self.updater.installed()
        if cur and cur.get("sha") == last["sha"]:
            return "✅ Tu as déjà la dernière version (<code>%s</code> — %s)." % (last["sha"][:7], F.esc(last["message"]))
        self._pending_update = last
        return ("⬆️ <b>Nouvelle version disponible</b> : <code>%s</code>\n« %s »\n\n🛡 <i>Avant d'installer, je lance les tests "
                "automatiques : s'ils échouent, rien ne change. Si la nouvelle version plante au démarrage, je remets "
                "l'ancienne tout seul.</i>" % (last["sha"][:7], F.esc(last["message"])),
                [[("⬆️ Installer", "cmd:update"), ("❌ Plus tard", "cmd:noop")]])

    def start_update(self):
        up = self._pending_update
        if not up:
            return "Relance la vérification : bouton « ⬆️ Mise à jour »."
        if not self._busy.acquire(blocking=False):
            return "Une mise à jour est déjà en cours."

        def work():
            try:
                self.notify("⏳ Téléchargement et tests de la version <code>%s</code> (1 à 2 min)…" % up["sha"][:7])
                msg = self.updater.install(up["sha"], up["message"])
                self.engine.log("info", "Mise à jour : %s" % msg)
                self.notify("✅ %s\n🔄 Redémarrage…" % F.esc(msg))
                self._pending_update = None
                self.restart()
            except Exception as ex:
                log.exception("Mise à jour")
                self.engine.log("error", "Mise à jour annulée : %s" % ex)
                self.notify("❌ <b>Mise à jour annulée</b> : rien n'a été changé.\n<i>%s</i>" % F.esc(ex))
            finally:
                self._busy.release()

        threading.Thread(target=work, name="update", daemon=True).start()
        return None


def _num(args):
    if not args:
        return None
    try:
        return float(args[0].replace(",", ".").replace("€", ""))
    except ValueError:
        return None
