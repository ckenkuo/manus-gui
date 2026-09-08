import asyncio
import os
import threading
import tomllib
import uuid
import webbrowser
import json
from datetime import datetime
from functools import partial
from json import dumps
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import BUNDLE_ROOT, DATA_ROOT, PROJECT_ROOT


def _resolve_user_config_path() -> Path:
    """用户配置 config.toml 的落点（读写共用同一口径）。

    与 app/config.py 的 _get_config_path 保持一致：可写侧优先。
    已存在就用已存在的那份，避免出现「读的是 A、写的是 B」的分裂；
    都不存在时返回可写侧路径，供保存接口首次创建。
    """
    for root in (DATA_ROOT, PROJECT_ROOT):
        candidate = root / "config" / "config.toml"
        if candidate.exists():
            return candidate
    return DATA_ROOT / "config" / "config.toml"

app = FastAPI()

# 静态资源与模板属「只读随包」侧：冻结后它们被解到 _internal/，而进程工作目录
# 通常是 exe 所在目录甚至任意目录，原先的相对路径 "static"/"templates" 会直接
# 让 StaticFiles 在构造时抛 RuntimeError（目录不存在），Web 端起不来。
# 统一走 BUNDLE_ROOT 取绝对路径；开发态该值即项目根，行为不变。
_STATIC_DIR = BUNDLE_ROOT / "static"
_TEMPLATES_DIR = BUNDLE_ROOT / "templates"

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Task(BaseModel):
    id: str
    prompt: str
    created_at: datetime
    status: str
    steps: list = []

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        data["created_at"] = self.created_at.isoformat()
        return data


class TaskManager:
    def __init__(self):
        self.tasks = {}
        self.queues = {}

    def create_task(self, prompt: str) -> Task:
        task_id = str(uuid.uuid4())
        task = Task(
            id=task_id, prompt=prompt, created_at=datetime.now(), status="pending"
        )
        self.tasks[task_id] = task
        self.queues[task_id] = asyncio.Queue()
        return task

    async def update_task_step(
        self, task_id: str, step: int, result: str, step_type: str = "step"
    ):
        if task_id in self.tasks:
            task = self.tasks[task_id]
            task.steps.append({"step": step, "result": result, "type": step_type})
            await self.queues[task_id].put(
                {"type": step_type, "step": step, "result": result}
            )
            await self.queues[task_id].put(
                {"type": "status", "status": task.status, "steps": task.steps}
            )

    async def complete_task(self, task_id: str, result: str):
        if task_id in self.tasks:
            task = self.tasks[task_id]
            task.status = "completed"
            await self.queues[task_id].put(
                {"type": "status", "status": task.status, "steps": task.steps}
            )
            await self.queues[task_id].put({"type": "complete", "result": result})

    async def fail_task(self, task_id: str, error: str):
        if task_id in self.tasks:
            self.tasks[task_id].status = f"failed: {error}"
            await self.queues[task_id].put({"type": "error", "message": error})


task_manager = TaskManager()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/collect", response_class=HTMLResponse)
async def collect_page(request: Request):
    """批量采集页：清单展示 + 一键开跑 + SSE 实时进度（独立于通用对话页）。"""
    return templates.TemplateResponse("collect.html", {"request": request})


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    """活动管理页：SPU 清单 + dry-run/正式执行开关 + SSE 实时进度（独立页）。

    最高优先级安全：涉及真实商家账号的不可逆操作，前端「正式执行」默认关闭，
    默认只跑 dry-run（只读+算计划+出报名清单，不点任何变更按钮），对标采集页 base_only。
    """
    return templates.TemplateResponse("activity.html", {"request": request})


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    """订单登记页：Temu 待发货订单 → 本地订单登记表（含 DISPIMG 主图），SSE 实时进度。

    同活动页的安全语义：写登记表不可逆（虽有自动备份），前端「正式写入」默认关闭，
    默认只跑试跑——走完导出与解析，只把「将写入什么」列出来给人核对。
    """
    return templates.TemplateResponse("orders.html", {"request": request})


@app.get("/publish", response_class=HTMLResponse)
async def publish_page(request: Request):
    """商品发布页：1688 链接/认领行 → 店小秘 Temu 半托管刊登（15 阶段），SSE 实时进度。

    发布闸门：页面上有「自动发布」开关（默认开），勾着就在 ⑭ 保存成功后继续走
    ⑮「立即发布」。不勾则收尾在保存落库，草稿留在店小秘等人工核对。
    """
    return templates.TemplateResponse("publish.html", {"request": request})


@app.get("/download")
async def download_file(file_path: str):
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(file_path, filename=os.path.basename(file_path))


@app.post("/tasks")
async def create_task(prompt: str = Body(..., embed=True)):
    task = task_manager.create_task(prompt)
    asyncio.create_task(run_task(task.id, prompt))
    return {"task_id": task.id}


from app.agent.manus import Manus


async def run_task(task_id: str, prompt: str):
    try:
        task_manager.tasks[task_id].status = "running"

        agent = Manus(
            name="Manus",
            description="A versatile agent that can solve various tasks using multiple tools",
        )

        async def on_think(thought):
            await task_manager.update_task_step(task_id, 0, thought, "think")

        async def on_tool_execute(tool, input):
            await task_manager.update_task_step(
                task_id, 0, f"Executing tool: {tool}\nInput: {input}", "tool"
            )

        async def on_action(action):
            await task_manager.update_task_step(
                task_id, 0, f"Executing action: {action}", "act"
            )

        async def on_run(step, result):
            await task_manager.update_task_step(task_id, step, result, "run")

        from app.logger import logger

        class SSELogHandler:
            def __init__(self, task_id):
                self.task_id = task_id

            async def __call__(self, message):
                import re

                # Extract - Subsequent Content
                cleaned_message = re.sub(r"^.*? - ", "", message)

                event_type = "log"
                if "✨ Manus's thoughts:" in cleaned_message:
                    event_type = "think"
                elif "🛠 Manus selected" in cleaned_message:
                    event_type = "tool"
                elif "🎯 Tool" in cleaned_message:
                    event_type = "act"
                elif "📝 Oops!" in cleaned_message:
                    event_type = "error"
                elif "🏁 Special tool" in cleaned_message:
                    event_type = "complete"

                await task_manager.update_task_step(
                    self.task_id, 0, cleaned_message, event_type
                )

        sse_handler = SSELogHandler(task_id)
        hwnd = logger.add(sse_handler)

        result = await agent.run(prompt)
        logger.remove(hwnd)
        await task_manager.update_task_step(task_id, 1, result, "result")
        await asyncio.sleep(3)
        await task_manager.complete_task(task_id, result)
    except Exception as e:
        await task_manager.fail_task(task_id, str(e))


@app.get("/tasks/{task_id}/events")
async def task_events(task_id: str):
    async def event_generator():
        if task_id not in task_manager.queues:
            yield f"event: error\ndata: {dumps({'message': 'Task not found'})}\n\n"
            return

        queue = task_manager.queues[task_id]

        task = task_manager.tasks.get(task_id)
        if task:
            yield f"event: status\ndata: {dumps({'type': 'status', 'status': task.status, 'steps': task.steps})}\n\n"

        while True:
            try:
                event = await queue.get()
                formatted_event = dumps(event)

                yield ": heartbeat\n\n"

                if event["type"] == "complete":
                    yield f"event: complete\ndata: {formatted_event}\n\n"
                    break
                elif event["type"] == "error":
                    yield f"event: error\ndata: {formatted_event}\n\n"
                    break
                elif event["type"] == "step":
                    task = task_manager.tasks.get(task_id)
                    if task:
                        yield f"event: status\ndata: {dumps({'type': 'status', 'status': task.status, 'steps': task.steps})}\n\n"
                    yield f"event: {event['type']}\ndata: {formatted_event}\n\n"
                elif event["type"] in ["think", "tool", "act", "run"]:
                    yield f"event: {event['type']}\ndata: {formatted_event}\n\n"
                else:
                    yield f"event: {event['type']}\ndata: {formatted_event}\n\n"

            except asyncio.CancelledError:
                print(f"Client disconnected for task {task_id}")
                break
            except Exception as e:
                print(f"Error in event stream: {str(e)}")
                yield f"event: error\ndata: {dumps({'message': str(e)})}\n\n"
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/tasks")
async def get_tasks():
    sorted_tasks = sorted(
        task_manager.tasks.values(), key=lambda task: task.created_at, reverse=True
    )
    return JSONResponse(
        content=[task.model_dump() for task in sorted_tasks],
        headers={"Content-Type": "application/json"},
    )


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    if task_id not in task_manager.tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return task_manager.tasks[task_id]


