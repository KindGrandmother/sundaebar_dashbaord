# SN121 submit bot

Submits a skill bundle to a sundae_bar Lab challenge by driving the real submit form in a browser window, with your own signed-in session. It handles no tokens, cookies or passwords. Human checks are left to you; the bot waits while one is on screen.

## Requirements

- Python 3.10 or newer
- `requirements.txt` (Playwright 1.65.2)
- A computer with a screen (the bot needs a visible window; the site's human check fails headless)
- A sundae_bar account with your wallet connected on your profile

## Setup (once)

Windows (PowerShell):

    python -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
    .venv\Scripts\python -m playwright install chromium

Mac / Linux:

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python -m playwright install chromium
    # Linux only, if Chromium will not start: sudo .venv/bin/python -m playwright install-deps chromium

The `playwright install chromium` step downloads the browser. Run it again whenever the Playwright version changes. It is not needed if you only use `--cdp`.

## Signing in: pick one

**A. Your own Chrome (recommended)**

1. Start Chrome with remote debugging and a separate profile folder (Chrome refuses debugging on your everyday profile). Windows, one line:

       & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\Users\<you>\.sundaebar-chrome"

   Mac: `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222 --user-data-dir="$HOME/.sundaebar-chrome"`
2. Open http://127.0.0.1:9222/json/version in that window. A short JSON page means the port is open.
3. Sign in to sundaebar.ai by hand and check that your wallet is connected. The login is kept in that profile folder.
4. Leave this Chrome open and add `--cdp` to every bot command. The bot opens its own tab and never closes your browser.

Close that Chrome when you are done: while it runs, any program on your computer can control it through port 9222.

**B. The bot's own browser profile**

    python submit_skill.py . --login

Sign in in the window that opens, check the wallet, then press Enter in the terminal. The login is kept in `~/.sundaebar-bot-profile`. If Google sign-in refuses this browser, add `--chrome` to use your installed Google Chrome, or use option A.

## Use

    python submit_skill.py my-bundle --dry-run                  # check the bundle only, upload nothing
    python submit_skill.py my-bundle --cdp --wallet 5FRCrE...vGNh
    python submit_skill.py my-bundle.zip --cdp                  # a ready .zip also works
    python submit_skill.py my-bundle/SKILL.md --cdp             # a single SKILL.md also works

`my-bundle` is a folder holding `SKILL.md` and an optional `references/` folder. Only those two go in the zip; any other files in the folder are listed and left out. A `.zip` is uploaded as is, without local checks.

You do not need to update the address each round: the bot opens https://www.sundaebar.ai/lab, reads the live round's link from its card, and goes to that round's page. If the Lab still shows an ended round, it checks once a minute until the new one appears. To force a page, use `--url https://www.sundaebar.ai/lab/challenges/<round>`.

Running without `--dry-run` ticks the terms-of-service and anti-memorization boxes on your behalf, and uses one of your daily submissions.

## What a run does

1. **Checks the bundle**: SKILL.md at most 10,000 bytes with `name` and `description` frontmatter; reference files lowercase, text types only (.md .txt .json .yaml .yml .csv), at most 30 files, 32,000 characters each and 128,000 in total; no HTML comments, hidden characters, hidden files or binary files.
2. **Waits for the round to open**: the site does not update an open page by itself, so the bot has to reload it. Rounds open at 12:00 PM US Eastern (17:00 UTC). It uses the opening time shown on the page, or 17:00 UTC today if the page shows none (`START_UTC` in the script). If the page names a later time, it still watches 17:00 UTC today in case that date is wrong: every 2 s for 20 s, every 5 s for 30 minutes, then once a minute for 4 hours. Otherwise it does not reload before the opening; then it reloads right away, every 2 s for 20 s, then every 5 s (`--reload`) until Contribute is enabled. It never reloads while a human check is on screen.
3. **Opens the form**: stops if you are signed out, if the wallet is not connected or differs from `--wallet`, or if the counter shows 0 submissions left.
4. **Fills the form**: attaches the zip and ticks both boxes (only if they are not ticked already). If the site wrongly flags the zip ("Needs changes", "The file must be a .md skill file"), the bot deletes it with the bin button and attaches it again, up to 5 times (`UPLOAD_TRIES` at the top of the script).
5. **Submits**: clicks Submit every second until the upload request goes out (the site can need a second click after its human check), then never clicks again. While a human check is on screen, it does nothing and waits.
6. **Reads the result**: success when the counter goes down or the site confirms; failure when the site reports similarity, already submitted, limits, integrity or another error. It prints what the site said, saves a screenshot and appends to `submit_log.txt`.

## Options

| Option | Meaning |
|---|---|
| `--dry-run` | Check the bundle only. |
| `--cdp [URL]` | Attach to your own Chrome (default `http://127.0.0.1:9222`). |
| `--login` | Open the bot's browser profile to sign in once. |
| `--chrome` | Use installed Google Chrome for the bot's own profile. |
| `--wallet 5FRCrE...vGNh` | Refuse to submit unless this wallet is connected (full address or as shown). |
| `--open-wait N` | Give up if Contribute stays disabled for N seconds (default 0 = wait until Ctrl+C). |
| `--url URL` | The challenge page to submit to (default: the live round on the Lab page). |
| `--reload N` | Once the opening time has come, reload every N seconds until Contribute is enabled (default 5, 0 = never). |
| `--wait N` | Seconds to wait for Submit to enable and for the result (default 600). |
| `--inspect` | Open the form and save `dialog_dump.txt` and `dialog.png`; upload nothing. |
| `--headless` | Hide the window (not recommended: the human check usually fails). |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Submitted (counter went down or the site confirmed), or `--inspect` / `--dry-run` finished |
| 1 | Bundle problems (listed) |
| 2 | Wallet not connected |
| 3 | No submissions left (challenge or daily limit) |
| 4 | Not signed in |
| 5 | No Chrome listening at the `--cdp` address |
| 6 | Form not recognised (`dialog_dump.txt` and `dialog.png` saved) |
| 7 | No result within `--wait` seconds; check the Submissions page (tab left open) |
| 8 | Connected wallet differs from `--wallet` |
| 9 | The site rejected the submission (messages printed and logged) |
| 10 | Contribute stayed disabled, or no new round appeared, for `--open-wait` seconds |
| 11 | The site still flagged the zip after 5 attach attempts (`dialog_dump.txt` and `dialog.png` saved) |
| 12 | The page given with `--url` says the round has ended |
| 13 | The live round could not be read from the Lab page |

## Files the bot writes (next to the script)

- `after_submit.png`: screenshot after the result
- `submit_log.txt`: one entry per submission with the outcome, the site's messages and the server responses
- `dialog_dump.txt`, `dialog.png`: only with `--inspect` or when the form is not recognised

## Troubleshooting

- **"Executable doesn't exist ... chrome.exe"**: run `python -m playwright install chromium` inside the venv.
- **"No Chrome is listening at http://127.0.0.1:9222"**: Chrome was not started with the command above, or was closed. Check http://127.0.0.1:9222/json/version.
- **"Not logged in"**: the form showed "Sign in to submit" for 10 seconds. Sign in in the browser the bot actually uses: with `--cdp`, the Chrome started with `--remote-debugging-port` (its own profile, not your everyday Chrome); without `--cdp`, the bot's profile from `--login`. If you are signed in there, send `dialog_dump.txt` and `dialog.png`.
- **Exit 6, or the form looks different**: run with `--inspect` and send `dialog_dump.txt` and `dialog.png`.
- **Exit 7 although the site showed a message**: the wording was not recognised. Send the entry from `submit_log.txt` so it can be added.
