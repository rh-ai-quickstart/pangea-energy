"""Conversational agent proxy: forwards a grounded chat request (dashboard state
as context) to the OpenAI-compatible LLM. Blocking /chat and SSE /chat/stream."""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from collections.abc import AsyncIterator, Callable
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock_time
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .. import db, laya, tracing
from ..config import settings
from ..models import AgentChatRequest, AgentChatResponse, ProposedAction
from . import actions, alerts, fleet, maintenance, operators, solar, wave, wind

router = APIRouter(prefix="/api/agent", tags=["agent"])

logger = logging.getLogger("pangea.agent")

_site_reference: list[dict] | None = None


class Grounding(NamedTuple):
    """What a tool produced.

    `prompt` grounds the model. When `facts` is set the tool has already
    resolved the answer in code: the model writes only a short lead-in and
    `facts` is appended to it verbatim, so names and numbers cannot be
    invented, dropped or merged. If the model is unreachable the lead-in is
    simply skipped and `facts` still answers the question.

    `card` is a write awaiting confirmation, already recorded in
    `agent_actions`."""
    prompt: str
    facts: str | None = None
    card: ProposedAction | None = None
    site: str | None = None


async def _load_site_reference() -> list[dict]:
    global _site_reference
    if _site_reference is None:
        rows = await db.fetch(
            "SELECT site_name, site_code, energy_type::text AS energy_type, "
            "       country, nearest_city, latitude, longitude "
            "FROM sites WHERE is_active ORDER BY site_name"
        )
        _site_reference = [dict(r) for r in rows]
    return _site_reference


def _build_messages(req: AgentChatRequest, context: dict | None,
                    site_ref: list[dict] | None,
                    directive: str | None = None) -> list[dict]:
    messages = [{"role": "system", "content": settings.agent_system_prompt}]
    # A directive (e.g. the alerts tool) supersedes the generic state dump and
    # is self-contained, so we drop the site reference, the JSON context and the
    # history replay to stop the model answering from anything but the directive.
    if directive:
        messages.append({"role": "system", "content": directive})
    else:
        if site_ref:
            places = [{k: s.get(k) for k in
                       ("site_name", "site_code", "country", "nearest_city")}
                      for s in site_ref]
            messages.append({
                "role": "system",
                "content": "Authoritative site locations (use these for any country "
                "or city question; do not guess):\n" + json.dumps(places, default=str, ensure_ascii=False),
            })
        if context:
            messages.append({
                "role": "system",
                "content": "Current dashboard state (JSON):\n"
                + json.dumps(context, default=str, ensure_ascii=False),
            })
        for turn in (req.history or [])[-6:]:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": req.message})
    return messages


# "and Runde?", "alerts?", "totals" need the previous turn to mean anything.
# Anything that stands on its own must not get it: the older message is usually
# the longer one, so prepending it drags the classifier back to the old topic.
_ELLIPTICAL_RE = re.compile(r"^\s*(?:and|what about|how about|same for|what of)\b",
                            re.I)


def _is_elliptical(message: str) -> bool:
    text = (message or "").strip()
    return len(text.split()) <= 3 or bool(_ELLIPTICAL_RE.match(text))


def _route_state(req: AgentChatRequest) -> str:
    if not _is_elliptical(req.message):
        return req.message
    prev_user = ""
    for turn in reversed(req.history or []):
        if turn.get("role") == "user" and turn.get("content"):
            prev_user = turn["content"]
            break
    return f"{prev_user}\n{req.message}".strip() if prev_user else req.message


_SEVERITIES = ("emergency", "critical", "warning", "info")
_WO_PRIORITIES = ("critical", "high", "medium", "low")


def _list_directive(noun: str, one: str, scope: str, items: list,
                    render: Callable[[dict], str],
                    highlight: Callable[[list], str] | None = None) -> Grounding:
    """A list answer, written entirely in code.

    Granite 4.0 350M cannot summarise rows it has been handed: opening a list
    of fourteen it has claimed there were three, that there were none, and
    that it was unable to help. The count and the one thing worth pointing out
    are both computable, so they are computed and the model is not called.

    Plain text, no markdown: the chat bubble renders with textContent and the
    reply is also what gets read aloud by /api/voice/speak."""
    if not items:
        return Grounding(prompt="", facts=f"There are no {scope}{noun} right now.")
    head = f"{len(items)} {scope}{noun}" if len(items) > 1 else f"One {scope}{one}"
    extra = highlight(items) if highlight else ""
    return Grounding(
        prompt="",
        facts=f"{head}{' - ' + extra if extra else ''}:\n"
              + "\n".join("- " + render(i) for i in items))


def _worst(items: list, key: str, order: tuple[str, ...]) -> dict | None:
    ranked = [i for i in items if (i.get(key) or "").lower() in order]
    return min(ranked, key=lambda i: order.index(i[key].lower()), default=None)


def _lead_tasks(items: list) -> str:
    top = _worst(items, "priority", _WO_PRIORITIES)
    if not top:
        return ""
    n = sum(1 for i in items if i.get("priority") == top["priority"])
    return (f"{n} at {top['priority']} priority" if n > 1
            else f"{top['title']} is the {top['priority']} one")


def _lead_alerts(items: list) -> str:
    top = _worst(items, "severity", _SEVERITIES)
    if not top:
        return ""
    n = sum(1 for i in items if i.get("severity") == top["severity"])
    return f"{n} {top['severity']}" if n > 1 else f"{top['title']} is {top['severity']}"


def _lead_operators(items: list) -> str:
    free = min(items, key=lambda o: o.get("open_assignments") or 0, default=None)
    if not free:
        return ""
    n = free.get("open_assignments") or 0
    return f"{free['name']} is lightest loaded on {n} job{'s' if n != 1 else ''}"


def _lead_wind(items: list) -> str:
    down = [w for w in items if w.get("shutdown")]
    if down:
        return f"{len(down)} shut down: " + ", ".join(w["site_name"] for w in down)
    top = max(items, key=lambda w: w.get("wind_speed_ms") or 0, default=None)
    return f"{top['site_name']} windiest at {top['wind_speed_ms']:.1f} m/s" if top else ""


def _lead_output(items: list) -> str:
    worst = max(items, key=lambda d: d.get("residual_kw") or 0, default=None)
    if not worst or (worst.get("residual_kw") or 0) < 1:
        return "all close to expectation"
    return f"{worst['site_name']} is {worst['residual_kw']:.0f} kW under"


_LEAD_IN_MAX_TOKENS = 90
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s")


def _lead_in(text: str) -> str:
    """Keep the model's prose; drop any list it tried to emit regardless."""
    kept: list[str] = []
    for line in (text or "").strip().splitlines():
        if _BULLET_RE.match(line):
            break
        kept.append(line.strip())
    return " ".join(line for line in kept if line).strip()


_DENIAL_RE = re.compile(
    r"\b(?:no|not any|none|nothing|zero)\s+(?:active|open|current|outstanding|"
    r"available|assigned|pending)?\s*(?:tasks?|jobs?|alerts?|operators?|"
    r"technicians?|work orders?|readings?)\b"
    r"|\b(?:unable|cannot|can't|can not) (?:to )?(?:provide|assist|help|access)\b"
    r"|\bthere are (?:currently )?(?:no|none)\b"
    r"|\bmy limitations\b", re.I)


def _with_facts(reply: str, facts: str) -> str:
    lead = _lead_in(reply)
    # The list below is correct by construction. A lead-in that denies it is
    # worse than no lead-in at all.
    if lead and _DENIAL_RE.search(lead):
        logger.info("agent grounding | dropped contradicting lead-in: %r", lead[:120])
        lead = ""
    return f"{lead}\n\n{facts}" if lead else facts


def _join(*bits: str) -> str:
    return " — ".join(b for b in bits if b)


def _render_alert(a: dict) -> str:
    where = " / ".join(str(x) for x in (a.get("turbine"), a.get("site")) if x)
    sev = (a.get("severity") or "").lower()
    title = a.get("title") or "untitled"
    return (f"{sev}: {title}" if sev else title) + (f" ({where})" if where else "")


def _render_task(t: dict) -> str:
    where = " / ".join(str(x) for x in (t.get("turbine"), t.get("site")) if x)
    state = ", ".join(x for x in (
        f"{t['priority']} priority" if t.get("priority") else "",
        str(t.get("status") or "")) if x)
    title = t.get("title") or t.get("id")
    who = t.get("assigned_operative") or "nobody assigned"
    return _join(f"{title} ({where})" if where else str(title), state, who)


