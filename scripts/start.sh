#!/usr/bin/env sh
# Container entrypoint for the Railway runtime.
# Lives in a real shell script (not the railway.toml startCommand) because
# Railway does NOT perform POSIX ${VAR:-default} expansion on startCommand —
# it would pass the literal string to uvicorn. Inside sh, expansion works.
set -e

echo "[start] running database migrations (alembic upgrade head)..."
uv run --no-sync alembic -c alembic.ini upgrade head
echo "[start] migrations complete; booting uvicorn on port ${PORT:-8080}"

exec uv run --no-sync uvicorn --factory parts_lookup.api.main:create_app \
  --host 0.0.0.0 --port "${PORT:-8080}"
