"""店小秘发布操作：navigation。模块导航见 docs/publish-pipeline-refactor.md。"""

import os
import re
from app.logger import logger
from app.publish import common, images, variant_dom
from app.publish.browser import BrowserSession, DRAFT_LIST_URL, EDIT_URL, J
from app.publish.media import preview as media_preview
from typing import Optional


# 编辑页右侧锚点导航的 7 个区块 ID（用 getElementById 定位比文本匹配稳定）
SECTION_IDS = {
    "productBasicInfo": "基本信息",
    "dxmInfo": "店小秘信息",
    "productProductInfo": "产品信息",
    "skuAttrsInfo": "变种属性",
    "skuDataInfo": "变种信息",
    "describeInfo": "产品描述",
    "shipmentInfo": "运输信息",
}


async def open_edit(session: BrowserSession, rowid: str) -> dict:
    """在会话内打开编辑页，等表单加载完成。

    刻意拼 URL 而不点草稿列表里的「编辑」按钮：那是个无 href 的 <button>，点击后
    在【新标签页】打开，会跑出当前会话拿不到（原脚本实测结论）。
    以 skuDataInfo 区块出现为加载完成判据——它在页面靠后，出现说明表单渲染完了。
    """
    url = EDIT_URL.format(rowid=rowid)
    logger.info(f"打开编辑页：{url}")
    r = await session.navigate(url)
    if not r.get("ok"):
        raise RuntimeError(f"导航失败: {r}")
    data = await session.wait_for(
        "JSON.stringify({loaded: !!document.getElementById('skuDataInfo'), "
        "title: document.title})",
        lambda d: d.get("loaded"), timeout=40)
    if not data.get("loaded"):
        raise RuntimeError("编辑页加载超时（未登录或 rowid 无效）")
    logger.info(f"编辑页已加载：{data.get('title')}")
    return {"rowid": rowid, "url": url, "title": data.get("title")}


async def find_rowid(session: BrowserSession, keyword: str) -> dict:
    """草稿列表按标题关键词找 rowid（只读，不点任何按钮）。

    rowid 是草稿列表行的 <tr rowid="..."> 属性（18 位数字），后续所有阶段都靠它拼
    编辑页 URL。一个商品可能被认领到多个站点、出现多行，故返回全部匹配交调用方按
    店铺/站点筛（见 service 层的 pick_rowid）。

    【text 必须够长】调用方（claim.pick_rowid）就是拿这个 text 按站点/店铺筛行的，
    而这两列排在标题【之后】：2026-09-12 实测 Temu 英文标题单是标题就 230+ 字符，
    原来 slice(0,120) 把站点和店铺整段切掉，于是报「草稿列表未找到店铺/站点」——
    而同一行人在页面上看得清清楚楚。1688 标题短（中文 30 来字）所以一直没暴露。
    500 足够容纳长标题 + 站点 + 店铺。
    """
    r = await session.navigate(DRAFT_LIST_URL)
    if not r.get("ok"):
        raise RuntimeError(f"导航失败: {r}")
    js = r"""(() => {
      const rows = Array.from(document.querySelectorAll('tr[rowid]'));
      const m = rows.filter(r => (r.textContent||'').includes(__KW__))
        .map(r => ({rowid: r.getAttribute('rowid'),
                    text: (r.textContent||'').replace(/\s+/g,' ').slice(0,500)}));
      return JSON.stringify({total: rows.length, matched: m});
    })()""".replace("__KW__", J(keyword))
    data = await session.wait_for(js, lambda d: d.get("total", 0) > 0, timeout=30)
    return data


