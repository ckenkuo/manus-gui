# 店小秘 Temu 半托管发布管线 - 技术交付文档

## 概述

从原 skill（dianxiaomi-temu-publish，3423行）移植核心发布流程到 `app/publish/` 模块，**按只读优先分批搬运并对真站验证**。已完成 8 个核心阶段，覆盖「类目→属性→标题→尺码→变种→库存」的完整发布准备链路。

## 已完成阶段

| 阶段 | 函数 | 功能 | 验证状态 |
|---|---|---|---|
| ① | `auto_cat` | 类目逐级选择（DeepSeek语义判断 + lookahead前瞻） | ✅ 3级类目成功 |
| ② | `list_images` | 提取素材图/颜色图/描述图 URL 并下载 | ✅ 三组图提取成功 |
| ③ | `inspect` | 导出表单状态（7区块 + 变种表 + 运输信息） | ✅ 完整导出 |
| ④ | `check_attrs` / `dump_attrs` | 属性审核与填写（LLM判断 + 虚拟列表滚动 + 回读轮询） | ✅ 9项成功写入 |
| ⑤ | `set_titles` | 标题生成（LLM 10候选 + 合规筛选）+ 产地两级下拉 | ✅ 中英文标题 + 产地成功 |
| ⑧ | `fix_sizes` | 尺码勾选修正（使勾选状态与源商品一致） | ✅ 8尺码勾选 + 16行SKU表重生成 |
| ⑩ | `set_variant` | 变种信息填写（申报价/尺寸/重量/建议售价） | ✅ 16行全部填写 |
| ⑪ | `set_stock` | 库存SKU分类填写（分类/数量/单位） | ✅ 16行全部填写 |

## 文件结构

```
app/publish/
├── browser.py          # CDP会话管理 + 等待轮询
├── pipeline.py         # 8个阶段的完整实现（1730行）
└── extract.py          # 商品采集（1688源商品信息提取）

publish_inspect.py      # CLI入口，11个子命令
tests/test_publish_attrs.py  # 属性校验单测12项
workspace/_verify_pipeline.py  # 完整链路验证脚本
```

## 关键技术点

### 1. LLM推理（3处）

**类目语义选择**（阶段①）：
- DeepSeek 逐级判断最匹配类目（lookahead 前瞻：一次读全部子类目，减少API调用）
- 验证结果：3级类目「服装配饰 > 男装 > 套装」，每级带判断理由

**属性审核**（阶段④）：
- 7条实战规则（只填必填、成分和=100、里料纹理、拿不准不动...）
- 成分：源给了含量就按源值（棉90% → 自动补差10%）；源没写含量按该纤维 100%；
  源连主面料成分都没写按聚酯纤维 100%（`extract.COMP_DEFAULT_FIBER`），不让模型编比例
- 虚拟列表滚动：67项纤维列表每屏180ms滚动收集
- 验证结果：dry-run 10项建议，`--apply` 9项成功写入

**标题生成**（阶段⑤）：
- 10个候选英文标题 + 1个中文标题
- 合规性筛选：40-70字符、纯ASCII、无品牌词、无Emoji
- 品牌红线：属性里的品牌值 + 源标题开头3字，两处都不许进标题
- 验证结果：英文66字符合规，中文重写成功

### 2. 复杂交互处理

**虚拟列表滚动点选**（属性下拉）：
```javascript
// 虚拟列表每屏只渲染可视项，目标选项可能在窗口外
// 解决：滚动容器 holder.scrollTop += holder.clientHeight，每屏等180ms
for (let k = 0; k < 40; k++) {
  collect();  // 收集当前可见选项
  if (holder.scrollTop === lastTop) break;  // 到底了
  holder.scrollTop += holder.clientHeight;
  await sleep(180);
}
```

**产地两级联动下拉**（阶段⑤）：
- 第一级：国家下拉（中国大陆/中国香港...）
- 第二级：省份下拉（**动态出现**，选「中国大陆」后才有）
- 解决：分两步操作，选国家 → 等800ms → 选省份

