"""店小秘发布共用能力：service。各来源流程由 workflows/ 独立定义。"""

import asyncio
import inspect
import json
import os
import re
import shutil
import time
from app.error_report import report
from app.logger import logger
from app.publish import (
    alert,
    cache,
    extract,
    images,
    preferences,
    state as publish_state,
    video as videolib,
    vision,
    workflows,
)
from app.publish.accessories import judge_sku_category
from app.publish.attributes.workflow import check_attrs
from app.publish.browser import BrowserSession, ensure_cdp_alive
from app.publish.category import auto_cat
from app.publish.claim import collect_and_claim
from app.publish.llm import active_llm_label, reset_token_counters
from app.publish.media.description import (
    desc_delete,
    desc_map,
    desc_save,
    desc_text_apply,
    desc_text_delete_all,
    desc_text_map,
    ensure_desc_closed,
)
from app.publish.media.description_replace import desc_replace
from app.publish.media.materials import set_material
from app.publish.media.preview import PREVIEW_MIN_SIDE, sku_preview_replace_row, sku_preview_state
from app.publish.media.skc import (
    SKC_ROW_MIN_IMAGES,
    _skc_row_matches,
    _skc_row_state,
    skc_image_support,
    skc_replace_row,
)
from app.publish.media.video import delete_video, read_video_url, set_video
from app.publish.navigation import live_state, open_edit
from app.publish.packaging import estimate_pack
from app.publish.persistence import publish_now
from app.publish.preferences import (
    IMAGE_CONCURRENCY_DEFAULT as IMAGE_CONCURRENCY_DEFAULT,
    IMAGE_CONCURRENCY_MAX as IMAGE_CONCURRENCY_MAX,
    PREFS_PATH as PREFS_PATH,
    get_image_concurrency as get_image_concurrency,
    load_prefs as load_prefs,
    save_prefs as save_prefs,
    set_image_concurrency as set_image_concurrency,
)
from app.publish.saving import save
from app.publish.shipping import set_shipping
from app.publish.sizechart.editor import add_sizechart
from app.publish.sizechart.parts import _size_category_for
from app.publish.sizes import fix_sizes
from app.publish.sku_codes import fix_sku_codes
from app.publish.sources import base as sources_base
from app.publish.stages import (
    cleaning as stages_cleaning,
    description as stages_description,
    extracting as stages_extracting,
    form as stages_form,
    material as stages_material,
    preview as stages_preview,
    prewarm as stages_prewarm,
    results as stages_results,
    resume as stages_resume,
    saving as stages_saving,
    shipping as stages_shipping,
    skc as stages_skc,
    variants as stages_variants,
    video as stages_video,
)
from app.publish.stages.cleaning import (
    CLEAN_TIMEOUT as CLEAN_TIMEOUT,
    _clean_main_images as _clean_main_images,
    _save_info as _save_info,
    _st_clean_images as _st_clean_images,
)
from app.publish.stages.cleaning_rules import _retry_hint as _retry_hint
from app.publish.stages.description import _desc_note as _desc_note, _st_desc as _st_desc
from app.publish.stages.description_images import (
    DESC_QC_TRIES as DESC_QC_TRIES,
    DESC_QC_TRIES_TEXT as DESC_QC_TRIES_TEXT,
    _desc_cache_paths as _desc_cache_paths,
    _prepare_desc_image as _prepare_desc_image,
    _prewarm_desc_images as _prewarm_desc_images,
    _rehost_desc_keeps as _rehost_desc_keeps,
    _resolve_desc_pos as _resolve_desc_pos,
)
from app.publish.stages.description_text import (
    _keep_size_text as _keep_size_text,
    _size_evidence as _size_evidence,
)
from app.publish.stages.extracting import _st_extract as _st_extract
from app.publish.stages.form import (
    _st_attrs as _st_attrs,
    _st_auto_cat as _st_auto_cat,
    _st_claim as _st_claim,
    _st_titles as _st_titles,
)
from app.publish.stages.material import _st_material as _st_material
from app.publish.stages.preview import _st_sku_preview as _st_sku_preview
from app.publish.stages.prewarm import (
    _PREWARM_KEYS as _PREWARM_KEYS,
    _run_prewarm as _run_prewarm,
    _start_prewarm as _start_prewarm,
)
from app.publish.stages.prewarm_access import (
    _await_prewarm as _await_prewarm,
    _take_prewarm as _take_prewarm,
)
from app.publish.stages.prewarm_plans import (
    _desc_modules_from_raw as _desc_modules_from_raw,
    _prewarm_desc as _prewarm_desc,
    _replan_desc_by_url as _replan_desc_by_url,
)
from app.publish.stages.results import _DONE as _DONE
from app.publish.stages.resume import (
    _CAT_STAGES as _CAT_STAGES,
    _FORM_ONLY_STAGES as _FORM_ONLY_STAGES,
    _FORM_STAGES_AFTER_CAT as _FORM_STAGES_AFTER_CAT,
    _stale_form_stages as _stale_form_stages,
)
from app.publish.stages.saving import _st_publish as _st_publish, _st_save as _st_save
from app.publish.stages.shipping import _st_shipping as _st_shipping
from app.publish.stages.skc import (
    _pad_row_images as _pad_row_images,
    _skc_size_fallback as _skc_size_fallback,
    _st_drop_acc as _st_drop_acc,
    _st_skc as _st_skc,
)
from app.publish.stages.variants import (
    _st_fix_sizes as _st_fix_sizes,
    _st_sizechart as _st_sizechart,
    _st_sku_code as _st_sku_code,
    _st_stock as _st_stock,
    _st_variant as _st_variant,
)
from app.publish.stages.video import _st_video as _st_video
from app.publish.state import (
    STATE_DIR as STATE_DIR,
    _load_info as _load_info,
    _state_path as _state_path,
    _task_key as _task_key,
    load_state as load_state,
    save_state as save_state,
)
from app.publish.stock import set_stock
from app.publish.titles import generate_titles, set_titles
from app.publish.variant_colors import accessory_colors_from_rows, drop_accessory_colors
from app.publish.variants import set_variant
from typing import Awaitable, Callable, Optional, Union


