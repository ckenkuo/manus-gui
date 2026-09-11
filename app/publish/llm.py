"""发布管线的 LLM 判断层：把 6 个单发文本判断点收到一处。

为什么单独一层而不直接在 pipeline 里调 LLM：
原 skill 的 dianxiaomi_edit.py 里散着两个几乎一样的 DeepSeek 调用函数
（deepseek_chat / _deepseek_json），各自硬编码 URL、模型名和兜底 key，改模型要改多处、
密钥进了版本库。这里统一走项目的 app.llm.LLM（config_name="publish"，见
config.toml 的 [llm.publish] 段），于是换模型只改配置、密钥不落代码。

判断点（都是「小输入 → 要 JSON → 判完就完」，无历史累积，故用单发而非 agent 循环）：
    阶段③ 类目：从候选叶子类目里选一个（UI 树与 API 树不一致时会二次调用兜底）
    阶段④ 属性：比对源商品信息与表单当前属性，产出修改清单
    阶段⑤ 标题：生成中英文标题（禁品牌词/emoji，不照抄源标题）
    阶段⑧ 尺码表：按源身高体重估算各尺码测量值
    阶段⑨ SKU 货号：颜色名翻译成英文（SKU 货号禁中文）
    阶段⑩ SKU 分类 + 包装清单判断
    阶段① 视觉回填（ask_json_with_images）：看主图/详情图填 imageUnderstanding /
        sizeChart / sizeMeasurements / complianceNotes

原 skill 的结论是「standalone 不能看图，只能 agent 人工看」（SKILL.md 阶段①：Packy 的
gpt-4o 系视觉渠道 503 model_not_found，2026-08-19 实测）。改配 grok-4.6 后 Packy 同一个
端点已能收图，故补 ask_json_with_images 这条视觉入口。注意与 images.py 的 Packy
gpt-image-2 不是一回事：那边是【图像编辑】（图生图去中文/英化），这里是【图像理解】，
两条都要留。

【max_tokens 必须给足】deepseek-flash 是推理模型，先产 reasoning_content 再产
content。额度给小了推理就占满、content 返回空字符串——表现为「调用成功但结果为空」，
排查时极容易误以为是提示词问题。[llm.publish] 里配了 16000（grok-4.6 同理），别往下调。

判断失败一律抛异常交上层重试兜底，不在这里造 fallback 值：类目/属性填错了会一路带到
发布，比直接失败更糟（这与项目「辅助路径 best-effort、主流程失败才抛」的取向一致——
这些判断都是主流程）。
"""
import base64
import json
import os
import tempfile
import tomllib
from typing import Annotated, Any, Optional

from pydantic import BaseModel, StringConstraints, ValidationError

# 复用采集管道已实测的 JSON 解析（剥 ```json 围栏 + 兜底抓首个 {...}），避免重复实现。
from app.collect.pipeline import _parse_json
from app.config import config
from app.llm import LLM
from app.logger import logger
from app.schema import Message

# LLM 配置段名：对应 config.toml 的 [llm.publish]。配置缺失时 app.config 会回落到
# [llm]（见 _load_initial_config 的 default_settings 合并），故这里不必自己兜底。
CONFIG_NAME = "publish"

# ---- 可切换模型（发布页 UI 下拉）------------------------------------------------
# 每个选择对应 config.toml 里一个 [llm.<config_name>] 段；当前选择持久化在
# workspace/publish_llm.json（不放进 publish_prefs.json：那边是 service 层的
# 店铺/站点偏好，这边是 LLM 层，各自独立读写互不覆盖）。
LLM_CHOICES = {
    "grok": {"config_name": "publish", "label": "grok-4.6（Packy）"},
    "kimi": {"config_name": "publish-kimi", "label": "Kimi k3 1M（Kimi Code）"},
    "kimi-k256k": {"config_name": "publish-kimi-k256k",
                   "label": "Kimi k3 256K（Kimi Code）"},
    "kimi-k27": {"config_name": "publish-kimi-k27",
                 "label": "Kimi K2.7 Code（Kimi Code）"},
    "kimi-highspeed": {"config_name": "publish-kimi-highspeed",
                       "label": "Kimi K2.7 高速（Kimi Code）"},
    "deepseek": {"config_name": "publish-deepseek",
                 "label": "DeepSeek 视觉（官方）"},
    # 【2026-09-10 移除 packy-deepseek 档】该网关没有 deepseek-flash；它的 DeepSeek 线
    # （deepseek-v4-flash / v4-pro）实测均返回 400「Model do not support image input」，
    # 原配的 deepseek-v4-flash-vision-exp 也已 503 model_not_found——Packy 上已无可用
    # 的 DeepSeek 视觉档，故整档删掉（config.toml 的 [llm.publish-packy-deepseek] 同步移除）。
    # Gemini 官网直连（CDP 驱动已登录 Chrome 的 gemini.google.com 页签），不经反代/
    # 官方 API，绕开反代封号风险。backend="gemini-web" 标记它不走 app.llm.LLM 的
    # HTTP 链路，而是 app/gemini_web.py 的浏览器对话（见 get_llm 的分流）。
    # 官网一次问话最多 10 个附件（图片），超过会在 GeminiWebClient.ask 里报错。
    "gemini-web": {"config_name": "publish-gemini-web", "label": "Gemini 官网（CDP 直连）",
                   "backend": "gemini-web"},
}
_DEFAULT_CHOICE = "grok"
_LLM_PREFS_PATH = os.path.join("workspace", "publish_llm.json")
_CONFIG_TOML_PATH = os.path.join("config", "config.toml")

