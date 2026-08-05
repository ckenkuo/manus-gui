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

**悬浮层遮挡点击（2026-07-29 实机踩到，整批中止）**
- 商家助手插件在右下角注入 `#temu-ass-core-ui-dashboder-root`（内含 base64 悬浮球图），
  `position:fixed` 压在**分页条 / 底部操作栏 / 弹窗页脚**上方。
- 症状：Playwright 已解析到目标元素、判定 `visible, enabled and stable`、也滚进视口，但点击前的
  hit-target 检查命中的是那张 img，于是 retry 到 30s 超时：
  `<img src="data:image/png;base64,..."> from <div id="temu-ass-core-ui-dashboder-root"> subtree
  intercepts pointer events`。
  **这是遮挡，不是改版**——别去改选择器。
- 浮层按账号灰度下发，所以同一份代码换店铺/换天才复现。
- 解法：点击前把 `[id*="temu-ass"]` 及其**后代**设 `pointer-events:none !important`
  （子节点自己声明 auto 会盖掉继承，只设根节点无效）。不隐藏节点、不还原（同一标签页要连续点
  勾选→翻页→导出，还原等于把坑埋回下一次点击）。插件是 React 渲染、节点会重建，所以每次点击前都调。
- 已接入的三处：`select_all_on_page` / `goto_next_page` / `trigger_export`（含确认导出前再来一次）。

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

### 口径变更（2026-07-29）：允许成交价为空

原先默认 `require_price=True`，无价订单留到下批。实际用下来这条太贵：回填延迟约一天，等价
等于把当天所有单都压住，而登记表的主用途是发货与采购跟单，`平台成交价` 只是参考列。

现在 `plan_writes` / `run_orders_batch` 默认 **`require_price=False`**：成交价抓不到就把
`平台成交价` 留空，订单照常登记。已知代价：判重键是 `订单号 + 尺码`，空价行入库后重跑会被
判定已入库、不再补价，那一格需人工补填。

要恢复旧口径（无价留到下批、单独归 `unpriced` 上报，不混进 `unmapped`——unmapped 是配置
缺失要人去改，unpriced 是暂态）：CLI 加 `--require-price`，Web `POST /orders/batch
{"allow_no_price": false}`。汇总/事件里的 `unpriced_skipped`、`unpriced_samples` 仍是
UI/CLI 共用契约，只是默认口径下恒为 0/空。

## 15. 本批新增订单的 SPU/SKU 采购汇总

完成分表与判重后，把本批真正待新增的订单按 `SPU ID + SKU ID` 聚合，导出到桌面
`manus输出/订单采购汇总/<YYYYMMDD>/新增订单采购汇总_时间戳.xlsx`。汇总表包含采购总件数、订单数、
子订单数、商品属性以及完整订单号；第二张表保留逐条订单明细。已登记的重复订单、未映射订单
和被业务护栏延后的订单不进入采购汇总，避免重复采购。

### 15.1 每批一份 md 采购统计（2026-07-29）

同一份聚合结果再出一份 md：`新增订单采购统计_时间戳.md`，落同一目录，每批一个新文件
（时间戳取自官方导出文件名，与 xlsx 同批同名）。为什么 xlsx 之外还要 md：采购前真正要看的
是「这批里哪几张单买的是同一 SPU+SKU、能合成一次下单、各要几件」，这种判断要一眼读完的清单，
不是打开 WPS 拉筛选器；md 还能直接贴进聊天跟供应商对量。

三段结构见 §15.2（口径已从 SPU+SKU 改为商品级）。商品名里的 `|` 会截断表格列，统一转义。

### 15.2 聚合口径改为【商品级】：同款不同码合成一组（2026-07-29 实机反馈）

原先按 `SPU ID + SKU ID` 聚合。SKU 含颜色尺码，于是一条公主裙的 5 个尺码被拆成 5 组，
看的人得自己在表里认哪几行是同一款——而这正是这份统计该替他做的事。**采购是按【商品链接】
下单的：打开一次链接就该把这批要的所有码数/款式一次买齐。**

