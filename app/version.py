"""Version strings ``ANNEE.MOIS.NNN[-suffixe]`` (§4.18).

Ordering rules
--------------
* ``2026.11.001 < 2026.11.001-c1 < 2026.11.002 < 2026.12.001``;
* a suffix other than a corrective ``-cN`` (``-dev``, ``-rc1``…) sorts *older*
  than the bare version, because it is a pre-release of it.

A corrective build ``-c1`` is a re-cut of the same release, so it sorts *after*
the bare version but before the next release number.
"""

from __future__ import annotations

import re

_VERSION_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{3})(?:-([A-Za-z0-9.]+))?$")
_CORRECTIVE_RE = re.compile(r"^c(\d+)$", re.IGNORECASE)

# Suffix ranks: pre-release < final release < corrective re-cut of that release.
_RANK_PRERELEASE = 0
_RANK_FINAL = 1
_RANK_CORRECTIVE = 2


def _suffix_rank(suffix: str | None) -> tuple[int, int]:
    if not suffix:
        return (_RANK_FINAL, 0)
    match = _CORRECTIVE_RE.match(suffix.strip())
    if match:
        return (_RANK_CORRECTIVE, int(match.group(1)))
    return (_RANK_PRERELEASE, 0)


def parse_version(value: str) -> tuple[int, int, int, int, int]:
    """Return a sortable tuple; raise :class:`ValueError` on a malformed version."""
    text = (value or "").strip()
    match = _VERSION_RE.match(text)
    if not match:
        raise ValueError(f"version invalide : {value!r} (attendu ANNEE.MOIS.NNN[-cX])")
    rank, number = _suffix_rank(match.group(4))
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), rank, number)


def is_valid_version(value: str) -> bool:
    try:
        parse_version(value)
    except ValueError:
        return False
    return True


def compare_versions(left: str, right: str) -> int:
    """-1 if *left* is older, 0 if equal, 1 if *left* is newer."""
    a = parse_version(left)
    b = parse_version(right)
    return (a > b) - (a < b)


def newest_version(values) -> str | None:
    """Highest valid version in *values* (invalid entries ignored)."""
    best: str | None = None
    for value in values:
        if not is_valid_version(value):
            continue
        if best is None or compare_versions(value, best) > 0:
            best = value
    return best


def versions_since(installed: str, values) -> list[str]:
    """Versions strictly newer than *installed*, oldest first."""
    newer = [
        value for value in values
        if is_valid_version(value) and compare_versions(value, installed) > 0
    ]
    return sorted(newer, key=parse_version)
