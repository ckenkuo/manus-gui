"""店小秘发布操作：media.video。模块导航见 docs/publish-pipeline-refactor.md。"""

import os
from app.logger import logger
from app.publish.browser import BrowserSession, J
from app.publish.upload import upload_video
from typing import Optional


# ==================== 阶段⑥b 产品视频（比例合规化后回填）====================
# 【为什么需要这个阶段】视频不是我们传的，是阶段② 认领 1688 商品时平台连带搬来的
# （edit.json 响应里的 videoUrl，指向淘宝 CDN）。1688 商品视频绝大多数是 9:16 竖屏，
# 而 Temu 只收 1:1 / 3:4 / 16:9，于是发布时被打回：
#     上传视频接口报错:get video result response error :
#     Video ratio should be 1:1 or 3:4 or 16:9, recommended ratio 1:1 or 3:4
# 这个报错出现在阶段⑮ 发布之后——前 14 个阶段全绿、save 也落库了，最后一步才被弹回，
# 且回执不说是哪个视频。故必须在发布【之前】把关。几何处理见 app/publish/video.py。
#
# 【走「网络上传」而不是「本地上传」——2026-08-26 前端 chunk 溯源 + 真站探查】
# 「重新上传」下拉只有两项：本地上传、网络上传（没有素材图那样的「空间图片」弹窗）。
#   - 本地上传：唤起【原生文件选择框】。CDP 下要么 DOM.setFileInputFiles（店小秘的
#     上传控件是自绘的、拿不到真 input），要么模拟键盘敲路径（时序脆）——与图片侧
#     当初避开文件框的理由完全相同。
#   - 网络上传：弹一个「视频地址」输入框，填 URL 点确定即可，纯 DOM 操作。
# 故这里先把合规化后的视频直传图床（upload.upload_video，走 smtmedia bucket），
# 拿到 CDN 地址后再用「网络上传」把地址填回去。两步都不碰原生文件框。
#
# 【这个下拉是 hover 触发，且 Escape 关不掉】与素材图的悬停菜单同类但有两处不同：
# 收起必须派发 mouseleave/mouseout（Escape 无效），且离场有动画要等。
#
# 【判菜单项可点必须用 rect，不能用 offsetHeight】2026-08-26 实测：hover 后菜单项的
# offsetHeight 立刻就是 32，但 getBoundingClientRect 全 0、浮层还停在
# ant-slide-up-enter-prepare、opacity:0、top 是 2527px 的陈旧停靠位（视口高仅 1313）。
# 根因是窗口被遮挡时 rAF 被节流到约 1.7 帧/秒（此时 visibilityState 仍是 visible、
# hasFocus() 仍是 true，fix_hidden_tab 那两个 CDP 开关修不了），而 antd CSSMotion
# 每跳一个阶段要一个 rAF。故固定 sleep 会误判，须轮询 rect 且【等待期间每轮补派发
# mouseover/mousemove】——否则 antd 的 hover 判定会超时把菜单自己收掉。
# 这与记忆 dianxiaomi-publish-dropdown-hover-anim 是同一条坑。

# 「重新上传」下拉的两个菜单项（popTemu 只有这两项；第三项「从速卖通视频库选择」
# 是 smt 平台的，popTemu 传进来的 menu 里没有）
VIDEO_MENU_ITEMS = ("本地上传", "网络上传")

# 「网络上传」弹窗的标题（2026-08-26 实测原文，严格等值匹配）
VIDEO_MODAL_TITLE = "视频地址"


