"""管线错误集中上报到 MySQL（切面 + 注解式）。

为什么需要它：多台 PC 将同时跑采集 / 订单 / 活动 / 发布四条管线，错误只落各自
本地 loguru 日志、无法跨机器汇总。本模块把「主流程失败」（商品失败 / 批次中止 /
批次异常退出）结构化写入一个公网 MySQL，方便按 pipeline / stage / item / machine
聚合定位、针对性改代码。

为什么做成「切面 + 注解」两层，而不是给 loguru 加 MySQL sink：
- 管线失败大部分不抛异常，而是「正常返回 fail 状态」再经 on_progress 事件发出，
  logger 里根本没有（三条管线 logger.exception 全为 0，异常多被 logger.warning 吞）。
  装饰器（注解）只能捕获「抛异常」，抓不到这类失败。
- on_progress 事件流是所有失败的统一出口，切面（attach）挂在上面一次即可全覆盖；
  record_exception 装饰器补「抛异常」场景，未来给函数加错误记录 = 加一个注解。
- 全项目 logger.error/exception 126 处散布 26 文件、噪音大且无结构字段，不适合 sink。

best-effort：未配置 / 未装 pymysql / 连接失败 / 插入失败一律 logger.warning 吞掉，
绝不中断主流程（同 app/publish/alert.py 的飞书告警取向）。写库用 pymysql 短连接 +
asyncio.to_thread 下沉线程，不堵事件循环。

配置在 config/config.toml 的 [error_report] 段（gitignored，含密码不进版本库）：

    [error_report]
    enabled = false
    host = "127.0.0.1"
    port = 3306
    user = "manus"
    password = ""
    database = "manus"
    table = "pipeline_errors"
    instance = ""          # 可选，同一台机器跑多实例/多账号时用于区分
"""
import asyncio
import functools
import inspect
import re
import socket
import traceback as _traceback
from typing import Awaitable, Callable, Optional, Union

from app.config import config_search_dirs
from app.logger import logger

# 默认表名；machine 用 hostname，多台 PC 靠它区分。
DEFAULT_TABLE = "pipeline_errors"
MACHINE = socket.gethostname()

# 表名白名单：表名要拼进 DDL/INSERT，参数化不了，只放行字母数字下划线防注入/拼错。
_TABLE_RE = re.compile(r"^[A-Za-z0-9_]+$")

# 字段长度上限（TEXT 上限 64KB，留足余量）；截断避免超长内容撑爆写入。
_MESSAGE_MAX = 2000
_TRACEBACK_MAX = 8000

_DDL = """CREATE TABLE IF NOT EXISTS `{table}` (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    machine VARCHAR(64) NOT NULL,
    instance VARCHAR(64) NOT NULL DEFAULT '',
    pipeline VARCHAR(32) NOT NULL,
    stage VARCHAR(64) NOT NULL DEFAULT '',
    item VARCHAR(128) NOT NULL DEFAULT '',
    level VARCHAR(16) NOT NULL DEFAULT 'error',
    message TEXT,
    traceback TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_pipeline_time (pipeline, created_at),
    KEY idx_item (item)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_INSERT = """INSERT INTO `{table}`
    (machine, instance, pipeline, stage, item, level, message, traceback)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""


def load_config() -> dict:
    """读 config.toml 的 [error_report] 段，返回连接/开关参数；读不到返回禁用态。

    只读 config.toml、不回退 config.example.toml（example 里是空占位，读来无意义）。
    best-effort：读不到 / 解析失败一律 enabled=False，绝不影响主流程（照抄
    app/publish/alert.py 的 load_alert_config）。enabled 默认 false，需显式置 true
    且 host/database 非空才启用——避免没配好就连公网 MySQL 刷 warning。
    """
    result = {
        "enabled": False, "host": "", "port": 3306, "user": "", "password": "",
        "database": "", "table": DEFAULT_TABLE, "instance": "",
    }
    try:
        import tomllib

        for d in config_search_dirs():
            p = d / "config.toml"
            if not p.exists():
                continue
            with open(p, "rb") as f:
                section = tomllib.load(f).get("error_report") or {}
            if not section:
                continue
            result["host"] = str(section.get("host") or "").strip()
            result["port"] = int(section.get("port") or 3306)
            result["user"] = str(section.get("user") or "")
            result["password"] = str(section.get("password") or "")
            result["database"] = str(section.get("database") or "").strip()
            result["instance"] = str(section.get("instance") or "").strip()
            table = str(section.get("table") or DEFAULT_TABLE).strip()
            result["table"] = table if _TABLE_RE.match(table) else DEFAULT_TABLE
            result["enabled"] = (
                bool(section.get("enabled", False))
                and bool(result["host"]) and bool(result["database"])
            )
            break
    except Exception as e:
        logger.warning(f"读取 [error_report] 配置失败，本次不上报：{e}")
        result["enabled"] = False
    return result


