# -*- coding: utf-8 -*-
"""协作文档链接登记簿（app/cloud_docs.py）：登记/命名/排序/删除 + 管线自动登记钩子。"""
import json

from app import cloud_docs
from app.collect import service as collect_service
from app.orders import service as orders_service

URL_A = "https://www.kdocs.cn/l/aaa"
URL_B = "https://www.kdocs.cn/l/bbb"


def test_remember_and_list(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_docs, "CLOUD_DOCS", tmp_path / "cloud_docs.json")
    cloud_docs.remember(URL_A, name="wintop订单登记表")
    cloud_docs.remember(URL_B)

    by_url = {d["url"]: d for d in cloud_docs.list_docs()}
    assert set(by_url) == {URL_A, URL_B}
    assert by_url[URL_A]["name"] == "wintop订单登记表"
    assert by_url[URL_B]["name"] == ""
    # 落盘了，重读仍在
    assert json.loads(
        (tmp_path / "cloud_docs.json").read_text(encoding="utf-8")
    )["docs"]


def test_remember_keeps_name_when_unnamed(tmp_path, monkeypatch):
    """自动登记（不带名字）不能抹掉用户起过的名字；带了才更新。"""
    monkeypatch.setattr(cloud_docs, "CLOUD_DOCS", tmp_path / "cloud_docs.json")
    cloud_docs.remember(URL_A, name="登记表")
    cloud_docs.remember(URL_A)
    (entry,) = cloud_docs.list_docs()
    assert entry["name"] == "登记表"
    cloud_docs.remember(URL_A, name="新名字")
    assert cloud_docs.list_docs()[0]["name"] == "新名字"


def test_list_sorted_by_last_used_desc(tmp_path, monkeypatch):
    p = tmp_path / "cloud_docs.json"
    p.write_text(
        json.dumps({"docs": [
            {"name": "", "url": URL_A, "last_used": "2026-08-01T10:00:00"},
            {"name": "", "url": URL_B, "last_used": "2026-08-03T10:00:00"},
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(cloud_docs, "CLOUD_DOCS", p)
    assert [d["url"] for d in cloud_docs.list_docs()] == [URL_B, URL_A]


def test_remove(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_docs, "CLOUD_DOCS", tmp_path / "cloud_docs.json")
    cloud_docs.remember(URL_A)
    assert cloud_docs.remove(URL_A) is True
    assert cloud_docs.list_docs() == []
    assert cloud_docs.remove(URL_A) is False


def test_corrupt_file_returns_empty(tmp_path, monkeypatch):
    p = tmp_path / "cloud_docs.json"
    p.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(cloud_docs, "CLOUD_DOCS", p)
    assert cloud_docs.list_docs() == []


def test_orders_save_prefs_registers_cloud_link(tmp_path, monkeypatch):
    """订单页保存偏好（跑批/切工作簿都会触发）时，云端链接自动进登记簿；本地路径不进。"""
    monkeypatch.setattr(orders_service, "ORDERS_PREFS", tmp_path / "orders_prefs.json")
    monkeypatch.setattr(cloud_docs, "CLOUD_DOCS", tmp_path / "cloud_docs.json")
    orders_service.save_prefs(store="Pawly", workbook=URL_A, sheet="S")
    assert [d["url"] for d in cloud_docs.list_docs()] == [URL_A]
    orders_service.save_prefs(store="Pawly", workbook="D:/wb.xlsx", sheet="S")
    assert len(cloud_docs.list_docs()) == 1


def test_collect_save_prefs_registers_cloud_link(tmp_path, monkeypatch):
    """采集页保存偏好时，云端链接自动进登记簿；file_id 形态（非 http 链接）不进。"""
    monkeypatch.setattr(collect_service, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    monkeypatch.setattr(cloud_docs, "CLOUD_DOCS", tmp_path / "cloud_docs.json")
    collect_service.save_prefs(excel=URL_A, sheet="S")
    assert [d["url"] for d in cloud_docs.list_docs()] == [URL_A]
    collect_service.save_prefs(excel="fileid123", sheet="S")
    assert len(cloud_docs.list_docs()) == 1
