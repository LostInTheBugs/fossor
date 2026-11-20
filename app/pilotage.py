"""Qui peut être piloté : frontière ASIC et simulation par machine (préprod).

Deux règles, tenues ici pour être testables sans passer par l'UI :

* un ASIC est joint par IP, en **lecture seule** : il alimente le suivi mais
  n'entre jamais dans une campagne, une proposition Hermes, la règle « heures
  chères » ni la capacité libre du contexte ;
* une action réelle exige **une** autorisation : la machine doit être armée
  (« PILOTER »).  Une machine non armée reste simulée quel que soit le global ;
  le global ON est seulement la valeur par défaut des machines non armées, et
  le passer à OFF n'arme aucune machine.
"""

from __future__ import annotations

from typing import Any, Iterable

CONFIRM_WORD = "PILOTER"


# ── ASIC : suivi uniquement ───────────────────────────────────────────
def is_asic(machine: Any) -> bool:
    kind = str(getattr(machine, "machine_kind", "host") or "host").strip().lower()
    return kind == "asic"


def asic_ids(db: Any) -> set[int]:
    from app.models import Machine

    return {
        int(row[0])
        for row in db.query(Machine.id).filter(Machine.machine_kind == "asic").all()
    }


def without_asic(entries: Iterable[dict[str, Any]], ids: set[int]) -> list[dict[str, Any]]:
    return [entry for entry in entries if int(entry.get("machine_id") or 0) not in ids]


def asic_refusal(machine: Any) -> str:
    return (
        f"{getattr(machine, 'name', 'machine')} : un ASIC n'est pas pilotable — "
        "il sert uniquement au suivi (lecture seule, par IP)"
    )


def pilotable_machines(db: Any) -> list[Any]:
    """Machines proposables au pilotage : jamais un ASIC."""
    from app.models import Machine

    return (
        db.query(Machine)
        .filter(Machine.enabled.is_(True), Machine.machine_kind != "asic")  # noqa: E712
        .order_by(Machine.name.asc())
        .all()
    )


# ── Simulation par machine ────────────────────────────────────────────
def global_simulation(db: Any) -> bool:
    from app.routers.miners import simulation_enabled

    return simulation_enabled(db)


def machine_armed(machine: Any) -> bool:
    """La machine a reçu son mot « PILOTER » : elle accepte le pilotage réel."""
    return bool(getattr(machine, "simulation_disabled", False))


def simulated_for(db: Any, machine: Any) -> bool:
    """La machine est-elle simulée ? **Une seule règle fait foi.**

    * machine armée (« PILOTER ») → actions réelles, même si le global est ON ;
    * machine non armée → simulation, toujours, quel que soit le global.

    Le global ON reste la valeur par défaut des machines non armées : le passer
    à OFF n'arme **aucune** machine.
    """
    return not machine_armed(machine)


def label(db: Any, machine: Any) -> str:
    if machine_armed(machine):
        return "Pilotage réel"
    return "Simulation (globale)" if global_simulation(db) else "Simulation (machine non armée)"


def campaign_locked(db: Any, campaign: Any) -> bool:
    """Au moins une machine de la campagne est simulée : le lot l'est aussi."""
    from app.campaign_runner import campaign_targets

    return any(simulated_for(db, target.machine) for target in campaign_targets(db, campaign))


def campaign_simulated(db: Any, campaign: Any) -> bool:
    """Une campagne est réelle seulement si **toutes** ses machines sont armées.

    Une cible non armée rend la campagne simulée pour tout le lot : aucune
    machine n'est pilotée par ricochet.  Toute la décision passe par
    :func:`simulated_for`, jamais par le global.
    """
    from app.campaign_runner import campaign_targets

    targets = campaign_targets(db, campaign)
    return not targets or any(simulated_for(db, target.machine) for target in targets)


def set_machine_real(db: Any, machine: Any, real: bool, confirm: str | None) -> str:
    """Arme (mot « PILOTER ») ou désarme le pilotage réel d'une machine."""
    if is_asic(machine):
        raise ValueError(asic_refusal(machine))
    if real and str(confirm or "").strip() != CONFIRM_WORD:
        raise ValueError(
            f"Désactivation de la simulation refusée : saisissez « {CONFIRM_WORD} » pour confirmer"
        )
    machine.simulation_disabled = bool(real)
    db.commit()
    return label(db, machine)
