"""Confine the OpenSWMM MCP server's file access to permitted roots.

`OPENSWMM_MCP_WORKING_DIR` is not a sandbox. Upstream resolves a caller-supplied
absolute path and hands it back untouched, so `save` and `export` write anywhere
the Windows account can write and `open_model` reads anything it can read. That
was verified on 2026-10-02; the whole mechanism is two lines:

    openswmm_mcp/_util/validation.py:36   if expanded.is_absolute(): return expanded.resolve()
    openswmm_mcp/catalog.py:224           return str(path if path.is_absolute() else ...)

This module replaces both with containment-checked versions at runtime. Nothing
in the vendored `openswmm_mcp` tree is modified on disk — the patch lives in
memory and only for the life of the process, the same approach `serve_apikey.py`
already uses to attach `StaticTokenVerifier`.

Policy
------
writes   working directory only             `save`, `export`, and every `call` path
reads    working directory + READ_ROOTS     `open_model`, result readers, `compare`

Extra read roots are a path-separator-delimited list (``;`` on Windows)::

    set OPENSWMM_MCP_READ_ROOTS=;

What this does not do: the API key is still the only identity gate, and a key
holder can still make the server run SWMM and consume CPU. This closes the file
hole, not the authentication one.
"""

from __future__ import annotations

import functools
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from fastmcp.exceptions import ToolError

READ_ROOTS_ENV = "OPENSWMM_MCP_READ_ROOTS"

# The enclosing function of every resolve_path() call site in openswmm_mcp.tools,
# verified against openswmm.mcp 0.2.0.dev0 / openswmm 6.0.0a4.dev1:
#
#     model.py:207    open_model      read
#     model.py:389    save            WRITE
#     results.py:62   _with_reader    read
#     results.py:387  compare         read
#     results.py:440  export          WRITE
#
# resolve_path() cannot tell a read from a write on its own — the two call sites
# are textually identical — so the jail dispatches on the calling function's
# name. Anything not listed here is refused outright, so an upstream rename
# fails loudly rather than silently handing a write the read policy.
_WRITE_CALLERS = frozenset({"save", "export"})
_READ_CALLERS = frozenset({"open_model", "_with_reader", "compare"})


def _norm(path: Path | str) -> str:
    """Case- and separator-normalised string form, for prefix comparison."""
    return os.path.normcase(os.path.normpath(str(path)))


def _within(candidate: Path, root: Path) -> bool:
    """True if *candidate* is *root* or sits underneath it.

    `Path.is_relative_to` is not usable here: it compares case-sensitively, so a
    root of ``...\\scratch`` would reject ``...\\Scratch\\model.inp`` on Windows,
    where those name the same directory. `os.path.normcase` folds that away.

    Both sides are resolved before they reach this function, so a junction or
    symlink planted inside the working directory that points at `C:\\Windows`
    resolves outward and fails the check.
    """
    c, r = _norm(candidate), _norm(root)
    return c == r or c.startswith(r + os.sep)


def _enforce(resolved: Path, roots: Sequence[Path], mode: str, requested: str) -> Path:
    """Return *resolved* if it lies within *roots*, else raise.

    The message deliberately echoes only what the caller already supplied. Naming
    the resolved path would confirm where a traversal attempt actually landed.
    """
    if any(_within(resolved, root) for root in roots):
        return resolved

    permitted = "; ".join(str(r) for r in roots)
    hint = (
        f" Ask the server owner to add it to {READ_ROOTS_ENV} if it should be readable."
        if mode == "read"
        else " Writes are confined to the working directory."
    )
    raise ToolError(
        f"Path refused: {requested!r} resolves outside the permitted {mode} roots."
        f" Permitted: {permitted}.{hint}"
    )


def read_roots_from_env(raw: str | None = None) -> list[Path]:
    """Parse `OPENSWMM_MCP_READ_ROOTS` into a list of resolved directories."""
    if raw is None:
        raw = os.environ.get(READ_ROOTS_ENV)
    if not raw:
        return []
    return [
        Path(part.strip().strip('"')).expanduser().resolve()
        for part in raw.split(os.pathsep)
        if part.strip()
    ]


def _audit_roots(roots: Sequence[Path]) -> list[str]:
    """Reject read roots that would defeat the jail; warn about broad ones."""
    warnings: list[str] = []
    profile = Path("~").expanduser().resolve()

    for root in roots:
        is_anchor = root.parent == root
        is_unc = str(root).startswith("\\\\")

        if is_anchor and not is_unc:
            raise ValueError(
                f"{READ_ROOTS_ENV} contains the filesystem root {root}, which grants "
                "read access to the entire drive and defeats the jail."
            )
        if is_anchor and is_unc:
            warnings.append(f"{root} is a whole network share - that is a very broad grant.")
        elif _norm(root) == _norm(profile):
            warnings.append(f"{root} is your user profile root - the read jail barely narrows anything.")
        elif not root.is_dir():
            warnings.append(f"{root} does not exist or is not a directory - it grants nothing.")

    return warnings


