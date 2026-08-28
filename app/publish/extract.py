"""商品源页信息提炼（发布管线阶段①）：抓页面数据 + 下载主图/详情图 + 落盘。

【2026-08-27 起支持多来源】原先只做 1688；实测采集箱 200 条草稿里非 1688 源占 76 条
（拼多多 42 / Temu 30 / 亚马逊 4），它们在 GUI 上一条都跑不通。现在按 URL 域名分派到
app/publish/sources/ 下的平台适配器，本模块只保留【与平台无关】的那半：下图、
main-NN.jpg 命名、成分解析、素材图合规判断、product-info.json 落盘、视觉回填。
1688 的取数逻辑仍在本模块（_JS_EXTRACT / _JS_DETAIL / parse_attrs /
wait_human_verify），由 sources/alibaba1688.py 薄包装调用——那些是对真站逐个试出来
的、且已有单测覆盖，搬动只有风险没有收益。新增平台不必碰本模块。

从 skill 的 scripts/extract_1688.py 移植。改动只有三处，其余（两段 JS、属性切分、
SKU 透视、素材图合规判断）原样保留——那些 JS 选择器和键名表是对真站实测出来的，
凭记忆改必翻车。

改动一：浏览器层从 WebBridge 换成 app/publish/browser 的 Playwright over CDP。
改动二：图片下载从裸 urllib 换成带浏览器头 + 指数退避重试的实现。
    理由与 collect 侧同一个坑（见 app/collect/pipeline._download_main_image）：CDN 对
    无 UA/Referer 的请求做 bot 拦截，表现为连接重置或 403。原脚本只带了个 "Mozilla/5.0"
    的裸 UA、且零重试，一次瞬时重置就丢一张图。
改动三：输出目录走 config.get_output_dir("publish")，落到桌面 manus输出/ 分类目录下，
    不再是原脚本写死的 D:\\KimiData\\kimi\\workspace（换机器即失效）。

产出（工作目录 product-<id>/，非 1688 源为 product-<平台>-<id>/，见 workdir_for）：
    raw.json           页面原始抽取结果，留档便于排查
    product-info.json  结构化商品信息，后续 11 个阶段都读它
    main-NN.jpg        轮播主图
    desc-NN.jpg        详情长图
product-info.json 里 sizeChart / sizeMeasurements / imageUnderstanding /
complianceNotes 四个字段是【看图占位】：由本模块的 enrich_vision 用视觉模型回填
（默认不在提取主流程里，见该函数注释），也可人工补（见 SKILL.md 阶段①的看图要点）。
"""
import asyncio
import json
import os
import re
from typing import Any, Callable, Optional

from app.config import config, get_output_dir
from app.logger import logger
from app.publish.browser import J, BrowserSession
from app.publish import images
from app.publish.sources import get_adapter
from app.publish.sources.base import (
    UnsupportedSourceError,
    detect_platform,
    normalize_url,
    platform_name,
    source_id,
)

# 1688 商品参数常见键名（按长度降序匹配，避免 "主面料成分" 截获 "主面料成分含量"）。
# 这张表是对真实商品页面攒出来的，缺键会导致该属性并进上一个键的值里，别精简。
KNOWN_ATTR_KEYS = [
    "是毛头/杂余线头是否修剪", "毛头/杂余线头是否修剪", "主面料成分含量", "是否跨境出口专供货源",
    "AQL抽检标准", "主面料成分", "适合年龄段", "套装件数", "面料名称", "面料工艺",
    "货源类别", "货源类型", "适用性别", "套装类型", "礼盒内容", "适合季节", "是否连帽",
    "上市年份季节", "闭合款式", "安全等级", "平车针距", "是否IP授权",
    "图片实拍", "是否库存", "适合身高", "厚薄", "袖长", "裤长",
    "品牌", "货号", "产地", "风格", "图案", "元素", "颜色",
    "领标", "吊牌",
]

# 值里可能整段包含另一个已知键名的「陷阱值」白名单（值 → 它内含的键）。
# 例：货源类型的值「其他品牌」内含键「品牌」，切分时会被当成下一个键的起点，
# 把值截成「其他」；「源头工厂」「服装」等不含键名的值不受影响。
# 只列实测见过的，宁缺勿滥——误列会把真的键当值吞掉。
_VALUE_TRAPS = ("其他品牌", "有领标", "无领标", "有吊牌", "无吊牌")

# 商品主数据挂在 window.context.result.data（1688 详情页服务端注入）。
# 商品参数没有稳定容器，故用「找含已知键名的可见容器 + 按信息量打分」的启发式：
# 最短容器可能只是摘要会截断，最长的是整页噪音，故按命中键数降序、再按长度升序取第一个。
_JS_EXTRACT = """(() => {
  const d = (((window.context||{}).result)||{}).data;
  if (!d) return JSON.stringify({found: false});
  const gal = (d.gallery||{}).fields || {};
  const pack = (d.productPackInfo||{}).fields || {};
  const desc = (d.description||{}).fields || {};
  const skuMap = (((((d.mainPrice||{}).fields||{}).finalPriceModel||{})
      .tradeWithoutPromotion||{}).skuMapOriginal||[])
    .map(s => ({spec: (s.specAttrs||'').replace(/&gt;/g, '>'),
                price: s.discountPrice || s.price, stock: s.canBookCount}));
  const els = Array.from(document.querySelectorAll('div, section, ul'));
  const cands = els.filter(el => el.offsetHeight > 0)
    .map(el => (el.textContent||'').replace(/\\s+/g, ' ').trim())
    .filter(t => t.length > 30 && t.length < 3000 &&
             (t.includes('货号') || t.includes('适用性别') || t.includes('主面料')));
  const KEYS = ['主面料成分','适合年龄段','套装件数','厚薄','面料名称','袖长','裤长','品牌','货号','适用性别','风格','产地','适合季节','图案','颜色','适合身高','安全等级','闭合款式'];
  const score = t => KEYS.reduce((n, k) => n + (t.includes(k) ? 1 : 0), 0);
  cands.sort((a, b) => score(b) - score(a) || a.length - b.length);
  return JSON.stringify({
    found: true,
    subject: gal.subject || document.title || null,
    images: (gal.offerImgList||[]).map(i => i && (i.url || i.imageUrl || i.fullPathImageURI || i)),
    unitWeight: pack.unitWeight != null ? pack.unitWeight : null,
    detailUrl: desc.detailUrl || null,
    skuMap: skuMap,
    attrText: cands.length ? cands[0] : null
  });
})()"""