# 展开「重新上传」下拉并点「网络上传」，打开地址输入弹窗。
#
# 【整段放在一个 evaluate 里】与空间图片弹窗同一个理由：hover 菜单会因失焦自动收起，
# 分成多次往返时中间那一步可能落在已消失的 DOM 上。
_JS_OPEN_VIDEO_NET_MODAL = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const op = document.querySelector('.video-operate');
  if (!op) return JSON.stringify({stage: 'locate', err: '找不到视频区块 .video-operate'});

  // 「重新上传」按钮：文本可能是「重新上传」（已有视频）或「添加视频」（没有视频），
  // 由组件按当前有没有视频决定，故两种都认
  const btn = Array.from(op.querySelectorAll('button')).find(b => {
    const t = (b.textContent || '').trim();
    return t === '重新上传' || t === '添加视频';
  });
  if (!btn) return JSON.stringify({stage: 'locate',
    err: '视频区里找不到「重新上传」/「添加视频」按钮',
    btns: Array.from(op.querySelectorAll('button')).map(b => (b.textContent || '').trim())});

  btn.scrollIntoView({block: 'center'});
  await sleep(300);

  const want = __ITEMS__;
  // 在众多 dropdown 实例里按【菜单项集合】认出视频那个菜单，排除 display:none 的
  const findMenu = () => Array.from(document.querySelectorAll('.ant-dropdown')).find(d => {
    if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
    const txt = Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .map(i => (i.textContent || '').trim());
    return want.every(w => txt.includes(w));
  });
  const hover = () => ['mouseenter', 'mouseover', 'mousemove'].forEach(t =>
    btn.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));

  // 轮询等菜单项真正可点：判据是 rect 有尺寸且落在视口内（见上方注释，
  // offsetHeight 会在入场动画中途就通过）。每轮补派发 hover 维持 antd 的悬停态。
  let menu = null, item = null, rect = null;
  for (let i = 0; i < 40; i++) {
    hover();
    menu = findMenu();
    if (menu) {
      item = Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'))
        .find(x => (x.textContent || '').trim() === '网络上传');
      if (item) {
        const r = item.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && r.top >= 0 && r.top < window.innerHeight) {
          rect = {w: Math.round(r.width), h: Math.round(r.height), y: Math.round(r.top)};
          break;
        }
      }
    }
    await sleep(150);
  }
  if (!menu) return JSON.stringify({stage: 'menu', err: 'hover 后视频下拉未展开'});
  if (!item) return JSON.stringify({stage: 'menu', err: '下拉里没有「网络上传」项',
    items: Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'))
      .map(x => (x.textContent || '').trim())});
  if (!rect) return JSON.stringify({stage: 'menu',
    err: '「网络上传」项 6s 内没进入可点状态（入场动画未完成，页面可能被遮挡）'});

  item.click();

  // 轮询等弹窗出现。
  //
  // 【可见性判据：标题匹配 + 内层 textarea 有真实尺寸。三个看着更自然的判据都不行】
  // 2026-08-26 逐帧采样取证（workspace/_diag_video_modal_{open,close}.py）：
  //   - inline display:none —— 离场动画整段都不出现，关闭判定会一直以为还开着；
  //   - getComputedStyle(wrap).display —— 同样整段是 block，同上；
  //   - offsetParent !== null —— 【恒为 false】。.ant-modal-wrap 是 position:fixed，
  //     而 fixed 元素的 offsetParent 按规范就是 null，与它可不可见无关。
  //     用它判「已打开」会永远判不到（实测：弹窗完全可操作、textarea 552×199，
  //     offsetParent 仍是 false）。
  // 真正跟随实际状态的是【内层控件的 rect】：开着时 textarea 有尺寸，
  // 关闭时整个节点被移除（约 1s），rect 自然消失。这也正是我们真正关心的东西——
  // 「能不能往里填字」，而不是「某个 style 属性长什么样」。
  //
  // 【另外绝不能取第一个】实测同一时刻页面上有 2 个标题都是「视频地址」的 wrap
  // （上一轮遗留的隐藏节点 + 本次新建的），故必须逐个验 rect 再挑。
  const liveModal = () => Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .map(w => w.querySelector('.ant-modal'))
    .find(m => {
      if (!m) return false;
      if (((m.querySelector('.ant-modal-title') || {}).textContent || '').trim() !== __TITLE__)
        return false;
      const ta = m.querySelector('textarea');
      return !!ta && ta.getBoundingClientRect().height > 0;
    });
  const findModal = liveModal;
  let modal = null;
  for (let i = 0; i < 60; i++) {
    modal = findModal();
    if (modal) break;
    await sleep(100);
  }
  if (!modal) return JSON.stringify({stage: 'modal',
    err: '点了「网络上传」但「' + __TITLE__ + '」弹窗 6s 内没出现',
    titles: Array.from(document.querySelectorAll('.ant-modal-wrap'))
      .map(w => (((w.querySelector('.ant-modal-title')) || {}).textContent || '').trim())});
  return JSON.stringify({stage: 'ok', opened: true, itemRect: rect});
})()"""


# 在「视频地址」弹窗里填 URL 并点确定。
#
# 【输入控件是 <textarea> 不是 <input>】2026-08-26 真站探查纠正：
# `.ant-modal input` 命中 0 个，唯一命中的是 .ant-modal-body textarea
# （class 是 ant-input，无 id 无 name）。按 input 找会永远找不到、报成「弹窗结构变了」。
#
# 【必须派发 input 事件】Vue 的 v-model 靠 input 事件同步，只设 value 属性
# 页面状态不会变，点确定会被当成空值、弹「请输入视频地址」。
#
# 【确定按钮不能按位置取】footer 里 DOM 顺序是【取消在前、确定在后】（视觉上确定在
# 左边），按 nth-child 或第一个 button 会点到取消——那会静默放弃本次设置。
# 故按按钮文本严格等值定位。
_JS_FILL_VIDEO_URL = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 判据：标题匹配 + textarea 有真实尺寸（详见 _JS_OPEN_VIDEO_NET_MODAL 里那段长注释；
  // inline/computed display 与 offsetParent 三者都不可用）。
  // 这里尤其关键——本函数正是靠「弹窗消失」判断 beforeCheck 有没有放行。
  const findModal = () => Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .map(w => w.querySelector('.ant-modal'))
    .find(m => {
      if (!m) return false;
      if (((m.querySelector('.ant-modal-title') || {}).textContent || '').trim() !== __TITLE__)
        return false;
      const t = m.querySelector('textarea');
      return !!t && t.getBoundingClientRect().height > 0;
    });
  const modal = findModal();
  if (!modal) return JSON.stringify({stage: 'modal', err: '「' + __TITLE__ + '」弹窗没打开'});

  const ta = modal.querySelector('.ant-modal-body textarea')
    || modal.querySelector('textarea');
  if (!ta) return JSON.stringify({stage: 'input',
    err: '弹窗里找不到地址输入框（textarea）——弹窗结构可能变了'});

  // v-model 要靠 input 事件同步；只设 value 不派发事件，点确定会被当成空值
  ta.focus();
  ta.value = __URL__;
  ta.dispatchEvent(new Event('input', {bubbles: true}));
  ta.dispatchEvent(new Event('change', {bubbles: true}));
  await sleep(120);
  const filled = ta.value;

  // 【按文本定位确定，别按位置】footer 里取消在前、确定在后
  const ok = Array.from(modal.querySelectorAll('.ant-modal-footer button'))
    .find(b => (b.textContent || '').trim() === '确定');
  if (!ok) return JSON.stringify({stage: 'confirm', err: '找不到「确定」按钮',
    btns: Array.from(modal.querySelectorAll('.ant-modal-footer button'))
      .map(b => (b.textContent || '').trim())});
  ok.click();

  // beforeCheck 不通过时【弹窗不关、输入不清空】，故「弹窗消失」就是校验通过的信号。
  // 校验失败的三档文案（请输入视频地址 / 视频地址必须以http或https开头！/
  // 视频地址格式不支持）由 browser.py 的 toast 哨兵打进日志，这里只判弹窗是否关掉。
  let stillOpen = true;
  for (let i = 0; i < 60; i++) {
    stillOpen = !!findModal();
    if (!stillOpen) break;
    await sleep(100);
  }
  return JSON.stringify({stage: 'ok', filled: filled, stillOpen: stillOpen,
    accepted: !stillOpen});
})()"""


