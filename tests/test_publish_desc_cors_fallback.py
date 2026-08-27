# -*- coding: utf-8 -*-
"""① 描述长图：detailUrl 跨域时的直连兜底单测（不连 CDP、不开浏览器、不出网）。

2026-08-26 实测 734821729953：页面内 fetch 详情接口抛 TypeError: Failed to fetch，
描述图 0 张。根因不是延迟——该商品的 detailUrl 是老形态
`itemcdn.tmall.com/desc/icoss!<offerId>!<x>?var=desc`，服务端不返
access-control-allow-origin，浏览器读不到跨域响应体，重试多少次都一样；同批次另一
商品的新形态 `/1688offer/icoss<hash>` 带 `ACAO: *`，描述图 16 张。老端点不认来源，
Python 裸请求就是 200，故兜底换协议栈直连。

这里锁三条不变量：跨域抛错要落到直连、直连结果必须进 descImages 落盘、
以及两条路径抠图规则一致（同一段响应体抠出同一批图）。
"""
import re

import pytest

from app.publish import extract as E


# 老端点的真实响应片段（GB18030 中文 + 转义斜杠的图片 URL），按实测结构裁剪
_DESC_BODY = (
    'var offer_details={"content":"<p>产品参数：纯棉套装</p>'
    '<img src=\\"https://cbu01.alicdn.com/img/ibank/O1CN01BY11DP_a.jpg\\">'
    '<img src=\\"https://cbu01.alicdn.com/img/ibank/O1CN01C6eiTK_b.png\\">'
    '<img src=\\"https://cbu01.alicdn.com/img/ibank/O1CN01BY11DP_a.jpg\\">"};'
)
_EXPECT = [
    "https://cbu01.alicdn.com/img/ibank/O1CN01BY11DP_a.jpg",
    "https://cbu01.alicdn.com/img/ibank/O1CN01C6eiTK_b.png",
]


class _FakeResp:
    """requests.get 的最小替身：只提供 content 与 raise_for_status。"""

    def __init__(self, body: str, status: int = 200):
        self.content = body.encode("gb18030")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_直连抠图去重且保序(monkeypatch):
    """直连要按接口返回顺序去重，重复图只留第一次出现的位置。"""
    monkeypatch.setattr(
        "requests.get", lambda url, **kw: _FakeResp(_DESC_BODY), raising=False
    )
    got = E._fetch_desc_imgs_direct("https://itemcdn.tmall.com/desc/icoss!1!2?var=desc")
    assert got == _EXPECT


def test_直连带浏览器头(monkeypatch):
    """必须带 UA/Referer：CDN 侧 bot 拦截是本项目反复踩的坑。"""
    seen = {}

    def _fake_get(url, **kw):
        seen.update(kw.get("headers") or {})
        return _FakeResp(_DESC_BODY)

    monkeypatch.setattr("requests.get", _fake_get, raising=False)
    E._fetch_desc_imgs_direct("https://itemcdn.tmall.com/desc/icoss!1!2?var=desc")
    assert "User-Agent" in seen and "Referer" in seen


def test_两条路径抠图规则一致():
    """JS 里那条正则与 _RE_DESC_IMG 必须抠出同一批图，否则换路径结果就变。

    直接把 _JS_DETAIL 里的正则字面量取出来转成 Python 正则跑同一段响应体。
    """
    m = re.search(r"t\.match\(/(.+?)/gi\)", E._JS_DETAIL)
    assert m, "JS 里的图片正则字面量没找到，_JS_DETAIL 结构变了要同步本测试"
    js_pat = m.group(1).replace(r"\/", "/")
    js_hits = list(dict.fromkeys(re.findall(js_pat, _DESC_BODY, re.I)))
    py_hits = list(dict.fromkeys(E._RE_DESC_IMG.findall(_DESC_BODY)))
    # JS 正则带捕获组（扩展名），findall 只回组内容，故只比 Python 侧完整 URL 的条数
    assert len(js_hits) == len(py_hits) == len(_EXPECT)
    assert py_hits == _EXPECT


def test_直连全失败不抛给主流程(monkeypatch):
    """直连也挂时只该 warning + 描述图为空，不能让阶段① 失败（best-effort 约定）。"""
    def _boom(url, **kw):
        raise ConnectionError("连接重置")

    monkeypatch.setattr("requests.get", _boom, raising=False)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="直连重试"):
        E._fetch_desc_imgs_direct("https://itemcdn.tmall.com/desc/icoss!1!2?var=desc")
