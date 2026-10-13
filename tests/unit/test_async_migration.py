from __future__ import annotations

import ast

from codeshift.analysis.ast_analyzer import parse_module
from codeshift.analysis.callgraph import CallGraph
from codeshift.config import LLMConfig
from codeshift.llm.sample_provider import SampleProvider
from codeshift.models.migrations import LLMUsage
from codeshift.rules.registry import AnalysisContext, build_registry
from codeshift.transformations.async_migration import AsyncMigrator, unawaited_calls
from codeshift.transformations.llm_transformer import LLMTransformer


def modules(files: dict[str, str]):
    return {path: parse_module(path, source) for path, source in files.items()}


def migrate(files: dict[str, str], with_provider: bool = True):
    transformer = (
        LLMTransformer(SampleProvider(), LLMConfig(), LLMUsage()) if with_provider else None
    )
    return AsyncMigrator(modules(files), transformer).run()


CLIENT = """import time

import requests


def fetch(url):
    response = requests.get(url, timeout=3)
    return response.json()


def fetch_twice(url):
    first = fetch(url)
    time.sleep(0.1)
    return [first, fetch(url)["items"][0], fetch(url).get("x")]
"""


def test_call_graph_resolves_imports_methods_and_blocking_calls():
    graph = CallGraph(
        modules(
            {
                "pkg/client.py": CLIENT,
                "pkg/service.py": (
                    "from pkg import client\n"
                    "from pkg.client import fetch as get\n"
                    "class Service:\n"
                    "    def load(self):\n"
                    "        return self.parse(get('u'))\n"
                    "    def parse(self, data):\n"
                    "        return client.fetch_twice(data)\n"
                ),
            }
        )
    )
    fetch = graph.functions["pkg/client.py::fetch"]
    assert [(c.qualified, c.kind) for c in fetch.blocking] == [("requests.get", "http")]
    callers = {(s.caller, s.callee) for s in graph.call_sites}
    assert ("pkg/service.py::Service.load", "pkg/client.py::fetch") in callers
    assert ("pkg/service.py::Service.load", "pkg/service.py::Service.parse") in callers
    assert ("pkg/service.py::Service.parse", "pkg/client.py::fetch_twice") in callers


def test_converts_function_and_propagates_await_through_callers():
    result = migrate({"client.py": CLIENT})
    source = result.sources["client.py"]

    assert "async def fetch(url):" in source
    assert "async def fetch_twice(url):" in source
    assert "async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:" in source
    assert "response = await client.get(url, timeout=3)" in source
    assert "first = await fetch(url)" in source
    assert "await asyncio.sleep(0.1)" in source
    # await binds looser than subscript/attribute access, so parentheses are required.
    assert '(await fetch(url))["items"][0]' in source
    assert '(await fetch(url)).get("x")' in source
    assert "import requests" not in source and "import time" not in source
    assert "import httpx" in source and "import asyncio" in source
    ast.parse(source)
    assert sorted(result.converted) == ["client.py::fetch", "client.py::fetch_twice"]
    assert result.groups[0].converted and result.groups[0].used_llm
    assert unawaited_calls(CallGraph(modules({"client.py": source})), set(result.converted)) == []


def test_sleep_only_function_needs_no_provider():
    source = "import time\n\n\ndef wait():\n    time.sleep(1)\n\n\nasync def main():\n    wait()\n"
    result = migrate({"m.py": source}, with_provider=False)
    out = result.sources["m.py"]
    assert "async def wait():\n    await asyncio.sleep(1)" in out
    assert "    await wait()" in out
    assert result.groups[0].used_llm is False


def test_blocking_sleep_inside_existing_async_function_is_fixed():
    source = "import time\n\n\nasync def tick():\n    time.sleep(1)\n    return 1\n"
    result = migrate({"m.py": source}, with_provider=False)
    assert "    await asyncio.sleep(1)" in result.sources["m.py"]
    findings = [
        f.rule_id
        for rule in build_registry().select(["sync-to-async"])
        for f in rule.detect(
            parse_module("m.py", source),
            AnalysisContext(graph=CallGraph(modules({"m.py": source}))),
        )
    ]
    assert findings == ["async.blocking-call-in-async"]


def test_http_function_without_provider_becomes_a_proposal():
    result = migrate({"client.py": CLIENT}, with_provider=False)
    assert result.sources == {}
    assert result.converted == []
    assert result.groups[0].converted is False
    assert "requires an LLM provider" in result.groups[0].blockers[0]
    assert len(result.proposals) == 1


