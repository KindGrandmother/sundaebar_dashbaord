#!/usr/bin/env python3
"""
Submit a SKILL.md bundle to a sundae_bar SN121 challenge through a real browser.

Why a browser: the site uses a Next.js Server Action whose ID changes on every
deploy, so replaying the raw request breaks. Driving the real form is stable
and uses your own logged-in session. No tokens or cookies are handled here.

Setup (once):
    pip install playwright
    playwright install chromium

First run (log in by hand, wallet must already be connected on your profile):
    python submit_skill.py ./my-skill --login

Normal runs:
    python submit_skill.py ./my-skill --dry-run     # validate only, no upload
    python submit_skill.py ./my-skill               # validate, then submit
    python submit_skill.py ./my-skill/SKILL.md      # single file also works

The browser window is shown by default: the site's human check fails in
headless mode. If a check appears in the window, complete it by hand.
Only SKILL.md and references/ are zipped; other files in the folder stay out.

By running without --dry-run you agree, on your own behalf, to the two
checkboxes in the form (terms of service + anti-memorization rule).
"""
import argparse
import re
import sys
import tempfile
import zipfile
from pathlib import Path

LAB_URL = "https://www.sundaebar.ai/lab"  # its live-round card links to the current round's page
CHALLENGE_URL = None  # --url: use this round's page instead of finding it on LAB_URL
PROFILE_DIR = Path.home() / ".sundaebar-bot-profile"  # persistent browser profile
USE_CHROME = False  # --chrome: use the installed Google Chrome instead of Playwright's Chromium
CDP_URL = None      # --cdp: attach to a Chrome you started yourself (see README)
INSPECT = False     # --inspect: open the dialog, save what it shows, upload nothing
EXPECT_WALLET = None  # --wallet: refuse to submit unless this wallet is the one connected
WAIT_LIMIT_S = 600    # --wait: how long to wait for Submit / the result (a human check pauses here)
MAX_CLICKS = 3        # Submit is clicked once; again only after a human check (each click can pay)
OPEN_WAIT_S = 0       # --open-wait: max seconds to wait for Contribute to enable (0 = no limit)
RELOAD_EVERY_S = 5    # --reload: reload this often once the opening time has come (0 = never)
NEW_ROUND_CHECK_S = 60  # while the Lab still shows an ended round, look for the new one this often
START_UTC = "17:00:00"  # rounds open at 12:00 PM US Eastern (winter) = 17:00 UTC; used if the page shows no opening time
UPLOAD_TRIES = 5     # times to remove and re-attach the zip when the site wrongly flags it
HERE = Path(__file__).resolve().parent


SUBMIT_NAME = re.compile(r"\bsubmit\b", re.I)  # "Submit", "Pay 0.05 TAO and submit", ...
MAX_FEE_TAO = 0.05    # --max-fee: refuse to submit if the button asks for more TAO than this

# Tags the form (data-sbbot-form) and its Submit button (data-sbbot-submit). Returns the
# button's label, or null if no Submit button sits near the visible counter.
MARK_FORM_JS = r"""() => {
  document.querySelectorAll('[data-sbbot-form],[data-sbbot-submit]').forEach(e => {
    e.removeAttribute('data-sbbot-form'); e.removeAttribute('data-sbbot-submit'); });
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const counterRe = /submissions left/i;
  const label = b => (b.innerText || b.value || b.getAttribute('aria-label') || '').trim();
  const leaves = [...document.querySelectorAll('body *')].filter(e => vis(e) && counterRe.test(e.textContent)
      && ![...e.children].some(c => counterRe.test(c.textContent)));
  const tests = [b => /\bsubmit\b/i.test(label(b)) && !/sign in/i.test(label(b)),  // "Pay 0.05 TAO and submit"...
                 b => b.getAttribute('type') === 'submit' && !/sign in|disconnect|cancel|close/i.test(label(b))];
  for (const test of tests) {
    for (const leaf of leaves) {
      for (let e = leaf.parentElement; e && e !== document.documentElement; e = e.parentElement) {
        const b = [...e.querySelectorAll('button, input[type=submit]')].find(x => vis(x) && test(x));
        if (b) { e.setAttribute('data-sbbot-form', '1'); b.setAttribute('data-sbbot-submit', '1'); return label(b); }
      }
    }
  }
  return null;
}"""