新增 `summarize_products()`，在 SKU 级之上再来一层：

- **分组键＝SPU ID**（等价于一个商品链接）。SPU 缺失时退回商品名——宁可按名字并，也不要
  每条各成一组；名字也空才用子订单号兜底，保证不同商品绝不会被并到一起。
- **变体键＝商品属性**（`杏色 / 3-4Y`）而不是 SKU ID：属性才是下单时页面上要选的那一栏，
  也是登记表「尺码」列的值。属性为空时退回 SKU ID，免得多个无属性变体并成一条。
- 同商品同属性被多张单买到 → 件数累加成一个规格，不重复列（`杏色 / 5-6Y | 3 件 |
  PO-045-2；PO-045-5`）。
- `is_multi`＝规格数 > 1 **或** 子订单数 > 1：两种都值得单独拎出来（前者要一次选多码，
  后者要合单）。

md 三段随之改为：一、需一次买多个码数/款式（**每个商品一个小节**，下面一张「要买哪些码各
几件」的表）；二、单规格单次采购；三、子订单明细（按属性排序，收货后按此分回各订单）。
第一段刻意不做成一张大表——大表里同款的行会被别的商品隔开，等于把「这一款要买哪几个码」
这个判断又推回给人。

xlsx 新增「商品汇总」作**第一张表**（原 `SPU_SKU汇总`/`订单明细` 保留，逐 SKU 核对仍要用）。
它的「采购清单」列把该商品所有规格拼成一格（`杏色 / 3-4Y×1；杏色 / 5-6Y×3`），一眼能照着下单。

`purchase` 字段与 `purchase_summary` 事件新增 `products` / `multi_products` / `variants`；
`groups` / `repeated_groups`（SKU 级）保留不动，逐 SKU 口径仍有人要看。

xlsx 与 md 各自独立 try、互不连坐，整段 best-effort：采购统计是辅助产物，写失败只
`logger.warning`，绝不影响登记表写入。dry-run 也照样出——试跑时人最需要的就是这份清单。
事件 `purchase_summary` 与汇总的 `purchase` 字段新增 `md_file`。

### 15.3 按日期分目录归档（2026-08-05）

两份产物不再直接堆在 `订单采购汇总/` 下，改落 `订单采购汇总/<YYYYMMDD>/`。跑了一周之后
根目录已经积到近 20 个文件，xlsx 和 md 交错排列，找「昨天那批」得先看文件名里的时间戳。

**为什么按天而不是按批**：一天里往往要跑好几批（补单、换店铺），一批一个目录会把父目录撑成
几十个只装两个文件的空壳；而采购是按天推进的活儿，同一天的几批本来就要一起看。

日期取 `stamp` 前 8 位（`stamp` 来自官方导出文件名 `订单导出_YYYYMMDD_HHMMSS`），
而不是现取 `datetime.now()`：同一批的 xlsx 和 md 共用一次计算结果，跨零点跑的批次不会
被拆到两个目录里。`stamp` 异常（拿不到导出文件名，退化成 `batch`）时才用当天日期兜底。

建子目录失败（目录名被同名文件占了之类）只 `logger.warning` 并落回分类根目录——沿用本段
既有的 best-effort 取向，归档方式坏了不该让整份统计导不出来。见
[app/orders/service.py](../app/orders/service.py) 的 `_purchase_out_dir`。既有的历史文件
留在根目录不动，管线不做迁移。

### 15.4 文件名前置店铺名（2026-08-05）

两份产物的文件名都变成 `<店铺>_新增订单采购汇总_<时间戳>.xlsx` / `<店铺>_新增订单采购统计_<时间戳>.md`。
多店铺时同一天会有好几份，光看时间戳分不出是哪家店的。

**店铺名前置而不是插在时间戳后面**：文件管理器按名称排序时，前置能把同店的几批自然聚到一起。

