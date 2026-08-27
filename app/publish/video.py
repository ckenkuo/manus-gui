# -*- coding: utf-8 -*-
"""产品视频合规化（发布管线阶段⑥b 用）：纯 ffmpeg 几何处理，无网络、无密钥、可离线单测。

【为什么需要这一步】视频不是我们传的，是阶段② 认领 1688 商品时平台连带搬过来的
（`/api/popTemuProduct/edit.json` 响应里的 `videoUrl`，指向淘宝 CDN）。1688 商品视频
绝大多数是 9:16 竖屏（2026-08-26 实测那批失败品：720×1280，比例 0.5625），而 Temu
只收 1:1 / 3:4 / 16:9，于是发布时报：

    上传视频接口报错:get video result response error :
    Video ratio should be 1:1 or 3:4 or 16:9, recommended ratio 1:1 or 3:4

【为什么这个报错特别坑】它出现在阶段⑮ 发布之后——前面 14 个阶段全绿、save 也落库了，
最后一步才被打回，且回执只说比例不对、不说是哪个视频。故必须在发布【之前】就把关。

【裁切而不是补黑边——与图片侧刻意相反】images.fit_34 对 SKC 图选的是补白边（裁切
可能把衣服切掉一截）。视频这里反过来选中心裁切，理由是平台明令「无黑边」（见编辑页
提示原文），补边出来的视频必然带边、等于用一条硬规则去换另一条。竖屏转 3:4 只裁掉
上下各约 12.5%，商品主体通常在画面中心，损失可接受。

【目标比例选 3:4 而非 1:1】平台建议「优先 1:1 或 3:4」，而源视频是竖屏，裁成 3:4
只丢上下 25%，裁成 1:1 要丢 44%——3:4 保留的画面最多，且同属推荐档。横屏源反过来
用 16:9（裁得最少）。选法见 pick_target_ratio。

【ffmpeg 二进制来自 imageio-ffmpeg 包，不依赖系统安装】这台机器 PATH 里没有 ffmpeg，
让用户去装系统 ffmpeg 等于给发布流程加一条环境前置条件。imageio-ffmpeg 自带
win_amd64 的 ffmpeg.exe（7.1），pip 装完即可用，与项目「依赖都 pin 在 requirements」
的取向一致。系统 PATH 里有 ffmpeg 时优先用系统的（版本通常更新），见 resolve_ffmpeg。

【为什么用子进程而不是 moviepy/av】只需要「读元数据 + 一次裁切转码」两件事，
命令行足够；moviepy 会把帧读进 Python 内存，处理 500M 上限的视频不现实。
"""
import json
import os
import shutil
import subprocess
from typing import Optional

from app.logger import logger

# ---- 平台硬规则（编辑页提示原文 + 2026-08-26 发布回执实测）--------------------
# 允许的宽高比：只有这三个，别的一律被上传视频接口打回
ALLOWED_RATIOS = {
    "1:1": 1.0,
    "3:4": 0.75,
    "16:9": 16 / 9,
}
# 判定容差：编码后的实际像素往往差一两个（如 720×960 恰好 0.75，但 719×960 就不是），
# 故用容差而不是精确相等。0.01 足够区分三档（相邻档差 0.25 以上）。
RATIO_TOLERANCE = 0.01
# 体积上限 500M；留 5% 余量，压到 475M 以内才算稳（平台按接收到的字节判）
MAX_SIZE_MB = 500
SAFE_SIZE_MB = 475
# 建议时长 1 分钟内。这是【建议】不是硬规则，故默认只告警不截断，
# 要截断由调用方显式传 max_seconds（见 normalize_video）
RECOMMEND_MAX_SECONDS = 60

# 编码参数：H.264 + AAC 是 Temu 最稳的组合；crf 23 是画质/体积的常规平衡点
VIDEO_CODEC = "libx264"
AUDIO_CODEC = "aac"
CRF = 23
PRESET = "medium"
# 裁切后的宽高必须是偶数，libx264 的 yuv420p 要求宽高都能被 2 整除，
# 奇数会直接报 "width not divisible by 2" 整个转码失败
EVEN = 2


def resolve_ffmpeg() -> str:
    """取 ffmpeg 可执行文件：系统 PATH 优先，回落到 imageio-ffmpeg 自带的二进制。

    刻意不在这里抛「请安装 ffmpeg」：imageio-ffmpeg 已在 requirements 里 pin，
    正常装完依赖就有。两条都找不到才抛，且把两条路径都写进错误信息里。
    """
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise RuntimeError(
            f"找不到 ffmpeg：系统 PATH 里没有，imageio-ffmpeg 也不可用（{e}）。"
            "装依赖即可：pip install imageio-ffmpeg==0.6.0"
        )


