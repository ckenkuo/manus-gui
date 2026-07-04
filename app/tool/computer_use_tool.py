# -*- coding: utf-8 -*-
"""系统桌面级 computer use 工具。

与 browser_use 的 gui_action 同源：都复用 app.tool.gui_agent 里的视觉「大脑」
（query_gui_action：给截图+子目标，返回一个像素坐标的原子操作），区别只在于
「手」——browser_use 把坐标落到 Playwright 的 page.mouse（浏览器视口内），本工具
把坐标落到 PyAutoGUI（整块屏幕，操作系统级）。

定位：补上浏览器工具够不到的场景——系统原生文件对话框、WPS/Office 桌面端、
资源管理器、安装程序、任何原生窗口。当目标不在浏览器页面里时用本工具。

安全护栏（按用户选定）：
- PyAutoGUI FAILSAFE：把鼠标猛甩到屏幕左上角可随时急停整个循环。
- 单次 task 的原子操作步数上限 _MAX_ITERATIONS，防止视觉循环失控。
- 关键操作前 ask_human 人工确认：每个 task 首次真实操作前确认一次；破坏性
  KEY_PRESS（alt+f4、delete 等）每次单独确认。

坐标契约（沿用浏览器工具那套，解决 Win10 DPI 缩放）：
视觉模型返回【相对截图图片的绝对像素坐标】。截图物理像素宽 img_w，屏幕逻辑宽
screen_w，则 dpr = img_w / screen_w，落到 PyAutoGUI 的逻辑坐标 = 像素 / dpr。
未改动进程 DPI 感知状态，dpr 动态实测，故与具体缩放比（100%/125%/150%）无关。

注：本文件原为 OpenManus 派生的「Daytona 远程沙箱」版 computer_use（走 HTTP 调
远端 VM 的自动化 API），项目里无人引用、也不符合本地桌面控制的目标，已整体替换
为下面的本地 PyAutoGUI 版。旧实现可在 git 历史 commit 77c16a2 找回。
"""

import asyncio
import base64
import hashlib
import os
import subprocess
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, Optional, Tuple

from app.logger import logger
from app.tool.ask_human import AskHuman
from app.tool.base import BaseTool, ToolResult
from app.tool.gui_agent import GUI_SYSTEM_PROMPT, query_gui_action


# 桌面增强版系统提示词：在浏览器版 6 动作之外，补桌面刚需的 DOUBLE_CLICK /
# RIGHT_CLICK / DRAG。以「追加到工具集」的方式扩展，不改动浏览器路径用的原提示词。
_DESKTOP_ACTIONS_ADDENDUM = """

## [桌面场景补充] 追加可用动作与约定
本次是【整块操作系统桌面】的截图（不是浏览器页面）。坐标为相对该截图的绝对像素。
在 `## 3. 工具集` 的动作之外，桌面场景**额外支持**以下动作，用法与规则完全一致：
### DOUBLE_CLICK
- **功能**: 双击屏幕（打开桌面图标、文件等）。
- **Parameters模板**: {"x": <integer>, "y": <integer>, "description": "<string, optional>"}
### RIGHT_CLICK
- **功能**: 右键单击（呼出上下文菜单）。
- **Parameters模板**: {"x": <integer>, "y": <integer>, "description": "<string, optional>"}
### DRAG
- **功能**: 从一点按住拖到另一点（拖动窗口、选区、滑块）。
- **Parameters模板**: {"from_x": <integer>, "from_y": <integer>, "to_x": <integer>, "to_y": <integer>, "description": "<string, optional>"}

补充约定：
- 桌面上打开程序/文件通常需要 DOUBLE_CLICK 图标，而不是单击。
- KEY_PRESS 支持系统级组合键，如 "win", "win+d", "alt+f4", "ctrl+s", "alt+tab"。
- 若目标窗口不在最前或被遮挡，先设法点其任务栏/可见部分使其置前，再操作。
"""

