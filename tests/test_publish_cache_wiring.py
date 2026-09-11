# -*- coding: utf-8 -*-
"""类目缓存与属性选项来源在 pipeline / service 里的接线单测（假 session，不碰 CDP、不碰 LLM）。

这里测的是「数据来源怎么影响流程」，而不是缓存文件本身的读写（那部分在
test_publish_cache.py）。重点钉住几条容易在后续改动中被无声打穿的不变式：
  - 非必填未填 / 隐藏行不给选项，且这两条 guard 排在填选项之前；
  - 行集只由活 DOM 决定，服务端清单是超集也不许凭空造行；
  - required 取活页面的值，不取任何远端/缓存版本；
  - 服务端没有的属性行如实标出来，不静默也不回退去开下拉；
  - 写入失败只做「原值再试一次」，不换值、不碰数值行。
"""

from publish_patching import patch_publish
import pytest

from app.publish import cache, category, category_api, pipeline, service


PATH = ["服装、鞋靴和珠宝饰品", "女童时尚", "女童服装",
        "女童毛衣、针织衫", "女童针织套头衫"]


# ---- 假 session：只实现被测路径用到的那几个方法 ------------------------------

class _FakeSession:
    """按预设应答 eval_json / wait_for，并记录 _read_active_options 是否被调用。"""

    def __init__(self, rows=None, click_results=None):
        self.rows = rows or {"found": True, "attrs": []}
        self.click_results = list(click_results or [])
        self.clicks = []
        self.confirmed = False

    async def eval_json(self, js, *a, **kw):
        if "productBasicInfo" in js and "ant-form-item" in js:
            return self.rows
        # 【点击与读列必须靠 item.click() 区分，不能靠 categories-box/-item】
        # 两段 JS 的类名高度重叠：_JS_CAT_COLUMNS（读列）与 _click_cat_in_column
        # （点项）都含 'categories-box'，点项那段还含 'categories-item'。
        # 靠类名分流会串台——优化后的 _click_cat_path 会轮询读列，读列被记成点击时
        # clicks 涨到百万级；反之把点击当读列则 clicks 恒为 0。两种都会让
        # 「第 3 级失败后就停」这条不变式看起来被打穿，而实际是替身的问题。
        # item.click() 只出现在点项那段，是唯一可靠的判据。
        if "item.click()" in js:
            self.clicks.append(js)
            return (self.click_results.pop(0) if self.click_results
                    else {"clicked": True})
        if "categories-box" in js:
            # 读列：假定点完第 N 级后列数变成 N+2（见 pipeline 类目区块开头的联动
            # 实测），按已点次数给出「新列已挂上」的列数，让条件等待立刻收敛。
            return {"ready": True, "n": len(self.clicks) + 2}
        if "选择类目" in js and "ant-modal-footer" in js:
            self.confirmed = True
            return {"confirmed": True}
        return {}

    async def wait_for(self, js, pred, **kw):
        return self.rows

    async def kill_stuck_modals(self):
        return {}


def _async(v):
    async def _f():
        return v
    return _f()


def _row(label, required=True, current="(请选择)", visible=True):
    return {"label": label, "required": required, "current": current,
            "numValues": [], "visible": visible}


@pytest.fixture
def _no_sleep(monkeypatch):
    """把 pipeline 里的等待全部掐掉，单测不该为实测出来的等待时间付时间。"""
    async def _s(*a, **kw):
        return None
    monkeypatch.setattr(pipeline.asyncio, "sleep", _s)


# ---- dump_attrs 的选项来源（服务端）-----------------------------------------
#
# 2026-09-11 起选项不再从缓存/下拉里来，而是 attributes/server_options 一次问服务端
# 拿全（原因见该模块说明：DOM 那条路会读到「真前缀、假完整」的截断清单还自称完整）。
# 这里钉住几条仍然成立的不变式——它们都是 _validate_attr_changes 那些闸的前提。

