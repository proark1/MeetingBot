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
import base64
import contextlib
import functools
import itertools
import logging
import os
import struct
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logger = logging.getLogger(__name__)

PULSE_SINK_NAME = "meetingbot_sink"
PULSE_MIC_NAME  = "meetingbot_mic"   # TTS audio plays here; Chrome captures it as mic input

# Each concurrent bot gets a unique virtual X display so they don't collide.
# Counter starts at 99 and increments; the OS will reject duplicates gracefully.
_xvfb_display_counter: itertools.count = itertools.count(99)

_WAV_HEADER_SIZE = 44          # standard WAV header length (bytes)
_PCM_BYTES_PER_S = 32_000      # 16 kHz, mono, s16le = 32 000 bytes/s
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
        # Poll up to 3 s in 200 ms steps — return as soon as PulseAudio is ready
        for _ in range(15):
            time.sleep(0.2)
            if _pulse_ok():
                return True
        return _pulse_ok()
    except Exception as exc:
        logger.warning("PulseAudio start failed: %s", exc)
        return False


def _create_pulse_sink(name: str = PULSE_SINK_NAME) -> Optional[str]:
    """Create a named null sink and make it the default output. Returns module index."""
    try:
        r = _run([
            "pactl", "load-module", "module-null-sink",
            f"sink_name={name}",
            f"sink_properties=device.description={name}",
        ])
        if r.returncode != 0:
            logger.warning("Could not create null sink %s: %s", name, r.stderr)
            return None
        idx = r.stdout.strip()
        _run(["pactl", "set-default-sink", name])
        # Ensure the sink is unmuted and at full volume — a zero-volume sink
        # would cause ffmpeg to record silence even when Chrome is routing audio
        # to it correctly.
        _run(["pactl", "set-sink-volume", name, "100%"])
        _run(["pactl", "set-sink-mute",   name, "0"])
        logger.info("PulseAudio null sink ready: %s (module %s)", name, idx)
        return idx
    except Exception as exc:
        logger.warning("PulseAudio sink setup failed: %s", exc)
        return None


def _create_pulse_mic(name: str = PULSE_MIC_NAME) -> tuple[Optional[str], Optional[str], str]:
    """Create a null sink + virtual source for TTS mic injection.

    Returns (sink_module_idx, virt_module_idx_or_None, pulse_source_name).

    The null sink receives TTS audio played by ffplay/ffmpeg.  Chrome records
    from the virtual source (module-virtual-source) which is backed by the
    sink's monitor.  Chrome's WebRTC getUserMedia enumerates virtual sources
    as real microphones but filters out raw .monitor sources, so the virtual
    source is required for Google Meet's microphone check to pass.
    """
    try:
        # Step 1: null sink where TTS audio is played into
        r = _run([
            "pactl", "load-module", "module-null-sink",
            f"sink_name={name}",
            f"sink_properties=device.description={name}",
        ])
        if r.returncode != 0:
            logger.warning("Could not create TTS mic sink %s: %s", name, r.stderr)
            return None, None, f"{name}.monitor"
        sink_idx = r.stdout.strip()

        # Step 2: virtual source backed by the monitor — Chrome enumerates this
        # as a real microphone device instead of filtering it out as a monitor.
        virt_name = f"{name}_virt"
        rv = _run([
            "pactl", "load-module", "module-virtual-source",
            f"source_name={virt_name}",
            f"master={name}.monitor",
            f"source_properties=device.description={virt_name}",
        ])
        if rv.returncode == 0:
            virt_idx = rv.stdout.strip()
            _run(["pactl", "set-default-source", virt_name])
            logger.info(
                "PulseAudio TTS mic ready: sink=%s (mod %s), virt-source=%s (mod %s)",
                name, sink_idx, virt_name, virt_idx,
            )
            return sink_idx, virt_idx, virt_name
        else:
            # module-virtual-source not available — fall back to monitor directly
            logger.warning(
                "module-virtual-source unavailable (%s) — Chrome may show 'no mic'; "
                "falling back to monitor source",
                rv.stderr.decode(errors="replace").strip() if isinstance(rv.stderr, bytes) else rv.stderr.strip(),
            )
            _run(["pactl", "set-default-source", f"{name}.monitor"])
            logger.info("PulseAudio TTS mic sink ready: %s (module %s)", name, sink_idx)
            return sink_idx, None, f"{name}.monitor"
    except Exception as exc:
        logger.warning("PulseAudio mic sink setup failed: %s", exc)
        return None, None, f"{name}.monitor"


def _unload_pulse_sink(idx: str) -> None:
    try:
        subprocess.run(["pactl", "unload-module", idx], capture_output=True, timeout=5)
    except Exception:
        pass


