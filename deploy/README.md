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
