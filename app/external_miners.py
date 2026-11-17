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


def _flag_value(tokens: list[str], flags: tuple[str, ...]) -> str:
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
        "binary": binary or tokens[0].split("/")[-1].lower(),
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
    """Ticker whose ``pool_hosts`` entry matches *pool* (longest match wins)."""
    host = pool_host(pool)
    if not host:
        return ""
    pairs = hosts.items() if isinstance(hosts, dict) else hosts
    best, ticker = "", ""
    for key, value in pairs:
        needle = str(key or "").strip().lower()
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