店名取 `got["store"]`（采集时实际识别或显式指定的那家），不是 `run_orders_batch` 的入参
`store`——后者可能为空、由 `detect_store` 补上。店名是页面自由文本，可能带 Windows
文件名非法字符（`\ / : * ? " < > |`），`purchase_file_stem()` 统一剔除；剔空或根本没识别到
店铺时退化成原来的不带店铺名格式，不留 `_` 空占位。

md 正文抬头也加一行 `- 店铺：<店铺>`：这份 md 常被整段贴进聊天跟供应商对量，脱离文件名之后
也得看得出是哪家店。`purchase` 字段与 `purchase_summary` 事件同步新增 `store`。

## 16. 增量采集：读表里已登记的订单号当水位，追上就停（2026-07-29）

原先每批都翻完全部页（247 条约 13 页），而绝大多数页早就登记过了，纯属白翻——逐行
`scrollIntoView` + 等 260ms 是翻页耗时的主要来源。改成采集前先读登记表，翻到「追上上次
登记位置」就停。

**水位＝目标 Sheet「订单号」列表头正下方那一条**（`read_known_order_nos` →
`first_data_value`）。写入走 `insert_at_top`（新行插到表头下方）且写前按创建时间倒序排，
所以数据区顶端行必然是上次登记的最新一条，单条即完整水位。不用「最新创建时间」当水位：
历史 368 行里 208 行的「平台创建时间」是空的、时间文本还有 4 种长度（见 §8），不可靠；
订单号做等值判定是精确的。

> 初版读的是**整列集合**（`existing_key_values`）。改成单条的原因见 §16.1——整列配合
> 「整页全已登记才停」的判据在稀疏表上结构性失效。单条不受表内行数影响，还省掉一次整列
> 扫描（本地是 zip 解压全表、云端是分页拉几千行）。返回值仍是 `set`（0 或 1 个元素），
> `sweep_pages` / `should_stop_incremental` 的既有契约不变。

**只在单表模式（显式给了 sheet）启用**。`sheet_map` 分流一次 sweep 同时喂 7 张表，各表
新旧程度不同（某张上周登记过、另一张停了一个月）。取并集会让落后的表被领先的表掩盖——
它的旧单在并集里「已知」，于是早停，那批永远补不回来。单表模式落点唯一、水位唯一。
分流模式一律退全量靠判重兜底，行为与改动前完全一致。

**早停判据**（`should_stop_incremental`，纯函数、离线可测）：本页出现水位那条订单号就停。
页面新→旧，翻到它说明它之后的都更旧、都是上次登记时已处理过的。空表读不到顶端值 → 水位为
空集，永远命不中，自然翻到底。判据按集合成员判定，`known` 多元素时退化为「遇到任意一条即停」，
语义一致。

**触发早停的那一页照样勾选导出**：判重键是 `订单号 + 尺码`，订单号已登记不代表它每个
尺码都登记了（同订单新增尺码是真实情况），跳过就漏行。重叠的旧单由判重挡掉，代价只是多
导出几十行。

### 16.1 判据修正：从「整页全已登记」改成「出现即停」（2026-08-04）

初版判据是「本页订单号**全部**已登记才停」，外加一条交错检测护栏（「首个已登记单之后不该
再有新单，否则退全量」）。实机跑 `WINTAK8.4+`（新建 Sheet，只登记过 1 个订单号，页面 1617
条待发货）暴露出两个叠加的缺陷，实际翻了 63 页：

- **粒度不匹配**：判据的粒度是「页」，水位的粒度是「单」。水位少于一页条数时，任何一页都
  不可能 20 条全命中，`all(flags)` 恒为假，早停出口结构性走不通。
- **交错检测无法区分「排序被改」和「表本来就稀疏」**：那唯一一条已登记单出现在第 4 页中间，
  前面的更新、后面的更旧，都从没登记过——这是新建表的正常状态，却被判成「排序可能被改或表
  里有空洞」，`desc_broken` 永久置位、退全量。

于是「表越空，增量越失效」，与设计意图正好相反（表空时本该也能停在第 4 页）。修正：

- 判据改为「出现任意一条已登记单即停」，与新→旧排序的语义直接对应，不再依赖水位密度。
- **删掉交错检测**。排序异常改由护栏 B（创建时间单调）单独负责——时间单调性才真正指向排序，
  不会跟水位稀疏混淆。
