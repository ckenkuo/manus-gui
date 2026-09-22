"""配置中心：项目配置存 MySQL（整份 config.toml 文本存一行）。

为什么入库：多台机器（开发机/生产机）共用一套代码，但 config.toml 含密钥、
gitignored 不走 git，改一次配置要逐机手改，漏一台就是「多模态白名单跨机错配」
那种整批卡死事故（2026-09-11）。入库后一处 push、处处生效。

为什么整份文本存一行而不是键值表：配置里有大量注释（实测结论、踩坑记录），
键值化会丢注释；且 [[orders.sheet_map]] 这类嵌套数组拍平成键值又丑又脆。
整份文本读出后 tomllib.loads 得到与读文件完全一致的 dict，消费方零感知。

表结构刻意只一行（name='global'）：已拍板「纯全局一份」，不按机器分行；
updated_by 只记录最后是谁推的，方便排查「哪台机改了没推」。

本模块【不 import app.config】：它要被 app.config 反过来调用来加载配置，
循环 import 会炸。连接参数由调用方读好后以 cfg dict 传入（键：
host/port/user/password/database/table），来源见 app/config.py 的
read_config_store_section——本地 [config_store] 段优先，缺了复用 [error_report]
的连接参数（各机部署时本就配好了它，零额外配置）。

失败语义与 error_report 相反：配置是主流程依赖（用户已拍板「DB 挂了宁可不跑」），
一切失败都抛 ConfigStoreError，绝不静默降级——拿错/旧配置跑批次比不跑更糟。
"""
import re
import socket

# 表名要拼进 DDL/SQL，参数化不了，只放行字母数字下划线（同 error_report 的取向）。
_TABLE_RE = re.compile(r"^[A-Za-z0-9_]+$")

DEFAULT_TABLE = "app_config"
GLOBAL_NAME = "global"

_DDL = """CREATE TABLE IF NOT EXISTS `{table}` (
    name VARCHAR(64) NOT NULL,
    content MEDIUMTEXT,
    updated_by VARCHAR(64) NOT NULL DEFAULT '',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_UPSERT = """INSERT INTO `{table}` (name, content, updated_by)
    VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE content = VALUES(content),
    updated_by = VALUES(updated_by)"""


class ConfigStoreError(RuntimeError):
    """配置中心不可用/配置为空/配置无法解析。主流程配置错误，必须显式抛出。"""


def _validate_cfg(cfg: dict) -> tuple:
    """从 cfg dict 提取并校验连接参数，返回 (连接 kwargs, table)。

    table 过白名单后才允许拼进 SQL；host/database 为空说明段没配好，
    这种「配了一半」的状态要当场点破，不能留给 pymysql 报一个莫名其妙的目标错。
    """
    host = str(cfg.get("host") or "").strip()
    database = str(cfg.get("database") or "").strip()
    if not host or not database:
        raise ConfigStoreError(
            "配置中心连接的 host/database 为空：本地 config.toml 既没有 "
            "[config_store] 段，[error_report] 的连接参数也不全；两处都不配则"
            "自动退回文件模式（读本地 config.toml）")
    table = str(cfg.get("table") or DEFAULT_TABLE).strip()
    if not _TABLE_RE.match(table):
        raise ConfigStoreError(
            f"[config_store].table 含非法字符（只允许字母数字下划线）：{table!r}")
    kwargs = {
        "host": host,
        "port": int(cfg.get("port") or 3306),
        "user": str(cfg.get("user") or ""),
        "password": str(cfg.get("password") or ""),
        "database": database,
        "charset": "utf8mb4",
        "connect_timeout": 5,
        # 配置文本几十 KB，公网慢连接也绰绰有余；不设超时会在 DB 挂死时把
        # 启动流程一直挂住，违背「连不上就快速报错退出」的既定语义。
        "read_timeout": 15,
        "write_timeout": 15,
    }
    return kwargs, table


def _connect(kwargs: dict):
    """建立短连接；导入失败/连接失败统一包装成 ConfigStoreError（带出下一步怎么办）。"""
    try:
        import pymysql
    except ImportError as e:
        raise ConfigStoreError(
            "未安装 pymysql，配置存库模式不可用（pip install pymysql）") from e
    try:
        return pymysql.connect(**kwargs)
    except Exception as e:
        raise ConfigStoreError(
            f"配置中心连接失败（{kwargs['host']}:{kwargs['port']}/{kwargs['database']}）：{e}。"
            "应用拿不到配置、按既定策略直接退出；请检查 MySQL 可达性。要临时退回"
            "文件模式：设环境变量 MANUS_CONFIG_SOURCE=file（本机 config.toml 其余段"
            "仍在时才读得到配置），或摘掉 [config_store]/[error_report] 的连接配置") from e


def fetch(cfg: dict) -> str:
    """读 global 行的配置文本；连接失败/无行/内容为空一律抛 ConfigStoreError。"""
    kwargs, table = _validate_cfg(cfg)
    conn = _connect(kwargs)
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL.format(table=table))
            cur.execute(f"SELECT content FROM `{table}` WHERE name = %s", (GLOBAL_NAME,))
            row = cur.fetchone()
        conn.commit()
    except ConfigStoreError:
        raise
    except Exception as e:
        raise ConfigStoreError(
            f"配置中心读取失败（{kwargs['host']}/{kwargs['database']}.{table}）：{e}") from e
    finally:
        try:
            conn.close()
        except Exception:
            pass
    content = row[0] if row else None
    if not content or not str(content).strip():
        raise ConfigStoreError(
            f"配置中心表里还没有 {GLOBAL_NAME} 配置行（{kwargs['host']}/"
            f"{kwargs['database']}.{table}）：先在一台已配好的机器上执行 "
            "python -m app.config_sync push 把本地 config.toml 推上去")
    return str(content)


def push(cfg: dict, text: str, machine: str = "") -> None:
    """把整份配置文本 upsert 进 global 行；失败抛 ConfigStoreError。"""
    kwargs, table = _validate_cfg(cfg)
    conn = _connect(kwargs)
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL.format(table=table))
            cur.execute(
                _UPSERT.format(table=table),
                (GLOBAL_NAME, text, (machine or socket.gethostname())[:64]),
            )
        conn.commit()
    except Exception as e:
        raise ConfigStoreError(
            f"配置中心写入失败（{kwargs['host']}/{kwargs['database']}.{table}）：{e}") from e
    finally:
        try:
            conn.close()
        except Exception:
            pass