def _render_operator(o: dict) -> str:
    who = ", ".join(str(x) for x in (o.get("skill_level"), o.get("base_location")) if x)
    shift = (o.get("today_shift") or "").replace("_", "-")
    load = o.get("open_assignments") or 0
    name = str(o.get("name")) + (f" ({who})" if who else "")
    return _join(name,
                 f"{shift} shift today" if shift else "",
                 f"{load} job{'s' if load != 1 else ''} assigned",
                 f"now: {o['current_task']}" if o.get("current_task") else "")


def _merge(*parts: Grounding) -> Grounding:
    """Answer with several tools at once.

    Asked about the weather at one of our own sites, the general observation
    and the hub-height wind are both right and neither is the whole answer:
    Open-Meteo reports wind at 10 m, the cut-out that stops the turbines is
    measured at 100 m.
    """
    kept = [g for g in parts if g]
    facts = "\n\n".join(g.facts for g in kept if g.facts) or None
    return Grounding(
        prompt="\n\n".join(g.prompt for g in kept if g.prompt),
        facts=facts,
        card=next((g.card for g in kept if g.card), None),
        site=next((g.site for g in kept if g.site), None))


def _render_wind(w: dict) -> str:
    bits = [f"{w['site_name']}: {w['wind_speed_ms']:.1f} m/s"]
    if w.get("gust_ms") is not None:
        bits.append(f"gusting {w['gust_ms']:.1f}")
    state = ("SHUT DOWN, above the {:.0f} m/s cut-out".format(w["cutout_ms"])
             if w["shutdown"] else w.get("status") or "operating")
    return _join(", ".join(bits), state,
                 f"{w['turbine_count']} turbines" if w.get("turbine_count") else "")


async def _wind_directive(req: AgentChatRequest, site: str | None) -> Grounding:
    # Live per-site wind from the same source as GET /api/wind/status, rendered
    # in code: the speeds are the whole answer, so the model never states one.
    rows = [w.model_dump() for w in await wind.wind_statuses()]
    if site and site != "any":
        rows = [w for w in rows if w["site_code"] == site]
    scope = f"{rows[0]['site_name']} " if len(rows) == 1 else ""
    logger.info("wind tool | site=%s -> %d site(s)", site or "all", len(rows))
    return _list_directive("wind readings", "wind reading", scope, rows, _render_wind, _lead_wind)


def _render_solar(d: dict) -> str:
    return _join(f"{d['site_name']}: {d['actual_now_kw']:.0f} kW now",
                 f"model expects {d['predicted_now_kw']:.0f} kW",
                 (f"{abs(d['residual_kw']):.0f} kW "
                  f"{'under' if d['residual_kw'] > 0 else 'over'}")
                 if abs(d.get("residual_kw") or 0) >= 1 else "on expectation")


def _render_wave(d: dict) -> str:
    return _render_solar(d)


async def _solar_directive(req: AgentChatRequest, site: str | None) -> Grounding:
    rows = [r.model_dump() for r in await solar.predict_all(
        site_code=site if site and site != "any" else None)]
    scope = f"{rows[0]['site_name']} " if len(rows) == 1 else ""
    logger.info("solar tool | site=%s -> %d site(s)", site or "all", len(rows))
    return _list_directive("solar sites", "solar site", scope, rows, _render_solar, _lead_output)


async def _wave_directive(req: AgentChatRequest, site: str | None) -> Grounding:
    rows = [r.model_dump() for r in await wave.predict_all(
        site_code=site if site and site != "any" else None)]
    scope = f"{rows[0]['site_name']} " if len(rows) == 1 else ""
    logger.info("wave tool | site=%s -> %d farm(s)", site or "all", len(rows))
    return _list_directive("wave farms", "wave farm", scope, rows, _render_wave, _lead_output)


async def _fleet_totals_directive(req: AgentChatRequest) -> Grounding:
    st = await fleet.fleet_status()
    by = ", ".join(f"{c.count} {c.status}" for c in st.by_status)
    pct = (st.live_output_mw / st.rated_capacity_mw * 100) if st.rated_capacity_mw else 0
    facts = (f"Fleet output right now: {st.live_output_mw:.1f} MW of "
             f"{st.rated_capacity_mw:.1f} MW rated ({pct:.0f}%).\n"
             f"- {st.total_turbines} turbines: {by}")
    logger.info("fleet tool | %.1f MW of %.1f MW", st.live_output_mw, st.rated_capacity_mw)
    return Grounding(
        prompt=("The user asked for combined fleet output. The figures are already "
                "written out below your reply, so write ONLY a one-sentence lead-in. "
                "Do not repeat or recalculate the numbers.\n\n" + facts),
        facts=facts)


async def _alerts_directive(req: AgentChatRequest) -> Grounding:
    # Read the feed, not the browser's copy of it: ui_context carries only the
    # first ten, and voice requests carry none at all. Same source as
    # GET /api/alerts, so live high-wind shutdowns are included.
    rows = [{"title": a.title, "severity": a.severity,
             "turbine": a.turbine_code, "site": a.site_name}
            for a in await alerts.list_alerts(status="active", limit=200)]
    wanted = {s for s in _SEVERITIES if s in req.message.lower()}
    if wanted:
        rows = [a for a in rows if (a.get("severity") or "").lower() in wanted]
    scope = "/".join(sorted(wanted)) + " " if wanted else ""
    logger.info("alerts tool | wanted=%s -> %d alert(s)", wanted or "all", len(rows))
    return _list_directive("alerts", "alert", scope, rows, _render_alert,
                           _lead_alerts)


# "Open" to a supervisor means nobody is on it yet - work that needs their
# action. Internally every unfinished order is "open", which is not the
# question being asked.
_NEEDS_ACTION_RE = re.compile(
    r"\b(?:open|outstanding|unassigned|unallocated|needs? (?:action|assigning|"
    r"someone|somebody)|nobody on|no[- ]one on|uncovered)\b", re.I)


async def _maintenance_directive(req: AgentChatRequest) -> Grounding:
    rows = [{"id": w.work_order_number, "title": w.title, "type": w.work_type,
             "priority": w.priority, "status": w.status,
             "turbine": w.turbine_code, "site": w.site_name,
             "scheduled_start": w.scheduled_start,
             "assigned_operative": w.assigned_operative}
            for w in await maintenance.list_work_orders(limit=200)]
    scope = ""
    if _NEEDS_ACTION_RE.search(req.message):
        rows = [t for t in rows if not t.get("assigned_operative")]
        scope = "unassigned "
    wanted = {p for p in _WO_PRIORITIES if p in req.message.lower()}
    if wanted:
        rows = [t for t in rows if (t.get("priority") or "").lower() in wanted]
        scope += "/".join(sorted(wanted)) + " priority "
    logger.info("maintenance tool | wanted=%s -> %d task(s)", wanted or "all", len(rows))
    return _list_directive("maintenance tasks", "maintenance task", scope,
                           rows, _render_task, _lead_tasks)


_FREE_RE = re.compile(
    r"\b(free|available|availability|spare|idle|unassigned|not busy|anyone|"
    r"anybody|somebody|someone|send|dispatch|assign|capacity|spare capacity|"
    r"best person|recommend|suggest|take a look|pick up|cover)\b"
    r"|\bwho (?:can|should|could|would|do i|to)\b"
    r"|\b(?:any|which) (?:operators?|technicians?|engineers?|crew)\b"
    r"|\bcan (?:take|do|handle|look)\b", re.I)
_SHIFT_TERMS = {"night": "night", "nights": "night", "on call": "on_call",
                "on-call": "on_call", "day shift": "day", "days": "day"}
_CERT_TERMS = {"rope access": "rope_access", "offshore": "offshore_safety",
               "high voltage": "high_voltage", "high-voltage": "high_voltage",
               "electrical": "electrical", "mechanical": "mechanical",
               "hydraulic": "hydraulic", "blade": "blade_repair"}
_OPERATOR_BRIEF = ("name", "skill_level", "base_location", "today_shift",
                   "open_assignments", "current_task")
_OPERATOR_FULL = _OPERATOR_BRIEF + (
    "employee_code", "certifications", "specializations", "offshore_certified",
    "weekly_schedule", "exception_dates", "upcoming_assignments")


def _fold(text: str) -> str:
    """Lower-case and strip accents; half the roster has them."""
    stripped = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in stripped if not unicodedata.combining(c)).lower()


def _mentions(low: str, word: str) -> bool:
    return bool(re.search(rf"\b{re.escape(word)}\b", low))