# 描述长图不在页面 DOM 里，要拉 description.detailUrl 那个接口再正则抠图片 URL。
# 在页面上下文里 fetch（而不是 Python 直连）是因为该接口认来源；credentials:'omit'
# 是原脚本实测的写法，带 cookie 反而可能被拒。
_JS_DETAIL = """(async () => {
  const r = await fetch(__URL__, {credentials: 'omit'});
  const t = await r.text();
  const m = t.match(/https?:\\/\\/[^"'\\s\\\\]+\\.(jpg|jpeg|png|webp)/gi);
  const imgs = m ? [...new Set(m)] : [];
  return JSON.stringify({status: r.status, len: t.length, imgs: imgs});
})()"""

# 描述图 URL 的抠取规则，与上面 JS 里那条正则保持一字不差：两条取图路径
# （页面内 fetch / Python 直连）必须抠出同一批图，否则同一商品换条路径结果就变。
_RE_DESC_IMG = re.compile(r"""https?://[^"'\s\\]+\.(?:jpg|jpeg|png|webp)""", re.I)

# 图片 CDN 的 bot 拦截规避（同 collect 侧的坑）：裸请求会连接重置或 403。
_IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://detail.1688.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def workdir_for(product_id: str, platform: str = "1688") -> str:
    r"""单品工作目录：桌面 manus输出/商品发布/product-<id>/（非 1688 源带平台前缀）。

    走 get_output_dir 而非写死路径：原脚本的 D:\KimiData\kimi\workspace 是作者机器的
    路径，换机器/换账号第一次跑就报文件不存在。get_output_dir 本身 best-effort
    （桌面不可写会退到项目 workspace/输出），坏不了主流程。

    【1688 刻意不带前缀】既有目录全是 product-<offerId>/ 这个形态，加前缀会让所有
    历史工作目录与状态文件里的 workdir 失配（续跑时找不到已下好的图，白重下一遍）。
    新平台带前缀（product-pdd-<goodsId>/）则是必须的——不同平台的商品 ID 可能撞号
    （拼多多 goods_id 与 1688 offerId 都是纯数字，位数也重叠），撞了就是两个商品
    共用一个目录、图互相覆盖。
    """
    slug = f"{product_id}" if platform == "1688" else f"{platform}-{product_id}"
    return str(get_output_dir("publish") / f"product-{slug}")


def parse_attrs(attr_text: Optional[str]) -> dict:
    """把 '主面料成分棉适合年龄段中小童...' 这类无分隔文本按已知键名切开。

    1688 的商品参数容器取 textContent 后键值完全粘连、没有任何分隔符，故只能靠已知
    键名表做最长匹配切分：每个键的值 = 到下一个已知键出现位置为止。
    """
    if not attr_text:
        return {}
    # 去掉前缀噪音（视频播放器文案等），从第一个已知键开始
    starts = [attr_text.find(k) for k in KNOWN_ATTR_KEYS if attr_text.find(k) >= 0]
    if starts:
        attr_text = attr_text[min(starts):]
    keys_sorted = sorted(KNOWN_ATTR_KEYS, key=len, reverse=True)
    result: dict = {}
    i = 0
    while i < len(attr_text):
        hit = None
        for k in keys_sorted:
            if attr_text.startswith(k, i):
                hit = k
                break
        if not hit:
            i += 1
            continue
        j = i + len(hit)
        # 值的终点 = 下一个已知键出现处，但要跳过「陷阱值」内部的键名：
        # 「货源类型其他品牌吊牌...」里，紧跟其后的「品牌」属于值「其他品牌」的一部分，
        # 不是下一个键的起点。命中陷阱值时直接把终点推到该值末尾之后再重找。
        trap = next((t for t in _VALUE_TRAPS if attr_text.startswith(t, j)), None)
        scan_from = j + len(trap) if trap else j
        nxt = len(attr_text)
        for k in keys_sorted:
            p = attr_text.find(k, scan_from)
            if 0 <= p < nxt:
                nxt = p
        val = attr_text[j:nxt].strip()
        if hit not in result:
            result[hit] = val
        i = nxt
    return result


# 1688 的「主面料成分含量」写法不统一，实测见过 "55（%）" 和
# "90%（含）-95%（不含）（%）" 两种。区间一律取【下界】、小数向下取整：下界是卖家
# 承诺的最低含量，往高报在 Temu 属实质合规风险；且取下界必然给补差纤维留出份额，
# 不会出现补 0% 的空行。
_PCT_RE = re.compile(r"\d+(?:\.\d+)?")

# 源页面完全没写主面料成分时的默认纤维（2026-08-25 用户确认）。
# 聚酯纤维是跨境服装最常见的面料，写它比让模型凭图猜一种更可复核；
# 与 pipeline._COMP_FILLERS 的首选保持一致，避免「默认主成分」与「补差纤维」撞成同一根。
COMP_DEFAULT_FIBER = "聚酯纤维"


def parse_main_composition(attrs: dict) -> dict:
    """把源属性里的主面料成分与含量提炼成结构化字段，供阶段④做确定性覆盖。

    为什么要单独提炼：卖家填的「主面料成分含量」是源页面上的确定事实，但整个
    attributes 原先只是 json.dumps 塞进属性审核的 prompt，成分百分比实际由 LLM
    单次判断决定——pipeline.check_attrs 注释里记的「55/45 变成 90/10」漂移正是
    这么来的。提炼出来后，写入前可以直接用源值覆盖模型填的数字，不再靠模型复读。

    【2026-08-25 改：缺失时不再返回 {}，而是给确定性默认值】用户结论——源页面一般
    只写一个含量（如聚酯纤维 90%），剩下的份额靠推断；**源什么都没写时一律按
    聚酯纤维 100% 处理**，不要退回让模型自己编一组比例。故三种情形：
      1. 纤维 + 合法含量  → 按源值（percent < 100 时剩余份额由阶段④补差纤维）
      2. 有纤维、含量缺失或非法 → 该纤维 100%（源信息里有的就用，只是没给比例）
      3. 连纤维都没有      → 聚酯纤维 100%（assumed=True，最常见的跨境服装面料）
    情形 2/3 打 assumed 标记，供提示词与日志区分「源事实」和「默认值」。
    """
    attrs = attrs or {}
    fiber = (attrs.get("主面料成分") or attrs.get("面料名称") or "").strip()
    raw = (attrs.get("主面料成分含量") or "").strip()
    m = _PCT_RE.search(raw) if raw else None
    pct = int(float(m.group())) if m else 0
    if fiber and 0 < pct <= 100:
        return {"fiber": fiber, "percent": pct, "raw": raw}
    if fiber:
        return {"fiber": fiber, "percent": 100, "raw": raw,
                "assumed": "源未给含量，按单一成分 100% 处理"}
    return {"fiber": COMP_DEFAULT_FIBER, "percent": 100, "raw": raw,
            "assumed": "源未给主面料成分，按默认纤维 100% 处理"}


