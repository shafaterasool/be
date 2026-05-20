"""
structured_logger.py — Professional Structured Logging

Features:
  ✅ Unique Request IDs (UUID) per operation
  ✅ Stack traces on errors (full traceback)
  ✅ Retry attempt logging with delay tracking
  ✅ HTTP request/response logging with duration
  ✅ JSON-structured log entries
  ✅ Async background flush (3-second daemon)
  ✅ New DB table: structured_logs
  ✅ Severity levels: DEBUG / INFO / WARNING / ERROR / CRITICAL / RETRY / REQUEST
"""

import json
import logging
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Optional, Any

log = logging.getLogger("structured")

# ─── Background Flush Buffer ──────────────────────────────────────────────────
_s_log_buffer      = []
_s_log_buffer_lock = threading.Lock()
_s_get_db_fn       = None   # injected at init time


def _flush_structured_buffer():
    with _s_log_buffer_lock:
        if not _s_log_buffer:
            return
        buf = _s_log_buffer[:]
        _s_log_buffer.clear()
    try:
        db = _s_get_db_fn()
        db.executemany(
            """INSERT INTO structured_logs
               (channel_id, level, request_id, message, extra, ts)
               VALUES (?,?,?,?,?,?)""",
            buf
        )
        db.commit()
    except Exception as e:
        log.error(f"[StructuredLog] flush error: {e}")


def _flush_daemon():
    while True:
        time.sleep(3)
        try:
            if _s_get_db_fn:
                _flush_structured_buffer()
        except Exception as e:
            log.error(f"[StructuredLog] daemon error: {e}")


_flush_thread = threading.Thread(
    target=_flush_daemon, daemon=True, name="slog-flusher"
)
_flush_thread.start()


# ─── Init ─────────────────────────────────────────────────────────────────────
def init_structured_logging(get_db_fn, db):
    """
    Call once at startup with:
      get_db_fn : callable → DB connection (same as get_db())
      db        : an active DB connection to create the table
    """
    global _s_get_db_fn
    _s_get_db_fn = get_db_fn

    # Create the structured_logs table
    if _is_postgres():
        db.execute("""
            CREATE TABLE IF NOT EXISTS structured_logs (
                id         BIGSERIAL PRIMARY KEY,
                channel_id INTEGER,
                level      TEXT,
                request_id TEXT,
                message    TEXT,
                extra      TEXT,
                ts         TEXT
            )
        """)
    else:
        db.execute("""
            CREATE TABLE IF NOT EXISTS structured_logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                level      TEXT,
                request_id TEXT,
                message    TEXT,
                extra      TEXT,
                ts         TEXT
            )
        """)

    # Index for fast channel_id queries
    try:
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_slog_channel "
            "ON structured_logs(channel_id, ts DESC)"
        )
    except Exception:
        pass

    try:
        db.commit()
    except Exception:
        pass


def _is_postgres():
    """Check if using PostgreSQL."""
    import os
    return bool(os.environ.get("DATABASE_URL", "").strip())


# ─── Request ID Generator ─────────────────────────────────────────────────────
def new_request_id() -> str:
    """Generate short unique request ID (8 hex chars)."""
    return uuid.uuid4().hex[:8].upper()


