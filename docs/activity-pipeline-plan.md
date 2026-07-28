# 活动管理管线 · 落地文档

> 本文档由规划 session 产出，供新 session 直接执行。自包含：读完即可开工。
> 开发方式：**主 agent 调度 subagent 分波次施工**，波次间主 agent 验收 + 跑 import/语法检查再放行。

## 0. 一句话目标

给定一批 SPU，对每个商品在 Temu 商家后台（agentseller.temu.com）依次执行：
**关流量加速器 → LLM 选活动并报名(填申报价) → 重开流量加速器**，
并封装成「像采集页那样的 web 可视化操作页」+ CLI，二者共用 service 层。

## 1. 已确认的决策（本 session 问答结论，不要再推翻）

| 维度 | 结论 |
|---|---|
| 处理范围 | **我给定的 SPU/SKU 列表**（不是全店扫、不是页面筛选圈定） |
| 执行方式 | **自己 RPA 原生操作**（裸 Playwright over CDP，不点已装的「Temu商家助手」插件按钮，不走 Manus agent 自由循环） |
| 活动选择 | **由 LLM 判断每个商品该报哪个活动**（不是固定活动、不是一键全报） |
| LLM 判断点 | ①选活动 ②判价：活动申报价是否守利润红线 ③结果校验：操作后确认状态确实变更 |
| 成本来源 | **读 Excel 成本核算表**（按 SPU 匹配拿成本） |
| 申报价策略 | 用户让"我先定默认方案，实施时再调"→ 默认 `discount_ceiling`（贴折扣上限取最高价） |
| 毛利率红线粒度 | **全局默认 + 可逐品覆盖**（三级回退，见 §4） |
| 交付形态 | **web 可视化操作页**（对标现有 `/collect` 采集页）+ CLI |

## 2. 探到的页面事实（2026-07 实测，agentseller.temu.com）

**流量页 `main/flux-analysis`（标题「商品流量」）**
- 每个商品一行，行文本内含 `SPU ID： {数字}`（注意是**全角冒号 +空格**），可用于定位行。
- 行内操作是 `<a>` 标签：`立即开启`(流量加速器行内入口) / `查看详情` / `去补货` / `去投放推广`。
- 顶部「快速筛选」标签：`流量加速器待开启` / `流量加速中` / `Max模式待开启` / `商品流量待关注` / `全球Best Seller同类商品流量可提效`。
- 工具栏批量按钮：`批量关闭流量加速` / `批量打开流量加速` / `导出无流量在售商品` / `导出有流量商品`（**本方案不用这些插件/批量按钮，走逐商品原生操作**）。
- ⚠️ 当前探测停在「流量加速器待开启」筛选，只看到「立即开启」；**关闭入口须在「流量加速中」筛选下才出现，尚未探测确认**（阶段2/3 受控确认）。

**活动页 `activity/marketing-activity`（标题「营销活动首页」）**——每行右侧一个 `<a>报名`：

| 活动主题 | 折扣条件 | 库存条件 |
|---|---|---|
| 清仓甩卖 | ≤ 7折 | ≥ 10个 |
| 限时秒杀 | ≤ 8.5折 | ≥ 30个 |
| 官方大促 | ≤ 9折 | ≥ 30个 |
| 额外降价超级万人团 | 详见商品提报列表 | ≥ 15个 |

- ⚠️ **报名弹窗结构尚未探测**（刻意没点「报名」，怕触发真实报名）。阶段2 第一步先只读探测。

**环境事实**
- 已登录的调试 Chrome 在 `http://localhost:9222`（CDP）。playwright 连法：`p.chromium.connect_over_cdp(cdp)`，取 `browser.contexts[0]`，遍历 `ctx.pages` 按 `page.url` 里 `flux-analysis` / `marketing-activity` 找到对应页。
- Windows 控制台中文乱码：探测/调试 dump 一律写 **UTF-8 JSON 文件**再用 Read 读，勿直接 print DOM。
- 项目根 `c:\Users\Administrator\Desktop\manus-gui`，git 仓库，分支 `feature/checkout-shipping-compare`。

## 3. 架构（照搬现有采集页三层 + web 层）

现有采集功能是范本，**强烈建议新 session 先读**：
- `app/collect/pipeline.py`（确定性单商品例程 + 2 次单发 LLM 判断点）
- `app/collect/service.py`（UI/CLI 共用编排 + 结构化进度事件 + CDP 护栏）
- `app.py` 里 `/collect/*` 接口 + `CollectJob` 类（SSE 作业）
- `templates/collect.html`（独立可视化页 + SSE 消费）
- `batch_collect.py`（CLI 薄壳）

新功能对标落位：
```
app/activity/__init__.py    新建
app/activity/pipeline.py    新建  ← 对标 collect/pipeline.py
app/activity/service.py     新建  ← 对标 collect/service.py
app.py                      改：加 /activity/* 接口 + ActivityJob  ← 对标 /collect/*
templates/activity.html     新建  ← 对标 collect.html
templates/index.html        改：左上角「批量采集」旁加「活动管理」入口按钮
app/tool/wps_excel_tool.py  改：加 read_row_by_key（✅ 已完成，见 §6 Wave1）
config/config.example.toml  改：加 [activity] 段（✅ 已完成，见 §6 Wave1）
activity_manage.py          新建：CLI 薄壳  ← 对标 batch_collect.py
```

## 4. 毛利率红线：三级回退（本 session 重点确认）

- **全局默认**：`config.toml` 的 `[activity] min_margin`（出厂 0.15），见 §6 Wave1 已写入 example。
- **本批统一值**：web 页参数框 / CLI `--min-margin 0.2` 覆盖全局默认。
- **逐品覆盖**：SPU 清单支持 `9072868889:0.25` 语法（该品单独用 25%），缺省回退本批值。
- 生效顺序：逐品 > 本批 > 全局默认。
- 红线价 = `成本 ÷ (1 − 生效毛利率)`；判定 `(申报价−成本)/申报价 ≥ 生效毛利率`。
- 申报价 < 红线价 → **`skip_redline` 不报**（记录，不提交）。

## 5. 单 SPU 确定性流程（service 编排、pipeline 执行）

```
1. 流量页 evaluate 按 "SPU ID： {spu}" 定位行 → 读加速器状态
2. 若加速中 → 点关闭 → 校验①确认已关   （dry-run 只记录"将关"、不点）
3. 活动页 evaluate 读 4 活动的折扣/库存条件
4. [LLM 判断点A 选活动]：喂 日常价 + 成本红线 + 库存门槛 + 4活动 → {activity, reason}
5. 点该活动「报名」→ 弹窗 → 在提报列表定位该 SPU → 读日常价/库存
6. [判价+守红线]：申报价 = 日常价 × 活动折扣率；< 红线价 → skip_redline 不提交
7. dry-run: 只发 product_plan 事件；正式: 填申报价 → 提交 → 校验②确认已报名
8. 回流量页 → 点「立即开启」→ 校验③确认加速中   （dry-run 只记录、不点）
```

折扣率解析：`7折→0.7`、`8.5折→0.85`、`9折→0.9`；「详见商品提报列表」类无固定折扣的活动，
LLM 选中后申报价须依赖弹窗里的实际要求（阶段3 落地时按弹窗字段处理，阶段1 可先跳过这类）。

## 6. 结构化进度事件契约（对标 collect service，on_progress 收 dict，均含 "type"）

```
{"type":"batch_start",   "total":int, "todo":int, "batch":int, "dry_run":bool}
{"type":"product_start", "index":int, "total":int, "spu":str, "name":str}
{"type":"product_plan",  "spu":str, "activity":str, "daily_price":float|None,
                         "cost":float|None, "margin":float, "submit_price":float|None,
                         "red_line":float|None, "within_red_line":bool, "reason":str}
{"type":"product_done",  "spu":str,
                         "status":"done"|"skip_redline"|"skip_nomatch"|"skip_nocost"|"fail",
                         "accel_closed":bool, "enrolled":bool, "submit_price":float|None,
                         "accel_reopened":bool, "verified":bool, "note":str}
{"type":"batch_done",    "done":int, "skip":int, "fail":int}
{"type":"aborted",       "reason":str}
{"type":"log",           "level":"info"|"warning"|"error", "message":str}
```
on_progress 兼容同步/异步（内部统一 await），回调异常只告警不阻断——照抄 collect/service.py 的 `_emit`。

## 7. 安全设计（真实商家账号、不可逆 —— 最高优先级，写进每个 subagent 的 prompt）

1. **默认 dry-run**：默认只读+算+出计划，**绝不点任何变更按钮**（关加速/报名/开加速）。web 页有开关，
   默认关（dry-run）；审完计划再开「正式执行」。对标 collect 的 `base_only=True` 默认。
2. **报名弹窗结构受控探测**：弹窗刻意没点。阶段2 第一步先做只读探测（打开→dump 结构→**不提交直接关**），
   确认「提报列表定位 SPU / 申报价输入框 / 提交按钮」选择器后再写变更逻辑。
3. 每步操作前先读状态，已是目标态则跳过（幂等）；日常价读失败 / 弹窗找不到该 SPU / 申报价<红线 / 成本读不到 → **一律不提交**。
4. 成本从 Excel 读到的值若以 `=` 开头（公式单元格），当作"读不到成本"保守跳过（`skip_nocost`）。
5. 复用 `collect/service.py` 的 `ensure_cdp_alive` CDP 健康检查 + 单 SPU 超时。

