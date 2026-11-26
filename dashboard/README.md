# SN121 Live Board

A local dashboard for the live sundae_bar Lab round. It shows the ranking, the queue of submissions waiting to be scored, and failed entries side by side, with username, miner UID and hotkey.

## Run

Use the same venv as the submit bot (Playwright with Chromium installed). From the `bot` folder:

    .venv\Scripts\python dashboard\live_board.py          # Windows
    .venv/bin/python dashboard/live_board.py              # Mac / Linux

Then open http://127.0.0.1:8121 in any browser. Leave the terminal window open; Ctrl+C stops it.

- The script reads the live round's public submissions page in a hidden browser once a minute (about 30 seconds per read).
- The page fetches the latest read every 20 seconds by itself; reloading the page shows it at once.
- **Read the Lab now** starts a read immediately instead of waiting for the next minute.
- The round is found automatically from the Lab, so nothing needs changing when a new round opens.

## Options

| Option | Meaning |
|---|---|
| `--round 40` | Show a specific round instead of the live one |
| `--every 30` | Seconds between reads (default 60, minimum 20) |
| `--port 8121` | Local port for the page |

## What the page shows

- **Summary line and tiles:** leader, leader score, the score needed to take the lead (leader + 1-point margin), submissions, waiting, miners, closing time.
- **Ranking:** scored entries by score. Green row = the site's current leader (which can sit below a higher score that did not beat it by the margin). Blue edge = submitted within the last hour. "Best per miner" keeps each miner's best entry. Click a hotkey to copy it.
- **Waiting:** uploaded but not scored yet, oldest first.
- **Failed:** evaluations that failed.
