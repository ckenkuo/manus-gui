# -*- coding: utf-8 -*-
"""描述图尺寸闸门与⑧ 缺失尺码告警（不碰 CDP、不碰 LLM、不生图）。

钉住 2026-08-29 那一单暴露的两个系统性缺口：
  1. `upload_image` 的尺寸闸一律套服装 1340×1785，把达标的 480×480 描述图拒掉，
     页面留着 1688 原始外链、⑬ 整单失败——被拒的图其实合规。
  2. ⑧ 的 `missing`（源尺码在页面无对应框）被 service 层整个丢掉，note 只剩
     「源尺码 5 个 | SKU 表 4 行」，少了谁全靠人对数字。
"""
import asyncio
import json
import os

import pytest
from PIL import Image

from app.publish import images, pipeline, service, upload


def _img(path, w, h):
    Image.new("RGB", (w, h), (200, 180, 160)).save(path, quality=88)
    return str(path)


# ---- upload_image 的尺寸闸按用途取下限 --------------------------------------

class _UpSession:
    """只走到 size-check 就够了：闸门放行后取签名返回空，用它当「过了闸」的标记。"""

    def __init__(self):
        self.reached_sign = False

    async def eval_json(self, js, *a, **kw):
        self.reached_sign = True
        return {}


@pytest.mark.asyncio
async def test_描述图按480下限过闸(tmp_path):
    """真实现场复现：480×480 的描述图达标（两边 >= 480），不该被拒。"""
    p = _img(tmp_path / "desc.jpg", 480, 480)
    s = _UpSession()
    r = await upload.upload_image(s, p, min_w=images.DESC_MIN_W,
                                 min_h=images.DESC_MIN_H)
    assert r.get("stage") != "size-check", r
    assert s.reached_sign, "闸门应放行并继续走到取签名"


@pytest.mark.asyncio
async def test_不传下限仍按服装1340x1785把关(tmp_path):
    """闸门本身要留：素材图/SKC 图不传参时行为一行不变。"""
    p = _img(tmp_path / "small.jpg", 480, 480)
    s = _UpSession()
    r = await upload.upload_image(s, p)
    assert r["status"] == "error" and r["stage"] == "size-check"
    assert "1340" in r["err"] and not s.reached_sign


@pytest.mark.asyncio
async def test_描述图仍拦真正过小的图(tmp_path):
    """放宽下限不等于不把关：80×80（本次那张的源图尺寸）必须拦。"""
    p = _img(tmp_path / "tiny.jpg", 80, 80)
    s = _UpSession()
    r = await upload.upload_image(s, p, min_w=images.DESC_MIN_W,
                                  min_h=images.DESC_MIN_H)
    assert r["status"] == "error" and r["stage"] == "size-check"
    assert "480" in r["err"] and not s.reached_sign


def test_那张图本就符合描述图要求(tmp_path):
    """判据自证：480×480 过 check_desc_size，却过不了服装闸门。"""
    p = _img(tmp_path / "d.jpg", 480, 480)
    assert images.check_desc_size(480, 480)["ok"] is True
    assert images.check_cloth_size(p)["ok"] is False          # 误拒的来源
    assert images.check_cloth_size(p, min_w=images.DESC_MIN_W,
                                   min_h=images.DESC_MIN_H)["ok"] is True


def test_desc_replace传的是描述图下限():
    """调用点必须显式传 DESC_MIN_*，否则闸门又退回服装口径。"""
    import inspect
    src = inspect.getsource(pipeline.desc_replace)
    assert "images.DESC_MIN_W" in src and "images.DESC_MIN_H" in src


# ---- 落盘产物尺寸不达标时不能当缓存命中 -------------------------------------

