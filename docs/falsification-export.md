# 证伪档案导出契约（falsification export）

alpha-radar 把「结果库里的每条回测」和「手写的精选档案」按五道闸门判成结论，
导出成一份 JSON，给 aiquant 后端的 `GET /v1/strategy/falsification` 当数据源。

| 入口 | 用法 |
|---|---|
| CLI | `alpharadar falsify-export --out falsification.json [--include-insufficient] [--limit N] [--db path]`；`--out -` 输出到 stdout |
| HTTP | `GET /api/falsification[?include=insufficient][&limit=N]`，只读、免鉴权，进程内缓存 `ALPHARADAR_FALSIFY_TTL` 秒（默认 300）|
| 代码 | `alpharadar.falsify.build_export(...) -> dict` |

`schema_version` 目前是 `1`。**只增不改**：新增字段向后兼容；改字段含义或删字段时升版本号。

## 硬约束

1. **不含报告路径**。HTML 报告和逐笔 CSV 只在内部使用，条目按白名单字段拼装
   （`falsify.ENTRY_FIELDS`），不会出现 `report` / `report_url`。
2. **许可过滤**。策略的 `license` 字段、语料库里原 Pine 源码头部声明、作者兜底名单
   （`config/licenses.json`）任一判为非商用（CC BY-NC*）或禁止再分发，整条不导出。
   许可无法判定的按 `unknown_policy` 处理（默认导出，`license_status = "unknown"`）。
3. **insufficient 默认不导出**（自动条目）。`?include=insufficient` / `--include-insufficient`
   才带上，供搜索与「跑一次证伪」结果页使用。精选档案（`curated: true`）总是导出。
4. **tradable 只能人工给出**：`config/verdict_overrides.json` 登记，且该条自动结论必须是
   `pending`，否则忽略并计入 `summary.override_ignored`。
5. 严格 JSON：`NaN` / `Infinity`（例如 1–3 笔交易的 PF∞）一律输出为 `null`。

## 顶层结构

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | int | 契约版本，当前 1 |
| `generated_at` | string | 导出时间（服务器本地时间，ISO 8601，无时区，服务器为 UTC+8）|
| `threshold_version` | string | 本次判定使用的阈值版本（`config/gates.json`）|
| `gates` | array[5] | 闸门说明，固定顺序 sample → scale → yearly → drawdown → robust；每项 `{id, name, rule, why}` |
| `summary` | object | 计数，见下 |
| `archive` | array | 档案条目：先精选档案（文件顺序），再自动条目（按 verdict → family_key → strategy_key → symbol → freq 排序）|

`gates[0]` 示例：

```json
{
  "id": "sample",
  "name": "样本闸门",
  "rule": "交易笔数 ≥ 200，且有交易的年份 ≥ 3",
  "why": "少于 30 笔的「高 PF」是噪声；55 笔那一档再漂亮也不敢实盘。样本不够只写「样本不足」，不算淘汰，也不当结论用。"
}
```

### summary

| 字段 | 说明 |
|---|---|
| `archive_total` / `curated` / `auto` | 条目总数 / 精选 / 自动 |
| `by_verdict` | `{tradable, pending, finding, reject, insufficient}` 计数 |
| `failed_gate` | 被淘汰条目按第一道失败闸门计数 `{scale, yearly, drawdown}` |
| `tradable` / `pending` / `rejected` | 便捷计数（= by_verdict 对应项）|
| `scale_marginal` | 尺度闸门「勉强通过」的条目数 |
| `rerun_pending` | 需要在生产环境重跑的条目数（clean-room 重写后的精选档案）|
| `include_insufficient` | 本次是否包含样本不足条目 |
| `excluded_license` | 因许可被排除的条目数 |
| `insufficient_hidden` | 因样本不足被隐藏的自动条目数 |
| `unregistered` | 结果库里有、但策略已不在注册表（无法核许可）而跳过的条目数 |
| `override_applied` / `override_ignored` | 人工覆盖生效 / 被忽略的条数 |
| `truncated` | 因 `limit` 截掉的自动条目数 |

