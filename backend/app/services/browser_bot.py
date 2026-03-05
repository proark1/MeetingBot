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
SCREENSHOT_DIR = Path("/tmp/meetingbot_screenshots")

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
            logger.info("ffmpeg recording → %s", audio_path)
            return proc
        logger.warning("ffmpeg exited immediately — PulseAudio sink may not be ready")
        return None
    except FileNotFoundError:
        logger.warning("ffmpeg not found — audio recording disabled")
        return None


def _stop_ffmpeg(proc: subprocess.Popen) -> None:
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
        path = SCREENSHOT_DIR / f"{label}_{int(time.time())}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Screenshot → %s", path)
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


async def _join_google_meet(page: Page, url: str, bot_name: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    await asyncio.sleep(3)

    logger.info("Google Meet page loaded: %s", page.url)

    # Google may redirect to accounts.google.com — escape it
    if "accounts.google.com" in page.url:
        await _click(page, [
            "text=Use without an account",
            "text=Continue without signing in",
            "text=Join as guest",
            "[data-action='cancel']",
        ], timeout=6000)
        await asyncio.sleep(2)

    # Dismiss cookie/consent banners
    await _click(page, [
        "button:has-text('Accept all')",
        "button:has-text('Reject all')",
        "button:has-text('Accept')",
        "form[action*='consent'] button",
    ], timeout=3000)
    await asyncio.sleep(1)

    # "Continue without signing in" on the Meet page itself
    await _click(page, [
        "button:has-text('Continue without signing in')",
        "button:has-text('Use without an account')",
        "a:has-text('Join as guest')",
    ], timeout=4000)
    await asyncio.sleep(2)

    # Enter bot name
    ok = await _fill(page, [
        "input[placeholder*='name' i]",
        "input[aria-label*='name' i]",
        "input[data-initial-value]",
        "input[autocomplete='name']",
        "input[type='text']",
    ], bot_name)
    if not ok:
        await _screenshot(page, "gmeet_no_name_field")
        raise MeetingBotError("Could not find name input on Google Meet")
    await asyncio.sleep(1)

    # Mute mic (sends silence — no echo in the call)
    await _click(page, [
        "button[aria-label*='Turn off microphone' i]",
        "button[aria-label*='microphone' i][aria-pressed='false']",
    ], timeout=2000)
    # Camera off
    await _click(page, [
        "button[aria-label*='Turn off camera' i]",
        "button[aria-label*='camera' i][aria-pressed='false']",
    ], timeout=2000)
    await asyncio.sleep(0.5)

    # Ask to join / Join now
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

    # "Join from browser" link
    clicked = await _click(page, [
        "a:has-text('join from your browser')",
        "a:has-text('Join from Browser')",
        "#btnJoinByBrowser",
        "span:has-text('join from your browser')",
    ], timeout=6000)
    if clicked:
        await asyncio.sleep(3)

    # Name input
    ok = await _fill(page, [
        "input#inputname",
        "input[name='inputname']",
        "input[placeholder*='name' i]",
        "input[aria-label*='name' i]",
    ], bot_name)
    if not ok:
        await _screenshot(page, "zoom_no_name_field")
        raise MeetingBotError("Could not find name input on Zoom")

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
    await asyncio.sleep(3)

    # "Continue on this browser"
    ok = await _click(page, [
        "button:has-text('Continue on this browser')",
        "button:has-text('Join on the web instead')",
        "a:has-text('Continue on this browser')",
        "button:has-text('Join without Teams')",
    ], timeout=8000)
    if ok:
        await asyncio.sleep(3)
    else:
        await _screenshot(page, "teams_no_continue_button")

    # Name input
    ok = await _fill(page, [
        "input[data-tid='prejoin-display-name-input']",
        "input[placeholder*='name' i]",
        "input[aria-label*='name' i]",
        "input[name='displayName']",
    ], bot_name)
    if not ok:
        await _screenshot(page, "teams_no_name_field")
        raise MeetingBotError("Could not find name input on Teams")

    # Mute mic & camera
    for label in ["Mute", "Turn off camera"]:
        await _click(page, [
            f"button[aria-label*='{label}' i]",
            f"div[role='button'][aria-label*='{label}' i]",
        ], timeout=2000)
        await asyncio.sleep(0.3)

    # Join
    ok = await _click(page, [
        "button[data-tid='prejoin-join-button']",
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


async def _wait_for_meeting_end(page: Page, platform: str, max_s: int) -> None:
    end_texts = _END_TEXTS.get(platform, [])
    deadline  = time.monotonic() + max_s

    # After a few seconds in the meeting, move Chrome audio to our sink
    # (belt-and-suspenders in case the default-sink setting wasn't picked up)
    asyncio.get_event_loop().call_later(5, _move_chrome_audio)

    while time.monotonic() < deadline:
        try:
            body = (await page.inner_text("body")).lower()
            if any(t in body for t in end_texts):
                logger.info("Meeting end detected (%s)", platform)
                return
        except Exception:
            logger.info("Page inaccessible — meeting likely ended")
            return
        await asyncio.sleep(10)

    logger.info("Max meeting duration (%ds) reached", max_s)


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_browser_bot(
    meeting_url: str,
    platform: str,
    bot_name: str,
    audio_path: str,
    admission_timeout: int = 300,
    max_duration: int = 7200,
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
        on_admitted:       Optional async callback fired when the bot is let in.

    Returns:
        {"success", "audio_path", "error", "admitted", "duration_seconds"}
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
        # Media
        "--use-fake-ui-for-media-stream",           # auto-grant mic/camera
        "--use-file-for-fake-audio-capture=/dev/zero",  # send silence as mic
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
            await _wait_for_meeting_end(page, platform, max_duration)

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
            }

        finally:
            await browser.close()
            if ffmpeg_proc:
                _stop_ffmpeg(ffmpeg_proc)
            if pulse_idx:
                _unload_pulse_sink(pulse_idx)
            if xvfb_proc:
                try:
                    xvfb_proc.terminate()
                    xvfb_proc.wait(timeout=5)
                except Exception:
                    pass
