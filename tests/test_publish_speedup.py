# -*- coding: utf-8 -*-
"""发布管线提速改造的离线单测：生图并发可配置、描述图并发备料、纯本地判断提前预热。

这些测试守的是【改造没有改变判定】：
- 并发数由用户配置决定，且写 store/site 时不被抹掉；
- 描述图备料并发跑，但「源图不在页面上不生图」这道省钱闸仍在（另见
  test_publish_service.py 里那条更早的测试）；
- 预热命中时不再重复调 LLM，预热失败/未跑时各阶段照原路现场算（行为与改造前一致）。
"""

from publish_patching import patch_publish
import asyncio
import inspect
import json
import os

import pytest

from app.publish import service


@pytest.fixture(autouse=True)
def _isolate_prefs(tmp_path, monkeypatch):
    """prefs 重定向到临时目录，不污染真实 workspace/publish_prefs.json。"""
    patch_publish(monkeypatch, "service", "PREFS_PATH", str(tmp_path / "publish_prefs.json"))


# ---- 生图并发可配置 ----------------------------------------------------------

def test_并发数默认取30():
    assert service.get_image_concurrency() == service.IMAGE_CONCURRENCY_DEFAULT == 30


def test_并发数可配置并生效():
    assert service.set_image_concurrency(12) == 12
    assert service.get_image_concurrency() == 12


def test_并发数非法值回落默认():
    """手改配置文件写了鬼东西时不能让整条管线起不来（照 prefs 的既有取向）。"""
    service.save_prefs({"imageConcurrency": "很多"})
    assert service.get_image_concurrency() == service.IMAGE_CONCURRENCY_DEFAULT
    service.save_prefs({"imageConcurrency": 0})
    assert service.get_image_concurrency() == 1          # 夹到下限
    service.save_prefs({"imageConcurrency": 9999})
    assert service.get_image_concurrency() == service.IMAGE_CONCURRENCY_MAX


def test_并发数越界时设置被拒():
    for bad in (0, -3, service.IMAGE_CONCURRENCY_MAX + 1, "x"):
        with pytest.raises(ValueError):
            service.set_image_concurrency(bad)


def test_写store不抹掉并发配置():
    """run_batch 每次启动都 save_prefs({store, site})，覆盖语义会静默清掉用户设的并发数。"""
    service.set_image_concurrency(7)
    service.save_prefs({"store": "某店", "site": "美国"})
    assert service.get_image_concurrency() == 7, "save_prefs 必须是合并语义"
    assert service.load_prefs()["store"] == "某店"


# ---- 描述图并发备料 ----------------------------------------------------------

def _prep_env(tmp_path, monkeypatch, calls: dict, delay: float = 0.0):
    """把下载/生图/质检换成假的，生图带可选延时以便观察是否真并发。"""
    def fake_download(url, dst, retries=3):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(b"x")
        return 1

    def fake_edit(path, prompt=None, out_path=None, **kw):
        # 同步阻塞函数：若调用方没走 to_thread，这个 sleep 会把事件循环整个占住，
        # 下面「墙钟远小于串行耗时」那条断言就会失败——这正是要守的点。
        import time
        time.sleep(delay)
        calls["edit"] = calls.get("edit", 0) + 1
        out = out_path or (os.path.splitext(path)[0] + "-edited.jpg")
        with open(out, "wb") as f:
            f.write(b"edited")
        return {"status": "ok", "output": out}

    async def fake_qc(path):
        calls["qc"] = calls.get("qc", 0) + 1
        return {"clean": True}

    monkeypatch.setattr(service.extract, "_download_image", fake_download)
    monkeypatch.setattr(service.images, "edit_image", fake_edit)
    monkeypatch.setattr(service.vision, "check_cleaned", fake_qc)


@pytest.mark.asyncio
async def test_备料并发跑而非串行(tmp_path, monkeypatch):
    """6 张各阻塞 0.2s，并发 6 时墙钟应远小于串行的 1.2s。

    这同时守住 to_thread：edit_image 是同步 curl 子进程，直接 await 调用会阻塞
    事件循环，并发就成了摆设（⑤b 清理早就是 to_thread 写法，⑬ 原先不是）。
    """
    calls = {}
    _prep_env(tmp_path, monkeypatch, calls, delay=0.2)
    service.set_image_concurrency(6)
    reps = [{"pos": i, "url": f"https://cdn/{i}.jpg"} for i in range(1, 7)]

    async def _emit(ev):
        return None

    t0 = asyncio.get_event_loop().time()
    out = await service._prewarm_desc_images(str(tmp_path), reps, _emit)
    elapsed = asyncio.get_event_loop().time() - t0

    assert len(out) == 6 and all(v["ok"] for v in out.values())
    assert calls["edit"] == 6
    assert elapsed < 0.8, f"墙钟 {elapsed:.2f}s 接近串行 1.2s，说明并发没生效"


