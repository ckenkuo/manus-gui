# 系统桌面级 computer_use 工具 + 隔离运行方案探索

本文记录 `computer_use`（系统桌面级 computer use）工具的设计与实现，以及"让 agent
在隔离环境操作系统"这一后续方向的探讨结论。

> 状态（2026-07）：本地版工具已实现并测试（tests/test_computer_use.py，43 项通过）。
> **真正的隔离运行（VM / 独立会话）暂缓**，先保留本探索文档；当前先去调试 browser-use。

---

## 一、它是什么，和 browser_use 的关系

核心洞察：项目里视觉「大脑」`app/tool/gui_agent.py` 的 `query_gui_action`（给截图 +
子目标 → 返回一个像素坐标的原子操作）**本来就是按桌面写的**，与截图来源无关。
`browser_use` 只是给这个大脑接了一套 **Playwright 的手**（坐标落到 `page.mouse`）。

`computer_use` 就是给同一个大脑接一套 **操作系统级的手**（坐标落到 PyAutoGUI，整块屏幕）：

| | browser_use（gui_action） | computer_use |
|---|---|---|
| 大脑 | `query_gui_action`（共用） | `query_gui_action`（共用） |
| 眼 | 浏览器视口截图 | 整屏截图 |
| 手 | Playwright `page.mouse` | PyAutoGUI |
| 适用 | 浏览器页面内 | 浏览器够不到的系统桌面 |

**分工**：网页内容一律优先 browser_use；系统原生文件对话框、WPS/Office 桌面端、
资源管理器、安装程序、任意原生窗口 → computer_use。（痛点来源：网页里点相机图标
弹出的是系统原生文件框，DOM/gui_action 都点不了。）

相关文件：
- `app/tool/computer_use_tool.py` —— 工具本体
- `app/tool/gui_agent.py` —— 视觉大脑（新增可选 `system_prompt` 参数，桌面版在此扩展）
- `app/tool/__init__.py`、`app/agent/manus.py` —— 导出与注册
- `app/prompt/manus.py` —— `COMPUTER_USE_RULES`（告诉 agent 何时用它）

---

## 二、动作集与工作方式

一个工具、`action` 分发（同 `WpsExcelTool` 风格）：

| action | 类型 | 说明 |
|---|---|---|
| `task` | 高层自驱 | 给自然语言子目标，内部跑「截图→视觉决策→执行→再截图确认」循环 |
| `launch_app` | 确定性 | 启动程序 / 打开文件（`os.startfile` / `subprocess`） |
| `run` | 确定性 | 执行 shell 命令，返回 stdout |
| `hotkey` | 确定性 | 组合键，如 `win+d`、`alt+f4`、`ctrl+s` |
| `focus_window` | 确定性 | 按标题子串把窗口置前（`pygetwindow`） |
| `screenshot` | 观察 | 只截整屏返回（`ToolResult.base64_image`） |
| `wait` | 确定性 | 睡 N 秒，等 UI / 安装器稳定 |

视觉词汇表在浏览器版 `CLICK/TYPE/SCROLL/KEY_PRESS/FINISH/FAILE` 之外，桌面版**追加**
`DOUBLE_CLICK / RIGHT_CLICK / DRAG`（经 `DESKTOP_GUI_SYSTEM_PROMPT` 扩展，浏览器路径零回归）。

依赖：`pyautogui`（连带 `pygetwindow`）、`pyperclip`（中文输入走剪贴板粘贴）。
视觉模型复用现成的 `[llm.gui]` 配置段，零新增配置。

---

## 三、坐标契约：按当前环境处理，不写死像素

### 3.1 DPI 缩放（坐标映射）

视觉模型返回**相对截图图片的绝对像素坐标**。Win10 高 DPI 下，截图是物理像素、
PyAutoGUI 用逻辑坐标，若不换算会整体偏移。沿用浏览器工具那套契约：

```
dpr = 截图物理像素宽 / 屏幕逻辑宽(pyautogui.size)
逻辑坐标 = 像素 / dpr
```

