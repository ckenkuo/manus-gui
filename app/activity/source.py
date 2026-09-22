"""活动管线的金山文档数据源与商品预览。"""

import math
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.orders.kdocs_sheet import KdocsSheet, KdocsSheetError
from app.tool.wps_excel_tool import WpsExcelTool

MAX_ROWS = 20000
READ_BATCH = 500
REQUIRED_FIELDS = {"spu": "SPU", "daily": "日常价", "sale": "销售价（底价）"}


def validate_document(document: str) -> str:
    document = str(document or "").strip()
    parsed = urlparse(document)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        host == "kdocs.cn" or host.endswith(".kdocs.cn")
        or host == "wps.cn" or host.endswith(".wps.cn")
    ) or parsed.username or parsed.password:
        raise ValueError("请填写金山 WPS 在线表格的分享链接，不支持本地 Excel 路径。")
    return document


def price_value(value):
    text = str(value or "").strip().replace(",", "")
    text = re.sub(r"[¥￥$元\s]", "", text)
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def read_document(document: str, sheet: str = "") -> dict:
    document = validate_document(document)
    cloud = KdocsSheet(document)
    sheets = cloud.sheet_names()
    result = {
        "cloud_url": document, "sheets": sheets, "sheet": sheet,
        "columns": [], "rows": [], "fields": {}, "warnings": [],
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    }
    if not sheet:
        return result
    if sheet not in sheets:
        raise ValueError("所选工作表已不存在，请重新选择。")
    candidates = cloud.read_rows(sheet, 1, 10)
    best_score = -1
    header = {}
    header_row = 1
    fields = {}
    for row_number, cells in sorted(candidates.items()):
        titles = {column: cell["text"].strip() for column, cell in cells.items() if cell["text"].strip()}
        mapped = WpsExcelTool._resolve_fields_from_header(titles)
        score = len(set(mapped) & set(REQUIRED_FIELDS))
        if score > best_score:
            best_score, header, header_row, fields = score, titles, row_number, mapped
    missing = [label for field, label in REQUIRED_FIELDS.items() if field not in fields]
    if missing:
        result["warnings"].append("未识别到列：" + "、".join(missing) + "；请核对工作表表头，当前不可选择商品。")
    # 合并单元格只展开身份/展示列：一个 SPU 多行货号时 SPU/站点/类目是纵向合并格，
    # 不展开会把合并覆盖的后续货号行读成「SPU 为空」整行作废。价格列绝不进白名单——
    # 空价格 = 该货号行无效、整组不可选（宁缺毋错：静默继承价格可能报错价，报错价
    # 不可逆，行不可选是可见可修的）。
    expand_cols = {fields[k] for k in ("spu", "site", "category") if k in fields}
    end_row = cloud.data_end_row(sheet) + 1
    if end_row - header_row > MAX_ROWS:
        raise KdocsSheetError(f"工作表超过 {MAX_ROWS} 行，请选择较小的工作表后读取。")
    records = []
    for start_row in range(header_row + 1, end_row + 1, READ_BATCH):
        rows = cloud.read_rows(sheet, start_row, min(start_row + READ_BATCH - 1, end_row),
                               expand_merged=expand_cols)
        for row_number, cells in sorted(rows.items()):
            values = {column: cell["text"].strip() for column, cell in cells.items()}
            if not any(values.values()):
                continue
            # 展开只补身份：若一行除白名单列外没有任何自有数据，说明它是合并区尾巴上
            # 的空行（合并多拖了一行），不是真货号行——跳过，否则它的空价格会连坐
            # 整个 SPU 不可选。
            if not any(value for column, value in values.items() if column not in expand_cols):
                continue
            spu = values.get(fields.get("spu"), "")
            daily = price_value(values.get(fields.get("daily")))
            sale = price_value(values.get(fields.get("sale")))
            issues = []
            if missing:
                issues.append("缺少必需列")
            if not re.fullmatch(r"[0-9]+", spu):
                issues.append("SPU 为空或不是完整数字")
            if daily is None:
                issues.append("日常价无有效计算结果")
            if sale is None:
                issues.append("销售底价无有效计算结果")
            records.append({
                "row_number": row_number, "spu": spu,
                "sku": values.get(fields.get("sku"), ""),
                "daily": daily, "sale": sale,
                "purchase": values.get(fields.get("purchase"), ""),
                "values": values, "issues": issues, "selectable": not issues,
            })
    # 按 SPU 分组判定：一个 SPU 允许多行货号（各自价格不同是合法场景），但提报页勾选
    # 是 SPU 级、无法只报部分货号，故任一货号行无效 → 整组连坐不可选，防止按残缺价格申报。
    by_spu = {}
    for record in records:
        if record["spu"]:
            by_spu.setdefault(record["spu"], []).append(record)
    for group in by_spu.values():
        # 货号重复守卫：同一 SPU 下同一货号出现多次，货号维已乱（执行层按行数/价格多重
        # 集合匹配会算错数量），读文档期就拦下。
        label_rows = {}
        for record in group:
            if record["sku"]:
                label_rows.setdefault(record["sku"], []).append(record["row_number"])
        for label, dup_rows in label_rows.items():
            if len(dup_rows) > 1:
                rows_text = "、".join(str(r) for r in dup_rows)
                for record in group:
                    if record["sku"] == label:
                        record["issues"].append(
                            f"同一 SPU 货号「{label}」重复（第 {rows_text} 行），请先核对")
        if len(group) < 2:
            continue
        bad = [record for record in group if record["issues"]]
        if not bad:
            continue
        bad_rows = "、".join(str(record["row_number"]) for record in bad)
        for record in group:
            record["selectable"] = False
            if not record["issues"]:
                record["issues"].append(f"同一 SPU 第 {bad_rows} 行数据无效，整组不可选")
    result.update(
        columns=[{"key": column, "title": title} for column, title in header.items()],
        rows=records, fields=fields, header_row=header_row,
        total=len(records), selectable=len({record["spu"] for record in records if record["selectable"]}),
    )
    return result


def read_costs(document: str, sheet: str, spus) -> dict:
    """读取所选 SPU 的逐货号价格 → {spu: {"items": [{label, daily, sale, purchase, row_number}]}}。

    一个 SPU 可有多个货号行（各自日常价/底价不同），items 按行号升序、货号序即表内行序。
    仅当该 SPU 全部货号行都有效（selectable）才收录——提报页勾选是 SPU 级，无法只报
    部分货号，任一货号价格无效都不给价，避免按残缺价格申报。label 取货号列，无货号列
    兜底「行N」（执行层加速器按货号文本匹配时会因此中止不开，属预期 fail-closed）。
    """
    snapshot = read_document(document, sheet)
    targets = set(spus)
    costs = {}
    for record in snapshot["rows"]:
        if record["spu"] in targets and record["selectable"]:
            entry = costs.setdefault(record["spu"], {"items": []})
            entry["items"].append({
                "label": record.get("sku") or f"行{record['row_number']}",
                "daily": record["daily"],
                "sale": record["sale"],
                "purchase": record["purchase"],
                "row_number": record["row_number"],
            })
    missing = targets - costs.keys()
    if missing:
        raise ValueError("文档中的商品已缺失或价格无效，请刷新后重新选择：" + "、".join(sorted(missing)))
    return costs
