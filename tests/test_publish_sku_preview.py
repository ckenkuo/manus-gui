# -*- coding: utf-8 -*-
r"""阶段⑦b SKU 预览图（变种信息表第一列）——2026-08-30 玩具类发布被拒后新增。

钉住的成因：预览图是【第三处】图片位，与 ⑥ 素材图（.material-img-module）、
⑦ SKC 颜色图（#skuAttrsInfo 变种属性区）都不是同一个地方，它在 #skuDataInfo
（变种信息表）第一列，管线此前从来没有代码碰过它。

真站取证（rowid 173539495459009087，玩具「儿童弹射泡沫飞机」，8 颜色无尺码）：
  - 阶段⑦ 记 skipped 且【判断是对的】——skc_image_support 探 #skuAttrsInfo，
    该类目那个区块确实无图位（8 个复选框全是颜色）；
  - 但 8 行预览图仍是 1688 原始外链：720x606 / 749x627 / 717x610 三张连 1:1
    比例都不满足，另有 3 张恰好 800x800、1 张 1920x1920；
  - 发布被平台拒：「错误：预览图尺寸不能小于800*800」。

服装类此前没暴露，是因为 1688 服装主图普遍 >=800x800 恰好蒙过。

全程离线：假会话 + 纯函数，不连 CDP、不发 LLM 请求、不碰真实图床。
"""

from publish_patching import patch_publish
import json

import pytest

from app.publish import pipeline as P
from app.publish import service as S


# ---- 1) 阶段接线：⑦b 必须是独立阶段且进续跑重跑集 ----------------------------

def test_阶段已注册且顺序在skc之后fix_sizes之前():
    """⑦b 的位置不能随意挪：它读的是变种信息表，而那张表由 ③ 类目决定何时渲染，
    放在 ⑦ 之后是因为两者都属图片类、便于人看日志；必须在 ⑭ save 之前。"""
    ids = [k for k, _ in S.STAGES]
    assert "sku_preview" in ids
    assert ids.index("skc") < ids.index("sku_preview") < ids.index("fix_sizes")
    assert ids.index("sku_preview") < ids.index("save")


def test_阶段表与STAGES一一对应():
    """漏在 _STAGE_FUNCS 里注册会让该阶段被静默跳过（跑批时完全看不出）。"""
    assert set(k for k, _ in S.STAGES) == set(S._STAGE_FUNCS)


def test_进续跑重跑集():
    """⑦b 只改【未保存表单】，save 没成功过就等于没跑（同 ⑤~⑬ 全体）。
    不进重跑集的话，续跑会跳过它、直奔 save/publish，又被同一个错拦下。"""
    assert "sku_preview" in S._FORM_ONLY_STAGES
    assert "sku_preview" in S._FORM_STAGES_AFTER_CAT


# ---- 2) 现状判读：bad 的判据 --------------------------------------------------

class _StateSession:
    """只回一份预定的 _JS_SKU_PREVIEW_STATE 结果。"""

    def __init__(self, payload: dict):
        self.payload = payload
        self.js_log: list = []

    async def eval_json(self, js, *a, **kw):
        self.js_log.append(js)
        return self.payload


def _row(i, color, w, h, url="https://cbu01.alicdn.com/x.jpg"):
    """按 JS 侧那套判据算出 bad，模拟真实返回（JS 已算好 bad，Python 侧只读）。"""
    known = w > 0 and h > 0
    square = known and abs(w / h - 1) < 0.01
    bad = known and (not square or w < P.PREVIEW_MIN_SIDE or h < P.PREVIEW_MIN_SIDE)
    return {"i": i, "color": color, "url": url, "w": w, "h": h, "bad": bad}


@pytest.mark.asyncio
async def test_有trigger判supported():
    s = _StateSession({"rows": [_row(0, "蓝枪普通款", 800, 800)],
                       "heads": ["预览图( 批量)", "颜色"],
                       "previewIdx": 0, "colorIdx": 1, "hasTrigger": True})
    st = await P.sku_preview_state(s)
    assert st["supported"] is True


