# -*- coding: utf-8 -*-
"""判 keep 的描述图转存到店小秘图床（不碰 CDP、不碰 LLM、不生图、不联网）。

钉住 2026-08-30 日志里每轮必现的「描述保存后 仍有外链图未转存：['cbu01.alicdn.com']」。

成因取证（rowid 173539495458369319，2026-08-30 真站只读探查）：认领时平台把 1688
描述图【按外链原样】挂在描述区，编辑页描述区 10 张图【全部】在 cbu01；而当轮计划只
删 3 换 1，判 keep 的 6 张谁都不会去动，于是 desc_save 回读必然报外链未转存。
原先把这条告警一律归因成「有图替换失败」，只对替换失败那一种成因成立——keep 的图
从来没人管过，属于漏了一条链路。

修复：keep 且不在店小秘图床上的图，下载原图后原样重挂（画面一个像素都不动）。
"""
import os

import pytest

from app.publish import service


class _Session:
    """desc_map 由 _resolve_desc_pos 调，这里只需要它能按 URL 报出当前 pos。"""

    def __init__(self):
        self.replaced: list = []


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """把三个外部依赖换成可控桩：定位 pos、下载、替换。"""
    state = {"downloads": [], "replaced": [], "resolve_fail": set(),
             "replace_fail": set(), "fatal": set()}

    async def _resolve(session, url):
        if url in state["fatal"]:
            return 0, "页签已被导航走", True
        if url in state["resolve_fail"]:
            return 0, "描述区已找不到这张源图", False
        return state["pos"][url], "", False

    def _download(url, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(b"x" * 100)
        state["downloads"].append(url)
        return 100

    async def _replace(session, pos, path, expect_url=None, **kw):
        if expect_url in state["replace_fail"]:
            return {"status": "error", "stage": "upload", "err": "尺寸不达标"}
        state["replaced"].append({"pos": pos, "url": expect_url, "path": path})
        return {"status": "ok"}

    monkeypatch.setattr(service, "_resolve_desc_pos", _resolve)
    monkeypatch.setattr(service.extract, "_download_image", _download)
    monkeypatch.setattr(service, "desc_replace", _replace)
    state["workdir"] = str(tmp_path)
    return state


def _mods(n, hosted=()):
    """造 n 张描述图；hosted 里的 pos 算已在店小秘图床上。"""
    out = []
    for p in range(1, n + 1):
        if p in hosted:
            url = f"https://wxalbum-10001658-file.dianxiaomi.com/a/{p}.jpg"
        else:
            url = f"https://cbu01.alicdn.com/img/ibank/O1CN{p}.jpg"
        out.append({"pos": p, "url": url, "onDxmHost": p in hosted})
    return out


async def _noop_emit(ev):
    return None


@pytest.mark.asyncio
async def test_keep的外链图被转存(stub):
    """真实现场：10 张全在 cbu01，keep 6 张 → 这 6 张都要下载后重挂。"""
    mods = _mods(10)
    stub["pos"] = {m["url"]: m["pos"] for m in mods}
    keep = [2, 4, 5, 7, 8, 9]
    r = await service._rehost_desc_keeps(
        {"workdir": stub["workdir"]}, _Session(), mods, keep, _noop_emit)
    assert r["done"] == 6 and r["failed"] == [], r
    assert sorted(x["pos"] for x in stub["replaced"]) == keep
    # 转存必须原样挂原图，不能走生图/放大产物
    assert len(stub["downloads"]) == 6


@pytest.mark.asyncio
async def test_已在图床的keep图不动(stub):
    """已经是店小秘地址的图没有转存必要，碰它只是白花一次上传。"""
    mods = _mods(4, hosted=(1, 2, 3, 4))
    stub["pos"] = {m["url"]: m["pos"] for m in mods}
    r = await service._rehost_desc_keeps(
        {"workdir": stub["workdir"]}, _Session(), mods, [1, 2, 3, 4], _noop_emit)
    assert r["done"] == 0 and stub["replaced"] == []
    assert stub["downloads"] == []


@pytest.mark.asyncio
async def test_只碰keep不碰replace和delete(stub):
    """replace 的图由替换轮处理、delete 的已经删了，转存只捡 keep。"""
    mods = _mods(5)
    stub["pos"] = {m["url"]: m["pos"] for m in mods}
    r = await service._rehost_desc_keeps(
        {"workdir": stub["workdir"]}, _Session(), mods, [3], _noop_emit)
    assert r["done"] == 1
    assert [x["pos"] for x in stub["replaced"]] == [3]


@pytest.mark.asyncio
async def test_pos按现查而非计划序号(stub):
    """删图后描述区整体前移，必须按 URL 现查 pos（同 _replace_round 的取向）。"""
    mods = _mods(3)
    # 页面实况：这张图现在在第 1 位，而 mods 里记的是 3
    stub["pos"] = {mods[2]["url"]: 1, mods[0]["url"]: 2, mods[1]["url"]: 3}
    r = await service._rehost_desc_keeps(
        {"workdir": stub["workdir"]}, _Session(), mods, [3], _noop_emit)
    assert r["done"] == 1
    assert stub["replaced"][0]["pos"] == 1, "应按现查 pos 替换，否则张冠李戴"


@pytest.mark.asyncio
async def test_单张失败不影响其余(stub):
    """best-effort：一张传不上去只记一笔，其余照转。"""
    mods = _mods(4)
    stub["pos"] = {m["url"]: m["pos"] for m in mods}
    stub["replace_fail"].add(mods[1]["url"])
    r = await service._rehost_desc_keeps(
        {"workdir": stub["workdir"]}, _Session(), mods, [1, 2, 3, 4], _noop_emit)
    assert r["done"] == 3 and len(r["failed"]) == 1
    assert "尺寸不达标" in str(r["failed"][0]["why"])


@pytest.mark.asyncio
async def test_页签被导航走立刻收工(stub):
    """fatal 对后续每一张都成立，逐张重试只会刷同样的噪音。"""
    mods = _mods(6)
    stub["pos"] = {m["url"]: m["pos"] for m in mods}
    stub["fatal"].add(mods[1]["url"])
    msgs = []

    async def _emit(ev):
        msgs.append(ev)

    r = await service._rehost_desc_keeps(
        {"workdir": stub["workdir"]}, _Session(), mods, [1, 2, 3, 4, 5, 6], _emit)
    # 第 1 张成功，第 2 张 fatal 后不再继续
    assert r["done"] == 1
    assert len(stub["replaced"]) == 1
    assert any("续跑" in (m.get("message") or "") for m in msgs)


@pytest.mark.asyncio
async def test_复用已下载的原图不重复下载(stub):
    """产物按 URL 哈希落 desc-edit/，重跑时不该再下一遍。"""
    mods = _mods(2)
    stub["pos"] = {m["url"]: m["pos"] for m in mods}
    local, _en = service._desc_cache_paths(stub["workdir"], mods[0]["url"])
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "wb") as f:
        f.write(b"cached")
    r = await service._rehost_desc_keeps(
        {"workdir": stub["workdir"]}, _Session(), mods, [1, 2], _noop_emit)
    assert r["done"] == 2
    assert mods[0]["url"] not in stub["downloads"], "已落盘的原图不该重复下载"
    assert mods[1]["url"] in stub["downloads"]


@pytest.mark.asyncio
async def test_无keep图时不做无谓动作(stub):
    mods = _mods(3)
    stub["pos"] = {m["url"]: m["pos"] for m in mods}
    r = await service._rehost_desc_keeps(
        {"workdir": stub["workdir"]}, _Session(), mods, [], _noop_emit)
    assert r == {"done": 0, "failed": [], "skipped": 0}


def test_note带上转存数():
    """结论文案要能看出转存了几张，否则「10 张全在 cbu01」这类情况看不出做了什么。"""
    note = service._desc_note(3, 1, "", 0, 1, rehosted=6)
    assert "转存 6 张" in note
    # 没转存时不要多出一段空文案
    assert "转存" not in service._desc_note(3, 1, "", 0, 1)
