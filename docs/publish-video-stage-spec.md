# ⑬b 产品视频：已知问题与修复方案

面向接手这一阶段的开发者。2026-09-18 跑批（1688 毛绒玩具三单）暴露的三个问题，
以下每条都附实测取证，按「先确认再改」的顺序排列。

**2026-09-19 状态**：问题一、问题二已按下述方案改完并补了离线单测
（`tests/test_publish_video.py`、`tests/test_publish_video_stage.py`，
视频四个测试文件共 94 项全过；发布全量 1522 项全过）。真站单商品验证尚未跑。
问题三经复核确认是刻意设计，未改动。

涉及文件：
- [app/publish/stages/video.py](../app/publish/stages/video.py) — 阶段编排（106 行）
- [app/publish/video.py](../app/publish/video.py) — 下载/探测/转码（410 行）
- [app/publish/media/video.py](../app/publish/media/video.py) — 页面操作与直传（488 行）
- [app/publish/stages/resume.py](../app/publish/stages/resume.py) — 续跑判定

## 背景：这个阶段为什么存在

视频不是我们传的，是阶段② 认领时平台从 1688 连带搬来的。1688 商品视频绝大多数是
9:16 竖屏，而 Temu 只收 1:1 / 3:4 / 16:9，发布时会被打回
`Video ratio should be 1:1 or 3:4 or 16:9`——**而这个报错出现在阶段⑮ 发布之后**：
前 14 个阶段全绿、save 也落库了，最后一步才被弹回，回执还不说是哪个视频。
故必须在发布前把关。

该阶段是**纯增益路径、从不 fail**：下载/转码/上传任一步失败都报 manual_check 但仍返回
ok，让流程走到 save。这条取向是刻意的（见 `_st_video` docstring），下面的修复**不要**
改动它——视频只是加分项，为它让整单 fail 得不偿失。

---

## 问题一：note 显示 `None×None，NoneMB`（文案缺陷，优先修）

### 取证

三单的阶段结论都长这样：

```
√ (13)b 产品视频 (11.0s) | 720×720（1.0）→ None×None（1:1），NoneMB
√ (13)b 产品视频 (9.6s)  | 720×720（1.0）→ None×None（1:1），NoneMB
```

### 根因