# 关掉「视频地址」弹窗（点取消）。best-effort，但必须尽力关：
# 遮罩留着会挡住后续所有点击，让下一个阶段莫名其妙全部失败。
_JS_CLOSE_VIDEO_MODAL = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 判据：标题匹配 + textarea 有真实尺寸（理由见 _JS_OPEN_VIDEO_NET_MODAL 里那段长注释）
  const findModal = () => Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .map(w => w.querySelector('.ant-modal'))
    .find(m => {
      if (!m) return false;
      if (((m.querySelector('.ant-modal-title') || {}).textContent || '').trim() !== __TITLE__)
        return false;
      const t = m.querySelector('textarea');
      return !!t && t.getBoundingClientRect().height > 0;
    });
  const modal = findModal();
  if (!modal) return JSON.stringify({wasOpen: false});
  const btn = Array.from(modal.querySelectorAll('.ant-modal-footer button'))
    .find(b => (b.textContent || '').trim() === '取消')
    || modal.querySelector('.ant-modal-close');
  if (!btn) return JSON.stringify({wasOpen: true, clicked: false, stillOpen: true});
  btn.click();
  // 轮询等它真的不可见（实测离场动画约 1s，rAF 被节流时更久），别用固定 sleep
  let open = true;
  for (let i = 0; i < 40; i++) {
    open = !!findModal();
    if (!open) break;
    await sleep(100);
  }
  return JSON.stringify({wasOpen: true, clicked: true, stillOpen: open});
})()"""


# 收起「重新上传」下拉：Escape 关不掉（实测），必须派发 mouseleave/mouseout。
_JS_CLOSE_VIDEO_MENU = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const op = document.querySelector('.video-operate');
  if (!op) return JSON.stringify({ok: false, err: '找不到视频区块'});
  const btn = Array.from(op.querySelectorAll('button')).find(b => {
    const t = (b.textContent || '').trim();
    return t === '重新上传' || t === '添加视频';
  });
  const want = __ITEMS__;
  const visible = () => Array.from(document.querySelectorAll('.ant-dropdown')).some(d => {
    if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
    const txt = Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .map(i => (i.textContent || '').trim());
    return want.every(w => txt.includes(w));
  });
  if (btn) {
    ['mouseleave', 'mouseout'].forEach(t =>
      btn.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
  }
  // 离场有动画，轮询等 display:none
  let open = true;
  for (let i = 0; i < 30; i++) {
    open = visible();
    if (!open) break;
    await sleep(100);
  }
  return JSON.stringify({ok: !open, stillOpen: open});
})()"""


