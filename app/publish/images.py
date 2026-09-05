"""商品图片合规化（发布管线阶段⑥⑦⑪用）：纯 Pillow 几何处理 + 可选 AI 编辑。

从 skill 的 scripts/image_cleaner.py 移植。两部分性质完全不同，故这里显式分开：

【纯本地几何处理】fit_34 / square_image / compress —— 无网络、无密钥、可离线单测。
这是发布流程的刚性依赖：服装类图片过不了尺寸校验就会被 Temu 发布弹回，且弹回时
【没有 toast】，只是静默滚到出错区块、右侧锚点变红，极难排查（见 SKILL.md）。
两条硬规则都是实测撞出来的：
  - 素材图/SKC 图通用：不小于 1340×1785（2026-08-18 实测拦截）
  - SKC 颜色图宽高比必须 3:4，素材图仍 1:1（2026-08-19 实测提示「服装类图片宽高比例需要3:4」）

【AI 编辑】edit_image / generate_image —— 走 Packy gpt-image-2，用于中文图英化、
去水印。原脚本把 API key 硬编码进源码兜底，这里改成读 [publish] 配置段（见
resolve_packy_key），没配就直接报错让用户去配，不留内置密钥。

必须保留 curl.exe 子进程的理由（别顺手改成 httpx/requests）：Packy 的 Cloudflare
按 TLS 指纹拦截 urllib/requests，返回 403 error 1010（2026-08-14 实测）。这不是加请求头
能绕的，只有系统 curl 的指纹能过。同理不要加文档示例里的 Host 头，加了反而 403。

/images/edits 的请求形状是实测锁死的（2026-08-26），改前先读 _edits_post_with_retry
上方那段注释：只能 multipart 传 image 文件，不能传 input_fidelity，报错里让你改用
image_url 的那条建议是坏渠道吐的、照做会被网关前置校验打回。
"""
import json
import os
import subprocess
import tempfile
import time
from typing import Optional

from app.logger import logger

# ---- 尺寸硬规则（发布校验实测，勿凭记忆改）--------------------------------
# Temu 服装类图片最小尺寸（2026-08-18 发布校验实测拦截规则）
CLOTH_MIN_W = 1340
CLOTH_MIN_H = 1785
# SKC 服装图宽高比必须 3:4（2026-08-19 实测：比例不对会被拦下并提示
# 「服装类图片宽高比例需要3:4」）。1340×1785 ≈ 3:4（0.7507），两者需同时满足。
SKC_RATIO = 3 / 4
# 长边安全上限（图片 API 上限，也防止放大过头体积爆炸）
MAX_DIM = 3840
# 素材图目标边长：1785 同时满足素材图 ≥800×800 和服装类 ≥1340×1785 两条规则
MATERIAL_TARGET = 1785

# ---- 描述长图（产品描述里的「图片模块」）的规则，与 SKC/素材图【完全不同】-------
# 2026-08-28 用户截图取证：描述图模块弹窗自带说明——
#   「0.5 <= [图片宽 + 图片高] <= 2，宽度 >= 480，高度 >= 480，
#     上传图片大小限制在 10M 以内，发布到 temu 后台时请压缩至 3M 以内」
# 说明里的「图片宽 + 图片高」按其取值范围（0.5~2）只能是【宽高比】，不是相加。
#
# 【为什么必须与 CLOTH_MIN_W/H 分开】原先描述图也套服装 SKC 的 1340x1785 硬红线，
# 于是 1000x1000（比例 1.0、两边都 >= 480，本来完全合格）被判成「低于 1340x1785」，
# 触发放大甚至重新生图——每跑一次白烧一轮生图，而平台压根没这个要求。
# 服装那条红线是「服装类图片尺寸/比例」校验，只管颜色图与素材图，不管描述图。
DESC_MIN_W = 480
DESC_MIN_H = 480
DESC_RATIO_MIN = 0.5
DESC_RATIO_MAX = 2.0
# 体积上限取 temu 后台口径（3M）而不是上传口径（10M）：上传时不拦、发布到 temu
# 才被拦的话要回到这一步重做，代价比一开始就压到 3M 内高。
DESC_MAX_BYTES = 3 * 1024 * 1024


def check_desc_size(w, h, size_bytes=None) -> dict:
    """描述长图合规判断：比例 0.5~2、两边 >= 480、体积 <= 3M。

    返回 {"ok": bool|None, "reasons": [...]}。宽高读不到时 ok=None，交调用方按
    「读不到」处理——把未知判成不合格会触发无谓的放大/重新生图，正是这次要避免的。
    """
    if not w or not h:
        return {"ok": None, "reasons": ["宽高读取失败"]}
    reasons = []
    if w < DESC_MIN_W or h < DESC_MIN_H:
        reasons.append(f"{w}x{h} 小于 {DESC_MIN_W}x{DESC_MIN_H}")
    ratio = w / h
    if not (DESC_RATIO_MIN <= ratio <= DESC_RATIO_MAX):
        reasons.append(f"宽高比 {ratio:.3f} 超出 {DESC_RATIO_MIN}~{DESC_RATIO_MAX}")
    if size_bytes and size_bytes > DESC_MAX_BYTES:
        reasons.append(f"体积 {size_bytes / 1024 / 1024:.1f}M 超过 "
                       f"{DESC_MAX_BYTES // 1024 // 1024}M")
    return {"ok": not reasons, "reasons": reasons}


# ---- AI 编辑（Packy gpt-image-2）------------------------------------------
API_BASE = "https://cf.api.fan/v1"
MODEL = "gpt-image-2"

