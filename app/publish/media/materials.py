"""店小秘发布操作：media.materials。模块导航见 docs/publish-pipeline-refactor.md。"""

from app.logger import logger
from app.publish.browser import BrowserSession, J
from app.publish.media import space as media_space
from app.publish.upload import upload_image
from typing import Optional


# ==================== 阶段⑥⑦⑪ 图片替换的公共机制 ====================
# 这三个阶段都走同一条路：图片直传图床（upload.upload_image）→ 在页面上打开
# 「空间图片」弹窗 → 选中刚传的图 → 确定。差别只在【入口】怎么打开菜单：
#   ⑥ 素材图：悬停图片展开 4 项菜单（本地图片/空间图片/网络图片/引用采集图片）
#   ⑦ SKC 行：点行内「选择图片」按钮
#   ⑪ 描述图：在描述编辑器里点图片
# 故把「弹窗内选图 + 确定」抽成 _pick_from_space 共用，入口各自实现。
#
# 【2026-08-20 真站实测修正了原 skill 的三条结论，勿照搬原脚本】
# 1. 悬停菜单用【合成 mouseenter 即可展开】，不必 CDP 真实鼠标移动、也不必「先移开
#    再移入」。原脚本走 WebBridge 时的结论在 Playwright 直连下不成立。
# 2. 弹窗标题是「从图片空间选择」，不是原脚本匹配的「图片空间」（原脚本用 includes
#    恰好命中，但按全等匹配就会失败）。
# 3. 判断菜单是否展开【不能用 offsetHeight】：菜单 off-screen 停靠，隐藏时 style 里
#    仍留着上次的 left/top，只有 display:none 是可靠信号。
#
# 页面上有 13 个 .ant-dropdown 实例并存（多组「应用到全部/同颜色/同尺码」、
# 「小秘美图/图片翻译/...」等干扰项），故一律按【菜单项集合】定位，绝不取第一个。

# 素材图悬停菜单的 4 个菜单项文本，用于在众多 dropdown 实例里认出它
MATERIAL_MENU_ITEMS = ("本地图片", "空间图片", "网络图片", "引用采集图片")


# 悬停素材图展开菜单并点「空间图片」。
# 合成 mouseenter 对图片元素和它的 .single-image 容器都派发：实测容器才是绑事件的
# 那一层，但两个都发更稳（多余的那次无副作用）。
_JS_OPEN_MATERIAL_SPACE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const mod = document.querySelector('.material-img-module');
  if (!mod) return JSON.stringify({stage: 'locate', err: '找不到素材图区块'});
  const img = mod.querySelector('.single-image img') || mod.querySelector('img');
  if (!img) return JSON.stringify({stage: 'locate', err: '素材图区块里没有图片'});
  const before = img.src || '';
  img.scrollIntoView({block: 'center'});
  await sleep(900);

  const box = img.closest('.single-image') || img.parentElement;
  [img, box].forEach(el => {
    if (!el) return;
    ['mouseenter', 'mouseover', 'mousemove'].forEach(t =>
      el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
  });
  await sleep(1500);

  // 按【菜单项集合】在众多 dropdown 实例里认出素材图那个菜单，并且排除
  // display:none 的隐藏实例（offsetHeight 对 off-screen 停靠的菜单不可靠）
  const want = __ITEMS__;
  const menu = Array.from(document.querySelectorAll('.ant-dropdown')).find(d => {
    if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
    const txt = Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .map(i => (i.textContent || '').trim());
    return want.every(w => txt.includes(w));
  });
  if (!menu) return JSON.stringify({stage: 'menu', err: '悬停后菜单未展开'});

  const item = Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'))
    .find(i => (i.textContent || '').trim() === '空间图片');
  if (!item) return JSON.stringify({stage: 'menu', err: '菜单里没有「空间图片」项'});
  item.click();
  await sleep(3000);
  const opened = Array.from(document.querySelectorAll('.ant-modal'))
    .some(m => m.offsetHeight > 0 &&
      ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
  return JSON.stringify({stage: 'ok', opened, srcBefore: before.slice(-50)});
})()"""


# 回读素材图 src，用于替换前后对比（成功判据）
_JS_MATERIAL_SRC = r"""(() => {
  const mod = document.querySelector('.material-img-module');
  const img = mod && (mod.querySelector('.single-image img') || mod.querySelector('img'));
  return JSON.stringify({src: img ? (img.currentSrc || img.src || '') : null,
    w: img ? img.naturalWidth : 0, h: img ? img.naturalHeight : 0});
})()"""


async def set_material(session: BrowserSession, image_path: str,
                       full_cid: Optional[str] = None) -> dict:
    """阶段⑥ 素材图替换：本地图 → 直传图床 → 悬停菜单选「空间图片」→ 弹窗选图 → 确定。

    image_path 应当是【已做过合规化】的图（images.square_image 出的 1785² 方图）：
    素材图要求 1:1 且不小于 1340×1785 的短边，原图 800×800 会被发布校验拦下。
    本函数不代做合规化——那是纯本地的确定性变换，调用方（service 层）先做好再传进来，
    免得这里既管页面交互又管图片处理、失败时分不清是哪一层的问题。

    不导航：与其它写入阶段一致，须在编辑页当前会话执行。
    """
    up = await upload_image(session, image_path, full_cid=full_cid)
    if up.get("status") != "ok":
        return {"status": "error", "stage": "upload", "upload": up}

    before = await session.eval_json(_JS_MATERIAL_SRC)

    opened = await session.eval_json(
        _JS_OPEN_MATERIAL_SPACE
        .replace("__ITEMS__", J(list(MATERIAL_MENU_ITEMS)))
        .replace("__TITLE__", J(media_space.SPACE_MODAL_TITLE))
    )
    if opened.get("err") or not opened.get("opened"):
        return {"status": "error", "stage": "open-space", "detail": opened, "upload": up}

    picked = await media_space._pick_from_space(session, up["fileId"])
    if picked.get("err"):
        # 弹窗可能还开着挡住后续操作，尽力关掉（best-effort，失败不影响错误返回）
        await media_space._close_space_modal(session)
        return {"status": "error", "stage": "pick", "detail": picked, "upload": up}

    after = await session.eval_json(_JS_MATERIAL_SRC)
    fid = up["fileId"].rsplit("/", 1)[-1]
    # 成功判据是回读到的 src 含新 fileId：只看「src 变了」不够，
    # 选错图或挂到别处时 src 同样会变（原脚本的素材图误替换事故就是这么发生的）
    ok = fid in (after.get("src") or "")
    if not ok:
        logger.error(f"素材图替换后回读不含新 fileId：before={before.get('src')} "
                     f"after={after.get('src')}")
    return {"status": "ok" if ok else "error",
            "stage": "" if ok else "readback",
            "upload": up, "srcBefore": before.get("src"), "srcAfter": after.get("src"),
            "sizeAfter": {"w": after.get("w"), "h": after.get("h")}, "picked": picked}
