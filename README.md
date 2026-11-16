# Fossor

Mining fleet management — inventory, hardware detection, and pilotage of mining machines.

**Version:** [2026.11.002](https://github.com/LostInTheBugs/fossor/releases/tag/2026.11.002) ·
[All releases](https://github.com/LostInTheBugs/fossor/releases) ·
[Container image](https://github.com/LostInTheBugs/fossor/pkgs/container/fossor)

## Description

Fossor (ex-MineCtl) replaces the ad-hoc `mining.html` dashboard with an application
that keeps the fleet inventory, connects to each machine, and records every action.

## Fonctionnalités

- **Inventaire des machines** — nom, hôte, port et utilisateur SSH, authentification par
  clé ou mot de passe, indicateur sudo, type d'OS, usage minage. Les secrets (mot de
  passe, clé, clé d'API) sont chiffrés au repos avec `FOSSOR_MASTER_KEY` et ne sortent
  jamais de l'API : les réponses ne portent que `has_secret` / `has_api_key`.
- **SSH durci** — l'empreinte de la clé d'hôte est lue et épinglée **avant** tout envoi
  d'identifiant, `known_hosts` impose la clé épinglée pendant la poignée de main, et les
  commandes distantes sont restreintes à une liste blanche (pas de shell ad hoc).
- **Détection matériel** — une commande en lecture seule par machine renvoie l'inventaire
  CPU/GPU (modèle, VRAM, bus PCI, UUID NVIDIA, pilote, hôte KVM/PVE) ; un parseur pur
  alimente la table `devices`. La re-détection est idempotente : GPU rapprochés par bus
  PCI normalisé puis par UUID NVIDIA, device disparu conservé en `present = false`,
  choix `enabled_for_mining` préservés.
- **Choix par device** — les GPU intégrés sont signalés et proposés décochés par défaut ;
  l'usage de la machine (`cpu` / `gpu` / `both` / `none`) est recalculé à chaque
  changement.
- **Export/import de configuration** — JSON versionné avec des `uuid` stables ; les
  secrets sont chiffrés par une **phrase** au choix (PBKDF2-SHA256, 600 000 itérations,
  sel aléatoire, Fernet), jamais par la clé maître. L'import affiche un aperçu
  (`dry_run`), fusionne ou remplace, écrit une sauvegarde dans
  `<FOSSOR_DATA_DIR>/backups/` avant toute écriture, et n'exporte ni le jeton entrant ni
  la clé maître.
- **Vérification des nouvelles versions** — interrogation des releases GitHub (ETag,
  rate limit, intervalle réglable) et bandeau reprenant les notes de toutes les versions
  manquées.
- **Écran Analyse IA** — fournisseurs compatibles OpenAI principal et secours avec
  pré-réglages, sonde « Tester » réelle (latence, modèle, extraction), et jeton entrant
  Hermes stocké haché, affiché une seule fois, protégé par `require_hermes_token`.
- **Journal d'audit** — connexions, modifications de machines, détections et écritures de
  configuration, secrets masqués.
- **Conteneur non root** — l'image tourne en `fossor` (uid 1000) ; `/data` appartient à
  cet utilisateur.

## Installation

### Image Docker (recommandé)

```bash
docker run -d --name fossor \
  -p 8008:8008 \
  -v "$PWD/data:/data" \
  -e FOSSOR_MASTER_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  -e FOSSOR_ADMIN_PASSWORD='change-me' \
  ghcr.io/lostinthebugs/fossor:latest
```

Ou avec Compose (construit depuis les sources) :

```bash
cp .env.example .env      # renseigner les deux secrets
docker compose up -d
```

L'interface écoute sur `http://localhost:8008` : `/` est la page de connexion, et
`/machines`, `/settings`, `/audit` exigent une session. `FOSSOR_ADMIN_PASSWORD` n'est lu
qu'au **premier** démarrage, pour créer le compte admin ; il est ensuite haché en base et
les changements se font depuis l'application.

### Développement

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8008
```

## Variables d'environnement

| Variable | Description | Défaut |
|----------|-------------|--------|
| `FOSSOR_MASTER_KEY` | Clé Fernet chiffrant les secrets stockés. **Obligatoire.** La changer invalide tous les secrets en base. | *(requise)* |
| `FOSSOR_ADMIN_PASSWORD` | Mot de passe admin, lu au premier démarrage seulement puis haché en base. | *(requis au 1er démarrage)* |
| `FOSSOR_DATA_DIR` | Répertoire de la base SQLite et des sauvegardes de configuration. | `/data` |
| `FOSSOR_PORT` | Port de l'hôte publié vers le 8008 du conteneur (Compose uniquement). | `8008` |
| `FOSSOR_UPDATE_CHECK_INTERVAL` | Secondes entre deux vérifications des releases GitHub ; `0` désactive la vérification de fond. | `3600` |
| `DATABASE_URL` | Chaîne de connexion SQLAlchemy. | `sqlite:////data/fossor.db` |

## API

Toutes les routes `/api` exigent le cookie de session, sauf `/api/version`, `/api/login`
et le groupe `/api/hermes/*`, authentifié par le jeton entrant Hermes
(`Authorization: Bearer ...`) — prévu pour les routes de contexte, d'analyse et de
proposition de l'étape 4.

| Groupe | Objet |
|--------|-------|
| `/api/machines` | CRUD, test de connexion, détection matériel, liste des devices, réinitialisation de la clé d'hôte |
| `/api/devices/{uuid}` | Activer/désactiver un device pour le minage |
| `/api/config` | Export et import de la configuration (aperçu, fusion/remplacement) |
| `/api/config/ai` | Fournisseurs IA, sonde « Tester », jeton entrant |
| `/api/update` | État de la version et vérification manuelle des releases |
| `/api/audit-log` | Journal d'audit |
| `/api/hermes` | Points d'entrée machine (jeton Bearer) |

## Données et sauvegardes

`FOSSOR_DATA_DIR` contient `fossor.db` et `backups/config-<horodatage>.json`, écrit avant
tout import modifiant la configuration. Base et sauvegardes sont créées en uid 1000 dans
le conteneur : un montage `./data` reste lisible sans root.

## Notes de sécurité

- Les secrets sont chiffrés par `FOSSOR_MASTER_KEY` ; l'API n'expose que `has_secret` /
  `has_api_key`.
- Les clés d'hôte sont épinglées au premier contact : n'utiliser « Réinitialiser la clé
  d'hôte » qu'après avoir vérifié que l'empreinte a réellement changé (réinstallation,
  nouveau disque).
- Les adresses d'exemple utilisent la plage de documentation `192.0.2.0/24`.

## État du dépôt

```
sources/
├── app/                 # FastAPI : auth, secrets, SSH, détection, export/import, IA, update
│   └── routers/         # auth, machines, devices, config, ai, update, hermes, audit, ui
├── static/              # pages HTML/CSS/JS (aucun CDN)
├── tests/               # pytest (unit/API, intégration SSH, e2e Playwright)
├── Dockerfile, Dockerfile.e2e, docker-compose.yml
└── VERSION, CHANGELOG.md, pyproject.toml, requirements.txt
```

## License

MIT — See [LICENSE](LICENSE) for details.
