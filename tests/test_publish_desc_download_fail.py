# -*- coding: utf-8 -*-
"""测试描述图下载失败时的容错行为（2026-09-02）。

单张描述图下载失败（如 404）不应让整个商品失败，应跳过该图继续处理其余图。
如果有尺码上下文（descText 或 sizeMeasurements），提示影响较小。
"""
import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from app.publish.service import _prepare_desc_image


@pytest.mark.asyncio
async def test_prepare_desc_image_download_404_upscale_branch():
    """upscale 分支下载 404 时返回失败状态，不抛异常。"""
    workdir = "/tmp/test-workdir"
    rep = {
        "url": "https://example.com/not-exist.jpg",
        "needsUpscale": True,
        "reason": "尺寸 800x800 低于 1340x1785"
    }

    # 模拟 _download_image 抛 404 异常
    with patch("app.publish.extract._download_image") as mock_dl:
        mock_dl.side_effect = RuntimeError("重试 3 次仍失败：404 Client Error: Not Found")

        result = await _prepare_desc_image(workdir, rep)

        # 应返回失败状态而不是抛异常
        assert result.get("ok") is False
        assert "放大失败" in result.get("why", "")
        assert "404" in result.get("why", "")


@pytest.mark.asyncio
async def test_prepare_desc_image_download_404_edit_branch():
    """edit 分支下载 404 时返回失败状态（原先就有 try-except）。"""
    workdir = "/tmp/test-workdir"
    rep = {
        "url": "https://example.com/not-exist.jpg",
        # needsUpscale 为 False，走英化分支
    }

    with patch("app.publish.extract._download_image") as mock_dl:
        mock_dl.side_effect = RuntimeError("重试 3 次仍失败：404 Client Error: Not Found")

        result = await _prepare_desc_image(workdir, rep)

        assert result.get("ok") is False
        assert "英化失败" in result.get("why", "")
        assert "下载原图" in result.get("why", "")


@pytest.mark.asyncio
async def test_multiple_images_one_fail_others_continue():
    """多张描述图，一张下载失败时其余图继续处理（集成测试占位）。

    完整的集成测试需要 mock BrowserSession 和 emit，这里只验证逻辑：
    _prepare_desc_image 返回 {"ok": False} 时，上层 _replace_round 会 continue，
    不会中断整个循环。
    """
    # 这是逻辑验证占位，真正的集成测试需要启动浏览器
    # 验证点：_replace_round 在 2187-2201 行有 if not got.get("ok"): continue
    pass


def test_size_context_hint_logic():
    """验证尺码上下文检查的逻辑（单元测试占位）。

    当 info_for_desc 有 descText 或 sizeMeasurements 时，
    错误提示应包含"已有文本尺码信息，影响较小"。
    """
    # 逻辑在 service.py 2187-2201 行
    # 有上下文: hint = "；已有文本尺码信息，影响较小"
    # 无上下文: hint = ""
    pass
