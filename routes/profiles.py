from __future__ import annotations

import json

import asyncpg
from fastapi import APIRouter, Body, Depends

from core import verify_token
from db import get_db

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.get("")
async def list_profiles(db: asyncpg.Connection = Depends(get_db)):
    rows = await db.fetch(
        "SELECT user_id, profile_data, updated_at FROM user_profiles ORDER BY updated_at DESC"
    )
    return {"profiles": [dict(r) for r in rows], "count": len(rows)}


@router.get("/{user_id}")
async def get_profile(user_id: str, db: asyncpg.Connection = Depends(get_db)):
    row = await db.fetchrow(
        "SELECT user_id, profile_data, updated_at FROM user_profiles WHERE user_id = $1",
        user_id,
    )
    if not row:
        return {"user_id": user_id, "profile_data": {}, "updated_at": None}
    return {"profile": dict(row)}


@router.post("/{user_id}")
async def update_profile(
    user_id: str,
    profile_data: dict = Body(...),
    db: asyncpg.Connection = Depends(get_db),
    _: None = Depends(verify_token),
):
    row = await db.fetchrow(
        """
        INSERT INTO user_profiles (user_id, profile_data, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            profile_data = user_profiles.profile_data || EXCLUDED.profile_data,
            updated_at = NOW()
        RETURNING user_id, profile_data, updated_at
    """,
        user_id,
        json.dumps(profile_data),
    )
    return {"status": "updated", "profile": dict(row)}
