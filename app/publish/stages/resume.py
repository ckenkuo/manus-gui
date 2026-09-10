"""店小秘发布共用能力：stages.resume。各来源流程由 workflows/ 独立定义。"""

from app.logger import logger
from app.publish.variant_colors import accessory_colors_from_rows


# 【只改未保存表单、成果靠 ⑭ save 一次性提交的阶段】save 没成功过，它们就等于没跑：
# 页签一关 / Chrome 一退 / open_edit 重新导航，成果全丢（2026-08-23 实测取证，见
# pipeline.live_state 上方注释）。故续跑时这些阶段【不认状态文件】，改按页面实况判定。
#
# 不在此列的：① extract（产物是本地 product-info.json 与图片，磁盘上）、
# ② claim（服务端建了草稿，rowid 已在状态文件里）。
#
# 【③ auto_cat 与 ④ attrs 也在此列，2026-08-24 起】原先把它们排除在外，依据是
# 「类目与属性由服务端持久化，重开编辑页仍在」。890843533224 这单证伪了：save 从未
# 成功时类目一样丢，回落到认领带来的旧类目、且那旧类目已被平台下线（页面弹「该分类
# 已在平台删除！」）。类目没生效 → 尺码行不渲染 → ⑧⑨⑩⑪ 无处可填 → save 死循环。
# 详见 pipeline._JS_LIVE_STATE 上方的注释与 _stale_form_stages 里的类目判据。
# 类目与它的从属阶段：类目一换属性区整体重建，故这两个永远一起进出重跑集。
_CAT_STAGES = ["auto_cat", "attrs"]


# ⑤ 起的纯表单阶段（类目有效时只有这些需要按实况逐项细判）
_FORM_STAGES_AFTER_CAT = [
    "titles", "clean_images", "material", "drop_acc", "skc", "sku_preview", "fix_sizes",
    "sizechart", "sku_code", "variant", "stock", "shipping", "desc", "video",
]


_FORM_ONLY_STAGES = _CAT_STAGES + _FORM_STAGES_AFTER_CAT


# ---- 各阶段编排（入参统一 ctx/session/emit，返回 {"status", "note"}）----------
# status ∈ ok / skipped / fail；manual_check 事件由阶段内按需发。

