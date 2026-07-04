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

弹窗/遮罩挡路时（重要）：
* 页面出现营销弹窗、cookie 提示、引导蒙层等遮挡了主内容、点不到目标元素时，
  请优先用 action="close_popup"。它会用 DOM 语义定位关闭按钮（×/关闭/close）并真实点击，
  再补一发 Escape 兜底，比用 gui_action 猜 × 图标坐标稳得多。
* 不要反复用 gui_action 去点同一个关闭按钮——视觉坐标差几像素就点空，连续两次点同一
  坐标会被防死锁中止。先 close_popup 关掉弹窗，再继续后续操作。

在页面执行 JS 读数据 / 下载图片（execute_js，重要）：
* action="execute_js" 在【当前已打开的页面】上执行 JavaScript，返回值序列化回传。
  这是「DOM 没有交互抓手、但要读取属性/文本/图片 src」时的首选，比 extract_content 稳、比
  gui_action 右键/F12 可靠。别再用那些方式去拿图片 URL 或列表字段。
* 读取字段示例（一次性批量读，减少来回）：
  script="() => ({name: document.querySelector('.title')?.innerText, img: document.querySelector('img')?.src})"
* [取商品图的首选] 用页面自身的 fetch 会话内下载图片转 base64（自动带 cookie/referer，
  避开 CDN 的 referer 校验导致的 403），返回 dataURL（不会被截断）：
  script="async () => { const url = document.querySelector('img').src; const resp = await fetch(url, {credentials:'include'}); const blob = await resp.blob(); return await new Promise(r => { const fr = new FileReader(); fr.onload = () => r({url, dataURL: fr.result}); fr.readAsDataURL(blob); }); }"
  拿到 dataURL 后，用 python_execute 把 base64 解码写成本地图片文件，再把该路径传给 wps_excel_tool 的 image_path。

1688 以图搜图（paste_image，首选，实测可用）：
* action="paste_image"，file_path 传本地商品图绝对路径。这是 1688 以图搜图的**首选且唯一稳的**路径。
* 它一步走完整套：点「以图搜款」相机唤出面板 -> 把图写进系统剪贴板 -> 发真实 Ctrl+V 把图粘进面板 ->
  点「搜索图片」按钮 -> 结果开在新标签页时自动切过去，返回里给出结果页 URL。
* [为什么不用 upload_file 搜图] 走 <input type=file> 会弹系统原生「打开」对话框，那是 OS 级模态，
  会**冻住整个浏览器自动化**（页面变 about:blank、后续全卡死）。paste_image 全程不碰文件框，绕开该坑。
* [重要] 返回显示「→ 图搜结果页」即成功，直接在结果页继续挑同款、读采购价；
  绝不要退化成关键词搜索——关键词经常召回完全不相干的品类（实测把毛绒玩偶搜成锅具）。
  若提示「未检测到跳转/图片没粘进面板」，多为面板未打开，wait 后重试 paste_image 即可。
* 依赖：pillow + pywin32（本机已装），仅 Windows。

批量清理标签页（close_tabs，连续采集必备）：
* action="close_tabs"，text 传 URL 子串，关闭所有 URL 含该子串的标签页。
* 连续采集多个商品时，每采完一个就 close_tabs(text="1688") 把这次开的图搜结果页/详情页
  一并关掉，只留 Temu 工作标签，防止标签越积越多拖慢、并干扰「当前页」判定。
* 安全：绝不会关到零标签（全命中则保留最后一个），关完自动切到一个存活标签。

上传本地文件（upload_file，用于普通文件上传，不是搜图）：
* action="upload_file" 把本地文件塞进页面的 <input type=file>，用 file_path 传本地绝对路径。
  适合表单附件、头像等普通上传；对隐藏 input 也有效。1688 以图搜图请改用 paste_image。
* 不给 index 时自动找页面里的 input[type=file]（index 可作第几个的序号）；给了 index 则定位该元素。
* 若上传框藏在点击后才出现的弹层里，先 click_element 触发，再 upload_file。
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

# gui_action SCROLL 的滚动幅度：按【当前视口高度】的比例算，而非写死像素。
# 这样在任意分辨率/缩放/窗口尺寸下，一次 medium 始终约滚动 60% 视口，行为一致。
_SCROLL_VIEWPORT_FRACTIONS = {"small": 0.3, "medium": 0.6, "large": 0.9}

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


