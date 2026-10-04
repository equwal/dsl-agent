"""Checks for server.py: a round trip, name validation, and one run over stdio."""

import asyncio
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import server

SERVER = str(Path(__file__).with_name("server.py"))
UPPER = "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read().upper())\n"


# One interpreter per extension. Each copies its program to stdout.
ECHO = {
    "awk": "{ print }\n",
    "sh": "cat\n",
    "lisp": "(loop for line = (read-line nil nil) while line do (write-line line))\n",
    "scm": "(use-modules (ice-9 rdelim))\n"
    "(let loop ((line (read-line)))\n"
    "  (unless (eof-object? line) (write-line line) (loop (read-line))))\n",
    "hs": "main :: IO ()\nmain = interact id\n",
    "clj": "(print (slurp *in*))\n(flush)\n",
    "js": "process.stdin.pipe(process.stdout);\n",
    "py": "import sys\nsys.stdout.write(sys.stdin.read())\n",
}


@pytest.fixture(scope="module", autouse=True)
def dsl_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    home = tmp_path_factory.mktemp("dsl")
    # Keep the overrides of this machine, so the runtime test checks them.
    mine = server.home() / "runtimes.json"
    table = json.loads(mine.read_bytes()) if mine.exists() else {}
    # "up" exists only in this override, so a run with it proves the override loads.
    (home / "runtimes.json").write_text(json.dumps(table | {"up": [sys.executable]}))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DSL_HOME", str(home))
        yield


@given(
    grammar=st.text(),
    ext=st.sampled_from(sorted(server.RUNTIMES)),
    source=st.text(),
)
def test_put_then_get_returns_the_same_dsl(grammar: str, ext: str, source: str) -> None:
    server.put("t", grammar, ext, source)
    assert server.get("t") == {"grammar": grammar, "ext": ext, "source": source}
    assert server.ls()["t"] == ext


@given(name=st.text().filter(lambda s: not server.NAME.fullmatch(s)))
@example("../evil")
@example("a/b")
@example("")
def test_put_rejects_a_name_that_is_not_a_slug(name: str) -> None:
    with pytest.raises(ValueError):
        server.put(name, "", "py", "")


@pytest.mark.parametrize("ext", sorted(server.RUNTIMES))
def test_each_installed_runtime_runs_an_interpreter(ext: str) -> None:
    if not server.runtimes()[ext]:
        pytest.skip(f"{server.table()[ext][0]} is not installed")
    server.put("echo", "program <- .*", ext, ECHO[ext])
    result = server.run("echo", "a b\n", timeout=60)
    assert (result["exit"], str(result["stdout"]).splitlines()) == (0, ["a b"]), result


def test_put_and_run_over_stdio() -> None:
    async def call() -> Any:
        params = StdioServerParameters(
            command=sys.executable, args=[SERVER], env=dict(os.environ)
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert tools == {"runtimes", "ls", "get", "put", "run"}
            dsl = {
                "name": "upper",
                "grammar": "text <- .*",
                "ext": "up",
                "source": UPPER,
            }
            await session.call_tool("put", dsl)
            result = await session.call_tool(
                "run", {"name": "upper", "program": "a b\n"}
            )
            assert not result.is_error
            return result.structured_content

    assert asyncio.run(call()) == {"stdout": "A B\n", "stderr": "", "exit": 0}
