---
name: agent-team
description: "调用现有GPT主导的Agent Team V3.2：多Agent、明确无人值守、Claude前端设计与实现、架构/算法/调试专家、独立审查，以及任务监控、暂停、修改、接管、交还和恢复。普通轻量开发由Codex完成。"
---

# Agent Team 调用入口

本 Skill 调用现有 agent-team，不创建另一套调度器。普通开发优先直接处理；讨论能力、引用触发词或禁止多 Agent 的请求不会启动任务。

V3.2在隔离项目已通过真实Claude Read/Edit/Write、受控测试、真实Codex独立验证及集成回归。Native调用使用短临时运行目录，长期state-dir/费用账本不变。Windows内置Git Bash的MSYS在现有sandbox中不可运行，不能关闭sandbox让它通过；实际测试工具是mcp__agent_team__Bash，只接收批准的精确命令并交给同一runner在无网络沙箱执行。不称其为直接Git Bash，也不开放任意shell。实际边界/证据见tool_root/docs/VALIDATION.md。

完整开发自测入口为tool_root/scripts/e2e-claude-native.py；明确用户任务预算后prepare原Task ID，再execute。普通任务默认不运行基础设施自测；$8及最多3轮修复只在用户明确授权对应范围时使用。每轮真实重试需要失败分析、实际代码修改和无费用测试证据，未知已付费结果仍禁止重发。已有Claude交付的验证恢复复用commit/工具证据，不再次付费；已验收成功任务禁止为了确认而重复调用。

V3.1 Native启动使用临时credential bridge和显式沙箱环境白名单；密钥仅来自已授权的用户Claude配置/指定provider环境，不能复制全环境、用户登录文件或修改CC Switch。先在无网络沙箱执行auth status，检查认证和临时config目录；失败不派发推理、不自动/login。auth status成功只证明本地凭证解析，不能代替真实服务端认证或工具验收。

工具的最低成本真实自测用tool_root/scripts/smoke-claude-native.py：默认只创建隔离claude-native-smoke-test并给Task ID，不开发业务项目、不付费。执行必须使用该ID、--execute及本次明确批准金额；仍经过原账本的余额/价格/日月预算检查。只Read context.txt，核验真实工具结果和receipt，失败不自动改代码后重付，不复用其他任务费用授权。

## 调用与项目识别

入口为本 Skill 的 `scripts/entry.py`。先读取当前工作区的项目约定，运行只读检查；不要把工具安装目录误当成目标项目：

```powershell
python '<本 Skill 目录>\scripts\entry.py' --cwd '<当前工作区>' context
```

输出 Git 根目录、固定 HEAD、未提交状态、当前适用的 AGENTS 文件和项目测试配置。未提交修改不复制到协调器工作区；若需求依赖这些修改，先与原会话约定保存 commit，不能替其他会话提交或覆盖。

测试来源依次是显式 `--tests-file`、项目根 `.agent-team.json`、按 Git 仓库身份隔离的本机项目配置。项目配置可包含 `version`、`test_commands` 和更小的 `claude_budget`，不能扩大授权或更换模型渠道。缓存不同项目的测试互不复用；同一仓库的worktree共享本机测试配置。

缺少配置时检查输出的候选、项目 AGENTS/README、package.json 或测试目录。确认是真实测试命令后写入临时 JSON 数组，并用 `configure --tests-file <文件>` 保存本项目配置；它不会改业务仓库。无法确定测试时询问必要输入，继续独立检查。只有用户明确接受无自动测试时才用 `--no-tests`，不能将此结果称为测试通过。

## 三种入口

- `--mode gpt_only`：用户指定 GPT-only、只用 GPT 或禁止 Claude；实际 CLI 策略为 gpt_only，绝不授权付费。
- `--mode selective`：用户要求使用多 Agent 开发、让 Claude 必要时参与；仍由 GPT 开发，只有具体收益、风险与预算条件满足才审查。复杂算法、关键并发/权限或多次修复失败可考虑此模式，不因一个关键词把普通任务升级。
- `--mode review`：用户明确要求对现有代码独立 AI 审查，尤其是只读审计。所有模型只读，确认缺陷也只报告；未经实施授权不修复。若用户要求“开发并独立审查”，使用 selective 并把独立复审要求写入需求，不能把开发任务改成只读。

将需求写入 UTF-8 临时文件，可先 `plan` 查看真实 CLI 参数，再 `run`：