def dump_dialog(page, dialog, why: str) -> None:
    """Save the dialog's text, controls and a screenshot so selectors can be matched to the live form."""
    out = HERE / "dialog_dump.txt"
    lines = [f"# {why}", "", "## Dialog text", dialog.inner_text() if dialog.count() else "(no dialog)", ""]
    if dialog.count():
        lines.append("## Controls (tag | type | id | name | role | aria-label | text)")
        for el in dialog.locator("input, button, [role=checkbox], [role=button], label, select, textarea").all():
            info = el.evaluate(
                "e => [e.tagName, e.type||'', e.id||'', e.name||'', e.getAttribute('role')||'', "
                "e.getAttribute('aria-label')||'', (e.innerText||'').trim().slice(0,80), e.disabled===true]"
            )
            lines.append(" | ".join(str(x) for x in info))
    out.write_text("\n".join(lines), encoding="utf-8")
    page.screenshot(path=str(HERE / "dialog.png"))
    print(f"{why}\nSaved {out.name} and dialog.png in {HERE}. Send both to fix the bot.")

# Limits published on the challenge page (the site may change them).
SKILL_MAX_BYTES = 10_000
REF_MAX_FILES = 30
REF_MAX_CHARS = 32_000
REF_TOTAL_CHARS = 128_000
ALLOWED_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}
NAME_RE = re.compile(r"^[a-z0-9._-]+$")
INVISIBLE_RE = re.compile("[​-‏‪-‮⁠-⁤﻿]")


def validate(path: Path) -> list[str]:
    """Return a list of problems; empty list means the bundle looks OK."""
    problems: list[str] = []

    if path.is_file() and path.suffix == ".zip":
        print("Zip supplied: skipping local validation (the site validates it).")
        return problems

    if path.is_file():
        skill = path
        refs: list[Path] = []
    else:
        skill = path / "SKILL.md"
        refs_dir = path / "references"
        refs = sorted(p for p in refs_dir.rglob("*") if p.is_file()) if refs_dir.exists() else []

    if skill.name != "SKILL.md" or not skill.exists():
        return ["SKILL.md not found at the top level."]
    if path.is_dir():
        extra = [f.name for f in path.iterdir() if f.name not in ("SKILL.md", "references")]
        if extra:
            print("Note: not uploaded (only SKILL.md and references/ go in the zip):", ", ".join(sorted(extra)))
    hidden = [f for f in refs if any(part.startswith(".") for part in f.relative_to(path).parts)] if path.is_dir() else []
    for f in hidden:
        problems.append(f"{f.relative_to(path)}: hidden file inside references/ (rejected at upload).")

    text = skill.read_text(encoding="utf-8")
    size = len(text.encode("utf-8"))
    if size > SKILL_MAX_BYTES:
        problems.append(f"SKILL.md is {size} bytes (max {SKILL_MAX_BYTES}).")
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not fm:
        problems.append("SKILL.md needs YAML frontmatter with name and description.")
    else:
        for key in ("name:", "description:"):
            if key not in fm.group(1):
                problems.append(f"Frontmatter is missing '{key}'.")

    all_files = [skill] + refs
    total = 0
    if len(refs) > REF_MAX_FILES:
        problems.append(f"{len(refs)} reference files (max {REF_MAX_FILES}).")
    for f in all_files:
        try:
            body = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"{f.name}: not UTF-8 text (binary files are rejected).")
            continue
        if "<!--" in body:
            problems.append(f"{f.name}: contains an HTML comment (rejected at upload).")
        if INVISIBLE_RE.search(body):
            problems.append(f"{f.name}: contains hidden/invisible characters.")
        if f in refs:
            total += len(body)
            if f.suffix.lower() not in ALLOWED_EXT:
                problems.append(f"{f.name}: extension not allowed.")
            if not NAME_RE.match(f.name):
                problems.append(f"{f.name}: use lowercase letters, digits, dots, dashes, underscores.")
            if len(body) > REF_MAX_CHARS:
                problems.append(f"{f.name}: {len(body)} chars (max {REF_MAX_CHARS}).")
    if total > REF_TOTAL_CHARS:
        problems.append(f"References total {total} chars (max {REF_TOTAL_CHARS}).")
    return problems


def prepare_upload(path: Path) -> Path:
    """Folders are zipped to a temp file; single files and zips are uploaded as-is."""
    if path.is_file():
        return path
    out = Path(tempfile.mkdtemp()) / f"{path.resolve().name}.zip"
    files = [path / "SKILL.md"]
    if (path / "references").is_dir():
        files += sorted(f for f in (path / "references").rglob("*") if f.is_file())
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(path).as_posix())
    print("Zip contents:", ", ".join(zipfile.ZipFile(out).namelist()))
    return out