DESKTOP_GUI_SYSTEM_PROMPT = GUI_SYSTEM_PROMPT + _DESKTOP_ACTIONS_ADDENDUM


# 单次 task 视觉循环的最大原子操作步数。视觉模型每步只产出一个原子操作，
# 到达上限仍未 FINISH 则停下，交回上层判断，避免失控空转。
_MAX_ITERATIONS = 15
# 每个原子操作后固定停顿（秒），等 UI 响应（窗口弹出、菜单展开）再截下一张图。
_SETTLE_SECONDS = 0.8
# 单次 run 命令的超时（秒）。
_RUN_TIMEOUT = 120
# 破坏性 KEY_PRESS（规范化后的组合键字符串）：按下前每次都单独 ask_human 确认。
_DESTRUCTIVE_KEYS = {"alt+f4", "delete", "ctrl+w", "ctrl+q", "ctrl+shift+w"}

# SCROLL 幅度：按【当前屏幕高度】的比例算，而非写死像素——不同分辨率下体验一致。
_SCROLL_SCREEN_FRACTIONS = {"small": 0.3, "medium": 0.6, "large": 0.9}
# PyAutoGUI.scroll 的单位是「滚轮档」而非像素，Windows 一档约 120 单位（≈3 行）。
# 把「按屏高比例算出的目标像素」近似换算成档数时用作基准；不同鼠标/系统设置下
# 实际行距略有差异，故为近似值，但已随屏幕分辨率自适应，远优于写死档数。
_WHEEL_CLICK_PX = 120

# 干扰检测（人机共用同一套鼠标/键盘/屏幕时的协作护栏）：
# 视觉调用耗时数秒，其间人若移动鼠标或切换前台窗口，说明截图已过期，基于它下手
# 会点错。故在"观察"与"执行"之间比对环境指纹（前台窗口 + 鼠标位置），有变化就
# 丢弃本次动作、重新观察，而不是硬点。
# 鼠标位移超过该像素阈值即判为人为移动（留一点传感器抖动容差）。
_MOUSE_MOVE_TOLERANCE_PX = 4
# 连续检测到干扰达到该次数，就 ask_human 暂停等人忙完，避免和人抢输入。
_MAX_INTERFERENCE_STREAK = 3

# KEY_PRESS 键名 → PyAutoGUI 键名的别名表。PyAutoGUI 的 KEYBOARD_KEYS 多为小写。
_KEY_ALIASES = {
    "control": "ctrl",
    "ctrl": "ctrl",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
    "cmd": "win",
    "command": "win",
    "win": "win",
    "windows": "win",
    "super": "win",
    "meta": "win",
    "return": "enter",
    "enter": "enter",
    "escape": "esc",
    "esc": "esc",
    "del": "delete",
    "delete": "delete",
    "backspace": "backspace",
    "space": "space",
    "tab": "tab",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "pgup": "pageup",
    "pageup": "pageup",
    "pgdn": "pagedown",
    "pagedown": "pagedown",
    "home": "home",
    "end": "end",
    "ins": "insert",
    "insert": "insert",
    "capslock": "capslock",
    "printscreen": "printscreen",
}


