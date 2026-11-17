"""Detection of external miners (§2.6, correctif 2026.11.003-c2).

A machine may already mine with a miner Fossor does not manage (SRBMiner,
cpuminer-opt, wam-miner, xmrig…).  Detection combines:

* ``nvidia-smi --query-compute-apps=pid,process_name,gpu_id,used_memory``
  (which GPU is busy, and by which PID);
* the ``PS_MINERS`` section (``ps -eo user,pid,pcpu,etimes,args``) to read
  the full command line of every user (root, ``screen``…).

Rules enforced here:

* the miner list is ordered (most specific binary first) so the reported name
  is stable — never rely on set/dict iteration order;
* the list of patterns is **configurable** (``name`` + process regex) so a
  home-made miner is detected without a new release;
* passwords (``--password``, ``-p``, ``--pass``, ``--api-password``…) are
  always replaced by ``•••``;
* wallets (``0x…``, ``prl1…``, ``bc1…``, long base58) are truncated: the full
  wallet must never reach the API, the UI, the audit log or the database.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

MASKED = "•••"

# Ordered: the first match wins, so longer / more specific names come first.
MINER_BINARIES: tuple[tuple[str, str], ...] = (
    ("srbminer-multi", "SRBMiner-MULTI"),
    ("srbminer", "SRBMiner"),
    ("cpuminer-opt", "cpuminer-opt"),
    ("cpuminer-multi", "cpuminer-multi"),
    ("cpuminer", "cpuminer"),
    ("wam-miner", "wam-miner"),
    ("bzminer", "BzMiner"),
    ("onezerominer", "OneZeroMiner"),
    ("peakminer", "PeakMiner"),
    ("lolminer", "lolMiner"),
    ("teamredminer", "TeamRedMiner"),
    ("phoenixminer", "PhoenixMiner"),
    ("nanominer", "Nanominer"),
    ("nbminer", "NBMiner"),
    ("gminer", "GMiner"),
    ("xmrig", "XMRig"),
    ("t-rex", "T-Rex"),
    ("rigel", "Rigel"),
)
MINER_TOKENS = tuple(binary for binary, _label in MINER_BINARIES)

# Miners that work on the CPU: their process marks the CPU device, not a GPU.
CPU_MINERS = frozenset(
    ("cpuminer", "cpuminer-opt", "cpuminer-multi", "wam-miner", "xmrig")
)

SECRET_FLAGS = frozenset(
    ("--password", "--pass", "--passwd", "--api-password", "--wallet-password", "-p")
)
WALLET_FLAGS = frozenset(("-u", "--user", "--wallet", "--address", "-w"))

WALLET_RE = re.compile(
    r"^(?:"
    r"0x[0-9a-fA-F]{40,64}"
    r"|prl1[0-9a-z]{20,}"
    r"|bc1[0-9a-z]{20,}"
    r"|(?:4|8)[0-9AB][1-9A-HJ-NP-Za-km-z]{90,106}"
    r")[.,;]?$"
)

VERSION_RE = re.compile(r"^v?(\d+\.\d+(?:\.\d+)?)[.,;]?$")

# A home-made script started from ``screen`` or a shell.
SCRIPT_PATTERN = r"(?:^|[\s/])(?:ba|da|z)?sh\s+\S*mine[-\w]*\.sh"

DEFAULT_PATTERNS: tuple[dict[str, str], ...] = tuple(
    {"name": label, "pattern": re.escape(binary)}
    for binary, label in MINER_BINARIES
) + ({"name": "Script de minage", "pattern": SCRIPT_PATTERN},)

PATTERN_MAX = 60


# ── Configurable patterns ─────────────────────────────────────────────
def normalize_patterns(raw: Any) -> list[dict[str, str]]:
    """Validate a stored pattern list; ``None``/empty falls back to defaults."""
    if raw is None:
        return [dict(entry) for entry in DEFAULT_PATTERNS]
    entries: list[dict[str, str]] = []
    if isinstance(raw, str):
        raw = [line for line in raw.splitlines()]
    if not isinstance(raw, (list, tuple)):
        return [dict(entry) for entry in DEFAULT_PATTERNS]
    for item in raw:
        if isinstance(item, str):
            name, _, pattern = item.partition("|")
            item = {"name": name.strip(), "pattern": pattern.strip()}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:PATTERN_MAX]
        pattern = str(item.get("pattern") or "").strip()
        if not name or not pattern or len(pattern) > 300:
            continue
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error:
            continue
        entries.append({"name": name, "pattern": pattern})
    return entries or [dict(entry) for entry in DEFAULT_PATTERNS]


def _compiled(patterns: Sequence[dict[str, str]] | None) -> list[tuple[str, re.Pattern[str]]]:
    entries = normalize_patterns(patterns)
    out: list[tuple[str, re.Pattern[str]]] = []
    for entry in entries:
        try:
            out.append((entry["name"], re.compile(entry["pattern"], re.IGNORECASE)))
        except re.error:  # pragma: no cover - normalize_patterns already filters
            continue
    return out


def get_patterns(db) -> list[dict[str, str]]:
    """Stored pattern list (``Configuration``), or the defaults."""
    from app.ai_config import get_setting

    return normalize_patterns(get_setting(db, "external_miner_patterns", None))


def set_patterns(db, raw: Any) -> list[dict[str, str]]:
    from app.ai_config import set_setting

    entries = normalize_patterns(raw)
    set_setting(db, "external_miner_patterns", entries)
    return entries


# ── Masking ───────────────────────────────────────────────────────────
def truncate_wallet(value: str) -> str:
    """Keep a readable prefix/suffix, never the whole wallet."""
    value = value or ""
    if len(value) <= 16:
        return value
    return value[:10] + "…" + value[-4:]


def _looks_like_wallet(token: str) -> bool:
    return bool(WALLET_RE.match(token))


def mask_command(cmd: str, max_len: int = 300) -> str:
    """Return *cmd* with passwords masked and wallets truncated."""
    if not cmd:
        return ""
    out: list[str] = []
    hide_next = False
    truncate_next = False
    for raw in str(cmd).split():
        if hide_next:
            out.append(MASKED)
            hide_next = False
            continue
        if truncate_next:
            out.append(truncate_wallet(raw))
            truncate_next = False
            continue
        base, sep, value = raw.partition("=")
        if base in SECRET_FLAGS:
            out.append(f"{base}={MASKED}" if sep else raw)
            hide_next = not sep
            continue
        if base in WALLET_FLAGS:
            out.append(f"{base}={truncate_wallet(value)}" if sep else raw)
            truncate_next = not sep
            continue
        # pool URL carrying credentials: scheme://user:pass@host → scheme://•••@host
        if "://" in raw and "@" in raw:
            scheme, rest = raw.split("://", 1)
            out.append(f"{scheme}://{MASKED}@{rest.split('@', 1)[1]}")
            continue
        if "@" in raw and ":" in raw.split("@", 1)[0]:
            out.append(f"{MASKED}@{raw.split('@', 1)[1]}")
            continue
        out.append(truncate_wallet(raw) if _looks_like_wallet(raw) else raw)
    return " ".join(out)[:max_len]


_RAW_JSON_SECRET = re.compile(
    r'("(?:passwd|password|pass|rig_id|api[-_]?key|api[-_]?password)"\s*:\s*")([^"]*)(")',
    re.IGNORECASE,
)
_RAW_WALLET_TOKEN = re.compile(
    r"(?<![0-9A-Za-z])(0x[0-9a-fA-F]{40,64}|prl1[0-9a-z]{20,}|bc1[0-9a-z]{20,}"
    r"|(?:4|8)[0-9AB][1-9A-HJ-NP-Za-km-z]{90,106})(?![0-9A-Za-z])"
)


def mask_raw_collect(text: str, max_len: int = 20000) -> str:
    """Mask a whole raw collection output (diagnostic screen).

    Two layers: JSON keys that carry HiveOS secrets (``passwd``, ``rig_id``…)
    and every command line (passwords + wallets) through :func:`mask_command`.
    """
    text = (text or "")[:max_len]
    text = _RAW_JSON_SECRET.sub(lambda m: m.group(1) + MASKED + m.group(3), text)
    text = _RAW_WALLET_TOKEN.sub(lambda m: truncate_wallet(m.group(0)), text)
    lines = [mask_command(line, max_len=400) for line in text.splitlines()]
    return "\n".join(lines)[:max_len]


# ── Command line parsing ──────────────────────────────────────────────
def miner_for(text: str) -> tuple[str, str]:
    """Return (binary, display name) of the first known miner found."""
    lower = (text or "").lower()
    for binary, label in MINER_BINARIES:
        if binary in lower:
            return binary, label
    return "", ""


def _flag_value(tokens: Sequence[str], flags: Iterable[str]) -> str:
    for index, token in enumerate(tokens):
        base, sep, value = token.partition("=")
        if base not in flags:
            continue
        if sep:
            return value
        if index + 1 < len(tokens):
            return tokens[index + 1]
    return ""


def parse_miner_cmd(cmd: str, name_hint: str = "") -> dict[str, str]:
    """Extract name, version, algorithm, pool and threads from a command line."""
    empty = {
        "name": "", "binary": "", "version": "", "algorithm": "",
        "pool": "", "threads": "",
    }
    text = (cmd or "").strip()
    if not text:
        return empty
    binary, label = miner_for(text)
    if not label and not name_hint:
        return empty
    label = label or name_hint

    tokens = text.split()
    version = ""
    for token in tokens[1:4]:
        match = VERSION_RE.match(token)
        if match:
            version = match.group(1)
            break

    algorithm = _flag_value(tokens, ("-a", "--algorithm", "--algo"))
    algorithm = algorithm.strip().rstrip(",;").removeprefix("--algorithm=")

    pool = _flag_value(
        tokens, ("-o", "--pool", "--url", "--stratum+tcp", "--stratum+ssl")
    )
    if "@" in pool:
        pool = pool.split("@", 1)[1]
    pool = pool.split("?", 1)[0].rstrip("/,")
    if _looks_like_wallet(pool):
        pool = ""

    threads = _flag_value(tokens, ("-t", "--threads", "--cpu-threads")).strip().rstrip(",;")

    return {
        "name": label,
        # The executable really running, not the matched pattern: a renamed
        # ``cpuminer-korsh`` stays readable in the UI (2026.11.003-c4).
        "binary": tokens[0].split("/")[-1].lower() if tokens else binary,
        "version": version,
        "algorithm": algorithm,
        "pool": pool,
        "threads": threads,
    }


# ── Coin inference from the pool host ─────────────────────────────────
def pool_host(value: str) -> str:
    """``stratum+tcp://user@pearl-eu2.luckypool.io:3360`` → ``pearl-eu2.luckypool.io``."""
    text = (value or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("@")[-1].split("/")[0].split("?")[0]
    return text.split(":")[0]


def infer_coin(pool: str, hosts: dict[str, str] | Iterable[tuple[str, str]]) -> str:
    """Ticker whose ``pool_hosts`` entry matches *pool* (exact, then longest).

    Catalogue entries are normalised the same way as the miner's pool, so a
    stored ``stratum+tcp://pearl-eu2.luckypool.io:3360`` still matches the host
    ``pearl-eu2.luckypool.io`` (2026.11.003-c4).
    """
    host = pool_host(pool)
    if not host:
        return ""
    pairs = hosts.items() if isinstance(hosts, dict) else hosts
    best, ticker = "", ""
    for key, value in pairs:
        needle = pool_host(str(key or "")) or str(key or "").strip().lower()
        if not needle:
            continue
        if needle == host:
            return str(value or "").upper()
        if needle and needle in host and len(needle) > len(best):
            best, ticker = needle, str(value or "").upper()
    return ticker


# ── ps rows ───────────────────────────────────────────────────────────
def parse_ps_rows(section: str) -> list[dict[str, Any]]:
    """Rows of ``ps -eo user,pid,pcpu,etimes,args``.

    Tolerates the old ``ps -eo args`` shape: when the first columns are not
    ``user pid pcpu etimes``, the whole line is the command line.
    """
    rows: list[dict[str, Any]] = []
    for line in (section or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("user ") or "grep" in line.lower():
            continue
        parts = line.split(None, 4)
        row: dict[str, Any] = {
            "user": "", "pid": None, "cpu_pct": None, "elapsed_s": None,
            "args": line,
        }
        if len(parts) == 5:
            pid = None
            cpu: float | None = None
            elapsed: int | None = None
            try:
                pid = int(parts[1])
                cpu = float(str(parts[2]).replace(",", "."))
                elapsed = int(parts[3])
            except (TypeError, ValueError):
                pid = None
            if pid is not None:
                row.update(
                    user=parts[0], pid=pid, cpu_pct=cpu, elapsed_s=elapsed,
                    args=parts[4],
                )
        rows.append(row)
    return rows


def match_pattern(args: str, patterns: Sequence[dict[str, str]] | None = None) -> str:
    for name, regex in _compiled(patterns):
        if regex.search(args or ""):
            return name
    return ""


def miner_kind(name: str, args: str) -> str:
    lower = (args or "").lower()
    if (
        name.lower() in CPU_MINERS
        or name.lower().startswith("script de minage")
        or "--disable-gpu" in lower
    ):
        return "cpu"
    if "--cpu-threads" in lower and "--gpu-id" not in lower:
        return "cpu"
    return "gpu"


# ── nvidia-smi --query-compute-apps ───────────────────────────────────
def _int_or_none(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value or "")
    return int(digits) if digits else None


def parse_compute_apps(section: str) -> list[dict[str, Any]]:
    """Parse ``--query-compute-apps`` CSV (3 or 4 columns, header optional)."""
    apps: list[dict[str, Any]] = []
    for line in (section or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("pid"):
            continue
        if "no running" in line.lower() or line.startswith("###"):
            continue
        parts = [part.strip() for part in line.split(",")]
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            continue
        if len(parts) >= 4:
            process_name = parts[1]
            gpu_id = _int_or_none(parts[2])
            used_mb = _int_or_none(parts[3])
        elif len(parts) == 3:
            process_name = parts[1]
            gpu_id = None
            used_mb = _int_or_none(parts[2])
        else:
            continue
        if not process_name:
            continue
        apps.append(
            {
                "pid": pid,
                "process_name": process_name,
                "gpu_id": gpu_id,
                "used_memory_mb": used_mb,
            }
        )
    return apps


def _ps_lines(section: str) -> list[str]:
    return [row["args"] for row in parse_ps_rows(section)]


def _cmd_for(process_name: str, ps_lines: list[str]) -> str:
    basename = process_name.split("/")[-1].lower()
    for line in ps_lines:
        head = line.split()[0].split("/")[-1].lower()
        if head == basename or (basename and basename in line.lower()):
            return line
    return ""


# ── 2026.11.003-c4: wrappers, grouping, solo nodes ────────────────────
# A miner started from ``screen``/``bash -c``/``unbuffer``… appears in ``ps`` as
# several processes (the launcher and the real binary).  Only the wrapped
# command is a miner; the launcher is stripped and the remaining command is the
# grouping key, so the launcher and its child collapse into **one** entry.
LAUNCHER_BINARIES = frozenset((
    "screen", "tmux", "script", "unbuffer", "nohup", "timeout", "sudo", "env",
    "setsid", "stdbuf", "taskset", "nice", "ionice", "su", "exec", "chrt",
    "caffeinate", "bash", "sh", "dash", "zsh", "ksh", "fish", "busybox",
))

# Option whose *argument is the wrapped command* (stop stripping right after).
_COMMAND_OPTIONS: dict[str, frozenset[str]] = {
    "bash": frozenset(("-c",)),
    "sh": frozenset(("-c",)),
    "dash": frozenset(("-c",)),
    "zsh": frozenset(("-c",)),
    "ksh": frozenset(("-c",)),
    "script": frozenset(("-c", "-qc", "--command")),
    "su": frozenset(("-c",)),
}
# Option taking a value that belongs to the launcher (window name, user, …).
_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "screen": frozenset(("-S", "-c", "-t", "-s", "-L", "-p", "-X", "-dmS")),
    "tmux": frozenset(("-L", "-f", "-S", "-c")),
    "timeout": frozenset(("-s", "-k", "--signal", "--kill-after")),
    "sudo": frozenset(("-u", "-g", "-p", "-C", "-h", "-U")),
    "nice": frozenset(("-n",)),
    "ionice": frozenset(("-c", "-n", "-p")),
    "env": frozenset(("-u", "-C", "-S")),
    "stdbuf": frozenset(("-i", "-o", "-e")),
    "taskset": frozenset(("-c", "-p")),
    "chrt": frozenset(("-p",)),
}

_REDIRECT_RE = re.compile(r"\s+\d?>{1,2}\s*\S+|\s+\d?>&\d|\s+&\s*$")
_STRATUM_RE = re.compile(r"stratum(?:\+(?:tcp|ssl|tls))?://", re.IGNORECASE)
_API_PORT_RE = re.compile(r"--api[-_]port[=\s]+(\d+)")

SOLO_LABEL = "Minage solo (nœud)"
DAEMON_BINARIES = frozenset((
    "korshd", "bitcoind", "bigcoind", "litecoind", "dogecoind", "dashd",
    "monerod", "firod", "particld", "pivxd", "grind", "verusd",
))
DEFAULT_COIN_HINTS: dict[str, str] = {
    "bitcoin": "BTC", "litecoin": "LTC", "dogecoin": "DOGE", "monero": "XMR",
    "dash": "DASH", "korsh": "KSH", "bigcoin": "BIG", "particl": "PART",
    "pivx": "PIVX", "firo": "FIRO", "verus": "VRSC",
}
COIN_ALIASES: dict[str, str] = {"PEARL": "PRL"}


def basename(token: str) -> str:
    return (token or "").rsplit("/", 1)[-1].strip().lower()


def split_launcher(args: str) -> tuple[str, str]:
    """``SCREEN -dmS wam bash -c exec /root/wam/wam-miner -o …`` → inner command.

    Returns ``("", args)`` when the line is not a wrapper, else
    ``("screen/bash/exec", "/root/wam/wam-miner -o …")`` so the wrapper and the
    real process share the same signature and group together.
    """
    tokens = (args or "").split()
    if not tokens or basename(tokens[0]) not in LAUNCHER_BINARIES:
        return "", (args or "").strip()
    layers: list[str] = []
    while tokens and basename(tokens[0]) in LAUNCHER_BINARIES:
        name = basename(tokens[0])
        layers.append(name)
        tokens = tokens[1:]
        takes_value = _VALUE_OPTIONS.get(name, frozenset())
        command_option = _COMMAND_OPTIONS.get(name, frozenset())
        index = 0
        while index < len(tokens) and tokens[index].startswith("-") and tokens[index] != "-":
            option = tokens[index]
            index += 1
            if option in command_option:
                break  # the following tokens are the wrapped command
            if option in takes_value and index < len(tokens):
                index += 1
        tokens = tokens[index:]
        if name == "timeout" and tokens and not tokens[0].startswith("-"):
            tokens = tokens[1:]  # timeout DURATION command
    return "/".join(layers), " ".join(tokens).strip()


def clean_command(cmd: str) -> str:
    """Strip shell redirections and ``script``'s trailing log file."""
    text = _REDIRECT_RE.sub("", " " + (cmd or "")).strip()
    tokens = text.split()
    while tokens and tokens[-1] in ("/dev/null", "2>&1", "&"):
        tokens.pop()
    return " ".join(tokens)


def looks_like_miner(cmd: str) -> bool:
    """An unknown binary is still a miner when it clearly talks to a pool."""
    text = cmd or ""
    low = text.lower()
    if not text.strip():
        return False
    if _STRATUM_RE.search(text):
        return True
    if "--cpu-threads" in low or low.endswith(" --cpu"):
        return True
    for flag in ("--url", "--pool", "--algo", "--algorithm"):
        if flag in low and ("." in low or "://" in low):
            return True
    return False


def normalize_coin(value: str, aliases: dict[str, str] | None = None) -> str:
    """Ticker upper-cased, HiveOS spellings folded (``PEARL`` → ``PRL``)."""
    coin = (value or "").strip().upper()
    table = {str(k).upper(): str(v).upper() for k, v in COIN_ALIASES.items()}
    for key, val in (aliases or {}).items():
        table[str(key).upper()] = str(val).upper()
    return table.get(coin, coin)


def coin_for_client(key: str, names: dict[str, str] | None = None) -> str:
    """Ticker of a node client (``korsh-cli`` → ``KSH``), catalogue first.

    The solo entries and the CPU probes must agree on the coin without sharing
    a mutable group: this is the one place the table is read (2026.11.003-c7).
    """
    return _resolve_key(key, {k.lower(): v for k, v in (names or {}).items()},
                        DEFAULT_COIN_HINTS)


def infer_coin_from_wallet(address: str, tickers: Iterable[str] | None = None) -> str:
    """``oms1test…`` → ``OMS`` when that ticker exists in the catalogue."""
    known = {str(t).upper() for t in (tickers or [])}
    if not known:
        return ""
    head = (address or "").split("1", 1)[0]
    if 1 <= len(head) <= 8 and head.upper() in known:
        return head.upper()
    return ""


def _datadir(args: str) -> str:
    match = re.search(r"-datadir[=\s]+(\S+)", args or "")
    return match.group(1) if match else ""


def _coin_key(text: str, names: dict[str, str]) -> str:
    """First path/argument component naming a known coin (``…/bigcoin/data``)."""
    for chunk in re.split(r"[/=:,]", text or ""):
        for token in re.split(r"[-_.]", chunk):
            token = token.strip().lower()
            if token and token in names:
                return token
    return ""


def _resolve_key(key: str, names: dict[str, str], hints: dict[str, str]) -> str:
    token = (key or "").strip().lower()
    if token in names:
        return names[token]
    if token in hints:
        return hints[token]
    return ""


SOLO_SCRIPT_RE = re.compile(
    r"(?:^|[\s/])([A-Za-z0-9_.\-]*(?:-miner|mine-)[A-Za-z0-9_.\-]*)\.sh\b"
)


def _solo_script_key(args: str) -> str:
    """Key of a solo mining script (``korsh-miner.sh``, ``mine-bigcoin.sh``)."""
    match = SOLO_SCRIPT_RE.search(args or "")
    if not match:
        return ""
    name = match.group(1)  # « .sh » n'est pas dans le groupe
    if name.startswith("mine-"):
        name = name[5:]
    for suffix in ("-miner", "-mining", "-mine"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def detect_solo_miners(
    rows: Sequence[dict[str, Any]],
    coin_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Node-driven solo mining, detected **stably** (2026.11.003-c8).

    A ``*-cli … generatetoaddress`` client is transient: collecting at the wrong
    second made the whole KORSH entry disappear from the rig.  The durable
    witnesses are the daemon (``*d -datadir=… -daemon`` consuming CPU) and the
    mining script (``*-miner.sh`` / ``mine-*.sh``, usually under ``screen``).
    Clients only count the workers; one entry per coin either way.
    """
    names = {str(k).lower(): str(v).upper() for k, v in (coin_names or {}).items()}
    hints = dict(DEFAULT_COIN_HINTS)
    clients: list[tuple[dict[str, Any], str]] = []
    daemons: list[tuple[dict[str, Any], str]] = []
    scripts: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        tokens = (row.get("args") or "").split()
        if not tokens:
            continue
        base = basename(tokens[0])
        # Un client lancé par un script (`sh /usr/local/bin/x-cli …`) reste un
        # client : le shell n'est qu'un interpréteur, pas le programme.
        if base in ("sh", "bash", "dash") and len(tokens) > 1 and basename(tokens[1]).endswith("-cli"):
            tokens = tokens[1:]
            base = basename(tokens[0])
        if base.endswith("-cli") and "generatetoaddress" in row["args"]:
            clients.append((row, base[:-4]))
        elif base in DAEMON_BINARIES and (
            "-daemon" in tokens or (row.get("cpu_pct") or 0) > 0
        ):
            daemons.append((row, base))
        else:
            # Un script n'est un témoin solo que lancé dans `screen` : un
            # `bash mine-x.sh` quelconque reste un « Script de minage ».
            key = _solo_script_key(row["args"]) if "screen" in row["args"].lower() else ""
            if key:
                scripts.append((row, key))
    if not clients and not daemons and not scripts:
        return []

    groups: dict[str, dict[str, Any]] = {}

    def group_for(key: str) -> dict[str, Any]:
        ticker = _resolve_key(key, names, hints)
        return groups.setdefault(
            ticker or (key or "?").upper(),
            {
                "coin": ticker, "key": key, "clients": 0, "scripts": set(),
                "pids": set(), "daemon": "", "cpu_pct": None, "user": "",
                "pid": None, "elapsed_s": None,
            },
        )

    def remember_pid(group: dict[str, Any], row: dict[str, Any]) -> None:
        group["pids"].add(row.get("pid"))
        if not group["user"]:
            group["user"] = row.get("user", "")
            group["pid"] = row.get("pid")
            group["elapsed_s"] = row.get("elapsed_s")

    for row, key in clients:
        group = group_for(key)
        group["clients"] += 1
        remember_pid(group, row)

    for row, base in daemons:
        datadir = _datadir(row["args"])
        key = _coin_key(datadir, names) or _coin_key(row["args"], names) or base[:-1]
        group = group_for(key)
        group["daemon"] = base
        group["pids"].add(row.get("pid"))
        cpu = row.get("cpu_pct")
        if cpu is not None and (group["cpu_pct"] is None or cpu > group["cpu_pct"]):
            group["cpu_pct"] = cpu
            group["user"] = row.get("user", "") or group["user"]
            group["pid"] = row.get("pid")
            group["elapsed_s"] = row.get("elapsed_s")

    for row, key in scripts:
        group = group_for(key)
        group["scripts"].add(row.get("pid"))
        remember_pid(group, row)

    entries: list[dict[str, Any]] = []
    for group in groups.values():
        workers = group["clients"] or (1 if group["scripts"] else 0)
        daemon = group["daemon"] or ""
        entries.append({
            "name": SOLO_LABEL,
            "label": SOLO_LABEL,
            "miner_name": SOLO_LABEL,
            "binary": daemon,
            "kind": "solo",
            "version": "",
            "algorithm": "",
            "pool": "",
            "coin": group["coin"],
            "workers": workers,
            "count": 1,
            "threads": "",
            "user": group["user"],
            "pid": group["pid"],
            "cpu_pct": group["cpu_pct"],
            "elapsed_s": group["elapsed_s"],
            "api_port": None,
            "cmd": mask_command(
                f"{daemon or 'nœud'} -datadir=… · {workers} worker(s)"
            ),
            "_pids": {pid for pid in group["pids"] if pid},
            "_match": group["key"],
        })
    return entries


def _is_real_row(row: dict[str, Any]) -> bool:
    """Drop kernel threads, the collector's own command and empty rows."""
    args = (row.get("args") or "").strip()
    if not args or args.startswith("["):
        return False
    if (row.get("user") or "").lower() == "fossor":
        return False
    if "### HIVE_STAT" in args or "collect_stats" in args:
        return False
    if args.startswith("ps -eo") or args.startswith("awk "):
        return False
    return True


def detect_running_miners(
    section: str,
    patterns: Sequence[dict[str, str]] | None = None,
    coin_hosts: dict[str, str] | None = None,
    coin_tickers: Iterable[str] | None = None,
    coin_names: dict[str, str] | None = None,
    coin_aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Running miners of one machine, deduplicated and grouped by coin.

    * launchers are stripped and merged with the process they started;
    * identical processes (same binary + same arguments) become one entry with
      a ``count``;
    * node solo mining becomes one ``Minage solo (nœud)`` entry per coin;
    * the coin comes from the pool host (normalised), else from the wallet
      prefix, and HiveOS spellings are folded through the alias table.
    """
    from app.coins import coin_for_algo, coin_for_pool

    rows = [row for row in parse_ps_rows(section) if _is_real_row(row)]
    solo = detect_solo_miners(rows, coin_names)
    solo_pids: set[int] = set()
    for entry in solo:
        solo_pids |= entry.pop("_pids", set())

    entries: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        if row.get("pid") in solo_pids:
            continue
        args = row.get("args") or ""
        launcher, inner = split_launcher(args)
        target = clean_command(inner if launcher else args)
        if not target:
            continue
        # A launcher is matched on the program it wraps, never on the whole
        # line: ``bash /hive/bin/miner-run srbminer 1`` is the HiveOS runner,
        # not an SRBMiner process.
        name = match_pattern(
            basename(target.split()[0]) if launcher else target, patterns
        )
        if not name and launcher and re.search(SCRIPT_PATTERN, args, re.IGNORECASE):
            name = "Script de minage"
        if not name and looks_like_miner(target):
            name = basename(target.split()[0])
        if not name:
            continue
        details = parse_miner_cmd(target, name_hint=name)
        if not details["name"]:
            continue
        tokens = target.split()
        signature = (basename(tokens[0]), " ".join(tokens[1:]))
        group = entries.get(signature)
        if group is None:
            group = {
                "name": details["name"],
                "label": details["name"],
                "miner_name": details["name"],
                "binary": details["binary"],
                "kind": miner_kind(details["name"], target),
                "version": details["version"],
                "algorithm": details["algorithm"],
                "pool": details["pool"],
                "threads": details["threads"],
                "user": row.get("user", ""),
                "pid": row.get("pid"),
                "cpu_pct": row.get("cpu_pct"),
                "elapsed_s": row.get("elapsed_s"),
                "api_port": None,
                "cmd": mask_command(target),
                "coin": "",
                "count": 0,
                "_direct": 0,
                "_total": 0,
            }
            entries[signature] = group
            order.append(signature)
        group["_total"] += 1
        if not launcher:
            group["_direct"] += 1
        cpu = row.get("cpu_pct")
        if cpu is not None and (group["cpu_pct"] is None or cpu > group["cpu_pct"]):
            # The busiest process is the one that really mines.
            group["cpu_pct"] = cpu
            group["pid"] = row.get("pid")
            group["user"] = row.get("user", "")
            group["elapsed_s"] = row.get("elapsed_s")
            group["cmd"] = mask_command(target)

    miners: list[dict[str, Any]] = []
    for signature in order:
        group = entries[signature]
        direct, total = group.pop("_direct"), group.pop("_total")
        group["count"] = direct or total
        match = _API_PORT_RE.search(group["cmd"])
        group["api_port"] = int(match.group(1)) if match else None
        miners.append(group)

    # Node solo mining absorbs the script/CPU lines that drive it.
    for entry in solo:
        key = str(entry.pop("_match", "") or "")
        if key:
            miners = [
                miner for miner in miners
                if not (
                    miner["name"] == "Script de minage"
                    and key in (miner.get("cmd") or "").lower()
                )
            ]
        miners.append(entry)

    hosts = coin_hosts or {}
    tickers = {str(t).upper() for t in (coin_tickers or [])}
    for miner in miners:
        coin = miner.get("coin") or ""
        if not coin:
            coin = infer_coin(miner.get("pool", ""), hosts)
        if not coin:
            # Built-in table (003-c6): a real install started with an empty
            # coin catalogue, so pool → coin must not depend on it.
            coin = coin_for_pool(miner.get("pool", ""))
        if not coin:
            wallet = _flag_value((miner.get("cmd") or "").split(), WALLET_FLAGS)
            coin = infer_coin_from_wallet(wallet, tickers)
        if not coin:
            coin = coin_for_algo(miner.get("algorithm", ""))
        miner["coin"] = normalize_coin(coin, coin_aliases)

    cpu_miners = [m for m in miners if m["kind"] in ("cpu", "solo")]
    api_port = next((m["api_port"] for m in miners if m.get("api_port")), None)
    # Stable order: the process that exposes an API (the one whose hashrate is
    # read) first, then the others, node solo mining last.
    miners.sort(key=lambda m: (
        m["kind"] == "solo",
        m.get("api_port") is None,
        m["name"].lower(),
        str(m.get("pid")),
    ))
    return {
        "miners": miners,
        "cpu_miners": cpu_miners,
        "api_port": api_port,
        "solo": [m for m in miners if m["kind"] == "solo"],
    }


def detect_external_miners(
    compute_section: str,
    ps_section: str = "",
    patterns: Sequence[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Merge compute apps and process list into external miner entries.

    Each returned dict: ``pid``, ``process_name``, ``gpu_id``,
    ``used_memory_mb``, ``miner_name``, ``kind``, ``version``,
    ``algorithm``, ``pool``, ``threads`` and ``cmd`` (masked/truncated).
    """
    rows = {row["pid"]: row for row in parse_ps_rows(ps_section) if row["pid"]}
    ps_lines = _ps_lines(ps_section)
    results: list[dict[str, Any]] = []

    for app in parse_compute_apps(compute_section):
        cmd = _cmd_for(app["process_name"], ps_lines)
        name = match_pattern(cmd or app["process_name"], patterns)
        parsed = parse_miner_cmd(cmd or app["process_name"], name_hint=name)
        if not parsed["name"]:
            continue
        row = rows.get(app["pid"], {})
        results.append(
            {
                **app,
                "miner_name": parsed["name"],
                "kind": "gpu",
                "user": row.get("user", ""),
                "cpu_pct": row.get("cpu_pct"),
                "version": parsed["version"],
                "algorithm": parsed["algorithm"],
                "pool": parsed["pool"],
                "threads": parsed["threads"],
                "cmd": mask_command(cmd),
            }
        )

    if results:
        return results

    # No compute-app data (AMD rig, CPU miner, older driver): use the ps list.
    for row in parse_ps_rows(ps_section):
        line = row["args"]
        name = match_pattern(line, patterns)
        if not name:
            continue
        parsed = parse_miner_cmd(line, name_hint=name)
        if not parsed["name"]:
            continue
        results.append(
            {
                "pid": row.get("pid"),
                "process_name": line.split()[0].split("/")[-1],
                "gpu_id": None,
                "used_memory_mb": None,
                "miner_name": parsed["name"],
                "kind": miner_kind(parsed["name"], line),
                "user": row.get("user", ""),
                "cpu_pct": row.get("cpu_pct"),
                "elapsed_s": row.get("elapsed_s"),
                "version": parsed["version"],
                "algorithm": parsed["algorithm"],
                "pool": parsed["pool"],
                "threads": parsed["threads"],
                "cmd": mask_command(line),
            }
        )
    return results
