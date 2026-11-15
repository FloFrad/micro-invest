# Micro-invest : guide de mise en place (Synology)

**Durée totale : environ 50 min**, réparties en 8 phases courtes. Tu peux faire une pause entre deux phases.
Les phases 1 à 6 se font **sans argent réel** (mode simulation). Le réel arrive à la phase 8, après 2 semaines de test.

| Phase | Quoi | Durée |
|---|---|---|
| 1 | Préparer le NAS | 15 min |
| 2 | Créer le bot Telegram | 5 min |
| 3 | Assistant de configuration | 5 min |
| 4 | Vérifier et lancer un premier essai | 5 min |
| 5 | Démarrage automatique | 5 min |
| 6 | Interface web à la maison + mises à jour par Telegram | 5 min |
| 7 | 2 semaines de simulation | 0 min (ça tourne seul) |
| 8 | Passer en réel avec Revolut X | 20 min |

---

## Ce que fait l'outil

- **Chaque lundi à 10 h** : il achète automatiquement ≈ 8 € de crypto (35 €/mois ÷ 4,33 semaines).
  Il choisit la crypto la plus en retard sur ta répartition cible : BTC 40 %, ETH 25 %, SOL 10 %, XRP 10 %, ADA 7,5 %, LINK 7,5 %.
