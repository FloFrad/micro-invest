"""Export des opérations (CSV) et calcul des plus-values crypto selon la méthode française.

Méthode (article 150 VH bis du CGI, formulaire 2086) : pour chaque cession de crypto contre euros,
  plus-value = prix de cession − prix total d'acquisition × prix de cession / valeur globale du portefeuille
puis le prix total d'acquisition est diminué de la fraction utilisée.

Limites, affichées à l'utilisateur :
- seules les cryptos achetées par l'outil sur Revolut X sont comptées. Si tu détiens d'autres cryptos
  (ailleurs ou achetées à la main), la valeur globale et le prix d'acquisition réels sont différents ;
- ce calcul aide à remplir la déclaration, il ne remplace pas un conseil fiscal.
"""
import csv
import io
import time

EXEMPTION = 305.0  # total des cessions de l'année en dessous duquel les plus-values sont exonérées


def _n(x, d=2):
    return ("%.*f" % (d, x)).replace(".", ",")


def _csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    for r in rows:
        w.writerow(r)
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM : accents corrects dans Excel


def operations_csv(store):
    rows = [["Date", "Heure", "Type", "Catégorie", "Actif", "Quantité", "Prix unitaire (€)", "Montant (€)", "Motif", "Référence"]]
    for t in store.all_trades():
        lt = time.localtime(t["ts"])
        if t["kind"] == "etf":
            typ, cat = "Achat (déclaré)", "Actions/ETF"
        elif t["order_id"] == "adjust":
            typ, cat = "Ajustement", "Crypto"
        else:
            typ, cat = ("Achat" if t["side"] == "buy" else "Vente"), "Crypto"
        rows.append([time.strftime("%d/%m/%Y", lt), time.strftime("%H:%M", lt), typ, cat, t["asset"],
                     _n(t["qty"], 8) if t["qty"] else "", _n(t["price"], 6) if t["price"] else "", _n(t["eur"]),
                     t["reason"], t["order_id"] or ""])
    return _csv(rows)


def plus_values(store):
    """Liste des cessions avec leur plus-value, dans l'ordre chronologique."""
    total_acq = 0.0
    held = {}
    cessions = []
    for t in store.all_trades():
        if t["kind"] != "crypto":
            continue
        if t["side"] == "buy":
            total_acq += t["eur"]
            held[t["asset"]] = held.get(t["asset"], 0.0) + t["qty"]
            continue
        held[t["asset"]] = max(0.0, held.get(t["asset"], 0.0) - t["qty"])
        if t["order_id"] == "adjust":
            continue  # sortie hors outil (retrait, vente manuelle) : pas une cession calculable ici
        price = t["eur"]
        value = t.get("portfolio_value")
        estimated = False
        if not value or value < price:
            value = max(price, value or 0.0)
            estimated = True
        frac = total_acq * price / value if value else 0.0
        pv = price - frac
        cessions.append({"ts": t["ts"], "year": time.localtime(t["ts"]).tm_year, "asset": t["asset"], "qty": t["qty"],
                         "price": price, "portfolio_value": value, "acq_before": total_acq, "acq_fraction": frac,
                         "pv": pv, "estimated": estimated})
        total_acq -= frac
    return cessions


def year_summary(store, year):
    cs = [c for c in plus_values(store) if c["year"] == year]
    total = sum(c["price"] for c in cs)
    pv = sum(c["pv"] for c in cs)
    return {"year": year, "cessions": cs, "total_cessions": total, "pv": pv, "exempt": total <= EXEMPTION,
            "tax_estimate": 0.0 if total <= EXEMPTION or pv <= 0 else pv * 0.30}


def plus_values_csv(store, year):
    s = year_summary(store, year)
    rows = [["Cession", "Date (211)", "Actif", "Quantité", "Valeur globale du portefeuille (212)", "Prix de cession net de frais (213-215)",
             "Prix total d'acquisition avant cession (220)", "Fraction du prix d'acquisition (223 calc.)", "Plus ou moins-value", "Remarque"]]
    for i, c in enumerate(s["cessions"], 1):
        rows.append([i, time.strftime("%d/%m/%Y", time.localtime(c["ts"])), c["asset"], _n(c["qty"], 8), _n(c["portfolio_value"]),
                     _n(c["price"]), _n(c["acq_before"]), _n(c["acq_fraction"]), _n(c["pv"]),
                     "valeur globale estimée" if c["estimated"] else ""])
    rows.append([])
    rows.append(["Total des cessions", "", "", "", "", _n(s["total_cessions"]), "", "", _n(s["pv"]),
                 "exonéré (total des cessions ≤ 305 €)" if s["exempt"] else "imposable (flat tax 30 % si plus-value)"])
    rows.append(["Attention : seules les cryptos de l'outil sont comptées. Ajoute tes autres cryptos si tu en as. Aide au calcul, pas un conseil fiscal."])
    return _csv(rows)
