# 开发过程记忆

> 项目于 2026-07-02 从 DietOS 更名为 MealCircuit（食回路）。以下旧名称保留为真实历史记录。

> 本文件只记录开发历史，不是 Agent 的需求输入。当前行为以代码、测试、`AGENTS.md`、`README.md` 和 `docs/agent-workbench.md` 为准。

## 2026-07-14：今天状态改为连续问答

- 目标：用户从今天页进入状态填写后，可以连续答完当前模块及后续模块，不再每答一题就返回主页、重新寻找“现在回答”。
- 核心改动：从“今天的状态”进入的问答会在“下一题”后保留当前位置；一个模块完成或跳过后自动进入下一个尚未处理的启用模块，全部处理完才返回今天页。问答中的“返回”在模块内优先回上一题，第一题才退出到今天页；从“查看全部”进入的原有逐模块浏览方式保持不变。
- 验证：运行 `test_today_checkin_continues_through_questions_and_modules` 与 `test_checkin_web_question_flow_settings_and_origin`，覆盖模块内下一题、训练完成后进入饥饿模块、连续跳过睡眠/肠胃、最终返回今天页、设置页及来源安全检查，2 项均通过；`git diff --check` 通过。按用户要求未运行浏览器校验、全量测试、Android 构建或 CI 等待。
- 用户用法：在今天页点击一次“现在回答”后直接顺序填写；需要中途退出时使用“返回”，系统仍保留已经回答的草稿。

## 2026-07-14：今天的文字记录保存后持续可见

- 目标：用户点击“记下来”后仍能在今天页看到自己完整写下的内容，而不是面对一个空白输入框并怀疑记录丢失；需要纠正时可直接修改。
- 核心改动：当天已保存的文字以低调的“已记下”卡片显示在输入框上方，正文不截断；“修改这条内容”展开原文并允许保存。修改沿用同一记录 ID，但每次写入都会产生新的领域 revision，旧内容仍保留在可追溯历史中；当前记录投影和对应 Agent intake 事件同步更新，避免新旧文本同时进入下一次规划。新增和修改后都返回今天页的记录位置。
- 验证：运行 `test_today_intake_stays_visible_and_can_be_revised`，确认保存后回显、锚点返回、编辑、同一记录 ID、两代领域 revision、Agent 输入更新和旧文不再污染当前页面；同时复跑三项用餐回执/照片定向测试，4 项均通过，`git diff --check` 通过。按用户要求未运行浏览器校验、全量测试、Android 构建或 CI 等待。
- 用户用法：继续在“记一笔”中添加新情况；保存后内容会出现在“今天已记下”，需要纠正时展开对应记录修改即可。

## 2026-07-14：用餐回执支持照片并保留未完成表单

- 目标：让“吃得怎么样？”可以把实际用餐照片和这顿回执保存在一起；用户选择“调整后完成”或“没有执行”却漏选原因时，不再离开计划页或丢失已经填写的内容。
- 核心改动：回执表单支持一次选择多张图片，也可以之后继续追加，不设置图片张数上限；每张照片沿用既有本地照片任务并分别关联为这顿的真实执行证据，旧的单图回执仍可读取。任何用餐回执校验错误都会在当前表单内提示，并恢复实际情况、份量感觉、原因、文字与已上传照片。文字框在输入端明确限制为 2000 字，超限时使用用户看到的字段名称，不再跳到通用错误页。
- 验证：运行 `test_plan_feedback_preserves_inputs_and_photo_after_missing_reason`、`test_plan_feedback_text_limit_stays_on_the_plan_with_inputs` 与既有 `test_photo_upload_form`，覆盖同字段多文件解析、多张照片持久化与展示、保存后继续追加、漏选原因、文字超限、400 原页恢复、执行证据关联和原照片上传入口兼容，结果通过；`git diff --check` 通过。按用户要求未运行浏览器校验、全量测试、Android 构建或 CI 等待。
- 用户用法：在计划的任一餐展开“吃得怎么样？”，可以一次选择多张实际照片，也可以保存后再次打开继续添加；若某项内容需要修改，页面会就地说明并保留已填内容，修正后再次点击“记下来”即可。

## 2026-07-14：收紧 Agent 基础指令与产品版本语言

- 目标：让任何新 Agent 首先看到一份短、稳定、没有重构历史的项目运行规则，同时从用户可见页面移除内部工作台代号、记录版本、哈希和英文工程标签。
- 核心改动：将 `AGENTS.md` 收缩为目标、执行流程、不可突破边界和开发约束；删除旧导航、旧截图和历史纠偏说明。工作台说明改为无版本名称的 `docs/agent-workbench.md`，README 同步更新链接。
- 代码清理：上下文检查页只展示人能理解的规划参考，不再显示内部上下文名称、哈希、用户模型版本或知识包版本；CLI 和错误文案也不再暴露内部代号。删除已无调用方的旧今日工作台、旧学习页、洞察页、记录页、旧生成控件和旧 dashboard 渲染代码。
- 保留边界：同步协议、数据格式、迁移以及内部模型上下文仍保留机器可识别的版本，因为它们承担兼容和校验职责，但不会作为普通产品文案展示。
- 验证：遵循用户当前指示，未运行测试、构建、浏览器流程、Android 或 CI；仅检查剩余引用并运行 `git diff --check`，结果无格式错误。
- 用户用法：入口和操作方式不变；变化只体现在更自然的解释页面以及更干净的 Agent 项目指令。

## 2026-07-14：清理过期 Agent 上下文

- 目标：删除已被当前三入口工作台取代的界面资料和验证快照，并让所有 Agent 明确区分当前规范与历史记录，避免旧导航、旧按钮、旧测试数量或旧视觉方向重新进入产品判断。
- 删除文件：移除旧 calibrated-console `design-qa.md`、旧自适应闭环验证矩阵、两张只描述旧 dashboard/处理队列的截图，以及侧栏收口后已无任何页面引用的 8 个旧导航图标；这些文件没有运行时消费者，当前 README 的旧截图引用和对应 CSS 映射也已删除。
- 保留与更新：保留仍被运行时代码使用的 `templates/profile.md`、`templates/settings.json`、协议、迁移、发布、安全和同步文档；维护 `docs/agent-workbench.md` 的当前产品说明，并更新中英文 README 与多端验收文档的时效边界。
- 验证：按用户持续有效的指示未运行测试、构建、发布检查、浏览器流程或 CI；仅检查删除候选的代码/文档引用并确认被删资料没有运行时消费者。当前不能把未执行检查描述为通过。
- 仍未实现：`DEVELOPMENT.md` 保留较早历史条目以维持真实开发记录，但已明确降级为非规范历史；未删除任何用户数据、运行时模板、数据库迁移、协议夹具或发布资产。
- 下一最小任务：将文档与资产清理追加到现有 Draft PR #22；后续 Agent 应只按 `AGENTS.md` 中的当前信息优先级读取项目。
- 用户用法：没有运行时操作变化；Web 仍只通过“今天、计划、我的”完成日常使用。

## 2026-07-14：Web 产品表层收口与今日工作台重构

- 目标：让普通用户只需要理解“今天要做什么”，把规划阶段、任务队列、版本、来源清单、校准资格和规则实验等内部机制退回后台；保留全部真实数据、规划、安全、学习、同步与兼容能力。
- 改动文件：调整 `mealcircuit/server.py`、`mealcircuit/static/app.css`、`mealcircuit/static/app.js`、定向 Web/Agent 测试和本开发记录；未修改 Android、领域规则、数据库结构或同步协议，也未触碰工作树中用户已有的 `docs/agent-workbench-v2.md` 换行状态。
- 核心功能：一级导航固定为“今天、计划、我的”，顶栏只保留“记一笔”；新增计划总览和个人中心，旧 `/capture`、`/daily`、`/insights` 安全重定向。今天页按已有计划、待确认、规划中、草案完成、情况变化和失败状态只展示当前下一步，直接提供自然语言记录与相关今日状态问题；照片、食材和库存降为附加入口。计划、复盘、学习、库存、初始化和模型设置统一采用自然中文，隐藏普通用户不需要理解的状态枚举、版本、来源清单和原始 JSON；正式计划只保留“为什么这样安排”和“这次参考了什么”两类解释。学习中心只展示正在影响计划或等待确认的理解，并提供“对 / 不对 / 只适用于今天 / 以后记住 / 暂时别用 / 忘记”。
- 体验修正：侧栏折叠继续持久化，并额外保存滚动位置；跨页面恢复后确保当前入口可见，手机打开导航时直接聚焦当前页。今日状态回答可返回今天页原位置，不再强迫用户理解固定完成率。
- 验证：按用户本轮明确指示未运行全量测试、Android 构建、发布检查、浏览器验收或 CI 等待；仅补写了对应定向测试，当前不能把这些路径描述为已验证通过。
- 仍未实现：Android 信息架构未随本轮调整；旧书签仍可进入未导航的高级/兼容页面，其中保留面向开发者的诊断能力。视觉与交互的真实浏览器验收留待用户恢复验证要求后执行。
- 下一最小任务：将当前分支推送为 Draft PR 供产品审阅；后续如恢复验证，优先只跑 `/`、`/plans`、`/me`、旧入口重定向和桌面/手机侧栏状态的定向浏览器流程。
- 用户用法：日常只进入“今天”记录变化、补充真正相关的状态并查看草案；“计划”集中查看今天、明天和历史安排；长期目标、MealCircuit 对用户的理解、库存、设备和数据统一在“我的”。

