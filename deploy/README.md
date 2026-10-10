# 部署（阿里云 ECS · Workbench）

目标实例 `i-mj758zcz8k917p3ppsuj`（首尔 ap-northeast-2a，公网 47.80.244.154）。
**这台机同时跑 lastdays.com 与 infiniti.website**，部署必须与它们共存。

## 硬约束（踩过的坑，别重复）

1. **不要走 SSH 22。** 本地网络封了出站 22，一律用 Workbench CLI：
   `C:\Program Files\workbench\workbench.exe exec|upload -i <实例>`。
2. **绝不整份覆盖 `/etc/caddy/Caddyfile`。** 本项目的站点块放在
   `/etc/caddy/conf.d/alpharadar.caddy`（独占文件），主文件只需保留
   `import /etc/caddy/conf.d/*.caddy`。历史上整份覆盖导致邻居站点下线 4 天。
3. **端口不要撞**：lastdays=8787，infiniti=3100，本项目=**8901**。
4. **部署后三方验收**：本站 200，且 lastndays.com / infiniti.website 仍 200。
5. `workbench exec` 默认 30 秒超时，长命令要加 `--timeout`；复杂脚本用 base64
   传输，避免 PowerShell 引号被本地展开。

   **更正（实测踩坑）**：不要给 `workbench exec` 传 `--timeout`。传了之后会留下
   一个僵死会话（`STATE=OPEN`），此后每条命令都复用那个坏会话 —— 症状是
   「所有命令都 30 秒超时、stdout 只回来一半、exit_code 却是 0」。恢复方式：

   ```powershell
   & $w session list                 # 看是否有长期 OPEN 的会话
   & $w session close --all          # 关掉所有会话，下一条命令会自动新建
   ```

   `deploy.py` 已内置这个自愈：exec 走 JSON 模式（能拿到 `timed_out`），
   超时自动 `session close --all` 后重试一次。长任务（扫描/采集）仍应
   `setsid` 丢后台再轮询，不要指望一次 exec 跑完。

## 一键部署

```powershell
cd D:\GitHub\alpha-radar
python deploy\deploy.py --steps all
# 后续只更新代码：
python deploy\deploy.py --steps upload,systemd,verify
```

步骤（全部幂等，可单独重跑）：

`pack → upload → user → venv → env → systemd → schedule → caddy → firewall → verify`

| 步骤 | 做什么 |
|---|---|
| pack | 本地打包（排除 .git / 缓存 / 报告） |
| upload | 传到 `/opt/alpha-radar` 并解包 |
| user | 建系统用户 `alpharadar` 与目录属主 |
| venv | 建虚拟环境并 `pip install -e .` |
| env | 上传 `.env`（含 TUSHARE_TOKEN），权限 600 |
| systemd | 安装并重启 `alpharadar.service` |
| schedule | 安装 `alpharadar-scheduler.{service,timer}`（每 6 小时扫描） |
| caddy | 安装 `conf.d/alpharadar.caddy`，校验后 reload |
| firewall | 确认本机 80/443 放行（云安全组需另配） |
| verify | 本机 /healthz + 域名 HTTPS + 两个邻居站点 |

## 管理口令（必配）与证伪档案接口

`POST /runs/trigger`（立即扫描）和 `POST /scripts/harvest`（立即采集）会在服务器上
起后台任务，以前任何人都能触发。现在必须带管理口令：

- 口令放在 `.env` 的 `ALPHARADAR_ADMIN_TOKEN`（`deploy.py --steps env` 会把本地
  `.env` 传上去，权限 600；`alpharadar.service` 通过 `EnvironmentFile` 读取）；
- 调用方式任选：请求头 `X-Admin-Token: <口令>`、`Authorization: Bearer <口令>`，
  或网页表单里的「管理口令」输入框（字段名 `token`）；
- **没配置口令时这两个 POST 一律 403（fail closed）**，web 启动日志和每次被拒都会打
  `[warn] ALPHARADAR_ADMIN_TOKEN 未配置` —— 上线后在 journalctl 里看到这行就说明漏配了。

```powershell
# 1. 本地 .env 里加一行（生成随机口令）
python -c "import secrets;print('ALPHARADAR_ADMIN_TOKEN=' + secrets.token_urlsafe(32))" >> .env
# 2. 上传代码 + .env，重启服务
python deploy\deploy.py --steps pack,upload,env,systemd,worker,verify
# 3. 验收：不带口令必须 403，带口令 303
& $w exec -i i-mj758zcz8k917p3ppsuj -c "curl -s -o /dev/null -w '%{http_code}' -X POST localhost:8901/runs/trigger"
```

`GET /api/falsification` 是只读、免鉴权的证伪档案接口（给 aiquant 后端拉取），
默认不含「样本不足」条目，`?include=insufficient` 才返回；结果在进程内缓存
`ALPHARADAR_FALSIFY_TTL` 秒（默认 300），自动条目上限 `ALPHARADAR_FALSIFY_LIMIT`
（默认 2000，可用 `?limit=` 覆盖，最大 20000）。契约见 `docs/falsification-export.md`。
也可以离线导出：