def _named_operators(message: str, rows: list[dict]) -> list[dict]:
    """Operatives the message names, by full name, surname or first name. A
    first name only counts when exactly one person on the roster has it."""
    low = _fold(message)
    firsts: dict[str, int] = {}
    for r in rows:
        first = _fold(r.get("name") or "").split(" ")[0]
        firsts[first] = firsts.get(first, 0) + 1

    hits = []
    for r in rows:
        name = _fold(r.get("name") or "")
        if not name:
            continue
        first, _, surname = name.partition(" ")
        surname = surname or first
        if (name in low
                or (len(surname) > 3 and _mentions(low, surname))
                or (len(first) > 2 and firsts[first] == 1 and _mentions(low, first))):
            hits.append(r)
    return hits


def _operators_directive(req: AgentChatRequest, ops: list) -> Grounding:
    rows = [o.model_dump() for o in ops]
    low = req.message.lower()
    scope = ""

    named = _named_operators(req.message, rows)
    if named:
        slim = [{k: r.get(k) for k in _OPERATOR_FULL} for r in named]
        who = ", ".join(r["name"] for r in named)
        logger.info("operators tool | named=%s", who)
        return Grounding(prompt=(
            f"The user asked about {who}. Below is their complete record. Answer "
            "ONLY about them, in at most two sentences, using these fields: "
            "today_shift is the shift they are working today ('off' means not "
            "working), open_assignments is how many jobs they already hold, and "
            "current_task is what they are doing right now. Do not list other "
            "operators and do not invent anyone.\n"
            + json.dumps(slim, default=str, ensure_ascii=False)
        ))

    shift = next((v for k, v in _SHIFT_TERMS.items() if k in low), None)
    if shift:
        rows = [r for r in rows if (r.get("today_shift") or "").lower() == shift]
        scope = f"{shift.replace('_', '-')} shift "
    elif _FREE_RE.search(low):
        rows = [r for r in rows if (r.get("today_shift") or "").lower() != "off"]
        rows.sort(key=lambda r: (r.get("open_assignments") or 0, r["name"]))
        scope = "on-shift "

    cert = next((v for k, v in _CERT_TERMS.items() if k in low), None)
    if cert:
        rows = [r for r in rows if cert in (r.get("certifications") or [])]
        scope += f"{cert.replace('_', ' ')}-certified "

    fields = _OPERATOR_FULL if len(rows) <= 3 else _OPERATOR_BRIEF
    slim = [{k: r.get(k) for k in fields} for r in rows]
    logger.info("operators tool | scope=%s -> %d operator(s)", scope or "all", len(slim))
    return _list_directive("operators/technicians", "operator/technician",
                           scope, slim, _render_operator,
                           _lead_operators)


# ---------------------------------------------------------------------------
# Scheduling: turning an instruction into a proposed work order
# ---------------------------------------------------------------------------

# Deliberately narrow: a false positive puts a confirm card in front of
# somebody who only asked a question.
_SCHEDULE_RE = re.compile(
    r"\b(?:book|raise|create|arrange|set up|put in|dispatch|assign|"
    r"reassign|re-assign|swap)\b"
    # "put Astrid on WO-..." / "put someone on CRG-14"
    r"|\bput\s+\w+\s+(?:on|onto)\b"
    # "open a work order" is an instruction; "open work orders" is a read.
    r"|\b(?:open|log)\s+(?:up\s+)?an?\b"
    r"|(?<!the )(?<!my )(?<!our )(?<!their )\bschedule\b"
    r"|\bsend\s+(?:out\s+)?(?:somebody|someone|a |an |\w+ )?(?:to|over|up)\b"
    r"|\bget\s+(?:somebody|someone|\w+)\s+(?:to|out|over)\b", re.I)
_QUESTION_RE = re.compile(r"^\s*(?:who|what|when|which|where|how|why|is|are|"
                          r"can|could|does|do|did|has|have|should i know)\b", re.I)
# Rotas, leave, parts and alerts are not work orders. With a turbine named it
# is a job regardless ("assign Robin to MWP-14"); without one it is not.
_NOT_WORK_RE = re.compile(
    r"\b(?:shifts?|nights?|rota|roster|leave|holiday|time off|parts?|stock|"
    r"inventory|spares?|alerts?|alarms?)\b", re.I)

_TURBINE_RE = re.compile(r"\b([A-Z]{3})[- ]?(\d{1,2})\b", re.I)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_IN_DAYS_RE = re.compile(r"\bin (\d{1,2}) days?\b", re.I)
_TIME_RE = re.compile(r"\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b")
_HOURS_RE = re.compile(r"\b(\d{1,2}(?:\.\d)?)\s*(?:h\b|hrs?\b|hours?\b)", re.I)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")

_WORK_TYPE_TERMS = (
    ("emergency",  ("emergency", "urgent", "asap", "right now", "immediately")),
    ("corrective", ("repair", "fix", "replace", "broken", "fault", "failed",
                    "leak", "not working", "swap")),
    ("preventive", ("service", "routine", "lubricat", "grease", "clean",
                    "top up", "scheduled maintenance", "preventive")),
    ("inspection", ("inspect", "inspection", "check", "survey", "look at",
                    "assess", "examine")),
)
_PRIORITY_TERMS = (
    ("critical", ("critical", "emergency", "urgent", "asap", "immediately")),
    ("high",     ("high priority", "high-priority", "important", "soon")),
    ("low",      ("low priority", "low-priority", "when convenient",
                  "no rush", "sometime")),
)
_SUBJECTS = ("gearbox", "generator", "blade", "yaw", "pitch", "converter",
             "transformer", "bearing", "brake", "sensor", "hydraulic",
             "cooling", "nacelle", "tower", "bolt", "cable", "switchgear",
             "lubrication", "oil", "seal", "anemometer")


_SOONEST_RE = re.compile(
    r"\b(?:first available|next available|earliest|soonest|as soon as|asap|"
    r"whenever|first slot|first free)\b", re.I)


def _parse_day(low: str, today: date) -> date | None:
    # "first available" means today onward; _find_slot moves it to the first
    # day the chosen operative actually works.
    if _SOONEST_RE.search(low):
        return today
    if "today" in low or "tonight" in low:
        return today
    if "tomorrow" in low:
        return today + timedelta(days=1)
    iso = _ISO_DATE_RE.search(low)
    if iso:
        try:
            return date.fromisoformat(iso.group(1))
        except ValueError:
            return None
    in_days = _IN_DAYS_RE.search(low)
    if in_days:
        return today + timedelta(days=int(in_days.group(1)))
    for i, name in enumerate(_WEEKDAYS):
        if name in low:
            ahead = (i - today.weekday()) % 7
            return today + timedelta(days=ahead or 7)
    return None


def _first_term(low: str, table) -> str | None:
    return next((value for value, terms in table
                 if any(t in low for t in terms)), None)


def _parse_schedule(message: str, today: date) -> dict:
    """Pull the work order arguments out of the message in code; Granite 4.0
    350M cannot be trusted to extract them. Anything unstated is left None."""
    low = message.lower()
    turbine = _TURBINE_RE.search(message)
    hours = _HOURS_RE.search(low)
    clock = _TIME_RE.search(low)
    work_type = _first_term(low, _WORK_TYPE_TERMS) or "inspection"
    priority = _first_term(low, _PRIORITY_TERMS) or (
        "critical" if work_type == "emergency" else "medium")
    subject = next((s for s in _SUBJECTS if s in low), None)

    est = float(hours.group(1)) if hours else None
    if est is None:
        if "half a day" in low or "half day" in low:
            est = 4.0
        elif "two days" in low or "couple of days" in low:
            est = 16.0

    return {
        "turbine_code": (turbine.group(1) + "-" + turbine.group(2).zfill(2)).upper()
                        if turbine else None,
        "day": _parse_day(low, today),
        "hour": int(clock.group(1)) if clock else None,
        "work_type": work_type,
        "priority": priority,
        "subject": subject,
        "estimated_hours": est,
    }


def _schedule_title(turbine_code: str, subject: str | None, work_type: str) -> str:
    noun = {"corrective": "repair", "preventive": "service",
            "emergency": "emergency repair"}.get(work_type, "inspection")
    return f"{turbine_code} {subject} {noun}" if subject else f"{turbine_code} {noun}"


# A scheduling request often arrives in pieces: the turbine in one message,
# the day in the next. Each message is parsed alone, so without this the two
# never meet and the user is asked the same question forever.
_PENDING: dict[str, tuple[float, dict]] = {}
_PENDING_TTL = 600.0