# 【必须显式点出中文标点】只说「中文文字」时模型会把汉字译干净、却把中日韩标点原样留下
# （2026-08-26 实测：一张图译成了 `『Dino Back Strap Overalls and Top Set』`，
# 书名号还在，被 vision.check_cleaned 判「残留中文书名号」退回原图，那张图于是又撞回
# 1340×1785 尺寸闸门）。标点也算中文字符，提示词里要单独列出来。
_NO_CJK_PUNCT = (
    "中文标点（『』「」、，。！？；：（）《》～等）也必须一并去掉或换成对应英文标点，"
    "不能留在图上"
)
DEFAULT_CLEAN_PROMPT = (
    "移除图片中所有中文文字、水印和 logo，" + _NO_CJK_PUNCT + "，"
    "保持商品主体、配色和构图完全不变，被遮挡处按周围内容自然补全。"
)
# 【翻译优先，与 DEFAULT_CLEAN_PROMPT 的「移除中文」刻意相反】描述图英化与主图清理
# 走这条：商品介绍、说明类中文文字要翻译成英文原位保留，不能当待清理文字抹掉——
# 材质/质量特写图上的说明正是商品要传达的信息，抹掉就丢信息（2026-09-03 用户要求）。
# 【范围用「凡是……都翻译保留」的开放措辞，别列举几个类别】用户 2026-09-03 明确：
# 商品介绍类中文只要不违规都该翻译，列举「材质/工艺/尺寸/卖点」会漏掉使用说明、
# 注意事项等，模型就把没列到的当装饰删了。移除只针对水印/店铺名/他人 logo。
DEFAULT_TRANSLATE_PROMPT = (
    "把图片中叠加的中文文字翻译成简洁的英文并原位替换，字体风格、字号和排版尽量保持一致；"
    "凡是商品介绍、说明类文字（材质成分、工艺、尺寸、功能卖点、使用说明、注意事项等）"
    "都翻译保留、不要删除；"
    "仅水印、店铺名、拍摄者账号文字、他人品牌 logo 这类非商品信息直接移除，"
    + _NO_CJK_PUNCT + "，商品主体、配色、图案和构图完全不变，被移除处按周围内容自然补全。"
)

# 尺码表图/尺寸图专用的英化提示词：在 DEFAULT_TRANSLATE_PROMPT 的基础上，额外要求把
# 尺寸数值的长度单位 cm 换算成英寸 in。2026-09-04 用户要求：当实测尺寸没识别到、描述区
# 保留源尺码表图时，买家按它选码；非服装类（手提袋/包包/玩具/杂货）的尺寸示意图则是
# 买家了解整体大小的唯一凭据——两类都需 cm→英寸（数值保留 1 位小数）。
# 换算数字这一步依赖生图模型，可能换算错，属已知风险（用户已接受，见 service._st_desc）。
SIZECHART_TRANSLATE_PROMPT = (
    DEFAULT_TRANSLATE_PROMPT
    + "尺寸表/尺寸图里的长度数值请把单位 cm 换算成英寸 in（1cm≈0.39in），数值保留 1 位小数；"
    "只换算长度单位，不要把胸围/腰围这类全围数值误当半围。"
)

# API 允许的尺寸集合 → (宽, 高)。文档要求：16 的倍数、最长边 ≤3840、宽高比 ≤3:1、
# 总像素 655360~8294400。"auto" 不参与自动选择（用户要求显式指定，不用 auto）。
#
# 【这份清单不是服务端白名单，只是精选档位】2026-08-26 实测传 2064x1792、2608x1792
# 都正常出图且服务端【严格按请求尺寸返回】，非清单尺寸完全可用。故下面 _gate_size
# 敢按闸门算出定制档，不必受这几个档位限制。
ALLOWED_SIZES = {
    "1024x1024": (1024, 1024), "1536x1024": (1536, 1024), "1024x1536": (1024, 1536),
    "1536x864": (1536, 864), "2048x2048": (2048, 2048), "2048x1152": (2048, 1152),
    "3840x2160": (3840, 2160), "2160x3840": (2160, 3840),
}

# 服务端尺寸约束（文档四条，_gate_size 生成定制档时必须逐条满足）
SIZE_MULTIPLE = 16          # 宽高都要是 16 的倍数
SIZE_MAX_RATIO = 3.0        # 宽高比 ≤3:1
SIZE_MIN_PIXELS = 655360
SIZE_MAX_PIXELS = 8294400


def resolve_packy_key() -> str:
    """取 Packy 图像编辑 key：只读 config.toml 的 [publish].packy_api_key。

    原脚本在源码里内置了一个兜底 key，这里刻意不留：密钥进版本库是安全问题，
    且那个 key 迟早失效、届时报错信息会指向 API 而不是「你没配密钥」，更难排查。

    【2026-08-20 起不再读环境变量 PACKY_API_KEY】两处维护同一个 key 时，环境变量
    优先级更高会静默盖掉配置值——看配置文件是新 key、实际生效的是旧 key，排查时
    完全看不出来。唯一来源就是配置文件。

    注意这个 key 与 [llm.publish].api_key 是【两个不同分组】的 key，不能混用
    （2026-08-20 实测）：图像编辑走 sora 分组（只有 gpt-image-2），文本与视觉理解
    走 grok 分组（只有 grok-4.5/4.6）。拿 grok 的 key 调 gpt-image-2 会得到 503
    「分组 grok-sale 下模型 gpt-image-2 无可用渠道」，反之同理。
    """
    try:
        import tomllib

        from app.config import config_search_dirs
        for d in config_search_dirs():
            p = d / "config.toml"
            if not p.exists():
                continue
            with open(p, "rb") as f:
                data = tomllib.load(f)
            key = (data.get("publish") or {}).get("packy_api_key") or ""
            if key:
                return key
    except Exception as e:
        logger.warning(f"读取 [publish].packy_api_key 失败：{e}")
    raise RuntimeError(
        "缺少 Packy 图像编辑 key：在 config/config.toml 的 [publish] 段配 "
        "packy_api_key（须是能访问 gpt-image-2 的分组，与 [llm.publish].api_key 不同）"
    )


