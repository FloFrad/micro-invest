"""Mise en forme des messages (HTML Telegram) et des nombres à la française."""
import html
import re
import time

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
        "octobre", "novembre", "décembre"]
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
NBSP = " "  # espace fine insécable (séparateur de milliers)


def esc(x):
    return html.escape(str(x), quote=False)


def b(x):
    return "<b>%s</b>" % esc(x)


def i(x):
    return "<i>%s</i>" % esc(x)


def code(x):
    return "<code>%s</code>" % esc(x)


def pre(x):
    return "<pre>%s</pre>" % esc(x)


def num(x, decimals=2):
    s = "%.*f" % (decimals, abs(x))
    whole, _, frac = s.partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    out = NBSP.join(groups) + ("," + frac if frac else "")
    return ("-" if x < 0 and float(s) != 0 else "") + out


def eur(x, decimals=2):
    return num(x, decimals) + NBSP + "€"


def signed_eur(x):
    return ("+" if x >= 0 else "−") + eur(abs(x))


def pct(x, decimals=1):
    s = num(abs(x), decimals)
    return ("+" if x >= 0 else "−") + s + NBSP + "%"


def price(x):
    if x >= 100:
        return eur(x, 2)
    if x >= 1:
        return eur(x, 3)
    return eur(x, 6)


def bar(frac, width=10):
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return "▰" * n + "▱" * (width - n)


def trend_icon(p):
    return "🟢" if p > 0.05 else ("🔴" if p < -0.05 else "⚪")


def when(ts, now=None):
    """« aujourd'hui à 18 h », « demain à 10 h », « lundi 16/11 à 10 h »."""
    now = now or time.time()
    t, n = time.localtime(ts), time.localtime(now)
    h = "%d h" % t.tm_hour if t.tm_min == 0 else "%d h %02d" % (t.tm_hour, t.tm_min)
    days = (time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
            - time.mktime((n.tm_year, n.tm_mon, n.tm_mday, 0, 0, 0, 0, 0, -1))) / 86400
    if round(days) == 0:
        return "aujourd'hui à " + h
    if round(days) == 1:
        return "demain à " + h
    return "%s %s à %s" % (JOURS[t.tm_wday], time.strftime("%d/%m", t), h)


def month_name(ts=None):
    return MOIS[time.localtime(ts or time.time()).tm_mon - 1]


def strip_tags(text):
    """Version texte simple d'un message HTML (secours si Telegram refuse le HTML)."""
    return html.unescape(re.sub(r"</?(b|i|code|pre|u|s)>", "", text))


def table(rows, align=None):
    """Tableau aligné en police fixe (pour <pre>). rows : liste de listes de chaînes."""
    if not rows:
        return ""
    widths = [max(len(str(r[c])) for r in rows) for c in range(len(rows[0]))]
    align = align or ["<"] + [">"] * (len(widths) - 1)
    out = []
    for r in rows:
        cells = []
        for c, v in enumerate(r):
            v = str(v)
            cells.append(v.ljust(widths[c]) if align[c] == "<" else v.rjust(widths[c]))
        out.append("  ".join(cells).rstrip())
    return "\n".join(out)


def teur(x, decimals=2):
    """Montant pour les tableaux en police fixe (espaces normales)."""
    return num(x, decimals).replace(NBSP, " ") + "€"


def tpct(x):
    return ("+" if x >= 0 else "-") + ("%.1f" % abs(x)).replace(".", ",") + "%"
