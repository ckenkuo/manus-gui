# -*- coding: utf-8 -*-
"""发布管线 service 层编排的离线单测（mock 浏览器/LLM，不碰 CDP、不碰网络）。

测的是编排逻辑本身：断点续跑的状态读写与跳过规则、from_stage 语义、
单阶段失败不拖垮批次、事件序列契约、vision 决策的解析与兜底。
真实页面交互已由各阶段自己的真站验证覆盖，这里全部换成假阶段函数。
"""
import json
import os
import asyncio

import pytest

from PIL import Image

from app.publish import service, vision

_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"A" * 40


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """状态文件和 prefs 重定向到临时目录，不污染真实 workspace/。"""
    monkeypatch.setattr(service, "STATE_DIR", str(tmp_path / "publish-state"))
    monkeypatch.setattr(service, "PREFS_PATH", str(tmp_path / "publish_prefs.json"))


def _fake_stages(monkeypatch, calls: list, fail_at: str = "", raise_at: str = ""):
    """把 15 个阶段全换成假函数，按调用顺序记录到 calls。"""
    for sid, _ in service.STAGES:
        async def fake(ctx, session, emit, _sid=sid):
            calls.append(_sid)
            if _sid == raise_at:
                raise RuntimeError("模拟页面爆炸")
            if _sid == fail_at:
                return {"status": "fail", "note": "模拟失败"}
            return {"status": "ok", "note": f"{_sid} done"}
        monkeypatch.setitem(service._STAGE_FUNCS, sid, fake)
    # 续跑补开编辑页（publish_one 在 claim/auto_cat 都跳过时调用）也换成假的，
    # 记录为 "open_edit" 便于断言调用时机
    async def fake_open_edit(session, rowid):
        calls.append("open_edit")
        return {"rowid": rowid}
    monkeypatch.setattr(service, "open_edit", fake_open_edit)

    # 续跑实况探测默认「表单成果都还在」：这些用例测的是状态文件的跳过规则本身，
    # 不该被实况判定干扰。测实况判定的用例自己覆盖 live_state（见 _fake_live）。
    async def fake_live(session):
        return {"rendered": True, "titleFilled": True, "skuRowCount": 8,
                "skuFilledRows": 8, "sizechartAdded": True, "attrImgCount": 6,
                "shippingSet": True, "descImgCount": 10, "descForeignCount": 0,
                "skuCodeCount": 8, "skuCodeBad": 0,
                # ⑦b 预览图达标（这份桩的语义是「成果都还在」）
                "previewCount": 8, "previewBad": 0,
                "catText": "女士运动卫裤", "catUnset": False, "catDeleted": False}
    monkeypatch.setattr(service, "live_state", fake_live)


def _fake_live(monkeypatch, **over):
    """覆盖实况探测返回值：默认「表单成果全丢」，按需覆盖单项。"""
    live = {"rendered": True, "titleFilled": False, "skuRowCount": 0,
            "skuFilledRows": 0, "sizechartAdded": False, "attrImgCount": 0,
            "shippingSet": False, "descImgCount": 12, "descForeignCount": 12,
            "skuCodeCount": 0, "skuCodeBad": 0,
            # ⑦b 预览图：「全丢」语义下这一列也是认领带来的 1688 原始破线图
            # （2026-08-30 玩具类那单实测 8 行里 3 行非 1:1 且短边不足 800）
            "previewCount": 8, "previewBad": 8,
            # ⑦a 剔配件色：默认按【没有配件色】的普通商品给（各色有效行数齐平），
            # 于是 drop_acc 默认不进重跑集——它只在真有配件色时才有事做，不像
            # ⑤~⑬ 那样「表单丢了就一定要重跑」。要测它的用例自己覆盖本字段。
            "variantByColor": {"白色": {"total": 5, "filled": 5},
                               "黑色": {"total": 5, "filled": 5}},
            # 类目默认有效：⑤ 起的表单丢了是常态，类目失效是另一类故障，
            # 由 test_类目失效时连带重跑类目与属性 单独覆盖
            "catText": "女士运动卫裤", "catUnset": False, "catDeleted": False}
    live.update(over)

    async def fake(session):
        return live
    monkeypatch.setattr(service, "live_state", fake)


def _collect(events: list):
    def cb(ev: dict):
        events.append(ev)
    return cb


def _collect_async(events: list):
    """阶段函数内部的 emit 是被 await 的（publish_one 里包了 async wrapper），
    单独测某个阶段函数时要给它 async 版，不能直接用 _collect。"""
    async def cb(ev: dict):
        events.append(ev)
    return cb


# ---- 状态持久化 --------------------------------------------------------------

def test_state_读写roundtrip(tmp_path):
    key = "123456"
    state = service.load_state(key)
    assert state["status"] == "new" and state["stages"] == {}
    state["stages"]["extract"] = {"status": "ok", "elapsed_s": 1.5, "note": ""}
    state["rowid"] = "999"
    service.save_state(state)
    again = service.load_state(key)
    assert again["stages"]["extract"]["status"] == "ok"
    assert again["rowid"] == "999"
    assert again.get("updated_at")


def test_state_损坏文件当全新跑(tmp_path):
    os.makedirs(service.STATE_DIR, exist_ok=True)
    with open(service._state_path("bad"), "w", encoding="utf-8") as f:
        f.write("{不是合法json")
    state = service.load_state("bad")
    assert state["status"] == "new" and state["stages"] == {}


def test_task_key_两种模式():
    assert service._task_key({"url": "https://detail.1688.com/offer/123456.html"}) == "123456"
    assert service._task_key({"rowid": "999"}) == "rowid-999"
    with pytest.raises(ValueError):
        service._task_key({"title": "什么都没有"})


# ---- publish_one 编排 ---------------------------------------------------------

@pytest.mark.asyncio
async def test_续跑跳过已完成阶段(tmp_path, monkeypatch):
    calls, events = [], []
    _fake_stages(monkeypatch, calls)
    state = service.load_state("rowid-1")
    for sid in ("extract", "claim"):
        state["stages"][sid] = {"status": "ok", "elapsed_s": 1.0, "note": ""}
    service.save_state(state)

    r = await service.publish_one(None, {"rowid": "1", "info_path": "x.json"}, "Pawly",
                                  on_progress=_collect(events))
    assert r["status"] == "ok"
    assert "extract" not in calls and "claim" not in calls
    # ⑮ publish 排在 ⑭ save 之后，故末尾是 publish（真实运行里它默认 skipped，
    # 但 _fake_stages 把每个阶段都换成了「被调用即记账」的假函数）
    assert calls[0] == "auto_cat" and calls[-2:] == ["save", "publish"]
    skipped = [e for e in events if e["type"] == "stage_done" and e["status"] == "skipped"]
    assert {e["stage"] for e in skipped} == {"extract", "claim"}


@pytest.mark.asyncio
async def test_from_stage_从指定阶段起重跑(tmp_path, monkeypatch):
    calls, events = [], []
    _fake_stages(monkeypatch, calls)
    state = service.load_state("rowid-2")
    for sid, _ in service.STAGES:
        state["stages"][sid] = {"status": "ok", "elapsed_s": 1.0, "note": ""}
    service.save_state(state)

    r = await service.publish_one(None, {"rowid": "2", "info_path": "x.json"}, "Pawly",
                                  on_progress=_collect(events), from_stage="skc")
    assert r["status"] == "ok"
    # claim/auto_cat 都被跳过时，publish_one 会先补开编辑页（open_edit 在最前）
    assert calls == ["open_edit"] + [sid for sid, _ in service.STAGES
                     if service._STAGE_IDS.index(sid) >= service._STAGE_IDS.index("skc")]


@pytest.mark.asyncio
async def test_阶段失败即停并写状态(tmp_path, monkeypatch):
    calls, events = [], []
    _fake_stages(monkeypatch, calls, fail_at="attrs")
    r = await service.publish_one(None, {"rowid": "3", "info_path": "x.json"}, "Pawly",
                                  on_progress=_collect(events))
    assert r["status"] == "fail" and r["failed_stage"] == "attrs"
    assert calls[-1] == "attrs" and "titles" not in calls
    state = service.load_state("rowid-3")
    assert state["status"] == "fail" and state["failed_stage"] == "attrs"
    # 失败阶段下次重跑：不享受跳过；且 claim/auto_cat 已完成被跳过，
    # 先补开编辑页再从 attrs 续跑
    calls2 = []
    _fake_stages(monkeypatch, calls2)
    r2 = await service.publish_one(None, {"rowid": "3", "info_path": "x.json"}, "Pawly")
    assert r2["status"] == "ok"
    assert calls2[0] == "open_edit" and calls2[1] == "attrs"


