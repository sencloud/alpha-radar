"""最小示例：取一段棕榈油 5 分钟数据，跑一个策略，打印成绩并出 HTML 报告。

运行：
    python examples/quickstart.py
（需要 .env 里的 TUSHARE_TOKEN，且账号有 ft_mins 权限）
"""

from alpharadar.metrics import render_text
from alpharadar.pipeline import run_one, save_result


def main() -> None:
    res = run_one(symbol="P.DCE", strategy="utbot", freq="5min",
                  start="20220101", params={"use_regime": 1, "er_min": 0.25,
                                            "atr_ratio_min": 0.0})
    print(render_text(res))
    trades_csv, html = save_result(res)
    print(f"\n逐笔明细：{trades_csv}\nHTML 报告：{html}")


if __name__ == "__main__":
    main()