# 回读视频区当前状态：判「地址填进去了没有」的取证依据。
#
# 【不能按「有没有 img」判】组件的封面容器里始终有一张 img——没视频时是内嵌 base64
# 的播放器占位图（2026-08-26 探查确认）。可靠信号是「播放」链接是否可见：
# 组件按当前有没有视频地址来控制它的显隐。
_JS_VIDEO_STATE = r"""(() => {
  const op = document.querySelector('.video-operate');
  if (!op) return JSON.stringify({err: '找不到视频区块'});
  const links = Array.from(op.querySelectorAll('a')).map(a => ({
    text: (a.textContent || '').trim(),
    shown: !/display:\s*none/.test(a.getAttribute('style') || ''),
  }));
  const nameEl = op.querySelector('.video-name');
  return JSON.stringify({
    hasPlay: links.some(l => l.text === '播放' && l.shown),
    hasDelete: links.some(l => l.text === '删除' && l.shown),
    videoName: nameEl ? (nameEl.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80) : '',
    links: links,
  });
})()"""


async def set_video(session: BrowserSession, video_path: str,
                    full_cid: Optional[str] = None) -> dict:
    """阶段⑥b：把【已合规化】的本地视频直传图床，再用「网络上传」把地址填回表单。

    video_path 应当是 video.normalize_video 出的产物（比例已在 1:1/3:4/16:9 之内）。
    本函数不代做合规化——那是纯本地的确定性变换，由 service 层先做好再传进来，
    与素材图（square_image 在外面做）保持同一分工，失败时分得清是哪一层的问题。
    upload_video 内部仍有一道比例闸门兜底，绕不过去。

    不导航：与其它写入阶段一致，须在编辑页当前会话执行。

    【成果到 ⑭ save 才落库】本阶段只改页面上的 Vue 状态，与 ⑤~⑬ 同性质。
    """
    up = await upload_video(session, video_path, full_cid=full_cid)
    if up.get("status") != "ok":
        return {"status": "error", "stage": "upload", "upload": up}

    before = await session.eval_json(_JS_VIDEO_STATE)

    opened = await session.eval_json(
        _JS_OPEN_VIDEO_NET_MODAL
        .replace("__ITEMS__", J(list(VIDEO_MENU_ITEMS)))
        .replace("__TITLE__", J(VIDEO_MODAL_TITLE))
    )
    if opened.get("err") or not opened.get("opened"):
        # 菜单可能还展开着挡住后续操作，尽力收起（best-effort）
        await session.eval_json(
            _JS_CLOSE_VIDEO_MENU.replace("__ITEMS__", J(list(VIDEO_MENU_ITEMS))))
        return {"status": "error", "stage": "open-modal", "detail": opened, "upload": up}

    filled = await session.eval_json(
        _JS_FILL_VIDEO_URL
        .replace("__TITLE__", J(VIDEO_MODAL_TITLE))
        .replace("__URL__", J(up["url"]))
    )
    if filled.get("err") or not filled.get("accepted"):
        # 校验没过弹窗不会关，必须关掉——遮罩留着会让后续阶段全挂
        closed = await _close_video_modal(session)
        return {"status": "error", "stage": filled.get("stage") or "fill",
                "detail": filled, "closed": closed, "upload": up,
                "note": "地址被弹窗校验拒了（看日志里的页面提示找原因）"}

    after = await session.eval_json(_JS_VIDEO_STATE)
    # 成功判据：弹窗已关（accepted）+「播放」入口可见（组件按有没有视频地址控制它）
    ok = bool(after.get("hasPlay"))
    if not ok:
        logger.error(f"视频地址填完后「播放」入口仍不可见：before={before} after={after}")
    else:
        logger.info(f"产品视频已替换为 {os.path.basename(video_path)}（{up['url'][:80]}）")
    return {"status": "ok" if ok else "error",
            "stage": "" if ok else "readback",
            "upload": up, "url": up["url"], "videoId": up.get("videoId"),
            "stateBefore": before, "stateAfter": after,
            "hint": "改动尚未落库，需走阶段⑭ save" if ok else ""}


