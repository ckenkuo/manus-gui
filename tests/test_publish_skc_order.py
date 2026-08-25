"""阶段⑦ SKC 整行替换的顺序不变式单测（离线，假 session）。

【为什么必须单测这个】这段顺序逻辑有三条互相拉扯的真站约束，改错的表现都是静默的：
  - 行内不能为空：删空后建不了行绑定，图会挂到【别的颜色行】去（2026-08-18 实测）
  - 每行不能超 10 张：超了那张静默挂不进去
  - 每行不能少于 3 张：保存时被拦，页面只有区块变红
2026-08-24 把「先全挂再全删 + 预删腾位」改成「一挂一删交替」，正是靠穷举这些组合
发现朴素交替在 old=10 时会触顶（第一轮先挂就 11 张），才补上「行满则先删后挂」。

这里用假 session 记录每一步操作，重演出行内图数的完整轨迹，对轨迹断言不变式——
比只断言最终结果强得多：预删那版的最终结果也是对的，坏在中间态。
"""
import os

import pytest
from PIL import Image

from app.publish import pipeline as pl
from app.publish.images import CLOTH_MIN_H, CLOTH_MIN_W


class FakeRow:
    """模拟一个 SKC 颜色行：维护图片 id 列表，按真站语义响应各步操作。

    真站行为的三条要点（都在 pipeline 的 JS 常量里有对应实测注释）：
      - 删除固定作用于第 1 张（最老的）
      - 挂载把新图追加到行末
      - 行满 10 张时挂载【静默失败】，不报错但数量不增（这正是要防的坑）
    """

    def __init__(self, old_count: int, max_images: int = 10):
        self.imgs = [f"old-{i:02d}" for i in range(1, old_count + 1)]
        self.max_images = max_images
        self.trace = [len(self.imgs)]      # 每次操作后的行内图数
        self.ops = []                      # 操作序列，如 ["del", "add:new-01", ...]
        self.overflow_attempts = 0         # 满行时试图挂图的次数（应恒为 0）

    def delete_first(self) -> dict:
        before = len(self.imgs)
        if not self.imgs:
            return {"err": "行内已无图片"}
        self.imgs.pop(0)
        self.ops.append("del")
        self.trace.append(len(self.imgs))
        return {"deleted": True, "before": before, "after": len(self.imgs)}

    def attach(self, fid: str) -> None:
        if len(self.imgs) >= self.max_images:
            # 真站在这里静默失败：数量不增、不报错
            self.overflow_attempts += 1
            self.ops.append(f"add-FAILED:{fid}")
            self.trace.append(len(self.imgs))
            return
        self.imgs.append(fid)
        self.ops.append(f"add:{fid}")
        self.trace.append(len(self.imgs))

    def state(self) -> dict:
        return {"count": len(self.imgs), "srcs": list(self.imgs),
                "urls": [f"https://wxalbum.dianxiaomi.com/{i}" for i in self.imgs],
                "sizes": [[CLOTH_MIN_W, CLOTH_MIN_H] for _ in self.imgs]}


class FakeSession:
    """把 pipeline 用到的 eval_json / cdp 接到 FakeRow 上。

    只按 JS 片段的特征字符串分派——与 test_publish_upload.py 的 FakeSession 同一路子。
    """

    def __init__(self, row: FakeRow):
        self.row = row

    async def eval_json(self, code: str, timeout: int = 90, retries: int = 3) -> dict:
        if "icon_delete" in code:
            return self.row.delete_first()
        if "naturalWidth" in code:
            return self.row.state()
        raise AssertionError(f"未预期的 JS 调用：{code[:100]}")

    async def cdp(self, method: str, params: dict) -> dict:
        return {}


