"""Detection of external miners (§2.6).

A machine may already mine with a miner Fossor does not manage (SRBMiner,
lolMiner, xmrig…).  Detection combines:

* ``nvidia-smi --query-compute-apps=pid,process_name,gpu_id,used_memory``
  (which GPU is busy, and by which PID);
* the ``PS_MINERS`` section (``ps -eo args``) to read the full command line.

Rules enforced here:

* the miner list is ordered (most specific binary first) so the reported name
  is stable — never rely on set/dict iteration order;
* passwords (``--password``, ``-p``, ``--pass``, ``--api-password``…) are
  always replaced by ``•••``;
* wallets (``0x…``, ``prl1…``, ``bc1…``, long base58) are truncated: the full
  wallet must never reach the API, the UI, the audit log or the database.
"""

from __future__ import annotations

import re
from typing import Any

MASKED = "•••"

# Ordered: the first match wins, so longer / more specific names come first.
MINER_BINARIES: tuple[tuple[str, str], ...] = (
    ("srbminer-multi", "SRBMiner-MULTI"),
    ("srbminer", "SRBMiner"),
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


def parse_miner_cmd(cmd: str) -> dict[str, str]:
    """Extract name, version, algorithm and pool from a miner command line."""
    empty = {"name": "", "binary": "", "version": "", "algorithm": "", "pool": ""}
    text = (cmd or "").strip()
    if not text:
        return empty
    binary, label = miner_for(text)
    if not label:
        return empty

    tokens = text.split()
    version = ""
    for token in tokens[1:4]:
        match = VERSION_RE.match(token)
        if match:
            version = match.group(1)
            break

    algorithm = _flag_value(tokens, ("--algorithm", "--algo"))
    algorithm = algorithm.strip().rstrip(",;")

    pool = _flag_value(tokens, ("--pool", "--url", "-o", "--stratum+tcp", "--stratum+ssl"))
    if "@" in pool:
        pool = pool.split("@", 1)[1]
    pool = pool.split("?", 1)[0].rstrip("/,")
    if _looks_like_wallet(pool):
        pool = ""

    return {
        "name": label,
        "binary": binary,
        "version": version,
        "algorithm": algorithm.removeprefix("--algorithm="),
        "pool": pool,
    }


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
    lines = []
    for line in (section or "").splitlines():
        line = line.strip()
        if not line or "grep" in line.lower():
            continue
        lines.append(line)
    return lines


def _cmd_for(process_name: str, ps_lines: list[str]) -> str:
    basename = process_name.split("/")[-1].lower()
    for line in ps_lines:
        head = line.split()[0].split("/")[-1].lower()
        if head == basename or (basename and basename in line.lower()):
            return line
    return ""


def detect_external_miners(
    compute_section: str, ps_section: str = ""
) -> list[dict[str, Any]]:
    """Merge compute apps and process list into external miner entries.

    Each returned dict: ``pid``, ``process_name``, ``gpu_id``,
    ``used_memory_mb``, ``miner_name``, ``version``, ``algorithm``, ``pool``
    and ``cmd`` (already masked/truncated).
    """
    ps_lines = _ps_lines(ps_section)
    results: list[dict[str, Any]] = []

    for app in parse_compute_apps(compute_section):
        cmd = _cmd_for(app["process_name"], ps_lines)
        parsed = parse_miner_cmd(cmd or app["process_name"])
        if not parsed["name"]:
            continue
        results.append(
            {
                **app,
                "miner_name": parsed["name"],
                "version": parsed["version"],
                "algorithm": parsed["algorithm"],
                "pool": parsed["pool"],
                "cmd": mask_command(cmd),
            }
        )

    if results:
        return results

    # No compute-app data (AMD rig, older driver): fall back to the ps list.
    for line in ps_lines:
        parsed = parse_miner_cmd(line)
        if not parsed["name"]:
            continue
        results.append(
            {
                "pid": None,
                "process_name": line.split()[0].split("/")[-1],
                "gpu_id": None,
                "used_memory_mb": None,
                "miner_name": parsed["name"],
                "version": parsed["version"],
                "algorithm": parsed["algorithm"],
                "pool": parsed["pool"],
                "cmd": mask_command(line),
            }
        )
    return results
