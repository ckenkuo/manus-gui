# 采集侧多 SKU 前端价改造计划（跨 session 交接）

> 本文件是给「专门处理采集侧 + Excel」的新 session 的交接文档。
> 编写日期 2026-07-24。所有事实均来自一次只读调查（未改任何文件），证据带 `file_path:line`，
> 新 session 可直接据此上手，不必从零重查。

## 0. 一句话缘由

下游「活动报名」页面：同一个 SPU 展开后每个 SKU 一行、各自参考价不同，需要**按 SKU 分别填申报价**。
但采集管线目前只采了 SPU 级的一个统一价，Excel 成本表也是一品一行、无 SKU 维度，导致活动侧
拿不到「每个 SKU 各自的价格基准」，只能对所有 SKU 行填同一个 SPU 级价。本次要从**源头**补齐。

关联：活动侧多 SKU 分别填价的需求（Request G）依赖本文件的采集侧改造先落地。活动侧填价改造
另见 [docs/activity-pipeline-plan.md](activity-pipeline-plan.md)，本文件只管采集侧 + Excel。

## 1. 现状事实（全链路数据流 + 证据）

```
[Temu 列表接口 searchForSemiSupplier]
   dataList[].supplierPrice   ← 唯一价格字段，SPU 级单值（供货价，非前端售价）
        │  app/collect/service.py:202  (_FETCH_ALL_JS 内 price: it.supplierPrice)
        ▼
[worklist.json 每条 item]  扁平 dict: {spu,name,site,category,price,image,mallid,store}
        │  service.py:196-206  (push 的对象结构；price 就是 supplierPrice，无 skus 字段)
        ▼
[采集管道 collect_one_product]  只用 item['image'] 去 1688 图搜比价；
   item['price'] 不参与判价，仅透传
        │  app/collect/pipeline.py:733, 710
        ▼
[write_product_row 组装 column_values]
   sale_cell = _to_number(item['price'])      ← pipeline.py:937
   daily     = sale_cell (同一个值)            ← pipeline.py:955（注释：日常价无独立来源时暂用销售价）
        ▼
[WpsExcelTool append_product_row]  一个 SPU 写一行
   sale → 「销售价」列，daily → 「日常价」列    ← 列由表头解析，wps_excel_tool.py:42-53
   purchase(采购价)/weight 来自 1688 货源侧    ← 与 Temu 前端价无关
```

关键结论：

