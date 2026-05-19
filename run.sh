#!/usr/bin/env bash
# Driver for the SDP Investor Network ingest pipeline.
# Idempotent end-to-end: re-running produces the same DB state and exports.
#
# Usage:
#   ./run.sh            # full pipeline
#   ./run.sh inventory  # just Phase 0 (inventory)
#   ./run.sh schema     # apply migrations
#   ./run.sh ingest     # Phase 2
#   ./run.sh dedup      # Phase 3
#   ./run.sh enrich     # Phase 4 (scraping + heuristic extract)
#   ./run.sh network    # Phase 5
#   ./run.sh export     # Phase 6
#   ./run.sh qa         # Phase 7

set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv/bin/python
[ -x "$VENV" ] || { echo "venv not found — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }

phase="${1:-all}"
case "$phase" in
  inventory)     $VENV scripts/00_inventory.py ;;
  schema)        $VENV scripts/01_schema.py ;;
  ingest)        $VENV scripts/02_ingest.py ;;
  dedup)         $VENV scripts/03_dedup.py ;;
  enrich)        $VENV scripts/04_enrich.py ;;
  apollo)        $VENV scripts/08_apollo_ingest.py ;;
  mandate)       $VENV scripts/09_mandate_extract.py ;;
  attio)         $VENV scripts/10_attio_ingest.py ;;
  llm_extract)   $VENV scripts/11_llm_mandate_extract.py ;;
  network)       $VENV scripts/05_network.py ;;
  export)        $VENV scripts/06_export.py ;;
  qa)            $VENV scripts/07_qa.py ;;
  all)
    $VENV scripts/00_inventory.py
    $VENV scripts/01_schema.py
    $VENV scripts/02_ingest.py
    $VENV scripts/03_dedup.py
    $VENV scripts/04_enrich.py
    $VENV scripts/08_apollo_ingest.py
    $VENV scripts/09_mandate_extract.py
    $VENV scripts/10_attio_ingest.py
    $VENV scripts/11_llm_mandate_extract.py     # only runs if ANTHROPIC_API_KEY set
    $VENV scripts/05_network.py
    $VENV scripts/06_export.py
    $VENV scripts/07_qa.py
    ;;
  best)
    shift
    $VENV scripts/best_contacts.py "$@"
    ;;
  *) echo "unknown phase: $phase"; echo "phases: inventory schema ingest dedup enrich apollo mandate attio llm_extract network export qa all best"; exit 1 ;;
esac
