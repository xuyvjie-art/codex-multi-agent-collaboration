# 本机单写锁

## 适用与限制

`coordination_lock.py` 是合作式锁：同一台 macOS / Linux 机器上的参与者使用同一个私有锁目录，并约定写入共享状态时先持锁。它使用 `fcntl`、POSIX 路径和信号机制；不支持 Windows。它不能阻止同一用户绕过工具写入，也不是网络文件系统、跨机器分布式锁或数据库事务。调用命令不得后台脱离进程组继续写入。

锁目录必须事先存在，使用绝对路径、由当前用户拥有、不可被组或其他用户写入，路径组件不可为符号链接。锁文件和审计文件只允许当前用户访问。所有会话必须指向同一个目录；`resource` 只是审计描述，不会分出多把锁。

## 用法

```bash
LOCK_DIR="$(pwd -P)/.coordination-lock"
mkdir -m 700 "$LOCK_DIR"  # 目录尚不存在时执行一次
python3 scripts/coordination_lock.py --dir "$LOCK_DIR" status
python3 scripts/coordination_lock.py --dir "$LOCK_DIR" run \
  --owner "session-a" --resource "shared-state" --ttl 300 -- \
  python3 -c 'print("replace with your bounded foreground write")'
```

`run` 优先于手动获取：它只在取得锁后执行命令；正常退出后释放。异常强制关闭可能留下锁，命令子进程也可能继续运行。其他写入者应停止并检查，不得因超时就抢占。

手动模式的 `acquire` 会返回 token，`release --token` 使用该 token；不要把它写到公开报告。`status` 隐藏 token，但同一系统用户可以读取锁文件，因此这不是同用户之间的安全隔离。审计记录不保存 token。

## 恢复

1. 使用 `status` 查看锁记录和当前记录的 SHA-256。
2. 确认原写入进程及其子任务已经停止，记录证据和恢复原因。
3. 对完整记录，仅在到期后以刚才读取的精确摘要执行：

```bash
python3 scripts/coordination_lock.py --dir "$LOCK_DIR" recover \
  --sha256 "从 status 读取的摘要" \
  --reason "确认原写入者停止的依据" --confirm-writer-stopped
```

摘要变化、记录不存在或完整记录未到期时恢复失败。不完整记录没有可靠到期时间，仍需确认原写入者停止并提供精确摘要和原因。恢复后必须重新取得锁才可写入；不要直接删除锁文件。审计失败时操作保守失败并保留锁，先核查现场。审计记录并非防篡改日志。
