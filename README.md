# MealCircuit（食回路）

> 记录一餐，校准长期趋势。

MealCircuit 是本地优先的长期饮食反馈工作台。它把照片任务、原材料、食品营养库、每日记录和长期记忆保存到仓库外的 SQLite 数据目录，再由 Codex、Claude Code 或其他 Agent 读取完整上下文并提交结构化分析。

## 快速开始

环境要求：Python 3.11+；Windows 可直接使用仓库内 PowerShell 脚本。项目运行时不需要第三方 Python 包。

```powershell
python -m mealcircuit.agent_cli init
python -m mealcircuit.agent_cli doctor
.\start.ps1
```

首次初始化后，在 `doctor` 显示的私人目录中填写 `profile.md` 和 `settings.json`。打开 <http://127.0.0.1:8765>，停止服务使用 `Ctrl+C`。

```powershell
.\test.ps1
python tools\release_check.py
```

## 私人数据

运行数据不存放在源码仓库。默认位置：

- Windows：`%LOCALAPPDATA%\MealCircuit`
- macOS：`~/Library/Application Support/MealCircuit`
- Linux：`$XDG_DATA_HOME/mealcircuit` 或 `~/.local/share/mealcircuit`

可通过 `MEALCIRCUIT_HOME` 修改整个私人目录，通过 `MEALCIRCUIT_DB` 单独覆盖数据库路径，通过 `MEALCIRCUIT_PORT` 修改端口。`DIETOS_DB` 和 `DIETOS_PORT` 仅为 `v0.1` 迁移兼容项。

从旧工程迁移时先预览，再实际复制：

```powershell
python -m mealcircuit.agent_cli migrate-data --from-repo <旧工程路径>
python -m mealcircuit.agent_cli migrate-data --from-repo <旧工程路径> --apply
```

迁移只复制数据，不删除源文件；数据库使用 SQLite Backup API，并校验完整性、表行数和逻辑摘要。

## Agent 工作流

```powershell
python -m mealcircuit.agent_cli pending
python -m mealcircuit.agent_cli context <任务ID> --output context.json
python -m mealcircuit.agent_cli schema photo
python -m mealcircuit.agent_cli complete <任务ID> --file result.json
python -m mealcircuit.agent_cli correct <任务ID> --text "用户确认的更正"

python -m mealcircuit.agent_cli day-context 2026-01-01 --output context.json
python -m mealcircuit.agent_cli schema daily
python -m mealcircuit.agent_cli day-complete 2026-01-01 --file result.json
```

照片分析必须使用区间并列出不可见油、酱汁、重量和品牌等未知项。每日复盘固定包含事实、推断、1–3 条核心建议、无需调整项、风险信号、次日菜单和全部高优先级食品裁决。个人目标与用餐环境来自私人 `settings.json`，不写死在公开代码中。

## 安全与真实边界

- MealCircuit 自身不调用外部模型 API、不要求 API Key，也不包含遥测。
- 使用云端 Agent 时，上下文和图片可能被发送给对应模型服务商；请按其数据政策自行判断。
- Web UI 默认只监听回环地址。`--allow-remote` 不会增加认证或 TLS，不建议暴露公网。
- 上传只创建待办，不会在后台自动识别或生成菜单。
- 当前没有用户账户、云同步、移动端、包装 OCR 或外部营养数据库。
- MealCircuit 提供一般性记录和决策支持，不构成医疗诊断或治疗建议，详见 [DISCLAIMER.md](DISCLAIMER.md)。

隐私、安全和贡献规则分别见 [PRIVACY.md](PRIVACY.md)、[SECURITY.md](SECURITY.md) 与 [CONTRIBUTING.md](CONTRIBUTING.md)。