def _stub_server_opts(monkeypatch, mapping):
    """把选项来源换成服务端桩：mapping 是 {属性名: [值…]}。

    cat_id 必须收下：它是阶段④ 查选项用的叶子类目 id（类目是运行中改的、没保存，
    服务端只认草稿已保存的那版，见 attributes/server_options），调用方按位置传。
    """
    async def _fetch(session, rowid="", cat_id=""):
        return {k: list(v) for k, v in mapping.items()}
    patch_publish(monkeypatch, "pipeline", "fetch_attr_options", _fetch)


def _stub_dump(monkeypatch, rows, mapping):
    s = _FakeSession(rows={"found": True, "attrs": rows})
    _stub_server_opts(monkeypatch, mapping)
    patch_publish(monkeypatch, "pipeline", "_expand_attr_section",
                  lambda *a, **kw: _async({}))
    return s


@pytest.mark.asyncio
async def test_非必填未填的行不给选项(_no_sleep, monkeypatch):
    """optional-skipped 必须排在填选项之前：_validate_attr_changes 靠「非必填 +
    未填 + options 空」判定「按策略留空」，给它们填上选项会把这条策略打穿。"""
    s = _stub_dump(monkeypatch,
                   [_row("品牌名", required=False)],
                   {"品牌名": ["A", "B"]})
    d = await pipeline.dump_attrs(s)
    a = d["attrs"][0]
    assert a["options"] == [] and a["optionsEmptyReason"] == "optional-skipped"
    assert "optionsFrom" not in a


@pytest.mark.asyncio
async def test_隐藏行不给选项(_no_sleep, monkeypatch):
    s = _stub_dump(monkeypatch,
                   [_row("里衬成分", visible=False)],
                   {"里衬成分": ["棉"]})
    d = await pipeline.dump_attrs(s)
    assert d["attrs"][0]["optionsEmptyReason"] == "row-hidden"
    assert d["attrs"][0]["options"] == []


@pytest.mark.asyncio
async def test_服务端有而表单没有的属性不造行(_no_sleep, monkeypatch):
    """行集只由活 DOM 决定：服务端清单是表单的超集（水枪 10 vs 表单 6 行），
    多出来的属性不能凭空出现在输出里。"""
    s = _stub_dump(monkeypatch,
                   [_row("织造方式")],
                   {"织造方式": ["梭织"], "早已下架的属性": ["X"]})
    d = await pipeline.dump_attrs(s)
    assert [a["label"] for a in d["attrs"]] == ["织造方式"]


@pytest.mark.asyncio
async def test_required用活页面的值(_no_sleep, monkeypatch):
    """required 是 _validate_attr_changes 第 1 道闸的依据，必须取活页面的值
    （平台会调整必填与否，实测同类目从 18 必填变成 16）。"""
    s = _stub_dump(monkeypatch,
                   [_row("腰带", required=True)],
                   {"腰带": ["有", "无"]})
    d = await pipeline.dump_attrs(s)
    assert d["attrs"][0]["required"] is True
    assert d["attrs"][0]["optionsFrom"] == "server"


@pytest.mark.asyncio
async def test_服务端没有的属性标出来(_no_sleep, monkeypatch):
    """服务端清单里没有这一行时如实标 server-missing 并进 optionsMissed，
    供 note 报给人看——不静默、也不回退去开下拉。"""
    s = _stub_dump(monkeypatch,
                   [_row("织造方式"), _row("季节")],
                   {"季节": ["春/秋", "夏"]})
    d = await pipeline.dump_attrs(s)
    by = {a["label"]: a for a in d["attrs"]}
    assert by["织造方式"]["optionsEmptyReason"] == "server-missing"
    assert by["织造方式"]["options"] == []
    assert by["季节"]["options"] == ["春/秋", "夏"]
    assert d["optionsMissed"] == ["织造方式"]
    assert d["serverAttrs"] == 1


