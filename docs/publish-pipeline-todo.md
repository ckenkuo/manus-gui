# 发布管线待完成工作交接

> 写于 2026-08-20。接手前先读 [publish-pipeline-delivery.md](publish-pipeline-delivery.md)（已完成部分的技术说明）。
> 原 skill 在 `D:\KimiData\daimon-share\daimon\skills\dianxiaomi-temu-publish\`，搬运时**先读原实现再改**，那些看似多余的等待/重试/清理几乎每行都对应一个踩过的坑。

> **2026-08-21 更新**：P0 已全部闭环（保存/运输信息/图片三件套/采集认领均已搬完并真站验证，
> 见下方现状表的函数清单），**service 层编排已交付**：`app/publish/service.py`（14 阶段批量编排 +
> 断点续跑 + 结构化事件）、`app/publish/vision.py`（Grok 视觉驱动图片三件套选图/删图）、
> `publish_run.py`（CLI）、`/publish` 页面（SSE 进度 + 人工检查清单）。
> 离线单测 `tests/test_publish_service.py`（21 项）。**尚未做整批真站跑通**——第一次跑批前
> 建议先用 rowid 模式单商品过一遍。

## 一、现状核实（2026-08-20）

`app/publish/pipeline.py` 2157 行，已实现 14 个公开函数：

| 函数 | 阶段 | 真站验证 |
|---|---|---|
| `open_edit` / `find_rowid` / `inspect` | 基础 | 已验证 |
| `list_images` | 图片清单（只读） | 已验证 |
| `auto_cat` / `read_current_category` | ③ 类目 | 已验证（6 级，含前瞻） |
| `dump_attrs` / `set_attr` / `check_attrs` | ④ 属性 | 已验证（9 项写入） |
| `set_titles` | ⑤ 标题 + 产地 | 已验证 |
| `fix_sizes` | ⑧ 尺码勾选 | 已验证（8 尺码 / 16 行） |
| `add_sizechart` | ⑨ 尺码表 | 已验证（2 参数 × 8 尺码） |
| `fix_sku_codes` | ⑩a SKU 货号英化 | 已验证（2026-08-23，8 行 / 2 词并发翻译） |
| `set_variant` | ⑩ 变种信息 | 已验证（16 行） |
| `set_stock` | ⑪ 仓库/库存/SKU分类 | 代码完成，**未真站验证** |

**回答「图片、描述长图、运输信息完成了吗」：都没有。**

| 缺口 | 状态 |
|---|---|
| ⑥ 产品素材图 | 未搬。`images.py` 的 `square_image` 已就绪，缺上传+替换 |
| ⑦ SKC 颜色图 | 未搬。`images.py` 的 `fit_34` 已就绪，缺上传+整行替换 |
| ⑪ 描述长图 | 未搬。删/替/生图全缺 |
| 运输信息 | 未搬。`inspect` 只读到 `shipmentInfo`，无写入函数 |
| ⑫ 保存 | 未搬。**没有保存，前面所有阶段的修改都不落库** |
| ② 采集+认领 | 未搬。目前只能手工认领后拿 rowid 进来 |
| service 层编排 | 未做。现在只有 CLI 逐阶段手动跑 |

另外 `set_stock` 里包装清单是占位（留「请选择配件」不动），原 skill 有 LLM 判断逻辑。

## 二、Grok 迁移带来的改动

LLM 主力已换 Grok（有视觉能力），这改变两件事：

**1. 图片合规检查可以自动化了。** 原 skill 的最大短板是「standalone 不能看图」，中文/水印/logo 识别、描述图相关性判断只能人工。Grok 有视觉能力后：
- 阶段① 提取后的 `imageUnderstanding` / `complianceNotes` 可自动回填（现在是空占位）
- 阶段⑥ 素材图选图可自动避开中文海报图（现在靠 `complianceNotes` 里的人工标注）
- 阶段⑦⑪ 图片合规可自动判断，不必转人工清单

**2. `app/publish/llm.py` 需要增加视觉入口。** 当前只有 `ask_json` / `ask_text` 两个纯文本入口。要用 `LLM.ask_with_images`（项目已有，见 `app/llm.py`），封装成 `ask_json_with_images(prompt, images, what)`。

注意 `images.py` 里 AI 编辑走的是 Packy `gpt-image-2`（图生图，去中文/英化），那是**图像编辑**不是图像理解，与 Grok 不冲突，两者都要保留。

## 三、按优先级排的待办

### P0：让流程闭环（不做这些，前面 9 个阶段的成果落不了地）

#### 1. 阶段⑫ 保存（`save`）

原实现：`dianxiaomi_edit.py:1976` 的 `cmd_save`。

要点：
- 顶部「保存」按钮是 `btn-orange`，**JS `el.click()` 有效**（编辑页按钮实测结论）
- 保存后弹「继续编辑 / 返回列表」确认框，**会挡住发布按钮，必须先关掉**
- 成功判据：无 `.ant-form-item-explain-error` + 草稿列表更新时间变化。`.ant-message` 捕获不到（自定义实现）
- 校验失败**没有 toast**：静默滚到出错区块、右侧锚点变红 `f-red`，要逐节读锚点找哪块红了

**~~安全约定原样继承：不实现发布入口~~**（2026-08-24 已按用户明确要求解除，
新增阶段⑮ `publish_now`，见文末「⑮ 立即发布」一节。闸门改为：默认不发布，
`confirm=True` / `do_publish=True` / CLI `--publish` 三处显式开关才执行。）

#### 2. 运输信息（承诺发货时效 + 运费模板）

原实现：`dianxiaomi_edit.py:355` 的 `cmd_set_shipping` + `:3176` 的 `cmd_set_shipping_page`。

要点：
- 发货时效是 radio 组，**必须点选项内部的 `input` 元素**，点外层 wrapper 无效
- 规则：选**最长**时效（SKILL.md 阶段⑩）
- 运费模板是 ant-select，默认唯一项
- 选后 DOM 重渲染，回读必须重新查询元素（不能用缓存引用）
- 两个入口的区别：`cmd_set_shipping` 会导航，`cmd_set_shipping_page` 不导航（工作流收尾用）。搬**不导航**那个，跟其它阶段一致

#### 3. service 层编排 ✅（2026-08-21 已交付）

对标 `app/collect/service.py` 已实现：
- `app/publish/service.py`：14 阶段顺序执行，进度走结构化事件（`stage_start` / `stage_done` /
  `manual_check` / `product_done` / `batch_done`），UI（`/publish` 页）SSE 直接渲染
- 断点续跑：`workspace/publish-state/<offer>.json` 存 rowid + 各阶段耗时 + 状态，
  重跑自动跳过已完成阶段；`--from-stage` 从指定阶段起重跑
- `manual_check` 事件对应原 skill 的「人工检查清单」，页面有独立面板
- 每阶段耗时统计（`stage_done.elapsed_s` + `product_done.elapsed_s`）

### P1：图片三件套

这三个都依赖同一套「图片直传 + 空间图片弹窗选图」机制，**先把公共部分搬对，三个阶段就都通了**。

#### 公共基础：`upload_image`（图片直传店小秘图床）

原实现：`dianxiaomi_edit.py:1329` 的 `cmd_upload_image`。三步：
1. 页面内 `fetch POST /api/cos/getSign.json`（带 cookie）拿 COS 签名
2. **`curl.exe` PUT** 文件字节到签名 URL（`Authorization: sign`，不需要店小秘 cookie）
3. 页面内 `fetch POST /api/cos/cosDxmCallBack.json` 登记入库

返回 URL：`https://wxalbum-10001658-file.dianxiaomi.com<fileId>`

