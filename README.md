# alpha-radar · 策略雷达

持续从 TradingView 采集开源策略，在 **A 股 / 期货** 上自动回测、筛选、记录结论。
数据源 Tushare，agent 底座 Codex harness + deepseek-flash。

**这个项目不是「策略生成器」，是「策略证伪器」。** 它批量地读、批量地测，
并把「什么不行、为什么不行」沉淀成可复现的记录 —— 因为量化研究里
90% 的工作量在否定，而不是在发现。

**在线演示**：<https://alpha-radar.infiniti.website> ——
选品种 / 策略 / 周期，直接跑一次含成本回测，看 HTML 报告。
**实时看板**：<https://alpha-radar.infiniti.website/runs> ——
服务器上每 6 小时自动采集 + 扫描全品种，这里按 PF 排序看结果、按品种/策略筛选、查运行历史。

## 它长什么样

```text
$ alpharadar harvest --max-fetch 300
搜索通道：40 个关键词，命中 1180 个唯一脚本
脚本流通道：新增 779 个候选（累计 1959）
论坛通道：新增 0 个候选，其中正文代码块 0 段
语料库更新：1200 个脚本（开源 1200，本轮新下载 300）-> corpus/sources

$ alpharadar corpus -n 5
     点赞  标题                                作者             类型    行数
 171640   Smart Money Concepts [LuxAlgo]      LuxAlgo          study   1002
  82983   Supertrend                           KivancOzbilgic   study     72
  59634   UT Bot Alerts                        QuantNomad       study     83
  29378   Chandelier Exit                      everget          study     64

$ alpharadar run --symbol P.DCE --strategy utbot --freq 5min --start 20220101
===== 棕榈油（P.DCE · futures） / UT Bot =====
笔数 1060   胜率 42.7%   PF 1.05   每手均点 +1.059   合计 +11,227 元
最大回撤 -25,076（-18.5%）   收益回撤比 0.45   正年数 2/5
分年： 2022 +23,554 / 2023 +5,915 / 2024 -2,666 / 2025 -3,885 / 2026 -11,691
-> HTML 报告：reports/P.DCE_utbot_5min.html
```

注意上面这条结论：**PF 大于 1，但依然不能实盘**（利润全在 2022 年、
最近三年连亏、收益回撤比 0.45）。把它如实印出来，比印一条漂亮曲线重要。

### 三条采集通道

| 通道 | 接口 | 说明 |
|---|---|---|
| 关键词搜索 | `pubscripts-suggest-json` | 按 40 个策略族关键词找「热门」脚本 |
| 脚本流 | `api/v1/scripts/` | 「最新发布」脚本，分页 1000 条，补齐搜索漏掉的近期发布 |
| 论坛帖 | `api/v1/ideas/` | 社区帖子；脚本型帖子带 `script_id_part`，正文里的代码块也会被抠出来 |

三条通道统一走 `pine-facade` 取完整 Pine 源码，只收 `scriptAccess=open` 的。
`max_fetch` 限制**每轮新增下载数**，语料库在连续几轮里逐步补齐。

实测（2026-10-06）：脚本流 1000 条里 **779 条开源**，而原搜索通道只覆盖 233 条；
论坛帖最新 996 条**没有一条含代码块或脚本**（全是看盘分析）——
通道保留并记录产出，一旦出现代码会入库。

## 快速开始

```bash
git clone <your-fork> && cd alpha-radar
pip install -e .
cp .env.example .env          # 填入 TUSHARE_TOKEN

alpharadar harvest            # 采集 TradingView 开源策略
alpharadar list               # 看内置策略
alpharadar matrix --symbols P.DCE,Y.DCE,M.DCE \
                  --strategies utbot,orb,vreversal --freqs 5min,15min
```

期货分钟线需要 Tushare 的 `ft_mins` 权限；A 股分钟线需要 `stk_mins` 权限。
日线（`--freq 1d`）权限门槛低得多，A 股建议从日线开始。

## 内置策略

| key | 名称 | 出处 | 许可 |
|---|---|---|---|
| `utbot` | UT Bot | TradingView @QuantNomad | — |
| `supertrend` | SuperTrend | TradingView @KivancOzbilgic | MPL-2.0 |
| `chandelier` | Chandelier Exit | TradingView @everget | MIT |
| `orb` | 开盘区间突破 | TradingView @LuxAlgo | CC BY-NC-SA 4.0 |
| `false_breakout` | 假突破反向 | TradingView @Zeiierman | CC BY-NC-SA 4.0 |
| `vreversal` | 冰点反转 | 原创 | MIT |
| `ema_cross` | 双均线交叉（基线） | 通用 | MIT |

移植脚本保留了原作者与许可声明。**CC BY-NC-SA 许可禁止商业使用**，
商业场景请自行确认许可或改用 MIT/MPL 来源。

## 两只手：确定性内核 + agent 大脑

### 全市场回测：任务队列 + 常驻 worker

全市场是 **4 万量级**的回测单元（全 A 股 + 全期货品种 × 各自可用周期 × 全部策略），
一轮跑不完，所以用**持久化任务队列**而不是固定清单：

```
alpharadar-scheduler.timer  (每 6h)  -> 采集语料库 + 同步品种表/任务队列
alpharadar-worker.service   (常驻)   -> 从队列取到期任务，串行回测
```

