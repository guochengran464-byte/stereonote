# StereoNote Skill

**简介：** 面向 DCS / StereoNote 科研计算环境的 Agent Skill。通过用户已经登录的 Microsoft Edge 与 Jupyter 工作区，让 Claude Code 或 Codex 在没有 SSH 的情况下安全地检查文件、执行短任务，并提交可脱离 Agent 持续运行的长任务。

**开发者：guochengran**  
**当前版本：v3.2.1**

通过**已经登录 DCS / StereoNote 的 Microsoft Edge**，让 Claude Code 或 Codex
在没有 SSH 的情况下检查服务器文件、运行短命令，以及提交可脱离 Agent 持续运行的
Jupyter/Linux 长任务。

> **v3.2.1 是公开发布加固版。** 这个 Skill 具有真实的服务器代码执行能力，不是沙箱。
> 因此 Codex 默认关闭模型隐式调用；Claude Code 版本要求用户明确调用/明确要求操作 DCS/StereoNote。写文件、错误传播、输出大小和长任务均做了额外保护。

## 适用环境

当前控制端仅正式支持 **Windows 原生环境**：

- Windows + Microsoft Edge
- Edge 已安装并启用兼容的 `kimi-webbridge` 扩展
- 用户已在该 Edge profile 中登录 DCS/StereoNote
- `~/.kimi-webbridge/bin/kimi-webbridge.exe` 已安装
- Python 3
- Python 包 `requests`

DCS/StereoNote 服务器本身仍是 Linux 环境。

## 安装

### 0. 首次安装 Kimi WebBridge

如果电脑上还没有 Kimi WebBridge，请先完成 WebBridge 安装，再安装/使用 StereoNote Skill。

完整图文步骤与故障排查见：

**[Kimi WebBridge 安装指南（Windows + Edge）](docs/KIMI_WEBBRIDGE_INSTALL.md)**

最短流程是：

1. 从 Kimi 官方 WebBridge 页面安装 **Windows + Edge + Local Agent** 组件；
2. 确认 Kimi WebBridge 扩展安装在你登录 DCS/StereoNote 的**同一个 Edge profile**；
3. 确认本地桥接程序可用；
4. 安装 StereoNote 后运行：

```bash
python scripts/sn.py doctor
```

当前公开 smoke test 已验证 **Kimi WebBridge v1.11.1** 可完成 StereoNote 的核心连接流程。升级 WebBridge 后建议先在非生产 workspace 重新运行 `doctor` 和最小 smoke test。

### 1. 安装 Python 依赖

```bash
python -m pip install -r requirements.txt
```

### 2. 安装 Skill

Claude Code 用户级目录：

```text
~/.claude/skills/stereonote/
```

Codex 用户级目录：

```text
~/.agents/skills/stereonote/
```

把本仓库完整内容放到对应目录即可。不要只复制 `SKILL.md`，因为运行还依赖
`scripts/`、`agents/` 等文件。

由于这是有写入和远程执行能力的 Skill，v3.2.1 默认要求用户**明确调用/明确要求操作
DCS/StereoNote**，而不是看到普通的 `/data/work` 路径就自动触发。

## 每次连接工作区

先运行环境检查：

```bash
python scripts/sn.py doctor
```

每次开始新的浏览器/工作区会话时：

1. 在自己的 Edge 中手动打开目标“个性分析”。
2. 等待工作区启动完成。
3. 复制当前地址栏 URL 并提供给 Agent。
4. 让 Agent 执行：

```bash
python scripts/sn.py connect --url "<当前 StereoNote workspace URL>"
```

v3.2.1 只会导航到本次明确提供的 URL，并在 Jupyter 探针成功后才保存
`projectId` 和 `workspaceId`。连接失败不会污染旧配置，完整 URL 不会写进 Git 仓库。

Windows 默认配置位置：

```text
%APPDATA%\stereonote\config.json
```

因此不同用户可以共用同一份 Git 仓库，而不会互相覆盖 workspace 配置，也不容易把
个人 workspace URL 误提交到 GitHub。

`connect --url` 是唯一允许创建或替换受控浏览器标签页的命令。其他命令发现标签页
缺失时会要求重新提供当前 URL，不会根据旧配置偷偷打开工作区。

## 常用命令

```bash
# Jupyter 状态
python scripts/sn.py probe

# 目录和文本预览（有大小上限）
python scripts/sn.py ls work/my_project
python scripts/sn.py cat work/my_project/run.py

# 生信文件结构检查
python scripts/sn.py inspect /data/work/my_project/data.h5ad
python scripts/sn.py inspect /data/work/my_project/object.rds
python scripts/sn.py inspect /data/work/my_project/table.tsv
python scripts/sn.py inspect /data/work/my_project/result.parquet
python scripts/sn.py inspect /data/work/my_project/notebook.ipynb

# 短 Python / shell
python scripts/sn.py run-python --code "print(1 + 1)"
python scripts/sn.py run-shell --cwd /data/work/my_project --cmd "python check.py"
```

输出过大时会明确标记为 truncated，而不是无限量把 stdout/stderr 塞进 Agent 上下文。
`run-shell` 的底层 stdout/stderr 使用临时文件承接，再只读取有界尾部。单次 `write` 和
`run-python` 文本 payload 上限为 1 MiB；超出时直接失败，不静默截断可执行内容。
同步 Python/shell 最长 50 秒，可能更久的工作必须使用 `submit`。