def resolve_ffprobe() -> Optional[str]:
    """取 ffprobe：只在系统 PATH 里找。

    imageio-ffmpeg【不带】ffprobe（只有 ffmpeg.exe），故这里可能返回 None——
    调用方要能在没有 ffprobe 时也读出元数据，见 probe_video 的两条路。
    """
    return shutil.which("ffprobe")


# ---- 元数据读取 --------------------------------------------------------------
# 【为什么不强依赖 ffprobe】imageio-ffmpeg 只带 ffmpeg.exe，没有 ffprobe。而 ffmpeg
# 在「给了 -i 但没给输出文件」时会把流信息打到 stderr 再以退出码 1 结束——那份文本里
# 就有宽高和时长（2026-08-26 实测：`Video: h264 ... 720x1280 [SAR 1:1 DAR 9:16]`、
# `Duration: 00:00:35.41`）。故 ffprobe 有就用（输出是 JSON，解析可靠），没有就退回
# 解析 ffmpeg 的 stderr。这不是 fallback 备用实现，是同一件事的两种可用数据源。
#
# 【必须避开 SAR/DAR 陷阱】`720x1280 [SAR 1:1 DAR 9:16]` 里三个数都像比例：
# 存储宽高是 720x1280，SAR 是像素长宽比，DAR 才是显示比例。SAR 非 1:1 时（老素材、
# 部分手机横拍）存储宽高算出的比例与观众看到的不一致，而平台校验的是【显示比例】。
# 故取 DAR 优先，没有 DAR 才用存储宽高——只按 720x1280 硬算会在 SAR≠1:1 的源上判错。
_RE_STREAM = None   # 延迟编译，见 _stream_re


def _stream_re():
    """惰性编译并缓存视频流行的正则（模块导入时不必付编译代价）。"""
    global _RE_STREAM
    if _RE_STREAM is None:
        import re
        _RE_STREAM = {
            # 宽高：`, 720x1280` 或 `, 720x1280 [SAR ...`。要求前面是逗号空格，
            # 免得把 `Video: h264` 后面别的数字对误当成分辨率
            "size": re.compile(r",\s(\d{2,5})x(\d{2,5})(?:[\s,\[]|$)"),
            # 显示比例 DAR：`[SAR 1:1 DAR 9:16]`
            "dar": re.compile(r"DAR\s+(\d+):(\d+)"),
            # 时长：`Duration: 00:00:35.41,`
            "dur": re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})(?:\.(\d+))?"),
        }
    return _RE_STREAM


def _run(args: list, timeout: int = 600) -> subprocess.CompletedProcess:
    """跑子进程并统一收字节输出。

    刻意不 text=True：ffmpeg 在 Windows 上的 stderr 可能夹带 GBK 无法解码的字符
    （源文件名带日文/emoji 时），text 模式会直接抛 UnicodeDecodeError，
    让「读个元数据」变成异常。收字节再按 utf-8 replace 解，坏字符退化成占位符。
    """
    return subprocess.run(args, capture_output=True, timeout=timeout)