@pytest.mark.asyncio
async def test_续跑编辑页补开规则(tmp_path, monkeypatch):
    """claim/auto_cat 在待跑清单里时由它们自己开编辑页，publish_one 不补开。"""
    calls, events = [], []
    _fake_stages(monkeypatch, calls)
    # 只有 extract 完成：claim 会跑 → publish_one 不补开
    state = service.load_state("rowid-6")
    state["stages"]["extract"] = {"status": "ok", "elapsed_s": 1.0, "note": ""}
    service.save_state(state)
    r = await service.publish_one(None, {"rowid": "6", "info_path": "x.json"}, "Pawly",
                                  on_progress=_collect(events))
    assert r["status"] == "ok"
    assert "open_edit" not in calls and calls[0] == "claim"


@pytest.mark.asyncio
async def test_阶段异常按失败处理不炸编排(tmp_path, monkeypatch):
    calls, events = [], []
    _fake_stages(monkeypatch, calls, raise_at="titles")
    r = await service.publish_one(None, {"rowid": "4", "info_path": "x.json"}, "Pawly",
                                  on_progress=_collect(events))
    assert r["status"] == "fail" and r["failed_stage"] == "titles"
    assert "异常" in r["note"]


@pytest.mark.asyncio
async def test_事件序列_stage配对(tmp_path, monkeypatch):
    calls, events = [], []
    _fake_stages(monkeypatch, calls)
    await service.publish_one(None, {"rowid": "5", "info_path": "x.json"}, "Pawly",
                              on_progress=_collect(events), index=2, total=3)
    starts = [e["stage"] for e in events if e["type"] == "stage_start"]
    dones = [e["stage"] for e in events if e["type"] == "stage_done"]
    assert starts == dones == service._STAGE_IDS
    assert all(e["index"] == 2 and e["offer"] == "rowid-5" for e in events)
    assert all("elapsed_s" in e for e in events if e["type"] == "stage_done")


@pytest.mark.asyncio
async def test_日志桥_把模块日志转成log事件():
    """_install_log_bridge：app.publish/app.llm 的 INFO+ 日志应出现在事件流里，
    其它模块（如 app.collect）不该被转发。"""
    from app.logger import logger as _logger

    events = []
    sink_id = service._install_log_bridge(_collect(events))
    try:
        _logger.patch(lambda r: r.__setitem__("name", "app.publish.pipeline")) \
            .info("发布模块的小步日志")
        _logger.patch(lambda r: r.__setitem__("name", "app.llm")) \
            .info("LLM 模块的日志")
        _logger.patch(lambda r: r.__setitem__("name", "app.collect.pipeline")) \
            .info("采集模块不该被转发")
        # sink 经 call_soon_threadsafe 异步投递，让事件循环跑一轮
        await asyncio.sleep(0.2)
    finally:
        service.logger.remove(sink_id)
    msgs = [e["message"] for e in events if e["type"] == "log"]
    assert "发布模块的小步日志" in msgs
    assert "LLM 模块的日志" in msgs
    assert "采集模块不该被转发" not in msgs


# ---- vision 决策解析 -----------------------------------------------------------

def _write_main(d, name: str) -> str:
    p = os.path.join(str(d), name)
    with open(p, "wb") as f:
        f.write(_JPEG)
    return p


@pytest.mark.asyncio
async def test_pick_material_优先读complianceNotes(tmp_path, monkeypatch):
    for n in ("main-01.jpg", "main-02.jpg", "main-03.jpg"):
        _write_main(tmp_path, n)
    info = {"complianceNotes": {"files": [
        {"file": "main-01.jpg", "clean": False, "chinese": True, "note": "中文海报"},
        {"file": "main-02.jpg", "clean": True, "note": "白底平铺"},
        {"file": "main-03.jpg", "clean": True, "note": "模特图"},
    ]}}

    async def _boom(*a, **k):
        raise AssertionError("有 complianceNotes 不该再调 LLM")
    monkeypatch.setattr(vision, "ask_json_with_images", _boom)

    r = await vision.pick_material(info, str(tmp_path))
    assert r["status"] == "ok" and r["image"].endswith("main-02.jpg")
    assert r["uncertain"] is False


@pytest.mark.asyncio
async def test_pick_material_无干净图兜底uncertain(tmp_path):
    _write_main(tmp_path, "main-01.jpg")
    _write_main(tmp_path, "main-02.jpg")
    info = {"complianceNotes": {"files": [
        {"file": "main-01.jpg", "clean": False, "note": "中文"},
        {"file": "main-02.jpg", "clean": False, "duplicate": True, "note": "与 main-01 重复"},
    ]}}
    r = await vision.pick_material(info, str(tmp_path))
    assert r["status"] == "ok" and r["uncertain"] is True
    assert r["image"].endswith("main-01.jpg")  # 重复图不做兜底候选


@pytest.mark.asyncio
async def test_pick_material_无标注走视觉且坏文件名兜底(tmp_path, monkeypatch):
    _write_main(tmp_path, "main-01.jpg")
    _write_main(tmp_path, "main-02.jpg")

    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        return {"image": "main-99.jpg", "reason": "不存在的文件", "uncertain": False}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)

    r = await vision.pick_material({"title": "t"}, str(tmp_path))
    assert r["status"] == "ok" and r["image"].endswith("main-01.jpg")
    assert r["uncertain"] is True  # LLM 返回不存在的文件名 → 兜底第一张 + uncertain


@pytest.mark.asyncio
async def test_pick_material_无图报错(tmp_path):
    r = await vision.pick_material({"title": "t"}, str(tmp_path))
    assert r["status"] == "error"


@pytest.mark.asyncio
async def test_plan_skc_分色与漏色进uncertain(tmp_path, monkeypatch):
    for n in ("main-01.jpg", "main-02.jpg", "main-03.jpg"):
        _write_main(tmp_path, n)

    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        return {"rows": [
            {"color": "灰色", "images": ["main-02.jpg", "main-01.jpg", "ghost.jpg"],
             "uncertain": False},
        ]}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)

    info = {"title": "t", "colors": ["灰色", "卡其"]}
    r = await vision.plan_skc(info, str(tmp_path))
    assert r["status"] == "ok" and len(r["rows"]) == 1
    row = r["rows"][0]
    assert row["keyword"] == "灰色"
    # 主图排第一（main-02 是 LLM 给的首位）；不存在的文件名被滤掉
    assert [os.path.basename(p) for p in row["images"]] == ["main-02.jpg", "main-01.jpg"]
    assert "卡其" in r["uncertain_rows"]  # LLM 漏掉的颜色不能静默丢


@pytest.mark.asyncio
async def test_plan_skc_无颜色直接空计划(tmp_path):
    r = await vision.plan_skc({"title": "t", "colors": []}, str(tmp_path))
    assert r["status"] == "ok" and r["rows"] == []


@pytest.mark.asyncio
async def test_plan_desc_动作分桶且漏判保留(monkeypatch):
    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        return {"actions": [
            {"pos": 1, "action": "delete", "reason": "工厂图"},
            {"pos": 2, "action": "replace", "reason": "中文"},
            {"pos": 99, "action": "delete", "reason": "越界pos"},
        ]}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    # plan_desc 自己会把 URL 解析成 data URL（位次要与传图一一对应，见
    # test_publish_desc_unreachable_pos.py），测试里桩掉避免真去下载
    monkeypatch.setattr(vision, "image_ref", lambda u: "data:image/jpeg;base64,AAAA")

    mods = [{"pos": i, "url": f"https://x/{i}.jpg", "onDxmHost": True} for i in (1, 2, 3)]
    r = await vision.plan_desc(mods, {"title": "t"})
    assert r["delete"] == [1]
    assert [x["pos"] for x in r["replace"]] == [2]
    assert r["replace"][0]["url"] == "https://x/2.jpg"
    assert 3 in r["keep"]  # LLM 漏判的一律保留——保守方向，不删不该删的


