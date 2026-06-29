import asyncio
import base64
import json
from typing import Generic, Optional, TypeVar

from browser_use import Browser as BrowserUseBrowser
from browser_use import BrowserConfig
from browser_use.browser.context import BrowserContext, BrowserContextConfig
from browser_use.dom.service import DomService
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from app.config import config
from app.llm import LLM
from app.logger import logger
from app.tool.base import BaseTool, ToolResult
from app.tool.gui_agent import query_gui_action
from app.tool.web_search import WebSearch


_BROWSER_DESCRIPTION = """\
一个强大的浏览器自动化工具，允许通过各种操作与网页交互。
* 此工具提供用于控制浏览器会话、导航网页和提取信息的命令
* 它在调用之间保持状态，保持浏览器会话活动直到显式关闭
* 当你需要浏览网站、填写表单、点击按钮、提取内容或执行网页搜索时使用此工具
* 每个操作都需要工具依赖项中定义的特定参数

主要功能包括：
* 导航：转到特定 URL、返回、搜索网页或刷新页面
* 交互：点击元素、输入文本、通过键盘逐字输入、聚焦元素、从下拉菜单中选择、发送键盘命令
* 滚动：按像素量向上/向下滚动或滚动到特定文本
* 内容提取：根据特定目标从网页中提取和分析内容
* 标签页管理：在标签页之间切换、打开新标签页或关闭标签页

日期选择器操作技巧（重要）：
* 选日期请优先用 action="select_date"，index=日期输入框索引，text="2026-07-01"（也支持 "7月1日"）
* 它会自动打开日历、按日期属性/文本点中真实日期格（必要时 JS 兜底设值），比视觉坐标稳得多
* 不要用 gui_action 去点日历——日历格小、易差一格，select_date 是日期的首选路径

视觉（GUI）坐标操作技巧：
* action="gui_action" 是与 DOM 索引操作（click_element/input_text/select_date）**并列、平等**的另一种交互方式
* 它会对当前页面截图，交给视觉模型分析，并按像素坐标直接驱动鼠标/键盘，不依赖 DOM 索引
* 仅在 DOM 确实没有抓手时才用它：canvas、地图、图片热区、富文本等无法用选择器命中的自定义控件
* [关键] task 必须是**单一原子子目标**，不要写复合步骤。
  正确："点击右上角的登录按钮"、"选中画布上的红色圆形"
  错误："点击输入框再打开日历再选7月1日"（复合步骤会让模型卡在第一步）

注意：DOM 操作（click_element/input_text/select_date）与坐标操作（gui_action）是平行路径，
按页面状态选最合适的一种；有 DOM 抓手时优先 DOM，更精确稳定。
"""

# 单次 gui_action 调用内部的最大原子操作步数。GUI 视觉模型每次只产出一个
# 原子操作（CLICK/TYPE/...），通过内部循环让它「操作 -> 重新截图 -> 确认」自洽
# 完成一个子目标（如选中日期），避免外层 LLM 在多种方式之间反复试探。
_GUI_MAX_ITERATIONS = 6
# 每个原子操作后等待页面响应、再重新截图的毫秒数。
_GUI_SETTLE_MS = 500
# 原子操作（尤其 CLICK）后，额外等待可能的导航/网络空闲的上限毫秒数。
# 携程等站点点击搜索后跳转较慢，若不等导航完成就截下一张图，会截到旧页面，
# 导致模型重复同一动作而被防死锁误判为「卡死」。等导航稳定可消除该误判。
_GUI_NAV_TIMEOUT_MS = 4000

# 视觉模型返回的功能键名（如 'esc'/'enter'/'alt+f4'）映射到 Playwright 规范键名。
# Playwright 只认规范名（'Escape' 而非 'Esc'），naive 的 capitalize 会得到非法键名报错。
_KEY_ALIASES = {
    "esc": "Escape", "escape": "Escape",
    "enter": "Enter", "return": "Enter", "ret": "Enter",
    "tab": "Tab",
    "space": " ", "spacebar": " ",
    "del": "Delete", "delete": "Delete",
    "backspace": "Backspace", "bksp": "Backspace",
    "ins": "Insert", "insert": "Insert",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
    "pageup": "PageUp", "pgup": "PageUp",
    "pagedown": "PageDown", "pgdn": "PageDown",
    "home": "Home", "end": "End",
}
# 修饰键别名。
_MODIFIER_ALIASES = {
    "ctrl": "Control", "control": "Control",
    "alt": "Alt", "option": "Alt", "opt": "Alt",
    "shift": "Shift",
    "cmd": "Meta", "command": "Meta", "meta": "Meta", "win": "Meta",
}


# 在 DOM 中定位目标日期格并返回其视口中心坐标的 JS。站点无关：
# 先按属性(data-date/aria-label/title 含 ISO 或中文日期)匹配，
# 再退化到「月份感知的日号匹配」——在含目标月份标签的可见容器里找文本为日号的格。
_DATE_CELL_FINDER_JS = """
(args) => {
  const {iso, cn, cnFull, day, monthLabel} = args;
  const vis = (e) => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.top >= 0 && r.left >= 0
      && r.bottom <= (window.innerHeight + 100)
      && getComputedStyle(e).visibility !== 'hidden';
  };
  const ctr = (e) => { const r = e.getBoundingClientRect(); return {x: r.left + r.width/2, y: r.top + r.height/2}; };

  // 1) 属性精确匹配
  for (const e of document.querySelectorAll('[data-date],[data-value],[data-day],[aria-label],[title]')) {
    const vals = ['data-date','data-value','data-day','aria-label','title']
      .map(a => e.getAttribute(a)).filter(Boolean);
    if (vals.some(v => v.includes(iso) || v.includes(cnFull) || v.includes(cn)) && vis(e)) {
      return {found: true, via: 'attr', ...ctr(e)};
    }
  }

  // 2) 月份感知的日号匹配：文本恰为日号的叶子格，且其所属「单月面板」正是目标月。
  //    [关键] 携程把多个月并排放在同一父容器里，不能只看某层祖先是否含 '7月'——
  //    那样 6月面板里的 '1' 也会因祖先同时含 '6月'/'7月' 而误中。做法：从格子向上，
  //    找到第一个文本里恰好只含一种 'N月' 的祖先（即单月面板），要求它就是目标月。
  const dayStr = String(day);
  const leaves = Array.from(document.querySelectorAll('td,div,span,a,li,button'))
    .filter(e => e.textContent.trim() === dayStr && e.children.length <= 1 && vis(e));
  for (const e of leaves) {
    let p = e.parentElement, hops = 0;
    while (p && hops < 8) {
      const months = (p.textContent || '').match(/\\d{1,2}月/g) || [];
      const uniq = Array.from(new Set(months));
      if (uniq.length === 1) {
        // 该祖先是单月面板：是目标月才接受，否则该格属于别的月份，放弃
        if (uniq[0] === monthLabel) return {found: true, via: 'dayText+singleMonthPanel', ...ctr(e)};
        break;
      }
      if (uniq.length > 1) break;  // 已到跨月容器仍没找到单月面板，放弃该格
      p = p.parentElement; hops++;
    }
  }

  return {found: false, leafCount: leaves.length, dataDateCount: document.querySelectorAll('[data-date]').length};
}
"""