# 站点无关地在 DOM 中定位「弹窗/遮罩的关闭按钮」并返回其视口中心坐标的 JS。
# 适用场景：营销弹窗、cookie 提示、引导蒙层等遮挡了主内容、且关闭按钮是小图标
# （×/✕）时——视觉模型靠猜坐标点关闭按钮极易差几像素而失败（实测连点两次同一
# 坐标被防死锁中止）。这里用 DOM 语义精确定位，比视觉坐标稳得多。
#
# 策略（按可信度排序，命中即返回最靠上层的那个）：
#   1) 语义属性匹配：aria-label/title/class/id 含 close/关闭/dismiss 等的可点击元素；
#   2) 文本图标匹配：文本恰为 ×/✕/⨯/x/X 的小尺寸可点击元素（典型关闭图标）；
# 仅在「可见 + 处于高层叠（position fixed/absolute 或祖先 z-index 较高）」的元素里找，
# 避免误点正文里的普通按钮。绝不匹配「确定/取消/提交」这类有副作用的语义词。
_POPUP_CLOSER_JS = r"""
() => {
  const vis = (e) => {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(e);
    if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0)
      return false;
    // 必须落在视口内（关闭按钮一定可见才点得到）
    return r.bottom > 0 && r.right > 0
      && r.top < window.innerHeight && r.left < window.innerWidth;
  };
  const ctr = (e) => {
    const r = e.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  };
  // 元素是否处于「浮层」中：自身或祖先为 fixed/absolute 定位（弹窗/蒙层的典型布局）。
  const inOverlay = (e) => {
    let p = e, hops = 0;
    while (p && hops < 12) {
      const pos = getComputedStyle(p).position;
      if (pos === 'fixed' || pos === 'absolute' || pos === 'sticky') return true;
      p = p.parentElement; hops++;
    }
    return false;
  };
  // 关闭语义关键字（属性匹配用）。只含「关闭」意图，绝不含确定/提交等有副作用的词。
  const CLOSE_RE = /(^|[\s_-])(close|dismiss|关闭|×|✕|关掉|close-?(btn|icon|button)|modal-?close|dialog-?close|popup-?close)([\s_-]|$)/i;
  // 文本恰为这些字符的，视为关闭图标。
  const ICON_TEXT = new Set(['×', '✕', '⨯', '╳', '✖', 'x', 'X', '关闭']);

  const score = (e) => {
    // 越靠上层（z-index 越大）越优先；同层取尺寸更像图标按钮的。
    let z = 0, p = e, hops = 0;
    while (p && hops < 12) {
      const v = parseInt(getComputedStyle(p).zIndex, 10);
      if (!isNaN(v)) z = Math.max(z, v);
      p = p.parentElement; hops++;
    }
    return z;
  };

  const candidates = [];
  // 1) 语义属性匹配：限定在可点击/可聚焦元素上，减少误命中。
  const clickable = 'button,a,[role=button],[role=close],i,span,div,svg,[tabindex]';
  for (const e of document.querySelectorAll(clickable)) {
    if (!vis(e) || !inOverlay(e)) continue;
    const attrs = [
      e.getAttribute('aria-label'), e.getAttribute('title'),
      e.getAttribute('class'), e.getAttribute('id'),
      e.getAttribute('data-role'), e.getAttribute('data-testid'),
    ].filter(Boolean).join(' ');
    if (CLOSE_RE.test(attrs)) {
      candidates.push({ e, via: 'attr', z: score(e) });
    }
  }
  // 2) 文本图标匹配：文本恰为关闭图标字符、且是小叶子节点（典型 × 按钮）。
  for (const e of document.querySelectorAll(clickable)) {
    if (!vis(e) || !inOverlay(e)) continue;
    const t = (e.textContent || '').trim();
    if (ICON_TEXT.has(t) && e.children.length <= 1) {
      const r = e.getBoundingClientRect();
      // 关闭图标通常很小；过大的（如整块文字"关闭账号"）已被文本精确匹配排除
      if (r.width <= 80 && r.height <= 80) {
        candidates.push({ e, via: 'iconText', z: score(e) });
      }
    }
  }
  if (!candidates.length) {
    return { found: false, overlayCount:
      document.querySelectorAll('[class*=modal],[class*=popup],[class*=dialog],[class*=mask],[class*=overlay]').length };
  }
  // 取层叠最高的候选（最可能是最上层弹窗的关闭按钮）
  candidates.sort((a, b) => b.z - a.z);
  const best = candidates[0];
  try { best.e.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
  return { found: true, via: best.via, z: best.z, count: candidates.length, ...ctr(best.e) };
}
"""


