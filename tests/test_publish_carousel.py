import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from app.publish import vision
from app.publish.vision import check_cleaned as real_check_cleaned
from app.publish.stages import carousel
from app.publish.workflows import get_workflow


@pytest.fixture
def scene(tmp_path, monkeypatch):
    class Scene:
        def __init__(self):
            self.items = []
            self.sources = {}
            self.sizes = {}
            self.paths = {}
            self.info = set()
            self.dirty = set()
            self.bad_final = set()
            self.flaky_final = set()
            self.unreachable = set()
            self.upload_fail = set()
            self.checks = []
            self.edits = []
            self.edit_prompts = []
            self.generated_paths = set()
            self.prepared_verdicts = {}
            self.events = []
            self.pending = []
            self.toasts = []
            self.refuse = set()
            self.ctx = {"workdir": str(tmp_path)}

        def add(self, selected=True, info=False):
            index = len(self.items)
            url = f"https://source.test/{index}.jpg"
            self.items.append({"i": index, "url": url, "checked": selected, "bad": False})
            self.sources[url] = url
            if info:
                self.info.add(url)
            return url

        async def read(self, session):
            return {"supported": True, "items": [dict(item) for item in self.items]}

        def download(self, url, destination):
            if url in self.unreachable:
                return 0
            self.paths[str(destination)] = url
            color = (list(self.sources).index(url) * 27 % 255, 80, 100)
            Image.new("RGB", self.sizes.get(url, (800, 800)), color).save(destination)
            return os.path.getsize(destination)

        async def classify(self, entries, info):
            return {"items": {entry["file"]: {
                "isInfo": self.paths[entry["path"]] in self.info,
                "kind": "尺码表", "value": 3, "chinese": False,
            } for entry in entries}}

        async def check(self, path):
            url = self.paths[str(path)]
            final = Path(path).name.startswith("final-")
            if final:
                assert any(item["url"] == url and item["checked"] for item in self.items)
            self.checks.append((url, final))
            generated = str(path) in self.generated_paths
            if generated and self.prepared_verdicts.get(url):
                return self.prepared_verdicts[url].pop(0)
            bad = (url in self.dirty and not generated) or (final and self.sources[url] in self.bad_final)
            # flaky_final 模拟真站抖动：收尾复检第一次判好、复问同一文件时判坏
            if final and self.sources[url] in self.flaky_final:
                seen = sum(1 for u, f in self.checks if f and u == url)
                bad = seen > 1
            return {"clean": not bad, "issues": "中文或乱码" if bad else ""}

        def edit(self, path, **kwargs):
            self.edits.append(self.paths[path])
            self.edit_prompts.append(kwargs["prompt"])
            destination = kwargs["out_path"]
            Image.new("RGB", (800, 800), "white").save(destination)
            self.paths[destination] = self.paths[path]
            self.generated_paths.add(destination)
            return {"output": destination}

        async def upload(self, session, paths, **kwargs):
            assert kwargs == {"min_w": 800, "min_h": 800}
            self.pending = []
            uploaded, failed = [], []
            for index, path in enumerate(paths):
                source = self.paths[path]
                if source in self.upload_fail:
                    failed.append({"path": path})
                    continue
                file_id = f"new-{index}.jpg"
                url = f"https://host.test/{file_id}"
                self.sources[url] = source
                self.pending.append(url)
                uploaded.append({"fileId": file_id})
            return {"uploaded": uploaded, "failed": failed}

        async def pick(self, session, file_ids):
            # 真站行为（2026-09-17 实测 1005064778878）：确定后新图【自动勾选】，
            # 且平台按「最多选用 10 张」在插入那一刻截断，超出的那几张连候选列表都不进
            # （页面只弹一句「最大支持10张图片,上传成功5张!」）。
            names = [f.rsplit("/", 1)[-1] for f in file_ids]
            wanted = [url for url in self.pending if url.rsplit("/", 1)[-1] in names]
            room = 10 - sum(item["checked"] for item in self.items)
            taken = wanted[:max(0, room)]
            self.toasts.append(f"最大支持10张图片,上传成功{len(taken)}张!")
            self.items = [{"url": url, "checked": True, "bad": False}
                          for url in reversed(taken)] + self.items
            for index, item in enumerate(self.items):
                item["i"] = index
            return {"stage": "ok"}

        async def toggle(self, session, index, want):
            item = self.items[index]
            if item["url"] in self.refuse:
                return {"stage": "failed"}
            count = sum(entry["checked"] for entry in self.items)
            if want and count >= 10:
                return {"stage": "limit"}
            if not want and count <= 3:
                return {"stage": "minimum"}
            item["checked"] = want
            return {"stage": "ok"}

        async def emit(self, event):
            self.events.append(event)

        async def run(self):
            return await carousel._st_carousel(self.ctx, object(), self.emit)

    instance = Scene()
    monkeypatch.setattr(carousel, "expand_carousel_pool", AsyncMock(return_value={"expanded": True}))
    monkeypatch.setattr(carousel, "carousel_state", instance.read)
    monkeypatch.setattr(carousel.extract, "_download_image", instance.download)
    monkeypatch.setattr(carousel.vision, "plan_carousel", instance.classify)
    monkeypatch.setattr(carousel.vision, "check_cleaned", instance.check)
    monkeypatch.setattr(carousel.images, "edit_image", instance.edit)
    monkeypatch.setattr(carousel, "upload_many", instance.upload)
    monkeypatch.setattr(carousel, "open_carousel_space", AsyncMock(return_value={"opened": True}))
    monkeypatch.setattr(carousel.media_space, "_pick_many_from_space", instance.pick)
    monkeypatch.setattr(carousel, "toggle_carousel", instance.toggle)
    normalize = carousel._to_carousel_size

    def normalize_and_track(path, out_path, preserve_info=False):
        result = normalize(path, out_path, preserve_info)
        if result:
            instance.paths[result] = instance.paths[path]
        return result

    monkeypatch.setattr(carousel, "_to_carousel_size", normalize_and_track)
    return instance


