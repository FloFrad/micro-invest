"""Stockage local SQLite : opérations, validations, état, historique."""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, asset TEXT, side TEXT, qty REAL, eur REAL, price REAL,
    reason TEXT, order_id TEXT, kind TEXT DEFAULT 'crypto'
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, expires REAL, kind TEXT, payload TEXT, text TEXT,
    status TEXT DEFAULT 'pending', tg_msg INTEGER
);
CREATE TABLE IF NOT EXISTS snapshots (ts REAL, value REAL, cost REAL);
CREATE TABLE IF NOT EXISTS events (ts REAL, level TEXT, text TEXT);
"""


class Store:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
            try:  # migration : valeur globale du portefeuille au moment d'une vente (calcul fiscal)
                self.db.execute("ALTER TABLE trades ADD COLUMN portfolio_value REAL")
            except sqlite3.OperationalError:
                pass
            self.db.commit()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    # --- clé/valeur ---
    def get(self, k, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def set(self, k, v):
        self.x("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v)))

    # --- opérations ---
    def add_trade(self, asset, side, qty, eur, price, reason, order_id="", kind="crypto", portfolio_value=None):
        return self.x(
            "INSERT INTO trades (ts, asset, side, qty, eur, price, reason, order_id, kind, portfolio_value) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.clock(), asset, side, qty, eur, price, reason, order_id, kind, portfolio_value),
        )

    def all_trades(self):
        return self.q("SELECT * FROM trades ORDER BY id")

    def trades(self, limit=50, kind=None):
        if kind:
            return self.q("SELECT * FROM trades WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))
        return self.q("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    def spent_since(self, ts, kind="crypto"):
        r = self.q("SELECT COALESCE(SUM(eur),0) AS s FROM trades WHERE side='buy' AND kind=? AND ts>=?", (kind, ts))
        return float(r[0]["s"])

    def positions(self):
        """Quantité et coût moyen pondéré par actif (achats/ventes du bot uniquement)."""
        pos = {}
        for t in self.q("SELECT * FROM trades WHERE kind='crypto' ORDER BY id"):
            p = pos.setdefault(t["asset"], {"qty": 0.0, "cost": 0.0, "realized": 0.0})
            if t["side"] == "buy":
                p["qty"] += t["qty"]
                p["cost"] += t["eur"]
            else:
                avg = p["cost"] / p["qty"] if p["qty"] > 0 else 0
                sold = min(t["qty"], p["qty"])
                p["realized"] += t["eur"] - avg * sold
                p["cost"] -= avg * sold
                p["qty"] -= sold
        for p in pos.values():
            p["avg"] = p["cost"] / p["qty"] if p["qty"] > 1e-12 else 0.0
        return pos

    # --- validations ---
    def add_approval(self, kind, payload, text, hours):
        now = self.clock()
        return self.x(
            "INSERT INTO approvals (ts, expires, kind, payload, text) VALUES (?,?,?,?,?)",
            (now, now + hours * 3600, kind, json.dumps(payload), text),
        )

    def approval(self, aid):
        r = self.q("SELECT * FROM approvals WHERE id=?", (aid,))
        if not r:
            return None
        r[0]["payload"] = json.loads(r[0]["payload"])
        return r[0]

    def pending_approvals(self):
        rows = self.q("SELECT * FROM approvals WHERE status='pending' ORDER BY id")
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def set_approval(self, aid, status=None, tg_msg=None):
        if status is not None:
            self.x("UPDATE approvals SET status=? WHERE id=?", (status, aid))
        if tg_msg is not None:
            self.x("UPDATE approvals SET tg_msg=? WHERE id=?", (tg_msg, aid))

    def claim_approval(self, aid, status):
        """Passe une validation de 'pending' à status, une seule fois (anti double-clic)."""
        with self.lock:
            cur = self.db.execute("UPDATE approvals SET status=? WHERE id=? AND status='pending'", (status, aid))
            self.db.commit()
            return cur.rowcount == 1

    # --- historique / journal ---
    def snapshot(self, value, cost):
        self.x("INSERT INTO snapshots (ts, value, cost) VALUES (?,?,?)", (self.clock(), value, cost))

    def snapshots(self, since):
        return self.q("SELECT ts, value, cost FROM snapshots WHERE ts>=? ORDER BY ts", (since,))

    def event(self, level, text):
        self.x("INSERT INTO events (ts, level, text) VALUES (?,?,?)", (self.clock(), level, text))

    def events(self, limit=30):
        return self.q("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))
