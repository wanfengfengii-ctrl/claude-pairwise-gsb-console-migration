# 部署与迁移说明

## 1. 适用环境

当前版本面向 macOS，使用 Terminal、GNU Screen、Docker Desktop 和 Google Chrome。建议使用与 Claude Docker 镜像相同架构的电脑；当前默认镜像 `claude-eval-runtime:claude-2.1.269` 为 Apple Silicon `arm64` 镜像。

需要提前安装并登录：

- Python 3.9 或更高版本；
- Node.js 20 或更高版本及 npm；
- Docker Desktop，包含 Docker Compose；
- Google Chrome，安装在 `/Applications/Google Chrome.app`；
- Git 和 GitHub CLI；
- Codex CLI（使用 ChatGPT 桌面应用时会自动识别其内置 Codex）；
- macOS 自带的 `screen` 与 `osascript`。

认证检查：

```bash
gh auth login
gh auth status
codex login status
```

Claude 容器从 `~/.claude/settings.json` 的 `env.ANTHROPIC_AUTH_TOKEN` 或 `env.ANTHROPIC_API_KEY` 读取凭据。该文件只保存在本机，不要提交到 Git。

## 2. 准备 Claude Docker 镜像

目标电脑必须先存在基础 Claude 镜像：

```bash
docker image inspect claude-eval-runtime:claude-2.1.269
```

安装脚本会基于该镜像构建 `claude-eval-runtime:prepared-2.1.269`，预装 Python
venv/pip、Go、pnpm、TypeScript、tsx、Vite、Vitest、CMake、SQLite、编译、压缩和常用网络诊断工具。这样 A/B 新会话可以
直接安装项目依赖；基础镜像仍保留，便于以后重建预装镜像。也可手动执行：

```bash
./scripts/build_claude_runtime.sh
```

如果镜像只在原电脑上，可离线迁移：

```bash
# 原电脑
docker save claude-eval-runtime:claude-2.1.269 | gzip > claude-eval-runtime-2.1.269.tar.gz

# 把 tar.gz 复制到目标电脑后
gunzip -c claude-eval-runtime-2.1.269.tar.gz | docker load
docker image inspect claude-eval-runtime:claude-2.1.269
```

目标电脑架构不同时应重新构建对应架构镜像，并在 `config.env` 中修改 `PAIRWISE_CLAUDE_IMAGE`。

## 3. 全新安装

这次只迁移程序及题目，请以本私有迁移仓库的[题目迁移步骤](../migration/README.md)为准，先导入题目包再安装服务。不要克隆旧的公开仓库或复制旧电脑的 `.data`、`projects`。

```bash
git clone git@github.com:wanfengfengii-ctrl/claude-pairwise-gsb-console-migration.git
cd claude-pairwise-gsb-console-migration
python3 scripts/migrate_ready_tasks.py import \
  --bundle migration/ready-tasks.json \
  --archive-bundle migration/completed-prompt-dedup.json \
  --db "$HOME/Library/Application Support/Claude A-B GSB Console/.data/pairwise.db" \
  --baseline-dir "$HOME/Library/Application Support/Claude A-B GSB Console/.data/task-baselines" \
  --apply
chmod +x scripts/*.sh
./scripts/install_launch_agent.sh
```

安装脚本会先构建预装开发镜像，再执行完整预检并启动服务。需要单独诊断环境时再运行
`./scripts/preflight.sh`。

首次执行安装脚本会创建：

```text
~/Library/Application Support/Claude A-B GSB Console/config.env
```

脚本会根据当前 `gh` 登录账号填写 GitHub Owner 和 noreply 邮箱，并把提交人设为刘昱。检查该文件；如需修改，保存后重新运行安装脚本。升级时脚本不会覆盖它。

主要配置示例见仓库根目录的 `config.example.env`。Pair 并发可设为 1–4；Claude
开发终端并发可独立设为 1–8，默认 3。比如 Pair 并发为 2、终端并发为 3 时，
第二个 Pair 的一侧会等待空位，已有侧完成后再自动启动。

安装完成后验证：

```bash
curl -fsS http://127.0.0.1:8865/api/health
launchctl print "gui/$(id -u)/com.local.claude-pairwise-gsb-console" | grep 'state ='
open http://127.0.0.1:8865
```

进入“系统设置”，确认 Git/GitHub、Codex CLI、Claude Docker 和浏览器录像四项均显示可用。

## 4. 安装 SOLO-QA 提交小助手

1. 在 Chrome 打开 `chrome://extensions/`。
2. 打开右上角“开发者模式”。
3. 点击“加载未打包的扩展程序”。
4. 选择：

```text
~/Library/Application Support/Claude A-B GSB Console/app/chrome-solo-qa-gsb-helper
```

5. 打开并登录 `https://solo2.jzxhnh.com`。
6. 刷新 `http://127.0.0.1:8865/#exports`，页面应显示“提交助手已连接”。

小助手固定连接本机 `8865` 端口。修改端口会使扩展无法连接，除非同步修改扩展清单和后台脚本后重新加载。

## 5. 旧系统题目导入

默认读取当前用户目录下：