```powershell
python '<本 Skill 目录>\scripts\entry.py' --cwd '<当前工作区>' plan --mode selective --request-file '<需求文件>'
python '<本 Skill 目录>\scripts\entry.py' --cwd '<当前工作区>' run --mode selective --request-file '<需求文件>' --background --watch
```

不要止于输出计划：已授权执行且测试/权限齐备时实际启动，记录 Task ID。用户只要求审计、预览或只排队时分别遵守 `review`、`plan` 或 `--queue` 的边界。排队是调度选项；需求文件主要保存实际任务目标。用户随后要求继续时使用同一 Task ID 的 resume，不创建重复任务。

启动多Agent任务时默认加 `--watch`，执行和实时监控一同接入当前命令输出；GPT/Claude事件按agent字段区分，Claude实际参与时自动显示其公开过程，无须用户再输入watch。用户明确只后台/不显示日志时省略，queue不加。恢复任务默认用 `resume <ID> --background --watch`。在Codex App使用命令工具启动时如实说明显示在命令输出中，当前没有打开右侧原生终端窗格的工具就不能声称已自动打开它。长命令保留session_id并按需读取输出，不另外启动第二个runner；停止watch不取消后台任务。

用户要自己在终端独立查看时，只需运行 `python "$env:USERPROFILE\.agents\skills\agent-team\scripts\watch.py"`，自动使用installation.json的state-dir，识别当前Git项目唯一活动任务；没有活动任务回放该项目最近任务。多个活动任务显示ID并要求指定，不猜测跨项目任务。此入口只有监控，不启动/恢复Agent或授权费用。

## Claude 费用与恢复

明确多Agent或Claude专家参与时，添加`--use-default-budget`，普通单任务默认$3；明确“无人值守模式”“我去上课，按无人值守执行”才加`--unattended`并使用无人值守默认$8。不能从复杂、长任务或用户未在线推断$8。上海自然日默认$15，每月钱包默认$100；调用前以`budget show`读取当前配置。金额均为平台账面额度，钱包在授权范围内已获准使用，不逐阶段重新确认。普通任务、讨论、否定或GPT-only不授权Claude。

更小的用户金额用 `--task-budget <美元>` 收紧；超过默认额度的明确批准用 `--approve-claude-budget <美元>`，每日扩大另用原 CLI `--approve-daily-budget`。项目 `.agent-team.json` 中可配置更小 `claude_budget`，不能扩大用户默认。默认一次 Claude 审查，不升级 Opus/渠道。复杂任务预估超额、长时间多轮、价格无法核实时，展示具体收益、次数、平台费用范围或未知、建议额度和 GPT-only 方案并等待授权；可先完成 GPT 部分且标记独立审查待完成。不能把预算当消费目标。预算为客户端软限制；任务/每日账本、套餐窗口、钱包、密钥额度和 Codex 订阅分开记录。

## V3 专家分工

普通selective不加专家强制参数，让Codex基于真实调查判断任务类型。样式微调/表单/文档仍是general，不因“前端”一词升级。明确Claude负责视觉设计和前端实现时使用`--task-type ui_ux --claude-role frontend`；架构用`--task-type architecture --claude-role architect`；模块算法实现用`--task-type algorithm --claude-role developer`；复杂Bug先配置实际复现测试，用`--task-type debug`，有明确专项修复要求才加`--claude-role developer`。独立只读审查仍用`--mode review`。所有参数经真实entry/协调器执行，不口头宣称已委派。

例：`entry.py --cwd <项目> run --mode selective --use-default-budget --unattended --task-type ui_ux --claude-role frontend --request-file <需求> --background --watch`。不明确无人值守就省略该flag。

Codex在plan_v3整理产品、用户、页面、交互、现有问题、保留功能、源文件及技术约束；Claude设计与代码分别有阶段交付，不让它一次返回完整文件JSON。NativeDeveloper在独立worktree获得Read/Grep/Glob、授权文件的Edit/Write及仅供批准测试的Bash入口，由协调器提交并交给Codex验证/集成/回归。不使用bypass。按规划具体收益可分配默认上限内的只读子代理，最大深度1、每子代理4turn；不开放通用递归或子代理写入。

前置检查整个专家阶段的费用可行性，动态把平台额度保守换算为CLIcap，不固定$0.20，不把平台$8直接传CLI。月钱包统计实际钱包扣费及未知来源风险，套餐/赠送另列；账本不覆盖其他state-dir及手动工具。预算/格式/工具异常保存完整公开段落、实际文件和可验证提交，Codex继续能独立完成的部分；未知已派发请求不自动重付。图像仅文本引用时必须明确未传图；没有可信前后渲染/截图时，视觉验收待人工，代码测试通过不能代替。

