# -*- coding: utf-8 -*-
"""发布管线 llm 层的离线单测：ask_json 重试语义 + 可切换模型的选择与可用性。

ask_json：grok 偶尔会把 agent 工具调用当正文吐出来（2026-08-21 属性审核实测），
单次抖动不该搞挂整个阶段；连续失败才抛。

模型切换：UI 下拉 ↔ workspace/publish_llm.json ↔ config.toml 的 [llm.publish*] 段，
三者各管一段，这里钉住它们的衔接语义。
"""
import json

import pytest

from app.publish import llm


class _FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def ask(self, messages, stream=False):
        self.calls += 1
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_ask_json_首次垃圾重试成功(monkeypatch):
    fake = _FakeLLM(["我去工作区找一下字段 shellcallcommand ls", '{"a": 1}'])
    monkeypatch.setattr(llm, "get_llm", lambda stage=None: fake)
    r = await llm.ask_json("随便", what="测试")
    assert r == {"a": 1}
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_ask_json_连续垃圾抛异常(monkeypatch):
    fake = _FakeLLM(["废文1", "废文2", "废文3"])
    monkeypatch.setattr(llm, "get_llm", lambda stage=None: fake)
    with pytest.raises(RuntimeError, match="连续 3 次"):
        await llm.ask_json("随便", what="测试")
    assert fake.calls == 3


@pytest.mark.asyncio
async def test_ask_json_带围栏的散文也能抠(monkeypatch):
    fake = _FakeLLM(['前言\n```json\n{"ok": true}\n```\n后记'])
    monkeypatch.setattr(llm, "get_llm", lambda stage=None: fake)
    r = await llm.ask_json("随便", what="测试")
    assert r == {"ok": True}
    assert fake.calls == 1


# ---- 模型切换 -----------------------------------------------------------------

@pytest.fixture
def _isolate_llm_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_LLM_PREFS_PATH", str(tmp_path / "publish_llm.json"))


def test_choice_默认与持久化(_isolate_llm_prefs):
    assert llm.get_llm_choice() == "grok"  # 文件不存在回落默认
    llm.set_llm_choice("kimi")
    assert llm.get_llm_choice() == "kimi"
    with pytest.raises(ValueError):
        llm.set_llm_choice("不存在的模型")


def test_choice_文件值非法回落默认(_isolate_llm_prefs):
    with open(llm._LLM_PREFS_PATH, "w", encoding="utf-8") as f:
        json.dump({"choice": "被改坏的值"}, f)
    assert llm.get_llm_choice() == "grok"


def _write_toml(tmp_path, monkeypatch, sections: dict):
    """sections: config_name -> {api_key: ...}，写成临时 config.toml 并指过去。"""
    lines = []
    for name, fields in sections.items():
        lines.append(f"[llm.{name}]")
        for k, v in fields.items():
            lines.append(f'{k} = "{v}"')
    p = tmp_path / "config.toml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(llm, "_CONFIG_TOML_PATH", str(p))


def test_list_choices_可用性(_isolate_llm_prefs, tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, {
        "publish": {"api_key": "sk-real"},
        "publish-kimi": {"api_key": ""},       # 没填 key
    })
    choices = {c["id"]: c for c in llm.list_llm_choices()}
    assert choices["grok"]["available"] is True
    assert choices["kimi"]["available"] is False
    assert choices["grok"]["active"] is True and choices["kimi"]["active"] is False
    # 段整个缺失也算不可用
    _write_toml(tmp_path, monkeypatch, {"publish": {"api_key": "sk-real"}})
    choices = {c["id"]: c for c in llm.list_llm_choices()}
    assert choices["kimi"]["available"] is False


def test_每个选择的模型都在多模态白名单里():
    """钉住「加了选项忘了登记模型」这个坑。

    app.llm.ask_with_images 对不在 MULTIMODAL_MODELS 里的模型直接抛 ValueError，
    而发布管线阶段⑥⑦⑬ 全要看图——漏登记的表现是切过去后看图阶段全线失败，
    而文本阶段照常，很容易误判成端点问题。
    """
    from app.config import config as app_config
    from app.llm import MULTIMODAL_MODELS

    for cid, meta in llm.LLM_CHOICES.items():
        if meta.get("backend") == "gemini-web":
            continue  # 官网直连不走 HTTP 白名单（见 llm._choice_multimodal）
        section = (app_config.llm or {}).get(meta["config_name"])
        assert section is not None, f"{cid}: config 里没有 [llm.{meta['config_name']}] 段"
        assert section.model in MULTIMODAL_MODELS, (
            f"{cid}: 模型 {section.model} 未登记进 MULTIMODAL_MODELS，看图阶段会抛 ValueError")