@pytest.mark.asyncio
async def test_备料尊重并发上限(tmp_path, monkeypatch):
    """并发设 1 时应退化成串行——用户把它调小就是为了压住网络拥堵，必须真的生效。"""
    calls = {}
    _prep_env(tmp_path, monkeypatch, calls, delay=0.1)
    service.set_image_concurrency(1)
    reps = [{"pos": i, "url": f"https://cdn/{i}.jpg"} for i in range(1, 4)]

    async def _emit(ev):
        return None

    t0 = asyncio.get_event_loop().time()
    await service._prewarm_desc_images(str(tmp_path), reps, _emit)
    elapsed = asyncio.get_event_loop().time() - t0
    assert elapsed >= 0.25, f"并发 1 应串行（≥0.3s），实测 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_备料命中落盘缓存不重烧(tmp_path, monkeypatch):
    """产物已在 desc-edit/ 里就直接复用，一次生图都不发（重跑很常见，见模块头注释）。"""
    calls = {}
    _prep_env(tmp_path, monkeypatch, calls)
    url = "https://cdn/a.jpg"
    _, en_path = service._desc_cache_paths(str(tmp_path), url)
    os.makedirs(os.path.dirname(en_path), exist_ok=True)
    with open(en_path, "wb") as f:
        f.write(b"already-clean")

    async def _emit(ev):
        return None

    out = await service._prewarm_desc_images(str(tmp_path), [{"pos": 1, "url": url}], _emit)
    assert out[url]["how"] == "cached"
    assert "edit" not in calls and "qc" not in calls, "缓存命中不该再生图、也不该重复质检"




@pytest.mark.asyncio
async def test_备料单张失败不影响其它张(tmp_path, monkeypatch):
    """best-effort：一张烧不出来只记原因，其余照常就绪（⑬ 会按原图保留那一张）。"""
    calls = {}
    _prep_env(tmp_path, monkeypatch, calls)
    real_edit = service.images.edit_image

    def selective(path, prompt=None, out_path=None, **kw):
        """第 2 张失败，其余成功（并发设 1 才能稳定指定是第几张）。"""
        calls["n"] = calls.get("n", 0) + 1
        if calls["n"] == 2:
            raise RuntimeError("网关 503")
        return real_edit(path, prompt=prompt, out_path=out_path, **kw)

    monkeypatch.setattr(service.images, "edit_image", selective)
    service.set_image_concurrency(1)
    reps = [{"pos": i, "url": f"https://cdn/{i}.jpg"} for i in range(1, 4)]

    async def _emit(ev):
        return None

    out = await service._prewarm_desc_images(str(tmp_path), reps, _emit)
    oks = [v for v in out.values() if v.get("ok")]
    bads = [v for v in out.values() if not v.get("ok")]
    assert len(oks) == 2 and len(bads) == 1
    assert "503" in bads[0]["why"]


@pytest.mark.asyncio
async def test_质检未过必须删掉产物(tmp_path, monkeypatch):
    """留着会被下次重跑当成「已通过的缓存」复用，把带乱码的图挂上去。"""
    calls = {}
    _prep_env(tmp_path, monkeypatch, calls)

    async def bad_qc(path):
        return {"clean": False, "issues": "残留拼音"}

    monkeypatch.setattr(service.vision, "check_cleaned", bad_qc)
    url = "https://cdn/a.jpg"
    got = await service._prepare_desc_image(str(tmp_path), {"pos": 1, "url": url})
    _, en_path = service._desc_cache_paths(str(tmp_path), url)
    assert not got["ok"] and "质检未过" in got["why"]
    assert not os.path.exists(en_path), "质检未过的产物必须删掉"


