"""Solar generation forecast + live under-performance detection.

For each solar site we pull live weather (Open-Meteo), turn it into the 7
features the ``solar-output`` ONNX model expects, and call the KServe v2
endpoint to get expected output. The plant's *actual* output is a simulated
SCADA stand-in derived from the prediction (there is no real inverter feed in
the demo); the gap between the two is the predictive-maintenance signal.

Everything degrades gracefully: no cluster egress -> clear-sky simulation for
weather; model unreachable -> a simple physics proxy for the prediction.
"""
from __future__ import annotations

import math
import random
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..config import settings
from ..models import (
    SolarDailyForecast,
    SolarForecastDetail,
    SolarForecastPoint,
    SolarPrediction,
)

router = APIRouter(prefix="/api/solar", tags=["solar"])

# Nominal Operating Cell Temperature model constant: panel runs hotter than
# ambient in sun. panel_temp ~= ambient + (NOCT-20)/800 * irradiance.
_NOCT = 45.0
_FORECAST_HOURS = 24
_DETAIL_DAYS = 5                      # horizon for the per-site click-through forecast
# Simulated plant health. One site carries a fault so the demo shows a real
# under-performance alert; the rest hover near nominal. Swap for a real SCADA
# feed later. Keyed by site_code.
_DEGRADED_SITES = {"TRN": 0.82}
_HEALTHY = 1.00
# Below this irradiance there is effectively no sun, so generation is zero
# (keeps W/m^2 = 0 consistent with 0 output instead of model noise at night).
_MIN_IRRADIANCE = 5.0
# A site counts as "underperforming" when actual is this far below predicted.
_UNDERPERF_PCT = 8.0

# Per-site cache: site_code -> (expiry_epoch, SolarPrediction).
_cache: dict[str, tuple[float, SolarPrediction]] = {}


# Feature engineering
def _features(irradiance: float, ambient: float, cloud: float, hour: int) -> list[float]:
    """Build the 7 model features (order matters)."""
    panel = ambient + (_NOCT - 20.0) / 800.0 * irradiance
    temp_delta = panel - ambient
    hour_sin = math.sin(2 * math.pi * hour / 24)
    hour_cos = math.cos(2 * math.pi * hour / 24)
    return [irradiance, ambient, panel, cloud, temp_delta, hour_sin, hour_cos]


def _health_factor(site_code: str, hour_seed: str) -> float:
    # Losses only: real generation sits at or just below the weather-based
    # prediction (soiling, inverter losses), never above it. So the factor is
    # always <= 1.0 and residual = predicted - actual is always >= 0.
    base = _DEGRADED_SITES.get(site_code, _HEALTHY)
    rng = random.Random(f"{site_code}:{hour_seed}")
    return max(0.0, min(1.0, base - rng.uniform(0.0, 0.02)))


