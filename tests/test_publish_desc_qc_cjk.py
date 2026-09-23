"""残留中文的质检重试与质检口径：离线单测。

钉的是 2026-08-26 那一批（925861971282）描述长图的三类真实失败：
  - 第 2 张「残留中文」→ 退回原图，于是描述区留着 1688 外链、还撞 1340×1785 闸门。
    中文是 Temu 最硬的红线，而生图有随机性，必须多烧几发保证合格。
  - 第 5 张「衣服上有品牌logo及疑似乱码英文字符」→ 那是衣服实物的绣标与装饰字母，
    人工选品时已确认过，不该判失败（误报的代价同样是退回原图）。
  - 描述保存后「仍有外链图未转存」→ 不是独立 bug，就是上面两条退回原图的后果。

不覆盖：真实 gpt-image-2 出图与真实视觉端点（要 key 和网络）。这里只钉判据、
重试发数、以及重试时提示词有没有真的加码。
"""
import json
import os

import pytest

from app.publish import service, vision


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [
    {"issues": "Cute 属情绪夸大类"},
    {"issues": "Super Cute!属情绪夸大"},
    {"issues": "Cute 属情绪夸大类", "marketingClaimTexts": []},
    {"issues": "普通审美形容词", "marketingClaimTexts": ["Cute", "Super Cute!", "Lovely"]},
])
async def test_style_only_model_rejection_cannot_block(monkeypatch, evidence):
    async def fake_model(*args, **kwargs):
        return {"residualChinese": False, "garbled": False, "watermark": False,
                "brokenSubject": False, "marketingClaim": True, **evidence}

    monkeypatch.setattr(vision, "ask_json_with_images", fake_model)
    result = await vision.check_cleaned("cute.jpg")
    assert result["clean"] is True
    assert result["marketingClaim"] is False
    assert result["issues"] == ""
    assert result["nonBlockingIssues"]


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["residualChinese", "garbled", "watermark", "brokenSubject"])
async def test_style_override_preserves_other_image_failures(monkeypatch, problem):
    async def fake_model(*args, **kwargs):
        return {"residualChinese": False, "garbled": False, "watermark": False,
                "brokenSubject": False, "marketingClaim": True,
                "issues": "Cute 属情绪夸大类", problem: True}

    monkeypatch.setattr(vision, "ask_json_with_images", fake_model)
    result = await vision.check_cleaned("mixed.jpg")
    assert result["clean"] is False
    assert result["marketingClaim"] is False
    assert result["issues"] and "Cute" not in result["issues"]


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [
    {"issues": "Cute与Best Seller营销宣称"},
    {"issues": "Cute 属情绪夸大类；High-Quality品质夸大"},
    {"issues": "Cute及销量宣称", "marketingClaimTexts": ["Cute", "Best Seller"]},
    {"issues": "Guaranteed承诺", "marketingClaimTexts": ["Cute"]},
    {"issues": "Cute 属情绪夸大类", "marketingClaimTexts": "Cute"},
    {"issues": "未明确识别文案", "marketingClaimTexts": []},
])
async def test_mixed_or_unknown_claims_are_not_style_override(monkeypatch, evidence):
    async def fake_model(*args, **kwargs):
        return {"residualChinese": False, "garbled": False, "watermark": False,
                "brokenSubject": False, "marketingClaim": True, **evidence}

    monkeypatch.setattr(vision, "ask_json_with_images", fake_model)
    result = await vision.check_cleaned("mixed.jpg")
    assert result["clean"] is False
    assert result["marketingClaim"] is True
    assert not result["nonBlockingIssues"]


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", [None, "marketingClaim", "watermark", "residualChinese", "garbled"])
async def test_style_boundary_shared_by_generation_and_qc_without_blanket_bypass(monkeypatch, problem):
    from app.publish import claims, images

    async def fake_model(prompt, *args, **kwargs):
        assert claims._IMAGE_STYLE_BOUNDARY in prompt
        assert "实际可见的完整宣称" in prompt
        response = {field: field == problem for field in (
            "residualChinese", "garbled", "watermark", "brokenSubject", "marketingClaim")}
        response["issues"] = "Cute旁还有Best Seller" if problem == "marketingClaim" else ""
        return response

    assert claims._IMAGE_STYLE_BOUNDARY in images.DEFAULT_TRANSLATE_PROMPT
    assert claims._IMAGE_STYLE_BOUNDARY in images.SIZECHART_TRANSLATE_PROMPT
    monkeypatch.setattr(vision, "ask_json_with_images", fake_model)

    result = await vision.check_cleaned("cute-glider.jpg")

    assert result["clean"] is (problem is None)
    if problem:
        assert result[problem] is True


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """质检重试路径本身不 sleep，但 _prepare_desc_image 会过 to_thread，直接放行。"""
    async def _run(fn, *a, **kw):
        return fn(*a, **kw)
    monkeypatch.setattr(service.asyncio, "to_thread", _run)


