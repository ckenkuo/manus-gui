"""校验 PyInstaller 产物是否完好、是否被外部改写。

为什么需要这一步：本机装有会改写新建 exe 的安全软件，它会把 PyInstaller 产物
重写成 GUI 子系统、注入伪造的版本资源（ProductName=RuntimeBroker），
严重时直接剥掉追加在 PE 尾部的 PKG 载荷（overlay），让 exe 变成空壳。
这种损坏不会让构建报错——PyInstaller 退出码依然是 0——只有在运行时表现为
「进程秒退、零输出」，极难定位。所以每次构建后必须机检一遍，别靠肉眼看大小。

判定项：
  1. 子系统必须是 CONSOLE(3)。被改写后会变成 GUI(2)，届时 sys.stdout/stderr
     为 None，一切控制台输出（含异常回溯）静默丢失。
  2. overlay 必须存在且够大。PyInstaller 把打包好的 PKG 追加在 PE 结构之后，
     overlay 为 0 意味着载荷被剥离，exe 必然跑不起来。
  3. 不得出现非 PyInstaller 写入的版本资源。我们没传 version= 参数，
     正常产物不该有 ProductName/CompanyName。

用法：python verify_build.py <dist目录>
退出码 0 表示全部通过，非 0 表示存在问题。
"""

import os
import sys

try:
    import pefile
except ImportError:
    print("需要 pefile（随 PyInstaller 一并安装）：pip install pefile")
    sys.exit(2)

# overlay 至少应有这么大才算载荷完整。三个入口的 PKG 都在 20MB 以上，
# 取 1MB 只是为了区分「有载荷」与「被剥空」，不追求精确。
MIN_OVERLAY_BYTES = 1 * 1024 * 1024

SUBSYSTEM_NAMES = {2: "GUI", 3: "CONSOLE"}


def inspect(path: str) -> dict:
    """读取单个 exe 的关键 PE 特征。"""
    pe = pefile.PE(path)
    try:
        subsystem = pe.OPTIONAL_HEADER.Subsystem
        overlay_offset = pe.get_overlay_data_start_offset()
        total = os.path.getsize(path)
        overlay = (total - overlay_offset) if overlay_offset else 0

        version_strings = {}
        try:
            for file_info in pe.FileInfo[0]:
                if file_info.Key.decode() == "StringFileInfo":
                    for table in file_info.StringTable:
                        for key, value in table.entries.items():
                            version_strings[key.decode(errors="replace")] = value.decode(
                                errors="replace"
                            )
        except Exception:
            # 没有版本资源是正常情况，这里不该因缺失而报错
            pass

        return {
            "subsystem": subsystem,
            "overlay": overlay,
            "total": total,
            "version": version_strings,
        }
    finally:
        pe.close()


def check(path: str) -> list:
    """返回该 exe 的问题列表，空列表表示通过。"""
    info = inspect(path)
    problems = []

    if info["subsystem"] != 3:
        problems.append(
            "子系统是 %s(%d)，应为 CONSOLE(3)；控制台输出会被吞掉"
            % (SUBSYSTEM_NAMES.get(info["subsystem"], "?"), info["subsystem"])
        )

    if info["overlay"] < MIN_OVERLAY_BYTES:
        problems.append(
            "overlay 仅 %.2fMB，PKG 载荷疑似被剥离，exe 无法运行"
            % (info["overlay"] / 1048576.0)
        )

    if info["version"]:
        product = info["version"].get("ProductName", "")
        problems.append("存在非预期的版本资源（ProductName=%s），产物疑似被改写" % product)

    return problems


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    dist_dir = sys.argv[1]
    if not os.path.isdir(dist_dir):
        print("目录不存在: %s" % dist_dir)
        return 2

    exes = [
        os.path.join(dist_dir, name)
        for name in sorted(os.listdir(dist_dir))
        if name.lower().endswith(".exe")
    ]
    if not exes:
        print("目录下没有 exe: %s" % dist_dir)
        return 2

    failed = 0
    for path in exes:
        info = inspect(path)
        problems = check(path)
        status = "OK  " if not problems else "FAIL"
        print(
            "%s %-18s %s  总计%.2fMB  overlay%.2fMB"
            % (
                status,
                os.path.basename(path),
                SUBSYSTEM_NAMES.get(info["subsystem"], "?"),
                info["total"] / 1048576.0,
                info["overlay"] / 1048576.0,
            )
        )
        for problem in problems:
            print("       - %s" % problem)
        if problems:
            failed += 1

    print()
    if failed:
        print("校验未通过：%d/%d 个可执行文件存在问题。" % (failed, len(exes)))
        print("若问题是「被改写」，请把构建输出目录加入安全软件白名单后重新构建。")
        return 1

    print("校验通过：%d 个可执行文件均完好。" % len(exes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
