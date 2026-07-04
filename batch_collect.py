"""批量采集驱动：两段式处理 Temu「已发布到站点」的大量商品到 Excel。

为什么要它：把 100+ 商品塞进一次 agent 对话必崩——每步都重发全部历史，上下文
二次方膨胀（实测枚举一次就烧到 468 万 token），且一崩全崩、无法续跑。本驱动用
**确定性编排 + 每商品独立上下文**解决：

  第一段（枚举，确定性 0-LLM）：直接命中 Temu 列表后端接口
      POST /api/kiana/mms/robin/searchForSemiSupplier
      （筛选 secondarySelectStatusList=[12] 即「已发布到站点」，翻页取全），
      从 JSON 里干净地拿 SPU/站点/类目/售价/主图，写工作清单。比让 agent 滚动
      虚拟列表既准（185 全拿到，实测 agent 滚动只拿到 54）又几乎不花钱。

  第二段（逐个隔离跑，用 agent）：对清单里每个【未入库】SPU，清空 agent 记忆得到
      全新上下文，跑一个聚焦子任务（1688 以图搜图→挑同款最便宜→详情读常规价+推
      重量→写 Excel→close_tabs 清标签），max_steps 收紧。跑完即重置，进行下一个。

特性：幂等（按 Excel 已入库 SPU 跳过）→ 可断点续跑、崩了只丢当前一个；分批（--limit）。

用法：
    python batch_collect.py --enumerate-only        # 只枚举列表，写 worklist.json
    python batch_collect.py --limit 1               # 冒烟：只采 1 个
    python batch_collect.py --limit 20              # 采 20 个（复用已有清单）
    python batch_collect.py --refresh --limit 20    # 先重新枚举再采
"""
import argparse
import asyncio
import json

from playwright.async_api import async_playwright

from app.agent.manus import Manus
from app.config import config
from app.logger import logger
from app.prompt.manus import NEXT_STEP_PROMPT
from app.schema import AgentState
from app.tool.wps_excel_tool import WpsExcelTool

EXCEL = r"C:\Users\Administrator\Desktop\商品成本核算_原始备份.xlsx"
SHEET = "pawly全球"
TEMU_URL = "https://agentseller.temu.com/newon/product-select"
LIST_API = "searchForSemiSupplier"
PUBLISHED_STATUS = 12  # secondarySelectStatusList=[12] → 已发布到站点
WORKLIST = config.workspace_root / "worklist.json"

# 稳定性护栏参数（阶段一）
CDP_URL = getattr(config.browser_config, "cdp_url", None) or "http://localhost:9222"
PRODUCT_TIMEOUT = 300  # 单商品超时（秒）：超时判失败、跳下一个
PRODUCT_RETRIES = 2  # 单商品尝试次数（首次 + 重试 1 次）
CDP_PING_RETRIES = 3  # CDP 连续 ping 失败次数上限，超过才放弃整批
CDP_PING_WAIT = 5.0  # 每次 CDP ping 失败后的等待秒数

# 页内翻页取全部商品并抽字段（命中列表接口，带 mallid + cookie）。
_FETCH_ALL_JS = r"""
async (mallid) => {
  const out = [];
  const hdr = {'content-type': 'application/json'};
  if (mallid) hdr['mallid'] = mallid;
  for (let pageNum = 1; pageNum <= 40; pageNum++) {
    const resp = await fetch('/api/kiana/mms/robin/searchForSemiSupplier', {
      method: 'POST', headers: hdr, credentials: 'include',
      body: JSON.stringify({pageSize: 50, pageNum,
        secondarySelectStatusList: [__STATUS__], supplierTodoTypeList: []})
    });
    const j = await resp.json();
    const dl = (j.result && j.result.dataList) || [];
    for (const it of dl) {
      out.push({
        spu: String(it.productId),
        name: (it.productName || '').slice(0, 120),
        site: it.siteName || (it.siteInfoList && it.siteInfoList[0] && it.siteInfoList[0].siteName) || '',
        category: Array.isArray(it.fullCategoryName) ? it.fullCategoryName.join(' / ') : (it.leafCategoryName || ''),
        price: it.supplierPrice || '',
        image: (it.carouselImageUrlList && it.carouselImageUrlList[0]) || ''
      });
    }
    if (dl.length < 50) break;  // 最后一页
  }
  return out;
}
""".replace("__STATUS__", str(PUBLISHED_STATUS))


