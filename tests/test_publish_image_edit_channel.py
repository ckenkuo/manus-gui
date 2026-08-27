# -*- coding: utf-8 -*-
"""gpt-image-2 /images/edits 请求形状与坏渠道重试的单测（2026-08-26 实测契约）。

这批用例锁的是【线上探测出来的三条硬事实】，改动 edit_image 时它们必须继续通过：
  1. 请求里绝不能带 input_fidelity —— 带了必然 400，一张也出不来；
  2. 图必须以 multipart 文件（files 的 image 字段）传，不能改成 image_url / data URL；
  3. 「坏渠道拒收 multipart」要重试换渠道，而参数错之类的其它错误不许重试。
全部 mock 掉 _multipart_post，不打网络。
"""
import pytest

from app.publish import images


# 坏渠道的真实响应（HTTP 500 + 这个 code/文案，实测原样抄回）
BAD_CHANNEL_RESP = {
    "error": {
        "message": ("gpt-image-2 does not accept multipart file upload; please "
                    "provide a public URL via 'image_url' form field instead"),
        "type": "packy_api_error",
        "param": "",
        "code": "convert_request_failed",
    }
}
# 参数错的真实响应：这种重试多少次都是同样的错，不许重试
FIDELITY_RESP = {
    "error": {
        "message": "The model 'gpt-image-2' does not support the 'input_fidelity' parameter.",
        "type": "packy_image_generation_user_error",
        "param": "input_fidelity",
        "code": "invalid_input_fidelity_model",
    }
}
OK_RESP = {"data": [{"b64_json": "aGVsbG8="}]}


@pytest.fixture
def 假图(tmp_path):
    from PIL import Image
    p = tmp_path / "src.jpg"
    Image.new("RGB", (790, 1197), "white").save(p)
    return str(p)


def _stub(monkeypatch, responses: list, calls: list):
    """把 _multipart_post 换成按序返回 responses 的假实现，并记录每次入参。"""
    def fake(url, fields, files, timeout=280):
        calls.append({"url": url, "fields": dict(fields), "files": dict(files)})
        return responses[min(len(calls) - 1, len(responses) - 1)]
    monkeypatch.setattr(images, "_multipart_post", fake)


def test_请求不带input_fidelity(monkeypatch, 假图, tmp_path):
    """带 input_fidelity 会被服务端 400 打回，字段必须已从请求里去掉。"""
    calls = []
    _stub(monkeypatch, [OK_RESP], calls)
    monkeypatch.setattr(images, "_save_result", lambda r, o: o)
    images.edit_image(假图, out_path=str(tmp_path / "out.png"), do_compress=False)
    assert calls, "没有发出请求"
    assert "input_fidelity" not in calls[0]["fields"]


def test_图走multipart文件字段而不是image_url(monkeypatch, 假图, tmp_path):
    """网关只认 multipart 的 image 文件：只传 image_url 报缺 image、同传报未知参数。"""
    calls = []
    _stub(monkeypatch, [OK_RESP], calls)
    monkeypatch.setattr(images, "_save_result", lambda r, o: o)
    images.edit_image(假图, out_path=str(tmp_path / "out.png"), do_compress=False)
    c = calls[0]
    assert c["files"].get("image") == 假图
    assert "image_url" not in c["fields"] and "image_url" not in c["files"]
    assert "image" not in c["fields"], "image 不能以字符串字段形式传（会报 invalid_type）"
    assert c["url"].endswith("/images/edits")


def test_坏渠道重试后成功(monkeypatch, 假图, tmp_path):
    """随机分流到坏渠道时，重试应换到好渠道并正常出图。"""
    calls = []
    _stub(monkeypatch, [BAD_CHANNEL_RESP, BAD_CHANNEL_RESP, OK_RESP], calls)
    monkeypatch.setattr(images, "_save_result", lambda r, o: o)
    r = images.edit_image(假图, out_path=str(tmp_path / "out.png"), do_compress=False)
    assert r["status"] == "ok"
    assert len(calls) == 3, f"应重试到第 3 发才成功，实际发了 {len(calls)} 次"


def test_坏渠道重试有上限(monkeypatch, 假图, tmp_path):
    """全落坏渠道时按上限停手并抛错，不能无限重试把阶段拖死。"""
    calls = []
    _stub(monkeypatch, [BAD_CHANNEL_RESP], calls)
    with pytest.raises(RuntimeError, match="响应中没有图片"):
        images.edit_image(假图, out_path=str(tmp_path / "out.png"), do_compress=False)
    assert len(calls) == images.EDIT_BAD_CHANNEL_RETRY


