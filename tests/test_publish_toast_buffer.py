"""toast 环形缓冲：让阶段⑭ save 能回捞平台真实拒绝原因（2026-09-01 实测两单）。

取证：save 后平台弹「错误：请上传预览图」，被 toast 哨兵抓到并记了 warning，
但 _JS_SAVE_FEEDBACK 只读 .ant-message / .ant-notification —— 店小秘的
d-message 是自有浮层，两个选择器都抓不到，于是 save 判据两边都空，只能报
「无校验错误但草稿更新时间未变」，把排查方向从「预览图没上传」带偏。
"""
import time

from app.publish import browser


def setup_function(_fn):
    browser._TOAST_LOG.clear()


def test_toast进缓冲且可按时间窗回捞():
    t0 = time.time()
    browser.BrowserSession._on_toast("错误：请上传预览图")
    assert "错误：请上传预览图" in browser.recent_toasts(since=t0)


def test_时间窗之前的旧提示不算进本次结论():
    browser.BrowserSession._on_toast("错误：上一阶段的旧提示")
    time.sleep(0.01)
    t_click = time.time()
    browser.BrowserSession._on_toast("错误：请上传预览图")
    got = browser.recent_toasts(since=t_click)
    assert got == ["错误：请上传预览图"]


def test_bad_only只留命中关键词的():
    t0 = time.time()
    browser.BrowserSession._on_toast("保存成功")
    browser.BrowserSession._on_toast("错误：请上传预览图")
    assert browser.recent_toasts(since=t0, bad_only=True) == ["错误：请上传预览图"]
    assert len(browser.recent_toasts(since=t0)) == 2


def test_重复文案去重保序():
    t0 = time.time()
    for _ in range(3):
        browser.BrowserSession._on_toast("错误：请上传预览图")
    browser.BrowserSession._on_toast("错误：请先选择尺码")
    assert browser.recent_toasts(since=t0, bad_only=True) == [
        "错误：请上传预览图", "错误：请先选择尺码"]


def test_缓冲有上界不会无限增长():
    for i in range(browser._TOAST_LOG_MAX + 40):
        browser.BrowserSession._on_toast(f"提示{i}")
    assert len(browser._TOAST_LOG) <= browser._TOAST_LOG_MAX


def test_空文案不入缓冲():
    browser.BrowserSession._on_toast("")
    browser.BrowserSession._on_toast("   ")
    assert browser._TOAST_LOG == []
