# CLAUDE.md — manus-gui 项目规则

本文件是给 Claude Code 的项目级指令。约定优先级高于默认行为，请严格遵守。

## 项目定位

manus-gui 是 [OpenManus](https://github.com/FoundationAgents/OpenManus) 的个人衍生项目（MIT），在完整继承 agent 栈之上新增：

1. **视觉定位 `gui_action` + 反爬浏览器接管**（CDP 接管已登录的真实 Chrome）
2. **RAG 经验库**（成功流程提炼成 recipe，按相似度检索并作 few-shot 注入）
3. **批量采集管道**（Temu → 1688 选品比价，确定性批处理 + UI/CLI 共用 service 层）

改动只集中在上述三块，其余（工具集、MCP、run-flow、沙箱、搜索）尽量原样跟随上游。

## 技术栈与环境

- Python **3.12**，Pydantic v2，FastAPI + uvicorn，Playwright / browser-use，loguru
- 平台 **Windows**，shell 默认 **PowerShell**；PowerShell 与 Bash 语法不同，注意区分
- 依赖锁在 [requirements.txt](requirements.txt)，版本已 pin，新增依赖也要 pin 且优先用已在库里的包
- 配置读 `config/config.toml`（**gitignored，含密钥，勿提交/勿回显密钥值**），改配置示例改 [config/config.example.toml](config/config.example.toml)

## 常用命令

```powershell
python main.py                                   # 交互式 agent
python main.py --prompt "任务" --max-steps 40    # 非交互，复杂任务调高步数
python app.py                                    # FastAPI Web（agent + 独立采集页）
python batch_collect.py                          # 批量采集 CLI
python run_mcp.py                                # MCP 工具版
pytest tests/test_experience_*.py -v             # 经验库单测（离线，已 mock）
```

改代码后按 [verification 指南](#验证) 跑对应验证，不要只改不验。

## 工作方式（务必遵守）

- **只聚焦当前明确提出的功能或问题**：之前已完成且行为正确的功能尽量不动；除非该修改与当前任务直接相关，或用户明确要求一并调整。
- **增量修改，先理解再改**：要改某个函数的实现，必须先读懂原函数已有逻辑，在原有实现基础上做增量修改，保留原先正确逻辑，不要无故移除已有的处理分支。
- **默认不写 fallback**：编写实现代码时默认不考虑 fallback 方案；只有用户明确要求时，才补充 fallback 相关设计或代码。（注意：这与本项目辅助路径的 best-effort 吞异常不冲突——best-effort 是既定模式，fallback 指另造备用实现分支。）
- **中文编码守则**：生成或补充的代码注释必须用中文；文件一律以 **UTF-8** 保存。生成、修改或落盘中文内容后，必须检查是否存在中文乱码，发现乱码立即修正后再结束任务。
- **全程中文**：思考过程和最终结论都用中文输出。
- **不用 emoji**：生成的代码、注释、文档和提交内容中一律不用 emoji。
- **善用 subagent 控制上下文**：任务面板拆出多个子任务时，主 agent 应判断哪些可交给 subagent 完成（如广搜代码、批量读文件、独立子任务），避免主 agent 上下文过长。
- **不重复造轮子**：改或扩展现有功能时，先理解之前的实现逻辑（设计意图、已考虑的边界情况）；项目已有记忆库（memory/*.md），可结合项目搜索工具发现已解决过的问题和踩过的坑，避免重复发明轮子。
- **代码迁移用 copy 再改**：迁移或复制代码时，先 copy 完整逻辑到新位置，再在新位置上改写；不要边读边从头手写一遍（容易遗漏分支或引入新错误）。
- **默认不写测试与项目说明文档**：用户未明确要求时，不生成测试脚本，不写专门的项目说明 Markdown；只在用户明确要求时提供测试或文档。
- **不盲目折中方案**：折中方案往往意味着方案尚未理清；首选思路清晰、合理的单一方案，不过度优化、不预留不必要的可扩展点。

## 代码约定

- **注释和 docstring 用中文，重在讲「为什么」**：跟随现有风格——模块/函数开头用中文 docstring 解释设计动机、踩过的坑、为什么不用另一种做法（见 [app/collect/pipeline.py](app/collect/pipeline.py)、[app/collect/service.py](app/collect/service.py) 开头）。不要写「这个函数做 X」这类复述代码的废话注释。
- **回复、写文档也用中文。**
- **工具遵循 `BaseTool` 模式**：继承 [app/tool/base.py](app/tool/base.py) 的 `BaseTool`（Pydantic + async `execute`），用 `ToolResult` / `success_response` / `fail_response` 返回，不要另造返回结构。
- **日志统一 `from app.logger import logger`**（loguru），不要 `print`、不要自建 logger。
- **best-effort 是本项目的核心模式**：偏好持久化、经验检索、输出目录创建、进度回调等辅助路径，异常一律 `logger.warning` 吞掉、绝不中断主流程（见 service.py 的 `_emit`、config.py 的 `get_output_dir`）。判重/判价等主流程失败才抛异常交由重试兜底。新增辅助逻辑时沿用这个「坏了不影响主流程」的取向。
- 相对日期换成绝对日期再写进注释/文档/记忆。

## 架构要点（改前先读对应文件）

| 区域 | 入口 |
|------|------|
| 视觉 `gui_action`、键名/坐标换算、去高亮 | [app/tool/browser_use_tool.py](app/tool/browser_use_tool.py), [app/tool/gui_agent.py](app/tool/gui_agent.py) |
| RAG 经验库（embedding/store/retriever/recorder） | [app/experience/](app/experience/)；注入在 [app/agent/manus.py](app/agent/manus.py)，落库在 [main.py](main.py) |
| 采集 service 层（UI/CLI 共用，结构化进度事件走 SSE） | [app/collect/service.py](app/collect/service.py) |
| 确定性采集管道（单商品 2 次 LLM，其余全脚本） | [app/collect/pipeline.py](app/collect/pipeline.py) |
| WPS Excel 读写 | [app/tool/wps_excel_tool.py](app/tool/wps_excel_tool.py) |
| Web 接口 / 前端 | [app.py](app.py), [templates/](templates/), [static/](static/) |
| 配置 schema | [app/config.py](app/config.py) |

**采集管道刻意不封成 tool、不上 LangGraph**：它是确定性批处理作业（for 每个未入库商品：确定性步骤 + 2 次单发 LLM），不是 agent function-calling。改采集逻辑时别把它 agent 化。

## 已知陷阱（务必遵守）

- **WPS DISPIMG Excel 别用 openpyxl `save`**：商品成本核算表用 WPS 嵌入图，openpyxl 保存会毁图，必须走 zip/XML 直接改。
- **主图下载必须带浏览器头 + 重试**：裸 `requests` 会被 CDN 拦（连接重置/403）。
- **按 Sheet 真实表头写入/判重**：各 Sheet 列序不同，批次开始解析一次 `SheetSchema` 全批复用，**绝不硬编码列号**。
- **1688 选择器已实测**：结果卡片 `.search-offer-wrapper`（连字符）、详情价 `.module-od-main-price`、运费 `.module-od-shipping-services`、重量 `.module-od-product-pack-info`；改前先确认页面结构没变。
- **材积重量硬校验**：平台要求 长×宽×高÷6 <= 实际重量(g)，宠物服装等轻薄品类易触发。阶段⑩已自动上调重量到材积重（超30kg才压缩尺寸）。
- **标题生成禁主观营销词**：Temu 明确禁用 Best/Perfect/Must Have/Essential/Top/#1/Amazing 等主观化/绝对化表述，中文禁「好物/神器/必备/最好/第一/完美/极致/顶级」。提示词只建议客观描述（材质/数量/功能/场景），`_has_subjective_claim` 闸门拦截两次生成都违规的直接报错。严禁价格/优惠（Under $X/Cheap/Sale/Discount/Free Shipping）、年份时效词（2025/New Arrival/Latest）。
- **Temu「区域」≠ 店铺，区域就是域名**：顶栏「全球/美国/欧区」切换换的是**域名**（全球 `agentseller.temu.com`、美国 `agentseller-us.temu.com`），`mallid` 与 `region` cookie 跨区域**完全不变**（`region=211` 只是碰巧与美国站订单前缀同码）。故店铺键必须带区域（`mallid@host`），页面 URL 一律运行时拼域名、**不留写死全球域的 URL 常量**。切区域**不能点顶栏标签**（业务页上非当前区域全带 `disabled` 类，合成与真实点击都不跳），只能换域名导航 + 等顶栏渲染后复核。只有全球/美国域名经实测，其它区域不许臆造。详见 [app/temu_region.py](app/temu_region.py)。
- **店名读 `window.__USER_INFO__`，不是 `rawData`**：本版后台**没有** `window.rawData`（原先「rawData 优先」形同虚设，一路掉到顶栏启发式，靠排除词表挡「查看使用教程」这类按钮，文案一改就误命中）。可靠挂载点是 `window.__USER_INFO__.shopList[].malInfoList[]`（`{mallId: 数字, mallName}`）——注意平台把 mall 拼成 **`mal`**，且 `mallId` 是**数字**，与 mallid 字符串比对前必须 `String()` 归一。
- **视觉模型 `qwen3.7-plus` 是多模态**（能看图），坐标按 `css = px / dpr` 换算，截图前先 `remove_highlights()`。
- 生成物（Excel 备份、图片、截图）统一落到桌面 `manus输出/` 分类目录，不要堆在桌面根。

## Git

- 已在 feature 分支上，PR 目标一般是 `main`；不要直接推 main，push 新分支用 `-u`。
- 只在用户明确要求时提交；暂存具体文件而非 `git add .`；提交前排查 `.env`/`config.toml` 等含密钥文件。
