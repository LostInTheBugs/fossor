# Changelog

## 2026.11.004-c5 — 2026-11-18
- Solo KORSH : un `### JLOG` vide (« -- No entries -- ») n'est plus pris pour un
  log, et un vrai fichier passe avant le journal — le rig retrouve ≈ 6,25 kH/s
  (source « log du script ») ; la fixture `PROBE_RIG` porte les JLOG réels.
- `POST /api/update/apply` : la version demandée est validée (format puis release
  publiée, avec un `check` si le cache l'ignore) et utilisée telle quelle ; sans
  version, la dernière connue.
- Alimentations : `psu2_id` sans `psu_id` → 422, `psu2_id == psu_id` → 422,
  `split_w` = part slot/riser (75 W par défaut, NULL sans seconde PSU) ; le
  second menu n'est actif qu'avec une PSU 1 différente ; migration des
  affectations existantes (`psu_id` NULL + `psu2_id` → `psu_id`).
- Puissance CPU : table de TDP des modèles courants (Athlon 3000G 35 W, Ryzen 5
  3600/5600G 65 W, i5-4570 84 W, i5-12400 65 W), estimation déclarée
  (`power_estimated`) avec sa source, ordre mesure > saisie > TDP > 65 W
  générique ; `GET /api/machines/<id>/power` expose le bloc `cpu`.

## [2026.11.004-c4] — 2026-11-18

### Corrigé
- Le filtre `PS_MINERS` garde les scripts solo (`*-miner.sh`) même à 0 % de CPU : `korsh-miner.sh`, endormi dans `screen` entre deux scans, n'était plus vu, donc plus aucune sonde de log n'était planifiée et le solo KORSH du rig perdait son hashrate (6,25 kH/s en 2026.11.004).
- La sonde lit le journal que le script déclare lui-même (`/proc/<pid>/fd/255`), chemin relatif résolu sur son `cwd` : `/root/korsh/mining.log` est retrouvé sans deviner de nom, et le hashrate revient par les scans (200 000 nonces / intervalle).
- `raw_collections` conserve 40 000 caractères au lieu de 20 000 : la sonde CPU, placée en tête, tronquait `PS_MINERS`, `NODE` et `CHAIN` du rig.
- `pyproject.toml` aligné sur la version de base (2026.11.4).

## 2026.11.004-c3 — 2026-11-18

### Correctif — le vrai défaut de la sonde `/proc/<pid>/fd`

- Les fragments de la sonde étaient écrits `\"$c\"` dans la chaîne Python : le shell
  recevait un mot **entouré de guillemets littéraux**, donc `case "$c" in /home/*)`
  ne matchait **jamais**. Toutes les sondes de mineurs CPU retombaient sur
  `journalctl` — y compris le solo BIG, dont le log est le seul porteur du hashrate.
- Les guillemets sont maintenant émis nus (`"$c"`), et le test d'existence passe par
  `RD` (`[ -e ]` puis `sudo -n test -e`) : le home en 750 rend le fichier invisible au
  compte `fossor` alors que `sudo -n` le lit.
- Preuve sur la machine réelle : la sonde exécutée en `fossor` rend
  `### LOGI 1104502 /home/administrator/bigcoin-mine/bigcoin/mine.log` et ses lignes
  `~6996 total over 11 workers`.

## 2026.11.004-c2 — 2026-11-18

### Correctif — le log du solo BIG, seconde voie

- En production le compte `fossor` ne peut pas lire `/proc/<pid>/fd/1` d'un script
  lancé par `administrator` (réservé au propriétaire et à root) : la c1 retombait donc
  sur le journal du démon et le hashrate restait inconnu.
- La sonde lit maintenant aussi la **redirection du lanceur** lue dans `ps`
  (`> ~/bigcoin-mine/bigcoin/mine.log`) et développe `~/…` avec le home du compte du
  processus (`getent passwd`), puis `stat`/`tail` (avec `sudo -n` en repli).
- Le chemin est suivi jusqu'à `node.log` ; s'il reste vide, la note dit « illisible ou
  vide » au lieu d'un « inconnu » muet. Les pids de script suivis passent à trois.

## 2026.11.004-c1 — 2026-11-18

### Correctif — hashrate du solo BigCoin en production

- **La sonde du solo suit la redirection réelle du script** : `mine-bigcoin.sh` est
  lancé par `bash -c … > ~/bigcoin-mine/bigcoin/mine.log 2>&1 &`. Les noms devinés
  (`mining.log`, `miner.log`) sont supprimés : la sonde lit `/proc/<pid>/fd/1|2` du
  **script**, puis de son **lanceur**, le **démon** en dernier. Le solo BIG du Ryzen
  retrouve son hashrate (6952 H/s, 11 workers).
- **Test de bout en bout** : sortie `ps` réelle → plan de sonde → sortie machine →
  `6952 H/s`, plus un cas de régression où le journal du démon ne donne rien.
- **Test d'auto-mise à jour** : la cible est construite depuis la version installée
  (`NNN + 1`) ; il passait sur `main` mais échouait sur le clone du tag.

## 2026.11.004 — 2026-11-17

### Puissance CPU : sources nommées et coût à la prise (§4.2 / §4.15)

- **Ordre des sources de puissance CPU**, chacune nommée dans `power_source` :
  `package_power` de l'API du mineur (> 0), puis **RAPL**
  (`/sys/class/powercap/intel-rapl:*/energy_uj`, deux lectures sur l'intervalle de
  collecte, `sudo -n` si nécessaire), puis hwmon `power1_input`
  (`amd_energy` / `zenpower`), puis la valeur saisie sur la machine
  (`machines.cpu_power_w`), puis « non mesurée ». Une API à 0 ne masque plus RAPL.
- **RAPL avec plafond** : `max_energy_range_uj` est lu avec `energy_uj` ; un compteur
  reparti à zéro donne `max - précédent + courant` au lieu d'une puissance écartée.
  `psys` (`intel-rapl:1`) et le miroir `intel-rapl-mmio:*` ne sont jamais additionnés
  au paquet.
- **Coût €/jour et €/mois** par machine et au total, calculé sur la **puissance à la
  prise** (charge DC / rendement des alimentations) ; `GET /api/energy` expose
  `cost_eur_month` et une section `coins` : revenu estimé, électricité du coin répartie
  **au prorata de la puissance** de chaque mineur, coût net par coin. Tarifs fixes,
  plages heures pleines/creuses et Tempo de §4.15 appliqués minute par minute.
- Tableau de bord : coût / mois par carte et total (jour · mois).

### Type d'OS choisi à l'ajout (§4.1 ter)

- Champ **Type d'OS** juste après l'IP (HiveOS, Ubuntu/Debian, Proxmox, Autre) :
  il pré-remplit port, mode d'authentification, login, sudo, usage et hôte de VM.
  HiveOS : mode « Préparation automatique », login temporaire `user`, **mot de passe
  jamais pré-rempli** avec le texte d'aide « mot de passe par défaut de HiveOS : 1
  (à changer) », collecteur HiveOS et mineur du flight sheet signalé externe.
  Proxmox : compte `root`, `vm_host` coché, pas de minage direct.
- `GET /api/machines/os-presets` expose les préréglages (aucun secret) ;
  `POST /api/machines/{id}/os-preset` applique le préréglage détecté.
- `os_kind` = type **choisi**, `os_kind_detected` = OS **réel** écrit par la détection ;
  en cas d'écart, bannière « OS détecté : X (choisi : Y) » et bouton « Appliquer le
  préréglage X ». Les machines existantes reçoivent le type détecté (migration,
  backfill `os_kind_detected = os_kind`).

### Contenu de l'étape depuis 2026.11.003

- **3a — pilotage des mineurs** : profils validés (`shlex` puis substitution,
  `re.fullmatch`), démarrage/arrêt par `Step`, refus **409 `psu_overload`** au-delà de
  80 % de charge prévue sauf `confirm_overload`, mode simulation par défaut.
- **004-a — parc et consommation** : GPU intégrés masqués du minage par défaut
  (réglage global), alimentations multiples avec courbes 80 PLUS 230 V, déclassement,
  affectation des devices (dont GPU réparti sur deux PSU), inventaire des composants
  (RAM, disques classés par TRAN avant ROTA, cartes PCIe, ventilateurs) et consommation
  totale par machine distinguant mesuré et estimé, à la prise.
- **004-a2 — solo BIG** : hashrate lu au format réel du log
  (`N hashes/s/worker (~T total over W workers)`), total machine déjà sommé.
- **Correctifs c1 à c9** (déjà publiés, détail ci-dessous) : préparation SSH et
  provisionnement, empreinte d'hôte, cookies forgés, import/export, collecte HiveOS et
  SRBMiner, sonde CPU (log redirigé, fréquence), solo stable, garde-fou de tag
  (`VERSION` == tag) et compagnon de mise à jour qui tire son image.

## 2026.11.003-c9 — 2026-11-17

### Correctif : image c8 retirée, garde-fou de tag

- **2026.11.003-c8 est retirée** : l'image publiée sous ce tag annonçait
  `2026.11.004-dev` (le tag avait été déplacé sur un commit de `main`), l'updater
  l'a refusée au contrôle de santé et la prod est revenue à c7 en 4 s. Utiliser
  `2026.11.003-c9`.
- **Garde-fou de release** : `scripts/verify_release_tag.sh` fait échouer les
  jobs `release` et `docker` si le contenu de `VERSION` dans l'arbre du tag
  diffère du nom du tag — un tag déplacé ne peut plus publier d'image.
- **Contrôle avant publication** : le job `docker` construit l'image **sans** la
  pousser, la lance et vérifie que `/api/version` renvoie exactement le nom du
  tag ; `:<tag>` puis `:latest` ne partent qu'après ce contrôle.
- **Règle** : un tag publié ne se déplace jamais ; les tests écrits après coup
  vont dans la version suivante.
- Tests : fixtures c8 et harnais d'intégration du compagnon
  (`tests/docker/selfupdate_c8.sh`, `tests/test_collect_c8.py`) désormais
  versionnés sous un tag cohérent.

### Contenu repris de 2026.11.003-c8

- Compagnon : `_self_update` **tire** désormais son image cible (`docker pull`,
  délai 300 s) au lieu de vérifier sa seule présence locale ; en cas d'échec du
  pull seulement, il reste en place avec la raison. Trace `self_pull` dans
  `history.jsonl`.
- Journaux des mineurs : `/proc/<pid>/fd/1|2` lu avec `sudo -n readlink`, donc
  le log d'un lanceur (`/var/log/athlon-korsh-miner.log`) et plus seulement
  celui écrit dans la ligne de commande ; socket/pty → `journalctl _PID`.
- Nœuds solo : client relatif (`./bigcoin-cli`) résolu par
  `/proc/<pid>/cwd` + `-datadir` relatif ; `getmininginfo` appelé avec `sudo -n`.
- Solo stable : démon `*d -daemon` qui consomme du CPU, ou script
  `*-miner.sh`/`mine-*.sh` ; le client `generatetoaddress` ne compte que les
  workers (le solo KORSH du rig ne disparaît plus entre deux collectes).
- Mineur à 0 % CPU : « mineur inactif (0 % CPU, log sans hashrate récent) » avec
  la date de dernière modification du log, au lieu de « inconnu ».
- CPU : sonde exécutée à chaque collecte (fréquence `cpu MHz` normalisée même
  sans mineur), puissance SRBMiner `cpu_devices[0].package_power`, RAPL via
  `sudo -n`, puis `amd_energy`/`zenpower` ; la source de la valeur est exposée.
- Diagnostic : `raw_collections` porte la sortie de la sonde CPU, toujours masquée.
- Tests : fixtures fd fichier/socket, `TTF @ 237.8 Mh/s`, cwd + `getmininginfo`,
  korshd sans client, `cpu MHz`, RAPL root, `package_power` ; bout en bout
  athlon/Ryzen/rig ; intégration Docker du compagnon (pull absent → c8, puis
  c7 → c8 image pré-tirée).

## 2026.11.003-c8 — 2026-11-17

> **Retirée** : image publiée avec une mauvaise version, utiliser `2026.11.003-c9`.

### Correctif : auto-mise à jour, hashrate CPU, fréquence et puissance
- Compagnon : `_self_update` **tire** désormais son image cible (`docker pull`,
  délai 300 s) au lieu de vérifier sa seule présence locale ; en cas d'échec du
  pull seulement, il reste en place avec la raison. Trace `self_pull` dans
  `history.jsonl`.
- Journaux des mineurs : `/proc/<pid>/fd/1|2` lu avec `sudo -n readlink`, donc
  le log d'un lanceur (`/var/log/athlon-korsh-miner.log`) et plus seulement
  celui écrit dans la ligne de commande ; socket/pty → `journalctl _PID`.
- Nœuds solo : client relatif (`./bigcoin-cli`) résolu par
  `/proc/<pid>/cwd` + `-datadir` relatif ; `getmininginfo` appelé avec `sudo -n`.
- Solo stable : démon `*d -daemon` qui consomme du CPU, ou script
  `*-miner.sh`/`mine-*.sh` ; le client `generatetoaddress` ne compte que les
  workers (le solo KORSH du rig ne disparaît plus entre deux collectes).
- Mineur à 0 % CPU : « mineur inactif (0 % CPU, log sans hashrate récent) » avec
  la date de dernière modification du log, au lieu de « inconnu ».
- CPU : sonde exécutée à chaque collecte (fréquence `cpu MHz` normalisée même
  sans mineur), puissance SRBMiner `cpu_devices[0].package_power`, RAPL via
  `sudo -n`, puis `amd_energy`/`zenpower` ; la source de la valeur est exposée.
- Diagnostic : `raw_collections` porte la sortie de la sonde CPU, toujours masquée.
- Tests : fixtures fd fichier/socket, `TTF @ 237.8 Mh/s`, cwd + `getmininginfo`,
  korshd sans client, `cpu MHz`, RAPL root, `package_power` ; bout en bout
  athlon/Ryzen/rig ; intégration Docker du compagnon (pull absent → c8, puis
  c7 → c8 image pré-tirée).

## 2026.11.003-c7 — 2026-11-17

### Minage CPU : hashrate et état du CPU
- Sonde `collect_cpu` en lecture seule (compteurs `/proc`, hwmon, RAPL, journaux,
  API locales, getters de nœud) : aucune commande ne modifie un mineur.
- Hashrate CPU par ordre de préférence : API du mineur (port lu dans la commande),
  journal (chemin lu dans la commande **ou** résolu par `/proc/<pid>/fd/1|2`, donc
  celui d'un service systemd) avec contrôle de fraîcheur 300 s, puis nœud solo
  (`getmininginfo`/`getblockchaininfo`/`getnetworkhashps` + estimation
  « nonces / intervalle des scans »).
- État du CPU par machine : charge (deux `/proc/stat`), température (k10temp Tctl,
  coretemp Package, `cputemp` HiveOS), fréquence moyenne, puissance
  (`package_power` SRBMiner, sinon RAPL, sinon « non mesurée »).
- Tableau de bord : cartes par coin avec un vrai hashrate (KSH, WAM…), ligne CPU
  sur la carte machine et dans la fiche, source de chaque hashrate affichée.
- Un mineur sans hashrate affiche désormais la raison exacte, jamais un « — » muet.


> MineCtl a été renommé **Fossor**. Les fonctionnalités et l'API restent identiques.

All notable changes to fossor are documented in this file.

## [2026.11.003-c6] — 2026-11-17

### Fixed
- **Coin vide sur une base réelle** : la table `coins` de la production était
  vide, et la correspondance pool → coin ne vivait que dans le catalogue : la
  recherche ne trouvait donc jamais rien et SRBMiner, wam-miner, cpuminer
  affichaient un coin vide. Une table **intégrée au code** (`app/coins.py`)
  fait désormais foi : `pearl*.luckypool.io` → PRL, `pool.korsh.org` → KSH,
  `pool.wamcoin.org` → WAM, et `pearlhash` → PRL en secours. Le catalogue la
  complète ou la surcharge. Au démarrage, un catalogue vide reçoit les coins
  de base (PRL, KSH, WAM, BIG avec leur algorithme).
- **Coin de la machine** : une machine qui lance plusieurs mineurs n'a pas
  « un » coin. Le champ `coin` ne vient plus que de HiveOS ; l'API expose
  `coins`, la liste des coins de la machine, affichée dans l'interface.
- **Hashrate SRBMiner illisible** : le parseur ne connaissait que le format
  plat. La charge réelle de SRBMiner-MULTI 3.8.3 (`algorithms[]`) est
  maintenant lue : total = `hashrate.gpu.total` + `hashrate.cpu.total`,
  hashrate par GPU rattaché au device par `gpu_devices[].topology_id` /
  `bus_id` (PCI), algo, pool, shares acceptées/rejetées et `asic_power` par
  carte. Un mineur à plusieurs algorithmes donne une ligne par algorithme.
  Le format plat déjà géré (HiveOS, anciens SRBMiner) reste lu. Le wallet de
  la réponse n'est jamais stocké ni affiché.

## [2026.11.003-c5] — 2026-11-17

### Fixed
- **Compagnon figé après une mise à jour réussie** : le nonce traité n'était pas
  mémorisé dans la variable de la boucle. La demande restait sur le disque,
  `handle()` répondait « ignoré » sans réécrire `status.json`, et le battement de
  cœur s'arrêtait à la seconde du `done` — l'interface en concluait « service
  absent » alors que le conteneur tournait. Le nonce est désormais écrit dans
  `status.json` (`handled_nonce`) **avant** tout travail, relu au démarrage, et la
  boucle bat le cœur avant de rendre la main.
- **Battement indépendant** : un fil dédié réécrit `heartbeat_at` toutes les
  `FOSSOR_UPDATER_HEARTBEAT` secondes, y compris pendant un `docker pull` long.
- **Appels Docker bornés** : `docker pull` ≤ 300 s, `docker compose up` ≤ 120 s,
  `inspect`/`ps` ≤ 30 s. Un démon muet rend la main (état `failed`) au lieu de figer
  la boucle pour toujours.
- **Journal** : chaque étape (démarrage, demande, pull, bascule, santé, fin, erreur)
  est écrite sur la sortie standard — `docker logs` était vide parce que le
  compagnon n'imprimait rien.

### Added
- **Auto-mise à jour du compagnon** : après une bascule réussie, si l'image
  `fossor-updater:<cible>` existe, `FOSSOR_UPDATER_TAG` est réécrit dans le `.env`
  et un conteneur d'amorçage détaché recrée le service `fossor-updater`. Si
  l'amorçage échoue, le `.env` est remis dans son état d'avant et l'ancien
  compagnon reste en service.
- **Reprise après arrêt** : un état actif trouvé au démarrage est signalé
  (`step=recover`) et la demande n'est jamais rejouée.
- Configuration → Mise à jour affiche la version de l'application **et** celle du
  compagnon, et distingue « Updater sans réponse depuis X min » (conteneur présent,
  battement figé, marche à suivre) du service réellement absent.

## [2026.11.003-c4] — 2026-11-17

### Fixed
- **Mineurs réels mal regroupés** : les lanceurs (`SCREEN`, `script -qc`, `sh -c`,
  `bash -c`, `unbuffer`, `nohup`, `timeout`, `sudo`, `setsid`…) ne sont plus comptés
  comme des mineurs ; chaque ligne est ramenée au processus qu'elle enveloppe et les
  processus identiques (même binaire, mêmes arguments) forment **une** entrée avec un
  compteur. Le Ryzen n'affiche plus 25 lignes pour un seul minage BigCoin à 11 workers,
  le rig n'a plus chaque mineur en double. La commande de collecte de Fossor
  (utilisateur `fossor`, `### HIVE_STAT`) est ignorée.
- **Mineurs non reconnus** : un processus qui parle à un pool (`stratum://`, `--url`,
  `--pool`) ou qui mine en CPU (`--cpu-threads`, `--cpu`) est retenu et nommé d'après
  son binaire (`supryolo`) ; les SRBMiner renommés (`srbminer_custom_bin`,
  `srbminer_compute`) sont reconnus sur le programme enveloppé, jamais sur la ligne
  entière — le lanceur HiveOS `miner-run srbminer` n'est plus un mineur.
- **Minage solo par nœud** : `korsh-cli`/`bigcoin-cli … generatetoaddress` pilotés par
  un démon (`korshd`, `bitcoind -datadir=…`) donnent **une** entrée
  « Minage solo (nœud) » avec le coin déduit du client/datadir, le nombre de workers et
  le %CPU du démon.
- **Coin vide** : l'hôte du pool est normalisé des deux côtés (le catalogue peut
  stocker `stratum+tcp://pearl-eu2.luckypool.io:3360`), correspondance exacte puis par
  sous-chaîne, `PEARL` replié sur `PRL` (alias), et un pool inconnu est résolu par le
  préfixe du wallet (`oms1…` → OMS) quand ce ticker existe au catalogue.
- **Hashrate absent** : une réponse HTTP 200 sans hashrate (page web, JSON étranger)
  ne court-circuite plus le repli SSH — c'était la cause du « — » silencieux. Si un
  mineur expose un port et que le hashrate reste vide après direct + SSH, `api_error`
  est renseigné et affiché.
- **%CPU et utilisateur** : `ps -eo user:32,pid,pcpu,etimes,args` (utilisateur complet,
  plus d'`adminis+`), %CPU du processus retenu rempli et `count` exposé.
- **Tableau de bord** : une carte par **coin** (PRL, KSH, WAM, BIG, OMS/inconnu) avec
  machines, mineurs et workers ; un rig multi-coins ne compte plus comme un seul coin.
- **Collecte allégée** : filtre `ps` appliqué sur la machine (threads noyau et
  processus à 0 % sans indice de minage écartés), une seule ligne `raw_collections`
  par machine.
- **Version du compagnon** : `Configuration → Mise à jour` affiche la version de
  `fossor-updater` (une mise à jour ne bascule que l'image de l'app) et signale l'écart
  avec l'application.

### Tests
- Fixtures `ps` des trois machines (rig HiveOS, Ryzen 5 3600, athlon-3000g) avec wallets
  et mots de passe fictifs : parseur figé, collecte → base → `/api/metrics/live`, e2e
  des cartes par coin, intégration réelle (SSH + faux SRBMiner renommé sur 127.0.0.1)
  pour le repli et pour le cas d'échec (`api_error`).

## [2026.11.003-c3] — 2026-11-17

### Fixed
- **Bouton « Mettre à jour » inopérant** : le bouton de la mise à jour et celui de
  l'import de configuration partageaient `id="applyBtn"` ; le gestionnaire de l'import
  écrasait celui de la mise à jour et répondait « Aperçu requis avant d'appliquer ».
  Ids uniques `updateApplyBtn` / `importApplyBtn`, sélecteurs JS et testeur « en cours »
  alignés.
- **Garde-fou** : test d'unicité des `id` sur chaque page de `static/` (`nav.js` inclus)
  et d'absence de deux gestionnaires de clic sur un même élément ; e2e du clic réel sur
  « Mettre à jour vers X » (requête `POST /api/update/apply` + contenu de
  `request.json`) et de l'import avec et sans aperçu.
- **Vérification GitHub surchargeable** par `FOSSOR_RELEASES_URL` pour les tests e2e
  hors ligne.

## [2026.11.003-c2] — 2026-11-17

### Fixed
- **Mineurs externes jamais enregistrés** : la collecte persiste désormais la liste
  détectée (`parse_collect_stats` renvoie `miners`), marque le CPU et les GPU
  occupés à chaque tour, et un test part de la sortie brute jusqu'à
  `/api/metrics/live`.
- **Hashrate nul** : repli SSH (`curl`/`wget` sur `127.0.0.1`) quand l'API SRBMiner
  n'écoute pas sur l'interface réseau ; les deux échecs affichent
  « API du mineur injoignable ».
- **Minage CPU** : motifs par défaut (cpuminer, cpuminer-opt, cpuminer-multi,
  wam-miner, xmrig, SRBMiner `--cpu-threads`/`--disable-gpu`, scripts maison),
  liste configurable dans Configuration, lecture `ps -eo user,pid,pcpu,etimes,args`,
  algo/pool/threads/%CPU et coin déduit du pool (`coins.pool_hosts`).
- **Diagnostic** : bouton « Voir la dernière collecte brute » (admin), sortie masquée.
- **Tableau de bord** : une carte par token/algo, plus aucune somme de hashrates
  d'algorithmes différents.
- **Audit** : une seule ligne de préparation, acteur = utilisateur connecté.

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
## 2026.11.003-c1 — 2026-11-17

### Correctif hotfix — détection mineurs externes et état de préparation

- **Mineurs externes (§4.24)** : `nvidia-smi --query-compute-apps=pid,process_name,gpu_id,used_memory` enrichi pour extraire le `gpu_id`. Le parser `parse_gpu_compute` détecte les mineurs (SRBMiner, lolMiner, xmrig…), lit la ligne de commande pour `--algorithm`, `--pool`, tronque les wallets et masque les mots de passe. Les GPU concernés sont marqués `occupied_external=True` sur le dashboard et la fiche machine.
- **État de préparation** : nouvelle valeur `auth_type=provision_pending` — tant que la préparation n'a pas réussi, la machine reste "à préparer" ; test/detect/collecte répondent sans tenter de connexion. Bouton "Réessayer la préparation" en frontend.
- **Messages d'erreur** : tous les messages d'échec SSH sont en français explicite ("Le login ou le mot de passe temporaires ont été refusés par la machine").
- **Acteur d'audit** : `host-key-pinned` et tous les audits utilisent maintenant l'utilisateur connecté (ou `system` pour la collecte automatique au lieu de `hermes` codé en dur).
- **Formulaire d'ajout** : labels corrigés par mode (Login/Mot de passe, Compte sur la machine + clé, Login temporaire/Mot de passe temporaire/Compte à créer).
- **Bouton ✏️** : `editMachine(id)` implémenté (alias vers `openModal`) ; la fermeture du modal fonctionne (`closeModal` retirait la classe `undefined`).
- **Validation** : nom, hôte (IPv4 / IPv6 / nom DNS) et port SSH (1-65535) validés côté API (422, message français) et côté formulaire — plus de machine créée avec un hôte vide.
- **Migration** : `devices.occupied_external` ajouté via `_migrate_device_occupied_external_column`.

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