@app.get("/config/status")
async def check_config_status():
    # 必须与 app/config.py 的查找口径一致：可写侧（DATA_ROOT）优先。
    # 冻结后 __file__ 指向 _internal，那里只有 example，本接口会恒定报 missing，
    # 前端每次启动都弹配置向导，且报告的不是真正生效的那份配置。
    config_path = _resolve_user_config_path()
    example_config_path = next(
        (
            p
            for p in (
                DATA_ROOT / "config" / "config.example.toml",
                PROJECT_ROOT / "config" / "config.example.toml",
                BUNDLE_ROOT / "config" / "config.example.toml",
            )
            if p.exists()
        ),
        None,
    )

    if config_path.exists():
        return {"status": "exists"}
    elif example_config_path is not None:
        try:
            with open(example_config_path, "rb") as f:
                example_config = tomllib.load(f)
            return {"status": "missing", "example_config": example_config}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    else:
        return {"status": "no_example"}


@app.post("/config/save")
async def save_config(config_data: dict = Body(...)):
    try:
        # 存到可写侧。原先写进 _internal/config：用户在文件管理器里找不到，
        # 升级覆盖 _internal 就丢，而且 config 查找是可写侧优先——一旦别处也有
        # config.toml，这份会被永久静默忽略，表现为「保存成功但不生效」。
        config_path = _resolve_user_config_path()
        config_dir = config_path.parent
        config_dir.mkdir(parents=True, exist_ok=True)

        toml_content = ""

        if "llm" in config_data:
            toml_content += "# Global LLM configuration\n[llm]\n"
            llm_config = config_data["llm"]
            for key, value in llm_config.items():
                if key != "vision":
                    if isinstance(value, str):
                        toml_content += f'{key} = "{value}"\n'
                    else:
                        toml_content += f"{key} = {value}\n"

        if "server" in config_data:
            toml_content += "\n# Server configuration\n[server]\n"
            server_config = config_data["server"]
            for key, value in server_config.items():
                if isinstance(value, str):
                    toml_content += f'{key} = "{value}"\n'
                else:
                    toml_content += f"{key} = {value}\n"

        with open(config_path, "w", encoding="utf-8") as f:
            f.write(toml_content)

        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==== 协作文档链接登记簿（app/cloud_docs.py）：订单页/采集页共用 ==============
# 凡是用过的 kdocs 协作链接都会自动登记；这里提供列表（UI 下拉候选）、命名/改名、
# 删除三个接口，纯本地读写，不碰云端文档本身。

from app import cloud_docs


@app.get("/api/cloud_docs")
async def cloud_docs_list():
    """登记簿列表 [{name, url, last_used}]，按最近使用倒序，供 UI 下拉候选。"""
    return JSONResponse(content={"docs": cloud_docs.list_docs()})


@app.post("/api/cloud_docs")
async def cloud_docs_save(
    url: str = Body(..., embed=True), name: str = Body("", embed=True)
):
    """登记/更新一条链接（主要是给链接起名字）；url 为空报 400。"""
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="url 不能为空")
    cloud_docs.remember(url, name=name)
    return {"status": "success"}


@app.delete("/api/cloud_docs")
async def cloud_docs_delete(url: str):
    """从登记簿删掉一条链接。"""
    return {"status": "success" if cloud_docs.remove(url) else "not_found"}


# ==== 批量采集（Temu→1688→Excel）：确定性管道，独立于通用 agent 任务 ==========
# UI 复用 app/collect/service.py 的 run_batch，进度经 on_progress 结构化事件走 SSE。
# 与 /tasks 的区别：/tasks 是通用 agent 自由循环；采集是确定性批处理作业，事件语义
# 专属（SPU/比价/无同款留空），故单开一组接口，不挤进通用 step。

from app.collect import service as collect_service


class CollectJob:
    """一次批量采集作业：持有进度队列，供 SSE 消费。"""

    def __init__(
        self, job_id: str, limit: int, use_pipeline: bool,
        excel: str = "", sheet: str = "", store: str = "", base_only: bool = True,
    ):
        self.id = job_id
        self.limit = limit
        self.use_pipeline = use_pipeline
        self.base_only = base_only
        self.excel = excel
        self.sheet = sheet
        self.store = store
        self.queue: asyncio.Queue = asyncio.Queue()
        self.done = False
        self.summary: dict = {}

    async def push(self, event: dict):
        await self.queue.put(event)


collect_jobs: dict = {}


@app.get("/collect/worklist")
async def collect_worklist(
    excel: str = "", sheet: str = "", store: str = "", doc_mode: str = "",
):
    """UI 首屏：清单总量/已入库/待采 + 每条状态（不触发采集）。

    可选 excel/sheet/store 过滤：缺省回填上次选择。响应含 workbooks/sheets/stores 供下拉。
    doc_mode（local/cloud）钉死本地表还是协作文档；不传则沿用上次选的模式。
    附带回传区域状态（当前区域 + 该账号可选区域，供下拉渲染；best-effort，读不到只带 error）。
    """
    status = collect_service.get_worklist_status(
        excel=excel or None, sheet=sheet or None, store=store or None,
        doc_mode=doc_mode or None,
    )
    status["region"] = await collect_service.peek_regions()
    status["region_label"] = collect_service.load_prefs().get("region_label") or ""
    return JSONResponse(content=status)


@app.post("/collect/enumerate")
async def collect_enumerate(status: str = Body("", embed=True),
                            region: str = Body("", embed=True)):
    """重新枚举所有已打开店铺标签的清单，写 worklist.json，返回条数。

    status：采集范围。空串（默认）= 跟随各店 Temu 页面当前的页签 + 所有筛选（类目/站点/
    商品名等），点页面「查询」原样采集；非空 = 先替你切到该页签再查询。本次选择记入偏好回显。
    region：目标区域（顶栏「全球 / 美国 / 欧区」）。**以这里选的为准**——浏览器停在别的
    区域会先替你切过去再采；空串 = 跟随浏览器当前区域。
    """
    tab = status or ""
    # 记住本次采集范围与区域，保留已存的 excel/sheet/store 不被覆盖；
    # 云端目标存在 cloud_url 键（与 excel 互斥），回传时二选一，避免被空 excel 清掉
    prefs = collect_service.load_prefs()
    collect_service.save_prefs(
        prefs.get("excel") or prefs.get("cloud_url") or "",
        prefs.get("sheet", ""), prefs.get("store", ""), tab,
        region_label=region or "",
    )
    # 区域/页签问题（没开商品列表页 / 显式指定的区域不存在 / 切过去后复核失败）是可操作的
    # 用户侧问题，不是服务异常：回 409 + 明确文案，让前端红条提示，而不是抛 500 堆栈。
    # 注意采集本身已不再要求「先确认区域」——留空区域时按各页签所在域名归类，不会因读不到
    # 顶栏而 409（见 collect_service.enumerate_worklist）。
    try:
        count = await collect_service.enumerate_worklist(
            status_tab=tab, region_label=region or "")
    except collect_service.RegionNotConfirmed as e:
        return JSONResponse(
            status_code=409,
            content={"error": "region_not_confirmed", "reason": str(e),
                     "status": collect_service.get_worklist_status()},
        )
    return {"count": count, "status": collect_service.get_worklist_status()}


