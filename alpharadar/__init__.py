"""alpha-radar：持续采集 TradingView 开源策略，在 A 股 / 期货上自动回测检验。

分层：
  data    —— Tushare 数据层（A 股 / 期货，日线与分钟线，带缓存与限流）
  harvest —— TradingView 开源策略采集（公开 HTTP 接口，非浏览器自动化）
  strategy—— 策略库（Pine 逻辑的 Python 移植）
  engine  —— 事件驱动回测引擎（成本、滑点、分批止盈、跟踪止损、T+1）
  report  —— 指标与报告
  pipeline—— 端到端编排，供 CLI 与 agent 调用

设计原则见 docs/design.md，反过拟合守则见 docs/pitfalls.md。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
