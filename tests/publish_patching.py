"""旧集成测试的依赖替换：覆盖拆分前共享的绑定，避免真实网络副作用。"""

import importlib
import sys


def patch_publish(monkeypatch, facade, name, replacement, raising=True):
    module = importlib.import_module(f"app.publish.{facade}")
    missing = object()
    original = getattr(module, name, missing)
    if original is missing:
        monkeypatch.setattr(module, name, replacement, raising=raising)
        return
    targets = [candidate for key, candidate in list(sys.modules.items())
               if key.startswith("app.publish.") and candidate is not None
               and vars(candidate).get(name, missing) is original]
    for target in targets:
        monkeypatch.setattr(target, name, replacement)