def _move_chrome_audio(sink: str = PULSE_SINK_NAME) -> None:
    """Move all browser/meeting audio sink-inputs to our virtual recording sink.

    Chrome's WebRTC audio renderer may appear under a different application name
    than "chrome"/"chromium" (e.g. the renderer process uses a distinct PA
    client).  To avoid missing the WebRTC audio stream we route ALL sink-inputs
    except known non-meeting processes (TTS ffmpeg, arecord, etc.).

    In a containerised bot environment the only sink-inputs are:
      • Chrome browser + renderer audio  → move to recording sink
      • TTS playback ffmpeg              → stays on the mic sink (skip)
    """
    # Processes whose sink-inputs we must NOT redirect to the recording sink.
    # ffmpeg is the TTS playback process; it outputs to the mic null-sink and
    # must not be rerouted or TTS will feed back into the recording.
    SKIP_APPS = {"ffmpeg", "ffmpeg-static", "arecord", "parecord", "parec"}
    try:
        detail = _run(["pactl", "list", "sink-inputs"])
        if detail.returncode != 0:
            return

        # Parse into per-entry dicts {id, app, current_sink}
        entries: list[dict] = []
        current: dict | None = None
        for raw_line in detail.stdout.splitlines():
            line = raw_line.strip()
            if line.startswith("Sink Input #"):
                if current is not None:
                    entries.append(current)
                current = {"id": line.split("#", 1)[1].strip(), "app": "", "sink": ""}
            elif current is not None:
                if "application.name" in line or "application.process.binary" in line:
                    val = line.split("=", 1)[-1].strip().strip('"').lower()
                    if val:
                        current["app"] = val
                elif line.startswith("Sink:"):
                    # e.g. "Sink: 3"  or  "Sink: mbot_1ea088adc2"
                    current["sink"] = line.split(":", 1)[-1].strip()
        if current is not None:
            entries.append(current)

        moved = 0
        for entry in entries:
            app = entry["app"]
            if app in SKIP_APPS:
                logger.debug("Skipping sink-input %s (app=%s)", entry["id"], app)
                continue
            r = subprocess.run(
                ["pactl", "move-sink-input", entry["id"], sink],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                moved += 1
                logger.info("Moved sink-input %s (app=%s) → %s", entry["id"], app or "?", sink)
                # Force full volume and unmute — Chrome may set volume to 0%
                # or cork the sink-input when running headless/automated.
                subprocess.run(["pactl", "set-sink-input-volume", entry["id"], "100%"],
                               capture_output=True, timeout=5)
                subprocess.run(["pactl", "set-sink-input-mute",   entry["id"], "0"],
                               capture_output=True, timeout=5)
            else:
                logger.debug("move-sink-input %s failed: %s", entry["id"], r.stderr.strip())

        if moved == 0:
            logger.debug("_move_chrome_audio: no sink-inputs found to move")
    except Exception as exc:
        logger.debug("_move_chrome_audio failed: %s", exc)


def _move_chrome_source_output(source: str = f"{PULSE_MIC_NAME}.monitor") -> None:
    """Move Chrome's microphone capture (source-outputs) to meetingbot_mic.monitor.

    This ensures that when TTS audio is played into meetingbot_mic, Chrome
    picks it up and transmits it to Google Meet as the bot's voice.

    Strategy: move ALL source-outputs that are NOT ffmpeg/arecord/parecord.
    This is belt-and-suspenders — Chrome's WebRTC stream may use an app name
    other than "chromium" (e.g. "chrome", "Chromium", or just the binary name),
    and after disabling WebRtcPipeWireCapture it will appear as a PulseAudio
    source-output.  We skip only known recorder processes.
    """
    SKIP_APPS = {"ffmpeg", "ffmpeg-static", "arecord", "parecord", "parec"}
    try:
        detail = _run(["pactl", "list", "source-outputs"])
        if detail.returncode != 0:
            return

        # Parse long-form output into per-entry dicts
        entries: list[dict] = []
        current: dict | None = None
        for raw_line in detail.stdout.splitlines():
            line = raw_line.strip()
            if line.startswith("Source Output #"):
                if current is not None:
                    entries.append(current)
                current = {"id": line.split("#", 1)[1].strip(), "app": ""}
            elif current is not None and ("application.name" in line or "application.process.binary" in line):
                # e.g.  application.name = "Chromium"
                val = line.split("=", 1)[-1].strip().strip('"').lower()
                if val:
                    current["app"] = val
        if current is not None:
            entries.append(current)

        if not entries:
            logger.debug("_move_chrome_source_output: no source-outputs found at all")
            return

        logger.debug(
            "_move_chrome_source_output: found %d source-output(s): %s",
            len(entries),
            [(e["id"], e["app"]) for e in entries],
        )

        moved = 0
        for entry in entries:
            if entry["app"] in SKIP_APPS:
                logger.debug("Skipping source-output %s (app=%s)", entry["id"], entry["app"])
                continue
            r = subprocess.run(
                ["pactl", "move-source-output", entry["id"], source],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                moved += 1
                logger.info(
                    "Moved source-output %s (app=%s) → %s",
                    entry["id"], entry["app"], source,
                )
            else:
                logger.debug(
                    "move-source-output %s failed: %s",
                    entry["id"], r.stderr.decode(errors="replace").strip(),
                )

        if moved == 0:
            logger.info(
                "_move_chrome_source_output: no source-outputs moved to %s "
                "(Chrome mic not yet open, already routed, or using PipeWire — "
                "check --disable-features=WebRtcPipeWireCapture is in launch args)",
                source,
            )
    except Exception as exc:
        logger.debug("_move_chrome_source_output failed: %s", exc)


def _sync_chrome_audio_routing(
    pulse_sink: str = PULSE_SINK_NAME,
    pulse_mic: str = PULSE_MIC_NAME,
    pulse_source: str | None = None,
) -> None:
    """Route Chrome's audio output to the recording sink and its mic to the TTS source.

    Call this once immediately after Chrome joins the meeting, then periodically
    throughout the call to handle WebRTC stream restarts.

    pulse_source: the PulseAudio source name to route Chrome's mic capture to.
    Defaults to the virtual source name (pulse_mic + '_virt') if not supplied,
    falling back to the monitor if the virtual source doesn't exist.
    """
    _move_chrome_audio(pulse_sink)
    # Prefer the virtual source (proper mic device Chrome enumerates), fall
    # back to the raw monitor if not available.
    target = pulse_source or f"{pulse_mic}_virt"
    _move_chrome_source_output(target)


# ── Xvfb & ffmpeg ─────────────────────────────────────────────────────────────

def _start_xvfb() -> tuple[Optional[subprocess.Popen], str]:
    """Start Xvfb on the next available virtual display.

    Each concurrent bot gets its own display number so they don't collide.
    Returns (proc, display_string) — e.g. (proc, ":99").
    If Xvfb is unavailable, returns (None, ":99") and the caller falls back
    to headless Chromium.
    """
    for _ in range(50):  # try up to 50 display numbers
        display = f":{next(_xvfb_display_counter)}"
        try:
            proc = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1280x720x24", "-ac", "+extension", "RANDR"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # Poll up to 2 s; Xvfb usually binds in <0.5 s
            for _ in range(20):
                time.sleep(0.1)
                if proc.poll() is not None:
                    break  # display already taken or other error — try next
            if proc.poll() is None:
                _register_proc(proc)
                logger.info("Xvfb started on display %s", display)
                return proc, display
            # Process exited; try the next display number
        except FileNotFoundError:
            logger.warning("Xvfb not available — falling back to headless mode")
            return None, display
        except Exception as exc:
            logger.debug("Xvfb attempt on %s failed: %s", display, exc)

    logger.warning("Could not start Xvfb on any display — falling back to headless mode")
    return None, ":99"


def _start_ffmpeg(audio_path: str, pulse_sink: str = PULSE_SINK_NAME) -> Optional[subprocess.Popen]:
    try:
        # Carry PulseAudio server address explicitly so ffmpeg connects to the
        # same server even if XDG_RUNTIME_DIR is not set in the child env.
        pulse_env = {**os.environ}
        rt = os.environ.get("XDG_RUNTIME_DIR", "/tmp/runtime-meetingbot")
        pulse_env.setdefault("PULSE_SERVER", f"unix:{rt}/pulse/native")

        proc = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-f", "pulse", "-i", f"{pulse_sink}.monitor",
                "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le",
                audio_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=pulse_env,
        )
        time.sleep(0.5)
        if proc.poll() is None:
            _register_proc(proc)
            logger.info("ffmpeg recording %s → %s", pulse_sink, audio_path)
            return proc
        logger.warning("ffmpeg exited immediately — PulseAudio sink %s may not be ready", pulse_sink)
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


async def _join_google_meet(page: Page, url: str, bot_name: str, start_muted: bool = True) -> None:
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
    if start_muted:
        await _click(page, [
            "button[aria-label*='Turn off microphone' i]",
            "button[aria-label*='microphone' i][aria-pressed='true']",
        ], timeout=2000)
        logger.info("Google Meet: mic muted before joining")
    else:
        logger.info("Google Meet: joining with mic ON (start_muted=False)")
        # Google Meet frequently auto-mutes on join. Unmute by clicking the
        # "Turn on microphone" button if visible; if not visible (mic already on
        # or button not yet rendered), use Ctrl+D which is safe here because we
        # know we want the mic ON — worst case it toggles an already-on mic off,
        # so we retry once more with a button click.
        await asyncio.sleep(0.8)
        unmuted = await _click(page, [
            "button[aria-label*='Turn on microphone' i]",
            "button[aria-label*='unmute' i]",
            "button[data-is-muted='true'][aria-label*='microphone' i]",
        ], timeout=1500)
        if not unmuted:
            # Button not found: mic is either already on, or not rendered yet.
            # Check state; use Ctrl+D only if confirmed muted or still unknown
            # (pre-join state where the in-call toolbar isn't visible yet).
            try:
                muted = await page.evaluate("""
                    () => {
                        const off = document.querySelector('button[aria-label*="Turn on microphone" i]');
                        if (off) return true;
                        const on = document.querySelector('button[aria-label*="Turn off microphone" i]');
                        if (on) return false;
                        return null;
                    }
                """)
            except Exception:
                muted = None
            if muted is not False:  # True (muted) or None (unknown) → try Ctrl+D
                await page.keyboard.press("Control+d")
                logger.info("Google Meet: mic unmuted via Ctrl+D on join (state=%s)", muted)
            else:
                logger.info("Google Meet: mic already on at join time — no action needed")
    # Turn off camera if currently on
    await _click(page, [
        "button[aria-label*='Turn off camera' i]",
        "button[aria-label*='camera' i][aria-pressed='true']",
    ], timeout=2000)
    logger.info("Google Meet: camera muted before joining")

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


async def _join_zoom(page: Page, url: str, bot_name: str, start_muted: bool = True) -> None:
    # Convert to Zoom web-client URL
    web_url = url
    if "/j/" in url and "/wc/" not in url:
        meeting_id = url.split("/j/")[1].split("?")[0].split("/")[0]
        pwd = ("&pwd=" + url.split("pwd=")[1].split("&")[0]) if "pwd=" in url else ""
        web_url = f"https://app.zoom.us/wc/{meeting_id}/join?prefer=1{pwd}"

    await page.goto(web_url, wait_until="domcontentloaded", timeout=30_000)
    # Wait for either the "join from browser" link or the name field — whichever
    # appears first (avoids a fixed 3 s sleep).
    try:
        await page.wait_for_selector(
            "a:has-text('join from your browser'), #btnJoinByBrowser, "
            "button:has-text('Join from Browser'), "
            "input#inputname, input[name='inputname'], input[placeholder*='name' i]",
            timeout=8000,
        )
    except Exception:
        pass

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
        # Wait for the name input to appear rather than sleeping a fixed 3 s
        try:
            await page.wait_for_selector(
                "input#inputname, input[name='inputname'], input[placeholder*='name' i]",
                timeout=6000,
            )
        except Exception:
            await asyncio.sleep(1)

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
    if start_muted:
        await _click(page, [
            "button.preview-audio-control",
            "button[aria-label*='mute microphone' i]",
            "button[aria-label*='mute audio' i]",
            "button[aria-label*='microphone' i]",
            ".join-audio-by-voip__mute-btn",
        ], timeout=2000)
        logger.info("Zoom: mic muted before joining")
    else:
        logger.info("Zoom: joining with mic ON (start_muted=False)")
    await _click(page, [
        "button.preview-video-control",
        "button[aria-label*='stop video' i]",
        "button[aria-label*='turn off camera' i]",
        "button[aria-label*='camera' i]",
    ], timeout=2000)
    logger.info("Zoom: camera muted before joining")

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


async def _join_teams(page: Page, url: str, bot_name: str, start_muted: bool = True) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    # Wait for the Teams SPA to render a recognisable element instead of a
    # fixed 5 s sleep.  We accept either a gate button OR the pre-join name
    # input (light-experience meetings skip the gate entirely).
    try:
        await page.wait_for_selector(
            "button:has-text('Join anonymously'), "
            "button[data-tid='anonymous-join-button'], "
            "button[data-tid='joinAsGuestButton'], "
            "button:has-text('Continue without signing in'), "
            "button:has-text('Join as a guest'), "
            "button:has-text('Continue on this browser'), "
            "button:has-text('Join on the web instead'), "
            "button:has-text('Join without Teams'), "
            "input[data-tid='prejoin-display-name-input'], "
            "input[placeholder*='name' i]",
            timeout=10000,
        )
    except Exception:
        pass
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
    # After clicking the gate (or if none was found), wait for the pre-join
    # name field instead of sleeping a fixed 3 s.
    _prejoin_name_sel = (
        "input[data-tid='prejoin-display-name-input'], "
        "input[data-tid='anonymous-join-name-input'], "
        "input[placeholder*='name' i], input[type='text']"
    )
    if ok:
        try:
            await page.wait_for_selector(_prejoin_name_sel, timeout=6000)
        except Exception:
            await asyncio.sleep(0.5)
    else:
        # No gate button found — light experience lands directly on pre-join screen
        await _screenshot(page, "teams_no_continue_button")
        try:
            await page.wait_for_selector(_prejoin_name_sel, timeout=6000)
        except Exception:
            await asyncio.sleep(0.5)

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
    if start_muted:
        await _click(page, [
            "button[aria-label*='Mute' i][aria-pressed='true']",
            "button[aria-label*='Mute microphone' i]",
            "button[aria-label*='microphone' i][aria-pressed='true']",
            "div[role='button'][aria-label*='Mute' i]",
        ], timeout=2000)
        logger.info("Teams: mic muted before joining")
    else:
        logger.info("Teams: joining with mic ON (start_muted=False)")
    # Turn off camera if on
    await _click(page, [
        "button[aria-label*='Turn off camera' i]",
        "button[aria-label*='camera' i][aria-pressed='true']",
        "button[aria-label*='Stop video' i]",
        "div[role='button'][aria-label*='Turn off camera' i]",
    ], timeout=2000)
    logger.info("Teams: camera muted before joining")

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

        await asyncio.sleep(0.5)

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


# ── Live-caption & chat helpers (for respond-on-mention) ──────────────────────

async def _leave_meeting(page: Page, platform: str) -> None:
    """Best-effort: click the Leave / End call button so the bot exits gracefully."""
    try:
        if platform == "google_meet":
            await _click(page, [
                "button[aria-label*='Leave call' i]",
                "button[aria-label*='leave' i]",
                "button[jsname='CQylAd']",
                "div[role='button'][aria-label*='Leave call' i]",
            ], timeout=3000)
        elif platform == "zoom":
            await _click(page, [
                "button[aria-label*='Leave' i]",
                "button[aria-label*='End' i]",
                ".footer-button__leave-btn",
            ], timeout=2000)
            await asyncio.sleep(0.4)
            # Zoom shows a confirmation dialog — click "Leave Meeting"
            await _click(page, [
                "button:has-text('Leave Meeting')",
                "button[aria-label*='Leave Meeting' i]",
            ], timeout=2000)
        elif platform == "microsoft_teams":
            await _click(page, [
                "button[aria-label*='Leave' i]",
                "button[data-tid*='leave' i]",
                "button:has-text('Leave')",
            ], timeout=2000)
    except Exception as exc:
        logger.debug("_leave_meeting: %s", exc)


async def _captions_already_active(page: Page, platform: str) -> bool:
    """Return True if live captions appear to be ON already.

    Checks:
    1. Whether the CC/captions button has aria-pressed/checked = true
    2. Whether a known caption container element exists in the DOM
    """
    try:
        if platform == "google_meet":
            result = await page.evaluate("""
                () => {
                    // 1. Button pressed-state — most reliable indicator
                    const btns = document.querySelectorAll(
                        'button[aria-label*="caption" i], button[aria-label*="subtitle" i]'
                    );
                    for (const btn of btns) {
                        const pressed = btn.getAttribute('aria-pressed');
                        const checked = btn.getAttribute('aria-checked');
                        if (pressed === 'true' || checked === 'true')
                            return 'button:aria-pressed=true';
                        // Filled / highlighted icon class (varies by Meet version)
                        if (btn.className && btn.className.includes('r6xAKc'))
                            return 'button:class=r6xAKc';
                        // If the button says "Turn off captions", captions are ON
                        // (Google Meet 2026 uses label semantics instead of aria-pressed)
                        const lbl = (btn.getAttribute('aria-label') || '').toLowerCase();
                        if (lbl === 'turn off captions' || lbl === 'disable captions'
                                || lbl.includes('turn off caption') || lbl.includes('disable caption'))
                            return 'button:label=turn-off-captions';
                    }
                    // Also check by jsname — 'RrG0hf' is the "Turn off captions" button in 2026
                    if (document.querySelector('button[jsname="RrG0hf"]'))
                        return 'button:jsname=RrG0hf';
                    // 2. Caption container present in the DOM
                    const containers = [
                        'div[jsname="tgaKEf"]', 'div[jsname="YSxPC"]',
                        'div[jsname="VUpckd"]', 'div[jsname="z1asCe"]',
                        'div[class*="a4cQT"]',
                        // 2026 caption container classes confirmed via live DOM inspection
                        'div[class*="vNKgIf"]', 'div[class*="ygicle"]', 'div[class*="nMcdL"]',
                    ];
                    for (const s of containers) {
                        if (document.querySelector(s)) return 'container:' + s;
                    }
                    return '';
                }
            """)
            if result:
                logger.debug("Captions active indicator: %s", result)
            return bool(result)
        elif platform == "zoom":
            result = await page.evaluate("""
                () => {
                    // Check if Live Transcript button is pressed
                    const btns = document.querySelectorAll(
                        'button[aria-label*="Live Transcript" i], button[aria-label*="caption" i]'
                    );
                    for (const btn of btns) {
                        if (btn.getAttribute('aria-pressed') === 'true' ||
                            btn.getAttribute('aria-checked') === 'true') return true;
                    }
                    // Caption container visible
                    return !!document.querySelector('.zm-caption-container');
                }
            """)
            return bool(result)
        elif platform == "microsoft_teams":
            result = await page.evaluate("""
                () => {
                    const btns = document.querySelectorAll(
                        'button[data-tid*="caption"], button[aria-label*="caption" i]'
                    );
                    for (const btn of btns) {
                        if (btn.getAttribute('aria-pressed') === 'true' ||
                            btn.getAttribute('aria-checked') === 'true') return true;
                    }
                    return !!document.querySelector('div[data-tid*="caption"]');
                }
            """)
            return bool(result)
    except Exception:
        pass
    return False


async def _enable_captions(page: Page, platform: str) -> None:
    """Best-effort: ensure live captions are ON.

    Checks whether captions are already active before clicking the toggle —
    clicking when ON would turn them OFF (same toggle-close bug as chat panel).
    After clicking, verifies captions are now active; if they disappeared we
    accidentally toggled them off and click once more to restore.
    """
    if platform == "google_meet":
        if await _captions_already_active(page, platform):
            logger.debug("Google Meet captions already active — skipping toggle click")
            return
        # Use short per-selector timeout (500 ms) so we don't waste 15+ seconds
        # when none of the selectors match the 2026 Google Meet UI.
        # IMPORTANT: do NOT include 'Turn off captions' here — clicking it when
        # captions are already ON would disable them (toggle bug).  Only click
        # selectors that enable captions.
        clicked = await _click(page, [
            # 2026 exact aria-label for the OFF state (captions are currently off)
            "button[aria-label='Turn on captions']",
            # jsname-based (stable internal identifiers)
            "button[jsname='r8qRAd']",
            # Partial aria-label matches (older Meet versions)
            "button[aria-label*='live caption' i]",
            "button[aria-label*='subtitles' i]",
        ], timeout=500)

        if not clicked:
            # Fallback: captions may be hidden inside the ⋮ "More options" menu
            logger.debug("Google Meet: captions button not found directly — trying ⋮ menu")
            opened = await _click(page, [
                "button[aria-label='More options']",
                "button[aria-label*='More' i][aria-haspopup]",
                "div[role='button'][aria-label*='More' i]",
            ], timeout=2000)
            if opened:
                await asyncio.sleep(0.5)
                await _click(page, [
                    "li[aria-label*='caption' i]",
                    "div[role='menuitem'][aria-label*='caption' i]",
                    "li:has-text('captions')",
                    "span:has-text('captions')",
                ], timeout=2000)

        # Brief wait then verify: if captions not yet active, try once more.
        # Only use explicit "Turn on" selectors to avoid accidentally disabling.
        await asyncio.sleep(0.6)
        if not await _captions_already_active(page, platform):
            await _click(page, [
                "button[aria-label='Turn on captions']",
                "button[aria-label*='live caption' i]",
                "button[aria-label*='subtitles' i]",
            ], timeout=500)
    elif platform == "zoom":
        # Captions may live inside a "More" overflow menu
        await _click(page, [
            "button[aria-label*='More' i]",
            "button[aria-label='More actions']",
        ], timeout=1500)
        await asyncio.sleep(0.4)
        await _click(page, [
            "button[aria-label*='caption' i]",
            "button[aria-label*='live caption' i]",
            "button:has-text('Enable live caption')",
            "button:has-text('Start live transcription')",
            "button[aria-label*='CC' i]",
        ], timeout=2000)
    elif platform == "microsoft_teams":
        await _click(page, [
            "button[aria-label*='Live caption' i]",
            "button[aria-label*='Caption' i]",
            "button[data-tid*='caption']",
            "button:has-text('Turn on live captions')",
        ], timeout=3000)


async def _chat_input_visible(page: Page, platform: str) -> bool:
    """Return True if the chat input element is already visible on screen."""
    if platform == "google_meet":
        sels = [
            "div[contenteditable='true'][aria-label*='message' i]",
            "div[contenteditable='true'][aria-label*='chat' i]",
            "div[contenteditable='true'][role='textbox']",
            "textarea[aria-label*='message' i]",
        ]
    elif platform == "zoom":
        sels = [
            "input[placeholder*='message' i]",
            "textarea[placeholder*='message' i]",
            "div[contenteditable='true']",
        ]
    elif platform == "microsoft_teams":
        sels = [
            "div[data-tid='send-message-input']",
            "div[contenteditable='true'][role='textbox']",
        ]
    else:
        return False
    for sel in sels:
        try:
            if await page.locator(sel).first.is_visible(timeout=400):
                return True
        except Exception:
            pass
    return False


async def _open_chat(page: Page, platform: str) -> None:
    """Ensure the meeting chat panel is open.

    Checks whether the chat input is already visible before clicking the toggle
    button — clicking it when the panel is already open would close it.
    """
    if await _chat_input_visible(page, platform):
        logger.debug("Chat panel already open on %s — skipping toggle click", platform)
        return

    if platform == "google_meet":
        await _click(page, [
            "button[aria-label*='chat' i]",
            "button[aria-label*='message' i]",
            "button[aria-label*='show chat' i]",
            "div[role='button'][aria-label*='chat' i]",
        ], timeout=2000)
    elif platform == "zoom":
        await _click(page, [
            "button[aria-label*='chat' i]",
            "button[aria-label*='open chat' i]",
        ], timeout=2000)
        await asyncio.sleep(0.3)
    elif platform == "microsoft_teams":
        await _click(page, [
            "button[aria-label*='Chat' i]",
            "button[aria-label*='Open chat' i]",
        ], timeout=1500)


async def _scrape_captions(page: Page, platform: str) -> str:
    """Return the current live-caption text visible in the DOM, or '' on failure."""
    try:
        if platform == "google_meet":
            text = await page.evaluate("""
                () => {
                    // Try caption-specific containers in order of specificity.
                    // IMPORTANT: do NOT use aria-live='polite' — that matches
                    // Google Meet's screen-reader announcements ("You have joined
                    // the call", "Your microphone is on", etc.) which are NOT
                    // speech captions and would break mention detection.
                    const selectors = [
                        // Google Meet 2026 class names confirmed via live DOM inspection
                        "div[class*='ygicle']",    // pure caption text (no speaker name)
                        "div[class*='nMcdL']",     // full caption row: speaker + text
                        "div[class*='vNKgIf']",    // outer caption container
                        // jsname attrs — caption-specific (may be stale in 2026)
                        "div[jsname='tgaKEf']",
                        "div[jsname='YSxPC']",
                        "div[jsname='VUpckd']",
                        "div[jsname='z1asCe']",
                        // aria-label specifically for captions
                        "div[aria-label*='caption' i]",
                        // Class-name fragments seen in the caption overlay
                        "div[class*='VbkSUe']",
                        "div[class*='CNusmb']",
                        "div[class*='a4cQT']",
                        "div[class*='caption']",
                        "div[class*='bj4p3b']",
                        "div[class*='lTnCnb']",
                        "div[class*='subtitle']",
                        // 2026 broader fallbacks
                        "[data-caption-text]",
                        "div[class*='transcript' i] span",
                        "div[class*='Transcript' i] span",
                        "span[class*='caption' i]",
                        "span[class*='Caption' i]",
                    ];
                    // Known accessibility-announcement prefixes to skip
                    const skipPrefixes = [
                        'You have joined', 'Your microphone', 'Your camera',
                        'There is one other', 'There are ', 'You are now',
                        'This call is being', 'Your hand',
                        // Material icon text + UI button labels that leak into
                        // caption containers via the "scroll to bottom" button
                        'arrow_downward', 'Jump to bottom',
                        'expand_more', 'expand_less', 'keyboard_arrow',
                    ];
                    // Also skip strings that look like a Material icon name only
                    // (all lowercase/underscores, no spaces) — these are never speech.
                    const materialIconRe = /^[a-z][a-z_]+$/;
                    const isValid = t => t && t.length > 3 &&
                        !skipPrefixes.some(p => t.startsWith(p)) &&
                        !t.includes('Jump to bottom') &&
                        !materialIconRe.test(t.split('\n')[0]);
                    // Google Meet adds captions in DOM order: older utterances
                    // first, the current / most-recent one last.  We must return
                    // the LAST valid text across all matches so that we get the
                    // newest speech, not the bot's own previous TTS response which
                    // appears earlier in the DOM and would otherwise always win.
                    for (const s of selectors) {
                        const els = document.querySelectorAll(s);
                        let last = '';
                        for (const el of els) {
                            const t = (el.innerText || el.textContent || '').trim();
                            if (isValid(t)) last = t;
                        }
                        if (last) return last;
                    }
                    // Scan aria-live="assertive" regions — Google Meet 2026 uses
                    // assertive for caption overlays so screen readers read them immediately.
                    const assertiveEls = Array.from(document.querySelectorAll('[aria-live="assertive"]'));
                    for (const el of assertiveEls) {
                        const t = (el.innerText || el.textContent || '').trim();
                        if (isValid(t)) return t;
                    }

                    // Scan aria-live="polite" regions.
                    const liveEls = Array.from(document.querySelectorAll('[aria-live="polite"]'));
                    if (liveEls.length) {
                        const candidates = liveEls
                            .map(el => (el.innerText || el.textContent || '').trim())
                            .filter(t => t.length >= 10 && isValid(t));
                        if (candidates.length) {
                            return candidates.reduce((a, b) => a.length >= b.length ? a : b);
                        }
                    }

                    // Broad class-name fallback: any element whose class contains
                    // "caption" or "transcript".
                    const broadRe = /caption|transcript|subtitle/i;
                    for (const el of document.querySelectorAll('div, span')) {
                        if (broadRe.test(el.className || '') || broadRe.test(el.getAttribute('data-type') || '')) {
                            const t = (el.innerText || el.textContent || '').trim();
                            if (isValid(t) && t.length > 10) return t;
                        }
                    }

                    // Last-resort: element-based viewport sweep.
                    // Uses the same approach as the diagnostic DOM dump (confirmed
                    // to work).  Iterates all div/span elements in document order,
                    // keeps those in the bottom 35% of the viewport, and returns
                    // the LAST valid one — newer captions appear lower in the DOM
                    // so taking the last gives us the most recent speech.
                    // NOTE: no upper length limit — accumulated caption text can
                    // grow to thousands of chars across a long meeting.
                    try {
                        const vpY = window.innerHeight * 0.65;
                        const vpEls = Array.from(document.querySelectorAll('div,span'))
                            .filter(el => {
                                const r = el.getBoundingClientRect();
                                return r.top >= vpY && r.width > 30 && r.height > 8;
                            });
                        let lastVp = '';
                        for (const el of vpEls) {
                            const t = (el.innerText || el.textContent || '').trim();
                            if (t && t.length >= 8 && isValid(t)) lastVp = t;
                        }
                        if (lastVp) return lastVp;
                    } catch(_) {}

                    return '';
                }
            """)
        elif platform == "zoom":
            text = await page.evaluate("""
                () => {
                    const sel = [
                        ".zm-caption-container",
                        "div[class*='caption']",
                        "div[role='region']",
                        "div[class*='subtitle']",
                    ];
                    for (const s of sel) {
                        const el = document.querySelector(s);
                        const t = el ? (el.innerText || el.textContent || '').trim() : '';
                        if (t && t.length > 3) return t;
                    }
                    return '';
                }
            """)
        elif platform == "microsoft_teams":
            text = await page.evaluate("""
                () => {
                    const sel = [
                        "div[data-tid*='caption']",
                        "div[class*='captions-container']",
                        "div[role='region'][aria-label*='caption' i]",
                        "div[class*='subtitle']",
                    ];
                    for (const s of sel) {
                        const el = document.querySelector(s);
                        const t = el ? (el.innerText || el.textContent || '').trim() : '';
                        if (t && t.length > 3) return t;
                    }
                    return '';
                }
            """)
        else:
            return ""
        return (text or "").strip()
    except Exception:
        return ""


async def _scrape_chat_messages(page: Page, platform: str) -> str:
    """Return all visible incoming chat message text, or '' on failure.

    Only reads the messages list — not the input box.  The chat panel must
    already be open (call _open_chat first) for this to return anything useful.
    """
    try:
        if platform == "google_meet":
            text = await page.evaluate("""
                () => {
                    const tryText = el => (el ? (el.innerText || el.textContent || '').trim() : '');
                    const good    = t  => t && t.length > 3;

                    // 1. Known message-list containers (ordered by specificity)
                    const listSels = [
                        "div[jsname='xySENc']",                        // historical jsname
                        "div[role='list'][aria-label*='message' i]",
                        "div[role='list'][aria-label*='chat' i]",
                        "div[role='log']",                             // ARIA log region
                        "div[class*='chat'] div[role='list']",
                        "div[class*='GDhqjd']",                       // 2024-2025 class
                        "div[class*='oIy2qc']",                       // 2026 variant
                        "c-wiz div[role='list']",                     // generic CWiz list
                    ];
                    for (const s of listSels) {
                        const el = document.querySelector(s);
                        const t  = tryText(el);
                        if (good(t)) return t;
                    }

                    // 2. Chat panel containers — strip editable/button children
                    const panelSels = [
                        "div[aria-label='In-call messages']",
                        "div[aria-label*='in-call' i]",
                        "div[aria-label='Chat']",
                        "div[aria-label*='chat' i][role='region']",
                        "div[aria-label*='chat' i][role='dialog']",
                        "div[data-panel-id='3']",                     // Meet side-panel id
                        "div[data-panel-id='chat']",
                    ];
                    for (const s of panelSels) {
                        const el = document.querySelector(s);
                        if (!el) continue;
                        const clone = el.cloneNode(true);
                        clone.querySelectorAll(
                            "div[contenteditable], textarea, input, button, form, [aria-label*='send' i]"
                        ).forEach(e => e.remove());
                        const t = tryText(clone);
                        if (good(t)) return t;
                    }

                    // 3. Broad sweep: any aside/section with 'chat' in aria-label
                    for (const el of document.querySelectorAll(
                        "aside, section, div[role='complementary']"
                    )) {
                        const lbl = (el.getAttribute('aria-label') || '').toLowerCase();
                        if (lbl.includes('chat') || lbl.includes('message')) {
                            const clone = el.cloneNode(true);
                            clone.querySelectorAll(
                                "div[contenteditable], textarea, input, button, form"
                            ).forEach(e => e.remove());
                            const t = tryText(clone);
                            if (good(t)) return t;
                        }
                    }
                    return '';
                }
            """)
        elif platform == "zoom":
            text = await page.evaluate("""
                () => {
                    const sel = [
                        ".chat-message-list__scroll-helper",
                        "div[class*='chat-message-list']",
                        "ul[class*='chat-list']",
                        "div[class*='ChatPanel'] div[role='list']",
                    ];
                    for (const s of sel) {
                        const el = document.querySelector(s);
                        const t = el ? (el.innerText || el.textContent || '').trim() : '';
                        if (t && t.length > 3) return t;
                    }
                    return '';
                }
            """)
        elif platform == "microsoft_teams":
            text = await page.evaluate("""
                () => {
                    const sel = [
                        "div[data-tid='chat-messages-panel']",
                        "div[aria-label*='Chat conversation']",
                        "div[class*='ui-chat__messageList']",
                        "div[class*='chat-messages-list']",
                    ];
                    for (const s of sel) {
                        const el = document.querySelector(s);
                        const t = el ? (el.innerText || el.textContent || '').trim() : '';
                        if (t && t.length > 3) return t;
                    }
                    return '';
                }
            """)
        else:
            return ""
        return (text or "").strip()
    except Exception:
        return ""


async def _type_into_chat(page: Page, selectors: list[str], message: str, timeout: int = 4000) -> bool:
    """Click and type a message into a chat input — works for both <textarea> and contenteditable divs.

    Unlike _fill() (which calls Playwright's .fill() and only works on <input>/<textarea>),
    this function uses .click() + page.keyboard.type() which works on all element types.
    """
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=timeout)
            await el.click()
            # Clear existing content (triple-click selects all, then type replaces)
            await page.keyboard.press("Control+a")
            await page.keyboard.type(message, delay=10)
            return True
        except Exception as exc:
            logger.debug("_type_into_chat selector %r failed: %s", sel, exc)
    return False