def probe_video(path: str) -> dict:
    """读视频元数据，返回 {"ok", "w", "h", "ratio", "duration", "sizeMB", "reason"}。

    ratio 是【显示比例】（DAR 优先，见上方注释），平台校验的就是它。
    读不出来一律 ok=False 并说清原因——与 images.check_cloth_size 同一取向：
    与其让它传上去在发布时被静默打回，不如在这里就报明白。
    """
    if not os.path.exists(path):
        return {"ok": False, "reason": f"文件不存在：{path}"}
    size_mb = os.path.getsize(path) / (1024 * 1024)

    probe = resolve_ffprobe()
    if probe:
        p = _run([probe, "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=width,height,display_aspect_ratio",
                  "-show_entries", "format=duration",
                  "-of", "json", path], timeout=120)
        if p.returncode == 0:
            try:
                d = json.loads(p.stdout.decode("utf-8", "replace"))
                st = (d.get("streams") or [{}])[0]
                w, h = int(st.get("width") or 0), int(st.get("height") or 0)
                dur = float((d.get("format") or {}).get("duration") or 0)
                ratio = _ratio_from(w, h, st.get("display_aspect_ratio"))
                if w and h:
                    return {"ok": True, "w": w, "h": h, "ratio": ratio,
                            "duration": round(dur, 2), "sizeMB": round(size_mb, 2),
                            "reason": "", "source": "ffprobe"}
            except Exception as e:
                logger.warning(f"ffprobe 输出解析失败，改用 ffmpeg 读元数据：{e}")

    # ffprobe 不可用（imageio-ffmpeg 不带它）或解析失败：解析 ffmpeg 的 stderr
    ff = resolve_ffmpeg()
    p = _run([ff, "-hide_banner", "-i", path], timeout=120)
    txt = (p.stderr or b"").decode("utf-8", "replace")
    rex = _stream_re()
    vlines = [ln for ln in txt.splitlines() if "Video:" in ln]
    if not vlines:
        return {"ok": False, "sizeMB": round(size_mb, 2),
                "reason": f"ffmpeg 读不出视频流（文件损坏或不是视频）：{os.path.basename(path)}"}
    m = rex["size"].search(vlines[0])
    if not m:
        return {"ok": False, "sizeMB": round(size_mb, 2),
                "reason": f"ffmpeg 输出里找不到分辨率：{vlines[0].strip()[:160]}"}
    w, h = int(m.group(1)), int(m.group(2))
    dm = rex["dar"].search(vlines[0])
    dar = f"{dm.group(1)}:{dm.group(2)}" if dm else None
    ratio = _ratio_from(w, h, dar)
    dur = 0.0
    du = rex["dur"].search(txt)
    if du:
        frac = float("0." + du.group(4)) if du.group(4) else 0.0
        dur = int(du.group(1)) * 3600 + int(du.group(2)) * 60 + int(du.group(3)) + frac
    return {"ok": True, "w": w, "h": h, "ratio": ratio,
            "duration": round(dur, 2), "sizeMB": round(size_mb, 2),
            "reason": "", "source": "ffmpeg-stderr"}


def _ratio_from(w: int, h: int, dar: Optional[str]) -> Optional[float]:
    """算显示比例：DAR 可用就用 DAR，否则用存储宽高。

    DAR 形如 "9:16"。ffprobe 在 SAR=1:1 时会给出 "0:1" 这种无意义值（表示未知），
    故分母为 0 或分子为 0 都视作没有 DAR。
    """
    if dar and ":" in dar:
        try:
            a, b = dar.split(":", 1)
            a, b = int(a), int(b)
            if a > 0 and b > 0:
                return round(a / b, 4)
        except Exception:
            pass
    return round(w / h, 4) if (w and h) else None


# ---- 合规判定 ---------------------------------------------------------------

def match_ratio(ratio: Optional[float]) -> Optional[str]:
    """比例落在哪一档允许值上；都不匹配返回 None。"""
    if not ratio:
        return None
    for name, target in ALLOWED_RATIOS.items():
        if abs(ratio - target) < RATIO_TOLERANCE:
            return name
    return None


def check_video(path: str) -> dict:
    """校验视频是否符合 Temu 硬规则，返回 {"ok", "meta", "ratioName", "reason", "issues"}。

    【只拦不改】与 images.check_cloth_size 同构：本函数不动文件，只报「差在哪」，
    改由 normalize_video 做。这样上传前的把关与「怎么修」解耦，失败时分得清是哪一层。

    时长超 1 分钟只进 issues 不判 ok=False：平台原文是「建议时长在1分钟内」，
    是建议不是硬规则，把它当硬规则会让一堆本可发布的视频被自己拦下。
    """
    meta = probe_video(path)
    if not meta.get("ok"):
        return {"ok": False, "meta": meta, "ratioName": None,
                "reason": meta.get("reason") or "读不出视频元数据", "issues": ["unreadable"]}

    issues, reasons = [], []
    name = match_ratio(meta.get("ratio"))
    if not name:
        issues.append("ratio")
        reasons.append(
            f"宽高比不合规：{meta['w']}×{meta['h']} 比例 {meta['ratio']}，"
            f"平台只收 1:1 / 3:4 / 16:9"
        )
    if meta.get("sizeMB", 0) > MAX_SIZE_MB:
        issues.append("size")
        reasons.append(f"体积超限：{meta['sizeMB']}MB > {MAX_SIZE_MB}MB")
    if meta.get("duration", 0) > RECOMMEND_MAX_SECONDS:
        # 只提示不拦：见 docstring
        issues.append("duration-warn")

    return {"ok": not [i for i in issues if i != "duration-warn"],
            "meta": meta, "ratioName": name,
            "reason": "；".join(reasons), "issues": issues}


