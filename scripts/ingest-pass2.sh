#!/usr/bin/env bash
# Ingest pass 2: the spare-parts catalogs (~11 files, the largest PDFs in
# the corpus — up to ~264 pages each). Run one at a time so a docling
# crash or OOM on one catalog isolates and the others still complete.
#
# Targets the same Railway Postgres + Cloudflare R2 the deployed API reads,
# via the local .env. No local containers required.
#
# Prereqs: `uv sync --extra ingestion` and a Voyage account with a payment
# method (the free tier's 10K-TPM cap stalls bulk ingest).
#
# Idempotent: SHA-256 dedup short-circuits already-ingested PDFs.
# Resumes naturally — re-run after any interruption.
#
# Usage:
#   ./scripts/ingest-pass2.sh
#   nohup ./scripts/ingest-pass2.sh > /dev/null 2>&1 &   # background

set -uo pipefail   # NB: not -e; we want the loop to keep going on per-PDF failures.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANUALS_DIR="${MANUALS_DIR:-$HOME/code/manuals/manuals}"
STAGE_DIR="${STAGE_DIR:-/tmp/parts-lookup-pass2}"
LOG_FILE="${LOG_FILE:-/tmp/parts-lookup-pass2.log}"

cd "$REPO_ROOT"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "=================================================================="
echo "parts-lookup ingest pass 2 (catalogs) — started $(date '+%Y-%m-%d %H:%M:%S')"
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
mkdir -p "$STAGE_DIR"
find "$STAGE_DIR" -maxdepth 1 -name '*.pdf' -delete
find "$MANUALS_DIR" -name '*.pdf' -path '*spare-parts-catalog*' \
  -exec ln -sf {} "$STAGE_DIR/" \;
COUNT=$(find "$STAGE_DIR" -maxdepth 1 -name '*.pdf' | wc -l | tr -d ' ')
echo "[stage] $COUNT catalog PDFs symlinked into $STAGE_DIR"

if (( COUNT == 0 )); then
  echo "ERROR: no catalog PDFs found under $MANUALS_DIR/*spare-parts-catalog*" >&2
  exit 1
fi

# ---- Pre-run row counts ----
echo "[pre-run] DB state: $(db_counts)"

# ---- Loop: one ingest call per catalog ----
# Sorted for deterministic order. A failure on one catalog prints FAIL and
# the loop continues; the next run will skip already-ingested ones.
ATTEMPTED=0
SUCCESS=0
FAILED=0
mapfile -t CATALOGS < <(find "$STAGE_DIR" -maxdepth 1 -name '*.pdf' | sort)

for f in "${CATALOGS[@]}"; do
  ATTEMPTED=$(( ATTEMPTED + 1 ))
  PDF_START=$(date +%s)
  echo "------------------------------------------------------------------"
  echo "[$(date '+%H:%M:%S')] ($ATTEMPTED/$COUNT) ingesting $(basename "$f")"
  if uv run --extra ingestion parts-lookup ingest "$f"; then
    SUCCESS=$(( SUCCESS + 1 ))
    STATUS=ok
  else
    FAILED=$(( FAILED + 1 ))
    STATUS=fail
  fi
  PDF_END=$(date +%s)
  printf "[%s] (%d/%d) %s  rc-class=%s  elapsed=%ds\n" \
    "$(date '+%H:%M:%S')" "$ATTEMPTED" "$COUNT" \
    "$(basename "$f")" "$STATUS" "$(( PDF_END - PDF_START ))"
done

# ---- Post-run summary ----
END_EPOCH=$(date +%s)
ELAPSED=$(( END_EPOCH - START_EPOCH ))

echo "------------------------------------------------------------------"
echo "[post-run] DB state: $(db_counts)"

echo "=================================================================="
printf "pass 2 done in %dh%02dm%02ds  attempted=%d  ok=%d  failed=%d\n" \
  $((ELAPSED/3600)) $(((ELAPSED%3600)/60)) $((ELAPSED%60)) "$ATTEMPTED" "$SUCCESS" "$FAILED"
echo "log: $LOG_FILE"
echo "=================================================================="

if (( FAILED > 0 )); then
  echo "Some catalogs failed — re-run to retry (already-OK PDFs are skipped)."
  exit 1
fi
exit 0
