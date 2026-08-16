"""确定性采集管道（阶段二核心降本）：把第二段的 agent 自由循环，替换为
逐商品的确定性例程 + 2 次单发大模型判断。

为什么要它：agent 自由循环每步 1 次大模型、历史零裁剪全量重发，单商品烧十万级
token、往返 7–20 次。本管道把单商品大模型往返压到 **2 次**（视觉挑同款 + 文本读价），
每次输入小、无历史累积；浏览器操作全部确定性，杜绝"错误丢回大模型让它瞎试"。

单商品流程（复用已登录 Chrome 的单个 page，全程不新开 agent）：
    [脚本] 1. requests 下载主图（调用方已下好则跳过）
    [脚本] 2. paste_image 以图搜图 → 落地 s.1688.com 结果页
    [脚本] 3. read_search_results：从 .search-offer-wrapper 卡片抽
             {offerId, detailUrl, title, img, priceText}
    [LLM×1] 4. 判断点A（视觉）：目标主图 + 候选缩略图 → 选出同款的 offerId
    [脚本] 5. 打开 detail.1688.com/offer/<id>.html；抓 .module-od-main-price
             / .item-price-stock 价格+规格文本
    [LLM×1] 6. 判断点B（文本）：价格块 → {采购价, 运费, 估重}（剔除一次性优惠）
    [脚本] 7. WpsExcelTool append_product_row 写入
    [脚本] 8. close_tabs(text="1688")

选择器均对真实 1688 站点实测确认（2026-07，样品 offer 815392475657 + 关键词
"水气球"结果页），并修正了原计划文档的两处错误：
  - 结果卡片容器是 `.search-offer-wrapper`（连字符），非文档写的驼峰 `searchOfferWrapper`。
  - offerId 在卡片链接的 `offerIds=`/`offerId=` 参数或 `data-aplus-report` 的
    `object_id@` 里，非文档写的 `offer/<id>.html` 路径。

判断质量护栏：解析/判断失败 → 抛异常，交由 batch_collect 的单商品重试兜底；
两处判断均 best-effort，不阻断整批。
"""
import asyncio
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.collect.image_extract import extract_white_bg
from app.config import config
from app.llm import LLM
from app.logger import logger
from app.schema import Message
from app.tool.wps_excel_tool import WpsExcelTool

# ---- 已实测确认的选择器（勿凭记忆改；改前对真站重验）----------------------
# 结果页单个商品卡片容器（s.1688.com 图搜/关键词结果页通用）。
_CARD_SELECTOR = ".search-offer-wrapper"
# 详情页价格：主价区间 + 各 SKU 价/库存 + 运费 + 重量。
# 【2026-07 改·实测选择器】首屏 DOM 本就含批发阶梯价、各 SKU 价、运费、重量，故不必
# 再让 agent 点数量「+」触发内联结算面板（去掉整个 agent probe，见
# batch_collect._checkout_probe）。以下 module-od-* 类名对 3 个真实 offer
# （811937050886/1050735486992/940655272431）实测跨店稳定命中：
#   .module-od-main-price       主价阶梯，如「¥9.50 1件起批 / ¥8.50 300-499件 / ¥7.50 ≥500件」
#   .module-od-sku-selection    各 SKU 名+价+库存，如「雪人款 ¥9.5 库存4472件」
#   .module-od-shipping-services 物流区，含「运费 ¥7 起」← 之前漏抓致运费全填 0 的根因
#   .module-od-submit-order      首屏就有的金额面板，「商品金额：¥9.50 另需运费(预估)：¥7」
#                                 （即原想点「+」才出的那块，实测首屏已在 DOM）
#   .module-od-product-pack-info 包装信息，含「重量(g) 60」← 之前没抓致重量全靠猜的根因
# 运费/重量文本喂给 judge_price 判（优先按文本、缺失才估）。
_DETAIL_PRICE_JS = r"""
() => {
  const pick = (sel) => {
    const el = document.querySelector(sel);
    return el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : '';
  };
  const skuBlocks = [...document.querySelectorAll('.item-price-stock')]
      .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim())
      .filter(Boolean);
  return {
    mainPrice: pick('.module-od-main-price'),
    skuText: pick('.module-od-sku-selection'),
    skuPriceStock: skuBlocks,
    shipping: pick('.module-od-shipping-services') || pick('.module-od-submit-order'),
    weightText: pick('.module-od-product-pack-info'),
  };
}
"""

# 结果页卡片抽取：offerId / detailUrl / title / img / priceText / priceContext。
# 关键：paste_image 落地的是【图搜结果页】air.1688.com/kapp/1688-search/pc-image-search，
# 它用 CSS-Modules 哈希类名（如 searchOfferWrapper--St2OXxYo），故容器用【前缀】选择器
# `[class*="searchOfferWrapper--"]` 匹配；`.search-offer-wrapper`（连字符）是 s.1688 关键词
# 页的类名，图搜页匹配不到——务必对图搜页取，勿再凭关键词页假设（曾在此翻车）。
# offerId 多源兜底：卡片内 a[href] 的 offerId=/offerIds= 参数（图搜页是 ?offerId=）→
# data-aplus-report 的 object_id@。广告/无 id 卡跳过。
# 价格取两个：priceItem--（干净单价，如 ¥40）+ offerPriceRow--（含运费/新人价等上下文，
# 交给判断点B剔除一次性优惠）。
_READ_RESULTS_JS = r"""
(maxN) => {
  let cards = [...document.querySelectorAll('[class*="searchOfferWrapper--"]')];
  if (!cards.length) cards = [...document.querySelectorAll('.search-offer-wrapper')];  // 关键词页兜底
  const pickId = (c) => {
    for (const a of c.querySelectorAll('a[href]')) {
      const m = (a.href || '').match(/(?:offerIds?=)(\d{8,})/);
      if (m) return m[1];
    }
    for (const el of c.querySelectorAll('[data-aplus-report]')) {
      const m = (el.getAttribute('data-aplus-report') || '').match(/object_id@(\d{8,})/);
      if (m) return m[1];
    }
    return null;
  };
  const q = (c, sels) => { for (const s of sels) { const e = c.querySelector(s); if (e) return e; } return null; };
  const txt = (e) => e ? (e.innerText || '').replace(/\s+/g, ' ').trim() : '';
  const out = [];
  for (const c of cards) {
    const offerId = pickId(c);
    if (!offerId) continue;  // 广告/无 id 卡跳过
    const titleEl = q(c, ['[class*="offerTitleRow--"]', '.title-text', '[class*=title-text]', '[class*=title]']);
    const imgEl = c.querySelector('img[src*=cbu01], img[data-src*=cbu01], img');
    const priceItemEl = q(c, ['[class*="priceItem--"]', '.price-item', '[class*=price-item]']);
    const priceRowEl = q(c, ['[class*="offerPriceRow--"]', '[class*="offer-price-row"]']);
    out.push({
      offerId,
      detailUrl: 'https://detail.1688.com/offer/' + offerId + '.html',
      title: txt(titleEl).slice(0, 80),
      img: imgEl ? (imgEl.src || imgEl.getAttribute('data-src') || '') : '',
      priceText: txt(priceItemEl).slice(0, 40),
      priceContext: txt(priceRowEl).slice(0, 60),
    });
    if (out.length >= (maxN || 20)) break;
  }
  return out;
}
"""