- 采集**全程只有一个 SPU 级价** `supplierPrice`，且被同时写进「销售价」和「日常价」两列。
- **Excel 成本表判重按 SPU、一品一行**，字段规则里只有
  `spu/image/site/category/daily/sale/purchase/weight/ros/note`，**无 sku/skc 列**。
  证据：`_FIELD_RULES` in [app/tool/wps_excel_tool.py:42-53](../app/tool/wps_excel_tool.py#L42-L53)；
  判重按 SPU：`existing_key_values`（wps_excel_tool.py:264-290）、批次过滤 `str(it["spu"]) not in done`
  （service.py:1015-1018）。
- 1688 货源侧确实读了各 SKU 价（`.module-od-sku-selection`/`.item-price-stock`，pipeline.py:57-74），
  但 `judge_price` 把它们**压成一个采购单价**返回（pipeline.py:567-594）——那是**采购成本侧**，
  方向相反，不能拿来填 Temu 前端售价的缺口。

## 2. 两个必须正视的裂缝（不只是"缺 SKU 维度"）

1. **写进 Excel 的是供货价 `supplierPrice`，不是前端实际售价**。而活动页参考价是按**前端实际售价**
   算的（见 [app/activity/pipeline.py:1764-1766](../app/activity/pipeline.py#L1764-L1766) 注释）。
   这正是此前 SPU 6322830186「申报价 7.0 高于参考价 6.65」的**同源问题**（Excel 价口径 ≠ 前端售价）。
   多 SKU 化若不解决口径，只是把这个错误复制到每个 SKU。
2. **接口是否真有 SKU 级价格字段，尚未实测**。子 agent 未抓包，不能断言。项目铁律：**接口码/选择器
   必须实测**，不能凭猜写 `_FETCH_ALL_JS`。有力线索见 §4。

## 3. 待用户拍板的开放问题（决定改动方向，务必先定）

### 问题 A（最关键）：各 SKU 的活动申报价怎么定？

这一条直接决定「要不要动采集侧和 Excel」：

- **方案 A1｜每个 SKU 填到页面那行的参考价上限**：活动侧直接读页面逐行「参考申报价格」即可，
  **采集侧和 Excel 一行都不用改**，只改活动报名填价逻辑（照抄加速器侧成熟范式 `_set_accel_prices`
  的 `data-accel-idx` 逐行标记 + 逐行读参考价 + 逐行填）。**改动面最小、风险最低**。
- **方案 A2｜申报价 = 各 SKU 前端售价 × 折扣率，且需 ≥ 各 SKU 销售底价**：必须采集各 SKU 前端价
  （可能还要各 SKU 销售底价），采集侧 + Excel 都要改。改动面大。

> 建议先确认 A。若填价规则本质是「贴着每个 SKU 各自参考价上限报」，则根本不用碰采集/Excel 高风险区。
> 若必须按各 SKU 前端价 × 折扣率算并卡各自底价，才进入下面的采集侧改造。

### 问题 B：若确需采 SKU 价存进 Excel，承载方式选哪个？

- **方案 B-JSON（推荐）**：不动行结构、不动一品一行，用某列存 `{skcId: 前端价}` 的 JSON 串。
  与 DISPIMG 写入、按 SPU 判重全兼容，风险最低。缺点：人工在 Excel 里看 JSON 不直观。
- **方案 B-多行（不建议）**：一个 SPU 写多行（每 SKU 一行）。会打破「判重按 SPU 一行一品」的
  全套假设（`existing_key_values`、公式随行、DISPIMG 图片随行、活动侧 `read_row_by_key` 按 SPU
  唯一行），几乎要重写 `wps_excel_tool` 和判重逻辑。**风险最高**。
- **方案 B-定宽多列**：sku1价/sku2价…仅当 SKU 数固定且少时可行；SKU 数不定则不现实，且要动表头。

### 问题 C：Excel 该存的到底是前端售价还是供货价？

见 §2 裂缝 1。做多 SKU 前必须一并厘清口径，否则错误被复制到每个 SKU。

## 4. 动手前必做：抓包确认接口字段（项目铁律）

在改 `_FETCH_ALL_JS` 之前，**先抓一次真实响应**确认 SKU 级价格字段名，不能凭猜：

- 首选核实 `searchForSemiSupplier`（列表接口，service.py:178-210 的注入脚本消费它）响应里
  `dataList[]` 是否挂着 SKU 级数组/价格字段。
- 备选线索：活动侧 `read_stock` 被动监听的 `skc/pageQuery` 接口，响应
  `pageItems[].productSkuSummaries` 就是 **SKU 级数组**，每个 SKU 现在只取了 `virtualStock`
  （[app/activity/pipeline.py:791-793](../app/activity/pipeline.py#L791-L793)），
  且 `it.productId == 成本表 SPU ID`（该文件第 24 行注释）。`productSkuSummaries` 里很可能
  也带 SKU 级价格字段（代码没取价，故字段名待抓包确认）。
- 抓包方式：可在已接管的 Chrome 里打开半托管商品/列表页，DevTools Network 过滤上述接口关键字，
  查看响应 JSON 结构，记录 SKU 价的确切字段路径。

## 5. 改动点清单（仅当问题 A 选 A2 时才需要采集侧改造）

按文件列出，**Excel 行结构/表头改动为最高风险项**：

| 文件 / 函数 | 改动 | 风险 |
|---|---|---|
| `app/collect/service.py` `_FETCH_ALL_JS`(:178-210) | 从接口响应额外解析各 SKU 价数组（**前提：先抓包，见 §4**；若列表接口不含，则仿 `read_stock` 被动监听 `skc/pageQuery` 另取） | 中，接口字段未实测须先抓包 |
| `app/collect/service.py` worklist 落盘(:196-206, 478) | worklist item 增加 `skus: [{skcId, skuName, frontPrice, ...}]` 数组 | 低，JSON 扁平加字段兼容 |
| `app/collect/pipeline.py` `write_product_row`(:908-992) | 决定 SKU 价往哪写（优先方案 B-JSON） | **高（触碰 Excel 写入）** |
| `app/tool/wps_excel_tool.py` `_FIELD_RULES`(:42-53) 及写入 | 若走"新列/多行"需动此处；DISPIMG 表只能走 zip/XML | **高**，`_append` 的行/样式/公式/图片逻辑都假设「一 SPU 一行」 |
| `app/activity/service.py` `_read_cost`(:149-166) + `_process_one_spu`(:283-321) | 改为读 SKU 级价、逐 SKU 算 submit_price | 中 |
| `app/activity/pipeline.py` `enroll_activity` step4/5(:1768-1809) | 单值填价改逐 SKU（照抄 `_set_accel_prices` :2096-2118 的逐行范式） | 中，加速器侧已有成熟范式 |

> 最小改动路径：若问题 A 选 A1（填到页面参考价上限），则**仅改** `enroll_activity` step4/5 为逐行，
> 采集侧与 Excel **零改动**。

## 6. 铁律与陷阱（务必遵守，摘自 CLAUDE.md）

- **WPS DISPIMG Excel 绝不用 openpyxl `save`**：成本表用 WPS 嵌入图，openpyxl 保存会毁图，必须走
  zip/XML 直接改。任何触碰 Excel 写入的改动都受此约束。
- **按 Sheet 真实表头写入/判重，绝不硬编码列号**：批次开始解析一次 `SheetSchema` 全批复用。
  新增 SKU 列同样要走表头解析。
- **接口码/选择器必须实测**：见 §4，先抓包再写。
- **best-effort**：采集辅助路径异常 `logger.warning` 吞掉不中断主流程；判价/判重主流程失败才抛异常。
- 相对日期换绝对日期再写进注释/文档。

## 7. 给新 session 的起手动作建议

1. 先跟用户敲定 §3 的问题 A（A1 还是 A2）——这决定后续全部工作量。
2. 若 A2：按 §4 抓包确认接口 SKU 价字段 → 再定 §3 问题 B 的 Excel 承载方式 → 才动代码。
3. 若 A1：跳过采集侧，直接去活动侧改 `enroll_activity` 逐行填价（见活动计划文档）。
4. 改 Excel 相关前重读 [app/tool/wps_excel_tool.py](../app/tool/wps_excel_tool.py) 的 `_append`/DISPIMG 处理。
5. 改完按 CLAUDE.md 验证指南跑对应单测，别只改不验。
