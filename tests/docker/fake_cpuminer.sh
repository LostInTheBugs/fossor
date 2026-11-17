#!/bin/sh
# Faux cpuminer-opt : imprime le format réel « TTF @ 237.8 Mh/s » sur sa sortie
# standard.  Le fichier de log est celui de la redirection du lanceur, donc
# retrouvé par /proc/<pid>/fd/1 — aucun chemin n'est écrit en dur.
i=0
while :; do
  i=$((i + 1))
  echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] yespower 1.0 block 21458, diff 128.421"
  echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] TTF @ 237.8 Mh/s: Block 0s, Share 0s"
  echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] accepted: $i/$i (100.0%), 237.8 Mh/s yes!"
  sleep 2
done