@pytest.mark.asyncio
async def test_plan_desc_空模块直接空计划():
    r = await vision.plan_desc([], {"title": "t"})
    assert r["status"] == "ok" and r["delete"] == [] and r["replace"] == []


@pytest.mark.asyncio
async def test_check_cleaned_透传质检结论(monkeypatch):
    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        return {"clean": False, "issues": "残留拼音"}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    r = await vision.check_cleaned("x.jpg")
    assert r["clean"] is False and "拼音" in r["issues"]


# ---- plan_clean 与清理阶段 -----------------------------------------------------

def _notes(*entries) -> dict:
    return {"complianceNotes": {"files": list(entries)}}


def test_plan_clean_干净图够下限就一次调用都不发(tmp_path):
    """省钱口径不变，只是「够」的判据从 1 张改成 ⑦ 的行下限（见 plan_clean 说明）。"""
    for n in ("main-01.jpg", "main-02.jpg", "main-03.jpg", "main-04.jpg"):
        _write_main(tmp_path, n)
    info = _notes({"file": "main-01.jpg", "clean": False, "watermark": True},
                  {"file": "main-02.jpg", "clean": True},
                  {"file": "main-03.jpg", "clean": True},
                  {"file": "main-04.jpg", "clean": True})
    r = vision.plan_clean(info, str(tmp_path), min_clean=3)
    assert r["items"] == [] and "无需清理" in r["reason"]


def test_plan_clean_干净图不足下限时只清缺口(tmp_path):
    """1 张干净 + 4 张脏、下限 3 → 只清 2 张（缺口），剩下两张脏图留着不清。

    原口径是「有任意一张干净图就整段跳过」，那是照阶段⑥「素材图只取一张」写的；
    ⑦ SKC 每个颜色行要 3 张，于是剩下的脏图被原样挂到颜色行上发出去。
    """
    for i in range(1, 6):
        _write_main(tmp_path, f"main-{i:02d}.jpg")
    info = _notes({"file": "main-01.jpg", "clean": True},
                  {"file": "main-02.jpg", "clean": False, "chinese": True},
                  {"file": "main-03.jpg", "clean": False, "watermark": True},
                  {"file": "main-04.jpg", "clean": False, "logo": True},
                  {"file": "main-05.jpg", "clean": False, "chinese": True})
    r = vision.plan_clean(info, str(tmp_path), min_clean=3)
    # 只清 2 张，且按 _dirty_score 从轻到重取（logo < 水印 < 中文）：
    # 带中文的最难修成功（要英化重排版、还会因残留拼音过不了质检），排最后
    assert [i["file"] for i in r["items"]] == ["main-04.jpg", "main-03.jpg"]
    assert "只清缺口 2 张" in r["reason"]


def test_plan_clean_跳过重复图与尺码表(tmp_path):
    for n in ("main-01.jpg", "main-02.jpg", "main-03.jpg"):
        _write_main(tmp_path, n)
    info = _notes(
        {"file": "main-01.jpg", "clean": False, "watermark": True, "note": "水印"},
        {"file": "main-02.jpg", "clean": False, "duplicate": True},
        {"file": "main-03.jpg", "clean": False, "chinese": True, "kind": "尺码表"},
    )
    r = vision.plan_clean(info, str(tmp_path))
    assert [i["file"] for i in r["items"]] == ["main-01.jpg"]


def test_plan_clean_提示词按标注定制(tmp_path):
    _write_main(tmp_path, "main-01.jpg")
    _write_main(tmp_path, "main-02.jpg")
    info = _notes({"file": "main-01.jpg", "clean": False, "chinese": True},
                  {"file": "main-02.jpg", "clean": False, "watermark": True})
    items = {i["file"]: i["prompt"] for i in vision.plan_clean(info, str(tmp_path))["items"]}
    # 有中文才提英化；只有水印时不提，免得模型去改本来没问题的地方
    assert "翻译成简洁英文" in items["main-01.jpg"]
    assert "翻译成简洁英文" not in items["main-02.jpg"]
    assert "水印" in items["main-02.jpg"]


def test_plan_clean_无标注不清理(tmp_path):
    _write_main(tmp_path, "main-01.jpg")
    r = vision.plan_clean({}, str(tmp_path))
    assert r["items"] == [] and "无 complianceNotes" in r["reason"]


def test_dirty_score_中文优先级最高():
    """兜底排序：中文 > 水印 > logo > 重复（2026-08-22 踩坑的直接回归点）。"""
    chinese = {"chinese": True, "watermark": True, "logo": True}
    watermark = {"watermark": True, "logo": True}
    assert vision._dirty_score(watermark) < vision._dirty_score(chinese)


@pytest.mark.asyncio
async def test_pick_material_兜底避开中文图(tmp_path):
    for n in ("main-01.jpg", "main-02.jpg"):
        _write_main(tmp_path, n)
    info = _notes(
        {"file": "main-01.jpg", "clean": False, "chinese": True, "watermark": True},
        {"file": "main-02.jpg", "clean": False, "watermark": True},
    )
    r = await vision.pick_material(info, str(tmp_path))
    # 编号在前的 main-01 含中文（Temu 最硬红线），必须让位给只有水印的 main-02
    assert r["image"].endswith("main-02.jpg") and r["uncertain"] is True


@pytest.mark.asyncio
async def test_plan_skc_单颜色免视觉请求(tmp_path, monkeypatch):
    for n in ("main-01.jpg", "main-02.jpg", "main-03.jpg"):
        _write_main(tmp_path, n)

    async def _boom(*a, **k):
        raise AssertionError("单颜色不该发视觉请求")
    monkeypatch.setattr(vision, "ask_json_with_images", _boom)

    info = {"title": "t", "colors": ["黄色"], **_notes(
        {"file": "main-01.jpg", "clean": False, "chinese": True, "watermark": True},
        {"file": "main-02.jpg", "clean": True},
        {"file": "main-03.jpg", "clean": False, "duplicate": True},
    )}
    r = await vision.plan_skc(info, str(tmp_path))
    assert len(r["rows"]) == 1 and r["uncertain_rows"] == []
    row = r["rows"][0]
    assert row["keyword"] == "黄色" and row["uncertain"] is False
    # 重复图被排除；干净图排首位当该颜色主图
    assert [os.path.basename(p) for p in row["images"]] == ["main-02.jpg", "main-01.jpg"]


@pytest.mark.asyncio
async def test_st_clean_images_成功回写标注(tmp_path, monkeypatch):
    for n in ("main-01.jpg", "main-02.jpg"):
        _write_main(tmp_path, n)
    info_path = str(tmp_path / "product-info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(_notes({"file": "main-01.jpg", "clean": False, "watermark": True},
                         {"file": "main-02.jpg", "clean": False, "chinese": True}), f)

    def _fake_edit(path, prompt=None, out_path=None, **kw):
        with open(out_path, "wb") as f:
            f.write(_JPEG)
        return {"status": "ok", "output": out_path}
    monkeypatch.setattr(service.images, "edit_image", _fake_edit)

    async def _fake_qc(p):
        return {"status": "ok", "clean": True, "issues": ""}
    monkeypatch.setattr(service.vision, "check_cleaned", _fake_qc)

    events = []
    ctx = {"info_path": info_path, "workdir": str(tmp_path)}
    r = await service._st_clean_images(ctx, None, _collect_async(events))
    assert r["status"] == "ok" and "清理 2/2" in r["note"]
    with open(info_path, encoding="utf-8") as f:
        files = {e["file"]: e for e in json.load(f)["complianceNotes"]["files"]}
    # 回写后 ⑥⑦ 的选图逻辑不用改就能挑到干净图
    assert all(files[k]["clean"] and not files[k]["chinese"] for k in files)
    assert json.load(open(info_path, encoding="utf-8"))[
        "complianceNotes"]["cleanFiles"] == ["main-01.jpg", "main-02.jpg"]