@pytest.fixture
def stub_steps(monkeypatch):
    """替掉真正碰网络/页面的三步，只留顺序逻辑。"""
    async def fake_upload(session, path, full_cid=None, **kw):
        return {"status": "ok", "fileId": "wxalbum/" + os.path.basename(path),
                "url": "https://x/" + os.path.basename(path)}

    async def fake_open_space(session, row_keyword):
        return {"opened": True}

    async def fake_pick(session, file_id):
        # 真站语义：从空间弹窗选中图 → 追加到行末
        session.row.attach(file_id.rsplit("/", 1)[-1])
        return {"picked": True}

    async def no_sleep(_seconds):
        """删图后的 0.4s 等待是真站 DOM 重排需要的，离线穷举 88 个组合会累积到分钟级。"""
        return None

    monkeypatch.setattr(pl, "upload_image", fake_upload)
    monkeypatch.setattr(pl, "_skc_open_space", fake_open_space)
    monkeypatch.setattr(pl, "_pick_from_space", fake_pick)
    monkeypatch.setattr(pl.asyncio, "sleep", no_sleep)


def _imgs(tmp_path, n: int) -> str:
    """造 n 张达标测试图，返回目录。"""
    d = tmp_path / f"new{n}"
    d.mkdir()
    for i in range(1, n + 1):
        Image.new("RGB", (CLOTH_MIN_W, CLOTH_MIN_H), (i * 7 % 255, 60, 90)).save(
            str(d / f"new-{i:02d}.jpg"))
    return str(d)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_count", list(range(0, 11)))
@pytest.mark.parametrize("new_count", list(range(1, 11)))
async def test_invariants_over_all_combinations(tmp_path, stub_steps, old_count, new_count):
    """穷举 0..10 旧 x 3..10 新：三条不变式在整条轨迹上都必须成立。

    这是发现「朴素交替在 old=10 触顶」那个反例的测试。
    """
    row = FakeRow(old_count)
    r = await pl.skc_replace_row(FakeSession(row), "图色", _imgs(tmp_path, new_count))

    assert r["status"] == "ok", r
    # 1. 从不触顶：不能有任何一次在满行时试图挂图（那在真站是静默失败）
    assert row.overflow_attempts == 0, f"满行时仍试图挂图，轨迹={row.trace}"
    assert max(row.trace) <= pl.SKC_ROW_MAX_IMAGES, f"峰值超上限，轨迹={row.trace}"
    # 2. 从不删空（原本有图的话）：删空会丢行绑定，图会挂到别的颜色行
    if old_count > 0:
        assert min(row.trace) >= 1, f"行被删空，轨迹={row.trace}"
    # 3. 收尾恰好是新图，且旧图全部删净
    assert row.imgs == [f"new-{i:02d}.jpg" for i in range(1, new_count + 1)], row.imgs
    assert r["finalCount"] == new_count
    assert r["deletedOld"] == old_count


@pytest.mark.asyncio
async def test_no_predelete_in_typical_case(tmp_path, stub_steps):
    """6 旧换 6 新（日志里那个真实场景）：不再有开头的预删，第一步就是挂图。"""
    row = FakeRow(6)
    r = await pl.skc_replace_row(FakeSession(row), "图色", _imgs(tmp_path, 6))
    assert r["status"] == "ok"
    assert row.ops[0].startswith("add:"), f"第一步应当是挂图而不是删图：{row.ops[:3]}"
    # 交替后峰值只有 7（旧实现峰值 10，且需要预删 2 张）
    assert max(row.trace) == 7, row.trace
    assert min(row.trace) == 6, row.trace


@pytest.mark.asyncio
async def test_full_row_deletes_first(tmp_path, stub_steps):
    """行已满 10 张：这一轮必须先删后挂，否则那张静默挂不进去。"""
    row = FakeRow(10)
    r = await pl.skc_replace_row(FakeSession(row), "图色", _imgs(tmp_path, 6))
    assert r["status"] == "ok"
    assert row.ops[0] == "del", f"满行时第一步应当是删图：{row.ops[:3]}"
    assert max(row.trace) == 10, row.trace
    # 交替阶段在 10<->9 之间摆动（先删一张腾位、挂上又回到 10）；
    # 6 张新图挂完还剩 4 张旧图，收尾逐张删净，故轨迹最低点是新图数 6。
    assert row.trace[:4] == [10, 9, 10, 9], row.trace
    assert min(row.trace) == 6, row.trace