def test_deepseek_选项已登记():
    """DeepSeek 视觉段的模型名/端点按官方文档钉死，改错了看图会 404 model_not_found。"""
    from app.config import config as app_config

    meta = llm.LLM_CHOICES["deepseek"]
    assert meta["config_name"] == "publish-deepseek"
    section = (app_config.llm or {}).get("publish-deepseek")
    assert section.model == "deepseek-v4-flash-vision-exp"
    assert "api.deepseek.com" in section.base_url


def test_packy_deepseek_选项已登记():
    """Packy 版 deepseek 段：与官方段同模型、不同网关，两者都要在册。

    钉住三件事：段名对得上、模型名与官方段一致（同一个模型，故白名单天然覆盖）、
    base_url 指的是 Packy 网关而不是官方端点——写错了会拿 Packy 的 key 打官方域名，
    表现为 401 而非「模型不存在」，排查方向容易带偏。
    """
    from app.config import config as app_config

    meta = llm.LLM_CHOICES["packy-deepseek"]
    assert meta["config_name"] == "publish-packy-deepseek"
    section = (app_config.llm or {}).get("publish-packy-deepseek")
    assert section is not None, "config 里没有 [llm.publish-packy-deepseek] 段"
    assert section.model == "deepseek-v4-flash-vision-exp"
    assert "cf.api.fan" in section.base_url
    # 与 grok 段的区别：deepseek 走标准 chat/completions，不能配成 openai-response
    # （2026-08-25 实测两个协议都通，取 chat 以复用既有 image_url 拼装）
    assert section.api_type == "openai"


def test_get_llm_按选择映射config_name(_isolate_llm_prefs, monkeypatch):
    seen = []

    class _Recorder:
        def __init__(self, config_name="default", llm_config=None):
            seen.append(config_name)

    monkeypatch.setattr(llm, "LLM", _Recorder)
    llm.get_llm()
    llm.set_llm_choice("kimi")
    llm.get_llm()
    assert seen == ["publish", "publish-kimi"]


# ---- 按阶段覆盖模型 -------------------------------------------------------------
# 阶段覆盖的动机是耗时（见 llm.LLM_STAGES 注释：属性审核换非推理档 65 秒→17 秒），
# 但它同时引入两个能静默出错的地方，这里各钉一条：
#   1. 视觉阶段配了纯文本模型 → 该阶段每次调用抛 ValueError（不是慢一点）
#   2. 改全局默认时顺手清掉了阶段覆盖 → 用户以为还在提速，实际全走回默认

def test_stage_覆盖读写与回落(_isolate_llm_prefs):
    llm.set_llm_choice("grok")
    assert llm.get_stage_choice("attrs") == "grok"      # 没配覆盖时跟随默认
    llm.set_stage_choice("attrs", "kimi-highspeed")
    assert llm.get_stage_choice("attrs") == "kimi-highspeed"
    assert llm.get_stage_choice("desc") == "grok"       # 其它阶段不受影响
    llm.set_stage_choice("attrs", None)                 # None = 跟随默认
    assert llm.get_stage_overrides() == {}


def test_stage_改默认不清覆盖(_isolate_llm_prefs):
    llm.set_llm_choice("grok")
    llm.set_stage_choice("titles", "kimi-highspeed")
    llm.set_llm_choice("deepseek")
    assert llm.get_llm_choice() == "deepseek"
    assert llm.get_stage_overrides() == {"titles": "kimi-highspeed"}


def test_stage_非法值(_isolate_llm_prefs):
    with pytest.raises(ValueError, match="未知阶段"):
        llm.set_stage_choice("不存在的阶段", "grok")
    with pytest.raises(ValueError, match="未知模型选择"):
        llm.set_stage_choice("attrs", "不存在的模型")


def test_stage_文件被手改后只留合法项(_isolate_llm_prefs):
    with open(llm._LLM_PREFS_PATH, "w", encoding="utf-8") as f:
        json.dump({"choice": "grok",
                   "stages": {"attrs": "kimi", "错阶段": "grok", "titles": "错模型"}}, f)
    # 手改出的非法项静默丢弃（回落默认照旧能跑），不该让整条管线起不来
    assert llm.get_stage_overrides() == {"attrs": "kimi"}


