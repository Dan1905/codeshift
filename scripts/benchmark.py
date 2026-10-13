"""Reproducible timing benchmark for scan, analysis and planning.

Generates a synthetic repository of N modules (each with a blocking HTTP
function, two callers, and a few deprecated API uses), then times the pipeline
stages. Usage:

    python scripts/benchmark.py --files 50 200 --repeat 3
"""

from __future__ import annotations

import argparse
import os
import platform
import statistics
import tempfile
import time
from pathlib import Path

MODULE = '''"""Synthetic module {i}."""

import logging
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)


def fetch_{i}(item):
    response = requests.get(f"http://upstream.local/items/{{item}}", timeout=2)
    response.raise_for_status()
    return response.json()


def retry_{i}(item):
    try:
        return fetch_{i}(item)
    except IOError:
        logger.warn("retrying %s", item)
        time.sleep(0.1)
        return fetch_{i}(item)


def summarise_{i}(items):
    rows = [retry_{i}(item) for item in items]
    return {{"count": len(rows), "at": datetime.utcnow().isoformat()}}
'''


def build_repo(root: Path, files: int) -> None:
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    for index in range(files):
        (package / f"module_{index}.py").write_text(MODULE.format(i=index))


def measure(files: int, repeat: int) -> dict[str, float]:
    from codeshift.config import Config
    from codeshift.orchestration.migration_runner import MigrationRunner
    from codeshift.storage.database import RunStore

    samples: dict[str, list[float]] = {"scan": [], "analyze": [], "plan": []}
    changed = functions = 0
    for _ in range(repeat):
        with tempfile.TemporaryDirectory() as scratch:
            repo = Path(scratch) / "repo"
            build_repo(repo, files)
            os.environ["CODESHIFT_HOME"] = str(Path(scratch) / "home")
            config = Config.model_validate(
                {
                    "migrations": {"max_files_per_run": 100_000},
                    # One provider request per HTTP function; lift the default cap of 25.
                    "llm": {"max_requests_per_run": 100_000},
                }
            )
            runner = MigrationRunner(repo, config, RunStore())

            started = time.perf_counter()
            runner.scan()
            samples["scan"].append(time.perf_counter() - started)

            started = time.perf_counter()
            runner.analyze(use_cache=False)
            samples["analyze"].append(time.perf_counter() - started)

            started = time.perf_counter()
            record = runner.plan(llm_mode="sample")
            samples["plan"].append(time.perf_counter() - started)
            changed = len(record.plan.changes)
            functions = len(record.plan.converted_functions)
    result = {name: statistics.median(values) for name, values in samples.items()}
    result["files_changed"] = changed
    result["functions_converted"] = functions
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, nargs="+", default=[50, 200])
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    print(f"Python {platform.python_version()} on {platform.platform()}")
    print(f"median of {args.repeat} run(s); plan uses the offline sample provider\n")
    print(
        "| Files | Scan (s) | Scan + analyze (s) | Full plan (s) | Files changed | Functions converted |"
    )
    print("|---:|---:|---:|---:|---:|---:|")
    for files in args.files:
        row = measure(files, args.repeat)
        print(
            f"| {files + 1} | {row['scan']:.3f} | {row['analyze']:.3f} | {row['plan']:.3f} "
            f"| {int(row['files_changed'])} | {int(row['functions_converted'])} |"
        )


if __name__ == "__main__":
    main()
