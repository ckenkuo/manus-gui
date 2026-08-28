"""店小秘 Temu 半托管发布 —— service 层编排。

把 app/publish/ 下已就位的 15 个阶段原语（pipeline.py / claim.py / extract.py /
images.py）编排成「可批量、可断点续跑、带结构化事件」的长流程。模式对标
app/collect/service.py：_emit 回调制事件、aborted 统一出口、队列/SSE 由 app.py 侧加。

事件契约（SSE 的 event: 名就是 type；index 从 1 起）：

    {"type": "batch_start",   "total": int, "store": str, "site": str, "batch": int}
    {"type": "product_start", "index": int, "total": int, "offer": str, "title": str}
    {"type": "stage_start",   "index": int, "offer": str, "stage": str, "name": str}
    {"type": "stage_done",    "index": int, "offer": str, "stage": str, "name": str,
                              "status": "ok"|"skipped"|"fail", "elapsed_s": float, "note": str}
    {"type": "manual_check",  "index": int, "offer": str, "stage": str, "message": str}
    {"type": "product_done",  "index": int, "total": int, "offer": str, "rowid": str|None,
                              "status": "ok"|"fail", "elapsed_s": float, "note": str,
                              "failed_stage": str}
    {"type": "batch_done",    "ok": int, "fail": int, "elapsed_s": float}
    {"type": "log",           "level": "info"|"warning"|"error", "message": str}
    {"type": "aborted",       "reason": str}   # 可预期中止：清单为空/CDP 不可用/缺店铺…

阶段表（STAGES 的顺序即执行顺序）：

    ① extract    1688 采集提炼（extract_product，enrich=True 顺带视觉回填）
    ② claim      采集+认领取 rowid（collect_and_claim），然后 open_edit 打开编辑页
    ③ auto_cat   产品类目            ④ attrs     属性审核（apply=True）
    ⑤ titles     中英文标题+产地      ⑤b clean_images 脏图 AI 清理（⑥⑦ 共用产物）
    ⑥ material   素材图（视觉选图→square_image→替换）
    ⑦ skc        SKC 颜色图逐行       ⑧ fix_sizes 尺码勾选
    ⑨ sizechart  尺码表              ⑩ variant   变种信息
    ⑪ stock      仓库/库存/SKU分类    ⑫ shipping  运输信息（选最长时效）
    ⑬ desc       描述长图（视觉规划→删/换→desc_save）
    ⑬b video     产品视频（比例裁到 1:1/3:4/16:9 后回填，纯增益、从不 fail）
    ⑭ save       保存落库

【⑤b 为什么插在⑥ 之前而不是并进⑥】清理产物要被⑥素材图与⑦SKC颜色图共用，
放进⑥ 就得在⑦ 再清一遍同一批图，而 gpt-image-2 每张都是一次生图调用。
它是纯增益路径：清不动就按原图继续（⑥ 会按脏度打分挑最不脏的），从不 fail。

【⑬b 产品视频为什么在 save 之前、且从不 fail】视频不是我们传的，是阶段② 认领时
平台从 1688 连带搬来的（edit.json 的 videoUrl，指向淘宝 CDN）。1688 视频绝大多数
是 9:16 竖屏，而 Temu 只收 1:1/3:4/16:9，于是发布时被打回「Video ratio should be
1:1 or 3:4 or 16:9」——这个报错出现在阶段⑮【之后】：前 14 阶段全绿、save 也落库了，
最后一步才弹回，回执还不说是哪个视频。故必须在发布前把关，且要在 save 之前完成
（它改的是未保存的表单字段，与 ⑤~⑬ 同性质）。
它是纯增益路径：没视频/已合规则 skipped，下载或转码或回填失败一律报 manual_check
但仍返回 ok——视频只是加分项，为它让整个商品 fail 不值得（失败的实际后果只是
「带着不合规视频去发布被打回」，人工删掉视频即可发布）。

【生图产物一律落盘复用，重跑不重烧】gpt-image-2 是全流程最贵的调用，而 ⑤~⑬ 的
成果 save 前一重载就丢（见下方续跑那段），重跑很常见。两处生图都做了产物复用：
⑤b 清理产物落 cleaned/ 并回写 complianceNotes.clean=true，重跑时 plan_clean 判
「已有干净图」整段跳过；⑬ 英化产物落 desc-edit/<源URL哈希>-en.jpg，命中即复用、
连质检也不重跑（落盘前提就是质检已过）。质检未过的产物必须删掉，否则会被当成
已通过的缓存。

【页面生命周期约束】② 之后 open_edit 打开编辑页，此后直到 ⑭ save 全程不刷新——
open_edit 会刷新页面把未保存的修改丢掉（publish_inspect.py 的实测注释）。

【哪些动作被提前/并行了（2026-08-26 提速改造）】阶段表本身仍是严格串行，提速只动
两处「与页面无关的等待」：

1. ① 之后立刻起一个后台预热任务（_start_prewarm → _run_prewarm），并发跑五件
   只依赖本地产物的事：⑤ 标题生成、⑤b 主图清理（整段）、⑩ 包装尺寸重量估算、
   ⑪ SKU 分类判断、⑬ 描述图规划 + 英化备料。它与 ② 认领（实测 33~37s）、③ 类目
   （缓存命中 15s，未命中曾达 314s）并行——那段时间浏览器在忙、LLM 完全空闲。
   各阶段到点用 _await_prewarm 取结果，取不到就照原路现场算，故预热纯属增益。
   【⑥ 素材图与 ⑦ SKC 刻意不预热】它们要读 ⑤b 清理【之后】的 complianceNotes，
   提前跑会读到旧标注、把清理成果作废（详见 _st_material 里那段）。
2. ⑬ 描述图的生图与替换拆成两段：先并发把所有英化产物烧好，再串行逐张定位 + 替换。
   原先两者在同一循环里，生图被迫一张一张来（单张约 35s，是该阶段耗时主体）。

生图并发数由用户配置（get_image_concurrency，默认 30，发布页与 CLI 共用一份 prefs）：
Packy 侧的实际并发上限随网关档位与出网链路变化，写死任何一个数都会在另一种环境里
是错的（链路差时高并发只会互相挤占带宽、集体超时）。

断点续跑：workspace/publish-state/<key>.json 记录每阶段状态与耗时（key = offerId
或 rowid-<rowid>）。重跑时已 ok/skipped 的阶段自动跳过（stage_done 发 skipped）；
from_stage 指定则从该阶段起重跑（含它自己，之前的阶段保留）。状态读写全部
best-effort，文件坏了当全新跑，不阻断主流程。

【状态文件记的是「跑过」，不是「存住了」——续跑必须再看页面实况】⑤~⑬ 全部只改
未保存的表单，成果靠 ⑭ save 一次性提交。save 没成功过时，页签一关 / Chrome 一退 /
open_edit 重新导航，这些成果就全丢了；只按状态文件跳过会直奔 save 提交一张空表单，
永远卡在「产品信息、变种信息」校验未过（2026-08-23 实测 947662049255：状态文件
③~⑬ 全 ok，编辑页实测标题空、变种表 0 行、尺码表未加、描述图退回 1688 外链）。
故 save 未成功时读一次编辑页实况（pipeline.live_state）来定夺哪些表单阶段要重跑，
见 _FORM_ONLY_STAGES 与 _stale_form_stages。显式给了 from_stage 时不做这个判定
（人工指定起点优先）。

错误策略（对齐 collect 侧）：
- 可预期前置失败（清单为空/缺店铺/CDP 不可用）→ aborted 事件 + 提前返回，不空跑；
- 单阶段失败 → 该商品记 fail、写状态文件，继续下一个商品不中止批次；
- 保存校验未过（红区块）→ manual_check + 该商品 fail（不丢人，草稿还在，改完可续跑）；
- CDP 连续 3 次 ping 不上 → aborted + break，已完成商品不受影响。

【发布闸门】⑮ publish（点「发布」→「立即发布」）由 do_publish 控制，不传则该阶段
skipped、流程收尾在 ⑭ save（保存落库）。发布不可逆（真实商家账号，上架后要下架才能
改），故闸门始终在调用方，两个入口的默认值刻意不同：
- Web 发布页「自动发布」开关，默认【开】（用户 2026-08-25 要求）——日常批量作业，
  全自动才是目的（见记忆 publish-full-auto-data-quality 的取向）；开着时前端先弹
  一次确认，那是这个默认值的配套约束。
- CLI publish_run.py --publish，默认【关】——CLI 常用于单步验证与断点续跑调试，
  默认上架会误伤。
"""
import asyncio
import inspect
import json
import os
import re
import shutil
import time
from typing import Awaitable, Callable, Optional, Union

from app.logger import logger
from app.publish import cache, extract, images, video as videolib, vision
from app.publish.sources import base as sources_base
from app.publish.browser import BrowserSession, ensure_cdp_alive
from app.publish.claim import collect_and_claim
from app.publish.llm import reset_token_counters, active_llm_label
from app.publish.pipeline import (
    add_sizechart,
    _size_category_for,
    auto_cat,
    check_attrs,
    desc_delete,
    desc_map,
    desc_text_apply,
    desc_text_map,
    desc_replace,
    desc_save,
    ensure_desc_closed,
    estimate_pack,
    fix_sizes,
    fix_sku_codes,
    generate_titles,
    judge_sku_category,
    live_state,
    open_edit,
    publish_now,
    save,
    set_material,
    set_shipping,
    set_stock,
    set_titles,
    set_variant,
    set_video,
    skc_replace_row,
    read_video_url,
    SKC_ROW_MIN_IMAGES,
    _skc_row_matches,
    _skc_row_state,
)

ProgressCB = Optional[Callable[[dict], Union[None, Awaitable[None]]]]

STATE_DIR = os.path.join("workspace", "publish-state")
PREFS_PATH = os.path.join("workspace", "publish_prefs.json")

PRODUCT_TIMEOUT = 1800   # 单商品 15 阶段含多次 LLM + 图片上传/生图，给足 30 分钟
CDP_PING_RETRIES = 3     # 每商品前的 CDP 健康检查次数（对齐 collect 侧）

# 生图并发的默认值与上限（实际值由用户在发布页配置，见 get_image_concurrency）
IMAGE_CONCURRENCY_DEFAULT = 30
IMAGE_CONCURRENCY_MAX = 64
# 描述图英化的「出图 + 质检」总发数：质检未过时再烧一发（理由见 _prepare_desc_image）
DESC_QC_TRIES = 2
# 【文字层没清干净的给更多发数】判据是 vision.check_cleaned 的 residualChinese 或
# garbled——这两类都是「这一发生图碰巧没弄好」，正是重试能救的随机失败：
#   - residualChinese：中文是 Temu 最硬的红线，退回原图的代价是这张图既带中文又撞
#     1340×1785 闸门（2026-08-26 实测那批：pos 2/3/5 三张退回原图，desc_save 回读
#     同时报「仍有外链图未转存」和三张破线）。
#   - garbled：生图自己吐出的无意义英文，随机性最强的一类。2026-08-26 ⑤b main-04
#     两发都是这个（「AI英化后英文为无意义拼写」→「疑似乱码/无意义文字」），恰好
#     2 发用完就放弃、那张主图于是以脏图身份参与 ⑥⑦ 选图（vision._dirty_score）。
# brokenSubject 仍只给 DESC_QC_TRIES：模型在改坏商品主体时多烧只会得到另一张坏图，
# 且风险方向相反（宁可退回原图，也不要一张主体被改烂的图上真店）。
DESC_QC_TRIES_TEXT = 4

STAGES = [
    ("extract", "① 采集提炼"),
    ("claim", "② 采集认领"),
    ("auto_cat", "③ 产品类目"),
    ("attrs", "④ 属性审核"),
    ("titles", "⑤ 标题产地"),
    ("clean_images", "⑤b 图片清理"),
    ("material", "⑥ 素材图"),
    ("skc", "⑦ SKC颜色图"),
    ("fix_sizes", "⑧ 尺码勾选"),
    ("sizechart", "⑨ 尺码表"),
    ("sku_code", "⑩a SKU货号"),
    ("variant", "⑩ 变种信息"),
    ("stock", "⑪ 库存SKU"),
    ("shipping", "⑫ 运输信息"),
    ("desc", "⑬ 描述长图"),
    ("video", "⑬b 产品视频"),
    ("save", "⑭ 保存落库"),
    ("publish", "⑮ 立即发布"),
]
_STAGE_IDS = [s for s, _ in STAGES]

_DONE = ("ok", "skipped")  # 阶段终态里算「完成」的（skipped = 本商品无需该阶段）

# 进入前必须已经停在店小秘编辑页的阶段（auto_cat 自己会 open_edit，故从 attrs 算起；
# claim 也会 open_edit，在 publish_one 里统一判断）
_EDIT_PAGE_STAGES = set(_STAGE_IDS[_STAGE_IDS.index("attrs"):])

# 【只改未保存表单、成果靠 ⑭ save 一次性提交的阶段】save 没成功过，它们就等于没跑：
# 页签一关 / Chrome 一退 / open_edit 重新导航，成果全丢（2026-08-23 实测取证，见
# pipeline.live_state 上方注释）。故续跑时这些阶段【不认状态文件】，改按页面实况判定。
#
# 不在此列的：① extract（产物是本地 product-info.json 与图片，磁盘上）、
# ② claim（服务端建了草稿，rowid 已在状态文件里）。
#
# 【③ auto_cat 与 ④ attrs 也在此列，2026-08-24 起】原先把它们排除在外，依据是
# 「类目与属性由服务端持久化，重开编辑页仍在」。890843533224 这单证伪了：save 从未
# 成功时类目一样丢，回落到认领带来的旧类目、且那旧类目已被平台下线（页面弹「该分类
# 已在平台删除！」）。类目没生效 → 尺码行不渲染 → ⑧⑨⑩⑪ 无处可填 → save 死循环。
# 详见 pipeline._JS_LIVE_STATE 上方的注释与 _stale_form_stages 里的类目判据。
# 类目与它的从属阶段：类目一换属性区整体重建，故这两个永远一起进出重跑集。
_CAT_STAGES = ["auto_cat", "attrs"]

# ⑤ 起的纯表单阶段（类目有效时只有这些需要按实况逐项细判）
_FORM_STAGES_AFTER_CAT = [
    "titles", "clean_images", "material", "skc", "fix_sizes",
    "sizechart", "sku_code", "variant", "stock", "shipping", "desc", "video",
]

_FORM_ONLY_STAGES = _CAT_STAGES + _FORM_STAGES_AFTER_CAT


async def _emit(on_progress: ProgressCB, event: dict) -> None:
    """同步/异步回调都兼容；回调炸不影响主流程（照 collect/service._emit）。"""
    if on_progress is None:
        return
    try:
        r = on_progress(event)
        if inspect.isawaitable(r):
            await r
    except Exception as e:
        logger.warning(f"进度回调异常（忽略）：{e}")


def _install_log_bridge(on_progress: ProgressCB) -> int:
    """把 app.publish / app.llm 的 INFO+ 日志转发成 UI 的 log 事件，返回 sink id。

    各小步（认领勾选店铺、SKC 逐张挂图、LLM token 用量……）本来就写 loguru，
    只是没推到页面——桥接比逐处加 emit 覆盖得全（2026-08-22 用户反馈：
    批次跑起来几分钟没动静，以为卡死）。loguru sink 是同步回调，
    经 call_soon_threadsafe 交回事件循环发事件。
    """
    loop = asyncio.get_event_loop()

    def _sink(message) -> None:
        rec = message.record
        ev = {"type": "log", "level": rec["level"].name.lower(),
              "message": rec["message"]}
        try:
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(_emit(on_progress, ev)))
        except RuntimeError:
            pass  # 事件循环已关（批次结束后的零星日志），丢弃即可

    return logger.add(
        _sink, level="INFO",
        filter=lambda r: r["name"].startswith(("app.publish", "app.llm")))


# ---- 状态持久化（断点续跑）---------------------------------------------------

def _state_path(key: str) -> str:
    return os.path.join(STATE_DIR, f"{key}.json")


def load_state(key: str) -> dict:
    """读单品状态；没有或坏了返回空壳（best-effort，不阻断主流程）。"""
    state = {"key": key, "stages": {}, "status": "new"}
    try:
        with open(_state_path(key), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("stages"), dict):
            state.update(data)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"状态文件损坏，当全新跑（{_state_path(key)}）：{e}")
    return state


def save_state(state: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_state_path(state["key"]), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"状态文件写入失败（忽略）：{e}")