文件 API 使用 `work/...` 表示 `/data/work/...`；`run-shell` 默认 cwd 已经是
`/data/work`，shell 命令应使用普通 Linux 相对路径或绝对路径。

## 写文件：默认禁止覆盖

新文件：

```bash
python scripts/sn.py write work/agent_outputs/result.txt --content "hello"
```

如果文件已经存在，命令默认失败：

```text
file_exists_use_overwrite
```

只有你**明确决定替换该文件**时才使用：

```bash
python scripts/sn.py write work/agent_outputs/result.txt \
  --content "replacement" \
  --overwrite
```

默认创建使用服务器端 `O_EXCL`，即使两个 Agent 并发写同一新文件，也只能有一个成功。
文件权限设置为 `0600`，成功写入后还会做 readback 校验。内核写入失败、目标类型异常或
读回不一致都按失败处理。

## 长任务

假设脚本位于：

```text
/data/work/my_project/big_pipeline.py
```

正确提交方式：

```bash
python scripts/sn.py submit \
  --cwd /data/work/my_project \
  --cmd "python big_pipeline.py"
```

默认 `--cwd` 是 `/data/work`，所以脚本不在 `/data/work` 根目录时建议始终显式写
`--cwd`。

返回 `job_id` 后可以按需检查：

```bash
python scripts/sn.py poll <job_id>
python scripts/sn.py artifacts <job_id>
```

每个任务的控制记录位于：

```text
/data/work/agent_jobs/<job_id>/
```

控制目录、命令和日志使用 `umask 077`。命令本身会为了可复现性持久化，因此不要把 token、
密码等秘密直接写入 `submit --cmd`。

任务脚本中还可以使用：

```text
$SN_JOB_DIR
$SN_ARTIFACT_DIR
```

推荐把需要交付的结果写入 `$SN_ARTIFACT_DIR`。artifact 清单会记录文件大小；不超过
64 MiB 的文件计算 SHA-256，大文件默认跳过全文件 hash，避免一个几十 GB 的 `.h5ad`
或 `.rds` 已经算完后又因为 checksum 再完整读盘一次。

对于长时间计算，推荐 Agent 的行为是：**检查命令 → 提交 → 确认 runner 正常启动 →
返回 job_id → 停止当前响应**。不要让 LLM 持续轮询日志等待计算结束。

## v3.2.1 主要安全修复

- 修复 Jupyter runtime “内部失败、外层仍 `ok:true`”的假成功问题。
- CLI 对明确失败返回非 0 exit code，便于 Agent/CI 正确判断。
- `write` 使用服务器端原子 no-clobber；覆盖必须显式 `--overwrite`。
- 长任务增加明确的 `--cwd`，修复脚本因 job 工作目录改变而找不到的问题。
- workspace 配置移到用户目录，只持久化必要 ID，不保存完整 URL。
- shell/Jupyter/文本/目录输出增加边界，避免超大输出吞内存或上下文。
- 大型 artifact 跳过默认全文件 SHA-256。
- Codex 通过官方 `agents/openai.yaml` 关闭隐式 Skill 调用。
- 只有 `connect --url` 能导航，并且只使用用户本次明确提供的工作区 URL。
- WebBridge 地址强制限制为带显式端口的本机 loopback HTTP 地址。
- iframe origin/path 使用严格匹配，后台任务状态使用 `umask 077`。
- 新增 `doctor` 预检、CI、LICENSE、SECURITY、CHANGELOG 和 release checklist。

完整变更见 [`CHANGELOG.md`](CHANGELOG.md)，安全模型见 [`SECURITY.md`](SECURITY.md)，WebBridge 兼容边界见 [`COMPATIBILITY.md`](COMPATIBILITY.md)。

## 测试

```bash
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
node --check scripts/jupyter_runtime.js
```

测试包含：路径与 job ID 校验、bounded inspect、poll 协议、真实 Node runtime 错误传播、
no-clobber 约束、workspace URL 去敏、large-artifact hash 策略，以及 POSIX 环境下真实
启动 detached runner 的工作目录集成测试。

## 安全提醒

`run-shell` 和 `submit --cmd` **不是 OS sandbox**。它们拥有当前 DCS/Jupyter 账号本身
拥有的权限。不要把不可信命令直接交给它执行；删除、覆盖、安装软件、杀进程、大规模
数据传输等操作应在用户明确同意后执行。

浏览器 cookie、Authorization、token、`_xsrf` 等值不应出现在输出、日志或 Git 仓库中。

## 已知限制（v3.2.1）

本版本按“先公开、持续维护”的方式发布，以下事项保留为后续版本维护记录：

- Codex 已通过 `agents/openai.yaml` 硬关闭隐式调用；Claude Code 当前主要依赖 Skill 描述中的显式调用约束，尚未提供独立的 Claude 专用 metadata 包。
- 当前会严格验证 DCS Jupyter iframe 的 origin/path，但尚未把每次特权操作与最初连接的 workspace identity 做强绑定；切换工作区后应重新执行 `connect --url`。
- DCS 默认 ACL 曾使 `umask 077` 产生比预期更宽的权限；v3.2.1 已增加显式 `chmod 700/600`，但修复后的 detached-job 权限路径仍建议在更多真实容器中继续回归验证。

完整发布审计见 [`docs/RELEASE_AUDIT_v3.2.1.md`](docs/RELEASE_AUDIT_v3.2.1.md)。这些限制不会被隐藏，后续修复将记录在 `CHANGELOG.md`。

## License

MIT，见 [`LICENSE`](LICENSE)。
