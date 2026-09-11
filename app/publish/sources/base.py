# -*- coding: utf-8 -*-
"""来源适配层的公共契约：平台识别 + 中间结构 SourceProduct。

本模块【刻意不 import browser】：collectbox 扫采集箱时要给每一行判平台（决定前端
能不能走全流程），那是纯字符串判断，不该为此拉起 Playwright 依赖链。适配器本身
（各平台的取数实现）才需要浏览器，故分在兄弟模块里，由 sources.get_adapter 延迟导入。
"""
import asyncio
import re
from dataclasses import dataclass, field

from app.logger import logger


class UnsupportedSourceError(ValueError):
    """URL 的域名不在支持的平台白名单里。

    单独一个异常类型而不是 ValueError：阶段① 要能把它与「URL 里抽不出商品 ID」
    区分开——前者是「这个平台还没做适配」（用户得换个来源或等开发），后者是
    「链接残缺」（用户改链接就能跑）。两种处置完全不同，UI 上的提示也不同。
    """


# 平台白名单：域名后缀 → 平台名。
# 【为什么按后缀匹配而不是全等】同一平台有多个域名与多国站点，实测见过：
#   1688     detail.1688.com（桌面）、m.1688.com（移动）
#   拼多多   mobile.pinduoduo.com、mobile.yangkeduo.com（yangkeduo 是拼多多的老域名，
#            实测采集箱里 2 条走的是它，漏掉会让这些行被判成「不支持」）
#   Temu     www.temu.com 下按 /<区域>-<语言>/ 分路径（co-en、jp-zh-Hans…），域名不变
#   亚马逊   amazon.com / amazon.com.au / amazon.co.jp … 各国站点后缀各异，故用
#            「amazon.」前缀 + 顶级域宽松匹配（见 _AMAZON_RE），不逐国枚举
# 【顺序有讲究】按最长后缀优先匹配，避免 "1688.com" 命中到某个包含它的更长域名。
_HOST_MAP = {
    "detail.1688.com": "1688",
    "m.1688.com": "1688",
    "1688.com": "1688",
    "mobile.pinduoduo.com": "pdd",
    "pinduoduo.com": "pdd",
    "mobile.yangkeduo.com": "pdd",
    "yangkeduo.com": "pdd",
    "temu.com": "temu",
}

# 亚马逊各国站点：amazon.<tld> 或 amazon.<tld>.<cc>（amazon.com.au / amazon.co.jp）。
# 不枚举国家：站点几十个且会新增，枚举必然漏（漏了的表现是整条来源被判「不支持」）。
_AMAZON_RE = re.compile(r"(?:^|\.)amazon\.[a-z]{2,3}(?:\.[a-z]{2})?$", re.I)

# 各平台的商品 ID 抽取规则。ID 用来建工作目录名（product-<id>/）与状态文件键，
# 故必须【同一商品每次都抽出同一个值】——带 URL 参数的链接（拼多多的
# _oak_rcto/_x_query 一长串）也要稳定，所以一律从固定位置抽、不做兜底猜数字。
#
# 【不许用「随便找 6 位以上数字」兜底】service.py 的 _task_key 就是这么写的，
# 一个 temu URL 里的 refer_page_id 时间戳会被当成商品 ID，两个不同商品可能撞到
# 同一个状态文件键。这里各平台都有明确的参数名/路径段，没有猜的必要。
_ID_RULES = {
    # 1688：/offer/<数字>.html
    "1688": (re.compile(r"/offer/(\d{6,})"),),
    # 拼多多：goods_id=<数字> 查询参数（goods.html 与 goods1.html 等变体都用它）
    "pdd": (re.compile(r"[?&]goods_id=(\d{6,})"),),
    # Temu：路径末尾 -g-<数字>.html，实测形如 .../girls-princess-dresses-g-605936466903004.html
    # 备用 goods_id=<数字>（分享短链跳转后的形态）
    "temu": (re.compile(r"-g-(\d{6,})\.html"), re.compile(r"[?&]goods_id=(\d{6,})")),
    # 亚马逊：ASIN 是 10 位大写字母数字，在 /dp/<ASIN> 或 /gp/product/<ASIN>
    "amazon": (re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})"),),
}

