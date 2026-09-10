import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.publish import extract, stock
from app.publish.browser import BrowserSession
from app.publish.media import description_scripts
from app.publish.sizechart import measurements, scripts
from app.publish.sources import base, temu
from app.publish.stages import saving


def run_js(code, setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node.js 执行页面脚本")
    harness = setup + "\n(async () => { console.log(JSON.stringify(await (" + code + "))); })().catch(error => {console.error(error); process.exit(1)});"
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    return json.loads(value) if isinstance(value, str) else value


@pytest.mark.parametrize("context,expected", [({}, False), ({"result": {}}, True)])
def test_1688_missing_data_diagnostic(context, expected):
    result = run_js(extract._JS_EXTRACT, "global.window=" + json.dumps({"context": context}) + "; global.location={href:'https://detail.1688.com/offer/1.html'}; global.document={title:'商品',readyState:'complete'};")
    assert result["found"] is False
    assert result["diagnostic"]["hasResult"] is expected


def test_1688_no_context_is_not_existing_result():
    result = run_js(extract._JS_EXTRACT, "global.window={}; global.location={href:'https://www.1688.com/'}; global.document={title:'首页',readyState:'complete'};")
    assert result["diagnostic"]["hasContext"] is False
    assert result["diagnostic"]["hasResult"] is False


def test_temu_id_without_title_is_not_ready():
    result = run_js(temu._JS_EXTRACT, "global.window={rawData:{store:{goodsId:'606290331961233',goods:{}}}}; global.location={href:'https://www.temu.com/g-606290331961233.html'};")
    assert result["found"] is False
    assert result["diagnostic"]["reason"] == "missing-title"


@pytest.mark.parametrize("split_table", [False, True])
def test_sizechart_ignores_measure_row_and_reads_input_size(split_table):
    setup = r"""
const hidden = {matches: () => true};
const cell = {textContent:'',querySelector:()=>({value:'均码'})};
const row = {matches:()=>false,querySelector:selector=>selector==='td'?cell:{value:''}};
const table = {querySelector:()=>({}),querySelectorAll: selector=>selector==='thead th'?
  ['尺码','宽','净重','长'].map(text=>({childNodes:[{nodeType:3,textContent:text}]})):[hidden,row]};
const wrap = {textContent:'添加尺码表',getBoundingClientRect:()=>({width:0}),querySelectorAll:()=>[table],querySelector:()=>table};
global.document = {querySelectorAll:()=>[wrap]};
global.getComputedStyle=()=>({display:'block'});
"""
    if split_table:
        setup += "const header={querySelector:()=>({}),querySelectorAll:selector=>selector==='thead th'?table.querySelectorAll(selector):[]};wrap.querySelectorAll=()=>[header,table];"
    result = run_js(scripts._JS_SIZECHART_PARAMS, setup)
    assert result == {"params": ["宽", "净重", "长"], "sizes": ["均码"]}


def test_description_waits_for_slow_modal_without_reclicking():
    setup = r"""
let ticks=0,clicks=0;
global.setTimeout=callback=>{ticks++;callback()};
const button={scrollIntoView:()=>{},click:()=>{clicks++}};
const modal={querySelector:()=>({}),getClientRects:()=>[{}]};
global.document={getElementById:()=>({querySelector:()=>button}),querySelectorAll:()=>ticks>=13?[modal]:[]};
global.location={href:'https://www.dianxiaomi.com/web/popTemu/edit?id=1'};
"""
    result = run_js(description_scripts._JS_DESC_OPEN, setup)
    assert result["opened"] is True


@pytest.mark.asyncio
async def test_measurements_retry_missing_parameter_and_preserve_source(monkeypatch):
    from app.publish import llm
    responses = AsyncMock(side_effect=[{"均码": {"宽": 55}}, {"均码": {"宽": 55, "净重": 500, "长": 55}}])
    monkeypatch.setattr(llm, "ask_json", responses)
    monkeypatch.setattr(measurements.packaging, "classify_category", AsyncMock(return_value="pet_supply"))
    result = await measurements._estimate_measurements("猫窝", {}, ["均码"], ["宽", "净重", "长"], {}, src_rows={"小号": {"直径": 55}, "大号": {"直径": 60}})
    assert result["onesize"]["净重"] == 500
    assert responses.await_count == 2
    prompt = responses.call_args_list[0].args[0]
    assert "小号" in prompt and "直径55" in prompt
    assert '"均码": {"宽": null, "净重": null, "长": null}' in prompt


def test_measurements_reject_nonfinite_and_missing_values():
    problems = measurements._check_measurements({"S": {"宽": float("nan"), "长": -1, "净重": True}}, ["S"], ["宽", "长", "净重"])
    assert problems and all(parameter in problems[0] for parameter in ["宽", "长", "净重"])


@pytest.mark.asyncio
async def test_warehouse_requires_fresh_stock_columns(monkeypatch):
    monkeypatch.setattr(stock.asyncio, "sleep", AsyncMock())
    selected = {"selected": ["嘉运美东仓"], "stockHeaders": []}
    ready = {"selected": ["嘉运美东仓"], "stockHeaders": ["嘉运美东仓库存(批量)"]}
    session = SimpleNamespace(eval_json=AsyncMock(side_effect=[selected, selected, selected, ready]))
    result = await stock.ensure_warehouse(session, "嘉运美东仓库")
    assert result["status"] == "ok"
    assert session.eval_json.await_count == 4
    assert not stock._warehouse_ready({"selected": ["嘉运美东仓二号"], "stockHeaders": ["嘉运美东仓二号库存"]}, "嘉运美东仓")


@pytest.mark.asyncio
async def test_save_blocks_missing_preview_before_click(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(saving, "save", save)
    monkeypatch.setattr(saving, "sku_preview_state", AsyncMock(return_value={"supported": True, "rows": [{"i": 3, "color": "黄草莓", "empty": True}]}))
    session = SimpleNamespace(eval_json=AsyncMock(return_value={"selected": ["嘉运美东仓"], "stockHeaders": ["嘉运美东仓库存"]}))
    result = await saving._st_save({"rowid": "1", "warehouse": "嘉运美东仓"}, session, AsyncMock())
    assert result["status"] == "fail" and "第 4 行" in result["note"]
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_blocks_missing_warehouse_before_click(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(saving, "save", save)
    session = SimpleNamespace(eval_json=AsyncMock(return_value={"selected": ["嘉运美东仓"], "stockHeaders": []}))
    result = await saving._st_save({"rowid": "1", "warehouse": "嘉运美东仓"}, session, AsyncMock())
    assert result["status"] == "fail" and "站点仓库" in result["note"]
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_human_recovery_checks_new_tabs(monkeypatch):
    monkeypatch.setattr(base, "WAIT_HUMAN_INTERVAL", 0)
    session = SimpleNamespace(page=SimpleNamespace(bring_to_front=AsyncMock()), eval_json=AsyncMock(side_effect=[{"blocked": True, "hasData": True}, {"blocked": False, "hasData": True}]))
    recover = AsyncMock()
    assert await base.wait_human(session, "probe", "hasData", "等待登录", timeout=1, before_probe=recover)
    assert recover.await_count == 2


@pytest.mark.asyncio
async def test_adopt_ignores_login_redirect_with_product_id():
    login = SimpleNamespace(url="https://www.temu.com/login.html?from=-g-606290331961233", is_closed=lambda: False)
    product = SimpleNamespace(url="https://www.temu.com/g-606290331961233.html", is_closed=lambda: False)
    session = BrowserSession()
    session._browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[login, product])])
    session._page = product
    session._cdp = object()
    result = await session.adopt_open_page("-g-606290331961233")
    assert result["ok"] and session.page is product