_SAMEMATCH_SYSTEM = (
    "你是电商选品助手，帮我在 1688 找到与目标商品【精确同款】的货源。"
    "第一张图是目标商品主图，其余是 1688 候选商品缩略图（每张配了编号和标题）。"
    "注意：目标主图可能是营销拼图（带促销文案、多角度小图、模特实拍），"
    "识别其【核心商品本体】后，再逐一比对候选。"
    "【严格判定】只有当候选与目标是【同一件商品/同款】——同品类且主要特征、规格、款式一致"
    "（如同为万圣节骷髅南瓜灯串，而非泛泛的 LED 灯串）——才算同款。"
    "结合候选标题辅助判断，但以商品本体一致为准，宁缺毋滥。"
    "返回严格 JSON：{\"index\": <候选编号,从0开始>, \"reason\": \"<15字内理由>\"}。"
    "若没有任何候选是精确同款，返回 {\"index\": -1, \"reason\": \"无同款\"}。"
    "只输出 JSON，勿加围栏或解释。"
)

_PRICE_SYSTEM = (
    "你是采购价核算助手。给你一个 1688 商品详情页的价格文本块（含主价区间、各规格 SKU 的价/库存，"
    "可能还含【运费文本】）。"
    "请判断该商品的【常规批发价】——务必剔除首单价/新人价/限时价/优惠券等一次性优惠，取可持续拿到的常规价。"
    "【运费】优先按给出的运费文本判断（如「运费¥5起」取 5、「另需运费(预估)：¥7」取 7、「包邮/免运费」取 0）；"
    "文本缺失才据商品推测，仍不确定填 0。"
    "【重量】优先按『包装/重量信息』文本读单件重量（如「重量(g) 60」取 60；若按 SKU 分列了多个重量、取其一即可）；"
    "文本缺失才据商品品类/材质推测。"
    "返回严格 JSON："
    "{\"purchase_price\": <数字, 常规批发单价元>, \"shipping\": <数字, 运费元, 不确定填0>, "
    "\"weight_g\": <数字, 单件重量克>, \"note\": \"<存疑点/依据, 如 运费按文本¥7、重量按属性60g; 无则空串>\"}。"
    "只输出 JSON，勿加围栏或解释。"
)

# 主体裁剪：Temu 主图常是营销拼图/模特实拍/带促销文案，直接拿去图搜/严格判同款会
# 因干扰判「无同款」。判断前先让视觉模型框出【核心商品本体】，裁出干净单图再搜再判，
# 实测能把严格模式的漏采品救回来（费尔岛帽、万圣节灯串均从"无同款"变命中）。
# 坐标约定（qwen3-vl-plus 实测）：返回【绝对像素】，用数组 [x1,y1,x2,y2] 最稳（键名易被
# 模型写乱）；会偶尔越界，须 clamp；整图无干扰时会返回近满幅，据此跳过裁剪。
_CROP_SYSTEM = (
    "你是图像分析助手。给你一张电商商品主图（可能是营销拼图、模特实拍或含促销文案）。"
    "框出图中【最主要的可售卖商品本体】：排除模特的脸/手、背景、促销文字、装饰元素。"
    "若整张图就是单一商品、无干扰，则返回整图范围。"
)


def _crop_prompt(w: int, h: int) -> str:
    return (
        f"图片宽{w}像素、高{h}像素，左上角为(0,0)。返回主商品本体的边界框，"
        "严格输出一个 JSON 数组 [x1,y1,x2,y2]（像素整数，x1<x2 且 y1<y2），"
        "不要键名、不要解释、不要围栏。"
    )


def _parse_bbox(text: str, W: int, H: int) -> Optional[tuple]:
    """从模型输出解析 [x1,y1,x2,y2] 并 clamp 到图像边界；非法/退化返回 None。"""
    t = re.sub(r"^```(?:json)?\s*", "", (text or "").strip())
    t = re.sub(r"\s*```$", "", t)
    m = re.search(r"\[\s*-?\d+\s*,\s*-?\d+\s*,\s*-?\d+\s*,\s*-?\d+\s*\]", t)
    if not m:
        return None
    try:
        x1, y1, x2, y2 = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    x1 = max(0, min(int(x1), W))
    x2 = max(0, min(int(x2), W))
    y1 = max(0, min(int(y1), H))
    y2 = max(0, min(int(y2), H))
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def _to_image_ref(img: str) -> str:
    """把本地文件路径转成 data URL；已是 http(s)/data URL 则原样返回。

    ask_with_images 只接受 URL 字符串，本地裁图必须转 data URL 才能当目标图传入。
    """
    if not img:
        return img
    if img.startswith(("http://", "https://", "data:")):
        return img
    if os.path.exists(img):
        try:
            import base64

            with open(img, "rb") as f:
                return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
        except Exception:
            return img
    return img


async def crop_main_subject(
    src_img_path: str, config_name: str = "samematch", min_shrink: float = 0.95
) -> Optional[str]:
    """从商品主图裁出核心商品本体，写到 <path>_crop.jpeg，返回裁图路径。

    - 视觉模型返回边界框（绝对像素数组），clamp 到图像边界。
    - 若框≥原图 min_shrink（几乎满幅，说明是干净单品图），不裁剪、返回 None（调用方用原图）。
    - 任何失败（模型超时/解析失败/PIL 异常）均返回 None，退回原图，绝不阻断采集。
    """
    try:
        from PIL import Image
    except Exception:
        return None
    if not os.path.exists(src_img_path):
        return None
    try:
        im = Image.open(src_img_path).convert("RGB")
        W, H = im.size
        llm = LLM(config_name=config_name)
        # 视觉模型接受本地文件 → 转 data URL 传入（ask_with_images 接受 URL 字符串）
        data_url = _to_image_ref(src_img_path)
        raw = await llm.ask_with_images(
            messages=[Message.user_message(_crop_prompt(W, H))],
            images=[data_url],
            system_msgs=[Message.system_message(_CROP_SYSTEM)],
            stream=False,
            temperature=0.0,
        )
        box = _parse_bbox(raw, W, H)
        if not box:
            return None
        frac = (box[2] - box[0]) * (box[3] - box[1]) / (W * H)
        if frac >= min_shrink:
            logger.info(f"crop_main_subject：框近满幅（{frac:.2f}），判为干净单图，不裁剪")
            return None
        out_path = os.path.splitext(src_img_path)[0] + "_crop.jpeg"
        im.crop(box).save(out_path)
        logger.info(f"crop_main_subject：已裁主体 {box}（占原图 {frac:.2f}）→ {out_path}")
        return out_path
    except Exception as e:
        logger.warning(f"crop_main_subject 失败（退回原图）：{e}")
        return None


@dataclass
class CollectResult:
    """单商品采集结果（供调用方决定写 Excel / 记失败）。"""

    spu: str
    ok: bool
    offer_id: Optional[str] = None
    detail_url: Optional[str] = None
    purchase_price: Optional[float] = None
    shipping: Optional[float] = None
    weight_g: Optional[float] = None
    note: str = ""
    fail_reason: str = ""
    # 图搜出了候选、但严格同款判断判「无同款」。此种失败退回 agent 也没用：
    # agent 落到同一个 air.1688 图搜 SPA 页只会撞 token 上限/超时，且即便成功也是
    # 宽松匹配（与严格模式的取舍相悖）。故调用方应对此跳过 agent 兜底、直接记漏采。
    no_same_match: bool = False
    candidates: list = field(default_factory=list)


