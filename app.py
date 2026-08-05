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

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
    config_path = Path(__file__).parent / "config" / "config.toml"
    example_config_path = Path(__file__).parent / "config" / "config.example.toml"

    if config_path.exists():
        return {"status": "exists"}
    elif example_config_path.exists():
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
        config_dir = Path(__file__).parent / "config"
        config_dir.mkdir(exist_ok=True)

        config_path = config_dir / "config.toml"

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
    excel: str = "", sheet: str = "", store: str = "",
):
    """UI 首屏：清单总量/已入库/待采 + 每条状态（不触发采集）。

    可选 excel/sheet/store 过滤：缺省回填上次选择。响应含 workbooks/sheets/stores 供下拉。
    """
    return JSONResponse(content=collect_service.get_worklist_status(
        excel=excel or None, sheet=sheet or None, store=store or None,
    ))


@app.post("/collect/enumerate")
async def collect_enumerate(status: str = Body("", embed=True)):
    """重新枚举所有已打开店铺标签的清单，写 worklist.json，返回条数。

    status：采集范围。空串（默认）= 跟随各店 Temu 页面当前的页签 + 所有筛选（类目/站点/
    商品名等），点页面「查询」原样采集；非空 = 先替你切到该页签再查询。本次选择记入偏好回显。
    """
    tab = status or ""
    # 记住本次采集范围，保留已存的 excel/sheet/store 不被覆盖；
    # 云端目标存在 cloud_url 键（与 excel 互斥），回传时二选一，避免被空 excel 清掉
    prefs = collect_service.load_prefs()
    collect_service.save_prefs(
        prefs.get("excel") or prefs.get("cloud_url") or "",
        prefs.get("sheet", ""), prefs.get("store", ""), tab
    )
    count = await collect_service.enumerate_worklist(status_tab=tab)
    return {"count": count, "status": collect_service.get_worklist_status()}


@app.post("/collect/batch")
async def collect_batch(
    limit: int = Body(20, embed=True),
    use_pipeline: bool = Body(True, embed=True),
    base_only: bool = Body(True, embed=True),
    excel: str = Body("", embed=True),
    sheet: str = Body("", embed=True),
    store: str = Body("", embed=True),
):
    """启动一批采集作业，返回 job_id；进度经 /collect/batch/{job_id}/events (SSE) 消费。

    base_only=True（默认）只采 Temu 基础信息、采购价/重量留空待人工填；False 走 1688 自动采价。
    excel/sheet/store 指定目标工作簿/Sheet/店铺（缺省回填上次选择）。
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
async def orders_worklist(store: str = "", workbook: str = "", sheet: str = ""):
    """订单页首屏/切换：可选工作簿与 Sheet 列表、上次选择回显、选中 Sheet 的可写性。

    query 参数缺省（不传）时回填「上次选择」，再兜底 config 的 [orders].workbook。
    注意 store/sheet 用空串表示「本次不覆盖」，与 service 的 None 语义对齐：显式传空串
    是「清空选择」，不传则沿用偏好。纯读，不触发采集或写入。
    """
    return JSONResponse(content=orders_service.get_worklist_status(
        store=store or None, workbook=workbook or None, sheet=sheet or None,
    ))


@app.get("/orders/sheet_info")
async def orders_sheet_info(workbook: str, sheet: str):
    """单独探测某 Sheet 的可写性（判重列是否齐、图片列、表头行）。

    独立成一个接口是因为登记表 102MB，切 Sheet 时只该解析这一张表的表头，不必把整个
    worklist（含工作簿枚举）重算一遍。
    """
    cfg = orders_service.load_orders_config()
    return JSONResponse(content=orders_service.inspect_sheet(
        workbook, sheet, list(cfg.get("dedupe_by") or []),
        cloud=orders_service.cloud_backend(
            cfg,
            cloud_url=workbook if orders_service.is_cloud_link(workbook) else "",
        ),
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
    """
    job_id = str(uuid.uuid4())
    job = OrdersJob(job_id, store, dry_run)
    orders_jobs[job_id] = job
    # 记住本次选择，下次开页直接回填（写失败不影响本批）
    orders_service.save_prefs(store=store, workbook=workbook, sheet=sheet)

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


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500, content={"message": f"Server error: {str(exc)}"}
    )


def open_local_browser(config):
    webbrowser.open_new_tab(f"http://{config['host']}:{config['port']}")


def load_config():
    try:
        config_path = Path(__file__).parent / "config" / "config.toml"

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
