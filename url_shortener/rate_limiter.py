import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

RATE_LIMIT_MESSAGE = "Create rate limit exceeded. Try again later."


class SlidingWindowRateLimiter:
    """Single-process rolling-window limiter; its counters reset on restart."""

    def __init__(
        self,
        max_requests: int = 10,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        cleanup_interval: int = 256,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if cleanup_interval < 1:
            raise ValueError("cleanup_interval must be positive")

        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._clock = clock
        self._cleanup_interval = cleanup_interval
        self._calls_since_cleanup = 0
        self._requests_by_ip: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, client_ip: str) -> bool:
        with self._lock:
            # Sampling under the lock keeps each client's deque chronological.
            now = self._clock()
            timestamps = self._requests_by_ip.get(client_ip)
            if timestamps is not None:
                self._remove_expired(timestamps, now)
                if not timestamps:
                    del self._requests_by_ip[client_ip]
                    timestamps = None

            allowed = timestamps is None or len(timestamps) < self._max_requests
            if allowed:
                if timestamps is None:
                    timestamps = deque()
                    self._requests_by_ip[client_ip] = timestamps
                timestamps.append(now)

            self._calls_since_cleanup += 1
            if self._calls_since_cleanup >= self._cleanup_interval:
                self._prune_inactive(now)
                self._calls_since_cleanup = 0

            return allowed

    def _remove_expired(self, timestamps: deque[float], now: float) -> None:
        cutoff = now - self._window_seconds
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

    def _prune_inactive(self, now: float) -> None:
        # Periodic full sweeps remove buckets for IPs that stopped sending requests.
        inactive_ips = []
        for client_ip, timestamps in self._requests_by_ip.items():
            self._remove_expired(timestamps, now)
            if not timestamps:
                inactive_ips.append(client_ip)
        for client_ip in inactive_ips:
            del self._requests_by_ip[client_ip]


class CreateRateLimitExceeded(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=429, detail=RATE_LIMIT_MESSAGE)


async def enforce_create_rate_limit(request: Request) -> None:
    client = request.client
    client_ip = client.host if client is not None else "unknown"
    limiter: SlidingWindowRateLimiter = request.app.state.create_rate_limiter
    if not limiter.allow(client_ip):
        raise CreateRateLimitExceeded()


async def create_rate_limit_exception_handler(
    request: Request, error: Exception
) -> JSONResponse:
    del request, error
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": "rate_limited",
                "message": RATE_LIMIT_MESSAGE,
            }
        },
    )
