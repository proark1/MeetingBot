"""Real browser-based meeting bot using Playwright.

Strategy:
  - Headed Chromium on a virtual framebuffer (Xvfb) — more compatible with
    Google Meet / Teams than pure headless mode.
  - PulseAudio null sink set as the default audio device so Chromium routes
    all audio there automatically.
  - ffmpeg records from the null-sink monitor → 16 kHz WAV for Whisper.
  - Stealth JS + Chrome flags hide automation signals from Google/Microsoft.
  - `on_admitted` async callback lets the caller react when the bot is let in.
"""

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logger = logging.getLogger(__name__)

PULSE_SINK_NAME = "meetingbot_sink"
SCREENSHOT_DIR = Path("/app/data/screenshots")
_SCREENSHOT_MAX_AGE_S = 7 * 86_400  # 7 days


def _prune_screenshots() -> None:
    """Delete screenshot/HTML files older than 7 days to prevent disk exhaustion."""
    if not SCREENSHOT_DIR.exists():
        return
    cutoff = time.time() - _SCREENSHOT_MAX_AGE_S
    for f in SCREENSHOT_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)

# Track all live subprocesses (ffmpeg, Xvfb) so they can be killed on SIGTERM.
_active_procs: list[subprocess.Popen] = []


def _register_proc(proc: subprocess.Popen) -> subprocess.Popen:
    _active_procs.append(proc)
    return proc


def _unregister_proc(proc: subprocess.Popen) -> None:
    try:
        _active_procs.remove(proc)
    except ValueError:
        pass


def kill_all_procs() -> None:
    """Kill every tracked subprocess. Called on SIGTERM to avoid orphaned
    ffmpeg/Xvfb processes surviving a Railway redeploy."""
    for proc in list(_active_procs):
        try:
            proc.kill()
        except Exception:
            pass
    _active_procs.clear()

# ── Stealth JS ────────────────────────────────────────────────────────────────
# Patches the most common automation signals that Google Meet and Teams check.
_STEALTH_JS = """
() => {
    // Most critical: remove the webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Add chrome object present in real Chrome builds
    if (!window.chrome) {
        window.chrome = { runtime: {} };
    }

    // Realistic plugin list
    const _plugins = [
        { name: 'Chrome PDF Plugin',   filename: 'internal-pdf-viewer',               description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer',   filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
        { name: 'Native Client',       filename: 'internal-nacl-plugin',              description: '' },
    ];
    Object.defineProperty(navigator, 'plugins',   { get: () => _plugins });
    Object.defineProperty(navigator, 'mimeTypes', { get: () => [{ type: 'application/pdf' }] });

    // Language / platform
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'platform',  { get: () => 'Linux x86_64' });

    // Permissions API — avoid 'denied' for notifications fingerprint
    const _origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origQuery(p);

    // Hide patched toString so it doesn't look like a polyfill
    const _origToString = Function.prototype.toString;
    Function.prototype.toString = function () {
        if (this === window.navigator.permissions.query)
            return 'function query() { [native code] }';
        return _origToString.call(this);
    };
}
"""

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ── PulseAudio helpers ────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _pulse_ok() -> bool:
    try:
        return _run(["pactl", "info"]).returncode == 0
    except Exception:
        return False


def _start_pulseaudio() -> bool:
    if _pulse_ok():
        return True
    try:
        rt = "/tmp/runtime-meetingbot"
        os.makedirs(rt, exist_ok=True)
        env = {**os.environ, "XDG_RUNTIME_DIR": rt}
        subprocess.run(
            ["pulseaudio", "--start", "--exit-idle-time=-1", "--log-target=stderr"],
            env=env, capture_output=True, timeout=10,
        )
        time.sleep(2)
        return _pulse_ok()
    except Exception as exc:
        logger.warning("PulseAudio start failed: %s", exc)
        return False


def _create_pulse_sink() -> Optional[str]:
    """Create a null sink and make it the system default. Returns module index."""
    try:
        r = _run([
            "pactl", "load-module", "module-null-sink",
            f"sink_name={PULSE_SINK_NAME}",
            "sink_properties=device.description=MeetingBotSink",
        ])
        if r.returncode != 0:
            logger.warning("Could not create null sink: %s", r.stderr)
            return None
        idx = r.stdout.strip()
        # Make it the default so Chromium uses it automatically
        _run(["pacmd", "set-default-sink", PULSE_SINK_NAME])
        logger.info("PulseAudio null sink ready (module %s)", idx)
        return idx
    except Exception as exc:
        logger.warning("PulseAudio sink setup failed: %s", exc)
        return None