@app.post("/collect/batch")
async def collect_batch(
    limit: int = Body(20, embed=True),
    use_pipeline: bool = Body(True, embed=True),
    base_only: bool = Body(True, embed=True),
    excel: str = Body("", embed=True),
    sheet: str = Body("", embed=True),
    store: str = Body("", embed=True),
    doc_mode: str = Body("", embed=True),
    append_mode: str = Body("", embed=True),
    append_row: Optional[int] = Body(None, embed=True),
):
    """启动一批采集作业，返回 job_id；进度经 /collect/batch/{job_id}/events (SSE) 消费。

    base_only=True（默认）只采 Temu 基础信息、采购价/重量留空待人工填；False 走 1688 自动采价。
    excel/sheet/store 指定目标工作簿/Sheet/店铺（缺省回填上次选择）。
    doc_mode（local/cloud）钉死写本地表还是协作文档：kdocs 有配额，用满了要能立刻切回
    本地继续干活。不传则按链接形态与 config 自动判（历史行为）。
    append_mode 选新行落点：bottom（默认，追加末尾）/ top（表头下）/ row_down / row_up
    （后两者配 append_row 指定起始行）。不传沿用上次选择。
    """
    job_id = str(uuid.uuid4())
    job = CollectJob(job_id, limit, use_pipeline, excel, sheet, store, base_only)
    collect_jobs[job_id] = job

    async def _on_progress(event: dict):
        await job.push(event)

    async def _run():
        try:
            job.summary = await collect_service.run_batch(
                limit=limit, use_pipeline=use_pipeline, base_only=base_only,
                on_progress=_on_progress,
                excel=excel or None, sheet=sheet or None, store=store or None,
                doc_mode=doc_mode,
                append_mode=append_mode, append_row=append_row,
            )
        except Exception as e:
            await job.push({"type": "aborted", "reason": f"采集异常：{e}"})
        finally:
            job.done = True
            await job.push({"type": "_end"})  # 哨兵：通知 SSE 收尾

    asyncio.create_task(_run())
    return {"job_id": job_id}


@app.get("/collect/batch/{job_id}/events")
async def collect_batch_events(job_id: str):
    """SSE 推送某次采集作业的结构化进度事件（原样转发 service 的 on_progress 事件）。"""

    async def event_generator():
        job = collect_jobs.get(job_id)
        if job is None:
            yield f"event: error\ndata: {dumps({'reason': 'job not found'})}\n\n"
            return
        while True:
            try:
                event = await job.queue.get()
            except asyncio.CancelledError:
                break
            if event.get("type") == "_end":
                yield f"event: done\ndata: {dumps(job.summary)}\n\n"
                break
            yield f"event: {event.get('type', 'log')}\ndata: {dumps(event)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ==== 活动管理（关加速器→报活动→重开加速器）：确定性批处理，独立于通用 agent ========
# 对标上面的采集接口：ActivityJob 照抄 CollectJob（内部队列 + on_progress 塞事件 + SSE 逐条
# yield）。与采集不同的是【安全语义】：涉及真实商家账号的不可逆操作，dry_run 默认 True，
# 只有前端显式开「正式执行」才 False。工作簿/Sheet 枚举复用采集 service，不重复造。

from app.activity import service as activity_service


class ActivityJob:
    """一次活动管理作业：持有进度队列，供 SSE 消费（对标 CollectJob）。"""

    def __init__(
        self, job_id: str, excel: str = "", sheet: str = "", dry_run: bool = True,
    ):
        self.id = job_id
        self.excel = excel
        self.sheet = sheet
        self.dry_run = dry_run
        self.queue: asyncio.Queue = asyncio.Queue()
        self.done = False
        self.summary: dict = {}

    async def push(self, event: dict):
        await self.queue.put(event)


activity_jobs: dict = {}


@app.get("/activity/worklist")
async def activity_worklist(excel: str = "", sheet: str = ""):
    """活动页首屏：可选工作簿/Sheet 列表 + 上次选择回显 + 全局毛利率红线默认。

    工作簿/Sheet 枚举直接复用采集 service 的 get_worklist_status（同一数据源，不重复造），
    只额外附上活动模块的全局默认毛利率（config [activity].min_margin，缺失兜底 0.15）。
    纯读、不触发任何变更。
    """
    status = collect_service.get_worklist_status(
        excel=excel or None, sheet=sheet or None,
    )
    return JSONResponse(content={
        "excel": status.get("excel", ""),
        "sheet": status.get("sheet", ""),
        "workbooks": status.get("workbooks", []),
        "sheets": status.get("sheets", []),
        "excel_locked": status.get("excel_locked", False),
        # 活动模块的全局默认毛利率红线（前端批量毛利率输入框初值）
        "min_margin": activity_service.global_min_margin(),
    })


@app.post("/activity/batch")
async def activity_batch(
    excel: str = Body("", embed=True),
    sheet: str = Body("", embed=True),
    spus: str = Body("", embed=True),
    min_margin: Optional[float] = Body(None, embed=True),
    dry_run: bool = Body(True, embed=True),
):
    """启动一批活动管理作业，返回 job_id；进度经 /activity/batch/{job_id}/events (SSE) 消费。

    安全：dry_run 默认 True（只读+算计划+出报名清单，不点任何变更按钮）；只有前端显式
    开「正式执行」才传 False。spus 为原始清单文本（支持逐品 "spu:margin" 语法），min_margin
    为本批统一毛利率红线（缺省走 config 全局默认）。excel/sheet 为目标成本核算表与 Sheet。
    """
    job_id = str(uuid.uuid4())
    job = ActivityJob(job_id, excel, sheet, dry_run)
    activity_jobs[job_id] = job

    async def _on_progress(event: dict):
        await job.push(event)

    async def _run():
        try:
            job.summary = await activity_service.run_activity_batch(
                spus, excel, sheet,
                min_margin=min_margin, dry_run=dry_run, live=not dry_run,
                on_progress=_on_progress,
            )
        except Exception as e:
            await job.push({"type": "aborted", "reason": f"活动管理异常：{e}"})
        finally:
            job.done = True
            await job.push({"type": "_end"})  # 哨兵：通知 SSE 收尾

    asyncio.create_task(_run())
    return {"job_id": job_id}


@app.get("/activity/batch/{job_id}/events")
async def activity_batch_events(job_id: str):
    """SSE 推送某次活动管理作业的结构化进度事件（原样转发 service 的 on_progress 事件）。"""

    async def event_generator():
        job = activity_jobs.get(job_id)
        if job is None:
            yield f"event: error\ndata: {dumps({'reason': 'job not found'})}\n\n"
            return
        while True:
            try:
                event = await job.queue.get()
            except asyncio.CancelledError:
                break
            if event.get("type") == "_end":
                yield f"event: done\ndata: {dumps(job.summary)}\n\n"
                break
            yield f"event: {event.get('type', 'log')}\ndata: {dumps(event)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---- 订单登记接口（对标 /activity/*）---------------------------------------
# 同样的作业模式：POST 起作业拿 job_id，进度走 SSE。安全语义也一致——写登记表不可逆
# （虽有自动备份），dry_run 默认 True，只有前端显式开「正式写入」才 False。
# 目标工作簿/Sheet 由用户在页面上选（对标活动页的工作簿+Sheet 选择器），config 只提供缺省
# 值；显式选了 Sheet 就绕过 sheet_map 的站点分流——用户自己判断落点，管线不猜也不拦。