@pytest.mark.asyncio
async def test_skip_options时不取选项(_no_sleep, monkeypatch):
    """那条路径语义是「只看现状」，带着 options 返回会把它的契约打乱。"""
    s = _FakeSession(rows={"found": True, "attrs": [_row("织造方式")]})
    called = []

    async def _fetch(session, rowid="", cat_id=""):
        called.append(1)
        return {"织造方式": ["梭织"]}

    patch_publish(monkeypatch, "pipeline", "fetch_attr_options", _fetch)
    patch_publish(monkeypatch, "pipeline", "_expand_attr_section",
                  lambda *a, **kw: _async({}))

    d = await pipeline.dump_attrs(s, skip_options=True)
    assert d["optionsRead"] is False
    assert "options" not in d["attrs"][0]
    assert called == [], "skip_options 连服务端都不该问"


# ---- 类目缓存快路径 ----------------------------------------------------------

@pytest.mark.asyncio
async def test_清单为空时不调LLM(_no_sleep, monkeypatch):
    """一条已知路径都没有时连提示词都不该拼——省一次 LLM 调用。"""
    called = []
    patch_publish(monkeypatch, "pipeline", "_pick_cached_category",
                        lambda *a, **kw: called.append(1))
    assert await pipeline._try_cached_category(_FakeSession(), "标题") is None
    assert called == []


@pytest.mark.asyncio
async def test_答不匹配则落回遍历(_no_sleep, monkeypatch):
    cache.remember_category(PATH, "旧标题")

    async def _pick(title, known, clues=""):
        return None, "都不匹配"
    patch_publish(monkeypatch, "pipeline", "_pick_cached_category", _pick)
    assert await pipeline._try_cached_category(_FakeSession(), "标题") is None


@pytest.mark.asyncio
async def test_LLM异常时落回遍历不抛(_no_sleep, monkeypatch):
    """缓存是加速手段，它自己的 LLM 调用失败不该拖垮整个阶段。"""
    cache.remember_category(PATH, "旧标题")

    async def _boom(title, known):
        raise RuntimeError("模型抽风")
    patch_publish(monkeypatch, "pipeline", "_pick_cached_category", _boom)
    assert await pipeline._try_cached_category(_FakeSession(), "标题") is None


@pytest.mark.asyncio
async def test_中途点不中则落回且不再点后续级(_no_sleep, monkeypatch):
    """类目树变了，继续点只会在错位的列里乱点，把弹窗状态搅得更脏。"""
    cache.remember_category(PATH, "旧标题")

    async def _pick(title, known, clues=""):
        return PATH, "命中"
    patch_publish(monkeypatch, "pipeline", "_pick_cached_category", _pick)
    s = _FakeSession(click_results=[
        {"clicked": True}, {"clicked": True},
        {"clicked": False, "reason": "item-not-found", "options": ["别的"]}])

    assert await pipeline._try_cached_category(s, "标题") is None
    assert len(s.clicks) == 3          # 第 3 级失败后就停，不点第 4、5 级
    assert s.confirmed is False        # 更不该去点确认


@pytest.mark.asyncio
async def test_回读见不到叶子则落回(_no_sleep, monkeypatch):
    cache.remember_category(PATH, "旧标题")

    async def _pick(title, known, clues=""):
        return PATH, "命中"
    patch_publish(monkeypatch, "pipeline", "_pick_cached_category", _pick)
    patch_publish(monkeypatch, "pipeline", "read_current_category",
                        lambda *a, **kw: _async("产品分类：完全不相干的类目"))
    assert await pipeline._try_cached_category(_FakeSession(), "标题") is None


