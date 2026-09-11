"""把发布页面函数绑定到当前商品会话，供 Manus ReAct 调用。"""

import inspect
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import ConfigDict, PrivateAttr, create_model

from app.publish.stages.results import _DONE
from app.tool.base import BaseTool, ToolResult
from app.tool.tool_collection import ToolCollection


CONTEXT_ARGUMENTS = {
    "rowid", "info_path", "store", "site", "warehouse", "cat_path", "cat_id", "use_cache",
}


@dataclass
class RecoveryContext:
    session: Any
    ctx: dict
    stage: str
    handlers: dict
    emit: Callable
    verified: dict | None = None
    history: list = field(default_factory=list)

    async def run_stage(self, stage: str) -> dict:
        """执行原阶段及其校验，不递归启动兜底。"""
        self.verified = None
        if stage == "publish":
            if not self.ctx.get("do_publish") or self.stage != "publish":
                raise ValueError("本次任务未授权在此阶段发布")
            saved = self.ctx.get("state", {}).get("stages", {}).get("save", {})
            if saved.get("status") != "ok":
                raise ValueError("保存尚未确认成功，不能发布")
            from app.publish.persistence import _publish_landed

            landed = await _publish_landed(self.session, self.ctx["rowid"])
            if (landed.get("online") or {}).get("found") and not (
                landed.get("fail") or {}
            ).get("found"):
                self.verified = {"status": "ok", "note": "在线产品列表确认已发布"}
                return self.verified
            if not any((landed.get(key) or {}).get("found") for key in ("draft", "fail")):
                return {"status": "fail", "note": "发布状态不明，不能重复提交；请先确认列表状态"}
        result = await self.handlers[stage](self.ctx, self.session, self.emit)
        if stage == "save" and result.get("status") == "ok":
            self.ctx.setdefault("state", {}).setdefault("stages", {})[stage] = dict(result)
        if stage == self.stage and result.get("status") in _DONE:
            self.verified = dict(result)
        return result


class PublishFunctionTool(BaseTool):
    _callback: Any = PrivateAttr()
    _arguments: Any = PrivateAttr()
    _recovery: Any = PrivateAttr()

    def __init__(self, name, description, callback, arguments, recovery):
        super().__init__(
            name=name,
            description=description,
            parameters=arguments.model_json_schema(),
        )
        self._callback = callback
        self._arguments = arguments
        self._recovery = recovery

    async def execute(self, **kwargs) -> ToolResult:
        self._recovery.verified = None
        try:
            values = self._arguments.model_validate(kwargs).model_dump(exclude_unset=True)
            result = await self._callback(**values)
            if isinstance(result, ToolResult):
                return result
            observation = json.dumps(result, ensure_ascii=False, default=str)
            self._recovery.history.append({"tool": self.name, "result": observation[:2000]})
            await self._recovery.emit({
                "type": "log", "level": "info", "stage": self._recovery.stage,
                "message": f"Manus 工具 {self.name}：{observation[:1000]}",
            })
            return ToolResult(output=observation)
        except Exception as error:
            self._recovery.history.append({"tool": self.name, "error": str(error)[:2000]})
            return ToolResult(error=str(error))


def _argument_model(name, function, bound=()):
    fields = {}
    for parameter in inspect.signature(function).parameters.values():
        if parameter.name in bound:
            continue
        annotation = parameter.annotation
        if annotation is inspect.Parameter.empty:
            annotation = Any
        default = parameter.default
        if default is inspect.Parameter.empty:
            default = ...
        fields[parameter.name] = (annotation, default)
    return create_model(name, __config__=ConfigDict(extra="forbid"), **fields)


def page_functions():
    """注册兼容入口导出的每个公开 CDP 页面函数及细粒度交互函数。"""
    from app.publish import pipeline
    from app.publish.attributes import dropdowns
    from app.publish.category import _cat_columns, _click_cat_in_column, _confirm_cat
    from app.publish.media.skc import _skc_row_state
    from app.publish.stock import ensure_warehouse
    from app.publish.upload import upload_many

    functions = {
        name: function for name, function in vars(pipeline).items()
        if not name.startswith("_") and inspect.iscoroutinefunction(function)
        and "session" in inspect.signature(function).parameters
    }
    functions.update({
        "ensure_warehouse": ensure_warehouse,
        "upload_many": upload_many,
        "category_columns": _cat_columns,
        "category_click": _click_cat_in_column,
        "category_confirm": _confirm_cat,
        "skc_row_state": _skc_row_state,
        "attribute_open": dropdowns._open_attr_dropdown,
        "attribute_options": dropdowns._read_active_options,
        "attribute_click": dropdowns._click_dropdown_option,
        "press_escape": dropdowns._press_escape,
    })
    return functions


