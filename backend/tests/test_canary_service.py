"""Tests for the canary's health-evaluation brain and config loading."""

from app.services import canary_service as C


def _healthy_outcome():
    return {
        "status": "done",
        "admitted": True,
        "exit_reason": "ended",
        "transcript_len": 12,
        "participants_count": 2,
        "audio_peak": 4000,
        "error": None,
    }


def test_all_checks_pass_on_healthy_outcome():
    cfg = C.CanaryConfig(require_participants=True)
    report = C.evaluate_bot_outcome("google_meet", _healthy_outcome(), cfg)
    assert report.ok
    assert all(report.checks.values())
    assert "PASS" in report.summary()


def test_silent_recording_fails_audio_check():
    cfg = C.CanaryConfig()
    out = _healthy_outcome()
    out["audio_peak"] = 5  # below min_audio_peak (200)
    report = C.evaluate_bot_outcome("zoom", out, cfg)
    assert not report.ok
    assert report.checks["audio"] is False
    assert report.checks["transcript"] is True


def test_no_transcript_fails_when_required_but_ok_when_not():
    out = _healthy_outcome()
    out["transcript_len"] = 0
    assert not C.evaluate_bot_outcome("zoom", out, C.CanaryConfig(require_transcript=True)).ok
    assert C.evaluate_bot_outcome(
        "zoom", out, C.CanaryConfig(require_transcript=False, require_participants=False)
    ).ok


def test_error_status_fails_completed_and_clean_exit():
    out = _healthy_outcome()
    out.update(status="error", exit_reason=None, error="join failed")
    report = C.evaluate_bot_outcome("microsoft_teams", out, C.CanaryConfig(require_audio=False, require_transcript=False))
    assert not report.ok
    assert report.checks["completed"] is False
    assert report.checks["clean_exit"] is False


def test_admitted_requirement_toggle():
    out = _healthy_outcome()
    out["admitted"] = False
    assert not C.evaluate_bot_outcome("zoom", out, C.CanaryConfig()).ok
    # not required -> the admitted check is absent
    rep = C.evaluate_bot_outcome("zoom", out, C.CanaryConfig(require_admitted=False))
    assert "admitted" not in rep.checks


def test_config_from_env_parses_urls_and_toggles():
    env = {
        "CANARY_BASE_URL": "https://api.example.com",
        "CANARY_API_KEY": "sk_live_x",
        "CANARY_MEET_URL": "https://meet.google.com/abc",
        "CANARY_ZOOM_URL": "https://zoom.us/j/1",
        "CANARY_REQUIRE_AUDIO": "false",
        "CANARY_MIN_AUDIO_PEAK": "500",
    }
    cfg = C.CanaryConfig.from_env(env)
    assert cfg.base_url == "https://api.example.com"
    assert cfg.meeting_urls == {
        "google_meet": "https://meet.google.com/abc",
        "zoom": "https://zoom.us/j/1",
    }
    assert cfg.require_audio is False
    assert cfg.min_audio_peak == 500


async def test_run_canary_uses_injected_runner_and_aggregates():
    cfg = C.CanaryConfig(meeting_urls={"zoom": "https://zoom.us/j/1"}, require_participants=False)

    async def fake_runner(platform, url, cfg):
        return _healthy_outcome()

    reports = await C.run_all(cfg, runner=fake_runner)
    assert len(reports) == 1 and reports[0].ok and reports[0].platform == "zoom"


async def test_run_canary_handles_runner_exception():
    cfg = C.CanaryConfig(meeting_urls={"zoom": "https://zoom.us/j/1"})

    async def boom(platform, url, cfg):
        raise RuntimeError("api unreachable")

    report = await C.run_canary("zoom", "https://zoom.us/j/1", cfg, runner=boom)
    assert not report.ok and report.checks == {"ran": False}