def pick_target_ratio(ratio: Optional[float]) -> str:
    """给源比例挑「裁得最少」的目标档位。

    因为裁切必然丢画面，选离源比例最近的那一档就是丢得最少的：
      竖屏 9:16（0.5625）→ 3:4（0.75），上下各裁 12.5%
      正方附近 → 1:1，几乎不裁
      横屏 → 16:9
    竖屏若强行裁成 1:1 要丢 44% 的高度，商品下半身会被切掉。

    读不出比例时按 3:4：源是 1688 服装视频，竖屏占绝大多数，且 3:4 属平台推荐档。
    """
    if not ratio:
        return "3:4"
    return min(ALLOWED_RATIOS, key=lambda k: abs(ratio - ALLOWED_RATIOS[k]))


def _even(n: int) -> int:
    """向下取到偶数且至少 2：libx264 的 yuv420p 要求宽高可被 2 整除。"""
    n = int(n)
    return max(EVEN, n - (n % EVEN))


def crop_box(w: int, h: int, target: float) -> tuple:
    """算中心裁切框，返回 (cw, ch, x, y)，全部为偶数。

    比源「更宽」的目标 → 裁高；「更窄」的目标 → 裁宽。取整后可能出现 cw/ch 比源大
    一两个像素（浮点向上），故最后夹到源尺寸内——ffmpeg 的 crop 超出画面会直接报错。
    """
    src = w / h
    if abs(src - target) < 1e-9:
        cw, ch = w, h
    elif src > target:          # 源更宽 → 裁掉左右
        cw, ch = round(h * target), h
    else:                        # 源更高 → 裁掉上下
        cw, ch = w, round(w / target)
    cw, ch = _even(min(cw, w)), _even(min(ch, h))
    return cw, ch, (w - cw) // 2, (h - ch) // 2