`normalize_video` 判定视频已合规时走 `action="skip"` 分支，返回值里**没有
`outMeta` 键**（[video.py:352](../app/publish/video.py#L352)）：

```python
return {"status": "ok", "action": "skip", "output": path, "meta": meta,
        "ratioName": chk["ratioName"]}
```

而阶段收尾无条件读它（[stages/video.py:101](../app/publish/stages/video.py#L101)）：

```python
out = norm.get("outMeta") or {}          # skip 分支下恒为 {}
... f"{out.get('w')}×{out.get('h')}（{norm.get('ratioName')}）{dur}，{out.get('sizeMB')}MB"
```

于是三个字段全 None。**视频本身是传成功的**（日志有
`视频直传完成：source.mp4 2.53MB` 与 `产品视频已替换为 source.mp4`），
纯粹是上报文案错误。

这条路径不是死代码：`action="skip"` 但源地址不在店小秘图床时仍会转存一次
（Temu 源视频挂 goods-vod.kwcdn.com，店小秘拉它会被限流，见 stages/video.py:79-85
的取证），所以「已合规 + 转存」是常见组合，每次都会打出 None。

### 方案

skip 分支下没有新产物，`meta` 就是最终产物的元数据。两种改法择一：

1. **阶段侧兜底**（改动小）：`out = norm.get("outMeta") or norm.get("meta") or {}`。
2. **库侧补齐**（更彻底）：skip 分支一并返回 `outMeta=meta`，语义变成「outMeta 恒为
   最终产物的元数据」，调用方不必知道 skip 与否。

倾向 2——让返回契约自洽，免得下一个调用方再踩一遍。但注意 `sizeMB`：`probe_video`
是否给这个字段要先确认，没有就用 `os.path.getsize(norm["output"])` 现算。

按项目既有规矩，文案要能指向有效动作（见记忆 `publish-stage-fail-message-by-stage`）。

### 已实施（2026-09-19）

取方案 2：`normalize_video` 的 skip 分支一并返回 `outMeta=meta`、`trimmed=False`，
语义定为「`outMeta` 恒为最终产物的元数据」。`sizeMB` 那点已核实——`probe_video`
两条数据源（ffprobe 与 ffmpeg-stderr）都给 `sizeMB`，不必现算。

阶段侧另外按环节把文案分了两支（原来两支共用一句「前 → 后」的模板）：

- 已合规转存那支打 `已合规（720×720 1:1，2.53MB），未裁切，已转存到店小秘图床`。
  尺寸前后一样，打成 `720×720 → 720×720` 会让人怀疑白转了一遍；这一支的成果是
  **地址换到店小秘图床**，文案就该说这件事。
- 回填失败的文案原来一律讲「裁切成功但回填失败」，对没裁过的那支是语义错误
  （人照着这句去查裁切根本没有产物）。判据取 `norm["action"]`，不 parse 文案。

---

## 问题二：`download_video` 零重试（真实缺陷）

### 取证

```
! 人工检查[video]：视频下载失败，未处理（发布时可能被平台按比例打回）：
  下载失败 rc=28: curl: (28) Failed to connect to caiyuanbao.alicdn.com
  port 443 after 42649 ms: Couldn't connect to server
√ (13)b 产品视频 (43.8s) | 下载失败，视频原样保留
```

rc=28 是超时，属瞬时故障。

### 根因

[video.py:307](../app/publish/video.py#L307) 只发一次 curl，非零退出立即返回 error：

```python
p = _run(["curl.exe", "-sSL", "-f", "--max-time", str(timeout), ...])
if p.returncode != 0:
    return {"status": "error", ...}
```

这与项目其它下载路径**不一致**：

- 主图下载带浏览器头 + 指数退避重试，CLAUDE.md 明列为已知陷阱
  （「裸 requests 会被 CDN 拦」），实现见 `extract._download_image`，
  且刻意区分「404 不重试、其它错误重试」；
- 出图链路有 `_edits_post_with_retry`，rc=28 时打「链路抖动（n/4），6s 后重试」。

视频下载是同一类网络操作，却没有这层防护。

### 方案

照 `extract._download_image` 的形状补重试（**copy 再改，别手写**，见 CLAUDE.md
「代码迁移用 copy 再改」）：

- 指数退避，3 次左右；
- **区分错误类型**：rc=22/404 这类「源站就没有」不重试，rc=28/56/35 这类瞬时故障才重试；
- 日志沿用「链路抖动（n/m），Xs 后重试」的既有措辞，便于跨阶段 grep。

注意 `--max-time` 已是 300s，重试会把最坏耗时拉到 15 分钟量级。建议**缩小单次
timeout 再配重试**（例如单次 120s × 3 次），总耗时可控且对瞬时故障更有效。

另需确认：`caiyuanbao.alicdn.com` 连不上是否与代理有关。出图链路走 Clash
（见记忆 `image-api-needs-proxy`，rc=35 是节点挂了），视频 CDN 是否也需要走代理、
或者反过来**必须直连**，这点没有实测结论，改之前先用两条 curl 判链路。

### 已实施（2026-09-19）

**代理那点先判了链路，结论是必须直连**：

```
direct：  http=403 time=1.25   （TLS 通、连接正常；403 只是根路径无资源）
proxy  ： curl: (7) Failed to connect ... over proxy 127.0.0.1 after 2024 ms
```

即**与出图链路刚好相反**：阿里 CDN 是国内站，代理绕出国反而断。这台机器当前没设
proxy 环境变量，但一旦为出图链路设了全局代理，curl 会默认吃掉它把视频下载也带上，
于是整批视频静默失败。故 `download_video` 显式加 `--noproxy *`。

重试按 `extract._download_image` 的形状补齐，落在 `app/publish/video.py`：

- `_CURL_TRANSIENT_RC = {6,7,16,18,28,35,52,55,56}`，只有这些码重试；
  `-f` 下源站 403/404 是 rc=22，属「源站就没有」，不重试。这份集合与
  `images._CURL_TRANSIENT_RC` 同源，刻意各留一份而不互相 import——本模块的取向是
  「无网络密钥依赖、可离线单测」，拉 images 进来会把出图的配置与密钥读取一并拖进来。
- 3 次、退避 `3s × 次数`，日志沿用「链路抖动（n/m），Xs 后重试」的既有措辞。
- 单次 `--max-time` 从 300s 收到 **120s**（按 spec 的建议），另加
  `--connect-timeout 15`：连不上要快速失败去重试，不该占满整个传输预算。
  最坏耗时落在 6 分钟量级，而不是原方案的 15 分钟。

单测用假 `_run` 覆盖四条决策（重试后成功、rc=22 不重试、用尽仍失败如实报错、
显式直连），不出网。

---

## 问题三：阶段每次续跑都重跑（**已确认是刻意设计，不要改**）

### 现象

`⑬b 产品视频` 出现在**每一个**续跑集里，无一例外：

```
[rowid-101] 上次 save 未成功，按页面实况重跑 2 个阶段：⑤c 产品轮播图、⑬b 产品视频
[rowid-103] 上次 save 未成功，按页面实况重跑 3 个阶段：⑤c 产品轮播图、⑩a SKU货号、⑬b 产品视频
```

### 为什么不是 bug

[resume.py:150-156](../app/publish/stages/resume.py#L150) 写明了理由：`videoUrl`
不在 DOM 里（只在 edit.json 响应里），`live_state` 那段 JS 读不到它，没法像别的阶段
那样按实况细判；而 save 没成功时该字段会退回认领带来的 1688 原始竖屏地址。
让阶段自己判是安全且便宜的——开头读接口，没视频或已合规都直接 skipped。

实测代价确实小：已合规的单子 9.6~11.5s，其中「此前已完成，续跑跳过」只花 9.6s。
漏跑的代价反过来大得多（走完 15 个阶段才被平台打回）。

**记录在此只为免得下一个人把它当成缺陷去"修"。** 真要优化，方向是让 live_state 能
拿到 videoUrl（比如阶段② 落一份到断点文件），而不是把它从续跑集里摘掉。

2026-09-19 复核：未改动。`resume.py:150-156` 的注释与
`test_publish_video_stage.py` 的两条断言（`test_续跑时视频阶段总要重跑`、
`test_读不到实况时视频阶段也在重跑集`）已经把这个设计钉住了。

---

## 验证方式

单测（离线，不碰浏览器）：

```powershell
pytest tests/test_publish_video_stage.py -v
```

真站单商品（会操作真实页面，需先起带 `--remote-debugging-port=9222` 的 Chrome）：

```powershell
python publish_run.py --rowid <rowid> --info <product-info.json> --from-stage video
```

三单可复现的现场：`1040482047185`（下载失败 rc=28）、`1005064778878` 与
`1049857947880`（None×None）。断点在 `workspace/publish-state/rowid-*.json`。

不加 `--publish` 只跑到 save，不会真上架。

## 不在本 spec 范围

- ⑤c 轮播图的五处修复（已完成并真站验证，三单均已落库）
- 质检判定抖动（见记忆 `publish-vision-qc-flaky-recheck`）
- `probe_video` / `check_video` 的比例判定本身——三单的 720×720 判定都正确
