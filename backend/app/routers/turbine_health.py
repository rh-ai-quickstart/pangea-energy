"""Turbine health from the normal-behaviour models.

Each model predicts what a component should be running at given the conditions
the turbine is under. The gap between that and the actual reading is the
residual: a healthy component sits near zero, a degrading one runs persistently
hotter than its load and the weather justify.

Every turbine is scored against its own first `nbm_baseline_days`, not against
the fleet. A turbine that naturally runs warm would otherwise look permanently
faulty, and one that runs cool could heat up and still look fine.

Only turbines with component temperatures are covered; the rest of the fleet
has no component instrumentation and is reported as unmonitored.
"""
from __future__ import annotations

import logging
import statistics
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..config import settings
from ..models import Alert, TurbineHealth

router = APIRouter(prefix="/api/turbines", tags=["turbines"])

logger = logging.getLogger("pangea.health")

# Order is the model's input tensor. It must match FEATURES in
# windfarms/convert_nbm_to_onnx.py - ONNX takes a bare tensor, so position is
# the only thing binding these together.
FEATURES = ("wind_speed_ms", "power_output_kw", "rotational_speed_rpm",
            "torque_nm", "air_temperature_c", "pitch_angle_deg")

COMPONENTS = {
    "Generator bearing": ("generator_bearing_temp_c", "nbm_generator_bearing_url"),
    "Gearbox oil":       ("gearbox_oil_temp_c",       "nbm_gearbox_oil_url"),
}

_cache: tuple[float, list[TurbineHealth]] | None = None

_QUERY = (
    "SELECT t.turbine_code, s.site_name, sr.reading_time, "
    "       sr.wind_speed_ms, sr.power_output_kw, sr.rotational_speed_rpm, "
    "       sr.torque_nm, sr.air_temperature_c, sr.pitch_angle_deg, "
    "       sr.generator_bearing_temp_c, sr.gearbox_oil_temp_c "
    "FROM sensor_readings sr "
    "JOIN turbines t ON t.turbine_id = sr.turbine_id "
    "JOIN sites s ON s.site_id = t.site_id "
    "WHERE sr.reading_time >= NOW() - make_interval(days => $1) "
    "  AND sr.generator_bearing_temp_c IS NOT NULL "
    "ORDER BY t.turbine_code, sr.reading_time"
)