## 档案条目（archive[]）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 稳定 id。自动条目 = `<strategy_key>-<symbol>-<freq>` 小写、非字母数字转 `-`（如 `utbot-p-dce-5min`）；精选档案用手写 id |
| `strategy` / `strategy_key` | string | 展示名 / 注册表 key（精选「研究发现」条目指向它研究的策略）|
| `family` / `family_key` | string | 家族中文名 / 英文 key：`trend` `breakout` `reversal` `oscillator` `bands` `level` `volatility` `volume` `pattern` `research` `unknown` |
| `source` | string | 出处；clean-room 重写的写「原创实现」|
| `origin` | string | 思路来源说明（原创实现时必填），否则空串 |
| `license` / `license_status` | string | 许可文字 / `open` `unknown`（`nc`、`restricted` 不会出现，已被过滤）|
| `symbol` / `name` | string | 代码 / 中文名 |
| `asset_class` | string | `futures` / `stock` / `etf` |
| `freq` | string | `1min` `5min` `15min` `30min` `60min` `1d` |
| `verdict` | string | `insufficient` 样本不足 · `reject` 淘汰 · `pending` 仍在验证 · `tradable` 可交易（仅人工）· `finding` 研究发现 |
| `failed_gate` | string\|null | 第一道没过的闸门：reject 时为 `scale` / `yearly` / `drawdown`；insufficient 且样本不足时为 `sample`；其他为 null |
| `insufficient_reason` | string\|null | `sample`（笔数或年数不够）或 `data:<gate>`（该闸门所需数据缺失，判不了）|
| `gates` | object | 五道闸门各自的 `{status, value, threshold[, note]}`，见下 |
| `threshold_version` | string | 判定所用阈值版本 |
| `flags` | string[] | `scale_marginal` 尺度勉强 · `yearly_degraded` 分年只按汇总正年数判 · `rerun_pending` 需重跑 |
| `editor_verdict` | string\|null | 精选档案的手写结论（自动条目为 null）|
| `headline` | string | 一句话结论（精选为手写，自动条目按失败闸门生成）|
| `metrics` | object | `trades` `win`(0–1) `pf` `avg_points`(每手净点数) `max_dd_pct` `pnl_dd` `positive_years` `years` `total_pnl`(元) `max_dd`(元，负数)；未知为 null |
| `few` | string | 关键数字的一行摘要 |
| `yearly` | [[year, pnl]] | 逐年盈亏（元）；未知为空数组 |
| `mechanism` | string | 失效机制（精选档案手写；自动条目为空串）|
| `command` | string | 复现命令 |
| `window` | object | 回测窗口 `{start, end}`，YYYYMMDD |
| `curated` | bool | 是否为手写机制的精选档案 |
| `rerun_note` | string\|null | `rerun_pending` 的说明 |
| `judged_at` | string | 本次判定时间 |
| `updated_at` | string | 回测结果时间（自动条目）/ 精选档案更新日期 |

### gates.\<id\>

| id | status 取值 | value | threshold |
|---|---|---|---|
| `sample` | pass / fail / unknown | `{trades, years}` | `{min_trades: 200, min_years: 3}` |
| `scale` | pass / marginal / fail / unknown | 往返成本 ÷ 平均振幅（如 0.2216）| `{pass_below: 0.25, fail_at: 0.40}` |
| `yearly` | pass / fail / unknown | `{positive_years, years, ratio, recent:[[年,盈亏]×3]\|null, source: yearly\|trades_csv\|summary}` | `{min_positive_ratio: 0.8, recent_full_years: 3}` |
| `drawdown` | pass / fail / unknown | 总盈亏 ÷ \|最大回撤\| | `{min_pnl_dd: 1.0, require_positive_pnl: true}` |
| `robust` | review（人工覆盖后为 pass）| null | null |

