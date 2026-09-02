# -*- coding: utf-8 -*-
"""类目/属性缓存在 pipeline 与 service 里的接线单测（假 session，不碰 CDP、不碰 LLM）。

这里测的是「缓存怎么影响流程」，而不是缓存文件本身的读写（那部分在
test_publish_cache.py）。重点钉住几条容易在后续改动中被无声打穿的不变式：
  - 缓存注入的 guard 必须排在两个 skip 之后，否则「非必填留空」策略会被绕过；
  - 缓存永不造行、永不覆盖活页面读到的 required；
  - 写入失败后的单行重读只对缓存来的行触发，成分行一律不碰；
  - use_cache=False / cat_path=None 时行为与加缓存前完全一致（零回归）。
"""
import pytest

from app.publish import cache, pipeline, service


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
        if "选择类目" in js and "categories-item" in js:
            self.clicks.append(js)
            return (self.click_results.pop(0) if self.click_results
                    else {"clicked": True})
        if "选择类目" in js and "ant-modal-footer" in js:
            self.confirmed = True
            return {"confirmed": True}
        return {}

    async def wait_for(self, js, pred, **kw):
        return self.rows

    async def kill_stuck_modals(self):
        return {}


def _fake_read(options, calls=None, complete=True):
    """假 _read_active_options，兼容 with_meta 的两种返回形状。

    真函数在 with_meta=True 时返回 (options, meta)、否则返回裸列表；替身必须照这个
    契约来，否则测的就不是真实调用路径了。
    """
    async def _f(session, label, with_meta=False):
        if calls is not None:
            calls.append(label)
        if with_meta:
            return list(options), {"virtual": True, "scrollHeight": 1704,
                                   "scrolledToEnd": complete,
                                   "complete": bool(options) and complete}
        return list(options)
    return _f


def _row(label, required=True, current="(请选择)", visible=True):
    return {"label": label, "required": required, "current": current,
            "numValues": [], "visible": visible}


@pytest.fixture
def _no_sleep(monkeypatch):
    """把 pipeline 里的等待全部掐掉，单测不该为实测出来的等待时间付时间。"""
    async def _s(*a, **kw):
        return None
    monkeypatch.setattr(pipeline.asyncio, "sleep", _s)


# ---- dump_attrs 的缓存注入 ---------------------------------------------------

@pytest.mark.asyncio
async def test_命中缓存的行不再点开下拉(_no_sleep, monkeypatch):
    cache.save_attr_options("女童针织套头衫", PATH, [
        {"label": "织造方式", "required": True, "options": ["梭织", "针织"]}])
    s = _FakeSession(rows={"found": True, "attrs": [_row("织造方式")]})
    read_calls = []

    monkeypatch.setattr(pipeline, "_read_active_options",
                        _fake_read(["现场读的"], read_calls))
    monkeypatch.setattr(pipeline, "_expand_attr_section",
                        lambda *a, **kw: _async({}))
    monkeypatch.setattr(pipeline, "_park_ghost_dropdowns",
                        lambda *a, **kw: _async({"parked": 0}))

    d = await pipeline.dump_attrs(s, cat_path=PATH)
    assert read_calls == []                      # 一次下拉都没点开
    assert d["attrs"][0]["options"] == ["梭织", "针织"]
    assert d["attrs"][0]["optionsFrom"] == "cache"
    assert d["cacheRead"] == 1 and d["activeRead"] == 0


def _async(v):
    async def _f():
        return v
    return _f()


@pytest.mark.asyncio
async def test_缓存没有的必填行仍现场读(_no_sleep, monkeypatch):
    cache.save_attr_options("女童针织套头衫", PATH, [
        {"label": "织造方式", "required": True, "options": ["梭织", "针织"]}])
    s = _FakeSession(rows={"found": True, "attrs": [_row("织造方式"), _row("季节")]})
    read_calls = []

    monkeypatch.setattr(pipeline, "_read_active_options",
                        _fake_read(["春/秋", "夏"], read_calls))
    monkeypatch.setattr(pipeline, "_expand_attr_section", lambda *a, **kw: _async({}))
    monkeypatch.setattr(pipeline, "_park_ghost_dropdowns",
                        lambda *a, **kw: _async({"parked": 0}))

    d = await pipeline.dump_attrs(s, cat_path=PATH)
    assert read_calls == ["季节"]                # 只有缓存缺的那行被现场读
    by = {a["label"]: a for a in d["attrs"]}
    assert by["织造方式"]["optionsFrom"] == "cache"
    assert by["季节"]["optionsFrom"] == "live"