from app.orders import service as orders_service


class OrdersJob:
    """一次订单登记作业：持有进度队列，供 SSE 消费（对标 ActivityJob）。"""

    def __init__(self, job_id: str, store: str = "", dry_run: bool = True):
        self.id = job_id
        self.store = store
        self.dry_run = dry_run
        self.queue: asyncio.Queue = asyncio.Queue()
        self.done = False
        self.summary: dict = {}

    async def push(self, event: dict):
        await self.queue.put(event)


orders_jobs: dict = {}


@app.get("/orders/worklist")
async def orders_worklist(store: str = "", workbook: str = "", sheet: str = "",
                          doc_mode: str = ""):
    """订单页首屏/切换：可选工作簿与 Sheet 列表、上次选择回显、选中 Sheet 的可写性。

    query 参数缺省（不传）时回填「上次选择」，再兜底 config 的 [orders].workbook。
    注意 store/sheet 用空串表示「本次不覆盖」，与 service 的 None 语义对齐：显式传空串
    是「清空选择」，不传则沿用偏好。纯读，不触发采集或写入。
    doc_mode（local/cloud）钉死本地登记表还是协作文档；不传则沿用上次选的模式。
    附带回传当前浏览器选定的区域（只读展示，best-effort，读不到只带 error）——区域决定
    这批能看到哪些订单，操作者点「开始」前要能对一眼。
    """
    status = orders_service.get_worklist_status(
        store=store or None, workbook=workbook or None, sheet=sheet or None,
        doc_mode=doc_mode or None,
    )
    status["region"] = await orders_service.peek_current_region()
    status["region_label"] = orders_service.load_prefs().get("region_label") or ""
    return JSONResponse(content=status)


@app.get("/orders/sheet_info")
async def orders_sheet_info(workbook: str, sheet: str, doc_mode: str = ""):
    """单独探测某 Sheet 的可写性（判重列是否齐、图片列、表头行）。

    独立成一个接口是因为登记表 102MB，切 Sheet 时只该解析这一张表的表头，不必把整个
    worklist（含工作簿枚举）重算一遍。

    doc_mode 与 worklist 同一套语义：选了本地就只探本地表，不去碰协作文档（否则本地模式
    下这里仍会按 config 的 cloud_file_id 去读云端，探出来的表头根本不是要写的那张）。
    """
    cfg = orders_service.load_orders_config()
    cloud, local_wb, _ = orders_service.resolve_doc_target(cfg, workbook, doc_mode)
    return JSONResponse(content=orders_service.inspect_sheet(
        local_wb or workbook, sheet, list(cfg.get("dedupe_by") or []), cloud=cloud,
    ))


@app.post("/orders/batch")
async def orders_batch(
    store: str = Body("", embed=True),
    dry_run: bool = Body(True, embed=True),
    max_pages: int = Body(200, embed=True),
    workbook: str = Body("", embed=True),
    sheet: str = Body("", embed=True),
    allow_no_price: bool = Body(True, embed=True),
    incremental: bool = Body(True, embed=True),
    doc_mode: str = Body("", embed=True),
    region: str = Body("", embed=True),
):
    """启动一批订单登记作业，返回 job_id；进度经 /orders/batch/{job_id}/events (SSE) 消费。

    store 为空则由页面识别当前登录店铺；识别不到会中止（店铺决定写哪张表，不猜）。
    workbook/sheet 为空则用 config 缺省值并按 sheet_map 分流；显式给了 sheet 就全部写进
    那一张表。max_pages 供冒烟用（只翻前 N 页）。

    allow_no_price 默认 True（2026-07-29 确认允许空成交价）：插件回填成交单价有约一天
    延迟，等价会让当天的单全部积压，故「平台成交价」允许留空、订单照常登记。传 False 才
    恢复「无价留到下批」的旧口径。

    incremental 默认 True，但只在显式指定了 sheet 时真正生效：采集前读该 Sheet 已登记的
    订单号当水位，翻页追上就停。按 sheet_map 分流时水位有歧义，自动退全量。

    doc_mode（local/cloud）钉死写本地登记表还是协作文档：kdocs 有配额，用满了要能立刻
    切回本地继续干活。不传则按链接形态与 config 自动判（历史行为）。

    region：目标区域（顶栏「全球 / 美国 / 欧区」），**以这里选的为准**——浏览器停在别的
    区域会先切过去，list_url 的域名也按该区域改写（只换域名，筛选与排序参数原样保留）。
    空串＝跟随浏览器当前区域。
    """
    job_id = str(uuid.uuid4())
    job = OrdersJob(job_id, store, dry_run)
    orders_jobs[job_id] = job
    # 记住本次选择，下次开页直接回填（写失败不影响本批）
    orders_service.save_prefs(store=store, workbook=workbook, sheet=sheet,
                              doc_mode=doc_mode, region_label=region or "")

    async def _on_progress(event: dict):
        await job.push(event)

    async def _run():
        try:
            job.summary = await orders_service.run_orders_batch(
                store=store, dry_run=dry_run, max_pages=max_pages,
                workbook=workbook, sheet=sheet,
                on_progress=_on_progress,
                require_price=not allow_no_price,
                incremental=incremental,
                doc_mode=doc_mode,
                region_label=region or "",
            )
        except Exception as e:
            await job.push({"type": "aborted", "reason": f"订单登记异常：{e}"})
        finally:
            job.done = True
            await job.push({"type": "_end"})  # 哨兵：通知 SSE 收尾

    asyncio.create_task(_run())
    return {"job_id": job_id}


@app.get("/orders/batch/{job_id}/events")
async def orders_batch_events(job_id: str):
    """SSE 推送某次订单登记作业的结构化进度事件（原样转发 service 的 on_progress 事件）。"""

    async def event_generator():
        job = orders_jobs.get(job_id)
        if job is None:
            yield f"event: error\ndata: {dumps({'reason': 'job not found'})}\n\n"
            return
        while True:
            try:
                event = await job.queue.get()
            except asyncio.CancelledError:
                break
            if event.get("type") == "_end":
                yield f"event: done\ndata: {dumps(job.summary)}\n\n"
                break
            # service 的 batch 收尾事件也叫 done，与 SSE 收尾哨兵撞名，转发时改名成
            # batch_done，避免前端 done 处理器被同样的汇总触发两次。
            name = event.get("type", "log")
            if name == "done":
                name = "batch_done"
            yield f"event: {name}\ndata: {dumps(event)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---- 商品发布接口（店小秘 Temu 半托管，对标 /collect/batch 三件套）-----------------
# PublishJob 照抄 CollectJob：内部队列 + on_progress 塞事件 + SSE 逐条 yield。
# 与采集/订单的区别：tasks 由前端从 textarea 解析（每行一个 1688 链接，或 rowid|info_path），
# 后端不做清单枚举；阶段编排与 15 阶段语义全在 app/publish/service.py。
# 发布闸门：/publish/batch 接 do_publish（默认 True，前端 chkDoPublish 开关控制），
# 勾着就在 ⑭ 保存成功后继续 ⑮「立即发布」；发布不可逆，关掉开关即收尾在保存落库。
# service 的批次收尾事件就叫 batch_done，与 SSE 哨兵映射出的 done 不撞名（不同于
# /orders/batch 那边 service 收尾事件叫 done 需改名转发），故原样转发即可。

from app.publish import service as publish_service
from app.publish import llm as publish_llm
from app.publish import cache as publish_cache
from app.publish import shops as publish_shops
from app.publish import alert as publish_alert
from app.publish.pipeline import resolve_warehouse