@pytest.mark.asyncio
async def test_st_clean_images_失败不阻塞且发人工检查(tmp_path, monkeypatch):
    _write_main(tmp_path, "main-01.jpg")
    info_path = str(tmp_path / "product-info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(_notes({"file": "main-01.jpg", "clean": False, "watermark": True}), f)

    def _boom(*a, **k):
        raise RuntimeError("Packy 超时")
    monkeypatch.setattr(service.images, "edit_image", _boom)

    events = []
    ctx = {"info_path": info_path, "workdir": str(tmp_path)}
    r = await service._st_clean_images(ctx, None, _collect_async(events))
    # 清理是增益路径：全失败也算 ok，绝不 fail 掉整个商品
    assert r["status"] == "ok" and "未成功" in r["note"]
    assert [e["type"] for e in events] == ["manual_check"]
    # 原标注保持脏，该图仍以脏图身份参与 ⑥⑦ 的兜底打分
    with open(info_path, encoding="utf-8") as f:
        assert json.load(f)["complianceNotes"]["files"][0]["clean"] is False


@pytest.mark.asyncio
async def test_st_clean_images_质检未过保留原图(tmp_path, monkeypatch):
    _write_main(tmp_path, "main-01.jpg")
    info_path = str(tmp_path / "product-info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(_notes({"file": "main-01.jpg", "clean": False, "chinese": True}), f)
    original = open(str(tmp_path / "main-01.jpg"), "rb").read()

    def _fake_edit(path, prompt=None, out_path=None, **kw):
        with open(out_path, "wb") as f:
            f.write(_JPEG + b"dirty")
        return {"status": "ok", "output": out_path}
    monkeypatch.setattr(service.images, "edit_image", _fake_edit)

    async def _fake_qc(p):
        return {"status": "ok", "clean": False, "issues": "残留拼音"}
    monkeypatch.setattr(service.vision, "check_cleaned", _fake_qc)

    events = []
    r = await service._st_clean_images(
        {"info_path": info_path, "workdir": str(tmp_path)}, None, _collect_async(events))
    assert r["status"] == "ok"
    # 质检不过的产物绝不能顶替原图（否则把带拼音乱码的图挂上真店）
    assert open(str(tmp_path / "main-01.jpg"), "rb").read() == original
    assert "残留拼音" in events[0]["message"]


# ---- run_batch 批量编排 --------------------------------------------------------

@pytest.mark.asyncio
async def test_run_batch_空清单aborted():
    events = []
    r = await service.run_batch([], store="Pawly", on_progress=_collect(events))
    assert r == {"ok": 0, "fail": 0, "batch": r["batch"]}
    assert events[-1]["type"] == "aborted" and "为空" in events[-1]["reason"]


@pytest.mark.asyncio
async def test_run_batch_未知from_stage_aborted():
    events = []
    r = await service.run_batch([{"rowid": "1", "info_path": "x"}], store="Pawly",
                                on_progress=_collect(events), from_stage="nosuch")
    assert r["ok"] == 0 and events[-1]["type"] == "aborted"
    assert "nosuch" in events[-1]["reason"]


@pytest.mark.asyncio
async def test_run_batch_缺店铺aborted():
    events = []
    r = await service.run_batch([{"rowid": "1", "info_path": "x"}], store="",
                                on_progress=_collect(events))
    assert events[-1]["type"] == "aborted" and "店铺" in events[-1]["reason"]


@pytest.mark.asyncio
async def test_run_batch_缺站点aborted():
    """站点没有默认值可兜——店小秘认领弹窗里根本没有「全球」这一项（原默认值），
    走到阶段② 才抛「未找到站点」的话，前面 extract 已经白跑一遍。"""
    events = []
    r = await service.run_batch([{"rowid": "1", "info_path": "x"}], store="Pawly",
                                site="", on_progress=_collect(events))
    assert r["ok"] == 0
    assert events[-1]["type"] == "aborted" and "站点" in events[-1]["reason"]


@pytest.mark.asyncio
async def test_run_batch_单商品失败不拖垮批次(tmp_path, monkeypatch):
    async def _alive(*a, **k):
        return True
    monkeypatch.setattr(service, "ensure_cdp_alive", _alive)

    class _FakeSession:
        async def open(self):
            return None
        async def close(self):
            return None
        def is_alive(self):
            return True
    monkeypatch.setattr(service, "BrowserSession", _FakeSession)
    monkeypatch.setattr(service, "reset_token_counters", lambda: None)

    async def _fake_one(session, task, store, site, on_progress=None,
                        index=0, total=1, from_stage="", use_cache=True,
                        do_publish=False, price="", **kw):
        if task.get("url"):
            return {"status": "ok", "rowid": "111", "failed_stage": "",
                    "note": "", "elapsed_s": 1.0}
        return {"status": "fail", "rowid": None, "failed_stage": "attrs",
                "note": "模拟失败", "elapsed_s": 0.5}
    monkeypatch.setattr(service, "publish_one", _fake_one)

    events = []
    tasks = [{"url": "https://detail.1688.com/offer/123456.html"},
             {"rowid": "999", "info_path": "x.json"}]
    r = await service.run_batch(tasks, store="Pawly", site="美国",
                                on_progress=_collect(events))
    assert r["ok"] == 1 and r["fail"] == 1
    types = [e["type"] for e in events]
    assert types[0] == "batch_start" and types[-1] == "batch_done"
    dones = [e for e in events if e["type"] == "product_done"]
    assert [d["status"] for d in dones] == ["ok", "fail"]
    assert dones[0]["rowid"] == "111" and dones[1]["failed_stage"] == "attrs"
    assert events[-1]["ok"] == 1 and "elapsed_s" in events[-1]
    # prefs 记住了本次店铺选择
    with open(service.PREFS_PATH, encoding="utf-8") as f:
        assert json.load(f)["store"] == "Pawly"


# ---- 续跑时按编辑页实况重跑表单阶段 -------------------------------------------
# 2026-08-23 实测 947662049255：状态文件里 ③~⑬ 全 ok，但 save 从未成功，
# 编辑页一重载表单成果全丢，续跑却跳过它们直奔 save，永远卡在校验未过。

@pytest.mark.asyncio
async def test_save未成功时按实况重跑表单阶段(tmp_path, monkeypatch):
    calls, events = [], []
    _fake_stages(monkeypatch, calls)
    # 实况：表单成果全丢；变种表按有配件色给（⑦a 只在真有配件色时才有事做，
    # 见 _fake_live 里 variantByColor 的说明）
    _fake_live(monkeypatch,
               variantByColor={"白色": {"total": 5, "filled": 5},
                               "红色": {"total": 5, "filled": 1}})
    state = service.load_state("rowid-100")
    for sid, _ in service.STAGES:               # 除 save 外全记 ok
        if sid != "save":
            state["stages"][sid] = {"status": "ok", "elapsed_s": 1.0, "note": ""}
    service.save_state(state)

    r = await service.publish_one(None, {"rowid": "100", "info_path": "x.json"},
                                  "Pawly", on_progress=_collect(events))

    assert r["status"] == "ok"
    # ⑤ 起的表单阶段全部重跑；类目有效时 ③④ 不必重来（③ 走类目树很贵），
    # 本地产物 ①② 也仍跳过
    for sid in service._FORM_STAGES_AFTER_CAT:
        assert sid in calls, f"{sid} 应重跑"
    assert "extract" not in calls and "claim" not in calls
    assert "auto_cat" not in calls and "attrs" not in calls
    # 给人一条明确提示，而不是静默重跑
    msgs = [e["message"] for e in events if e["type"] == "manual_check"]
    assert any("编辑页数据已丢失" in m for m in msgs)


@pytest.mark.asyncio
async def test_实况显示表单还在时不重复重跑(tmp_path, monkeypatch):
    """已 save 失败但表单内容还在（同一页面内重试）：不该把贵阶段再跑一遍。"""
    calls, events = [], []
    _fake_stages(monkeypatch, calls)
    _fake_live(monkeypatch, titleFilled=True, skuRowCount=8, skuFilledRows=8,
               sizechartAdded=True, attrImgCount=6, shippingSet=True,
               descForeignCount=0, skuCodeCount=8, skuCodeBad=0, previewBad=0)
    state = service.load_state("rowid-101")
    for sid, _ in service.STAGES:
        if sid != "save":
            state["stages"][sid] = {"status": "ok", "elapsed_s": 1.0, "note": ""}
    service.save_state(state)

    r = await service.publish_one(None, {"rowid": "101", "info_path": "x.json"},
                                  "Pawly", on_progress=_collect(events))

    assert r["status"] == "ok"
    # ⑬b video 无条件进重跑集：videoUrl 不在 DOM 里、live_state 读不到它，
    # 故由阶段自己读接口判跳过（没视频/已合规都是 skipped，只花几秒）。
    # 见 service._stale_form_stages 末尾那段与 test_publish_video_stage.py。
    assert calls == ["open_edit", "video", "save"]


@pytest.mark.asyncio
async def test_尺码勾选丢了连带重跑尺码表变种库存(tmp_path, monkeypatch):
    """变种表 0 行时 ⑨⑩⑪ 全都无处可填，只补 ⑨ 没用。"""
    calls, events = [], []
    _fake_stages(monkeypatch, calls)
    # 标题/图片/运输都在，只有变种表空了
    _fake_live(monkeypatch, titleFilled=True, attrImgCount=6, shippingSet=True,
               descForeignCount=0, skuRowCount=0, previewBad=0)
    state = service.load_state("rowid-102")
    for sid, _ in service.STAGES:
        if sid != "save":
            state["stages"][sid] = {"status": "ok", "elapsed_s": 1.0, "note": ""}
    service.save_state(state)

    await service.publish_one(None, {"rowid": "102", "info_path": "x.json"},
                              "Pawly", on_progress=_collect(events))

    assert [c for c in calls if c != "open_edit"] == [
        "fix_sizes", "sizechart", "sku_code", "variant", "stock", "video", "save"]
    assert "titles" not in calls and "skc" not in calls


@pytest.mark.asyncio
async def test_save已成功则不做实况判定(tmp_path, monkeypatch):
    """已落库的商品重跑：实况早就是「空表单」（数据在服务端），不能因此重跑一遍。"""
    calls, events = [], []
    _fake_stages(monkeypatch, calls)
    _fake_live(monkeypatch)                     # 实况全空，但 save 已 ok
    state = service.load_state("rowid-103")
    for sid, _ in service.STAGES:
        state["stages"][sid] = {"status": "ok", "elapsed_s": 1.0, "note": ""}
    service.save_state(state)

    r = await service.publish_one(None, {"rowid": "103", "info_path": "x.json"},
                                  "Pawly", on_progress=_collect(events))

    assert r["status"] == "ok"
    assert calls == []                          # 全部跳过，连编辑页都不必开


@pytest.mark.asyncio
async def test_显式from_stage不被实况覆盖(tmp_path, monkeypatch):
    """人工指定起点是明确判断，实况判定不该往里加阶段。"""
    calls, events = [], []
    _fake_stages(monkeypatch, calls)
    _fake_live(monkeypatch)                     # 实况全丢，但不该生效
    state = service.load_state("rowid-104")
    for sid, _ in service.STAGES:
        state["stages"][sid] = {"status": "ok", "elapsed_s": 1.0, "note": ""}
    service.save_state(state)

    await service.publish_one(None, {"rowid": "104", "info_path": "x.json"}, "Pawly",
                              on_progress=_collect(events), from_stage="shipping")

    assert calls == ["open_edit", "shipping", "desc", "video", "save", "publish"]


@pytest.mark.asyncio
async def test_实况读取失败则保守重跑全部表单阶段(tmp_path, monkeypatch):
    calls, events = [], []
    _fake_stages(monkeypatch, calls)

    async def boom(session):
        raise RuntimeError("页面炸了")
    monkeypatch.setattr(service, "live_state", boom)
    state = service.load_state("rowid-105")
    for sid, _ in service.STAGES:
        if sid != "save":
            state["stages"][sid] = {"status": "ok", "elapsed_s": 1.0, "note": ""}
    service.save_state(state)

    r = await service.publish_one(None, {"rowid": "105", "info_path": "x.json"},
                                  "Pawly", on_progress=_collect(events))

    assert r["status"] == "ok"
    for sid in service._FORM_ONLY_STAGES:
        assert sid in calls


# ---- ⑬ 描述图英化产物复用 -----------------------------------------------------
# gpt-image-2 每张一次生图调用，是本阶段最贵的一步。而 ⑬ 的成果只活在未保存的表单里，
# save 没成功就得整段重跑——重跑时若连图也重新生成，等于白烧一遍生图钱。

def _desc_env(tmp_path, monkeypatch, calls: dict):
    """把 ⑬ 用到的浏览器/视觉/下载/生图全换成假的，记录调用次数。"""
    # info_path 默认参数是必须的：_resolve_desc_pos 只传 session（重查当前序号）
    async def fake_map(session, info_path=""):
        calls["mapped"] = calls.get("mapped", 0) + 1
        return {"status": "ok", "modules": [{"pos": 1, "url": "https://cdn/a.jpg"}]}

    async def fake_plan(mods, info):
        return {"delete": [], "replace": [{"pos": 1, "url": "https://cdn/a.jpg"}]}

    async def fake_replace(session, pos, image, full_cid=None, expect_url=None):
        calls.setdefault("replaced", []).append(image)
        calls.setdefault("replacedPos", []).append(pos)
        return {"status": "ok"}

    async def fake_save(session):
        return {"status": "ok"}

    async def fake_ensure_closed(session):
        calls["ensureClosed"] = calls.get("ensureClosed", 0) + 1
        return {"status": "ok", "wasOpen": False}

    async def fake_qc(path):
        calls["qc"] = calls.get("qc", 0) + 1
        return {"clean": True}

    def fake_download(url, dst, retries=3):
        calls["download"] = calls.get("download", 0) + 1
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(b"x")
        return 1

    def fake_edit(path, prompt=None, out_path=None, **kw):
        calls["edit"] = calls.get("edit", 0) + 1
        # 真 edit_image 收尾会 compress() 把 png 转 jpg，这里如实模拟落盘
        out = out_path or (os.path.splitext(path)[0] + "-edited.jpg")
        with open(out, "wb") as f:
            f.write(b"edited")
        return {"status": "ok", "output": out}

    monkeypatch.setattr(service, "desc_map", fake_map)
    monkeypatch.setattr(service.vision, "plan_desc", fake_plan)
    monkeypatch.setattr(service, "desc_replace", fake_replace)
    monkeypatch.setattr(service, "desc_save", fake_save)
    # 描述编辑器是全屏 modal，收尾必须确认关掉（否则后续阶段全被遮住点不中）
    monkeypatch.setattr(service, "ensure_desc_closed", fake_ensure_closed)
    monkeypatch.setattr(service.vision, "check_cleaned", fake_qc)
    monkeypatch.setattr(service.extract, "_download_image", fake_download)
    monkeypatch.setattr(service.images, "edit_image", fake_edit)

    info = tmp_path / "product-info.json"
    info.write_text('{"title": "t"}', encoding="utf-8")
    return {"info_path": str(info), "workdir": str(tmp_path), "rowid": "1"}


@pytest.mark.asyncio
async def test_描述图英化产物第二次跑直接复用(tmp_path, monkeypatch):
    calls, events = {}, []
    ctx = _desc_env(tmp_path, monkeypatch, calls)

    r1 = await service._st_desc(ctx, None, _collect_async(events))
    assert r1["status"] == "ok" and calls["edit"] == 1

    # 第二次跑（模拟 save 失败后重跑 ⑬）：不再生图、不再下载、不再质检
    r2 = await service._st_desc(ctx, None, _collect_async(events))
    assert r2["status"] == "ok"
    assert calls["edit"] == 1, "英化产物应复用，不该再调生图"
    assert calls["download"] == 1, "原图也不该重新下载"
    assert calls["qc"] == 1, "缓存命中不该再走一次视觉质检"
    assert "复用" in r2["note"]
    # 两次替换用的是同一个产物文件
    assert calls["replaced"][0] == calls["replaced"][1]


@pytest.mark.asyncio
async def test_删图后替换按源URL重定位序号(tmp_path, monkeypatch):
    """plan_desc 的 pos 是删图前的序号，删完整体前移，必须按源 URL 重查。

    2026-08-24 线上表现：删 3 张后按旧 pos 6 去替换，描述区只剩 5 个模块，
    desc_replace 报「序号 6 越界」，该英化的外链图没换掉，desc_save 回读仍有
    cbu01.alicdn.com。没越界的情形更糟——静默替换到别的模块上。
    """
    calls, events = {}, []
    ctx = _desc_env(tmp_path, monkeypatch, calls)

    # 页面上 5 张；计划基于删图前的 6 张，要替换的是删图前 pos 6（现已前移到 pos 4）
    async def fake_map(session, info_path=""):
        return {"status": "ok", "modules": [
            {"pos": 1, "url": "https://cdn/k1.jpg"},
            {"pos": 2, "url": "https://cdn/k2.jpg"},
            {"pos": 3, "url": "https://cdn/k3.jpg"},
            {"pos": 4, "url": "https://cdn/a.jpg"},
            {"pos": 5, "url": "https://cdn/k5.jpg"}]}

    async def fake_plan(mods, info):
        return {"delete": [], "replace": [{"pos": 6, "url": "https://cdn/a.jpg"}]}

    monkeypatch.setattr(service, "desc_map", fake_map)
    monkeypatch.setattr(service.vision, "plan_desc", fake_plan)

    r = await service._st_desc(ctx, None, _collect_async(events))

    assert r["status"] == "ok"
    assert calls["replacedPos"] == [4], "必须按源 URL 重定位到当前序号 4，而不是用计划里的 6"


@pytest.mark.asyncio
async def test_源图已不在描述区时跳过且不烧生图(tmp_path, monkeypatch):
    """源图被删或已替换过就没法重定位，此时该跳过——生图是本阶段最贵的一步。"""
    calls, events = {}, []
    ctx = _desc_env(tmp_path, monkeypatch, calls)

    async def fake_map(session, info_path=""):
        return {"status": "ok", "modules": [{"pos": 1, "url": "https://cdn/other.jpg"}]}

    async def fake_plan(mods, info):
        return {"delete": [], "replace": [{"pos": 1, "url": "https://cdn/a.jpg"}]}

    monkeypatch.setattr(service, "desc_map", fake_map)
    monkeypatch.setattr(service.vision, "plan_desc", fake_plan)

    r = await service._st_desc(ctx, None, _collect_async(events))

    assert r["status"] == "skipped"
    assert "edit" not in calls, "定位失败必须在生图之前就跳过"
    assert not calls.get("replaced")
    msgs = [e["message"] for e in events if e["type"] == "manual_check"]
    assert any("定位失败" in m for m in msgs)


@pytest.mark.asyncio
async def test_描述图缓存键按URL而非序号(tmp_path, monkeypatch):
    """删图会让 pos 整体前移，同一个 pos 下次可能是另一张图。"""
    a1, a2 = service._desc_cache_paths(str(tmp_path), "https://cdn/a.jpg")
    b1, b2 = service._desc_cache_paths(str(tmp_path), "https://cdn/b.jpg")
    assert a1 != b1 and a2 != b2
    # 同一 URL 稳定命中同一路径
    assert service._desc_cache_paths(str(tmp_path), "https://cdn/a.jpg") == (a1, a2)
    # 产物扩展名必须是 .jpg：edit_image 收尾 compress() 会把 png 转 jpg 并删掉 png，
    # 按 .png 探测缓存会永远不命中
    assert a2.endswith("-en.jpg")


@pytest.mark.asyncio
async def test_英化质检未过的产物不留缓存(tmp_path, monkeypatch):
    """质检未过的图留在盘上会被下次重跑当成「已通过的缓存」复用。"""
    calls, events = {}, []
    ctx = _desc_env(tmp_path, monkeypatch, calls)

    async def bad_qc(path):
        calls["qc"] = calls.get("qc", 0) + 1
        return {"clean": False, "issues": "还有中文"}
    monkeypatch.setattr(service.vision, "check_cleaned", bad_qc)

    r = await service._st_desc(ctx, None, _collect_async(events))

    assert r["status"] == "skipped"          # 没删没换
    _, en = service._desc_cache_paths(str(tmp_path), "https://cdn/a.jpg")
    assert not os.path.exists(en), "质检未过的产物必须删掉，不能留成缓存"
    msgs = [e["message"] for e in events if e["type"] == "manual_check"]
    assert any("质检未过" in m for m in msgs)


@pytest.mark.asyncio
async def test_货号是中文时单独重跑货号阶段(tmp_path, monkeypatch):
    """表单全在、变种行也填了，只有货号是平台生成的中文值：只该补 ⑩a。

    这一条是本次 bug 的回归钉子：货号列自己就是 input，中文值会让 skuFilledRows
    非 0，看着像「填过了」。若不单独看 skuCodeBad，续跑就会跳过 ⑩a 直奔 save，
    永远卡在「SKU货号不能包含中文和中文符号」。
    """
    calls, events = [], []
    _fake_stages(monkeypatch, calls)
    _fake_live(monkeypatch, titleFilled=True, skuRowCount=8, skuFilledRows=8,
               sizechartAdded=True, attrImgCount=6, shippingSet=True,
               descForeignCount=0, skuCodeCount=8, skuCodeBad=2, previewBad=0)
    state = service.load_state("rowid-103")
    for sid, _ in service.STAGES:
        if sid != "save":
            state["stages"][sid] = {"status": "ok", "elapsed_s": 1.0, "note": ""}
    service.save_state(state)

    await service.publish_one(None, {"rowid": "103", "info_path": "x.json"},
                              "Pawly", on_progress=_collect(events))

    assert [c for c in calls if c != "open_edit"] == ["sku_code", "video", "save"]
    # 贵阶段（图片/属性）不该被连带
    assert "skc" not in calls and "clean_images" not in calls


# ---- 描述图尺寸兜底（1340×1785 硬红线）--------------------------------------
# 2026-08-23 真站取证（rowid 173539495453435641 保存报错）：描述区 10 张 1688 外链
# 全是 1000×1000 / 900×1200，内容干净所以模型判 keep，却过不了服装类尺寸校验，
# save 被静默弹回。故 keep 里的小图要改判 replace + needsUpscale。

@pytest.mark.asyncio
async def test_plan_desc_干净小图改判放大(monkeypatch):
    """模型判 keep 的干净图，尺寸不达标也必须换掉——内容与尺寸是两条正交判据。"""
    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        return {"actions": [{"pos": 1, "action": "keep", "reason": "干净"},
                            {"pos": 2, "action": "keep", "reason": "干净"}]}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    # plan_desc 自己会把 URL 解析成 data URL（位次要与传图一一对应，见
    # test_publish_desc_unreachable_pos.py），测试里桩掉避免真去下载
    monkeypatch.setattr(vision, "image_ref", lambda u: "data:image/jpeg;base64,AAAA")

    mods = [
        {"pos": 1, "url": "https://x/1.jpg", "size": "900x1200", "tooSmall": True,
         "sizeReasons": ["900x1200 小于 480x480"]},
        {"pos": 2, "url": "https://x/2.jpg", "size": "1340x2010", "tooSmall": False},
    ]
    r = await vision.plan_desc(mods, {"title": "t"})
    # 小图改判 replace 并带放大标记；达标的仍然 keep
    assert [x["pos"] for x in r["replace"]] == [1]
    assert r["replace"][0]["needsUpscale"] is True
    # 理由取 desc_map 算好的 sizeReasons，不写死服装的 1340x1785——描述图的口径是
    # 两边 >= 480 且比例 0.5~2.0，写死尺寸会把超比例的长条图说成「像素不够」
    # （2026-09-02 实测 790x1847 那两张就是这么被说错的）
    assert "480" in r["replace"][0]["reason"]
    assert r["keep"] == [2]


@pytest.mark.asyncio
async def test_plan_desc_已判删的小图不改判(monkeypatch):
    """要删的图不必管尺寸，改判成替换等于把该删的图留下来了。"""
    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        return {"actions": [{"pos": 1, "action": "delete", "reason": "工厂图"}]}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    # plan_desc 自己会把 URL 解析成 data URL（位次要与传图一一对应，见
    # test_publish_desc_unreachable_pos.py），测试里桩掉避免真去下载
    monkeypatch.setattr(vision, "image_ref", lambda u: "data:image/jpeg;base64,AAAA")

    mods = [{"pos": 1, "url": "https://x/1.jpg", "size": "800x800", "tooSmall": True}]
    r = await vision.plan_desc(mods, {"title": "t"})
    assert r["delete"] == [1]
    assert r["replace"] == [] and r["keep"] == []


@pytest.mark.asyncio
async def test_plan_desc_脏图不叠加放大标记(monkeypatch):
    """已判 replace 的图本来就要重新出图，出图收尾的 compress 会把尺寸拉够。"""
    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        return {"actions": [{"pos": 1, "action": "replace", "reason": "中文"}]}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    # plan_desc 自己会把 URL 解析成 data URL（位次要与传图一一对应，见
    # test_publish_desc_unreachable_pos.py），测试里桩掉避免真去下载
    monkeypatch.setattr(vision, "image_ref", lambda u: "data:image/jpeg;base64,AAAA")

    mods = [{"pos": 1, "url": "https://x/1.jpg", "size": "900x1200", "tooSmall": True}]
    r = await vision.plan_desc(mods, {"title": "t"})
    assert len(r["replace"]) == 1                      # 不重复入桶
    assert not r["replace"][0].get("needsUpscale")     # 走英化那条路，不是纯放大


@pytest.mark.asyncio
async def test_plan_desc_读不到尺寸时不误判(monkeypatch):
    """naturalWidth 为 0（图还没加载完）时 desc_map 不给 tooSmall，这里不能瞎猜。"""
    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        return {"actions": [{"pos": 1, "action": "keep", "reason": "干净"}]}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    # plan_desc 自己会把 URL 解析成 data URL（位次要与传图一一对应，见
    # test_publish_desc_unreachable_pos.py），测试里桩掉避免真去下载
    monkeypatch.setattr(vision, "image_ref", lambda u: "data:image/jpeg;base64,AAAA")

    mods = [{"pos": 1, "url": "https://x/1.jpg"}]      # 无 size / tooSmall 字段
    r = await vision.plan_desc(mods, {"title": "t"})
    assert r["keep"] == [1] and r["replace"] == []


# ---- SKC 颜色行的尺寸兜底（1340×1785）---------------------------------------
# 2026-08-24 真站取证（rowid 173539495453435641 保存报「服装类图片尺寸不能小于
# 1340px * 1785px」）：两个颜色里粉红色换图成功（1340×1787），咖啡色因视觉分不出
# 归属被整行跳过，6 张全是 cbu01.alicdn.com 的 1000×1000 / 1200×1200。状态文件当时
# 如实记着「1/2 行完成（失败：咖啡色）」——阶段没骗人，是失败后没人兜底。

def _fake_skc_env(monkeypatch, tmp_path, row_states, replace_results=None):
    """造 _skc_size_fallback 的环境：行状态查表、下载与 fit_34 换成本地造图。"""
    calls = {"replaced": [], "downloaded": []}

    async def fake_state(session, kw):
        return row_states.get(kw, {"err": f"找不到颜色行: {kw}"})

    def fake_download(url, dst, retries=3):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        Image.new("RGB", (1000, 1000), (90, 90, 90)).save(dst)
        calls["downloaded"].append(url)
        return 100

    def fake_fit34(path, out_path=None, quality=85):
        out = out_path or path
        Image.new("RGB", (1340, 1787), (90, 90, 90)).save(out)
        return {"status": "ok", "output": out, "outSize": "1340x1787"}

    async def fake_replace(session, kw, img_dir, full_cid=None):
        n = len([f for f in os.listdir(img_dir) if f.endswith(".jpg")])
        calls["replaced"].append((kw, n))
        return (replace_results or {}).get(kw, {"status": "ok"})

    monkeypatch.setattr(service, "_skc_row_state", fake_state)
    monkeypatch.setattr(service.extract, "_download_image", fake_download)
    monkeypatch.setattr(service.images, "fit_34", fake_fit34)
    monkeypatch.setattr(service, "skc_replace_row", fake_replace)
    return calls


@pytest.mark.asyncio
async def test_skc兜底重做破线行(tmp_path, monkeypatch):
    """没换成图的行里有破线图 → 整行下载重做合规化后替换。"""
    states = {"咖啡色": {"count": 2,
                       "urls": ["https://cbu01/1.jpg", "https://cbu01/2.jpg"],
                       "sizes": [[1000, 1000], [1200, 1200]],
                       "tooSmall": [{"idx": 0, "url": "https://cbu01/1.jpg",
                                     "size": "1000x1000"},
                                    {"idx": 1, "url": "https://cbu01/2.jpg",
                                     "size": "1200x1200"}]}}
    calls = _fake_skc_env(monkeypatch, tmp_path, states)
    ctx = {"workdir": str(tmp_path)}
    events = []

    async def emit(ev):
        events.append(ev)

    fixed = await service._skc_size_fallback(ctx, None, emit, ["粉红色", "咖啡色"],
                                             ["粉红色"])
    assert fixed == ["咖啡色"]
    # 整行替换：破线的和达标的都要重挂，只补破线那几张会让达标旧图被一并删掉
    assert calls["replaced"] == [("咖啡色", 2)]
    # 已换过图的行不许再动
    assert all("粉红色" not in str(c) for c in calls["replaced"])


@pytest.mark.asyncio
async def test_skc兜底跳过已达标行(tmp_path, monkeypatch):
    """行里图都达标就别动——重挂一遍纯属浪费且有换坏的风险。"""
    states = {"咖啡色": {"count": 2, "urls": ["u1", "u2"],
                       "sizes": [[1340, 1787], [1340, 1787]], "tooSmall": []}}
    calls = _fake_skc_env(monkeypatch, tmp_path, states)
    events = []

    async def emit(ev):
        events.append(ev)

    fixed = await service._skc_size_fallback({"workdir": str(tmp_path)}, None, emit,
                                             ["咖啡色"], [])
    assert fixed == [] and calls["replaced"] == []


@pytest.mark.asyncio
async def test_skc兜底替换失败不静默(tmp_path, monkeypatch):
    """兜底本身失败要发人工确认：此时行里仍是破线原图，保存还是会被拦。"""
    states = {"咖啡色": {"count": 1, "urls": ["https://cbu01/1.jpg"],
                       "sizes": [[1000, 1000]],
                       "tooSmall": [{"idx": 0, "url": "https://cbu01/1.jpg",
                                     "size": "1000x1000"}]}}
    calls = _fake_skc_env(monkeypatch, tmp_path, states,
                          replace_results={"咖啡色": {"status": "error",
                                                    "stage": "open-space"}})
    events = []

    async def emit(ev):
        events.append(ev)

    fixed = await service._skc_size_fallback({"workdir": str(tmp_path)}, None, emit,
                                             ["咖啡色"], [])
    assert fixed == []
    assert any(e["type"] == "manual_check" and "尺寸兜底替换失败" in e["message"]
               for e in events)


@pytest.mark.asyncio
async def test_skc兜底读不到行状态只告警(tmp_path, monkeypatch):
    calls = _fake_skc_env(monkeypatch, tmp_path, {})     # 查表查不到 → err
    events = []

    async def emit(ev):
        events.append(ev)

    fixed = await service._skc_size_fallback({"workdir": str(tmp_path)}, None, emit,
                                             ["咖啡色"], [])
    assert fixed == [] and calls["replaced"] == []
    assert any("读不到状态" in e.get("message", "") for e in events)


def test_live_state_变种属性区尺寸单独判():
    """张数够不代表尺寸合规：某行没换过图时 attrImgCount 照样非 0。"""
    from app.publish import pipeline

    js = pipeline._JS_LIVE_STATE
    assert "attrImgBad" in js
    assert "__MINW__" in js and "__MINH__" in js
    assert "if (w && h" in js or "w && h &&" in js     # 未加载完不算破线


# ---- 类目失效（2026-08-24 实测 890843533224）----------------------------------
# save 从未成功时 ③ 选定的类目也会丢，页面回落到认领带来的旧类目，而那个旧类目已被
# 平台下线：编辑页弹「该分类已在平台删除！」、分类行下方显示暗红「未选择分类」。
# 类目没生效 → 变种属性区不渲染尺码行 → ⑧⑨⑩⑪ 无处可填 → save 死循环。

def test_stale判定_类目失效时从类目起全跑():
    """catDeleted / catUnset 任一命中都要把 ③④ 拉进重跑集。"""
    base = {"rendered": True, "titleFilled": True, "skuRowCount": 8,
            "skuFilledRows": 8, "sizechartAdded": True, "attrImgCount": 6,
            "shippingSet": True, "descImgCount": 9, "descForeignCount": 0,
            "skuCodeCount": 8, "skuCodeBad": 0}
    # 表单成果看着都还在，但类目失效 → 仍要从 ③ 重来（那些成果建立在错类目上）
    for flag in ("catDeleted", "catUnset"):
        stale = service._stale_form_stages({**base, flag: True})
        assert "auto_cat" in stale and "attrs" in stale, flag
        assert set(stale) == set(service._FORM_ONLY_STAGES), flag


def test_stale判定_类目有效时不重跑类目():
    """③ 走类目树要 110s，类目没问题就绝不能顺手重跑。"""
    live = {"rendered": True, "titleFilled": False, "skuRowCount": 0,
            "skuFilledRows": 0, "sizechartAdded": False, "attrImgCount": 0,
            "shippingSet": False, "descImgCount": 12, "descForeignCount": 12,
            "skuCodeCount": 0, "skuCodeBad": 0,
            "catText": "女士运动卫裤", "catUnset": False, "catDeleted": False}
    stale = service._stale_form_stages(live)
    assert "auto_cat" not in stale and "attrs" not in stale
    assert "titles" in stale and "fix_sizes" in stale


def test_stale判定_实况读不到时连类目一起保守重跑():
    assert set(service._stale_form_stages({"rendered": False})) == set(service._FORM_ONLY_STAGES)


def test_live_state_含类目三信号():
    """catText/catUnset/catDeleted 是排查整单卡死的上游线索，JS 里必须都读。"""
    from app.publish import pipeline

    js = pipeline._JS_LIVE_STATE
    assert "catText" in js and "catUnset" in js and "catDeleted" in js
    assert "category-list" in js          # 分类行下方那个暗红提示块
    assert "分类已在平台删除" in js        # 页面弹的 d-message-error


@pytest.mark.asyncio
async def test_类目失效时连带重跑类目与属性(tmp_path, monkeypatch):
    calls, events = [], []
    _fake_stages(monkeypatch, calls)
    # 表单成果都还在，只有类目被平台删了
    _fake_live(monkeypatch, titleFilled=True, skuRowCount=8, skuFilledRows=8,
               sizechartAdded=True, attrImgCount=6, shippingSet=True,
               descForeignCount=0, skuCodeCount=8, skuCodeBad=0,
               catText="其他（女装长裤）", catUnset=True, catDeleted=True)
    state = service.load_state("rowid-103")
    for sid, _ in service.STAGES:
        if sid != "save":
            state["stages"][sid] = {"status": "ok", "elapsed_s": 1.0, "note": ""}
    service.save_state(state)

    r = await service.publish_one(None, {"rowid": "103", "info_path": "x.json"},
                                  "Pawly", on_progress=_collect(events))

    assert r["status"] == "ok"
    assert "auto_cat" in calls and "attrs" in calls
    assert calls.index("auto_cat") < calls.index("attrs")
    # ①② 仍跳过（本地产物与草稿都在）
    assert "extract" not in calls and "claim" not in calls
    msgs = [e["message"] for e in events if e["type"] == "manual_check"]
    assert any("编辑页数据已丢失" in m for m in msgs)


def test_stale判定_有破线图只重跑skc():
    """破线时不该连带重跑 ⑤b/⑥——那是素材图那条路，重跑要白烧生图。"""
    live = {"rendered": True, "titleFilled": True, "skuRowCount": 8,
            "skuFilledRows": 8, "sizechartAdded": True, "attrImgCount": 6,
            "attrImgBad": 6, "shippingSet": True, "descImgCount": 9,
            "descForeignCount": 0, "skuCodeCount": 8, "skuCodeBad": 0}
    stale = service._stale_form_stages(live)
    assert "skc" in stale
    assert "clean_images" not in stale and "material" not in stale


# ---- 阶段⑮ 立即发布的闸门（2026-08-24 新增）--------------------------------


async def _noop_emit(ev):
    """不关心事件的用例用它占位（阶段函数签名要求给个 emit）。"""
    return None


# 发布不可逆，所以闸门有两道：do_publish 没显式开就不跑；⑭ save 没成功也不跑
# （草稿没落库点发布只会重复撞同一批前端校验）。两道都用 skipped 而非 fail——
# 「按约定没发布」不是错误。

@pytest.mark.asyncio
async def test_publish阶段_默认不发布(monkeypatch):
    called = []

    async def fake_publish_now(session, rowid, confirm=False):
        called.append(confirm)
        return {"status": "ok"}

    monkeypatch.setattr(service, "publish_now", fake_publish_now)
    ctx = {"rowid": "1", "state": {"stages": {"save": {"status": "ok"}}}}
    r = await service._st_publish(ctx, None, _noop_emit)
    assert r["status"] == "skipped"
    assert not called          # 一次都没点


@pytest.mark.asyncio
async def test_publish阶段_save未成功不发布(monkeypatch):
    called = []

    async def fake_publish_now(session, rowid, confirm=False):
        called.append(confirm)
        return {"status": "ok"}

    monkeypatch.setattr(service, "publish_now", fake_publish_now)
    ctx = {"rowid": "1", "do_publish": True,
           "state": {"stages": {"save": {"status": "fail"}}}}
    r = await service._st_publish(ctx, None, _noop_emit)
    assert r["status"] == "skipped"
    assert not called


@pytest.mark.asyncio
async def test_publish阶段_开关加save成功才真发(monkeypatch):
    called = []

    async def fake_publish_now(session, rowid, confirm=False):
        called.append((rowid, confirm))
        return {"status": "ok", "messages": ["发布成功"]}

    monkeypatch.setattr(service, "publish_now", fake_publish_now)
    ctx = {"rowid": "42", "do_publish": True,
           "state": {"stages": {"save": {"status": "ok"}}}}
    r = await service._st_publish(ctx, None, _noop_emit)
    assert r["status"] == "ok"
    assert called == [("42", True)]     # confirm 必须显式为 True


@pytest.mark.asyncio
async def test_publish阶段_判据不足记fail并转人工(monkeypatch):
    """点下去了但没抓到成功提示：不敢判成功（可能真上架了），交人工确认。"""
    events = []

    async def fake_publish_now(session, rowid, confirm=False):
        return {"status": "unknown", "messages": []}

    monkeypatch.setattr(service, "publish_now", fake_publish_now)

    async def emit(ev):
        events.append(ev)

    ctx = {"rowid": "1", "do_publish": True,
           "state": {"stages": {"save": {"status": "ok"}}}}
    r = await service._st_publish(ctx, None, emit)
    assert r["status"] == "fail"
    assert [e["type"] for e in events] == ["manual_check"]


@pytest.mark.asyncio
async def test_do_publish不进状态文件(tmp_path, monkeypatch):
    """发布意愿属于本次运行，不该被续跑继承——否则重跑一次就静默又发一遍。"""
    calls = []
    _fake_stages(monkeypatch, calls)
    await service.publish_one(None, {"rowid": "1", "info_path": "x.json"}, "Pawly",
                              do_publish=True)
    state = service.load_state("rowid-1")
    assert "do_publish" not in state


# ---- 阶段⑮ 的发布取证（2026-08-24 真站结论）--------------------------------
# 成功 toast 抓不到、页面也不跳转，故判据只能去列表拿服务端事实。
# 见 pipeline._publish_landed 上方注释。

def test_发布取证不依赖前端提示():
    """publish_now 的判据里不能再出现「离开编辑页」那套信号。"""
    import inspect

    from app.publish import pipeline as P

    srcbody = inspect.getsource(P.publish_now)
    assert "_publish_landed" in srcbody          # 走列表取证
    assert "leftEditPage" not in srcbody         # 旧的前端判据已移除


def test_取证JS按rowid精确匹配行():
    from app.publish import pipeline as P

    js = P._JS_LIST_HAS_ROW
    # 属性选择器必须带引号：rowid 是数字开头，不加引号是非法选择器（实测报 SyntaxError）
    assert 'tr[rowid="' in js
    # 要等表格渲染，否则「还没加载」会被误判成「不在这个列表里」
    assert "tr[rowid]" in js and "sleep" in js