```text
~/Library/Application Support/Claude Eval Console/.data/console.db
```

没有旧系统时会直接跳过，不影响新系统启动。需要指定其他位置时，在 `config.env` 增加：

```bash
PAIRWISE_OLD_DB=/absolute/path/to/console.db
```

系统只导入旧库中的困难/地狱 0–1 和 Feature 候选，仍需重新完成准入检查。历史 Bug、简单和中等题不会导入。

新生成的 0–1 与 Feature 采用旧系统的范围预算和可读性校验：一个工程核心、一条主流程、3～4 个实现模块，题面保持 4～6 句且限制单句长度与分号数量。服务升级时只会停用尚未开始、且明显超过当前范围预算的自动生成题；已经进入 Pair 的开发、轨迹和产物不受影响。

## 6. 题目迁移范围

本次迁移在目标电脑创建新数据库，只导入 `migration/ready-tasks.json` 中未使用的可用题目，以及 `migration/completed-prompt-dedup.json` 中已完成题面的只读去重索引。具体检查、导入和复核命令见[题目迁移说明](../migration/README.md)。不要复制原电脑的 `pairwise.db`、`projects`、录像或轨迹；它们包含与旧电脑绑定的绝对路径，也不在本次授权范围内。Feature/Bug 题的基线代码按仓库 URL 与提交 SHA 从 GitHub 获取，不随题目包上传。

## 7. 升级

```bash
cd claude-pairwise-gsb-console
git pull --ff-only
python3 -m unittest discover -s tests -v
npm ci
npm test
./scripts/install_launch_agent.sh
```

安装脚本在每次升级前使用 SQLite Backup API 创建一致性备份：

```text
~/Library/Application Support/Claude A-B GSB Console/.data/backups/
```

`config.env`、数据库、项目、录像和轨迹位于运行副本之外，不会被 `rsync --delete` 删除。

升级完成后打开“题目池”，点击“一键自动运行完整流程”即可持续维持 3 个活动 Pair。该开关保存在数据库中，服务重启后会继续生效；需要暂停自动补位时点击“停止自动运行”，已启动的项目不会被强制中断。

Claude 开发遇到 429、504、证书或连接类 API Error 时先记录错误并继续等待该侧本轮的 60 分钟无业务代码时限，不立即归档或打断，也不停止另一侧。满 60 分钟仍无代码时才把该侧计为一次 Pair 级失败并建立新 Session 重试一次。A/B 合计第二次失败时只停止本次失败侧；另一侧继续到完成或触发自己的 60 分钟时限，随后再退役 Pair 并换下一道合格 0–1 题。

Feature 和 Bug 修复在启动 A/B 前会先对共同基线做清洁 Docker 预检。A/B 任一侧完成后，
系统会立即校验该侧首轮轨迹并运行 Docker 清洁验收；验收结果与当前提交 SHA 绑定，已完成
的验收不会重复运行。A/B 当前提交通过后录制真实功能；Claude 已完成但原始交付无法构建、
启动或通过测试时保留提交和失败证据，不再发起 Claude 返修，而是录制 12–16 秒的终端失败
命令画面。待两侧录像结束后，系统根据真实轨迹和验收结果生成 GSB 与提交数据。首轮题面发送后 15 分钟仍无业务代码会告警，60 分钟仍无业务代码会
终止本次开发；第二次尝试达到 40 分钟仍无代码且轨迹与上一次完全相同时提前停止重复空跑，轨迹不同则继续等满 60 分钟。A/B 容器都准备完成后先
向 A 发送题面，默认 30 秒后再向 B 发送；两侧分别从各自实际发送时刻开始计算时间窗口。

## 8. 日志、重启与卸载

日志：

```text
~/Library/Logs/Claude A-B GSB Console/server.log
~/Library/Logs/Claude A-B GSB Console/server-error.log
```

重启：

```bash
launchctl kickstart -k "gui/$(id -u)/com.local.claude-pairwise-gsb-console"
```

停止自动启动：

```bash
launchctl bootout "gui/$(id -u)/com.local.claude-pairwise-gsb-console"
rm "$HOME/Library/LaunchAgents/com.local.claude-pairwise-gsb-console.plist"
```

上述命令不会删除数据库和项目。确认不再需要数据后，再手动删除 `~/Library/Application Support/Claude A-B GSB Console`。

## 9. 常见问题

- 页面打不开：运行 `curl http://127.0.0.1:8865/api/health`，再查看 `server-error.log`。
- Docker 不可用：先启动 Docker Desktop，然后运行 `scripts/preflight.sh`。
- Claude 容器起不来：检查镜像架构、镜像标签和 `~/.claude/settings.json` 的凭据。
- GitHub 建仓失败：确认 `gh auth status`、仓库权限和 `config.env` 中的 Owner、邮箱。
- Codex 作业失败：确认 `codex login status`，并从“Codex 作业”查看错误。
- 录像失败：确认 Chrome 位于 `/Applications`，重新运行安装脚本安装 Playwright FFmpeg，再检查项目的 Compose 端口配置。
- 小助手未连接：确认扩展已启用、加载的是运行副本目录，并刷新本地导出页。