**幽灵浮层防护**（属性点选）：
- 问题：同选项列表的多个字段（上装成分/下装成分/辅料成分）共享一个浮层池，点在隐藏浮层上会改错字段
- 解决：只在「距目标行最近的可见浮层」里找选项（`top > -1000 && width > 50`，按距离排序取最近）

**回读轮询+自愈重试**（属性写入）：
- 问题：Vue重渲染时序不定，点选后立刻回读可能读到旧值
- 解决：轮询最多4s，连续2次读到目标值才算成功；失败时「打开→点选→回读」整流程重试一次

**成分行数必须裁到本轮结果**（阶段④，2026-08-30）：
- 问题：保存报「错误：XX成分不能重复选择」，整单卡在阶段⑭
- 判据取证（编辑页 bundle `Layout-*.js`）：selectPercent 属性逐行取 `attrValueId`，
  `if (n.includes(p)) return C(\`${t._label}不能重复选择\`)`，且这道闸排在「百分比之和=100」**之前**
- 成因：页面初始行数由认领搬来的源数据决定，可能**多于**本轮重建结果；而写入只覆盖前 N 行、
  `_ensure_comp_rows` 只加不减，多出来的旧行原样留着。真站复现（rowid 173539495458369319）：
  「成分」3 行时按 2 行写入，回读得到 `['聚酯纤维(涤纶）', '棉', '棉']` —— 第 3 行旧「棉」与新写的第 2 行同纤维
- 解决：`_trim_comp_rows` 按本轮该字段 change 的最大 `row` 为目标，点末行 `.icon_remove` 裁到位
  （行 1 只有 `.icon_add`，行 2 起才有 `.icon_remove`；实测点末行删的就是末行）。
  只裁本轮真写过的成分字段，best-effort 吞异常

### 3. 批量表格填充

**变种信息**（阶段⑩）：
- 申报价：默认 **188.88**（`pipeline.DECLARE_PRICE_DEFAULT`）。发布页有「申报价」输入框、
  CLI 有 `--price`，想按实际卖价申报就填，留空按默认值。传入值统一过
  `normalize_declare_price` 归一（剥币种符号、保留两位小数、超范围退默认并告警）
- 尺寸：优先级 **显式 dims > 服装类固定 30x25x3 > 源 packInfo > LLM 预估**。
  服装压平装快递袋、规格由袋决定，逐个问模型只会在同批里估出一堆互不相同的数；
  玩具等有刚性包装的品类才交模型按实际体积估（提示词要求先判包装形式，
  不再写死「快递袋」）。判服装看类目路径，类目缺失退标题，排除词优先
  （见 `_is_apparel` / `_APPAREL_WORDS` / `_APPAREL_EXCLUDE`）
- 重量：源 packInfo.unitWeightKg×1000，没有交 LLM 预估（`360g`）
- 建议售价：申报价÷7（188.88 → `26.98 USD`）
- 验证结果：16行全部填写，回读一致

**库存SKU分类**（阶段⑪）：
- SKU分类：0=单品 1=同款多件 2=混合套装
- 数量：默认1
- 单位：3=件 4=套 5=条
- 验证结果：16行全部填写，联动切换成功

## CLI 使用

```bash
# 阶段① 类目选择
python publish_inspect.py auto-cat <rowid> "<商品标题>" [--no-lookahead]

# 阶段④ 属性审核（dry-run）
python publish_inspect.py check-attrs <rowid> <product-info.json>

# 阶段④ 属性审核（实际写入）
python publish_inspect.py check-attrs <rowid> <product-info.json> --apply

# 阶段⑤ 标题生成与填写
python publish_inspect.py set-titles <rowid> <product-info.json>

# 阶段⑧ 尺码勾选
python publish_inspect.py fix-sizes <rowid> <product-info.json>

# 阶段⑩ 变种信息
python publish_inspect.py set-variant <rowid> <product-info.json> [--price 188.88] [--dims 25x20x5] [--weight 350]

# 阶段⑪ 库存SKU分类
python publish_inspect.py set-stock <rowid> [--category 1] [--qty 1] [--unit 3]

# 只读工具
python publish_inspect.py find <关键词>         # 草稿列表按标题找rowid
python publish_inspect.py inspect <rowid>       # 导出表单状态
python publish_inspect.py images <rowid> [--out <目录>]  # 提取图片URL并下载
python publish_inspect.py dump-attrs <rowid>    # 导出属性+选项
```