def _parse_json(text: str) -> Optional[dict]:
    """剥 ```json 围栏后 json.loads；再兜底抓第一个 {...}。失败返回 None。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


async def _get_page(browser_tool):
    """拿到复用已登录 Chrome 的当前活动 page。"""
    ctx = await browser_tool._ensure_browser_initialized()
    return await ctx.get_current_page()


async def read_search_results(browser_tool, max_n: int = 20) -> list[dict]:
    """从当前结果页抽 offer 卡片列表（确定性）。空列表表示没抓到卡片。

    关键：paste_image 落地的 air.1688 图搜页是 SPA，卡片异步渲染。若 paste_image
    刚返回就 evaluate，卡片往往还没挂载 → 抓 0 条（曾在此翻车）。故先轮询等待卡片
    选择器出现（最多约 12s），再抽取。
    """
    page = await _get_page(browser_tool)
    # 等卡片挂载：图搜页哈希类名前缀 or 关键词页连字符类名，任一出现即可。
    try:
        await page.wait_for_selector(
            '[class*="searchOfferWrapper--"], .search-offer-wrapper', timeout=12000
        )
    except Exception:
        logger.warning("read_search_results：等卡片渲染超时（12s），仍尝试抽取")
    await page.wait_for_timeout(800)  # 让首屏卡片补全
    try:
        results = await page.evaluate(_READ_RESULTS_JS, max_n)
    except Exception as e:
        logger.warning(f"read_search_results 解析异常：{e}")
        return []
    return results or []


# 1688 图搜页的 YOLO 主体框：覆盖在上传图预览上的可点小框（.cropRegion--<hash>）。
# 取其屏幕中心坐标（w,h>10 过滤退化框）。屏幕位置固定，不随选中态/DOM 重排移动，
# 故用坐标点击切主体最稳（详见 collect docs）。
_REGION_CENTERS_JS = (
    '() => [...document.querySelectorAll(\'[class*="cropRegion--"]\')].map(e=>{'
    "const r=e.getBoundingClientRect();"
    "return {cx:Math.round(r.left+r.width/2),cy:Math.round(r.top+r.height/2),"
    "w:Math.round(r.width),h:Math.round(r.height)};"
    "}).filter(b=>b.w>10&&b.h>10)"
)


def _region_key(url: str) -> str:
    """从图搜页 URL 取主体框标识；无 region 参数即默认主体，记 'R0'。"""
    m = re.search(r"region=\d+,\d+,\d+,\d+", url or "")
    return m.group(0) if m else "R0"


async def search_match_over_regions(
    browser_tool, target_img: str, max_regions: int = 4
) -> Optional[dict]:
    """在图搜结果页逐个尝试 1688 的主体框，返回首个判为同款的候选，无则 None。

    背景（均对真站实测）：
    - 1688 默认选中的主体不稳定：模特实拍常默认框到脸/手（→ 搜出手套），漏掉真商品。
    - 顶部那排框（.cropRegion--<hash>）就是 1688 的 YOLO 主体检测，点不同框 = 换主体重搜。
    - 坑1：点框会【原地】重搜，但重搜后的卡片 <a href> 是空的 → offerId 抓不到。
      解法：点完框拿到 URL 的 region 参数后，再 goto 一次该 URL【整页重载】才会补全链接。
    - 坑2：点选一个框后列表会重排，位置索引失效 → 用【屏幕坐标】点框（位置固定不随重排动），
      并用 URL 的 region 串去重，避免重复/漏试。

    流程：先判默认主体(R0)；不中再逐个坐标点框 → 重载 → 读候选 → 判同款；命中即返回。
    """
    matches = await collect_matches_over_regions(
        browser_tool, target_img, top_k=1, max_regions=max_regions
    )
    return matches[0] if matches else None


async def collect_matches_over_regions(
    browser_tool, target_img: str, top_k: int = 3, max_regions: int = 4
) -> list[dict]:
    """在图搜结果页跨主体框【累计】最多 top_k 个去重同款候选（用于比价），无则空列表。

    与 search_match_over_regions 同一套主体框 dance（默认主体 + 逐框重搜），区别在于
    不「命中即停」，而是把每个框读到的同款累计去重（按 offerId），凑够 top_k 或试完
    所有框才返回。top_k=1 即退化为原「命中即停」单选行为。
    """
    page = await _get_page(browser_tool)
    collected: list[dict] = []
    seen: set = set()

    def _accumulate(cands_matched: list[dict]) -> bool:
        """把一批同款候选并进结果（去重）；返回是否已凑够 top_k。"""
        for c in cands_matched:
            oid = c.get("offerId")
            if oid and oid not in seen:
                seen.add(oid)
                collected.append(c)
        return len(collected) >= top_k

    async def _read_and_judge() -> list[dict]:
        cands = await read_search_results(browser_tool, 8)
        if not cands:
            return []
        if top_k == 1:
            one = await judge_same_match(target_img, cands)
            return [one] if one else []
        return await judge_same_matches(target_img, cands, top_k=top_k)

    # 1) 默认主体（paste_image 落地即此，已加载好、卡片带链接）
    if _accumulate(await _read_and_judge()):
        return collected[:top_k]

    tried = {_region_key(page.url)}

    # 2) 先一次性抓所有主体框的屏幕中心（位置固定，抓早了也不受后续重排影响）
    try:
        centers = await page.evaluate(_REGION_CENTERS_JS)
    except Exception as e:
        logger.warning(f"主体框枚举失败：{e}")
        centers = []
    # 去重相近中心
    uniq = []
    for c in centers:
        if all(abs(c["cx"] - u["cx"]) > 15 or abs(c["cy"] - u["cy"]) > 15 for u in uniq):
            uniq.append(c)

    for c in uniq[:max_regions]:
        prev = page.url
        try:
            await page.mouse.click(c["cx"], c["cy"])
        except Exception as e:
            logger.warning(f"点主体框({c['cx']},{c['cy']})失败：{e}")
            continue
        # 等 URL 出现/变化到新 region
        for _ in range(20):
            await page.wait_for_timeout(400)
            if page.url != prev:
                break
        key = _region_key(page.url)
        if key in tried:
            continue
        tried.add(key)
        # 整页重载该 region URL → 卡片链接才会补全（原地重搜的卡片 href 是空的）
        try:
            await page.goto(page.url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2600)
        except Exception as e:
            logger.warning(f"重载主体框 {key} 失败：{e}")
            continue
        logger.info(f"collect_matches_over_regions：试主体框 {key}（已收集 {len(collected)}）")
        if _accumulate(await _read_and_judge()):
            return collected[:top_k]
    return collected[:top_k]


async def read_detail_price(browser_tool, detail_url: str) -> dict:
    """打开详情页并抓价格文本块（确定性）。"""
    page = await _get_page(browser_tool)
    await page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2500)  # 等价格模块渲染
    try:
        return await page.evaluate(_DETAIL_PRICE_JS)
    except Exception as e:
        logger.warning(f"read_detail_price 解析异常：{e}")
        return {}


async def judge_same_match(
    target_img: str, candidates: list[dict], config_name: str = "samematch"
) -> Optional[dict]:
    """判断点A（视觉）：目标主图 + 候选缩略图 → 选同款候选。

    返回选中的候选 dict（含 offerId/detailUrl），无同款或失败返回 None。
    """
    if not candidates:
        return None
    llm = LLM(config_name=config_name)
    lines = ["目标商品主图见第一张图。候选商品（编号→标题）："]
    images = [_to_image_ref(target_img)]  # 目标图可能是本地裁图 → 转 data URL
    for i, c in enumerate(candidates):
        lines.append(f"[{i}] {c.get('title', '')}（参考价 {c.get('priceText', '')}）")
        if c.get("img"):
            images.append(c["img"])
    prompt = "\n".join(lines) + "\n\n请选出与目标同款的候选编号。"

    raw = await llm.ask_with_images(
        messages=[Message.user_message(prompt)],
        images=images,
        system_msgs=[Message.system_message(_SAMEMATCH_SYSTEM)],
        stream=False,
        temperature=0.0,
    )
    data = _parse_json(raw)
    if not data:
        logger.warning(f"judge_same_match：解析失败，原始：{(raw or '')[:120]}")
        return None
    idx = data.get("index", -1)
    if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
        logger.info(f"judge_same_match：无同款（{data.get('reason', '')}）")
        return None
    chosen = dict(candidates[idx])
    chosen["_match_reason"] = data.get("reason", "")
    logger.info(
        f"判同款选中 offer={chosen.get('offerId')}：{chosen.get('title', '')[:30]}"
        f" | {chosen.get('detailUrl', '')}"
    )
    return chosen


_MULTIMATCH_SYSTEM = (
    "你是电商选品助手，帮我在 1688 找到与目标商品【精确同款】的多个货源用于比价。"
    "第一张图是目标商品主图，其余是 1688 候选商品缩略图（每张配了编号和标题）。"
    "注意：目标主图可能是营销拼图（带促销文案、多角度小图、模特实拍），"
    "识别其【核心商品本体】后，再逐一比对候选。"
    "【严格判定】只有当候选与目标是【同一件商品/同款】——同品类且主要特征、规格、款式一致"
    "——才算同款。结合候选标题辅助判断，但以商品本体一致为准，宁缺毋滥。"
    "从所有候选里选出【最多 {top_k} 个】精确同款、且最有比价价值的（不同店铺/价格有差异的优先），"
    "按推荐优先级排序。返回严格 JSON："
    "{{\"indexes\": [<候选编号,从0开始>, ...], \"reason\": \"<15字内理由>\"}}。"
    "若没有任何候选是精确同款，返回 {{\"indexes\": [], \"reason\": \"无同款\"}}。"
    "只输出 JSON，勿加围栏或解释。"
)


async def judge_same_matches(
    target_img: str, candidates: list[dict], top_k: int = 3, config_name: str = "samematch"
) -> list[dict]:
    """判断点A（视觉·多选）：目标主图 + 候选缩略图 → 选出最多 top_k 个同款用于比价。

    返回选中的候选 dict 列表（含 offerId/detailUrl，按推荐优先级），无同款返回空列表。
    """
    if not candidates:
        return []
    llm = LLM(config_name=config_name)
    lines = ["目标商品主图见第一张图。候选商品（编号→标题）："]
    images = [_to_image_ref(target_img)]
    for i, c in enumerate(candidates):
        lines.append(f"[{i}] {c.get('title', '')}（参考价 {c.get('priceText', '')}）")
        if c.get("img"):
            images.append(c["img"])
    prompt = "\n".join(lines) + f"\n\n请选出最多 {top_k} 个与目标同款的候选编号（比价用）。"

    raw = await llm.ask_with_images(
        messages=[Message.user_message(prompt)],
        images=images,
        system_msgs=[Message.system_message(_MULTIMATCH_SYSTEM.format(top_k=top_k))],
        stream=False,
        temperature=0.0,
    )
    data = _parse_json(raw)
    if not data:
        logger.warning(f"judge_same_matches：解析失败，原始：{(raw or '')[:120]}")
        return []
    idxs = data.get("indexes", [])
    if not isinstance(idxs, list):
        return []
    out = []
    seen = set()
    for idx in idxs:
        if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
            continue
        oid = candidates[idx].get("offerId")
        if oid in seen:
            continue
        seen.add(oid)
        c = dict(candidates[idx])
        c["_match_reason"] = data.get("reason", "")
        out.append(c)
        logger.info(
            f"判同款选中 [{len(out)}] offer={oid}：{c.get('title', '')[:30]}"
            f" | {c.get('detailUrl', '')}"
        )
        if len(out) >= top_k:
            break
    if not out:
        logger.info(f"judge_same_matches：无同款（{data.get('reason', '')}）")
    return out


async def judge_price(price_block: dict, config_name: str = "default") -> Optional[dict]:
    """判断点B（文本）：价格块 → {purchase_price, shipping, weight_g, note}。"""
    parts = []
    if price_block.get("mainPrice"):
        parts.append("主价区间：\n" + price_block["mainPrice"])
    if price_block.get("skuText"):
        parts.append("规格区：\n" + price_block["skuText"][:600])
    if price_block.get("skuPriceStock"):
        parts.append("各SKU价/库存：\n" + " | ".join(price_block["skuPriceStock"][:20]))
    if price_block.get("shipping"):
        parts.append("运费文本：\n" + price_block["shipping"])
    if price_block.get("weightText"):
        parts.append("包装/重量信息：\n" + price_block["weightText"][:300])
    if not parts:
        return None

    llm = LLM(config_name=config_name)
    raw = await llm.ask(
        messages=[Message.user_message("\n\n".join(parts))],
        system_msgs=[Message.system_message(_PRICE_SYSTEM)],
        stream=False,
        temperature=0.0,
    )
    data = _parse_json(raw)
    if not data:
        logger.warning(f"judge_price：解析失败，原始：{(raw or '')[:120]}")
        return None
    return data


def archive_unmatched_image(spu: str) -> Optional[str]:
    """把"已提取主图但没找到同款采购价"的漏采品主图，归档到「未找到同款主图」目录，
    文件名带 SPU，供人工后续手动找货源补价。

    - 优先归档【白底提取图】`{spu}_white.png`（干净、最适合人工再搜）；无则退回原始
      主图 `{spu}.jpeg`（如白底提取失败/未启用）。两者都没有则返回 None。
    - 归档为 `未找到同款主图/{日期}-{spu}<原扩展名>`（日期为归档当天 YYYY-MM-DD）；
      同一天重复归档直接覆盖，跨天则各留一份（便于看漏采发生在哪批）。
    - best-effort：任何异常仅告警、返回 None，绝不阻断采集主流程。
    """
    import shutil
    from datetime import datetime

    img_dir = str(config.output_dir("image"))
    white = os.path.join(img_dir, f"{spu}_white.png")
    orig = os.path.join(img_dir, f"{spu}.jpeg")
    src = white if os.path.exists(white) else (orig if os.path.exists(orig) else None)
    if not src:
        logger.warning(f"archive_unmatched_image：SPU={spu} 无主图可归档（白底/原图都不在）")
        return None
    date = datetime.now().strftime("%Y-%m-%d")
    dst = os.path.join(
        str(config.output_dir("unmatched")), f"{date}-{spu}{os.path.splitext(src)[1]}"
    )
    try:
        shutil.copy2(src, dst)
        return dst
    except Exception as e:
        logger.warning(f"archive_unmatched_image：SPU={spu} 归档失败 {e}")
        return None


# Temu 主图 CDN（img.kwcdn.com）会对"裸" requests（无 UA/Referer）做 bot 拦截，
# 表现为连接被重置（WinError 10054）或 403。带上浏览器化请求头 + 有限重试即可稳。
# 实测：agent 路径的提示词早已说明"直连可 200、仅 403 才退回会话内 fetch"，管道这里
# 补齐同样的请求头 + 重试，避免一次瞬时重置就把商品推到昂贵的 agent 兜底。
_IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.temu.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def _download_main_image(url: str, dst_path: str, retries: int = 3) -> None:
    """把主图 URL 下载到 dst_path，带浏览器化请求头 + 指数退避重试。

    失败（重试耗尽/HTTP 错/内容为空）抛异常，交由 collect_one_product 记 fail_reason。
    """
    import time

    import requests

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=_IMG_HEADERS, timeout=(10, 30))
            r.raise_for_status()
            if not r.content:
                raise ValueError("响应体为空")
            with open(dst_path, "wb") as f:
                f.write(r.content)
            return
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = (0, 1, 3)[min(attempt, 2)]
                logger.warning(
                    f"主图下载失败（{attempt}/{retries}）：{e}；{wait}s 后重试"
                )
                time.sleep(wait)
    raise RuntimeError(f"重试 {retries} 次仍失败：{last_err}")


async def collect_one_product(
    browser_tool, item: dict, checkout_probe=None, top_k: int = 3
) -> CollectResult:
    """确定性采集单个商品（不写 Excel，由调用方决定落库）。

    任一确定性步骤失败即抛/记 fail_reason，交上层重试或退回 agent 兜底。

    checkout_probe: 可选的异步回调 async (offer: dict) -> Optional[dict]，用于让 agent 进
        每个同款候选的结算页读【真实货价/运费/重量】，返回
        {goods_price, shipping, total, weight_g, weight_basis, sku_desc} 或 None。
        提供时走「收集 top_k 个同款 → 各自 probe → 取总价最低」的比价路径；
        为 None 时退回原「单个同款 + 详情页文本读价」路径。
    """
    spu = str(item.get("spu", ""))
    res = CollectResult(spu=spu, ok=False)

    img_dir = config.output_dir("image")
    img_path = os.path.join(str(img_dir), f"{spu}.jpeg")

    # 1. 主图（调用方通常已下好；缺则下载）。带浏览器化请求头 + 重试，避免 Temu CDN
    #    对裸 requests 的 bot 拦截（连接重置/403）一次瞬时失败就把商品推到 agent 兜底。
    #    下载放线程里跑，不阻塞浏览器所在事件循环。
    if not os.path.exists(img_path) and item.get("image"):
        try:
            await asyncio.to_thread(_download_main_image, item["image"], img_path)
        except Exception as e:
            res.fail_reason = f"主图下载失败：{e}"
            return res
    if not os.path.exists(img_path):
        res.fail_reason = "无主图可用"
        return res

    # 2. 图搜图 → 结果页。先把主图提取成【白底单品图】当搜索查询图：Temu 主图常是
    #    营销拼图（多角度/模特/促销文案），直接搜 recall 差、还得靠 1688 的主体框逐个
    #    点选救。白底图接近 1688 供货商主图，搜得准且省掉框选 dance。提取失败退回原图。
    #    标题（item["name"]）喂给生图模型约束该保留哪一件。
    search_img = await extract_white_bg(img_path, item.get("name", "")) or img_path
    got_white = search_img != img_path
    # 【必须新开标签】用 open_tab 而非 go_to_url：go_to_url 是在【当前活动标签】原地导航，
    # 而枚举复用并置前了 Temu 的 product-select 标签，采集开始时当前标签常就是它——原地
    # 导航会把 Temu 页覆盖成 1688，随后 close_tabs(text="1688") 又把它关掉，导致用户的
    # Temu 页签消失。开新标签则图搜全程在新标签里，采完关掉，Temu 标签原样保留。
    r = await browser_tool.execute(action="open_tab", url="https://www.1688.com/")
    if r.error:
        res.fail_reason = f"打开 1688 失败：{r.error}"
        return res
    r = await browser_tool.execute(action="paste_image", file_path=search_img)
    if r.error:
        res.fail_reason = f"图搜失败：{r.error}"
        return res

    # 3+4. 挑同款。【红线】判同款始终用原图：白底图是重绘、可能改动商品外观，只影响
    #      recall；精度必须锚在真实像素（item["image"]）上。
    #      有 checkout_probe（比价模式）→ 收集最多 top_k 个同款；否则单选（原行为）。
    #
    #      【2026-07 修正】只在图搜【落地结果列表页】里直接对卡片判同款，不再点 1688 的
    #      主体框（YOLO cropRegion）换区域重搜——实测「换框重搜=让 1688 用图里某块区域
    #      找相似」，结果会发散到不相干品类（搜"水果毛绒玩偶"点框后搜出女装/童装套装）。
    #      白底提取已能保证查询图干净、默认主体即准，主体框 dance 弊大于利，故弃用。
    target_img = item.get("image", "") or img_path
    want_k = top_k if checkout_probe else 1
    cands = await read_search_results(browser_tool, 20)  # 多读些卡片供筛（不再靠重搜补）
    if not cands:
        matches = []
        had_candidates = False
    elif want_k == 1:
        one = await judge_same_match(target_img, cands)
        matches = [one] if one else []
        had_candidates = True
    else:
        matches = await judge_same_matches(target_img, cands, top_k=want_k)
        had_candidates = True
    if not matches:
        # 有候选但无同款 → no_same_match=True：clean query 下 agent 也不会更好，记漏采、
        #   跳过 agent 兜底（省批次时间）。
        # 0 候选（白底图可能生崩/过裁）→ no_same_match=False：放行 agent 用原图兜底。
        res.fail_reason = "未匹配到同款" if had_candidates else "白底图搜无候选"
        res.no_same_match = had_candidates
        res.candidates = matches
        return res
    res.candidates = matches

    # 5+6. 比价读价。
    if checkout_probe:
        # 比价模式：每个同款候选进结算页读【真实货价+运费+重量】，取总价最低。
        priced = []
        for m in matches:
            try:
                info = await checkout_probe(m)
            except Exception as e:
                logger.warning(f"结算探测异常 offer={m.get('offerId')}：{e}")
                info = None
            if info and info.get("total") is not None:
                info["_offer"] = m
                priced.append(info)
        if priced:
            best = min(priced, key=lambda x: float(x.get("total", 1e9)))
            res.offer_id = best["_offer"].get("offerId")
            res.detail_url = best["_offer"].get("detailUrl")
            res.purchase_price = _to_number(best.get("goods_price"))
            res.shipping = _to_number(best.get("shipping")) or 0
            res.weight_g = _to_number(best.get("weight_g"))
            basis = best.get("weight_basis", "")
            res.note = (f"比价{len(priced)}家取最低; " if len(priced) > 1 else "") + (
                f"重量依据:{basis}" if basis else ""
            )
            res.ok = res.purchase_price is not None
            if not res.ok:
                res.fail_reason = "结算读价未得出货价"
            return res
        # 全部 probe 失败 → 退回旧读价路径（用第一个同款）
        logger.warning(f"SPU={spu} 所有候选结算探测失败，退回详情页文本读价")

    # 单选模式 / 比价全失败兜底：用第一个同款 + 详情页文本读价（含 judge_price 估重）
    chosen = matches[0]
    res.offer_id = chosen.get("offerId")
    res.detail_url = chosen.get("detailUrl")
    price_block = await read_detail_price(browser_tool, res.detail_url)
    if not price_block or not price_block.get("mainPrice"):
        res.fail_reason = "详情页未读到价格"
        return res
    price = await judge_price(price_block)
    if not price:
        res.fail_reason = "价格判断失败"
        return res
    res.purchase_price = price.get("purchase_price")
    res.shipping = price.get("shipping", 0)
    res.weight_g = price.get("weight_g")
    res.note = price.get("note", "")

    res.ok = res.purchase_price is not None
    if not res.ok:
        res.fail_reason = "未得出采购价"
    return res


# 【勿再硬编码列】不同 Sheet（pawly美国/全球、wintak、VibeMakers…）的列序完全不同：
# SPU 在 C 还是 D、图片在 D 还是 E、采购价在 I 还是 J、ros 在 O/P/Q…全不一样。写入列现在
# 一律由 inspect 的「字段列映射」按【本 Sheet 真实表头】解析（见 WpsExcelTool._FIELD_RULES），
# 公式/固定值只对明确的成本计算列仿写；平台清单没有的加速器参考价、叠加折扣保持空白。


def _template_cell_value(text: Any) -> Any:
    """历史模板格显示值 → 写回值；纯数字保持数值，百分比/文本保持原文。"""
    value = str(text or "").strip()
    try:
        number = float(value)
    except ValueError:
        return value
    return int(number) if number == int(number) else number


def _to_number(v) -> Optional[float]:
    """从可能带货币符（¥）/千分位的价格串解析出数字；失败返回 None。

    Temu 原始价形如 "46.10¥"/"1,299.00¥"，是字符串。直接写进 G/I 会让 H=I/G 折扣
    公式因文本参与运算而崩。此处剥成纯数字。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group(0)) if m else None