def test_stage_视觉阶段拒非多模态(_isolate_llm_prefs, monkeypatch):
    """看图阶段配纯文本模型必须在设置时就拒掉。

    当前 6 个选项恰好全是多模态，故摘掉白名单里的一项来构造这个场景——
    真正要防的是【将来新增一个纯文本快档】时被配到看图阶段上。
    """
    from app import llm as core

    monkeypatch.setattr(
        core, "MULTIMODAL_MODELS",
        [m for m in core.MULTIMODAL_MODELS if m != "deepseek-v4-flash-vision-exp"])
    assert llm._choice_multimodal("deepseek") is False
    with pytest.raises(ValueError, match="不是多模态"):
        llm.set_stage_choice("desc", "deepseek")        # 视觉阶段：拒
    llm.set_stage_choice("attrs", "deepseek")           # 文本阶段：放行
    assert llm.get_stage_choice("attrs") == "deepseek"


def test_get_llm_按阶段映射config_name(_isolate_llm_prefs, monkeypatch):
    seen = []

    class _Recorder:
        def __init__(self, config_name="default", llm_config=None):
            seen.append(config_name)

    llm.set_llm_choice("grok")
    llm.set_stage_choice("attrs", "kimi-highspeed")
    monkeypatch.setattr(llm, "LLM", _Recorder)
    llm.get_llm("attrs")        # 有覆盖 → 覆盖的段
    llm.get_llm("desc")         # 无覆盖 → 默认段
    llm.get_llm()               # 不传阶段 → 默认段
    assert seen == ["publish-kimi-highspeed", "publish", "publish"]


def test_list_stages_给UI的形状(_isolate_llm_prefs):
    llm.set_llm_choice("grok")
    llm.set_stage_choice("attrs", "kimi-highspeed")
    rows = {s["id"]: s for s in llm.list_llm_stages()}
    assert rows["attrs"]["override"] == "kimi-highspeed"
    assert rows["attrs"]["effective"] == "kimi-highspeed"
    assert rows["desc"]["override"] is None
    assert rows["desc"]["effective"] == "grok"          # 前端不算账，后端给实际值
    assert rows["desc"]["vision"] is True and rows["attrs"]["vision"] is False


# 【可配模型但不是可续跑阶段】的判断点：它们在某个管线阶段【内部】跑，故不进
# service.STAGES（那张表驱动续跑下拉与 _STAGE_FUNCS，塞进去会多出一个没有实现函数的
# 假阶段），但仍要能单独配模型。
#   extract_text：阶段①b 从详情文字抽尺码表，在 extract 内部跑（2026-09-01 新增）。
#     与 ① 分开登记正因为它【不看图】——纯文本判断点能走快档模型，没必要跟视觉档一起慢。
_NON_PIPELINE_STAGES = {"extract_text"}


def test_每个LLM阶段id都在service的STAGES里():
    """阶段 id 必须与 service.STAGES 对齐：UI 阶段名、续跑下拉、模型覆盖共用一套命名。

    对不上的表现是「设了覆盖但不生效」——UI 上看着配好了，实际调用点传的 stage
    与登记表里的 key 不是一个字符串，静默回落默认。

    例外见 _NON_PIPELINE_STAGES：那些是阶段内部的判断点，白名单显式列出而不是放宽
    判据——否则下次真写错一个阶段 id 时这条测试就拦不住了。
    """
    from app.publish.service import STAGES

    ids = {s for s, _ in STAGES}
    for s in llm.LLM_STAGES:
        if s["id"] in _NON_PIPELINE_STAGES:
            continue
        assert s["id"] in ids, f"{s['id']} 不在 service.STAGES 里"


def test_阶段内部判断点仍可配模型():
    """_NON_PIPELINE_STAGES 里的 id 要真的能设覆盖：白名单不能变成「免检」。"""
    for sid in _NON_PIPELINE_STAGES:
        assert sid in {s["id"] for s in llm.LLM_STAGES}, f"{sid} 没登记进 LLM_STAGES"
        assert sid in llm._LLM_STAGE_IDS, f"{sid} 不被 set_stage_choice 接受"


def test_active_label_带出阶段覆盖(_isolate_llm_prefs):
    llm.set_llm_choice("grok")
    assert "阶段覆盖" not in llm.active_llm_label()
    llm.set_stage_choice("attrs", "kimi-highspeed")
    label = llm.active_llm_label()
    assert "阶段覆盖" in label and "属性审核" in label
