# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CODESHIFT_HOME=/data \
    CODESHIFT_WORKSPACE_ROOT=/workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/codeshift
# Pinned dependencies first so the layer is cached across source changes.
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-deps .

# Run history and artifacts live in /data, never inside the mounted repository.
RUN useradd --create-home --uid 10001 codeshift \
    && mkdir -p /data /workspace \
    && chown codeshift:codeshift /data /workspace
USER codeshift
WORKDIR /workspace
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

ENTRYPOINT ["codeshift"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