ProgressCB = Optional[Callable[[dict], Union[None, Awaitable[None]]]]


CDP_PING_RETRIES = 3     # 每商品前的 CDP 健康检查次数（对齐 collect 侧）


STAGES = [
    ("extract", "① 采集提炼"),
    ("claim", "② 采集认领"),
    ("auto_cat", "③ 产品类目"),
    ("attrs", "④ 属性审核"),
    ("titles", "⑤ 标题产地"),
    ("clean_images", "⑤b 图片清理"),
    ("material", "⑥ 素材图"),
    ("drop_acc", "⑦a 剔配件色"),
    ("skc", "⑦ SKC颜色图"),
    ("sku_preview", "⑦b SKU预览图"),
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


# 进入前必须已经停在店小秘编辑页的阶段（auto_cat 自己会 open_edit，故从 attrs 算起；
# claim 也会 open_edit，在 publish_one 里统一判断）
_EDIT_PAGE_STAGES = set(_STAGE_IDS[_STAGE_IDS.index("attrs"):])


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


def _alert_hook(on_progress: ProgressCB, store: str, site: str,
                total: int) -> ProgressCB:
    """在事件流上挂飞书告警，返回包装后的回调（原回调照常收到全部事件）。

    【为什么挂在事件流上而不是逐处插 send】「中断」在本文件里有十来个出口：
    aborted 有 6 处（未知阶段/清单空/缺店铺/缺站点/CDP 不通/连不上中止/重连失败），
    商品 fail 则散在 publish_one 的多个 return 里。逐处加 await alert.xxx 既容易漏、
    又把告警关注点摊到整个编排逻辑里。而这三类中断【都已经有对应事件】，
    在事件流上判一次 type 就全覆盖了，新增出口自动纳入。

    只认三类事件，与 alert 模块的取向一致（详见 app/publish/alert.py 开头）：
      aborted      → 批次中止，立刻报
      product_done + status=fail → 单商品失败，逐条报
      batch_done + fail>0        → 收尾汇总（全绿不发）
    stage_done 的 fail 不单独报：它紧接着就会让商品 fail 出 product_done，
    报两遍是噪音。manual_check 也不报（数量多、大多不阻断，会把群刷成日志流）。

    【告警是 await 在回调里同步发的，不 create_task】一发约 300ms，相对单商品
    数分钟的耗时可以忽略；而 fire-and-forget 的任务在批次收尾、事件循环即将关闭时
    会被取消，最该发出去的那条 batch_done 汇总恰好最容易丢。
    """
    stat = {"done": 0, "failures": []}

    async def cb(event: dict) -> None:
        await _emit(on_progress, event)
        t = event.get("type")
        try:
            if t == "aborted":
                await alert.alert_batch_aborted(
                    event.get("reason") or "", store=store, site=site,
                    done=stat["done"], total=total)
                await report("publish", stage="", item="",
                             message=str(event.get("reason") or ""))
            elif t == "product_done":
                stat["done"] += 1
                if event.get("status") == "fail":
                    stage_name = dict(STAGES).get(event.get("failed_stage") or "", "")
                    stat["failures"].append((event.get("offer") or "", stage_name))
                    await alert.alert_product_fail(
                        offer=event.get("offer") or "", title=event.get("title") or "",
                        stage=stage_name, note=event.get("note") or "",
                        store=store, site=site, rowid=event.get("rowid"),
                        index=event.get("index") or 0, total=event.get("total") or total,
                        elapsed_s=event.get("elapsed_s") or 0.0)
                    await report("publish", stage=stage_name,
                                 item=str(event.get("offer") or ""),
                                 message=str(event.get("note") or ""))
            elif t == "batch_done" and int(event.get("fail") or 0) > 0:
                await alert.alert_batch_done(
                    ok=int(event.get("ok") or 0), fail=int(event.get("fail") or 0),
                    elapsed_s=event.get("elapsed_s") or 0.0,
                    store=store, site=site, failures=stat["failures"])
        except Exception as e:
            # 告警属辅助路径：发不出去只告警，绝不能反过来打断发布批次
            logger.warning(f"飞书告警钩子异常（忽略）：{e}")

    return cb


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


_STAGE_FUNCS = {
    "extract": stages_extracting._st_extract,
    "claim": stages_form._st_claim,
    "auto_cat": stages_form._st_auto_cat,
    "attrs": stages_form._st_attrs,
    "titles": stages_form._st_titles,
    "clean_images": stages_cleaning._st_clean_images,
    "material": stages_material._st_material,
    "drop_acc": stages_skc._st_drop_acc,
    "skc": stages_skc._st_skc,
    "sku_preview": stages_preview._st_sku_preview,
    "fix_sizes": stages_variants._st_fix_sizes,
    "sizechart": stages_variants._st_sizechart,
    "sku_code": stages_variants._st_sku_code,
    "variant": stages_variants._st_variant,
    "stock": stages_variants._st_stock,
    "shipping": stages_shipping._st_shipping,
    "desc": stages_description._st_desc,
    "video": stages_video._st_video,
    "save": stages_saving._st_save,
    "publish": stages_saving._st_publish,
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
    keep_video: bool = True,
    warehouse: str = "",
) -> dict:
    """兼容入口：按来源选择独立发布管线。"""
    try:
        state = publish_state.load_state(publish_state._task_key(task))
        info_path = task.get("info_path") or state.get("info_path")
        info = publish_state._load_info(info_path) if info_path else {}
        workflow = workflows.resolve_workflow(task, state, info)
        if workflow is None:
            raise workflows.SourceMismatchError(
                "商品来源不明确，请提供来源 URL 或 source_platform")
        validated_task = workflow.validate_task(task, state)
    except (ValueError, OSError) as error:
        note = f"来源校验失败：{error}"
        await _emit(on_progress, {"type": "manual_check", "index": index,
                                 "stage": "extract", "message": note})
        return {"status": "fail", "rowid": task.get("rowid"),
                "failed_stage": "extract", "note": note, "elapsed_s": 0.0}
    return await workflow.rules.publish_one(
        session, validated_task, store,
        site=site, on_progress=on_progress, index=index, total=total,
        from_stage=from_stage, use_cache=use_cache, do_publish=do_publish,
        price=price, keep_video=keep_video, warehouse=warehouse)


async def _run_product(
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
    keep_video: bool = True,
    warehouse: str = "",
    *,
    workflow,
) -> dict:
    """按 STAGES 顺序跑一个商品，返回 {"status", "rowid", "failed_stage", "note", "elapsed_s"}。

    只发 stage_* / manual_check / log 事件；product_start / product_done 由 run_batch 发
    （与 collect 侧「service 主循环统一收发商品级事件」的分工一致）。
    """
    key = publish_state._task_key(task)
    state = publish_state.load_state(key)
    # 【workdir 必须能从 info_path 反推，不能只靠状态文件】它原先只在 ① extract 里
    # 赋值，而 --from-stage 会跳过 ①（_should_run 对 from_idx 之前一律 False）。
    # rowid 模式的状态键是 rowid-<rowid>（与 offer 键的那份是两个文件，见
    # publish-resume-state-key 的结论），那份状态里 workdir 为 null，于是
    # ctx["workdir"] 一路是 None，⑬ 描述图转存 _desc_cache_paths(None, url) 抛
    # 「expected str, bytes or os.PathLike object, not NoneType」，被 best-effort
    # 吞成 warning：5 张 keep 图全部没转存，商品带着 1688 外链就发出去了
    # （2026-09-01 实跑取证）。info_path 与 workdir 本是同一目录的两种说法，
    # 能反推就不该等 ① 来填。
    workdir = state.get("workdir")
    info_path = task.get("info_path") or state.get("info_path")
    try:
        info = publish_state._load_info(info_path) if info_path and os.path.isfile(info_path) else {}
        selected = workflows.resolve_workflow(task, state, info)
        if selected and selected.platform != workflow.platform:
            raise workflows.SourceMismatchError("续跑管线与当前管线不一致")
    except (ValueError, OSError) as error:
        note = f"来源校验失败：{error}"
        await _emit(on_progress, {"type": "manual_check", "index": index,
                                 "offer": key, "stage": "extract", "message": note})
        return {"status": "fail", "rowid": task.get("rowid"),
                "failed_stage": "extract", "note": note, "elapsed_s": 0.0}
    if not workdir and info_path:
        workdir = os.path.dirname(os.path.abspath(info_path))
    ctx = {
        "workflow": workflow,
        "source_platform": workflow.platform if workflow else "",
        "workflow_id": workflow.workflow_id if workflow else "",
        "url": task.get("url"),
        "title": task.get("title") or state.get("title"),
        "rowid": task.get("rowid") or state.get("rowid"),
        "info_path": info_path,
        "workdir": workdir,
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
        # ⑬b 视频去留：批次级开关。True 走原来的比例合规化回填，False 直接点「删除」。
        # 与 use_cache/price 一样不进状态文件——「这批要不要视频」属于本次运行的决定，
        # 续跑不该继承上次的取向（上次删掉了，这次开着开关重跑就该重新处理）。
        "keep_video": keep_video,
        # ⑪ 选择仓库：批次级参数（发布页仓库下拉的真实选项 / CLI --warehouse），
        # 空串＝set_stock 里退回 config 站点映射。与 price 同理不进状态文件——
        # 用哪个仓属于本次运行的决定，续跑不该继承。
        "warehouse": warehouse,
        # ⑮ 要核 ⑭ save 的终态做前置判断，故把状态字典本身透给阶段函数
        # （同一个对象，主循环写完 stages[sid] 后 ⑮ 读到的就是最新值）
        "state": state,
        # 本次运行的显式起点：① extract 要靠它区分「续跑沿用旧产物」与
        # 「人工点名重抓源数据」（见 _st_extract 开头的说明）。批次级、不进状态文件。
        "from_stage": from_stage,
    }
    plan = workflow.stages()
    stages = [(stage.key, stage.name) for stage in plan]
    handlers = {stage.key: stage.run for stage in plan}
    stage_ids = [stage.key for stage in plan]
    from_idx = stage_ids.index(from_stage) if from_stage else None
    t0 = time.monotonic()

    async def emit(ev: dict) -> None:
        await _emit(on_progress, {"index": index, "offer": key,
                                 "source_platform": ctx.get("source_platform", ""),
                                 "workflow_id": ctx.get("workflow_id", ""), **ev})

    if workflow:
        state["source_platform"] = workflow.platform
        state["workflow_id"] = workflow.workflow_id
        publish_state.save_state(state)
        await emit({"type": "log", "level": "info", "message": workflow.name})

    # 表单阶段的实况判定结果（open_edit 之后才填得上，见下方补开编辑页那段）
    stale_form: set = set()

    def _should_run(i: int, sid: str) -> bool:
        prev = state["stages"].get(sid) or {}
        if from_idx is not None:
            return i >= from_idx
        if prev.get("status") in stages_results._DONE:
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
    run_ids = [sid for i, (sid, _) in enumerate(stages) if _should_run(i, sid)]
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
        saved_ok = (state["stages"].get("save") or {}).get("status") in stages_results._DONE
        if not saved_ok:
            try:
                live = await live_state(session)
                stale_form = set(stages_resume._stale_form_stages(live))
            except Exception as e:
                # best-effort：读不到实况就保守重跑全部表单阶段，宁可多花时间
                logger.warning(f"编辑页实况读取失败，按全部表单阶段重跑：{e}")
                stale_form = set(stages_resume._FORM_ONLY_STAGES)
            redo = [n for sid, n in stages
                    if sid in stale_form and (state["stages"].get(sid) or {}).get("status") in stages_results._DONE]
            if redo:
                await emit({"type": "manual_check", "stage": "resume",
                            "message": f"上次未落库，编辑页数据已丢失，重跑：{'、'.join(redo)}"})
                logger.warning(f"[{key}] 上次 save 未成功，按页面实况重跑 {len(redo)} 个阶段："
                               + "、".join(redo))
            run_ids = [sid for i, (sid, _) in enumerate(stages) if _should_run(i, sid)]

    # 【预热必须在 for 之外启动】它要与 ②③ 并行，而 ① extract 是循环里的第一个阶段：
    # 在阶段函数里 create_task 也行，但那样 ① 被跳过（任务自带 product-info.json）时就
    # 不会启动了——而那种情况下产物早已在磁盘上，恰恰是最该预热的。故改成循环开始前
    # 判一次：有 info_path 直接起，没有就等 ① 跑完再起（下面 sid == "extract" 那处）。
    try:
        if ctx.get("info_path"):
            stages_prewarm._start_prewarm(ctx, emit)

        for i, (sid, name) in enumerate(stages):
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
                r = await handlers[sid](ctx, session, emit)
                status = r.get("status") if r.get("status") in stages_results._DONE else "fail"
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
            for k in ("rowid", "info_path", "workdir", "title", "cat_path", "skc_done",
                      "source_platform", "workflow_id"):
                if ctx.get(k):
                    state[k] = ctx[k]
            state["status"] = "running"
            publish_state.save_state(state)
            await emit({"type": "stage_done", "stage": sid, "name": name,
                        "status": status, "elapsed_s": elapsed, "note": note[:200]})

            if status == "fail":
                state["status"] = "fail"
                state["failed_stage"] = sid
                publish_state.save_state(state)
                return {"status": "fail", "rowid": ctx.get("rowid"), "failed_stage": sid,
                        "note": note[:200],
                        "elapsed_s": round(time.monotonic() - t0, 1)}

            # ① 刚跑完就起预热，让它与 ② 认领、③ 类目并行（那两步实测 50~350s，
            # 期间浏览器在忙、LLM 完全空闲）。放在 stage_done 之后：预热失败不该影响
            # ① 的阶段结论，而它需要 ① 落下的 info_path/workdir。
            if sid == "extract" and status in stages_results._DONE:
                stages_prewarm._start_prewarm(ctx, emit)

        state["status"] = "ok"
        publish_state.save_state(state)
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
    keep_video: bool = True,
    warehouse: str = "",
    pause_ctrl=None,
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

    warehouse：⑪「选择仓库」要勾的仓库名（发布页仓库下拉的真实选项，或 CLI
    --warehouse）。空串＝退回 pipeline.resolve_warehouse 的 config 站点映射。
    【不进 prefs】仓库是按店铺+站点的，记全局 prefs 换店/换站后会带出一个
    该站点不存在的名字，比不填更糟（不填走映射还有命中的机会）。

    【site 没有默认值，两者都必填】原先默认「全球」，而店小秘认领弹窗里根本没有
    「全球」这一项（那是 Temu 后台的域名级区域，不是站点，见 app/publish/shops.py），
    默认值一路走到 _select_store_and_site 必然抛「未找到站点」。宁可这里就拦下来
    报清楚，也不要跑到阶段②才炸。

    pause_ctrl：可选的暂停控制器，需实现 `async wait_if_paused()`。Web 侧把 PublishJob
    传进来（内部按 paused 标志阻塞到 resume），CLI 不传＝永不暂停。只在【商品之间】
    检查——不打断正在跑的商品：单个商品表单填到一半就停，会让页面停在半填状态，
    而阶段里的 LLM/生图调用也无法安全取消，与其留一个难恢复的烂摊子，不如让当前
    商品跑完、停在一个干净的收尾边界（商品跑完要么已落库、要么记了 fail 状态）。
    """

    prefs = preferences.load_prefs()
    store = store or prefs.get("store") or ""
    site = site or prefs.get("site") or ""
    batch = int(time.time())
    # 【告警包装放在这里而不是函数开头】它要带上店铺/站点进卡片，而这两个值刚由
    # prefs 回填定下来；放前面就只能报空。包装之后本函数与 publish_one 都用这个
    # on_progress，故前置校验的 aborted 也在告警覆盖范围内（那些正是最该报的中断）。
    on_progress = _alert_hook(on_progress, store, site, len(tasks or []))

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
    preferences.save_prefs({"store": store, "site": site})

    total = len(tasks)
    await _emit(on_progress, {"type": "batch_start", "total": total,
                              "store": store, "site": site, "batch": batch})
    await _emit(on_progress, {"type": "log", "level": "info",
                              "message": f"本次使用模型：{active_llm_label()}"})
    # 仓库与模型同理要在开跑就写明：它决定 ⑪ 勾哪个仓，事后从阶段结论反推不如
    # 开跑可见（用户指定=发布页下拉真实选项；未指定=config 站点映射猜）
    await _emit(on_progress, {
        "type": "log", "level": "info",
        "message": (f"本次仓库：{warehouse}" if warehouse
                    else "本次仓库：未指定（⑪ 按 config 站点映射选择）")})
    # 视频取向要在跑之前就报出来：这两条路线耗时差几十秒到几分钟每个商品，
    # 事后从阶段结论里反推不如开跑就写明（与「本次使用模型」同一处）
    await _emit(on_progress, {
        "type": "log", "level": "info",
        "message": ("本次保留产品视频：按比例合规化后回填" if keep_video
                    else "本次丢弃产品视频：编辑页直接删除，不做比例审核")})
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
            # 暂停闸门：放在循环最顶部，暂停时当前商品已经收尾，这里阻塞到恢复为止。
            # 暂停期间 session 一直开着（不 close），恢复后下方 ensure_cdp_alive 会兜住
            # 「暂停太久 CDP 掉线」的情况。
            if pause_ctrl is not None:
                await pause_ctrl.wait_if_paused()
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
                key = publish_state._task_key(task)
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
                r = await publish_one(session, task, store, site, on_progress,
                                      index=i, total=total, from_stage=from_stage,
                                      use_cache=use_cache, do_publish=do_publish,
                                      price=price, keep_video=keep_video,
                                      warehouse=warehouse)
            except Exception as e:
                logger.exception(f"[{key}] 商品级异常")
                r = {"status": "fail", "rowid": task.get("rowid"), "failed_stage": "",
                     "note": f"异常：{e}"}
            if r["status"] == "ok":
                ok += 1
            else:
                fail += 1
                # 【失败保留编辑页签】编辑页上还留着未落库的表单，直接进下一个商品会
                # navigate 冲掉这份现场（等于关页签重开、从头再来）。故把页签原样
                # 留在浏览器里给人工接着处理，另开新页签跑后面的商品。
                if session.edit_page_open:
                    parked = await session.park_edit_tab()
                    if parked.get("ok"):
                        await _emit(on_progress, {
                            "type": "log", "level": "warning",
                            "message": (f"[{key}] 商品失败，编辑页签已保留（含未保存的修改），"
                                        "后续商品在新页签继续；请人工到该页签接着处理")})
            await _emit(on_progress, {"type": "product_done", "index": i, "total": total,
                                      "offer": key, "rowid": r.get("rowid"),
                                      # title 原先只在 product_start 里（前端自己记着）。
                                      # 告警卡片要在一条消息里说清是哪个商品，故这里带上，
                                      # 前端多收一个字段无影响。
                                      "title": task.get("title") or "",
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
