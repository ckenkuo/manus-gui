"""把订单管线的采购汇总产物通过 WebDAV 上传到 TNAS。

为什么走 WebDAV 而不是 SMB：SMB(445) 是内网协议，直连公网会被大规模扫描器和勒索软件
盯上，绝不能为了外网上传去做端口转发。WebDAV 是 HTTP 之上的协议，配 HTTPS 就能安全
穿公网，TOS 后台自带该服务，不用在 NAS 上额外装东西。

端口（2026-08-10 实测本机）：HTTP=8800、HTTPS=474。这是 TOS 后台可自定义的，不是固定值，
常见文档写的默认 5005/5006 在本机并不适用。配置前先探测你实际的端口。

注意 HTTP 与 HTTPS 的差别不只是加密：Basic Auth 把账号密码 base64 后放在请求头里，
base64 不是加密，HTTP 下等于明文。所以 HTTP 的 WebDAV 只能内网用，公网必须 HTTPS，
upload_file 里对 http:// 会打警告提醒这件事。

自签证书（2026-08-10 实测）：TOS 出厂证书是 TerraMaster 自签（CN=webdav，2040 到期），
requests 默认 verify=True 会直接 SSLError，必须在配置里 verify_ssl=false 才能连上。
代价是失去中间人防护——公网长期用建议换正规证书（Let's Encrypt），换好后改 verify_ssl=true。

内网 IP vs 公网域名：
  - 内网 IP（如 https://192.168.10.252:474/manus）：只在家里网络可用，速度快
  - 公网域名（如 https://你的域名:474/manus）：外网可用，需 DDNS 或反向代理
  从内网 IP 切换到公网域名时只需改 webdav_url，其他字段（username/password/verify_ssl）不变。

为什么不用 TNAS.online 的中继地址：那是给浏览器和官方客户端用的网页会话，没有稳定的
文件上传 API；脚本要的是能 PUT 的 URL。所以上传走 WebDAV 域名/DDNS，人看文件仍可以
继续用 TNAS.online，两者互不影响。

为什么整段 best-effort：上传是登记表写入之后的归档动作，NAS 不在线、证书过期、密码改了
都不该让已经写好的登记表算作失败（那会让人误以为要重跑，而重跑会触发判重逻辑）。所以
所有异常一律 logger.warning 吞掉，把结果放进返回值供 UI 展示，绝不往上抛。
"""

import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth

from app.logger import logger


# 上传超时：连接 10 秒足够判断域名/端口通不通；读超时给 120 秒——采购汇总带嵌图，
# 几十兆的 xlsx 走家用宽带上行并不快，给太短会在快传完时被掐断。
_TIMEOUT = (10, 120)


def _webdav_conf() -> dict:
    """读 [orders.upload] 段。密码支持从环境变量取，避免明文写进 config.toml。

    与 load_orders_config 一样按段回退到 example，但这里**不**回退：上传目标是每台机器
    各自的 NAS，example 里给不出有意义的默认值，缺配置就是「没开这个功能」。
    """
    from app.orders.service import load_orders_config

    conf = (load_orders_config().get("upload") or {}).copy()
    # 密码优先取环境变量：config.toml 虽然 gitignored，但仓库是 public，
    # 少一处明文就少一次误提交的机会。
    env_pwd = os.environ.get("TNAS_WEBDAV_PASSWORD")
    if env_pwd:
        conf["password"] = env_pwd
    return conf