def build_publish_tools(recovery: RecoveryContext) -> ToolCollection:
    tools = ToolCollection()
    empty_arguments = create_model("NoArguments", __config__=ConfigDict(extra="forbid"))

    for name, function in page_functions().items():
        if name in {"save", "publish_now"}:
            continue
        parameters = inspect.signature(function).parameters
        bound = {"session"} | (CONTEXT_ARGUMENTS & parameters.keys())

        async def call_function(_function=function, _bound=bound, **values):
            injected = {key: recovery.ctx[key] for key in _bound
                        if key != "session" and recovery.ctx.get(key) is not None}
            return await _function(session=recovery.session, **injected, **values)

        description = (inspect.getdoc(function) or f"店小秘页面操作 {name}")[:800]
        description += "\n复用当前 CDP 会话，商品 ID、数据路径、店铺、站点、仓库由任务注入。"
        tools.add_tool(PublishFunctionTool(
            f"dxm_{name}", description, call_function,
            _argument_model(name, function, bound), recovery,
        ))

    for stage in recovery.handlers:
        if stage == "publish" and (stage != recovery.stage or not recovery.ctx.get("do_publish")):
            continue

        async def call_stage(_stage=stage):
            return await recovery.run_stage(_stage)

        tools.add_tool(PublishFunctionTool(
            f"dxm_stage_{stage}",
            f"按当前商品原始参数执行 {stage} 阶段及校验。"
            "修复完成必须调用失败阶段的此工具，只有校验通过才能继续管线。"
            "claim/auto_cat/extract 可能导航并丢失未保存编辑，须谨慎调用。",
            call_stage, empty_arguments, recovery,
        ))
        if stage == recovery.stage:
            break

    async def observe() -> dict:
        from app.publish.browser import recent_toasts

        snapshot = await recovery.session.eval_json("""(() => ({
            url: location.href, title: document.title,
            text: (document.body.innerText || '').slice(0, 14000),
            dialogs: [...document.querySelectorAll('[role=dialog],.ant-modal')]
                .filter(element => element.getClientRects().length)
                .map(element => element.innerText.slice(0, 3000))
        }))()""")
        return {"page": snapshot, "toasts": recent_toasts(),
                "task": {key: recovery.ctx.get(key) for key in (
                    "source_platform", "workflow_id", "rowid", "info_path", "workdir",
                    "title", "store", "site", "warehouse", "price", "keep_video", "do_publish",
                )}}

    async def read_info(keys: str = "") -> dict:
        """读取本商品的源数据 product-info.json（采集阶段抓到的原始商品信息）。

        补必填属性时用它取源商品的颜色、成分、材质等——这些在编辑页上看不到。
        不带 keys 先返回有哪些字段、各多大；再用 keys 逗号分隔取需要的字段。
        """
        path = recovery.ctx.get("info_path") or ""
        if not path or not os.path.isfile(path):
            return {"error": "本任务没有 product-info.json，只能依据编辑页可见信息判断"}
        try:
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
        except Exception as error:
            return {"error": f"读取失败：{error}"}
        wanted = [key.strip() for key in keys.split(",") if key.strip()]
        if not wanted:
            return {"path": path,
                    "keys": {key: len(json.dumps(value, ensure_ascii=False, default=str))
                             for key, value in info.items()}}
        # 单字段截断：详情文字/颜色这类字段可以很长，整段灌进上下文会挤掉后续观察。
        # 截断处注明以便模型知道「这里不完整」，不静默截。
        picked = {}
        for key in wanted:
            text = json.dumps(info.get(key), ensure_ascii=False, default=str)
            picked[key] = text if len(text) <= 4000 else text[:4000] + f"…（截断，原长 {len(text)}）"
        return picked

    async def evaluate(code: str) -> Any:
        """在当前页面执行 JavaScript，用于读取 DOM 或修复既有函数无法处理的页面状态。

        不得更换商品、修改任务配置、规避阶段校验或直接保存/发布；保存发布须调用阶段工具。
        """
        return await recovery.session.eval_json(code)

    async def click(selector: str) -> dict:
        """在当前页面用 CDP 点击 CSS 选择器；保存/发布须调用阶段工具。"""
        return await recovery.session.mouse_click(selector)

    async def cdp_input(
        method: Literal["Input.dispatchMouseEvent", "Input.dispatchKeyEvent", "Input.insertText"],
        params: dict,
    ) -> dict:
        """在当前页面发送 CDP 鼠标或键盘输入。保存/发布须调用阶段工具。"""
        return await recovery.session.cdp(method, params)

    async def screenshot() -> ToolResult:
        """读取当前页面截图，供观察页面与弹窗，不切换页面。"""
        captured = await recovery.session.cdp("Page.captureScreenshot", {"format": "png"})
        # 【base64 在里层】browser.cdp() 返回的是 {"ok", "data": <CDP 返回体>} 这层包装，
        # 图像数据在返回体的 data 字段里。原先直接把外层 captured["data"] 当 base64 传，
        # pydantic 报「base64_image Input should be a valid string, input_value=
        # {'data': 'iVBOR…'}」，这个工具每次调用都失败、白丢一步
        # （2026-09-11 真机兜底实测）。取不到就如实报错，不塞空串冒充成功。
        image = (captured.get("data") or {}).get("data")
        if not image:
            return ToolResult(error=f"截图失败：{captured.get('err') or 'CDP 未返回图像数据'}")
        return ToolResult(output="当前店小秘工作页截图", base64_image=image)

    for name, function in (
        ("observe", observe), ("read_info", read_info), ("evaluate", evaluate),
        ("click", click), ("cdp_input", cdp_input), ("screenshot", screenshot),
    ):
        tools.add_tool(PublishFunctionTool(
            f"dxm_{name}", inspect.getdoc(function) or "读取当前页面、弹窗、提示与任务上下文",
            function, _argument_model(name, function), recovery,
        ))
    return tools
