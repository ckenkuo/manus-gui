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
    end_row = cloud.data_end_row(sheet) + 1
    if end_row - header_row > MAX_ROWS:
        raise KdocsSheetError(f"工作表超过 {MAX_ROWS} 行，请选择较小的工作表后读取。")
    records = []
    for start_row in range(header_row + 1, end_row + 1, READ_BATCH):
        rows = cloud.read_rows(sheet, start_row, min(start_row + READ_BATCH - 1, end_row))
        for row_number, cells in sorted(rows.items()):
            values = {column: cell["text"].strip() for column, cell in cells.items()}
            if not any(values.values()):
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
                "row_number": row_number, "spu": spu, "daily": daily, "sale": sale,
                "purchase": values.get(fields.get("purchase"), ""),
                "values": values, "issues": issues, "selectable": not issues,
            })
    by_spu = {}
    for record in records:
        if record["spu"]:
            by_spu.setdefault(record["spu"], []).append(record)
    for duplicates in by_spu.values():
        if len(duplicates) < 2:
            continue
        prices = {(record["daily"], record["sale"]) for record in duplicates}
        if len(prices) > 1 or any(record["issues"] for record in duplicates):
            for record in duplicates:
                record["issues"].append("同一 SPU 多行价格不一致或有无效价格，请先核对")
                record["selectable"] = False
    result.update(
        columns=[{"key": column, "title": title} for column, title in header.items()],
        rows=records, fields=fields, header_row=header_row,
        total=len(records), selectable=len({record["spu"] for record in records if record["selectable"]}),
    )
    return result


def read_costs(document: str, sheet: str, spus) -> dict:
    snapshot = read_document(document, sheet)
    targets = set(spus)
    costs = {}
    for record in snapshot["rows"]:
        if record["spu"] in targets and record["selectable"]:
            costs[record["spu"]] = {field: record[field] for field in ("daily", "sale", "purchase")}
    missing = targets - costs.keys()
    if missing:
        raise ValueError("文档中的商品已缺失或价格无效，请刷新后重新选择：" + "、".join(sorted(missing)))
    return costs
