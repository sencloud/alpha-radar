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

## 一键部署

```powershell
cd D:\GitHub\alpha-radar
python deploy\deploy.py --steps all
# 后续只更新代码：
python deploy\deploy.py --steps upload,systemd,verify
```

步骤（全部幂等，可单独重跑）：

`pack → upload → user → venv → env → systemd → caddy → firewall → verify`

| 步骤 | 做什么 |
|---|---|
| pack | 本地打包（排除 .git / 缓存 / 报告） |
| upload | 传到 `/opt/alpha-radar` 并解包 |
| user | 建系统用户 `alpharadar` 与目录属主 |
| venv | 建虚拟环境并 `pip install -e .` |
| env | 上传 `.env`（含 TUSHARE_TOKEN），权限 600 |
| systemd | 安装并重启 `alpharadar.service` |
| caddy | 安装 `conf.d/alpharadar.caddy`，校验后 reload |
| firewall | 确认本机 80/443 放行（云安全组需另配） |
| verify | 本机 /healthz + 域名 HTTPS + 两个邻居站点 |

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
