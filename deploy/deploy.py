"""alpha-radar 部署编排（阿里云 ECS · Workbench CLI）。

用法（在仓库根目录）：
    python deploy/deploy.py --steps all
    python deploy/deploy.py --steps upload,systemd,verify
    python deploy/deploy.py --steps rollback

设计约束（源自同机 lastdays 的历史事故，见 deploy/README.md）：
  - 不走 SSH 22，一律走 Workbench CLI；
  - **绝不整份覆盖 /etc/caddy/Caddyfile**，站点块只写 conf.d/ 下自己的文件；
  - 复杂远程脚本 base64 传输，避免 PowerShell 引号被本地展开。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
WORKBENCH = Path(os.environ.get(
    "WORKBENCH_EXE", r"C:\Program Files\workbench\workbench.exe"))
INSTANCE = os.environ.get("ALPHARADAR_INSTANCE", "i-mj758zcz8k917p3ppsuj")

APP = "/opt/alpha-radar"
USER = "alpharadar"
SVC = "alpharadar"
PORT = 8901
DOMAIN = "alpha-radar.infiniti.website"
COHOSTS = ("https://lastndays.com/", "https://infiniti.website/")
BACKUP = "/var/backups/alpha-radar"

# 注意：data/（结果库）、.env、*.db、*.lock 必须排除。
# 踩过的坑：早期版本漏了 data/，把本地测试数据库打包上传，
# 服务端解包后 DB 属主变成 root，且混进了本地测试记录。
EXCLUDE = {".git", "data", "data_cache", "reports", "corpus", "dist", ".venv",
           "__pycache__", ".pytest_cache", ".idea", ".vscode"}
EXCLUDE_FILES = {".env"}
EXCLUDE_SUFFIX = (".db", ".db-wal", ".db-shm", ".lock", ".pyc")

ALL_STEPS = ("pack", "upload", "user", "venv", "env", "systemd", "worker",
             "autoport", "schedule", "caddy", "firewall", "verify")


# ==================== Workbench 封装 ====================
def wb(*args: str, timeout: int = 900, check: bool = True) -> str:
    """调用 workbench 原始子命令（upload / session 等）。"""
    cmd = [str(WORKBENCH), *args]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        raise RuntimeError(f"workbench 失败（{p.returncode}）：{out.strip()[-800:]}")
    return out


def _exec_json(cmd: str, timeout: int = 300) -> dict:
    """执行远程命令并解析 JSON 结果。

    为什么走 JSON：文本模式偶发丢输出（exit=0 但 stdout 为空）。JSON 里有明确的
    stdout / stderr / exit_code / timed_out，可以判定、可以重试。
    """
    p = subprocess.run([str(WORKBENCH), "exec", "-i", INSTANCE, "-o", "json",
                        "-c", cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout + 30)
    text = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    i = text.find("{")
    if i < 0:
        raise RuntimeError(f"workbench 无 JSON 输出：{text[-400:]}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i:])
    except ValueError as exc:
        raise RuntimeError(f"workbench JSON 解析失败：{text[-400:]}") from exc
    return obj


def reset_sessions() -> None:
    """关闭所有 workbench 会话。

    踩过的坑：给 exec 传 `--timeout` 会留下僵死会话，之后每条命令都复用那个坏会话
    —— 表现是「所有命令都 30 秒超时、stdout 只回来一半」。关掉会话立刻恢复。
    """
    try:
        wb("session", "close", "--all", check=False, timeout=60)
    except Exception:
        pass


def sh(script: str, timeout: int = 900, check: bool = True) -> str:
    """把多行 bash 脚本 base64 后交给远端执行（避免引号/换行被本地 shell 吃掉）。

    - **不要给 workbench exec 传 `--timeout`**：实测会留下僵死会话，越用越坏。
    - 单次命令保持短（默认 30 秒上限）；长任务用 setsid 丢后台再轮询。
    - 超时自动关会话重试一次（会话僵死是最常见的假故障）。
    """
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    cmd = f"echo {b64} | base64 -d > /tmp/_ar.sh && bash /tmp/_ar.sh; rm -f /tmp/_ar.sh"
    for _ in range(2):
        res = _exec_json(cmd, timeout=min(timeout, 300))
        if not res.get("timed_out"):
            out = (res.get("stdout") or "") + (res.get("stderr") or "")
            if check and int(res.get("exit_code") or 0) != 0:
                raise RuntimeError(f"远程命令失败（{res.get('exit_code')}）："
                                   f"{out.strip()[-800:]}")
            return out
        print("  [warn] 远程命令超时，重置 workbench 会话后重试…")
        reset_sessions()
        time.sleep(2)
    raise RuntimeError(f"远程命令连续超时：{cmd[:120]}")


def upload(local: Path, remote: str) -> None:
    wb("upload", str(local), remote, "-i", INSTANCE, "-f", timeout=1800)


# ==================== 步骤 ====================
def step_pack() -> Path:
    DIST.mkdir(exist_ok=True)
    tar = DIST / "alpha-radar.tar.gz"
    with tarfile.open(tar, "w:gz") as t:
        for p in ROOT.rglob("*"):
            rel = p.relative_to(ROOT)
            if any(part in EXCLUDE for part in rel.parts):
                continue
            if p.name in EXCLUDE_FILES or p.name.endswith(EXCLUDE_SUFFIX):
                continue
            if p.is_file():
                t.add(p, arcname=str(rel))
    print(f"[pack] {tar}  {tar.stat().st_size / 1024:.0f} KB")
    return tar


def step_upload(tar: Path) -> None:
    sh(f"mkdir -p {BACKUP} && tar -czf {BACKUP}/code-$(date +%Y%m%d-%H%M%S).tar.gz "
       f"-C {APP} --exclude=.venv --exclude=data_cache --exclude=reports . 2>/dev/null; "
       f"ls -t {BACKUP}/code-*.tar.gz 2>/dev/null | tail -n +6 | xargs -r rm -f; "
       f"mkdir -p {APP}")
    upload(tar, "/tmp/alpha-radar.tar.gz")
    sh(f"tar -xzf /tmp/alpha-radar.tar.gz -C {APP} && rm -f /tmp/alpha-radar.tar.gz "
       f"&& ls {APP} | head -20")
    print("[upload] 代码已同步到", APP)


def step_user() -> None:
    sh(f"id -u {USER} >/dev/null 2>&1 || useradd --system --no-create-home "
       f"--shell /usr/sbin/nologin {USER}; "
       f"mkdir -p {APP}/data {APP}/data_cache {APP}/reports {APP}/corpus; "
       f"chown -R {USER}:{USER} {APP}; chmod 755 {APP}")
    print(f"[user] {USER} 就绪，目录属主已设置")


def step_venv() -> None:
    sh(f"cd {APP} && python3 -m venv .venv && "
       f".venv/bin/pip install -q --upgrade pip setuptools wheel && "
       f".venv/bin/pip install -q -e . && "
       f"chown -R {USER}:{USER} {APP}/.venv", timeout=1800)
    out = sh(f"{APP}/.venv/bin/python -c "
             f"'import alpharadar,pandas,tushare;print(alpharadar.__version__)'")
    print("[venv] 依赖就绪，版本", out.strip().splitlines()[-1])


def step_env() -> None:
    local = ROOT / ".env"
    if not local.exists():
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise SystemExit("缺少 .env 或环境变量 TUSHARE_TOKEN")
        local.write_text(f"TUSHARE_TOKEN={token}\n", encoding="utf-8")
    upload(local, f"{APP}/.env")
    # Windows 写出的 .env 是 CRLF，值里会多一个 \r，导致 token 校验失败
    # （踩过：手工跑 --sync 报「您的token不对」，systemd 下却正常）。
    # 统一转成 LF 并去掉行尾空白，再自检 token 能被解析出来。
    sh(f"sed -i 's/\\r$//' {APP}/.env && "
       f"chmod 600 {APP}/.env && chown {USER}:{USER} {APP}/.env && "
       f"grep -c . {APP}/.env && "
       f"grep -q '^TUSHARE_TOKEN=..*' {APP}/.env && echo 'TUSHARE_TOKEN 已就位'")
    # 自动移植需要 LLM 凭据：同机 lastdays 项目已有 DeepSeek 配置，直接复用
    # （同一个账号、同一台机；只在 alpha-radar 自己缺这三个键时才补）
    sh(f"for k in LLM_API_KEY LLM_BASE_URL LLM_MODEL; do "
       f"  grep -q \"^$k=..*\" {APP}/.env || {{ "
       f"    v=$(grep -m1 \"^$k=\" /opt/lastdays/.env 2>/dev/null); "
       f"    [ -n \"$v\" ] && echo \"$v\" >> {APP}/.env && echo \"[env] 复用 lastdays 的 $k\"; "
       f"  }}; "
       f"done; "
       f"grep -q '^LLM_API_KEY=..*' {APP}/.env && echo 'LLM 凭据已就位' || "
       f"echo '[warn] 缺 LLM 凭据：自动移植不可用（其余功能不受影响）'")
    print("[env] .env 已就位（LF + 600）")


def step_systemd() -> None:
    sh(f"cp {APP}/deploy/{SVC}.service /etc/systemd/system/{SVC}.service && "
       f"systemctl daemon-reload && systemctl enable {SVC} >/dev/null 2>&1; "
       f"systemctl restart {SVC} && sleep 3 && systemctl is-active {SVC}")
    print(f"[systemd] {SVC}.service 已重启")


def step_schedule() -> None:
    """安装定时扫描：oneshot service + timer（每 6 小时）。"""
    sh(f"cp {APP}/deploy/{SVC}-scheduler.service /etc/systemd/system/ && "
       f"cp {APP}/deploy/{SVC}-scheduler.timer /etc/systemd/system/ && "
       f"systemctl daemon-reload && "
       f"systemctl enable --now {SVC}-scheduler.timer >/dev/null 2>&1; "
       f"systemctl is-active {SVC}-scheduler.timer; "
       f"systemctl list-timers {SVC}-scheduler.timer --no-pager | head -2")
    print("[schedule] 定时扫描已启用（每 6 小时）")


def step_worker() -> None:
    """安装全市场串行 worker（常驻服务）。"""
    sh(f"cp {APP}/deploy/{SVC}-worker.service /etc/systemd/system/ && "
       f"systemctl daemon-reload && systemctl enable {SVC}-worker >/dev/null 2>&1; "
       f"systemctl restart {SVC}-worker && sleep 3 && "
       f"systemctl is-active {SVC}-worker")
    print("[worker] 全市场 worker 已启动")


def step_autoport() -> None:
    """安装 LLM 自动移植服务（依赖 .env 里的 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL）。"""
    sh(f"cp {APP}/deploy/{SVC}-autoport.service /etc/systemd/system/ && "
       f"systemctl daemon-reload && systemctl enable {SVC}-autoport >/dev/null 2>&1; "
       f"grep -q '^LLM_API_KEY=..*' {APP}/.env && echo '[env] LLM_API_KEY 已配置' || "
       f"echo '[warn] .env 缺 LLM_API_KEY，autoport 会启动失败'; "
       f"systemctl restart {SVC}-autoport && sleep 3 && "
       f"systemctl is-active {SVC}-autoport")
    print("[autoport] 自动移植服务已启动")


def step_caddy() -> None:
    # 只写自己的 snippet；主 Caddyfile 仅在缺 import 行时追加，且先备份
    sh(f"install -m 644 {APP}/deploy/{SVC}.caddy /etc/caddy/conf.d/{SVC}.caddy && "
       f"cp -n /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S) "
       f"2>/dev/null; "
       f"grep -q 'import /etc/caddy/conf.d/\\*\\.caddy' /etc/caddy/Caddyfile || "
       f"printf '\\n# 由 alpha-radar 部署脚本添加\\nimport /etc/caddy/conf.d/*.caddy\\n' "
       f">> /etc/caddy/Caddyfile; "
       f"caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -3")
    try:
        sh(f"cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-ar && "
           f"systemctl reload caddy && sleep 2 && systemctl is-active caddy")
    except RuntimeError as exc:                        # 校验/加载失败 → 回退
        sh("cp /etc/caddy/Caddyfile.bak-ar /etc/caddy/Caddyfile && "
           "systemctl reload caddy", check=False)
        raise RuntimeError(f"Caddy 加载失败，已回退到备份：{exc}") from exc
    print("[caddy] snippet 已安装并 reload")


def step_firewall() -> None:
    out = sh("ufw status 2>/dev/null | head -1 || echo 'ufw 未安装'")
    if "active" in out and "inactive" not in out:
        sh("ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null && "
           "ufw status | head -8")
    else:
        print("[firewall] ufw 未启用（海外地域默认如此），跳过")
    print("[firewall] 提醒：云安全组入方向需放行 TCP 80/443")


def step_verify() -> bool:
    ok = True
    health = _curl_retry(f"http://127.0.0.1:{PORT}/api/health", body=True)
    print(f"[verify] 本机 healthz: {health.strip()[:200]}")
    ok &= '"ok": true' in health.replace("'", '"')

    svc = sh(f"systemctl is-active {SVC} {SVC}-scheduler.timer {SVC}-worker "
             f"{SVC}-autoport").split()
    print(f"[verify] 服务状态: {svc}")

    for url, must in [(f"https://{DOMAIN}/api/health", True),
                      (f"https://{DOMAIN}/", True)] + [(c, False) for c in COHOSTS]:
        code = _curl_retry(url).strip()
        flag = "OK" if code == "200" else ("WARN" if not must else "FAIL")
        print(f"[verify] {flag}  {code}  {url}")
        if must and code != "200":
            ok = False
    return bool(ok)


def _curl_retry(url: str, body: bool = False, tries: int = 2) -> str:
    """单 URL 探测，失败重试一次 —— 服务器忙时 workbench 会话可能被拖慢。"""
    tail = "" if body else "-o /dev/null -w '%{http_code}'"
    out = ""
    for _ in range(tries):
        try:
            out = sh(f"curl -sk -m 15 {tail} {url} || echo FAIL").strip()
        except RuntimeError:
            out = ""
        if out and out != "FAIL":
            return out
        time.sleep(2)
    return out


def step_rollback() -> None:
    snap = sh(f"ls -t {BACKUP}/code-*.tar.gz 2>/dev/null | head -1").strip()
    if not snap:
        raise SystemExit("没有可用快照")
    sh(f"tar -xzf {snap} -C {APP} && chown -R {USER}:{USER} {APP} && "
       f"systemctl restart {SVC} && sleep 3 && systemctl is-active {SVC}")
    print(f"[rollback] 已回退到 {snap}")


def main() -> int:
    global INSTANCE
    ap = argparse.ArgumentParser(description="alpha-radar 部署（Workbench）")
    ap.add_argument("--steps", default="all",
                    help=f"逗号分隔；all = {','.join(ALL_STEPS)}")
    ap.add_argument("--instance", default=INSTANCE)
    args = ap.parse_args()
    INSTANCE = args.instance

    steps = list(ALL_STEPS) if args.steps == "all" else \
        [s.strip() for s in args.steps.split(",") if s.strip()]
    print(f"目标实例 {INSTANCE} | 步骤 {steps}\n")

    t0 = time.time()
    tar = DIST / "alpha-radar.tar.gz"
    for s in steps:
        if s == "pack":
            tar = step_pack()
        elif s == "upload":
            step_upload(tar if tar.exists() else step_pack())
        elif s == "user":
            step_user()
        elif s == "venv":
            step_venv()
        elif s == "env":
            step_env()
        elif s == "systemd":
            step_systemd()
        elif s == "schedule":
            step_schedule()
        elif s == "worker":
            step_worker()
        elif s == "autoport":
            step_autoport()
        elif s == "caddy":
            step_caddy()
        elif s == "firewall":
            step_firewall()
        elif s == "verify":
            if not step_verify():
                print("\n验收未通过")
                return 1
        elif s == "rollback":
            step_rollback()
        else:
            raise SystemExit(f"未知步骤：{s}")
    print(f"\n完成，用时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