# 各平台的商品页规范形态：只保留 ID，丢掉一切跟踪参数。
# 【为什么要归一】拼多多链接带 60+ 个字符的 _oak_rcto 与搜索词参数，Temu 带
# refer_page_id 时间戳。带着这些参数导航没有坏处，但它们会进 product-info.json 的
# source.url 与日志，让同一商品的两次抓取看起来是两个不同来源；而人工排查时想复现
# 也没法直接用（搜索词参数过期后跳首页）。故一律归一成最短可访问形态。
#
# Temu 是例外：它的 /<区域>-<语言>/ 路径段决定了页面语言与币种，丢掉会跳到默认区域，
# 拿到的价格币种就变了（实测 jp-zh-Hans 给日元）。故 Temu 保留区域段（见 normalize_url）。
_TEMU_REGION_RE = re.compile(r"^/([a-z]{2}(?:-[A-Za-z-]+)?)/")


def detect_platform(url: str) -> str:
    """URL → 平台名（1688 / pdd / temu / amazon）。认不出抛 UnsupportedSourceError。

    只看域名，不看路径也不看参数：域名是平台的唯一可靠标识。空 URL 也抛——
    调用方（collectbox 的行渲染）要区分「没有来源链接」和「来源平台不支持」，
    但那是调用方的语义，本函数不替它兜（见 platform_of 的宽松版本）。
    """
    host = _host_of(url)
    if not host:
        raise UnsupportedSourceError(f"URL 里没有域名：{url!r}")
    if _AMAZON_RE.search(host):
        return "amazon"
    # 最长后缀优先：把 map 的键按长度降序试，"detail.1688.com" 先于 "1688.com"
    for suffix in sorted(_HOST_MAP, key=len, reverse=True):
        if host == suffix or host.endswith("." + suffix):
            return _HOST_MAP[suffix]
    raise UnsupportedSourceError(f"来源平台暂不支持：{host}")


def platform_of(url: str) -> str:
    """detect_platform 的宽松版：认不出返回空串，不抛。

    给「列一整页采集箱，每行标个平台」这类批量场景用——一行认不出不该让整次扫描失败，
    而调用方拿到空串就显示「未知源」（collectbox 的来源列就是这么用的）。
    """
    try:
        return detect_platform(url)
    except UnsupportedSourceError:
        return ""


def source_id(url: str, platform: str = "") -> str:
    """抽商品 ID（1688 offerId / 拼多多 goods_id / Temu goodsId / 亚马逊 ASIN）。

    抽不到返回空串而不抛：调用方对「认得出平台但抽不出 ID」有不同处置——阶段①
    要报错（没 ID 建不了工作目录），而采集箱列表只是少显示一个 ID。
    """
    platform = platform or platform_of(url)
    for rule in _ID_RULES.get(platform, ()):
        m = rule.search(url or "")
        if m:
            return m.group(1)
    return ""


