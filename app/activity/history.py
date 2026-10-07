# -*- coding: utf-8 -*-
"""活动历史：把 live 批次的真实平台动作（报名结论 / 加速器开关）持久化到 MySQL。

为什么需要它：活动是【持续时间维度】的行为——活动按期数轮换、加速器有 24h 锁定期、
同一商品会反复报名。而管线的报名结论（含 enrollId）、加速器开关动作此前只活在内存
summary 与 SSE 事件流里，批次结束即消失（matrix.py 的资格缓存又按天作废）。没有历史，
下次给同一商品搞活动时看不到「报过哪些活动、哪些成了、申报价多少、加速器何时开关过」。

为什么入库而不是照 matrix.py 落本地 JSON：多台 PC 都跑活动管线（同 error_report 的
多机动机），本地文件各记各的，同一商品在不同机器跑过历史就分散了。与 pipeline_errors
同库、连接参数缺省逐项复用 [error_report] 段，零新增配置。

为什么用「事件切面」而不是改主流程：所有平台动作都经 on_progress 结构化事件流出
（契约见 app/activity/service.py 模块 docstring），error_report.attach 已是同款切面。
attach_history 在其后再包一层，主流程函数（执行遍/对账/报名循环）一行不改，
UI（app.py）与 CLI（activity_manage.py）自动全覆盖。

只记 live=True（用户 2026-09-30 拍板）：dry-run 只规划、半程（live=False）可逆验证，
都不产生平台事实，不落库。批次级 live 从 exec_start 事件捕获（exec_close/exec_reopen
的异常路径事件缺 live 字段，逐事件判会丢异常记录）。

best-effort：未配置 / 未装 pymysql / 连接失败 / 读写失败一律 logger.warning 吞掉，
降级为「没有历史」，绝不中断扫描/报名主流程（同 error_report 的取向）。写库用
pymysql 短连接 + asyncio.to_thread 下沉线程（_emit 是 await 回调，inline 写保住
事件顺序与测试确定性；执行遍事件间隔秒级，短连接写几十 ms 可忽略）。

表结构首版定稿、永不加列（error_report.py 的 ALTER 教训：CREATE TABLE IF NOT EXISTS
意味着已部署机器上加列不会执行）：易变字段全塞 detail JSON 列。

配置在统一配置源的 [activity_history] 段（app/config.py 的 get_config_section）：

    [activity_history]
    # host/port/user/password/database/instance 缺省逐项复用 [error_report] 段
    # table = "activity_history"   # 默认；只允许字母数字下划线
    # 账号需 SELECT + INSERT（error_report 若配的是只写账号，历史查询会降级为空）

【没有开关】：记录活动历史是这条管线的固有动作，连接参数齐备即生效——不像
error_report 是「出事才写」的可选上报、需要 enabled 兜着。缺省复用 error_report
的连接参数，实际一个字段都不用配；哪台机器连不上库就静默降级为「没有历史」。
"""
import asyncio
import inspect
import json
import re
import socket
import time
import uuid
from typing import Callable, Optional

from app.config import get_config_section
from app.logger import logger

DEFAULT_TABLE = "activity_history"
MACHINE = socket.gethostname()

# 表名白名单：表名要拼进 DDL/SQL，参数化不了，只放行字母数字下划线（同 error_report）。
_TABLE_RE = re.compile(r"^[A-Za-z0-9_]+$")

KIND_ENROLL = "enroll"
KIND_ACCEL_CLOSE = "accel_close"
KIND_ACCEL_OPEN = "accel_open"

# detail 内 note 类自由文本的截断上限（照 error_report 的 _MESSAGE_MAX 先例）：
# 防极端长文本把行写肥；实际 detail 只有几百字节到几 KB（最大是 accel_prices 逐货号数组）。
_NOTE_MAX = 2000
# summarize 一次归并的行数上限：每 SPU 一批约 10 条（n 活动 + 1~2 加速器），
# 50 条/SPU 约覆盖最近 5 批；全局再压 2000 行兜底（几百 KB JSON，短连接一次查完没压力）。
_SUMMARY_PER_SPU = 50
_SUMMARY_ROW_CAP = 2000