def run_browser(upload: Path | None, login_only: bool, headless: bool) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        if CDP_URL:
            # Your own Chrome window: the bot opens one tab and never closes your browser.
            try:
                browser = p.chromium.connect_over_cdp(CDP_URL)
            except Exception as e:
                print(f"No Chrome is listening at {CDP_URL} ({str(e).splitlines()[0]}).")
                print("Start Chrome with --remote-debugging-port=9222 and its own --user-data-dir "
                      "(see README), check http://127.0.0.1:9222/json/version opens, then run again.")
                return 5
            own = browser.contexts[0]
            page = own.new_page()
            ctx = page  # closing ctx below closes only this tab
        else:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=headless and not login_only,
                channel="chrome" if USE_CHROME else None,
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        import time
        if login_only:
            page.goto(LAB_URL, wait_until="domcontentloaded")
            input("Log in in the browser window, make sure your wallet is connected, "
                  "then press Enter here...")
            ctx.close()
            return 0

        def find_live_round():
            """Address of the current round's page, from the live-round card on the Lab page."""
            from urllib.parse import urljoin
            page.goto(LAB_URL, wait_until="domcontentloaded")
            link_sel = 'a[href*="/lab/challenges/sbc"]'
            page.locator(link_sel).first.wait_for(state="attached", timeout=20_000)
            href = page.evaluate("""sel => {
                // the link in the smallest box that also holds the Contribute button
                const b = [...document.querySelectorAll('button')].find(x => x.innerText.trim() === 'Contribute');
                for (let e = b && b.parentElement; e; e = e.parentElement) {
                    const a = e.querySelector(sel);
                    if (a) return a.getAttribute('href');
                }
                // no Contribute on the page: the newest round number
                const n = a => +(/sbc(\\d+)/.exec(a.getAttribute('href')) || [0, 0])[1];
                const all = [...document.querySelectorAll(sel)].sort((x, y) => n(y) - n(x));
                return all.length ? all[0].getAttribute('href') : null;
            }""", link_sel)
            return urljoin(LAB_URL, href).split("?")[0].split("#")[0]

        round_url = CHALLENGE_URL
        if not round_url:
            for tries_left in (2, 1, 0):
                try:
                    round_url = find_live_round()
                    break
                except Exception as e:
                    print("Could not read the live round from the Lab page:", str(e).splitlines()[0])
                    if not tries_left:
                        print("Pass the round's page with --url. Nothing submitted.")
                        ctx.close()
                        return 13
                    page.wait_for_timeout(5_000)
        print("Round page:", round_url)
        sent_at = time.time()
        first_resp = page.goto(round_url, wait_until="domcontentloaded")

        page.wait_for_timeout(3_000)

        def dismiss_cookies():
            cookie = page.get_by_role("button", name="Reject all")
            if cookie.count():
                try:
                    cookie.first.click(timeout=2_000)
                except Exception:
                    pass

        dismiss_cookies()

        # Wait for the round to open. The site does not update an open page by itself, so a page
        # loaded before the start keeps a disabled Contribute button until it is reloaded. The page
        # carries its own opening time: wait for that moment, reload right then, and keep reloading
        # every RELOAD_EVERY_S seconds until the button is enabled.
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        open_source = ""
        clock_offset = 0.0  # site clock minus this computer's clock, in seconds

        def sync_clock(resp, sent_at):
            """Compare the site's Date header with the moment the request was sent. The header has
            whole seconds and is stamped after sending, so half a second is the fair middle."""
            nonlocal clock_offset
            try:
                offset = parsedate_to_datetime(resp.headers["date"]).timestamp() + 0.5 - sent_at
                clock_offset = offset if abs(offset) > 2 else 0.0  # small differences are just rounding
            except Exception:
                pass

        def since_usual_start():
            """Seconds since START_UTC of the current day by the site's clock (negative = still ahead)."""
            now = time.time() + clock_offset
            h, m, sec = (int(x) for x in START_UTC.split(":"))
            day = datetime.fromtimestamp(now, timezone.utc)
            return now - day.replace(hour=h, minute=m, second=sec, microsecond=0).timestamp()

        def seconds_to_open():
            """Seconds until submissions open, by the site's clock (negative = passed). Uses the
            page's own opening time; if it shows none, START_UTC of the current day."""
            nonlocal open_source
            now = time.time() + clock_offset
            try:
                found = set(re.findall(r'submissions_open_at\\*":\s*\\*"([^"\\]+)', page.content()))
                if len(found) == 1:
                    at = datetime.fromisoformat(found.pop().replace("Z", "+00:00"))
                    if at.tzinfo is None:
                        at = at.replace(tzinfo=timezone.utc)
                    open_source = "from the page"
                    return at.timestamp() - now
            except Exception:
                pass
            open_source = "the usual start"
            return -since_usual_start()

        def check_on_screen():
            """A human check is showing: reloading would interrupt it."""
            try:
                return any(f.is_visible() for f in
                           page.locator('iframe[src*="challenges.cloudflare.com"]').all())
            except Exception:
                return False

        sync_clock(first_resp, sent_at)
        contribute = page.get_by_role("button", name="Contribute").first
        ended = page.get_by_text(re.compile(r"this challenge has ended", re.I)).filter(visible=True)
        start = last_reload = last_note = time.monotonic()
        announced = waiting_new = False
        left = seconds_to_open()
        open_mark = time.monotonic() + left  # opening time on our timer
        while True:
            try:
                ready = contribute.count() > 0 and contribute.is_enabled()
            except Exception:
                ready = False
            if ready:
                if announced:
                    print(f"Contribute is enabled after {int(time.monotonic() - start)} s. Submitting.")
                break
            try:
                is_over = ended.count() > 0
            except Exception:
                is_over = False
            if is_over and not CHALLENGE_URL:
                # The Lab still shows the old round: look for the new one every minute.
                if OPEN_WAIT_S and time.monotonic() - start >= OPEN_WAIT_S:
                    print(f"No new round after {OPEN_WAIT_S} s. Nothing submitted.")
                    ctx.close()
                    return 10
                if not waiting_new:
                    print("That round has ended. Checking the Lab every minute for the next one; Ctrl+C to stop.")
                    waiting_new = True
                page.wait_for_timeout(NEW_ROUND_CHECK_S * 1_000)
                try:
                    newest = find_live_round()
                    sent_at = time.time()
                    sync_clock(page.goto(newest, wait_until="domcontentloaded"), sent_at)
                    page.wait_for_timeout(2_000)
                    dismiss_cookies()
                    if newest != round_url:
                        print("New round page:", newest)
                        round_url, waiting_new, announced = newest, False, False
                    last_reload = time.monotonic()
                    open_mark = time.monotonic() + seconds_to_open()
                except Exception as e:
                    print("  check failed, retrying:", str(e).splitlines()[0])
                    try:
                        page.goto(round_url, wait_until="domcontentloaded")
                    except Exception:
                        pass
                continue
            if is_over:
                print("This challenge has ended, so Contribute will never be enabled on this page:\n  " + round_url
                      + "\nLeave out --url to use the live round. Nothing submitted.")
                ctx.close()
                return 12
            now = time.monotonic()
            ahead = open_mark - now
            if not announced:
                print("Contribute is disabled. Checking every second; Ctrl+C to stop.")
                if ahead > 0:
                    at = datetime.fromtimestamp(time.time() + clock_offset + ahead, timezone.utc)
                    print(f"Submissions open in {int(ahead)} s, at {at:%d %b %H:%M:%S} UTC "
                          f"({open_source}); the page will be reloaded at that moment.")
                    usual = -since_usual_start()
                    if 0 < usual < ahead - 1:
                        print(f"Also watching the usual start, {START_UTC} UTC today (in {int(usual)} s), "
                              "in case the page's date is wrong.")
                announced = True
            if OPEN_WAIT_S and now - start >= OPEN_WAIT_S:
                print(f"Contribute stayed disabled for {OPEN_WAIT_S} s. Nothing submitted.")
                ctx.close()
                return 10
            if now - last_note >= 60:
                print(f"  still disabled ({int(now - start)} s"
                      + (f", opens in {int(ahead)} s)" if ahead > 0 else ")"))
                last_note = now
            since = now - last_reload
            if not RELOAD_EVERY_S:
                due = False
            elif ahead > 0:
                # The page names a later time, which can be wrong. Still refresh at the usual start:
                # every 2 s for 20 s, every RELOAD_EVERY_S for 30 min, then once a minute for 4 hours.
                past = since_usual_start()
                every = min(2, RELOAD_EVERY_S) if past < 20 else RELOAD_EVERY_S if past < 1800 else max(60, RELOAD_EVERY_S)
                due = 0 <= past <= 4 * 3600 and (since > past or since >= every)
            else:                                    # opening time reached
                # every 2 s for the first 20 s, in case the site opens a moment late
                due = last_reload < open_mark or since >= (min(2, RELOAD_EVERY_S) if ahead > -20 else RELOAD_EVERY_S)
            if due and not check_on_screen():
                last_reload = time.monotonic()
                try:
                    sent_at = time.time()
                    sync_clock(page.reload(wait_until="domcontentloaded"), sent_at)
                    page.wait_for_timeout(1_000)  # let the page's scripts start
                    dismiss_cookies()
                    left = seconds_to_open()
                    open_mark = time.monotonic() + left
                except Exception as e:
                    print("  reload failed, retrying next time:", str(e).splitlines()[0])
                continue
            # wake right at the opening time or the usual start
            nap = min([1.0] + [max(x, 0.05) for x in (ahead, -since_usual_start()) if 0 < x < 1])
            page.wait_for_timeout(nap * 1_000)

        contribute.click()

        # The submit form is identified by its own text, not by "the first dialog on the page"
        # (a cookie banner or another popup can also be a dialog).
        counter_re = re.compile(r"(\d+)\s+of\s+(\d+)\s+submissions left.*?daily limit:\s*(\d+)\s+left", re.I | re.S)
        # Only visible text counts: the page can hold a hidden "Sign in to submit" block, and the
        # form can show it for a moment while your session is still loading. So the counter wins
        # whenever it appears, and "signed out" is decided only if that text stays visible for 10 s
        # with no counter.
        signed_out = page.get_by_text(re.compile(r"sign in to submit", re.I)).filter(visible=True)
        counter = page.get_by_text(re.compile(r"submissions left", re.I)).filter(visible=True)
        signed_out_since = None
        any_submit = page.get_by_role("button", name=SUBMIT_NAME).filter(visible=True)
        for i in range(60):  # up to ~30 s
            if counter.count():
                break
            # A click right after a reload can land before the page's scripts are ready and do nothing.
            if i and i % 6 == 0 and not signed_out.count() and not any_submit.count():
                try:
                    contribute.click(timeout=1_000)
                except Exception:
                    pass
            if signed_out.count():
                signed_out_since = signed_out_since or time.monotonic()
                if time.monotonic() - signed_out_since >= 10:
                    break
            else:
                signed_out_since = None
            page.wait_for_timeout(500)
        if not counter.count() and signed_out.count():
            dump_dialog(page, page.get_by_role("dialog").last if page.get_by_role("dialog").count() else page.locator("body"),
                        "The form says 'Sign in to submit'.")
            print("Not logged in, according to the form. " + ("Sign in to sundaebar in the Chrome window started with --remote-debugging-port, then run again." if CDP_URL else "Run with --login first, or use --cdp with your own Chrome.") + " Aborting.")
            print("If you are signed in there, send dialog_dump.txt and dialog.png.")
            ctx.close()
            return 4
        # The form is the smallest box around the visible counter that also holds the Submit
        # button. The page's own script finds and tags both, so a changed button label
        # ("Submit skill", ...) or a changed layout does not break the bot.
        def mark_form():
            try:
                return page.evaluate(MARK_FORM_JS)
            except Exception:
                return None

        submit_label = None
        for _ in range(20):  # up to ~10 s for the form to finish drawing
            submit_label = mark_form()
            if submit_label is not None:
                break
            page.wait_for_timeout(500)
        form = page.locator("[data-sbbot-form]").first
        submit = page.locator("[data-sbbot-submit]").first

        def form_ready():
            """Re-tag the form if the page redrew it."""
            if not form.count() or not submit.count():
                mark_form()
            return form.count() > 0

        if not INSPECT and counter.count() and submit_label is None:
            page.wait_for_timeout(1_000)
            dump_dialog(page, page.get_by_role("dialog").last if page.get_by_role("dialog").count() else page.locator("body"),
                        "Found the 'submissions left' counter but no Submit button near it. Nothing uploaded.")
            ctx.close()
            return 6
        if submit_label and submit_label.lower() != "submit":
            print(f"Submit button found (label: {submit_label!r}).")
        # Submitting now costs TAO, paid from your wallet when you click. Read the price from
        # the button (or the form) and refuse anything above MAX_FEE_TAO.
        fee = None
        try:
            m = re.search(r"([\d.]+)\s*TAO", (submit_label or "") + "\n" + form.inner_text(timeout=5_000), re.I)
            fee = float(m.group(1)) if m else None
        except Exception:
            pass
        if fee is not None and not INSPECT:
            if fee > MAX_FEE_TAO + 1e-9:
                print(f"This submission costs {fee:g} TAO, above --max-fee {MAX_FEE_TAO:g}. Nothing submitted.")
                ctx.close()
                return 14
            print(f"This submission costs {fee:g} TAO from your wallet. Approve it in your wallet if it asks.")

        if INSPECT or not counter.count():
            page.wait_for_timeout(2_000)
            dump_dialog(page, form if form.count() else page.get_by_role("dialog").last,
                        "Inspect mode: nothing uploaded." if INSPECT
                        else "Could not find the 'submissions left' text. Nothing uploaded.")
            ctx.close()
            return 0 if INSPECT else 6

        def read_counter():
            try:
                form_ready()
                m = counter_re.search(form.inner_text(timeout=5_000))
            except Exception:
                return None
            return tuple(int(x) for x in m.groups()) if m else None

        before = read_counter()
        print("Before:", counter.first.inner_text().strip())
        if form.get_by_text("Wallet connected").count() == 0:
            print("Wallet is not connected on your profile. Aborting.")
            ctx.close()
            return 2
        shown = form.get_by_text("Wallet connected").locator("xpath=following-sibling::*[1]").inner_text().strip()
        print("Wallet:", shown)
        if EXPECT_WALLET:
            head, _, tail = shown.replace("\u2026", "...").partition("...")
            want = EXPECT_WALLET.replace("\u2026", "...")
            ok = want == shown.replace("\u2026", "...") or (want.startswith(head) and want.endswith(tail))
            if not ok:
                print(f"Connected wallet {shown} does not match --wallet {EXPECT_WALLET}. Aborting.")
                ctx.close()
                return 8
        if before and (before[0] == 0 or before[2] == 0):
            print("No submissions left. Aborting.")
            ctx.close()
            return 3

        # Attach the zip. The site sometimes rejects a valid zip right after attaching it
        # ("Needs changes" / "The file must be a .md skill file"); removing the file and
        # attaching it again clears that, so the bot does exactly that, up to UPLOAD_TRIES times.
        bad_re = re.compile(r"needs changes|must be a \.md|only skill\.md .*allowed", re.I)

        def file_input():
            fi = form.locator('input[type="file"]:not([webkitdirectory])')
            return (fi if fi.count() else form.locator('input[type="file"]')).first

        def upload_state():
            """'bad' (site flagged the file), 'ok' (file shown without errors) or 'pending'."""
            try:
                text = form.inner_text()
            except Exception:
                return "pending"
            if upload.name not in text:
                return "pending"
            return "bad" if bad_re.search(text) else "ok"

        def remove_file():
            """Click the delete button on the attached file's card."""
            card = (form.locator("div")
                    .filter(has_text=upload.name)
                    .filter(has=page.locator("button"))
                    .last)  # innermost box holding the file name and its delete button
            btn = card.locator("button").last
            btn.click(timeout=5_000)
            for _ in range(20):  # wait up to 10 s for the file to disappear
                if upload.name not in form.inner_text():
                    return
                page.wait_for_timeout(500)

        for attempt in range(1, UPLOAD_TRIES + 1):
            file_input().set_input_files(str(upload))
            state = "pending"
            for _ in range(10):  # up to 5 s for the site to check the file
                page.wait_for_timeout(500)
                state = upload_state()
                if state == "bad":
                    break
            if state != "bad":
                print("Attached:", upload.name + (f" (attempt {attempt})" if attempt > 1 else ""))
                break
            msgs = [l.strip() for l in form.inner_text().splitlines() if bad_re.search(l)]
            print(f"Attempt {attempt}: the site flagged the file ({'; '.join(msgs)}). "
                  + ("Removing it and attaching again." if attempt < UPLOAD_TRIES else "Removing it."))
            try:
                remove_file()
            except Exception as e:
                print("  could not click the delete button:", str(e).splitlines()[0])
                try:
                    file_input().set_input_files([])
                except Exception:
                    pass
            page.wait_for_timeout(1_000)
        else:
            dump_dialog(page, form, f"The site still flags the file after {UPLOAD_TRIES} attempts. Nothing submitted.")
            ctx.close()
            return 11
        for box in ("terms", "rules"):
            el = page.locator(f"#{box}")
            if el.get_attribute("aria-checked") != "true" and el.get_attribute("data-state") != "checked":
                el.click()

        challenge = page.locator('iframe[src*="challenges.cloudflare.com"]')

        def human_check_visible():
            try:
                return any(f.is_visible() for f in challenge.all())
            except Exception:
                return False

        # 1) Wait for Submit to enable. A human check may appear here: never time out on it,
        #    never touch it; keep the tab open until the person completes it.
        print("Waiting for Submit to enable...")
        told = False
        waited = 0
        def submit_disabled():
            try:
                form_ready()
                return submit.is_disabled(timeout=3_000)
            except Exception:
                return True  # redrawn or not there yet: check again next second

        while submit_disabled():
            if human_check_visible() and not told:
                print("Human check shown in the browser window: please complete it. Waiting...")
                told = True
            page.wait_for_timeout(1_000)
            waited += 1
            if waited >= WAIT_LIMIT_S:
                print(f"Submit stayed disabled for {WAIT_LIMIT_S} s. Nothing submitted. Tab left open.")
                return 7

        # 2) Record everything the site says after the click.
        responses = []

        def on_response(resp):
            req = resp.request
            if req.method == "POST" and ("next-action" in req.headers or "submission" in req.url.lower()):
                try:
                    body = resp.text()
                except Exception:
                    body = ""
                responses.append((resp.status, req.url, body))

        upload_sent = []  # the POST that carries the file; once it has gone, never click again

        def on_request(req):
            if req.method != "POST":
                return
            ctype = (req.headers.get("content-type") or "").lower()
            size = len(req.post_data_buffer or b"") if ctype else 0
            if "multipart/form-data" in ctype or ("next-action" in req.headers and size > 2000):
                upload_sent.append(req.url)
                print("Upload request sent; no more clicks.")

        page.on("request", on_request)
        page.on("response", on_response)
        form_text_before = form.inner_text()
        clicks = 0

        notice_sel = "[data-sonner-toast], [role=alert], [role=status], [aria-live]"
        error_re = re.compile(
            r"insufficient|cancell?ed|denied|declined|user rejected|similar|similarity|already (been )?submitted|duplicate|rejected|reject|error|failed|"
            r"fail|not allowed|limit reached|no submissions left|too many|invalid|exceed|integrity|"
            r"under review|try again|verification failed", re.I)
        success_re = re.compile(r"submitted|success|received|queued|evaluat|thank", re.I)

        def collect_messages():
            msgs = []
            for el in page.locator(notice_sel).all():
                try:
                    t = el.inner_text().strip()
                except Exception:
                    continue
                if t:
                    msgs.append(t)
            if form.count():
                try:
                    old = set(form_text_before.splitlines())
                    msgs += [l.strip() for l in form.inner_text().splitlines()
                             if l.strip() and l not in old and not counter_re.search(l)]
                except Exception:
                    pass
            for status, url, body in responses:
                if status >= 400:
                    msgs.append(f"HTTP {status} from {url}")
                for key, val in re.findall(r'"(error|message|detail|reason)"\s*:\s*"([^"]{3,400})"', body or ""):
                    if error_re.search(val) or status >= 400:
                        msgs.append(f"server {key}: {val}")
            seen, out = set(), []
            for m in msgs:
                if m not in seen:
                    seen.add(m)
                    out.append(m)
            return out

        outcome, messages, told = None, [], False
        waited = 0
        check_since_click = False  # a human check appeared after our last click
        while waited < WAIT_LIMIT_S:
            if human_check_visible():
                if not told:
                    print("Human check shown in the browser window: waiting for it to finish...")
                    told = True
                check_since_click = True
                page.wait_for_timeout(1_000)
                waited += 1
                continue  # never click or decide anything while a check is on screen
            told = False
            messages = collect_messages()
            if any(error_re.search(m) for m in messages):
                outcome = "error"
                break
            now = read_counter() if form.count() else None
            if before and now and now[0] < before[0]:
                outcome = "ok"
                break
            if any(success_re.search(m) for m in messages):
                outcome = "ok"
                break
            # Submitting pays a fee, so the bot clicks once and then waits (for your wallet approval,
            # the upload and the result). It clicks again only if a human check came and went after
            # its last click and the upload has not gone out; the site can drop a click then.
            if not upload_sent and clicks < MAX_CLICKS and (clicks == 0 or check_since_click):
                try:
                    form_ready()
                    if submit.count() and submit.is_visible() and submit.is_enabled():
                        submit.click(timeout=2_000)
                        clicks += 1
                        check_since_click = False
                        print("Clicked Submit." if clicks == 1 else
                              f"Clicked Submit again after the human check ({clicks}).")
                        if fee is not None and clicks == 1:
                            print("If your wallet asks you to approve the payment, approve it there.")
                except Exception:
                    pass
            page.wait_for_timeout(1_000)
            waited += 1
        page.wait_for_timeout(1_500)
        messages = collect_messages() or messages
        shot = HERE / "after_submit.png"
        page.screenshot(path=str(shot))
        log = HERE / "submit_log.txt"
        with open(log, "a", encoding="utf-8") as fh:
            import datetime
            fh.write(f"\n[{datetime.datetime.now().isoformat(timespec='seconds')}] {upload.name} -> {outcome}\n")
            for m in messages:
                fh.write("  " + m + "\n")
            for status, url, body in responses:
                fh.write(f"  POST {status} {url}\n  body: {re.sub(chr(10), ' ', (body or '')[:2000])}\n")
        page.remove_listener("response", on_response)
        page.remove_listener("request", on_request)

        if messages:
            print("Site said:")
            for m in messages:
                print("  -", m[:300])
        if outcome == "ok":
            after = read_counter() if form.count() else None
            print("Submitted." + (f" Now {after[0]} of {after[1]} left, daily {after[2]} left." if after else ""),
                  f"Screenshot: {shot}")
            if not CDP_URL:
                ctx.close()
            return 0
        if outcome == "error":
            print(f"Submission was NOT accepted (see messages above). Screenshot: {shot}. Log: {log}")
            return 9
        print(f"No result seen within {WAIT_LIMIT_S} s. Check the Submissions page. Screenshot: {shot}. Log: {log}")
        return 7


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path, help="Skill folder, .zip, or SKILL.md")
    ap.add_argument("--dry-run", action="store_true", help="validate only")
    ap.add_argument("--login", action="store_true", help="open browser to log in once")
    ap.add_argument("--headless", action="store_true",
                    help="hide the browser window (the site's human check usually fails headless)")
    ap.add_argument("--cdp", nargs="?", const="http://127.0.0.1:9222", default=None,
                    help="attach to a Chrome you started with --remote-debugging-port (default port 9222)")
    ap.add_argument("--wallet", default=None,
                    help="expected wallet, full address or as shown (e.g. 5FRCrE...vGNh); abort if another is connected")
    ap.add_argument("--wait", type=int, default=600, help="seconds to wait for Submit and for the result (default 600)")
    ap.add_argument("--open-wait", type=int, default=0,
                    help="max seconds to wait for Contribute to become enabled (default 0 = wait until Ctrl+C)")
    ap.add_argument("--url", help="challenge page to submit to (default: the live round on the Lab page)")
    ap.add_argument("--reload", type=int, default=5,
                    help="reload every N seconds once the opening time has come (default 5, 0 = never)")
    ap.add_argument("--max-fee", type=float, default=0.05,
                    help="refuse to submit if the fee is above this many TAO (default 0.05)")
    ap.add_argument("--inspect", action="store_true",
                    help="open the submit dialog and save its text/controls/screenshot; uploads nothing")
    ap.add_argument("--chrome", action="store_true",
                    help="use the installed Google Chrome (helps when Google sign-in refuses Chromium)")
    args = ap.parse_args()
    global CHALLENGE_URL, USE_CHROME, CDP_URL, INSPECT, EXPECT_WALLET, WAIT_LIMIT_S, OPEN_WAIT_S, RELOAD_EVERY_S, MAX_FEE_TAO
    if args.url:
        CHALLENGE_URL = args.url
    USE_CHROME = args.chrome
    CDP_URL = args.cdp
    INSPECT = args.inspect
    EXPECT_WALLET = args.wallet
    WAIT_LIMIT_S = args.wait
    OPEN_WAIT_S = args.open_wait
    RELOAD_EVERY_S = args.reload
    MAX_FEE_TAO = args.max_fee

    if args.login:
        return run_browser(None, True, False)

    problems = validate(args.path)
    if problems:
        print("Fix these before submitting:")
        for pr in problems:
            print(" -", pr)
        return 1
    print("Local validation passed.")
    if args.dry_run:
        return 0

    return run_browser(prepare_upload(args.path), False, args.headless)


if __name__ == "__main__":
    sys.exit(main())
