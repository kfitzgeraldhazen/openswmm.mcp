"""Per-call cost accounting: the two clocks, the contention rule, and the surfaces.

The accounting cases are pure; the ones that price a real simulation drive the
server through the in-memory client, the way a client would.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from openswmm_mcp.metrics import (
    COST_NOTE,
    Sample,
    SessionMetrics,
    Totals,
    measure,
    measured,
    op_label,
    process_snapshot,
)


def _burn(seconds: float = 0.1) -> int:
    """Spin, so the CPU clock moves as well as the wall clock.

    Long enough to clear the scheduler-tick granularity of the per-thread clock
    on Windows (~16 ms), which a shorter burn can round away to zero.
    """
    end, n = time.monotonic() + seconds, 0
    while time.monotonic() < end:
        n += 1
    return n


# ---------------------------------------------------------------------------
# The clocks
# ---------------------------------------------------------------------------
def test_a_busy_block_records_cpu_and_wall():
    metrics = SessionMetrics()
    with measure(metrics, "burn"):
        _burn()

    assert metrics.total.calls == 1
    assert metrics.total.wall_seconds > 0.0
    assert metrics.total.cpu_seconds > 0.0
    # One busy thread cannot burn more CPU than wall time.
    assert metrics.total.cpu_seconds <= metrics.total.wall_seconds + 0.02


def test_an_idle_block_costs_wall_but_not_cpu():
    """The distinction the whole module exists for: waiting is not computing."""
    metrics = SessionMetrics()
    with measure(metrics, "sleep"):
        time.sleep(0.1)

    assert metrics.total.wall_seconds > 0.08
    assert metrics.total.cpu_seconds < 0.03


def test_cost_is_attributed_per_operation():
    metrics = SessionMetrics()
    for _ in range(3):
        with measure(metrics, "stepping"):
            _burn(0.01)
    with measure(metrics, "finishing"):
        _burn(0.01)

    assert metrics.total.calls == 4
    assert metrics.by_op["stepping"].calls == 3
    assert metrics.by_op["finishing"].calls == 1
    assert sorted(metrics.as_dict(by_op=True)["by_op"]) == ["finishing", "stepping"]


# ---------------------------------------------------------------------------
# The contention rule -- when process-wide CPU may be attributed to a session
# ---------------------------------------------------------------------------
def test_overlapping_calls_are_flagged_contended():
    metrics = SessionMetrics()
    gate = threading.Barrier(2)

    def work():
        with measure(metrics, "parallel"):
            gate.wait(timeout=5)  # both inside measure() at once
            _burn(0.02)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert metrics.total.calls == 2
    assert metrics.total.contended_calls == 2
    # Contended calls contribute nothing to the all-thread accumulator.
    assert metrics.total.clean_wall_seconds == 0.0
    assert "contended_calls" in metrics.as_dict()


def test_a_call_nested_entirely_inside_another_counts_as_overlap():
    """The entry counter catches an overlap the in-flight count alone would miss."""
    metrics = SessionMetrics()
    with measure(metrics, "outer"):
        with measure(metrics, "inner"):
            _burn(0.01)
        _burn(0.01)

    assert metrics.total.contended_calls == 2


def test_sequential_calls_are_not_contended():
    metrics = SessionMetrics()
    for _ in range(2):
        with measure(metrics, "serial"):
            _burn(0.01)

    assert metrics.total.contended_calls == 0
    assert metrics.total.clean_wall_seconds > 0.0


# ---------------------------------------------------------------------------
# Accounting must never break an engine call
# ---------------------------------------------------------------------------
def test_recording_failure_never_breaks_the_call():
    metrics = SessionMetrics()

    def explode(sample):
        raise RuntimeError("accounting is broken")

    metrics.record = explode  # type: ignore[method-assign]
    assert measured(metrics, "op", lambda: 42)() == 42


def test_a_failing_call_is_still_measured():
    metrics = SessionMetrics()

    def boom():
        raise ValueError("engine said no")

    with pytest.raises(ValueError):
        measured(metrics, "boom", boom)()
    assert metrics.by_op["boom"].calls == 1


def test_measured_without_a_sink_returns_the_function_untouched():
    def fn():
        return 1

    assert measured(None, "op", fn) is fn


# ---------------------------------------------------------------------------
# Totals: windowing and the cores_used arithmetic
# ---------------------------------------------------------------------------
def test_minus_gives_the_cost_of_one_window():
    metrics = SessionMetrics()
    with measure(metrics, "before"):
        _burn(0.05)
    mark = metrics.snapshot()
    with measure(metrics, "window"):
        _burn(0.05)

    spent = metrics.snapshot().minus(mark)
    assert spent.calls == 1
    assert spent.wall_seconds > 0.0
    assert spent.wall_seconds < metrics.total.wall_seconds


def test_snapshot_is_a_copy_not_a_view():
    metrics = SessionMetrics()
    mark = metrics.snapshot()
    with measure(metrics, "after"):
        _burn(0.01)
    assert mark.calls == 0


def test_cores_used_is_none_until_something_is_measured():
    assert Totals().cores_used is None
    assert Totals().engine_threads_internally is None


def test_cores_used_prefers_the_all_thread_clock():
    """One uncontended second of wall in which the process burned three CPU seconds."""
    totals = Totals()
    totals.add(Sample("run", 1.0, 1.0, 3.0, contended=False))

    assert totals.cores_used == pytest.approx(3.0)
    assert totals.engine_threads_internally is True


def test_cores_used_falls_back_when_every_call_overlapped():
    totals = Totals()
    totals.add(Sample("run", 2.0, 1.0, 9.9, contended=True))

    # The 9.9 s belongs to nobody in particular, so the own-thread ratio is used.
    assert totals.cores_used == pytest.approx(0.5)
    assert totals.engine_threads_internally is None


def test_a_single_threaded_engine_is_reported_as_such():
    totals = Totals()
    totals.add(Sample("run", 1.0, 1.0, 1.0, contended=False))

    assert totals.engine_threads_internally is False


def test_peak_memory_is_the_maximum_not_the_last():
    metrics = SessionMetrics()
    for rss in (100.0, 400.0, 150.0):
        metrics.record(Sample("op", 0.1, 0.1, 0.1, contended=False, rss_mb=rss))

    assert metrics.as_dict()["peak_rss_mb"] == 400.0


# ---------------------------------------------------------------------------
# Labels and the process snapshot
# ---------------------------------------------------------------------------
def test_op_label_prefers_the_error_context():
    assert op_label(lambda: None, "stepping") == "stepping"


def test_lambdas_collapse_into_one_inline_row():
    assert op_label(lambda: None) == "inline"


def test_named_helpers_keep_their_name_without_the_underscore():
    def _reopen():
        pass

    assert op_label(_reopen) == "reopen"


def test_process_snapshot_reports_a_backend_and_cpu():
    snapshot = process_snapshot()

    assert snapshot["backend"] in ("psutil", "stdlib")
    assert snapshot["cpu_seconds_total"] >= 0.0
    assert snapshot["uptime_seconds"] >= 0.0


def test_memory_appears_with_psutil_and_is_signposted_without_it():
    snapshot = process_snapshot()

    if snapshot["backend"] == "psutil":
        assert snapshot["rss_mb"] > 0.0
        assert snapshot["threads"] >= 1
    else:
        assert "openswmm.mcp[metrics]" in snapshot["hint"]


# ---------------------------------------------------------------------------
# The cost of a real simulation, through the MCP surface
# ---------------------------------------------------------------------------
async def test_run_reports_what_it_cost(tools, inp_path):
    await tools("open_model", path=inp_path, session_id="cost")
    result = await tools("run", session_id="cost")

    assert result["finished"] is True
    assert result["steps"] > 0
    assert result["wall_seconds"] > 0.0
    assert result["cpu_seconds"] > 0.0
    assert result["cpu_cores_used"] > 0.0


async def test_each_run_is_priced_on_its_own_window(tools, inp_path):
    """A second run must report its own cost, not the session's running total."""
    await tools("open_model", path=inp_path, session_id="cost")
    first = await tools("run", session_id="cost", until="+6h")
    second = await tools("run", session_id="cost", until="+12h")

    lifetime = (await tools("session", action="state", session_id="cost"))["cost"]
    assert second["cpu_seconds"] > 0.0
    # Both windows plus the open and the state read fit inside the lifetime total.
    assert first["cpu_seconds"] + second["cpu_seconds"] <= lifetime["cpu_seconds"] + 0.05


