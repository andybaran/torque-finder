#!/usr/bin/env bash
# Ingest pass 1: every PDF under ~/code/manuals/manuals EXCEPT the
# spare-parts catalogs (~249 files). Catalogs are deferred to pass 2 so
# one giant docling parse failure can't take the rest of the run with it.
#
# Targets the same Railway Postgres + Cloudflare R2 the deployed API reads,
# via the local .env (DATABASE_URL = Railway public proxy, R2_* = real R2).
# No local containers required.
#
# Prereqs: `uv sync --extra ingestion` (docling/PyTorch) and a Voyage account
# with a payment method (the free tier's 10K-TPM cap stalls bulk ingest).
#
# Idempotent: SHA-256 dedup short-circuits already-ingested PDFs.
# Resumes naturally — re-run after any interruption.
#
# Usage:
#   ./scripts/ingest-pass1.sh
#   nohup ./scripts/ingest-pass1.sh > /dev/null 2>&1 &   # background

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANUALS_DIR="${MANUALS_DIR:-$HOME/code/manuals/manuals}"
STAGE_DIR="${STAGE_DIR:-/tmp/parts-lookup-pass1}"
LOG_FILE="${LOG_FILE:-/tmp/parts-lookup-pass1.log}"

cd "$REPO_ROOT"

# tee stdout+stderr into the log so both interactive and nohup runs leave a trace.
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=================================================================="
echo "parts-lookup ingest pass 1 — started $(date '+%Y-%m-%d %H:%M:%S')"
echo "=================================================================="
START_EPOCH=$(date +%s)

# ---- Pre-flight ----
[[ -d "$MANUALS_DIR" ]] || { echo "ERROR: manuals dir not found: $MANUALS_DIR" >&2; exit 1; }
[[ -f "$REPO_ROOT/.env" ]] || { echo "ERROR: .env missing at $REPO_ROOT/.env" >&2; exit 1; }
command -v uv >/dev/null || { echo "ERROR: uv not on PATH" >&2; exit 1; }
echo "[pre-flight] repo root = $REPO_ROOT, target DB/R2 from .env"

# ---- DB row-count helper (reads DATABASE_URL from the project config) ----
db_counts() {
  uv run --extra ingestion python - <<'PY'
import asyncio, asyncpg, urllib.parse as up
from parts_lookup.config import get_settings
s = get_settings()
url = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
parts = up.urlsplit(url)
q = parts.query or ""
ssl = "require" if ("ssl=require" in q or "sslmode=require" in q) else None
clean = up.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
async def main():
    c = await asyncpg.connect(clean, ssl=ssl)
    pdfs = await c.fetchval("select count(*) from pdfs")
    pages = await c.fetchval("select count(*) from pages")
    print(f"pdfs={pdfs} pages={pages}")
    await c.close()
asyncio.run(main())
PY
}

# ---- Build flat symlink staging dir ----
# ingest-dir is non-recursive; flatten manuals/ into one dir of symlinks.
mkdir -p "$STAGE_DIR"
find "$STAGE_DIR" -maxdepth 1 -name '*.pdf' -delete
find "$MANUALS_DIR" -name '*.pdf' -not -path '*spare-parts-catalog*' \
  -exec ln -sf {} "$STAGE_DIR/" \;
COUNT=$(find "$STAGE_DIR" -maxdepth 1 -name '*.pdf' | wc -l | tr -d ' ')
echo "[stage] $COUNT non-catalog PDFs symlinked into $STAGE_DIR"

# ---- Pre-run row counts ----
echo "[pre-run] DB state: $(db_counts)"

# ---- Run the ingest ----
echo "[ingest] uv run parts-lookup ingest-dir $STAGE_DIR"
echo "------------------------------------------------------------------"
uv run --extra ingestion parts-lookup ingest-dir "$STAGE_DIR" || INGEST_RC=$?
INGEST_RC=${INGEST_RC:-0}
echo "------------------------------------------------------------------"

# ---- Post-run summary ----
END_EPOCH=$(date +%s)
ELAPSED=$(( END_EPOCH - START_EPOCH ))
OK_COUNT=$(grep -c '^OK '   "$LOG_FILE" || true)
FAIL_COUNT=$(grep -c '^FAIL ' "$LOG_FILE" || true)

echo "[post-run] DB state: $(db_counts)"

echo "=================================================================="
printf "pass 1 done in %dh%02dm%02ds  rc=%d  OK=%s  FAIL=%s\n" \
  $((ELAPSED/3600)) $(((ELAPSED%3600)/60)) $((ELAPSED%60)) "$INGEST_RC" "$OK_COUNT" "$FAIL_COUNT"
echo "log: $LOG_FILE"
echo "=================================================================="

if (( FAIL_COUNT > 0 )); then
  echo "Failures:"
  grep '^FAIL ' "$LOG_FILE" || true
fi

exit "$INGEST_RC"