def normalize_url(url: str, platform: str = "") -> str:
    """商品页 URL 归一成最短形态（丢跟踪参数），用作【标识】而非访问地址。

    【绝不要拿归一结果去导航】2026-08-27 实测两个反例：
      Temu   丢掉标题 slug（.../girls-dresses-g-<id>.html → .../g-<id>.html）后导航，
             被 302 到 login.html，window.rawData 压根不注入
      拼多多 只留 goods_id、丢掉 _oak_rcto 等参数后导航，initDataObj 也不注入
    平台把 URL 里那些看着像「跟踪参数」的东西当会话/来源凭证用。故归一只服务于
    日志、product-info.json 的 source.url、工作目录名与状态键这类【标识】场景；
    要访问页面一律用用户给的原始 URL（见 extract.extract_product 里的处理）。

    认不出平台或抽不出 ID 时原样返回：归一是为了日志与复现好看，不是正确性前提，
    坏了不该阻断提取（本项目 best-effort 取向）。
    """
    platform = platform or platform_of(url)
    gid = source_id(url, platform)
    if not gid:
        return url
    if platform == "1688":
        return f"https://detail.1688.com/offer/{gid}.html"
    if platform == "pdd":
        # 【保留原域名，不要统一到某一个】2026-08-27 实测：把 mobile.pinduoduo.com 的
        # 链接改写成 mobile.yangkeduo.com 再导航，会撞上「请在拼多多 App 打开」的
        # 验证中间页，goods.html 的 SSR 数据压根不注入——而原域名直接可读。
        # 两个域名不是纯别名，各自的风控策略不同，故只丢跟踪参数、不动 host。
        return f"https://{_host_of(url)}/goods.html?goods_id={gid}"
    if platform == "temu":
        # 保留区域-语言段：它决定页面语言与价格币种（实测 jp-zh-Hans 给日元），
        # 丢掉会跳默认区域、抓到的价格换了币种。抽不到就用 us-en 兜（不是猜测：
        # Temu 无区域段时服务端本身就按 IP 跳，写死一个至少可复现）。
        m = _TEMU_REGION_RE.match(_path_of(url))
        region = m.group(1) if m else "us-en"
        return f"https://www.temu.com/{region}/g-{gid}.html"
    if platform == "amazon":
        # 保留原站点域名：amazon.com.au 与 amazon.com 是不同站点、不同价格与库存，
        # 归一到 .com 会抓错商品（同 ASIN 在别国站点可能压根不存在）。
        return f"https://{_host_of(url)}/dp/{gid}"
    return url


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/?#]+)", (url or "").strip(), re.I)
    return m.group(1).lower() if m else ""


def _path_of(url: str) -> str:
    m = re.match(r"https?://[^/?#]+(/[^?#]*)", (url or "").strip(), re.I)
    return m.group(1) if m else "/"


# 平台的中文显示名：日志、UI 来源列、product-info.json 的 source.platformName 都用它。
# 【product-info.json 的 source.platform 存英文键名而不是中文】它是数据字段、要能当
# 字典键与文件名片段用；中文只在给人看的地方出现。
PLATFORM_NAMES = {
    "1688": "1688",
    "pdd": "拼多多",
    "temu": "Temu",
    "amazon": "亚马逊",
}


def platform_name(platform: str) -> str:
    return PLATFORM_NAMES.get(platform, platform or "未知")


@dataclass
class SourceProduct:
    """各平台适配器的统一产出：「这个商品页上有什么」，不含任何 Temu 侧的要求。

    与 product-info.json 的差别（刻意的）：
      - 没有 mainComposition —— 那是从 attributes 解析出来的 Temu 成分行素材，
        规则由 extract.parse_main_composition 统一实现，各平台不该各写一份
      - 没有 colors/sizes —— 由 pivot_skus 从 skuMap 透视，同上
      - 没有 sizeChart/imageUnderstanding/complianceNotes —— 那是视觉回填的产物

    字段语义：
      platform      平台名（1688 / pdd / temu / amazon），进 product-info.json 的
                    source.platform。【必须由适配器显式给】不从 url 反推：适配器
                    知道自己是谁，反推等于把 detect_platform 的结论再猜一遍
      url           归一后的商品页 URL（见 base.normalize_url）
      productId     平台侧商品 ID（1688 offerId / 拼多多 goods_id / Temu goodsId /
                    亚马逊 ASIN）。工作目录名与状态文件键都用它，故必须稳定
      title         商品标题（源语言原样，不翻译——阶段⑤ 要看源标题定核心品类词）
      attributes    商品属性 {中文键: 值}。各平台的键名【不做跨平台归一】：阶段④ 的
                    属性审核是把整个 attributes 塞进提示词交模型对齐到 Temu 的属性行，
                    强行归一反而会丢信息（如拼多多的「填充物」在 1688 键表里没有对应）
      skuMap        [{spec, price, stock}]，spec 形如「颜色>尺码」。单规格商品也要给
                    一条（spec 用「默认>默认尺码」不行——见各适配器对无规格商品的处理）
      mainImages    轮播主图 URL 列表，顺序即页面顺序（首图会成为 main-01.jpg，
                    而店小秘素材图取的就是 main-01）
      descImages    详情长图 URL 列表
      descText      详情描述里的【纯文字】（剥掉标签后的可读文本），拿不到给空串。
                    2026-09-01 取证（offer 971999094281 韩系牛仔外套）：有些商家把
                    整张尺码表直接打在详情文字里而不是做成图（「S 衣长59 胸围118
                    袖长57 肩宽52」三行），而原先详情接口的 HTML 只被正则抠了 <img>、
                    文字整段丢弃，于是 sizeMeasurements 落成空、阶段⑨ 两列全靠估算。
                    只做「把文字留下来」，怎么解析是 extract 侧的事（见 enrich_desc_text）
      unitWeightKg  单件重量（千克）。拿不到给 None——阶段⑩ 会转 LLM 预估
      videoUrl      主视频 URL，没有给空串
      extra         平台专属的附加信息，原样进 raw.json 留档便于排查（如亚马逊的
                    跨 ASIN 变体表、Temu 的 mall 信息）。下游不读它
    """
    platform: str = ""
    url: str = ""
    productId: str = ""
    title: str = ""
    attributes: dict = field(default_factory=dict)
    skuMap: list = field(default_factory=list)
    mainImages: list = field(default_factory=list)
    descImages: list = field(default_factory=list)
    descText: str = ""
    unitWeightKg: float = None
    videoUrl: str = ""
    extra: dict = field(default_factory=dict)

    def as_raw(self) -> dict:
        """转成 raw.json 里的形状（留档用，键名沿用 1688 时代的 raw.json）。

        沿用旧键名（subject/images/descImages/unitWeight/skuMap）是为了让既有的
        raw.json 与新平台的 raw.json 能用同一套眼睛看——排查时不必先分辨这是哪个
        平台的档。
        """
        return {
            "platform": self.platform,
            "url": self.url,
            "productId": self.productId,
            "subject": self.title,
            "images": list(self.mainImages),
            "descImages": list(self.descImages),
            "descText": self.descText,
            "unitWeight": self.unitWeightKg,
            "skuMap": list(self.skuMap),
            "attrs": dict(self.attributes),
            "videoUrl": self.videoUrl,
            "extra": dict(self.extra),
        }


