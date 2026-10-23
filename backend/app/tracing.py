"""Optional MLflow tracing for the assistant path. Every helper degrades to a
no-op if the SDK is missing or the tracking server is unreachable."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import sys
from collections.abc import Iterator

from .config import settings

logger = logging.getLogger("pangea.tracing")

AGENT = "AGENT"
LLM = "LLM"
TOOL = "TOOL"
CHAT_MODEL = "CHAT_MODEL"

_mlflow = None


def _bearer_token() -> str:
    if settings.mlflow_tracking_token:
        return settings.mlflow_tracking_token
    if not settings.mlflow_token_file:
        return ""
    try:
        return pathlib.Path(settings.mlflow_token_file).read_text().strip()
    except OSError:
        return ""


def init() -> None:
    global _mlflow
    if not settings.mlflow_tracing_enabled:
        logger.info("mlflow tracing off: MLFLOW_TRACING_ENABLED=false")
        return
    if not settings.mlflow_tracking_uri:
        logger.info("mlflow tracing off: MLFLOW_TRACKING_URI is unset")
        return
    try:
        import mlflow
    except ImportError:
        logger.warning("mlflow tracing off: mlflow-tracing is not installed")
        return
    token = _bearer_token()
    if token:
        os.environ.setdefault("MLFLOW_TRACKING_TOKEN", token)
    else:
        logger.warning("mlflow tracing: no bearer token, expect an auth redirect")
    try:
        mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
        mlflow.set_experiment(settings.mlflow_experiment)
    except Exception as exc:
        logger.warning("mlflow tracing disabled, setup failed: %s",
                       clip(str(exc), 400))
        return
    with contextlib.suppress(Exception):
        mlflow.config.enable_async_logging()
    _mlflow = mlflow
    logger.info("mlflow tracing -> %s experiment=%s",
                mlflow.get_tracking_uri(), settings.mlflow_experiment)


def enabled() -> bool:
    return _mlflow is not None


class _NullSpan:
    def set_inputs(self, *_args, **_kwargs) -> None: ...
    def set_outputs(self, *_args, **_kwargs) -> None: ...
    def set_attribute(self, *_args, **_kwargs) -> None: ...
    def set_attributes(self, *_args, **_kwargs) -> None: ...


@contextlib.contextmanager
def span(name: str, span_type: str = "UNKNOWN") -> Iterator:
    if _mlflow is None:
        yield _NullSpan()
        return
    try:
        cm = _mlflow.start_span(name=name, span_type=span_type)
        active = cm.__enter__()
    except Exception as exc:
        logger.warning("trace span %r unavailable: %s", name, exc)
        yield _NullSpan()
        return
    try:
        yield active
    except BaseException:
        with contextlib.suppress(Exception):
            cm.__exit__(*sys.exc_info())
        raise
    else:
        with contextlib.suppress(Exception):
            cm.__exit__(None, None, None)


def tag(**tags) -> None:
    if _mlflow is None:
        return
    clean = {k: str(v) for k, v in tags.items() if v is not None}
    if not clean:
        return
    with contextlib.suppress(Exception):
        _mlflow.update_current_trace(tags=clean)


def clip(value, limit: int | None = None) -> str:
    limit = limit or settings.mlflow_trace_max_chars
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + f"…[+{len(text) - limit} chars]"


def context_summary(ui_context: dict | None) -> dict | None:
    if ui_context is None:
        return None
    if settings.mlflow_trace_ui_context:
        return ui_context
    open_site = ui_context.get("open_site") or {}
    return {
        "view": ui_context.get("view"),
        "at": ui_context.get("at"),
        "open_site": open_site.get("code"),
        "counts": {k: len(ui_context.get(k) or [])
                   for k in ("wind_sites", "solar_sites", "wave_sites",
                             "active_alerts", "maintenance_tasks")},
    }
