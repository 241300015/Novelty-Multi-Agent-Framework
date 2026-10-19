"""Process-wide scheduling for the arXiv export API.

This module is the sole HTTP emission point for ``export.arxiv.org/api/query``.
Search requests remain semantically independent; metadata requests are collected
for a short window and emitted as de-duplicated ``id_list`` batches.
"""

from __future__ import annotations

import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import Future
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

import httpx

from .arxiv_identifiers import atom_identifier


provider_task_id: ContextVar[str | None] = ContextVar("arxiv_task_id", default=None)

ARXIV_QUERY_URL = "https://export.arxiv.org/api/query"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
)


class ArxivCircuitOpenError(RuntimeError):
    """Raised without I/O while the process-wide arXiv circuit is open."""


class ArxivRetryBudgetExceeded(httpx.TimeoutException):
    """Raised when throttle/retry waits exhaust one physical request budget."""


class ArxivResponseParseError(ValueError):
    """Raised when HTTP 200 does not contain a valid Atom document."""


def retry_delay(response: httpx.Response, attempt: int, *, max_delay: float) -> float:
    """Honor numeric Retry-After exactly; cap only locally-generated backoff."""

    header = (response.headers.get("Retry-After") or "").strip()
    if header.isdigit():
        return float(header)
    if header:
        try:
            retry_at = parsedate_to_datetime(header)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                (retry_at - datetime.now(timezone.utc)).total_seconds(),
            )
        except (TypeError, ValueError, OverflowError):
            pass
    return min(2.0 * (2**attempt), max_delay)


def _runtime_recorder() -> Any | None:
    try:
        from ....core.runtime_artifacts import current_runtime_artifacts

        return current_runtime_artifacts()
    except (ImportError, AttributeError):
        return None


def _strip_version(value: str) -> str:
    head, marker, version = value.rpartition("v")
    return head if marker and version.isdigit() else value