@pytest.mark.asyncio
async def test_非必填且源未给值的行即使缓存有也保持留空(_no_sleep, monkeypatch):
    """guard 必须排在 optional-skipped 之后：_validate_attr_changes 靠「非必填 +
    未填 + options 空」判定「按策略留空」，提前注入会把这条策略打穿。"""
    cache.save_attr_options("女童针织套头衫", PATH, [
        {"label": "品牌名", "required": False, "options": ["A", "B"]}])
    s = _FakeSession(rows={"found": True,
                           "attrs": [_row("品牌名", required=False)]})
    monkeypatch.setattr(pipeline, "_read_active_options", _fake_read([]))
    monkeypatch.setattr(pipeline, "_expand_attr_section", lambda *a, **kw: _async({}))
    monkeypatch.setattr(pipeline, "_park_ghost_dropdowns",
                        lambda *a, **kw: _async({"parked": 0}))

    d = await pipeline.dump_attrs(s, cat_path=PATH)
    a = d["attrs"][0]
    assert a["options"] == [] and a["optionsEmptyReason"] == "optional-skipped"
    assert "optionsFrom" not in a


@pytest.mark.asyncio
async def test_隐藏行即使缓存有也不注入(_no_sleep, monkeypatch):
    cache.save_attr_options("女童针织套头衫", PATH, [
        {"label": "里衬成分", "required": True, "options": ["棉"]}])
    s = _FakeSession(rows={"found": True,
                           "attrs": [_row("里衬成分", visible=False)]})
    monkeypatch.setattr(pipeline, "_read_active_options", _fake_read([]))
    monkeypatch.setattr(pipeline, "_expand_attr_section", lambda *a, **kw: _async({}))
    monkeypatch.setattr(pipeline, "_park_ghost_dropdowns",
                        lambda *a, **kw: _async({"parked": 0}))

    d = await pipeline.dump_attrs(s, cat_path=PATH)
    assert d["attrs"][0]["optionsEmptyReason"] == "row-hidden"
    assert d["attrs"][0]["options"] == []


@pytest.mark.asyncio
async def test_缓存不造行(_no_sleep, monkeypatch):
    """缓存里有、活页面没有的 label 不能出现在输出里——行集只由活 DOM 决定。"""
    cache.save_attr_options("女童针织套头衫", PATH, [
        {"label": "织造方式", "required": True, "options": ["梭织"]},
        {"label": "早已下架的属性", "required": True, "options": ["X"]}])
    s = _FakeSession(rows={"found": True, "attrs": [_row("织造方式")]})
    monkeypatch.setattr(pipeline, "_read_active_options", _fake_read([]))
    monkeypatch.setattr(pipeline, "_expand_attr_section", lambda *a, **kw: _async({}))
    monkeypatch.setattr(pipeline, "_park_ghost_dropdowns",
                        lambda *a, **kw: _async({"parked": 0}))

    d = await pipeline.dump_attrs(s, cat_path=PATH)
    assert [a["label"] for a in d["attrs"]] == ["织造方式"]


@pytest.mark.asyncio
async def test_required用活页面的值不用缓存的(_no_sleep, monkeypatch):
    """required 是 _validate_attr_changes 第 1 道闸的依据，用缓存里的旧值等于拿旧
    策略判新表单（平台会调整必填与否，实测同类目从 18 必填变成 16）。"""
    cache.save_attr_options("女童针织套头衫", PATH, [
        {"label": "腰带", "required": False, "options": ["有", "无"]}])
    s = _FakeSession(rows={"found": True,
                           "attrs": [_row("腰带", required=True)]})
    monkeypatch.setattr(pipeline, "_read_active_options", _fake_read([]))
    monkeypatch.setattr(pipeline, "_expand_attr_section", lambda *a, **kw: _async({}))
    monkeypatch.setattr(pipeline, "_park_ghost_dropdowns",
                        lambda *a, **kw: _async({"parked": 0}))

    # 活页面说必填，所以这行不会被 optional-skipped，能走到注入
    d = await pipeline.dump_attrs(s, cat_path=PATH)
    assert d["attrs"][0]["required"] is True
    assert d["attrs"][0]["optionsFrom"] == "cache"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"use_cache": False, "cat_path": PATH},
                                    {"cat_path": None}])
