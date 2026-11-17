#!/usr/bin/env bash
# Intégration 2026.11.003-c8 — auto-mise à jour du compagnon.
#
# Deux scénarios, sur un démon Docker local et un registre jetable :
#   A. compagnon au code c8, image cible ABSENTE du démon → le pull est visible
#      (`docker pull`), le compagnon passe en c8, le battement continue ;
#   B. compagnon c6 (code d'avant le correctif), image c8 pré-tirée → l'ancien
#      code se remplace par la c8 (scénario de bascule prod c7 → c8).
#
# Aucune connexion à un rig, à .195 ou à une vraie machine : l'application est
# un serveur `/api/version` de test.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
REG="127.0.0.1:5057"
REG_NAME="fossor-c8-registry"
APP="$REG/fossor-app"
UPD="$REG/fossor-updater"
OLD="$UPD:2026.11.003-c7"      # code d'avant le correctif (tag publié)
NEW="$UPD:2026.11.003-c8"      # code corrigé
NEWP="$UPD:2026.11.003-c7p"    # code corrigé, tag de départ du scénario A
WORK="$(mktemp -d /tmp/fossor-c8.XXXXXX)"

cleanup() {
  for project in c8a c8b; do
    docker compose -p "$project" --project-directory "$WORK/$project" \
      -f "$WORK/$project/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1 || true
  done
  docker rm -f "$REG_NAME" >/dev/null 2>&1 || true
  docker rmi -f "$NEWP" "$NEW" "$OLD" "$APP:2026.11.003-c7" "$APP:2026.11.003-c8" >/dev/null 2>&1 || true
  docker network rm fossor-c8-net >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "=== registre local $REG ==="
docker network create fossor-c8-net >/dev/null 2>&1 || true
docker rm -f "$REG_NAME" >/dev/null 2>&1 || true
docker run -d --name "$REG_NAME" --network fossor-c8-net -p 127.0.0.1:5057:5000 registry:2 >/dev/null
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:5057/v2/" >/dev/null 2>&1 && break
  sleep 1
done

echo "=== image applicative de test (/api/version) ==="
mkdir -p "$WORK/app"
cat > "$WORK/app/server.py" <<'PY'
import http.server, json, os
VERSION = os.environ.get("APP_VERSION", "inconnu")
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"version": VERSION}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
http.server.HTTPServer(("", 8008), Handler).serve_forever()
PY
cat > "$WORK/app/Dockerfile" <<'DOCKER'
FROM python:3.12-alpine
ARG APP_VERSION
ENV APP_VERSION=${APP_VERSION}
COPY server.py /app/server.py
CMD ["python", "-u", "/app/server.py"]
DOCKER
for version in 2026.11.003-c7 2026.11.003-c8; do
  docker build -q --build-arg APP_VERSION="$version" -t "$APP:$version" "$WORK/app" >/dev/null
  docker push -q "$APP:$version" >/dev/null
done

echo "=== images du compagnon ==="
mkdir -p "$WORK/upd-old" "$WORK/upd-new"
git -C "$REPO" show 2026.11.003-c7:updater/updater.py > "$WORK/upd-old/updater.py"
cp "$REPO/updater/updater.py" "$WORK/upd-new/updater.py"
cp "$REPO/updater/Dockerfile" "$WORK/upd-old/Dockerfile"
cp "$REPO/updater/Dockerfile" "$WORK/upd-new/Dockerfile"
docker build -q -t "$OLD" "$WORK/upd-old" >/dev/null
docker build -q -t "$NEW" "$WORK/upd-new" >/dev/null
docker tag "$NEW" "$NEWP"
docker push -q "$OLD" >/dev/null
docker push -q "$NEW" >/dev/null
docker push -q "$NEWP" >/dev/null

