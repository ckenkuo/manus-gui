# ⑤c 轮播图修复：交接现状（2026-09-18）

## 2026-09-19 18:00：普通风格误报在代码层不阻断（优先）

- 提示词仍可能被模型忽略，因此新增 `claims.is_style_only_image_claim`：
  仅普通风格词的宣称证据，或旧版完整拒因 `Cute 属情绪夸大类`，自动降为
  `nonBlockingIssues`，不再作为 `marketingClaim` 阻断，也不触发修图或人工换图。
- 新质检请求列出全部 `marketingClaimTexts`；混有 Best Seller、High-Quality、
  Guaranteed 等宣称或证据不完整时不使用此豁免。中文、乱码、水印、主体破坏仍独立判定。
- 回归测试刻意让模型每次都报用户贴出的 Cute 误报，实际轮播阶段仍返回 `ok`，
  无生图、无 `manual_check`，前后双检均走同一处理。相关 134 项测试通过。
- `resume` 中“上次未落库”的消息是自动补跑说明，不是阻断项；本次未修改断点恢复逻辑。

## 2026-09-19 17:56：普通形容词误判修正（优先于下文）

- `1051894953703` 17:09 重跑，第 27 张实际文案为 `Cute Glider Blaster`。
  “Cute 属情绪夸大”是内部视觉质检结论，不是平台拒绝；这是普通外观描述被误判。
- 上轮把 `Super Cute`、`Wonderful` 直接列入图片拒绝示例，缺少普通审美描述边界，
  已撤销这个过度收紧。生成与质检共用普通外观/风格/趣味描述边界，按完整短语和语境判断；
  不能仅凭 Cute 或感叹号拦截，也不能因出现 Cute 而豁免其他销量、品质、保证宣称。
- 同一张失败原图未改动，真实视觉质检连续三次均返回 `clean=true`；对照图
  `High-Quality Material` 仍按品质宣称拒绝。记录在
  `workspace/cute-claim-boundary-1051894953703/results.json`。相关回归测试 89 项通过。
- 双检、中文、乱码、水印和主体破坏检查保留。本轮只复测图片质检，未重跑整单或保存发布。

## 2026-09-19 同批轮播图续修

- 商品 `1051894953703` 的 `final-23/29/32.jpg` 分别实有 `Super Cute!`、
  `AIRPLAN`、`High-Quality`。它们与 `carousel-edit` 对应文件的 SHA-256 相同，
  确认是生图产物未清干净，非上传错图或页面替换错位。
- `_english_one` 原来只查缓存尺寸，生图一次后直接上传，首次质检原因未进入提示词，
  页面复检失败也不作废缓存。现改为缓存和新产物上传前均严格双检，
  带具体质检原因最多生图三次；失败产物删除，不上传、不替换原图。
  页面收尾仍按双检取严，收尾失败也删除对应的本地英化缓存。
- 续跑时未替换的已选图也改为双检；原先单次判好就加入跳检集合，曾漏过
  仍含 `High-Quality` 的旧图。图片生成与质检共用的宣传规则补齐本批明确出现的
  `High-Quality`、`Super Cute`、`Wonderful` 示例。
- 选用上限为 10 张，剩余候选信息图不构成失败。溢出改记普通日志，
  显示计划补勾数量，不再在上传前声称“当前已选满 10 张”或要求人工处理。
- 同批 `1062937493650` 的轮播图 1000×1000 被上传层默认服装尺寸门槛拒绝。
  `upload_many` 增加可选尺寸下限透传，轮播调用传入 800×800；其他用途的默认门槛不变。