# ---- 纯本地几何处理（无网络、无密钥、可离线单测）---------------------------

def image_size(path: str) -> Optional[tuple]:
    """用 PIL 读图片尺寸 (w, h)；读不到返回 None。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


# ---- 近重复检测（感知哈希）--------------------------------------------------
# 【为什么 md5 不够】1688 轮播图里常有同一张摄影的多个裁切/压缩版本：2026-08-24 实测
# product-957056453209 的 main-04（750×1000）就是 main-06（1920×1920）的 3:4 中心裁切，
# 画面完全一样但 md5 不同，md5 去重判不出，两张都被挂进同一个 SKC 颜色行，
# 成品页上肉眼可见两张重复图。
#
# 【为什么只用 ahash，不用 dhash】同一实测样本上 ahash 距离 4、其余 14 个配对最低 10，
# 间隔充裕；dhash 反而是 19——裁切改变了边缘梯度分布，「dhash 对裁切不敏感」这个
# 常见说法在这里恰好是缺点。故判近重复只看 ahash。
AHASH_SIZE = 8
# ahash 汉明距离阈值：≤ 此值判近重复。实测样本近重复为 4、非重复最低 10，取 6 居中。
# 【别往上调】同款不同颜色的平铺图构图高度一致，阈值放大会把不同颜色的图判成重复。
# 误杀比漏判严重得多：漏判只是页面上多一张重复图，误杀会让某个颜色一张图都不剩。
AHASH_MAX_DISTANCE = 6


def ahash(path: str, size: int = AHASH_SIZE) -> Optional[int]:
    """均值哈希：灰度缩到 size×size，逐像素与全图均值比较得位串。

    读不出图返回 None，调用方按「无法判断」处理——绝不能当成重复，
    那会让一张本该保留的图被静默丢掉。
    """
    from PIL import Image
    try:
        with Image.open(path) as im:
            px = list(im.convert("L").resize((size, size), Image.LANCZOS).getdata())
    except Exception as e:
        logger.warning(f"计算 ahash 失败（按无法判断处理）：{path} {e}")
        return None
    avg = sum(px) / len(px)
    return sum(1 << i for i, v in enumerate(px) if v > avg)


def ahash_distance(a: int, b: int) -> int:
    """两个 ahash 的汉明距离（不同位的个数）。"""
    return bin(a ^ b).count("1")


def color_signature(path: str, size: int = 16) -> Optional[list]:
    """图的颜色指纹：缩到 size×size 的 RGB 像素序列。读不出图返回 None。

    【为什么不用 ahash 做颜色配对】ahash 先转灰度，而 SKC 分色要区分的恰恰是颜色：
    同款服装的红版与蓝版灰度化后构图一致，ahash 距离常常是 0（2026-09-03 实测
    product-969144784315：16 张主图里 main-08/09 的 ahash 完全相同、无法区分）。
    保留 RGB 后同一实测集 16/16 命中，自身距离 1.7~3.1、次近 9.1~41，区分度干净。
    ahash 仍留给判近重复（那里要的正是「不管颜色只看构图」）。
    """
    from PIL import Image
    try:
        with Image.open(path) as im:
            return list(im.convert("RGB").resize((size, size), Image.LANCZOS).getdata())
    except Exception as e:
        logger.warning(f"计算颜色指纹失败（按无法判断处理）：{path} {e}")
        return None


def color_distance(a: list, b: list) -> float:
    """两个颜色指纹的平均通道差（0~255，越小越像）。长度不等时返回 255（判为不像）。"""
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x[0] - y[0]) + abs(x[1] - y[1]) + abs(x[2] - y[2])
               for x, y in zip(a, b)) / (len(a) * 3)


def is_near_duplicate(path_a: str, path_b: str,
                      max_distance: int = AHASH_MAX_DISTANCE) -> bool:
    """两张图是否近重复。任一张算不出哈希时返回 False（不判重复，理由见 ahash）。"""
    ha, hb = ahash(path_a), ahash(path_b)
    if ha is None or hb is None:
        return False
    return ahash_distance(ha, hb) <= max_distance


# ---- 上传前的硬性尺寸闸门 ---------------------------------------------------
# 【为什么必须有这道闸门】1340×1785 是 Temu 服装类的硬红线，而违规的代价极不对称：
# 平台在【保存时】才校验，且失败时页面完全静默——不弹 toast，只是滚到出错区块、
# 右侧锚点变红（见 save() 的成功判据注释）。于是一张小图能让整单卡在 ⑭ save，
# 报出来的只有「区块变红」，根本看不出是哪张图、差多少像素。
#
# 原先这条规则只靠 fit_34 / square_image / compress 的「出图达标」来保证，约束写在
# 各调用点的注释里（「传进来的图必须已做过合规化，本函数不代做」）。约定不是校验：
# CLI 直接传原图、或几何函数自己出了不达标的图，都能一路传到图床。故收敛成一个
# 显式函数，由 upload_image 统一把关——上传是所有图片进平台的唯一入口。
#
# 【只拦不改】这里不代做合规化：要放大还是补白边取决于用途（素材图 1:1、SKC 3:4），
# 闸门无从判断，猜错会把商品图裁坏。故只报「差在哪」，让调用方去调对应的几何函数。

def check_cloth_size(image_path: str, min_w: int = CLOTH_MIN_W,
                     min_h: int = CLOTH_MIN_H) -> dict:
    """校验图片是否达到服装类最小尺寸，返回 {"ok", "size", "reason"}。

    读不到尺寸（文件损坏/不是图片）也判不 ok：与其让它传上去在 save 时静默弹回，
    不如在这里就说清楚。
    """
    size = image_size(image_path)
    if size is None:
        return {"ok": False, "size": None,
                "reason": f"读不出图片尺寸（文件损坏或非图片）：{image_path}"}
    w, h = size
    if w < min_w or h < min_h:
        return {"ok": False, "size": f"{w}x{h}",
                "reason": (f"服装类图片不能小于 {min_w}×{min_h}，当前 {w}×{h}"
                           f"（宽差 {max(0, min_w - w)}px、高差 {max(0, min_h - h)}px）；"
                           "素材图走 square_image、SKC 图走 fit_34 合规化后再传")}
    return {"ok": True, "size": f"{w}x{h}", "reason": ""}


def _fit_min_size(im, min_w: int = CLOTH_MIN_W, min_h: int = CLOTH_MIN_H):
    """服装类最小尺寸兜底：宽 <min_w 或高 <min_h 时等比放大到达标（LANCZOS）。

    放大倍数取 max(min_w/w, min_h/h)，保证两边都 ≥ 最小值；已达标则原样返回
    （只放大不缩小——缩小会重新掉到校验红线以下）。
    """
    from PIL import Image
    w, h = im.size
    sc = max(min_w / w, min_h / h)
    if sc > 1:
        nw, nh = round(w * sc), round(h * sc)
        # 【留余量】round 可能把宽恰好落在 min_w 边界（800×1.675=1340），而平台
        # 「不能小于 1340」的校验会把 =1340 也拦下（2026-09-05 1071736188944 发布
        # 报「服装类图片尺寸不能小于1340px*1785px」就是它）。宽或高仍 == 下限时 +1，
        # 保证严格大于；宽高同加 1px，3:4 比例偏差 1px 在容差内。
        if nw <= min_w or nh <= min_h:
            nw += 1
            nh += 1
        im = im.resize((nw, nh), Image.LANCZOS)
    return im


def fit_34(image_path: str, out_path: Optional[str] = None, quality: int = 85) -> dict:
    """SKC 服装图合规化（纯 PIL，不走 AI，不动画面内容）：

    1) 白边补齐到 3:4（太宽补高、太高补宽，居中，白底，不裁切不拉伸）；
    2) 等比放大到 ≥1340×1785；
    3) JPEG 保存控体积。

    刻意用补白边而非裁切：SKC 颜色图是商品实拍，裁掉边缘可能把衣服切掉一截；
    补白边在 Temu 详情页里视觉上可接受，且能同时满足比例和最小尺寸两条硬规则。
    素材图要求 1:1，不要用本函数（用 square_image）。
    """
    from PIL import Image
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)
    out_path = out_path or os.path.splitext(image_path)[0] + "-34.jpg"
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        r = w / h
        if abs(r - SKC_RATIO) > 0.005:
            if r > SKC_RATIO:      # 太宽 → 补高
                nw, nh = w, round(w / SKC_RATIO)
            else:                  # 太高 → 补宽
                nw, nh = round(h * SKC_RATIO), h
            canvas = Image.new("RGB", (nw, nh), (255, 255, 255))
            canvas.paste(im, ((nw - w) // 2, (nh - h) // 2))
            im = canvas
        im = _fit_min_size(im)
        im.save(out_path, quality=quality)
    with Image.open(out_path) as im:
        ow, oh = im.size
    return {"status": "ok", "input": image_path, "output": out_path,
            "outSize": f"{ow}x{oh}", "ratio": round(ow / oh, 4)}


def square_image(image_path: str, out_path: Optional[str] = None,
                 target: int = MATERIAL_TARGET) -> dict:
    """素材图几何修正（纯 PIL，不走 AI，不动画面内容）：

    中心裁切成 1:1，再缩放到 target×target（默认 1785，同时满足素材图 ≥800×800
    和服装类最小尺寸 ≥1340×1785 两条规则）。
    用于 materialCheck.needsProcessing=true 的素材图处理。
    """
    from PIL import Image
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)
    out_path = out_path or os.path.splitext(image_path)[0] + "-square.jpg"
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        im = im.crop((left, top, left + side, top + side))
        # 【修复】无论裁切后尺寸多大，统一缩放到 target×target
        # 原逻辑 "if side < target" 会导致 800-1784 范围的图不被放大、直接保存小图
        # 也会导致 >1785 的图不被缩小、浪费存储空间
        if side != target:
            im = im.resize((target, target), Image.LANCZOS)
        im.save(out_path, quality=85)
    with Image.open(out_path) as im:
        ow, oh = im.size
    return {"status": "ok", "input": image_path, "output": out_path,
            "outSize": f"{ow}x{oh}"}


def compress(path: str, max_dim: int = MAX_DIM, quality: int = 80,
             min_w: int = CLOTH_MIN_W, min_h: int = CLOTH_MIN_H) -> str:
    """已有图片纯处理（不走 AI）：放大到 ≥1340×1785 + 长边 ≤max_dim + 转 JPEG。

    返回最终路径（非 jpg 输入会改名成 .jpg 并删除旧文件）。
    背景：AI 出图 PNG 每张 2MB+，店小秘图片空间 500M 很快打满，JPEG q80 控体积。
    注意先放大再判缩小的顺序：最小尺寸是硬红线，长边上限只是体积保护，
    反过来做会把图缩到红线以下。

    【长边缩小必须让位于最小尺寸】只按 max_dim 等比缩会把窄边压破 1340×1785。
    极端狭长图会踩到：1340×5000 放大后长边 5000>3840，缩下来是 1029×3840，宽已
    破线；描述长图天生窄而极长，正是高发场景。故缩放系数取
    max(max_dim/长边, min_w/w, min_h/h) 的下限保护——宁可留着超出 max_dim 的长边
    （只是体积大些），也不能掉到红线以下让 save 静默弹回。
    """
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im = _fit_min_size(im, min_w, min_h)
        w, h = im.size
        if max(w, h) > max_dim:
            sc = max_dim / max(w, h)
            # 缩到不破下限为止：任一边会跌破就按那一边的比例封底
            floor = max(min_w / w, min_h / h)
            sc = max(sc, floor)
            if sc < 1:
                im = im.resize((round(w * sc), round(h * sc)), Image.LANCZOS)
        if path.lower().endswith((".jpg", ".jpeg")):
            im.save(path, quality=quality)
            return path
        out = os.path.splitext(path)[0] + ".jpg"
        im.save(out, quality=quality)
    os.unlink(path)
    return out


def _gate_size(w: int, h: int, min_w: int = CLOTH_MIN_W,
               min_h: int = CLOTH_MIN_H) -> Optional[str]:
    """按原图比例算一个【不低于 1340×1785 闸门】的出图尺寸，算不出返回 None。

    【为什么需要这个】原先 pick_size 只按比例在 8 个档位里挑最接近的，挑出来的档
    普遍小于闸门：790×684 挑中 1024x1024、749×513 挑中 1536x1024，出图后一律被
    compress 插值放大 1.31~1.74 倍才够过闸门（2026-08-26 实测，7 张描述图无一例外）。
    等于每张图都先降采样生成、再拉大，两次重采样把细节磨掉——尤其伤这类「面料细节」
    特写图，纹理糊掉正好毁掉它要展示的东西。

    直接让服务端按目标尺寸生成就不必放大。服务端不限于 ALLOWED_SIZES 那几档
    （见其上方注释的实测），只需满足四条约束：16 的倍数、比例 ≤3:1、
    长边 ≤MAX_DIM、像素在 SIZE_MIN_PIXELS~SIZE_MAX_PIXELS。

    保持原图比例而不是硬套 3:4：描述图是长图混排，比例被改就会变形；SKC 图要 3:4
    是另一条规则，由 fit_34 负责，不在这里做。
    """
    if w <= 0 or h <= 0:
        return None
    ratio = w / h
    if ratio > SIZE_MAX_RATIO or 1 / ratio > SIZE_MAX_RATIO:
        return None            # 比例本身越界，交给 pick_size 走原路
    # 等比放到同时不低于两条下限，再向上取整到 16 的倍数（取整只会变大，不会跌破）
    sc = max(min_w / w, min_h / h, 1.0)
    tw = int(-(-round(w * sc) // SIZE_MULTIPLE) * SIZE_MULTIPLE)
    th = int(-(-round(h * sc) // SIZE_MULTIPLE) * SIZE_MULTIPLE)
    # 取整后仍要保证不低于下限（极端窄边可能因取整方向差 1 个像素）
    while tw < min_w:
        tw += SIZE_MULTIPLE
    while th < min_h:
        th += SIZE_MULTIPLE
    if max(tw, th) > MAX_DIM or not (SIZE_MIN_PIXELS <= tw * th <= SIZE_MAX_PIXELS):
        return None            # 放大后越界（超长图/超大图），退回原路由 compress 兜
    if tw / th > SIZE_MAX_RATIO or th / tw > SIZE_MAX_RATIO:
        return None
    return f"{tw}x{th}"


def pick_size(w: int, h: int, no_downscale: bool = False) -> str:
    """按原图宽高比选最接近的 API 允许尺寸（不用 auto）。

    例：800x800 → 1024x1024；750x1000(0.75) → 1024x1536(0.667)。

    no_downscale=True 时在【同比例】候选里再挑一个不小于原图的，避免「先降采样出图、
    再 compress 插值放大」的二次重采样把画质磨掉。2026-08-22 实测：1276×1276 的方图
    按纯比例只会选中 1024x1024（比原图还小），出图后被 compress 拉到 1785，
    背景纹理明显变糊、小字整块消失。素材图是轮播首图，糊了最伤转化，故走这条；
    SKC 图和描述图仍用默认（生成像素是 2048 档的 1/4，性价比优先）。
    """
    ratio = w / h
    best = min(ALLOWED_SIZES,
               key=lambda s: abs(ALLOWED_SIZES[s][0] / ALLOWED_SIZES[s][1] - ratio))
    if not no_downscale:
        return best
    bw, bh = ALLOWED_SIZES[best]
    target = bw / bh
    # 同比例档位（如 1024x1024 与 2048x2048）里选够大的最小者，一个都不够大就取最大档
    same = [s for s in ALLOWED_SIZES
            if abs(ALLOWED_SIZES[s][0] / ALLOWED_SIZES[s][1] - target) < 0.005]
    fit = [s for s in same if ALLOWED_SIZES[s][0] >= w and ALLOWED_SIZES[s][1] >= h]
    pool = fit or same
    return (min if fit else max)(pool, key=lambda s: ALLOWED_SIZES[s][0] * ALLOWED_SIZES[s][1])


def pick_size_for_file(image_path: str, no_downscale: bool = False,
                      gate_aware: bool = True) -> str:
    """读原图尺寸并选出图尺寸；读不到尺寸退回 1024x1024。

    gate_aware=True（默认）优先用 _gate_size 直接生成不低于 1340×1785 闸门的尺寸，
    省掉出图后那次 compress 插值放大（实测 7 张描述图放大 1.31~1.74 倍，纹理明显糊）。
    算不出（比例越界/放大后超上限）才退回 pick_size 的档位挑选。

    gate_aware=False 保留纯档位行为，给不需要过服装闸门的调用方留口子。
    """
    wh = image_size(image_path)
    if not wh:
        return "1024x1024"
    if gate_aware:
        gs = _gate_size(*wh)
        if gs:
            return gs
    return pick_size(*wh, no_downscale=no_downscale)


# ---- AI 编辑（Packy gpt-image-2，必须走 curl.exe）--------------------------

# curl 退出码里属于「链路瞬时故障」的那些：重发一次很可能就过，值得重试。
# 6 DNS 解析失败 / 7 连不上 / 16 HTTP2 帧错 / 18 传输被截断 / 28 超时 /
# 35 TLS 握手失败 / 52 服务端没回内容 / 55 发送失败 / 56 接收失败（连接重置）。
# 【为什么必须按码白名单，不能「非 0 就重试」】curl 的确定性失败也会给非 0：
# 3 URL 格式错、26 读本地文件失败（图片路径不对）、重试多少次都是同样的错。
# 更要紧的是 HTTP 400/500 这类【业务错误 curl 是 rc=0】、响应体照常返回，
# 走的是下面 JSON 解析那条路，压根不经过这里——所以参数错（input_fidelity）
# 不会被误当成抖动重试，这是这个白名单能成立的前提。
_CURL_TRANSIENT_RC = frozenset({6, 7, 16, 18, 28, 35, 52, 55, 56})


class TransientNetError(RuntimeError):
    """链路瞬时故障（curl 退出码在 _CURL_TRANSIENT_RC 里），调用方可重试。

    单开一个异常类型而不是让调用方去 parse 错误字符串：文案会被截断也会变，
    按类型判是唯一稳的判据（同 service._resolve_desc_pos 用 navigatedAway 标志
    而不是错误文案的理由）。
    """


def _curl_json(args: list, timeout: int = 280) -> dict:
    """用 curl.exe 发请求并把 stdout 解析为 JSON。出错时抛带响应体的异常。

    链路瞬时故障抛 TransientNetError（可重试），其余抛 RuntimeError（别重试）。
    """
    cmd = ["curl.exe", "-s", "--max-time", str(timeout), *args]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 20)
    except subprocess.TimeoutExpired as e:
        # 子进程整体超时（curl 自己的 --max-time 没生效住）也是瞬时故障
        raise TransientNetError(f"curl 子进程超时（{timeout + 20}s）") from e
    text = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace")[:200]
        msg = f"curl 失败 rc={r.returncode}: {err}"
        raise (TransientNetError if r.returncode in _CURL_TRANSIENT_RC
               else RuntimeError)(msg)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 空响应/被中途掐断也算抖动：正常的业务错误一定是完整 JSON
        if not text.strip():
            raise TransientNetError("API 返回空响应（连接可能被中断）")
        raise RuntimeError(f"API 返回非 JSON（可能被拦截）: {text[:300]}")


def _multipart_post(url: str, fields: dict, files: dict, timeout: int = 280) -> dict:
    """multipart/form-data POST，走 curl。files: {字段名: 文件路径}。

    fields 里的长文本/中文（如 prompt）写临时文件用 `-F name=<file` 读入：
    直接拼进命令行会被 Windows 的编码转换弄坏中文（原脚本实测踩过）。
    """
    args = [url, "-H", f"Authorization: Bearer {resolve_packy_key()}", "-H", "Accept: */*"]
    tmps: list = []
    try:
        for name, value in fields.items():
            value = str(value)
            if len(value) > 50 or any(ord(c) > 127 for c in value):
                tmp = tempfile.NamedTemporaryFile(
                    "w", suffix=".txt", delete=False, encoding="utf-8"
                )
                tmp.write(value)
                tmp.close()
                tmps.append(tmp.name)
                args += ["-F", f"{name}=<{tmp.name}"]
            else:
                args += ["-F", f"{name}={value}"]
        for name, path in files.items():
            args += ["-F", f"{name}=@{path}"]
        return _curl_json(args, timeout)
    finally:
        # 原脚本只记住最后一个临时文件、多字段时会漏删，这里逐个清理
        for p in tmps:
            if os.path.exists(p):
                os.unlink(p)


def _json_post(url: str, payload: dict, timeout: int = 280) -> dict:
    """JSON POST，走 curl（body 写临时文件，防中文/长文本在命令行里损坏）。"""
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(payload, tmp, ensure_ascii=False)
        tmp.close()
        return _curl_json(
            [url, "-H", f"Authorization: Bearer {resolve_packy_key()}",
             "-H", "Content-Type: application/json", "-H", "Accept: */*",
             "--data-binary", f"@{tmp.name}"], timeout
        )
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


def _save_result(resp_json: dict, out_path: str) -> str:
    """从响应保存图片（url 或 b64_json），返回保存路径。下载也走 curl（同一拦截策略）。

    实测（2026-08-18）：结果 CDN 偶发连接失败，curl -s 会吞错误信息，故用 -sS 且失败
    重试；超长签名 URL 用 -K 配置文件传，避免 WinError 206（命令行过长）。
    """
    import base64

    data = (resp_json.get("data") or [{}])[0]
    if data.get("url"):
        url = data["url"]
        last_err = ""
        r = None
        for attempt in range(3):
            if len(url) > 1500:
                cfg = tempfile.NamedTemporaryFile(
                    "w", suffix=".curlrc", delete=False, encoding="utf-8"
                )
                try:
                    cfg.write(f'url = "{url}"\n')
                    cfg.close()
                    r = subprocess.run(
                        ["curl.exe", "-sS", "-L", "--max-time", "280",
                         "-K", cfg.name, "-o", out_path],
                        capture_output=True, timeout=300)
                finally:
                    if os.path.exists(cfg.name):
                        os.unlink(cfg.name)
            else:
                r = subprocess.run(
                    ["curl.exe", "-sS", "-L", "--max-time", "280", "-o", out_path, url],
                    capture_output=True, timeout=300)
            if r.returncode == 0 and os.path.exists(out_path) \
                    and os.path.getsize(out_path) > 1000:
                return out_path
            last_err = r.stderr.decode("utf-8", "replace")[:200]
            logger.warning(f"结果图下载失败（{attempt + 1}/3）：{last_err}")
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"下载结果图失败：{last_err}")
    if data.get("b64_json"):
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(data["b64_json"]))
        return out_path
    raise RuntimeError(f"响应中没有图片: {json.dumps(resp_json, ensure_ascii=False)[:300]}")


# 网关坏渠道的错误签名（2026-08-26 实测）：cf.api.fan 后面挂了多个 gpt-image-2 上游，
# 其中一部分不接 multipart 上传，命中时返 HTTP 500 + code=convert_request_failed，
# 文案是「does not accept multipart file upload; please provide a public URL via
# 'image_url' form field instead」。
#
# 【这条建议不能照做，照做必然更糟】按它说的改传 image_url 会被网关【前置校验】直接
# 打回：只传 image_url 报 missing_required_parameter「Missing required parameter:
# 'image'」，image 与 image_url 同传报 unknown_parameter「Unknown parameter:
# 'image_url'」，image 传 URL 字符串报 invalid_type「expected one of an array of
# files or file, but got a string instead」。也就是说网关只认 multipart 文件，
# 这句建议是坏渠道自己吐的、与网关契约矛盾。同理走 JSON body 传 data URL 也不行
# （报「does not accept base64 image upload」）。唯一能出图的形状就是现在这个。
#
# 【所以只能重试换渠道】分流是随机的、不粘连：同一张图同一个 key，重试立刻就能落到
# 好渠道（实测 3 并发 3/3 一发即中；6 并发 6/6；12 并发 10/12；命中坏渠道时约 35s
# 就返回，比出图的 45~55s 还快，重试代价可接受）。故这里只对这一个错误签名重试，
# 别扩大成「所有错误都重试」——参数错（如 input_fidelity）重试多少次都是同样的错，
# 白等几分钟还把真错误埋掉（见 [[publish-vision-400-no-retry]] 同类教训）。
_BAD_CHANNEL_CODE = "convert_request_failed"
_BAD_CHANNEL_MARK = "does not accept"
# 重试次数：单发命中坏渠道约 35s。按实测最差档（12 并发 83% 成功）算，4 发全落坏渠道
# 的概率极低；给 4 次上限，最坏也就多等约 105s，不至于把整个阶段拖垮。
#
# 【坏渠道与网络抖动共用这一份预算】两者都靠「重发一次」解决，各记一套次数会叠乘成
# 最坏 16 发、十几分钟，把阶段拖死。共用后无论怎么交替失败，总发数都不超过 4。
EDIT_BAD_CHANNEL_RETRY = 4
# 抖动重试的退避基数（秒）：第 n 次失败后睡 n × 这个值。
# 【为什么抖动要退避、坏渠道不要】坏渠道是随机分流、与时间无关，等待纯属浪费；
# 而抖动往往是链路一小段时间的劣化（VPN 抖、CDN 拥塞，见
# [[packy-image-response-format-and-vpn]] 那次 260 倍速度差），立刻重发很可能
# 撞上同一段劣化，隔几秒成功率明显更高。退避短是因为并发 30 时几十秒的睡眠会
# 累积成可观的墙钟。
EDIT_TRANSIENT_BACKOFF = 3


def _is_bad_channel(resp_json: dict) -> bool:
    """判响应是否为「坏渠道拒收 multipart」——只有这种错值得重试换渠道。"""
    err = resp_json.get("error")
    if not isinstance(err, dict):
        return False
    return (err.get("code") == _BAD_CHANNEL_CODE
            and _BAD_CHANNEL_MARK in str(err.get("message") or ""))


def _edits_post_with_retry(fields: dict, image_path: str, timeout: int = 280,
                           tries: int = EDIT_BAD_CHANNEL_RETRY) -> dict:
    """发 /images/edits，对【坏渠道】与【链路抖动】两类瞬时失败重试。

    两类失败的判据与取舍见上方 _BAD_CHANNEL_CODE / _CURL_TRANSIENT_RC 注释；
    共用同一份 tries 预算（理由见 EDIT_BAD_CHANNEL_RETRY），退避只给抖动
    （理由见 EDIT_TRANSIENT_BACKOFF）。

    【只有这两类重试，别扩大】确定性失败（参数错、图片路径错、被 Cloudflare 拦）
    一律原样抛出：重试多少次都是同样的错，白等还把真错误埋成一串重试噪音
    （见 [[publish-vision-400-no-retry]]）。这也是抖动必须按 curl 退出码白名单
    判、而不是「有异常就重试」的原因。

    抖动重试到最后一次仍失败时把异常抛出去（而不是返回坏响应），交由上层
    best-effort 兜住——⑤b 清理与⑬ 备料都会把单张失败降级成「保留原图」。
    """
    last_resp = None
    for attempt in range(1, tries + 1):
        try:
            resp = _multipart_post(f"{API_BASE}/images/edits", fields,
                                   {"image": image_path}, timeout=timeout)
        except TransientNetError as e:
            if attempt >= tries:
                raise
            logger.warning(
                f"gpt-image-2 链路抖动（{attempt}/{tries}），{EDIT_TRANSIENT_BACKOFF * attempt}s 后重试："
                f"{os.path.basename(image_path)} {e}"
            )
            time.sleep(EDIT_TRANSIENT_BACKOFF * attempt)
            continue
        if not _is_bad_channel(resp):
            return resp
        last_resp = resp
        logger.warning(
            f"gpt-image-2 命中坏渠道（{attempt}/{tries}），换渠道重试："
            f"{os.path.basename(image_path)}"
        )
    return last_resp


def edit_image(image_path: str, prompt: Optional[str] = None,
               out_path: Optional[str] = None, size: Optional[str] = None,
               quality: str = "low", target: Optional[str] = None,
               do_compress: bool = True, no_downscale: bool = False,
               timeout: int = 280, desc_mode: bool = False) -> dict:
    """AI 编辑单张图（去中文/去水印/英化）。

    prompt 缺省用 DEFAULT_CLEAN_PROMPT，但【建议调用方按图定制】：先看图定位具体问题
    （什么文字、在哪个位置），针对性给提示词，效果远好于笼统的「移除所有文字」。
    size 缺省按原图宽高比自动选允许尺寸；quality 默认 low（够用且快）。
    出图后默认 compress（>=1340x1785、长边 <=3840、JPEG q80）控体积。

    no_downscale=True 让自动选档不小于原图（见 pick_size 注释），素材图用。
    timeout 可压短：批量清理时一张卡住不该拖住整批（阶段⑥前置清理给 90s）。

    desc_mode=True 走【描述长图】的尺寸口径（两边 >= 480、比例 0.5~2，见
    check_desc_size）：不按服装闸门挑出图尺寸、收尾 compress 也不放大到 1340x1785。
    2026-08-28 用户明确：描述图不需要 1700+，套服装红线只会白插值放大、糊掉画面，
    还让本来合格的 1000x1000 被判不达标而重烧生图。
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)
    out_path = out_path or os.path.splitext(image_path)[0] + "-edited.png"
    fields = {
        "model": MODEL,
        "prompt": prompt or DEFAULT_CLEAN_PROMPT,
        # 描述图不过服装闸门：按原图比例挑档即可，不必为「够 1340x1785」而放大出图
        "size": size or pick_size_for_file(image_path, no_downscale=no_downscale,
                                           gate_aware=not desc_mode),
        "quality": quality,
        "n": 1,
    }
    resp = _edits_post_with_retry(fields, image_path, timeout=timeout)
    saved = _save_result(resp, out_path)
    if target:
        tw, th = (int(x) for x in target.lower().split("x"))
        from PIL import Image
        with Image.open(saved) as im:
            im.convert("RGB").resize((tw, th), Image.LANCZOS).save(saved)
    if do_compress:
        saved = (compress(saved, min_w=DESC_MIN_W, min_h=DESC_MIN_H)
                 if desc_mode else compress(saved))
    return {"status": "ok", "input": image_path, "output": saved,
            "size": fields["size"], "outSize": "x".join(map(str, image_size(saved) or ()))}


