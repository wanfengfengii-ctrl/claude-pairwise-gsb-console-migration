# 仅迁移可用题目

`ready-tasks.json` 是导出时处于 `ready`、尚未绑定 Pair 的题目快照。它只含题面、验收要求、分类/难度、评估元数据，以及 Feature/Bug 题所需的基线仓库 URL 与精确提交 SHA。不含原数据库、Pair、评价、提交状态、日志、轨迹、录像或已使用/废弃题目。

新电脑先克隆本私有仓库并完成程序安装；不要复制旧电脑的 `.data` 或 `projects` 目录。然后在仓库根目录执行：

```bash
python3 scripts/migrate_ready_tasks.py import \
  --bundle migration/ready-tasks.json \
  --db .data/pairwise.db \
  --baseline-dir .data/task-baselines
```

上面的命令只检查题目。检查通过后，使用同样参数并加 `--apply` 才会导入。Feature/Bug 的源代码不在题目包里；导入时从各自的基线仓库拉取并核对 SHA，所以新电脑必须能访问这些仓库。如果运行服务时使用了自定义 `PAIRWISE_DATA_DIR`，`--db` 和 `--baseline-dir` 必须改成该目录下的路径。先导入，再启动自动流水线；避免生成新题抢占任务池。

实际切换电脑前，原电脑可以重新导出当时仍可用的题目：

```bash
python3 scripts/migrate_ready_tasks.py export \
  --db '/Users/niuyuhang/Library/Application Support/Claude A-B GSB Console/.data/pairwise.db' \
  --output migration/ready-tasks.json
```

导出会重新检查每道题的状态和本地精确基线提交；不能远端重建的题目会使导出失败，不会悄悄把不可用题算进库存。题目池会继续运行，因此这个快照只代表导出时刻，不保证明天仍未被使用。