compose_file() {
  cat > "$1/docker-compose.yml" <<'YAML'
services:
  fossor:
    image: ${FOSSOR_IMAGE}:${FOSSOR_IMAGE_TAG}
    ports:
      - "${FOSSOR_PORT}:8008"
    volumes:
      - ./data:/data
    restart: unless-stopped
  fossor-updater:
    image: ${FOSSOR_UPDATER_IMAGE}:${FOSSOR_UPDATER_TAG}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - .:/compose
      - ./data:/data:ro
      - ./data/update:/data/update
    environment:
      - FOSSOR_UPDATER_COMPOSE_DIR=/compose
      - FOSSOR_UPDATER_PROJECT=${COMPOSE_PROJECT_NAME}
      - FOSSOR_UPDATER_PROJECT_DIR=${FOSSOR_HOME}
      - FOSSOR_UPDATER_VERSION=${FOSSOR_UPDATER_TAG}
      - FOSSOR_UPDATER_EXPECTED_IMAGE=${FOSSOR_IMAGE}
      - FOSSOR_UPDATER_IMAGE=${FOSSOR_UPDATER_IMAGE}
      - FOSSOR_UPDATER_DIR=/data/update
      - FOSSOR_UPDATER_BACKUP_DIR=/data/backups
      - FOSSOR_UPDATER_HEALTH_URL=http://fossor:8008/api/version
      - FOSSOR_UPDATER_HEALTH_TIMEOUT=60
      - FOSSOR_UPDATER_POLL=2
      - FOSSOR_UPDATER_HEARTBEAT=2
    group_add:
      - "${DOCKER_GID}"
    restart: unless-stopped
YAML
}

start_project() {
  local dir="$1" project="$2" port="$3" updater_tag="$4"
  mkdir -p "$dir/data/update" "$dir/data/backups"
  compose_file "$dir"
  cat > "$dir/.env" <<ENV
FOSSOR_IMAGE=$APP
FOSSOR_IMAGE_TAG=2026.11.003-c7
FOSSOR_UPDATER_IMAGE=$UPD
FOSSOR_UPDATER_TAG=$updater_tag
FOSSOR_PORT=$port
COMPOSE_PROJECT_NAME=$project
FOSSOR_HOME=$dir
DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
ENV
  chmod 666 "$dir/.env"
  chmod 777 "$dir" "$dir/data" "$dir/data/update" "$dir/data/backups"
  docker compose -p "$project" --project-directory "$dir" -f "$dir/docker-compose.yml" \
    up -d >/dev/null
  for _ in $(seq 1 30); do
    curl -fsS "http://127.0.0.1:$port/api/version" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "application injoignable sur $port" >&2
  return 1
}

request_update() {
  local dir="$1" target="$2"
  mkdir -p "$dir/data/backups/pre-$target-test"
  printf 'sqlite-test\n' > "$dir/data/backups/pre-$target-test/fossor.db"
  chown -R 1000:1000 "$dir/data"
  python3 - "$dir" "$target" <<'PY'
import json, secrets, sys
path, target = sys.argv[1], sys.argv[2]
payload = {
    "target": target,
    "image": "127.0.0.1:5057/fossor-app",
    "requested_at": "2026-11-17T10:00:00+00:00",
    "nonce": secrets.token_hex(16),
}
open(f"{path}/data/update/request.json", "w", encoding="utf-8").write(
    json.dumps(payload, ensure_ascii=False)
)
PY
  chmod 666 "$dir/data/update/request.json"
}