def _remember(session: str | None, parsed: dict) -> dict:
    """Merge this message's slots over whatever is still outstanding."""
    if not session:
        return parsed
    now = time.monotonic()
    for k, (ts, _) in list(_PENDING.items()):
        if now - ts > _PENDING_TTL:
            _PENDING.pop(k, None)
    prior = _PENDING.get(session, (0.0, {}))[1]
    # Start from this message so every key exists, then fill the gaps from what
    # was already known. Overlaying the other way drops keys that are legitimately
    # unset in this turn.
    merged = dict(parsed)
    for k, v in prior.items():
        if v is not None and merged.get(k) is None:
            merged[k] = v
    _PENDING[session] = (now, merged)
    return merged


def _forget(session: str | None) -> None:
    if session:
        _PENDING.pop(session, None)


async def _schedule_directive(req: AgentChatRequest, ops: list) -> Grounding:
    """Resolve an instruction into a work order proposal, or ask what is missing."""
    today = date.today()
    parsed = _remember(req.session_id, _parse_schedule(req.message, today))
    rows = [o.model_dump() for o in ops]
    named = _named_operators(req.message, rows)
    operative = named[0] if named else None
    if operative is None and parsed.get("operative_code"):
        operative = next((r for r in rows
                          if r["employee_code"] == parsed["operative_code"]), None)
    if operative is not None:
        parsed["operative_code"] = operative["employee_code"]
        _remember(req.session_id, parsed)

    missing = []
    if not parsed["turbine_code"]:
        missing.append("which turbine (for example MTP-07)")
    if not parsed["day"]:
        missing.append("which day")
    if missing:
        ask = " and ".join(missing)
        logger.info("schedule tool | incomplete, missing=%s", missing)
        return Grounding(
            prompt=(f"The user asked to schedule work but did not say {ask}. "
                    f"Ask them for exactly that, in one short sentence. Do not "
                    f"invent a turbine, a date or a technician."),
            facts=f"I need to know {ask} before I can raise that.")

    turbine = await db.fetchrow(
        "SELECT t.turbine_code, s.site_code, s.site_name, s.terrain::text AS terrain FROM turbines t "
        "JOIN sites s ON s.site_id = t.site_id WHERE t.turbine_code = $1",
        parsed["turbine_code"])
    if not turbine:
        logger.info("schedule tool | unknown turbine=%s", parsed["turbine_code"])
        return Grounding(
            prompt=(f"The user asked to schedule work on {parsed['turbine_code']}, "
                    "which is not a turbine in the fleet. Say so in one sentence "
                    "and ask them to check the code."),
            facts=f"{parsed['turbine_code']} is not a turbine in the fleet.")

    hours = parsed["estimated_hours"] or 8.0
    warnings: list[str] = []
    start_day, shift_hour = parsed["day"], parsed["hour"]

    # "send whoever is free" is an instruction to choose, not to leave it open.
    if operative is None and _FREE_RE.search(req.message):
        job = {"title": _schedule_title(turbine["turbine_code"], parsed["subject"],
                                        parsed["work_type"]),
               "description": req.message,
               "terrain": turbine["terrain"]}
        operative, why = await _pick_operator(job, rows, start_day, hours)
        if operative is None:
            return Grounding(
                prompt=(f"Nobody can take this: {why}. Say so in one sentence."),
                facts=f"Cannot assign anyone — {why}.")
        warnings.append(why)
        parsed["operative_code"] = operative["employee_code"]

    if operative:
        slot, note = await _find_slot(operative["employee_code"], parsed["day"], hours)
        if slot is None:
            logger.info("schedule tool | no slot for %s", operative["employee_code"])
            return Grounding(
                prompt=(f"{operative['name']} has no free shift in the next three "
                        "weeks for this job. Say so in one sentence and suggest "
                        "the user pick somebody else."),
                facts=(f"{operative['name']} has no free shift in the next three "
                       f"weeks that can take {hours:g}h."))
        start_day, shift_hour = slot["day"], slot["hour"]
        if note:
            warnings.append(note)
    elif shift_hour is None:
        shift_hour = 7

    start = datetime.combine(start_day, clock_time(hour=shift_hour or 7),
                             tzinfo=timezone.utc)
    title = _schedule_title(turbine["turbine_code"], parsed["subject"],
                            parsed["work_type"])
    params = {
        "turbine_code": turbine["turbine_code"],
        "work_type": parsed["work_type"],
        "priority": parsed["priority"],
        "title": title,
        "description": f"Raised from the assistant: “{req.message.strip()}”",
        "assigned_operative_code": operative["employee_code"] if operative else None,
        "scheduled_start": start.isoformat(),
        "estimated_hours": hours,
        "created_by": "pangea-assistant",
    }
    fields = [
        {"label": "Turbine", "value": f"{turbine['turbine_code']} — {turbine['site_name']}"},
        {"label": "Work", "value": f"{title} ({parsed['work_type']}, {parsed['priority']} priority)"},
        {"label": "Assigned to", "value": operative["name"] if operative else "Unassigned"},
        {"label": "Starts", "value": start.strftime("%a %d %b, %H:%M")},
        {"label": "Estimate", "value": f"{hours:g}h"},
    ]
    summary = (f"Raise {title} for "
               f"{operative['name'] if operative else 'nobody yet'} on "
               f"{start:%a %d %b}?")
    card = await actions.record(
        req.session_id, "schedule_work", params, req.message,
        summary, fields, warnings)
    _forget(req.session_id)

    facts = summary + "\n" + "\n".join(f"- {f['label']}: {f['value']}" for f in fields)
    if warnings:
        facts += "\n" + "\n".join(f"- Note: {w}" for w in warnings)
    facts += "\nNothing is booked until you confirm."
    return Grounding(
        prompt=("The user asked to schedule work. The proposal is already "
                "written out and shown to them with Confirm and Cancel "
                "buttons, so write ONLY a one-sentence lead-in telling them to "
                "check it over. Do not restate the details, do not list "
                "anything, and do not claim it is booked — it is not yet.\n\n"
                "Proposal (already displayed): " + json.dumps(params, default=str, ensure_ascii=False)),
        facts=facts, card=card, site=turbine["site_code"])


_WO_RE = re.compile(r"\b(WO-\d{4}-\d{6})\b", re.I)
_MOVE_RE = re.compile(
    r"\b(?:reassign|re-assign|swap|instead|take over|hand (?:it )?(?:to|over)|"
    r"assign|dispatch)\b|\bput\s+\w+\s+on(?:to)?\b|\bmove\s+\w+\s+(?:to|onto)\b",
    re.I)
_UNASSIGN_RE = re.compile(
    r"\b(?:unassign|de-?assign)\b"
    r"|\btake\s+(?:\w+\s+){0,2}?off\b"
    r"|\bremove\s+(?:\w+\s+){0,2}?(?:from|off)\b"
    r"|\bclear\s+(?:\w+\s+){0,2}?(?:from|off)\b", re.I)

_CERT_HINTS = (
    ("rope_access",    ("rope access", "rope-access", "abseil", "leading edge",
                        "blade survey", "blade inspection")),
    ("high_voltage",   ("high voltage", "high-voltage", "hv ", "switchgear",
                        "transformer")),
    ("offshore_safety",("offshore", "vessel", "boat landing", "platform")),
    ("blade_repair",   ("blade repair", "composite", "erosion repair")),
    ("hydraulic",      ("hydraulic", "accumulator", "pitch cylinder")),
    ("electrical",     ("electrical", "converter", "generator winding", "cable")),
)


def _required_certs(text: str) -> set[str]:
    low = (text or "").lower()
    return {cert for cert, terms in _CERT_HINTS if any(t in low for t in terms)}


