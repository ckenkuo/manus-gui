# -*- coding: utf-8 -*-
"""ComputerUseTool（系统桌面级 computer use）单测。

分两部分：
- 纯逻辑/接线/确定性动作/优雅降级：不依赖 pyautogui、显示器或视觉模型 API，
  任何环境都能跑（缺 pyautogui 时相关断言验证的正是"干净降级"）。
- 真机截图：用 importorskip 守卫，仅在装了 pyautogui 且有可截屏的桌面时运行，
  验证坐标契约（逻辑坐标 = 像素 / dpr）在本机成立。

运行：pytest tests/test_computer_use.py -v
"""

import asyncio
import base64
import inspect
from io import BytesIO

import pytest

import app.tool.computer_use_tool as cu
from app.tool.computer_use_tool import DESKTOP_GUI_SYSTEM_PROMPT, ComputerUseTool
from app.tool.gui_agent import GUI_SYSTEM_PROMPT, query_gui_action


def _run(coro):
    """在同步测试里跑协程，沿用本仓库其它测试的 asyncio.run 风格。"""
    return asyncio.run(coro)


class _FakePg:
    """假的 PyAutoGUI，记录调用而不真正操作屏幕，用于测 _apply_action 的换算。"""

    def __init__(self):
        self.calls = []

    def click(self, x, y):
        self.calls.append(("click", x, y))

    def doubleClick(self, x, y):
        self.calls.append(("doubleClick", x, y))

    def rightClick(self, x, y):
        self.calls.append(("rightClick", x, y))

    def scroll(self, n):
        self.calls.append(("scroll", n))

    def write(self, text, interval=0):
        self.calls.append(("write", text))

    def press(self, key):
        self.calls.append(("press", key))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))

    def moveTo(self, x, y):
        self.calls.append(("moveTo", x, y))

    def dragTo(self, x, y, duration=0, button="left"):
        self.calls.append(("dragTo", x, y))


# ---- 接线与身份 ------------------------------------------------------ #


def test_package_exports_same_class():
    """app.tool 的包级导出与模块内定义应是同一个类（agent 注册依赖它）。"""
    from app.tool import ComputerUseTool as Exported

    assert Exported is ComputerUseTool


def test_registered_in_manus_available_tools():
    """ComputerUseTool 应被注册进 Manus 的默认工具集。"""
    from app.agent.manus import Manus

    names = {t.name for t in Manus().available_tools.tools}
    assert "computer_use" in names


def test_to_param_schema_shape():
    """to_param() 输出 OpenAI function-call 格式，action 枚举齐全。"""
    tool = ComputerUseTool()
    param = tool.to_param()
    assert param["type"] == "function"
    assert param["function"]["name"] == "computer_use"
    actions = set(param["function"]["parameters"]["properties"]["action"]["enum"])
    assert actions == {
        "task",
        "launch_app",
        "run",
        "hotkey",
        "focus_window",
        "screenshot",
        "wait",
    }


# ---- 视觉大脑的零回归扩展 -------------------------------------------- #


def test_desktop_prompt_extends_browser_prompt():
    """桌面提示词以浏览器版为前缀（不改动它），并追加桌面动作。"""
    assert DESKTOP_GUI_SYSTEM_PROMPT.startswith(GUI_SYSTEM_PROMPT)
    for action in ("DOUBLE_CLICK", "RIGHT_CLICK", "DRAG"):
        assert action in DESKTOP_GUI_SYSTEM_PROMPT


def test_query_gui_action_has_system_prompt_param():
    """query_gui_action 新增可选 system_prompt 参数，且默认不改浏览器行为。"""
    sig = inspect.signature(query_gui_action)
    assert "system_prompt" in sig.parameters
    assert sig.parameters["system_prompt"].default is None


# ---- 纯逻辑：键名规范化 ---------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("alt+f4", ["alt", "f4"]),
        ("Win + D", ["win", "d"]),
        ("ctrl+S", ["ctrl", "s"]),
        ("Escape", ["esc"]),
        ("cmd+c", ["win", "c"]),  # cmd/command/super 均归一到 win
        ("Ctrl+Shift+W", ["ctrl", "shift", "w"]),
        ("F12", ["f12"]),
    ],
)
def test_normalize_keys(raw, expected):
    assert ComputerUseTool._normalize_keys(raw) == expected


# ---- 纯逻辑：DPI 坐标换算 -------------------------------------------- #


@pytest.mark.parametrize(
    "px, dpr, logical",
    [
        (200, 2.0, 100.0),  # 200% 缩放
        (250, 1.25, 200.0),  # 125% 缩放
        (150, 1.0, 150.0),  # 100% 缩放
        (150, 0, 150.0),  # dpr=0 兜底为不缩放，避免除零
    ],
)
def test_to_logical(px, dpr, logical):
    assert ComputerUseTool._to_logical(px, dpr) == logical


