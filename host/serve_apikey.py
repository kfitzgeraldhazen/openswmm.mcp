"""Run the OpenSWMM MCP server with static API-key authentication.

Fallback for when an Entra app registration is blocked by tenant policy. Gives the
server a real credential check without Azure, without an IdP, and without modifying
the upstream `openswmm.mcp` package.

This builds the normal server via `build_server()` and swaps its auth provider for
fastmcp's `StaticTokenVerifier`, so every MCP request must carry:

    Authorization: Bearer <OPENSWMM_MCP_API_KEY>

Usage:

    $env:OPENSWMM_MCP_API_KEY      = "<generated secret>"
    $env:OPENSWMM_MCP_TRANSPORT    = "http"
    $env:OPENSWMM_MCP_HTTP_PORT    = "8081"
    $env:OPENSWMM_MCP_WORKING_DIR  = "<scratch dir>"
    $env:OPENSWMM_MCP_READ_ROOTS   = "<optional extra readable dirs, ';'-separated>"
    python serve_apikey.py

It also installs `pathjail`, which confines writes to the working directory and
reads to the working directory plus `OPENSWMM_MCP_READ_ROOTS`. Without it the
working directory is only a default for relative paths, and absolute paths reach
anything the Windows account can reach — see APIKEY_SETUP.md §2.

See APIKEY_SETUP.md for the security trade-offs. This is prototype-grade auth:
one shared secret, no per-user identity. It is a large improvement on auth=None
and a downgrade from OAuth.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys

import pathjail

MIN_KEY_CHARS = 32
GENERATE_HINT = f'"{sys.executable}" -c "import secrets; print(secrets.token_urlsafe(32))"'
UPSTREAM_WARNING = "no auth is configured"


class _NoAuthWarningFilter(logging.Filter):
    """Drop upstream's "HTTP transport is active but no auth is configured" warning.

    `create_auth()` runs inside `build_server()` and truthfully reports that the
    *environment* configures no provider — it only knows about the JWKS/issuer
    settings. We attach a StaticTokenVerifier immediately afterwards, so the
    warning is accurate when logged and false by the time the server accepts a
    request. Leaving it in would train the reader to ignore it on the day it is
    real, which is the whole value of the warning.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return UPSTREAM_WARNING not in record.getMessage()


def _fail(msg: str, *extra: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    for line in extra:
        print(f"       {line}", file=sys.stderr)
    return 2


def main() -> int:
    key = os.environ.get("OPENSWMM_MCP_API_KEY", "").strip()
    if not key:
        return _fail(
            "OPENSWMM_MCP_API_KEY is not set.",
            "Generate one with:",
            f"  {GENERATE_HINT}",
        )
    if len(key) < MIN_KEY_CHARS:
        return _fail(
            f"OPENSWMM_MCP_API_KEY is {len(key)} characters; {MIN_KEY_CHARS} is the minimum.",
            "A guessable key is the same as no key once the URL is public.",
            f"  {GENERATE_HINT}",
        )

    # Install the filter before importing openswmm_mcp.server: that module runs
    # `mcp = build_server()` at import time (server.py:81), which fires the warning
    # once on its own, before any code here gets a chance to run. Importing
    # openswmm_mcp.auth alone builds nothing.
    from openswmm_mcp import auth as _auth

    _logger = getattr(_auth, "logger", None) or logging.getLogger("openswmm_mcp.auth")
    _logger.addFilter(_NoAuthWarningFilter())

    from openswmm_mcp.config import ServerSettings

    settings = ServerSettings()

    # build_server() registers run_python when enable_python is set AND transport is
    # stdio. Requiring http here keeps that branch unreachable; refusing enable_python
    # outright means it stays unreachable even if that logic changes upstream.
    if settings.transport != "http":
        return _fail(
            f"transport is {settings.transport!r}; this launcher is http-only.",
            "Set OPENSWMM_MCP_TRANSPORT=http (API-key auth is meaningless over stdio).",
        )
    if settings.enable_python:
        return _fail(
            "refusing to start with OPENSWMM_MCP_ENABLE_PYTHON=true.",
            "run_python executes arbitrary code with the Solver in scope.",
        )

    # The jail must be installed before openswmm_mcp.server, which imports
    # openswmm_mcp.tools.model and .results at module scope; both bind
    # resolve_path with a from-import, so a later patch would never reach them.
    try:
        jail = pathjail.install(settings.working_dir, pathjail.read_roots_from_env())
    except (RuntimeError, ValueError) as exc:
        return _fail(str(exc))

    from openswmm_mcp.server import build_server

    # install() guards the ordering going in; verify() confirms it coming out, so a
    # future upstream refactor surfaces here rather than halfway through a demo.
    try:
        pathjail.verify()
    except RuntimeError as exc:
        return _fail(str(exc))

    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    host = os.environ.get("FASTMCP_HOST", "127.0.0.1")
    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:8]

    server = build_server(settings)
    server.auth = StaticTokenVerifier(
        tokens={key: {"client_id": "copilot-studio", "scopes": ["openswmm"]}}
    )

    # flush: stdout is block-buffered when redirected to a pipe or file, and this
    # banner is the only confirmation that auth is on.
    print("OpenSWMM MCP - static API-key auth ENABLED", flush=True)
    print(f"  bind         http://{host}:{settings.http_port}/mcp", flush=True)
    print(f"  working dir  {settings.working_dir}", flush=True)
    print(f"  jail         writes  {jail['write_roots'][0]}", flush=True)
    first_read, *other_reads = jail["read_roots"]
    print(f"               reads   {first_read}", flush=True)
    for extra_read in other_reads:
        print(f"                       {extra_read}", flush=True)
    print(f"  key          {len(key)} chars, sha256:{fingerprint}", flush=True)
    print("  header       Authorization: Bearer <key>", flush=True)
    print(f"  note         upstream's {UPSTREAM_WARNING!r} warning is suppressed here", flush=True)
    print("               (it fires before the verifier is attached, and is wrong)", flush=True)
    for warning in jail["warnings"]:
        print(f"  WARNING      {warning}", flush=True)
    if host not in ("127.0.0.1", "::1"):
        print(f"  WARNING      bound to {host} - reachable from the local network", flush=True)
    print(flush=True)

    server.run(transport="http", host=host, port=settings.http_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