def _unload_pulse_sink(idx: str) -> None:
    try:
        subprocess.run(["pactl", "unload-module", idx], capture_output=True, timeout=5)
    except Exception:
        pass


def _move_chrome_audio(sink: str = PULSE_SINK_NAME) -> None:
    """Belt-and-suspenders: move any Chrome sink-inputs to our virtual sink."""
    try:
        r = _run(["pactl", "list", "short", "sink-inputs"])
        for line in r.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            sink_input_id = parts[0]
            # Check whether this input belongs to Chromium
            detail = _run(["pactl", "list", "sink-inputs"])
            if "chromium" in detail.stdout.lower() or "chrome" in detail.stdout.lower():
                subprocess.run(
                    ["pactl", "move-sink-input", sink_input_id, sink],
                    capture_output=True, timeout=5,
                )
    except Exception as exc:
        logger.debug("move-sink-input: %s", exc)


# ── Xvfb & ffmpeg ─────────────────────────────────────────────────────────────

def _start_xvfb(display: str = ":99") -> Optional[subprocess.Popen]:
    try:
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1280x720x24", "-ac", "+extension", "RANDR"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)
        if proc.poll() is None:
            _register_proc(proc)
            logger.info("Xvfb started on display %s", display)
            return proc
        logger.warning("Xvfb exited immediately")
        return None
    except FileNotFoundError:
        logger.warning("Xvfb not available — falling back to headless mode")
        return None


def _start_ffmpeg(audio_path: str) -> Optional[subprocess.Popen]:
    try:
        proc = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-f", "pulse", "-i", f"{PULSE_SINK_NAME}.monitor",
                "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le",
                audio_path,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)
        if proc.poll() is None:
            _register_proc(proc)
            logger.info("ffmpeg recording → %s", audio_path)
            return proc
        logger.warning("ffmpeg exited immediately — PulseAudio sink may not be ready")
        return None
    except FileNotFoundError:
        logger.warning("ffmpeg not found — audio recording disabled")
        return None


def _stop_ffmpeg(proc: subprocess.Popen) -> None:
    _unregister_proc(proc)
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ── Screenshot ────────────────────────────────────────────────────────────────

async def _screenshot(page: Page, label: str) -> None:
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        png_path = SCREENSHOT_DIR / f"{label}_{ts}.png"
        await page.screenshot(path=str(png_path), full_page=True)
        logger.info("Screenshot → %s", png_path)
        # Also dump page HTML so selectors can be diagnosed without a display
        html_path = SCREENSHOT_DIR / f"{label}_{ts}.html"
        html = await page.content()
        html_path.write_text(html, encoding="utf-8")
        logger.info("HTML dump  → %s", html_path)
    except Exception:
        pass


# ── Playwright interaction helpers ────────────────────────────────────────────

async def _click(page: Page, selectors: list[str], timeout: int = 4000) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=timeout)
            await el.click()
            return True
        except Exception:
            pass
    return False


async def _fill(page: Page, selectors: list[str], value: str, timeout: int = 6000) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=timeout)
            await el.triple_click()
            await el.fill(value)
            return True
        except Exception:
            pass
    return False


async def _apply_stealth(page: Page) -> None:
    await page.add_init_script(_STEALTH_JS)
    try:
        from playwright_stealth import stealth_async  # type: ignore
        await stealth_async(page)
        logger.debug("playwright-stealth applied")
    except ImportError:
        logger.debug("playwright-stealth not installed — manual JS stealth only")


# ── Platform join logic ───────────────────────────────────────────────────────

class MeetingBotError(Exception):
    pass


class AdmissionTimeoutError(MeetingBotError):
    pass


async def _gmeet_dismiss_consent(page: Page) -> None:
    """Dismiss Google consent/cookie banners (optional — short timeout)."""
    await _click(page, [
        "button:has-text('Accept all')",
        "button:has-text('Reject all')",
        "button:has-text('Accept')",
        "form[action*='consent'] button",
    ], timeout=1500)


