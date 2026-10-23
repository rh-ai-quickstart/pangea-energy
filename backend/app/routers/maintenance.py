"""Maintenance work orders: read the open feed and create/assign new tasks."""
from datetime import datetime, timedelta, timezone
from math import ceil

from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..models import WorkOrder, WorkOrderAssign, WorkOrderCreate
from .operators import operative_availability

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])

_OPEN_STATUSES = ("scheduled", "assigned", "in_progress", "on_hold")
_WORK_TYPES = {"preventive", "corrective", "inspection", "emergency"}
_PRIORITIES = {"low", "medium", "high", "critical"}


async def _check_shift(code: str, name: str, req: WorkOrderCreate, conn=None,
                       exclude_work_order: str | None = None) -> None:
    """Refuse an assignment the operative's rota cannot absorb, reading the
    same availability the calendar showed."""
    if req.scheduled_start is None:
        return
    # Stored values come back UTC-aware, the form sends naive wall-clock. Judge
    # both on the same basis the database writes them on.
    start = req.scheduled_start
    if start.tzinfo is not None:
        start = start.astimezone(timezone.utc).replace(tzinfo=None)
    day = start.date()
    schedule = await operative_availability(days=15, code=code, start=day, conn=conn)
    if not schedule:
        return
    days = schedule[0].days
    today, ahead = days[0], days[1:]

    if not today.working:
        nxt = next((d for d in ahead if d.working), None)
        state = "on leave" if today.on_leave else "not working"
        hint = f" Next working day is {nxt.day:%a %d %b}." if nxt else ""
        raise HTTPException(409, f"{name} is {state} on {day:%a %d %b}.{hint}")

    others = [a for a in today.assignments
              if a["work_order_number"] != exclude_work_order]
    free = today.capacity_hours - sum(a["hours"] for a in others)
    wanted = min(req.estimated_hours or today.capacity_hours, today.capacity_hours)
    if free < wanted:
        booked = ", ".join(a["title"] for a in others) or "other work"
        raise HTTPException(
            409,
            f"{name} has {free:g}h free on {day:%a %d %b} but this "
            f"needs {wanted:g}h ({booked}).")

    if today.window is None:                    # on_call
        return
    start_hour, end_hour = (int(h[:2]) for h in today.window.split("-"))
    hour = start.hour
    within = (start_hour <= hour < end_hour if start_hour < end_hour
              else hour >= start_hour or hour < end_hour)
    if not within:
        raise HTTPException(
            409,
            f"{name} works the {today.shift} shift on {day:%a %d %b} "
            f"({today.window}); {start:%H:%M} falls outside it.")


@router.get("", response_model=list[WorkOrder])
async def list_work_orders(
    limit: int = Query(50, ge=1, le=200),
) -> list[WorkOrder]:
    rows = await db.fetch(
        "SELECT w.work_order_number, w.title, w.work_type::text AS work_type, "
        "       w.priority::text AS priority, w.status::text AS status, "
        "       t.turbine_code, s.site_name, w.scheduled_start, "
        "       o.first_name || ' ' || o.last_name AS assigned_operative "
        "FROM work_orders w "
        "LEFT JOIN turbines t ON t.turbine_id = w.turbine_id "
        "LEFT JOIN sites s ON s.site_id = t.site_id "
        "LEFT JOIN maintenance_operatives o ON o.operative_id = w.assigned_operative_id "
        "WHERE w.status = ANY($1::work_order_status[]) "
        "ORDER BY CASE w.priority "
        "           WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "           WHEN 'medium' THEN 2 ELSE 3 END, "
        "         w.scheduled_start NULLS LAST "
        "LIMIT $2",
        list(_OPEN_STATUSES),
        limit,
    )
    return [WorkOrder(**dict(r)) for r in rows]


