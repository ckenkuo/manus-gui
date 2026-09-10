# -*- coding: utf-8 -*-
"""阶段⑬ 描述图替换失败后的处置：重试一轮 + 尺寸破线不放行。离线单测。

2026-08-28 线上表现（pdd-994437651298，logs/20260828102412.log）：⑬ 计划替换 6 张，
6 张【全部】因同一个页面遮挡失败（「更换图片」链接被 fixed 顶栏压住，见
tests/test_publish_desc_link_occluded.py）。后果是两层：

  1. 6 张 1688 原图原样留着 → desc_save 回读报「仍有外链未转存 img.pddpic.com」
     且 6 张 1200x1200 全低于服装硬红线 1340x1785；
  2. 阶段⑬ 只 emit 了一条 manual_check 就【返回 ok】，流程一路走到「立即发布」，
     10:35:45 真把这个商品发了出去——带着 6 张中文原图、且尺寸不达标。

遮挡本身已在 pipeline 侧修，但页面态问题不可能一次穷尽。按项目取向（发布流程全自动，
靠数据可靠性 + 自动重试而非人工卡点，见 CLAUDE.md 与 publish-full-auto-data-quality），
这里钉三件事：
  1. 有图没换成时，自动重开编辑器重试一轮（同一页面态下重试没意义，必须先关再开）
  2. 重试前必须先 desc_save 把已经换成的落下来（关编辑器会丢弃未保存的改动）
  3. 尺寸破线时本阶段必须 fail，不能带着不合规的图走到发布
不覆盖真实页面（要 Chrome 和登录态）。
"""

from publish_patching import patch_publish
import asyncio
import os

import pytest

from app.publish import service as S


def _env(tmp_path, monkeypatch, calls: dict, replace_results: list,
         save_result: dict = None):
    """搭一套「⑬ 只剩替换与保存两件事」的假环境。

    replace_results 是 desc_replace 按调用次序返回的结果列表（用完后一律算成功），
    这样各用例只描述「第几次成功/失败」，不必各自写一遍桩。
    """
    plan = {"delete": [], "replace": [{"pos": i, "url": f"http://x/{i}.jpg"}
                                      for i in (1, 2)]}

    async def _map(session, info_path=""):
        return {"status": "ok",
                "modules": [{"pos": i, "url": f"http://x/{i}.jpg"} for i in (1, 2)]}

    async def _plan_desc(mods, info):
        return plan

    async def _resolve(session, url):
        # 源 URL 形如 http://x/N.jpg，直接拿 N 当当前序号
        return int(url.rsplit("/", 1)[-1].split(".")[0]), "", False

    async def _replace(session, pos, path, expect_url=None, **kw):
        calls.setdefault("replaced", []).append(expect_url)
        i = len(calls["replaced"]) - 1
        if i < len(replace_results):
            return replace_results[i]
        return {"status": "ok"}

    async def _save(session):
        calls["saves"] = calls.get("saves", 0) + 1
        # 只有【最后】那次保存用给定的结果：前面的是重试前的中途保存
        if save_result and calls["saves"] > 1:
            return save_result
        if save_result and not replace_results:
            return save_result
        return {"status": "ok", "descImgs": 2, "dxmHosted": 2}

    async def _closed(session):
        calls["closed"] = calls.get("closed", 0) + 1
        # 记下关闭发生在第几次保存之后，用来验证「先保存再关」的次序
        calls.setdefault("closeAfterSaves", []).append(calls.get("saves", 0))
        return {"status": "ok"}

    # 第 1 张走「缓存命中」这条最短的路：本测只关心重试与放行，不测生图
    en = tmp_path / "desc-edit"
    en.mkdir()

    def _paths(workdir, url):
        p = str(en / (url.rsplit("/", 1)[-1] + ".jpg"))
        with open(p, "wb") as f:
            f.write(b"x")
        return p, p

    patch_publish(monkeypatch, "service", "desc_map", _map)
    monkeypatch.setattr(S.vision, "plan_desc", _plan_desc)
    patch_publish(monkeypatch, "service", "_resolve_desc_pos", _resolve)
    patch_publish(monkeypatch, "service", "_desc_cache_paths", _paths)
    patch_publish(monkeypatch, "service", "desc_replace", _replace)
    patch_publish(monkeypatch, "service", "desc_save", _save)
    patch_publish(monkeypatch, "service", "ensure_desc_closed", _closed)
    patch_publish(monkeypatch, "service", "_load_info", lambda p: {})
    return {"info_path": "", "workdir": str(tmp_path)}