async def _gmeet_click_guest(page: Page) -> bool:
    """Click through any 'join as guest / continue without signing in' prompts.

    Returns True if a button was found and clicked.
    Uses a short timeout since these buttons are optional — they may not exist
    on every page state.
    """
    return await _click(page, [
        "button:has-text('Continue without signing in')",
        "button:has-text('Use without an account')",
        "button:has-text('Join as guest')",
        "a:has-text('Join as guest')",
        "button:has-text('Use a guest account')",
        "span:has-text('Continue without signing in')",
        "span:has-text('Use without an account')",
        # jsname-based buttons Google uses internally (try last — risky)
        "button[jsname='LgbsSe']",
    ], timeout=1500)


async def _gmeet_fill_name(page: Page, bot_name: str) -> bool:
    """Try multiple strategies to fill the guest name field."""
    # Strategy 1: standard attribute selectors (short timeout — fail fast)
    ok = await _fill(page, [
        "input[placeholder*='name' i]",
        "input[aria-label*='name' i]",
        "input[data-initial-value]",
        "input[autocomplete='name']",
        "input[jsname]",
        "input[type='text']:visible",
        "input[type='text']",
    ], bot_name, timeout=2000)
    if ok:
        return True

    # Strategy 2: JS-based — find any visible text input and fill it
    try:
        filled = await page.evaluate(f"""
            () => {{
                const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type])'));
                const visible = inputs.find(el => {{
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && !el.disabled && !el.readOnly;
                }});
                if (!visible) return false;
                visible.focus();
                visible.value = {repr(bot_name)};
                visible.dispatchEvent(new Event('input', {{bubbles: true}}));
                visible.dispatchEvent(new Event('change', {{bubbles: true}}));
                return true;
            }}
        """)
        if filled:
            logger.info("Filled name via JS fallback")
            return True
    except Exception as exc:
        logger.debug("JS name fill failed: %s", exc)

    return False


async def _gmeet_wait_ready(page: Page) -> None:
    """Wait until the Meet page has rendered enough to interact with."""
    # networkidle or 4s, whichever comes first
    try:
        await page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        pass  # best-effort — proceed even if still loading


