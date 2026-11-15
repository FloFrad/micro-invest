#!/bin/sh
# Lance Micro-invest en arrière-plan et le relance automatiquement s'il s'arrête.
# Utilisé par le Planificateur de tâches Synology au démarrage du NAS.
cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"
mkdir -p logs data

# 1er passage : on se détache du Planificateur de tâches (sinon DSM peut couper l'outil)
if [ -z "$MICROINVEST_DETACHED" ]; then
  rm -f logs/stop
  export MICROINVEST_DETACHED=1
  if command -v setsid >/dev/null 2>&1; then
    setsid sh "$DIR/run.sh" </dev/null >/dev/null 2>&1 &
  else
    nohup sh "$DIR/run.sh" </dev/null >/dev/null 2>&1 &
  fi
  echo "$(date) lancé en arrière-plan" >> logs/run.out
  exit 0
fi

trap '' HUP
PY=""
for p in python3 /usr/local/bin/python3 /usr/local/bin/python3.9 /usr/local/bin/python3.11 /usr/bin/python3 /bin/python3; do
  if command -v "$p" >/dev/null 2>&1; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then echo "$(date) Python 3 introuvable" >> logs/run.out; exit 1; fi

while [ ! -f logs/stop ]; do
  start=$(date +%s)
  "$PY" main.py run >> logs/run.out 2>&1
  code=$?
  dur=$(( $(date +%s) - start ))
  if [ "$code" -eq 3 ]; then exit 0; fi            # déjà en cours d'exécution
  [ -f logs/stop ] && break
  if [ "$code" -eq 4 ]; then                        # redémarrage demandé (mise à jour, /redemarrer)
    echo "$(date) redémarrage demandé" >> logs/run.out
    continue
  fi
  # La nouvelle version plante au démarrage : retour automatique à l'ancienne
  if [ -f data/update_pending ] && [ "$dur" -lt 120 ] && [ -f data/backup/last/main.py ]; then
    echo "$(date) la mise à jour plante (code $code) : retour à la version précédente" >> logs/run.out
    # Restauration avec le code de l'ancienne version (remplacement atomique, run.sh compris)
    "$PY" -c "import sys; sys.path.insert(0, 'data/backup/last'); from microinvest.updater import Updater; print(Updater('.', {}).rollback())" >> logs/run.out 2>&1
    rm -f data/update_pending
    touch data/rolled_back
    continue
  fi
  echo "$(date) arrêt (code $code), redémarrage dans 30 s" >> logs/run.out
  sleep 30
done
rm -f logs/stop
echo "$(date) arrêté par stop.sh" >> logs/run.out