_DDL = """CREATE TABLE IF NOT EXISTS `{table}` (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    machine VARCHAR(64) NOT NULL COMMENT '执行机器 hostname',
    instance VARCHAR(64) NOT NULL DEFAULT '' COMMENT '实例标识（[activity_history].instance）：同机多实例/多账号时区分',
    region VARCHAR(32) NOT NULL DEFAULT '' COMMENT 'Temu 区域标签（UI 选定的 全球/美国 等；切区域=换域名）',
    batch_uid VARCHAR(32) NOT NULL DEFAULT '' COMMENT '批次 UID：一次执行遍的唯一标识，汇总/对账按它归组',
    spu VARCHAR(32) NOT NULL DEFAULT '' COMMENT '商品 SPU',
    activity VARCHAR(128) NOT NULL DEFAULT '' COMMENT '活动名；仅 enroll 行有，加速器开关行（SPU 粒度）留空',
    kind VARCHAR(16) NOT NULL COMMENT '事件类型：enroll=报名结论 / accel_close=关流量 / accel_open=开流量',
    ok TINYINT(1) NOT NULL DEFAULT 0 COMMENT '期望终态是否确认：enroll=报名成功，close=流量已关，open=流量已开',
    status VARCHAR(32) NOT NULL DEFAULT '' COMMENT '结果状态细分：enroll 取事件结论（done/skip/fail/info 等）；accel 由纯函数派生（closed/already_off/cooldown/opened/throttled/rejected 等）',
    detail MEDIUMTEXT COMMENT '事件明细 JSON（易变字段全塞这里：表结构首版定稿、永不加列）',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '落库时间',
    PRIMARY KEY (id),
    KEY idx_spu_time (spu, created_at),
    KEY idx_activity_time (activity, created_at),
    KEY idx_batch (batch_uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='活动报名历史：只记 live 批次的平台动作（切面写入，best-effort）'"""