**`full_cid` 是账号级常量**（原脚本硬编码 `5153348-`）。已在 `config.example.toml` 的 `[publish].full_cid` 留好配置位，搬运时从配置读，**不要硬编码**。抓包 `cosDxmCallBack.json` 的 `fullCid` 参数可得。

#### 4. 阶段⑥ 素材图（`set_material`）

原实现：`dianxiaomi_edit.py:1385`。

流程：`square_image`（1785²，已就绪）→ `upload_image` → CDP 悬停素材图展开菜单 → 点「空间图片」→ 弹窗选中刚上传的图 → 确定 → 回读 src 校验。

**踩过的坑（原样保留）：**
- 悬停菜单（本地图片/空间图片/网络图片/引用采集图片）**合成事件触发不了**，必须 CDP `Input.dispatchMouseEvent` 真实鼠标移动
- 要**先移开再移入**（重复悬停不触发 `mouseenter`）
- 菜单会自动收起，**点菜单项和选图确认必须放在同一个 evaluate 里连贯完成**
- 选图：`main-01` 常是中文海报图，优先选 `complianceNotes` 标注「干净」的图（Grok 视觉可自动判断）

**搬到 Playwright 后必须重验鼠标事件是否等效**（见 `browser.py` 的 `mouse_click` 注释：编辑页按钮上 CDP `dispatchMouseEvent` 被吞，但素材图悬停又必须用它，两条结论矛盾，只能实测）。

