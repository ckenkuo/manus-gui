# -*- coding: utf-8 -*-
"""一次性迁移：给项目四张 MySQL 表的【现有部署】补列注释/表注释（2026-09-30）。

为什么要单独跑它：三处建表 DDL（app/error_report.py、app/config_store.py、
app/activity/history.py）都是 CREATE TABLE IF NOT EXISTS——表已存在的机器上
新加的 COMMENT 不会生效，只能 ALTER TABLE ... MODIFY COLUMN 补。每条 MODIFY
的列定义与 DDL 逐字一致、只多 COMMENT，幂等、可重复执行，不动任何数据。

连接参数完全走项目自己的配置读取（error_report / activity_history / config_store
三个来源），脚本本身不含密钥、也不回显密钥。跑完查 information_schema 回显
每列注释做验证。

用法：python _add_db_comments.py
"""
import sys
from collections import OrderedDict

import pymysql

from app import error_report
from app.activity import history
from app.config import read_config_store_section
from app.logger import logger


# ---- 四张表的（列定义, 注释）清单 ------------------------------------------------
# 列定义必须与 DDL 逐字一致：MODIFY COLUMN 会整体替换列定义，写岔了等于改表结构。

PIPELINE_ERRORS = OrderedDict([
    ("id", ("BIGINT UNSIGNED NOT NULL AUTO_INCREMENT", "自增主键")),
    ("machine", ("VARCHAR(64) NOT NULL", "上报机器 hostname（多台 PC 靠它区分）")),
    ("instance", ("VARCHAR(64) NOT NULL DEFAULT ''", "实例标识（[error_report].instance）：同机多实例/多账号时区分，空=只按机器区分")),
    ("pipeline", ("VARCHAR(32) NOT NULL", "管线标识（attach 切面传入：collect/publish/activity 等）")),
    ("stage", ("VARCHAR(64) NOT NULL DEFAULT ''", "管线内阶段（如发布管线的阶段号），可空")),
    ("item", ("VARCHAR(128) NOT NULL DEFAULT ''", "业务对象标识（SPU/货号等），可空")),
    ("level", ("VARCHAR(16) NOT NULL DEFAULT 'error'", "级别（error/warning），默认 error")),
    ("message", ("TEXT", "错误摘要（写入前截断 2000 字符）")),
    ("traceback", ("TEXT", "异常堆栈（写入前截断 8000 字符）")),
    ("created_at", ("DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP", "落库时间")),
])
PIPELINE_ERRORS_COMMENT = "管线错误集中上报主表（各机各管线共享，best-effort 写入）"

PIPELINE_ERRORS_SNAP = OrderedDict([
    ("id", ("BIGINT UNSIGNED NOT NULL AUTO_INCREMENT", "自增主键")),
    ("error_id", ("BIGINT UNSIGNED NOT NULL", "关联主表 id（同一连接先插主表拿 lastrowid，顺序不能反）")),
    ("machine", ("VARCHAR(64) NOT NULL", "上报机器 hostname（冗余主表字段，明细可独立检索）")),
    ("instance", ("VARCHAR(64) NOT NULL DEFAULT ''", "实例标识（冗余主表字段）")),
    ("pipeline", ("VARCHAR(32) NOT NULL", "管线标识（冗余主表字段）")),
    ("item", ("VARCHAR(128) NOT NULL DEFAULT ''", "业务对象标识（冗余主表字段）")),
    ("stage", ("VARCHAR(64) NOT NULL DEFAULT ''", "管线内阶段（冗余主表字段）")),
    ("exit_tag", ("VARCHAR(8) NOT NULL DEFAULT ''", "失败退出点标签（发布管线的失败出口分类字母 A/B/C/D/E/F）")),
    ("snapshot", ("MEDIUMTEXT", "失败现场 JSON（页面 URL/任务状态等快照）")),
    ("shot_png", ("MEDIUMBLOB", "失败页 PNG 截图二进制")),
    ("shot_bytes", ("INT UNSIGNED NOT NULL DEFAULT 0", "shot_png 字节数；几 KB 即空白页/黑帧，免取 BLOB 一眼认出")),
    ("created_at", ("DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP", "落库时间")),
])
PIPELINE_ERRORS_SNAP_COMMENT = "失败现场明细表（主表之外的快照+截图；另起表是因主表 CREATE IF NOT EXISTS 加列不生效）"

APP_CONFIG = OrderedDict([
    ("name", ("VARCHAR(64) NOT NULL", "配置名；当前固定 global——整份配置文本存一行")),
    ("content", ("MEDIUMTEXT", "配置原文（TOML 全文）")),
    ("updated_by", ("VARCHAR(64) NOT NULL DEFAULT ''", "最后写入者标识（config_sync push 的机器名）")),
    ("updated_at", ("DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP", "最后更新时间（ON UPDATE 自动维护）")),
])
APP_CONFIG_COMMENT = "配置中心：整份 config.toml 文本存一行（name=global），各机启动时拉取"

