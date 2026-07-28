# 订单登记管线 · 落地文档

> 本文档由探索 session 产出（2026-07-27 实测），供实施直接开工。自包含：读完即可动手。

## 0. 一句话目标

定期把 Temu 卖家后台「待发货」订单，采集写入本地订单登记表
（路径在 `config.toml` 的 `[orders] workbook` 配置，示例见 `config.example.toml`）的对应
Sheet，含产品主图（WPS DISPIMG 嵌入图）。手动触发为主，UI 按钮 + CLI 双入口。

## 1. 已确认的决策（本 session 问答结论，不要再推翻）

| 维度 | 结论 |
|---|---|
| 订单来源 | 当前 CDP 里已登录的店铺（用户手动切店，管线不负责切换）。探索时为 **StoreA** |
| 写入目标 | **按店铺/站点写入对应现有表**，不新建 Sheet |
| 采集周期 | **手动触发为主**，提供 UI 按钮 + CLI，不做定时任务 |
| 抓取方式 | **用页面自带「导出订单」按钮拿官方 xlsx**，不做 DOM 逐字段提取、不再攻接口抓包 |
| 产品图片 | **要写 DISPIMG 嵌入主图** |

**为什么放弃抓接口**：订单列表请求走 Web Worker（`blob:https://agentseller.temu.com/...`）
+ 商家助手扩展的 `xhr-interceptor.js`，页面级 Playwright 和页面级 CDP `Network.*` 都拿不到
响应体（实测 98 条请求 / 71 种 URL 全量枚举，订单接口确实不在其中）。自造 fetch 直连又过不了
anti-content 动态签名。官方导出是唯一稳且省事的路。

## 2. 探到的页面事实（2026-07-27 实测，agentseller.temu.com）

订单页 URL（待发货）：
```
https://agentseller.temu.com/mmsos/orders.html?fulfillmentMode=0&queryType=2&sortType=1
&timeZone=UTC%2B8&needBuySignService=0&sellerNoteLabelList=&packageAbnormalTypeList=
```

**导出闭环（已跑通）**
1. 「导出订单」按钮**未勾选订单时是 disabled**（`BTN_disabled_123`），必须先勾选。
2. 勾表格 `thead input[type="checkbox"]` = 全选**当前页**（20 条），不是全部。
3. 「跨页勾选」开关默认是开的（`SIH_active_123`）——**实测翻页后已勾选状态保留并累积**
   （第1页勾20 → 翻页仍显示20 → 第2页再勾 → 累计40）。所以逐页勾完再一次性导出可行。
4. 点「导出订单」弹出「导出字段设置」弹窗，7 个字段分组复选框 + 1 个「过滤已取消的商品」，
   默认全勾；第 0 个「订单号」是 disabled（强制导出）。序号固定：
   `0订单号(禁用) 1订单信息 2子订单信息 3商品信息 4收货信息 5运单信息 6操作节点 7过滤已取消的商品`
5. **取消勾选序号 4「收货信息」**——登记表用不到，且是买家 PII（姓名/电话/邮箱/身份证号/税号/地址），
   不该落到本地表。设置会被后台记住，下次打开已是未勾状态。
6. 点「确认导出」→ **同步直接下载**，不走异步任务中心。文件名形如 `订单导出2026727002.xlsx`。

**分页 DOM**（`ul[data-testid="beast-core-pagination"]`，`data-status="beast-core-pagination-{pageSize}-{page}"`）
- 总数：`li.PGT_totalText_123` → 「共有 247 条」
- 每页条数：`li.PGT_sizeChanger_123` 内 `div[data-testid="beast-core-select"]`，默认 20
- 页码：`li.PGT_pagerItem_123`，当前页带 `PGT_pagerItemActive_123`
- 下一页：`li[data-testid="beast-core-pagination-next"]`，到尾页加 `PGT_disabled_123`
- 已选计数：正则 `已选订单[:：]\s*(\d+)`

**商品主图（导出 xlsx 里没有，只能从 DOM 抓）**
- 图片是 IntersectionObserver 懒加载，`window.scrollTo` **无效**（表格在内部滚动容器里）。
  必须**逐行 `tr.scrollIntoView({block:'center'})` + 等 ~260ms**，加载完的 img 带
  `class="lazy-image ... loaded"` / `data-state="succ"`。实测 20 行 42 个子订单 100% 拿到图。