- 调用点不再把「本页读不到订单号」当退全量信号（原先 `reason and not stop` 一律置
  `desc_broken`），它只跳过本页；一次瞬时 DOM 读失败不该毒化整批增量。

**已知代价**：比表内最新那条**更旧**的空洞（上批某几条因未映射/无价被跳过）不再被增量补上，
翻到那条就停了。要补洞显式走全量（`--no-incremental`）。这是刻意取舍——原先为补洞而在稀疏
水位下无条件退全量，代价是增量完全不起作用。

**两道护栏**（早停依赖列表是新→旧排序，`list_url` 自带 `sortType=1`，但页面手点列头
排序或配置里 URL 被改都会破掉它）：

- **A 排序前置校验（确定性，2026-08-04 补）**：`check_list_sort_url` 在采集开始前校验
  `list_url` 的 `sortType`，不是 `1`（或压根没这个参数）就【整批中止】，提示里给出改哪个
  文件和 `--no-incremental` 两条出路。只在增量真要生效时校验——全量模式不依赖排序，不拦。
  为什么中止而不是降级成全量：排序反了会停在第 1 页、把新单全漏掉，而汇总显示的是
  「待写 0 行、早停」，跟正常的「本批无新单」无法区分，属于**静默漏采**。
  拦不住「页面手点列头改排序」（那不改 URL），那种仍靠护栏 B。

- **B 创建时间单调校验（best-effort）**：`check_created_desc` 校验跨页/页内时间不递增，违反则
  本批退全量并告警。⚠️ 列表页 DOM 里有没有创建时间**未经实机确认**，读不到就返回空串、静默
  降级到无护栏，不中断也不阻塞早停。另注意 `_PAGE_ORDERS_JS` 取的是行内**首个**
  `YYYY-MM-DD HH:MM:SS`，若首个匹到的是「要求最晚发货时间」而非「订单创建时间」，加急单与
  普通单期限不同会造出非单调序列而误触发——实机确认这一列时要一并核对取到的是哪个时间。

**早停与 `truncated` 语义相反，绝不复用同一字段**：`truncated` 是「被 max_pages 截断，
本批可能漏」，早停是「故意只取新的，不漏」。故新增独立字段 `stopped_early` / `fell_back`，
汇总里是 `incremental{enabled, known, stopped_early, fell_back, pages_swept, reason}`。
早停同样跳过「已选==总数」校验（本就只选了前几页）。

开关：默认开。CLI `--no-incremental` 关；Web `POST /orders/batch {"incremental": false}`，
页面上是默认勾选的开关，未选目标 Sheet 时置灰并注明「按站点分流，本批全量翻页」。
事件：`watermark`（水位与未启用原因）、`page` 增 `new_on_page`/`known_on_page`、
`swept` 增 `stopped_early`/`stop_reason`/`fell_back`。

## 17. 登记表补「数量」列：管线自动插到「尺码」右侧（2026-07-30）

**问题**：导出的「应履约件数」一直解析着，但 5 张登记 Sheet 的表头里**根本没有数量列**，
于是一单买 2 件跟买 1 件在表里长得完全一样，采购照 1 件下单就会缺货。

**裁定（用户确认，勿再推翻）**：不要人工加列，由管线自己改 XML 插列，位置固定在**「尺码」
列右侧**，标题固定「数量」。

- **只在写入模式插，dry-run 一个字节都不动**（改结构不可逆）。dry-run 的预览与「待写计划
  CSV」里补一个虚拟列 `数量*`（带星号区分），紧跟在「尺码」后面，让人照样能核对多件单。
- **只碰本批真有订单落进去的表**。目标表用 `resolve_sheet` 纯配置查出来——插列必须发生在
  `plan_writes` 之前（它一进去就把表头缓存进 `SheetPlan` 全批复用），那时还没有 plans。
