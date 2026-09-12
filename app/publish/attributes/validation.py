"""店小秘发布操作：attributes.validation。模块导航见 docs/publish-pipeline-refactor.md。"""

import re
from app.logger import logger
from app.publish.attributes import composition as attributes_composition
from typing import Optional


def _stale_main_comp(row: dict, main_comp: Optional[dict]) -> bool:
    """主面料成分字段「页面现状合计 ≠ 100、且源值能确定性重建」时返回 True。

    【为什么要在 keep_current 这条路上单开口子】模型判「保持原值」的依据是 current，
    而成分字段读到的 current 只有第 1 行的纤维名——它看不到也不去算这个字段的百分比
    合计（numValues 虽然就在喂给它的行里，但「保持原值」这条规则本身只管 current 与
    源参数对不对得上）。于是合计 117% 的字段照样被判「保持原值」，而 keep_current 是
    【不进 valid】的，comp 分组、合计校验、_rebuild_main_comp 的源值确定性覆盖跟着
    全被绕过，页面原样留着认领带来的旧行，一路带到保存被平台拦「单个材料属性的百分比
    之和需等于100」（2026-09-12 商品 908737332112）。

    放行的只有「合计不对」这一类：合计本来就 100 的字段照旧保持原值、一个下拉都不点
    ——那是这条规则的本意，也是它省时间的价值所在。合计不对的落回正常流程后进 comp
    分组、被 _rebuild_main_comp 按源主成分含量重建成 2 行，那条路本就是确定性的。

    【前提必须与重建的触发条件严格对齐】多一个 main_comp 有含量的要求：源没给含量时
    重建压根不会发生，那条 change 会直落下面「合计 100%」的补差分支，被当成唯一一行
    独占 100%（页面上的 90+5+22 会被写成单行 100%）——那比放着不动更糟。

    【代价：配料纤维降一级依据】重建的第 2 行优先沿用「模型选的配料」，而这条路上模型
    只给了一条「保持原值」（value 就是主纤维本身），会被 _rebuild_main_comp 的 used 判
    同过滤掉，于是退到下一级——源属性推断（_infer_filler）或候选表（_COMP_FILLERS）。
    即页面预填的配料可能被换成另一根纤维。这个取舍是接受的：能触发例外的本就是「页面
    合计已经不对」的字段，而源属性里明写的纤维本来就比预填更硬；顺带一提，主纤维是
    聚酯纤维时候选表给出的正是氨纶，与常见的弹性混纺预填一致。
    """
    if not (main_comp and main_comp.get("percent")):
        return False
    # 「产品属性」是包裹全部属性行的分组标题行，它的 numValues 是各字段百分比的大杂烩
    # （详见 form._comp_total_problems 的同名排除）。当前 _is_main_comp_label 也会拒掉它
    # （名字里没有「成分/材质」），但那是两个判据凑巧对齐，靠不住：这里显式挡一道。
    if str(row.get("label") or "") == "产品属性":
        return False
    if not row.get("hasPercent"):
        return False
    if not attributes_composition._is_main_comp_label(str(row.get("label") or "")):
        return False
    return abs(attributes_composition._comp_percent_total(row) - 100) > 1e-6


