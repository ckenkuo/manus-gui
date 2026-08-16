import asyncio
import os
import shutil
import sys
from typing import Dict

from app.tool.base import BaseTool


def _resolve_interpreter():
    """定位一个真正的 Python 解释器，返回可执行文件路径；找不到返回 None。

    不能直接用 sys.executable：冻结后它指向 manus-*.exe 自身，而 PyInstaller
    的 bootloader 不认 -X/-c，会把这些参数当成业务 argv 把整个应用重新拉起
    ——Web 入口会二次绑定端口、再开一次浏览器、再建一份 agent。
    开发态 sys.executable 就是解释器，直接用；冻结态退回系统 PATH 上的 python。
    """
    if not getattr(sys, "frozen", False):
        return sys.executable

    override = os.environ.get("MANUS_PYTHON")
    if override and os.path.exists(override):
        return override

    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            return found

    return None


class PythonExecute(BaseTool):
    """用于执行 Python 代码的工具，具有超时和安全限制。"""

    name: str = "python_execute"
    description: str = "执行 Python 代码字符串。注意：只有 print 输出可见，函数返回值不会被捕获。使用 print 语句查看结果。"
    parameters: dict = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要执行的 Python 代码。",
            },
        },
        "required": ["code"],
    }

    async def execute(
        self,
        code: str,
        timeout: int = 30,
    ) -> Dict:
        """
        在一个干净的子进程中执行提供的 Python 代码。

        使用 `python -c` 启动一个独立解释器，而不是 multiprocessing.Process。
        后者在 Windows（spawn 启动方式）上会重新 import 整个 app 包
        （连带 browser_use、浏览器初始化等重型依赖），仅启动就远超超时时间，
        导致代码尚未运行就被判定为超时。

        Args:
            code (str): 要执行的 Python 代码。
            timeout (int): 执行超时时间（秒）。

        Returns:
            Dict: 包含执行输出的 'observation' 和 'success' 状态。
        """
        # 强制子进程的 stdout/stderr 使用 UTF-8，避免中文 Windows 上的编码错误。
        # 用 -X utf8（UTF-8 模式）而非依赖 PYTHONIOENCODING，更可靠。
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

        interpreter = _resolve_interpreter()
        if interpreter is None:
            return {
                "observation": (
                    "未找到可用的 Python 解释器，无法执行代码。\n"
                    "打包版不自带解释器，请在本机安装 Python 后重试"
                    "（或设置环境变量 MANUS_PYTHON 指向 python.exe）。"
                ),
                "success": False,
            }

        try:
            process = await asyncio.create_subprocess_exec(
                interpreter,
                "-X",
                "utf8",
                "-c",
                code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except Exception as e:
            return {"observation": f"Failed to start interpreter: {e}", "success": False}

        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            return {
                "observation": f"Execution timeout after {timeout} seconds",
                "success": False,
            }

        output = stdout.decode(errors="replace")
        return {
            "observation": output,
            "success": process.returncode == 0,
        }