# ---- 按阶段覆盖模型 -------------------------------------------------------------
# 【为什么要分阶段而不是一个全局选择】2026-08-24 实测同一批商品的耗时账：属性审核
# 用推理型的 deepseek 档要 65 秒（7523 completion token，其中约 6700 是推理链），
# 换成非推理的 kimi 高速档同样的活 17 秒干完；标题阶段更极端，一次烧掉 15896
# completion token（2 分 13 秒）。但视觉阶段（看图）不能随便换——能力差异是真的。
# 故改成「全局默认 + 按阶段覆盖」两层：慢而准的模型留给看图，纯文本判断点走快档。
#
# key 与 service.STAGES 的阶段 id 对齐（UI 里阶段名、续跑下拉、这里共用一套命名），
# 不另造名字。vision=True 的阶段要把图喂给模型，选项必须在 app/llm.py 的
# MULTIMODAL_MODELS 白名单里，否则 ask_with_images 直接抛 ValueError（不是静默丢图）。
LLM_STAGES = [
    {"id": "extract", "label": "① 采集提炼（视觉回填）", "vision": True},
    # ①b 是【纯文本】判断点：从详情描述的明文里抽尺码表（见 extract.enrich_desc_text）。
    # 与 ① 分开登记而不是共用，正因为它不看图——能走快档模型，没必要跟着视觉档一起慢。
    {"id": "extract_text", "label": "①b 详情文字尺码表", "vision": False},
    {"id": "auto_cat", "label": "③ 产品类目", "vision": False},
    # 品类识别（pipeline.classify_category 的非服装细分）：纯文本、轻量，服务⑨尺码表
    # 估算与⑪ SKU分类的品类分流。单独登记好让用户能用快档模型跑这个高频小判断。
    {"id": "category", "label": "品类识别（尺码/SKU分流）", "vision": False},
    {"id": "attrs", "label": "④ 属性审核", "vision": False},
    # ④b 主纤维归一化：把「棉混纺」「涤棉」这类源「主面料成分」合成词/占位词归约成
    # 确定主纤维。单独登记以便用快档模型跑这个轻量判断，不并进属性审核那种几十字段的慢档。
    {"id": "comp_norm", "label": "④b 成分纤维归一（合成词→主纤维）", "vision": False},
    {"id": "titles", "label": "⑤ 标题生成", "vision": False},
    {"id": "clean_images", "label": "⑤b 图片英化质检", "vision": True},
    {"id": "material", "label": "⑥ 素材图选图", "vision": True},
    {"id": "skc", "label": "⑦ SKC 分色选图", "vision": True},
    {"id": "sizechart", "label": "⑨ 尺码表估算", "vision": False},
    {"id": "sku_code", "label": "⑩a SKU 货号翻译", "vision": False},
    {"id": "variant", "label": "⑩ 变种/包装判断", "vision": False},
    {"id": "stock", "label": "⑪ SKU 分类判断", "vision": False},
    {"id": "desc", "label": "⑬ 描述图规划与质检", "vision": True},
]
_LLM_STAGE_IDS = {s["id"] for s in LLM_STAGES}
_VISION_STAGE_IDS = {s["id"] for s in LLM_STAGES if s["vision"]}


def _read_prefs() -> dict:
    """读偏好文件；文件缺失/损坏都当空配置（全部回落默认），不抛。"""
    try:
        with open(_LLM_PREFS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_llm_choice() -> str:
    """全局默认模型 id（LLM_CHOICES 的 key）；文件丢了/值非法都回落默认。"""
    choice = _read_prefs().get("choice")
    return choice if choice in LLM_CHOICES else _DEFAULT_CHOICE


def get_stage_overrides() -> dict:
    """按阶段的模型覆盖 {stage_id: choice_id}，只保留合法且已登记的项。

    非法值静默丢弃而不是报错：这份文件可能被手改，一个错拼的阶段名不该让整条
    管线起不来——回落到全局默认是安全的行为（照旧能跑，只是没提速）。
    """
    raw = _read_prefs().get("stages")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items()
            if k in _LLM_STAGE_IDS and v in LLM_CHOICES}


