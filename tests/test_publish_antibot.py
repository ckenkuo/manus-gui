"""阶段① 1688 反爬人工验证闸门的离线单测（mock 浏览器会话，不碰网络）。

测的是「检测判据 + 等人工 + 过关后继续」这段确定性逻辑：

- 正常详情页绝不能误判成被拦：误判的代价是把一次本来能跑通的提取卡成等人工，
  而人可能根本不在电脑前，600s 后整单失败；
- 文案判据必须受 hasData 约束：详情页里出现「验证」二字（店铺资质、买家评价）
  是常事，无条件看文案必然误命中；
- 命中时必须发出人工提示：这是整个功能的目的，提示没发出去等于静默卡住；
- 过关后返回 True，让 extract_product 知道要重读一次数据；
- 超时抛异常而不是静默返回：阶段① 要判失败才能被续跑重跑。
"""
import asyncio

import pytest

from app.publish import extract


class FakeSession:
    """按预设序列逐次返回反爬探测结果的假会话。

    eval_json 只会被闸门用来跑 _JS_ANTIBOT，故不解析 JS、直接按调用次数吐结果；
    序列用完后一直返回最后一个（模拟状态稳定不变）。
    """

    def __init__(self, probes: list):
        self.probes = probes
        self.calls = 0
        self.navigations: list = []
        self.brought_front = 0
        self.page = self

    async def eval_json(self, code: str, **kw) -> dict:
        i = min(self.calls, len(self.probes) - 1)
        self.calls += 1
        r = self.probes[i]
        if isinstance(r, Exception):
            raise r
        return r

    async def navigate(self, url: str, **kw) -> dict:
        self.navigations.append(url)
        return {"ok": True}

    async def bring_to_front(self) -> None:
        self.brought_front += 1


def _clean(**kw) -> dict:
    d = {"blocked": False, "kind": "", "detail": "", "hasData": True,
         "url": "https://detail.1688.com/offer/1.html", "title": "商品"}
    d.update(kw)
    return d


def _blocked(kind: str = "dom", detail: str = "baxia-dialog") -> dict:
    return {"blocked": True, "kind": kind, "detail": detail, "hasData": False,
            "url": "https://detail.1688.com/offer/1.html", "title": ""}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """把轮询间隔压到 0：单测不该真等 3 秒一轮。"""
    monkeypatch.setattr(extract, "_ANTIBOT_INTERVAL", 0.001)
    monkeypatch.setattr(extract, "_ANTIBOT_REMIND", 999.0)


@pytest.mark.asyncio
async def test_正常页面不误判(monkeypatch):
    """未被拦时必须直接返回 False，且不发人工提示、不动页签。"""
    sess = FakeSession([_clean()])
    hints: list = []
    r = await extract.wait_human_verify(sess, "u", on_manual=hints.append)
    assert r is False
    assert hints == []
    assert sess.brought_front == 0


@pytest.mark.asyncio
async def test_命中即提示人工并等到过关(monkeypatch):
    """滑块弹出 → 发提示 + 页签提前台 → 人拖完 → 返回 True 让调用方重读数据。"""
    sess = FakeSession([_blocked(), _blocked(), _clean()])
    hints: list = []
    r = await extract.wait_human_verify(sess, "u", on_manual=hints.append, timeout=5)
    assert r is True
    assert len(hints) == 1
    assert "滑块" in hints[0] and "手动" in hints[0]
    assert sess.brought_front == 1


@pytest.mark.asyncio
async def test_异步提示回调也支持():
    """service 层传的是 async 闭包（要 await emit），闭包必须被真正等待。"""
    sess = FakeSession([_blocked(), _clean()])
    hints: list = []

    async def _on_manual(msg: str) -> None:
        await asyncio.sleep(0)
        hints.append(msg)

    r = await extract.wait_human_verify(sess, "u", on_manual=_on_manual, timeout=5)
    assert r is True
    assert len(hints) == 1


@pytest.mark.asyncio
async def test_提示回调异常不影响等待():
    """提示是辅助路径：回调炸掉也要照常等人工过关（本项目 best-effort 惯例）。"""
    sess = FakeSession([_blocked(), _clean()])

    def _boom(msg: str) -> None:
        raise RuntimeError("UI 掉线")

    assert await extract.wait_human_verify(sess, "u", on_manual=_boom, timeout=5) is True


@pytest.mark.asyncio
async def test_过关但停在中转页会导回详情页():
    """punish 过关后不一定自动跳回详情页：blocked 已解除但无数据时要重新导航。"""
    sess = FakeSession([
        _blocked("url", "punish"),
        _clean(hasData=False, url="https://sec.1688.com/ok"),  # 过关但没数据
        _clean(),
    ])
    r = await extract.wait_human_verify(
        sess, "https://detail.1688.com/offer/1.html", timeout=5)
    assert r is True
    assert sess.navigations == ["https://detail.1688.com/offer/1.html"]


@pytest.mark.asyncio
async def test_超时抛异常():
    """一直没人处理要抛，让阶段① 判失败进状态文件，人工过关后可原命令重跑。"""
    sess = FakeSession([_blocked()])
    with pytest.raises(RuntimeError, match="超时"):
        await extract.wait_human_verify(sess, "u", timeout=0.05)


@pytest.mark.asyncio
async def test_检测执行失败按未拦截处理():
    """检测本身报错（导航中途上下文销毁）不下结论，交回调用方照常等数据。"""
    sess = FakeSession([RuntimeError("Execution context was destroyed")])
    assert await extract.wait_human_verify(sess, "u") is False


# ---- 判据本身（在真实 JS 语义下用 Python 复刻同一套规则做断言）--------------
# JS 没法离线跑，但判据的两条硬规则可以在这里钉住，防止日后把它们改松：
#   1. 文案判据必须受 hasData 约束
#   2. 浮层判据只认有尺寸的元素
def test_JS判据保留hasData约束与尺寸判定():
    js = extract._JS_ANTIBOT
    # 文案分支必须写在 !hasData 里面
    assert "if (!hasData && document.body)" in js
    # 浮层可见性按 rect 尺寸判，不是靠 display 字符串或 offsetHeight
    assert "r.width > 20 && r.height > 20" in js
    # 三条判据齐全
    for sel in ("baxia-dialog", "nc_1_wrapper", "punish"):
        assert sel in js


def test_提示文案按命中类型区分():
    """人要知道该做什么：登录页/滑块/整页拦截三种处置动作不同。"""
    assert "登录" in extract._antibot_hint(
        {"kind": "url", "detail": "login.1688.com"})
    assert "滑块" in extract._antibot_hint({"kind": "dom", "detail": "baxia-dialog"})
    assert "验证页" in extract._antibot_hint({"kind": "url", "detail": "punish"})
    assert "文案" in extract._antibot_hint({"kind": "text", "detail": "安全验证"})