@pytest.mark.asyncio
async def test_只缺像素的图走放大不烧生图(tmp_path, monkeypatch):
    """needsUpscale 的图内容本来就干净，重画一遍既贵又可能改坏内容。"""
    calls = {}
    _prep_env(tmp_path, monkeypatch, calls)
    # 2026-08-28 起 _prepare_desc_image 会传描述图下限（min_w/min_h，见
    # images.check_desc_size），假 compress 的签名要跟上，否则 TypeError → ok=False
    monkeypatch.setattr(service.images, "compress",
                        lambda p, quality=88, min_w=None, min_h=None: p)
    monkeypatch.setattr(service.images, "image_size", lambda p: (1340, 1785))
    got = await service._prepare_desc_image(
        str(tmp_path), {"pos": 1, "url": "https://cdn/a.jpg",
                        "needsUpscale": True, "reason": "尺寸不够"})
    assert got["ok"] and got["how"] == "upscaled"
    assert "edit" not in calls and "qc" not in calls, "放大路径不该生图、也不必质检"


# ---- 预热：命中就不重复调，未命中照原路 --------------------------------------

def _info_file(tmp_path, extra=None) -> str:
    info = {"title": "儿童秋季连衣裙", "attributes": {}, "skus": {},
            "imageUnderstanding": {}}
    info.update(extra or {})
    p = tmp_path / "product-info.json"
    p.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return str(p)


@pytest.mark.asyncio
async def test_预热命中时标题阶段不再调LLM(tmp_path, monkeypatch):
    """标题生成实测单次可达 100s+，命中预热必须直接填、不再问一遍。"""
    seen = {}

    async def fake_set_titles(session, info_path, generated=None):
        seen["generated"] = generated
        return {"status": "ok", "generated": generated or {}}

    async def never(*a, **kw):
        raise AssertionError("预热已命中，不该再调 generate_titles")

    patch_publish(monkeypatch, "service", "set_titles", fake_set_titles)
    patch_publish(monkeypatch, "service", "generate_titles", never)

    ctx = {"info_path": _info_file(tmp_path),
           "prewarm": {"titles": {"status": "ok",
                                  "generated": {"title": "中文标题",
                                                "enTitle": "EN Title"}}}}

    async def _emit(ev):
        return None

    r = await service._st_titles(ctx, object(), _emit)
    assert r["status"] == "ok"
    assert seen["generated"]["enTitle"] == "EN Title"


@pytest.mark.asyncio
async def test_预热未跑时标题阶段照原路现场生成(tmp_path, monkeypatch):
    """预热是纯增益：没有它时行为必须与改造前完全一致。"""
    seen = {}

    async def fake_set_titles(session, info_path, generated=None):
        seen["generated"] = generated
        return {"status": "ok", "generated": {"enTitle": "现场生成"}}

    patch_publish(monkeypatch, "service", "set_titles", fake_set_titles)
    ctx = {"info_path": _info_file(tmp_path)}          # 没有 prewarm

    async def _emit(ev):
        return None

    r = await service._st_titles(ctx, object(), _emit)
    assert r["status"] == "ok"
    assert seen["generated"] is None, "取不到预热就该传 None，让 set_titles 现场生成"


@pytest.mark.asyncio
async def test_预热结果只用一次(tmp_path, monkeypatch):
    """⑤~⑬ 的成果 save 前一重载就丢，同批次内可能重跑；重跑一律走现场那条路。"""
    calls = []

    async def fake_set_titles(session, info_path, generated=None):
        calls.append(generated)
        return {"status": "ok", "generated": {"enTitle": "x"}}

    patch_publish(monkeypatch, "service", "set_titles", fake_set_titles)
    ctx = {"info_path": _info_file(tmp_path),
           "prewarm": {"titles": {"status": "ok", "generated": {"enTitle": "预热"}}}}

    async def _emit(ev):
        return None

    await service._st_titles(ctx, object(), _emit)
    await service._st_titles(ctx, object(), _emit)
    assert calls[0] is not None and calls[1] is None, "第二次必须回到现场生成"


@pytest.mark.asyncio
async def test_预热生成失败时不当成命中(tmp_path, monkeypatch):
    """预热里生成失败（status != ok）时要现场重来，不能把失败结果当标题填进去。"""
    seen = {}

    async def fake_set_titles(session, info_path, generated=None):
        seen["generated"] = generated
        return {"status": "ok", "generated": {"enTitle": "x"}}

    patch_publish(monkeypatch, "service", "set_titles", fake_set_titles)
    ctx = {"info_path": _info_file(tmp_path),
           "prewarm": {"titles": {"status": "error",
                                  "reason": "title-generation-failed"}}}

    async def _emit(ev):
        return None

    await service._st_titles(ctx, object(), _emit)
    assert seen["generated"] is None