def _run(ctx, msgs):
    async def _emit(ev):
        msgs.append(ev)
    return asyncio.run(S._st_desc(ctx, None, _emit))


def test_有图没换成时自动重试一轮(tmp_path, monkeypatch):
    """2026-08-28 那次 6 张全挂后一次都没重试，直接带着原图往下走。

    重试只针对失败的那几张（已经换成的不许再动——重复替换等于白烧一次上传，
    还可能撞上 expect_url 闸门，因为它的源图已经不是 1688 那张了）。
    """
    calls, msgs = {}, []
    # 第 1 张成功、第 2 张失败；重试轮里第 2 张成功
    ctx = _env(tmp_path, monkeypatch, calls, [
        {"status": "ok"},
        {"status": "error", "stage": "menu", "err": "描述专属菜单未展开"},
    ])

    r = _run(ctx, msgs)

    assert r["status"] == "ok", f"重试成功后该是 ok，实际 {r}"
    # 首轮两张 + 重试轮只重试失败的那一张
    assert calls["replaced"] == ["http://x/1.jpg", "http://x/2.jpg", "http://x/2.jpg"], \
        f"只该重试失败的那张，实际 {calls['replaced']}"
    assert "替换 2 张" in r["note"], f"重试换成的也要计入，实际 {r['note']}"


def test_重试前先保存再关编辑器(tmp_path, monkeypatch):
    """关编辑器会丢弃未保存的改动——先关再存等于把首轮换成的那几张白扔了。

    描述编辑器点「关闭」即丢弃（见 desc_replace 的 hint），故次序必须是存→关→重开。
    """
    calls, msgs = {}, []
    ctx = _env(tmp_path, monkeypatch, calls, [
        {"status": "ok"},
        {"status": "error", "stage": "menu", "err": "菜单未展开"},
    ])

    _run(ctx, msgs)

    assert calls["saves"] >= 2, "重试前要先保存一次，收尾再保存一次"
    # 每次关闭都必须发生在至少一次保存之后
    assert all(n >= 1 for n in calls["closeAfterSaves"]), \
        f"关编辑器前必须先保存，实际关闭时机 {calls['closeAfterSaves']}"


def test_重试仍失败时报清楚且不假装成功(tmp_path, monkeypatch):
    """重试也换不上就得如实说：这时页面上仍留着 1688 原图。"""
    calls, msgs = {}, []
    err = {"status": "error", "stage": "menu", "err": "描述专属菜单未展开"}
    ctx = _env(tmp_path, monkeypatch, calls, [err, err, err, err])

    r = _run(ctx, msgs)

    assert calls["replaced"].count("http://x/1.jpg") == 2, "两张都该各重试一次"
    fails = [m["message"] for m in msgs
             if m.get("type") == "manual_check" and "替换失败" in m.get("message", "")]
    assert len(fails) == 4, f"两轮各两张失败都要报出来，实际 {fails}"
    # 一张都没换成、也没删图，走的是既有的 skipped 路（"N 张全部保留"）——不是 ok，
    # 免得「替换 0 张」这种结论被当成正常完成
    assert r["status"] == "skipped" and "全部保留" in r["note"], \
        f"全没换成时不许报成正常完成，实际 {r}"