class ArxivRequestScheduler:
    """Serialize physical API traffic and coalesce concurrent metadata lookups."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        base_url: str = ARXIV_QUERY_URL,
        min_interval: float = 4.0,
        timeout: float = 20.0,
        max_retries: int = 2,
        max_retry_delay: float = 5.0,
        retry_budget_seconds: float = 45.0,
        circuit_failure_threshold: int = 2,
        circuit_cooldown_seconds: float = 60.0,
        scheduler_enabled: bool = True,
        metadata_batch_enabled: bool = True,
        metadata_batch_window_ms: int = 200,
        metadata_batch_max_size: int = 32,
    ) -> None:
        if timeout <= 0 or retry_budget_seconds <= 0:
            raise ValueError("timeout and retry_budget_seconds must be positive")
        if min_interval < 0 or max_retries < 0 or max_retry_delay < 0:
            raise ValueError("retry and throttle values must be non-negative")
        if circuit_failure_threshold < 1 or circuit_cooldown_seconds < 0:
            raise ValueError("circuit breaker values are invalid")
        if metadata_batch_window_ms < 0 or metadata_batch_max_size < 1:
            raise ValueError("metadata batch values are invalid")
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None
        self._base_url = base_url
        self._min_interval = min_interval if scheduler_enabled else 0.0
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_retry_delay = max_retry_delay
        self._retry_budget_seconds = retry_budget_seconds
        self._failure_threshold = circuit_failure_threshold
        self._cooldown = circuit_cooldown_seconds
        self._batch_enabled = metadata_batch_enabled
        self._batch_window = metadata_batch_window_ms / 1000.0
        self._batch_max_size = metadata_batch_max_size
        self._request_gate = threading.Lock()
        self._state_lock = threading.RLock()
        self._pending = threading.Condition(self._state_lock)
        self._pending_metadata: dict[str, list[tuple[Future[str | None], float, Any]]] = {}
        self._first_pending_at: float | None = None
        self._closed = False
        self._next_allowed_at = 0.0
        self._blocked_until = 0.0
        self._last_dispatch_at: float | None = None
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._probe_in_flight = False
        self._started_at = time.monotonic()
        self._events: list[dict[str, Any]] = []
        self._logical_tasks: dict[str, str | None] = {}
        self._metrics: dict[str, Any] = {
            "logical_api_requests": 0,
            "physical_api_requests": 0,
            "metadata_logical_requests": 0,
            "metadata_physical_requests": 0,
            "unique_metadata_ids": 0,
            "batch_count": 0,
            "batch_sizes": [],
            "dedup_count": 0,
            "http_200_count": 0,
            "http_429_count": 0,
            "read_timeout_count": 0,
            "retry_count": 0,
            "queue_wait_ms": 0.0,
            "api_elapsed_ms": 0.0,
            "interval_violation_count": 0,
        }
        self._dispatcher = threading.Thread(
            target=self._metadata_loop,
            name="arxiv-metadata-batcher",
            daemon=True,
        )
        self._dispatcher.start()

    def search(self, query: str, *, limit: int) -> httpx.Response:
        logical_id = self._record_logical("search", query=query)
        return self._request(
            {"search_query": query, "start": 0, "max_results": limit},
            operation="search",
            logical_request_ids=[logical_id],
            logical_count=1,
            unique_count=0,
            queue_started=time.monotonic(),
            batch_wait_ms=0.0,
            recorders=[_runtime_recorder()],
        )

    def request_url(self, url: str) -> httpx.Response:
        """Compatibility entry point used by the layered smoke probe."""

        params = dict(parse_qsl(urlsplit(url).query))
        operation = "known_id" if "id_list" in params else "search"
        logical_id = self._record_logical(operation)
        return self._request(
            params,
            operation=operation,
            logical_request_ids=[logical_id],
            logical_count=1,
            unique_count=len(params.get("id_list", "").split(",")) if params.get("id_list") else 0,
            queue_started=time.monotonic(),
            batch_wait_ms=0.0,
            recorders=[_runtime_recorder()],
        )

    def resolve_metadata(self, document_id: str) -> str | None:
        doc_id = _strip_version(document_id)
        recorder = _runtime_recorder()
        logical_id = self._record_logical("metadata", document_id=doc_id, recorder=recorder)
        future: Future[str | None] = Future()
        queued_at = time.monotonic()
        with self._pending:
            if self._closed:
                raise RuntimeError("arxiv scheduler is shut down")
            self._pending_metadata.setdefault(doc_id, []).append(
                (future, queued_at, recorder)
            )
            if self._first_pending_at is None:
                self._first_pending_at = queued_at
            # Preserve each logical request ID on its future for the batch event.
            setattr(future, "_arxiv_logical_id", logical_id)
            self._pending.notify_all()
        return future.result()

    def _metadata_loop(self) -> None:
        while True:
            with self._pending:
                while not self._pending_metadata and not self._closed:
                    self._pending.wait()
                if self._closed and not self._pending_metadata:
                    return
                first = self._first_pending_at or time.monotonic()
                deadline = first + (self._batch_window if self._batch_enabled else 0.0)
                while (
                    not self._closed
                    and len(self._pending_metadata) < self._batch_max_size
                    and time.monotonic() < deadline
                ):
                    self._pending.wait(timeout=max(0.0, deadline - time.monotonic()))
                take_size = self._batch_max_size if self._batch_enabled else 1
                ids = list(self._pending_metadata)[:take_size]
                batch = {doc_id: self._pending_metadata.pop(doc_id) for doc_id in ids}
                self._first_pending_at = time.monotonic() if self._pending_metadata else None
            self._flush_metadata(batch, first)

    def _flush_metadata(
        self,
        batch: dict[str, list[tuple[Future[str | None], float, Any]]],
        first_pending_at: float,
    ) -> None:
        waiters = [item for values in batch.values() for item in values]
        logical_ids = [getattr(item[0], "_arxiv_logical_id") for item in waiters]
        recorders = [item[2] for item in waiters]
        batch_wait_ms = (time.monotonic() - first_pending_at) * 1000
        try:
            response = self._request(
                {"id_list": ",".join(batch), "max_results": len(batch)},
                operation="metadata_batch",
                logical_request_ids=logical_ids,
                logical_count=len(waiters),
                unique_count=len(batch),
                queue_started=min(item[1] for item in waiters),
                batch_wait_ms=batch_wait_ms,
                recorders=recorders,
            )
            try:
                root = ET.fromstring(response.text)
            except ET.ParseError as exc:
                raise ArxivResponseParseError("invalid arXiv Atom response") from exc
            by_id: dict[str, str] = {}
            for entry in root.findall(f"{ATOM_NS}entry"):
                atom_id = entry.findtext(f"{ATOM_NS}id") or ""
                doc_id = _strip_version(atom_identifier(atom_id))
                if doc_id:
                    by_id[doc_id.casefold()] = ET.tostring(entry, encoding="unicode")
            for doc_id, values in batch.items():
                result = by_id.get(doc_id.casefold())
                for future, _, _ in values:
                    future.set_result(result)
        except BaseException as exc:
            for future, _, _ in waiters:
                future.set_exception(exc)

    def _request(
        self,
        params: dict[str, Any],
        *,
        operation: str,
        logical_request_ids: list[str],
        logical_count: int,
        unique_count: int,
        queue_started: float,
        batch_wait_ms: float,
        recorders: list[Any],
    ) -> httpx.Response:
        with self._request_gate:
            was_probe = self._acquire_circuit_permission(recorders)
            deadline = time.monotonic() + self._retry_budget_seconds
            last_error: Exception | None = None
            attempt_limit = 1 if was_probe else self._max_retries + 1
            for attempt in range(attempt_limit):
                wait = max(self._next_allowed_at, self._blocked_until) - time.monotonic()
                if wait > 0:
                    if wait >= deadline - time.monotonic():
                        error = last_error or self._budget_error()
                        self._record_failure(was_probe, recorders)
                        raise error
                    time.sleep(wait)
                now = time.monotonic()
                if now >= deadline:
                    error = last_error or self._budget_error()
                    self._record_failure(was_probe, recorders)
                    raise error
                previous_interval_ms = (
                    None if self._last_dispatch_at is None else (now - self._last_dispatch_at) * 1000
                )
                if previous_interval_ms is not None and previous_interval_ms + 0.001 < self._min_interval * 1000:
                    with self._state_lock:
                        self._metrics["interval_violation_count"] += 1
                self._last_dispatch_at = now
                self._next_allowed_at = now + self._min_interval
                request_id = uuid.uuid4().hex
                dispatch_wall = datetime.now(timezone.utc).isoformat()
                started = time.monotonic()
                response: httpx.Response | None = None
                retry_after: float | None = None
                backoff = 0.0
                try:
                    for recorder in {id(item): item for item in recorders
                                     if item is not None}.values():
                        recorder.reserve_provider_request(provider="arxiv_api",
                                                          operation=operation)
                    response = self._client.get(
                        self._base_url,
                        params=params,
                        timeout=min(self._timeout, deadline - now),
                    )
                    response.raise_for_status()
                    self._record_success(recorders)
                    self._record_physical(
                        request_id, operation, logical_request_ids, logical_count,
                        unique_count, queue_started, batch_wait_ms, now,
                        previous_interval_ms, attempt + 1, response.status_code,
                        None, 0.0, started, recorders, dispatch_wall=dispatch_wall,
                        response_headers=dict(response.headers),
                        response_body=response.text[:2000],
                    )
                    return response
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status in _RETRYABLE_STATUS:
                        retry_after = retry_delay(
                            exc.response, attempt, max_delay=self._max_retry_delay
                        )
                        backoff = retry_after
                    self._record_physical(
                        request_id, operation, logical_request_ids, logical_count,
                        unique_count, queue_started, batch_wait_ms, now,
                        previous_interval_ms, attempt + 1, status, retry_after, backoff,
                        started, recorders, dispatch_wall=dispatch_wall,
                        response_headers=dict(exc.response.headers),
                        response_body=exc.response.text[:2000],
                    )
                    if status not in _RETRYABLE_STATUS:
                        self._record_success(recorders)
                        raise
                    last_error = exc
                    if status == 429:
                        self._blocked_until = max(self._blocked_until, time.monotonic() + retry_after)
                except _RETRYABLE_TRANSPORT_ERRORS as exc:
                    last_error = exc
                    backoff = min(2.0 * (2**attempt), self._max_retry_delay)
                    self._record_physical(
                        request_id, operation, logical_request_ids, logical_count,
                        unique_count, queue_started, batch_wait_ms, now,
                        previous_interval_ms, attempt + 1, None, None, backoff,
                        started, recorders, dispatch_wall=dispatch_wall,
                        error_type=type(exc).__name__,
                    )
                if attempt + 1 >= attempt_limit:
                    self._record_failure(was_probe, recorders)
                    raise last_error
                with self._state_lock:
                    self._metrics["retry_count"] += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0 or backoff >= remaining:
                    if remaining > 0:
                        time.sleep(remaining)
                    self._record_failure(was_probe, recorders)
                    raise last_error
                # For 429, blocked_until drives this wait on the next iteration.
                if not (response is not None and response.status_code == 429):
                    time.sleep(backoff)
        raise AssertionError("unreachable")

    def _record_logical(self, operation: str, *, recorder: Any = None, **fields: Any) -> str:
        logical_id = uuid.uuid4().hex
        event = {
            "event_type": "logical_request",
            "provider": "arxiv",
            "transport": "api",
            "operation": operation,
            "logical_request_id": logical_id,
            "task_id": provider_task_id.get(),
            **fields,
        }
        with self._state_lock:
            self._logical_tasks[logical_id] = provider_task_id.get()
            self._metrics["logical_api_requests"] += 1
            if operation == "metadata":
                self._metrics["metadata_logical_requests"] += 1
            self._events.append(event)
        self._emit_runtime(recorder if recorder is not None else _runtime_recorder(), event)
        return logical_id

    def _record_physical(
        self, request_id: str, operation: str, logical_ids: list[str],
        logical_count: int, unique_count: int, queue_started: float,
        batch_wait_ms: float, dispatch_at: float, previous_interval_ms: float | None,
        attempt: int, status_code: int | None, retry_after: float | None,
        backoff: float, started: float, recorders: list[Any],
        *, dispatch_wall: str, error_type: str | None = None,
        response_headers: dict[str, str] | None = None,
        response_body: str | None = None,
    ) -> None:
        elapsed = (time.monotonic() - started) * 1000
        queue_wait = (dispatch_at - queue_started) * 1000
        event = {
            "event_type": "physical_request",
            "provider": "arxiv",
            "transport": "api",
            "physical_request_id": request_id,
            "request_id": request_id,
            "task_id": self._logical_tasks.get(logical_ids[0]) if logical_ids else None,
            "task_ids": list(dict.fromkeys(self._logical_tasks.get(key) for key in logical_ids if self._logical_tasks.get(key) is not None)),
            "interval_violation": previous_interval_ms is not None and previous_interval_ms + 0.001 < self._min_interval * 1000,
            "operation": operation,
            "batch_id": request_id if operation == "metadata_batch" else None,
            "logical_request_ids": logical_ids,
            "logical_request_count": logical_count,
            "unique_id_count": unique_count,
            "queue_wait_ms": round(queue_wait, 3),
            "batch_wait_ms": round(batch_wait_ms, 3),
            "dispatch_at": dispatch_wall,
            "previous_request_interval_ms": previous_interval_ms,
            "attempt": attempt,
            "status_code": status_code,
            "retry_after": retry_after,
            "backoff_ms": round(backoff * 1000, 3),
            "elapsed_ms": round(elapsed, 3),
            "error_type": error_type,
            "response_headers": response_headers,
            "response_body": response_body,
        }
        with self._state_lock:
            self._metrics["physical_api_requests"] += 1
            self._metrics["queue_wait_ms"] += queue_wait
            self._metrics["api_elapsed_ms"] += elapsed
            if operation == "metadata_batch":
                self._metrics["metadata_physical_requests"] += 1
                self._metrics["unique_metadata_ids"] += unique_count
                self._metrics["batch_count"] += 1
                self._metrics["batch_sizes"].append(unique_count)
                self._metrics["dedup_count"] += logical_count - unique_count
            if status_code == 200:
                self._metrics["http_200_count"] += 1
            if status_code == 429:
                self._metrics["http_429_count"] += 1
            if error_type == "ReadTimeout":
                self._metrics["read_timeout_count"] += 1
            self._events.append(event)
        for recorder in {id(item): item for item in recorders if item is not None}.values():
            self._emit_runtime(recorder, event)

    @staticmethod
    def _emit_runtime(recorder: Any, event: dict[str, Any]) -> None:
        if recorder is not None and hasattr(recorder, "record_provider_request"):
            recorder.record_provider_request(event)

    def _acquire_circuit_permission(self, recorders: list[Any]) -> bool:
        now = time.monotonic()
        with self._state_lock:
            if self._circuit_open_until <= 0:
                return False
            if now < self._circuit_open_until or self._probe_in_flight:
                raise ArxivCircuitOpenError(
                    "arxiv circuit is open after consecutive provider failures"
                )
            self._probe_in_flight = True
            self._record_circuit("HALF_OPEN", recorders)
            return True

    def _record_success(self, recorders: list[Any]) -> None:
        with self._state_lock:
            if self._circuit_open_until > 0:
                self._record_circuit("CLOSED", recorders)
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0
            self._probe_in_flight = False

    def _record_failure(self, was_probe: bool, recorders: list[Any]) -> None:
        with self._state_lock:
            self._probe_in_flight = False
            self._consecutive_failures += 1
            if was_probe or self._consecutive_failures >= self._failure_threshold:
                self._circuit_open_until = time.monotonic() + self._cooldown
                self._record_circuit("OPEN", recorders)

    def _record_circuit(self, state: str, recorders: list[Any]) -> None:
        event = {"event_type": "circuit_transition", "provider": "arxiv",
                 "transport": "api", "operation": "circuit", "circuit_state": state}
        self._events.append(event)
        for recorder in {id(r): r for r in recorders if r is not None}.values():
            self._emit_runtime(recorder, event)

    def _budget_error(self) -> ArxivRetryBudgetExceeded:
        return ArxivRetryBudgetExceeded(
            "arxiv retry budget exhausted",
            request=httpx.Request("GET", self._base_url),
        )

    def snapshot_metrics(self, *, include_events: bool = False) -> dict[str, Any]:
        with self._state_lock:
            result = dict(self._metrics)
            sizes = list(result.pop("batch_sizes"))
            physical = result["physical_api_requests"]
            metadata_physical = result["metadata_physical_requests"]
            result.update(
                circuit_open_count=sum(e.get("circuit_state") == "OPEN" for e in self._events),
                circuit_state="HALF_OPEN" if self._probe_in_flight else "OPEN" if self._circuit_open_until else "CLOSED",
                average_batch_size=(sum(sizes) / len(sizes) if sizes else 0.0),
                max_batch_size=max(sizes, default=0),
                request_compression_ratio=(result["logical_api_requests"] / physical if physical else 0.0),
                metadata_batch_ratio=(result["metadata_logical_requests"] / metadata_physical if metadata_physical else 0.0),
                total_arxiv_elapsed_ms=(time.monotonic() - self._started_at) * 1000,
            )
            if include_events:
                result["events"] = list(self._events)
            return result

    def shutdown(self) -> None:
        with self._pending:
            self._closed = True
            self._pending.notify_all()
        if threading.current_thread() is not self._dispatcher:
            self._dispatcher.join(timeout=max(1.0, self._batch_window + 0.5))
        if self._owns_client:
            self._client.close()


_SHARED_LOCK = threading.Lock()
_SHARED_SCHEDULER: ArxivRequestScheduler | None = None


def get_shared_arxiv_scheduler(**kwargs: Any) -> ArxivRequestScheduler:
    global _SHARED_SCHEDULER
    with _SHARED_LOCK:
        if _SHARED_SCHEDULER is None:
            _SHARED_SCHEDULER = ArxivRequestScheduler(**kwargs)
        return _SHARED_SCHEDULER


def reset_shared_arxiv_scheduler() -> None:
    global _SHARED_SCHEDULER
    with _SHARED_LOCK:
        scheduler, _SHARED_SCHEDULER = _SHARED_SCHEDULER, None
    if scheduler is not None:
        scheduler.shutdown()


__all__ = [
    "ARXIV_QUERY_URL",
    "ArxivCircuitOpenError",
    "ArxivRequestScheduler",
    "ArxivResponseParseError",
    "ArxivRetryBudgetExceeded",
    "get_shared_arxiv_scheduler",
    "reset_shared_arxiv_scheduler",
    "retry_delay",
]
