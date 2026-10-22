"""MCP resources: the engine catalog, live session summaries and cost (``swmm://`` URIs)."""

from __future__ import annotations

import json

from fastmcp import Context, FastMCP

from openswmm_mcp import catalog as cat
from openswmm_mcp.dependencies import get_session, get_session_manager
from openswmm_mcp.metrics import COST_NOTE, process_snapshot
from openswmm_mcp.tools.model import summary

resources_mcp = FastMCP("resources")


@resources_mcp.resource("swmm://catalog")
async def catalog_index() -> str:
    """Every target the engine exposes, with its class and one-line description."""
    return json.dumps(
        {name: {"class": e["class"], "doc": e["doc"][:200]} for name, e in cat.targets().items()},
        indent=1,
    )


@resources_mcp.resource("swmm://catalog/{target}")
async def catalog_target(target: str) -> str:
    """Full catalog entries (fields and methods) for one target, e.g. ``node`` or ``forcing``."""
    cat.require_target(target)
    return json.dumps(
        {"target": cat.targets()[target], "members": list(cat.members(target).values())}, indent=1
    )


@resources_mcp.resource("swmm://sessions")
async def sessions(ctx: Context) -> str:
    """All open sessions with their state and input file."""
    return json.dumps(
        [
            {"session_id": s.session_id, "state": s.state, "inp": s.inp_path}
            for s in await get_session_manager(ctx).sessions()
        ],
        indent=1,
    )


@resources_mcp.resource("swmm://session/{session_id}/summary")
async def session_summary(session_id: str, ctx: Context) -> str:
    """Counts, key options, time window and file paths for one session."""
    session = await get_session(ctx, session_id)
    return json.dumps(await session.call(summary, session), indent=1)


@resources_mcp.resource("swmm://metrics")
async def metrics(ctx: Context) -> str:
    """CPU, wall time and memory used by the server process and by each open session."""
    sessions = await get_session_manager(ctx).sessions()
    return json.dumps(
        {
            "process": process_snapshot(),
            "sessions": {s.session_id: s.metrics.as_dict() for s in sessions},
            "note": COST_NOTE,
        },
        indent=1,
    )


@resources_mcp.resource("swmm://session/{session_id}/cost")
async def session_cost(session_id: str, ctx: Context) -> str:
    """One session's CPU, wall time and memory cost, broken down by operation."""
    session = await get_session(ctx, session_id)
    return json.dumps(
        {"session_id": session_id, **session.metrics.as_dict(by_op=True), "note": COST_NOTE},
        indent=1,
    )
