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
"""
import json
import os
import subprocess
import tempfile
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

# ---- AI 编辑（Packy gpt-image-2）------------------------------------------
API_BASE = "https://cf.api.fan/v1"
MODEL = "gpt-image-2"

DEFAULT_CLEAN_PROMPT = (
    "移除图片中所有中文文字、水印和 logo，保持商品主体、配色和构图完全不变，"
    "被遮挡处按周围内容自然补全。"
)
DEFAULT_TRANSLATE_PROMPT = (
    "把图片中的中文文字翻译成简洁的英文并原位替换，字体风格、字号和排版尽量保持一致，"
    "商品主体、配色和构图完全不变。"
)

# API 允许的尺寸集合 → (宽, 高)。文档要求：16 的倍数、最长边 ≤3840、宽高比 ≤3:1、
# 总像素 655360~8294400。"auto" 不参与自动选择（用户要求显式指定，不用 auto）。
ALLOWED_SIZES = {
    "1024x1024": (1024, 1024), "1536x1024": (1536, 1024), "1024x1536": (1024, 1536),
    "1536x864": (1536, 864), "2048x2048": (2048, 2048), "2048x1152": (2048, 1152),
    "3840x2160": (3840, 2160), "2160x3840": (2160, 3840),
}


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
        im = im.resize((round(w * sc), round(h * sc)), Image.LANCZOS)
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
        if side < target:
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


def pick_size_for_file(image_path: str, no_downscale: bool = False) -> str:
    """读原图尺寸并 pick_size；读不到尺寸退回 1024x1024。"""
    wh = image_size(image_path)
    return pick_size(*wh, no_downscale=no_downscale) if wh else "1024x1024"


# ---- AI 编辑（Packy gpt-image-2，必须走 curl.exe）--------------------------

def _curl_json(args: list, timeout: int = 280) -> dict:
    """用 curl.exe 发请求并把 stdout 解析为 JSON。出错时抛带响应体的异常。"""
    cmd = ["curl.exe", "-s", "--max-time", str(timeout), *args]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 20)
    text = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        raise RuntimeError(
            f"curl 失败 rc={r.returncode}: {r.stderr.decode('utf-8', 'replace')[:200]}"
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
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
    import time

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


def edit_image(image_path: str, prompt: Optional[str] = None,
               out_path: Optional[str] = None, size: Optional[str] = None,
               quality: str = "low", target: Optional[str] = None,
               do_compress: bool = True, no_downscale: bool = False,
               timeout: int = 280) -> dict:
    """AI 编辑单张图（去中文/去水印/英化）。

    prompt 缺省用 DEFAULT_CLEAN_PROMPT，但【建议调用方按图定制】：先看图定位具体问题
    （什么文字、在哪个位置），针对性给提示词，效果远好于笼统的「移除所有文字」。
    size 缺省按原图宽高比自动选允许尺寸；quality 默认 low（够用且快）。
    出图后默认 compress（≥1340×1785、长边 ≤3840、JPEG q80）控体积。

    no_downscale=True 让自动选档不小于原图（见 pick_size 注释），素材图用。
    timeout 可压短：批量清理时一张卡住不该拖住整批（阶段⑥前置清理给 90s）。
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)
    out_path = out_path or os.path.splitext(image_path)[0] + "-edited.png"
    fields = {
        "model": MODEL,
        "prompt": prompt or DEFAULT_CLEAN_PROMPT,
        "size": size or pick_size_for_file(image_path, no_downscale=no_downscale),
        "quality": quality,
        "input_fidelity": "high",   # 保主体不走形
        "n": 1,
    }
    resp = _multipart_post(f"{API_BASE}/images/edits", fields, {"image": image_path},
                           timeout=timeout)
    saved = _save_result(resp, out_path)
    if target:
        tw, th = (int(x) for x in target.lower().split("x"))
        from PIL import Image
        with Image.open(saved) as im:
            im.convert("RGB").resize((tw, th), Image.LANCZOS).save(saved)
    if do_compress:
        saved = compress(saved)
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
