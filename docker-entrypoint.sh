#!/bin/sh
# §4.18 — point de restauration du retour arrière.
#
# L'updater ne monte `/data` qu'en lecture seule : quand le contrôle de santé
# échoue et que la version fautive a migré le schéma, il recrée ce service avec
# FOSSOR_RESTORE_BACKUP=<data>/backups/pre-<cible>-<horodatage>. C'est donc
# l'application elle-même qui remet sa base en place, avant que le code restauré
# (l'ancien) ne la lise. Le marqueur `.restored` rend l'opération idempotente.
set -e

backup="${FOSSOR_RESTORE_BACKUP:-}"
db="${FOSSOR_DB_PATH:-/data/fossor.db}"

if [ -n "$backup" ] && [ -d "$backup" ] && [ -f "$backup/fossor.db" ] && [ ! -f "$backup/.restored" ]; then
  echo "fossor: restauration de la base depuis $backup"
  if [ -f "$db" ]; then
    cp "$db" "$db.failed-$(date +%Y%m%dT%H%M%S)"
  fi
  cp "$backup/fossor.db" "$db"
  touch "$backup/.restored"
fi

exec "$@"
