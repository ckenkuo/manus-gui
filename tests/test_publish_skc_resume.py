"""阶段⑦ 续跑跳过判定的离线单测（_skc_row_matches，纯函数）。

【为什么值得单测】阶段⑤~⑬ 的表单成果一重载就丢，续跑很常见；而原先主路径拿到
视觉选图就无条件整行换，上一轮换好的行会被再换一遍（6 张图约一分钟白工，还要白担
一次页面交互失败的风险）。

【这里钉住的核心取向：没有 fileId 清单就不判完成】宽判据（数量对 + 都在店小秘图床
+ 尺寸达标）认不出「6 张达标图但内容不是这一批」，误判会把错图静默留在页面上。
白跑一轮只是慢，留错图是错——所以宁可白跑。
"""
from app.publish.pipeline import _skc_row_matches
from app.publish.images import CLOTH_MIN_H, CLOTH_MIN_W


def _state(n: int, host: str = "wxalbum-10001658-file.dianxiaomi.com",
           w: int = CLOTH_MIN_W, h: int = CLOTH_MIN_H, prefix: str = "new") -> dict:
    urls = [f"https://{host}/wxalbum/2525332/{prefix}-{i:02d}.jpg" for i in range(1, n + 1)]
    return {"count": n, "srcs": [u[-46:] for u in urls], "urls": urls,
            "sizes": [[w, h]] * n,
            "tooSmall": [] if (w >= CLOTH_MIN_W and h >= CLOTH_MIN_H)
                        else [{"idx": i} for i in range(n)],
            "foreign": [u for u in urls if "dianxiaomi.com" not in u]}


def _ids(n: int, prefix: str = "new") -> list:
    return [f"wxalbum/2525332/{prefix}-{i:02d}.jpg" for i in range(1, n + 1)]


def test_全中判完成():
    r = _skc_row_matches(_state(6), _ids(6), 6)
    assert r["done"] is True, r


def test_无清单一律不判完成():
    """旧状态文件没有清单时不能靠宽判据放行——认不出内容是否为本批。"""
    r = _skc_row_matches(_state(6), [], 6)
    assert r["done"] is False
    assert "fileId" in r["reason"]


def test_数量不符不判完成():
    r = _skc_row_matches(_state(5), _ids(6), 6)
    assert r["done"] is False and "数量不符" in r["reason"]


def test_有外链图不判完成():
    """还挂着 1688 外链，说明这一行没被本管线换过。"""
    st = _state(6, host="cbu01.alicdn.com")
    r = _skc_row_matches(st, _ids(6), 6)
    assert r["done"] is False and "外链" in r["reason"]


def test_有破线图不判完成():
    """尺寸不达标必须重做：保存时会被静默弹回。"""
    st = _state(6, w=1000, h=1000)
    r = _skc_row_matches(st, _ids(6), 6)
    assert r["done"] is False and "低于" in r["reason"]


def test_内容变了不判完成():
    """张数、托管、尺寸全对，但页面上的图不是清单里那批——这正是宽判据认不出的情况。"""
    st = _state(6, prefix="other")          # 页面上是 other-01..06
    r = _skc_row_matches(st, _ids(6), 6)    # 清单记的是 new-01..06
    assert r["done"] is False and "内容已变" in r["reason"]


def test_部分命中也不判完成():
    """只要有一张不在页面上就得重换——半批图等于没换。"""
    st = _state(6)
    ids = _ids(5) + ["wxalbum/2525332/missing-99.jpg"]
    r = _skc_row_matches(st, ids, 6)
    assert r["done"] is False and "内容已变" in r["reason"]