_INSERT = """INSERT INTO `{table}`
    (machine, instance, region, batch_uid, spu, activity, kind, ok, status, detail)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""

_SELECT = """SELECT id, machine, instance, region, batch_uid, spu, activity, kind, ok, status,
    detail, created_at FROM `{table}`"""


def load_config() -> dict:
    """读统一配置源的 [activity_history] 段；连接参数逐项缺省回退 [error_report]。

    为什么逐项回退而不是整段回退：各机部署时 error_report 的连接参数本就配好，
    逐项回退即可零新增配置生效（与配置中心复用它的取向一致）；单配某项（比如
    另一组账号）也只需写那一项。本段没有 enabled 开关——活动历史是这条管线的
    固有记录，连接参数齐备就该记；返回的 enabled 只表示「连接参数是否齐备」，
    供调用方短路，不是用户可以关的功能开关。

    best-effort：读不到 / 解析失败一律 enabled=False（照 error_report.load_config）。
    """
    result = {
        "enabled": False, "host": "", "port": 3306, "user": "", "password": "",
        "database": "", "table": DEFAULT_TABLE, "instance": "",
    }
    try:
        section = get_config_section("activity_history") or {}
        fallback = get_config_section("error_report") or {}
        for key in ("host", "port", "user", "password", "database", "instance"):
            value = section.get(key)
            if value in (None, ""):
                value = fallback.get(key)
            if value not in (None, ""):
                result[key] = value
        result["host"] = str(result["host"]).strip()
        result["database"] = str(result["database"]).strip()
        result["instance"] = str(result["instance"]).strip()
        result["port"] = int(result["port"] or 3306)
        table = str(section.get("table") or DEFAULT_TABLE).strip()
        result["table"] = table if _TABLE_RE.match(table) else DEFAULT_TABLE
        result["enabled"] = bool(result["host"]) and bool(result["database"])
    except Exception as e:
        logger.warning(f"读取 [activity_history] 配置失败，本次不记历史：{e}")
        result["enabled"] = False
    return result


def _connect_kwargs(cfg: dict) -> dict:
    return {
        "host": cfg["host"], "port": cfg["port"], "user": cfg["user"],
        "password": cfg["password"], "database": cfg["database"],
        "charset": "utf8mb4", "connect_timeout": 5,
        # 历史写在批次事件流里被 await、读在扫描/规划主路径上：慢速公网不设读写超时
        # 会一直挂到 TCP 超时，拖住整批（error_report._write_snapshot 同款教训）。
        "read_timeout": 15, "write_timeout": 15,
    }


def _write(cfg: dict, entry: dict) -> None:
    """同步写一条记录（在线程里跑）：connect → 建表 → insert → close。

    照 error_report._write：延迟导入 pymysql（未安装时 ImportError 吞掉）；短连接
    不维护池（历史是低频事件）；best-effort，任何一步失败只 logger.warning。
    """
    try:
        import pymysql
    except ImportError:
        logger.warning("未安装 pymysql，活动历史跳过（pip install pymysql）")
        return

    conn = None
    try:
        conn = pymysql.connect(**_connect_kwargs(cfg))
        with conn.cursor() as cur:
            cur.execute(_DDL.format(table=cfg["table"]))
            cur.execute(
                _INSERT.format(table=cfg["table"]),
                (
                    MACHINE, cfg["instance"], entry["region"], entry["batch_uid"],
                    entry["spu"], entry["activity"], entry["kind"], entry["ok"],
                    entry["status"], entry["detail"],
                ),
            )
        conn.commit()
    except Exception as e:
        logger.warning(f"活动历史写库失败（忽略）：{e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_rows(cfg: dict, spu: str = "", spus=None, activity: str = "",
                kind: str = "", limit: int = 200) -> list:
    """同步查记录（在线程里跑），按 id DESC（最新在前）返回行 dict 列表。

    异常【抛给调用方】：读路径的降级语义由 query/summarize 各自定（接口要如实
    报「暂不可用」、运行时注入要静默降级为空），不能在这里吞成同一种空结果。
    """
    import pymysql  # 读路径延迟导入；调用方已判启用，装没装让它抛

    where, params = [], []
    spu_list = [str(s).strip() for s in (spus or []) if str(s).strip()]
    if spu:
        where.append("spu = %s")
        params.append(str(spu))
    elif spu_list:
        where.append("spu IN (" + ",".join(["%s"] * len(spu_list)) + ")")
        params.extend(spu_list)
    if activity:
        where.append("activity = %s")
        params.append(str(activity))
    if kind:
        where.append("kind = %s")
        params.append(str(kind))
    limit = max(1, min(int(limit or 200), 500))
    sql = _SELECT.format(table=cfg["table"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit)

    conn = None
    try:
        conn = pymysql.connect(**_connect_kwargs(cfg))
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, params)
            rows = list(cur.fetchall())
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    for row in rows:
        # created_at 转串给接口/摘要用；datetime 原件另存 _created_dt，
        # 供 summarize 与 DB 时钟算 within_24h（少它就得把串再 parse 回去）。
        row["_created_dt"] = row.get("created_at")
        row["created_at"] = str(row.get("created_at") or "")
        detail = row.get("detail")
        if isinstance(detail, str) and detail:
            try:
                row["detail"] = json.loads(detail)
            except Exception:
                pass  # 损坏行保留原文，不弄丢
    return rows


def _db_now(cfg: dict):
    """取 DB 服务端时钟（datetime）：多机部署时各机系统时钟有漂移，24h 锁定期
    判定必须用与 created_at 同一时钟源（created_at 是 DB 的 CURRENT_TIMESTAMP）。"""
    import pymysql

    conn = None
    try:
        conn = pymysql.connect(**_connect_kwargs(cfg))
        with conn.cursor() as cur:
            cur.execute("SELECT NOW()")
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _clip_note(value) -> str:
    return str(value or "")[:_NOTE_MAX]


def _close_status(ev: dict) -> str:
    """accel_close 的 status 派生（纯函数）。ok=1 仅当期望终态「流量已关」确认。"""
    if ev.get("cooldown"):
        return "cooldown"        # 24h 锁定期拦截，没关成（仍是 on）
    if ev.get("already_off"):
        return "already_off"
    if ev.get("ok"):
        return "closed"
    if ev.get("state") == "unknown":
        return "unknown_state"   # 临场读不到状态，保守没动
    note = str(ev.get("note") or "")
    if note.startswith("成本表折扣列读不到"):
        return "no_discount"     # 定不了档位，按规则不动流量
    return "error"


def _open_status(ev: dict) -> str:
    """accel_open 的 status 派生（纯函数）。ok=1 仅当期望终态「流量已开」确认。

    throttled（限流品按规则不动流量）记 ok=0：流量确实没开；它不是失败，靠
    status 在时间上单独标识（查询「成功的活动」只看 enroll，不受影响）。
    """
    if ev.get("throttled"):
        return "throttled"
    if ev.get("already_on"):
        return "already_on"
    if ev.get("ok"):
        return "opened"
    note = str(ev.get("note") or "")
    if note.startswith("成本表折扣列读不到"):
        return "no_discount"
    if note.startswith("异常："):
        return "error"
    return "rejected"            # 平台没开成（档上限低于底价 / 无入口 / 回查非加速中）


def _entry(region: str, batch_uid: str, spu, activity, kind: str, ok: bool,
           status: str, detail: dict) -> dict:
    """组装一行记录（写入前各列截断，表名已过白名单）。"""
    return {
        "region": str(region or "")[:32],
        "batch_uid": str(batch_uid or "")[:32],
        "spu": str(spu or "")[:32],
        "activity": str(activity or "")[:128],
        "kind": str(kind or "")[:16],
        "ok": 1 if ok else 0,
        "status": str(status or "")[:32],
        "detail": json.dumps(detail or {}, ensure_ascii=False, default=str),
    }


async def record(entry: dict) -> None:
    """异步落一条历史；best-effort，绝不抛。连接参数不齐时直接返回，不开线程。"""
    cfg = load_config()
    if not cfg["enabled"]:
        return
    try:
        await asyncio.to_thread(_write, cfg, entry)
    except Exception as e:
        logger.warning(f"活动历史记录失败（忽略）：{e}")


async def query(spu: str = "", spus=None, activity: str = "", kind: str = "",
                limit: int = 200) -> dict:
    """供 HTTP 接口的查询：返回 {"enabled", "rows"}；未配置/异常如实标注。

    查询失败不吞成空结果——接口侧要把「历史查询暂不可用」如实传给页面，
    静默空结果会让操作者以为商品从没搞过活动（比没有功能更误导）。
    """
    cfg = load_config()
    if not cfg["enabled"]:
        return {"enabled": False, "rows": []}
    try:
        rows = await asyncio.to_thread(
            _query_rows, cfg, spu, spus, activity, kind, limit)
        for row in rows:
            # datetime 原件不可 JSON 序列化，接口输出只留转串后的 created_at。
            row.pop("_created_dt", None)
        return {"enabled": True, "rows": rows}
    except Exception as e:
        logger.warning(f"活动历史查询失败：{e}")
        return {"enabled": True, "rows": [], "error": str(e)[:200]}


def _summarize_rows(rows: list, db_now) -> dict:
    """把 id DESC（最新在前）的行归并成运行时注入用的摘要（纯函数，可单测）。

    每 (spu, 活动) 首条 enroll 即最新结论；每 spu 首条 ok=1 的 accel_open /
    accel_close 即最近一次成功开/关。within_24h 用 DB 时钟判。
    """
    out: dict = {}
    for row in rows:
        spu = str(row.get("spu") or "")
        if not spu:
            continue
        slot = out.setdefault(spu, {"activities": {}, "last_accel_open": None,
                                    "last_accel_close": None})
        kind = row.get("kind")
        at = str(row.get("created_at") or "")
        if kind == KIND_ENROLL:
            activity = str(row.get("activity") or "")
            if activity and activity not in slot["activities"]:
                detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
                slot["activities"][activity] = {
                    "status": row.get("status") or "",
                    "ok": bool(row.get("ok")),
                    "submit_price": detail.get("submit_price"),
                    "at": at,
                }
        elif kind == KIND_ACCEL_OPEN and row.get("ok") and slot["last_accel_open"] is None:
            within = False
            if db_now is not None and row.get("_created_dt") is not None:
                try:
                    within = (db_now - row["_created_dt"]).total_seconds() < 86400
                except Exception:
                    within = False
            slot["last_accel_open"] = {"at": at, "within_24h": within}
        elif kind == KIND_ACCEL_CLOSE and row.get("ok") and slot["last_accel_close"] is None:
            slot["last_accel_close"] = {"at": at}
    return out


async def summarize_for_display(spus) -> dict:
    """供列表页展示的批量摘要：返回 {"enabled", "map", "error"?}。

    与 summarize 的分工：summarize 是运行时注入路径，任何异常降级 {}（查不到 =
    页面上没有徽标，绝不阻塞主流程）；本函数服务人看的列表页，查询失败要如实
    回 error——静默空结果会让操作者以为这些商品从没搞过活动（比没有功能更误导，
    同 query() 的取向）。map 的归并口径与 summarize 同一份（_summarize_rows），
    列表看到的结论和管线运行时注入的决策依据一致。
    """
    spu_list = sorted({str(s).strip() for s in (spus or []) if str(s).strip()})
    cfg = load_config()
    if not cfg["enabled"]:
        return {"enabled": False, "map": {}}
    if not spu_list:
        return {"enabled": True, "map": {}}
    try:
        limit = min(_SUMMARY_ROW_CAP, _SUMMARY_PER_SPU * len(spu_list))

        def _load():
            # 与 summarize 同款：数据 + DB 时钟各一次短连接，within_24h 不受本机漂移影响。
            return _query_rows(cfg, spus=spu_list, limit=limit), _db_now(cfg)

        rows, db_now = await asyncio.to_thread(_load)
        return {"enabled": True, "map": _summarize_rows(rows, db_now)}
    except Exception as e:
        logger.warning(f"活动历史展示摘要查询失败：{e}")
        return {"enabled": True, "map": {}, "error": str(e)[:200]}


async def summarize(spus) -> dict:
    """运行时注入的唯一入口：批量一次查，返回 {spu: 摘要}；任何异常降级 {}。

    降级语义（拍板）：历史是辅助信息，查不到 = 页面上没有徽标，绝不阻塞
    扫描/报名主流程——与「偏好持久化坏了不影响主流程」的项目取向一致。
    """
    spu_list = [str(s).strip() for s in (spus or []) if str(s).strip()]
    if not spu_list:
        return {}
    cfg = load_config()
    if not cfg["enabled"]:
        return {}
    try:
        limit = min(_SUMMARY_ROW_CAP, _SUMMARY_PER_SPU * len(spu_list))

        def _load():
            # 同一批数据 + DB 时钟各一次短连接；时钟用于 within_24h（多机漂移不进判定）。
            return _query_rows(cfg, spus=spu_list, limit=limit), _db_now(cfg)

        rows, db_now = await asyncio.to_thread(_load)
        return _summarize_rows(rows, db_now)
    except Exception as e:
        logger.warning(f"活动历史摘要查询失败（降级为无历史）：{e}")
        return {}


def attach_history(on_progress: Optional[Callable], region: str = "") -> Callable:
    """切面：包装 on_progress 回调，把 live 批次的平台动作记入 MySQL。

    返回的新回调：原样转发事件给 on_progress（若存在），再按规则落库。on_progress
    为 None 时仍返回一个「只记录、不转发」的内部回调（同 error_report.attach 的取向，
    保证 CLI 入口不传回调时也记录）。

    包装时刻生成 batch_uid：run_activity_batch 每次调用都现包新回调，包装时刻 =
    批次时刻，同批所有记录共享一个 batch_uid 便于整批回溯。
    配置在包装时只读一次（cfg 闭包持有）：一批次几十条事件，逐条 load_config
    就是几十次配置源往返（DB 模式下各有一次 30s TTL 缓存兜着，但没必要）；连接
    参数改动最多等下一批次生效，与「单例段重启生效」的项目取向同级。

    记录规则（只记 live=True 批次；dry-run 无 exec_* 事件天然不记）：
    - exec_start：捕获批次级 live（异常路径的 exec_close/exec_reopen 事件缺 live
      字段，逐事件判会把异常记录丢掉）；
    - exec_fill：全量暂存 (spu,活动)→价格现场（失败/ineligible 格同样带
      submit_price/ref_price/over_ref，而终态 exec_log_verify 不带价格上下文；
      over_ref 现场是操作者改 Excel 日常价的依据）；
    - exec_log_verify：落 enroll（ok/status 取事件；detail 合并暂存 + enroll_id）；
    - exec_close / exec_reopen：各落一条（status 由纯函数派生）；
    - exec_cell_skip：忽略——它只在报名循环【走到该格】时发，活动中途 halt / 提报页
      开失败时排在后面的跳过格无任何事件；权威全集是 exec_done.skipped_cells（必发）；
    - exec_done：对 skipped_cells 逐格补落 skipped_by_user。
    """
    batch_uid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    cfg = load_config()
    state = {"live": None, "fills": {}, "skipped_written": set()}

    async def _record(entry: dict) -> None:
        # 用包装时读好的 cfg 直接写（不经过 record() 的逐条 load_config）；_write
        # 内部全吞异常，to_thread 这一层的异常由 _wrapped 的 try 兜底。
        await asyncio.to_thread(_write, cfg, entry)

    async def _handle(event: dict) -> None:
        t = event.get("type")
        if t == "exec_start":
            state["live"] = bool(event.get("live"))
            return
        if t == "exec_fill":
            spu = str(event.get("spu") or "")
            activity = str(event.get("activity") or "")
            if spu and activity:
                # 事件的 note 与对账事件的 note 含义不同，改名 fill_note 防合并时互相覆盖。
                state["fills"][(spu, activity)] = {
                    "submit_price": event.get("submit_price"),
                    "ref_price": event.get("ref_price"),
                    "over_ref": bool(event.get("over_ref")),
                    "ineligible": bool(event.get("ineligible")),
                    "sku_count": event.get("sku_count"),
                    "fill_note": _clip_note(event.get("note")),
                }
            return
        if not state["live"]:
            return  # 半程/未知批次：以下事件都不产生平台事实，不记
        if not cfg["enabled"]:
            return  # 连接参数不齐：暂存照做（无成本），不落库
        if t == "exec_log_verify":
            spu = str(event.get("spu") or "")
            activity = str(event.get("activity") or "")
            fill = state["fills"].get((spu, activity)) or {}
            detail = {
                "enroll_id": event.get("enroll_id"),
                "note": _clip_note(event.get("note")),
                **fill,
            }
            await _record(_entry(region, batch_uid, spu, activity, KIND_ENROLL,
                                bool(event.get("ok")), str(event.get("status") or ""),
                                detail))
            return
        if t == "exec_close":
            status = _close_status(event)
            detail = {
                "state": event.get("state"),
                "already_off": bool(event.get("already_off")),
                "cooldown": bool(event.get("cooldown")),
                "live": event.get("live"),
                "note": _clip_note(event.get("note")),
            }
            await _record(_entry(region, batch_uid, str(event.get("spu") or ""), "",
                                KIND_ACCEL_CLOSE,
                                status in ("closed", "already_off"), status, detail))
            return
        if t == "exec_reopen":
            status = _open_status(event)
            detail = {
                "tier": event.get("tier"),
                "throttled": bool(event.get("throttled")),
                "accel_prices": event.get("accel_prices"),
                "accel_price": event.get("accel_price"),
                "precheck_state": event.get("precheck_state"),
                "already_on": bool(event.get("already_on")),
                "live": event.get("live"),
                "note": _clip_note(event.get("note")),
            }
            await _record(_entry(region, batch_uid, str(event.get("spu") or ""), "",
                                KIND_ACCEL_OPEN,
                                status in ("opened", "already_on"), status, detail))
            return
        if t == "exec_done":
            for pair in event.get("skipped_cells") or []:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                spu, activity = str(pair[0]), str(pair[1])
                if (spu, activity) in state["skipped_written"]:
                    continue
                state["skipped_written"].add((spu, activity))
                await _record(_entry(region, batch_uid, spu, activity, KIND_ENROLL,
                                    False, "skipped_by_user",
                                    {"note": "执行中被操作者跳过（本格未提交）"}))
            return

    async def _wrapped(event: dict) -> None:
        if on_progress is not None:
            try:
                r = on_progress(event)
                if inspect.isawaitable(r):
                    await r
            except Exception as e:
                logger.warning(f"进度回调异常（忽略）：{e}")
        try:
            await _handle(event)
        except Exception as e:
            logger.warning(f"活动历史切面异常（忽略）：{e}")

    return _wrapped