@dataclass
class SheetSchema:
    """一个目标 Sheet 的写入结构（表头解析结果）——每批只解析一次，全批复用。

    为什么单拎出来：同一工作簿里各 Sheet 列序完全不同（SPU 在 C 还是 D、采购价在 I 还是 J
    …），必须【按真实表头】决定往哪列写。而这些事实在一批采集里是恒定的，逐商品重新 inspect
    一个 225MB 工作簿既慢又浪费——故在批次开始时 resolve_sheet_schema 一次，随后每个商品的
    write_product_row 直接复用。真正逐次变化的只有"插入到第几行/复制哪行样式"，那由
    WpsExcelTool._append 自己读，不在此缓存。

    字段：
    - sheet：表名。
    - fields：逻辑字段 → 列字母（spu/image/site/category/daily/discount/sale/purchase/weight/ros/note）。
    - formula_columns：公式列 → 模板（行号已换成 {r} 占位，随行自适应）。
    - constant_columns：从同 Sheet 健康历史行继承的允许仿写固定值（空运头程、尾程、
      广告、ros、成本、利润、毛利、操作费）。名称为兼容旧调用保留。
    - format_columns：逐列学到的单元格格式（数字格式/对齐），写新行时回放，
      新行才有和历史行一样的两位小数、百分比。云端专用（本地靠复制样式索引）。
    - ok / error：结构是否可写（解析不出 SPU 列即不可写，error 带原因）。
    - header_row：表头行号（1-based）。云端写行/判重需要它（新行插到表头正下方、
      公式行号 = header_row + 1）；本地路径用不上，默认 1 无影响。
    """

    sheet: str
    fields: dict = field(default_factory=dict)
    formula_columns: dict = field(default_factory=dict)
    constant_columns: dict = field(default_factory=dict)
    format_columns: dict = field(default_factory=dict)
    ok: bool = False
    error: str = ""
    header_row: int = 1


