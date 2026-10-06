# AGENTS.md — alpha-radar 的 agent 工作手册

本文件是 Codex（harness）在本仓库工作时的操作约定。运行模型：**Codex harness +
deepseek-flash**。人只给方向，agent 负责跑完整闭环：发现 → 评估 → 移植 → 回测 → 记录。

## 这个仓库在做什么

持续从 TradingView 采集**开源** Pine 策略，把有希望的在 A 股 / 期货上做**含成本**的
回测检验，并把结论沉淀成可复现的记录。目标是「批量证伪」，不是「批量发策略」。

## 铁律（违反即视为任务失败）

1. **没有含成本的回测，不许说有效**。滑点默认 1 跳/边，A 股含印花税与最低佣金。
2. **必须给分年结果**。只报总收益的策略一律不算数 —— 本仓库的历史结论是：
   几乎所有"有效"都来自单一年份（2022 印尼出口禁令年）。
3. **必须声明搜索规模**。一次任务跑了多少组参数要说清楚；搜索越广，
   样本内最优值的乐观偏差越大。
4. **不许用未来函数**。指标只用 `<= t` 的数据；移植 Pine 时尤其注意
   `request.security` / `security()` 的 lookahead、`pivothigh` 的确认延迟。
5. **不许把闭源脚本"猜出来"**。`scriptAccess` 不是 open 的脚本直接跳过；
   移植的脚本在文件头保留原作者与许可（CC BY-NC-SA / MPL / MIT）。
6. **样本量与正年数一起报**。少于 30 笔的"高 PF"必须显式标注样本不足。
7. **不许为了好看而调参**。参数一旦被用来挑结果，它就是样本内参数；
   定稿参数必须报 OOS 或至少分年结构。

## 标准循环

```
1. 采集   alpharadar harvest                     # 更新语料库 corpus/
2. 选题   alpharadar corpus -n 50                # 按点赞/类型挑候选
          读 corpus/sources/*.pine，判断是否可机械化为规则
3. 移植   在 alpharadar/strategies/library.py 注册新策略
          （用 @register，标注 source / license / notes）
4. 回测   alpharadar matrix --symbols ... --strategies ... --freqs ...
5. 深挖   alpharadar run --symbol ... --strategy ... --freq 5min
          看分年、离场原因、平均止损/止盈、平衡胜率
6. 记录   把结论写回策略的 notes 与 docs/findings.md（含失败结论）
```

## 判定一个策略是否值得继续（按顺序卡）

1. **尺度**：成本 ÷ 该周期平均振幅 < 25%？棕榈油 1 分钟是 58%，直接淘汰。
2. **样本**：≥ 200 笔，且覆盖 ≥ 3 年。
3. **分年**：正年数 ≥ 4/5，或最近三年不亏。
4. **收益回撤比** ≥ 1.0（总盈亏 ÷ 最大回撤）。
5. **参数稳健**：最优参数附近的邻域不能塌方（单调或平台，不能是孤点）。

任何一条不过，就在 findings 里写清楚**为什么失败**，这比"找到一个能用的"更有价值。

## 常用命令

```bash
alpharadar harvest --max-fetch 300
alpharadar corpus -n 40
alpharadar list
alpharadar run    --symbol P.DCE --strategy utbot --freq 5min --start 20220101
alpharadar run    --symbol 600519.SH --strategy ema_cross --freq 1d --start 20180101
alpharadar matrix --symbols P.DCE,Y.DCE,M.DCE --strategies utbot,orb,vreversal \
                  --freqs 5min,15min --set use_regime=1 --set er_min=0.25
```

## 代码约定

- 策略只产出信号列（`sig` / `stop_px` / `st_stop` / `sig_tag`），
  撮合、成本、时段、T+1、统计全部由 `engine.py` 负责 —— 不要把交易规则写进策略。
- 新指标加到 `indicators.py`，新体制度量也放那里（`add_regime`）。
- A 股与期货的差异只允许出现在 `universe.py` 的 `Instrument` 里。
- 任何外部数据访问都走 `tushare_client.py`（自带限流与增量缓存），不要直接调 tushare。

## 交付物的默认形态

一次完整任务的产出应包含：
1. 语料库增量（corpus/）
2. 新增/修改的策略及其出处与许可
3. 一个 `matrix` 或 `run` 的结果，含**分年表**与**搜索规模声明**
4. 结论（写在策略 notes 与 docs/findings.md），失败也要写
