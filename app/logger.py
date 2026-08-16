import sys
from datetime import datetime

from loguru import logger as _logger

from app.config import DATA_ROOT


_print_level = "INFO"


def define_log_level(print_level="INFO", logfile_level="DEBUG", name: str = None):
    """将日志级别调整到指定级别"""
    global _print_level
    _print_level = print_level

    current_date = datetime.now()
    formatted_date = current_date.strftime("%Y%m%d%H%M%S")
    log_name = (
        f"{name}_{formatted_date}" if name else formatted_date
    )  # 使用前缀名称命名日志

    _logger.remove()
    _logger.add(sys.stderr, level=print_level)

    # 文件 sink 走可写数据根（冻结态可能是 %LOCALAPPDATA%），而不是安装目录。
    # 这里必须吞异常：本函数在模块级被调用（见文件末尾），而 app.logger 又被
    # 三个入口全部间接导入；一旦建目录/开文件抛错，异常发生在日志系统建立之前，
    # 用户只会看到窗口一闪而过、没有任何线索。落不了盘就只保留 stderr，
    # 让程序照常跑——符合本项目「辅助路径坏了不影响主流程」的取向。
    try:
        _logger.add(DATA_ROOT / f"logs/{log_name}.log", level=logfile_level)
    except Exception as exc:  # noqa: BLE001 - 落盘失败绝不能中断启动
        _logger.warning(f"日志文件无法写入（{exc}），本次仅输出到控制台。")

    return _logger


logger = define_log_level()


if __name__ == "__main__":
    logger.info("Starting application")
    logger.debug("Debug message")
    logger.warning("Warning message")
    logger.error("Error message")
    logger.critical("Critical message")

    try:
        raise ValueError("Test error")
    except Exception as e:
        logger.exception(f"An error occurred: {e}")
