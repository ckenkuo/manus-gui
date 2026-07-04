import asyncio
import os
import sys
from typing import Dict

from app.tool.base import BaseTool


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

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
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