- 轮播、上传、上传尺寸闸门、发布编排、描述图质检、标题宣称共 177 项回归测试通过。
- `1051894953703` 在保留编辑页完成轮播专项实测：先修复 `Super Cute!` 和
  `AIRPLAN`，补齐已选图双检后再修复 `High-Quality` 及一处数字水印。
  `High-Quality` 图第一发仍有 `Quality/Great`，上传前双检拦下，反馈重做后通过。
  最终 `_st_carousel` 返回 `ok`，选用 10 张，尺寸不合规 0 张，页面复检通过。
  记录在 `workspace/carousel-live-1051894953703-v2/result.json` 和 `run.log`。
  仅验证轮播阶段，未保存、未上架；结果落盘后的全页截图超时，不影响上述阶段结果。

## 续修更新（优先于下方历史记录）

- 问题 B 的两单不是慢加载：用户指出页面为灰色在线产品；服务端按 ID 查询确认
  `184807703149199877`、`184807703145874485` 均为
  `dxmState=online / dxmOfflineState=publishSuccess`。
- `persistence.is_online_product` 读取 `GET /api/popTemuProduct/edit.json?id=...`，
  校验返回的 `idStr` 与任务相同；不通过列表第一页推断，不按控件灰色猜状态。
  `service._run_product` 在预热、编辑及各阶段执行之前检查，在线商品直接返回
  “商品已发布，跳过编辑与重复处理”，清除旧失败阶段并记录 `published_online`。
  显式 `--from-stage` 同样经过预检；下一次仍查询实际状态，避免商品下线后永久跳过。
  查询失败或缺商品时停止本商品，不能当成草稿继续编辑。
- 已在真实会话调用 `publish_one` 验证上述两单：分别约 1.5s、1.3s 返回
  `already_published=true`，未导航、未进入编辑或生图。
  `184807703149109095` 查询为 `draft`，没有误判为在线。
- 类目等待兼容“店铺账号”和“商家账号”，识别按钮禁用；撤掉针对在线商品误加的
  加时与弹窗重开。就绪失败明确停止，不再对禁用按钮报“已打开”。
- 问题 A：`open/opened=false` 也会重瞄（最多三次），超时补齐明确 `err`。
  严格双次质检规则保留。
- 验证：service、online_precheck、carousel、cat_clues 四组共 153 项通过。
- 原四单批次实际上后来完成：1 成功、3 失败，第四单 15:16:13 已保存未发布。
  本次重新启动的旧逻辑批次已停止。第一单 `184807703146508445` 最新查询未返回
  product，不能继续当作可编辑草稿，需确认商品 ID 是否仍有效；本次未重新整批实跑。

---

给接手的 agent。本文件只讲**当前状态与未决问题**，不讲已完成工作的推导过程——
那些取证都写在各自代码的注释里了。看完这份应该能直接上手继续。

---

## 一、原始任务与完成度

用户报三个商品跑批失败，要求「把问题解决了、落实到代码、改完继续实跑」。
共查出**六处**缺陷，前五处已真站验证，第六处（收尾复检取严）**代码已改、测试已过，
但实跑验证被中断**。

### 已完成并真站验证（三单全部落库）

| 商品 | rowid | 落库时间（平台回读的更新时间） |
|---|---|---|
| 1040482047185 | 184807703149201245 | 2026-09-18 09:46:11 |
| 1005064778878 | 184807703149180683 | 2026-09-18 11:28:15 |
| 1049857947880 | 184807703149149807 | 2026-09-18 12:09:54 |

三单都未上架（跑批没给 `--publish`）。断点在 `workspace/publish-state/rowid-*.json`。

### 前五处缺陷

| # | 缺陷 | 改动位置 |
|---|---|---|
| 1 | 补勾图整批丢失：平台在**插入那一刻**按 10 张上限截断，超出的连候选列表都不进 | `stages/carousel.py` 按剩余选用位分批插入 |
| 2 | 「N 张读不出结论」误导：因位满补不下的信息图被塞进 `unknown`，还整单阻断 | 同上，单列 `overflow` 且不阻断 |
| 3 | ⑩a 尺码列填 `/` 时整阶段失败：`/` 是 ASCII 不送翻译，以空 token 进 mapping 被闸误判 | `sku_codes.py` 闸只看保留维度 |
| 4 | ⑪ 仓库死锁：续跑只看下拉值、save 还要库存列，两处口径不一致 →「请重跑⑪」永不被安排 | `navigation.py` 增 `stockHeaders`；`resume.py` 同口径 |
| 5 | ⑤c 开弹窗 `no-dropdown`：坐标「JS 读一次→另发 CDP 点击」两次往返之间页面重渲染就打偏 | `media/carousel.py` 重瞄重试（最多 3 次） |

