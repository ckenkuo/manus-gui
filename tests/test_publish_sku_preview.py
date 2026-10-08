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
import os

import pytest
from PIL import Image, ImageDraw

from app.publish import pipeline as P
from app.publish import service as S


# ---- 1) 阶段接线：⑦b 必须是独立阶段且进续跑重跑集 ----------------------------

def test_阶段已注册且顺序在fix_sizes之后save之前():
    """⑦b 的位置不能随意挪：它读的是变种信息表，而那张表由 ③ 类目决定何时渲染，
    放在 ⑦ 之后是因为两者都属图片类、便于人看日志；必须在 ⑭ save 之前。

    【2026-09-28 起从「⑧ 之前」改成「⑧ 之后」】⑧ 的职责是「勾选状态与源 SKU 一致」，
    会把 ⑦b 反选掉的规格重新勾回来——而 ⑦b 的反选正是「预览图补不上就不发这个规格」
    的落地手段，被勾回来那行又是空图位，整单卡在 ⑭「请上传预览图」（1688 offer
    1067355258988 实况：变种表第一维是尺码，反选的是 S/XL）。故反选必须排在 ⑧ 之后，
    同时也在 ⑨⑩a⑩⑪ 之前——那几步按行填数，行集中途一变就全对不上。
    阶段 id 与显示名保持 sku_preview / ⑦b 不变，续跑状态文件按 id 认它。"""
    ids = [k for k, _ in S.STAGES]
    assert "sku_preview" in ids
    assert ids.index("skc") < ids.index("fix_sizes") < ids.index("sku_preview")
    assert ids.index("sku_preview") < ids.index("sizechart")
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
    square = known and w == h
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


def test_只差两px的近方图也判bad():
    """2026-09-19 商品 1081125452313：1206x1204 差 2px，比例容差 0.00166 曾放它过去，
    平台照样拒「错误：变种预览图必须为1:1尺寸」。方图判据必须严格 w == h——那单因此
    整行一次没碰（note 写「0 行预览图已处理」），错误一路延后到发布才暴露。"""
    assert _row(0, "可爱甜玉米抱枕", 1206, 1204)["bad"] is True
    assert _row(1, "某色", 1206, 1206)["bad"] is False


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


def test_预览图列存在就进重跑集():
    """⑦b 与 ⑤c 同口径：成果丢没丢实况里没有稳定信号。

    2026-09-19 起 ⑦b 还会英化【几何本来就合格】的预览图（那批同样是源站原图、同样
    可能带中文），而这类成果被页面重载冲掉后回到的仍是尺寸没问题的源图，previewBad
    恒 0 判不出来——故判据从 previewBad 改成 previewCount。
    """
    stale = S._stale_form_stages(_live(previewBad=0))
    assert "sku_preview" in stale


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


def test_skc破线只牵连自己那一侧():
    """两处正交：变种属性区的图（⑦）与变种信息表的预览图（⑦b）各判各的。

    ⑦b 恒在重跑集里（判据是 previewCount，见上一条），这里钉的是 skc 那一侧：
    只因 attrImgBad 破线而入集，不因为别的图位。
    """
    stale = S._stale_form_stages(_live(attrImgBad=2, previewBad=0))
    assert "skc" in stale
    assert "sku_preview" in stale


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
        if "closedModals" in js:            # 关残留浮层（replace_row 前置清理，不含「取消」误判）
            return {}
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


# ---- 6) 英化产物的判定口径：软类（只报乱码/错拼）要两问一致才继续重生 ---------
# 判据本体在 vision.qc_hard（与 ⑤c 共用一份，取证见那边 docstring）。这里钉的是
# _clean_downloaded_preview 的三支行为：软类先补问、硬红线不补问、两次一致的软类照旧重生。

def _stub_clean_env(monkeypatch, verdicts):
    """把出图与质检换成脚本化假实现，返回 (出图调用记录, 质检调用记录)。"""
    from app.publish.stages import preview

    edits, checks = [], []

    async def fake_edit(path, **kwargs):
        edits.append(path)
        out = kwargs["out_path"]
        Image.new("RGB", (800, 800), "white").save(out)
        return {"output": out}

    async def fake_check(path):
        checks.append(path)
        return verdicts.pop(0) if verdicts else {"clean": True, "issues": ""}

    monkeypatch.setattr(preview.images, "edit_image_async", fake_edit)
    monkeypatch.setattr(preview.vision, "check_cleaned", fake_check)
    return edits, checks


def _raw(tmp_path):
    path = tmp_path / "row-raw.jpg"
    Image.new("RGB", (800, 800), "white").save(path)
    return str(path)


@pytest.mark.asyncio
async def test_软类首答坏复问好就采用这一版(monkeypatch, tmp_path):
    """明确只报乱码/错拼时，首答的坏结论不可复现（取证见 vision.qc_hard）：补问一次
    （零生图成本），复问说好就用这一版——不白烧一发生图，也不退回源图停整单。"""
    from app.publish.stages import preview

    edits, checks = _stub_clean_env(monkeypatch, [
        {"clean": False, "garbled": True, "issues": "叠加文案拼写错误 Magnuetic"},
        {"clean": True, "issues": ""},
    ])
    out = await preview._clean_downloaded_preview(_raw(tmp_path), str(tmp_path))
    assert out and os.path.isfile(out)
    assert len(edits) == 1, "复问判好就不该再烧一发生图"
    assert len(checks) == 2


