# MealCircuit Agent 运行规则

## 最高规则

- `context` / `day-context` 返回的 `doctrine.content` 是本次饮食判断的最高规则，不得静默改写；其来源由 `doctrine.mode` 和 `doctrine.sources` 标明。
- 照片/原材料分析使用 `context <任务ID>`；每日复盘使用 `day-context <日期>`。两者都必须读取近14天记录与已发布状态、长期记忆和当前调整，不得依赖聊天模型自身记忆。
- 默认不调用外部模型 API，不要求用户配置 API Key；仅在用户显式通过环境变量或 Web UI 的“API 接入”页面配置 provider、模型和对应 API Key 后，才允许通过内置手动生成命令调用用户选择的模型服务。Web UI 接入只允许把 key 放入当前服务进程环境，不得落库或写文件；DeepSeek provider 只用于文本型原材料和每日复盘，照片任务不得伪造未支持的视觉输入。

## 处理待办任务

1. 使用 `python -m mealcircuit.agent_cli pending` 统一取得照片、原材料和每日复盘待办。
2. 使用 `context` 导出完整上下文。`task.original_input` 是用户当前确认的最新输入，`task.input_history` 保留其修改历史；照片任务必须读取 `task.image_path` 指向的本机图片。
3. 依据上下文内 `result_schema` 生成 JSON。所有营养值使用区间；无法判断的营养区间使用 `null`。
4. 照片不得伪造不可见的油、酱汁、重量或品牌，必须放入 `unknowns`。置信度必须反映可见证据。
5. 原材料任务优先引用 `food_library_matches` 中的用户数据；没有数据时明确估算依据和风险。
6. 综合总纲、近 14 天趋势、长期记忆和当前调整给出最小有效调整，不孤立识别食物。
7. 使用 `complete` 提交。校验失败时修正 JSON；不得绕过校验或直接改数据库。
8. 待处理任务的文字输入可由用户在详情页修改并保留版本历史；任务完成后，用户更正使用 `correct` 追加，不覆盖已锁定输入或既有结果。

## 每日核心建议与菜单

1. 每条每日记录或正式发布的状态模块都会按日期创建唯一复盘；使用 `day-context YYYY-MM-DD` 获取上下文。
2. 每日复盘必须读取 `target_checkin`、`checkin_coverage` 和 `recent_checkins`。同日期同模块以最新已发布版本为准；草稿不会导出，跳过和缺失都保持未知，不得推断为否定答案。
3. 每日复盘必须给出事实、推断、1–3条核心建议、无需调整项、风险信号和一句话复盘。
4. 次日菜单必须包含早餐、午餐、晚餐、条件加餐、训练日调整和肠胃异常调整；用餐环境以 `day-context.settings.meal_environment` 为准。
5. 蛋白目标以 `day-context.settings.protein_target_g` 为准。达到目标下界后不得为了数字机械加餐。
6. 不得只写“不补偿性节食”；必须明确写成“不跳餐、不清零主食、不极端压低热量，只撤掉重复加餐并恢复标准份量”。
7. 用户补充同日记录后，旧复盘进入历史，新版本重新分析；不得静默覆盖。
8. 使用 `day-complete YYYY-MM-DD --file result.json` 提交，禁止直接改数据库绕过校验。
9. `day-context` 中的 `priority_foods` 必须逐项评估；结果的 `priority_food_decisions` 必须覆盖全部高优先级食品，标明 `use` 或 `skip` 及具体原因。
10. 高优先级只表示满足对应功能条件时优先；具体食品用途、份量和跳过条件必须读取食品库及私人设置，不得在公开规则中假定。
11. 三餐的 `home_cook`、`quick_assembly` 或 `eat_out` 方式以版本化个人策略中的 `day-context.settings.meal_modes` 为准，不得由仓库规则固定。存在在家下厨餐次时，必须读取 `recent_home_meals`、`recent_online_categories` 和 `home_cooking_generation_protocol`；每个 `home_cook` 餐次分别生成一人份新手执行卡，并完整提供共享采购、网购筛选和三日食材复用信息。
12. `ingredient_carryover_obligations` 是上一轮复用计划推导出的可能剩余食材；生成明日菜单前必须逐项评估，并在 `ingredient_carryover_decisions` 覆盖全部项目。可用且临近窗口结束的食材优先进入明日午餐或晚餐；若今日记录否定库存、已坏或与肠胃状态冲突，必须使用 `skip` 或 `discard` 写清原因。
13. 每个在家下厨餐次均不得超过个人策略中的时间和炊具限制；同一餐次连续下厨不得重复菜品或主风味。确因健康恢复、临期食材或采购限制重复时，必须使用允许的 `repeat_reason` 明确说明，不得用“方便”笼统绕过轮换。

## 开发约束

- 只记录真实完成和真实验证，不把待处理协议描述成自动 AI 识别。
- 每轮实质开发后更新 `DEVELOPMENT.md`：目标、改动文件、核心功能、验证、仍未实现、下一最小任务、用户用法。
- 运行 `.\test.ps1`。需要检查 UI 时启动 `.\start.ps1`，再直接检查页面与关键路径。
- 保持 Python 标准库方案；增加依赖或架构变化前需用户明确授权。内置模型接入必须继续使用标准库 HTTP 客户端，不保存 API Key，不把密钥写入数据库、日志或页面。
