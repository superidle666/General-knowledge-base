"""
全局日志配置。

@FilePath: tool/logger.py
@Date: 2026-09-07
@Description: 创建带颜色格式的根日志记录器，统一输出处理流程运行信息。
"""

import logging

import colorlog

logger = logging.getLogger()
logger.setLevel(logging.INFO)

handler = colorlog.StreamHandler()
handler.setFormatter(colorlog.ColoredFormatter(
    '%(log_color)s%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    log_colors={
        'DEBUG': 'cyan',
        'INFO': 'green',  # INFO 显示为绿色
        'WARNING': 'yellow',
        'ERROR': 'red',
        'CRITICAL': 'bold_red',
    }
))

logger.handlers.clear()
logger.addHandler(handler)
