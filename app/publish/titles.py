"""店小秘发布操作：titles。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import json
import re
from app.logger import logger
from app.publish import claims
from app.publish.browser import BrowserSession, J
from typing import Optional


# 夸大宣传词表与品类词掩码已搬到 app/publish/claims.py：那套判据图片链路（英化提示词、
# 英化质检、keep 图复核）也要用，各留一份必然漂移。此处只保留标题侧的调用。
# _TOP_GARMENT_RE 原先只掩服装类的「修饰词 + Top」，搬过去后并入 claims._TOP_NOUN_RE，
# 顺带补上 Table Top / Counter Top 这类部件名。


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


# 标题年龄硬闸的判据（2026-09-19 用户要求「标题不填年龄」）。拦的是**具体年龄数值/
# 月龄**，不是人群词：kids / children / baby / toddler 是搜索大词，提示词第 4 条本就
# 要求写人群场景；平台上的适用年龄有属性字段与尺码表（阶段⑥⑧）负责，标题再写一遍
# 只会多一处与实际适用年龄对不上的地方。
_AGE_RE = (
    r"\bage[sd]?\s*\d",                                    # Age 3+ / Ages 3-6
    r"\b\d+\s*(?:[-–~]|to)\s*\d+\s*"                       # 3-6 Years / 0-12M / 2-4T
    r"(?:years?|yrs?|y|months?|mos?|m|t)\b",
    r"\b\d+\s*(?:years?|yrs?)\s*(?:old|and up|plus)\b",     # 3 Years Old / 3 Years and up
    r"\bfor\s+\d+\s*(?:\+|and\s+up\b)",                     # for 3+ / for 3 and up
    r"\b\d+\s*months?\b",                                   # 6 Months
    r"\b\d+\s*t\b",                                         # 2T / 3T（童装码）
    r"\d+\s*[-–~至到]\s*\d+\s*岁",                           # 3-6岁
    r"\d+\s*岁",                                            # 3岁 / 1岁半
    r"\d+\s*个月",                                          # 6个月 / 0-12个月
    r"\d+\s*月龄",                                          # 12月龄
)


def _has_age(text: str) -> bool:
    """标题里出现具体年龄数值/月龄就返回 True（判不合格）。

    【为什么裸 "3 Years" 不拦】电子产品/家居标题常把保修写成 "1 Year Warranty"，
    按「数字 + Years」一刀切会误伤；标题里真正会出现的年龄写法是范围（3-6 Years）、
    "3 Years Old"、"Age 3+"、"for 3+"，这几类都已覆盖。误伤的代价不只是拒一个候选——
    两次生成都撞上就会整个阶段失败。月份那类按年计价的保修少，故 "6 Months" 直接拦。
    【为什么 "2T/3T" 要拦】童装码本身就是年龄码，与「3-6 Years」等价。
    """
    t = str(text or "")
    return any(re.search(p, t, re.I) for p in _AGE_RE)


# ---- 标题码数硬闸（2026-10-09 用户定案「标题不注明码数」）------------------------
# 为什么码数不能进标题：尺码由 SKU 与尺码表承载，标题写死某个码（尤其带型号的
# 「Size 24」）会与整条链接的实际尺码范围对不上，属于商品信息不实。
# 材质那条硬闸见下面 _material_reject 上方的说明：词表复用 claims.material_hits。
#
# 【为什么 "Small/Medium/Large" 单独出现【不】拦】英文标题里 "for Small Dogs"、
# "Large Capacity" 是高频正常写法，按词拦会大面积误伤——而误伤的代价是整条标题
# 两次被拒、阶段⑤ 直接失败。故这三个词只在紧跟 Size 时才认（Size M / Size Large）。
_SIZE_EN_RE = re.compile(
    # 字母码。单字母前面要卡一个字符：`\b[sml]\b` 会命中 "Women's" 里的 s
    # （撇号是非单词字符，s 两侧都成了词边界），那是所有格不是尺码。
    r"(?<![A-Za-z'’])\b(?:xxs|xs|s|m|l|xl|xxl|xxxl|[2-9]xl)\b"
    # Size M / Size 24 / Size XL——【必须跟具体尺码值】，否则 "Queen Size Bed"、
    # "Size Chart" 这类正常写法会被误伤
    r"|\bsize\s*[:#]?\s*(?:\d{1,3}|xxs|xs|s|m|l|xl|xxl|xxxl|[2-9]xl)\b"
    r"|\b(?:plus|one|free)\s+size\b",
    re.I)

_SIZE_ZH_RE = re.compile(
    # 中文码数写法。单字母码必须带「码」字（S码/M码），因为中文标题里的裸拉丁字母
    # 常常是别的意思（USB/LED/PU）；XL 及以上歧义小，裸写也认。
    r"均码|加大码|特大码|大码|中码|小码|[0-9]{1,3}\s*码"
    r"|(?:XXS|XS|XXL|XXXL|[2-9]XL|XL)(?![A-Za-z])"
    r"|[SML]\s*码",
    re.I)


def _size_hits(text: str) -> list:
    """标题里出现的尺码码数（去重、保序），供拒因与日志。

    两个正则都要跑：中文标题常混着 XL 这类拉丁码，英文标题也可能整段带中文。
    与 claims.material_hits 同一形状（返回命中的原文而不是布尔），理由是同一件事——
    拒因要带上具体命中的词，模型才知道改哪儿，排查也不必去翻原始响应。
    """
    s = str(text or "")
    if not s:
        return []
    out = []
    for r in (_SIZE_EN_RE, _SIZE_ZH_RE):
        for m in r.finditer(s):
            w = m.group(0).strip()
            if w and w.lower() not in {x.lower() for x in out}:
                out.append(w)
    return out


# 材质词的判据不走本模块，复用 claims.material_hits——全管线只留一份材质词表。
#
# 【为什么必须挡在标题外】平台对图片侧的原话是「含棉材质宣传，但属性中材质描述与该
# 材质不匹配」，同一个矛盾在标题上照样成立：标题写 Cotton 而属性材质填聚酯纤维就是
# 一次材质宣传不实。此前只修了图片侧（claims.MARK_REMOVE_RULE），文本侧一直留着不挡，
# 由本次改动补上。
#
# 【为什么复用而不是另写一份】那套正则已经把三个坑填过了（英文卡 \b 词边界、中文单字
# 「棉/涤/毛/麻/丝/绒」只在「数字%」或「面料/成分/材质」上下文才认，避免把「棉签」
# 「毛绒玩具」判成材质）。照抄一份必然与原版漂移，而项目里「同一概念两份词表」的账
# 已经吃过多次。代价是 claims 那份清单同时服务图片侧质检：【扩词表会同时影响两侧】，
# 这是有意的——材质在管线里只该有一个口径。
#
# 【有意不拦的边界】外观/结构词与品类词都不算材质宣称：Mesh、Denim、Plush、Pattern、
# Knit 这类可以照旧写；「毛绒玩具」「针织衫」「棉服」是品类名（单字「棉」只在
# 「100% 棉」这类形态才认）。这与 claims._MATERIAL_EN_RE 的取向一致，见那里的说明。


def _material_hits(text: str) -> list:
    """标题里出现的材质/成分词。判据是 claims.material_hits 那一份（见上方说明）。"""
    return claims.material_hits(text)


def _material_reject(text: str) -> Optional[str]:
    """材质硬闸：命中返回带具体词的拒因，合规返回 None。中英文标题共用。

    【拒因必须带改法】同 _banned_reject：只说「不许写材质」的话，模型会把材质词删掉
    却不补词，标题长度掉出 40-70 又被长度闸拒——两次下来阶段⑤ 直接失败。故要说清
    拿什么补位（功能、风格、场景词）。
    """
    hits = _material_hits(text)
    if not hits:
        return None
    return (f"含材质/成分词 {hits[:4]}——标题不写材质：材质由平台属性与成分字段声明，"
            f"标题再写一遍只会与属性对不上（平台判「材质宣传与属性材质不匹配」）。"
            f"删掉材质词，改用功能、风格、场景词补位")


def _size_reject(text: str) -> Optional[str]:
    """码数硬闸：命中返回带具体词的拒因，合规返回 None。中英文标题共用。

    改法同 _material_reject：删掉码数后要用人群、场景、功能词把长度补回来。
    """
    hits = _size_hits(text)
    if not hits:
        return None
    return (f"含尺码码数 {hits[:4]}——标题不注明码数：尺码由 SKU 与尺码表承载，"
            f"标题写死某个码会与整条链接的实际尺码范围对不上。"
            f"删掉码数，改用人群、场景、功能词补位")


def _title_content_reject(text: str) -> Optional[str]:
    """续跑判据用：页面上这个英文标题还过得了内容闸吗（不过就该重跑 ⑤）。

    【为什么「框里有值」不等于「⑤ 的成果还在」】⑤ 的续跑判据原先只看「非空 + 无中文」
    （navigation._JS_LIVE_STATE 的 titleFilled/titleHasCjk），前提假定是那个框里的值
    只有两个来源：⑤ 填的，或认领把【中文】源标题预填进去的。2026-10-09 真站实测
    （草稿 184807703163093667）证实【认领确实会把源标题原样预填进英文标题框】——对中文源
    这个假定成立，但源标题若是拉丁字母（亚马逊等英文源），预填值非空且无中文，⑤ 会被判
    「成果还在」直接跳过，源标题原样进发布。
    另一半是旧产物：2026-10-09 之前生成的标题允许写材质，页面上留着（同日实测草稿
    184807703163545945 的英文标题就是「…Washed Blue Polyester Pants…」），续跑一样跳过。

    【为什么只查材质与码数，不把 ⑤ 的全部闸搬过来】这两条正是本次新增的红线，也正是
    旧产物与源标题预填会带的东西；其余判据要么需要 info 上下文（品牌闸要品牌词表、
    照抄源标题那条要源标题原文），不是纯内容判据，要么判的是生成质量而非内容违规。
    搬全部闸等于把 _en_reject 拆成两半，收益不抵那份复杂度。
    """
    t = str(text or "")
    return _material_reject(t) or _size_reject(t)


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
      - 年龄红线：不出现具体年龄数值/月龄（3-6 Years、2T、3-6岁、6个月），
        人群词（kids/儿童/男童）可写，见 _has_age
      - 材质红线：不写材质/成分（棉/涤纶/Cotton/Polyester），材质由属性与成分
        字段声明，标题再写一遍只会与属性对不上，见 _material_hits
      - 码数红线：不写尺码（S/M/L/XL、均码、Size 24），尺码由 SKU 与尺码表承载，
        标题写死某个码会与整条链接的实际尺码范围对不上，见 _size_hits
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

    def _claim_reject(text: str) -> Optional[str]:
        """夸大宣传硬闸：命中返回带具体词的拒因，合规返回 None。

        判据走 claims.has_marketing_claim（全管线共用词表）。原先这里内联一份
        只覆盖最高级/必备/第一三类的词表，销量与排名类（BEST-SELLER、Hot Sale、
        爆款、销量第一）压根不在其中——那正是平台罚得最实的一类。

        【拒因要带上命中的词】两次生成都不合格时只报「含主观营销用语」，日志里看不出
        撞的是哪个词，重试提示词也没法把它喂回模型（同本函数外层 last_reasons 的取向）。
        """
        hits = claims.hit_words(text)
        return f"含夸大宣传用语 {hits[:4]}" if hits else None

    def _banned_reject(text: str) -> Optional[str]:
        """平台禁词硬闸（安抚 / PP棉 / 环保声明）：命中返回带具体词的拒因，合规返回 None。

        判据走 claims.has_banned_term（与图片链路同一份词表，中英文都查）。与
        _claim_reject 分开是因为两类的修正方向不同：夸大宣传要「删掉这个形容词」，禁词要
        【换一种说法】（安抚玩偶→Plush Companion Doll、PP棉→改说填充饱满），拒因里必须
        说出改法，否则模型只会把词删掉、把商品说成别的东西。
        【PP棉的改法 2026-10-09 由「改成聚酯纤维」改成「不点材质」】原因同提示词 8b：
        标题现已禁写材质，再让模型把 PP棉 改成聚酯纤维，等于喂它撞材质闸。
        【环保声明是反例：只许删、不许换说法】它 2026-09-25 并进禁词表，但处置与另外两类
        不同——任何环保声明平台都不接受，换成别的环保说法同样违规，故拒因里必须说清楚
        「删掉这个概念」而不是「换个同义写法」，否则模型会写出 Sustainable 替 Eco-Friendly。
        """
        hits = claims.banned_hits(text)
        if not hits:
            return None
        return (f"含平台禁词 {hits[:4]}——这类词禁止出现：安抚/PP棉要换成同义的规范写法"
                f"（安抚玩偶→Plush Companion Doll/Comfort Plush Toy/毛绒公仔、"
                f"PP棉→改说填充饱满/Softly Stuffed，【不要换成聚酯纤维/Polyester Fiber，"
                f"标题禁写材质】），不要只删掉词；"
                f"环保声明（Eco-Friendly/Sustainable/环保/可降解这类）则【整句删掉、"
                f"不要用别的环保说法替代】，直接不提环保这件事")

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
        if _has_age(en):
            return "含年龄数值（适用年龄由属性字段声明，标题只写人群词）"
        # 材质/码数紧挨着年龄放：这四类同源，都是「标题重复声明了本该由属性/SKU
        # 字段承载的产品属性」，改法也同源——删掉那个词，换别的词把长度补回来。
        why = _material_reject(en) or _size_reject(en)
        if why:
            return why
        if _has_price_claim(en):
            return "含价格/优惠宣称"
        return _claim_reject(en) or _banned_reject(en)

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
        if _has_age(zh):
            return "含年龄数值（适用年龄由属性字段声明，标题只写人群词）"
        # 材质/码数与英文侧同一口径（见 _material_reject / _size_reject）：中文标题
        # 里「纯棉」「加厚针织」这类写法同样是材质宣称，两侧都拦，不做例外。
        why = _material_reject(zh) or _size_reject(zh)
        if why:
            return why
        if _has_price_claim(zh) or re.search(
                r"包邮|免邮|特价|清仓|折扣|秒杀|亏本|甩卖|超值|白菜价", zh):
            return "含价格/优惠宣称"
        # 中文标题同样过禁词闸：「安抚」「PP棉」中英文两侧都拦（与睡眠类词不同，
        # 它们没有正当的品类用法，换成规范写法即可，不会把品类词拦死）。
        return _claim_reject(zh) or _banned_reject(zh)

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
        "1. 通用公式：品牌/自主标（仅在明确合规时；本流程会统一移除品牌词） + 核心大词 +\n"
        "   属性/风格词 + 卖点词 + 适用场景/受众 + 数量/容量等客观参数。任何疑似第三方或\n"
        "   未授权商标必须删除。\n"
        "2. 核心大词（类目词）必须前置或位于前半段，例如 Bluetooth Earbuds、Plush Toy、Denim Vest。\n"
        "3. 长尾词用于区分竞品：只使用资料中能证实的风格、功能、数量、容量和适用场景。\n"
        "4. 明确人群与场景（如 kids、girls、camping、birthday gift），避免引入不准确的流量。\n"
        "5. 有套装件数、容量、接口等客观参数时必须保留；没有证据的参数不得推测。\n"
        "6. 搜索型平台（Amazon/淘宝/京东）优先采用「品牌（如有）+核心词+卖点+参数+人群/场景」；\n"
        "   Temu/拼多多等推荐型平台采用「强卖点+核心词+属性词+适用对象」，紧凑直白，前 30-40 个字符\n"
        "   放核心词和最大卖点。当前来源平台为：" + platform + "。\n"
        "7. 拒绝关键词堆砌：使用自然语序、空格和连字符，标题必须读起来像商品名称。\n"
        "8. 只描述客观事实，禁止夸大或无法验证的词，以下几类一个都不许出现：\n"
        "   - 销量/排名：Best Seller、BEST-SELLER、Hot Sale、Top Rated、#1、Trending、\n"
        "     Viral、1000+ Sold、爆款、热卖、畅销、销量第一；\n"
        "   - 最高级/绝对化：Best、Perfect、Top Quality、Premium、Luxury、Ultimate、\n"
        "     Unbeatable、最好、最佳、完美、顶级、极致、独一无二、全网第一；\n"
        "   - 必备/必买：Must Have、Essential、必备、必买、神器、好物；\n"
        "   - 情绪夸大与保证：Amazing、Incredible、Stunning、Guaranteed、100% Satisfaction、\n"
        "     震撼、逆天、保证效果；\n"
        "   禁止价格、折扣、包邮、清仓等承诺。品类名里的词不受此限（Tank Top、Essential Oil\n"
        "   这类搭配中的 Top/Essential 是品类词，可以正常使用）。\n"
        # 【这条必须写进提示词，不能只靠校验层拦】「安抚」「PP棉」常是源标题的核心词
        # （安抚玩偶、PP棉填充），只加硬闸的话模型两次都会照写、两次都被拒，阶段⑤ 直接
        # title-generation-failed。故要给【替代写法】而不是只说「不许写」（同规则 11 对
        # 年龄那条的取向：说清用什么补位，模型才有的可写）。
        # 【中英文标题都受约束】这两类词没有正当的品类用法，换成规范写法即可，不像睡眠词
        # 那样会把品类词拦死，故两侧同一口径。
        "8b. 禁词红线（中文标题与英文标题都不许出现，无论源商品怎么写）：\n"
        "   - 「安抚」类用途描述：安抚、安抚玩偶、安抚巾、Soothing、Soother、Comforter；\n"
        "   - 「PP棉」类填充物俗称：PP棉、pp棉、聚丙烯棉、PP Cotton。\n"
        "   这类词【换说法、不是删掉】——商品该表达的意思要用规范写法写出来：\n"
        "     安抚玩偶/安抚公仔 → 英文 Plush Companion Doll、Comfort Plush Toy；\n"
        "                          中文 毛绒公仔、毛绒玩偶、陪伴玩偶；\n"
        # 【PP棉的替代写法 2026-10-09 改过：原先是「改成规范材质名聚酯纤维/Polyester
        # Fiber」，但第 12 条现在禁止标题写材质，两条并存就是死结——模型照 8b 改成
        # 聚酯纤维、立刻被材质闸拒，两次下来阶段⑤ 必失败。故替代写法一律不点材质。
        "     PP棉填充 → 英文 Softly Stuffed、Plush；中文 填充饱满、柔软填充，\n"
        "                 或者干脆不提填充物。【不要换成聚酯纤维/Polyester Fiber 这类\n"
        "                 材质名——第 12 条禁止标题写材质】。\n"
        "   注意 Comfort（舒适）不是禁词，可以正常用，只有 Comforter 才算。\n"
        "   注意「聚酯纤维」「聚丙烯纤维/丙纶」虽然是规范材质名，但标题里照样不许写，\n"
        "   见第 12 条（材质整体不进标题，不分规范不规范）。\n"
        "   - 环保声明：【整句删掉、直接不提环保这件事】，不要用别的环保说法替代——\n"
        "     任何环保声明平台都不接受，换一个词同样违规。含 eco-friendly、\n"
        "     environmental friendly、planet-friendly、sustainable、biodegradable、\n"
        "     recycled、carbon neutral、zero waste、环保、可持续、可降解、可回收、\n"
        "     生态友好 这类说法的表述一律不要写。注意 Green（绿色）、Natural（自然色）\n"
        "     是普通颜色与客观描述，可以正常用。\n"
        "9. 禁止年份及短期时效词（2025/2026/25/26、New Arrival、Latest、This Year、新款、新品、上新）；\n"
        "   Winter/Fall 等真实季节属性可以保留。禁止 Emoji、商标蹭词和特殊符号。\n"
        "10. 英文标题只允许 ASCII 字母、数字、空格和连字符，长度严格 40-70 个字符（含空格）。\n"
        "11. 禁止出现任何年龄数值或月龄（Age 3+、Ages 3-6、3-6 Years、2-4T、6 Months、2T、\n"
        "   3-6岁、6个月等）：适用年龄由平台属性字段声明，标题写了只会与实际适用年龄对不上。\n"
        "   人群词 kids / children / baby / toddler / 儿童 / 男童 可以正常写，但不要带岁数；\n"
        "   需要凑长度时用功能、风格、场景补，不要用年龄。\n"
        # 【同 8b：必须写进提示词，不能只靠校验层拦】源标题几乎条条带材质（1688 惯用
        # 「纯棉/德绒/加厚针织」），只加硬闸的话模型会照写、两次都被拒，阶段⑤ 直接
        # title-generation-failed。故这里要同时说清【拿什么补位】，模型才有得可写。
        "12. 标题不写材质、不写码数（中文标题与英文标题都不许出现，无论源商品怎么写）：\n"
        "   - 材质/成分：棉、纯棉、涤纶、聚酯纤维、尼龙、羊毛、真丝、亚麻、莫代尔、\n"
        "     Cotton、Polyester、Nylon、Wool、Silk、Linen、Spandex 这类成分宣称一个都不要写。\n"
        "     材质由平台属性与成分字段声明，标题再写一遍只会与属性对不上（平台对图上同类\n"
        "     问题判的就是「材质宣传与属性材质不匹配」）。源标题里的材质词要主动丢掉。\n"
        "   - 码数：S/M/L/XL/XXL/2XL、均码、加大码、Size M、Size 24 这类尺码标识都不要写。\n"
        "     尺码由 SKU 与尺码表承载，标题写死某个码会与整条链接的实际尺码范围对不上。\n"
        "   - 【外观与品类词不受限】Mesh、Denim、Plush、Pattern 这类外观/结构词照旧可用；\n"
        "     「毛绒玩具」「针织衫」「棉服」是品类名，也照旧可用；「加绒」「加厚」是工艺\n"
        "     描述，同样可以写。\n"
        "   - 少了材质和码数这两个占位，用功能、风格、场景、人群、数量词把长度补回来。\n"
        "标题结构公式（推荐按商品类型选用）：\n"
        "A 基础型：[核心品类] + [关键属性] + [功能/风格] + [适用场景/人群]\n"
        "   示例：Adjustable Pet Harness for Small Dogs, Padded Design, Daily Walking\n"
        "B 套装型：[数量] + [核心品类] + [关键属性] + [功能] + [适用人群]\n"
        "   示例：2 Pcs Kitchen Storage Containers, Airtight Seal, For Home Use\n"
        "C 多功能型：[核心品类] + [多个功能点] + [风格] + [场景]\n"
        "   示例：Waterproof Phone Holder for Car, Adjustable Angle, Dashboard Mount\n"
        "分析时必须结合商品标题、源商品参数、SKU 信息和图片理解摘要，先提炼品类、核心词、属性、卖点、\n"
        "人群、场景和数量，再组织标题；任何资料没有明确支持的信息都不要添加。\n"
        f"品牌红线：品牌名严禁出现在任何标题中（本商品品牌为 {brand or '无'}，\n"
        f"源标题前几个字符也可能是品牌词，一律剔除）。\n"
        "中文标题要求：去掉品牌名，不得照抄源标题——重新组织关键词和语序，突出本商品实际卖点\n"
        "（从源参数/图片理解中提炼，如套装件数/风格/季节/装饰元素等），通顺简洁≤60字；\n"
        "同样严禁年份和「新款/新品/上新」这类时效词（季节词可留），也严禁年龄数值/月龄\n"
        "（如 3-6岁、6个月），人群词（儿童/男童/宝宝）可保留但不带岁数；\n"
        "同样不许写材质与码数（见第 12 条）——源标题里的「纯棉」「加厚针织」「2XL」都要丢掉。\n"
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
