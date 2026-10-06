---
name: pine-port
description: 把 TradingView 的 Pine 脚本批量翻译成可回测的 Python 策略（含指标→策略的包装）。当用户要求"批量翻译 Pine""把采到的策略跑回测""移植指标""扩充策略库""让语料库里的脚本都能回测"时使用。
---

# Pine 批量移植

把语料库里的 Pine 脚本变成能进回测队列的 Python 策略。完整规范见
[`docs/porting.md`](../../../docs/porting.md)，先读它。

## 核心原则

**agent 负责判断（这个指标该怎么变成策略），工具负责把关（翻译得对不对）。**

不要试图"直译后直接回测"——Pine 与 Python 的语义差异写错了不报错，
只会安静地产出一条假曲线。正确做法是用机械闸门把致命错误卡死。

## 执行循环（一次处理一批）

### 1. 分诊

```bash
python -m alpharadar.porting.triage
python -m alpharadar.porting.scaffold --list 20
```

看候选清单。优先挑：分数高、家族明确、**没有重绘风险**（无感叹号标记）的。

### 2. 取工单

```bash
python -m alpharadar.porting.scaffold --next
# 产出 work/port-<hash>.md：元信息 + 建议包装器 + Python 模板 + 完整 Pine 源码
```

工单里已经给出**建议包装器**（按检测到的指标族）。读了源码后如果认为包装器
不合适，可以换，但必须：

- 在策略的 `notes=` 里写明用了哪个包装器、为什么换
- 保持同一指标族内口径一致（见 `docs/porting.md` 的七个标准包装器）

### 3. 写实现

在 `alpharadar/strategies/library.py` 末尾追加一个 `@register` 装饰的策略函数，
形如：

```python
@register("tv_xxxxxxxxxx", "策略名", source="TradingView @作者",
          license="原脚本许可", source_sid="PUB;xxxx",
          freqs=("1min", "5min", "15min", "30min", "60min"),
          defaults={...}, notes="包装器与改写说明")
def _tv_xxxxxxxxxx(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    ...
    return signal_frame(df, sig, stop=stop)
```

硬要求（细节见 docs/porting.md）：

1. **只用 `<= t` 的数据**。`security(lookahead=...)`、未确认的 `pivothigh`、
   `varip`、`barstate.isrealtime` 都要按已确认口径改写。
2. **只输出信号列**，不要在策略里算钱或撮合。
3. **`defaults` 列全**该策略读取的每个参数。
4. **保留原作者与许可**。

### 4. 机械校验（不可跳过）

```bash
python -m alpharadar.porting.verify --strategy <key> --real P.DCE:5min
```

四道闸门：无未来函数 / 信号健全 / 可复现 / 预热纪律。

- **不过就改，改完再跑**；连续两次不过就标记 `rejected` 并写清原因，别硬凑。
- 形态类策略（反转、吞没、假突破）在合成数据上信号可能过少，
  **必须加 `--real`** 用真实行情复核。
- `--all` 可以一次校验全部已注册策略，防止改公共代码时误伤别人。

### 5. 入库并开跑

```bash
python -m pytest -q                                  # 先确认没改坏别的
python -m alpharadar.worker --sync --minutes 0.1     # 同步任务队列
```

新策略会自动铺满全市场（按 `freqs` 白名单过滤周期）。回测结果进 `/runs` 看板。

### 6. 记录

更新两处：

- 策略的 `notes=`：一句话结论（含关键数字）
- `docs/findings.md`：完整记录，**失败也要写**

## 批量推进的建议节奏

1. 先跑 5 个跑通闭环（分诊 → 工单 → 实现 → 校验 → 同步 → 看结果）
2. 确认闸门有效后放量：一批 10~20 个，按分数从高到低
3. 每批结束后按**包装器**聚合复盘：哪类包装器在全市场更稳
   —— 这才是这套流水线的真正产出：不是某一条策略，而是一张
   「哪类想法有效」的地图

## 输出给用户时的口径

- 报「已移植 N / 待移植 M / 校验未过 K」，不要只报成功的
- 报策略时同时给**笔数、分年、收益回撤比**，并声明是全市场还是单品种
- 校验未过的要说明是"重绘风险放弃"还是"包装器选错"
- 不要承诺收益，不要说"稳赚"
