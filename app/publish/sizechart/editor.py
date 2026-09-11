"""店小秘发布操作：sizechart.editor。模块导航见 docs/publish-pipeline-refactor.md。"""

import json
import math
from app.logger import logger
from app.publish import titles, workflows
from app.publish.browser import BrowserSession, J
from app.publish.sizechart import (
    measurements as sizechart_measurements,
    parameters as sizechart_parameters,
    parts as sizechart_parts,
    scripts as sizechart_scripts,
)
from typing import Optional


def _sc_js(template: str, which: int) -> str:
    """把尺码表 JS 模板里的 __LOCATE__（按 label 定位的工具函数）与 __IDX__ 填好。

    两个占位符分开填而不是写死：定位工具要在多段 JS 间复用（状态回读、开弹窗），
    而 which 每次调用都可能不同（0=尺码表，1=尺码表2）。
    """
    return template.replace("__LOCATE__", sizechart_scripts._JS_SIZECHART_LOCATE).replace("__IDX__", J(which))


async def add_sizechart(session: BrowserSession, info_path: str,
                        category: Optional[str] = None,
                        name: Optional[str] = None,
                        which: int = 0,
                        cat_path=None) -> dict:
    """阶段⑨：添加尺码表（尺码分类 + 测量参数填表）。

    which：填第几张表（0=「尺码表」，1=「尺码表2」）。套装商品平台要求两张都填，
    见下方【套装要两张尺码表】。

    流程：
    1. 点「添加尺码表」入口（按 label 文字定位第 which 张表里的 span.link）
    2. 确认尺码分类：默认跟随平台按已选类目预选的值，category 给了才按关键词改
    3. 读取弹窗参数列表（衣长/胸围全围/袖长...）和尺码行（80/90/100...）
    4. 测量值来源（按参数逐列组合，不是二选一）：
       - 源 product-info.json 的 sizeMeasurements（实测平铺尺寸）给了哪列就用哪列
       - 弹窗要而源没有的列，交当前选择的模型估算（跟随发布页 UI 下拉，见 llm.get_llm）
    5. 参数键模糊对齐（胸围 ↔ 胸围全围），对齐后仍缺值报错不填半残表
    6. 填模板名 + 表格，点确定，回读验证

    实测要点（2026-08-18）：
    - 多弹窗陷阱：重复执行会叠多个「添加尺码表」弹窗，开新前先全关掉
    - 测量参数：分类强制的，取消勾选无效，必须填值
    - 基码表（身高/体重）：系统自动填充，不支持手动录入

    【尺码分类不要写死关键词】2026-08-22 实测 947662049255（女童网纱连衣裙）：
    该下拉的选项由页面已选类目决定（这里只有「女童装-连衣裙」一项）且平台已预选好，
    原先默认拿 "上装" 去匹配，报 option-not-found 使整个商品未落库。类目在阶段③
    已经选定，平台据此给的分类比这里猜的准，故默认不指定、只做确认。

    【套装要两张尺码表】2026-08-27 商品 1051793179451（两件套裙套装）发布被平台打回：
    「接口报错:套装尺码模板数量不合法 / 您发布的产品是套装，尺码表2也需要设置」。
    套装的两件各有自己的尺码维度（上衣量衣长胸围、半身裙量裙长腰围），平台因此要求
    两张模板。这个校验只在【服务端】做：同日实测把 SKU分类在三档间切换，尺码表2 的
    label 始终没有 required 类、控件文案也不变，前端一点提示都没有（与包装清单件数和
    同性质），故要不要填第二张只能按 SKU分类自己判，见 service._st_sizechart。
    """
    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)
    normalize_size = workflows.size_normalizer(info)
    title = info.get("title", "")
    size_ref = info.get("sizeChart") or {}
    # 模板名去年份：源标题惯用「2026新款」，前 10 字硬截取会把年份带进模板名
    clean_title = titles._strip_dated(title)
    tpl_name = name or ((clean_title[:10] + "尺码表") if clean_title else "通用尺码表")
    # 两张表的模板名必须不同：同名模板平台会当成同一个，第二张覆盖第一张而不是新增
    if which and not name:
        tpl_name = f"{tpl_name}2"

    # 已添加则直接返回（第 which 张表自己的状态，别读成另一张的）
    st = await session.eval_json(_sc_js(sizechart_scripts._JS_SIZECHART_STATE, which))
    if st.get("found") and "添加尺码表" not in st.get("text", ""):
        return {"status": "ok", "skipped": True, "reason": "尺码表已存在",
                "which": which, "label": st.get("label"),
                "current": st.get("text")}
    if not st.get("found"):
        # 尺码表2 不存在（非套装类目只有一张表）：报出来交调用方判是否可跳过
        return {"status": "error", "reason": "no-sizechart-item",
                "which": which, "charts": st.get("charts", 0)}

    # 多弹窗陷阱：开新前先关闭残留弹窗
    await session.eval_json(r"""(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const list = () => Array.from(document.querySelectorAll('.ant-modal-wrap'))
        .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
      let n = 0;
      for (const w of list()) {
        const cancel = Array.from(w.querySelectorAll('button')).find(b => /取消|关闭/.test((b.textContent||'').trim()))
          || w.querySelector('.ant-modal-close');
        if (cancel) { cancel.click(); await sleep(1000); n++; }
      }
      return JSON.stringify({closed: n});
    })()""")

    # 打开弹窗
    opened = await session.eval_json(_sc_js(sizechart_scripts._JS_OPEN_SIZECHART_MODAL, which))
    if not opened.get("opened"):
        return {"status": "error", "which": which,
                "reason": f"添加尺码表弹窗未打开: {opened}"}

    # 确认尺码分类（默认跟随平台预选，category 给了才按关键词改）
    sel = await session.eval_json(sizechart_scripts._JS_SET_SIZECHART_CAT.replace("__CAT__", J(category)))
    if not sel.get("ok"):
        return {"status": "error", "reason": f"尺码分类选择失败: {sel}"}

    # 【套装按部件取数】源图分开给了「部件：上衣」「部件：连衣裙」两张表时（见
    # extract._VISION_PROMPT 的 sizeMeasurementsByPart），两张平台尺码表各取自己那件；
    # 只有扁平表时照旧共用（那本就是一张合表）。真站取证见 _pick_part_measurements。
    # 配对放在勾参数复选框【之前】：勾哪些可选参数也按本张表那件的参数名判——原先
    # 固定拿 parts[0]，第一件是裤子时连马甲表都按裤子的参数名去勾（2026-09-08
    # 取证 1050772789299，马甲+长裤两件套）。
    parts = info.get("sizeMeasurementsByPart") or []
    part_used = ""
    if parts:
        # 配对键取【平台实际选中的分类】：category 可能是 None（跟随预选），
        # 而 sel 里的选中值是刚回读的事实。
        # source='only-option' 时 category 非空却【没被采纳】（关键词落空、下拉只有
        # 唯一选项，见 _JS_SET_SIZECHART_CAT 里那段）——此时拿 category 去配对会用一个
        # 平台并未选中的词，故一律以回读值为准。
        key = (None if sel.get("source") == "only-option" else category) \
            or sizechart_parts._category_keyword_of(sel.get("selected") or "")
        picked, part_used = await sizechart_parts._pick_part_measurements(
            parts, key, which, selected=sel.get("selected") or "")
        if picked:
            src_meas = picked
            logger.info(f"尺码表{which + 1} 取源分件实测表「{part_used}」"
                        f"（分类 {sel.get('selected')}）")
        else:
            src_meas = info.get("sizeMeasurements") or {}
    else:
        src_meas = info.get("sizeMeasurements") or {}

    # 【源表里的平铺半围先换算成平台要的全围】源图写「腰围x2」「20x2」「腰围半围」
    # 「平铺腰围」说的都是平铺单面的半围，平台参数（腰围全围/臀围全围/测量全围）要的是
    # 绕一圈的全围——不换算就填错一半，而且「腰围x2」这名字连 _match_param 都过不了、
    # 只能掉到模型映射或估算（2026-09-11 男童长裤取证，识别写法表见
    # parameters.normalize_half_marks 上方那段）。
    # 换算放在【取值之后、勾参数与对齐之前】：勾选判断、名称映射、估算锚点看到的都是
    # 换算后的同一份数，不会再出现「源半围 20 + 估算全围 38」混在同一列里。
    src_meas = sizechart_parameters.normalize_half_marks(src_meas)

    # 【按源数据主动勾选「尺码参数」复选框】弹窗上方那排参数是【可选】的，平台默认只勾
    # 了分类默认集（背带裤默认勾「领围」），源数据能覆盖的其它部位（裤长/胸围全围/臀围
    # 全围…）默认都没勾。原先代码只读已渲染的 thead、等于只认默认勾的那几项，源数据全
    # 被浪费（offer 1074392045040：源 8 列全在却改估一个「领围」）。源参数列表来自
    # product-info.json、不依赖页面渲染，故这里在读 thead 前先把「源能覆盖的」勾上、
    # 「默认勾中但源没有的」取消掉，再读表格。源参数列表只在勾选这一步用一次，真正的
    # 取值仍用上面配对好的那份（src_meas）。
    src_for_pick = src_meas
    src_param_names = sorted({p for row in src_for_pick.values()
                              for p in (row or {})})
    if src_param_names:
        avail = await session.eval_json(sizechart_scripts._JS_SIZECHART_AVAILABLE_PARAMS)
        available = avail.get("params") or []
        if available:
            pick = await sizechart_parameters._pick_params_by_llm(title, available, src_param_names)
            to_check = pick.get("check") or []
            to_uncheck = pick.get("uncheck") or []
            if to_check or to_uncheck:
                setr = await session.eval_json(
                    sizechart_scripts._JS_SET_SIZECHART_PARAMS
                    .replace("__CHECK__", J(to_check))
                    .replace("__UNCHECK__", J(to_uncheck)))
                if setr.get("changed"):
                    logger.info("尺码参数勾选已调整：" + "、".join(setr["changed"]))
                if not setr.get("ok"):
                    logger.warning(f"尺码参数勾选部分未生效（照旧按当前集填）：{setr}")
        else:
            logger.info("弹窗未读到可选「尺码参数」复选框（可能本平台无此项），照旧按当前集填")

    # 读取参数列表和尺码行（勾选调整后，thead 反映的是新的参数集）
    meta = await session.eval_json(sizechart_scripts._JS_SIZECHART_PARAMS)
    params, sizes = meta.get("params", []), meta.get("sizes", [])
    if not params or not sizes:
        return {"status": "error", "reason": f"弹窗参数/尺码行读取失败: {meta}"}

    # 参数键对齐（弹窗强制参数名可能是「摆长」「测量全围」，而源数据写「总衣长」「腰围」）。
    # 判据走 _match_param 的三级：字面/剥后缀 → 同义词表 → 子串包含，真站取证与
    # 「为什么纯子串不够」见 _PARAM_SYNONYMS 上方那段。extra 是模型补的映射
    # （{平台参数: 源参数}），只在词表全都对不上时才有值。
    def _align_params(vals, extra: Optional[dict] = None):
        out = {}
        for p in params:
            k = sizechart_parameters._match_param(p, vals)
            if k is None and extra:
                mapped = extra.get(p)
                k = mapped if mapped in vals else None
            if k is not None:
                out[p] = vals[k]
        return out

    # 测量值来源：源商品实测平铺尺寸（sizeMeasurements）优先，弹窗要而源没有的参数交模型估算。
    # 尺码键与弹窗尺码行两侧都过 norm_size 再比（源键可能带「建议身高」描述，见 norm_size）
    #
    # 【为什么按参数补而不是整块判死】2026-08-22 实测 984360345330（套头衫）：源尺码表只有
    # 衣长/裤长两列（店家套了套装模板），弹窗按类目强制的胸围全围/袖长一个都没有。原先
    # 「有源数据就必须全齐，否则报错要人工清空字段走兜底」——源给了一半反倒比完全没有更糟。
    # 改为源给的照用（实测值比估算准），只把缺的那几列问模型，人工不必再介入。
    # （分件配对已在勾选参数之前完成，src_meas 就是本张表那件的数据）
    src_norm = {normalize_size(k): (v or {}) for k, v in src_meas.items()}
    size_keys = {normalize_size(s) for s in sizes}
    if src_norm and not (set(src_norm) & size_keys):
        logger.warning(f"源尺寸表尺码 {list(src_norm)[:5]} 与弹窗尺码 {list(size_keys)[:5]} "
                       f"完全对不上，保留为模型估算参考")
    # 【「键在不在」与「值能不能填」必须是同一个判据】need 原先只判参数键在不在，
    # 而下面的 lacking 判的是「值是不是正有限数」，两套口径对不上就成了死结：源里某列
    # 的值是脏的（字符串 "74-78"、区间文本、0），need 认为不缺 → 不问模型 → 来源记成
    # source，lacking 认为缺 → 直接报「测量数据缺参数」，报出来的「模型应补 []」正是
    # need 空集的自证。2026-09-10 两单（993873426154、994643657205）卡死在这里，重跑
    # 必然复现、且模型永远没机会补。统一到 _valid_value 之后，脏值会进 need 交模型重估。
    def _valid_value(v) -> bool:
        return (isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and v > 0)

    def _missing(row, p) -> bool:
        return not _valid_value((row or {}).get(p))

    norm = {k: _align_params(v) for k, v in src_norm.items()}
    need = [p for p in params
            if any(_missing(norm.get(normalize_size(s)), p) for s in sizes)]

    # 【词表兜不住时先问名称映射，再退估算】源参数名是商家在图上随手写的自由文本
    # （腰围(橡筋)、裤长(不含吊带)…），穷举不完；而对齐失败的代价是【整列改用凭空
    # 估算】而不是少填一列（真站取证见 _PARAM_SYNONYMS）。故只要源确实给了数据、
    # 却有参数一列都没对上，就先花一次极短的调用问名字，比整表重新造数划算。
    if need and src_norm and set(src_norm) & size_keys:
        src_keys = sorted({k for row in src_norm.values() for k in (row or {})})
        # 只问那些【一行都没对上】的参数：部分尺码缺值是源表本身不全，问名字没用
        unmapped = [p for p in need
                    if not any(p in (norm.get(normalize_size(s)) or {}) for s in sizes)]
        if unmapped and src_keys:
            extra = await sizechart_parameters._map_params_by_llm(unmapped, src_keys)
            if extra:
                norm = {k: _align_params(v, extra) for k, v in src_norm.items()}
                need = [p for p in params
                        if any(p not in (norm.get(normalize_size(s)) or {}) for s in sizes)]

    # 【源数据一列都没用上的异常】源给了实测尺寸，平台参数却一个都对不上
    # （need 覆盖了全部 params）：经过上面 LLM 勾选后仍出现，说明源表头连语义匹配
    # 都对不上任何可选项（或源数据与商品严重不符）。源数据全白费意味着买家拿到一份
    # 维度对不上实物的尺码表，必须报出来交人工复核，不能静默填。
    source_unused = bool(src_meas) and bool(need) and set(need) == set(params)
    if source_unused:
        logger.warning(
            "尺码表参数与源实测尺寸一列都没对上（源有数据却全走估算）："
            "平台参数 " + "、".join(params)
            + "；源参数 " + "、".join(sorted(
                {k for row in src_norm.values() for k in (row or {})})))

    if need:
        est = await sizechart_measurements._estimate_measurements(title, size_ref, sizes, need, norm,
                                           part=part_used, src_rows=src_norm,
                                           cat_path=cat_path, normalize_size=normalize_size)
        for s in sizes:
            key = normalize_size(s)
            row = dict(norm.get(key) or {})
            # 只填 need 里本行【还缺有效值】的：有效的源实测值照旧绝不被模型值覆盖。
            # 原判据是「键不在 row 里」，而脏值恰恰是键在、值不可用——统一判据后 need
            # 会把它们列出来，这里却仍拦着不让模型值写进去，等于白问一次模型。
            for p, v in _align_params(est.get(normalize_size(s)) or {}).items():
                if p in need and _missing(row, p):
                    row[p] = v
            norm[key] = row
        gen = "source+model" if src_meas else "model"
    else:
        gen = "source"

    # _JS_FILL_SIZECHART 按【页面原始尺码文本】取 data[size]，故归一只用于匹配，
    # 最终 norm 的键必须换回页面原文，否则填表时全部取不到值
    norm = {s: norm.get(normalize_size(s), {}) for s in sizes}
    # 与上面的 need 共用 _missing：判据分家过一次（need 判键、这里判值），代价是脏值
    # 那两单永远填不出表，各自维护一套必然再次分家
    lacking = [s for s in sizes if any(_missing(norm[s], p) for p in params)]
    if lacking:
        return {"status": "error",
                "reason": f"测量数据缺参数（对齐后仍缺）: 尺码{lacking} × 参数{params}"
                          f"；来源 {gen}，模型应补 {need}",
                "data": norm}

    # 填表格
    fill = await session.eval_json(sizechart_scripts._JS_FILL_SIZECHART
                                    .replace("__NAME__", J(tpl_name))
                                    .replace("__DATA__", J(norm))
                                    .replace("__PARAMS__", J(params)))
    if not fill.get("ok"):
        return {"status": "error", "reason": f"表格填充不完整: {fill.get('empty')}",
                "data": norm}

    # 点确定
    done = await session.eval_json(sizechart_scripts._JS_CLICK_SIZECHART_OK)
    if done.get("stillOpen"):
        return {"status": "error", "reason": "点确定后弹窗未关闭（校验未过？）", "fill": fill}

    # 回读验证
    final = await session.eval_json(_sc_js(sizechart_scripts._JS_SIZECHART_STATE, which))
    ok = tpl_name in final.get("text", "")
    return {"status": "ok" if ok else "validation-error",
            "which": which, "label": final.get("label"),
            "tplName": tpl_name, "category": sel.get("selected"),
            "categorySource": sel.get("source"),
            "params": params, "measureSource": gen, "estimated": need,
            "partUsed": part_used, "sourceUnused": source_unused,
            "data": norm, "formText": final.get("text"),
            "charts": final.get("charts", st.get("charts", 1))}
