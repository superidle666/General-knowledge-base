"""
项目日志配置。
tool/logger.py
提供项目统一的彩色控制台日志格式，并暴露问答侧共用的 logger 句柄。
日志处理器只由进程入口（API、导入 Worker、节点 __main__ 脚本）显式
              挂载，模块导入期绝不改动根日志器——此前这里在 import 期 clear() 根
              处理器并挂上自己的 handler，与导入侧 setup_logging 互相覆盖，
              "谁最后 import 谁赢"，是日志格式随机、问答侧看到导入侧 handler 的根因。
"""

import logging

import colorlog

# 问答侧与 utils 共用命名空间，便于宿主进程按名字整体调级或屏蔽，
# 不再直接返回根日志器（根日志器属于宿主，不属于本库）。
LOGGER_NAME = "kb"
logger = logging.getLogger(LOGGER_NAME)

# 标记位用于幂等判定：重复调用 configure_logging 只调级别，不会叠加第二个 handler。
_CONSOLE_HANDLER_MARK = "_knowledge_base_console_handler"

_LOG_COLORS = {
    "DEBUG": "cyan",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "bold_red",
}
_LOG_FORMAT = "%(log_color)s%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def build_console_handler(level: int = logging.INFO) -> logging.Handler:
    """构造项目统一的彩色控制台 handler（带幂等标记）。"""
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S", log_colors=_LOG_COLORS))
    handler.setLevel(level)
    setattr(handler, _CONSOLE_HANDLER_MARK, True)
    return handler


def configure_logging(level: int = logging.INFO) -> logging.Handler:
    """给根日志器挂上项目控制台输出，幂等且从不移除他人已有的 handler。

    由进程入口显式调用一次即可；重复调用只会调整已有 handler 的级别。

    :param level: 项目控制台的日志级别
    :return: 项目中使用的控制台 handler
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, _CONSOLE_HANDLER_MARK, False):
            handler.setLevel(level)
            return handler

    # 根日志器出厂默认就是 WARNING：不把它降到 INFO，本项目的运行日志会全部
    # 静默丢弃（看不到日志比格式乱更难排查）；但宿主已经配过根日志器时不抢方向盘。
    handler = build_console_handler(level)
    root.addHandler(handler)
    _adopt_logging_level(root, level)
    return handler


def _adopt_logging_level(root: logging.Logger, level: int) -> None:
    """保证项目自己的日志可见，同时不抢宿主对根日志器的方向盘。

    两层处理：
    1 本项目命名空间（kb / import.*）无条件设为目标级别——项目 INFO 是运维排查的主通道，
      不能被第三方库在导入时调过 basicConfig 后的 root=WARNING 静默门掉；
    2 根日志器只在没人配过时降级。root.level 无法区分“Python 默认 WARNING”和
      “宿主显式调成 WARNING”，所以用“root 上有没有别人的 handler”代理“是否配过”。
    """
    for namespace in (LOGGER_NAME, "import"):
        logging.getLogger(namespace).setLevel(level)

    host_configured = any(
        not getattr(handler, _CONSOLE_HANDLER_MARK, False) for handler in root.handlers
    )
    if not host_configured:
        root.setLevel(level)


def reset_logging() -> None:
    """移除本项目挂载的全部 handler，保留宿主进程和其他库的。

    供测试清理和"重新配置日志"场景使用，避免用例之间残留控制台输出。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _CONSOLE_HANDLER_MARK, False):
            root.removeHandler(handler)
