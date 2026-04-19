"""
logging_setup.py — central structured-logging init.

Audit round-11 (MEDIUM item): every module was printing with no timestamp
or request context, so correlating a Sentry event with a stdout line
required guesswork. This module configures the stdlib ``logging`` package
with a JSON formatter + ISO-8601 ET timestamps, wires Sentry's
LoggingIntegration (already loaded via ``observability.py``) so
``logger.error(...)`` events flow into Sentry automatically, and exposes
the usual ``getLogger(__name__)`` surface for callers.

Migration is intentionally incremental. Newly-touched modules should use
``logger = logging.getLogger(__name__)``. Existing modules still using
``print(..., flush=True)`` keep working — their output lands in the same
stdout stream, just without JSON envelope. Phase 2 of the migration will
sweep the remaining ~400 print calls (strategies, one-shot tools).

Public API:

    init(level: str = "INFO") -> None
        Idempotent. Call as early as possible at process boot.

    json_line(record: logging.LogRecord) -> str
        Exposed for testing.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    from datetime import timezone as _tz
    _ET = _tz.utc  # fall back; prod always has zoneinfo


_INITIALIZED = False


# Prefixes we consider "warning / error" when auto-classifying the level of
# a routed print() call. The existing codebase uses these consistently —
# `[ERROR ...]`, `[WARN ...]`, `[FATAL ...]` — so matching on the first
# ~40 characters is a reliable proxy for the caller's intended severity.
_WARN_PREFIXES = ("[WARN", "[WARNING", "WARN:", "WARNING:")
_ERROR_PREFIXES = ("[ERROR", "[FATAL", "[CRITICAL", "ERROR:", "FATAL:")


def _classify_level(msg: str):
    """Pick WARNING/ERROR/INFO for a routed print based on common prefixes."""
    head = msg.lstrip()[:40].upper()
    if head.startswith(_ERROR_PREFIXES):
        return logging.ERROR
    if head.startswith(_WARN_PREFIXES):
        return logging.WARNING
    return logging.INFO


class _JsonFormatter(logging.Formatter):
    """Single-line JSON log envelope.

    Fields emitted on every record:
      ts      — ISO-8601 ET timestamp, millisecond precision
      level   — INFO / WARNING / ERROR / DEBUG
      logger  — qualified module name
      msg     — formatted message
      exc     — traceback string (only if ``exc_info`` provided)

    Extra context attached via ``logger.info("x", extra={"k": "v"})``
    is merged into the top-level object.
    """

    # Standard LogRecord attributes that must not be re-serialised as
    # "extra" keys — would duplicate fields and blow up cardinality in
    # log-aggregation tools.
    _RESERVED = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=_ET).isoformat(timespec="milliseconds")
        payload = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Merge extras the caller passed via logger.X("msg", extra={...}).
        for k, v in record.__dict__.items():
            if k in self._RESERVED or k.startswith("_"):
                continue
            # Best-effort stringify so json.dumps never fails — a bad
            # log line is still better than a crash.
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        return json.dumps(payload, ensure_ascii=False)


def json_line(record: logging.LogRecord) -> str:
    """Test hook — returns the same JSON envelope the handler emits."""
    return _JsonFormatter().format(record)


def init(level: str | None = None) -> None:
    """Install the JSON handler on the root logger. Idempotent."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    lvl_name = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    lvl = getattr(logging, lvl_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(lvl)
    # Strip default handlers that basicConfig or stdlib imports may have
    # added so we don't double-log. Safe because nothing else should have
    # configured the root logger this early in boot.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.setLevel(lvl)
    root.addHandler(handler)

    # Silence a couple of chatty libraries that default to INFO on their
    # own loggers — they'll still emit WARNING+ into our JSON stream.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    # Phase 2 of the logging migration: route every remaining `print(...)`
    # call through the logger automatically. This avoids a 400-site manual
    # sweep across strategies/one-shots (learn.py, update_dashboard.py,
    # update_scorecard.py, wheel_strategy.py, smart_orders.py, and ~30
    # others) by intercepting at the builtin level. Explicit `log.info()`
    # calls in server.py / auth.py / observability.py / handlers/*.py
    # remain unchanged (those went through proper migration in the
    # previous PR).
    _patch_builtin_print()

    _INITIALIZED = True


def _patch_builtin_print():
    """Replace builtins.print with one that routes through the logger.

    Routing rules:
      - If caller passed `file=<anything other than stdout/stderr>`, delegate
        to the real print (e.g. writing to a file object for reports).
      - Otherwise emit through `logging.getLogger(caller_module_name)`.
      - Level inferred from `[ERROR ...]` / `[WARN ...]` prefixes, defaults
        to INFO (matches 95%+ of current call sites).
      - If caller passed `file=sys.stderr`, minimum level is WARNING even
        if the message text doesn't indicate severity.

    Idempotent — subsequent calls are no-ops so re-init is safe.
    Exposes the original on `builtins._orig_print` for any caller that
    needs to bypass the shim.
    """
    import builtins
    import sys as _sys

    if getattr(builtins, "_print_shim_installed", False):
        return

    _orig_print = builtins.print
    # Cache logger lookups so repeated prints from the same module don't
    # re-traverse logging's hierarchy each time.
    _logger_cache: dict = {}

    def _patched(*args, sep=" ", end="\n", file=None, flush=False, **kwargs):
        # Escape hatch for writes to actual file objects.
        if file is not None and file is not _sys.stdout and file is not _sys.stderr:
            return _orig_print(*args, sep=sep, end=end, file=file, flush=flush, **kwargs)
        try:
            msg = sep.join(str(a) for a in args).rstrip("\n")
        except Exception:
            return _orig_print(*args, sep=sep, end=end, file=file, flush=flush, **kwargs)
        # Caller frame → module name for logger hierarchy.
        try:
            frame = _sys._getframe(1)
            mod_name = frame.f_globals.get("__name__", "root")
        except Exception:
            mod_name = "root"
        logger = _logger_cache.get(mod_name)
        if logger is None:
            logger = logging.getLogger(mod_name)
            _logger_cache[mod_name] = logger
        level = _classify_level(msg)
        if file is _sys.stderr and level < logging.WARNING:
            level = logging.WARNING
        logger.log(level, msg)

    builtins._orig_print = _orig_print  # type: ignore[attr-defined]
    builtins.print = _patched           # type: ignore[assignment]
    builtins._print_shim_installed = True  # type: ignore[attr-defined]