def upload_file(local_path: str, remote_subdir: str = "") -> dict:
    """把单个文件 PUT 到 TNAS 的 WebDAV 目录，返回 {ok, url, error}。

    remote_subdir 用于按日期归档（与本地 _purchase_out_dir 的 <YYYYMMDD>/ 保持一致），
    这样 NAS 上的目录结构和桌面上的一模一样，人找文件不用换脑子。

    WebDAV 的 PUT 不会自动建父目录，缺目录会返回 409，所以先逐级 MKCOL。
    """
    result = {"ok": False, "url": "", "error": ""}

    conf = _webdav_conf()
    base = (conf.get("webdav_url") or "").rstrip("/")
    user = conf.get("username") or ""
    pwd = conf.get("password") or ""

    if not base:
        result["error"] = "未配置 [orders.upload].webdav_url，跳过上传"
        logger.info(result["error"])
        return result
    if not user or not pwd:
        result["error"] = "未配置 WebDAV 账号或密码（密码可用环境变量 TNAS_WEBDAV_PASSWORD）"
        logger.warning(result["error"])
        return result

    # HTTP + Basic Auth = 账号密码 base64 明文传输，能被中间人直接读。
    # 内网测试可以接受；外网上传必须换 HTTPS，否则等于把密码交给路由上的每一跳。
    if base.startswith("http://"):
        logger.warning(
            "WebDAV 用 HTTP 而非 HTTPS：账号密码以 base64 明文传输，仅适合内网测试。"
            "公网上传必须在 TOS 后台勾上 HTTPS 并改 webdav_url 为 https://。"
        )

    src = Path(local_path)
    if not src.exists():
        result["error"] = f"待上传文件不存在：{local_path}"
        logger.warning(result["error"])
        return result

    auth = HTTPBasicAuth(user, pwd)
    # verify 默认开：TOS 自签证书的话用户得显式在配置里关，不默认放行中间人
    verify = conf.get("verify_ssl", True)

    # 关了校验就顺手压掉 urllib3 的 InsecureRequestWarning：一次上传要发多个请求
    # （逐级 MKCOL + PUT），每个都警告会把跑批日志刷满，而这是用户在配置里显式选的、
    # 已经在 config 注释里讲清代价的取舍，重复喊没有信息量。
    if not verify:
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass  # 压警告失败无所谓，不该影响上传

    try:
        # 逐级建目录：WebDAV 的 MKCOL 不递归，一层层来。已存在会返回 405，当成功处理。
        if remote_subdir:
            parts = [p for p in remote_subdir.strip("/").split("/") if p]
            for i in range(len(parts)):
                seg = "/".join(quote(p) for p in parts[: i + 1])
                r = requests.request(
                    "MKCOL", f"{base}/{seg}", auth=auth, timeout=_TIMEOUT, verify=verify,
                )
                # 201=建好 405=已存在，其余码不中断：可能是权限只给了写文件不给建目录，
                # 那就让后面的 PUT 去试，真不行 PUT 会报更准确的错。
                if r.status_code not in (201, 405):
                    logger.debug(f"MKCOL {seg} 返回 {r.status_code}")

        remote_name = quote(src.name)
        prefix = "/".join(quote(p) for p in remote_subdir.strip("/").split("/") if p)
        url = f"{base}/{prefix}/{remote_name}" if prefix else f"{base}/{remote_name}"

        with src.open("rb") as f:
            r = requests.put(url, data=f, auth=auth, timeout=_TIMEOUT, verify=verify)

        # 201=新建 204/200=覆盖已有，都算成功
        if r.status_code in (200, 201, 204):
            result.update(ok=True, url=url)
            logger.info(f"已上传到 TNAS：{src.name} -> {url}")
        else:
            result["error"] = f"PUT 返回 {r.status_code}：{r.text[:200]}"
            logger.warning(f"上传 {src.name} 失败：{result['error']}")
    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"上传 {src.name} 到 TNAS 失败（不影响登记表写入）：{e}")

    return result


def upload_purchase(purchase: dict, stamp: str = "") -> dict:
    """上传本批采购汇总的 xlsx + md，返回 {uploaded, failed, urls, errors}。

    两份各自独立上传：一个失败不该连坐另一个（与 _export_purchase 里两份独立 try 同理）。
    远端按 <YYYYMMDD>/ 归档，日期取自 stamp 前缀，与本地 _purchase_out_dir 口径一致。
    """
    summary = {"uploaded": 0, "failed": 0, "urls": [], "errors": []}

    conf = _webdav_conf()
    if not conf.get("webdav_url"):
        return summary  # 没配就是没开这个功能，安静跳过，不刷警告

    date = stamp[:8] if stamp[:8].isdigit() else ""
    # 远端根目录名与本地分类目录同名，NAS 上一眼能对上
    remote_dir = f"订单采购汇总/{date}" if date else "订单采购汇总"

    for key in ("file", "md_file"):
        path = purchase.get(key) or ""
        if not path:
            continue
        res = upload_file(path, remote_dir)
        if res["ok"]:
            summary["uploaded"] += 1
            summary["urls"].append(res["url"])
        else:
            summary["failed"] += 1
            summary["errors"].append(res["error"])

    return summary