## 2026-07-14：纵向个案 Agent 工作台 v2

- 目标：让 MealCircuit 从“模型填结构、服务端校验”升级为围绕真实人的连续个案闭环；每轮先理解目标、状态、矛盾和证据，再集中追问、设计、独立审查、协商草案、发布执行并从真实结果修正用户模型。
- 改动文件：新增 `mealcircuit/agent_workspace.py`、`mealcircuit/professional.py`、`docs/agent-workbench-v2.md` 和 Agent 纵向测试；扩展数据库/迁移、模型 provider、服务、安全触发、Web/CLI、Portable/Domain 投影、协议契约、Android 新结果展示、样式及说明。没有新增 Python 依赖，API Key 仍只存在当前进程或平台安全存储。
- 核心功能：`AgentContextV2` 把本次上下文编译为 person/today/longitudinal/professional_basis/decision_task 五层，并提供可读上下文检查器；CaseFormulationV1 → DailyPlanV3 → PlanReviewV1 三阶段生成仅产生可替换草案，最多 3 条决定性追问，审查最多回到规划阶段一次。每餐提供克数范围、生熟/上桌口径、生活量具、营养置信度及饥饿/低食欲/肠胃调整；局部修订会确定性恢复未受影响餐次，接受前再次校验上下文和全部既有安全、逐餐模式、食材承接与语义轮换门。
- 学习与生命周期：用户模型保存支持证据、反证、置信度、适用范围、有效期、使用记录和回滚版本；一次明确纠正或两个独立真实信号可激活低风险理解，模型自己的重复猜测只保持待确认。目标、安全、过敏、疾病、药物、孕哺、营养目标和强排除不能由学习中心激活。新记录、发布状态、临时逐餐安排、库存、目标/策略、安全、正式规则、实验和用户模型变化会让草案过期并在已配置 provider 时重新防抖生成；失败运行不进入正式历史。
- 专业与多端：离线版本化知识包只选择适用的 WHO、NIDDK、ACOG、运动营养联合立场和当前 DGA 原则及边界，运行时不联网且不能自行推算确认目标。紧凑 `agent_user_model` 作为偏好实体参与 Portable Data 和 E2EE 同步，详细草案与运行诊断留在 Python 本机；Android 只读展示 Python 发布计划中的个案摘要、三餐目的、份量与依据，不运行本地深度 Agent。
- 验证：最终一次 `test.ps1` 的 144 项测试通过（26 项可选加密、同步服务与 PostgreSQL 测试按既有环境条件跳过）；协议 JSON、两份 workflow YAML、依赖锁检查、发布扫描和 `git diff --check` 通过。隔离数据库的真实 Edge/Playwright 完成草案、上下文、接受发布、份量回执和用户模型闭环，并在浏览器中发现和修复“饱腹反馈未回显/未学习”“正式计划仍显示成待生成草案”“移动端关闭抽屉仍可进入焦点”三处产品缺口；1440px 与 390px 均单一 `h1`、无横向溢出、控制台零错误，键盘可进入并关闭导航。内置浏览器连接因运行时 `Cannot redefine property: process` 失败，已按工具规则改用隔离 Playwright。Android Gradle 本机因没有 Android SDK 在任务解析前停止；按用户后续指示不再补装 SDK、不追加全量或 CI 等待验证，不能把 Android 编译描述为本地通过。
- 仍未实现：Android 不提供本地三阶段生成、学习中心或自动草案；不做医疗诊断、运行时联网更新知识库或自行修改代码。模型质量仍需在真实使用中通过执行回执、局部纠正和纵向场景人工审查持续校准。
- 下一最小任务：提交并打开草稿 PR 供审阅；遵循用户最新决定，不等待或追加 Android/CI 全量验证。
- 用户用法：启动 Web 后在“今天”直接描述变化；只回答会改变方案的问题，查看草案的份量和上下文选择理由，用“局部重算”协商，确认后再点“接受并开始执行”。执行页记录是否完成、饱腹和偏离原因；学习中心可查看、确认、纠正、暂停或遗忘系统理解。CLI 对应 `agent-intake`、`agent-context`、`agent-draft/state/answer/revise/accept` 与 `user-model`。

## 2026-07-13：现实的开源发布签名策略

- 目标：让正式 `v*` tag 在缺少商业桌面签名凭据时仍能发布完整开源产物，同时继续把 Android release key 作为正式发布硬门禁。
- 改动文件：仅调整 `.github/workflows/release.yml`、`docs/releases/v0.3.0.md`、发布工作流静态检查与对应单测，并记录本轮过程；未修改业务代码、Android 应用逻辑、同步协议或产物结构。
- 核心功能：Android signing availability 必须同时具备 keystore、keystore 密码、alias 和 key 密码，正式 tag 继续逐项硬校验；Windows Authenticode availability 必须同时具备证书和密码，Apple availability 必须同时具备全部六项 Developer ID/notarization 凭据。桌面凭据完整时沿用现有正式签名流程，缺失或部分配置时发布未签名 Windows ZIP/installer 与仅 ad-hoc signed、未 notarize 的 macOS DMG，并在 tagged run 输出明确 warning 而不失败。
- 验证：release workflow YAML 解析通过；新增 6 项策略静态测试通过，覆盖桌面硬门禁拒绝、Windows/Apple 部分凭据、Android 四项凭据和四平台 release 依赖；`python tools/dependency_check.py`、`python tools/release_check.py` 通过；完整 `.\test.ps1` 124 项通过（26 项可选依赖测试按设计跳过）；`git diff --check` 通过。
- 仍未实现：未配置或修改任何 GitHub secret，未创建 tag 或 Release；Android 正式发布仍需要仓库所有者提供四项签名 secrets。Windows Authenticode 与 Apple Developer ID/notarization 继续是可选的分发信任增强项，不在本轮生成或代管商业证书。
- 下一最小任务：仓库所有者配置四项 Android secrets 并在合并后从受保护的最新 main 创建正式 tag；发布后按 `SHA256SUMS.txt` 复核全部资产。
- 用户用法：下载 `v0.3.0` 资产后先使用 `SHA256SUMS.txt` 校验；Windows 在未配置 Authenticode 时可能显示 Unknown Publisher，macOS 在未配置完整 Apple 凭据时需要按未 notarize 应用处理。
## 2026-07-13：稳定 main 的 Android 模拟器验收

- 目标：修复 PR #17 全绿但合并到 main 后 `android-instrumentation` 偶发失败的问题，确保正式发布前的 Python ↔ Android E2EE 验收可重复运行。
- 改动文件：仅调整 `.github/workflows/test.yml` 的 instrumentation runner 与临时恢复字符串日志掩码，并记录本轮过程；不改变 Android、同步协议、服务端或业务逻辑。
- 根因：普通 Android 编译、单测和 lint 均通过；失败发生在 Ubuntu 无 KVM 模拟器已经报告 `sys.boot_completed=1` 后，Android `input/settings` 服务仍返回 `Broken pipe`，`reactivecircus/android-emulator-runner` 因而在执行项目测试脚本前退出。PR 成功而 main 失败是同一工作流的基础设施时序抖动，不是 PR #17 的业务回归。
- 核心功能：把真实 instrumentation job 移到 `macos-15-intel`，使用 GitHub 托管 macOS 的硬件加速虚拟化；保留 API 35、完整 Gradle instrumentation、真实同步服务、Python → Android → 新 Python 双向验证和服务端明文扫描。写入 `GITHUB_ENV` 前把 `add-mask` 工作流命令强制放在独立日志行，避免无换行的 healthz 响应使 GitHub 忽略遮罩并在后续环境摘要中回显合成恢复字符串。
- 验证：工作流 YAML 解析、`git diff --check`、`python tools/release_check.py` 和基础 `.\test.ps1` 118 项通过（26 项可选依赖测试按设计跳过）；Android `testDebugUnitTest lintDebug compileDebugAndroidTestKotlin` 通过。最终以草稿 PR 的 macOS instrumentation、PostgreSQL 18、Android build 和发行矩阵为准。
- 仍未实现：此修复不改变正式 APK/AAB 签名配置；正式 tag 仍要求仓库提供 Android、Apple 和 Windows 签名 secrets。
- 下一最小任务：让草稿 PR CI 全绿后，由仓库所有者审阅合并，再从合并后的 main 创建正式签名 tag 和 GitHub Release。
- 用户用法：无需改变；这是 CI 稳定性修复。
## 2026-07-13：稳定 PR #19 的 Android 双向同步验收