@pytest.mark.asyncio
async def test_落盘产物过小则不算缓存命中(tmp_path, monkeypatch):
    """质检管画面不管像素：历史产物可能是更早口径出的小图，尺寸要复查一次。"""
    wd = tmp_path / "wd"
    (wd / "desc-edit").mkdir(parents=True)
    url = "https://cbu01.alicdn.com/img/ibank/x.jpg"
    local, en = service._desc_cache_paths(str(wd), url)
    _img(en, 80, 80)          # 落盘产物过小

    calls = []

    def _dl(u, dst):
        calls.append(u)
        _img(dst, 80, 80)
        # 与真实 _download_image 一致返回字节数：0/None 表示「源站取不到」，
        # 调用方据此跳过该张（见 extract._download_image 的 404 分支）
        return os.path.getsize(dst)
    monkeypatch.setattr("app.publish.extract._download_image", _dl)
    monkeypatch.setattr(images, "compress",
                        lambda p, **kw: _img(p, 480, 480))

    r = await service._prepare_desc_image(
        str(wd), {"url": url, "needsUpscale": True, "reason": "尺寸不足"})
    assert r["ok"] and r["how"] == "upscaled", r
    assert calls, "应当当缓存未命中、重新走放大分支"


@pytest.mark.asyncio
async def test_落盘产物达标才算缓存命中(tmp_path, monkeypatch):
    """零回归：达标的产物照旧直接复用，不重新下载也不重新生图。"""
    wd = tmp_path / "wd"
    (wd / "desc-edit").mkdir(parents=True)
    url = "https://cbu01.alicdn.com/img/ibank/y.jpg"
    _local, en = service._desc_cache_paths(str(wd), url)
    _img(en, 480, 480)

    def _boom(*a, **kw):
        raise AssertionError("达标产物不该重新下载")
    monkeypatch.setattr("app.publish.extract._download_image", _boom)

    r = await service._prepare_desc_image(str(wd), {"url": url})
    assert r == {"ok": True, "path": en, "how": "cached"}


# ---- ⑧ 缺失尺码要报 manual_check --------------------------------------------

class _SizeSess:
    def __init__(self, page):
        self.page = page

    async def eval_json(self, js, *a, **kw):
        if "skuAttrsInfo" in js and "d-checkbox" in js:
            return [{"t": s, "c": True} for s in self.page]
        if "skuDataInfo" in js:
            return {"n": len(self.page)}
        if "productBasicInfo" in js:
            return {"snippet": "产品分类 女童长裤套装"}
        return {}


def _info(tmp_path, sizes):
    p = tmp_path / "product-info.json"
    p.write_text(json.dumps({"skus": {s: {} for s in sizes}}, ensure_ascii=False),
                 encoding="utf-8")
    return str(p)


@pytest.mark.asyncio
async def test_缺失尺码结构化返回(tmp_path):
    """missing 要单出一个键，供 service 发 manual_check（不靠解析 warning 串）。"""
    # 页面最小月龄档是 9-12M：源 6-9m 无框可勾（真实现场）
    r = await pipeline.fix_sizes(
        _SizeSess(["9-12M", "12-18M", "18-24M", "2-3Y"]),
        _info(tmp_path, ["6-9m", "9-12m", "12-18m", "18-24m", "2-3y"]))
    assert r["status"] == "ok"
    assert r["missing"] == ["6-9m"], r
    assert "6-9m" in r["warning"]


@pytest.mark.asyncio
async def test_service层把缺失尺码报成manual_check(tmp_path, monkeypatch):
    events = []

    async def _emit(ev):
        events.append(ev)

    async def _fake(session, info_path, **kw):
        return {"status": "ok", "wantedSizes": ["6-9m", "9-12m"],
                "rowCount": 4, "missing": ["6-9m"]}
    monkeypatch.setattr(service, "fix_sizes", _fake)

    r = await service._st_fix_sizes({"info_path": "x.json"}, None, _emit)
    assert r["status"] == "ok"
    assert "6-9m" in r["note"]
    mc = [e for e in events if e["type"] == "manual_check"]
    assert mc and "6-9m" in mc[0]["message"]
    # 缺失往往是类目选错，提示里要点出来
    assert "类目" in mc[0]["message"]


@pytest.mark.asyncio
async def test_无缺失时不发manual_check(tmp_path, monkeypatch):
    """零回归：全部命中时 note 与事件都与从前一致。"""
    events = []

    async def _emit(ev):
        events.append(ev)

    async def _fake(session, info_path, **kw):
        return {"status": "ok", "wantedSizes": ["6-9m"], "rowCount": 5}
    monkeypatch.setattr(service, "fix_sizes", _fake)

    r = await service._st_fix_sizes({"info_path": "x.json"}, None, _emit)
    assert r["status"] == "ok" and r["note"] == "源尺码 1 个 | SKU 表 5 行"
    assert not [e for e in events if e["type"] == "manual_check"]
