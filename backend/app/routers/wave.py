"""Wave-farm generation forecast + live under-performance detection.

For each wave farm we pull live sea state (Open-Meteo Marine: significant wave
height Hs and wave period Te), turn it into the incident wave-energy flux, and
scale each farm's rated capacity by that flux to estimate output. The farm's
*actual* output is a simulated SCADA stand-in derived from the prediction (there
is no real converter feed in the demo); the gap between the two is the
predictive-maintenance signal.

The farm *design* — buoy count, rated capacity and array q-factor — is derived
from the UCI "Wave Energy Converters" (WEC) optimisation dataset
(`wavefarms/WEC`): the best 100-buoy layouts land near 7.4 MW at each site's
reference sea state, with an array interaction factor qW ~= 0.7. The plants
themselves are deployed at North Atlantic / Norwegian Sea sites (Orkney, Runde).

A served ONNX layout->power model on OpenShift AI is a planned follow-up; today
the prediction is the physics model below.

Everything degrades gracefully: no cluster egress -> calm-sea simulation.
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
    WaveDailyForecast,
    WaveForecastDetail,
    WaveForecastPoint,
    WavePrediction,
)

router = APIRouter(prefix="/api/wave", tags=["wave"])

_FORECAST_HOURS = 24
_DETAIL_DAYS = 5                      # horizon for the per-site click-through forecast

# Per-site sea-state design point: the incident wave-energy flux (kW per metre of
# wave crest) at which the farm reaches rated output. Above this the converters
# rate out (survival mode) and output is held at capacity. Tuned so autumn North
# Atlantic seas give a realistic capacity factor and storms hit rated.
_RATED_FLUX = {
    "ORK": 70.0,   # Orkney (EMEC, Scotland)
    "RUN": 72.0,   # Runde (offshore Ålesund, Norway)
}
_DEFAULT_RATED_FLUX = 70.0

# Simulated plant health. One farm carries a fault so the demo shows a real
# under-performance alert; the rest hover near nominal. Keyed by site_code.
_DEGRADED_SITES = {"RUN": 0.86}
_HEALTHY = 1.00

# Below this flux the sea is effectively flat calm -> no generation (keeps the
# reported 0 kW consistent with the sea state instead of physics noise).
_MIN_FLUX = 1.5
# A farm counts as "underperforming" when actual is this far below predicted.
_UNDERPERF_PCT = 8.0

# Energy period Te is ~0.9 * peak period Tp for typical ocean spectra; Open-Meteo
# reports the (peak) wave_period.
_TE_FROM_TP = 0.9

# Per-site cache: site_code -> (expiry_epoch, WavePrediction).
_cache: dict[str, tuple[float, WavePrediction]] = {}


# Physics
def _wave_power_flux(hs: float, te: float) -> float:
    """Deep-water wave energy flux in kW per metre of wave crest.

    P = rho * g^2 / (64*pi) * Hs^2 * Te  ~=  0.49 * Hs^2 * Te   (kW/m)
    """
    return 0.49 * hs * hs * te


# Sea state: Open-Meteo Marine primary, calm-sea simulation fallback
async def _fetch_marine(lat: float, lon: float, hours: int = _FORECAST_HOURS) -> list[dict]:
    """Return up to `hours` hourly sea-state records starting at the current hour.

    Each record: {time, hs, te, flux, hour}. Raises on any failure so the caller
    can fall back to simulation.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "wave_height,wave_period",
        "forecast_days": min(16, hours // 24 + 2),
        "timezone": "auto",
    }
    async with httpx.AsyncClient(timeout=settings.solar_http_timeout) as client:
        resp = await client.get(settings.open_meteo_marine_url, params=params)
        resp.raise_for_status()
        data = resp.json()

    hourly = data["hourly"]
    times = [datetime.fromisoformat(t) for t in hourly["time"]]  # naive local
    hs_seq = hourly["wave_height"]
    tp_seq = hourly["wave_period"]
    offset = data.get("utc_offset_seconds", 0)
    now_local = (datetime.now(timezone.utc) + timedelta(seconds=offset)).replace(tzinfo=None)

    def val(seq, i: int) -> float:
        return float(seq[i] or 0.0)

    # Current hour bucket + fraction elapsed, so the live reading is interpolated
    # to *now* rather than snapping to the top of the hour.
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
        hs = max(0.0, lerp(hs_seq, i, f))
        te = max(0.0, lerp(tp_seq, i, f)) * _TE_FROM_TP
        out.append(
            {
                "time": times[i],
                "hs": hs,
                "te": te,
                "flux": _wave_power_flux(hs, te),
                "hour": times[i].hour,
            }
        )
    if not out:
        raise ValueError("Open-Meteo Marine returned no usable hours")
    return out


def _calm_sea(lat: float, lon: float, hours: int = _FORECAST_HOURS) -> list[dict]:
    """Deterministic moderate-sea fallback when the marine API is unreachable.

    A gentle swell with a slow diurnal-ish rise and fall so the dashboard still
    shows a plausible, non-flat curve. Latitude adds a little energy toward the
    stormier high-latitude coasts.
    """
    now_utc = datetime.now(timezone.utc)
    lat_boost = 1.0 + max(0.0, (abs(lat) - 45.0) / 50.0)   # rougher toward the poles
    out: list[dict] = []
    for h in range(hours):
        t = now_utc + timedelta(hours=h)
        phase = (t.hour + lon / 15.0) / 24.0 * 2 * math.pi
        hs = (1.6 + 0.6 * math.sin(phase)) * lat_boost      # ~1.0 - 2.6 m
        te = 8.0 + 1.0 * math.cos(phase)                    # ~7 - 9 s
        out.append(
            {
                "time": t.replace(tzinfo=None),
                "hs": hs,
                "te": te,
                "flux": _wave_power_flux(hs, te),
                "hour": t.hour,
            }
        )
    return out


