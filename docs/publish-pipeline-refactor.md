# 按来源拆分店小秘商品发布

发布流程由来源管线独立定义。`service.publish_one` 识别任务来源并分流；
`service.run_batch` 允许混合任务，但逐个商品选择对应管线，不共享商品上下文。

| 管线 | 入口模块 | 数据解释 |
| --- | --- | --- |
| 1688 商品店小秘发布 | `app.publish.workflows.alibaba1688` | 主面料成分/含量、1688 尺码清洗、童装字母码映射 |
| 拼多多商品店小秘发布 | `app.publish.workflows.pinduoduo` | 面料/材质与成分含量、占位前缀清洗、直径规格归一 |
| Temu 商品店小秘发布 | `app.publish.workflows.temu` | 保留零售源规格区间与版型；缺成分证据不回退到 1688 |
| 亚马逊商品店小秘发布 | `app.publish.workflows.amazon` | 保留现有来源支持，独立阶段表和规格解释 |

每个模块提供 `publish_one(session, task, store, **kwargs)` 和 `run_batch(tasks, **kwargs)`。
显式管线入口拒绝其他来源；自动入口按 URL、商品数据和断点来源交叉校验。
无法确定来源时停止，不能猜成 1688。

CLI 自动识别来源，也可用 `--source 1688|pdd|temu|amazon` 限定管线。
现有 Web 入口不变；阶段事件带 `source_platform` 和 `workflow_id`，日志展示管线名称。

## 模块边界

```text
service.py                 兼容入口、任务路由、共用执行器与批次生命周期
workflows/<来源>.py         独立阶段顺序、源数据解释、单品/批量入口
sources/<来源>.py           源站取数、字段语义和单位
stages/                    可由管线显式选用的店小秘发布阶段
attributes/                属性表单、联动、成分、校验与审核
sizechart/                 尺码表编辑、参数、分件与测量估算
media/                     主图、SKC、预览图、描述与视频
navigation.py / saving.py  页面探查与保存
preferences.py / state.py  偏好与断点
pipeline.py                旧函数和常量的显式兼容导出
```

`_pipeline_impl.py` 已删除，原页面操作真正迁移到小模块。
`stages/` 不依赖 service 实现，各来源 `build_stages()` 直接选择阶段函数。
修改阶段顺序、移除阶段或替换实现，只修改对应来源的阶段表；不继承其他来源阶段表，
也不隐式继承 service 的全局 `_STAGE_FUNCS`。共用店小秘交互复用同一实现，
避免复制浏览器选择器和发布确认机制。

来源属性、成分、尺码解释放在 `workflows/<来源>.py`；平台表单约束放在共用页面模块。
新来源需要同时提供取数适配器和完整发布阶段表。

## 续跑与兼容

- 状态和商品数据记录 `workflow_id`；状态额外记录 `source_platform`。
- 保持旧文件名规则：1688 用商品 ID，其他来源用平台前缀，不批量改名旧进度。
- rowid 断点若绑定其他来源，执行前失败，不覆盖原进度。
- URL 与商品数据中的来源或商品 ID 冲突时失败，即使 `from_stage` 跳过提取也校验。
- 旧数据可用来源 URL 或仅有 `offerId` 的旧 1688 元数据识别；来源不明需补齐元数据。
- `pipeline` 导入名保持有效；依赖替换应针对定义模块。历史集成测试通过
  `tests/publish_patching.py` 迁移共享绑定的替换，生产代码不增加动态代理。
- `do_publish` / `confirm=True` 确认机制保持原样。

## 验证

`tests/test_publish_workflows.py` 覆盖来源冲突、商品 ID 冲突、独立阶段执行、
混合批次拒绝、续跑串源、成分和尺码隔离。现有发布测试验证拆分后的页面操作与编排。
离线测试不执行真实店铺发布，源站/店小秘改版仍需真实页面验收。

2026-09-09 验证：发布测试 1367 通过、8 失败；失败名单与拆分前一致。
新增来源隔离测试 17 项全部通过。后续导入整理与校验调整后重点复测 145 项通过。