async def _open_work_order(message: str) -> dict | None:
    """The existing job the user means: by number, else an unassigned one on a
    turbine they named."""
    wo = _WO_RE.search(message)
    if wo:
        return await db.fetchrow(
            "SELECT w.work_order_number, w.title, w.description, w.priority::text AS priority, "
            "       w.estimated_hours, w.scheduled_start, w.assigned_operative_id, "
            "       t.turbine_code, s.site_code, s.site_name, s.terrain::text AS terrain "
            "FROM work_orders w JOIN turbines t ON t.turbine_id = w.turbine_id "
            "JOIN sites s ON s.site_id = t.site_id "
            "WHERE upper(w.work_order_number) = upper($1) "
            "  AND w.status NOT IN ('completed','cancelled')", wo.group(1))

    turbine = _TURBINE_RE.search(message)
    if not turbine:
        return None
    code = (turbine.group(1) + "-" + turbine.group(2).zfill(2)).upper()
    # An unassigned job on that turbine is the obvious target. An assigned one
    # only counts when the user is plainly moving somebody, so "book an
    # inspection on BJF-09" still raises new work.
    row = await db.fetchrow(
        "SELECT w.work_order_number, w.title, w.description, w.priority::text AS priority, "
        "       w.estimated_hours, w.scheduled_start, w.assigned_operative_id, "
        "       t.turbine_code, s.site_code, s.site_name, s.terrain::text AS terrain "
        "FROM work_orders w JOIN turbines t ON t.turbine_id = w.turbine_id "
        "JOIN sites s ON s.site_id = t.site_id "
        "WHERE t.turbine_code = $1 AND w.status NOT IN ('completed','cancelled') "
        "ORDER BY (w.assigned_operative_id IS NOT NULL), w.scheduled_start LIMIT 1", code)
    if (row and row["assigned_operative_id"] is not None
            and not (_MOVE_RE.search(message) or _UNASSIGN_RE.search(message))):
        return None
    return row


async def _pick_operator(job: dict, rows: list[dict], day: date, hours: float,
                         exclude: set[str] | None = None
                         ) -> tuple[dict | None, str | None]:
    """Lightest-loaded operative who is free that day and holds what the job needs."""
    needed = _required_certs(f"{job.get('title')} {job.get('description')}")
    if (job.get("terrain") or "") == "offshore":
        needed.add("offshore_safety")

    blocked = exclude or set()
    qualified = [r for r in rows
                 if needed <= set(r.get("certifications") or [])
                 and r["employee_code"] not in blocked]
    if not qualified:
        return None, (f"nobody on the roster holds "
                      f"{', '.join(sorted(needed)).replace('_', ' ')}")

    # Earliest day any of them can take it, not just the day asked for.
    free = []
    for r in qualified:
        slot, _ = await _find_slot(r["employee_code"], day, hours)
        if slot:
            free.append((slot["day"], r.get("open_assignments") or 0,
                         r.get("name") or "", r))
    if not free:
        return None, (f"none of the {len(qualified)} qualified technicians has a "
                      f"free shift in the next three weeks")

    free.sort(key=lambda x: x[:3])
    first_day, _, _, chosen = free[0]
    same_day = sum(1 for d, *_ in free if d == first_day)
    if first_day == day:
        note = (f"Picked {chosen['name']} — free on {day:%a %d %b} and the lightest "
                f"loaded of {same_day} who qualify.")
    else:
        note = (f"Nobody qualified is free on {day:%a %d %b}; "
                f"{chosen['name']} is the first, on {first_day:%a %d %b}.")
    return chosen, note


async def _unassign_directive(req: AgentChatRequest, job: dict) -> Grounding:
    """Propose taking whoever is on a job back off it."""
    if job["assigned_operative_id"] is None:
        return Grounding(
            prompt=(f"{job['work_order_number']} has nobody on it already. Say so "
                    "in one short sentence."),
            facts=f"{job['work_order_number']} already has nobody assigned.")

    who = await db.fetchrow(
        "SELECT first_name || ' ' || last_name AS name FROM maintenance_operatives "
        "WHERE operative_id = $1", job["assigned_operative_id"])
    name = who["name"] if who else "the assigned technician"
    params = {"work_order_number": job["work_order_number"],
              "assigned_operative_code": None}
    fields = [
        {"label": "Work order", "value": f"{job['work_order_number']} — {job['title']}"},
        {"label": "Turbine", "value": f"{job['turbine_code']} — {job['site_name']}"},
        {"label": "Remove", "value": name},
        {"label": "Leaves it", "value": "unassigned, back in the pool"},
    ]
    summary = f"Take {name} off {job['work_order_number']} ({job['title']})?"
    card = await actions.record(req.session_id, "unassign_work", params, req.message,
                                summary, fields, [])
    facts = (summary + "\n"
             + "\n".join(f"- {f['label']}: {f['value']}" for f in fields)
             + "\nNothing changes until you confirm.")
    return Grounding(
        prompt=("The user asked to take somebody off a job. The proposal is shown "
                "to them with Confirm and Cancel buttons. Write ONLY a one-sentence "
                "lead-in. Do not restate the details and do not claim it is done.\n\n"
                + json.dumps(params, default=str, ensure_ascii=False)),
        facts=facts, card=card, site=job["site_code"])


# "Samuel is sick", "he can't do it", "someone else" — a name can be mentioned
# to rule somebody OUT. Without this, naming anyone assigns them.
_EXCLUDE_RE = re.compile(
    r"\b(?:sick|ill|unavailable|unable|away|on leave|off today|"
    r"can'?t|cannot|can not|won'?t|someone else|somebody else|"
    r"another (?:operator|technician|engineer|person)|anyone else)\b", re.I)

# ...but "put Lukas on it instead" names a replacement, not an exclusion.
_POSITIVE_CUE_RE = re.compile(
    r"(?:put|assign(?:ed)?(?: it)?(?: to)?|give(?: it)?(?: to)?|"
    r"reassign(?: it)?(?: to)?|swap(?: it)?(?: for)?|over to|hand(?: it)?(?: to)?)"
    r"\s+$", re.I)


def _positive_target(message: str, named: list[dict]) -> dict | None:
    """The operative the message asks for, as opposed to merely mentions."""
    low = _fold(message)
    best = None
    for r in named:
        name = _fold(r.get("name") or "")
        first, _, surname = name.partition(" ")
        for token in (name, surname, first):
            if not token:
                continue
            i = low.find(token)
            if i < 0:
                continue
            if _POSITIVE_CUE_RE.search(low[max(0, i - 24):i]):
                if best is None or i < best[0]:
                    best = (i, r)
                break
    return best[1] if best else None


async def _assign_directive(req: AgentChatRequest, ops: list, job: dict) -> Grounding:
    """Propose putting somebody on a job that already exists."""
    rows = [o.model_dump() for o in ops]
    named = _named_operators(req.message, rows)
    # A name can mean "give it to them" or "they cannot do it". Decide which
    # before treating anyone as the assignee.
    wanted = _positive_target(req.message, named)
    ruled_out: set[str] = set()
    if wanted is None and named and _EXCLUDE_RE.search(req.message):
        ruled_out = {r["employee_code"] for r in named}
        named = []
    elif _EXCLUDE_RE.search(req.message) and wanted is None:
        named = []
    if wanted is not None:
        named = [wanted]
    hours = float(job["estimated_hours"] or 8)
    parsed_day = _parse_day(_fold(req.message), date.today())
    day = parsed_day or (job["scheduled_start"].date() if job["scheduled_start"]
                         else date.today())

    warnings: list[str] = []
    if job["assigned_operative_id"] is not None:
        warnings.append(f"{job['work_order_number']} already has somebody on it; "
                        "confirming moves it.")

    if named:
        operative = named[0]
        slot, note = await _find_slot(operative["employee_code"], day, hours)
        if slot is None:
            return Grounding(
                prompt=(f"{operative['name']} has no free shift for "
                        f"{job['work_order_number']}. Say so in one sentence."),
                facts=(f"{operative['name']} has no free shift in the next three "
                       f"weeks that can take {hours:g}h."))
        day = slot["day"]
        if note:
            warnings.append(note)
    else:
        # Whoever is on it now is implicitly ruled out when the user is asking
        # for somebody else.
        if ruled_out or _EXCLUDE_RE.search(req.message):
            cur = job.get("assigned_operative_id")
            if cur is not None:
                here = next((r for r in rows if r.get("operative_id") == cur), None)
                code = next((r["employee_code"] for r in rows
                             if r.get("name") == (here or {}).get("name")), None)
                if code:
                    ruled_out.add(code)
        operative, why = await _pick_operator(job, rows, day, hours,
                                              exclude=ruled_out)
        if operative is not None:
            picked, _ = await _find_slot(operative["employee_code"], day, hours)
            if picked:
                day = picked["day"]
        if operative is None:
            return Grounding(
                prompt=(f"Nobody can take {job['work_order_number']}: {why}. Say so "
                        "in one sentence and suggest another day."),
                facts=f"Cannot assign {job['work_order_number']} — {why}.")
        warnings.append(why)
        if ruled_out:
            names = ", ".join(sorted(
                r["name"] for r in rows if r["employee_code"] in ruled_out))
            warnings.append(f"Ruled out: {names}.")

    slot, _ = await _find_slot(operative["employee_code"], day, hours)
    start = datetime.combine(day, clock_time(hour=slot["hour"] if slot else 7),
                             tzinfo=timezone.utc)
    params = {"work_order_number": job["work_order_number"],
              "assigned_operative_code": operative["employee_code"],
              "scheduled_start": start.isoformat()}
    fields = [
        {"label": "Work order", "value": f"{job['work_order_number']} — {job['title']}"},
        {"label": "Turbine", "value": f"{job['turbine_code']} — {job['site_name']}"},
        {"label": "Assign to", "value": f"{operative['name']} ({operative['skill_level']})"},
        {"label": "Starts", "value": start.strftime("%a %d %b, %H:%M")},
        {"label": "Estimate", "value": f"{hours:g}h"},
    ]
    summary = (f"Put {operative['name']} on {job['work_order_number']} "
               f"({job['title']}) on {start:%a %d %b}?")
    card = await actions.record(req.session_id, "assign_work", params, req.message,
                                summary, fields, warnings)
    facts = summary + "\n" + "\n".join(f"- {f['label']}: {f['value']}" for f in fields)
    if warnings:
        facts += "\n" + "\n".join(f"- Note: {w}" for w in warnings)
    facts += "\nNothing is booked until you confirm."
    return Grounding(
        prompt=("The user asked to put somebody on an existing job. The proposal is "
                "already shown to them with Confirm and Cancel buttons. Write ONLY a "
                "one-sentence lead-in telling them to check it. Do not restate the "
                "details and do not claim it is booked.\n\n"
                + json.dumps(params, default=str, ensure_ascii=False)),
        facts=facts, card=card, site=job["site_code"])


