"""旧发布管线兼容导出；业务编排入口位于 workflows/，页面操作按职责拆分。"""

import asyncio as asyncio
import json as json
import os as os
import re as re
import time as time
from app.logger import logger as logger
from app.publish import browser as browser, cache as cache, images as images
from app.publish.accessories import (
    PACKING_FALLBACK_ACCESSORY as PACKING_FALLBACK_ACCESSORY,
    _PACKING_ACCESSORY_WORDS as _PACKING_ACCESSORY_WORDS,
    _PACKING_ALIAS as _PACKING_ALIAS,
    _TITLE_ACCESSORY_HINTS as _TITLE_ACCESSORY_HINTS,
    _canon_accessory as _canon_accessory,
    _guess_accessory_by_title as _guess_accessory_by_title,
    _normalize_sku_judge as _normalize_sku_judge,
    judge_sku_category as judge_sku_category,
)
from app.publish.attributes.composition import (
    _COMP_FILLERS as _COMP_FILLERS,
    _COMP_HINTS as _COMP_HINTS,
    _COMP_NORM_PROMPT as _COMP_NORM_PROMPT,
    _COMP_SUB_MARKS as _COMP_SUB_MARKS,
    _FIBER_NAMES as _FIBER_NAMES,
    _FIBER_SYNONYMS as _FIBER_SYNONYMS,
    _fiber_key as _fiber_key,
    _infer_filler as _infer_filler,
    _is_main_comp_label as _is_main_comp_label,
    _match_fiber as _match_fiber,
    _merge_composition_sources as _merge_composition_sources,
    _merge_same_fiber as _merge_same_fiber,
    _norm_fiber as _norm_fiber,
    _rebuild_main_comp as _rebuild_main_comp,
    _resolve_main_fiber as _resolve_main_fiber,
)
from app.publish.attributes.dropdowns import (
    _click_dropdown_option as _click_dropdown_option,
    _expand_attr_section as _expand_attr_section,
    _is_dropdown_open as _is_dropdown_open,
    _js_dropdown_options_rendered as _js_dropdown_options_rendered,
    _open_attr_dropdown as _open_attr_dropdown,
    _park_ghost_dropdowns as _park_ghost_dropdowns,
    _press_escape as _press_escape,
    _read_active_options as _read_active_options,
    _scroll_click_option as _scroll_click_option,
    _visible_dropdown_near as _visible_dropdown_near,
)
from app.publish.attributes.form import (
    _JS_LIST_ATTR_ROWS as _JS_LIST_ATTR_ROWS,
    _JS_SET_ATTR_NUM as _JS_SET_ATTR_NUM,
    _ensure_comp_rows as _ensure_comp_rows,
    _readback_attr as _readback_attr,
    _readback_current as _readback_current,
    _set_select_by_label as _set_select_by_label,
    _trim_comp_rows as _trim_comp_rows,
    dump_attrs as dump_attrs,
    set_attr as set_attr,
)
from app.publish.attributes.review import (
    _ATTR_PROMPT as _ATTR_PROMPT,
    _ATTR_SPLIT_MIN_ROWS as _ATTR_SPLIT_MIN_ROWS,
    _apply_attr_changes as _apply_attr_changes,
    _ask_attr_review as _ask_attr_review,
    _retry_row as _retry_row,
    _split_attr_rows as _split_attr_rows,
)
from app.publish.attributes.validation import _validate_attr_changes as _validate_attr_changes
from app.publish.attributes.server_options import (
    fetch_attr_options as fetch_attr_options,
)
from app.publish.attributes.workflow import (
    _LINKAGE_MAX_ROUNDS as _LINKAGE_MAX_ROUNDS,
    _fill_linkage_round as _fill_linkage_round,
    _fill_linkage_rows as _fill_linkage_rows,
    _read_linkage_options as _read_linkage_options,
    _scan_linkage_rows as _scan_linkage_rows,
    check_attrs as check_attrs,
)
from app.publish.browser import (
    BrowserSession as BrowserSession,
    DRAFT_LIST_URL as DRAFT_LIST_URL,
    EDIT_URL as EDIT_URL,
    J as J,
    ONLINE_LIST_URL as ONLINE_LIST_URL,
    PUBLISH_FAIL_LIST_URL as PUBLISH_FAIL_LIST_URL,
)
from app.publish.category import (
    _CAT_FROM_CACHE_PROMPT as _CAT_FROM_CACHE_PROMPT,
    _CAT_PROMPT as _CAT_PROMPT,
    _JS_CAT_COLUMNS as _JS_CAT_COLUMNS,
    _JS_OPEN_CAT_MODAL as _JS_OPEN_CAT_MODAL,
    _cat_columns as _cat_columns,
    _click_cat_in_column as _click_cat_in_column,
    _click_cat_path as _click_cat_path,
    _confirm_cat as _confirm_cat,
    _format_cached_paths as _format_cached_paths,
    _pick_cached_category as _pick_cached_category,
    _pick_category as _pick_category,
    _try_cached_category as _try_cached_category,
    auto_cat as auto_cat,
    cat_clues as cat_clues,
    read_current_category as read_current_category,
)
from app.publish.common import _poll_until as _poll_until
from app.publish.media.description import (
    _desc_ensure_open as _desc_ensure_open,
    desc_delete as desc_delete,
    desc_map as desc_map,
    desc_save as desc_save,
    desc_text_apply as desc_text_apply,
    desc_text_delete_all as desc_text_delete_all,
    desc_text_map as desc_text_map,
    ensure_desc_closed as ensure_desc_closed,
)
from app.publish.media.description_replace import (
    DESC_MENU_ITEMS as DESC_MENU_ITEMS,
    _desc_aim_replace_link as _desc_aim_replace_link,
    desc_replace as desc_replace,
)
from app.publish.media.description_replace_scripts import (
    _JS_DESC_BOX_POS as _JS_DESC_BOX_POS,
    _JS_DESC_BOX_SCROLL as _JS_DESC_BOX_SCROLL,
    _JS_DESC_DISPATCH_LINK as _JS_DESC_DISPATCH_LINK,
    _JS_DESC_MENU_STATE as _JS_DESC_MENU_STATE,
    _JS_DESC_PICK_SPACE as _JS_DESC_PICK_SPACE,
    _JS_DESC_REPLACE_LINK as _JS_DESC_REPLACE_LINK,
)
from app.publish.media.description_scripts import (
    _JS_DESC_CLOSE_EXTRA as _JS_DESC_CLOSE_EXTRA,
    _JS_DESC_CLOSE_IF_OPEN as _JS_DESC_CLOSE_IF_OPEN,
    _JS_DESC_DELETE as _JS_DESC_DELETE,
    _JS_DESC_IDX_MAP as _JS_DESC_IDX_MAP,
    _JS_DESC_MODAL as _JS_DESC_MODAL,
    _JS_DESC_OPEN as _JS_DESC_OPEN,
    _JS_DESC_SAVE as _JS_DESC_SAVE,
    _JS_DESC_STATE as _JS_DESC_STATE,
    _JS_DESC_TEXT_MAP as _JS_DESC_TEXT_MAP,
    _JS_DESC_TEXT_SET as _JS_DESC_TEXT_SET,
)
from app.publish.media.materials import (
    MATERIAL_MENU_ITEMS as MATERIAL_MENU_ITEMS,
    _JS_MATERIAL_SRC as _JS_MATERIAL_SRC,
    _JS_OPEN_MATERIAL_SPACE as _JS_OPEN_MATERIAL_SPACE,
    set_material as set_material,
)
from app.publish.media.menus import (
    _JS_BLANK_POINT as _JS_BLANK_POINT,
    _JS_PARK_IMAGE_MENUS as _JS_PARK_IMAGE_MENUS,
    _cdp_click_xy as _cdp_click_xy,
    _park_image_menus as _park_image_menus,
)
from app.publish.media.preview import (
    PREVIEW_MIN_SIDE as PREVIEW_MIN_SIDE,
    SKU_PREVIEW_EMPTY_MENU_ITEMS as SKU_PREVIEW_EMPTY_MENU_ITEMS,
    SKU_PREVIEW_MENU_ITEMS as SKU_PREVIEW_MENU_ITEMS,
    _JS_OPEN_SKU_PREVIEW_FILL_SPACE as _JS_OPEN_SKU_PREVIEW_FILL_SPACE,
    _JS_OPEN_SKU_PREVIEW_SPACE as _JS_OPEN_SKU_PREVIEW_SPACE,
    _JS_SKU_PREVIEW_ROW_SRC as _JS_SKU_PREVIEW_ROW_SRC,
    _JS_SKU_PREVIEW_STATE as _JS_SKU_PREVIEW_STATE,
    sku_preview_replace_row as sku_preview_replace_row,
    sku_preview_state as sku_preview_state,
)
from app.publish.media.skc import (
    DXM_IMAGE_HOST_MARK as DXM_IMAGE_HOST_MARK,
    SKC_MENU_EXTRA_ITEM as SKC_MENU_EXTRA_ITEM,
    SKC_ROW_MAX_IMAGES as SKC_ROW_MAX_IMAGES,
    SKC_ROW_MIN_IMAGES as SKC_ROW_MIN_IMAGES,
    _skc_aim_row_button as _skc_aim_row_button,
    _skc_open_space as _skc_open_space,
    _skc_row_matches as _skc_row_matches,
    _skc_row_state as _skc_row_state,
    _wait_scroll_settled as _wait_scroll_settled,
    _wait_skc_menu as _wait_skc_menu,
    skc_image_support as skc_image_support,
    skc_replace_row as skc_replace_row,
)
from app.publish.media.skc_scripts import (
    _JS_SCROLL_SETTLED as _JS_SCROLL_SETTLED,
    _JS_SKC_BTN_POS as _JS_SKC_BTN_POS,
    _JS_SKC_BTN_SCROLL as _JS_SKC_BTN_SCROLL,
    _JS_SKC_CLICK_SPACE as _JS_SKC_CLICK_SPACE,
    _JS_SKC_DEL_FIRST as _JS_SKC_DEL_FIRST,
    _JS_SKC_IMAGE_SUPPORT as _JS_SKC_IMAGE_SUPPORT,
    _JS_SKC_ROW_STATE as _JS_SKC_ROW_STATE,
    _JS_WAIT_SKC_MENU as _JS_WAIT_SKC_MENU,
)
from app.publish.media.space import (
    SPACE_MODAL_TITLE as SPACE_MODAL_TITLE,
    _JS_PICK_FROM_SPACE as _JS_PICK_FROM_SPACE,
    _JS_SPACE_CHECK_ONE as _JS_SPACE_CHECK_ONE,
    _JS_SPACE_CLICK_ONE as _JS_SPACE_CLICK_ONE,
    _JS_SPACE_CONFIRM as _JS_SPACE_CONFIRM,
    _close_space_modal as _close_space_modal,
    _pick_from_space as _pick_from_space,
    _pick_many_from_space as _pick_many_from_space,
)
from app.publish.media.video import (
    VIDEO_MENU_ITEMS as VIDEO_MENU_ITEMS,
    VIDEO_MODAL_TITLE as VIDEO_MODAL_TITLE,
    _JS_CLOSE_VIDEO_MENU as _JS_CLOSE_VIDEO_MENU,
    _JS_CLOSE_VIDEO_MODAL as _JS_CLOSE_VIDEO_MODAL,
    _JS_DELETE_VIDEO as _JS_DELETE_VIDEO,
    _JS_FILL_VIDEO_URL as _JS_FILL_VIDEO_URL,
    _JS_OPEN_VIDEO_NET_MODAL as _JS_OPEN_VIDEO_NET_MODAL,
    _JS_VIDEO_STATE as _JS_VIDEO_STATE,
    _close_video_modal as _close_video_modal,
    delete_video as delete_video,
    read_video_url as read_video_url,
    set_video as set_video,
)
from app.publish.navigation import (
    SECTION_IDS as SECTION_IDS,
    _JS_ALL_IMAGES as _JS_ALL_IMAGES,
    _JS_INSPECT as _JS_INSPECT,
    _JS_LIVE_STATE as _JS_LIVE_STATE,
    find_rowid as find_rowid,
    inspect as inspect,
    list_images as list_images,
    live_state as live_state,
    open_edit as open_edit,
)
from app.publish.packaging import (
    _APPAREL_DIMS as _APPAREL_DIMS,
    _APPAREL_EXCLUDE as _APPAREL_EXCLUDE,
    _APPAREL_WORDS as _APPAREL_WORDS,
    _CATEGORY_TAGS as _CATEGORY_TAGS,
    _PACK_FALLBACK as _PACK_FALLBACK,
    _check_pack_est as _check_pack_est,
    _is_apparel as _is_apparel,
    _llm_classify_nonapparel as _llm_classify_nonapparel,
    _order_dims as _order_dims,
    classify_category as classify_category,
    estimate_pack as estimate_pack,
)
from app.publish.persistence import (
    _JS_CLICK_PUBLISH_NOW as _JS_CLICK_PUBLISH_NOW,
    _JS_CONFIRM_PUBLISH_MODAL as _JS_CONFIRM_PUBLISH_MODAL,
    _JS_LIST_HAS_ROW as _JS_LIST_HAS_ROW,
    _JS_OPEN_PUBLISH_DROPDOWN as _JS_OPEN_PUBLISH_DROPDOWN,
    _JS_PUBLISH_FEEDBACK as _JS_PUBLISH_FEEDBACK,
    _publish_landed as _publish_landed,
    publish_now as publish_now,
)
from app.publish.saving import (
    _JS_CLICK_SAVE as _JS_CLICK_SAVE,
    _JS_CLOSE_FOREIGN_MODAL as _JS_CLOSE_FOREIGN_MODAL,
    _JS_CLOSE_SAVE_CONFIRM as _JS_CLOSE_SAVE_CONFIRM,
    _JS_RED_ANCHORS as _JS_RED_ANCHORS,
    _JS_SAVE_FEEDBACK as _JS_SAVE_FEEDBACK,
    _draft_update_time as _draft_update_time,
    save as save,
)
from app.publish.shipping import (
    _JS_CLICK_SHIPPING_RADIO as _JS_CLICK_SHIPPING_RADIO,
    _JS_FREIGHT_TPL_STATE as _JS_FREIGHT_TPL_STATE,
    _JS_OPEN_FREIGHT_TPL as _JS_OPEN_FREIGHT_TPL,
    _JS_PICK_FIRST_TPL as _JS_PICK_FIRST_TPL,
    _JS_SCROLL_FREIGHT_TPL as _JS_SCROLL_FREIGHT_TPL,
    _JS_SHIPPING_RADIOS as _JS_SHIPPING_RADIOS,
    _longest_deadline as _longest_deadline,
    set_shipping as set_shipping,
)
from app.publish.size_rules import (
    _ADULT_SIZE_TOKENS as _ADULT_SIZE_TOKENS,
    _AGE_ATTR_KEYS as _AGE_ATTR_KEYS,
    _LETTER_ORDER as _LETTER_ORDER,
    _RE_AGE_RANGE as _RE_AGE_RANGE,
    _RE_HEIGHT_RANGE as _RE_HEIGHT_RANGE,
    _RE_MONTH_SIZE as _RE_MONTH_SIZE,
    _RE_YEAR_SIZE as _RE_YEAR_SIZE,
    _SIZE_ALIASES as _SIZE_ALIASES,
    _age_is_child as _age_is_child,
    _age_to_height as _age_to_height,
    _has_older_than_one_year as _has_older_than_one_year,
    _map_letter_sizes as _map_letter_sizes,
    _page_height_codes as _page_height_codes,
    _parse_age_range as _parse_age_range,
    _parse_discrete_heights as _parse_discrete_heights,
    _parse_height_range as _parse_height_range,
    _slope_ok as _slope_ok,
    _sort_letter_sizes as _sort_letter_sizes,
    norm_size as norm_size,
    size_tier as size_tier,
)
from app.publish.sizechart.editor import _sc_js as _sc_js, add_sizechart as add_sizechart
from app.publish.sizechart.measurements import (
    _NONAPPAREL_KIND as _NONAPPAREL_KIND,
    _check_measurements as _check_measurements,
    _estimate_measurements as _estimate_measurements,
    _guess_size_kind as _guess_size_kind,
)
from app.publish.sizechart.parameters import (
    _PARAM_MAP_PROMPT as _PARAM_MAP_PROMPT,
    _PARAM_PICK_PROMPT as _PARAM_PICK_PROMPT,
    _PARAM_STEM_KEEP as _PARAM_STEM_KEEP,
    _PARAM_SYNONYMS as _PARAM_SYNONYMS,
    _RE_PARAM_SUFFIX as _RE_PARAM_SUFFIX,
    _map_params_by_llm as _map_params_by_llm,
    _match_param as _match_param,
    _param_stem as _param_stem,
    _pick_params_by_llm as _pick_params_by_llm,
)
from app.publish.sizechart.parts import (
    _ACCESSORY_SIZE_CATEGORY as _ACCESSORY_SIZE_CATEGORY,
    _PART_MATCH_PROMPT as _PART_MATCH_PROMPT,
    _PART_SIZE_CATEGORY_KEYWORDS as _PART_SIZE_CATEGORY_KEYWORDS,
    _category_keyword_of as _category_keyword_of,
    _match_part_by_llm as _match_part_by_llm,
    _pick_part_measurements as _pick_part_measurements,
    _size_category_for as _size_category_for,
    _size_category_for_part as _size_category_for_part,
)
from app.publish.sizechart.scripts import (
    _JS_CLICK_SIZECHART_OK as _JS_CLICK_SIZECHART_OK,
    _JS_FILL_SIZECHART as _JS_FILL_SIZECHART,
    _JS_OPEN_SIZECHART_MODAL as _JS_OPEN_SIZECHART_MODAL,
    _JS_SET_SIZECHART_CAT as _JS_SET_SIZECHART_CAT,
    _JS_SET_SIZECHART_PARAMS as _JS_SET_SIZECHART_PARAMS,
    _JS_SIZECHART_AVAILABLE_PARAMS as _JS_SIZECHART_AVAILABLE_PARAMS,
    _JS_SIZECHART_LOCATE as _JS_SIZECHART_LOCATE,
    _JS_SIZECHART_PARAMS as _JS_SIZECHART_PARAMS,
    _JS_SIZECHART_STATE as _JS_SIZECHART_STATE,
)
from app.publish.sizes import fix_sizes as fix_sizes
from app.publish.sku_codes import (
    SKU_CODE_CONCURRENCY as SKU_CODE_CONCURRENCY,
    SKU_CODE_MAX as SKU_CODE_MAX,
    _JS_FILL_SKU_CODES as _JS_FILL_SKU_CODES,
    _JS_READ_SKU_CODES as _JS_READ_SKU_CODES,
    _SKU_TOKEN_RE as _SKU_TOKEN_RE,
    _translate_term as _translate_term,
    fix_sku_codes as fix_sku_codes,
    has_cjk as has_cjk,
    sku_token as sku_token,
)
from app.publish.stock import (
    _ROW_BATCH as _ROW_BATCH,
    _fill_rows_batched as _fill_rows_batched,
    resolve_warehouse as resolve_warehouse,
    set_stock as set_stock,
)
from app.publish.stock_scripts import (
    _JS_FILL_PACKING as _JS_FILL_PACKING,
    _JS_FILL_STOCK_CAT as _JS_FILL_STOCK_CAT,
    _JS_FILL_STOCK_ONLY as _JS_FILL_STOCK_ONLY,
    _JS_PICK_WAREHOUSE as _JS_PICK_WAREHOUSE,
    _JS_WH_STATE as _JS_WH_STATE,
)
from app.publish.titles import (
    _NON_BRAND_WORDS as _NON_BRAND_WORDS,
    _brand_words as _brand_words,
    _js_fill_by_label as _js_fill_by_label,
    _set_origin as _set_origin,
    _strip_dated as _strip_dated,
    generate_titles as generate_titles,
    set_titles as set_titles,
)
from app.publish.upload import upload_image as upload_image, upload_video as upload_video
from app.publish.variant_colors import (
    _JS_VARIANT_ROW_FILL as _JS_VARIANT_ROW_FILL,
    _drop_fake_variants as _drop_fake_variants,
    _identify_fake_colors as _identify_fake_colors,
    _norm_color_name as _norm_color_name,
    _raw_sku_count as _raw_sku_count,
    _strip_replay as _strip_replay,
    accessory_colors_from_rows as accessory_colors_from_rows,
    drop_accessory_colors as drop_accessory_colors,
)
from app.publish.variant_dom import (
    _JS_CLICK_SIZE_CB as _JS_CLICK_SIZE_CB,
    _JS_COLOR_GROUP_STATES as _JS_COLOR_GROUP_STATES,
    _JS_SIZE_GROUP_PRESENCE as _JS_SIZE_GROUP_PRESENCE,
    _JS_SIZE_GROUP_STATES as _JS_SIZE_GROUP_STATES,
    _JS_SKU_ROW_COUNT as _JS_SKU_ROW_COUNT,
    _JS_UNCHECK_COLOR as _JS_UNCHECK_COLOR,
)
from app.publish.variants import (
    DECLARE_PRICE_DEFAULT as DECLARE_PRICE_DEFAULT,
    _JS_FILL_VARIANT as _JS_FILL_VARIANT,
    normalize_declare_price as normalize_declare_price,
    set_variant as set_variant,
)
from typing import Optional as Optional