async def resolve_sheet_schema(excel_tool, excel_path: str, sheet: str) -> SheetSchema:
    """inspect 一次目标 Sheet，解析出稳定的写入结构（SSheetSchema）。每批调用一次。

    - 缺 SPU 列 → ok=False（表头认不出，拒绝写入，避免列错位乱写）。
    - 公式列过滤掉图片列与任何含 DISPIMG 的公式：坏历史行可能把嵌入图误写到别的列（实测
      pawly美国 曾把 DISPIMG 落到"货号"列），若当普通公式复制会把新行也污染成图片公式。
    """
    inspect = await excel_tool.execute(
        action="inspect", file_path=excel_path, sheet_name=sheet
    )
    if inspect.error:
        return SheetSchema(sheet=sheet, error=f"inspect 失败：{inspect.error}")
    try:
        info = json.loads(inspect.output)
    except Exception as e:
        return SheetSchema(sheet=sheet, error=f"inspect 输出解析失败：{e}")

    fields = info.get("字段列映射", {}) or {}
    if not fields.get("spu"):
        return SheetSchema(
            sheet=sheet,
            error=f"无法在 Sheet「{sheet}」表头解析出 SPU 列（避免列错位，拒绝写入）",
        )
    image_col = fields.get("image")
    # 逐商品写值的列不仿公式：本表「销售价格」这类列历史可能是 =参考价*折扣，仿走会把
    # 平台申报价顶掉（见 wps_excel_tool._ITEM_INPUT_FIELDS 的注释）。云端同口径。
    item_cols = WpsExcelTool.item_input_columns(fields)

    # 公式列：sample 里 = 开头、非图片列、非逐商品写值列、且不含 DISPIMG。把公式里
    # 【所有相对单元格引用】的行号换成 {r} 占位符，_append 的 .format(r=新行号) 才能让公式
    # 随行自适应；否则新行公式冻在旧行号。
    # 【关键教训】不能假设引用行号=最后数据行去替换：采样行本身可能是历史坏行、公式冻在更早
    # 行号 → 按【实际出现的引用行号】通配替换，谁在换谁。
    # (?<![A-Za-z$]) 避开函数名尾随数字与绝对引用（$G$1）；常数（如 *80，无字母前缀）不误伤。
    sample = info.get("sample_最后行公式与值", {})
    formula_columns = {}
    for c, f in sample.items():
        if c == image_col or c in item_cols:
            continue
        if not isinstance(f, str) or not f.startswith("="):
            continue
        if "DISPIMG" in f:  # 坏行把嵌入图落到了普通列 → 别当公式复制
            continue
        formula_columns[c] = re.sub(r"(?<![A-Za-z$])([A-Z]{1,3})\d+", r"\1{r}", f)

    return SheetSchema(
        sheet=sheet,
        fields=fields,
        formula_columns=formula_columns,
        constant_columns=(
            info.get("模板输入列", {}) or info.get("常量输入列", {}) or {}
        ),
        ok=True,
    )