@pytest.mark.asyncio
async def test_fewer_new_than_old_cleans_up(tmp_path, stub_steps):
    """6 旧换 3 新：交替 3 轮后仍剩 3 张旧图，收尾必须删干净。"""
    row = FakeRow(6)
    r = await pl.skc_replace_row(FakeSession(row), "图色", _imgs(tmp_path, 3))
    assert r["status"] == "ok"
    assert row.imgs == ["new-01.jpg", "new-02.jpg", "new-03.jpg"], row.imgs
    assert r["deletedOld"] == 6


@pytest.mark.asyncio
async def test_over_capacity_input_now_works(tmp_path, stub_steps):
    """3 旧换 8 新：旧实现直接报错（3+8>10 且一张都不能删），交替版能跑完。"""
    row = FakeRow(3)
    r = await pl.skc_replace_row(FakeSession(row), "图色", _imgs(tmp_path, 8))
    assert r["status"] == "ok", r
    assert r["finalCount"] == 8
    assert max(row.trace) <= 10, row.trace


@pytest.mark.asyncio
@pytest.mark.parametrize("new_count", [1, 2])
async def test_few_new_images_still_replaced(tmp_path, stub_steps, new_count):
    """新图只有 1~2 张时【照常换】，不在这里拦——多颜色商品每行只分到 1~2 张是常态。

    2026-08-24 曾在入口拦「< 3 张」，product-985713733384 的 4 个颜色行全被拦死，
    还把「入口拦下」虚报成「换图失败」。下限由 service 那层负责（先补齐、补不上报人工）。
    """
    row = FakeRow(6)
    r = await pl.skc_replace_row(FakeSession(row), "图色", _imgs(tmp_path, new_count))
    assert r["status"] == "ok", r
    assert row.imgs == [f"new-{i:02d}.jpg" for i in range(1, new_count + 1)], row.imgs
    assert min(row.trace) >= 1, f"行被削空，轨迹={row.trace}"


@pytest.mark.asyncio
async def test_precheck_rejects_too_many(tmp_path, stub_steps):
    """新图超过 10 张时入口就拦。"""
    row = FakeRow(6)
    r = await pl.skc_replace_row(FakeSession(row), "图色", _imgs(tmp_path, 11))
    assert r["status"] == "error" and r["stage"] == "precheck", r
    assert row.ops == []


@pytest.mark.asyncio
async def test_failure_midway_never_drops_below_start(tmp_path, stub_steps, monkeypatch):
    """中途失败时行内不会被削残——这正是预删那版的病根。

    预删版：6 张预删 2 张后 open-space 失败 → 行内只剩 4 张（实测咖啡色行）。
    交替版：失败时行内始终不低于初始值。
    """
    calls = {"n": 0}

    async def flaky_open(session, row_keyword):
        calls["n"] += 1
        if calls["n"] == 3:              # 第 3 张时失败
            return {"err": "空间弹窗打不开（模拟）"}
        return {"opened": True}

    monkeypatch.setattr(pl, "_skc_open_space", flaky_open)
    row = FakeRow(6)
    r = await pl.skc_replace_row(FakeSession(row), "图色", _imgs(tmp_path, 6))
    assert r["status"] == "error" and r["stage"] == "open-space", r
    assert min(row.trace) >= 6, f"轨迹曾低于初始值 6：{row.trace}"


@pytest.mark.asyncio
async def test_returns_fileids_for_resume(tmp_path, stub_steps):
    """返回 fileIds 清单：续跑判「本行是否已是这一批图」要靠它比对。"""
    row = FakeRow(6)
    r = await pl.skc_replace_row(FakeSession(row), "图色", _imgs(tmp_path, 6))
    assert r["status"] == "ok"
    assert len(r["fileIds"]) == 6
    assert all(f.startswith("wxalbum/new-") for f in r["fileIds"]), r["fileIds"]
