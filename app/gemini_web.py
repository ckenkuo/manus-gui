"""Gemini 官网（gemini.google.com）的 CDP 对话客户端。

为什么走 CDP 驱动官网，而不是反代或官方 API：
- 反代（antigravity / Gemini Code Assist 账号池）会封号——2026-09-04 实测它返回
  Token pool is empty + 403 Verify your account，账号池耗尽，换个 key 也一样；
- 官方 API 要单独充值，且图片上传路径复杂；
- 本项目一贯「CDP 接管已登录真实 Chrome」——采集/订单/发布三条管线都靠它绕登录墙，
  这里同理：用户已在 Chrome 里登录 gemini.google.com，我们直接在那个页签里打字、发消息、
  读回答，不经过任何第三方网关，封号风险从根上绕开。

图片走「合成 paste 事件」而非找 file input（2026-09-04 实测）：
官网图片上传的 file input 是动态生成、accept 只列文档类（.txt/.pdf/.xlsx…，不含图），
而用户日常贴图就是「复制图片 Ctrl+V」。Gemini 的 Quill 输入框监听 paste、从
event.clipboardData 读图片文件，故这里构造 File → DataTransfer → ClipboardEvent 派发，
与真实粘贴走同一条处理链，图片成为输入框上方的附件 chip（blob 预览，120x120 实测生效）。
这条比逆向 file input 更贴合用户习惯、也更稳。

会话模型：单发对话客户端——每次 ask 建立一次 CDP 连接、用完断开（断开只断 CDP 连接，
不动用户的 Chrome 和页签）。独立入口阶段先跑通，暂不接发布管线的长流程会话，故不做
进程级长连接缓存；等确定要接进发布管线时，再参照 app/publish/browser.py 的
BrowserSession 改成长连接复用。

读取回答的正确姿势（踩坑记录）：官网回答是流式打字机，且页面会累积多条历史回答，
所以【必须按数量而非位置】取最新一条——发送前记下回答条数 N，发送后等条数变为 N+1
且「停止回答」按钮消失、新回答文本连续两次采样不变，才算完成。直接读「最后一条」在
新回答还没生成时会拿到上一条的旧文本（2026-09-04 首次验证时就因此把红图测试误读成
上一条的 "OK"）。
"""
import asyncio
import base64
import re
import time
from typing import List, Optional

from playwright.async_api import async_playwright

from app.config import config
from app.logger import logger

# 选择器与文案（2026-09-04 实测 gemini.google.com/app，中文界面）
GEMINI_HOST = "gemini.google.com"
EDITOR_SEL = 'div[contenteditable="true"][role="textbox"]'  # Quill 富文本输入框
SEND_LABEL = "发送"
STOP_LABEL = "停止回答"
# 回答正文渲染宿主；读最后一条回答靠「数量增加」而不是固定位置（见模块 docstring）
ANSWER_SEL = ".markdown.markdown-main-panel"

# 等待回答完成的默认超时（秒）。看图/长回答可能更久，调用方可覆盖。
DEFAULT_TIMEOUT = 180
# 回答稳定判据：连续这么多次采样（间隔 1s）文本不变才返回
_STABLE_SAMPLES = 2
# 贴图后等附件 chip 落地的时长（blob 预览异步生成，短了会赶在附件就绪前点发送）
_PASTE_SETTLE_MS = 1500
# 一次问话的附件（图片/文件）上限：官网硬限制，超了后端拒收。2026-09-04 用户实测探明。
# 发布管线看图阶段（阶段① 最多 20 张、阶段⑬ 单次 11 张）若未来接进来，须按此上限分批。
MAX_ATTACHMENTS = 10


class GeminiWebError(RuntimeError):
    """Gemini 官网对话失败（页签缺失、发送失败、超时等），带中文可操作提示。"""


def _normalize_cdp(url: str) -> str:
    """localhost 归一成 127.0.0.1：Chrome 的 --remote-debugging-port 只监听 IPv4，
    Windows 上 localhost 优先解析成 ::1，connect_over_cdp 会 EACCES（与
    app/publish/browser.py 同因）。"""
    return re.sub(r"//(localhost)(?=[:/]|$)", "//127.0.0.1", url)


def _cdp_url() -> str:
    return _normalize_cdp(
        getattr(config.browser_config, "cdp_url", None) or "http://127.0.0.1:9222"
    )


def _image_mime(path: str) -> str:
    """按文件真实格式返回 mime（看内容不看扩展名，与 publish/llm._mime_of 同取向）。

    合成 paste 时 File 的 type 若是错的，部分网关会判为损坏图；PIL 在依赖库里，
    直接用它读真实格式最省事。
    """
    try:
        from PIL import Image

        with Image.open(path) as im:
            fmt = (im.format or "").upper()
    except Exception:
        fmt = ""
    return {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "JPG": "image/jpeg",
        "WEBP": "image/webp",
        "GIF": "image/gif",
    }.get(fmt, "image/png")