## 完整链路示例

### service 层批量编排（2026-08-21 起，推荐入口）

```bash
# 批量：tasks.json = [{"url": "<1688链接>", "title": "可选"}, ...]
python publish_run.py --tasks tasks.json --store Pawly --site 全球

# 单商品全流程（1688 链接 → 保存落库）
python publish_run.py --url <1688链接> --store Pawly

# 已有草稿续跑（跳过采集认领）
python publish_run.py --rowid 173539495450551101 --info <product-info.json>

# 断点续跑：重跑同一命令即可（已完成阶段自动跳过）；
# 或从指定阶段起重跑（阶段 id 见 --list-stages）
python publish_run.py --tasks tasks.json --from-stage material
```

UI：浏览器开 `/publish` 页，填店铺/站点/任务清单（每行一个 1688 链接或 `rowid|info_path`），
SSE 实时渲染 15 阶段进度格 + 人工检查清单。默认收尾在「保存落库」，⑮「立即发布」
需显式开启（CLI `--publish`）才执行。

⑬b 产品视频有一个批次级开关（UI「保留视频」/ CLI `--no-video`），默认保留——即把
认领时从 1688 连带搬来的视频裁成 Temu 允许的比例（1:1/3:4/16:9）后回填。整批不想要
视频时关掉它：直接点编辑页视频区的「删除」，省掉下载 + ffmpeg 转码 + 直传（每个商品
几十秒到几分钟）。删除只落在未保存的表单里，靠 ⑭ save 才真正生效。

### 逐阶段手动跑（调试用）

```bash
ROWID="173539495450551101"
INFO="C:\Users\Administrator\Desktop\manus输出\商品发布\product-1073654064193\product-info.json"

# 阶段① 类目
python publish_inspect.py auto-cat $ROWID "男童秋季POLO衫套装"

# 阶段④ 属性
python publish_inspect.py check-attrs $ROWID $INFO --apply

# 阶段⑤ 标题
python publish_inspect.py set-titles $ROWID $INFO

# 阶段⑧ 尺码
python publish_inspect.py fix-sizes $ROWID $INFO

# 阶段⑩ 变种
python publish_inspect.py set-variant $ROWID $INFO --price 188.88

# 阶段⑪ 库存
python publish_inspect.py set-stock $ROWID
```

## 待搬运阶段

| 阶段 | 函数 | 复杂度 | 优先级 | 备注 |
|---|---|---|---|---|
| ⑨ | `add_sizechart` | 高 | **高** | 尺码表是必填项，DeepSeek预估测量参数 + 复杂弹窗交互（200+行） |
| ⑥ | `set_material` | 高 | 中 | 素材图替换：API上传 + CDP鼠标悬停菜单 + 弹窗选图 |
| ⑦ | `skc_replace_row` | 高 | 中 | 颜色图替换：批量上传 + 逐行悬停 + 拖拽排序 |
| ⑫ | `desc_*` | 中 | 低 | 描述图：富文本编辑器插入图片 |

### 搬运策略

**阶段⑨ 尺码表**：
- 优先级最高（必填项，不填无法提交）
- DeepSeek 按源 sizeChart 身高/体重参考生成测量参数（衣长/胸围/袖长...）
- 弹窗交互复杂：尺码分类搜索 + 参数填表格 + 模板名

**阶段⑥⑦ 图片上传**：
- 依赖 CDP `Input.dispatchMouseEvent` 真实鼠标移动（合成事件无效）
- 悬停菜单展开 + 弹窗选图 + 拖拽排序
- 需要验证 Playwright 的鼠标事件是否与原脚本的 CDP 调用等效

**阶段⑫ 描述图**：
- 富文本编辑器插入图片
- 相对简单，可延后

## 安全约定（发布闸门）