@app.get("/publish/stores")
async def publish_store_list():
    """当前登录账号下的店铺清单，供发布页「店铺」下拉渲染。

    走店小秘的 /api/userIn.json（秒级、无副作用），细节与「为什么站点不能一起给」
    见 app/publish/shops.py 的模块 docstring。连不上 Chrome / 未登录时返回 503：
    这不是请求写错了，是环境没就绪，前端据此提示用户去开调试 Chrome。
    """
    try:
        return await publish_shops.fetch_stores()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/publish/sites")
async def publish_site_list(store: str, refresh: bool = False):
    """某店铺可认领的站点清单，供发布页「站点」下拉渲染。

    站点只能从认领弹窗里读（勾中店铺后才渲染），故首次要 3~5 秒并占用那个 CDP
    页面；结果按店铺缓存到磁盘，之后秒出。refresh=true 强制重探（店铺新开通了
    站点时用）。探测过程只读、不点「确定」，不会产生任何认领。
    """
    try:
        return await publish_shops.fetch_sites(store, refresh=refresh)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/publish/warehouses")
async def publish_warehouse_list(store: str, site: str, refresh: bool = False):
    """某店铺在某站点的可选仓库清单，供发布页「仓库」下拉渲染。

    走 /api/popTemuCategory/warehouseList.json（秒级、无副作用），结果按
    「店铺|站点」缓存到磁盘。响应里的 default 是 config 的 [publish] 站点仓库
    映射——它现在只做「推荐默认值」（命中真实列表才带出来），不再是阶段⑪
    盲目猜测的依据；查不到/未命中时前端不预选。
    """
    try:
        r = await publish_shops.fetch_warehouses(store, site, refresh=refresh)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    mapped = resolve_warehouse(site)
    r["default"] = mapped if mapped in (r.get("warehouses") or []) else ""
    return r


@app.get("/publish/llm")
async def publish_llm_list():
    """发布管线可选模型清单 + 按阶段配置，供发布页下拉与阶段表格渲染。

    stages 一起返回而不另开接口：前端渲染阶段表格时每行都要拿到完整的模型选项
    （含可用性/是否多模态）才能决定禁用哪些项，分两个接口只会让它自己去对齐。
    """
    return {"choices": publish_llm.list_llm_choices(),
            "stages": publish_llm.list_llm_stages()}


@app.post("/publish/llm")
async def publish_llm_switch(choice: str = Body(..., embed=True)):
    """切换发布管线使用的模型。拒切未配置 api_key 的选项（切了也只会全线失败）。"""
    for c in publish_llm.list_llm_choices():
        if c["id"] == choice:
            if not c["available"]:
                # 段名取 LLM_CHOICES 里登记的 config_name，别按 choice 拼——grok 的段名
                # 是 [llm.publish]（没有后缀），拼出来的路径会让人去改一个不存在的段
                section = publish_llm.LLM_CHOICES[choice]["config_name"]
                raise HTTPException(
                    400, f"{c['label']} 未配置 api_key：请先在 config/config.toml "
                         f"的 [llm.{section}] 段填上再切换")
            publish_llm.set_llm_choice(choice)
            return {"ok": True, "choices": publish_llm.list_llm_choices(),
                    "stages": publish_llm.list_llm_stages()}
    raise HTTPException(400, f"未知模型选择：{choice}")


@app.post("/publish/llm/stage")
async def publish_llm_stage_switch(stage: str = Body(..., embed=True),
                                  choice: Optional[str] = Body(None, embed=True)):
    """设/清某阶段的模型覆盖。choice 传 null 或空串表示「跟随全局默认」。

    未配 key 的选项照 publish_llm_switch 的做法拒掉（切了只会该阶段全线失败）；
    给视觉阶段配非多模态模型由 set_stage_choice 拦，错误信息原样转成 400——
    那是配置错误而非服务端故障，不该以 500 上报。
    """
    if choice:
        info = next((c for c in publish_llm.list_llm_choices() if c["id"] == choice), None)
        if info is None:
            raise HTTPException(400, f"未知模型选择：{choice}")
        if not info["available"]:
            section = publish_llm.LLM_CHOICES[choice]["config_name"]
            raise HTTPException(
                400, f"{info['label']} 未配置 api_key：请先在 config/config.toml "
                     f"的 [llm.{section}] 段填上再切换")
    try:
        publish_llm.set_stage_choice(stage, choice)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "choices": publish_llm.list_llm_choices(),
            "stages": publish_llm.list_llm_stages()}


@app.get("/publish/settings")
async def publish_settings():
    """发布管线的运行参数（目前只有生图并发数），供发布页初始化输入框。"""
    return {"imageConcurrency": publish_service.get_image_concurrency(),
            "imageConcurrencyMax": publish_service.IMAGE_CONCURRENCY_MAX,
            "imageConcurrencyDefault": publish_service.IMAGE_CONCURRENCY_DEFAULT}


@app.post("/publish/settings")
async def publish_settings_save(imageConcurrency: int = Body(..., embed=True)):
    """设生图并发数。越界由 set_image_concurrency 拒掉，原样转 400（配置错误不是故障）。

    为什么做成用户可配置而不是写死：Packy 侧 gpt-image-2 的实际并发上限随网关档位
    与本机出网链路（VPN）变化，最佳值只有用户的环境能测出来（见
    publish_service.get_image_concurrency 的说明）。
    """
    try:
        n = publish_service.set_image_concurrency(imageConcurrency)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "imageConcurrency": n}


@app.get("/publish/cache")
async def publish_cache_list():
    """类目/属性缓存现状（统计 + 明细），供发布页缓存面板渲染。

    命中缓存时阶段③约 15s、未命中要 110s，阶段④同理，所以「现在缓存里有什么」
    是跑批前值得看一眼的信息。
    """
    return publish_cache.cache_stats()


@app.delete("/publish/cache")
async def publish_cache_clear(slug: str = ""):
    """清缓存：带 slug 只删那一个类目的属性缓存，不带则连类目路径清单一起清空。

    清完下一次跑就回落到全量遍历/全量读选项（慢，但不会错），故这里不做二次确认，
    交前端按钮自己问。
    """
    return {"status": "success", "removed": publish_cache.clear(slug)}


class PublishJob:
    """一次商品发布作业：持有进度队列，供 SSE 消费（照抄 CollectJob）。"""

    def __init__(self, job_id: str, store: str = "", site: str = ""):
        self.id = job_id
        self.store = store
        self.site = site
        self.queue: asyncio.Queue = asyncio.Queue()
        self.done = False
        self.summary: dict = {}
        # 暂停控制：paused 由 /publish/batch/{job_id}/pause 置位，_resume 事件负责唤醒。
        # 初始不暂停，_resume 已 set——wait_if_paused 里 paused=False 时根本不看它。
        self.paused = False
        self._resume = asyncio.Event()
        self._resume.set()

    async def push(self, event: dict):
        await self.queue.put(event)

    async def wait_if_paused(self):
        """暂停闸门：paused 置位时阻塞到 resume 事件（run_batch 每商品前调用）。

        真正停住的那一刻发一条 paused 事件让前端把状态文案从「暂停中」切到「已暂停」；
        恢复时发 resumed。两次事件走同一套 SSE 队列，前端刷新重连后仍能消费到积压的
        事件、看到正确的暂停状态。事件在阻塞期间 push 不会卡死——queue 无限容量。
        """
        if not self.paused:
            return
        await self.push({"type": "paused"})
        while self.paused:
            await self._resume.wait()
        await self.push({"type": "resumed"})


publish_jobs: dict = {}


