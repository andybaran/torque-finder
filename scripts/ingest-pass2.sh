#!/usr/bin/env bash
# Ingest pass 2: the spare-parts catalogs (~11 files, the largest PDFs in
# the corpus — up to 264 pages each). Run one at a time so a docling
# crash or OOM on one catalog isolates and the others still complete.
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
PG_CONTAINER="${PG_CONTAINER:-parts-lookup-pg}"
MINIO_CONTAINER="${MINIO_CONTAINER:-parts-lookup-minio}"

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
command -v podman >/dev/null || { echo "ERROR: podman not on PATH" >&2; exit 1; }

for c in "$PG_CONTAINER" "$MINIO_CONTAINER"; do
  if ! podman inspect -f '{{.State.Running}}' "$c" 2>/dev/null | grep -q true; then
    echo "ERROR: container '$c' is not running. Start it with: podman start $c" >&2
    exit 1
  fi
done
echo "[pre-flight] containers up, repo root = $REPO_ROOT"

# ---- Build flat symlink staging dir ----
mkdir -p "$STAGE_DIR"
find "$STAGE_DIR" -maxdepth 1 -name '*.pdf' -delete
find "$MANUALS_DIR" -name '*.pdf' -path '*/spare-parts-catalog*' \
  -exec ln -sf {} "$STAGE_DIR/" \;
COUNT=$(find "$STAGE_DIR" -maxdepth 1 -name '*.pdf' | wc -l | tr -d ' ')
echo "[stage] $COUNT catalog PDFs symlinked into $STAGE_DIR"

if (( COUNT == 0 )); then
  echo "ERROR: no catalog PDFs found under $MANUALS_DIR/*spare-parts-catalog*" >&2
  exit 1
fi

# ---- Pre-run row counts ----
echo "[pre-run] DB state:"
podman exec "$PG_CONTAINER" psql -U parts -d parts_lookup -tAc \
  "SELECT 'pdfs=' || count(*) FROM pdfs UNION ALL SELECT 'pages=' || count(*) FROM pages;"

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
  if uv run parts-lookup ingest "$f"; then
    SUCCESS=$(( SUCCESS + 1 ))
    STATUS=ok
  else
    FAILED=$(( FAILED + 1 ))
    STATUS=fail
  fi
  PDF_END=$(date +%s)
  printf "[%s] (%d/%d) %s  rc-class=%s  elapsed=%ds  %s\n" \
    "$(date '+%H:%M:%S')" "$ATTEMPTED" "$COUNT" \
    "$(basename "$f")" "$STATUS" "$(( PDF_END - PDF_START ))" \
    "$(basename "$f")"
done

# ---- Post-run summary ----
END_EPOCH=$(date +%s)
ELAPSED=$(( END_EPOCH - START_EPOCH ))

echo "------------------------------------------------------------------"
echo "[post-run] DB state:"
podman exec "$PG_CONTAINER" psql -U parts -d parts_lookup -tAc \
  "SELECT 'pdfs=' || count(*) FROM pdfs UNION ALL SELECT 'pages=' || count(*) FROM pages;"

echo "=================================================================="
printf "pass 2 done in %dh%02dm%02ds  attempted=%d  ok=%d  failed=%d\n" \
  $((ELAPSED/3600)) $(((ELAPSED%3600)/60)) $((ELAPSED%60)) "$ATTEMPTED" "$SUCCESS" "$FAILED"
echo "log: $LOG_FILE"
echo "=================================================================="

if (( FAILED > 0 )); then
  echo "Failures (re-run the script to retry — already-OK PDFs will be skipped):"
  grep '^FAIL ' "$LOG_FILE" || true
  exit 1
fi
exit 0