# ---- 纯逻辑：人工确认解析 -------------------------------------------- #


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("y", True),
        ("yes", True),
        ("是", True),
        ("确认", True),
        ("n", False),
        ("no thanks", False),
        ("否", False),
        ("", False),  # 空回答默认不放行（保守）
    ],
)
def test_confirm_parsing(monkeypatch, answer, expected):
    async def stub_execute(self, inquire):
        return answer

    monkeypatch.setattr(cu.AskHuman, "execute", stub_execute)
    tool = ComputerUseTool()
    assert _run(tool._confirm("?")) is expected


# ---- 确定性动作（不需要 pyautogui / 显示器） ------------------------ #


def test_run_action_returns_stdout():
    tool = ComputerUseTool()
    result = _run(tool.execute(action="run", command="echo hello_ct"))
    assert result.error is None
    assert "hello_ct" in result.output


def test_wait_action():
    tool = ComputerUseTool()
    result = _run(tool.execute(action="wait", seconds=0))
    assert result.error is None


def test_unknown_action_errors():
    tool = ComputerUseTool()
    result = _run(tool.execute(action="bogus"))
    assert result.error and "未知 action" in result.error


def test_task_without_goal_errors():
    tool = ComputerUseTool()
    result = _run(tool.execute(action="task"))
    assert result.error and "task" in result.error


# ---- 环境自适应：滚动幅度按实际屏幕/视口比例，而非写死像素 ----------- #


@pytest.mark.parametrize(
    "screen_h, expected_down_clicks",
    [
        (768, -4),  # round(768*0.6/120)  = round(3.84) = 4
        (1080, -5),  # round(1080*0.6/120) = round(5.4)  = 5
        (2160, -11),  # round(2160*0.6/120) = round(10.8) = 11（4K：滚更多）
    ],
)
def test_scroll_clicks_scales_with_screen_height(screen_h, expected_down_clicks):
    """屏幕越高，一次 medium 滚动的档数越多——随环境自适应，不再是定值。"""
    assert ComputerUseTool._scroll_clicks("medium", screen_h, "down") == expected_down_clicks


def test_scroll_clicks_direction_sign():
    """方向符号符合 PyAutoGUI：up 为正、down 为负。"""
    assert ComputerUseTool._scroll_clicks("medium", 1000, "up") > 0
    assert ComputerUseTool._scroll_clicks("medium", 1000, "down") < 0


def test_apply_action_scroll_is_screen_relative():
    """_apply_action 的 SCROLL 用实测 screen_h 算档数，且不触碰真实屏幕。"""
    tool = ComputerUseTool()
    pg_small = _FakePg()
    tool._apply_action(pg_small, "SCROLL", {"amount": "medium", "direction": "down"}, 1.0, 1000)
    assert ("scroll", -5) in pg_small.calls  # round(1000*0.6/120)=5

    pg_big = _FakePg()  # 屏幕更高 -> 滚更多，证明随环境变化
    tool._apply_action(pg_big, "SCROLL", {"amount": "medium", "direction": "down"}, 1.0, 2000)
    assert ("scroll", -10) in pg_big.calls  # round(2000*0.6/120)=10


def test_apply_action_click_applies_dpr():
    """CLICK 把图片像素按 dpr 换算成逻辑坐标后再点击。"""
    tool = ComputerUseTool()
    pg = _FakePg()
    tool._apply_action(pg, "CLICK", {"x": 200, "y": 100}, 2.0, 1000)
    assert ("click", 100.0, 50.0) in pg.calls  # 200/2, 100/2


def test_browser_gui_scroll_pixels_is_viewport_relative():
    """浏览器 gui_action 的滚动像素按视口高度比例算，不写死。"""
    from app.tool.browser_use_tool import BrowserUseTool

    assert BrowserUseTool._gui_scroll_pixels("small", 1000) == 300
    assert BrowserUseTool._gui_scroll_pixels("medium", 1000) == 600
    assert BrowserUseTool._gui_scroll_pixels("large", 1000) == 900
    assert BrowserUseTool._gui_scroll_pixels("medium", 2000) == 1200  # 视口更高滚更多


# ---- 干扰检测：人机共用桌面时的协作护栏 ----------------------------- #


def test_detect_interference_no_change():
    """指纹不变 -> 无干扰。"""
    fp = ("winA", (100, 100))
    assert ComputerUseTool._detect_interference(fp, fp) is False


def test_detect_interference_foreground_changed():
    """前台窗口变了 -> 有人切窗口/弹窗抢焦点。"""
    assert ComputerUseTool._detect_interference(("winA", (100, 100)), ("winB", (100, 100))) is True


