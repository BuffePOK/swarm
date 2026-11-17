# syntax=docker/dockerfile:1
# Dependencies come from uv.lock (every wheel hash-checked). The dev extra is included on
# purpose: the self-improvement loop runs the test suite inside the container.
FROM python:3.12-slim AS build
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /src
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY roj ./roj
RUN uv sync --locked --extra dev --extra browser --no-editable

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH=/opt/venv/bin:$PATH ROJ_HOME=/data \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers ROJ_BROWSER_NO_SANDBOX=1
COPY --from=build /opt/venv /opt/venv
# Headless Chromium only (no full browser UI) plus the system libraries it needs. The container is its sandbox.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && /opt/venv/bin/playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --home-dir /data --shell /usr/sbin/nologin roj \
 && mkdir /data && chown roj /data
COPY examples /opt/roj/examples
USER roj
WORKDIR /data
VOLUME /data
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s CMD ["roj", "healthcheck"]
ENTRYPOINT ["roj"]
CMD ["serve", "--host", "127.0.0.1", "--port", "18732"]
