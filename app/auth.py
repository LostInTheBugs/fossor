import os
import bcrypt
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Response, Request
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

router = APIRouter(prefix="/api", tags=["auth"])

SERIALIZER: URLSafeTimedSerializer | None = None
ACCESS_TOKEN_EXPIRE_MINUTES = 60


def _get_data_dir() -> Path:
    """Return the data directory from env or default /data."""
    env_dir = os.environ.get("FOSSOR_DATA_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.is_dir():
            return p
    p = Path("/data")
    if p.is_dir():
        return p
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


def create_access_token(data: dict) -> str:
    """Create a signed token using itsdangerous TimestampSigner.

    The token payload contains only user data (e.g. sub).
    Expiry is enforced by itsdangerous TimestampSigner via max_age
    — no need for a hand-rolled 'exp' field.
    """
    s = _load_or_create_serializer()
    payload = data.copy()
    return s.dumps(payload)


def get_current_user(request: Request) -> str | None:
    """Decode the session cookie and verify expiry.

    Returns the 'sub' value (username) or None.
    """
    token = request.cookies.get("fossor_session")
    if not token:
        return None
    try:
        s = _load_or_create_serializer()
        payload = s.loads(token, max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
        return payload.get("sub")
    except (SignatureExpired, BadSignature, ValueError):
        return None


def require_login(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


@router.post("/login")
def login(request: Request, response: Response, username: str = Form(...), password: str = Form(...)):
    admin_hash = os.environ.get("FOSSOR_ADMIN_PASSWORD_HASH")
    if not admin_hash:
        pw_env = os.environ.get("FOSSOR_ADMIN_PASSWORD")
        if pw_env:
            admin_hash = hash_password(pw_env)
        else:
            # No env var: read admin hash from database
            from app.models import User
            from app.database import SessionLocal
            db = SessionLocal()
            try:
                admin = db.query(User).filter(User.username == "admin").first()
                if admin is None:
                    db.close()
                    raise HTTPException(status_code=500, detail="Admin user not found in database")
                admin_hash = admin.password_hash
            finally:
                db.close()
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
