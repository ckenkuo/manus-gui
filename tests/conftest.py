# -*- coding: utf-8 -*-
"""pytest 全局夹具。

协作文档登记簿（app/cloud_docs.py）重定向到临时目录：save_prefs 等钩子会自动
remember 链接，不隔离的话测试里的假链接会写进真实的 workspace/cloud_docs.json。

发布管线缓存（app/publish/cache.py）同理，而且更要紧：run_batch 一开始就读
cache_stats() 报缓存现状，各阶段跑通还会往里写类目路径与属性选项。不隔离的话
测试里的假类目（叶子「叶子0」之类）会污染真实缓存，之后真跑批次时被喂给 LLM。
"""
import pytest

from app import cloud_docs
from app.publish import cache as publish_cache


@pytest.fixture(autouse=True)
def _isolate_cloud_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_docs, "CLOUD_DOCS", tmp_path / "cloud_docs.json")


@pytest.fixture(autouse=True)
def _isolate_publish_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_cache, "CACHE_DIR", str(tmp_path / "publish-cache"))
