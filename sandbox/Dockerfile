# Decision sandbox container (spec 14.6): no ROS, no Open-RMF, no database.
# Build from the repository root:  docker build -f sandbox/Dockerfile -t auspex-sandbox .
# Run:  docker run --rm -p 8000:8000 -e SANDBOX_ENABLED=true -e NEBIUS_API_BASE=... \
#         -e NEBIUS_API_KEY=<dedicated sandbox key> -e MODEL_TRIAGE=... \
#         -e SANDBOX_ALLOWED_ORIGIN=https://<you>.github.io auspex-sandbox
FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY site ./site
COPY policy ./policy
COPY playbooks ./playbooks
COPY sandbox ./sandbox
RUN uv sync --frozen --no-dev && test -f sandbox/snapshots/s1.json && ls site/nav_graphs/*.json
RUN useradd --create-home --uid 10001 sandbox
USER sandbox
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "sandbox.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
