"""店小秘发布：图上文字的定点抹除（视觉粗定位 + 本地像素精修）。

【为什么存在】生图模型做不好「选择性抹除」。2026-10-08 取证（1045936299360 轮播
第 10 张，名称/面料/填充/尺寸四行规格表）：让它抹掉材质行，原提示词三连败——中段
补例举纹丝不动（B 组）、前置点名就换标签词规避（C 组，面料→"Surface: 4-Sided
Spring"，语义还在只是 QC 认不出）。但它的整表英化质量很高，而质检能逐条列出材质
原文（check_cleaned 的 materialTexts）。于是分工换过来：生图照常，材质行由这条
链路定点填掉——视觉模型按原文给粗框、本地按文字像素扩成完整行带再填背景色。
模型框差几十像素没关系（D 组实测：框右缘漏一个字母、框高不够留半截残影，复检
照样抓得到——闸有效），精修后没有残字可抓（E 组双检 clean 通过）。

【与提示词路线的分界】乱码/残留中文/水印/夸大宣称是生图质量问题，仍走
_english_one 的带反馈重生；本模块只治「QC 唯一不满是材质文字」这一类——
那也是平台定案（2026-09-25）要求必须消失的一类，且商品数据里材质是权威值
（阶段④ 重建的 attributes.composition 就在 product-info.json），图上抹掉不丢信息。

【2026-10-08 起第二条能力：品牌标识（图形标记）】（`locate_marks` / `erase_marks`）
同一类确定性失败的第二个实例——包装盒上印的第三方商标「生图三发都抹不掉」（同日
1048494210610 的 ⑦b 第 3 行、第 6 行两次实证，而同一个盒子别的行偶尔能抹掉，纯随机）。
抹掉实物上的他人商标是平台明令要求（侵权风险），故这条同样只做「抹干净」不做「保留」。
与文字行的两点差别见 erase_marks 上方的注释（问法要问整个标记图形、且不做行带精修）。
"""

from app.logger import logger
from app.publish.llm import ask_json_with_images

# 深灰/黑文字判据：灰度低且三通道接近——咬得住文字，不吃彩色主体
# （黄色鸭子 (255,215,0) 灰度高、红缎带 max-min 大，都被排除；2026-10-08 实测调定）
def _is_ink(p: tuple) -> bool:
    r, g, b = p[:3]
    return (r + g + b) / 3 < 110 and max(r, g, b) - min(r, g, b) < 60


def _norm_box(raw, w: int, h: int) -> tuple:
    """把模型给的框归一到像素。兼容三种口径：0~1、0~1000、直接像素。"""
    vals = [float(v) for v in raw]
    mx = max(vals)
    if mx <= 1.5:
        x1, y1, x2, y2 = (vals[0] * w, vals[1] * h, vals[2] * w, vals[3] * h)
    elif mx <= 1000:
        x1, y1, x2, y2 = (vals[0] / 1000 * w, vals[1] / 1000 * h,
                          vals[2] / 1000 * w, vals[3] / 1000 * h)
    else:
        x1, y1, x2, y2 = vals
    return max(0, int(x1)), max(0, int(y1)), min(w, int(x2)), min(h, int(y2))


# 行内横向扩行的空白列阈值：要大于字母间距与单词间距（66px 字号的活泼圆体字母
# 间距实测可达 13px、词距约 20px），否则扩行在行内断档处提前收兵、留下行尾字母
# 被 QC 判乱码（2026-10-08 F 组实测：阈值 6 时 "Cotton" 结尾的 n 漏网）。
# 也不能太大：同一行带上 40px 内出现另一段该保留的文字才会误伤，规格卡的
# 栏间距远大于此；误抹的代价（多抹一句同 line 文字）也由复检兜底。
_LINE_GAP_PX = 40


