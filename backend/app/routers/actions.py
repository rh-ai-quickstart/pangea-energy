"""Agent actions: writes the assistant proposes and the supervisor confirms.

The assistant records a proposal here with the arguments already resolved;
only an explicit confirmation turns one into a work order, and it goes through
the same `POST /api/maintenance` validation as the booking form.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException

from .. import db
from ..models import ProposedAction, WorkOrderAssign, WorkOrderCreate

router = APIRouter(prefix="/api/agent/actions", tags=["agent"])

logger = logging.getLogger("pangea.actions")


async def record(session_id: str | None, action: str, params: dict,
                 message: str, summary: str, fields: list[dict],
                 warnings: list[str]) -> ProposedAction:
    """Persist a proposal and return the card for the chat to render."""
    row = await db.fetchrow(
        "INSERT INTO agent_actions (session_id, action, params, message) "
        "VALUES ($1, $2, $3::jsonb, $4) RETURNING action_id, status::text AS status",
        session_id, action, json.dumps(params, default=str), message)
    logger.info("action proposed | id=%s action=%s params=%s",
                row["action_id"], action, params)
    return ProposedAction(
        action_id=row["action_id"], action=action, summary=summary,
        fields=fields, warnings=warnings, status=row["status"])


async def _load(action_id: int) -> dict:
    row = await db.fetchrow(
        "SELECT action_id, action, params, status::text AS status "
        "FROM agent_actions WHERE action_id = $1", action_id)
    if not row:
        raise HTTPException(404, f"No such action {action_id}")
    if row["status"] != "proposed":
        raise HTTPException(409, f"Action {action_id} is already {row['status']}")
    params = row["params"]
    return {**dict(row), "params": json.loads(params) if isinstance(params, str)
            else params}


async def _resolve(action_id: int, status: str, result: dict | None) -> None:
    await db.fetchrow(
        "UPDATE agent_actions SET status = $2::agent_action_status, "
        "       result = $3::jsonb, resolved_at = NOW() WHERE action_id = $1",
        action_id, status, json.dumps(result, default=str) if result else None)


@router.post("/{action_id}/confirm")
async def confirm(action_id: int) -> dict:
    """Carry out a proposed action."""
    row = await _load(action_id)
    if row["action"] not in ("schedule_work", "assign_work", "unassign_work"):
        raise HTTPException(400, f"Cannot execute action {row['action']!r}")

    # Via the ordinary endpoints, so the rota checks still apply.
    from .maintenance import assign_work_order, create_work_order

    params = dict(row["params"])
    try:
        if row["action"] in ("assign_work", "unassign_work"):
            number = params.pop("work_order_number")
            order = await assign_work_order(number, WorkOrderAssign(**params))
        else:
            order = await create_work_order(WorkOrderCreate(**params))
    except HTTPException as exc:
        await _resolve(action_id, "failed", {"error": exc.detail})
        logger.info("action failed | id=%s detail=%s", action_id, exc.detail)
        raise

    result = order.model_dump()
    await _resolve(action_id, "executed", result)
    logger.info("action executed | id=%s action=%s work_order=%s",
                action_id, row["action"], order.work_order_number)
    return {"status": "executed", "work_order": result}


@router.post("/{action_id}/cancel")
async def cancel(action_id: int) -> dict:
    await _load(action_id)
    await _resolve(action_id, "cancelled", None)
    logger.info("action cancelled | id=%s", action_id)
    return {"status": "cancelled"}


@router.get("")
async def list_actions(limit: int = 20) -> list[dict]:
    """Recent proposals and what became of them."""
    rows = await db.fetch(
        "SELECT action_id, session_id, action, params, message, "
        "       status::text AS status, result, proposed_at, resolved_at "
        "FROM agent_actions ORDER BY proposed_at DESC LIMIT $1", limit)
    out = []
    for r in rows:
        d = dict(r)
        for key in ("params", "result"):
            if isinstance(d[key], str):
                d[key] = json.loads(d[key])
        out.append(d)
    return out
