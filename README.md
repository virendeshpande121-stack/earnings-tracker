# Alphapeak Earnings Tracker

A local dashboard that tracks the status of quarterly results for the Nifty 500 and your watchlist. It pulls live data from NSE and BSE.

## Hosted copy (shareable link)
**https://virendeshpande121-stack.github.io/earnings-tracker/**

- **How it updates:** GitHub Actions (`.github/workflows/publish.yml`) runs one cycle and republishes the page. That's every 5 minutes from 06:30 to 01:30 IST and every 30 minutes overnight. GitHub can delay scheduled runs.
- **NSE only:** BSE blocks GitHub's servers. The hosted page therefore has no BSE links or codes, shows free-float Mcap, and uses NSE timestamps. Run the Mac app for the full NSE + BSE view.
- **Read-only:** to change the hosted watchlist, edit `hosted_watchlist.txt` on GitHub and commit.
- **Run now:** on the repo, go to **Actions → Publish dashboard → Run workflow**.
- **60-day rule:** GitHub pauses scheduled workflows after 60 days without commits. Re-enable it from the Actions tab.

## Get it on another Mac
The app runs on each user's own Mac, because it pulls from NSE/BSE directly. GitHub only hosts the code.
1. On the repo page, click **Code → Download ZIP** and unzip it, or run `git clone https://github.com/virendeshpande121-stack/earnings-tracker.git`.
2. Double-click **Start Earnings Tracker.command**. If macOS blocks it, right-click it, choose **Open**, then confirm.
3. If the launcher won't run, open Terminal in the folder and run `chmod +x "Start Earnings Tracker.command"`.

Your watchlist, `config.json` and season state are not in the repo. Each person keeps their own.

## Run
- Double-click **Start Earnings Tracker.command**. It installs `requests` if it's missing, starts the server, and opens http://127.0.0.1:8765/.
- Terminal: `python3 earnings_tracker.py` (`--demo` for offline synthetic data, `--no-browser`, `--port N`).
- Tests: `python3 -m unittest discover -s tests -v`

## What it does
| Status | Rule |
|---|---|
| DECLARED | A results filing on NSE or BSE since the season start (quarter end + 1). The earliest timestamp wins, and links from both exchanges are kept. |
| AWAITED | The board meeting is today and no filing has been seen yet |
| UPCOMING | The board meeting is after today |
| OVERDUE | The meeting date has passed and no filing has been seen. Verify these manually. |
| UNSCHEDULED | No result-related board meeting has been announced |

Sources and cadence:
- **Fast loop (every 60s):** NSE announcements, BSE Result and Board Meeting announcements, and Nifty 500 prices. Watchlist quotes run every 5th cycle. From 01:00 to 07:00 IST the loop slows to the 15-minute interval.
- **Slow loop (every 15 min):**
  - Index lists, NSE EQUITY_L and the BSE scrip master (all refreshed daily and cached in `data/`)
  - NSE board meetings (season start to +45 days)
  - BSE results calendar, which only fills gaps
  - NSE results register (Integrated Filing – Financials)
- **One-time BSE backfill:** walks the season a few days per cycle, so the first full picture takes about 5 to 10 minutes.

Each source shows a chip in the header. Hover over a chip to see its error and the last time it succeeded. If one source fails, the others keep running.

## Files
| File | Purpose |
|---|---|
| `config.json` | Created automatically on first run. Holds the port, intervals, night hours, lookahead, `quarter_end_override` (e.g. `30-Sep-2026`), `alert_groups`, and email settings. |
| `watchlist.json` | Your watchlist. Add to it from the Watchlist tab, which takes symbols, BSE codes, ISINs, names or `NSE:SYM`. |
| `data/state.json` | Season state: meetings, declarations, feed and the backfill cursor. It resets automatically when the quarter changes. |
| `data/samples/` | Raw endpoint responses used by the tests |
| `demo_watchlist.json`, `data/demo_state.json` | Used only by `--demo` |

## Email alerts (optional)
In `config.json`, set `email.enabled: true`, `smtp_user` (your Gmail address), `smtp_app_password` (a 16-character Google App Password, not your login password), `to`, and `groups` (e.g. `["WATCH","N100"]`). The tracker sends one mail per cycle for new declarations in those groups. Backfilled filings older than 60 minutes never trigger a mail.

## Endpoint notes (verified 28-Oct-2026)
- NSE `/api/corporates-financial-results` now returns only legacy Dec-2024 rows. The results register uses `/api/integrated-filing-results?type=Integrated Filing- Financials` (paged with `page`/`size`) instead.
- NSE `/api/equity-stockIndices` returns 404. The correct path is `/api/equity-stock-indices`, which is tried first, with the old path as a fallback.
- NSE `/api/quote-equity` is blocked by Akamai (403) for scripts. Watchlist quotes fall back to `/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData`.
- Watchlist names listed only on BSE get no live price.