def _build_column_values(item: dict, res: CollectResult, schema: SheetSchema) -> dict:
    """构造新行的 {列字母: 值}（不含公式列），本地/云端两个写入口共用。

    含：采购价=货价+运费、售价剥¥转数字、重量克→公斤、成本语义列的历史固定值回填、
    存疑备注列。字段没解析到的列跳过，不误写。公式列由调用方按各自行号规则补上
    （本地是 _append 复制样式时 format，云端是写前 format(r=header_row+1)）。
    """
    fields = schema.fields

    # 采购价/重量：有值就写，无值（如所有主体框均未匹配的快速失败）留空待人工补。
    if res.purchase_price is not None:
        purchase_cell = round(float(res.purchase_price) + float(res.shipping or 0), 2)
    else:
        purchase_cell = ""
    # 价格剥¥转数字（否则 折扣=销售价/日常价 公式崩）；解析不出则留空、不写脏字符串。
    # item["price"] 现在是【该 SKU 自己的申报价】（枚举时从 siteSupplierPriceList 取）。
    # 改造前这里拿的是 SPU 级 supplierPrice，对多 SKU 商品是区间串 "60.00~299.68¥"，
    # 被下面的 _to_number 正则静默截成下限——实测 128 个商品里 89 个销售价因此写错。
    sale_price = _to_number(item.get("price"))
    sale_cell = sale_price if sale_price is not None else ""
    # 重量：judge_price 返回【克】，本表重量列是【公斤】（历史 0.3kg→空运头程=K*80+1=25 吻合），克÷1000。
    if res.weight_g is not None:
        try:
            weight_cell = round(float(res.weight_g) / 1000.0, 3)
        except (TypeError, ValueError):
            weight_cell = ""
    else:
        weight_cell = ""

    # 货号列＝逐 SKU 标识：清单已是一个 SKU 一行（见 service._FETCH_ALL_JS），同一 SPU 会
    # 占多行、SPU 列必然重复，必须靠它区分。
    # 写【纯规格值】（如 `奶白+黑色/10双`）而不是 skuId：这列本来就是人工在记规格，历史值
    # 形如「5双」「直径32CM」「单人」，塞一串平台 skuId 进去会让新旧行风格割裂、也没法人工核对。
    # 判重因此认这串文本（见 service.dedupe_key），代价是平台改文案会多写一行。
    # 没有 sku_spec（老清单/无 SKU 结构）就不写这列，行为与改造前一致。
    sku_cell = str(item.get("sku_spec") or "").strip()

    # 按解析出的真实列填逐商品字段（字段没解析到就跳过该列，不误写）。
    column_values = {}
    _field_val = {
        "site": item.get("site", ""),
        "category": item.get("category", ""),
        "spu": res.spu,
        "sale": sale_cell,
        "daily": sale_cell,  # 日常价：无独立来源时暂用销售价，同旧行为
        "purchase": purchase_cell,
        "weight": weight_cell,
    }
    if sku_cell:
        _field_val["sku"] = sku_cell
    for fname, val in _field_val.items():
        col = fields.get(fname)
        if col:
            column_values[col] = val

    # 只回填 schema 明确筛选出的允许仿写列。加速器参考价、叠加折扣等不在其中，保持空白。
    for col, cval in schema.constant_columns.items():
        column_values.setdefault(col, cval)

    note = res.note or ""
    note_col = fields.get("note")
    if note and note_col:
        column_values[note_col] = note  # 存疑标记，不阻断

    return column_values


