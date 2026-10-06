"""策略库：TradingView Pine 逻辑的 Python 移植 + 原创策略。"""

import importlib.util
import logging
import sys

from ..config import DATA_DIR
from .base import (REGISTRY, Strategy, get, list_strategies,      # noqa: F401
                   register, signal_frame)
from . import library                                              # noqa: F401

# 自动移植生成的策略放在 data/ 下，**不在源码树里** ——
# 源码树每次部署都会被覆盖，生成物放进去会被清掉（踩过：两个已通过校验的
# 策略被一次部署冲没了）。data/ 是部署排除目录且服务可写，才是它该在的地方。
GENERATED = DATA_DIR / "generated_strategies.py"


def load_generated() -> int:
    """导入 data/generated_strategies.py 里的策略，返回本次注册数量。

    语法错/运行错都不影响主库：捕获后记日志，继续用内置策略跑。
    """
    if not GENERATED.exists():
        return 0
    before = len(REGISTRY)
    try:
        spec = importlib.util.spec_from_file_location("alpharadar_generated", GENERATED)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["alpharadar_generated"] = mod
        spec.loader.exec_module(mod)
    except Exception:
        logging.getLogger(__name__).warning("auto-ported 策略加载失败，已忽略",
                                            exc_info=True)
    return len(REGISTRY) - before


load_generated()

__all__ = ["REGISTRY", "Strategy", "get", "list_strategies", "register",
           "signal_frame", "library"]