async def test_禁用缓存或无类目路径时零回归(_no_sleep, monkeypatch, kwargs):
    cache.save_attr_options("女童针织套头衫", PATH, [
        {"label": "织造方式", "required": True, "options": ["缓存的"]}])
    s = _FakeSession(rows={"found": True, "attrs": [_row("织造方式")]})
    monkeypatch.setattr(pipeline, "_read_active_options",
                        _fake_read(["现场读的"]))
    monkeypatch.setattr(pipeline, "_expand_attr_section", lambda *a, **kw: _async({}))
    monkeypatch.setattr(pipeline, "_park_ghost_dropdowns",
                        lambda *a, **kw: _async({"parked": 0}))

    d = await pipeline.dump_attrs(s, **kwargs)
    assert d["attrs"][0]["options"] == ["现场读的"]
    assert d["cacheRead"] == 0 and d["activeRead"] == 1


@pytest.mark.asyncio
async def test_skip_options不注入缓存(_no_sleep, monkeypatch):
    """那条路径语义是「一个下拉都不点、只看现状」，注入会让它返回 optionsRead=False
    却带着 options，把返回契约打乱。"""
    cache.save_attr_options("女童针织套头衫", PATH, [
        {"label": "织造方式", "required": True, "options": ["梭织"]}])
    s = _FakeSession(rows={"found": True, "attrs": [_row("织造方式")]})
    monkeypatch.setattr(pipeline, "_expand_attr_section", lambda *a, **kw: _async({}))

    d = await pipeline.dump_attrs(s, skip_options=True, cat_path=PATH)
    assert d["optionsRead"] is False
    assert "options" not in d["attrs"][0]


@pytest.mark.asyncio
async def test_现场读到的行写回缓存(_no_sleep, monkeypatch):
    s = _FakeSession(rows={"found": True, "attrs": [_row("织造方式")]})
    monkeypatch.setattr(pipeline, "_read_active_options",
                        _fake_read(["梭织", "针织"]))
    monkeypatch.setattr(pipeline, "_expand_attr_section", lambda *a, **kw: _async({}))
    monkeypatch.setattr(pipeline, "_park_ghost_dropdowns",
                        lambda *a, **kw: _async({"parked": 0}))

    await pipeline.dump_attrs(s, cat_path=PATH)
    assert cache.load_attr_options("女童针织套头衫", PATH) == {
        "织造方式": ["梭织", "针织"]}


@pytest.mark.asyncio
async def test_未滚到底的选项不写缓存(_no_sleep, monkeypatch):
    """静默截断是这一带最难发现的一类错（67 项成分只读到首屏 10 条）。
    缓存了截断清单，之后每个同类目商品都拿缺项 options 做校验与纤维匹配。"""
    s = _FakeSession(rows={"found": True, "attrs": [_row("上装成分")]})
    monkeypatch.setattr(pipeline, "_read_active_options",
                        _fake_read(["棉", "腈纶"], complete=False))
    monkeypatch.setattr(pipeline, "_expand_attr_section", lambda *a, **kw: _async({}))
    monkeypatch.setattr(pipeline, "_park_ghost_dropdowns",
                        lambda *a, **kw: _async({"parked": 0}))

    d = await pipeline.dump_attrs(s, cat_path=PATH)
    # 本次仍然用它（现场读的就是当下能拿到的最好结果），但不许进缓存
    assert d["attrs"][0]["options"] == ["棉", "腈纶"]
    assert d["attrs"][0]["optionsComplete"] is False
    assert cache.load_attr_options("女童针织套头衫", PATH) == {}


@pytest.mark.asyncio
async def test_截断的重读结果不回灌缓存(_no_sleep, monkeypatch):
    cache.save_attr_options("女童针织套头衫", PATH, [
        {"label": "织造方式", "required": True, "options": ["旧选项"]}])
    monkeypatch.setattr(pipeline, "_read_active_options",
                        _fake_read(["截断的"], complete=False))
    monkeypatch.setattr(pipeline, "set_attr",
                        lambda *a, **kw: _async({"status": "ok"}))
    monkeypatch.setattr("app.publish.llm.ask_json",
                        lambda *a, **kw: _async({"value": "截断的", "reason": "r"}))

    await pipeline._refresh_row_and_retry(
        _FakeSession(), {"label": "织造方式", "value": "已下架"},
        {"current": "(请选择)"}, PATH)
    # 过期数据不该被换成缺项数据
    assert cache.load_attr_options("女童针织套头衫", PATH)["织造方式"] == ["旧选项"]