# ---- inspect（只读导出表单状态）---------------------------------------------
# 按 label 读当前值：ant-design 的值可能在 input、已选下拉文本、或勾中的 radio 上，
# 三处都要试。顺序有讲究——radio 优先于 select，因为部分行两者都存在
# （如「敏感属性」是 radio，但同一 form-item 里可能还有个联动下拉的残留 DOM）。
# 【必须用 r"""】JS 正则里的 \s 若走普通字符串就得写成 \\s，一旦被格式化工具"修正"成
# \s，Python 转义后 JS 收到的是字面 s，正则 [*\s] 就变成「匹配星号或字母s」，会把
# label 里的 s 删掉。raw string 让两边看到的都是同一个 \s，不给这种改动留缝隙。
_JS_INSPECT = r"""(() => {
  const out = {sections: {}, form: [], skus: [], shipping: {}};

  document.querySelectorAll('.ant-form-item').forEach(item => {
    const lab = item.querySelector('.ant-form-item-label');
    const label = lab ? (lab.textContent||'').trim().replace(/[*\s]/g,'') : '';
    if (!label) return;
    const ctl = item.querySelector('.ant-form-item-control') || item;
    const sel = ctl.querySelector('.ant-select-selection-item');
    const inp = ctl.querySelector('input:not([type=hidden]), textarea');
    const radio = ctl.querySelector('.ant-radio-wrapper-checked');
    let value = null;
    if (radio) value = (radio.textContent||'').trim();
    else if (sel) value = (sel.textContent||'').trim();
    else if (inp) value = inp.value;
    if (value !== null && value !== '') out.form.push({label, value: String(value).slice(0,120)});
  });

  const skuSec = document.getElementById('skuDataInfo');
  if (skuSec) {
    skuSec.querySelectorAll('tr').forEach(tr => {
      const tds = Array.from(tr.querySelectorAll('td'));
      if (tds.length < 3) return;
      const rowText = tds.map(td => (td.textContent||'').trim().slice(0,20));
      const inputs = Array.from(tr.querySelectorAll('input:not([type=hidden])'))
        .map(i => i.value || '');
      if (rowText.some(t => t)) out.skus.push({cells: rowText.slice(0,4), inputs});
    });
  }

  const ship = document.getElementById('shipmentInfo');
  if (ship) {
    const checked = ship.querySelector('.ant-radio-wrapper-checked, .ant-radio-button-wrapper-checked');
    const tpl = ship.querySelector('.ant-select-selection-item');
    out.shipping = {
      deadline: checked ? (checked.textContent||'').trim() : null,
      freightTemplate: tpl ? (tpl.textContent||'').trim() : null,
    };
  }

  for (const id of ['productBasicInfo','dxmInfo','productProductInfo','skuAttrsInfo','skuDataInfo','describeInfo','shipmentInfo'])
    out.sections[id] = !!document.getElementById(id);

  return JSON.stringify(out);
})()"""


async def inspect(session: BrowserSession, rowid: str) -> dict:
    """打开编辑页并导出当前表单状态（只读，不改任何字段）。

    用途：搬运写入类子命令前先看清页面此刻长什么样；出问题时对比预期与实际。
    """
    info = await open_edit(session, rowid)
    # 等 Vue 把表单项渲染出来（原固定 sleep(2)）。
    # 【判据是表单项数量，不是区块存在】区块 div 几乎立刻挂上，而 _JS_INSPECT 读的是
    # .ant-form-item —— 只判容器会 0ms 返回、读到半张表（form 项残缺，且完全静默）。
    await common._poll_until(
        lambda: session.eval_json(
            "(() => JSON.stringify({n: "
            "document.querySelectorAll('.ant-form-item').length}))()"),
        lambda d: d.get("n", 0) >= 10,   # 编辑页表单项远多于 10，够用来判「渲染开始」
        timeout=2.0)
    data = await session.eval_json(_JS_INSPECT)
    return {"status": "ok", **info, **data}


# ---- 图片清单（只读，发布前合规检查用）--------------------------------------
# 三组图分别在不同区块：素材图在产品信息区、颜色图在变种属性区（按行分组）、
# 描述长图在产品描述区。只收 http(s) 图，过滤掉 base64 占位图和图标。
_JS_ALL_IMAGES = r"""(() => {
  const out = {material: [], colors: [], desc: []};
  const httpImg = el => (el.currentSrc || el.src || '').startsWith('http');

  const pi = document.getElementById('productProductInfo');
  if (pi) out.material = Array.from(pi.querySelectorAll('img')).filter(httpImg)
    .map(i => i.currentSrc || i.src);

  const sku = document.getElementById('skuAttrsInfo');
  if (sku) sku.querySelectorAll('tr').forEach(tr => {
    const tds = tr.querySelectorAll('td');
    if (!tds.length) return;
    const color = (tds[0].textContent||'').trim().slice(0,20);
    const urls = Array.from(tr.querySelectorAll('img')).filter(httpImg)
      .map(i => i.currentSrc || i.src);
    if (urls.length) out.colors.push({color, urls});
  });

  const desc = document.getElementById('describeInfo');
  if (desc) out.desc = Array.from(desc.querySelectorAll('img')).filter(httpImg)
    .map(i => i.currentSrc || i.src);

  return JSON.stringify(out);
})()"""


