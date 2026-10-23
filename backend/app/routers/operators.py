"""Operative availability: skills, weekly schedule, upcoming work, and load."""
import asyncio
import json
from datetime import date

from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..models import DayAvailability, OperatorAvailability, OperatorSchedule
from .shifts import shift_definitions

router = APIRouter(prefix="/api/operators", tags=["operators"])

_OPEN_STATUSES = ("scheduled", "assigned", "in_progress", "on_hold")
_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_QUERY = (
    "SELECT o.operative_id, o.employee_code, "
    "       o.first_name || ' ' || o.last_name AS full_name, "
    "       o.skill_level::text AS skill_level, "
    "       o.certifications::text[] AS certifications, o.specializations, "
    "       o.base_location, o.offshore_certified, o.max_travel_distance_km, "
    "       o.on_leave_until, "
    "       s.monday::text AS monday, s.tuesday::text AS tuesday, "
    "       s.wednesday::text AS wednesday, s.thursday::text AS thursday, "
    "       s.friday::text AS friday, s.saturday::text AS saturday, "
    "       s.sunday::text AS sunday, s.exception_dates, "
    "       (SELECT count(*) FROM work_orders w "
    "          WHERE w.assigned_operative_id = o.operative_id "
    "            AND w.status = ANY($1::work_order_status[])) AS open_assignments, "
    "       (SELECT w.title FROM work_orders w "
    "          WHERE w.assigned_operative_id = o.operative_id "
    "            AND w.status = ANY($1::work_order_status[]) "
    "            AND w.status = 'in_progress' "
    "          ORDER BY w.scheduled_start NULLS LAST LIMIT 1) AS current_task, "
    "       (SELECT json_agg(json_build_object("
    "                 'title', w.title, 'status', w.status::text, "
    "                 'scheduled_start', w.scheduled_start, "
    "                 'scheduled_end', w.scheduled_end) ORDER BY w.scheduled_start) "
    "          FROM work_orders w "
    "          WHERE w.assigned_operative_id = o.operative_id "
    "            AND w.status = ANY($1::work_order_status[]) "
    "            AND w.scheduled_start >= CURRENT_DATE "
    "            AND w.scheduled_start < CURRENT_DATE + make_interval(days => $2)) AS upcoming "
    "FROM maintenance_operatives o "
    "LEFT JOIN LATERAL ("
    "  SELECT * FROM operative_schedules "
    "  WHERE operative_id = o.operative_id AND effective_date <= CURRENT_DATE "
    "    AND (end_date IS NULL OR end_date >= CURRENT_DATE) "
    "  ORDER BY effective_date DESC LIMIT 1"
    ") s ON TRUE "
    "WHERE o.is_active = TRUE "
    "ORDER BY o.skill_level DESC, full_name"
)


async def available_operators(
    horizon_days: int = 14,
    include_on_leave: bool = False,
) -> list[OperatorAvailability]:
    rows = await db.fetch(_QUERY, list(_OPEN_STATUSES), horizon_days)
    now = date.today()
    today = _DAYS[now.weekday()]
    result = []
    for r in rows:
        if not include_on_leave and r["on_leave_until"] and r["on_leave_until"] >= now:
            continue
        weekly = {d: r[d] for d in _DAYS}
        raw = r["upcoming"]
        upcoming = json.loads(raw) if isinstance(raw, str) else (raw or [])
        result.append(OperatorAvailability(
            employee_code=r["employee_code"],
            name=r["full_name"],
            skill_level=r["skill_level"],
            certifications=list(r["certifications"] or []),
            specializations=list(r["specializations"] or []),
            base_location=r["base_location"],
            offshore_certified=r["offshore_certified"],
            max_travel_distance_km=r["max_travel_distance_km"],
            today_shift=weekly[today],
            weekly_schedule=weekly,
            exception_dates=list(r["exception_dates"] or []),
            on_leave_until=r["on_leave_until"],
            open_assignments=r["open_assignments"],
            current_task=r["current_task"],
            upcoming_assignments=upcoming,
        ))
    return result


@router.get("", response_model=list[OperatorAvailability])
async def list_operators(
    horizon_days: int = Query(14, ge=1, le=60),
    include_on_leave: bool = Query(True),
) -> list[OperatorAvailability]:
    """Full active roster by default; `include_on_leave=false` for who can work now."""
    return await available_operators(horizon_days, include_on_leave)


