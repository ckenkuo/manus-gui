import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.activity import pipeline, service
from app.activity.reconciliation import assess_registration


def record(enroll_id="new", status=4, timestamp=2000, spu="111", activity="活动A", **extra):
    return pipeline._parse_activity_log_item({
        "productId": spu, "activityThematicName": activity,
        "enrollId": enroll_id, "enrollStatus": status, "enrollTime": timestamp, **extra,
    })


def snapshot(records=(), complete=True):
    return {
        "records": list(records), "complete": complete,
        "queries": [{"spu": "111", "complete": complete}],
    }


@pytest.mark.parametrize("status", [1, 3, 4, "1", "3", "4"])
def test_new_verified_record_is_success_even_with_session_failure(status):
    current = record(status=status, assignSessionList=[{"sessionFailReason": "个别场次不符合"}])
    outcome = assess_registration(snapshot(), snapshot([current]), "111", "活动A", True)
    assert outcome["ok"] is True
    assert outcome["record"]["session_failures"] == ["个别场次不符合"]


@pytest.mark.parametrize("status", [6, "6", None, "", 99, "99", "invalid", True])
def test_exited_and_unknown_states_never_count_as_success(status):
    current = record(status=status)
    assert current["success"] is False
    assert not assess_registration(snapshot(), snapshot([current]), "111", "活动A", True)["ok"]


def test_current_exit_overrides_same_id_success_in_baseline():
    before = record("same", 4)
    after = record("same", "6")
    outcome = assess_registration(snapshot([before]), snapshot([after]), "111", "活动A", True)
    assert outcome["status"] == "exited"


def test_missing_current_record_cannot_be_filled_from_history():
    outcome = assess_registration(snapshot([record()]), snapshot(), "111", "活动A", True)
    assert not outcome["ok"]
    assert "未查到" in outcome["note"]


def test_unchanged_history_and_natural_status_progress_are_not_new_submission():
    before = record("same", 4)
    for status in [4, 1]:
        outcome = assess_registration(snapshot([before]), snapshot([record("same", status)]), "111", "活动A", True)
        assert outcome["status"] == "historical"


@pytest.mark.parametrize("before,after", [
    (record("same", 6), record("same", 4)),
    (record("same", 4, 1000), record("same", 4, 2000)),
])
def test_same_id_reenrollment_or_new_enrollment_time_is_confirmed(before, after):
    assert assess_registration(snapshot([before]), snapshot([after]), "111", "活动A", True)["ok"]


@pytest.mark.parametrize("current", [
    [record("old", 4, 1000), record("new", 6, 2000)],
    [record("new", 6, 2000), record("old", 4, 1000)],
    [record("old", 4, 1000), record("new", 99, 2000)],
])
def test_older_success_never_masks_latest_exit_or_unknown(current):
    assert not assess_registration(snapshot(), snapshot(current), "111", "活动A", True)["ok"]


@pytest.mark.parametrize("current", [
    [record("old", 4, None), record("new", 4, 2000)],
    [record("same", 4), record("same", 6)],
    [record("first", 4), record("second", 4)],
    [record(None, 4, None)],
])
def test_ambiguous_identity_time_or_status_is_not_success(current):
    assert not assess_registration(snapshot(), snapshot(current), "111", "活动A", True)["ok"]


@pytest.mark.parametrize("baseline,current,attempted", [
    (snapshot(complete=False), snapshot([record()]), True),
    (snapshot(), snapshot([record()], complete=False), True),
    (snapshot(), snapshot([record()]), False),
    (snapshot(), snapshot([record(spu="222")]), True),
    (snapshot(), snapshot([record(activity="活动B")]), True),
])
def test_complete_queries_exact_pair_and_submit_attempt_are_required(baseline, current, attempted):
    assert not assess_registration(baseline, current, "111", "活动A", attempted)["ok"]


def test_other_spu_query_failure_does_not_invalidate_complete_spu():
    current = snapshot([record()])
    current["complete"] = False
    current["queries"].append({"spu": "222", "complete": False})
    assert assess_registration(snapshot(), current, "111", "活动A", True)["ok"]


def reconcile(monkeypatch, baseline, responses):
    reader = AsyncMock(side_effect=responses)
    monkeypatch.setattr(pipeline, "read_activity_log_records", reader)
    monkeypatch.setattr(service, "LOG_VERIFY_RETRY_DELAY", 0)
    plans = [{"spu": "111", "accel_state": "off", "enrolled_activities": [
        {"activity": "活动A", "submit_price": 10},
    ]}]
    summary = {
        "log_baseline": baseline, "submitted_attempts": {"活动A": ["111"]},
        "enrolled_activities": {}, "ineligible_activities": {}, "activity_results": {},
        "failed": [], "errors": [],
    }
    events = []
    asyncio.run(service._reconcile_activity_log(plans, SimpleNamespace(context=object()), events.append, summary))
    return summary, events, reader, plans


def test_delayed_record_is_confirmed_on_retry(monkeypatch):
    summary, events, reader, _ = reconcile(monkeypatch, snapshot(), [snapshot(), snapshot([record()])])
    assert reader.await_count == 2
    assert summary["enrolled_activities"] == {"活动A": ["111"]}
    assert summary["failed"] == []
    assert len(summary["log_verification"]["attempts"]) == 2
    assert events[-1]["ok"] is True


def test_exit_is_not_hidden_by_baseline_and_blocks_success_outcome(monkeypatch):
    before = snapshot([record("same", 4)])
    after = snapshot([record("same", 6)])
    summary, events, reader, plans = reconcile(monkeypatch, before, [after] * 3)
    assert reader.await_count == 3
    assert summary["enrolled_activities"]["活动A"] == []
    assert summary["activity_results"]["活动A"]["verified"] is False
    assert summary["failed"] and "已退出" in summary["failed"][0]["reason"]
    assert not any(event.get("ok") for event in events if event["type"] == "exec_log_verify")
    assert service._execution_product_results(plans, summary, True)[0]["status"] == "fail"


def test_repeated_page_is_incomplete_even_when_raw_count_matches_total():
    first = {"total": 2, "pageSize": 1, "list": [{"enrollId": 1}]}
    next_page = AsyncMock(return_value={"total": 2, "list": [{"enrollId": 1}]})
    result = asyncio.run(pipeline._collect_activity_log_pages(first, next_page))
    assert result["complete"] is False
    assert "重复" in result["error"]


def test_total_change_and_missing_response_cannot_look_like_complete_query():
    first = {"total": 2, "pageSize": 1, "list": [{"enrollId": 1}]}
    next_page = AsyncMock(return_value={"total": 1, "list": [{"enrollId": 2}]})
    assert not asyncio.run(pipeline._collect_activity_log_pages(first, next_page))["complete"]
    with pytest.raises(ValueError, match="list/total"):
        asyncio.run(pipeline._collect_activity_log_pages({}, next_page))