@pytest.mark.asyncio
async def test_有行但无trigger仍检查预览图():
    """空格可能只有点击入口，没有悬停触发器。"""
    s = _StateSession({"rows": [_row(0, "均码", 800, 800)],
                       "heads": ["预览图( 批量)", "颜色"],
                       "previewIdx": 0, "colorIdx": 1, "hasTrigger": False})
    st = await P.sku_preview_state(s)
    assert st["supported"] is True


@pytest.mark.asyncio
async def test_零行判证据不足而非不支持():
    """表没渲染完时不能当「不支持」——那会静默跳过一个真该跑的阶段。"""
    s = _StateSession({"rows": [], "heads": [], "previewIdx": 0,
                       "colorIdx": 1, "hasTrigger": False})
    st = await P.sku_preview_state(s)
    assert st["supported"] is None


@pytest.mark.asyncio
async def test_没有预览图列判证据不足():
    s = _StateSession({"err": "no-preview-column", "heads": ["颜色", "尺码"]})
    st = await P.sku_preview_state(s)
    assert st["supported"] is None and st["err"] == "no-preview-column"


def test_bad判据覆盖真站那八行():
    """2026-08-30 实测的 8 行：3 张 800x800、1 张 1920x1920、3 张非 1:1 破线。

    注意 800x800 这一档：拒绝文案说「不能小于 800*800」，字面上 800 应当通过，
    故 bad 判据里它【不算 bad】——统一重做是 service 层的取向（顺带消除边界），
    不是把判据改松。两者要分清，否则这条判据日后会被误改。
    """
    assert _row(0, "泡沫飞机", 1920, 1920)["bad"] is False
    assert _row(1, "蓝枪普通款", 800, 800)["bad"] is False
    assert _row(4, "蓝枪升级款", 720, 606)["bad"] is True     # 非 1:1 且短边破线
    assert _row(5, "红枪升级款", 749, 627)["bad"] is True
    assert _row(6, "黄枪升级款", 717, 610)["bad"] is True


def test_未知尺寸不判bad():
    """naturalWidth=0 是「图没加载完」，未知不等于不合格（同 upload_image 尺寸闸）。"""
    assert _row(0, "某色", 0, 0)["bad"] is False


def test_方图但小于下限仍判bad():
    """1:1 满足、像素不够也要重做——两条要求是并列的，不是二选一。"""
    assert _row(0, "某色", 600, 600)["bad"] is True


def test_大图但非方形判bad():
    """像素够、比例不对同样过不了（页面原文要求比例 1：1）。"""
    assert _row(0, "某色", 1600, 1200)["bad"] is True


def test_状态脚本带行级换图入口():
    """坏行要能分流「有 trigger 能换图」与「无 trigger 继承图」——后者
    sku_preview_replace_row 报「该行没有预览图 trigger」，service 据此不判 fail
    （2026-09-06 两单宠物窝 0/19、0/6 全卡在这里）。"""
    assert "hasTrigger" in P._JS_SKU_PREVIEW_STATE
    assert "sku-image-box.ant-dropdown-trigger" in P._JS_SKU_PREVIEW_STATE


@pytest.mark.asyncio
async def test_坏行全无换图入口判fail(monkeypatch, tmp_path):
    """不合规图片无入口仍必须拦截。"""
    async def fake_state(session):
        return {"supported": True, "rows": [
            {"i": 0, "color": "洛克黄", "url": "https://x.jpg", "w": 600, "h": 600,
             "bad": True, "empty": False, "hasTrigger": False},
        ], "previewIdx": 0, "colorIdx": 1}

    events = []

    async def emit(ev):
        events.append(ev)

    # service 模块 import 的是自己的名字，mock 要打在 S 上而不是 P 上
    patch_publish(monkeypatch, "service", "sku_preview_state", fake_state)
    r = await S._st_sku_preview({"workdir": str(tmp_path)}, None, emit)
    assert r["status"] == "fail"
    assert any(e.get("type") == "manual_check" and e.get("stage") == "sku_preview"
               for e in events)


# ---- 3) 续跑实况判定：⑦b 与 ⑥⑦ 必须分开判 ------------------------------------