def test_test_callers_are_wrapped_with_asyncio_run():
    files = {
        "client.py": "import time\n\n\ndef wait(n):\n    time.sleep(n)\n    return n\n",
        "tests/test_client.py": (
            "from client import wait\n\n\ndef test_wait():\n    assert wait(0) == 0\n"
        ),
    }
    result = migrate(files, with_provider=False)
    test_source = result.sources["tests/test_client.py"]
    assert "assert asyncio.run(wait(0)) == 0" in test_source
    assert "import asyncio" in test_source
    assert "def test_wait():" in test_source and "async def test_wait" not in test_source


BLOCKED_CASES = {
    "callback": (
        "import time\n\ndef wait(x):\n    time.sleep(x)\n\ndef run(xs):\n    return list(map(wait, xs))\n",
        "passed around as a value",
    ),
    "lambda": (
        "import time\n\ndef wait(x):\n    time.sleep(x)\n\nhandler = lambda: wait(1)\n",
        "inside a lambda",
    ),
    "generator expression": (
        "import time\n\ndef wait(x):\n    time.sleep(x)\n\ndef run(xs):\n    return sum(wait(x) for x in xs)\n",
        "inside a generator expression",
    ),
    "module level call": (
        "import time\n\ndef wait(x):\n    time.sleep(x)\n\nwait(1)\n",
        "called at module level",
    ),
    "generator function": (
        "import time\n\ndef ticks():\n    time.sleep(1)\n    yield 1\n",
        "generator function",
    ),
    "unknown decorator": (
        "import time\nimport functools\n\n@functools.cache\ndef wait():\n    time.sleep(1)\n",
        "decorator @functools.cache",
    ),
    "dunder": (
        "import time\n\nclass A:\n    def __init__(self):\n        time.sleep(1)\n",
        "special method",
    ),
    "subclass method": (
        "import time\n\nclass Base: pass\n\nclass A(Base):\n    def wait(self):\n        time.sleep(1)\n",
        "base classes",
    ),
    "unsupported blocking call": (
        "import time\nimport subprocess\n\ndef run():\n    time.sleep(1)\n    subprocess.run(['ls'])\n",
        "no supported async equivalent",
    ),
}


def test_unsafe_conversions_are_refused_with_a_reason():
    for name, (source, reason) in BLOCKED_CASES.items():
        result = migrate({"m.py": source}, with_provider=False)
        assert result.sources == {}, name
        assert result.converted == [], name
        assert any(reason in blocker for blocker in result.groups[0].blockers), (
            name,
            result.groups[0].blockers,
        )


def test_blocked_caller_blocks_the_whole_group():
    source = (
        "import time\n\n"
        "def wait(x):\n    time.sleep(x)\n\n"
        "def ok():\n    wait(1)\n\n"
        "def bad(xs):\n    return any(wait(x) for x in xs)\n"
    )
    result = migrate({"m.py": source}, with_provider=False)
    assert result.sources == {}
    assert set(result.proposals[0].functions) == {"m.py::wait", "m.py::ok", "m.py::bad"}


def test_fastapi_route_handlers_may_become_async_but_flask_routes_may_not():
    fastapi_app = (
        "import time\nfrom fastapi import FastAPI\n\napp = FastAPI()\n\n"
        "@app.get('/x')\ndef handler():\n    time.sleep(1)\n    return {}\n"
    )
    flask_app = (
        "import time\nfrom flask import Flask\n\napp = Flask(__name__)\n\n"
        "@app.get('/x')\ndef handler():\n    time.sleep(1)\n    return {}\n"
    )
    converted = migrate({"m.py": fastapi_app}, with_provider=False)
    assert "@app.get('/x')\nasync def handler():" in converted.sources["m.py"]
    refused = migrate({"m.py": flask_app}, with_provider=False)
    assert refused.sources == {}
    assert "decorator @app.get" in refused.groups[0].blockers[0]


def test_sample_provider_refuses_what_it_cannot_migrate():
    source = (
        "import requests\n\n\ndef fetch(url):\n    return requests.get(url, verify=False).json()\n"
    )
    result = migrate({"m.py": source})
    assert result.sources == {}
    assert "no per-request httpx equivalent" in result.groups[0].blockers[0]


def test_mocks_of_requests_in_tests_are_flagged():
    files = {
        "client.py": CLIENT,
        "tests/test_client.py": (
            "from unittest import mock\n\n\n"
            "def test_it():\n    with mock.patch('requests.get'):\n        pass\n"
        ),
    }
    result = migrate(files)
    assert any("patches 'requests.get'" in w for w in result.warnings)