def generate_image(prompt: str, out_path: str, size: str = "1024x1024",
                   quality: str = "low", do_compress: bool = True) -> dict:
    """文生图（描述区营销图增补用）。"""
    resp = _json_post(f"{API_BASE}/images/generations", {
        "model": MODEL, "prompt": prompt, "size": size, "quality": quality, "n": 1,
    })
    saved = _save_result(resp, out_path)
    if do_compress:
        saved = compress(saved)
    return {"status": "ok", "output": saved,
            "outSize": "x".join(map(str, image_size(saved) or ()))}


def batch_fit34(indir: str, outdir: Optional[str] = None,
                pattern: str = "main-") -> dict:
    """批量把目录里的图处理成 SKC 合规（3:4 + ≥1340×1785），纯本地无 AI。

    发布阶段⑦上传 SKC 颜色图前的必经步骤。单张失败只记录不中断——一张图坏了不该
    让整行 SKC 全传不上（沿用项目 best-effort 取向）。
    """
    if not os.path.isdir(indir):
        raise NotADirectoryError(indir)
    outdir = outdir or os.path.join(indir, "skc-34")
    os.makedirs(outdir, exist_ok=True)
    results = []
    for f in sorted(os.listdir(indir)):
        if not f.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        if pattern and not f.startswith(pattern):
            continue
        src = os.path.join(indir, f)
        dst = os.path.join(outdir, os.path.splitext(f)[0] + ".jpg")
        try:
            results.append(fit_34(src, dst))
        except Exception as e:
            logger.warning(f"{f} fit34 失败：{e}")
            results.append({"status": "error", "input": src, "err": str(e)})
    ok = sum(1 for r in results if r.get("status") == "ok")
    return {"status": "ok" if ok == len(results) else "partial",
            "outdir": outdir, "total": len(results), "done": ok, "results": results}