- **Si une crypto a baissé de 10 % ou plus** par rapport à sa moyenne sur 30 jours : il propose d'en acheter le double (≈ 16 €). Il te demande d'abord sur Telegram, parce que ça dépasse 10 €.
- **Forte baisse (15 % ou plus)** : il te demande si tu veux faire un achat en plus.
- **Prise de bénéfices** : si une crypto monte à +40 % au-dessus de ton prix moyen, il te propose d'en vendre 25 %. Il ne vend jamais sans ton « Oui ».
- **Actions/ETF le 5 de chaque mois** : Revolut n'a pas d'API pour les actions. Le bot t'envoie donc à 18 h la liste à acheter dans l'appli (par défaut 15 € d'IWDA). Tu le fais en 1 minute, puis tu appuies sur **Fait**.
- **Radar chaque matin** : il note toutes les cryptos de Revolut X et une trentaine d'actions/ETF, et te propose d'ajouter les mieux notées en un clic.
- **Chaque dimanche à 19 h** : il t'envoie un rapport sur Telegram.
- **Tout se pilote depuis Telegram ou depuis l'interface web** (à la maison), avec des boutons : achats, pause, réglages, Radar, objectif, alertes, export fiscal, sauvegarde, clé Revolut X, mises à jour.
- **Il veille pour toi** : message chaque matin (preuve qu'il tourne), alerte quand le solde Revolut X baisse, sauvegarde chaque nuit, vérification quotidienne que ses comptes correspondent à Revolut X.

### Qui fait quoi : l'outil et toi

| | Ce que fait l'outil, seul | Ce que tu fais | Temps pour toi |
|---|---|---|---|
| **Achats crypto** | Achat chaque semaine, choix de la crypto, garde-fous | Rien (ou « 🛒 Acheter » / « ✏️ Répartir » si tu veux décider) | 0 |
| **Achats renforcés, opportunités** | Détecte les baisses, calcule le montant | Répondre ✅ / ❌ | ~10 s, 1 à 3 fois par mois |
| **Ventes (prise de bénéfices)** | Détecte les gains de +40 %, ne vend jamais à perte | Répondre ✅ / ❌ | ~10 s, rare |
| **Actions/ETF** | Rappel le 5 du mois, montants par ligne, suivi | Acheter dans l'appli Revolut, puis « ✅ Fait » | ~2 min par mois |
| **Argent sur Revolut X** | Alerte quand le solde baisse | Déposer des euros sur Revolut X | ~2 min, tous les 1 à 2 mois |
| **Surveillance** | Message du matin, gel à −25 %, vérification Revolut X, sauvegarde de nuit, Radar | Lire les alertes | 0 |
| **Décisions de fond** | Radar, laboratoire, rapport du dimanche | Choisir les cibles, ajouter ou retirer des actifs, appliquer des réglages | Quand tu veux |
| **Impôts** | Export CSV et calcul des plus-values | Reporter dans la déclaration (2086 si ventes, 3916-bis) | ~15 min par an |
| **Mises à jour** | Tests, installation, retour arrière automatique | Appuyer sur « ⬆️ Installer » | ~30 s |

En tout : **environ 5 minutes par mois**, plus quelques réponses ✅ / ❌.

### « Jamais en négatif » : ce qui est garanti, ce qui ne l'est pas

Garanti par le code (et testé) :
1. **Jamais de découvert ni de dette.** Avant chaque achat, l'outil vérifie le solde EUR disponible sur Revolut X. Il n'utilise ni levier ni emprunt (Revolut X ne fait que du comptant de toute façon).
2. **Jamais plus de 35 €/mois** en crypto, et jamais plus de 20 € par ordre.
3. **Jamais de vente à perte.** Une vente est annulée si le prix repasse sous ton prix moyen d'achat.
4. **Gel automatique** : si le portefeuille crypto perd 25 %, tous les achats s'arrêtent et tu reçois une alerte. Rien n'est vendu.
5. L'outil ne touche **que** les cryptos qu'il a achetées lui-même. Tes autres avoirs restent intacts.

Pas garanti : **la valeur** de tes cryptos peut baisser, comme pour tout investissement. Tu ne peux pas perdre plus que ce que tu as versé sur Revolut X, mais tu peux perdre une partie de ce montant.

---

## Phase 1 — Préparer le NAS (15 min)

1. **Activer SSH** : DSM → *Panneau de configuration* → *Terminal & SNMP* → coche **Activer le service SSH** → *Appliquer*.
2. **Installer Python** : *Centre de paquets* → cherche **Python** → installe **Python 3.9** (ou plus récent). S'il est déjà installé, passe à l'étape suivante.
3. **Créer un dossier** : *Panneau de configuration* → *Dossier partagé* → *Créer* → nom : `bots` → *Suivant* jusqu'au bout. (Ou utilise un dossier partagé existant, comme `docker`.)
4. **Copier l'outil** : *File Station* → ouvre `docker` → *Charger* → choisis `microinvest.zip` → clic droit sur le fichier → *Extraire* → *Extraire ici*.
   Tu dois voir un dossier `docker/microinvest`.
5. **Se connecter en SSH** depuis ton ordinateur :
   - Windows : ouvre **PowerShell**. Mac : ouvre **Terminal**.
   - Tape la commande ci-dessous en remplaçant `TON_UTILISATEUR` par ton identifiant DSM et `192.168.1.50` par l'adresse IP du NAS (visible dans *Panneau de configuration* → *Réseau*) :

```bash
ssh TON_UTILISATEUR@192.168.1.50
```

   - Réponds `yes` à la première question, puis tape ton mot de passe DSM (il ne s'affiche pas, c'est normal).
6. Va dans le dossier de l'outil :

```bash
cd /volume1/docker/microinvest
```

7. Vérifie Python :

```bash
python3 --version
```

   Résultat attendu : `Python 3.8` ou plus. Si tu vois « command not found », utilise `python3.9` à la place de `python3` dans toutes les commandes de ce guide.

✅ Phase 1 terminée quand `python3 --version` affiche une version.

---

## Phase 2 — Créer le bot Telegram (5 min)

1. Dans Telegram, cherche **@BotFather** (badge bleu certifié) → *Démarrer*.
2. Envoie `/newbot`.
3. Donne un nom, par exemple `Mon Micro-invest`.
4. Donne un identifiant qui finit par `bot`, par exemple `flo_microinvest_bot`.
5. BotFather répond avec un **token** du type `7123456789:AAH...`. Garde ce message ouvert.

⚠️ Ce token donne le contrôle du bot. Ne le partage avec personne.

---

## Phase 3 — Assistant de configuration (5 min)

Dans la fenêtre SSH :

```bash
python3 main.py setup
```

L'assistant pose 4 séries de questions :
1. **Telegram** : colle le token. Ensuite, sur ton téléphone, ouvre ton bot, appuie sur **Démarrer**, puis reviens appuyer sur Entrée. Tu reçois « ✅ Micro-invest est relié à ce chat ».
2. **Interface web** : un mot de passe est proposé. Appuie sur Entrée pour le garder et **note-le**.
3. **Budget** : appuie deux fois sur Entrée pour garder 35 € crypto + 15 € ETF.
4. **Revolut X** : laisse vide pour l'instant (simulation). Appuie sur Entrée.

✅ Phase 3 terminée quand tu vois « config.json enregistré ».

---

## Phase 4 — Vérifier et faire un premier essai (5 min)

1. Lance la vérification :

```bash
python3 main.py check
```

   Résultat attendu : des ✅ partout, sauf un ⚠️ « pas encore de clé API » (normal en simulation), et un message de test sur Telegram.
2. Lance l'outil pour essayer :

```bash
python3 main.py run
```

3. Sur Telegram, tu reçois « 🚀 Micro-invest démarré — mode SIMULATION ». Envoie `/statut` au bot : il répond.
4. Dans le navigateur de ton ordinateur, ouvre `http://192.168.1.50:8088` (avec l'IP de ton NAS). Identifiant : `admin`. Mot de passe : celui de la phase 3.
5. Arrête l'essai avec **Ctrl + C** dans la fenêtre SSH.

✅ Phase 4 terminée quand le bot répond et que la page web s'affiche.

---

## Phase 5 — Démarrage automatique (5 min)

Pour que l'outil tourne 24 h/24 et redémarre avec le NAS :

1. DSM → *Panneau de configuration* → *Planificateur de tâches* → *Créer* → *Tâche déclenchée* → *Script défini par l'utilisateur*.
2. Onglet **Général** :
   - Nom : `Micro-invest`
   - Utilisateur : **ton identifiant DSM** (le même que pour SSH)
   - Événement : **Démarrage**
   - Coche **Activé**
3. Onglet **Paramètres de tâche** → *Script défini par l'utilisateur* :

```bash
sh /volume1/docker/microinvest/run.sh
```

4. *OK*. Ensuite, sélectionne la tâche dans la liste → **Exécuter** (pour la démarrer tout de suite sans redémarrer le NAS).
5. Sur Telegram, tu reçois « 🚀 Micro-invest démarré ».

Si l'outil plante, `run.sh` le relance automatiquement après 30 secondes.

✅ Phase 5 terminée quand tu reçois le message de démarrage.

---

## Phase 6 — Interface web à la maison + mises à jour par Telegram (5 min)

### 6a. Interface web (uniquement à la maison)

L'interface refuse toute connexion qui ne vient pas de ton réseau local. Il n'y a rien à ouvrir sur ta box.

1. Sur Telegram : **⚙️ Menu** → **⚙️ Système** → **🌐 Interface web**. Le bot t'envoie l'adresse, par exemple `http://192.168.1.50:8088`.
2. Ouvre-la depuis un appareil connecté au **Wi-Fi de la maison**. Identifiant : `admin`. Mot de passe : celui de la phase 3.
3. Tu y trouves 5 onglets : **Portefeuille**, **Radar**, **Actions** (tous les boutons de commande), **Historique**, **Réglages** (modifiables directement). Tout ce qui se fait sur Telegram se fait aussi ici.
3. Si tu avais créé une règle de *proxy inversé* ou une redirection de port pour Micro-invest, supprime-la.

### 6b. Brancher les mises à jour (une seule fois)

Les nouvelles versions sont publiées sur ton dépôt GitHub `FloFrad/micro-invest`. Le NAS les télécharge quand tu le demandes.

- **Dépôt public** : envoie au bot

```
/depot FloFrad/micro-invest
```

- **Dépôt privé** : crée d'abord un jeton en lecture seule sur GitHub : *Settings* → *Developer settings* → *Fine-grained tokens* → *Generate new token* → *Repository access* : seulement `micro-invest` → *Permissions* : **Contents : Read-only** → *Generate*. Puis envoie :

```
/depot FloFrad/micro-invest LE_JETON
```

  Le bot efface aussitôt ton message, qui contient le jeton.

Résultat attendu : « ✅ Dépôt enregistré … Dernière version disponible : … ».

✅ Phase 6 terminée quand la page s'affiche à la maison **et** que `/depot` répond ✅.

---

## Phase 7 — 2 semaines de simulation (rien à faire)

L'outil fonctionne avec **les vrais prix de Revolut X** et **100 € fictifs**. Pendant ce temps :
- Lundi 10 h : tu reçois « ✅ Acheté 8,08 € de BTC… ».
- Réponds aux questions avec les boutons ✅ / ❌ pour voir comment ça marche.

### Les boutons

En bas de l'écran (toujours visibles) :

| Bouton | Effet |
|---|---|
| 📊 Statut | État, budget, prochain achat |
| 💼 Portefeuille | Positions et gains |
| 🛒 Acheter | Fait l'achat de la semaine maintenant (avec confirmation) |
| ❓ Questions | Réaffiche les questions en attente |
| ⏸ Pause / ▶️ Reprendre | Suspend ou relance les achats automatiques |
| ⚙️ Menu | 🔭 Radar · 📈 Plan actions · 🎯 Objectif · 🔔 Alertes · 📉 Graphique · 📜 Historique · 🧰 Outils (budget, vérification, sauvegarde, export fiscal, test 12 mois, journal) · 🔧 Réglages · ⚙️ Système (mise à jour, version, redémarrer, mode, clé, web) |

Si les boutons ont disparu, envoie `/start`.

---

## Phase 8 — Passer en réel avec Revolut X (15 min, tout depuis Telegram)

1. **Ouvrir Revolut X** : sur ton ordinateur, va sur `exchange.revolut.com` et connecte-toi avec l'appli Revolut (QR code).
2. **Alimenter en euros** : dans Revolut X → *Déposer* → EUR depuis ton compte Revolut. Conseil : 70 € (2 mois de budget crypto). L'outil n'utilisera jamais plus que ce solde.
3. **Clé de signature** : sur Telegram, **⚙️ Menu** → **⚙️ Système** → **🔑 Clé Revolut X** (ou onglet **Actions** du web). Le NAS crée la clé et t'envoie la partie publique, qui peut être partagée sans risque. Copie tout le bloc, de `-----BEGIN PUBLIC KEY-----` à `-----END PUBLIC KEY-----`.
4. **Clé API** : dans Revolut X → *Profil* → *API keys* → *Créer* → colle la clé publique → nom : `microinvest` → autorise le **trading**, rien d'autre. Copie la **clé API** affichée.
5. **L'envoyer au bot** :

```
/cleapi LA_CLE_API
```

   Le bot vérifie la clé auprès de Revolut X, l'enregistre et **efface ton message**. Attendu : « ✅ Clé API valide … Solde EUR : 70,00 € ».
6. **Vérifier** : **⚙️ Menu** → **🧰 Outils** → **🔍 Vérifier Revolut X**.
7. **Basculer** : **⚙️ Menu** → **⚙️ Système** → **🔁 Mode simu/réel** → **💶 Passer en réel** → confirme.
8. **Premier ordre de contrôle** : bouton **🛒 Acheter** → **Oui**. Vérifie dans Revolut X que l'achat apparaît.

La simulation et le réel ont des historiques séparés (`data/paper.db` et `data/live.db`).

✅ Phase 8 terminée quand le premier achat réel apparaît dans Revolut X **et** sur Telegram.

---

## Mises à jour (30 secondes)

Quand je publie une nouvelle version sur GitHub :
1. **⚙️ Menu** → **⚙️ Système** → **⬆️ Mise à jour** (ou onglet **Actions** du web).
2. Le bot affiche la nouvelle version et ce qu'elle change → **⬆️ Installer**.
3. Le NAS télécharge la version, vérifie la syntaxe et lance les **tests automatiques**. Si un seul test échoue, **rien n'est changé**.
4. Il sauvegarde la version actuelle, installe la nouvelle et redémarre (« 🚀 démarré — Version : … »).
5. Au bout de 90 secondes : « ✅ Mise à jour validée ». Si la nouvelle version plante au démarrage, l'ancienne est **remise automatiquement** et tu reçois une alerte.

Revenir en arrière à la main : **⚙️ Système** → **↩️ Version précédente**.
Ce qui n'est jamais modifié par une mise à jour : `config.json`, `keys/`, `data/`, `logs/`.

---

## Le Radar : quelles cryptos et actions regarder (30 secondes)

Chaque matin à 7 h, le Radar analyse **toutes les cryptos échangeables en euros sur Revolut X** et **une trentaine d'actions et d'ETF** disponibles chez Revolut : les grands ETF (Monde, S&P 500, Nasdaq, émergents, Europe), les géants US (Apple, Microsoft, Nvidia…) et les grandes valeurs européennes (ASML, LVMH, Airbus…).

1. **⚙️ Menu** → **🔭 Radar** : les 5 cryptos et les 5 actions/ETF les mieux notées, avec la raison principale de chaque note.
2. Sous le message, **➕ SYMBOLE** ajoute un actif à ton plan (une confirmation est demandée).
3. **🪙 Classement crypto** / **📈 Classement actions** : le classement complet.
4. `/info SOL` : l'analyse détaillée d'un actif (prix, 30 j, 90 j, plus haut sur 1 an, volatilité).

**Comment la note sur 100 est calculée** (aucune prédiction, que des règles vérifiables) :

| Critère | Points | Idée |
|---|---|---|
| Tendance | 40 | Prix au-dessus de sa moyenne sur 200 jours, et la moyenne 50 j au-dessus de la moyenne 200 j |
| Élan | 20 | Performance sur 90 jours |
| Point d'entrée | 25 | Repli de 3 à 15 % sous la moyenne 30 j dans une tendance haussière (« acheter la baisse d'un actif qui monte ») |
| Risque | 15 | Plus l'actif est calme (volatilité faible), plus il a de points |

Étiquettes : ⭐ Intéressant (65 et plus) · 👀 À surveiller (45 à 64) · 😐 Neutre · ⛔ À éviter (forte baisse, tendance cassée) · 💧 Peu liquide (trop peu échangée, écartée).
Les stablecoins (USDT, USDC…) sont ignorés. Le Radar ne fait **jamais** d'achat tout seul : il propose, et c'est toi qui décides.

---

## Le plan actions/ETF chaque mois (1 minute)

Revolut n'a pas d'API pour les actions : cette partie reste manuelle, mais guidée.

- Par défaut, le plan contient une seule ligne : **iShares Core MSCI World (IWDA)**.
- Ajoute des lignes avec **➕** depuis le Radar, ou avec `/cible_action NVDA 20`. Pour en retirer une : `/cible_action NVDA 0`. Voir le plan : **⚙️ Menu** → **📈 Plan actions/ETF**.
- Le 5 du mois à 18 h, le bot t'envoie la liste à acheter, avec un montant par ligne : au maximum 3 lignes de 5 € minimum, en priorité celles qui sont en retard sur leur cible.
  1. Ouvre l'appli Revolut → *Investir*.
  2. Achète chaque ligne indiquée (le nom à chercher est donné).
  3. Reviens sur Telegram → **✅ Fait**.
  4. Le bot te demande ensuite la valeur totale de tes actions/ETF : réponds `/etfvaleur 123.45`.
- Sans réponse au bout de 24 h, il te le rappelle une fois.
- **Déclarer un achat sur une ligne précise** (autre montant, achat en plus, action hors plan) : web, onglet **Portefeuille** → carte **Plan actions/ETF** → choisis la ligne dans la liste, saisis le montant, puis **Déclarer cet achat**. Sur Telegram : `/etf 12.50 NVDA`, ou le bouton de la ligne sous `/etf`.

---

## Ce que l'outil surveille tout seul

| Quand | Quoi | Si problème |
|---|---|---|
| Chaque matin à 8 h | ☀️ Message « Micro-invest tourne ✅ » + programme du jour | **Pas de message = l'outil est arrêté** : Planificateur de tâches → `Micro-invest` → Exécuter |
| Toutes les heures | Solde EUR sur Revolut X | 🏦 Alerte si le solde couvre moins de 2 semaines d'achats |
| Chaque nuit à 3 h | 🛟 Sauvegarde complète dans `backups/` sur le NAS (14 gardées) | Alerte si la sauvegarde échoue |
| Chaque matin à 9 h | 🔍 Comparaison de ses comptes avec Revolut X | Alerte + bouton « Aligner » en cas d'écart (vente ou retrait manuel) |
| Toutes les 5 min | 🔔 Tes alertes de prix | Message dès qu'un seuil est franchi |

---

## Les outils (Telegram : ⚙️ Menu → 🧰 Outils · Web : onglet Actions)

- **🎯 Objectif** : `/objectif 1000 2027-06` → progression et effort mensuel nécessaire (affichés aussi chaque matin).
- **🔔 Alertes de prix** : `/alerte BTC < 55000`, `/alerte ETH > 4000` ; la liste et les 🗑 avec `/alertes`.
- **📉 Graphique** : courbe de ton portefeuille en image (30 j, 90 j ou tout). Envoyée aussi chaque dimanche avec le rapport.
- **🧪 Test sur 12 mois** : rejoue ta stratégie sur les vrais prix de l'année passée et la compare à un achat fixe.
- **🧾 Export fiscal** : `/export 2026` → 2 fichiers CSV (toutes les opérations, plus-values crypto selon la méthode du formulaire 2086) + le résumé (exonération sous 305 € de ventes, impôt estimé).
- **🛟 Sauvegarde** : sauvegarde complète sur le NAS + copie **sans aucun secret** envoyée sur Telegram (ou téléchargeable sur le web).

### Choisir toi-même la répartition de la part de la semaine

- Telegram : « 🛒 Acheter » → **✏️ Répartir autrement** → choisis une répartition (selon tes cibles, tout sur une crypto, moitié-moitié), ou écris la tienne : `/acheter BTC:5 ETH:3`, ou en % de la part de la semaine : `/acheter BTC:60% SOL:40%`.
- Web : onglet **Actions** → **✏️ Répartir moi-même** → saisis un montant par crypto (bouton **📊 Selon mes cibles** pour préremplir), puis **🛒 Acheter cette répartition**.
- Un résumé est toujours affiché avant l'achat. Les garde-fous s'appliquent (budget du mois, maximum par ordre, solde disponible). Cet achat **remplace l'achat automatique de la semaine**.

### Laboratoire : tester d'autres réglages sur le passé

Web : onglet **Actions** → carte **Laboratoire**. Change le budget, le plafond par ordre, le seuil de baisse, le multiplicateur, la période (3, 6 ou 12 mois) ou les cibles, puis **🧪 Lancer le test**.
- Chaque test s'ajoute au tableau de comparaison : la stratégie de l'outil avec ces réglages, face à un achat fixe.
- **Appliquer** remplace tes réglages actuels par ceux du test (une confirmation est demandée).
- Telegram : `/test budget=50 baisse=15 multi=1.5 cibles=BTC:60,ETH:40 mois=6`, puis `/tests` pour comparer.

### Réajuster la simulation (jamais le réel)

- **Remettre à zéro** : `/simulation reset 100` ou web, onglet **Actions** → carte **Simulation**. Les opérations simulées sont effacées et le solde fictif repart à 100 €.
- **Changer le solde fictif** : `/simulation solde 200`.
- **Annuler une opération** : web, onglet **Historique** → 🗑, ou `/annuler 12` (le n° est affiché dans `/historique`). Les soldes simulés sont rétablis.
- En mode réel, seules les déclarations d'achats d'actions/ETF peuvent être annulées. Les opérations crypto ont vraiment eu lieu sur Revolut X.

**Restaurer une sauvegarde** : arrête l'outil (`stop.sh`), décompresse `backups/microinvest-AAAA-MM-JJ.zip` dans `/volume1/docker/microinvest` (accepte de remplacer `config.json`, `data/`, `keys/`), puis relance la tâche.

---

## Modifier les réglages (Telegram ou web)

Sur le web : onglet **Réglages**, change la valeur puis **OK** (cibles crypto et plan actions modifiables au même endroit).

Sur Telegram :
1. **⚙️ Menu** → **🔧 Réglages** : le bot affiche chaque réglage, sa valeur et son nom.
2. Change une valeur avec `/regler nom valeur`. C'est appliqué tout de suite, sans redémarrage :

| Exemple | Effet |
|---|---|
| `/regler budget_crypto 40` | Budget crypto : 40 €/mois |
| `/regler budget_etf 10` | Budget actions/ETF : 10 €/mois |
| `/regler max_ordre 20` | Plafond absolu par ordre |
| `/regler validation_au_dela 10` | Au-delà, le bot demande ton accord |
| `/regler reserve 20` | 20 € jamais utilisés sur Revolut X |
| `/regler jour_achat mercredi` | Jour de l'achat hebdo |
| `/regler heure_achat 9` | Heure de l'achat hebdo |
| `/regler plafond_perte 25` | Perte (%) qui gèle les achats |
| `/regler prise_benefices 40` | Gain (%) qui déclenche une proposition de vente |
| `/regler prise_benefices_active non` | Plus aucune proposition de vente |
| `/regler jour_etf 5` | Jour du rappel actions/ETF |
| `/regler radar_heure 7` | Heure de l'analyse quotidienne du Radar |
| `/cible SOL 12` | Poids de SOL dans la répartition (`/cible SOL 0` pour la retirer) |

Une valeur hors limites est refusée avec une explication, et rien n'est changé.

---

## Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| Aucun message Telegram | L'outil ne tourne pas | *Planificateur de tâches* → `Micro-invest` → *Exécuter*. Puis lis `logs/run.out` |
| Les boutons ont disparu | Clavier masqué par Telegram | Envoie `/start` |
| « Revolut X injoignable » | NAS sans Internet ou Revolut X en maintenance | L'outil réessaie toutes les 15 min, rien à faire |
| « Revolut X refuse cette clé » | Clé mal copiée, ou clé publique différente | Refais la phase 8, étapes 3 à 5 |
| « solde insuffisant » | Plus assez d'euros sur Revolut X | Dépose des euros sur Revolut X |
| « Dépôt inaccessible » | Dépôt privé sans jeton, ou jeton expiré | Refais `/depot FloFrad/micro-invest NOUVEAU_JETON` |
| « Mise à jour annulée : les tests échouent » | La nouvelle version a un défaut | Rien n'a changé. Préviens-moi, je corrige et je republie |
| Page web refusée (« réseau local ») | Tu n'es pas sur le Wi-Fi de la maison | Normal : l'interface n'est accessible qu'à la maison. Utilise Telegram |

Journaux détaillés : `logs/microinvest.log` (et **⚙️ Menu** → **📒 Journal**).

---

## Arrêter, sauvegarder

- **Pause sans arrêter** : bouton **⏸ Pause**.
- **Redémarrer** : **⚙️ Menu** → **🔄 Redémarrer**.
- **Arrêter complètement** (SSH) :

```bash
sh /volume1/docker/microinvest/stop.sh
```

- **Sauvegarder** : `config.json`, `keys/` et `data/` suffisent (Hyper Backup peut le faire).

---

## À savoir

- **Sécurité** : `keys/private.pem` et `config.json` donnent accès au trading. Ne les partage jamais. La clé API ne permet pas de retirer de l'argent vers l'extérieur si tu ne coches que le trading. Le bot n'obéit qu'à **ton** compte Telegram et ignore tous les autres.
- **Frais Revolut X** : environ 0,09 % par ordre au marché, soit moins d'un centime sur 8 €.
- **Impôts (France)** : les ventes de crypto contre euros sont imposables (flat tax de 30 % sur la plus-value). Les comptes crypto détenus à l'étranger se déclarent chaque année (formulaire 3916-bis). L'historique est dans l'interface web et dans `data/live.db`.
- Ceci est un outil d'automatisation, pas un conseil en investissement.