- 目标：修复 PR #19 唯一失败的 `android-instrumentation`，保持真实 Python ↔ Android 双向同步测试和现有 Android 测试强度不变。
- 改动文件：仅调整 `.github/workflows/test.yml` 的 instrumentation runner 与合成恢复字符串日志遮罩，并记录本轮过程；未修改 Android 业务逻辑、同步协议或测试命令。
- 根因：Ubuntu 无 KVM 模拟器耗时约 434 秒后报告 `sys.boot_completed=1`，但 `connectedDebugAndroidTest` 安装 APK 时 Android package service 返回 `Broken pipe (32)`，最终只启动 0 个 instrumentation 测试；后续 Python 验证因 Android 没有产生离线 revision 而失败。这是与 PR #18 同源的模拟器服务时序故障，不是菜单语义或同步业务回归。
- 核心功能：把 instrumentation job 移到已在 PR #18 验证通过的 `macos-15-intel` 硬件加速 runner；保留 API 35、`x86_64`、真实同步服务、`connectedDebugAndroidTest` 和双向验证。写入 `GITHUB_ENV` 前用独立换行的 `printf` 注册 `add-mask`，避免无结尾换行的 healthz 响应吞并工作流命令并在环境摘要中回显合成恢复字符串。
- 验证：修改前已下载 PR #19 的失败 job 专属日志并确认上述根因，同时确认 PR #18 的同源补丁及其 macOS instrumentation 已成功。修改后 workflow YAML 真实解析、`git diff --check` 和发布扫描通过，基础 `test.ps1` 124 项通过（26 项可选依赖测试按设计跳过）。本机没有 Android SDK，Gradle 在解析任务依赖前明确停止，未把本地 Android 三项误报为通过；推送后由已配置 SDK 的 `android` job 执行 `testDebugUnitTest lintDebug compileDebugAndroidTestKotlin`，最终结果以完整 CI 和日志密钥扫描为准。
- 仍未实现：本修复不改变正式 APK/AAB 签名配置，也不降低或跳过 instrumentation；正式 tag 仍受既有签名门禁约束。
- 下一最小任务：推送当前修复，等待 `test` 与 `release-builds` 全绿，并扫描完成日志确认恢复字符串正则零命中。
- 用户用法：无需改变；这是 CI runner 与日志安全修复。

## 2026-07-13：菜单语义轮换与生成记录生命周期

- 目标：阻止仅复制旧菜单、改日期或标签的低质量生成，并让尚未执行的 Agent 错误可直接替换，而不污染用户看到的正式历史。
- 改动文件：新增 `mealcircuit/menu_semantics.py` 与 `review_lifecycle.py`；扩展复盘提交/生成、计划投影、领域同步、CLI、Web、规则、双语 README 和自动化测试。
- 核心功能：服务端从菜名、食材、调味、步骤和技法计算语义指纹，旧结果没有 `rotation` 也能反向比较；完整重复全部拒绝，自炊近似重复只接受有上下文证据的健康恢复、临期食材或采购限制。内置生成候选先在内存完成结构、安全、约束和语义校验，最多自动重试两次，通过后才入库。今天及未来、无执行证据的计划原位更新同一版本与计划项；已有回执、救场、学习引用或已过期结果锁定后才追加正式历史。同步层以稳定 active-result 实体更新当前生成物，定向清理会为旧派生实体写 tombstone。
- 验证：124 项 `test.ps1` 全部通过（26 项可选 E2EE/PostgreSQL 依赖按设计跳过）；`compileall`、发布扫描和 `git diff --check` 通过。新增覆盖语义改名/词序/旧 rotation、食材承接换蛋白风味技法、早餐基础食材复用与完整组合重复、三次候选重试、失败后原计划不变、可替换/锁定、执行反馈版本化、定向清理和同步 tombstone。真实 Edge/Playwright 检查 `reviews/2026-07-12` 与 `plans/2026-07-13`：页面身份正确、单一 h1、360px 无横向溢出、控制台零 warning/error，主题按钮从 light 切换到 dark；内置浏览器运行时因本机 kernel assets 路径错误无法初始化，已记录降级原因。真实库清理预检发现 v1 已有关联的 `modified` 执行回执与已完成救场，证据门按设计停止，未删除 v1–v4 或任何事实数据。
- 仍未实现：语义词表是确定性中文/英文常见食材与技法集合，不替代营养专业判断；同步服务的可选 E2EE/PostgreSQL 集成仍需对应依赖与测试地址才会运行。
- 下一最小任务：推送 Draft PR 并等待 CI；真实库 v1–v4 因已有执行证据必须保留，除非用户未来明确将对应回执和救场判定为误操作并另行授权事实级修复。
- 用户用法：照常提交或生成复盘；重复菜单会返回包含日期、餐次和冲突维度的错误。页面会标明当前计划“可替换”或“已锁定”。维护清理先运行 `review-cleanup DATE --expected-version N` 预览，再明确追加 `--apply`。

## 2026-07-12：单日逐餐安排覆盖长期默认

- 目标：用户明确说明“明天午餐外食、晚餐自炊”时，复盘与执行计划直接显示这一有效安排，而不是先生成虚假自炊菜单再用救场修正。
- 改动文件：扩展自适应问题、有效餐次模式解析、上下文/schema/校验、模型工具结构、Web 问答与复盘/计划展示，并补充规则、说明和回归测试。
- 核心功能：长期三餐方式仍保存在版本化个人策略；单日逐餐覆盖只作用于次日并保存问题 ID/版本，未指定餐次回退默认。外食必须给蛋白/主食/蔬菜/酱汁/备选规则且禁止菜谱卡；旧 `mixed` 保持未知。
- 验证：73 项 `test.ps1` 全部通过；隔离数据目录的真实浏览器完成逐餐表单提交，确认复盘页直接显示午餐外食提醒且没有午餐菜谱卡、执行计划页显示外食选择规则和晚餐自炊卡，页面仅一个 `h1`、桌面无横向溢出、控制台无警告或错误。浏览器截图接口超时，未把截图作为通过依据；CI 待推送后复验。
- 仍未实现：不从自由文本静默猜测逐餐覆盖，必须由结构化逐餐问答确认。
- 下一最小任务：推送 Draft PR 并确认 Python 3.11/3.13 CI 绿色。
- 用户用法：在“最少提问”中分别选择明天早餐、午餐和晚餐的临时方式；选择“沿用个人默认”的餐次不会改变。

## 2026-07-12：逐餐个人准备方式与双下厨菜单

- 目标：早餐、午餐、晚餐分别由用户在初始化时决定在家下厨、快速组装或外食；支持午餐和晚餐均自己做且分别获得完整菜单执行卡。
- 改动文件：新增逐餐模式领域模块，扩展版本化初始化、设置解析、上下文、结果结构、确定性校验、计划投影、模型工具结构、Web 初始化与菜单展示，并补充规则、说明和测试。
- 核心功能：逐餐方式保存到个人策略版本，不再由仓库模板固定；每个在家下厨餐次独立执行时间/厨具限制、菜式轮换和历史追踪，共享采购、网购筛选与三日食材复用；旧用户仍按原“早餐组装、午餐外食、晚餐下厨”兼容读取，重新确认后才迁移到新个人策略。
- 验证：69 项测试在 Python 3.12.13 和 3.13 均通过，覆盖版本化保存、午晚餐双执行卡、逐餐硬约束、计划投影、历史与 Web 展示；隔离数据目录的真实浏览器验证初始化选择和双执行卡，320/1440px 均无横向溢出、仅一个 `h1`，控制台无警告或错误。GitHub CI 待推送后复验。
- 仍未实现：无自动猜测用户三餐方式；未重新确认的老用户继续采用旧兼容语义。
- 下一最小任务：推送当前原子提交并确认 Draft PR 的双 Python 版本 CI 绿色。
- 用户用法：进入“目标与边界”重新确认或首次初始化，在“现实限制”中分别选择三餐准备方式；午餐和晚餐都选“在家下厨”后，次日菜单会输出两张独立执行卡。

## 2026-07-12：多端独立运行、E2EE 同步与发行基线