def test_参数错不重试(monkeypatch, 假图, tmp_path):
    """参数错重试多少次都是同样的错，只会白等还把真错误埋掉，必须一发就抛。"""
    calls = []
    _stub(monkeypatch, [FIDELITY_RESP], calls)
    with pytest.raises(RuntimeError, match="响应中没有图片"):
        images.edit_image(假图, out_path=str(tmp_path / "out.png"), do_compress=False)
    assert len(calls) == 1, f"参数错不该重试，实际发了 {len(calls)} 次"


def test_坏渠道判据只认那个签名():
    """判据要同时看 code 与文案，别把别的 500 也当坏渠道重试。"""
    assert images._is_bad_channel(BAD_CHANNEL_RESP)
    assert not images._is_bad_channel(FIDELITY_RESP)
    assert not images._is_bad_channel(OK_RESP)
    assert not images._is_bad_channel(
        {"error": {"code": "convert_request_failed", "message": "其它转换失败"}})


def test_默认提示词点明中文标点也要去掉():
    """只说「中文文字」时模型会留下『』这类中日韩标点，被质检判残留中文后退回原图，
    那张图于是又撞回 1340x1785 尺寸闸门（2026-08-26 实测）。两条默认提示词都要覆盖。"""
    for p in (images.DEFAULT_CLEAN_PROMPT, images.DEFAULT_TRANSLATE_PROMPT):
        assert "标点" in p, f"提示词没提标点：{p}"
        assert "『" in p and "』" in p, f"提示词没给出书名号示例：{p}"


def test_plan_clean的定制提示词也覆盖标点():
    """主图侧走 vision.plan_clean 自己拼的提示词，别只修 images 的默认值漏了这边。"""
    from app.publish import vision
    info = {"complianceNotes": {"files": [
        {"file": "main-01.jpg", "chinese": True, "clean": False}]}}
    import os as _os
    import tempfile
    d = tempfile.mkdtemp()
    open(_os.path.join(d, "main-01.jpg"), "wb").close()
    plan = vision.plan_clean(info, d)
    items = plan.get("items") or []
    assert items, f"没挑出待清理项：{plan}"
    assert "标点" in items[0]["prompt"], items[0]["prompt"]


# ---- 链路抖动重试（并发下网络不稳，聚合渠道会偶发连接失败）------------------

def _stub_raising(monkeypatch, outcomes: list, calls: list):
    """按序回放 outcomes：异常实例就 raise，dict 就返回。记录调用次数。"""
    def fake(url, fields, files, timeout=280):
        calls.append(1)
        o = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(o, BaseException):
            raise o
        return o
    monkeypatch.setattr(images, "_multipart_post", fake)
    # 退避 sleep 在单测里直接跳过，否则跑一次要等十几秒
    monkeypatch.setattr(images.time, "sleep", lambda *_: None, raising=False)


@pytest.mark.parametrize("rc", [7, 28, 35, 52, 56])
def test_curl瞬时退出码判为可重试(rc):
    """连不上/超时/TLS 握手失败/连接重置这些码重发一次很可能就过。"""
    assert rc in images._CURL_TRANSIENT_RC


@pytest.mark.parametrize("rc", [3, 26])
def test_curl确定性退出码不判可重试(rc):
    """3 URL 格式错、26 读本地文件失败，重试多少次都是同样的错。"""
    assert rc not in images._CURL_TRANSIENT_RC


def test_链路抖动重试后成功(monkeypatch, 假图, tmp_path):
    """并发下偶发连接失败，重试应把它救回来而不是让这张图直接失败。"""
    calls = []
    _stub_raising(monkeypatch, [
        images.TransientNetError("curl 失败 rc=56: Connection was reset"),
        images.TransientNetError("curl 失败 rc=28: Operation timed out"),
        OK_RESP,
    ], calls)
    monkeypatch.setattr(images, "_save_result", lambda r, o: o)
    r = images.edit_image(假图, out_path=str(tmp_path / "o.png"), do_compress=False)
    assert r["status"] == "ok"
    assert len(calls) == 3


def test_抖动与坏渠道共用同一份重试预算(monkeypatch, 假图, tmp_path):
    """两者各记一套次数会叠乘成最坏 16 发、十几分钟，把阶段拖死。"""
    calls = []
    _stub_raising(monkeypatch, [
        BAD_CHANNEL_RESP,
        images.TransientNetError("curl 失败 rc=56: reset"),
        BAD_CHANNEL_RESP,
        images.TransientNetError("curl 失败 rc=56: reset"),
        OK_RESP,          # 第 5 发才好，但预算只有 4，够不到
    ], calls)
    with pytest.raises(images.TransientNetError):
        images.edit_image(假图, out_path=str(tmp_path / "o.png"), do_compress=False)
    assert len(calls) == images.EDIT_BAD_CHANNEL_RETRY, \
        f"总发数应被夹在 {images.EDIT_BAD_CHANNEL_RETRY}，实际 {len(calls)}"


