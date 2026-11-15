"""Mises à jour depuis un dépôt GitHub, pilotées par Telegram.

Sécurités :
- la nouvelle version est testée (tests automatiques) AVANT d'être installée ;
- l'ancienne version est sauvegardée dans data/backup/last ;
- si la nouvelle version plante au démarrage, run.sh remet l'ancienne tout seul ;
- config.json, keys/, data/ et logs/ ne sont jamais touchés.
"""
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

log = logging.getLogger("updater")

API = "https://api.github.com"
# Fichiers et dossiers qui composent le code (tout le reste est à toi et n'est jamais modifié)
CODE_FILES = ["main.py", "run.sh", "stop.sh", "README.md", "GUIDE.md", "config.example.json", ".gitignore"]
CODE_DIRS = ["microinvest", "tests"]


class UpdateError(Exception):
    pass


class Updater:
    def __init__(self, base_dir, cfg, python=None, http=None):
        self.base = base_dir
        self.cfg = cfg
        self.python = python or sys.executable
        self._http = http or self._urlopen
        self.data = os.path.join(base_dir, "data")
        self.backup = os.path.join(self.data, "backup", "last")

    # ----------------------------------------------------------------- GitHub
    @property
    def repo(self):
        return (self.cfg.get("update") or {}).get("repo", "").strip()

    @property
    def branch(self):
        return (self.cfg.get("update") or {}).get("branch", "main") or "main"

    def _urlopen(self, url, accept):
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "microinvest-updater",
                                                    "X-GitHub-Api-Version": "2022-11-28"})
        token = (self.cfg.get("update") or {}).get("token", "").strip()
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise UpdateError("dépôt %s introuvable (ou privé sans jeton valide)" % self.repo)
            if e.code in (401, 403):
                raise UpdateError("accès refusé par GitHub (jeton invalide ou expiré ?)")
            raise UpdateError("GitHub a répondu HTTP %s" % e.code)
        except urllib.error.URLError as e:
            raise UpdateError("GitHub injoignable : %s" % e.reason)

    def latest(self):
        if not self.repo:
            raise UpdateError("aucun dépôt configuré. Envoie : /depot proprietaire/nom-du-depot")
        raw = self._http("%s/repos/%s/commits/%s" % (API, self.repo, self.branch), "application/vnd.github+json")
        c = json.loads(raw.decode())
        return {"sha": c["sha"], "message": c["commit"]["message"].split("\n")[0][:200],
                "date": c["commit"]["committer"]["date"]}

    # --------------------------------------------------------------- versions
    def installed(self):
        try:
            with open(os.path.join(self.data, "version.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write_version(self, info):
        os.makedirs(self.data, exist_ok=True)
        with open(os.path.join(self.data, "version.json"), "w") as f:
            json.dump(info, f)

    # -------------------------------------------------------------- install
    def install(self, sha, message=""):
        """Télécharge, teste puis installe la version `sha`. Retourne un résumé."""
        raw = self._http("%s/repos/%s/zipball/%s" % (API, self.repo, sha), "application/vnd.github+json")
        tmp = os.path.join(self.data, "update_tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for name in z.namelist():
                    dest = os.path.realpath(os.path.join(tmp, name))
                    if not dest.startswith(os.path.realpath(tmp) + os.sep):
                        raise UpdateError("archive invalide (chemin suspect : %s)" % name)
                z.extractall(tmp)
        except zipfile.BadZipFile:
            raise UpdateError("archive téléchargée illisible")
        root = self._find_root(tmp)
        self._run_tests(root)
        self._backup_current()
        self._copy_code(root, self.base)
        self._write_version({"sha": sha, "message": message, "ts": time.time()})
        open(os.path.join(self.data, "update_pending"), "w").close()
        shutil.rmtree(tmp, ignore_errors=True)
        return "version %s installée : %s" % (sha[:7], message)

    @staticmethod
    def _find_root(tmp):
        for dirpath, dirnames, filenames in os.walk(tmp):
            if "main.py" in filenames and "microinvest" in dirnames:
                return dirpath
        raise UpdateError("l'archive ne contient pas Micro-invest (main.py introuvable)")

    def _run_tests(self, root):
        if not os.path.isdir(os.path.join(root, "tests")):
            raise UpdateError("la nouvelle version n'a pas de tests : installation refusée")
        # 1) chaque fichier Python doit être syntaxiquement correct
        for dirpath, dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.endswith(".py"):
                    path = os.path.join(dirpath, fn)
                    try:
                        with open(path, "rb") as f:
                            compile(f.read(), path, "exec")
                    except (SyntaxError, ValueError) as e:
                        raise UpdateError("erreur de syntaxe dans la nouvelle version (%s, ligne %s) : installation annulée" % (
                            fn, getattr(e, "lineno", "?")))
        # 2) les tests automatiques doivent tous passer
        try:
            env = dict(os.environ, MICROINVEST_UPDATING="1")
            r = subprocess.run([self.python, "-W", "ignore", "-m", "unittest", "discover", "-s", "tests"],
                               cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300, env=env)
        except subprocess.TimeoutExpired:
            raise UpdateError("les tests de la nouvelle version ne se terminent pas : installation annulée")
        if r.returncode != 0:
            tail = r.stdout.decode(errors="replace").strip().splitlines()[-6:]
            raise UpdateError("les tests de la nouvelle version échouent, rien n'a été changé :\n" + "\n".join(tail))

    def _backup_current(self):
        shutil.rmtree(self.backup, ignore_errors=True)
        os.makedirs(self.backup)
        self._copy_code(self.base, self.backup)
        v = self.installed()
        if v:
            with open(os.path.join(self.backup, "version.json"), "w") as f:
                json.dump(v, f)

    @staticmethod
    def _copy_code(src, dst):
        """Copie le code de src vers dst. Chaque fichier est remplacé de façon atomique
        (écriture dans un fichier temporaire puis renommage) : run.sh peut être
        remplacé pendant qu'il tourne sans risque."""
        def put(s, d):
            os.makedirs(os.path.dirname(d), exist_ok=True)
            tmpf = d + ".new"
            shutil.copyfile(s, tmpf)
            shutil.copymode(s, tmpf)
            os.replace(tmpf, d)

        for f in CODE_FILES:
            if os.path.isfile(os.path.join(src, f)):
                put(os.path.join(src, f), os.path.join(dst, f))
        for d in CODE_DIRS:
            sd = os.path.join(src, d)
            for dirpath, dirnames, filenames in os.walk(sd):
                dirnames[:] = [x for x in dirnames if x != "__pycache__"]
                for fn in filenames:
                    if fn.endswith((".pyc", ".new")):
                        continue
                    s = os.path.join(dirpath, fn)
                    put(s, os.path.join(dst, d, os.path.relpath(s, sd)))

    # ------------------------------------------------------------- rollback
    def can_rollback(self):
        return os.path.isfile(os.path.join(self.backup, "main.py"))

    def rollback(self):
        if not self.can_rollback():
            raise UpdateError("aucune version précédente sauvegardée")
        self._copy_code(self.backup, self.base)
        vb = os.path.join(self.backup, "version.json")
        if os.path.isfile(vb):
            shutil.copyfile(vb, os.path.join(self.data, "version.json"))
        else:
            try:
                os.remove(os.path.join(self.data, "version.json"))
            except OSError:
                pass
        try:
            os.remove(os.path.join(self.data, "update_pending"))
        except OSError:
            pass
        return "version précédente remise en place"
