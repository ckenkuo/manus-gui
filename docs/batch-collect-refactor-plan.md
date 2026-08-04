# 批量采集重构：确定性管道 + 单发判断 + 稳定性护栏

> 本文档为实施计划。可在新 session 中直接打开、按阶段执行。
> 研究结论均带 `文件:行号` 引用，行号以当前工作树为准（改动后可能漂移，按符号名定位更稳）。

## Context（为什么做这件事）

`batch_collect.py` 第二段对每个商品跑一个自由 agent 循环。实测基线（17:58 那批，Excel 权威数据）：
**尝试 17 个、成 8 个（47%）、耗时 4 小时 20 分、中途 3 次重杀 Chrome**。

审计确认两个根因：

1. **成本 O(N²)**：每步 1 次大模型，历史零裁剪全量重发；最大乘数是"247 个交互元素列表"——每步重建、永久留在历史、每步再全量重发（`app/agent/toolcall.py:41-56`、`app/agent/browser.py:98-104`）。无 `max_input_tokens` 兜底（`app/llm.py:256-261`）。单商品烧十万级 token。
2. **零稳定性护栏**：CDP 失联时无确定性重连、无单商品超时、无失败重试（`app/tool/browser_use_tool.py:507-538, 1038-1039`；`batch_collect.py:240` 无 `wait_for`）。错误只丢回大模型让它瞎试——这是后半程 #11→#12 卡 99 分钟的元凶。

**目标**：单商品大模型往返从 7–20 次降到 2–3 次、token 降到万级、杜绝卡死，同款/读价判断质量基本保持。

**关键取舍（已确认）**：同款匹配用**视觉图片匹配**（需加多模态模型档）；落地**分两阶段**，先护栏后管道。

---

## 已验证的事实（研究阶段结论）

- **单发大模型可行**：`LLM(config_name=...).ask([...], stream=False)` 无 agent/memory/browser 依赖，只需 `DASHSCOPE_API_KEY`；retry(tenacity 6 次) + token 计数自动生效（`app/llm.py:361-367, 454-461`）。实例是按 `config_name` 的单例（`app/llm.py:182-191`）。
- **视觉判断需改配置**：当前 `qwen3.7-plus` **不在** `MULTIMODAL_MODELS`（`app/llm.py:39-49`），传图会被**静默丢弃**（`app/llm.py:344-346`）。视觉模型用 **`qwen3-vl-plus`**（已用 DashScope 实测传图可看图、返回商品描述）。因其名不在 `MULTIMODAL_MODELS`（现有仅 `qwen-vl-plus`/`qwen-vl-max`，不含 `3`），**必须把 `qwen3-vl-plus` 加进 `MULTIMODAL_MODELS`**（`app/llm.py:39-49`），否则 `ask_with_images` 会 `raise ValueError`、`ask` 会丢图。
- **1688 详情价 DOM 干净可读**（CDP 实测，无登录墙/验证码）：`module-od-main-price`（价格区间）、`module-od-sku-selection` + `item-price-stock`（每个 SKU 的价/库存）。脚本可直接抓。
- **强制 JSON 范例**：`app/tool/browser_use_tool.py:894-943`（`ask_tool` + tool_choice + `json.loads(tool_calls[0].function.arguments)`）；文本兜底范例 `app/experience/recorder.py:105-135`。
- **已有可复用确定性零件**：第一段枚举 `enumerate_worklist()`、`paste_image` 动作、`WpsExcelTool`、`excel_write_locked()` 预检。

> ⚠️ **勘误（2026-07-03 实测修正 + 已落地）**：本文档原多处（28/65/100/114 行）称 `read_1688_results` 动作已在"上一次未提交 session"实现并端到端验证——**实为不存在**（不在动作枚举、`git log --all -S` 查无、无 stash）。相关 1688 选择器原也只是设想。经连真站（样品 offer 815392475657 + 关键词"水气球"结果页）实测确认、并**已在 `app/collect/pipeline.py` 落地**的真实取法：
> - 结果卡片容器：**`.search-offer-wrapper`（连字符）**，非原写的驼峰 `searchOfferWrapper`。
> - offerId：卡片内 `a[href]` 的 `offerIds=`/`offerId=` 参数，兜底 `data-aplus-report` 的 `object_id@`；**非** `offer/<id>.html` 路径。详情直链由 offerId 拼 `detail.1688.com/offer/<id>.html`。
> - 卡片字段：`.title-text`（标题）/ `img[src*=cbu01]`（缩略图）/ `.price-item`（价格文本）。
> - 详情页价格：`.module-od-main-price`（区间 ✓）/ `.module-od-sku-selection`（SKU 区，1 容器 ✓）/ `.item-price-stock`（各 SKU 价/库存交替 ✓）——这三个文档原本是对的。
> - 落地实现：管道读取器不再做成 browser_use 动作，而是 `pipeline.py` 内直接 `page.evaluate(_READ_RESULTS_JS / _DETAIL_PRICE_JS)`，绕开 `execute_js` 的 2000 字截断。