async def _close_video_modal(session: BrowserSession) -> dict:
    """关掉「视频地址」弹窗（点取消）。best-effort：关不掉只记警告不抛。

    但必须尽力关：遮罩会挡住后续一切点击，留着它会让下一个阶段莫名其妙地全部失败
    （与 _close_space_modal 同一取向）。
    """
    try:
        r = await session.eval_json(
            _JS_CLOSE_VIDEO_MODAL.replace("__TITLE__", J(VIDEO_MODAL_TITLE)))
        if r.get("stillOpen"):
            killed = await session.kill_stuck_modals()
            r["killedStuck"] = killed.get("removed")
        return r
    except Exception as e:
        logger.warning(f"关视频地址弹窗失败（不影响主流程判定）：{e}")
        return {"err": str(e)}


# 视频区「删除」链接：把整块封面 + 播放/删除入口置空（详见 delete_video 的说明）。
#
# 【为什么按 .video-operate-box 里的 a 文本认，而不用 nth-child】2026-08-29 探查
# （workspace/_probe_video_delete.py）确认这一块的真实结构是：
#     .video-operate-img > .video-operate-img-box
#         ├─ .img-out > img.img-css        封面（没视频时是内嵌 base64 占位图）
#         └─ .video-operate-box
#               ├─ <div><a class="link"> 播放 </a></div>   ← 播放多套了一层 div
#               └─ <a class="link"> 删除 </a>              ← 删除是直接子节点
# 两个 a 的 class 都只是 link、层级还不对称，故只有文本可靠；且文本两侧带空格，
# 必须 trim 后严格等值比（用 includes 会把别处的「删除」按钮也认进来）。
_JS_DELETE_VIDEO = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const op = document.querySelector('.video-operate');
  if (!op) return JSON.stringify({stage: 'locate', err: '找不到视频区块 .video-operate'});
  const box = op.querySelector('.video-operate-box');
  if (!box) return JSON.stringify({stage: 'locate', err: '找不到 .video-operate-box'});
  const a = Array.from(box.querySelectorAll('a'))
    .find(x => (x.textContent || '').trim() === '删除');
  if (!a) return JSON.stringify({stage: 'locate', err: '视频区里找不到「删除」链接',
    links: Array.from(box.querySelectorAll('a')).map(x => (x.textContent || '').trim())});

  const imgBox = op.querySelector('.video-operate-img');
  const gone = () => !imgBox || getComputedStyle(imgBox).display === 'none';
  if (gone()) return JSON.stringify({stage: 'done', already: true});

  a.scrollIntoView({block: 'center'});
  await sleep(200);
  a.click();

  // 轮询等置空生效。实测是同步的（0.4s 采样第一帧就已 display:none），但组件
  // 若哪天改成异步置空，固定 sleep 会误判成失败，故仍轮询。
  let ok = false;
  for (let i = 0; i < 20; i++) {
    ok = gone();
    if (ok) break;
    await sleep(150);
  }
  return JSON.stringify({stage: 'done', already: false, cleared: ok,
    imgBoxStyle: imgBox ? (imgBox.getAttribute('style') || '') : ''});
})()"""


async def delete_video(session: BrowserSession) -> dict:
    """把编辑页产品视频【删掉】（丢弃视频路线，与 set_video 二选一）。

    【为什么要这条路线】set_video 是把 1688 搬来的竖屏视频裁成 Temu 允许的比例再回填，
    但下载 + ffmpeg 转码 + 直传三步每个商品要花几十秒到几分钟，而视频本身只是加分项。
    批量发布时用户往往宁可整批不要视频、换取速度与确定性（不合规视频会在阶段⑮ 发布
    之后才被平台打回，见 set_video 上方的说明）。故给批次一个开关：开则走原来的比例
    审核，关就在这里直接点「删除」。

    【纯前端置空，不打接口，不弹二次确认——2026-08-29 逐帧探查取证】
    （workspace/_probe_video_delete_click.py，10 帧 × 0.4s 采样）：
      - 点击后第一帧 .video-operate-img 就已 display:none，播放/删除两个 a 高度归 0；
      - 全程 .ant-modal-wrap / .ant-popconfirm 一个都没出现，也没有任何 toast；
      - 不 save 直接重载编辑页，封面块回到 display:flex——说明改动只在未保存的表单里。
    最后一点意味着这个操作的安全阀与 desc-delete 相同：真正生效要靠后续阶段⑭ save。

    返回 {"status": "ok"/"error", "already": 本来就没视频, "stage": 失败环节}。
    """
    r = await session.eval_json(_JS_DELETE_VIDEO)
    if r.get("err"):
        return {"status": "error", "stage": r.get("stage") or "", **r}
    if r.get("already"):
        logger.info("视频区本来就是空的，无需删除")
        return {"status": "ok", "already": True}
    if not r.get("cleared"):
        # 点了但封面块还在：组件没吃这一下，页面上视频还挂着
        return {"status": "error", "stage": "verify",
                "err": "已点「删除」但视频封面块 3s 内仍未消失", **r}
    logger.info("产品视频已删除（未保存，落库靠后续 save）")
    return {"status": "ok", "already": False}


async def read_video_url(session: BrowserSession, rowid: str) -> dict:
    """只读：从详情接口取当前商品的 videoUrl 一族字段。

    【为什么读接口而不读 DOM】视频区 DOM 里【没有】真实地址——封面是内嵌 base64
    的播放器占位图，Vue 3 的 setupState 也被编译隐藏，扒不到（2026-08-26 探查确认）。
    地址只在 /api/popTemuProduct/edit.json 的响应里。

    页面内 fetch 而不在 Python 侧发：这个接口靠登录 cookie 鉴权，页面内天然带 cookie。

    【字段在 data.product 下，不在顶层——必须递归找】2026-08-26 实测：只扫顶层键会
    得到空结果，于是每个商品都被报成「没有视频」、⑬b 整段静默跳过，等于这一步白做，
    而表面上一切正常（skipped 不是错误）。故这里递归下钻，并回报命中路径便于排查。
    """
    js = r"""(async () => {
      const r = await fetch('/api/popTemuProduct/edit.json?id=' + encodeURIComponent(__ID__),
        {credentials: 'include'});
      const j = await r.json();
      const d = j.data || j;
      // 递归找含 video 的键：实测挂在 data.product 下，但别写死路径——
      // 换个平台/版本层级可能变，递归对两种都成立
      const pick = {};
      const paths = {};
      const walk = (o, path, depth) => {
        if (!o || typeof o !== 'object' || depth > 4) return;
        for (const k of Object.keys(o)) {
          const v = o[k];
          if (/^video|^dxmVideo|^detailVideo/i.test(k) && !(k in pick)) {
            pick[k] = v;
            paths[k] = path + '.' + k;
          }
          if (v && typeof v === 'object' && !Array.isArray(v)) walk(v, path + '.' + k, depth + 1);
        }
      };
      walk(d, 'data', 0);
      return JSON.stringify({code: j.code, fields: pick, paths: paths});
    })()""".replace("__ID__", J(rowid))
    r = await session.eval_json(js)
    fields = r.get("fields") or {}
    return {"status": "ok" if r.get("code") == 0 else "error",
            "videoUrl": fields.get("videoUrl") or "",
            "dxmVideoId": fields.get("dxmVideoId"),
            "fields": fields, "paths": r.get("paths") or {}}