@pytest.mark.asyncio
async def test_全命中时不重复写缓存(_no_sleep, monkeypatch):
    cache.save_attr_options("女童针织套头衫", PATH, [
        {"label": "织造方式", "required": True, "options": ["梭织"]}])
    s = _FakeSession(rows={"found": True, "attrs": [_row("织造方式")]})
    monkeypatch.setattr(pipeline, "_expand_attr_section", lambda *a, **kw: _async({}))
    monkeypatch.setattr(pipeline, "_park_ghost_dropdowns",
                        lambda *a, **kw: _async({"parked": 0}))
    written = []
    monkeypatch.setattr(cache, "save_attr_options",
                        lambda *a, **kw: written.append(a))

    await pipeline.dump_attrs(s, cat_path=PATH)
    assert written == []          # activeRead == 0，没有新东西可写


# ---- 类目缓存快路径 ----------------------------------------------------------

@pytest.mark.asyncio
async def test_清单为空时不调LLM(_no_sleep, monkeypatch):
    """一条已知路径都没有时连提示词都不该拼——省一次 LLM 调用。"""
    called = []
    monkeypatch.setattr(pipeline, "_pick_cached_category",
                        lambda *a, **kw: called.append(1))
    assert await pipeline._try_cached_category(_FakeSession(), "标题") is None
    assert called == []


@pytest.mark.asyncio
async def test_答不匹配则落回遍历(_no_sleep, monkeypatch):
    cache.remember_category(PATH, "旧标题")

    async def _pick(title, known, clues=""):
        return None, "都不匹配"
    monkeypatch.setattr(pipeline, "_pick_cached_category", _pick)
    assert await pipeline._try_cached_category(_FakeSession(), "标题") is None


@pytest.mark.asyncio
async def test_LLM异常时落回遍历不抛(_no_sleep, monkeypatch):
    """缓存是加速手段，它自己的 LLM 调用失败不该拖垮整个阶段。"""
    cache.remember_category(PATH, "旧标题")

    async def _boom(title, known):
        raise RuntimeError("模型抽风")
    monkeypatch.setattr(pipeline, "_pick_cached_category", _boom)
    assert await pipeline._try_cached_category(_FakeSession(), "标题") is None


@pytest.mark.asyncio
async def test_中途点不中则落回且不再点后续级(_no_sleep, monkeypatch):
    """类目树变了，继续点只会在错位的列里乱点，把弹窗状态搅得更脏。"""
    cache.remember_category(PATH, "旧标题")

    async def _pick(title, known, clues=""):
        return PATH, "命中"
    monkeypatch.setattr(pipeline, "_pick_cached_category", _pick)
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
    monkeypatch.setattr(pipeline, "_pick_cached_category", _pick)
    monkeypatch.setattr(pipeline, "read_current_category",
                        lambda *a, **kw: _async("产品分类：完全不相干的类目"))
    assert await pipeline._try_cached_category(_FakeSession(), "标题") is None


