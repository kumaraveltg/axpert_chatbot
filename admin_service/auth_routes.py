"""
admin_service/auth_routes.py
All auth endpoints — login, users CRUD, change password
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session

from shared.database import get_db
from shared.models import User
from shared.auth import (
    create_token,
    hash_password,
    verify_password,
    require_admin,
    get_current_user
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Pydantic schemas ──────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class CreateUserRequest(BaseModel):
    username:    str
    password:    Optional[str] = "newuser"
    role:        Optional[str] = "user"
    schema_name: Optional[str] = None

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


# ── Login ─────────────────────────────────────────────────────

@router.post("/login")
async def login(req: LoginRequest):
    db   = next(get_db())
    user = db.query(User).filter_by(
        username  = req.username,
        is_active = 'Y'
    ).first()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(req.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_token(user)
    return {
        "token":       token,
        "role":        user.role,
        "username":    user.username,
        "schema_name": user.schema_name
    }


# ── Current user info ─────────────────────────────────────────

@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return current_user


# ── Change password (any logged in user) ──────────────────────

@router.post("/change-password")
async def change_password(
    req:          ChangePasswordRequest,
    current_user: dict = Depends(get_current_user)
):
    db   = next(get_db())
    user = db.query(User).filter_by(
        id        = int(current_user["sub"]),
        is_active = 'Y'
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not verify_password(req.old_password, user.password):
        raise HTTPException(status_code=401, detail="Old password is incorrect")

    if len(req.new_password) < 6:
        raise HTTPException(
            status_code = 400,
            detail      = "New password must be at least 6 characters"
        )

    user.password = hash_password(req.new_password)
    db.commit()
    return {"message": "Password changed successfully ✅"}


# ── List users (admin only) ───────────────────────────────────

@router.get("/users", dependencies=[Depends(require_admin)])
async def list_users():
    db    = next(get_db())
    users = db.query(User).filter_by(is_active='Y').all()
    return [
        {
            "id":          u.id,
            "username":    u.username,
            "role":        u.role,
            "schema_name": u.schema_name
        }
        for u in users
    ]


# ── Create user (admin only) ──────────────────────────────────

@router.post("/users", dependencies=[Depends(require_admin)])
async def create_user(req: CreateUserRequest):
    db = next(get_db())

    exists = db.query(User).filter_by(username=req.username).first()
    if exists:
        raise HTTPException(
            status_code = 400,
            detail      = "Username already exists"
        )

    # Default username = schema_name if not provided
    username = req.username or req.schema_name

    user = User(
        username    = username,
        password    = hash_password(req.password or "newuser"),
        role        = req.role or "user",
        schema_name = req.schema_name
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        "id":           user.id,
        "username":     user.username,
        "role":         user.role,
        "schema_name":  user.schema_name,
        "default_pass": "newuser"
    }


# ── Reset password (admin only) ───────────────────────────────

@router.post("/users/{user_id}/reset-password",
             dependencies=[Depends(require_admin)])
async def reset_password(user_id: int):
    """Reset any user password back to newuser"""
    db   = next(get_db())
    user = db.query(User).filter_by(id=user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.password = hash_password("newuser")
    db.commit()
    return {"message": f"Password reset to 'newuser' for {user.username} ✅"}


# ── Delete user (admin only) ──────────────────────────────────

@router.delete("/users/{user_id}",
               dependencies=[Depends(require_admin)])
async def delete_user(user_id: int):
    db   = next(get_db())
    user = db.query(User).filter_by(id=user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = 'N'
    db.commit()
    return {"message": f"User {user.username} deactivated ✅"}