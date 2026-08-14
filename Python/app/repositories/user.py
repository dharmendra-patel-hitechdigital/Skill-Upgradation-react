"""User data access."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.user import User, UserRole
from app.schemas.user import UserAdminUpdate, UserCreate, UserUpdate


async def get_by_id(db: AsyncSession, user_id: int) -> User | None:
    return await db.get(User, user_id)


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    """Look up by normalised (lower-cased) email."""
    stmt = select(User).where(User.email == email.strip().lower())
    return await db.scalar(stmt)


async def email_exists(db: AsyncSession, email: str, *, exclude_id: int | None = None) -> bool:
    stmt = select(func.count()).select_from(User).where(User.email == email.strip().lower())
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return bool(await db.scalar(stmt))


async def count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(User)) or 0)


async def create(
    db: AsyncSession, payload: UserCreate, *, role: UserRole = UserRole.USER
) -> User:
    user = User(
        email=str(payload.email),
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=role,
    )
    db.add(user)
    await db.flush()
    return user


async def update_profile(db: AsyncSession, user: User, payload: UserUpdate) -> User:
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(user, field, str(value) if field == "email" else value)
    await db.flush()
    return user


async def set_password(db: AsyncSession, user: User, new_password: str) -> User:
    user.hashed_password = hash_password(new_password)
    await db.flush()
    return user


async def admin_update(db: AsyncSession, user: User, payload: UserAdminUpdate) -> User:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await db.flush()
    return user


async def list_users(db: AsyncSession, *, offset: int, limit: int) -> tuple[list[User], int]:
    """Admin listing. Returns the page plus the unpaginated total."""
    total = int(await db.scalar(select(func.count()).select_from(User)) or 0)
    stmt = select(User).order_by(User.id).offset(offset).limit(limit)
    rows = list((await db.scalars(stmt)).all())
    return rows, total