def pivot_skus(sku_map: list) -> tuple[dict, list, list]:
    """'6633-灰色>100码' → {'100码': {'6633-灰色': 29.8}} + 颜色/尺码列表。

    【不含 `>` 的 spec 仍然丢，但必须出声】spec 的两维形状由各适配器保证
    （见 sources/base.py 的 SourceProduct 契约与 alibaba1688.norm_spec）。这里不再
    补一份归一——两处各归一一遍，改规则时必然只改一边。
    但静默丢是 2026-08-28 那次排查的真正代价：offer 1014675972015（手工编织摆件）
    6 条单维 spec 被这个 continue 全吃掉，落盘成 skus={} / colors=[] / sizes=[]，
    直到阶段⑦ 报「视觉未给出任何颜色行选图」、阶段⑧ 报「无 skus 数据」才暴露，
    而那两条提示都指不回「源 SKU 形状不对」。故丢弃时打 warning 点出具体 spec。
    """
    pivot: dict = {}
    colors: list = []
    sizes: list = []
    dropped: list = []
    for s in sku_map:
        spec = s.get("spec", "")
        if ">" not in spec:
            dropped.append(spec)
            continue
        color, size = spec.rsplit(">", 1)
        color, size = color.strip(), size.strip()
        if color not in colors:
            colors.append(color)
        if size not in sizes:
            sizes.append(size)
        pivot.setdefault(size, {})[color] = float(s["price"]) if s.get("price") else None
    if dropped:
        logger.warning(
            f"skuMap 有 {len(dropped)} 条 spec 不含「>」被丢弃（适配器未归一成"
            f"「颜色>尺码」两维？）：{dropped[:6]}；"
            "这会让 skus/colors/sizes 落成空值，阶段⑦⑧ 随后必挂")
    return pivot, colors, sizes


def image_dims(path: str) -> dict:
    """读取图片宽高（PIL 不可用时解析 JPEG/PNG 头兜底）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        return {"w": w, "h": h}
    except Exception:
        pass
    try:
        with open(path, "rb") as f:
            head = f.read(24)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w = int.from_bytes(head[16:20], "big")
                h = int.from_bytes(head[20:24], "big")
                return {"w": w, "h": h}
            if head[:2] == b"\xff\xd8":  # JPEG: 扫 SOF 段
                f.seek(2)
                while True:
                    b = f.read(1)
                    if not b:
                        break
                    if b != b"\xff":
                        continue
                    marker = f.read(1)
                    if marker in (b"\xc0", b"\xc1", b"\xc2"):
                        f.read(3)
                        h = int.from_bytes(f.read(2), "big")
                        w = int.from_bytes(f.read(2), "big")
                        return {"w": w, "h": h}
                    seg_len = int.from_bytes(f.read(2), "big")
                    f.seek(seg_len - 2, 1)
    except Exception:
        pass
    return {"w": None, "h": None}


def check_material_image(main_entries: list) -> dict:
    """素材图合规判断：店小秘素材图自动取轮播图/颜色图第一张（= main-01），
    要求 1:1 比例且不小于 800x800。返回是否需调图片修改技能及原因。"""
    first = next((e for e in main_entries if e.get("file")), None)
    if not first:
        return {"needsProcessing": None, "reason": "无主图（--no-images 或下载失败）"}
    w, h = first.get("w"), first.get("h")
    result = {"sourceFile": first["file"], "w": w, "h": h}
    if not w or not h:
        return {**result, "needsProcessing": None, "reason": "尺寸读取失败，需人工看图确认"}
    square = abs(w / h - 1) < 0.01
    big_enough = w >= 800 and h >= 800
    if square and big_enough:
        return {**result, "needsProcessing": False,
                "reason": f"{w}x{h} 满足 1:1 且 >=800x800，可直接用"}
    reasons = []
    if not square:
        reasons.append(f"比例 {w}:{h} 非 1:1，需裁方")
    if not big_enough:
        reasons.append(f"{w}x{h} 小于 800x800，需放大")
    return {**result, "needsProcessing": True,
            "reason": "；".join(reasons) + "（需调图片修改技能处理）"}


def _download_image(url: str, dst_path: str, retries: int = 3) -> int:
    """下载图片到 dst_path，带浏览器头 + 指数退避重试，返回字节数。

    与 collect 侧同一个坑（见 app/collect/pipeline._download_main_image）：CDN 对
    无 UA/Referer 的请求做 bot 拦截，表现为连接重置或 403。原脚本只带裸 UA + 零重试，
    一次瞬时重置就丢一张图。这里补齐浏览器头 + 指数退避重试。
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
            return len(r.content)
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = (0, 1, 3)[min(attempt, 2)]
                logger.warning(f"图片下载失败（{attempt}/{retries}）：{e}；{wait}s 后重试")
                time.sleep(wait)
    raise RuntimeError(f"重试 {retries} 次仍失败：{last_err}")


