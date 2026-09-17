"""按本次提交、前后快照及最新报名状态核验活动。"""

import math
from datetime import datetime, timezone


def query_complete(snapshot: dict, spu: str) -> bool:
    queries = [query for query in snapshot.get("queries", []) if str(query.get("spu")) == spu]
    return bool(queries) and all(
        query.get("complete", snapshot.get("complete", False)) and not query.get("error")
        for query in queries
    )


def enrollment_time(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return None
        return number / 1000 if number > 100000000000 else number
    except (ValueError, TypeError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError):
            return None


def _identity(record):
    enroll_id = record.get("enroll_id")
    if enroll_id not in (None, ""):
        return ("id", str(enroll_id))
    timestamp = enrollment_time(record.get("enroll_time"))
    return ("time", timestamp) if timestamp is not None else None


def _latest(records):
    if not records:
        return None, "报名记录页未查到该 SPU/活动的记录"
    if len(records) > 1:
        times = [enrollment_time(record.get("enroll_time")) for record in records]
        if any(timestamp is None for timestamp in times):
            identities = {_identity(record) for record in records}
            if None in identities or len(identities) != 1:
                return None, "同一活动存在多条记录，但报名时间不足以确定最新记录"
        else:
            newest = max(times)
            records = [record for record, timestamp in zip(records, times) if timestamp == newest]
        states = {(record.get("enroll_status"), record.get("success")) for record in records}
        identities = {_identity(record) for record in records}
        if len(states) != 1 or len(identities) != 1:
            return None, "最新报名记录的时间或状态存在冲突，无法确认"
    return records[0], ""


def assess_registration(baseline: dict, current: dict, spu: str, activity: str, attempted: bool) -> dict:
    """成功须有完整前后快照、实际提交及新增或重新报名的有效记录。"""
    outcome = {"ok": False, "status": "not_verified", "note": "", "record": None}
    if not query_complete(current, spu):
        return {**outcome, "note": "报名记录查询不完整，无法确认最新状态"}
    records = [record for record in current.get("records", [])
               if str(record.get("spu")) == spu and record.get("activity") == activity]
    latest, error = _latest(records)
    if error:
        return {**outcome, "note": error}
    outcome["record"] = latest
    if latest.get("exited") or latest.get("enroll_status") == 6:
        return {**outcome, "status": "exited", "note": "最新报名记录已退出"}
    if latest.get("enroll_status") not in {1, 3, 4} or not latest.get("success"):
        return {**outcome, "note": f"最新报名状态未标定或未成功（enrollStatus={latest.get('enroll_status')}）"}
    if not attempted:
        return {**outcome, "note": "本批未对该商品执行提交，现有记录不能证明本次报名成功"}
    if not query_complete(baseline, spu):
        return {**outcome, "note": "报名前基线不完整，无法区分历史记录和本次报名"}
    identity = _identity(latest)
    if identity is None:
        return {**outcome, "note": "报名记录缺少 ID 和有效报名时间，无法关联本次提交"}
    previous = [record for record in baseline.get("records", [])
                if str(record.get("spu")) == spu and record.get("activity") == activity]
    if any(_identity(record) is None for record in previous):
        return {**outcome, "note": "历史记录缺少标识，无法确认本次新增记录"}
    same = [record for record in previous if _identity(record) == identity]
    if same:
        before, error = _latest(same)
        if error:
            return {**outcome, "note": "报名前基线记录有冲突，无法确认本次更新"}
        before_time = enrollment_time(before.get("enroll_time"))
        after_time = enrollment_time(latest.get("enroll_time"))
        reenrolled = before.get("enroll_status") == 6 or before.get("exited")
        advanced = before_time is not None and after_time is not None and after_time > before_time
        if not reenrolled and not advanced:
            return {**outcome, "status": "historical", "note": "仅查到未更新的历史报名记录，未确认本次报名"}
    return {**outcome, "ok": True, "status": "success",
            "note": f"报名记录确认本次{'更新' if same else '新增'}成功（enrollId={latest.get('enroll_id')}）"}
