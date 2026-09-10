"""按来源选择店小秘发布流程；未知来源不自动视作 1688。"""

from importlib import import_module

from app.publish.sources.base import UnsupportedSourceError, detect_platform
from .base import PublishWorkflow, SourceMismatchError, identity

_MODULES = {
    "1688": ("alibaba1688", "1688商品店小秘发布"),
    "pdd": ("pinduoduo", "拼多多商品店小秘发布"),
    "temu": ("temu", "Temu商品店小秘发布"),
    "amazon": ("amazon", "亚马逊商品店小秘发布"),
}


def get_workflow(platform):
    if platform not in _MODULES:
        raise UnsupportedSourceError(f"没有 {platform!r} 的店小秘发布流程")
    module, name = _MODULES[platform]
    return PublishWorkflow(platform, name, import_module(f"{__name__}.{module}"))


def resolve_workflow(task, state=None, info=None):
    state = state or {}
    info = info or {}
    candidates = [task.get("source_platform"), state.get("source_platform"),
                  identity(info)[0]]
    if task.get("url"):
        candidates.append(detect_platform(task["url"]))
    platforms = {platform for platform in candidates if platform}
    if len(platforms) > 1:
        raise SourceMismatchError(f"任务、断点与商品数据来源冲突：{sorted(platforms)}")
    if not platforms:
        return None
    workflow = get_workflow(platforms.pop())
    if state.get("workflow_id") not in (None, "", workflow.workflow_id):
        raise SourceMismatchError("断点属于另一条发布流程")
    workflow.validate_info(info, task.get("url") or "")
    return workflow


def composition_for(info):
    platform, _ = identity(info)
    if not platform:
        return {}
    return get_workflow(platform).rules.parse_composition(info.get("attributes") or {})


def rules_for(info):
    platform, _ = identity(info or {})
    return get_workflow(platform).rules if platform else None


def source_name(info):
    from app.publish.sources.base import platform_name

    platform, _ = identity(info or {})
    return platform_name(platform)


def size_normalizer(info):
    from app.publish.size_rules import norm_size

    rules = rules_for(info)
    return rules.normalize_size if rules else norm_size