def test_fill_sizechart_writes_real_rows_and_ignores_measure_row():
    setup = r"""
class Input {constructor(){this.current=''} get value(){return this.current} set value(value){this.current=value} dispatchEvent(){}}
global.window={HTMLInputElement:Input};
global.Event=class {};
global.setTimeout=callback=>callback();
const sizeInput=new Input();sizeInput.value='均码';
const sizeCell={querySelector:()=>sizeInput,textContent:''};
const measurement=new Input();
const valueCell={querySelectorAll:()=>[measurement]};
const row={matches:()=>false,querySelector:()=>measurement,querySelectorAll:()=>[sizeCell,valueCell]};
const hidden={matches:()=>true};
const table={querySelector:()=>({}),querySelectorAll:selector=>selector==='thead th'?
  ['尺码','宽'].map(text=>({childNodes:[{nodeType:3,textContent:text}]})):[hidden,row]};
const name=new Input();
const wrap={textContent:'添加尺码表',getBoundingClientRect:()=>({width:0}),querySelectorAll:()=>[table],querySelector:selector=>selector==='table'?table:name};
global.document={querySelectorAll:()=>[wrap]};
global.getComputedStyle=()=>({display:'block'});
"""
    code = scripts._JS_FILL_SIZECHART.replace("__NAME__", '"猫窝尺码表"').replace("__DATA__", '{"均码":{"宽":55}}').replace("__PARAMS__", '["宽"]')
    result = run_js("(async()=>{const result=JSON.parse(await (" + code + "));return {result,value:measurement.value}})()", setup)
    assert result == {"result": {"ok": True, "empty": []}, "value": "55"}