- 目标：把 MealCircuit 从桌面 local-only 工作台升级为 Android 与桌面均可完整离线运行、可连接用户自选同步地址、远端只持有密文的 local-first 产品；保持账户、网络和模型 API 全部可选。
- 改动文件：新增 `protocol/` 语言无关契约与夹具、`mealcircuit/domain*.py`、显式数据库迁移、Portable Data、桌面同步和安全存储、`sync_server/` 参考服务、完整原生 `android/` 工程、三平台桌面与 Android 发行配置、CI、威胁模型、备份恢复和协议文档；扩展现有服务、CLI、Web 同步/冲突界面和测试。
- 领域与迁移：Domain v1 使用不可变 revision、完整 UUIDv4、父 revision 图、UTC RFC 3339 时间点和用户 IANA 时区；旧短 ID 继续读取。SQLite schema v1→v6 每步显式迁移并在修改前用 Backup API 留快照，失败恢复最初快照；真正旧表结构测试确认备份先于补列。配置文件作为版本实体同步并保持原子镜像，图片迁为内容寻址资产，缺失外部路径保留并由 doctor/同步状态提示。实现已语义合并主分支最新的自适应闭环、安全门、逐餐准备方式和计划投影，原有 Web/CLI 行为与 provenance 均继续通过回归。
- Portable Data：桌面和 Android 均实现加密 `.mcx`、风险确认后的明文 ZIP、restore/merge preview+apply、领域级 round-trip、资产 SHA-256、revision 图合并，以及路径逃逸、重复项、压缩炸弹、截断、篡改、缺失引用和中途失败防护。Python 导入先在同卷兄弟目录完成全量 staging、验证和二次导出比较，再用恢复日志原子提升；进程中断后会回滚旧目录或确认已完整提升的新目录，不在正式目录补偿式写入。
- 同步与安全：Sync v1 实现 HMAC 不透明实体 ID、HKDF 派生、AES-256-GCM、AAD 绑定、幂等 op、CAS、单调游标、90 天压缩/完整快照、三方合并、冲突 sibling、墓碑、4 MiB 加密附件分块、设备确认、恢复字符串、10 分钟一次性二维码、刷新令牌轮换、设备撤销和分阶段全量密钥轮换；未知新 schema 密文会完整保留但不被旧客户端物化或重写。FastAPI/PostgreSQL 18 参考服务只保存账户/设备元数据、密文状态、密文日志和加密 blob，提供 first-user 注册、配额、Alembic、Docker Compose、Caddy、可注入 `BlobStorage` 边界与无解密能力的管理 CLI。
- 客户端：Python 保留浏览器和 Agent CLI，新增交互式同步 CLI、Web 冲突/设备/状态、keyring 与 pywebview/PyInstaller；Android 使用 Kotlin、Compose、Room、WorkManager、OkHttp 和系统相机/Photo Picker，实现记录、状态、任务、食品库、复盘、记忆、调整、配置、AI provider、导入导出、同步、设备、二维码、冲突和密钥轮换，Room 是所有 UI 的唯一读源。
- 验证：合并最新主分支后，基础 `.\test.ps1` 共 113 项通过（未安装可选依赖时 26 项按设计跳过）；完整依赖环境 113 项中 112 项通过，仅本机未提供 PostgreSQL URL 的真实 PostgreSQL 项跳过。新增全新空目录首启回归，确认没有 `settings.json` 时直接显示初始化而不创建虚假配置；源码与 Windows PyInstaller 清洁包都在唯一临时目录通过 `--smoke-test`。11 项 Android JVM 测试、Android release lint、debug instrumentation 编译和模拟器 instrumentation 通过；另有 10 项连接真实参考服务的 instrumentation 全通过，并完成 Python 离线任务 → Android、Android 离线记录 → 新 Python 客户端的双向 E2EE 验收，同时扫描服务端数据库/WAL/备份、blob 和日志确认合成饮食明文、密码、恢复字符串及 API Key 零命中。Domain/OpenAPI/release/dependency 检查、Python compileall、`git diff --check`、通用 `uv.lock` 与 Android Gradle lock、Python 锁定依赖漏洞扫描、Alembic fresh/legacy 升级均通过。Android unsigned release APK/AAB 清洁构建通过；模拟器冷启动、总览、更多与同步设置页面无崩溃且关键文本/布局可见。CI 的测试与发行工作流会分别在 PR 上执行 PostgreSQL 18、双 Python、Android 模拟器和 Windows/macOS/Linux/Android 产物矩阵，并自动取消同一 PR 的旧运行。
- 仍未实现：不包含 iOS、官方托管云、同设备快速账户切换、多人共享或服务器代理 AI。密码学未经过第三方审计；本机数据库不做 SQLCipher。macOS/Windows 正式签名、notarization、Play 发布需要用户提供平台账号与 CI secrets；tagged release 已设置硬门禁。桌面标准库无法可靠判断当前连接是否为 Wi-Fi/非计费网络，因此手动同步把 `all_wifi` 视为允许，Android 会按网络计费状态执行。
- 下一最小任务：确认草稿 PR 的测试与四平台发行构建矩阵全部通过；任何平台失败都在同一 PR 内修复，不跳过签名账户之外的门禁。
- 用户用法：不登录时照常本地使用。加密备份运行 `python -m mealcircuit.agent_cli export-data --output backup.mcx`；自托管服务按 `sync_server/README.md` 部署，再用 `sync-configure` 交互输入密码和恢复字符串。Android 直接安装 APK，本地功能无需服务器；进入“更多 → 同步”后填写兼容的 HTTPS Sync v1 地址才启用同步。

## 2026-07-09：CI 拒绝请求测试稳定性

- 目标：修复 GitHub Actions Windows / Python 3.13 上 origin-policy 拒绝请求测试偶发 `ConnectionAbortedError` 的 CI 红灯。
- 改动文件：仅更新 HTTP 测试辅助和本开发记录；未改变生产 Host/Origin 校验逻辑。
- 核心功能：新增 `rejected_post` 测试辅助，对预期被拒绝且不会产生写入的 POST 请求在连接被系统中止时安全重试，避免 Windows socket 抖动掩盖真实断言。
- 验证：相关单测 `test_origin_policy_rejects_bad_port_null_cross_site_and_invalid_host`、43 项 `.\test.ps1`、`python tools\release_check.py` 和 `git diff --check` 通过。
- 仍未实现：未修改服务器拒绝逻辑；若未来生产请求也出现连接中止，需要另行排查 HTTP server 生命周期。
- 下一最小任务：等待 PR CI 重新跑完，确认 push 与 pull_request 事件都为绿色。
- 用户用法：无需改变。

## 2026-07-09：用户 API Key 手动生成

- 目标：在保留 Codex/Claude Code 外部 Agent 工作流的同时，让用户可用自己的 OpenAI、Anthropic 或 DeepSeek API Key 手动处理待办。
- 改动文件：新增标准库 HTTP 模型 provider 层，扩展 CLI、服务层、Web 待办按钮、运行时 API 接入页、配置诊断、README、Agent 规则、环境变量示例和自动化测试；未新增依赖、后台队列、持久化密钥存储或自动触发。
- 核心功能：`generate <TASK_ID>` 与 `day-generate <日期>` 会读取现有上下文、调用所选模型、解析 JSON，并继续走现有本地校验与完成逻辑；Web 的 pending 任务和复盘页新增“用 API Key 生成”表单，侧栏“API 接入”可在本次运行中启用或关闭 provider/model/key。
- 供应商边界：支持 `MEALCIRCUIT_AI_PROVIDER=openai|anthropic|deepseek`、显式 `MEALCIRCUIT_AI_MODEL`、对应 API Key、超时和最大输出 token；OpenAI 使用 Responses API 图片 data URL 与 JSON schema，Anthropic 使用 Messages API 图片 base64 与强制 tool result，DeepSeek 使用 OpenAI-compatible Chat JSON mode 且只处理文本任务。
- 验证：`python -m py_compile mealcircuit\ai.py mealcircuit\service.py mealcircuit\server.py mealcircuit\agent_cli.py mealcircuit\configuration.py tests\test_mealcircuit.py`、43 项 `.\test.ps1` 测试、`python tools\release_check.py` 和 `git diff --check` 通过；新增覆盖缺少环境变量不写库、OpenAI 照片 payload、Anthropic tool payload、DeepSeek Chat JSON mode、Web 运行时 API Key 模式启用/关闭且不回显密钥、非法模型结果保持 pending、Web 成功/失败路径。使用隔离 `MEALCIRCUIT_HOME` 与 DeepSeek `deepseek-v4-flash` 真实烟测通过：配置诊断、原材料生成和每日复盘生成完成；照片任务按预期拒绝 DeepSeek 未支持的图片输入并保持 pending。
- 仍未实现：不持久化保存 API Key、不提供后台自动清队列、不做供应商价格/可用性判断、不支持通用 OpenAI-compatible base_url、本地模型或营养数据库外查。
- 下一最小任务：用真实临时 API Key 对一条照片任务和一条每日复盘做端到端人工验收，重点看提示词是否足够稳定地产生可通过校验的 JSON。
- 用户用法：进入 Web UI 的“API 接入”页在本次运行内启用 key，或设置环境变量后重启服务；随后点击待办页按钮，或运行 `python -m mealcircuit.agent_cli generate <TASK_ID>` / `day-generate YYYY-MM-DD`。

