"""Chargement et validation de config.json."""
import copy
import json
import os

DEFAULTS = {
    "mode": "paper",
    "paper_start_eur": 100,
    "quote": "EUR",
    "revolutx": {"api_key": "", "private_key_file": "keys/private.pem"},
    "telegram": {"token": "", "chat_id": ""},
    "web": {"host": "0.0.0.0", "port": 8088, "password": ""},
    "budget": {
        "monthly_crypto_eur": 35,
        "monthly_etf_eur": 15,
        "max_order_eur": 20,
        "approval_above_eur": 10,
        "keep_reserve_eur": 0,
    },
    "dca": {
        "weekday": 0,
        "hour": 10,
        "targets": {"BTC": 40, "ETH": 25, "SOL": 10, "XRP": 10, "ADA": 7.5, "LINK": 7.5},
        "dip_threshold_pct": 10,
        "dip_multiplier": 2.0,
        "opportunity_threshold_pct": 15,
    },
    "take_profit": {"enabled": True, "gain_pct": 40, "sell_fraction_pct": 25, "min_position_eur": 20},
    "safety": {"max_drawdown_pct": 25, "resume_above_pct": 15},
    "etf": {"enabled": True, "name": "iShares Core MSCI World (IWDA)", "day_of_month": 5, "hour": 18},
    "approval_timeout_hours": 12,
    "weekly_report": {"weekday": 6, "hour": 19},
    "update": {"repo": "", "branch": "main", "token": ""},
    "stocks": {"targets": {"IWDA.AS": 100}, "min_line_eur": 5, "max_lines": 3},
    "radar": {"enabled": True, "hour": 7},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "targets":  # les cibles se remplacent en bloc
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigError(Exception):
    pass


def validate(c):
    errs = []
    if c["mode"] not in ("paper", "live"):
        errs.append('mode doit valoir "paper" ou "live"')
    b = c["budget"]
    for k in ("monthly_crypto_eur", "monthly_etf_eur", "max_order_eur", "approval_above_eur", "keep_reserve_eur"):
        if not isinstance(b[k], (int, float)) or b[k] < 0:
            errs.append("budget.%s doit être un nombre positif" % k)
    if b["monthly_crypto_eur"] > 0 and b["max_order_eur"] <= 0:
        errs.append("budget.max_order_eur doit être > 0")
    t = c["dca"]["targets"]
    if not t or any((not isinstance(v, (int, float)) or v <= 0) for v in t.values()):
        errs.append("dca.targets : chaque poids doit être > 0")
    if not 0 <= c["dca"]["weekday"] <= 6:
        errs.append("dca.weekday : 0 = lundi … 6 = dimanche")
    if not 0 <= c["dca"]["hour"] <= 23:
        errs.append("dca.hour : 0 à 23")
    if c["take_profit"]["gain_pct"] < 5:
        errs.append("take_profit.gain_pct doit être ≥ 5 (sinon les frais mangent le gain)")
    if not 0 < c["take_profit"]["sell_fraction_pct"] <= 100:
        errs.append("take_profit.sell_fraction_pct : entre 1 et 100")
    if not 1 <= c["safety"]["max_drawdown_pct"] <= 90:
        errs.append("safety.max_drawdown_pct : entre 1 et 90")
    if not 1 <= c["etf"]["day_of_month"] <= 28:
        errs.append("etf.day_of_month : entre 1 et 28")
    st = c["stocks"]["targets"]
    if any((not isinstance(v, (int, float)) or v <= 0) for v in st.values()):
        errs.append("stocks.targets : chaque poids doit être > 0")
    if not 1 <= c["stocks"]["max_lines"] <= 10:
        errs.append("stocks.max_lines : entre 1 et 10")
    if not 0 <= c["radar"]["hour"] <= 23:
        errs.append("radar.hour : 0 à 23")
    repo = (c.get("update") or {}).get("repo", "")
    if repo and (repo.count("/") != 1 or " " in repo):
        errs.append('update.repo doit avoir la forme "proprietaire/nom-du-depot"')
    if errs:
        raise ConfigError("config.json invalide :\n - " + "\n - ".join(errs))
    return c


def load(path):
    if not os.path.exists(path):
        raise ConfigError("config.json introuvable. Lance d'abord : python3 main.py setup")
    with open(path, encoding="utf-8") as f:
        try:
            user = json.load(f)
        except ValueError as e:
            raise ConfigError("config.json n'est pas un JSON valide : %s" % e)
    return validate(_merge(DEFAULTS, user))


def save(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def update_file(path, dotted_key, value):
    """Modifie une valeur de config.json (ex. "budget.monthly_crypto_eur"), avec validation.

    Ne touche que la clé demandée. Si le résultat est invalide, rien n'est écrit."""
    raw = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    node = raw
    keys = dotted_key.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    if value is None:
        node.pop(keys[-1], None)
    else:
        node[keys[-1]] = value
    validate(_merge(DEFAULTS, raw))
    save(path, raw)
    return raw