# ---- 下载与转码 -------------------------------------------------------------
# 【为什么用 curl.exe 下载而不是 requests】与 images.py / upload.py 同一个理由：
# 淘宝 CDN（cloud.video.taobao.com）对 HEAD 直接回 490，且 videoUrl 会 302 跳到
# caiyuanbao.alicdn.com 带签名的真实地址（2026-08-26 实测）。curl 的 -L 跟跳、
# 且不必把视频字节读进 Python 内存。requests 的 TLS 指纹在这条链上同样容易被拦。
#
# 【必须带浏览器 UA】与「主图下载必须带浏览器头」同一条已知陷阱，裸请求会被拦。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def download_video(url: str, out_path: str, timeout: int = 300) -> dict:
    """下载视频到本地，返回 {"status", "path", "sizeMB", "err"}。

    -L 跟 302（淘宝 CDN 必跳），-f 让 HTTP 错误码变成非零退出码（否则 curl 会把
    错误页当成文件写下来，后面 ffmpeg 才报「不是视频」，错误信息指不到根因）。
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    p = _run(["curl.exe", "-sSL", "-f", "--max-time", str(timeout),
              "-H", f"User-Agent: {_UA}",
              "-o", out_path, url], timeout=timeout + 30)
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace")[:200]
        return {"status": "error", "path": out_path, "err": f"下载失败 rc={p.returncode}: {err}"}
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return {"status": "error", "path": out_path, "err": "下载得到空文件"}
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    logger.info(f"视频下载完成：{os.path.basename(out_path)} {size_mb:.2f}MB")
    return {"status": "ok", "path": out_path, "sizeMB": round(size_mb, 2), "err": ""}


def normalize_video(path: str, out_path: Optional[str] = None,
                    target_ratio: str = "", max_seconds: int = 0,
                    crf: int = CRF) -> dict:
    """把视频裁成平台允许的比例，返回 {"status", "output", "meta", "action", ...}。

    【已合规就不重编码】check_video 通过时直接返回 action="skip"：重编码必然掉画质，
    对本来就合规的视频做一遍纯属倒扣分。这也让本函数对续跑幂等。

    target_ratio 不给就按 pick_target_ratio 自动挑「裁得最少」的档位。
    max_seconds 给了才截断（平台的 1 分钟是建议不是硬规则，见模块顶部注释）；
    截断取前 max_seconds 秒——商品视频的卖点一般在开头（平台自己的建议也是
    「前5s内突出核心卖点」），从头截比从中间截合理。

    转码失败直接返回 error 不改原文件：视频是平台连带搬来的既有数据，
    我们宁可如实报「这个视频处理不了」，也不要留个半截文件让上传阶段拿去传。
    """
    meta = probe_video(path)
    if not meta.get("ok"):
        return {"status": "error", "action": "probe", "meta": meta,
                "err": meta.get("reason") or "读不出视频元数据"}

    chk = check_video(path)
    need_trim = bool(max_seconds) and meta.get("duration", 0) > max_seconds
    if chk["ok"] and not need_trim:
        logger.info(f"视频已合规（{meta['w']}×{meta['h']} {chk['ratioName']}），跳过转码")
        return {"status": "ok", "action": "skip", "output": path, "meta": meta,
                "ratioName": chk["ratioName"]}

    name = target_ratio or pick_target_ratio(meta.get("ratio"))
    if name not in ALLOWED_RATIOS:
        return {"status": "error", "action": "target",
                "err": f"目标比例 {name} 不在允许集合 {sorted(ALLOWED_RATIOS)} 里"}
    target = ALLOWED_RATIOS[name]
    w, h = meta["w"], meta["h"]
    cw, ch, x, y = crop_box(w, h, target)
    out_path = out_path or os.path.splitext(path)[0] + f"-{name.replace(':', 'x')}.mp4"

    ff = resolve_ffmpeg()
    args = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", path]
    if need_trim:
        # -t 放在输出侧：放输入侧（-ss/-t 在 -i 前）是按关键帧粗切，
        # 输出侧才是精确到帧的截断
        args += ["-t", str(max_seconds)]
    args += [
        # crop 与 setsar 必须写在【同一个 -vf】里：给两个 -vf 时后一个覆盖前一个，
        # 只留下的那个会让另一半静默失效。
        # setsar=1 把像素长宽比归一：源 SAR 非 1:1 时，只 crop 出来的显示比例仍然
        # 不等于 cw/ch，平台照样打回（见 _ratio_from 上方的 SAR/DAR 陷阱）
        "-vf", f"crop={cw}:{ch}:{x}:{y},setsar=1",
        "-c:v", VIDEO_CODEC, "-crf", str(crf), "-preset", PRESET,
        "-pix_fmt", "yuv420p",
        "-c:a", AUDIO_CODEC, "-b:a", "128k",
        # +faststart 把 moov 挪到文件头：平台/浏览器边下边播要靠它，
        # 也让上传后的首帧提取不必拉完整个文件
        "-movflags", "+faststart",
        out_path,
    ]

    logger.info(f"视频转码：{w}×{h}（{meta.get('ratio')}）→ {cw}×{ch}（{name}）"
                + (f"，截断到 {max_seconds}s" if need_trim else ""))
    p = _run(args, timeout=1800)
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace")[:400]
        logger.error(f"ffmpeg 转码失败 rc={p.returncode}: {err}")
        return {"status": "error", "action": "encode", "err": err,
                "meta": meta, "cmd": " ".join(args[1:])}

    # 【转码完必须回读校验】与 images 侧「写入即回读」同一取向：ffmpeg 返回 0
    # 不等于出来的比例真对（crop 参数算错、SAR 没归一都会返回 0）。
    out_meta = probe_video(out_path)
    out_name = match_ratio(out_meta.get("ratio"))
    if out_name != name:
        return {"status": "error", "action": "readback", "meta": meta, "outMeta": out_meta,
                "err": (f"转码后比例仍不合规：期望 {name}，实得 "
                        f"{out_meta.get('w')}×{out_meta.get('h')} 比例 {out_meta.get('ratio')}")}
    if out_meta.get("sizeMB", 0) > SAFE_SIZE_MB:
        return {"status": "error", "action": "size", "meta": meta, "outMeta": out_meta,
                "err": (f"转码后体积 {out_meta['sizeMB']}MB 超过安全线 {SAFE_SIZE_MB}MB，"
                        f"请调高 crf（当前 {crf}）或用 max_seconds 截断")}
    logger.info(f"视频合规化完成：{os.path.basename(out_path)} "
                f"{out_meta['w']}×{out_meta['h']} {name} {out_meta['sizeMB']}MB")
    return {"status": "ok", "action": "crop", "output": out_path, "meta": meta,
            "outMeta": out_meta, "ratioName": name,
            "crop": {"w": cw, "h": ch, "x": x, "y": y}, "trimmed": need_trim}