@pytest.mark.asyncio
async def test_命中返回与遍历同构(_no_sleep, monkeypatch):
    """_st_auto_cat 与 publish_inspect 都消费这个返回，形状必须一致。"""
    cache.remember_category(PATH, "旧标题")

    async def _pick(title, known, clues=""):
        return PATH, "标题里有针织套头"
    patch_publish(monkeypatch, "pipeline", "_pick_cached_category", _pick)
    patch_publish(monkeypatch, "pipeline", "read_current_category",
        lambda *a, **kw: _async("产品分类 女童针织套头衫 选择分类"))

    r = await pipeline._try_cached_category(_FakeSession(), "标题")
    assert r["status"] == "ok" and r["source"] == "cache"
    assert r["path"] == " > ".join(PATH) and r["pathList"] == PATH
    assert r["leaf"] == "女童针织套头衫" and r["levels"] == 5
    assert r["trace"] and r["catSnippet"]


# ---- 从已知路径里选一条（提示词解析）----------------------------------------

def _known(*leaves):
    return [{"path": PATH[:-1] + [lf], "leaf": lf, "titles": []} for lf in leaves]


@pytest.mark.asyncio
@pytest.mark.parametrize("idx", [2, -1, 99, "1", None])
async def test_哨兵与非法index一律当不匹配(monkeypatch, idx):
    """「都不匹配」是清单里的最后一个选项（index == len），模型也可能给 -1 或越界。
    与 _pick_category「越界即抛」形成对照：这里答不出来是合法答案。"""
    async def _ask(prompt, what="判断", **kw):
        return {"index": idx, "reason": "r"}
    monkeypatch.setattr("app.publish.llm.ask_json", _ask)
    path, _reason = await pipeline._pick_cached_category("标题", _known("A", "B"))
    assert path is None


@pytest.mark.asyncio
async def test_合法index返回对应路径(monkeypatch):
    async def _ask(prompt, what="判断", **kw):
        return {"index": 1, "reason": "第二条"}
    monkeypatch.setattr("app.publish.llm.ask_json", _ask)
    path, reason = await pipeline._pick_cached_category("标题", _known("A", "B"))
    assert path[-1] == "B" and reason == "第二条"


def test_提示词末尾带不匹配哨兵():
    txt = pipeline._format_cached_paths(_known("A", "B"))
    assert "2. 【以上都不匹配" in txt      # 序号紧接在最后一条之后


def test_提示词按第一级分组且序号全局连续():
    known = [{"path": ["女装", "上装", "衬衫"], "leaf": "衬衫", "titles": ["t1"]},
             {"path": ["女装", "下装", "长裤"], "leaf": "长裤", "titles": []},
             {"path": ["男装", "上装", "POLO"], "leaf": "POLO", "titles": []}]
    txt = pipeline._format_cached_paths(known)
    assert "【女装】" in txt and "【男装】" in txt
    for i in range(3):
        assert f"{i}. " in txt
    assert "曾用于: t1" in txt


# ---- 单行写入失败后的原值重试 -----------------------------------------------
#
# 原先这三条测的是「重读下拉选项 → 回灌缓存 → 值没了就问 LLM 重选」。选项改服务端
# 之后那条路整个不成立了（选项当次现取、不存在过期），只剩「原值再试一次」。

@pytest.mark.asyncio
async def test_写入失败原值再试一次(_no_sleep, monkeypatch):
    """不白花 LLM：set_attr 内部已自愈过一次，这次隔了若干项写入与开合下拉，
    页面状态已刷新，能救回偶发的点击落空。"""
    sets = []

    async def _set(session, label, value, num=None, row=1):
        sets.append((label, value))
        return {"status": "ok", "readback": {"current": value}}
    patch_publish(monkeypatch, "pipeline", "set_attr", _set)

    fix = await pipeline._retry_row(_FakeSession(), {"label": "织造方式", "value": "针织"})
    assert fix["status"] == "ok"
    assert sets == [("织造方式", "针织")], "必须拿原值重试，不能换值"


@pytest.mark.asyncio
async def test_重试失败如实报错(_no_sleep, monkeypatch):
    async def _set(*a, **kw):
        return {"status": "error", "reason": "option-not-rendered"}
    patch_publish(monkeypatch, "pipeline", "set_attr", _set)

    fix = await pipeline._retry_row(_FakeSession(), {"label": "织造方式", "value": "针织"})
    assert fix["status"] == "error"