@pytest.mark.asyncio
async def test_defaults_and_all_information_images_pass_qc(scene):
    defaults = [scene.add() for _ in range(5)]
    information = [scene.add(selected=False, info=True) for _ in range(4)]
    scene.dirty.update([defaults[0], information[0]])
    result = await scene.run()
    assert result["status"] == "ok"
    assert set(scene.edits) == {defaults[0], information[0]}
    assert {url for url, final in scene.checks if not final} == set(defaults + information)
    assert sum(item["checked"] for item in scene.items) == 9
    # 5 张待复检的图，每张问满两次（任一次判坏就拦下，故两次都要问）
    assert len([final for _, final in scene.checks if final]) == 10


@pytest.mark.asyncio
async def test_cute_false_positive_never_blocks_carousel_or_requests_manual_edit(scene, monkeypatch):
    for index in range(3):
        scene.add()
    scene.add(selected=False, info=True)
    monkeypatch.setattr(carousel.vision, "check_cleaned", real_check_cleaned)
    model = AsyncMock(return_value={
        "residualChinese": False, "garbled": False, "watermark": False,
        "brokenSubject": False, "marketingClaim": True, "issues": "Cute 属情绪夸大类"})
    monkeypatch.setattr(carousel.vision, "ask_json_with_images", model)

    result = await scene.run()

    assert result["status"] == "ok"
    assert sum(item["checked"] for item in scene.items) == 4
    assert model.await_count == 10
    assert not scene.edits
    assert not any(event["type"] == "manual_check" for event in scene.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("information", [False, True])
async def test_second_qc_failure_stops_without_regenerating(scene, information):
    defaults = [scene.add() for _ in range(3)]
    target = scene.add(selected=False, info=True) if information else defaults[0]
    scene.dirty.add(target)
    scene.bad_final.add(target)
    result = await scene.run()
    assert result["status"] == "fail"
    assert scene.edits == [target]
    assert not os.path.exists(carousel._en_cache_path(scene.ctx["workdir"], target))
    assert any(event["type"] == "manual_check" and "复检未通过" in event["message"]
               for event in scene.events)
    assert any(item["checked"] and scene.sources[item["url"]] == target
               and item["url"] != target for item in scene.items)


@pytest.mark.asyncio
@pytest.mark.parametrize("information", [False, True])
async def test_unreadable_images_require_manual_review(scene, information):
    defaults = [scene.add() for _ in range(3)]
    target = scene.add(selected=False, info=True) if information else defaults[0]
    scene.unreachable.add(target)
    assert (await scene.run())["status"] == "fail"
    assert not scene.edits


@pytest.mark.asyncio
async def test_generation_failure_for_information_image_blocks(scene, monkeypatch):
    for _ in range(3):
        scene.add()
    target = scene.add(selected=False, info=True)
    scene.dirty.add(target)
    def broken(*args, **kwargs):
        raise RuntimeError("generation unavailable")
    monkeypatch.setattr(carousel.images, "edit_image", broken)
    assert (await scene.run())["status"] == "fail"


@pytest.mark.asyncio
async def test_partial_upload_failure_blocks(scene):
    for _ in range(3):
        scene.add()
    first = scene.add(selected=False, info=True)
    second = scene.add(selected=False, info=True)
    scene.upload_fail.add(first)
    assert (await scene.run())["status"] == "fail"
    assert any(item["checked"] and scene.sources[item["url"]] == second for item in scene.items)


@pytest.mark.asyncio
async def test_full_carousel_can_replace_without_exceeding_limit(scene):
    defaults = [scene.add() for _ in range(10)]
    scene.dirty.update(defaults[:2])
    assert (await scene.run())["status"] == "ok"
    assert sum(item["checked"] for item in scene.items) == 10


@pytest.mark.asyncio
async def test_information_overflow_reports_without_blocking(scene):
    # 选用位已满时补不进信息图：如实上报交人工，但不阻断——10 张选用图本身合格、
    # 数量也合规，页面已是可发布状态（详见 _st_carousel 里 overflow 的注释）。
    for _ in range(10):
        scene.add()
    scene.add(selected=False, info=True)
    assert (await scene.run())["status"] == "ok"
    assert any("选用位已满" in event.get("message", "") for event in scene.events)
    assert not any(event["type"] == "manual_check" for event in scene.events)
    assert sum(item["checked"] for item in scene.items) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("issue", ["Super Cute!属情绪夸大", "AIRPLAN应为AIRPLANE", "High-Quality等夸大宣称"])
async def test_failed_generation_is_repaired_before_upload(scene, issue):
    defaults = [scene.add() for _ in range(3)]
    target = defaults[0]
    scene.dirty.add(target)
    scene.prepared_verdicts[target] = [{"clean": False, "issues": issue}]

    result = await scene.run()

    assert result["status"] == "ok"
    assert scene.edits == [target, target]
    assert issue in scene.edit_prompts[1]
    assert "中文或乱码" in scene.edit_prompts[0]
    assert sum(item["checked"] for item in scene.items) == 3


@pytest.mark.asyncio
async def test_prepared_qc_disagreement_requires_regeneration(scene):
    defaults = [scene.add() for _ in range(3)]
    target = defaults[0]
    scene.dirty.add(target)
    scene.prepared_verdicts[target] = [
        {"clean": True}, {"clean": False, "issues": "Super Cute仍在"}]

    assert (await scene.run())["status"] == "ok"
    assert scene.edits == [target, target]
    assert "Super Cute仍在" in scene.edit_prompts[1]


@pytest.mark.asyncio
async def test_repeated_bad_generations_do_not_upload_or_replace(scene):
    defaults = [scene.add() for _ in range(3)]
    target = defaults[0]
    scene.dirty.add(target)
    scene.prepared_verdicts[target] = [
        {"clean": False, "issues": "High-Quality仍在"}] * carousel.EN_QC_TRIES

    assert (await scene.run())["status"] == "fail"
    assert len(scene.edits) == carousel.EN_QC_TRIES
    assert not scene.pending
    assert {item["url"] for item in scene.items if item["checked"]} == set(defaults)
    assert not os.path.exists(carousel._en_cache_path(scene.ctx["workdir"], target))


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [{}, {"clean": "true"}, {"clean": True, "status": "error"}])
async def test_unknown_prepared_qc_blocks_without_spending_more_generations(scene, verdict):
    defaults = [scene.add() for _ in range(3)]
    target = defaults[0]
    scene.dirty.add(target)
    scene.prepared_verdicts[target] = [verdict]

    assert (await scene.run())["status"] == "fail"
    assert scene.edits == [target]
    assert not scene.pending
    assert not os.path.exists(carousel._en_cache_path(scene.ctx["workdir"], target))


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_cache", [False, True])
async def test_cached_image_must_pass_qc_before_reuse(scene, tmp_path, bad_cache):
    target = scene.add()
    local = str(tmp_path / "source.jpg")
    scene.download(target, local)
    cached = carousel._en_cache_path(str(tmp_path), target)
    Path(cached).parent.mkdir()
    scene.edit(local, out_path=cached, prompt="old prompt")
    scene.edits.clear()
    scene.edit_prompts.clear()
    if bad_cache:
        scene.prepared_verdicts[target] = [{"clean": False, "issues": "AIRPLAN拼写错误"}]

    result = await carousel._english_one(local, target, str(tmp_path))

    assert result["ok"] is True
    assert result["how"] == ("edited" if bad_cache else "cached")
    assert scene.edits == ([target] if bad_cache else [])
    if bad_cache:
        assert "AIRPLAN拼写错误" in scene.edit_prompts[0]
    else:
        assert len(scene.checks) == 2


