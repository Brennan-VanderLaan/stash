"""Observability — structured logging + request context.

Spec § "Logging & observability".  Every log line carries enough
context that a single grep tells the story of a request:

* ``request_id`` — short uuid stamped by :func:`bind_request_id`.
* ``actor_email`` and ``tenant_id`` — set by ``current_actor``
  middleware after identity resolves.
* ``surface`` — which API surface group ran (``ai``, ``upload``,
  ``core``, ``admin``).
* ``layer`` — which architectural layer emitted the log
  (``route``, ``dao``, ``vision``, ``backup``, ``quota``).

Implementation:

* ``contextvars`` carry the per-request context.  An async-safe
  ``ContextVar`` is the right primitive — FastAPI's threadpool
  executor for sync routes preserves it, and async routes get it
  for free.
* :func:`get_logger` returns a :class:`logging.LoggerAdapter` keyed
  on ``layer``.  Each ``log`` call merges current contextvars into
  ``extra`` so the formatter can stamp them.
* JSON output in production (one line per record, structured fields
  alongside the message) and a pretty key=value format in dev.
  Toggle via ``STASH_LOG_FORMAT=json`` (default ``pretty``).
* Audit-worthy events still write to ``audit_log`` separately —
  this module is the *operational* trail; the audit log is the
  *user-visible* one.

Sentry / external aggregators are deferred per spec.  The JSON
output is exactly the shape that aggregator would consume.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone


# ── Context ─────────────────────────────────────────────────────────


_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None,
)
_actor_email: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "actor_email", default=None,
)
_tenant_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "tenant_id", default=None,
)
_surface: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "surface", default=None,
)


def new_request_id() -> str:
    """Short id good enough for grep + correlation across log lines.
    First 8 hex chars of a uuid4 — collision rate is fine for the
    request-volume scale stash runs at.  If we ever need real
    uniqueness across distributed nodes we lengthen here."""
    return uuid.uuid4().hex[:8]


def bind_request_id(value: str) -> contextvars.Token:
    """Set the request_id contextvar.  Returns the token so the
    middleware can reset cleanly after the response (avoids leaking
    one request's id into another via reused threads)."""
    return _request_id.set(value)


def bind_actor(email: str | None, tenant_id: int | None) -> tuple[
    contextvars.Token, contextvars.Token,
]:
    """Set actor + tenant context together.  Returns both tokens so
    the middleware resets in reverse order."""
    return _actor_email.set(email), _tenant_id.set(tenant_id)


def bind_surface(value: str | None) -> contextvars.Token:
    return _surface.set(value)


def reset_tokens(*tokens: contextvars.Token | None) -> None:
    """Reset context tokens in reverse order so re-used worker
    threads don't carry one request's context into the next."""
    for token in reversed(tokens):
        if token is None:
            continue
        try:
            token.var.reset(token)
        except (LookupError, ValueError):
            pass


def current_context() -> dict:
    """Snapshot of the current contextvars — used by tests + the
    audit-log helper.  Excludes None-valued fields so the resulting
    dict isn't peppered with empties."""
    out: dict = {}
    for key, var in (
        ("request_id", _request_id),
        ("actor_email", _actor_email),
        ("tenant_id", _tenant_id),
        ("surface", _surface),
    ):
        val = var.get()
        if val is not None:
            out[key] = val
    return out


# ── Logger ──────────────────────────────────────────────────────────


class _ContextAdapter(logging.LoggerAdapter):
    """LoggerAdapter that merges the active contextvars into every
    record's ``extra``.  The ``layer`` field is set per-adapter at
    construction; everything else is per-request."""

    def __init__(self, logger: logging.Logger, layer: str) -> None:
        super().__init__(logger, extra={"layer": layer})
        self._layer = layer

    def process(self, msg, kwargs):
        # Merge the per-call ``extra`` (if any) over the request
        # context, then over the adapter's layer field.  Caller-
        # supplied ``extra`` always wins in case a caller wants to
        # override (e.g. logging an action against a *different*
        # tenant_id from the one the request is bound to).
        ctx = current_context()
        ctx["layer"] = self._layer
        extra = kwargs.get("extra") or {}
        ctx.update(extra)
        kwargs["extra"] = ctx
        return msg, kwargs


def get_logger(layer: str) -> _ContextAdapter:
    """Return the LoggerAdapter for a given architectural layer.
    Idempotent — repeated calls with the same layer return adapters
    over the same underlying logger so handler setup happens once.
    """
    base = logging.getLogger(f"stash.{layer}")
    return _ContextAdapter(base, layer=layer)


# ── Formatters ──────────────────────────────────────────────────────


# These are the fields we explicitly surface; anything else on the
# record (line numbers, module names, etc.) flows through the JSON
# formatter under their stdlib names.
_CONTEXT_FIELDS = ("request_id", "actor_email", "tenant_id",
                   "surface", "layer")


class _JsonFormatter(logging.Formatter):
    """One-line JSON per record — what an aggregator wants.  Drops
    None-valued context fields so a request that hasn't bound an
    actor yet doesn't litter the log with explicit nulls."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc,
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for f in _CONTEXT_FIELDS:
            val = getattr(record, f, None)
            if val is not None:
                payload[f] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class _PrettyFormatter(logging.Formatter):
    """Human-readable dev format.  Lays the context fields out as
    ``key=value`` pairs after the message so eyeballing is fast."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(
            record.created, tz=timezone.utc,
        ).isoformat(timespec="seconds")
        head = f"{ts} {record.levelname:<5} {record.name}"
        msg = record.getMessage()
        bits = []
        for f in _CONTEXT_FIELDS:
            val = getattr(record, f, None)
            if val is not None:
                bits.append(f"{f}={val}")
        suffix = (" " + " ".join(bits)) if bits else ""
        out = f"{head}: {msg}{suffix}"
        if record.exc_info:
            out += "\n" + self.formatException(record.exc_info)
        return out