#### 5. 阶段⑦ SKC 颜色图（`skc_replace_row`）

原实现：`dianxiaomi_edit.py:1795`。

流程：`fit_34` 批量（3:4 且 ≥1340×1785，已就绪）→ 逐张 `upload_image` → 空间图片回填（追加行末）→ 再删行首旧图。

**顺序关键（2026-08-18 晚实测）：必须先挂新图再删旧图。** 行被删空后点「选择图片」建立不了行绑定，空间弹窗会把图挂到别的行（驼色行的图挂进了卡其行）。挂 N 张后旧图仍在前 N 位，逐次删第 1 张删 N 次，剩下的新图顺序=文件排序，颜色专属图命名 `01.jpg` 就自然在首位，免拖拽。

**其它坑：**
- 行按钮**必须 CDP 真实点击**才能建立绑定（合成 `fire()` 无效）
- `scrollIntoView` 平滑滚动未停就读坐标会导致 CDP 点偏——必须滚动 → 等待 → **另起 evaluate** 读坐标 → `elementFromPoint` 校验命中
- 每行**上限 10 张**：旧 6 张时先删 2 张再挂，防触顶挂不进去
- **素材图误替换事故**（2026-08-17 晚）：页面有两个「空间图片」菜单——SKC 行的（5 项，含「应用到所有颜色」）和素材图悬停的（4 项），都用 off-screen 负坐标停靠（`offsetHeight` 检测失效）。点错菜单会把弹窗绑定到素材图，确定后替换素材图**且不报错**。必须按「菜单项集合」区分：**SKC 菜单含「应用到所有颜色」**
- 多个同名菜单实例并存时，选离刚点的行按钮**最近**的那个（排除 -9999 停靠的旧实例）

#### 6. 阶段⑪ 描述长图（`desc_*`）

原实现：`dianxiaomi_edit.py:2786` 起的 `_desc_modules` / `cmd_desc_map` / `cmd_desc_delete` / `cmd_desc_replace` / `cmd_desc_save`。

规则（SKILL.md 阶段⑪）：工厂/公司/尺码表/与商品无关的图直接删（垃圾桶图标）；重复图删；中文图 `image_cleaner` 英化后替换；营销图可生图增补。**Temu 只关心商品图。**

要点：
- 「编辑描述」在 `.wireless-description-shadow` 里，CSS 悬停才显示，但 **JS `btn.click()` 直接有效**
- 编辑器是**全屏 `.ant-modal`**（不是新页面/新标签），打开后盖住整个编辑页——此时页面上所有 `elementFromPoint` 都会命中编辑器内的 IMG，**别误判成「遮挡」**
- 模块图在 `.smt-desc-content .desc-img-box img`，DOM 顺序即展示顺序
- 替换走「点图片 → 空间图片」

**Grok 视觉在这里价值最大**：判断哪张图与商品无关、哪张是工厂图/尺码表图，原来只能人工。

### P2：补齐与优化

#### 7. `set_stock` 真站验证 + 包装清单

代码已完成但没跑过真站。要验证的：
- 仓库勾选后库存列（`input[name=stock]`）才渲染这个联动
- LLM 判断的 SKU 分类值能正确写进原生 `select`