# ---- service 层接线 ---------------------------------------------------------

@pytest.mark.asyncio
async def test_use_cache透传进ctx(monkeypatch, tmp_path):
    seen = {}

    async def _fake_auto_cat(session, rowid, title, **kw):
        seen.update(kw)
        return {"status": "ok", "path": " > ".join(PATH), "pathList": PATH,
                "source": "cache"}
    patch_publish(monkeypatch, "service", "auto_cat", _fake_auto_cat)

    ctx = {"rowid": "1", "title": "标题", "use_cache": False, "site": "全球"}
    r = await service._st_auto_cat(ctx, None, None)
    assert seen["use_cache"] is False and seen["site"] == "全球"
    assert ctx["cat_path"] == PATH            # 供阶段④当缓存键
    assert r["note"].startswith("[缓存]")     # 走的哪条路径要记进 note


@pytest.mark.asyncio
async def test_遍历路径的note标遍历(monkeypatch):
    async def _fake_auto_cat(session, rowid, title, **kw):
        return {"status": "ok", "path": "A > B", "pathList": ["A", "B"],
                "source": "walk"}
    patch_publish(monkeypatch, "service", "auto_cat", _fake_auto_cat)
    ctx = {"rowid": "1", "title": "标题"}
    r = await service._st_auto_cat(ctx, None, None)
    assert r["note"].startswith("[遍历]")


@pytest.mark.asyncio
async def test_成分写入失败发manual_check(monkeypatch):
    async def _fake_check(session, info_path, **kw):
        return {"status": "ok", "applied": [{"result": "error"}],
                "rejected": [], "compFailed": ["上装成分"],
                "serverAttrs": 7, "optionsMissed": ["腰带"]}
    patch_publish(monkeypatch, "service", "check_attrs", _fake_check)
    events = []

    async def _emit(ev):
        events.append(ev)
    r = await service._st_attrs({"info_path": "x.json"}, None, _emit)
    assert r["status"] == "ok"
    assert any(e["type"] == "manual_check" and "上装成分" in e["message"]
               for e in events)
    # 服务端没给的属性行必须在 note 里报出来，否则现场只剩一句「改 0/0 项」，
    # 看不出是模型没给值还是这行压根没选项可给
    assert "1 行无选项（腰带）" in r["note"]


@pytest.mark.asyncio
async def test_cat_path随状态文件往返(monkeypatch, tmp_path):
    """续跑 from attrs 时阶段③被跳过，cat_path 必须能从状态文件回填，
    否则属性缓存取不到键、退化成全量读。"""
    patch_publish(monkeypatch, "service", "STATE_DIR", str(tmp_path / "s"))
    st = {"key": "k1", "stages": {}, "status": "running", "cat_path": PATH}
    service.save_state(st)
    assert service.load_state("k1")["cat_path"] == PATH


def test_旧状态文件没有cat_path也能读(monkeypatch, tmp_path):
    """向后兼容：旧文件没这个键 → None → 未命中 → 全量读，与今天行为一致。"""
    patch_publish(monkeypatch, "service", "STATE_DIR", str(tmp_path / "s"))
    service.save_state({"key": "k2", "stages": {}, "status": "ok"})
    assert service.load_state("k2").get("cat_path") is None


# ---- 叶子类目 id 的接线（阶段③ → 阶段④）--------------------------------------
#
# 阶段④ 查属性选项要带【页面上当前生效】的叶子类目 id。类目是阶段③ 运行中改的、
# 还没保存，服务端只认草稿里已保存的那版——2026-09-11 实测按旧类目查出的是上一版
# 类目的属性清单（电子类的那 20 个），页面上真正要填的动态属性一个都不在里面，
# 阶段④ 于是判「面料类型」这类必填填不上、交兜底也没救回来。这几个用例钉住这条 id
# 从阶段③ 一路传到接口的那几段接线，别在后续重构里被无声掐断。