class ComputerUseTool(BaseTool):
    """在整块操作系统桌面上执行 computer use：视觉自驱 + 少量确定性动作。

    动作（action）：
    - task：给一个自然语言子目标，工具内部跑「整屏截图→视觉决策→PyAutoGUI 执行
      →再截图确认」循环，直到 FINISH/FAILE 或到步数上限。用于点击/输入/拖拽等
      需要看画面才能定位的操作。
    - launch_app：启动程序或打开文件/路径（如 "notepad"、"C:/a.xlsx"）。
    - run：执行 shell 命令，返回输出。
    - hotkey：按系统级组合键，如 "win+d"、"alt+f4"、"ctrl+s"。
    - focus_window：按标题子串把某个窗口置前。
    - screenshot：只截当前整屏返回（观察，不操作）。
    - wait：等待 N 秒，让界面/安装器稳定。
    """

    name: str = "computer_use"
    description: str = """在【整个操作系统桌面】上执行操作（系统级 computer use），与 browser_use 并列、互补。
当目标不在浏览器页面里、browser_use 够不到时用本工具：系统原生文件对话框、WPS/Office 桌面端、资源管理器、
安装程序、任务栏、任意原生窗口。若操作对象是网页内容，请优先用 browser_use。
动作：
- task：给一个自然语言子目标（如 "在打开的记事本里点击文件菜单"），工具自动截屏→视觉定位→点击/输入，循环到完成。
  首次真实操作前会向你（人类）确认一次。用于需要看屏幕才能定位的点击/输入/双击/右键/拖拽。
- launch_app：启动程序或打开文件（app 传程序名或文件路径，如 "notepad"、"C:/账目.xlsx"）。
- run：执行系统命令（command），返回标准输出。
- hotkey：按组合键（keys，如 "win+d"、"alt+f4"、"ctrl+s"、"alt+tab"）。
- focus_window：把标题含指定子串（window_title）的窗口切到最前。
- screenshot：只截当前整屏返回，供你观察当前桌面状态。
- wait：等待 seconds 秒（等窗口弹出、安装器跑完）。
安全：鼠标甩到屏幕左上角可急停；单个 task 有步数上限；关键/破坏性操作前会人工确认。"""

    parameters: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "task",
                    "launch_app",
                    "run",
                    "hotkey",
                    "focus_window",
                    "screenshot",
                    "wait",
                ],
                "description": "要执行的桌面动作",
            },
            "task": {
                "type": "string",
                "description": "action=task 时：自然语言子目标，如 '点击桌面左上角的此电脑图标'",
            },
            "app": {
                "type": "string",
                "description": "action=launch_app 时：程序名或文件/路径，如 'notepad'、'C:/账目.xlsx'",
            },
            "command": {
                "type": "string",
                "description": "action=run 时：要执行的 shell 命令",
            },
            "keys": {
                "type": "string",
                "description": "action=hotkey 时：组合键，如 'win+d'、'alt+f4'、'ctrl+s'",
            },
            "window_title": {
                "type": "string",
                "description": "action=focus_window 时：目标窗口标题的子串",
            },
            "seconds": {
                "type": "number",
                "description": "action=wait 时：等待秒数",
            },
            "require_confirm": {
                "type": "boolean",
                "description": "action=task 时：首次真实操作前是否人工确认，默认 true。已获授权可传 false",
            },
        },
        "required": ["action"],
    }

    # ------------------------------------------------------------------ #
    # PyAutoGUI 惰性加载：模块导入不依赖 pyautogui（未装也能构造工具、跑单测），
    # 真正操作时才导入；缺失时给出明确安装提示而非 ImportError 崩栈。
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pyautogui():
        try:
            import pyautogui
        except Exception as e:  # ImportError 或无显示环境下的初始化错误
            raise RuntimeError(
                f"未能加载 pyautogui（{e}）。请先安装：pip install pyautogui"
            ) from e
        pyautogui.FAILSAFE = True  # 鼠标甩到左上角 -> 急停
        pyautogui.PAUSE = 0.1
        return pyautogui

    # ---- 截图与坐标 ---------------------------------------------------- #

    def _grab_screen(self) -> Tuple[bytes, int, int, int, int, float]:
        """整屏截图。返回 (png_bytes, img_w, img_h, screen_w, screen_h, dpr)。"""
        pg = self._pyautogui()
        screen_w, screen_h = pg.size()  # 逻辑分辨率（PyAutoGUI 鼠标坐标系）
        image = pg.screenshot()  # PIL.Image，物理像素
        img_w, img_h = image.size
        buf = BytesIO()
        image.save(buf, format="PNG")
        # dpr = 截图物理像素 / 屏幕逻辑像素。未设 DPI 感知时通常为 1；
        # 若为高 DPI（125%/150%）则 >1，据此把像素换算回逻辑坐标。
        dpr = (img_w / screen_w) if screen_w else 1.0
        return buf.getvalue(), img_w, img_h, screen_w, screen_h, dpr

    def _save_debug_screenshot(self, screenshot_bytes: bytes) -> Optional[str]:
        """把发给视觉模型的整屏截图落盘，便于人工核对坐标。"""
        try:
            debug_dir = "screenshots"
            os.makedirs(debug_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = os.path.join(debug_dir, f"computer_use_{stamp}.png")
            with open(path, "wb") as f:
                f.write(screenshot_bytes)
            return path
        except Exception as e:
            logger.debug(f"🖥️ computer_use 截图落盘失败（不影响主流程）：{e}")
            return None

    @staticmethod
    def _to_logical(px: float, dpr: float) -> float:
        return float(px) / (dpr or 1.0)

    # ---- 键名规范化 ---------------------------------------------------- #

    @staticmethod
    def _normalize_keys(keys: str) -> list:
        """把 'alt+f4' / 'win + d' 规范成 PyAutoGUI 键名列表 ['alt','f4']。"""
        import re

        parts = [p for p in keys.replace(" ", "").split("+") if p]
        out = []
        for p in parts:
            low = p.lower()
            if low in _KEY_ALIASES:
                out.append(_KEY_ALIASES[low])
            elif re.fullmatch(r"f\d{1,2}", low):  # 功能键 f1..f12
                out.append(low)
            else:
                out.append(low)  # 单字符/其它原样小写
        return out

    # ---- 人工确认（复用 ask_human） ----------------------------------- #

    async def _confirm(self, prompt: str) -> bool:
        """向人类确认。回答以 y/是/ok/continue 等开头视为同意，否则视为拒绝。"""
        answer = (await AskHuman().execute(inquire=prompt)) or ""
        low = answer.strip().lower()
        if not low:
            return False
        negative = ("n", "no", "否", "不", "取消", "stop", "cancel")
        if low.startswith(negative):
            return False
        affirmative = ("y", "yes", "是", "好", "ok", "行", "可以", "continue", "确认", "go")
        return low.startswith(affirmative)

    # ---- task：视觉自驱循环 ------------------------------------------- #

    async def _run_task(self, task: str, require_confirm: bool) -> ToolResult:
        """整屏截图→视觉决策→PyAutoGUI 执行→再截图确认的循环。

        镜像 browser_use_tool._execute_gui_action，但截图取整屏、动作落到 PyAutoGUI，
        并把浏览器路径没有的桌面动作（双击/右键/拖拽）纳入执行。
        """
        try:
            pg = self._pyautogui()
        except RuntimeError as e:
            return self.fail_response(str(e))

        outputs: list = []
        history: list = []
        confirmed = not require_confirm  # 首次真实操作前是否已确认
        last_signature: Optional[str] = None
        last_screen_hash: Optional[str] = None
        interference_streak = 0  # 连续检测到人为干扰的次数
        productive_steps = 0  # 已真正执行的动作数（干扰导致的重新观察不计入）

        while productive_steps < _MAX_ITERATIONS:
            try:
                shot, img_w, img_h, screen_w, screen_h, dpr = await asyncio.to_thread(
                    self._grab_screen
                )
            except RuntimeError as e:
                return self.fail_response(str(e))

            base64_image = base64.b64encode(shot).decode("utf-8")
            debug_path = self._save_debug_screenshot(shot)
            screen_hash = hashlib.md5(shot).hexdigest()
            # 截图当下的环境指纹（前台窗口 + 鼠标位置），稍后与"下手前一刻"比对。
            fp_at_shot = await asyncio.to_thread(self._env_fingerprint, pg)
            logger.info(
                f"🖥️ computer_use[{productive_steps + 1}/{_MAX_ITERATIONS}] 截图={img_w}x{img_h}px, "
                f"屏幕逻辑={screen_w}x{screen_h}, dpr={dpr:.3f}, 截图={debug_path}"
            )

            try:
                decision = await query_gui_action(
                    base64_image,
                    task,
                    history=history,
                    system_prompt=DESKTOP_GUI_SYSTEM_PROMPT,
                )
            except Exception as e:
                return ToolResult(error=f"GUI 视觉模型调用失败：{e}")

            action = decision["action"]
            params = decision["parameters"]
            thought = decision["thought"]

            # 收尾动作不碰屏幕，无需干扰/死锁检测，直接结束。
            if action == "FINISH":
                outputs.append(
                    f"[{productive_steps + 1}] FINISH: {params.get('message', '完成')}"
                )
                return ToolResult(output="[computer_use] " + " | ".join(outputs))
            if action == "FAILE":
                outputs.append(
                    f"[{productive_steps + 1}] FAILED: {params.get('reason', '未知原因')}"
                )
                return ToolResult(error="[computer_use] " + " | ".join(outputs))

            # 首次真实操作前的人工确认门（这一步会阻塞等人输入）
            if not confirmed:
                ok = await self._confirm(
                    f"computer_use 即将在系统桌面上执行任务：'{task}'。\n"
                    f"第一步计划：{action} {params}（{thought}）。\n是否允许？(y/n)"
                )
                if not ok:
                    return self.fail_response("用户拒绝了 computer_use 的桌面操作。")
                confirmed = True

            # 破坏性 KEY_PRESS 逐次确认（同样会阻塞等人输入）
            if action == "KEY_PRESS":
                norm = "+".join(self._normalize_keys(params.get("key", "")))
                if norm in _DESTRUCTIVE_KEYS:
                    ok = await self._confirm(
                        f"computer_use 准备按下破坏性组合键 '{norm}'（{thought}）。是否允许？(y/n)"
                    )
                    if not ok:
                        return self.fail_response(f"用户拒绝按下 '{norm}'。")

            # [干扰护栏] 下手前一刻再取指纹，与截图当下比对。视觉调用/确认对话框
            # 耗时里你若移动鼠标或切了前台窗口，说明这张截图已过期——丢弃本次动作、
            # 重新观察，而不是按旧坐标硬点。放在确认门之后，故也能覆盖确认耗时。
            fp_now = await asyncio.to_thread(self._env_fingerprint, pg)
            if self._detect_interference(fp_at_shot, fp_now):
                interference_streak += 1
                note = (
                    f"[干扰{interference_streak}] 观察后检测到人为操作"
                    f"（前台窗口/鼠标变化：{fp_at_shot} -> {fp_now}），"
                    f"丢弃基于旧截图的 {action}，重新观察。"
                )
                logger.info("🖥️ computer_use " + note)
                outputs.append(note)
                if interference_streak >= _MAX_INTERFERENCE_STREAK:
                    ok = await self._confirm(
                        f"我连续 {interference_streak} 次检测到你在使用鼠标/切换窗口，"
                        f"已暂停任务 '{task}'。忙完请回复 y/continue 继续，回复 n/stop 终止。"
                    )
                    if not ok:
                        return self.fail_response(
                            "用户在人为操作冲突后选择终止 computer_use 任务。"
                        )
                    interference_streak = 0  # 人已交回控制，重新观察后继续
                await asyncio.sleep(_SETTLE_SECONDS)
                continue
            interference_streak = 0

            # 防死锁：同一动作且画面无变化，判为视觉定位卡死，中止交回上层。
            # 放在干扰护栏之后：干扰导致的重新观察走上面的 continue，不会误触死锁判定。
            signature = f"{action}:{sorted((params or {}).items(), key=lambda kv: kv[0])}"
            if signature == last_signature and screen_hash == last_screen_hash:
                outputs.append(
                    f"[{productive_steps + 1}] 检测到重复动作 {action} 且屏幕无变化，视觉定位疑似"
                    "失败/卡死，已中止。请换用确定性动作（launch_app/hotkey/focus_window）"
                    "或调整子目标描述后重试。"
                )
                return ToolResult(error="[computer_use] " + " | ".join(outputs))
            last_signature = signature
            last_screen_hash = screen_hash

            outcome = await asyncio.to_thread(
                self._apply_action, pg, action, params, dpr, screen_h
            )
            productive_steps += 1
            outputs.append(f"[{productive_steps}] {outcome['message']} | {thought}")
            history.append(
                {"action": action, "thought": thought, "parameters": params}
            )
            if outcome["error"]:
                return ToolResult(error="[computer_use] " + " | ".join(outputs))

            await asyncio.sleep(_SETTLE_SECONDS)

        outputs.append(
            f"(达到最大步数 {_MAX_ITERATIONS} 仍未收到 FINISH，请查看桌面状态判断是否完成)"
        )
        return ToolResult(output="[computer_use] " + " | ".join(outputs))

    # ---- 干扰检测（人机共用桌面时的协作护栏） -------------------------- #

    @staticmethod
    def _env_fingerprint(pg) -> tuple:
        """取当前环境指纹：(前台窗口标识, 鼠标坐标)。取不到的部分为 None。

        前台窗口用 pygetwindow 的活动窗口（优先 _hWnd，退回 title）；鼠标用
        PyAutoGUI 当前坐标。两者都不改屏幕状态，纯读取。
        """
        try:
            mouse = tuple(pg.position())
        except Exception:
            mouse = None
        foreground = None
        try:
            import pygetwindow as gw

            win = gw.getActiveWindow()
            if win is not None:
                foreground = getattr(win, "_hWnd", None) or getattr(win, "title", None)
        except Exception:
            foreground = None
        return (foreground, mouse)

    @staticmethod
    def _detect_interference(before: tuple, after: tuple) -> bool:
        """比对两次指纹，判断"观察后、下手前"是否有人为操作。

        - 前台窗口变了 -> 有人切了窗口（或弹窗抢焦点），截图已过期。
        - 鼠标移动超过容差 -> 有人动了鼠标（此刻 agent 尚未执行动作，故必为外部）。
        任一命中即判为干扰。指纹缺失（None）时保守地不判为干扰，避免误报。
        """
        if not before or not after:
            return False
        fg_before, mouse_before = before
        fg_after, mouse_after = after
        if fg_before is not None and fg_after is not None and fg_before != fg_after:
            return True
        if mouse_before and mouse_after:
            if (
                abs(mouse_after[0] - mouse_before[0]) > _MOUSE_MOVE_TOLERANCE_PX
                or abs(mouse_after[1] - mouse_before[1]) > _MOUSE_MOVE_TOLERANCE_PX
            ):
                return True
        return False

    @staticmethod
    def _scroll_clicks(amount: str, screen_h: float, direction: str) -> int:
        """把 small/medium/large 按【当前屏幕高度】比例换算成带符号的滚轮档数。

        先按屏高比例算出目标像素，再按 _WHEEL_CLICK_PX 近似成档数。
        正数向上、负数向下（与 PyAutoGUI.scroll 语义一致）。
        """
        frac = _SCROLL_SCREEN_FRACTIONS.get(amount or "medium", 0.6)
        clicks = max(1, round((screen_h or 0) * frac / _WHEEL_CLICK_PX))
        return clicks if direction == "up" else -clicks

    def _apply_action(
        self, pg, action: str, params: Dict[str, Any], dpr: float, screen_h: float
    ) -> Dict[str, Any]:
        """执行单个原子操作（PyAutoGUI，同步；由 _run_task 放进线程里跑）。"""
        try:
            if action in ("CLICK", "DOUBLE_CLICK", "RIGHT_CLICK"):
                x, y = params.get("x"), params.get("y")
                if x is None or y is None:
                    return {"message": f"{action} 缺少坐标：{params}", "error": True}
                lx, ly = self._to_logical(x, dpr), self._to_logical(y, dpr)
                if action == "CLICK":
                    pg.click(lx, ly)
                elif action == "DOUBLE_CLICK":
                    pg.doubleClick(lx, ly)
                else:
                    pg.rightClick(lx, ly)
                desc = params.get("description", "")
                return {
                    "message": f"{action} px({x},{y})->逻辑({lx:.0f},{ly:.0f}) {desc}",
                    "error": False,
                }

            if action == "DRAG":
                fx, fy = params.get("from_x"), params.get("from_y")
                tx, ty = params.get("to_x"), params.get("to_y")
                if None in (fx, fy, tx, ty):
                    return {"message": f"DRAG 缺少坐标：{params}", "error": True}
                pg.moveTo(self._to_logical(fx, dpr), self._to_logical(fy, dpr))
                pg.dragTo(
                    self._to_logical(tx, dpr),
                    self._to_logical(ty, dpr),
                    duration=0.4,
                    button="left",
                )
                return {"message": f"DRAG ({fx},{fy})->({tx},{ty})", "error": False}

            if action == "TYPE":
                text = params.get("text", "")
                self._type_text(pg, text)
                if params.get("needs_enter"):
                    pg.press("enter")
                suffix = " + Enter" if params.get("needs_enter") else ""
                return {"message": f"TYPE '{text}'{suffix}", "error": False}

            if action == "SCROLL":
                direction = params.get("direction", "down")
                # 幅度按运行时实测的屏幕高度比例算，不写死像素/档数（见 _scroll_clicks）。
                clicks = self._scroll_clicks(
                    params.get("amount", "medium"), screen_h, direction
                )
                pg.scroll(clicks)
                return {
                    "message": f"SCROLL {direction} {abs(clicks)} 档（按 {screen_h}px 屏高比例）",
                    "error": False,
                }

            if action == "KEY_PRESS":
                keys = self._normalize_keys(params.get("key", ""))
                if not keys:
                    return {"message": f"KEY_PRESS 缺少 key：{params}", "error": True}
                if len(keys) == 1:
                    pg.press(keys[0])
                else:
                    pg.hotkey(*keys)
                return {"message": f"KEY_PRESS {'+'.join(keys)}", "error": False}

            return {"message": f"未知动作：{action}", "error": True}
        except Exception as e:
            return {"message": f"动作 '{action}' 执行失败：{e}", "error": True}

    @staticmethod
    def _type_text(pg, text: str) -> None:
        """输入文本。含非 ASCII（中文等）时走剪贴板粘贴，PyAutoGUI.write 打不出中文。"""
        if text and any(ord(c) > 127 for c in text):
            try:
                import pyperclip

                pyperclip.copy(text)
                pg.hotkey("ctrl", "v")
                return
            except Exception:
                pass  # 无 pyperclip 则退回逐字符（可能丢中文）
        pg.write(text, interval=0.02)

    # ---- 确定性动作 --------------------------------------------------- #

    async def _launch_app(self, app: str) -> ToolResult:
        if not app:
            return self.fail_response("launch_app 需要提供 app（程序名或文件路径）")

        def _do():
            # Windows：文件/已注册程序名优先用 os.startfile（走系统关联）；
            # 其余（含参数的命令、非 Windows）退回 shell 启动。
            if os.name == "nt" and (os.path.exists(app) or os.sep not in app):
                try:
                    os.startfile(app)  # type: ignore[attr-defined]  # 仅 Windows 有
                    return
                except Exception:
                    pass
            subprocess.Popen(app, shell=True)

        try:
            await asyncio.to_thread(_do)
            return self.success_response(f"已启动：{app}")
        except Exception as e:
            return self.fail_response(f"启动失败：{app} —— {e}")

    async def _run(self, command: str) -> ToolResult:
        if not command:
            return self.fail_response("run 需要提供 command")
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=_RUN_TIMEOUT,
            )
            out = (proc.stdout or "")[:4000]
            err = (proc.stderr or "")[:2000]
            body = f"returncode={proc.returncode}\n[stdout]\n{out}"
            if err:
                body += f"\n[stderr]\n{err}"
            return self.success_response(body)
        except subprocess.TimeoutExpired:
            return self.fail_response(f"命令超时（>{_RUN_TIMEOUT}s）：{command}")
        except Exception as e:
            return self.fail_response(f"命令执行失败：{e}")

    async def _hotkey(self, keys: str) -> ToolResult:
        if not keys:
            return self.fail_response("hotkey 需要提供 keys，如 'win+d'")
        norm = self._normalize_keys(keys)
        if "+".join(norm) in _DESTRUCTIVE_KEYS:
            if not await self._confirm(
                f"computer_use 准备按下破坏性组合键 '{'+'.join(norm)}'。是否允许？(y/n)"
            ):
                return self.fail_response(f"用户拒绝按下 '{'+'.join(norm)}'。")
        try:
            pg = self._pyautogui()
            if len(norm) == 1:
                await asyncio.to_thread(pg.press, norm[0])
            else:
                await asyncio.to_thread(pg.hotkey, *norm)
            return self.success_response(f"已按下：{'+'.join(norm)}")
        except RuntimeError as e:
            return self.fail_response(str(e))
        except Exception as e:
            return self.fail_response(f"按键失败：{e}")

    async def _focus_window(self, title: str) -> ToolResult:
        if not title:
            return self.fail_response("focus_window 需要提供 window_title 子串")
        try:
            import pygetwindow as gw
        except Exception as e:
            return self.fail_response(f"未能加载 pygetwindow（{e}）。它随 pyautogui 一起安装。")
        try:
            wins = await asyncio.to_thread(gw.getWindowsWithTitle, title)
            if not wins:
                return self.fail_response(f"未找到标题含 '{title}' 的窗口")
            w = wins[0]

            def _activate():
                try:
                    if w.isMinimized:
                        w.restore()
                except Exception:
                    pass
                w.activate()

            await asyncio.to_thread(_activate)
            return self.success_response(f"已置前窗口：{w.title}")
        except Exception as e:
            return self.fail_response(f"窗口置前失败：{e}")

    async def _screenshot(self) -> ToolResult:
        try:
            shot, img_w, img_h, screen_w, screen_h, dpr = await asyncio.to_thread(
                self._grab_screen
            )
        except RuntimeError as e:
            return self.fail_response(str(e))
        self._save_debug_screenshot(shot)
        b64 = base64.b64encode(shot).decode("utf-8")
        return ToolResult(
            output=f"已截取整屏：{img_w}x{img_h}px，屏幕逻辑 {screen_w}x{screen_h}，dpr={dpr:.3f}",
            base64_image=b64,
        )

    # ---- 分发 --------------------------------------------------------- #

    async def execute(
        self,
        action: str,
        task: Optional[str] = None,
        app: Optional[str] = None,
        command: Optional[str] = None,
        keys: Optional[str] = None,
        window_title: Optional[str] = None,
        seconds: Optional[float] = None,
        require_confirm: bool = True,
        **kwargs: Any,
    ) -> ToolResult:
        try:
            if action == "task":
                if not task:
                    return self.fail_response("action=task 需要提供 task 子目标")
                return await self._run_task(task, require_confirm)
            if action == "launch_app":
                return await self._launch_app(app or "")
            if action == "run":
                return await self._run(command or "")
            if action == "hotkey":
                return await self._hotkey(keys or "")
            if action == "focus_window":
                return await self._focus_window(window_title or "")
            if action == "screenshot":
                return await self._screenshot()
            if action == "wait":
                await asyncio.sleep(float(seconds or 1))
                return self.success_response(f"已等待 {seconds or 1} 秒")
            return self.fail_response(f"未知 action：{action}")
        except Exception as e:
            import traceback

            return self.fail_response(
                f"ComputerUseTool 执行失败：{e}\n{traceback.format_exc()}"
            )
