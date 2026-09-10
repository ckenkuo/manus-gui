"""店小秘发布操作：media.description_replace。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
from app.logger import logger
from app.publish import images
from app.publish.browser import BrowserSession, J
from app.publish.media import (
    description as media_description,
    description_replace_scripts as media_description_replace_scripts,
    description_scripts as media_description_scripts,
    menus as media_menus,
)
from app.publish.upload import upload_image
from typing import Optional


# ---- 阶段⑪ 描述图替换（中文图英化后回填）------------------------------------
# 【描述编辑器有自己的一套菜单，菜单项与素材图/SKC 完全不同】2026-08-20 实测：
#   素材图 / SKC 行：本地图片 / 空间图片 / 网络图片 / 引用采集图片[ / 应用到所有颜色]
#   描述编辑器：    本地上传 / 空间上传 / 网络上传 / 引用产品轮播图 / 引用采集图片 / 小秘美图
#                   （2026-08-28 实测文案；该项原名「引用skc轮播图」，平台改过一次）
# 注意是「空间【上传】」不是「空间【图片】」。我起初照素材图那套去找「空间图片」，
# 在 JS click / CDP 点击 / 清浮层之间反复试错都失败——可见菜单始终是素材图那个残留
# 实例，而真正的描述菜单一直隐藏着没被触发到。判据见下方 DESC_MENU_ITEMS。
#
# 触发序列（缺一步都不行）：
#   1. CDP 真实点击模块图 → 右侧面板出现「更换图片」链接（JS click 建立不了绑定）
#   2. CDP 真实点击「更换图片」→ 描述专属菜单展开在链接旁（left/top 贴着链接）
#   3. 点菜单里「空间上传」→ 空间弹窗打开
#   4. 弹窗内选中刚直传的图 → 确定
# 第 3、4 步必须在同一个 evaluate 里连贯做完：菜单会自动收起。
#
# 【不要对描述编辑器做 elementFromPoint 校验】编辑器是全屏 modal，任何坐标都会命中
# 编辑器内的 IMG，看着像「被遮挡」其实正常——我在这里差点误判并放弃这条路。

# 描述编辑器菜单的特征项（组合在页面上唯一）。
#
# 【2026-08-28 平台把「引用skc轮播图」改名成「引用产品轮播图」】判据是
# `__ITEMS__.every(w => t.includes(w))` 的精确全等匹配，一项改名就整条失配：
# 菜单其实【已经展开】（实测可见项 ['本地上传','空间上传','网络上传',
# '引用产品轮播图','引用采集图片','小秘美图']），却被判成「未展开」，
# 于是重点两次 + 派发事件兜底全部空转，9 张描述图无一替换成功。
# 日志证据：logs/20260828183839.log 里 32 次「描述专属菜单未展开」，每次都把
# 那份正确的菜单项清单原样打了出来——判据与现实只差「skc」→「产品」两个字。
#
# 故只保留【两个平台没动过、且与素材图菜单相区分的项】：
#   「空间上传」——素材图那套是「空间图片」，这一项就能区分两个菜单实例；
#   「小秘美图」——素材图菜单没有它。
# 轮播图那一项不再进判据：它的文案已经变过一次，就该假定还会再变。
DESC_MENU_ITEMS = ("空间上传", "小秘美图")


async def _desc_aim_replace_link(session: BrowserSession, idx: str) -> dict:
    """求一个经 elementFromPoint 校验过的「更换图片」瞄点；被遮挡时换模块图滚动位置重试。

    idx 是模块图的 0 基下标（要重滚模块图，故必须知道点的是哪一个）。

    【为什么单靠 _JS_DESC_REPLACE_LINK 内部的修正不够】那段 JS 减 .ant-modal-body 的
    scrollTop 把链接从顶栏底下推出来，但 scrollTop 已经是 0 时无处可推
    （2026-08-27 probe_desc_moved.py 正是为验证这一点写的）。此时唯一还能动的量是
    【模块图滚到视口的哪个位置】——右侧面板跟着模块走，模块换个落点，链接也就换个高度。
    这与 _skc_aim_row_button 用 block 退让绕开 fixed 浮层是同一招，代价也一样低。

    【重滚模块图必须连带重点一次】滚动会让模块失去选中态、右侧面板随之消失（
    probe_desc_click_modes.py 的注释记的就是这个约束），所以每轮都得重新 CDP 点模块图。

    顺序「center → nearest → start → end」：先用与原实现相同的落点（多数情况一轮即过），
    再由近及远退让。每轮都重新校验，绝不返回未命中的坐标当成功——命中不了就把最后一轮
    的诊断原样返回，由调用方决定是报错还是改走无坐标的兜底。
    """
    last: dict = {}
    link_js = media_description_replace_scripts._JS_DESC_REPLACE_LINK.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL)

    async def _hover_link() -> dict:
        """把鼠标移到链接位置让它浮上来，再求瞄点。

        【这才是「菜单未展开」的真因——2026-08-28 三跑排查的终点】链接外层那个 div 的
        z-index 随 hover 变化：未 hover 是 **-1**（在 IMG.image-box 预览大图【下面】,
        elementFromPoint 命中 IMG，校验必然失败），hover 后变 **2**（浮到图上面，
        命中 A 链接本身）。真站取证：只发一个 mouseMoved 到链接中心、不点任何东西，
        onLink 就从 False 变 True。
        故原先「滚容器调链接高度 + 12 点采样」整套从一开始就治错了方向——链接位置
        一直是好的（top=227），缺的只是一次 hover。_cdp_click_xy 里确实有 mouseMoved，
        但那在瞄点求出来【之后】，求瞄点这一步过不去就永远走不到点击。
        """
        r = await session.eval_json(link_js)
        if r.get("err") or r.get("onLink"):
            return r
        # 用链接矩形中心作 hover 落点：此刻它可能还在图下面，但 mouseMoved 只看坐标，
        # 命中谁都会让该位置的 :hover 链生效
        x, y = r.get("x"), r.get("y")
        if x is None or y is None:
            return r
        # best-effort：hover 发不出去（会话不支持 CDP 输入）不该让整个求瞄点崩掉，
        # 退回未 hover 的结果交后续退让轮次处理，与本项目辅助路径的一贯取向一致
        try:
            await session.cdp("Input.dispatchMouseEvent",
                              {"type": "mouseMoved", "x": x, "y": y})
        except Exception as e:
            logger.warning(f"hover 链接失败（跳过 hover 直接判瞄点）：{e}")
            return r
        await asyncio.sleep(0.45)
        r2 = await session.eval_json(link_js)
        if r2.get("onLink"):
            logger.info(f"「更换图片」链接 hover 后浮出（z-index -1 → 2），瞄点 ({x},{y}) 命中")
            return r2
        return r2 if not r2.get("err") else r

    for i, block in enumerate(("center", "nearest", "start", "end")):
        if i:
            # 换落点重滚 + 重点模块图（不重点则右侧面板不在，读链接必然失败）
            sc = await session.eval_json(
                media_description_replace_scripts._JS_DESC_BOX_SCROLL.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL)
                .replace("__IDX__", idx).replace("__BLOCK__", J(block)))
            if sc.get("err"):
                return {"err": sc["err"], "stage": "scroll"}
            await asyncio.sleep(1.0)
            bp = await session.eval_json(
                media_description_replace_scripts._JS_DESC_BOX_POS.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL).replace("__IDX__", idx))
            if bp.get("err"):
                return {"err": bp["err"], "stage": "locate"}
            await media_menus._cdp_click_xy(session, bp["x"], bp["y"])
            await asyncio.sleep(1.5)
        lp = await _hover_link()
        if lp.get("err"):
            return lp
        if lp.get("onLink"):
            if i:
                logger.info(f"「更换图片」瞄点：第 {i + 1} 轮 block={block} 命中"
                            f"（链接 top={lp.get('linkTop')}，"
                            f"遮挡带下沿={lp.get('blockerBottom')}）")
            elif lp.get("fixTried"):
                logger.info(f"「更换图片」链接被遮挡，滚容器修正后命中："
                            f"{lp.get('fixTried')}")
            return lp
        last = lp
        # 【把「在视口下方」与「被上方压住」分开报】两者的修正方向相反（见
        # _JS_DESC_REPLACE_LINK 的双向推注释），日志混在一起会让人照着「遮挡带」
        # 去查顶栏，而真因可能是链接掉到了视口下沿之外（hit 恒为 None 就是这个特征）。
        below = (lp.get("linkTop") or 0) > (lp.get("innerH") or 0) > 0
        why = "在视口下方" if below else f"被遮挡（下沿={lp.get('blockerBottom')}）"
        logger.info(f"「更换图片」瞄点落在 {lp.get('hitTag')}（{lp.get('hitAt')}）上"
                    f"，链接 top={lp.get('linkTop')} {why} "
                    f"bodyScrollTop={lp.get('bodyScrollTop')} "
                    f"修正尝试={lp.get('fixTried')}；换模块图落点重瞄")
        if i == 0:
            # 第 1 轮未命中先收一次残留图片菜单：那类浮层是 fixed，收掉后面几轮都省了
            # （历史上 hitAt=ant-dropdown 的遮挡正是靠这一步救回的）
            await media_menus._park_image_menus(session)
            await asyncio.sleep(0.5)
            lp2 = await _hover_link()
            if not lp2.get("err") and lp2.get("onLink"):
                logger.info("收起残留浮层后「更换图片」瞄点命中")
                return lp2
            if not lp2.get("err"):
                last = lp2
    return last


async def desc_replace(session: BrowserSession, pos: int, image_path: str,
                       full_cid: Optional[str] = None,
                       expect_url: Optional[str] = None) -> dict:
    """阶段⑪ 把第 pos 个描述模块的图换成本地图（序号从 1 起）。

    典型用途：中文描述图经 images.edit_image 英化后回填。
    英化质量【必须另做视觉质检】——实测生图会残留拼音、误译品类，只看「中文没了」
    会把带乱码文案的图挂上去（见 llm.ask_json_with_images）。本函数只负责替换动作。

    【expect_url 是防错位的硬闸门】pos 是描述区的【当前】序号，删过模块后整体前移，
    调用方拿旧序号进来会张冠李戴：越界时报错还算好的，没越界则静默把 A 图的英化产物
    贴到 B 图上——收尾的 landed 只校验「新 fileId 落在了 pos」，看不出位置错。故调用方
    只要知道该替换哪张源图，就把它的 URL 传进来，替换前后各比对一次；不一致直接拒绝
    动作，把静默错位变成显式失败（2026-08-24 实测：⑬ 删 3 张后按旧 pos 6 替换，
    描述区只剩 5 个模块）。

    改完记得调 desc_save：描述编辑器点「关闭」即丢弃改动。
    """
    st = await media_description._desc_ensure_open(session)
    if st.get("err"):
        return {"status": "error", **st}
    n = st.get("count") or 0
    if pos < 1 or pos > n:
        return {"status": "error", "stage": "precheck",
                "err": f"序号 {pos} 越界（当前共 {n} 个模块）"}
    # 先用已读到的 srcs 比对，能在上传之前就拦下错位，省掉一次白传
    if expect_url:
        cur = (st.get("srcs") or [])[pos - 1] if pos <= len(st.get("srcs") or []) else ""
        if cur != expect_url:
            return {"status": "error", "stage": "expect-mismatch",
                    "err": f"第 {pos} 个模块当前不是期望的源图，拒绝替换以免错位",
                    "expect": expect_url, "actual": cur}

    # 描述图按【描述图】的下限过闸，不套服装的 1340×1785：平台对描述图只要求
    # 两边 >= 480、比例 0.5~2（见 images.check_desc_size 与 upload_image 的
    # min_w/min_h 说明）。2026-08-29 实测一张达标的 480×480 被服装闸门拒掉，
    # 页面留着 1688 原始外链，⑬ 整单失败。
    up = await upload_image(session, image_path, full_cid=full_cid,
                            min_w=images.DESC_MIN_W, min_h=images.DESC_MIN_H)
    if up.get("status") != "ok":
        return {"status": "error", "stage": "upload", "upload": up}
    fid = up["fileId"].rsplit("/", 1)[-1]

    idx = str(pos - 1)
    sc = await session.eval_json(
        media_description_replace_scripts._JS_DESC_BOX_SCROLL.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL)
        .replace("__IDX__", idx).replace("__BLOCK__", J("center")))
    if sc.get("err"):
        return {"status": "error", "stage": "scroll", **sc}
    await asyncio.sleep(1.0)

    bp = await session.eval_json(
        media_description_replace_scripts._JS_DESC_BOX_POS.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL).replace("__IDX__", idx))
    if bp.get("err"):
        return {"status": "error", "stage": "locate", **bp}
    # 滚动过程中若页面自己动过（懒加载补图等），这里再比一次才算真的对上
    if expect_url and bp.get("srcBefore") != expect_url:
        return {"status": "error", "stage": "expect-mismatch",
                "err": f"滚动后第 {pos} 个模块的图与期望源图不一致，拒绝替换",
                "expect": expect_url, "actual": bp.get("srcBefore"), "upload": up}
    # 1. 点模块图，让右侧面板出现「更换图片」
    await media_menus._cdp_click_xy(session, bp["x"], bp["y"])
    await asyncio.sleep(1.5)

    # 【瞄点求解交给 _desc_aim_replace_link】它负责三件事：链接被 fixed 顶栏压住时滚
    # 容器把它推出遮挡带、推不动就换模块图落点重瞄、顺带收一次残留浮层。原实现只做了
    # 「收浮层 + 重读坐标」，对顶栏那类遮挡完全无效——2026-08-28 实测 6 张图全挂在这。
    lp = await _desc_aim_replace_link(session, idx)
    if lp.get("err"):
        return {"status": "error", "stage": lp.get("stage") or "replace-link", **lp}
    # 2. 点「更换图片」，展开描述专属菜单。
    # 【轮询而不是固定 sleep，且点不出来要补点一次】原实现点完固定等 1.8s 就去找菜单，
    # 2026-08-26 实测第 3 张图报「菜单未展开、visibleMenus 为空」而前后两张都正常
    # ——那是时序（等短了）与 rc-trigger 的「第一次真实 mousedown 先关旧浮层」两种
    # 成因，都靠重点一次 + 等到为止解决，见 _JS_DESC_MENU_STATE 上方的实测记录。
    menu_js = media_description_replace_scripts._JS_DESC_MENU_STATE.replace("__ITEMS__", J(list(DESC_MENU_ITEMS)))
    # 【点之前先确认没有残留的描述菜单开着】否则轮询会立刻看到 found=true，而那可能是
    # 上一张图留下的旧实例——更糟的是这一次点击恰好把它 toggle 关掉，于是下一步
    # _JS_DESC_PICK_SPACE 又找不到菜单，回到原来那条「菜单未展开」。
    # 注意【不能指望 _park_image_menus 收掉它】：那个函数的判据是菜单里含「空间图片」
    # （素材图/SKC 那套 4~5 项菜单），而描述菜单的对应项叫「空间【上传】」，两套文案
    # 不同（见 DESC_MENU_ITEMS 上方 2026-08-20 的实测记录），它对描述菜单完全不生效。
    pre = await session.eval_json(menu_js)
    if pre.get("found"):
        logger.info("点「更换图片」前已有描述菜单开着（上一张的残留），先点空白处收掉")
        bp2 = await session.eval_json(media_menus._JS_BLANK_POINT)
        if not bp2.get("err"):
            await media_menus._cdp_click_xy(session, bp2["x"], bp2["y"])
            await asyncio.sleep(0.5)
        else:
            logger.warning(f"没找到安全空白点，无法收起残留描述菜单：{bp2['err']}")
    ms = {}
    # 【瞄点没命中时不要再拿它去点】原实现照点不误，于是两轮点击都打在顶栏上、
    # 轮询必然空转 6.4s 才报「菜单未展开」——报错还把成因说成时序。改成：坐标可用就
    # 走坐标（rc-trigger 只认真实 mousedown），不可用就直接跳到派发事件那条兜底路。
    if lp.get("onLink"):
        for click_round in range(2):
            await media_menus._cdp_click_xy(session, lp["x"], lp["y"])
            for _ in range(8):                   # 最多轮询 8×0.4s = 3.2s
                await asyncio.sleep(0.4)
                ms = await session.eval_json(menu_js)
                if ms.get("found"):
                    break
            if ms.get("found"):
                if click_round:
                    logger.info("描述专属菜单在补点一次后展开（首次点击被 rc-trigger 吃掉）")
                break
            logger.warning(f"描述专属菜单未展开（第 {click_round + 1} 次点击），"
                           f"当前可见菜单：{ms.get('visibleMenus')}")
    else:
        logger.warning(f"「更换图片」瞄点始终被遮挡（落在 {lp.get('hitTag')} "
                       f"{lp.get('hitAt')}），跳过坐标点击直接派发事件")
    if not ms.get("found"):
        # 【最后一招：不用坐标，直接给链接派发鼠标事件】遮挡带盖住链接时坐标这条路
        # 物理上走不通（点到的是顶栏），但事件可以绕过命中测试直达元素。
        # 之所以不把它当首选：rc-trigger 对合成事件的响应不如真实 mousedown 稳，
        # 素材图/SKC 那两处都是吃过亏才改用 CDP 的。这里只在坐标无路时用。
        disp = await session.eval_json(
            media_description_replace_scripts._JS_DESC_DISPATCH_LINK.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL))
        if not disp.get("err"):
            for _ in range(8):
                await asyncio.sleep(0.4)
                ms = await session.eval_json(menu_js)
                if ms.get("found"):
                    logger.info("描述专属菜单靠派发鼠标事件展开（坐标被遮挡时的兜底）")
                    break
        else:
            logger.warning(f"派发鼠标事件失败：{disp['err']}")
    if not ms.get("found"):
        return {"status": "error", "stage": "menu",
                "err": "描述专属菜单未展开（已换落点重瞄、重点一次并派发事件兜底）",
                "visibleMenus": ms.get("visibleMenus"),
                "linkAim": {k: lp.get(k) for k in
                            ("x", "y", "onLink", "hitTag", "hitAt",
                             "linkTop", "blockerBottom", "bodyScrollTop", "fixTried")},
                "upload": up}

    # 3+4. 点「空间上传」并在弹窗里选图确定（同一 evaluate，菜单会自动收起）
    picked = await session.eval_json(
        media_description_replace_scripts._JS_DESC_PICK_SPACE
        .replace("__ITEMS__", J(list(DESC_MENU_ITEMS)))
        .replace("__FID__", J(fid)))
    if picked.get("err"):
        return {"status": "error", "stage": picked.get("stage") or "pick",
                "detail": picked, "upload": up}

    after = await session.eval_json(media_description_scripts._JS_DESC_STATE.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL))
    srcs = after.get("srcs") or []
    landed = pos <= len(srcs) and fid in srcs[pos - 1]
    if not landed:
        logger.error(f"描述模块 {pos} 替换后回读不含新 fileId；"
                     f"该位置现为 {srcs[pos-1][-40:] if pos <= len(srcs) else '不存在'}")
    return {"status": "ok" if landed else "error",
            "stage": "" if landed else "readback",
            "pos": pos, "upload": up,
            "srcBefore": bp.get("srcBefore"),
            "srcAfter": srcs[pos - 1] if pos <= len(srcs) else None,
            "countAfter": after.get("count"),
            "note": "" if landed else "位置对不上，可能替换到了别的模块",
            "hint": "改动尚未生效，需再调 desc_save 保存" if landed else ""}
