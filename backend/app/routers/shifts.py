"""Shift definitions: the clock hours behind each shift_type, served from the
database so the rota functions, the API and the dashboard share one source.
"""
from fastapi import APIRouter

from .. import db
from ..models import ShiftDefinition

router = APIRouter(prefix="/api/shifts", tags=["shifts"])


async def shift_definitions(conn=None) -> dict[str, ShiftDefinition]:
    rows = await db.fetch(
        "SELECT shift::text AS shift, start_hour, end_hour, booked_hours, description "
        "FROM shift_definitions ORDER BY shift", conn=conn)
    return {r["shift"]: ShiftDefinition(**dict(r)) for r in rows}


@router.get("", response_model=list[ShiftDefinition])
async def list_shifts() -> list[ShiftDefinition]:
    """Clock hours per shift. `night` wraps midnight; `on_call`/`off` have none."""
    return list((await shift_definitions()).values())