@router.post("", response_model=WorkOrder, status_code=201)
async def create_work_order(req: WorkOrderCreate) -> WorkOrder:
    if req.work_type not in _WORK_TYPES:
        raise HTTPException(400, f"Invalid work_type; expected one of {sorted(_WORK_TYPES)}")
    if req.priority not in _PRIORITIES:
        raise HTTPException(400, f"Invalid priority; expected one of {sorted(_PRIORITIES)}")

    # Serialised: without it two requests can both read a free shift and book
    # it, and both can claim the same work order number.
    async with db.transaction() as conn:
        await db.fetchrow("SELECT pg_advisory_xact_lock(hashtext('work_order_create'))",
                          conn=conn)

        turb = await db.fetchrow(
            "SELECT turbine_id FROM turbines WHERE turbine_code = $1",
            req.turbine_code, conn=conn)
        if not turb:
            raise HTTPException(400, f"Unknown turbine_code {req.turbine_code!r}")

        op_id = None
        if req.assigned_operative_code:
            op = await db.fetchrow(
                "SELECT operative_id, first_name || ' ' || last_name AS full_name "
                "FROM maintenance_operatives WHERE employee_code = $1",
                req.assigned_operative_code, conn=conn)
            if not op:
                raise HTTPException(
                    400, f"Unknown operative {req.assigned_operative_code!r}")
            op_id = op["operative_id"]
            await _check_shift(req.assigned_operative_code, op["full_name"], req,
                               conn=conn)

        year = datetime.now().year
        seq = await db.fetchrow(
            "SELECT COALESCE(MAX(SUBSTRING(work_order_number FROM 9)::int), 0) + 1 AS n "
            "FROM work_orders WHERE work_order_number ~ $1",
            f"^WO-{year}-[0-9]+$", conn=conn)
        wo_number = f"WO-{year}-{seq['n']:06d}"
        status = "assigned" if op_id else "scheduled"

        # Jobs occupy whole shifts, so an unspecified end is the shift(s) the
        # estimate needs.
        scheduled_end = req.scheduled_end
        if scheduled_end is None and req.scheduled_start is not None:
            span = max(1, ceil((req.estimated_hours or 8) / 8))
            scheduled_end = req.scheduled_start + timedelta(days=span - 1, hours=8)

        row = await db.fetchrow(
            "INSERT INTO work_orders "
            "  (work_order_number, turbine_id, work_type, priority, status, title, description, "
            "   assigned_operative_id, assigned_date, scheduled_start, scheduled_end, "
            "   estimated_hours, created_by) "
            "VALUES ($1, $2, $3::work_order_type, $4::work_order_priority, "
            "        $5::work_order_status, $6, $7, $8, "
            "        CASE WHEN $8::int IS NULL THEN NULL ELSE now() END, $9, $10, $11, $12) "
            "RETURNING work_order_number, title, work_type::text AS work_type, "
            "          priority::text AS priority, status::text AS status, scheduled_start",
            wo_number, turb["turbine_id"], req.work_type, req.priority, status,
            req.title, req.description, op_id, req.scheduled_start, scheduled_end,
            req.estimated_hours, req.created_by, conn=conn)

        site = await db.fetchrow(
            "SELECT s.site_name FROM turbines t JOIN sites s ON s.site_id = t.site_id "
            "WHERE t.turbine_id = $1", turb["turbine_id"], conn=conn)

    return WorkOrder(
        turbine_code=req.turbine_code,
        site_name=site["site_name"] if site else None,
        **dict(row),
    )


@router.patch("/{work_order_number}", response_model=WorkOrder)
async def assign_work_order(work_order_number: str,
                            req: WorkOrderAssign) -> WorkOrder:
    """Put an operative on an existing work order.

    Same rota validation and the same lock as creating one, so an assignment
    made here cannot double-book somebody a creation would have refused.
    """
    async with db.transaction() as conn:
        await db.fetchrow("SELECT pg_advisory_xact_lock(hashtext('work_order_create'))",
                          conn=conn)

        wo = await db.fetchrow(
            "SELECT w.work_order_id, w.work_order_number, w.title, w.status::text AS status, "
            "       w.scheduled_start, w.scheduled_end, w.estimated_hours, "
            "       t.turbine_code, s.site_name "
            "FROM work_orders w "
            "JOIN turbines t ON t.turbine_id = w.turbine_id "
            "JOIN sites s ON s.site_id = t.site_id "
            "WHERE w.work_order_number = $1", work_order_number, conn=conn)
        if not wo:
            raise HTTPException(404, f"No such work order {work_order_number}")
        if wo["status"] in ("completed", "cancelled"):
            raise HTTPException(409, f"{work_order_number} is already {wo['status']}")

        if req.assigned_operative_code is None:
            row = await db.fetchrow(
                "UPDATE work_orders SET assigned_operative_id = NULL, "
                "       assigned_date = NULL, updated_at = NOW(), "
                "       status = CASE WHEN status = 'assigned' "
                "                     THEN 'scheduled'::work_order_status "
                "                     ELSE status END "
                " WHERE work_order_number = $1 "
                "RETURNING work_order_number, title, work_type::text AS work_type, "
                "          priority::text AS priority, status::text AS status, "
                "          scheduled_start", work_order_number, conn=conn)
            return WorkOrder(turbine_code=wo["turbine_code"],
                             site_name=wo["site_name"], **dict(row))

        op = await db.fetchrow(
            "SELECT operative_id, first_name || ' ' || last_name AS full_name "
            "FROM maintenance_operatives WHERE employee_code = $1",
            req.assigned_operative_code, conn=conn)
        if not op:
            raise HTTPException(400, f"Unknown operative {req.assigned_operative_code!r}")

        start = req.scheduled_start or wo["scheduled_start"]
        if start is None:
            raise HTTPException(
                400, f"{work_order_number} has no scheduled start; give one to assign it.")

        check = WorkOrderCreate(
            turbine_code=wo["turbine_code"], work_type="inspection", priority="low",
            title=wo["title"], scheduled_start=start,
            estimated_hours=float(wo["estimated_hours"]) if wo["estimated_hours"] else None)
        await _check_shift(req.assigned_operative_code, op["full_name"], check,
                           conn=conn, exclude_work_order=work_order_number)

        end = wo["scheduled_end"]
        if req.scheduled_start is not None or end is None:
            span = max(1, ceil(float(wo["estimated_hours"] or 8) / 8))
            end = start + timedelta(days=span - 1, hours=8)

        row = await db.fetchrow(
            "UPDATE work_orders SET assigned_operative_id = $2, assigned_date = NOW(), "
            "       scheduled_start = $3, scheduled_end = $4, "
            "       status = CASE WHEN status = 'scheduled' THEN 'assigned'::work_order_status "
            "                     ELSE status END, "
            "       updated_at = NOW() "
            " WHERE work_order_number = $1 "
            "RETURNING work_order_number, title, work_type::text AS work_type, "
            "          priority::text AS priority, status::text AS status, scheduled_start",
            work_order_number, op["operative_id"], start, end, conn=conn)

    return WorkOrder(turbine_code=wo["turbine_code"], site_name=wo["site_name"],
                     assigned_operative=op["full_name"], **dict(row))