def _refine_box(px, w: int, h: int, box: tuple) -> tuple:
    """以种子框为起点，按文字像素向四周扩到整行：先垂直扩行带，再水平扩行首尾。

    模型框普遍差几十像素（漏行尾字母、盖不满行高），精修就是把「大致在哪」
    变成「整行盖全」：行与行之间有空白行、文字与主体之间有空白列，扫到连续
    几行/几列无文字像素就是边界。
    """
    x1, y1, x2, y2 = box

    def row_ink(y, xa, xb):
        return sum(1 for x in range(max(0, xa), min(w, xb)) if _is_ink(px[x, y]))

    def col_ink(x, ya, yb):
        return sum(1 for y in range(max(0, ya), min(h, yb)) if _is_ink(px[x, y]))

    pad_x = 30
    run = 0
    while y1 > 0 and run < 4:
        y1 -= 1
        run = run + 1 if row_ink(y1, x1 - pad_x, x2 + pad_x) == 0 else 0
    y1 += run  # 退回无字区
    run = 0
    while y2 < h and run < 4:
        y2 += 1
        run = run + 1 if row_ink(y2 - 1, x1 - pad_x, x2 + pad_x) == 0 else 0
    y2 -= run

    run = 0
    while x1 > 0 and run < _LINE_GAP_PX:
        x1 -= 1
        run = run + 1 if col_ink(x1, y1, y2) == 0 else 0
    x1 += run
    run = 0
    while x2 < w and run < _LINE_GAP_PX:
        x2 += 1
        run = run + 1 if col_ink(x2 - 1, y1, y2) == 0 else 0
    x2 -= run
    return x1, y1, x2, y2


def _tokens(text) -> set:
    """行文的 token 集（小写字母数字串）：内容匹配用，对标点/大小写/连字符不敏感。"""
    import re
    return {t for t in re.split(r"[^a-z0-9]+", str(text).lower()) if t}


def _match_boxes(lines: list, targets: list, w: int, h: int) -> list:
    """从模型列出的全部文字行里，按【原文内容】挑出目标是材质行的框。

    【为什么不能信「模型说这就是那行」】定位模型会把框安到别的行头上
    （2026-10-08 F 组实测：让它框 "Filling: Down Cotton"，它把 Name 行框了回来——
    框错行若照抹，Name 这种该保留的行就被静默挖掉，而 QC 不管名称缺失，拦不住）。
    故让它连原文一起报，本地按 token 重叠过滤：转录与目标对不上的框一律拒。
    拒绝的代价只是「该抹的没抹」——复检拦下、回退重生，是安全方向。
    """
    picked = []
    for item in lines:
        raw = item.get("box")
        if not raw or len(raw) != 4:
            continue
        line_tokens = _tokens(item.get("text"))
        if not line_tokens:
            continue
        hit = False
        for target in targets:
            want = _tokens(target)
            if want and len(want & line_tokens) / len(want) >= 0.6:
                hit = True
                break
        if not hit:
            continue
        x1, y1, x2, y2 = _norm_box(raw, w, h)
        if x2 - x1 > 5 and y2 - y1 > 3:
            picked.append((x1, y1, x2, y2))
    # 同一行被多个目标命中（「面料：X 填充：Y」排在同一视觉行）只留一个框
    deduped = []
    for box in picked:
        if not any(abs(box[1] - old[1]) < 20 and abs(box[0] - old[0]) < 20
                   for old in deduped):
            deduped.append(box)
    return deduped


async def locate_text_lines(image_path: str, texts: list) -> list:
    """视觉模型列出图上全部文字行（原文+粗框），本地按内容筛出目标行的框。
    任何失败都返回 None（调用方回退重生路径）。

    【best-effort 的取向】定位是救援链路的一环，坏了绝不能把主流程也拖死——
    没有它时流程退化为改动前的「带反馈重生」，不新增失败面。ask_json_with_images
    失败会抛（它的既定契约），故整段包 try。
    """
    wanted = [t for t in dict.fromkeys(texts) if t and str(t).strip()]
    if not wanted:
        return None
    prompt = (
        "把这张图上所有叠加的文字行逐行列出：每行给出【实际可见的原文】"
        "（照抄，含标签和值）和紧贴文字的外接矩形坐标，"
        "坐标用 0 到 1 的归一化小数（相对图片宽高，x1,y1 为左上、x2,y2 为右下）。\n"
        '返回 JSON：{"lines": [{"text": "原文", "box": [x1, y1, x2, y2]}]}'
    )
    try:
        from PIL import Image
        with Image.open(image_path) as im:
            w, h = im.size
        data = await ask_json_with_images(prompt, [image_path], what="图上文字行定位")
    except Exception as e:
        logger.warning(f"文字行定位失败（{e}），放弃定点抹除")
        return None
    boxes = _match_boxes(data.get("lines") or [], wanted, w, h)
    if not boxes:
        logger.warning(f"文字行定位没有内容匹配的框（目标 {len(wanted)} 行、"
                       f"列出 {len(data.get('lines') or [])} 行），放弃定点抹除")
        return None
    return boxes