async def write_product_row(
    excel_tool,
    excel_path: str,
    sheet: str,
    item: dict,
    res: CollectResult,
    image_path: str,
    schema: Optional[SheetSchema] = None,
) -> tuple[bool, str]:
    """把采集结果按【本 Sheet 真实表头】写入 Excel。

    schema：批次开始时 resolve_sheet_schema 解析好的结构，全批复用（省去逐商品重 inspect
    大工作簿）。未传时就地解析一次（CLI/单元测试等场景的兜底）。
    返回 (是否成功, 消息)。存疑（res.note 非空）时写入备注列，不阻断。
    """
    if schema is None:
        schema = await resolve_sheet_schema(excel_tool, excel_path, sheet)
    if not schema.ok:
        return False, schema.error
    fields = schema.fields
    image_col = fields.get("image")
    formula_columns = schema.formula_columns
    column_values = _build_column_values(item, res, schema)

    kwargs = dict(
        action="append_product_row",
        file_path=excel_path,
        sheet_name=sheet,
        column_values=column_values,
        formula_columns=formula_columns,
    )
    if image_col and os.path.exists(image_path):
        kwargs["image_path"] = image_path
        kwargs["image_column"] = image_col  # 本 Sheet 的产品图片列（勿写货号列）

    r = await excel_tool.execute(**kwargs)
    if r.error:
        return False, f"写入失败：{r.error}"
    return True, r.output or "已写入"


async def resolve_sheet_schema_cloud(cloud, sheet: str) -> SheetSchema:
    """云端协作文档版的 resolve_sheet_schema：从 KdocsSheet 读表头与采样数据行，
    解析出与本地同构的 SheetSchema（字段列/公式模板/常量列）。每批调用一次。

    与本地版的差异只在数据源：云端 API 的 fmlaText 直接给公式原文（本地要解 xlsx
    里的 <f> 节点），cellText 是显示值。表头→字段映射复用本地同一个纯函数
    （WpsExcelTool._resolve_fields_from_header），保证两条路径口径一致。
    缺 SPU 列 → ok=False（表头认不出，拒绝写入，避免列错位乱写）。
    """
    header, header_row = await asyncio.to_thread(cloud.read_header, sheet)
    fields = WpsExcelTool._resolve_fields_from_header(header)
    if not fields.get("spu"):
        return SheetSchema(
            sheet=sheet,
            error=f"无法在 Sheet「{sheet}」表头解析出 SPU 列（避免列错位，拒绝写入）",
        )
    image_col = fields.get("image")

    sample = await asyncio.to_thread(cloud.read_data_sample, sheet, header_row)

    # 先枚举采样区里真实公式列。随后不是盲取第一/最后一行，而是选出「公式最齐、公式依赖
    # 输入最完整」的健康历史行作模板，避免刚写坏的新行反过来污染下一批。
    # 公式列【不受成本列白名单限制】（与本地 _inspect 同口径）：公式是本表的计算逻辑，
    # 按标题表去卡会让改过列名/多加一列计算列的 Sheet 整列丢公式。
    mimic_cols = WpsExcelTool.collect_mimic_columns(header)
    item_cols = WpsExcelTool.item_input_columns(fields)
    formula_candidates = {}
    for row in sample:
        for col, cell in row.items():
            if col == image_col or col in item_cols:
                continue
            f = cell.get("formula") or ""
            if not f.startswith("=") or "DISPIMG" in f:
                continue
            formula_candidates.setdefault(col, f)

    # 照抄历史固定值仍只在成本语义白名单内——数据形态区分不了「尾程运费」和
    # 「加速器参考价格」，只有标题能（见 wps_excel_tool._COLLECT_MIMIC_TITLES）。
    template_input_cols = mimic_cols - set(formula_candidates) - item_cols

    def row_score(row: dict) -> tuple:
        formulas = sum(
            1 for col in formula_candidates
            if str((row.get(col) or {}).get("formula") or "").startswith("=")
        )
        inputs = sum(
            1 for col in template_input_cols
            if str((row.get(col) or {}).get("text") or "").strip()
        )
        populated = sum(
            1 for cell in row.values()
            if str(cell.get("text") or cell.get("formula") or "").strip()
        )
        return formulas, inputs, populated

    template_row = max(sample, key=row_score, default={})
    formula_columns = {}
    # 优先拿同一健康模板行的公式，缺列才退回采样区其它行；保证整套逻辑来自同一行。
    for col in formula_candidates:
        f = (template_row.get(col) or {}).get("formula") or formula_candidates[col]
        formula_columns[col] = re.sub(
            r"(?<![A-Za-z$])([A-Z]{1,3})\d+", r"\1{r}", f
        )

    constant_columns = {
        col: _template_cell_value(template_row[col]["text"])
        for col in sorted(template_input_cols)
        if col in template_row and template_row[col].get("text", "").strip()
    }
    # 数字格式/对齐：按【同列多行投票取最常见的那个】，不用「模板行优先」。
    # 理由是实测出来的：模板行往往就是上一批管线写的行，而管线此前根本没回放过格式
    # （读侧字段名认错，见 kdocs_sheet.read_cell_xf），那行自己就是「无格式」——
    # 拿它当基准等于把没格式一代代继承下去。同列投票天然绕开这类行。
    #
    # 【数字格式与对齐分开投票】否则「没设数字格式」的行会连带压掉真实格式：实测
    # 广告列只有部分历史行带 0.00_，整字典投票时「只有对齐」那一版反而赢，新行又
    # 退回通用格式。而管线自己写出来的行恰恰是「只有对齐、没有数字格式」那一类，
    # 让它们参与数字格式的投票等于让历史包袱决定新行长什么样。
    numfmt_votes: Dict[str, Counter] = {}
    align_votes: Dict[str, Counter] = {}
    for row in sample:
        for col, cell in row.items():
            if col == image_col:
                continue
            fmt = cell.get("format")
            if not isinstance(fmt, dict) or not fmt:
                continue
            if fmt.get("numfmt"):
                numfmt_votes.setdefault(col, Counter())[fmt["numfmt"]] += 1
            align = {k: fmt[k] for k in ("alcH", "alcV") if k in fmt}
            if align:
                key = json.dumps(align, sort_keys=True)
                align_votes.setdefault(col, Counter())[key] += 1

    format_columns = {}
    for col in set(numfmt_votes) | set(align_votes):
        xf = {}
        if col in align_votes:
            xf.update(json.loads(align_votes[col].most_common(1)[0][0]))
        if col in numfmt_votes:
            xf["numfmt"] = numfmt_votes[col].most_common(1)[0][0]
        format_columns[col] = xf

    return SheetSchema(
        sheet=sheet,
        fields=fields,
        formula_columns=formula_columns,
        constant_columns=constant_columns,
        format_columns=format_columns,
        ok=True,
        header_row=header_row,
    )


