"""Routes du coffre des wallets.

Deux surfaces volontairement séparées :

- ``/api/vault/*`` — administration, **cookie de session uniquement**.  Le
  contournement d'authentification des réseaux de confiance ne s'applique pas
  ici : la phrase maîtresse est toujours exigée, quelle que soit l'IP.
- ``/api/hermes/wallets`` — dépôt scellé avec le jeton Hermes.  Hermes peut
  déposer, jamais relire : aucune route de lecture n'existe.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app import auth, vault
from app.database import get_db
from app.dependencies import require_hermes_token

admin_router = APIRouter(prefix="/api/vault", tags=["vault"])
hermes_router = APIRouter(prefix="/api/hermes", tags=["hermes"])


def require_vault_admin(request: Request) -> str:
    """Session réelle obligatoire : le réseau de confiance n'ouvre pas le coffre."""
    user = auth.cookie_user(request)
    if not user:
        raise HTTPException(
            status_code=401,
            detail=(
                "Coffre : connexion requise. L'authentification désactivée pour "
                "le réseau de confiance ne s'applique pas au coffre."
            ),
        )
    return user


def _fail(exc: vault.VaultError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@admin_router.get("/status")
def vault_status(
    db: Session = Depends(get_db),
    _user: str = Depends(require_vault_admin),
):
    return vault.status(db)


@admin_router.post("/init")
def vault_init(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: str = Depends(require_vault_admin),
):
    try:
        vault.init_vault(
            db,
            str(payload.get("passphrase") or ""),
            str(payload.get("confirm") or ""),
            lock_minutes=payload.get("lock_minutes", vault.DEFAULT_LOCK_MINUTES),
            actor=user,
        )
    except vault.VaultError as exc:
        raise _fail(exc)
    return vault.status(db)


@admin_router.post("/unlock")
def vault_unlock(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: str = Depends(require_vault_admin),
):
    try:
        return vault.unlock(db, str(payload.get("passphrase") or ""), actor=user)
    except vault.VaultError as exc:
        raise _fail(exc)


@admin_router.post("/lock")
def vault_lock(
    db: Session = Depends(get_db),
    user: str = Depends(require_vault_admin),
):
    vault.lock()
    vault.audit(db, user, "vault-lock", "vault", "ok", "verrouillage manuel")
    return vault.status(db)


@admin_router.post("/settings")
def vault_settings(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: str = Depends(require_vault_admin),
):
    try:
        return vault.set_lock_minutes(db, payload.get("lock_minutes"), actor=user)
    except vault.VaultError as exc:
        raise _fail(exc)


@admin_router.get("/wallets")
def vault_wallets(
    db: Session = Depends(get_db),
    _user: str = Depends(require_vault_admin),
):
    try:
        vault.require_initialized(db)
    except vault.VaultError as exc:
        raise _fail(exc)
    return {"wallets": vault.list_wallets(db)}


@admin_router.post("/wallets", status_code=201)
def vault_wallet_add(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: str = Depends(require_vault_admin),
):
    try:
        row = vault.add_wallet(
            db,
            payload,
            payload.get("secrets") or {},
            actor=user,
            origin=str(payload.get("origin") or "fred"),
        )
    except vault.VaultError as exc:
        raise _fail(exc)
    return row


@admin_router.put("/wallets/{wallet_id}")
def vault_wallet_edit(
    wallet_id: int,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: str = Depends(require_vault_admin),
):
    try:
        return vault.update_wallet(
            db, wallet_id, payload, payload.get("secrets") or {}, actor=user
        )
    except vault.VaultError as exc:
        raise _fail(exc)


@admin_router.delete("/wallets/{wallet_id}")
def vault_wallet_delete(
    wallet_id: int,
    db: Session = Depends(get_db),
    user: str = Depends(require_vault_admin),
):
    try:
        return vault.delete_wallet(db, wallet_id, actor=user)
    except vault.VaultError as exc:
        raise _fail(exc)


@admin_router.post("/wallets/{wallet_id}/reveal")
def vault_wallet_reveal(
    wallet_id: int,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: str = Depends(require_vault_admin),
):
    try:
        return vault.reveal(
            db,
            wallet_id,
            str(payload.get("field") or ""),
            str(payload.get("passphrase") or ""),
            actor=user,
        )
    except vault.VaultError as exc:
        raise _fail(exc)


@admin_router.post("/passphrase")
def vault_change_passphrase(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: str = Depends(require_vault_admin),
):
    try:
        return vault.change_passphrase(
            db,
            str(payload.get("current") or ""),
            str(payload.get("new") or ""),
            str(payload.get("confirm") or ""),
            actor=user,
        )
    except vault.VaultError as exc:
        raise _fail(exc)


@admin_router.post("/export")
def vault_export(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: str = Depends(require_vault_admin),
):
    try:
        raw = vault.export_vault(db, str(payload.get("passphrase") or ""), actor=user)
    except vault.VaultError as exc:
        raise _fail(exc)
    return Response(
        content=raw,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="fossor-vault-export.json"'},
    )


@admin_router.post("/import/registre")
def vault_import_registre(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: str = Depends(require_vault_admin),
):
    try:
        return vault.import_from_registre(
            db,
            str(payload.get("text") or ""),
            actor=user,
            dry_run=bool(payload.get("dry_run", True)),
        )
    except vault.VaultError as exc:
        raise _fail(exc)


# ── Surface Hermes : dépôt scellé, aucune relecture ─────────────────────────
@hermes_router.get("/wallets/pubkey")
def hermes_vault_pubkey(
    db: Session = Depends(get_db),
    _actor: str = Depends(require_hermes_token),
):
    try:
        key = vault.public_key(db)
    except vault.VaultError as exc:
        raise _fail(exc)
    return {
        "algorithm": "x25519",
        "sealed_box": "libsodium SealedBox (PyNaCl)",
        "public_key": key,
        "readable_by_api": False,
    }


@hermes_router.post("/wallets", status_code=201)
def hermes_vault_deposit(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    _actor: str = Depends(require_hermes_token),
):
    try:
        return vault.hermes_deposit(db, payload)
    except vault.VaultError as exc:
        raise _fail(exc)