- 一行可含多个子订单，每个子订单是独立块（含 img + `子订单号：` + SKU/SKC/SPU ID + 属性）。
  **按「包含 img 且包含『子订单号：』的最小块」遍历**取 (子订单号 → 图 URL)，
  不要按整行取（会把主订单号误当子订单号）。类名 `_3AHRHYjy` 是构建期 hash，别硬依赖。
- URL 形如 `https://img.kwcdn.com/product/open/{hash}-goods.jpeg?imageView2/2/w/800/q/70/format/avif`。
  **`format/avif` Excel 不认，取图时要把参数改成 jpeg**（如 `?imageView2/2/w/800/q/85/format/jpeg`）。
- 下载必须带浏览器头 + 重试（kwcdn 与 Temu CDN 同源策略，裸 requests 会被拦，见 CLAUDE.md）。

**DOM 里额外有、导出文件里没有的字段**
- `成交单价：63.99(CNY)` / `成交总价：127.98(CNY)`，容器 id 规整：
  `{订单号}-{子订单号}-info`（单价）、`{订单号}-{子订单号}-all`（总价）。
- ⚠️ 疑似「Temu商家助手」插件注入（工具栏有「半托获取成交价（商家助手）」按钮）。
  **按 best-effort 处理：抓到就写「平台成交价」列，抓不到留空，绝不因此中断主流程。**

**环境**
- 已登录调试 Chrome 在 `http://localhost:9222`（CDP），`connect_over_cdp` → `browser.contexts[0]`。
- 顶部站点切换标签：`全球 / 美国 / 欧区 / 商家中心`。探索时停在「全球」，其下混合各国站点。
- Windows 控制台中文乱码：调试 dump 一律写 UTF-8 文件再 Read，勿直接 print。

## 3. 导出 xlsx 的真实列结构（19 列，sheet 名 `sheet1`，表头在第 1 行）

```
0订单号  1站点  2订单状态  3子订单号  4应履约件数  5商品名称  6SKUID  7SKCID  8SPUID
9SKU货号  10商品属性  11运单号  12物流商  13发货仓
14订单创建时间  15要求最晚发货时间  16实际发货时间  17预计送达时间  18实际签收时间
```

样例行（待发货态）：
```
PO-045-<订单号> | 哥伦比亚站 | 待发货 | 045-<子订单号> | 1
| 2026 新款夏季女童公主裙，儿童生日派对礼服 | <SKUID> | <SKCID> | <SPUID>
| 120341-Apricot-3-4Y | 杏色 / 3-4Y | -- | -- | --
| 2026-07-27 10:16:38 | 2026-08-20 13:29:59 | -- | 2026-09-03 12:59:59 | --
```

要点：
- **按子订单展开**（20 个订单导出 22 行）。
- 空值是字符串 `--` 而非空单元格，解析时要归一化成空串。
- 待发货态下 运单号/物流商/发货仓/实际发货时间/实际签收时间 全是 `--`。
- 样本文件留在 `workspace/orders_sample/sample_export.xlsx`（gitignored），可离线跑解析单测。

## 4. 登记表结构与 Sheet 映射

登记表 102MB，13 个数据 Sheet + `WpsReserved_CellImgList`（WPS 嵌入图索引表）。
**表头行位置不统一**：`牛仔裤`/`童装` 第 1 行是大标题、第 2 行才是表头，其余第 1 行即表头。
→ 必须按 §7 的表头探测逻辑动态定位，绝不硬编码行号/列号。

**订单号前缀编码站点**（可用于校验，但优先信导出的「站点」列）：
`PO-045-`=哥伦比亚 `PO-159-`=秘鲁 `PO-211-`=美国 `PO-162-/098-/076-`=欧区各国

**各 Sheet 实际取值采样**（有效数据行 / 订单店铺 / 站点区分）：

| Sheet | 行数 | 订单店铺 | 站点区分 |
|---|---|---|---|
（行数为量级示意，不是真实业务量）