def get_stage_choice(stage: Optional[str]) -> str:
    """某阶段实际用的模型 id：有覆盖用覆盖，否则用全局默认。"""
    if stage:
        override = get_stage_overrides().get(stage)
        if override:
            return override
    return get_llm_choice()


def _write_prefs(choice: str, stages: dict) -> None:
    os.makedirs(os.path.dirname(_LLM_PREFS_PATH), exist_ok=True)
    with open(_LLM_PREFS_PATH, "w", encoding="utf-8") as f:
        json.dump({"choice": choice, "stages": stages}, f, ensure_ascii=False)


def set_llm_choice(choice: str) -> None:
    """改全局默认，保留已有的按阶段覆盖（切默认不该悄悄清掉阶段配置）。"""
    if choice not in LLM_CHOICES:
        raise ValueError(f"未知模型选择 {choice!r}，可选：{list(LLM_CHOICES)}")
    _write_prefs(choice, get_stage_overrides())


def set_stage_choice(stage: str, choice: Optional[str]) -> None:
    """设/清某阶段的模型覆盖。choice 传 None 或空串表示「跟随全局默认」。

    视觉阶段挡住非多模态模型：选错了不是慢一点，是该阶段每次调用直接抛
    ValueError（见 MULTIMODAL_MODELS 的注释），不如在设置时就拒掉。
    """
    if stage not in _LLM_STAGE_IDS:
        raise ValueError(f"未知阶段 {stage!r}，可选：{sorted(_LLM_STAGE_IDS)}")
    stages = get_stage_overrides()
    if not choice:
        stages.pop(stage, None)
    else:
        if choice not in LLM_CHOICES:
            raise ValueError(f"未知模型选择 {choice!r}，可选：{list(LLM_CHOICES)}")
        if stage in _VISION_STAGE_IDS and not _choice_multimodal(choice):
            raise ValueError(
                f"{LLM_CHOICES[choice]['label']} 不是多模态模型，"
                f"不能用于需要看图的阶段（{stage}）")
        stages[stage] = choice
    _write_prefs(get_llm_choice(), stages)


def _section_api_key(config_name: str) -> str:
    """直接读 config.toml 原文里该段的 api_key。

    为什么不能看加载后的 config.llm：app/config.py 合并时会【剔除空 api_key 再合并】
    （空串会盖掉 [llm] 默认 key，见 _load_initial_config 注释），于是段里留
    api_key = "" 时，合并结果反而带着 [llm] 默认段的真 key——拿它判可用性会
    误判为「已配置」，切过去就拿别家的 key 打 Kimi 的端点（2026-08-21 实测踩中）。
    """
    try:
        with open(_CONFIG_TOML_PATH, "rb") as f:
            raw = tomllib.load(f)
        return str(raw.get("llm", {}).get(config_name, {}).get("api_key") or "").strip()
    except Exception:
        return ""


def _choice_available(config_name: str) -> bool:
    """对应配置段是否可用：config.toml 里该段的 api_key 已填（空串/占位符都算没填）。"""
    key = _section_api_key(config_name)
    return bool(key) and not key.startswith("<")


def _choice_multimodal(choice: str) -> bool:
    """该选择的模型能不能看图：查 app/llm.py 的 MULTIMODAL_MODELS 白名单。

    判据只能是白名单，不能是「配置段里写了 vision 字样」——ask_with_images 就是拿
    这份名单做闸的（不在册直接抛 ValueError），任何别的判据都会和它对不上。
    """
    from app.llm import MULTIMODAL_MODELS

    if LLM_CHOICES[choice].get("backend") == "gemini-web":
        return True  # 官网对话天然能看图，不查 HTTP 白名单
    section = (config.llm or {}).get(LLM_CHOICES[choice]["config_name"])
    return bool(section) and getattr(section, "model", "") in MULTIMODAL_MODELS


def list_llm_stages() -> list:
    """给 UI 的按阶段配置清单：阶段 id/显示名/是否要看图/当前覆盖值。

    override 为 None 表示「跟随全局默认」，effective 是实际会用的模型 id——
    前端只渲染不算账，免得两边各算一遍算出不一样的结果。
    """
    overrides = get_stage_overrides()
    default = get_llm_choice()
    return [{
        "id": s["id"],
        "label": s["label"],
        "vision": s["vision"],
        "override": overrides.get(s["id"]),
        "effective": overrides.get(s["id"]) or default,
    } for s in LLM_STAGES]


