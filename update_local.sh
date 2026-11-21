#!/usr/bin/env bash
# Run the update from your own PC (use if NSE blocks GitHub's servers).
# Schedule with cron:  0 16 * * 1-5  /path/to/update_local.sh
cd "$(dirname "$0")" && git pull -q && python3 analyze.py && \
git add data.json track.json && { git add oc_cache.json 2>/dev/null; true; } && git commit -qm "Local data update $(date +%F)" && git push -q
