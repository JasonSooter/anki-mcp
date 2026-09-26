"""OTLP telemetry, e.g. to a Grafana Cloud stack.

Logs go out over OTLP, land in Loki, and every structured field is queryable
as structured metadata, so one set of queries covers every service that ships
the same shape --

    {service_name="anki-mcp", deployment_environment_name="production"}
      | tool="add_note" | status="error"

Configuration is the standard OTel environment: OTEL_EXPORTER_OTLP_ENDPOINT and
OTEL_EXPORTER_OTLP_HEADERS. With no endpoint set, telemetry is a no-op and the
server logs to stdout as usual -- the collection must stay servable whether or
not Grafana is reachable.
"""

from __future__ import annotations

import functools
import logging
import os
import time
from typing import Any, Awaitable, Callable, TypeVar

log = logging.getLogger(__name__)

_enabled = False
_provider: Any = None

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

# Emitted as the log body; the dashboard counts these by name.
EVENT_TOOL_CALL = "tool call"
EVENT_AUTH_FAILURE = "auth failure"
EVENT_LOGIN = "oauth login"
EVENT_MUTATION = "collection mutation"
EVENT_SYNC = "ankiweb sync"
EVENT_COLLECTION_STATS = "collection stats"
EVENT_STARTUP = "server started"


def setup(*, service_name: str, environment: str, version: str) -> bool:
    """Wire OTLP log export, if an endpoint is configured. Returns enabled."""
    global _enabled, _provider

    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        log.info("OTEL_EXPORTER_OTLP_ENDPOINT not set; telemetry disabled")
        return False

    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        log.warning("opentelemetry packages missing; telemetry disabled")
        return False

    try:
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": version,
                # The dashboard's $env template variable filters on this.
                "deployment.environment.name": environment,
            }
        )
        _provider = LoggerProvider(resource=resource)
        _provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))

        # Attaching to the root logger means every existing log.info/exception in
        # this codebase ships too, without threading a telemetry object through
        # modules that have no other reason to know about it.
        handler = LoggingHandler(level=logging.INFO, logger_provider=_provider)
        logging.getLogger().addHandler(handler)
        _enabled = True
        log.info("telemetry enabled: service=%s env=%s", service_name, environment)
        return True
    except Exception:
        # Never let an observability problem stop the server from serving.
        log.exception("could not initialise telemetry; continuing without it")
        return False


def shutdown() -> None:
    """Flush pending log records. Best effort."""
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception:
            log.exception("telemetry shutdown failed")


# Keys logging refuses to accept in `extra`, because a LogRecord already owns
# them -- `created` is the record's own timestamp, `module` its module. Rather
# than shadowing one, makeRecord raises KeyError, so an innocent attribute name
# can take down the call it was only meant to describe. Derived from a real
# record instead of hardcoded, so a Python that adds one (3.12 added
# `taskName`) is covered without an edit here.
_RESERVED_LOGRECORD_KEYS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime"}
# Collisions are renamed rather than dropped: the value is usually the point of
# the event, and a silently missing attribute is harder to spot on a dashboard
# than an oddly-named one.
_RESERVED_PREFIX = "attr_"


def emit(event: str, *, level: int = logging.INFO, **attributes: Any) -> None:
    """Emit one structured event.

    Attributes become Loki structured metadata, so keep the keys stable -- the
    dashboard queries them by name. Values are scalars only; a dict or list
    would be flattened to a string and become useless to query.
    """
    # Booleans are stringified: Loki structured-metadata filters compare
    # strings, so `| mutation="true"` works uniformly while a real bool would
    # depend on how the backend renders it.
    clean = {
        _RESERVED_PREFIX + k if k in _RESERVED_LOGRECORD_KEYS else k: (
            str(v).lower() if isinstance(v, bool) else v
        )
        for k, v in attributes.items()
        if v is not None
    }
    try:
        logging.getLogger("anki_mcp.telemetry").log(level, event, extra=clean)
    except Exception:
        # A telemetry line must never be able to fail the tool it describes.
        # `created=` did exactly that: the deck was already made, and the caller
        # still got "Error executing tool create_deck" with no body.
        log.exception("could not emit %s; continuing", event)


def instrumented(name: str, *, mutation: bool = False) -> Callable[[F], F]:
    """Time a tool call and emit its outcome.

    Applied *under* @mcp.tool so the SDK still sees the real signature through
    functools.wraps -- the tool's JSON schema is generated from the wrapped
    function's annotations, and losing them would silently break the schema.
    """

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            status = "ok"
            error_kind = None
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                status = "error"
                error_kind = type(exc).__name__
                raise
            finally:
                emit(
                    EVENT_TOOL_CALL,
                    level=logging.ERROR if status == "error" else logging.INFO,
                    tool=name,
                    status=status,
                    error=error_kind,
                    mutation=mutation,
                    # Deck is the natural dimension for this collection: it is
                    # how the vocab work is actually partitioned.
                    deck=kwargs.get("deck"),
                    note_type=kwargs.get("note_type"),
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )

        return wrapper  # type: ignore[return-value]

    return decorate


def record_auth_failure(reason: str) -> None:
    """A request presented a token that did not verify.

    This is the security signal on a server whose one endpoint is public, so it
    is logged at WARNING and never includes any part of the presented token.
    """
    emit(EVENT_AUTH_FAILURE, level=logging.WARNING, reason=reason)


def record_login(*, success: bool, client_ip: str, reason: str | None = None) -> None:
    """An attempt at the OAuth login page.

    Failures here are the loudest signal the server has: the login page is the
    only thing guarding a public endpoint, so someone probing it matters. The
    submitted password and code are never recorded.
    """
    emit(
        EVENT_LOGIN,
        level=logging.INFO if success else logging.WARNING,
        status="ok" if success else "failed",
        client_ip=client_ip,
        reason=reason,
    )
