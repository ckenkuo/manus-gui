"""店小秘发布操作：media.skc。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import os
from app.logger import logger
from app.publish import images
from app.publish.browser import BrowserSession, J
from app.publish.media import (
    menus as media_menus,
    skc_scripts as media_skc_scripts,
    space as media_space,
)
from app.publish.upload import upload_image
from typing import Optional


# ==================== 阶段⑦ SKC 颜色图整行替换 ====================
# 【顺序关键，别改成「先删后挂」】2026-08-18 原脚本实测：行被删空后点「选择图片」
# 建立不了行绑定，空间弹窗会把图挂到别的行（驼色行的图挂进了卡其行）。故行内
# 【任何时刻都不能为空】：删旧图必须在挂了新图之后。
#
# 【挂一批删一批地交替，不是「先全挂再全删」——2026-08-24 定序、2026-08-26 改批】
# 原实现挂完 N 张再删 N 张，峰值 = 旧图数 + 新图数，6 旧 + 6 新 = 12 直接超上限 10，
# 于是不得不在开头「预删」2 张腾位置。预删是个坏补丁：预删完若挂图失败，行内图数就
# 【净减少】（实测咖啡色行 6 张预删 2 张后 open-space 失败，只剩 4 张），反复重跑会
# 一路把行削到下限以下、换出另一种保存报错。
# 改成交替后不再需要预删：每批只挂「行内当前余量」那么多张，挂完删掉同等数量旧图。
#     6 旧 → 挂 4 张（10，触顶不超）→ 删 4 张（6）→ 挂 2 张（8）→ 删 2 张（6）
# 既不触顶也不破下限。旧图恒在前列这个不变式没变（新图都挂在行末），所以仍是
# 「固定删第 1 张」，不必处理索引位移。
# 【行已满 10 张时先删一张腾位再挂】否则先挂就变 11 张、挂不进去（离线穷举
# 0..10 旧 × 1..10 新发现的反例，见 tests/test_publish_skc_order.py）。此时行内
# 从 10 掉到 9 再回到 10，【始终非空】，开头那条「不能删空」的约束仍然成立。
#
# 【为什么按批而不是逐张——2026-08-26 提速改造】空间弹窗支持【累积多选】：
# workspace/_probe_skc_space_modal.py 对 rowid 173539495455603009 真站探查，连点 3 张
# 后弹窗顶部计数逐次变成「已选择1/2/3张图片」，且每个 .img-item 的角标从「点击选择」
# 变成「取消选中」后【保持不变】。故一次弹窗可以勾完一整批。
# 逐张版每张都要走整轮「瞄点 → CDP 点行按钮 → 开菜单 → 开弹窗 → 选 1 张 → 确定 →
# 关弹窗」，实测（logs/20260826110756.log）6 行 29 张花 402s、每张 13.9s。
#
# 【选中态判据是 .img-check 的文本，不是 class】探查确认 item 的 className 恒为
# "img-item" 完全不变。别改回按 class 判——那会恒判未选中、整行换不了图。
#
# 【固定 sleep 一律改轮询——同次改造】同一份实测把每张 13.9s 拆开看：真实网络只占
# 1.1s（上传 0.8s + 回读 0.3s），其余约 11s 全是写死的等待。而探查测得的真实就绪时间：
#   - CDP 点击 → SKC 菜单出现：0ms（同步渲染），原硬等 1600ms
#   - 点「空间图片」→ 弹窗连图列表就绪：106ms，原硬等 3000ms
# 故等菜单/等弹窗/等删除回读/等滚动停稳四处都改成「轮询到就绪信号即走，超时才报错」。
# 别为了「稳」把 sleep 加回去：轮询的上限比原来的固定值更宽容，只是命中时不白等
# （tests/test_publish_speedup.py 有一条测试专门守这个）。
#
# 【上传可以并发，挂图必须串行】upload_image 只是三次 HTTP（取签名/PUT/登记），不碰
# 页面、彼此无关；而挂图要动 DOM。故整批图先并发传进图床，再逐批挂。并发数复用生图
# 那个用户旋钮（service.get_image_concurrency）：本质都是「同时开多少路网络」。
#
# 【行按钮必须 CDP 真实点击，JS click 无效——2026-08-20 亲自踩坑复现】
# 一开始以为 JS btn.click() 够用：菜单确实展开了。但那展开的是【素材图的菜单实例】
# （上一次 set_material 留下的残留绑定），于是选的图挂到了素材图上，把素材图替换掉了
# 且不报任何错——原脚本记录的「素材图误替换事故」就这么复现了一次。
# 用 CDP 真实点击（mouseMoved→mousePressed→mouseReleased，每步隔 120ms）后，页面才
# 新建出【SKC 专属菜单实例】，它有 5 项、带「应用到所有颜色」，与素材图的 4 项菜单
# 可以明确区分。故：
#   - 定位 SKC 菜单必须用「含『应用到所有颜色』」这条判据（原交接文档是对的）
#   - 行按钮点击必须走 CDP，且要 滚动 → 等停 → 另起 evaluate 读坐标 →
#     elementFromPoint 校验命中 再点（平滑滚动未停就读坐标会点偏）
#   - 每挂一张后【必须回读本行 src】：挂错位置时本行数量不增却不报错

# 判「图是否已入店小秘图床」的域名特征。图床域名是
# wxalbum-10001658-file.dianxiaomi.com（见 upload.WXALBUM_HOST），但这里只匹配主域：
# 未入图床的图是 1688 的 cbu01.alicdn.com 外链，两者主域就能区分，
# 匹配主域可避免图床子域变更（存储桶编号变了）时误判成外链。
DXM_IMAGE_HOST_MARK = "dianxiaomi.com"


# 每行图片上限（超过挂不进去）
SKC_ROW_MAX_IMAGES = 10

# 每行图片下限：表头写的是「图片(3-10张)」，少于 3 张同样过不了保存校验。
# 【这个下限不在 skc_replace_row 里把关，别在那儿加校验】它是【行最终状态】的约束，
# 而最终状态不只由换图这一步决定：多颜色商品里视觉按颜色归属给每行只分到 1~2 张是
# 常态（一件衣服的某个颜色不会有 6 张独立照片），换完不足 3 张时由 service 的
# _skc_size_fallback 把该行现有图原地做合规化补齐。2026-08-24 曾在换图入口拦
# 「新图 < 3 张」，结果 product-985713733384 的 4 个颜色行全被拦死、还把「入口拦下」
# 虚报成「换图失败」，掩盖了真失败。故这里只作为兜底那一层的判据。
SKC_ROW_MIN_IMAGES = 3


# SKC 菜单的判据：比素材图菜单【多一项「应用到所有颜色」】。
# 这是区分两个菜单实例的唯一可靠特征（2026-08-20 实测，见本节开头的踩坑记录）。
SKC_MENU_EXTRA_ITEM = "应用到所有颜色"


async def _skc_aim_row_button(session: BrowserSession, row_keyword: str) -> dict:
    """滚到某颜色行的「选择图片」按钮并求一个经 elementFromPoint 校验过的瞄点。

    【为什么要按不同 scrollIntoView 位置重试】残留的图片菜单是 position:fixed，
    上一张图挂完时它就停在视口中段；而每一行都 `block:'center'` 滚动，下一行的按钮
    被滚到的正是同一片区域，于是 9 个候选瞄点【整块】都被盖住（2026-08-24 起反复出现
    「落在 '引用采集图片' 上」的同一条报错，start/end 之外的退让从未被试过）。
    换 block 让按钮离开固定浮层覆盖的那条带，不动菜单也能命中，是最便宜的一招。

    顺序刻意是「center → 收浮层 → center → nearest/start/end」：先便宜后重，且每次
    都重新 elementFromPoint 校验，绝不会点到隔壁行。返回值同 _JS_SKC_BTN_POS。
    """
    last: dict = {}
    for i, block in enumerate(("center", "center", "nearest", "start", "end")):
        sc = await session.eval_json(
            media_skc_scripts._JS_SKC_BTN_SCROLL.replace("__KEY__", J(row_keyword))
            .replace("__BLOCK__", J(block)))
        if sc.get("err"):
            return {"err": sc["err"], "stage": "scroll"}
        # 【轮询等滚动停稳，不再硬等 1.2s】平滑滚动动画中途读坐标会点偏到隔壁行，
        # 这个约束没变；变的是判据——改成「连续两次读到的按钮 top 相同」即认为停稳，
        # 通常 200~300ms 就满足。一行 6 张图要瞄点 6 次，硬等在这一处每行白花约 6s。
        await _wait_scroll_settled(session, row_keyword)
        bp = await session.eval_json(media_skc_scripts._JS_SKC_BTN_POS.replace("__KEY__", J(row_keyword)))
        if bp.get("err"):
            return {"err": bp["err"], "stage": "locate"}
        if bp.get("hit"):
            if i or bp.get("aimAt") != "center":
                logger.info(f"行按钮瞄点：第 {i + 1} 轮 block={block} "
                            f"aimAt={bp.get('aimAt')} 命中")
            return bp
        last = bp
        if i == 0:
            # 第 1 轮未命中先收浮层：菜单能收掉时后面几轮都省了
            parked = await media_menus._park_image_menus(session)
            await asyncio.sleep(0.5)
            logger.info(f"行按钮被浮层遮挡（落在 {bp.get('atText')!r}），"
                        f"已尝试收浮层 parked={parked.get('parked')}，换位重瞄")
    return last


async def _wait_scroll_settled(session: BrowserSession, row_keyword: str) -> dict:
    """等某颜色行的平滑滚动停稳（best-effort：读不到就当停稳，交后续瞄点校验兜住）。

    停不稳也不报错：后面 _JS_SKC_BTN_POS 会做 elementFromPoint 校验，坐标不对时那层
    会判未命中并换 block 重瞄——本函数只是让「多数情况快得多」，不是新增一道闸。
    """
    try:
        r = await session.eval_json(
            media_skc_scripts._JS_SCROLL_SETTLED.replace("__KEY__", J(row_keyword)))
        if r.get("err"):
            # 行找不到是真问题，但报错留给紧随其后的 _JS_SKC_BTN_POS（它的错误信息更全）
            await asyncio.sleep(0.6)
        return r
    except Exception as e:
        logger.warning(f"等滚动停稳失败（按固定等待兜底）：{e}")
        await asyncio.sleep(1.2)
        return {"err": str(e)}


async def _wait_skc_menu(session: BrowserSession) -> dict:
    """轮询等 CDP 点击后新建的 SKC 菜单就绪（上限 8s）。

    实测菜单同步渲染（0ms），这层只为兜住偶发的慢一拍，代价是命中时几乎零等待。
    """
    r = await session.eval_json(
        media_skc_scripts._JS_WAIT_SKC_MENU.replace("__EXTRA__", J(SKC_MENU_EXTRA_ITEM)))
    if r.get("ready"):
        return r
    return {"err": f"CDP 点击后 8s 内没出现 SKC 菜单（含「{SKC_MENU_EXTRA_ITEM}」）",
            "visibleMenus": r.get("visibleMenus")}


async def _skc_open_space(session: BrowserSession, row_keyword: str) -> dict:
    """点某颜色行的「选择图片」→「空间图片」，打开空间弹窗。

    分三步而不是一个 evaluate 搞定，每一步都是为了绕开一个实测过的坑，见各 JS 常量
    上方的注释。核心是行按钮【必须 CDP 真实点击】才会新建 SKC 专属菜单实例；
    瞄点由 _skc_aim_row_button 负责（含收浮层与换滚动位置的重试）。
    """
    bp = await _skc_aim_row_button(session, row_keyword)
    if bp.get("err"):
        return bp
    if not bp.get("hit"):
        # 带上遮挡物：blockers 同时覆盖弹窗与残留菜单，非空即能定位到具体是谁挡的
        bl = bp.get("blockers") or []
        hint = f"，疑似有浮层未收起：{bl}" if bl else ""
        return {"err": (f"瞄点未命中行按钮（落在 {bp.get('atText')!r}"
                        f" class={bp.get('atClass')!r}）{hint}"),
                "stage": "aim", "pos": bp}

    # CDP 真实点击三连（JS click 不会新建 SKC 菜单，见本节开头）
    await media_menus._cdp_click_xy(session, bp["x"], bp["y"])
    # 【轮询等菜单，不再硬等 1.6s】2026-08-26 真站实测（_probe_skc_space_modal.py）：
    # 菜单是同步渲染的，CDP 点击返回时就已在 DOM 里（menuReadyMs=0）。硬等 1.6s 是
    # 纯浪费——一个商品 6 行 29 张图就白等 46s。仍留轮询而不是直接不等：菜单由真实
    # 事件触发，理论上可能慢一拍，等到了就走、没等到由下一步报「SKC 菜单不在」。
    waited = await _wait_skc_menu(session)
    if waited.get("err"):
        return {"err": waited["err"], "stage": "menu"}

    return await session.eval_json(
        media_skc_scripts._JS_SKC_CLICK_SPACE
        .replace("__EXTRA__", J(SKC_MENU_EXTRA_ITEM))
        .replace("__TITLE__", J(media_space.SPACE_MODAL_TITLE))
    )


async def _skc_row_state(session: BrowserSession, row_keyword: str) -> dict:
    """读某颜色行当前的图片数量、完整 URL 与尺寸，并标出破服装类下限的那些。

    tooSmall 是 [{"idx", "url", "size"}]：服装类 1340×1785 是保存时的硬校验，而这
    一行的图可能压根没被阶段⑦ 换过（视觉分不出该颜色的图时整行会被跳过），留在页面
    上的就是 1688 原始小图。2026-08-24 真站实测：咖啡色行 6 张全是 1000×1000 /
    1200×1200 的 cbu01.alicdn.com 外链，阶段⑫ save 报「服装类图片尺寸不能小于
    1340px * 1785px」。故读行状态时一并把这个判据给出来，供调用方做尺寸兜底。

    foreign 是仍挂在非店小秘图床上的图（1688 外链），与 tooSmall 一起构成「这一行
    还没被本管线换过」的判据——续跑时靠它跳过已换好的行，见 _skc_row_matches。
    """
    st = await session.eval_json(
        media_skc_scripts._JS_SKC_ROW_STATE.replace("__KEY__", J(row_keyword)))
    if st.get("err"):
        return st
    urls, sizes = st.get("urls") or [], st.get("sizes") or []
    small = []
    for i, u in enumerate(urls):
        wh = sizes[i] if i < len(sizes) else None
        w, h = (wh or [0, 0])[:2]
        if w and h and (w < images.CLOTH_MIN_W or h < images.CLOTH_MIN_H):
            small.append({"idx": i, "url": u, "size": f"{w}x{h}"})
    st["tooSmall"] = small
    st["foreign"] = [u for u in urls if DXM_IMAGE_HOST_MARK not in u]
    return st


def _skc_row_matches(state: dict, file_ids: list, expect_count: int) -> dict:
    """判某颜色行是否已经就是「这一批」图，返回 {"done", "reason"}。

    【为什么需要这个】阶段⑤~⑬ 的表单成果一重载就丢，续跑很常见；而原先主路径
    拿到视觉选图就无条件整行换，上一轮已经换好的行（图已入店小秘图床、尺寸达标）
    会被再换一遍——6 张图约一分钟纯白工。

    【判据分两档，宽判据刻意不足以判定"已完成"】
      - 有 file_ids（上一轮换图成功时落进状态文件的清单）：逐个比对页面 src，
        全中才算完成。这是唯一能真正认出"就是这一批图"的判据。
      - 无 file_ids（旧状态文件、或上一轮没跑到这一行）：不判完成，照常换。
        宽判据（数量对 + 都在店小秘图床 + 尺寸达标）认不出"6 张达标图但内容不是
        这一批"，误判会把错图留在页面上还不报错，代价远高于白跑一轮。
    """
    if not file_ids:
        return {"done": False, "reason": "无上一轮的 fileId 清单，无法确认页面上是这一批图"}
    if state.get("count") != expect_count:
        return {"done": False,
                "reason": f"行内 {state.get('count')} 张，本轮要挂 {expect_count} 张，数量不符"}
    if state.get("foreign"):
        return {"done": False,
                "reason": f"仍有 {len(state['foreign'])} 张非店小秘图床的外链图"}
    if state.get("tooSmall"):
        return {"done": False,
                "reason": f"仍有 {len(state['tooSmall'])} 张图低于 "
                          f"{images.CLOTH_MIN_W}x{images.CLOTH_MIN_H}"}
    srcs = state.get("srcs") or []
    missing = [f for f in file_ids
               if not any(f.rsplit("/", 1)[-1] in x for x in srcs)]
    if missing:
        return {"done": False,
                "reason": f"{len(missing)}/{len(file_ids)} 张上一轮的图不在页面上，内容已变"}
    return {"done": True, "reason": f"页面上就是上一轮挂的 {len(file_ids)} 张图"}


async def skc_image_support(session: BrowserSession) -> dict:
    """变种属性区是否支持按颜色配图。返回 {"supported": bool|None, ...}。

    supported=False：区块已渲染（有复选框）但三个图片位信号全为 0 → 本类目不支持，
    调用方应当 skipped。
    supported=None：区块没渲染完（连复选框都没有）→ 证据不足，别当成「不支持」，
    照旧走原路让真失败暴露出来。
    """
    st = await session.eval_json(media_skc_scripts._JS_SKC_IMAGE_SUPPORT)
    if not st.get("section") or not st.get("checkboxes"):
        return {"supported": None, **st}
    has_slot = bool(st.get("imgs") or st.get("pickBtns") or st.get("imageCells"))
    return {"supported": has_slot, **st}


async def skc_replace_row(session: BrowserSession, row_keyword: str, img_dir: str,
                          full_cid: Optional[str] = None) -> dict:
    """阶段⑦ 整行替换某颜色的 SKC 图（服装类需 3:4 且不小于 1340×1785）。

    img_dir 里的图按【文件名排序】逐张挂上，故颜色专属图命名 01.jpg 就落在首位、
    免拖拽。图应当已做过 fit_34 合规化（本函数不代做，理由同 set_material）。

    流程：一挂一删地交替（见本节开头的顺序说明），行内数量在 旧/新 数量之间小幅
    摆动，从不触顶、从不为空。任一步失败都立即返回并带上 attached/deletedOld 计数——
    此时行内是「新旧混杂」的中间态，需要人工看一眼再决定是重跑还是手工收拾，所以
    刻意不做自动回滚（回滚同样要靠这套脆弱的页面交互，失败时反而更难判断现场状态）。
    交替顺序让这个中间态比原先的「先全挂再全删」更安全：任意时刻行内图数都不低于
    min(旧图数, 新图数)，不会像预删那样把行削残。
    """
    exts = (".jpg", ".jpeg", ".png", ".webp")
    try:
        files = sorted(f for f in os.listdir(img_dir) if f.lower().endswith(exts))
    except OSError as e:
        return {"status": "error", "stage": "precheck", "err": f"读目录失败: {e}"}
    if not files:
        return {"status": "error", "stage": "precheck", "err": f"目录无图片: {img_dir}"}
    # 【只拦上限，不拦下限——2026-08-24 踩坑后改回】超过 10 张是真的挂不进去，必须拦；
    # 但新图少于 3 张【不能拦】：多颜色商品里视觉按颜色归属给每行分到 1~2 张是常态
    # （实测 product-985713733384：8 张主图分 4 个颜色，每行 1~2 张），一件衣服的某个
    # 颜色本来就不会有 6 张独立照片。行的下限 3 张是【最终状态】的约束，而最终状态
    # 不只由本轮新图决定——换完不足 3 张时 service 的 _skc_size_fallback 会把该行
    # 现有图原地做合规化补齐（实测那 4 行最后都补到了 6 张达标图）。
    # 在这里拦死等于把兜底的工作机会掐掉，还会把「我拦的」虚报成「换图失败」，
    # 掩盖真正的失败。故下限只由兜底那一层负责。
    if len(files) > SKC_ROW_MAX_IMAGES:
        return {"status": "error", "stage": "precheck",
                "err": (f"新图 {len(files)} 张超过每行上限 {SKC_ROW_MAX_IMAGES} 张，"
                        "多出来的会静默挂不进去；请减少图片张数"),
                "newFiles": len(files)}

    base = await _skc_row_state(session, row_keyword)
    if base.get("err"):
        return {"status": "error", "stage": "locate", **base}
    old_count = base["count"]
    logger.info(f"SKC 行「{row_keyword}」现有 {old_count} 张，准备换成 {len(files)} 张新图"
                f"（一挂一删交替，不再预删）")

    async def _del_first(stage: str, ctx: dict):
        """删本行第 1 张（恒为最老的旧图）。成功返回 None，失败返回给调用方的错误 dict。"""
        d = await session.eval_json(media_skc_scripts._JS_SKC_DEL_FIRST.replace("__KEY__", J(row_keyword)))
        if not d.get("deleted"):
            return {"status": "error", "stage": stage, "detail": d, **ctx}
        # 原固定 sleep(0.4) 已去掉：_JS_SKC_DEL_FIRST 内部已轮询等待删除完成（上限 6s）
        return None

    attached: list = []
    remaining_old = old_count      # 还没删掉的旧图数，恒在行内最前面
    deleted = 0

    # 【先并发把整批图传进图床，再逐批挂】上传只是三次 HTTP（取签名/PUT/登记），不碰
    # 页面、彼此无关，故可以并发；而挂图要动页面，必须串行。2026-08-26 实测每张上传
    # 0.8s，一行 6 张串行就是 5s 白等。并发数用与生图同一个用户旋钮：都是「同时开多少
    # 路网络」，链路差时该一起调小（见 service.get_image_concurrency）。
    from app.publish.preferences import get_image_concurrency

    sem = asyncio.Semaphore(get_image_concurrency())

    async def _up(fname: str) -> dict:
        async with sem:
            path = os.path.join(img_dir, fname)
            r = await upload_image(session, path, full_cid=full_cid)
            return {"file": fname, **r}

    ups = await asyncio.gather(*(_up(f) for f in files))
    bad = [u for u in ups if u.get("status") != "ok"]
    if bad:
        return {"status": "error", "stage": "upload", "upload": bad[0],
                "attached": [], "deletedOld": 0, "file": bad[0]["file"]}
    logger.info(f"整批 {len(ups)} 张已传入图床（并发上传），开始分批挂图")

    # 【分批而不是逐张：一次弹窗能勾多张——2026-08-26 真站探查证实】每批的大小由
    # 两条真站约束夹出来（与逐张版完全相同的约束，只是现在按批算）：
    #   - 行内不能超 SKC_ROW_MAX_IMAGES：本批能挂 上限 - 当前行内图数 张；
    #   - 行内不能为空：旧图只能在挂了新图【之后】删，故每批挂完才删同等数量。
    # 行已满时先删一张腾位（同逐张版的「行满则先删后挂」，理由见本节开头注释）。
    idx = 0
    while idx < len(ups):
        in_row = remaining_old + len(attached)
        room = SKC_ROW_MAX_IMAGES - in_row
        if room <= 0:
            # 行已满：先删一张腾位。此时从上限掉 1 张再挂回来，【始终非空】。
            if remaining_old <= 0:
                # 不该发生（新图数已在入口按上限校验过），但真到了就如实报错而不是死循环
                return {"status": "error", "stage": "no-room",
                        "err": f"行内已满 {in_row} 张且无旧图可删，无法继续挂图",
                        "attached": [a["file"] for a in attached], "deletedOld": deleted}
            err = await _del_first("delete-old",
                                   {"attached": [a["file"] for a in attached],
                                    "deletedOld": deleted})
            if err:
                return err
            remaining_old -= 1
            deleted += 1
            continue

        batch = ups[idx:idx + room]
        ctx = {"attached": [a["file"] for a in attached], "deletedOld": deleted,
               "file": batch[0]["file"], "batch": [b["file"] for b in batch]}

        opened = await _skc_open_space(session, row_keyword)
        if opened.get("err") or not opened.get("opened"):
            return {"status": "error", "stage": "open-space", "detail": opened, **ctx}
        picked = await media_space._pick_many_from_space(session, [b["fileId"] for b in batch])
        if picked.get("err"):
            # 半选状态绝不点确定（_pick_many_from_space 已保证没点），关掉弹窗再报错：
            # 留着开着会盖住后续所有操作（同 ensure_desc_closed 那类踩坑）
            await media_space._close_space_modal(session)
            return {"status": "error", "stage": "pick", "detail": picked, **ctx}

        # 回读校验这一批都挂到了【本行】：菜单实例全页共用，挂错行时本行数量不增却不报错
        st = await _skc_row_state(session, row_keyword)
        srcs = st.get("srcs") or []
        missed = [b["file"] for b in batch
                  if not any(b["fileId"].rsplit("/", 1)[-1] in x for x in srcs)]
        if missed:
            logger.error(f"图 {missed} 挂载后未出现在「{row_keyword}」行，可能挂到了别的行")
            return {"status": "error", "stage": "verify-row", "rowState": st,
                    "missed": missed, **ctx}
        attached.extend({"file": b["file"], "fileId": b["fileId"]} for b in batch)
        idx += len(batch)

        # 【每批挂完就主动收菜单，不要等下一批被挡了再救】菜单是 position:fixed 停在
        # 视口中段，而下一行按钮也会被滚到视口中段，几何上正好重叠——事后补救要靠
        # 换滚动位置绕（见 _skc_aim_row_button），成本远高于这里顺手收一次。
        # best-effort：收不掉也继续，瞄点那一层还有退让。
        await media_menus._park_image_menus(session)

        # 挂成功后把同等数量的旧图删掉，位置还回去——这是交替的核心（按批版）
        for _ in range(min(len(batch), remaining_old)):
            err = await _del_first("delete-old",
                                   {"attached": [a["file"] for a in attached],
                                    "deletedOld": deleted})
            if err:
                return err
            remaining_old -= 1
            deleted += 1
        logger.info(f"已挂 {len(batch)} 张（{'、'.join(b['file'] for b in batch)}）"
                    f"，行内现 {len(attached) + remaining_old} 张，"
                    f"待删旧图 {remaining_old} 张")

    # 新图比旧图少时（如 6 旧换 3 新）还有剩余旧图，收尾删干净
    while remaining_old > 0:
        err = await _del_first("delete-old",
                               {"attached": [a["file"] for a in attached],
                                "deletedOld": deleted})
        if err:
            return err
        remaining_old -= 1
        deleted += 1

    final = await _skc_row_state(session, row_keyword)
    ok = final.get("count") == len(files)
    if not ok:
        logger.error(f"行「{row_keyword}」收尾数量不符：期望 {len(files)}，实际 {final.get('count')}")
    return {"status": "ok" if ok else "error",
            "stage": "" if ok else "final-count",
            "row": row_keyword, "oldCount": base["count"],
            "attached": [a["file"] for a in attached],
            # fileId 清单供续跑判「本行是否已是这一批图」（见 _skc_row_matches）
            "fileIds": [a["fileId"] for a in attached], "deletedOld": deleted,
            "finalCount": final.get("count"), "finalSrcs": final.get("srcs")}