def _live(**kw):
    """一份「一切正常」的实况，再按需覆盖——只测被覆盖那一项的影响。"""
    base = {
        "rendered": True, "catUnset": False, "catDeleted": False,
        "titleFilled": True, "attrImgCount": 6, "attrImgBad": 0,
        "skuRowCount": 8, "skuFilledRows": 8, "skuCodeCount": 8, "skuCodeBad": 0,
        "hasSizeGroup": False, "sizechartCount": 0, "sizechartAdded": False,
        "sizechart2Added": None, "shippingSet": True,
        "descImgCount": 6, "descForeignCount": 0,
        "previewCount": 8, "previewBad": 0,
    }
    base.update(kw)
    return base


def test_预览图破线进重跑集():
    stale = S._stale_form_stages(_live(previewBad=3))
    assert "sku_preview" in stale


def test_预览图全达标不进重跑集():
    stale = S._stale_form_stages(_live(previewBad=0))
    assert "sku_preview" not in stale


def test_无预览图列不进重跑集():
    """previewCount=0 说明该类目没这一列，交阶段自己 skipped，不必每轮重跑。"""
    stale = S._stale_form_stages(_live(previewCount=0, previewBad=0))
    assert "sku_preview" not in stale


def test_预览图破线不连带重跑skc():
    """两处正交：变种属性区的图好着，没理由重跑 ⑦（那要白烧一轮上传与挂图）。

    这正是这次事故的镜像——⑦ 跳过不代表 ⑦b 不用跑，反过来也一样。
    """
    stale = S._stale_form_stages(_live(previewBad=3, attrImgCount=6, attrImgBad=0))
    assert "sku_preview" in stale and "skc" not in stale


def test_skc破线不连带重跑预览图():
    stale = S._stale_form_stages(_live(attrImgBad=2, previewBad=0))
    assert "skc" in stale and "sku_preview" not in stale


def test_类目失效时全量重跑含预览图():
    """类目一失效下游全部作废，⑦b 也在其中（它读的表由类目决定何时渲染）。"""
    stale = S._stale_form_stages(_live(catUnset=True))
    assert "sku_preview" in stale


def test_读不到实况时全量重跑含预览图():
    assert "sku_preview" in S._stale_form_stages({"rendered": False})


# ---- 4) 单行替换：行序核对与成功判据 ------------------------------------------

class _ReplaceSession:
    """模拟一次完整的单行替换：上传 → 开弹窗 → 选图 → 回读。

    按 JS 内容分派（与 _FakeSession 在别处的做法一致）：
      含 sku-image-box  → 开弹窗脚本，回 nowColor
      含 img-item       → 弹窗选图脚本
      含 naturalWidth   → 回读脚本
    """

    def __init__(self, now_color="蓝枪普通款", readback_has_fid=True,
                 opened=True, open_err=""):
        self.now_color = now_color
        self.readback_has_fid = readback_has_fid
        self.opened = opened
        self.open_err = open_err
        self.js_log: list = []
        self.closed = 0

    async def eval_json(self, js, *a, **kw):
        self.js_log.append(js)
        if "sku-image-box" in js:
            r = {"stage": "ok", "opened": self.opened,
                 "nowColor": self.now_color,
                 "srcBefore": "https://cbu01.alicdn.com/old.jpg"}
            if self.open_err:
                r = {"stage": "menu", "err": self.open_err,
                     "nowColor": self.now_color}
            return r
        if "img-item" in js:
            return {"stage": "ok", "picked": True, "stillOpen": False}
        if "naturalWidth" in js:
            src = ("https://wxalbum-10001658-file.dianxiaomi.com"
                   "/wxalbum/2525332/20260830/newfid.jpg"
                   if self.readback_has_fid
                   else "https://cbu01.alicdn.com/old.jpg")
            return {"src": src, "w": 1785, "h": 1785}
        if "取消" in js:                     # _close_space_modal
            self.closed += 1
            return {"wasOpen": True, "clicked": True, "stillOpen": False}
        return {}


@pytest.fixture
def _stub_upload(monkeypatch):
    """把 upload_image 换成不碰网络的桩：回一个固定 fileId。"""
    async def _fake(session, path, full_cid=None):
        return {"status": "ok",
                "fileId": "/wxalbum/2525332/20260830/newfid.jpg"}

    patch_publish(monkeypatch, "pipeline", "upload_image", _fake)
    return _fake


