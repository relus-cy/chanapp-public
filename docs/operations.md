# 运维

chanapp 是单进程应用：API 与采集器在同一个 uvicorn 进程里运行，采集器是事实库唯一的写者。运维的核心约束有三条：一个缓存根只跑一个应用进程、单个 worker；更新前先备份，备份失败就中止；事实库只用一致快照备份，从快照恢复后必须生成新的运行身份。

本页写通用做法。下文命令都在仓库根执行，已实际运行过（服务管理器相关的部分除外，那部分只给出要求）。配置项见 [配置](configuration.md)。

`chanapp/scripts/` 里另有一套可作参考的实现：部署脚本 `deploy.sh`、systemd 单元模板 `chanapp.service`、配置校验脚本 `check_instance_config.py`，以及自检程序 `selfcheck.py` 与它的定时单元、安装脚本 `install_selfcheck.sh`。它们假设的目录布局与参数以各文件头部注释为准。

## 启动

```bash
CHANAPP_INSTANCE_CONFIG=/path/to/instance.json .venv/bin/python -m uvicorn chanapp.api.main:app --host 127.0.0.1 --port 8899
```

- **单 worker**：不要加 `--workers`，也不要让两个进程共用同一缓存根。事实库靠进程内锁加 `facts.writer.lock` 文件锁保证单写者，文件锁以非阻塞方式获取，多个进程会互相抢锁，拿不到的一方提交直接失败。
- **只监听回环地址**：应用本身没有登录与访问控制。需要从别的机器访问时，在前面放一个带认证与 TLS 的反向代理，或用 SSH 隧道。
- 启动时先校验实例配置、初始化实例目录，失败则进程退出（`Application startup failed`），不会带着错误配置运行。
- 日志写到标准输出与标准错误。`chanapp` 日志器为 INFO 级，`[timing]` 行记录每次图表请求的耗时。

用服务管理器（如 systemd）长期运行时，单元需要做到：

- 以普通用户运行，工作目录为仓库根，启动命令同上，异常退出后自动重启；
- 注入 `CHANAPP_INSTANCE_CONFIG`（指向代码目录之外的实例配置）、`COLLECTOR_ENABLED=1`、`TZ=Asia/Shanghai`；需要时注入路径变量；
- 凭据放在只有运行用户可读（权限 600）的环境文件里，由服务管理器读入；应用不会自己读取 `.env`。不要在环境文件里覆盖 `CHANAPP_INSTANCE_CONFIG`，以免实际读取的配置与部署前校验的不一致。

启动或部署后确认：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8899/    # 200
curl -s http://127.0.0.1:8899/api/status                             # mode 与 enabled
```

## 部署与更新

按下面的顺序做，任一步失败就停在那里，不要继续覆盖：

1. **配置先行**：实例配置、实例目录与凭据文件放在代码目录之外，由使用者维护；部署过程不生成、不同步、不覆盖它们。
2. **只读校验配置**：在部署机上用将要部署的代码、与服务相同的环境变量校验一次。它检查字段、来源能力、粒度、额度和真实模式凭据，失败只报变量名、不打印值：

   ```bash
   CHANAPP_INSTANCE_CONFIG=/path/to/instance.json .venv/bin/python -c "from chanapp.engine.kline import instance; s = instance.load_instance(); print('ok', s.mode, {m: (v.source, v.minute_fact_freq) for m, v in s.markets.items()}, s.per_minute, s.per_day)"
   ```

   也可以用 `.venv/bin/python chanapp/scripts/check_instance_config.py /path/to/instance.json [环境文件 ...]`：它按服务管理器 `EnvironmentFile` 的方式读入给出的环境文件后再校验，看到的凭据与服务一致；同样只报变量名。

3. **备份先于覆盖**：在同步新代码之前备份当前代码目录、个人状态（自选、查看记录、周期偏好）与事实库快照（见下节）。备份任一步失败（包括记录当前依赖版本失败）就中止部署。凭据不进备份。
4. **同步代码**：同步时排除实例目录、缓存根、虚拟环境、`.env` 与凭据文件，不要用会删除它们的方式覆盖。
5. **安装依赖**：按 `requirements.txt` 安装；虚拟环境不完整时中止，不要删掉重建正在使用的环境。
6. **重启并检查**：重启服务，检查首页返回 200、`/api/status` 的 `mode` 与预期一致；真实模式下 `enabled` 为 `true`。

**回退**：检出上一个可用的提交，重新执行第 4–6 步；事实库与个人状态不受代码回退影响。只有事实库本身损坏时才按下文从快照恢复。

**改分钟事实粒度**（`minute_fact_freq`）：重启后自动切换，日志出现「分钟事实粒度切换 … 绑定代次已推进」，旧粒度的事实只读保留、不删除；新粒度重新规划历史回填，首次打开分钟图会同步取窗口，`open_gaps` 暂时变多属正常。回退时改回原粒度并重启即可。

## 备份

事实库是缓存根下的 `facts.sqlite`（WAL 模式）。**不要直接复制运行中的主文件，也不要手工拼 `-wal`、`-shm`**：未写回主文件的数据在 `-wal` 里。用 SQLite 在线备份生成一致快照，服务运行中也可以执行。下面的脚本在任一步失败时停止；备份目录不存在时会以 0700 新建，已存在时必须属于当前用户：

```bash
set -euo pipefail
umask 077
DB=/path/to/cache/facts.sqlite
BACKUP_DIR=/path/to/backups
test -s "$DB" || { echo "facts.sqlite missing: $DB" >&2; exit 1; }
mkdir -p "$BACKUP_DIR"
test -O "$BACKUP_DIR" || { echo "backup dir not owned by $(id -un): $BACKUP_DIR" >&2; exit 1; }
chmod 700 "$BACKUP_DIR"
SNAPSHOT="$BACKUP_DIR/facts-$(date -u +%Y%m%dT%H%M%SZ).sqlite"
test ! -e "$SNAPSHOT" || { echo "snapshot exists: $SNAPSHOT" >&2; exit 1; }
.venv/bin/python - "$DB" "$SNAPSHOT" <<'EOF'
import sqlite3, sys
from chanapp.engine.kline import facts
facts.snapshot(facts.open_facts(sys.argv[1]), sys.argv[2])
check = sqlite3.connect(f"file:{sys.argv[2]}?mode=ro&immutable=1", uri=True)
result = check.execute("PRAGMA integrity_check").fetchone()[0]
if result != "ok":
    sys.exit(f"snapshot integrity_check failed: {result}")