# ── Setup ───────────────────────────────────────────────────────────


_INSTALLED = False


def setup_logging() -> None:
    """Wire the root ``stash`` logger.  Idempotent — repeated calls
    no-op so test fixtures re-importing app.py don't stack handlers.

    Format toggle via ``STASH_LOG_FORMAT``: ``json`` for production
    (one line per record, aggregator-ready) or ``pretty`` for dev
    (default).  Level via ``STASH_LOG_LEVEL`` (default INFO)."""
    global _INSTALLED
    if _INSTALLED:
        return
    fmt = os.environ.get("STASH_LOG_FORMAT", "pretty").lower()
    level_name = os.environ.get("STASH_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _JsonFormatter() if fmt == "json" else _PrettyFormatter()
    )

    root = logging.getLogger("stash")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Don't double-log via the root stdlib logger.  Uvicorn brings
    # its own access log for HTTP-level concerns; our records carry
    # the per-request context aggregators care about.
    root.propagate = False

    _INSTALLED = True


# ── Audit helper ────────────────────────────────────────────────────


def write_audit(
    conn,
    *,
    tenant_id: int | None,
    actor_email: str | None,
    action: str,
    target_kind: str | None = None,
    target_id: int | None = None,
    metadata: dict | None = None,
) -> None:
    """One canonical audit_log row writer.  Every DAO mutation that
    matters uses this so the trail has a uniform shape.  Caller
    supplies the connection so the audit row commits inside the same
    transaction as the mutation it's recording — a rolled-back
    mutation never leaves an orphan audit entry behind.

    Spec § "Audit log" — actions are short verb-noun strings
    (e.g. ``box.update``, ``share.create``).  Operator-on-tenant
    actions can use ``tenant_id=None`` for the global audit log
    pile; the column is nullable for that case."""
    conn.execute(
        "INSERT INTO audit_log "
        "(tenant_id, actor_email, action, target_kind, target_id, "
        " metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, actor_email, action, target_kind, target_id,
         json.dumps(metadata or {})),
    )
