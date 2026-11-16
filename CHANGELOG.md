# Changelog

> MineCtl a été renommé **Fossor**. Les fonctionnalités et l'API restent identiques.

All notable changes to fossor are documented in this file.

## 2026.11.004 — en cours (étape 3a)

### Étape 3a — profils de minage, simulation, lancement/arrêt (mode host)

- **Mode simulation global (§5.7)** : activé par défaut ; la désactivation exige la case
  décochée **et** la saisie de « PILOTER », sinon refus. Bandeau permanent sur toutes les
  pages. En simulation, chaque action renvoie et journalise la liste exacte des commandes
  sans en exécuter une seule (aucun appel SSH de pilotage).
- **Profils de minage (§4.5)** : coin, wallet, pool (`solo` ou `stratum[+tcp|+ssl]://`),
  logiciel (`srbminer`, `lolminer`, `bzminer`, `xmrig`, `custom`), binaire (URL d'archive,
  **SHA256 obligatoire**, chemin dans l'archive), version, gabarit de ligne de commande à
  variables fermées (`{wallet} {pool} {worker} {gpus} {threads} {api_port}`), port d'API,
  mode d'exécution (`host` seulement). Chaque valeur substituée est validée par motif ;
  le gabarit est découpé avec `shlex.split` **avant** substitution et la commande reste
  un tableau d'arguments.
- **Token par défaut** : un profil global (Configuration) et une surcharge par machine.
- **Lancement/arrêt en mode host** : téléchargement **par la machine cible**, `sha256sum`
  vérifié avant extraction (refus + nettoyage si différent), service systemd
  `fossor-miner-<profil>.service` généré (ExecStart échappé, `Restart=on-failure`,
  utilisateur dédié), `daemon-reload`/`enable`/`start`, repli détecté sans systemd :
  `setsid nohup` + fichier PID dans `/opt/fossor/run/`. Arrêt par `systemctl stop` ou
  PID vérifié. Statut : actif, PID, uptime, hashrate lu sur l'API du mineur.
- **Cohabitation** : un autre programme qui occupe les GPU (nvidia-smi / `ps`) fait
  refuser le démarrage — Fossor ne touche jamais à un mineur qu'il n'a pas lancé.
- **API** : `POST /api/machines/{id}/miner/start` et `/stop`, `GET …/miner/status`,
  CRUD `/api/miner/profiles`, `GET|PUT /api/miner/settings`. Tout est audité (qui, quoi,
  simulé ou non, résultat).

## 2026.11.003 — 2026-11-16

### Étape 2f — navigation unique et notifications

- **Navigation unique** : `static/nav.js` est la seule source de la barre de
  navigation ; chaque page pose `<nav id="nav"></nav>` et l'onglet actif est déduit
  de l'URL. Test e2e qui parcourt les cinq pages connectées (même liste, même ordre,
  onglet actif, zéro erreur console).
- **Notifications (§4.14)** : canaux Discord (webhook), Telegram (jeton + chat id) et
  e-mail (SMTP : hôte, port, STARTTLS/TLS, utilisateur, mot de passe). Secrets chiffrés
  par la clé maître, jamais renvoyés (`has_*`), inclus chiffrés dans l'export avec
  secrets. Matrice événement × canal, niveau (info / alerte / critique), heures
  silencieuses Europe/Paris (les « critique » passent). File d'attente en base,
  3 tentatives, journal (date, événement, canal, résultat) sans secret ni adresse
  complète de wallet. Bouton « Envoyer un test » par canal.
- Événements branchés : machine hors ligne / revenue (anti-rebond de 2 collectes),
  hashrate < 50 % de la moyenne 1 h, solde de pool au-dessus du seuil de paiement,
  paiement reçu, nouveau prix d'un token sans prix, mise à jour disponible, mise à
  jour terminée / annulée (retour arrière).
- Tests : `httpx.MockTransport` pour Discord/Telegram, `aiosmtpd` en fixture pour le
  SMTP, matrice, heures silencieuses traversant minuit, anti-rebond, absence de secret
  dans le journal.
- Release : le workflow publie aussi `ghcr.io/lostinthebugs/fossor-updater:<tag>` et
  `:latest`.