- 成功 → `next_due = 现在 + 7 天`；失败 → `+6 小时`自动重试
- 进程内 `flock` 防重入；磁盘低于 2GB 自动暂停（同机还有两个邻居服务）
- `Nice=10 / CPUWeight=20 / IOWeight=20`，不抢资源
- 看板 <https://alpha-radar.infiniti.website/runs> 有实时队列进度与 ETA

改范围只动 `config/universe.json` 的 `auto` 段：

```json
"stocks":  {"freqs": ["1d","1min","5min","15min","30min","60min"],
            "minute_top_n": 300, "start_minute": "20240101"},
"futures": {"freqs": ["1min","5min","15min","30min","60min","1d"]}
```

**物理约束（必须知道）**：全市场 1 分钟数据约 **78GB**，普通云主机放不下。
所以默认策略是「**日线跑全市场，分钟级按流动性分层**」——
A 股全市场跑日线，分钟级只覆盖成交额前 300 只；期货品种少（103 个），六个周期全开。

### 磁盘不够怎么办：按品种回收行情缓存

worker 是**按品种成批**处理任务的（一个品种 6~11 个任务共用一份行情），
一个品种全部跑完后立刻删掉它的行情缓存，需要时再下：

```
[cache] 000153.SZ 完成，回收 2 个文件 0.1MB
[cache] 000155.SZ 完成，回收 2 个文件 0.1MB
```

- 删的是**合约行情**；**主力映射表保留**（很小，且每个期货任务都要用）
- 再加一道全局兜底：缓存超过 `cache.max_gb`（默认 3GB）按 LRU 删到 80%
- 代价：每轮扫描都相当于冷启动，稳定态约 30 小时/轮（requeue 是 7 天一次，够用）
- **扩盘后**：给 worker 加 `--keep-cache`，省掉全部重复下载，一轮降到几小时

**无人值守**：`alpharadar-scheduler.timer` 每 6 小时跑一轮
「增量采集 TradingView 开源脚本 → 扫描 `config/universe.json` 里的品种×周期×策略 → 写 SQLite」。
某个组合在 `max_age_days` 内成功过就跳过；需要重跑的按上次成功时间从旧到新排队，
一轮跑不完也不会饿死后面的组合。

**内核**（`alpharadar/`）是可独立运行的 Python 包：数据、撮合、成本、指标、报告。
不依赖任何 agent，`pip install` 后即可用。

**大脑**（`AGENTS.md` + `.codex/skills/alpha-radar/`）把 Codex 变成研究员：
按固定循环 发现 → 筛选 → 移植 → 回测 → 记录，并受一组铁律约束：

- 没有含成本的回测，不许说有效
- 必须给分年结果、声明搜索规模、报样本量
- 不许为了好看而调参；失败的结论也要写进 `docs/findings.md`

内核刻意把所有交易规则（撮合顺序、成本、T+1）锁在 `engine.py` 一处，
agent 只能产出信号列 —— 这样它**没有能力**通过改口径造出一条好看的曲线。

## 回测口径

- 信号当根收盘确认，按收盘价 ± 1 跳滑点成交
- **止损优先于止盈**；止盈按触价成交（限价单），止损吃滑点
- 期货：按手手续费（可叠加成交额比例）；A 股：佣金 + 卖出印花税 + 最低 5 元
- **A 股 T+1 强制执行**（当日买入当日不可卖，含止损）
- 默认只做日盘、09:10 前与 14:30 后不开仓、收盘强平

## 我们已经得到的结论

完整记录见 [docs/findings.md](docs/findings.md) 与 [docs/pitfalls.md](docs/pitfalls.md)：

1. **成本是第一道闸门**：往返成本 ÷ 平均振幅 > 25% 的尺度不要做。
   棕榈油 1 分钟是 58%，五个策略家族全负。
2. **宽止损 > 紧止损，没有例外**。ATR 倍数 1.0→3.0 单调改善；
   1.5×ATR 止损和保本止损都是负贡献。
3. **胜率和赚钱没关系**：胜率最高的策略（49.3%）亏得最多。
4. **体制支配一切**：加一个 ER 效率系数闸门，PF 从 1.043 抬到 1.578、
   正年数从 2/5 抬到 4/5 —— 比两轮入场调优加起来都有效。
5. **没有圣杯**：目前最好的一档只有 55 笔（约 12 笔/年），样本太薄；
   千笔样本那一档收益回撤比只有 0.45。**都还不能实盘。**

## 路线图

- [ ] 跨品种验证体制闸门（Y / OI / M / C / RB / A 股指数）
- [ ] 把「不交易」做成正式状态机（体制转弱立刻空仓）
- [ ] 补 2016–2021 高波动年，检验趋势族的真样本外表现
- [ ] 组合层：多品种同时交易的资金分配与相关性控制
- [ ] dsh（DeepSeek Harness）插件形态，支持定时自动扫描
- [ ] 更多数据源（AkShare / 本地 CTP 行情）

## 免责声明

本项目是研究工具，**不构成投资建议**。回测不含冲击成本、涨跌停无法成交、
盘中流动性枯竭等实盘约束；历史表现不代表未来收益。作者不对任何使用后果负责。

采集的 TradingView 脚本版权归原作者所有，本仓库仅用于研究检索与许可范围内的移植。

## License

MIT（本项目代码）。第三方策略按其原始许可（见上表与各文件头）。

## 其他

如果你喜欢我的项目，可以给我买杯咖啡：

<img src="https://github.com/user-attachments/assets/e75ef971-ff56-41e5-88b9-317595d22f81" alt="image" width="300" height="300">
