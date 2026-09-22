"""配置中心同步工具：本地 config.toml ↔ MySQL 全局配置行。

用法：
    python -m app.config_sync push [--path 文件] [--trim]   本地 → 推送入库
    python -m app.config_sync pull [--write]                库 → 打印（--write 写回本地）
    python -m app.config_sync diff [--path 文件]            本地 vs 库 的差异

为什么文件顶部先钉死 MANUS_CONFIG_SOURCE=file 再 import app.config：本工具是
「配置坏了时的修理工具」，自身绝不能因为 DB 挂了、或库里的配置文本解析失败而
起不来（import app.config 会触发 Config 单例加载，DB 模式下那会直连 MySQL）。
push/pull/diff 都只需要本地文件 + config_store 直连，不经过统一源。
"""
import argparse
import difflib
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

os.environ.setdefault("MANUS_CONFIG_SOURCE", "file")

from app import config_store
from app.config import config_search_dirs, read_config_store_section
from app.config_store import ConfigStoreError

# 推送后精简本地文件时的文件头注释：点破「其余段运行时一律从库里读」，
# 免得有人看到光秃秃的文件以为配置丢了、或在本地改了其他段纳闷不生效。
_TRIM_HEADER = """# 本机配置已迁入 MySQL 配置中心，本文件只保留连接段 [config_store]。
# 它是「库在哪」的自举信息，必须留在本地；其余所有段运行时一律从库里读，
# 在这里增删改任何其他段都【不会生效】。
# 改配置：python -m app.config_sync pull --write 拉下来改，改完 push（可再 --trim）。
# 机制说明见 config/config.example.toml 顶部。
"""


def _default_config_path():
    """默认操作的本地 config.toml：取搜索链上第一个已存在的；都不存在返回可写侧路径。"""
    for d in config_search_dirs():
        p = d / "config.toml"
        if p.exists():
            return p
    return config_search_dirs()[0] / "config.toml"


def _read_local_text(path) -> str:
    if not path.exists():
        raise ConfigStoreError(f"本地配置文件不存在：{path}")
    return path.read_text(encoding="utf-8")


def _fetch_db_text() -> str:
    return config_store.fetch(read_config_store_section())


def _bootstrap_section(parsed: dict) -> dict:
    """从解析后的配置提取自举连接段：[config_store] 优先，否则 [error_report] 的连接参数。

    与 app.config.read_config_store_section 的回退口径一致。--trim 精简本地文件时
    统一落成显式 [config_store] 段——之后这台机器的自举不再依赖 error_report 段在不在。
    """
    section = parsed.get("config_store") or {}
    if section:
        return section
    er = parsed.get("error_report") or {}
    if str(er.get("host") or "").strip() and str(er.get("database") or "").strip():
        return {
            "host": er.get("host"),
            "port": er.get("port") or 3306,
            "user": er.get("user") or "",
            "password": er.get("password") or "",
            "database": er.get("database"),
        }
    return {}


def _cmd_push(args) -> int:
    path = args.path or _default_config_path()
    text = _read_local_text(path)
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        print(f"拒绝推送：{path} 有 TOML 语法错误（{e}），修正后再推", file=sys.stderr)
        return 1
    section = _bootstrap_section(parsed)
    config_store.push(read_config_store_section(), text)
    print(f"已推送 {len(text.encode('utf-8'))} 字节到配置中心（global 行）。")
    if args.trim:
        if not section:
            print("--trim 跳过：本地文件既没有 [config_store] 段，[error_report] 的"
                  "连接参数也不全，精简后无法自举", file=sys.stderr)
            return 1
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        lines = [_TRIM_HEADER, "[config_store]\n"]
        for k, v in section.items():
            # 连接段是扁平的 str/int，json.dumps 的字符串转义与 TOML 基本串兼容
            if isinstance(v, str):
                lines.append(f"{k} = {json.dumps(v, ensure_ascii=False)}\n")
            else:
                lines.append(f"{k} = {v}\n")
        path.write_text("".join(lines), encoding="utf-8")
        print(f"本地 {path} 已精简为仅 [config_store]（原文件备份在 {backup.name}）。")
    else:
        print("提示：push --trim 可把本地文件精简为仅连接段，消除「改了本地副本不生效」的困惑。")
    return 0


def _cmd_pull(args) -> int:
    text = _fetch_db_text()
    if not args.write:
        print(text)
        return 0
    path = args.path or _default_config_path()
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        print(f"原 {path} 已备份为 {backup.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"已写回 {path}（{len(text.encode('utf-8'))} 字节）。")
    return 0


def _cmd_diff(args) -> int:
    path = args.path or _default_config_path()
    local = _read_local_text(path)
    remote = _fetch_db_text()
    diff = list(difflib.unified_diff(
        local.splitlines(), remote.splitlines(),
        fromfile=f"本地 {path.name}", tofile="配置中心 global", lineterm="",
    ))
    if not diff:
        print("一致：本地与配置中心内容相同。")
    else:
        print("\n".join(diff))
    return 0


def main() -> int:
    # Windows 控制台默认 GBK，配置里有大量中文注释，不钉 UTF-8 会乱码/抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        prog="python -m app.config_sync",
        description="配置中心同步：本地 config.toml 与 MySQL 全局配置行互传")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_push = sub.add_parser("push", help="推送本地配置到配置中心")
    p_push.add_argument("--path", type=Path,
                        default=None, help="本地配置文件（默认取搜索链上的 config.toml）")
    p_push.add_argument("--trim", action="store_true",
                        help="推送成功后把本地文件精简为仅 [config_store] 段")
    p_pull = sub.add_parser("pull", help="从配置中心拉取（默认打印，--write 写回本地）")
    p_pull.add_argument("--write", action="store_true", help="写回本地 config.toml")
    p_pull.add_argument("--path", type=Path,
                        default=None, help="--write 的目标文件（默认 config.toml）")
    p_diff = sub.add_parser("diff", help="比较本地与配置中心的差异")
    p_diff.add_argument("--path", type=Path,
                        default=None, help="本地配置文件（默认 config.toml）")
    args = parser.parse_args()
    handler = {"push": _cmd_push, "pull": _cmd_pull, "diff": _cmd_diff}[args.cmd]
    try:
        return handler(args)
    except ConfigStoreError as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