def test_尺寸破线时本阶段必须fail(tmp_path, monkeypatch):
    """2026-08-28 的第二层错误：明知有图不合规，⑬ 仍返回 ok，商品被发了出去。

    这条闸门是保存时【静默】校验的，带着不合规的图往下走，最终会在发布后被平台
    弹回，且回执不说是哪张图。
    【判据是描述图自己那套】同日用户截图取证：描述图要求「比例 0.5~2、两边 >= 480」,
    不是服装的 1340x1785（那条只管 SKC/素材图）。tooSmall 由 desc_save 按新口径算，
    本用例只验「⑬ 收到 tooSmall 就必须 fail 且报清数量」这条编排行为。
    """
    calls, msgs = {}, []
    ctx = _env(tmp_path, monkeypatch, calls, [], save_result={
        "status": "validation-error", "descImgs": 2, "dxmHosted": 2,
        "tooSmall": [{"pos": 2, "size": "300x300"}, {"pos": 3, "size": "1200x400"}],
        "foreignHosts": ["img.pddpic.com"]})

    r = _run(ctx, msgs)

    assert r["status"] == "fail", f"尺寸破线绝不能放行到发布，实际 {r}"
    assert "480" in r["note"], f"结论要点明是哪条规则，实际 {r['note']}"
    # 破线的张数要报出来，否则人得自己去页面上数
    assert "2 张" in r["note"], f"要报出破线张数，实际 {r['note']}"


def test_尺寸破线fail前仍要关掉编辑器(tmp_path, monkeypatch):
    """描述编辑器是全屏 modal，开着会盖住整个编辑页，后续阶段全点不中。

    fail 之后同一浏览器会话仍可能被续跑复用，故关闭动作不能留在 return 之后
    （2026-08-24 实测：⑬ 跑完编辑器留着，阶段⑦ 连续两次 open-space 失败）。
    """
    calls, msgs = {}, []
    ctx = _env(tmp_path, monkeypatch, calls, [], save_result={
        "status": "validation-error", "descImgs": 2, "dxmHosted": 2,
        "tooSmall": [{"pos": 2, "size": "1200x1200"}]})

    r = _run(ctx, msgs)

    assert r["status"] == "fail"
    assert calls.get("closed"), "fail 也必须先把描述编辑器关掉"


def test_只有外链未转存不算硬错误(tmp_path, monkeypatch):
    """外链可能本来就是采集时的原始状态（本商品没替换过描述图）。

    见 desc_save 的回读注释：那种情况不该拖垮整个商品，仍报 manual_check 但放行。
    尺寸破线才是硬闸——两者必须区别对待。
    """
    calls, msgs = {}, []
    ctx = _env(tmp_path, monkeypatch, calls, [], save_result={
        "status": "validation-error", "descImgs": 2, "dxmHosted": 1,
        "foreignHosts": ["cbu01.alicdn.com"], "tooSmall": []})

    r = _run(ctx, msgs)

    assert r["status"] == "ok", f"只有外链问题时应放行，实际 {r}"
    notes = [m["message"] for m in msgs if m.get("type") == "manual_check"]
    assert any("外链" in m for m in notes), f"但仍要报出来，实际 {notes}"


def test_页签被导航走时不重试(tmp_path, monkeypatch):
    """fatal 错误对后续每一张都成立，重开编辑器无从下手——重试只会把噪音翻倍。

    这与 test_publish_desc_navigated_away 盯的是同一件事：那里管首轮不逐张试，
    这里管别再整轮重来一遍。
    """
    calls, msgs = {}, []
    ctx = _env(tmp_path, monkeypatch, calls, [])

    async def _resolve_fatal(session, url):
        calls["resolve"] = calls.get("resolve", 0) + 1
        return 0, "页签已不在编辑页（当前 .../draft）", True
    patch_publish(monkeypatch, "service", "_resolve_desc_pos", _resolve_fatal)

    _run(ctx, msgs)

    assert calls["resolve"] == 1, f"fatal 后一次都不该再定位，实际 {calls['resolve']}"
    assert not calls.get("replaced"), "页面都不在了，不该有任何替换动作"