## 2026-07-09：明日菜单承接剩余食材

- 目标：让独居下厨菜单在生成明日计划时承接上一轮三日复用方向，避免按采购清单多买的食材被下一次菜单静默忽略。
- 改动文件：更新每日复盘上下文与提交校验、Agent规则、README、自动化测试和本开发记录；未新增库存表、手动库存页面、商品链接、提醒或外部 API。
- 核心功能：`day-context` 新增 `ingredient_carryover_obligations`，从近 14 天已完成复盘的 `reuse_plan` 和 required 采购项中推导仍在复用窗口内的可能剩余食材；生成协议要求优先处理临期可用食材，结果 schema 新增 `ingredient_carryover_decisions`。
- 校验：独居模式下若存在承接食材，`day-complete` 必须覆盖全部承接 ID，并逐项写明 `use`、`skip` 或 `discard`、原因和计划用途；同一剩余食材可以换菜式复用，但连续晚餐重复菜品或主风味仍需合法 `repeat_reason`。
- 验证：`python -m py_compile mealcircuit\service.py tests\test_mealcircuit.py`、35 项 `.\test.ps1` 测试、`python tools\release_check.py` 和 `git diff --check` 通过，覆盖承接上下文导出、缺失裁决失败、完整裁决成功、同食材换菜式不触发重复、显式临期重复例外。
- 仍未实现：不追踪真实库存、实际购买确认、开封时间、价格、商品链接、物流或提醒；承接逻辑只把上一轮计划转成“可能剩余/应处理”的 Agent 约束。
- 下一最小任务：用真实一轮“买 400g、用 200g、剩 200g”的记录检查 Agent 文案是否足够明确地区分“默认可能已买”和“用户确认已买”。
- 用户用法：照常生成每日复盘；若上一轮菜单安排了复用食材，下一次 `day-context` 会要求 Agent 在明日菜单里使用、跳过或丢弃这些食材并说明原因。

## 2026-07-08：待办任务用户输入编辑

- 目标：让用户在照片或原材料任务处理前修正文字输入，同时继续保留完成任务的输入、结果和校正证据链。
- 改动文件：扩展任务数据库迁移、服务层、任务详情页、自动化测试、README、Agent规则与本开发记录；未增加依赖、账户权限系统、照片替换或完成任务重开。
- 核心功能：待处理任务详情页提供预填充编辑表单；原材料输入保持非空和 10000 字限制，照片备注允许清空；每次真实修改先归档旧版本，再以版本号原子更新当前输入，相同内容不重复留痕，过期表单与已完成任务均拒绝写入。
- 数据与 Agent：旧数据库自动新增 `input_version` 和 `task_input_history`，既有任务从版本 1 开始；`context` 使用最新 `task.original_input` 并同时导出 `task.input_history`；已完成任务继续通过用户校正追加事实，不覆盖锁定输入或结果。
- 验证：`python -m py_compile`、34 项 `.\test.ps1` 测试、`python tools\release_check.py` 和 `git diff --check` 通过。隔离数据库中的真实流程已完成“创建待办 → 编辑保存 → 查看旧版本 → 完成后锁定”；Edge Playwright 在 320、768、1024、1440px 确认无横向溢出，输入框可见、保存按钮 44px、Tab 可从输入框进入保存按钮，控制台零应用警告或错误。内置浏览器可读取页面并完成保存，但其 DOM 快照与导航等待接口不稳定，最终响应式截图改用本机 Edge 验证。
- 仍未实现：已完成任务不能修改或重开输入，只能追加用户校正；照片文件本身不能替换；输入历史首版只读，不提供一键恢复。
- 下一最小任务：用一条真实待办修改一次材料数量或照片备注，确认版本历史的文案和位置符合实际使用习惯。
- 用户用法：进入“全部任务”并打开 `pending` 任务，在“用户输入”中修改后保存；任务完成后该区域自动锁定，后续事实变化写入“用户校正历史”。

## 2026-07-08：标签图标与手动主题切换

- 目标：替换浏览器默认标签图标，并在保留系统初始偏好的前提下提供可持久化的浅色 / 深色切换。
- 改动文件：更新 `mealcircuit/server.py`、`mealcircuit/static/app.css`、`mealcircuit/static/app.js`、HTTP 测试与本开发记录；新增餐具 favicon、主题预加载脚本及 Lucide 太阳 / 月亮图标。
- 核心功能：所有页面引用 MealCircuit 深灰 / 薄荷绿 SVG favicon；顶部工具栏新增带动态辅助标签的主题按钮；首次访问读取系统主题，手动选择写入本机 `localStorage`，同步脚本在样式加载前应用主题以避免刷新闪烁；360px 以下将“记录状态”收敛为 44px 图标按钮。
- 验证：`python -m py_compile mealcircuit/server.py`、`git diff --check`、31 项 `.\test.ps1` 测试和 `python tools\release_check.py` 均通过。真实浏览器在 `http://127.0.0.1:8766/` 确认页面标题与 favicon 引用、深浅主题点击切换、刷新持久化、动态 `aria-label`、44px 主题按钮及控制台零错误；768、1024、1440px 无页面级横向溢出或顶栏重叠。320px 首轮检查发现旧缓存样式下的横向滚动，已通过移除固定最小宽度、压缩移动端主操作并提升静态资源版本修正；浏览器视口控制随后超时，备用 Playwright 运行时缺少 `playwright-core`，因此最终 320px 截图未能再次自动抓取。
- CI 修复：Windows 上不同 Python 版本会把 `.js` 识别为 `text/javascript` 或 `application/javascript`；HTTP 测试改为接受这两种标准 MIME 类型，不改变生产响应逻辑。
- 仍未实现：主题选择只有浅色 / 深色二选一，没有单独的“恢复跟随系统”入口；最终 320px 视觉复查仍需在可用浏览器会话中补做。
- 下一最小任务：重启 8765 服务后在 320px 视口复查顶栏和页面横向滚动；若未来需要恢复系统主题，再增加第三态设置而不是改变当前二选一按钮语义。
- 用户用法：重启 `.\start.ps1` 后，浏览器标签页会显示 MealCircuit 餐具图标；点击顶栏太阳或月亮按钮切换主题，刷新页面后选择保持不变。

## 2026-07-08：README 双语化与清晰截图

- 目标：把 README 从单语中文整理成 GitHub 友好的中英双语入口，并替换掉 GitHub 上发糊的首页演示图，同时统一对外版本展示到 `v0.2.0`。
- 改动文件：更新 `README.md`、新增无损截图 `docs/assets/mealcircuit-dashboard.png`、同步 `mealcircuit/__init__.py` 版本号，并记录本轮开发过程；删除旧的 `docs/assets/mealcircuit-dashboard.jpg`。
- 核心功能：README 顶部新增语言切换锚点，正文重排为完整 English / 简体中文 两段镜像结构；徽章版本与 release 链接统一到 `v0.2.0`；首页截图改为从真实本地服务抓取的 PNG，保留 1440x1024 视口但去掉有损 JPEG 压缩带来的模糊。
- 验证：`.\test.ps1` 31 项测试通过；`python tools\release_check.py` 零命中；真实浏览器检查 `http://127.0.0.1:8768/` 的首页，确认标题为“首页 · MealCircuit”、控制台无错误、桌面三列布局正常、处理队列只显示两条演示待办，并重新抓取 README 使用的 PNG 截图。
- 仍未实现：链接文档如 `PRIVACY.md`、`SECURITY.md`、`CONTRIBUTING.md` 仍保持当前语言，不随本轮 README 一起双语化。
- 下一最小任务：若后续还要继续国际化，优先补齐隐私、安全、贡献和免责声明页面的英文版本，并在 README 中改成按语言跳转。
- 用户用法：直接查看仓库根目录 `README.md` 即可；如需重新生成演示截图，可在隔离的 `MEALCIRCUIT_HOME` 下启动本地服务并抓取首页 PNG。

## 2026-07-07：Calibrated Console UI / UX 升级