def test_确定性网络错误不重试(monkeypatch, 假图, tmp_path):
    """非白名单的 curl 失败（如读不到本地图）是确定性错，重试只会白等。"""
    calls = []
    _stub_raising(monkeypatch, [
        RuntimeError("curl 失败 rc=26: Failed to open/read local data"),
    ], calls)
    with pytest.raises(RuntimeError, match="rc=26"):
        images.edit_image(假图, out_path=str(tmp_path / "o.png"), do_compress=False)
    assert len(calls) == 1, f"确定性错不该重试，实际发了 {len(calls)} 次"


def test_被拦截的非JSON响应不重试(monkeypatch, 假图, tmp_path):
    """Cloudflare 拦截返 HTML，是确定性失败（要换请求方式），不是抖动。"""
    calls = []
    _stub_raising(monkeypatch, [RuntimeError("API 返回非 JSON（可能被拦截）: <html>")], calls)
    with pytest.raises(RuntimeError, match="非 JSON"):
        images.edit_image(假图, out_path=str(tmp_path / "o.png"), do_compress=False)
    assert len(calls) == 1


def test_全程抖动最终抛异常而不是返回None(monkeypatch, 假图, tmp_path):
    """一次都没拿到响应时必须抛错：返回 None 会让 _save_result 报个莫名其妙的错。"""
    calls = []
    _stub_raising(monkeypatch, [images.TransientNetError("curl 失败 rc=7: couldn't connect")], calls)
    with pytest.raises(images.TransientNetError, match="rc=7"):
        images.edit_image(假图, out_path=str(tmp_path / "o.png"), do_compress=False)
    assert len(calls) == images.EDIT_BAD_CHANNEL_RETRY


# ---- 出图尺寸直接对齐闸门（省掉 compress 二次插值）--------------------------

@pytest.mark.parametrize("w,h", [
    (749, 513), (790, 684), (790, 702), (790, 1013), (790, 1197), (1276, 1276),
])
def test_gate_size真实样本都不低于闸门(w, h):
    """实测这 6 种真实描述图/素材图尺寸，原先一律要 compress 放大 1.31~1.74 倍。"""
    g = images._gate_size(w, h)
    assert g, f"{w}x{h} 应能算出定制档"
    tw, th = (int(x) for x in g.split("x"))
    assert tw >= images.CLOTH_MIN_W and th >= images.CLOTH_MIN_H, g


@pytest.mark.parametrize("w,h", [(749, 513), (790, 684), (790, 1197)])
def test_gate_size保持原图比例(w, h):
    """描述图是长图混排，比例被改就变形；3:4 是 SKC 的规则、由 fit_34 负责。"""
    tw, th = (int(x) for x in images._gate_size(w, h).split("x"))
    assert abs(tw / th - w / h) < 0.01, f"{w}x{h} -> {tw}x{th} 比例跑偏"


@pytest.mark.parametrize("w,h", [(749, 513), (790, 684), (790, 1197), (1276, 1276)])
def test_gate_size满足服务端四条约束(w, h):
    """16 的倍数、比例 <=3:1、长边 <=MAX_DIM、像素在区间内——任一条不满足服务端就报错。"""
    tw, th = (int(x) for x in images._gate_size(w, h).split("x"))
    assert tw % images.SIZE_MULTIPLE == 0 and th % images.SIZE_MULTIPLE == 0
    assert max(tw / th, th / tw) <= images.SIZE_MAX_RATIO
    assert max(tw, th) <= images.MAX_DIM
    assert images.SIZE_MIN_PIXELS <= tw * th <= images.SIZE_MAX_PIXELS


@pytest.mark.parametrize("w,h", [(790, 4000), (500, 2000), (4000, 4000)])
def test_gate_size放不下时退回None(w, h):
    """超长图放大后会破长边上限，必须退回档位挑选让 compress 兜（它有封底保护）。"""
    assert images._gate_size(w, h) is None


def test_pick_size_for_file默认走闸门档(tmp_path):
    """默认 gate_aware=True：出图即达标，不再依赖 compress 插值放大。"""
    from PIL import Image
    p = tmp_path / "a.jpg"
    Image.new("RGB", (790, 684), "white").save(p)
    s = images.pick_size_for_file(str(p))
    tw, th = (int(x) for x in s.split("x"))
    assert tw >= images.CLOTH_MIN_W and th >= images.CLOTH_MIN_H, s


def test_pick_size_for_file可关掉闸门档(tmp_path):
    """gate_aware=False 保留纯档位行为，给不过服装闸门的调用方留口子。"""
    from PIL import Image
    p = tmp_path / "a.jpg"
    Image.new("RGB", (790, 684), "white").save(p)
    assert images.pick_size_for_file(str(p), gate_aware=False) in images.ALLOWED_SIZES