EOF
chmod 600 "$SNAPSHOT"
ls -l "$SNAPSHOT"
```

- 先确认事实库路径存在：`facts.open_facts` 遇到不存在的路径会新建空库，路径写错时「备份」会成功复制一个空库。
- 快照与备份目录放在代码目录之外，只有运行用户可读写。
- 个人状态里的 `views.sqlite` 也是 SQLite 文件，应在服务停止时复制；`watchlist.json`、`periods.json` 可直接复制。
- 只含随包样本、没有导入过其他数据的 demo 实例可以随时用 `seed_demo` 重建，不必备份事实库。导入过自定义 CSV 的实例，要么按上面备份事实库，要么完整保留导入用的原始 CSV 与实例配置，以便重新导入。

## 恢复

从快照恢复会让事实库回到较早的时点，已打开页面持有的旧令牌必须失效，所以恢复后**必须生成新的运行身份**。步骤：先停止服务，再运行下面的脚本，最后启动服务。

脚本按顺序做五件事，任一步失败即停：核实快照可读且完整；新建一个本次专用、带时间戳的保留目录（0700，已存在则失败，不覆盖上一次恢复留下的文件）；把现有的 `facts.sqlite`、`-wal`、`-shm` 移进保留目录（不删除）；全部移走后才复制快照并再次核验；最后生成新运行身份。

```bash
set -euo pipefail
umask 077
D=/path/to/cache                                      # 缓存根；服务必须已停止
SNAPSHOT=/path/to/backups/facts-<时间戳>.sqlite
check() {
  .venv/bin/python - "$1" <<'EOF'
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro&immutable=1", uri=True)
result = conn.execute("PRAGMA integrity_check").fetchone()[0]
tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
if result != "ok" or "run_identity" not in tables:
    sys.exit(f"not a usable facts snapshot: {sys.argv[1]} ({result})")
EOF
}
check "$SNAPSHOT"
ASIDE="$D/restore-aside-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir "$ASIDE"
for f in facts.sqlite facts.sqlite-wal facts.sqlite-shm; do
  if [ -e "$D/$f" ]; then
    test ! -e "$ASIDE/$f"
    mv "$D/$f" "$ASIDE/$f"
  fi
