# Pine → Python 批量移植规范

把语料库里的 Pine 脚本变成可回测的 Python 策略。**agent 负责判断，工具负责把关。**

## 为什么指标也能变成策略

`study`（指标）没有入场/出场规则，但它**计算的东西**可以定义规则。这是标准做法，
不是凑数：布林带指标 → 触及下轨做多；RSI 指标 → 超卖回升做多。
关键是**包装器要事先约定、事后记录**，不能每个脚本各拍一次脑袋。

## 七个标准包装器

| 家族 | 典型指标 | 包装器（→ 策略） | 出场 |
|---|---|---|---|
| **oscillator** | RSI / Stochastic / CCI / Williams %R / MFI | 阈值穿越：上穿超卖线做多、下穿超买线做空；或零轴穿越 | 反向穿越 / ATR 跟踪 |
| **bands** | Bollinger / Keltner / Donchian / Envelope | 二选一：(a) 触带反转 (b) 带外突破 | 回中轨 / 对侧带 / ATR |
| **trend** | MA cross / SuperTrend / PSAR / Ribbon / VWAP | 方向翻转：空转多做多 | 再次翻转 / ATR 跟踪 |
| **level** | Pivot / S&R / Fib / Value Area | 收盘穿越关键位 | 回到位下方 / ATR |
| **volatility** | ATR / BB width / Squeeze | **过滤器**：波动扩张才允许交易；或挤压后突破 | 同被包装的主策略 |
| **volume** | Volume / OBV / OI / Delta | **确认过滤器**：放量/增仓才入场 | 同主策略 |
| **pattern** | Engulfing / Fractal / Divergence | 形态确认当根收盘入场 | 形态失效点 / ATR |

规则：

1. **原生 `strategy()` 脚本优先**：直接用它的 `strategy.entry` 条件，不要另创包装器。
2. **`volatility` / `volume` 通常不能单独成策略**，要叠加在 `trend` / `bands` / `pattern` 上。
3. 包装器的选择必须写进策略的 `notes=` 与移植台账，方便日后按包装器聚合复盘。
4. 同一个指标族用**同一个**包装器口径，这样策略之间的比较才有意义。

## 硬要求

1. **只能用 `<= t` 的数据。** 重绘写法必须按已确认口径改写：

   | Pine 写法 | 必须改成 |
   |---|---|
   | `security(..., lookahead=barmerge.lookahead_on)` | 去掉 lookahead；或用 `close[1]` 取上一根已收盘值 |
   | `ta.pivothigh(n, n)` 当根判断 | 右移 n 根后再判断（第 t 根才知道第 t-n 根是枢轴） |
   | `request.security` 取高周期 close | 按高周期**已收盘**的那根取值 |
   | `barstate.isrealtime` / `varip` | 回测语义不同，改用确定性等价写法或放弃移植 |

2. **只输出信号列**：`sig` / `stop_px` / `sig_tag`。撮合、成本、时段、T+N
   全部由 `engine.py` 负责 —— 策略里算钱的话，每个策略的成本口径都会不一样。
3. **`defaults` 列全参数**：策略读到的每个 key 都要在 `defaults` 里有默认值。
4. **保留署名与许可**：`source=` / `license=` 写原作者的许可（CC BY-NC-SA / MPL / MIT）。
5. **`source_sid=` 填语料库脚本 id**，保证可追溯。

## 机械校验（唯一不可跳过的环节）

```bash
python -m alpharadar.porting.verify --strategy <key> --real P.DCE:5min
```

四道闸门，任一不过就**拒绝入库**，不进回测队列：

| 闸门 | 查什么 | 为什么 |
|---|---|---|
| **无未来函数** | 在 `bars[:k]` 上算的信号，必须与全量结果的前 k 个完全一致 | 这是决定性的：任何用到未来数据的实现都会在这里暴露 |
| **信号健全** | 取值只能是 -1/0/1；不能全 0；不能 >50% 的 bar 都触发；结构止损必须有限 | 全 0 = 包装器选错；每根都触发 = 退化成"永远在场" |
| **可复现** | 同输入跑两次结果一致 | 排除随机数 / 时间依赖 |
| **预热纪律** | 前 `min_bars` 根不得出信号 | 指标未成形的信号是噪音（已由 `signal_frame` 中央保证） |

**形态类策略（冰点反转、假突破、吞没）在合成数据上信号可能过少**，必须加
`--real` 用真实行情复核 —— 合成的均匀随机游走覆盖不到那些形态。

## 完整流程

```bash
# 1) 分诊：给全部脚本打分、分类、检测风险
python -m alpharadar.porting.triage

# 2) 取工单（含 Pine 源码 + 建议包装器 + Python 模板 + 风险清单）
python -m alpharadar.porting.scaffold --next

# 3) agent 读工单，在 strategies/library.py 里写实现（用 @register）

# 4) 机械校验
python -m alpharadar.porting.verify --strategy <key> --real P.DCE:5min

# 5) 过关后同步任务队列，全市场自动开跑
python -m alpharadar.worker --sync --minutes 0.1
```

## 移植台账

`ports` 表记录每个脚本的状态：

| 状态 | 含义 |
|---|---|
| `pending` | 待移植 |
| `ported` | 已派工单（进行中） |
| `verified` | 通过全部闸门，已进回测队列 |
| `rejected` | 校验未过，`notes` 里记原因 |

看板 <https://alpha-radar.infiniti.website/runs> 会显示各状态计数与家族分布。