def _row_fill_colors(px, w: int, h: int, box: tuple, fallback) -> dict:
    """逐行取填充色：返回 {y: color}。

    【为什么要逐行】包装盒面这种大平面常有光照渐变：整块用一个中位数色填，会留下一圈
    看得见的色阶边（2026-10-08 在 lang05 那张实物上肉眼可见）。逐行取框两侧紧邻的非墨
    像素中位数，跟着渐变走；某行取不到样本、或样本互差过大（蹭到别的元素）就退回整体
    中位数。采样点都在框外，填充不会污染后续行的取样。
    """
    X1, Y1, X2, Y2 = box
    rows = {}
    for y in range(Y1, Y2):
        samples = [px[x, y] for x in (X1 - 4, X1 - 2, X2 + 1, X2 + 3)
                   if 0 <= x < w and not _is_ink(px[x, y])]
        if len(samples) < 2:
            continue
        samples.sort()
        mid = samples[len(samples) // 2]
        if all(max(abs(c[i] - mid[i]) for i in range(3)) <= 40 for c in samples):
            rows[y] = mid
    return rows


def erase_boxes(image_path: str, boxes: list, out_path: str = None,
                refine: bool = True, per_row: bool = False, pad: int = 10) -> dict:
    """精修并填除若干文字行。背景不是近纯色的框拒绝抹（留色疤不如不抹）。

    refine=False 时不做行带精修、直接按给的框填——那是给【图形标记】用的（商标是
    白框+条纹+黑字，按文字像素扩行只套得到字，见 erase_marks 的说明）。

    per_row=True 逐行跟背景渐变取色（见 _row_fill_colors），默认关：材质行那条验证过
    的路径就铺在平底卡片上，一个中位数色足够，不去动它。

    pad 是框外扩的像素：模型给的框普遍比实际标记紧一点（2026-10-08 实测差了十来个像素，
    商标外沿会留一小截彩色残影），图形标记那侧给 14。底色近纯校验照旧把关；逐行取色
    也让「扩多了」变得无害——越过盒沿落在白底上的那几行，取到的就是白色。
    """
    from PIL import Image
    im = Image.open(image_path).convert("RGB")
    w, h = im.size
    px = im.load()
    jobs = []
    for box in boxes:
        x1, y1, x2, y2 = _refine_box(px, w, h, box) if refine else box
        X1, Y1 = max(0, x1 - pad), max(0, y1 - pad)
        X2, Y2 = min(w, x2 + pad), min(h, y2 + pad)
        ring = []
        for x in range(X1, X2, 3):
            for y in (Y1, Y2 - 1):
                if not _is_ink(px[x, y]):
                    ring.append(px[x, y])
        for y in range(Y1, Y2, 3):
            for x in (X1, X2 - 1):
                if not _is_ink(px[x, y]):
                    ring.append(px[x, y])
        if not ring:
            logger.warning("定点抹除放弃：行框外沿取不到背景像素")
            return {"ok": False, "why": "行框外沿取不到背景像素"}
        ring.sort()
        bg = ring[len(ring) // 2]
        # 【均匀度要看「主体占比」，不能看全距】行框边缘常常蹭到彩色主体（文字行
        # 就贴在商品边上的情况实测存在），全距 max-min 必然爆表，但外沿大头是
        # 均匀的背景色、填中位数完全没问题。故判「与中位数近色的样本占比」：
        # 不到七成才是真的杂底（双色拼接、纹理），那种填出来是一块色疤，不抹。
        near = sum(1 for c in ring
                   if max(abs(c[i] - bg[i]) for i in range(3)) <= 40)
        if near < len(ring) * 0.7:
            logger.warning(f"定点抹除放弃：文字行背景非近纯色"
                           f"（外沿近色占比 {near}/{len(ring)}），填除会留色疤")
            return {"ok": False,
                    "why": "文字行背景非近纯色，填除会留色疤"}
        jobs.append((X1, Y1, X2, Y2, bg))
    for X1, Y1, X2, Y2, bg in jobs:
        row_colors = (_row_fill_colors(px, w, h, (X1, Y1, X2, Y2), bg)
                      if per_row else {})
        for y in range(Y1, Y2):
            color = row_colors.get(y, bg)
            for x in range(X1, X2):
                px[x, y] = color
        logger.info(f"定点抹除：({X1},{Y1})-({X2},{Y2}) 填 "
                    f"{'逐行跟渐变' if row_colors else bg}")
    im.save(out_path or image_path, quality=92)
    return {"ok": True, "boxes": len(jobs)}


async def erase_text_lines(image_path: str, texts: list, out_path: str = None) -> dict:
    """把图上的指定文字行定点抹掉。ok=False 时图片未被改动，调用方走原路径。"""
    boxes = await locate_text_lines(image_path, texts)
    if boxes is None:
        return {"ok": False, "why": "文字行定位失败"}
    try:
        return erase_boxes(image_path, boxes, out_path=out_path)
    except Exception as e:
        logger.warning(f"文字行填除异常（{e}），图片保持原样")
        return {"ok": False, "why": f"填除异常：{e}"}


# ---- 品牌标识（图形标记）的定点抹除 -------------------------------------------
# 【为什么不能复用文字行那条】商标不是「一行字」：2026-10-08 商品 1048494210610 包装盒
# 正面右上角那个标是【白底圆角方框 + 彩色条纹 + 黑字 382 TOYS】——按「文字行」问，模型
# 只会把框套到「382 TOYS」那几个字上，_refine_box 又按文字像素扩行带，抹完留下白框与
# 条纹，复检照样判品牌标识。故这边问「整个标记图形的外接框」，且【不做行带精修、整框照填】。
# 【为什么整框照填也安全】这类标就印在包装盒的近纯色大平面上：框松出去几像素，填的还是
# 同一种底色（蓝填蓝，肉眼无痕）；而「外沿近纯色」那道校验照旧把关——框若蹭到商品主体
# 或照片，近色占比不达标就直接放弃（留白不抹，交由重生/人工）。
# 【取向同 ⑤c 的材质行救援】生图侧做不到的选择性抹除，改由这一条确定性链路兜底，抹完
# 必须复检通过才认；定位失败/底色不纯/复检不过都退回原路径，不新增失败面。


async def locate_marks(image_path: str, texts: list) -> list:
    """视觉模型列出图上全部品牌标识/logo 标记（可见原文+外接框），本地按内容筛框。
    任何失败都返回 None（调用方回退重生路径），取向同 locate_text_lines。
    """
    wanted = [t for t in dict.fromkeys(texts) if t and str(t).strip()]
    if not wanted:
        return None
    prompt = (
        "把这张图上所有【品牌标识 / 第三方 logo】逐处列出：每处给出【可见的品牌名或"
        "标识原文】（照抄，例如 382 TOYS）和它【整个标记图形的外接矩形】——要连标记的"
        "外框与色块一起框进去，不是只框里面那几个字。"
        "坐标用 0 到 1 的归一化小数（相对图片宽高，x1,y1 为左上、x2,y2 为右下）。\n"
        '返回 JSON：{"marks": [{"text": "原文", "box": [x1, y1, x2, y2]}]}'
    )
    try:
        from PIL import Image
        with Image.open(image_path) as im:
            w, h = im.size
        data = await ask_json_with_images(prompt, [image_path], what="品牌标识定位")
    except Exception as e:
        logger.warning(f"品牌标识定位失败（{e}），放弃定点抹除")
        return None
    boxes = _match_boxes(data.get("marks") or [], wanted, w, h)
    if not boxes:
        logger.warning(f"品牌标识定位没有内容匹配的框（目标 {len(wanted)} 处、"
                       f"列出 {len(data.get('marks') or [])} 处），放弃定点抹除")
        return None
    return boxes


async def erase_marks(image_path: str, texts: list, out_path: str = None) -> dict:
    """把图上的指定品牌标识/logo 定点抹掉。ok=False 时图片未被改动，调用方走原路径。"""
    boxes = await locate_marks(image_path, texts)
    if boxes is None:
        return {"ok": False, "why": "品牌标识定位失败"}
    try:
        return erase_boxes(image_path, boxes, out_path=out_path,
                           refine=False, per_row=True, pad=14)
    except Exception as e:
        logger.warning(f"品牌标识填除异常（{e}），图片保持原样")
        return {"ok": False, "why": f"填除异常：{e}"}