def load_prefs() -> dict:
    try:
        with open(PREFS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(prefs: dict) -> None:
    """把 prefs 里的键【合并】进偏好文件，未提及的键原样保留。

    【为什么必须是合并而不是覆盖】这个文件同时存着两类东西：run_batch 每次成功启动
    都会记的 store/site，以及用户在页面上单独设过一次就该长期生效的生图并发数
    （imageConcurrency）。原先是整体覆盖，run_batch 一跑就把并发配置抹掉、静默回落
    默认值——用户改过的设置在下一次跑批后自己消失，且没有任何提示。
    """
    try:
        merged = {**load_prefs(), **prefs}
        os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
        with open(PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"prefs 写入失败（忽略）：{e}")


def get_image_concurrency() -> int:
    """生图（gpt-image-2）并发数：读用户配置，非法值回落默认，并夹到 1~上限。

    【为什么要让用户自己配】Packy 侧对 gpt-image-2 的实际并发上限随网关档位与本机
    出网链路（VPN）变化：链路好时 30 并发能把 ⑤b 与 ⑬ 的生图墙压到接近单张耗时，
    链路差时高并发只会互相挤占带宽、集体超时，反而比小并发更慢。这个最佳值只有
    用户的实际环境能测出来，写死任何一个数都会在另一种环境里是错的。

    默认 30 是常规链路的经验值（原先写死 3 是「Packy 限流未知先保守」的临时取值，
    实测远未触及上限）；上限 64 只为挡住手改配置时的荒谬值（几百并发必然全线超时）。
    """
    raw = load_prefs().get("imageConcurrency")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return IMAGE_CONCURRENCY_DEFAULT
    return max(1, min(IMAGE_CONCURRENCY_MAX, n))


def set_image_concurrency(n) -> int:
    """设生图并发数并落盘，返回实际生效值（越界被夹住时与入参不同）。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise ValueError(f"生图并发数必须是整数，收到 {n!r}")
    if n < 1 or n > IMAGE_CONCURRENCY_MAX:
        raise ValueError(f"生图并发数需在 1~{IMAGE_CONCURRENCY_MAX} 之间，收到 {n}")
    save_prefs({"imageConcurrency": n})
    return n


def _task_key(task: dict) -> str:
    """单品状态键：url 模式取「平台-商品ID」，rowid 模式取 rowid-<rowid>。

    【2026-08-27 从「随便找 6 位以上数字」改成按平台抽】原先是
    `re.search(r"(\\d{6,})", url)`，它不看域名：一个拼多多长链接里的
    refer_page_id 时间戳（10015_1786254742569_...）会先被命中，于是两个不同商品
    可能撞到同一个状态文件、互相覆盖进度。现在走 sources.source_id，各平台锚定
    自己的参数名/路径段（goods_id= / -g-<id>.html / /dp/<ASIN>）。

    【1688 的键保持纯 offerId 不带平台前缀】既有状态文件全是这个形态，加前缀会让
    所有历史进度失配（续跑时读不到上次跑到哪，等于从头再来）。新平台带前缀是必须的：
    拼多多 goods_id 与 1688 offerId 都是纯数字且位数重叠，撞号就是两个商品共用一份
    状态文件。
    """
    if task.get("url"):
        url = task["url"]
        platform = sources_base.platform_of(url)
        if not platform:
            raise ValueError(f"来源平台暂不支持（认不出域名）：{url}")
        pid = sources_base.source_id(url, platform)
        if not pid:
            raise ValueError(
                f"无法从 url 解析商品 ID（{sources_base.platform_name(platform)}）：{url}")
        return pid if platform == "1688" else f"{platform}-{pid}"
    if task.get("rowid"):
        return f"rowid-{task['rowid']}"
    raise ValueError(f"任务必须带 url 或 rowid：{task}")


def _load_info(info_path: str) -> dict:
    with open(info_path, encoding="utf-8") as f:
        return json.load(f)


# ---- 各阶段编排（入参统一 ctx/session/emit，返回 {"status", "note"}）----------
# status ∈ ok / skipped / fail；manual_check 事件由阶段内按需发。

def _stale_form_stages(live: dict) -> list:
    """按编辑页实况算出「成果已丢、需要重跑」的表单阶段。

    判据取粗而确定的信号（详见 pipeline._JS_LIVE_STATE 注释）：重跑一个其实还在的
    表单阶段只是多花时间，各阶段本身幂等；漏跑一个真丢了的会让整单卡死在 save。

    【⑧ 尺码勾选丢了要连带 ⑨⑩⑪】变种信息表 0 行时，尺码表/变种/库存全都无处可填，
    只补 ⑨ 是没用的（2026-08-23 实测：skuRowCount=0 时变种属性区只剩「请选择引用模板」）。

    【⑩a SKU 货号不能跟着 skuFilledRows 判】货号列自己就是 input，平台「一键生成」
    写进去的中文货号会让 skuFilledRows 非 0，于是「填过了」和「填的是非法值」在这个
    信号里长得一样。故 live_state 单出 skuCodeBad 计数，这里独立判。

    【③ 类目丢了要连带 ④ 属性，且此时不必再算下游】属性行是类目决定的（女童针织套头衫
    33 行 / 女童长裤套装 42 行），类目一换属性区整体重建，只补 ④ 是白填。而 ⑤ 起的所有
    表单阶段本来就在重跑集里，故类目异常时直接返回全量、不再逐项细判——细判的输入
    （尺码行、图、尺码表）在类目未生效的页面上全是 0，结论必然也是「全跑」。
    """
    if not live.get("rendered"):
        return list(_FORM_ONLY_STAGES)      # 读不到实况，保守全跑（含 ③④）
    # 类目失效（回落到认领旧类目 / 旧类目已被平台下线）是最上游的坏账，必须从 ③ 重来。
    # 【只有这一个分支会把 ③④ 放进重跑集】类目有效时重跑 ③ 是纯浪费：走一遍类目树
    # 要 110s（缓存命中也要 7.6s），而 ④ 属性确实由服务端存住了（重开编辑页 42 条
    # 属性行都在，2026-08-21 实测），不像 ⑤ 起的表单那样一重载就丢。
    if live.get("catUnset") or live.get("catDeleted"):
        return list(_FORM_ONLY_STAGES)
    stale = []
    if not live.get("titleFilled"):
        stale.append("titles")
    # ⑥⑦ 图片类：变种属性区一张图都没有说明素材图与 SKC 换图都丢了。
    # ⑤b clean_images 是它们的上游产物提供者，跟着一起重跑（它自己会判「已有干净图」跳过）。
    if not live.get("attrImgCount"):
        stale += ["clean_images", "material", "skc"]
    elif live.get("attrImgBad"):
        # 图在、但有破线的（某颜色行没被 ⑦ 换过，留着 1688 原始小图）：只重跑 ⑦。
        # 不连带 ⑤b/⑥——那条是素材图，与某个颜色行漏换无关，重跑要白烧生图。
        stale.append("skc")
    if not live.get("skuRowCount"):
        stale += ["fix_sizes", "sizechart", "sku_code", "variant", "stock"]
    else:
        # 【无尺码维的类目不判尺码表】非服装类目（仿真花/玩具/饰品）压根没有尺码表栏，
        # sizechartAdded 恒 false，照判会让⑨ 每次续跑都进重跑集、每次又 skipped，
        # 白跑一轮还把日志搅浑。hasSizeGroup 与 sizechartCount 都没有才算「本类目不要」，
        # 两个信号一起看是为了不把「渲染慢」误判成「不需要」（见 live_state 的取证）。
        no_size_dim = not live.get("hasSizeGroup") and not live.get("sizechartCount")
        if no_size_dim:
            logger.info("本类目无尺码维（无尺码组且无尺码表栏），续跑判定不安排⑨ 尺码表")
        elif not live.get("sizechartAdded"):
            stale.append("sizechart")
        # 第二张表：套装商品必填，没填平台会打回「套装尺码模板数量不合法」。
        # sizechart2Added 为 None 表示该类目没有这一栏（不判 stale）；为 False 才是
        # 「有栏但空着」。是否真的必填由 _st_sizechart 按 SKU分类判，这里只负责把
        # 「第一张有了、第二张空着」这种半成品状态也算进重跑范围——只看第一张会让
        # 套装商品续跑时跳过 ⑨，接着又被平台打回同一个错。
        elif live.get("sizechart2Added") is False:
            stale.append("sizechart")
        # ⑩a 单独判：货号列有值不代表合法，平台生成的中文值恰恰是要改的那个
        # （skuCodeBad 已把「空」与「含非 ASCII」都算进去，见 _JS_LIVE_STATE）
        if live.get("skuCodeBad") or not live.get("skuCodeCount"):
            stale.append("sku_code")
        if not live.get("skuFilledRows"):
            stale += ["variant", "stock"]
    if not live.get("shippingSet"):
        stale.append("shipping")
    # ⑬ 描述：图全是 1688 外链（alicdn）说明删图/英化成果没了。
    # 描述区一张图都没有时不判 stale——那可能是本商品本就无描述图，交阶段自己判。
    if live.get("descImgCount") and live.get("descForeignCount") == live.get("descImgCount"):
        stale.append("desc")
    # ⑬b 视频：【无条件进重跑集】。save 没成功时 videoUrl 会退回认领带来的 1688 原始
    # 竖屏地址，而这个字段不在 DOM 里（只在 edit.json 响应里），live_state 那段 JS
    # 读不到它、没法像别的阶段那样按实况细判。让它自己去判是安全且便宜的：阶段开头
    # 就读接口，没视频或已合规都直接 skipped（只花一次接口 + 一次下载探测的几秒），
    # 与 clean_images「自己判已有干净图就跳过」同一取向。
    # 漏跑的代价反过来大得多——带着竖屏视频去发布，走完 15 个阶段才被平台打回。
    stale.append("video")
    return [s for s in _FORM_ONLY_STAGES if s in set(stale)]


async def _st_extract(ctx: dict, session: BrowserSession, emit) -> dict:
    # 【显式 from_stage=extract 必须真的重提，不能被旧产物挡住】原先只判 info_path
    # 有值就 skipped，于是落盘产物本身错了的商品无路可走：状态文件回填 info_path
    # （见 publish_one 的 ctx 构造），指定 from_stage 也照样跳过。
    # 2026-08-28 offer 1014675972015 就撞在这里——单维 spec 的 bug 修好后，该商品
    # 磁盘上那份 skus={} 的 product-info.json 仍会被原样沿用，重跑几次都是同一个错。
    # from_stage 是人工显式判断（publish_one 里「显式指定起点不该被实况覆盖」同一取向），
    # 点名 ① 就是要重新抓一遍源数据。rowid 模式（无 url）没得可提，仍然跳过。
    redo = ctx.get("from_stage") == "extract" and ctx.get("url")
    if ctx.get("info_path") and not redo:
        ctx["workdir"] = os.path.dirname(os.path.abspath(ctx["info_path"]))
        return {"status": "skipped", "note": "任务自带 product-info.json"}
    if redo and ctx.get("info_path"):
        await emit({"type": "log", "stage": "extract", "level": "info",
                    "message": "指定从 ① 起重跑：重新抓取源数据并覆盖 product-info.json"})
    # 源站弹滑块/验证码时把提示转成 manual_check 事件：Web 页面会多出一条人工检查、
    # CLI 打「! 人工检查」行。1688 会原地等人拖滑块过关（extract.wait_human_verify），
    # 拼多多/Temu/亚马逊没有可拖的东西，只报错交人处理（见各适配器）。
    async def _on_manual(message: str) -> None:
        await emit({"type": "manual_check", "stage": "extract", "message": message})

    r = await extract.extract_product(ctx["url"], session=session, enrich=True,
                                      on_manual=_on_manual)
    if r.get("status") != "ok":
        return {"status": "fail", "note": f"提取失败: {r}"[:200]}
    ctx["info_path"] = r["infoPath"]
    ctx["workdir"] = r["outdir"]
    ctx["title"] = r.get("title") or ctx.get("title")
    if r.get("visionError"):
        await emit({"type": "manual_check", "stage": "extract",
                    "message": f"视觉回填失败（图片阶段将现场看图）：{r['visionError'][:100]}"})
    return {"status": "ok",
            "note": f"属性 {r.get('attrCount')} 项 | 图 {r.get('mainImgs')}+{r.get('descImgs')}"}


def _start_prewarm(ctx: dict, emit) -> None:
    """把预热任务丢到后台跑，立刻返回，不等它（见 _run_prewarm 的说明）。

    【为什么是后台任务而不是一个阶段】它的价值恰恰在于与 ② 认领、③ 类目【并行】：
    做成阶段就又变成串行等待，一点提速都没有。任务句柄挂在 ctx 里由 publish_one
    在收尾时统一回收（见那边的 finally），免得批次结束还留着悬挂任务。

    【失败一律不影响主流程】_run_prewarm 内部逐项吞异常；这里再兜一层 done 回调只为
    把「整个预热协程炸了」也写成 warning——预热没跑成，各阶段照原路现场算而已。
    """
    if not ctx.get("info_path") or not ctx.get("workdir"):
        return

    task = asyncio.ensure_future(_run_prewarm(ctx, emit))

    def _done(t) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning(f"预热任务整体失败（各阶段将现场计算）：{exc}")

    task.add_done_callback(_done)
    ctx["prewarm_task"] = task


# ---- 纯本地判断的提前预热 ----------------------------------------------------
#
# 【为什么能提前】① extract 一跑完，product-info.json 与 main/desc 图就全在磁盘上了。
# ⑤ 标题、⑥ 素材图、⑦ SKC 分色、⑩ 包装估算、⑪ SKU 分类、⑬ 描述图规划这六个判断的
# 输入【只有这些本地产物】，与店小秘页面无关（逐个核对过各自的取值来源：set_titles 读
# title/attributes/skus/imageUnderstanding，pick_material 与 plan_skc 读 workdir 的
# main-NN + complianceNotes，set_variant 读 title/packInfo，set_stock 的分类判断读
# title/套装件数，plan_desc 读描述图 URL 与尺寸）。
#
# 【为什么值得提前】它们原先各自等在自己的阶段里串行发请求，而 ② 认领 + 打开编辑页
# （实测 33~37s）和 ③ 类目（缓存命中 15s，未命中曾达 314s）这段时间浏览器在忙、
# LLM 完全空闲。把这六个判断挪到 ① 之后并发起跑，正好填进这段空窗。
#
# 【必须是纯增益，绝不改变判定】预热只把结果放进 ctx["prewarm"]，各阶段命中就用、
# 没有就照原路现场算。任何一个预热失败都只写 warning：这是本项目辅助路径的既定取向
# （见模块头「best-effort」那段），不是 fallback——现场算那条路本来就一直在。
#
# 【描述图规划为什么也算纯本地】plan_desc 的输入是 modules 的 url + tooSmall，而
# 认领后店小秘描述区挂的就是 1688 源外链（desc_save 的校验项「仍有外链图未转存」即
# 此，2026-08-23 真站取证），与 raw.json 的 descImages 同源。故可按源 URL 预先出计划、
# 并把英化产物按 URL 哈希烧进 desc-edit/ 缓存；⑬ 到点仍按页面实况重查一遍 modules，
# 命中缓存直接复用、URL 对不上就现场跑。预热在这里【只做备料，不替代 plan_desc】。
_PREWARM_KEYS = ("titles", "material", "skc", "variant", "stock", "desc")


def _desc_modules_from_raw(workdir: str) -> list:
    """按 raw.json + 本地 desc-NN.jpg 拼一份 plan_desc 能吃的 modules（供预热用）。

    尺寸从本地文件读而不是靠网络：extract 下载时 `enumerate(imgs, 1)` 保证
    desc-NN.jpg 与 descImages 按下标一一对应，故第 i 张的尺寸就是 desc-{i:02d}.jpg 的。
    读不到尺寸的不标 tooSmall——与 desc_map 的取向一致（naturalWidth 为 0 时按
    「读不到」处理，不当成不达标），免得把好图误判成要放大。
    """
    raw_path = os.path.join(workdir, "raw.json")
    try:
        with open(raw_path, encoding="utf-8") as f:
            urls = (json.load(f) or {}).get("descImages") or []
    except Exception as e:
        logger.warning(f"预热读 raw.json 失败（跳过描述图预热）：{e}")
        return []
    mods = []
    for i, u in enumerate(urls, 1):
        if not isinstance(u, str) or not u:
            continue
        m = {"pos": i, "url": u, "onDxmHost": "dianxiaomi.com" in u}
        wh = images.image_size(os.path.join(workdir, f"desc-{i:02d}.jpg"))
        if wh:
            m["size"] = f"{wh[0]}x{wh[1]}"
            # 描述图按描述图的规则判（比例 0.5~2、两边 >= 480），不套服装 1340x1785：
            # 套错会把 1000x1000 这种合格图标成 needsUpscale，每跑白烧一轮生图
            # （见 images.check_desc_size 上方的截图取证）
            chk = images.check_desc_size(wh[0], wh[1])
            m["tooSmall"] = chk["ok"] is False
            if chk["ok"] is False:
                m["sizeReasons"] = chk["reasons"]
        mods.append(m)
    return mods


def _replan_desc_by_url(pre_plan: dict, mods: list) -> dict:
    """把预热出的规划按页面 modules 的【当前序号】重挂，返回同 plan_desc 形状的产物。

    【为什么必须重挂而不能直接用】预热的 pos 是 raw.json 里 descImages 的源顺序；页面
    描述区的 pos 由 desc_map 按 .desc-img-box img 现数（且文字模块处理后还会重排）。
    两套序号只是常常相同、并不保证相同——直接拿源 pos 去删/换，就是本项目已经踩过的
    那类错位（见 pipeline._JS_DESC_IDX_MAP 上方那次「删 pos 3/2 实际删掉 pos 2/1」）。
    URL 是稳定标识，故一律按 URL 重新对齐 pos。

    动作从预热计划里按 URL 取；页面上有而预热没判过的图【一律按 keep 处理】——与
    plan_desc 对漏判项的取向一致（保守方向，不删不该删的）。needsUpscale 则按页面
    现测的 tooSmall 重算：尺寸是页面事实，预热按本地文件算的只是估计，两者不一致时
    要信页面（本地 desc-NN.jpg 与页面挂的图理论同源，但页面可能已被替换过）。
    """
    # plan_desc 的 delete 只回 pos，要还原成 URL 得靠预热时那份 modules 的映射，
    # 故 _prewarm_desc 会把它翻成 deleteUrls 一起落下来（见那边）。
    act_by_url, reason_by_url = {}, {}
    for u in pre_plan.get("deleteUrls") or []:
        act_by_url[u] = "delete"
    for r in pre_plan.get("replace") or []:
        if r.get("url"):
            act_by_url[r["url"]] = "replace"
            reason_by_url[r["url"]] = r.get("reason") or ""

    delete, replace = [], []
    for m in mods:
        u, pos = m.get("url"), m.get("pos")
        if not u or not pos:
            continue
        act = act_by_url.get(u, "keep")
        if act == "delete":
            delete.append(pos)
            continue
        # 尺寸按页面现测重算：keep 但不达标的照 plan_desc 的规矩改判 replace + 放大
        too_small = bool(m.get("tooSmall"))
        if act == "replace":
            replace.append({"pos": pos, "url": u,
                            "reason": reason_by_url.get(u, "")})
        elif too_small:
            replace.append({"pos": pos, "url": u, "needsUpscale": True,
                            "reason": f"尺寸 {m.get('size')} 不符合描述图要求："
                                      + "、".join(m.get("sizeReasons") or [])})
    replace.sort(key=lambda r: r["pos"])
    keep = [m["pos"] for m in mods
            if m.get("pos") not in set(delete)
            and all(r["pos"] != m.get("pos") for r in replace)]
    return {"status": "ok", "delete": sorted(set(delete)),
            "replace": replace, "keep": sorted(set(keep))}


async def _prewarm_desc(ctx: dict, info: dict, emit) -> dict:
    """预热描述图：按源 URL 出规划 + 并发把英化产物烧进 desc-edit/ 缓存。

    返回 {"plan": plan_desc 产物, "prepared": {url: 备料结果}}。⑬ 阶段拿 plan 当
    页面实况对得上时的现成计划，prepared 则由 _prewarm_desc_images 的落盘缓存自然生效
    （产物在磁盘上，⑬ 那边 how=cached 直接命中）。
    """
    mods = _desc_modules_from_raw(ctx["workdir"])
    if not mods:
        return {}
    plan = await vision.plan_desc(mods, info)
    # delete 只回 pos，而 ⑬ 那边要按 URL 重挂序号（见 _replan_desc_by_url），故这里
    # 就把 pos 翻成 URL 存下来——翻译要用的映射只在此刻手上有。
    by_pos = {m["pos"]: m["url"] for m in mods}
    plan = {**plan,
            "deleteUrls": [by_pos[p] for p in (plan.get("delete") or []) if p in by_pos]}
    prepared = await _prewarm_desc_images(ctx["workdir"], plan.get("replace") or [], emit)
    return {"plan": plan, "prepared": prepared}


async def _run_prewarm(ctx: dict, emit) -> None:
    """并发跑完六个纯本地判断，结果写进 ctx["prewarm"]（每项独立 best-effort）。

    【为什么用 gather 而不是逐个 await】它们互不依赖，且都是纯网络等待（LLM 调用）。
    单项失败不能影响其它项，故 return_exceptions=True 逐项收，异常只写 warning。

    【为什么这个函数自己不 emit stage 事件】它不是阶段，是阶段的提前量。跑成什么样
    由各阶段照常报——预热命中时那个阶段的 elapsed_s 自然就短了，这比多一路事件清楚。
    """
    info = _load_info(ctx["info_path"])

    async def _titles():
        # 只做生成，不填页面：填写要 session，且必须在编辑页打开之后。
        return await generate_titles(info)

    async def _variant():
        # 按超集问（要尺寸也要重量）：此刻 cat_path 还没有，判不了服装类是否走固定尺寸，
        # 也不知道源 packInfo 会不会补上重量。多问的字段 set_variant 用不到即可，
        # 缺字段才会让它补问一次（见 estimate_pack 的 need_dims/need_weight 说明）。
        return await estimate_pack(info, need_dims=True, need_weight=True)

    async def _stock():
        return await judge_sku_category(info)

    async def _desc():
        return await _prewarm_desc(ctx, info, emit)

    async def _clean():
        return await _clean_main_images(ctx, emit)

    jobs = {"titles": _titles, "variant": _variant, "stock": _stock,
            "desc": _desc, "clean_images": _clean}
    keys = list(jobs)
    logger.info(f"提前预热 {len(keys)} 个纯本地判断（与认领/类目阶段并行）：{'、'.join(keys)}")
    results = await asyncio.gather(*(jobs[k]() for k in keys), return_exceptions=True)

    out: dict = {}
    ok_keys, bad_keys = [], []
    for k, r in zip(keys, results):
        if isinstance(r, BaseException):
            bad_keys.append(k)
            logger.warning(f"预热 {k} 失败（该阶段照原路现场算）：{r}")
            continue
        if r:
            out[k] = r
            ok_keys.append(k)
    ctx["prewarm"] = out
    logger.info(f"预热完成：命中 {len(ok_keys)} 项（{'、'.join(ok_keys) or '无'}）"
                + (f"，失败 {len(bad_keys)} 项（{'、'.join(bad_keys)}）" if bad_keys else ""))
    await emit({"type": "log", "stage": "extract",
                "message": f"提前预热完成：{len(ok_keys)}/{len(keys)} 项可直接复用"
                           + (f"，{len(bad_keys)} 项失败将现场重算" if bad_keys else "")})


async def _await_prewarm(ctx: dict, key: str):
    """等预热任务跑完（若还在跑），然后取一次该项结果；没有就返回 None。

    【为什么要等而不是「有就用、没有就现场算」】预热是与 ②③ 并行的后台任务，到 ⑤ 时
    多半已完成；但类目命中缓存时 ②③ 只花 50s 左右，标题这类可能还差几秒。此时直接
    判「没有」就会再发一次同样的请求——两条路都在跑同一个判断，既慢又多花一次调用。
    等它反而是快的：最差情况等到的时刻，与不预热时现场算完的时刻相同。

    等待本身不设超时：_run_prewarm 内部每一项都吞了异常、必然会返回，而它包住的都是
    带自身重试上限的 LLM 调用。这里再加一层超时只会造出「等了一半又去重算」的浪费。
    """
    task = ctx.get("prewarm_task")
    if task is not None and not task.done():
        try:
            await task
        except Exception as e:
            # done 回调已记过，这里只是别让它把当前阶段带下水
            logger.warning(f"等待预热任务时出错（现场计算）：{e}")
    return _take_prewarm(ctx, key)


def _take_prewarm(ctx: dict, key: str):
    """取一次预热结果并【摘掉】，取不到返回 None。

    摘掉而不是留着：⑤~⑬ 的成果 save 前一重载就丢，同一批次内该阶段可能被重跑
    （见模块头 _stale_form_stages 那段）。重跑时页面状态已变，而预热结果是按【第一轮
    的本地产物】算的——标题/包装估算这类与页面无关的仍然有效，但让它只用一次更稳妥：
    重跑走现场那条路，与不带预热时的行为完全一致，不必再论证「复用第二次是否安全」。
    """
    pool = ctx.get("prewarm") or {}
    return pool.pop(key, None)


async def _st_claim(ctx: dict, session: BrowserSession, emit) -> dict:
    note = ""
    if not ctx.get("rowid"):
        r = await collect_and_claim(session, ctx["url"], ctx["title"],
                                    ctx["store"], ctx["site"])
        if not r.get("rowid"):
            return {"status": "fail", "note": "认领后未取到 rowid（采集列表同步延迟？可续跑）"}
        ctx["rowid"] = r["rowid"]
        note = f"新建草稿 rowid={r['rowid']}"
    else:
        note = f"rowid={ctx['rowid']}（任务自带，跳过采集认领）"
    # 此后直到 save 全程不刷新页面（open_edit 会丢未保存修改）
    await open_edit(session, ctx["rowid"])
    await asyncio.sleep(2)
    await session.fix_hidden_tab()
    return {"status": "ok", "note": note}


async def _st_auto_cat(ctx: dict, session: BrowserSession, emit) -> dict:
    title = ctx.get("title")
    if not title and ctx.get("info_path"):
        title = _load_info(ctx["info_path"]).get("title")
    if not title:
        return {"status": "fail", "note": "缺商品标题（LLM 判断类目要用）"}
    ctx["title"] = title
    r = await auto_cat(session, ctx["rowid"], title,
                       use_cache=ctx.get("use_cache", True),
                       site=ctx.get("site") or "")  # 失败抛异常，交外层
    # 阶段④的属性缓存要按类目路径取，这里把它落进 ctx（并经回写元组进状态文件，
    # 续跑 from attrs 时才拿得到）
    ctx["cat_path"] = r.get("pathList") or []
    # note 里记明走的是缓存还是遍历：缓存快路径选错了下游没有任何校验能发现，
    # 事后核对全靠这一条（见 _try_cached_category 的风险注释）
    src = "缓存" if r.get("source") == "cache" else "遍历"
    return {"status": "ok", "note": f"[{src}] {r.get('path') or ''}"}


async def _st_attrs(ctx: dict, session: BrowserSession, emit) -> dict:
    if not ctx.get("info_path"):
        return {"status": "fail", "note": "缺 product-info.json（rowid 模式必须带 info_path）"}
    r = await check_attrs(session, ctx["info_path"], apply=True,
                          cat_path=ctx.get("cat_path"),
                          use_cache=ctx.get("use_cache", True),
                          site=ctx.get("site") or "")
    if r.get("status") != "ok":
        return {"status": "fail", "note": (r.get("reason") or str(r))[:200]}
    applied = r.get("applied") or []
    ok_n = sum(1 for a in applied if a.get("result") == "ok")
    note = f"改 {ok_n}/{len(applied)} 项，LLM 拒 {len(r.get('rejected') or [])} 项"
    if r.get("cacheRead"):
        note += f"，缓存选项 {r['cacheRead']} 行"
    if r.get("cacheRefreshed"):
        note += f"，过期重读 {len(r['cacheRefreshed'])} 行"
    if r.get("linkageFilled"):
        # 联动新增的必填行（里料纹理选「光面」带出的里衬成分/里料克重）由 check_attrs
        # 补填轮处理，这里只把结果记进 note——不记的话看日志完全不知道跑过这一轮。
        lk = r["linkageFilled"]
        lk_ok = sum(1 for a in lk if a.get("result") == "ok")
        note += f"，联动补填 {lk_ok}/{len(lk)} 项"
    if r.get("compFailed"):
        # 成分组不做单行重试（会破坏合计 100%），交人工
        await emit({"type": "manual_check", "stage": "attrs",
                    "message": f"成分字段写入失败需人工核对：{'、'.join(r['compFailed'])}"})
    if r.get("unfilledRequired"):
        # 【必填项留空会直接卡保存】所以这条必须提示到人，不能像原先那样只放在返回值里
        # 没人看（2026-08-25 用户截图的里料克重/里衬成分就是这么漏过去的）。
        miss = r["unfilledRequired"]
        await emit({"type": "manual_check", "stage": "attrs",
                    "message": f"必填属性仍留空需人工补：{'、'.join(miss)}"})
        note += f"，仍空 {len(miss)} 项必填"
    return {"status": "ok", "note": note}


async def _st_titles(ctx: dict, session: BrowserSession, emit) -> dict:
    # 预热命中直接填，省掉这里最贵的一次调用（标题生成含最多 2 轮，实测单次可达 100s+）
    gen = await _await_prewarm(ctx, "titles")
    generated = (gen or {}).get("generated") if (gen or {}).get("status") == "ok" else None
    r = await set_titles(session, ctx["info_path"], generated=generated)
    if r.get("status") != "ok":
        # 带上 err（具体拒因）：只报 title-generation-failed 时排查必须去翻日志，
        # 而阶段结果是写进状态文件、UI 也直接显示的那一份，原因得在这里就看得见。
        note = (r.get("reason") or "")
        if r.get("err"):
            note += f" | {r['err']}"
        return {"status": "fail", "note": note[:300]}
    g = r.get("generated") or {}
    return {"status": "ok", "note": f"英文 {(g.get('enTitle') or '')[:40]}"}


CLEAN_TIMEOUT = 90      # 单张清理超时（实测一张约 35s）；超了走原图兜底，不拖住整批


def _save_info(info_path: str, info: dict) -> None:
    """回写 product-info.json（best-effort：写坏了不影响当前批次内存里的决策）。"""
    try:
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"回写 product-info.json 失败（忽略）：{e}")


def _retry_hint(issues: str, cjk: bool) -> str:
    """质检未过后重烧时追加的提示词（⑤b 与 ⑬ 共用）。

    【为什么必须加码而不是原样重发】原样重发只是赌生图的随机性；把「上一发到底哪里
    没弄好」当成新约束喂回去，命中率明显更高（同 llm._JSON_RETRY_HINT 的取向）。

    两类失败要说不同的话——2026-08-26 实测这两类真实出现的比例差不多：
      - 残留中文（cjk=True）：要点是「逐块扫，一个字符和标点都不留」；
      - 生图乱码（cjk=False，如 ⑤b main-04 的 `oOaLanTanAt`）：根因是模型硬造英文，
        要点反过来——【认不出就整块删掉，不要编】。对乱码说「请翻译干净」只会让它
        再编一串新的无意义字母。
    【抽成一个函数】两处给的加码必须一致，各写一份必然漂移。
    """
    head = f"【上一次处理后仍不合格：{issues[:40]}】"
    if cjk:
        return head + ("请逐块检查图上每一处文字，包括竖排文字、角标、小字号说明、"
                       "以及印在色块或图案上的文字，一个中文字符和中文标点都不能留下。")
    return head + ("上一次生成的英文是无意义的拼写。请不要凭猜测编造英文单词："
                   "看不清或认不出的文字直接连同背景一起干净移除，"
                   "只保留你能确读并正确翻译的内容。")


async def _clean_main_images(ctx: dict, emit) -> dict:
    """把带中文/水印/他人 logo 的主图送 gpt-image-2 清理，产物顶替原图。

    返回 {"status": "ok"|"skipped", "note": ...}，直接就是 ⑤b 阶段的返回值形状。

    为什么单列一步而不是塞进⑥：产物要被⑥素材图和⑦SKC颜色图【共用】。塞进⑥
    就得在⑦再清一遍同一批图，而 gpt-image-2 每张都是一次生图调用。这里清完直接把
    complianceNotes 里对应条目改成 clean=true 并指向新文件，⑥⑦ 的选图逻辑一行不用改
    就自动挑到干净图。

    【绝不阻塞流程】这是用户明确要求：单张失败/超时/质检不过一律保留原标注，
    该图仍以脏图身份参与⑥⑦ 的兜底打分（见 vision._dirty_score），本步照常 ok。
    清理是「能修就修」的增益路径，不是硬前置。

    并发而非串行：单张实测约 35s，4 张串行 140s 会明显拖慢单商品耗时。
    edit_image 是同步 curl 子进程，故用 to_thread 丢线程池 + Semaphore 限流。
    并发数取用户配置（见 get_image_concurrency）：最佳值随出网链路变化，写死不了。

    【不接 session 参数】整段只读写本地图与 product-info.json，与店小秘页面无关——
    这正是它能被提前到 ② 认领之前跑的前提（见 _run_prewarm）。原先它作为阶段函数
    带着 session 形参却一次没用到，抽出来时一并去掉，免得让人误以为它碰页面。
    """
    if not ctx.get("info_path"):
        return {"status": "skipped", "note": "无 product-info.json，跳过清理"}
    info = _load_info(ctx["info_path"])
    plan = vision.plan_clean(info, ctx["workdir"])
    items = plan.get("items") or []
    if not items:
        return {"status": "skipped", "note": plan.get("reason") or "无可清理项"}

    outdir = os.path.join(ctx["workdir"], "cleaned")
    os.makedirs(outdir, exist_ok=True)
    conc = get_image_concurrency()
    sem = asyncio.Semaphore(conc)

    async def _one(item: dict) -> dict:
        """清一张：出图 → 质检 → 通过才算成功。返回 {"file", "ok", "path", "why"}。

        质检未过时再烧一发（DESC_QC_TRIES）：生图有随机性，同图同提示词两发结果不同，
        理由与 ⑬ 那边同源，见 _prepare_desc_image 里那段实测记录。这一路的失败代价
        更大——主图脏着会以脏图身份参与⑥⑦选图（vision._dirty_score）。
        """
        async with sem:
            dst = os.path.join(outdir, os.path.splitext(item["file"])[0] + "-clean.png")
            last_why, cjk_left = "", False
            attempt, tries = 0, DESC_QC_TRIES
            while attempt < tries:
                attempt += 1
                # 残留中文时重试要加码提示词（同 _prepare_desc_image 的理由）：
                # 原样重发只是赌随机性，把上一发残留了什么当新约束喂回去命中率更高。
                prompt = item["prompt"]
                if attempt > 1 and last_why:
                    prompt += _retry_hint(last_why, cjk_left)
                try:
                    # 素材图是轮播首图，糊了最伤转化，故这一路不降采样出图（见 pick_size 注释）
                    ed = await asyncio.to_thread(
                        images.edit_image, item["path"], prompt=prompt,
                        out_path=dst, no_downscale=True, timeout=CLEAN_TIMEOUT)
                except Exception as e:
                    return {"file": item["file"], "ok": False, "why": f"出图失败：{e}"[:120]}
                try:
                    qc = await vision.check_cleaned(ed["output"])
                except Exception as e:
                    return {"file": item["file"], "ok": False, "why": f"质检失败：{e}"[:120]}
                if qc.get("clean"):
                    return {"file": item["file"], "ok": True, "path": ed["output"]}
                last_why = f"质检未过：{qc.get('issues') or ''}"
                cjk_left = bool(qc.get("residualChinese"))
                # 同 _prepare_desc_image：中文残留与生图乱码都算「这发没弄好」，多烧
                # 有救。main-04 那次两发全是 garbled，只给 2 发正好白放弃。
                if cjk_left or qc.get("garbled"):
                    tries = max(tries, DESC_QC_TRIES_TEXT)
                if attempt < tries:
                    logger.info(f"{item['file']} 清理质检未过（{attempt}/{tries}"
                                f"{'，残留中文' if cjk_left else ''}），"
                                f"重烧一发：{(qc.get('issues') or '')[:60]}")
            return {"file": item["file"], "ok": False, "why": last_why[:120]}

    logger.info(f"图片清理：{len(items)} 张待处理（并发 {conc}）")
    results = await asyncio.gather(*(_one(it) for it in items))

    notes = info.get("complianceNotes") or {}
    by_name = {e.get("file"): e for e in (notes.get("files") or []) if isinstance(e, dict)}
    ok_files, fail_files = [], []
    for r in results:
        if not r.get("ok"):
            fail_files.append(r["file"])
            await emit({"type": "manual_check", "stage": "clean_images",
                        "message": f"{r['file']} 清理未成功（仍用原图，不影响流程）：{r.get('why')}"})
            continue
        ok_files.append(r["file"])
        # 产物顶替原文件：⑥⑦ 都按 main-NN 文件名找图（vision._main_files 的 _IMG_RE），
        # 直接覆盖原图最省事——原图在 cleaned/ 外已被 edit_image 读过，
        # 且 raw.json 里留着源 URL，需要时能重新下载。
        shutil.copy(r["path"], os.path.join(ctx["workdir"], r["file"]))
        e = by_name.get(r["file"])
        if e is not None:
            e.update({"clean": True, "chinese": False, "watermark": False, "logo": False,
                      "cleaned": True, "note": "AI 清理后质检通过"})
    if by_name:
        notes["files"] = [by_name[k] for k in sorted(by_name)]
        notes["cleanFiles"] = sorted(k for k, v in by_name.items() if v.get("clean"))
        info["complianceNotes"] = notes
        _save_info(ctx["info_path"], info)

    note = f"清理 {len(ok_files)}/{len(items)} 张"
    if fail_files:
        note += f"（未成功：{'、'.join(fail_files)}，按原图继续）"
    return {"status": "ok", "note": note}


async def _st_clean_images(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑤b 图片清理：预热已经清过就直接复用它的结论，否则现在清。

    【为什么这一步的预热结果能整体复用，而 ⑥⑦ 不能】清理的产物是磁盘上的文件和
    product-info.json 里的标注，两者都已落盘、与页面无关；而 ⑥⑦ 的判断要读【清理之后】
    的标注，提前跑就会读到旧值（详见 _st_material 里那段）。所以正确的提前量是把这一步
    整段挪早，让 ⑥⑦ 留在原位读它的成果。
    """
    done = await _await_prewarm(ctx, "clean_images")
    if done:
        note = done.get("note") or ""
        logger.info(f"图片清理沿用提前预热的结果：{note}")
        return {**done, "note": (note + "（已在采集后提前完成）")[:200]}
    return await _clean_main_images(ctx, emit)


async def _st_material(ctx: dict, session: BrowserSession, emit) -> dict:
    info = _load_info(ctx["info_path"])
    # 【⑥⑦ 刻意不预热】pick_material 在 complianceNotes 非空时完全不看图、只按标注选
    # （见 vision.pick_material），而那份标注正是 ⑤b 清理改写的对象：在 ① 之后预热拿到的
    # 是「无干净图 → 兜底取最不脏的一张 + uncertain」，等于把 ⑤b 的成果作废、还多报一次
    # 人工确认。plan_skc 的单色分支同样按 clean 排序决定首位主图。
    # 提速改由「把 ⑤b 清理整段提前」实现（见 _start_prewarm）：生图与 ②③ 重叠，而这里
    # 保持现场判断、读到的是清理后的标注。
    plan = await vision.pick_material(info, ctx["workdir"])
    if plan.get("status") != "ok":
        return {"status": "fail", "note": plan.get("reason") or "无可用素材图"}
    if plan.get("uncertain"):
        await emit({"type": "manual_check", "stage": "material",
                    "message": f"素材图选择没把握：{plan.get('reason')}（已用 "
                               f"{os.path.basename(plan['image'])} 继续）"})
    sq = images.square_image(plan["image"],
                             out_path=os.path.join(ctx["workdir"], "material-square.jpg"))
    r = await set_material(session, sq["output"])
    if r.get("status") != "ok":
        return {"status": "fail", "note": f"替换失败[{r.get('stage')}]: {str(r)[:150]}"}
    return {"status": "ok",
            "note": f"{os.path.basename(plan['image'])} → {sq['outSize']}（{plan.get('reason') or ''}）"[:200]}


async def _skc_size_fallback(ctx: dict, session: BrowserSession, emit,
                             colors: list, done_rows: list) -> list:
    """尺寸兜底：把没换成图的颜色行里破线的图，就地下载 + fit_34 + 整行替换。

    【为什么需要这一层】阶段⑦ 的换图靠视觉判「哪张图属于哪个颜色」，判不出来时整行
    被跳过（plan_skc 把该色塞进 uncertain_rows，_st_skc 只对 rows 换图）。于是那一行
    留在页面上的就是 1688 原始图，往往低于服装类下限 1340×1785，一路带到阶段⑫ save
    报「服装类图片尺寸不能小于 1340px * 1785px」——而这个报错页面是静默的，只有区块
    变红，极难定位到是哪一行的哪张图。

    2026-08-24 真站取证（rowid 173539495453435641）：两个颜色里粉红色换图成功
    （1340×1787），咖啡色因视觉分不出图被跳过，6 张全是 cbu01.alicdn.com 的
    1000×1000 / 1200×1200，保存被拦。状态文件当时如实记着「1/2 行完成（失败：咖啡色）」
    ——阶段没骗人，是失败后没人兜底。

    【内容判断失败 ≠ 尺寸可以不管】这与描述图那条兜底（plan_desc 的 needsUpscale）
    同源：颜色归属判不出来是内容问题，尺寸达标是平台硬校验，两者正交。这里不试图
    重新判归属（那正是失败的那一步），只把该行【现有的】图原地做合规化——画面一张
    不换、顺序一张不动，只补像素与比例。

    只在【纯增益】方向动手：读不到尺寸、行里图本来就达标、或下载/合规化失败，
    一律保持原样并报人工确认，绝不把行搞成空的。
    """
    fixed = []
    for kw in colors:
        st = await _skc_row_state(session, kw)
        if st.get("err"):
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行读不到状态，尺寸兜底跳过：{st['err']}"})
            continue
        small = st.get("tooSmall") or []
        # 【张数不足不在这里补，别加 count < 下限 的判据】本兜底只把该行现有图下载重做
        # 合规化后原样重挂，张数一张不增——对「行内只有 1~2 张」毫无帮助，白跑一轮。
        # 张数补齐必须在换图之前用同款其它主图凑（见 _pad_row_images），这里只管尺寸。
        # 另外行内图数不足时同样【要】报出来，否则保存被拦时无从定位，见下方 thin 分支。
        if kw in done_rows:
            continue                      # 已经换过图的行不动（尺寸由上传闸门保证）
        if (st.get("count") or 0) < SKC_ROW_MIN_IMAGES:
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行只有 {st.get('count')} 张图，低于每行下限 "
                                   f"{SKC_ROW_MIN_IMAGES} 张，保存会被拦（请人工补图）"})
        if not small:
            continue                      # 该行图都达标，不必动
        # 该行【全部】图都要重做：整行替换是「挂新图再删旧图」，只补破线那几张会让
        # 达标的旧图被一并删掉（skc_replace_row 的语义是整行换）
        urls = st.get("urls") or []
        prep = os.path.join(ctx["workdir"], f"skc-fix-{kw}")
        shutil.rmtree(prep, ignore_errors=True)
        os.makedirs(prep, exist_ok=True)
        ok_files = 0
        for i, u in enumerate(urls, 1):
            dst = os.path.join(prep, f"{i:02d}.jpg")
            try:
                extract._download_image(u, dst)
                images.fit_34(dst, out_path=dst)   # 3:4 + ≥1340×1785，两条硬规则一起满足
                ok_files += 1
            except Exception as e:
                logger.warning(f"「{kw}」行第 {i} 张兜底处理失败，跳过该张：{e}")
        if not ok_files:
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行 {len(small)} 张图低于 "
                                   f"{images.CLOTH_MIN_W}x{images.CLOTH_MIN_H}，"
                                   "但一张都没处理成功，仍是原图（保存会被拦）"})
            continue
        r = await skc_replace_row(session, kw, prep)
        if r.get("status") == "ok":
            fixed.append(kw)
            logger.info(f"「{kw}」行尺寸兜底完成：{ok_files} 张重做合规化"
                        f"（原有 {len(small)} 张破线）")
        else:
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行尺寸兜底替换失败：{str(r)[:120]}"})
    return fixed


