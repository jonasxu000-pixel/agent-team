# 使用指南

先阅读 README 的依赖和预算说明。下列命令从已安装框架或源码目录运行，`<PROJECT>`、`<ID>` 为需要替换的占位符。

## 项目识别与批准测试

```powershell
python -m agent_team.entry --cwd '<PROJECT>' context
python -m agent_team.entry --cwd '<PROJECT>' configure --tests-file '<TESTS_JSON>'
```

`context` 只读展示 Git 根目录、基准、未提交状态、项目约定和测试候选。`configure` 把你确认过的测试保存到按 Git 身份隔离的本机 profile，不改业务仓库。也可在项目根提交不含凭证的 `.agent-team.json`。

任务可直接用 `--request` 或 UTF-8 `--request-file`。测试 JSON 是 argv 数组列表，不是模型可随意拼接的 shell 字符串。

## 模式

| 意图 | 入口 | Claude 费用 |
|---|---|---|
| GPT-only / 禁用 Claude | `--policy gpt_only`，Skill mode=gpt_only | 完全禁止 |
| GPT 主导、Claude 按需 | `--policy selective --use-default-budget` | 有具体收益且预算门禁通过才调用 |
| 独立只读审查 | Skill `--mode review` | 需要明确预算授权；不擅自修复代码 |

原 CLI 的 `deep_review` 更积极地安排必要审查，也不会绕过费用、安全条件。通过 `python -m agent_team run --help` 查看当前完整参数。

## Claude 专家职责

| 任务 | 参数 |
|---|---|
| Claude 明确负责前端设计和实现 | `--task-type ui_ux --claude-role frontend` |
| 架构分析与建议 | `--task-type architecture --claude-role architect` |
| 精确模块的局部实现 | `--task-type algorithm --claude-role developer` |
| 有真实复现测试的复杂 Bug | `--task-type debug`，明确专项实现时再指定 developer |

这些是任务职责，不应因为关键词将普通小改动升级成昂贵流程。框架不会自动提升模型档位或渠道。UI 视觉效果缺少可信渲染证据时仍待人工确认。

## 状态与实时记录

```powershell
python -m agent_team status
python -m agent_team status <ID>
python -m agent_team watch <ID> --replay --full
python -m agent_team report <ID>
python -m agent_team costs --task <ID>
```

`watch` 只显示公开输出、工具、测试和交接事件，不显示私有思维链。Ctrl+C 只断开监控。后台启动和恢复加 `--watch` 可直接接入当前命令输出。

安装 Skill 后也可运行 `python "$env:USERPROFILE\.agents\skills\agent-team\scripts\watch.py"`，它识别当前 Git 项目唯一活动任务；多个任务需明确 ID，不跨项目猜测。

## 暂停、需求修改与人工接管

```powershell
python -m agent_team pause <ID> --wait 30
python -m agent_team amend <ID> --request-file change.txt
python -m agent_team takeover <ID> --wait 30
python -m agent_team handoff <ID>
python -m agent_team resume <ID> --background --watch
```

只有状态和所有权实际确认后才视为暂停/接管完成。人工未提交改动是否纳入需要具体授权；不自动 reset、clean 或解决冲突。`stop` 紧急中断受控进程树，`cancel` 为永久终态，均保留现场。

恢复仍受同任务已消耗预算、调用次数和未知费用限制。Codex 订阅耗尽时保存检查点，不切换 OpenAI API；只有可靠状态或明确恢复确认才再派发。查看各子命令 `--help` 后使用恢复确认/有界等待选项。

## 手动基础设施验收

公开自动测试不付费。真实开发验收脚本须先安装可选 Skill 入口以生成本机安装元数据，然后在你明确批准本次费用后准备任务：

```powershell
python scripts/e2e-claude-native.py --approve-task-budget <APPROVED_PLATFORM_BUDGET>
python scripts/e2e-claude-native.py --task-id <PREPARED_ID> --execute
```

它创建独立最小 Git 项目，不使用业务项目。已经成功的验收不会重复调用；未知付费结果仍阻断。最多三轮修复仅适用于明确授权的对应验收任务，每轮须有失败分析、代码修改和无费用检查证据。脚本参数不代替用户授权或账本门禁。