async def _join_google_meet(page: Page, url: str, bot_name: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    await _gmeet_wait_ready(page)

    logger.info("Google Meet page loaded: %s", page.url)

    # Google may redirect to accounts.google.com — escape it
    if "accounts.google.com" in page.url:
        logger.info("Redirected to Google sign-in — clicking guest option")
        await _gmeet_dismiss_consent(page)
        clicked = await _gmeet_click_guest(page)
        if clicked:
            # Wait for redirect back to meet.google.com
            try:
                await page.wait_for_url("**/meet.google.com/**", timeout=8000)
            except Exception:
                pass
            await _gmeet_wait_ready(page)
            logger.info("After guest click, URL: %s", page.url)

    # Dismiss cookie/consent banners on the Meet page
    await _gmeet_dismiss_consent(page)

    # "Continue without signing in" — try once; if not present, proceed
    guest_clicked = await _gmeet_click_guest(page)
    if guest_clicked:
        logger.info("Clicked guest/continue button")
        await _gmeet_wait_ready(page)

    # Enter bot name — up to 3 attempts, clicking guest button between each
    logger.info("Looking for name input field…")
    ok = False
    for attempt in range(3):
        ok = await _gmeet_fill_name(page, bot_name)
        if ok:
            logger.info("Name filled on attempt %d", attempt + 1)
            break
        logger.debug("Name fill attempt %d failed — retrying guest click", attempt + 1)
        await _gmeet_click_guest(page)
        await _gmeet_wait_ready(page)

    if not ok:
        await _screenshot(page, "gmeet_no_name_field")
        raise MeetingBotError("Could not find name input on Google Meet")

    # Mute mic if currently on (aria-pressed="true" = active/on → click to mute)
    await _click(page, [
        "button[aria-label*='Turn off microphone' i]",
        "button[aria-label*='microphone' i][aria-pressed='true']",
    ], timeout=2000)
    # Turn off camera if currently on
    await _click(page, [
        "button[aria-label*='Turn off camera' i]",
        "button[aria-label*='camera' i][aria-pressed='true']",
    ], timeout=2000)
    logger.info("Google Meet: mic and camera muted before joining")

    # Ask to join / Join now
    logger.info("Clicking join button…")
    ok = await _click(page, [
        "button[jsname='Qx7uuf']",
        "button[data-idom-class*='join' i]",
        "button:has-text('Ask to join')",
        "button:has-text('Join now')",
        "button:has-text('Join')",
    ])
    if not ok:
        await _screenshot(page, "gmeet_no_join_button")
        raise MeetingBotError(
            "Could not click 'Ask to join' on Google Meet — "
            "the UI may have changed or the bot was detected. "
            "Check screenshot in /tmp/meetingbot_screenshots/"
        )
    logger.info("Google Meet join button clicked")


async def _join_zoom(page: Page, url: str, bot_name: str) -> None:
    # Convert to Zoom web-client URL
    web_url = url
    if "/j/" in url and "/wc/" not in url:
        meeting_id = url.split("/j/")[1].split("?")[0].split("/")[0]
        pwd = ("&pwd=" + url.split("pwd=")[1].split("&")[0]) if "pwd=" in url else ""
        web_url = f"https://app.zoom.us/wc/{meeting_id}/join?prefer=1{pwd}"

    await page.goto(web_url, wait_until="domcontentloaded", timeout=30_000)
    await asyncio.sleep(3)

    # "Join from browser" link — short timeout, page may skip this step
    clicked = await _click(page, [
        "a:has-text('join from your browser')",
        "a:has-text('Join from Browser')",
        "a:has-text('join from browser')",
        "#btnJoinByBrowser",
        "span:has-text('join from your browser')",
        "button:has-text('Join from Browser')",
        "button:has-text('join from your browser')",
    ], timeout=3000)
    if clicked:
        await asyncio.sleep(3)

    # Name input — use force=True to bypass Zoom web client's actionability quirks
    ok = False
    for sel in [
        "input#inputname",
        "input[name='inputname']",
        "input[placeholder*='name' i]",
        "input[aria-label*='name' i]",
        "input[type='text']",
    ]:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="attached", timeout=10000)
            await el.fill(bot_name, force=True)
            ok = True
            logger.info("Zoom: name filled with selector %s (force)", sel)
            break
        except Exception:
            pass

    # JS fallback
    if not ok:
        try:
            filled = await page.evaluate("""(name) => {
                const inputs = [...document.querySelectorAll(
                    'input[type="text"], input:not([type]), [contenteditable="true"], [role="textbox"]'
                )];
                const el = inputs.find(e => {
                    const r = e.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                if (!el) return false;
                el.focus();
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeSetter.call(el, name);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }""", bot_name)
            ok = bool(filled)
            if ok:
                logger.info("Zoom: name filled via JS fallback")
        except Exception as js_err:
            logger.warning("Zoom: JS name fallback failed: %s", js_err)

    if not ok:
        await _screenshot(page, "zoom_no_name_field")
        raise MeetingBotError("Could not find name input on Zoom")

    # Mute mic & camera on pre-join screen
    await _click(page, [
        "button.preview-audio-control",
        "button[aria-label*='mute microphone' i]",
        "button[aria-label*='mute audio' i]",
        "button[aria-label*='microphone' i]",
        ".join-audio-by-voip__mute-btn",
    ], timeout=2000)
    await _click(page, [
        "button.preview-video-control",
        "button[aria-label*='stop video' i]",
        "button[aria-label*='turn off camera' i]",
        "button[aria-label*='camera' i]",
    ], timeout=2000)
    logger.info("Zoom: mic and camera muted before joining")

    # Join button
    ok = await _click(page, [
        "button#joinBtn",
        "button[type='submit']",
        "button:has-text('Join')",
    ])
    if not ok:
        await _screenshot(page, "zoom_no_join_button")
        raise MeetingBotError("Could not find join button on Zoom")
    logger.info("Zoom join button clicked")