费用说明分别给出预计费用情景区间、保守预留和已关联平台账单，不能将预留上界当成预计实付报价。预计区间使用明确标注的token启发式及同模型新鲜账单的缓存参考，样本不足或价格未知时直说；不把某次任务的缓存比例固定套到未来。预算门禁仍按保守预留核验，预计区间不授予额外消费权限。如果仅预留超过现有授权，先整理当前有效任务书、删除传递中的重复旧上下文（保留历史记录）并按实际源文件范围重新预估，再说明是否确实需要增加预算。工具自测默认无费用，已完成任务不为校准估算再次付费调用。

工具自测默认Mock；如本次任务明确未授权付费模型，真实验收要先提交范围/模型/平台费用，再取得授权。不能从其他任务批准推导自测付费许可；普通日常任务的既有授权按本节执行，不重复确认。

App 会话与 CLI 会话独立。检查父任务的身份和 Task ID；在 `AGENT_TEAM_CHILD` 环境、`codex/team/...` 分支、协调器管理的 worktree，或已被协调器委派的 CLI 角色中直接完成角色职责，绝不再次启动入口。即使原需求包含“多 Agent”，也不能递归启动。

尊重当前项目的确认、文件职责和验收约定，使用现有权限；权限阻断时说明具体限制并请求必要权限，不自动切到全权限。状态、Git diff、测试与费用以协调器记录为准；未经用户批准不合并、推送或部署。通过 installation.json 定位工具目录及 state_dir，给原 `agent-team` 的 status/report/costs/pause/resume/cancel 传同一个 `--state-dir`。入口已使用物理路径规避 MSIX 目录重定向，不能在外部终端换成另一个默认状态库。

## 操作已有任务

用户说“暂停这个任务”“需求改成……”“我接管代码”“修改好了交还”“继续刚才任务”“显示 Claude 可见过程”时，先读 installation.json，在 tool_root 调用 `python -m agent_team --state-dir <state_dir> ...`。不重新创建任务。不在 agent-team child 角色中执行父任务控制。

Task ID 优先来自用户或当前聊天已记录的任务。否则查看 status 列表及任务的 repo；只在当前 Git 仓库对应唯一任务时自动选择。多个任务时询问具体 ID，不猜测也不暂停全部任务。

- 暂停：`pause <ID> --expected-revision <当前版本> --wait 30`。PAUSE_REQUESTED/PAUSING 只能说已请求；PAUSED 且 runner/active_pid 已退出才说已暂停。默认宽限120秒后中断受控子进程，会标记 INTERRUPTED。用户明确要求紧急停止才用 `stop`；cancel 永久取消任务。
- 修改需求：把变更写入临时 UTF-8 文件，`amend <ID> --request-file <文件> --expected-revision <版本> --command-id <固定token>`。保留原目标及变更记录；检查 revision 增加、命令是否PENDING或APPLIED。运行中在下个检查点重规划；暂停中要 resume 才执行。
- 接管：`takeover <ID> --wait 30`；确认 workspaces.codex.owner=USER 后给真实 worktree 路径。仍在请求/处理中则继续检查状态，不能提前让用户与 Agent 同时写入。
- 交还：`handoff <ID>`；如有未提交修改，检查 diff 并询问是否纳入，或沿用用户已明确授权的未提交修改交还意图使用 `--include-uncommitted`。冲突/变基需用户解决；不 reset/clean。所有权回到 ORCHESTRATOR、revision 更新后，用户说交还并继续则执行 resume。
- 恢复：`resume <ID> --background --watch --expected-revision <版本>`。新 CLI 调用是从持久化检查点开启的新会话。USER所有权、活进程、未知付费结果均不可绕过；预算增加仍须本任务明确费用授权。
- 监控：`watch <ID> --replay --full`；JSON用 `--json`；重连用 `--after <watch_cursor>`。Ctrl+C仅退出watch，不停止任务。仅展示公开文本/工具/结果和真实事件，不能声称显示私有思维链。账单用 `costs --task <ID>`；需要更新时加 `--refresh`，不触发推理。

重复提交用同一 `--command-id`；存在不同的当前revision时重新检查目标再决定，不能盲目重试旧命令。控制提交成功不等于执行完成；以 commands 的结果、实际任务状态、Git及所有权共同确认。
