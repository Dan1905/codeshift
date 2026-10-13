# Safety model

## Threats considered

| Threat | Control |
|---|---|
| The tool damages the user's code | Read-only by default, explicit approval, hash checks, backups, rollback |
| The tool overwrites work in progress | Apply and rollback compare file hashes and refuse on any mismatch |
| A model returns wrong or malicious code | Schema validation, AST validation, signature pinning, tests, mandatory review |
| Repository text manipulates the model | Untrusted-content framing, and output validation that does not depend on the model behaving |
| Secrets leak to a model, logs or history | Minimal context, redaction, credentials only from the environment |
| The repository's tests are hostile | Tests are opt-in, run in a temporary copy with a timeout; optional Docker isolation |
| A request reaches files outside the repository | Every path is resolved and checked against the repository or workspace root |

## Read-only by default

`scan`, `analyze`, `plan`, `diff`, `verify`, `history` and `report` never write to the target repository. Analysis uses `ast.parse` and LibCST only; repository code is never imported or executed. `tests/unit/test_discovery_config.py` and `tests/integration/test_pipeline.py` assert that scanning and planning leave every file untouched.

## Applying changes

1. Approval is required. "Review required" changes (every async or AI-assisted change, and medium-risk rules) cannot be applied without it, even with `auto_apply_low_risk = true`.
2. Every target file must hash to the value recorded at planning time. One mismatch refuses the whole apply.
3. Targets must be writable. A read-only mount fails here, before anything is written.
4. Originals are copied to the state directory, then each file is written through a temporary file and `os.replace`.
5. If a write fails part-way, files already written are restored.

## Rollback

Rollback restores a file only if its current hash equals the hash the run wrote. If anything was edited afterwards, rollback refuses and changes nothing. CodeShift never runs `git reset`, `git checkout`, `git clean` or `git stash`; its only Git commands are `status` and `rev-parse`.

## LLM handling

**What is sent.** One function at a time: its source, the module's import lines, up to five call-site lines, and structured metadata. Never the whole repository. `max_context_chars` caps the prompt; call sites are dropped first, and the request is refused if it is still too large.

**Redaction.** Before source goes to a hosted provider, string values assigned to secret-like names (`password`, `token`, `api_key`, ...) and known key shapes (Groq, OpenAI, AWS, GitHub, Slack, bearer tokens, private keys) are replaced with placeholders. Extra patterns can be configured. The mapping stays in memory and is used to put the real values back into accepted code locally. Redaction is pattern-based and cannot catch every secret; do not rely on it for code that embeds unusual credentials.

**Prompt injection.** Repository text is placed between markers carrying a random nonce and described to the model as data. This lowers the chance of the model following embedded instructions, but the real control is that the reply is treated as untrusted too:

- It must be JSON matching the response schema, with no unknown fields and a supported operation.
- It may only target the requested file and function.
- The code must parse as exactly one `async def` with the same name and parameters.
- No placeholders (`...`, a body of only `pass`), no imports or `global` statements in the body.
- No remaining `requests` usage or supported blocking calls.
- No newly introduced calls to `eval`, `exec`, `open`, `os.*`, `subprocess.*`, `socket.*` and similar.
- Only `httpx` and `asyncio` may be newly imported.
- Only the function body is spliced in; the signature and decorators come from the original.

A rejected reply gets one corrective retry with the validation error. If that fails, the function's whole conversion group is left unchanged.

**Limits.** `max_requests_per_run`, `max_total_tokens`, `timeout_seconds` and `max_retries` (with backoff, honouring `Retry-After`) bound cost and time. Provider, model, status and timing are logged; prompts, replies and keys are not.

**Valid is not correct.** Passing validation means a reply is well formed. Whether it is right is decided by the structural invariants, the project's tests against a baseline, and a person. No model is used as a correctness gate.

## Running tests

Tests execute repository code. Controls:

- Off by default (`verification.run_tests = false`).
- They run in a temporary copy of the repository, never in the working tree.
- A timeout applies (`test_timeout_seconds`).
- Environment variables whose names contain `API_KEY`, `TOKEN`, `SECRET` or `PASSWORD` are removed from the test process.
- Commands are executed without a shell.

With `isolation = "docker"`, tests run in a container with no network, dropped capabilities, `no-new-privileges`, and memory, CPU and PID limits.

**Limits of that isolation.** Without Docker isolation, tests run with your user's privileges and can read anything you can. Docker narrows this but is not a complete security boundary: container escapes exist, and the mounted copy is writable by the container. Do not run tests from a repository you do not trust on a machine that matters. Docker isolation is implemented but not covered by the automated tests.

## REST API

- Repositories are resolved under `CODESHIFT_WORKSPACE_ROOT`; `..` traversal is rejected with HTTP 400.
- Runs belonging to repositories outside the root return HTTP 403.
- `CODESHIFT_API_TOKEN` enables bearer authentication (constant-time comparison). Without it the API is unauthenticated, so bind it to localhost or a private network.
- `POST /runs/{id}/apply` needs `{"approve": true}` for review-required changes.
- Request bodies are validated and reject unknown fields. `/transform` caps source size.

## What is stored

The history database holds run metadata, findings (with short source excerpts), hashes and diffs. The artifact directory holds proposed content and backups for changed files only. No credentials are stored. `codeshift purge --run-id` deletes a run's artifacts.
