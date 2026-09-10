"""来源隔离、独立阶段编排和续跑身份校验，不连接真实店铺。"""

import json

import pytest

from app.publish import pipeline, service, state
from app.publish.sources.base import SourceProduct
from app.publish.workflows import (
    SourceMismatchError,
    composition_for,
    get_workflow,
    resolve_workflow,
    size_normalizer,
)
from app.publish.workflows import alibaba1688, pinduoduo, temu
from app.publish.workflows.base import Stage


URLS = {
    "1688": "https://detail.1688.com/offer/123456.html",
    "pdd": "https://mobile.yangkeduo.com/goods.html?goods_id=123456",
    "temu": "https://www.temu.com/us-en/product-g-123456.html",
}


def info_for(platform):
    return {"source": {"platform": platform, "productId": "123456",
                       "url": URLS[platform]}}


@pytest.mark.parametrize("platform", URLS)
def test_resolves_each_source(platform):
    workflow = resolve_workflow({"url": URLS[platform]}, info=info_for(platform))
    assert workflow.platform == platform
    assert workflow.workflow_id == f"{platform}-dianxiaomi"
    assert workflow.stages()[0].name != "① 采集提炼"


@pytest.mark.parametrize("platform", ["pdd", "temu"])
def test_cross_source_info_rejected(platform):
    with pytest.raises(SourceMismatchError):
        resolve_workflow({"url": URLS["1688"]}, info=info_for(platform))


def test_unknown_source_never_defaults_to_1688():
    assert resolve_workflow({"rowid": "123456"}) is None
    with pytest.raises(ValueError):
        resolve_workflow({"url": "https://example.com/123456"})


def test_stale_source_binding_rejected():
    with pytest.raises(SourceMismatchError):
        resolve_workflow({"url": URLS["pdd"]}, {"source_platform": "1688"})
    with pytest.raises(SourceMismatchError):
        resolve_workflow({"url": URLS["pdd"]}, {"workflow_id": "1688-dianxiaomi"})


def test_other_product_id_rejected():
    info = info_for("pdd")
    info["source"]["productId"] = "999999"
    with pytest.raises(SourceMismatchError):
        resolve_workflow({"url": URLS["pdd"]}, info=info)


def test_embedded_workflow_binding_rejected():
    info = {**info_for("pdd"), "workflow_id": "temu-dianxiaomi"}
    with pytest.raises(SourceMismatchError):
        resolve_workflow({}, info=info)


def test_composition_does_not_leak_between_sources():
    attrs = {"主面料成分": "棉", "主面料成分含量": "90%",
             "面料/材质": "其它/涤纶（聚酯纤维）", "成分含量": "70%"}
    results = {platform: composition_for({**info_for(platform), "attributes": attrs})
               for platform in URLS}
    assert results["1688"]["fiber"] == "棉"
    assert results["1688"]["percent"] == 90
    assert results["pdd"]["fiber"] == "涤纶"
    assert results["pdd"]["percent"] == 70
    assert results["temu"] == {}
    merged = pipeline._merge_composition_sources({**info_for("pdd"), "attributes": attrs})
    assert merged["fiber"] == "涤纶"


def test_explicit_composition_evidence_preserved():
    info = {**info_for("temu"), "compositionFromText": {"main": {"棉": 85}}}
    assert pipeline._merge_composition_sources(info)["percent"] == 85


def test_size_normalization_is_source_specific():
    pdd_size = size_normalizer(info_for("pdd"))
    temu_size = size_normalizer(info_for("temu"))
    alibaba_size = size_normalizer(info_for("1688"))
    assert pdd_size("【直径 60cm+22朵玫瑰】") == "60"
    assert temu_size("110-120") == "110-120"
    assert alibaba_size("110-120") == "110"
    assert temu_size("Asian Tall XL") == "Asian Tall XL"
    assert temu_size("6-9M") == "6-9m"
    assert alibaba_size("L背30") == "L"


def test_adapter_mismatch_rejected_before_conversion():
    product = SourceProduct(platform="pdd", url=URLS["pdd"], productId="123456")
    with pytest.raises(SourceMismatchError):
        get_workflow("1688").prepare_product(product)


@pytest.mark.asyncio
async def test_independent_plans_and_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", str(tmp_path / "states"))
    calls = []

    async def fake_stage(ctx, session, emit):
        calls.append((ctx["source_platform"], ctx["workflow_id"]))
        return {"status": "ok"}

    for platform, module in (("1688", alibaba1688), ("pdd", pinduoduo), ("temu", temu)):
        stage = Stage(f"only_{platform}", platform, fake_stage)
        monkeypatch.setattr(module, "build_stages", lambda selected=stage: (selected,))

    for platform in URLS:
        result = await service.publish_one(None, {"url": URLS[platform]}, "shop")
        assert result["status"] == "ok", result
        saved = state.load_state(state._task_key({"url": URLS[platform]}))
        assert set(saved["stages"]) == {f"only_{platform}"}
        assert saved["workflow_id"] == f"{platform}-dianxiaomi"
    assert calls == [(platform, f"{platform}-dianxiaomi") for platform in URLS]


@pytest.mark.asyncio
async def test_resume_rejects_wrong_source_before_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", str(tmp_path / "states"))
    info_path = tmp_path / "product-info.json"
    info_path.write_text(json.dumps(info_for("pdd")), encoding="utf-8")
    state.save_state({"key": "rowid-123456", "stages": {},
                      "source_platform": "1688", "workflow_id": "1688-dianxiaomi"})
    result = await service.publish_one(None, {"rowid": "123456", "info_path": str(info_path)}, "shop")
    assert result["status"] == "fail"
    assert "来源" in result["note"]


@pytest.mark.asyncio
async def test_explicit_entry_rejects_other_source():
    with pytest.raises(SourceMismatchError):
        await alibaba1688.publish_one(None, {"url": URLS["pdd"]}, "shop")


@pytest.mark.asyncio
async def test_explicit_batch_rejects_mixed_sources_before_start(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("不得启动浏览器批次")

    monkeypatch.setattr(service, "run_batch", forbidden)
    with pytest.raises(SourceMismatchError):
        await pinduoduo.run_batch([{"url": URLS["pdd"]}, {"url": URLS["temu"]}], store="shop")