@pytest.mark.asyncio
async def test_替换成功判据是回读含新fileId(_stub_upload):
    s = _ReplaceSession(readback_has_fid=True)
    r = await P.sku_preview_replace_row(s, 1, "x.jpg", 0,
                                        color_idx=1, expect_color="蓝枪普通款")
    assert r["status"] == "ok", r
    assert r["fileId"].endswith("newfid.jpg")
    assert r["sizeAfter"] == {"w": 1785, "h": 1785}


@pytest.mark.asyncio
async def test_回读不含新fileId判失败(_stub_upload):
    """挂错行时本行 src 同样会变，故只看「变了」不够——必须含新 fileId。

    这是原脚本素材图误替换事故的教训（见 set_material 注释），不能放松。
    """
    s = _ReplaceSession(readback_has_fid=False)
    r = await P.sku_preview_replace_row(s, 1, "x.jpg", 0,
                                        color_idx=1, expect_color="蓝枪普通款")
    assert r["status"] == "error" and r["stage"] == "readback", r


@pytest.mark.asyncio
async def test_行序变了拒绝替换并关弹窗(_stub_upload):
    """Vue 若在读与写之间重排过，挂上去就是挂到别的 SKU 上——宁可报错跳过。"""
    s = _ReplaceSession(now_color="红枪升级款【闪光】")
    r = await P.sku_preview_replace_row(s, 1, "x.jpg", 0,
                                        color_idx=1, expect_color="蓝枪普通款")
    assert r["status"] == "error" and r["stage"] == "row-moved", r
    assert s.closed == 1, "拒绝替换后必须关掉弹窗，否则遮罩挡住后续所有点击"


@pytest.mark.asyncio
async def test_不传expect_color则不核对(_stub_upload):
    """CLI 单步调试时可能拿不到颜色名，此时不该因为核对不了而拒绝。"""
    s = _ReplaceSession(now_color="任意颜色")
    r = await P.sku_preview_replace_row(s, 1, "x.jpg", 0, color_idx=-1)
    assert r["status"] == "ok", r


@pytest.mark.asyncio
async def test_上传失败不碰页面(monkeypatch):
    """上传就失败时不该去开菜单——那会留下一个开着的弹窗挡住后续阶段。"""
    async def _fail(session, path, full_cid=None):
        return {"status": "error", "err": "COS PUT 被拦"}

    patch_publish(monkeypatch, "pipeline", "upload_image", _fail)
    s = _ReplaceSession()
    r = await P.sku_preview_replace_row(s, 0, "x.jpg", 0)
    assert r["status"] == "error" and r["stage"] == "upload"
    assert not s.js_log, "上传失败后不该有任何页面操作"


@pytest.mark.asyncio
async def test_开弹窗失败带上诊断(_stub_upload):
    s = _ReplaceSession(open_err="悬停后预览图菜单未展开", opened=False)
    r = await P.sku_preview_replace_row(s, 0, "x.jpg", 0)
    assert r["status"] == "error" and r["stage"] == "open-space"
    assert "菜单未展开" in json.dumps(r, ensure_ascii=False)


# ---- 5) 菜单与规格常量 --------------------------------------------------------

def test_菜单特征项含应用到全部():
    """「应用到全部」是预览图菜单独有的特征项（素材图菜单只有 4 项、没有它），
    靠它才能在页面十几个 .ant-dropdown 实例里唯一认出预览图那个菜单。"""
    assert "应用到全部" in P.SKU_PREVIEW_MENU_ITEMS
    assert "空间图片" in P.SKU_PREVIEW_MENU_ITEMS


def test_菜单项不与素材图菜单混淆():
    """素材图菜单 4 项里没有「应用到全部」，故两套特征集不会互相命中。"""
    assert "应用到全部" not in P.MATERIAL_MENU_ITEMS


def test_预览图下限是800():
    """页面素材图区原文「比例为1：1，不小于800*800」，拒绝文案与之一致。
    改这个常量等于改平台规格，必须有新的真站取证。"""
    assert P.PREVIEW_MIN_SIDE == 800


def test_复用素材图那套空间弹窗():
    """2026-08-30 探查确认预览图与 ⑥⑦ 的空间弹窗是同一个组件实例（标题、
    .img-item、.img-check 文本、计数器全一致），故 _pick_from_space 可直接复用。"""
    assert P.SPACE_MODAL_TITLE == "从图片空间选择"