def list_llm_choices() -> list:
    """给 UI 的选项清单：id/显示名/模型名/是否可用/是否当前激活。"""
    active = get_llm_choice()
    out = []
    for cid, meta in LLM_CHOICES.items():
        if meta.get("backend") == "gemini-web":
            # 官网直连不走 config.toml 段，可用性取决于「CDP 里有没有已登录的 gemini
            # 页签」；这里不每次探测、先视为可用，真正调用时连接失败会给出明确报错。
            out.append({
                "id": cid, "label": meta["label"], "model": "gemini.google.com",
                "available": True, "active": cid == active, "multimodal": True,
            })
            continue
        section = (config.llm or {}).get(meta["config_name"])
        out.append({
            "id": cid,
            "label": meta["label"],
            "model": getattr(section, "model", "") if section else "",
            "available": _choice_available(meta["config_name"]),
            "active": cid == active,
            # 前端据此禁掉视觉阶段里的非多模态项（见 set_stage_choice 的同名闸）
            "multimodal": _choice_multimodal(cid),
        })
    return out


def active_llm_label() -> str:
    """当前模型配置的显示名（批次开始事件里用，让日志能看出每批用的谁）。

    配了按阶段覆盖时要把覆盖也带出来：否则日志上只写着「默认用 X」，而实际慢/快
    在哪个阶段完全看不出来，事后对耗时账时会误判。
    """
    base = LLM_CHOICES[get_llm_choice()]["label"]
    overrides = get_stage_overrides()
    if not overrides:
        return base
    stage_labels = {s["id"]: s["label"] for s in LLM_STAGES}
    detail = "、".join(
        f"{stage_labels.get(sid, sid)}→{LLM_CHOICES[cid]['label']}"
        for sid, cid in sorted(overrides.items()))
    return f"{base}（阶段覆盖：{detail}）"


def _content_text(m) -> str:
    """从 Message 对象或 dict 里取纯文本内容（多模态 content 只取 text 段）。

    Gemini 官网对话只收一段文本，故把发布管线的 messages/system_msgs 压平成纯文本。
    """
    c = getattr(m, "content", None)
    if c is None and isinstance(m, dict):
        c = m.get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        return " ".join(
            seg.get("text") or "" for seg in c
            if isinstance(seg, dict) and seg.get("text")
        ).strip()
    return ""


def _messages_to_text(messages, system_msgs=None) -> str:
    """把 messages + system_msgs 拼成一段 prompt。

    官网对话没有 system role，故 system 文本直接排在前面（发布管线视觉阶段的 _SYS
    本就是完整的角色设定句子，原样前置即可，无需加「系统：」前缀）。
    """
    chunks = []
    for m in (system_msgs or []):
        t = _content_text(m)
        if t:
            chunks.append(t)
    for m in (messages or []):
        t = _content_text(m)
        if t:
            chunks.append(t)
    return "\n\n".join(chunks)


class GeminiWebLLM:
    """把 Gemini 官网对话适配成 app.llm.LLM 的 ask / ask_with_images 接口。

    发布管线的 ask_json / ask_json_with_images 只调这两个方法（签名对齐 LLM），
    故适配器只实现它们，让所有判断点零改动就能切到官网直连。

    与 HTTP LLM 的差异：
    - 官网无 system role，system_msgs 由 _messages_to_text 拼进 prompt 开头；
    - 每次调用新建一次 CDP 连接、用完断开——先保证正确，批量发布的长连接复用
      待确认要用它跑生产批次后再参照 app/publish/browser.py 的 BrowserSession 优化。
    """

    async def ask(self, messages, system_msgs=None, stream=False, temperature=None):
        from app.gemini_web import GeminiWebClient

        prompt = _messages_to_text(messages, system_msgs)
        async with GeminiWebClient() as g:
            return await g.ask(prompt)

    async def ask_with_images(self, messages, images, system_msgs=None,
                              stream=False, temperature=None):
        from app.gemini_web import GeminiWebClient

        prompt = _messages_to_text(messages, system_msgs)
        # images 是 image_ref 产出的 data URL 列表，GeminiWebClient 已能直接收 data URL
        async with GeminiWebClient() as g:
            return await g.ask(prompt, images=images)


_GEMINI_WEB: Optional[GeminiWebLLM] = None