- 目标：在不改变现有 URL、表单协议、数据库和 Agent CLI 的前提下，把廉价的荧光科技感界面改为克制、信息密集、可长期使用的本地营养工作台。
- 改动文件：重构 `mealcircuit/server.py`，新增 `mealcircuit/static/` 设计系统、交互脚本和 Lucide 图标；扩展 `service.py` 首页只读聚合与测试；更新 README、真实演示截图和设计验收记录。
- 核心功能：新增跟随系统的暗色/浅色主题、216/72px 可折叠侧栏、移动导航抽屉、首页三列总览、14 天已发布状态趋势、五模块状态、次日计划时间线和统一处理队列；复盘改为双栏报告，食品表单、历史列表、任务和设置页面统一为紧凑操作界面。
- 数据语义：`dashboard_snapshot()` 只读取正式发布状态；草稿不会进入趋势，跳过、缺失和未测量保持独立语义；首页 GET 不创建或重新排队复盘，没有正式复盘时不展示伪菜单。
- 验证：`.\test.ps1` 31 项测试通过，新增覆盖连续 14 天、草稿隔离、未知语义、菜单保护和首页读取无数据库副作用；浏览器已检查 375px 移动抽屉、Escape 与焦点恢复、1440px 三列界面和横向溢出。其余发布检查与最终设计 QA 见本轮 `design-qa.md`。
- 仍未实现：不新增手动主题开关、导出、同步、自动后台模型或虚构综合健康评分；所有 Agent 处理仍由用户发起。
- 下一最小任务：用真实但脱敏的日常长文本持续观察趋势密度、次日菜单长度和任务表格扫描效率，仅按真实使用阻力做局部调整。
- 用户用法：继续运行 `.\start.ps1` 并访问 `http://127.0.0.1:8765`；主题自动跟随系统，桌面侧栏折叠偏好仅保存在本机浏览器。

## 2026-07-06：独居新手餐单生成逻辑

- 目标：让即将独居且缺少做饭经验的用户得到真正可执行的餐单，不只知道吃什么，还能知道怎么买、怎么挑、怎么做、失败时怎么补救和剩余食材如何继续使用。
- 改动文件：扩展私人设置、每日复盘上下文与结果校验、今日建议页面、Agent规则、配置模板和测试；未增加数据库表、第三方依赖、外部商品 API 或库存录入。
- 核心功能：可选 `home_cooking` 模式固定早餐低摩擦组装、午餐食堂/外食、晚餐一人份下厨；晚餐限制在25分钟和两件主要炊具内，包含食材、调味时机、火力、完成标志、失败补救、清洁成本和肠胃降级；同时输出采购清单、最多3项网购筛选建议及三日复用方向。
- 轮换与兼容：`day-context` 导出近14天正式晚餐和近期网购品类；连续晚餐不得重复菜品或主风味，健康恢复、临期食材或采购限制可显式说明例外。旧设置自动视为未启用，既有复盘和旧页面继续可读，不迁移或重写历史。
- 验证：`.\test.ps1` 30项测试通过，覆盖旧配置、独居结构、25分钟上限、三日复用日期、历史上下文、连续重复例外、HTML转义和Web展示；真实浏览器检查320、768、1024、1440px均无页面横向溢出或餐单内容裁切，桌面端三餐与菜谱双栏清晰、320px自动单列，控制台无错误；`python tools\release_check.py` 零命中。
- 仍未实现：不追踪家庭库存、价格、商品链接、物流、提醒或固定七日菜单；三日复用是后续用途方向，每日仍会根据新状态校准。
- 下一最小任务：用首个真实独居晚餐验证采购量、步骤文字和25分钟限制是否符合实际，再把已验证的组合沉淀到食品库或长期记忆。
- 用户用法：未来每日复盘会自动按独居模式生成明日餐单；从“今日建议”直接查看晚餐执行卡、采购、网购筛选和三日复用，无需额外填写库存。

## 2026-07-06：本机回环来源校验修复

- 目标：修复每日状态表单在本机浏览器中因 `localhost`、`127.0.0.1`、`::1` 表示差异而被误判为跨来源请求的问题，同时继续拒绝外部来源和跨端口提交。
- 改动文件：`mealcircuit/server.py`、`tests/test_mealcircuit.py`、`DEVELOPMENT.md`；未修改问卷数据模型或数据库。
- 核心功能：来源校验拆分为可测试的 Host 解析和 Origin 匹配；相同端口的回环地址视为同一可信本机来源；`Origin: null` 仅在回环 Host 且 Fetch Metadata 为 `same-origin` 或 `none` 时通过；拒绝日志记录 Host、Origin 和 `Sec-Fetch-Site`，页面不暴露内部细节。
- 验证：`.\test.ps1` 27 项测试通过，新增覆盖回环别名、IPv6、跨端口、外部来源、`null` 来源、非法 Host，以及单选、多选、跳过、放弃草稿和设置保存的真实 Origin 头提交。隔离服务的问答页加载正常；内置浏览器控制在点击提交时被其自身 URL 安全策略阻止，因此实际点击需由用户在重启后的 8765 页面最终确认。
- 仍未实现：不接受缺少可信 Fetch Metadata 的 `Origin: null`；不放宽非回环监听模式的来源规则。
- 下一最小任务：重启 `.\start.ps1` 后在 `http://127.0.0.1:8765` 提交任一状态答案和一次跳过，确认页面进入下一题或返回状态主页。
- 用户用法：重启本地服务后继续使用“今日状态”；无需迁移数据或重新填写既有配置。

## 2026-07-04：每日状态自适应问答

- 目标：把体重、训练、饥饿饱腹、睡眠和肠胃反应从依赖自然语言补充，改为软件逐题提问、用户主要点击完成的每日状态回路。
- 改动文件：新增 `mealcircuit/checkins.py`；扩展 `db.py`、`service.py`、`server.py`、测试、README、AGENTS 与本开发记录；未增加第三方依赖。
- 核心功能：新增五个自适应模块、逐题持久化草稿、完成后发布、明确跳过、同日修改与旧版本历史；新增 `/check-ins/<日期>`、模块问答和设置页；支持模块隐藏、排序、每日/按需频率；首页、今日建议和历史复盘加入状态入口。
- Agent 上下文：`day-context` 新增 `target_checkin`、`checkin_coverage`、`recent_checkins` 和解析规则；只导出已发布版本，草稿隔离；每日复盘记录所使用的模块版本，状态问答也可独立创建待复盘。
- 验证：`.\test.ps1` 25 项测试全部通过，覆盖旧库兼容迁移、五类答案分支、无效分支拒绝、草稿隔离、分支裁剪、跳过语义、版本冲突、历史归档、复盘只重开一次、设置进度、未来日期拒绝、HTTP 问答流程与同源保护。隔离数据库上的真实页面检查覆盖 320、768、1024、1440px，无页面级横向溢出；问答卡键盘原生控件完整，控制台无错误。
- 仍未实现：首版不提供任意自定义模块、提醒通知、账户同步或后台自动模型调用；明确跳过仍保持未知，不生成替代答案。
- 下一最小任务：用真实日常记录完成五个模块，检查 Agent 复盘是否正确使用新信号，并根据实际完成阻力微调问题文案和选项顺序。
- 用户用法：重启 `.\start.ps1`，从主导航“今日状态”进入；逐个完成或跳过模块，需要调整时进入“调整模块”。完成模块后再按既有 Agent 工作流处理待复盘。

## 2026-07-01：本地 Agent-in-the-loop MVP

- 目标：实现图片所示 DietOS 功能，提供真实 UI、SQLite 持久化和无 API Key 的 Agent 处理路径。
- 改动文件：`dietos/` 应用包、`tests/`、`start.ps1`、`test.ps1`、`README.md`、`AGENTS.md`；删除此前未被实现使用的提示词、规格、Markdown 记录模板和建档脚本；保留原始总纲。
- 核心功能：食物照片上传并安全落盘、原材料任务、任务列表、两类结果的人类可读展示与折叠原始 JSON、食品营养库 CRUD 与历史、每日记录、长期记忆、当前调整、Agent 上下文导出、结构化结果校验与提交、用户更正历史。
- 验证：9 项自动化测试通过，覆盖数据持久化、两类任务、14 天上下文、食品库 CRUD/历史、合法结果、非法结果拒绝、防覆盖，以及真实 HTTP 服务的五个页面、原材料表单、照片上传表单、两类完成结果的人类可读字段和 HTML 转义。另以运行中的服务确认 `/`、`/tasks/photo`、`/tasks/material`、`/foods`、`/overview` 均返回 HTTP 200。主 Agent 已实际完成首页、食物照片、原材料分析、食品营养库、记录与记忆入口的 DOM 和截图检查。
- 仍未实现：自动后台 AI、移动端、账户/云同步、包装 OCR、外部营养数据库。照片和原材料营养由 Agent 基于证据做区间估算。
- 下一最小任务：用户直接创建首个真实待办，并在 Codex / Claude Code 中说“处理 DietOS 待办任务”；无需先准备测试饮食记录。
- 用户用法：运行 `.\start.ps1`，访问 `http://127.0.0.1:8765` 创建任务；Agent 按 README 中的 CLI 流程处理。

## 2026-07-01：Web UI / UX 精修

