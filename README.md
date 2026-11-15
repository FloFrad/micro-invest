# Micro-invest

Outil autonome de micro-investissement : crypto 100 % automatique via **Revolut X**, ETF en semi-auto via l'appli Revolut, pilotage complet par **Telegram** (boutons, réglages, mises à jour), tableau de bord **web** consultable à la maison.

- Python 3.8+ pur, **aucune dépendance** (tourne sur un Synology sans Docker).
- Démarre en **simulation** (vrais prix, faux argent).
- Garde-fous : jamais de découvert, budget mensuel plafonné, jamais de vente à perte, gel des achats à -25 %.
- Radar quotidien : note sur 100 de toutes les cryptos Revolut X et d'une trentaine d'actions/ETF, ajout au plan en un clic.
- Mises à jour depuis Telegram : tests automatiques avant installation, retour automatique à l'ancienne version si la nouvelle plante.

👉 Mise en place pas à pas : **[GUIDE.md](GUIDE.md)**

```
python3 main.py setup    # configuration guidée
python3 main.py check    # vérification
python3 main.py run      # lancement
```
