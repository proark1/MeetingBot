# Changelog

All notable changes to JustHereToListen.io are documented here.

Format: `## [version] - YYYY-MM-DD` followed by categorised bullet points.

> **Latest version:** 2.41.0 — **Last updated:** 2026-04-29

---

## [2.41.0] - 2026-04-29

### Fixed (revenue / billing correctness)
- **Stripe `customer.subscription.deleted` was a no-op** — `account.stripe_subscription_id` was never written at checkout time, so the cancellation handler's lookup always missed and customers kept their paid plan forever. The `checkout.session.completed` branch now persists `session.subscription` alongside the customer id.
- **Stripe `invoice.paid` didn't reset usage on renewal** — paid customers hit their plan ceiling on day 31 because `monthly_bots_used` and `monthly_reset_at` were only set at initial subscription. The renewal handler now zeros the counter and advances `monthly_reset_at` to the invoice's `period.end` (or +30 days as fallback).

### Security
- **Cross-tenant export / MCP IDOR via legacy bots** — the `bot.account_id is not None` short-circuit pattern that round-1 fixed in `api/bots._get_or_404` survived in `api/exports.py:44` and `services/mcp_service.py` (3 sites). Tightened all four to strict equality so `account_id IS NULL` bots are no longer visible to authenticated tenants.
- **Stripe webhook plan/price cross-check (defense in depth)** — `checkout.session.completed` now verifies the line-item `price_id` matches the claimed `metadata.plan` against the configured `STRIPE_<PLAN>_PRICE_ID` envs when line items are expanded. Best-effort: silently skips when Stripe didn't expand line items.
- **Support keys + share tokens use HMAC-SHA256 instead of bare SHA-256** — new tokens are stored as `h2:<hmac-sha256>` peppered with `JWT_SECRET`, so a DB-only leak can no longer be correlated against plaintext keys appearing in headers/logs. Verification accepts both formats so already-issued tokens keep working until they expire/are revoked. `support_keys.key_hash` widened to `VARCHAR(128)`.
- **Dev-mode auth fail-closed when accounts exist** — at startup, if `API_KEY` is unset and at least one `Account` row exists, the auth dependency now requires a Bearer token even in dev mode. Previously, a missing-Bearer request resolved to `account_id=None` and silently bypassed every per-tenant ownership check. New `ALLOW_UNAUTHENTICATED_DEV_MODE=true` setting restores the legacy behaviour for local prototyping.

### Reliability
- **`/share/{token}` survives the 24-hour memory eviction window** — added indexed `share_token_hash` + `share_token_expires_at` columns to `bot_snapshots` (migration shipped). The route now first matches in-memory bots, then falls back to a single indexed `BotSnapshot` query. Shared meeting links no longer 404 the day after the meeting.
- **`/share/{token}` rate-limited (60/min) and supports expiry** — `POST /api/v1/bot/{id}/share` accepts an optional `expires_in_hours`; the token is rejected after that window. Hashes are peppered (see above).
- **Untracked fire-and-forget `asyncio.create_task` calls** — audit logs in `api/auth.py` (×4) and `api/bots.py` (×3 + chat task), the per-bot extra webhook in `services/webhook_service.py`, and the long-lived USDC monitor loop in `services/crypto_service.py` now go through a shared `services.background_tasks.tracked_task` helper. Prevents CPython from garbage-collecting an in-flight task and silently dropping work. The shutdown lifespan still drains via `cancel_all_tracked_tasks()`.
- **JWT_SECRET persists across restarts in non-prod environments** — when `JWT_SECRET` is the default and `ENVIRONMENT != production`, a generated value is now written to `./jwt_secret.local` (mode `0600`) and re-loaded on subsequent boots. Stops Railway preview deploys from logging every user out on each restart.

### Migrations (idempotent)
- `bot_snapshots`: add `share_token_hash VARCHAR(128)` + index, add `share_token_expires_at`.
- `support_keys`: widen `key_hash` to `VARCHAR(128)` (PostgreSQL only — SQLite VARCHAR length is advisory).

## [2.40.0] - 2026-04-29

### Security
- **Webhook tenant scoping** — Webhook registrations are now scoped to the authenticated account. The `webhooks` table gained an `account_id` column (migration was already in place), and `WebhookEntry`/the API filter every read, write and delivery by the originating account. Previously, any authenticated tenant could list, modify, delete or read delivery logs for every other tenant's webhooks. Legacy webhooks with `account_id IS NULL` are treated as superadmin globals and remain visible to the legacy API_KEY only.
- **DNS-rebinding / TOCTOU SSRF on webhook delivery** — `_attempt_delivery` now re-resolves the URL host on every send and rejects private/loopback/cloud-metadata IPs (AWS IMDS at 169.254.169.254 and friends). Registration-time validation alone allowed an attacker to register a benign domain and later flip its DNS to an internal address. The shared rule set lives in `webhook_service.check_url_ssrf` and is reused by the registration handler.
- **`_get_or_404` ownership check** — Removed the `bot.account_id is not None` short-circuit so legacy bots whose `account_id` is `NULL` are no longer visible to authenticated tenants.
- **Email HTML escaping** — `meeting_url`, `bot_id` and `platform` are now passed through `html.escape` before interpolation in done-emails and the weekly digest. Prevents stored-HTML payloads in user-supplied meeting URLs from rendering in recipient mailboxes.

### Changed
- **Webhook delivery retry classification (BREAKING for clients depending on prior behaviour)** — 4xx responses other than 408/425/429 are now classified as permanent receiver rejections: logged as `failed`, never retried, and counted toward the consecutive-failure auto-disable threshold. 2xx/3xx are success; 5xx, 408, 425, 429 and connection-level errors retry with the existing exponential back-off. Previously every status `< 500` was treated as success — including 429s, which silently dropped events whenever a recipient was rate-limiting.
- **Webhook retry loop locking** — `_process_retries` now takes the per-webhook lock for the full read→deliver→update→persist iteration, mirroring the initial dispatch path. `consecutive_failures` increments on every failed attempt (initial + every retry), so a permanently-broken endpoint will now auto-disable as designed.
- **Audit log task tracking** — Fire-and-forget `asyncio.create_task(_audit(...))` calls now keep a strong reference in a module-level set, so tasks can't be garbage-collected mid-await and exceptions/log writes aren't silently dropped.
- **`Store.update_bot` immutable-field guard** — Raises `ValueError` if a caller tries to mutate `id`, `account_id`, `sub_user_id`, `created_at`, `meeting_url`, or `meeting_platform`. These are set at create time only.
- **Brand consistency** — Top-of-README, top-of-CHANGELOG and the `SMTP_FROM_ADDRESS` example now read "JustHereToListen.io" instead of "MeetingBot", per CLAUDE.md project identity.

## [2.39.0] - 2026-04-17

### Fixed
- **onepizza: silent recordings — definitive fix by bypassing PulseAudio entirely** (industry-standard Recall.ai pattern). The v2.38.0 swap from AudioContext → hidden `<audio autoplay>` element produced the expected page-side state (`audio_sinks: [{paused:false, readyState:4, currentTime: advancing}]`, WebRTC `total_audio_energy` climbing 0.10 → 3.55 over 60 s), but ffmpeg STILL recorded `peak_amp:0` and `audio_health` logged `SILENT` every 15 s. Both AudioContext and HTMLMediaElement output paths produce silence to PulseAudio in headless+Xvfb Chromium for remote WebRTC streams — the issue is below the JS layer, in Chrome's audio renderer / Xvfb interaction.
- **Fix in `backend/app/services/browser_bot.py`**: capture audio inside the page using `MediaRecorder` on the remote `MediaStream`, ship encoded chunks back to Python via Playwright `expose_function`, write them to disk, then ffmpeg-decode at end-of-meeting and use the resulting WAV as the source-of-truth recording. This is the documented Recall.ai approach (*"access a MediaStream object and its audio track from the webpage running inside the bot, and get samples of the meeting audio"*) and bypasses Chrome's audio output device, the `--use-fake-device-for-media-stream` debate, the AudioContext-on-remote-stream Chromium bug, the `<audio autoplay>` autoplay-policy debate, and the entire PulseAudio routing pipeline.

### Added
- **In-page MediaRecorder**: a new `_mbStartOrUpdateRecorder()` block in the audio-attach init script. When the first remote audio track arrives, creates a shared `MediaStream`, picks the best supported mime (`audio/webm;codecs=opus` → `audio/webm` → `audio/ogg;codecs=opus` → default), starts MediaRecorder at 64 kbps with `start(2000)` (2 s chunks), and on `ondataavailable` base64-encodes the blob and calls back to Python via `window._mbOnAudioBlob(b64, seq, size)`. If a second remote audio track appears mid-call, the recorder is stopped and restarted on the combined stream. Stop entry point exposed as `window._mbStopAudioRecorder()` so Python can flush the last chunk before tearing down.
- **`page.expose_function('_mbOnAudioBlob', ...)`** registered on the BrowserContext. Receives `(b64, seq, size)`, base64-decodes, appends to `{audio_path}.remote.webm`. Logs `remote-audio: first MediaRecorder chunk received seq=N size=M` on first chunk and a counter every 15 chunks.
- **Post-meeting WebM → WAV decode + swap** in the cleanup `finally` block: stops the recorder via `page.evaluate(window._mbStopAudioRecorder)`, sleeps 1.5 s for the final flush, closes the file, then runs `ffmpeg -i {webm} -ac 1 -ar 16000 -acodec pcm_s16le {wav}`. Peak-amplitude probes the result; if `peak ≥ 200/32767` it `os.replace`s the silent PulseAudio WAV with the MediaRecorder-derived one. Logs:
  - `remote-audio: ✓ MediaRecorder WAV adopted as recording (peak=NNNN/32767, replaced PulseAudio WAV)` — success
  - `remote-audio: MediaRecorder WAV has peak=N/32767 (<200), KEEPING PulseAudio WAV` — both silent
  - `remote-audio: no MediaRecorder data captured (webm size=N)` — recorder never produced data
- **Safety**: the PulseAudio path is preserved as a fallback. Other platforms (Meet, Zoom, Teams) where PulseAudio capture historically worked aren't disturbed — the WAV is only swapped when the MediaRecorder-derived file actually has signal.

### JS-side logs added (visible via console_log_tail)
- `[mb-rec] MediaRecorder started mime=audio/webm;codecs=opus tracks=1`
- `[mb-rec] restarting recorder to include new track …` (multi-party mid-call)
- `[mb-rec] start failed: …` (MediaRecorder API problem)
- `[mb-rec] callback failed: …` (expose_function mismatch)
- `[mb-rec] recorder stopped state=…` (clean shutdown)

---

## [2.38.0] - 2026-04-17