- 目标：在不改动业务内容、数据结构和处理流程的前提下，统一 Web 端视觉语言并改善桌面端与移动端可用性。
- 改动文件：`dietos/server.py`；沿用标准库服务端渲染方案，未增加依赖。
- 核心功能：建立语义化深色营养工作台设计令牌；加入三段式营养标尺品牌元素、稳定的间距与排版层级、响应式导航与网格、移动端表格滚动、44px 触控目标、清晰的 hover / focus / active 状态、当前导航状态、减弱动态效果支持；补齐表单 label 关联、跳转主内容链接、单一 h1、表格区域语义和列标题 scope。
- 验证：`.\test.ps1` 9 项测试全部通过；`python -m py_compile dietos\server.py` 通过；关键前景/背景配色对比度实测均高于 9:1；启动真实服务后在浏览器逐页检查 `/`、`/tasks/photo`、`/tasks/material`、`/foods`、`/overview`，桌面端与 375px 移动端均无页面级横向溢出、无未关联标签的表单字段、每页恰有一个 h1，控制台无错误。
- 仍未实现：本轮未引入主题切换、客户端状态管理或新业务交互；原有内容与工作流保持不变。
- 下一最小任务：在有真实食品与任务数据后复查长文本、长品牌名和多行任务表格的极端内容布局。
- 用户用法：仍运行 `.\start.ps1` 并访问 `http://127.0.0.1:8765`，无需迁移数据或调整配置。

## 2026-07-02：每日核心建议与次日菜单

- 目标：让 DietOS 对每个日期的记录固定承担核心建议和次日菜单责任，并支持个人化用餐环境、份量方式和蛋白目标（具体值已移入私人配置）。
- 改动文件：`dietos/db.py`、`service.py`、`validation.py`、`agent_cli.py`、`server.py`、测试与运行文档；正式数据库新增每日复盘及版本历史表。
- 核心功能：每日记录自动创建按日期唯一的待复盘；同日补充保存旧版本并重开；Agent CLI支持统一待办、日期上下文和复盘提交；Web展示事实、推断、核心建议、三餐菜单、条件加餐及训练/肠胃调整。
- 数据迁移：保留既有私人记录与偏好；回填首个版本复盘和次日菜单；未新增重复每日记录。
- 验证：12项自动化与HTTP集成测试通过，覆盖复盘排队、日期唯一、版本历史、上下文设置、结果校验、防覆盖、Web菜单展示和HTML转义；主Agent在真实浏览器中检查了2026-07-02复盘、2026-07-03三餐菜单、记录页入口和横向溢出，显示正常。
- 仍未实现：无API Key模式不会后台调用模型；每日记录只会自动产生待办，需Codex / Claude Code处理。
- 下一最小任务：用后续记录验证第二个日期的自动复盘，并在积累趋势后校准私人目标区间和菜单份量。
- 用户用法：保存每日记录后说“处理 DietOS 待办任务”，再从记录与记忆页打开对应日期查看核心建议和次日菜单。

## 2026-07-02：一级今日建议与优先食品

- 目标：把“今日建议”提升为与照片、原材料同级的主页入口，并让用户常备食品实际进入食品库与菜单决策。
- 核心功能：新增`/daily`已完成/待处理/未记录三态；主页三等权卡片；食品库增加类别、优先级、默认份量、使用条件、纤维和钠；每日上下文加入高优先级食品并强制逐项使用/跳过裁决。
- 数据迁移：持久化包装标签并幂等录入多项常备食品；既有复盘版本已归档，新版本按私人条件裁决常备食品。
- 验证：13项自动化测试通过；正式库保留每日记录、高优先级食品和复盘版本历史。真实浏览器验证主页、`/daily`、营养库及360px移动端无页面级横向溢出。
- 仍未实现：个别私人食品属性尚未确认；条目保留未知状态，未进行猜测。
- 下一最小任务：以后每次菜单生成观察高优先级食品裁决是否符合当日正餐结构，并在用户确认属性后更新。
- 用户用法：重启本地服务后从主页“今日建议”直接进入；食品库可查看并编辑高优先级食品。

## 2026-07-02：MealCircuit 开源隔离与工程重命名

- 目标：将项目重命名为 MealCircuit（食回路），把真实数据库、照片、私人总纲和配置迁出源码目录，建立可验证的开源发布边界。
- 改动文件：应用包重命名为 `mealcircuit/`；新增统一存储、私人配置和迁移模块、公开核心规则、初始化模板、发布检查、开源文档与 CI；更新测试、启动脚本、README 和 Agent 规则。
- 核心功能：新增 `MEALCIRCUIT_HOME`、动态私人设置、完整私人总纲覆盖、`init`、`doctor`、`migrate-data`；迁移使用 SQLite Backup API、完整性检查、逻辑摘要和 SHA-256 文件清单；Web 增加非回环监听门禁、Origin/Host 校验和安全响应头。
- 数据迁移：真实数据已复制到操作系统私人数据目录，另有迁移前独立备份；源/目标数据库完整性均为 `ok`，表行数一致，核心记录逐行一致，总纲哈希一致，媒体文件可解析；验证后已清除源码目录内的私人副本。
- 验证：`.\test.ps1` 19 项测试全部通过；`python tools\release_check.py` 零命中；真实服务的 `/`、`/daily`、照片、原材料、食品库和概览页面均返回 200，显示 MealCircuit 且不显示旧品牌，并包含 CSP、`X-Frame-Options`、`X-Content-Type-Options` 和 Referrer Policy。内置浏览器控制因本地运行环境路径错误未能初始化，因此本轮没有完成视觉截图检查。
- CI 修复：GitHub Windows Runner 会把同一临时目录分别表示为 8.3 短路径和长路径；初始化测试改为使用 `Path.samefile()` 比较文件身份，避免依赖路径字符串表现形式。
- 仍未实现：未创建远程公开仓库；MealCircuit 的正式商标清查不属于代码验证范围。域名是否可用也不作为本地开源发布门禁。
- 下一最小任务：在本地 Git 发布门禁通过后，根据用户选定的托管账号创建公开远程仓库；正式商标清查仍需独立完成。
- 用户用法：先运行 `python -m mealcircuit.agent_cli init` 并填写私人设置；用 `doctor` 查看实际数据位置；日常仍可运行 `.\start.ps1` 并在 Agent 中说“处理 MealCircuit 待办任务”。

## 2026-07-03：GitHub README 产品化

- 目标：提升项目在 GitHub 首屏的品牌辨识度、产品定位与首次使用路径，同时保持能力描述与真实实现一致。
- 改动文件：`README.md`、`assets/readme/mealcircuit-hero.svg`、`DEVELOPMENT.md`。
- 核心功能：新增独立的 MealCircuit 回路视觉、项目状态徽章、三项产品原则、工作闭环图、产品入口矩阵、分流后的 Agent 操作示例以及更集中可读的数据与能力边界。
- 验证：Markdown 结构与相对链接检查通过，SVG XML 可解析并成功渲染为 1200×460 PNG 完成视觉检查；`git diff --check` 通过，19 项自动化测试全部通过，开源发布检查零命中。
- 仍未实现：本轮未增加产品功能、自动后台 AI、云同步或移动端，也未使用含私人数据的界面截图。
- 下一最小任务：首次公开发布后，复查 GitHub 实际渲染效果及徽章状态，并仅在无私人数据的演示库可用时补充真实产品截图。
- 用户用法：从 README 首屏按“快速开始”初始化，或通过“Agent 工作流”直接处理已有待办。

## 2026-07-04：历史建议卡片与首页信息降噪

- 目标：让用户能从明确入口回看全部历史“今日建议”，并移除首页对普通用户无价值的照片任务明细。
- 改动文件：`mealcircuit/server.py`、`tests/test_mealcircuit.py`、`DEVELOPMENT.md`；私人食品库同步更正一条鸡胸肉名称与品牌，不进入源码仓库。
- 核心功能：新增 `/history` 历史建议页及主导航入口；以日期、状态、一句话复盘、首条核心建议和次日菜单组成紧凑卡片；“记录与记忆”页改为最近建议卡片并提供查看全部入口；首页删除“最近任务”表格及照片任务 ID；今日建议页增加历史入口。
- 私人数据：`food_eea2ceb33f9f` 已通过食品库服务更新为“低脂水煮鸡胸肉（原味）”，品牌为“袋鼠先生”，保留原有营养标签数据和修改历史。
- 验证：`python -m py_compile mealcircuit/server.py` 通过；`.\test.ps1` 19 项测试全部通过；真实服务 `/history`、`/overview`、`/` 和 `/foods` 返回 200。内置浏览器验证桌面双列与 360px 单列卡片、历史卡片详情跳转、单一 h1、无横向溢出、首页不再出现任务 ID，控制台无警告或错误。
- 仍未实现：历史建议暂不提供日期筛选和分页；当前数据量较小，先保持完整列表以降低交互复杂度。
- 下一最小任务：历史记录增长到影响扫描或加载时，再增加按月分组或年份筛选，不提前引入分页状态。
- 用户用法：从主导航“历史建议”、首页“历史建议”按钮或“记录与记忆”中的“查看全部”进入；点击卡片“打开复盘”查看完整建议与菜单。

