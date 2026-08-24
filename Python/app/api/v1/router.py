"""Aggregates every v1 endpoint router behind one APIRouter."""
from fastapi import APIRouter

from app.api.v1.endpoints import (
    analytics,
    auth,
    dashboard,
    documents,
    health,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(documents.router)
api_router.include_router(dashboard.router)
api_router.include_router(analytics.router)
