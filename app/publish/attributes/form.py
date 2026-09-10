"""店小秘发布操作：attributes.form。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
from app.logger import logger
from app.publish import cache, common
from app.publish.attributes import dropdowns as attributes_dropdowns
from app.publish.browser import BrowserSession, J
from typing import Optional


# ---- 阶段④ 产品属性（下拉交互辅助）------------------------------------------
# 这批函数是原 skill 里最难缠的部分，几乎每一行都对应一个踩过的坑。搬运时原样保留
# 那些看似多余的清理/重试，别"简化"：
#
# 坑1「幽灵浮层」：合成事件展开的 ant-select 下拉，Vue 内部状态关了但内联样式残留
#   定位，页面上会挂一堆看不见却仍能响应点击的浮层。若扫 DOM 找选项，同选项列表的
#   字段（上装成分/下装成分/材质/辅料成分都是同一份纤维列表）会全命中，点在隐藏浮层
#   上事件照样生效——结果把别的字段改掉了。故一切选项查找都限定在「目标行附近唯一
#   可见浮层」内（top>-1000 且 width>50，距行最近者），并在每步前后清幽灵。
# 坑2「视口外坐标不可靠」：行在视口外时下拉开在负坐标处，可见性判定误判为未打开。
#   故每次定位前先 scrollIntoView({block:'center'})。
# 坑3「虚拟列表」：选项用 rc-virtual-list 只渲染可视窗口约 10 条，静态扫 DOM 既拿不
#   全也会错配。故读选项要滚动收集，点选项时目标不在窗口内要滚动到它渲染出来再点。
# 坑4「动态增删行」：改里料纹理等字段会触发表单重渲染并联动新增必填行（里衬成分、
#   里料克重）。固定等待的单次回读会读到旧值/空行造成假失败，故回读改轮询等值稳定。
# 坑5「必填标记在内层 span」（2026-08-19 本项目实测修正原脚本的缺陷）：
#   固定字段是 <label class="ant-form-item-required" title="产品分类">，
#   而动态属性行是 <label class="ant-form-item-no-colon" title="">
#                   └ <span class="attr-label required">适用人群</span>
#   —— 必填 class 在【内层 span】上，且 label 的 title 是【空的】。原脚本只看
#   labelEl.className 和行内 .ant-form-item-required，于是 33 个属性行全被判成非必填。
#   这会直接毁掉 check_attrs 的核心策略（「只填必填项、非必填一律留空」）：必填判断
#   全 false 时，要么该填的必填项一个都不填，要么反过来把几十个非必填项全填上，
#   正是原脚本想避免的「填得多错得多」。故 label 与 required 都必须先取内层 span。

_JS_LIST_ATTR_ROWS = r"""(() => {
  const sec = document.getElementById('productBasicInfo');
  if (!sec) return JSON.stringify({found: false});
  const skip = ['店铺账号', '经营站点', '产品分类'];
  const out = [];
  sec.querySelectorAll('.ant-form-item').forEach(it => {
    const labelEl = it.querySelector('.ant-form-item-label label');
    if (!labelEl) return;
    // 动态属性行的 label 结构与固定字段不同（2026-08-19 实测，见下方注释）：
    // 名字在内层 span.attr-label 里、label 的 title 是空的，故先取内层 span。
    const attrSpan = labelEl.querySelector('.attr-label');
    const label = attrSpan
      ? (attrSpan.textContent || '').trim()
      : (labelEl.getAttribute('title') || labelEl.textContent || '').trim();
    if (!label || skip.includes(label)) return;
    // 【kind 判据是控件序里的第一个控件，不是「有没有 .ant-select」】
    // 2026-08-25 真站取证（rowid 173539495454695591）纠正了原先的错误前提——
    // 原注释断言「纯数值属性没有 .ant-select」，实际「里料克重（g/m²)」是
    // 【数值输入框 + 单位下拉】的复合行：
    //     里料克重（g/m²)  →  input[type=text].ant-input, select[cur=g/㎡]
    //     里衬成分         →  select[ph=请选择], input（百分比）
    //     成分             →  select[cur=棉], input[80], select[聚酯纤维], input[20]
    // 按「有 select 就算下拉行」会把克重判成 select，后果是三连错：current 读成
    // 单位文本「g/㎡」（于是这行看起来【已填】，LLM 不会填、末尾的必填复扫也不报），
    // options 读成单位列表 ['g/㎡'] 并落进缓存，写入还会走下拉分支去点单位下拉。
    // 数值框就一直空着，保存卡「请输入产品属性」。
    // 反过来「按 input 存在就算数值行」也不行——成分行同样有百分比 input。
    // 区分点在【顺序】：数值行的输入框在单位下拉之前，成分行的下拉在百分比之前。
    // （旁证：单位下拉内部的搜索框带 readonly，是纯展示的固定单位，不必也不该改。）
    const ctrl = it.querySelector('.ant-form-item-control') || it;
    // 控件序：select 与「可填 input」按 DOM 顺序排一列。select 内部的搜索框
    // （.ant-select-selection-search-input）会一并匹配到，按 class 剔除。
    const seq = Array.from(ctrl.querySelectorAll('.ant-select, input')).filter(el => {
      if (el.tagName !== 'INPUT') return true;
      if (el.type === 'hidden' || el.type === 'radio' || el.type === 'checkbox') return false;
      return !(el.className || '').includes('ant-select');
    });
    if (!seq.length) return;                      // 既无下拉也无可填输入，不是属性行
    const firstIsInput = seq[0].tagName === 'INPUT';
    const kind = firstIsInput ? 'number' : 'select';
    const fillable = seq.filter(el => el.tagName === 'INPUT');
    // 【成分类复合行的结构判据】下拉打头、后面还跟着可填 input = 「纤维 + 百分比」
    // 复合行（里衬成分、上装成分…）。纯下拉行（里料纹理、季节）没有这个 input。
    // 为什么必须给出这个信号：下游原先靠「LLM 有没有给 num」来认成分行，模型漏给
    // num 时那行既跳过了「合计=100」闸、又不填百分比框，表现为纤维选上了、百分比
    // 空着，平台报「请完善里衬成分信息」（2026-08-25 用户截图实测）。行是不是成分
    // 行由 DOM 结构决定，不该由模型的输出完整性决定。
    const hasPercent = !firstIsInput
      && seq.slice(1).some(el => el.tagName === 'INPUT');
    // 【数值行必须是动态属性行才收】固定字段（产品标题/来源URL/产品货号/
    // 站外产品链接）同样是输入框打头，误收进来会让 LLM 去改标题，与阶段⑤ 打架。
    // 判据用内层 span.attr-label：只有动态属性行有它（2026-08-19 实测，见上方注释）。
    if (firstIsInput && !attrSpan) return;
    it.setAttribute('data-attr-label', label);
    const curEl = it.querySelector('.ant-select-selection-item');
    const phEl = it.querySelector('.ant-select-selection-placeholder');
    const numInputs = Array.from(it.querySelectorAll('input'))
      .filter(i => i.type !== 'hidden' && !i.className.includes('ant-select'))
      .map(i => i.value).filter(v => v !== '');
    // 数值行的填写线索：单位与 placeholder 决定该填什么量级。
    // 【单位主要来自那个只读的单位下拉】原先只找 .ant-input-suffix / -group-addon /
    // .unit，而克重行的单位是 select 里的 selection-item（文本 'g/㎡'），三个选择器
    // 一个都命中不了，unit 恒为空、模型少了唯一的量级线索。故数值行优先取【输入框
    // 之后】的那个 select 的当前文本，再退回原来的三个后缀选择器。
    const unitSel = kind === 'number'
      ? seq.slice(1).find(el => el.tagName !== 'INPUT') : null;
    const unitFromSel = unitSel
      ? ((unitSel.querySelector('.ant-select-selection-item') || {}).textContent || '').trim()
      : '';
    const unitEl = it.querySelector('.ant-input-suffix, .ant-input-group-addon, .unit');
    const numHint = kind !== 'number' ? null : {
      placeholder: (fillable[0] && fillable[0].placeholder) || '',
      unit: unitFromSel || (unitEl ? (unitEl.textContent || '').trim() : ''),
      value: (fillable[0] && fillable[0].value) || '',
    };
    // 必填判定三处都要看：动态属性行是 span.attr-label.required（主力），
    // 固定字段是 label.ant-form-item-required，末项是 antd 的通用兜底。
    const required = (attrSpan && attrSpan.classList.contains('required'))
      || labelEl.classList.contains('ant-form-item-required')
      || !!it.querySelector('.ant-form-item-required');
    out.push({label: label,
      // 【数值行的 current 只能读输入框，绝不能读 selection-item】克重行那个
      // selection-item 是【单位】'g/㎡'，读它等于把空行报成已填（原缺陷的核心）。
      // 空值按 "(请输入)" 表达「未填」，与下拉行的 "(请选择)" 语义一致，
      // 下游判空（cur.startswith('(')）不必分叉。
      current: kind === 'number'
        ? ((numHint && numHint.value) ? numHint.value : '(请输入)')
        : (curEl ? (curEl.textContent || '').trim()
                 : (phEl ? '(' + (phEl.textContent||'').trim() + ')' : null)),
      kind: kind,
      hasPercent: hasPercent,
      numHint: numHint,
      numValues: numInputs,
      required: required,
      visible: it.offsetHeight > 0});
  });
  return JSON.stringify({found: true, attrs: out});
})()"""


async def dump_attrs(session: BrowserSession, skip_options: bool = False,
                     required_only: bool = True,
                     cat_path=None, use_cache: bool = True, site: str = "") -> dict:
    """阶段④(只读)：导出产品属性当前值 + 每项下拉的真实选项。

    不导航——必须紧跟 auto_cat 在同一页执行：类目决定了有哪些属性行，换页就得重选。

    required_only=True（默认）只读【必填项】的选项：非必填项一律不填（见 _ATTR_PROMPT
    规则4「填得多错得多」），拿到它们的选项也用不上，而每项都要点开下拉+滚动虚拟列表
    收集，读全部 33 项要 3-5 分钟、只读必填项能省掉近一半。非必填项仍返回当前值，
    只是 options 为空并标 optionsEmptyReason=optional-skipped。
    要看全部选项（排查/探查场景）传 required_only=False。

    【2026-08-24 撤掉「源商品给了值的非必填行也读」这个例外】原先按源属性键与表单行名
    做双向子串包含匹配（_labels_matching_src），给匹配上的非必填行也读 options 交给 LLM
    填。撤掉的原因是那个匹配偏松，实测把语义无关的行也开了口子：源「货源类型」命中表单
    「类型」（那是夹克款式）、源「主面料成分/面料工艺」命中表单「面料」（那是弹力档位）；
    且源键本身存在解析粘连的长串（「是否跨境出口专供货源…主要下游平台ebay,亚马逊」），
    短行名被长键包含就命中，误开口子的面还会继续扩大。现在的规则回到单一口径：
    非必填一律留空，源商品写了也不填。

    skip_options=True 时一个下拉都不点，只读当前值——最快，但没有 options 就不能喂给
    LLM 做修改建议，只能看现状。

    cat_path（类目路径列表）+ use_cache 命中时，options 从缓存注入、跳过该行的「点开
    下拉 + 滚虚拟列表」，那正是本阶段 92.9s 的主体。cat_path 给不出来（续跑、
    publish_inspect 单跑）时为 None → 当未命中 → 全量现场读，行为与加缓存前完全一致。
    注意【缓存只补 options，永不造行】：行集、current、required、visible 一律从活页面
    读——required 是 _validate_attr_changes 第 1 道闸的依据，用缓存里的旧值等于拿旧
    策略判新表单。

    隐藏行（依赖字段未触发、行仍 display:none）跳过并标 optionsEmptyReason=row-hidden，
    不强行点开——那种行点不开是正常的，不是故障。
    """
    exp = await attributes_dropdowns._expand_attr_section(session)
    if exp.get("expanded"):
        # 等折叠区展开动画结束（原固定 sleep(1.2)，上限不变）：判据是属性行数稳定
        # （展开过程中 Vue 逐步渲染，行数从 0 增长到最终值）。
        # 选择器用 '#productBasicInfo .ant-form-item' —— 与本文件其它处读属性行的
        # 口径完全一致，别另造类名。
        prev_n = -1
        for _ in range(12):  # 1.2s / 0.1s
            await asyncio.sleep(0.1)
            r = await session.eval_json(
                "(() => JSON.stringify({n: document.querySelectorAll("
                "'#productBasicInfo .ant-form-item').length}))()")
            n = r.get("n", 0)
            if n > 0 and n == prev_n:
                break  # 连续两次读到相同非零行数 = 渲染完成
            prev_n = n
    # 属性行是懒渲染：open_edit 只等到 skuDataInfo 出现，跟在后面立刻读经常
    # 读到 0 行（2026-08-21 实测：续跑补开编辑页后 2.5s 就判空失败）。
    # 故这里轮询等到行出来再判，超时返回的最后一次结果交给下方空行分支。
    # 【展开与轮询都不能因命中缓存而跳过】命中省掉的只是逐行读选项，行本身仍要渲染出来。
    rows = await session.wait_for(
        _JS_LIST_ATTR_ROWS,
        lambda d: d.get("found") and bool(d.get("attrs")),
        timeout=20, interval=1.5)
    if not rows.get("found"):
        raise RuntimeError("productBasicInfo 未找到（当前页不是编辑页？）")
    attrs = [a for a in rows.get("attrs", []) if a["label"] != "产品属性"]  # 分组标题行
    if skip_options:
        # 这条路径语义是「一个下拉都不点、只看现状」，故【不注入缓存】：注入了会返回
        # optionsRead=False 却带着 options，把它的返回契约打乱。
        return {"status": "ok", "count": len(attrs), "attrs": attrs,
                "optionsRead": False}
    n_required = sum(1 for a in attrs if a.get("required"))
    # 同类目的 options 清单不随商品变，能从缓存拿就不必逐行点开下拉
    cached = (cache.load_attr_options(cat_path[-1], cat_path, site)
              if use_cache and cat_path else {})
    logger.info(f"属性行 {len(attrs)} 条（必填 {n_required}），"
                + (f"缓存 {len(cached)} 行可用，" if cached else "")
                + "逐个读必填项下拉选项…")
    active_read = 0
    skipped_optional = 0
    cache_read = 0
    for a in attrs:
        a["options"] = []
        if a.get("visible") is False:
            a["optionsEmptyReason"] = "row-hidden"
            continue
        if required_only and not a.get("required"):
            a["optionsEmptyReason"] = "optional-skipped"
            skipped_optional += 1
            continue
        # 【数值行不读选项】它行内那个 select 是【只读的单位】（克重行是 'g/㎡'），
        # 点开读到的是单位清单、不是可选值。读了有三重害处：白花一次开合下拉、
        # 一份 ['g/㎡'] 落进缓存（缓存文件里的「面料克重1（g/m²) → ['g/㎡']」就是
        # 这么来的），以及让 _validate_attr_changes 里「数值行 options 恒为空」这个
        # 前提失效——那道量级闸靠 kind 分流，前提失效不至于出错，但缓存脏了会一直脏。
        if a.get("kind") == "number":
            a["optionsEmptyReason"] = "number-row"
            continue
        # 【这个 guard 必须在上面两个 skip 之后，顺序不能调】_validate_attr_changes 的
        # 第 2 个分支靠「非必填 + 未填 + options 空」判定「按策略留空」；提前注入会给
        # 那些行填上 options，把这条既定策略打穿（填得多错得多）。
        hit = cached.get(a["label"])
        if hit:
            a["options"] = hit
            a["optionsFrom"] = "cache"   # 写入失败后要不要重读该行，就看这个标记
            cache_read += 1
            continue
        opts, meta = await attributes_dropdowns._read_active_options(session, a["label"], with_meta=True)
        logger.info(f"读选项：{a['label']} -> {len(opts)} 个"
                    + ("" if meta["complete"] else "（未滚到底，不进缓存）"))
        if opts:
            a["options"] = opts
            a["optionsFrom"] = "live"
            # 只有确认读全的清单才允许落盘：截断的清单进了缓存，之后每个同类目商品
            # 都会拿一份缺项的 options 去做校验与纤维匹配，且没人会发现
            a["optionsComplete"] = meta["complete"]
            active_read += 1
        else:
            a["optionsEmptyReason"] = "open-failed"
        # 行间隔：0.3s → 0.12s。这里等的是「上一行的下拉收起、不干扰下一行定位」，
        # 而 _read_active_options 收尾已经关下拉 + 清幽灵；真没清干净时下一行的
        # _open_attr_dropdown 本就有幂等打开与二次重试兜着。逐行读只在缓存未命中时
        # 发生（实测每类目 16-18 必填行），这一项省约 3s。
        await asyncio.sleep(0.12)
    parked = await attributes_dropdowns._park_ghost_dropdowns(session)
    # 现场读到的行写回缓存（按 label 并集合并，见 cache.save_attr_options）
    if use_cache and cat_path and active_read:
        cache.save_attr_options(cat_path[-1], cat_path, attrs, site)
    return {"status": "ok", "count": len(attrs), "attrs": attrs,
            "optionsRead": True, "requiredOnly": required_only,
            "activeRead": active_read, "cacheRead": cache_read,
            "skippedOptional": skipped_optional,
            "parkedGhosts": parked.get("parked", 0)}


async def _readback_attr(session: BrowserSession, label: str, row_no: int = 1) -> Optional[dict]:
    """回读某属性行第 row_no 个下拉的当前值（1 起）。"""
    if row_no <= 1:
        rows = await session.eval_json(_JS_LIST_ATTR_ROWS)
        return next((a for a in rows.get("attrs", []) if a["label"] == label), None)
    js = r"""(() => {
      const it = Array.from(document.querySelectorAll('#productBasicInfo .ant-form-item'))
        .find(el => {
          const l = el.querySelector('.ant-form-item-label label');
          if (!l) return false;
          const sp = l.querySelector('.attr-label');
          const name = sp ? (sp.textContent||'').trim()
                          : (l.getAttribute('title')||l.textContent||'').trim();
          return name === __LABEL__; });
      if (!it) return JSON.stringify(null);
      const sel = Array.from(it.querySelectorAll('.ant-select'))[__IDX__];
      const c = sel ? sel.querySelector('.ant-select-selection-item') : null;
      const inp = Array.from(it.querySelectorAll('input'))
        .filter(i => i.type !== 'hidden' && !i.className.includes('ant-select'))[__IDX__];
      return JSON.stringify({label: __LABEL__,
        current: c ? (c.textContent || '').trim() : null,
        numValues: inp && inp.value !== '' ? [inp.value] : []});
    })()""".replace("__LABEL__", J(label)).replace("__IDX__", str(row_no - 1))
    return await session.eval_json(js)


async def _trim_comp_rows(session: BrowserSession, label: str, keep: int) -> dict:
    r"""成分类字段行数多于 keep 时，点末行的 .icon_remove 裁到只剩 keep 行。

    【为什么必须裁】平台按 attrValueId 逐行判重（2026-08-30 从编辑页 bundle
    Layout-*.js 取证：selectPercent 属性遍历各行，`if (n.includes(p)) return
    C(\`${t._label}不能重复选择\`)`，且这道闸排在「百分比之和=100」之前）。
    而认领带来的初始行数由源商品决定：本例「成分」页面初始就是 2 行
    （聚酯纤维 90% + 氨纶 10%），我们按源含量重建出的却是 2 行
    （聚酯纤维 60% + 棉 40%）——行数刚好相等时没事，一旦初始行比重建结果多，
    多出来的旧行就【原样留在表单里】：_ensure_comp_rows 只加不减，写入又只覆盖
    前 N 行。2026-08-30 实测复现（rowid 173539495458369319）：页面 3 行时按
    2 行写入，回读得到 ['聚酯纤维(涤纶）', '棉', '棉'] —— 第 3 行的旧「棉」与
    新写的第 2 行撞同一根纤维，保存即报「成分不能重复选择」，整单卡在阶段⑭。

    只点【末行】的 icon_remove：第 1 行没有这个图标（2026-08-30 实测：行 1 只有
    icon_add，行 2 起才同时有 icon_add + icon_remove），且实测点末行删的就是末行、
    不会错删前面已填好的行。keep < 1 一律按 1 处理——首行删不掉，硬要删只会空转。
    """
    keep = max(1, int(keep))
    js = r"""(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const it = Array.from(document.querySelectorAll('#productBasicInfo .ant-form-item'))
        .find(el => {
          const l = el.querySelector('.ant-form-item-label label');
          if (!l) return false;
          const sp = l.querySelector('.attr-label');
          const name = sp ? (sp.textContent||'').trim()
                          : (l.getAttribute('title')||l.textContent||'').trim();
          return name === __LABEL__; });
      if (!it) return JSON.stringify({err: 'row-not-found'});
      const rows = () => it.querySelectorAll('.ant-select').length;
      const before = rows();
      let guard = 0;
      while (rows() > __KEEP__ && guard++ < 10) {
        const rms = Array.from(it.querySelectorAll('.icon_remove'));
        if (!rms.length) break;              // 只剩首行（无删除图标），到此为止
        rms[rms.length - 1].click();
        await sleep(1200);
      }
      return JSON.stringify({before: before, after: rows(), trimmed: before - rows()});
    })()""".replace("__LABEL__", J(label)).replace("__KEEP__", str(keep))
    return await session.eval_json(js)


async def _ensure_comp_rows(session: BrowserSession, label: str, row_no: int) -> dict:
    """成分类字段行数不够时点 .icon_add 加行，直到有 row_no 个下拉。"""
    js = r"""(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const it = Array.from(document.querySelectorAll('#productBasicInfo .ant-form-item'))
        .find(el => {
          const l = el.querySelector('.ant-form-item-label label');
          if (!l) return false;
          const sp = l.querySelector('.attr-label');
          const name = sp ? (sp.textContent||'').trim()
                          : (l.getAttribute('title')||l.textContent||'').trim();
          return name === __LABEL__; });
      if (!it) return JSON.stringify({err: 'row-not-found'});
      let n = it.querySelectorAll('.ant-select').length;
      let guard = 0;
      while (n < __ROWNO__ && guard++ < 10) {
        const add = it.querySelector('.icon_add');
        if (!add) return JSON.stringify({err: 'no-add-btn', rows: n});
        add.click();
        await sleep(1200);
        n = it.querySelectorAll('.ant-select').length;
      }
      return JSON.stringify({rows: n});
    })()""".replace("__LABEL__", J(label)).replace("__ROWNO__", str(row_no))
    return await session.eval_json(js)


# 往【动态属性行】的数值输入框里直填（里料克重 g/m² 这类没有下拉的必填项）。
#
# 行定位先用 data-attr-label（_JS_LIST_ATTR_ROWS 枚举时打上的），【但必须带 label
# 文本兜底】：同一批补填里先写的下拉行会触发联动重渲染，排在后面的行若节点被 Vue
# 重建，那个手工打的属性就随旧节点一起没了，只认属性等于报 attr-row-not-found、
# 该行永远填不上。下拉侧的 5 处定位一直是「属性 + 文本」双路（见
# _visible_dropdown_near 等），数值侧原先漏了这层，多轮联动补填会放大它。
# 找到后顺手补打标记，后续回读轮询才不必每轮都走兜底分支。
#
# 占位符走 J() 转义（自带引号）而非裸文本：属性名里有「（g/m²)」这类字符，裸拼进
# JS 字符串字面量一旦出现引号或反斜杠就破语法。
#
# 【属性名绝不拼进 CSS 选择器】取行只枚举 [data-attr-label] 再按 getAttribute 比对，
# 不写成 [data-attr-label=<名字>]：2026-08-25 实测「里料克重（g/m²)」拼出的选择器带
# 全角括号，querySelector 直接抛 SyntaxError，阶段④整个异常、商品未落库。给属性值补
# 引号只是把红线推远——名字里真出现 " 照样抛，出现 \ 则静默 miss 白走一趟兜底；而属性
# 名是平台下发的，我们控制不了。故下拉侧那 5 处定位也一并收敛成同一套比对方式。
#
# 【必须派发 input 事件】Vue 只认事件，直接赋 value 保存时会丢（与 _js_fill_by_label
# 同一个坑）。回读带轮询：填克重可能触发联动重渲染，读太早拿到旧值是假失败。
_JS_SET_ATTR_NUM = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const LB = __LABELQ__;
  // 取行：先认标记，丢了就按 label 文本重找（动态属性行的名字在内层 .attr-label）
  const pick = () => {
    const byMark = Array.from(
      document.querySelectorAll('.ant-form-item[data-attr-label]'))
      .find(el => el.getAttribute('data-attr-label') === LB);
    if (byMark) return byMark;
    const hit = Array.from(
      document.querySelectorAll('#productBasicInfo .ant-form-item')).find(el => {
        const l = el.querySelector('.ant-form-item-label label');
        if (!l) return false;
        const sp = l.querySelector('.attr-label');
        const name = sp ? (sp.textContent || '').trim()
                        : (l.getAttribute('title') || l.textContent || '').trim();
        return name === LB; });
    if (hit) hit.setAttribute('data-attr-label', LB);
    return hit || null;
  };
  const it = pick();
  if (!it) return JSON.stringify({status: 'error', reason: 'attr-row-not-found'});
  const inp = Array.from(it.querySelectorAll('input')).find(i =>
    i.type !== 'hidden' && i.type !== 'radio' && i.type !== 'checkbox'
    && !(i.className || '').includes('ant-select'));
  if (!inp) return JSON.stringify({status: 'error', reason: 'input-not-found'});
  inp.scrollIntoView({block: 'center'});
  await sleep(300);
  const before = inp.value;
  const desc = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value');
  desc.set.call(inp, __VALUEQ__);
  inp.dispatchEvent(new Event('input', {bubbles: true}));
  inp.dispatchEvent(new Event('change', {bubbles: true}));
  inp.dispatchEvent(new Event('blur', {bubbles: true}));   // 有些字段 blur 才校验
  // 回读轮询等稳定（联动重渲染会换掉 input 实例，故每轮重新查）
  for (let i = 0; i < 8; i++) {
    await sleep(400);
    const now = pick();
    const cur = now && Array.from(now.querySelectorAll('input')).find(x =>
      x.type !== 'hidden' && x.type !== 'radio' && x.type !== 'checkbox'
      && !(x.className || '').includes('ant-select'));
    if (cur && String(cur.value).trim() === __VALUEQ__) {
      return JSON.stringify({status: 'ok', before: before, readback: cur.value});
    }
  }
  const last = pick();
  const lastInp = last && last.querySelector('input');
  return JSON.stringify({status: 'error', reason: 'readback-mismatch',
    before: before, readback: lastInp ? lastInp.value : null});
})()"""