判定规则：样本没过 → `insufficient`；否则按 尺度 → 分年 → 收益回撤比 找第一道 `fail` → `reject`；
都没 fail 但有 `unknown` → `insufficient`（`data:<gate>`）；全过 → `pending`。
「完整年度」= 回测窗口从该年 1 月 10 日前开始、到 12 月 25 日后结束。

## 示例：自动条目（尺度勉强通过 → pending）

```json
{
  "id": "supertrend-p-dce-5min",
  "strategy": "SuperTrend",
  "strategy_key": "supertrend",
  "family": "趋势跟随",
  "family_key": "trend",
  "source": "TradingView @KivancOzbilgic",
  "origin": "",
  "license": "MPL-2.0",
  "license_status": "open",
  "symbol": "P.DCE",
  "name": "棕榈油",
  "asset_class": "futures",
  "freq": "5min",
  "verdict": "pending",
  "failed_gate": null,
  "gates": {
    "sample": {
      "status": "pass",
      "value": {
        "trades": 500,
        "years": 4
      },
      "threshold": {
        "min_trades": 200,
        "min_years": 3
      }
    },
    "scale": {
      "status": "marginal",
      "value": 0.3,
      "threshold": {
        "pass_below": 0.25,
        "fail_at": 0.4
      }
    },
    "yearly": {
      "status": "pass",
      "value": {
        "positive_years": 4,
        "years": 4,
        "ratio": 1.0,
        "recent": [
          [
            "2023",
            10.0
          ],
          [
            "2024",
            10.0
          ],
          [
            "2025",
            10.0
          ]
        ],
        "source": "yearly"
      },
      "threshold": {
        "min_positive_ratio": 0.8,
        "recent_full_years": 3
      }
    },
    "drawdown": {
      "status": "pass",
      "value": 5.0,
      "threshold": {
        "min_pnl_dd": 1.0,
        "require_positive_pnl": true
      }
    },
    "robust": {
      "status": "review",
      "value": null,
      "threshold": null,
      "note": "MVP 不做自动判定，待人工复核"
    }
  },
  "threshold_version": "2026-10-10.v1",
  "flags": [
    "scale_marginal"
  ],
  "insufficient_reason": null,
  "editor_verdict": null,
  "headline": "四道闸门全过，等待参数稳健性人工复核",
  "metrics": {
    "trades": 500,
    "win": 0.45,
    "pf": 1.3,
    "avg_points": 2.0,
    "max_dd_pct": null,
    "pnl_dd": 5.0,
    "positive_years": 4,
    "years": 4,
    "total_pnl": 50000.0,
    "max_dd": -10000.0
  },
  "few": "500 笔 / 胜率 45.0% / PF 1.30 / 每手 +2.00 点 / 4/4 年为正",
  "yearly": [
    [
      "2022",
      10
    ],
    [
      "2023",
      10
    ],
    [
      "2024",
      10
    ],
    [
      "2025",
      10
    ]
  ],
  "mechanism": "",
  "command": "alpharadar run --symbol P.DCE --strategy supertrend --freq 5min --start 20220101",
  "window": {
    "start": "20220101",
    "end": "20260930"
  },
  "curated": false,
  "rerun_note": null,
  "judged_at": "2026-10-10T18:00:00",
  "updated_at": "2026-10-09T12:00:00"
}
```

## 示例：精选档案（clean-room 重写，待重跑）

