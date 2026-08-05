# -*- coding: utf-8 -*-
"""pytest 全局夹具。

协作文档登记簿（app/cloud_docs.py）重定向到临时目录：save_prefs 等钩子会自动
remember 链接，不隔离的话测试里的假链接会写进真实的 workspace/cloud_docs.json。
"""
import pytest

from app import cloud_docs


@pytest.fixture(autouse=True)
def _isolate_cloud_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_docs, "CLOUD_DOCS", tmp_path / "cloud_docs.json")