async def _find_slot(code: str, wanted: date, hours: float) -> tuple[dict | None, str | None]:
    """First day on or after `wanted` the operative can absorb the job."""
    schedule = await operators.operative_availability(days=21, code=code, start=wanted)
    if not schedule:
        return None, None
    for day in schedule[0].days:
        if day.working and day.free_hours >= min(hours, day.capacity_hours):
            note = None
            if day.day != wanted:
                note = (f"{wanted:%a %d %b} does not work — "
                        f"moved to {day.day:%a %d %b}.")
            start_hour = int(day.window[:2]) if day.window else 7
            return {"day": day.day, "hour": start_hour}, note
    return None, None


_PLACE_RE = re.compile(
    r"\b(?:in|at|for|near|around)\s+"
    r"(?P<place>[A-Za-zÀ-ÿ0-9'’.\-]+(?:[ \-][A-Za-zÀ-ÿ0-9'’.\-]+){0,3})",
    re.IGNORECASE,
)
_PLACE_STOP = re.compile(
    r"\s+(?:right now|now|today|tonight|tomorrow|currently|please|"
    r"at the moment|this (?:morning|afternoon|evening))\b.*$",
    re.IGNORECASE,
)

_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
    67: "heavy freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "light rain showers", 81: "rain showers",
    82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}

_weather_cache: dict[str, tuple[float, dict]] = {}


def _completes_pending(req: AgentChatRequest) -> bool:
    """A bare follow-up like "tomorrow" belongs to the request it answers.

    Only claimed when the message actually supplies a missing slot, so an
    unrelated question mid-conversation is not hijacked by a stale request.
    """
    sid = req.session_id
    if not sid or sid not in _PENDING:
        return False
    ts, prior = _PENDING[sid]
    if time.monotonic() - ts > _PENDING_TTL:
        _PENDING.pop(sid, None)
        return False
    parsed = _parse_schedule(req.message, date.today())
    adds_day = parsed["day"] is not None and prior.get("day") is None
    adds_turbine = parsed["turbine_code"] is not None and prior.get("turbine_code") is None
    return bool(adds_day or adds_turbine)


def _wants_schedule(message: str) -> bool:
    text = (message or "").strip()
    if _QUESTION_RE.match(text):
        return False
    if _NOT_WORK_RE.search(text) and not _TURBINE_RE.search(text):
        return False
    return bool(_SCHEDULE_RE.search(text))


def _extract_place(message: str) -> str | None:
    m = _PLACE_RE.search(message or "")
    if not m:
        return None
    return _PLACE_STOP.sub("", m.group("place")).strip(" ?.,!'\"") or None


def _match_site(place: str, sites: list[dict]) -> dict | None:
    low = place.lower()
    for s in sites:
        for field in ("site_name", "site_code", "nearest_city"):
            if (s.get(field) or "").lower() == low:
                return s
    for s in sites:
        name = (s.get("site_name") or "").lower()
        if name and (low in name or name in low):
            return s
    return None


async def _weather_lookup(place: str, sites: list[dict]) -> dict | None:
    key = place.strip().lower()
    hit = _weather_cache.get(key)
    if hit and time.monotonic() - hit[0] < settings.weather_cache_ttl_seconds:
        return hit[1]

    site = _match_site(place, sites)
    try:
        async with httpx.AsyncClient(timeout=settings.weather_http_timeout) as client:
            if site and site.get("latitude") is not None:
                lat, lon = float(site["latitude"]), float(site["longitude"])
                label = " · ".join(str(p) for p in (site.get("site_name"),
                                   site.get("nearest_city"), site.get("country")) if p)
            else:
                resp = await client.get(settings.open_meteo_geocoding_url, params={
                    "name": place, "count": 1, "language": "en", "format": "json"})
                resp.raise_for_status()
                results = (resp.json() or {}).get("results") or []
                if not results:
                    logger.info("weather tool | place=%r -> not found", place)
                    return None
                top = results[0]
                lat, lon = top.get("latitude"), top.get("longitude")
                label = ", ".join(str(p) for p in (top.get("name"),
                                  top.get("admin1"), top.get("country")) if p)

            resp = await client.get(settings.open_meteo_url, params={
                "latitude": lat, "longitude": lon,
                "current": ("temperature_2m,apparent_temperature,relative_humidity_2m,"
                            "precipitation,weather_code,wind_speed_10m,"
                            "wind_gusts_10m,cloud_cover"),
                "wind_speed_unit": "ms", "timezone": "auto"})
            resp.raise_for_status()
            cur = (resp.json() or {}).get("current") or {}
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        logger.warning("weather tool | place=%r failed: %s", place, exc)
        return None

    code = cur.get("weather_code")
    out = {
        "place": label,
        "local_time": cur.get("time"),
        "conditions": _WMO.get(code) if code is not None else None,
        "temperature_c": cur.get("temperature_2m"),
        "feels_like_c": cur.get("apparent_temperature"),
        "humidity_pct": cur.get("relative_humidity_2m"),
        "precipitation_mm": cur.get("precipitation"),
        "cloud_cover_pct": cur.get("cloud_cover"),
        "wind_ms": cur.get("wind_speed_10m"),
        "gust_ms": cur.get("wind_gusts_10m"),
        "source": "Open-Meteo",
    }
    _weather_cache[key] = (time.monotonic(), out)
    logger.info("weather tool | place=%r -> %s, %s°C, %s",
                place, out["place"], out["temperature_c"], out["conditions"])
    return out


def _render_weather(d: dict) -> str:
    bits = []
    if d.get("temperature_c") is not None:
        t = f"{d['temperature_c']:.0f}°C"
        if d.get("feels_like_c") is not None:
            t += f" (feels {d['feels_like_c']:.0f}°C)"
        bits.append(t)
    for key, fmt in (("conditions", "{}"), ("wind_ms", "wind {:.1f} m/s"),
                     ("gust_ms", "gusting {:.1f} m/s"),
                     ("humidity_pct", "{:.0f}% humidity"),
                     ("cloud_cover_pct", "{:.0f}% cloud"),
                     ("precipitation_mm", "{:.1f} mm precipitation")):
        if d.get(key) is not None:
            bits.append(fmt.format(d[key]))
    return _join(d.get("place") or "there", ", ".join(bits))


def _weather_directive(place: str | None, data: dict | None) -> Grounding:
    """Live observation, rendered in code so the model never states a number."""
    if not data:
        where = f" for {place}" if place else ""
        return Grounding(
            prompt=(f"The user asked about the weather{where}. The weather service "
                    "could not resolve that location. Say you could not find that "
                    "place and ask which city or site they mean. Do NOT state any "
                    "temperature or conditions, and do NOT say you lack real-time "
                    "weather access."),
            facts=(f"I could not find {place}." if place
                   else "I could not work out which place you mean."))
    return Grounding(
        prompt=("The user asked about the weather. The current observation is "
                "already written out below your reply, so write ONLY a one-sentence "
                "lead-in. Do not repeat the numbers and never say you lack "
                "real-time data.\n\nObservation (already displayed):\n"
                + json.dumps(data, default=str, ensure_ascii=False)),
        facts="Current weather — " + _render_weather(data))