阶段⑮「立即发布」是全管线唯一不可逆的一步，**默认不执行**：
- `pipeline.publish_now` 必须显式传 `confirm=True`，否则返回 `refused`
- `service` 的 ⑮ 阶段默认 `skipped`，要 `do_publish=True` 才跑；且会再核一次
  ⑭ save 是否成功（草稿没落库点发布只会重复撞同一批前端校验）
- CLI 必须显式加 `--publish`
- 发布意愿**不进状态文件**——它属于本次运行，否则续跑一次就会静默又发一遍

2026-08-24 之前这里是「不提供 publish 入口」，已按用户明确要求解除；
「不显式要求就绝不发布」的取向不变。理由仍然是：真实商家账号、上架后要下架才能改。

## 测试覆盖

**单元测试**（`tests/test_publish_attrs.py`）：
- 属性校验规则 12 项全过
- 成分补差、里料纹理、拿不准不动、只填必填...

**真站验证**（每个阶段）：
- 商品：男童POLO衫套装（1073654064193）
- rowid：173539495450551101
- 8个阶段全部通过真站验证

**完整链路验证**（`workspace/_verify_pipeline.py`）：
- 阶段①→④→⑤→⑧→⑩→⑪ 依次执行
- 验证各阶段输出可被下一阶段使用
- 最终表单状态符合发布要求

## 配置要求

**LLM 配置**（`config/config.toml`）：
```toml
[llm.publish]
name = "deepseek-reasoner"  # DeepSeek推理模型
temperature = 0
max_tokens = 4000
```

**CDP 启动**：
```bash
chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\chrome-profile"
```

## 已知限制

1. **点击方式因场景而异**：
   - 编辑页按钮（保存等）：JS `el.click()` 有效
   - 认领弹窗店铺复选框：必须真实点击，JS `.click()` 不触发 Vue
   - 素材图悬停菜单：必须 CDP 真实鼠标移动

2. **产地两级联动**：
   - 第二级省份下拉是动态出现的
   - 必须先选「中国大陆」，等待800ms，省份下拉才会渲染

3. **虚拟列表滚动**：
   - 每屏等待180ms（太快会漏选项，太慢浪费时间）
   - 最多滚动40屏（覆盖67项纤维列表）

4. **幽灵浮层**：
   - 同选项列表的多个字段共享浮层池
   - 必须按距离定位最近可见浮层，禁止扫描隐藏浮层

## 后续工作

1. ~~**交付 service 层**~~ ✅（2026-08-21 已交付）：
   - `app/publish/service.py`：15 阶段批量编排（`run_batch`），结构化事件 + 断点续跑
     （`workspace/publish-state/<offer>.json`）+ 每阶段耗时统计 + 错误分类
     （aborted 前置中止 / 阶段失败记 fail 继续下一商品 / manual_check 人工提示）
   - `app/publish/vision.py`：Grok 视觉决策层，图片三件套（素材图选图 / SKC 分色 /
     描述图删换规划 + 英化质检）全自动，拿不准发 `manual_check`
   - CLI：`publish_run.py`（--tasks/--url/--rowid 三种输入，--from-stage 续跑）
   - UI：`/publish` 页面（POST /publish/batch + SSE 进度渲染 + 人工检查清单面板）
   - 离线单测：`tests/test_publish_service.py`（21 项，mock 浏览器/LLM）

2. **继续搬运**：
   - 阶段⑨ 尺码表（优先级高）
   - 阶段⑥⑦ 图片上传（需验证鼠标事件）
   - 阶段⑫ 描述图（相对简单）

3. **监控与日志**：
   - 每阶段耗时统计
   - LLM token 消耗统计
   - 失败重试次数记录

4. **批量处理**：
   - 支持多商品并发（每个商品独立会话）
   - 进度展示（N/M 完成，当前阶段）

## 参考资料

- 原 skill：`D:\KimiData\daimon-share\daimon\skills\dianxiaomi-temu-publish\`
- SKILL.md：阶段划分与规则说明
- 实测记录：2026-08-17/18 Pawly/哥伦比亚站验证
