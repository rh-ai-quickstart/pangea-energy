"""Pangea Energy and Power — backend API.

Thin async orchestration layer between the PostgreSQL database and the dashboard
UI. The UI talks only to this API; this API is the only component that holds DB
credentials. ML inference (via OpenShift AI / KServe v2) will be wired in later.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db, tracing

logging.basicConfig(level=logging.INFO)
from .config import settings
from .routers import (
    actions, agent, alerts, fleet, maintenance, operators, shifts, solar,
    turbine_health, turbines, voice, wave, wind,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    tracing.init()
    yield
    await db.disconnect()


app = FastAPI(
    title="Pangea Energy and Power API",
    version="0.1.0",
    description="Read API for the wind-farm operations dashboard.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(fleet.router)
# before turbines.router: its /{turbine_code} route would otherwise
# capture /api/turbines/health as a turbine code.
app.include_router(turbine_health.router)
app.include_router(turbines.router)
app.include_router(alerts.router)
app.include_router(maintenance.router)
app.include_router(operators.router)
app.include_router(shifts.router)
app.include_router(actions.router)
app.include_router(solar.router)
app.include_router(wave.router)
app.include_router(wind.router)
app.include_router(voice.router)
app.include_router(agent.router)


@app.get("/api/health", tags=["health"])
async def health() -> dict:
    """Liveness + DB connectivity probe."""
    try:
        row = await db.fetchrow("SELECT 1 AS ok")
        db_ok = row is not None and row["ok"] == 1
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "database": db_ok}
