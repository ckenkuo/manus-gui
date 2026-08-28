"""店小秘编辑页探查/操作 CLI（发布管线阶段②-⑫的搬运验证用）。

只读命令（不改店小秘任何数据）：
  python publish_inspect.py find "标题关键词"              草稿列表找 rowid
  python publish_inspect.py inspect <rowid>                导出编辑页表单状态
  python publish_inspect.py images <rowid> [--out 目录]      列出/下载三组图片
  python publish_inspect.py dump-attrs <rowid>             属性当前值+下拉选项
  python publish_inspect.py check-attrs <rowid> <info.json> 属性审核清单（dry-run）
  python publish_inspect.py enrich-vision <info.json>      视觉回填阶段①四个看图字段
                                                           （不连浏览器，只读本地图）
  python publish_inspect.py pick-rowid "标题" "店铺" [--site 站点]  认领后按站点筛 rowid
  python publish_inspect.py cache-list                      类目/属性缓存现状（不连浏览器）
  python publish_inspect.py cache-clear [--slug <slug>]     清缓存（不给 slug 则全清）

写入命令（会真实修改草稿，但不会点发布）：
  python publish_inspect.py crawl "<1688链接>"                        链接采集（建采集记录）
  python publish_inspect.py claim "标题" "店铺" [--site 站点]           认领到店铺站点（建草稿）
  python publish_inspect.py collect-claim "<链接>" "标题" "店铺"        阶段②全流程→rowid
  python publish_inspect.py auto-cat <rowid> "商品标题"      逐级选定产品类目
  python publish_inspect.py check-attrs <rowid> <info.json> --apply   应用属性修改
  python publish_inspect.py set-titles <rowid> <info.json>            中英文标题+产地
  python publish_inspect.py fix-sizes <rowid> <info.json>             尺码勾选修正
  python publish_inspect.py add-sizechart <rowid> <info.json>         添加尺码表
  python publish_inspect.py fix-sku-code <rowid>                      SKU货号英化（去中文）
  python publish_inspect.py set-variant <rowid> <info.json>           变种信息（价格/尺寸/重量）
  python publish_inspect.py set-stock <rowid> <info.json>             仓库/库存/SKU分类
  python publish_inspect.py set-shipping <rowid> [--deadline 文本]      发货时效+运费模板
  python publish_inspect.py set-material <rowid> <方图.jpg>             阶段⑥ 素材图替换
  python publish_inspect.py skc-row <rowid> "灰色" <图目录>              阶段⑦ 整行换 SKC 图
  python publish_inspect.py desc-delete <rowid> 1,3,5                  阶段⑪ 删描述模块
  python publish_inspect.py desc-save <rowid>                          阶段⑪ 保存描述编辑器
  python publish_inspect.py save <rowid>                              保存落库（不发布）

图片类阶段的注意事项：
  - set-material / skc-row 传进来的图必须【已做过合规化】（素材图 1785² 方图走
    images.square_image；SKC 图 3:4 且 ≥1340×1785 走 images.fit_34）。这两个命令
    不代做合规化，尺寸不对会被发布校验静默拦下。
  - skc-row 按【文件名排序】挂图，故颜色专属图命名 01.jpg 就落在首位、免拖拽。
  - desc-delete 删完【必须再跑 desc-save】才生效：描述编辑器点「关闭」即丢弃改动
    （2026-08-20 实测）。这一点反过来也是安全阀——验证时不保存就不会动到真实草稿。
  - 阶段⑪ 的 desc-save 只保存【描述编辑器】，整个商品仍要再走一次 save 才落库。

耗时提示：dump-attrs / check-attrs 要逐个点开下拉读选项（虚拟列表需滚动收集），
默认只读必填项（约 18 项）仍需数分钟；加 --skip-options 可跳过（但没有 options
就不能做 LLM 审核）。

前提：调试 Chrome（--remote-debugging-port=9222）里已登录店小秘。
本 CLI 不提供发布入口——「发布」按钮永不自动点击。
"""
import argparse
import asyncio
import json
import sys

