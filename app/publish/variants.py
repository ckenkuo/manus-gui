"""店小秘发布操作：variants。模块导航见 docs/publish-pipeline-refactor.md。"""

import json
import re
from app.logger import logger
from app.publish import packaging, variant_dom
from app.publish.browser import BrowserSession, J
from typing import Optional


# ---- 阶段⑩ 变种信息（set_variant）------------------------------------------

_JS_FILL_VARIANT = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const setVal = (inp, v) => { setter.call(inp, String(v)); inp.dispatchEvent(new Event('input', {bubbles:true})); inp.dispatchEvent(new Event('change', {bubbles:true})); };
  const PRICE = __PRICE__, DIMS = __DIMS__, WEIGHT = __WEIGHT__, MSRP = __MSRP__;
  const txt = e => ((e || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({err: 'no-skuDataInfo'});
  const tb = sku.querySelectorAll('tbody')[0];
  if (!tb) return JSON.stringify({err: 'no-first-tbody'});

  // 【列位置按表头定位，不写死下标】2026-08-28 真站取证（草稿 173539495451708963,
  // 类目仿真花）：无尺码类目的变种表【没有「尺码」列】，表头是
  //   [预览图, 颜色, SKU货号, EAN/UPC/ISBN, 申报价格(CNY), 尺寸(cm), 重量(g), 建议售价]
  // 而原实现写死 tds[3]=申报价 / tds[4]=尺寸 / tds[5]=重量 / tds[6]=建议售价——
  // 那套下标只在【服装表头多一列「尺码」】时才对得上，非服装类整体错位一列：
  // 188.88 被写进 EAN 列、建议售价 13.59 落到申报价列、尺寸三个框全空，
  // 页面逐行红字「尺寸不能为0或空」，save 被平台拒（实跑回读证据：price=13.59、
  // skuLength/skuWidth/skuHeight 全空）。
  //
  // 【一套判据同时覆盖服装与非服装，不做分支】服装表头有「尺码」列时申报价在
  // tds[3]、非服装在 tds[4]，按表头文字找「申报价」两种都命中，不必也不该按类目
  // 分叉——分支会让两条路各自漂移，而这里要的恰恰是「以页面真实表头为准」这一条
  // 规则（与 ⑩a 货号列同一次修复、也与项目「按 Sheet 真实表头写入」的既有约定一致）。
  // 表头文案带「(批量)」这类后缀，故一律用 includes 而不是全等。
  const heads = Array.from(sku.querySelectorAll('thead th')).map(txt);
  const findCol = (...keys) => heads.findIndex(h => keys.some(k => h.includes(k)));
  // 变种维列（本段只拿来回读 sample 里的行标识，不参与写值）按结构位置认，与 ⑩a/⑦a/⑦b
  // 共用 variant_dom._JS_DIM_COLS：车贴类目那维叫「型号」，按名字认会读成空串，
  // 让 sample 与 bad 的日志失去行标识（写值本身按申报价/尺寸/重量列，不受影响）。
  __DIM_COLS__
  const {colorIdx: iColor, sizeIdx: iSize} = dimIdx(heads);
  const iPrice = findCol('申报价');
  const iDims = findCol('尺寸');
  const iWeight = findCol('重量');
  const iMsrp = findCol('建议售价');
  // 缺列直接报出来：静默按下标猜正是这次的病根
  const miss = [];
  if (iPrice < 0) miss.push('申报价');
  if (iDims < 0) miss.push('尺寸');
  if (iWeight < 0) miss.push('重量');
  if (miss.length) return JSON.stringify({err: 'no-column:' + miss.join('/'), heads: heads});

  const rows = Array.from(tb.querySelectorAll('tr'));
  const results = [];
  const cellAt = (tds, i) => (i >= 0 && tds[i] ? tds[i] : null);
  for (const r of rows) {
    const tds = Array.from(r.querySelectorAll('td'));
    // 行必须够宽到含最靠右的目标列（原先写死 tds.length < 7，那也是按服装列数定的）
    if (tds.length <= Math.max(iPrice, iDims, iWeight, iMsrp)) continue;
    const color = txt(cellAt(tds, iColor));
    const size = iSize >= 0 ? txt(cellAt(tds, iSize)) : '';
    const priceInp = (cellAt(tds, iPrice) || document.createElement('td')).querySelector('input');
    const dimInps = Array.from((cellAt(tds, iDims) || document.createElement('td')).querySelectorAll('input'));
    const weightInp = (cellAt(tds, iWeight) || document.createElement('td')).querySelector('input');
    const msrpCell = cellAt(tds, iMsrp);
    // 建议售价格子里还有个币种下拉（USD/CNY…），它也是 input：只取第一个数值输入框
    const msrpInp = msrpCell ? msrpCell.querySelector('input:not(.ant-select-selection-search-input)') : null;
    if (priceInp && priceInp.value !== PRICE) setVal(priceInp, PRICE);
    dimInps.forEach((d, i) => { if (DIMS[i] && d.value !== DIMS[i]) setVal(d, DIMS[i]); });
    if (weightInp && weightInp.value !== WEIGHT) setVal(weightInp, WEIGHT);
    if (msrpInp && msrpInp.value !== MSRP) setVal(msrpInp, MSRP);
    results.push([color, size, priceInp?priceInp.value:'', dimInps.map(d=>d.value).join('x'),
                  weightInp?weightInp.value:'', msrpInp?msrpInp.value:'']);
  }
  await sleep(400);
  const bad = results.filter(x => x[2]!==PRICE || x[3]!==DIMS.join('x') || x[4]!==WEIGHT || x[5]!==MSRP);
  return JSON.stringify({count: rows.length, bad, sample: results.slice(0,4),
                         cols: {color: iColor, size: iSize, price: iPrice, dims: iDims,
                                weight: iWeight, msrp: iMsrp}, heads: heads});
})()"""


# 申报价默认值（人民币，写进变种表「申报价」列）。
# 【为什么是 188.88 而不是原来的 168】2026-08-25 用户确认：申报价按本店统一口径填，
# 不逐商品估；UI/CLI 想填实际售价时传 price 覆盖，不填就用这个默认值。
# 建议售价由它 ÷ 7 折算成美元（见 set_variant），改这里两列一起变。
DECLARE_PRICE_DEFAULT = "188.88"


def normalize_declare_price(price) -> str:
    """把外部传入的申报价归一成可直接写进输入框的数字串，非法值退回默认值。

    UI 是自由输入框，用户可能填 "￥188"、"188.888"、空格甚至中文，直接
    setVal 进去平台要么拦、要么静默截断。这里统一处理：
      - 抽第一段数字（含小数点），最多保留 2 位小数（平台申报价就是两位）
      - 落在 0 < p <= 100000 之外的一律退默认值并告警（不静默）
    不抛异常：定价填错不该让整个商品发布中断，退默认值是可核对的确定行为。
    """
    raw = str(price if price is not None else "").strip()
    if not raw:
        return DECLARE_PRICE_DEFAULT
    # 【负号必须纳入匹配】只写 \d+ 会从 "-5" 里提出 "5"，负数就被当成合法价放行了
    # （与数值属性行校验踩过的同一个坑，见 _validate_attr_changes 里的同名注释）。
    m = re.search(r"-?\d+(?:\.\d+)?", raw)
    if not m:
        logger.warning(f"申报价 {raw!r} 里没有数字，按默认 {DECLARE_PRICE_DEFAULT} 处理")
        return DECLARE_PRICE_DEFAULT
    val = round(float(m.group()), 2)
    if not 0 < val <= 100000:
        logger.warning(f"申报价 {raw!r} 超出 0~100000 合理范围，"
                       f"按默认 {DECLARE_PRICE_DEFAULT} 处理")
        return DECLARE_PRICE_DEFAULT
    # 去掉多余的 .0 / .00：页面回读的是 "188" 而不是 "188.0"，
    # 不归一会让 _JS_FILL_VARIANT 的回读比对全判成 bad。
    out = f"{val:.2f}".rstrip("0").rstrip(".")
    if out != raw:
        logger.info(f"申报价归一：{raw!r} → {out}")
    return out


async def set_variant(session: BrowserSession, info_path: str,
                      price: str = "", dims: Optional[str] = None,
                      weight: Optional[str] = None, cat_path=None,
                      pack_est: Optional[dict] = None) -> dict:
    """阶段⑩：变种信息批量填写（申报价/尺寸/重量/建议售价）。

    规则（2026-08-18 定，2026-08-25 按用户结论修订申报价与尺寸）：
    - 货号不动（fix_sizes 已保证 <颜色>-<尺码>）
    - 申报价默认 188.88（DECLARE_PRICE_DEFAULT），UI/CLI 可传 price 覆盖
    - 尺寸 = 打包后长宽高（cm）。取值优先级：显式 dims > 服装类固定 30x25x3
      > 源 packInfo > LLM 预估。服装类不问模型的理由见 _APPAREL_DIMS 注释。
    - 重量 = 打包后克重(g)：源 packInfo.unitWeightKg×1000，没有交 LLM 预估；weight 覆盖
    - 建议售价 = 申报价 ÷ 7（币种列保持页面默认 USD）

    cat_path：编辑页已生效的类目路径，用来判服装类；为空退到标题判定。
    pack_est：提前预热的包装估算结果（见 service._run_prewarm）。命中就省掉本阶段
    的 LLM 调用；它是按超集问的，用前仍过一遍 _check_pack_est 确认本次要的字段都在。
    """
    from app.publish.llm import ask_json

    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)
    pack = info.get("packInfo") or {}
    title = info.get("title", "")
    price = normalize_declare_price(price)

    # 重量：参数 → 源 packInfo → LLM（与尺寸一样，显式传入的最高优先）
    w_g = weight
    if not w_g and pack.get("unitWeightKg"):
        w_g = str(round(float(pack["unitWeightKg"]) * 1000))

    # 尺寸：参数 → 服装类固定值 → 源 packInfo → LLM
    # 【服装类排在源 packInfo 之前】源 packInfo 是 1688 卖家填的整箱/散件口径，
    # 与本店压平装快递袋的实际包裹无关；服装既已有确定规格，就不该被源值带偏。
    d_list = None
    apparel = packaging._is_apparel(cat_path, title)
    if dims:
        d_list = [x.strip() for x in re.split(r"[x×*]", dims)]
    elif apparel:
        d_list = list(packaging._APPAREL_DIMS)
        logger.info(f"服装类包裹尺寸按固定规格 {'x'.join(d_list)}cm（不走模型估算）")
    elif pack.get("dimsCm"):
        d_list = [str(x) for x in pack["dimsCm"]]

    # LLM 预估兜底（预热命中就不再问，见 estimate_pack 的说明）
    if not w_g or not d_list:
        est = None
        if pack_est:
            # 预热结果按【本次实际要哪些字段】校验一遍再用：预热时 cat_path 还没有，
            # 服装判定可能与此刻不同，故不能假定它一定备齐了本次要用的字段。
            if not packaging._check_pack_est(pack_est, need_dims=not d_list, need_weight=not w_g):
                est = pack_est
                logger.info("包装尺寸重量沿用提前预热的估算结果，跳过本阶段 LLM 调用")
            else:
                logger.info("预热的包装估算不满足本次所需字段，现场重新估算")
        if est is None:
            est = await packaging.estimate_pack(info, need_dims=not d_list, need_weight=not w_g)
        if not d_list:
            d_list = [str(est["长"]), str(est["宽"]), str(est["高"])]
        if not w_g:
            w_g = str(est["重量"])

    # 【平台硬校验：长 >= 宽 >= 高】2026-08-28 真站取证（用户截图）：填 25x20x30 后
    # 尺寸列每一行都挂红字「尺寸长宽高需要满足长≥宽≥高」，save 被拒。
    # 这三个数描述的是同一个盒子，谁叫「长」只是命名问题，降序排一遍即可满足，
    # 不改变申报的实际体积、也不牵动运费——所以是归一而不是 fallback。
    # 只有【模型估算】那条路会给出乱序（本次 25x20x30 就是模型给的）；
    # 服装固定值 30x25x3 与兜底 30x24x5 本来就是降序，排序对它们是恒等操作。
    d_list = packaging._order_dims(d_list)

    if len(d_list) != 3:
        return {"status": "error", "reason": f"尺寸需为 长x宽x高 三个值: {d_list}"}

    # 【平台硬校验：材积重量 <= 实际重量】2026-09-01 真站取证（宠物服装发布失败）：
    # 接口报错「材积重量大于实际重量，无法录入，请调整体积或者重量」。
    # 材积重量 = 长×宽×高÷6，单位 g。宠物衣服等体积相对大但实际很轻的商品易触发。
    # 调整策略：优先上调重量到材积重量（保持尺寸不变，运费按体积重算本就该如此）；
    # 若上调后超出 30kg 上限才考虑压缩尺寸（罕见，此时已是超大件）。
    dims_float = [float(d) for d in d_list]
    volume_weight = (dims_float[0] * dims_float[1] * dims_float[2]) / 6
    weight_float = float(w_g)
    if volume_weight > weight_float:
        # 上调重量到材积重量，向上取整（平台按整数 g 计）
        adjusted_weight = int(volume_weight) + 1
        if adjusted_weight <= 30000:
            logger.info(
                f"材积重量 {volume_weight:.1f}g > 实际重量 {w_g}g，"
                f"上调重量到 {adjusted_weight}g（尺寸 {'x'.join(d_list)}cm 不变）"
            )
            w_g = str(adjusted_weight)
        else:
            # 极端情况：材积重量超 30kg（如 50x50x72cm），此时等比压缩尺寸到材积重 30kg
            # 保持长宽高比例，体积缩到原来的 (30000*6 / 原体积) 倍，各边乘 该比例的立方根
            target_vol = 30000 * 6  # 30kg 对应的 cm³
            current_vol = dims_float[0] * dims_float[1] * dims_float[2]
            ratio = (target_vol / current_vol) ** (1/3)
            d_list = [str(int(d * ratio)) for d in dims_float]
            w_g = "30000"
            logger.warning(
                f"材积重量 {volume_weight:.1f}g 超出 30kg 上限，"
                f"等比压缩尺寸到 {'x'.join(d_list)}cm（材积重 30kg）"
            )

    msrp = str(round(float(price) / 7, 2)).rstrip("0").rstrip(".")

    # 【等变种表自己渲染出来，不能只等容器】2026-09-01 取证（--from-stage variant
    # 直接进编辑页）：open_edit 的判据是 skuDataInfo 这个容器 div 存在，而容器属于
    # 页面骨架，它出现时里面先渲染的是【库存表】——此刻 skuDataInfo 下只有一张表，
    # 表头 ['SKU货号', '包装清单']、0 行，_JS_FILL_VARIANT 取 tbody[0] 拿到的正是它，
    # 于是报 no-column:申报价/尺寸/重量。约 2s 后变种表才挂上，且挂在库存表【之前】
    # （DOM 序 tbody[0] 才成立）。全流程跑时前面十几个阶段耗时几分钟，掩盖了这个竞态。
    # 判据用「申报价 + 尺寸 + 重量三列齐备」而不是表数量：要等的就是这三列可定位。
    js_wait = r"""(() => {
      const sku = document.getElementById('skuDataInfo');
      if (!sku) return JSON.stringify({ready: false, reason: 'no-skuDataInfo'});
      const txt = e => ((e||{}).textContent||'').replace(/\s+/g, ' ').trim();
      const heads = Array.from(sku.querySelectorAll('thead th')).map(txt);
      const has = k => heads.some(h => h.includes(k));
      return JSON.stringify({
        ready: has('申报价') && has('尺寸') && has('重量'),
        heads: heads,
      });
    })()"""
    ready = await session.wait_for(js_wait, lambda d: d.get("ready"), timeout=30)
    if not ready.get("ready"):
        return {"status": "error",
                "reason": f"变种表未渲染出申报价/尺寸/重量列（表头 {ready.get('heads')}）"}

    js = (_JS_FILL_VARIANT
          .replace("__PRICE__", J(str(price)))
          .replace("__DIMS__", J(d_list))
          .replace("__WEIGHT__", J(str(w_g)))
          .replace("__MSRP__", J(msrp))
          .replace("__DIM_COLS__", variant_dom._JS_DIM_COLS))
    res = await session.eval_json(js)
    # 缺列是硬错误：过去按下标猜列时，错位表现为「填了但填错格子」，一路带到 save
    # 才被平台以含糊文案拒掉（见 _JS_FILL_VARIANT 的取证）。宁可在这里就失败。
    if res.get("err"):
        return {"status": "error",
                "reason": f"变种表列定位失败：{res['err']}（表头 {res.get('heads')}）"}
    ok = res.get("count", 0) > 0 and not res.get("bad")
    if res.get("cols"):
        logger.info(f"变种表列定位（按表头）：{res['cols']}")
    return {"status": "ok" if ok else "validation-error",
            "price": price, "dims": d_list, "weight": w_g, "msrp": msrp,
            "rowCount": res.get("count"), "bad": res.get("bad"),
            "sample": res.get("sample"), "cols": res.get("cols")}
