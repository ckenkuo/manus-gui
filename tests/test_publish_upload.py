"""图片直传（app/publish/upload.py）的离线单测：不碰网络、不碰真站。

为什么值得单测：直传的三步里，两步的入参是抓包实测得来的字符串拼接
（getSign / cosDxmCallBack 的 form body），字段名或转义写错时真站表现是
「上传成功但空间里找不到图」——错在别处、极难定位。这里用假的 session 与
假的 curl 把三步的调用序列和入参固化下来，改坏了单测立刻红。

不覆盖的部分：COS 是否真的收到字节、图床是否真的登记——那要真账号，属真站验证。
"""
import os

import pytest
from PIL import Image

from app.publish.images import CLOTH_MIN_H, CLOTH_MIN_W

from app.publish import upload as up


class FakeSession:
    """记录 eval_json 收到的 JS，并按预设返回值应答。

    直传的第 1、3 步都是页面内 fetch，故只需假冒 eval_json 一个方法。
    """

    def __init__(self, sign_resp: dict, cb_resp: dict):
        self.sign_resp = sign_resp
        self.cb_resp = cb_resp
        self.calls = []

    async def eval_json(self, code: str, timeout: int = 90, retries: int = 3) -> dict:
        self.calls.append(code)
        if "getSign.json" in code:
            return self.sign_resp
        if "cosDxmCallBack.json" in code:
            return self.cb_resp
        raise AssertionError(f"未预期的 JS 调用：{code[:80]}")


class FakeProc:
    def __init__(self, rc: int = 0, stderr: bytes = b""):
        self.returncode = rc
        self.stderr = stderr


def _img(path: str, w: int = CLOTH_MIN_W + 1, h: int = CLOTH_MIN_H + 1) -> str:
    """造一张【尺寸达标】的测试图。

    默认给 1341×1786（比服装下限大 1px）：upload_image 的 precheck 按【严格大于】判
    （平台把 =1340×1785 也拦下，见 images.check_cloth_size 的 strict 参数），拿贴线图
    或占位小图会连第一步取签名都走不到。本文件测的是直传三步协议，闸门本身由
    test_publish_upload_sizegate.py 覆盖。
    """
    Image.new("RGB", (w, h), (10, 20, 30)).save(path)
    return path


@pytest.fixture
def curl_spy(monkeypatch):
    """替掉 _curl_put，记录 PUT 入参，避免真发请求。"""
    seen = {}

    async def fake(put_url, file_path, sign, ctype):
        seen.update(url=put_url, path=file_path, sign=sign, ctype=ctype)
        return FakeProc(seen.pop("_rc", 0))

    monkeypatch.setattr(up, "_curl_put", fake)
    return seen


def test_三步顺序与最终URL(tmp_path, curl_spy):
    """成功路径：getSign → PUT → callback，最终 URL = 图床前缀 + fileId。"""
    src = _img(str(tmp_path / "01.jpg"))
    s = FakeSession(
        {"code": 0, "sign": "SIGN-ABC", "url": "//cos.example.com/put/01.jpg",
         "fileId": "/5153348-/01.jpg"},
        {"code": 0, "msg": "ok"},
    )
    import asyncio
    r = asyncio.run(up.upload_image(s, src, full_cid="5153348-"))

    assert r["status"] == "ok"
    assert r["url"] == up.WXALBUM_HOST + "/5153348-/01.jpg"
    # 调用序列必须是先签名后回调
    assert "getSign.json" in s.calls[0] and "cosDxmCallBack.json" in s.calls[1]
    # 协议相对 URL 必须补成 https（curl 不认 //host/path）
    assert curl_spy["url"] == "https://cos.example.com/put/01.jpg"
    assert curl_spy["sign"] == "SIGN-ABC"