def _validate_attr_changes(changes: list, attrs: list,
                           main_comp: Optional[dict] = None) -> tuple:
    """对 LLM 的修改清单做二次校验，返回 (valid, rejected)。

    LLM 会编造不存在的选项、也会违反「非必填不填」的策略，故不能直接执行。四道闸：
      1. 非必填且当前未填 → 拒（策略：填得多错得多）
      2. 下拉行 value 不在该行 options 内 → 拒（编造值点不中，白跑一趟还留幽灵浮层）；
         数值输入行（kind=number，如里料克重）没有 options，改校验量级合理性，
         并把 LLM 常带的单位剥掉只留数字
      3. 主面料成分字段：有源含量时按源值重建成分行，模型给的百分比不作准；页面现状
         合计 ≠ 100 的字段连「保持原值」都不认，一并拉进重建（见 _stale_main_comp）
      4. 其余成分类合计必须 100%：不足则自动补一行填充纤维，超过则整组拒绝交人工

    main_comp 为 extract.parse_main_composition 的产物（可能为 {}）：为空时第 3 闸
    整个跳过，退回原有的「模型给数 + 合计校验」路径——不为了凑数去猜源页面没写的东西。
    """
    import json as _json

    valid: list = []
    rejected: list = []
    keep_current: list = []  # 明确标记"保持原值"的项
    row_map = {a["label"]: a for a in attrs}
    opt_map = {a["label"]: a.get("options", []) for a in attrs}
    for c in changes:
        label = c.get("label")
        row = row_map.get(label, {})
        cur = row.get("current") or ""
        if not row:
            rejected.append({**c, "rejectReason": "表单没有这个属性行"})
        elif not row.get("required") and cur.startswith("("):
            # 【不看 options 是否为空，只看必填与否】2026-08-24 起非必填未填的行一律
            # 留空，源商品写了也不填。原先这里放行「options 非空」的非必填行（那是
            # dump_attrs 按源键匹配特意读出来的），随那个例外一起撤掉；required_only=
            # False 的探查场景会给所有行都读上 options，用旧写法等于把闸门整个打开。
            rejected.append({**c, "rejectReason": "非必填且当前未填，按策略留空"})
        # 【保持原值识别】value == current 且 reason 包含"保持"关键词 → 标记为 keep_current
        # 这类 change 不需要实际写入（页面已经是这个值），但要记录下来，避免被误判为"该改没改"
        # 【主面料成分的例外】页面合计不对时不吃"保持原值"，落回下面的正常流程走源值重建
        # （见 _stale_main_comp）。这道例外必须加在这里、不能等进了 valid 再挑：keep_current
        # 压根不进 valid，一旦记进去，这个字段就再没有别的分支会碰它了。
        elif (c.get("value") == cur and cur and not cur.startswith("(")
              and any(kw in str(c.get("reason", "")).lower()
                     for kw in ["保持", "keep", "不动", "不改", "原值"])
              and not _stale_main_comp(row, main_comp)):
            keep_current.append({**c, "reason": c.get("reason", "") + "（已验证匹配）"})
        elif row.get("kind") == "number":
            # 【数值行不查 options】里料克重这类纯输入行 options 恒为空，走 options 闸
            # 会把每个值都拒掉，该行永远填不上、保存卡在「请输入产品属性」
            # （2026-08-24 用户截图的自动化断点）。改为只校验量级合理性。
            # 取值：优先 value，空则回退 num（LLM 两处都可能放数值）
            raw = str(c.get("value") or "").strip()
            if not raw and c.get("num") is not None:
                raw = str(c.get("num")).strip()
            # 【负号必须纳入匹配】只写 \d+ 会从 "-5" 里提出 "5"，负数就被放行了
            m = re.search(r"-?\d+(?:\.\d+)?", raw)
            if not m:
                rejected.append({**c, "rejectReason": f"数值行但给的不是数字：{raw!r}"})
            elif not (0 < float(m.group()) <= 100000):
                rejected.append({**c,
                                 "rejectReason": f"数值 {m.group()} 超出 0~100000 合理范围"})
            else:
                # 统一成纯数字串写入：LLM 常带上单位（"120g/m²"），输入框只收数字
                valid.append({**c, "value": m.group(), "kind": "number"})
        elif row.get("kind") == "checkbox":
            # 【复选框组：选项来自行内 DOM，一趟校验一条值】这类行是多选（如「颜色」
            # 116 个复选框），LLM 给几个值就出几条同 label 的 change，每条都按
            # 「在该行 options 内」这一条闸过——语义与下拉行完全相同，故不另立判据。
            # kind 必须显式标透：_apply_attr_changes 靠它把同一 label 的多条归并成
            # 一个目标集合一次性重设，漏标会让它们各自走下拉分支、必然 no-select。
            if c.get("value") in opt_map.get(label, []):
                valid.append({**c, "kind": "checkbox"})
            else:
                rejected.append({**c, "rejectReason": "value 不在 options 内，已拒绝"})
        elif c.get("value") in opt_map.get(label, []):
            valid.append(c)
        else:
            rejected.append({**c, "rejectReason": "value 不在 options 内，已拒绝"})

    # 成分和=100 校验：同一成分字段的 num 合计
    #
    # 【分组判据是行结构，不是「LLM 给没给 num」】原先写 `if c.get("num")`：模型漏给
    # num（或给 0/null）时那行根本不进分组，于是「合计=100」这道闸整个不执行，
    # set_attr 里 `num is not None` 也不成立、百分比框不填——纤维选上了、百分比空着，
    # 平台报「请完善里衬成分信息」（2026-08-25 用户截图实测）。闸本身没坏，是该进闸
    # 的行没进来，而这恰恰在模型输出不全时才发生，正是最该兜住的场景。
    # 现在按枚举给的 hasPercent（下拉后面跟可填 input）认成分行：是不是成分行由 DOM
    # 决定，模型漏 num 就走下面的补差逻辑填满 100%，而不是静默留空。
    comp: dict = {}
    for c in valid:
        # 【数值属性行必须排除】里料克重 90 g/m² 这类的数值可能放在 num 里，被这里
        # 收进成分分组就会按「合计必须 100」拒掉（实测报「成分合计 90%<100%」），
        # 该行又填不上了。克重是物性数值，与成分百分比无关。
        if c.get("kind") == "number":
            continue
        row_meta = row_map.get(c["label"]) or {}
        is_comp = row_meta.get("hasPercent") or bool(c.get("num"))
        if is_comp:
            comp.setdefault(c["label"], []).append(c)
    for label, items in comp.items():
        opts = opt_map.get(label, [])
        # 主面料成分：源含量说了算，先按源值重建整组，重建成功就不再走合计校验
        if main_comp and main_comp.get("percent") and attributes_composition._is_main_comp_label(label):
            rows, reject = attributes_composition._rebuild_main_comp(label, items, opts, main_comp)
            if rows:
                valid = [v for v in valid if v["label"] != label] + rows
                continue
            if reject:
                rejected.append(reject)
                valid = [v for v in valid if v["label"] != label]
                continue
            # 【区分两种退回】fiber 为空 = 源是「其它」这类占位词（parse 阶段已识别、
            # 打 assumed），此时不该说「不在 options 内」——那会误导成选项列表缺纤维；
            # 实际是源没有可确定的纤维，只能靠 LLM 依据面料名称/品类/图片推断。
            if main_comp.get("fiber"):
                logger.warning(
                    f"{label}: 源主纤维「{main_comp.get('fiber')}」不在 options 内，"
                    "退回模型给数 + 合计校验")
            else:
                logger.warning(
                    f"{label}: 源主面料成分无效（{main_comp.get('assumed') or '占位词'}），"
                    "交 LLM 依据面料名称/品类/图片推断成分")
        # 先合并同一根纤维的多行：模型可能同时给出「涤纶 80%」和「聚酯纤维 20%」，
        # 那是同一根纤维的两种写法，平台不接受同字段两行同纤维（2026-08-24 实测报错），
        # 正解是合并成一行 100%。合并后再算合计，多数情况直接就等于 100 了。
        merged = attributes_composition._merge_same_fiber(items, opts)
        if len(merged) != len(items):
            valid = [v for v in valid if v["label"] != label] + merged
            items = merged
        total = sum(i["num"] for i in items if i.get("num"))
        # 【模型漏给 num 的行先按「独占剩余」补满，再判合计】否则 total=0 会掉进下面的
        # 补差分支，去补一根【别的】纤维 100%，把模型真正选中的那根留在 0%——里衬成分
        # 只有一行时表现就是「棉Cotton 选上了、百分比空着」。单行独占 100 是成分行最
        # 常见的形态（里衬/辅料基本都是单一纤维），故这里按剩余量补给缺 num 的行：
        # 一行缺就补满剩余，多行缺就均分（除不尽的余数给第一行，保证合计精确等于 100）。
        blank = [i for i in items if not i.get("num")]
        if blank and total < 100:
            share, rest = divmod(100 - total, len(blank))
            for k, i in enumerate(blank):
                i["num"] = share + (rest if k == 0 else 0)
                i["reason"] = (str(i.get("reason") or "")
                               + f"（模型未给含量，自动补 {i['num']}%）")
            total = sum(i["num"] for i in items if i.get("num"))
        if total == 100:
            continue
        if total > 100:
            rejected.append({"label": label,
                             "rejectReason": f"成分合计 {total}%>100%，整组拒绝，需人工核对"})
            valid = [v for v in valid if v["label"] != label]
            continue
        # 填充纤维要排除该组已占用的：主成分本身是聚酯纤维时再补一行聚酯纤维必被平台拦。
        # 【必须用 _fiber_key 而不是 _norm_fiber】后者只去括号，「涤纶」与「聚酯纤维」
        # 归一后不相等，排除会失效并填出同字段两行同纤维。
        used = {attributes_composition._fiber_key(i.get("value", "")) for i in items}
        # 挑候选必须走 _pick_filler 的 _match_fiber 匹配，不能 `o in opts`：候选表是裸
        # 写法、options 带括号注解，精确相等全数失配（见 _pick_filler 的说明）。
        filler = attributes_composition._pick_filler(opts, used)
        if filler:
            valid.append({"label": label, "value": filler, "num": 100 - total,
                          "row": max((i.get("row") or 1) for i in items) + 1,
                          "reason": f"补差 {100 - total}% 凑足 100%（自动）"})
        else:
            rejected.append({
                "label": label,
                "rejectReason": f"成分合计 {total}%<100% 且 options 无可用填充纤维"})
            valid = [v for v in valid if v["label"] != label]

    # 同字段多行按 row 升序：先覆盖第 1 行再加新行，否则加行时行号对不上
    valid.sort(key=lambda c: (c["label"], c.get("row") or 1))
    return valid, rejected, keep_current