### 第六处：收尾复检判定抖动（代码已改，**未实跑验证**）

**取证**：商品 1049857947880 的收尾复检报「2 张未换成合规图」，用同一批文件复测
`vision.check_cleaned`，结论与跑批时**相反**：

| 图 | 跑批时 | 复测 | 实际画面 |
|---|---|---|---|
| final-34 | 通过 | 不通过（`Citizens后出现缺字乱码方块`） | 确有残留方块字 `Citizens囚` |
| final-35 | 不通过 | 不通过（`底部残留数字水印串`） | 确有 1688 水印+电话号 |
| final-36 | 不通过 | **通过**（clean） | 干净、全英文、2048² |

即误报一次（好图被拦致整单停摆）、真问题两次、其中一次被漏过。

**当前实现（用户明确要求的方向）**：问两次，**任一次判坏就算坏**，只有两次都
`clean is True` 才放行。取严的理由是不对称——误报的代价是整单停摆交人工（人工一看
就放行），漏报的代价是带中文的图**发上真店**（Temu 硬红线，后面没有第二道闸）。

代码在 `stages/carousel.py` 收尾复核段，日志会打「取严的一侧」。

**注意方向变更过**：我最初实现的是反向（两次都说坏才算坏，为了消误报），用户看过
取证后要求改成取严。如果接手时看到测试名是 `test_final_qc_flaky_pass_is_blocked` /
`test_final_qc_needs_two_clean_verdicts`，那是取严版的；原先的
`test_final_qc_false_negative_is_rechecked` 已被替换掉。

**代价**：每张待复检图多一次质检调用（约 2 秒），且误报停摆会变多。这正是要实跑看的
东西——**还没验证过**。

---

## 二、最后一次跑批的结果（4 单，被中断）

命令：`python publish_run.py --tasks workspace/_batch_strict_qc.json --source 1688`
日志：`workspace/_batch_strict_qc.log`（193KB，15:09 后被中断）

选的 4 单是「1688、未落库、info 文件仍在」的历史失败品：

| 序 | rowid | 结果 |
|---|---|---|
| 1 | 184807703146508445 | × 卡在 ⑤c：`打开选图弹窗失败[open]：`（err 为空） |
| 2 | 184807703149199877 | × 卡在 ③：`异常：选择类目弹窗未就绪` |
| 3 | 184807703145874485 | × 卡在 ③：`异常：选择类目弹窗未就绪` |
| 4 | 184807703149109095 | 跑到 ⑤c 中途被中断（仍在生图英化） |

**三单失败里没有一处是本次六项修复导致的**，但暴露了两个新问题，见下节。

---

## 三、两个新暴露的问题（未处理，等接手）

### 问题 A：`打开选图弹窗失败[open]：`（err 为空）——修复 5 的覆盖缺口

**现象**：`media/carousel.py` 的 `open_carousel_space` 返回
`{"stage": "open", "opened": false}`，而阶段侧取的 `opened.get('err')` 不存在，
于是失败信息拼成空的。

**根因（确定）**：我加的重瞄重试只对 `no-dropdown` 生效：

```python
# media/carousel.py 约 385-388 行
if r.get("err") != "no-dropdown":
    return r          # ← open 分支没有 err 键，get 返回 None，这里就 return 了
```

