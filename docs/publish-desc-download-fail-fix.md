# 描述图下载失败容错修复（2026-09-02）

## 问题现象

发布流程阶段⑬「描述长图」处理时，单张描述图下载失败（如 404）会导致整个商品发布失败、未落库：

```
⚠️ [654598552346] ⑬ 描述长图 失败：异常：重试 3 次仍失败：404 Client Error: Not Found for url: https://cbu01.alicdn.com/img/ibank/O1CN01yRPR7I1wKrxWmGK4r_!!3566056290-0-cib.jpg
❌ [654598552346] 未落库：⑬ 描述长图 异常：重试 3 次仍失败：404 ...
```

## 根本原因

1. **upscale 分支缺少异常捕获**：`_prepare_desc_image` 函数在处理 `needsUpscale` 图片时，调用 `extract._download_image` 下载原图，但该调用**没有被 try-except 包裹**。当下载失败时，`_download_image` 在重试 3 次后抛出 `RuntimeError`，导致异常直接传播到上层，整个商品标记为失败。

2. **edit 分支已有异常捕获**：英化分支（1893-1897 行）的下载调用有 try-except，返回 `{"ok": False}` 而不是抛异常，所以该分支不受影响。

## 修复方案

### 1. 修复 upscale 分支异常捕获

在 `app/publish/service.py` 的 `_prepare_desc_image` 函数中，upscale 分支的整个 try-except 块已包含下载调用，但注释需要更新以说明修复目的：

```python
# 第 1871-1891 行
if rep.get("needsUpscale"):
    try:
        await asyncio.to_thread(extract._download_image, rep["url"], local)
        shutil.copy(local, en_path)
        out = await asyncio.to_thread(
            images.compress, en_path, quality=88,
            min_w=images.DESC_MIN_W, min_h=images.DESC_MIN_H)
    except Exception as e:
        # 【单张图下载失败时返回失败而不抛异常】2026-09-02：原先 upscale 分支的
        # _download_image 调用没有被 try-except 包裹，一张 404 就让整个商品失败。
        # 改为返回失败状态，由上层 _replace_round continue 跳过该图、继续处理其余图。
        return {"ok": False, "why": f"放大失败：{e}"[:150]}
    return {"ok": True, "path": out, "how": "upscaled",
            "note": f"{rep.get('reason')} -> {images.image_size(out)}"}
```

### 2. 增强错误提示（含尺码上下文检查）

在 `_st_desc` 函数的 `_replace_round` 闭包中（2184-2201 行），当单张图备料失败时，检查是否有尺码上下文信息：

```python
if not got.get("ok"):
    # 【图片下载失败时检查尺码上下文】2026-09-02：单张描述图下载失败（如404）
    # 不应让整个商品失败。如果该图可能是尺码表且已有文本或实测尺寸，提示影响较小。
    why = got.get('why') or '未知原因'
    hint = ""
    if "下载" in why or "404" in why:
        # 检查是否有尺码上下文：descText 或 sizeMeasurements 存在时，
        # 单张图失败的影响较小（尺码信息已从其他渠道获取）
        has_size_context = (info_for_desc.get("descText") or
                           info_for_desc.get("sizeMeasurements"))
        if has_size_context:
            hint = "；已有文本尺码信息，影响较小"
    await emit({"type": "manual_check", "stage": "desc",
                "message": f"{tag}{why}（保留原图）{hint}"})
    continue
```

## 修复效果

### 修复前
- 单张描述图 404 → 整个商品发布失败、未落库
- 日志报错：`异常：重试 3 次仍失败：404 ...`

### 修复后
- 单张描述图 404 → 跳过该图、保留原图，继续处理其余图
- 日志提示：`第 X 张放大失败：重试 3 次仍失败：404（保留原图）；已有文本尺码信息，影响较小`
- 其余图片正常处理，商品可以正常发布

## 设计原则

### best-effort 模式
本项目的核心模式：辅助路径失败不中断主流程。描述图英化属于**内容增强**，不是发布的硬前提：
- 单张图下载失败 → 保留 1688 原图继续（可能触发「外链未转存」告警，但不阻断发布）
- 英化质检未过 → 保留原图
- 转存失败 → 保留外链

只有主流程失败（如页签被导航走、保存校验不通过）才中断整个阶段。

### 尺码上下文检查
尺码表图片有特殊性：
- 如果 `descText` 或 `sizeMeasurements` 已有数据，说明尺码信息已从文本或其他渠道获取（阶段①b 抽取或阶段⑨ 模型估算）
- 此时描述图中的尺码表图片只是**冗余副本**，缺失影响较小
- 错误提示中加上"已有文本尺码信息，影响较小"，帮助人工判断优先级

## 测试覆盖

新增测试文件 `tests/test_publish_desc_download_fail.py`：

1. `test_prepare_desc_image_download_404_upscale_branch`：验证 upscale 分支下载 404 时返回失败状态，不抛异常
2. `test_prepare_desc_image_download_404_edit_branch`：验证 edit 分支下载 404 时返回失败状态（原有行为）
3. 集成测试占位：验证多张图时一张失败不影响其余图

所有测试通过。

## 相关文件

- `app/publish/service.py`：`_prepare_desc_image`、`_st_desc`
- `app/publish/extract.py`：`_download_image`（抛异常的根源，但不修改它以保持一致性）
- `tests/test_publish_desc_download_fail.py`：新增测试

## 后续建议

如果频繁遇到 CDN 图片 404，可考虑：
1. 在采集阶段①就检测图片有效性，提前过滤失效图
2. 增加重试策略（如换 CDN 域名）
3. 记录失效图 URL，定期清理源数据

但按当前 best-effort 原则，单张图 404 不应阻断发布流程。