# Sea state -> capacity-scaled forecast (shared by both endpoints)
async def _scaled_forecast(site: dict, hours: int) -> tuple[list[dict], str, list[WaveForecastPoint]]:
    """Return (raw sea-state rows, source, capacity-scaled forecast points)."""
    lat = float(site["latitude"])
    lon = float(site["longitude"])
    capacity = int(site["capacity_kw"] or 0)
    rated_flux = _RATED_FLUX.get(site["site_code"], _DEFAULT_RATED_FLUX)

    try:
        rows = await _fetch_marine(lat, lon, hours)
        source = "open-meteo-marine"
    except Exception:
        rows = _calm_sea(lat, lon, hours)
        source = "calm-sea-sim"

    forecast: list[WaveForecastPoint] = []
    for r in rows:
        if r["flux"] < _MIN_FLUX or not capacity:
            kw = 0.0                       # flat calm -> no generation
        else:
            kw = capacity * min(1.0, r["flux"] / rated_flux)
        forecast.append(WaveForecastPoint(time=r["time"], predicted_kw=round(max(0.0, kw), 1)))
    return rows, source, forecast


def _health_factor(site_code: str, hour_seed: str) -> float:
    # Losses only: real generation sits at or just below the modelled output
    # (mooring drag, PTO losses), never above it, so residual is always >= 0.
    base = _DEGRADED_SITES.get(site_code, _HEALTHY)
    rng = random.Random(f"{site_code}:{hour_seed}")
    return max(0.0, min(1.0, base - rng.uniform(0.0, 0.02)))


# Per-site prediction (cached)
async def _predict_site(site: dict) -> WavePrediction:
    code = site["site_code"]
    cached = _cache.get(code)
    if cached and cached[0] > time.time():
        return cached[1]

    capacity = int(site["capacity_kw"] or 0)
    rows, source, forecast = await _scaled_forecast(site, _FORECAST_HOURS)
    now = rows[0]
    predicted_now = forecast[0].predicted_kw

    hour_seed = now["time"].strftime("%Y-%m-%dT%H")
    actual_now = round(predicted_now * _health_factor(code, hour_seed), 1)

    residual = round(predicted_now - actual_now, 1)
    residual_pct = round((residual / predicted_now * 100) if predicted_now > 0.5 else 0.0, 1)

    if predicted_now < max(1.0, 0.005 * capacity):
        status = "offline"          # flat calm / no meaningful generation
    elif residual_pct >= _UNDERPERF_PCT:
        status = "underperforming"
    else:
        status = "ok"

    result = WavePrediction(
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
        wave_height_m=round(now["hs"], 2),
        wave_period_s=round(now["te"], 1),
        wave_power_kw_per_m=round(now["flux"], 1),
        weather_source=source,
        forecast_24h=forecast,
    )
    _cache[code] = (time.time() + settings.wave_cache_ttl_seconds, result)
    return result


# Routes
async def _wave_sites(site_code: str | None = None) -> list[dict]:
    query = (
        "SELECT site_code, site_name, latitude, longitude, capacity_kw, nearest_city "
        "FROM sites WHERE energy_type = 'wave' AND is_active "
        "  AND latitude IS NOT NULL AND longitude IS NOT NULL"
    )
    args: list = []
    if site_code:
        args.append(site_code)
        query += f" AND site_code = ${len(args)}"
    query += " ORDER BY site_name"
    return [dict(r) for r in await db.fetch(query, *args)]


@router.get("/predict", response_model=list[WavePrediction])
async def predict_all(
    site_code: str | None = Query(None, description="Limit to one wave site code"),
) -> list[WavePrediction]:
    """Live predicted-vs-actual output for wave farms, plus a 24h forecast."""
    sites = await _wave_sites(site_code)
    if site_code and not sites:
        raise HTTPException(status_code=404, detail=f"No wave site {site_code}")
    return [await _predict_site(s) for s in sites]


# Separate cache for the heavier multi-day forecast (fetched on card click).
_detail_cache: dict[str, tuple[float, WaveForecastDetail]] = {}


@router.get("/forecast/{site_code}", response_model=WaveForecastDetail)
async def forecast_detail(site_code: str) -> WaveForecastDetail:
    """Predicted power output for the next ~5 days (from the marine forecast)."""
    cached = _detail_cache.get(site_code)
    if cached and cached[0] > time.time():
        return cached[1]

    sites = await _wave_sites(site_code)
    if not sites:
        raise HTTPException(status_code=404, detail=f"No wave site {site_code}")
    site = sites[0]

    _, source, hourly = await _scaled_forecast(site, _DETAIL_DAYS * 24)

    by_day: dict[str, list[float]] = {}
    for p in hourly:
        by_day.setdefault(p.time.date().isoformat(), []).append(p.predicted_kw)
    daily = [
        WaveDailyForecast(
            date=day,
            peak_kw=round(max(kws), 1),
            energy_kwh=round(sum(kws), 1),
        )
        for day, kws in sorted(by_day.items())
    ]

    result = WaveForecastDetail(
        site_code=site["site_code"],
        site_name=site["site_name"],
        nearest_city=site["nearest_city"],
        capacity_kw=int(site["capacity_kw"] or 0) or None,
        weather_source=source,
        hourly=hourly,
        daily=daily,
    )
    _detail_cache[site_code] = (time.time() + settings.wave_cache_ttl_seconds, result)
    return result
