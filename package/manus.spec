# -*- mode: python ; coding: utf-8 -*-
# manus-gui PyInstaller 配置文件
# 使用：pyinstaller manus.spec

import sys
from pathlib import Path

block_cipher = None

# 项目根目录。
# spec 由 PyInstaller 以 exec() 执行，没有模块上下文，因此 __file__ 不存在；
# 可用的是 PyInstaller 注入的 SPECPATH（spec 所在目录）。
# 本 spec 放在 package/ 下，所以要取父目录才是项目根。
ROOT = Path(SPECPATH).parent

# 收集所有需要打包的数据文件
datas = [
    (str(ROOT / 'templates'), 'templates'),
    (str(ROOT / 'static'), 'static'),
    (str(ROOT / 'config' / 'config.example.toml'), 'config'),
    (str(ROOT / 'config' / 'mcp.example.json'), 'config'),
]

# 收集隐藏导入（PyInstaller 自动分析可能遗漏的模块）
hiddenimports = [
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'fastapi',
    'playwright',
    'browser_use',
    'faiss',
    'jieba',
    'rank_bm25',
    'PIL',
    'pyautogui',
    'pyperclip',
    'boto3',
    'botocore',
    'mcp',
    'openai',
    'tenacity',
    'loguru',
    'pydantic',
    'jinja2',
    'structlog',
]

# 排除的模块。
# app/tool/__init__.py 顶层导入 Crawl4aiTool，而 crawl4ai.py 在 execute() 里
# `from crawl4ai import ...`；PyInstaller 静态分析会跟进函数体内的 import，
# 于是把 crawl4ai -> litellm -> langchain -> nltk -> torch -> datasets 整条链拖进来。
# 后果有二：一是 import_library('datasets') 在隔离子进程里访问违例（0xC0000005）
# 直接打断构建，二是白白多出数 GB 体积。
# 这些包全项目无任何直接 import，且 Manus agent 的 available_tools 里没有注册
# Crawl4aiTool；crawl4ai.py 本身也已对 ImportError 兜底返回可读错误，
# 所以排除后不影响实际功能路径。
# 注意：langchain_core 不能排除——browser_use 顶层就 import 它
# （browser_use/agent/prompts.py），排掉会让三个 exe 全部在启动阶段
# ModuleNotFoundError。litellm 可以排：扫过 browser_use 全部源码，它不依赖 litellm。
excludes = [
    'crawl4ai',
    'litellm',
    'datasets',
    'torch',
    'torchvision',
    'torchaudio',
    'transformers',
    'sentence_transformers',
    'nltk',
    'sklearn',
    'scipy',
    'browsergym',
    'gymnasium',
    'tensorboard',
    'matplotlib',
]

# Web 入口分析（app.py）
a_web = Analysis(
    [str(ROOT / 'app.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# CLI 入口分析（main.py）
a_cli = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# 批量采集入口分析（batch_collect.py）
a_batch = Analysis(
    [str(ROOT / 'batch_collect.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# 这里刻意不用 MERGE。
# MERGE 是给「每个 app 各占一个目录」的多包布局设计的：它把共享依赖只留在第一个
# app 目录里，其余 app 的对应条目换成指向 `manus-web/...` 的 DEPENDENCY 引用。
# 而本 spec 是三个 exe 放进同一个目录、共用同一套 _internal，那些跨目录引用无处可指。
# 单个 COLLECT 本身就按目标路径去重，重复依赖不会被打包两份，MERGE 在此无收益。

# Web 可执行文件
pyz_web = PYZ(a_web.pure, a_web.zipped_data, cipher=block_cipher)
exe_web = EXE(
    pyz_web,
    a_web.scripts,
    [],
    exclude_binaries=True,
    name='manus-web',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # 保留控制台以显示日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# CLI 可执行文件
pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data, cipher=block_cipher)
exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name='manus-cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# 批量采集可执行文件
pyz_batch = PYZ(a_batch.pure, a_batch.zipped_data, cipher=block_cipher)
exe_batch = EXE(
    pyz_batch,
    a_batch.scripts,
    [],
    exclude_binaries=True,
    name='manus-batch',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# 收集所有文件到 dist 目录
coll = COLLECT(
    exe_web,
    a_web.binaries,
    a_web.zipfiles,
    a_web.datas,
    exe_cli,
    a_cli.binaries,
    a_cli.zipfiles,
    a_cli.datas,
    exe_batch,
    a_batch.binaries,
    a_batch.zipfiles,
    a_batch.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='manus-gui',
)