包装清单：原 skill 有 LLM 判断（`packingList`：无 / 具体配件名），需要配件时逐行选 ant-select。纯服装无配件 → 留空。

#### 8. 阶段② 采集 + 认领

原实现：`dianxiaomi_claim.py`（564 行）。

- 数据采集页「链接采集」textarea 填链接 → 点「开始采集」（**合成 click 有效**）→ 等「采集成功 1」
- 认领弹窗勾店铺：**必须真实点击，JS `.click()` 不触发 Vue**（与编辑页按钮结论相反）
- 店铺列表接口慢会超时，但弹窗通常还开着且列表随后加载完，要能续跑
- 认领可能同时认领到多站点，`find_rowid` 后**按站点名筛**（已实现的 `find_rowid` 返回全部匹配，交调用方筛）

#### 9. Grok 视觉回填阶段①

`extract.py` 产出的 `product-info.json` 里 `imageUnderstanding` / `sizeChart` / `sizeMeasurements` / `complianceNotes` 是空占位，现在可以自动填了。

看图要点（SKILL.md 阶段①）：
- 识别每颜色对应实物
- 找尺码表：平铺实测 → `sizeMeasurements`；身高体重参考 → `sizeChart`；**尺码键不带「码」字**
- 逐张标注中文/水印/logo/重复图 → `complianceNotes`

提速做法：先 md5 去重（desc 常与 main 重复），唯一图一次请求多张。

## 四、绕不开的既有坑

搬运时会撞上的，按已实测结论办，别重新试：

1. **虚拟列表滚动每屏须等 180ms**。原 skill 的 120ms 在 Playwright 直连下不够（WebBridge 的 HTTP 往返开销间接补足了等待），表现为静默只读首屏 10 条。见记忆 `dianxiaomi-virtual-list-scroll-timing`。基准：上装成分 67 项。

2. **动态属性行的必填标记在内层 `span.attr-label.required`**，`label` 的 `title` 是空的。见记忆 `dianxiaomi-attr-required-in-inner-span`。基准：33 行中 18 必填。

3. **LLM `max_tokens` 需 16000**。推理模型先产 reasoning 再产 content，额度不足时 content 返回空字符串（表现为「调用成功但结果为空」）。见记忆 `publish-llm-max-tokens-16000`。

4. **JS 常量必须用 `r"""`**。普通字符串里 `\s` 被格式化工具「修正」后，Python 转义使 JS 收到字面 `s`，正则 `[*\s]` 变成「匹配星号或字母 s」。

5. **点击方式因场景而异，不要统一**：
   - 编辑页按钮（保存等）：JS `el.click()` 有效，`mouse_click` 与 CDP `dispatchMouseEvent` 被吞
   - 认领弹窗店铺复选框：必须真实点击，JS `.click()` 不触发 Vue
   - 素材图/SKC 悬停菜单：必须 CDP 真实鼠标移动
   Playwright 下每条都要重验。

6. **幽灵浮层**：同选项列表的多个字段共享浮层池，点在隐藏浮层上事件照样生效会改错字段。一切选项查找限定在「目标行附近唯一可见浮层」内（`top > -1000 && width > 50`，按距离取最近），每步前后清幽灵。

7. **`curl.exe` 子进程不能换成 httpx/requests**：Packy 的 Cloudflare 按 TLS 指纹拦截，403 error 1010。店小秘 COS PUT 同理。

8. **隐藏页签 rAF 节流**：窗口被遮挡时浮层定位算不出来（`getBoundingClientRect` 恒 -9999）。修复是 `Emulation.setFocusEmulationEnabled` + `Page.setWebLifecycleState`，每次导航后失效需重发（已收进 `browser.navigate`）。

## 五、建议的推进顺序

