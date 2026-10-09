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
【2026-10-09 换代】这条链路从「整框填一个背景色」改成「逐像素背景重建 + 只填标记像素」——
老实现要求「框外沿近纯色占比 ≥70%」，压在渐变/纹理背景上或框里混进产品结构时必然放弃
（Klokflip、荆牌两例实测）；新机制为什么能扛住、拿什么挡「从框外伸进来的结构」，见
品牌标识那一段的长注释。文字行那条路径（`erase_text_lines`）不受影响，仍是原来的整框填色。
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
    """行文的 token 集（小写字母数字串 + 中日韩单字）：内容匹配用，对标点/大小写/连字符不敏感。

    【为什么要收中日韩单字】原实现只按 [^a-z0-9] 切，中文全被当成分隔符丢掉——纯中文的
    标记原文 token 集为空，_match_boxes 里「token 空则跳过」与「want 空则永不命中」两道
    直接把中文目标筛没，定位注定失败。2026-10-09 实录：1069210564470 第 1 张轮播图的
    「荆牌/荆牌智造」，定位模型明明列出了 4~5 处标记，却一处都配不上，⑤c 三发重生全废、
    最后交人工。这条链路 2026-10-08 是为英文标（382 TOYS）加的，当时只验了英文。
    中文按【单字】切：品牌名多为 2~4 字，2 字目标要求两字全中（≥0.6 即 2/2 或 4 字中 3 字），
    够严也够用；整词切分要引入分词依赖，超出这里「别把框安错行」的精度需求。
    """
    import re
    s = str(text).lower()
    toks = {t for t in re.split(r"[^a-z0-9]+", s) if t}
    toks |= set(re.findall(r"[一-鿿぀-ヿ가-힯]", s))
    return toks


def _near_token(a: str, b: str) -> bool:
    """两个 token 是不是【同一个词的转录抖动】，不是「两个不同的词」。

    【为什么要有这条】目标原文与定位模型的转录来自两次独立调用，同一处标识会抖：
    2026-10-09 实录（989696721265 第 6 行预览图）QC 给 "Klokfilp"、定位给 "Klokflip"
    （第 6/7 位互换），纯 token 精确交集为空 —— 定位判失败、整条定点抹除链路退回人工，
    而那张图正是该单卡在 ⑦b 的原因。抖动的形态是【相邻字符换位】与【个别字符替换】，
    都不是新词；只要还按精确相等判，这类救援就会在看运气的地方失效。
    【闸门：只在够长的词上启用，且字符计数差异 ≤2】短词（4 字母的 down/dawn）差两个
    字符就是另一个词了，放进来等于把框安到别的行头上的口子重新打开（见 _match_boxes
    的框错行取证）；5 字母以上、差两个字符以内，落到别的词上的概率极低。计数差异而不是
    编辑距离：换位与替换在这把尺子上都只算 2，正是要盖的那两种抖动。
    """
    if a == b:
        return True
    if len(a) < 5 or len(b) < 5 or abs(len(a) - len(b)) > 1:
        return False
    from collections import Counter
    ca, cb = Counter(a), Counter(b)
    return sum((ca - cb).values()) + sum((cb - ca).values()) <= 2


