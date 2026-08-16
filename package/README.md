# 打包工具集

把项目打成自包含绿色版，目标是拷到新电脑解压后能直接跑采集管线。

## 目录内容

| 文件 | 作用 |
|------|------|
| [build.ps1](build.ps1) | 打包主脚本 |
| [manus.spec](manus.spec) | PyInstaller 配置（三入口共用一套依赖） |
| [verify_build.py](verify_build.py) | 产物完整性机检，构建后自动调用 |
| [runtime/](runtime/) | 随包分发的运行期脚本，构建时拷到 exe 同级 |

## 打包

```powershell
package\build.ps1 -Clean -Version "1.0.0"
```

约需 6-8 分钟。产出：

```
C:\manus-build\dist\manus-gui\     分发目录（约 494 MB）
C:\manus-build\manus-gui-1.0.0.zip 分发压缩包（约 242 MB）
```

可选参数：

- `-IncludeBrowsers` 额外内置 Playwright 浏览器（+520MB）。**采集管线不需要**——三条管线全部走 `connect_over_cdp` 接管用户已登录的真实 Chrome，不自己启动浏览器。只有 agent 模式自行开浏览器时才用得上。
- `-SkipZip` 只出目录不压缩
- `-BuildRoot <路径>` 换构建输出位置

## 为什么构建输出不落在项目目录

**本机装有会改写新建 exe 的安全软件**。凡是写进桌面项目树的可执行文件都会被：

- 改成 GUI 子系统 → `sys.stdout`/`sys.stderr` 变成 None，所有控制台输出（含异常回溯）静默消失
- 注入伪造版本资源（`ProductName=RuntimeBroker`）
- 严重时直接剥掉追加在 PE 尾部的 PKG 载荷 → exe 变成空壳

而整个过程 PyInstaller 一声不吭、退出码依然是 0，只在运行时表现为「进程秒退、零输出」，极难定位。

对照实验：同一个脚本、同一条命令，建到 `%TEMP%` 是干净的 CONSOLE 程序（1.61MB）；建到桌面项目目录就变成 GUI、体积撑到 4.01MB、`ProductName` 变成 `RuntimeBroker`。干净 exe 复制进桌面目录的瞬间也会被改写（SHA256 变化）。

所以输出隔离到 `C:\manus-build`（实测不受影响），且构建后必须机检：

```powershell
python package\verify_build.py C:\manus-build\dist\manus-gui
```

三项判据：CONSOLE 子系统、overlay 不为空、无非预期版本资源。不通过就别分发。

**若换机器打包**：先确认目标输出目录没有这类干预，或把它加入安全软件白名单。

## 为什么产出 ZIP 而不是安装程序

1. ZIP 里的 exe 在分发途中不会被安全软件改写
2. 解压即用，装到哪都行，避开 `C:\Program Files` 只读引发的一连串权限问题

## 冻结产物的路径约定

冻结后有三个根（见 [app/config.py](../app/config.py)）：

| 根 | 冻结态 | 开发态 | 用途 |
|----|--------|--------|------|
| `BUNDLE_ROOT` | `_internal/`（`sys._MEIPASS`） | 项目根 | 只读随包资源：templates、static、示例配置 |
| `PROJECT_ROOT` | exe 所在目录 | 项目根 | 安装位置 |
| `DATA_ROOT` | exe 目录（可写时）否则 `%LOCALAPPDATA%\ManusGUI` | 项目根 | 可写数据：config.toml、workspace、logs、experience |

开发态三者同为项目根，行为与改造前一致。

`DATA_ROOT` 会真去写一个探针文件来判断可写性——Windows 上只看 `os.access` 不可靠（UAC 虚拟化、ACL 继承都会骗过它）。装到 Program Files 时自动降级，避免 [app/logger.py](../app/logger.py) 在 import 期建日志文件失败导致三个 exe 一起闪退。

可用 `MANUS_DATA_DIR` 强制指定数据目录。

## spec 里的两个坑

**`__file__` 在 spec 中不存在**。spec 由 PyInstaller 以 `exec()` 执行，没有模块上下文。用它注入的 `SPECPATH`；又因 spec 放在 `package/` 下，项目根要取父目录：

```python
ROOT = Path(SPECPATH).parent
```

**`excludes` 的取舍**。`app/tool/__init__.py` 顶层导入 `Crawl4aiTool`，而 `crawl4ai.py` 在 `execute()` 里 `from crawl4ai import ...`；PyInstaller 的静态分析**会跟进函数体内的 import**，于是把 crawl4ai → litellm → nltk → torch → datasets 整条链拖进来。后果是 `import_library('datasets')` 在隔离子进程里于 `pyarrow\arrow.dll` 访问违例（`0xC0000005`）直接打断构建，外加数 GB 无用体积。

这些包全项目无直接 import，Manus 的 `available_tools` 也没注册 `Crawl4aiTool`，故排除。

但 **`langchain_core` 不能排除**——`browser_use` 顶层就 import 它，排掉会让三个 exe 全部在启动阶段 `ModuleNotFoundError`。

`faiss` 对 torch/scipy 的引用只在未被 `__init__` 导入的 `contrib/` 里；numpy/pandas 对 matplotlib/scipy/sklearn 的引用都是函数内懒加载，均可安全排除。

## PowerShell 注意

- 脚本必须存为 **UTF-8 with BOM**。Windows PowerShell 5.1 读无 BOM 的 `.ps1` 时按系统 ANSI 解码，中文注释变乱码，行尾中文还会吞掉换行符导致解析错误。
- 不要用 PowerShell 的 `> log 2>&1` 接原生命令。PyInstaller 把进度打在 stderr，5.1 会把每行包装成 `NativeCommandError`，配合 `$ErrorActionPreference='Stop'` 会在第一行日志出现时就抛异常中断构建。build.ps1 改走 `cmd /c` 重定向。

## 已验证项

在全新解压位置（模拟新电脑）实测通过：

- 三个 exe 均为 CONSOLE 子系统、overlay 完整，解压后仍完好
- `manus-cli.exe --help`、`manus-batch.exe --help` 中文输出正常
- Web 界面：首页 / `/collect` / `/orders` / `/static/*` 全部 200
- 采集入口成功连上 CDP 并进入业务逻辑
- `logs/`、`config.toml` 落在 exe 同级而非 `_internal`
- `MANUS_DATA_DIR` 覆盖生效
- 全部 521 项非 sandbox 单测通过（sandbox 那批因本机未运行 Docker 而失败，与打包无关）
