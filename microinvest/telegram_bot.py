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

log = logging.getLogger("telegram")

# Clavier permanent en bas de l'écran
KEYBOARD = [["📊 Statut", "💼 Portefeuille"], ["🛒 Acheter", "❓ Questions"], ["⏸ Pause", "▶️ Reprendre"], ["⚙️ Menu"]]
KEY_TO_CMD = {"📊 Statut": "/statut", "💼 Portefeuille": "/portefeuille", "🛒 Acheter": "/acheter",
              "❓ Questions": "/questions", "⏸ Pause": "/pause", "▶️ Reprendre": "/reprendre", "⚙️ Menu": "/menu"}

MENU = [
    [("📜 Historique", "m:/historique"), ("📒 Journal", "m:/journal")],
    [("📈 ETF du mois", "m:/etf"), ("💶 Budget", "m:/budget")],
    [("🔧 Réglages", "m:/reglages"), ("🔁 Mode simu/réel", "m:/mode")],
    [("⬆️ Mise à jour", "m:/maj"), ("↩️ Version précédente", "m:/retour")],
    [("🔄 Redémarrer", "m:/redemarrer"), ("🌐 Interface web", "m:/web")],
    [("🔑 Clé Revolut X", "m:/cle"), ("❔ Aide", "m:/aide")],
]

BOT_COMMANDS = [
    ("menu", "Tous les boutons"), ("statut", "État, budget, prochain achat"), ("portefeuille", "Positions et gains"),
    ("acheter", "Achat de la semaine maintenant"), ("questions", "Questions en attente"), ("pause", "Suspendre les achats"),
    ("reprendre", "Relancer les achats"), ("historique", "Dernières opérations"), ("journal", "Journal des événements"),
    ("etf", "Achat ETF du mois"), ("reglages", "Voir / modifier les réglages"), ("maj", "Mettre à jour l'outil"),
    ("version", "Version installée"), ("mode", "Simulation ou réel"), ("aide", "Aide"),
]