def get_llm(stage: Optional[str] = None):
    """取该阶段该用的 LLM（HTTP 单例或 gemini-web 适配器，按选择分流）。

    stage 传 service.STAGES 的阶段 id（见 LLM_STAGES）：配了覆盖用覆盖的模型，
    没配就用全局默认。不传 stage 等于「用全局默认」，供尚未细分的调用点沿用。

    切换模型即切换 config_name——不同选择是不同单例，互不污染，也无需重置。
    gemini-web 不是 HTTP LLM，返回共享的 GeminiWebLLM 适配器（见其 docstring）。
    """
    choice = get_stage_choice(stage)
    meta = LLM_CHOICES[choice]
    if meta.get("backend") == "gemini-web":
        global _GEMINI_WEB
        if _GEMINI_WEB is None:
            _GEMINI_WEB = GeminiWebLLM()
        return _GEMINI_WEB
    return LLM(config_name=meta["config_name"])


def reset_token_counters() -> None:
    """清零所有已登记模型的 token 计数。

    LLM 是按 config_name 的进程级单例，check_token_limit 用的是【累计】
    total_input_tokens；不清零则批量发布多个商品时会一路累加、最终误触上限。
    与 collect 侧 reset_pipeline_llms 同一个理由。

    【为什么要遍历全部而不只清当前那个】按阶段覆盖后一次批次会同时用到好几个
    config_name（如视觉走 deepseek、文本走 kimi 高速），只清全局默认那个会把
    其余几个的累计量留着，几批之后照样误触上限——那正是本函数要防的事。
    只清已实例化的，避免为了清零反而把没用到的段全建一遍单例。
    """
    for meta in LLM_CHOICES.values():
        inst = LLM._instances.get(meta["config_name"])
        if inst is not None:
            inst.total_input_tokens = 0
            inst.total_completion_tokens = 0


# 重试时追加的硬约束：grok 偶尔会把 agent 工具调用当正文吐出来（2026-08-21 实测
# 属性审核返回 "shellcallcommand ls -la && find ..." 残骸），原样重问往往还是
# 同样的调调，明确「第一个字符必须是 {」才有拉回来的机会。
_JSON_RETRY_HINT = (
    "\n\n【重申，最高优先级】只输出一个 JSON 对象：不要输出任何思考过程、"
    "工具调用、shell 命令或解释文字。第一个字符必须是 {，最后一个字符必须是 }。"
)

# 断尾专用提醒：正文以 { 开头却解析不出来，几乎都是模型没写完就收笔。这时重申
# 「只输出 JSON」是白说——它本来就在输出 JSON，缺的是把它写完。
# 2026-09-03 取证（offer 968804940018 阶段①视觉回填）：completion 2049 token 处
# 断在 imageUnderstanding 中间，而该档 max_tokens 配的 32000 只用掉 6%，
# finish_reason 不可能是 length，是模型自己收的笔（同批下一个商品早停在 1766）。
# 故这条改为要求它把字段值写短、优先保证结构闭合——直接对着「写不完」下药，
# 而不是去抬那个根本没用完的额度。
_JSON_TRUNCATED_HINT = (
    "\n\n【重申，最高优先级】上一次回答在中途断掉了、JSON 没有闭合。这次请把每个"
    "字段的文字描述【尽量精简】（能少写就少写，不要任何解释），优先保证整个 JSON "
    "结构完整、最后一个字符是 }。"
)


def _retry_hint(raw: str, why: str = "") -> str:
    """按上一次响应的形态挑重试提醒：结构不对、断尾、吐废文要说的不是一回事。

    【为什么不能三种共用一句】说错话是有代价的：对着断尾的响应重申「只输出 JSON」，
    模型下一次照样写不完；反过来对着结构不对的响应喊「写短点」，它会把本来该填的
    字段一起精简掉。故按形态分开给词，把话说到点上。
    """
    if why:
        return (f"\n\n【重申，最高优先级】上一次回答的结构不对：{why}。这次请严格按"
                "提示词末尾给出的 JSON 形状输出，字段名与值的类型都不要改。")
    text = (raw or "").strip()
    if text.startswith(("{", "```")):
        return _JSON_TRUNCATED_HINT
    return _JSON_RETRY_HINT


# ---- 返回 JSON 的结构审查（Pydantic）--------------------------------------------
# 【为什么解析成功之后还要再查一遍类型】形状对不上时下游一律【静默丢弃】：
# extract._merge_vision 的 isinstance(got, dict) 不成立就跳过该字段、vision.plan_skc
# 的非 dict 条目直接 continue、plan_desc 的 pos 类型不符也是 continue。表现全是
# 「字段空/条目少 + 没人报错」，与 2026-09-03 那次断尾（见 _JSON_TRUNCATED_HINT）
# 的下场一模一样——事后都分不清是模型没答对，还是压根没答。
# 故解析之后按模型再查一遍，不合格与解析失败【同等对待】：都重问，
# 别把脏结构往下游放，也别让它静默变成空值。
#
# 各判断点的期望结构用 Pydantic BaseModel 声明（项目本来就是 Pydantic v2，
# 不自造 schema 格式）：类型即文档、嵌套结构自动递归、报错还自带字段路径
# （rows.2.color 这种），拼进重试提示词正好能把话说到具体哪一个字段上。