@app.post("/publish/batch")
async def publish_batch(
    tasks: list = Body(..., embed=True),
    store: str = Body("", embed=True),
    site: str = Body("", embed=True),
    from_stage: str = Body("", embed=True),
    use_cache: bool = Body(True, embed=True),
    price: str = Body("", embed=True),
    do_publish: bool = Body(True, embed=True),
    keep_video: bool = Body(True, embed=True),
    warehouse: str = Body("", embed=True),
):
    """启动一批发布作业，返回 job_id；进度经 /publish/batch/{job_id}/events (SSE) 消费。

    tasks 元素：{"url": "...", "title": "..."} 或 {"rowid": "...", "info_path": "...", ...}。
    from_stage 非空时从该阶段续跑（前序阶段按已完成跳过）；空串＝全新跑 15 阶段。
    use_cache=False 时类目与属性都走全量读取（新品类首次跑、或怀疑缓存选错类目时用）。
    price 是 ⑩ 变种表的申报价（人民币），空串＝用管线默认 188.88；这里不做校验，
    交 pipeline.normalize_declare_price 归一（非法值退默认并告警，不让整批中断）。

    warehouse 是 ⑪「选择仓库」要勾的仓库名：前端仓库下拉（/publish/warehouses
    查出的真实选项）选什么就传什么；空串＝退回 config 的 [publish] 站点映射
    （老逻辑，映射缺失时会在 ⑪ 报 no-option）。

    do_publish 是阶段⑮「立即发布」的闸门，【默认 True】——用户 2026-08-25 明确要求
    页面上给开关且默认开启、自动发布。原先本接口刻意不接这个参数（只有 CLI 有
    --publish），实际使用中每次都要人再去命令行跑一遍，反而把「全自动优先」的取向
    抵消掉了。开关在前端（chkDoPublish），默认勾选；不勾则流程仍收尾在 ⑭ 保存落库。
    注意发布不可逆：上架后要下架才能改。

    keep_video 是 ⑬b 产品视频的去留开关（默认 True＝保留）。True 走原来的比例合规化
    （下载→ffmpeg 裁比例→直传→回填，每个商品几十秒到几分钟）；False 就在编辑页直接
    点视频区的「删除」丢弃，整批换速度与确定性。开关在前端 chkKeepVideo。
    """
    job_id = str(uuid.uuid4())
    job = PublishJob(job_id, store, site)
    publish_jobs[job_id] = job

    async def _on_progress(event: dict):
        await job.push(event)

    async def _run():
        try:
            job.summary = await publish_service.run_batch(
                tasks, store=store, site=site,
                on_progress=_on_progress, from_stage=from_stage,
                use_cache=use_cache, price=price, do_publish=do_publish,
                keep_video=keep_video, warehouse=warehouse, pause_ctrl=job,
            )
        except Exception as e:
            # run_batch 内部的中断已由 service 的告警钩子报过（aborted / product_done /
            # batch_done）；能落到这里的是 run_batch 本身抛出的未捕获异常——钩子在
            # run_batch 里，此时已经出不来了，故这一处要自己发一发，否则 Web 端跑批
            # 崩了群里一点动静都没有。
            await publish_alert.alert_batch_crash(str(e), store=store, site=site)
            await job.push({"type": "aborted", "reason": f"发布异常：{e}"})
        finally:
            job.done = True
            await job.push({"type": "_end"})  # 哨兵：通知 SSE 收尾

    asyncio.create_task(_run())
    return {"job_id": job_id}


@app.get("/publish/batch/{job_id}/events")
async def publish_batch_events(job_id: str):
    """SSE 推送某次发布作业的结构化进度事件（原样转发 service 的 on_progress 事件）。"""

    async def event_generator():
        job = publish_jobs.get(job_id)
        if job is None:
            yield f"event: error\ndata: {dumps({'reason': 'job not found'})}\n\n"
            return
        while True:
            try:
                event = await job.queue.get()
            except asyncio.CancelledError:
                break
            if event.get("type") == "_end":
                yield f"event: done\ndata: {dumps(job.summary)}\n\n"
                break
            yield f"event: {event.get('type', 'log')}\ndata: {dumps(event)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/publish/batch/{job_id}/pause")
async def publish_batch_pause(job_id: str, paused: bool = Body(..., embed=True)):
    """暂停 / 恢复某个发布作业。

    暂停是「商品间」粒度：run_batch 在下一个商品开始前才调 wait_if_paused，故这里的
    paused=True 只是置位标志，当前商品仍会跑完（不打断正在填的表单）。真正的停顿点
    在 service 层，前端收到 paused 事件才算「已暂停」。恢复置位 _resume 事件即可。
    """
    job = publish_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "作业不存在")
    if job.done:
        raise HTTPException(409, "批次已结束，无法暂停")
    job.paused = paused
    if paused:
        job._resume.clear()
    else:
        job._resume.set()
    return {"job_id": job_id, "paused": job.paused}


# ---- 采集箱定时扫描（发布页顶部的开关 + 未编辑商品清单）-----------------------
# 定时器跑在服务端（app/publish/collectbox.py），故开关状态是【后端 prefs 的事实】而不是
# localStorage：换个浏览器标签页看到的必须是同一个状态。默认关闭（用户明确要求），
# 由页面顶部那个开关开启。
#
# 【扫的是采集箱 draft 里「已认领但没编辑过」的行】offline（页面文案「待发布」）里躺的
# 是已经编辑过的商品，不是本功能的目标。「编辑过没有」判不了列表接口，只能逐个 rowid
# 查 edit.json——判据与那一堆被证伪的候选判据都记在 collectbox.py 的模块 docstring 里。
#
# 【扫描只读】这几个接口不点任何按钮、不改店小秘任何状态：只导航列表页 + 页面内 fetch
# 读接口。真正的发布仍走 /publish/batch——用户在清单里勾选、选好店铺站点后再发起，
# 定时器本身永远不会自己发布任何东西。
#
# 【与发布作业互斥】扫描与发布抢同一个 CDP 页面，故把「有作业在跑」的判据注入给定时器，
# 让它在作业期间跳过这一轮（见 collectbox.set_busy_checker 与 _tick 的说明）。

from app.publish import collectbox as publish_collectbox

# ---- 数据采集页「未认领」清单定时扫描 ----------------------------------------
# 【与上面的采集箱扫描是两个不同的列表，别混】采集箱（collectbox）里是**已认领**的草稿，
# 带店铺/站点/rowid；这里（crawlbox）扫的是数据采集页「未认领」标签下的采集记录，
# 还没有店铺、没有站点、没有 rowid——正因如此它才要走「认领 → 编辑 → 发布」的完整流程。
# 两者的接口、主键、以及「填进任务框该填什么」全不同，详见 app/publish/crawlbox.py 头部。
#
# 故这里是**另一套**接口与另一个定时器，而不是给 /publish/collectbox 加个参数：
# 用户可能只想扫其中一个，共用一个开关会让「开一个就开两个」。
from app.publish import crawlbox as publish_crawlbox

# ---- 数据搬家清单定时扫描 ----------------------------------------------------
# 【第三张来源清单】数据搬家池里是**别人已在 Temu 上架过的成品**（自带平台 SPU ID），
# 可整条搬到自己店铺；上面两张一个是自采记录（crawlbox）、一个是已认领草稿（collectbox）。
# 三者的列表接口、主键、可用字段全不同，对照表见 app/publish/banjia.py 头部。
# 它的接口在本文件下方（含唯一的写接口 /publish/banjia/claim），这里先导入是为了
# 与另两个模块一起注入 busy 判据、一起在 startup 里起定时器。
from app.publish import banjia as publish_banjia


def _publish_job_busy() -> bool:
    """当前是否有发布作业在跑（定时器据此跳过这一轮扫描）。

    只看未完成的 job：publish_jobs 里的条目跑完不删（SSE 断线重连要能取到 summary），
    故不能拿字典非空当判据。
    """
    return any(not j.done for j in publish_jobs.values())


publish_collectbox.set_busy_checker(_publish_job_busy)
publish_crawlbox.set_busy_checker(_publish_job_busy)
publish_banjia.set_busy_checker(_publish_job_busy)