@pytest.mark.asyncio
async def test_清理预热命中时不重复清理(tmp_path, monkeypatch):
    """⑤b 是整段提前的（不是只提前判断），命中就直接复用结论，一次生图都不再发。"""
    async def never(ctx, emit):
        raise AssertionError("预热已清过，不该再清一遍")

    patch_publish(monkeypatch, "service", "_clean_main_images", never)
    ctx = {"info_path": _info_file(tmp_path),
           "prewarm": {"clean_images": {"status": "ok", "note": "清理 2/2 张"}}}

    async def _emit(ev):
        return None

    r = await service._st_clean_images(ctx, object(), _emit)
    assert r["status"] == "ok"
    assert "提前完成" in r["note"], "note 要看得出这一步是提前跑的"


@pytest.mark.asyncio
async def test_清理未预热时照原路现场清(tmp_path, monkeypatch):
    seen = {}

    async def fake_clean(ctx, emit):
        seen["ran"] = True
        return {"status": "ok", "note": "清理 1/1 张"}

    patch_publish(monkeypatch, "service", "_clean_main_images", fake_clean)
    ctx = {"info_path": _info_file(tmp_path)}

    async def _emit(ev):
        return None

    r = await service._st_clean_images(ctx, object(), _emit)
    assert seen.get("ran") and r["status"] == "ok"


@pytest.mark.asyncio
async def test_包装估算预热不满足所需字段时重算(tmp_path, monkeypatch):
    """预热按超集问，但万一它缺了本次要用的字段（如只给了尺寸没给重量），必须补问。

    守的是 set_variant 里那道 _check_pack_est 复核：直接信预热会把 KeyError 抛到
    阶段外，或把空重量填进申报字段。
    """
    from app.publish import pipeline

    asked = []

    async def fake_ask(prompt, what="判断", retries=3, stage=None, **kw):
        asked.append(what)
        return {"长": 30, "宽": 25, "高": 3, "重量": 420}

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask)
    info = {"title": "玩具积木", "packInfo": {}}
    # 缺「重量」的预热结果：need_weight=True 时 _check_pack_est 应判不合格
    bad = {"长": 30, "宽": 25, "高": 3}
    assert pipeline._check_pack_est(bad, need_dims=True, need_weight=True)
    est = await pipeline.estimate_pack(info, need_dims=True, need_weight=True)
    assert est["重量"] == 420 and asked, "缺字段就该现场问一次"


# ---- 描述图预热计划按 URL 重挂序号 -------------------------------------------
#
# 这是整个 desc 预热复用里唯一可能【删错图】的地方，故单独把守：预热的 pos 是
# raw.json 的源顺序，页面的 pos 由 desc_map 现数，两套序号只是常常相同、不保证相同。
# 本项目已经踩过一次这类错位（见 pipeline._JS_DESC_IDX_MAP 上方那次「删 pos 3/2
# 实际删掉 pos 2/1」），所以一律按 URL 对齐。

def test_重挂按URL对齐而非沿用源序号():
    """页面上少了前两张时，源 pos 3 的图在页面上是 pos 1——必须按 URL 认。"""
    pre = {"delete": [3], "deleteUrls": ["https://cdn/c.jpg"],
           "replace": [{"pos": 4, "url": "https://cdn/d.jpg", "reason": "含中文"}],
           "keep": [1, 2]}
    mods = [{"pos": 1, "url": "https://cdn/c.jpg"},
            {"pos": 2, "url": "https://cdn/d.jpg"}]
    out = service._replan_desc_by_url(pre, mods)
    assert out["delete"] == [1], "要删的那张在页面上是 pos 1，不是源 pos 3"
    assert [r["pos"] for r in out["replace"]] == [2]
    assert out["replace"][0]["reason"] == "含中文", "复用要连理由一起带过来"


def test_重挂时页面新增的图按保留处理():
    """预热没判过的图一律 keep——与 plan_desc 对漏判项的取向一致（不删不该删的）。"""
    pre = {"delete": [], "deleteUrls": [], "replace": [], "keep": [1]}
    mods = [{"pos": 1, "url": "https://cdn/a.jpg"},
            {"pos": 2, "url": "https://cdn/新来的.jpg"}]
    out = service._replan_desc_by_url(pre, mods)
    assert out["delete"] == [] and out["replace"] == []
    assert out["keep"] == [1, 2]