async def _send_chat_message(page: Page, platform: str, message: str) -> bool:
    """Type and send a chat message. Returns True on success."""
    try:
        if platform == "google_meet":
            # Step 1: Open chat panel — use _open_chat() which checks visibility
            # first to avoid accidentally closing the panel if it's already open.
            await _open_chat(page, platform)
            # Poll for chat input (up to 2s) to let the panel animate open
            for _ in range(8):
                if await _chat_input_visible(page, platform):
                    break
                await asyncio.sleep(0.25)

            # Step 2: Find and fill chat input (Google Meet uses a contenteditable div)
            input_sels = [
                # contenteditable div — most common in modern Meet
                "div[contenteditable='true'][aria-label*='message' i]",
                "div[contenteditable='true'][aria-label*='chat' i]",
                "div[contenteditable='true'][role='textbox']",
                # textarea fallback (older Meet versions)
                "textarea[aria-label*='message' i]",
                "textarea[aria-label*='chat' i]",
                "input[aria-label*='message' i]",
            ]
            typed = await _type_into_chat(page, input_sels, message, timeout=4000)
            logger.debug("GMeet type-into-chat: %s", typed)
            if not typed:
                await _screenshot(page, "gmeet_chat_input_not_found")
                return False

            # Step 3: Send (button or Enter)
            sent = await _click(page, [
                "button[aria-label*='send' i]",
                "button[aria-label*='Send message' i]",
            ], timeout=2000)
            if not sent:
                await page.keyboard.press("Enter")
            logger.debug("GMeet chat message sent")
            return True

        elif platform == "zoom":
            await _open_chat(page, platform)
            await asyncio.sleep(0.8)
            input_sels = [
                "input[placeholder*='message' i]",
                "textarea[placeholder*='message' i]",
                "div[contenteditable='true']",
            ]
            typed = await _type_into_chat(page, input_sels, message, timeout=3000)
            logger.debug("Zoom type-into-chat: %s", typed)
            if not typed:
                await _screenshot(page, "zoom_chat_input_not_found")
                return False
            sent = await _click(page, [
                "button[aria-label*='send' i]",
                "button[id*='send']",
            ], timeout=2000)
            if not sent:
                await page.keyboard.press("Enter")
            return True

        elif platform == "microsoft_teams":
            await _open_chat(page, platform)
            await asyncio.sleep(0.5)
            input_sels = [
                "div[data-tid='send-message-input']",
                "div[contenteditable='true'][role='textbox']",
                "textarea[aria-label*='message' i]",
            ]
            typed = await _type_into_chat(page, input_sels, message, timeout=3000)
            logger.debug("Teams type-into-chat: %s", typed)
            if not typed:
                await _screenshot(page, "teams_chat_input_not_found")
                return False
            sent = await _click(page, [
                "button[aria-label*='send' i]",
                "button[aria-label*='Send message' i]",
                "button[data-tid*='send']",
            ], timeout=2000)
            if not sent:
                await page.keyboard.press("Control+Enter")
            return True

    except Exception as exc:
        logger.warning("_send_chat_message failed (%s): %s", platform, exc)
    return False


