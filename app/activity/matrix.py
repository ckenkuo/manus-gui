# -*- coding: utf-8 -*-
"""活动识别矩阵的当天落盘。

为什么要落这一层：识别矩阵要逐个（商品×活动）开详情页探测资格，每格 10~20 秒，
扫一次很贵。于是把「资格」这个客观事实当天持久化，重扫时命中即跳过、刷新页面也能回读；
跨日按天切文件——活动是按期数轮换的（「官方大促」每月一轮），跨天复用会拿到过期资格。

【只缓存客观事实，不缓存判断】对齐 app/publish/cache.py 的既定取向：这里只存平台的
资格结论（eligible 真/假）与扫描时间；价格层（申报价/是否达底价）每次都由成本表现算，
折扣率改了、日常价改了自动反映。`eligible` 为 None（超时/开页失败）**不落盘**——
一次卡顿不该被当成永久事实复用。

骨架照 app/publish/cache.py：模块级只有函数、没有路径常量，子路径一律函数内拼，
否则单测 monkeypatch 会写脏真目录；写入用临时文件 + os.replace 原子替换。
"""

import json
import os
import threading
import time
from typing import Optional

from app.config import get_output_dir
from app.logger import logger

SCHEMA_VERSION = 1

_lock = threading.Lock()


def _dir() -> str:
    """矩阵落盘目录（桌面输出目录下的分类子目录，不可写时 get_output_dir 自行回退）。

    包一层函数是为了让单测 monkeypatch 它指向 tmp_path——绝不能在测试里真写桌面。
    """
    return str(get_output_dir("activity"))


def today() -> str:
    return time.strftime("%Y-%m-%d")


def matrix_path(day: str = "") -> str:
    return os.path.join(_dir(), f"activity-matrix-{day or today()}.json")


def empty(day: str = "") -> dict:
    return {
        "version": SCHEMA_VERSION, "day": day or today(), "generated_at": "",
        "document": "", "sheet": "", "region": "",
        "activities": [], "price": {}, "eligibility": {}, "counts": {},
    }


def load(day: str = "") -> dict:
    """读当天矩阵；缺失/损坏一律返回空骨架（调用方当没有缓存）。"""
    path = matrix_path(day)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty(day)
    except Exception as e:
        logger.warning(f"识别矩阵文件损坏，当没有缓存（{path}）：{e}")
        return empty(day)
    if not isinstance(data, dict) or "activities" not in data:
        return empty(day)
    return data


def save(data: dict) -> str:
    """落盘（临时文件 + os.replace 原子替换），返回文件路径；写失败只告警不抛错。

    为什么不直接覆盖写：json.dump 中途进程被杀会留下截断 JSON，等于把当天扫好的
    几十个格子的资格一次清零——识别一次很贵，丢不起。
    """
    path = matrix_path(data.get("day") or today())
    data["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"识别矩阵落盘失败（{path}）：{e}")
    return path


def apply_scan(prev: dict, day: str, document: str, sheet: str, region: str,
               activities: list, price: dict, eligibility: dict) -> dict:
    """把一次扫描的结果并进当天矩阵，返回新的矩阵数据（不落盘，由调用方 save）。

    - activities/price 全量覆盖：活动列表与价格每次扫描都以现场为准；
    - eligibility 增量保留：只保留「本次仍在扫的 SPU + 仍在列表里的活动」的旧结论，
      再叠加本次新探到的格子。消失的活动整列丢弃（期数换轮了，旧资格没意义），
      新出现的活动自然没有记录 → 前端显示「未扫」，下次扫描补。
    """
    spu_set = {str(spu) for spu in price}
    names = {act["name"] for act in activities}
    kept = {
        spu: {name: cell for name, cell in cells.items() if name in names}
        for spu, cells in (prev.get("eligibility") or {}).items()
        if str(spu) in spu_set
    }
    kept = {spu: cells for spu, cells in kept.items() if cells}
    for spu, cells in (eligibility or {}).items():
        kept.setdefault(str(spu), {}).update(cells)
    data = {
        "version": SCHEMA_VERSION, "day": day, "document": document, "sheet": sheet,
        "region": region, "activities": activities, "price": price, "eligibility": kept,
    }
    data["counts"] = counts(data)
    return data


def flatten_cells(data: dict) -> list:
    """把矩阵摊平成逐格列表（扫描事件与 /activity/matrix 接口共用同一份形状）。

    每格 = 价格层（本地算，含未达底价）+ 资格层（扫出来的，未扫为 None）。
    **不带逐货号明细（skus）**：格子是概览视图，活动页有近百个活动、一格一份货号明细
    会让 scan_start 这个事件涨到几 MB；逐货号价格在报名计划表与落盘文件里都有。
    """
    cells = []
    for spu, entry in (data.get("price") or {}).items():
        entry = entry or {}
        for name, cell in (entry.get("cells") or {}).items():
            eligible = ((data.get("eligibility") or {}).get(spu) or {}).get(name) or {}
            cells.append({
                "spu": spu, "activity": name,
                "verdict": cell.get("verdict"), "note": (cell.get("note") or "")[:200],
                "discount_rate": cell.get("discount_rate"),
                "submit_price": cell.get("submit_price"),
                "floor_price": cell.get("floor_price"),
                "within_floor": bool(cell.get("within_floor")),
                "sku_count": cell.get("sku_count", 0),
                "min_stock": cell.get("min_stock"),
                "eligible": eligible.get("eligible"),
                "scanned_at": eligible.get("scanned_at", ""),
                "from_cache": bool(eligible),
            })
    return sorted(cells, key=lambda c: (c["spu"], c["activity"]))


def counts(data: dict) -> dict:
    """矩阵计数：可报/不可报/未扫（只统计价格初筛通过的格子，其余注定不报）。"""
    total = eligible = ineligible = unknown = 0
    for cell in flatten_cells(data):
        if cell["verdict"] != "pass":
            continue
        total += 1
        if cell["eligible"] is True:
            eligible += 1
        elif cell["eligible"] is False:
            ineligible += 1
        else:
            unknown += 1
    return {"cells": total, "eligible": eligible,
            "ineligible": ineligible, "unknown": unknown}
