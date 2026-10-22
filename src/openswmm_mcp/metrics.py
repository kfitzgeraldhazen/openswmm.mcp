"""What each session costs: CPU, wall time and resident memory per engine call.

Every engine call goes through :meth:`openswmm_mcp.session.SimSession.call`,
which runs it in a worker thread under the session lock. That single chokepoint
is instrumented here, so one wrapper accounts for ``open_model``, every ``run``
chunk, ``report``, ``export`` and every ``call(...)``.

Two clocks, because neither alone is honest:

``cpu_seconds``
    :func:`time.thread_time` read inside the worker thread: CPU (user+system)
    burned by *that call alone*, never contaminated by a concurrent session.
    It cannot see threads the engine spawns for itself, so it is a lower bound.
``clean_cpu_seconds``
    :func:`time.process_time`, which counts every thread in the process. It is
    only attributable to one session when no other measured call overlapped, so
    it is accumulated from uncontended calls only -- see :class:`Totals`.

Comparing the two over the same uncontended calls is a standing check on
whether the engine threads internally: a ratio above ``_THREADED_RATIO`` means
it does, and that ``cpu_seconds`` alone under-reports the machine you need.

``psutil`` is optional (``pip install 'openswmm.mcp[metrics]'``) and adds
resident memory and the user/system split. Everything else is stdlib.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")

#: Why consumed CPU is not the bill. Shipped with the metrics resources,
#: because the numbers mislead without it.
COST_NOTE = (
    "Per-second-billed hosts charge allocated vCPU x wall seconds, not consumed CPU: "
    "wall_seconds drives the bill and cpu_cores_used says whether the allocation fits. "
    "cpu_seconds counts this session's own worker thread; cpu_cores_used counts every "
    "engine thread, but only from calls that no other session overlapped."
)

# Present on Windows and Linux; the abstract fallback keeps the module importable
# anywhere, at the cost of per-call CPU being process-wide (reported in the snapshot).
_HAS_THREAD_CLOCK = hasattr(time, "thread_time")
_thread_time: Callable[[], float] = getattr(time, "thread_time", time.process_time)

try:  # Tier 1, optional
    import psutil

    _PROCESS: Any = psutil.Process()
    _PROCESS.memory_info()  # prove it works here, not at the first call site
except Exception:  # pragma: no cover - depends on environment
    psutil = None  # type: ignore[assignment]
    _PROCESS = None

#: Reading RSS is a syscall. Skip it for the sub-millisecond state peeks that
#: dominate the step loop by count and contribute nothing to cost.
_RSS_MIN_WALL = 0.001
#: Uncontended all-thread/own-thread CPU ratio above which the engine is threaded.
_THREADED_RATIO = 1.2
#: Enough uncontended wall time for that ratio to mean anything.
_THREADED_MIN_WALL = 0.05

_STARTED = time.monotonic()

# Overlap bookkeeping. ``_entries`` only ever increases, so a call that both
# started and finished inside our window is still detected.
_inflight = 0
_entries = 0
_counter_lock = threading.Lock()

_TOTAL_FIELDS = (
    "calls",
    "wall_seconds",
    "cpu_seconds",
    "contended_calls",
    "clean_wall_seconds",
    "clean_cpu_seconds",
    "clean_thread_seconds",
)


def _rss_mb() -> float | None:
    if _PROCESS is None:
        return None
    try:
        return _PROCESS.memory_info().rss / (1024 * 1024)
    except Exception:  # pragma: no cover - process gone or platform refusal
        return None


@dataclass(frozen=True)
class Sample:
    """What one measured call cost."""

    op: str
    wall_seconds: float
    cpu_seconds: float
    full_cpu_seconds: float
    contended: bool
    rss_mb: float | None = None


@dataclass
class Totals:
    """Accumulated cost. ``clean_*`` fields hold the uncontended subset only."""

    calls: int = 0
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    contended_calls: int = 0
    clean_wall_seconds: float = 0.0
    clean_cpu_seconds: float = 0.0
    clean_thread_seconds: float = 0.0

    def add(self, sample: Sample) -> None:
        self.calls += 1
        self.wall_seconds += sample.wall_seconds
        self.cpu_seconds += sample.cpu_seconds
        if sample.contended:
            self.contended_calls += 1
        else:
            self.clean_wall_seconds += sample.wall_seconds
            self.clean_cpu_seconds += sample.full_cpu_seconds
            self.clean_thread_seconds += sample.cpu_seconds

    def minus(self, other: Totals) -> Totals:
        """This total less *other*: the cost of the window between the two."""
        return Totals(**{f: getattr(self, f) - getattr(other, f) for f in _TOTAL_FIELDS})

    @property
    def cores_used(self) -> float | None:
        """Average cores saturated -- all-thread CPU over wall, uncontended calls only.

        Falls back to this thread's own CPU when every call was contended, which
        under-reports rather than inventing attribution it does not have.
        """
        if self.clean_wall_seconds > 1e-9:
            return self.clean_cpu_seconds / self.clean_wall_seconds
        if self.wall_seconds > 1e-9:
            return self.cpu_seconds / self.wall_seconds
        return None

    @property
    def engine_threads_internally(self) -> bool | None:
        """Does the engine burn CPU on threads of its own? ``None`` when unmeasured."""
        if self.clean_wall_seconds < _THREADED_MIN_WALL or self.clean_thread_seconds <= 1e-9:
            return None
        return self.clean_cpu_seconds / self.clean_thread_seconds > _THREADED_RATIO

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "calls": self.calls,
            "wall_seconds": round(self.wall_seconds, 4),
            "cpu_seconds": round(self.cpu_seconds, 4),
        }
        cores = self.cores_used
        if cores is not None:
            out["cpu_cores_used"] = round(cores, 3)
        if self.contended_calls:
            out["contended_calls"] = self.contended_calls
        return out


@dataclass
class SessionMetrics:
    """One session's running cost: totals, a per-operation breakdown, peak memory."""

    total: Totals = field(default_factory=Totals)
    by_op: dict[str, Totals] = field(default_factory=dict)
    peak_rss_mb: float = 0.0
    last: Sample | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, sample: Sample) -> None:
        with self.lock:
            self.total.add(sample)
            self.by_op.setdefault(sample.op, Totals()).add(sample)
            if sample.rss_mb is not None:
                self.peak_rss_mb = max(self.peak_rss_mb, sample.rss_mb)
            self.last = sample

    def snapshot(self) -> Totals:
        """A copy of the totals right now, for differencing with :meth:`Totals.minus`."""
        with self.lock:
            return replace(self.total)

    def as_dict(self, by_op: bool = False) -> dict[str, Any]:
        with self.lock:
            out = self.total.as_dict()
            if self.peak_rss_mb:
                out["peak_rss_mb"] = round(self.peak_rss_mb, 1)
            threaded = self.total.engine_threads_internally
            if threaded is not None:
                out["engine_threads_internally"] = threaded
            if by_op:
                ranked = sorted(self.by_op.items(), key=lambda kv: -kv[1].wall_seconds)
                out["by_op"] = {op: totals.as_dict() for op, totals in ranked}
        return out


