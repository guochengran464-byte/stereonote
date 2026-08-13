# Kimi WebBridge 安装指南（Windows + Edge）

本文面向第一次安装 StereoNote Skill 的 Windows 用户。

StereoNote **不会自行安装或升级 Kimi WebBridge**。WebBridge 负责让本地 Agent 使用你已经登录的 Edge 浏览器；StereoNote 再通过这个本地桥接进入 DCS/StereoNote 的 Jupyter 工作区。

> 本项目当前正式验证的控制端组合是 **Windows + Microsoft Edge + Kimi WebBridge + Python**。公开 smoke test 曾验证 WebBridge **v1.11.1** 可以完成 `doctor`、显式 workspace 连接、Jupyter 探针和原子写入等核心操作。未来版本不保证天然兼容，升级后请先运行本文末尾的检查。

## 1. 使用官方安装入口

优先使用 Kimi 官方 WebBridge 页面，不要从不明来源下载扩展或 `kimi-webbridge.exe`：

- 产品页：<https://www.kimi.com/zh-cn/features/webbridge>
- 官方帮助：<https://www.kimi.com/zh-cn/help/kimi-webbridge/kimi-webbridge-introduction>

Kimi 官方当前支持 Windows/macOS，浏览器支持 Chrome/Edge，并明确支持搭配 Claude Code、Codex 等 Local Agent。

对于本项目，请选择 **Windows + Edge + Local Agent** 的安装流程。

官方页面会提供当前版本对应的 Windows/PowerShell 安装方式。**以官方页面当时给出的命令为准**，本仓库不复制固定的远程安装命令，避免官方安装地址或版本更新后本文留下过期命令。

## 2. 安装 Edge 扩展

推荐优先从 Edge Add-ons 安装 Kimi WebBridge。

如果应用商店不可用，可以按 Kimi 官方帮助中心的手动方式安装：

1. 从 Kimi WebBridge 官方页面下载扩展安装包。
2. 解压安装包。
3. 在 Edge 地址栏打开：

   ```text
   edge://extensions/
   ```

4. 打开“开发人员模式”。
5. 点击“加载解压缩的扩展”。
6. 选择刚刚解压的 WebBridge 扩展目录。
7. 确认扩展列表里出现 Kimi WebBridge，并保持启用。
8. 建议把 WebBridge 固定到浏览器工具栏，便于检查连接状态。

**必须使用你平时登录 DCS/StereoNote 的同一个 Edge profile。** 如果扩展装在 Profile A，而 DCS 登录态在 Profile B，StereoNote 无法借用正确的浏览器登录态。

## 3. 安装本地桥接服务

按照 Kimi 官方页面的“搭配本地 Agent” → Windows 流程完成安装。

StereoNote v3.2.1 当前期望本地可执行文件位于：

```text
~/.kimi-webbridge/bin/kimi-webbridge.exe
```

在 Windows 中，`~` 表示当前用户 Home 目录，例如：

```text
C:\Users\<你的用户名>\.kimi-webbridge\bin\kimi-webbridge.exe
```

不要把来源不明的同名 EXE 手动塞到这个目录来绕过检查。

## 4. 安装 StereoNote 后先跑 doctor

进入 StereoNote Skill 目录后执行：

```bash
python scripts/sn.py doctor
```

正常情况下应至少确认：

- Python 环境可用；
- `requests` 已安装；
- WebBridge 本地服务可访问；
- WebBridge 地址是本机 loopback HTTP + 显式端口；
- 浏览器扩展已连接；
- 能读取 WebBridge 返回的状态/版本信息。

StereoNote 默认按当前 v1.x-style WebBridge contract 与本地服务通信；默认端点使用：

```text
http://127.0.0.1:10086
```

如果 `doctor` 没通过，不要继续执行 `connect`、`write`、`run-shell` 或 `submit`。

## 5. 第一次连接 DCS/StereoNote

1. 用安装 WebBridge 的**同一个 Edge profile**登录 DCS/StereoNote。
2. 手动打开目标“个性分析”/workspace。
3. 等工作区真正启动完成。
4. 复制当前地址栏 URL。
5. 在 StereoNote 目录运行：

```bash
python scripts/sn.py connect --url "<当前 StereoNote workspace URL>"
```

连接成功后再检查：

```bash
python scripts/sn.py probe
python scripts/sn.py ls work
```

`connect --url` 只有在真实 Jupyter probe 成功后才保存 workspace ID；普通操作不会根据旧 ID 偷偷导航到另一个 workspace。

## 6. 常见问题

### `doctor` 提示找不到 kimi-webbridge.exe

先确认官方 Local Agent 安装步骤已经完成，并检查：

```text
C:\Users\<你的用户名>\.kimi-webbridge\bin\kimi-webbridge.exe
```

如果官方未来改变安装目录，请先提 Issue，不建议通过复制未知 EXE 强行伪造旧目录。

### daemon 可访问，但 extension 未连接

依次检查：

1. Edge 是否正在运行；
2. Kimi WebBridge 扩展是否启用；
3. 是否装在当前正在使用的 Edge profile；
4. DCS 页面是否也是在该 profile 中打开；
5. 重新启动 Edge / WebBridge 后再次运行 `doctor`。

### 扩展已经安装，但 StereoNote 找不到 DCS 登录态

最常见原因是 **Edge profile 不一致**。WebBridge 使用的是浏览器当前 profile 的真实登录态，不会把另一个 profile 的 cookie 自动搬过来。

### 升级 WebBridge 后突然不能用

不要直接假设是 DCS 或 StereoNote 数据问题。先运行：

```bash
python scripts/sn.py doctor
```

然后在**非生产 workspace**执行最小 smoke test：

```bash
python scripts/sn.py connect --url "<current workspace URL>"
python scripts/sn.py probe
python scripts/sn.py ls work
```

如果新版本改变 `/status`、`/command` 或 CDP 行为，请在仓库提交兼容性 Issue，并附上**脱敏后的** `doctor` 输出。不要贴 cookie、token、workspace ID 或科研数据。

## 7. 安装完成后的最小验收

建议第一次安装只在临时路径做验证：

```bash
python scripts/sn.py doctor
python scripts/sn.py connect --url "<current workspace URL>"
python scripts/sn.py probe
python scripts/sn.py ls work
python scripts/sn.py write work/agent_outputs/stereonote_install_smoke.txt --content "ok"
```

最后一条再次执行相同命令时应因为默认 no-clobber 而失败，而不是覆盖已有文件。

确认以上流程正常后，再让 Agent 操作正式科研目录。

---

开发者：**guochengran**