# 必填字符串：空串与漏填等价（如阶段⑥ 的 image），都不带任何信息，故一并拦掉。
# 注意 dict/list 【不】要求非空——「这批图里没有尺码表」「一个颜色都没分出来」
# 都是合法结论，要求非空等于逼模型编。
NonEmptyStr = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]


def _check_result(data: dict, result_model: type[BaseModel]) -> str:
    """按 Pydantic 模型校验解析出来的 JSON，通过返回空串，否则返回一句中文错因。

    【校验用，不换掉返回值】调用方拿到的仍是原始 dict，不是 model 实例、也不是
    model_dump() 的产物，两个理由：
      1. 未声明的字段会被 model_dump 丢掉（extra 默认 ignore）。而条件字段恰恰
         都没声明——_merge_vision 要读的 sizeMeasurementsRows / sizeMeasurementsByPart
         / composition 全在此列，dump 一次就没了，等于修一个坑挖三个。
      2. 各判断点的下游全是 .get() 读 dict，换成实例要动每一处取值逻辑，
         而这次要修的是「模型答得不完整」，不是取值方式。
    模型一律配 strict=True，所以「校验通过的 dict」在已声明字段上与 dump 出来的
    等价，不存在放过去的值和校验的值不是一回事的问题——理由见下。

    【为什么各模型必须 strict=True】Pydantic 默认的 lax 模式会做隐式转换：
    2026-09-03 实测 pos 字段收到 "1"（字符串）和 true 都照样通过。而我们返回的是
    原 dict、里面还是那个字符串，下游 plan_desc 的 `pos not in valid_pos` 依旧
    不成立、依旧静默丢掉这条动作——校验通过了，坑一个没少填。strict 下这两种
    输入都被判错、进而触发重问，才是「让模型输出符合预期」而不是「我们私下把
    它转成符合预期」。后者掩盖问题，且转换后的值与返回的 dict 对不上。
    """
    try:
        result_model.model_validate(data)
        return ""
    except ValidationError as e:
        # 只取前 3 条：错误全量拼进提示词会很长，而模型一次能改对的也就头几个
        parts = []
        for err in e.errors()[:3]:
            loc = ".".join(str(x) for x in err["loc"]) or "(根)"
            parts.append(f"{loc}: {err['msg']}")
        return "；".join(parts)


async def ask_json(prompt: str, what: str = "判断", retries: int = 3,
                   stage: Optional[str] = None,
                   result_model: Optional[type[BaseModel]] = None) -> dict:
    """单发提问并要求返回 JSON 对象，解析后返回 dict。

    what 只用于日志和报错信息，方便一眼看出是哪个判断点出的问题。
    stage 是 LLM_STAGES 里的阶段 id，决定这次用哪个模型（见 get_llm）。

    【不传 response_format】项目的 LLM.ask 不接受该参数（它只透传
    max_tokens/temperature），硬塞会 TypeError。改为「提示词里明确要求 JSON +
    _parse_json 兜底解析」——后者能剥 ```json 围栏、也能从散文里抠出首个 {...}，
    采集侧的 judge_same_match 用的就是这条路，已在生产里跑住。
    所以调用方的 prompt 末尾务必写清「只输出JSON: {...}」。

    解析失败或结构不合格（result_model，见 _check_result）都重试 retries 次，仍失败
    才抛 RuntimeError：这些判断都在主流程上，返回个空 dict 会让调用方把错值写进表单，
    不如直接失败；但单次抖动（模型吐了非 JSON 的废文、或字段类型给错）不该直接
    搞挂整个阶段。result_model 不传就只查「能不能解析成 JSON」，与原行为一致。
    """
    last_raw, last_why = "", ""
    ask_prompt = prompt
    for attempt in range(1, retries + 1):
        raw = await get_llm(stage).ask(
            [Message.user_message(ask_prompt)], stream=False)
        last_raw = raw or ""
        data = _parse_json(raw)
        bad = ""
        if data is None:
            last_why = "JSON 解析失败"
        else:
            bad = _check_result(data, result_model) if result_model else ""
            if not bad:
                return data
            last_why = f"JSON 结构不合格（{bad}）"
        logger.warning(
            f"{what}：第 {attempt}/{retries} 次{last_why}，"
            f"原始响应（前 300 字）：{last_raw[:300]}"
        )
        # 【每次都从原始 prompt 重拼，不累加】累加会让三次下来同时挂上「写短点」和
        # 「别写废文」两条互相拉扯的提醒，模型照哪条都不对。
        ask_prompt = prompt + _retry_hint(last_raw, bad)
    raise RuntimeError(
        f"{what}：LLM 连续 {retries} 次{last_why}。"
        f"最后一次原始响应（前 200 字）：{last_raw[:200]}"
    )