async def _join_teams(page: Page, url: str, bot_name: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    # New Teams React app needs extra time to render (may also redirect to teams.live.com)
    await asyncio.sleep(5)
    logger.info("Teams: landed on %s", page.url)

    # Step 1: Click through any gate button (short per-selector timeout to avoid long waits)
    # New Teams (/meet/ URLs): "Join anonymously" / "Continue without signing in"
    # Old Teams (/l/meetup-join/ URLs): "Continue on this browser"
    ok = await _click(page, [
        # New Teams — personal / teams.live.com
        "button:has-text('Join anonymously')",
        "button[data-tid='anonymous-join-button']",
        "button[data-tid='joinAsGuestButton']",
        # New Teams — business
        "button:has-text('Continue without signing in')",
        "button:has-text('Join as a guest')",
        "button:has-text('Join as guest')",
        # Old Teams
        "button:has-text('Continue on this browser')",
        "button:has-text('Join on the web instead')",
        "a:has-text('Continue on this browser')",
        "button:has-text('Join without Teams')",
    ], timeout=2000)
    if ok:
        await asyncio.sleep(3)
    else:
        # No gate button found — light experience lands directly on pre-join screen
        await _screenshot(page, "teams_no_continue_button")
        await asyncio.sleep(3)  # give the SPA time to render the pre-join UI

    # Step 1b: Dismiss "Continue without audio or video" dialog.
    # Teams shows this when the browser denies camera/mic permissions.
    # Non-fatal: the helper returns False silently if the dialog is absent.
    await _click(page, [
        "button:has-text('Continue without audio or video')",
        "button:has-text('Continue without audio')",
    ], timeout=3000)

    # Step 2: Fill name
    # Fluent UI v9 wraps <input> in a styled <span>; the inner element may not
    # pass Playwright's strict "visible" check even though it renders fine.
    # Strategy: try force-fill on known selectors first (bypasses actionability
    # checks), then fall back to normal _fill, then JS.
    ok = False
    for sel in [
        "input[data-tid='prejoin-display-name-input']",
        "input[data-tid='anonymous-join-name-input']",
        "input[placeholder='Type your name']",
        "input[placeholder*='name' i]",
        "input[type='text']",
    ]:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="attached", timeout=10000)
            await el.fill(bot_name, force=True)
            ok = True
            logger.info("Teams: name filled with selector %s (force)", sel)
            break
        except Exception:
            pass

    # If force-fill failed, try a JS fallback that fills the first
    # visible <input type="text"> or contenteditable element on the page.
    if not ok:
        try:
            filled = await page.evaluate("""(name) => {
                const inputs = [...document.querySelectorAll(
                    'input[type="text"], input:not([type]), [contenteditable="true"], [role="textbox"]'
                )];
                const el = inputs.find(e => {
                    const r = e.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                if (!el) return false;
                el.focus();
                if (el.isContentEditable) {
                    el.textContent = name;
                } else {
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    nativeSetter.call(el, name);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
                return true;
            }""", bot_name)
            ok = bool(filled)
            if ok:
                logger.info("Teams: name filled via JS fallback")
        except Exception as js_err:
            logger.warning("Teams: JS name fallback failed: %s", js_err)

    if not ok:
        await _screenshot(page, "teams_no_name_field")
        raise MeetingBotError("Could not find name input on Teams")

    # Mute mic if on (aria-pressed="true" = active/unmuted → click to mute)
    await _click(page, [
        "button[aria-label*='Mute' i][aria-pressed='true']",
        "button[aria-label*='Mute microphone' i]",
        "button[aria-label*='microphone' i][aria-pressed='true']",
        "div[role='button'][aria-label*='Mute' i]",
    ], timeout=2000)
    # Turn off camera if on
    await _click(page, [
        "button[aria-label*='Turn off camera' i]",
        "button[aria-label*='camera' i][aria-pressed='true']",
        "button[aria-label*='Stop video' i]",
        "div[role='button'][aria-label*='Turn off camera' i]",
    ], timeout=2000)
    logger.info("Teams: mic and camera muted before joining")

    # Step 3: Join
    ok = await _click(page, [
        "button[data-tid='prejoin-join-button']",
        "button[data-tid='prejoin-join-btn']",
        "button:has-text('Join now')",
        "button:has-text('Join meeting')",
        "button:has-text('Join')",
    ])
    if not ok:
        await _screenshot(page, "teams_no_join_button")
        raise MeetingBotError("Could not find join button on Teams")
    logger.info("Teams join button clicked")


# ── Admission & end detection ─────────────────────────────────────────────────

_IN_CALL_TEXTS = {
    "google_meet": ["leave call", "you're in the call", "turn on camera", "everyone in this call"],
    "zoom": ["stop video", "audio connected", "end meeting"],
    "microsoft_teams": ["you're in the meeting", "leave", "raise your hand"],
}
_WAITING_TEXTS = {
    "google_meet": ["waiting to be admitted", "waiting room", "someone will let you in"],
    "zoom": ["waiting for the host", "waiting room"],
    "microsoft_teams": ["waiting for others", "someone in the meeting should let you in", "lobby"],
}
_END_TEXTS = {
    "google_meet": ["you left the meeting", "call has ended", "meeting ended", "you've been removed"],
    "zoom": ["meeting has been ended", "meeting is ended", "this meeting has ended"],
    "microsoft_teams": ["the meeting has ended", "call ended", "you left"],
}