`open` 分支（菜单建起来了、「空间图片」也点了，但弹窗 6s 内没出现）被直接放行、
不重试。我写那行注释时的理由是「那边自己轮询过 6s，再点只会把菜单收掉」——
**这个理由是错的**：重试是从「重新读按钮坐标 + 重新点按钮」开始的，菜单会被重建，
不存在「把菜单收掉」的问题。所以 `open` 分支也该重试。

**建议改法**：把重试判据从「仅 no-dropdown」放宽到「未 opened 且不是 CDP 点击失败」，
即 `locate`/`click` 这种硬失败不重试，`no-dropdown` 与 `open`（含空 err）都重试。

**待确认**：这次是页面慢（产品 1 那次在 ⑤c 里直传了 6 张英化产物，页面正重渲染）还是
真有别的阻塞。重跑时留意 ⑥ 素材图是否抢了同一个弹窗。

### 问题 B：`异常：选择类目弹窗未就绪` ×2 —— 疑似页面没加载完就继续

**现象**：产品 2、3 都在 ③ 产品类目失败，`category.py:738` 抛出。同一日志里两者都先有：

```
WARNING ... 等商家账号回填超时（no-shop-row），实际表单字段：
  ['店铺账号', '经营站点', '产品分类', '产品属性']     ← 只有 4 个字段
```

**根因（高度可疑，未确证）**：`category.py` 等「商家账号」回填的上限是 8s，超时后
**刻意不抛**、只记 warning 就继续（那条分支的注释写明了这个取舍：让后面的弹窗报错
带出更有用的诊断）。产品 2、3 的字段列表只有 4 项（正常 7~10 项），说明页面数据
根本还没回填完；随后 `_JS_OPEN_CAT_MODAL` 报 opened=true，但类目列 15s 内没就绪。

也就是：**8s 超时后继续，撞上了「弹窗能开、列加载不出来」**。
产品 1（字段 7 项）和产品 4（字段 10 项）就没这个问题——它们的页面回填得多一些。

**待确认**：这是页面慢，还是产品 1 失败留下的残留态（产品 1 那单 park 了编辑页签、
另开了工作页）。两者都可能，需要看重跑时是否复现。

**可能的修法**（择一，先定因再改）：
- 等商家账号的超时从 8s 放宽（但要先量准回填到底要多久，别又拍一个估值）；
- 或者超时后不继续，直接 fail 交人工——但这与那段注释的既有取舍相反，要慎重；
- 或者 `选择类目弹窗未就绪` 时重试一次（先 `kill_stuck_modals` + 重开弹窗）。

---

## 四、工作区文件清单（**重要**）

**本会话改的文件**（8 个）：

```
app/publish/stages/carousel.py
app/publish/media/carousel.py
app/publish/sku_codes.py
app/publish/navigation.py
app/publish/stages/resume.py
tests/test_publish_carousel.py
tests/test_publish_skucode.py
tests/test_publish_service.py
```

**另一个 session 或后续工作改的文件**（不要当成本次改动，也别顺手回退）：

```
app/publish/images.py
app/publish/vision.py
app/publish/video.py
app/publish/stages/cleaning.py
app/publish/stages/preview.py
app/publish/stages/skc.py
app/publish/stages/video.py          ← 看起来是在做 docs/publish-video-stage-spec.md
tests/test_publish_desc_qc_cjk.py
tests/test_publish_image_edit_channel.py
tests/test_publish_preview_empty_slot.py
tests/test_publish_skc_usable.py
tests/test_publish_video.py
tests/test_publish_video_stage.py
```

**提交时务必逐个 `git add` 指定文件，不要 `git add .`**（项目规矩 + 工作区确实混着
两拨改动）。

`app/publish/video.py` 与 `stages/video.py` 的当前内容已经**包含**视频 spec 里的部分
修复（note 文案分两种说法、失败文案用 `norm.get("action")` 判而不是 parse 文案）。
所以那份 spec 可能已被部分执行——接手前先确认。

---

## 五、测试状态

```
pytest tests/test_publish_carousel.py tests/test_publish_skucode.py -q
→ 63 passed
```

