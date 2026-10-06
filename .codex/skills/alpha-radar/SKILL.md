---
name: alpha-radar
description: 在 alpha-radar 仓库里持续发现、移植并回测 TradingView 开源策略（A 股 / 期货，Tushare 数据源）。当用户要求"找策略""挖策略""回测某个 Pine 策略""扫描 TradingView""验证某个品种上有没有可交易的日内策略""扩充策略库"时使用。
---

# alpha-radar：策略雷达

## 什么时候用

用户提出以下任一意图时启用：

- 找 / 挖 / 筛选交易策略（尤其提到 TradingView、Pine、开源策略）
- 把某个 Pine 策略移植过来并在 A 股或期货上回测
- 检验某个品种、某个周期上"有没有可交易的边际"
- 持续扩充策略库、跑策略排行榜

## 执行流程

先读仓库根目录的 `AGENTS.md`（铁律与判定标准），再按下面的循环执行。**不要跳过
含成本回测，也不要只报总收益。**

### 1. 采集与选题

```bash
alpharadar harvest --max-fetch 300      # 更新语料库
alpharadar corpus -n 50                 # 看点赞最高的开源脚本
```

在 `corpus/sources/` 里读候选的 Pine 源码。筛选原则：

- **可机械化**：能用规则说清楚（均线、ATR、区间、量能…）。
  主观概念（订单块、流动性、SMC）默认跳过，除非能给出无歧义定义。
- **有出场逻辑**：只有入场没有出场的指标，需要自己补一套出场再测。
- **非重绘**：`security(..., lookahead=barmerge.lookahead_on)`、
  未确认的 `pivothigh` 都是重绘陷阱，移植时要改成已确认口径。

### 2. 移植

在 `alpharadar/strategies/library.py` 里用 `@register` 注册：

```python
@register("my_strategy", "策略名", source="TradingView @作者", license="MPL-2.0",
          defaults={...})
def _my_strategy(df, p):
    ...
    return signal_frame(df, sig, stop=stop, tag=tag)
```

只输出信号，不要写撮合逻辑。

### 3. 回测与筛选

先用 `matrix` 横扫，再对入围者用 `run` 深挖：

```bash
alpharadar matrix --symbols P.DCE,Y.DCE,M.DCE --strategies utbot,orb \
                  --freqs 1min,5min,15min --start 20220101
alpharadar run --symbol P.DCE --strategy utbot --freq 5min --start 20220101
```

按 `AGENTS.md` 的五条判定标准逐条卡：成本/振幅比 → 样本量 → 分年 → 收益回撤比 →
参数邻域稳健性。

### 4. 记录（必做）

把结论写进两处：

- 策略的 `notes=`（一句话结论，含关键数字）
- `docs/findings.md`（完整记录：数据集、参数、分年表、失败原因）

**失败的结论同样要写。** 本仓库最有价值的部分是"什么不行、为什么不行"。

## 输出给用户时的口径

- 先给结论（能/不能交易），再给证据（笔数、PF、均点、分年、回撤）。
- 主动声明搜索规模与乐观偏差。
- 主动指出样本不足、单年依赖、成本敏感等弱点。
- 不要用"圣杯""稳赚""必赚"这类词；不要承诺收益。