# ---- 登录墙 / 人机验证的人工等待闸门 -----------------------------------------
# 【为什么要等，而不是直接报错】这三家（拼多多/Temu/亚马逊）原先命中拦截就抛异常，
# 阶段① 判失败、要人重跑整个商品。但人往往就坐在这台机器前——登录一下或点个验证
# 只要几秒，而重跑要把已下的几十张图重下一遍。1688 侧早就是「停下来喊人 + 原地等」
# 的取向（见 extract.wait_human_verify），这里对齐它。
#
# 【与 1688 的差别：等的东西不同】1688 是滑块（拖一下就过），这三家可能是：
#   登录墙   要人在该窗口登录账号（Temu login.html、亚马逊 /ap/signin）
#   人机验证 要人点/长按/输验证码（亚马逊 validateCaptcha、Temu bgn_verification）
#   会话丢失 Temu 特有：要人把商品页从站内点开、保持页签开着（见 temu.fetch）
# 故提示文案由各适配器给（它们知道自己命中的是哪种），本函数只管「提醒 + 轮询等」。
#
# 【不重试导航】被拦时刷新页面通常只会再弹一次，且 Temu 重新导航必跳登录页。
# 等的期间页面由人操作，我们只反复读判据。
WAIT_HUMAN_TIMEOUT = 600.0   # 人工处理的最长等待（秒）；超时才把阶段① 判失败
WAIT_HUMAN_INTERVAL = 3.0    # 轮询间隔：登录/验证过关后页面几乎立刻恢复
WAIT_HUMAN_REMIND = 30.0     # 日志重复提醒的间隔（别每 3s 刷一行）


