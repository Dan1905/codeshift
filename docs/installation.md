# Installation

## Requirements

- Python 3.12 or newer (developed and tested on 3.13)
- Git, for working-tree checks
- Docker, only for the container image or `verification.isolation = "docker"`

## With pip

```bash
python -m venv .venv && source .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

Extras:

| Extra | Adds |
|---|---|
| `dev` | pytest, pytest-cov, mypy, requests (for the demo service) |
| `demo` | pytest and requests only: enough to verify the bundled demo |

## With uv

```bash
uv venv --python 3.13 && uv pip install -e ".[dev]"
```

`requirements.lock` pins every runtime dependency. It was generated with:

```bash
uv pip compile pyproject.toml --extra demo --universal --python-version 3.12 -o requirements.lock
```

## Groq API key

Create a key at <https://console.groq.com/keys>, then:

```bash
export GROQ_API_KEY=your_key_here
```

Enable the provider per run with `--llm groq`, or in `codeshift.toml`:

```toml
[llm]
enabled = true
provider = "groq"
```

The key is read from the environment each time it is needed. It is never written to configuration, logs, reports or the history database.

Any OpenAI-compatible endpoint works too:

```toml
[llm]
enabled = true
provider = "openai-compatible"
base_url = "https://example.com/v1"
model = "your-model"
```

with the key in `CODESHIFT_LLM_API_KEY`.

## Docker

```bash
docker build -t codeshift-ai .
```

See the Docker section of the README for run commands.

## Verifying the installation

```bash
codeshift --version
```

```bash
codeshift scan examples/legacy_service
```

```bash
./scripts/run_checks.sh
```
