from fastapi import Depends, HTTPException, Request

from app.auth import get_current_user


async def require_login(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def require_hermes_token(request: Request):
    """Inbound Hermes token (§5.4): ``Authorization: Bearer <token>``.

    Only a SHA-256 digest is stored, so a database dump does not leak a working
    token.  Used by the phase-4 Hermes API (``/api/hermes/*``).
    """
    from app.ai_config import check_token
    from app.database import SessionLocal

    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Jeton Hermes manquant",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = header[7:].strip()
    db = SessionLocal()
    try:
        valid = check_token(db, token)
    finally:
        db.close()
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Jeton Hermes invalide",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "hermes"
