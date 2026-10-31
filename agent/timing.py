"""
Detailed step-by-step timing log, plus the error log.

One Timeline per request (text or voice). Each step is recorded with the time since the
request started and, for spans, how long it took. When the detailed log is enabled, steps are
printed to the console, written to logs/timing.log and streamed to the dashboard.

Errors (server exceptions and browser errors) always go to logs/errors.log with tracebacks,
whether or not the detailed log is on.
"""

import asyncio
import logging
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Awaitable, Callable, Optional

from config import settings


LOG_DIR = Path(settings.log_dir) if settings.log_dir else Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _file_logger(name: str, filename: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = RotatingFileHandler(LOG_DIR / filename, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


timing_log = _file_logger("sr.timing", "timing.log")
error_log = _file_logger("sr.errors", "errors.log")


class DebugState:
    """Runtime on/off switch for the detailed log (toggled from the dashboard)."""
    enabled: bool = settings.detailed_log


debug = DebugState()

Emit = Callable[[dict], Awaitable[None]]


def log_timing_line(line: str):
    """Console + logs/timing.log, only while the detailed log is on."""
    if debug.enabled:
        print(f"[TIMING] {line}", flush=True)
        timing_log.info(line)


def _describe(exc: BaseException) -> str:
    """Short reason; Sarvam SDK errors keep the useful part in .status_code / .body."""
    status, body = getattr(exc, "status_code", None), getattr(exc, "body", None)
    if status or body:
        return f"{type(exc).__name__} status={status} body={body}"
    return repr(exc)


def log_error(where: str, exc: BaseException | None = None, detail: str = "", trace_id: str = ""):
    """Always written: console + logs/errors.log, with traceback when there is an exception.

    An exception is logged in full once (where it happened); later handlers only add a one-line note.
    """
    head = f"[ERROR] {where}" + (f" trace={trace_id}" if trace_id else "") + (f" | {detail}" if detail else "")
    reason = f" | {_describe(exc)}" if exc else ""
    already = exc is not None and getattr(exc, "_sr_logged", False)
    tb = "".join(traceback.format_exception(exc, limit=-8)) if exc and not already else ""
    print(head + reason, flush=True)
    error_log.error(head + reason + (" (traceback logged above)" if already else "") + (f"\n{tb}" if tb else ""))
    if exc is not None:
        try:
            exc._sr_logged = True
        except AttributeError:
            pass
    if debug.enabled:
        timing_log.info(head + reason)


class _Span:
    def __init__(self, timeline: "Timeline", step: str, detail: str):
        self.timeline, self.step, self.detail = timeline, step, detail

    def __enter__(self):
        self.start = time.perf_counter()
        self.timeline.mark(f"{self.step} start", self.detail)
        return self

    def __exit__(self, exc_type, exc, tb):
        ms = (time.perf_counter() - self.start) * 1000
        detail = f"FAILED: {exc!r}" if exc else self.detail
        self.timeline.mark(f"{self.step} done", detail, ms=ms)
        if exc:
            log_error(self.step, exc, trace_id=self.timeline.trace_id)
        return False


class Timeline:
    """Collects timed steps for one request."""

    def __init__(self, trace_id: str, kind: str, emit: Optional[Emit] = None):
        self.trace_id = trace_id
        self.kind = kind
        self.emit = emit
        self.t0 = time.perf_counter()
        self.events: list[dict] = []

    def mark(self, step: str, detail: str = "", ms: float | None = None):
        """Record a point in time (optionally with the duration of what just finished)."""
        at = (time.perf_counter() - self.t0) * 1000
        event = {"trace_id": self.trace_id, "source": "server", "step": step,
                 "at_ms": round(at), "ms": round(ms) if ms is not None else None, "detail": str(detail)[:300]}
        self.events.append(event)
        if not debug.enabled:
            return
        dur = f"{event['ms']:>6}ms" if ms is not None else " " * 8
        # Full detail goes to the file; the dashboard event is truncated above
        log_timing_line(f"{self.trace_id} +{event['at_ms']:>6}ms {dur}  {step:<34} {detail}")
        if self.emit:
            try:
                asyncio.get_running_loop().create_task(self.emit({"type": "timing_step", **event}))
            except RuntimeError:
                pass

    def span(self, step: str, detail: str = "") -> _Span:
        """Time a block: `with timeline.span("stt"): ...` records start, done and duration."""
        return _Span(self, step, detail)

    def total_ms(self) -> int:
        return round((time.perf_counter() - self.t0) * 1000)