### Fixed
- **onepizza: silent recordings were caused by a Chromium Web Audio bug, not audio routing or join logic** — confirmed by the v2.37.0 webrtc-stats diagnostic. Bot `edb8af4d…` (13:49 UTC) showed: `connection_state: "connected"`, `ice_connection_state: "connected"`, `signaling_state: "stable"` (v2.37.1 join fix is working), `inbound_audio.packets` climbing 639 → 3640 over 60 s, `inbound_audio.total_audio_energy` climbing 0.92 → 3.37 (**non-zero energy = Chrome IS decoding actual non-silent samples**), onepizza's active-speaker border flickering green (its own VAD sees incoming audio — matches user report of "frame flickering green"), the Chromium sink-input correctly attached to our null-sink with non-zero `Buffer Latency: 34058 usec`, and `<video>` elements with `paused: false muted: false volume: 1 ready_state: 4 audio_tracks: 1 current_time` advancing. And yet `peak_amp: 0.0` in the WAV and onepizza's caption UI reporting "No sound detected — check your mic".
- **Root cause**: the init script `_mbRtcAudioForced` (added in 2.33.x) was doing `AudioContext.createMediaStreamSource(remoteStream).connect(ctx.destination)` for every remote audio track to "force" Chromium to render audio. That API path is a long-standing Chromium bug for remote WebRTC streams — Chrome silences the audio pipeline when a remote `MediaStreamTrack` is taken into Web Audio (chromium#121673, w3c/webrtc-pc#2564 and multiple community reports: *"when this code was added to the flow, Chrome stopped generating all WebRTC audio, though it worked on Firefox"*). Our own shim was the thing producing the silence.
- **Fix in `backend/app/services/browser_bot.py`**: replaced the AudioContext-based `connectTrack()` with `attachAudioTrack()`, which creates a hidden `<audio autoplay srcObject=new MediaStream([track])>` element for each remote audio track inside a fixed-position off-viewport `<div id="_mb_audio_sinks">`. This is the WebRTC-samples canonical pattern and routes through Chrome's HTMLMediaElement pipeline, which respects `PULSE_SINK` and actually emits real samples to our null-sink. `createMediaStreamSource` is no longer called anywhere on remote tracks. Kept: the RTCPeerConnection constructor wrapping, `setRemoteDescription` receiver-sweep, `track`/`addstream` event listeners, and the periodic 3 s sweep of `window.__mbRtcPcs` for receivers added via `addTransceiver` or SDP re-offer.
- Extended the v2.37.0 `__mbCollectWebrtcStats()` probe to also report `audio_sinks: [{trackId, paused, muted, volume, readyState, currentTime, srcObject, error, ...}]` — one entry per hidden `<audio>` element we created. Surfaced via `GET /api/v1/bot/{id}/debug` as `webrtc_stats_samples[*].snap.audio_sinks`. Lets us verify at a glance that every inbound track has a playing element.

### Added
- **Live Python-side peak-amplitude logger** (user-requested more logging). `_audio_health_loop` now reads the last ~3 s of the recording file on each iteration and logs one of:
  - `audio_health: OK — size=… peak_recent=1234/32767 ✓`  (INFO, peak ≥ 200)
  - `audio_health: SILENT — size=… peak_recent=0/32767 (<200 threshold)`  (WARNING)
  - `audio_health: size=… peak_recent=n/a`  (file too small / probe failed)
  This surfaces the silence-vs-audio distinction immediately in the Railway deploy-log tail without needing the `/debug` JSON. Loop frequency bumped 30 s → 15 s for faster feedback. `peak_recent_3s` added to each `audio_health_samples` entry.
- **Compact webrtc-stats log line per 15 s sample**: `webrtc_stats: pc[0] state=connected ice=connected inAudio pkts=3640 bytes=267993 energy=3.372 | sinks=2 playing=2 | page_media_elems=3`. Makes the "is remote audio being received?" vs "are our sinks playing?" distinction visible in the normal log stream.
- **Rich JS-side logging in the audio-attach shim**: `[mb-audio] attached track … -> <audio autoplay>, play() ok` / `[mb-audio] <audio>.play() rejected for track …:` / `[mb-audio] ontrack audio id=… enabled=… muted=… -> attached=true` / `[mb-audio] scanReceivers attached N audio track(s) after setRemoteDescription` / `[mb-audio] track … ended|muted|unmuted`. Captured by the v2.35.0 `console_log_tail` so the whole attach sequence is post-mortem-visible.

### Context
- The user also pointed at `github.com/proark1/meetingservice/` as a possible reference. Investigation confirmed that repo is the **onepizza platform itself** (Node.js + Express + Socket.IO, no Playwright/Puppeteer/ffmpeg/PulseAudio/Chromium), not a sibling bot implementation — nothing to borrow from it.

---

## [2.37.1] - 2026-04-17

### Fixed
- **onepizza: silent recordings were caused by a join-detection bug, not audio routing** — confirmed by the v2.37.0 diagnostic. `console_log_tail` of bot `1ddac2dd…` (08:54 UTC) showed `[error] Set answer error: InvalidStateError: Failed to execute 'setRemoteDescription' on 'RTCPeerConnection': Failed to set remote answer sdp: Called in wrong state: stable`. Combined with the click sequence in the API logs (5 different join-button strategies fired in 30 s on attempt 1, then a fresh Xvfb display/Chrome process for attempt 2), the root cause is that `_join_onepizza` was multi-clicking the join button and corrupting onepizza's WebRTC SDP state machine. Each click initiates a new SDP offer; by the time `setRemoteDescription` is called for the second answer, the peer is already in `stable` state → InvalidStateError → no remote audio ever rendered → null-sink records pure zeros (peak_amp=0). The user-visible symptom was the bot "blinking" / "doubling, tripling" in the meeting then disconnecting — multiple `RTCPeerConnection` instances, each one a fresh participant, before the SDP failure killed them.
- **Why the multi-click happened**: the state-detection loop at the top of `_join_onepizza` had a catch-all selector `"button, [role='button']"` as its 4th tier. When Playwright's stricter `wait_for_selector(state="visible")` timed out on `#meetingRoom` (which IS in the DOM but hidden until joined) it fell through to the catch-all and matched `#lobbyMicBtn` (first button in document order) instead of `#lobbyJoinBtn`. The branch then ran all 5 click strategies on `#lobbyJoinBtn` back-to-back, because the post-click `wait_for_selector("#meetingRoom, #waitingRoomOverlay", state="visible", timeout=8_000)` had the same Playwright-visibility issue and kept timing out.
- **Fix in `backend/app/services/browser_bot.py:_join_onepizza`**:
  - Replaced the 4-tier selector list (and the brittle `wait_for_selector(state="visible")` checks) with a single JS state-probe (`state_probe_js`) that returns `'in_meeting' | 'waiting' | 'lobby' | 'unknown'` based on actual DOM visibility (offsetParent + computed display/visibility). No more catch-all that could match the wrong button
  - Replaced the per-strategy 8-second `wait_for_selector` with a JS progress-probe (`progress_probe_js`) polled every 500 ms for up to 20 s. The probe returns `'in_meeting' | 'waiting' | 'joining' | 'connecting' | 'lobby'` and looks at: visible toolbar (`#leaveBtn` / `#micBtn` / `#camBtn`), hidden `#lobby`, `#lobbyJoinBtn.disabled`, button text containing "joining"/"connecting"/"please wait", and any `RTCPeerConnection` whose `connectionState`/`iceConnectionState` advanced past `'new'` (using the `window.__mbRtcPcs` registry from 2.37.0)
  - The strategy loop now bails as soon as the probe returns anything other than `'lobby'`. Anything-but-`'lobby'` means the click registered with onepizza's JS — clicking again would corrupt the SDP exchange
  - Reduced strategies from 5 → 3 (removed the JS text match and JS form submit fallbacks; if the first three real clicks on `#lobbyJoinBtn` fail to advance the page, the 4th and 5th would just queue duplicate join attempts and make things worse)

---

## [2.37.0] - 2026-04-17

### Added
- **WebRTC-stats diagnostic probe** — additive, diagnostic-only, no behavior change. The 2.35.1 getUserMedia shim got onepizza through the join flow, but a subsequent production run (bot `2a97d1e9…`, 2026-04-17 08:54 UTC) still ended with `peak_amp: 0.0` and `max_sink_inputs_on_target: 0` across the whole call, even though `pre_leave` pactl showed the Chromium sink-input correctly attached to the target sink, not muted, not corked. The null-sink recorded 2.8 MB of pure zeros. The distinction we couldn't make from existing diagnostics: is Chrome receiving remote audio RTP at all (signalling / ICE failure), or is it receiving packets but not playing them to PulseAudio (audio-element / autoplay failure)? This release collects the data to tell those apart.
- **New init script** in `backend/app/services/browser_bot.py` wraps `window.RTCPeerConnection` a third time (on top of the audio-forcing + getUserMedia shims) to push every created `pc` into `window.__mbRtcPcs`, and exposes `window.__mbCollectWebrtcStats()` — a JSON-safe snapshot of every peer-connection's `connectionState` / `iceConnectionState` / `signalingState` plus `getStats()` inbound-rtp audio (packets, bytes, jitter, `totalAudioEnergy`) and outbound-rtp counters, plus every `<audio>` / `<video>` element on the page with `paused` / `muted` / `volume` / `audio_tracks` / `has_srcobject`
- **New async loop `_webrtc_stats_loop(bot_id, page, debug_dir, stop_event)`** in `browser_bot.py` polls that function every 15 s via `page.evaluate()` (5 s timeout), appends the snapshot to `bot.webrtc_stats_samples` (capped at the last 40) and to `{debug_dir}/webrtc_stats.jsonl`. Safe no-op when `bot_id` is empty or when `window.__mbCollectWebrtcStats` isn't defined yet
- **New `BotSession.webrtc_stats_samples: list`** in `backend/app/store.py`, persisted into `BotSnapshot.data`, restored on startup, and surfaced by `GET /api/v1/bot/{bot_id}/debug` as `webrtc_stats_samples`. No DB migration required
- Looking for `connection_state: 'failed'` / `'disconnected'` vs. `'connected'` with `inbound_audio.packets == 0` distinguishes a handshake failure from a received-but-not-played failure; a playing `<audio>` with `paused: false` and `audio_tracks > 0` confirms remote tracks are wired; `totalAudioEnergy` remaining 0 over several samples with non-zero `packets` points at the autoplay / audio-element attach path

---

## [2.36.0] - 2026-04-17

### Added
- **Settings: Microphone Test** — new card in the dashboard Settings tab that lets a user verify their mic is actually picking up voice before sending a bot into a meeting. Shows a live input-level bar (silent / green / amber), a decaying peak-% readout, and a "Voice detected ✓" status once sustained audio is observed. Uses `navigator.mediaDevices.getUserMedia` + Web Audio API `AnalyserNode` in-browser; no audio ever leaves the client and no backend route is involved. Includes an input-device dropdown populated from `enumerateDevices()` (labels appear after permission is granted), handles `NotAllowedError` / `NotFoundError` inline, and auto-releases the mic on tab-hide. Lives in `backend/app/templates/dashboard.html` inside `#section-settings`

---

## [2.35.1] - 2026-04-17

### Fixed
- **onepizza.io: empty transcript because Chrome produced no audio at all** — root cause confirmed via `/api/v1/bot/{id}/debug` data shipped in 2.35.0. Throughout a 3 m 50 s onepizza call, `audio_health_samples` showed `sink_inputs_total: 0` at 7 of 8 samples and `pactl_dumps.pre_leave` showed the bot's null sink **SUSPENDED** with zero sink-inputs. Chrome was never emitting *any* audio to PulseAudio — previous 2.34.1/2.34.2 fixes were routing a stream that didn't exist. The `console_log_tail` captured the real reason: `"[warning] Camera/mic access: NotFoundError Requested device not found"`. onepizza's WebRTC join flow calls `getUserMedia({video: true, audio: true})`; the bot container has no camera, Chrome throws `NotFoundError` for the whole constraint, onepizza's JS aborts its peer-connection setup and never receives remote audio.
- **Fix in `backend/app/services/browser_bot.py`** — injected a second init script that patches `navigator.mediaDevices.getUserMedia` to retry with a synthetic fallback when the original call fails with `NotFoundError` / `NotReadableError` / `OverconstrainedError`. The fallback:
  - keeps the real audio track (via the existing PulseAudio `module-virtual-source` virtual mic) so TTS still works
  - provides a 640×480 15 fps black `canvas.captureStream()` video track when the camera is missing, so the meeting UI sees a working MediaStream
  - also patches `enumerateDevices()` to always report at least one `videoinput` and `audioinput` entry, so meeting clients that probe devices before calling `getUserMedia` proceed through the join path
- Other platforms (Google Meet, Zoom, Teams) already handle the missing-camera case themselves, so the shim only activates on failure and is a no-op in the healthy path — no regression risk there

---

## [2.35.0] - 2026-04-17

### Added
- **Transcription-failure diagnostic bundle + `GET /api/v1/bot/{id}/debug` endpoint** — prior attempts (2.34.1, 2.34.2) tried to fix "no audio captured" blindly; this release stops guessing and captures the data needed to diagnose the root cause on every run. When a bot ends with `"Transcription returned no content / No audio was captured"` the error message now carries a `diag={...}` JSON blob with: audio file size, silence-check peak amplitude, ffmpeg exit code + last 500 chars of stderr, peak count of PulseAudio sink-inputs routed onto the null sink during the call, last audio-file size delta, and Gemini's `finish_reason` + safety ratings. All of this plus full forensics is available at `GET /api/v1/bot/{bot_id}/debug` (owner-gated), including:
  - `ffmpeg_stderr_tail` — last 16 KB of previously-discarded ffmpeg stderr (captured to `{RECORDINGS_DIR}/debug/{bot_id}/ffmpeg.stderr.log`)
  - `audio_health_samples` — 30 s samples over the call showing file growth, ffmpeg liveliness, sink-input total + count routed to the bot's null sink
  - `pactl_dumps` — PulseAudio state (`list sinks/sink-inputs/sources/modules short`) snapshotted at `pre_pulse`, `post_setup`, `post_ffmpeg`, and `pre_leave`
  - `console_log_tail` — last 200 browser console / pageerror / requestfailed entries from Playwright (was never captured before)
  - `last_gemini_finish_reason` + `last_gemini_safety_blocks` — distinguishes a SAFETY block from a silent recording
- New `BotSession` dataclass fields (`audio_health_samples`, `pactl_dumps`, `console_log_tail`, `ffmpeg_exit_code`, `ffmpeg_stderr_tail`, `audio_peak_amplitude`, `last_gemini_finish_reason`, `last_gemini_safety_blocks`, `debug_dir`) are persisted into the existing `BotSnapshot.data` JSON column — no DB migration needed, survives restart, retains for the usual 24 h TTL
- Per-bot diagnostic directory at `{RECORDINGS_DIR}/debug/{bot_id}/` holds `ffmpeg.stderr.log`, `audio_health.jsonl`, `console.jsonl`, and `pactl_*.txt`

### Changed
- `_start_ffmpeg()` in `backend/app/services/browser_bot.py` now accepts `stderr_log_path=` and pipes ffmpeg's stderr there instead of `DEVNULL`; backwards-compatible default preserves old behavior for any unforeseen caller. Exit code is appended to the log on termination
- `run_browser_bot()` accepts a new `bot_id=""` kwarg so the audio-health loop / Playwright console ring-buffer / pactl snapshots can be attributed to a specific BotSession. `bot_service.run_bot_lifecycle` now passes `bot_id=bot.id`
- `transcribe_audio()` in `backend/app/services/transcription_service.py` accepts a new `bot_id=` kwarg; when present, peak amplitude, Gemini finish_reason, and safety ratings are recorded on the BotSession

---

## [2.34.2] - 2026-04-17

### Fixed
- **"Transcription returned no content / No audio was captured" on long calls** — the one-shot WebRTC force-connect ramp shipped in 2.34.1 stopped at ~60 s, so any PeerJS / RTCPeerConnection established later (late joiners, renegotiations on onepizza) was never wired into Chrome's `AudioContext.destination` and Chrome stopped rendering that peer's audio to PulseAudio, leaving the null-sink recording silent for the remainder of the meeting. Three changes in `backend/app/services/browser_bot.py`:
  - **Continuous routing + force-connect loop** — `_late_routing_syncs()` now keeps `_sync_chrome_audio_routing()` and `_force_connect_webrtc_audio()` running every 15 s for the entire meeting, not just the first 60 s. Repeat passes log at DEBUG unless they actually moved sink-inputs or connected new tracks, so log volume is unchanged in the healthy case
  - **Audio-health watchdog** — new `_audio_health_watchdog()` task samples the tail of the growing WAV every 20 s (reusing `transcription_service._check_audio_has_speech` / `_SILENCE_PEAK_THRESHOLD`). After ~60 s of continuous silence it logs the current PulseAudio sink-input count + ffmpeg pid/liveness and triggers an out-of-band routing + WebRTC re-connect pass. Diagnostic-only — the meeting is not failed from the watchdog
  - **One-shot ffmpeg restart** — if the ffmpeg recorder dies mid-meeting (pipe break, PulseAudio hiccup) the watchdog restarts it exactly once against the same WAV path, so a transient failure no longer produces a zero-byte recording
- **Chromium AudioContext occasionally stuck in 'suspended'** — the init script in `browser_bot.py` now runs a 2 s `setInterval` that calls `_mbAudioCtx.resume()` whenever the context is not `running`. A suspended AudioContext silently drops all graph output, which manifested identically to the WebRTC-not-connected bug

---

## [2.34.1] - 2026-04-16

### Fixed
- **onepizza.io: recorded audio was always silent (peak amplitude 0) → empty transcript** — Chrome's WebRTC remote-audio tracks were never wired into the page's Web Audio destination, so even though the PulseAudio sink-input was routed correctly to the bot's recording sink, ffmpeg captured pure zeros (`VAD: zero speech frames detected in 196 s`). The post-admission "Audio force-connect fallback" only ran once at admission+0.8 s — at that point onepizza's PeerJS connections didn't exist yet, so it found `connected=0` and never retried. Two complementary fixes in `backend/app/services/browser_bot.py`:
  - **Periodic re-attempts** — extracted the imperative WebRTC track-connect logic into a new module-level helper `_force_connect_webrtc_audio(page)` and call it after every routing-sync step (post-admit, ~3 s, ~8 s, ~15 s, ~30 s, and a new ~60 s pass). Each call walks `<audio>` / `<video>` elements with `srcObject`, scans `window` for RTCPeerConnection wrappers (PeerJS / SimplePeer / direct), and hooks any new audio receivers into `ctx.destination`. A `window._mbConnectedTracks` `WeakSet` makes the call idempotent — previously-connected MediaStreamTracks are skipped, so repeated invocations cannot double-connect a stream
  - **Stronger init-script patch** — `RTCPeerConnection.prototype.setRemoteDescription` is now patched in addition to the `track` event listener. After every offer/answer resolves, the patched method sweeps `pc.getReceivers()` and force-connects any new audio tracks. This is more reliable than the `track` event across PeerJS / SimplePeer / unified-plan implementations. Also added a legacy `addstream` event listener for older WebRTC libraries
- Effect: on onepizza, the next periodic pass after a participant's PeerJS connection establishes will discover and connect the audio track, forcing Chrome to render the decoded PCM through PulseAudio. ffmpeg then captures real samples and transcription proceeds normally. No regression on Google Meet / Zoom / Teams — the idempotency guard prevents any double-connect

---

## [2.34.0] - 2026-04-16

### Documentation
- **`api/openapi.json`** — bumped `info.version` to 2.34.0; added `/say` and `/chat` paths and `SayRequest` / `SayResponse` / `ChatRequest` / `ChatResponse` component schemas; extended `BotResponse.transcript` description with the new `source` / `message_id` / `bot_generated` fields; expanded `WebhookCreate.events` description to enumerate all supported events including `bot.live_chat_message`
- **`README.md`** — added the v2.34.0 entry under "Recent changes", added `/say`, `/chat`, `/stream` rows to the Bots endpoint table, added a full "Supported events" webhook table, updated the Bot response object's `transcript` field description, and the webhook payload example now includes both a voice and a chat entry
- **`INTEGRATION_GUIDE.md`** — new "Driving the Bot Mid-Meeting" section with `curl` examples for `/say`, `/chat`, and the SSE `/stream`; added the new live events to the webhook table
- **`sdk/python/README.md`, `sdk/js/README.md`** — added "Live interaction" sections showing how to call `/say`, `/chat`, and consume `/stream` directly until typed wrappers ship; updated webhook-create examples to include `bot.live_chat_message`
- **`CLAUDE.md`** — bumped the webhook event count from 13 to 14 and described the source/message_id semantics

### Added
- **Unified live transcript (voice + chat)** — meeting chat messages are now captured as first-class transcript entries alongside voice. A new `_chat_capture_loop` in `browser_bot.py` polls the chat panel at 4 Hz across Google Meet / Zoom / Teams / onepizza, dedups per-line via a stable short sha1, and appends each new message to the shared `structured_transcript` with `source="chat"` and a `message_id` field. Voice entries carry `source="voice"` (default). Both flow through the existing `on_live_entry` pipeline, so WebSocket, SSE, DB persistence, and webhook fan-out come free
- **New webhook event `bot.live_chat_message`** — fired for every captured chat message. Added to `WEBHOOK_EVENTS` in `api/webhooks.py`. `bot_service.on_live_entry` branches on `entry["source"]` to dispatch this event for chat and the existing `bot.live_transcript` for voice
- **`POST /api/v1/bot/{id}/say`** — make the bot speak arbitrary text in a live meeting. Body: `{text, voice: "gemini"|"edge", interrupt: bool}`. Defaults to Gemini TTS for natural voice. Returns 202 immediately; synthesis + playback run in the background. Concurrent calls serialise behind an `asyncio.Lock` on the `BotSession`; `interrupt=true` cancels the in-flight speak task and jumps ahead. Requires `in_call` status, enforces ownership check, rate-limited at 30/min
- **`POST /api/v1/bot/{id}/chat`** — post arbitrary text into the live meeting's chat panel. Body: `{text}`. Returns 202 immediately; keyboard typing runs in the background behind an `asyncio.Lock` so concurrent calls don't race the per-platform chat DOM selectors. The bot's own message id is pre-registered in `seen_chat_ids` so the capture loop doesn't re-emit the bot's own messages
- **`BotSession.runtime`** — new in-memory handle (populated after admission, cleared on exit via `on_runtime_ready` callback) that exposes the Playwright Page, per-bot pulse-mic name, TTS config, and the two serialisation locks so the API endpoints can drive the live bot without going through `run_browser_bot`
- **`BotSession.seen_chat_ids`** — per-bot set of chat message hashes, seeded on first successful capture poll and preserved across browser reconnects within a single bot lifecycle

---

## [2.33.5] - 2026-04-16

### Fixed
- **Dashboard API key row XSS (defense-in-depth)** — `_prependKeyRow()` in `dashboard.html` was interpolating the user-supplied key name directly into `innerHTML`. Exploitable only as self-XSS today (you can only set your own key name), but the name is also rendered on shared surfaces, so the pattern needed fixing. Name cell is now populated via `textContent`; the interpolated fields that remain (`key_preview`, `full_key`, `id`) are all server-generated. Also aligned the Copy button markup with the server-rendered row (`type="button"`, `title="Copy key"`, `⎘` glyph) so JS-inserted rows match their Jinja-rendered siblings exactly.
- **Revoke API key returned 405 Method Not Allowed** — `_handleRevoke()` POSTed to `/dashboard/keys/{id}` but the backend route is `POST /dashboard/keys/{id}/revoke` (ui.py:534). Added the missing `/revoke` suffix so the button actually revokes the key.
- **Per-bot `webhook_url` only received terminal events** — `_set_status()` and six other `dispatch_event()` callsites in `bot_service.py` (for `bot.joining`, `bot.in_call`, `bot.call_ended`, `bot.transcribing`, `bot.transcript_ready`, `bot.analysis_ready`, `bot.coaching_summary`, `bot.coaching_alert`, `bot.recurring_intel_ready`) did not pass `extra_webhook_url=bot.webhook_url`, so integrations that rely on the per-bot URL (e.g. 1tab.ai / onepizza.io) never saw non-terminal lifecycle events and their `meeting_recordings.status` appeared frozen at `joining` even when the bot had joined successfully. All ten in-lifecycle callsites now fan out to the per-bot webhook; the `BotSession.webhook_url` comment is updated to match the new contract

---

## [2.33.4] - 2026-04-16

### Fixed
- **Dashboard API key Name/Key columns swapped on creation** — `_prependKeyRow()` in `dashboard.html` built the new `<tr>` with the key preview in the first cell and the user-supplied name in the second, mismatching the table header (`Name | Key | Last used`) and the Jinja-rendered rows. The cell order is now Name → Key → Last used (set to `Never` for a freshly created key) so newly created keys render identically to existing ones without requiring a page reload

---

## [2.33.3] - 2026-04-10

### Fixed
- **Login lockout crashes on SQLite** — `last_failed_login_at` loaded from SQLite is a naive datetime; comparing it with `datetime.now(timezone.utc)` raised `TypeError`. Normalise to UTC before the comparison (matches the existing pattern in `store.py`)
- **Streaming transcription tasks not fully cleaned up on cancel** — Cancelled `_transcribe_utterance` tasks were not awaited after cancellation, leaving them in `"cancelling"` state and producing asyncio warnings. Now awaited with `gather(return_exceptions=True)` before re-raising `CancelledError`

---

## [2.33.2] - 2026-04-10

### Fixed
- **Analytics meeting cost undercount** — `if mc:` changed to `if mc is not None:` so meetings with a computed cost of exactly `$0.00` are included in the total meeting cost stat
- **Silent integration failures** — `dispatch_integrations()` now logs a warning for each failed task after `asyncio.gather()`, making Google Drive / Notion / Linear failures visible in logs
- **Audit log 500 leaks raw DB error** — `GET /api/v1/analytics/audit-log` now returns a generic "Audit log query failed" message instead of raw SQLAlchemy exception text
- **Streaming transcription task leak** — `_transcribe_utterance` tasks are now tracked in a set and cancelled when the streaming transcription loop is cancelled, preventing orphaned Gemini API calls after bot shutdown
- **Silent migration error pass** — Index creation failures in both PostgreSQL and SQLite migration paths now log at `DEBUG` level instead of silently `pass`

---

## [2.33.1] - 2026-04-10

### Fixed
- **Recurring meeting intelligence false matches** — URL comparison now uses scheme+netloc+path instead of path only, preventing meetings on different platforms with coincidentally identical path segments from being grouped together
- **Bot GET endpoint 500 on malformed analysis data** — `MeetingAnalysis` and `AIUsageEntry` Pydantic construction in `_to_response()` is now wrapped in try/except; bots with old or partially-corrupted stored data return a valid (empty) response instead of crashing
- **Webhook body/header timestamp inconsistency** — `_build_body` and `_sign` now share a single `datetime.now()` call in `dispatch_event`, so the `ts` field in the JSON body and the `X-MeetingBot-Timestamp` header always represent the same second

---

## [2.33.0] - 2026-04-10

### Improved
- **Faster mention responses** — Mention replies now use `claude-sonnet-4-6` (no thinking) instead of `claude-opus-4-6` with adaptive thinking, cutting response latency from 10-20s to 2-4s.
- **Faster chat detection** — Chat messages are now polled every 300ms cycle (was every 600ms). Typing delay reduced from 10ms to 3ms per character.
- **Better audio capture** — Added 5 PulseAudio routing syncs in the first 30s after joining (was only 2 in 4s), and increased sync frequency to every 8s during the first 2 minutes. Fixes silent recordings on onepizza.io and other WebRTC platforms where sink-inputs appear late.
- **Voice response reliability** — `_speak_in_meeting()` now retries mic routing if Chrome hasn't opened its mic yet, and `_move_chrome_source_output()` returns the number of moved outputs so callers can detect and handle failures.

---

## [2.32.2] - 2026-04-10

### Fixed
- **MCP `create_bot` tool broken** — Fixed `AttributeError` caused by calling `bot_service._detect_platform()` (private name that doesn't exist); corrected to `bot_service.detect_platform()`. AI agents using the MCP server could not create bots.
- **UI audio download button always visible** — `BotSession.recording_available()` was a plain method, not a `@property`. Jinja2 evaluated it as a truthy method object, showing the audio download button on every bot page even when no recording file existed.
- **UI video download button never visible** — `BotSession.video_available` property was missing entirely. Jinja2 returned `Undefined` (falsy), hiding the video download button even when a video file was present. Added the `@property`.
- **Idempotency keys never cleaned up** — The retention loop checked `expires_at` on read but never deleted expired rows, causing the `idempotency_keys` table to grow unboundedly. Expired keys are now batch-deleted each retention cycle.

---

## [2.32.1] - 2026-04-06

### Fixed
- **NameError in health endpoint** — Fixed `_task_heartbeats` variable scope issue causing 500 errors on GET /health. Moved variable to module-level scope so background task health monitoring works correctly.

---

## [2.32.0] - 2026-04-04

### Added
- **Structured error responses with incident IDs** — All 5xx errors now include a unique `incident_id` for tracking; 422 validation errors also include `incident_id`
- **Background task health monitoring** — `/health` endpoint now reports heartbeat status for all 7 background tasks (queue processor, cleanup, webhook retry, calendar poll, retention, monthly reset, weekly digest) with staleness detection
- **Webhook PATCH endpoint** — `PATCH /api/v1/webhook/{id}` allows updating URL, events, or re-enabling auto-disabled webhooks (resets `consecutive_failures` on re-enable)
- **Webhook auto-disable audit logging** — When a webhook is auto-disabled after 5 consecutive failures, an audit log entry (`webhook.auto_disabled`) is now created
- **Paginated envelope for list endpoints** — Webhook list, delivery logs, and integrations now return `{results, total, limit, offset, has_more}` instead of bare arrays
- **Webhook event payload schemas** — OpenAPI docs now include `WebhookEventPayload` and `WebhookEventList` models documenting the webhook delivery format
- **WebSocket message format documentation** — `/ws` endpoint docstring now documents the JSON message format and all broadcast event types
- **`PaginatedResponse` schema** — Reusable pagination envelope in `schemas/bot.py` for consistent list responses
- **`ENVIRONMENT` config setting** — New `ENVIRONMENT` env var (default: `development`) to control production-strict behavior
- **Bot store memory settings** — New `BOT_TTL_HOURS`, `STORE_CLEANUP_INTERVAL_SECONDS`, `STORE_MAX_BOTS` config settings for tuning in-memory store

### Improved
- **Input validation hardened** — `webhook_url` and `keyword_alerts[].webhook_url` capped at 2048 chars; `template` field enforced as Literal enum; `metadata` limited to 20 keys (64-char keys, 256-char string values); `vocabulary` items capped at 200 chars per term; `keyword` capped at 100 chars
- **CORS tightened** — `allow_methods` restricted to `GET/POST/PUT/PATCH/DELETE/OPTIONS` (was `*`); `allow_headers` restricted to `Authorization/Content-Type/X-Idempotency-Key/X-Sub-User` (was `*`)
- **Sub-user ID validation** — `X-Sub-User` header now validated against `^[a-zA-Z0-9_\-\.@]{1,255}$` regex; rejects special characters
- **JWT production safety** — Server now refuses to start if `JWT_SECRET` is the default value and `ENVIRONMENT=production`
- **Graceful shutdown** — All fire-and-forget `asyncio.create_task()` calls in bot_service.py now tracked via `_tracked_task()` and cancelled on shutdown
- **Background task supervision** — `_supervised()` now records heartbeats per task; logs at CRITICAL level (not just error) when max restarts exceeded
- **Memory management** — Bot store cleanup interval configurable (default 30 min, was 1 hour); LRU eviction of terminal bots when store exceeds `STORE_MAX_BOTS` (default 10,000)
- **Rate limiting** — Added rate limits to recording download (5/min), video download (5/min), PDF export (5/min), markdown/JSON/SRT exports (10/min)

---

## [2.31.0] - 2026-04-03

### Fixed
- **Keyword alert deletion ignores account_id** — `DELETE /api/v1/keyword-alerts/{id}` checked ownership in the SELECT but not in the DELETE statement, allowing cross-account deletion by ID guessing
- **Export-to-Drive endpoint leaks data across sub-users** — `POST /api/v1/bot/{id}/export/drive` was the only export endpoint missing the `sub_user_id` isolation check added in v2.30.2
- **TypeScript SDK error messages always return 0** — `parseErrorDetail()` used bitwise OR (`|`) instead of logical OR (`||`), causing error detail strings to be coerced to `0` via bitwise operations at runtime
- **Stripe top-up commits before credits are added** — `process_stripe_webhook()` committed the topup as "completed" before calling `add_credits()`, so a failure in credit addition would leave the customer charged but with no credits
- **PDF export crashes on text with angle brackets** — Six `Paragraph()` calls in PDF export passed AI-generated text (summaries, action items, chapter titles) without HTML-escaping, causing ReportLab parse errors when text contained `<`, `>`, or tag-like sequences
- **SSRF bypass via keyword alert webhook URLs** — Bot creation validated the main `webhook_url` against internal IPs but did not validate individual `keyword_alerts[].webhook_url` entries, allowing SSRF to internal infrastructure
- **Webhook null-safety crash on startup** — `load_persisted_webhooks()` accessed `.tzinfo` on `row.created_at` without a null guard, crashing if any webhook record had a NULL `created_at`
- **monthly_reset_task not cancelled on shutdown** — All background tasks were cancelled during lifespan shutdown except `monthly_reset_task`, causing task leaks and potentially blocking graceful shutdown
- **Bot scheduling ignores client timezone** — `join_at.replace(tzinfo=timezone.utc)` forcefully replaced the timezone label instead of converting, so a bot scheduled for `14:00 EST` would join at `14:00 UTC` (5 hours early)

---

## [2.30.4] - 2026-04-02

### Fixed
- **Inaccurate live transcript flush log count** — `bot_service.py` logged `len(_live_buffer)` after releasing `_live_lock`, so the count reflected buffer state after new entries could have been appended by concurrent transcription. Now uses `len(final_buffer)` (the snapshot taken inside the lock).

---

## [2.30.3] - 2026-04-02

### Fixed
- **Wrong `bot_data` keys in Google Drive, Linear, and Jira integrations** — `integration_service.py` referenced `bot_data["id"]` and `bot_data["platform"]` but the payload built by `_build_done_payload()` uses `"bot_id"` and `"meeting_platform"`. Google Drive filenames silently fell back to `"unknown"`, and Linear/Jira issue descriptions used an empty bot ID.

---

## [2.30.2] - 2026-04-02

### Fixed
- **Python SDK crashes on all delete operations** — Every delete method (`cancel_bot`, `delete_webhook`, `revoke_api_key`, `delete_keyword_alert`, `delete_calendar_feed`, `delete_integration`, `delete_workspace`, `remove_workspace_member`) called `response.json()` on HTTP 204 No Content responses, causing a JSON decode error. Added `_delete_no_content()` helper and updated all affected methods in both sync and async clients to return `None` instead.
- **Export endpoints leak data across sub-users** — The `_get_or_404` helper in `exports.py` only checked `account_id` but not `sub_user_id`, allowing business account sub-users to access each other's markdown, PDF, JSON, and SRT exports. Now enforces sub-user isolation matching the pattern in `bots.py`.
- **Personal analytics ignores sub_user_id** — `GET /api/v1/analytics/me` returned aggregated data for all sub-users within a business account instead of scoping to the requesting sub-user via the `X-Sub-User` header.

---

## [2.30.1] - 2026-03-29

### Fixed
- **Voice response not heard when bot is auto-muted on join** — `_speak_in_meeting` now always calls `_unmute_mic` before playing TTS audio, regardless of the `start_muted` setting. Previously, when `start_muted=False` (the default), the unmute step was skipped on the assumption the mic was already live. Zoom and Teams routinely auto-mute bots on admission, so TTS audio was captured by WebRTC but never transmitted. `_unmute_mic` is safe to call unconditionally — it checks the current UI state and is a no-op when the mic is already on.
- **Silent voice failure** — `_dispatch_reply` now logs a warning when `_speak_in_meeting` returns `False` in `voice` mode, making TTS/PulseAudio errors visible in logs.

---

## [2.30.0] - 2026-03-29

### Performance
- **Weekly digest N+1 eliminated** — `send_weekly_digest` now issues 3 DB queries total (accounts, batch snapshots, batch action-item counts via `GROUP BY`) instead of 2 queries per account; `len(scalars().all())` count replaced with `func.count()` aggregation
- **Webhook retry efficiency** — Retry loop pre-fetches all needed `WebhookEntry` objects in a single lock acquisition instead of N separate `store.get_webhook()` calls; `_retry_delays()` result cached as module constant `_RETRY_DELAYS` to avoid repeated string parsing
- **Translation concurrency cap** — `asyncio.Semaphore(20)` added to post-meeting transcript translation to prevent unbounded concurrent AI requests on long transcripts
- **Duplicate Account query eliminated** — `_post_completion_notifications` fetches `Account` once and reuses it across email notification and auto follow-up branches
- **Gemini polling backoff** — File-state polling in `transcription_service` switched from a flat 1 s interval (max 60 polls) to exponential backoff (1 s → 2 s → 4 s … capped at 10 s; 90 s total budget), reducing unnecessary API calls for quickly-processed files
- **Store lock contention reduced** — `list_webhooks` now copies values inside the lock and sorts outside, freeing the lock sooner
- **DB indexes** — Added composite `(account_id, status)` index on `bot_snapshots`; added `status` index on `action_items` to speed up filtered queries

---

## [2.29.0] - 2026-03-29

### Added
- **Favicon** — Brand icon (microphone on dark background) now shows in browser tabs
- **Toast animations** — Notifications slide in from right with backdrop blur and bounce easing
- **Modal bounce animation** — Ask, Email, Re-analyze modals now scale-bounce in instead of plain fade
- **Search highlight styling** — `<mark>` elements get yellow background with rounded corners
- **Landing page scroll animations** — Feature cards, pricing cards, integration items fade-in with stagger as user scrolls
- **Landing page animated stat counters** — Numbers count up from 0 when scrolled into view
- **Analysis card polish** — Each analysis section (Summary, Key Points, Action Items, Decisions, Next Steps, Risks, Unresolved) now has a color-coded left accent border, gradient background, and icon badge
- **Dashboard welcome card** — New users see a branded dark gradient card with "Send your first bot" CTA, dismissible with ×

---

## [2.28.1] - 2026-03-29

### Fixed
- **Dashboard sidebar navigation broken for History and Action Items** — Missing from `_VALID_SECTIONS` array, so URL hash navigation and browser back button didn't work for these sections
- **Form error text persists on resubmission** — Old error message stayed visible when user fixed input and resubmitted. Now clears error text before each submission attempt

---

## [2.28.0] - 2026-03-29

### Improved
- **Bot detail auto-refresh** — Pages for active bots (joining, in_call, transcribing, etc.) now auto-refresh every 8 seconds instead of requiring manual refresh
- **Error message prominent** — Error bots now show the error message directly below the status chip instead of buried in the details table
- **Empty state fallbacks** — "No speaker statistics available" and "No AI analysis available (Transcript-only mode)" messages now shown for completed bots with missing data, instead of silently hiding sections
- **Duration display** — Calls longer than 1 hour now show hours (e.g. "1h 23m 45s" instead of "83m 45s")
- **Scheduled status chip** — Changed from yellow (warning) to blue (info) to distinguish from ready/queued
- **Copyright year** — Updated from 2025 to 2026

### Fixed
- **API domain inconsistency** — Developer code sample on landing page used `api.meetingbot.dev` while hero used `api.justheretolisten.io`. Standardized to brand domain

---

## [2.27.5] - 2026-03-29

### Fixed
- **XSS in dashboard** — Unescaped meeting URLs, status values, and error messages in JS template literals could allow HTML injection. All dynamic values now use `_escHtml()` before insertion
- **Leave button not hidden on status change** — Dashboard polling now removes the Leave button when bot exits `in_call` status
- **Null dereference in bot detail page** — `leaveBot()` and `cancelBot()` JS functions crashed if their button elements didn't exist in the DOM. Added null guards
- **Calendar iCal parsing crash** — Malformed iCal feed data crashed the entire calendar sync. Now catches parse errors and returns empty list
- **Calendar dispatch cache memory growth** — Dispatch dedup cache pruned every 24h, growing unbounded with large calendar feeds. Reduced to every 4h

---

## [2.27.4] - 2026-03-29

### Fixed
- **Audio force-connect only scanned PeerJS** (CRITICAL) — The `if (window.Peer)` guard prevented RTCPeerConnection scanning on non-PeerJS platforms like onepizza.io. Removed the guard and expanded scanning to cover SimplePeer (`_pc` property), generic `peerConnection` properties, and direct `RTCPeerConnection` instances on `window`

---

## [2.27.3] - 2026-03-29

### Fixed
- **Leave event reuse on retry** (CRITICAL) — `asyncio.Event` stays set once triggered. On bot join retries, the stale event caused the browser bot to immediately exit instead of waiting. Now creates a fresh event on each retry attempt
- **Cancel leaves bot in non-terminal state** — Cancelling a bot in `joining` status left it stuck (never transitioned to `cancelled`). Now forces terminal state after task cancellation if still in an active status
- **Consent message None crash** — If `CONSENT_MESSAGE` config was unset and no custom message provided, `build_announcement_message()` returned `None`, crashing `_send_chat_message`. Added hardcoded fallback
- **`process_consent` None transcript crash** — Passing `None` instead of `[]` caused `TypeError` when iterating. Added early return guard

---

## [2.27.2] - 2026-03-29

### Fixed
- **Bot detail page crash on null list fields** — When loading a completed bot from the database, JSON `null` values for list fields (`transcript`, `chapters`, `speaker_stats`, `participants`, `vocabulary`, `opted_out_participants`, `keyword_alerts`, `ai_usage`) caused `TypeError: object of type 'NoneType' has no len()` in the Jinja2 template. Fixed with `or []` in both `store.py` deserialization and `ui.py` snapshot parsing

---

## [2.27.1] - 2026-03-29

### Fixed
- **Leave event race condition** — If the leave API was called between bot admission and the monitoring loop start (~3-5s window), the event was set before anything was waiting on it. Now checks `leave_event.is_set()` before creating the wait task
- **`respond_on_mention` form logic** — Used `!== false` which returned `true` for `undefined` (when element missing). Now uses nullish coalescing `?? true`
- **Form always sends `transcription_provider` and `analysis_mode`** — Previously skipped sending defaults (`gemini`, `full`), which was fragile if server defaults changed
- **Leave command logging** — Fixed misplaced log statement outside the if-block

---

## [2.27.0] - 2026-03-29

### Fixed
- **Consent announcement never sent** — The consent service only processed opt-outs after the meeting but never actually sent the announcement to the chat. Now `run_browser_bot` sends the consent message via `_send_chat_message` immediately after the bot is admitted to the meeting
- **Audio silent on onepizza.io** — The RTCPeerConnection init script patch wasn't firing because onepizza may use PeerJS or another WebRTC wrapper. Added a force-connect fallback: after joining, the bot creates an AudioContext, finds all `<audio>`/`<video>` element streams and RTCPeerConnection receivers, and connects them to the audio destination so Chrome outputs audio to PulseAudio
- **PulseAudio routing timing** — Added a second `_sync_chrome_audio_routing` call 3 seconds after the first (which runs at 0.8s). WebRTC streams on onepizza take several seconds to fully initialize, so the first sync missed them
- **PulseAudio diagnostics** — Added `pactl list short sink-inputs` dump after joining so logs show whether Chrome audio is actually routed to the recording sink

### Added
- `consent_enabled` and `consent_message` params passed through to `run_browser_bot`

---

## [2.26.0] - 2026-03-28

### Added
- **Graceful leave endpoint** — `POST /api/v1/bot/{id}/leave` tells an in-call bot to leave the meeting cleanly, then proceed with transcription and analysis (unlike Cancel which stops everything)
- **Leave button in dashboard** — Yellow "Leave" button appears next to Cancel for bots with `in_call` status
- **Leave and Cancel buttons on bot detail page** — Active bots now show "Leave Meeting" and "Cancel" buttons in the header
- **Dashboard proxy** — `/dashboard/bot/{id}/leave` cookie-auth proxy for the leave endpoint

---

## [2.25.3] - 2026-03-28

### Fixed
- **onepizza chat mention detection** — Chat scraper was reading the participant list tab on first polls, then switching to actual messages. The diff logic treated the shorter content as a "re-render" and skipped it, causing all mentions to be missed. Fixed scraper to target message area near `#chatInput`, detect DOM context switches, and check for new mentions on content replacement

---

## [2.25.1] - 2026-03-28

### Fixed
- **onepizza.io chat scraping** — Rewrote `_scrape_chat_messages` for onepizza with 3-tier fallback: known containers, DOM traversal from `#chatInput` up to the panel to extract message text, and broad side-panel fallback
- **Caption error filtering** — Captions returning platform error text like "Captions error: network" are now filtered out instead of being treated as real speech, preventing the mention monitor from processing error messages
- **Chat debug logging** — Added INFO-level chat scrape logs for first few polls to diagnose empty chat reads

---

## [2.25.0] - 2026-03-28

### Added
- **onepizza.io: full chat & caption support** — The bot can now read and send chat messages, enable/scrape captions, and unmute/mute mic on onepizza.io meetings. Previously all four functions (`_enable_captions`, `_scrape_captions`, `_scrape_chat_messages`, `_send_chat_message`) silently returned empty/false for onepizza
- **onepizza.io selectors**: `#chatBtn`, `#chatInput`, `#chatSendBtn` for chat; `#captionsBtn`, `#moreCaptionsOpt` for captions; `#micBtn` for mic control

### Fixed
- **Dashboard bot options hidden behind collapse** — All bot creation options are now visible by default in organized labeled groups

---

## [2.24.1] - 2026-03-28

### Fixed
- **Dashboard bot options hidden behind collapse** — All bot creation options were buried under a collapsed "Advanced options" toggle. Removed the collapse and organized all options into visible labeled groups: Bot Behaviour, Branding & Identity, Recording & Transcription, Analysis & Output, Privacy & Compliance, and Alerts

---

## [2.24.0] - 2026-03-28

### Added
- **Dashboard: all remaining bot options** — Bot creation form now exposes every BotCreate field: analysis mode (full/transcript-only), custom analysis prompt, domain vocabulary, join muted, bot avatar URL, consent message, keyword alerts, and hourly rate for meeting cost estimation

---

## [2.23.0] - 2026-03-28

### Added
- **Dashboard: voice/chat interaction options** — Bot creation form now includes respond-on-mention toggle, response mode (text/voice/both), TTS provider (Edge/Gemini), consent announcement, auto follow-up email, and transcription provider (Gemini/Whisper) under Advanced Options

---

## [2.22.2] - 2026-03-28

### Fixed
- **onepizza.io bot join loop** — When `?name=` auto-join worked, the bot was already in the meeting (toolbar buttons visible, participant count showing) but `_join_onepizza` couldn't detect it because `#meetingRoom` container failed Playwright's visibility check. Added `#leaveBtn`, `#micBtn`, `#camBtn` toolbar selectors and a JS fallback that checks DOM state directly. Also fixed the same issue in `_wait_for_admission` for onepizza where `#meetingRoom.is_visible()` returned False despite the bot being in the call

---

## [2.22.1] - 2026-03-28

### Fixed
- **Bot leave/rejoin loop** — Leave-keyword detection (`_LEAVE_KEYWORDS`) used substring matching (`in` operator), causing false positives from words like "believe" (contains "leave"), "quite" (contains "quit"), "stopwatch" (contains "stop"). Now uses word-boundary regex matching
- **False positive meeting-end detection** — `_wait_for_meeting_end` checked the entire page body (including captions and chat) for end-text patterns like "meeting ended". A single detection from a transient caption would cause the bot to leave prematurely. Now requires confirmation on two consecutive polls (~10s apart) before exiting

---

## [2.22.0] - 2026-03-28

### Added
- **Landing page: 11 missing feature cards** — Added Voice Response/TTS, Transcription Provider Choice, Audio & Video Recording, Export Formats, Shareable Meeting Links, Post-Meeting Q&A, Analysis Templates, Auto Follow-up Email, Bot Customization, Scheduled Bot Joins, and Domain Vocabulary to the landing page
- **New "Recording & Output" section** on landing page showcasing recording, export, sharing, and Q&A capabilities

---

## [2.21.1] - 2026-03-28

### Fixed
- **DoS: unbounded vocabulary/keyword_alerts arrays** — `BotCreate` schema now enforces `max_length=100` on `vocabulary` and `max_length=50` on `keyword_alerts` to prevent memory exhaustion from oversized payloads
- **Missing rate limits on AI endpoints** — added `@_limiter.limit()` to `/analyze` (10/min), `/ask` (10/min), `/ask-live` (10/min), and `/followup-email` (5/min) to prevent API quota exhaustion
- **Memory DoS in search endpoint** — reduced bot loading limit from 10,000 to 500 in the `/search` endpoint to prevent OOM on accounts with large transcript volumes

---

## [2.21.0] - 2026-03-28

### Added
- **Dark mode** — system-wide dark theme with toggle button in navbar on all pages (dashboard, landing, login, register, bot detail, share). Respects `prefers-color-scheme` OS setting, persists preference in localStorage, no flash of unstyled content on page load
- **Mobile responsive dashboard** — sidebar collapses to a fixed bottom navigation bar on mobile (< 768px) with horizontally scrollable icons, bottom padding for content, and responsive section headers
- **Mobile responsive login/register** — split-panel layout stacks vertically on mobile with condensed branding panel and full-width form
- **Mobile responsive bot detail** — action buttons wrap properly, tables scroll horizontally, meta cards maintain 2-column grid on small screens
- **Mobile responsive share page** — padding adjustments for small screens
- **Visual polish** — page fade-in animation (0.3s), card hover lift effect, pulse animation on in-progress status chips, loading skeleton CSS utility class, focus-visible keyboard navigation outlines
- **Loading button state** — `.btn-loading` CSS class with spinner animation, applied on form submit for login/register
- **Password visibility toggle** — eye icon button on login and register password fields
- **Confirmation dialog** — reusable `confirmAction(message)` JS helper using Bootstrap modal for destructive actions
- **Progress indicator** — animated progress bar shown on bot detail page for in-progress statuses (joining, in_call, transcribing)
- **Section transitions** — dashboard section switching now uses fade-in animation

### Fixed
- **Mobile navbar overflow** — navbar elements (brand, dark mode toggle, balance pill, avatar, logout) now scale down properly on small screens (< 480px) with reduced padding, smaller fonts, and hidden brand text on very narrow (< 360px) screens
- **KPI cards clipped on mobile** — overview cards now use single-column layout on phones (< 480px) and 2-column on tablets, with proper `min-width: 0` and `overflow: hidden` to prevent text clipping
- **Section header stacking** — "Overview" header and "+ Add Credits" button now stack vertically with full-width button on mobile instead of floating awkwardly
- **Click hint animation on mobile** — decorative cursor animation hidden on small screens where it overlaps content

## [2.20.6] - 2026-03-28

### Fixed
- **Webhook `/deliveries` endpoint unreachable** — `GET /api/v1/webhook/deliveries` was defined after `GET /api/v1/webhook/{webhook_id}`, so FastAPI matched "deliveries" as a webhook ID and returned 404. Moved the literal route before the parameterized one
- **Semantic search unreachable** — duplicate `GET /search` handler in analytics meant the second handler (with `semantic` embedding search) was shadowed by the first. Merged both into a single handler supporting `q`, `limit`, `include_archived`, `platform`, and `semantic` parameters

## [2.20.5] - 2026-03-28

### Fixed
- **Race condition in credit addition** — `add_credits()` read the account balance without a database lock, allowing concurrent additions to lose updates. Now uses `SELECT ... FOR UPDATE` matching the existing `deduct_credits_for_bot()` pattern
- **OAuth authorization URL not URL-encoded** — query parameters were concatenated with raw `f"{k}={v}"` instead of `urlencode()`. The `state` parameter (base64 HMAC) can contain `+` and `=` which broke the redirect URL
- **JS SDK dead code** — removed unused `new URL()` construction in the URL builder method

## [2.20.4] - 2026-03-28

### Fixed
- **HTML injection in error notification email** — `bot_id` and `error` were injected raw into HTML in `notify_meeting_error`. Now escaped with `html.escape()` to prevent XSS via crafted error messages
- **OAuth callback CSRF bypass** — the `state` parameter was only validated if present, allowing attackers to omit it entirely and bypass CSRF protection. Now required on all OAuth callbacks
- **Integration update skips config validation** — `PATCH /integrations/{id}` validated the type but not the config, allowing type changes without required fields (e.g. Slack without `webhook_url`). Extracted validation into a shared helper used by both create and update

## [2.20.3] - 2026-03-28

### Fixed
- **AI cost tracking returning $0 for Haiku calls** — `_estimate_cost()` failed to match model IDs with date suffixes (e.g. `claude-haiku-4-5-20251001`) against the pricing table keyed by short names (`claude-haiku-4-5`). Now strips `-YYYYMMDD` suffixes before lookup

## [2.20.2] - 2026-03-28

### Fixed
- **Bot cancellation broken** — `asyncio.shield()` in `delete_bot` was protecting the lifecycle task from the cancellation signal, so `DELETE /bot/{id}` appeared to succeed but the bot kept running
- **Webhook accessor race condition** — `get_webhook()`, `list_webhooks()`, and `active_webhooks()` accessed the shared webhook dict without acquiring the asyncio lock, risking `RuntimeError: dictionary changed size during iteration` when webhooks were deleted concurrently
- **Oversized error messages** — bot error messages from uncaught exceptions were stored without truncation, potentially producing very large API responses and webhook payloads. Now capped at 2000 characters

## [2.20.1] - 2026-03-28

### Fixed
- **Double credit deduction** — if webhook dispatch failed after credits were deducted on the success path, the exception handler would deduct again. Added idempotency guard to prevent duplicate charges
- **Webhook test signature broken** — `POST /webhooks/{id}/test` assigned the raw `(sig, ts)` tuple to the `X-MeetingBot-Signature` header instead of unpacking it; also missing `X-MeetingBot-Timestamp` header entirely
- **422 validation errors not in structured format** — Pydantic `RequestValidationError` responses now include `error_code` and `retryable` fields matching the new machine-readable error model

## [2.20.0] - 2026-03-28

### Added
- **`POST /api/v1/bot/validate-meeting-url`** — fast-fail pre-flight endpoint that checks URL validity, detects the meeting platform, and reports whether real recording is supported
- **Machine-readable error responses** — all HTTP error responses now include `error_code` (e.g. `not_found`, `rate_limited`) and `retryable` (boolean) fields alongside `detail`
- **Webhook payload enrichment** — `bot.error` and `bot.cancelled` webhook events now include `error_code`, `error_message`, and `retryable` fields for programmatic error handling
- **Meeting URL normalisation** — personalisation query params (`name`, `displayName`, `email`, `avatar`, etc.) are stripped from meeting URLs before passing to the browser, preventing unintended auto-fill behaviour

### Fixed
- **Bot stuck in "ready" — never joins** — direct (non-scheduled) bot creation was missing the `status="joining"` update before starting the lifecycle task, so the bot appeared to never start
- **Scheduled bots with `join_at` ≈ now never joining** — if `join_at` was less than 1 second in the future, the 0-second timer could misfire; now starts immediately via `_start_or_queue_bot`
- **Queue processor race condition** — `_queue_event` was cleared before checking slot availability, causing up to 30-second delays for queued bots when a slot freed up

## [2.19.0] - 2026-03-26

### Added
- **Meeting History tab** in dashboard — browse all past meetings from the database, not just the 24-hour in-memory window. Shows URL, platform, status, duration, participant count, and transcript/analysis availability badges
- **Bot detail page DB fallback** — `/bot/{id}` now loads from `BotSnapshot` DB when the bot has expired from RAM. Users can view transcripts, analysis, and all meeting details from any historical meeting

## [2.18.0] - 2026-03-26

### Added
- **Transcript search** on bot detail page — filter and highlight entries in real time with match counter
- **"Ask about this meeting"** button — AI-powered Q&A on any completed meeting via modal (wires existing `POST /ask` endpoint)
- **"Generate follow-up email"** button — one-click AI follow-up email generation with copy-to-clipboard (wires existing `POST /followup-email` endpoint)
- **Bot search/filter** on dashboard — search by ID or URL, filter by platform (Zoom/Teams/Meet)

### Fixed
- **Auth broken on bot detail page** — Share link and speaker rename were calling `/api/v1/...` directly with cookies (API only reads Bearer tokens). Added proxy routes: `/dashboard/bot/{id}/share`, `/dashboard/bot/{id}/speakers`, `/dashboard/bot/{id}/ask`, `/dashboard/bot/{id}/followup-email`
- **Rate limiter crash on dashboard bot creation** — Internal ASGI proxy requests had `request.client=None`, crashing `slowapi.get_remote_address()`. Added safe wrapper with `X-Forwarded-For` fallback
- **httpx 0.28+ compatibility** — Replaced removed `AsyncClient(app=...)` shortcut with `httpx.ASGITransport(app=...)` across all 6 proxy routes
- **Bot status polling crash** — `store.list_bots()` returns `(list, total)` tuple but code iterated it as a list. Fixed tuple unpacking
- **Alone detection broken for onepizza** — Empty `_ALONE_TEXTS` made `text_alone` always False; now falls back to tile-only detection for platforms without text patterns
- **DELETE race condition with queued bots** — Removing a queued bot now also cleans up the `_bot_queue` and re-signals the queue processor
- **JS-created bot rows missing attributes** — `_prependBotRow` now adds `data-bot-id`, `bot-status-cell`, `bot-actions-cell` classes, and cancel button so polling/cancel/filter work on newly created rows
- **onepizza.io join button** — Lobby join now tries direct click, JS click fallback, then text-match fallback for robustness
- **Missing DB migration** — Added `ALTER TABLE action_items ADD COLUMN IF NOT EXISTS sub_user_id` to PostgreSQL migration script
- **All proxy routes error handling** — Added try-except to all 6 cookie-auth proxy routes to return 502 instead of crashing

## [2.17.0] - 2026-03-26

### Added
- **Live bot status polling** — Dashboard auto-updates bot status chips every 10 seconds without page refresh (`GET /dashboard/bots/status`)
- **Cancel bot from dashboard** — Cancel button on each active bot row, with `POST /dashboard/bot/{id}/cancel` proxy route
- **Advanced bot options in Send Bot form** — Collapsible section with record video, live transcription, PII redaction, and translation language toggles
- **"See it in action" demo section** on landing page — terminal-style API demo with 3-step walkthrough between How It Works and Pricing

### Fixed
- **Mobile responsiveness** — Dashboard: section headers wrap, bot action buttons stack vertically, advanced options 2-column grid, KPI grids adapt to 2 columns, webhook events grid fits small screens. Landing: demo terminal scrollable and sized for mobile, demo widget full-width on phones
- **Robust alone detection** — `_is_bot_alone()` now requires BOTH text pattern AND DOM tile count to agree before flagging the bot as alone, eliminating false positives from tooltips or loading text
- **Scheduled bots no longer block concurrent slots** — Scheduled bots use deferred `call_later()` timers instead of occupying a `_running_tasks` slot while sleeping; slots are only claimed at join time
- **CORS restricted in production** — When `API_KEY` is set and `CORS_ORIGINS` is still `*`, CORS is now restricted to same-origin only (set `CORS_ORIGINS` explicitly to allow specific origins)

## [2.16.2] - 2026-03-26

### Fixed
- **Bot leaves meeting immediately after joining** — Added 60-second grace period after join before alone-detection activates, preventing false positives from DOM not fully rendering participant tiles

## [2.16.1] - 2026-03-26

### Fixed
- **Critical: Dashboard bot creation auth** — "Send Bot Now" button now works for logged-in users; added `/dashboard/bot` proxy route that accepts cookie auth and forwards to the API with proper Bearer token
- **Critical: XSS in bot table row** — HTML-escape all dynamic values (`meeting_url`, `bot.id`, etc.) in `_prependBotRow` to prevent injection
- **Bot status badge** — Immediate bots now show "Ready" or "Queued" chip instead of always showing "Scheduled"
- **Dead CSS cleanup** — Removed orphaned `.hero-badge` styles from landing page after badge removal

## [2.16.0] - 2026-03-26

### Added
- **Send Bot Now** button in dashboard — logged-in users can send a bot to a meeting immediately from the UI, not just via API
- Toggle between "Send Now" (immediate) and "Schedule for later" modes in the bot creation form

### Changed
- Removed "Live on Zoom · Google Meet · Microsoft Teams" badge from landing page hero section

## [2.15.0] - 2026-03-24

### Changed
- **UI redesign — dark navy + warm beige theme** — Complete visual overhaul inspired by modern SaaS design. Primary color changed from Warm Coral (#E05A33) to Dark Navy (#1B2033). Body background changed to warm pinkish beige (#EDE4DF) with white cards. All buttons, gradients, active states, focus rings, chart colors, brand icons, and avatars updated across all 11 templates.

---

## [2.14.0] - 2026-03-24

### Changed
- **UI color palette overhaul** — Replaced indigo/cyan theme with warm coral palette across all templates (landing, login, register, dashboard, admin, API dashboard, webhook playground, share, bot, topup). Primary color is now Warm Coral (#E05A33), with warm neutral backgrounds, borders, and text colors. Gradients, buttons, badges, form focus states, and chart colors all updated to match.

---

## [2.13.0] - 2026-03-24

### Added
- **ClickHint cursor animations** — Animated cursor click hints on primary CTA buttons across all pages (landing, login, register, dashboard, topup) to guide new users toward key actions. Includes cursor movement, ripple, and glow effects with a 4.5s lifecycle. Hidden on mobile via media query.

---

## [2.12.3] - 2026-03-24

### Fixed
- **Login 500 error — missing DB columns** — `stripe_customer_id` and `stripe_subscription_id` were defined in the Account model but missing from the database migration script, causing every `SELECT` on the accounts table to fail with an `OperationalError` on databases created before v2.11.0
- **OAuth login 500 error** — OAuth callback (Google/Microsoft SSO) imported non-existent `_create_access_token` function instead of `_create_jwt`, causing an `ImportError` on every OAuth login attempt
- **OAuth cookie name mismatch** — OAuth callback set cookie as `access_token` but the dashboard reads from `mb_token`, so OAuth users appeared logged out after redirect

---

## [2.12.2] - 2026-03-22

### Fixed — Performance (Round 6)
- **Dashboard: 7 queries → 5** — Integration and calendar feed queries consolidated (was querying each table twice: once for active, once for all). Derives active subset in Python from single query.
- **Dashboard: OAuth query bounded** — Added `.limit(20)` to OAuthAccount query (was unbounded)
- **Admin: PlatformConfig query bounded** — Added `.limit(500)` (was unbounded)
- **Admin: Chart.js deferred** — Added `defer` to Chart.js CDN script tag in admin.html
- **Background loops: imports hoisted** — Moved `import json`, `from app.services.email_service` out of `while True` loops in main.py retention/digest tasks

---

## [2.12.1] - 2026-03-22

### Added — SDK coverage for new endpoints
- **Python SDK** (sync + async): 3 new methods — `subscribe(plan, success_url?, cancel_url?)`, `get_usage()`, `get_trends(days=30)` with `SubscribeResponse`, `UsageResponse`, `TrendsResponse` models
- **TypeScript SDK**: 3 new methods — `subscribe(params)`, `getUsage()`, `getTrends(days?)` with full type interfaces (`SubscribeParams`, `SubscribeResponse`, `UsageResponse`, `TrendsResponse`)

---

## [2.12.0] - 2026-03-22

### Added — Dashboard UI for Monetization & Trends
- **"Usage & Billing" analytics tab** — New 4th sub-tab in analytics section. Shows: monthly bot usage progress bar (color-coded green/amber/red), plan badge, credits balance, credits spent this month, avg cost per bot, billing reset date, daily usage table. Lazy-loaded from `GET /analytics/usage`.
- **Longitudinal trends in Trends tab** — After loading personal trends, also fetches `GET /analytics/trends?days=30` and displays: top 10 topics across meetings, meetings per day table (last 14 days).
- **Plan upgrade button** — Billing section plan card now shows "Upgrade Plan" button (hidden for Business). Triggers `POST /billing/subscribe` and redirects to Stripe Checkout.

---

## [2.11.0] - 2026-03-22

### Added — Subscriptions, Usage Analytics, Longitudinal Trends
- **Stripe subscription billing** — New `POST /api/v1/billing/subscribe` endpoint creates Stripe Checkout in subscription mode for Starter/Pro/Business plans. Expanded webhook handler processes `invoice.paid` (renew), `checkout.session.completed` mode=subscription (activate plan), `customer.subscription.deleted` (downgrade to free).
- **Account model: Stripe fields** — `stripe_customer_id` and `stripe_subscription_id` columns for linking accounts to Stripe customers.
- **Usage analytics endpoint** — `GET /api/v1/analytics/usage` returns: bots_used, bots_limit, plan, credits_balance, credits_spent_this_month, avg_cost_per_bot, daily_usage chart data.
- **MeetingSummary model** — Permanent lightweight record of each meeting (bot_id, platform, duration, participant_count, sentiment, health_score, topics, ai_cost, word_count). Persisted in `bot_service._do_analysis_inner()` after analysis completes. Survives beyond BotSnapshot's 24h TTL.
- **Longitudinal trends API** — `GET /api/v1/analytics/trends?days=30` returns: meetings_per_day, sentiment_trend, health_trend, top_topics (frequency across all meetings), cost_trend. Powered by the MeetingSummary table.

---

## [2.10.0] - 2026-03-22

### Added — Monetization + CI Pipeline
- **Plan limit enforcement** — Bot creation now checks `monthly_bots_used` against plan limits (Free=5, Starter=50, Pro=500, Business=unlimited). Returns HTTP 402 with upgrade message when limit reached. Uses `SELECT ... FOR UPDATE` to prevent race conditions.
- **Monthly usage counter** — Atomically incremented on each bot creation; hourly background task resets counters for accounts past their `monthly_reset_at` date.
- **Feature gating** — Premium features locked by plan tier via `check_feature()` in deps.py. Calendar auto-join, integrations, translation → Starter+. PII redaction, workspaces, keyword alerts → Pro+. SAML SSO, org analytics → Business.
- **Gated endpoints** — `POST /calendar/feeds` checks `calendar_auto_join`; `POST /bot` checks `translation`, `pii_redaction`, `keyword_alerts` when those options are used.
- **Stripe subscription config** — Added `STRIPE_STARTER_PRICE_ID`, `STRIPE_PRO_PRICE_ID`, `STRIPE_BUSINESS_PRICE_ID` config vars (subscription endpoints coming next).
- **CI pipeline** — GitHub Actions workflow (`.github/workflows/test.yml`) runs 17 pytest tests on every push to main and every PR. Python 3.12, pip caching, 5-minute timeout.
- **pytest config** — `backend/pyproject.toml` with `asyncio_mode=auto` and test markers.

---

## [2.9.5] - 2026-03-22

### Fixed — Final consistency pass (Round 5)
- **Landing page speed** — Added `preconnect` hints for Google Fonts, gstatic, and jsDelivr CDN; deferred Bootstrap JS (was the only template still blocking)
- **Standalone template consistency** — Added `preconnect` hints to all 4 standalone templates (login, register, api_dashboard, webhook_playground) that load fonts without extending base.html

---

## [2.9.4] - 2026-03-22

### Fixed — Final cleanup (Round 4)
- **crypto_service: blocking `requests.post()`** — Fallback RPC test now wrapped in `asyncio.to_thread()` to avoid blocking event loop when httpx is unavailable
- **bot_service: silent SSE exception** — `except Exception: pass` replaced with `logger.debug()` for SSE push setup failures (was invisible in logs)
- **analytics: silent action items query failure** — Now logs a warning instead of silently defaulting to 0 (misleading analytics data)
- **webhook_service: lock eviction race condition** — LRU eviction now skips locks that are currently held (`lock.locked()` check), preventing in-flight delivery corruption

---

## [2.9.3] - 2026-03-22

### Fixed — Performance & Reliability (Round 3)
- **browser_bot.py: async HTML writes** — `_screenshot()` HTML dump now uses `asyncio.to_thread()` instead of blocking `write_text()` (unblocks event loop during 1-5MB writes)
- **browser_bot.py: caption_log memory leak** — Truncation to last 40 entries now runs unconditionally, not just when captions are non-empty (prevented unbounded growth during silent periods)
- **store.py: startup OOM prevention** — `load_persisted_bots()` capped at 10k rows; `load_persisted_webhooks()` filters to `is_active=True` only (was loading entire table)
- **store.py: lock contention** — `_persist_bot()` now builds dict inside lock but does JSON serialization outside (avoids blocking other bot operations during slow serialize of large transcripts)
- **main.py: parallel startup** — Bot restore, webhook restore, and USDC monitor now run concurrently via `asyncio.gather()` (cuts startup time from sequential ~15s to parallel ~5s)

---

## [2.9.2] - 2026-03-22

### Fixed — Production Bugs & Performance (Round 2)
- **Admin analytics crash** — `settings` and `func` now imported at module level (was `NameError` at runtime); initial account queries wrapped in try-except with rollback; bot snapshots query capped at 50k rows (was unlimited — OOM risk)
- **Credit deduction race condition** — `deduct_credits_for_bot()` now uses `SELECT ... FOR UPDATE` to prevent two concurrent bot completions from reading the same balance
- **Claude API timeout** — `messages.stream()` now has `timeout=300s`; `messages.create()` has `timeout=60s` (was indefinite — hung event loop)
- **Integration HTTP client waste** — Linear and Jira integrations now reuse the global `_http_client` instead of creating new `httpx.AsyncClient` per request
- **USDC monitor crash loop** — `_monitor_loop()` now uses exponential backoff (60s → 1h cap) instead of fixed 60s retry on all errors
- **Bootstrap version mismatch** — `share.html` upgraded from 5.3.0 to 5.3.2 (matches all other templates)
- **Render-blocking scripts** — Added `defer` to Bootstrap JS in `login.html` and `register.html`

---

## [2.9.1] - 2026-03-22

### Fixed — Performance & Reliability
- **Memory leak: webhook locks** — `_webhook_locks` dict now uses LRU-bounded `OrderedDict` (max 500 entries) to prevent unbounded growth
- **Race condition: duplicate analysis** — `_analysis_in_flight` check-and-add now protected by `asyncio.Lock` (TOCTOU fix)
- **N+1 query: action items upsert** — Single batch `SELECT ... WHERE hash IN (...)` replaces per-item query loop
- **N+1 query: retention policy enforcement** — Pre-loads all per-account policies in one query; batch-deletes expired snapshots in single `DELETE ... WHERE id IN (...)`
- **Missing DB index** — Added `index=True` on `Webhook.is_active` (queried in admin analytics aggregations)
- **Store.list_bots() lock contention** — Filtering now happens inside the lock to avoid copying unneeded bots to a snapshot list
- **Missing timeout: Gemini transcription** — Added 5-minute `asyncio.wait_for` safety net on `generate_content_async()`
- **Missing timeout: SMTP email** — Added 30-second timeout on `asyncio.to_thread(_send)` to prevent indefinite hangs
- **Page load speed: base.html** — Added `preconnect` hints for Google Fonts and jsDelivr CDN; deferred Bootstrap JS loading
- **Admin email parsing** — Cached parsed `ADMIN_EMAILS` set instead of re-parsing on every admin request

---

## [2.9.0] - 2026-03-22

### Added
- **Python SDK: ~50 new methods** covering all API endpoints — bots (transcript, analyze, ask, highlights, share, follow-up email, rename speakers), webhooks (events list, test delivery), auth (get_me, test keys, account management), templates, analytics (dashboard, recurring, API usage, personal, search, audit log), action items, keyword alerts, calendar feeds, integrations, workspaces (full CRUD + member management), retention policies, and MCP tools
- **TypeScript SDK: ~50 new methods** mirroring all Python SDK additions with full type safety — new interfaces for all response types, param types, and camelCase method names
- **Workspace management UI** in the dashboard — replaces "coming soon" placeholder with full list view, create form, member management panel with add/remove/role-change, and permission-aware actions (owner vs admin vs member vs viewer)
- **Test infrastructure** — pytest + pytest-asyncio setup with shared fixtures (in-memory SQLite, app instance, authenticated client), 17 smoke tests across health, auth, bots, and webhooks

---

## [2.8.0] - 2026-03-22

### Added
- **Landing page mode switcher** — pill-style toggle in the hero section lets visitors choose between "For Teams" (calendar-first, dashboard-focused) and "For Developers" (API-first, code-focused) experiences
- Teams mode: simplified hero messaging, calendar auto-join flow, dashboard preview widget, UI-focused "How It Works" steps, and team-oriented CTA
- Developers mode: API-focused hero with curl quickstart widget, SDK/WebSocket/MCP platform pills, code-first "How It Works" steps, and developer-oriented CTA
- Mode preference persists via localStorage across visits
- Smooth fade-in transitions between modes

---

## [2.7.0] - 2026-03-21

### Fixed
- **OnePizza bot: complete rewrite of join + admission + alone detection** —
  - `_join_onepizza()`: Simplified from 100+ lines to 40. Uses meetingservice's auto-join (`?name=` URL param triggers automatic `joinMeeting()` call). Falls back to manual lobby flow only if auto-join doesn't fire.
  - `_wait_for_admission()`: Now checks `#meetingRoom` visible + `#lobby` hidden (the definitive meetingservice signals), instead of checking `#leaveBtn` + `#waitingRoomOverlay` which didn't match the actual DOM.
  - `_is_bot_alone()`: Now counts `#videoGrid > div` children instead of `.video-tile:not(.is-local)` which didn't match meetingservice's actual CSS classes.
  - `_END_TEXTS["onepizza"]`: Added "the meeting has ended" for completeness.
  - Mic/camera controls: Now uses in-call controls (`#micBtn`, `#camBtn`) instead of lobby controls, since auto-join skips the lobby.

### Added
- **INTEGRATION_GUIDE.md** — Complete guide for 1tab and other consumers: bot creation, webhook-driven status tracking (not polling), supported platforms, error handling, lifecycle documentation. Highlights that polling with 404 loops forever after server restarts.

---

## [2.6.0] - 2026-03-21

### Fixed
- **Admin analytics: 3 critical bugs** —
  1. `func.strftime` → `cast(col, Date)` — strftime is SQLite-only, doesn't exist in PostgreSQL (Railway production DB). Now uses SQLAlchemy `cast(col, Date)` which works on both.
  2. `NameError: settings` → `from app.config import settings as _settings` — settings wasn't imported in the analytics function scope
  3. `InFailedSQLTransactionError` cascade — Added `await db.rollback()` in each except block so a failed billing query doesn't corrupt the DB transaction for subsequent webhook/action-item queries

---

## [2.5.9] - 2026-03-21

### Fixed
- **OnePizza lobby-skip handling** — When the room is already active, the page skips the lobby (`#lobby` hidden) and shows the meeting room (`#meetingRoom`) directly. Bot now detects both states: if `#meetingRoom` or `#videoGrid` is visible, skip lobby flow entirely. Prevents the 30s timeout on invisible `#lobbyName` that was causing join failures on active rooms.

---

## [2.5.8] - 2026-03-21

### Fixed
- **OnePizza join button disabled** — The lobby disables the join button until a name is entered; the `?name=` URL param doesn't always auto-populate. Now force-fills the name field, waits 0.5s for UI to react, and retries up to 3x if button is disabled (re-filling name each time). Added logging at every step for diagnostics.

---

## [2.5.7] - 2026-03-21

### Fixed
- **OnePizza bot join — Socket.IO compatibility** — `networkidle` never resolves on Socket.IO pages (WebSocket keeps connection open); switched to `load` + explicit `wait_for_selector("#lobby, #lobbyJoinBtn, #lobbyName", state="visible")` with 30s timeout; join button now waits for visibility before clicking

---

## [2.5.6] - 2026-03-21

### Fixed
- **OnePizza bot join failure** — Changed `page.goto` from `domcontentloaded` to `networkidle` to wait for SPA JavaScript to render lobby elements; increased lobby selector timeout from 15s to 20s; increased join button click timeout from 4s to 10s; added extra selectors (`button:has-text('join')`, `[data-action='join']`)

---

## [2.5.5] - 2026-03-21

### Fixed
- **OnePizza platform detection** — Added `meetingservice-production.up.railway.app` to the onepizza netloc set so bots correctly identify the platform instead of falling back to "unknown" demo mode

---

## [2.5.4] - 2026-03-20

### Fixed
- **Admin analytics error visibility** — Proxy endpoint now returns the actual exception type and message instead of generic "Internal Server Error", enabling debugging

---

## [2.5.3] - 2026-03-20

### Fixed
- **Admin analytics crash** — `func.date()` is PostgreSQL-only; replaced with `func.strftime('%Y-%m-%d', ...)` for SQLite compatibility
- **Graceful degradation** — All new analytics queries (billing, webhooks, action items) wrapped in try/except so individual query failures don't crash the entire analytics endpoint

---

## [2.5.2] - 2026-03-20

### Fixed
- **Landing page**: Renamed misleading `--dark` CSS variables to semantic `--bg`, `--bg-alt`, `--bg-muted` names — all resolve to white/light gray, eliminating any confusion about theme
- **Dashboard analytics consolidated**: Replaced bare 4-KPI section + external `/api-dashboard` links with 3 tabbed sub-sections (Overview, API Usage, Trends) that load data inline via existing REST endpoints
- **Removed external link fragmentation**: No more links to separate `/api-dashboard` page — all analytics data is now accessible within the dashboard's own Analytics tab

### Changed
- **Dashboard Analytics → Overview tab**: Monthly bots, integrations, calendar feeds, API keys + recent bot performance table (server-rendered)
- **Dashboard Analytics → API Usage tab**: Bots 7d/30d, tokens, cost, error rate, platform breakdown, tokens by operation (lazy-loaded from `/api/v1/analytics/api-usage`)
- **Dashboard Analytics → Trends tab**: Monthly meetings, AI cost, action items, avg duration, sentiment trend, cost by platform (lazy-loaded from `/api/v1/analytics/me`)

---

## [2.5.1] - 2026-03-20

### Changed
- **Analytics reorganized with tabbed sub-navigation** — All analytics data consolidated into 5 clear tabs: Overview, AI & Costs, Errors & Health, Features, Users
- **Admin Overview slimmed** — Removed duplicated Platform Features card and Bot Breakdown (same data now lives in Analytics tabs)
- **System Status moved** — Runtime metrics (running tasks, queue depth) relocated from Analytics to the System tab where they belong
- **Chart.js resize fix** — Charts properly resize when switching between hidden/visible tabs

---

## [2.5.0] - 2026-03-20

### Changed
- **Modern light theme UI redesign** — Complete visual overhaul across all 8 user-facing templates:
  - **base.html** — New CSS variables, frosted glass navbar (white + backdrop blur), mobile hamburger menu, softer shadows and borders
  - **landing.html** — Converted from full dark (#0a0f1e) to clean white/light gray with subtle indigo accents
  - **login.html & register.html** — Dark left panels replaced with soft indigo gradient, dark text
  - **admin.html** — Dark navy sidebar converted to white with light borders and indigo active states
  - **dashboard.html** — Sidebar polish, horizontal scrollable pill nav on mobile, sticky positioning
  - **webhook_playground.html & api_dashboard.html** — Light navbar and background
- **Mobile responsiveness** — Added hamburger menu to all pages, horizontal scrollable sidebar on mobile, proper sticky positioning, tablet breakpoints
- **Design system** — Updated color palette: softer borders (#e5e7eb), subtle shadows, 14px body text, rounded corners (14px cards)

---

## [2.4.0] - 2026-03-20

### Added
- **Comprehensive admin analytics** — Expanded `/admin#analytics` with 6 new visualization sections:
  - **Status & Plan distribution** — Horizontal bar chart of bot statuses + doughnut of plan tiers
  - **Revenue & Billing** — 30-day daily revenue line chart + credit flow breakdown (added/consumed/net by type)
  - **Error analysis** — Errors by platform bar chart + top 10 error messages table
  - **Webhook health** — Delivery success rate, status breakdown, recent failures list
  - **Action items** — Total/open/done counts with completion rate progress bar
  - **Template & transcription** — Template usage table + Gemini vs Whisper doughnut
  - **System status** — Running tasks, queue depth, max concurrent, in-memory bots
- **Expanded KPI grid** — 13 cards (was 8): added Bots (7d), Avg Duration, Error Rate, Revenue (30d), and more
- **Backend analytics API extended** — `platform_analytics()` now returns billing, webhook, action item, error, and system data

---

## [2.3.1] - 2026-03-20

### Fixed
- **RBAC fails closed** — Workspace role check now returns 500 on DB errors instead of silently allowing access
- **Exception details no longer leaked** — Webhook delivery list endpoints return generic error, log details server-side
- **Calendar feed SSRF protection** — iCal URLs validated against private/reserved IPs (reuses webhook `_block_ssrf`)
- **Webhook state race condition** — Per-webhook locking prevents concurrent `dispatch_event` calls from corrupting `consecutive_failures` / `is_active`
- **Action item sub-user isolation** — New `sub_user_id` column + filtering so sub-users only see their own action items
- **Live transcript flush resilience** — Failed flush retries on next entry instead of losing the timestamp
- **SSE push error handling** — Fire-and-forget tasks wrapped in safe handler to prevent silent "Task exception never retrieved" warnings

### Changed
- **Bot queue** — Uses `collections.deque` (O(1) popleft) instead of `list.pop(0)` (O(n))
- **URL parsing** — Recurring meeting intelligence parses meeting URL once instead of 4 times
- **Screenshot pruning** — Runs at session start in addition to session end
- **VERSION file** — Fixed sync (was `2.2.0`, now matches actual version)
- **Pre-commit hook** — `.githooks/pre-commit` warns if VERSION/README/CHANGELOG are stale
- **CLAUDE.md** — Added mandatory pre-commit checklist for version and date updates

---

## [2.3.0] - 2026-03-19

### Added
- **Async dashboard — zero page reloads** — All 10 dashboard actions (API key create/revoke, webhook register, integration add/toggle/delete, calendar feed add/toggle/delete) now use `fetch()` + in-place DOM updates with toast notifications. No full-page reload occurs for any dashboard action.
- **`apiFetch()` helper** — Shared JS utility in `base.html` for all dashboard mutations. Automatically attaches `Content-Type: application/json` and `Accept: application/json` headers; throws on non-2xx responses with the server's `detail` message.
- **Browser back button support** — `switchSection()` now uses `window.history.pushState()` (not `replaceState`). A `popstate` listener restores the correct section when the user presses Back — the browser history stack works fully within the dashboard.
- **Schedule Bot in-place update** — After scheduling a bot, a `<tr>` is inserted into the bots table immediately without calling `window.location.reload()`.
- **Admin endpoint rate limiting** — `PUT /api/v1/admin/wallet`, `PUT /api/v1/admin/rpc-url`, and `POST /api/v1/admin/credit` are now limited to **10 requests/minute per IP**. `POST /api/v1/admin/usdc/rescan` is limited to **5/minute**. Returns HTTP 429 when exceeded.
- **Webhook replay protection** — Signed webhook deliveries now include an `X-MeetingBot-Timestamp` header alongside `X-MeetingBot-Signature`. The HMAC is computed over `{timestamp}.{body}` instead of just `{body}`. Recipients should reject deliveries where `abs(now - timestamp) > 300 seconds`.

### Changed
- **Webhook HMAC format (BREAKING)** — The signed payload is now `f"{timestamp}.{body}"`. Update your HMAC verification to extract `X-MeetingBot-Timestamp`, prepend it to the body, and verify the combined string. The 5-minute replay window is enforced server-side on delivery; recipients are responsible for enforcing it client-side.
- **Bot queue latency: 10 s → near-zero** — The queue processor previously polled every 10 seconds with `asyncio.sleep(10)`. It now wakes immediately via `asyncio.Event` when a bot is enqueued. Slots are filled in under 100 ms.
- **Analytics response caching** — `GET /api/v1/analytics` results are cached for **30 seconds** per account. `GET /api/v1/analytics/api-usage` results are cached for **60 seconds**. Reduces `list_bots()` calls under high-frequency polling.

### Fixed
- **WebSocket DB error now fails explicitly** — Previously, a database error during token lookup would return `None`, which was indistinguishable from an unknown/invalid token. Now returns close code **4503 (Service Temporarily Unavailable)** with reason `"Service temporarily unavailable"` so clients can distinguish a transient DB failure from a bad token.
- **Calendar feed dedup memory leak** — The `_dispatched` set grew unbounded across long-running instances. Changed to a `dict[key, float]` with a 48-hour TTL. A prune sweep runs every 288 poll cycles (~24 h at default 5-minute intervals). Memory is now bounded.
- **Dashboard JSON response branches** — All 10 `POST /dashboard/*` handlers now return `JSONResponse` when the request includes `Accept: application/json`, enabling the new async fetch flow. The redirect-based form flow is preserved for non-JS clients.

---

## [2.2.0] - 2026-03-17

### Added
- **Split API documentation** — public Swagger UI at `/api/docs` exposes only user-facing endpoints (admin-only routes, platform analytics, and `ai_usage` cost fields are hidden). Full schema including all admin endpoints and AI cost data is available at `/api/v1/admin/docs` (admin accounts only)
- **`/bot/{id}` session viewer** — new web UI page showing transcript, AI analysis (summary, key points, action items, decisions, next steps, sentiment, topics), speaker breakdown, chapters, meeting metadata, and download links for audio/video/markdown/PDF
- **`GET /api/v1/templates/default-prompt`** — returns the raw default analysis prompt so callers can inspect or extend it before passing `prompt_override`
- **`GET /api/v1/search`** — full-text search across all transcripts in memory; query param `q` returns matching snippets with bot context
- **Modern landing page** — public marketing homepage at `/` replacing the previous redirect; shows features, quick-start examples, and sign-up CTA. Authenticated users are auto-redirected to `/dashboard`
- **Dashboard redesign** — full account management in the dashboard: API key copy-to-clipboard, integrations (Slack/Notion) add/pause/delete, calendar feed add/pause/remove, notification preferences, and recent bots overview — all without leaving the page

### Fixed
- **Startup hang fix** — asyncpg now uses a 10 s connection timeout; `create_all_tables()`, `load_persisted_bots()`, and `load_persisted_webhooks()` wrapped in `asyncio.wait_for()` so the server always becomes ready (and `/health` always responds) even when the database is temporarily unavailable at boot
- **DB startup retry** — `create_all_tables()` is retried up to 5 times with a 5 s delay between attempts (handles Railway where the PostgreSQL container starts in parallel with the app container)
- **PostgreSQL migration compatibility** — schema migration `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` now uses a `try/except` guard compatible with older asyncpg/PostgreSQL versions

## [2.2.0] - 2026-03-16

### Added
- **Business accounts** — new `account_type` field (`personal` or `business`) for platforms integrating MeetingBot on behalf of multiple end-users. Business accounts use a single API key and credit balance but can completely isolate data between end-users via the `X-Sub-User` header
- **Sub-user data isolation** — pass `X-Sub-User: <user-id>` header (or `sub_user_id` in bot creation body) to scope all bot data to a specific end-user. Users cannot see each other's bots, transcripts, or analyses. Omit the header for an account-wide view
- **Copy-to-clipboard for API keys** — clipboard icon beside each API key in the dashboard, with visual feedback on copy. Newly created keys show the full key once with a prominent copy button
- Account type selection on the registration page (Personal / Business)
- Business account info card on the dashboard with integration examples
- Account type column in the admin panel's user accounts table
- `sub_user_id` field in bot creation, bot response, and bot summary schemas

### Changed
- `POST /api/v1/auth/register` now accepts `account_type` field (`personal` | `business`)
- `GET /api/v1/auth/me` now returns `account_type` in the response
- All bot endpoints (`GET`, `DELETE`, transcript, recording, analyze, ask, highlight, follow-up email) now respect `X-Sub-User` header for data isolation
- Bot list and stats endpoints filter by `sub_user_id` when the header is present

---

## [2.1.0] - 2026-03-16

### Added
- **Admin interface** — platform administration panel at `/admin` (web UI) and `/api/v1/admin/*` (API), restricted to admin accounts only
- **Platform USDC collection wallet** — admins can set/change a single Ethereum wallet address where all users send USDC via `PUT /api/v1/admin/wallet` or the admin web UI
- **User wallet registration** — users register their Ethereum wallet on their account (`PUT /api/v1/auth/wallet`). The USDC monitor matches the `from` address of incoming transfers to the platform wallet against registered user wallets, automatically crediting the correct account
- **Admin API endpoints:** `GET /api/v1/admin/wallet`, `PUT /api/v1/admin/wallet`, `GET /api/v1/admin/config`
- **User wallet endpoints:** `GET /api/v1/auth/wallet`, `PUT /api/v1/auth/wallet`
- **Admin access control** — only `assad.dar@gmail.com` or accounts with `is_admin=true` can access admin endpoints and the `/admin` page; all others receive HTTP 403
- `is_admin` and `wallet_address` fields on Account model
- `PlatformConfig` database model for storing platform-level key/value settings
- Admin nav link (visible only to admin users) in the web UI navbar
- Wallet registration card on the user dashboard
- Wallet status shown on the top-up page with warnings if not registered

### Changed
- `GET /api/v1/billing/usdc/address` now returns the admin-configured platform wallet when set (with the user's registered wallet info), falling back to HD-derived per-user addresses
- USDC transfer monitor now supports two modes: platform wallet (matches `from` address to user wallets) and HD wallet (matches `to` address to per-user deposit addresses)
- Top-up page (`/topup`) shows the platform wallet when configured by an admin, with user wallet status
- `CRYPTO_HD_SEED` is no longer required for USDC deposits if a platform wallet is set via the admin panel
- `GET /api/v1/auth/me` now includes `wallet_address` in the response

---

## [1.5.1] - 2026-03-14

### Fixed
- Silent audio capture: disabled out-of-process audio service, corrected PulseAudio sink volume, fixed VAD streaming loop reliability
- Caption scraping failure and audio silence in Google Meet sessions
- VAD streaming loop now always runs when Gemini is available
- Removed blocking `socket.getaddrinfo()` DNS lookup from `WebhookCreate` Pydantic validator — the synchronous call was blocking the async event loop and raising "Network is unreachable" when DNS was unavailable (same fix previously applied to the bot URL validator)
- `POST /api/v1/bot` now returns HTTP 503 with a clear diagnostic message when the database is unreachable (e.g. misconfigured `DATABASE_URL` or Supabase credentials), instead of the opaque "Database error: [Errno 101] Network is unreachable"

---

## [1.4.0] - 2026-03-07

### Added
- **AI usage tracking** — every bot response now includes a full `ai_usage` breakdown: tokens, cost, provider, model, and per-operation timing
- **Stripe billing** — flat per-meeting fees, per-token usage billing, and a cost-markup multiplier; checkout and subscription endpoints added
- **Claude API integration** — `ANTHROPIC_API_KEY` enables `claude-opus-4-6` for meeting analysis; takes precedence over Gemini when both keys are set
- `GET /api/v1/billing/usage` — aggregated AI usage across all meetings
- `GET /api/v1/billing/meeting/{bot_id}` — per-meeting charge breakdown
- `POST /api/v1/billing/checkout` — Stripe one-time payment checkout
- `POST /api/v1/billing/subscribe` — Stripe metered subscription checkout
- `POST /api/v1/billing/webhook` — Stripe webhook handler

### Changed
- Frontend served from the correct `FRONTEND_DIR` path
- All new billing and usage panels added to the web UI

---

## [1.3.0] - 2026-02-28

### Added
- **Third-party integrations** — Slack, Notion, Linear, Jira, HubSpot post-meeting push
- **PDF and Markdown export** — `GET /api/v1/bot/{id}/export/pdf` and `/export/markdown`
- **Speaker profiles** — auto-created after each meeting; cross-meeting stats (talk time, meeting count, questions asked); CRUD endpoints under `/api/v1/speakers`
- **Bot queue** — `MAX_CONCURRENT_BOTS` (default 3) limits simultaneous bots; extras queue and start automatically when a slot opens
- **AI tools** — follow-up email draft (`POST /api/v1/bot/{id}/followup-email`), pre-meeting brief (`POST /api/v1/bot/{id}/brief`), recurring meeting intelligence (`GET /api/v1/bot/{id}/recurring`)
- **Ask Anything** — `POST /api/v1/bot/{id}/ask` for free-form transcript Q&A
- **Share links** — unique `share_token` per bot; public read-only report at `GET /api/v1/share/{token}`
- **Recording download** — `GET /api/v1/bot/{id}/recording` to retrieve raw WAV audio
- Hardened security: SSRF DNS checks on webhook URLs and meeting URLs, LIKE-escape injection fix, parallel webhook broadcasts

### Fixed
- SQLite-incompatible pool args removed from async engine
- SMTP calls moved to thread pool to avoid event loop freeze
- DB indexes added for status, `created_at`, `meeting_url`, `share_token`

---

## [1.2.0] - 2026-02-14

### Added
- **Weekly digest email** — sent every Monday 09:00 UTC; requires `SMTP_HOST` and `DIGEST_EMAIL`
- **Recording retention** — auto-deletes WAV files older than `RECORDING_RETENTION_DAYS` (default 30) via daily background job at 03:00 UTC
- **Calendar auto-join** — iCal feed polled every 5 min; set `CALENDAR_ICAL_URL` to auto-dispatch bots to upcoming meetings
- **APScheduler** — background job scheduler managing digest, cleanup, and calendar sync tasks
- **10 built-in meeting templates** — Default, Sales Call, Daily Standup, 1:1, Sprint Retro, Client Kickoff, All-Hands, Incident Post-Mortem, Interview/Hiring, Design Review
- **Customized template** — `seed-customized` + `prompt_override` for inline one-off prompts without saving a template
- `GET /api/v1/templates/default-prompt` — returns the raw default analysis prompt as a starting point
- **Action item tracking** — cross-meeting action items stored in DB; `GET /api/v1/action-items`, `PATCH` to update, `GET /api/v1/action-items/stats`
- **Full-text search** — `GET /api/v1/search?q=` across all transcripts with highlighted snippets
- **Analytics** — `GET /api/v1/analytics` returns sentiment distribution, meetings per day, top topics, top participants, platform breakdown
- **Highlights** — bookmark transcript moments via `POST/GET/DELETE /api/v1/bot/{id}/highlight`
- **Mobile-responsive UI** — hamburger sidebar, full mobile layout

### Changed
- Parallel AI analysis pipeline (summary, action items, chapters run concurrently)
- Faster transcription with reduced latency
- UI auto-polls for bot status updates

### Fixed
- Mobile sidebar hide/show with `display:none` and fixed overlay
- Custom radio pickers in Deploy Bot modal
- Mode pill selection reliability

---

## [1.1.0] - 2026-01-31

### Added
- **Gemini Live API** — real-time bidirectional audio streaming using `google-genai>=1.0.0`
- **Live transcription** — `live_transcription: true` transcribes audio in 15-second rolling chunks during the call; enables voice-based bot-name detection without DOM captions
- **Voice mention responses** — `respond_on_mention`, `mention_response_mode` (`text` / `voice` / `both`), `tts_provider` (`edge` / `gemini`)
- **Microsoft Edge TTS** (`edge-tts`) — fast (~300 ms) voice replies with no extra API key
- **Gemini TTS** — more natural voice via `gemini-2.5-flash-preview-tts`
- **`start_muted`** — controls whether the bot joins with its microphone muted
- Bot join retry logic — `BOT_JOIN_MAX_RETRIES` and `BOT_JOIN_RETRY_DELAY_S`
- `cancelled` bot status — `DELETE /api/v1/bot/{id}` triggers graceful shutdown with background transcript + analysis
- Debug screenshots — `GET /api/v1/debug/screenshots` to inspect join failures
- WebSocket real-time events at `ws://localhost:8080/ws`
- Bearer token auth via `API_KEY` environment variable
- `extra_metadata` arbitrary JSON field on bots

### Fixed
- Gemini Live session invalid argument (error 1007)
- Live transcription audio overlap and real-time frontend display
- Caption detection reliability improvements
- Railway deployment: nixpacks.toml, Procfile, Dockerfile auto-detection

---

## [1.0.0] - 2026-01-15

### Added
- Initial release
- Playwright-based browser bot joins **Google Meet**, **Zoom**, and **Microsoft Teams** as a guest
- ffmpeg + PulseAudio audio capture
- Gemini transcription and AI analysis (summary, key points, action items, decisions, next steps, sentiment, topics, chapters, speaker stats)
- `analysis_mode: "transcript_only"` to skip AI analysis and return raw transcript only
- Bot lifecycle: `joining` → `in_call` → `call_ended` → `done` / `error`
- REST API at `/api/v1` with Swagger UI at `/api/docs`
- `POST /api/v1/bot` — create bot (join meeting)
- `GET /api/v1/bot/{id}` — get bot status, transcript, and analysis
- `GET /api/v1/bot/{id}/transcript` — transcript only
- `POST /api/v1/bot/{id}/analyze` — re-run analysis on demand
- `GET /api/v1/bot` — list bots with status filter
- `DELETE /api/v1/bot/{id}` — stop bot
- `GET /api/v1/bot/stats` — aggregate statistics
- `POST/GET/DELETE /api/v1/webhook` — webhook registration and delivery
- `POST /api/v1/webhook/{id}/test` — test webhook endpoint
- CORS support with `CORS_ORIGINS`
- Docker Compose deployment
- Railway deployment config
- SQLite (WAL mode) with SQLAlchemy async
- `vocabulary` field for transcription hints
- SSRF protection on meeting URLs (blocks private/loopback ranges)
- Web UI with Reports, Search, Action Items, Templates, Analytics, Webhooks, Debug, Speakers tabs
