import os
import bcrypt
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Response, Request
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

router = APIRouter(prefix="/api", tags=["auth"])

SERIALIZER: URLSafeTimedSerializer | None = None
ACCESS_TOKEN_EXPIRE_MINUTES = 60

ADMIN_PASSWORD_HASH: str | None = None


def _get_data_dir() -> Path:
    """Return the /data directory (volume mount point)."""
    p = Path("/data")
    if p.is_dir():
        return p
    # Fallback for dev / tests
    return Path(__file__).resolve().parent.parent / "data"


def _load_or_create_serializer() -> URLSafeTimedSerializer:
    global SERIALIZER
    if SERIALIZER is not None:
        return SERIALIZER
    data_dir = _get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = data_dir / "fossor_signing_key"
    if key_file.exists():
        key = key_file.read_text().strip()
    else:
        import secrets
        key = secrets.token_urlsafe(48)
        key_file.write_text(key)
        os.chmod(key_file, 0o600)
    SERIALIZER = URLSafeTimedSerializer(key)
    return SERIALIZER


def get_admin_password_hash() -> str | None:
    global ADMIN_PASSWORD_HASH
    if ADMIN_PASSWORD_HASH is None:
        ADMIN_PASSWORD_HASH = os.environ.get("FOSSOR_ADMIN_PASSWORD_HASH")
        pw_env = os.environ.get("FOSSOR_ADMIN_PASSWORD")
        if not ADMIN_PASSWORD_HASH and pw_env:
            salt = bcrypt.gensalt()
            ADMIN_PASSWORD_HASH = bcrypt.hashpw(pw_env.encode("utf-8"), salt).decode("utf-8")
        elif not ADMIN_PASSWORD_HASH and not pw_env:
            ADMIN_PASSWORD_HASH = None
    return ADMIN_PASSWORD_HASH


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(data: dict) -> str:
    s = _load_or_create_serializer()
    payload = {**data, "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)}
    import json
    return s.dumps(json.dumps(payload))


def get_current_user(request: Request) -> str | None:
    token = request.cookies.get("fossor_session")
    if not token:
        return None
    try:
        s = _load_or_create_serializer()
        payload_str = s.loads(token)
        import json
        payload = json.loads(payload_str)
        return payload.get("sub")
    except (SignatureExpired, BadSignature, ValueError):
        return None


def require_login(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@router.post("/login")
def login(request: Request, response: Response, username: str = Form(...), password: str = Form(...)):
    admin_hash = get_admin_password_hash()
    if not admin_hash:
        raise HTTPException(status_code=500, detail="Admin password not configured")
    if username == "admin" and verify_password(password, admin_hash):
        token = create_access_token({"sub": "admin"})
        response.set_cookie(
            key="fossor_session",
            value=token,
            httponly=True,
            samesite="strict",
            path="/",
            max_age=3600,
        )
        return {"ok": True, "message": "Connected"}
    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key="fossor_session", path="/")
    return {"ok": True}
