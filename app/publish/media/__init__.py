"""店小秘图片与视频操作。"""

from .materials import set_material
from .skc import skc_replace_row
from .preview import sku_preview_replace_row, sku_preview_state
from .description import desc_map, desc_delete, desc_save, desc_text_apply, desc_text_map, desc_text_delete_all, ensure_desc_closed
from .description_replace import desc_replace
from .video import set_video, delete_video, read_video_url
