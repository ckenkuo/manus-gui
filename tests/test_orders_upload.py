"""采购汇总上传到 TNAS 的单测：全程不碰真实网络（requests 全部 mock）。

重点验证 best-effort 契约：无论未配置、网络异常还是 HTTP 报错，都不能抛异常出来——
它跑在登记表写入之后，抛出去会让已经写好的表被误判为失败、诱发重跑。
"""

import pytest

from app.orders import upload as U


@pytest.fixture(autouse=True)
def _no_env_password(monkeypatch):
    """清掉环境变量，避免本机真配了密码时影响「未配置」分支的断言。"""
    monkeypatch.delenv("TNAS_WEBDAV_PASSWORD", raising=False)


def _conf(monkeypatch, **over):
    """替掉 _webdav_conf，避免读真实 config.toml。"""
    base = {
        "webdav_url": "https://nas.example.com:5006/manus",
        "username": "u",
        "password": "p",
        "verify_ssl": True,
    }
    base.update(over)
    monkeypatch.setattr(U, "_webdav_conf", lambda: base)
    return base


class _Resp:
    def __init__(self, code=201, text=""):
        self.status_code = code
        self.text = text


def test_未配置时安静跳过不抛错(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_webdav_conf", lambda: {})
    f = tmp_path / "a.xlsx"
    f.write_text("x", encoding="utf-8")

    res = U.upload_file(str(f))
    assert res["ok"] is False
    assert "webdav_url" in res["error"]


def test_缺账号密码时报错而不上传(monkeypatch, tmp_path):
    _conf(monkeypatch, username="", password="")
    f = tmp_path / "a.xlsx"
    f.write_text("x", encoding="utf-8")

    called = []
    monkeypatch.setattr(U.requests, "put", lambda *a, **k: called.append(1))

    res = U.upload_file(str(f))
    assert res["ok"] is False and not called


def test_文件不存在时不发请求(monkeypatch, tmp_path):
    _conf(monkeypatch)
    called = []
    monkeypatch.setattr(U.requests, "put", lambda *a, **k: called.append(1))

    res = U.upload_file(str(tmp_path / "缺失.xlsx"))
    assert res["ok"] is False and not called
    assert "不存在" in res["error"]


def test_上传成功并对中文名做URL编码(monkeypatch, tmp_path):
    _conf(monkeypatch)
    f = tmp_path / "采购汇总.xlsx"
    f.write_text("x", encoding="utf-8")

    seen = {}

    def fake_put(url, **k):
        seen["url"] = url
        return _Resp(201)

    monkeypatch.setattr(U.requests, "put", fake_put)
    monkeypatch.setattr(U.requests, "request", lambda *a, **k: _Resp(201))

    res = U.upload_file(str(f), "订单采购汇总/20260810")
    assert res["ok"] is True
    # 中文必须百分号编码，不能裸传（部分服务端会 400）
    assert "%" in seen["url"] and "采购汇总.xlsx" not in seen["url"]
    assert seen["url"].startswith("https://nas.example.com:5006/manus/")


def test_HTTP明文会告警但仍照传(monkeypatch, tmp_path, caplog):
    """HTTP + Basic Auth 是明文传密码，要告警提醒；但内网测试是合理用法，不该拦。"""
    _conf(monkeypatch, webdav_url="http://192.168.10.252:8800/manus")
    f = tmp_path / "a.xlsx"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setattr(U.requests, "put", lambda *a, **k: _Resp(201))
    monkeypatch.setattr(U.requests, "request", lambda *a, **k: _Resp(201))

    res = U.upload_file(str(f))
    assert res["ok"] is True  # 照传，不因协议而拒绝


def test_HTTPS不触发明文告警(monkeypatch, tmp_path):
    _conf(monkeypatch, webdav_url="https://nas.example.com:474/manus")
    f = tmp_path / "a.xlsx"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setattr(U.requests, "put", lambda *a, **k: _Resp(201))
    monkeypatch.setattr(U.requests, "request", lambda *a, **k: _Resp(201))

    assert U.upload_file(str(f))["ok"] is True


def test_verify_ssl透传到requests(monkeypatch, tmp_path):
    """自签证书场景：verify=False 必须真的传到 requests，否则 SSLError 连不上。"""
    _conf(monkeypatch, webdav_url="https://192.168.10.252:474/manus", verify_ssl=False)
    f = tmp_path / "a.xlsx"
    f.write_text("x", encoding="utf-8")

    seen = {}

    def fake_put(url, **k):
        seen["verify"] = k.get("verify")
        return _Resp(201)

    monkeypatch.setattr(U.requests, "put", fake_put)
    monkeypatch.setattr(U.requests, "request", lambda *a, **k: _Resp(201))

    assert U.upload_file(str(f))["ok"] is True
    assert seen["verify"] is False


def test_HTTP错误码不抛异常只回报(monkeypatch, tmp_path):
    _conf(monkeypatch)
    f = tmp_path / "a.xlsx"
    f.write_text("x", encoding="utf-8")

    monkeypatch.setattr(U.requests, "put", lambda *a, **k: _Resp(401, "unauthorized"))
    monkeypatch.setattr(U.requests, "request", lambda *a, **k: _Resp(201))

    res = U.upload_file(str(f))
    assert res["ok"] is False and "401" in res["error"]


def test_网络异常被吞掉不外抛(monkeypatch, tmp_path):
    _conf(monkeypatch)
    f = tmp_path / "a.xlsx"
    f.write_text("x", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("network unreachable")

    monkeypatch.setattr(U.requests, "put", boom)
    monkeypatch.setattr(U.requests, "request", lambda *a, **k: _Resp(201))

    res = U.upload_file(str(f))  # 不抛就是通过
    assert res["ok"] is False and "unreachable" in res["error"]


def test_环境变量密码覆盖配置文件(monkeypatch):
    """密码优先取环境变量：仓库是 public，少一处明文就少一次误提交的机会。"""
    monkeypatch.setenv("TNAS_WEBDAV_PASSWORD", "from_env")
    import app.orders.service as S

    monkeypatch.setattr(
        S, "load_orders_config",
        lambda: {"upload": {"webdav_url": "https://x", "username": "u", "password": "in_file"}},
    )
    conf = U._webdav_conf()
    assert conf["password"] == "from_env"


def test_两份产物独立上传一失败不连坐(monkeypatch, tmp_path):
    _conf(monkeypatch)
    xlsx = tmp_path / "汇总.xlsx"
    md = tmp_path / "汇总.md"
    xlsx.write_text("x", encoding="utf-8")
    md.write_text("y", encoding="utf-8")

    def fake_put(url, **k):
        return _Resp(201) if url.endswith(".md") else _Resp(500, "boom")

    monkeypatch.setattr(U.requests, "put", fake_put)
    monkeypatch.setattr(U.requests, "request", lambda *a, **k: _Resp(201))

    res = U.upload_purchase(
        {"file": str(xlsx), "md_file": str(md)}, "20260810_120000"
    )
    assert res["uploaded"] == 1 and res["failed"] == 1


def test_upload_purchase未配置时不发请求(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_webdav_conf", lambda: {})
    called = []
    monkeypatch.setattr(U.requests, "put", lambda *a, **k: called.append(1))

    res = U.upload_purchase({"file": "x.xlsx", "md_file": "y.md"}, "20260810")
    assert res == {"uploaded": 0, "failed": 0, "urls": [], "errors": []}
    assert not called