from app.logger import logger
from app.publish.browser import BrowserSession, ensure_cdp_alive
from app.publish.pipeline import (
    add_sizechart,
    auto_cat,
    check_attrs,
    dump_attrs,
    find_rowid,
    fix_sizes,
    fix_sku_codes,
    inspect,
    list_images,
    open_edit,
    save,
    set_shipping,
    set_stock,
    set_titles,
    set_variant,
)


async def main() -> int:
    ap = argparse.ArgumentParser(description="店小秘编辑页探查/操作")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("find", help="草稿列表按标题关键词找 rowid")
    p.add_argument("keyword")
    p = sub.add_parser("inspect", help="导出编辑页表单状态")
    p.add_argument("rowid")
    p = sub.add_parser("images", help="列出/下载素材图/颜色图/描述图")
    p.add_argument("rowid")
    p.add_argument("--out", default=None, help="下载目录；不给则只列 URL")
    p = sub.add_parser("auto-cat", help="逐级选定产品类目（写入）")
    p.add_argument("rowid")
    p.add_argument("title", help="商品标题，供 LLM 判断类目")
    p.add_argument("--no-lookahead", action="store_true",
                   help="关闭前瞻（不读候选的子类目，快但判断质量差）")
    p.add_argument("--no-cache", action="store_true",
                   help="不用已知类目路径缓存，强制走完整遍历")
    p.add_argument("--site", default="", help="经营站点（缓存键的一部分）")
    p = sub.add_parser("dump-attrs", help="导出产品属性当前值+下拉选项（只读）")
    p.add_argument("rowid")
    p.add_argument("--skip-options", action="store_true",
                   help="只读当前值不点开下拉（快，但没有 options）")
    p.add_argument("--all-options", action="store_true",
                   help="读全部项的选项（默认只读必填项，省一半时间）")
    p.add_argument("--cat-path", default="",
                   help='类目路径（如 "A > B > C"），给了才能用属性选项缓存')
    p.add_argument("--no-cache", action="store_true", help="不用属性选项缓存")
    p.add_argument("--site", default="", help="经营站点（缓存键的一部分）")
    p = sub.add_parser("check-attrs", help="LLM 比对属性并产出修改清单")
    p.add_argument("rowid")
    p.add_argument("info_json", help="product-info.json 路径")
    p.add_argument("--apply", action="store_true",
                   help="执行修改（默认 dry-run 只出清单不动表单）")
    p.add_argument("--all-options", action="store_true",
                   help="读全部项的选项（默认只读必填项）")
    p.add_argument("--cat-path", default="",
                   help='类目路径（如 "A > B > C"），给了才能用属性选项缓存')
    p.add_argument("--no-cache", action="store_true", help="不用属性选项缓存")
    p.add_argument("--site", default="", help="经营站点（缓存键的一部分）")
    # 缓存管理：只读本地 JSON，两条合并成一个 handler 分支（见下方 CDP 检查之前）
    p = sub.add_parser("cache-list", help="类目/属性缓存现状（只读，不连浏览器）")
    p = sub.add_parser("cache-clear", help="清类目/属性缓存（只删本地文件）")
    p.add_argument("--slug", default="",
                   help="只清这个属性缓存文件（cache-list 里的 slug）；不给则全清")
    p = sub.add_parser("set-titles", help="LLM 生成中英文标题并填写（写入）")
    p.add_argument("rowid")
    p.add_argument("info_json", help="product-info.json 路径")
    p = sub.add_parser("fix-sizes", help="尺码勾选修正（写入）")
    p.add_argument("rowid")
    p.add_argument("info_json", help="product-info.json 路径")
    p = sub.add_parser("fix-sku-code", help="SKU货号重写为纯英文（写入）")
    p.add_argument("rowid")
    p = sub.add_parser("set-variant", help="变种信息批量填写（写入）")
    p.add_argument("rowid")
    p.add_argument("info_json", help="product-info.json 路径")
    p.add_argument("--price", default="",
                   help="申报价（不给按管线默认 188.88）")
    p.add_argument("--dims", help="尺寸覆盖 长x宽x高，如 25x20x5；"
                                  "服装类不给也会按固定 30x25x3")
    p.add_argument("--weight", help="重量覆盖（g）")
    p = sub.add_parser("set-stock", help="库存SKU分类批量填写（写入）")
    p.add_argument("rowid")
    p.add_argument("info_json", help="product-info.json 路径")
    p.add_argument("--stock", default="100", help="库存数量（默认100）")
    p.add_argument("--warehouse", default="飞特COL仓库", help="仓库名称（默认飞特COL仓库）")
    p = sub.add_parser("add-sizechart", help="添加尺码表（写入）")
    p.add_argument("rowid")
    p.add_argument("info_json", help="product-info.json 路径")
    p.add_argument("--category", default=None,
                   help="尺码分类关键词（默认跟随平台按已选类目预选的值，一般不用给）")
    p.add_argument("--name", help="模板名称（默认：商品标题前10字+尺码表）")
    p.add_argument("--which", type=int, default=0, choices=(0, 1),
                   help="填第几张表：0=「尺码表」（默认），1=「尺码表2」"
                        "（套装商品平台要求两张都填，否则发布被打回）")
    p = sub.add_parser("set-shipping", help="发货时效+运费模板（写入）")
    p.add_argument("rowid")
    p.add_argument("--deadline", default="", help="承诺发货时效（如'15个工作日内发货'，不指定则选最长）")
    p = sub.add_parser("save", help="保存落库（写入，不发布）")
    p.add_argument("rowid")
    p = sub.add_parser("set-material", help="阶段6 素材图替换（写入）")
    p.add_argument("rowid")
    p.add_argument("image", help="已合规化的方图路径（1785²，见 images.square_image）")
    p = sub.add_parser("skc-row", help="阶段7 某颜色行整行换图（写入）")
    p.add_argument("rowid")
    p.add_argument("row_keyword", help="颜色行关键词，如「灰色」")
    p.add_argument("img_dir", help="图片目录，按文件名排序挂载（01.jpg 落首位）")
    p = sub.add_parser("desc-map", help="阶段11 列出描述模块（只读）")
    p.add_argument("rowid")
    p.add_argument("--info-json", default="", help="附带 complianceNotes 供对照")
    p = sub.add_parser("desc-delete", help="阶段11 删除描述模块（写入，需再 desc-save）")
    p.add_argument("rowid")
    p.add_argument("positions", help="要删的序号，逗号分隔（从 1 起），如 1,3,5")
    p = sub.add_parser("desc-replace", help="阶段11 换某个描述模块的图（写入，需再 desc-save）")
    p.add_argument("rowid")
    p.add_argument("pos", type=int, help="模块序号（从 1 起，见 desc-map）")
    p.add_argument("image", help="替换用的本地图（中文图英化产物，见 images.edit_image）")
    p = sub.add_parser("desc-save", help="阶段11 保存描述编辑器（写入）")
    p.add_argument("rowid")
    p = sub.add_parser("enrich-vision", help="视觉回填阶段1四字段（只读）")
    p.add_argument("info_json", help="product-info.json 路径")
    p.add_argument("--overwrite", action="store_true",
                   help="重填已有值（默认只填空字段）")
    p.add_argument("--max-images", type=int, default=12,
                   help="一次请求最多传几张唯一图（默认12）")
    # 阶段②（app/publish/claim.py）：会真实建采集记录/草稿，副作用不可逆
    p = sub.add_parser("crawl", help="链接采集单个 1688 链接（写入，建采集记录）")
    p.add_argument("url", help="1688 商品链接")
    p = sub.add_parser("claim", help="认领到店铺站点（写入，建草稿；弹窗开着可续跑）")
    p.add_argument("title", help="商品完整标题（用于在采集列表里搜索）")
    p.add_argument("store", help="目标店铺名（如 Pawly）")
    p.add_argument("--site", required=True,
                   help="目标站点（如 美国；店小秘没有「全球」站点，必须给具体国家站点）")
    p = sub.add_parser("pick-rowid", help="认领后按站点筛出 rowid（只读）")
    p.add_argument("title")
    p.add_argument("store")
    p.add_argument("--site", required=True,
                   help="目标站点（如 美国；店小秘没有「全球」站点，必须给具体国家站点）")
    p.add_argument("--keyword", help="草稿列表搜索关键词（默认取标题前12字）")
    p = sub.add_parser("collect-claim", help="阶段2全流程：采集+认领+取 rowid（写入）")
    p.add_argument("url", help="1688 商品链接")
    p.add_argument("title", help="商品完整标题")
    p.add_argument("store", help="目标店铺名")
    p.add_argument("--site", required=True,
                   help="目标站点（如 美国；店小秘没有「全球」站点，必须给具体国家站点）")
    p.add_argument("--skip-crawl", action="store_true",
                   help="跳过采集只做认领（商品已在采集列表里时用，少造一条采集记录）")
    args = ap.parse_args()

    # 视觉回填只读本地 product-info.json + 目录里的图，不碰浏览器：放在 CDP 检查之前
    # 直接返回，免得没开调试 Chrome 就跑不了这条（也避免白占一个 CDP 会话）。
    if args.cmd == "enrich-vision":
        from app.publish.extract import enrich_vision
        try:
            r = await enrich_vision(args.info_json, max_images=args.max_images,
                                    overwrite=args.overwrite)
        except Exception as e:
            logger.error(f"视觉回填失败：{e}")
            return 1
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    # 缓存管理同理只碰本地 JSON：放在 CDP 检查之前，没开调试 Chrome 也能看/能清。
    if args.cmd in ("cache-list", "cache-clear"):
        from app.publish.cache import cache_stats, clear
        if args.cmd == "cache-list":
            st = cache_stats()
            print(json.dumps(st, ensure_ascii=False, indent=2))
            logger.info(f"已知类目路径 {st['paths']} 条，"
                        f"已缓存属性类目 {st['attrCategories']} 个（{st['attrRows']} 行）")
            return 0
        removed = clear(args.slug)
        print(json.dumps(removed, ensure_ascii=False, indent=2))
        logger.info(f"已清缓存：属性类目 {len(removed['attrFiles'])} 个"
                    + ("，含类目路径清单" if removed["categories"] else ""))
        return 0

    if not await ensure_cdp_alive():
        return 2
    session = BrowserSession()
    try:
        await session.open()
        if args.cmd == "find":
            r = await find_rowid(session, args.keyword)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            logger.info(f"列表共 {r.get('total')} 行，匹配 {len(r.get('matched') or [])} 行")
        elif args.cmd == "inspect":
            r = await inspect(session, args.rowid)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            logger.info(
                f"{r.get('title')} | 表单 {len(r.get('form') or [])} 项 | "
                f"变种行 {len(r.get('skus') or [])} | 区块 {sum(1 for v in (r.get('sections') or {}).values() if v)}/7"
            )
        elif args.cmd == "auto-cat":
            r = await auto_cat(session, args.rowid, args.title,
                               lookahead=not args.no_lookahead,
                               use_cache=not args.no_cache, site=args.site)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            logger.info(f"类目已选定（{r['levels']} 级，走{'缓存' if r.get('source') == 'cache' else '遍历'}）"
                        f"：{r['path']}")
        elif args.cmd == "dump-attrs":
            await open_edit(session, args.rowid)
            await asyncio.sleep(2)
            # --cat-path 不给就当未命中走全量读（不去反推当前类目：那要么从 300 字
            # 文本里抠路径太脆，要么得重开类目弹窗——在带着未保存修改的表单上开弹窗
            # 不值得为一个探查命令引入）
            cat_path = [x.strip() for x in args.cat_path.split(">") if x.strip()]
            r = await dump_attrs(session, skip_options=args.skip_options,
                                 required_only=not args.all_options,
                                 cat_path=cat_path or None,
                                 use_cache=not args.no_cache, site=args.site)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            req = sum(1 for a in r["attrs"] if a.get("required"))
            unfilled = sum(1 for a in r["attrs"]
                           if a.get("required") and (a.get("current") or "").startswith("("))
            logger.info(
                f"属性 {r['count']} 项（必填 {req}，必填未填 {unfilled}）"
                + (f"，现场读 {r.get('activeRead')} 项"
                   + (f"、缓存 {r['cacheRead']} 项" if r.get("cacheRead") else "")
                   if r.get("optionsRead") else "")
            )
        elif args.cmd == "check-attrs":
            await open_edit(session, args.rowid)
            await asyncio.sleep(2)
            cat_path = [x.strip() for x in args.cat_path.split(">") if x.strip()]
            r = await check_attrs(session, args.info_json, apply=args.apply,
                                  required_only=not args.all_options,
                                  cat_path=cat_path or None,
                                  use_cache=not args.no_cache, site=args.site)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            logger.info(
                f"属性 {r['attrCount']} 项 | 建议改 {len(r['proposed'])} 项 | "
                f"拒绝 {len(r['rejected'])} 项"
                + (f" | 已应用 {sum(1 for a in r['applied'] if a['result'] == 'ok')}/"
                   f"{len(r['applied'])}" if r.get("applied") else "（dry-run，未写入）")
                + (f" | 缓存过期重读 {len(r['cacheRefreshed'])} 行"
                   if r.get("cacheRefreshed") else "")
            )
        elif args.cmd == "set-titles":
            await open_edit(session, args.rowid)
            await asyncio.sleep(2)
            r = await set_titles(session, args.info_json)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            if r.get("status") == "ok":
                logger.info(
                    f"标题已填写 | 中文: {r['generated']['title'][:40]} | "
                    f"英文: {r['generated']['enTitle'][:50]}"
                )
            else:
                logger.error(f"标题生成失败: {r.get('reason')} {r.get('err')}")
        elif args.cmd == "fix-sku-code":
            await open_edit(session, args.rowid)
            await asyncio.sleep(2)
            r = await fix_sku_codes(session)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            if r.get("status") == "error":
                logger.error(f"SKU货号重写失败: {r.get('reason')}")
            else:
                tr = r.get("translated") or {}
                logger.info(
                    f"SKU货号已重写 {r.get('filled')}/{r.get('rowCount')} 行"
                    + (" | 译 " + "、".join(f"{k}→{v}" for k, v in tr.items()) if tr else "")
                    + (f" | 未过校验 {r.get('bad')}" if r.get("bad") else "")
                )
        elif args.cmd == "fix-sizes":
            await open_edit(session, args.rowid)
            await asyncio.sleep(2)
            r = await fix_sizes(session, args.info_json)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            if r.get("status") == "ok":
                logger.info(
                    f"尺码勾选完成 | 源尺码 {len(r['wantedSizes'])} 个 | "
                    f"切换 {len(r['toggled'])} 次 | SKU 表 {r['rowCount']} 行"
                )
            else:
                logger.error(f"尺码勾选失败: {r.get('reason')}")
        elif args.cmd == "set-variant":
            await open_edit(session, args.rowid)
            await asyncio.sleep(2)
            # 不传 cat_path：单命令调试没有类目上下文，服装判定退到标题匹配
            # （批量走 service 时会把阶段③拿到的类目路径传下去，判得更准）
            r = await set_variant(session, args.info_json, args.price, args.dims, args.weight)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            if r.get("status") == "ok":
                logger.info(
                    f"变种信息已填写 | 申报价={r['price']} 尺寸={r['dims']} "
                    f"重量={r['weight']}g MSRP={r['msrp']} | {r['rowCount']} 行"
                )
            else:
                logger.error(f"变种信息填写失败: {r.get('reason')}")
        elif args.cmd == "set-stock":
            await open_edit(session, args.rowid)
            await asyncio.sleep(2)
            r = await set_stock(session, args.info_json, args.stock, args.warehouse)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            if r.get("status") == "ok":
                cat_map = {"1": "单品", "2": "同款多件", "3": "混合套装"}
                unit_map = {"1": "件", "2": "双", "3": "包"}
                sc = r.get("skuCategory", {})
                logger.info(
                    f"库存SKU分类已填写 | 仓库={r['warehouse']} 库存={r['stock']} | "
                    f"分类={cat_map.get(sc.get('cat'), sc.get('cat'))} 数量={sc.get('qty')} "
                    f"单位={unit_map.get(sc.get('unit'), sc.get('unit'))} | 处理 {r['processed']} 行"
                )
            else:
                logger.error(f"库存SKU分类填写失败: {r.get('reason')} {r.get('err')}")
        elif args.cmd == "add-sizechart":
            await open_edit(session, args.rowid)
            await asyncio.sleep(2)
            r = await add_sizechart(session, args.info_json, args.category, args.name,
                                    which=args.which)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            if r.get("status") == "ok":
                if r.get("skipped"):
                    logger.info(f"尺码表已存在，跳过: {r.get('current')}")
                else:
                    logger.info(
                        f"尺码表已添加 | 模板={r['tplName']} 分类={r['category']} "
                        f"参数={len(r['params'])}项 来源={r['measureSource']}"
                    )
            else:
                logger.error(f"尺码表添加失败: {r.get('reason')}")
        elif args.cmd == "set-shipping":
            await open_edit(session, args.rowid)
            await asyncio.sleep(2)
            r = await set_shipping(session, args.deadline)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            if r.get("status") == "ok":
                logger.info(
                    f"运输信息已填写 | 发货时效={r['deadline']}"
                    + ("（自动选最长）" if r.get("autoPicked") else "")
                    + f" | 运费模板={r.get('freightTemplate')}"
                    + ("（本次新选）" if r.get("templatePicked") else "（原已选中）")
                )
            else:
                logger.error(
                    f"运输信息填写失败[{r.get('stage')}]: {r.get('reason')} {r.get('err') or ''}"
                    + (f" | 可选时效={r.get('options')}" if r.get("options") else "")
                )
        elif args.cmd == "save":
            # 【单独跑 save 时才 open_edit】编排里 save 应紧跟前序阶段在同一页面执行，
            # open_edit 会刷新页面把未保存的修改丢掉——这里是独立子命令，页面本来就是干净的
            await open_edit(session, args.rowid)
            await asyncio.sleep(2)
            r = await save(session, args.rowid)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            if r.get("status") == "ok":
                ut = r.get("updateTime") or {}
                logger.info(
                    "已保存（未发布）"
                    + (f" | 更新时间 {ut.get('before')} → {ut.get('after')}"
                       if ut.get("after") else " | 更新时间未读到（不影响落库）")
                    + f" | 确认框={(r.get('confirmDialog') or {}).get('closed') or '未出现'}"
                )
            else:
                red = "、".join(s["name"] for s in (r.get("redSections") or []))
                logger.error(
                    f"保存未通过校验（页面无 toast）: {r.get('reason') or ''}"
                    + (f" | 红色区块={red}" if red else "")
                    + (f" | 错误={r.get('errors')}" if r.get("errors") else "")
                )
        elif args.cmd in ("set-material", "skc-row", "desc-map", "desc-delete",
                          "desc-replace", "desc-save"):
            # 阶段⑥⑦⑪ 共用一个分支：都要先进编辑页，再按子命令分流。
            # 就地 import 同 enrich-vision 的取向，不动顶部的 import 块。
            from app.publish.pipeline import (
                desc_delete,
                desc_map,
                desc_replace,
                desc_save,
                set_material,
                skc_replace_row,
            )
            await open_edit(session, args.rowid)
            await asyncio.sleep(2)
            await session.fix_hidden_tab()

            if args.cmd == "set-material":
                r = await set_material(session, args.image)
            elif args.cmd == "skc-row":
                r = await skc_replace_row(session, args.row_keyword, args.img_dir)
            elif args.cmd == "desc-map":
                r = await desc_map(session, args.info_json)
            elif args.cmd == "desc-delete":
                pos = [int(x) for x in args.positions.split(",") if x.strip()]
                r = await desc_delete(session, pos)
            elif args.cmd == "desc-replace":
                r = await desc_replace(session, args.pos, args.image)
            else:
                r = await desc_save(session)
            print(json.dumps(r, ensure_ascii=False, indent=2))

            if args.cmd in ("desc-delete", "desc-replace") and r.get("status") == "ok":
                # 描述编辑器的改动【点保存才生效】，关闭即丢弃（2026-08-20 实测）。
                # 这条提示很重要：删完不 desc-save 就白删了。
                logger.info("改动已应用，但【尚未生效】——需再跑 desc-save 保存描述编辑器")
            elif r.get("status") == "validation-error":
                logger.warning(f"{r.get('note') or '校验未通过'}｜{r.get('foreignHosts')}")
            elif r.get("status") != "ok":
                logger.error(f"{args.cmd} 失败：stage={r.get('stage')} {r.get('err') or ''}")
        elif args.cmd in ("crawl", "claim", "pick-rowid", "collect-claim"):
            # 阶段② 的四条命令共用一个分支：函数在这里就地 import（同 enrich-vision 的
            # 取向），避免顶部的 pipeline import 块被多个并行改动同时碰。
            from app.publish.claim import (
                claim_to_store,
                collect_and_claim,
                crawl_link,
                pick_rowid,
            )
            if args.cmd == "crawl":
                r = await crawl_link(session, args.url)
                print(json.dumps(r, ensure_ascii=False, indent=2))
                logger.info(f"采集 {r.get('status')}：{(r.get('text') or '')[:100]}")
            elif args.cmd == "claim":
                r = await claim_to_store(session, args.title, args.store, args.site)
                print(json.dumps(r, ensure_ascii=False, indent=2))
                res = r.get("result") or {}
                logger.info(
                    f"认领完成 | 店铺={r['store']} 站点={r['site']}"
                    + ("（续跑）" if r.get("resumed") else "")
                    + f" | 成功={res.get('success')} 失败={res.get('failed')} 跳过={res.get('skipped')}"
                    + (f" | 已取消默认站点={r['unchecked']}" if r.get("unchecked") else "")
                )
            elif args.cmd == "pick-rowid":
                r = await pick_rowid(session, args.title, args.store, args.site,
                                     keyword=args.keyword)
                print(json.dumps(r, ensure_ascii=False, indent=2))
                logger.info(
                    f"rowid={r['rowid']}（按 {r['matchedBy']} 筛出，"
                    f"候选 {len(r.get('candidates') or [])} 行 / 列表共 {r.get('total')} 行）"
                )
            else:
                r = await collect_and_claim(session, args.url, args.title, args.store,
                                            args.site, skip_crawl=args.skip_crawl)
                print(json.dumps(r, ensure_ascii=False, indent=2))
                logger.info(
                    f"阶段②完成 | 采集={r['crawl'].get('status')} "
                    f"认领成功={(r['claim'].get('result') or {}).get('success')} "
                    f"| rowid={r['rowid']}（接着跑 auto-cat）"
                )
        else:
            r = await list_images(session, args.rowid, args.out)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            g = r.get("groups") or {}
            logger.info(f"素材图 {g.get('material')} 颜色图 {g.get('colors')} 描述图 {g.get('desc')}")
    except Exception as e:
        logger.error(f"失败：{e}")
        return 1
    finally:
        await session.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