def test_重挂时尺寸按页面现测重算():
    """预热按本地文件算的尺寸只是估计，页面才是事实（图可能已被替换过）。"""
    pre = {"delete": [], "deleteUrls": [], "replace": [], "keep": [1]}
    mods = [{"pos": 1, "url": "https://cdn/a.jpg", "size": "900x1200",
             "tooSmall": True}]
    out = service._replan_desc_by_url(pre, mods)
    assert out["keep"] == []
    assert out["replace"][0]["needsUpscale"] is True, "keep 但不达标要改判放大"
    assert "900x1200" in out["replace"][0]["reason"]


def test_重挂时已判replace的不被尺寸覆盖成放大():
    """本来就要重出图的，收尾 compress 会把尺寸拉够，不该退化成纯放大（画面不清）。"""
    pre = {"delete": [], "deleteUrls": [],
           "replace": [{"pos": 1, "url": "https://cdn/a.jpg", "reason": "含水印"}],
           "keep": []}
    mods = [{"pos": 1, "url": "https://cdn/a.jpg", "size": "900x1200",
             "tooSmall": True}]
    out = service._replan_desc_by_url(pre, mods)
    assert len(out["replace"]) == 1
    assert not out["replace"][0].get("needsUpscale"), "含水印的图必须真英化，不能只放大"


def test_从raw读描述模块并标出尺寸不达标(tmp_path):
    """预热的 modules 由 raw.json + 本地 desc-NN.jpg 拼出，尺寸从文件读。"""
    from PIL import Image

    (tmp_path / "raw.json").write_text(
        json.dumps({"descImages": ["https://cdn/1.jpg", "https://cdn/2.jpg",
                                   "https://cdn/3.jpg", "https://cdn/4.jpg"]},
                   ensure_ascii=False), encoding="utf-8")
    # 【判据是描述图自己那套，不是服装 1340x1785】2026-08-28 用户截图取证后改：
    # 描述图只要「比例 0.5~2 且两边 >= 480」，见 images.check_desc_size。
    # 第 1 张 1400x1900 合格；第 2 张 900x1200 按服装红线是不达标，但按描述图口径
    # 【同样合格】（比例 0.75、两边都 >= 480）——原先正是这类图被误判后白烧生图。
    Image.new("RGB", (1400, 1900)).save(tmp_path / "desc-01.jpg")
    Image.new("RGB", (900, 1200)).save(tmp_path / "desc-02.jpg")
    # 真正不合格的：短边不足 480 / 比例超界
    Image.new("RGB", (300, 300)).save(tmp_path / "desc-03.jpg")
    Image.new("RGB", (1200, 400)).save(tmp_path / "desc-04.jpg")

    mods = service._desc_modules_from_raw(str(tmp_path))
    assert [m["pos"] for m in mods] == [1, 2, 3, 4]
    assert mods[0]["tooSmall"] is False and mods[1]["tooSmall"] is False
    assert mods[2]["tooSmall"] is True and mods[3]["tooSmall"] is True


def test_从raw读不到文件时不标尺寸不达标(tmp_path):
    """读不到尺寸按「未知」处理，不当成不达标——免得把好图误判成要放大。

    与 desc_map 的取向一致（naturalWidth 为 0 时按读不到处理）。
    """
    (tmp_path / "raw.json").write_text(
        json.dumps({"descImages": ["https://cdn/1.jpg"]}, ensure_ascii=False),
        encoding="utf-8")
    mods = service._desc_modules_from_raw(str(tmp_path))
    assert len(mods) == 1 and "tooSmall" not in mods[0]


def test_raw文件缺失时预热描述图安静跳过(tmp_path):
    """best-effort：raw.json 不在（老产物目录）时不抛，⑬ 照原路现场出计划。"""
    assert service._desc_modules_from_raw(str(tmp_path)) == []


# ---- Web 接口与 CLI 的接线 ---------------------------------------------------
#
# 记忆 publish-model-switch-checklist 记着一条教训：新增可选项要四处同步，漏登记会
# 让整个阶段静默挂掉。并发数这条链是「service 读写 → /publish/settings → 发布页
# 输入框 → CLI 参数」，故每一环都要有测试，不能只测最底层那个函数。


