"""店小秘发布共用能力：stages.results。各来源流程由 workflows/ 独立定义。"""


_DONE = ("ok", "skipped")  # 阶段终态里算「完成」的（skipped = 本商品无需该阶段）