`dpr` 运行时实测，与具体缩放比（100%/125%/150%）无关；不改进程 DPI 感知状态。
实测本机 1920×1080 @ dpr=1.0，中心像素换算后精确落在屏幕中心。

### 3.2 滚动幅度（曾写死，已改为环境相对）

**问题**：滚动量原本写死 `{"small":300,"medium":600,"large":1000}` 像素，
高分屏上一次 medium 只滚一点、小屏上滚过头——没随环境走。

**修复**：改为按**运行时实测的视口/屏幕高度比例**算，比例集中成常量、提成纯静态函数：
- 浏览器：`_gui_scroll_pixels(amount, css_h)` = 视口高 × `{0.3/0.6/0.9}`
- 桌面：`_scroll_clicks(amount, screen_h, dir)` = 屏高 × 比例，再按 `_WHEEL_CLICK_PX≈120`
  换成 PyAutoGUI 的滚轮档（其单位是"档"非像素，故为随分辨率自适应的近似换算）

> 若某 app 里滚太多/太少，调 `_WHEEL_CLICK_PX` 一个常量即可，不用动逻辑。

---

## 四、安全护栏

按选定策略：

1. **FAILSAFE 急停**：鼠标猛甩到屏幕左上角随时中止（`pyautogui.FAILSAFE=True`）。
2. **步数上限**：单个 `task` 最多 `_MAX_ITERATIONS` 个原子操作，防失控空转。
3. **关键操作前人工确认**：复用 `ask_human`。每个 task 首次真实操作前确认一次；
   破坏性 `KEY_PRESS`（`alt+f4`/`delete` 等 `_DESTRUCTIVE_KEYS`）每次单独确认。
4. **优雅降级**：未装 pyautogui / 无显示环境时返回干净错误，不崩栈。

---

## 五、人机冲突与协作式护栏

### 5.1 根因

`computer_use` 与人**共用同一套鼠标、键盘焦点、屏幕**（browser_use 没这问题——它在
自己的 Playwright 上下文里，与桌面隔离）。三种典型冲突：

1. **观察↔执行的时间窗**：循环先截图 → 调视觉模型（数秒）→ 才下手。这几秒里人动了
   鼠标 / 切了窗口，agent 就拿着过期截图去点，坐标/焦点已错位。**最常见。**
2. **键盘焦点被抢**：人点了别的窗口，agent 的 TYPE 打进人的窗口。
3. **目标窗口被切走/最小化**：截图与真实状态不一致。

### 5.2 协作式护栏（已实现）

在 `task` 循环里加**干扰检测**：截图当下记一次环境指纹 `(前台窗口, 鼠标位置)`，
真正下手前一刻再记一次比对（放在确认门之后，故也覆盖确认耗时）：

- 前台窗口变了、或鼠标被移动超过 `_MOUSE_MOVE_TOLERANCE_PX` → 判为人为干扰 →
  **丢弃这次基于旧截图的动作、重新截图**，而不是硬点。
- 单次/偶发干扰静默重观察（`[干扰N]` 记进结果，不消耗正式步数预算）；
  **连续 `_MAX_INTERFERENCE_STREAK`(=3) 次**仍被打断 → `ask_human` 暂停等人忙完。

对应实现：`_env_fingerprint` / `_detect_interference` / `_run_task` 循环。

### 5.3 诚实的边界

这是**缓解**，不是隔离——你和 agent 抢鼠标时它只能等你，无法真正并行；键盘焦点
被抢的极端时序仍有极小残留窗口。要**彻底**消除冲突 + 真并行，只能靠隔离（见下）。

---

## 六、隔离运行方案（后续方向，暂缓）

让 agent 在隔离环境操作系统，是更全面、更安全的必然方向：真并行、爆炸半径可控、
可快照回滚、凭据隔离。探讨出三种架构：

### 方案 A：整个项目跑在 Windows VM 里（agent-in-VM）—— 首选

agent 主体连同所有工具都在 VM 内运行，`computer_use` 操作的是 **VM 自己的本地桌面**。

