# Nifty 50 EOD Dashboard

Price chart with indicators, NSE option chain (OI, PCR, max pain), India VIX,
a rule-based CE/PE/no-trade signal, and a track record that grades every
signal against the next day's actual market move.

## Setup (one time)
1. Create a GitHub repo and upload all these files (including the `.github` folder).
2. Settings → Pages → Deploy from branch → `main` / root.
3. Actions tab → "Update Nifty data" → Run workflow (creates data.json the first time).
4. Open https://<your-username>.github.io/<repo>/

After that it refreshes itself every weekday at 4:00 PM IST.

## Files the script keeps
- `data.json`  – everything the page shows (rewritten each run)
- `track.json` – the permanent signal log. Don't delete it, or the track record restarts.
- `oc_cache.json` – last good option-chain snapshot, shown (flagged as old) if NSE fails.

## If the option chain shows "couldn't be fetched"
NSE often blocks requests from cloud servers, including GitHub's. Prices, VIX
and signals still update; only the option-chain panel goes missing or stale.
Fix: run the update from your own computer instead.
- Windows: install Python + Git, `pip install -r requirements.txt`, then schedule
  `update_local.bat` in Task Scheduler for weekdays 4:00 PM.
- Mac/Linux: schedule `update_local.sh` with cron (`0 16 * * 1-5`).
You can then disable the GitHub workflow (Actions → workflow → Disable).

## Testing
- Live: `python analyze.py && python -m http.server` → http://localhost:8000
- Offline demo: `python analyze.py --demo`

Educational only — not investment advice.