done
test ! -e "$D/facts.sqlite"
cp "$SNAPSHOT" "$D/facts.sqlite"
chmod 600 "$D/facts.sqlite"
check "$D/facts.sqlite"
.venv/bin/python -c "
import sys; from chanapp.engine.kline import facts
print('run identity', facts.new_run_identity(facts.open_facts(sys.argv[1])))" "$D/facts.sqlite"
echo "previous files kept in $ASIDE"
```

启动后验收：带恢复前令牌的历史分页返回 409 `数据已更新`，页面整窗重载；真实模式下 `/api/status` 的 `enabled` 为 `true`。快照时点之后的事实由采集器按缺口与发现水位重新补取。确认无误后，保留目录可以按需清理。

## 状态检查

可以按下面的判据自行监控，判据都来自 `GET /api/status`。`chanapp/scripts/selfcheck.py` 是一个读取本机 `/api/status` 与采集器导出的交易日历、不取行情也不修数据的自检实现，可作参考。

| 检查 | 正常 | 异常时 |
| --- | --- | --- |
| 服务可达 | 首页 200，`/api/status` 返回 JSON | 进程未运行或启动失败，看服务日志 |
| 运行模式 | `mode` 与实例配置一致（`demo` 或 `real`） | 真实部署却报 `demo`：服务没有拿到 `CHANAPP_INSTANCE_CONFIG` |
| 采集器 | 真实模式下 `enabled` 为 `true`（开关打开，调度线程与历史线程都在运行） | `COLLECTOR_ENABLED=0`，或采集器启动失败（日志有「采集器启动失败」） |
| 新鲜度 | 自选各数据集 `stale` 为 `false` | 盘中超过 180 秒没有成功提交，或定稿截止后当日仍未定稿；先看来源是否在冷却、额度是否用尽（`budget`） |
| 缺口 | `open_gaps` 逐步下降 | 刚加入自选或刚改粒度时变多属正常；`known_gaps` 是重试 5 次后不再自动补取的缺口，需要人工处理 |
| 待核验 | `pending_review` 为 0 | 已收盘的值前后不一致，裁决前继续服务原值，当日不算完成 |

判断 stale 时注意时段：会话刚开始的几分钟里第一轮盘中数据可能还没落库；会话内的日线数据集、交易日开盘后到定稿截止之间的会话外时段本来就不判 stale。真实模式的采集器每天把交易日历导出到 `<缓存根>/selfcheck_calendar.json`（`/api/status` 的 `calendar_export` 给出路径），外部监控可以据此判断当前是否在交易时段。服务启停一律用服务管理器，不要用按命令行匹配的 `pkill`。

## 凭据更新

1. 在凭据环境文件里更新值（权限保持 600）。不要把值写进实例配置、仓库或日志。
2. 先按「部署与更新」第 2 步做一次只读校验。
3. 重启服务。真实模式在启动时检查所选来源的全部凭据，缺失或为空时只报变量名并拒绝启动；即使 `COLLECTOR_ENABLED=0` 也检查。

凭据过期或失效时，失败怎样归类由 adapter 抛出的异常决定：provider 初始化失败或 `ProviderConnectionError` 计入来源冷却（连续 3 次后该来源冷却 600 秒）；其他 `ProviderError` 只让该标的退避（连续 2 次后 300 秒）。两种情况都不会自动回落到冷备，页面表现为 stale。有到期时间的凭据请提前续期。

## 冷备切换

分析 K 线不自动跨来源回落。主来源长时间不可用时，可以手动把某个取数项切到安装清单里登记的冷备。切换必须针对实例配置所对应的那个事实库，并且先应用实例配置里的市场绑定：下面的脚本读 `CHANAPP_INSTANCE_CONFIG`、按配置算出事实库路径，再切换，事实库与配置因此一定属于同一实例。用与服务相同的环境变量运行（真实模式会照常检查凭据）：

```bash
# 先停止服务
set -euo pipefail
export CHANAPP_INSTANCE_CONFIG=/path/to/instance.json
.venv/bin/python - CN stock day_history '<冷备来源>' '原因与日期' <<'EOF'
import sys
from chanapp.engine import instance_paths
from chanapp.engine.kline import bindings, facts, instance
settings = instance.load_instance()                 # 校验并读取实例配置
bindings.configure(settings.markets)                # 应用实例的市场来源与粒度
db = instance_paths.resolve(settings.instance_dir).cache_dir / facts.DB_NAME
if not db.is_file() or db.stat().st_size == 0:
    sys.exit(f"facts.sqlite missing: {db}")
market, kind, item, target, reason = sys.argv[1:]
print(db, "binding_gen", bindings.switch(facts.open_facts(db), market, kind, item, target, reason=reason))
EOF
# 再启动服务
```

参数依次为市场（`CN`/`HK`）、标的类型（`stock`/`index`/`any`）、取数项、目标来源、原因；各取数项登记的冷备见安装清单 `catalog.py` 的 `BINDING_DEFAULTS`（每行第五项）。输出是事实库路径与新的绑定代次。目标只能是该取数项的主来源（实例配置所选）或安装清单登记的冷备，否则报错 `<来源> 不是 CN/stock/day_history 的主源或冷备`。切回主来源用同一脚本，把目标换成主来源名。

- 切换推进绑定代次，旧代次迟到的批次整批拒写，页面令牌随之变化（409 整窗重载）。
- 港股个股的日线或分钟历史切到冷备时，供应商前复权缓存同时冻结：前复权只服务到冻结点并标 stale；切回主来源后两个周期都重取成功才解冻。
- 只读查询事实库的方法见 [数据契约 · 只读查询事实库](data-contract.md#只读查询事实库)。
