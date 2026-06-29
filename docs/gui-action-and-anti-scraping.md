# GUI 视觉操作 + 反爬站点接管指南

本项目的 `browser_use` 工具除了基于 DOM 索引的操作外，还提供 `gui_action`：
截图当前页面 → 交给视觉模型按**像素坐标**决策单一原子操作 → 驱动 Playwright 执行。
适合目标控件未出现在 DOM 元素列表、或自定义控件（日历 / 地图 / 画布 / 富文本 /
图片热区）难以被 DOM 树捕获的场景。

针对携程这类**强反爬**站点，裸 Playwright 会被登录墙 + 假“无航班”拦截。
决定性解法是：**接管你已登录的真实 Chrome**（真实指纹 + 已有 cookie）。

---

## 一、为什么裸浏览器不行

携程等站点会检测自动化特征：
- `navigator.webdriver` 为 true（Playwright/Selenium 默认会暴露）；
- 缺少真实用户的 cookie / 登录态；
- 指纹异常 → 触发登录墙，或返回假的“无航班 / 无结果”。

结果就是 agent 拿不到真实数据。

## 二、解法：用 CDP 接管已登录的真实 Chrome

### 1. 用独立用户目录 + 远程调试端口启动 Chrome

**Windows (PowerShell)：**

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="C:\chrome-debug-ctrip"
```

要点：
- `--remote-debugging-port=9222`：开放 CDP 端口，供 agent 接管。
- `--user-data-dir="C:\chrome-debug-ctrip"`：**独立用户目录**。用单独目录的好处是
  cookie / 登录态隔离留存，且不会与你日常的 Chrome 实例冲突（同一 user-data-dir
  不能被两个 Chrome 进程同时占用）。

### 2. 在弹出的窗口里手动登录一次目标站点

在这个 Chrome 窗口里打开携程并**手动登录**。登录态（cookie）会保存在
`C:\chrome-debug-ctrip`，下次用同一目录启动即免登录。

### 3. 让 agent 通过 cdp_url 接管该实例

在 `config/config.toml` 配置：

```toml
[browser]
cdp_url = "http://localhost:9222"
```

agent 启动时会**连接到这个已登录实例**，而不是新开一个裸浏览器。于是：
- `navigator.webdriver` 干净（真实 Chrome）；
- 携带你手动登录留下的 cookie；
- 不再触发登录墙和假“无航班”提示。

> ⚠️ 该 Chrome 实例需在 agent 运行期间**保持开启**。关掉它 agent 就失去接管目标。

---

## 三、gui_action 坐标契约（改这块务必遵守）

视觉模型（DashScope，配在 `[llm.gui]`）返回的是**相对截图图片的绝对像素坐标**，
提示词在 [app/tool/gui_agent.py](../app/tool/gui_agent.py) 的 `GUI_SYSTEM_PROMPT`。

- **像素→CSS 换算用 `css = px / dpr`**
  （见 [app/tool/browser_use_tool.py](../app/tool/browser_use_tool.py) 的
  `_apply_gui_atomic_action`）。
  **不要用 `window.innerWidth/innerHeight`** —— 会被滚动条干扰，实测 innerHeight
  报 1100 但实际可点区域只有 ~1040，按它换算会点偏。

- **截图前必须 `await context.remove_highlights()`**
  否则 browser_use 注入的红色索引框会糊满截图，视觉模型从“看”退化成“猜”，
  grounding 直接失效。这是该能力可用的前提。

- **防死锁**：重复动作判定加了“且 URL 未跳转”条件 + 操作后 `_wait_for_gui_settle`
  （等 networkidle）。否则点击触发跳转有延迟，会被误判为卡死。

- **键名规范化**：`_normalize_key_press` 把 `esc → Escape` 等映射到 Playwright
  规范名。

- **日期选择**：优先用 `select_date`（DOM 定位日期格），比视觉点击日历更稳。

---

## 四、其他

- `max_steps` 默认从 20 提到 40（复杂任务：多页表单 / 反爬重试 / 跨页操作 20 步常不够）；
  可用 `main.py --max-steps` 覆盖。
- 该方案 2026-06-29 实跑验证通过：携程查上海→北京机票，成功拿到真实票价。
