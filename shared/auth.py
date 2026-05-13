"""
shared/auth.py
JWT utility — used by admin_service
"""

from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from datetime import datetime, timedelta
import jwt
import bcrypt
import os

SECRET_KEY  = os.getenv("JWT_SECRET", "axpert-secret-change-in-production")
ALGORITHM   = "HS256"
EXPIRE_DAYS = 7

security = HTTPBearer()


# ── Token creation ────────────────────────────────────────────

def create_token(user) -> str:
    # Admin gets 7 days, end user gets 1 day
    expire = timedelta(days=7) if user.role == 'admin' else timedelta(days=1)
    
    payload = {
        "sub":         str(user.id),
        "username":    user.username,
        "role":        user.role,
        "schema_name": user.schema_name,
        "exp":         datetime.utcnow() + expire
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


# ── Token decode ──────────────────────────────────────────────

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── Password helpers ──────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(
        plain.encode(), bcrypt.gensalt()
    ).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── FastAPI dependencies ──────────────────────────────────────

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> dict:
    """Any logged in user — admin or user"""
    return decode_token(credentials.credentials)


def require_admin(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> dict:
    """Admin only — throws 403 if role is not admin"""
    payload = decode_token(credentials.credentials)
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code = 403,
            detail      = "Admin access required"
        )
    return payload