全量（排除 `tests/sandbox`，那里面有 18 项要 docker、与本次无关）：

```
pytest tests/ -q --ignore=tests/sandbox
→ 2169 passed, 1 skipped     （取严版改动之前跑的，改完只重跑了上面两个文件）
```

**取严版改完后还没跑过全量**，接手时建议先跑一遍确认无回归。

新增/改动的测试断言：

- `test_final_qc_flaky_pass_is_blocked` — 首次判好、复问判坏 → 拦下
- `test_final_qc_needs_two_clean_verdicts` — 两次都判好才放行，且确实问满两次
- `test_defaults_and_all_information_images_pass_qc` — 断言从「5 次复检」改成「10 次」
  （5 张图 × 2 次），这是取严版预期的翻倍
- `test_batched_insert_keeps_all_adds_under_platform_cap` — 桩按真站行为截断后仍一张不丢
- `test_open_space_retries_when_menu_never_opens` — 三种路径（重试成功/三次都败/不误重试）
- `test_要丢的维度是纯符号时不判失败` + `test_保留维度里的纯符号词仍判失败`
- `test_stale判定_仓库有值但库存列没生成也要重跑库存`

---

## 六、怎么继续实跑

前置：Chrome 要带 `--remote-debugging-port=9222` 起（`curl -s http://127.0.0.1:9222/json/version`
验证）。没有它管线会在建 session 时就中止，`ok:0 fail:0`。

**不要加 `--from-stage`**：它会跳过续跑的实况判定（`service.py:558` 明写「显式指定起点
是人工判断，不该被实况覆盖」）。加了的话 ⑤ 不跑、英文标题会留着草稿里的中文源标题，
⑭ 落库被平台拒「标题中含有中文字符」——那不是缺陷，是用法错了（本会话踩过一次）。

用行任务清单跑，让续跑判定按页面实况自己决定阶段：

```powershell
python publish_run.py --tasks workspace/_batch_strict_qc.json --source 1688
```

`workspace/_batch_strict_qc.json` 里是上面那 4 单。可复现的现场：

- 商品 1049857947880（本轮取严改动的直接验证对象，它上次正是栽在复检误报上）
- `workspace/_batch_strict_qc.log` 是中断那次的日志

真站单商品调试用：

```powershell
python publish_run.py --rowid <rowid> --info <product-info.json> --source 1688
```

不加 `--publish` 只跑到 save，不会真上架。

---

## 七、未决的产品判断（需要用户拍板）

1. **取严 vs 取宽的取舍**。取严会拦住 `Citizens囚` 这类单字符残留（好），但误报停摆
   会变多（坏）。用户已选取严，但实跑数据还没有——**如果误报率高到影响产能，可能要
   回头讨论**。判断依据应该是跑一批后的「停摆单数 / 总单数」。

2. **问题 B 是不是级联污染**。如果产品 1 失败会污染后续商品的页面状态，那批处理的
   隔离性就有问题，值得单独修。

3. **视频链路**另有 spec：`docs/publish-video-stage-spec.md`（本会话写的，三个问题都
   附了取证；注意其中「问题三 每次续跑都重跑」是**刻意设计**、不要改）。

---

## 八、相关记忆

`C:\Users\Administrator\.claude\projects\c--Users-Administrator-Desktop-manus-gui\memory\`

- `publish-vision-qc-flaky-recheck.md` — 质检抖动取证（含「别拿预览缩放尺寸当文件
  尺寸」的排查教训，本会话在这上面误判过一次）
- `publish-resume-save-criteria-parity.md` — 续跑与 save 判据必须同口径
- `publish-skucode-dropped-dim-gate.md` — 货号闸 + 「验证落库别加 --from-stage」
- `carousel-info-add-pending-live-test.md` — ⑤c 首跑实测（新图自动勾选、上限在插入时截断）
- `parallel-sessions-same-repo.md` — 多 session 并行时先判对方的中途态
