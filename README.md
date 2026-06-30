# 🖱️ manus-gui

> **manus-gui** 是基于 [OpenManus](https://github.com/FoundationAgents/OpenManus)（作者 [@FoundationAgents](https://github.com/FoundationAgents)，manna_and_poem 等）的个人衍生项目，沿用 MIT 许可证。原始框架的全部功劳归上游作者所有 —— 见 [致谢与归属](#致谢与归属)。
>
> **manus-gui** is a personal derivative work based on [OpenManus](https://github.com/FoundationAgents/OpenManus), released under the MIT License. All credit for the original framework goes to the upstream authors — see [Attribution](#attribution).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

[中文](#中文) | [English](#english)

manus-gui 保留了 OpenManus 完整的 agent 能力，并新增两项面向**真实浏览器自动化**的特性：在杂乱页面和反爬防护下依然能操作，并且用得越多越快。其余部分（工具集、MCP、run-flow 多智能体、沙箱、搜索）原样继承自 OpenManus。

---

# 中文

manus-gui 在 OpenManus 之上新增两项核心能力：

1. **视觉定位 GUI 操作 + 反爬浏览器接管** —— 当 DOM 索引自动化找不到元素时，agent 改用视觉模型给出的像素坐标点击，并可通过 CDP 接管你已登录的真实 Chrome，绕过登录墙与机器人检测。
2. **RAG 经验库** —— 把跑通的任务流程提炼成可复用的「经验」，新任务按相似度检索并作为 few-shot 注入，让 agent 少走弯路。

## ✨ 相比 OpenManus 的改动

### 1. 视觉定位 `gui_action` + 反爬浏览器接管

`browser_use` 工具新增了一个 `gui_action`，与 DOM 索引操作（`click_element` / `input_text` / `select_date`）**并列、平等**，agent 按页面情况自主选择：

- **DOM 索引路径** —— `browser_use` 能识别交互元素时，又快又稳。
- **视觉路径（`gui_action`）** —— 用于 DOM 识别失败的页面：自定义日期/地图/canvas 控件、标记混淆的反爬页面。工具对页面截图，请视觉模型给出一个**像素坐标的原子操作**，再把像素换算成视口 CSS 像素（`css = px / dpr`）。
- **`select_date` DOM 辅助** —— 日期选择器首选它（按 DOM 定位日期格比视觉点击小日历格更稳）。

让视觉路径可靠的关键工程细节：

- **截图前先去高亮**（`await context.remove_highlights()`）—— 否则 `browser_use` 注入的红色索引框糊满页面，视觉模型会从「看」退化成「猜」。
- **基于 dpr 的坐标换算**，而非 `window.innerWidth/innerHeight`（会被滚动条干扰）。
- **防死锁** —— 重复动作判定额外要求「URL 未跳转」，并在每次操作后等待 `networkidle`，避免点击触发的跳转被误判为卡死。
- **键名归一化**（`esc → Escape` 等）映射到 Playwright 规范名。

**反爬接管**：携程一类网站会用登录墙和假「无航班」拦截裸 Playwright。解法是通过 CDP **接管你已经登录好的真实 Chrome**：

```toml
[browser]
cdp_url = "http://localhost:9222"
```

自己先启动并手动登录 Chrome，再让 agent 接管：

```powershell
chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\chrome-debug"
```

真实浏览器指纹（干净的 `navigator.webdriver`）加上你真实的 cookie，是拿到实时数据的决定性因素。

> 视觉模型配置在 `[llm.gui]`（默认 DashScope `qwen3.7-plus`）。若省略则回退到 `[llm.vision]`，再回退到环境变量 `DASHSCOPE_API_KEY`。模型返回相对截图的绝对像素坐标，由浏览器工具完成换算。

此外：默认 `max_steps` 从 20 提到 40，并新增 `--max-steps` 命令行参数应对复杂任务。

### 2. RAG 经验库（从历史成功中提取 few-shot）

任务以 `terminate(status=success)` 结束后，manus-gui 可把本次运行**提炼成一条高层经验**并存库。新任务开跑时，检索最相似的历史经验作为 few-shot 注入，复用有效做法而非重新摸索。

- **提炼** —— 由 LLM 把运行总结为 `{ steps, result_summary, tips }`。步骤采用泛化+本例括注（如「填入对应城市（本例：上海、北京）」）；踩坑对策走独立的 `tips` 字段；`tools_used` / `step_count` 程序化补全。**不存**原始 DOM index / 坐标（易过期）。
- **混合检索** —— 稠密向量（Faiss `IndexFlatIP`，对 L2 归一化的 `text-embedding-v4` 求余弦）**+** BM25 关键词检索（`rank-bm25` + `jieba` 中文分词），用 **RRF 倒数排名融合**。`min_score` 相关性闸门防止给无关任务硬塞经验。
- **注入** —— 开跑前把检索到的经验一次性追加到 system_prompt，跑完恢复，避免同一实例多次运行时经验块累积。
- **写入仅交互入口** —— 任务成功后终端打印候选预览并询问 `[y/N]`。非交互运行（`--prompt` 脚本、非 TTY）按设计跳过保存。
- **存储人类可读** —— 经验存在 `experience/recipes.jsonl`，每行一个 JSON 对象。增删经验直接手编该文件（可 `git diff`），下次启动自动重建索引。
- **可干净下线** —— 由 `[experience].enabled` 控制。关闭后既不注入也不弹保存询问，对主流程零侵入。检索全程 best-effort：缺 `faiss` 回退 numpy 点积，缺 `jieba`/`rank-bm25` 禁用 BM25 通路，任何异常都吞掉而非中断运行。

新增模块 `app/experience/`（`embedding`、`store`、`retriever`、`recorder`、`__init__`）；集成点为 `Manus.run()`（注入）与 `main.py`（`_maybe_save_experience`，确认 + 落库）。

分层验证步骤（自检 → 离线冒烟 → 真实端到端 → 关闭回归）见 [docs/rag-experience-verification.md](docs/rag-experience-verification.md)，2026-06-30 验证通过。

## 🛠️ 安装

### 方式一：conda

```bash
conda create -n manus_gui python=3.12
conda activate manus_gui
git clone <your-fork-url> manus-gui
cd manus-gui
pip install -r requirements.txt
```

### 方式二：uv（推荐）

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <your-fork-url> manus-gui
cd manus-gui
uv venv --python 3.12
source .venv/bin/activate    # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt
```

### 浏览器自动化

```bash
playwright install
```

RAG 经验库需要三个额外依赖（已在 `requirements.txt` 锁定版本）：

```bash
pip install faiss-cpu rank-bm25 jieba
```

## ⚙️ 配置

复制示例并填入你的密钥：

```bash
cp config/config.example.toml config/config.toml
```

```toml
# 核心 LLM
[llm]
model = "claude-3-7-sonnet-20250219"
base_url = "https://api.anthropic.com/v1/"
api_key = "YOUR_API_KEY"
max_tokens = 8192
temperature = 0.0

# 视觉模型（DOM 识别失败时由 gui_action 使用）
[llm.gui]
api_type = "openai"
model = "qwen3.7-plus"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key = "YOUR_DASHSCOPE_API_KEY"
max_tokens = 2048
temperature = 0.0

# 接管已登录的真实 Chrome 以绕过反爬
[browser]
cdp_url = "http://localhost:9222"

# RAG 经验库
[experience]
enabled = true
embedding_model = "text-embedding-v4"   # 建库与查询必须用同一模型
top_k = 2                               # 注入多少条相似经验
min_score = 0.35                        # 相关性下限闸门
rrf_k = 60                              # 倒数排名融合常数
```

说明：
- `[llm.gui]`、`[browser].cdp_url`、`[experience]` 均为可选。省略 `[experience]`（或设 `enabled = false`）即与上游 OpenManus 完全一致。
- embedding 复用 `[llm.default]` 的 DashScope 兼容端点，因此该段的 `api_key` / `base_url` 必须有效，检索才能工作。

## 🚀 快速开始

```bash
python main.py
# 然后在提示符输入你的任务
```

```bash
# 非交互运行，并为复杂任务调高步数预算
python main.py --prompt "在携程查上海→北京的机票并报告价格" --max-steps 40
```

继承自 OpenManus 的其他入口：

```bash
python run_mcp.py     # MCP 工具版
python run_flow.py    # 多智能体 run-flow（不稳定）
```

## 🧪 测试

```bash
# 经验库单元测试（离线，embedding/LLM 已 mock）
pytest tests/test_experience_store.py tests/test_experience_retriever.py tests/test_experience_recorder.py -v
```

## 📁 改动位置

| 区域 | 文件 |
|------|------|
| 视觉 `gui_action` + 键名/坐标处理 | [app/tool/browser_use_tool.py](app/tool/browser_use_tool.py), [app/tool/gui_agent.py](app/tool/gui_agent.py) |
| RAG 经验库 | [app/experience/](app/experience/)（`embedding`、`store`、`retriever`、`recorder`、`__init__`） |
| 运行时注入 / 捕获 terminate 状态 | [app/agent/manus.py](app/agent/manus.py) |
| 交互式保存 + `--max-steps` | [main.py](main.py) |
| 配置 schema | [app/config.py](app/config.py), [config/config.example.toml](config/config.example.toml) |
| 验证指南 | [docs/rag-experience-verification.md](docs/rag-experience-verification.md) |

## 致谢与归属

manus-gui 是 **OpenManus** 的个人衍生作品，并以原始 **MIT 许可证**再分发其代码。上游框架、设计及全部原始代码均出自 [@FoundationAgents](https://github.com/FoundationAgents) / [MetaGPT](https://github.com/geekan/MetaGPT) 的 OpenManus 作者。本 fork 仅新增上文所述的 GUI 控制与 RAG 经验库特性。同时感谢 [anthropic-computer-use](https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo)、[browser-use](https://github.com/browser-use/browser-use)、[crawl4ai](https://github.com/unclecode/crawl4ai) 提供的浏览器/自动化基础支持。

- 上游：https://github.com/FoundationAgents/OpenManus

---

# English

manus-gui keeps the full OpenManus agent stack and adds two capabilities aimed at **real-world browser automation that survives messy pages and anti-bot defenses, and gets faster the more you use it**:

1. **Vision-grounded GUI control + anti-scraping browser takeover** — when DOM-index automation can't find an element, the agent falls back to clicking by pixel coordinates from a vision model, and can drive your real logged-in Chrome over CDP to get past login walls and bot detection.
2. **RAG experience library** — successful task flows are distilled into reusable "recipes", retrieved by similarity on a new task, and injected as few-shot guidance so the agent takes fewer detours.

Everything else (tool set, MCP support, run-flow multi-agent mode, sandbox, search) is inherited from OpenManus unchanged.

## ✨ What's different from OpenManus

### 1. Vision-grounded `gui_action` + anti-scraping browser takeover

The `browser_use` tool gains a `gui_action` that sits **side-by-side with the DOM-index actions** (`click_element` / `input_text` / `select_date`). The agent chooses per page:

- **DOM-index path** — fast and stable when `browser_use` can see interactive elements.
- **Visual path (`gui_action`)** — for pages where DOM recognition fails: custom date/map/canvas widgets, anti-bot pages with obfuscated markup. The tool screenshots the page and asks a vision model for a **pixel-coordinate atomic action**, then scales pixels to viewport CSS pixels (`css = px / dpr`).
- **`select_date` DOM helper** — preferred for date pickers (locating the date cell by DOM is steadier than clicking a small calendar cell visually).

Key engineering details that make the visual path reliable:

- **Highlights removed before screenshot** (`await context.remove_highlights()`) — otherwise `browser_use`'s red index boxes cover the page and the vision model has to guess instead of see.
- **dpr-based coordinate scaling**, not `window.innerWidth/innerHeight` (which is thrown off by scrollbars).
- **Deadlock guards** — repeated-action detection also requires "URL did not change", plus a `networkidle` settle wait after each action, so click-triggered navigation isn't misread as a stall.
- **Normalized key names** (`esc → Escape`, etc.) mapped to Playwright's canonical names.

**Anti-scraping takeover:** sites like Ctrip block headless Playwright with login walls and fake "no results". The fix is to **take over a real Chrome you already logged into**, via CDP:

```toml
[browser]
cdp_url = "http://localhost:9222"
```

Start Chrome yourself, log in manually, and let the agent attach:

```powershell
chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\chrome-debug"
```

A real browser fingerprint (clean `navigator.webdriver`) plus your real cookies is the decisive factor for getting live data.

> Vision model config lives in `[llm.gui]` (DashScope `qwen3.7-plus` by default). If omitted it falls back to `[llm.vision]`, then to env `DASHSCOPE_API_KEY`. The model returns absolute pixel coordinates relative to the screenshot; the browser tool scales them.

Also bumped: default `max_steps` 20 → 40, with a `--max-steps` CLI override for complex tasks.

### 2. RAG experience library (few-shot from past successes)

After a task ends in `terminate(status=success)`, manus-gui can **distill the run into a high-level recipe** and store it. On a new task, it retrieves the most similar past recipes and injects them as few-shot context, so the agent reuses what worked instead of rediscovering it.

- **Distillation** — an LLM summarizes the run into `{ steps, result_summary, tips }`. Steps are generalized with this-example notes in parentheses (e.g. "fill in the city (this run: Shanghai, Beijing)"); pitfalls go into a separate `tips` field. `tools_used` / `step_count` are filled programmatically. Raw DOM indices/coordinates are **not** stored (they go stale).
- **Hybrid retrieval** — dense vectors (Faiss `IndexFlatIP`, cosine via L2-normalized `text-embedding-v4`) **+** BM25 keyword search (`rank-bm25` + `jieba` Chinese tokenization), fused with **Reciprocal Rank Fusion** (RRF). A `min_score` relevance gate prevents injecting unrelated recipes onto an unrelated task.
- **Injection** — retrieved recipes are appended once to the system prompt before the run, then restored afterward so repeated runs don't accumulate context.
- **Write path is interactive only** — after a successful run you get a candidate preview and a `[y/N]` prompt in the terminal. Non-interactive runs (`--prompt` scripts, non-TTY) skip saving by design.
- **Storage is human-readable** — recipes live in `experience/recipes.jsonl`, one JSON object per line. To edit or delete an entry, hand-edit the file (it's `git diff`-friendly); the index rebuilds on next start.
- **Cleanly optional** — gated by `[experience].enabled`. With it off, nothing is injected and no save prompt appears — zero impact on the main flow. Retrieval is best-effort: missing `faiss` falls back to a numpy dot-product, missing `jieba`/`rank-bm25` disables the BM25 path, and any error is swallowed rather than crashing the run.

New module `app/experience/` (`embedding`, `store`, `retriever`, `recorder`, `__init__`); integration points are `Manus.run()` (inject) and `main.py` (`_maybe_save_experience`, confirm + persist).

See [docs/rag-experience-verification.md](docs/rag-experience-verification.md) for layered verification steps (self-check → offline smoke → real end-to-end → clean-disable regression), validated 2026-06-30.

## 🛠️ Installation

### Method 1: conda

```bash
conda create -n manus_gui python=3.12
conda activate manus_gui
git clone <your-fork-url> manus-gui
cd manus-gui
pip install -r requirements.txt
```

### Method 2: uv (recommended)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <your-fork-url> manus-gui
cd manus-gui
uv venv --python 3.12
source .venv/bin/activate    # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt
```

### Browser automation

```bash
playwright install
```

The RAG experience library needs three extra packages (already pinned in `requirements.txt`):

```bash
pip install faiss-cpu rank-bm25 jieba
```

## ⚙️ Configuration

Copy the example and edit your keys:

```bash
cp config/config.example.toml config/config.toml
```

```toml
# Core LLM
[llm]
model = "claude-3-7-sonnet-20250219"
base_url = "https://api.anthropic.com/v1/"
api_key = "YOUR_API_KEY"
max_tokens = 8192
temperature = 0.0

# Vision model (used by gui_action when DOM recognition fails)
[llm.gui]
api_type = "openai"
model = "qwen3.7-plus"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key = "YOUR_DASHSCOPE_API_KEY"
max_tokens = 2048
temperature = 0.0

# Take over a real logged-in Chrome to bypass anti-scraping
[browser]
cdp_url = "http://localhost:9222"

# RAG experience library
[experience]
enabled = true
embedding_model = "text-embedding-v4"   # build & query must use the same model
top_k = 2                               # how many similar recipes to inject
min_score = 0.35                        # relevance floor gate
rrf_k = 60                              # reciprocal rank fusion constant
```

Notes:
- `[llm.gui]`, `[browser].cdp_url`, and `[experience]` are all optional. Omit `[experience]` (or set `enabled = false`) to run exactly like upstream OpenManus.
- The embedding model reuses the `[llm.default]` DashScope-compatible endpoint, so that section's `api_key` / `base_url` must be valid for retrieval to work.

## 🚀 Quick start

```bash
python main.py
# then type your task at the prompt
```

```bash
# non-interactive, with a higher step budget for complex tasks
python main.py --prompt "Look up Shanghai→Beijing flights on Ctrip and report the price" --max-steps 40
```

Other entry points inherited from OpenManus:

```bash
python run_mcp.py     # MCP tool version
python run_flow.py    # multi-agent run-flow (unstable)
```

## 🧪 Tests

```bash
# experience-library unit tests (offline, embedding/LLM mocked)
pytest tests/test_experience_store.py tests/test_experience_retriever.py tests/test_experience_recorder.py -v
```

## 📁 Where the changes live

| Area | Files |
|------|-------|
| Visual `gui_action` + key/coordinate handling | [app/tool/browser_use_tool.py](app/tool/browser_use_tool.py), [app/tool/gui_agent.py](app/tool/gui_agent.py) |
| RAG experience library | [app/experience/](app/experience/) (`embedding`, `store`, `retriever`, `recorder`, `__init__`) |
| Inject on run / capture terminate status | [app/agent/manus.py](app/agent/manus.py) |
| Interactive save + `--max-steps` | [main.py](main.py) |
| Config schema | [app/config.py](app/config.py), [config/config.example.toml](config/config.example.toml) |
| Verification guide | [docs/rag-experience-verification.md](docs/rag-experience-verification.md) |

## Attribution

manus-gui is a personal derivative of **OpenManus** and redistributes its code under the original **MIT License**. The upstream framework, design, and all original code are the work of the OpenManus authors at [@FoundationAgents](https://github.com/FoundationAgents) / [MetaGPT](https://github.com/geekan/MetaGPT). This fork only adds the GUI-control and RAG-experience features described above. Thanks also to [anthropic-computer-use](https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo), [browser-use](https://github.com/browser-use/browser-use), and [crawl4ai](https://github.com/unclecode/crawl4ai) for the browser/automation support this project builds on.

- Upstream: https://github.com/FoundationAgents/OpenManus

## Cite the upstream framework

```bibtex
@misc{openmanus2025,
  author = {Xinbin Liang and Jinyu Xiang and Zhaoyang Yu and Jiayi Zhang and Sirui Hong and Sheng Fan and Xiao Tang and Bang Liu and Yuyu Luo and Chenglin Wu},
  title = {OpenManus: An open-source framework for building general AI agents},
  year = {2025},
  publisher = {Zenodo},
  doi = {10.5281/zenodo.15186407},
  url = {https://doi.org/10.5281/zenodo.15186407},
}
```