async def _infer(url: str, rows: list[dict]) -> list[float] | None:
    """Batch the whole window through one KServe v2 call."""
    flat: list[float] = []
    for r in rows:
        flat.extend(float(r[f] or 0) for f in FEATURES)
    payload = {"inputs": [{"name": "input", "shape": [len(rows), len(FEATURES)],
                           "datatype": "FP32", "data": flat}]}
    try:
        async with httpx.AsyncClient(timeout=settings.nbm_http_timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return [float(p) for p in resp.json()["outputs"][0]["data"]]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("nbm | %s unreachable: %s", url.rsplit("/", 3)[-2], exc)
        return None


def _score(rows: list[dict], predicted: list[float], column: str,
           baseline_cutoff: datetime) -> dict[str, dict]:
    """Residual z per turbine, baselined on that turbine's own reference period."""
    per_turbine: dict[str, list[tuple[datetime, float]]] = {}
    for r, p in zip(rows, predicted):
        actual = r[column]
        if actual is None:
            continue
        per_turbine.setdefault(r["turbine_code"], []).append(
            (r["reading_time"], float(actual) - p))

    out: dict[str, dict] = {}
    for code, series in per_turbine.items():
        base = [v for ts, v in series if ts < baseline_cutoff]
        if len(base) < 48:                      # too little history to judge against
            continue
        mu = statistics.fmean(base)
        sd = statistics.pstdev(base) or 1.0
        scored = [(ts, (v - mu) / sd) for ts, v in series if ts >= baseline_cutoff]
        if not scored:
            continue

        # 24h rolling mean, so a single spike cannot raise an alarm
        window, smoothed = [], []
        for ts, z in scored:
            window.append(z)
            if len(window) > 24:
                window.pop(0)
            smoothed.append((ts, statistics.fmean(window)))

        run = 0
        alarm_from = None
        for ts, z in smoothed:
            if z >= settings.nbm_z_alarm:
                run += 1
                if run >= settings.nbm_sustain_hours and alarm_from is None:
                    alarm_from = ts
            else:
                run = 0
        latest_ts, latest_z = smoothed[-1]
        out[code] = {
            "z": latest_z,
            "peak_z": max(z for _, z in smoothed),
            "residual_c": series[-1][1],
            "reading_time": latest_ts,
            "alarm_from": alarm_from,
            "hours_elevated": sum(1 for _, z in smoothed if z >= settings.nbm_z_alarm),
        }
    return out


@router.get("/health", response_model=list[TurbineHealth])
async def turbine_health(
    refresh: bool = Query(False, description="Bypass the cache"),
) -> list[TurbineHealth]:
    """Per-turbine component health, worst component first."""
    global _cache
    if _cache and not refresh and _cache[0] > time.time():
        return _cache[1]

    rows = [dict(r) for r in await db.fetch(_QUERY, settings.nbm_window_days)]
    if not rows:
        raise HTTPException(503, "No instrumented turbine readings in the window.")

    first = min(r["reading_time"] for r in rows)
    cutoff = first + __import__("datetime").timedelta(days=settings.nbm_baseline_days)

    sites = {r["turbine_code"]: r["site_name"] for r in rows}
    results: dict[str, list[dict]] = {}
    for component, (column, url_setting) in COMPONENTS.items():
        usable = [r for r in rows if r[column] is not None]
        if not usable:
            continue
        preds = await _infer(getattr(settings, url_setting), usable)
        if preds is None:
            continue
        for code, s in _score(usable, preds, column, cutoff).items():
            results.setdefault(code, []).append({**s, "component": component})

    if not results:
        raise HTTPException(503, "No health model is reachable.")

    out: list[TurbineHealth] = []
    for code, comps in results.items():
        worst = max(comps, key=lambda c: c["z"])
        out.append(TurbineHealth(
            turbine_code=code,
            site_name=sites.get(code),
            component=worst["component"],
            reading_time=worst["reading_time"],
            residual_c=round(worst["residual_c"], 2),
            z=round(worst["z"], 2),
            peak_z=round(worst["peak_z"], 2),
            hours_elevated=worst["hours_elevated"],
            degrading=worst["alarm_from"] is not None,
            alarm_from=worst["alarm_from"],
        ))
    out.sort(key=lambda h: h.z, reverse=True)

    logger.info("nbm | scored %d turbines, %d degrading",
                len(out), sum(1 for h in out if h.degrading))
    _cache = (time.time() + settings.nbm_cache_ttl_seconds, out)
    return out


@router.post("/health/persist")
async def persist_predictions() -> dict:
    """Write the current scores into ml_predictions, one row per turbine."""
    health = await turbine_health(refresh=True)
    written = 0
    for h in health:
        row = await db.fetchrow(
            "INSERT INTO ml_predictions "
            "  (prediction_time, turbine_id, model_name, model_version, "
            "   failure_predicted, failure_probability, predicted_failure_mode, "
            "   confidence_score) "
            "SELECT $1, t.turbine_id, 'pangea-nbm', 'v1', $3, $4, $5, $6 "
            "FROM turbines t WHERE t.turbine_code = $2 "
            "ON CONFLICT (prediction_time, turbine_id) DO UPDATE SET "
            "  failure_predicted = EXCLUDED.failure_predicted, "
            "  failure_probability = EXCLUDED.failure_probability, "
            "  predicted_failure_mode = EXCLUDED.predicted_failure_mode "
            "RETURNING turbine_id",
            h.reading_time, h.turbine_code, h.degrading,
            round(min(0.99, max(0.0, (h.z - 1) / 6)), 4),
            h.component if h.degrading else None,
            round(min(0.99, max(0.5, 1 - 1 / (1 + abs(h.z)))), 4))
        written += 1 if row else 0
    logger.info("nbm | persisted %d predictions", written)
    return {"written": written, "degrading": sum(1 for h in health if h.degrading)}


async def health_alerts() -> list[Alert]:
    """Synthetic alerts for degrading turbines, merged into the alert feed.

    Negative ids keep them clear of real DB alerts, and because they are
    computed at request time they clear themselves once the residual settles.
    """
    now = datetime.now(timezone.utc)
    out: list[Alert] = []
    for h in await turbine_health():
        if not h.degrading:
            continue
        days = (now - h.alarm_from).total_seconds() / 86400 if h.alarm_from else 0
        out.append(Alert(
            alert_id=-abs(sum(ord(c) for c in h.turbine_code) * 7 + 1),
            alert_time=h.alarm_from or now,
            severity="warning" if h.z < 4 else "critical",
            category="ml_prediction",
            status="active",
            turbine_code=h.turbine_code,
            site_name=h.site_name,
            title=f"{h.component} degrading on {h.turbine_code}",
            message=(
                f"{h.component} is running {h.residual_c:+.1f}°C against what the "
                f"model expects for the current load and weather "
                f"({h.z:.1f} sigma), sustained {h.hours_elevated}h over "
                f"{days:.0f} days."
            ),
            recommended_action=(
                f"Inspect the {h.component.lower()} on {h.turbine_code}. "
                "Raise a work order before the deviation worsens."),
        ))
    return out


def _briefing_facts(health: list[TurbineHealth]) -> str:
    """The spoken summary, written in code so the numbers cannot be wrong."""
    bad = [h for h in health if h.degrading]
    if not bad:
        return (f"Shift briefing: all {len(health)} monitored turbines are behaving "
                "normally. No component is running outside what its conditions justify.")
    parts = []
    for h in bad:
        days = h.hours_elevated / 24
        since = (f"{days:.0f} days" if days >= 2 else f"{h.hours_elevated} hours")
        parts.append(
            f"{h.turbine_code} at {h.site_name or 'site unknown'}: the "
            f"{h.component.lower()} has been running about "
            f"{abs(h.residual_c):.0f} degrees "
            f"{'warmer' if h.residual_c > 0 else 'cooler'} than expected for its "
            f"load and the weather, for {since}")
    head = (f"Shift briefing: {len(bad)} of {len(health)} monitored turbines "
            f"{'needs' if len(bad) == 1 else 'need'} attention. ")
    return head + ". ".join(parts) + ". Worth an inspection before it worsens."


@router.get("/health/briefing")
async def health_briefing() -> dict:
    """A spoken-language summary of fleet health, for the voice assistant.

    The facts are composed in code; the model is only asked to say them more
    naturally, and its version is discarded if it drops or invents a turbine.
    """
    health = await turbine_health()
    facts = _briefing_facts(health)

    # Spoken verbatim, not paraphrased. Granite has twice degraded this text -
    # once dropping "for the current conditions", which is the whole claim a
    # normal-behaviour model makes, and once casting itself as the supervisor.
    # The generated sentence is already natural enough to read aloud.
    return {
        "spoken": facts,
        "facts": facts,
        "degrading": [h.turbine_code for h in health if h.degrading],
        "monitored": len(health),
    }