async def enumerate_worklist() -> int:
    """确定性枚举：命中列表接口取全部「已发布到站点」商品，写 worklist.json，返回条数。"""
    cdp = getattr(config.browser_config, "cdp_url", None) or "http://localhost:9222"
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp)
        ctx = browser.contexts[0]

        # 优先复用已打开的列表标签（已登录已加载，免去 SPA 全量加载超时）
        page = next((p for p in ctx.pages if "product-select" in (p.url or "")), None)
        created = page is None
        if created:
            page = await ctx.new_page()
            try:  # commit 只等导航提交、不等整页加载，够跑 fetch 了
                await page.goto(TEMU_URL, wait_until="commit", timeout=60000)
            except Exception as e:
                logger.warning(f"打开列表页超时（忽略，继续）：{e}")

        client = await ctx.new_cdp_session(page)
        await client.send("Network.enable")
        mallid = {"v": None}

        def on_req(params):
            req = params.get("request", {})
            if LIST_API in req.get("url", "") and not mallid["v"]:
                mallid["v"] = req.get("headers", {}).get("mallid")

        client.on("Network.requestWillBeSent", on_req)

        # 触发一次真实列表请求以抓 mallid：点「已发布到站点」tab（对已加载/新开都有效）
        await asyncio.sleep(3)
        for _ in range(20):  # 最多等 ~20s
            if mallid["v"]:
                break
            try:
                await page.evaluate(
                    "() => { const t=[...document.querySelectorAll('*')].find("
                    "e=>/已发布到站点/.test(e.textContent||'')&&e.children.length<=2"
                    "&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().width<200);"
                    "if(t)t.click(); }"
                )
            except Exception:
                pass
            await asyncio.sleep(1)

        if not mallid["v"]:
            logger.warning("未抓到 mallid，尝试直接重放（可能失败）")
        items = await page.evaluate(_FETCH_ALL_JS, mallid["v"])
        if created:
            await page.close()  # 只关自己开的
        await browser.close()

    # 去重（按 spu），写文件
    seen, uniq = set(), []
    for it in items:
        s = it.get("spu")
        if s and s not in seen:
            seen.add(s)
            uniq.append(it)
    WORKLIST.parent.mkdir(parents=True, exist_ok=True)
    WORKLIST.write_text(json.dumps(uniq, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"枚举完成：{len(uniq)} 个商品（mallid={'有' if mallid['v'] else '无'}）→ {WORKLIST}")
    return len(uniq)


def reset_agent(agent: Manus) -> None:
    """把 agent 恢复到「全新对话」状态：清空记忆/步数/状态，避免上下文跨商品累积。"""
    agent.memory.messages = []
    agent.current_step = 0
    agent.state = AgentState.IDLE
    agent.next_step_prompt = NEXT_STEP_PROMPT  # handle_stuck_state 可能改过它
    agent._last_terminate_status = None
    if agent.browser_context_helper is not None:
        agent.browser_context_helper._current_base64_image = None
    # LLM 是按 config_name 的进程级单例，token 计数会跨商品累加；而
    # check_token_limit 用的是【累计】total_input_tokens（app/llm.py:256-261）。
    # 不清零的话，max_input_tokens 会变成"整批"上限——跑一两个后累计就爆、
    # 之后每个商品第一次调用即死。这里清零，让 max_input_tokens 成为【单商品】护栏。
    if getattr(agent, "llm", None) is not None:
        agent.llm.total_input_tokens = 0
        agent.llm.total_completion_tokens = 0


def reset_pipeline_llms() -> None:
    """清零管道用的 LLM 单例 token 计数（判断点A samematch + 判断点B default）。

    与 reset_agent 同理：LLM 是按 config_name 的进程级单例，`check_token_limit` 用
    累计 total_input_tokens。管道成功路径不走 reset_agent，若不在此清零，samematch
    每商品约累加 5k token，约 12 个商品后累计撞 60000 上限 → 之后每次同款判断都抛
    TokenLimitExceeded、整批后半段静默退化。故每商品开跑前清零，使 max_input_tokens
    成为【单商品】护栏而非整批上限。
    """
    from app.llm import LLM

    for name in ("samematch", "default"):
        inst = LLM._instances.get(name)
        if inst is not None:
            inst.total_input_tokens = 0
            inst.total_completion_tokens = 0


def per_product_prompt(item: dict) -> str:
    img_dir = config.output_dir("image")
    spu = item.get("spu", "")
    return (
        "这是一个已从 Temu 采集好基础信息的商品，请完成它在 1688 的采购价采集并写入 Excel："
        f"\n- SPU: {spu}\n- 商品名: {item.get('name')}\n- 站点: {item.get('site')}"
        f"\n- 类目: {item.get('category')}\n- 销售价: {item.get('price')}"
        f"\n- 主图URL: {item.get('image')}\n\n"
        "步骤：\n"
        f"1. 把主图下载到 {img_dir}\\{spu}.jpeg：直接用 python_execute + requests.get 下载"
        "（服务端请求，不受浏览器同源策略限制；Temu 的 img.kwcdn.com 主图实测可直连 200）。"
        "不要用 execute_js 会话内 fetch——从 Temu 页跨域抓 CDN 必然 CORS 报错（Failed to fetch），白费一步。"
        "仅当直连被 CDN 以 403 拦截时，才退回 execute_js 会话内 fetch 转 dataURL。\n"
        "2. 到 1688 首页用 browser_use 的 paste_image 以图搜图；落地结果页后先确认「框选主体」选对了"
        "（Temu 主图多为营销拼图，默认主体常选错→召回不相干品类），品类不对就点顶部【目标商品主体的裁剪缩略图】"
        "切换主体、或用「框选主体」重框；主体选对后在结果里滤掉不相干品类、只看真正同款，挑常规批发价最便宜的一家进详情页。"
        "关键词兜底只在图搜实在无果时用，且要在页面搜索框 input_text 输入再回车（勿手拼 offer_search.htm?keywords= 的 URL，会 GBK 乱码）。\n"
        "3. 选与本商品一致的规格，读【常规批发价+运费】作采购价（务必剔除新人价/首单价/优惠券等一次性优惠）；"
        "重量不照抄平台，据尺寸/材质/填充推测。红线：绝不下单/支付。\n"
        f"4. 用 wps_excel_tool 先 inspect 再 append_product_row，把该商品追加到 {EXCEL} 的「{SHEET}」表："
        "站点(A)/类目(B)/SPU(D)/销售价(I 纯数字, 剥掉¥)/日常价(G 同)/采购价(J)/重量(K 单位公斤, 如 0.05)/"
        "ros(O=7) 走 column_values，公式列 H/L/N/P/Q/R 照 inspect 的同列公式（把行号写成 {r} 占位符随行自适应）；"
        "主图 image_path 传步骤1的路径、image_column=E（产品图片列；F 是货号列，勿写图）。\n"
        "5. 用 browser_use 的 close_tabs(text=\"1688\") 关掉本商品开的 1688 标签。\n"
        "6. 完成后用 terminate 结束。只处理这一个商品，不要去碰列表里的其它商品。"
    )


def excel_write_locked(path: str) -> bool:
    """Excel 是否被占用（WPS/Excel 打开中）导致写不进。

    两个信号：① Office 打开时会在同目录生成 ~$ 锁文件；② 尝试以读写方式打开，
    被占用时 Windows 抛 PermissionError。任一命中即视为锁定。
    """
    from pathlib import Path

    p = Path(path)
    if (p.parent / ("~$" + p.name)).exists():
        return True
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def load_worklist() -> list:
    if not WORKLIST.exists():
        return []
    try:
        data = json.loads(WORKLIST.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"读取工作清单失败：{e}")
        return []


async def ensure_cdp_alive(cdp_url: str = CDP_URL) -> bool:
    """CDP 健康检查 + 确定性重连：能连上返回 True，连续失败达上限返回 False。

    每个商品开跑前 ping 一次已登录的 Chrome（复用第一段 connect_over_cdp 的连接
    方式，batch_collect.py:78-80）。断开时按固定逻辑等待重试，而不是把错误丢回大
    模型让它 taskkill 瞎试——后者是基线里后半程卡死的元凶。连续 N 次都连不上才
    放弃整批（返回 False），交由调用方决定是否重启 Chrome。
    """
    for attempt in range(1, CDP_PING_RETRIES + 1):
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(cdp_url)
                # 能拿到 context 即视为存活；不做任何页面操作，避免副作用。
                _ = browser.contexts
                await browser.close()
            return True
        except Exception as e:
            logger.warning(
                f"CDP ping 失败（{attempt}/{CDP_PING_RETRIES}）：{e}；"
                f"{CDP_PING_WAIT}s 后重试"
            )
            if attempt < CDP_PING_RETRIES:
                await asyncio.sleep(CDP_PING_WAIT)
    logger.error(
        f"❌ CDP 连续 {CDP_PING_RETRIES} 次连不上（{cdp_url}）。"
        f"请确认已登录的 Chrome 仍以 --remote-debugging-port=9222 运行。"
    )
    return False


async def collect_one(agent: Manus, item: dict) -> bool:
    """采集单个商品，带超时 + 重试护栏。返回是否已入库。

    - 超时（PRODUCT_TIMEOUT）判失败，reset_agent 已能清状态后重试。
    - 每次尝试前 reset_agent（清记忆/步数/状态/token 计数）。
    - 幂等：已入库 SPU 由上层跳过，重跑安全（batch_collect.py:226-227）。
    """
    spu = str(item.get("spu", ""))
    for attempt in range(1, PRODUCT_RETRIES + 1):
        reset_agent(agent)
        try:
            await asyncio.wait_for(
                agent.run(per_product_prompt(item)), timeout=PRODUCT_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"⏱️ SPU={spu} 第 {attempt}/{PRODUCT_RETRIES} 次超时"
                f"（>{PRODUCT_TIMEOUT}s），放弃本次"
            )
        except Exception as e:
            logger.error(f"SPU={spu} 第 {attempt}/{PRODUCT_RETRIES} 次异常：{e}")
        finally:
            try:  # 兜底清理本商品可能残留的 1688 标签
                await agent.available_tools.get_tool("browser_use").execute(
                    action="close_tabs", text="1688"
                )
            except Exception:
                pass

        if spu in WpsExcelTool.existing_key_values(EXCEL, SHEET, "D"):
            return True
        if attempt < PRODUCT_RETRIES:
            logger.info(f"↻ SPU={spu} 未入库，重试（{attempt + 1}/{PRODUCT_RETRIES}）")
    return False


async def collect_one_pipeline(agent: Manus, item: dict) -> bool:
    """确定性管道采集单商品（阶段二），失败退回 agent 兜底（阶段二 2.4）。

    单商品大模型往返 = 2 次（视觉挑同款 + 文本读价），无 247 元素历史累积。
    管道任一确定性步骤失败 → 记原因并退回旧 agent 路径跑该商品，不背水一战。
    """
    import os

    from app.collect.pipeline import (
        archive_unmatched_image,
        collect_one_product,
        write_product_row,
    )

    spu = str(item.get("spu", ""))
    browser_tool = agent.available_tools.get_tool("browser_use")
    excel_tool = WpsExcelTool()
    img_path = os.path.join(str(config.output_dir("image")), f"{spu}.jpeg")

    reset_pipeline_llms()  # 单商品护栏：清零 samematch/default 单例 token 计数（防批量后半段撞上限退化）

    try:
        res = await asyncio.wait_for(
            collect_one_product(browser_tool, item), timeout=PRODUCT_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(f"⏱️ SPU={spu} 管道超时（>{PRODUCT_TIMEOUT}s），退回 agent 兜底")
        res = None
    except Exception as e:
        logger.error(f"SPU={spu} 管道异常：{e}，退回 agent 兜底")
        res = None

    if res is not None and res.ok:
        wrote, msg = await write_product_row(
            excel_tool, EXCEL, SHEET, item, res, img_path
        )
        try:
            await browser_tool.execute(action="close_tabs", text="1688")
        except Exception:
            pass
        if wrote and spu in WpsExcelTool.existing_key_values(EXCEL, SHEET, "D"):
            logger.info(
                f"管道成功：SPU={spu} offer={res.offer_id} 采购价={res.purchase_price} "
                f"运费={res.shipping} 重量={res.weight_g}g {('存疑:' + res.note) if res.note else ''}"
            )
            return True
        logger.warning(f"SPU={spu} 管道判断成功但写入未确认（{msg}），退回 agent 兜底")
    else:
        reason = res.fail_reason if res is not None else "超时/异常"
        # 「无同款」跳过 agent 兜底：agent 落到同一 air.1688 图搜 SPA 只会撞 token
        # 上限/超时（实测每个漏采品白烧 5–10 分钟兜底），且即便成功也是宽松匹配、
        # 与严格模式取舍相悖。直接记漏采，把批次时间省下来。
        if res is not None and res.no_same_match:
            # 所有主体框都试完仍无同款：快速失败。按你的要求写入一行、采购价(J)/
            # 重量(K)留空待人工补，其余已知字段(站点/类目/SPU/售价/日常价/主图)照填，
            # 这样该 SPU 记为已处理不再重试、也给人工留下一行可补价。不退回 agent。
            wrote, msg = await write_product_row(
                excel_tool, EXCEL, SHEET, item, res, img_path
            )
            # 归档漏采品主图（优先白底提取图）到「未找到同款主图」目录、命名带 SPU，
            # 供人工后续手动找货源补价。
            archived = archive_unmatched_image(spu)
            try:
                await browser_tool.execute(action="close_tabs", text="1688")
            except Exception:
                pass
            if wrote:
                extra = f"，主图已归档 → {archived}" if archived else ""
                logger.info(
                    f"⬜ SPU={spu} 无同款 → 已入库留空行（采购价/重量待人工补）{extra}"
                )
                return True
            logger.warning(f"SPU={spu} 无同款且留空行写入失败（{msg}），跳过")
            return False
        logger.warning(f"SPU={spu} 管道未成（{reason}），退回 agent 兜底")

    # 2.4 兜底：确定性管道失败（非无同款）→ 走旧 agent 自由循环路径
    try:
        await browser_tool.execute(action="close_tabs", text="1688")
    except Exception:
        pass
    return await collect_one(agent, item)


async def run_phase2(agent: Manus, limit: int, use_pipeline: bool = False) -> None:
    worklist = load_worklist()
    if not worklist:
        logger.error(f"工作清单为空（{WORKLIST}）。先用 --refresh 或 --enumerate-only 枚举。")
        return

    # 开跑前预检：Excel 被 WPS/Excel 打开会写不进（Permission denied），
    # 提前失败并给清楚提示，避免白白消耗 LLM 步数。
    if excel_write_locked(EXCEL):
        logger.error(
            f"❌ Excel 正被占用（疑似 WPS/Excel 打开中），无法写入：{EXCEL}\n"
            "   请先在 WPS/Excel 里【关闭该文件】，再重新运行 batch_collect.py。"
        )
        return

    done = WpsExcelTool.existing_key_values(EXCEL, SHEET, "D")
    todo = [it for it in worklist if str(it.get("spu", "")).strip() and str(it["spu"]) not in done]
    batch = min(limit, len(todo))
    logger.info(
        f"=== 第二段：清单 {len(worklist)} 个，已入库 {len(done)}，待采 {len(todo)}，本批 {batch} 个 ==="
    )

    agent.max_steps = 20  # 单商品收紧步数，控成本（相对默认 40 仍收紧）
    ok = fail = 0
    for i, item in enumerate(todo[:limit], 1):
        spu = item.get("spu")
        logger.info(f"--- [{i}/{batch}] 采集 SPU={spu}（{item.get('name', '')[:20]}）---")

        # 开跑前 CDP 健康检查：断了就确定性重连；连续失败达上限则放弃整批，
        # 避免逐个商品都白跑一遍超时。
        if not await ensure_cdp_alive():
            logger.error("CDP 不可用，中止本批（已入库的不受影响，可稍后续跑）。")
            break

        collector = collect_one_pipeline if use_pipeline else collect_one
        if await collector(agent, item):
            ok += 1
            logger.info(f"✅ SPU={spu} 已写入")
        else:
            fail += 1
            logger.warning(f"⚠️ SPU={spu} 未入库，已重试仍失败（继续下一个）")

    logger.info(f"=== 本批完成：成功 {ok}，失败/存疑 {fail} ===")


async def main():
    parser = argparse.ArgumentParser(description="批量采集 Temu→1688→Excel")
    parser.add_argument("--limit", type=int, default=20, help="本批处理的未入库商品数量")
    parser.add_argument("--refresh", action="store_true", help="重新枚举工作清单")
    parser.add_argument("--enumerate-only", action="store_true", help="只枚举，不采购")
    parser.add_argument(
        "--pipeline",
        action="store_true",
        help="阶段二：走确定性管道（2 次单发大模型判断），失败自动退回 agent 兜底",
    )
    args = parser.parse_args()

    if args.refresh or args.enumerate_only or not WORKLIST.exists():
        await enumerate_worklist()
    if args.enumerate_only:
        return

    # 采集前预检：Excel 被占用就别启动 agent（省去浏览器/MCP 初始化与 LLM 开销）
    if excel_write_locked(EXCEL):
        logger.error(
            f"❌ Excel 正被占用（疑似 WPS/Excel 打开中），无法写入：{EXCEL}\n"
            "   请先在 WPS/Excel 里【关闭该文件】，再重新运行。"
        )
        return

    agent = await Manus.create()
    try:
        await run_phase2(agent, args.limit, use_pipeline=args.pipeline)
    finally:
        await agent.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
