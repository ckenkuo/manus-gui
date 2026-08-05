# -*- coding: utf-8 -*-
"""kdocs-cli 的通用调用封装。

为什么不复用 app/orders/kdocs_sheet._run_once：那个方法只认两种信封
（{"code":0,"data":{...}} 和 {"result":"ok","detail":{...}}），而 merge_range /
range_sort / auto_fit 这类「数据操作」走的是【脚本执行通道】，响应形如

    {"code":0,"data":{"data":{"logs":[...],"result":"[Undefined]"},
                      "error":"","status":"finished"}}

被原解包逻辑当成失败抛错（2026-08-05 实测踩到）。这里按 status/error 判成功，
并把脚本日志里的 error/warn 级别当失败——脚本通道 HTTP 200 也可能内部报错，
不看日志会把失败当成功。

另一个踩过的坑：参数临时 JSON 必须【不带 BOM】。PowerShell 5.1 的
`Out-File -Encoding utf8` 会写 BOM，kdocs-cli 直接判 "not valid JSON"。这里统一
用 json.dump 写 UTF-8 无 BOM。
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from app.logger import logger

# CLI 单次调用超时（秒）：大表整列读、批量写都可能偏慢。
DEFAULT_TIMEOUT = 180
# 限频（429001）退避秒数；响应不带恢复时间时按这个等。
_RATE_LIMIT_WAIT = 20


class KdocsCliError(Exception):
    """kdocs-cli 调用失败（进程错误 / 业务错误码 / 脚本内部报错 / 响应不可解析）。"""


def resolve_cli(cli: str = "kdocs-cli") -> str:
    """定位 kdocs-cli 可执行文件。

    不能只看 PATH：Web 服务由 启动.bat/launcher.ps1 拉起，继承的是开机环境，
    没有交互 shell 里加的 ~/.local/bin。找不到时回退到安装脚本默认目录，
    仍找不到则原样返回（让 FileNotFoundError 给出可读报错）。
    """
    if shutil.which(cli):
        return cli
    for candidate in (
        Path.home() / ".local" / "bin" / cli,
        Path.home() / ".local" / "bin" / f"{cli}.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return cli


def _unwrap(env: Any, label: str) -> Any:
    """把响应信封解到真正承载业务数据那一层。"""
    if isinstance(env, dict) and env.get("code") not in (None, 0):
        raise KdocsCliError(f"{label} 业务错误 code={env.get('code')}: {env.get('msg')}")

    data = env.get("data", env) if isinstance(env, dict) else env

    # 脚本执行通道：status 必须 finished 且 error 为空，再查日志里的 error/warn
    if isinstance(data, dict) and "status" in data:
        if data.get("status") != "finished" or data.get("error"):
            raise KdocsCliError(
                f"{label} 脚本执行失败：{data.get('error') or str(data)[:300]}"
            )
        inner = data.get("data")
        if isinstance(inner, dict):
            bad = [l for l in (inner.get("logs") or [])
                   if str(l.get("level")) in ("error", "warn")]
            if bad:
                raise KdocsCliError(f"{label} 脚本日志报错：{str(bad)[:300]}")
            return inner.get("result")
        return None

    # 普通通道：逐层往 data/detail 里钻
    while isinstance(data, dict):
        inner: Optional[Any] = None
        for key in ("data", "detail"):
            if isinstance(data.get(key), (dict, list)):
                inner = data[key]
                break
        if inner is None:
            break
        data = inner
    return data


def _call_once(cli: str, service: str, action: str, payload: dict, timeout: int) -> Any:
    label = f"kdocs-cli {service} {action}"
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="kdocs_", delete=False, encoding="utf-8"
    )
    try:
        with tmp:
            json.dump(payload, tmp, ensure_ascii=False)
        cmd = [cli, service, action, "--file", tmp.name,
               "--timeout", str(timeout * 1000), "--output", "json"]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout + 30,
        )
    except subprocess.TimeoutExpired as e:
        raise KdocsCliError(f"{label} 超时：{e}") from e
    except FileNotFoundError as e:
        raise KdocsCliError(f"找不到 kdocs-cli（{cli}），请先安装并认证") from e
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise KdocsCliError(
            f"{label} 退出码 {proc.returncode}：{(proc.stderr or out)[:500]}"
        )
    try:
        env = json.loads(out)
    except ValueError as e:
        raise KdocsCliError(f"{label} 输出不是 JSON：{out[:300]}") from e
    return _unwrap(env, label)


def call(service: str, action: str, payload: dict, cli: Optional[str] = None,
         timeout: int = DEFAULT_TIMEOUT, retry_5xx: bool = False) -> Any:
    """调一次 kdocs-cli 并返回解包后的数据。

    429001（限频）等 _RATE_LIMIT_WAIT 秒重试一次；retry_5xx=True 时 HTTP 5xx 等 3s
    重试一次——只能开给【幂等】操作（读、同值写），insert_rows_cols 这类非幂等的
    禁止开，重试会重复插行。429002（熔断）直接抛。
    """
    cli = cli or resolve_cli()
    try:
        return _call_once(cli, service, action, payload, timeout)
    except KdocsCliError as e:
        msg = str(e)
        if "429002" in msg:
            raise
        if "429001" in msg:
            logger.warning(f"kdocs-cli 限频，{_RATE_LIMIT_WAIT}s 后重试一次：{msg[:200]}")
            time.sleep(_RATE_LIMIT_WAIT)
            return _call_once(cli, service, action, payload, timeout)
        if retry_5xx and "HTTP 5" in msg:
            logger.warning(f"kdocs-cli 5xx，3s 后重试一次：{msg[:200]}")
            time.sleep(3)
            return _call_once(cli, service, action, payload, timeout)
        raise