def test_detect_interference_mouse_moved():
    """鼠标移动超容差 -> 有人动了鼠标。"""
    assert ComputerUseTool._detect_interference(("winA", (100, 100)), ("winA", (100, 200))) is True


def test_detect_interference_mouse_jitter_within_tolerance():
    """微小位移在容差内 -> 不误报。"""
    assert ComputerUseTool._detect_interference(("winA", (100, 100)), ("winA", (102, 101))) is False


def test_detect_interference_missing_fingerprint_is_safe():
    """指纹缺失时保守地不判为干扰。"""
    assert ComputerUseTool._detect_interference(None, ("winA", (0, 0))) is False
    assert ComputerUseTool._detect_interference(("winA", (0, 0)), (None, None)) is False


def test_task_loop_reobserves_on_interference_then_acts(monkeypatch):
    """观察后检测到干扰的那一轮丢弃动作、重新观察；干扰消失后才真正执行。"""
    tool = ComputerUseTool()

    # 假 pyautogui / 截图 / 落盘
    monkeypatch.setattr(ComputerUseTool, "_pyautogui", staticmethod(lambda: _FakePg()))
    monkeypatch.setattr(
        ComputerUseTool, "_grab_screen",
        lambda self: (b"png-bytes", 100, 100, 100, 100, 1.0),
    )
    monkeypatch.setattr(ComputerUseTool, "_save_debug_screenshot", lambda self, b: None)

    # 指纹序列（每轮取两次：截图时、下手前）：
    #   轮1：前台从 winA 变 winB -> 判为干扰，丢弃并重新观察
    #   轮2：winA 保持不变 -> 放行执行
    fps = [
        ("winA", (0, 0)), ("winB", (0, 0)),  # 轮1 -> 干扰
        ("winA", (0, 0)), ("winA", (0, 0)),  # 轮2 -> 正常
    ]
    monkeypatch.setattr(
        ComputerUseTool, "_env_fingerprint",
        lambda self, pg: fps.pop(0) if fps else ("winA", (0, 0)),
    )

    # 决策序列：CLICK（轮1被丢弃）、CLICK（轮2执行）、FINISH
    decisions = [
        {"action": "CLICK", "parameters": {"x": 10, "y": 10}, "thought": "点1"},
        {"action": "CLICK", "parameters": {"x": 20, "y": 20}, "thought": "点2"},
        {"action": "FINISH", "parameters": {"message": "done"}, "thought": "完成"},
    ]

    async def fake_query(*a, **k):
        return decisions.pop(0)

    monkeypatch.setattr(cu, "query_gui_action", fake_query)

    applied = []

    def fake_apply(self, pg, action, params, dpr, screen_h):
        applied.append((action, params))
        return {"message": f"applied {action}", "error": False}

    monkeypatch.setattr(ComputerUseTool, "_apply_action", fake_apply)

    result = _run(tool._run_task("测试任务", require_confirm=False))

    # 轮1 的 CLICK 因干扰未执行；只有轮2 的 CLICK(x=20) 真正落地
    assert applied == [("CLICK", {"x": 20, "y": 20})]
    assert result.error is None
    assert "干扰" in result.output  # 输出里记录了这次干扰
    assert "FINISH" in result.output


# ---- 真机截图：坐标契约验证（无 pyautogui/显示器则跳过） ------------- #


def test_real_screenshot_and_coordinate_contract():
    """真机整屏截图：PNG 合法、尺寸自洽、逻辑坐标 = 像素 / dpr。"""
    pytest.importorskip("pyautogui")
    from PIL import Image

    tool = ComputerUseTool()
    try:
        shot, img_w, img_h, screen_w, screen_h, dpr = tool._grab_screen()
    except Exception as e:  # 无头/无显示环境（如 CI）：跳过而非失败
        pytest.skip(f"无可截屏的桌面环境：{e}")

    # PNG 合法且尺寸与上报一致
    Image.open(BytesIO(shot)).verify()
    assert (img_w, img_h) == Image.open(BytesIO(shot)).size
    assert screen_w > 0 and screen_h > 0 and dpr > 0

    # 坐标契约：截图正中的像素换算后应落在屏幕逻辑中心（±1 容差）
    lx = tool._to_logical(img_w / 2, dpr)
    ly = tool._to_logical(img_h / 2, dpr)
    assert abs(lx - screen_w / 2) <= 1
    assert abs(ly - screen_h / 2) <= 1


def test_real_execute_screenshot_returns_decodable_image():
    """execute(screenshot) 端到端返回可解码的 base64 截图。"""
    pytest.importorskip("pyautogui")
    from PIL import Image

    tool = ComputerUseTool()
    result = _run(tool.execute(action="screenshot"))
    if result.error:
        pytest.skip(f"无可截屏的桌面环境：{result.error}")
    assert result.base64_image
    Image.open(BytesIO(base64.b64decode(result.base64_image))).verify()
