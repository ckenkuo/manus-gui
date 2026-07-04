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
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.collect.image_extract import extract_white_bg
from app.config import config
from app.llm import LLM
from app.logger import logger
from app.schema import Message

# ---- 已实测确认的选择器（勿凭记忆改；改前对真站重验）----------------------
# 结果页单个商品卡片容器（s.1688.com 图搜/关键词结果页通用）。
_CARD_SELECTOR = ".search-offer-wrapper"
# 详情页价格：主价区间 + 各 SKU 价/库存。
_DETAIL_PRICE_JS = r"""
() => {
  const pick = (sel) => {
    const el = document.querySelector(sel);
    return el ? (el.innerText || '').replace(/\n{2,}/g, '\n').trim() : '';
  };
  const skuBlocks = [...document.querySelectorAll('.item-price-stock')]
      .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim())
      .filter(Boolean);
  return {
    mainPrice: pick('.module-od-main-price'),
    skuText: pick('.module-od-sku-selection'),
    skuPriceStock: skuBlocks,
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
    "你是采购价核算助手。给你一个 1688 商品详情页的价格文本块（含主价区间与各规格 SKU 的价/库存）。"
    "请判断该商品的【常规批发价】——务必剔除首单价/新人价/限时价/优惠券等一次性优惠，取可持续拿到的常规价。"
    "并据商品推测运费与单件估重（克）。返回严格 JSON："
    "{\"purchase_price\": <数字, 常规批发单价元>, \"shipping\": <数字, 运费元, 不确定填0>, "
    "\"weight_g\": <数字, 单件估重克>, \"note\": \"<存疑点, 无则空串>\"}。只输出 JSON，勿加围栏或解释。"
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
    page = await _get_page(browser_tool)

    async def _read_and_judge() -> Optional[dict]:
        cands = await read_search_results(browser_tool, 8)
        if not cands:
            return None
        return await judge_same_match(target_img, cands)

    # 1) 默认主体（paste_image 落地即此，已加载好、卡片带链接）
    chosen = await _read_and_judge()
    if chosen:
        return chosen

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
        logger.info(f"search_match_over_regions：试主体框 {key}")
        chosen = await _read_and_judge()
        if chosen:
            return chosen
    return None


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
    return chosen


async def judge_price(price_block: dict, config_name: str = "default") -> Optional[dict]:
    """判断点B（文本）：价格块 → {purchase_price, shipping, weight_g, note}。"""
    parts = []
    if price_block.get("mainPrice"):
        parts.append("主价区间：\n" + price_block["mainPrice"])
    if price_block.get("skuText"):
        parts.append("规格区：\n" + price_block["skuText"][:600])
    if price_block.get("skuPriceStock"):
        parts.append("各SKU价/库存：\n" + " | ".join(price_block["skuPriceStock"][:20]))
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


async def collect_one_product(browser_tool, item: dict) -> CollectResult:
    """确定性采集单个商品（不写 Excel，由调用方决定落库）。

    任一确定性步骤失败即抛/记 fail_reason，交上层重试或退回 agent 兜底。
    """
    spu = str(item.get("spu", ""))
    res = CollectResult(spu=spu, ok=False)

    img_dir = config.output_dir("image")
    img_path = os.path.join(str(img_dir), f"{spu}.jpeg")

    # 1. 主图（调用方通常已下好；缺则直连下载）
    if not os.path.exists(img_path) and item.get("image"):
        try:
            import requests

            os.makedirs(str(img_dir), exist_ok=True)
            r = requests.get(item["image"], timeout=30)
            r.raise_for_status()
            with open(img_path, "wb") as f:
                f.write(r.content)
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
    r = await browser_tool.execute(action="go_to_url", url="https://www.1688.com/")
    if r.error:
        res.fail_reason = f"打开 1688 失败：{r.error}"
        return res
    r = await browser_tool.execute(action="paste_image", file_path=search_img)
    if r.error:
        res.fail_reason = f"图搜失败：{r.error}"
        return res

    # 3+4. 挑同款。【红线】判同款始终用原图：白底图是重绘、可能改动商品外观，只影响
    #      recall；精度必须锚在真实像素（item["image"]）上。
    target_img = item.get("image", "") or img_path
    if got_white:
        # 查询图已干净 → 1688 默认主体即准，不必再跟它的框较劲（框只对脏拼图有用）。
        # 直接读默认结果判同款。
        cands = await read_search_results(browser_tool, 8)
        chosen = await judge_same_match(target_img, cands) if cands else None
        had_candidates = bool(cands)
    else:
        # 退回脏拼图搜 → 保留原有主体框 dance 兜底（默认主体常框到脸/手，漏真商品）。
        chosen = await search_match_over_regions(browser_tool, target_img)
        had_candidates = True  # dance 内部已尽力枚举各框，保持原「无同款」语义
    if not chosen:
        # 有候选但无同款 → no_same_match=True：clean query 下 agent 也不会更好，记漏采、
        #   跳过 agent 兜底（省批次时间）。
        # 0 候选（白底图可能生崩/过裁）→ no_same_match=False：放行 agent 用原图兜底。
        res.fail_reason = "未匹配到同款" if had_candidates else "白底图搜无候选"
        res.no_same_match = had_candidates
        return res
    res.offer_id = chosen.get("offerId")
    res.detail_url = chosen.get("detailUrl")

    # 5. 详情页读价
    price_block = await read_detail_price(browser_tool, res.detail_url)
    if not price_block or not price_block.get("mainPrice"):
        res.fail_reason = "详情页未读到价格"
        return res

    # 6. 判断点B：文本读价
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


# 列映射照本表表头（实测行1）：站点A/类目B/【产品图片E】/SPU D/销售价I/日常价G/
# 采购价J/重量K(公斤)/ros O=7；公式列 H/L/N/P/Q/R 照 inspect 最后一行同列公式（须把
# 字面行号换成 {r} 才会随行自适应）。注意：图片列是 E（产品图片），F 是货号，勿写图。
_FORMULA_COLS = ["H", "L", "N", "P", "Q", "R"]
_IMAGE_COLUMN = "E"  # 产品图片列（表头行1实测；F=货号，历史459张图全在E）


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


async def write_product_row(
    excel_tool,
    excel_path: str,
    sheet: str,
    item: dict,
    res: CollectResult,
    image_path: str,
) -> tuple[bool, str]:
    """把采集结果写入 Excel（inspect 取公式模板 → append_product_row）。

    返回 (是否成功, 消息)。存疑（res.note 非空）时写入备注列 T，不阻断。
    """
    inspect = await excel_tool.execute(
        action="inspect", file_path=excel_path, sheet_name=sheet
    )
    if inspect.error:
        return False, f"inspect 失败：{inspect.error}"
    try:
        info = json.loads(inspect.output)
    except Exception as e:
        return False, f"inspect 输出解析失败：{e}"
    sample = info.get("sample_最后行公式与值", {})
    # inspect 采到的是最后数据行的字面公式（如 "=I575/G575"）。必须把公式里【所有相对
    # 单元格引用】的行号换成 {r} 占位符，_append 的 .format(r=新行号) 才能让公式随行自适应；
    # 否则新行公式冻在旧行号。
    # 【关键教训】不能假设引用行号 = 最后数据行去替换：采样的那行本身可能是历史坏行、
    # 公式冻在更早行号（实测从 575 行采到的却是 I543/G543）→ 按 575 替换什么都换不到。
    # 故按【实际出现的引用行号】通配替换，谁在换谁。
    # (?<![A-Za-z$]) 避开函数名尾随数字与绝对引用（$G$1）；常数（如 *80，无字母前缀）不误伤。
    formula_columns = {}
    for c in _FORMULA_COLS:
        f = sample.get(c)
        if not f or not str(f).startswith("="):
            continue
        formula_columns[c] = re.sub(r"(?<![A-Za-z$])([A-Z]{1,3})\d+", r"\1{r}", str(f))

    # 采购价/重量：有值就写，无值（如所有主体框均未匹配的快速失败）留空待人工补。
    if res.purchase_price is not None:
        purchase_cell = round(float(res.purchase_price) + float(res.shipping or 0), 2)
    else:
        purchase_cell = ""
    # 价格剥¥转数字（否则 H=I/G 折扣公式崩）；解析不出则留空、不写脏字符串。
    sale_price = _to_number(item.get("price"))
    sale_cell = sale_price if sale_price is not None else ""
    # 重量：judge_price 返回【克】，本表 K 列是【公斤】（历史 0.3kg→L=K*80+1=25 吻合），克÷1000。
    if res.weight_g is not None:
        try:
            weight_cell = round(float(res.weight_g) / 1000.0, 3)
        except (TypeError, ValueError):
            weight_cell = ""
    else:
        weight_cell = ""
    column_values = {
        "A": item.get("site", ""),
        "B": item.get("category", ""),
        "D": res.spu,
        "I": sale_cell,
        "G": sale_cell,  # 日常价：无独立来源时暂用销售价，同旧 prompt 行为
        "J": purchase_cell,
        "K": weight_cell,
        "O": 7,  # ros
    }
    note = res.note or ""
    if note:
        column_values["T"] = note  # 存疑标记，不阻断

    kwargs = dict(
        action="append_product_row",
        file_path=excel_path,
        sheet_name=sheet,
        column_values=column_values,
        formula_columns=formula_columns,
    )
    if os.path.exists(image_path):
        kwargs["image_path"] = image_path
        kwargs["image_column"] = _IMAGE_COLUMN  # E=产品图片（勿写 F 货号列）

    r = await excel_tool.execute(**kwargs)
    if r.error:
        return False, f"写入失败：{r.error}"
    return True, r.output or "已写入"