# Text signals that the bot is the only one in the meeting
_ALONE_TEXTS = {
    "google_meet": [
        "no one else is here",
        "you're the only one",
        "you are the only one",
        "no one else has joined",
        "add others to this call",
    ],
    "zoom": [
        "you are the only participant",
        "waiting for others to join",
    ],
    "microsoft_teams": [
        "you're the only one here",
        "you are the only one here",
        "no one else is here",
    ],
}


async def _is_bot_alone(page: Page, platform: str) -> bool:
    """Return True if the bot appears to be the only participant in the meeting."""
    try:
        body = (await page.inner_text("body")).lower()

        if any(t in body for t in _ALONE_TEXTS.get(platform, [])):
            return True

        # DOM participant tile count as secondary signal.
        # Only trust count == 1 (just the bot's own tile); count == 0 may mean
        # the DOM hasn't rendered yet, so we skip that to avoid false positives.
        if platform == "google_meet":
            count = await page.locator("[data-participant-id]").count()
            if count == 1:
                return True
        elif platform == "zoom":
            count = await page.locator(
                ".video-avatar__avatar, .participants-list-item"
            ).count()
            if count == 1:
                return True
        elif platform == "microsoft_teams":
            count = await page.locator("[data-tid='roster-participant']").count()
            if count == 1:
                return True
    except Exception:
        pass
    return False


async def _wait_for_admission(
    page: Page,
    platform: str,
    timeout_s: int,
    on_admitted: Optional[Callable[[], Awaitable[None]]],
) -> bool:
    in_call  = _IN_CALL_TEXTS.get(platform, [])
    waiting  = _WAITING_TEXTS.get(platform, [])
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        try:
            body = (await page.inner_text("body")).lower()
            in_lobby = any(t in body for t in waiting)
            admitted = any(t in body for t in in_call)

            if admitted and not in_lobby:
                logger.info("Bot admitted to %s meeting", platform)
                if on_admitted:
                    await on_admitted()
                return True

            # DOM element fallbacks (more reliable than text in some platforms)
            if platform == "google_meet":
                if await page.locator("button[aria-label*='Leave call' i]").count() > 0:
                    if on_admitted:
                        await on_admitted()
                    return True
            elif platform == "zoom":
                if await page.locator(".meeting-client-inner, #wc-footer").count() > 0:
                    if on_admitted:
                        await on_admitted()
                    return True
            elif platform == "microsoft_teams":
                if await page.locator("button[data-tid='hangup-button']").count() > 0:
                    if on_admitted:
                        await on_admitted()
                    return True

        except Exception:
            pass

        await asyncio.sleep(3)

    return False


async def _collect_participants(page: Page, platform: str) -> set[str]:
    """Best-effort scrape of visible participant names from the meeting UI."""
    names: set[str] = set()
    try:
        if platform == "google_meet":
            # Video tile name labels (multiple possible class names across versions)
            for sel in [
                "[data-participant-id] [jsname='EkIl7d']",
                "[data-participant-id] .zWGUib",
                "[data-participant-id] [aria-label]",
            ]:
                els = page.locator(sel)
                count = await els.count()
                for i in range(min(count, 20)):
                    try:
                        txt = (await els.nth(i).inner_text()).strip()
                        if txt and len(txt) < 60 and not txt.lower().startswith("you"):
                            names.add(txt)
                    except Exception:
                        pass
            # Also try JS approach for tile overlays
            try:
                found = await page.evaluate("""
                    () => {
                        const names = [];
                        document.querySelectorAll('[data-participant-id]').forEach(tile => {
                            const nameEl = tile.querySelector('[jsname], .zWGUib, [data-self-name]');
                            if (nameEl) {
                                const txt = nameEl.innerText || nameEl.textContent || nameEl.getAttribute('aria-label') || '';
                                if (txt.trim()) names.push(txt.trim());
                            }
                        });
                        return names;
                    }
                """)
                for name in (found or []):
                    if name and len(name) < 60:
                        names.add(name)
            except Exception:
                pass

        elif platform == "zoom":
            for sel in [
                ".participants__participant--name",
                ".video-avatar__name",
                ".display-name",
                "[class*='participant-name']",
            ]:
                els = page.locator(sel)
                count = await els.count()
                for i in range(min(count, 20)):
                    try:
                        txt = (await els.nth(i).inner_text()).strip()
                        if txt and len(txt) < 60:
                            names.add(txt)
                    except Exception:
                        pass

        elif platform == "microsoft_teams":
            for sel in [
                "[data-tid='roster-participant'] [data-tid='roster-participant-displayname']",
                "[data-tid='roster-participant'] span",
                "[data-cid='calling-roster-participant-display-name']",
                ".ts-calling-roster-participant-name",
            ]:
                els = page.locator(sel)
                count = await els.count()
                for i in range(min(count, 20)):
                    try:
                        txt = (await els.nth(i).inner_text()).strip()
                        if txt and len(txt) < 60:
                            names.add(txt)
                    except Exception:
                        pass

    except Exception as exc:
        logger.debug("_collect_participants error: %s", exc)

    # Filter out obvious non-names
    names.discard("")
    return {n for n in names if len(n) >= 2}