_SOUL_FALLBACK = (
    "I am Pangea, the operations assistant for the Pangea Energy and Power "
    "fleet. I can read live site data, alerts, work orders and the operator "
    "roster, but I cannot act on the fleet myself."
)


@lru_cache(maxsize=1)
def _soul() -> str:
    try:
        text = Path(settings.soul_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("soul.md unreadable at %s (%s); using fallback",
                       settings.soul_path, exc)
        return _SOUL_FALLBACK
    return text or _SOUL_FALLBACK


def _identity_directive(req: AgentChatRequest) -> str:
    return (
        "The user asked about you. The document below is who you are; treat it "
        "as true about yourself and speak in the first person. Answer their "
        "question from it in two or three sentences, using whichever part of "
        "it their question is about. Do not repeat the opening paragraph when "
        "they asked about something else, do not offer further help, do not "
        "quote the headings, do not list the sites unless they asked which "
        "sites you cover, and do not mention this document.\n\n"
        + _soul()
    )


def _site_facts(snap: dict) -> list[str]:
    """Deterministic site summary: work the snapshot into finished sentences here
    so the model only has to join them up, never read the nested JSON."""
    name = snap.get("name") or "The site"
    out, cap = snap.get("live_output_mw"), snap.get("rated_capacity_mw")
    facts = []

    if out is not None and cap:
        facts.append(f"{name} is generating {out} MW of its {cap} MW capacity "
                     f"({round(out / cap * 100)}% of rated).")
    elif out is not None:
        facts.append(f"{name} is generating {out} MW.")

    t = snap.get("turbines") or {}
    if t.get("total"):
        facts.append(f"{t.get('operational') or 0} of {t['total']} turbines are "
                     f"running, {t.get('maintenance') or 0} in maintenance and "
                     f"{t.get('offline') or 0} offline.")

    if snap.get("shutdown"):
        facts.append(f"The site is shut down for high wind at "
                     f"{snap.get('wind_ms')} m/s against a "
                     f"{snap.get('cutout_ms')} m/s cut-out.")
    elif snap.get("wind_ms") is not None:
        facts.append(f"Wind is {snap['wind_ms']} m/s, gusting "
                     f"{snap.get('gust_ms')} m/s.")

    if snap.get("cloud_cover_pct") is not None:
        facts.append(f"Cloud cover is {snap['cloud_cover_pct']}%.")

    alerts = snap.get("open_alerts") or []
    if alerts:
        facts.append("Open alerts: " + "; ".join(
            f"{a.get('title')} ({a.get('severity')}"
            + (f" on {a['turbine']}" if a.get("turbine") else "") + ")"
            for a in alerts) + ".")
    else:
        facts.append("There are no open alerts.")

    work = snap.get("scheduled_work") or []
    if work:
        facts.append("Scheduled work: " + "; ".join(
            f"{w.get('title')} ({w.get('priority')} priority, {w.get('status')}, "
            + (w["assigned_operative"] if w.get("assigned_operative") else "unassigned")
            + ")" for w in work) + ".")
    else:
        facts.append("No maintenance is scheduled.")

    return facts


def _site_summary_directive(snap: dict) -> str:
    facts = _site_facts(snap)
    logger.info("site summary | site=%s -> %d fact(s)", snap.get("code"), len(facts))
    return (
        f"The user asked about {snap.get('name')}. Below are the facts for that "
        "site, already worked out for you. Report every one of them as a short "
        "paragraph in your own words, keeping the numbers exactly as written. Do "
        "not add anything, do not leave anything out, and do not mention any "
        "other site.\n"
        + "\n".join("- " + f for f in facts)
    )


async def _weather_route(req: AgentChatRequest, matched_by: str) -> Grounding:
    with tracing.span("weather", tracing.TOOL) as trace_span:
        place = _extract_place(req.message)
        if not place and req.ui_context:
            open_site = req.ui_context.get("open_site") or {}
            place = open_site.get("city") or open_site.get("name")
        trace_span.set_inputs({"place": place, "matched_by": matched_by})
        data = await _weather_lookup(
            place, await _load_site_reference()) if place else None
        trace_span.set_outputs({"resolved": data is not None, "observation": data})
    return _weather_directive(place, data)


async def _resolve_context(
    req: AgentChatRequest,
) -> tuple[dict | None, dict | None, Grounding | None]:
    context = req.ui_context
    route_info = None
    directive = None

    # Regex, not laya: proposing a write on a misread question is worse than
    # missing one.
    if (_wants_schedule(req.message) or _UNASSIGN_RE.search(req.message)
            or _completes_pending(req)):
        # A job that already exists gets assigned, not duplicated: a work order
        # number, or an unassigned order on a turbine the user named.
        job = await _open_work_order(req.message)
        # "unassign it and put Lukas on" is a move, not a removal: naming a
        # replacement outranks the unassign verb.
        replacement = None
        if job:
            roster = await operators.available_operators(include_on_leave=True)
            replacement = _positive_target(
                req.message, _named_operators(req.message,
                                              [o.model_dump() for o in roster]))
        taking_off = bool(job and _UNASSIGN_RE.search(req.message)
                          and replacement is None)
        tool = ("unassign_work" if taking_off
                else "assign_work" if job else "schedule_work")
        route_info = {"tool": tool, "tool_confidence": 1.0, "site": None}
        tracing.tag(tool=tool, routed_by="regex")
        with tracing.span(tool, tracing.TOOL) as trace_span:
            trace_span.set_inputs({"message": req.message,
                                   "work_order": job["work_order_number"] if job else None})
            # On-leave staff included so a named absentee is reported rather
            # than quietly dropped.
            ops = roster if job else await operators.available_operators(
                include_on_leave=True)
            directive = (await _unassign_directive(req, dict(job)) if taking_off
                         else await _assign_directive(req, ops, dict(job)) if job
                         else await _schedule_directive(req, ops))
            route_info["site"] = directive.site
            trace_span.set_outputs({"action_id": directive.card.action_id
                                    if directive.card else None,
                                    "site": directive.site})
        return context, route_info, directive

    if req.ui_context:
        route_info = await laya.route(_route_state(req), req.ui_context)
        if route_info and route_info.get("about_self", 0.0) >= settings.laya_about_self_floor:
            tracing.tag(tool="identity", routed_by="laya",
                        about_self=round(route_info["about_self"], 3))
            with tracing.span("identity", tracing.TOOL) as trace_span:
                trace_span.set_inputs({"about_self": route_info["about_self"]})
                directive = Grounding(_identity_directive(req))
                trace_span.set_outputs({"soul_chars": len(_soul())})
            return context, route_info, directive
        if route_info and route_info["tool"] == "weather":
            tracing.tag(tool="weather", routed_by="laya",
                        confidence=round(route_info["tool_confidence"], 3))
            directive = await _weather_route(req, "laya")
            # Weather at one of our own wind sites: add the hub-height reading
            # and the cut-out state, which the general forecast cannot give.
            site = route_info.get("site")
            if site and site != "any":
                gust = await _wind_directive(req, site)
                if gust.facts:
                    directive = _merge(directive, gust)
            return context, route_info, directive
        if (route_info and route_info["tool"] and route_info["tool"] != "none"
                and route_info["tool_confidence"] >= settings.laya_confidence_floor):
            with tracing.span(route_info["tool"], tracing.TOOL) as trace_span:
                trace_span.set_inputs({"site": route_info["site"],
                                       "confidence": route_info["tool_confidence"]})
                if route_info["tool"] == "wind_status":
                    directive = await _wind_directive(req, route_info["site"])
                elif route_info["tool"] == "solar_forecast":
                    directive = await _solar_directive(req, route_info["site"])
                elif route_info["tool"] == "wave_forecast":
                    directive = await _wave_directive(req, route_info["site"])
                elif route_info["tool"] == "fleet_totals":
                    directive = await _fleet_totals_directive(req)
                elif route_info["tool"] == "active_alerts":
                    directive = await _alerts_directive(req)
                elif route_info["tool"] == "maintenance_tasks":
                    directive = await _maintenance_directive(req)
                elif route_info["tool"] == "operators":
                    ops = await operators.available_operators()
                    directive = _operators_directive(req, ops)
                else:
                    context = laya.select_context(
                        req.ui_context, route_info["tool"], route_info["site"])
                trace_span.set_outputs({"directive": tracing.clip(directive.prompt)
                                        if directive else None,
                                        "grounded_facts": bool(
                                            directive and directive.facts),
                                        "context_keys": sorted(context or {})})
            logger.info(
                "agent grounding | tool=%s conf=%.3f >= floor=%.2f -> using tool context",
                route_info["tool"], route_info["tool_confidence"],
                settings.laya_confidence_floor)
        elif route_info:
            reason = ("no tool matched" if route_info["tool"] in (None, "none")
                      else "conf %.3f < floor %.2f" % (route_info["tool_confidence"],
                                                       settings.laya_confidence_floor))
            logger.info("agent grounding | tool=%s (%s) -> full ui_context fallback",
                        route_info["tool"], reason)
        if route_info:
            tracing.tag(tool=route_info["tool"], site=route_info["site"],
                        confidence=round(route_info["tool_confidence"], 3),
                        routed_by="laya",
                        below_floor=route_info["tool_confidence"]
                        < settings.laya_confidence_floor)
    if directive is None and (req.ui_context or {}).get("open_site"):
        snap = req.ui_context["open_site"]
        tracing.tag(tool="site_summary", site=snap.get("code"))
        with tracing.span("site_summary", tracing.TOOL) as trace_span:
            trace_span.set_inputs({"site": snap.get("code")})
            directive = Grounding(_site_summary_directive(snap))
            trace_span.set_outputs({"directive": tracing.clip(directive.prompt)})
        logger.info("agent grounding | open_site=%s -> site summary directive",
                    snap.get("code"))
        return context, route_info, directive

    if (directive is None and context is not req.ui_context and req.ui_context
            and req.ui_context.get("active_alerts") is not None):
        context = {**context, "active_alerts": req.ui_context["active_alerts"]}
    return context, route_info, directive


def _build_payload(req: AgentChatRequest, context: dict | None,
                   site_ref: list[dict] | None, stream: bool,
                   directive: Grounding | None = None) -> dict:
    lead_in_only = bool(directive and directive.facts)
    return {
        "model": settings.agent_llm_model,
        "messages": _build_messages(req, context, site_ref,
                                    directive.prompt if directive else None),
        "max_tokens": (_LEAD_IN_MAX_TOKENS if lead_in_only
                       else settings.agent_max_tokens),
        "temperature": settings.agent_temperature,
        "frequency_penalty": settings.agent_frequency_penalty,
        "presence_penalty": settings.agent_presence_penalty,
        "repetition_penalty": settings.agent_repetition_penalty,
        "stream": stream,
    }


@router.get("/health")
async def health() -> dict:
    """Report whether the LLM endpoint is reachable, for the UI status dot."""
    models_url = settings.agent_llm_url.replace("/chat/completions", "/models")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(models_url)
            resp.raise_for_status()
    except httpx.HTTPError:
        return {"online": False, "model": settings.agent_llm_model}
    return {"online": True, "model": settings.agent_llm_model}


def _trace_request(req: AgentChatRequest) -> dict:
    return {
        "message": req.message,
        "history_turns": len(req.history or []),
        "ui_context": tracing.context_summary(req.ui_context),
    }


async def _call_llm(payload: dict) -> dict:
    with tracing.span("granite_chat", tracing.LLM) as trace_span:
        trace_span.set_inputs({"model": payload.get("model"),
                               "messages": payload.get("messages"),
                               "temperature": payload.get("temperature"),
                               "max_tokens": payload.get("max_tokens")})
        try:
            async with httpx.AsyncClient(timeout=settings.agent_http_timeout) as client:
                resp = await client.post(settings.agent_llm_url, json=payload)
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as exc:
            trace_span.set_outputs({"error": str(exc)})
            raise HTTPException(
                status_code=503, detail="Assistant model unavailable.") from exc
        try:
            content = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            content = ""
        trace_span.set_outputs({"content": content, "usage": body.get("usage")})
        return body


@router.post("/chat", response_model=AgentChatResponse)
async def chat(req: AgentChatRequest) -> AgentChatResponse:
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message.")

    with tracing.span("agent_chat", tracing.AGENT) as root:
        tracing.tag(session_id=req.session_id, endpoint="chat")
        root.set_inputs(_trace_request(req))

        context, route_info, directive = await _resolve_context(req)
        facts = directive.facts if directive else None
        card = directive.card if directive else None

        # A grounded tool has already written the answer. Calling the model to
        # introduce it only ever added a sentence that contradicted it.
        if facts:
            root.set_outputs({"reply": facts, "model": None, "route": route_info,
                              "grounded_facts": True, "lead_in": False,
                              "action_id": card.action_id if card else None})
            return AgentChatResponse(reply=facts, model=None, route=route_info,
                                     action=card)

        site_ref = await _load_site_reference()
        payload = _build_payload(req, context, site_ref, stream=False, directive=directive)
        try:
            body = await _call_llm(payload)
        except HTTPException:
            if not facts:
                raise
            logger.warning("agent grounding | LLM unreachable, serving facts alone")
            root.set_outputs({"reply": facts, "model": None, "route": route_info,
                              "grounded_facts": True, "lead_in": False})
            return AgentChatResponse(reply=facts, model=None, route=route_info,
                                     action=card)

        try:
            reply = (body["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            reply = ""
        if facts:
            reply = _with_facts(reply, facts)
        root.set_outputs({"reply": reply, "model": body.get("model"),
                          "route": route_info, "grounded_facts": bool(facts),
                          "action_id": card.action_id if card else None})
    return AgentChatResponse(reply=reply, model=body.get("model"),
                             route=route_info, action=card)


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


async def _stream_reply(req: AgentChatRequest) -> AsyncIterator[str]:
    with tracing.span("agent_chat", tracing.AGENT) as root:
        tracing.tag(session_id=req.session_id, endpoint="chat_stream")
        root.set_inputs(_trace_request(req))

        try:
            context, route_info, directive = await _resolve_context(req)
            site_ref = await _load_site_reference()
            payload = _build_payload(req, context, site_ref, stream=True,
                                     directive=directive)
        except Exception as exc:
            logger.exception("stream setup failed")
            root.set_outputs({"error": str(exc)})
            yield _sse({"error": "Assistant unavailable."})
            yield _sse({"done": True})
            return

        if route_info:
            yield _sse({"route": route_info})

        # With facts in hand the model writes only the lead-in, so its tokens
        # are buffered: the reply is sanitised before any of it is shown.
        facts = directive.facts if directive else None
        card = directive.card if directive else None

        # Grounded answers are already written; the model is not involved.
        if facts:
            for line in facts.splitlines(keepends=True):
                yield _sse({"delta": line})
            if card:
                yield _sse({"card": card.model_dump()})
            yield _sse({"done": True})
            root.set_outputs({"reply": facts, "route": route_info, "error": None,
                              "grounded_facts": True,
                              "action_id": card.action_id if card else None})
            return

        chunks: list[str] = []
        error: str | None = None
        with tracing.span("granite_chat", tracing.LLM) as llm_span:
            llm_span.set_inputs({"model": payload.get("model"),
                                 "messages": payload.get("messages"),
                                 "temperature": payload.get("temperature"),
                                 "max_tokens": payload.get("max_tokens"),
                                 "stream": True})
            try:
                async with httpx.AsyncClient(timeout=settings.agent_http_timeout) as client:
                    async with client.stream("POST", settings.agent_llm_url,
                                             json=payload) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                delta = json.loads(data)["choices"][0]["delta"].get("content")
                            except (KeyError, IndexError, ValueError, TypeError):
                                continue
                            if delta:
                                chunks.append(delta)
                                if not facts:
                                    yield _sse({"delta": delta})
            except httpx.HTTPError as exc:
                error = str(exc)
                if not facts:
                    yield _sse({"error": "Assistant model unavailable."})
                else:
                    logger.warning(
                        "agent grounding | LLM unreachable, serving facts alone")
            llm_span.set_outputs({"content": "".join(chunks), "error": error})

        if facts:
            # The card carries the detail, so stream only the lead-in.
            reply = _lead_in("".join(chunks)) if card else _with_facts(
                "".join(chunks), facts)
            if card and not reply:
                reply = "Here is what I would raise — check it over."
            for line in reply.splitlines(keepends=True):
                yield _sse({"delta": line})
        else:
            reply = "".join(chunks)

        if card:
            yield _sse({"card": card.model_dump()})

        yield _sse({"done": True})
        root.set_outputs({"reply": reply, "route": route_info, "error": error,
                          "grounded_facts": bool(facts),
                          "action_id": card.action_id if card else None})


@router.post("/chat/stream")
async def chat_stream(req: AgentChatRequest) -> StreamingResponse:
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message.")

    return StreamingResponse(
        _stream_reply(req),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
