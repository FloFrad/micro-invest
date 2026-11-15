#!/usr/bin/env python3
"""Micro-invest — outil autonome de micro-investissement (Revolut X + Telegram + web).

Usage :
  python3 main.py setup     assistant de configuration (à faire une fois)
  python3 main.py genkey    crée la paire de clés pour l'API Revolut X
  python3 main.py check     vérifie que tout est bien branché
  python3 main.py run       lance l'outil (24 h/24)
  python3 main.py status    affiche l'état dans le terminal
"""
import json
import logging
import logging.handlers
import os
import secrets
import signal
import sys
import threading
import time

if sys.version_info < (3, 8):
    sys.exit("Python 3.8 minimum requis (tu as %s)." % sys.version.split()[0])

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from microinvest import config as config_mod  # noqa: E402
from microinvest import ed25519_pure  # noqa: E402
from microinvest.broker import BrokerError, MarketData, RevolutXBroker  # noqa: E402
from microinvest.engine import Engine  # noqa: E402
from microinvest.telegram_bot import TelegramBot, TelegramError, api_call  # noqa: E402
from microinvest.updater import Updater  # noqa: E402

CFG = os.path.join(BASE, "config.json")
OK, KO, WARN = "✅", "❌", "⚠️ "


def setup_logging(verbose=False):
    os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(os.path.join(BASE, "logs", "microinvest.log"),
                                              maxBytes=1000000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def ask(question, default=""):
    suffix = " [%s]" % default if default else ""
    try:
        r = input("%s%s : " % (question, suffix)).strip()
    except EOFError:
        r = ""
    return r or default


# --------------------------------------------------------------------- genkey
def cmd_genkey():
    keys = os.path.join(BASE, "keys")
    os.makedirs(keys, exist_ok=True)
    priv_path = os.path.join(keys, "private.pem")
    if os.path.exists(priv_path):
        with open(priv_path) as f:
            seed = ed25519_pure.load_private_pem(f.read())
        pub = ed25519_pure.public_pem_from_seed(seed)
        print("Une clé existe déjà (keys/private.pem). Je la garde.\n")
    else:
        priv, pub = ed25519_pure.generate_keypair()
        fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(priv)
        print("Clé privée créée : keys/private.pem (ne la partage JAMAIS).\n")
    with open(os.path.join(keys, "public.pem"), "w") as f:
        f.write(pub)
    print("👉 Copie TOUT le bloc ci-dessous (lignes BEGIN et END comprises) dans Revolut X → Profil → API keys :\n")
    print(pub)


# ---------------------------------------------------------------------- setup
def cmd_setup():
    print("\n=== Assistant de configuration Micro-invest ===\n")
    cfg = {}
    if os.path.exists(CFG):
        with open(CFG, encoding="utf-8") as f:
            cfg = json.load(f)
        print("config.json existe déjà : appuie sur Entrée pour garder chaque valeur actuelle.\n")
    d = config_mod.DEFAULTS
    tg = cfg.setdefault("telegram", {})
    web = cfg.setdefault("web", {})
    rx = cfg.setdefault("revolutx", {"private_key_file": "keys/private.pem"})
    bud = cfg.setdefault("budget", {})

    # 1. Telegram
    print("ÉTAPE 1/4 — Telegram")
    token = ask("Colle le token donné par @BotFather", tg.get("token", ""))
    try:
        me = api_call(token, "getMe")
        print("%s Bot reconnu : @%s" % (OK, me["username"]))
        tg["token"] = token
        if not tg.get("chat_id"):
            input("\n👉 Sur ton téléphone, ouvre @%s, appuie sur DÉMARRER (ou envoie /start), puis appuie ici sur Entrée…" % me["username"])
            chat = None
            for _ in range(10):
                ups = api_call(token, "getUpdates", {"timeout": 5}, timeout=15) or []
                for u in ups:
                    m = u.get("message") or {}
                    if m.get("chat", {}).get("type") == "private":
                        chat = m["chat"]["id"]
                if chat:
                    break
                print("   … pas encore reçu, renvoie /start au bot")
            if chat:
                tg["chat_id"] = str(chat)
                print("%s Ton identifiant Telegram : %s" % (OK, chat))
            else:
                print("%s Message non reçu. Relance « python3 main.py setup » plus tard." % KO)
        if tg.get("chat_id"):
            api_call(token, "sendMessage", {"chat_id": tg["chat_id"], "text": "✅ Micro-invest est relié à ce chat."})
            print("%s Message de test envoyé sur Telegram" % OK)
    except TelegramError as e:
        print("%s Telegram : %s (tu pourras relancer le setup)" % (KO, e))
        tg["token"] = token

    # 2. Interface web
    print("\nÉTAPE 2/4 — Interface web")
    pw = ask("Mot de passe de l'interface web", web.get("password") or secrets.token_urlsafe(9))
    web.update({"password": pw, "port": int(ask("Port", str(web.get("port", 8088)))),
                "host": web.get("host", "0.0.0.0")})

    # 3. Budget
    print("\nÉTAPE 3/4 — Budget (Entrée = garder la valeur proposée)")
    bud["monthly_crypto_eur"] = float(ask("Budget crypto par mois (€)", str(bud.get("monthly_crypto_eur", d["budget"]["monthly_crypto_eur"]))).replace(",", "."))
    bud["monthly_etf_eur"] = float(ask("Budget ETF par mois (€, achat manuel guidé)", str(bud.get("monthly_etf_eur", d["budget"]["monthly_etf_eur"]))).replace(",", "."))

    # 4. Revolut X
    print("\nÉTAPE 4/4 — Revolut X (laisse vide pour rester en simulation pour l'instant)")
    rx["api_key"] = ask("Clé API Revolut X", rx.get("api_key", ""))
    rx.setdefault("private_key_file", "keys/private.pem")
    cfg.setdefault("mode", "paper")

    config_mod.validate(config_mod._merge(config_mod.DEFAULTS, cfg))
    config_mod.save(CFG, cfg)
    print("\n%s config.json enregistré. Mode : %s" % (OK, "SIMULATION" if cfg["mode"] == "paper" else "RÉEL"))
    print("Mot de passe web : %s  (identifiant : n'importe lequel, ex. « admin »)" % pw)
    print("\nÉtape suivante : python3 main.py check")


# ---------------------------------------------------------------------- check
def cmd_check():
    errors = 0
    print("\n=== Vérification ===\n")
    print("%s Python %s" % (OK, sys.version.split()[0]))
    try:
        cfg = config_mod.load(CFG)
        print("%s config.json valide (mode %s)" % (OK, cfg["mode"]))
    except config_mod.ConfigError as e:
        print("%s %s" % (KO, e))
        return 1
    market = MarketData(cfg["quote"])
    assets = list(cfg["dca"]["targets"].keys())
    try:
        pairs = market.pairs()
        missing = [a for a in assets if market.symbol(a) not in pairs]
        if missing:
            errors += 1
            print("%s Paires absentes sur Revolut X : %s → retire-les de dca.targets" % (KO, ", ".join(market.symbol(a) for a in missing)))
        t = market.tickers([a for a in assets if a not in missing])
        print("%s Prix Revolut X : %s" % (OK, ", ".join("%s %.2f" % (a, v["last"]) for a, v in t.items())))
        for a in assets:
            if a in missing:
                continue
            mini = float(pairs[market.symbol(a)].get("min_order_size_quote") or 0)
            if mini > cfg["budget"]["monthly_crypto_eur"] * 12 / 52:
                print("%s Minimum d'ordre %s = %.2f € > achat hebdo : %s sera acheté moins souvent" % (WARN, a, mini, a))
        try:
            market.daily_closes(assets[0], 30)
            print("%s Historique des prix (moyenne 30 j) disponible" % OK)
        except BrokerError as e:
            print("%s Historique indisponible (%s) : les achats « baisse » seront désactivés" % (WARN, e))
    except BrokerError as e:
        errors += 1
        print("%s Revolut X injoignable : %s" % (KO, e))

    tg = cfg["telegram"]
    if tg.get("token") and tg.get("chat_id"):
        try:
            me = api_call(tg["token"], "getMe")
            api_call(tg["token"], "sendMessage", {"chat_id": tg["chat_id"], "text": "🔧 Test de vérification : Telegram OK"})
            print("%s Telegram : @%s, message de test envoyé" % (OK, me["username"]))
        except TelegramError as e:
            errors += 1
            print("%s Telegram : %s" % (KO, e))
    else:
        errors += 1
        print("%s Telegram non configuré → python3 main.py setup" % KO)

    rx = cfg["revolutx"]
    key_path = rx["private_key_file"] if os.path.isabs(rx["private_key_file"]) else os.path.join(BASE, rx["private_key_file"])
    if rx.get("api_key") and os.path.exists(key_path):
        try:
            with open(key_path) as f:
                b = RevolutXBroker(rx["api_key"], f.read(), market).balances()
            print("%s Clé API Revolut X OK — solde EUR disponible : %.2f €" % (OK, b.get(cfg["quote"], 0)))
            if b.get(cfg["quote"], 0) < cfg["budget"]["monthly_crypto_eur"]:
                print("%s Solde EUR inférieur au budget du mois : pense à alimenter Revolut X" % WARN)
        except BrokerError as e:
            errors += 1
            print("%s Clé API Revolut X refusée : %s" % (KO, e))
    elif cfg["mode"] == "live":
        errors += 1
        print("%s Mode réel demandé mais clé API ou keys/private.pem manquant" % KO)
    else:
        print("%s Revolut X : pas encore de clé API (normal en simulation)" % WARN)

    import socket
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((cfg["web"]["host"], int(cfg["web"]["port"])))
        print("%s Port web %s libre" % (OK, cfg["web"]["port"]))
    except OSError:
        print("%s Port %s déjà utilisé (l'outil tourne peut-être déjà)" % (WARN, cfg["web"]["port"]))
    finally:
        s.close()

    print("\n%s" % ("Tout est prêt : python3 main.py run" if errors == 0 else "%d problème(s) à corriger (voir ❌ ci-dessus)." % errors))
    return 1 if errors else 0


# ------------------------------------------------------------------------ run
def single_instance():
    import fcntl
    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    f = open(os.path.join(BASE, "data", "run.lock"), "a+")  # "a+" : ne pas effacer le PID de l'instance en cours
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Micro-invest tourne déjà.")
        sys.exit(3)
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def cmd_run():
    lock = single_instance()  # noqa: F841 (garde le verrou ouvert)
    setup_logging()
    log = logging.getLogger("main")
    cfg = config_mod.load(CFG)
    engine = Engine(cfg, CFG, BASE)
    updater = Updater(BASE, engine.cfg)
    stop = threading.Event()
    exit_code = [0]

    def restart():
        exit_code[0] = 4  # run.sh relance immédiatement
        stop.set()

    bot = None
    tg = cfg["telegram"]
    if tg.get("token") and tg.get("chat_id"):
        bot = TelegramBot(tg["token"], tg["chat_id"], engine, updater=updater, restart=restart, base_dir=BASE)
        engine.notifier = bot
        bot.register_commands()
        threading.Thread(target=bot.run_forever, name="telegram", daemon=True).start()
    else:
        log.warning("Telegram non configuré : aucune alerte ne sera envoyée")
    from microinvest import web
    web.start(engine, cfg["web"]["host"], cfg["web"]["port"], cfg["web"]["password"])

    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    v = updater.installed()
    version = "%s — %s" % (v["sha"][:7], v.get("message", "")) if v else "manuelle (zip)"
    log.info("Micro-invest démarré (mode %s, version %s)", engine.mode, version)
    engine.log("info", "Démarrage (mode %s, version %s)" % (engine.mode_label(), version))
    rolled = os.path.join(BASE, "data", "rolled_back")
    if os.path.exists(rolled):
        os.remove(rolled)
        engine.log("error", "La mise à jour plantait au démarrage : version précédente remise automatiquement")
        engine.notify("⚠️ <b>La nouvelle version plantait au démarrage.</b>\nJ'ai remis automatiquement la version précédente. "
                      "Rien à faire de ton côté : préviens-moi et je corrige.")
    start_msg = "🚀 <b>Micro-invest a démarré</b> · version %s\n\n%s" % (version.split(" — ")[0], engine.status_text())
    if bot:
        try:
            bot.notify(start_msg, keyboard=True)  # affiche aussi les boutons en bas de l'écran
        except Exception as e:
            log.warning("Message de démarrage non envoyé : %s", e)
    started = time.time()
    pending = os.path.join(BASE, "data", "update_pending")
    while not stop.is_set():
        try:
            engine.tick()
        except Exception:
            log.exception("Erreur dans la boucle principale")
        if os.path.exists(pending) and time.time() - started > 90:
            os.remove(pending)  # la nouvelle version tourne bien depuis 90 s : elle est validée
            engine.log("info", "Mise à jour validée")
            engine.notify("✅ <b>Mise à jour validée</b> : la nouvelle version fonctionne.")
        stop.wait(30)
    log.info("Arrêt demandé (code %d)", exit_code[0])
    if exit_code[0] != 4:
        engine.notify("⏹ <b>Micro-invest arrêté.</b>")
    sys.exit(exit_code[0])


def cmd_status():
    cfg = config_mod.load(CFG)
    from microinvest import fmt
    print(fmt.strip_tags(Engine(cfg, CFG, BASE).status_text()))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    try:
        if cmd == "setup":
            cmd_setup()
        elif cmd == "genkey":
            cmd_genkey()
        elif cmd == "check":
            sys.exit(cmd_check())
        elif cmd == "run":
            cmd_run()
        elif cmd == "status":
            cmd_status()
        else:
            print(__doc__)
    except config_mod.ConfigError as e:
        sys.exit("❌ %s" % e)
    except BrokerError as e:
        sys.exit("❌ %s" % e)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
