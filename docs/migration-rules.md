# Migration rules

List the registered rules with `codeshift rules`. Select packs or rule ids with `--rules`, for example `--rules deprecated-api,python.oserror-aliases`.

Deprecations are grounded in the Python documentation linked from each rule's `references`. Nothing is reported as deprecated because a model said so.

## Pack `sync-to-async` (experimental)

### `async.sync-io-candidate`

A synchronous function that calls `time.sleep` or `requests.get/post/put/delete/patch/head/options/request`.

- **Fix:** the repository-wide async engine. `time.sleep` is deterministic. `requests` calls need a provider (`--llm sample` or `--llm groq`).
- **Risk:** high. Always requires review.
- **Compatibility:** the result imports `httpx` (0.27 or newer is assumed) and `asyncio`. `httpx` must be installed in the target project; CodeShift does not edit dependency manifests.

Before:

```python
def fetch_stock(sku):
    response = requests.get(f"{BASE}/stock/{sku}", timeout=2)
    response.raise_for_status()
    return response.json()["quantity"]

def check(sku, quantity):
    return fetch_stock(sku) >= quantity
```

After:

```python
async def fetch_stock(sku):
    async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
        response = await client.get(f"{BASE}/stock/{sku}", timeout=2)
        response.raise_for_status()
        return response.json()["quantity"]

async def check(sku, quantity):
    return await fetch_stock(sku) >= quantity
```

Semantic differences to review:

| Topic | requests | Result |
|---|---|---|
| Redirects | Followed by default | Client created with `follow_redirects=True` |
| Timeout | None by default | Client created with `timeout=None`; per-call `timeout=` is kept |
| Exceptions | `requests.RequestException` and subclasses | Mapped to the nearest `httpx` class (`HTTPError`, `HTTPStatusError`, `TimeoutException`, `ConnectError`); not an exact match |
| `response.ok` | Exists | Not in httpx (`is_success`); flagged as a risk |
| Connections | New connection per call | New `AsyncClient` per call; no pooling is introduced |
| Cancellation | Not applicable | Awaits are cancellation points; code that assumed it could not be interrupted needs review |

When conversion is refused (reported as a proposal):

- The function is a generator, a special method, nested, or a method of a class with base classes.
- It has a decorator other than `staticmethod`, `classmethod`, or a route decorator in a module that imports FastAPI or Starlette.
- It is passed as a value (callback, `map`, thread target).
- It is called from a lambda, a generator expression, or module level.
- It also calls an unsupported blocking API (`subprocess.run`, `urllib.request.urlopen`, `requests.Session`, `sqlite3.connect`, and similar).
- Several classes define a method with the same name.
- The provider is unavailable or its reply failed validation.

Tests: calls from synchronous test functions are rewritten to `asyncio.run(f(...))`. Mocks that patch `requests` are flagged in the plan and must be updated by hand.

### `async.blocking-call-in-async`

A blocking call inside an existing `async def`. The same engine fixes `time.sleep` and `requests` calls in place; no callers change. Unsupported blocking calls are reported only.

## Pack `deprecated-api`

### `python.datetime.utcnow`

`datetime.utcnow()` and `datetime.utcfromtimestamp()` are deprecated since Python 3.12.

```python
datetime.utcnow()                  ->  datetime.now(timezone.utc)
dt.datetime.utcnow()               ->  dt.datetime.now(dt.timezone.utc)
datetime.utcfromtimestamp(ts)      ->  datetime.fromtimestamp(ts, tz=timezone.utc)
```

- **Risk:** medium, requires review. The result is timezone-aware. Comparing it with naive datetimes raises `TypeError`, and `isoformat()` gains `+00:00`.
- `datetime.timezone.utc` is used instead of `datetime.UTC` so the output runs on every Python 3 version.
- `from datetime import timezone` is added when needed and never duplicated. If the name `timezone` is already used for something else, the call is skipped with a warning.

### `python.logging.warn`

`logging.warn()` and `Logger.warn()` are deprecated aliases of `warning()` since Python 3.3.

- **Risk:** low. `warn()` only forwards to `warning()` after a `DeprecationWarning`.
- Loggers are recognised when assigned at module level from `logging.getLogger(...)`, or called directly as `logging.getLogger(...).warn(...)`. A `warn` method on any other object is not touched.

### `python.collections.abc`

`from collections import Mapping` and the other ABC aliases were removed in Python 3.10.

```python
from collections import OrderedDict, Mapping
# becomes
from collections import OrderedDict
from collections.abc import Mapping
```

- **Risk:** low. The classes are identical.
- Attribute use (`collections.Mapping`) is reported but not rewritten.

## Pack `python-modernization`

| Rule | Behaviour |
|---|---|
| `python.oserror-aliases` | `IOError` and `EnvironmentError` become `OSError`. They are the same class object since 3.3, so behaviour is identical. Names shadowed in the module are skipped. A tuple that listed both ends up as `(OSError, OSError)`, which is valid but redundant. |
| `python.except.bare` | Reports bare `except:`. Never rewritten, because choosing the exception type changes behaviour. |
| `python.syntax.target-version` | Reports files that do not parse under the target version's grammar. Best effort, via `ast.parse(feature_version=...)`. |

## Project rules

`codeshift.toml` can define `rename-call` rules:

```toml
[[rules.custom]]
id = "acme.fetch-all"
kind = "rename-call"
source = "acme_sdk.fetch_all"
target = "acme_sdk.fetch_page"
risk = "medium"
```

Calls are matched through imports, so `other.fetch_all()` is not changed. Source and target must be in the same module; anything more structural needs a rule class implementing `Rule`.

## Writing a rule class

```python
class MyRule(Rule):
    spec = RuleSpec(id="acme.my-rule", name="...", description="...", pack="custom",
                    references=["https://..."], semantic_notes="...")

    def detect(self, module, ctx):          # ast-based, returns Finding objects
        ...

    def transform(self, module):            # LibCST-based, returns RuleTransform
        ...
```

Register it with `RuleRegistry.register`. Each built-in pack ships with positive and negative tests in `tests/unit/test_rules.py` and `tests/unit/test_async_migration.py`, sample input under `tests/fixtures/before/` and `examples/legacy_service/`, and expected output under `tests/fixtures/expected/`.
