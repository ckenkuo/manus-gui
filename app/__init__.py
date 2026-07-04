# Python version check: 3.11-3.13
import os
import sys


# Windows 上 faiss/MKL 自带的 Intel OpenMP（libiomp5md）与其他库的 LLVM OpenMP
# （libomp140）会双双链入同一进程，触发 OMP: Error #15 并中止。这里在任何
# OpenMP 相关库（numpy/faiss）导入前放行重复运行时。个人级小规模检索，
# 单线程点积为主，重复运行时带来的性能/正确性风险可忽略。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


if sys.version_info < (3, 11) or sys.version_info > (3, 13):
    print(
        "Warning: Unsupported Python version {ver}, please use 3.11-3.13".format(
            ver=".".join(map(str, sys.version_info))
        )
    )