- ✅ **几乎零代码改动**：pyautogui 照跑，隔离在 VM 边界实现。
- ✅ 所有工具（browser/wps/python/computer_use）**共享同一文件系统和桌面，无分裂**。
- ⚠️ 控制面进 VM：给任务、答 `ask_human` 都在 VM 内（进 VM 交互，或以后加薄远程 UI）。

### 方案 B：只在 computer_use 时切到 VM（host-drives-VM / 按工具切）

agent 主体留宿主，只有 computer_use 把动作路由到常开 VM（HTTP 调 VM 内自动化服务）。
（注意："切环境"只能指向独立 VM，**不能切宿主自己的会话**——那会锁你的桌面、毁掉并行。）

- ✅ **控制面留宿主**：本地给任务、答确认，无需进 VM。
- ❌ **环境分裂**：computer_use 在 VM，但 browser 采的图 / wps 写的表 / python 存的文件
  都在宿主。WPS 流水线（采集→存图→写表→WPS 核对）会因两边文件系统不同而断裂，
  需**文件桥（共享文件夹）**且路径一致——这是本方案的真实代价。
- 需要 `DesktopBackend` 抽象 + VM 内自动化服务（git `77c16a2` 被删的 sandbox 版即参考实现）。

### 方案 C：Daytona / Linux 沙箱

仓库已埋地基（`daytona-sdk`、`app/daytona/`、noVNC 6080、supervisord）。
但 **Daytona 是 Linux**，装不了 Windows 版 WPS → **不适用于 DISPIMG 主力负载**；
仅适合 web / Linux 应用自动化。

### 决策依据（一条）

**computer_use 任务是否需要碰其他工具在宿主产出的同一批文件？**
- 需要（WPS 场景大概率是）→ 选 **A**（agent-in-VM），零同步、最干净。
- 不需要（桌面任务自成一体）→ 选 **B**，白赚"控制面留宿主"。

### 迁移成本低的原因

现有 `ComputerUseTool` 已把「大脑」(`query_gui_action`，来源无关) 与「手」(pyautogui)
分离。任一隔离方案都只是**换后端**，不必重写：

```
computer_use → DesktopBackend（接口：screenshot/click/double/right/drag/type/scroll/hotkey/launch/run）
               ├─ LocalBackend  (现在的 pyautogui，本机/VM 本地桌面)
               └─ VmBackend      (HTTP → VM 内自动化服务，方案 B 用)
```

`_run_task` 循环、干扰护栏、`css=px/dpr` 坐标契约、工具 `action` schema **全不变**。
- 方案 A 只用 `LocalBackend`，**连抽象都不必做**。
- 方案 B 才需要抽象 + `VmBackend` + 文件桥。

### 已知运维坑（选任一 VM 方案时）

1. **RDP 断开会杀掉 GUI 自动化**：用 RDP 进 VM 后断开，Windows 锁会话、拆交互桌面，
   pyautogui 截图变黑/点击失效。解法：用**控制台会话**（VMConnect / VNC，镜像 console），
   或**自动登录 + `tscon` 把 RDP 会话重定向回 console + 关锁屏/屏保**保活。
2. **文件进出**：宿主↔VM 共享文件夹 / 网络盘。
3. **一次性装机**：VM 内装 Windows + WPS（登录/授权）+ Python 依赖 + Playwright + Chrome，
   配 `config.toml`，打快照存干净态。
4. VM 显示缩放建议设 100%（dpr=1 最省心）。

---

## 七、待定与下一步

- 暂不上真正的隔离；当前保持"本地 + 协作护栏"，够覆盖眼下的桌面即时任务。
- 待用户确认「computer_use 是否需要接着改宿主上的 WPS 文件」后再定 A/B：
  - 选 A → 整备项目进 VM（bootstrap 脚本 + console 保活指南 + 无人值守开关）。
  - 选 B → 做 `DesktopBackend` 抽象 + `VmBackend`（含文件桥约定）。
- 相关记忆：项目记忆 `computer-use-isolation-direction`。