```bash
sudo -u alpharadar /opt/alpha-radar/.venv/bin/alpharadar falsify-export \
     --out /opt/alpha-radar/data/falsification.json
```

结果库在升级后第一次 `store.init()` 时会自动补四列（`avg_amp / avg_px / cost_rt /
yearly`，判定层要用）。旧结果没有这几列：尺度闸门会尝试用本机行情缓存重算，
分年闸门会回退到报告目录里的逐笔 CSV，都拿不到的条目判为「样本不足（数据缺失）」，
等 worker 下一轮重跑（7 天轮转）后自然补齐。

## 定时扫描（无人值守）

`alpharadar-scheduler.timer` 每 6 小时触发一次 `alpharadar-scheduler.service`：

1. **增量采集** TradingView 开源脚本，三条通道并行：
   关键词搜索 / 最新脚本流（`api/v1/scripts/`）/ 论坛帖（`api/v1/ideas/`）。
   已下过的文件不重复请求，`harvest_max_fetch` 限制每轮新增下载数
2. **扫描回测** `config/universe.json` 里的 品种 × 周期 × 策略
3. 结果写进 `/opt/alpha-radar/data/alpharadar.db`（SQLite，WAL）

两个关键设计：

- **增量 + 轮转**：`max_age_days`（默认 7 天）内的成功记录直接跳过；
  需要重跑的按「上次成功时间」从旧到新排序，一轮跑不完（`--limit`）也不会饿死后面的组合。
- **只跑一份**：脚本内部 `flock` 防重入，timer 与「立即扫描」按钮不会叠跑。

改扫描范围只需编辑 `config/universe.json`（增减品种/周期/策略），不用改代码。
A 股分钟线需要 `stk_mins` 权限，默认只跑日线。

```bash
# 手动跑一轮（--force 忽略新鲜度，--limit 限量）
sudo -u alpharadar /opt/alpha-radar/.venv/bin/python -m alpharadar.scheduler \
     --once --limit 10

# 看下次触发时间 / 最近的运行
systemctl list-timers alpharadar-scheduler.timer
journalctl -u alpharadar-scheduler -n 50 --no-pager
```

留出给邻居的资源：`Nice=10`、`CPUWeight=20`、`IOWeight=20`，
不与 web / lastdays / infiniti 抢 CPU。

## 看板

## 全市场 worker

```powershell
$w = "C:\Program Files\workbench\workbench.exe"
& $w exec -i i-mj758zcz8k917p3ppsuj -c "systemctl status alpharadar-worker --no-pager | head -12"
& $w exec -i i-mj758zcz8k917p3ppsuj -c "journalctl -u alpharadar-worker -n 50 --no-pager"
& $w exec -i i-mj758zcz8k917p3ppsuj -c "systemctl restart alpharadar-worker"
```

```bash
# 手动同步品种表与任务队列（改完 universe.json 后）
sudo -u alpharadar /opt/alpha-radar/.venv/bin/python -m alpharadar.worker --sync --minutes 0.1
# 只跑 20 个任务做验证
sudo -u alpharadar /opt/alpha-radar/.venv/bin/python -m alpharadar.worker --minutes 0 --limit 20
```

队列在 `data/alpharadar.db` 的 `tasks` 表；`state` 表里的 `worker` 键存最近一次
运行状态（速率、进度），看板据此算 ETA。

### 磁盘策略

worker 按品种成批处理，跑完一个品种就删它的行情缓存（映射表保留）。
全局上限 `--cache-max-gb`（默认 3）超了就按 LRU 回收。

```bash
# 扩容后：关掉回收，速度提升一个量级
sudo systemctl edit alpharadar-worker   # 在 ExecStart 末尾加 --keep-cache
# 或者临时手动跑一轮
sudo -u alpharadar /opt/alpha-radar/.venv/bin/python -m alpharadar.worker \
     --minutes 0 --keep-cache
```

<https://alpha-radar.infiniti.website/runs> —— 调度状态、语料库统计、品种列表、
结果榜（按 PF 排序，可按品种/策略/周期/市场筛选）、运行记录，以及「立即扫描一轮」按钮。

## 首次部署前的两件事

1. **DNS**：`alpha-radar.infiniti.website` 的 A 记录指向 `47.80.244.154`。
2. **云安全组**：入方向放行 TCP 80/443（lastdays 已配，通常无需再动）。

## 运维

```powershell
$w = "C:\Program Files\workbench\workbench.exe"
& $w exec -i i-mj758zcz8k917p3ppsuj -c "journalctl -u alpharadar -n 50 --no-pager"
& $w exec -i i-mj758zcz8k917p3ppsuj -c "systemctl restart alpharadar"
& $w exec -i i-mj758zcz8k917p3ppsuj -c "curl -s localhost:8901/api/health"
& $w exec -i i-mj758zcz8k917p3ppsuj -c "systemctl is-active alpharadar caddy lastdays infiniti"
```

## 回滚

```powershell
python deploy\deploy.py --steps rollback      # 回到上一份代码快照
```

快照在 `/var/backups/alpha-radar/code-*.tar.gz`，保留最近 5 份。
