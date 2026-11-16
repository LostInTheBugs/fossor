# Changelog

> MineCtl a été renommé **Fossor**. Les fonctionnalités et l'API restent identiques.

All notable changes to fossor are documented in this file.

## 2026.11.002 — 2026-11-16

Étape 1 terminée : de l'inventaire à la configuration complète de l'application.

### Ajouté
- **Connexion SSH** : authentification par clé ou par mot de passe, empreinte de la clé
  d'hôte lue et épinglée **avant** tout envoi d'identifiant, `known_hosts` imposant la clé
  épinglée pendant la poignée de main, commandes distantes restreintes à une liste blanche
- **Détection CPU/GPU** : commande `detect_hw` en lecture seule et parseur pur (modèle,
  VRAM, bus PCI, UUID NVIDIA, pilote, hôte KVM/PVE), table `devices` remplie par
  `POST /api/machines/{id}/detect` ; re-détection idempotente (rapprochement par bus PCI
  normalisé puis par UUID NVIDIA, device disparu conservé en `present: false`, `uuid` et
  `enabled_for_mining` préservés)
- **Choix par device** : case « miner avec » par device, GPU intégrés signalés et
  désactivés par défaut, usage machine (`cpu`/`gpu`/`both`/`none`) recalculé à chaque
  changement
- **Export/import de configuration** : JSON versionné, `uuid` stables, secrets chiffrés
  par une phrase (PBKDF2-SHA256 600 000 itérations, sel aléatoire, Fernet), aperçu
  `dry_run`, modes `merge`/`replace`, sauvegarde dans `<FOSSOR_DATA_DIR>/backups/` avant
  écriture ; ni le jeton entrant ni la clé maître ne sont exportés
- **Vérification des nouvelles versions GitHub** : ETag, rate limit, intervalle
  `FOSSOR_UPDATE_CHECK_INTERVAL`, notes de toutes les versions manquées, bandeau dans
  l'interface
- **Écran Analyse IA** : fournisseur principal et secours (compatibles OpenAI),
  pré-réglages, sonde « Tester » réelle (latence, modèle, extraction), jeton entrant
  Hermes stocké haché, affiché une seule fois, protégé par `require_hermes_token`
- Écran Configuration et écran d'audit

### Corrigé
- Page de connexion servie sur `/`, redirections 303, helper `$` local (plus de CDN)
- `/api/audit-log` renvoyait 500 : `actor` était stocké en enum au lieu d'une chaîne
- Le test de `/api/version` ne fige plus la version de développement : il compare au
  fichier `VERSION`

### Modifié
- Conteneur non root : `fossor` uid 1000, `/data` lui appartient
- Workflow de release : publication de `ghcr.io/lostinthebugs/fossor:<tag>` et `:latest`,
  notes de release extraites de ce fichier pour le tag

### Tests
- pytest unitaires/API, intégration SSH contre un serveur jetable, e2e Playwright
  (connexion, détection matériel, onglet Analyse IA)

## [2026.11.001-c1] — 2026-11-15

### Security fixes
- Sessions expire correctly: `URLSafeTimedSerializer` with `max_age` enforcement in `get_current_user()`; removed hand-rolled `exp` claim
- Forged cookies rejected (wrong signing key → 401)
- Test: expired token → 401, forged cookie → 401

### Bug fixes
- Admin account persisted in database on startup (via `FOSSOR_ADMIN_PASSWORD`)
- Test isolation: all tests use `tmp_path` for DB and signing key — pass on fresh `git clone`
- Test teardown drops tables between test classes

### Tests
- 25 passing tests: CRUD, auth-required, no-secret-leak, forged-cookie, expired-token

## [2026.11.001] — 2026-11-15

### Added
- FastAPI application with SQLite backend
- Docker deployment (port 8008, ./data:/data volume)
- Admin login with bcrypt password hashing, HttpOnly + SameSite=Strict cookie
- Fernet-encrypted secrets storage (master key from FOSSOR_MASTER_KEY)
- Machine CRUD API (/api/machines) and Machines management screen
- Machine fields: name, host, ssh_port (default 22), ssh_user, auth_type, secret_ref, sudo, os_kind, host_key, usage, vm_host, enabled
- Audit log table and screen (masked secrets)
- GET /api/version endpoint
- Secrets never exposed in API responses (only `has_secret: true`)
- pytest suite: machine CRUD, no-secret leak test, login required, default port 22