```
P0-1 阶段⑫ 保存          ← 先做这个，不然前面的修改都不落库
P0-2 运输信息             ← 简单，radio + select
P2-7 set_stock 真站验证   ← 代码已完成，只差跑一次
P1   公共基础 upload_image ← 图片三件套的前提
P1-4 阶段⑥ 素材图         ← 先验证 Playwright 鼠标事件是否等效
P1-5 阶段⑦ SKC 图         ← 复用⑥验证过的机制
P1-6 阶段⑪ 描述图         ← 用 Grok 视觉判断哪些图该删
P0-3 service 层编排       ← 阶段齐了再编排
P2-8 阶段② 采集认领       ← 补全入口
P2-9 Grok 视觉回填阶段①   ← 提升整体判断质量
```

## 六、验证方式

已有的验证脚手架：
- CLI：`publish_inspect.py`，12 个子命令，逐阶段手动跑
- 真站基准：Pawly 店 / 哥伦比亚站 / 男童 POLO 衫套装
  - rowid `173539495450551101`
  - `product-info.json` 在桌面 `manus输出/商品发布/product-1073654064193/`
- 单测：`tests/test_publish_images.py`（12 项，纯 Pillow 离线）、`tests/test_publish_attrs.py`、`tests/test_publish_stock.py`
- 完整链路：`workspace/_verify_pipeline.py`（①→④→⑤→⑧→⑩→⑪ 顺序跑）

**写入类阶段验证时，改完记得改回原值**——那是真实商家草稿。前面验证 `auto_cat` 时类目被改成「男童长裤套装」，后来靠前瞻优化才回到原本的「男童休闲套装」。


## 七、⑮ 立即发布（2026-08-24 新增并真站验证）

按用户要求补上发布入口，`app/publish/pipeline.py` 的 `publish_now` +
`service` 的 `publish` 阶段 + CLI `--publish`。

**真站验证**：offer `846106032776`（女装韩版圆领刺绣针织开衫），rowid
`173539495454560681` → 已上架，平台 ID `2319138008`，店铺 Pawly / 站点哥伦比亚。

### 三条踩坑（都已写进代码注释与记忆）

1. **「发布」下拉是 hover 触发，不是 click**。`btn.click()` 后页面上一个含「立即发布」
   的节点都没有；合成 `mouseover`/`mouseenter`/`mousemove` 才渲染出 `.ant-dropdown`。

2. **菜单有入场动画，`offsetHeight` 不能当判据**。逐帧采样（hover 后 0/900/2400ms）：
   t+900ms 时 inline style 是 `opacity:0` + `transform: matrix(0,0,0,0,0,0)`，
   容器与项的 `getBoundingClientRect()` 都是 0×0，而 `offsetHeight` **已经是最终值**
   72/32。用 offsetHeight 收敛会在动画中途退出、拿到 0×0 坐标。
   判据必须用**目标菜单项的 `rect.height > 0`**。

3. **发布成功只能去列表取证**。成功 toast 抓不到（自定义实现、转瞬即逝，轮询 12s
   一条没有），页面**也不跳转**（发布后留在编辑页），两个前端信号都恒为「没有」。
   判据改为：该 rowid 从草稿箱消失 或 出现在在线产品列表（`_publish_landed`）。
   行选择器必须写 `tr[rowid="<id>"]`——rowid 数字开头，不加引号是非法选择器。

### 同轮修掉的两个既有 bug

- **⑧ 尺码勾选**：源「均码」× 成人女装英文尺码（`one-size`/`XXS`/…）匹配不上，
  `fix_sizes` 把页面原本勾着的取消完还返回 ok，⑨ 才报「请先选择尺码」。
  修法：`_SIZE_ALIASES` 别名表（均码↔one-size，**不收** Asian/Petite/Tall 版型变体）
  + **先校验再动手**（源尺码全不匹配时立刻报错，不改动任何勾选）。
- **⑦ SKC 行按钮瞄点**：残留图片菜单盖住按钮中心，且它 Escape/合成 click 都收不掉
  （`parked` 恒 0），而原实现只在 `parked>0` 时重读坐标，于是一次都没重试就失败。
  修法：按钮矩形内**多瞄点**退让（中心→左右→上下→四角内缩，每点各自过
  `elementFromPoint` 校验）+ 收浮层后**无条件重读**。
  注：此项仅有单测覆盖，真站未复现验证（该商品发布后编辑页不再渲染 SKC 行）。
