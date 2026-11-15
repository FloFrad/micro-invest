"""Sauvegardes : copie datée de la configuration, des clés et des bases de données.

- Sauvegarde complète (avec secrets) : gardée uniquement sur le NAS, dans backups/.
- Copie « Telegram » : mêmes données SANS les secrets (clé API, jeton du bot,
  mot de passe web, clé privée), pour ne jamais envoyer de secret dans une conversation.
"""
import io
import json
import os
import sqlite3
import tempfile
import time
import zipfile

SECRET_PATHS = [("revolutx", "api_key"), ("telegram", "token"), ("web", "password"), ("update", "token")]
KEEP = 14  # nombre de sauvegardes conservées sur le NAS


def _db_copy(path):
    """Copie cohérente d'une base SQLite, même si elle est en cours d'utilisation."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src = sqlite3.connect(path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with open(tmp, "rb") as f:
        data = f.read()
    os.remove(tmp)
    return data


def build_zip(base_dir, with_secrets=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        cfg_path = os.path.join(base_dir, "config.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            if not with_secrets:
                for sect, key in SECRET_PATHS:
                    if isinstance(cfg.get(sect), dict) and cfg[sect].get(key):
                        cfg[sect][key] = "(retiré de cette copie)"
            z.writestr("config.json", json.dumps(cfg, indent=2, ensure_ascii=False))
        data = os.path.join(base_dir, "data")
        if os.path.isdir(data):
            for fn in sorted(os.listdir(data)):
                p = os.path.join(data, fn)
                if fn.endswith(".db"):
                    z.writestr("data/" + fn, _db_copy(p))
                elif fn == "version.json":
                    z.write(p, "data/" + fn)
        keys = os.path.join(base_dir, "keys")
        if with_secrets and os.path.isdir(keys):
            for fn in os.listdir(keys):
                z.write(os.path.join(keys, fn), "keys/" + fn)
        z.writestr("LISEZMOI.txt", (
            "Sauvegarde Micro-invest du %s.\n\nRestaurer : arrête l'outil (stop.sh), copie ces fichiers dans le dossier "
            "microinvest (config.json, data/, keys/), puis relance la tâche.\n%s" % (
                time.strftime("%d/%m/%Y %H:%M"),
                "" if with_secrets else "\nCette copie ne contient AUCUN secret : remets ta clé API, le jeton du bot et "
                                        "le mot de passe web dans config.json (ou relance « python3 main.py setup »).\n")))
    return buf.getvalue()


def save_local(base_dir, now=None):
    """Écrit backups/microinvest-AAAA-MM-JJ.zip (complète) et supprime les plus anciennes."""
    folder = os.path.join(base_dir, "backups")
    os.makedirs(folder, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    name = "microinvest-%s.zip" % time.strftime("%Y-%m-%d", time.localtime(now or time.time()))
    path = os.path.join(folder, name)
    data = build_zip(base_dir, with_secrets=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    old = sorted(f for f in os.listdir(folder) if f.startswith("microinvest-") and f.endswith(".zip"))
    for f in old[:-KEEP]:
        os.remove(os.path.join(folder, f))
    return path, len(data)


def list_local(base_dir):
    folder = os.path.join(base_dir, "backups")
    if not os.path.isdir(folder):
        return []
    return sorted((f for f in os.listdir(folder) if f.endswith(".zip")), reverse=True)
