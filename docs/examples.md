# Examples

All commands assume CodeShift is installed and that you work on a copy of the demo:

```bash
cp -R examples/legacy_service /tmp/legacy_service && cd /tmp
```

## Deprecated APIs only, no LLM

```bash
codeshift plan ./legacy_service --rules deprecated-api,python-modernization
```

```bash
codeshift diff
```

```bash
codeshift apply --yes
```

```bash
codeshift verify --run-tests
```

## Sync-to-async with Groq

```bash
export GROQ_API_KEY=your_key_here
```

```bash
codeshift plan ./legacy_service --rules sync-to-async --llm groq
```

If Groq is unreachable or its reply is rejected, the affected group is left as a proposal. To fall back to the offline provider automatically, set `fallback = "sample"` under `[llm]`.

## Check a proposal before touching the working tree

`verify` on a run that has not been applied tests a temporary copy:

```bash
codeshift plan ./legacy_service --llm sample
```

```bash
codeshift verify --run-tests
```

The summary ends with `Checked: proposed copy`. The semantic review stage reports `REVIEW REQUIRED` until the changes are applied with approval.

## CI gate

Fail a pipeline when fixable deprecated APIs remain (exit code 5):

```bash
codeshift analyze . --rules deprecated-api --fail-on-findings
```

Machine-readable findings:

```bash
codeshift analyze . --json > findings.json
```

## REST API session

```bash
CODESHIFT_WORKSPACE_ROOT=/tmp codeshift serve --port 8000
```

```bash
curl -s -X POST localhost:8000/plan -H 'content-type: application/json' -d '{"repo_path": "legacy_service", "llm": "sample"}'
```

Take `run_id` from the response, then:

```bash
curl -s localhost:8000/runs/RUN_ID/diff
```

```bash
curl -s -X POST localhost:8000/runs/RUN_ID/apply -H 'content-type: application/json' -d '{"approve": true}'
```

```bash
curl -s -X POST localhost:8000/runs/RUN_ID/verify -H 'content-type: application/json' -d '{"run_tests": true}'
```

```bash
curl -s -X POST localhost:8000/runs/RUN_ID/rollback
```

Transform a snippet without a repository:

```bash
curl -s -X POST localhost:8000/transform -H 'content-type: application/json' -d '{"source": "import logging\nlogging.warn(\"x\")\n", "rules": ["deprecated-api"]}'
```

## What a refusal looks like

`tools/export_report.py` in the demo has two functions the engine will not convert:

```text
Async group grp_2: proposal only
  functions: archive
  blocked: archive: also calls subprocess.run() at line 40, which has no supported async equivalent

Async group grp_3: proposal only
  functions: throttle
  blocked: throttle: passed around as a value at tools/export_report.py:34
```

`throttle` is handed to `ThreadPoolExecutor.map`. Making it a coroutine would make the pool return un-awaited coroutine objects, so the group is reported and left unchanged.

## Structured logs

```bash
codeshift --log-level INFO --json-logs plan ./legacy_service --llm sample
```

Logs go to stderr as one JSON object per line, so stdout stays clean for `--json` output.