@pytest.mark.asyncio
async def test_硬红线首答坏不补问直接重生(monkeypatch, tmp_path):
    """品牌标识/中文这类一次就当真：补问只是给同一次抖动第二次机会，红线不吃这一套。"""
    from app.publish.stages import preview

    edits, checks = _stub_clean_env(monkeypatch, [
        {"clean": False, "brandMark": True, "issues": "盒上有他人品牌标识 382 TOYS"},
        {"clean": True, "issues": ""},
    ])
    out = await preview._clean_downloaded_preview(_raw(tmp_path), str(tmp_path))
    assert out and os.path.isfile(out)
    assert len(edits) == 2, "硬红线首答坏要直接重生"
    assert len(checks) == 2, "硬红线不补问（两问分别属于两次尝试）"


@pytest.mark.asyncio
async def test_软类两次一致照旧重生到预算耗尽(monkeypatch, tmp_path):
    """两次都说坏 = 残留是真的：照原路重生，修不动才退回源图交人工
    （与 ⑤c「两次一致才拦」同口径），并把末发产物的结论交给调用方如实报出。"""
    from app.publish.stages import preview

    verdicts = [{"clean": False, "garbled": True, "issues": "仍写作 Magnuetic"}] * 6
    edits, checks = _stub_clean_env(monkeypatch, verdicts)
    detail = {}
    out = await preview._clean_downloaded_preview(_raw(tmp_path), str(tmp_path),
                                                  detail=detail)
    assert out == "", "两次一致的软类不许放行"
    assert len(edits) == 3, "三发预算都要用掉"
    assert len(checks) == 6
    assert "Magnuetic" in detail["issues"]


# ---- 7) 品牌标识定点抹除救援：生图抹不掉实物上的第三方商标时的确定性兜底 ---------
# 2026-10-08 商品 1048494210610 实证：同一个包装盒的「382 TOYS」在 ⑦b 第 3 行、第 6 行
# 两次都是「三发生图纹丝不动」，而同款别的行偶尔能抹掉——纯随机，属确定性能力缺口。
# 救援链路 = 视觉按原文给整个标记图形的框 → 按周围底色整框填 → 复检必须通过。

def test_抹除图形标记按整框填底色(tmp_path):
    """refine=False 是给图形标记用的：白底方框要整块被底色吃掉，不能只抹里面的字。"""
    from app.publish import text_erase

    src = tmp_path / "mark.jpg"
    picture = Image.new("RGB", (400, 300), (0, 100, 200))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((250, 40, 350, 100), fill=(255, 255, 255))
    draw.text((262, 62), "382 TOYS", fill=(0, 0, 0))
    picture.save(src)
    result = text_erase.erase_boxes(str(src), [(250, 40, 350, 100)],
                                    out_path=str(tmp_path / "erased.jpg"), refine=False)
    assert result["ok"] is True
    with Image.open(tmp_path / "erased.jpg") as erased:
        pixel = erased.getpixel((300, 70))
    assert pixel[2] > 150 and pixel[0] < 60, "白框该被整块填成盒面底色"


def test_框外沿不是近纯色就放弃抹除(tmp_path):
    """蹭到商品主体/照片的框不抹：填出来是一块色疤，不如不抹（留白交重生或人工）。"""
    from app.publish import text_erase

    src = tmp_path / "busy.jpg"
    picture = Image.new("RGB", (400, 300), (0, 100, 200))
    draw = ImageDraw.Draw(picture)
    for x in range(200, 400, 10):                      # 半幅打上高对比条纹，外沿必杂
        draw.rectangle((x, 0, x + 4, 300), fill=(255, 255, 0))
    picture.save(src)
    result = text_erase.erase_boxes(str(src), [(250, 40, 350, 100)],
                                    out_path=str(tmp_path / "out.jpg"), refine=False)
    assert result["ok"] is False
    assert "近纯色" in result["why"]


@pytest.mark.asyncio
async def test_品牌标识三发抹不掉时走定点抹除救援(monkeypatch, tmp_path):
    """三发生图都抹不掉商标 → 不再烧第四发，改用确定性定点抹除；抹完复检通过就采用。"""
    from app.publish.stages import preview

    bad = {"clean": False, "brandMark": True, "brandMarkTexts": ["382 TOYS"],
           "issues": "包装盒正面右上角有品牌标识 382 TOYS"}
    edits, checks = _stub_clean_env(monkeypatch, [
        dict(bad), dict(bad), dict(bad),
        {"clean": True, "issues": ""}, {"clean": True, "issues": ""},
    ])
    seen = []

    async def fake_erase(path, texts, out_path=None):
        seen.append(list(texts))
        return {"ok": True}

    monkeypatch.setattr(preview.text_erase, "erase_marks", fake_erase)
    out = await preview._clean_downloaded_preview(_raw(tmp_path), str(tmp_path))
    assert out and os.path.isfile(out)
    assert seen == [["382 TOYS"]], "要拿质检给出的商标原文去定位"
    assert len(edits) == 3, "确定性兜底不该再烧第四发生图"


@pytest.mark.asyncio
async def test_定点抹除没做成仍退回源图交人工(monkeypatch, tmp_path):
    """抹除没执行（底色不纯/定位失败）时照原样退回，不新增失败面。"""
    from app.publish.stages import preview

    bad = {"clean": False, "brandMark": True, "brandMarkTexts": ["382 TOYS"],
           "issues": "包装盒上有品牌标识 382 TOYS"}
    _stub_clean_env(monkeypatch, [dict(bad), dict(bad), dict(bad)])

    async def fake_erase(path, texts, out_path=None):
        return {"ok": False, "why": "文字行背景非近纯色，填除会留色疤"}

    monkeypatch.setattr(preview.text_erase, "erase_marks", fake_erase)
    detail = {}
    out = await preview._clean_downloaded_preview(_raw(tmp_path), str(tmp_path),
                                                  detail=detail)
    assert out == ""
    assert "382 TOYS" in detail["issues"], "退回时仍要如实报末发产物的残留"
