# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3"]
# ///
"""MCP server: a registry of DSLs.

Each DSL is a folder in $DSL_HOME (default ~/.dsl) that holds a grammar and
one interpreter file, impl.<ext>. `run` pipes a program to that interpreter.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

# The client sees the message of a ToolError. For any other exception it sees
# only "Error executing tool <name>".
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer("dsl")

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
LIMIT = 100_000  # characters of stdout, and of stderr, that `run` returns

# Interpreter argv for each impl extension. `run` appends the impl path.
RUNTIMES: dict[str, list[str]] = {
    "awk": ["awk", "-f"],
    "sh": ["sh"],
    "lisp": ["sbcl", "--script"],
    "scm": ["guile", "--no-auto-compile", "-s"],
    "hs": ["runghc"],
    "clj": ["bb"],
    "js": ["node"],
    "py": [sys.executable],
}


def home() -> Path:
    return Path(os.environ.get("DSL_HOME") or Path.home() / ".dsl")


def table() -> dict[str, list[str]]:
    """RUNTIMES, plus the entries of $DSL_HOME/runtimes.json when it exists."""
    override = home() / "runtimes.json"
    extra: dict[str, list[str]] = (
        json.loads(override.read_bytes()) if override.exists() else {}
    )
    return RUNTIMES | extra


def folder(name: str) -> Path:
    # The name becomes a path segment, so reject anything that can leave home().
    if not NAME.fullmatch(name):
        raise ToolError(f"name must match {NAME.pattern}")
    return home() / name


def impl(name: str) -> Path:
    found = sorted(folder(name).glob("impl.*"))
    if not found:
        raise ToolError(f"no DSL named {name}")
    return found[0]


@mcp.tool()
def runtimes() -> dict[str, bool]:
    """Map each impl extension to whether its interpreter is on PATH."""
    return {ext: shutil.which(argv[0]) is not None for ext, argv in table().items()}


@mcp.tool()
def ls() -> dict[str, str]:
    """Map each stored DSL name to the extension of its interpreter."""
    return {f.parent.name: f.suffix[1:] for f in sorted(home().glob("*/impl.*"))}


@mcp.tool()
def get(name: str) -> dict[str, str]:
    """Return the grammar, the extension and the interpreter source of a DSL."""
    f = impl(name)
    return {
        "grammar": (f.parent / "grammar").read_bytes().decode(),
        "ext": f.suffix[1:],
        "source": f.read_bytes().decode(),
    }


@mcp.tool()
def put(name: str, grammar: str, ext: str, source: str) -> str:
    """Store or replace a DSL.

    `source` is the interpreter, in the language of `ext` (a key of `runtimes`).
    It reads a program on stdin, writes the result to stdout, and exits non-zero
    with a message on stderr for a bad program.
    """
    if ext not in table():
        raise ToolError(f"ext must be one of {sorted(table())}")
    d = folder(name)
    d.mkdir(parents=True, exist_ok=True)
    new = d / f"impl.{ext}"
    (d / "grammar").write_bytes(grammar.encode())
    new.write_bytes(source.encode())
    for old in d.glob("impl.*"):
        if old != new:
            old.unlink()
    return f"{name}.{ext}"


@mcp.tool()
def run(name: str, program: str, timeout: float = 10) -> dict[str, str | int]:
    """Run a program through the interpreter of a DSL.

    The interpreter starts in the working directory of the server.
    Return stdout, stderr and the exit code.
    """
    f = impl(name)
    argv = table().get(f.suffix[1:])
    if argv is None:
        raise ToolError(f"no runtime for {f.name}")
    exe = shutil.which(argv[0])
    if exe is None:
        raise ToolError(f"{argv[0]} is not on PATH")
    # ponytail: captures all output in memory, then cuts it to LIMIT.
    # Read through a capped pipe if a DSL can flood stdout within the timeout.
    try:
        done = subprocess.run(
            [exe, *argv[1:], str(f)],
            input=program.encode(),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"timed out after {timeout} s") from None
    return {
        "stdout": done.stdout.decode(errors="replace")[:LIMIT],
        "stderr": done.stderr.decode(errors="replace")[:LIMIT],
        "exit": done.returncode,
    }


if __name__ == "__main__":
    mcp.run()