---

## 阶段一：稳定性护栏（低风险，先做，先止血）

只改 `batch_collect.py` 的 `run_phase2` + 加一个 CDP 健康检查 helper，**不改 agent 内部**。

### 1.1 单商品超时
用 `asyncio.wait_for` 包住 `agent.run(...)`（`batch_collect.py:240`），超时（建议 300s）判失败、跳下一个。超时后 `reset_agent` 已能清状态。

### 1.2 CDP 健康检查 + 确定性重连
新增 `async def ensure_cdp_alive(cdp_url) -> bool`：每个商品开跑前 `connect_over_cdp` ping 一次（复用第一段 `batch_collect.py:78-80` 的连接方式），断了就按固定逻辑等待/重连；**连续 N 次失败才放弃整批**（而不是让大模型 taskkill）。
- 若确实要重启 Chrome，写成脚本里的确定性 `subprocess`（用 `config.toml:36-38` 文档里的 `--remote-debugging-port=9222 --user-data-dir=...` 命令），而非交给 agent。

### 1.3 单商品失败重试
`collect` 失败（超时/异常/未入库）时自动重跑 1 次再放弃。幂等已保证重跑安全（已入库 SPU 跳过，`batch_collect.py:226-227`）。

### 1.4 token 上限兜底
`config/config.toml` 的 `[llm]` 加 `max_input_tokens`（如 60000），让 `check_token_limit`（`app/llm.py:256-261`）在失控前抛错，被 1.3 的重试兜住。

**阶段一验证**：`python batch_collect.py --limit 5`，确认无单商品超 5 分钟、CDP 断开能重连、失败自动重试一次。对比基线耗时。

---

## 阶段二：确定性管道 + 单发视觉判断（核心降本）

把第二段的 agent 自由循环替换为确定性例程。**新建 `app/collect/pipeline.py`**（或就近放 `batch_collect.py`），`run_phase2` 改为逐商品调 `collect_one_product(page, item)`。

### 2.1 新增多模态视觉档
- `config/config.toml` 新增 `[llm.samematch]`（或复用 `[llm.vision]` 改 model）：`model = "qwen3-vl-plus"`（已实测可用），api_key 留空走 env。
- `app/llm.py:39-49` 的 `MULTIMODAL_MODELS` 加入 `"qwen3-vl-plus"`（否则图被静默丢弃）。

### 2.2 确定性管道（每商品，复用已登录 Chrome 的单个 page）
```
[脚本] 1. requests 下载主图到 image_dir/{spu}.jpeg（已有逻辑）
[脚本] 2. 打开 1688 + paste_image 以图搜图（调 BrowserUseTool 的 paste_image 动作）
[脚本] 3. read_1688_results → offerId/detailUrl/price/title/img JSON（调已有动作）
[LLM×1] 4. 判断点A（视觉）：ask_with_images(目标图 + 候选缩略图, "选出同款的 offerId")
         → 返回一个 offerId（结构化）。候选缩略图 img URL 已在结果里。
[脚本] 5. go_to_url detailUrl；抓 module-od-sku-selection / item-price-stock 价格+规格 DOM
[LLM×1] 6. 判断点B：喂价格块文本+规格 → {常规批发价, 运费, 估重}（剔除首单/新人价）
         qwen3.7-plus 文本单发即可；估重合并进同一次调用。
[脚本] 7. WpsExcelTool inspect + append_product_row 写入（列映射照现有 per_product_prompt）
[脚本] 8. close_tabs(text="1688")
```
单商品大模型往返 = **2 次**（判断点A视觉 + 判断点B文本），每次输入小、无历史累积。