@pytest.mark.asyncio
async def test_命中返回与遍历同构(_no_sleep, monkeypatch):
    """_st_auto_cat 与 publish_inspect 都消费这个返回，形状必须一致。"""
    cache.remember_category(PATH, "旧标题")

    async def _pick(title, known, clues=""):
        return PATH, "标题里有针织套头"
    monkeypatch.setattr(pipeline, "_pick_cached_category", _pick)
    monkeypatch.setattr(
        pipeline, "read_current_category",
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


# ---- 单行回落重读 -----------------------------------------------------------

@pytest.mark.asyncio
async def test_目标值仍在新选项里则原值重试不问LLM(_no_sleep, monkeypatch):
    """不白花一次 LLM 调用：set_attr 内部已自愈过一次，这里隔了一次真实下拉开合。"""
    cache.save_attr_options("女童针织套头衫", PATH, [
        {"label": "织造方式", "required": True, "options": ["旧选项"]}])
    monkeypatch.setattr(pipeline, "_read_active_options",
                        _fake_read(["梭织", "针织"]))
    asked = []
    monkeypatch.setattr("app.publish.llm.ask_json",
                        lambda *a, **kw: asked.append(1))
    sets = []

    async def _set(session, label, value, num=None, row=1):
        sets.append((label, value))
        return {"status": "ok", "readback": {"current": value}}
    monkeypatch.setattr(pipeline, "set_attr", _set)

    fix = await pipeline._refresh_row_and_retry(
        _FakeSession(), {"label": "织造方式", "value": "针织"},
        {"current": "(请选择)", "required": True}, PATH)
    assert fix["status"] == "ok" and fix["askedLLM"] is False
    assert asked == [] and sets == [("织造方式", "针织")]
    # 不管救不救得回来，过期缓存都得换掉
    assert cache.load_attr_options("女童针织套头衫", PATH)["织造方式"] == ["梭织", "针织"]


@pytest.mark.asyncio
async def test_目标值没了才问LLM并再过options闸(_no_sleep, monkeypatch):
    monkeypatch.setattr(pipeline, "_read_active_options",
                        _fake_read(["梭织", "针织"]))

    async def _ask(prompt, what="判断", **kw):
        return {"value": "手工编织", "reason": "编的"}   # 模型照样会编造
    monkeypatch.setattr("app.publish.llm.ask_json", _ask)
    sets = []
    monkeypatch.setattr(pipeline, "set_attr",
                        lambda *a, **kw: sets.append(a) or _async({"status": "ok"}))

    fix = await pipeline._refresh_row_and_retry(
        _FakeSession(), {"label": "织造方式", "value": "已下架选项"},
        {"current": "(请选择)", "required": True}, PATH)
    assert fix["status"] == "error" and fix["askedLLM"] is True
    assert sets == []                     # 不在 options 内就不去点


@pytest.mark.asyncio
async def test_重读为空直接放弃(_no_sleep, monkeypatch):
    monkeypatch.setattr(pipeline, "_read_active_options", _fake_read([]))
    fix = await pipeline._refresh_row_and_retry(
        _FakeSession(), {"label": "织造方式", "value": "针织"},
        {"current": "(请选择)"}, PATH)
    assert fix["status"] == "error" and fix["askedLLM"] is False


# ---- service 层接线 ---------------------------------------------------------

@pytest.mark.asyncio
async def test_use_cache透传进ctx(monkeypatch, tmp_path):
    seen = {}

    async def _fake_auto_cat(session, rowid, title, **kw):
        seen.update(kw)
        return {"status": "ok", "path": " > ".join(PATH), "pathList": PATH,
                "source": "cache"}
    monkeypatch.setattr(service, "auto_cat", _fake_auto_cat)

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
    monkeypatch.setattr(service, "auto_cat", _fake_auto_cat)
    ctx = {"rowid": "1", "title": "标题"}
    r = await service._st_auto_cat(ctx, None, None)
    assert r["note"].startswith("[遍历]")


@pytest.mark.asyncio
async def test_成分写入失败发manual_check(monkeypatch):
    async def _fake_check(session, info_path, **kw):
        return {"status": "ok", "applied": [{"result": "error"}],
                "rejected": [], "compFailed": ["上装成分"], "cacheRead": 3}
    monkeypatch.setattr(service, "check_attrs", _fake_check)
    events = []

    async def _emit(ev):
        events.append(ev)
    r = await service._st_attrs({"info_path": "x.json"}, None, _emit)
    assert r["status"] == "ok"
    assert any(e["type"] == "manual_check" and "上装成分" in e["message"]
               for e in events)
    assert "缓存选项 3 行" in r["note"]


@pytest.mark.asyncio
async def test_cat_path随状态文件往返(monkeypatch, tmp_path):
    """续跑 from attrs 时阶段③被跳过，cat_path 必须能从状态文件回填，
    否则属性缓存取不到键、退化成全量读。"""
    monkeypatch.setattr(service, "STATE_DIR", str(tmp_path / "s"))
    st = {"key": "k1", "stages": {}, "status": "running", "cat_path": PATH}
    service.save_state(st)
    assert service.load_state("k1")["cat_path"] == PATH


def test_旧状态文件没有cat_path也能读(monkeypatch, tmp_path):
    """向后兼容：旧文件没这个键 → None → 未命中 → 全量读，与今天行为一致。"""
    monkeypatch.setattr(service, "STATE_DIR", str(tmp_path / "s"))
    service.save_state({"key": "k2", "stages": {}, "status": "ok"})
    assert service.load_state("k2").get("cat_path") is None