| Sheet | 行数量级 | 订单店铺 | 站点区分 |
|---|---|---|---|
| `StoreA全球1` | 数百 | StoreA全球 | 哥伦比亚 秘鲁（各占几成） |
| `StoreA牛仔裤` | 数百 | StoreA | 无此列，订单号全 PO-211(美国) |
| `StoreB` | 数千 | StoreB | 哥伦比亚(多数) 秘鲁(少数) |
| `StoreB欧区` | 数十 | — | 德国/西班牙/意大利/法国/波兰/匈牙利 |
| `StoreC美国` | 数十 | StoreC | 美国 |
| `StoreC全球` | 百余 | StoreC | 哥伦比亚(多数) 秘鲁(少数) |
| `牛仔裤` | 数百 | StoreE / StoreF / StoreA | 无此列 |
| `童装` | 数百 | StoreE / StoreF | GL/ML/哥伦比亚站/秘鲁站 |
| `牛仔裤2026-2月` | 数千 | StoreE / StoreF | 美国站 / 美国 |
| ` StoreD` | 十余 | StoreD | 美国（列名是「站点」不是「站点区分」） |
| `StoreB-袜子` | 数十 | — | 美国 |
| `美国袜子` | 个位数 | StoreF | 美国站 |
| `运单号` | 数十 | 只有 订单号/包裹号 五组重复列 | — |

注意 `站点区分` 取值本身不统一（不带「站」的写法占绝大多数，带「站」的仅数十行），
**主流写法是不带「站」**，写入时把导出的「哥伦比亚站」归一化成「哥伦比亚」。

**映射规则做成配置**（`config/config.toml` 的 `[orders]` 段），不写死在代码里：
```toml
[[orders.sheet_map]]
store = "StoreA"                      # CDP 当前登录店铺名（页面右上角）
sites = ["哥伦比亚", "秘鲁"]          # 归一化后的站点
sheet = "StoreA全球1"                 # 目标 Sheet
store_value = "StoreA全球"            # 写进「订单店铺」列的值（≠ 登录店铺名，照抄表内既有写法）
```
未命中映射的订单**跳过并在进度事件里报告**，不臆测目标表（`StoreA牛仔裤` 虽是 StoreA 的
美国单，但列结构完全不同——有 Y1/Y2、XM、货号，是牛仔裤专用表，不能想当然当作 StoreA
美国站的落点）。

**已确认映射（2026-07-27，共 5 条，见 `config.example.toml`）**：

| store | sites | → sheet | store_value |
|---|---|---|---|
| StoreA | 哥伦比亚 秘鲁 | `StoreA全球1` | StoreA全球 |
| StoreB | 哥伦比亚 秘鲁 | `StoreB` | StoreB |
| StoreB | 德国 西班牙 意大利 法国 波兰 匈牙利 | `StoreB欧区` | StoreB |
| StoreC | 美国 | `StoreC美国` | StoreC |
| StoreC | 哥伦比亚 秘鲁 | `StoreC全球` | StoreC |

取值口径来自对登记表的只读探测，**按出现频次统计**而非去重集合：表里混着被贴进
「订单店铺/站点区分」两列的尺码与 DISPIMG 公式，去重后噪声与真值都只算「一条」分不清；
按行计数后真值动辄几十上百行（主表某站点可达数千行），噪声多为 1 行，一眼可分。

**只有 7 张 Sheet 具备写入条件**（判重列「订单号」+「尺码」都在）：`StoreA全球1`、`StoreB`、
`StoreB欧区`、`StoreC美国`、`StoreC全球`、`童装`、`牛仔裤2026-2月`。其余 7 张缺
「尺码」列（或连订单号列都没有），按现口径会被整表标 `no_key` 跳过——映射到它们没有意义。

**三条待定项已裁定（2026-07-27 用户确认，勿再推翻）**：

1. **StoreE / StoreF 的美国单：暂不映射，报未映射。** 导出文件没有类目字段，而这两个
   店铺在表里横跨多张按类目分的表（`牛仔裤2026-2月` 数千行美国单、`美国袜子` 个位数、
   `童装` 数百行），「店铺+站点」无法唯一确定落点。要支持得给 sheet_map 加类目维度。
2. **表名带月份的 Sheet 不是新订单落点**（`牛仔裤2026-2月`、`童装20262月份`）——是归档表，
   别往里追加当月单。故 `牛仔裤2026-2月` 虽可写，也不配进 sheet_map。
