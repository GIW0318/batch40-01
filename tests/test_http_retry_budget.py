import pytest

import http_retry_budget as hrb


def planner(**kwargs):
    return hrb.RetryBudgetPlanner(**kwargs)


def request(**over):
    data = {
        "method": "GET",
        "idempotency_key": None,
        "body_replayable": True,
        "deadline": 100.0,
        "redirects": 0,
        "caller": "svc-a",
    }
    data.update(over)
    return data


def response(status=503, retry_after=None, committed=False):
    return {"status": status, "retry_after": retry_after, "committed": committed}


def test_post_without_idempotency_key_is_not_retried():
    result = planner().plan(request(method="POST"), response(500), 1, 0.0)
    assert result["retry"] is False
    assert result["reason"] == "not_idempotent"


def test_post_with_idempotency_key_is_retried():
    result = planner().plan(request(method="POST", idempotency_key="key-1"), response(500), 1, 0.0)
    assert result["retry"] is True


def test_patch_is_not_treated_as_idempotent():
    result = planner().plan(request(method="PATCH"), response(503), 1, 0.0)
    assert result["retry"] is False
    assert result["reason"] == "not_idempotent"


def test_get_retries_on_server_error():
    result = planner().plan(request(), response(503), 1, 0.0)
    assert result["retry"] is True
    assert 0.1 <= result["delay"] <= 0.15


def test_non_retryable_statuses_stop_immediately():
    engine = planner()
    for status in (400, 404, 501):
        result = engine.plan(request(), response(status), 1, 0.0)
        assert result["retry"] is False
        assert result["reason"] == "status"


def test_retry_after_overrides_local_backoff():
    result = planner().plan(request(), response(429, retry_after=2.0), 2, 0.0)
    assert result["retry"] is True
    assert result["delay"] == pytest.approx(2.0)


def test_backoff_grows_and_carries_jitter():
    engine = planner()
    first = engine.plan(request(), response(503), 1, 0.0)
    second = engine.plan(request(), response(503), 2, 0.0)
    assert 0.1 <= first["delay"] <= 0.15
    assert 0.2 <= second["delay"] <= 0.3


def test_committed_response_is_never_retried():
    result = planner().plan(request(), response(503, committed=True), 1, 0.0)
    assert result["retry"] is False
    assert result["reason"] == "committed"


def test_non_replayable_body_blocks_retry():
    result = planner().plan(
        request(method="POST", idempotency_key="key-2", body_replayable=False),
        response(503),
        1,
        0.0,
    )
    assert result["retry"] is False
    assert result["reason"] == "body_not_replayable"


def test_retry_budget_is_shared_per_caller():
    engine = planner(budget_ratio=0.1)
    for _ in range(10):
        engine.note_request("svc-a")
    assert engine.note_retry("svc-a") is True
    assert engine.note_retry("svc-a") is False
    for _ in range(10):
        engine.note_request("svc-b")
    assert engine.note_retry("svc-b") is True


def test_max_attempts_stops_further_retries():
    result = planner(max_attempts=3).plan(request(), response(503), 3, 0.0)
    assert result["retry"] is False
    assert result["reason"] == "max_attempts"


def test_deadline_blocks_retry():
    result = planner().plan(request(deadline=0.15), response(503), 2, 0.0)
    assert result["retry"] is False
    assert result["reason"] == "deadline"


def test_redirect_limit_is_tracked_separately():
    engine = planner(max_redirects=3)
    allowed = engine.plan(request(redirects=2), response(302), 1, 0.0)
    blocked = engine.plan(request(redirects=3), response(302), 1, 0.0)
    assert allowed["retry"] is False
    assert allowed["reason"] == "redirect"
    assert blocked["reason"] == "redirect_limit"


def test_half_open_allows_only_one_probe():
    engine = planner()
    assert engine.probe_allowed("downstream", "half_open", 0.0) is True
    assert engine.probe_allowed("downstream", "half_open", 0.1) is False
    engine.probe_finished("downstream")
    assert engine.probe_allowed("downstream", "half_open", 0.2) is True
    assert engine.probe_allowed("other", "open", 0.2) is False
    assert engine.probe_allowed("other", "closed", 0.2) is True


def test_attempts_and_requests_are_counted_separately():
    engine = planner()
    for _ in range(10):
        engine.note_request("svc-a")
    assert engine.note_retry("svc-a") is True
    stats = engine.stats()
    assert stats["requests"] == 10
    assert stats["attempts"] == 11