@pytest.mark.asyncio
async def test_dump_attrs把类目id转给服务端(_no_sleep, monkeypatch):
    seen = {}

    async def _fetch(session, rowid="", cat_id=""):
        seen["cat_id"] = cat_id
        return {}

    patch_publish(monkeypatch, "pipeline", "fetch_attr_options", _fetch)
    patch_publish(monkeypatch, "pipeline", "_expand_attr_section",
                  lambda *a, **kw: _async({}))
    s = _FakeSession(rows={"found": True, "attrs": [_row("织造方式")]})
    await pipeline.dump_attrs(s, cat_id="11717")
    assert seen["cat_id"] == "11717"


@pytest.mark.asyncio
async def test_叶子catId落进ctx并传给阶段4(monkeypatch):
    async def _fake_auto_cat(session, rowid, title, **kw):
        return {"status": "ok", "path": "A > B", "pathList": ["A", "B"],
                "source": "walk", "leafCatId": "11717"}
    patch_publish(monkeypatch, "service", "auto_cat", _fake_auto_cat)

    ctx = {"rowid": "1", "title": "标题"}
    await service._st_auto_cat(ctx, None, None)
    assert ctx["cat_id"] == "11717"

    seen = {}

    async def _fake_check(session, info_path, **kw):
        seen.update(kw)
        return {"status": "ok", "applied": []}
    patch_publish(monkeypatch, "service", "check_attrs", _fake_check)

    async def _emit(ev):
        return None

    r = await service._st_attrs({"info_path": "x.json", "cat_id": "11717"}, None, _emit)
    assert r["status"] == "ok" and seen["cat_id"] == "11717"


def test_cat_id随状态文件往返(monkeypatch, tmp_path):
    """续跑 from attrs 时阶段③被跳过，cat_id 必须能从状态文件回填，否则阶段④
    退回按草稿已保存的类目查选项——那正是本次要修掉的错。"""
    patch_publish(monkeypatch, "service", "STATE_DIR", str(tmp_path / "s"))
    service.save_state({"key": "k3", "stages": {}, "status": "running", "cat_id": "11717"})
    assert service.load_state("k3")["cat_id"] == "11717"


@pytest.mark.asyncio
async def test_老缓存条目按路径反查补catId(monkeypatch):
    """catIds 是 2026-09-11 起才随路径记的，此前攒下的几十条缓存都没有这个键。
    不补的话，「改了类目又命中缓存」的商品会继续按草稿已保存的旧类目查选项。"""
    levels = {"": [{"catId": "9711", "catName": "家居、厨房用品"}],
              "9711": [{"catId": "11649", "catName": "活动和派对用品"}],
              "11649": [{"catId": "11717", "catName": "派对装饰"}]}

    async def _shop(session, rowid):
        return "8746250"

    async def _children(session, shop_id, parent_id=""):
        return levels.get(parent_id, [])

    monkeypatch.setattr(category_api, "fetch_shop_id", _shop)
    monkeypatch.setattr(category_api, "fetch_children", _children)

    got = await category._lookup_cat_ids(_FakeSession(), "1", ["家居、厨房用品", "活动和派对用品", "派对装饰"])
    assert got == ["9711", "11649", "11717"]


@pytest.mark.asyncio
async def test_反查时中间级对不上就放弃(monkeypatch):
    """类目树变了（路径里有一级已下架）就必须整条作废：缺一级的 id 串会让阶段④
    拿它去查一个别的类目的属性清单，而查出来的东西「看起来正常」。"""
    async def _shop(session, rowid):
        return "8746250"

    async def _children(session, shop_id, parent_id=""):
        return {"": [{"catId": "9711", "catName": "家居、厨房用品"}]}.get(parent_id, [])

    monkeypatch.setattr(category_api, "fetch_shop_id", _shop)
    monkeypatch.setattr(category_api, "fetch_children", _children)

    got = await category._lookup_cat_ids(_FakeSession(), "1", ["家居、厨房用品", "已下架的中间级"])
    assert got == []
