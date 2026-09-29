# 新电脑迁移：程序、可用题目与已完成题面去重

本私有仓库是迁移快照。它不包含旧电脑的数据库、A/B 产物、答卷、评价、日志、轨迹或录像；也不包含账号凭据。先在新电脑登录有权访问本仓库及各题基线仓库的 GitHub 账号，再克隆：

```bash
git clone git@github.com:wanfengfengii-ctrl/claude-pairwise-gsb-console-migration.git
cd claude-pairwise-gsb-console-migration
gh auth status
```

`ready-tasks.json` 是导出时处于 `ready`、尚未绑定 Pair 的题目快照。它只含题面、验收要求、分类/难度、评估元数据，以及 Feature/Bug 题所需的基线仓库 URL 与精确提交 SHA。

`completed-prompt-dedup.json` 单独保存已完成 Pair 对应的题面。新电脑把它们导入为 `archived_dedup`，只供出题去重，不会排队、创建 Pair 或充当可用题目。归档里没有产物代码、A/B 答卷、评价或提交记录。两个文件都不含原数据库、日志、轨迹或录像；失败、废弃和未完成题目不迁移。

不要复制旧电脑的 `.data` 或 `projects` 目录。在安装和启动程序**之前**，于仓库根目录执行题目包检查：

```bash
python3 scripts/migrate_ready_tasks.py import \
  --bundle migration/ready-tasks.json \
  --archive-bundle migration/completed-prompt-dedup.json \
  --db "$HOME/Library/Application Support/Claude A-B GSB Console/.data/pairwise.db" \
  --baseline-dir "$HOME/Library/Application Support/Claude A-B GSB Console/.data/task-baselines"
```

上面的命令只检查题目。检查通过后，使用同样参数并加 `--apply` 导入；重复执行不会重复建题。Feature/Bug 的源代码不在题目包里；导入时从各自的基线仓库拉取并核对精确 SHA，所以新电脑必须能访问这些仓库。按[部署说明](../docs/DEPLOYMENT.md)准备好 Docker Desktop、Claude 基础镜像和本机凭据后，运行 `./scripts/install_launch_agent.sh` 安装程序，核对页面题目池数量，再按需要开启自动流水线。若使用自定义 `PAIRWISE_DATA_DIR`，把上述两个路径改到该目录下。新库默认不自动运行流水线，因此不会在导入前抢占题目。

已完成题目以 `archived_dedup` 保存，只参与后续出题查重，不会成为可调度的 `ready` 题；迁入后可在数据库里检查：

```bash
sqlite3 "$HOME/Library/Application Support/Claude A-B GSB Console/.data/pairwise.db" \
  'SELECT status,COUNT(*) FROM tasks GROUP BY status;'
```

实际切换电脑前，原电脑可以重新导出当时仍可用的题目：

```bash
python3 scripts/migrate_ready_tasks.py export \
  --db '/Users/niuyuhang/Library/Application Support/Claude A-B GSB Console/.data/pairwise.db' \
  --output migration/ready-tasks.json \
  --archive-output migration/completed-prompt-dedup.json
```

导出会重新检查每道题的状态和本地精确基线提交；正式切换前还需从空库试导入，确认远端仓库确实包含这些提交。题目池会继续运行，因此快照只代表导出时刻，不保证之后仍未被使用。切换前重新导出并提交推送，避免把已消耗的题当作库存。
