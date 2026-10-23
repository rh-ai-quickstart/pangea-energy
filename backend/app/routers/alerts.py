"""Alert feed for the dashboard."""
from fastapi import APIRouter, Query

from .. import db
from ..models import Alert
from .turbine_health import health_alerts
from .wind import high_wind_alerts

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("", response_model=list[Alert])
async def list_alerts(
    status: str = Query("active", description="Filter by alert status"),
    limit: int = Query(50, ge=1, le=200),
) -> list[Alert]:
    # Live high-wind shutdowns are computed from weather, not stored in the DB;
    # surface them at the top of the active feed alongside the stored alerts.
    live: list[Alert] = []
    if status == "active":
        for source in (high_wind_alerts, health_alerts):
            try:
                live += await source()
            except Exception:
                pass

    rows = await db.fetch(
        "SELECT a.alert_id, a.alert_time, a.severity::text AS severity, "
        "       a.category::text AS category, a.status::text AS status, "
        "       t.turbine_code, s.site_name, a.title, a.message, a.recommended_action "
        "FROM alerts a "
        "LEFT JOIN turbines t ON t.turbine_id = a.turbine_id "
        "LEFT JOIN sites s ON s.site_id = a.site_id "
        "WHERE a.status = $1::alert_status "
        "ORDER BY a.alert_time DESC "
        "LIMIT $2",
        status,
        limit,
    )
    return live + [Alert(**dict(r)) for r in rows]