ACTIVITY_HISTORY = OrderedDict([
    ("id", ("BIGINT UNSIGNED NOT NULL AUTO_INCREMENT", "自增主键")),
    ("machine", ("VARCHAR(64) NOT NULL", "执行机器 hostname")),
    ("instance", ("VARCHAR(64) NOT NULL DEFAULT ''", "实例标识（[activity_history].instance）：同机多实例/多账号时区分")),
    ("region", ("VARCHAR(32) NOT NULL DEFAULT ''", "Temu 区域标签（UI 选定的 全球/美国 等；切区域=换域名）")),
    ("batch_uid", ("VARCHAR(32) NOT NULL DEFAULT ''", "批次 UID：一次执行遍的唯一标识，汇总/对账按它归组")),
    ("spu", ("VARCHAR(32) NOT NULL DEFAULT ''", "商品 SPU")),
    ("activity", ("VARCHAR(128) NOT NULL DEFAULT ''", "活动名；仅 enroll 行有，加速器开关行（SPU 粒度）留空")),
    ("kind", ("VARCHAR(16) NOT NULL", "事件类型：enroll=报名结论 / accel_close=关流量 / accel_open=开流量")),
    ("ok", ("TINYINT(1) NOT NULL DEFAULT 0", "期望终态是否确认：enroll=报名成功，close=流量已关，open=流量已开")),
    ("status", ("VARCHAR(32) NOT NULL DEFAULT ''", "结果状态细分：enroll 取事件结论（done/skip/fail/info 等）；accel 由纯函数派生（closed/already_off/cooldown/opened/throttled/rejected 等）")),
    ("detail", ("MEDIUMTEXT", "事件明细 JSON（易变字段全塞这里：表结构首版定稿、永不加列）")),
    ("created_at", ("DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP", "落库时间")),
])
ACTIVITY_HISTORY_COMMENT = "活动报名历史：只记 live 批次的平台动作（切面写入，best-effort）"


def _connect_kwargs(cfg: dict) -> dict:
    """从 load_config 风格的 cfg 提取 pymysql 连接参数（复用项目读法）。"""
    return {
        "host": cfg["host"], "port": int(cfg.get("port") or 3306),
        "user": cfg.get("user") or "", "password": cfg.get("password") or "",
        "database": cfg["database"], "charset": "utf8mb4",
        "connect_timeout": 5, "read_timeout": 15, "write_timeout": 15,
    }


def _collect_targets() -> list:
    """汇总三组配置源的迁移目标，按 (连接, 表) 去重；返回 [(kwargs, table, columns, table_comment)]。"""
    targets = []
    seen = set()

    def add(cfg: dict, table: str, columns: OrderedDict, table_comment: str) -> None:
        if not str(cfg.get("host") or "").strip() or not str(cfg.get("database") or "").strip():
            logger.warning(f"配置源的 host/database 不全，跳过表 {table}")
            return
        kwargs = _connect_kwargs(cfg)
        key = (kwargs["host"], kwargs["port"], kwargs["database"], table)
        if key in seen:
            return
        seen.add(key)
        targets.append((kwargs, table, columns, table_comment))

    er = error_report.load_config()
    add(er, er.get("table") or error_report.DEFAULT_TABLE,
        PIPELINE_ERRORS, PIPELINE_ERRORS_COMMENT)
    add(er, er.get("detail_table") or f"{error_report.DEFAULT_TABLE}_snapshots",
        PIPELINE_ERRORS_SNAP, PIPELINE_ERRORS_SNAP_COMMENT)

    ah = history.load_config()
    add(ah, ah.get("table") or history.DEFAULT_TABLE,
        ACTIVITY_HISTORY, ACTIVITY_HISTORY_COMMENT)

    cs = read_config_store_section()
    add(cs, str(cs.get("table") or "").strip() or "app_config",
        APP_CONFIG, APP_CONFIG_COMMENT)
    return targets


def _apply(conn, database: str, table: str, columns: OrderedDict, table_comment: str) -> bool:
    """对一张表执行注释迁移；表不存在返回 False（该机还没建过这张表，不算失败）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.TABLES"
            " WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (database, table))
        if cur.fetchone()[0] == 0:
            return False
        ops = [f"MODIFY COLUMN `{col}` {definition} COMMENT '{comment}'"
               for col, (definition, comment) in columns.items()]
        cur.execute(f"ALTER TABLE `{table}` " + ", ".join(ops)
                    + f", COMMENT='{table_comment}'")
    conn.commit()
    return True


def _verify(conn, database: str, table: str) -> None:
    """回显一张表的列注释（information_schema 视角），供人工核对。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME, COLUMN_COMMENT FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
            (database, table))
        for name, comment in cur.fetchall():
            print(f"    {name:<12} {comment or '（空）'}")
        cur.execute(
            "SELECT TABLE_COMMENT FROM information_schema.TABLES"
            " WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (database, table))
        print(f"    [表注释] {cur.fetchone()[0]}")


def main() -> int:
    targets = _collect_targets()
    if not targets:
        print("三个配置源都没读到连接参数，无可迁移目标（检查本机 config.toml）")
        return 1
    for kwargs, table, columns, table_comment in targets:
        # 只回显 host/database/表名，绝不打印账号密码
        print(f"\n== {kwargs['host']}:{kwargs['port']}/{kwargs['database']}.{table}")
        try:
            conn = pymysql.connect(**kwargs)
        except Exception as e:
            print(f"  连接失败，跳过：{e}")
            continue
        try:
            if _apply(conn, kwargs["database"], table, columns, table_comment):
                print("  已迁移，当前注释：")
                _verify(conn, kwargs["database"], table)
            else:
                print("  表不存在（该机还没建过），跳过")
        except Exception as e:
            print(f"  迁移失败：{e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