def _pad_row_images(picked: list, info: dict, workdir: str) -> tuple:
    """把某颜色行的选图补到每行下限，返回（补齐后的路径列表, 补进来的文件名）。

    【为什么必须补】平台要求每行 3~10 张，而视觉按颜色归属给每行只分到 1~2 张是
    常态——一件衣服的某个颜色不会有 6 张独立照片（实测 product-985713733384：
    8 张主图分 4 个颜色，每行 1~2 张）。不补齐就是换完保存被拦，而那个报错是静默的
    （只有区块变红）。

    【补什么】同款其它主图：平铺、细节、材质图这类不体现颜色差异的图，挂在任何
    颜色行下都说得通，这也是人工发布时的做法。优先干净图（无水印/无中文），
    排除重复图与尺码表/工厂图这类非商品图。

    【顺序】原选图在前、补进来的在后——首位仍是该颜色的主图，符合
    skc_replace_row「按文件名排序挂图」的约定（调用方会重命名成 main-NN）。
    """
    if len(picked) >= SKC_ROW_MIN_IMAGES:
        return picked, []
    notes = vision._notes_by_file(info)

    def _note(path: str) -> dict:
        return notes.get(os.path.basename(path)) or {}

    have = set(picked)
    cands = [p for p in vision._main_files(workdir)
             if p not in have
             and not _note(p).get("duplicate")
             and (_note(p).get("kind") or "") not in vision._SKIP_KINDS]
    # 干净图优先，其余按脏度——与 plan_skc 单色分支同一套排序取向
    cands.sort(key=lambda p: (not _note(p).get("clean"), vision._dirty_score(_note(p))))
    added = cands[:SKC_ROW_MIN_IMAGES - len(picked)]
    return picked + added, [os.path.basename(p) for p in added]


