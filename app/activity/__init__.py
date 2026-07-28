"""活动管理管线包（Temu 商家后台：关流量加速器 → 报活动 → 重开加速器）。

对标 app/collect：pipeline.py 是确定性单商品例程 + LLM 判断点，service.py 是
UI/CLI 共用编排 + 结构化进度事件。阶段1 只做【只读 + dry-run】：变更动作（关加速/
报名/开加速）一律留桩，绝不点任何变更按钮，见 pipeline.py 里的桩函数与 service.py
的 dry-run 分支。
"""