# Weather: Open-Meteo primary, clear-sky simulation fallback
async def _fetch_open_meteo(lat: float, lon: float, hours: int = _FORECAST_HOURS) -> list[dict]:
    """Return up to `hours` hourly records starting at the current hour.

    Each record: {time: datetime, irradiance, ambient, cloud, hour}.
    Raises on any failure so the caller can fall back to simulation.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        # Instantaneous radiation, not the preceding-hour mean: the mean of the
        # sunrise hour reads ~0 well after the sun is actually up (the sun only
        # cleared the horizon at the end of that hour), which made morning plants
        # look idle. The instantaneous value reflects the sun's real position.
        "hourly": "shortwave_radiation_instant,temperature_2m,cloud_cover",
        # Enough days to cover the current-hour offset + requested window.
        "forecast_days": min(16, hours // 24 + 2),
        "timezone": "auto",
    }
    async with httpx.AsyncClient(timeout=settings.solar_http_timeout) as client:
        resp = await client.get(settings.open_meteo_url, params=params)
        resp.raise_for_status()
        data = resp.json()

    hourly = data["hourly"]
    times = [datetime.fromisoformat(t) for t in hourly["time"]]  # naive local
    irr = hourly["shortwave_radiation_instant"]
    amb = hourly["temperature_2m"]
    cld = hourly["cloud_cover"]
    offset = data.get("utc_offset_seconds", 0)
    now_local = (datetime.now(timezone.utc) + timedelta(seconds=offset)).replace(tzinfo=None)

    def val(seq, i: int) -> float:
        return float(seq[i] or 0.0)

    # Index of the current hour bucket, plus the fraction of the hour elapsed so
    # the live reading is interpolated to *now* instead of snapping to the top of
    # the hour (e.g. at 08:48 we're 80% of the way to the 09:00 sample).
    start = 0
    for i, t in enumerate(times):
        if t <= now_local:
            start = i
    frac = 0.0
    if start + 1 < len(times):
        span = (times[start + 1] - times[start]).total_seconds() or 3600.0
        frac = max(0.0, min(1.0, (now_local - times[start]).total_seconds() / span))

    def lerp(seq, i: int, f: float) -> float:
        a = val(seq, i)
        b = val(seq, i + 1) if i + 1 < len(seq) else a
        return a + (b - a) * f

    out: list[dict] = []
    for i in range(start, min(start + hours, len(times))):
        f = frac if i == start else 0.0
        out.append(
            {
                "time": times[i],
                "irradiance": max(0.0, lerp(irr, i, f)),
                "ambient": lerp(amb, i, f),
                "cloud": lerp(cld, i, f),
                "hour": times[i].hour,
            }
        )
    if not out:
        raise ValueError("Open-Meteo returned no usable hours")
    return out


def _clear_sky(lat: float, lon: float, hours: int = _FORECAST_HOURS) -> list[dict]:
    """Physically-plausible clear-sky fallback when Open-Meteo is unreachable.

    Works purely from solar geometry in UTC (no timezone lookup needed): the sun
    peaks at local solar noon, which occurs at 12:00 - lon/15 in UTC. This is what
    makes mornings correct — e.g. Seville (lon ~-6) has solar noon ~12:24 UTC, so
    at 07:00 UTC (09:00 local) the sun is already well up.
    """
    now_utc = datetime.now(timezone.utc)
    # Latitude derate (weakest near the poles, strongest near the tropics).
    lat_damp = max(0.25, math.cos(math.radians(abs(lat) - 23.0)))
    solar_noon_utc = 12.0 - lon / 15.0        # UTC hour of local solar noon
    half_day = 6.5                            # ~13 h of daylight
    civil_offset = 2                          # CEST; all demo sites are European
    out: list[dict] = []
    for h in range(hours):
        t_utc = now_utc + timedelta(hours=h)
        t_hours = t_utc.hour + t_utc.minute / 60.0
        delta = (t_hours - solar_noon_utc + 12) % 24 - 12   # hours from solar noon, [-12,12]
        if abs(delta) < half_day:
            irradiance = max(0.0, math.cos(delta / half_day * math.pi / 2)) * 1000.0 * lat_damp
        else:
            irradiance = 0.0                  # night
        ambient = 16.0 + 9.0 * math.cos(math.radians((delta - 2.0) * 15.0))  # warmest mid-afternoon
        local = t_utc + timedelta(hours=civil_offset)
        out.append(
            {
                "time": local.replace(tzinfo=None),
                "irradiance": irradiance,
                "ambient": ambient,
                "cloud": 10.0,
                "hour": local.hour,
            }
        )
    return out


# Model inference (batched) with a physics proxy fallback
async def _infer(rows: list[dict]) -> list[float]:
    """Return per-unit predicted kW for each hour via the KServe v2 endpoint.

    Falls back to a simple irradiance/cloud physics proxy if the model is
    unreachable, so the dashboard still shows a plausible curve.
    """
    flat: list[float] = []
    for r in rows:
        flat.extend(_features(r["irradiance"], r["ambient"], r["cloud"], r["hour"]))
    payload = {
        "inputs": [
            {"name": "input", "shape": [len(rows), 7], "datatype": "FP32", "data": flat}
        ]
    }
    try:
        async with httpx.AsyncClient(timeout=settings.solar_http_timeout) as client:
            resp = await client.post(settings.solar_model_url, json=payload)
            resp.raise_for_status()
            preds = resp.json()["outputs"][0]["data"]
        return [max(0.0, float(p)) for p in preds]
    except Exception:
        peak = settings.solar_model_peak_kw
        return [
            max(0.0, peak * (r["irradiance"] / 1000.0) * (1 - r["cloud"] / 200.0))
            for r in rows
        ]


# Weather -> model -> capacity-scaled forecast (shared by both endpoints)
async def _scaled_forecast(site: dict, hours: int) -> tuple[list[dict], str, list[SolarForecastPoint]]:
    """Return (raw weather rows, weather source, capacity-scaled forecast points)."""
    lat = float(site["latitude"])
    lon = float(site["longitude"])
    capacity = int(site["capacity_kw"] or 0)

    try:
        rows = await _fetch_open_meteo(lat, lon, hours)
        source = "open-meteo"
    except Exception:
        rows = _clear_sky(lat, lon, hours)
        source = "clear-sky-sim"

    per_unit = await _infer(rows)

    # Scale the model's per-unit output up to the site's rated capacity.
    scale = (capacity / settings.solar_model_peak_kw) if capacity else 1.0
    forecast: list[SolarForecastPoint] = []
    for r, p in zip(rows, per_unit):
        if r["irradiance"] < _MIN_IRRADIANCE:
            kw = 0.0                       # no sun -> no generation (night/pre-dawn)
        else:
            kw = min(p * scale, float(capacity)) if capacity else p * scale
        forecast.append(SolarForecastPoint(time=r["time"], predicted_kw=round(max(0.0, kw), 1)))
    return rows, source, forecast


# Per-site prediction (cached)
async def _predict_site(site: dict) -> SolarPrediction:
    code = site["site_code"]
    cached = _cache.get(code)
    if cached and cached[0] > time.time():
        return cached[1]

    capacity = int(site["capacity_kw"] or 0)
    rows, source, forecast = await _scaled_forecast(site, _FORECAST_HOURS)
    now = rows[0]
    predicted_now = forecast[0].predicted_kw

    # Simulated live SCADA reading around the prediction.
    hour_seed = now["time"].strftime("%Y-%m-%dT%H")
    actual_now = round(predicted_now * _health_factor(code, hour_seed), 1)

    residual = round(predicted_now - actual_now, 1)
    residual_pct = round((residual / predicted_now * 100) if predicted_now > 0.5 else 0.0, 1)

    if predicted_now < max(1.0, 0.005 * capacity):
        status = "offline"          # night / no meaningful generation
    elif residual_pct >= _UNDERPERF_PCT:
        status = "underperforming"
    else:
        status = "ok"

    result = SolarPrediction(
        site_code=code,
        site_name=site["site_name"],
        nearest_city=site["nearest_city"],
        latitude=float(site["latitude"]),
        longitude=float(site["longitude"]),
        capacity_kw=capacity or None,
        predicted_now_kw=predicted_now,
        actual_now_kw=actual_now,
        residual_kw=residual,
        residual_pct=residual_pct,
        status=status,
        irradiance_wm2=round(now["irradiance"], 1),
        ambient_temp_c=round(now["ambient"], 1),
        cloud_cover_pct=round(now["cloud"], 1),
        weather_source=source,
        forecast_24h=forecast,
    )
    _cache[code] = (time.time() + settings.solar_cache_ttl_seconds, result)
    return result


# Routes
async def _solar_sites(site_code: str | None = None) -> list[dict]:
    query = (
        "SELECT site_code, site_name, latitude, longitude, capacity_kw, nearest_city "
        "FROM sites WHERE energy_type = 'solar' AND is_active "
        "  AND latitude IS NOT NULL AND longitude IS NOT NULL"
    )
    args: list = []
    if site_code:
        args.append(site_code)
        query += f" AND site_code = ${len(args)}"
    query += " ORDER BY site_name"
    return [dict(r) for r in await db.fetch(query, *args)]


@router.get("/predict", response_model=list[SolarPrediction])
async def predict_all(
    site_code: str | None = Query(None, description="Limit to one solar site code"),
) -> list[SolarPrediction]:
    """Live predicted-vs-actual output for solar sites, plus a 24h forecast."""
    sites = await _solar_sites(site_code)
    if site_code and not sites:
        raise HTTPException(status_code=404, detail=f"No solar site {site_code}")
    return [await _predict_site(s) for s in sites]


# Separate cache for the heavier multi-day forecast (fetched on card click).
_detail_cache: dict[str, tuple[float, SolarForecastDetail]] = {}


@router.get("/forecast/{site_code}", response_model=SolarForecastDetail)
async def forecast_detail(site_code: str) -> SolarForecastDetail:
    """Predicted power output for the next ~5 days (from the Open-Meteo forecast)."""
    cached = _detail_cache.get(site_code)
    if cached and cached[0] > time.time():
        return cached[1]

    sites = await _solar_sites(site_code)
    if not sites:
        raise HTTPException(status_code=404, detail=f"No solar site {site_code}")
    site = sites[0]

    _, source, hourly = await _scaled_forecast(site, _DETAIL_DAYS * 24)

    # Roll the hourly curve up into per-day peak + energy (kW over 1h == kWh).
    by_day: dict[str, list[float]] = {}
    for p in hourly:
        by_day.setdefault(p.time.date().isoformat(), []).append(p.predicted_kw)
    daily = [
        SolarDailyForecast(
            date=day,
            peak_kw=round(max(kws), 1),
            energy_kwh=round(sum(kws), 1),
        )
        for day, kws in sorted(by_day.items())
    ]

    result = SolarForecastDetail(
        site_code=site["site_code"],
        site_name=site["site_name"],
        nearest_city=site["nearest_city"],
        capacity_kw=int(site["capacity_kw"] or 0) or None,
        weather_source=source,
        hourly=hourly,
        daily=daily,
    )
    _detail_cache[site_code] = (time.time() + settings.solar_cache_ttl_seconds, result)
    return result