3. **缺「尺码」列的 4 张表本批不写**（`美国袜子`、`StoreB-袜子`、` StoreD`、`StoreA牛仔裤`）。
   不降级成「仅订单号」判重：一个订单多个子订单（尺码不同）时只有第一条能写进去，其余会被
   当重复丢掉。

## 5. 字段映射（导出列 → `StoreA全球1` 表列）

`StoreA全球1` 表头：`订单店铺 站点区分 订单号 尺码 平台物流跟踪号 国内发出时间 产品图片 产品图2
平台创建时间 采购日期 采购费用 平台成交价 Y2头程费用 采购订单号 物流情况`

| 登记表列 | 来源 | 说明 |
|---|---|---|
| 订单店铺 | 配置 `store_value` | 常量 |
| 站点区分 | 导出「站点」 | 去尾「站」：哥伦比亚站→哥伦比亚 |
| 订单号 | 导出「订单号」 | `PO-...` 原样 |
| 尺码 | 导出「商品属性」 | 如 `杏色 / 3-4Y`；表内既有数据末尾带换行，属文本换行样式 |
| 平台物流跟踪号 | 导出「运单号」 | 待发货态是 `--` → 留空 |
| 产品图片 | DOM 抓图 → DISPIMG | 按子订单号 join |
| 平台创建时间 | 导出「订单创建时间」 | `2026-07-27 10:16:38` |
| 平台成交价 | DOM 成交单价 | best-effort，抓不到留空 |
| 国内发出时间 / 采购日期 / 采购费用 / Y2头程费用 / 采购订单号 / 物流情况 / 产品图2 | — | 人工后续填，管线留空 |

**判重键**：`订单号 + 尺码`。不能只用订单号——一个订单可含多个子订单（多行，尺码不同）。
登记表没有子订单号列，所以用这个组合作为等价键。

## 6. 采集流程（确定性批处理，不 agent 化）

```
1. 预检 CDP 存活（复用 collect.service.ensure_cdp_alive）
2. 新建专用页签打开待发货订单 URL（open_owned_page 模式，绝不复用用户正在操作的页签）
3. 读「共有 N 条」与页数
4. 逐页循环：
     a. 逐行 scrollIntoView 触发图片懒加载
     b. 抓 (子订单号 → 图 URL, 成交单价) 映射，累积
     c. 点 thead 全选（勾本页）
     d. 点「下一页」，等表格刷新（data-status 的 page 变化）
   直到尾页（next 带 PGT_disabled_123）
5. 校验「已选订单」计数 == 总条数，不等则抛异常交重试
6. 点「导出订单」→ 取消勾「收货信息」→ 点「确认导出」→ expect_download 捕获 xlsx
7. 解析 xlsx（19 列，`--` 归一化为空）
8. 按子订单号 join 图片/成交价
9. 按 店铺+站点 查映射表定目标 Sheet；未命中的记为 skipped
10. 每个目标 Sheet：解析一次表头 SheetSchema 全批复用 → 读既有 (订单号,尺码) 集合判重
11. 下载主图（浏览器头 + 重试，avif→jpeg）
12. zip/XML 增量写行 + DISPIMG 嵌图（绝不用 openpyxl save）
13. 汇总：新增 N 行 / 判重跳过 M / 无映射跳过 K / 失败 L
```

**断点续跑**：靠第 10 步的判重，重复跑不会写重复行。

## 7. 架构落位（对标现有采集/活动管线三层）

```
app/orders/__init__.py     新建
app/orders/pipeline.py     新建 ← 页面操作（翻页勾选/触发导出/抓图）+ 解析导出 xlsx + 下图
app/orders/service.py      新建 ← 编排 + CDP 护栏 + 结构化进度事件（UI/CLI 共用）
app.py                     改：加 /orders/* 接口 + OrdersJob（对标 /activity/*）
templates/orders.html      新建 ← 对标 collect.html / activity.html
templates/index.html       改：加「订单登记」入口
orders_collect.py          新建：CLI 薄壳 ← 对标 batch_collect.py
config/config.example.toml 改：加 [orders] 段（登记表路径 + sheet_map）
app/tool/wps_excel_tool.py 改：判重需支持「两列组合键」（现有 existing_key_values 只支持单列）
```

