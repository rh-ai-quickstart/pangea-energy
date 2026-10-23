"""Wind-farm live status + high-wind cut-out detection. Falls back to the
site's seeded average wind speed if Open-Meteo is unreachable."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter

from .. import db
from ..config import settings
from ..models import Alert, WindStatus

router = APIRouter(prefix="/api/wind", tags=["wind"])

_cache: tuple[float, list[WindStatus]] | None = None


async def _fetch_wind_now(lat: float, lon: float) -> tuple[float, float | None]:
    """Return (hub-height wind m/s, gust m/s) for the current hour; raises on failure."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "wind_speed_100m,wind_gusts_10m",
        "wind_speed_unit": "ms",
        "forecast_days": 1,
        "timezone": "auto",
    }
    async with httpx.AsyncClient(timeout=settings.wind_http_timeout) as client:
        resp = await client.get(settings.open_meteo_url, params=params)
        resp.raise_for_status()
        data = resp.json()

    hourly = data["hourly"]
    times = [datetime.fromisoformat(t) for t in hourly["time"]]
    speed = hourly["wind_speed_100m"]
    gust = hourly["wind_gusts_10m"]
    offset = data.get("utc_offset_seconds", 0)
    now_local = (datetime.now(timezone.utc) + timedelta(seconds=offset)).replace(tzinfo=None)

    idx = 0
    for i, t in enumerate(times):
        if t <= now_local:
            idx = i
    wind = float(speed[idx] or 0.0)
    g = gust[idx]
    return wind, (float(g) if g is not None else None)


async def wind_statuses() -> list[WindStatus]:
    """Live cut-out status for every active wind site (cached)."""
    global _cache
    if _cache and _cache[0] > time.time():
        return _cache[1]

    sites = await db.fetch(
        "SELECT site_code, site_name, nearest_city, latitude, longitude, "
        "       turbine_count, avg_wind_speed_ms "
        "FROM sites "
        "WHERE energy_type = 'wind' AND is_active "
        "  AND latitude IS NOT NULL AND longitude IS NOT NULL "
        "ORDER BY site_name"
    )

    cutout = settings.wind_cutout_ms
    out: list[WindStatus] = []
    for s in sites:
        try:
            wind, gust = await _fetch_wind_now(float(s["latitude"]), float(s["longitude"]))
            source = "open-meteo"
        except Exception:
            wind = float(s["avg_wind_speed_ms"] or 0.0)
            gust = None
            source = "site-average"

        shutdown = wind >= cutout
        status = "shutdown" if shutdown else ("calm" if wind < 3.0 else "operating")
        out.append(
            WindStatus(
                site_code=s["site_code"],
                site_name=s["site_name"],
                nearest_city=s["nearest_city"],
                latitude=float(s["latitude"]),
                longitude=float(s["longitude"]),
                turbine_count=int(s["turbine_count"] or 0),
                wind_speed_ms=round(wind, 1),
                gust_ms=round(gust, 1) if gust is not None else None,
                cutout_ms=cutout,
                shutdown=shutdown,
                status=status,
                weather_source=source,
            )
        )

    _cache = (time.time() + settings.wind_cache_ttl_seconds, out)
    return out


async def high_wind_alerts() -> list[Alert]:
    """Synthetic alerts for sites in high-wind shutdown; negative IDs avoid
    colliding with real DB alert IDs."""
    now = datetime.now(timezone.utc)
    alerts: list[Alert] = []
    for w in await wind_statuses():
        if not w.shutdown:
            continue
        gust = f", gusting {w.gust_ms:.0f} m/s" if w.gust_ms is not None else ""
        alerts.append(
            Alert(
                alert_id=-abs(sum(ord(c) for c in w.site_code)),
                alert_time=now,
                severity="warning",
                category="weather",
                status="active",
                turbine_code=None,
                site_name=w.site_name,
                title="High-wind shutdown",
                message=(
                    f"Wind {w.wind_speed_ms:.0f} m/s{gust} exceeds the "
                    f"{w.cutout_ms:.0f} m/s cut-out — turbines shut down for protection."
                ),
                recommended_action="No action: turbines resume automatically when wind drops below cut-out.",
            )
        )
    return alerts


@router.get("/status", response_model=list[WindStatus])
async def status() -> list[WindStatus]:
    """Live hub-height wind and cut-out status per wind site."""
    return await wind_statuses()
