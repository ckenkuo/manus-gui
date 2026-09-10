"""店小秘发布操作：titles。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import json
import re
from app.logger import logger
from app.publish.browser import BrowserSession, J
from typing import Optional


def _js_fill_by_label(label: str, value: str) -> str:
    """按 label 填 input/textarea（产品标题/英文标题等文本框）。返回 JS 代码字符串。"""
    return r"""(() => {
      const it = Array.from(document.querySelectorAll('.ant-form-item'))
        .find(el => {
          const l = el.querySelector('.ant-form-item-label label');
          return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__;
        });
      if (!it) return JSON.stringify({filled: false, reason: 'item-not-found'});
      const inp = it.querySelector('input:not([type=hidden]), textarea');
      if (!inp) return JSON.stringify({filled: false, reason: 'no-input'});
      const setter = Object.getOwnPropertyDescriptor(
        inp.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype,
        'value').set;
      setter.call(inp, __VALUE__);
      inp.dispatchEvent(new Event('input', {bubbles: true}));
      inp.dispatchEvent(new Event('change', {bubbles: true}));
      return JSON.stringify({filled: true, readback: inp.value});
    })()""".replace("__LABEL__", J(label)).replace("__VALUE__", J(value))


async def _set_origin(session: BrowserSession, country: str, province: str) -> dict:
    """填写产地（两级下拉：国家 + 省份）。产地字段有两个并列的 .ant-select。"""
    # 第一步：选国家
    js1 = r"""(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const it = Array.from(document.querySelectorAll('.ant-form-item'))
        .find(el => {
          const l = el.querySelector('.ant-form-item-label label');
          return l && (l.getAttribute('title')||l.textContent||'').trim() === '产地';
        });
      if (!it) return JSON.stringify({status: 'error', reason: 'item-not-found'});
      const selectors = it.querySelectorAll('.ant-select-selector');
      if (selectors.length < 1) return JSON.stringify({status: 'error', reason: 'no-select'});
      const sel1 = selectors[0];
      ['mousedown','mouseup','click'].forEach(t =>
        sel1.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
      await sleep(600);
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .filter(d => d.offsetHeight > 0);
      if (!drops.length) return JSON.stringify({status: 'error', reason: 'dropdown-not-open'});
      const opt = Array.from(drops[drops.length - 1].querySelectorAll('.ant-select-item-option'))
        .find(o => (o.textContent||'').trim() === __COUNTRY__);
      if (!opt) return JSON.stringify({status: 'error', reason: 'country-not-found'});
      opt.click();
      await sleep(800);
      const readback1 = it.querySelectorAll('.ant-select-selection-item')[0];
      return JSON.stringify({status: 'ok', country: readback1 ? readback1.textContent.trim() : null});
    })()""".replace("__COUNTRY__", J(country))
    r1 = await session.eval_json(js1)
    if r1.get("status") != "ok":
        return {"status": "error", "step": "country", **r1}

    # 第二步：等省份下拉出现，选省份
    await asyncio.sleep(0.8)
    js2 = r"""(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const it = Array.from(document.querySelectorAll('.ant-form-item'))
        .find(el => {
          const l = el.querySelector('.ant-form-item-label label');
          return l && (l.getAttribute('title')||l.textContent||'').trim() === '产地';
        });
      const selectors = it.querySelectorAll('.ant-select-selector');
      if (selectors.length < 2) return JSON.stringify({status: 'error', reason: 'province-select-not-appeared'});
      const sel2 = selectors[1];
      ['mousedown','mouseup','click'].forEach(t =>
        sel2.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
      await sleep(600);
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .filter(d => d.offsetHeight > 0);
      if (!drops.length) return JSON.stringify({status: 'error', reason: 'dropdown-not-open'});
      const opt = Array.from(drops[drops.length - 1].querySelectorAll('.ant-select-item-option'))
        .find(o => (o.textContent||'').trim() === __PROVINCE__);
      if (!opt) return JSON.stringify({status: 'error', reason: 'province-not-found'});
      opt.click();
      await sleep(400);
      const readback2 = it.querySelectorAll('.ant-select-selection-item')[1];
      return JSON.stringify({status: 'ok', province: readback2 ? readback2.textContent.trim() : null});
    })()""".replace("__PROVINCE__", J(province))
    r2 = await session.eval_json(js2)
    if r2.get("status") != "ok":
        return {"status": "error", "step": "province", "country": r1, **r2}

    return {"status": "ok", "country": r1.get("country"), "province": r2.get("province")}


def _strip_dated(text: str) -> str:
    """剥掉标题里的年份与「新款/新品」这类时效词。

    1688 源标题惯用「2026新款冬季…」，年份一旦进了 Temu 标题或尺码表模板名，
    次年就成了过期信息（商品生命周期跨年，标题却停在去年），用户 2026-08-24 明确
    要求去掉。这里只删年份与紧随的新款/新品/上新，不动季节词（冬季/加厚是真实卖点）。
    """
    text = str(text or "")
    # 「2026新款」「2026年新品」「20 春夏新款」等：年份 + 可选「年」+ 可选新款词
    text = re.sub(r"(19|20)\d{2}\s*年?\s*(新款|新品|上新)?", "", text)
    # 年份删掉后可能剩下光秃秃的「新款」，一并去掉
    text = re.sub(r"(新款|新品|上新)", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


# 明显不是品牌的词：1688 的「品牌」属性经常被卖家填成品类/功能描述（实测
# offer 971877978455 填的是「无功能保暖」），源标题开头也常是「儿童秋冬季…」这类
# 品类词。这些一旦进了品牌违禁词表，任何正常重写的中文标题都会被判「含品牌词」，
# 阶段⑤ 会连着两次重试全废（表现为 title-generation-failed，且看不出为什么）。
_NON_BRAND_WORDS = (
    "品牌", "其他", "其它", "无", "功能", "保暖", "加厚", "加绒", "抗菌", "德绒",
    "徳绒", "纯棉", "棉", "套装", "两件套", "三件套", "家居服", "睡衣", "秋衣",
    "秋裤", "内衣", "童装", "儿童", "宝宝", "男童", "女童", "男女", "通用",
    "春", "夏", "秋", "冬", "季", "款", "新", "版", "自有", "工厂", "代工",
)


def _brand_words(brand: str, src_title: str) -> list:
    """算出真正该拦的品牌违禁词。

    【为什么不再切源标题前 3 字】原实现把 `src_title[:3]` 无条件当品牌词。中文源标题
    开头绝大多数是品类+季节（「儿童秋」「女童冬」），这等于禁掉了中文标题必须出现的
    核心品类词——LLM 无论怎么重写都过不了闸，阶段⑤ 必然失败。改为只认源标题开头的
    拉丁字母商标 token（真商标才会用英文打头，如「MODAL 儿童…」），中文开头一律不猜。

    品牌属性值同样要过滤：剔掉 _NON_BRAND_WORDS 后若不剩 2 个字符以上的实词，就说明
    它是描述而不是品牌（「无功能保暖」→「无」→ 丢弃），不进违禁词表。
    """
    words = []
    b = (brand or "").strip()
    if b:
        core = b
        for w in _NON_BRAND_WORDS:
            core = core.replace(w, "")
        core = re.sub(r"[\s　（）()【】\[\]/、,，.。-]", "", core)
        if len(core) >= 2:
            words.append(b)
    m = re.match(r"[A-Za-z][A-Za-z0-9&.\-]{1,}", (src_title or "").strip())
    if m and len(m.group(0)) >= 3:
        words.append(m.group(0))
    return words


async def generate_titles(info: dict) -> dict:
    """只生成中英文标题，不碰页面。返回 {"status": "ok", "generated": {...}} 或 error。

    标题生成规则（实测沉淀）：
      - 英文标题 40-70 字符（确保手机端完整显示），纯 ASCII（禁 emoji/特殊符号）
      - 中文标题重写（不照抄源标题），突出卖点，≤60 字
      - 品牌红线：属性里的品牌值（过滤掉「无功能保暖」这类描述性假品牌）
        + 源标题开头的英文商标 token，见 _brand_words
      - 多候选兜底：一次生成 3 个英文标题，按推荐序取第一个合规的
    生成失败时最多重试一次（喂上次不合格的原因）；两次都不合格才报错。

    【为什么从 set_titles 里抽出来】它的输入只有 product-info.json 的字段
    （title/attributes/skus/imageUnderstanding），与店小秘页面无关，因此可以在
    ② 认领打开编辑页之前就先跑（见 service._run_prewarm）。抽的是【同一份实现】而不是
    另写一套：合规闸（品牌/年份/价格宣称、40-70 字符）都留在这里，预热与现场走的是
    完全相同的判定，否则两条路的产出会漂移。
    """
    from app.publish.llm import ask_json

    src_attrs = json.dumps(info.get("attributes", {}), ensure_ascii=False)
    img_sum = json.dumps(info.get("imageUnderstanding", {}), ensure_ascii=False)
    skus_sum = json.dumps(info.get("skus", {}), ensure_ascii=False)[:600]
    brand = (info.get("attributes") or {}).get("品牌", "")
    src_title = (info.get("title") or "").strip()
    # 来源平台中文名：标题提示词里的「源商品参数（1688）」标签按它填，
    # 避免非 1688 源（拼多多/Temu/亚马逊）还顶着「1688」误导模型
    from app.publish.workflows import source_name

    platform = source_name(info)
    # 品牌违禁词：过滤后的品牌属性值 + 源标题开头的英文商标（见 _brand_words 说明，
    # 不再无条件切前 3 字——那会把「儿童秋」这类品类词当品牌拦掉）
    forbidden = _brand_words(brand, src_title)
    if forbidden:
        logger.info(f"标题品牌违禁词：{forbidden}")

    def _has_brand(text: str) -> bool:
        low = text.lower()
        return any(f.lower() in low for f in forbidden)

    def _has_year(text: str) -> bool:
        """年份硬闸：提示词是软约束，模型照抄源标题里的「2026」是常态，
        必须在校验层拦。四位年份按 19xx/20xx 匹配；两位年份只认紧跟
        季节/品类词的形式（如 26FW/25AW），避免误伤尺码 24/26 这类数字。"""
        return bool(re.search(r"(19|20)\d{2}", text)
                    or re.search(r"\b\d{2}(FW|AW|SS|SP)\b", text, re.I)
                    or re.search(r"New Arrival|Latest|This Year", text, re.I))

    def _has_price_claim(text: str) -> bool:
        """价格/优惠宣称硬闸——虚假宣传与平台风控的高发项。

        阶段⑤（本函数）跑在阶段⑩定价之前，此刻真实售价还不存在：
        LLM 只看到 1688 人民币源价，据此折算出的美元价必然与最终
        申报价（默认 188.88 ÷ 7 ≈ 27 USD，见 DECLARE_PRICE_DEFAULT）不符。
        实测 offer 855602801145 生成 "Under 10USD" 而真实约 24USD（当时申报价
        168 的口径），差 3 倍；换成 188.88 后差得更多。故价格一律不许进标题。
        运费/折扣同理：都由平台活动决定，不是标题能承诺的。
        """
        pats = (
            r"\$\s*\d",                     # $9.99 / $ 9
            r"\d+\s*(USD|usd|dollars?)",     # 10USD / 10 dollars
            r"\bunder\s*\d",                # Under 10
            r"\b(cheap|cheapest|budget|bargain|lowest|affordable)\b",
            r"\b(sale|discount|deal|clearance|promo|coupon)\b",
            r"\d+\s*%\s*off|\boff\b\s*\d+\s*%",
            r"\bfree\s*(shipping|delivery|gift)\b",
            r"\b(buy\s*\d+\s*get|bogo)\b",
        )
        return any(re.search(p, text, re.I) for p in pats)

    def _has_subjective_claim(text: str) -> bool:
        """主观营销用语硬闸——Temu 明确禁止的绝对化/主观化表述。

        平台禁用词类型：最高级（Best/Perfect）、必备类（Must Have）、
        第一类（#1/Top/Leading）、完美类（Perfect/Flawless）等。
        中文同样拦截「好物/神器/必备/最好/第一/完美」等。
        """
        # 英文主观营销词
        en_pats = (
            r"\b(best|perfect|flawless|ultimate|ideal)\b",
            r"\b(must[\s-]?have|essential|necessary)\b",
            r"\b(top|leading|premier|superior|unbeatable)\b",
            r"\b(number[\s-]?one|#\s*1|no\.?\s*1)\b",
            r"\b(amazing|incredible|unbelievable|revolutionary)\b",
        )
        # 中文主观营销词
        zh_pats = r"(好物|神器|必备|最好|第一|完美|极致|顶级|首选|王牌)"
        return (any(re.search(p, text, re.I) for p in en_pats)
                or bool(re.search(zh_pats, text)))

    def _en_reject(en: str) -> Optional[str]:
        """英文标题不合格的具体原因；合格返回 None。

        【为什么要返回原因而不是布尔】两次重试都不合格时只能报
        title-generation-failed，日志里看不出到底是超长、含中文还是撞了品牌词——
        实测排查一次失败要去翻模型原始响应。返回原因后失败信息里直接带着，
        重试时也能把具体原因喂回模型（比笼统的「上次不合格」有效）。
        """
        if not en:
            return "空标题"
        if re.search(r"[一-鿿]", en):
            return "含中文字符"
        if not 40 <= len(en) <= 70:
            return f"长度 {len(en)} 不在 40-70"
        if not all(ord(ch) < 128 for ch in en):
            return "含非 ASCII 字符（emoji/特殊符号）"
        if _has_brand(en):
            return f"含品牌词 {forbidden}"
        if _has_year(en):
            return "含年份或时效词"
        if _has_price_claim(en):
            return "含价格/优惠宣称"
        if _has_subjective_claim(en):
            return "含主观营销用语"
        return None

    def _zh_reject(zh: str) -> Optional[str]:
        """中文标题不合格的具体原因；合格返回 None。"""
        if not zh:
            return "空标题"
        if zh == src_title:
            return "照抄源标题"
        if _has_brand(zh):
            return f"含品牌词 {forbidden}"
        if _has_year(zh) or re.search(r"新款|新品|上新", zh):
            return "含年份或新款等时效词"
        if _has_price_claim(zh) or re.search(
                r"包邮|免邮|特价|清仓|折扣|秒杀|亏本|甩卖", zh):
            return "含价格/优惠宣称"
        if _has_subjective_claim(zh):
            return "含主观营销用语"
        return None

    # 【候选数 3 不是 10】2026-08-24 耗时实测：原先要 10 个候选、每个还带 logic 字段，
    # 推理模型为 10 个候选各推演一遍，一次烧掉 15896 completion token / 2 分 13 秒——
    # 占当次批次全部 completion 的 64%，是整条管线最慢的单点。而下面的挑选逻辑只取
    # 第一个合规的，其余 9 个基本是废品。降到 3 个仍留够「首选不合规还有备选」的余量
    # （_en_ok 卡 40-70 字符 + 禁中文/品牌词，单个候选不合规是常态，故不能只要 1 个）。
    prompt = (
        "你是跨境电商商品标题优化师。你的目标只有两个：让搜索算法精准抓取获得曝光，\n"
        "让买家一眼看懂并愿意点击。请根据商品资料生成 3 个英文标题候选，并选出最合适的 1 个；\n"
        "同时生成 1 个自然、简洁、不可照抄源标题的中文标题。\n"
        "标题设计与关键词布局（按商品实际情况取舍，禁止硬塞无关词）：\n"
        "1. 通用公式：品牌/自主标（仅在明确合规时；本流程会统一移除品牌词） + 核心大词 + 属性/材质词 +\n"
        "   卖点/风格词 + 适用场景/受众 + 规格/型号。任何疑似第三方或未授权商标必须删除。\n"
        "2. 核心大词（类目词）必须前置或位于前半段，例如 Bluetooth Earbuds、Plush Toy、Denim Vest。\n"
        "3. 长尾词用于区分竞品：只使用资料中能证实的材质、功能、风格、尺寸、数量和型号。\n"
        "4. 明确人群与场景（如 kids、girls、camping、birthday gift），避免引入不准确的流量。\n"
        "5. 有套装件数、尺寸、容量、接口、型号等参数时必须保留；没有证据的参数不得推测。\n"
        "6. 搜索型平台（Amazon/淘宝/京东）优先采用「品牌（如有）+核心词+卖点+材质/参数+人群/场景」；\n"
        "   Temu/拼多多等推荐型平台采用「强卖点+核心词+属性词+适用对象」，紧凑直白，前 30-40 个字符\n"
        "   放核心词和最大卖点。当前来源平台为：" + platform + "。\n"
        "7. 拒绝关键词堆砌：使用自然语序、空格和连字符，标题必须读起来像商品名称。\n"
        "8. 只描述客观事实，禁止夸大或无法验证的词：Best、Perfect、Must Have、Top、#1、Amazing、\n"
        "   Incredible、最高级、全网第一、最好、必买等；禁止价格、折扣、包邮、清仓等承诺。\n"
        "9. 禁止年份及短期时效词（2025/2026/25/26、New Arrival、Latest、This Year、新款、新品、上新）；\n"
        "   Winter/Fall 等真实季节属性可以保留。禁止 Emoji、商标蹭词和特殊符号。\n"
        "10. 英文标题只允许 ASCII 字母、数字、空格和连字符，长度严格 40-70 个字符（含空格）。\n"
        "标题结构公式（推荐按商品类型选用）：\n"
        "A 基础型：[核心品类] + [关键属性] + [材质/功能] + [适用场景/人群]\n"
        "   示例：Adjustable Pet Harness for Small Dogs, Breathable Mesh, Daily Walking\n"
        "B 套装型：[数量] + [核心品类] + [关键属性] + [材质/功能] + [适用人群]\n"
        "   示例：2 Pcs Kitchen Storage Containers, Airtight Seal, BPA Free, For Home Use\n"
        "C 多功能型：[核心品类] + [多个功能点] + [材质] + [场景]\n"
        "   示例：Waterproof Phone Holder for Car, Adjustable Angle, Dashboard Mount\n"
        "分析时必须结合商品标题、源商品参数、SKU 信息和图片理解摘要，先提炼品类、核心词、属性、卖点、\n"
        "人群、场景和规格，再组织标题；任何资料没有明确支持的信息都不要添加。\n"
        f"品牌红线：品牌名严禁出现在任何标题中（本商品品牌为 {brand or '无'}，\n"
        f"源标题前几个字符也可能是品牌词，一律剔除）。\n"
        "中文标题要求：去掉品牌名，不得照抄源标题——重新组织关键词和语序，突出本商品实际卖点\n"
        "（从源参数/图片理解中提炼，如材质/套装件数/风格/季节/装饰元素等），通顺简洁≤60字；\n"
        "同样严禁年份和「新款/新品/上新」这类时效词（季节词可留）。\n"
        # 不要 logic 字段：它只进日志不进表单，却让模型为每个候选多写一段推演
        "只输出 JSON：{\"candidates\": [{\"enTitle\": \"...\"}...共3个],\n"
        "\"recommend\": <0-2 的序号，选最贴合商品且合规的>, \"title\": \"<重写后的中文标题>\"}\n\n"
        f"商品标题：{src_title}\n"
        f"源商品参数（{platform}）：{src_attrs}\n"
        f"SKU 价格：{skus_sum}\n"
        f"图片理解摘要：{img_sum}"
    )
    titles, gen_err, last_reasons = None, None, ""
    for attempt in (1, 2):  # 英文标题含中文等不合格时重生成一次
        try:
            # 重试时喂上一轮的具体拒因（哪个候选因为什么被拒），比原先笼统罗列所有
            # 可能原因有效得多——模型不必猜自己到底犯了哪条。
            hint = ""
            if attempt > 1 and last_reasons:
                hint = ("\n\n上次输出不合格，逐条原因：" + last_reasons
                        + "\n请针对上述原因逐一修正后重新生成。")
            t = await ask_json(prompt + hint, what="标题生成", stage="titles")
            cands = t.get("candidates", [])
            rec = t.get("recommend", 0)
            # 按推荐优先、其余顺序兜底，取第一个合规的
            order = [rec] + [i for i in range(len(cands)) if i != rec]
            picked, reasons = None, []
            for i in order:
                if 0 <= i < len(cands):
                    en = (cands[i].get("enTitle") or "").strip()
                    why = _en_reject(en)
                    if why is None:
                        picked = {"enTitle": en, "logic": cands[i].get("logic", "")}
                        break
                    reasons.append(f"英文候选{i}「{en[:60]}」{why}")
            zh = (t.get("title") or "").strip()
            zh_why = _zh_reject(zh)
            if zh_why is not None:
                reasons.append(f"中文标题「{zh[:40]}」{zh_why}")
                zh = None
            if picked and zh:
                titles = {"title": zh, "enTitle": picked["enTitle"],
                          "logic": picked["logic"],
                          "candidateCount": len(cands),
                          "allCandidates": [c.get("enTitle") for c in cands]}
                break
            last_reasons = "；".join(reasons)
            logger.warning(f"标题第 {attempt}/2 次校验未通过：{last_reasons}")
            gen_err = "校验未通过: " + last_reasons[:300]
        except Exception as e:
            gen_err = str(e)
    if not titles:
        return {"status": "error", "reason": "title-generation-failed", "err": gen_err}
    return {"status": "ok", "generated": titles}


async def set_titles(session: BrowserSession, info_path: str,
                     generated: Optional[dict] = None) -> dict:
    """阶段⑤：把中英文标题填进表单；产地一律填广东省。

    generated 给了就直接用（service 层的提前预热产物，见 _run_prewarm），否则现场
    调 generate_titles 生成。两条路进来的都是同一个函数的产物、过的是同一套合规闸，
    故此处不再复检——复检一遍等于把闸的判据抄第二份，两份迟早漂移。
    """
    if generated is None:
        with open(info_path, encoding="utf-8") as f:
            info = json.load(f)
        gen = await generate_titles(info)
        if gen.get("status") != "ok":
            return gen
        titles = gen["generated"]
    else:
        titles = generated

    result = {"status": "ok", "generated": titles}
    # 填写中文标题
    r1 = await session.eval_json(_js_fill_by_label("产品标题", titles["title"]))
    result["title"] = r1
    # 填写英文标题
    r2 = await session.eval_json(_js_fill_by_label("英文标题", titles["enTitle"]))
    result["enTitle"] = r2
    ok = (r1.get("filled") and r1.get("readback") == titles["title"] and
          r2.get("filled") and r2.get("readback") == titles["enTitle"])
    # 产地固定填「中国大陆 → 广东省」（两级下拉：先选国家，省份下拉才会动态出现）。
    # 【别改成读源商品的产地】2026-08-24 用户明确：本店是**从 1688 采购再由广东仓
    # 发货**的半托管模式，Temu 这里要的是**实际发货地**，不是 1688 卖家所在地。
    # 源属性里的「产地」（浙江织里/福建石狮等）填进来反而是申报不实。
    # 曾按「源产地抓了却没用」把它改成读源值，方向错了，已回退。
    await asyncio.sleep(0.5)
    origin = await _set_origin(session, "中国大陆", "广东省")
    result["origin"] = origin
    if origin.get("status") != "ok":
        ok = False
    result["status"] = "ok" if ok else "error"
    return result