复用（不重造）：
- `app.collect.service` 的 `CDP_URL` / `_emit` / `ensure_cdp_alive`
- `app.activity.service._connect_pages` 的 `open_owned_page` 专用页签模式
- `app.tool.wps_excel_tool` 的 zip/XML 写入 + DISPIMG 嵌图 + 表头解析

## 8. 已知坑（务必遵守）

- **openpyxl 绝不 `save` 登记表**：会删 `cellimages.xml`，毁掉全表 WPS 嵌入图。只走 zip/XML。
- **`max_row` 不可信**：`StoreA全球1` 的 `max_row=1048472` 但有效数据仅 280 行（格式撑出来的）。
  定位必须扫真实末行，不能用 `max_row`。
- **新单插在表头正下方、不追加到表尾**：登记表要「时间越新的越在上面」。`plan_writes` 先按
  `平台创建时间` 倒序排本批，`append_rows(insert_at_top=True)` 把表头以下所有行下移。
  下移要连带改的东西比想象的多：`mergeCell` / `autoFilter` / 条件格式 `sqref`、共享公式
  `ref`、`cfRule` 公式里的**绝对**引用（`$C$1:$C$10`；相对的 `C1` 是求值锚点不能动，
  只改 sqref 会让「订单号重复标红」整段错位）、`workbook.xml` 里本表的 `definedName`。
- **历史 368 行不做整表重排**：其中 208 行的「平台创建时间」是空的（第 73~280 行那段人工
  录入没填），时间文本还有 4/10/16/19 四种长度，没有可靠排序依据。故只保证新写入的块
  内部有序且在最上面，底下的人工历史块维持原样。
- **表头行位置不统一**（第 1 行或第 2 行），必须动态探测。
- **各 Sheet 列序不同**，批次开始解析一次 SheetSchema 全批复用，绝不硬编码列号。
- **图片懒加载**：`window.scrollTo` 无效，必须逐行 `scrollIntoView`。
- **avif 格式**：Excel 不认，下载前把 URL 参数改 jpeg。
- **收货信息不导出**：买家 PII，不落本地表。
- **导出按钮 disabled**：未勾选时点了也没反应，别误判成流程走通。

## 9. 施工顺序

1. `[orders]` 配置段 + `wps_excel_tool` 两列组合判重（小改，先做）
2. `app/orders/pipeline.py`：先做**离线可测**的部分——解析导出 xlsx、字段映射、站点归一化，
   用 `workspace/orders_sample/sample_export.xlsx` 跑单测
3. `app/orders/pipeline.py`：页面部分（翻页勾选、触发导出、抓图）——**先 dry-run**
   （只导出+解析+打印将写入的行，不落盘）实机验证
4. `app/orders/service.py` 编排 + 进度事件
5. 实机 dry-run 全量跑通 → 确认行数/字段对得上 → 再开写入
6. `orders_collect.py` CLI + `app.py` 路由 + `templates/orders.html`

**写入是不可逆操作**（虽有备份），第 5 步 dry-run 结果须人工确认后才开写。

## 10. 施工进度（2026-07-27）

代码侧 1/2/3/4/6 步已落地，离线单测全绿（`pytest tests --ignore=tests/sandbox` 168 passed）：

| 交付物 | 说明 |
|--------|------|
| `config/config.example.toml` `[orders]` | 登记表路径 / 列表 URL / `dedupe_by` / `sheet_map` |
| `app/tool/wps_excel_tool.py` `append_rows()` | 批量写入：一次备份 + 一次 zip 重写（逐行追加 247 行要几小时、留 200+ 份备份）；`key_cols` 组合键判重；`_xml_unescape` 修掉「表里存 `&#10;` 导致判重永不命中」；`insert_at_top=True` 插到表头下方并整体下移（见 §8） |
| `app/orders/pipeline.py` | 翻页勾选 → 官方导出 → 解析 xlsx → 抓主图 → avif 转 jpeg；`detect_store` best-effort |
| `app/orders/service.py` | `load_orders_config`（config.toml 缺 `[orders]` 段时退到 example）、`plan_writes`、`run_orders_batch`（默认 `dry_run=True`） |
| `orders_collect.py` | CLI，默认试跑，`--write` 才落盘 |
| `app.py` `/orders` `/orders/worklist` `/orders/sheet_info` `/orders/batch` `/orders/batch/{id}/events` | 作业 + SSE，对标 `/activity/*`；service 的收尾 `done` 转发时改名 `batch_done`，避免和 SSE 哨兵撞名 |
| `templates/orders.html` | 店铺 / 登记表 / 目标 Sheet 三个选择器（见 §11）；「正式写入」开关默认关 + 二次确认 |
| 单测 | `tests/test_wps_excel_batch.py`(12) / `test_orders_pipeline.py`(16) / `test_orders_service.py`(11) / `test_orders_web.py`(7) |