@pytest.mark.asyncio
async def test_preview_all_empty_still_fills_without_hover_trigger(monkeypatch, tmp_path):
    from app.publish.stages import preview
    info_path = tmp_path / "product-info.json"
    info_path.write_text(json.dumps({"colorImages": {"白": {"mainFile": "white.jpg"}}}), encoding="utf-8")
    (tmp_path / "white.jpg").write_bytes(b"image")
    state = {"supported": False, "rows": [{"i": 0, "color": "白", "empty": True, "hasFillSlot": True}], "previewIdx": 0, "colorIdx": 1}
    monkeypatch.setattr(preview, "sku_preview_state", AsyncMock(return_value=state))
    monkeypatch.setattr(preview.images, "square_image", lambda *args, **kwargs: {"output": str(tmp_path / "white.jpg"), "outSize": "1785x1785"})
    replace = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(preview, "sku_preview_replace_row", replace)
    result = await preview._st_sku_preview({"workdir": str(tmp_path), "info_path": str(info_path)}, None, AsyncMock())
    assert result["status"] == "ok"
    assert replace.call_args.kwargs["fill_empty"] is True


def test_warehouse_stale_selection_is_reselected():
    from app.publish import stock_scripts
    setup = r"""
let selected=true,clicks=0;
global.setTimeout=callback=>callback();
const option={textContent:'嘉运美东仓',classList:{contains:()=>selected},click:()=>{selected=!selected;clicks++}};
const dropdown={getAttribute:()=>'',querySelectorAll:()=>[option]};
const input={getAttribute:()=> 'warehouse-list'};
const select={scrollIntoView:()=>{},classList:{contains:()=>false},querySelector:()=>input,
  querySelectorAll:()=>selected?[{title:'嘉运美东仓'}]:[]};
const label={childElementCount:0,textContent:'选择仓库：',closest:()=>({}),parentElement:{querySelector:()=>select}};
global.document={querySelectorAll:()=>[label],getElementById:()=>({closest:()=>dropdown}),body:{click:()=>{}}};
"""
    code = stock_scripts._JS_PICK_WAREHOUSE.replace("__WH__", '"嘉运美东仓库"')
    result = run_js("(async()=>{const result=JSON.parse(await (" + code + "));return {result,clicks}})()", setup)
    assert result["clicks"] == 2
    assert result["result"]["selected"] == ["嘉运美东仓"]
