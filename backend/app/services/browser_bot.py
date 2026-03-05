"""Real browser-based meeting bot using Playwright.

Joins Google Meet, Zoom, and Microsoft Teams as a named guest,
captures meeting audio via PulseAudio virtual sink + ffmpeg,
and returns the path to the recorded audio file.
"""

import asyncio
import logging
import os
import subprocess
import time
from typing import Optional

from playwright.async_api import Browser, Page, async_playwright

logger = logging.getLogger(__name__)

PULSE_SINK_NAME = "meetingbot_sink"


# ── Audio / display infrastructure ──────────────────────────────────────────


def _pulse_running() -> bool:
    try:
        result = subprocess.run(
            ["pactl", "info"], capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _start_pulseaudio() -> bool:
    """Start a PulseAudio daemon if one isn't running. Returns True on success."""
    if _pulse_running():
        return True
    try:
        subprocess.run(
            ["pulseaudio", "--start", "--exit-idle-time=-1"],
            capture_output=True,
            timeout=10,
        )
        time.sleep(1)
        return _pulse_running()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _create_pulse_sink() -> Optional[str]:
    """Create a PulseAudio null sink. Returns module index string or None."""
    try:
        result = subprocess.run(
            [
                "pactl",
                "load-module",
                "module-null-sink",
                f"sink_name={PULSE_SINK_NAME}",
                "sink_properties=device.description=MeetingBotSink",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            idx = result.stdout.strip()
            logger.info("PulseAudio null sink created (module %s)", idx)
            return idx
        logger.warning("Failed to create PulseAudio sink: %s", result.stderr)
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("PulseAudio unavailable: %s", exc)
        return None


def _unload_pulse_sink(module_idx: str) -> None:
    try:
        subprocess.run(
            ["pactl", "unload-module", module_idx], timeout=5, capture_output=True
        )
    except Exception as exc:
        logger.warning("Failed to unload PulseAudio module: %s", exc)


def _start_xvfb(display: str = ":99") -> Optional[subprocess.Popen]:
    """Start a virtual framebuffer. Returns process or None."""
    try:
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1280x720x24", "-ac"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        if proc.poll() is None:
            logger.info("Xvfb started on display %s", display)
            return proc
        logger.warning("Xvfb exited immediately")
        return None
    except FileNotFoundError:
        logger.warning("Xvfb not found — will use headless mode")
        return None


def _start_ffmpeg_recording(audio_path: str) -> Optional[subprocess.Popen]:
    """Record from the virtual sink monitor into a WAV file."""
    try:
        proc = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-f",
                "pulse",
                "-i",
                f"{PULSE_SINK_NAME}.monitor",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-acodec",
                "pcm_s16le",
                audio_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("ffmpeg recording started (PID %s) → %s", proc.pid, audio_path)
        return proc
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


# ── Platform-specific join logic ─────────────────────────────────────────────


class MeetingBotError(Exception):
    pass


class AdmissionTimeoutError(MeetingBotError):
    pass


async def _click_first_visible(page: Page, selectors: list[str], timeout: int = 3000) -> bool:
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=timeout):
                await el.click()
                return True
        except Exception:
            pass
    return False


async def _fill_first_visible(page: Page, selectors: list[str], value: str, timeout: int = 5000) -> bool:
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=timeout):
                await el.clear()
                await el.fill(value)
                return True
        except Exception:
            pass
    return False


async def _join_google_meet(page: Page, url: str, bot_name: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    # Dismiss cookie consent
    await _click_first_visible(page, [
        "button[aria-label*='Accept all' i]",
        "button[aria-label*='Accept' i]",
        "form[action*='consent'] button",
    ])
    await asyncio.sleep(1)

    # "Continue without signing in"
    await _click_first_visible(page, [
        "button:has-text('Continue without signing in')",
        "button:has-text('Use without an account')",
        "a:has-text('Join as guest')",
    ])
    await asyncio.sleep(2)

    # Enter name
    await _fill_first_visible(page, [
        "input[placeholder*='name' i]",
        "input[aria-label*='name' i]",
        "input[data-initial-value]",
        "input[type='text']",
    ], bot_name)
    await asyncio.sleep(1)

    # Ensure mic is muted
    await _click_first_visible(page, [
        "button[aria-label*='Turn off microphone' i]",
        "button[data-is-muted='false'][aria-label*='microphone' i]",
    ])

    # Click join
    joined = await _click_first_visible(page, [
        "button[jsname='Qx7uuf']",
        "button[aria-label*='Ask to join' i]",
        "button[aria-label*='Join now' i]",
        "button:has-text('Ask to join')",
        "button:has-text('Join now')",
        "button:has-text('Join')",
    ])
    if not joined:
        raise MeetingBotError("Could not find join button for Google Meet")
    logger.info("Clicked join for Google Meet")


async def _join_zoom(page: Page, url: str, bot_name: str) -> None:
    # Convert to Zoom web client URL
    web_url = url
    if "/j/" in url and "/wc/" not in url:
        meeting_id = url.split("/j/")[1].split("?")[0].split("/")[0]
        pwd = ""
        if "pwd=" in url:
            pwd = "&pwd=" + url.split("pwd=")[1].split("&")[0]
        web_url = f"https://app.zoom.us/wc/{meeting_id}/join?prefer=1{pwd}"

    await page.goto(web_url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    # Click "join from browser"
    await _click_first_visible(page, [
        "a:has-text('join from your browser')",
        "a:has-text('Join from Browser')",
        "span:has-text('join from your browser')",
    ])
    await asyncio.sleep(3)

    # Enter name
    await _fill_first_visible(page, [
        "input#inputname",
        "input[placeholder*='name' i]",
        "input[aria-label*='name' i]",
        "input[name='inputname']",
    ], bot_name)

    # Join
    joined = await _click_first_visible(page, [
        "button#joinBtn",
        "button[type='submit']",
        "button:has-text('Join')",
    ])
    if not joined:
        raise MeetingBotError("Could not find join button for Zoom")
    logger.info("Clicked join for Zoom")


async def _join_teams(page: Page, url: str, bot_name: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    # "Continue on this browser"
    await _click_first_visible(page, [
        "button:has-text('Continue on this browser')",
        "a:has-text('Join on the web instead')",
        "button:has-text('Join without Teams')",
        "button:has-text('Join on this browser')",
    ])
    await asyncio.sleep(3)

    # Enter name
    await _fill_first_visible(page, [
        "input[data-tid='prejoin-display-name-input']",
        "input[placeholder*='name' i]",
        "input[aria-label*='name' i]",
    ], bot_name)

    # Disable mic & camera
    for label in ["Mute", "Turn off camera"]:
        await _click_first_visible(page, [
            f"button[aria-label*='{label}' i]",
            f"div[aria-label*='{label}' i]",
        ], timeout=2000)
        await asyncio.sleep(0.5)

    # Join
    joined = await _click_first_visible(page, [
        "button[data-tid='prejoin-join-button']",
        "button:has-text('Join now')",
        "button:has-text('Join meeting')",
        "button:has-text('Join')",
    ])
    if not joined:
        raise MeetingBotError("Could not find join button for Teams")
    logger.info("Clicked join for Teams")


# ── Admission & end detection ─────────────────────────────────────────────────


_ADMITTED_SELECTORS = {
    "google_meet": [
        "button[aria-label*='Leave call' i]",
        "button[aria-label*='Leave meeting' i]",
        "[data-participant-id]",
        "[data-self-name]",
    ],
    "zoom": [
        ".meeting-client-inner",
        "#wc-footer",
        ".participants-header",
        ".video-avatar__avatar",
    ],
    "microsoft_teams": [
        "button[data-tid='hangup-button']",
        "[data-tid='calling-roster']",
        "[data-tid='meeting-roster']",
    ],
}

_WAITING_ROOM_PHRASES = {
    "google_meet": ["waiting to be admitted", "waiting for others", "waiting room"],
    "zoom": ["waiting for the host", "waiting room", "meeting has not started"],
    "microsoft_teams": ["waiting for others", "someone in the meeting should let you in"],
}

_END_PHRASES = {
    "google_meet": ["you left the meeting", "call has ended", "meeting ended", "you've been removed"],
    "zoom": ["meeting has been ended by the host", "meeting is ended", "this meeting has ended"],
    "microsoft_teams": ["the meeting has ended", "call ended", "you left the meeting"],
}


async def _wait_for_admission(page: Page, platform: str, timeout_seconds: int) -> bool:
    selectors = _ADMITTED_SELECTORS.get(platform, [])
    waiting_phrases = _WAITING_ROOM_PHRASES.get(platform, [])
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                if await page.locator(sel).first.is_visible(timeout=1000):
                    body = (await page.inner_text("body")).lower()
                    if not any(p in body for p in waiting_phrases):
                        logger.info("Bot admitted (matched selector: %s)", sel)
                        return True
            except Exception:
                pass

        try:
            body = (await page.inner_text("body")).lower()
            if platform == "google_meet" and "leave call" in body:
                return True
            if platform == "zoom" and "connected" in body and "waiting" not in body:
                return True
            if platform == "microsoft_teams" and "you're in the meeting" in body:
                return True
        except Exception:
            pass

        await asyncio.sleep(3)

    return False


async def _wait_for_meeting_end(page: Page, platform: str, max_seconds: int) -> None:
    end_phrases = _END_PHRASES.get(platform, [])
    deadline = time.monotonic() + max_seconds

    while time.monotonic() < deadline:
        try:
            body = (await page.inner_text("body")).lower()
            if any(p in body for p in end_phrases):
                logger.info("Meeting end detected (%s)", platform)
                return
        except Exception:
            logger.info("Page inaccessible — meeting likely ended")
            return
        await asyncio.sleep(10)

    logger.info("Max meeting duration reached (%ds)", max_seconds)


# ── Main entry point ──────────────────────────────────────────────────────────


async def run_browser_bot(
    meeting_url: str,
    platform: str,
    bot_name: str,
    audio_path: str,
    admission_timeout: int = 300,
    max_duration: int = 7200,
) -> dict:
    """
    Launch a browser bot that joins the meeting, records audio, and waits for it to end.

    Returns:
        {
            "success": bool,
            "audio_path": str | None,   # path to WAV file, None if capture failed
            "error": str | None,
            "admitted": bool,
            "duration_seconds": float,
        }
    """
    pulse_module_idx: Optional[str] = None
    ffmpeg_proc: Optional[subprocess.Popen] = None
    xvfb_proc: Optional[subprocess.Popen] = None
    start = time.monotonic()

    # Start PulseAudio + virtual display for audio capture
    pulse_available = _start_pulseaudio()
    if pulse_available:
        pulse_module_idx = _create_pulse_sink()
        if pulse_module_idx:
            ffmpeg_proc = _start_ffmpeg_recording(audio_path)

    xvfb_proc = _start_xvfb(display=":99")
    headless = xvfb_proc is None  # fall back to headless if Xvfb unavailable

    browser_env = {}
    if pulse_module_idx:
        browser_env["PULSE_SINK"] = PULSE_SINK_NAME
    if xvfb_proc:
        browser_env["DISPLAY"] = ":99"

    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--use-fake-ui-for-media-stream",       # auto-grant mic/camera permissions
        "--use-file-for-fake-audio-capture=/dev/zero",  # send silence as mic input
        "--autoplay-policy=no-user-gesture-required",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=headless,
            args=launch_args,
            env=browser_env if browser_env else None,
        )
        context = await browser.new_context(
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720},
        )
        page = await context.new_page()

        try:
            logger.info(
                "Browser bot joining %s (%s) as '%s'", meeting_url, platform, bot_name
            )

            if platform == "google_meet":
                await _join_google_meet(page, meeting_url, bot_name)
            elif platform == "zoom":
                await _join_zoom(page, meeting_url, bot_name)
            elif platform == "microsoft_teams":
                await _join_teams(page, meeting_url, bot_name)
            else:
                raise MeetingBotError(f"Unsupported platform: {platform}")

            logger.info("Waiting to be admitted (timeout=%ds)…", admission_timeout)
            admitted = await _wait_for_admission(page, platform, admission_timeout)

            if not admitted:
                raise AdmissionTimeoutError(
                    f"Bot was not admitted within {admission_timeout}s. "
                    "The host needs to let the bot in from the waiting room."
                )

            logger.info("Bot is in the meeting — monitoring for end…")
            await _wait_for_meeting_end(page, platform, max_duration)

            duration = time.monotonic() - start
            has_audio = ffmpeg_proc is not None and os.path.exists(audio_path)
            return {
                "success": True,
                "audio_path": audio_path if has_audio else None,
                "error": None,
                "admitted": True,
                "duration_seconds": duration,
            }

        except Exception as exc:
            logger.error("Browser bot error: %s", exc)
            return {
                "success": False,
                "audio_path": None,
                "error": str(exc),
                "admitted": isinstance(exc, AdmissionTimeoutError) is False,
                "duration_seconds": time.monotonic() - start,
            }

        finally:
            await browser.close()
            if ffmpeg_proc:
                _stop_ffmpeg(ffmpeg_proc)
            if pulse_module_idx:
                _unload_pulse_sink(pulse_module_idx)
            if xvfb_proc:
                try:
                    xvfb_proc.terminate()
                    xvfb_proc.wait(timeout=5)
                except Exception:
                    pass