## 2026-07-04：仓库改动草稿 PR 交付 Skill

- 目标：把“新分支、最小粒度提交、推送远程、自动创建草稿 PR、等待用户审批”固化为所有带远程仓库改动的默认交付协议。
- 改动文件：`skills/ship-changes-via-draft-pr/SKILL.md`、`skills/ship-changes-via-draft-pr/agents/openai.yaml`、`DEVELOPMENT.md`。
- 核心功能：有 Git remote 时在首次编辑前创建独立 `codex/` 分支；隔离既有改动；按可独立审阅和回滚的逻辑单元提交；完成验证后推送并自行创建或更新草稿 PR；禁止自动合并、转为 Ready、强推和静默夹带用户改动。
- 验证：仓库源与本地安装副本均通过 `quick_validate.py`；对应文件 SHA-256 一致；`git diff --check` 通过；19 项自动化测试全部通过；开源发布检查零命中。
- 本地安装：已从远程分支安装到 `$HOME/.codex/skills/ship-changes-via-draft-pr`，需要重启 Codex 后进入后续会话的自动技能发现。
- 仍未实现：Skill 不会自动合并或替用户审批 PR；没有 Git remote 的本地目录只执行本地修改与验证，并明确跳过发布步骤。
- 下一最小任务：重启 Codex 后，在下一次仓库修改任务中验证隐式触发、原子提交和草稿 PR 交付链路。
- 用户用法：正常提出任何仓库修改需求即可；也可显式说“使用 `$ship-changes-via-draft-pr` 完成这次改动”。

## 2026-07-11：自适应闭环 P0 安全与可追溯基础

- 目标：保留已实现的闭环领域代码，同时把审计发现的安全门、目标来源、受限模式泄漏、反馈历史、规则作用域、迁移兼容和生成 provenance 问题纳入正式实现。
- 改动文件：`mealcircuit/db.py`、`personalization.py`、`adaptive.py`、`service.py`、`ai.py`、`validation.py`、`configuration.py`、`storage.py`、测试与 `docs/adaptive-closed-loop-verification.md`。
- 核心功能：引入带校验和的 v1/v2 迁移账本与升级前备份；新增独立营养目标版本及来源、方法、适用范围、确认与有效期；区分 standard、clinician-guided 与 halt-and-refer 安全资格；统一 context/generate/complete 安全门；受限模式使用不含建议字段的 fact-only Schema；执行回执修订写入追加事件；候选、规则、实验和反馈绑定档案、目标、策略、安全模式及 Policy；任务、复盘和 Agent run 保存 doctrine hash、Policy/Schema/Validator 版本、source manifest、context/result hash，且不保存 API Key。
- 验证：`.\test.ps1` 58 项测试全部通过；`python -m compileall -q mealcircuit tests\test_adaptive.py tests\test_mealcircuit.py` 通过；`git diff --check` 通过。新增覆盖未初始化门禁、孕期无专业指导的受限行为、专业目标 provenance、fact-only 字段拒绝、反馈历史和 Agent run 审计。
- 仍未实现：计划投影与硬约束编译、救场生成、完整 Web/CLI 闭环、数据导入导出、周期校准 UI、浏览器与无障碍验收尚未完成；本检查点不是交付终点。
- 下一任务：建立 plan versions/items 投影和确定性约束编译器，将确认规则、库存、目标和安全模式真正约束下一份计划及救场结果。
- 用户用法：现有记录入口保持可用；生成和提交现在要求先完成目标/安全初始化，clinician-guided 模式还要求已确认且仍有效的专业指导。

## 2026-07-11：自适应闭环纵向工作台

- 目标：把初始化、目标策略、事实记录、受约束计划、执行回执、救场、确定性学习、用户确认规则/实验、周期校准和数据迁移连成同一个可运行闭环，而不是互不相连的原型。
- 改动文件：新增 `mealcircuit/planning.py` 与 `mealcircuit/portability.py`；扩展数据库迁移、个性化、安全策略、Agent run、服务、CLI、Web、样式、测试、README 和验收矩阵；保持 Python 标准库方案。
- 核心功能：可恢复七步初始化创建带版本的档案、目标、策略和有来源/方法/适用范围/确认时间/有效期的营养目标；自定义目标保留用户原话；没有体重或数值目标时使用份量策略并保持数值未知。正式计划投影为不可变 plan/version/item，确认规则成为提交前硬约束；库存、低频问题和单变量实验进入同一上下文。
- 学习闭环：计划回执使用当前投影加追加事件历史；重复支持与反例由确定性阈值生成候选，候选不会直接进入硬上下文；接受、拒绝、暂停、规则启停和 3–7 日单变量实验均绑定目标、档案、策略、安全模式和 Policy 版本。救场绑定原计划版本并通过同一约束编译器，完成后自动追加执行事件。
- 安全与 provenance：统一 `require_generation()` 覆盖 Web、CLI、context、generate、complete、rescue 和 adaptation；clinician-guided / halt-and-refer 隐藏旧处方计划和已过期目标，仅保留记录及事实型照片/原材料 Schema。Source manifest 保存 doctrine hash、档案/目标/策略/目标值、规则/实验版本、Policy/Context/Result/Validator 版本和 Agent run ID；模型调用与外部 Agent JSON 提交都记录成功或失败 run，不保存 API Key。
- 迁移与兼容：v1–v4 迁移带校验和、拒绝未来版本并在升级前备份；旧设置可在初始化中重新确认，初始化只限制生成，不移除记录入口或旧总览。首次直接启动 Web 会创建私人目录模板。可移植 ZIP 使用安全路径、SHA-256 清单、Schema/SQLite 完整性预览、全量恢复前 ZIP 备份、原子数据库/配置替换和精确媒体恢复。
- Web/CLI：Web 主流程为“今天 / 计划 / 记录 / 洞察 / 学习确认 / 库存 / 目标与边界”，并提供初始化、回执修订、救场、指标、实验和备份恢复；CLI 覆盖 setup、plan、feedback、questions、learning/rules/experiments、inventory、evidence、rescue、metric、calibration、export/import。
- 无障碍：表单错误使用 `role=alert`；移动抽屉设置 `aria-hidden`、背景 inert、焦点进入/返回与 Tab 循环；页面保持单一 h1、原生标签控件、键盘路径、减少动画和 320px 单列响应式布局。
- 验证：领域专项 22 项通过；最近一次全量 67 项、`compileall`、`git diff --check` 和 `tools/release_check.py` 零命中。隔离数据库真实浏览器已完成七步初始化、实验提出/启动/完成、指标历史、正式计划救场及回执回写；320/720/1440 有效视口均无横向溢出，移动导航通过焦点进入/返回、Escape 与背景 inert 验证，页面保持单一 h1、控件标签完整且无控制台警告或错误。此前还验证了记录、问题、候选规则、数据页和受限模式无旧目标泄漏。
- 仍未实现：无自动后台模型、账户、云同步、诊断、治疗建议或自动接受学习规则；这些是明确产品边界，不是本闭环缺口。
- 下一任务：Draft PR #14 已创建并保持 Draft；等待 GitHub Actions 后由 reviewer 审阅，不自动合并或标记 Ready。
- 用户用法：运行 `.\start.ps1` 后按 Web 初始化进入“今天”；先记录真实情况，打开正式计划执行并回执，重复阻力会出现在“学习确认”；“目标与边界 → 备份与迁移”可导出完整 ZIP。也可用 `python -m mealcircuit.agent_cli --help` 查看同等 CLI 流程。

## 2026-07-12：CI 跨版本兼容修正

- 目标：修复 Draft PR #14 在 GitHub Windows runner 暴露的 Python 3.11 语法、CLI 编码和 Windows 短路径兼容问题，不改变领域行为。
- 改动文件：`mealcircuit/server.py`、`mealcircuit/agent_cli.py`、`tests/test_adaptive.py`。
- 核心功能：把计划步骤、学习页和救场完成态中的 Python 3.12+ 嵌套 f-string 拆为兼容 3.11 的预计算片段；CLI 明确使用 UTF-8 标准输出；可移植包与初始化路径断言按文件身份/规范路径比较，兼容 runner 的 `RUNNER~1` 与长路径别名。
- 验证：自适应专项 23 项通过；本机 Python 3.13 与临时非安装式 Python 3.11.9 均完成全量 67 项；两版 `compileall`、`git diff --check` 和 `tools/release_check.py` 通过。
- 仍未实现：无新增产品缺口。
- 下一任务：Draft PR #14 保持 Draft，等待 reviewer 审阅；不自动合并或标记 Ready。
- 用户用法：无变化；CLI 在 Windows 重定向或子进程调用时也稳定输出 UTF-8 JSON。