# ── Mic mute / unmute & voice output ─────────────────────────────────────────

_MIC_UNMUTE_SELS: dict[str, list[str]] = {
    "google_meet": [
        "button[aria-label*='unmute' i]",
        "button[aria-label*='Turn on microphone' i]",
        "button[data-is-muted='true'][aria-label*='microphone' i]",
    ],
    "zoom": [
        "button[aria-label*='unmute' i]",
        "button[title*='unmute' i]",
    ],
    "microsoft_teams": [
        "button[aria-label*='unmute' i]",
        "button[data-tid*='microphone'][aria-pressed='false']",
        "button[title*='unmute' i]",
    ],
}

_MIC_MUTE_SELS: dict[str, list[str]] = {
    "google_meet": [
        "button[aria-label*='Turn off microphone' i]",
        "button[aria-label*='mute microphone' i]",
        "button[data-is-muted='false'][aria-label*='microphone' i]",
    ],
    "zoom": [
        "button[aria-label*='mute' i]:not([aria-label*='un' i])",
        "button[title*='mute' i]:not([title*='un' i])",
    ],
    "microsoft_teams": [
        "button[aria-label*='mute' i]:not([aria-label*='un' i])",
        "button[data-tid*='microphone'][aria-pressed='true']",
    ],
}


async def _unmute_mic(page: Page, platform: str) -> None:
    """Best-effort: unmute the bot's microphone in the meeting UI."""
    sels = _MIC_UNMUTE_SELS.get(platform, [])
    if sels:
        ok = await _click(page, sels, timeout=2000)
        if ok:
            logger.info("Mic unmuted on %s", platform)
            return
        logger.debug("_unmute_mic: unmute button not found on %s (mic may already be on)", platform)
    if platform == "google_meet":
        # Ctrl+D TOGGLES — only use it when mic is confirmed muted
        try:
            muted = await page.evaluate("""
                () => {
                    const unmute = document.querySelector('button[aria-label*="Turn on microphone" i]');
                    if (unmute) return true;
                    const mute = document.querySelector('button[aria-label*="Turn off microphone" i]');
                    if (mute) return false;
                    return null;
                }
            """)
        except Exception:
            muted = None
        if muted is True:
            await page.keyboard.press("Control+d")
            logger.info("Mic unmuted via Ctrl+D (confirmed muted state)")
        elif muted is False:
            logger.debug("_unmute_mic: mic already on — skipping Ctrl+D")
        else:
            logger.debug("_unmute_mic: mic state unknown — skipping Ctrl+D to avoid toggle")


async def _mute_mic(page: Page, platform: str) -> None:
    """Best-effort: mute the bot's microphone in the meeting UI."""
    sels = _MIC_MUTE_SELS.get(platform, [])
    if sels:
        ok = await _click(page, sels, timeout=2000)
        if not ok:
            logger.debug("_mute_mic: no button matched on %s (may already be muted)", platform)


async def _speak_in_meeting(
    page: Page,
    platform: str,
    text: str,
    tts_provider: str = "edge",
    gemini_api_key: str | None = None,
    start_muted: bool = True,
    pulse_mic: str = PULSE_MIC_NAME,
    pre_synthesized_path: str | None = None,
) -> bool:
    """Speak *text* aloud in the meeting via TTS → PulseAudio virtual mic.

    When start_muted=True (default): unmute before speaking, mute again after.
    When start_muted=False: mic is already on — just play the audio.

    pre_synthesized_path: if provided, skip TTS synthesis and use this file
    directly.  Allows the caller to start synthesis concurrently with other
    work (e.g. sending the chat message) and pass the result in.

    Returns True on success.
    """
    from app.services import tts_service

    try:
        # Step 1: use pre-synthesized audio if provided, otherwise synthesize now.
        if pre_synthesized_path:
            tts_path = pre_synthesized_path
        else:
            tts_path = await tts_service.synthesize(
                text, provider=tts_provider, api_key=gemini_api_key
            )
        if not tts_path:
            logger.warning("TTS synthesis returned no file — skipping voice response")
            return False

        # Step 2: unmute before speaking only when the bot is configured to stay muted.
        # When start_muted=False the mic is already on — calling _unmute_mic risks
        # Ctrl+D toggling it OFF (since Ctrl+D is a toggle, not a set-unmute command).
        if start_muted:
            await _unmute_mic(page, platform)

        # Step 3: re-sync Chrome's mic capture to the TTS virtual source right before
        # playback — the periodic sync may be up to 20 s stale, so doing it here
        # guarantees Chrome is reading from the correct source when audio starts.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: _move_chrome_source_output(f"{pulse_mic}_virt")
        )

        # Step 4: play into this bot's TTS mic sink (Chrome hears it as mic input)
        await tts_service.play_audio(tts_path, pulse_mic)

        # Step 5: mute again only if the bot is supposed to stay muted between replies
        if start_muted:
            await _mute_mic(page, platform)
        return True

    except Exception as exc:
        logger.warning("_speak_in_meeting failed: %s", exc)
        if start_muted:
            try:
                await _mute_mic(page, platform)
            except Exception:
                pass
        return False


_LEAVE_KEYWORDS = frozenset({"leave", "bye", "goodbye", "exit", "stop", "quit"})


