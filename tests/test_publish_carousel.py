import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from app.publish import vision
from app.publish.stages import carousel
from app.publish.workflows import get_workflow


@pytest.fixture
def scene(tmp_path, monkeypatch):
    class Scene:
        def __init__(self):
            self.items = []
            self.sources = {}
            self.paths = {}
            self.info = set()
            self.dirty = set()
            self.bad_final = set()
            self.unreachable = set()
            self.upload_fail = set()
            self.checks = []
            self.edits = []
            self.events = []
            self.pending = []
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
            Image.new("RGB", (800, 800), color).save(destination)
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
            bad = url in self.dirty or (final and self.sources[url] in self.bad_final)
            return {"clean": not bad, "issues": "中文或乱码" if bad else ""}

        def edit(self, path, **kwargs):
            self.edits.append(self.paths[path])
            destination = kwargs["out_path"]
            Image.new("RGB", (800, 800), "white").save(destination)
            self.paths[destination] = self.paths[path]
            return {"output": destination}

        async def upload(self, session, paths):
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
            self.items = [{"url": url, "checked": False, "bad": False}
                          for url in reversed(self.pending)] + self.items
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
    assert len([final for _, final in scene.checks if final]) == 5


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
async def test_information_overflow_requires_manual_selection(scene):
    for _ in range(10):
        scene.add()
    scene.add(selected=False, info=True)
    assert (await scene.run())["status"] == "fail"
    assert any("上限" in event.get("message", "") for event in scene.events)


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
    assert len(scene.checks) == 4
    assert scene.edits == [target]
    assert sum(item["checked"] for item in scene.items) == 4


@pytest.mark.asyncio
async def test_missing_classification_requires_manual_review(scene, monkeypatch):
    for _ in range(3):
        scene.add()
    scene.add(selected=False, info=True)
    monkeypatch.setattr(carousel.vision, "plan_carousel", AsyncMock(return_value={"items": {}}))
    assert (await scene.run())["status"] == "fail"


@pytest.mark.asyncio
async def test_failed_swap_restores_original_at_maximum(scene):
    defaults = [scene.add() for _ in range(10)]
    scene.dirty.add(defaults[0])
    scene.refuse.add("https://host.test/new-0.jpg")
    assert (await scene.run())["status"] == "fail"
    assert {item["url"] for item in scene.items if item["checked"]} == set(defaults)


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