def _readback_current(r: dict):
    """从 set_attr 的返回里取回读值，形状不认识就返回 None。

    【为什么要这层】readback 只是写给人看的诊断字段（记进 applied 供 UI 展示），
    却因为「下拉行给字典、数值行给字符串」的形状差异让阶段④整个抛异常、商品未落库
    （2026-08-25）。形状已在 set_attr 里统一，这里再兜一道：诊断字段绝不该有能力
    中断主流程——判成功失败看的是 status，不是这个值。
    """
    back = r.get("readback")
    if isinstance(back, dict):
        return back.get("current")
    return back if isinstance(back, str) else None


async def set_attr(session: BrowserSession, label: str, value: str,
                   num: Optional[int] = None, row_no: int = 1,
                   kind: str = "select") -> dict:
    """把指定属性改选为指定选项文本，回读校验。不导航（须在编辑页且类目已选）。

    num：成分类复合字段的百分比数值。row_no（1 起）：成分字段的第 N 个成分行，
    行不够会自动点 .icon_add 加行。
    kind："select"（默认，开下拉点选项）或 "number"（纯数值输入框，如里料克重
    g/m²，直接写 input）。数值行没有下拉可开，走同一套流程会必然失败。

    可靠性设计（原脚本 2026-08-17 排查结论，原样保留）：
    改里料纹理等字段会触发表单动态增删行重渲染，固定等待的单次回读会读到旧值/空行
    造成【假失败】；点击也可能落在重渲染前的游离 DOM 上造成【真失败】。因此
    回读改成轮询等值稳定（最多约 4s），且回读不符时「打开→点选→回读」整流程自愈
    重试一次。
    """
    # 【数值输入行走独立分支】里料克重这类没有下拉可开，下面「开下拉→点选项」
    # 整套流程都不适用。判据由调用方按 dump_attrs 枚举出的 kind 传入。
    if kind == "number":
        r = await session.eval_json(
            _JS_SET_ATTR_NUM.replace("__LABELQ__", J(label))
                            .replace("__VALUEQ__", J(str(value))))
        # 【readback 必须与下拉分支同形状】上面那段 JS 回读的是 input.value（字符串），
        # 而下拉分支给的是 _readback_attr 的字典，调用方（_apply_attr_changes、
        # _refresh_row_and_retry）一律按 r["readback"]["current"] 取值。原样透出字符串
        # 会把阶段④整个炸掉：2026-08-25 联动补填写「里料克重（g/m²)」实测
        # AttributeError: 'str' object has no attribute 'get'，商品未落库。
        # 包成 current 不是硬凑：_JS_LIST_ATTR_ROWS 对 kind=number 的 current 读的
        # 就是输入框值，两边语义本来一致。error 分支同样要归一——那条路径也带 readback。
        back = r.get("readback")
        rb = {"label": label, "current": None if back is None else str(back)}
        if r.get("status") == "ok":
            return {"status": "ok", "label": label, "value": value,
                    "kind": "number", "readback": rb}
        return {"status": "error", "label": label, "value": value,
                "kind": "number", **r, "readback": rb}

    if row_no > 1:
        add = await _ensure_comp_rows(session, label, row_no)
        if add.get("err"):
            return {"status": "error", "stage": "add-row", **add}
        await asyncio.sleep(0.5)

    clicked: dict = {}
    cur: Optional[dict] = None
    for attempt in (1, 2):
        await attributes_dropdowns._open_attr_dropdown(session, label, sel_idx=row_no - 1)
        # 等浮层里的选项真渲染出来（替代固定 sleep(0.6)，上限同为 0.6s）：
        # _open_attr_dropdown 的收敛条件只是「浮层可见」，而浮层可见 ≠ 里面的
        # rc-virtual-list 已挂上 item。见 _poll_until 的说明。
        await common._poll_until(
            lambda: session.eval_json(attributes_dropdowns._js_dropdown_options_rendered(label)),
            lambda d: (d or {}).get("n", 0) > 0, timeout=0.6)
        clicked = await attributes_dropdowns._click_dropdown_option(session, label, value)
        if not clicked.get("clicked"):
            # 目标选项可能在虚拟列表可视窗口之外，滚动去找
            clicked = await attributes_dropdowns._scroll_click_option(session, label, value)
        if not clicked.get("clicked"):
            await attributes_dropdowns._park_ghost_dropdowns(session)  # 点开未点中也要清，别留浮层
            continue
        # 回读轮询，等重渲染稳定到目标值。
        # 【先读一次再等】原先点中后先无条件 sleep(0.8)、且每轮都先 sleep(0.5) 再读，
        # 等于每项白付 0.8s+；点击到 Vue 重渲染往往已完成。总上限不变（约 4s），
        # 判据（current == value）一字不改。
        cur = await common._poll_until(
            lambda: _readback_attr(session, label, row_no),
            lambda d: bool(d) and d.get("current") == value,
            timeout=4.0, interval=0.5)
        if cur and cur.get("current") == value:
            break
        logger.warning(f"{label} 第{attempt}次设为「{value}」后回读不符，重试")

    if not clicked.get("clicked"):
        return {"status": "error", "label": label, "value": value, **clicked}

    result: dict = {"status": "ok", "label": label, "value": value}
    if num is not None:
        # 【不再 sleep(0.5)】上面 select 分支的回读轮询已确认该行稳定到目标值，
        # 而填数值本身是同步的 setter + dispatchEvent，不需要预热等待；
        # 填完之后的重渲染由下方最终回读轮询兜住。
        js = r"""(() => {
          const it = Array.from(document.querySelectorAll('#productBasicInfo .ant-form-item'))
            .find(el => {
              const l = el.querySelector('.ant-form-item-label label');
              if (!l) return false;
              const sp = l.querySelector('.attr-label');
              const name = sp ? (sp.textContent||'').trim()
                              : (l.getAttribute('title')||l.textContent||'').trim();
              return name === __LABEL__; });
          if (!it) return JSON.stringify({filled: false, reason: 'row-not-found'});
          const inp = Array.from(it.querySelectorAll('input'))
            .filter(i => i.type !== 'hidden' && !i.className.includes('ant-select'))[__IDX__];
          if (!inp) return JSON.stringify({filled: false, reason: 'no-num-input'});
          const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
          setter.call(inp, __NUM__);
          inp.dispatchEvent(new Event('input', {bubbles: true}));
          inp.dispatchEvent(new Event('change', {bubbles: true}));
          inp.dispatchEvent(new Event('blur', {bubbles: true}));
          return JSON.stringify({filled: true, readback: inp.value});
        })()""".replace("__LABEL__", J(label)).replace("__NUM__", J(str(num))) \
            .replace("__IDX__", str(row_no - 1))
        result["num"] = await session.eval_json(js)

    # 最终回读（填数值也可能触发重渲染，再轮询一次）。
    # 【先读一次再等】上面 select 分支的轮询刚确认过 current == value，绝大多数情况
    # 这里第一次读就命中；原先每轮先读后 sleep 也要在未命中时才付出等待，但 num 分支
    # 前面那个无条件 sleep(0.5) 是白付的（填数值是同步 dispatchEvent）。
    # 上限不变（约 3s），判据不改。
    cur = await common._poll_until(
        lambda: _readback_attr(session, label, row_no),
        lambda d: bool(d) and d.get("current") == value,
        timeout=3.0, interval=0.5)
    result["readback"] = cur
    result["status"] = "ok" if cur and cur.get("current") == value else "error"
    # 幽灵浮层只在真有残留时才清（全文档扫 + 逐个量 rect，不必每项无条件跑）
    if (await attributes_dropdowns._visible_dropdown_near(session, label)).get("found"):
        parked = await attributes_dropdowns._park_ghost_dropdowns(session)
        result["parkedGhosts"] = parked.get("parked", 0)
    else:
        result["parkedGhosts"] = 0
    return result