wait_done() {
  local dir="$1" target="$2" project="$3"
  for _ in $(seq 1 60); do
    if grep -q '"event": "self_update"' "$dir/data/update/history.jsonl" 2>/dev/null; then
      # laisser le conteneur frère recréer le service avant de photographier
      for _ in $(seq 1 30); do
        docker ps --filter "label=com.docker.compose.project=$project" \
          --format '{{.Names}}\t{{.Image}}' | grep updater | grep -q "$target" && return 0
        sleep 1
      done
      echo "--- le compagnon n'a pas changé d'image, sortie du frère : ---"
      docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
        -v "$dir:/compose" --group-add "$(stat -c '%g' /var/run/docker.sock)" \
        --entrypoint docker "$UPD:$target" \
        compose -p "$project" --project-directory "$dir" --env-file "$dir/.env" \
        -f "$dir/docker-compose.yml" up -d --no-deps fossor-updater 2>&1 | tail -20
      return 0
    fi
    if grep -q '"event": "self_update_skipped"\|"event": "self_update_failed"' \
        "$dir/data/update/history.jsonl" 2>/dev/null; then
      return 1
    fi
    sleep 2
  done
  return 1
}

report() {
  local dir="$1" project="$2"
  echo "--- history.jsonl ---"
  cat "$dir/data/update/history.jsonl"
  echo "--- status.json ---"
  cat "$dir/data/update/status.json"
  echo "--- conteneurs (nom + image) ---"
  docker ps --filter "label=com.docker.compose.project=$project" \
    --format '{{.Names}}\t{{.Image}}'
  echo "--- journal du compagnon (pull + auto-maj) ---"
  local updater
  updater=$(docker ps -q --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.service=fossor-updater")
  docker logs "$updater" 2>&1 | grep -E "auto-maj|pull|bascule|fin" | tail -12 || true
}

echo
echo "############ SCÉNARIO A — image cible absente, le compagnon TIRE ############"
docker rmi -f "$NEW" >/dev/null 2>&1 || true
echo "image cible sur le démon avant démarrage : $(docker image inspect "$NEW" >/dev/null 2>&1 && echo présente || echo absente)"
A="$WORK/c8a"
start_project "$A" c8a 18081 "2026.11.003-c7p"
echo "compagnon au démarrage : $(docker ps --filter label=com.docker.compose.project=c8a --format '{{.Names}} {{.Image}}' | grep updater)"
request_update "$A" 2026.11.003-c8
if wait_done "$A" 2026.11.003-c8 c8a; then
  echo "auto-mise à jour : OK"
else
  echo "auto-mise à jour : ÉCHEC"
fi
report "$A" c8a
echo "--- battement (2 lectures à 4 s) ---"
python3 - "$A/data/update/status.json" <<'PY'
import json, subprocess, sys, time
path = sys.argv[1]
first = json.load(open(path, encoding="utf-8"))["heartbeat_at"]
time.sleep(4)
second = json.load(open(path, encoding="utf-8"))["heartbeat_at"]
print("heartbeat_at #1 =", first)
print("heartbeat_at #2 =", second)
print("battement continu :", first != second)
PY
docker compose -p c8a --project-directory "$A" -f "$A/docker-compose.yml" down -v >/dev/null 2>&1 || true

echo
echo "############ SCÉNARIO B — updater c6 (code c7), image c8 pré-tirée ############"
echo "pull préalable (comme Fred avant le clic) :"
docker pull "$NEW" 2>&1 | tail -1
B="$WORK/c8b"
start_project "$B" c8b 18082 2026.11.003-c7
echo "compagnon au démarrage : $(docker ps --filter label=com.docker.compose.project=c8b --format '{{.Names}} {{.Image}}' | grep updater)"
request_update "$B" 2026.11.003-c8
if wait_done "$B" 2026.11.003-c8 c8b; then
  echo "auto-mise à jour : OK"
else
  echo "auto-mise à jour : ÉCHEC"
fi
report "$B" c8b
echo "--- battement (2 lectures à 4 s) ---"
python3 - "$B/data/update/status.json" <<'PY'
import json, sys, time
path = sys.argv[1]
first = json.load(open(path, encoding="utf-8"))["heartbeat_at"]
time.sleep(4)
second = json.load(open(path, encoding="utf-8"))["heartbeat_at"]
print("heartbeat_at #1 =", first)
print("heartbeat_at #2 =", second)
print("battement continu :", first != second)
PY
