"""测试标题合规闸——主观营销用语（Must Have / Best / Perfect 等）。"""
import pytest


@pytest.mark.asyncio
async def test_subjective_claim_rejected():
    """主观营销用语被拒（英文标题）：Must Have / Best / Perfect / Top 等。"""
    from app.publish.pipeline import generate_titles

    info = {
        "title": "儿童格子连衣裙",
        "attributes": {"品牌": "无"},
        "skus": [{"price": "88.00", "priceUnit": "元/件"}],
        "imageUnderstanding": "",
    }
    # 模拟 LLM 返回含主观营销词的标题
    import app.publish.llm as pub_llm
    violating = [
        {"enTitle": "Must Have Kids Plaid Dress for Summer, Breathable Cotton"},
        {"enTitle": "Best Quality Girls Plaid Dress, Sleeveless, For Party"},
        {"enTitle": "Perfect Toddler Plaid Dress, Adjustable Straps, Daily Wear"},
        {"enTitle": "Top Rated Kids Dress, Plaid Pattern, Soft Fabric, Casual"},
        {"enTitle": "Amazing Girls Dress Set, Plaid Style, Lightweight Material"},
    ]

    async def _mock(*a, **kw):
        # 第一次返回全是违规词，第二次（重试）返回干净的
        if not hasattr(_mock, "called"):
            _mock.called = True
            return {"candidates": violating[:3], "recommend": 0, "title": "儿童格子连衣裙"}
        return {
            "candidates": [
                {"enTitle": "Kids Plaid Dress for Summer, Breathable Cotton, Casual Wear"}
            ],
            "recommend": 0,
            "title": "儿童夏季格子连衣裙透气棉质休闲款",
        }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(pub_llm, "ask_json", _mock)
    r = await generate_titles(info)
    assert r["status"] == "ok", f"should retry and succeed: {r}"
    # 干净标题不含主观词
    en = r["generated"]["enTitle"]
    assert "must" not in en.lower() and "best" not in en.lower()


@pytest.mark.asyncio
async def test_subjective_claim_chinese():
    """中文主观营销用语被拒：好物 / 神器 / 必备 / 最好 / 第一 等。"""
    from app.publish.pipeline import generate_titles
    import app.publish.llm as pub_llm

    info = {
        "title": "宠物牵引绳",
        "attributes": {"品牌": "无"},
        "skus": [{"price": "35.00", "priceUnit": "元/件"}],
        "imageUnderstanding": "",
    }

    async def _mock(*a, **kw):
        if not hasattr(_mock, "called"):
            _mock.called = True
            return {
                "candidates": [
                    {"enTitle": "Adjustable Pet Leash for Small Dogs, Durable Nylon"}
                ],
                "recommend": 0,
                "title": "养宠必备神器小型犬牵引绳最好用",  # 违规中文
            }
        return {
            "candidates": [
                {"enTitle": "Adjustable Pet Leash for Small Dogs, Durable Nylon"}
            ],
            "recommend": 0,
            "title": "小型犬可调节牵引绳耐用尼龙材质",
        }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(pub_llm, "ask_json", _mock)
    r = await generate_titles(info)
    assert r["status"] == "ok"
    zh = r["generated"]["title"]
    # 干净中文标题不含主观营销词
    assert not any(w in zh for w in ["必备", "神器", "最好", "好物", "第一", "完美"])


@pytest.mark.asyncio
async def test_subjective_claim_exhausted_retries():
    """两次都返回违规词，最终报错且原因明确。"""
    from app.publish.pipeline import generate_titles
    import app.publish.llm as pub_llm

    info = {
        "title": "儿童书包",
        "attributes": {"品牌": "无"},
        "skus": [{"price": "120.00", "priceUnit": "元/件"}],
        "imageUnderstanding": "",
    }

    async def _mock(*a, **kw):
        # 两次都返回含 Must Have 的标题
        return {
            "candidates": [
                {"enTitle": "Must Have Kids Backpack for School, Waterproof Design"}
            ],
            "recommend": 0,
            "title": "儿童书包防水设计",
        }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(pub_llm, "ask_json", _mock)
    r = await generate_titles(info)
    assert r["status"] == "error"
    assert "title-generation-failed" in r["reason"]
    # 错误信息里应该提到「含主观营销用语」
    assert "主观营销" in str(r.get("err", ""))