def _cloud_first_row(cloud, sheet: str, schema: SheetSchema,
                     insert_at_top: bool, at_row: Optional[int] = None,
                     up_count: int = 0) -> int:
    """本批新行的起始行号（1-based，用于公式模板的 {r}）。

    公式必须在写入【之前】按真实落点渲染，所以这里要先把落点算出来——与
    KdocsSheet.write_rows 内部同一套口径。两处口径若漂移，公式会指向错行、算出别人家的值。

    - at_row 为 None：插顶端=表头正下方；追加=数据区末行之后（空表退回表头正下方）。
    - at_row 有值：就用它（「从第 R 行向下插」）。
    - up_count>0：「向上插」——新行要落在 at_row 之前，故起点 = at_row - n，
      但不得越过表头（挤到表头上会写坏标题行），越界则从表头下一行开始。
    """
    if at_row is not None:
        if up_count > 0:
            return max(at_row - up_count, schema.header_row + 1)
        return at_row
    if insert_at_top:
        return schema.header_row + 1
    end = cloud.data_end_row(sheet)  # 0-based
    return max(end + 2, schema.header_row + 1)  # 0-based 末行 +1 行 → 1-based 再 +1


async def write_product_row_cloud(
    cloud,
    sheet: str,
    item: dict,
    res: CollectResult,
    schema: SheetSchema,
    insert_at_top: bool = False,
    at_row: Optional[int] = None,
    up_count: int = 0,
) -> tuple[bool, str]:
    """云端协作文档版 write_product_row：单行写入，值+公式同批，
    主图走 item["image"] 的在线 URL 嵌入（不经本地下载，KdocsSheet.write_rows 内部
    已做 avif→jpeg 转换）。

    落点：at_row 有值则钉死在那一行（采集页「从第 R 行插」，up_count>0 表示向上插）；
    否则 insert_at_top=True 插到表头正下方、False 追加到数据区末尾（默认）。

    KdocsSheetError → 返回 (False, msg)，单商品失败不连坐（与本地「写入失败」同口径）。
    """
    from app.orders.kdocs_sheet import KdocsSheetError

    if not schema.ok:
        return False, schema.error
    try:
        first_row = await asyncio.to_thread(
            _cloud_first_row, cloud, sheet, schema, insert_at_top, at_row, up_count
        )
        row = _cloud_row(item, res, schema, 0, first_row)
        await asyncio.to_thread(
            cloud.write_rows, sheet, [row], schema.header_row, insert_at_top,
            first_row if at_row is not None else None,
        )
    except KdocsSheetError as e:
        return False, f"写入失败：{e}"
    return True, "已写入"


def _cloud_row(item: dict, res: CollectResult, schema: SheetSchema,
               offset: int, first_row: int) -> dict:
    """组装 write_rows 需要的一行。

    offset = 该行在本批里的序号（0 起）；first_row = 本批第一行的 1-based 行号。
    公式行号必须按 offset 递增：一批 n 行占 first_row .. first_row+n-1，若全用
    first_row，n 行公式会齐刷刷指向第一行、算出同一个值。
    """
    values = _build_column_values(item, res, schema)
    # 公式放最后：write_rows 的读回校验取 values 里第一个非空值比对 cellText，
    # 公式格的 cellText 是计算值而非公式串，放前面必误判「写入验证失败」。
    for col, tpl in schema.formula_columns.items():
        values[col] = tpl.format(r=first_row + offset)
    return {
        "values": values,
        "formats": schema.format_columns,
        "image_column": schema.fields.get("image"),
        # 优先用该 SKU 自己的预览图：一个 SKU 一行后，同 SPU 的不同颜色行若都嵌 SPU 主图，
        # 表里看不出行与行的差别。sku_image 在枚举时已按 skuPreviewImage → SKC 预览图 →
        # SPU 主图逐级取好（见 service._FETCH_ALL_JS），这里只兜老清单没有该字段的情况。
        "image_url": item.get("sku_image") or item.get("image"),
    }


async def write_product_rows_cloud(
    cloud,
    sheet: str,
    items: list,
    schema: SheetSchema,
    insert_at_top: bool = False,
    at_row: Optional[int] = None,
    up_count: int = 0,
) -> tuple[bool, str, list]:
    """把整批商品【一次】写进协作文档，返回 (整批是否成功, 说明, 确认落表的 SPU 列表)。

    为什么要批量：逐商品写一行要 5~6 次 kdocs 调用（插行/文本/图片/读回校验/确认），
    而 write_rows 本身支持多行——内部把文本攒到 500 格一批、图片 10 张一批，写 20 行
    也就 6 次。实测 20 个商品从 124 次降到 10 次。kdocs 有配额（429001 限频要等 20s、
    429002 直接熔断），这个量级的差距决定了整批能不能一次跑完。

    【原子性是刻意的「一坏全坏」】一批里任一行文本写失败/读回校验不过，整批算失败并
    落日志，不做「挑出坏的再写剩下的」——那样表里会留下半批数据，而判重键已落表，
    重跑时这半批被当成已入库跳过，人工很难看出哪几行是残缺的。整批失败则一行不落，
    重跑即可，语义干净。图片失败仍只告警不连坐（沿用 write_rows 的既定取舍：
    文本已登记就算这行成立，图片格留空可人工补，见 kdocs_sheet.write_rows）。

    insert_at_top 决定落点：True 插到表头正下方，False 追加到数据区末尾。

    确认口径：只读新行区那 n 格（read_new_rows_column），不再拉整列。
    """
    from app.orders.kdocs_sheet import KdocsSheetError

    if not schema.ok:
        return False, schema.error, []
    if not items:
        return True, "本批无商品", []

    spu_col = schema.fields.get("spu")
    if not spu_col:
        return False, "schema 缺 SPU 列，无法确认写入", []

    try:
        # 落点要在渲染公式【之前】定下来（追加模式下依赖当前末行），并复用给写后确认——
        # 若确认时重新算一次，写入已让 row_to 增长，会读到本批之后的空白区。
        first_row = await asyncio.to_thread(
            _cloud_first_row, cloud, sheet, schema, insert_at_top, at_row, up_count
        )
        rows = [
            _cloud_row(
                item,
                CollectResult(spu=str(item.get("spu", "")), ok=True,
                              note="采购价/重量待人工填"),
                schema, offset, first_row,
            )
            for offset, item in enumerate(items)
        ]
        await asyncio.to_thread(
            cloud.write_rows, sheet, rows, schema.header_row, insert_at_top,
            first_row if at_row is not None else None,
        )
    except KdocsSheetError as e:
        return False, f"整批写入失败（一行不落，可直接重跑）：{e}", []

    # 写后确认：读新行区的 SPU 列，与本批 SPU 逐行比对（顺序与写入同序）
    try:
        got = await asyncio.to_thread(
            cloud.read_new_rows_column, sheet, spu_col, first_row, len(rows)
        )
    except KdocsSheetError as e:
        return False, f"整批写后确认失败：{e}", []

    expect = [str(it.get("spu", "")).strip() for it in items]
    missing = [s for s, g in zip(expect, got) if s != g]
    if missing:
        return False, (
            f"整批写后确认不一致：{len(missing)} 行 SPU 与预期不符"
            f"（期望 {expect[:3]}… 实际 {got[:3]}…）"
        ), []
    return True, f"整批已写入 {len(rows)} 行", expect
