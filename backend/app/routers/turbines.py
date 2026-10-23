"""Turbine list, detail, and recent sensor readings."""
from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..models import SensorReading, Turbine

router = APIRouter(prefix="/api/turbines", tags=["turbines"])

_TURBINE_SELECT = (
    "SELECT t.turbine_code, s.site_name, t.model::text AS model, "
    "       t.status::text AS status, t.current_power_output_kw, t.rated_power_kw "
    "FROM turbines t JOIN sites s ON s.site_id = t.site_id"
)


@router.get("", response_model=list[Turbine])
async def list_turbines(
    status: str | None = Query(None, description="Filter by status"),
    site_code: str | None = Query(None, description="Filter by site code"),
) -> list[Turbine]:
    where, args = [], []
    if status:
        args.append(status)
        where.append(f"t.status = ${len(args)}::turbine_status")
    if site_code:
        args.append(site_code)
        where.append(f"s.site_code = ${len(args)}")
    query = _TURBINE_SELECT
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY t.turbine_code"
    rows = await db.fetch(query, *args)
    return [Turbine(**dict(r)) for r in rows]


@router.get("/{turbine_code}", response_model=Turbine)
async def get_turbine(turbine_code: str) -> Turbine:
    row = await db.fetchrow(
        _TURBINE_SELECT + " WHERE t.turbine_code = $1", turbine_code
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Turbine {turbine_code} not found")
    return Turbine(**dict(row))


@router.get("/{turbine_code}/readings", response_model=list[SensorReading])
async def get_readings(
    turbine_code: str,
    hours: int = Query(24, ge=1, le=168, description="Look-back window in hours"),
) -> list[SensorReading]:
    rows = await db.fetch(
        "SELECT sr.reading_time, sr.power_output_kw, sr.wind_speed_ms, "
        "       sr.rotational_speed_rpm, sr.torque_nm, sr.air_temperature_c, "
        "       sr.process_temperature_c, sr.vibration_x_mms, sr.anomaly_score "
        "FROM sensor_readings sr "
        "JOIN turbines t ON t.turbine_id = sr.turbine_id "
        "WHERE t.turbine_code = $1 "
        "  AND sr.reading_time >= NOW() - ($2 || ' hours')::interval "
        "ORDER BY sr.reading_time",
        turbine_code,
        str(hours),
    )
    return [SensorReading(**dict(r)) for r in rows]