def _stale_form_stages(live: dict) -> list:
    """按编辑页实况算出「成果已丢、需要重跑」的表单阶段。

    判据取粗而确定的信号（详见 pipeline._JS_LIVE_STATE 注释）：重跑一个其实还在的
    表单阶段只是多花时间，各阶段本身幂等；漏跑一个真丢了的会让整单卡死在 save。

    【⑧ 尺码勾选丢了要连带 ⑨⑩⑪】变种信息表 0 行时，尺码表/变种/库存全都无处可填，
    只补 ⑨ 是没用的（2026-08-23 实测：skuRowCount=0 时变种属性区只剩「请选择引用模板」）。

    【⑩a SKU 货号不能跟着 skuFilledRows 判】货号列自己就是 input，平台「一键生成」
    写进去的中文货号会让 skuFilledRows 非 0，于是「填过了」和「填的是非法值」在这个
    信号里长得一样。故 live_state 单出 skuCodeBad 计数，这里独立判。

    【③ 类目丢了要连带 ④ 属性，且此时不必再算下游】属性行是类目决定的（女童针织套头衫
    33 行 / 女童长裤套装 42 行），类目一换属性区整体重建，只补 ④ 是白填。而 ⑤ 起的所有
    表单阶段本来就在重跑集里，故类目异常时直接返回全量、不再逐项细判——细判的输入
    （尺码行、图、尺码表）在类目未生效的页面上全是 0，结论必然也是「全跑」。
    """
    if not live.get("rendered"):
        return list(_FORM_ONLY_STAGES)      # 读不到实况，保守全跑（含 ③④）
    # 类目失效（回落到认领旧类目 / 旧类目已被平台下线）是最上游的坏账，必须从 ③ 重来。
    # 【只有这一个分支会把 ③④ 放进重跑集】类目有效时重跑 ③ 是纯浪费：走一遍类目树
    # 要 110s（缓存命中也要 7.6s），而 ④ 属性确实由服务端存住了（重开编辑页 42 条
    # 属性行都在，2026-08-21 实测），不像 ⑤ 起的表单那样一重载就丢。
    if live.get("catUnset") or live.get("catDeleted"):
        return list(_FORM_ONLY_STAGES)
    stale = []
    if not live.get("titleFilled") or live.get("titleHasCjk"):
        stale.append("titles")
    # ⑥⑦ 图片类：变种属性区一张图都没有说明素材图与 SKC 换图都丢了。
    # ⑤b clean_images 是它们的上游产物提供者，跟着一起重跑（它自己会判「已有干净图」跳过）。
    if not live.get("attrImgCount"):
        stale += ["clean_images", "material", "skc"]
    elif live.get("attrImgBad"):
        # 图在、但有破线的（某颜色行没被 ⑦ 换过，留着 1688 原始小图）：只重跑 ⑦。
        # 不连带 ⑤b/⑥——那条是素材图，与某个颜色行漏换无关，重跑要白烧生图。
        stale.append("skc")
    if not live.get("skuRowCount"):
        stale += ["fix_sizes", "sizechart", "sku_code", "variant", "stock"]
    else:
        # 【无尺码维的类目不判尺码表】非服装类目（仿真花/玩具/饰品）压根没有尺码表栏，
        # sizechartAdded 恒 false，照判会让⑨ 每次续跑都进重跑集、每次又 skipped，
        # 白跑一轮还把日志搅浑。hasSizeGroup 与 sizechartCount 都没有才算「本类目不要」，
        # 两个信号一起看是为了不把「渲染慢」误判成「不需要」（见 live_state 的取证）。
        no_size_dim = not live.get("hasSizeGroup") and not live.get("sizechartCount")
        if no_size_dim:
            logger.info("本类目无尺码维（无尺码组且无尺码表栏），续跑判定不安排⑨ 尺码表")
        elif not live.get("sizechartAdded"):
            stale.append("sizechart")
        # 第二张表：套装商品必填，没填平台会打回「套装尺码模板数量不合法」。
        # sizechart2Added 为 None 表示该类目没有这一栏（不判 stale）；为 False 才是
        # 「有栏但空着」。是否真的必填由 _st_sizechart 按 SKU分类判，这里只负责把
        # 「第一张有了、第二张空着」这种半成品状态也算进重跑范围——只看第一张会让
        # 套装商品续跑时跳过 ⑨，接着又被平台打回同一个错。
        elif live.get("sizechart2Added") is False:
            stale.append("sizechart")
        # ⑩a 单独判：货号列有值不代表合法，平台生成的中文值恰恰是要改的那个
        # （skuCodeBad 已把「空」与「含非 ASCII」都算进去，见 _JS_LIVE_STATE）
        if live.get("skuCodeBad") or not live.get("skuCodeCount"):
            stale.append("sku_code")
        if not live.get("skuFilledRows"):
            stale += ["variant", "stock"]
    # ⑦b SKU 预览图：【与 skuRowCount 那个分支平级，不放进 else】——它读的是变种
    # 【信息】表第一列，与 ⑥⑦ 看的 attrImg*（变种【属性】区）是两处不同的图，
    # 两处正交（2026-08-30 玩具类那单就是 ⑦ 合理跳过、⑦b 从未跑过而被平台拒）。
    # 也不该挂在 skuRowCount 非空的前提下：变种表 0 行时预览图列同样不存在，
    # 那种情况由 previewBad 自己为 0 兜住，不必再套一层分支。
    # previewCount 为 0 时不判 stale：那说明该类目没有这一列，交阶段自己 skipped。
    if live.get("previewBad"):
        stale.append("sku_preview")
    # ⑦a 剔配件色：变种表里只要还有「单行有源数据」的颜色就得重跑。反选只改未保存
    # 表单，save 没成功过时页面会回到认领时的全勾状态（与 ⑤~⑬ 其它表单阶段同理）。
    # 判据完全取页面实况（源颜色名与页面色板名对不上，见 _JS_VARIANT_ROW_FILL），
    # variantByColor 读不到时不判 stale：交阶段自己 skipped。
    if live.get("variantByColor"):
        if accessory_colors_from_rows(live["variantByColor"]):
            stale.append("drop_acc")
    if not live.get("shippingSet"):
        stale.append("shipping")
    # ⑬ 描述：图全是 1688 外链（alicdn）说明删图/英化成果没了。
    # 描述区一张图都没有时不判 stale——那可能是本商品本就无描述图，交阶段自己判。
    if live.get("descImgCount") and live.get("descForeignCount") == live.get("descImgCount"):
        stale.append("desc")
    # ⑬b 视频：【无条件进重跑集】。save 没成功时 videoUrl 会退回认领带来的 1688 原始
    # 竖屏地址，而这个字段不在 DOM 里（只在 edit.json 响应里），live_state 那段 JS
    # 读不到它、没法像别的阶段那样按实况细判。让它自己去判是安全且便宜的：阶段开头
    # 就读接口，没视频或已合规都直接 skipped（只花一次接口 + 一次下载探测的几秒），
    # 与 clean_images「自己判已有干净图就跳过」同一取向。
    # 漏跑的代价反过来大得多——带着竖屏视频去发布，走完 15 个阶段才被平台打回。
    stale.append("video")
    return [s for s in _FORM_ONLY_STAGES if s in set(stale)]