def _mime_of(head: bytes) -> str:
    """按魔术字节判图片 mime。

    【为什么不看扩展名】extract.py 无论源图真实格式一律存成 .jpg（main-NN.jpg /
    desc-NN.jpg），扩展名不可信；PNG 字节套 image/jpeg 声明会被部分网关判为损坏图
    直接 400。远端下载的图同理——URL 后缀也可能与真实字节不符。
    """
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def image_ref(img: str) -> str:
    """把图片统一转成 data URL；已是 data URL 则原样返回。

    LLM.ask_with_images 只接受「URL 字符串」或 {"url": ...} 字典（见 app/llm.py 的
    images 分支），本地文件必须自己转 data URL，它不会替你读盘。

    【http(s) 外链也要下载转码，不能透传】2026-08-22 实测：Kimi 系端点（k3 /
    kimi-for-coding*）对任何远程图片 URL 一律 400 "unsupported image url"，只吃
    base64；同一张图转成 data URL 就 200。Packy grok 能收外链，但那是网关自己出网
    取图，成不成看它的出网环境（实测 wikimedia 图返回 image_download_error）。
    原先此处透传 http(s)，于是阶段⑬ 描述图（desc_map 抠出的 cbu01.alicdn.com 外链
    是唯一直接传远程 URL 的地方）在切到 Kimi 后必挂，而阶段⑥⑦ 传本地文件反而没事。
    统一在这里下载转码后，各端点行为一致，也不再依赖网关能否出网。

    下载走 extract._download_image（浏览器头 + 指数退避重试）：图片 CDN 对无
    UA/Referer 的裸请求做 bot 拦截，表现为连接重置或 403。

    【取不到图返回空串，由调用方滤掉】源站图被商家删掉时是 404，_download_image
    按「跳过」处理（返回 0 字节、不落盘）。此时这里不能继续读那个从没写成的临时
    文件——2026-09-02 实测（offer 654598552346 的 desc-01）：404 改成不抛之后，
    异常只是从 RuntimeError 变成 FileNotFoundError，⑬ 照样整品失败。一张图取不到
    不该让整个看图阶段挂掉，故返回空串。
    """
    if not img or img.startswith("data:"):
        return img
    if img.startswith(("http://", "https://")):
        # 延迟导入避开与 extract 的循环依赖（extract 里也用本模块的 ask_json_with_images）
        from app.publish.extract import _download_image

        with tempfile.TemporaryDirectory() as td:
            tmp = os.path.join(td, "remote.img")
            if not _download_image(img, tmp) or not os.path.exists(tmp):
                logger.warning(f"图片取不到，本次看图跳过该张：{img}")
                return ""
            with open(tmp, "rb") as f:
                raw = f.read()
        return f"data:{_mime_of(raw[:12])};base64," + base64.b64encode(raw).decode()
    with open(img, "rb") as f:
        raw = f.read()
    return f"data:{_mime_of(raw[:12])};base64," + base64.b64encode(raw).decode()


