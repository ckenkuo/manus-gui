"""描述区文字模块处理的单测（阶段⑬）。

用户 2026-09-01 要求：描述长图里的【所有文字板块一律移除】。此前是交 LLM 判
「采集残留 JSON 删 / 尺码对照表英化保留」，现在不留保留分支，故原先针对
vision.plan_desc_text 的测试随该函数一起删除，这里只覆盖删除路径。

desc_text_delete_all 走浏览器，mock 掉 session 只验证枚举、顺序与失败隔离。
"""

from publish_patching import patch_publish
import re

import pytest

from app.publish.pipeline import desc_text_delete_all


# 两类真实样本（都要删）：
# - 1688 关联商品 JSON 残留（真站探查 rowid 173539495454339053 的 data-idx=0）
# - 尺码对照表 `80【身高65-75cm】`（用户截图；现在也删，尺码走阶段⑧ 尺码表）
TEXTS = [
    {"idx": "0", "text": '{"styleType":"offer-type-1","items":"888688384773"}', "len": 50},
    {"idx": "3", "text": "80【身高65-75cm】\n90【身高75-85cm】", "len": 24},
]


class FakeSession:
    """记录 eval_json 收到的 JS，按内容返回成功结果。"""

    def __init__(self, fail_idx=None):
        self.calls = []
        self.fail_idx = fail_idx

    async def eval_json(self, js):
        self.calls.append(js)
        m = re.search(r'data-idx="\' \+ (\d+)', js) or \
            re.search(r'data-idx="(\d+)"', js)
        idx = m.group(1) if m else "?"
        if idx == self.fail_idx:
            return {"err": "模块上没有删除图标"}
        return {"deleted": True, "targetId": idx}

    @staticmethod
    def deleted_idxs(calls):
        out = []
        for js in calls:
            m = re.search(r"data-idx=\"' \+ (\d+)", js) or \
                re.search(r'data-idx="(\d+)"', js)
            if m:
                out.append(m.group(1))
        return out


@pytest.fixture(autouse=True)
def _stub_text_map(monkeypatch):
    """跳过真实的编辑器打开与页面枚举（那要连浏览器）。"""
    texts = list(TEXTS)

    async def fake_map(session):
        return {"status": "ok", "count": len(texts), "texts": texts}

    patch_publish(monkeypatch, "pipeline", "desc_text_map", fake_map)
    return texts


@pytest.mark.asyncio
async def test_deletes_every_text_module(_stub_text_map):
    """枚举到的每个文字模块都要删——包括尺码对照表这类「有信息」的。"""
    session = FakeSession()
    r = await desc_text_delete_all(session)
    assert r["status"] == "ok"
    assert r["found"] == 2
    assert sorted(r["deleted"]) == ["0", "3"]


@pytest.mark.asyncio
async def test_deletes_in_reverse_order(_stub_text_map):
    """按 data-idx 从大到小删——data-idx 删除后会重排，正序删会错位。"""
    _stub_text_map[:] = [{"idx": i, "text": "x", "len": 1} for i in ("0", "2", "5")]
    session = FakeSession()
    r = await desc_text_delete_all(session)
    assert r["deleted"] == ["5", "2", "0"]
    assert FakeSession.deleted_idxs(session.calls) == ["5", "2", "0"]


@pytest.mark.asyncio
async def test_failure_isolated(_stub_text_map):
    """单项失败记进 failed 并继续，不拖垮整体（描述文字非必填）。"""
    session = FakeSession(fail_idx="0")
    r = await desc_text_delete_all(session)
    assert r["status"] == "partial"
    assert r["deleted"] == ["3"]
    assert len(r["failed"]) == 1 and r["failed"][0]["idx"] == "0"


@pytest.mark.asyncio
async def test_no_text_module_is_noop(_stub_text_map):
    """描述区没有文字模块时一次页面操作都不该发出。"""
    _stub_text_map[:] = []
    session = FakeSession()
    r = await desc_text_delete_all(session)
    assert r == {"status": "ok", "deleted": [], "failed": [], "found": 0,
                 "texts": []}
    assert not session.calls


@pytest.mark.asyncio
async def test_map_error_propagates(monkeypatch):
    """枚举失败要原样上报，不能当成「没有文字模块」静默通过。"""
    async def fake_map(session):
        return {"status": "error", "err": "编辑器不在"}

    patch_publish(monkeypatch, "pipeline", "desc_text_map", fake_map)
    session = FakeSession()
    r = await desc_text_delete_all(session)
    assert r["status"] == "error" and "编辑器不在" in str(r)
    assert not session.calls


@pytest.mark.asyncio
async def test_returns_texts_for_size_evidence(_stub_text_map):
    """删除前枚举到的原文要一并回传：调用方靠它做尺码取证，删完就再也读不到了
    （见 service._st_desc 的 _size_evidence）。"""
    session = FakeSession()
    r = await desc_text_delete_all(session)
    assert [t["idx"] for t in r["texts"]] == ["0", "3"]
    assert "身高65-75cm" in r["texts"][1]["text"]


def test_size_evidence_gate_hits_only_without_measurements():
    """取证闸：info 里已有实测尺寸就不告警；没有且原文命中尺码词才留追溯线索。

    ①b（extract.enrich_desc_text）有两道跳过闸，命中时 sizeMeasurements 为空、
    ⑨ 尺码表全靠模型估算——那时描述文字若真写着尺码，删掉前必须留下原文。
    """
    from app.publish.service import _size_evidence

    # ①b 已抽到实测值：不必取证
    assert _size_evidence({"sizeMeasurements": {"S": {"衣长": 59}}}, TEXTS) == ""
    # 没有实测值 + 尺码对照表那条命中尺码词：留原文
    ev = _size_evidence({}, TEXTS)
    assert "idx=3" in ev and "身高65-75cm" in ev
    # 采集残留 JSON 不含尺码词：不误报
    assert _size_evidence({}, [TEXTS[0]]) == ""
