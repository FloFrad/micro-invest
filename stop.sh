#!/bin/sh
# Arrête proprement Micro-invest (sans redémarrage automatique).
cd "$(dirname "$0")" || exit 1
mkdir -p logs
touch logs/stop   # dit à run.sh de ne pas relancer
if [ -f data/run.lock ] && kill "$(cat data/run.lock)" 2>/dev/null; then
  echo "Micro-invest arrêté."
else
  echo "Micro-invest ne tournait pas (ou était en train de redémarrer : il ne repartira pas)."
fi