async def wait_human(session, probe_js: str, ready_key: str, message: str,
                     on_manual=None, timeout: float = None, before_probe=None) -> bool:
    """提示人工处理（登录/验证/开页面），然后原地轮询等到数据就绪。

    probe_js    判据 JS，须返回带 blocked 与 ready_key 两个字段的 JSON
    ready_key   判「已就绪」的字段名（各平台不同：pdd 是 hasData、amazon 是 hasTitle）
    message     给人看的处置提示（各适配器按命中的拦截类型自己拼）
    返回 True 表示等到了（调用方要重读一次页面数据）；超时返回 False 由调用方判失败。

    on_manual 是提示通道（service 层传的是发 manual_check 事件的闭包，会在 Web 页面
    弹人工检查条目、CLI 打「! 人工检查」行）。按本项目惯例 best-effort 吞异常：
    提示没发出去不该让一次本来能等到人工的提取失败。
    """
    # 【在函数内读模块常量，不用默认参数】默认参数在 import 时求值，测试里
    # monkeypatch 模块常量就不生效了（改的是模块属性，函数已经绑好了旧值）。
    timeout = WAIT_HUMAN_TIMEOUT if timeout is None else timeout
    logger.warning(message)
    if on_manual is not None:
        try:
            r = on_manual(message)
            if asyncio.iscoroutine(r) or isinstance(r, asyncio.Future):
                await r
        except Exception as e:
            logger.warning(f"人工提示回调异常（忽略）：{e}")
    # 把页签提到前台：要人操作的东西藏在后面人根本看不见（best-effort）
    try:
        await session.page.bring_to_front()
    except Exception as e:
        logger.debug(f"页签提前台失败（忽略）：{e}")

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_remind = loop.time()
    while loop.time() < deadline:
        await asyncio.sleep(WAIT_HUMAN_INTERVAL)
        try:
            if before_probe is not None:
                await before_probe()
            probe = await session.eval_json(probe_js)
        except RuntimeError as e:
            # 人在操作时页面可能正在导航，执行上下文销毁是常态，继续等
            logger.debug(f"人工等待轮询失败，继续等：{e}")
            continue
        if probe.get(ready_key) and not probe.get("blocked"):
            logger.info("页面数据已就绪（人工处理完成），继续提取")
            return True
        now = loop.time()
        if now - last_remind >= WAIT_HUMAN_REMIND:
            left = int(deadline - now)
            logger.warning(f"仍在等待人工处理（剩余 {left}s）：{message[:80]}")
            last_remind = now
    logger.warning(f"等待人工处理超时（{int(timeout)}s）")
    return False


async def wait_for_open_tab(session, must_include: str, message: str,
                            on_manual=None, timeout: float = None) -> bool:
    """提示人工打开目标页签，然后原地轮询等它出现。

    【与 wait_human 的分工】那个等的是【当前页】上的数据就绪（登录/验证过关），
    本函数等的是【另一个页签被打开】。Temu 取数只能读人工开好的页签（理由见
    sources/temu.py 的取数策略），而批量跑时人未必预先开好每一条，故缺页签时
    停下来提醒人开当前这一条、开好即继续——比直接判失败更贴合实际用法。

    返回 True 表示页签已出现（调用方要重新 adopt 取数），超时返回 False。
    """
    timeout = WAIT_HUMAN_TIMEOUT if timeout is None else timeout
    logger.warning(message)
    if on_manual is not None:
        try:
            r = on_manual(message)
            if asyncio.iscoroutine(r) or isinstance(r, asyncio.Future):
                await r
        except Exception as e:
            logger.warning(f"人工提示回调异常（忽略）：{e}")
    # 把 Chrome 窗口提到前台：人要开的页签在那里面（best-effort，理由同 wait_human）
    try:
        await session.page.bring_to_front()
    except Exception as e:
        logger.debug(f"页签提前台失败（忽略）：{e}")

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_remind = loop.time()
    while loop.time() < deadline:
        await asyncio.sleep(WAIT_HUMAN_INTERVAL)
        try:
            if (await session.adopt_open_page(must_include)).get("ok"):
                logger.info("目标页签已打开（人工处理完成），继续提取")
                return True
        except Exception as e:
            # 人在操作浏览器时枚举页签可能瞬时失败，继续等（同 wait_human 的取向）
            logger.debug(f"页签等待轮询失败，继续等：{e}")
        now = loop.time()
        if now - last_remind >= WAIT_HUMAN_REMIND:
            left = int(deadline - now)
            logger.warning(f"仍在等待人工打开目标页签（剩余 {left}s）：{message[:80]}")
            last_remind = now
    logger.warning(f"等待人工打开页签超时（{int(timeout)}s）")
    return False
