# 验证 RAG 经验库是否生效

本文给出从「环境自检」到「真实端到端」的分层验证步骤，确认 RAG 经验库
（成功流程沉淀 + few-shot 检索注入）确实在工作。

代码位于 [app/experience/](../app/experience/)，集成点有两处：

- **检索注入**：[app/agent/manus.py:150](../app/agent/manus.py#L150) 的 `run()` —— 开跑前用用户提示检索相似经验，追加进 `system_prompt`。
- **沉淀落库**：[main.py:24](../main.py#L24) 的 `_maybe_save_experience()` —— 任务 `terminate(success)` 后交互确认 `[y/N]` 存库。

数据真相源：`<项目根>/experience/recipes.jsonl`（人类可读，可 `git diff`）。

---

## 一、前置自检（30 秒）

先确认特性已开 + 可选依赖齐全，否则后面都是白验。

### 1. 配置已启用

[config/config.toml](../config/config.toml) 须有：

```toml
[experience]
enabled = true
embedding_model = "text-embedding-v4"   # 建库与查询必须用同一模型
top_k = 2
min_score = 0.35
rrf_k = 60
```

`[llm.default]` 的 `api_key` / `base_url` 必须有效（embedding 走的就是这套 DashScope 兼容端点，见 [embedding.py:19](../app/experience/embedding.py#L19)）。

### 2. 依赖齐全

```powershell
python -c "import faiss, rank_bm25, jieba, openai; print('deps OK')"
```

> 缺 `faiss` → dense 检索回退 numpy 点积（仍能用）；
> 缺 `jieba`/`rank_bm25` → BM25 sparse 通路禁用（仅剩 dense）。
> 两路全废才会检索不到。这是有意的 best-effort 降级，不会报错中断。

### 3. 单元测试全绿

```powershell
pytest tests/test_experience_store.py tests/test_experience_retriever.py tests/test_experience_recorder.py -v
```

全过说明 CRUD / 去重 / RRF 融合 / 相关性闸门 / 提炼解析的纯逻辑正确（不烧 token，已 mock embedding 与 LLM）。

---

## 二、离线冒烟验证（不烧 token，验检索逻辑）

跳过 LLM 提炼，直接手塞一条带「假向量」的经验进库，再用相近 / 不相近的 query 检索，
观察「相关命中、无关被闸门挡掉」。这验证的是 store + retriever 全链路，但不调用真实 embedding。

把下面存成 `scripts/smoke_experience.py` 后运行 `python scripts/smoke_experience.py`：

```python
"""离线冒烟：不调用真实 embedding，手造向量验证检索链路。"""
import asyncio
from pathlib import Path

from app.experience.store import Recipe, RecipeStore


async def main():
    # 用临时库，别污染真实 experience/recipes.jsonl
    store = RecipeStore(Path("experience/_smoke.jsonl"))

    # 三维玩具向量即可验证余弦排序：query 与 A 同向、与 B 正交
    await store.add(Recipe(task="查上海到北京的机票", steps=["1. 打开携程", "2. 填出发到达城市"],
                           result_summary="查到票价", embedding=[1.0, 0.0, 0.0]))
    await store.add(Recipe(task="给朋友写一封生日邮件", steps=["1. 打开邮箱", "2. 写正文"],
                           result_summary="已发送", embedding=[0.0, 1.0, 0.0]))

    # 与「机票」那条同向(余弦≈1) → 应命中；与「邮件」正交(余弦≈0) → 被 min_score 闸掉
    hits = store.dense_search([1.0, 0.0, 0.0], top_n=5)
    print("dense 排序(下标, 余弦):", [(i, round(s, 3)) for i, s in hits])
    print("BM25 命中(下标, 分数):", store.sparse_search("上海 北京 机票", top_n=5))

    Path("experience/_smoke.jsonl").unlink(missing_ok=True)  # 清理临时库


asyncio.run(main())
```

**预期输出**：

```
dense 排序(下标, 余弦): [(0, 1.0), (1, 0.0)]
BM25 命中(下标, 分数): []
```

- dense 第一名是下标 0（机票，余弦≈1.0），邮件那条余弦≈0.0 排后面 —— 这是要验的核心，说明向量排序与降级链路正常。
- BM25 返回空是**这个 2 条玩具库的正常现象**，不是 bug：`BM25Okapi` 的 IDF 在语料极小时会退化（某词只在 1/2 文档出现时 `IDF = log((2-1+0.5)/(1+0.5)) = 0`），得分为 0 被 `scores>0` 过滤。真实库条目多了 BM25 才有区分度。要在冒烟里看到 BM25 正分，可多塞几条不含「机票」的经验把语料撑大。

---

## 三、真实端到端验证（验注入 + 落库闭环）

这一步用真实 embedding 与真实 agent 跑，分两段看日志。

### 步骤 1：先沉淀一条经验

经验库初始为空（无 `experience/recipes.jsonl`），必须先跑通一个任务并确认保存。
**在交互式终端**（非 `--prompt` 脚本，否则会跳过保存）运行：

```powershell
python main.py
# 输入一个能 terminate(success) 的简单任务，例如：
# 请输入你的提示: 打开 example.com 并告诉我页面标题
```

任务成功结束后，终端应打印候选经验预览并询问：

```
============================================================
📝 本次任务已成功，提炼出以下经验（确认后存入经验库）：
任务：...
步骤：
  1. ...
...
保存这条经验吗？[y/N]:
```

输入 `y`。看到日志 `✅ 经验已保存` 即落库成功。

**验证落库**：

```powershell
Get-Content experience/recipes.jsonl
```

应有一行 JSON，含 `task` / `steps` / `tips` / 非空 `embedding` 数组。

> 若没出现询问：检查任务是否真的走了 `terminate` 且 status=success（中途报错/超 max_steps 不算）；
> 以及是否在真正的 TTY 终端里跑（IDE 内嵌终端有时 `stdin.isatty()` 为 False，会自动跳过保存）。

### 步骤 2：再跑一个相似任务，看是否注入

库里有经验后，跑一个**与已存经验相似**的新任务，观察注入日志：

```powershell
python main.py
# 请输入你的提示: 打开 example.org 看下首页标题是什么
```

开跑时日志应出现：

```
经验检索：候选 N 条，过闸门后注入 M 条
🧠 已注入 M 条历史成功经验作为参考
```

看到 `🧠 已注入 M 条` 就证明 **检索 → few-shot 注入链路生效**。

**反向验证（确认闸门有效，不是无脑注入）**：再跑一个**完全无关**的任务
（如「帮我算 123×456」），日志里 `🧠 已注入` 应**不出现**（或注入 0 条）——
说明 `min_score` 相关性闸门挡住了无关经验，没有硬塞。

### 步骤 3：关闭开关回归（确认特性可干净下线）

把 [config/config.toml](../config/config.toml) 的开关关掉：

```toml
[experience]
enabled = false
```

再跑**任意**任务（哪怕和库里经验高度相似）。预期：

- 开跑时**不**出现 `🧠 已注入 ... 历史成功经验`、也**不**出现 `经验检索：...` —— 注入通路被 `is_enabled()` 短路（见 [manus.py:170](../app/agent/manus.py#L170)）。
- 任务成功后**不**弹 `[y/N]` 保存询问 —— 落库入口同样被 `is_enabled()` 短路（见 [main.py:35](../main.py#L35)）。
- 全程无报错，行为与未加该特性时一致。

这条证明 RAG 是**可配置、可干净下线**的旁路增益，关掉后对主流程零侵入。验证完记得按需把 `enabled` 改回 `true`。

---

## 四、判定标准速查

| 现象 | 结论 |
|------|------|
| `pytest` 三个测试文件全绿 | 核心逻辑正确 |
| 冒烟脚本：相关条目余弦≈1 排第一，无关条目排后 | 检索/排序/降级链路正常 |
| 任务成功后出现 `[y/N]` 询问，`y` 后有 `✅ 经验已保存` | 沉淀落库闭环生效 |
| `experience/recipes.jsonl` 出现含非空 `embedding` 的行 | 向量化 + 持久化生效 |
| 相似任务开跑时日志有 `🧠 已注入 M 条` | 检索注入生效（RAG 真正起作用） |
| 无关任务开跑时**不**注入 | 相关性闸门有效，非无脑注入 |
| `enabled = false` 后既不注入也不弹保存询问、无报错 | 特性可配置、可干净下线，对主流程零侵入 |

七条全中即 RAG 经验库端到端生效且可干净下线。

---

## 五、常见排查

- **`未找到可用的 embedding 接入配置`**：`[llm.default]` 的 `api_key`/`base_url` 缺失或无效。
- **检索永远注入 0 条**：库为空（先做步骤 1 沉淀）；或 `min_score` 设太高把都挡了，可临时调低到 `0.2` 观察。
- **`embedding 维度不一致`**：中途换过 `embedding_model`，新旧向量维度冲突。删掉 `experience/recipes.jsonl` 用统一模型重建。
- **想手动增删经验**：直接编辑 `experience/recipes.jsonl`，每行一个 JSON 对象，下次启动自动重建索引（见 [store.py:163](../app/experience/store.py#L163) 的 `load()`）。
- **非交互运行不保存**：`--prompt` 脚本或非 TTY 终端会跳过保存（设计如此）。要沉淀经验必须交互式跑。