施工中发现并修掉的问题：

- `sweep_pages` 同步调用 `on_page`，而 service 的回调是 async → 协程建了没 await，UI 一条页
  进度都收不到。改为 `inspect.isawaitable` 后 await（与 `collect.service._emit` 同口径），
  并补 `test_sweep_pages_awaits_async_on_page` 钉住。
- 登记表里的尺码带 XML 实体（实测 `'蓝色 / 120&#10;'`），判重取值须先反转义，否则每次重跑
  都会把同一行再写一遍。

`sheet_map` 已补到 5 条（见 §4），三条歧义项已裁定。

**待办：第 5 步实机 dry-run** — 当前**卡在登录**：2026-07-27 探测时调试 Chrome 里 8 个订单
页签全部重定向到 `/auth/authentication`，`detect_store` 读到空串、分页容器不存在。需要先在
调试 Chrome 里登录目标店铺，再跑 `python orders_collect.py` 或 UI 试跑，人工核对
「待登记行数 / 字段落位 / 带图比例」后，才勾「正式写入」。

注意 `detect_store` 读不到店铺名时 service 会直接中止（不猜落点），所以登录后若仍读不到，
用 `--store` / UI 的店铺输入框显式指定。

## 11. 页面上自选店铺 / 登记表 / Sheet（2026-07-27 追加）

原设计是「落点全由 config 的 sheet_map 决定，前端只回显」。但 sheet_map 覆盖不到的组合
（如 StoreE 的美国单，缺类目维度）会全部落进「未映射跳过」，用户没有别的出路。故给
UI 加了三个选择器，把落点判断交回用户：

- **当前登录店铺**：`<input list>` + `datalist`（候选取自 sheet_map 的 store，但**必须允许
  手填**——sheet_map 只配了 3 个店铺，真实店铺远多于此）。留空仍走页面自动识别。
- **订单登记表**：下拉，复用 `collect.service.list_workbooks()`（同一数据源，不重复造）。
  当前生效工作簿即使不在扫描目录里也并进候选，否则回显不出选中态。
- **目标 Sheet**：下拉，留空＝按 sheet_map 站点分流（原行为）；**选定则本批全部写进那张表**，
  绕过站点分流。实现是合成一条 `sites = []` 的通配映射（`_explicit_sheet_map`），因为
  `resolve_sheet` 里空 sites 本就是「不限站点」。此时 store 必须显式给出（要写进「订单店铺」
  列），否则中止。「订单店铺」列的值优先照抄 sheet_map 里同名 Sheet 的 `store_value`
  （表内既有写法，如 StoreA 的表写「StoreA全球」），没有就用用户填的店铺名。

**选 Sheet 时立刻探测可写性**（`/orders/sheet_info` → `inspect_sheet`）：判重列缺任一列就
红字标「不可写，会整表跳过」并禁用「开始」。为什么必须前置：service 遇到缺判重列的表会标
`no_key` 整表跳过、一行不写，若不提示，用户要跑完几分钟的翻页+导出才发现白跑。
独立成接口是因为登记表 102MB，切 Sheet 只该解析这一张表的表头——实测 0.1s，整个 worklist
（含工作簿枚举 + 14 张表名）也是 0.1s，够快。

选择落盘到 `workspace/orders_prefs.json`（与采集页的 `collect_prefs.json` **分开存**：两页选
的是不同的工作簿，混用会互相踩掉），下次开页自动回填。CLI 同步加了 `--sheet` 保持口径一致。

**未变的护栏**：dry_run 仍默认 True；管线仍不新建表、不切店铺；正式写入仍二次确认（确认框里
现在会写明目标工作簿与落点表）。

## 12. 实机 dry-run 暴露的三个 bug（2026-07-27）