# ---- check_cleaned 的判据 -------------------------------------------------------

@pytest.mark.asyncio
async def test_衣服实物logo和图案不算质检失败(monkeypatch):
    """选品阶段已人工过滤，实物绣标不是「待清理的文字层」，误报会让图退回原图。"""
    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        # 提示词必须明确把实物印花/刺绣排除在外，否则模型照旧误报
        assert "刺绣" in prompt and "印花" in prompt
        assert "不算问题" in prompt
        return {"residualChinese": False, "garbled": False,
                "brokenSubject": False, "watermark": False,
                "marketingClaim": False, "issues": ""}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    r = await vision.check_cleaned("x.jpg")
    assert r["clean"] is True and r["residualChinese"] is False


@pytest.mark.asyncio
async def test_残留中文单独回一个字段(monkeypatch):
    """residualChinese 决定重试发数，不能只混在 issues 文字里。"""
    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        return {"residualChinese": True, "garbled": False,
                "brokenSubject": False, "watermark": False,
                "marketingClaim": False, "issues": "底部仍有中文说明"}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    r = await vision.check_cleaned("x.jpg")
    assert r["clean"] is False and r["residualChinese"] is True


@pytest.mark.asyncio
async def test_乱码要透出garbled字段(monkeypatch):
    """【回归钉子】garbled 原先只参与算 clean、没进返回值，于是 service 侧的
    `qc.get("garbled")` 恒为 None，给乱码加长重试形同虚设——而 ⑤b main-04 两发
    恰好全是 garbled，正是要救的那一类。
    """
    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        return {"residualChinese": False, "garbled": True,
                "brokenSubject": False, "watermark": False,
                "marketingClaim": False, "issues": "拼音残留"}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    r = await vision.check_cleaned("x.jpg")
    assert r["clean"] is False
    assert r["garbled"] is True          # 必须透出去，否则重试发数抬不起来
    assert r["residualChinese"] is False  # 不是中文那类，话术要说反过来的那套


@pytest.mark.asyncio
async def test_破坏主体算不过但两个文字字段都是False(monkeypatch):
    """brokenSubject 只给 2 发，故它不能让任何文字类字段变 True。"""
    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        return {"residualChinese": False, "garbled": False,
                "brokenSubject": True, "watermark": False,
                "marketingClaim": False, "issues": "袖子被抹掉"}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    r = await vision.check_cleaned("x.jpg")
    assert r["clean"] is False
    assert r["garbled"] is False and r["residualChinese"] is False


