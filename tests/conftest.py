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

错误上报（app/error_report.py）与告警同源同理由，也掐在这里：同一批故意构造的失败
场景会走同一个 _alert_hook，而它写的是公网 MySQL —— 跑一轮 -k publish 就往真实错误
表里混进十几行假记录，跟真故障分不开。开发机通常 enabled=true（见 config.toml），
所以这条不是可选项。

活动历史（app/activity/history.py）同理且更要紧：它的写挂在 run_activity_batch 的
事件切面上、读挂在 scan_activity_matrix / run_activity_batch 主路径（summarize 注入）
上——不掐的话跑一轮 -k activity，写会往真实历史表混假行、读会真连公网 MySQL
（慢且依赖网络），还会把「无历史」的测试前提变成未知。
"""
import os

# 配置源钉死文件模式：单测不依赖配置中心（MySQL）的可达性，也不被库里的配置
# 改动带跑（2026-09-22 配置中心改造）。必须在任何 app.* import 之前设置——
# app.config 的 Config 单例在 import 时就完成首次加载。
os.environ.setdefault("MANUS_CONFIG_SOURCE", "file")

import pytest

from app import cloud_docs
from app.activity import matrix as activity_matrix
from app.publish import alert as publish_alert
from app.publish import cache as publish_cache


@pytest.fixture(autouse=True)
def _isolate_cloud_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_docs, "CLOUD_DOCS", tmp_path / "cloud_docs.json")


@pytest.fixture(autouse=True)
def _isolate_publish_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_cache, "CACHE_DIR", str(tmp_path / "publish-cache"))


@pytest.fixture(autouse=True)
def _isolate_activity_matrix(tmp_path, monkeypatch):
    """识别矩阵落盘目录指向 tmp_path：默认实现是 get_output_dir（会 mkdir 桌面输出目录），
    单测里绝不能真写桌面。与 _isolate_publish_cache 同理。"""
    monkeypatch.setattr(activity_matrix, "_dir", lambda: str(tmp_path / "activity-matrix"))


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


@pytest.fixture(autouse=True)
def _block_error_report(monkeypatch):
    """禁止单测把失败上报真写进 MySQL（含失败现场明细表）。

    掐在写库这一层、而不是让 load_config 返回禁用态：这样连 pymysql 都不会被 import、
    也不会有出网超时，而 load_config 的取键、快照组装的截断与降级、_alert_hook 的
    补写守卫这些逻辑仍真实执行，写错了照样能被测出来。

    需要断言「报了什么的」用例自己 monkeypatch 覆盖掉这个夹具即可。
    """
    from app import error_report

    def _blocked(*args, **kwargs):
        return 0

    monkeypatch.setattr(error_report, "_write", _blocked)
    monkeypatch.setattr(error_report, "_write_snapshot", _blocked)


@pytest.fixture(autouse=True)
def _block_activity_history(monkeypatch):
    """禁止单测把活动历史真写进 MySQL、或让 summarize 真连公网库。

    掐在最低层的 _write / _query_rows（而不是让 load_config 返回「连不上」）：这样
    load_config 的逐项回退、切面的事件→记录映射、summarize 的归并逻辑都真实执行，
    写错了照样能被测出来；同时连 pymysql 都不会被 import，离线跑也不会出网超时。
    _query_rows 返回空 = 「无历史」，正是大多数用例的前提。

    活动历史没有开关（开发机 [error_report] 配着生产库，load_config 必然可用），
    不掐这两处的话单测就会真写生产库。

    需要断言「记了什么」的用例自己 monkeypatch 覆盖掉这个夹具即可。
    """
    from app.activity import history

    def _blocked_write(*args, **kwargs):
        return None

    monkeypatch.setattr(history, "_write", _blocked_write)
    monkeypatch.setattr(history, "_query_rows", lambda *a, **k: [])
    # summarize 里与 _query_rows 同路径的 DB 时钟查询也掐掉（返回 None = 不判 24h）。
    monkeypatch.setattr(history, "_db_now", lambda *a, **k: None)