# ─── StructuredLogger Class ───────────────────────────────────────────────────
class StructuredLogger:
    """
    Professional structured logger for a channel/operation.

    Usage:
        logger = StructuredLogger(channel_id=5)
        logger.info("Upload started", video_id="abc123")
        logger.retry("FB upload failed, retrying", attempt=2, max_attempts=3)
        try:
            ...
        except Exception as e:
            logger.error("Upload crashed", exc=e, video_id="abc123")
    """

    def __init__(
        self,
        channel_id: Optional[int] = None,
        request_id: Optional[str] = None,
        context: Optional[dict] = None,
    ):
        self.channel_id  = channel_id
        self.request_id  = request_id or new_request_id()
        self.context     = context or {}
        self._start_time = time.time()

    def _emit(self, level: str, message: str, extra: dict):
        """Core log emit: buffer + console."""
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "ts":         now,
            "level":      level,
            "request_id": self.request_id,
            "channel_id": self.channel_id,
            "message":    message,
            **self.context,
            **extra,
        }

        # Console log (pretty JSON for easy tailing)
        console_msg = (
            f"[{level}][ch{self.channel_id}][{self.request_id}] {message}"
        )
        if level in ("ERROR", "CRITICAL"):
            log.error(console_msg)
        elif level == "WARNING":
            log.warning(console_msg)
        else:
            log.info(console_msg)

        # DB buffer
        if _s_get_db_fn:
            extra_json = json.dumps(
                {k: v for k, v in entry.items()
                 if k not in ("ts", "level", "request_id", "channel_id", "message")}
            )
            with _s_log_buffer_lock:
                _s_log_buffer.append((
                    self.channel_id,
                    level,
                    self.request_id,
                    message,
                    extra_json,
                    now,
                ))
                # Immediate flush on critical
                should_flush = len(_s_log_buffer) >= 50 or level == "CRITICAL"

            if should_flush and _s_get_db_fn:
                _flush_structured_buffer()

    # ── Log levels ──
    def debug(self, message: str, **extra):
        self._emit("DEBUG", message, extra)

    def info(self, message: str, **extra):
        self._emit("INFO", message, extra)

    def warning(self, message: str, **extra):
        self._emit("WARNING", message, extra)

    def error(self, message: str, exc: Exception = None, **extra):
        if exc is not None:
            extra["stack_trace"] = traceback.format_exc()
            extra["exception"]   = f"{type(exc).__name__}: {exc}"
        self._emit("ERROR", message, extra)

    def critical(self, message: str, exc: Exception = None, **extra):
        if exc is not None:
            extra["stack_trace"] = traceback.format_exc()
            extra["exception"]   = f"{type(exc).__name__}: {exc}"
        self._emit("CRITICAL", message, extra)

    # ── Retry logging ──
    def retry(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: float = 0,
        reason: str = "",
        **extra,
    ):
        self._emit("RETRY", message, {
            "attempt":       attempt,
            "max_attempts":  max_attempts,
            "delay_seconds": delay_seconds,
            "reason":        reason,
            **extra,
        })

    # ── HTTP Request logging ──
    def request(
        self,
        method: str,
        url: str,
        status_code: int,
        duration_ms: float,
        **extra,
    ):
        masked_url = _mask_token(url)
        self._emit("REQUEST", f"{method} {masked_url} → {status_code}", {
            "method":      method,
            "url":         masked_url,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 1),
            **extra,
        })

    # ── Token validation log ──
    def token_validated(self, page_id: str, valid: bool, reason: str = ""):
        self._emit("TOKEN_CHECK", f"Page {page_id} token {'VALID' if valid else 'INVALID'}", {
            "page_id": page_id,
            "valid":   valid,
            "reason":  reason,
        })

    # ── Page ownership log ──
    def ownership_claimed(self, page_id: str, account_id: int, was_owned_by: int = None):
        msg = f"Page {page_id} ownership → account {account_id}"
        if was_owned_by and was_owned_by != account_id:
            msg += f" (transferred from {was_owned_by})"
        self._emit("OWNERSHIP", msg, {
            "page_id":      page_id,
            "account_id":   account_id,
            "was_owned_by": was_owned_by,
        })

    # ── Timing helper ──
    def elapsed_ms(self) -> float:
        return round((time.time() - self._start_time) * 1000, 1)

    def done(self, message: str = "Operation complete", **extra):
        self._emit("INFO", message, {"elapsed_ms": self.elapsed_ms(), **extra})


# ─── Module-level helpers ─────────────────────────────────────────────────────
def get_logger(channel_id: int = None, request_id: str = None) -> StructuredLogger:
    """Quick factory: get a new StructuredLogger."""
    return StructuredLogger(channel_id=channel_id, request_id=request_id)


def _mask_token(url: str) -> str:
    """Replace access_token= value with *** in URLs for safe logging."""
    import re
    return re.sub(
        r"(access_token=)[^&]+",
        r"\1***MASKED***",
        url
    )


# ─── Convenience: timed HTTP request wrapper ──────────────────────────────────
def logged_request(logger: StructuredLogger, method: str, url: str, **kwargs) -> Any:
    """
    Make an HTTP request and log it with timing.
    Returns the requests.Response object.
    Raises on non-2xx if `raise_for_status=True` (default).
    
    Usage:
        resp = logged_request(logger, "GET", "https://graph.facebook.com/...", params={...})
    """
    import requests as _requests
    raise_on_error = kwargs.pop("raise_for_status", True)
    t0 = time.time()
    try:
        resp = _requests.request(method, url, **kwargs)
        duration_ms = (time.time() - t0) * 1000
        logger.request(method, url, resp.status_code, duration_ms)
        if raise_on_error:
            resp.raise_for_status()
        return resp
    except Exception as e:
        duration_ms = (time.time() - t0) * 1000
        logger.request(method, url, getattr(e, "response", type("", (), {"status_code": 0})()).status_code, duration_ms)
        raise