@pytest.mark.asyncio
async def test_模型只回旧的clean字段时不误判为干净(monkeypatch):
    """提示词换过多版，模型偶尔按老约定只回 clean；缺三个新字段时不能读成干净。"""
    async def _fake(prompt, images, what="", system=None, stage=None, **kw):
        return {"clean": False, "issues": "残留中文"}
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    r = await vision.check_cleaned("x.jpg")
    assert r["clean"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("issues", ["底部含1688店铺水印链接", ""])
async def test_英文网址水印独立拦截(monkeypatch, issues):
    async def fake_model(prompt, images, **kwargs):
        assert "纯英文、数字" in prompt and "半透明" in prompt
        return {"residualChinese": False, "garbled": False,
                "brokenSubject": False, "watermark": True,
                "marketingClaim": False, "clean": True, "issues": issues}

    monkeypatch.setattr(vision, "ask_json_with_images", fake_model)
    result = await vision.check_cleaned("x.jpg")
    assert result["status"] == "ok"
    assert result["clean"] is False
    assert result["watermark"] is True
    assert result["residualChinese"] is False and result["garbled"] is False
    assert "水印" in result["issues"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"residualChinese": False, "garbled": False, "brokenSubject": False,
     "marketingClaim": False, "issues": "底部含1688店铺水印链接"},
    {"clean": True},
    *[{"residualChinese": False, "garbled": False, "brokenSubject": False,
       "marketingClaim": False, "watermark": value}
      for value in (None, "false", "true", 0, 1)],
])
async def test_水印未明确检查不能放行(monkeypatch, response):
    async def fake_model(*args, **kwargs):
        return response

    monkeypatch.setattr(vision, "ask_json_with_images", fake_model)
    result = await vision.check_cleaned("x.jpg")
    assert result["status"] == "error"
    assert result["clean"] is False


# ---- 描述图英化的重试发数 -------------------------------------------------------

def _desc_stub(tmp_path, monkeypatch, qc_results, calls):
    """把下载/生图/质检换成假的，记录每次生图收到的提示词。"""
    def fake_download(url, dst, retries=3):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(b"x")
        return 1

    def fake_edit(path, prompt=None, out_path=None, **kw):
        calls.setdefault("prompts", []).append(prompt)
        with open(out_path, "wb") as f:
            f.write(b"edited")
        return {"status": "ok", "output": out_path}

    seq = list(qc_results)

    async def fake_qc(p):
        calls["qc"] = calls.get("qc", 0) + 1
        return seq.pop(0) if seq else {"clean": False, "residualChinese": False,
                                       "issues": "还是不行"}

    monkeypatch.setattr(service.extract, "_download_image", fake_download)
    monkeypatch.setattr(service.images, "edit_image", fake_edit)
    monkeypatch.setattr(service.vision, "check_cleaned", fake_qc)