@pytest.fixture(scope="module")
def _webapp():
    """按文件路径加载 app.py：它与 app 包同名，普通 import 会被包遮蔽
    （同 tests/test_publish_web.py 与 test_orders_web 的做法）。"""
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("webapp_speedup", str(root / "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_speedup"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def _client(_webapp):
    from fastapi.testclient import TestClient
    return TestClient(_webapp.app), _webapp


def test_settings接口返回当前并发与上限(_client, monkeypatch, tmp_path):
    client, webapp = _client
    patch_publish(monkeypatch, "service", "PREFS_PATH",
                        str(tmp_path / "p.json"))
    d = client.get("/publish/settings").json()
    assert d["imageConcurrency"] == 30
    assert d["imageConcurrencyMax"] == service.IMAGE_CONCURRENCY_MAX
    assert d["imageConcurrencyDefault"] == 30


def test_settings接口可保存并读回(_client, monkeypatch, tmp_path):
    client, webapp = _client
    patch_publish(monkeypatch, "service", "PREFS_PATH",
                        str(tmp_path / "p.json"))
    r = client.post("/publish/settings", json={"imageConcurrency": 16})
    assert r.status_code == 200 and r.json()["imageConcurrency"] == 16
    assert client.get("/publish/settings").json()["imageConcurrency"] == 16


def test_settings接口拒越界值(_client, monkeypatch, tmp_path):
    """越界是配置错误不是服务端故障，必须 400 而不是 500。"""
    client, webapp = _client
    patch_publish(monkeypatch, "service", "PREFS_PATH",
                        str(tmp_path / "p.json"))
    for bad in (0, -1, service.IMAGE_CONCURRENCY_MAX + 1):
        r = client.post("/publish/settings", json={"imageConcurrency": bad})
        assert r.status_code == 400, f"{bad} 应被拒"


def test_发布页有并发输入框且带默认占位(_client):
    """模板里少一个 id，前端就是静默不工作（同 test_publish_web 的取向）。"""
    client, _ = _client
    t = client.get("/publish").text
    assert 'id="inputImgConc"' in t
    assert 'placeholder="30"' in t, "占位符要显示默认值"
    assert '/publish/settings' in t, "前端要真的调接口，不能只画个框"
    assert 'loadImgConc()' in t, "初始化要回填后端现值"


def test_CLI有并发参数():
    """--image-concurrency 要能解析，且非法值由 argparse/set 拦住。"""
    import publish_run
    assert hasattr(publish_run, "set_image_concurrency"), "CLI 必须导入设置函数"


# ---- SKC 按批挂图（2026-08-26 真站探查证实弹窗支持累积多选）------------------
#
# 探查结论（workspace/_probe_skc_space_modal.py 对 rowid 173539495455603009 实测）：
#   - 计数逐次累加：「已选择0张图片」→ 1 → 2 → 3
#   - 每个 .img-item 的 class 恒为 "img-item" 不变，变的是内层 .img-check 的文本
#     （「点击选择」↔「取消选择」），且点第 2 张时第 1 张【保持】选中
#   - 菜单真实就绪 0ms（现行硬等 1600ms）、弹窗连图列表 106ms（现行硬等 3000ms）


def test_选中态判据必须兼容取消选中():
    """【差一个字排查了五次真站验证】平台文案是「取消选中」，不是「取消选择」。

    2026-08-26：正则写 /取消选择/ 时每张都报「点了但没翻转」、整行换不了图，而页面上
    其实已经选上了。更坑的是终端 GBK 把两个词的乱码显示得一模一样，五次验证里读到的
    「取消选择」全是乱码巧合——读诊断输出必须 PYTHONIOENCODING=utf-8。
    故正则兼容两种写法，这条测试防的是有人「顺手改回」单一文案。
    """
    from app.publish import pipeline as pl

    for name in ("_JS_SPACE_CLICK_ONE", "_JS_SPACE_CHECK_ONE"):
        js = getattr(pl, name)
        assert "取消选[择中]" in js, f"{name} 的选中态判据要兼容「取消选中」"
        assert "img-check" in js, f"{name} 要看 .img-check 的文本"


def test_选中态不按class判():
    """.img-item 的 className 恒定不变（真站探查确认），按 class 判会恒判未选中。"""
    from app.publish import pipeline as pl

    for name in ("_JS_SPACE_CLICK_ONE", "_JS_SPACE_CHECK_ONE"):
        js = getattr(pl, name)
        assert "img-item-selected" not in js
        assert "classList.contains" not in js


def test_点前先查已选态():
    """已经是选中态的不能再点——弹窗是 toggle 语义，再点会取消掉。"""
    from app.publish import pipeline as pl

    js = pl._JS_SPACE_CLICK_ONE
    i_check = js.find("before.some(t => /取消选[择中]/.test(t))")
    i_click = js.find(".click()")
    assert i_check > 0, "点击前必须先判一次已选态"
    assert i_check < i_click, "已选态检查必须在点击之前"


def test_按全部匹配项判选中():
    """同一内容的图在空间里可能有多份记录，只看第一个会导致「点了 A 去读 B」。"""
    from app.publish import pipeline as pl

    assert "items.filter(" in pl._JS_SPACE_CLICK_ONE, "要取全部匹配项而不是 find 第一个"
    assert "checks.some(" in pl._JS_SPACE_CHECK_ONE, "选中判据看全部匹配项里是否有任一选中"


def test_点击与确认分成两次往返():
    """连点版在真站四次失败，改成每张一次独立 eval（全新上下文）才走通。

    别合回一段 JS：那正是踩过的坑（详见 _JS_SPACE_CLICK_ONE 上方注释）。
    """
    from app.publish import pipeline as pl

    assert hasattr(pl, "_JS_SPACE_CLICK_ONE"), "点击要独立成一段"
    assert hasattr(pl, "_JS_SPACE_CHECK_ONE"), "状态确认要独立成一段"
    assert hasattr(pl, "_JS_SPACE_CONFIRM"), "点确定要独立成一段"
    src = inspect.getsource(pl._pick_many_from_space)
    assert "_JS_SPACE_CLICK_ONE" in src and "_JS_SPACE_CHECK_ONE" in src


def test_计数不符时不点确定():
    """半选状态点确定会挂上数量不对的图，比直接失败糟。"""
    from app.publish import pipeline as pl

    src = inspect.getsource(pl._pick_many_from_space)
    i_count = src.find("已选计数与预期不符")
    i_confirm = src.find("_JS_SPACE_CONFIRM")
    assert i_count > 0 and i_confirm > 0
    assert i_count < i_confirm, "计数校验必须在点确定之前 return 掉"


def test_计数元素缺失时跳过该校验而非报错():
    """不同弹窗实例不一定都有「已选择N张图片」（真站实测 SKC 行那个就取不到）。"""
    from app.publish import pipeline as pl

    src = inspect.getsource(pl._pick_many_from_space)
    assert "cnt is not None" in src, "计数取不到时要跳过校验，不能当成失败"


def test_轮询取代固定等待的三处都改了():
    """三处硬等（等弹窗 3s / 等删除 1.2s / 等菜单 1.6s）都必须是轮询。

    实测账（logs/20260826110756.log，6 行 29 张 402s）：每张 13.9s 里约 11s 是这些
    固定 sleep，真实网络只占 1.1s。这条测试防的是后人「为了稳」把 sleep 加回去。
    """
    from app.publish import pipeline as pl

    assert "await sleep(3000)" not in pl._JS_SKC_CLICK_SPACE, "等弹窗不该再硬等 3s"
    assert "await sleep(1200)" not in pl._JS_SKC_DEL_FIRST, "等删除不该再硬等 1.2s"
    assert "await sleep(1800)" not in pl._JS_PICK_FROM_SPACE, "等弹窗关不该再硬等 1.8s"
    # 等菜单那处改成了独立的轮询函数
    assert hasattr(pl, "_wait_skc_menu")
    assert hasattr(pl, "_wait_scroll_settled")


def test_等菜单JS仍按应用到所有颜色判据():
    """这是区分 SKC 菜单与素材图菜单的唯一可靠特征，认错会把图挂到素材图上。

    见 pipeline 阶段⑦ 开头记录的那次真实事故。改轮询不能顺手放宽这个判据。
    """
    from app.publish import pipeline as pl

    assert "__EXTRA__" in pl._JS_WAIT_SKC_MENU
    assert "空间图片" in pl._JS_WAIT_SKC_MENU


def test_滚动停稳判据是位置不再变():
    """固定时长既可能不够（长页面）又通常过头；位置稳定才是真实判据。"""
    from app.publish import pipeline as pl

    js = pl._JS_SCROLL_SETTLED
    assert "getBoundingClientRect().top" in js
    assert "same >= 2" in js, "要连续两次相同才算停稳，单次可能撞上匀速平台期"