## 8. LLM 判断点调用模式（实测，照抄）

```python
from app.llm import LLM
from app.schema import Message

llm = LLM(config_name="default")          # 复用进程级单例
raw = await llm.ask(
    messages=[Message.user_message(user_text)],
    system_msgs=[Message.system_message(system_text)],
    stream=False,
    temperature=0.0,
)
data = _parse_json(raw)                    # 见 collect/pipeline.py 的 _parse_json：剥 ```json 围栏 + 兜底抓首个 {...}
```
判断点A（选活动）产出建议 JSON：`{"activity": "官方大促", "reason": "..."}` 或 `{"activity": null, "reason": "都不合适"}`。
> ⚠️ LLM 单例 token 会跨商品累加，须在每商品开始时清零，照抄 collect/service.py 的 `reset_pipeline_llms()`（对 "default" 单例清零 total_input_tokens/total_completion_tokens）。

## 9. Excel 成本读取（Wave1 已交付的接口）

```python
from app.tool.wps_excel_tool import WpsExcelTool
# 先解析该 Sheet 的逻辑字段→真实列（各 Sheet 列序不同，勿硬编码）
cols = WpsExcelTool.resolve_field_columns(excel, sheet)   # {"spu":"D","purchase":"J","daily":"G","sale":"I",...}
spu_col = cols.get("spu", "D")
row = WpsExcelTool.read_row_by_key(
    excel, sheet, key=spu,
    cols={"purchase": cols.get("purchase","J"), "daily": cols.get("daily","G"), "sale": cols.get("sale","I")},
    key_col=spu_col,
)   # → {"purchase":"3.5","daily":"9.9",...} 只含非空项；找不到该 SPU 行返回 {}
```
成本 = `purchase` 列数字（剥非数字字符后 float）；`=` 开头视为读不到 → skip_nocost。

## 10. subagent 分波次派发（主 agent 编排）

主 agent 先锁定 §6 事件契约 + §11 函数签名，写进每个 subagent 的 prompt，保证接口互锁。
分 3 波，波次间主 agent **验收 + 跑 `python -c "import ..."` + 语法检查**再放行下一波；接口不符就回退重派。

### Wave 1 — 基础层 ✅ 已完成
- `WpsExcelTool.read_row_by_key`（见 §9，已交付并验证 `hasattr(...)==True`）
- `config.example.toml` 加 `[activity]` 段（`min_margin=0.15`、`price_strategy="discount_ceiling"`，`tomllib` 校验通过）

### Wave 2 — 活动核心包（1 个 subagent 独占，保证 pipeline/service 签名一致）✅ 已完成（2026-07-16）
产出 `app/activity/__init__.py` + `pipeline.py` + `service.py`。**阶段1 只做只读 + dry-run，变更动作（关/报/开）留桩**（函数存在、被 dry-run 分支跳过、正式分支先 `raise NotImplementedError("阶段3")` 或返回未实现标记）。
- pipeline：选择器常量 + 只读函数（定位行/读加速器态/读活动列表）+ judge_activity(LLM) + compute_submit_price + 三级毛利率解析 + verify_state 桩。
- service：`run_activity_batch(...)` 编排 + 结构化进度事件 + dry-run 护栏 + `ensure_cdp_alive` + `reset_pipeline_llms` + SPU 清单解析（`spu:margin` 语法）+ Excel 成本读取。
- 验收：`python -c "from app.activity import service, pipeline"` 通过；dry-run 跑一个假 SPU 不报错、能产出 product_plan 事件序列。

### Wave 3 — Web 层（依赖 Wave2 事件契约）✅ 已完成（2026-07-16）
产出 `app.py` 的 `/activity/*` 接口 + `ActivityJob`（照抄 CollectJob/SSE）、`templates/activity.html`（照抄 collect.html：统计卡片/工作簿+Sheet 级联/SPU 输入框/毛利率+dry-run 参数条/进度条+事件日志/结果表）、`templates/index.html` 加入口、`activity_manage.py` CLI 薄壳。
- 验收：起服务 `python app.py`，访问 `/activity`，dry-run 跑通、SSE 进度可视。

## 11. Wave 2 pipeline/service 建议函数签名（主 agent 定的契约）

```python
# app/activity/pipeline.py
FLUX_URL = "https://agentseller.temu.com/main/flux-analysis"
ACTIVITY_URL = "https://agentseller.temu.com/activity/marketing-activity"

async def locate_product_row(page, spu: str) -> dict | None: ...      # 只读：{found, accel_state, row_text}
async def read_accel_state(page, spu: str) -> str: ...                # "on"|"off"|"unknown"
async def read_activities(page) -> list[dict]: ...                    # [{name, discount_rate, min_stock, raw}]
async def judge_activity(daily_price, cost, red_line, activities: list) -> dict: ...  # LLM → {activity, reason}
def compute_submit_price(daily_price, discount_rate, cost, margin) -> dict: ...       # {submit_price, red_line, within_red_line}
async def close_accel(page, spu) -> bool: ...   # 阶段3；阶段1 dry-run 不调用
async def enroll_activity(page, spu, activity, submit_price) -> bool: ...  # 阶段3
async def open_accel(page, spu) -> bool: ...    # 阶段3
async def verify_state(page, spu, expect: str) -> bool: ...  # 校验③；阶段1 桩

# app/activity/service.py
def parse_spu_list(text: str, batch_margin: float) -> list[dict]: ...  # 解析 "spu" / "spu:margin"
async def run_activity_batch(spus, excel, sheet, min_margin, dry_run=True, on_progress=None) -> dict: ...
```
毛利率归一化：输入 20 或 0.2 都当 20% —— `m = m/100 if m > 1 else m`。

## 12. 分阶段落地（安全递进）

- **阶段1（全只读 + web 跑通）**：Wave2+Wave3 的只读/dry-run 路径。页面能看到每 SPU 完整计划（关谁/报什么活动/申报价/守红线/开谁），**零变更**。
- **阶段2（只读弹窗探测）**：打开报名弹窗 dump 结构（不提交直接关），确认提报列表定位 SPU / 申报价输入框 / 提交按钮选择器，回填进 pipeline。
- **阶段3（正式执行开关）**：实现 close_accel / enroll_activity / open_accel + 每步 verify_state；web 开关放开「正式执行」。每步失败即停、不硬闯。

## 13. 交接状态

- ✅ Wave 1 已完成并验证（read_row_by_key + config [activity]）。
- ✅ Wave 2 已完成并验证（2026-07-16）：`app/activity/{__init__,pipeline,service}.py`。
  独立验收：`from app.activity import service, pipeline` 通过；stub page 跑 dry-run 假 SPU 产出
  完整事件序列 `batch_start→product_start→product_plan→product_done→batch_done`；dry-run 下
  变更桩（close_accel/enroll_activity/open_accel）零调用（哨兵未触发）；compute_submit_price
  算价与守红线判定正确（9.9×0.9=8.91，红线 cost/(1-margin)，四舍五入到分位）。
- ✅ Wave 3 已完成并验证（2026-07-16）：`app.py` 加 `/activity`、`/activity/worklist`、
  `/activity/batch`、`/activity/batch/{job_id}/events`(SSE) + `ActivityJob`（照抄 CollectJob）；
  `templates/activity.html`（新建）；`templates/index.html` 加「活动管理」入口；`activity_manage.py`（CLI）。
  独立验收（importlib 加载 app.py + FastAPI TestClient）：4 条 /activity 路由注册、GET /activity 返回
  200 且含中文、首页有入口、/activity/worklist 复用 collect 枚举返回含 min_margin；CLI `--help` 正常，
  `--dry-run` 默认 / `--execute` 需显式。`/activity/worklist` 复用 `collect_service.get_worklist_status`（不重复造）。
- ⚠️ 阶段1 收尾修正：Wave2/3 subagent 产物里跟随 collect.html 风格误带了 emoji/装饰符（✎⚠📋▶❌✅⬜⏱），
  已按 CLAUDE.md「禁 emoji」全部清理为中文方括号标签（`[计划]/[结果]/[完成]/[失败]/[跳过]`），
  流程箭头 `→` 属普通标点予以保留；既有 `app.py` 里匹配 Manus 日志的 emoji 字面量（✨🛠🎯📝🏁）是既有代码不动。
- ✅ 判定核心重构 + 实机验证（2026-07-17，Leoaqr全球店，见另一份 plan `quiet-popping-nova.md`）：
  用户重定义判定——**废弃毛利率红线，改「申报价(=日常价×活动折扣率) ≥ 销售底价(Excel 销售价列 I，
  用户手填常量) 且 库存 ≥ 活动库存门槛 的活动全部报名」**（满足的都报，一 SPU 多活动，不再 LLM 选一个）。
  落地要点：
  - `compute_submit_price(daily, discount_rate, sale)` → `within_floor = submit ≥ sale`；成本/毛利率不再判定。
  - `read_activities` 改：完整活动名（不再 text[:12] 截断）+ 按活动名去重（保留带 min_stock 的版本）+
    折扣率解析修 bug（优先取「≤X折」条件、「85折」类两位整数二次归一 /100）。实测活动页近百个活动。
  - 新增 `read_stock(page, product_ids)`：库存在接口 `skc/pageQuery`（成本表 SPU ID == 接口 productId，
    库存=各 SKU virtualStock 之和）。自造 fetch 缺 anti-content 会 403，故被动监听 reload goods/list 捕获响应。
  - `_process_one_spu` 改多活动循环筛选 + 卡库存；skip 语义 `skip_nofloor`(底价缺)/`skip_nomatch`(无入选)。
  - 事件契约：product_plan 一 SPU 多条（新字段 sale/within_floor/stock/min_stock/selected）；
    product_done 加 enrolled_activities；activity.html 改一 SPU 多行、列名「销售底价/达底价/库存/门槛/入选」、去毛利率框。
  - 实机验证：3 SPU（库存 299/200/200）经真实 web 服务 dry-run，各入选 30/30/61 个活动，
    事件流 batch_start/product_start×3/product_plan×121/product_done×3/batch_done 完整，零变更、无重复名。
- ✅ 报名执行遍打通 + 半程实机验证（2026-07-20，Leoaqr全球店，见 plan `quiet-popping-nova.md`）：
  修了「5 个 9折活动 live 提交后『已报名』列全是 "-"、一个都没报成功」。用户在旁盯浏览器逐步纠正，
  定位 3 个根因并全部修复（`app/activity/pipeline.py`）：
  - **根因1 撞名点错活动**：`_MARK_ENROLL_JS` 旧逻辑「找含名最小元素→向上爬 8 层→抓第一个报名按钮」，
    虚拟表格里祖先含相邻行 → 抓到隔壁活动按钮（85折「周末48H爆款冲刺」撞进 6折「周末48H大折扣专区」=亏钱）。
    改为**从报名按钮反查行、行首文本须 `===` 目标活动名、且行内唯一报名按钮**才标记。
  - **根因2 缺 SPU 搜索**：详情页(detail-new)默认虚拟列表不含目标 SPU，必须先在顶部搜索区搜。
    新增 `_SEARCH_SPU_JS`（搜索框 = 值为「SPU ID」的标签框同行、x 更大、placeholder 含「输入」的 input）
    → fill(spu) → 点「查询」→ 轮询等结果行（搜索区异步渲染，加 10s 等待循环）。搜索后结果**须唯一**含
    `SPU ID: {spu}`，非唯一/为 0 一律保守跳过、绝不误报。
  - **根因3 没选场次（报名全失败的直接原因）**：新增 `_set_sessions_all(page)`——勾选商品后点
    「批量设置场次」→ 弹窗（beast `MDL_outerWrapper`、含「设置场次」标题，非 role=dialog）→ 点「全选」→
    校验有场次勾中 → 点「确认」。不设场次提交无效。用户确认「反正全报」用批量即可。
  - beast 表格坑：行拆成**左固定列（报名场次/勾选 CBX）+ 右滚动列（SPU ID）两个独立 tr**，
    勾选按**视觉 y 对齐**（`_CHECK_ROW_JS`：找「可报名场次」文本 y，勾同 y band±30 的 CBX 外层 wrapper）。
  - `open_enroll_page` 的活动名核对**已移除**：详情页 body 不含活动名（是通用导航+商品表、title 为「卖家中心」），
    做文本核对会误杀每个页面。防撞名靠已修好的 `_MARK_ENROLL_JS` 精确匹配。
  - 半程验证（allow_submit=False，官方大促 9折，SPU 2801689369）：`located/checked/sessions_set/filled`
    全 True，**页面「已选场次」0→2**（场次真选上了）；submitted 正确停在 False。8 个现有单测通过。
- ⬜ **阶段3 正式全流程实跑（下个 session 接手，24h 冷却已过）**：取消流量加速 → 报活动 → 开流量加速。
  见下「## 14. 下个 session 全流程实跑交接」。
- 记忆已存：`memory/activity-pipeline.md`（含最新判定逻辑、选择器与库存接口事实）。

## 14. 下个 session 全流程实跑交接（2026-07-20 起，24h 冷却已过）

目标：跑通**取消流量加速 → 报活动 → 开流量加速**的真实不可逆全流程（先小范围 1 活动验证，再放量）。

前置事实（已就位）：
- 报名执行遍 `enroll_activity(page, spu, activity, submit_price, allow_submit=False)` 已打通全序列
  （搜索→勾选→场次全选+确认→填价），默认停在提交前。**正式提交须显式 `allow_submit=True`**，
  或用 `submit_enroll_page(page, allow=True)` 一次提交本页已填的所有 SPU（活动维度批量）。
- 折扣+库存判定在 `service._process_one_spu` 已正确（`within_floor` 达底价 + `stock_ok` 够库存，
  折扣从活动页第 3 列「申报价格条件」解析）。规划层不用动。
- 加速开关：`read_accel_state / close_accel / open_accel`（流量页），加速档统一选「超级流量加权」
  （`_SELECT_SUPER_TIER_JS`，用户规则 2026-07-17）。

**强烈建议的实跑顺序（安全递进，每步人工确认）**：
1. **单活动单 SPU 真提交**：只对 1 个 9折活动、1 个 SPU 调 `enroll_activity(..., allow_submit=True)`
   → 回营销活动页看该活动「已报名」列由 "-" 变为已报名数（**这是成功的唯一权威判据**）。成了再往下。
2. 确认无误后，再按「取消加速 → 批量报名 → 开加速」串起单 SPU 全流程，仍限 1 SPU。
3. 全绿后才放量到多 SPU。

风险与防呆：
- 报名不可逆。务必逐活动核对「申报价格条件」列折扣 + 「库存条件」列（`within_floor`+`stock_ok` 已做）。
- 撞名已修，但放量前建议 log 每个 `_MARK_ENROLL_JS` 返回的行首文本，肉眼抽查确认没点错活动。
- 每步失败即停、不硬闯（close_accel/open_accel/enroll 任一步 verify 不过就停并报状态）。
- 加速「取消了要记得开回来」：全流程结尾必须 `open_accel` 并 verify 加速态恢复 on。

---

## 15. 全流程实跑结果 + 修复交接（2026-07-20，交给下个 agent）

测试对象：SPU **2801689369**（哥伦比亚站，Leoaqr全球 Sheet）。本 session 首次真正跑了
`run_activity_batch(dry_run=False, live=True)` 全流程（前几个 session 只单调 `_process_one_spu`/
`enroll_activity`，从没走执行遍）。**结论：报名遍能真把「已报名」列刷成数字（步骤1 达成），
但全流程暴露了一串真实 bug，已修 5 处 + 补单测（共 12 个全绿）。**

### 15.1 已达成的权威验证
- 报名遍**真提交成功**：爆款9折（7月第三周）、爆款9折（7月第四周）两个活动的「已报名」列
  由 "-" 变 "1"（回活动页直降 tab 核对，§14 铁律的唯一权威判据）。**submitted=True 仍不可信**，
  必须回活动页看已报名列。
- 规划层正确：数据修好后 13 个候选正确收敛到 **4 个 9折活动**（官方大促、爆款9折×2、直营品牌
  第14期），申报价 47.31；85折/8折因达不到底价 46 被正确淘汰。

### 15.2 本 session 修的 5 个 bug（都在 pipeline.py / service.py，已单测）
1. **参考价上限校验**（`pipeline.enroll_activity`）：提报页每行有「参考价」硬上限（平台规则
   「申报价不可大于参考价」）。填价前读该行参考价，若 `submit_price > 参考价` → **不填不提交、
   记 `over_ref` 失败**，note 写清「哪个 SPU/活动、Excel 算出价 vs 参考价、疑 Excel 日常价与
   前端实际售价不一致」。**不擅自改价重报**（用户明确要求：记失败、交操作者核对 Excel）。
   `service` 层把这类失败聚合进 `summary["failed"]`，`exec_done` 事件带 `failed` 明细。
2. **submit 按 disabled 保护**（`pipeline.submit_enroll_page`）：点「提交」前判 `is_disabled()`，
   禁用即优雅返回（本页无已填 SPU 时按钮 disabled）；`click` 加 8s 超时 + 异常捕获。
   旧代码硬点 disabled 按钮会等满 30s 抛异常。
3. **报名遍异常不再中断重开流量**（`service._run_execution_phases`）：`_enroll_by_activity` 整段
   包 try/except，异常记 `errors` 但**继续走阶段三重开流量**。这是最严重的健壮性缺陷——旧代码
   一个活动提交异常会冒泡中断执行遍，**导致阶段一关掉的流量永久留在关闭状态**。
4. **逐活动报完立刻关提报页 tab**（`service._enroll_by_activity`，**用户本 session 核心要求**）：
   每个活动处理包 try/finally，成功/失败/异常都 `page.close()`。旧代码报完不关，detail-new tab
   一个个堆着，下个活动 `open_enroll_page` 靠 diff 认新 tab 时被残留干扰 → 首个开不出、末个搜
   0 行等时序失败。**始终只留一个提报页，diff 才可靠。**
5. **read_activities / open_enroll_page 加重试**：
   - `read_activities`：点直降 tab→轮询等列表渲染（body 现「符合条件的活动数量」）→抓行，
     3 轮抓到非空即停（修偶发读 0 行→误判 skip_nomatch）。
   - `open_enroll_page`：「标记→点报名→同意规则→diff 认新 tab」整段 3 次重试，每次短超时，
     失败重新标记再点（修「第一个活动常打开提报页失败」——标记按钮能命中，是点击→开新 tab flake）。

### 15.3 查清、无需再查的真相（省下个 agent 的弯路）
- 「爆款9折 re-run 搜索 0 行」= **已报名 SPU 不再出现在提报页搜索**，是正常表现，不是 bug。
- 「skip_nomatch（无活动达底价）」某次的根因 = **活动列表 tab 消失了**（外部关闭/导航走），
  不是逻辑问题。重开 `marketing-activity` tab 即恢复（读到 44 个活动）。
- 参考价 47.31 反推：**Excel 日常价曾与前端实际售价不一致**（Excel 记 58.41，前端约 52.57）。
  用户已把 Excel 日常价改为 **52.57**、底价改为 **46**。现 9折申报价 = 52.57×0.9 = 47.31。

### 15.4 当前 batch 状态（脏，别盲跑全流程）
- 加速器：**仍 ON**（2801689369 加速开启不满 24h，关流量会撞冷却 toast，按设计冷却不拦报名，
  本 session 从未真关成任何流量）。
- 已报进：爆款9折（第三周）、爆款9折（第四周）。
- **未报**：官方大促、直营品牌专属活动（第14期）。
- 活动列表 tab 本 session 结尾是新开的（reused=false），确认可读。

### 15.5 下个 agent 的明确下一步
1. **先干净验证 §15.2 第 5 项修复**：对**官方大促单活动**跑一次 live（它上两轮都「打开提报页
   失败」，是验证 open_enroll_page 重试的最佳靶子）→ 看它这次能否开页、报进（已报名列 -→数字）。
2. 再补报**直营品牌（第14期）**：先只读确认该 SPU 在该活动提报页搜索能否出现（上轮搜 0 行，
   需判断是「本就不合格」还是「时序失败」——后者已被重试覆盖）。
3. 加速器冷却过后（开启满 24h），验证**关流量→报名→重开流量**完整串联，重开加速价 = 底价+1
   = **47.0**，且必须 verify 加速态恢复 on。注意重开若 47.0 > 超级档「参考申报价格」会中止不开。
4. 全绿后才放量多 SPU。

### 15.6 本 session 改动文件
- `app/activity/pipeline.py`：enroll_activity 加参考价校验；submit_enroll_page 加 disabled 保护；
  read_activities / open_enroll_page 加重试。
- `app/activity/service.py`：_enroll_by_activity 逐活动 try/finally 关 tab + 空填跳过提交 +
  submit 异常保护；_run_execution_phases 报名遍 try 包裹保证重开必跑 + summary["failed"] 聚合
  + exec_done 带 failed；事件契约 docstring 更新。
- `tests/test_activity_exec.py`：+4 个用例（over_ref 记失败、空填跳过提交、报名遍异常仍重开、
  逐活动关 tab），共 12 个全绿。
- `workspace/*.py`：一次性验证/诊断脚本（plan_one/enroll_one/verify_enrolled/verify4/check_accel/
  run_full/diag_tabs/diag_open/reopen_act）——都连 CDP:9222 的已开 Chrome，非产品代码，可参考可删。

### 15.7 续跑结果与最新状态（2026-07-20）
- **`open_enroll_page` 重试已真机验证**：官方大促提报页首轮打开成功，但 SPU `2801689369`
  搜索为 0 行，未填价、未提交，活动页「已报名」仍为 `-`。直营品牌专属活动（第14期）首轮开页
  失败、第二轮重试成功，随后同一 SPU 搜索仍为 0 行，未提交。由此确认 §15.2 第 5 项开页重试
  确实覆盖真实 flake；这两个活动对该 SPU 当前不可提报，不要继续盲重试。
- **流量档位页面新增两个真机坑并已修**（`pipeline.py`）：
  1. 「普通/高级/超级」文字已画进背景图，DOM `innerText` 不含档位名。保留旧文字匹配，同时新增
     结构匹配：收集三张含「让价/对应申报价格」的可见可点击卡，按 x 从左到右取最右侧超级档。
  2. 抽屉先打开、档位卡后异步挂载，固定等待后单次读取仍会偶发空。新增最多 10 秒短轮询。
- **流量开启成功判据改为 toast（权威）**：成功须捕获可见提示中含「成功」且含
  「流量/加速/开启」；失败提示精确为「活动太火爆，请稍后再试」，只有捕获该提示才在同一面板
  再点「立即加速」，最多共 3 次。商品列表行状态会延迟，不能覆盖 toast 结论。
- **SPU `6355823687` 真机开启成功**：Excel 日常价 `51.58`、底价 `45`、库存 `200`，加速价按
  底价+1 设为 **`46.0`**；1 个 SKC 参考价校验通过，第一次点击即捕获
  「流量加速成功，可在“查看详情-近期流量加速效果”中查看明细」。本次只测试加速，**未报名活动**。
  该 SPU dry-run 有 4 个可报 9 折活动，申报价均为 `46.42`，留待后续明确授权再报。
- **当前状态**：`2801689369` 已由用户手动开启加速，不再操作；`6355823687` 以成功 toast 判定
  已开启。两者都不要立刻跑关流量全流程（新开启后有 24 小时关闭冷却）。
- **验证**：`python -m pytest tests/test_activity_exec.py -q` 为 17 passed。新增/调整的一次性脚本：
  `workspace/plan_one.py`、`workspace/check_accel.py`、`workspace/reopen_one_accel.py` 支持 SPU 参数；
  `workspace/check_direct_brand.py`、`workspace/diag_accel_open.py`、`workspace/probe_open_accel.py`
  用于本轮只读/半程诊断。

### 15.8 关闭流量加速的 loading 卡点（2026-07-20，尚待冷却后真机验证）
- 用户截图确认关闭链路：商品数据分析 →「近期流量加速效果」→「停止流量加速」→ 弹窗
  「确定停止流量加速吗？」→ 最终按钮「停止」。点击最终「停止」后，按钮会转圈发送关闭请求。
- 旧代码点击后固定等 1.5 秒就调用 `verify_state`（会 reload），可能在请求仍转圈时刷新页面，导致
  请求现场被打断、关闭结果 toast 丢失，并把仍显示 on 的旧状态误判为失败。
- `pipeline.close_accel` 已改为：确认最终按钮确实点击成功 → 最多约 30 秒轮询弹窗和按钮 loading →
  等 loading 消失、弹窗关闭或结果 toast 出现才视为请求结算。请求未结算时**绝不 reload**，超时保守
  返回 `request_timeout=True`。
- 结算判定优先级：24 小时冷却 toast → 关闭失败 toast → 关闭成功 toast → 请求结算后 reload 回读
  OFF。返回附带 `request_completion`（是否见过 loading、轮询次数、toast 文案），便于追查。
- 新增单测覆盖「先空闲→转圈→转圈结束」和「转圈期间出现 24 小时冷却提示」；当前活动执行测试
  为 **17 passed**。这项只过单测，**尚未真实点击关闭**：`2801689369` 与 `6355823687` 都是刚开启，
  必须等满 24 小时后选一个商品做权威真机验证。

### 15.9 关闭 loading/冷却真机证据（2026-07-21）
- 按用户要求，`2801689369` 在最终确认弹窗点击过一次；平台返回
  「加速器开启后需满24小时才可手动关闭，请耐心等待」，`request_completion.saw_loading=true`、
  `polls=2`，未关闭。此前页面截图中的实际开启时间为 `2026-07-20 17:26:11`，确实未满 24 小时。
- 随后切换测试 `9072868889`：截图显示其开启时间为 `2026-07-20 16:29:31`，最终确认弹窗已截图给用户；
  用户要求先尝试后点击，结果同样捕获 loading + 24 小时冷却 toast，未关闭。
- 结论：关闭请求的 loading 等待与冷却 toast 捕获已经有真实证据；**成功关闭、最终 OFF 以及关闭后重开仍未真机验证**。
  `9072868889` 最早应在 `2026-07-21 16:29:31` 后重试，`2801689369` 最早在 `2026-07-21 17:26:11` 后重试。

### 15.10 关闭成功真机结论（2026-07-21 16:34 后）
- `9072868889` 满 24 小时后再次真点最终「停止」：按钮 loading 正常出现，请求结算后用户在浏览器中
  目视确认弹出**关闭成功提示**。用户明确规则：关闭成功 toast 是权威判据；商品列表 OFF 回显会延迟
  1–2 分钟，不能用即时列表状态覆盖成功 toast。
- 实跑同时暴露两个判定 bug，均已修：
  1. 旧等待在确认弹窗消失时立即结束，可能错过稍后出现的成功 toast。现增加约 3 秒结算宽限期，
     继续监听可见 toast；任一可见 toast 含「成功」即判 `closed=True`。
  2. 旧 `_detect_cooldown_toast` 扫整页正文，会把页面常驻「24小时内调价」公告误判为关闭冷却。
     现只扫描可见 Toast/Message/Alert/Notice，不再使用整页文本兜底。
- 关闭链路至此已真机跑通：最终确认 → loading → 成功 toast。列表 OFF 仅作为延迟诊断快照，非成败判据。

### 15.11 关闭态业务分支、弹窗与页签生命周期（2026-07-21）
- 用户补充业务规则：商品初始就是 `off` 时，不需要先关流量，但仍可“报名活动 → 开流量”。
  `service._run_execution_phases` 已改为：初始 `on` 仅关闭成功后重开；关闭失败/冷却时保持现有 `on`
  不重复开；初始 `off` 在报名遍结束后开启；初始 `unknown` 保守不操作。加速价仍为 Excel 底价+1。
- 商家后台经常出现随机公告遮罩。用户已安装商家助手插件，页面右上角提供“关闭所有弹窗”。新增
  `pipeline.dismiss_all_page_popups`，在每个商品规划和关/报/开阶段入口点击；业务确认框已出现或正在
  等待成功/失败 toast 时绝不调用，避免清掉权威结果。
- 不再要求操作者预先打开流量页/活动页。`service._connect_pages` 固定新建流量页、活动页、商品页，
  不扫描也不复用已有页签，避免继承未知筛选条件、弹窗、局部状态或正在关闭的页面句柄。三个新页签
  都记录为 `owned_pages`，用户原有页签不读取、不修改、不关闭。商品页在批量库存读取后立即关闭；
  流量页/活动页在整个批次结束后
  关闭。每个活动的 `detail-new` 提报页仍由 `_enroll_by_activity` 在提交成功/失败/异常后立即关闭，
  保证多商品串行时不积累残留详情页。
- 诊断脚本旧结果误读已封堵：`check_accel.py`、`plan_one.py`、`run_full.py` 启动时删除同名旧输出，
  新输出写 `generated_at`；`plan_one.py` 缺流量页/活动页时直接报错，不再把缺页误报为 0 个活动。
- `9072868889` 当前权威状态为 `off`；恢复活动页后，`2026-07-21 17:17` dry-run 读到库存 200、
  12 个可报活动，申报价为 `73.20/77.77/82.35`，计划报名后以 `74.00` 开启流量。尚未在本节内
  真提交，由操作者从网页正式执行。
- 验证覆盖初始 off 后开启、unknown 保守不动、插件关弹窗、批次自建专用页签、详情页及时关闭等行为。

### 15.12 网页正式执行未提交、重复首活动与状态提前完成（2026-07-21）
- `9072868889` 从网页勾选“正式执行”后，现场页已勾选 5 个场次并填价 `77.77`，但底部“提交”
  仍未点击；浏览器随后不断打开提报页，用户目视发现仍是同一个“限时秒杀”，而网页表格很早已
  显示全部 `done`。用户要求立即暂停，PID `24132` 已停止，当前已填未提交页保持原状。
- **未提交根因**：`app.py /activity/batch` 只传 `dry_run=False`，漏传 `live=True`；service 因
  `live` 默认 False 实际跑的是半程模式，设计上只填价、不提交、不真关/开。现改为网页请求
  `live=not dry_run`，正式执行确认后才进入真实模式。
- **重复首活动根因**：`_MARK_ENROLL_JS` 给报名按钮写 `data-kiro-enroll=1` 后从不清旧标记，下一活动
  虽也标记成功，但 `document.querySelector` 始终点击 DOM 中第一个旧标记，所以反复进入限时秒杀。
  现每次定位前先清所有旧标记；新提报页不再按相同 URL 做 diff，改按 Page 对象身份识别。
- **活动错位硬防线**：截图证明 detail-new 左上角实际显示活动名。新增
  `verify_enroll_page_activity`，填价前必须看到与目标完全一致的页头；不一致则立即关页、记录错误、
  绝不搜索 SPU/填价/提交。
- **网页状态修复**：正式模式的 `product_done` 仅显示“计划就绪”，网页新增订阅
  `exec_start/close/fill/enroll/reopen/product_done/done`，逐步显示真实动作；最终 SPU 状态由已点击提交
  活动数和流量步骤合成，不能再用规划 `done` 冒充执行完成。`batch_done` 的 done/fail 在正式模式也
  改取执行结果。
- **提交按钮防重复**：首次点底部“提交”后，二次确认只在可见 dialog/modal 内寻找；不再全页匹配
  “提交”导致可能重复点击原按钮。返回文案明确为“已点击提交（并确认/无二次确认弹窗）”。
- 验证：活动执行测试 **25 passed**；`app.py`/service/pipeline Python 编译通过；activity.html 内联
  JavaScript 经 Playwright 自带 Node `new Function` 语法检查通过。尚未恢复 service，也未再次真跑。

### 15.13 报名 RPA 路线改为逐步强约束（2026-07-21）
- 用户进一步澄清：当前核心不是结果状态展示，而是进入第一个活动后没有按既定 RPA 路线完成操作：
  **打开活动详情页 → 输入 SPU ID → 查询 → 勾选列表 → 填写场次 → 填写活动申报价 → 点提交**。
- 根因是旧 `enroll_activity` 任一步失败只返回 `filled=False`，外层 `_enroll_by_activity` 记录失败后仍
  继续下一个活动；因此首活动未完成也会继续开页，表现为跳步骤/重复活动。
- `pipeline.enroll_activity` 现为逐步状态机：新增 `queried`、`failed_step` 和 `on_step`；输入 SPU、
  查询、勾选、设置场次、填价每步都现场校验并发 `exec_rpa_step`。查询不再固定睡 3 秒后急读，改为
  最多约 10 秒轮询目标 SPU 行且必须唯一。
- `_enroll_by_activity` 改为 fail-fast：当前活动任一步失败时不点提交、关闭当前详情页、记录
  `summary.halted` 并停止整个后续活动遍；提交失败同样停止。网页逐条显示七步 RPA 的完成/失败。
- 流量开启联动收紧：初始 `off` 的 SPU 只有全部计划活动均完成提交后才允许开流量；初始 `on` 且
  已被本管线关闭的 SPU，即使报名失败仍恢复开启，避免遗留关闭状态。
- 只读 DOM 检查确认活动表格第 5 列为“已报名”，当时限时秒杀仍为 `-`，印证第一活动并未报名；
  但本节修复重点是 RPA 动作路线，不以该状态检查替代逐步操作。
- 验证：活动执行测试 **27 passed**，新增“首活动 RPA 失败后不打开第二活动、不提交、初始 off
  不开流量”回归覆盖；Python 编译和 activity.html JavaScript 语法检查通过。service 保持停止。

### 15.14 列表初筛通过但详情无可报名商品（2026-07-21）
- 用户指出「【爆款冲刺】周末48H爆款冲刺（07/25-07/26）」在活动列表按折扣/库存计算可通过，
  但进入详情搜索该 SPU 后没有可报名列表行。结论：列表页只暴露折扣与库存门槛，详情页还有平台
  未公开的类目/站点/定向/历史报名等资格条件；列表计算只能称为“初筛通过”。
- `pipeline.enroll_activity` 查询目标行最多等待约 10 秒：唯一行=详情资格通过并继续勾选；0 行=
  `detail_eligible=False`，表示 RPA 查询动作成功但业务资格不符；多行/查询按钮失败/页面异常仍属于
  RPA 故障并 fail-fast。
- service 新增 `ineligible_activities` 与 `exec_activity_skip`。详情 0 行时不填价、不提交当前活动，
  但继续检查下一活动；真实 RPA 故障仍停止整个后续报名遍。网页把“入选”改为“初筛通过，详情待
  验证”，详情不符使用黄色跳过日志，不再显示为红色自动化失败。
- 初始 `off` 的开流量条件收紧：全部初筛活动都已解析为“完成提交或详情不符”，且至少一个活动
  真实完成提交，才允许开启流量；若全部详情不符，最终状态为 skip 且不开流量。部分详情不符、
  其余提交成功则可正常完成。
- 验证：活动执行测试 **29 passed**，新增“详情不符跳过后继续下一活动”和“部分/全部详情不符的
  最终状态与开流量门控”覆盖；Python 与前端 JavaScript 语法检查通过。尚未恢复 service 真跑。

### 15.15 手动测试共存、二次确认与权威报名核验（2026-07-22）
- 用户会在调试期间手动打开某个活动、填写并截图演示，自动任务不能把这些页签当成自己的残留页。
  `open_enroll_page` 不再关闭启动前已经存在的 `detail-new`；只用 Page 对象 diff 认本次点击新建页，
  重试清理也只关闭本次尝试创建的页。service 仍只关闭函数返回的任务页，不碰手动测试页。
- 7 月 22 日现场失败的直接原因已确认：限时秒杀打开后，目标“官方大促”连续三次实际仍打开限时
  秒杀，严格 fail-fast 因此没有执行后面的“回归日常&周年庆大促85折专场”。规则弹窗点击现改为
  只在【可见且正文包含目标活动完整名称】的 dialog/modal 内找“同意活动规则”；发现其他活动残留
  弹窗时不点击，避免用上一个活动的旧弹窗再次开错页。
- `submit_enroll_page` 不再固定等待 2 秒后单次检查。点击底部提交后最多约 10 秒短轮询：延迟出现的
  二次确认只在可见弹窗内部点击且最多一次；捕获“活动太火爆/稍后再试/提交失败/报名失败”等提示
  立即返回失败；确认弹窗持续出现但确认按钮无法识别时保守失败并停止后续活动。
- `submitted=True` 仍只代表页面接受了提交动作，不代表报名成功。活动列表解析新增
  `registered_count/registered_display`，规划时保存提交前“已报名”基线；正式提交后刷新活动列表，
  只有目标活动“已报名”数字至少增加本次已填 SPU 数量才写入 `enrolled_activities`。成功提示存在但
  数字没增加也会停在当前活动，初始 off 商品不会提前开启流量。
- 网页提交日志区分“未提交/失败”“已点击提交，待活动页核验”“活动页已报名增量已确认”，不再因
  单纯点过提交显示绿色完成。当前页面只读确认新活动已出现：8折专场已报名数 1，85折专场为 0；
  对 `9072868889` 的85折价/库存初筛可通过，但须等修复后的单活动真实执行验证详情资格与权威增量。
- 验证：`python -m pytest tests/test_activity_exec.py -q` 为 **37 passed**；pipeline/service/tests Python
  编译通过；`templates/activity.html` 内联 JavaScript 经 Playwright 自带 Node `new Function` 检查
  通过。本节没有点击任何真实报名或提交，规则弹窗绑定与二次确认仍待下一次受控单活动真机验证。

### 15.16 “活动详情”全屏层不带 dialog/modal 标识（2026-07-22 15:18）
- 用户实跑限时秒杀时，页面已明确弹出“活动详情”，底部蓝色按钮为“同意活动规则，立即报名”，
  但日志连续两轮记录“未见同意活动规则按钮”。截图确认按钮不是没出现，而是该全屏弹层根节点没有
  命中 `[role=dialog]/[aria-modal]/MDL_outerWrapper/Modal` 等旧容器选择器。
- `_CLICK_ACTIVITY_RULE_JS` 改为先枚举可见的“同意活动规则”按钮，再从按钮向上最多 16 层查找
  宽高足够、同时包含“活动详情”和目标活动完整名称的共同祖先；截图中的普通全屏 `DIV` 因而可命中。
  搜索在 `document.body` 前停止，避免 body 同时含活动列表目标名造成假匹配；按钮所在层未包含目标活动
  名时返回 mismatch，仍不点击，防止上一个活动残留弹层错开活动。
- 验证：活动执行测试 **38 passed**，Python 编译通过。当前正在运行的 service 若未启用热重载，需要
  重启后才会加载本修复；下一次先对限时秒杀单活动确认日志出现“已打开提报页”，再观察完整 RPA。

### 15.17 全活动扫描 + 报名记录页最终对账（2026-07-22 15:35 后）
- 本轮 `9072868889` 在“回归日常&周年庆大促85折专场”已完成勾选、场次和 `77.77` 填价；点击提交
  后详情页发生导航，旧代码继续 `page.evaluate` 读取弹窗，收到 `Execution context was destroyed`
  就误判提交失败并 fail-fast。报名记录页随后给出权威证据：提交时间 `2026-07-22 15:35:26`、
  报名结果“报名成功待开始”、接口 `enrollStatus=4`、`enrollId=3000020543885092`。实际已报成功。
- `submit_enroll_page` 现把提交后的 navigation/context destroyed 归为“已点击提交，待报名记录页核验”，
  不再当失败。其他提交后读取异常同样保留为 pending；明确的失败提示仍记录，但也不阻断后续活动。
- 编排从旧 fail-fast 改为逐活动扫描：打开页、查询、填价或提交任一步失败时记录 `scan_failures` 和原因，
  关闭本任务详情页后继续下一活动。详情按 SPU 查询 0 行仍归为 `detail_ineligible`，说明列表价/库存初筛
  通过但详情资格不符（或已报导致不再出现）；不填价、不提交，记录原因后继续。
- 所有活动扫描完后自动新开 `/activity/marketing-activity/log`，切“全球”，按每个 SPU 查询真实
  `/api/kiana/gamblers/marketing/enroll/list` 响应；`enrollStatus=4` 且无场次失败原因才写入最终
  `enrolled_activities`。记录页有成功项时可覆盖扫描阶段的假失败或“详情 0 行（其实已报）”；记录页
  无成功项才最终失败。初始 off 商品也只有全部活动均被“成功记录/详情不符”解析后才开启流量。
- 固定活动接口不填 `activityThematicName`，已回退使用 `activityTypeName`。真机只读核验
  `9072868889` 共返回 4/4 条且均为 `enrollStatus=4`：85折专场、8折专场、限时秒杀、官方大促。
- 网页新增 `[记录核验]` 日志；填价阶段文案改为“等待当前活动统一点击提交”，不再误导成
  `allow_submit=False` 的“半程未提交”。验证：活动执行测试 **40 passed**，Python 编译与 activity.html
  JavaScript 语法检查通过。运行中的 service 需重启才会加载本节代码。

### 15.18 提交后的 detail-new-result 即时结果页（2026-07-22 16:38）
- “半托管活动85折专区”完整走完搜索、勾选、2 个场次、`77.77` 填价和提交后，平台不是弹普通
  dialog/toast，而是把当前详情页导航到 `detail-new-result`。截图中的 URL 参数明确含
  `successCount=1`，页面正文同时显示绿色勾和“已提交1个商品，可在报名记录中查看报名结果”。
- 旧逻辑在导航瞬间捕获 `Execution context was destroyed` 后直接返回 pending，没有等新页面稳定，
  所以用户看到成功页，网页日志却只有“页面发生跳转，待核验”。
- 新增 `_wait_submit_result_page`：提交反馈轮询前先检查 URL；遇到 context destroyed/navigation 时
  再等待结果页，交叉解析 query 的 `successCount` 和正文“已提交 N 个商品”。`N>0` 返回即时
  `submitted=True/verified=True/result_page=True`；`N=0` 返回未成功。结果页成功仍只是即时提交反馈，
  所有活动扫完后继续由 §15.17 的报名记录接口做最终权威对账。
- 网页提交日志现在区分“已点击提交，待报名记录页核验”和“结果页确认已提交，待报名记录页最终
  核验”；最终成功仍由独立 `[记录核验]` 绿色日志给出。验证：活动执行测试 **41 passed**，Python
  编译和 activity.html JavaScript 语法检查通过。需重启 service 后再观察新文案。

### 15.19 报名计划表按活动逐行同步状态（2026-07-22）
- 用户指出计划表右侧“状态”原来按 SPU 使用 `rowspan` 合并，14 个活动只能共同显示一个“执行中”，
  无法看出当前报到哪一项、哪些详情不符、哪些已提交待核验。
- 前端新增 `activityStates[spu][activity]`，SPU 列仍可合并，但状态列每个活动独立渲染。规划完成后
  每行先显示“待执行/排队中”；`exec_rpa_step` 依次更新“详情页已打开、SPU已输入、查询完成、商品
  已勾选、场次已填写、价格已填写、提交动作完成”。
- `exec_activity_skip/exec_fill/exec_enroll` 分别更新“详情不符、填价失败/价格已填写、已提交·待记录/
  结果页成功·待记录”。扫描结束后的 `exec_log_verify` 是最终覆盖：成功记录→绿色“报名成功”，详情
  0 行且无既有成功记录→黄色“详情不符”，记录页未确认→红色“记录未确认”。徽标 title 保留完整原因。
- SPU 级 `exec_product_done` 仍负责顶部统计和整单 done/fail，不再覆盖每个活动行的细粒度状态。
  新增静态回归防止状态列重新变成 rowspan。验证：活动执行测试 **42 passed**，activity.html 内联
  JavaScript 语法检查通过。正在运行的网页/service 需重启并刷新页面后加载新表格逻辑。

### 15.20 报名前 /log 基线与按 total 完整翻页（2026-07-22）
- 用户确认这里存在两个不同的 `total`：规划阶段显示的“初筛活动 14 个”是活动候选数；报名记录页
  按 SPU 查询后接口返回的 `total` 才是该 SPU 已有报名记录总数。旧代码只有前者，并未在报名前保存
  `/log total` 基线，这是缺口，现已补齐。
- 正式执行在任何提报动作前调用 `read_activity_log_records`，按各 SPU 查询并保存
  `summary.log_baseline`（含 `total`、已有活动记录和查询完整性）；扫描全部活动后再查一次。最终核验会
  合并报名前已有记录与报名后记录，所以活动报名前已经存在记录时也按成功处理，不要求一定是本轮新增。
- 删除“尝试把每页 10 条切成 100 条”的不稳定旁路。现在以首个 `/marketing/enroll/list` 响应的
  `total` 和 `pageSize`（接口未给时取首屏实际返回数）计算 `ceil(total/pageSize)`，逐次点击记录页的
  下一页并监听下一次接口响应，收集到 `returned >= total` 才标记完整。典型 `total=14` 会读取
  第 1 页 10 条 + 第 2 页 4 条，后四项不再因默认分页被误报为“报名记录查询不完整”。
- 下一页请求超时、按钮缺失或提前禁用时保留已拿到记录，但整体保持 `complete=False`；已查到的匹配
  活动仍可确认，未覆盖活动只标“核验不完整”，不能冒充成功，也不能把平台状态写成明确报名失败。
- 真页只读复查时 `9072868889` 当前查询为 `total=9/returned=9`；尝试用逗号合并两个 SPU 仍返回
  9 条，页面没有渲染下一页，因此本次没有伪称完成真机分页点击验证。分页收集器以 `10+4`、下一页
  异常两种用例覆盖；报名前基线时序也有回归测试。验证：活动执行测试 **45 passed**，pipeline、
  service 和测试文件 Python 编译通过。运行中的 service 必须重启，网页刷新后才会加载本节修复。

### 15.21 /log 查询的前台可观察性（2026-07-22）
- 用户指出运行时没有看到报名记录页搜索框输入 SPU。检查确认查询动作原本确实调用了 `fill(spu)`，
  但 `/log` 是 `context.new_page()` 创建的临时页，代码没有显式切到前台，且填入后立即点击查询；
  因而操作者停留在活动页时看不到输入过程，也没有独立日志能证明输入值是什么。
- `read_activity_log_records` 现在进入 `/log` 后显式 `bring_to_front()`；每个 SPU 调用 `fill` 后立即
  用 `input_value()` 反读校验，实际值不等于目标 SPU 时不点击查询并记为不完整。校验成功会打印
  “已在报名记录页输入 SPU=...，准备点击查询”，短暂停顿 0.8 秒后才点击，并在完成后打印
  `total/已读取/页数`。这样报名前基线和报名后核验两次查询都可在浏览器与日志中直接观察。

### 15.22 执行时临场确认流量状态（2026-07-22）
- 用户从正式执行页指出：页面宣称是一条“关流量 → 报名 → 开流量”管线，但 `9072868889` 执行时
  没看到关闭确认。根因是阶段一只遍历规划时缓存为 `accel_will_close=True` 的 SPU；规划时为 `off`
  会完全跳过这一阶段，既不临场复查，也不发送“本就关闭”的事件。多商品串行或人工操作后，这个
  缓存还可能过期，正是此前“使用旧数据”问题在流量阶段的残留。
- 阶段一现在逐个遍历所有正式计划 SPU，先把流量页切到前台，再调用 `read_accel_state` 临场复查。
  当前 `off` 时明确发送绿色 `[关流量确认] 当前已关闭，无需操作`；当前 `on` 才调用 `close_accel`
  进入停止、确认、等待请求转圈及成功提示流程。临场值会覆盖规划值，规划 `off`、执行 `on` 也会
  正确关闭并在管线末尾恢复。临场 `unknown` 不再沿用旧 `off`，改为保守不做流量开关并如实报错。
- `/log` 报名前基线从执行阶段最前面移到关闭检查之后，仍严格发生在任何活动提报之前。正式顺序现为：
  **临场确认/关闭流量 → `/log` 报名前基线 → 逐活动报名 → `/log` 最终核验 → 开启/恢复流量**。
- 新增回归覆盖“初始 off 也必须发 no-op 确认事件”“规划 off、执行时 on 必须关闭并恢复”和上述阶段
  顺序。验证：活动执行测试 **46 passed**，pipeline/service/tests Python 编译通过。service 与网页
  均需重启、刷新后才会加载本节逻辑。

### 15.23 流量页按 SPU 精确搜索与商品信息列判态（2026-07-22）
- 用户指出流量页顶部支持“商品ID查询=SPU”，并明确唯一页面判据：目标商品的**商品信息列**含绿色
  `流量加速中` 即为开启，不含即为关闭。旧 `read_accel_state` 从未使用搜索框，只扫描当前列表 DOM；
  商品不在当前页时会返回 unknown，且旧 `_LOCATE_ROW_JS` 向上找含操作入口的父容器，存在把相邻行
  `流量加速中` 串入当前商品的风险。
- 新增 `_search_flux_product`：定位 placeholder 含“多个查询/空格/逗号”的 SPU 输入框，`fill` 后用
  `input_value` 反读，点击顶部精确“查询”，必须捕获 `/api/flow/analysis/list` 响应；只有接口
  `total=1`、`pageItems` 中恰有一个 `productId==目标 SPU`、DOM 也出现该 SPU 行时才允许判态。
  规划读取、执行前复查、关闭入口和开启入口现均先走此搜索，不再依赖商品是否碰巧在当前分页。
- 状态 DOM 已收窄为 `SPU` 锚点的 `closest('td')` 商品信息单元格，不再读取跨列/跨行父容器；主判据
  严格为单元格是否含 `流量加速中`。接口 `flowGrowStatus` 同时记录到日志用于交叉诊断，但不替代用户
  指定的页面判据。搜索异常统一返回 unknown，保守不做未经确认的流量开关。
- 真页只读验证：输入 `9072868889` 与 `2801689369` 后，两次接口均 `total=1` 且 productId 精确匹配，
  证明搜索能力生效。17:43 查询时两项接口均为 `flowGrowStatus=1`，对应临时查询页商品信息格均含
  `流量加速中`；这说明 907 在此前关闭后已被后续流程重新开启，与用户提供的关闭时截图不是同一时点。
  用户提供的权威对照仍作为分支规则：907 关闭截图无标签→off，280 开启截图有标签→on。
- 新增搜索输入、接口等待、精确 productId 和商品信息列 on/off 的回归测试；活动执行测试当前
  **48 passed**。正式代码加载需要重启 service，网页需要刷新。

### 15.24 报名后开启流量前再次按 SPU 确认（2026-07-22）
- 用户确认阶段三也不能直接沿用阶段一状态。`_open_accel_once` 的第一步现为
  `read_accel_state(page, spu, search=True)`，完整复用 §15.23 的搜索框输入、接口 total/productId
  核对与商品信息列判态。开启前为 `on` 时直接 `opened=True` no-op，不再点“立即开启”；只有明确
  为 `off` 才进入选超级档、填加速价和最终“立即加速”；unknown 保守不操作。
- `exec_reopen` 新增 `precheck_state/already_on`，网页日志明确显示“开启前按SPU确认=已开启/已关闭/
  未知”。成功 toast 仍是提交开启动作后的权威成功判据；页面有 1–2 分钟状态延迟，因此不以立即刷新
  后的标签替代成功提示。
- 新增“开启前 search=True 且已 on 时 no-op”的回归测试；活动执行测试更新为 **49 passed**。

### 15.25 关闭/开启流量的按 SPU 可视化面板（2026-07-22）
- 用户指出滚动日志很长，操作者无法直观看到每个商品是否完成关/开流量。活动页在事件日志与报名计划
  之间新增“流量加速流程（按 SPU 实时确认）”表格，每个 SPU 固定展示：**关闭前按 SPU 确认、
  关闭动作、开启前按 SPU 确认、开启动作**。多商品串行时每行独立维护，不会被当前活动日志淹没。
- service 新增 `exec_accel_step` 中间事件。阶段一开始搜索时面板显示“正在查询”，确认 on 并进入关闭
  请求后立即显示“正在关闭”，覆盖平台确认按钮转圈的等待期；阶段三调用 `open_accel` 前显示“正在
  查询”。最终 `exec_close/exec_reopen` 再覆盖成明确结果。
- 颜色与分支区分：已开启/关闭成功/开启成功为绿，已关闭状态为灰，无需关闭/无需重复开启为蓝，
  冷却未关闭为黄，unknown/关闭失败/开启失败为红。全部详情不符、没有成功活动而不新增开启时显示
  “未执行”；冷却导致始终保持 on 时显示“无需恢复”。badge 的 title 保留完整平台原因。
- 开始新任务时 `accelStates` 与面板同步清空；SSE 注册新增 `exec_accel_step`。新增静态 UI 回归与
  close/open 中间事件回归，活动执行测试更新为 **50 passed**。service 及页面都需重启、刷新后生效。

### 15.26 报名基线搜索框重绘兼容（2026-07-22）
- 用户反馈日志出现 `[报名基线] /log 记录 total：2801689369=?（报名记录查询不完整）`，且肉眼没看到
  输入 SPU。复盘确认这表示查询前没有得到 `total`，不是报名记录总数为 0；旧 marker 只认 readonly
  selector 的 `value == "SPU ID"`，切换“全球”后的短暂重绘可能使它在那一轮找不到，随后直接跳过输入。
- `_MARK_ACTIVITY_LOG_SPU_JS` 现按可见、非 readonly、placeholder 含“多个/空格/逗号”的输入框定位，
  selector 仅作横向位置辅助，不再是唯一条件；切换全球后每个 SPU 最多轮询 12 次等待重绘完成。查询
  按钮改取可见最后一个，避免重复 DOM 节点导致 click 失败；定位失败会日志明确写“重绘后仍未找到可编辑
  SPU 查询框”，不再伪造 `total=?` 成功。
- 正式函数真页只读复测：18:17:25 已输入 `SPU=2801689369`，18:17:27 返回 `total=1`、读取 1 条、
  `complete=true`，记录为“官方大促/enrollStatus=4”。活动执行测试更新为 **51 passed**，Python 编译
  通过。运行中的 service 必须重启后才会加载此修复。

### 15.27 全页面站点通知面板自动关闭（2026-07-22）
- 用户反馈右上角“全部消息”通知面板会在任意业务页反复弹出，遮挡流量、活动详情和报名记录操作。
  原 `dismiss_all_page_popups` 只处理商家助手注入的“关闭所有弹窗”，没有处理站点通知面板。
- 新增 `dismiss_site_notification_panel`：先锁定可见“全部消息”标题，再只在该面板内部寻找关闭图标/关闭
  aria-label/右上角小按钮并点击；找不到面板或关闭按钮时返回 False，不会全页盲点 X，也不会触碰业务确认
  弹窗和成功 toast。`dismiss_all_page_popups` 现在串联站点通知面板 + 商家助手弹窗。
- 批次自建页、每个 SPU 业务入口、活动详情页和报名记录临时页都调用统一清理入口。这样进入任意
  流量/活动/商品/`/log` 页面都会先确认该通知面板已关闭。
- 新增通知面板作用域静态回归；活动执行测试更新为 **52 passed**。service 需重启、网页刷新后生效。
- 同时修正 reload 后流量状态校验仍传整行文本的问题，统一改传商品信息单元格，避免通知清理/刷新后
  又发生相邻商品状态串行误判。活动执行测试当前为 **53 passed**。

### 15.28 修复“全部消息 63”标题匹配（2026-07-22）
- 用户反馈关闭没有生效。根因已确认：截图中的标题是 `全部消息 63`（带未读数），旧逻辑要求标题
  完全等于 `全部消息`，因此根本没有找到面板；关闭图标还可能是 `role=img`，不在旧 controls 选择器内。
- 标题现在按 `startsWith('全部消息')` 匹配，优先点击真实 DOM 的
  `data-testid="beast-core-icon-close"`，关闭候选再覆盖 `button/a/[role=button]/[role=img]/svg`；
  页面入口连续尝试 5 次（每次间隔 0.5 秒）应对通知面板异步出现。仍只在面板内部点击，不会全页乱关。
- 真机复测发现该 testid 元素是 SVG，不能直接调用 `.click()`；现统一使用 HTML click 或冒泡
  `MouseEvent('click')` 触发，避免 SVG `TypeError`。
- 新增标题带未读数和图标节点回归断言；service 需重启、网页刷新后生效。

### 15.29 流量未开启原因前端可见（2026-07-22）
- 用户指出初始 off 且所有活动详情不符合时，开启动作显示“未执行”，但原因只在 badge `title` 悬浮提示
  中，默认不可见。开启动作现在对 `skipped/failed/warning` 状态在 badge 下方直接显示原因文本，例如
  **无成功报名活动，不新增开启流量**；悬浮 title 仍保留完整原因。
- 新增 `accel-flow-note` UI 回归断言。service 需重启、网页刷新后生效。

### 15.30 报名成功判据放宽：列表出现且非「已退出」即成功（2026-07-24）
- 2026-07-23 23:32~23:41 live 批次（SPU 2801689369，初始流量 off）报了 17 个活动：
  4 个 `enrollStatus=4` 核验成功、9 个详情不符、4 个被判「报名记录存在但未成功」
  （`enrollStatus=1`×2：夏季8折/破冰85折；`3`×2：半托管85折/新品8折）。初始 off 商品的开流量
  闸门要求全部计划活动被「成功/详情不符」解析，4 个悬置 → 阶段三根本没发起开流量（不是开了
  失败），最终 fail=1，note「流量最终状态未完成（初始=off）」。
- 用户后台核对：那 4 条都是正常报名记录（夏季8折接口=1、页面显示「进行中」，场次
  2026-07-05~07-31 已开始）。**平台规则：报名记录只要在列表出现、且不是「已退出」即代表
  成功报名**。成功值标定：`4`=报名成功待开始（07-22 标定）、`1`/`3`=进行中（07-24 用户核对）；
  已开始的场次报成功后不会是 4，旧 `==4` 判据把「进行中」误伤成未成功。
- `_parse_activity_log_item` 成功判据由「==4 且无场次失败原因」放宽为「非已退出即成功」；
  场次失败原因不再否决，改为成功 note 附注。**已退出=enrollStatus 6**（2026-07-24 用户后台
  核对确认），数值命中即判退出，记录文本含「已退出」作兜底；未标定的状态值按成功计并打
  校准日志供回填 `_EXITED_ENROLL_STATUSES`。service 未成功原因文案改为「报名记录存在但已退出」。
  完整标定：1/3=进行中、4=报名成功待开始、6=已退出。
- 该 SPU 流量仍保持 off（管线全程未动它），重跑批次即可验证闭环：4 个活动对账转正 →
  `all_resolved=true` → 阶段三自动补开流量。
- 附带观察：规则弹窗内容「加载中...」时做活动名绑定必然 mismatch，17 个活动里 15 个首次
  打开因此失败、等约 8s 重试才成功（每个活动白等 ~10s），可改为绑定前先等弹窗正文加载完。

### 15.31 修复规则弹窗「加载中」竞态：不再整轮重试（2026-07-24）
- 根因：`_CLICK_ACTIVITY_RULE_JS` 在弹窗正文异步加载完成前就做活动名绑定。正文「加载中...」
  时标题区还是占位符（活动名未渲染），`text.includes(target)` 必然为假 → 旧代码归为 mismatch →
  `open_enroll_page` 整轮重试（重新标记报名按钮、重新点、再等），每个活动白等约 8s。15/17 个
  活动首次打开都栽在这里。
- 修复：JS 在命中「活动详情」大层时探测正文是否含「加载中」，含则返回新状态 `loading`（而非
  mismatch）。`open_enroll_page` 的等弹窗轮询把 `loading` 当「还没加载完，继续轮询」——不 break、
  不重试；只有正文加载完仍不含目标活动名才判 mismatch 整轮重试。轮询上限从 16 提到 20（10s），
  持续 loading 超时打专门日志区分于真 mismatch。绑定安全性不变（仍要求弹层同时含「活动详情」
  和完整活动名）。
- 验证：新增 `test_open_enroll_page_waits_out_rule_popup_loading`（连续 2 次 loading 后 clicked，
  断言只标记报名按钮 1 次=没整轮重试）；`test_activity_rule_button_supports_non_dialog_fullscreen_layer`
  补断言 JS 含 loading 分支。service 需重启后生效。

### 15.32 开流量判成败改用接口状态兜底：toast 不权威（2026-07-24）
- 承 §15.30 判据放宽后首次真跑到阶段三：SPU 2801689369 报名 9 提交+8 详情不符=17 全 resolved，
  闸门放行、开流量动作真的执行了（点「立即加速」1 次、填价 41），但日志「未捕获成功提示
  （等待流量加速结果提示超时）」→ 判 opened=False → 上层报「流量加速失败」、fail。
- 根因：`_open_accel_once` 完整路径判成功【只信瞬时 toast】（`_wait_accel_submit_feedback` 抓
  「成功」提示，8s 没抓到返回 unknown）。但点「立即加速」后页面刷新/toast 一闪而过时抓不到，
  抓不到≠没开成。对比旧路径（无 accel_price）点完是用 `verify_state` 回读行状态校验的，完整
  路径却没有任何状态兜底——设计遗漏。而权威依据一直在手边：`_search_flux_product` 已解析平台
  列表接口，`read_accel_state(search=True)` 走 /list 接口 + 行「流量加速中」文本即持久权威状态。
- 修复（单次内两级判定）：`_open_accel_once` 里 feedback.status==unknown 时不直接判失败，回读
  一次 `read_accel_state(search=True)`，状态=on 即判 opened=True（note 注明「未捕获成功提示，但
  回查流量页确认状态=加速中」）；回读仍非 on 才如实失败。busy（火爆重试）与明确 success 的既有
  判定完全不动。与本项目「submitted 不可信、必须回查对账」铁律同构：toast 不权威、接口状态才权威。
- 外层整轮重试（用户要求 2026-07-24）：`open_accel` 再包一层「开启→回查确认」重试，最多 3 次。
  一轮结束仍未确认开启成功（opened=False）则等状态回显后从 precheck 重走整套开启流程，直到成功
  或用尽 3 次。安全前提：每轮 `_open_accel_once` 开头都按 SPU 回查状态，读到 on 即 no-op 成功——
  「上一轮其实已开成、只是没抓到 toast」时下一轮 precheck 读到 on 直接成功返回，绝不重复点「立即
  开启」（开流量不可逆锁 24h），故重试对已开成的品幂等。半程 allow=False 不真开、opened 恒 False，
  整轮重试无意义，只跑一次。两级重试互不干扰：busy 是按钮级重点（`_click_open_with_busy_retries`），
  本外层是整轮重走。
- 验证：新增 `test_open_accel_verifies_by_state_when_toast_unknown`（unknown+回读 on→opened）、
  `test_open_accel_stays_failed_when_toast_unknown_and_state_off`（unknown+回读 off→失败）、
  外层重试 4 个测试（三轮后成功/已 on 幂等 no-op/用尽 3 次报失败/半程只跑一次），60 passed。
  service 需重启后生效；重启后重跑该 SPU 应从 fail 转 done。

### 15.33 超参考价失败反推「建议核对日常价」（2026-07-24）
- 现象：SPU 6322830186 报多个活动，日志「申报价 7.0 高于提报页参考价 6.65（疑 Excel 日常价与
  前端实际售价不一致）——已跳过未报名」。根因不是代码 bug：Excel 日常价 8.24 系统性偏高约 5.4%，
  平台参考价按前端实际售价 × 折扣率给出（6.65 = 7.82 × 0.85），Excel 记的 8.24 偏高 → 按 Excel
  算的申报价 7.0 超过参考价，平台必拒。
- 用户要求：采集的 Excel 数据本身可能过期/错误，遇到超参考价要给出【建议人工核对的日常价】。
- 实现：新增纯函数 `build_over_ref_note(submit_price, ref_price, daily_price, discount_rate)`。
  正算 `submit_price = 日常价 × 折扣率`，平台参考价同样是「前端实际售价 × 折扣率」，故
  `参考价 / 折扣率 = 平台认可的前端售价基准 ≈ 应填日常价`。反推 `suggested = round(ref/dr, 2)`
  （dr 缺省/≤0 时不反推、退回原「请核对 Excel 日常价」文案，绝不抛错或给错误建议）。note 同时
  给出「平台认可日常价约 X，当前 Excel 日常价 Y」并排，供操作者核对。
- `enroll_activity` 签名加 `daily_price=None, discount_rate=None`（仅超参考价时反推用，不参与任何
  主流程判定，申报价仍是既算好的 submit_price）；service 在 enrolled/by_activity 两处透传 daily/dr。
- 验证：`test_build_over_ref_note_backsolves_suggested_daily_price`（6.65/0.85→7.82）、
  `test_build_over_ref_note_without_discount_falls_back`（dr 缺省/0→不反推），全套 62 passed。