def _fetch_desc_imgs_direct(detail_url: str, retries: int = 3) -> list:
    """Python 直连详情接口抠描述图 URL（页面内 fetch 被 CORS 拦下时走这条）。

    2026-08-26 实测：detailUrl 有两种形态，老形态不带 CORS 头，页面内 fetch 必挂——
        新 https://itemcdn.tmall.com/1688offer/icoss<hash>          有 access-control-allow-origin: *
        老 https://itemcdn.tmall.com/desc/icoss!<offerId>!<x>?var=desc  无该头
    浏览器读不到跨域响应体，抛的是 TypeError: Failed to fetch——没有状态码、与网络延迟
    无关，重试多少次都一样（故这条路径不是「fetch 慢了」的补救，是换协议栈）。
    老端点【不认来源】：无 UA、无 Referer、无 cookie 裸请求也是 200，所以直连能拿到，
    上面 _JS_DETAIL 那句「该接口认来源」的注释只对新端点成立。

    编码按 GB18030 而非 UTF-8（老端点响应头就是 charset=GB18030，按 UTF-8 解会乱码），
    但只用来抠 ASCII 的图片 URL，故解码 errors='ignore' 足够、不影响结果。
    """
    import time

    import requests

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(detail_url, headers=_IMG_HEADERS, timeout=(10, 30))
            r.raise_for_status()
            text = r.content.decode("gb18030", errors="ignore")
            # dict.fromkeys 而非 set：保持接口返回顺序，描述图顺序即详情页排版顺序
            return list(dict.fromkeys(_RE_DESC_IMG.findall(text)))
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = (0, 1, 3)[min(attempt, 2)]
                logger.warning(f"详情接口直连失败（{attempt}/{retries}）：{e}；{wait}s 后重试")
                time.sleep(wait)
    raise RuntimeError(f"直连重试 {retries} 次仍失败：{last_err}")


# ---- 阶段① 反爬人工验证闸门 -------------------------------------------------
# 1688 详情页在同 IP 高频访问后会弹「滑动验证」（阿里 baxia/nocaptcha），或整页跳到
# punish 页。这两种情况下 window.context.result.data 压根不存在，原先的表现是
# wait_for 干等 40s 然后抛「页面数据未就绪（未登录或被拦截？）」——阶段① 直接失败，
# 而人就坐在那台机器前，本来两秒拖一下滑块就过了。滑块是刻意设计成不可自动化的，
# 也没有绕过它的正当做法，唯一正确的处理就是【停下来喊人】。
#
# 故这里做成显式闸门：检测到验证 → 发 manual_check 事件（Web 页面弹人工检查条目、
# CLI 打「! 人工检查」行）+ warning 日志 + 把页签提到前台，然后原地轮询等到验证过关
# 再继续，而不是失败退出。等待期间不重试导航——punish 页在验证未过时刷新只会再弹一次。
_ANTIBOT_TIMEOUT = 600.0   # 人工过关的最长等待（秒）；超时才把阶段① 判失败
_ANTIBOT_INTERVAL = 3.0    # 轮询间隔：滑块过关后页面几乎立刻恢复，不必更密
_ANTIBOT_REMIND = 30.0     # 每隔多久在日志里重复提醒一次（别每 3s 刷一行）

# 判据三路，任一命中即算被拦：
#   url  ：整页已跳到 punish / captcha / 登录页（_____tmd_____ 是阿里 punish 的固定参数）
#   dom  ：验证浮层挂在当前页上（baxia 对话框 / nocaptcha 容器 / punish iframe）
#   text ：兜住换皮的验证页——【只在商品数据缺失时才看文案】，否则正常详情页里随便
#          一句「验证」都会误判，而误判的代价是把一次能跑通的提取卡成等人工。
# 可见性只看 rect 尺寸（>20px）：nc 容器在正常页面上也可能存在但 display:none，
# 那种 rect 是 0，不该算命中（与本项目其它浮层判据一致）。
_JS_ANTIBOT = r"""(() => {
  const href = location.href;
  const urlHit = /punish|captcha|_____tmd_____|\/\/sec\.|login\.1688\.com/i.exec(href);
  const SEL = '#baxia-dialog, .baxia-dialog, #nc_1_wrapper, .nc-container, #nocaptcha,'
    + ' .nch-container, iframe[src*="punish"], iframe[src*="captcha"], iframe[id*="baxia"]';
  let domHit = '';
  document.querySelectorAll(SEL).forEach(el => {
    if (domHit) return;
    const r = el.getBoundingClientRect();
    if (r.width > 20 && r.height > 20) domHit = el.id || el.className || el.tagName;
  });
  const hasData = !!((((window.context||{}).result)||{}).data);
  const WORDS = ['滑动验证','拖动滑块','请拖动','安全验证','完成验证','智能验证',
                 '验证码','访问被拒绝','环境异常','您的访问'];
  let wordHit = '';
  if (!hasData && document.body) {
    const t = (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 3000);
    wordHit = WORDS.find(w => t.includes(w)) || '';
  }
  return JSON.stringify({
    blocked: !!(urlHit || domHit || wordHit),
    kind: domHit ? 'dom' : (urlHit ? 'url' : (wordHit ? 'text' : '')),
    detail: String(domHit || (urlHit && urlHit[0]) || wordHit || ''),
    hasData: hasData, url: href, title: document.title || ''
  });
})()"""


def _antibot_hint(probe: dict) -> str:
    """把检测结果翻成一句给人看的处置提示（人工要做什么，不是命中了什么选择器）。"""
    detail = str(probe.get("detail") or "")
    if "login.1688.com" in detail:
        return "已跳转到 1688 登录页，需要在该 Chrome 窗口里重新登录"
    if probe.get("kind") == "dom":
        return "页面弹出滑块/安全验证浮层，需要手动拖动滑块过关"
    if probe.get("kind") == "url":
        return f"整页被拦到验证页（{detail}），需要手动完成页面上的验证"
    return f"疑似验证页（页面文案含「{detail}」），需要手动完成验证"


