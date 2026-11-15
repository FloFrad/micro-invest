"""Interface web (tableau de bord) — serveur HTTP intégré, protégé par mot de passe."""
import base64
import hmac
import ipaddress
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

log = logging.getLogger("web")
HERE = os.path.dirname(os.path.abspath(__file__))


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def is_local(ip):
    """Vrai pour une adresse du réseau local (maison) ou de la machine elle-même."""
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_private or a.is_loopback or a.is_link_local


MAX_FAILS = 5          # mots de passe faux autorisés…
FAIL_WINDOW = 15 * 60  # …sur 15 minutes, ensuite blocage de l'adresse IP pendant 15 minutes


def make_handler(engine, password, clock=time.time, commands=None):
    page_path = os.path.join(HERE, "dashboard.html")
    if commands is None:
        from .telegram_bot import WebCommands
        commands = WebCommands(engine, base_dir=engine.base_dir)
    fails = {}  # ip -> [horodatages des échecs]
    fails_lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        server_version = "microinvest"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _client_ip(self):
            ip = self.client_address[0]
            # Derrière le proxy inversé du NAS, la vraie IP est transmise dans un en-tête
            if ip in ("127.0.0.1", "::1"):
                fwd = self.headers.get("X-Real-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0]
                if fwd.strip():
                    return fwd.strip()
            return ip

        def _auth(self):
            ip = self._client_ip()
            if not is_local(ip):
                # L'interface n'est accessible que depuis le réseau de la maison
                log.warning("Accès web refusé depuis l'extérieur : %s", ip)
                self._send(403, '{"error": "interface accessible uniquement depuis le réseau local"}')
                return False
            now = clock()
            with fails_lock:
                for k in (ip, "*"):
                    fails[k] = [t for t in fails.get(k, []) if now - t < FAIL_WINDOW]
                # par adresse IP, plus un plafond global (si quelqu'un change d'IP à chaque essai)
                blocked = len(fails[ip]) >= MAX_FAILS or len(fails["*"]) >= MAX_FAILS * 4
            if blocked:
                self._send(429, '{"error": "trop de mots de passe faux, réessaie dans 15 minutes"}')
                return False
            hdr = self.headers.get("Authorization", "")
            if hdr.startswith("Basic "):
                try:
                    _user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                    if hmac.compare_digest(pw.encode(), password.encode()):
                        with fails_lock:
                            fails.pop(ip, None)
                        return True
                except Exception:
                    pass
                with fails_lock:  # un mot de passe a été essayé et il est faux
                    fails.setdefault(ip, []).append(now)
                    fails.setdefault("*", []).append(now)
                    n = len(fails[ip])
                    total = len(fails["*"])
                if total == MAX_FAILS * 4:
                    engine.notify("🔐 %d mots de passe faux en 15 min sur l'interface web (plusieurs adresses). "
                                  "Connexion web bloquée 15 minutes pour tout le monde. Telegram reste utilisable." % total)
                log.warning("Mot de passe web faux depuis %s (%d/%d)", ip, n, MAX_FAILS)
                if n == MAX_FAILS:
                    engine.log("warn", "Interface web : adresse %s bloquée 15 min (mots de passe faux)" % ip)
                    engine.notify("🔐 %d mots de passe faux sur l'interface web depuis l'adresse %s. "
                                  "Elle est bloquée 15 minutes. Si ce n'est pas toi, rien à faire : "
                                  "le blocage est automatique." % (MAX_FAILS, ip))
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="microinvest", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

        def do_GET(self):
            if not self._auth():
                return
            if self.path in ("/", "/index.html"):
                with open(page_path, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                try:
                    self._json(engine.state())
                except Exception as e:
                    log.exception("state")
                    self._json({"error": str(e)}, 500)
            elif self.path.startswith("/api/file/"):
                f = commands.files.get(self.path.rsplit("/", 1)[1])
                if not f:
                    return self._send(404, "{}")
                self._download(f[1], f[2], f[0], inline=f[2].startswith("image/"))
            elif self.path.startswith("/api/chart/portfolio.png"):
                q = self.path.partition("?")[2]
                days = int(q.split("=")[1]) if q.startswith("days=") and q.split("=")[1].isdigit() else 90
                img = engine.portfolio_chart_png(days)
                if not img:
                    return self._send(404, "{}")
                self._download(img, "image/png", "portefeuille.png", inline=True)
            elif self.path.startswith("/api/chart/backtest.png"):
                p = os.path.join(engine.base_dir, "data", "backtest.png")
                if not os.path.exists(p):
                    return self._send(404, "{}")
                with open(p, "rb") as fh:
                    self._download(fh.read(), "image/png", "test-12-mois.png", inline=True)
            elif self.path == "/api/download/operations.csv":
                from . import tax
                self._download(tax.operations_csv(engine.store), "text/csv; charset=utf-8", "operations-%s.csv" % engine.mode)
            elif self.path.startswith("/api/download/plusvalues-") and self.path.endswith(".csv"):
                from . import tax
                y = self.path[len("/api/download/plusvalues-"):-4]
                if not y.isdigit():
                    return self._send(404, "{}")
                self._download(tax.plus_values_csv(engine.store, int(y)), "text/csv; charset=utf-8", "plus-values-%s.csv" % y)
            elif self.path == "/api/download/backup.zip":
                from . import backup
                self._download(backup.build_zip(engine.base_dir, with_secrets=False), "application/zip",
                               "microinvest-sauvegarde-sans-secrets.zip")
            else:
                self._send(404, "{}")

        def _download(self, data, ctype, filename, inline=False):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", '%s; filename="%s"' % ("inline" if inline else "attachment", filename))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not self._auth():
                return
            # Protection anti-CSRF : un formulaire d'un autre site ne peut pas poser cet en-tête
            if self.headers.get("X-Requested-With") != "microinvest":
                return self._json({"error": "requête refusée"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode() or "{}") if n else {}
            except ValueError:
                body = {}
            p = self.path
            try:
                if p == "/api/cmd":
                    text, button = str(body.get("text") or "")[:500], str(body.get("button") or "")[:200]
                    if not text and not button:
                        return self._json({"error": "commande vide"}, 400)
                    engine.log("info", "Web : %s" % (button or text.split()[0]))
                    return self._json({"messages": commands.run(text=text or None, button=button or None)})
                if p == "/api/pause":
                    engine.store.set("paused", True)
                    engine.log("info", "Pause demandée depuis le web")
                    msg = "Achats automatiques suspendus."
                elif p == "/api/resume":
                    engine.store.set("paused", False)
                    engine.log("info", "Reprise demandée depuis le web")
                    msg = "Achats automatiques relancés."
                elif p == "/api/resume_freeze":
                    msg = engine.resume_freeze()
                elif p == "/api/dca_now":
                    msg = engine.run_dca(manual=True)
                elif p.startswith("/api/approve/") or p.startswith("/api/reject/"):
                    aid = int(p.rsplit("/", 1)[1])
                    msg = engine.resolve_approval(aid, p.startswith("/api/approve/"), "web")
                elif p == "/api/etf":
                    amt = float(body.get("amount", 0))
                    if not 0 < amt <= 10000:
                        return self._json({"error": "montant invalide"}, 400)
                    engine.record_etf(amt)
                    msg = "Achat ETF enregistré."
                elif p == "/api/etf_value":
                    v = float(body.get("value", -1))
                    if v < 0:
                        return self._json({"error": "valeur invalide"}, 400)
                    msg = engine.set_etf_value(v)
                else:
                    return self._json({"error": "inconnu"}, 404)
            except Exception as e:
                log.exception("action web")
                return self._json({"error": str(e)}, 500)
            self._json({"message": msg})

    return H


def start(engine, host, port, password, clock=time.time, commands=None):
    if not password:
        raise ValueError("web.password vide dans config.json")
    srv = _Server((host, int(port)), make_handler(engine, password, clock, commands))
    th = threading.Thread(target=srv.serve_forever, name="web", daemon=True)
    th.start()
    log.info("Interface web : http://%s:%s", host, port)
    return srv