async def test_opening_and_stepping_are_accounted_separately(tools, inp_path):
    await tools("open_model", path=inp_path, session_id="cost")
    await tools("run", session_id="cost")

    raw = await tools.client.read_resource("swmm://session/cost/cost")
    detail = json.loads(raw[0].text)

    assert detail["session_id"] == "cost"
    assert detail["by_op"]["stepping"]["cpu_seconds"] > 0.0
    assert "open_model" in detail["by_op"]
    assert detail["note"] == COST_NOTE
    # One session, one request at a time: every call is attributable.
    assert "contended_calls" not in detail


async def test_session_state_carries_the_running_cost(tools, inp_path):
    await tools("open_model", path=inp_path, session_id="cost")
    await tools("run", session_id="cost")
    state = await tools("session", action="state", session_id="cost")

    assert state["cost"]["calls"] > 0
    assert state["cost"]["cpu_seconds"] > 0.0
    assert "counts" in state  # the summary it is attached to survives


async def test_metrics_resource_covers_process_and_sessions(tools, inp_path):
    await tools("open_model", path=inp_path, session_id="cost")
    await tools("run", session_id="cost")

    raw = await tools.client.read_resource("swmm://metrics")
    overall = json.loads(raw[0].text)

    assert overall["process"]["backend"] in ("psutil", "stdlib")
    assert overall["sessions"]["cost"]["cpu_seconds"] > 0.0
    assert overall["note"] == COST_NOTE


async def test_metrics_cost_nothing_when_no_model_is_open(tools):
    raw = await tools.client.read_resource("swmm://metrics")
    overall = json.loads(raw[0].text)

    assert overall["sessions"] == {}
    assert overall["process"]["cpu_seconds_total"] >= 0.0