def _match_boxes(lines: list, targets: list, w: int, h: int) -> list:
    """从模型列出的全部文字行里，按【原文内容】挑出目标是材质行的框。

    【为什么不能信「模型说这就是那行」】定位模型会把框安到别的行头上
    （2026-10-08 F 组实测：让它框 "Filling: Down Cotton"，它把 Name 行框了回来——
    框错行若照抹，Name 这种该保留的行就被静默挖掉，而 QC 不管名称缺失，拦不住）。
    故让它连原文一起报，本地按 token 重叠过滤：转录与目标对不上的框一律拒。
    拒绝的代价只是「该抹的没抹」——复检拦下、回退重生，是安全方向。
    【token 相等之外还要认「转录抖动」】见 _near_token：同一处标识两次转录会差一两个
    字符，按精确相等判会让整条链路失效（2026-10-09 实录）。
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
            if not want:
                continue
            matched = sum(1 for t in want
                          if t in line_tokens
                          or any(_near_token(t, u) for u in line_tokens))
            if matched / len(want) >= 0.6:
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


def erase_boxes(image_path: str, boxes: list, out_path: str = None,
                pad: int = 10) -> dict:
    """精修并填除若干文字行。背景不是近纯色的框拒绝抹（留色疤不如不抹）。

    【只服务文字行】品牌标识（图形标记）那条走 _erase_mark_boxes，它逐像素重建背景、
    不看这里的「整框一个色」闸门（理由见品牌标识那段长注释）。pad 是框外扩的像素：
    模型给的框普遍比实际标记紧一点，商标外沿会留一小截彩色残影。
    """
    from PIL import Image
    im = Image.open(image_path).convert("RGB")
    w, h = im.size
    px = im.load()
    jobs = []
    for box in boxes:
        x1, y1, x2, y2 = _refine_box(px, w, h, box)
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
                   if max(abs(c[i] - bg[i]) for i in range(3)) <= _BG_TOL)
        if near < len(ring) * 0.7:
            logger.warning(f"定点抹除放弃：文字行背景非近纯色"
                           f"（外沿近色占比 {near}/{len(ring)}），填除会留色疤")
            return {"ok": False,
                    "why": "文字行背景非近纯色，填除会留色疤"}
        jobs.append((X1, Y1, X2, Y2, bg))
    for X1, Y1, X2, Y2, bg in jobs:
        for y in range(Y1, Y2):
            for x in range(X1, X2):
                px[x, y] = bg
        logger.info(f"定点抹除：({X1},{Y1})-({X2},{Y2}) 填 {bg}")
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
# 条纹，复检照样判品牌标识。故这边问「整个标记图形的外接框」，且【不做行带精修】。
#
# 【2026-10-09 换代：整框填一色 → 逐像素背景重建 + 只填标记像素】
# 老实现把整个外扩框刷成一个中位背景色，并要求「框外沿近纯色（容差 40）占比 ≥70%」。
# 两种常见画面必然被这道闸门拦死：
#   a) 背景是渐变或带纹理——占比重来凑不到七成（2026-10-09 两例实测：荆牌压在橙红渐变
#      顶栏、Klokflip 压在蓝色盒盖上，占比 311/482 = 64.5%）；
#   b) 框里混进了产品结构（盒沿剪影、提手孔）——占比同样掉下来。
# 而「整框照填」对 (b) 是毁灭性的：那是 2026-10-08 已经踩过的坑（框里有实物时整块被
# 抹平，商品直接消失）。故换成逐像素，几件事各归各位（细节见各自函数的 docstring）：
#   1. 背景模型 B：逐像素取 4 向射线，在候选色里找【两两一致的最大簇】按距离反比加权。
#      渐变/纹理靠「就近加权」自然跟上，不再要求整框一个色。射线取两遍（_local_rays）：
#      表面在检测区里就结束的像素（Klokflip 的字压在盒盖边上）只有本地射线才估得对。
#   2. 标记像素 = 与 B 偏离够大的像素，分强弱两档（_hysteresis）：强偏离当种子，弱偏离
#      只有长在种子上才算——盒盖高光带那种「半脏」的大片区域因此不会成片进来。
#   3. 只留【被表面包在中间】的连通成分（_drop_outer_components）：触及检测区边界、且
#      偏离强度只是「中等」的，是从框外伸进来的结构（提手孔、铰链、白底剪影），丢掉。
#   4. 填补只取【干净同表面】的参考像素，并在干净像素上做均值滤波（_surface_image）——
#      单像素取色会把它的噪声沿整行铺成横条纹。
#   5. 闸门：框内没有标记 / 背景模型立不住（大半框都与背景不符）/ 一眼就是标记的像素有
#      5% 以上落在掩膜外（框太小、或标记与框外结构连成一片）/ 有像素四周都判不出背景
#      ——任一命中都 ok=False 且【图片原样不动】，交调用方走重生或人工。
# 【已知够不着的一类】标记与它旁边的结构「同色且连成一片」时无解：Klokflip 那张的浮雕
# 红字字尾与盒盖红边连在一起，抹字就得连红边一起抹（那是商品结构），分开抹又必然留残影
# ——闸门如实放弃、交人工。真要自动抹这种，得走「把掩膜交给生图模型做局部重绘」那条路
# （本机三家网关的 mask 参数还没验过），不是本地像素规则能补的。
# 【取向同 ⑤c 的材质行救援】生图侧做不到的选择性抹除，改由这一条确定性链路兜底，抹完
# 必须复检通过才认；定位失败/闸门不过/复检不过都退回原路径，不新增失败面。

# 逐通道差 ≤ 该值算「同色」：射线投票与文字行的近纯色闸同一口径。
_BG_TOL = 40
# 检测区在模型框外的外扩：用来判「这团东西是从框外伸进来的，还是标记自己」。
# 【为什么是 30】两头都要让开：外来的贯通结构得能从检测区边界探出去才能被识别（Klokflip
# 那张的提手孔纵跨到 y≈570，框底 +30 仍探得出去；+42 就整块落进来了，会被误抹 23%）；
# 而模型给的框普遍比实际标记紧十来像素（2026-10-08 实测，实测里还见过更紧的），检测区
# 盖不住标记的话，标记自己会贴到边界上、白丢一截。30 是这两条之间实测可行的一档。
_MARK_PAD_DETECT = 30


def _ray_background(patch, rays: list, tol: float):
    """逐像素背景模型：4 向射线里取两两一致的最大簇，簇内按距离反比加权。

    rays 是 [(颜色 (bh,bw,3), 距离 (bh,bw), 有效 (bh,bw))]——无效的方向不参与一致性判定
    （本地射线常常整条路都是标记，那个方向就没有参考）。返回 (B, trust)：B 是背景色估计
    （float32），trust 表示「该像素取到了 ≥2 条一致的有效射线」。只有一条射线、或几条
    互相都不一致（多材质交界，比如框里正好压着产品轮廓）的像素【判不出背景】——这类像素
    既不算标记也不参与填色，宁可少抹（见 _erase_one_mark 的闸门）。
    """
    import itertools
    import numpy as np

    bh, bw = patch.shape[:2]
    n = len(rays)
    cols = np.stack([c for c, _, _ in rays])
    dist = np.stack([d for _, d, _ in rays])
    valid = np.stack([v for _, _, v in rays])
    agree = np.zeros((n, n, bh, bw), bool)
    for i in range(n):
        for j in range(i + 1, n):
            same = (np.abs(cols[i] - cols[j]).max(-1) <= tol) & valid[i] & valid[j]
            agree[i, j] = same
            agree[j, i] = same
    # 从大到小找「团」：4 个候选只有 11 个子集，直接枚举比写团算法直观
    size = np.zeros((bh, bw), np.int8)
    pick = np.zeros((n, bh, bw), bool)
    for k in range(n, 1, -1):
        for comb in itertools.combinations(range(n), k):
            ok = np.ones((bh, bw), bool)
            for i in range(k):
                ok &= valid[comb[i]]
                for j in range(i + 1, k):
                    ok &= agree[comb[i], comb[j]]
            ok &= size < k
            if not ok.any():
                continue
            size = np.where(ok, k, size)
            for i in comb:
                pick[i] |= ok
    wts = np.where(pick, 1.0 / np.maximum(dist, 1.0), 0.0)
    tot = wts.sum(0)
    B = np.zeros_like(patch, dtype=np.float32)
    np.divide((cols * wts[..., None]).sum(0), np.maximum(tot, 1e-9)[..., None],
              out=B, where=(tot > 0)[..., None])
    return B, size >= 2


def _components(mask) -> list:
    """4 邻域连通域，返回 [[(y, x), ...], ...]。"""
    from collections import deque
    import numpy as np

    bh, bw = mask.shape
    lab = np.zeros((bh, bw), np.int32)
    out = []
    ys, xs = np.nonzero(mask)
    cur = 0
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if lab[sy, sx]:
            continue
        cur += 1
        lab[sy, sx] = cur
        queue = deque([(sy, sx)])
        pts = []
        while queue:
            y, x = queue.popleft()
            pts.append((y, x))
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < bh and 0 <= nx < bw and mask[ny, nx] and not lab[ny, nx]:
                    lab[ny, nx] = cur
                    queue.append((ny, nx))
        out.append(pts)
    return out


def _hysteresis(weak, strong):
    """弱偏离像素只在与强偏离像素连通时才认（把标记的描边、抗锯齿、浮雕高光一起带走）。

    【为什么要两档】只按一个阈值切，两头都不对：阈值取低（40），盒盖上的高光带、表面明暗
    交界这类「半脏」的区域会成片进来，抹完像被人擦过一道（Klokflip 实测：框内 33% 的像素
    被判成标记，其中一大团是盒盖的高光带，中位偏差才 59）；取高（80），低对比的标（浅灰
    logo 压白底）就整个漏掉。分两档：强偏离当种子，弱偏离只有长在种子上才算标记。
    """
    import numpy as np

    keep = np.zeros(weak.shape, bool)
    for pts in _components(weak):
        if any(strong[y, x] for y, x in pts):
            for y, x in pts:
                keep[y, x] = True
    return keep


def _drop_outer_components(mask, dev, tol: float):
    """丢掉「从框外伸进来」的成分：触及检测区边界、且不像标记的那些。

    【为什么这道是关键】框内出现的白底剪影、盒沿、铰链、提手孔、相邻物件，全都是「从框外
    伸进来」的——它们的成分必然铺到检测区边界上。而标记是被表面围住的：Klokflip 那张实测，
    字形的每个字母都是独立成分、都与边界留着十来个像素，提手孔那条暗带则一路穿出边界。
    【为什么不能「触及边界就一律丢」】模型给的框常常比标记紧十来像素（2026-10-08 实测），
    标记自己就会贴到边界上——一律丢会把整块标记丢掉、白白退回人工。区分靠偏离强度：外来
    结构（盒盖红边、提手孔暗带）的偏差是「中等」，而标记是「极强」（382 TOYS 的实测中位
    偏差 214，红边 91），故中位偏差够强的边界成分仍按标记保留。
    """
    import numpy as np

    bh, bw = mask.shape
    keep = np.zeros((bh, bw), bool)
    for pts in _components(mask):
        touch = any(y in (0, bh - 1) or x in (0, bw - 1) for y, x in pts)
        if touch:
            d = np.array([dev[y, x] for y, x in pts])
            if float(np.median(d)) < 3 * tol:
                continue
        for y, x in pts:
            keep[y, x] = True
    return keep


def _dilate(mask, k: int = 1):
    """4 邻域膨胀 k 圈。"""
    import numpy as np

    out = mask.copy()
    for _ in range(k):
        t = out.copy()
        t[1:, :] |= out[:-1, :]
        t[:-1, :] |= out[1:, :]
        t[:, 1:] |= out[:, :-1]
        t[:, :-1] |= out[:, 1:]
        out = t
    return out


def _surface_image(patch, clean, k: int = 9):
    """只在【干净表面像素】上做均值滤波，得到一张「表面色」图，供填补取色。

    【为什么要这一步】直接拿「最近的那个参考像素」当填色，那个像素自己的噪声会沿着整行
    铺成一条横条纹（2026-10-09 在 382 TOYS 那张上肉眼可见：抹除区出现一层层横杠）。
    抹除面积大时尤其明显——一条行的填色取自一个像素，而行有几百像素长。
    窗口内只统计干净像素（积分图算），所以不会把标记/结构/过渡像素混进来；k=9 是为了
    既能压掉噪声、又不至于把表面的光照渐变抹平。
    """
    import numpy as np

    h, w = clean.shape
    wt = clean.astype(np.float32)
    pad = k // 2

    def box_sum(a):
        p = np.pad(a, ((pad + 1, pad), (pad + 1, pad)), mode="edge")
        c = p.cumsum(0).cumsum(1)
        return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k])[:h, :w]

    den = box_sum(wt)
    num = np.stack([box_sum(patch[..., i] * wt) for i in range(3)], axis=-1)
    out = np.where(den[..., None] > 0, num / np.maximum(den, 1e-6)[..., None], patch)
    return out.astype(np.float32)


def _fill_masked(patch, mask, trust, B, tol: float):
    """把掩膜像素填成周围的背景色。返回 (填好的图, 填不动的像素数)。

    【填充顺序】行内左右两端插值（跟着水平渐变走）→ 退同一列上下拷贝（跟着垂直渐变走，
    与 2026-10-08 营救链那套「行内左右接近就插值、否则上下拷贝」同源）→ 再退背景模型的
    加权值。都不行就不填、如实计入。
    【参考像素必须「与标记像素同一表面」】两道判据：一是它本身得是背景
    （|像素 − B| ≤ tol，挡掉提手孔那种也在掩膜外的暗结构）；二是它的背景模型要和被填
    像素的背景模型接近（|B(参考) − B(被填)| ≤ tol）。
    第二道是 2026-10-09 目检抓出来的：Klokflip 那张的框内有盒沿外的白底，白底像素自己
    是「可信背景」，于是被当成参考色，把白插值进了字形里——抹出来是一层白蒙蒙的残影
    （QC 双检还判不出，只有 1:1 目检看得见）。背景模型已经把「表面」分好了，照它筛参考
    即可：白底的 B 是白、盒面的 B 是蓝，两边不会串。
    """
    import numpy as np

    bh, bw = mask.shape
    ref = trust & (np.abs(patch - B).max(-1) <= tol) & ~mask
    # 取色走「干净表面均值图」，不用单像素：单像素的噪声会沿整行铺成横条纹（见那里注释）
    surf = _surface_image(patch, ref)
    out = surf.copy()
    done = np.zeros((bh, bw), bool)
    cols = np.arange(bw)[None, :]
    idx_l = np.maximum.accumulate(np.where(ref, cols, -1), axis=1)
    idx_r = np.minimum.accumulate(np.where(ref, cols, bw)[:, ::-1], axis=1)[:, ::-1]
    rows_i = np.arange(bh)[:, None]
    idx_u = np.maximum.accumulate(np.where(ref, rows_i, -1), axis=0)
    idx_d = np.minimum.accumulate(np.where(ref, rows_i, bh)[::-1, :], axis=0)[::-1, :]

    def same_surface(y, x, ry, rx) -> bool:
        return bool(trust[y, x]) and \
            float(np.abs(B[y, x] - B[ry, rx]).max()) <= tol

    for y, x in zip(*np.nonzero(mask)):
        l = idx_l[y, x]
        while l >= 0 and not same_surface(y, x, y, l):
            l = idx_l[y, l - 1] if l > 0 else -1
        r = idx_r[y, x]
        while r < bw and not same_surface(y, x, y, r):
            r = idx_r[y, r + 1] if r + 1 < bw else bw
        if l >= 0 and r < bw:
            wl, wr = float(x - l), float(r - x)
            out[y, x] = (surf[y, l] * wr + surf[y, r] * wl) / (wl + wr)
        elif l >= 0:
            out[y, x] = surf[y, l]
        elif r < bw:
            out[y, x] = surf[y, r]
        else:
            u = idx_u[y, x]
            while u >= 0 and not same_surface(y, x, u, x):
                u = idx_u[u - 1, x] if u > 0 else -1
            d = idx_d[y, x]
            while d < bh and not same_surface(y, x, d, x):
                d = idx_d[d + 1, x] if d + 1 < bh else bh
            if u >= 0:
                out[y, x] = surf[u, x]
            elif d < bh:
                out[y, x] = surf[d, x]
            elif trust[y, x]:
                out[y, x] = B[y, x]      # 四周取不到参考，就用射线模型给的表面色
            else:
                continue                 # 背景模型也没有，保持原样
        done[y, x] = True
    # 没被填的像素保持原样：填色图只在掩膜处取用，掩膜外原样交回
    out[~mask] = patch[~mask]
    return out, int((mask & ~done).sum())


def _local_rays(patch, blocked, tol: float) -> list:
    """把「射线」从检测区边界改成本地：每个像素取 4 个方向上【最近的非掩膜像素】当参考。

    【为什么不能只用边界射线】标记所在的那个表面可能在检测区【里面】就结束（Klokflip 那张
    的字就压在盒盖边上，框顶还留着一截盒沿外的白底）：从边界拉过来的射线会一路穿出物体，
    把白底当成这个像素的背景——于是字形被填成了白蒙蒙的残影（QC 双检还判不出来）。
    本地射线取的是「紧挨着标记的那一圈表面」，这个问题自然消失。

    blocked 是粗掩膜（第一遍边界射线的结果）：拿它当路障，射线才不会停在标记自己身上。
    返回 [(颜色 (bh,bw,3), 距离 (bh,bw)), ...]，方向缺失时少给几条。
    """
    import numpy as np

    bh, bw = patch.shape[:2]
    free = ~blocked
    cols = np.arange(bw)[None, :]
    rows = np.arange(bh)[:, None]
    idx = {"l": (np.maximum.accumulate(np.where(free, cols, -1), axis=1), 1, -1),
           "r": (np.minimum.accumulate(np.where(free, cols, bw)[:, ::-1], axis=1)[:, ::-1], 1, bw),
           "u": (np.maximum.accumulate(np.where(free, rows, -1), axis=0), 0, -1),
           "d": (np.minimum.accumulate(np.where(free, rows, bh)[::-1, :], axis=0)[::-1, :], 0, bh)}
    rays = []
    for ix, axis, missing in idx.values():
        ok = ix != missing
        if not ok.any():
            continue
        safe = np.where(ok, ix, 0)
        if axis == 1:
            color = patch[np.arange(bh)[:, None], safe]
            dist = np.abs(safe - cols).astype(np.float32)
        else:
            color = patch[safe, np.arange(bw)[None, :]]
            dist = np.abs(safe - rows).astype(np.float32)
        # 无效方向给 0 色 + 有效位 False：0 乘权重 0 不会污染加权和（NaN 会）
        rays.append((np.where(ok[..., None], color, 0.0).astype(np.float32),
                     np.maximum(dist, 1.0), ok))
    return rays


def _border_rays(arr, D: tuple, w: int, h: int) -> list:
    """检测区外紧邻一圈的射线色（粗背景用，见 _mark_mask 第一遍）。

    只取检测区【外】的一列/一行：这一遍要的是「大致哪儿偏离背景」，粗一点没关系，
    第二遍本地射线才做精细估计。
    """
    import numpy as np

    dx1, dy1, dx2, dy2 = D
    bh, bw = dy2 - dy1, dx2 - dx1
    rows = np.arange(bh, dtype=np.float32)[:, None]
    cols = np.arange(bw, dtype=np.float32)[None, :]
    yes = np.ones((bh, bw), bool)
    rays = []
    if dx1 > 0:
        c = np.broadcast_to(arr[dy1:dy2, dx1 - 1][:, None, :], (bh, bw, 3))
        rays.append((c, np.broadcast_to(cols + 1, (bh, bw)), yes))
    if dx2 < w:
        c = np.broadcast_to(arr[dy1:dy2, dx2][:, None, :], (bh, bw, 3))
        rays.append((c, np.broadcast_to(bw - cols, (bh, bw)), yes))
    if dy1 > 0:
        c = np.broadcast_to(arr[dy1 - 1, dx1:dx2][None, :, :], (bh, bw, 3))
        rays.append((c, np.broadcast_to(rows + 1, (bh, bw)), yes))
    if dy2 < h:
        c = np.broadcast_to(arr[dy2, dx1:dx2][None, :, :], (bh, bw, 3))
        rays.append((c, np.broadcast_to(bh - rows, (bh, bw)), yes))
    return rays


def _mark_mask(patch, border_rays: list) -> dict:
    """算一处标记的掩膜与背景模型。返回 {mask, dev, strong, trust, B, why}，why 非空即放弃。

    两层射线（先边界粗估、再本地精估）+ 强弱两档 + 连通成分判定，细节见各步注释。确定性
    填补（_erase_one_mark）与「掩膜交给生图模型局部重绘」（_mask_png）都从这里拿掩膜，
    判据只此一份。
    """
    import numpy as np

    if len(border_rays) < 2:
        return {"why": "检测区取不到足够的背景射线"}
    B0, trust0 = _ray_background(patch, border_rays, _BG_TOL)
    rough = trust0 & (np.abs(patch - B0).max(-1) > _BG_TOL)
    # 第二遍：本地射线（跳过粗掩膜）重算背景——表面在检测区里就结束的像素靠这一遍纠回来；
    # 表面交界处的像素会因几个方向的参考互不一致而判不出背景，于是既不判标记也不填（保住盒沿）。
    local = _local_rays(patch, rough, _BG_TOL)
    if len(local) < 2:
        return {"why": "标记四周取不到足够的背景参考"}
    B, trust = _ray_background(patch, local, _BG_TOL)
    dev = np.abs(patch - B).max(-1)
    strong = trust & (dev > 2 * _BG_TOL)      # 一眼就是标记（表面纹理/光照到不了这个量级）
    weak = trust & (dev > _BG_TOL)
    cand = strong | _hysteresis(weak, strong)
    if not cand.any():
        return {"why": "框内没有与背景不符的标记像素"}
    # 大面积与背景不符说明背景模型本身立不住（框里塞满东西、或标记占满整个框）
    if cand.mean() > 0.6:
        return {"why": "框内大面积与背景不符，背景模型不可信"}
    keep = _drop_outer_components(cand, dev, _BG_TOL)
    # 【往外扩 2px 把标记的软边包住】标记的投影、抗锯齿边、浮雕高光的偏差常常落在容差以下
    # （382 TOYS 那张的白色方框底下有一圈软阴影），只按判据切会留下一圈看得见的残影。
    mask = _dilate(keep, 2)
    if not mask.any():
        return {"why": "标记压在框边或与框外结构相连，无法安全抹除"}
    return {"mask": mask, "dev": dev, "strong": strong, "trust": trust, "B": B, "why": ""}


def _scar_check(patch, mask, filled, tol: float, min_px: int = 20) -> bool:
    """填出来的颜色像不像它周围原本的样子？True = 有疤。

    【为什么要逐个成分查、不能整块查】整块掩膜的「周围中位色」会被大面积表面盖住，一小撮
    填歪的像素根本拱不动中位数——2026-10-09 实测（Klokflip 同一张图、框漂到另一种位置时）：
    字的上半截被填成白蒙蒙一条，整块中位数判过关、QC 复检也只查残留照样过关，只有 1:1 目检
    看得见。故按连通成分各查各的：每个成分外扩 4px 取环、环的【原始像素】中位色当基准，
    成分内填色与它差太远就是疤。
    【基准用原始像素、不经过背景模型】背景模型自己判错时会一起错过去（实测框漂到相邻白色
    图案区时，B 认成白、填补也填白，在蓝盒面上横刷出一条白带）。
    """
    import numpy as np

    for pts in _components(mask):
        if len(pts) < min_px:
            continue
        cm = np.zeros_like(mask)
        ys = [p[0] for p in pts]
        xs = [p[1] for p in pts]
        cm[ys, xs] = True
        ring = _dilate(cm, 4) & ~mask
        if not ring.any():
            continue
        med = np.median(patch[ring], axis=0)
        off = np.abs(filled[ys, xs] - med).max(-1)
        if float(np.median(off)) > tol:
            return True
    return False



def _erase_one_mark(arr, out, box: tuple, w: int, h: int) -> str:
    """用本地像素填补抹掉一处标记。成功返回空串（结果已写进 out），失败返回放弃原因。"""
    import numpy as np

    x1, y1, x2, y2 = box
    D = (max(0, x1 - _MARK_PAD_DETECT), max(0, y1 - _MARK_PAD_DETECT),
         min(w, x2 + _MARK_PAD_DETECT), min(h, y2 + _MARK_PAD_DETECT))
    dx1, dy1, dx2, dy2 = D
    patch = arr[dy1:dy2, dx1:dx2]
    info = _mark_mask(patch, _border_rays(arr, D, w, h))
    if info["why"]:
        return info["why"]
    mask, dev, strong, trust, B = (info["mask"], info["dev"], info["strong"],
                                  info["trust"], info["B"])
    # 【抹不全就放弃】一眼就是标记的像素（strong）必须全部落在最终掩膜里。漏掉的两种情形都会
    # 留下看得见的残缺：标记被框外结构连坐丢掉（2026-10-09 Klokflip 那张：浮雕红字的字尾和盒盖
    # 红边连成一片，一丢就是半截字），或者框太小盖不住标记。这种改走局部重绘兜底（见 erase_marks）。
    if (strong & mask).sum() < strong.sum() * 0.95:
        return "标记有像素落在抹除范围之外（框太小或与框外结构相连），抹不干净"
    filled, unfilled = _fill_masked(patch, mask, trust, B, _BG_TOL)
    if unfilled > mask.sum() * 0.2:
        return "标记处背景多面交界，填除会留色疤"
    # 【填出来的颜色必须像它周围原本的样子】逐成分查（见 _scar_check 的取证）。
    if _scar_check(patch, mask, filled, _BG_TOL):
        return "填出来的颜色与周围不像（会留色疤），放弃定点抹除"
    out[dy1:dy2, dx1:dx2][mask] = filled[mask]
    logger.info(f"定点抹除：框 ({x1},{y1})-({x2},{y2}) 抹掉 {int(mask.sum())} 像素"
                f"（逐像素背景填补，检测区 {patch.shape[0]}x{patch.shape[1]}）")
    return ""



def _erase_mark_boxes(image_path: str, boxes: list, out_path: str = None) -> dict:
    """按标记框逐像素抹除（同步实现，供 erase_marks 调用，也便于单测直接喂框）。

    【全部框都过了闸门才落盘】任一处放弃就整体不写文件——与老实现「整框放弃」的语义
    一致，调用方（⑦b/⑤c）据此保持「ok=False 时图片未被改动」这条契约。
    """
    from PIL import Image
    import numpy as np

    im = Image.open(image_path).convert("RGB")
    w, h = im.size
    arr = np.asarray(im).astype(np.float32)
    out = arr.copy()
    for box in boxes:
        why = _erase_one_mark(arr, out, box, w, h)
        if why:
            logger.warning(f"定点抹除放弃：{why}（框 {box}）")
            return {"ok": False, "why": why}
    Image.fromarray(out.astype(np.uint8)).save(out_path or image_path, quality=92)
    return {"ok": True, "boxes": len(boxes)}


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
    except Exception as e:
        logger.warning(f"品牌标识定位失败（{e}），放弃定点抹除")
        return None
    # 【为什么没配到框要再问一遍】「这一问没有内容匹配的框」不等于图上没有标记：定位模型每次
    # 给的框都在漂、偶尔整处漏列（2026-10-09 实测同一张图前后两次结果不同）。一次没配到就退回
    # 人工，等于让这条兜底在看运气的地方失效；再问一遍只多一次视觉调用，命中率实打实高一截。
    for attempt in (1, 2):
        try:
            data = await ask_json_with_images(prompt, [image_path], what="品牌标识定位")
        except Exception as e:
            logger.warning(f"品牌标识定位失败（{e}），放弃定点抹除")
            return None
        boxes = _match_boxes(data.get("marks") or [], wanted, w, h)
        if boxes:
            return boxes
        logger.warning(f"品牌标识定位没有内容匹配的框（第 {attempt}/2 次：目标 {len(wanted)} 处、"
                       f"列出 {len(data.get('marks') or [])} 处）")
    return None


# 局部重绘交给生图模型时的提示词：只动掩膜内，按周围表面补全。
# 【为什么要专门写「别画字母/浮雕/轮廓」】模型会照着掩膜的形状把原来的字形「描」回来：
# 2026-10-09 Klokflip 实测，只说「按周围表面补全」时留下一层浅色浮雕鬼影（复检直接读成
# 「压印的字母标识」而判脏），点名禁掉字形与浮雕之后同一张图复检 clean、目检也无痕。
_MASK_INPAINT_PROMPT = (
    "Repaint ONLY the transparent masked area of this image: erase whatever brand logo, "
    "brand name, label or text was there, and fill it with an even continuation of the "
    "surrounding surface. Match the neighbouring colour, shading and texture. Do NOT draw "
    "any letters, symbols, embossing, outlines or shapes in the filled area. "
    "Keep every pixel outside the mask exactly unchanged."
)


def _mask_png(image_path: str, boxes: list) -> tuple:
    """生成 OpenAI 口径的重绘掩膜 PNG（【透明处】= 要重画），返回 (临时文件路径, 覆盖像素数)。

    掩膜取与确定性链路同一套判据（_mark_mask），再往外扩 3px 把标记的软边、投影一起交给
    模型。某处连掩膜都算不出来时（框里没有与背景不符的像素等）退化成「框 + 4px」的矩形，
    把判断交给模型——总比整条链路直接交人工强。
    """
    import os
    import tempfile
    import numpy as np
    from PIL import Image

    im = Image.open(image_path).convert("RGB")
    w, h = im.size
    arr = np.asarray(im).astype(np.float32)
    alpha = np.full((h, w), 255, np.uint8)
    covered = 0
    for box in boxes:
        x1, y1, x2, y2 = box
        D = (max(0, x1 - _MARK_PAD_DETECT), max(0, y1 - _MARK_PAD_DETECT),
             min(w, x2 + _MARK_PAD_DETECT), min(h, y2 + _MARK_PAD_DETECT))
        dx1, dy1, dx2, dy2 = D
        info = _mark_mask(arr[dy1:dy2, dx1:dx2], _border_rays(arr, D, w, h))
        m = info.get("mask") if not info["why"] else None
        if m is None or not m.any():
            pad = 4
            m = np.zeros((dy2 - dy1, dx2 - dx1), bool)
            m[max(0, y1 - pad) - dy1:y2 + pad - dy1,
              max(0, x1 - pad) - dx1:x2 + pad - dx1] = True
        # 【比确定性链路多扩几像素】掩膜外的东西模型一律保留，标记的浮雕高光、投影、抗锯齿
        # 边常常落在判据之外（Klokflip 实测：红字抹掉了，字形的浅色浮雕还留着一圈鬼影，
        # 复检直接读成「压印的字母标识」）。多扩 5px 把这些一起交给模型重画。
        m = _dilate(m, 5)
        alpha[dy1:dy2, dx1:dx2][m] = 0
        covered += int(m.sum())
        logger.info(f"定点抹除兜底：框 {box} 的掩膜 {int(m.sum())} 像素")
    # 【RGB 通道填常量，别把原图塞进去】OpenAI 口径只看 alpha（透明处=要重画），RGB 无关；
    # 塞原图等于把这份 PNG 变成一张照片——真实 1792² 素材实测 1.8~2.2MB、合成大图见过
    # 12.9MB，而官方对 mask 有 <4MB 的硬线，逼近或越线就会被网关整张请求拒掉，整条兜底
    # 链路白瞎。填常量后同一张掩膜只剩几十 KB（标记像素就那几千个）。
    # 【改动风险已被兜住】真有个网关把 mask 当图层合成（拿 RGB 去贴非透明区），最坏也只是
    # 掩膜区内重绘得不对——而 _generative_erase 只把掩膜区贴回原图、贴回前还要过 _recheck
    # 复检，坏结果进不了成品，不会污染掩膜外的画面。
    rgba = np.dstack([np.zeros((h, w, 3), np.uint8), alpha])
    tmp = tempfile.NamedTemporaryFile(prefix="mark-mask-", suffix=".png", delete=False)
    tmp.close()
    Image.fromarray(rgba, "RGBA").save(tmp.name)
    return tmp.name, covered


async def _generative_erase(image_path: str, boxes: list, out_path: str = None) -> dict:
    """确定性链路抹不掉时的兜底：把算好的掩膜交给生图模型做【局部重绘】。

    【为什么这条路能抹掉确定性链路抹不掉的】「标记与旁边的结构同色且连成一片」这类问题，
    本地像素规则无解——没有任何局部判据能区分同一块红色是标记还是盒沿；而局部重绘是在
    语义层做的：掩膜内的内容整体重画、按周围表面补全，模型会把「字」去掉、把「盒沿」顺着
    画回来。这也是「把这块东西擦掉」的标准解法；生图侧原先三发抹不掉，是因为只给了提示词
    没给掩膜（要它自己找、自己决定抹什么）。2026-10-09 Klokflip 那张（浮雕红字字尾与盒盖
    红边相连）实测：字迹清干净、提手孔与盒沿原样、QC 双检 clean。
    【只取抹除区那一块，其余一律用原图】出图模型是整张重渲染的（实测：掩膜只占 1.4%，
    掩膜外仍有 4.3% 的像素差 >60、全图平均差 9——它按自己的理解把整张图重画了一遍，连带
    把别处的细节和画质一起改了）。把它整张换上去等于让整条素材链路降质、还可能悄悄改掉
    商品本身，所以只把掩膜区（羽化 3px）贴回原图，其余像素保证逐位不变。
    【代价与安全】烧一发生图，只在确定性链路放弃时才走；抹完照旧由调用方复检通过才认，
    失败就按老路子退回人工，不新增失败面。
    """
    import os
    import tempfile
    import numpy as np
    from PIL import Image, ImageFilter
    from app.publish import images

    mask_png, covered = _mask_png(image_path, boxes)
    painted_png = tempfile.NamedTemporaryFile(prefix="mark-inpaint-", suffix=".png",
                                              delete=False)
    painted_png.close()
    # 【实际落盘路径要以 result["output"] 为准】出图侧的 compress 收尾会把 .png 改名成
    # 同名 .jpg 并删掉那个 .png（见 images.compress），只按 painted_png.name 清理会漏掉
    # 那份全尺寸 JPEG——兜底每跑一次就在 %TEMP% 里积一份。
    produced = painted_png.name
    try:
        result = await images.edit_image_async(
            image_path, prompt=_MASK_INPAINT_PROMPT, out_path=painted_png.name,
            mask_path=mask_png, no_downscale=True)
        produced = result.get("output") or produced
        orig = Image.open(image_path).convert("RGB")
        painted = Image.open(result["output"]).convert("RGB")
        if painted.size != orig.size:
            painted = painted.resize(orig.size, Image.LANCZOS)
        with Image.open(mask_png) as m:
            hole = np.asarray(m.convert("RGBA").getchannel("A"))
        alpha = Image.fromarray(255 - hole).filter(ImageFilter.GaussianBlur(3))
        Image.composite(painted, orig, alpha).save(out_path or image_path, quality=92)
    finally:
        for tmp in (mask_png, painted_png.name, produced):
            if os.path.exists(tmp):
                os.unlink(tmp)
    logger.info(f"定点抹除兜底：掩膜 {covered} 像素交生图模型局部重绘，只取该区域贴回原图")
    return {"ok": True, "boxes": len(boxes), "how": "inpaint"}



async def _recheck(image_path: str) -> tuple:
    """自己先复检一次，返回 (是否 clean, qc)。

    【为什么要自己检】两条路线的取舍不能靠「哪条跑完了」定：本地像素填补有时会留下一片
    1:1 目检才看得见、QC 又说不清的痕迹（2026-10-09 实测：字上半截被填成白蒙蒙一条，被
    复检读成「水印 / 白色小字」），那种必须换局部重绘再来一次，否则整行照样退回人工。
    调用方（⑦b/⑤c）事后还会再检一次，这里多花一次质检换的是「抹不干净自动换招」。
    """
    from app.publish import vision

    qc = await vision.check_cleaned_twice(image_path)
    return qc.get("clean") is True, qc


async def erase_marks(image_path: str, texts: list, out_path: str = None) -> dict:
    """把图上的指定品牌标识/logo 定点抹掉。ok=False 时图片未被改动，调用方走原路径。

    【两条路线，按可靠性递进】
      1. 本地像素的确定性填补（_erase_mark_boxes）：快、不花钱、可复现；抹完自己复检
         （_recheck），过了就收工。
      2. 局部重绘（_generative_erase）：掩膜交给生图模型重画那一块。确定性填补放弃了、
         或抹完复检不过，都落到这里。代价是一发生图，但这是专门用来解「标记与旁边结构
         同色相连」这类本地规则无解的画面的。
    两条都不成 → ok=False，调用方按老口径退回人工，不新增失败面。
    """
    boxes = await locate_marks(image_path, texts)
    if boxes is None:
        return {"ok": False, "why": "品牌标识定位失败"}
    import os
    import shutil
    import tempfile

    target = out_path or image_path
    # 【兜底必须从原图起手】本地像素那一步可能已经把图改花了（填出一片痕迹），若在它上面
    # 再抹，掩膜只圈得住那片痕迹、圈不住还在的标记残片——2026-10-09 实测正因如此漏了一截
    # 红字。故先留一份原图，改用局部重绘时先还原。
    backup = tempfile.NamedTemporaryFile(prefix="mark-orig-", suffix=".png", delete=False)
    backup.close()
    shutil.copy2(image_path, backup.name)
    why = ""
    try:
        result = _erase_mark_boxes(image_path, boxes, out_path=out_path)
    except Exception as e:
        logger.warning(f"品牌标识填除异常（{e}）")
        result = {"ok": False, "why": f"填除异常：{e}"}
    try:
        if result.get("ok"):
            passed, qc = await _recheck(target)
            if passed:
                return {"ok": True, "boxes": len(boxes), "how": "pixel"}
            logger.warning(f"本地像素抹除后复检未过（brandMark={qc.get('brandMark')}、"
                           f"{qc.get('issues') or '无结论'}），改走局部重绘兜底")
        else:
            why = result.get("why", "")
            logger.warning(f"定点抹除（本地像素）放弃：{why}，改走局部重绘兜底")
        shutil.copy2(backup.name, target)
        try:
            await _generative_erase(target, boxes, out_path=out_path)
        except Exception as e:
            logger.warning(f"定点抹除兜底（局部重绘）失败（{e}），退回人工")
            return {"ok": False, "why": why or "定点抹除失败"}
        # 【复检不过必须把图还原成原图】本函数 docstring 承诺「ok=False 时图片未被改动」，
        # 而这条分支若直接返回，留在文件里的是【没通过复检的重绘产物】。当前两个调用方
        # 恰好都丢弃失败分支的文件（⑦b 返回空串、⑤c 删掉 out），所以一直没暴露；但契约
        # 一旦被新调用方按字面用（先拿路径、失败后再读该路径）就会把未验证的图当合规图
        # 用出去。还原走 backup，与上面「兜底从原图起手」用的是同一份。
        passed, qc = await _recheck(out_path or image_path)
        if passed:
            return {"ok": True, "boxes": len(boxes), "how": "inpaint"}
        shutil.copy2(backup.name, target)
        logger.warning(f"局部重绘后复检仍未过（brandMark={qc.get('brandMark')}、"
                       f"{qc.get('issues') or '无结论'}），已还原原图并退回人工")
        return {"ok": False, "why": f"局部重绘后复检仍未过：{qc.get('issues') or '未给出原因'}"}
    finally:
        if os.path.exists(backup.name):
            os.unlink(backup.name)
