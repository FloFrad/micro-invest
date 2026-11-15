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
- **ETF (actions) le 5 de chaque mois** : Revolut n'a pas d'API pour les actions. Le bot t'envoie donc un message à 18 h : « achète 15 € d'IWDA dans l'appli ». Tu le fais en 30 secondes, puis tu appuies sur **Fait**.
- **Chaque dimanche à 19 h** : il t'envoie un rapport sur Telegram.
- **Tout se pilote depuis Telegram** avec des boutons : achats, pause, réglages, clé Revolut X, mises à jour. L'interface web ne sert qu'à consulter, et seulement depuis le Wi-Fi de la maison.

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

1. Sur Telegram : **⚙️ Menu** → **🌐 Interface web**. Le bot t'envoie l'adresse, par exemple `http://192.168.1.50:8088`.
2. Ouvre-la depuis un appareil connecté au **Wi-Fi de la maison**. Identifiant : `admin`. Mot de passe : celui de la phase 3.
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
| ⚙️ Menu | Tout le reste : historique, journal, ETF, budget, réglages, mode, mise à jour, version précédente, redémarrer, interface web, clé Revolut X |

Si les boutons ont disparu, envoie `/start`.

---

## Phase 8 — Passer en réel avec Revolut X (15 min, tout depuis Telegram)

1. **Ouvrir Revolut X** : sur ton ordinateur, va sur `exchange.revolut.com` et connecte-toi avec l'appli Revolut (QR code).
2. **Alimenter en euros** : dans Revolut X → *Déposer* → EUR depuis ton compte Revolut. Conseil : 70 € (2 mois de budget crypto). L'outil n'utilisera jamais plus que ce solde.
3. **Clé de signature** : sur Telegram, **⚙️ Menu** → **🔑 Clé Revolut X**. Le NAS crée la clé et t'envoie la partie publique, qui peut être partagée sans risque. Copie tout le bloc, de `-----BEGIN PUBLIC KEY-----` à `-----END PUBLIC KEY-----`.
4. **Clé API** : dans Revolut X → *Profil* → *API keys* → *Créer* → colle la clé publique → nom : `microinvest` → autorise le **trading**, rien d'autre. Copie la **clé API** affichée.
5. **L'envoyer au bot** :

```
/cleapi LA_CLE_API
```

   Le bot vérifie la clé auprès de Revolut X, l'enregistre et **efface ton message**. Attendu : « ✅ Clé API valide … Solde EUR : 70,00 € ».
6. **Basculer** : **⚙️ Menu** → **🔁 Mode simu/réel** → **💶 Passer en réel** → confirme.
7. **Premier ordre de contrôle** : bouton **🛒 Acheter** → **Oui**. Vérifie dans Revolut X que l'achat apparaît.

La simulation et le réel ont des historiques séparés (`data/paper.db` et `data/live.db`).

✅ Phase 8 terminée quand le premier achat réel apparaît dans Revolut X **et** sur Telegram.

---

## Mises à jour (30 secondes)

Quand je publie une nouvelle version sur GitHub :
1. **⚙️ Menu** → **⬆️ Mise à jour**.
2. Le bot affiche la nouvelle version et ce qu'elle change → **⬆️ Installer**.
3. Le NAS télécharge la version, vérifie la syntaxe et lance les **tests automatiques**. Si un seul test échoue, **rien n'est changé**.
4. Il sauvegarde la version actuelle, installe la nouvelle et redémarre (« 🚀 démarré — Version : … »).
5. Au bout de 90 secondes : « ✅ Mise à jour validée ». Si la nouvelle version plante au démarrage, l'ancienne est **remise automatiquement** et tu reçois une alerte.

Revenir en arrière à la main : **⚙️ Menu** → **↩️ Version précédente**.
Ce qui n'est jamais modifié par une mise à jour : `config.json`, `keys/`, `data/`, `logs/`.

---

## L'ETF chaque mois (30 secondes)

Le 5 du mois à 18 h, le bot t'envoie :
1. Ouvre l'appli Revolut → *Investir*.
2. Cherche **IWDA** (iShares Core MSCI World).
3. *Acheter* → 15 € → *Confirmer*.
4. Reviens sur Telegram → **✅ Fait**.
5. Le bot te demande ensuite la valeur totale de ta ligne ETF : réponds `/etfvaleur 123.45`.

Sans réponse au bout de 24 h, il te le rappelle une fois. Tu peux aussi passer par **⚙️ Menu** → **📈 ETF du mois** à tout moment.

---

## Modifier les réglages (depuis Telegram)

1. **⚙️ Menu** → **🔧 Réglages** : le bot affiche chaque réglage, sa valeur et son nom.
2. Change une valeur avec `/regler nom valeur`. C'est appliqué tout de suite, sans redémarrage :

| Exemple | Effet |
|---|---|
| `/regler budget_crypto 40` | Budget crypto : 40 €/mois |
| `/regler budget_etf 10` | Budget ETF : 10 €/mois |
| `/regler max_ordre 20` | Plafond absolu par ordre |
| `/regler validation_au_dela 10` | Au-delà, le bot demande ton accord |
| `/regler reserve 20` | 20 € jamais utilisés sur Revolut X |
| `/regler jour_achat mercredi` | Jour de l'achat hebdo |
| `/regler heure_achat 9` | Heure de l'achat hebdo |
| `/regler plafond_perte 25` | Perte (%) qui gèle les achats |
| `/regler prise_benefices 40` | Gain (%) qui déclenche une proposition de vente |
| `/regler prise_benefices_active non` | Plus aucune proposition de vente |
| `/regler jour_etf 5` | Jour du rappel ETF |
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