### Étape 2e — mise à jour pilotée

- Client de mise à jour (vérification GitHub, sauvegarde préalable de la base,
  demande au compagnon) et conteneur `fossor-updater`, seul détenteur du socket
  Docker, avec retour arrière et restauration de la base.
- Trois modes d'authentification SSH (mot de passe, clé, clé Fossor) et préparation
  automatique d'une machine.

### Étape 2d — électricité et coût net

- Tarifs `fixed` / `ranges` / `tempo` (Europe/Paris), intégration des puissances,
  revenu 24 h depuis les soldes de pool et prix EUR, coût net par machine.

### Étape 2c — tokens minés, prix et soldes

### Étape 2b — tokens minés, prix, soldes de pool, import de l'historique

- Catalogue `coins` (§4.3) et `wallets` (§4.4) : tables, API CRUD, écran (adresse,
  emplacement, chemin de sauvegarde — jamais de seed ni de mot de passe).
- `POST /api/import/legacy` (+ `/api/import/legacy/upload`) : import idempotent de
  `mining-status.json` / `mining-live.json` → coins, wallets, campagnes en cours
  (`current`, `current_cpu`, `*_spec`, plus Pearl/Luckypool du fichier live) et
  historique (`history` : montant miné parsé, verdict, liens). `dry_run` par défaut.
- Prix CoinGecko `simple/price` (usd, eur, variation 24 h), cache 5 min, uniquement
  pour les `coingecko_id` vérifiés (PRL `pearl-2`, QTC `quantus` ; WAM et QWC non
  cotés, prix manuel optionnel par coin).
- Connecteur Luckypool (`app/pools.py`) : `GET /api/stats` public → seuil de paiement
  et agrégats du pool ; aucune statistique par wallet n'est exposée par l'API, les
  soldes restent `null` avec la raison écrite — jamais inventés.
- Écran « Tokens minés » (§6 écran 3) : valeur totale €/$, cartes des campagnes en
  cours, historique dépliable, catalogue et wallets éditables, import.
- `GET /api/mined`, `POST /api/mined/refresh`.
- Collecte : une machine sans device est détectée automatiquement à sa première
  collecte (audit `scheduler`), les métriques GPU se rattachent par bus PCI.

### Étape 2a — collecteurs et tableau de bord en direct

### Ajouté
- **Commande `collect_stats`** (liste blanche, lecture seule, jamais sudo) : sections
  `HIVE_STAT`, `HIVE_GPU`, `NVSMI`, `PS_MINERS`, `UPTIME`, chaque outil optionnel
  (section vide au lieu d'un échec)
- **Parseur pur** `app/stats_parser.py` : devices rapprochés par bus PCI normalisé
  (l'indice 0 CPU/iGPU de HiveOS est ignoré pour le hashrate), hashrate normalisé en H/s
  depuis `hs_units`, temp/ventilo/conso/charge par device, mineur, algo/coin, uptime,
  shares acceptées/rejetées, port d'API découvert dans `ps`. `params.passwd` et
  `params.rig_id` ne sont jamais lus, donc jamais stockés ni journalisés
- **API SRBMiner-Multi** : si `--api-port N` est découvert, `GET http://<hôte>:N/`
  depuis Fossor (timeout 5 s) complète les hashrates absents ; port fermé = échec
  silencieux
- **Collecteur** : toutes les `FOSSOR_COLLECT_INTERVAL` secondes (60 s par défaut), 5
  machines en parallèle (asyncio), timeout par machine, échec → machine « hors ligne »
  avec l'erreur ; les machines `usage=none` ne sont jamais contactées
- **Stockage** : table `metrics` (une ligne par device et par tour) et table
  `machine_status` (dernier état), rétention `FOSSOR_METRICS_RETENTION_DAYS` (7 jours),
  purge quotidienne
- **API** : `GET /api/metrics/live`, `GET /api/metrics/history?machine=<uuid>&hours=24`
- **Tableau de bord** : page d'accueil après connexion, une carte par machine (en ligne /
  hors ligne, mineur, hashrate total avec unité adaptée, conso, uptime, âge de la
  dernière collecte) et une ligne par device, rafraîchi toutes les 30 s

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
