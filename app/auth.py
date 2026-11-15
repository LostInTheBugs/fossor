import os
import bcrypt
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Response, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

router = APIRouter(prefix="/api", tags=["auth"])

SECRET_KEY = os.environ.get("MINECTL_SECRET_KEY", "forged-test-key-not-a-secret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

ADMIN_PASSWORD_HASH: str | None = None
ADMIN_PASSWORD_SALT: bytes | None = None


def get_admin_password_hash() -> str | None:
    global ADMIN_PASSWORD_HASH, ADMIN_PASSWORD_SALT
    if ADMIN_PASSWORD_HASH is None:
        ADMIN_PASSWORD_HASH = os.environ.get("MINECTL_ADMIN_PASSWORD_HASH")
        if not ADMIN_PASSWORD_HASH and os.environ.get("MINECTL_ADMIN_PASSWORD"):
            pw = os.environ["MINECTL_ADMIN_PASSWORD"].encode("utf-8")
            ADMIN_PASSWORD_SALT = bcrypt.gensalt()
            ADMIN_PASSWORD_HASH = bcrypt.hashpw(pw, ADMIN_PASSWORD_SALT).decode("utf-8")
            # Persist it for subsequent starts
            env_path = Path("/home/administrator/work/minectl/.env")
            if env_path.exists():
                lines = env_path.read_text().splitlines()
                found = False
                new_lines = []
                for line in lines:
                    if line.startswith("MINECTL_ADMIN_PASSWORD_HASH="):
                        new_lines.append(f"MINECTL_ADMIN_PASSWORD_HASH={ADMIN_PASSWORD_HASH}")
                        found = True
                    else:
                        new_lines.append(line)
                if not found:
                    new_lines.append(f"MINECTL_ADMIN_PASSWORD_HASH={ADMIN_PASSWORD_HASH}")
                env_path.write_text("\n".join(new_lines) + "\n")
    return ADMIN_PASSWORD_HASH


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(request: Request) -> str | None:
    token = request.cookies.get("minectl_session")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
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
            key="minectl_session",
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
    response.delete_cookie(key="minectl_session", path="/")
    return {"ok": True}