async def _set_select_by_label(session: BrowserSession, label: str, value: str,
                                row_no: int = 1) -> dict:
    """填写单个下拉属性（支持成分行扩行、虚拟列表滚动、回读轮询、自愈重试）。

    row_no=1 时操作表单第一行（默认），row_no>1 时先点 .icon_add 加行到够数再操作对应行。
    回读改为轮询等值稳定（最多 ~4s），回读不符时「打开→点选→回读」整流程自愈重试一次。
    """
    def _js_readback():
        if row_no <= 1:
            # 普通属性行：用现成的列表接口读
            return """(() => {
              const rows = JSON.parse((""" + _JS_LIST_ATTR_ROWS + """)());
              const hit = rows.attrs.find(a => a.label === __LABEL__);
              return JSON.stringify(hit || null);
            })()""".replace("__LABEL__", J(label))
        # 成分第 N 行：用选择器定位该 form-item 下第 N-1 个下拉
        return r"""(() => {
          const it = Array.from(document.querySelectorAll('#productBasicInfo .ant-form-item'))
            .find(el => { const l = el.querySelector('.ant-form-item-label label');
              return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
          if (!it) return JSON.stringify(null);
          const sel = Array.from(it.querySelectorAll('.ant-select'))[__IDX__];
          const c = sel ? sel.querySelector('.ant-select-selection-item') : null;
          const inp = Array.from(it.querySelectorAll('input'))
            .filter(i => i.type !== 'hidden' && !i.className.includes('ant-select'))[__IDX__];
          return JSON.stringify({label: __LABEL__,
            current: c ? (c.textContent || '').trim() : null,
            numValues: inp && inp.value !== '' ? [inp.value] : []});
        })()""".replace("__LABEL__", J(label)).replace("__IDX__", str(row_no - 1))

    # 成分第 N 行：行不够先点 .icon_add 加行
    if row_no > 1:
        js_add = r"""(async () => {
          const sleep = ms => new Promise(r => setTimeout(r, ms));
          const it = Array.from(document.querySelectorAll('#productBasicInfo .ant-form-item'))
            .find(el => { const l = el.querySelector('.ant-form-item-label label');
              return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
          if (!it) return JSON.stringify({err: 'row-not-found'});
          let n = it.querySelectorAll('.ant-select').length;
          while (n < __ROWNO__) {
            const add = it.querySelector('.icon_add');
            if (!add) return JSON.stringify({err: 'no-add-btn', rows: n});
            add.click();
            await sleep(1200);
            n = it.querySelectorAll('.ant-select').length;
          }
          return JSON.stringify({rows: n});
        })()""".replace("__LABEL__", J(label)).replace("__ROWNO__", str(row_no))
        add = await session.eval_json(js_add)
        if add.get("err"):
            return {"status": "error", "stage": "add-row", **add}
        await asyncio.sleep(0.5)

    cur = None
    for attempt in (1, 2):
        # 打开下拉（内部已条件等待到浮层可见）
        await attributes_dropdowns._open_attr_dropdown(session, label, sel_idx=row_no - 1)
        # 再等浮层里的选项真渲染出来（替代原 sleep(0.6)，上限同为 0.6s）
        await common._poll_until(
            lambda: session.eval_json(attributes_dropdowns._js_dropdown_options_rendered(label)),
            lambda d: (d or {}).get("n", 0) > 0, timeout=0.6)
        # 点选项
        c = await attributes_dropdowns._click_dropdown_option(session, label, value)
        if not c.get("clicked"):
            # 目标选项可能在虚拟列表可视窗口之外：滚动该行的下拉找到后再点
            c = await attributes_dropdowns._scroll_click_option(session, label, value)
        if not c.get("clicked"):
            await attributes_dropdowns._park_ghost_dropdowns(session)  # 点开未点中也清一次，避免残留浮层
            continue
        # 回读轮询，等重渲染稳定到目标值。
        # 【先读一次再等】原先每轮都先读后 sleep，但首轮之前还有个 sleep(0.8)，
        # 等于每项无条件多花 0.8s；点击到 Vue 重渲染常常已完成。
        # 总时长上限不变（8 × 0.5 = 4s），判据（current == value）一字不改。
        cur = await common._poll_until(
            lambda: session.eval_json(_js_readback()),
            lambda d: bool(d) and d.get("current") == value,
            timeout=4.0, interval=0.5)
        if cur and cur.get("current") == value:
            break  # 成功，退出重试循环
    # 【幽灵浮层只在真有残留时才清】_park_ghost_dropdowns 要全文档扫
    # .ant-select-dropdown 并逐个量 rect，每项都无条件跑一次纯属浪费；
    # 先探一次「这行附近还有没有可见浮层」，有才清。判据与坑1 那套一致。
    if (await attributes_dropdowns._visible_dropdown_near(session, label)).get("found"):
        await attributes_dropdowns._park_ghost_dropdowns(session)
    return {"status": "ok" if (cur and cur.get("current") == value) else "error",
            "label": label, "value": value, "rowNo": row_no, "readback": cur}
