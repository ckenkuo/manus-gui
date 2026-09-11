"""店小秘发布操作：attributes.validation。模块导航见 docs/publish-pipeline-refactor.md。"""

import re
from app.logger import logger
from app.publish.attributes import composition as attributes_composition
from typing import Optional


def _validate_attr_changes(changes: list, attrs: list,
                           main_comp: Optional[dict] = None) -> tuple:
    """对 LLM 的修改清单做二次校验，返回 (valid, rejected)。

    LLM 会编造不存在的选项、也会违反「非必填不填」的策略，故不能直接执行。四道闸：
      1. 非必填且当前未填 → 拒（策略：填得多错得多）
      2. 下拉行 value 不在该行 options 内 → 拒（编造值点不中，白跑一趟还留幽灵浮层）；
         数值输入行（kind=number，如里料克重）没有 options，改校验量级合理性，
         并把 LLM 常带的单位剥掉只留数字
      3. 主面料成分字段：有源含量时按源值重建成分行，模型给的百分比不作准
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
        elif (c.get("value") == cur and cur and not cur.startswith("(")
              and any(kw in str(c.get("reason", "")).lower()
                     for kw in ["保持", "keep", "不动", "不改", "原值"])):
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
        filler = next((o for o in attributes_composition._COMP_FILLERS
                       if o in opts and attributes_composition._fiber_key(o) not in used), None)
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