@contextmanager
def measure(metrics: SessionMetrics, op: str) -> Iterator[None]:
    """Measure the enclosing block, in whichever thread runs it, into *metrics*."""
    global _inflight, _entries
    with _counter_lock:
        _inflight += 1
        _entries += 1
        inflight_at_start, entries_at_start = _inflight, _entries
    wall0, cpu0, full0 = time.monotonic(), _thread_time(), time.process_time()
    try:
        yield
    finally:
        wall = time.monotonic() - wall0
        cpu = _thread_time() - cpu0
        full = time.process_time() - full0
        with _counter_lock:
            contended = inflight_at_start > 1 or _inflight > 1 or _entries > entries_at_start
            _inflight -= 1
        try:
            rss = _rss_mb() if wall >= _RSS_MIN_WALL else None
            metrics.record(Sample(op, wall, cpu, full, contended, rss))
        except Exception:  # accounting must never break an engine call
            logger.debug("metrics recording failed", exc_info=True)


def measured(
    metrics: SessionMetrics | None, op: str, fn: Callable[..., T]
) -> Callable[..., T]:
    """Wrap *fn* so its cost lands in *metrics*, measured in the thread that runs it."""
    if metrics is None:
        return fn

    def run(*args: Any, **kwargs: Any) -> T:
        with measure(metrics, op):
            return fn(*args, **kwargs)

    return run


def op_label(fn: Callable[..., Any], context: str = "") -> str:
    """Name a measured call: its error context, else the callable's name.

    The step loop's state peeks are lambdas; they become one ``inline`` row,
    which is worth keeping -- it shows what the polling costs.
    """
    if context:
        return context
    name = getattr(fn, "__name__", "")
    return "inline" if not name or name == "<lambda>" else name.lstrip("_")


def process_snapshot() -> dict[str, Any]:
    """Whole-process CPU and memory, independent of any session."""
    out: dict[str, Any] = {
        "backend": "psutil" if _PROCESS is not None else "stdlib",
        "uptime_seconds": round(time.monotonic() - _STARTED, 1),
        "cpu_seconds_total": round(time.process_time(), 3),
    }
    if not _HAS_THREAD_CLOCK:  # pragma: no cover - not reachable on Windows or Linux
        out["degraded"] = "time.thread_time() is unavailable; per-call CPU is process-wide"
    if _PROCESS is None:
        out["hint"] = (
            "pip install 'openswmm.mcp[metrics]' adds resident memory and the "
            "user/system split"
        )
        return out
    try:
        cpu = _PROCESS.cpu_times()
        out.update(
            cpu_user_seconds=round(cpu.user, 3),
            cpu_system_seconds=round(cpu.system, 3),
            rss_mb=round(_PROCESS.memory_info().rss / (1024 * 1024), 1),
            threads=_PROCESS.num_threads(),
        )
    except Exception:  # pragma: no cover - process gone or platform refusal
        logger.debug("psutil process snapshot failed", exc_info=True)
    return out