async def _wait_for_meeting_end(
    page: Page,
    platform: str,
    max_s: int,
    alone_timeout_s: int = 300,
    participants: set | None = None,
) -> str:
    """
    Wait until the meeting ends, the max duration is reached, or the bot has
    been the only participant for alone_timeout_s consecutive seconds.

    Returns one of: "ended" | "max_duration" | "alone_timeout"
    """
    end_texts = _END_TEXTS.get(platform, [])
    deadline  = time.monotonic() + max_s

    # After a few seconds in the meeting, move Chrome audio to our sink
    # (belt-and-suspenders in case the default-sink setting wasn't picked up)
    asyncio.get_event_loop().call_later(5, _move_chrome_audio)

    alone_since: Optional[float] = None
    _last_participant_scrape = 0.0

    while time.monotonic() < deadline:
        try:
            body = (await page.inner_text("body")).lower()
            if any(t in body for t in end_texts):
                logger.info("Meeting end detected (%s)", platform)
                return "ended"
        except Exception:
            logger.info("Page inaccessible — meeting likely ended")
            return "ended"

        # ── Participant name scraping (every 30s) ─────────────────────────
        now = time.monotonic()
        if participants is not None and now - _last_participant_scrape >= 30:
            _last_participant_scrape = now
            found = await _collect_participants(page, platform)
            if found:
                participants.update(found)
                logger.debug("Participants so far: %s", participants)

        # ── Alone / empty-room detection ──────────────────────────────────
        alone = await _is_bot_alone(page, platform)
        if alone:
            if alone_since is None:
                alone_since = time.monotonic()
                logger.info(
                    "Bot is alone in the meeting — will leave in %ds if no one joins",
                    alone_timeout_s,
                )
            elif time.monotonic() - alone_since >= alone_timeout_s:
                logger.info(
                    "Bot has been alone for %ds — leaving meeting", alone_timeout_s
                )
                return "alone_timeout"
        else:
            if alone_since is not None:
                logger.info("Other participants detected — resetting alone timer")
            alone_since = None

        await asyncio.sleep(10)

    logger.info("Max meeting duration (%ds) reached", max_s)
    return "max_duration"


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_browser_bot(
    meeting_url: str,
    platform: str,
    bot_name: str,
    audio_path: str,
    admission_timeout: int = 300,
    max_duration: int = 7200,
    alone_timeout: int = 300,
    on_admitted: Optional[Callable[[], Awaitable[None]]] = None,
) -> dict:
    """
    Join a meeting as a named guest, record audio, wait for it to end.

    Args:
        meeting_url:       Full meeting URL.
        platform:          "google_meet" | "zoom" | "microsoft_teams".
        bot_name:          Display name shown in the meeting.
        audio_path:        Path where the WAV recording will be written.
        admission_timeout: Seconds to wait for the host to admit the bot.
        max_duration:      Max meeting length in seconds before bot leaves.
        alone_timeout:     Seconds the bot may be the only participant before
                           it leaves automatically (covers both the empty-room
                           case and the everyone-left case).
        on_admitted:       Optional async callback fired when the bot is let in.

    Returns:
        {"success", "audio_path", "error", "admitted", "duration_seconds", "exit_reason", "participants"}
    """
    pulse_idx: Optional[str] = None
    ffmpeg_proc: Optional[subprocess.Popen] = None
    xvfb_proc:   Optional[subprocess.Popen] = None
    t0 = time.monotonic()

    # ── Infrastructure ──────────────────────────────────────────────────────
    pulse_ok = _start_pulseaudio()
    if pulse_ok:
        pulse_idx = _create_pulse_sink()
        if pulse_idx:
            ffmpeg_proc = _start_ffmpeg(audio_path)

    xvfb_proc = _start_xvfb(":99")
    headless  = xvfb_proc is None   # fall back to headless if no Xvfb

    env: dict = {}
    if xvfb_proc:
        env["DISPLAY"] = ":99"
    if pulse_ok:
        env["PULSE_LATENCY_MSEC"] = "30"
        # Explicitly point Chrome at the PulseAudio server so it does not
        # fall back to a dummy audio output or PipeWire, both of which would
        # mean no audio reaches our null-sink monitor (and thus no recording).
        # start.sh sets XDG_RUNTIME_DIR=/tmp/runtime-meetingbot, so the socket
        # lives at /tmp/runtime-meetingbot/pulse/native.
        rt = os.environ.get("XDG_RUNTIME_DIR", "/tmp/runtime-meetingbot")
        default_pulse = f"unix:{rt}/pulse/native"
        env["PULSE_SERVER"] = os.environ.get("PULSE_SERVER", default_pulse)
        env["PULSE_SINK"] = PULSE_SINK_NAME

    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        # Hide automation signals
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
        "--exclude-switches=enable-automation",
        # Media — auto-grant mic/camera permissions without UI prompts.
        # NOTE: do NOT add --use-fake-device-for-media-stream here — that flag
        # replaces Chrome's audio OUTPUT device with an internal fake sink,
        # bypassing PulseAudio entirely so ffmpeg records silence instead of
        # real meeting audio.  PulseAudio's null-sink is already set as the
        # default device; Chrome will use it for both output and mic input.
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        # Performance
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--window-size=1280,720",
    ]

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=headless,
            args=launch_args,
            env=env or None,
        )
        ctx: BrowserContext = await browser.new_context(
            user_agent=_USER_AGENT,
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = await ctx.new_page()
        await _apply_stealth(page)

        try:
            logger.info(
                "Browser bot starting: %s  platform=%s  name='%s'",
                meeting_url, platform, bot_name,
            )

            if platform == "google_meet":
                await _join_google_meet(page, meeting_url, bot_name)
            elif platform == "zoom":
                await _join_zoom(page, meeting_url, bot_name)
            elif platform == "microsoft_teams":
                await _join_teams(page, meeting_url, bot_name)
            else:
                raise MeetingBotError(f"Unsupported platform: {platform}")

            await asyncio.sleep(2)
            await _screenshot(page, f"{platform}_after_join")

            logger.info("Waiting for admission (timeout=%ds)…", admission_timeout)
            admitted = await _wait_for_admission(
                page, platform, admission_timeout, on_admitted
            )

            if not admitted:
                await _screenshot(page, f"{platform}_not_admitted")
                raise AdmissionTimeoutError(
                    f"Bot was not admitted within {admission_timeout}s. "
                    "The host must click 'Admit' in the waiting room."
                )

            await _screenshot(page, f"{platform}_in_meeting")
            logger.info("Bot is in the meeting — monitoring for end…")
            _participants: set[str] = set()
            exit_reason = await _wait_for_meeting_end(
                page, platform, max_duration, alone_timeout, _participants
            )
            # One final scrape at end of meeting
            try:
                _participants.update(await _collect_participants(page, platform))
            except Exception:
                pass

            duration   = time.monotonic() - t0
            has_audio  = (
                ffmpeg_proc is not None
                and os.path.exists(audio_path)
                and os.path.getsize(audio_path) > 8192
            )
            return {
                "success": True,
                "audio_path": audio_path if has_audio else None,
                "error": None,
                "admitted": True,
                "duration_seconds": duration,
                "exit_reason": exit_reason,
                "participants": sorted(_participants),
            }

        except Exception as exc:
            logger.error("Browser bot error: %s", exc)
            await _screenshot(page, f"{platform}_error")
            return {
                "success": False,
                "audio_path": None,
                "error": str(exc),
                "admitted": False,
                "duration_seconds": time.monotonic() - t0,
                "exit_reason": "error",
                "participants": [],
            }

        finally:
            await browser.close()
            if ffmpeg_proc:
                _stop_ffmpeg(ffmpeg_proc)
            if pulse_idx:
                _unload_pulse_sink(pulse_idx)
            if xvfb_proc:
                _unregister_proc(xvfb_proc)
                try:
                    xvfb_proc.terminate()
                    xvfb_proc.wait(timeout=5)
                except Exception:
                    pass
            try:
                _prune_screenshots()
            except Exception:
                pass