def install(working_dir: Path | str, read_roots: Sequence[Path | str] = ()) -> dict[str, object]:
    """Patch upstream's two path resolvers. Call BEFORE importing openswmm_mcp.server.

    `openswmm_mcp.tools.model` and `.results` bind `resolve_path` with a
    from-import, so the name is captured at *their* import time. Patching after
    they load would leave the original in place and the jail would do nothing.
    `openswmm_mcp.server` imports both at module scope, so this has to run first
    — hence the guard below rather than a silent no-op.

    `catalog.coerce` has no such constraint: `access.py` reaches it as `cat.coerce`,
    an attribute lookup at call time. It is patched here only for symmetry.

    Returns the effective roots and any non-fatal warnings, for the banner.
    """
    too_late = [
        name
        for name in ("openswmm_mcp.tools.model", "openswmm_mcp.tools.results")
        if name in sys.modules
    ]
    if too_late:
        raise RuntimeError(
            "path jail installed too late: " + ", ".join(too_late) + " is already "
            "imported. install() must run before openswmm_mcp.server is imported."
        )

    work = Path(working_dir).expanduser().resolve()
    extra = [Path(r).expanduser().resolve() for r in read_roots]
    warnings = _audit_roots(extra)

    writes = [work]
    reads = [work, *extra]

    from openswmm_mcp._util import validation

    original_resolve = validation.resolve_path

    @functools.wraps(original_resolve)
    def jailed_resolve_path(path: str, working_dir_arg: str) -> Path:
        resolved = original_resolve(path, working_dir_arg)
        caller = sys._getframe(1).f_code.co_name
        if caller in _WRITE_CALLERS:
            return _enforce(resolved, writes, "write", path)
        if caller in _READ_CALLERS:
            return _enforce(resolved, reads, "read", path)
        raise ToolError(
            f"Path refused: resolve_path was called from an unrecognised function "
            f"({caller!r}). The path jail fails closed; this build of openswmm.mcp "
            "is newer than the jail's caller map and needs re-verifying."
        )

    validation.resolve_path = jailed_resolve_path

    from openswmm_mcp import catalog

    original_coerce = catalog.coerce
    path_params = catalog._PATH_PARAMS

    @functools.wraps(original_coerce)
    def jailed_coerce(value, type_str, name="", working_dir=None):
        out = original_coerce(value, type_str, name, working_dir)
        # Mirror upstream's own test for "this argument is a path" (catalog.py:218)
        # rather than inferring it from the return value.
        is_path_arg = (
            working_dir is not None
            and isinstance(value, str)
            and isinstance(out, str)
            and ("PathLike" in (type_str or "") or path_params.search(name))
        )
        if is_path_arg:
            # Write policy, not read: `call` reaches engine writers, notably
            # call(target='builder', method='write', args={'path': ...}) at
            # access.py:528. Readers routed through `call` (output, geopackage,
            # hotstart) are therefore confined to the working directory too.
            _enforce(Path(out), writes, "write", value)
        return out

    catalog.coerce = jailed_coerce

    return {"write_roots": writes, "read_roots": reads, "warnings": warnings}


def verify() -> None:
    """Confirm the patch actually took. Call AFTER importing openswmm_mcp.server.

    `install()` guards the ordering going in; this checks it coming out, so a
    future upstream refactor surfaces at startup instead of mid-demo.
    """
    from openswmm_mcp import catalog
    from openswmm_mcp._util import validation
    from openswmm_mcp.tools import model, results

    for module in (model, results):
        if module.resolve_path is not validation.resolve_path:
            raise RuntimeError(
                f"{module.__name__} still holds the unpatched resolve_path - the jail "
                "was installed after that module was imported, and is not in effect."
            )

    if getattr(catalog.coerce, "__wrapped__", None) is None:
        raise RuntimeError("catalog.coerce is not patched; the `call` tool is unjailed.")

    known = _WRITE_CALLERS | _READ_CALLERS
    missing = sorted(n for n in known if not (hasattr(model, n) or hasattr(results, n)))
    if missing:
        raise RuntimeError(
            "the jail's caller map does not match this openswmm.mcp build; missing: "
            + ", ".join(missing)
            + ". Re-verify the resolve_path call sites before trusting the jail."
        )