def test_回调body带全部实测字段(tmp_path, curl_spy):
    """cosDxmCallBack 的 form 字段是抓包实测的，少一个就登记不进图床。"""
    src = _img(str(tmp_path / "02.png"))
    size = os.path.getsize(src)
    s = FakeSession(
        {"code": 0, "sign": "S", "url": "https://c/p", "fileId": "/cid/02.png"},
        {"code": 0},
    )
    import asyncio
    asyncio.run(up.upload_image(s, src, full_cid="9999-"))

    cb_js = s.calls[1]
    for field in ("bucket=wxalbum", "fullCid=", "fileId=", "fileName=",
                  "isNeedTree=0", "fileSize="):
        assert field in cb_js, f"回调 body 缺字段 {field}"
    assert str(size) in cb_js, "fileSize 必须是真实字节数"
    # png 要给对 Content-Type
    assert curl_spy["ctype"] == "image/png"


def test_签名缺失即中止不发PUT(tmp_path, monkeypatch):
    """getSign 没返回 sign/url 时必须停在 getSign 阶段，不能盲发 PUT。"""
    src = _img(str(tmp_path / "03.jpg"))
    called = {"n": 0}

    async def fake(*a, **k):
        called["n"] += 1
        return FakeProc(0)

    monkeypatch.setattr(up, "_curl_put", fake)
    s = FakeSession({"code": 1, "msg": "未登录"}, {"code": 0})
    import asyncio
    r = asyncio.run(up.upload_image(s, src, full_cid="c-"))

    assert r["status"] == "error" and r["stage"] == "getSign"
    assert called["n"] == 0, "签名失败还发了 PUT"


def test_PUT失败不发回调(tmp_path, monkeypatch):
    """字节没上去就登记，会在图床留下坏记录，故 PUT 失败必须中止。"""
    src = _img(str(tmp_path / "04.jpg"))

    async def fake(*a, **k):
        return FakeProc(7, b"curl: (28) timeout")

    monkeypatch.setattr(up, "_curl_put", fake)
    s = FakeSession({"code": 0, "sign": "S", "url": "https://c/p", "fileId": "/c/04.jpg"},
                    {"code": 0})
    import asyncio
    r = asyncio.run(up.upload_image(s, src, full_cid="c-"))

    assert r["status"] == "error" and r["stage"] == "cos-put"
    assert len(s.calls) == 1, "PUT 失败后仍发了回调"


def test_文件不存在直接返回(tmp_path):
    import asyncio
    r = asyncio.run(up.upload_image(FakeSession({}, {}), str(tmp_path / "nope.jpg"),
                                    full_cid="c-"))
    assert r["status"] == "error" and r["stage"] == "precheck"


def test_upload_many按给定顺序串行(tmp_path, monkeypatch):
    """阶段⑦ 依赖上传顺序决定行内图片顺序，故必须串行且保序。"""
    names = ["01.jpg", "02.jpg", "03.jpg"]
    paths = [_img(str(tmp_path / n)) for n in names]
    order = []

    class Seq(FakeSession):
        def __init__(self):
            super().__init__({}, {"code": 0})
            self.i = 0

        async def eval_json(self, code, timeout=90, retries=3):
            if "getSign.json" in code:
                self.i += 1
                return {"code": 0, "sign": "S", "url": "https://c/p",
                        "fileId": f"/c/{self.i:02d}.jpg"}
            return {"code": 0}

    async def fake(put_url, file_path, sign, ctype):
        order.append(os.path.basename(file_path))
        return FakeProc(0)

    import asyncio
    monkeypatch.setattr(up, "_curl_put", fake)
    r = asyncio.run(up.upload_many(Seq(), paths, full_cid="c-"))

    assert r["status"] == "ok" and r["okCount"] == 3
    assert order == names, f"上传顺序被打乱：{order}"


def test_resolve_full_cid读环境变量(monkeypatch):
    monkeypatch.setenv("DXM_FULL_CID", "7777-")
    assert up.resolve_full_cid() == "7777-"


def test_缺full_cid时报错指向配置(monkeypatch):
    """不留内置兜底值：报错必须明确指向「你没配」，而不是后续莫名其妙的失败。"""
    monkeypatch.delenv("DXM_FULL_CID", raising=False)
    # 本机 config.toml 里可能已配好 full_cid，故两种结果都算通过：
    # 配了就返回非空字符串，没配就抛错且错误信息里点名 full_cid。
    try:
        cid = up.resolve_full_cid()
        assert isinstance(cid, str) and cid
    except RuntimeError as e:
        assert "full_cid" in str(e)