@app.on_event("startup")
async def _start_collectbox_timer():
    """进程启动时按设置决定是否起定时器（默认关，故全新环境什么都不发生）。

    放在 startup 而不是模块导入时：定时器是 asyncio 任务，要有运行中的事件循环才能建。
    """
    from app.logger import logger

    try:
        if publish_collectbox.start_if_enabled():
            logger.info("采集箱定时扫描按上次设置自动启动")
    except Exception as e:
        # best-effort：定时器起不来不该让整个 Web 服务起不来
        logger.warning(f"采集箱定时扫描启动失败（忽略）：{e}")
    # 【三个定时器各自单独 try】它们互不依赖，一个起不来不该连带另外两个也起不来。
    try:
        if publish_crawlbox.start_if_enabled():
            logger.info("未认领清单定时扫描按上次设置自动启动")
    except Exception as e:
        logger.warning(f"未认领清单定时扫描启动失败（忽略）：{e}")
    try:
        if publish_banjia.start_if_enabled():
            logger.info("数据搬家清单定时扫描按上次设置自动启动")
    except Exception as e:
        logger.warning(f"数据搬家清单定时扫描启动失败（忽略）：{e}")


@app.get("/publish/collectbox")
async def collectbox_state():
    """定时器现状 + 上次扫到的清单，供发布页顶部开关与清单表格渲染。

    【为什么现状与清单一个接口给】页面要同时显示「开关状态/下次扫描时间」和清单，
    分两个接口只会让前端自己去对齐两次往返的时序（清单来了但开关还没渲染完）。
    清单读的是磁盘缓存、不触发扫描，故这个接口很快且无副作用。
    """
    st = publish_collectbox.status()
    scan = publish_collectbox.load_scan()
    return {**st, "items": scan.get("items") or [],
            "states": [{"id": k, "label": v["label"]}
                       for k, v in publish_collectbox.DXM_STATES.items()],
            "intervalMin": publish_collectbox.INTERVAL_MIN,
            "intervalMax": publish_collectbox.INTERVAL_MAX,
            "intervalDefault": publish_collectbox.INTERVAL_DEFAULT}


@app.post("/publish/collectbox/settings")
async def collectbox_settings(enabled: Optional[bool] = Body(None, embed=True),
                              intervalMinutes: Optional[int] = Body(None, embed=True),
                              state: Optional[str] = Body(None, embed=True),
                              onlyUnedited: Optional[bool] = Body(None, embed=True)):
    """改定时器设置（开关/间隔/扫哪个列表/是否只列未编辑）。只改传了的项。

    开关变更当场生效（开→起定时器、关→停），其余项由循环下一轮自动读到。
    越界值由 set_settings 拒掉、原样转 400——那是页面上填错了，不是服务端故障。
    """
    try:
        return await publish_collectbox.apply_settings(
            enabled=enabled, interval_minutes=intervalMinutes, state=state,
            only_unedited=onlyUnedited)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/publish/collectbox/scan")
async def collectbox_scan_now(state: str = Body("", embed=True)):
    """立刻扫一次（页面上的「立即扫描」按钮）。返回现状 + 新清单。

    与定时器共用 collectbox 内部的扫描锁，故点这个按钮不会与定时轮次撞在一起。
    有发布作业在跑时拒掉：扫描会把那个 CDP 页面导航走，等于毁掉正在填的表单
    （15 阶段共用一个 page，见 app/publish/browser.py 的会话模型）。
    """
    if _publish_job_busy():
        raise HTTPException(409, "有发布作业正在跑，扫描会抢占编辑页；请等作业结束后再扫")
    cfg = publish_collectbox.get_settings()
    r = await publish_collectbox.scan_once(state or cfg["state"])
    if r.get("error") and not r.get("items"):
        raise HTTPException(503, f"扫描失败：{r['error']}")
    publish_collectbox.save_scan(r)
    st = publish_collectbox.status()
    return {**st, "items": r.get("items") or []}


# ---- 未认领清单接口（数据采集页「未认领」标签）--------------------------------
# 路由前缀刻意用 /publish/crawlbox 而不是给 collectbox 加参数：两个清单的行结构不同
# （那边有 rowid + 店铺 + 站点 + 编辑进度，这里有 cid + 可否认领、没有店铺站点），
# 前端也是两张各自渲染的表。合成一个接口只会让返回体变成「看 state 才知道有哪些字段」。

@app.get("/publish/crawlbox")
async def crawlbox_state():
    """未认领清单定时器现状 + 上次扫到的清单。读磁盘缓存，不触发扫描。"""
    st = publish_crawlbox.status()
    scan = publish_crawlbox.load_scan()
    return {**st, "items": scan.get("items") or [],
            "states": [{"id": k, "label": v["label"]}
                       for k, v in publish_crawlbox.CRAWL_STATES.items()],
            "intervalMin": publish_crawlbox.INTERVAL_MIN,
            "intervalMax": publish_crawlbox.INTERVAL_MAX,
            "intervalDefault": publish_crawlbox.INTERVAL_DEFAULT}


@app.post("/publish/crawlbox/settings")
async def crawlbox_settings(enabled: Optional[bool] = Body(None, embed=True),
                            intervalMinutes: Optional[int] = Body(None, embed=True),
                            state: Optional[str] = Body(None, embed=True),
                            onlyClaimable: Optional[bool] = Body(None, embed=True)):
    """改未认领清单定时器设置（开关/间隔/扫哪个标签/是否只列可认领）。只改传了的项。"""
    try:
        return await publish_crawlbox.apply_settings(
            enabled=enabled, interval_minutes=intervalMinutes, state=state,
            only_claimable=onlyClaimable)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/publish/crawlbox/scan")
async def crawlbox_scan_now(state: str = Body("", embed=True)):
    """立刻扫一次未认领清单。返回现状 + 新清单。

    与采集箱扫描共用 browser.PAGE_LOCK（都要导航同一个店小秘页签），故两个「立即扫描」
    同时点也不会互相把页面导航走——后点的那个在锁上等着。
    有发布作业在跑时拒掉：扫描会把那个 CDP 页面导航走，等于毁掉正在填的表单。
    """
    if _publish_job_busy():
        raise HTTPException(409, "有发布作业正在跑，扫描会抢占编辑页；请等作业结束后再扫")
    cfg = publish_crawlbox.get_settings()
    r = await publish_crawlbox.scan_once(state or cfg["state"])
    if r.get("error") and not r.get("items"):
        raise HTTPException(503, f"扫描失败：{r['error']}")
    publish_crawlbox.save_scan(r)
    st = publish_crawlbox.status()
    return {**st, "items": r.get("items") or []}


# ---- 数据搬家清单接口（模块导入与 busy 注入在上面的定时器段落）----------------
# 【本清单比另两张多一个写接口】/publish/banjia/claim 会真实创建草稿（不可逆）。
# 刻意不做成「扫到就自动认领」：定时器只扫清单，认领必须由用户勾选后显式发起。


@app.get("/publish/banjia")
async def banjia_state():
    """数据搬家清单定时器现状 + 上次扫到的清单。读磁盘缓存，不触发扫描。"""
    st = publish_banjia.status()
    scan = publish_banjia.load_scan()
    return {**st, "items": scan.get("items") or [],
            "states": [{"id": k, "label": v["label"]}
                       for k, v in publish_banjia.BANJIA_STATES.items()],
            "intervalMin": publish_banjia.INTERVAL_MIN,
            "intervalMax": publish_banjia.INTERVAL_MAX,
            "intervalDefault": publish_banjia.INTERVAL_DEFAULT}


