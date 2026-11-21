# Nifty 50 EOD Dashboard

Price chart with indicators, NSE option chain (OI, PCR, max pain), India VIX,
a rule-based CE/PE/no-trade signal, and a track record that grades every
signal against the next day's actual market move.

## Setup (one time)
1. Create a GitHub repo and upload all these files (including the `.github` folder).
2. Settings → Pages → Deploy from branch → `main` / root.
3. Actions tab → "Update Nifty data" → Run workflow (creates data.json the first time).
4. Open https://<your-username>.github.io/<repo>/

After that it refreshes itself every weekday:
- 4:00 PM IST: full analysis after the market closes
- 8:15 AM IST: pre-market run with news, FII/DII, global markets and the opening-gap forecast
  (GitHub often starts scheduled runs 5-20 minutes late, so it's usually ready by about 8:30)

To run either one by hand: Actions -> Update Nifty data -> Run workflow -> choose "full" or "premarket".

## Opening-gap forecast and GIFT Nifty
Every morning the forecast predicts a gap up, gap down or flat open, with points and
an expected opening level. It's graded against the actual open in the evening run
(saved in `gap_track.json`).

It always uses a **global cues model**: how far S&P 500 futures have moved since
Nifty's last close, scaled by how Nifty has historically gapped on such moves
(refitted on 2 years of data each evening).

**GIFT Nifty is fetched automatically** from NSE IX's own public website (nseix.com),
the exchange where it trades. No login or keys needed. The evening run records each
contract's price around Nifty's close, and the morning run compares the same contract's
price now with that, which gives the implied gap. If NSE IX blocks GitHub's servers
(exchanges sometimes block cloud servers), the forecast falls back to the global
cues model; running `premarket_local.bat` / `.sh` from your PC usually fixes that.

**Optional backup: Zerodha Kite** (paid Kite Connect plan, ₹500/month; needs a manual
login every morning after 7:35 AM, as the exchange requires).
1. developers.kite.trade -> your app -> Redirect URL = `https://<your-username>.github.io/<repo>/kite.html`
2. GitHub -> Settings -> Secrets and variables -> Actions: add `KITE_API_KEY` and `KITE_API_SECRET`
   (the secret lives only there, never in a file).
3. Paste your API key (not the secret) into the `KITE_API_KEY` line of `kite.html`.
Then on any morning: cockpit -> "Use Kite instead" -> log in -> copy the code -> GitHub ->
Run workflow -> premarket -> paste it. From a PC: `python kite_morning.py`.

## Login
The site opens on `login.html`. Demo credentials: **admin / admin123**.
To change them, edit `USERNAME` and `PASS_HASH` near the bottom of `login.html`
(the comment at the top of the file shows how to make a new hash).
This is a browser-only login: it keeps casual visitors out but is not real
security. Anyone determined can read the page source or open `data.json`
directly. For real protection, use a private host (for example Cloudflare
Pages with Cloudflare Access, or Netlify with password protection).

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
For the morning forecast, schedule `premarket_local.bat` / `premarket_local.sh` at 8:15 AM.
You can then disable the GitHub workflow (Actions → workflow → Disable).

## Testing
- Live: `python analyze.py && python -m http.server` → http://localhost:8000
- Offline demo: `python analyze.py --demo`

Educational only — not investment advice.