AIDE = """🤖 Micro-invest — aide

Utilise les boutons en bas de l'écran, ou « ⚙️ Menu » pour tout le reste.

Commandes avec une valeur :
/etf 15 — déclarer un achat ETF de 15 €
/etfvaleur 230.50 — valeur actuelle de ta ligne ETF
/regler budget_crypto 40 — changer un réglage (liste : /reglages)
/cible SOL 12 — changer le poids d'une crypto (0 = la retirer)
/cleapi TA_CLE — enregistrer la clé API Revolut X (message effacé)
/depot proprietaire/depot [jeton] — dépôt GitHub des mises à jour (message effacé)
/mode reel · /mode simu — changer de mode"""

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
    "budget_etf": ("budget.monthly_etf_eur", float, 0, 1000, "Budget ETF par mois (€)"),
    "max_ordre": ("budget.max_order_eur", float, 1, 1000, "Montant maximum d'un ordre (€)"),
    "validation_au_dela": ("budget.approval_above_eur", float, 0, 1000, "Au-delà, je te demande ton accord (€)"),
    "reserve": ("budget.keep_reserve_eur", float, 0, 100000, "Euros jamais utilisés sur Revolut X"),
    "jour_achat": ("dca.weekday", _parse_day, 0, 6, "Jour de l'achat hebdo (lundi … dimanche)"),
    "heure_achat": ("dca.hour", int, 0, 23, "Heure de l'achat hebdo"),
    "seuil_baisse": ("dca.dip_threshold_pct", float, 1, 90, "Baisse (%) qui déclenche un achat renforcé"),
    "plafond_perte": ("safety.max_drawdown_pct", float, 1, 90, "Perte (%) qui gèle les achats"),
    "prise_benefices": ("take_profit.gain_pct", float, 5, 1000, "Gain (%) qui déclenche une proposition de vente"),
    "prise_benefices_active": ("take_profit.enabled", _parse_bool, None, None, "Propositions de vente (oui/non)"),
    "jour_etf": ("etf.day_of_month", int, 1, 28, "Jour du mois du rappel ETF"),
    "nom_etf": ("etf.name", str, None, None, "Nom de l'ETF à acheter"),
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
    def notify(self, text, buttons=None, keyboard=False):
        params = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = _inline(buttons)
        elif keyboard:
            params["reply_markup"] = {"keyboard": [[{"text": t} for t in row] for row in KEYBOARD],
                                      "resize_keyboard": True, "is_persistent": True}
        res = self.api(self.token, "sendMessage", params)
        return res.get("message_id") if isinstance(res, dict) else None

    def edit(self, msg_id, text):
        self.api(self.token, "editMessageText", {"chat_id": self.chat_id, "message_id": msg_id, "text": text[:4000]})

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
                            self.notify("⚠️ Erreur interne : %s" % e)
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
            return ("⚙️ Menu — que veux-tu faire ?", MENU)
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
            return "📒 Journal\n\n" + "\n".join("%s %s" % (time.strftime("%d/%m %H:%M", time.localtime(x["ts"])), x["text"][:150])
                                                 for x in ev)
        if cmd == "/budget":
            bs = e.budget_status()
            return ("💶 Budget du mois\nCrypto : %.2f / %.2f € (achat hebdo de base ≈ %.2f €)\nETF : %.2f / %.2f €\n\n"
                    "Changer : /regler budget_crypto 40  ou  /regler budget_etf 10" % (
                        bs["spent"], bs["monthly"], bs["weekly_base"], bs["etf_spent"], bs["etf_monthly"]))
        if cmd in ("/acheter", "/buy"):
            plan = e.plan_dca()
            if plan["amount"] <= 0:
                return "Budget du mois épuisé : aucun achat possible avant le 1er."
            return ("Lancer maintenant l'achat de la semaine ?\nPrévu : %.2f € de %s\n%s" % (
                plan["amount"], plan["asset"], plan["reason"]), [[("✅ Oui", "cmd:dca_now"), ("❌ Non", "cmd:noop")]])
        if cmd == "/pause":
            e.store.set("paused", True)
            e.log("info", "Pause demandée depuis Telegram")
            return "⏸ Achats automatiques suspendus. Bouton « ▶️ Reprendre » pour relancer."
        if cmd in ("/reprendre", "/resume"):
            e.store.set("paused", False)
            e.log("info", "Reprise demandée depuis Telegram")
            if e.flag("frozen") and not e.flag("freeze_override"):
                return ("▶️ Pause levée, MAIS les achats sont gelés (plafond de perte atteint). Les réactiver ?",
                        [[("▶️ Réactiver les achats", "cmd:resume_freeze")]])
            return "▶️ Achats automatiques relancés."
        if cmd in ("/questions", "/q"):
            pend = e.store.pending_approvals()
            if not pend:
                return "Aucune question en attente."
            for a in pend:
                label = "✅ Fait" if a["kind"] == "etf" else "✅ Oui"
                no = "⏭ Sauter" if a["kind"] == "etf" else "❌ Non"
                self.notify("❓ " + a["text"], [[(label, "ap:%d:y" % a["id"]), (no, "ap:%d:n" % a["id"])]])
            return None
        if cmd == "/etf":
            if not args:
                amt = e.cfg["budget"]["monthly_etf_eur"]
                return ("📈 ETF du mois : %s\nAchète-le dans l'appli Revolut (Investir), puis déclare-le ici.\n"
                        "Autre montant : /etf 12.50 · Valeur de ta ligne : /etfvaleur 230" % e.cfg["etf"]["name"],
                        [[("✅ J'ai acheté %.2f €" % amt, "cmd:etf_default")]])
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
            return ("Mode actuel : %s" % e.mode_label(), [[other]])
        if cmd == "/reglages":
            return self.settings_text()
        if cmd == "/regler":
            return self.set_setting(args)
        if cmd == "/cible":
            return self.set_target(args)
        if cmd == "/web":
            port = e.cfg["web"]["port"]
            return ("🌐 Interface web (uniquement depuis le Wi-Fi de la maison) :\nhttp://%s:%s\n"
                    "Identifiant : admin · Mot de passe : celui du setup" % (local_ip(), port))
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
        return "Commande inconnue. Utilise les boutons ou « ⚙️ Menu »."

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
            e.record_etf(e.cfg["budget"]["monthly_etf_eur"])
            return None
        if data == "cmd:ask_live":
            return self.on_command("/mode reel")
        if data == "cmd:ask_paper":
            return self.on_command("/mode simu")
        if data in ("cmd:mode_live", "cmd:mode_paper"):
            try:
                e.set_mode("live" if data == "cmd:mode_live" else "paper")
                return "Mode : %s\n\n%s" % (e.mode_label(), e.status_text())
            except Exception as ex:
                return "Changement de mode impossible : %s" % ex
        if data == "cmd:update":
            return self.start_update()
        if data == "cmd:rollback":
            try:
                msg = self.updater.rollback()
            except Exception as ex:
                return "❌ Retour impossible : %s" % ex
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
        lines = ["🔧 Réglages actuels", ""]
        for name, (key, conv, lo, hi, desc) in SETTINGS.items():
            v = _get(c, key)
            if key == "dca.weekday":
                v = JOURS[v]
            elif isinstance(v, bool):
                v = "oui" if v else "non"
            lines.append("• %s = %s\n   %s" % (name, v, desc))
        t = c["dca"]["targets"]
        lines.append("\n🎯 Cibles : " + " · ".join("%s %g" % kv for kv in t.items()))
        lines.append("\nChanger : /regler nom valeur (ex. /regler budget_crypto 40)\nCibles : /cible SOL 12  (0 = retirer)")
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
            return "❌ Valeur refusée : %s" % ex
        self.engine.log("info", "Réglage modifié depuis Telegram : %s = %s" % (name, val))
        shown = JOURS[val] if key == "dca.weekday" else ("oui" if val is True else "non" if val is False else val)
        return "✅ %s = %s (appliqué immédiatement)" % (name, shown)

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
        return ("%s\n\n%s\n1. Copie tout le bloc (BEGIN et END compris)\n"
                "2. Revolut X (exchange.revolut.com) → Profil → API keys → Créer → colle-la → autorise le trading\n"
                "3. Envoie ici : /cleapi LA_CLE_API_AFFICHEE\n\nClé API enregistrée : %s" % (
                    intro, pub.strip(), "oui ✅" if has_api else "pas encore"))

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
            return "❌ Revolut X refuse cette clé : %s\n(Rien n'a été enregistré.)" % ex
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
                return "❌ Dépôt inaccessible : %s\n(Ancien réglage conservé.)" % ex
            return "✅ Dépôt enregistré : %s\nDernière version disponible : %s — %s\n\nInstaller : bouton « ⬆️ Mise à jour »." % (
                args[0], last["sha"][:7], last["message"])
        return "✅ Dépôt enregistré : %s" % args[0]

    # ---------------------------------------------------------- mises à jour
    def version_text(self):
        v = self.updater.installed() if self.updater else None
        repo = self.engine.cfg.get("update", {}).get("repo") or "aucun (/depot proprietaire/depot)"
        if v:
            return "📦 Version installée : %s — %s\nInstallée le %s\nDépôt : %s" % (
                v["sha"][:7], v.get("message", ""), time.strftime("%d/%m/%Y %H:%M", time.localtime(v["ts"])), repo)
        return "📦 Version installée : manuelle (zip)\nDépôt : %s" % repo

    def check_update(self):
        if not self.updater:
            return "Mises à jour indisponibles."
        self.updater.cfg = self.engine.cfg
        try:
            last = self.updater.latest()
        except Exception as ex:
            return "❌ Vérification impossible : %s" % ex
        cur = self.updater.installed()
        if cur and cur.get("sha") == last["sha"]:
            return "✅ Tu as déjà la dernière version (%s — %s)." % (last["sha"][:7], last["message"])
        self._pending_update = last
        return ("⬆️ Nouvelle version disponible : %s\n« %s »\n\nAvant d'installer, je lance les tests automatiques. "
                "S'ils échouent, rien ne change. Si la nouvelle version plante au démarrage, je remets l'ancienne tout seul." % (
                    last["sha"][:7], last["message"]),
                [[("⬆️ Installer", "cmd:update"), ("❌ Plus tard", "cmd:noop")]])

    def start_update(self):
        up = self._pending_update
        if not up:
            return "Relance la vérification : bouton « ⬆️ Mise à jour »."
        if not self._busy.acquire(blocking=False):
            return "Une mise à jour est déjà en cours."

        def work():
            try:
                self.notify("⏳ Téléchargement et tests de la version %s (1 à 2 min)…" % up["sha"][:7])
                msg = self.updater.install(up["sha"], up["message"])
                self.engine.log("info", "Mise à jour : %s" % msg)
                self.notify("✅ %s\n🔄 Redémarrage…" % msg)
                self._pending_update = None
                self.restart()
            except Exception as ex:
                log.exception("Mise à jour")
                self.engine.log("error", "Mise à jour annulée : %s" % ex)
                self.notify("❌ Mise à jour annulée : %s" % ex)
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
