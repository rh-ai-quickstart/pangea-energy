"""Fleet-level and site endpoints."""
from fastapi import APIRouter

from .. import db
from ..models import FleetStatus, Site, StatusCount

router = APIRouter(prefix="/api", tags=["fleet"])


@router.get("/fleet/status", response_model=FleetStatus)
async def fleet_status() -> FleetStatus:
    """Fleet health at a glance: turbine counts by status and live output."""
    rows = await db.fetch(
        "SELECT status::text AS status, count(*) AS count "
        "FROM turbines GROUP BY status ORDER BY status"
    )
    totals = await db.fetchrow(
        "SELECT "
        "  count(*) AS total, "
        "  COALESCE(sum(current_power_output_kw), 0) AS live_kw, "
        "  COALESCE(sum(rated_power_kw), 0) AS rated_kw "
        "FROM turbines"
    )
    return FleetStatus(
        total_turbines=totals["total"],
        by_status=[StatusCount(status=r["status"], count=r["count"]) for r in rows],
        live_output_mw=round(float(totals["live_kw"]) / 1000, 2),
        rated_capacity_mw=round(float(totals["rated_kw"]) / 1000, 2),
    )


@router.get("/sites", response_model=list[Site])
async def list_sites() -> list[Site]:
    """Sites with coordinates and live per-site health, for the map."""
    rows = await db.fetch(
        "SELECT s.site_code, s.site_name, s.terrain::text AS terrain, "
        "       s.energy_type::text AS energy_type, "
        "       s.turbine_count, s.capacity_kw, s.avg_wind_speed_ms, "
        "       s.latitude, s.longitude, s.nearest_city, s.country, "
        "       count(t.turbine_id) FILTER (WHERE t.status = 'operational') AS operational, "
        "       count(t.turbine_id) FILTER (WHERE t.status = 'maintenance') AS maintenance, "
        "       count(t.turbine_id) FILTER (WHERE t.status = 'offline') AS offline, "
        "       round(COALESCE(sum(t.current_power_output_kw), 0) / 1000, 1) AS live_output_mw "
        "FROM sites s LEFT JOIN turbines t ON t.site_id = s.site_id "
        "WHERE s.is_active "
        "GROUP BY s.site_id "
        "ORDER BY s.site_name"
    )
    return [Site(**dict(r)) for r in rows]