_AVAIL_QUERY = (
    "SELECT o.employee_code, o.first_name || ' ' || o.last_name AS full_name, "
    "       o.skill_level::text AS skill_level, o.base_location, "
    "       a.day, a.shift, a.on_leave, a.is_exception, "
    "       a.capacity_hours, a.booked_hours "
    "FROM operative_availability($1::date, $2) a "
    "JOIN maintenance_operatives o ON o.operative_id = a.operative_id "
    "WHERE ($3::text IS NULL OR o.employee_code = $3) "
    "ORDER BY o.skill_level DESC, full_name, a.day"
)

_AVAIL_JOBS = (
    "SELECT o.employee_code, wd.day, w.work_order_number, w.title, "
    "       w.status::text AS status, wd.hours, w.scheduled_start::date AS booked_for "
    "FROM work_orders w "
    "CROSS JOIN LATERAL work_order_days(w.work_order_id) wd "
    "JOIN maintenance_operatives o ON o.operative_id = w.assigned_operative_id "
    "WHERE w.status = ANY($4::work_order_status[]) "
    "  AND wd.day >= $1::date AND wd.day < $1::date + $2::int "
    "  AND ($3::text IS NULL OR o.employee_code = $3) "
    "ORDER BY wd.day, w.scheduled_start"
)


async def operative_availability(
    days: int = 14,
    code: str | None = None,
    start: date | None = None,
    conn=None,
) -> list[OperatorSchedule]:
    """Resolved day-by-day availability. The database owns the
    leave → exception → rota precedence; everything else reads it from here."""
    start = start or date.today()
    if conn is not None:
        rows = await db.fetch(_AVAIL_QUERY, start, days, code, conn=conn)
        jobs = await db.fetch(_AVAIL_JOBS, start, days, code,
                              list(_OPEN_STATUSES), conn=conn)
        shifts = await shift_definitions(conn=conn)
    else:
        rows, jobs, shifts = await asyncio.gather(
            db.fetch(_AVAIL_QUERY, start, days, code),
            db.fetch(_AVAIL_JOBS, start, days, code, list(_OPEN_STATUSES)),
            shift_definitions(),
        )

    # Drift: booked onto a day its operative no longer works, so shown on the
    # next day they do. Only the first day counts — later days of multi-day
    # work are meant to differ.
    first_day: dict[str, date] = {}
    for j in jobs:
        wo = j["work_order_number"]
        if wo not in first_day or j["day"] < first_day[wo]:
            first_day[wo] = j["day"]

    by_day: dict[tuple[str, date], list[dict]] = {}
    for j in jobs:
        booked_for = j["booked_for"]
        drifted = (booked_for >= start
                   and first_day[j["work_order_number"]] != booked_for)
        by_day.setdefault((j["employee_code"], j["day"]), []).append({
            "work_order_number": j["work_order_number"],
            "title": j["title"],
            "status": j["status"],
            "hours": float(j["hours"]),
            "booked_for": booked_for,
            "drifted": drifted,
        })

    result: dict[str, OperatorSchedule] = {}
    for r in rows:
        who = result.get(r["employee_code"])
        if who is None:
            who = result[r["employee_code"]] = OperatorSchedule(
                employee_code=r["employee_code"],
                name=r["full_name"],
                skill_level=r["skill_level"],
                base_location=r["base_location"],
            )
        shift = "leave" if r["on_leave"] else r["shift"]
        definition = shifts.get(r["shift"])
        capacity = float(r["capacity_hours"])
        booked = float(r["booked_hours"])
        who.days.append(DayAvailability(
            day=r["day"],
            shift=shift,
            window=_window(definition),
            on_leave=r["on_leave"],
            is_exception=r["is_exception"],
            capacity_hours=capacity,
            booked_hours=booked,
            free_hours=max(0.0, capacity - booked),
            working=capacity > 0,
            assignments=by_day.get((r["employee_code"], r["day"]), []),
        ))
    return list(result.values())


def _window(definition) -> str | None:
    if definition is None or definition.start_hour is None:
        return None
    return f"{definition.start_hour:02d}:00-{definition.end_hour:02d}:00"


@router.get("/availability", response_model=list[OperatorSchedule])
async def list_availability(
    days: int = Query(14, ge=1, le=60),
    code: str | None = Query(None, description="Limit to one employee code"),
) -> list[OperatorSchedule]:
    """Per-day shift, capacity and committed hours for the roster."""
    rows = await operative_availability(days, code)
    if code and not rows:
        raise HTTPException(404, f"Unknown operative {code!r}")
    return rows
