"""Test sur le passé : rejoue la stratégie sur 12 mois de vrais prix (Revolut X).

Compare :
  - DCA intelligent : la stratégie de l'outil (crypto la plus en retard sur sa cible, achat doublé
    sur forte baisse, budget mensuel plafonné, frais 0,09 %). Les achats renforcés sont supposés acceptés ;
    les prises de bénéfices ne sont pas simulées (elles demandent ton accord).
  - DCA simple : même budget chaque semaine, réparti selon les cibles, sans aucune règle.
Les performances passées ne préjugent pas des performances futures.
"""
from .broker import BrokerError

FEE = 0.0009


def run(market, cfg, days=365):
    targets = cfg["dca"]["targets"]
    tsum = float(sum(targets.values()))
    weights = {a: w / tsum for a, w in targets.items()}
    closes = {}
    for a in targets:
        try:
            c = market.daily_closes(a, days + 31)
        except BrokerError:
            c = []
        if len(c) >= 60:
            closes[a] = c
    if not closes:
        raise BrokerError("historique de prix indisponible")
    n = min(len(c) for c in closes.values())
    closes = {a: c[-n:] for a, c in closes.items()}
    assets = list(closes)
    wsum = sum(weights[a] for a in assets)
    weights = {a: weights[a] / wsum for a in assets}
    monthly = cfg["budget"]["monthly_crypto_eur"]
    base = monthly * 12 / 52.0
    max_order = cfg["budget"]["max_order_eur"]
    dip_th = cfg["dca"]["dip_threshold_pct"] / 100.0
    mult = cfg["dca"]["dip_multiplier"]

    smart = {a: 0.0 for a in assets}
    simple = {a: 0.0 for a in assets}
    invested_smart = invested_simple = 0.0
    month_spent, month_id = 0.0, None
    dip_buys = 0
    series = []
    start = 30
    for i in range(start, n, 7):
        price = {a: closes[a][i] for a in assets}
        mid = (i - start) // 30
        if mid != month_id:
            month_id, month_spent = mid, 0.0
        # --- DCA intelligent
        values = {a: smart[a] * price[a] for a in assets}
        total_after = sum(values.values()) + base
        avg30 = {a: sum(closes[a][i - 30:i]) / 30 for a in assets}
        dips = {a: (avg30[a] - price[a]) / avg30[a] for a in assets}
        cands = [a for a in assets if dips[a] >= dip_th and values[a] <= weights[a] * total_after * 1.5]
        if cands:
            a = max(cands, key=lambda x: dips[x])
            amount = base * mult
            dip_buys += 1
        else:
            a = max(assets, key=lambda x: weights[x] * total_after - values[x])
            amount = base
        amount = min(amount, max_order, monthly - month_spent)
        if amount > 0:
            smart[a] += amount * (1 - FEE) / price[a]
            invested_smart += amount
            month_spent += amount
        # --- DCA simple (même montant hebdomadaire de base, réparti)
        for b in assets:
            simple[b] += base * weights[b] * (1 - FEE) / price[b]
        invested_simple += base
        series.append({"i": i, "smart": sum(smart[x] * price[x] for x in assets), "simple": sum(simple[x] * price[x] for x in assets),
                       "inv_smart": invested_smart, "inv_simple": invested_simple})
    last = {a: closes[a][-1] for a in assets}
    v_smart = sum(smart[a] * last[a] for a in assets)
    v_simple = sum(simple[a] * last[a] for a in assets)

    def max_dd(key, inv):
        worst = 0.0
        for p in series:
            if p[inv] > 0:
                worst = min(worst, p[key] / p[inv] - 1)
        return worst

    return {
        "days": n - start, "assets": assets, "dip_buys": dip_buys, "weeks": len(series),
        "smart": {"invested": invested_smart, "value": v_smart, "gain_pct": (v_smart / invested_smart - 1) if invested_smart else 0,
                  "worst_pct": max_dd("smart", "inv_smart")},
        "simple": {"invested": invested_simple, "value": v_simple, "gain_pct": (v_simple / invested_simple - 1) if invested_simple else 0,
                   "worst_pct": max_dd("simple", "inv_simple")},
        "series": series,
    }
