# Runtime image for the parts-lookup API (Railway).
# Official Astral uv image pins Python 3.14, which Nixpacks can't provide.
# Intentionally CPU-light: the `ingestion` extra (docling/PyTorch) is NOT
# installed here — ingestion runs offline on a workstation, not on Railway.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

# uv: copy deps into the image (no hardlinks across layers) and precompile.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 1) Install dependencies first (cached layer) without the project itself.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

# 2) Copy source and install the project into the venv.
COPY . .
RUN uv sync --no-dev --frozen

# Run DB migrations, then boot the API. Railway injects $PORT.
# (railway.toml's startCommand overrides this for Railway deploys; this CMD
# keeps the image runnable on its own too.)
CMD ["sh", "scripts/start.sh"]