def _pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap raw s16le PCM (16 kHz, mono) in a valid WAV container."""
    n = len(pcm)
    return struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + n, b'WAVE',
        b'fmt ', 16, 1, 1,   # PCM, mono
        16000,               # sample rate
        32000,               # byte rate (16000 * 1 ch * 2 bytes)
        2,                   # block align
        16,                  # bits per sample
        b'data', n,
    ) + pcm


async def _streaming_transcription_loop(
    audio_path: str,
    live_transcript: list,
    structured_transcript: list,
    on_transcript_entry=None,
) -> None:
    """VAD-based streaming transcription loop.

    Reads the growing WAV file continuously in 100 ms frames, uses an
    energy-based Voice Activity Detector to detect utterance boundaries, and
    sends each complete utterance to Gemini inline for transcription.  Results
    are available in <1 s from end-of-speech.

    Populates two shared lists:
    - live_transcript: plain-text chunks consumed by _mention_monitor for
      wake-word detection and meeting context (unchanged interface).
    - structured_transcript: {speaker, text, timestamp} dicts persisted to DB.

    on_transcript_entry: optional async callable(entry) invoked immediately
    after each entry is created, for real-time DB saves.
    """
    from app.config import settings as _cfg
    if not _cfg.GEMINI_API_KEY:
        logger.warning("Streaming transcription: GEMINI_API_KEY not set — disabled")
        return
    try:
        import google.generativeai as genai
        genai.configure(api_key=_cfg.GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")
    except ImportError:
        logger.warning("Streaming transcription: google-generativeai not installed")
        return

    FRAME_MS        = 100                             # poll every 100 ms
    FRAME_BYTES     = _PCM_BYTES_PER_S * FRAME_MS // 1000  # bytes per 100 ms frame
    GUARD_BYTES     = _PCM_BYTES_PER_S // 4          # 250 ms guard against partial writes
    # VAD thresholds
    SPEECH_ENERGY_THRESHOLD = 0.05   # fraction of non-zero samples to count as speech
    SPEECH_START_FRAMES  = 1         # 1 consecutive speech frame to open an utterance (faster)
    TRAILING_SILENCE_MS  = 250       # ms of silence to close an utterance (was 400)
    TRAILING_SILENCE_FRAMES = TRAILING_SILENCE_MS // FRAME_MS
    MIN_UTTERANCE_MS    = 200        # ignore utterances shorter than this (was 300)
    MIN_UTTERANCE_BYTES = _PCM_BYTES_PER_S * MIN_UTTERANCE_MS // 1000
    MAX_UTTERANCE_BYTES = _PCM_BYTES_PER_S * 30     # cap at 30 s to bound API latency

    prompt = (
        "Transcribe this speech segment. Return ONLY the spoken words as plain "
        "text — no timestamps, no speaker labels, no markdown. "
        "If there is no intelligible speech, return nothing."
    )

    file_pos = _WAV_HEADER_SIZE  # cursor into the growing WAV file (skips header)

    # Diagnostic counters — logged periodically so we can see if audio is flowing
    _total_frames_processed = 0
    _total_speech_frames    = 0
    _last_diag_log_pos      = file_pos   # file position at last diagnostic log

    # VAD state machine
    speech_frames    = 0    # consecutive speech frames seen
    silence_frames   = 0    # consecutive silence frames after speech started
    in_utterance     = False
    utterance_pcm    = bytearray()
    utterance_start_byte = 0  # file offset where current utterance began

    logger.info("Streaming transcription loop started")

    def _is_speech_frame(pcm_frame: bytes) -> bool:
        """Energy-based VAD: true when >5% of s16le samples are non-zero.

        Uses struct.unpack to decode actual s16le sample values rather than
        inspecting raw bytes — the old byte-level check (`[::20]`) missed
        samples whose value is a non-zero multiple of 256 (e.g. 256 = 0x0100
        has a zero low byte and would be counted as silence).
        """
        n = len(pcm_frame) // 2  # number of s16le samples
        if n == 0:
            return False
        # Decode every 10th sample for efficiency (~160 samples per 100 ms frame)
        samples = struct.unpack_from(f"<{n}h", pcm_frame)[::10]
        nonzero = sum(1 for s in samples if s != 0)
        return nonzero / len(samples) > SPEECH_ENERGY_THRESHOLD

    async def _transcribe_utterance(pcm: bytes, start_byte: int) -> None:
        """Send PCM utterance to Gemini, append result to shared lists."""
        timestamp_s = max(0.0, (start_byte - _WAV_HEADER_SIZE) / _PCM_BYTES_PER_S)
        logger.info(
            "Streaming transcription: utterance %.1f s at t=%.1f s",
            len(pcm) / _PCM_BYTES_PER_S, timestamp_s,
        )
        try:
            audio_part = genai.protos.Part(
                inline_data=genai.protos.Blob(
                    mime_type="audio/wav",
                    data=_pcm_to_wav(pcm),
                )
            )
            response = await model.generate_content_async(
                [audio_part, prompt],
                generation_config={"temperature": 0.0, "max_output_tokens": 1024},
            )
            try:
                text = (response.text or "").strip()
            except ValueError:
                text = ""
            if not text:
                return
            # Plain-text list for mention monitor.  Do NOT truncate — the
            # mention monitor tracks absolute indices (last_audio_idx) so
            # truncating the list silently breaks voice-mention detection.
            live_transcript.append(text)
            logger.info("Streaming transcript (t=%.1f s): %r…", timestamp_s, text[:120])
            # Structured entry for DB persistence
            entry = {"speaker": "Unknown", "text": text, "timestamp": round(timestamp_s, 2)}
            structured_transcript.append(entry)
            if on_transcript_entry is not None:
                try:
                    await on_transcript_entry(entry)
                except Exception as cb_exc:
                    logger.warning("on_transcript_entry callback error: %s", cb_exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Streaming transcription utterance error: %s", exc)

    while True:
        await asyncio.sleep(FRAME_MS / 1000)
        try:
            if not os.path.exists(audio_path):
                continue
            file_size = os.path.getsize(audio_path)
            safe_end  = max(_WAV_HEADER_SIZE, file_size - GUARD_BYTES)
            if safe_end <= file_pos:
                continue

            # Read all new frames available
            with open(audio_path, 'rb') as f:
                f.seek(file_pos)
                new_data = f.read(safe_end - file_pos)

            if not new_data:
                continue

            # Process frame by frame
            offset = 0
            while offset + FRAME_BYTES <= len(new_data):
                frame = new_data[offset: offset + FRAME_BYTES]
                frame_file_pos = file_pos + offset
                offset += FRAME_BYTES
                is_speech = _is_speech_frame(frame)

                if not in_utterance:
                    if is_speech:
                        speech_frames += 1
                        if speech_frames >= SPEECH_START_FRAMES:
                            in_utterance = True
                            silence_frames = 0
                            utterance_start_byte = frame_file_pos - (speech_frames - 1) * FRAME_BYTES
                            utterance_pcm = bytearray()
                            # backfill the opening speech frames we already read
                            backfill_start = max(_WAV_HEADER_SIZE, utterance_start_byte)
                            with open(audio_path, 'rb') as bf:
                                bf.seek(backfill_start)
                                utterance_pcm.extend(bf.read(frame_file_pos - backfill_start + FRAME_BYTES))
                    else:
                        speech_frames = 0
                else:
                    # Inside an utterance
                    utterance_pcm.extend(frame)
                    if is_speech:
                        silence_frames = 0
                    else:
                        silence_frames += 1

                    too_long = len(utterance_pcm) >= MAX_UTTERANCE_BYTES
                    end_of_speech = silence_frames >= TRAILING_SILENCE_FRAMES

                    if end_of_speech or too_long:
                        in_utterance = False
                        speech_frames = 0
                        silence_frames = 0
                        if len(utterance_pcm) >= MIN_UTTERANCE_BYTES:
                            pcm_copy = bytes(utterance_pcm)
                            start_copy = utterance_start_byte
                            utterance_pcm = bytearray()
                            asyncio.create_task(_transcribe_utterance(pcm_copy, start_copy))
                        else:
                            utterance_pcm = bytearray()

            file_pos = file_pos + offset   # advance past fully-processed frames

            # Partial frame left at end — leave in next read (file_pos not advanced past it)

            # Diagnostic: log VAD activity every ~30 s of audio processed
            _total_frames_processed += offset // FRAME_BYTES
            _total_speech_frames    += sum(
                1 for i in range(0, offset, FRAME_BYTES)
                if _is_speech_frame(new_data[i: i + FRAME_BYTES])
            )
            bytes_since_last = file_pos - _last_diag_log_pos
            if bytes_since_last >= _PCM_BYTES_PER_S * 30:   # every ~30 s of audio
                _last_diag_log_pos = file_pos
                audio_s = (file_pos - _WAV_HEADER_SIZE) / _PCM_BYTES_PER_S
                logger.info(
                    "VAD heartbeat: %.0f s audio processed, %d/%d frames had speech (%.1f%%)",
                    audio_s, _total_speech_frames, _total_frames_processed,
                    100.0 * _total_speech_frames / max(1, _total_frames_processed),
                )
                if _total_speech_frames == 0:
                    logger.warning(
                        "VAD: zero speech frames detected in %.0f s — "
                        "audio may be silent; check PulseAudio routing and Chrome audio output",
                        audio_s,
                    )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Streaming transcription frame error (%s): %s", type(exc).__name__, exc)


# ── Gemini Live API loop ──────────────────────────────────────────────────────

_LIVE_MODEL      = "gemini-2.5-flash-native-audio-preview-12-2025"
_LIVE_CHUNK_MS   = 100                                   # send 100 ms of PCM per frame
_LIVE_CHUNK_BYTES = _PCM_BYTES_PER_S * _LIVE_CHUNK_MS // 1000   # bytes per 100 ms frame
_LIVE_GUARD_BYTES = _PCM_BYTES_PER_S // 4                # 250 ms guard against partial writes
_LIVE_SESSION_S   = 600                                  # reconnect every 10 min to refresh context


def _build_live_system_instruction(
    bot_name: str,
    structured_transcript: list,
) -> str:
    """Build the system instruction injected into each Gemini Live session."""
    if structured_transcript:
        ctx_lines = "\n".join(
            f"[{e.get('timestamp', 0):.0f}s] {e.get('speaker','?')}: {e.get('text','')}"
            for e in structured_transcript[-40:]   # last 40 entries ≈ ~10 min
        )
        context_block = f"\nMeeting transcript so far (for context only — do not re-read or repeat this):\n{ctx_lines}\n"
    else:
        context_block = ""

    return (
        f"You are '{bot_name}', an AI assistant attending a meeting as a silent observer.\n"
        f"You are LISTENING to the live meeting audio.\n\n"
        f"Your two jobs:\n"
        f"1. TRANSCRIBE what participants say (you will see the transcription appear automatically).\n"
        f"2. RESPOND in voice ONLY when a participant addresses you by name '{bot_name}'.\n"
        f"   - When addressed, answer helpfully in 2-3 natural spoken sentences.\n"
        f"   - Use the meeting context provided below to answer questions about this meeting.\n"
        f"   - For general knowledge questions, use your own knowledge.\n"
        f"   - If you were just greeted or your name was called without a question, "
        f"briefly acknowledge and offer to help.\n"
        f"   - Do NOT respond to speech not directed at you.\n"
        f"   - Do NOT comment on the meeting unless asked.\n"
        f"   - Speak naturally — no bullet points, no markdown.\n"
        f"{context_block}"
    )


async def _gemini_live_loop(
    audio_path: str,
    bot_name: str,
    live_transcript: list,
    structured_transcript: list,
    mention_response_mode: str = "both",
    page=None,
    platform: str = "google_meet",
    start_muted: bool = True,
    pulse_mic: str = PULSE_MIC_NAME,
    gemini_api_key: str = "",
    last_live_response_at: list | None = None,
) -> None:
    """Real-time Gemini Live voice-response loop (Session B).

    Streams raw PCM into a Gemini Live (native audio) session and plays back
    AUDIO responses when the bot is addressed.  Transcription of participants'
    speech is handled by the parallel _streaming_transcription_loop (Session A)
    so this session is dedicated entirely to voice responses.

    Uses structured_transcript (populated by Session A) as context when
    building the system instruction at the start of each 10-minute window.
    Reconnects every _LIVE_SESSION_S seconds to refresh that context.
    """
    try:
        import google.genai as genai_live
        from google.genai import types as genai_types
    except ImportError:
        logger.warning(
            "google-genai not installed — Gemini Live voice responses unavailable. "
            "Run: pip install 'google-genai>=1.0.0'."
        )
        return

    if not gemini_api_key:
        logger.warning("Gemini Live: no API key — disabled")
        return

    client = genai_live.Client(api_key=gemini_api_key)
    file_pos = _WAV_HEADER_SIZE   # cursor into growing WAV; skip WAV header on first read
    session_start = time.monotonic()
    session_count = 0
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 3

    logger.info("Gemini Live loop started for bot '%s'", bot_name)

    while True:
        session_count += 1
        system_instruction = _build_live_system_instruction(bot_name, structured_transcript)
        config = genai_types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            # input_audio_transcription intentionally omitted — transcription is
            # handled by the parallel _streaming_transcription_loop (VAD + Gemini
            # flash) so this session is dedicated to voice responses only.
            system_instruction=system_instruction,
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name="Aoede")
                )
            ),
        )

        try:
            logger.info("Gemini Live: opening session #%d", session_count)
            async with client.aio.live.connect(model=_LIVE_MODEL, config=config) as session:
                consecutive_failures = 0   # reset on any successful open
                session_start = time.monotonic()
                audio_buffer: bytearray = bytearray()
                turn_start_ts: float = (file_pos - _WAV_HEADER_SIZE) / _PCM_BYTES_PER_S
                _speak_task: asyncio.Task | None = None  # track active voice-response task

                async def _sender() -> None:
                    """Stream PCM chunks from the growing WAV file into the session."""
                    nonlocal file_pos
                    while True:
                        await asyncio.sleep(_LIVE_CHUNK_MS / 1000)
                        try:
                            if not os.path.exists(audio_path):
                                continue
                            file_size = os.path.getsize(audio_path)
                            safe_end = max(_WAV_HEADER_SIZE, file_size - _LIVE_GUARD_BYTES)
                            if safe_end <= file_pos:
                                continue
                            with open(audio_path, "rb") as f:
                                f.seek(file_pos)
                                chunk = f.read(min(safe_end - file_pos, _LIVE_CHUNK_BYTES * 4))
                            if not chunk:
                                continue
                            file_pos += len(chunk)
                            await session.send_realtime_input(
                                media=genai_types.Blob(
                                    data=chunk,
                                    mime_type="audio/pcm;rate=16000",
                                )
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.debug("Live sender error: %s", exc)

                async def _receiver() -> None:
                    """Consume voice responses from the Live model and play them back.

                    Transcription of participants' speech is handled by the separate
                    _streaming_transcription_loop (VAD + Gemini flash) — this receiver
                    only processes the model's AUDIO output turns.
                    """
                    nonlocal audio_buffer, turn_start_ts, _speak_task
                    import base64, io, wave

                    async for response in session.receive():
                        try:
                            sc = getattr(response, "server_content", None)
                            if sc is None:
                                continue

                            # Collect bot audio response from model turn
                            mt = getattr(sc, "model_turn", None)
                            if mt:
                                for part in (mt.parts or []):
                                    inl = getattr(part, "inline_data", None)
                                    if inl and getattr(inl, "data", None):
                                        raw = base64.b64decode(inl.data)
                                        audio_buffer.extend(raw)

                            # Turn complete: play bot audio response if any
                            if getattr(sc, "turn_complete", False):
                                # Update timestamp for next turn
                                turn_start_ts = (file_pos - _WAV_HEADER_SIZE) / _PCM_BYTES_PER_S

                                # Play audio response if bot was addressed
                                if audio_buffer:
                                    pcm_bytes = bytes(audio_buffer)
                                    audio_buffer = bytearray()
                                    if page is not None and mention_response_mode in ("voice", "both"):
                                        # Wrap PCM (24 kHz, 16-bit, mono) in WAV
                                        buf = io.BytesIO()
                                        with wave.open(buf, "wb") as wf:
                                            wf.setnchannels(1)
                                            wf.setsampwidth(2)
                                            wf.setframerate(24000)
                                            wf.writeframes(pcm_bytes)
                                        wav_bytes = buf.getvalue()
                                        import tempfile
                                        fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="bot_live_")
                                        os.close(fd)
                                        with open(tmp_path, "wb") as fh:
                                            fh.write(wav_bytes)
                                        logger.info(
                                            "Live: playing bot response (%d bytes PCM)", len(pcm_bytes)
                                        )
                                        # Record timestamp so _mention_monitor can avoid double-responding
                                        if last_live_response_at is not None:
                                            last_live_response_at[0] = time.monotonic()
                                        # Skip if previous voice response is still playing
                                        if _speak_task is not None and not _speak_task.done():
                                            logger.warning(
                                                "Live: previous voice response still playing — "
                                                "discarding overlapping response"
                                            )
                                            try:
                                                os.unlink(tmp_path)
                                            except OSError:
                                                pass
                                        else:
                                            _speak_task = asyncio.create_task(
                                                _speak_in_meeting(
                                                    page, platform, "",
                                                    start_muted=start_muted,
                                                    pulse_mic=pulse_mic,
                                                    pre_synthesized_path=tmp_path,
                                                )
                                            )
                                    else:
                                        audio_buffer = bytearray()

                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.debug("Live receiver message error: %s", exc)

                sender_task   = asyncio.create_task(_sender())
                receiver_task = asyncio.create_task(_receiver())

                # Run until session refresh time or task failure
                session_deadline = _LIVE_SESSION_S
                try:
                    done, pending = await asyncio.wait(
                        {sender_task, receiver_task},
                        timeout=session_deadline,
                        return_when=asyncio.FIRST_EXCEPTION,
                    )
                    for t in done:
                        exc = t.exception()
                        if exc:
                            logger.warning("Live task exception: %s", exc)
                finally:
                    sender_task.cancel()
                    receiver_task.cancel()
                    for t in (sender_task, receiver_task):
                        with contextlib.suppress(Exception):
                            await t

                logger.info(
                    "Gemini Live session #%d closed after %.0f s — refreshing context",
                    session_count, time.monotonic() - session_start,
                )

        except asyncio.CancelledError:
            logger.info("Gemini Live loop cancelled")
            raise
        except Exception as exc:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "Gemini Live: %d consecutive session failures — "
                    "voice responses disabled for the rest of the call. "
                    "Transcription continues via the parallel VAD loop. Last error: %s",
                    MAX_CONSECUTIVE_FAILURES, exc,
                )
                return  # VAD streaming transcription loop is always running in parallel
            logger.warning("Gemini Live session error: %s — retrying in 5 s", exc)
            await asyncio.sleep(5)


async def _mention_monitor(
    page: Page,
    platform: str,
    bot_name: str,
    mention_response_mode: str = "text",
    tts_provider: str = "edge",
    gemini_api_key: str | None = None,
    start_muted: bool = True,
    leave_event: asyncio.Event | None = None,
    live_transcript: list | None = None,
    structured_transcript: list | None = None,
    pulse_mic: str = PULSE_MIC_NAME,
    live_handles_audio: bool = False,
    last_live_response_at: list | None = None,
) -> None:
    """Coroutine that polls live captions AND chat messages, replying when the
    bot's name is mentioned in either source.

    When live_handles_audio=True (Gemini Live session is active), the audio
    transcript polling block is skipped — Gemini Live already handles voice
    detection and responses.  Only chat message polling remains active.

    structured_transcript is the shared list populated by _streaming_transcription_loop
    with {speaker, text, timestamp} dicts.  It grows throughout the meeting and is
    used as full meeting-history context when building mention responses, so the bot
    can answer questions about anything said since the call began.

    Designed to run concurrently with _wait_for_meeting_end and be cancelled when
    the meeting ends.
    """
    from app.services import intelligence_service as _intel

    seen_captions: str = ""
    seen_chat: str = ""
    last_response_at: float = 0.0
    caption_log: list[str] = []      # rolling buffer — last 60 caption chunks
    bot_name_lower = bot_name.lower()
    _poll_count = 0
    _empty_streak = 0
    last_audio_idx: int = 0   # tracks which live_transcript entries we've already checked
    logger.info("Mention monitor started for bot '%s' on %s", bot_name, platform)

    def _build_context(after_mention: str, source_label: str) -> str:
        """Build context for generate_mention_response using the full meeting
        transcript so the bot can answer questions about the entire meeting,
        not just the last few minutes.

        Priority:
        1. structured_transcript (full history with timestamps, from Session A)
        2. caption_log (rolling text buffer, fallback when transcription is off)
        """
        if structured_transcript:
            history_lines = [
                f"[{e.get('timestamp', 0):.0f}s] {e.get('speaker', '?')}: {e.get('text', '')}"
                for e in structured_transcript
            ]
            # Keep up to ~8 000 chars of history (roughly 30-45 min of speech)
            history = "\n".join(history_lines)
            if len(history) > 8000:
                history = "…(earlier content omitted)…\n" + history[-8000:]
            history_section = f"Full meeting transcript so far:\n{history}"
        elif caption_log:
            history_section = "Recent meeting captions:\n" + " ".join(caption_log)[-3000:]
        else:
            history_section = ""

        if after_mention:
            return (
                (history_section + "\n\n" if history_section else "") +
                f"[{source_label} to {bot_name}]: {after_mention}"
            )
        return history_section or " ".join(caption_log)[-3000:]

    # Open chat panel at startup so incoming messages are visible in the DOM.
    try:
        await _open_chat(page, platform)
        logger.info("Chat panel opened for mention monitoring on %s", platform)
    except Exception as exc:
        logger.warning("Could not open chat panel on %s: %s", platform, exc)

    async def _dispatch_reply(reply: str, source: str) -> None:
        """Send reply via the configured mode; source is 'caption' or 'chat'."""
        logger.info(
            "Mention detected via %s — responding (mode=%s): %s",
            source, mention_response_mode, reply,
        )
        try:
            if mention_response_mode == "voice":
                await _speak_in_meeting(page, platform, reply, tts_provider, gemini_api_key, start_muted, pulse_mic)
            elif mention_response_mode == "both":
                # TTS synthesis (pure HTTP) runs concurrently with chat typing
                # so that the audio file is ready by the time chat finishes.
                # We do NOT await here — synthesis happens in the background
                # while _send_chat_message occupies the browser with keyboard
                # events.  Only the final _speak_in_meeting call (unmute + play)
                # needs the browser; that still runs after chat completes, so
                # there is no keyboard-focus race.
                from app.services import tts_service as _tts_svc
                tts_task = asyncio.create_task(
                    _tts_svc.synthesize(reply, provider=tts_provider, api_key=gemini_api_key)
                )
                chat_ok = await _send_chat_message(page, platform, reply)
                if not chat_ok:
                    logger.warning("Bot mention chat reply failed (both mode)")
                pre_path = await tts_task   # likely already done while chat was sending
                await _speak_in_meeting(page, platform, reply, tts_provider, gemini_api_key, start_muted, pulse_mic, pre_synthesized_path=pre_path)
            else:  # "text" (default)
                success = await _send_chat_message(page, platform, reply)
                if not success:
                    logger.warning("Bot mention chat reply failed — see debug screenshot for DOM state")
        except Exception as exc:
            logger.warning("Mention response dispatch error: %s", exc)

    while True:
        await asyncio.sleep(0.3)   # 300 ms poll — faster mention detection (was 1 s)
        _poll_count += 1

        # ── Caption polling ───────────────────────────────────────────────────
        try:
            raw = await _scrape_captions(page, platform)
        except Exception as exc:
            logger.debug("Caption scrape error: %s", exc)
            raw = ""

        if not raw:
            _empty_streak += 1
            if _empty_streak == 30 or (_empty_streak > 30 and _empty_streak % 60 == 0):
                logger.warning(
                    "Mention monitor: no caption text in 30 polls — "
                    "attempting to re-enable captions"
                )
                try:
                    await _enable_captions(page, platform)
                except Exception as exc:
                    logger.warning("Caption re-enable failed: %s", exc)
                # Dump targeted DOM clues to diagnose stale selectors
                try:
                    dom_clues = await page.evaluate("""
                        () => {
                            const info = {};
                            // Caption-related buttons (regardless of pressed state)
                            info.captionBtns = Array.from(
                                document.querySelectorAll(
                                    'button[aria-label*="caption" i],'
                                    + 'button[aria-label*="subtitle" i],'
                                    + 'button[aria-label*="CC" i],'
                                    + 'button[jsname="r8qRAd"]'
                                )
                            ).map(el => ({
                                lbl:     el.getAttribute('aria-label'),
                                pressed: el.getAttribute('aria-pressed'),
                                jsname:  el.getAttribute('jsname'),
                                cls:     (el.className||'').slice(0,60),
                            }));
                            // All aria-live regions that contain text
                            info.ariaLive = Array.from(
                                document.querySelectorAll('[aria-live]')
                            ).map(el => ({
                                live: el.getAttribute('aria-live'),
                                tag:  el.tagName,
                                cls:  (el.className||'').slice(0,80),
                                text: (el.innerText||el.textContent||'').trim().slice(0,200),
                            })).filter(e => e.text);
                            // Known caption container jsnames
                            const captionJsnames = [
                                'tgaKEf','YSxPC','VUpckd','z1asCe','MuzmKe',
                                'CNusmb','iTTPOb','VbkSUe',
                            ];
                            info.knownCaptionEls = captionJsnames.map(jn => {
                                const el = document.querySelector('[jsname="' + jn + '"]');
                                return el ? {
                                    jsname: jn, tag: el.tagName,
                                    cls: (el.className||'').slice(0,60),
                                    text: (el.innerText||'').trim().slice(0,120),
                                } : null;
                            }).filter(Boolean);
                            // Elements in the bottom 35% of viewport
                            const bottomY = window.innerHeight * 0.65;
                            info.bottomViewport = Array.from(
                                document.querySelectorAll('div,span,p')
                            ).filter(el => {
                                const r = el.getBoundingClientRect();
                                return r.top >= bottomY && r.width > 30 && r.height > 8;
                            }).slice(0, 15).map(el => ({
                                tag:  el.tagName,
                                cls:  (el.className||'').slice(0,60),
                                text: (el.innerText||'').trim().slice(0,100),
                            })).filter(e => e.text);
                            return info;
                        }
                    """)
                    logger.info("Caption DOM clues: %s", dom_clues)
                except Exception as exc:
                    logger.debug("DOM clue dump failed: %s", exc)
        else:
            _empty_streak = 0

            # Diff captions: only the text that appeared since the last poll
            overlap = min(len(seen_captions), 100)
            if seen_captions and raw.startswith(seen_captions[:overlap]):
                new_caption_text = raw[len(seen_captions):]
            else:
                new_caption_text = raw
            seen_captions = raw

            # Log first 5 polls at INFO; then every 60 polls (~1 min) as heartbeat
            if _poll_count <= 5 or _poll_count % 60 == 0:
                logger.info("Caption sample (poll %d): %r", _poll_count, raw[:200])
            else:
                logger.debug("Caption poll %d: %r", _poll_count, raw[:120])

            if new_caption_text.strip():
                caption_log.append(new_caption_text.strip())
                caption_log = caption_log[-40:]

                if (
                    bot_name_lower in new_caption_text.lower()
                    and time.monotonic() - last_response_at >= 5
                ):
                    # Extract the specific request made AFTER the bot's name.
                    # This avoids feeding the model the full caption history as
                    # if it were a question — we focus on what was actually asked.
                    mention_pos = new_caption_text.lower().find(bot_name_lower)
                    after_mention = new_caption_text[mention_pos + len(bot_name_lower):].lstrip(" ,:!?-").strip()

                    # Leave command detection (before AI call)
                    if leave_event and any(kw in after_mention.lower() for kw in _LEAVE_KEYWORDS):
                        logger.info("Leave command detected via captions: %r", after_mention)
                        await _dispatch_reply("Understood, leaving the meeting now. Goodbye!", "caption")
                        await asyncio.sleep(2)
                        leave_event.set()
                        return

                    context = _build_context(after_mention, "Voice request")
                    uses_voice = mention_response_mode in ("voice", "both")
                    try:
                        reply = await _intel.generate_mention_response(
                            context, bot_name, for_voice=uses_voice, source="caption"
                        )
                    except Exception as exc:
                        logger.warning("generate_mention_response error: %s", exc)
                        reply = ""
                    if reply:
                        last_response_at = time.monotonic()
                        await _dispatch_reply(reply, "caption")

        # ── Chat polling (every 2 s — every other caption poll) ──────────────
        if _poll_count % 2 == 0:
            try:
                chat_raw = await _scrape_chat_messages(page, platform)
            except Exception as exc:
                logger.debug("Chat scrape error: %s", exc)
                chat_raw = ""

            if chat_raw and chat_raw != seen_chat:
                if not seen_chat:
                    # First poll — bootstrap without treating existing history as new.
                    # Without this, any old message that mentions the bot name would
                    # immediately trigger a false response on join.
                    seen_chat = chat_raw
                    logger.debug("Chat bootstrapped (%d chars)", len(chat_raw))
                    new_chat_text = ""
                else:
                    # Diff: extract only new content
                    overlap = min(len(seen_chat), 200)
                    if chat_raw.startswith(seen_chat[:overlap]):
                        new_chat_text = chat_raw[len(seen_chat):]
                    else:
                        # Chat panel re-rendered (e.g. scroll, DOM refresh).
                        # Don't re-process the entire history — skip this cycle.
                        seen_chat = chat_raw
                        new_chat_text = ""
                    seen_chat = chat_raw

                if new_chat_text.strip():
                    logger.debug("Chat update: %r", new_chat_text[:200])
                    if (
                        bot_name_lower in new_chat_text.lower()
                        and time.monotonic() - last_response_at >= 5
                    ):
                        # Extract text after the bot name in the chat message
                        mention_pos = new_chat_text.lower().find(bot_name_lower)
                        after_mention = new_chat_text[mention_pos + len(bot_name_lower):].lstrip(" ,:!?-").strip()

                        # Leave command detection (before AI call)
                        if leave_event and any(kw in after_mention.lower() for kw in _LEAVE_KEYWORDS):
                            logger.info("Leave command detected via chat: %r", after_mention)
                            await _dispatch_reply("Understood, leaving the meeting now. Goodbye!", "chat")
                            await asyncio.sleep(2)
                            leave_event.set()
                            return

                        # Use full meeting transcript as context so the bot can
                        # answer questions about anything discussed in the meeting.
                        chat_question = after_mention or new_chat_text.strip()[-1500:]
                        context = _build_context(chat_question, "Chat message")
                        uses_voice = mention_response_mode in ("voice", "both")
                        try:
                            reply = await _intel.generate_mention_response(
                                context, bot_name, for_voice=uses_voice, source="chat"
                            )
                        except Exception as exc:
                            logger.warning("generate_mention_response error: %s", exc)
                            reply = ""
                        if reply:
                            last_response_at = time.monotonic()
                            await _dispatch_reply(reply, "chat")

        # ── Audio transcript polling (from _streaming_transcription_loop) ──────
        # Provides voice bot-name detection and meeting context when DOM captions
        # are unavailable.  When Gemini Live is active, this still runs as a
        # fallback — but only dispatches a reply if Gemini Live hasn't responded
        # recently (avoids double responses).
        if live_transcript is not None:
            new_chunks = live_transcript[last_audio_idx:]
            if new_chunks:
                last_audio_idx = len(live_transcript)
                # Merge into caption_log so all context-building paths benefit
                caption_log.extend(new_chunks)
                caption_log = caption_log[-60:]
                new_audio_text = " ".join(new_chunks)

                if (
                    bot_name_lower in new_audio_text.lower()
                    and time.monotonic() - last_response_at >= 5
                ):
                    # When Gemini Live is active, skip if it already responded
                    # recently (within 10 s) to avoid double responses.
                    _live_resp_ts = (last_live_response_at[0] if last_live_response_at else 0.0)
                    if live_handles_audio and time.monotonic() - _live_resp_ts < 10:
                        logger.debug(
                            "Audio mention detected but Gemini Live responded %.1f s ago — skipping fallback",
                            time.monotonic() - _live_resp_ts,
                        )
                    else:
                        if live_handles_audio:
                            logger.info(
                                "Audio mention detected — Gemini Live did not respond, using fallback"
                            )
                        mention_pos   = new_audio_text.lower().find(bot_name_lower)
                        after_mention = new_audio_text[mention_pos + len(bot_name_lower):].lstrip(" ,:!?-").strip()

                        # Leave command check
                        if leave_event and any(kw in after_mention.lower() for kw in _LEAVE_KEYWORDS):
                            logger.info("Leave command detected via audio: %r", after_mention)
                            await _dispatch_reply("Understood, leaving the meeting now. Goodbye!", "audio")
                            await asyncio.sleep(2)
                            leave_event.set()
                            return

                        context = _build_context(after_mention, "Voice request")
                        uses_voice = mention_response_mode in ("voice", "both")
                        try:
                            reply = await _intel.generate_mention_response(
                                context, bot_name, for_voice=uses_voice, source="caption"
                            )
                        except Exception as exc:
                            logger.warning("generate_mention_response error: %s", exc)
                            reply = ""
                        if reply:
                            last_response_at = time.monotonic()
                            await _dispatch_reply(reply, "audio")


async def _wait_for_meeting_end(
    page: Page,
    platform: str,
    max_s: int,
    alone_timeout_s: int = 300,
    participants: set | None = None,
    pulse_sink: str = PULSE_SINK_NAME,
    pulse_mic: str = PULSE_MIC_NAME,
    pulse_source: str | None = None,
) -> str:
    """
    Wait until the meeting ends, the max duration is reached, or the bot has
    been the only participant for alone_timeout_s consecutive seconds.

    Returns one of: "ended" | "max_duration" | "alone_timeout"
    """
    end_texts = _END_TEXTS.get(platform, [])
    deadline  = time.monotonic() + max_s

    alone_since: Optional[float] = None
    _last_participant_scrape = 0.0
    _last_audio_routing_sync = 0.0   # tracks last PulseAudio re-routing

    while time.monotonic() < deadline:
        try:
            body = (await page.inner_text("body")).lower()
            if any(t in body for t in end_texts):
                logger.info("Meeting end detected (%s)", platform)
                return "ended"
        except Exception:
            logger.info("Page inaccessible — meeting likely ended")
            return "ended"

        now = time.monotonic()

        # ── PulseAudio routing sync (every 15s) ───────────────────────────
        # Re-route Chrome's audio output to our recording sink and its
        # microphone to our TTS virtual source.  Run in a thread so the
        # sync pactl calls don't block the asyncio event loop.
        if now - _last_audio_routing_sync >= 15:
            _last_audio_routing_sync = now
            await asyncio.get_event_loop().run_in_executor(
                None, functools.partial(_sync_chrome_audio_routing, pulse_sink, pulse_mic, pulse_source)
            )

        # ── Participant name scraping (every 30s) ─────────────────────────
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
    respond_on_mention: bool = True,
    mention_response_mode: str = "text",
    tts_provider: str = "edge",
    start_muted: bool = False,
    live_transcription: bool = False,
    on_live_transcript_entry=None,
    gemini_api_key: str = "",
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
        on_admitted:             Optional async callback fired when the bot is let in.
        on_live_transcript_entry: Optional async callable(entry) invoked after each
                                  structured transcript entry is created during the
                                  meeting — use for real-time DB saves.

    Returns:
        {"success", "audio_path", "error", "admitted", "duration_seconds",
         "exit_reason", "participants", "live_transcript"}
        live_transcript: list of {speaker, text, timestamp} dicts produced by
        the streaming VAD loop (may be empty if Gemini key is absent).
    """
    pulse_idx:          Optional[str] = None
    pulse_mic_idx:      Optional[str] = None   # null-sink module index
    pulse_mic_virt_idx: Optional[str] = None   # virtual-source module index
    ffmpeg_proc:   Optional[subprocess.Popen] = None
    xvfb_proc:     Optional[subprocess.Popen] = None
    t0 = time.monotonic()

    # ── Per-bot unique PulseAudio sink names ────────────────────────────────
    # Each concurrent bot gets its own named sinks so they don't interfere with
    # each other's recording or TTS mic streams.
    _short_id = Path(audio_path).stem.replace("-", "")[:10]
    pulse_sink = f"mbot_{_short_id}"
    pulse_mic  = f"mbot_mic_{_short_id}"
    # Will be set to the virtual source name after _create_pulse_mic() succeeds
    pulse_source_name: str = f"{pulse_mic}.monitor"

    # ── Infrastructure ──────────────────────────────────────────────────────
    pulse_ok = _start_pulseaudio()
    if pulse_ok:
        pulse_idx = _create_pulse_sink(pulse_sink)
        if pulse_idx:
            ffmpeg_proc = _start_ffmpeg(audio_path, pulse_sink)
        # Create the TTS mic sink + virtual source so the bot can speak.
        # Must be created before Chrome launches so PULSE_SOURCE takes effect.
        pulse_mic_idx, pulse_mic_virt_idx, pulse_source_name = _create_pulse_mic(pulse_mic)

    xvfb_proc, xvfb_display = _start_xvfb()
    headless = xvfb_proc is None   # fall back to headless if no Xvfb

    # Start with the full current process environment so Chrome inherits PATH,
    # HOME, XDG_RUNTIME_DIR, etc.  We then overlay our PulseAudio overrides.
    env: dict = dict(os.environ)
    if xvfb_proc:
        env["DISPLAY"] = xvfb_display
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
        env["PULSE_SINK"]   = pulse_sink
        # Point Chrome's microphone at the virtual source (backed by the TTS
        # null-sink monitor).  module-virtual-source exposes it as a real input
        # device so Chrome's getUserMedia succeeds and Google Meet shows the mic
        # as available.  Falls back to the monitor name if virt source failed.
        env["PULSE_SOURCE"] = pulse_source_name
        # Disable PipeWire so Chrome uses PulseAudio directly.
        # On systems with both installed, Chrome may prefer PipeWire which
        # ignores PULSE_SINK/PULSE_SOURCE env vars, causing silent recording.
        env["PIPEWIRE_RUNTIME_DIR"] = ""
        env["DISABLE_RTKIT"] = "1"

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
        # Ensure Chrome's WebRTC uses ALSA/PulseAudio, not a fake device.
        # WebRtcPipeWireCapture: force PulseAudio for mic capture — prevents
        # Chrome from using PipeWire (which bypasses our virtual null-sink mic)
        # so that source-outputs appear in `pactl list source-outputs` and TTS
        # audio routed into meetingbot_mic is captured by WebRTC.
        # AudioServiceOutOfProcess: disabled so Chrome's audio runs in-process,
        # ensuring it inherits PULSE_SINK/PULSE_SERVER from the main browser
        # process.  When the audio service runs out-of-process its subprocess
        # may not see our custom env vars, causing it to output to a different
        # device (ALSA default or /dev/null) and produce silence.
        "--disable-features=WebRtcHideLocalIpsWithMdns,WebRtcPipeWireCapture,AudioServiceOutOfProcess",
        "--enforce-webrtc-ip-permission-check=false",
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
            env=env,
        )
        ctx: BrowserContext = await browser.new_context(
            user_agent=_USER_AGENT,
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            timezone_id="America/New_York",
        )

        # Intercept RTCPeerConnection at the page level (before Google Meet
        # initialises its WebRTC) so that every incoming audio track is
        # explicitly connected to a Web Audio context.  This forces Chrome to
        # decode and render the incoming WebRTC audio through its normal audio
        # output pipeline (→ PulseAudio null-sink) even in an automated/Xvfb
        # environment where Chrome might otherwise suppress rendering.
        await ctx.add_init_script("""
            (function () {
                if (window._mbRtcAudioForced) return;
                window._mbRtcAudioForced = true;

                var _AudioCtx = window.AudioContext || window.webkitAudioContext;
                window._mbAudioCtx = null;
                function getCtx() {
                    if (!window._mbAudioCtx) {
                        window._mbAudioCtx = new _AudioCtx({ latencyHint: 'playback' });
                        window._mbAudioCtx.resume().catch(function(){});
                    }
                    return window._mbAudioCtx;
                }

                var _OrigRTC = window.RTCPeerConnection;
                function PatchedRTC() {
                    var pc = new (Function.prototype.bind.apply(
                        _OrigRTC, [null].concat(Array.prototype.slice.call(arguments))
                    ))();
                    pc.addEventListener('track', function (e) {
                        if (e.track.kind !== 'audio') return;
                        try {
                            var stream = (e.streams && e.streams[0])
                                ? e.streams[0]
                                : new MediaStream([e.track]);
                            var ctx = getCtx();
                            var src = ctx.createMediaStreamSource(stream);
                            src.connect(ctx.destination);
                        } catch (_) {}
                    });
                    return pc;
                }
                PatchedRTC.prototype = _OrigRTC.prototype;
                Object.setPrototypeOf(PatchedRTC, _OrigRTC);
                window.RTCPeerConnection = PatchedRTC;
            })();
        """)

        page = await ctx.new_page()
        await _apply_stealth(page)

        transcription_task: asyncio.Task | None = None   # VAD streaming transcription
        voice_task:         asyncio.Task | None = None   # Gemini Live voice responses
        monitor_task:       asyncio.Task | None = None

        try:
            logger.info(
                "Browser bot starting: %s  platform=%s  name='%s'",
                meeting_url, platform, bot_name,
            )

            if platform == "google_meet":
                await _join_google_meet(page, meeting_url, bot_name, start_muted=start_muted)
            elif platform == "zoom":
                await _join_zoom(page, meeting_url, bot_name, start_muted=start_muted)
            elif platform == "microsoft_teams":
                await _join_teams(page, meeting_url, bot_name, start_muted=start_muted)
            else:
                raise MeetingBotError(f"Unsupported platform: {platform}")

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

            # Post-admission mic check: meeting platforms sometimes auto-mute on
            # admit. When start_muted=False, ensure the mic is actually on now
            # that the in-call toolbar is fully rendered.
            if not start_muted and platform == "google_meet":
                try:
                    await asyncio.sleep(0.5)  # let the toolbar settle
                    muted = await page.evaluate("""
                        () => {
                            const off = document.querySelector('button[aria-label*="Turn on microphone" i]');
                            if (off) return true;
                            const on = document.querySelector('button[aria-label*="Turn off microphone" i]');
                            if (on) return false;
                            return null;
                        }
                    """)
                    if muted is True:
                        await page.keyboard.press("Control+d")
                        logger.info("Google Meet: post-admit Ctrl+D to unmute (was muted after admit)")
                    elif muted is False:
                        logger.info("Google Meet: mic confirmed ON after admit")
                    else:
                        logger.debug("Google Meet: mic state unknown post-admit")
                except Exception as exc:
                    logger.debug("Post-admit mic check failed: %s", exc)

            # Immediately sync PulseAudio routing: move Chrome's audio output to
            # meetingbot_sink (so ffmpeg captures meeting audio) and Chrome's mic
            # input to meetingbot_mic.monitor (so TTS is heard by participants).
            # The periodic sync in _wait_for_meeting_end handles subsequent drifts.
            await asyncio.sleep(0.8)  # let Chrome's WebRTC streams fully start (was 2.0 s)
            await asyncio.get_event_loop().run_in_executor(
                None, functools.partial(_sync_chrome_audio_routing, pulse_sink, pulse_mic, pulse_source_name)
            )

            # Unmute all audio/video elements in the page.  In headless/automated
            # Chrome, Google Meet may leave its WebRTC audio output element muted
            # (or at zero volume), causing the PulseAudio sink-input to carry
            # silence even though WebRTC packets are received.  Force-playing with
            # volume=1 ensures Chrome actually pushes PCM samples to PulseAudio.
            try:
                _audio_ctx_state = await page.evaluate("""
                    async () => {
                        document.querySelectorAll('audio, video').forEach(el => {
                            el.muted  = false;
                            el.volume = 1.0;
                            el.play().catch(() => {});
                        });
                        // Explicitly resume the RTCPeerConnection audio context so
                        // incoming WebRTC audio is rendered to PulseAudio.  Chrome
                        // may suspend AudioContexts until a user gesture; the flag
                        // --autoplay-policy=no-user-gesture-required helps but an
                        // explicit resume() here is belt-and-suspenders.
                        if (window._mbAudioCtx) {
                            try { await window._mbAudioCtx.resume(); } catch(e) {}
                            return window._mbAudioCtx.state;
                        }
                        return 'no-audio-ctx-yet';
                    }
                """)
                logger.info("Audio elements unmuted in page (AudioContext state: %s)", _audio_ctx_state)
            except Exception as exc:
                logger.debug("Audio unmute injection failed: %s", exc)

            # Enable live captions so the mention monitor can read them
            if respond_on_mention:
                try:
                    await _enable_captions(page, platform)
                    logger.info("Live captions enabled on %s", platform)
                except Exception as exc:
                    logger.warning("Could not enable captions on %s: %s", platform, exc)

            _participants: set[str] = set()

            # Shared live transcript lists:
            #   live_transcript       — plain-text chunks for _mention_monitor
            #   structured_transcript — {speaker, text, timestamp} dicts for DB
            live_transcript: list = []
            structured_transcript: list = []

            from app.config import settings as _s_cfg
            _key = gemini_api_key or _s_cfg.GEMINI_API_KEY or ""
            _gemini_available = bool(_key) and ffmpeg_proc is not None

            # ── Two-session architecture ───────────────────────────────────────
            # Session A — VAD + Gemini flash transcription  (always, when needed)
            #   Reads the growing WAV file, detects utterances via energy-based
            #   VAD, sends each utterance to Gemini 2.5-flash for transcription,
            #   fires on_live_transcript_entry so entries are persisted to DB and
            #   broadcast to the frontend in real-time.
            #
            # Session B — Gemini Live voice response  (voice/both mode only)
            #   Streams raw PCM into a Gemini Live session (native audio model).
            #   The session produces AUDIO-only responses when the bot is addressed.
            #   Transcription is NOT requested from this session — Session A owns that.
            #   Because these are two independent API calls, a failure in Session B
            #   does not affect transcription.
            #
            # This avoids the old single-session design where a Live session failure
            # silently killed transcription for up to 15 s before the fallback ran.

            # ── Session A: VAD streaming transcription ─────────────────────────
            # Run the VAD loop whenever Gemini is available, regardless of
            # respond_on_mention / live_transcription settings.  The loop serves
            # two purposes:
            #   1. Populate live_transcript for voice-based mention detection
            #      (used by _mention_monitor when respond_on_mention=True).
            #   2. Populate structured_transcript as a fallback in case batch
            #      Gemini transcription after the meeting is sparse or fails.
            # Without this, external callers that set respond_on_mention=False
            # (pure transcription bots) receive no voice detection during the
            # call and lose the live-transcript fallback.
            #
            # Real-time DB saves via on_live_transcript_entry are only triggered
            # when live_transcription=True or respond_on_mention=True so the API
            # contract ("audio only transcribed after meeting when both are False")
            # is preserved.
            if _gemini_available:
                logger.info(
                    "Starting VAD streaming transcription loop for bot '%s'", bot_name
                )
                transcription_task = asyncio.create_task(
                    _streaming_transcription_loop(
                        audio_path,
                        live_transcript,
                        structured_transcript,
                        on_transcript_entry=(
                            on_live_transcript_entry
                            if (live_transcription or respond_on_mention)
                            else None
                        ),
                    )
                )

            # ── Session B: Gemini Live voice responses ─────────────────────────
            # Only activated when the user chose voice or both response mode AND
            # respond_on_mention is enabled.
            _use_live = (
                _gemini_available
                and respond_on_mention
                and mention_response_mode in ("voice", "both")
            )
            if _use_live:
                try:
                    import google.genai  # noqa: F401
                except ImportError:
                    logger.warning(
                        "google-genai not installed — voice responses disabled. "
                        "Run: pip install 'google-genai>=1.0.0'. "
                        "Transcription continues via VAD loop."
                    )
                    _use_live = False

            # Shared mutable timestamp: [monotonic_time] updated by _gemini_live_loop
            # when it plays an audio response — used by _mention_monitor to avoid
            # double-responding when Gemini Live already answered.
            _last_live_response_at: list = [0.0]

            if _use_live:
                logger.info(
                    "Starting Gemini Live voice loop for bot '%s' (mode=%s) — "
                    "transcription handled by parallel VAD loop",
                    bot_name, mention_response_mode,
                )
                voice_task = asyncio.create_task(
                    _gemini_live_loop(
                        audio_path=audio_path,
                        bot_name=bot_name,
                        live_transcript=live_transcript,
                        structured_transcript=structured_transcript,
                        mention_response_mode=mention_response_mode,
                        page=page,
                        platform=platform,
                        start_muted=start_muted,
                        pulse_mic=pulse_mic,
                        gemini_api_key=_key,
                        last_live_response_at=_last_live_response_at,
                    )
                )

            # Run mention monitor concurrently with the meeting-end watcher.
            # When Gemini Live is active, the monitor also checks live_transcript
            # as a fallback (in case Gemini Live doesn't respond to a mention).
            leave_event = asyncio.Event()
            if respond_on_mention:
                monitor_task = asyncio.create_task(
                    _mention_monitor(
                        page, platform, bot_name,
                        mention_response_mode=mention_response_mode,
                        tts_provider=tts_provider,
                        gemini_api_key=_key or None,
                        start_muted=start_muted,
                        leave_event=leave_event,
                        live_transcript=live_transcript,
                        structured_transcript=structured_transcript,
                        pulse_mic=pulse_mic,
                        live_handles_audio=_use_live,
                        last_live_response_at=_last_live_response_at,
                    )
                )

            # Race: natural meeting end vs explicit leave command from a participant
            end_task   = asyncio.create_task(
                _wait_for_meeting_end(
                    page, platform, max_duration, alone_timeout, _participants,
                    pulse_sink=pulse_sink, pulse_mic=pulse_mic, pulse_source=pulse_source_name,
                )
            )
            leave_task = asyncio.create_task(leave_event.wait())
            done, pending = await asyncio.wait(
                {end_task, leave_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

            if leave_task in done:
                exit_reason = "leave_command"
                logger.info("Bot leaving meeting on participant command")
            else:
                exit_reason = end_task.result()

            # Cancel background tasks cleanly when the meeting ends
            if transcription_task is not None:
                transcription_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await transcription_task
            if voice_task is not None:
                voice_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await voice_task
            if monitor_task is not None:
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor_task

            # Gracefully leave the call (best-effort, bot may already be removed)
            if exit_reason not in ("ended",):
                with contextlib.suppress(Exception):
                    await _leave_meeting(page, platform)

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
                "live_transcript": list(structured_transcript),
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
            # Always cancel background tasks (handles external cancellation of run_browser_bot)
            if transcription_task is not None and not transcription_task.done():
                transcription_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await transcription_task
            if voice_task is not None and not voice_task.done():
                voice_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await voice_task
            if monitor_task is not None and not monitor_task.done():
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await monitor_task
            await browser.close()
            if ffmpeg_proc:
                _stop_ffmpeg(ffmpeg_proc)
            if pulse_idx:
                _unload_pulse_sink(pulse_idx)
            if pulse_mic_virt_idx:
                _unload_pulse_sink(pulse_mic_virt_idx)
            if pulse_mic_idx:
                _unload_pulse_sink(pulse_mic_idx)
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