async def list_images(session: BrowserSession, rowid: str,
                      outdir: Optional[str] = None) -> dict:
    """提取素材图/颜色图/描述图 URL，可选下载到本地供逐张合规检查。

    只读页面，不改任何数据。outdir 为 None 则只返回 URL 清单不下载。
    图片来源全是采集源（1688 等），可能含中文/水印/第三方 logo——发布前必须逐张过目，
    这是服装类发布最常被弹回的一环（见 SKILL.md 的图片硬校验）。
    """
    await open_edit(session, rowid)
    # 等图片真渲染出来（原固定 sleep(2)）。
    # 【判据是图片数量，不是区块存在】只判容器会 0ms 返回、读到空图单（静默漏图，
    # 而本函数的产物直接决定后续合规检查查什么）。判「有 http 图」与 _JS_ALL_IMAGES
    # 的取图口径一致。
    await common._poll_until(
        lambda: session.eval_json(r"""(() => {
            const n = Array.from(document.querySelectorAll('img'))
              .filter(i => (i.currentSrc || i.src || '').startsWith('http')).length;
            return JSON.stringify({n});
        })()"""),
        lambda d: d.get("n", 0) > 0,
        timeout=2.0)
    data = await session.eval_json(_JS_ALL_IMAGES)
    groups = {
        "material": len(data.get("material", [])),
        "colors": sum(len(g["urls"]) for g in data.get("colors", [])),
        "desc": len(data.get("desc", [])),
    }
    result = {"status": "ok", "urls": data, "groups": groups}
    if not outdir:
        return result

    # 下载复用 extract 里那套带浏览器头 + 重试的实现：店小秘图床同样会拦裸请求，
    # 原脚本用的 urlretrieve 无 UA、一次失败就丢图。
    from app.publish.extract import _download_image
    os.makedirs(outdir, exist_ok=True)
    files, failed = [], []

    def dl(url: str, name: str) -> None:
        ext = ".jpg"
        for e in (".jpeg", ".png", ".webp"):
            if e in url:
                ext = e
                break
        path = os.path.join(outdir, name + ext)
        try:
            if not _download_image(url, path):
                failed.append({"url": url[:120], "err": "源站取不到（404 等），已跳过"})
                logger.warning(f"{name} 源站取不到，跳过")
                return
            files.append(path)
        except Exception as e:
            failed.append({"url": url[:120], "err": str(e)})
            logger.warning(f"{name} 下载失败：{e}")

    for i, u in enumerate(data.get("material", []), 1):
        dl(u, f"material-{i:02d}")
    for grp in data.get("colors", []):
        # 颜色名可能含 Windows 文件名非法字符（斜杠等），清一遍再当文件名用
        color = re.sub(r'[\/:*?"<>|]', "_", grp["color"] or "unknown")
        for i, u in enumerate(grp["urls"], 1):
            dl(u, f"color-{color}-{i:02d}")
    for i, u in enumerate(data.get("desc", []), 1):
        dl(u, f"desc-{i:02d}")

    result.update({
        "status": "ok" if not failed else "partial",
        "outdir": outdir, "count": len(files), "files": files, "failed": failed,
        "note": "请逐张检查中文文字/水印/第三方logo后再决定是否发布",
    })
    return result


