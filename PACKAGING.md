# 打包说明

打包工具在 [package/](package/) 目录。

```powershell
package\build.ps1 -Clean -Version "1.0.0"
```

产出（**刻意不落在项目目录**，原因见下）：

```
C:\manus-build\dist\manus-gui\        分发目录（约 494 MB）
C:\manus-build\manus-gui-1.0.0.zip    分发压缩包（约 242 MB）
```

详细说明：[package/README.md](package/README.md)

## 两件必须知道的事

**1. 构建输出不能落在本项目目录。** 本机装有会改写新建 exe 的安全软件，写进桌面项目树的可执行文件会被改成 GUI 子系统、注入伪造版本资源、甚至被剥掉 PKG 载荷，而 PyInstaller 退出码仍是 0，只在运行时表现为「进程秒退、零输出」。故输出隔离到 `C:\manus-build`，并在构建后机检（`package/verify_build.py`），不通过即中止。

**2. 采集管线需要调试端口 Chrome。** 采集/订单/活动三条管线都不自己开浏览器，而是通过 CDP 接管用户已登录的真实 Chrome（`localhost:9222`）。分发包里带了 `启动调试Chrome.ps1`，跑管线前必须先用它启动 Chrome。

## 冻结产物的路径约定

| 根 | 冻结态 | 用途 |
|----|--------|------|
| `BUNDLE_ROOT` | `_internal/` | 只读随包资源（templates/static/示例配置） |
| `PROJECT_ROOT` | exe 所在目录 | 安装位置 |
| `DATA_ROOT` | exe 目录（可写时），否则 `%LOCALAPPDATA%\ManusGUI` | 可写数据（config.toml/workspace/logs/experience） |

开发态三者同为项目根，行为与改造前一致。见 [app/config.py](app/config.py)。