class GeminiWebClient:
    """CDP 驱动 Gemini 官网的单发对话客户端。

    用法：
        async with GeminiWebClient() as g:
            text = await g.ask("把这段翻译成英文：……")
            text2 = await g.ask("这张图是什么颜色", images=["a.png"])
    """

    def __init__(self, cdp_url: Optional[str] = None):
        self.cdp_url = _normalize_cdp(cdp_url or _cdp_url())
        self._pw = None
        self._browser = None
        self._page = None

    async def __aenter__(self) -> "GeminiWebClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ---- 连接 ---------------------------------------------------------------
    async def connect(self) -> None:
        """连上已登录 Chrome，复用 gemini.google.com 页签（没有则报错让人先打开）。"""
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        if not self._browser.contexts:
            raise GeminiWebError(f"CDP 连上了但没有 context（{self.cdp_url}）")
        ctx = self._browser.contexts[0]
        cands = [p for p in ctx.pages
                 if GEMINI_HOST in (p.url or "") and not p.is_closed()]
        if not cands:
            raise GeminiWebError(
                "没有找到 gemini.google.com 页签。请先在已登录的 Chrome 里打开 "
                "https://gemini.google.com/app 并保持登录。")
        self._page = cands[0]
        await self._page.bring_to_front()

    async def close(self) -> None:
        """断开 CDP 连接，不关用户的浏览器和页签（best-effort，坏了不影响主流程）。"""
        for closer, what in (
            (lambda: self._browser.close() if self._browser else None, "browser"),
            (lambda: self._pw.stop() if self._pw else None, "playwright"),
        ):
            try:
                r = closer()
                if r is not None:
                    await r
            except Exception as e:
                logger.warning(f"断开 {what} 失败（忽略）：{e}")
        self._page = self._browser = self._pw = None

    # ---- 内部原语 -----------------------------------------------------------
    def _editor(self):
        return self._page.locator(EDITOR_SEL).first

    async def _clear_input(self) -> None:
        """聚焦并清空输入框文本。

        只清文本不清附件 chip：正常路径下每次发送后官网会连文本带附件一起自动清空，
        故这里只需兜住「上一次 ask 中途失败、文本残留」的情形。附件 chip 残留属于
        更罕见的异常，第一版不处理，遇到就人工点掉。
        """
        editor = self._editor()
        await editor.click()
        await self._page.keyboard.press("Control+a")
        await self._page.keyboard.press("Backspace")
        await self._page.wait_for_timeout(200)

    async def _paste_image(self, ref: str) -> None:
        """把图片合成 paste 事件注入输入框，成为附件 chip（见模块 docstring）。

        ref 既可以是本地文件路径，也可以是 data URL（发布管线 image_ref 的产物），
        这里统一归一成 base64 + mime 再塞给 Quill 的 paste 处理链。
        """
        if ref.startswith("data:"):
            header, b64 = ref.split(",", 1)
            mime = header.split(";", 1)[0].split(":", 1)[1]
        else:
            with open(ref, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            mime = _image_mime(ref)
        await self._page.evaluate(
            """([b64, mime]) => {
              const editor = document.querySelector('.ql-editor');
              if (!editor) throw new Error('输入框未找到');
              const bin = atob(b64);
              const bytes = new Uint8Array(bin.length);
              for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
              const file = new File([bytes], 'img', {type: mime});
              const dt = new DataTransfer();
              dt.items.add(file);
              const ev = new Event('paste', {bubbles: true, cancelable: true});
              Object.defineProperty(ev, 'clipboardData', {get: () => dt});
              editor.dispatchEvent(ev);
            }""",
            [b64, mime],
        )
        await self._page.wait_for_timeout(_PASTE_SETTLE_MS)

    async def _type_text(self, text: str) -> None:
        editor = self._editor()
        await editor.click()
        await self._page.keyboard.type(text)

    async def _click_send(self) -> None:
        await self._page.locator(f'button[aria-label="{SEND_LABEL}"]').first.click()

    async def _read_state(self) -> dict:
        """返回 {stopping, count, last}：是否仍在生成 + 回答总数 + 最后一条文本。"""
        return await self._page.evaluate(
            """() => {
              const stopping = [...document.querySelectorAll('button')]
                .some(b => (b.getAttribute('aria-label') || '').includes('停止回答'));
              const marks = [...document.querySelectorAll('.markdown.markdown-main-panel')]
                .map(m => (m.innerText || '').trim()).filter(Boolean);
              return {stopping, count: marks.length, last: marks[marks.length - 1] || ''};
            }"""
        )

    # ---- 对外接口 -----------------------------------------------------------
    async def ask(self, prompt: str, images: Optional[List[str]] = None,
                  timeout: float = DEFAULT_TIMEOUT) -> str:
        """发一条消息（可带图），返回 Gemini 的回答文本。

        images 传图片引用列表（本地文件路径或 data URL），与 prompt 作为同一条消息
        发出；延续当前对话（不新开会话）。返回的是【本次新增】的回答文本。
        一次最多传 MAX_ATTACHMENTS 个附件（官网硬限制），超出直接报错，由调用方分批。
        """
        if self._page is None:
            raise GeminiWebError("客户端未连接：先 await connect() 或用 async with")

        images = images or []
        if len(images) > MAX_ATTACHMENTS:
            raise GeminiWebError(
                f"一次最多传 {MAX_ATTACHMENTS} 个附件（官网硬限制），本次给了 "
                f"{len(images)} 个，请分批调用。")
        await self._clear_input()
        for img in images:
            await self._paste_image(img)
        if prompt:
            await self._type_text(prompt)
        await self._page.wait_for_timeout(300)
        before = await self._read_state()
        await self._click_send()
        return await self._wait_answer(timeout, before["count"])

    async def _wait_answer(self, timeout: float, before_count: int) -> str:
        """等新回答（条数超过 before_count）出现且文本稳定后返回（见模块 docstring）。"""
        deadline = time.time() + timeout
        last = ""
        stable = 0
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            st = await self._read_state()
            if st["count"] <= before_count:
                continue            # 新回答还没出现
            if st["stopping"]:
                stable = 0
                continue            # 仍在生成，清零稳定计数
            if not st["last"]:
                continue
            if st["last"] == last:
                stable += 1
                if stable >= _STABLE_SAMPLES:
                    return st["last"]
            else:
                last = st["last"]
                stable = 1
        raise GeminiWebError(
            f"等待回答超时（{timeout}s）。最后采样：{last[:200]!r}")