def _parse_date(date_text: str) -> Optional[dict]:
    """把多种日期写法解析为 {year, month, day}。

    支持："2026-07-01"、"2026/7/1"、"2026年7月1日"、"7月1日"（无年份）。
    无法解析时返回 None。年份缺失时 year=None。
    """
    import re

    if not date_text:
        return None
    s = date_text.strip()

    # 2026-07-01 / 2026/7/1 / 2026.7.1
    m = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        return {"year": int(m.group(1)), "month": int(m.group(2)), "day": int(m.group(3))}

    # 7月1日 / 7-1 / 7/1（无年份）
    m = re.search(r"(\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        return {"year": None, "month": int(m.group(1)), "day": int(m.group(2))}

    return None


def _infer_year(parsed: dict) -> dict:
    """缺年份时按当前日期推断：取今年；若该月日已过则滚到明年。

    LLM 常只传 '7月1日'，没有年份会导致候选缺 ISO 形式、JS 兜底也无法设值。
    """
    if parsed.get("year"):
        return parsed
    from datetime import date

    today = date.today()
    year = today.year
    try:
        if date(year, parsed["month"], parsed["day"]) < today:
            year += 1
    except ValueError:
        pass
    return {**parsed, "year": year}


Context = TypeVar("Context")


class BrowserUseTool(BaseTool, Generic[Context]):
    name: str = "browser_use"
    description: str = _BROWSER_DESCRIPTION
    parameters: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "go_to_url",
                    "click_element",
                    "input_text",
                    "scroll_down",
                    "scroll_up",
                    "scroll_to_text",
                    "send_keys",
                    "get_dropdown_options",
                    "select_dropdown_option",
                    "go_back",
                    "web_search",
                    "wait",
                    "extract_content",
                    "switch_tab",
                    "open_tab",
                    "close_tab",
                    "focus_element",
                    "type_text",
                    "select_date",
                    "gui_action",
                ],
                "description": "要执行的浏览器操作",
            },
            "url": {
                "type": "string",
                "description": "用于 'go_to_url' 或 'open_tab' 操作的 URL",
            },
            "index": {
                "type": "integer",
                "description": "用于 'click_element'、'input_text'、'focus_element'、'get_dropdown_options'、'select_dropdown_option' 操作的元素索引，或 'select_date' 的日期输入框索引",
            },
            "text": {
                "type": "string",
                "description": "用于 'input_text'、'type_text'、'scroll_to_text'、'select_dropdown_option' 操作的文本，或 'select_date' 的日期（如 '2026-07-01' 或 '7月1日'）",
            },
            "scroll_amount": {
                "type": "integer",
                "description": "用于 'scroll_down' 或 'scroll_up' 操作的滚动像素数（正数向下，负数向上）",
            },
            "tab_id": {
                "type": "integer",
                "description": "用于 'switch_tab' 操作的标签页 ID",
            },
            "query": {
                "type": "string",
                "description": "用于 'web_search' 操作的搜索查询",
            },
            "goal": {
                "type": "string",
                "description": "用于 'extract_content' 操作的提取目标",
            },
            "keys": {
                "type": "string",
                "description": "用于 'send_keys' 操作要发送的按键（单个键如 Enter/Tab 或键盘组合键如 Control+a，也支持完整字符串如 '2026-06-30'）",
            },
            "seconds": {
                "type": "integer",
                "description": "用于 'wait' 操作要等待的秒数",
            },
            "task": {
                "type": "string",
                "description": "用于 'gui_action' 视觉坐标操作的自然语言子目标，例如 '选择 1月30日 的出发日期'。gui_action 与基于索引的 click_element/input_text 并列，由你按页面情况自主选择",
            },
        },
        "required": ["action"],
        "dependencies": {
            "go_to_url": ["url"],
            "click_element": ["index"],
            "input_text": ["index", "text"],
            "switch_tab": ["tab_id"],
            "open_tab": ["url"],
            "scroll_down": ["scroll_amount"],
            "scroll_up": ["scroll_amount"],
            "scroll_to_text": ["text"],
            "send_keys": ["keys"],
            "get_dropdown_options": ["index"],
            "select_dropdown_option": ["index", "text"],
            "focus_element": ["index"],
            "type_text": ["text"],
            "select_date": ["text"],
            "gui_action": ["task"],
            "go_back": [],
            "web_search": ["query"],
            "wait": ["seconds"],
            "extract_content": ["goal"],
        },
    }

    lock: asyncio.Lock = Field(default_factory=asyncio.Lock)
    browser: Optional[BrowserUseBrowser] = Field(default=None, exclude=True)
    context: Optional[BrowserContext] = Field(default=None, exclude=True)
    dom_service: Optional[DomService] = Field(default=None, exclude=True)
    web_search_tool: WebSearch = Field(default_factory=WebSearch, exclude=True)

    # Context for generic functionality
    tool_context: Optional[Context] = Field(default=None, exclude=True)

    llm: Optional[LLM] = Field(default_factory=LLM)

    @field_validator("parameters", mode="before")
    def validate_parameters(cls, v: dict, info: ValidationInfo) -> dict:
        if not v:
            raise ValueError("Parameters cannot be empty")
        return v

    async def _ensure_browser_initialized(self) -> BrowserContext:
        """确保浏览器和上下文已初始化。"""
        if self.browser is None:
            browser_config_kwargs = {"headless": False, "disable_security": True}

            if config.browser_config:
                from browser_use.browser.browser import ProxySettings

                # 处理代理设置。
                if config.browser_config.proxy and config.browser_config.proxy.server:
                    browser_config_kwargs["proxy"] = ProxySettings(
                        server=config.browser_config.proxy.server,
                        username=config.browser_config.proxy.username,
                        password=config.browser_config.proxy.password,
                    )

                browser_attrs = [
                    "headless",
                    "disable_security",
                    "extra_chromium_args",
                    "chrome_instance_path",
                    "wss_url",
                    "cdp_url",
                ]

                for attr in browser_attrs:
                    value = getattr(config.browser_config, attr, None)
                    if value is not None:
                        if not isinstance(value, list) or value:
                            browser_config_kwargs[attr] = value

            self.browser = BrowserUseBrowser(BrowserConfig(**browser_config_kwargs))

        if self.context is None:
            context_config = BrowserContextConfig()

            # 如果配置中有上下文配置，则使用它。
            if (
                config.browser_config
                and hasattr(config.browser_config, "new_context_config")
                and config.browser_config.new_context_config
            ):
                context_config = config.browser_config.new_context_config

            self.context = await self.browser.new_context(context_config)
            self.dom_service = DomService(await self.context.get_current_page())

        return self.context

    async def execute(
        self,
        action: str,
        url: Optional[str] = None,
        index: Optional[int] = None,
        text: Optional[str] = None,
        scroll_amount: Optional[int] = None,
        tab_id: Optional[int] = None,
        query: Optional[str] = None,
        goal: Optional[str] = None,
        keys: Optional[str] = None,
        seconds: Optional[int] = None,
        task: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        """
        执行指定的浏览器操作。

        Args:
            action: 要执行的浏览器操作
            url: 用于导航或新标签页的 URL
            index: 用于点击或输入操作的元素索引
            text: 用于输入操作或搜索查询的文本
            scroll_amount: 用于滚动操作的滚动像素数
            tab_id: 用于 switch_tab 操作的标签页 ID
            query: 用于 Google 搜索的搜索查询
            goal: 用于内容提取的提取目标
            keys: 用于键盘操作要发送的按键
            seconds: 要等待的秒数
            task: 用于 gui_action 视觉坐标操作的自然语言子目标
            **kwargs: 其他参数

        Returns:
            包含操作输出或错误的 ToolResult
        """
        async with self.lock:
            try:
                context = await self._ensure_browser_initialized()

                # 从配置中获取最大内容长度
                max_content_length = getattr(
                    config.browser_config, "max_content_length", 2000
                )

                # 导航操作
                if action == "go_to_url":
                    if not url:
                        return ToolResult(
                            error="URL is required for 'go_to_url' action"
                        )
                    page = await context.get_current_page()
                    await page.goto(url)
                    await page.wait_for_load_state()
                    return ToolResult(output=f"Navigated to {url}")

                elif action == "go_back":
                    await context.go_back()
                    return ToolResult(output="Navigated back")

                elif action == "refresh":
                    await context.refresh_page()
                    return ToolResult(output="Refreshed current page")

                elif action == "web_search":
                    if not query:
                        return ToolResult(
                            error="Query is required for 'web_search' action"
                        )
                    # 执行网页搜索并直接返回结果，无需浏览器导航
                    search_response = await self.web_search_tool.execute(
                        query=query, fetch_content=True, num_results=1
                    )
                    # 导航到第一个搜索结果
                    first_search_result = search_response.results[0]
                    url_to_navigate = first_search_result.url

                    page = await context.get_current_page()
                    await page.goto(url_to_navigate)
                    await page.wait_for_load_state()

                    return search_response

                # 元素交互操作
                elif action == "click_element":
                    if index is None:
                        return ToolResult(
                            error="Index is required for 'click_element' action"
                        )
                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")
                    download_path = await context._click_element_node(element)
                    output = f"Clicked element at index {index}"

                    # 🔧 修复日期选择器：点击后通过 JS 触发 focus + mousedown 事件
                    # 许多自定义日期选择器（如携程）监听 focus 事件而非 click 来弹出日历
                    try:
                        page = await context.get_current_page()
                        element_handle = await context.get_locate_element(element)
                        if element_handle:
                            tag_name = element.tag_name or ""
                            elem_type = (element.attributes or {}).get("type", "").lower()
                            placeholder = (element.attributes or {}).get("placeholder", "").lower()
                            # 检测是否是日期/输入相关元素
                            is_date_related = (
                                tag_name == "input"
                                or elem_type in ("date", "text", "search")
                                or "date" in placeholder
                                or "日期" in placeholder
                            )
                            if is_date_related:
                                await element_handle.evaluate("""(el) => {
                                    el.focus();
                                    el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                                    el.dispatchEvent(new Event('focus', {bubbles: true}));
                                }""")
                                output += " (triggered date picker focus events)"
                    except Exception:
                        pass  # 增强操作失败不影响主流程

                    if download_path:
                        output += f" - Downloaded file to {download_path}"
                    return ToolResult(output=output)

                elif action == "input_text":
                    if index is None or not text:
                        return ToolResult(
                            error="Index and text are required for 'input_text' action"
                        )
                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")

                    # 🔧 修复日期选择器输入：先尝试标准方法，失败后使用 JS 直接设置值
                    try:
                        await context._input_text_element_node(element, text)
                    except Exception:
                        # 标准方法失败（通常因为 readonly 属性），尝试 JS 方式
                        page = await context.get_current_page()
                        element_handle = await context.get_locate_element(element)
                        if element_handle:
                            try:
                                await element_handle.evaluate(f"""(el) => {{
                                    // 移除 readonly 属性
                                    el.removeAttribute('readonly');
                                    el.removeAttribute('disabled');
                                    // 聚焦并设置值
                                    el.focus();
                                    el.value = {json.dumps(text)};
                                    // 触发必要的事件，让日期选择器识别输入
                                    el.dispatchEvent(new Event('input', {{bubbles: true}}));
                                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                                    el.dispatchEvent(new KeyboardEvent('keydown', {{bubbles: true, key: 'Enter'}}));
                                    el.dispatchEvent(new KeyboardEvent('keyup', {{bubbles: true, key: 'Enter'}}));
                                }}""")
                                return ToolResult(
                                    output=f"Input '{text}' into element at index {index} (via JS, removed readonly)"
                                )
                            except Exception as js_err:
                                return ToolResult(
                                    error=f"Failed to input text into index {index}: {str(js_err)}"
                                )
                        return ToolResult(
                            error=f"Element with index {index} not found in page (JS fallback failed)"
                        )
                    return ToolResult(
                        output=f"Input '{text}' into element at index {index}"
                    )

                elif action == "scroll_down" or action == "scroll_up":
                    direction = 1 if action == "scroll_down" else -1
                    amount = (
                        scroll_amount
                        if scroll_amount is not None
                        else context.config.browser_window_size["height"]
                    )
                    await context.execute_javascript(
                        f"window.scrollBy(0, {direction * amount});"
                    )
                    return ToolResult(
                        output=f"Scrolled {'down' if direction > 0 else 'up'} by {amount} pixels"
                    )

                elif action == "scroll_to_text":
                    if not text:
                        return ToolResult(
                            error="Text is required for 'scroll_to_text' action"
                        )
                    page = await context.get_current_page()
                    try:
                        locator = page.get_by_text(text, exact=False)
                        await locator.scroll_into_view_if_needed()
                        return ToolResult(output=f"Scrolled to text: '{text}'")
                    except Exception as e:
                        return ToolResult(error=f"Failed to scroll to text: {str(e)}")

                elif action == "send_keys":
                    if not keys:
                        return ToolResult(
                            error="Keys are required for 'send_keys' action"
                        )
                    page = await context.get_current_page()
                    # 🔧 增强 send_keys：支持输入完整字符串
                    # 单键或组合键（如 "Enter", "Control+a", "Tab"）使用 press()
                    # 多字符字符串使用 type() 模拟真实键盘输入
                    is_single_key = (
                        "+" in keys
                        or keys.lower()
                        in {
                            "enter", "tab", "escape", "backspace", "delete",
                            "arrowup", "arrowdown", "arrowleft", "arrowright",
                            "pageup", "pagedown", "home", "end", "f1", "f2",
                            "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10",
                            "f11", "f12", "space",
                        }
                    )
                    if is_single_key:
                        await page.keyboard.press(keys)
                    else:
                        # 输入完整文本字符串，模拟逐字键盘输入
                        await page.keyboard.type(keys, delay=30)
                    return ToolResult(output=f"Sent keys: {keys}")

                elif action == "get_dropdown_options":
                    if index is None:
                        return ToolResult(
                            error="Index is required for 'get_dropdown_options' action"
                        )
                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")
                    page = await context.get_current_page()
                    options = await page.evaluate(
                        """
                        (xpath) => {
                            const select = document.evaluate(xpath, document, null,
                                XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                            if (!select) return null;
                            return Array.from(select.options).map(opt => ({
                                text: opt.text,
                                value: opt.value,
                                index: opt.index
                            }));
                        }
                    """,
                        element.xpath,
                    )
                    return ToolResult(output=f"Dropdown options: {options}")

                elif action == "select_dropdown_option":
                    if index is None or not text:
                        return ToolResult(
                            error="Index and text are required for 'select_dropdown_option' action"
                        )
                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")
                    page = await context.get_current_page()
                    await page.select_option(element.xpath, label=text)
                    return ToolResult(
                        output=f"Selected option '{text}' from dropdown at index {index}"
                    )

                # 🔧 新增 focus_element 操作：聚焦元素（用于日期选择器等需要先聚焦再输入的场景）
                elif action == "focus_element":
                    if index is None:
                        return ToolResult(
                            error="Index is required for 'focus_element' action"
                        )
                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")
                    page = await context.get_current_page()
                    element_handle = await context.get_locate_element(element)
                    if not element_handle:
                        return ToolResult(error=f"Cannot locate element with index {index}")
                    await element_handle.evaluate("""(el) => {
                        el.focus();
                        el.dispatchEvent(new Event('focus', {bubbles: true}));
                        el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                    }""")
                    return ToolResult(
                        output=f"Focused element at index {index} (dispatched focus+mousedown events)"
                    )

                # 🔧 新增 type_text 操作：通过键盘逐字输入文本（触发所有键盘事件）
                elif action == "type_text":
                    if not text:
                        return ToolResult(
                            error="Text is required for 'type_text' action"
                        )
                    page = await context.get_current_page()
                    await page.keyboard.type(text, delay=30)
                    return ToolResult(
                        output=f"Typed '{text}' via keyboard (simulated real typing)"
                    )

                # 内容提取操作
                elif action == "extract_content":
                    if not goal:
                        return ToolResult(
                            error="Goal is required for 'extract_content' action"
                        )

                    page = await context.get_current_page()
                    import markdownify

                    content = markdownify.markdownify(await page.content())

                    prompt = f"""\
Your task is to extract the content of the page. You will be given a page and a goal, and you should extract all relevant information around this goal from the page. If the goal is vague, summarize the page. Respond in json format.
Extraction goal: {goal}

Page content:
{content[:max_content_length]}
"""
                    messages = [{"role": "system", "content": prompt}]

                    # 定义提取函数模式
                    extraction_function = {
                        "type": "function",
                        "function": {
                            "name": "extract_content",
                            "description": "Extract specific information from a webpage based on a goal",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "extracted_content": {
                                        "type": "object",
                                        "description": "The content extracted from the page according to the goal",
                                        "properties": {
                                            "text": {
                                                "type": "string",
                                                "description": "Text content extracted from the page",
                                            },
                                            "metadata": {
                                                "type": "object",
                                                "description": "Additional metadata about the extracted content",
                                                "properties": {
                                                    "source": {
                                                        "type": "string",
                                                        "description": "Source of the extracted content",
                                                    }
                                                },
                                            },
                                        },
                                    }
                                },
                                "required": ["extracted_content"],
                            },
                        },
                    }

                    # 使用 LLM 通过必需的函数调用来提取内容
                    response = await self.llm.ask_tool(
                        messages,
                        tools=[extraction_function],
                        tool_choice="required",
                    )

                    if response and response.tool_calls:
                        args = json.loads(response.tool_calls[0].function.arguments)
                        extracted_content = args.get("extracted_content", {})
                        return ToolResult(
                            output=f"Extracted from page:\n{extracted_content}\n"
                        )

                    return ToolResult(output="No content was extracted from the page.")

                # 标签页管理操作
                elif action == "switch_tab":
                    if tab_id is None:
                        return ToolResult(
                            error="Tab ID is required for 'switch_tab' action"
                        )
                    await context.switch_to_tab(tab_id)
                    page = await context.get_current_page()
                    await page.wait_for_load_state()
                    return ToolResult(output=f"Switched to tab {tab_id}")

                elif action == "open_tab":
                    if not url:
                        return ToolResult(error="URL is required for 'open_tab' action")
                    await context.create_new_tab(url)
                    return ToolResult(output=f"Opened new tab with {url}")

                elif action == "close_tab":
                    await context.close_current_tab()
                    return ToolResult(output="Closed current tab")

                # 实用操作
                elif action == "wait":
                    seconds_to_wait = seconds if seconds is not None else 3
                    await asyncio.sleep(seconds_to_wait)
                    return ToolResult(output=f"Waited for {seconds_to_wait} seconds")

                # 📅 DOM 定向选择日期：日期选择器的首选路径，绕开视觉坐标精度问题
                elif action == "select_date":
                    if not text:
                        return ToolResult(
                            error="Text (date) is required for 'select_date' action"
                        )
                    return await self._execute_select_date(context, text, index)

                # 🖼️ 视觉坐标操作：与 DOM 索引操作并列，使用 GUI 视觉模型按像素坐标驱动
                elif action == "gui_action":
                    if not task:
                        return ToolResult(
                            error="Task is required for 'gui_action' action"
                        )
                    return await self._execute_gui_action(context, task)

                else:
                    return ToolResult(error=f"Unknown action: {action}")

            except Exception as e:
                return ToolResult(error=f"Browser action '{action}' failed: {str(e)}")

    async def _execute_select_date(
        self,
        context: BrowserContext,
        date_text: str,
        index: Optional[int] = None,
    ) -> ToolResult:
        """DOM 定向选择日期：打开日历 -> JS 在 DOM 里定位日期格 -> 真实鼠标点击 -> JS 兜底设值。

        这是日期选择器的首选路径，绕开视觉坐标的精度问题。对携程等把日历渲染成
        DOM（但未被 browser_use 列入交互元素）的站点同样适用，且站点无关。

        策略优先级：
        1. 若给了 index，先聚焦并点击该输入框，触发日历弹出；
        2. 用 JS 在 DOM 里定位目标日期格（属性匹配 ISO/中文日期，或月份感知的日号匹配），
           取其视口中心坐标后用真实鼠标点击（触发站点组件的内部状态更新，比直接设值可靠）；
        3. 兜底：直接在输入框上用 JS 设值并派发 input/change 事件。
        """
        parsed = _parse_date(date_text)
        if not parsed:
            return ToolResult(
                error=f"无法解析日期 '{date_text}'，请用如 '2026-07-01' 或 '7月1日' 的格式"
            )
        parsed = _infer_year(parsed)  # 缺年份时补全，保证 ISO 与 JS 兜底可用
        page = await context.get_current_page()
        logs: list[str] = []

        # 1. 打开日历：聚焦 + 点击输入框
        input_element = None
        if index is not None:
            input_element = await context.get_dom_element_by_index(index)
            if input_element:
                handle = await context.get_locate_element(input_element)
                if handle:
                    try:
                        await handle.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    try:
                        await handle.click(timeout=3000)
                    except Exception:
                        # 点击被拦截则用 JS 触发聚焦事件
                        try:
                            await handle.evaluate(
                                "(el)=>{el.focus();"
                                "el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));"
                                "el.dispatchEvent(new Event('focus',{bubbles:true}));}"
                            )
                        except Exception:
                            pass
                    await page.wait_for_timeout(_GUI_SETTLE_MS)
                    logs.append(f"已点击输入框 index={index} 打开日历")

        # 2. 用 JS 在 DOM 里精确定位目标日期格，返回其视口中心坐标，再用真实鼠标点击。
        #    站点无关：先按属性(data-date/aria-label/title 含 ISO 或中文日期)匹配，
        #    再退化到「月份感知的日号匹配」(在含 '7月'/'2026-07' 的容器里找文本为 '1' 的格)。
        iso = f"{parsed['year']:04d}-{parsed['month']:02d}-{parsed['day']:02d}"
        finder_args = {
            "iso": iso,
            "cn": f"{parsed['month']}月{parsed['day']}日",
            "cnFull": f"{parsed['year']}年{parsed['month']}月{parsed['day']}日",
            "day": parsed["day"],
            "monthLabel": f"{parsed['month']}月",
        }
        try:
            found = await page.evaluate(_DATE_CELL_FINDER_JS, finder_args)
        except Exception as e:
            found = None
            logs.append(f"JS 定位异常：{e}")

        if found and found.get("found"):
            await page.mouse.click(found["x"], found["y"])
            logs.append(f"JS 定位({found.get('via')})->鼠标点击({found['x']:.0f},{found['y']:.0f})")
            return ToolResult(
                output=f"[select_date] 选中 {date_text}(ISO={iso}) | " + " -> ".join(logs)
            )
        logs.append(
            f"JS 未定位到日期格(页面 data-date 元素数={found.get('dataDateCount') if found else 'NA'})"
        )

        # 3. 兜底：JS 直接在输入框设值并派发事件
        if input_element:
            handle = await context.get_locate_element(input_element)
            if handle:
                try:
                    await handle.evaluate(
                        "(el, v)=>{el.removeAttribute('readonly');"
                        "el.removeAttribute('disabled');el.focus();el.value=v;"
                        "el.dispatchEvent(new Event('input',{bubbles:true}));"
                        "el.dispatchEvent(new Event('change',{bubbles:true}));}",
                        iso,
                    )
                    logs.append(f"已用 JS 在输入框设值 {iso}")
                    return ToolResult(
                        output=f"[select_date] {date_text} (JS 兜底设值) | " + " -> ".join(logs)
                    )
                except Exception as e:
                    logs.append(f"JS 兜底失败：{e}")

        return ToolResult(
            error="[select_date] 未能选中日期 "
            f"{date_text}（ISO={iso}）。" + " -> ".join(logs)
            + "。可改用 gui_action 视觉方式，或检查日历是否已打开。"
        )

    async def _execute_gui_action(
        self, context: BrowserContext, task: str
    ) -> ToolResult:
        """视觉坐标操作：内部「截图 -> 决策 -> 执行 -> 再截图确认」循环。

        与 DOM 索引操作并列的交互路径，由 LLM 自主选择。尤其适合目标控件未出现在
        DOM 元素列表中、或自定义控件（日历、地图、画布、富文本）难以被 DOM 树捕获时。

        [为什么要循环] page.mouse.click() 点在任何位置都不会报错，单发模式下工具
        无法区分「真的点中目标」和「点了空白」，导致外层 LLM 误以为成功、或反复换
        方式试探。这里让视觉模型自洽完成一个子目标：每执行一个原子操作后重新截图，
        模型基于最新画面确认目标是否达成（输出 FINISH）或修正坐标重试，直到完成、
        失败（FAILE）或达到步数上限 _GUI_MAX_ITERATIONS。

        [坐标换算] 视觉模型返回 0-1000 归一化坐标，按 css = norm/1000 * 视口CSS边长
        映射到 page.mouse 的视口 CSS 像素空间。

        [重要] 必须独立截视口截图（full_page=False），不要复用 get_current_state()
        的整页截图：整页截图坐标系（可达数千像素）与 page.mouse 的视口坐标系不一致，
        会导致点击落在视口外。视口截图与 page.mouse 天然共享同一坐标系。
        """
        page = await context.get_current_page()
        await page.bring_to_front()

        outputs: list[str] = []
        history: list[dict] = []
        last_signature: Optional[str] = None
        last_url: Optional[str] = None

        for iteration in range(1, _GUI_MAX_ITERATIONS + 1):
            await page.wait_for_load_state()
            # [关键] 截图前移除 browser_use 注入的红色索引高亮框（playwright-highlight-container）。
            # 否则发给视觉模型的截图满屏红框+数字标签，严重干扰元素定位（grounding），
            # 模型会从"看"退化成"猜"。视口截图与 page.mouse 共享坐标系，移除高亮不影响点击。
            await context.remove_highlights()
            screenshot_bytes = await page.screenshot(
                full_page=False, animations="disabled", type="png"
            )
            base64_image = base64.b64encode(screenshot_bytes).decode("utf-8")
            css_w, css_h, img_w, img_h, dpr = await self._gui_viewport_metrics(
                page, screenshot_bytes
            )
            debug_path = self._save_gui_debug_screenshot(screenshot_bytes)
            logger.info(
                f"🖼️ GUI[{iteration}/{_GUI_MAX_ITERATIONS}] 截图={img_w}x{img_h}px, "
                f"视口CSS={css_w}x{css_h}, dpr={dpr}, 截图={debug_path}"
            )

            current_url = page.url

            try:
                decision = await query_gui_action(
                    base64_image, task, history=history
                )
            except Exception as e:
                return ToolResult(error=f"GUI vision model failed: {str(e)}")

            # 防死锁：仅当「相同动作」且「页面未发生跳转」时才判为原地空转。
            # [关键修正] 若上一步点击已触发跳转（URL 变了），即便模型想点同一坐标，
            # 也是在新页面上的操作，不算空转——否则会把"点击成功并跳转"误判为卡死
            # （实测携程点搜索后跳转，下一轮被误报 error）。配合操作后等待导航稳定，
            # 下一轮通常能看到新页面而自然 FINISH。
            signature = self._gui_action_signature(decision)
            if signature == last_signature and current_url == last_url:
                outputs.append(
                    f"[{iteration}] 检测到重复动作 {signature} 且页面无跳转，视觉定位"
                    "疑似失败/卡死，已中止。建议改用 DOM 定向操作（如 select_date 选日期、"
                    "click_element 按索引点击），不要继续用 gui_action 重试同一目标。"
                )
                return ToolResult(error="[GUI] " + " | ".join(outputs))
            last_signature = signature
            last_url = current_url

            outcome = await self._apply_gui_atomic_action(
                page=page,
                decision=decision,
                css_w=css_w,
                css_h=css_h,
                img_w=img_w,
                img_h=img_h,
                dpr=dpr,
                screenshot_bytes=screenshot_bytes,
                debug_path=debug_path,
            )
            outputs.append(f"[{iteration}] {outcome['message']}")
            history.append(
                {
                    "action": decision["action"],
                    "thought": decision["thought"],
                    "parameters": decision["parameters"],
                }
            )

            if outcome["error"]:
                return ToolResult(error="[GUI] " + " | ".join(outputs))
            if outcome["done"]:
                break

            # 等页面响应（如日历回填、面板关闭，或点击触发的页面跳转）再进入下一轮确认。
            # 等导航稳定可避免在跳转途中截到旧页面而把"已成功跳转"误判为重复动作。
            await self._wait_for_gui_settle(page)
        else:
            outputs.append(
                f"(达到最大步数 {_GUI_MAX_ITERATIONS}，未收到 FINISH，"
                "请查看当前页面状态判断是否已完成)"
            )

        return ToolResult(output="[GUI] " + " | ".join(outputs))

    @staticmethod
    def _gui_action_signature(decision: dict) -> str:
        """生成动作指纹，用于检测「原地空转」。

        CLICK 含坐标（取整到 10px 容忍微小抖动），其余动作含关键参数。
        连续两步指纹相同即视为卡死。
        """
        action = decision.get("action", "")
        params = decision.get("parameters", {}) or {}
        if action == "CLICK":
            try:
                x = round(float(params.get("x", 0)) / 10) * 10
                y = round(float(params.get("y", 0)) / 10) * 10
            except (TypeError, ValueError):
                x, y = params.get("x"), params.get("y")
            return f"CLICK:{x},{y}"
        if action == "TYPE":
            return f"TYPE:{params.get('text', '')}"
        if action == "KEY_PRESS":
            return f"KEY_PRESS:{params.get('key', '')}"
        if action == "SCROLL":
            return f"SCROLL:{params.get('direction', '')}:{params.get('amount', '')}"
        return action

    @staticmethod
    def _normalize_key_press(key: str) -> str:
        """把视觉模型给的功能键名规范成 Playwright 接受的键名。

        处理组合键（'alt+f4' -> 'Alt+F4'）、别名（'esc' -> 'Escape'）、
        功能键（'f4' -> 'F4'）；单字符原样保留（Playwright 接受 'a'）。
        """
        import re

        parts = [p for p in key.replace(" ", "").split("+") if p]
        normalized_parts = []
        for part in parts:
            low = part.lower()
            if low in _MODIFIER_ALIASES:
                normalized_parts.append(_MODIFIER_ALIASES[low])
            elif low in _KEY_ALIASES:
                normalized_parts.append(_KEY_ALIASES[low])
            elif re.fullmatch(r"f\d{1,2}", low):  # 功能键 f1..f12
                normalized_parts.append("F" + low[1:])
            elif len(part) == 1:
                normalized_parts.append(part)  # 单字符（字母/数字/符号）原样
            else:
                normalized_parts.append(part.capitalize())  # 兜底
        return "+".join(normalized_parts) if normalized_parts else key

    @staticmethod
    async def _wait_for_gui_settle(page) -> None:
        """原子操作后等待页面稳定：固定停顿 + 等待可能的导航/网络空闲。

        点击若触发跳转，必须等跳转完成再截下一张图，否则会截到旧页面、模型重复
        同一动作而被防死锁误判。networkidle 等不到也无妨（超时即返回）。
        """
        await page.wait_for_timeout(_GUI_SETTLE_MS)
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=_GUI_NAV_TIMEOUT_MS
            )
        except Exception:
            # 已经稳定、或站点长连接导致 networkidle 永不触发，均按已稳定处理
            pass

    async def _gui_viewport_metrics(self, page, screenshot_bytes: bytes):
        """返回 (css_w, css_h, img_w, img_h, dpr)。

        img_*：截图实际像素尺寸；css_*：page.mouse 使用的视口 CSS 尺寸。
        """
        from io import BytesIO

        from PIL import Image

        img_w, img_h = Image.open(BytesIO(screenshot_bytes)).size
        viewport = await page.evaluate(
            "() => ({ w: window.innerWidth, h: window.innerHeight,"
            " dpr: window.devicePixelRatio })"
        )
        css_w = viewport.get("w") or img_w
        css_h = viewport.get("h") or img_h
        dpr = viewport.get("dpr") or 1
        return css_w, css_h, img_w, img_h, dpr

    def _save_gui_debug_screenshot(self, screenshot_bytes: bytes) -> Optional[str]:
        """把发给视觉模型的截图落盘，便于人工核对坐标是否落在目标上。"""
        import os
        from datetime import datetime

        try:
            debug_dir = "screenshots"
            os.makedirs(debug_dir, exist_ok=True)
            # 用 datetime 取到微秒，循环内多张截图不会互相覆盖
            # （time.strftime 不支持 %f，会抛 ValueError）
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            debug_path = os.path.join(debug_dir, f"gui_action_{stamp}.png")
            with open(debug_path, "wb") as f:
                f.write(screenshot_bytes)
            return debug_path
        except Exception as e:
            logger.debug(f"🖼️ GUI 截图落盘失败（不影响主流程）：{e}")
            return None

    def _mark_gui_click(
        self,
        screenshot_bytes: bytes,
        debug_path: Optional[str],
        px_x: float,
        px_y: float,
        img_w: int,
        img_h: int,
    ) -> None:
        """在落盘截图上标出点击点（图片像素空间），便于肉眼核对是否点中。

        视觉模型返回的就是相对截图图片的绝对像素坐标，故直接落在图片上即可，
        与 _apply_gui_atomic_action 的换算同源，标记位置=模型给的点。
        """
        if not debug_path:
            return
        try:
            from io import BytesIO

            from PIL import Image, ImageDraw

            marked = Image.open(BytesIO(screenshot_bytes)).convert("RGB")
            # 裁剪到图片范围内，模型偶尔越界的坐标不会画到画布外
            mx = max(0.0, min(float(px_x), float(img_w)))
            my = max(0.0, min(float(px_y), float(img_h)))
            draw = ImageDraw.Draw(marked)
            r = 14
            draw.line([(mx - r, my), (mx + r, my)], fill=(255, 0, 0), width=3)
            draw.line([(mx, my - r), (mx, my + r)], fill=(255, 0, 0), width=3)
            draw.ellipse(
                [(mx - r, my - r), (mx + r, my + r)],
                outline=(255, 0, 0),
                width=3,
            )
            marked_path = debug_path.replace(".png", "_click.png")
            marked.save(marked_path)
            logger.info(f"🖼️ GUI 点击标记图已保存：{marked_path}")
        except Exception as e:
            logger.debug(f"🖼️ GUI 点击标记绘制失败（不影响主流程）：{e}")

    async def _apply_gui_atomic_action(
        self,
        page,
        decision: dict,
        css_w: int,
        css_h: int,
        img_w: int,
        img_h: int,
        dpr: float,
        screenshot_bytes: bytes,
        debug_path: Optional[str],
    ) -> dict:
        """执行单个视觉原子操作。

        Returns:
            dict: {"message": str, "done": bool, "error": bool}
            done=True 表示子目标完成（FINISH），应结束循环；
            error=True 表示该步失败（FAILE 或执行异常），应中止循环。
        """
        gui_action = decision["action"]
        params = decision["parameters"]
        thought = decision["thought"]

        try:
            if gui_action == "CLICK":
                x = params.get("x")
                y = params.get("y")
                if x is None or y is None:
                    return {
                        "message": f"CLICK missing coordinates: {params}",
                        "done": False,
                        "error": True,
                    }
                # 视觉模型返回的是【相对截图图片的绝对像素坐标】，需换算成 page.mouse
                # 使用的视口 CSS 坐标。对 full_page=False 视口截图恒有：
                #   截图像素 = 视口CSS × dpr   =>   视口CSS = 截图像素 ÷ dpr
                # 只用 dpr 换算，不依赖 window.innerWidth/innerHeight——后者会被滚动条、
                # 浏览器渲染差异影响，与截图尺寸对不齐（实测 innerHeight 1100 但实际可点
                # 区域约 1040），用它当分母会引入垂直偏移。dpr=1 时等价于直接使用原值。
                effective_dpr = dpr or 1.0
                css_x = float(x) / effective_dpr
                css_y = float(y) / effective_dpr
                logger.info(
                    f"🖼️ GUI CLICK 图片像素坐标=({x},{y}) [图{img_w}x{img_h}, dpr={dpr}] -> "
                    f"视口CSS坐标=({css_x:.1f},{css_y:.1f}) [视口{css_w}x{css_h}]"
                )
                self._mark_gui_click(
                    screenshot_bytes, debug_path, x, y, img_w, img_h
                )
                await page.mouse.click(css_x, css_y)
                desc = params.get("description", "")
                return {
                    "message": f"Clicked norm({x},{y})->css({css_x:.0f},{css_y:.0f}) {desc} | {thought}",
                    "done": False,
                    "error": False,
                }

            if gui_action == "TYPE":
                text_to_type = params.get("text", "")
                await page.keyboard.type(text_to_type, delay=30)
                if params.get("needs_enter"):
                    await page.keyboard.press("Enter")
                suffix = " + Enter" if params.get("needs_enter") else ""
                return {
                    "message": f"Typed '{text_to_type}'{suffix} | {thought}",
                    "done": False,
                    "error": False,
                }

            if gui_action == "SCROLL":
                direction = params.get("direction", "down")
                amount_map = {"small": 300, "medium": 600, "large": 1000}
                pixels = amount_map.get(params.get("amount", "medium"), 600)
                delta = pixels if direction == "down" else -pixels
                await page.mouse.wheel(0, delta)
                return {
                    "message": f"Scrolled {direction} by {pixels}px | {thought}",
                    "done": False,
                    "error": False,
                }

            if gui_action == "KEY_PRESS":
                key = params.get("key", "")
                if not key:
                    return {
                        "message": f"KEY_PRESS missing key: {params}",
                        "done": False,
                        "error": True,
                    }
                normalized = self._normalize_key_press(key)
                await page.keyboard.press(normalized)
                return {
                    "message": f"Pressed key '{key}'->'{normalized}' | {thought}",
                    "done": False,
                    "error": False,
                }

            if gui_action == "FINISH":
                message = params.get("message", "Task completed")
                return {"message": f"FINISH: {message}", "done": True, "error": False}

            if gui_action == "FAILE":
                reason = params.get("reason", "Unknown reason")
                return {"message": f"FAILED: {reason}", "done": False, "error": True}

            return {
                "message": f"Unknown GUI action from vision model: {gui_action}",
                "done": False,
                "error": True,
            }
        except Exception as e:
            return {
                "message": f"action '{gui_action}' execution failed: {str(e)}",
                "done": False,
                "error": True,
            }

    async def get_current_state(
        self, context: Optional[BrowserContext] = None
    ) -> ToolResult:
        """
        获取当前浏览器状态作为 ToolResult。
        如果未提供 context，则使用 self.context。
        """
        try:
            # 使用提供的 context 或回退到 self.context
            ctx = context or self.context
            if not ctx:
                return ToolResult(error="Browser context not initialized")

            state = await ctx.get_state()

            # 如果不存在，创建 viewport_info 字典
            viewport_height = 0
            if hasattr(state, "viewport_info") and state.viewport_info:
                viewport_height = state.viewport_info.height
            elif hasattr(ctx, "config") and hasattr(ctx.config, "browser_window_size"):
                viewport_height = ctx.config.browser_window_size.get("height", 0)

            # 为状态拍摄截图
            page = await ctx.get_current_page()

            await page.bring_to_front()
            await page.wait_for_load_state()

            screenshot = await page.screenshot(
                full_page=True, animations="disabled", type="jpeg", quality=100
            )

            screenshot = base64.b64encode(screenshot).decode("utf-8")
            screenshot_size_kb = len(screenshot) * 3 / 4 / 1024  # 估算图片大小（KB）

            # 获取可交互元素信息
            interactive_elements_str = (
                state.element_tree.clickable_elements_to_string()
                if state.element_tree
                else ""
            )
            element_count = interactive_elements_str.count("[") if interactive_elements_str else 0

            # 调试信息
            logger.info(f"🌐 Browser state captured: URL={state.url}, Title={state.title}")
            logger.info(f"📸 Screenshot size: {screenshot_size_kb:.2f} KB (base64)")
            logger.info(f"🔍 Interactive elements detected: {element_count}")
            if element_count == 0:
                logger.warning(f"⚠️ No interactive elements found - page may be empty or not loaded")
            if interactive_elements_str:
                # 显示前几个元素作为示例
                lines = interactive_elements_str.split("\n")[:5]
                preview = "\n".join(lines)
                logger.debug(f"🔍 Elements preview (first 5):\n{preview}")

            # 构建包含所有必需字段的状态信息
            state_info = {
                "url": state.url,
                "title": state.title,
                "tabs": [tab.model_dump() for tab in state.tabs],
                "help": "[0], [1], [2], etc., represent clickable indices corresponding to the elements listed. Clicking on these indices will navigate to or interact with the respective content behind them.",
                "interactive_elements": interactive_elements_str,
                "scroll_info": {
                    "pixels_above": getattr(state, "pixels_above", 0),
                    "pixels_below": getattr(state, "pixels_below", 0),
                    "total_height": getattr(state, "pixels_above", 0)
                    + getattr(state, "pixels_below", 0)
                    + viewport_height,
                },
                "viewport_height": viewport_height,
            }

            return ToolResult(
                output=json.dumps(state_info, indent=4, ensure_ascii=False),
                base64_image=screenshot,
            )
        except Exception as e:
            return ToolResult(error=f"Failed to get browser state: {str(e)}")

    async def cleanup(self):
        """清理浏览器资源。"""
        async with self.lock:
            if self.context is not None:
                await self.context.close()
                self.context = None
                self.dom_service = None
            if self.browser is not None:
                await self.browser.close()
                self.browser = None

    def __del__(self):
        """确保在对象销毁时进行清理。"""
        if self.browser is not None or self.context is not None:
            try:
                asyncio.run(self.cleanup())
            except RuntimeError:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(self.cleanup())
                loop.close()

    @classmethod
    def create_with_context(cls, context: Context) -> "BrowserUseTool[Context]":
        """创建具有特定上下文的 BrowserUseTool 的工厂方法。"""
        tool = cls()
        tool.tool_context = context
        return tool