async def _st_skc(ctx: dict, session: BrowserSession, emit) -> dict:
    info = _load_info(ctx["info_path"])
    plan = await vision.plan_skc(info, ctx["workdir"])
    rows = plan.get("rows") or []
    colors = [c for c in (info.get("colors") or []) if c]
    if not rows:
        await emit({"type": "manual_check", "stage": "skc",
                    "message": "视觉未给出任何颜色行选图，SKC 颜色图保持原样"})
        # 一行都没换 ≠ 尺寸不用管：留在页面上的 1688 原始图往往破线，
        # 阶段⑫ save 会被静默拦下（见 _skc_size_fallback）
        fx = await _skc_size_fallback(ctx, session, emit, colors, [])
        if fx:
            return {"status": "ok",
                    "note": f"未换图，但按尺寸兜底重做了 {len(fx)} 行：{'、'.join(fx)}"}
        return {"status": "skipped", "note": plan.get("reason") or "无可替换行"}
    # skipped_rows：续跑时页面上已经是本轮图片、无需重换的行。它必须与 ok_rows 一起
    # 传给尺寸兜底——跳过的行图是达标的，再被兜底重做一遍就白干了。
    ok_rows, fail_rows, skipped_rows = [], [], []
    for row in rows:
        kw = row["keyword"]
        if row.get("uncertain"):
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行的图片归属判断没把握，已按最优猜测继续"})
        # 按 skc_replace_row「文件名排序挂图」约定：主图命名 main-01.jpg 落首位、免拖拽
        prep = os.path.join(ctx["workdir"], f"skc-{kw}")
        os.makedirs(prep, exist_ok=True)
        for old in os.listdir(prep):
            op = os.path.join(prep, old)
            # 上一轮 batch_fit34 的产物 skc-34 就落在 prep 里，是子目录：
            # os.remove 删目录在 Windows 抛 PermissionError（WinError 5）导致整阶段挂掉；
            # 而且旧产物不清干净，本轮颜色行图数变少时残留旧图会被 skc_replace_row 一并挂上
            if os.path.isdir(op):
                shutil.rmtree(op, ignore_errors=True)
            else:
                os.remove(op)
        # 补到下限 3 张：视觉按颜色只分到 1~2 张是常态，不补齐换完保存会被静默拦下
        picked, padded = _pad_row_images(row["images"], info, ctx["workdir"])
        if padded:
            logger.info(f"「{kw}」行只分到 {len(row['images'])} 张，"
                        f"补 {len(padded)} 张同款图到下限：{padded}")
            await emit({"type": "log", "stage": "skc",
                        "message": f"「{kw}」行按颜色只分到 {len(row['images'])} 张，"
                                   f"补 {len(padded)} 张同款图凑够每行下限 "
                                   f"{SKC_ROW_MIN_IMAGES} 张"})
        if len(picked) < SKC_ROW_MIN_IMAGES:
            # 全库可用图都不够 3 张，补不上来。这属于源商品图太少，只能人工处理
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行只有 {len(picked)} 张可用图，"
                                   f"补不到每行下限 {SKC_ROW_MIN_IMAGES} 张，"
                                   "换图跳过（保存会被拦，请人工补图）"})
            fail_rows.append(kw)
            continue
        for i, src in enumerate(picked, 1):
            shutil.copy(src, os.path.join(prep, f"main-{i:02d}.jpg"))
        fitted = images.batch_fit34(prep)

        # 续跑跳过：上一轮换成功时把 fileId 清单落进了 ctx["skc_done"]，
        # 若页面上就是那一批图（数量/托管/尺寸/逐个 fileId 全中），本行不必重换。
        # 判据刻意要求有清单——宽判据认不出「张数相同但内容不是这一批」，
        # 误判会把错图留在页面上，代价高于白跑一轮，理由见 _skc_row_matches。
        want = len([f for f in sorted(os.listdir(fitted["outdir"]))
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))])
        prev_ids = (ctx.get("skc_done") or {}).get(kw) or []
        live = await _skc_row_state(session, kw)
        if not live.get("err"):
            m = _skc_row_matches(live, prev_ids, want)
            if m["done"]:
                skipped_rows.append(kw)
                logger.info(f"「{kw}」行跳过换图：{m['reason']}")
                await emit({"type": "log", "stage": "skc",
                            "message": f"「{kw}」行已是本轮图片，跳过换图（{m['reason']}）"})
                continue
            if prev_ids:
                logger.info(f"「{kw}」行需重换：{m['reason']}")

        r = await skc_replace_row(session, kw, fitted["outdir"])
        if r.get("status") == "ok":
            ok_rows.append(kw)
            # fileId 清单落进 ctx，由 run_product 的回写逻辑持久化，供下轮判跳过
            ctx.setdefault("skc_done", {})[kw] = r.get("fileIds") or []
        else:
            fail_rows.append(kw)
            # 换图失败的行上一轮的清单已经不作数了（行内是新旧混杂的中间态），
            # 留着会让下一轮拿旧清单去比对——比不中而已，但清掉更诚实
            (ctx.get("skc_done") or {}).pop(kw, None)
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行换图失败：{str(r)[:120]}"})
    # 没换成图的行（视觉分不出归属、或换图失败）仍可能留着破线的原始图：
    # 内容判断失败不等于尺寸可以不管，这里只补像素与比例，画面一张不换。
    # 已跳过的行同样算「已完成」，不能再被兜底重做一遍。
    fixed = await _skc_size_fallback(ctx, session, emit, colors, ok_rows + skipped_rows)

    note = f"{len(ok_rows) + len(skipped_rows)}/{len(rows)} 行完成"
    if skipped_rows:
        note += f"（其中 {len(skipped_rows)} 行已是本轮图片、跳过：{'、'.join(skipped_rows)}）"
    if fail_rows:
        note += f"（失败：{'、'.join(fail_rows)}）"
    if fixed:
        note += f"；尺寸兜底重做 {len(fixed)} 行：{'、'.join(fixed)}"
    return {"status": "ok", "note": note}