def _write(cfg: dict, entry: dict) -> None:
    """同步写一条记录（在线程里跑）：connect → 建表 → insert → close。

    延迟导入 pymysql：未安装时 ImportError 吞掉，不因缺依赖中断主流程。连接用短连接，
    错误是低频事件，没必要维护连接池；best-effort，任何一步失败只 logger.warning。
    """
    try:
        import pymysql
    except ImportError:
        logger.warning("未安装 pymysql，错误上报跳过（pip install pymysql）")
        return

    conn = None
    try:
        conn = pymysql.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"],
            password=cfg["password"], database=cfg["database"],
            charset="utf8mb4", connect_timeout=5,
        )
        with conn.cursor() as cur:
            cur.execute(_DDL.format(table=cfg["table"]))
            cur.execute(
                _INSERT.format(table=cfg["table"]),
                (
                    MACHINE, cfg["instance"], entry["pipeline"], entry["stage"],
                    entry["item"], entry["level"], entry["message"], entry["traceback"],
                ),
            )
        conn.commit()
    except Exception as e:
        logger.warning(f"错误上报写库失败（忽略）：{e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


async def report(pipeline: str, level: str = "error", stage: str = "",
                 item: str = "", message: str = "", traceback: str = "") -> None:
    """异步上报一条错误；best-effort，绝不抛。

    走 asyncio.to_thread 下沉线程（pymysql 同步，不堵事件循环）。未启用时直接返回，
    不开线程。
    """
    cfg = load_config()
    if not cfg["enabled"]:
        return
    entry = {
        "pipeline": str(pipeline or "")[:32],
        "level": str(level or "error")[:16],
        "stage": str(stage or "")[:64],
        "item": str(item or "")[:128],
        "message": str(message or "")[:_MESSAGE_MAX],
        "traceback": str(traceback or "")[:_TRACEBACK_MAX],
    }
    try:
        await asyncio.to_thread(_write, cfg, entry)
    except Exception as e:
        logger.warning(f"错误上报失败（忽略）：{e}")


def _is_failure(event: dict) -> bool:
    """判断一个 on_progress 事件是否属于「需要上报的失败」。

    只报真失败三类，跳过 empty / unmapped / unpriced / skip_* 等正常业务结果——
    那些是正常跳过，不是需要改代码的 bug。
    """
    t = event.get("type")
    if t == "aborted":
        return True
    if t == "write_failed":
        return True
    if t == "product_done" and event.get("status") == "fail":
        return True
    return False


def _extract(event: dict) -> dict:
    """从失败事件抽取结构化字段（stage / item / message），供上报。"""
    t = event.get("type")
    stage = str(event.get("failed_stage") or event.get("stage") or "")
    item = str(event.get("spu") or event.get("offer_id")
               or event.get("order_no") or event.get("rowid") or "")
    if t == "aborted":
        message = str(event.get("reason") or "")
    elif t == "write_failed":
        message = str(event.get("note") or event.get("message") or "")
    else:  # product_done status=fail
        message = str(event.get("note") or "")
    return {"stage": stage, "item": item, "message": message}


def attach(on_progress: Optional[Callable], pipeline: str) -> Callable:
    """切面：包装 on_progress 回调，失败事件自动上报 MySQL。

    返回的新回调：原样转发事件给 on_progress（若存在），再对失败事件上报。on_progress
    为 None 时仍返回一个「只上报、不转发」的内部回调——保证 CLI 入口（batch_collect.py
    等）不传回调时失败也能上报（_emit 对 None 直接 return，包装后恒非 None）。
    上报 best-effort，失败只告警。
    """
    async def _wrapped(event: dict) -> None:
        if on_progress is not None:
            try:
                r = on_progress(event)
                if inspect.isawaitable(r):
                    await r
            except Exception as e:
                logger.warning(f"进度回调异常（忽略）：{e}")
        if _is_failure(event):
            f = _extract(event)
            await report(pipeline, level="error", stage=f["stage"],
                         item=f["item"], message=f["message"])

    return _wrapped


def record_exception(pipeline: str, stage: str = ""):
    """装饰器：async 函数抛异常时自动上报（带 traceback）再原样 re-raise。

    用于「抛异常」场景（批次崩溃兜底、LLM 调用等）；未来给任意函数加错误记录，
    加一个注解即可。绝不吞异常、不改主流程控制流。仅支持 async 函数。
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                await report(pipeline, level="error", stage=stage, item="",
                             message=str(e), traceback=_traceback.format_exc())
                raise

        return wrapper

    return deco
