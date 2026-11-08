"""Allowlisted JSON telemetry; never log inputs, outputs, URLs or exceptions."""

import functools
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import ParamSpec, TypeVar

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
P = ParamSpec("P")
T = TypeVar("T")
logger = logging.getLogger("trading212.telemetry")


class PrivateFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Library messages/tracebacks can contain URLs, headers or financial data.
        result: dict[str, object] = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": "library_event",
        }
        if record.name == logger.name:
            for key in (
                "event",
                "stage",
                "operation",
                "duration_ms",
                "outcome",
                "request_id",
                "status_code",
                "response_bytes",
            ):
                if key in record.__dict__:
                    result[key] = record.__dict__[key]
        return json.dumps(result, allow_nan=False, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(PrivateFormatter())
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    for name in ("httpx", "httpcore", "mcp", "yfinance", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)


@contextmanager
def span(stage: str, operation: str) -> Generator[None, None, None]:
    start = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error_or_cancelled"
        raise
    finally:
        logger.info(
            "",
            extra={
                "event": "timing",
                "stage": stage,
                "operation": operation,
                "duration_ms": round((time.perf_counter() - start) * 1000, 3),
                "outcome": outcome,
                "request_id": request_id.get(),
            },
        )


def timed_async(
    stage: str,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    def decorate(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            with span(stage, fn.__qualname__):
                return await fn(*args, **kwargs)

        return wrapped

    return decorate


def timed_sync(stage: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    def decorate(fn: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(fn)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            with span(stage, fn.__qualname__):
                return fn(*args, **kwargs)

        return wrapped

    return decorate