- **幂等**：`insert_column_after` 见到该列已存在就直接返回 `inserted=False`，不动文件、
  不做备份，所以连跑多批不会插出第二列。表头标题集合 `QTY_TITLES` 还认「件数/商品数量/
  采购件数/应履约件数」，避免跟用户早先手工加的同义列重复。
- **写的是数值单元格**（`<c><v>2</v></c>`，没有 `t="str"`）：这一列要能直接求和、筛出
  多件单。解析不出的脏值退回原文，不静默写 0。
- **判重键不受影响**（仍是订单号+尺码），否则历史行会被判成新行重写一遍。
- best-effort：某张表插失败只 `logger.warning`，那张表数量列留空，绝不连累整批登记。
- 事件 `qty_column`：`{sheets: [{sheet, inserted, column, backup}]}`。

### 17.1 插列要同步平移的东西（`WpsExcelTool.insert_column_after`）

**不能用 openpyxl `insert_cols`**——本工作簿是 WPS DISPIMG 表，一 `save` 就毁嵌入图。
只能 zip/XML 直改，把所有跟列号绑定的结构一起右移：

| 结构 | 处理 |
|---|---|
| 单元格 `<c r>`、行 `spans`、共享公式 `ref` | `_shift_data_cols` |
| `dimension`、`sheetView` 视口/选区、`<cols>` 定义 | `_shift_head_cols` |
| `mergeCell`、`autoFilter`、条件格式 `sqref` 与 cfRule 绝对引用 | `_shift_tail_cols` |
| `workbook.xml` 里指向本表的 `definedName`（筛选区缓存） | `_shift_defined_names` |

几个要点：

- **`<cols>` 的新列继承左邻**：跨过插入位的段只把 `max` +1（新列并进那一段自然继承样式）；
  没有段覆盖插入位时补一条显式 `<col>`，样式取左邻段。不这么做新列会拿 `defaultColWidth`
  且无边框，跟左右两列明显不一致。**特别注意别把新列并进 `hidden="1"` 的段**——WINTAK 表
  原 E 列就是隐藏列，并错了新列直接看不见。
- **既有数据行不补空单元格**：新列的填充/边框由 `<col>` 的 style 提供；逐行插空 `<c>` 会给
  WINTAK 这种 5600 行的表凭空加几千个单元格。
- **新列的单元格样式取模板行左邻列**（`_left_neighbor_style`）：新列整表一个单元格都没有，
  `_text_cell_style` 学不到样式，写出来的格没边框，一看就是「补上去的」。
- **公式守卫**：带真单元格引用的公式一律抛错中止（移了列不改公式＝静默算错）。5 张表实测
  6111 个公式全是 `_xlfn.DISPIMG`（参数是图片 ID，与列号无关），放行。
- **`autoFilter` 存了筛选条件（`<filterColumn colId>`）时抛错中止**：colId 是相对筛选区起点
  的 0-based 序号，插列会让它指错列，表现是「表里数据凭空少了一半」。本表实测无此元素。
- 改结构前照例做时间戳备份（`*_插列前备份*.xlsx`）。

实机在真表副本上验过 5 张 Sheet：单元格引用逐一比对完全一致（多出的正好是新表头格），
DISPIMG 数量、`cellimages.xml`、`sharedStrings.xml`、`WpsReserved_CellImgList` 全部零改动。

## 18. 采购汇总 xlsx 版式：一屏放下 + 带图 + 人工勾采购完成（2026-07-30）

用户要求：自动换行、行高加宽、按当前屏幕分辨率调列宽做到**只上下滚、不左右滚**、每个 SKU
带主图、时间只留一个「创建时间」、再加一列人工填的「是否采购完成」。

- **列宽按屏幕分配**：`screen_client_px()` 读主屏逻辑分辨率（Windows `GetSystemMetrics`），
  减 130px 让给行号列/滚动条/窗口边框，余下的按各列权重分。权重是「该列希望占多宽」的相对
  值（标题字数 + 典型内容长度），**不是**内容实测最大值——改动前用 `max(len(内容))+2` 逐列
  取最宽，一个长商品名就能把那列撑到 45 字符、把整表推出屏幕，正是这次要治的病。配合自动
  换行，压窄的列折行而不截断。读不到分辨率按 1920 估（best-effort，不中断导出）。
