"""店小秘发布共用能力：stages.variants。各来源流程由 workflows/ 独立定义。"""

from app.publish.browser import BrowserSession
from app.publish.sizechart.editor import add_sizechart
from app.publish.sizechart.parts import _size_category_for
from app.publish.sizes import fix_sizes
from app.publish.sku_codes import fix_sku_codes
from app.publish.stages import prewarm_access as stages_prewarm_access
from app.publish.stock import set_stock
from app.publish.variants import set_variant


async def _st_fix_sizes(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑧ 尺码勾选。非服装类目没有尺码维，此时 fix_sizes 返回 skipped 而不是失败。

    【为什么 skipped 要原样透传】2026-08-28 真站取证（1014675972015 手工编织水果花束
    摆件，类目「仿真花」）：该类目的变种属性区只有「颜色」一维，6 个复选框全是颜色、
    且已与源 SKU 对上，变种表也已生成——⑧ 对它本就无事可做。把这种情形算失败会让
    整单永远卡在⑧（先是报「无 skus 数据」，补了均码后改报「源尺码在页面选项里全部
    不存在」，换个说法而已）。详见 pipeline._JS_SIZE_GROUP_PRESENCE 上方的取证记录。
    """
    r = await fix_sizes(session, ctx["info_path"])
    if r.get("status") == "skipped":
        # 记一条 log 事件：这一跳是正常的，但要让人在进度里看见「为什么⑧没做事」
        await emit({"type": "log", "stage": "fix_sizes",
                    "message": r.get("reason") or "本类目无尺码维，跳过尺码勾选"})
        return {"status": "skipped", "note": (r.get("reason") or "")[:200]}
    if r.get("status") != "ok":
        return {"status": "fail", "note": (r.get("reason") or "")[:200]}
    wanted = r.get("wantedSizes") or []
    note = f"源尺码 {len(wanted)} 个 | SKU 表 {r.get('rowCount')} 行"
    # 字母码童装映射：源 S/M/L/XL → 页面 100/110/120...，要让人在 note 里看见映射关系，
    # 否则 note 只显示「源尺码 4 个」却看不到勾的其实是身高码，排查时对不上源字母码。
    size_mapping = r.get("sizeMapping") or {}
    if size_mapping:
        map_txt = "、".join(f"{k}→{v}" for k, v in size_mapping.items())
        note += f" | 字母码已映射 {map_txt}"
    # 剔除的伪变种（颜色维「款式随机」/尺码维「尺寸参考图」之类占位项）要让人在 note 里
    # 看见拿掉了什么；它不是源规格、不算 missing，剔除是预期行为，只报出来不 manual_check。
    dropped_fake = r.get("droppedFake") or {}
    fake_txt = "、".join((dropped_fake.get("colors") or []) + (dropped_fake.get("sizes") or []))
    if fake_txt:
        note += f" | 已剔除伪选项 {fake_txt}"
    # 【部分源尺码在页面没有对应框：要报出来，不能只进返回值】fix_sizes 的
    # warning 原先被这里整个丢掉，note 只写「源尺码 5 个 | SKU 表 4 行」——
    # 少的那个尺码是谁、少没少，全靠人去对这两个数字。
    # 2026-08-29 实测（草稿 173539495458370139）：类目从「女婴裤套装」被改成
    # 「女童长裤套装」后，页面最小月龄档从 6-9M 抬到 9-12M，源尺码 6-9m 无框可勾，
    # SKU 表少一行；⑧ 照常 ok 一路走到 ⑭，缺的尺码只能人工核对时才发现。
    # 缺失往往意味着【类目选得不对】（同一件商品换个类目尺码档位就全了），故
    # 走 manual_check 让它在进度里显式停一下，而不是继续只当 note 附注。
    missing = r.get("missing") or []
    if missing:
        note += f"（源尺码 {'、'.join(missing)} 在页面无对应选项，未勾上）"
        await emit({"type": "manual_check", "stage": "fix_sizes",
                    "message": f"源尺码 {'、'.join(missing)} 在本类目的尺码选项里不存在，"
                               f"这 {len(missing)} 个尺码不会进 SKU 表（SKU 表 "
                               f"{r.get('rowCount')} 行）。常见成因是类目选得比商品实际年龄段"
                               f"偏大/偏小，请核对类目是否正确"})
    return {"status": "ok", "note": note}


async def _st_sizechart(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑨ 尺码表：套装商品要填两张（「尺码表」+「尺码表2」），且两张分类要分工。

    【为什么按 SKU分类判、且要在这里就判】2026-08-27 商品 1051793179451（两件套裙套装）
    发布被平台打回「套装尺码模板数量不合法 / 您发布的产品是套装，尺码表2也需要设置」。
    这个校验只在服务端做——同日实测把 SKU分类下拉在三档间切换，尺码表2 的 label 始终
    没有 required 类、控件文案也不变，前端一点提示都没有（与包装清单件数和同性质），
    所以页面上读不出「要不要填第二张」，只能靠我们自己的套装判断。

    判据取【预热好的 SKU分类】：本阶段（⑨）在 ⑪ set_stock 之前，页面上的 SKU分类下拉
    此刻还是认领时的默认值、读它没意义；而预热在 ① 之后就把这个判断算完了（与 ②③
    并行，见 _run_prewarm），值与 ⑪ 现场用的完全一致。预热失败取不到时按单件走——
    多填一张空表会让非套装商品也被平台按套装校验，比漏填更难查。

    【两张表的分类按包装清单的件别分派】同日实测：弹窗的尺码分类预选值两张都是
    「女童装-半身裙」，跟随预选会让两张表的测量参数都是裙长/腰围全围——数量校验能过，
    但上衣那件量的是衣长/胸围，等于给买家一份错尺码表。包装清单已经把件别判出来了
    （便服上衣 + 半身裙），拿它去选分类正好，见 pipeline._size_category_for。

    【分类分了还不够，数值也要分】2026-08-29 商品 1058585588864（T恤+牛仔背带裙）：
    上面那轮只让两张表的【分类】分了工，取值仍共用同一份 sizeMeasurements，于是两张表
    填出完全相同的测量值。源图其实按「部件：上衣/连衣裙」分开给了两张表，现由
    pipeline._pick_part_measurements 按分类配对取数，这里只负责把用了哪个部件报出来、
    并在两张仍然同值时提醒人工核对。

    【同款多件两张相同是对的，不能一并告警】skuCat=2 是「多件相同商品」，两件同款、
    尺码维度本就一样，同值是正确结果。只有混合套装（skuCat=3）同值才是可疑的。

    【三件以上平台装不下】平台只有「尺码表」「尺码表2」两栏（label 正则
    /^尺码表2?$/，见 pipeline._JS_SIZECHART_LOCATE），第 3 件起没有位置。丢弃是平台
    结构决定的事实，但不能静默——否则人工复核时看不出「有 3 件、只进了 2 件」。
    """
    judge = ctx.get("sku_judge") or await stages_prewarm_access._await_prewarm(ctx, "stock") or {}
    if judge:
        ctx["sku_judge"] = judge
    # skuCat 2=同款多件 3=混合套装 都算套装（平台按「不止一件」判，不区分同款与否）
    is_set_by_judge = str(judge.get("skuCat", "1")) in ("2", "3")
    # 包装清单的件别（已归一到平台词表，见 _normalize_sku_judge）；顺序即两张表的顺序
    packing = [x.get("name", "") for x in (judge.get("packing") or [])]
    cats = [_size_category_for(n) for n in packing]

    r = await add_sizechart(session, ctx["info_path"],
                            category=cats[0] if cats else None,
                            cat_path=ctx.get("cat_path"))
    if r.get("status") != "ok":
        # 【第一张表栏都没有 → 本类目不要尺码表，skipped 而不是失败】原先只对
        # 「尺码表2」缺栏宽容（下方那个分支），第一张缺栏仍算 fail。2026-08-28 真站
        # 取证（1014675972015 仿真花）：非服装类目整页 32 个 label 里没有任何带「尺」
        # 的项，连第一张表栏都不存在，于是⑨ 紧接⑧ 再挂一次。缺栏是平台按类目决定的
        # 事实，不是我们填失败——与⑧ 的无尺码维同一性质。
        if r.get("reason") == "no-sizechart-item":
            await emit({"type": "log", "stage": "sizechart",
                        "message": "本类目没有尺码表栏（非服装类目），跳过尺码表"})
            return {"status": "skipped", "note": "本类目无尺码表栏"}
        return {"status": "fail", "note": (r.get("reason") or "")[:200]}
    if r.get("skipped"):
        note = f"已存在：{r.get('current')}"
    else:
        # 模型估算过的参数列单独点出来：这几列不是源实测值，人工复核时要优先看
        est = r.get("estimated") or []
        note = (f"模板 {r.get('tplName')} | 分类 {r.get('category')}"
                f" | 参数 {len(r.get('params') or [])} 项")
        # 套装分件取数时点出用了源图哪一件：两张表数值必须分开，这是人工复核的第一眼
        if r.get("partUsed"):
            note += f" | 源部件 {r.get('partUsed')}"
        if est:
            note += f" | 模型估算 {'、'.join(est)}"
        # 【源数据一列都没用上】源有实测尺寸、平台参数却全走估算：尺码分类与商品维度
        # 对不上（背带裤归上装→参数变领围），源真实尺码全被丢弃。这是要人工介入的异常，
        # 不是普通「缺几列估算」，单独发一条 manual_check 而不是只混在 note 里。
        if r.get("sourceUnused"):
            note += " | 源数据未采用(待复核)"
            await emit({"type": "manual_check", "stage": "sizechart",
                        "message": "源商品有实测尺码数据，但平台尺码表参数一列都没对齐上"
                                   "（源表头与可选参数语义对不上，或源数据与商品严重不符），"
                                   "已全部改交模型估算，源真实尺码未采用，请人工核对尺码表"})

    # 【多重防线判定是否为套装】
    # 1. 大模型/预热判定：skuCat 为 2(同款多件) 或 3(混合套装)
    # 2. 页面 DOM 事实：编辑页实际上渲染了 2 张尺码表栏位（charts >= 2）。页面有第二张栏位
    #    却不填，平台服务端必定打回「套装尺码模板数量不合法 / 尺码表2也需要设置」
    # 3. 平台已选类目：阶段③已选定类目，若类目路径包含「两件套/三件套/四件套/多件套/套装」
    # 4. 标题特征：包含「两件套/三件套/四件套/多件套/套装」
    cat_str = " > ".join(str(c) for c in (ctx.get("cat_path") or []))
    is_set_by_cat = any(k in cat_str for k in ("两件套", "三件套", "四件套", "多件套", "套装"))
    is_set_by_dom = (r.get("charts", 1) >= 2)
    title_str = ctx.get("title") or ""
    is_set_by_title = any(k in title_str for k in ("两件套", "三件套", "四件套", "多件套", "套装"))

    is_set = is_set_by_judge or is_set_by_dom or is_set_by_cat or is_set_by_title

    if not is_set:
        return {"status": "ok", "note": note}

    # 【三件以上先报出来】平台只有两栏，第 3 件起填不进去。放在补第二张表【之前】发，
    # 是为了让这条提示排在尺码表结果前面——人先看到「有几件装不下」，再看两张表填了什么。
    if len(packing) > 2:
        await emit({"type": "manual_check", "stage": "sizechart",
                    "message": f"包装清单有 {len(packing)} 件（{'、'.join(packing)}），"
                               f"但平台只有「尺码表」「尺码表2」两栏，第 3 件起的尺码表"
                               f"填不进去。请人工确认这几件是否共用同一套尺码，"
                               f"或改小 SKU分类件数"})

    # 套装：补第二张表（分类取清单第二件；同款多件时两件相同，跟随第一件）
    # 当模型漏判导致清单只有 1 件、但平台/类目已明确是套装时：两张表绝不能选相同分类，
    # 按常见两件套互补推导第二张表的分类（如第一件是连体衣/下装，第二件选上装）。
    if len(cats) > 1:
        cat2 = cats[1]
    elif len(cats) == 1:
        first_cat = cats[0]
        if first_cat in ("下装", "半身裙", "连体衣"):
            cat2 = "上装"
        elif first_cat in ("上装", "马甲"):
            cat2 = "下装"
        else:
            cat2 = None
    else:
        cat2 = None
    r2 = await add_sizechart(session, ctx["info_path"], which=1, category=cat2,
                             cat_path=ctx.get("cat_path"))
    if r2.get("status") != "ok":
        if r2.get("reason") == "no-sizechart-item":
            # 该类目只有一张表栏：平台不会按套装校验第二张，不算失败
            await emit({"type": "log", "stage": "sizechart", "level": "warning",
                        "message": "判为套装但页面没有「尺码表2」栏，跳过第二张"})
            return {"status": "ok", "note": note + " | 无尺码表2栏"}
        await emit({"type": "manual_check", "stage": "sizechart",
                    "message": f"尺码表2 未加成（套装商品平台会打回）：{r2.get('reason')}"})
        return {"status": "fail", "note": f"尺码表2 失败：{(r2.get('reason') or '')}"[:200]}
    if r2.get("skipped"):
        note += f" | 尺码表2 已存在：{r2.get('current')}"
    else:
        note += f" | 尺码表2 模板 {r2.get('tplName')}"
        if r2.get("partUsed"):
            note += f"（源部件 {r2.get('partUsed')}）"
        # 【第二张表的估算列也要记】原先只有第一张表记 estimated，于是套装商品的状态
        # 文件通篇没有「模型估算」字样，看起来像两张表都用了源实测值。2026-09-01
        # 取证（999389808041 连体裤）第二张表两列全是估算值、且量级失真近一倍，
        # 而这条信息在状态文件与 UI 上完全不可见——人工复核该先看哪几列都无从下手。
        if r2.get("estimated"):
            note += f" | 尺码表2 模型估算 {'、'.join(r2['estimated'])}"
        # 混合套装两张表填出同一组数值 = 买家拿到一份错尺码表（真站坑，见
        # pipeline._pick_part_measurements）。源只给了一张合表时无从分开，故只告警、
        # 不判失败。skuCat=2（同款多件）两件本就同款，同值是正确结果，不在此列。
        if (str(judge.get("skuCat", "1")) == "3"
                and r.get("data") and r2.get("data")
                and r.get("data") == r2.get("data")):
            await emit({"type": "manual_check", "stage": "sizechart",
                        "message": "混合套装的两张尺码表填出了完全相同的测量值，"
                                   "请核对源商品是否分件给了尺码表（上衣量衣长胸围、"
                                   "裙裤量裙长腰围），必要时人工改第二张"})
            note += " | 两张数值相同(待复核)"
    return {"status": "ok", "note": note}


async def _st_sku_code(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑩a：把 SKU 货号重写成纯 ASCII（平台不收中文/中文符号，见 pipeline 侧注释）。

    排在 ⑩ variant 之前只为读的行序稳定：⑧ 已把尺码勾好、行也生成完了，这里读一次
    颜色/尺码就填，中间不点任何东西。放到 ⑩ 之后也能跑，纯粹是没必要多等一次渲染。
    不读 product-info.json：源 SKU 名与页面行对不上（真站取证见 pipeline.fix_sku_codes），
    页面自己的颜色/尺码两列才是唯一可信的行标识。
    """
    r = await fix_sku_codes(session)
    if r.get("status") == "error":
        return {"status": "fail", "note": (r.get("reason") or "")[:200]}
    if r.get("status") == "validation-error":
        await emit({"type": "manual_check", "stage": "sku_code",
                    "message": f"部分行货号未通过回读校验：{str(r.get('bad') or r.get('mismatch'))[:150]}"})
    tr = r.get("translated") or {}
    note = f"{r.get('rowCount')} 行 | 首个 {(r.get('codes') or [''])[0]}"
    if tr:
        note += " | 译 " + "、".join(f"{k}→{v}" for k, v in list(tr.items())[:4])
    return {"status": "ok", "note": note}


async def _st_variant(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑩ 变种信息：申报价与包裹尺寸都在这里落地。

    price 走批次级参数（UI 输入框 / CLI --price），空串直接透传——归一与退默认
    统一由 pipeline.normalize_declare_price 负责，这里不再兜一层默认值（两处都写
    默认值，改口径时必漏一处）。
    cat_path 传下去只为判服装类（服装包裹尺寸固定 30x25x3，不问模型），
    续跑时 cat_path 从状态文件回填、拿不到就退到标题判定。
    """
    r = await set_variant(session, ctx["info_path"],
                          price=ctx.get("price") or "",
                          cat_path=ctx.get("cat_path"),
                          pack_est=await stages_prewarm_access._await_prewarm(ctx, "variant"))
    if r.get("status") == "error":
        return {"status": "fail", "note": (r.get("reason") or "")[:200]}
    note = (f"{r.get('rowCount')} 行 | 申报价 {r.get('price')} | "
            f"尺寸 {'x'.join(r.get('dims') or [])}cm | 重量 {r.get('weight')}g")
    if r.get("status") == "validation-error":
        await emit({"type": "manual_check", "stage": "variant",
                    "message": f"变种信息部分行未通过回读校验：{str(r)[:120]}"})
    return {"status": "ok", "note": note}


async def _st_stock(ctx: dict, session: BrowserSession, emit) -> dict:
    sku_judge = ctx.get("sku_judge") or await stages_prewarm_access._await_prewarm(ctx, "stock")
    r = await set_stock(session, ctx["info_path"],
                        site=ctx.get("site") or "",
                        # 发布页仓库下拉选的真实选项优先；空串退回 config 站点映射
                        warehouse=ctx.get("warehouse") or "",
                        sku_judge=sku_judge,
                        cat_path=ctx.get("cat_path"))
    if r.get("status") == "error":
        note = f"[{r.get('stage')}] {(r.get('reason') or r.get('err') or '')}"
        if r.get("available"):
            note += f" | 可用: {r.get('available')}"
        return {"status": "fail", "note": note[:200]}
    if r.get("status") == "validation-error":
        await emit({"type": "manual_check", "stage": "stock",
                    "message": f"库存部分行未通过回读校验：{str(r)[:120]}"})
        return {"status": "fail", "note": "库存/SKU分类/包装清单未通过回读校验，请重跑⑪"}
    if r.get("warehouse"):
        ctx["warehouse"] = r["warehouse"]
    sc = r.get("skuCategory") or {}
    pk = "、".join(f"{x.get('name')}x{x.get('qty')}" for x in (r.get("packing") or [])) or "无"
    return {"status": "ok",
            "note": f"仓库 {r.get('warehouse')} | 处理 {r.get('processed')} 行"
                    f" | SKU分类 {sc.get('cat')} x{sc.get('qty')} | 包装清单 {pk}"}