登录恢复后首次实跑，按顺序踩到三个坑，均已修并补了回归单测（`tests/test_orders_pipeline.py`）。

**（1）店铺名读不到 —— 类名带构建哈希**
`detect_store` 原先只扫 DOM 文本，但店铺名挂在 `div._Wrz4-O9w` 这种构建期哈希类上，
祖先链里一个 `data-testid` 都没有，选择器一改版就失效。改成**先读页面状态**
`window.rawData.store.authUser.mallList[0].mallName`（`_STORE_JS`），只在恰好一个 mall 时
采信——多店账号说不清「当前店铺」是哪个，直接落到 DOM 兜底；两条路都读不到就返回 `''`
让上层中止，绝不猜。实测拿到 `StoreA`。

**（2）全选框点不动 —— beast-core 的真 input 是 0×0 透明的**
`table thead input[type=checkbox]` 点击 30s 超时报 not visible。DOM 探测发现 beast-core 惯例是
真 input 尺寸 0×0 且 `opacity:0`，可见可点的是外层 `label[data-testid="beast-core-checkbox"]`
（12×12）。于是拆成两个选择器：**状态从 input 读、点击点 label**（label 没了才退回点 input）。
点完追加 `is_checked()` 断言，状态没变就抛「页面可能改版」——不然勾选静默失败会导出空表。

**（3）`--max-pages` 被完整性护栏废掉**
翻页结束后有条「已选 != 总数就不导出」的护栏，但 `--max-pages` 本就是冒烟用的截断开关，
任何小于真实页数的限制都会撞上它（实测 `已选 40 != 总数 248` 直接中止）。改成**只在扫页自然
结束时（`has_next` 为假）才校验**；被 `max_pages` 截断时跳过校验，同时把 `truncated` 标记一路
透传 `swept` 事件 → collect 返回 → `_summary()`，让「只跑了 2 页」不会被误读成全量。
顺手补了循环外的 `cur = info` 初值，`max_pages <= 0` 不再 `NameError`。

**全量 dry-run 结果（248 条）**：13 页全勾（248/248，护栏未触发），官方导出 40117 字节，
解析 271 行 / 248 单，匹配并下载主图 271 张（失败 0），判重挡掉已登记 197 行，
待写 `StoreA全球1` 74 行、未映射 0，未落盘任何数据。

## 13. 逐行核对靠「待写计划 CSV」，不靠日志（2026-07-28）

日志里的 preview 只打 3 行，74 行的落点/字段没法核对。所以 **dry-run 结束时把每张目标
Sheet 的全量待写行导成 CSV**（`dump_plan_csv`，落桌面 `manus输出/订单待写计划/`），
列就是该 Sheet 的真实表头标题，附加 `序号` 与 `_图片`（有/无）两列，编码 `utf-8-sig`
让 WPS 直接双击能开、不乱码。

写入模式不导 CSV——数据已经进表了，核对直接看表更准。落盘失败只 `logger.warning`
返回已成功的部分：这是辅助产物，不能拖垮主流程。

## 14. 「成交单价」是插件注入的，且回填延迟约一天（2026-07-28 实测）

官方导出的 19 列里**没有价格列**，`平台成交价` 只能从 DOM 抓「成交单价」标签。这天再跑
dry-run，待写从 74 行涨到 88 行，新增的 14 行全部读不到价。逐单核对 DOM 的 `hasLabel`：
2026-07-27 12:04 之前下的单都有该标签，22:58 之后（含 07-28 当天）下的单连标签都不存在。
结论：这个字段由**商家助手浏览器插件**注入，不是 Temu 页面自带，回填有约一天延迟。

因此 `plan_writes` / `run_orders_batch` 默认 `require_price=True`：**无价订单不排入本批**，
单独归 `unpriced` 上报（不混进 `unmapped`——unmapped 是配置缺失、要人去改，unpriced 是
暂态、明天自己就好了）。为什么宁可留到下批也不带空价入库：判重键是 `订单号 + 尺码`，
空价行一旦入库就再也不会被这条管线重新访问，价格永远补不回来。

需要先占位时显式放行：CLI `--allow-no-price`，Web `POST /orders/batch {"allow_no_price": true}`
（页面上是一个默认不勾的开关）。汇总/事件里的 `unpriced_skipped`、`unpriced_samples` 是
UI/CLI 共用契约。
