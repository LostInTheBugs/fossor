#!/usr/bin/env bash
# Garde-fou de release : le contenu de VERSION dans l'arbre du tag doit être
# exactement le nom du tag.
#
# Motif : le tag 2026.11.003-c8 a été déplacé sur un commit de main (VERSION
# « 2026.11.004-dev ») et le workflow a republié l'image « :2026.11.003-c8 »,
# qui annonçait donc une autre version ; l'updater l'a refusée au contrôle de
# santé et la prod est revenue à c7. Un tag publié ne se déplace jamais : les
# tests ajoutés après coup vont dans la version suivante.
#
# Usage : scripts/verify_release_tag.sh <tag> [chemin du fichier VERSION]
set -euo pipefail

TAG="${1:-${GITHUB_REF_NAME:-}}"
VERSION_FILE="${2:-VERSION}"

if [ -z "$TAG" ]; then
  echo "::error::verify_release_tag: aucun tag fourni (argument ou GITHUB_REF_NAME)" >&2
  exit 2
fi

if [ ! -f "$VERSION_FILE" ]; then
  echo "::error::verify_release_tag: fichier VERSION introuvable : $VERSION_FILE" >&2
  exit 2
fi

FILE_VERSION="$(tr -d '\r\n' < "$VERSION_FILE")"

echo "tag            : $TAG"
echo "VERSION du tag : $FILE_VERSION"

if [ "$FILE_VERSION" != "$TAG" ]; then
  echo "::error::La version annoncée ($FILE_VERSION) diffère du tag ($TAG) : image refusée." >&2
  echo "Un tag publié ne se déplace jamais ; les tests ajoutés après coup vont dans la version suivante." >&2
  exit 1
fi

echo "OK : VERSION == tag"
