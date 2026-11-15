from fastapi import Depends, HTTPException, Request

from app.auth import get_current_user


async def require_login(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