@app.post("/publish/banjia/settings")
async def banjia_settings(enabled: Optional[bool] = Body(None, embed=True),
                          intervalMinutes: Optional[int] = Body(None, embed=True),
                          state: Optional[str] = Body(None, embed=True),
                          hideClaimed: Optional[bool] = Body(None, embed=True)):
    """改数据搬家定时器设置（开关/间隔/扫哪个标签/是否隐藏已搬到目标店的行）。"""
    try:
        return await publish_banjia.apply_settings(
            enabled=enabled, interval_minutes=intervalMinutes, state=state,
            hide_claimed=hideClaimed)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/publish/banjia/scan")
async def banjia_scan_now(state: str = Body("", embed=True)):
    """立刻扫一次数据搬家清单。返回现状 + 新清单。

    与另两个扫描共用 browser.PAGE_LOCK，故三个「立即扫描」同时点也不会互相把页面
    导航走。有发布作业在跑时拒掉（扫描会抢占那个 CDP 页面，毁掉正在填的表单）。
    """
    if _publish_job_busy():
        raise HTTPException(409, "有发布作业正在跑，扫描会抢占编辑页；请等作业结束后再扫")
    cfg = publish_banjia.get_settings()
    r = await publish_banjia.scan_once(state or cfg["state"])
    if r.get("error") and not r.get("items"):
        raise HTTPException(503, f"扫描失败：{r['error']}")
    publish_banjia.save_scan(r)
    st = publish_banjia.status()
    return {**st, "items": r.get("items") or []}


@app.post("/publish/banjia/claim")
async def banjia_claim(rowids: list = Body(..., embed=True),
                       store: str = Body(..., embed=True),
                       site: str = Body(..., embed=True),
                       state: str = Body("", embed=True)):
    """把勾中的数据搬家行批量认领到指定店铺站点。

    【会真实创建草稿，不可逆】故必须由用户在清单里勾选并选好店铺站点才能调到这里；
    后端不做「全选当页」这类便捷操作（rowids 为空直接 400）。

    与扫描互斥走同一把页面锁（认领全程要在页面上勾行、开弹窗）；有发布作业在跑时拒掉
    ——认领会把那个 CDP 页面导航走，等于毁掉正在填的表单。
    """
    if _publish_job_busy():
        raise HTTPException(409, "有发布作业正在跑，认领会抢占编辑页；请等作业结束后再认领")
    logs: list = []
    try:
        cfg = publish_banjia.get_settings()
        r = await publish_banjia.claim_batch(
            rowids, store, site, state=state or cfg["state"], on_log=logs.append)
        return {**r, "logs": logs}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        # 认领这一路的失败几乎都是「页面状态不对/店小秘接口异常」这类可重试的情况，
        # 报 503 让前端提示「稍后重试」，而不是 500（那会被当成本服务的 bug）
        raise HTTPException(503, str(e))


# ---- 采集箱「全属性修改」批量填仓库/发货时效/运费模板 -------------------------
# 【它补的是数据搬家认领后的空档】认领过来的草稿这三项是空的，而它们是发布前的硬性必填。
# 逐条进编辑页填（pipeline.set_stock / set_shipping）在批量场景下要十几分钟，
# 走列表页的「批量操作 → 全属性修改」一次弹窗改完整批。详见 app/publish/bulkattr.py。
#
# 【默认 dry-run】点「确定」会真实覆盖整批草稿的这三项，故接口的 dryRun 默认 True：
# 先返回「打算填什么」让用户核对，确认后再带 dryRun=false 提交。
from app.publish import bulkattr as publish_bulkattr


@app.post("/publish/bulkattr")
async def bulkattr_apply(rowids: list = Body(..., embed=True),
                         dryRun: bool = Body(True, embed=True)):
    """给采集箱里勾中的草稿批量填仓库/发货时效/运费模板。

    取值规则固定（2026-09-01 用户确认）：仓库与运费模板取下拉第一项，
    发货时效取工作日数最大的那项。规则不做成参数——批量入口的下拉是按店铺+站点分别
    渲染的，支持任意值就要让前端给出「店铺×站点 → 值」的完整矩阵，那已经不叫批量了。

    dryRun=True（默认）只探测并返回打算填什么，不提交；false 才真实修改（不可逆）。
    """
    if _publish_job_busy():
        raise HTTPException(409, "有发布作业正在跑，本操作会抢占编辑页；请等作业结束后再试")
    # 【过程日志随响应带回】这三个接口都是「一次调用跑几十秒」的长动作，期间用户只能
    # 干等。把每一步收集起来随响应返回，前端写进「本批进度」——与 /publish/batch 的
    # SSE 不同，这些动作不是长驻作业，为它们各开一条 SSE 不划算；而它们的日志量很小
    # （每条草稿几行），随响应带回最省事。
    logs: list = []
    try:
        r = await publish_bulkattr.apply_bulk_attrs(
            rowids, dry_run=dryRun, on_log=logs.append)
        return {**r, "logs": logs}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


# ---- 批量发布（数据搬家管线的最后一棒）----------------------------------------
# 走列表页的「批量操作 → 批量发布」。平台会先弹「发布检测」，检测未过的原因多是
# 「半托管仓库不能为空」——正是上一步 bulkattr 要填的三项，故这三个接口是一条链：
#     /publish/banjia/claim → /publish/bulkattr → /publish/publish  ← 这里
#
# 【confirm 是不可逆闸门】发布让商品在 Temu 真实上架、只能手动下架，故默认 False：
# 不显式传 True 就只跑到发布检测看「几条能发、几条不能发及原因」。
# 【检测未通过的处置：跳过】用户 2026-09-01 明确选定「跳过，发布检测通过的产品」，
# 能发的先发走、不因个别失败拖住整批；未通过的留在采集箱并把原因报回前端。
from app.publish import publish_batch as publish_publisher


@app.post("/publish/publish")
async def publish_drafts(rowids: list = Body(..., embed=True),
                         confirm: bool = Body(False, embed=True)):
    """批量发布采集箱里的草稿（管线最后一棒）。

    confirm=False（默认）只跑发布检测并返回 {passed, failed, issues}，不发布；
    true 才点「跳过，发布检测通过的产品」真实上架（不可逆）。
    """
    if _publish_job_busy():
        raise HTTPException(409, "有发布作业正在跑，本操作会抢占编辑页；请等作业结束后再试")
    logs: list = []
    try:
        r = await publish_publisher.publish_batch(
            rowids, confirm=confirm, on_log=logs.append)
        return {**r, "logs": logs}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        # 失败也要把已收集的日志给出去：它正是「跑到哪一步炸的」的唯一线索
        raise HTTPException(503, str(e) + (
            "｜过程：" + " / ".join(logs[-4:]) if logs else ""))


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500, content={"message": f"Server error: {str(exc)}"}
    )


def open_local_browser(config):
    webbrowser.open_new_tab(f"http://{config['host']}:{config['port']}")


def load_config():
    try:
        # 冻结后入口脚本的 __file__ 指向 _internal 里的解包路径，推不出用户实际
        # 编辑的那份配置；PROJECT_ROOT 在冻结态即 exe 所在目录，开发态即项目根。
        config_path = PROJECT_ROOT / "config" / "config.toml"

        if not config_path.exists():
            return {"host": "localhost", "port": 5172}

        with open(config_path, "rb") as f:
            config = tomllib.load(f)

        return {"host": config["server"]["host"], "port": config["server"]["port"]}
    except FileNotFoundError:
        return {"host": "localhost", "port": 5172}
    except KeyError as e:
        print(
            f"The configuration file is missing necessary fields: {str(e)}, use default configuration"
        )
        return {"host": "localhost", "port": 5172}


if __name__ == "__main__":
    import uvicorn

    config = load_config()
    open_with_config = partial(open_local_browser, config)
    threading.Timer(3, open_with_config).start()
    uvicorn.run(app, host=config["host"], port=config["port"])