def _desc_stub_real_qc(tmp_path, monkeypatch, model_replies, calls):
    """同 _desc_stub，但质检走【真实 check_cleaned】、只 mock 模型返回。

    【为什么要有这一版】_desc_stub 直接伪造 qc 结果，绕开了 check_cleaned 本身——
    `garbled` 漏出返回值那个 bug 就是这样躲过测试的（伪造的 dict 里有这个键，真实
    函数却不返回）。凡是断言「发数」的用例都该走这一版。
    """
    def fake_download(url, dst, retries=3):
        import os as _os
        _os.makedirs(_os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(b"x")
        return 1

    def fake_edit(path, prompt=None, out_path=None, **kw):
        calls.setdefault("prompts", []).append(prompt)
        with open(out_path, "wb") as f:
            f.write(b"edited")
        return {"status": "ok", "output": out_path}

    seq = list(model_replies)

    async def fake_model(prompt, images, what="", system=None, stage=None, **kw):
        # 【重烧提示词生成要走另一条分支】它同样是 ask_json_with_images 的调用方
        # （vision.build_retry_hint），不区分就会把下面这个质检序列吃掉、让质检提前
        # 判过，发数断言随之失真。返回空 blocks 表示「没生成出可用的处置块」，
        # 调用方按 best-effort 退回固定加码话术——正是这些用例要钉的那条路径。
        if what == "重烧提示词生成":
            return {"blocks": [], "summary": ""}
        calls["qc"] = calls.get("qc", 0) + 1
        return seq.pop(0) if seq else {"residualChinese": False, "garbled": True,
                                       "brokenSubject": False, "watermark": False,
                                       "marketingClaim": False, "issues": "还是不行"}

    monkeypatch.setattr(service.extract, "_download_image", fake_download)
    monkeypatch.setattr(service.images, "edit_image", fake_edit)
    monkeypatch.setattr(vision, "ask_json_with_images", fake_model)


@pytest.mark.asyncio
async def test_残留中文给到四发而不是两发(tmp_path, monkeypatch):
    """第 4 发才干净：2 发就放弃会让这张图带着中文退回原图（线上真实后果）。"""
    calls = {}
    bad = {"clean": False, "residualChinese": True, "issues": "底部仍有中文"}
    _desc_stub(tmp_path, monkeypatch, [bad, bad, bad,
                                       {"clean": True, "residualChinese": False}], calls)

    got = await service._prepare_desc_image(
        str(tmp_path), {"pos": 1, "url": "https://cdn/a.jpg"})

    assert got["ok"] is True and got["how"] == "edited"
    assert calls["qc"] == 4 == service.DESC_QC_TRIES_TEXT
    assert len(calls["prompts"]) == 4


@pytest.mark.asyncio
async def test_残留中文重试时提示词要加码(tmp_path, monkeypatch):
    """原样重发只是赌随机性；把上一发残留了什么当新约束喂回去。"""
    calls = {}
    _desc_stub(tmp_path, monkeypatch,
               [{"clean": False, "residualChinese": True, "issues": "袖口有中文"},
                {"clean": True, "residualChinese": False}], calls)

    got = await service._prepare_desc_image(
        str(tmp_path), {"pos": 1, "url": "https://cdn/a.jpg"})

    assert got["ok"] is True
    first, second = calls["prompts"][0], calls["prompts"][1]
    # 第一发用默认提示词（不带上一轮的残留信息）
    assert first is None or "仍不合格" not in first
    assert "仍不合格" in second and "袖口有中文" in second
    # 中文那一类的要点是「逐块扫、一个字符都不留」
    assert "中文标点" in second


@pytest.mark.asyncio
async def test_非中文问题仍只给两发(tmp_path, monkeypatch):
    """乱码/修图痕迹重烧是同样结果，多烧纯烧钱——gpt-image-2 每发都付费。"""
    calls = {}
    bad = {"clean": False, "residualChinese": False, "issues": "主体被改坏"}
    _desc_stub(tmp_path, monkeypatch, [bad, bad, bad, bad], calls)

    got = await service._prepare_desc_image(
        str(tmp_path), {"pos": 1, "url": "https://cdn/a.jpg"})

    assert got["ok"] is False
    assert calls["qc"] == 2 == service.DESC_QC_TRIES


@pytest.mark.asyncio
async def test_四发全没过时产物不留缓存(tmp_path, monkeypatch):
    """质检未过的图留在盘上会被下次重跑当成「已通过的缓存」复用。"""
    calls = {}
    bad = {"clean": False, "residualChinese": True, "issues": "还有中文"}
    _desc_stub(tmp_path, monkeypatch, [bad] * 4, calls)

    url = "https://cdn/a.jpg"
    got = await service._prepare_desc_image(str(tmp_path), {"pos": 1, "url": url})

    assert got["ok"] is False and got["residualChinese"] is True
    _, en = service._desc_cache_paths(str(tmp_path), url)
    assert not os.path.exists(en), "质检未过的产物必须删掉，不能留成缓存"


# ---- ⑤b 主图清理走同一套加长重试 -------------------------------------------------

@pytest.mark.asyncio
async def test_主图清理的中文残留也给到四发(tmp_path, monkeypatch):
    """⑤b 与 ⑬ 是同一个红线、同一个随机性，发数口径必须一致。"""
    img = tmp_path / "main-01.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    info_path = str(tmp_path / "product-info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump({"complianceNotes": {"files": [
            {"file": "main-01.jpg", "clean": False, "chinese": True}]}}, f)

    calls = {}

    def fake_edit(path, prompt=None, out_path=None, **kw):
        calls.setdefault("prompts", []).append(prompt)
        with open(out_path, "wb") as f:
            f.write(b"edited")
        return {"status": "ok", "output": out_path}

    seq = [{"clean": False, "residualChinese": True, "issues": "领标有中文"}] * 3 \
        + [{"clean": True, "residualChinese": False}]

    async def fake_qc(p):
        calls["qc"] = calls.get("qc", 0) + 1
        return seq.pop(0)

    monkeypatch.setattr(service.images, "edit_image", fake_edit)
    monkeypatch.setattr(service.vision, "check_cleaned", fake_qc)

    async def _emit(ev):
        calls.setdefault("events", []).append(ev)

    r = await service._clean_main_images(
        {"info_path": info_path, "workdir": str(tmp_path)}, _emit)

    assert r["status"] == "ok" and "清理 1/1" in r["note"]
    assert calls["qc"] == 4
    # 重试时同样要把上一轮的残留喂回去
    assert "仍不合格" in calls["prompts"][1]
    assert "领标有中文" in calls["prompts"][1]


# ---- 生图乱码（⑤b main-04 那类）也要多发 -----------------------------------------

@pytest.mark.asyncio
async def test_生图乱码也给到四发(tmp_path, monkeypatch):
    """2026-08-26 ⑤b main-04 两发都是 garbled（「AI英化后英文为无意义拼写」→
    「疑似乱码/无意义文字」），2 发用完就放弃，那张主图于是以脏图身份参与 ⑥⑦ 选图。
    乱码是生图自己吐的、随机性最强的一类，正该多烧。
    """
    calls = {}
    bad = {"clean": False, "residualChinese": False, "garbled": True,
           "issues": "英文为无意义拼写"}
    _desc_stub(tmp_path, monkeypatch, [bad, bad, bad,
                                       {"clean": True}], calls)

    got = await service._prepare_desc_image(
        str(tmp_path), {"pos": 1, "url": "https://cdn/a.jpg"})

    assert got["ok"] is True
    assert calls["qc"] == 4 == service.DESC_QC_TRIES_TEXT


@pytest.mark.asyncio
async def test_乱码的重试提示词要叫模型别编英文(tmp_path, monkeypatch):
    """对乱码说「请翻译干净」只会让它再编一串新的无意义字母，要点是「认不出就删掉」。"""
    calls = {}
    _desc_stub(tmp_path, monkeypatch,
               [{"clean": False, "residualChinese": False, "garbled": True,
                 "issues": "oOaLanTanAt"},
                {"clean": True}], calls)

    await service._prepare_desc_image(str(tmp_path), {"pos": 1, "url": "https://cdn/a.jpg"})

    second = calls["prompts"][1]
    # 只能对【追加的那一截】断言：「中文标点」在基础提示词里本来就有
    hint = second[second.index("【上一次"):]
    assert "不要凭猜测编造" in hint and "移除" in hint
    # 不该给乱码发中文那一套要点（两类失败要说不同的话）
    assert "一个中文字符" not in hint


@pytest.mark.asyncio
async def test_破坏主体只给两发(tmp_path, monkeypatch):
    """改坏主体时多烧只会得到另一张坏图，且宁可退回原图也不要主体被改烂的图上真店。"""
    calls = {}
    bad = {"clean": False, "residualChinese": False, "garbled": False,
           "brokenSubject": True, "issues": "袖子被抹掉"}
    _desc_stub(tmp_path, monkeypatch, [bad] * 4, calls)

    got = await service._prepare_desc_image(
        str(tmp_path), {"pos": 1, "url": "https://cdn/a.jpg"})

    assert got["ok"] is False
    assert calls["qc"] == 2 == service.DESC_QC_TRIES


@pytest.mark.asyncio
async def test_端到端_乱码走真实质检也给到四发(tmp_path, monkeypatch):
    """【最关键的一条】不伪造 qc 结果，只 mock 模型返回，走真实 check_cleaned。

    上一版测试用伪造的 qc dict，里面凭空带着 `garbled` 键，于是 check_cleaned
    漏传该字段的 bug 完全测不出来——加长重试对乱码那一路实际是失效的。
    """
    calls = {}
    bad = {"residualChinese": False, "garbled": True, "brokenSubject": False,
           "watermark": False, "marketingClaim": False, "issues": "英文为无意义拼写"}
    ok = {"residualChinese": False, "garbled": False, "brokenSubject": False,
          "watermark": False, "marketingClaim": False, "issues": ""}
    _desc_stub_real_qc(tmp_path, monkeypatch, [bad, bad, bad, ok], calls)

    got = await service._prepare_desc_image(
        str(tmp_path), {"pos": 1, "url": "https://cdn/a.jpg"})

    assert got["ok"] is True
    assert calls["qc"] == 4 == service.DESC_QC_TRIES_TEXT


@pytest.mark.asyncio
async def test_端到端_破坏主体走真实质检仍只两发(tmp_path, monkeypatch):
    calls = {}
    bad = {"residualChinese": False, "garbled": False, "brokenSubject": True,
           "watermark": False, "marketingClaim": False, "issues": "袖子被抹掉"}
    _desc_stub_real_qc(tmp_path, monkeypatch, [bad] * 4, calls)

    got = await service._prepare_desc_image(
        str(tmp_path), {"pos": 1, "url": "https://cdn/a.jpg"})

    assert got["ok"] is False
    assert calls["qc"] == 2 == service.DESC_QC_TRIES


@pytest.mark.asyncio
async def test_端到端_水印未去除不能复用产物(tmp_path, monkeypatch):
    calls = {}
    bad = {"residualChinese": False, "garbled": False, "brokenSubject": False,
           "watermark": True, "marketingClaim": False, "issues": "底部含1688店铺水印链接"}
    _desc_stub_real_qc(tmp_path, monkeypatch, [bad] * service.DESC_QC_TRIES, calls)

    result = await service._prepare_desc_image(
        str(tmp_path), {"pos": 1, "url": "https://cdn/a.jpg"})

    assert result["ok"] is False
    assert calls["qc"] == service.DESC_QC_TRIES
    assert "水印" in calls["prompts"][1]
    _, edited_path = service._desc_cache_paths(str(tmp_path), "https://cdn/a.jpg")
    assert not os.path.exists(edited_path)


# ---- ⑤b 与 ⑬ 共用同一套发数/话术（回归钉子）--------------------------------------

@pytest.mark.asyncio
async def test_5b乱码走真实质检也给到四发(tmp_path, monkeypatch):
    """⑤b 走的是 _clean_main_images._one，与 ⑬ 是两段独立循环——发数判据必须一致。

    这条同样 mock 到 ask_json_with_images 那一层（不伪造 qc dict），理由见
    _desc_stub_real_qc：`garbled` 漏出返回值那个 bug 就是被浅 mock 掩盖的。
    """
    import json as _json

    src = tmp_path / "main-01.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    info_path = tmp_path / "product-info.json"
    info_path.write_text(_json.dumps({"complianceNotes": {"files": [
        {"file": "main-01.jpg", "clean": False, "chinese": True}]}}),
        encoding="utf-8")

    calls = {}

    def fake_edit(path, prompt=None, out_path=None, **kw):
        calls.setdefault("prompts", []).append(prompt)
        with open(out_path, "wb") as f:
            f.write(b"edited")
        return {"status": "ok", "output": out_path}

    bad = {"residualChinese": False, "garbled": True, "brokenSubject": False,
           "watermark": False, "marketingClaim": False, "issues": "英文为无意义拼写"}
    ok = {"residualChinese": False, "garbled": False, "brokenSubject": False,
          "watermark": False, "marketingClaim": False, "issues": ""}
    seq = [bad, bad, bad, ok]

    async def fake_model(prompt, images, what="", system=None, stage=None, **kw):
        # 重烧提示词生成与质检共用这一层，按 what 分流（理由见 _desc_stub_real_qc）：
        # 这里给空 blocks，让这一路退回固定加码话术，下面那条「别编英文」的断言才成立。
        if what == "重烧提示词生成":
            return {"blocks": [], "summary": ""}
        calls["qc"] = calls.get("qc", 0) + 1
        return seq.pop(0) if seq else ok

    monkeypatch.setattr(service.images, "edit_image", fake_edit)
    monkeypatch.setattr(vision, "ask_json_with_images", fake_model)

    events = []

    async def _emit(e):
        events.append(e)

    r = await service._clean_main_images(
        {"info_path": str(info_path), "workdir": str(tmp_path)}, _emit)

    assert r["status"] == "ok" and "清理 1/1" in r["note"]
    assert calls["qc"] == 4 == service.DESC_QC_TRIES_TEXT
    # 乱码那类的话术要说「别编英文」，不是中文那套
    hint = calls["prompts"][1][calls["prompts"][1].index("【上一次"):]
    assert "不要凭猜测编造" in hint


@pytest.mark.asyncio
async def test_5b质检未过绝不顶替原图(tmp_path, monkeypatch):
    """【风险方向的钉子】⑤b 失败时不删产物（与 ⑬ 不同，那边 en_path 是缓存键必须删），
    安全性靠「失败的 file 在 copy 之前就 continue 掉」。这条钉住那个 continue：
    一旦有人重排这段循环，带乱码的图就会覆盖原图、以「干净」身份进 ⑥⑦ 选图。
    """
    import json as _json

    src = tmp_path / "main-01.jpg"
    original = b"\xff\xd8\xff\xe0jpeg"
    src.write_bytes(original)
    info_path = tmp_path / "product-info.json"
    info_path.write_text(_json.dumps({"complianceNotes": {"files": [
        {"file": "main-01.jpg", "clean": False, "chinese": True}]}}),
        encoding="utf-8")

    def fake_edit(path, prompt=None, out_path=None, **kw):
        with open(out_path, "wb") as f:
            f.write(b"garbled-output")
        return {"status": "ok", "output": out_path}

    async def fake_model(prompt, images, what="", system=None, stage=None, **kw):
        return {"residualChinese": False, "garbled": True, "brokenSubject": False,
                "watermark": False, "issues": "oOaLanTanAt"}

    monkeypatch.setattr(service.images, "edit_image", fake_edit)
    monkeypatch.setattr(vision, "ask_json_with_images", fake_model)

    events = []

    async def _emit(e):
        events.append(e)

    r = await service._clean_main_images(
        {"info_path": str(info_path), "workdir": str(tmp_path)}, _emit)

    # 【2026-09-22 起：质检多次未过的图直接丢弃】原先判 fail、停在现场等人工换图，
    # 代价是一张图拖停整单（offer 652503071023 的 desc-01 就是这么卡住的）；现在阶段
    # 照常 ok、该图标 unusable 硬排除出选图。本条测试的钉子不变：
    # 无论判 ok 还是 fail，失败的产物都绝不能顶替原图。
    assert r["status"] == "ok" and "已丢弃" in r["note"]
    assert src.read_bytes() == original, "质检未过的产物绝不能顶替原图"
    got = _json.loads(info_path.read_text(encoding="utf-8"))
    entry = got["complianceNotes"]["files"][0]
    # 丢弃 = 硬排除：clean 仍如实为 False（它确实带中文），但 unusable 让所有选图路径
    # 跳过它（见 vision.is_unusable）——原先靠「以脏图身份参与兜底打分」是软排除，
    # 图不够下限时还会被放回页面。
    assert entry["clean"] is False
    assert entry["unusable"] is True and entry["unusableReason"] == "qc_failed"
    # 文案要说「已丢弃、不用处理」，不能再说「请人工换图」——后者会让人去处理一张
    # 已经处理完（被丢弃）的图
    mc = [e for e in events if e["type"] == "manual_check"]
    assert mc and "已丢弃" in mc[0]["message"] and "不用处理" in mc[0]["message"]
    assert "请人工换图" not in mc[0]["message"]


# ---- ⑤b 内容审核拒收：标不可用绕开，不判 fail、不无限重跑 --------------------
# 2026-09-18 1051951604789 实况：main-03.jpg 被出图服务审核硬拒（见
# images.ModerationBlocked 的取证），原先落进 fail_files 判整阶段 fail，而这张图每次
# 重跑都被「轮播图中文复核」补充循环重新送进来，商品永久卡在 ⑤b——那单另有 10 张干净
# 图、plan_clean 本身早判了「无需清理」。

def _blocked_stub(tmp_path, monkeypatch, files: list) -> str:
    """造一个待清理的 workdir + product-info.json，edit_image 一律抛审核拒绝。"""
    for e in files:
        (tmp_path / e["file"]).write_bytes(b"\xff\xd8\xff\xe0jpeg")
    info_path = tmp_path / "product-info.json"
    info_path.write_text(json.dumps({"complianceNotes": {"files": files}}),
                         encoding="utf-8")

    def fake_edit(path, prompt=None, out_path=None, **kw):
        raise service.images.ModerationBlocked("内容审核拒绝了这张图")

    monkeypatch.setattr(service.images, "edit_image", fake_edit)
    return str(info_path)


@pytest.mark.asyncio
async def test_5b审核拒收不判fail而是标不可用(tmp_path, monkeypatch):
    """判 fail 会让整单停在 ⑤b，而这件事重跑一万次都是同一个结果。"""
    info_path = _blocked_stub(tmp_path, monkeypatch, [
        {"file": "main-01.jpg", "clean": False, "chinese": True}])
    events = []

    async def _emit(e):
        events.append(e)

    r = await service._clean_main_images(
        {"info_path": info_path, "workdir": str(tmp_path)}, _emit)

    assert r["status"] == "ok", r
    assert "审核拒收" in r["note"], r["note"]
    after = json.loads(open(info_path, encoding="utf-8").read())
    n = after["complianceNotes"]["files"][0]
    assert n["unusable"] is True and n["unusableReason"] == "moderation", n
    # 原图不能被顶替（它压根没有产物），中文标注也要如实留着
    assert n["chinese"] is True and not n.get("clean"), n


@pytest.mark.asyncio
async def test_5b审核拒收只发一次不烧完重试发数(tmp_path, monkeypatch):
    """永久性失败，多烧几发只是白花时间——与「质检未过多烧」的取向刻意相反。"""
    calls = []

    for e in [{"file": "main-01.jpg", "clean": False, "chinese": True}]:
        (tmp_path / e["file"]).write_bytes(b"\xff\xd8\xff\xe0jpeg")
    info_path = tmp_path / "product-info.json"
    info_path.write_text(json.dumps({"complianceNotes": {"files": [
        {"file": "main-01.jpg", "clean": False, "chinese": True}]}}),
        encoding="utf-8")

    def fake_edit(path, prompt=None, out_path=None, **kw):
        calls.append(1)
        raise service.images.ModerationBlocked("内容审核拒绝")

    monkeypatch.setattr(service.images, "edit_image", fake_edit)

    async def _emit(e):
        pass

    await service._clean_main_images(
        {"info_path": str(info_path), "workdir": str(tmp_path)}, _emit)
    assert len(calls) == 1, f"审核拒收不该重烧，实际发了 {len(calls)} 次"


@pytest.mark.asyncio
async def test_5b重跑不再送已标不可用的图(tmp_path, monkeypatch):
    """【死循环的钉子】补充循环按 chinese 追加，不跳过 unusable 就每轮都撞同一张图。"""
    info_path = _blocked_stub(tmp_path, monkeypatch, [
        {"file": "main-01.jpg", "clean": True},
        {"file": "main-02.jpg", "clean": True},
        {"file": "main-03.jpg", "clean": True},
        {"file": "main-04.jpg", "clean": False, "chinese": True},
    ])
    events = []

    async def _emit(e):
        events.append(e)

    ctx = {"info_path": info_path, "workdir": str(tmp_path)}
    first = await service._clean_main_images(ctx, _emit)
    assert first["status"] == "ok" and "审核拒收" in first["note"], first

    events.clear()
    second = await service._clean_main_images(ctx, _emit)
    assert second["status"] == "skipped", second
    assert not events, f"重跑又报了一次人工确认：{events}"


@pytest.mark.asyncio
async def test_5b审核拒收的提醒不叫用户换图(tmp_path, monkeypatch):
    """拒的是这张图本身，人工换图无从下手；处置已由本阶段自动做完（排除出选图）。"""
    info_path = _blocked_stub(tmp_path, monkeypatch, [
        {"file": "main-01.jpg", "clean": False, "chinese": True}])
    events = []

    async def _emit(e):
        events.append(e)

    await service._clean_main_images(
        {"info_path": info_path, "workdir": str(tmp_path)}, _emit)
    msgs = [e["message"] for e in events if e["type"] == "manual_check"]
    assert msgs, events
    assert "请人工换图" not in msgs[0] and "重跑本步" not in msgs[0], msgs[0]
    assert "已排除" in msgs[0] or "排除" in msgs[0], msgs[0]