async def ask_json_with_images(
    prompt: str, images: list, what: str = "判断", system: Optional[str] = None,
    stage: Optional[str] = None, retries: int = 3,
    result_model: Optional[type[BaseModel]] = None
) -> dict:
    """带图单发提问并要求返回 JSON 对象，解析后返回 dict。

    images 里可以混着本地文件路径和 http(s) URL，一律过 image_ref 归一：调用方
    （如阶段① 视觉回填）拿到的就是 product-<offerId>/ 下的本地 jpg，不必自己转码。

    与 ask_json 的差异只有「多传图 + 可选 system」两点，JSON 解析、重试、报错措辞、
    「失败就抛不造兜底值」的取向完全一致——看图结论会一路带到属性/标题/选图，
    编个空 dict 出来比直接失败更糟。

    【为什么补上重试：原先一次不成就抛】2026-09-03 取证 offer 968804940018 阶段①：
    模型在 2049 completion token 处收笔，JSON 断在 imageUnderstanding 中间解析不出，
    于是 imageUnderstanding / sizeChart / sizeMeasurements / complianceNotes 四个
    字段全空，尺码表只好由阶段⑨ 凭空估算，错数值一路发上真店。同批下一个商品同样
    早停（1766 token），只因它 content 是【全空】、撞上 app/llm.py 的「返空就重发」
    保险才被救回——一个空、一个半截，同一个毛病两种下场，差的就是这里的重问。
    注意 app/llm.py 那两道保险都只认「content 为空」，半截正文在那一层是漏网的，
    而它也不该管：那层拿到的是裸字符串，判 JSON 完整性天然是本层的活。

    【重试不重算 refs】图片下载/转码只做一次，重发只换提示词：一次带图请求要重传
    整批 base64（阶段①最多 20 张），refs 重算等于再读一遍盘、再下一遍外链。
    额度一个字不动——那次断尾时 32000 的额度只用掉 6%，抬额度是给没用完的桶加高桶壁。

    【前提：模型要在 app/llm.py 的 MULTIMODAL_MODELS 白名单里】不在名单里
    ask_with_images 直接抛 ValueError（不是静默丢图）。当前 [llm.publish] 配的
    grok-4.6 已登记在册，换模型时记得同步登记。按阶段覆盖时 set_stage_choice
    已挡住给视觉阶段配非多模态模型，故这里拿到的必是能看图的那些。
    """
    llm = get_llm(stage)
    # 【空引用一律抛，不在这里静默丢】空串 = 那张图取不到（源站 404 等，见 image_ref）。
    # 这里不能替调用方"跳过该张"：多数看图提示词把图的【位次】当标识（阶段⑬ 按 pos
    # 逐张判动作、⑥⑦ 按序号归属颜色），少传一张会让其后每张的位次整体前移一位，
    # 模型的判断于是落到错误的图上——2026-09-02 实测（offer 654598552346）：28 张
    # 少传 1 张，⑬ 的 pos 16/19 拿到的是邻图的结论，两张超比例长图被判 keep 漏掉。
    # 静默错位比直接失败糟得多（前者把错数据发上真店），故交由调用方按自己的
    # 位次语义决定怎么剔除，见 vision.plan_desc。
    refs = [image_ref(i) if isinstance(i, str) else i for i in images]
    n_bad = sum(1 for r in refs if not r)
    if n_bad:
        raise RuntimeError(
            f"{what}：{n_bad}/{len(refs)} 张图取不到（源站图可能已失效）。"
            "调用方须先剔除取不到的图再重排位次，不能直接少传")
    last_raw, last_why = "", ""
    ask_prompt = prompt
    for attempt in range(1, retries + 1):
        logger.info(f"{what}：视觉判断，传图 {len(refs)} 张"
                    + (f"（第 {attempt}/{retries} 次）" if attempt > 1 else ""))
        raw = await llm.ask_with_images(
            messages=[Message.user_message(ask_prompt)],
            images=refs,
            system_msgs=[Message.system_message(system)] if system else None,
            stream=False,
            # 不指定则用配置段的 temperature：grok 与 deepseek 两档都配 0.0（已是下限，
            # 没有更低可调）；Kimi 只接受 1（2026-08-21 实测给 0.0 直接 400），写死
            # 0.0 会让 Kimi 视觉全挂。
            # 【温度 0 下重试仍然有效，靠的是换提示词而不是换采样】温度 0 是确定性
            # 采样，原样重问理论上会拿回一模一样的断尾正文；_retry_hint 每次改写
            # 提示词，这才是打破复现的手段——所以那几句提醒是功能，不是客套。
        )
        last_raw = raw or ""
        data = _parse_json(raw)
        bad = ""
        if data is None:
            last_why = "JSON 解析失败（多为正文断尾未闭合）"
        else:
            bad = _check_result(data, result_model) if result_model else ""
            if not bad:
                return data
            last_why = f"JSON 结构不合格（{bad}）"
        logger.warning(
            f"{what}：第 {attempt}/{retries} 次{last_why}，"
            f"原始响应（前 300 字）：{last_raw[:300]}"
        )
        ask_prompt = prompt + _retry_hint(last_raw, bad)
    raise RuntimeError(
        f"{what}：LLM 连续 {retries} 次{last_why}。"
        f"最后一次原始响应（前 200 字）：{last_raw[:200]}"
    )


async def ask_text(prompt: str, what: str = "判断",
                   stage: Optional[str] = None) -> str:
    """单发提问，返回原始文本（少数场景不需要 JSON 结构）。"""
    raw = await get_llm(stage).ask([Message.user_message(prompt)], stream=False)
    if not (raw or "").strip():
        raise RuntimeError(f"{what}：LLM 返回空响应")
    return raw