async def _st_fix_sizes(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑧ 尺码勾选。非服装类目没有尺码维，此时 fix_sizes 返回 skipped 而不是失败。

    【为什么 skipped 要原样透传】2026-08-28 真站取证（1014675972015 手工编织水果花束
    摆件，类目「仿真花」）：该类目的变种属性区只有「颜色」一维，6 个复选框全是颜色、
    且已与源 SKU 对上，变种表也已生成——⑧ 对它本就无事可做。把这种情形算失败会让
    整单永远卡在⑧（先是报「无 skus 数据」，补了均码后改报「源尺码在页面选项里全部
    不存在」，换个说法而已）。详见 pipeline._JS_SIZE_GROUP_PRESENCE 上方的取证记录。
    """
    r = await fix_sizes(session, ctx["info_path"])
    if r.get("status") == "skipped":
        # 记一条 log 事件：这一跳是正常的，但要让人在进度里看见「为什么⑧没做事」
        await emit({"type": "log", "stage": "fix_sizes",
                    "message": r.get("reason") or "本类目无尺码维，跳过尺码勾选"})
        return {"status": "skipped", "note": (r.get("reason") or "")[:200]}
    if r.get("status") != "ok":
        return {"status": "fail", "note": (r.get("reason") or "")[:200]}
    return {"status": "ok",
            "note": f"源尺码 {len(r.get('wantedSizes') or [])} 个 | SKU 表 {r.get('rowCount')} 行"}


async def _st_sizechart(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑨ 尺码表：套装商品要填两张（「尺码表」+「尺码表2」），且两张分类要分工。

    【为什么按 SKU分类判、且要在这里就判】2026-08-27 商品 1051793179451（两件套裙套装）
    发布被平台打回「套装尺码模板数量不合法 / 您发布的产品是套装，尺码表2也需要设置」。
    这个校验只在服务端做——同日实测把 SKU分类下拉在三档间切换，尺码表2 的 label 始终
    没有 required 类、控件文案也不变，前端一点提示都没有（与包装清单件数和同性质），
    所以页面上读不出「要不要填第二张」，只能靠我们自己的套装判断。

    判据取【预热好的 SKU分类】：本阶段（⑨）在 ⑪ set_stock 之前，页面上的 SKU分类下拉
    此刻还是认领时的默认值、读它没意义；而预热在 ① 之后就把这个判断算完了（与 ②③
    并行，见 _run_prewarm），值与 ⑪ 现场用的完全一致。预热失败取不到时按单件走——
    多填一张空表会让非套装商品也被平台按套装校验，比漏填更难查。

    【两张表的分类按包装清单的件别分派】同日实测：弹窗的尺码分类预选值两张都是
    「女童装-半身裙」，跟随预选会让两张表的测量参数都是裙长/腰围全围——数量校验能过，
    但上衣那件量的是衣长/胸围，等于给买家一份错尺码表。包装清单已经把件别判出来了
    （便服上衣 + 半身裙），拿它去选分类正好，见 pipeline._size_category_for。
    """
    judge = await _await_prewarm(ctx, "stock") or {}
    # skuCat 2=同款多件 3=混合套装 都算套装（平台按「不止一件」判，不区分同款与否）
    is_set = str(judge.get("skuCat", "1")) in ("2", "3")
    # 包装清单的件别（已归一到平台词表，见 _normalize_sku_judge）；顺序即两张表的顺序
    packing = [x.get("name", "") for x in (judge.get("packing") or [])]
    cats = [_size_category_for(n) for n in packing]

    r = await add_sizechart(session, ctx["info_path"],
                            category=cats[0] if cats else None)
    if r.get("status") != "ok":
        # 【第一张表栏都没有 → 本类目不要尺码表，skipped 而不是失败】原先只对
        # 「尺码表2」缺栏宽容（下方那个分支），第一张缺栏仍算 fail。2026-08-28 真站
        # 取证（1014675972015 仿真花）：非服装类目整页 32 个 label 里没有任何带「尺」
        # 的项，连第一张表栏都不存在，于是⑨ 紧接⑧ 再挂一次。缺栏是平台按类目决定的
        # 事实，不是我们填失败——与⑧ 的无尺码维同一性质。
        if r.get("reason") == "no-sizechart-item":
            await emit({"type": "log", "stage": "sizechart",
                        "message": "本类目没有尺码表栏（非服装类目），跳过尺码表"})
            return {"status": "skipped", "note": "本类目无尺码表栏"}
        return {"status": "fail", "note": (r.get("reason") or "")[:200]}
    if r.get("skipped"):
        note = f"已存在：{r.get('current')}"
    else:
        # 模型估算过的参数列单独点出来：这几列不是源实测值，人工复核时要优先看
        est = r.get("estimated") or []
        note = (f"模板 {r.get('tplName')} | 分类 {r.get('category')}"
                f" | 参数 {len(r.get('params') or [])} 项")
        if est:
            note += f" | 模型估算 {'、'.join(est)}"

    if not is_set:
        return {"status": "ok", "note": note}

    # 套装：补第二张表（分类取清单第二件；同款多件时两件相同，跟随第一件）
    cat2 = cats[1] if len(cats) > 1 else (cats[0] if cats else None)
    r2 = await add_sizechart(session, ctx["info_path"], which=1, category=cat2)
    if r2.get("status") != "ok":
        if r2.get("reason") == "no-sizechart-item":
            # 该类目只有一张表栏：平台不会按套装校验第二张，不算失败
            await emit({"type": "log", "stage": "sizechart", "level": "warning",
                        "message": "判为套装但页面没有「尺码表2」栏，跳过第二张"})
            return {"status": "ok", "note": note + " | 无尺码表2栏"}
        await emit({"type": "manual_check", "stage": "sizechart",
                    "message": f"尺码表2 未加成（套装商品平台会打回）：{r2.get('reason')}"})
        return {"status": "fail", "note": f"尺码表2 失败：{(r2.get('reason') or '')}"[:200]}
    note += (f" | 尺码表2 已存在：{r2.get('current')}" if r2.get("skipped")
             else f" | 尺码表2 模板 {r2.get('tplName')}")
    return {"status": "ok", "note": note}


async def _st_sku_code(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑩a：把 SKU 货号重写成纯 ASCII（平台不收中文/中文符号，见 pipeline 侧注释）。

    排在 ⑩ variant 之前只为读的行序稳定：⑧ 已把尺码勾好、行也生成完了，这里读一次
    颜色/尺码就填，中间不点任何东西。放到 ⑩ 之后也能跑，纯粹是没必要多等一次渲染。
    不读 product-info.json：源 SKU 名与页面行对不上（真站取证见 pipeline.fix_sku_codes），
    页面自己的颜色/尺码两列才是唯一可信的行标识。
    """
    r = await fix_sku_codes(session)
    if r.get("status") == "error":
        return {"status": "fail", "note": (r.get("reason") or "")[:200]}
    if r.get("status") == "validation-error":
        await emit({"type": "manual_check", "stage": "sku_code",
                    "message": f"部分行货号未通过回读校验：{str(r.get('bad') or r.get('mismatch'))[:150]}"})
    tr = r.get("translated") or {}
    note = f"{r.get('rowCount')} 行 | 首个 {(r.get('codes') or [''])[0]}"
    if tr:
        note += " | 译 " + "、".join(f"{k}→{v}" for k, v in list(tr.items())[:4])
    return {"status": "ok", "note": note}


async def _st_variant(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑩ 变种信息：申报价与包裹尺寸都在这里落地。

    price 走批次级参数（UI 输入框 / CLI --price），空串直接透传——归一与退默认
    统一由 pipeline.normalize_declare_price 负责，这里不再兜一层默认值（两处都写
    默认值，改口径时必漏一处）。
    cat_path 传下去只为判服装类（服装包裹尺寸固定 30x25x3，不问模型），
    续跑时 cat_path 从状态文件回填、拿不到就退到标题判定。
    """
    r = await set_variant(session, ctx["info_path"],
                          price=ctx.get("price") or "",
                          cat_path=ctx.get("cat_path"),
                          pack_est=await _await_prewarm(ctx, "variant"))
    if r.get("status") == "error":
        return {"status": "fail", "note": (r.get("reason") or "")[:200]}
    note = (f"{r.get('rowCount')} 行 | 申报价 {r.get('price')} | "
            f"尺寸 {'x'.join(r.get('dims') or [])}cm | 重量 {r.get('weight')}g")
    if r.get("status") == "validation-error":
        await emit({"type": "manual_check", "stage": "variant",
                    "message": f"变种信息部分行未通过回读校验：{str(r)[:120]}"})
    return {"status": "ok", "note": note}


async def _st_stock(ctx: dict, session: BrowserSession, emit) -> dict:
    r = await set_stock(session, ctx["info_path"],
                        sku_judge=await _await_prewarm(ctx, "stock"))
    if r.get("status") == "error":
        return {"status": "fail",
                "note": f"[{r.get('stage')}] {(r.get('reason') or r.get('err') or '')}"[:200]}
    if r.get("status") == "validation-error":
        await emit({"type": "manual_check", "stage": "stock",
                    "message": f"库存部分行未通过回读校验：{str(r)[:120]}"})
    sc = r.get("skuCategory") or {}
    pk = "、".join(f"{x.get('name')}x{x.get('qty')}" for x in (r.get("packing") or [])) or "无"
    return {"status": "ok",
            "note": f"仓库 {r.get('warehouse')} | 处理 {r.get('processed')} 行"
                    f" | SKU分类 {sc.get('cat')} x{sc.get('qty')} | 包装清单 {pk}"}


async def _st_shipping(ctx: dict, session: BrowserSession, emit) -> dict:
    r = await set_shipping(session)  # 不给 deadline：按 SKILL.md 规则选最长时效
    if r.get("status") != "ok":
        return {"status": "fail",
                "note": f"[{r.get('stage')}] {(r.get('reason') or '')}"[:200]}
    return {"status": "ok",
            "note": f"时效 {r.get('deadline')} | 模板 {r.get('freightTemplate')}"}


def _desc_cache_paths(workdir: str, url: str) -> tuple:
    """描述图英化产物的本地缓存路径（原图、英化图），按【源 URL】哈希命名。

    【为什么不用 pos 当键】pos 是描述区里的当前序号，删图后整体前移——同一个
    pos-03 下次可能是另一张图，拿旧产物去替换会张冠李戴。源 URL 是稳定标识。

    2026-08-23 换成 URL 哈希前，产物是 desc-edit/pos-NN-edited.jpg。那批旧文件
    认领不回来（pos 是删图后的序号，反推不出源 URL），会被这里的缓存判定忽略、
    也不会被清理；靠猜的映射把 A 图的产物贴到 B 图上，比重烧一次生图糟得多。
    需要腾空间时人工删 desc-edit/pos-*.jpg 即可。
    """
    import hashlib

    # 【英化产物的扩展名必须是 .jpg】edit_image 收尾会 compress()，它把非 jpg 输入
    # 转成 JPEG q80（控图床体积）并【删掉原 png】。若这里按 -en.png 探测缓存，
    # 那个路径永远不存在，缓存一次都不会命中——白烧生图还看不出问题。
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    base = os.path.join(workdir, "desc-edit")
    return os.path.join(base, f"{h}.jpg"), os.path.join(base, f"{h}-en.jpg")


async def _resolve_desc_pos(session: BrowserSession, url: str) -> tuple:
    """按源 URL 查它在描述区【当前】的序号，返回 (pos, err, fatal)。

    fatal=True 表示这个失败对后续每一张都成立（页签被导航走了），调用方该收工而不是
    逐张重试——判据取 desc_map 带回的 navigatedAway 标志而不是错误文案，文案会被截断
    也会被改写（见 _desc_ensure_open 里那段实测记录）。

    【为什么必须重查】plan_desc 出的 pos 是删图【之前】的序号，desc_delete 一执行
    描述区就整体前移，旧 pos 全部失效：越界的报错、没越界的静默替换到别的模块上
    （2026-08-24 实测：删 3 张后按旧 pos 6 替换，描述区只剩 5 个模块）。
    URL 是稳定标识——这与 _desc_cache_paths 用 URL 哈希当缓存键是同一个理由。

    每张替换前都重查一次而不是删完只重查一次：替换本身也会改 src，逐张重查最贴近
    页面实况，代价只是一次 evaluate，与生图开销相比可忽略。
    """
    m = await desc_map(session)
    if m.get("status") != "ok":
        return 0, (m.get("err") or "desc_map 失败")[:200], bool(m.get("navigatedAway"))
    hits = [x["pos"] for x in (m.get("modules") or []) if x.get("url") == url]
    if not hits:
        return 0, "描述区已找不到这张源图（可能已被删除或已替换）", False
    # 同一 URL 出现多次时取最小序号：重复图本该被 plan_desc 判 delete，真漏了也
    # 只是先替换靠前那张，下一轮重查会落到剩下那张，不会错位到别的图上
    return hits[0], "", False


async def _prepare_desc_image(workdir: str, rep: dict) -> dict:
    """把一张待替换的描述图备好本地产物，返回 {"ok", "path", "how", "why"}。

    how ∈ cached（复用落盘产物）/ upscaled（纯几何放大）/ edited（生图英化）。
    三条分支的判据与取舍原样保留自原 _st_desc 内联实现——【不要在这里重新发明】：
    needsUpscale 走 compress 不烧生图、质检未过必须删产物、产物一律落 en_path
    （那是缓存键），每一条都是踩过坑换来的，理由见各分支注释。

    抽成独立函数【只为了能并发预热】：本函数不碰浏览器页面（输入是源 URL、输出是
    本地文件），故 N 张可以同时跑；而定位序号与替换必须逐张串行（见
    _resolve_desc_pos）。原实现把两者写在同一个循环里，生图就只能一张一张来——
    单张实测约 35s，10 张串行 350s，是 ⑬ 阶段耗时的主体（2026-08-25 状态文件实测
    971877978455 该阶段 792s）。

    同步阻塞调用（下载/生图/压缩都是 requests 与 curl 子进程）一律过 to_thread：
    并发跑时若直接调用会把事件循环整个占住，等于白并发（⑤b 清理已是这个写法）。
    """
    local, en_path = _desc_cache_paths(workdir, rep["url"])
    if os.path.exists(en_path) and os.path.getsize(en_path) > 0:
        # 缓存命中不再重复质检：check_cleaned 也是一次视觉调用，而落盘的前提就是它已通过
        return {"ok": True, "path": en_path, "how": "cached"}

    if rep.get("needsUpscale"):
        # 【只缺像素的图走纯几何放大，不烧生图】plan_desc 判 needsUpscale 的图内容
        # 是干净的（模型本来判 keep），只是尺寸不符合描述图要求过不了保存校验。
        # 走 compress 放大即可：gpt-image-2 每张都是一次付费调用，为「像素不够」
        # 去重画一遍画面既贵又可能改坏内容。也因此不需要 check_cleaned 质检——
        # 画面根本没动过。
        try:
            await asyncio.to_thread(extract._download_image, rep["url"], local)
            # 【产物必须落到 en_path】那是缓存键（见 _desc_cache_paths）。若就地
            # 改 local，重跑时 cached 判定看不到产物，每轮都要重新下载再放大一次。
            shutil.copy(local, en_path)
            # 【下限传描述图的 480，不能用 compress 的服装默认值 1340x1785】
            # 2026-08-28：不传的话这里会把 1000x1000 硬放大到 1340x1785，既无必要
            # （描述图只要两边 >= 480）又会插值放大糊掉画面、体积还涨。
            out = await asyncio.to_thread(
                images.compress, en_path, quality=88,
                min_w=images.DESC_MIN_W, min_h=images.DESC_MIN_H)
        except Exception as e:
            return {"ok": False, "why": f"放大失败：{e}"[:150]}
        return {"ok": True, "path": out, "how": "upscaled",
                "note": f"{rep.get('reason')} -> {images.image_size(out)}"}

    try:
        # 同包复用，带过浏览器头的下载
        await asyncio.to_thread(extract._download_image, rep["url"], local)
    except Exception as e:
        return {"ok": False, "why": f"英化失败：下载原图 {e}"[:150]}

    # 【质检未过要再烧一发】生图有随机性，同一张图同一个提示词两发结果就不同：
    # 2026-08-26 实测 700640528493 那张 749×513 的面料细节图，第一发被判
    # 「残留英文品牌字 QSMYSTYLE」（衣服上的实物刺绣被质检当成待清理的品牌字），
    # 重烧一发就把底部整条中文说明去干净、刺绣完好、质检通过。一发不中就放弃的代价
    # 不只是这张图退回原图，还连带撞 1340×1785 闸门、在描述区留下 1688 外链
    # （日志末尾那两条 manual_check 就是这么来的）。
    #
    # 【发数按失败类型分档】重试能救的是随机性，救不了确定性失败（字太密、嵌在花纹里
    # 译不干净），而 gpt-image-2 每发都是一次付费生图，故不能一律多烧：
    #   - 残留中文 / 生图乱码 → DESC_QC_TRIES_TEXT 发。中文是 Temu 最硬的红线，退回原图的代价是
    #     连带撞 1340×1785 闸门、在描述区留下 1688 外链（2026-08-26 那批日志末尾
    #     「仍有外链图未转存」+「三张破线」就是三张图退回原图的后果，不是独立故障）。
    #   - 其它 issues（修图痕迹之类）→ DESC_QC_TRIES 发，多烧是同样结果。
    last_issues, cjk_left = "", False
    attempt, tries = 0, DESC_QC_TRIES
    while attempt < tries:
        attempt += 1
        # 【重试要加码提示词，不能原样再发一遍】残留中文说明上一发没把那块文案吃掉，
        # 同样的话再说一次只是赌随机性；把「上一发残留了什么」当成新约束喂回去，
        # 命中率明显高于原样重发（同 llm._JSON_RETRY_HINT 的取向）。
        prompt = None
        if attempt > 1 and last_issues:
            prompt = images.DEFAULT_CLEAN_PROMPT + _retry_hint(last_issues, cjk_left)
        try:
            # desc_mode：按描述图口径出图与收尾，不套服装 1340x1785 闸门
            # （见 images.edit_image 的 desc_mode 说明）
            ed = await asyncio.to_thread(images.edit_image, local, prompt=prompt,
                                         out_path=en_path, desc_mode=True)
            qc = await vision.check_cleaned(ed["output"])
        except Exception as e:
            return {"ok": False, "why": f"英化失败：{e}"[:150]}
        if qc.get("clean"):
            return {"ok": True, "path": ed["output"], "how": "edited"}
        # 质检未过的产物必须删掉：留着会被下次重跑（以及下一轮重试）当成
        # 「已通过的缓存」复用——en_path 就是缓存键，见 _desc_cache_paths
        last_issues = qc.get("issues") or ""
        cjk_left = bool(qc.get("residualChinese"))
        # 文字层没清干净（中文残留 or 生图吐了乱码）都给到 DESC_QC_TRIES_TEXT 发，
        # 理由见该常量注释。发数在循环里抬而不是一开始就取大值：只有确实是这两类才
        # 多烧，brokenSubject 照旧 2 发。
        if cjk_left or qc.get("garbled"):
            tries = max(tries, DESC_QC_TRIES_TEXT)
        try:
            os.remove(ed["output"])
        except OSError:
            pass
        if attempt < tries:
            logger.info(f"描述图英化质检未过（{attempt}/{tries}"
                        f"{'，残留中文' if cjk_left else ''}），重烧一发："
                        f"{last_issues[:60]}")
    return {"ok": False, "why": f"英化质检未过：{last_issues}"[:150],
            "residualChinese": cjk_left}


async def _prewarm_desc_images(workdir: str, replace_plan: list, emit) -> dict:
    """并发把 replace 计划里每张图的本地产物备好，返回 {url: _prepare_desc_image 结果}。

    【为什么值得单开一轮】生图与页面完全无关，而替换必须串行。先并发烧完再串行替换，
    墙钟从「N × 单张耗时」压到「单张耗时 + N 次替换」。并发数取用户配置的生图并发
    （与 ⑤b 同一个旋钮，本质是同一个 gpt-image-2 端点，见 get_image_concurrency）。

    【best-effort】某张备料失败只记原因，由调用方按原有的 manual_check 路径报出来、
    保留页面原图；本函数不抛，一张烧不出来不该让整个 ⑬ 阶段失败。
    """
    if not replace_plan:
        return {}
    conc = get_image_concurrency()
    sem = asyncio.Semaphore(conc)

    async def _one(rep: dict) -> tuple:
        async with sem:
            try:
                return rep["url"], await _prepare_desc_image(workdir, rep)
            except Exception as e:
                # _prepare_desc_image 内部已分支吞异常，这里只兜住意料外的（如磁盘满）
                return rep["url"], {"ok": False, "why": f"备料异常：{e}"[:150]}

    logger.info(f"描述图备料：{len(replace_plan)} 张待处理（并发 {conc}）")
    pairs = await asyncio.gather(*(_one(r) for r in replace_plan))
    out = dict(pairs)
    n_ok = sum(1 for v in out.values() if v.get("ok"))
    n_new = sum(1 for v in out.values() if v.get("how") in ("edited", "upscaled"))
    logger.info(f"描述图备料完成：{n_ok}/{len(replace_plan)} 张就绪"
                f"（新出图/放大 {n_new} 张，其余复用落盘产物）")
    await emit({"type": "log", "stage": "desc",
                "message": f"描述图备料完成 {n_ok}/{len(replace_plan)} 张"
                           f"（并发 {conc}，新处理 {n_new} 张）"})
    return out


async def _st_desc(ctx: dict, session: BrowserSession, emit) -> dict:
    m = await desc_map(session, ctx["info_path"])
    if m.get("status") != "ok":
        return {"status": "fail", "note": (m.get("err") or "desc_map 失败")[:200]}
    mods = m.get("modules") or []
    # 【不能在这里因「无图片」就跳过】mods 只统计图片模块，而描述区可能只放了文字
    # （尺码对照表之类）。原先在这里 return skipped，会让那种商品的文字完全不被处理：
    # 采集残留的垃圾 JSON 留在页面上、中文尺码表原样发到海外站。
    # 故图片为空只跳过图片处理，文字照跑；两者都没有才是真的 skipped（见下方护栏）。
    info_for_desc = _load_info(ctx["info_path"])

    # 【先处理文字模块，再处理图片】描述区是图文混排的，删文字模块会让 data-idx
    # 重排；而图片侧按源 URL 现查 pos（_resolve_desc_pos）、删除时内部重建 idx
    # 映射，不受影响。反序则要多读一遍 data-idx。
    # 文字模块两类真实样本：1688 关联商品 JSON 残留（删）、尺码对照表（英化）。
    text_note = ""
    try:
        tm = await desc_text_map(session)
        if tm.get("status") == "ok" and tm.get("texts"):
            tplan = await vision.plan_desc_text(tm["texts"], info_for_desc)
            acts = tplan.get("plan") or []
            if any(p.get("action") in ("translate", "delete") for p in acts):
                tr = await desc_text_apply(session, acts)
                n_tr = len(tr.get("translated") or [])
                n_del = len(tr.get("deleted") or [])
                text_note = f"文字模块 英化 {n_tr} / 删 {n_del}"
                if tr.get("failed"):
                    text_note += f" / 失败 {len(tr['failed'])}"
                    await emit({"type": "manual_check", "stage": "desc",
                                "message": f"文字模块处理有 {len(tr['failed'])} 项未成功"
                                           f"（已保留原文）：{str(tr['failed'])[:150]}"})
                logger.info(f"描述文字模块处理完成：{text_note}")
    except Exception as e:
        # 文字模块不是必填内容，处理不了就保留原文，不能拖垮整个描述阶段
        logger.warning(f"文字模块处理异常（保留原文，继续图片处理）：{e}")
        text_note = "文字模块处理异常"

    # 文字模块删除后图片模块的 data-idx 已重排，故 desc_map 要重读一次拿最新状态
    if text_note and "删 0" not in text_note and "异常" not in text_note:
        m2 = await desc_map(session, ctx["info_path"])
        if m2.get("status") == "ok" and m2.get("modules"):
            mods = m2["modules"]

    if not mods:
        # 只有文字模块：文字已处理完，保存收尾，不进图片分支
        if not text_note:
            return {"status": "skipped", "note": "描述区无模块"}
        sv = await desc_save(session)
        if sv.get("status") != "ok":
            return {"status": "fail", "note": f"desc_save 失败：{str(sv)[:150]}"}
        await ensure_desc_closed(session)
        return {"status": "ok", "note": text_note}

    # 预热的规划按【URL 集合是否一致】决定能否复用：预热是照 raw.json 的 descImages
    # 出的计划，而认领后描述区挂的就是那批 1688 外链（desc_save 的校验项「仍有外链图
    # 未转存」即此，2026-08-23 真站取证）。但上面的文字模块处理可能删掉模块、页面也
    # 可能被人动过，故不能假定两边一定相同——集合对不上就现场重出计划，pos 一律以
    # mods 为准（预热给的 pos 是源顺序，与页面序号可能差一截）。
    pre = await _await_prewarm(ctx, "desc")
    plan = None
    if pre and pre.get("plan"):
        page_urls = {m.get("url") for m in mods if m.get("url")}
        pre_urls = set()
        for p in pre["plan"].get("replace") or []:
            pre_urls.add(p.get("url"))
        for m in _desc_modules_from_raw(ctx["workdir"]):
            pre_urls.add(m["url"])
        if page_urls and page_urls <= pre_urls:
            plan = _replan_desc_by_url(pre["plan"], mods)
            logger.info(f"描述图规划沿用提前预热的结果（{len(mods)} 张按页面序号重挂）")
        else:
            logger.info("页面描述图与预热时不一致（源图有增减），现场重出规划")
    if plan is None:
        plan = await vision.plan_desc(mods, info_for_desc)
    deleted, replaced = 0, 0
    if plan["delete"]:
        d = await desc_delete(session, plan["delete"])
        if d.get("status") != "ok":
            return {"status": "fail", "note": f"删描述模块失败：{str(d)[:150]}"}
        deleted = len(d.get("deleted") or [])
    # 【英化产物按源 URL 落盘复用】gpt-image-2 每张都是一次生图调用，是本阶段最贵的
    # 一步。而 ⑬ 的成果只活在未保存的表单里，save 没成功就得整段重跑（见模块头
    # 「状态文件记的是跑过」那段）——重跑时若连图也重新生成，等于白烧一遍生图钱。
    # 故产物落 desc-edit/<url哈希>-en.jpg，存在且质检过就直接复用。
    # 缓存命中不再重复质检：check_cleaned 也是一次视觉调用，而落盘的前提就是它已通过。
    #
    # 【备料与替换分两段】生图不碰页面，替换必须逐张串行（要现查 pos）。故先并发把
    # 所有产物烧好（_prewarm_desc_images），下面的循环里每张都已在本地，只剩定位与
    # 替换两个页面动作。原先两者写在同一循环里，生图被迫串行——单张约 35s，是本阶段
    # 耗时的主体。备料放在 desc_delete 【之后】：删掉的图不在 replace 计划里，不会白烧；
    # 而备料不动页面，删除后的 pos 漂移对它没有影响。
    #
    # 【备料前必须按页面实况筛一遍】原实现「先定位再出图」不只是为了拿准 pos，还是一道
    # 省钱闸：源图已不在描述区时直接跳过，省掉一次 gpt-image-2 调用。前移备料若不筛，
    # 这道闸就失效了（tests/test_publish_service.py 的「源图已不在描述区时不生图」正是
    # 为此把守）。这里用刚读到的 mods 过滤——它就是页面实况，不必再多发一次 desc_map；
    # 下面循环里每张仍会各自重查一次 pos（删除后序号会前移，见 _resolve_desc_pos）。
    live_urls = {m.get("url") for m in mods if m.get("url")}
    to_prepare = [r for r in plan["replace"] if r.get("url") in live_urls]
    if len(to_prepare) != len(plan["replace"]):
        skipped_n = len(plan["replace"]) - len(to_prepare)
        logger.info(f"描述图备料跳过 {skipped_n} 张：源图已不在描述区（省掉同等次数的生图调用）")
    prepared = await _prewarm_desc_images(ctx["workdir"], to_prepare, emit)

    reused, upscaled = 0, 0
    # 【替换轮抽成闭包，为的是失败后能整轮重试】2026-08-28 实测（pdd-994437651298）：
    # 6 张计划替换的图【全部】卡在同一个页面遮挡上（「更换图片」链接被 fixed 顶栏压住），
    # 于是 6 张 1688 原图原样留在描述区 —— desc_save 回读既报「外链未转存」又报 6 张
    # 1200x1200 破了服装硬红线 1340x1785，而流程照旧放行，最后带着这些图发了出去。
    # 遮挡本身已在 pipeline 侧修（见 _desc_aim_replace_link），但那类页面态问题不可能
    # 一次穷尽；本项目的取向是「靠自动重试保数据可靠性，不加人工卡点」，故这里补一轮
    # 重试：重试前先关掉编辑器再重开，让面板与滚动位置回到初始态（同一态下重试没意义）。
    failed_urls: list = []
    # fatal 指「页签被导航走」这类对后续每一张都成立的错误。它必须抑制重试：页面已经
    # 不在编辑页，重开编辑器与重试都无从下手，只会把 test_fatal时中断整段而不是逐张重试
    # 盯着的那种噪音再来一遍（2026-08-24 实测 890843533224 的 7 条同样报错）。
    fatal_hit = False

    async def _replace_round(items: list) -> None:
        """把 items（plan["replace"] 的子集）逐张替换，累计计数并记下失败的源 URL。"""
        nonlocal replaced, reused, upscaled, fatal_hit
        failed_urls.clear()
        for rep_i, rep in enumerate(items):
            # 【先定位再替换】计划里的 pos 是删图前的序号，必须按源 URL 现查当前序号
            # （见 _resolve_desc_pos）。定位到 desc_replace 之间不会再有页面操作，故这个
            # 序号仍然有效；真有漂移也由 desc_replace 的 expect_url 闸门拦住。
            pos = rep["pos"]
            cur_pos, perr, fatal = await _resolve_desc_pos(session, rep["url"])
            if perr:
                # 【页签被导航走时立刻收工，别逐张重试】2026-08-24 实测（890843533224）：
                # 替换到第 11 张时页签被另一个进程导航去了草稿列表，剩下 7 张各自重试一次、
                # 各报一条同样的「定位失败」——7 条噪音掩盖了唯一的根因，而且每次重试都
                # 要重开编辑器、白等一轮。这类错误对后续每一张都成立，逐张试没有意义。
                if fatal:
                    fatal_hit = True
                    await emit({"type": "manual_check", "stage": "desc",
                                "message": f"{perr}；本阶段剩余 "
                                           f"{len(items) - rep_i}"
                                           f" 张全部保留，请恢复编辑页后从 desc 阶段续跑"})
                    break
                await emit({"type": "manual_check", "stage": "desc",
                            "message": f"原第 {pos} 张定位失败（保留原图）：{perr}"})
                continue
            if cur_pos != pos:
                logger.info(f"描述图序号前移：计划 pos {pos} -> 当前 pos {cur_pos}"
                            f"（已删 {deleted} 张）")
            # 报给人看的序号一律用当前序号，前移过的额外标出计划序号——只报计划 pos 会
            # 让人按它去页面上数图，数到的是另一张
            tag = (f"第 {cur_pos} 张" if cur_pos == pos
                   else f"第 {cur_pos} 张（计划 pos {pos}）")
            # 备料结果按源 URL 取（与 _desc_cache_paths 同一个键）。缺项出现在备料轮整个
            # 没跑起来、或该图当时不在页面上（被上面的 live_urls 筛掉）——两种情况都已经
            # 走到「定位成功」这一步了，说明此刻它确实在页面上，就地补一张即可。
            # 补料复用同一个函数，不另写一套逻辑（另写必然与备料轮漂移）。
            got = prepared.get(rep["url"])
            if got is None:
                got = await _prepare_desc_image(ctx["workdir"], rep)
            if not got.get("ok"):
                await emit({"type": "manual_check", "stage": "desc",
                            "message": f"{tag}{got.get('why')}（保留原图）"})
                continue
            out_img = got["path"]
            how = got.get("how")
            if how == "cached":
                reused += 1
                logger.info(f"描述图{tag}复用已有英化产物：{os.path.basename(out_img)}")
            elif how == "upscaled":
                upscaled += 1
                logger.info(f"描述图{tag}按尺寸放大：{got.get('note')}")
            rr = await desc_replace(session, cur_pos, out_img, expect_url=rep["url"])
            if rr.get("status") == "ok":
                replaced += 1
            else:
                failed_urls.append(rep["url"])
                await emit({"type": "manual_check", "stage": "desc",
                            "message": f"{tag}替换失败（保留原图）：{str(rr)[:120]}"})

    await _replace_round(plan["replace"])
    if failed_urls and not fatal_hit:
        # 【重开编辑器再重试】遮挡与选中态都是页面态，同一态下重试必然同样失败：
        # 2026-08-28 那次 6 张各自「重点一次 + 轮询 3.2s」全打在顶栏上，白等 40s。
        # 关掉重开会丢掉未保存的改动，故【必须先 desc_save 把已换成的落下来】。
        retry = [r for r in plan["replace"] if r["url"] in set(failed_urls)]
        logger.info(f"描述图有 {len(retry)} 张未替换成功，先保存已换成的再重开编辑器重试一轮")
        await emit({"type": "log", "stage": "desc",
                    "message": f"{len(retry)} 张未替换成功，重开描述编辑器重试一轮"})
        keep = await desc_save(session)
        if keep.get("status") == "error":
            # 保存不了就别关编辑器（关掉等于丢弃已换成的那几张），直接放弃重试
            logger.warning(f"重试前保存失败，放弃重试：{str(keep)[:150]}")
        else:
            closed = await ensure_desc_closed(session)
            if closed.get("status") != "ok":
                logger.warning(f"重试前关闭编辑器失败，仍尝试重试：{closed.get('reason')}")
            await _replace_round(retry)
    if not deleted and not replaced:
        # 【这条早退路径也必须先关编辑器】2026-08-28 实测（1014675972015 仿真花）：
        # 9 张描述图全部替换失败（描述专属菜单未展开），走到这里 return skipped，
        # 把描述编辑器【留在页面上】。它是全屏 modal，于是 ⑭ save 点下去后：
        #   - 页面弹的是描述编辑器自己的「Temu产品描述批量操作/保存/关闭」弹窗，
        #     按钮文案不是 save 预期的「继续编辑」，关不掉、遮罩一直挡着；
        #   - 那条「错误：产品信息中有错误，请检查」toast 也是编辑器弹的，
        #     而 save 只看 .ant-form-item-explain-error，抓不到它；
        # 最后 save 报出「无校验错误但草稿更新时间未变」这种查不下去的结论，整单未落库。
        # 下方正常路径的注释早就写明「关编辑器必须在所有 return 之前」，只有这一条
        # 早退漏了——一张都没换成时恰恰最需要关（编辑器一定是开着的）。
        closed = await ensure_desc_closed(session)
        if closed.get("status") != "ok":
            await emit({"type": "manual_check", "stage": "desc",
                        "message": f"描述编辑器未能关闭（会挡住 ⑭ 保存）："
                                   f"{closed.get('reason')}"})
        return {"status": "skipped", "note": f"{len(mods)} 张全部保留"}
    s = await desc_save(session)
    # 【关编辑器必须在所有 return 之前】它是全屏 modal，开着会盖住整个编辑页，后续
    # ⑦⑧⑩⑪⑭ 全部点不中，且报的是「瞄点未命中」——看着像时序问题，实际是被遮住
    # （2026-08-24 实测：阶段⑦ 连续两次 open-space 失败，诊断才发现瞄点落在描述弹窗的
    # .page-content 上）。下面尺寸破线那条会让本阶段 fail，而 fail 后同一浏览器会话
    # 仍可能被续跑复用，故不能把关闭动作留在 return 之后。
    closed = await ensure_desc_closed(session)
    if closed.get("status") != "ok":
        await emit({"type": "manual_check", "stage": "desc",
                    "message": f"描述编辑器未能关闭（会挡住后续阶段的点击）："
                               f"{closed.get('reason')}"})
    if s.get("status") == "validation-error":
        # 外链未转存与尺寸不达标是两回事，分别报出来——只说「外链」会让人以为
        # 尺寸没问题（本商品实际是尺寸那条，见 desc_save 的回读注释）
        parts = []
        if s.get("foreignHosts"):
            parts.append(f"仍有外链图未转存：{s['foreignHosts']}")
        if s.get("tooSmall"):
            parts.append(f"仍有图不符合描述图要求（比例 {images.DESC_RATIO_MIN}~"
                         f"{images.DESC_RATIO_MAX}、两边 >= {images.DESC_MIN_W}）："
                         f"{s['tooSmall']}")
        # 【把成因一起报出来】这两条校验几乎从不是独立故障：本阶段每有一张图没换成，
        # 它的 1688 原始外链就还挂在页面上，于是「未转存」与「尺寸破线」必然同时出现
        # （2026-08-26 那批：3 张失败 → 恰好 3 张破线 + cbu01.alicdn.com 外链）。
        # 只报校验结果会让人去查转存链路，而真正要看的是上面那几条替换失败的原因。
        if kept_original := len(plan["replace"]) - replaced:
            parts.append(f"根因很可能是本阶段有 {kept_original} 张图未替换成功"
                         f"（它们的 1688 原始外链仍在页面上），请看上面各张的失败原因")
        await emit({"type": "manual_check", "stage": "desc",
                    "message": "描述保存后 " + ("；".join(parts) or str(s)[:120])})
        # 【尺寸不合规必须让本阶段 fail，不能只报一条提示就放行】2026-08-28 实测
        # （pdd-994437651298）：6 张图不合规时阶段⑬ 照样返回 ok，一路走到「立即发布」
        # 并按列表取证判定发布成功——等于带着不合规的图发了出去，而这条闸门是保存时
        # 才静默校验的，发出去迟早被弹回。
        # 【判据是描述图自己那套，不是服装 1340x1785】2026-08-28 用户截图取证后改。
        # 原先套服装红线，1000x1000（合格）被判 fail，整单卡死且每跑白烧一轮生图。
        # 外链未转存【不】单独当硬错误：desc_save 的注释已说明，本商品可能本来就没
        # 替换过描述图，外链是采集时的原始状态，那种情况不该拖垮整个商品。
        if s.get("tooSmall"):
            return {"status": "fail",
                    "note": f"{len(s['tooSmall'])} 张描述图不符合要求"
                            f"（宽高比 {images.DESC_RATIO_MIN}~{images.DESC_RATIO_MAX}、"
                            f"两边 >= {images.DESC_MIN_W}）："
                            f"{str(s['tooSmall'])[:120]}"}
    elif s.get("status") != "ok":
        return {"status": "fail", "note": f"desc_save 失败：{str(s)[:150]}"}
    return {"status": "ok", "note": _desc_note(deleted, replaced, text_note,
                                               upscaled, reused)}


def _desc_note(deleted: int, replaced: int, text_note: str,
               upscaled: int, reused: int) -> str:
    """拼阶段⑬ 的结论文案（抽出来只为让 _st_desc 的收尾路径不重复这段拼接）。"""
    note = f"删 {deleted} 张 / 替换 {replaced} 张"
    if text_note:
        note += f" | {text_note}"
    extra = []
    if upscaled:
        extra.append(f"{upscaled} 张仅放大未动画面")
    if reused:
        extra.append(f"{reused} 张复用已有产物，省了生图")
    if extra:
        note += "（" + "；".join(extra) + "）"
    return note


async def _st_video(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑬b 产品视频：把平台连带搬来的 1688 视频裁成 Temu 允许的比例后填回表单。

    【这一步为什么存在】视频不是我们传的，是阶段② 认领时平台从 1688 连带搬来的。
    1688 商品视频绝大多数是 9:16 竖屏，而 Temu 只收 1:1 / 3:4 / 16:9，于是发布时
    被打回「Video ratio should be 1:1 or 3:4 or 16:9」——而这个报错出现在阶段⑮
    发布【之后】：前 14 个阶段全绿、save 也落库了，最后一步才被弹回，且回执不说是
    哪个视频。故必须在发布前把关（2026-08-26 那批失败品实测：720×1280，比例 0.5625）。

    【纯增益路径，从不 fail】与 ⑤b 图片清理同一取向：
      - 没有视频 → skipped（大多数 1688 商品其实没视频）；
      - 视频已合规 → skipped，绝不重编码（重编码必然掉画质，对本来就合规的是倒扣分）；
      - 下载/转码/上传任一步失败 → 报 manual_check 但仍返回 ok，让流程继续走到 save。
    最后一条是刻意的：视频只是加分项，为它失败而让整个商品 fail 得不偿失——而原来
    那批品的实际后果只是「带着不合规视频去发布、被平台打回」，人工删掉视频即可发布。
    故这里失败时把话说清楚（哪个环节、什么原因），由人决定是删视频还是重试。

    【为什么读接口而不读页面】视频区 DOM 里没有真实地址（封面是内嵌 base64 占位图，
    Vue 3 的 setupState 也扒不到），只有 edit.json 的响应里有，见 read_video_url。
    """
    rowid = ctx.get("rowid") or ""
    if not rowid:
        return {"status": "skipped", "note": "没有 rowid，读不到视频字段"}

    cur = await read_video_url(session, rowid)
    src_url = (cur.get("videoUrl") or "").strip()
    if not src_url:
        return {"status": "skipped", "note": "该商品没有视频"}

    workdir = os.path.join(ctx["workdir"], "video")
    os.makedirs(workdir, exist_ok=True)
    raw = os.path.join(workdir, "source.mp4")

    # 1) 下载。视频在淘宝 CDN 上，要跟 302 且带浏览器 UA（见 video.download_video）
    dl = await asyncio.to_thread(videolib.download_video, src_url, raw)
    if dl.get("status") != "ok":
        await emit({"type": "manual_check", "stage": "video",
                    "message": f"视频下载失败，未处理（发布时可能被平台按比例打回）："
                               f"{dl.get('err')}"})
        return {"status": "ok", "note": f"下载失败，视频原样保留：{str(dl.get('err'))[:120]}"}

    # 2) 合规化。已合规会 action=skip 原样返回，不重编码
    norm = await asyncio.to_thread(videolib.normalize_video, raw)
    if norm.get("status") != "ok":
        await emit({"type": "manual_check", "stage": "video",
                    "message": f"视频转码失败，未处理（发布时可能被平台按比例打回）："
                               f"{norm.get('err')}"})
        return {"status": "ok", "note": f"转码失败，视频原样保留：{str(norm.get('err'))[:120]}"}

    meta = norm.get("meta") or {}
    if norm.get("action") == "skip":
        # 源视频本来就合规：什么都不用改，连上传都省掉
        return {"status": "skipped",
                "note": f"视频已合规（{meta.get('w')}×{meta.get('h')} "
                        f"{norm.get('ratioName')}），未改动"}

    # 3) 直传 + 用「网络上传」把地址填回表单
    r = await set_video(session, norm["output"])
    if r.get("status") != "ok":
        await emit({"type": "manual_check", "stage": "video",
                    "message": f"视频已裁好但没能填回表单[{r.get('stage')}]，"
                               f"页面上仍是原视频（发布可能被打回）：{str(r)[:150]}"})
        return {"status": "ok",
                "note": f"裁切成功但回填失败[{r.get('stage')}]，视频原样保留"}

    out = norm.get("outMeta") or {}
    dur = f"，截断到 {out.get('duration')}s" if norm.get("trimmed") else ""
    return {"status": "ok",
            "note": (f"{meta.get('w')}×{meta.get('h')}（{meta.get('ratio')}）→ "
                     f"{out.get('w')}×{out.get('h')}（{norm.get('ratioName')}）"
                     f"{dur}，{out.get('sizeMB')}MB")[:200]}


async def _st_save(ctx: dict, session: BrowserSession, emit) -> dict:
    r = await save(session, ctx["rowid"])
    if r.get("status") != "ok":
        red = "、".join(s["name"] for s in (r.get("redSections") or []))
        await emit({"type": "manual_check", "stage": "save",
                    "message": f"保存校验未过：{red or r.get('reason') or r.get('errors')}"
                               f"——草稿未落库，处理后可续跑"})
        return {"status": "fail", "note": f"校验未过：{red or r.get('reason')}"[:200]}
    ut = r.get("updateTime") or {}
    return {"status": "ok",
            "note": f"已保存（未发布）更新时间 {ut.get('before')} → {ut.get('after')}"}


async def _st_publish(ctx: dict, session: BrowserSession, emit) -> dict:
    """阶段⑮：点「发布」→「立即发布」真正上架。

    【默认跳过】do_publish 没显式开就 skipped——发布不可逆，闸门必须在人手上
    （见模块头「发布闸门」）。前置是 ⑭ save 成功：草稿没落库点发布只会重复撞同一批
    前端校验，故这里再核一次状态，不满足就 skipped 而非 fail（不是本阶段的错）。
    """
    if not ctx.get("do_publish"):
        # 文案要指得出开关在哪：2026-08-25 用户走 Web 跑完问「为什么没自动点发布」，
        # 当时页面上确实没有开关、文案却写「UI 勾选后才执行」，等于让人去找一个不存在
        # 的复选框。现在两个入口都有开关，故两个都点明。
        return {"status": "skipped",
                "note": "未开启发布（Web 页「自动发布」开关 / CLI --publish）"}

    saved = ((ctx.get("state") or {}).get("stages", {}).get("save") or {}).get("status")
    if saved not in _DONE:
        return {"status": "skipped",
                "note": f"⑭ 保存未成功（{saved or '未跑'}），不发布"}

    r = await publish_now(session, ctx["rowid"], confirm=True)
    st = r.get("status")
    if st == "ok":
        return {"status": "ok",
                "note": f"已发布：{'；'.join(r.get('messages') or []) or '已离开编辑页'}"[:200]}

    if st == "validation-error":
        red = "、".join(s["name"] for s in (r.get("redSections") or []))
        await emit({"type": "manual_check", "stage": "publish",
                    "message": f"发布校验未过：{red or r.get('errors')}"
                               f"——草稿已落库未上架，处理后可续跑"})
        return {"status": "fail", "note": f"发布校验未过：{red or r.get('errors')}"[:200]}

    if st == "unknown":
        # 点下去了但没抓到成功提示：不敢判成功（可能真上架了），交人工看一眼
        await emit({"type": "manual_check", "stage": "publish",
                    "message": f"发布结果判据不足（已点「立即发布」但未捕获提示）："
                               f"{r.get('messages')}——请到列表确认是否已上架"})
        return {"status": "fail", "note": "发布结果判据不足，需人工确认"}

    await emit({"type": "manual_check", "stage": "publish",
                "message": f"发布失败：{str(r)[:200]}"})
    return {"status": "fail", "note": f"发布失败：{str(r)[:200]}"}


_STAGE_FUNCS = {
    "extract": _st_extract,
    "claim": _st_claim,
    "auto_cat": _st_auto_cat,
    "attrs": _st_attrs,
    "titles": _st_titles,
    "clean_images": _st_clean_images,
    "material": _st_material,
    "skc": _st_skc,
    "fix_sizes": _st_fix_sizes,
    "sizechart": _st_sizechart,
    "sku_code": _st_sku_code,
    "variant": _st_variant,
    "stock": _st_stock,
    "shipping": _st_shipping,
    "desc": _st_desc,
    "video": _st_video,
    "save": _st_save,
    "publish": _st_publish,
}


# ---- 单商品与批量 ------------------------------------------------------------

async def publish_one(
    session: BrowserSession,
    task: dict,
    store: str,
    site: str = "",
    on_progress: ProgressCB = None,
    index: int = 0,
    total: int = 1,
    from_stage: str = "",
    use_cache: bool = True,
    do_publish: bool = False,
    price: str = "",
) -> dict:
    """按 STAGES 顺序跑一个商品，返回 {"status", "rowid", "failed_stage", "note", "elapsed_s"}。

    只发 stage_* / manual_check / log 事件；product_start / product_done 由 run_batch 发
    （与 collect 侧「service 主循环统一收发商品级事件」的分工一致）。
    """
    key = _task_key(task)
    state = load_state(key)
    ctx = {
        "url": task.get("url"),
        "title": task.get("title") or state.get("title"),
        "rowid": task.get("rowid") or state.get("rowid"),
        "info_path": task.get("info_path") or state.get("info_path"),
        "workdir": state.get("workdir"),
        "store": store,
        "site": site,
        # 类目路径：阶段③写入，阶段④拿它当属性缓存的键；续跑时从状态文件回填
        "cat_path": state.get("cat_path"),
        # {颜色: [fileId…]}：阶段⑦换图成功时写入，续跑时判某行是否已是这一批图。
        # 【为什么必须持久化】表单阶段的成果一重载就丢，页面上的图却还在——没有这份
        # 清单就无从确认页面上那几张是不是本轮的，只能无条件重换（6 张约一分钟白工）。
        "skc_done": state.get("skc_done") or {},
        # 缓存开关是批次级不变量，与 store/site 一样塞 ctx（不进状态文件：
        # 回写元组里没有它，见下方 for k in (...)）
        "use_cache": use_cache,
        # ⑮ 发布闸门，同样是批次级不变量、不进状态文件（发布意愿属于本次运行，
        # 不该被续跑继承——否则重跑一次就静默又发一遍）
        "do_publish": do_publish,
        # ⑩ 申报价：批次级参数（UI 输入框/CLI --price），空串＝用管线默认 188.88。
        # 与 use_cache 一样不进状态文件——定价属于本次运行的决定，续跑不该继承旧价。
        "price": price,
        # ⑮ 要核 ⑭ save 的终态做前置判断，故把状态字典本身透给阶段函数
        # （同一个对象，主循环写完 stages[sid] 后 ⑮ 读到的就是最新值）
        "state": state,
        # 本次运行的显式起点：① extract 要靠它区分「续跑沿用旧产物」与
        # 「人工点名重抓源数据」（见 _st_extract 开头的说明）。批次级、不进状态文件。
        "from_stage": from_stage,
    }
    from_idx = _STAGE_IDS.index(from_stage) if from_stage else None
    t0 = time.monotonic()

    async def emit(ev: dict) -> None:
        await _emit(on_progress, {"index": index, "offer": key, **ev})

    # 表单阶段的实况判定结果（open_edit 之后才填得上，见下方补开编辑页那段）
    stale_form: set = set()

    def _should_run(i: int, sid: str) -> bool:
        prev = state["stages"].get(sid) or {}
        if from_idx is not None:
            return i >= from_idx
        if prev.get("status") in _DONE:
            # 状态文件说完成了，但表单阶段的成果只要 save 没成功就可能已经丢了，
            # 此时以页面实况为准（stale_form 由 _stale_form_stages 算出）
            return sid in stale_form
        return True

    # 续跑补开编辑页：claim / auto_cat 都会自己 open_edit，但它们被跳过时
    # （续跑或 from_stage 从 attrs 起）没人把页签导航到编辑页——重启浏览器后
    # 页签停在店小秘首页，attrs 起的所有阶段都假定当前页就是编辑页，
    # 直接跑会报「productBasicInfo 未找到」（2026-08-21 实测）。
    # 类目选择持久化在店小秘服务端（同日实测：重开编辑页，类目与 42 条属性行都在），
    # 故补开不丢 auto_cat 的成果；会丢的只是未保存的表单修改，
    # 而那随上一个浏览器进程早就没了。
    run_ids = [sid for i, (sid, _) in enumerate(STAGES) if _should_run(i, sid)]
    opened_edit = False
    if (ctx.get("rowid")
            and any(s in _EDIT_PAGE_STAGES for s in run_ids)
            and not {"claim", "auto_cat"} & set(run_ids)):
        await emit({"type": "log", "level": "info",
                    "message": f"续跑：先打开编辑页 rowid={ctx['rowid']}"})
        try:
            await open_edit(session, ctx["rowid"])
            opened_edit = True
        except Exception as e:
            return {"status": "fail", "rowid": ctx.get("rowid"),
                    "failed_stage": run_ids[0] if run_ids else "",
                    "note": f"打开编辑页失败：{e}"[:200],
                    "elapsed_s": round(time.monotonic() - t0, 1)}

    # 【续跑的关键一步：状态文件说「跑过」不等于「存住了」】save 从未成功过时，
    # ⑤~⑬ 的成果全在那张未保存的表单里，页面一重载就没了。只按状态文件跳过会
    # 直奔 save 提交空表单，永远卡在「产品信息、变种信息」校验未过（2026-08-23
    # 实测 947662049255，人工反复点也出不来）。故这里读一次页面实况来定夺。
    #
    # 只在 from_stage 未指定时做：显式指定起点是人工判断，不该被实况覆盖。
    if opened_edit and from_idx is None:
        saved_ok = (state["stages"].get("save") or {}).get("status") in _DONE
        if not saved_ok:
            try:
                live = await live_state(session)
                stale_form = set(_stale_form_stages(live))
            except Exception as e:
                # best-effort：读不到实况就保守重跑全部表单阶段，宁可多花时间
                logger.warning(f"编辑页实况读取失败，按全部表单阶段重跑：{e}")
                stale_form = set(_FORM_ONLY_STAGES)
            redo = [n for sid, n in STAGES
                    if sid in stale_form and (state["stages"].get(sid) or {}).get("status") in _DONE]
            if redo:
                await emit({"type": "manual_check", "stage": "resume",
                            "message": f"上次未落库，编辑页数据已丢失，重跑：{'、'.join(redo)}"})
                logger.warning(f"[{key}] 上次 save 未成功，按页面实况重跑 {len(redo)} 个阶段："
                               + "、".join(redo))
            run_ids = [sid for i, (sid, _) in enumerate(STAGES) if _should_run(i, sid)]

    # 【预热必须在 for 之外启动】它要与 ②③ 并行，而 ① extract 是循环里的第一个阶段：
    # 在阶段函数里 create_task 也行，但那样 ① 被跳过（任务自带 product-info.json）时就
    # 不会启动了——而那种情况下产物早已在磁盘上，恰恰是最该预热的。故改成循环开始前
    # 判一次：有 info_path 直接起，没有就等 ① 跑完再起（下面 sid == "extract" 那处）。
    try:
        if ctx.get("info_path"):
            _start_prewarm(ctx, emit)

        for i, (sid, name) in enumerate(STAGES):
            prev = state["stages"].get(sid) or {}
            should_run = _should_run(i, sid)
            if not should_run:
                await emit({"type": "stage_done", "stage": sid, "name": name,
                            "status": "skipped", "elapsed_s": prev.get("elapsed_s", 0.0),
                            "note": "此前已完成，续跑跳过"})
                continue

            await emit({"type": "stage_start", "stage": sid, "name": name})
            st0 = time.monotonic()
            try:
                r = await _STAGE_FUNCS[sid](ctx, session, emit)
                status = r.get("status") if r.get("status") in _DONE else "fail"
                note = r.get("note") or ""
            except Exception as e:
                status, note = "fail", f"异常：{e}"
                logger.exception(f"[{key}] 阶段 {sid} 异常")
            elapsed = round(time.monotonic() - st0, 1)
            state["stages"][sid] = {"status": status, "elapsed_s": elapsed,
                                    "note": note[:200]}
            # ctx 里后续的产出（rowid/info_path/workdir/title/cat_path）回写状态，续跑全靠它们。
            # cat_path 是阶段③走通的类目路径，属性缓存要用它当键——续跑 from attrs 时
            # 阶段③被跳过，不持久化就取不到（旧状态文件没这个键 → None → 全量读，不回归）。
            for k in ("rowid", "info_path", "workdir", "title", "cat_path", "skc_done"):
                if ctx.get(k):
                    state[k] = ctx[k]
            state["status"] = "running"
            save_state(state)
            await emit({"type": "stage_done", "stage": sid, "name": name,
                        "status": status, "elapsed_s": elapsed, "note": note[:200]})

            if status == "fail":
                state["status"] = "fail"
                state["failed_stage"] = sid
                save_state(state)
                return {"status": "fail", "rowid": ctx.get("rowid"), "failed_stage": sid,
                        "note": note[:200],
                        "elapsed_s": round(time.monotonic() - t0, 1)}

            # ① 刚跑完就起预热，让它与 ② 认领、③ 类目并行（那两步实测 50~350s，
            # 期间浏览器在忙、LLM 完全空闲）。放在 stage_done 之后：预热失败不该影响
            # ① 的阶段结论，而它需要 ① 落下的 info_path/workdir。
            if sid == "extract" and status in _DONE:
                _start_prewarm(ctx, emit)

        state["status"] = "ok"
        save_state(state)
        pub = (state["stages"].get("publish") or {}).get("status")
        note = "已保存落库并发布" if pub == "ok" else "已保存落库（未发布）"
        return {"status": "ok", "rowid": ctx.get("rowid"), "failed_stage": "",
                "note": note, "elapsed_s": round(time.monotonic() - t0, 1)}
    finally:
        # 【必须回收】商品中途失败返回时，预热可能还在跑（比如 ③ 类目就挂了）。
        # 留着它会让下一个商品的批次里多一个跑着生图的悬挂任务，既烧钱又抢并发额度。
        # 已完成的不动（结果没人取而已）；没完成的取消，取消异常照 best-effort 吞掉。
        task = ctx.get("prewarm_task")
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                # CancelledError 在 3.8+ 继承 BaseException，故不能只 catch Exception；
                # 这里的等待纯为让取消真正生效，任何结果都不该影响商品收尾
                pass
            logger.info("商品收尾：未完成的预热任务已取消")


async def run_batch(
    tasks: list,
    store: str = "",
    site: str = "",
    on_progress: ProgressCB = None,
    from_stage: str = "",
    use_cache: bool = True,
    do_publish: bool = False,
    price: str = "",
) -> dict:
    """批量发布编排入口，返回 {"ok", "fail", "batch"}。

    tasks 元素三种形态（① 跑不跑看 info_path，② 跑不跑看 rowid，两个判据独立）：
      {"url": "<商品链接>", "title": "<可选，提取后自动回填>"}   —— 全流程 ①→⑭
      {"url": "<商品链接>", "rowid": "..."}     —— 跑①提炼、跳②认领（草稿已存在）
      {"rowid": "...", "info_path": "<product-info.json>"}      —— 跳过①②
    url 支持 1688 / 拼多多 / Temu / 亚马逊（按域名分派适配器，见 app/publish/sources/）。

    【第二种形态是采集箱清单专用】那些行在店小秘已认领好，既需要 ① 产出
    product-info.json（否则 ④ 属性阶段缺它 fail），又不能再认领一次（会凭空多一条
    草稿且不可逆）。前端「填入任务框」填的就是这个形态（见 templates/publish.html
    的 parseTasks）。

    store/site：显式参数 > prefs 回填；成功启动后 save_prefs 记住本次选择。

    【site 没有默认值，两者都必填】原先默认「全球」，而店小秘认领弹窗里根本没有
    「全球」这一项（那是 Temu 后台的域名级区域，不是站点，见 app/publish/shops.py），
    默认值一路走到 _select_store_and_site 必然抛「未找到站点」。宁可这里就拦下来
    报清楚，也不要跑到阶段②才炸。
    """
    prefs = load_prefs()
    store = store or prefs.get("store") or ""
    site = site or prefs.get("site") or ""
    batch = int(time.time())

    if from_stage and from_stage not in _STAGE_IDS:
        await _emit(on_progress, {"type": "aborted",
                                  "reason": f"未知阶段 {from_stage!r}，可选：{_STAGE_IDS}"})
        return {"ok": 0, "fail": 0, "batch": batch}
    if not tasks:
        await _emit(on_progress, {"type": "aborted", "reason": "任务清单为空"})
        return {"ok": 0, "fail": 0, "batch": batch}
    if not store:
        await _emit(on_progress, {"type": "aborted",
                                  "reason": "未指定店铺（--store 或先跑过一次让 prefs 记住）"})
        return {"ok": 0, "fail": 0, "batch": batch}
    if not site:
        await _emit(on_progress, {"type": "aborted",
                                  "reason": "未指定站点（--site；店小秘没有「全球」站点，"
                                            "必须选具体国家站点，如 美国）"})
        return {"ok": 0, "fail": 0, "batch": batch}
    if not await ensure_cdp_alive():
        await _emit(on_progress, {"type": "aborted", "reason": "CDP 不可用（调试 Chrome 未启动）"})
        return {"ok": 0, "fail": 0, "batch": batch}
    save_prefs({"store": store, "site": site})

    total = len(tasks)
    await _emit(on_progress, {"type": "batch_start", "total": total,
                              "store": store, "site": site, "batch": batch})
    await _emit(on_progress, {"type": "log", "level": "info",
                              "message": f"本次使用模型：{active_llm_label()}"})
    # 报一下缓存现状：类目/属性缓存命中与否直接决定阶段③④的耗时，跑之前就让人看见
    if use_cache:
        st = cache.cache_stats()
        await _emit(on_progress, {
            "type": "log", "level": "info",
            "message": f"缓存：已知类目路径 {st['paths']} 条，"
                       f"已缓存属性类目 {st['attrCategories']} 个（{st['attrRows']} 行）"})
    else:
        await _emit(on_progress, {"type": "log", "level": "info",
                                  "message": "已禁用缓存，类目与属性走全量读取"})
    ok = fail = 0
    t0 = time.monotonic()
    sink_id = _install_log_bridge(on_progress)
    session = BrowserSession()
    try:
        await session.open()
        for i, task in enumerate(tasks, 1):
            if not await ensure_cdp_alive(retries=CDP_PING_RETRIES, wait=5.0):
                await _emit(on_progress, {"type": "aborted",
                                          "reason": f"CDP 连续 {CDP_PING_RETRIES} 次连不上，批次中止"})
                break
            # 上一商品中途页签被关/崩溃会让会话失效（TargetClosedError）；
            # ensure_cdp_alive 只证明浏览器还在，不代表会话的页签还在——
            # 不重连的话后续每个商品都会在第一次 evaluate 时报同一个错。
            if not session.is_alive():
                logger.warning("会话页签被关闭或连接已断开，重连 CDP 后继续后续商品")
                await session.close()
                try:
                    await session.open()
                except Exception as e:
                    await _emit(on_progress, {"type": "aborted",
                                              "reason": f"会话重连失败，批次中止：{e}"})
                    break
            reset_token_counters()  # LLM 单例按累计 token 判限，每商品清零
            try:
                key = _task_key(task)
            except ValueError as e:
                key = f"task-{i}"
                await _emit(on_progress, {"type": "product_start", "index": i,
                                          "total": total, "offer": key, "title": ""})
                await _emit(on_progress, {"type": "product_done", "index": i, "total": total,
                                          "offer": key, "rowid": None, "status": "fail",
                                          "elapsed_s": 0.0, "note": str(e), "failed_stage": ""})
                fail += 1
                continue
            await _emit(on_progress, {"type": "product_start", "index": i, "total": total,
                                      "offer": key, "title": task.get("title") or ""})
            try:
                r = await asyncio.wait_for(
                    publish_one(session, task, store, site, on_progress,
                                index=i, total=total, from_stage=from_stage,
                                use_cache=use_cache, do_publish=do_publish,
                                price=price),
                    timeout=PRODUCT_TIMEOUT)
            except asyncio.TimeoutError:
                r = {"status": "fail", "rowid": task.get("rowid"), "failed_stage": "",
                     "note": f"超过 {PRODUCT_TIMEOUT}s 单商品超时"}
            except Exception as e:
                logger.exception(f"[{key}] 商品级异常")
                r = {"status": "fail", "rowid": task.get("rowid"), "failed_stage": "",
                     "note": f"异常：{e}"}
            if r["status"] == "ok":
                ok += 1
            else:
                fail += 1
            await _emit(on_progress, {"type": "product_done", "index": i, "total": total,
                                      "offer": key, "rowid": r.get("rowid"),
                                      "status": r["status"],
                                      "elapsed_s": r.get("elapsed_s", 0.0),
                                      "note": r.get("note") or "",
                                      "failed_stage": r.get("failed_stage") or ""})
    finally:
        logger.remove(sink_id)
        await session.close()
    await _emit(on_progress, {"type": "batch_done", "ok": ok, "fail": fail,
                              "elapsed_s": round(time.monotonic() - t0, 1)})
    return {"ok": ok, "fail": fail, "batch": batch}