@pytest.mark.asyncio
async def test_hosted_image_does_not_skip_current_qc(scene):
    defaults = [scene.add() for _ in range(3)]
    scene.ctx["state"] = {"stages": {"carousel": {"status": "ok"}}}
    scene.items[0]["url"] = "https://www.wxalbum.com/picture.jpg"
    scene.sources[scene.items[0]["url"]] = defaults[0]
    scene.dirty.add(scene.items[0]["url"])
    assert (await scene.run())["status"] == "ok"
    assert len(scene.edits) == 1


def test_information_geometry_retains_edges(tmp_path):
    source = tmp_path / "size.jpg"
    destination = tmp_path / "square.jpg"
    picture = Image.new("RGB", (400, 900), "white")
    draw = ImageDraw.Draw(picture)
    draw.rectangle((0, 0, 399, 80), fill="red")
    draw.rectangle((0, 820, 399, 899), fill="blue")
    picture.save(source)
    carousel._to_carousel_size(str(source), str(destination), preserve_info=True)
    with Image.open(destination) as result:
        assert result.width == result.height >= 800
        assert result.getpixel((result.width // 2, 10))[0] > 200
        assert result.getpixel((result.width // 2, result.height - 10))[2] > 200


@pytest.mark.asyncio
async def test_final_qc_flaky_pass_is_blocked(scene):
    """判定抖动取严的一侧：首次判合格、复问判不合格 → 按不合格拦下。

    2026-09-18 商品 1049857947880 取证：第 34 张那次判好被放行，复测却抓出
    「Citizens后出现缺字乱码方块」（原图确实残留方块字 `Citizens囚`）。漏报的后果是
    带中文的图发上真店（Temu 硬红线、后面没有第二道闸），比误报停摆重得多。"""
    defaults = [scene.add() for _ in range(3)]
    scene.dirty.add(defaults[0])
    scene.flaky_final.add(defaults[0])        # 首次判好、复问判坏 → 应拦下
    result = await scene.run()
    assert result["status"] == "fail"
    assert "未换成合规图" in result["note"]


@pytest.mark.asyncio
async def test_final_qc_needs_two_clean_verdicts(scene):
    """反向钉住：两次都判合格才放行，且确实问满了两次（不是问一次就过）。"""
    defaults = [scene.add() for _ in range(3)]
    scene.dirty.add(defaults[0])
    result = await scene.run()
    assert result["status"] == "ok"
    assert "未换成合规图" not in result["note"]
    # 任一次判坏都会拦下，故每张待复检的图必须问满两次
    finals = [url for url, final in scene.checks if final]
    assert finals and len(finals) == len(set(finals)) * 2


@pytest.mark.asyncio
async def test_open_space_retries_when_menu_never_opens(monkeypatch):
    """2026-09-18 商品 1005064778878 实测：⑤c 刚把 9 张产物直传完、页面重渲染过，
    点「选择图片」第一次必落空（no-dropdown）——坐标是「JS 读一次 → 另发一次 CDP
    点击」得来的，两次往返之间页面一动坐标就打偏。分批插入把开弹窗从一次变成每批
    一次，放大了这个脆弱点，故要重瞄。"""
    from app.publish.media import carousel as media_carousel
    monkeypatch.setattr(media_carousel.asyncio, "sleep", AsyncMock())

    class Session:
        def __init__(self, replies):
            self.replies = list(replies)
            self.menu_calls = 0

        async def eval_json(self, js):
            if "getBoundingClientRect" in js:
                return {"x": 100, "y": 200}
            self.menu_calls += 1
            return self.replies.pop(0)

        async def cdp(self, method, params):
            return {"ok": True}

    # 第一次落空、第二次点开：重瞄后成功
    session = Session([{"stage": "menu", "err": "no-dropdown"},
                       {"stage": "open", "opened": True}])
    assert (await media_carousel.open_carousel_space(session))["opened"] is True
    assert session.menu_calls == 2
    # 一直落空：如实报出最后一次失败，不假装成功
    session = Session([{"stage": "menu", "err": "no-dropdown"}] * 3)
    assert (await media_carousel.open_carousel_space(session))["err"] == "no-dropdown"
    assert session.menu_calls == 3
    session = Session([{"stage": "open", "opened": False},
                       {"stage": "open", "opened": True}])
    assert (await media_carousel.open_carousel_space(session))["opened"] is True
    assert session.menu_calls == 2
    session = Session([{"stage": "open", "opened": False}] * 3)
    result = await media_carousel.open_carousel_space(session)
    assert result["opened"] is False
    assert result["err"]
    assert session.menu_calls == 3
    session = Session([{"stage": "menu", "err": "no-menu-item"}])
    assert (await media_carousel.open_carousel_space(session))["err"] == "no-menu-item"
    assert session.menu_calls == 1


@pytest.mark.parametrize("platform", ["1688", "pdd", "temu", "amazon"])
def test_all_workflows_use_shared_carousel_gate(platform):
    stages = get_workflow(platform).stages()
    assert next(stage for stage in stages if stage.key == "carousel").run is carousel._st_carousel


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"garbled": False}, {"clean": "false"}])
async def test_incomplete_qc_is_not_clean(monkeypatch, response):
    monkeypatch.setattr(vision, "ask_json_with_images", AsyncMock(return_value=response))
    result = await vision.check_cleaned("missing.jpg")
    assert result["clean"] is False
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_resume_rechecks_selected_images_without_adding_original_again(scene):
    for _ in range(3):
        scene.add()
    target = scene.add(selected=False, info=True)
    scene.dirty.add(target)
    assert (await scene.run())["status"] == "ok"
    scene.checks.clear()
    assert (await scene.run())["status"] == "ok"
    assert len(scene.checks) == 8
    assert scene.edits == [target]
    assert sum(item["checked"] for item in scene.items) == 4


@pytest.mark.asyncio
async def test_selected_image_single_clean_verdict_cannot_skip_repair(scene, monkeypatch):
    defaults = [scene.add() for _ in range(3)]
    target = defaults[0]
    scene.dirty.add(target)
    original_check = scene.check
    calls = 0

    async def flaky_check(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"clean": True}
        return await original_check(path)

    monkeypatch.setattr(carousel.vision, "check_cleaned", flaky_check)

    assert (await scene.run())["status"] == "ok"
    assert scene.edits == [target]


@pytest.mark.asyncio
async def test_missing_classification_requires_manual_review(scene, monkeypatch):
    for _ in range(3):
        scene.add()
    scene.add(selected=False, info=True)
    monkeypatch.setattr(carousel.vision, "plan_carousel", AsyncMock(return_value={"items": {}}))
    assert (await scene.run())["status"] == "fail"


@pytest.mark.asyncio
async def test_failed_swap_restores_original_at_maximum(scene):
    # 满位时要先取消旧图腾出选用位，那一步被页面拒掉就没得换：如实报失败，
    # 且原图必须还留在选用中（绝不能把「图不合规」变成「图太少」）。
    defaults = [scene.add() for _ in range(10)]
    scene.dirty.add(defaults[0])
    scene.refuse.add(defaults[0])
    assert (await scene.run())["status"] == "fail"
    assert {item["url"] for item in scene.items if item["checked"]} == set(defaults)


@pytest.mark.asyncio
async def test_batched_insert_keeps_all_adds_under_platform_cap(scene):
    # 2026-09-17 两单（1040482047185、1005064778878）的真因：一次把 9 张新图选进弹窗，
    # 平台按「最多 10 张」当场截断，多出的连候选列表都不进，于是逐张报「信息图未补勾」。
    # 分批插入后每一张都得进列表并勾上，替换与补勾一张都不能丢。
    defaults = [scene.add() for _ in range(5)]
    information = [scene.add(selected=False, info=True) for _ in range(5)]
    scene.dirty.update(defaults)
    result = await scene.run()
    assert result["status"] == "ok"
    assert "未补勾" not in result["note"]
    selected = {item["url"] for item in scene.items if item["checked"]}
    assert len(selected) == 10
    # 5 张原选用图全部换成了新产物、5 张信息图全部补勾，页面上一张原图都不该留下
    assert not selected & set(defaults + information)
    assert {scene.sources[url] for url in selected} == set(defaults + information)


@pytest.mark.asyncio
async def test_clean_local_copy_cannot_certify_page_original(scene, tmp_path):
    defaults = [scene.add() for _ in range(3)]
    (tmp_path / "raw.json").write_text(json.dumps({"images": defaults}), encoding="utf-8")
    Image.new("RGB", (800, 800), "white").save(tmp_path / "main-01.jpg")
    info_path = tmp_path / "info.json"
    info_path.write_text(json.dumps({"complianceNotes": [
        {"file": "main-01.jpg", "clean": True, "chinese": False}]}), encoding="utf-8")
    scene.ctx["info_path"] = str(info_path)
    scene.dirty.add(defaults[0])
    assert (await scene.run())["status"] == "ok"
    assert scene.edits == [defaults[0]]


def test_similar_information_images_are_not_dropped(tmp_path, monkeypatch):
    candidates, pool, verdicts = [], {}, {}
    for index in range(2):
        path = tmp_path / f"size-{index}.jpg"
        picture = Image.new("RGB", (800, 800), "white")
        ImageDraw.Draw(picture).text((100, 100), f"Size {index + 1}", fill="black")
        picture.save(path)
        candidates.append({"i": index})
        pool[index] = {"path": str(path), "file": path.name}
        verdicts[path.name] = {"isInfo": True, "value": 3}
    monkeypatch.setattr(carousel.images, "is_near_duplicate", lambda *args: True)
    assert len(carousel._pick_adds(candidates, pool, verdicts, [], 10)) == 2


@pytest.mark.asyncio
async def test_expand_after_render_includes_hidden_information(scene, monkeypatch):
    for _ in range(3):
        scene.add()
    rendered = False

    async def read(session):
        nonlocal rendered
        rendered = True
        return await scene.read(session)

    async def expand(session):
        assert rendered
        scene.add(selected=False, info=True)
        return {"expanded": True}

    monkeypatch.setattr(carousel, "carousel_state", read)
    monkeypatch.setattr(carousel, "expand_carousel_pool", expand)
    assert (await scene.run())["status"] == "ok"
    assert sum(item["checked"] for item in scene.items) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(1080, 1082), (1082, 1080), (800, 801)])
async def test_clean_near_square_image_is_replaced(scene, size):
    defaults = [scene.add() for _ in range(3)]
    target = defaults[0]
    scene.sizes[target] = size
    result = await scene.run()
    assert result["status"] == "ok"
    assert not scene.edits
    selected = [item for item in scene.items if item["checked"]]
    assert len(selected) == 3
    assert target not in {item["url"] for item in selected}
    assert any(scene.sources[item["url"]] == target for item in selected)
    with Image.open(Path(scene.ctx["workdir"]) / "carousel" / "pic00.jpg") as image:
        assert image.width == image.height >= 800


@pytest.mark.parametrize("preserve_info", [False, True])
@pytest.mark.parametrize("size", [(1080, 1082), (1082, 1080), (800, 801), (800, 800)])
def test_carousel_geometry_requires_exact_square(tmp_path, size, preserve_info):
    source = tmp_path / "source.jpg"
    destination = tmp_path / "square.jpg"
    Image.new("RGB", size, "white").save(source)
    output = carousel._to_carousel_size(str(source), str(destination), preserve_info)
    with Image.open(output) as image:
        assert image.width == image.height >= 800
    if size == (800, 800):
        assert output == str(source)