- **图片列固定 84px**，不参与按比例放大：它要多宽由图框尺寸决定，跟屏幕多宽无关，跟着放大
  只是白占地方、挤掉真正需要宽度的商品名/采购清单。
- **行高**：带图的两张表 60 磅（≈80px，图缩到 76px 居中），不带图的「订单明细」25 磅。
  为什么带图表不能也用 25 磅：25 磅只有 33px 高，主图缩到那么小认不出是哪一款，而「看图确认
  要采的是这款」正是加图的目的。**「订单明细」的时间列因此要留够单行宽度**（约 140px），
  折成两行第二行会被 25 磅的行高截掉。
- **嵌图用 `twoCellAnchor` + `editAs="twoCell"`**（随单元格移动并调整大小），不是 openpyxl
  传字符串锚点时默认的 `oneCellAnchor`：这两张表开着筛选，人一按「是否采购完成」筛，
  oneCellAnchor 的图不会跟着隐藏，会整片糊在剩下的行上。两个锚点落在同一单元格内，靠偏移量
  圈出 76px 方框，图不会跨到右边的列。用的是普通浮动图而非 DISPIMG——这份文件是新建的，
  没有图索引表要维护。
- **「创建时间」＝该组最早的下单时间**（原「最早/最晚下单时间」两列合成一列）：采购紧急度
  看最早那张单什么时候下的。
- **「是否采购完成」**放第 2 列，留空给人填，挂「是/否」数据验证下拉——防手输「已采」「ok」
  这类五花八门的写法，后续按它筛未采购才筛得干净。
- 「订单明细」不放图：它是收货后分单用的长表，逐行放图会让文件明显变大、打开变慢。
- `purchase` 字段与 `purchase_summary` 事件新增 `product_images` / `sku_images`（各嵌了几张）。

## 19. 协作文档模式：登记目标切到金山在线表格（2026-08-04）

`[orders]` 配了 `cloud_file_id` 即进入云端模式：水位、判重、写入全部打在金山协作
表格上，本地 workbook 不再参与；**采购汇总不受影响，始终落本地**（`_export_purchase`
与写入路径天然解耦）。留空则走既有本地 xlsx 路径，行为不变。

- 云端文档：由本地登记表精简版上传生成（历史商品缩略图因上传网关 413 限制未上云，
  文字数据完整；本地缓存文件仍是全量存档）。file_id/链接写在 config.toml（gitignored）。
- 实现：`app/orders/kdocs_sheet.py`（KdocsSheet）封装 kdocs-cli 子进程调用，口径与
  WpsExcelTool 对齐——header 为 {列字母: 标题}、header_row 1-based、表头取前 3 行
  非空最多的一行；service 的 `_sheet_schema`/`_existing_keys`/`read_known_order_nos`/
  `ensure_qty_columns`/`_write_plans`/`inspect_sheet`/`get_worklist_status` 按
  `cloud_backend(cfg)` 分流。
- 写入链路：表头下方 `insert_rows_cols` 插空行 → `range_data_batch_update` 一次批量
  写值 + 按 URL 嵌图（`OrderRow.image_url`，avif 经 `to_jpeg_url` 转 jpeg；不再依赖
  本地图片文件）→ 读回首行验证（不信任 code:0）。限频 429001 自动重试一次，
  429002 熔断直接失败。
- 与本地模式的已知差异：
  - 列结构不自动改：云端表缺「数量」列只告警（提示手动补），不像本地自动插列——
    协作文档多人共享，管线不擅自改结构。
  - 判重/水位整列扫描上限 20000 行（`_MAX_SCAN_ROWS`）。
  - 无文件锁预检（云端无占用概念），dry-run 同样需要网络与 kdocs-cli 已认证。
- 认证：`kdocs-cli auth login`（token 存系统密钥链）；skill 文档见
  `~/.kimi-code/skills/kdocs/SKILL.md`。