# 点击 1688「以图搜款」相机入口，唤出以图搜图上传浮层（让 paste 监听器就位）的 JS。
# 只显示面板、不弹系统文件框（那是浮层里「从本地上传」按钮才会触发的）。
# 精确匹配入口文本且限制尺寸，避免命中包含该词的大容器。
_IMAGE_SEARCH_ENTRY_JS = r"""
() => {
  const WORDS = /^(以图搜款|图片搜索|拍照搜|以图搜图)$/;
  for (const e of document.querySelectorAll('*')) {
    const t = (e.textContent || '').trim();
    if (!WORDS.test(t)) continue;
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.width > 160) continue;
    e.scrollIntoView({ block: 'center', inline: 'center' });
    e.click();
    return { armed: true, text: t };
  }
  return { armed: false };
}
"""


# 在上传图片后的「以图搜图」面板里定位真正的「搜索图片/搜同款」按钮并点击的 JS。
# [关键] 必须精确匹配按钮文本并限制其尺寸：1688 的上传浮层里，最外层容器的
# textContent 会把「已上传1张图片搜索图片支持如下图搜同款...」整段都算进去，
# 用宽松的 includes('搜索图片') 会命中整块浮层而不是那颗按钮（实测 .click()
# 点在容器上不触发搜索）。这里要求文本恰为按钮词、且是尺寸像按钮的小元素。
_IMAGE_SEARCH_BUTTON_JS = r"""
() => {
  // [关键] 只匹配「确认搜索」按钮词，绝不含入口词「以图搜款/图片搜索」——那是
  // 唤出上传面板的相机入口，DOM 顺序里排在前面，误点它会把面板切关、丢掉已粘贴的图。
  const WORDS = /^(搜索图片|搜同款|立即搜索|开始搜索|搜一搜|search)$/i;
  const vis = (e) => {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(e);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && +cs.opacity !== 0;
  };
  const clickable = 'button,a,[role=button],span,div,i';
  for (const e of document.querySelectorAll(clickable)) {
    if (!vis(e)) continue;
    const t = (e.textContent || '').trim();
    if (!WORDS.test(t)) continue;
    const r = e.getBoundingClientRect();
    // 按钮通常不大；过大的元素是把整段说明文字都包住的容器，排除掉
    if (r.width > 260 || r.height > 120) continue;
    e.scrollIntoView({ block: 'center', inline: 'center' });
    e.click();
    return { clicked: true, text: t, w: Math.round(r.width), h: Math.round(r.height) };
  }
  return { clicked: false };
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
                    "close_tabs",
                    "focus_element",
                    "type_text",
                    "select_date",
                    "gui_action",
                    "close_popup",
                    "execute_js",
                    "upload_file",
                    "paste_image",
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
                "description": "用于 'input_text'、'type_text'、'scroll_to_text'、'select_dropdown_option' 操作的文本，或 'select_date' 的日期（如 '2026-07-01' 或 '7月1日'），或 'close_tabs' 要关闭标签的 URL 子串（如 '1688'）",
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
            "script": {
                "type": "string",
                "description": "用于 'execute_js' 操作：在当前页面执行的 JavaScript。返回值会被序列化回传（支持 async 箭头函数）。用于读取 DOM 属性/文本/图片 src，或用页面自身 fetch 会话内下载图片转 dataURL",
            },
            "file_path": {
                "type": "string",
                "description": "用于 'upload_file' 或 'paste_image' 操作：本地图片/文件的绝对路径。upload_file 走 input[type=file]；paste_image 走系统剪贴板+真实 Ctrl+V（1688 以图搜图首选，绕开原生文件对话框）",
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
            "close_popup": [],
            "execute_js": ["script"],
            "upload_file": ["file_path"],
            "paste_image": ["file_path"],
            "close_tabs": ["text"],
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

            # 将视口尺寸对齐到真实屏幕可用区：browser_use 默认 1280x1100，
            # 若与实际屏幕（如 1920x953）不符，DOM 可见性判定会比截图多算出
            # 视口外的元素，导致视觉模型 grounding 失配、点空。仅在显式配置时覆盖。
            if config.browser_config is not None:
                win_w = getattr(config.browser_config, "window_width", None)
                win_h = getattr(config.browser_config, "window_height", None)
                if win_w and win_h:
                    size = {"width": int(win_w), "height": int(win_h)}
                    try:
                        context_config.browser_window_size = size
                    except Exception:
                        # 某些 browser_use 版本字段不可写时，退化为不覆盖（用默认）
                        logger.warning(
                            "无法设置 browser_window_size，沿用默认视口尺寸"
                        )
                    else:
                        logger.info(f"🖥️ 浏览器视口尺寸设为 {win_w}x{win_h}")

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
        script: Optional[str] = None,
        file_path: Optional[str] = None,
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

                # 🧹 批量关闭 URL 含指定子串的标签页：连续采集多商品时及时清理，
                # 避免图搜结果页/详情页越开越多（拖慢、且干扰当前页解析）。
                elif action == "close_tabs":
                    return await self._execute_close_tabs(context, text)

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

                # ❌ 关闭弹窗/遮罩：DOM 语义定位关闭按钮 + Escape 兜底。
                # 弹窗挡住主内容时的首选路径，绕开视觉坐标猜 × 图标的精度问题。
                elif action == "close_popup":
                    return await self._execute_close_popup(context)

                # 🧩 在当前页面执行 JS：DOM 没有交互抓手、但需要读取属性/文本/图片 src，
                # 或用页面自身的 fetch（带 cookie/referer）会话内下载图片时的首选路径。
                # 打在同一个已打开的 page 上，天然共享登录态。
                elif action == "execute_js":
                    if not script:
                        return ToolResult(
                            error="Script is required for 'execute_js' action"
                        )
                    page = await context.get_current_page()
                    result = await page.evaluate(script)
                    # dataURL（会话内下载图片的 base64）绝不能截断，否则解不出图。
                    # 约定：返回对象含 'dataURL' 键则原样返回；其余按 max_content_length 截断。
                    if isinstance(result, dict) and "dataURL" in result:
                        return ToolResult(
                            output=json.dumps(result, ensure_ascii=False, default=str)
                        )
                    serialized = json.dumps(result, ensure_ascii=False, default=str)
                    if len(serialized) > max_content_length:
                        serialized = (
                            serialized[:max_content_length]
                            + f"\n...[已截断，共 {len(serialized)} 字符]"
                        )
                    return ToolResult(output=f"JS result:\n{serialized}")

                # 📎 上传本地文件到页面的 <input type=file>：1688 以图搜图等的核心动作。
                # playwright 的 set_input_files 直接给 file input 塞文件，连系统原生选择框
                # 都不用触发，对隐藏的（display:none）input 同样有效——这是 DOM/视觉都碰不到
                # 系统弹窗时唯一可靠的上传路径。
                elif action == "upload_file":
                    return await self._execute_upload_file(context, file_path, index)

                # 📋 剪贴板粘贴图片搜图：1688 以图搜图首选。把图片写进系统剪贴板，
                # 发真实 Ctrl+V 粘进面板，点「搜索图片」，跟进新结果标签页。
                # 全程不碰 <input type=file>，绕开会冻住 CDP 的原生文件对话框。
                elif action == "paste_image":
                    return await self._execute_paste_image(context, file_path)

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

        [坐标换算] 视觉模型返回【相对截图图片的绝对像素坐标】，按 css = 图片像素 ÷ dpr
        映射到 page.mouse 的视口 CSS 像素空间（见 _apply_gui_atomic_action）。
        img_w/img_h 取自截图真实尺寸、dpr 取自 window.devicePixelRatio，均为运行时动态
        读取，故与具体分辨率（1920×953 等）无关：dpr=1 时 css 即等于像素值。

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
                    "疑似失败/卡死，已中止。若是在关弹窗，请改用 action=\"close_popup\""
                    "（DOM 定位关闭按钮，比猜坐标稳）；其余情况改用 DOM 定向操作"
                    "（如 select_date 选日期、click_element 按索引点击），不要继续用 "
                    "gui_action 重试同一目标。"
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

    async def _execute_close_popup(self, context: BrowserContext) -> ToolResult:
        """关闭遮挡主内容的弹窗/蒙层。站点无关，分三级兜底：

        1. DOM 语义定位关闭按钮（aria-label/class/title 含 close/关闭，或文本为 ×），
           取其视口中心坐标后用真实鼠标点击——比视觉模型猜 × 图标坐标稳得多；
        2. 按 Escape：很多弹窗监听 Escape 关闭；
        3. 仍在则再扫一遍 DOM（关掉可能叠了多层的弹窗）。

        这是「弹窗挡路」的首选路径。视觉模型反复点同一坐标关不掉弹窗会被防死锁
        中止（见 _execute_gui_action），改用本动作可绕开该精度问题。
        """
        page = await context.get_current_page()
        await page.bring_to_front()
        logs: list[str] = []

        async def _try_dom_close() -> Optional[dict]:
            try:
                return await page.evaluate(_POPUP_CLOSER_JS)
            except Exception as e:
                logs.append(f"DOM 定位异常：{e}")
                return None

        # 最多关 3 层弹窗（有的站点连续弹多个）
        closed_any = False
        for layer in range(1, 4):
            found = await _try_dom_close()
            if found and found.get("found"):
                try:
                    await page.mouse.click(found["x"], found["y"])
                    closed_any = True
                    logs.append(
                        f"[第{layer}层] DOM 定位({found.get('via')},z={found.get('z')})"
                        f"->鼠标点击({found['x']:.0f},{found['y']:.0f})"
                    )
                    await page.wait_for_timeout(_GUI_SETTLE_MS)
                    continue
                except Exception as e:
                    logs.append(f"[第{layer}层] 点击失败：{e}")
                    break
            else:
                if layer == 1:
                    logs.append(
                        "DOM 未定位到关闭按钮"
                        f"（疑似弹窗容器数={found.get('overlayCount') if found else 'NA'}）"
                    )
                break

        # Escape 兜底：无论 DOM 是否命中都补一发，关掉监听 Escape 的弹窗
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(_GUI_SETTLE_MS)
            logs.append("已发送 Escape")
        except Exception as e:
            logs.append(f"Escape 失败：{e}")

        if closed_any:
            return ToolResult(
                output="[close_popup] 已尝试关闭弹窗 | " + " | ".join(logs)
                + "。请重新查看页面状态确认弹窗是否消失，再继续后续操作。"
            )
        return ToolResult(
            output="[close_popup] 未在 DOM 中找到明确的关闭按钮，已发送 Escape 兜底 | "
            + " | ".join(logs)
            + "。若弹窗仍在，可改用 gui_action 视觉点击关闭按钮，或点击弹窗外的遮罩区域。"
        )

    async def _execute_upload_file(
        self,
        context: BrowserContext,
        file_path: Optional[str],
        index: Optional[int] = None,
    ) -> ToolResult:
        """把本地文件塞进页面的 <input type=file>，并把「以图搜图」这类流程走完。

        为什么不是「设完文件就返回」：1688 的以图搜图上传框是自定义的
        FileReader/React 包装（input#img-search-upload，class=image-file-reader-wrapper），
        playwright 的 set_input_files 虽然会派发 change，但站点的框架有时收不到，
        导致图片没真正上传、后续「搜索图片」按钮点了也不跳转（实测停在 1688.com
        首页，外层 LLM 只好退化成关键词搜索、结果全错）。

        因此本动作在设值后：
          1. 主动补派 input/change 事件，确保自定义上传器接住文件；
          2. 稍等上传/预览就绪；若页面已自行跳转到结果页则直接采用；
          3. 否则在浮层里精确定位并点击「搜索图片/搜同款」按钮触发搜索；
          4. 轮询等待「同标签跳转」或「新开结果标签页」，若新开了标签页就
             switch_to_tab 切过去，让后续 get_current_state 看到的是结果页。
        返回里带上是否跳转/是否新标签/结果 URL，给外层明确信号。
        """
        import os

        if not file_path:
            return ToolResult(error="file_path is required for 'upload_file' action")
        if not os.path.exists(file_path):
            return ToolResult(error=f"文件不存在：{file_path}")

        page = await context.get_current_page()

        # 1) 定位目标 file input
        file_handle = None
        if index is not None:
            element = await context.get_dom_element_by_index(index)
            if not element:
                return ToolResult(error=f"Element with index {index} not found")
            file_handle = await context.get_locate_element(element)
            if not file_handle:
                return ToolResult(error=f"Cannot locate element with index {index}")
        else:
            # 隐藏的 input 常不在交互元素列表，用选择器兜底（index 作序号）
            inputs = page.locator("input[type='file']")
            count = await inputs.count()
            if count == 0:
                return ToolResult(
                    error="页面未找到 input[type=file]。若上传框藏在点击后才出现的弹层，"
                    "请先 click_element 触发，再 upload_file。"
                )
            file_handle = inputs.nth(0)

        # 记录基线：现有标签页数量与当前 URL，用于稍后判断是否跳转/新开标签
        try:
            session = await context.get_session()
            pages_before = len(session.context.pages)
        except Exception:
            session = None
            pages_before = 1
        url_before = page.url

        # 2) 设值 + 补派 input/change（set_input_files 对隐藏 input 也有效）
        await file_handle.set_input_files(file_path)
        try:
            await file_handle.evaluate(
                "(el) => {"
                "el.dispatchEvent(new Event('input', {bubbles: true}));"
                "el.dispatchEvent(new Event('change', {bubbles: true}));}"
            )
        except Exception:
            pass  # 补派失败不影响主流程

        logs: list[str] = [
            "已设值 file input"
            + (f" index={index}" if index is not None else "（自动定位）")
        ]

        # 3) 等上传/预览就绪，看是否已自行跳转或新开标签
        async def _detect_change() -> Optional[str]:
            """返回 'nav' / 'tab' / None。"""
            if session is not None and len(session.context.pages) > pages_before:
                return "tab"
            try:
                if (await context.get_current_page()).url != url_before:
                    return "nav"
            except Exception:
                pass
            return None

        await page.wait_for_timeout(1500)  # 给上传 XHR / 预览渲染留时间
        change = await _detect_change()

        # 轮询等待跳转或新标签（最多约 8s，命中即止）
        if change is None:
            for _ in range(16):
                await page.wait_for_timeout(500)
                change = await _detect_change()
                if change:
                    break

        # 6) 新开了结果标签页则切过去，让后续状态看到结果页
        result_url = url_before
        if change == "tab" and session is not None:
            try:
                await context.switch_to_tab(len(session.context.pages) - 1)
                result_url = (await context.get_current_page()).url
                logs.append(f"已切到新结果标签页：{result_url}")
            except Exception as e:
                logs.append(f"切换新标签页失败：{e}（可自行 switch_tab）")
        elif change == "nav":
            try:
                result_url = (await context.get_current_page()).url
            except Exception:
                pass
            logs.append(f"页面已跳转到结果页：{result_url}")

        if change is None:
            logs.append(
                f"上传后未检测到跳转/新标签（仍在 {url_before}）。"
                "若这是以图搜图：可能需要你 wait 后 get 页面状态，或手动 click_element "
                "点「搜索图片」；不要直接退化成关键词搜索。"
            )

        head = f"Uploaded file '{file_path}'"
        return ToolResult(output=head + " | " + " | ".join(logs))

    @staticmethod
    def _set_clipboard_image(img_path: str) -> Optional[str]:
        """把图片写入 Windows 剪贴板（CF_DIB）。成功返回 None，失败返回错误字符串。

        CF_DIB = BMP 去掉开头 14 字节的 BITMAPFILEHEADER。这样后续真实 Ctrl+V
        时浏览器能从系统剪贴板读到位图，等价于用户手动复制图片再粘贴。
        """
        try:
            import io

            import win32clipboard
            from PIL import Image

            img = Image.open(img_path).convert("RGB")
            out = io.BytesIO()
            img.save(out, "BMP")
            data = out.getvalue()[14:]
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
            finally:
                win32clipboard.CloseClipboard()
            return None
        except Exception as e:
            return str(e)

    async def _execute_paste_image(
        self, context: BrowserContext, file_path: Optional[str]
    ) -> ToolResult:
        """1688 以图搜图（剪贴板 + 真实 Ctrl+V 路径，实测可用）。

        为什么不走 upload_file/<input type=file>：点相机/文件框会弹出系统原生
        「打开」对话框，它是 OS 级模态，会**阻塞整个 CDP 自动化**（页面变 about:blank、
        后续操作全卡死）。1688 面板明写支持「ctrl+v 粘贴图片」，故走最忠实的路径：

          1. 点「以图搜款」相机入口唤出上传浮层（best-effort，让 paste 监听器就位）；
          2. 把本地图片写进 Windows 系统剪贴板（CF_DIB 位图）；
          3. 发一个**真实的 Control+V** 按键——浏览器用系统剪贴板填充 paste 事件，
             图片粘进面板并生成预览（合成 ClipboardEvent 会被 Chrome 安全策略吞掉，
             故必须用真实按键 + 真实剪贴板）；
          4. 点浮层里的「搜索图片」按钮触发搜索（精确定位，避开相机入口与整块浮层）；
          5. 结果会**开在新标签页**（s.1688.com/youyuan/...imageSearch...），自动
             switch_to_tab 切过去，返回结果页 URL。
        """
        import os

        if not file_path:
            return ToolResult(error="file_path is required for 'paste_image' action")
        if not os.path.exists(file_path):
            return ToolResult(error=f"文件不存在：{file_path}")

        page = await context.get_current_page()
        await page.bring_to_front()
        logs: list[str] = []

        # 记录基线：标签数与当前 URL，用于判断是否新开结果标签/跳转
        try:
            session = await context.get_session()
            pages_before = len(session.context.pages)
        except Exception:
            session = None
            pages_before = 1
        url_before = page.url

        # 1) 唤出以图搜图面板（best-effort：面板可能已开，找不到入口也继续）
        try:
            armed = await page.evaluate(_IMAGE_SEARCH_ENTRY_JS)
            if armed.get("armed"):
                logs.append(f"已点开搜图入口「{armed.get('text')}」")
                await page.wait_for_timeout(800)
            else:
                logs.append("未找到搜图入口（面板可能已打开，继续）")
        except Exception as e:
            logs.append(f"点开搜图入口异常：{e}（继续）")

        # 2) 写系统剪贴板
        err = self._set_clipboard_image(file_path)
        if err is not None:
            return ToolResult(
                error=f"写入系统剪贴板失败：{err}。paste_image 依赖 pillow + pywin32，"
                "且仅支持 Windows。可退回 upload_file（注意其会弹原生文件框）。"
            )
        logs.append("图片已写入系统剪贴板")

        # 3) 真实 Ctrl+V
        await page.keyboard.press("Control+v")
        await page.wait_for_timeout(1800)  # 等预览渲染 + 搜索按钮出现
        logs.append("已发送真实 Ctrl+V")

        # 4) 点「搜索图片」按钮
        try:
            clicked = await page.evaluate(_IMAGE_SEARCH_BUTTON_JS)
        except Exception as e:
            clicked = {"clicked": False, "error": str(e)}
        if clicked.get("clicked"):
            logs.append(f"已点击搜索按钮「{clicked.get('text')}」")
        else:
            logs.append(
                "未找到「搜索图片」按钮——图片可能未粘贴成功（面板未打开/未聚焦），"
                "请确认面板已弹出后重试 paste_image。"
            )

        # 5) 轮询等待新结果标签/跳转（最多约 10s），命中即切过去
        async def _new_tab_index() -> Optional[int]:
            if session is None:
                return None
            pages = session.context.pages
            if len(pages) <= pages_before:
                return None
            # 取最新一个 s.1688.com 结果页；否则取最后一个新标签
            for i in range(len(pages) - 1, pages_before - 1, -1):
                if "s.1688.com" in pages[i].url:
                    return i
            return len(pages) - 1

        result_url = url_before
        switched = False
        for _ in range(20):
            await page.wait_for_timeout(500)
            idx = await _new_tab_index()
            if idx is not None:
                try:
                    await context.switch_to_tab(idx)
                    result_url = (await context.get_current_page()).url
                    switched = True
                    logs.append(f"已切到新结果标签页：{result_url}")
                except Exception as e:
                    logs.append(f"切换新标签失败：{e}（可自行 switch_tab）")
                break
            # 也可能同标签跳转
            try:
                cur = (await context.get_current_page()).url
            except Exception:
                cur = url_before
            if cur != url_before and "1688.com" in cur and cur != "https://www.1688.com/":
                result_url = cur
                logs.append(f"页面已跳转到结果页：{result_url}")
                break

        head = f"paste_image '{os.path.basename(file_path)}'"
        if switched or result_url != url_before:
            head += f" → 图搜结果页：{result_url}"
            return ToolResult(output=head + " | " + " | ".join(logs))
        return ToolResult(
            output=head
            + "（未检测到结果页跳转）| "
            + " | ".join(logs)
            + "。请 wait 后 get 页面状态确认；若图片没粘进面板，多为面板未打开/未聚焦。"
        )

    async def _execute_close_tabs(
        self, context: BrowserContext, match: Optional[str]
    ) -> ToolResult:
        """关闭 URL 含 `match` 子串的所有标签页，用于连续采集时及时清理。

        典型用法：采完一个商品后 close_tabs(text="1688")，把这次开的图搜结果页/
        详情页/1688 首页一并关掉，只留 Temu 工作标签，防止标签越积越多。

        安全约束：绝不关到零标签（浏览器会退出）——若匹配到全部标签，保留最后一个。
        关闭后切到一个存活标签并置前，保证「当前页」有明确定义。
        """
        if not match:
            return ToolResult(
                error="close_tabs 需要 text 作为要关闭标签的 URL 子串（如 '1688'）"
            )
        try:
            session = await context.get_session()
        except Exception as e:
            return ToolResult(error=f"close_tabs 获取会话失败：{e}")

        pages = list(session.context.pages)
        to_close = [p for p in pages if match in (p.url or "")]
        keep = [p for p in pages if match not in (p.url or "")]

        # 不能关到零标签：若全部命中，保留最后一个
        if not keep and to_close:
            keep = [to_close.pop()]

        closed = 0
        for p in to_close:
            try:
                await p.close()
                closed += 1
            except Exception:
                pass  # 单个关闭失败不影响其余

        # 关完切到一个存活标签并置前，避免「当前页」悬空
        try:
            session = await context.get_session()
            remaining = session.context.pages
            if remaining:
                await context.switch_to_tab(len(remaining) - 1)
        except Exception:
            pass

        return ToolResult(
            output=f"已关闭 {closed} 个 URL 含 '{match}' 的标签页，剩余 {len(pages) - closed} 个。"
        )

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
    def _gui_scroll_pixels(amount: str, viewport_h: float) -> int:
        """把 small/medium/large 按【当前视口高度】比例换算成滚动像素（非写死像素）。

        viewport_h 为运行时实测的视口 CSS 高度，故任意分辨率/缩放下滚动比例一致。
        """
        frac = _SCROLL_VIEWPORT_FRACTIONS.get(amount or "medium", 0.6)
        return max(1, int((viewport_h or 0) * frac))

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
            # 统一放到桌面输出目录的「调试截图」子目录，不再散落到项目根的 ./screenshots
            debug_dir = str(config.output_dir("screenshot"))
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
                # 幅度按运行时实测的视口 CSS 高度比例算，不写死像素（见 _gui_scroll_pixels）。
                pixels = self._gui_scroll_pixels(params.get("amount", "medium"), css_h)
                delta = pixels if direction == "down" else -pixels
                await page.mouse.wheel(0, delta)
                return {
                    "message": (
                        f"Scrolled {direction} by {pixels}px "
                        f"({_SCROLL_VIEWPORT_FRACTIONS.get(params.get('amount', 'medium'), 0.6):.0%} "
                        f"of {css_h}px viewport) | {thought}"
                    ),
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
        """清理浏览器资源。

        [关键] 接管用户真实 Chrome（配置了 cdp_url）时，**绝不关闭 context/browser**：
        那会把用户自己的标签页（如 Temu 工作页）一并关掉、并断开其已登录会话。
        这种模式下我们只是「接管」别人的浏览器，清理时应只脱离（丢弃引用），
        让用户的 Chrome 与标签原样留存。只有当浏览器是我们自己启动的（无 cdp_url）
        才真正 close。
        """
        async with self.lock:
            attached = bool(
                config.browser_config
                and getattr(config.browser_config, "cdp_url", None)
            )
            if attached:
                # detach：接管别人的浏览器，清理时什么都不做——既不关 context/browser
                # （会连用户标签一起关），也不丢引用（丢引用会触发 browser_use 的 __del__
                # 强制关闭钩子、在运行中的事件循环里 asyncio.run 报错刷告警）。
                # 进程退出时 CDP 连接自然断开，不影响用户的真实 Chrome。
                return
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