```json
{
  "id": "orb-5min",
  "strategy": "开盘区间突破 ORB（原创实现）",
  "strategy_key": "orb_classic",
  "family": "日内突破",
  "family_key": "breakout",
  "source": "原创实现",
  "origin": "思路来源：Toby Crabel 的开盘区间突破（Opening Range Breakout，1990 年公开出版的交易思路）；按公开思路独立实现，未参照任何 TradingView 源码。",
  "license": "MIT",
  "license_status": "open",
  "symbol": "P.DCE",
  "name": "棕榈油",
  "asset_class": "futures",
  "freq": "5min",
  "verdict": "reject",
  "failed_gate": "yearly",
  "gates": {
    "sample": {
      "status": "pass",
      "value": {
        "trades": 1223,
        "years": 5
      },
      "threshold": {
        "min_trades": 200,
        "min_years": 3
      }
    },
    "scale": {
      "status": "pass",
      "value": 0.2216,
      "threshold": {
        "pass_below": 0.25,
        "fail_at": 0.4
      },
      "note": "P.DCE 5min 实测：往返成本 4.5 点 ÷ 平均振幅 20.31 点（2026-10-10 cost_scales）"
    },
    "yearly": {
      "status": "fail",
      "value": {
        "positive_years": 0,
        "years": 5,
        "ratio": 0.0,
        "recent": null,
        "source": "summary"
      },
      "threshold": {
        "min_positive_ratio": 0.8,
        "recent_full_years": 3
      },
      "note": "仅有汇总正年数：正年数不足，近三年不可能全不为负"
    },
    "drawdown": {
      "status": "unknown",
      "value": null,
      "threshold": {
        "min_pnl_dd": 1.0,
        "require_positive_pnl": true
      },
      "note": "缺少总盈亏或最大回撤"
    },
    "robust": {
      "status": "review",
      "value": null,
      "threshold": null,
      "note": "MVP 不做自动判定，待人工复核"
    }
  },
  "threshold_version": "2026-10-10.v1",
  "flags": [
    "rerun_pending",
    "yearly_degraded"
  ],
  "insufficient_reason": null,
  "editor_verdict": "reject",
  "headline": "全场最高胜率 49.3%，依然是负期望",
  "metrics": {
    "trades": 1223,
    "win": 0.493,
    "pf": 0.889,
    "avg_points": null,
    "max_dd_pct": null,
    "pnl_dd": null,
    "positive_years": 0,
    "years": 5,
    "total_pnl": null,
    "max_dd": null
  },
  "few": "1223 笔 / 胜率 49.3% / PF 0.889 / 0/5 年为正",
  "yearly": [],
  "mechanism": "赔率结构不成立：突破后回撤到对侧止损太常见，而赢的行程常在一倍区间高度之前就衰竭。**高胜率掩盖不了负期望** —— 这条是胜率和赚钱无关的最干净证据。",
  "command": "alpharadar run --symbol P.DCE --strategy orb_classic --freq 5min --start 20220101",
  "window": {
    "start": "20220101",
    "end": "20261009"
  },
  "curated": true,
  "rerun_note": "下列数字来自旧的 TradingView 移植版（CC BY-NC-SA 4.0，已停止对外使用）。原创实现的思路相同但代码独立，需在生产环境重跑回测后替换这些数字并重新判定。",
  "judged_at": "2026-10-10T18:00:00",
  "updated_at": "2026-10-10"
}
```

## 示例：summary

```json
{
  "archive_total": 9,
  "curated": 8,
  "auto": 1,
  "by_verdict": {
    "tradable": 0,
    "pending": 1,
    "finding": 3,
    "reject": 4,
    "insufficient": 1
  },
  "failed_gate": {
    "scale": 2,
    "yearly": 2,
    "drawdown": 0
  },
  "tradable": 0,
  "pending": 1,
  "rejected": 4,
  "scale_marginal": 1,
  "rerun_pending": 2,
  "include_insufficient": false,
  "excluded_license": 0,
  "insufficient_hidden": 0,
  "unregistered": 0,
  "override_applied": 0,
  "override_ignored": 0,
  "truncated": 0
}
```

## 阈值变更流程

1. 改 `config/gates.json` 的数字，**同时改 `threshold_version`**；
2. 跑 `pytest`（`tests/test_falsification.py` 锁住了当前阈值，按需同步）；
3. 部署后 `/api/falsification` 在缓存过期后自动用新阈值重判，客户端可以用
   `threshold_version` + `judged_at` 判断结论变化是因为阈值还是因为新数据。