async def wait_human_verify(
    session: BrowserSession,
    url: str,
    on_manual: Optional[Callable[[str], Any]] = None,
    timeout: float = _ANTIBOT_TIMEOUT,
) -> bool:
    """检测 1688 反爬验证；命中则提示人工并原地等到过关。返回「是否等过人工」。

    返回 False 表示没被拦（调用方照原路走），True 表示拦过且已过关（调用方需要重新
    读一次页面数据）。超时抛 RuntimeError，由上层把阶段① 判失败——此时状态文件里
    阶段① 未完成，人工过关后原命令重跑即可（提取是纯只读的，重跑幂等）。

    on_manual 是提示通道（service 层传的是发 manual_check 事件的闭包）。它是辅助路径，
    按本项目惯例 best-effort 吞异常：提示没发出去不该让一次本来能等到人工的提取失败。
    """
    try:
        probe = await session.eval_json(_JS_ANTIBOT)
    except RuntimeError as e:
        # 检测本身失败（导航中途上下文销毁等）不下结论：让调用方照常等数据
        logger.debug(f"反爬检测执行失败，按未拦截处理：{e}")
        return False
    if not probe.get("blocked"):
        return False

    mins = int(timeout // 60)
    message = (f"1688 触发反爬验证：{_antibot_hint(probe)}。"
               f"请切到已打开的 Chrome 窗口手动处理，完成后流程会自动继续"
               f"（最多等 {mins} 分钟，超时本商品判失败可重跑）")
    logger.warning(message)
    if on_manual is not None:
        try:
            r = on_manual(message)
            if asyncio.iscoroutine(r) or isinstance(r, asyncio.Future):
                await r
        except Exception as e:
            logger.warning(f"人工提示回调异常（忽略）：{e}")
    # 把页签提到前台：验证浮层要人拖，页签藏在后面人根本看不见（best-effort）
    try:
        await session.page.bring_to_front()
    except Exception as e:
        logger.debug(f"页签提前台失败（忽略）：{e}")

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_remind = loop.time()
    renav = 0
    while loop.time() < deadline:
        await asyncio.sleep(_ANTIBOT_INTERVAL)
        try:
            probe = await session.eval_json(_JS_ANTIBOT)
        except RuntimeError as e:
            logger.debug(f"反爬轮询失败，继续等：{e}")
            continue
        if not probe.get("blocked"):
            if probe.get("hasData"):
                logger.info("反爬验证已通过，继续提取")
                return True
            # 验证过了但停在中转页（punish 过关后不一定自动回详情页）：导回目标页
            # 再接着轮询。只允许两次，避免验证反复弹时在这里无限刷页。
            if renav < 2:
                renav += 1
                logger.info(f"验证已过但当前页不是详情页，重新导航（第 {renav} 次）")
                await session.navigate(url)
                continue
        now = loop.time()
        if now - last_remind >= _ANTIBOT_REMIND:
            last_remind = now
            logger.warning(f"仍在等待人工完成 1688 验证…剩余 {int(deadline - now)}s")
    raise RuntimeError(
        f"等待人工完成 1688 反爬验证超时（{int(timeout)}s）：{_antibot_hint(probe)}；"
        f"过关后重跑本商品即可")


async def extract_product(
    offer: str,
    session: Optional[BrowserSession] = None,
    outdir: Optional[str] = None,
    with_images: bool = True,
    enrich: bool = False,
    on_manual: Optional[Callable[[str], Any]] = None,
) -> dict:
    """提炼来源平台商品信息 + 下图，产出 raw.json / product-info.json，返回摘要。

    支持 1688 / 拼多多 / Temu / 亚马逊四个来源（见 app/publish/sources/）：平台按
    URL 域名识别，取数交对应适配器，本函数只管【下图与落盘】——那部分与平台无关，
    下游 11 个阶段也只认落盘后的这份结构。认不出的域名抛 UnsupportedSourceError。

    session 为空则自建并在结束时关闭；调用方传入则复用、由调用方负责关闭
    （沿用 collect 侧 run_batch 对 agent 的同一套所有权约定）。

    enrich=True 时接着跑一次视觉回填（见 enrich_vision）。默认关闭：提取是纯只读的
    确定性步骤且已真站验证，视觉判断要花钱、要几十秒、还可能因模型/额度失败，
    不该绑进必经路径。开了也不会因回填失败作废整次抓取（产物已落盘）。

    on_manual 是人工提示通道：源站弹滑块/验证码时用它通知人（1688 会原地等人拖滑块，
    另三家只报错交人处理，见各适配器），不给也照样跑、只是提示仅进日志。

    只读操作：只导航源商品页 + 页面内 fetch + 下载图片，不写任何店小秘数据。
    """
    platform = detect_platform(offer) if offer.startswith("http") else "1688"
    if platform == "1688":
        # 兼容既有用法：offer 可以只给 offerId 数字（CLI 与老状态文件里都有这种形态）
        m = re.search(r"(\d{6,})", offer)
        if not m:
            raise ValueError(f"无法从输入解析 offerId: {offer}")
        product_id = m.group(1)
        url = offer if offer.startswith("http") else \
            f"https://detail.1688.com/offer/{product_id}.html"
    else:
        product_id = source_id(offer, platform)
        if not product_id:
            raise ValueError(
                f"{platform_name(platform)}链接里抽不出商品 ID：{offer}"
                f"（需要形如 goods_id=<数字> / -g-<数字>.html / /dp/<ASIN>）")
        # 【导航一律用原始 URL，不用归一后的】2026-08-27 实测两个反例：
        #   Temu   丢掉 URL 里的标题 slug（.../girls-dresses-g-<id>.html → .../g-<id>.html）
        #          会被重定向到 login.html，页面数据压根不注入
        #   拼多多 只留 goods_id、丢掉 _oak_rcto 等参数后导航，initDataObj 也不注入
        # 归一形态只适合当「同一商品的稳定标识」（日志、目录名、状态键），不适合拿去
        # 访问——平台把那些参数当会话/来源凭证用。故这里 url 保持原样，商品 ID 单独抽。
        url = offer

    # 工作目录名带平台前缀（1688 保持原样不带，见 workdir_for 的说明）
    outdir = os.path.abspath(outdir or workdir_for(product_id, platform))
    os.makedirs(outdir, exist_ok=True)

    adapter = get_adapter(platform)
    own_session = session is None
    session = session or BrowserSession()
    try:
        if own_session:
            await session.open(url_hint=getattr(adapter, "URL_HINT", url))
        # 取数全权交适配器：各平台的挂载点、等待判据、反爬形态都不同
        # （详见各适配器模块的 docstring）
        prod = await adapter.fetch(session, url, on_manual=on_manual)
    finally:
        if own_session:
            await session.close()

    if not prod.title:
        raise RuntimeError(f"{platform_name(platform)}提取到的标题为空（页面改版？）")

    # 下载图片（脱离浏览器会话，纯 requests）
    main_imgs = [u for u in (prod.mainImages or []) if isinstance(u, str)]
    desc_imgs = [u for u in (prod.descImages or []) if isinstance(u, str)]
    downloaded: dict = {"main": [], "desc": []}
    if with_images:
        logger.info(f"下载图片：主图 {len(main_imgs)} 张 + 详情图 {len(desc_imgs)} 张")
        for prefix, imgs in (("main", main_imgs), ("desc", desc_imgs)):
            for i, u in enumerate(imgs, 1):
                fn = os.path.join(outdir, f"{prefix}-{i:02d}.jpg")
                try:
                    size = _download_image(u, fn)
                    entry = {"file": os.path.basename(fn), "bytes": size}
                    if prefix == "main":
                        entry.update(image_dims(fn))  # w/h 供素材图合规判断
                    downloaded[prefix].append(entry)
                except Exception as e:
                    downloaded[prefix].append({"url": u, "error": str(e)})
                    logger.warning(f"{prefix}-{i:02d} 下载失败：{e}")

    # 结构化提炼并落盘（这段与来源平台无关：适配器已把差异吃掉）
    material_check = check_material_image(downloaded["main"])
    attrs = dict(prod.attributes or {})
    main_comp = parse_main_composition(attrs)
    pivot, colors, sizes = pivot_skus(prod.skuMap or [])

    raw = {"offerId": product_id, "productId": product_id, "url": url,
           **prod.as_raw(), "descImages": desc_imgs}
    with open(os.path.join(outdir, "raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    info = {
        # 【offerId 保留】它是 1688 时代的键名，人工排查与既有 product-info.json 都在用；
        # 新增 productId 与 platformName 而不是改名，免得动下游任何一处读法
        # （实测下游没人读 source，但人在看的时候读，见 sources/__init__ 的说明）。
        # url 记【原始链接】（就是实际访问的那个）；canonicalUrl 记归一形态，供人工
        # 复现与跨批次对照。两个都留是因为原始链接里的会话参数会过期（拼多多的
        # 搜索词参数失效后跳首页），而归一形态不能直接访问（见 normalize_url 的说明）。
        "source": {"platform": prod.platform or platform,
                   "platformName": platform_name(prod.platform or platform),
                   "url": url,
                   "canonicalUrl": normalize_url(url, prod.platform or platform),
                   "offerId": product_id, "productId": product_id},
        "title": prod.title,
        "attributes": attrs,
        # 主面料成分/含量：阶段④成分行按它做确定性覆盖，解析不出时按默认纤维兜
        "mainComposition": main_comp,
        "packInfo": {"unitWeightKg": prod.unitWeightKg},
        "skus": pivot,
        "colors": colors,
        "sizes": sizes,
        # 以下四个是【看图占位】，enrich_vision 回填，也可人工补（见 SKILL.md 阶段①看图要点）
        "sizeChart": {},
        "sizeMeasurements": {},
        "imageUnderstanding": {},
        "complianceNotes": {},
        "images": {"dir": os.path.basename(outdir),
                   "main": len(downloaded["main"]), "desc": len(downloaded["desc"]),
                   "mainDetail": downloaded["main"]},
        "materialCheck": material_check,
    }
    info_path = os.path.join(outdir, "product-info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    logger.info(
        f"product-info.json 已落盘：属性 {len(attrs)} 项 / SKU {len(pivot)} 个 / "
        f"颜色 {len(colors)} / 尺码 {len(sizes)}")

    result = {
        "status": "ok", "outdir": outdir, "infoPath": info_path,
        "title": info["title"], "attrCount": len(attrs),
        "platform": prod.platform or platform,
        "platformName": platform_name(prod.platform or platform),
        "productId": product_id,
        "mainComposition": main_comp,
        "skuCount": len(prod.skuMap or []),
        "colors": colors, "sizes": sizes,
        "unitWeightKg": prod.unitWeightKg,
        "videoUrl": prod.videoUrl or "",
        "mainImgs": len(downloaded["main"]), "descImgs": len(downloaded["desc"]),
        "materialCheck": material_check,
    }

    # 视觉回填是【补充增强】：产物已落盘，回填失败不该把整次抓取（含几十张图的下载）
    # 作废，故这里 best-effort 吞异常，但把原因写进 visionError 不静默。
    if enrich and with_images:
        logger.info("视觉回填开始（看图理解，几十秒到几分钟）…")
        try:
            result["vision"] = await enrich_vision(info_path)
        except Exception as e:
            result["visionError"] = str(e)
            logger.warning(f"视觉回填失败（提取产物已保留，可用 enrich-vision 单独重跑）：{e}")

    return result


# ---- 阶段① 视觉回填 -------------------------------------------------------
# product-info.json 里 imageUnderstanding / sizeChart / sizeMeasurements /
# complianceNotes 四个字段原先是空占位：原 skill 的结论是「standalone 不能看图」
# （SKILL.md 阶段①：Packy 的 gpt-4o 系视觉渠道 503 model_not_found，2026-08-19 实测），
# 只能靠 agent 人工看图或人工标注。[llm.publish] 换成 grok-4.6 后同一端点已能收图，
# 于是这几个字段可以自动填了。
#
# 【为什么单独一个函数、默认不进提取主流程】提取（抓页面 + 下图）已真站验证可用且是
# 纯只读确定性步骤；视觉判断要花钱、要几十秒、还可能因模型/额度问题失败。混进必经路径
# 等于让一个已经跑住的步骤多背一个失败源。故做成 enrich_vision()，extract_product 只
# 留一个默认关闭的 enrich 开关。
#
# 【为什么本函数自己抛异常，而 extract_product 的开关处吞】本函数内部是主流程语义：
# 看图结论会一路带到阶段④属性审核、⑤标题、⑥选图，宁可失败让调用方重跑，也不能编个
# 空壳或猜测值写进 product-info.json（同 app/publish/llm.py 开头的说明）。但站在
# extract_product 的角度它只是【补充增强】——抓取产物已经落盘，不该因为补充字段失败
# 就把整次抓取（含几十张图的下载）作废，故那里 logger.warning 吞掉并把错误写进返回
# 摘要的 visionError，不静默。

_VISION_SYSTEM = (
    "你是跨境电商选品与合规审核助手，正在看一个 1688 服装商品的主图和详情长图。"
    "你的输出会直接驱动后续的属性填写、标题生成和素材图挑选，"
    "所以【只描述你在图里真正看到的东西】：图里没有的信息一律留空，绝对不要推测或编造。"
    "只输出 JSON，不要加 ``` 围栏、不要任何解释文字。"
)

_VISION_PROMPT = """商品标题：{title}
源商品属性：{attrs}
源商品颜色（SKU 里的颜色名）：{colors}
源商品尺码（SKU 里的尺码）：{sizes}

下面按顺序给你 {n} 张图，编号与文件名对应：
{listing}

请完成四件事，合并成一个 JSON 返回：

1. imageUnderstanding —— 看懂商品实物：
   - "product"：一句话说明品类/款式/版型（如「男童短袖 POLO 衫 + 长裤两件套，翻领拼色」）
   - "colors"：把上面每个源颜色名映射到你在图里看到的实物描述（配色、图案、细节）。
     认不出某个颜色对应哪张图就不要写那个键。
   - "images"：每个文件名 → 该图内容一句话（模特实拍/平铺/细节特写/尺码表/中文海报…）

2. sizeChart —— 尺码的【身高体重参考】表（通常印在某张详情图上）：
   {{"<尺码>": "<身高/体重参考原文>"}}。
   尺码键用图里的写法但【不要带「码」字】（如 "120" 或 "120cm"，不要 "120码"）。
   图里没有身高体重参考表就返回 {{}}。

3. sizeMeasurements —— 尺码的【平铺实测尺寸】表（衣长/胸围/袖长/裤长等，单位 cm）：
   {{"<尺码>": {{"<参数名>": <数值>}}}}。同样不带「码」字，值只填数字不带单位。
   全围类参数按图里写法照抄参数名，不要自己换算半围/全围。
   图里没有实测尺寸表就返回 {{}}——【不许按经验估算】，估算有后续阶段专门做。

4. complianceNotes —— 逐张标注合规风险（Temu 不接受中文/水印/他人 logo）：
   "files"：[{{"file": "<文件名>", "chinese": true/false, "watermark": true/false,
   "logo": true/false, "kind": "<模特实拍|平铺|细节|尺码表|中文海报|工厂图|其它>",
   "clean": true/false, "note": "<10字内>"}}]
   clean=true 的判据：无任何中文文字、无水印、无他人品牌 logo，且画面就是商品本身
   （阶段⑥ 挑素材图、阶段⑪ 删描述图直接读这个字段）。

只输出 JSON：{{"imageUnderstanding": {{...}}, "sizeChart": {{...}},
"sizeMeasurements": {{...}}, "complianceNotes": {{"files": [...]}}}}"""


def dedup_images(outdir: str) -> tuple[list, dict]:
    """按 md5 去重目录下的 main-NN.jpg / desc-NN.jpg，返回（唯一图路径, 重复映射）。

    去重是【提速手段也是必要手段】：1688 的详情长图常与轮播主图是同一批文件，实测
    product-1073654064193 的 12 个文件里只有 6 张唯一图（desc-01..05 全部与 main 重复）。
    不去重就是白花一倍的图片 token，而图片 token 很贵（count_image 按 512px 瓦片算，
    单张 800×800 约 425 token，真实 base64 更是直接推高请求体）。

    重复映射（{重复文件: 首见文件}）要留着：complianceNotes 得覆盖全部文件名，
    后续阶段⑪判「哪张描述图是重复图该删」正是靠它。

    【顺序刻意是 main 优先，不是文件名字典序】字典序会让 desc-01 排在 main-01 前面，
    于是同一张图的「首见文件」被记成 desc-01、main-01 反而成了重复项。而阶段⑥ 挑素材图
    读的就是 main-NN（素材图取轮播第一张），标注挂在 desc 上它就找不到、还会因为
    duplicate 标记把 main-01 判成不可用。故先按 main/desc 分组再按编号排。

    【两轮去重：md5 精确 + ahash 近重复】md5 只抓字节完全相同的副本，抓不到同一张
    摄影的不同裁切/压缩版本——2026-08-24 实测 product-957056453209 的 main-04 是
    main-06 的 3:4 中心裁切，md5 不同，两张都被挂进同一个 SKC 颜色行（成品页可见
    两张重复图）。故 md5 轮之后补一轮 ahash 近重复检测，理由与阈值见 images.ahash。

    【近重复组保留像素面积最大的那张，不是首见的那张】这是与 md5 轮刻意不同的取向：
    md5 相同意味着字节一致、留谁都一样，所以按 main 优先的首见规则即可；而近重复
    组里各版本尺寸不同，留大图能避开后续 fit_34 放大小图造成的画质损失（实测样本里
    main-06 是 1920×1920 原图、main-04 只有 750×1000）。
    但【main 图优先于 desc 图】仍然压在面积之前——阶段⑥⑦ 只认 main-NN，留下 desc
    会让这批画面对它们彻底不可见（见上一段的字典序踩坑，同一个道理）。
    """
    import hashlib

    files = sorted(
        (f for f in os.listdir(outdir)
         if re.match(r"^(main|desc)-\d+\.(jpg|jpeg|png|webp)$", f, re.I)),
        key=lambda f: (0 if f.lower().startswith("main") else 1, f.lower()),
    )
    seen: dict = {}
    uniq: list = []
    dupes: dict = {}
    for f in files:
        path = os.path.join(outdir, f)
        with open(path, "rb") as fh:
            digest = hashlib.md5(fh.read()).hexdigest()
        if digest in seen:
            dupes[f] = seen[digest]
            continue
        seen[digest] = f
        uniq.append(path)

    uniq, near = _dedup_near(uniq)
    dupes.update(near)
    # md5 轮里指向「被近重复轮淘汰掉的文件」的映射要改指向存活者，否则
    # complianceNotes 里 duplicateOf 会指到一个不在 uniq 里的名字，
    # 阶段⑪ 顺着它找首见文件的标注就找不到。
    for dup, src_name in list(dupes.items()):
        hop = near.get(src_name)
        if hop:
            dupes[dup] = hop
    return uniq, dupes


def _dedup_near(paths: list) -> tuple[list, dict]:
    """对已 md5 去重的图做一轮 ahash 近重复合并，返回（存活路径, {淘汰文件: 存活文件}）。

    分组用朴素两两比较：一批图最多十几张，O(n²) 完全够用，换成聚类反而要处理
    「A 近 B、B 近 C 但 A 不近 C」的传递性歧义——这里刻意让先到的组吸收后来者，
    行为确定且可预期。

    组内择优：main 优先于 desc（阶段⑥⑦ 只认 main-NN），其次像素面积大者，
    最后按文件名兜底保证结果稳定。算不出哈希的图一律单独成组（绝不与任何图判重复），
    理由见 images.ahash。
    """
    hashes = {p: images.ahash(p) for p in paths}
    groups: list = []          # [[路径…]]
    for p in paths:
        hp = hashes[p]
        if hp is None:
            groups.append([p])
            continue
        for g in groups:
            head = g[0]
            hh = hashes[head]
            if hh is not None and images.ahash_distance(hp, hh) <= images.AHASH_MAX_DISTANCE:
                g.append(p)
                break
        else:
            groups.append([p])

    def _rank(p: str) -> tuple:
        name = os.path.basename(p)
        wh = images.image_size(p) or (0, 0)
        return (0 if name.lower().startswith("main") else 1, -(wh[0] * wh[1]), name.lower())

    uniq: list = []
    dupes: dict = {}
    for g in groups:
        if len(g) == 1:
            uniq.append(g[0])
            continue
        keep = min(g, key=_rank)
        uniq.append(keep)
        keep_name = os.path.basename(keep)
        for p in g:
            if p is keep:
                continue
            dupes[os.path.basename(p)] = keep_name
        logger.info(f"近重复合并：保留 {keep_name}，淘汰 "
                    f"{[os.path.basename(p) for p in g if p is not keep]}")
    # 顺序按原 paths 还原：调用方（enrich_vision 的 _listing）依赖 main 在前的编号序
    order = {p: i for i, p in enumerate(paths)}
    uniq.sort(key=lambda p: order[p])
    return uniq, dupes


def _merge_vision(info: dict, vision: dict, dupes: dict) -> dict:
    """把视觉判断结果并进 info 的四个字段，返回回填统计。

    只填【原本为空】的字段：手工补过或上一轮已填的值优先，不被新一轮覆盖——
    人工标注的合规结论比模型的可靠，覆盖掉等于把人的工作抹了。

    重复图（dupes）在这里补齐：模型只看了唯一图，重复文件要继承首见文件的标注，
    并额外标 duplicate=true / clean=false。标 clean=false 是刻意的：重复图对
    阶段⑥⑦⑪ 来说等于「不该再用的图」，让它进不了候选，比留 clean=true 更安全。
    """
    stat = {}
    for key in ("imageUnderstanding", "sizeChart", "sizeMeasurements"):
        got = vision.get(key)
        if isinstance(got, dict) and got and not info.get(key):
            info[key] = got
        stat[key] = len(info.get(key) or {})

    notes = vision.get("complianceNotes") or {}
    files = notes.get("files") if isinstance(notes, dict) else None
    if isinstance(files, list) and files and not (info.get("complianceNotes") or {}).get("files"):
        by_name = {e.get("file"): e for e in files if isinstance(e, dict) and e.get("file")}
        for dup, src in dupes.items():
            base = by_name.get(src)
            if base:
                # file 必须改成 dup 自己的名字：直接 {**base} 会把 src 的 file 带过来，
                # 于是 desc-01 的标注写着 file=main-01，阶段⑥⑦⑪ 按文件名查就查不到。
                by_name[dup] = {**base, "file": dup, "duplicate": True,
                                "duplicateOf": src, "clean": False,
                                "note": f"与 {src} 重复"}
        info["complianceNotes"] = {
            "files": [by_name[k] for k in sorted(by_name)],
            "cleanFiles": sorted(k for k, v in by_name.items() if v.get("clean")),
            "source": "vision",
        }
    cn = info.get("complianceNotes") or {}
    stat["complianceNotes"] = len(cn.get("files") or [])
    stat["cleanFiles"] = len(cn.get("cleanFiles") or [])
    return stat


async def enrich_vision(
    info_path: str, max_images: int = 12, overwrite: bool = False
) -> dict:
    """视觉回填阶段①的四个空占位字段，原地更新 product-info.json，返回统计。

    可单独触发（publish_inspect.py enrich-vision / extract_product(enrich=True)），
    不在提取必经路径上——理由见本节开头。

    max_images 是【一次请求里最多传几张唯一图】的上限。原 skill 的固化做法是「唯一图
    一次请求多张」：分批会让模型看不到全貌（尺码表可能在第 8 张、颜色对应关系要跨图比
    对），所以宁可一次多传。上限存在只为兜住极端长图商品（几十张详情图）撑爆 token；
    截断时保留 main（dedup_images 已按 main 优先排序，轮播主图信息密度高于详情图），
    并在返回里报 truncated。

    overwrite=True 才会重填已有值：默认不覆盖，人工补过的标注比模型的可靠。
    """
    from app.publish.llm import ask_json_with_images

    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)
    outdir = os.path.dirname(os.path.abspath(info_path))

    if overwrite:
        for k in ("imageUnderstanding", "sizeChart", "sizeMeasurements", "complianceNotes"):
            info[k] = {}

    uniq, dupes = dedup_images(outdir)
    if not uniq:
        raise RuntimeError(f"{outdir} 下没有 main-NN / desc-NN 图片，无法看图回填")
    truncated = []
    if len(uniq) > max_images:
        # dedup_images 已按 main 优先返回，直接截尾即保留信息密度最高的轮播主图
        truncated = [os.path.basename(p) for p in uniq[max_images:]]
        uniq = uniq[:max_images]

    names = [os.path.basename(p) for p in uniq]
    listing = "\n".join(f"第{i} 张：{n}" for i, n in enumerate(names, 1))
    prompt = _VISION_PROMPT.format(
        title=info.get("title") or "（无）",
        attrs=json.dumps(info.get("attributes") or {}, ensure_ascii=False),
        colors=json.dumps(info.get("colors") or [], ensure_ascii=False),
        sizes=json.dumps(info.get("sizes") or [], ensure_ascii=False),
        n=len(names), listing=listing,
    )
    vision = await ask_json_with_images(
        prompt, uniq, what="阶段①视觉回填", system=_VISION_SYSTEM, stage="extract"
    )

    stat = _merge_vision(info, vision, dupes)
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    result = {"status": "ok", "infoPath": info_path,
              "uniqueImages": names, "duplicates": dupes, "filled": stat}
    if truncated:
        result["truncated"] = truncated
    logger.info(
        f"视觉回填完成：唯一图 {len(names)}/{len(names) + len(dupes)} 张 | "
        f"颜色理解 {stat['imageUnderstanding']} | 尺码参考 {stat['sizeChart']} | "
        f"实测尺寸 {stat['sizeMeasurements']} | 合规标注 {stat['complianceNotes']} "
        f"（干净图 {stat['cleanFiles']}）"
    )
    return result