### 2.3 判断点封装
- A：`ask_with_images`（`LLM(config_name="samematch")`）；提示词给目标图+编号候选，要求只回一个 offerId。图 URL 可直接传（`ask_with_images` 接受 URL 字符串，`app/llm.py:571-581`）。
- B：`LLM().ask([...], stream=False)` 拿文本，按 `recorder.py` 方式剥 ```json 围栏解析（qwen3.7-plus 的 required 会被降级，文本更稳）。
- 两处都 best-effort：解析失败→记失败走阶段一的重试；同款价明显偏离 Temu 售价过多→标记存疑写入备注列 T（不阻断）。

### 2.4 保留 agent 兜底（可选）
管道任一确定性步骤连续失败，可退回旧 `agent.run` 路径跑该商品（保留旧函数）。这样重构不是背水一战。

**阶段二验证**：
1. `--limit 3` 冒烟，日志确认单商品仅 2 次大模型调用、无 247 元素历史、token 万级。
2. 抽查写入的 3 行采购价/重量是否合理（对比人工判断）。
3. `--limit 20` 全批，对比基线：命中率、耗时、总 token。

---

## 改动文件清单

| 文件 | 阶段 | 改动 |
|---|---|---|
| `batch_collect.py` | 一 | `run_phase2` 加超时/重连/重试；新增 `ensure_cdp_alive` |
| `config/config.toml` | 一/二 | 加 `max_input_tokens`；新增 `[llm.samematch]` 多模态档 |
| `app/collect/pipeline.py`（新） | 二 | `collect_one_product` 确定性管道 + 2 个单发判断封装 |
| `app/tool/browser_use_tool.py` | 二 | 若需，暴露 paste_image/read_1688_results 供脚本直接调（已是 `execute(action=...)` 可调，可能无需改） |

**不改**：第一段枚举、`WpsExcelTool`、`read_1688_results`/`paste_image` 动作内核、agent 主循环（阶段一不碰；阶段二仅作兜底保留）。

## 风险与回退
- 视觉档模型名错→`ask_with_images` 抛 ValueError：`qwen3-vl-plus` 已用一次性脚本实测可用（传图能返回商品描述）；接入后仍须确认已加进 `MULTIMODAL_MODELS`。
- 判断质量下降：2.3 的存疑标记 + 2.4 的 agent 兜底双保险。
- 分阶段本身即回退策略：阶段一独立可用、可单独交付。

---

## 附：本次会话已完成的前置改动（未提交）

以下改动已落地并验证，是本重构的基础，勿重复做：
- `batch_collect.py`：主图改 python 直连下载（去掉必挂的 execute_js fetch）；`max_steps` 15→20；per_product 第2步走 `read_1688_results`。
- `app/prompt/manus.py`：1688 规则重塑（框选主体、关键词兜底防 GBK 乱码、挑同款用新动作禁 execute_js 捞 `<a>`）。
- `app/tool/browser_use_tool.py`：新增 `read_1688_results` 动作（限定 `searchOfferWrapper` 卡片内解析 offerId → `detail.1688.com/offer/<id>.html` 直链），已端到端验证返回 24 条干净结果。
- `experience/recipes.jsonl`：删脏 recipe #2（task 录成模板、教 execute_js 捞列表），留 recipe #1；备份 `recipes.jsonl.bak`。

---

## 云端协作文档模式（2026-08-04 增补）

商品采集管线打通金山协作文档（Kdocs），复用订单管线的云端后端
（`app/orders/kdocs_sheet.py` 的 `KdocsSheet`，经 kdocs-cli 读写云端表格）。
CLI（`batch_collect.py --excel <链接>`）与采集页面（工作簿输入框直接粘贴链接）都支持。

**配置键**（`config/config.toml` 的 `[collect]` 段，缺失时回退 config.example.toml）：

```toml
[collect]
cloud_file_id = ""   # 非空时采集默认写云端；UI/CLI 显式粘贴的链接优先于此
cloud_link = ""      # 人类可读的分享链接，仅用于 UI 回显
```

**目标优先级**（`app/collect/service.py` 的 run_batch / get_worklist_status 同一口径）：
显式粘贴的协作文档链接 > `cloud_url` 参数 > prefs 的 `cloud_url` > `[collect].cloud_file_id`
> 本地 xlsx（无云端目标时行为完全不变）。prefs 里链接与本地路径互斥：
`save_prefs` 把链接拆到 `cloud_url` 键、清空 `excel`（对齐订单页），显式选本地路径则清掉旧链接。

**公式模板从云端学出**：云端 API `sheet.get_range_data` 的响应里 `fmlaText` 返回公式原文、
`cellText` 是显示值。`KdocsSheet.read_data_sample` 读表头下前 10 行数据区，
`resolve_sheet_schema_cloud` 逐列取第一条 `fmlaText`（跳过图片列与含 `DISPIMG` 的坏行），
用与本地同一正则把行号换成 `{r}` 占位——无需本地 xlsx 模板。常量列是本地
`_scan_numeric_constants` 的简化版：公式引用到、非公式列、非逐商品字段列的列里，
采样行非空 `cellText` 全是同一个纯数字 → 收为常量。表头→字段映射复用本地同一个纯函数
`WpsExcelTool._resolve_fields_from_header`，两条路径口径一致。

**与订单管线的差异**：
- 采集写「值 + 公式列」同批（公式经 `cell_operation_type_formula` 写入，行号 =
  header_row + 1）；订单只写值。values 里普通值在前、公式最后——`write_rows` 的读回校验
  取第一个非空值比对 `cellText`，公式格的显示值是计算结果而非公式串。
- 采集逐商品单行写（每商品一次插行+写入）；订单是整批计划后批量写。
- 采集云端模式跳过 `excel_write_locked` 预检与主图本地下载（图片走 `item["image"]`
  的 kwcdn URL 嵌入，`write_rows` 内部已做 avif→jpeg）；纯 agent 兜底路径
  （`--no-pipeline`）自己 inspect 本地表，云端模式下在 run_batch 开头中止。
