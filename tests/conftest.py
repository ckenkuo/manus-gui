# -*- coding: utf-8 -*-
"""pytest 全局夹具。

协作文档登记簿（app/cloud_docs.py）重定向到临时目录：save_prefs 等钩子会自动
remember 链接，不隔离的话测试里的假链接会写进真实的 workspace/cloud_docs.json。

发布管线缓存（app/publish/cache.py）同理，而且更要紧：run_batch 一开始就读
cache_stats() 报缓存现状，各阶段跑通还会往里写类目路径与属性选项。不隔离的话
测试里的假类目（叶子「叶子0」之类）会污染真实缓存，之后真跑批次时被喂给 LLM。

飞书告警（app/publish/alert.py）必须掐死在这里：告警钩子挂在 run_batch 的事件流上
（service._alert_hook），而好几个用例会真调 run_batch 且【故意】构造中断场景
（CDP 不通、缺店铺、商品 fail）——不隔离的话每跑一轮 -k publish 就往真实告警群
发 6 条假告警（2026-09-01 实测，跑了三轮才发现）。这条隔离比前两条更要紧：
前两条脏的是本机文件，这条是往真人的群里发消息。
"""
import pytest

from app import cloud_docs
from app.publish import alert as publish_alert
from app.publish import cache as publish_cache


@pytest.fixture(autouse=True)
def _isolate_cloud_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_docs, "CLOUD_DOCS", tmp_path / "cloud_docs.json")


@pytest.fixture(autouse=True)
def _isolate_publish_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_cache, "CACHE_DIR", str(tmp_path / "publish-cache"))


@pytest.fixture(autouse=True)
def _block_publish_alert(monkeypatch):
    """禁止单测把飞书告警真发出去。

    掐在 send 这一层（而不是 load_alert_config 返回禁用态）：这样连 requests 都不会
    被调到，离线跑测试也不会因为出网超时而变慢；同时三个语义化入口
    （alert_product_fail / alert_batch_aborted / alert_batch_done）的卡片拼装逻辑
    仍然真实执行，拼错了照样能被测出来。

    需要断言「发了什么」的用例自己 monkeypatch alert.send 覆盖掉这个夹具即可。
    """
    async def _blocked(payload: dict) -> bool:
        return False

    monkeypatch.setattr(publish_alert, "send", _blocked)