# ---- 编辑页实况探测（续跑判据）---------------------------------------------
# 【为什么需要它：状态文件记的是「跑过」，不是「存住了」】2026-08-23 实测
# 947662049255：状态文件里 ③~⑬ 十几个阶段全是 ok，但重开编辑页取证发现服务端
# 几乎什么都没存——标题空、变种属性区连尺码行都没有（0 行）、尺码表回到「添加尺码表」、
# 运费模板「请选」、描述图全部退回 cbu01.alicdn.com 的 1688 原始外链。
#
# 根因是页面生命周期：⑤~⑬ 全部只改【未保存的表单】，成果要靠 ⑭ save 一次性提交。
# save 没成功过，这些阶段就等于没跑——页签一关、Chrome 一退、或 open_edit 重新导航，
# 全丢。而续跑只看状态文件、把它们当 ok 跳过，直奔 save 拿一张空表单去提交，于是
# 每次都卡在同一句「产品信息、变种信息」校验未过，人工怎么点都出不来。
#
# 【③ 类目与 ④ 属性同样不保证存住，2026-08-24 实测推翻旧结论】原先这里写「服务端
# 持久化的只有 ③④，故可按状态文件跳过」——那是 2026-08-21 在一个 save 成功过的草稿上
# 取的证，看到的是已落库的值。890843533224 这单证明：save 从未成功时，③ 选定的
# 「女士运动卫裤」一样丢，页面回落到认领时带来的旧类目「其他（女装长裤）」，而该类目
# 已被平台下线，编辑页据此弹 d-message-error「该分类已在平台删除！」、分类行下方显示
# 暗红「未选择分类」。类目没定 → 变种属性区不渲染尺码行（skuRowCount=0）→ ⑧⑨⑩⑪ 全部
# 无处可填 → save 必然卡在「产品信息、变种信息」校验未过。
# 故 ③④ 也要读实况：catUnset / catDeleted 即判定类目丢了，须从 ③ 重跑（属性行由类目
# 决定，类目一换属性区整体重建，只补 ④ 没有意义，见 service._stale_form_stages）。
#
# 【判据刻意取「粗而确定」的信号】比如 ⑧ 只看变种表有没有行、⑤ 只看标题非空，
# 不去逐字比对内容是否与源一致——那需要重算一遍 LLM 产物，成本高且会引入新的
# 判错风险。重跑一个已完成的表单阶段是幂等的（各阶段本身都做了「已有值就不动」
# 或直接覆盖），代价只是时间；漏跑一个丢了的阶段才会导致整单卡死。
_JS_LIVE_STATE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 表单懒渲染：等 SKU 属性区出现再读，否则会把「还没渲染」误判成「数据丢了」。
  //
  // 【判据不能只看尺码表 label】原先只等 /^尺码表2?$/ 出现。2026-08-28 真站取证
  // （1014675972015 仿真花）：非服装类目【永远没有】尺码表栏，这个循环必然空转满
  // 40×300ms=12s，然后 rendered=false，于是 _stale_form_stages 每次都返回「全部
  // 需重跑」（含③类目）——非服装商品的续跑判定永久失灵，且每次白等 12s。
  // 改成「尺码表栏 或 变种属性区已渲染出复选框」两者任一即可：后者对无尺码类目同样
  // 成立（那 6 个复选框是颜色），而两者都没有才是真的没渲染完。
  const _scReady = () => Array.from(document.querySelectorAll('label'))
    .some(l => /^尺码表2?$/.test((l.textContent || '').trim()));
  const _attrReady = () => {
    const a = document.getElementById('skuAttrsInfo');
    return !!a && a.querySelectorAll('label.d-checkbox').length > 0;
  };
  for (let i = 0; i < 40; i++) {
    if (_scReady() || _attrReady()) break;
    await sleep(300);
  }
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const host = u => { try { return new URL(u, location.href).host; } catch (e) { return ''; } };
  // 变种维列的共用判据（下方 variantByColor 用 dimIdx）：与 ⑦a/⑦b/⑩a/⑩ 同一段代码
  __DIM_COLS__

  // ⑤ 标题：按 label 定位「英文标题」框——它空或含中文（非 ASCII）都说明英文标题
  // 成果已丢/未填，要重跑。不能抓基本信息区所有框：中文标题框（产品标题）本来就
  // 该填中文，抓它会恒判「含中文」、让 titles 每次续跑都白重跑。
  const _labelVal = (label) => {
    const it = Array.from(document.querySelectorAll('.ant-form-item'))
      .find(el => {
        const l = el.querySelector('.ant-form-item-label label');
        return l && (l.getAttribute('title')||l.textContent||'').trim() === label;
      });
    const inp = it ? it.querySelector('input:not([type=hidden]), textarea') : null;
    return inp ? (inp.value || '').trim() : '';
  };
  const enTitle = _labelVal('英文标题');
  const titleHasCjk = !!enTitle && Array.from(enTitle).some(c => c.charCodeAt(0) > 127);

  // ⑧ 尺码勾选：变种信息表的行数（0 行 → ⑨⑩⑪ 全都无处可填，必须从 ⑧ 起重跑）
  const sku = document.getElementById('skuDataInfo');
  const skuRows = Array.from((sku || document).querySelectorAll('tbody tr'));

  // ⑩⑪ 变种/库存：行内 input 有值即算填过（申报价/重量/库存都在这张表里）
  const skuFilled = skuRows.filter(tr =>
    Array.from(tr.querySelectorAll('input')).some(i => (i.value || '').trim())).length;

  // ⑩a SKU 货号：空或含非 ASCII（中文/全角符号）都过不了平台校验，要重跑。
  // 【不能靠 skuFilled 顺带判】货号列自己就是 input，平台「一键生成」的中文值会让
  // skuFilled 非 0，看起来像填过了，实际正是要修的那个值（2026-08-23 实测）。
  const skuCodeInps = Array.from((sku || document).querySelectorAll('input[name=variationSku]'));
  const skuCodeBad = skuCodeInps.filter(i => {
    const v = (i.value || '').trim();
    return !v || Array.from(v).some(c => c.charCodeAt(0) > 127);
  }).length;

  // ⑨ 尺码表：控件文本仍是「添加尺码表」说明没加。
  // 套装商品有两张表（label「尺码表」「尺码表2」），按 label 取而不是靠
  // .skuAttrSizeChart——那个类只挂在第一张上，第二张没加会被判成「已加」而不重跑
  // （2026-08-27 实测，见 add_sizechart 的【套装要两张尺码表】）。
  const _scLabs = Array.from(document.querySelectorAll('label'))
    .filter(l => /^尺码表2?$/.test((l.textContent || '').trim()));
  const _scTexts = _scLabs.map(l => {
    const it = l.closest('.ant-form-item');
    return txt(it && it.querySelector('.ant-form-item-control-input'));
  });
  const scText = _scTexts[0] || '';
  const scText2 = _scTexts.length > 1 ? _scTexts[1] : null;   // null = 该类目没有第二张表
  const scCount = _scLabs.length;

  // ⑥⑦ 素材图/SKC：变种属性区的图（重载后一张都没有说明全丢）
  const attrArea = document.getElementById('skuAttrsInfo');
  const attrImgs = Array.from((attrArea || document).querySelectorAll('img'))
    .filter(i => /^https?:/.test(i.src || ''));
  // 【张数够 ≠ 尺寸合规】某颜色行的图可能压根没被 ⑦ 换过（视觉分不出该色的图时整行
  // 跳过），留着的是 1688 原始小图，attrImgCount 照样非 0。2026-08-24 实测：咖啡色
  // 行 6 张全是 1000x1000 / 1200x1200，save 报「服装类图片尺寸不能小于1340px*1785px」。
  // 变种属性区的复选框数与「有没有尺码组」：渲染判据与 ⑧⑨ 跳过的复核信号
  const attrCbCount = (attrArea || document).querySelectorAll('label.d-checkbox').length;
  const hasSizeGroup = Array.from((attrArea || document).querySelectorAll('.ant-form-item'))
    .some(it => {
      const l = it.querySelector('.ant-form-item-label');
      const lab = (l ? l.textContent : '').trim();
      return lab === '尺码' || (lab.includes('尺码') && !lab.includes('尺码表'));
    });
  // ⑦a 剔配件色：变种信息表按颜色统计「有源数据的行数」。续跑判定据此认配件色
  // （反选只改未保存表单，save 没成功过就会回到认领时的全勾状态）。
  // 判据与列定位同 _JS_VARIANT_ROW_FILL（都注入 variant_dom._JS_DIM_COLS：变种维列
  // 按「预览图之后、SKU货号之前」的结构位置认，不按颜色/尺码这两个名字猜），见那里的
  // 真站取证。两边判据必须一致：这里认不出维度列会让续跑判定永不安排 ⑦a。
  const variantByColor = (() => {
    const t0 = (sku || document).querySelector('table');
    if (!t0) return null;
    const hs = Array.from(t0.querySelectorAll('thead th')).map(th => txt(th));
    const iC = dimIdx(hs).colorIdx;
    const iCode = hs.findIndex(h => h.includes('SKU货号'));
    const iPrice = hs.findIndex(h => h.includes('申报价格'));
    const iW = hs.findIndex(h => h.includes('重量'));
    if (iC < 0) return null;
    const v = (tds, k) => {
      if (k < 0 || !tds[k]) return '';
      const ins = Array.from(tds[k].querySelectorAll('input, textarea'));
      for (const x of ins) { const q = (x.value || '').trim(); if (q) return q; }
      return '';
    };
    const acc = {};
    Array.from(t0.querySelectorAll('tbody tr')).forEach(tr => {
      const tds = Array.from(tr.children);
      const c = txt(tds[iC] || null);
      if (!acc[c]) acc[c] = {total: 0, filled: 0};
      acc[c].total += 1;
      if (v(tds, iCode) || v(tds, iPrice) || v(tds, iW)) acc[c].filled += 1;
    });
    return Object.keys(acc).length ? acc : null;
  })();
  const attrImgBad = attrImgs.filter(i => {
    const w = i.naturalWidth || 0, h = i.naturalHeight || 0;
    return w && h && (w < __MINW__ || h < __MINH__);
  }).length;

  // ⑦b SKU 预览图：变种【信息】表第一列，与上面 attrImg* 那组（变种【属性】区）
  // 是两处不同的图，续跑必须分开判——2026-08-30 玩具类那单正是 ⑦ 合理跳过、
  // ⑦b 从未跑过而被平台拒（见「阶段⑦b SKU 预览图」段落的取证）。
  // 判据与 sku_preview_state 一致：非 1:1 或短边 < __PREVMIN__ 即 bad；
  // 尺寸未知（naturalWidth=0，图没加载完）不计入 bad——未知不等于不合格。
  const prevTable = (document.getElementById('skuDataInfo') || document)
    .querySelector('table');
  const prevHeads = prevTable
    ? Array.from(prevTable.querySelectorAll('thead th')).map(th => txt(th)) : [];
  const iPrevCol = prevHeads.findIndex(h => h.includes('预览图'));
  let previewCount = 0, previewBad = 0;
  if (prevTable && iPrevCol >= 0) {
    Array.from(prevTable.querySelectorAll('tbody tr')).forEach(tr => {
      const cell = Array.from(tr.children)[iPrevCol];
      if (!cell) return;
      const im = Array.from(cell.querySelectorAll('img'))
        .find(x => /^https?:/.test(x.currentSrc || x.src || ''));
      if (!im) return;
      previewCount++;
      const w = im.naturalWidth || 0, h = im.naturalHeight || 0;
      if (w && h && (Math.abs(w / h - 1) >= 0.01 || w < __PREVMIN__ || h < __PREVMIN__)) {
        previewBad++;
      }
    });
  }

  // ③ 类目：三个信号一起读，任一命中即判「类目丢了/失效」，须从 ③ 重跑。
  // catText 取分类下拉的当前值（回落到认领旧值时这里是「其他（...）」这类占位类目）；
  // catUnset 读分类行下方那个暗红提示块（.category-list，实测文案「未选择分类」）——
  //   下拉有值不代表类目有效，这个块才是页面自己的判定结论；
  // catDeleted 抓页面弹的 d-message-error「该分类已在平台删除！」——它几秒后自动消失，
  //   故实况探测顺手取一次，不然日志里查不到这条关键线索。
  const catItem = Array.from(document.querySelectorAll('.ant-form-item')).find(it => {
    const lab = it.querySelector('.ant-form-item-label');
    return lab && (lab.textContent || '').includes('产品分类');
  });
  const catText = catItem ? txt(catItem.querySelector('.ant-select-selection-item')) : '';
  const catListEl = (document.getElementById('productBasicInfo') || document)
    .querySelector('.category-list');
  const catListText = txt(catListEl);
  const catDeleted = Array.from(document.querySelectorAll('.d-message-error, .d-message'))
    .map(e => txt(e)).some(t => t.includes('分类已在平台删除'));

  // ⑫ 运输：时效 radio 与运费模板
  const ship = document.getElementById('shipmentInfo');
  const shipSel = ship && ship.querySelector('.ant-select-selection-item');
  const shipRadio = ship && ship.querySelector(
    '.ant-radio-button-wrapper-checked, .ant-radio-wrapper-checked');

  // ⑬ 描述：全是来源站外链（非店小秘图床）说明删图/英化的成果没了。
  // 正向匹配店小秘主域（与 DXM_IMAGE_HOST_MARK 同口径），不枚举各家来源图床
  // （1688 cbu*.alicdn.com / Temu img.kwcdn.com / 拼多多七牛各有各的域名）
  const desc = document.getElementById('describeInfo');
  const descImgs = Array.from((desc || document).querySelectorAll('img'))
    .map(i => host(i.src)).filter(Boolean);
  const foreign = descImgs.filter(h => !h.includes('dianxiaomi.com'));

  return JSON.stringify({
    // 【渲染完＝尺码表栏出现 或 变种属性区已有复选框】与上面那个等待循环同判据。
    // 只看尺码表栏会让无尺码类目（仿真花等）恒为 false，续跑判定永久返回「全部重跑」，
    // 见循环处的取证说明。attrCbCount 一并报出来，便于排查时区分两条命中路径。
    rendered: scCount > 0 || attrCbCount > 0,
    attrCbCount: attrCbCount,
    // 本类目有没有尺码维：⑧⑨ 的 skipped 是否合理要靠它复核（无尺码表栏 + 无尺码组）
    hasSizeGroup: hasSizeGroup,
    variantByColor: variantByColor,
    catText: catText,
    catListText: catListText,
    catUnset: catListText.includes('未选择分类'),
    catDeleted: catDeleted,
    titleFilled: !!enTitle,
    titleHasCjk: titleHasCjk,
    skuRowCount: skuRows.length,
    skuFilledRows: skuFilled,
    skuCodeCount: skuCodeInps.length,
    skuCodeBad: skuCodeBad,
    sizechartAdded: !!scText && !scText.includes('添加尺码表'),
    // 第二张表：null 表示该类目没有这一栏；false 表示有栏但没填（套装商品必须填，
    // 否则平台打回「套装尺码模板数量不合法」）
    sizechart2Added: scText2 === null ? null : (!!scText2 && !scText2.includes('添加尺码表')),
    sizechartCount: scCount,
    attrImgCount: attrImgs.length,
    attrImgBad: attrImgBad,
    // ⑦b：预览图总数与其中不合规的张数（0 张说明该类目没有这一列，交阶段自己判）
    previewCount: previewCount,
    previewBad: previewBad,
    shippingSet: !!(shipSel && txt(shipSel)) && !!shipRadio,
    descImgCount: descImgs.length,
    descForeignCount: foreign.length,
  });
})()"""


async def live_state(session: BrowserSession) -> dict:
    """读编辑页实况，返回各表单阶段「成果是否还在页面上」。

    只读、不点任何东西。给 service 层的续跑判定用（见 _JS_LIVE_STATE 的注释）。
    """
    st = await session.eval_json(
        _JS_LIVE_STATE.replace("__MINW__", str(images.CLOTH_MIN_W))
                      .replace("__MINH__", str(images.CLOTH_MIN_H))
                      .replace("__PREVMIN__", str(media_preview.PREVIEW_MIN_SIDE))
                      .replace("__DIM_COLS__", variant_dom._JS_DIM_COLS))
    if not st.get("rendered"):
        logger.warning("编辑页 SKU 区未渲染完，实况判定按「全部需重跑」处理")
    # 类目异常单独打一行：这是整单卡死的上游根因，而页面那条 d-message 几秒就自己消失，
    # 不在这里落进日志的话，用户只看到后面 ⑧⑨⑩⑪ 一片「无处可填」，查不到源头。
    if st.get("catDeleted") or st.get("catUnset"):
        logger.warning(
            f"编辑页类目异常：当前分类「{st.get('catText') or '（空）'}」"
            f"{'，平台提示该分类已删除' if st.get('catDeleted') else ''}"
            f"{'，分类行显示「未选择分类」' if st.get('catUnset') else ''}"
            f"。类目未生效时变种属性区不渲染尺码行，须从阶段③ 重选类目。")
    return st
