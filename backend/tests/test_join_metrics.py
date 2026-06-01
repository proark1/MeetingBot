"""Tests for join-reliability metrics (self-healing-join observability).

These counters are the earliest signal of browser_bot selector drift: a rising
failure rate for a platform means its UI likely changed. We verify the helpers
increment the right labels (skipped gracefully if prometheus_client is absent).
"""
import pytest

prometheus = pytest.importorskip("prometheus_client")

from app.api import metrics


def _counter_value(counter, **labels) -> float:
    """Read a labelled counter's current value from the prometheus registry."""
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total") and sample.labels == labels:
                return sample.value
    return 0.0


def test_record_join_attempt_increments_platform():
    before = _counter_value(metrics.bot_join_attempts_total, platform="zoom")
    metrics.record_join_attempt("zoom")
    after = _counter_value(metrics.bot_join_attempts_total, platform="zoom")
    assert after == before + 1


def test_record_join_result_success_and_failure_labels():
    s_before = _counter_value(metrics.bot_join_results_total, platform="google_meet", result="success")
    f_before = _counter_value(metrics.bot_join_results_total, platform="google_meet", result="failure")

    metrics.record_join_result("google_meet", success=True)
    metrics.record_join_result("google_meet", success=False)

    s_after = _counter_value(metrics.bot_join_results_total, platform="google_meet", result="success")
    f_after = _counter_value(metrics.bot_join_results_total, platform="google_meet", result="failure")

    assert s_after == s_before + 1
    assert f_after == f_before + 1


def test_helpers_never_raise():
    # Must be safe to call from the hot path regardless of label values.
    metrics.record_join_attempt("onepizza")
    metrics.record_join_result("onepizza", success=True)
    metrics.record_bot_created("teams")
    metrics.record_bot_completed("error")
