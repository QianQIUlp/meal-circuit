# 开发过程记忆

## 2026-08-12：v0.3.1 发布基线清障（仅 Windows x64 + Android）

- 目标与范围：按当前产品范围只维护 Windows x64 与 Android；本轮清除已知发布阻碍并补齐可重复门禁，不执行提交、推送、打 tag 或公开发布，也不要求在本机生成已签名的最终六项资产。Python Web/CLI 属于 Windows 实现，Android 继续只消费经 Windows/Python 七阶段流程审查并发布的计划。
- 安装与首次使用：修复 wheel/sdist 缺少 `rules/`、`templates/`、协议和法律资料的问题，统一源码、冻结包与 wheel 的资源定位；新安装模板允许蛋白目标在 onboarding 前保持未知，`init` 后 `doctor` 不再误报。发布门会真实构建 wheel/sdist、拒绝 `pyc/__pycache__`，隔离安装后再运行 `init/doctor`。构建后端固定为支持 PEP 639 的 `setuptools==83.0.0`。
- 产品与边界：修复“我的”页 390px 主题控件横向溢出；README、v0.3.1 发行说明、中英文站点、安全与威胁模型统一说明无捆绑规划模型、模型 API Key 仅存当前 Python 进程、Android 不执行本地每日生成；公开下载范围固定为 Windows portable/setup、Android APK/AAB，加 SBOM 与 SHA256 共六项。
- 发布门：tag 会等待同一 tag/commit 的完整测试 workflow，私密漏洞报告、Windows Authenticode、Android 四项签名凭据均 fail closed；Android APK 与 keystore alias 必须延续公开 v0.3.0 证书 SHA-256，AAB 严格验签；release 只下载四个最终平台产物并生成锁文件派生的 CycloneDX SBOM 与校验和。Windows 包从实际锁定环境收集项目、Python 与每个分发包的许可证并在冻结包、便携包、安装目录复验；Android 在应用内提供六份法律文本，并直接打开最终 APK/AAB 核验法律资产、许可证全文及 ZIP 安全边界。
- Windows 真实窗口：新增隐藏的 `--ui-smoke-test`，使用一次性数据目录真正创建 pywebview/WebView2 窗口，等待首个页面加载后关闭，且有 45 秒加载上限与进程级 watchdog；发布 workflow 对冻结 EXE 单独执行 70 秒有界的真实 WebView 门禁。当前源码 WebView 与重新构建的 PyInstaller EXE 均实跑成功，冻结 EXE UI smoke 约 30.59 秒退出 0；此前也以隔离 home 人工检查过实际原生窗口和首次设置页。
- Android 法律与构建：新增 installed-app instrumentation 法律资产测试和最终 APK/AAB 标准库检查器；Android 源码法律门与恶意 ZIP/篡改/缺失回归通过。当前轮隔离 JDK 17 / SDK 36 验证中，JVM 单测 30/30 通过，生成的 debug APK 为 `org.mealcircuit.app`、`versionCode=30001`、`versionName=0.3.1` 并包含全部六份法律资产，androidTest Kotlin 也已编译；按用户缩小验收目标后中止长跑，因此本轮未完成 lint、设备 instrumentation 或 release 构建，没有把未完成的签名成品当作已发布证据。
- 验证：当前稳定快照的 Python 全量在发布范围收敛前为 333 通过、0 失败、1 项因本机未配置 PostgreSQL URL 跳过；随后发布/法律/真实 GUI 增量定向 48/48 通过。另有发布策略/版本/站点/SBOM等 39 项通过（1 项 actionlint 本地跳过，独立 actionlint 已通过）、资源/桌面/Android 24 项通过；`tools/dependency_check.py`、OpenAPI、协议、版本、`release_check`、两种 uv 的 lock check、wheel/sdist 隔离安装、SBOM 239 components、Windows 与 sync 两套 `pip-audit` 均通过并报告无已知漏洞；`git diff --check` 通过。浏览器实测 390px `/me` 的 document/body scrollWidth 均为 375、主题控件边界 45.33–329.33；桌面产品站 1440px 无溢出。
- 未执行的外部动作与当前外部阻塞：仓库的 private vulnerability reporting 仍未开启，Windows Authenticode secrets 尚未配置，v0.3.1 本地变更尚未形成提交/推送/tag，公开 v0.3.1 Release 尚不存在。因此不要先把 README/站点中的 v0.3.1 链接部署到 `main`；正确顺序是先配置管理员门禁与签名材料，再提交并跑绿同 tag CI、生成并核验 Release，最后上线对应站点链接。本轮没有改写或丢弃共享的 `tests/test_mealcircuit.py`；其 SHA-256 保持 `07E3561607A6690741385D241B36E356362A3B0FF1D62F195875FA136F812D47`。

## 2026-08-09：Windows 冻结包版本资源接缝

- 原因：`mealcircuit.__init__` 在冻结环境没有 distribution metadata 时，按既定版本语义回退读取项目根 `pyproject.toml`；PyInstaller spec 原先只携带静态资源，导致 clean Windows EXE smoke 缺少 `_internal/pyproject.toml`。短路径 locked sync 已成功，因此未修改 proxy-tools、pywebview、pyproject 依赖或 `uv.lock`。
- 选择与改动：在 spec 中携带项目根 `pyproject.toml`，继续使用单一项目版本源；没有改为依赖 distribution metadata，因为该元数据不是当前 spec 的稳定跨桌面资源合同，且 PyInstaller spec CLI 不接受直接的 metadata 复制选项。新增 Windows 桌面专项回归断言，防止资源接缝回归。
- 文件：`packaging/mealcircuit.spec`、`tests/test_windows_desktop.py`、`DEVELOPMENT.md`。
- 验证：任务目录临时 spec 加同一资源项后，clean PyInstaller EXE smoke exit 0；隔离 clone 的 `tests.test_windows_desktop` 为 9/9 通过；`tools/dependency_check.py`、`tools/release_check.py`、desktop `pip-audit --requirement requirements/desktop.lock` 均 exit 0，后者报告无已知漏洞；依赖使用 uv 0.8.22 locked sync，`proxy-tools==0.1.0` 正常构建。

## 2026-08-09：Web RenderContract UTF-8 index 修复

- 改动：仅将 `RenderContractTest` 的 mojibake 字符串恢复为当前工作树中的正确 UTF-8 字节；未修改 `server.py`，也未将独立的 `AndroidBoundaryTest` 纳入 index。
- 验证：Python 3.13.14、uv 0.8.22 的隔离 staged-tree 中，RenderContract 3 项、photo/material 3 项、AdaptiveDomain fact-only 1 项、`compileall` 和 `git diff --check` 均退出 0；测试文件 SHA-256 为 `ee476cca2795820e968649f343515cdbee4ba555f3cbab33afbfb3e4c24c291b`，RenderContract 片段 SHA-256 为 `d4862e9cba278e489c456b48a765d4c12b616420c5e10e0d1695f774f20b62a1`。
- 限制：提交后需从最终 HEAD 隔离导出复核同一专项矩阵；AndroidBoundaryTest 仍作为原有未暂存改动保留。

## 2026-08-09：依赖规范锁与 canonical OpenAPI checkpoint

- 改动：提交 `7e14d8e` 使用 uv 0.8.22 从 HEAD 机械生成规范锁并升级 `cryptography` 到 50.0.0；随后提交 canonical OpenAPI 生成器、环境污染专项回归和生成件。
- 验证：Python 3.13、uv 0.8.22 下 `uv lock --check`、OpenAPI 专项 unittest、干净/污染 `MEALCIRCUIT_SYNC_*` 的 `generate_openapi.py --check`、`dependency_check.py`、`validate_protocol.py`、`release_check.py` 以及 desktop/sync-server 两套 `pip-audit` 均退出 0；`uv.lock` SHA-256 为 `6a50bf318713e708c67099982c819e4aaa89738a6fe43888b203128b31d9fbf7`；此前 Windows 工作树 OpenAPI 输出的 `770f304570b7afb9ed4151af8dd78084d3c4e769d7b3db2b1f802fcc3af01365` 是 CRLF 表示，不作为跨平台 canonical 哈希。
- 限制：本合同未运行全量 Python、PostgreSQL、Android Gradle 或 API 35 instrumentation；现有 Web/Android 未提交改动保持未暂存且未纳入本次提交。

## 2026-08-09：OpenAPI canonical bytes 跨平台修复

- 改动：`generate_openapi.py` 的 `--output` 以 UTF-8/LF 写出；专项回归同时检查干净与污染 `MEALCIRCUIT_SYNC_*` 环境的原始字节、SHA-256 和换行数量。
- 验证：Python 3.13.14、uv 0.8.22 隔离候选副本中，两个输出均为 30,357 字节、1,158 个 LF、0 个 CRLF，SHA-256 均为 `5798b82c626918dd92058e655421577dd129a390f624a0ae4990b440b91d6939`；专项 unittest、干净/污染 `--check`、协议、依赖、发布检查和 `git diff --check` 均退出 0。
- 限制：本轮未运行全量 Python、PostgreSQL、Android 或 Web 渲染验证；现有 `server.py` 和 `tests/test_mealcircuit.py` 脏改动保持不变。

## 2026-08-09：Web fact-only 与 nullable target 渲染合同

- 改动：Web 结果渲染按 `analysis_mode` 区分 fact-only 与 advisory；历史缺少该字段的结果按 advisory 处理；fact-only 页面只呈现事实、营养、缺口、风险和未知项；复盘页面对 `protein_target_g = null` 显示“未知”。
- 验证：Python 3.13.14、uv 0.8.22 隔离候选副本中，RenderContract 3 项、photo/material 3 项、AdaptiveDomain fact-only 1 项、`compileall` 和 `git diff --check` 均退出 0。
- 限制：本轮未运行全量 Python、PostgreSQL、Android 或 Web 浏览器验证；`AndroidBoundaryTest` 和 Android 文件保持独立、未暂存。

> 项目于 2026-07-02 从 DietOS 更名为 MealCircuit（食回路）。以下旧名称保留为真实历史记录。

> 本文件只记录开发历史，不是 Agent 的需求输入。当前行为以代码、测试、`AGENTS.md`、`README.md` 和 `docs/agent-workbench.md` 为准。

## 2026-08-09：发布契约、Android 权威边界与 Portable 安全修复

- 目标：完成发布 workflow 语法/语义门禁、单一版本来源、PostgreSQL recovery 初始化、Android 远端复盘权威边界和 Portable 临时路径加固；保留历史设备 AI Key，只有显式确认动作才会清理。
- 改动：`release.yml` 与 `test.yml` 新增独立 contract job，固定 actionlint 1.7.12、`setup-uv 0.8.22`、Action SHA 及容器 digest；`pyproject.toml` 成为版本源，Android 以 SemVer 计算 `versionCode`，Windows/Inno Setup 和发行产物消费 contract output；PostgreSQL 集成测试先验证未配置 recovery 返回 409，再验证幂等 push；Android 删除本地模型客户端和 daily review 路径；Portable 临时目录复用 secure/reparse 校验并增加竞态测试。
- 已验证：actionlint 解析两个 workflow，故意破坏缩进的回归测试失败；版本、发布策略、Android 权威边界、Portable 竞态、SQLite sync server、adaptive 模块、OpenAPI、协议、`release_check`、`dependency_check`、`compileall`、`uv lock --check` 通过；当前环境的 `cryptography` 为 `50.0.0`。
- 未完成的环境验收：本机没有 Java/JAVA_HOME 或 Docker，Android Gradle/instrumentation 和真实 PostgreSQL 不能本地运行；完整 `unittest discover` 在 10 分钟上限内未完成，因此不能标记为全量 Python 测试通过。CI 的 PostgreSQL job 已改为强制配置 URL，并单独运行 `test_sync_postgres`，避免无声跳过。

## 2026-08-09：发布阻塞、安全边界与渲染稳定性修复

- 目标：执行发布前修复方案，消除已知的 `cryptography` 漏洞、OpenAPI 漂移、Web 空值崩溃和 Android 本地 Agent 越过 Python 七阶段工作流的问题；同时收紧 CI action 引用并让 tag 版本参与发布产物命名。
- 改动：将 `cryptography` 升级到 `50.0.0`，刷新 `uv.lock` 和桌面依赖锁；重新生成冻结的 `protocol/sync-v1.openapi.json`；Web 事实型任务允许建议字段缺失，复盘页面对未知蛋白目标显示“未知”；Android 移除本地任务生成入口，继续只保存事实、校正和已发布计划；新增版本一致性检查、tag 版本注入 Android/release workflow、全部 workflow action 的 immutable SHA 检查；新增 Web、Android 边界和版本/发布门禁测试。
- 验证：`uv run --no-sync python -m unittest discover -s tests -v` 为 301 项通过、1 项按设计跳过（未配置 PostgreSQL 集成地址）；`uv run pip-audit` 对同步服务和桌面解析依赖均报告无已知漏洞；`tools/version.py --check`、`tools/dependency_check.py`、`tools/release_check.py`、OpenAPI check、协议校验、`compileall` 和 `git diff --check` 通过。
- 剩余风险：本机没有 `JAVA_HOME` 或 Java，未能运行 Android Gradle、lint 和 instrumentation；PostgreSQL 集成仍需在 CI 或配置数据库的环境验证。历史 `v0.3.0` 文档保留不变，下一个 tag 必须先更新 `pyproject.toml` 和对应发行说明，使版本门禁通过。

## 2026-08-04：Android 首帧初始化与视觉验收修复

- 目标：执行 Android 模拟器验收计划，消除首帧前同步初始化、首屏输入提示、签到选项横向裁切和冲突页技术化操作文案等已确认阻断项；不改变领域数据格式、同步合并规则或 API Key 边界。
- 改动：`MealCircuitApplication` 将 metadata 初始化和孤儿照片清理移到应用级 IO 协程；`DomainRepository` 增加初始化门禁，写入/同步等待门禁完成，并将 Room 只读 Flow 的上游查询放到 IO；`MainViewModel` 延后 Keystore 状态读取和惰性创建 AI、Portable、问卷及同步对象。Today 页补充未聚焦时可见的“写下吃了什么、执行阻力或真实变化”提示，并在首屏后逐步组合状态模块；签到选项改为 `FlowRow`。冲突页改用“本机版本/远端版本”文案，跨实体类型冲突禁用错误的“保留远端”入口；食品库移除 sibling 技术术语。新增 repository 初始化门禁 instrumentation 回归测试。
- 验证：`git diff --check` 通过。使用隔离 SDK `C:\tmp\mc-android-019fb725\sdk`、`-Dkotlin.compiler.execution.strategy=in-process`，并临时排除本机缺失的 debug-only `ui-test-manifest` 后，最终源码的 `:app:assembleDebug` 成功；临时构建条件已恢复且不在工作树差异中。新 APK 为 `org.mealcircuit.app` `0.3.0`，SHA-256 为 `EDAAFE2084C119074B71540E187077A4169B6CDFAABAD8FEA517942E1B158B8A`。隔离 API 35 AVD 使用 ADB 端口 `5038` 连续冷启动 5 次为 `2552ms、2519ms、2602ms、2693ms、2589ms`，均低于 3 秒；首屏提示截图为 `C:\tmp\mc-android-019fb725\visual-acceptance\61-today-final.png`，签到换行截图为 `62-checkin-flow.png`，无 `FATAL EXCEPTION` 或应用 ANR。
- 后续复验：Google Maven 使用临时环境变量 `MEALCIRCUIT_LOCAL_MAVEN=https://maven.aliyun.com/repository/google`，Central 使用同样不入库的临时镜像入口；未修改仓库依赖配置。`testDebugUnitTest`、真实配置 `assembleDebug`、`lintDebug` 和 `compileDebugAndroidTestKotlin` 均通过。普通 connected instrumentation 为 27 项完成、1 项按设计跳过；补齐真实 `syncServerUrl/login/password/recovery` 后跨客户端 connected instrumentation 的 26 项全部通过，Python `cross_client_sync_test verify` 也通过并完成服务端数据库、blob、日志明文审计。模拟器仍报告首次 Compose 绘制约 77--93 个 skipped frames，虽不影响 3 秒首屏门槛，仍需在硬件加速设备上复查。跨端测试结束后临时 server、state 和镜像配置均已清理；本轮误用保留变量名造成的用户目录 synthetic 数据已从迁移前备份恢复，数据库再次确认无 synthetic 标记。

## 2026-07-29：Cloudflare Pages 双语产品介绍站

- 目标：让 `main` 分支可以直接连接 Cloudflare Pages，托管一份不暴露本地应用或私人数据的中英文 MealCircuit 对外介绍站；参考 `verisilo.qiu.works` 的信息组织和其公开仓库部署方式，但不把 Astro/pnpm 引入当前 Python/Android 工程。
- 改动：新增无构建依赖的 `site/` 静态站，包含英文首页、`/zh/` 中文首页、显式 404、响应式样式、SVG 品牌图标、manifest、robots 和 Cloudflare `_headers`；根目录新增 `wrangler.jsonc`，固定项目名 `meal-circuit`、输出目录 `site`、兼容日期和关闭 Wrangler 指标上报。README 补充产品站入口和目录说明，`docs/site-deployment.md` 记录 `main` Git 集成、构建参数、预览、自定义域名与部署后检查；新增标准库站点契约测试，未增加 Python、Node 或运行时依赖。
- 页面内容：围绕“记录事实—理解个体—比较策略—执行反馈”的长期回路介绍今天、计划、我的三个入口，本地 SQLite、可选自托管 E2EE 同步、主动配置模型等数据边界，以及不诊断、不编造未知、不提供官方托管云、不静默发布计划的真实限制；下载和源码链接指向当前公开 `v0.3.0` 与仓库。
- 验证：`uv run --no-sync python -m unittest tests.test_product_site tests.test_release_workflow -v` 共 12 项通过；`uv run --no-sync python tools/dependency_check.py` 通过；`uv run --no-sync python tools/release_check.py` 零发现；`git diff --check` 通过。Wrangler 4.115.0 从 `site/` 启动等价 Pages 预览，解析 `_headers` 规则，`/` 返回 200，未知路径返回自定义 404，CSP、Permissions Policy、Referrer Policy、nosniff 和防嵌入响应头均实际返回。Chromium 在 320、768、1440px 检查中英文页面，均为单一 h1、无缺图、无控制台/请求错误、外链带 `noreferrer` 且无页面级横向溢出；另完成 1440px 英文和 390px 中文长页截图视觉检查。预览生成的 `.wrangler` 本地缓存已清理，发布门禁复跑通过。
- 剩余风险：本轮没有写入 Cloudflare 账户、创建 Pages 项目或配置 DNS；PR 分支本身不会触发 `main` 的生产部署。仓库未猜测最终自定义域名，因此暂不声明 canonical、`og:url` 或 sitemap；选定并激活真实域名后应一次补齐这些绝对 URL。

## 2026-07-18：Android 正式发行资产平铺修复

- 问题：`v0.3.0` 的 tag workflow（`29646367792`）已成功生成并严格验签 `app-release.apk` 与 `app-release.aab`，但 Android artifact 保留了 `apk/release/`、`bundle/release/` 子目录；Release job 的 `release-assets/*` 与顶层校验和扫描因此没有发布这两项。该 tag 的公开 Release 已从同一成功工作流下载签名产物，补齐 APK/AAB，并用包含全部 9 项公开文件的新 `SHA256SUMS.txt` 覆盖旧清单；回读后逐项 `sha256sum -c` 通过，APK 的 `apksigner verify --verbose` 也通过。
- 改动：Android job 现在在上传前将已构建的 APK/AAB 复制到 `$RUNNER_TEMP/android-release` 的顶层；artifact 下载合并后，现有的顶层 checksum 与 Release glob 会自然包含这两个文件。`dependency_check` 和 Release 策略测试新增此平铺契约，防止以后静默回归。
- 验证：`uv run --no-sync python -m unittest tests.test_release_workflow -v` 的 9 项通过；`uv run --no-sync python tools/release_check.py`、`uv run --no-sync python tools/dependency_check.py` 与 `git diff --check` 均通过。
- 剩余风险：该工作流修复尚待独立受保护分支 PR 合入，因而影响下一个 tag；已发布的 `v0.3.0` 资产不依赖该未来合入，且其公开清单已回读核验。

## 2026-07-18：v0.3.0 发布说明与下载指引

- 目标：在正式 `v0.3.0` tag 前，将 README、Android 签名说明和 GitHub Release 正文同步为用户可执行的跨平台安装、校验和信任边界说明，而不把“可运行”误写成商业签名或公证。
- 改动：README 新增中英文 v0.3.0 下载区，区分 Windows 安装器/便携包、Linux AppImage、可直接安装的 Android APK、仅供分发渠道上传的 AAB，以及 macOS universal DMG；同时给出 `SHA256SUMS.txt` 校验入口。`docs/releases/v0.3.0.md` 现在直接作为 Release 正文，列出资产用途、SBOM、升级恢复和各平台签名状态。Android README 明确 release keystore 无论文件扩展名均以 PKCS12 打开，和 CI 的实际签名契约一致。
- 验证：`uv run --no-sync python -m unittest tests.test_release_workflow -v` 的 8 项通过；`uv run --no-sync python tools/release_check.py`、`uv run --no-sync python tools/dependency_check.py` 和 `git diff --check` 均通过。
- 剩余风险：文档分支尚未合入受保护 main，未创建 tag 或 GitHub Release；最终发布仍须等待该 PR 的 CI 通过，并在合入后的 main 提交上触发 `v0.3.0` workflow。Windows/macOS/Linux 的分发信任限制已经在面向用户的说明中如实标明。

## 2026-07-18：Windows EXE 发布与 Android 资产安全降级

- 目标：在没有 Android 正式签名密钥的当前仓库条件下，完成可运行 Windows EXE 与 Linux 产物的正式 tag/Release；保留 Android 的构建验证，但绝不把未签名 APK/AAB 放入公开发布。
- 改动：发行工作流仍在原生 Windows runner 中生成、冒烟运行 Windows `MealCircuit.exe`，并产出便携 ZIP 与安装器 EXE。tag 上若 Android 四项签名 secret 不完整，Android job 会给出明确 warning、继续执行无签名构建检查并跳过 Android artifact 上传；完整密钥存在时才上传并验证签名 APK/AAB。AAB 的严格验证把恢复出的 release keystore、其密码和 alias 作为信任源，因此仍强制加密签名正确且签名者为预期 release key，同时允许 Android 标准的自签名证书。发布 job 继续等待全部平台 job，Windows/Linux（及可用的 macOS）资产会照常生成 SBOM 和 `SHA256SUMS.txt`。同步更新发布说明、跨端验收说明、静态策略检查和回归测试。
- 验证：`uv run --no-sync python -m unittest tests.test_release_workflow -v` 的 8 项策略测试通过；`uv run --no-sync python tools/dependency_check.py` 与 `uv run --no-sync python tools/release_check.py` 均通过；`git diff --check` 通过。使用临时自签名 release JKS、其密码和 alias 执行 AAB 的 `jarsigner -verify -strict` 已通过。Draft PR #28 的 GitHub Actions `release-builds`（`29643012187`）已在原生 Windows、Linux、Android 和 macOS runner 全部成功，tag 专属 `release` job 按预期跳过；常规 `test`（`29643012203`）的 Android、Android instrumentation、Python 3.11/3.13、PostgreSQL sync 和 supply-chain 也全部成功。下载 `desktop-windows` artifact 后确认便携 ZIP 内含 `MealCircuit.exe`，其为 x86-64 Windows GUI PE 文件；安装器 EXE 也为 Windows GUI PE 文件。
- 剩余风险：没有 Authenticode 证书时 Windows EXE/安装器仍可运行，但会显示 `Unknown Publisher`；Android 正式签名产物仍需仓库所有者之后配置四项 Android signing secrets。

## 2026-07-18：Android 对齐 Windows 已发布计划与跨端重审

- 目标：以 Windows 的七阶段工作流为唯一计划发布端，把 Android 从旧的“总览/记录/更多”和粗略复盘展示收束为可执行的同步客户端；手机可以记录真实经过、填写状态、查看已发布的计划依据、份量和调整条件，但不能在本地生成或发布竞争性的每日复盘。
- 改动：Android 主导航改为“今天、计划、我的”。今天页可新增和修改当天文字记录、填写状态，并展示 Windows 已正式发布的当天方向和计划；计划页完整呈现问题、策略与取舍、营养估算、三餐方式、食物、份量合同、外食原则、饥饿/低食欲/肠胃调整和执行风险；“我的”补齐档案、目标、已确认理解、照片/食材与食品库入口。新增已发布计划投影及契约测试。Android 新增记录、编辑记录或发布状态只同步原始事实，不再创建本地 `daily_review`。Python 同步端收到 Android 的新/变更记录或状态后，会在来源未被当前已完成复盘采用时重开 Windows 审查队列、保留已发布历史，并处理新文字的意图学习；首次同步一份已含来源的完成复盘不会误重开。
- 验证：GitHub Actions `29638703786`（候选源码提交 `10073fe`）的 Android、Android instrumentation、Python 3.11/3.13、PostgreSQL sync 与 supply-chain 全部成功。`uv run --no-sync python -m unittest discover -s tests -v` 为 178 项通过、1 项按设计跳过（未配置 PostgreSQL 集成地址），覆盖新增的跨端复盘回归。Android `./gradlew --no-daemon testDebugUnitTest assembleDebug lintDebug compileDebugAndroidTestKotlin` 通过；Debug APK SHA-256 为 `edf3324ed8176727b7230ad78e42edbdf27b172e317086490795b0fc6eaf2ef3`，`apksigner` 确认 v2 签名，包为 `org.mealcircuit.app` 0.3.0、`minSdk 26`、`targetSdk 36`、入口为 `MainActivity`。`./gradlew --no-daemon lintRelease assembleRelease bundleRelease` 也通过，生成 release APK/AAB；无密钥时 APK 按预期为 `app-release-unsigned.apk`，不能替代正式签名产物。随后使用仅存于临时目录的自签名 JKS 复跑 `assembleRelease bundleRelease`：`app-release.apk` 已由 `apksigner` 验证为 v2 签名，AAB 的 `jarsigner -verify -strict` 也完成验证；自签名证书产生的信任、无时间戳和短有效期提示符合这次非发布性验证的预期。
- 剩余风险：未在实体 Android 设备上做视觉/交互验收；正式 tag 与 GitHub Release 仍严格等待用户提供 Android 签名密钥。本次文档提交会再次触发远端 CI，必须在其完成后才可维持候选版本的全绿结论。

## 2026-07-18：发布前 CI 回归修复与 Linux 产物核验

- 问题：主分支最新提交的 GitHub Actions `test` 工作流失败。两个 Python 版本仍用单步每日生成假设验证已经切换到强制分阶段 Agent 流程的路径；Web 断言还保留旧的 `BEGINNER` 餐次标签。Windows Web 测试在拥挤 runner 上的 5 秒本地 HTTP 超时会让测试清理先于请求结束，从而引发后续 SQLite 临时库错误。首次远端复跑还暴露了 Agent 工作目录测试将未规范化的 Windows 临时路径和 `app_home().resolve()` 的规范化路径直接比较，遇到 junction 或 8.3 路径时误报越界。
- 改动：测试改为分别验证 Anthropic 当前单步请求协议，并用确定性的分阶段 provider 覆盖真实的语义拒绝后草案重试；同步当前餐次文案断言，并将仅测试用的本地 HTTP 等待时间调至 15 秒，避免竞态而不改变运行时服务超时。Agent 私人工作目录断言现在两端都使用规范化路径，仍验证真实的目录边界。未改变产品功能、领域规则、Android 源码或发布版本号。
- 验证：`uv run --no-sync python -m unittest discover -s tests -v` 结果为 176 项通过、1 项按设计跳过（未提供 PostgreSQL 集成地址）；`pyinstaller --noconfirm --clean packaging/mealcircuit.spec` 成功生成 `dist/MealCircuit/MealCircuit`，其 `--smoke-test` 通过。随后用 release 工作流同款 AppImage 工具生成 `dist/MealCircuit-0.3.0-linux-x86_64.AppImage`，以 `--appimage-extract-and-run --smoke-test` 实际启动成功，SHA-256 为 `112bc9637c39af219c635bf945508a6a1d3d2399d2f40356b14e18a6d4174153`。本地安装临时 JDK 17 与 Android 36 SDK 后，`./gradlew --no-daemon testDebugUnitTest assembleDebug lintDebug compileDebugAndroidTestKotlin` 通过；`app-debug.apk` 用 `apksigner verify --verbose` 确认为 v2 签名、可启动包 `org.mealcircuit.app`（`minSdk 26`、`targetSdk 36`），但其证书为 Android Debug，不能替代正式发布签名。
- 剩余风险：Android 客户端相对 Windows 核心端的功能落后，不能仅因 Debug APK 可构建就把它标为当前正式客户端；用户已决定先以 Windows 为基线补齐 Android，完成前不创建正式 tag/release。远端 CI 尚待推送最新 Windows 兼容性修复后复跑。

## 2026-07-17：Windows 桌面端首启补齐私人配置

- 问题：打包后的 Windows 桌面程序走 `mealcircuit.desktop`，此前只创建数据库；首次使用的私人模板与 Web 启动入口不一致，页面虽可打开，但后续配置或 Agent 功能可能缺少 `profile.md`、`settings.json` 等必需文件。
- 改动：桌面入口现在先调用既有的私人目录初始化，再创建或迁移数据库；不改变端口、WebView 优先/浏览器回退、打包格式或发布流程。新增子进程冒烟测试，在全新私人目录实际启动桌面入口并校验模板被创建。
- 验证：`uv run --no-sync python -m unittest tests.test_mealcircuit.DesktopEntryPointTest -v` 通过；`uv run --no-sync python -m py_compile mealcircuit/desktop.py tests/test_mealcircuit.py` 与 `git diff --check` 通过。Linux 无法验证原生 Windows WebView 或浏览器启动，需在 Windows 设备最终确认。

## 2026-07-16：从词面过关改为语义个案编译

- 问题：完整 Agent 流程已经能强制分阶段，但外部模型仍需照抄目标长句、个案问题和数据库来源 ID；“不安排牛肉”会因出现“牛肉”二字被误判，轻微蛋白估算差异会迫使模型机械加量，回执或版本元数据变化还可能让整轮规划失效。手动阶段 JSON 也容易落在仓库根目录。
- 改动：策略、计划和独立审查改用短引用表达“怎样处理”，系统再确定性编译真实目标、问题和证据来源；食材限制只检查餐食、菜谱、替换与采购等实际安排字段。上下文同时保存完整审计哈希与只包含决策含义的哈希，只有真实事实、目标、有效理解或约束变化才使运行过期。份量门按目标区间与估算置信度判断合理重叠，并把食欲、预算和执行摩擦交给独立审查，不再要求每个估算下界精确撞线。
- 闭环：用户明确要求长期改变的执行归因会成为下一轮策略、计划和审查必须回应的现实问题；一次“今天起晚了”仍只等待确认，不进入强制长期调整。CLI 自动在私人数据目录为每个阶段准备 context、schema 和 result 文件，省略 `--file` 即提交该私人结果文件，仓库不再是默认草稿目录。
- 定向验证：语义引用、伪证据拒绝、目标遗漏、负向食材表述、真实食材安排、审计元数据稳定性、轻微份量估算差异、一次性时间反馈、跨日执行学习、局部修订和私人 Agent 工作目录均已通过对应 `unittest`；修改模块 `py_compile` 与 `git diff --check` 通过。按当前约束未运行全量测试、浏览器、Android、发布检查或 CI，不能据此宣称生产验收完成。

## 2026-07-15：一次性反馈不再冒充长期理解

- 问题：一次“今天起晚了，所以没时间煮鸡蛋”的早餐反馈被直接激活为长期执行摩擦，今天页又用“我从这次记录里记住了”和抽象系统措辞展示，既没有说明真实来源，也没有说明确认后会怎样改变安排。
- 修复：单次时间不足或步骤过多只进入待确认；只有用户明确说“以后、经常、总是”等长期表达，或出现两个独立真实证据后才会自动生效。旧版本中由单条执行反馈错误激活的同类理解会保留证据与版本历史并自动退回待确认；已经由用户确认的理解不会被回退。
- 交互：今天页按最近一次真实输入选择最具体的理解，直接引用用户原话，并改成“以后需要给早餐准备一个不用开火或更快的备选吗？”；同时解释长期采用会怎样改变后续计划，选项为“以后都准备、只是今天、不用这样调整”。明确的预算等长期表达仍按原规则立即生效并允许纠正。
- 定向验证：8 项通过，覆盖一次性时间反馈、旧错误理解修复、用户确认保留、长期预算理解、两个独立证据、份量反馈、反馈事件历史、结果归因和现有 Web 工作台；修改模块编译与 `git diff --check` 通过。按当前约束未运行真实浏览器、全量测试、Android、发布检查或 CI。

## 2026-07-15：自适应个案 Agent 完整运行链

- 目标：让每日复盘和次日安排不再由一份模型 JSON 直接发布；即使模型能力较弱，也必须逐条处理真实输入、理解深层需求、遵守专业边界、比较现实策略、写出可执行份量并接受独立审查。成功标准是改善用户实际决策与执行，而不是增加可见的工程概念。
- 日常生成：Web 内置模型、CLI 和外部 Agent 统一进入事实整理、意图与学习识别、个案理解、专业边界、策略比较、计划设计和独立审查七段运行。每段只获得所需上下文并产生回执，不能跳步或调换；审查只允许退回计划修订一次，全部通过后仍只是可替换草案，用户接受才发布。`day-context` 降为只读检查，`day-complete` 必须绑定当前完整运行。上下文变化、模型失败或审查失败均保留原正式计划。
- 目标与专业边界：初始化形成带版本的目标契约，保存目标原因、优先级、现实成功指标、不可牺牲项、冲突取舍、三餐方式、记录/追问强度以及营养目标来源和有效期。不同目标加载对应规划维度；营养数值只来自已确认目标。离线专业知识按目标和生命阶段选择，孕期、哺乳期、未成年人及专业协作模式不会混入普通成人目标。
- 学习与真实经过：每条用户文字都必须得到处理。确定性信号识别“以后、不要再、太贵、买不起、太麻烦、没吃饱”等弱模型容易遗漏的表达；明确的低风险长期需求当次成为可回滚理解，高影响信息仍只能进入档案确认。“牛肉太贵，以后算了”会让日常蛋白方案优先长期负担得起的来源，并把牛肉从默认项降级，而不是建立永久禁食。用户修改记录时，旧证据保留追踪但立即停止影响当前理解。多张餐食照片、文字执行说明和更正归并成同一用餐事件，当前事实采用用户纠正，早期视觉观察仍保留。
- 计划与反馈：每份计划记录要解决的问题、所选策略和取舍、饱腹/恢复/成本/时间预测及调整条件；每餐提供目的、克数区间、生熟或上桌口径、生活量具和加减量顺序。每条核心建议必须绑定本次真实事实、生效中的长期理解、已确认目标或适用专业原则，独立审查会逐条核对来源；无关追问不能阻塞规划。结果回执区分价格、库存、时间、复杂度、份量、口味、身体状态和临时事件，不把所有偏离归因于执行力。没有真实采购或剩余食材时，购物与复用清单允许为空；明天用完的食材不再被迫制造后续用途或改变包装规格。
- 产品与多端：普通 Web 仍只显示“今天、计划、我的”。当天新学到的低风险理解以自然语言轻提示出现；技术阶段、Schema、哈希和回执留在高级检查入口。Android 普通界面移除本地一键生成入口，只读取 Python 发布的核心建议、安排依据、份量与加减条件；新增目标契约、用户模型和用餐事件投影通过既有加密偏好实体同步，旧客户端会保留无法理解的内容而不丢弃。
- 定向验证：修改过的 Python 模块编译和协议 JSON 解析通过；`tests.test_agent_workspace` 27 项全部通过，覆盖强制阶段、真实长句中的牛肉预算约束、高价酸奶、反证、建议证据、无关追问、局部修订和并发失效；目标契约、受限模式隔离、空购物/复用、可替换与锁定生命周期、应用迁移、Portable 投影往返和 Web 工作台定向测试通过。`agent-run` 与收紧后的 `day-complete` 帮助入口可用，`git diff --check` 通过。
- 未执行：遵循用户当前约束，没有运行全量 `test.ps1`、真实浏览器、Android Gradle、发布检查或 CI 等待，不能声称生产验收完成。Android 源码兼容性目前只通过静态检查；知识包仍需按 `review_due_on` 定期人工复核。

## 2026-07-14：今日工作台补回个案核心建议

- 目标：每日复盘完成后，用户无需进入历史详情就能在今天页看到最值得保持或调整的方向；建议必须来自当日真实执行和个人状态，而不是通用饮食口号。
- 核心改动：今天页在当前计划之后显示“今天的核心建议”，草案准备好时即可看到一句当日判断和最多三条可执行方向，接受后再提供详细复盘入口；尚未生成时不显示空卡片。每日生成与三阶段规划提示同时明确比较计划和实际，并综合个人目标、训练、饥饿、睡眠、肠胃、日程和执行反馈；合理临时调整不被自动判为失败，整体执行良好时具体肯定并说明继续保持什么。
- 验证：运行 Agent 草案接受与每日 Anthropic 生成两项定向测试，覆盖核心建议在今天页出现、详细复盘入口、生成提示的个案因素和结构化输出说明；运行 `git diff --check`。按用户当前要求未运行浏览器校验、全量测试、Android 构建或 CI 等待。
- 用户用法：完成当天复盘或接受明日草案后回到“今天”，即可直接看到当天最重要的调整方向；需要了解依据时点击“看看我是怎么判断的”。

## 2026-07-14：文字记录改为原位保存和直接修改

- 目标：今天页不再把已保存内容复制到输入框上方，也不要求用户先展开“修改”操作；同一块输入区始终承载当前内容。
- 核心改动：首次保存后，原文保留在原输入框中并以较弱的颜色显示；输入框仍可直接聚焦和编辑，保存继续沿用同一记录 ID 与既有可追溯 revision。移除“今天已记下”“已记下”和“修改这条内容”等重复界面。
- 验证：运行 `test_today_intake_stays_visible_and_can_be_revised`，覆盖保存后原位回显、直接编辑、同一记录 ID、领域 revision 和 Agent 输入同步；运行 `git diff --check`。按用户要求未运行浏览器校验、全量测试、Android 构建或 CI 等待。
- 用户用法：在“记一笔”中写下情况并保存；内容会留在原处，之后直接点进文字修改并再次“记下来”即可。

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

## 2026-07-16：系统主题与中英文界面偏好

- 目标：让桌面 Web 界面的亮暗主题默认匹配操作系统，并在“我的”设置中提供英文/简体中文选择，首次使用默认英文。
- 改动文件：`mealcircuit/static/theme-init.js`、`mealcircuit/static/app.js`、`mealcircuit/static/app.css`、`mealcircuit/server.py`、`tests/test_mealcircuit.py`、`DEVELOPMENT.md`。
- 核心功能：主题偏好升级为“跟随系统 / 浅色 / 深色”三态；跟随系统时监听 `prefers-color-scheme` 的实时变化；保留旧版已保存的亮暗选择。新增当前设备级语言偏好，默认 `en`，可在“我的 → 外观与语言”切换 `English` 或 `简体中文`；共享导航、设备状态、日期、操作按钮和该设置页随语言即时更新。
- 验证：`python3 -m py_compile mealcircuit/server.py`、`git diff --check` 与 `WebAppTest.test_pages_and_material_form` 通过；该测试新增主题三态、默认英文和语言设置入口断言。运行整个 `WebAppTest` 时，18 项通过，2 项既有菜单夹具断言因共享测试状态失败（缺少 `BEGINNER LUNCH` 等预期文案），与本轮外观/语言改动无关；全仓库 `unittest` 发现还会因未安装同步服务可选依赖而报告 `sync_server` 导入错误。
- 剩余风险：当前英文覆盖共享应用外壳和“我的”设置入口；计划、记录、初始化等领域页面仍保留原始内容语言，后续应逐页迁移为相同的稳定翻译键，用户或模型生成的饮食内容则应保持原语言，避免误译事实记录。

## 2026-07-16：每日 Agent 签到模块列表兼容

- 目标：修复 `plan_design` 阶段份量校验把真实签到模块列表误当成字典、对列表调用 `.get()` 而中断合法计划提交的问题。
- 改动文件：`mealcircuit/agent_workspace.py`、`tests/test_agent_workspace.py`、`DEVELOPMENT.md`。
- 核心功能：`_validate_portions()` 现在同时接受当前由每日上下文和领域 Schema 使用的模块列表，以及旧版以模块名为键的字典；列表中只读取 `module_key == "hunger"` 的字典项。缺少 hunger、空模块、非字典列表元素和未知容器均按空签到处理；`answers_json`、`answers` 和直接答案字典的读取顺序保持不变，其他份量规则未改动。
- 验证：新增列表、旧字典三种答案载荷、无 hunger、非字典元素、未知容器和空模块的定向回归测试；`python3 -m unittest tests.test_agent_workspace` 共 35 项通过；`python3 -m py_compile mealcircuit/agent_workspace.py tests/test_agent_workspace.py` 与 `git diff --check` 通过。
- 剩余风险：未通过真实外部模型重新执行完整每日 Agent 七阶段流程；本轮验证覆盖上下文形态、`plan_design` 所调用的份量校验和现有 Agent 工作区回归，且未改变计划生成或份量业务规则。

## 2026-07-31：Windows 安全加固与可双击验收

- 目标与范围：只验收当前 Windows 11 x64 桌面端，使源码在用户人工测试前达到可双击运行状态；未下载、配置或调用 Android SDK、Gradle、模拟器、macOS/Linux 构建链。工作分支为 `codex/windows-acceptance`，所有本轮可控构建、依赖、测试数据和缓存均隔离在 `C:\tmp\mc-win-019fb725`。
- 依赖：在临时目录准备官方 Python 3.11.9 embeddable、Python 3.13 虚拟环境、uv 0.11.16、PyInstaller、Inno Setup 6.7.3 和 actionlint 1.7.12；未改动全局 Python 或系统 PowerShell 执行策略。`pip-audit` 检查桌面运行依赖 20 项，已知漏洞 0 项。
- 安全与可靠性修复：收紧 Windows 私人目录 ACL、reparse/no-follow 与路径归属验证；用私有 DACL named mutex 替代可被替换的相邻锁文件；为数据库迁移、便携恢复和同步更新增加锁、事务归属、原子替换、大小/深度限制及失败回滚；限制 AI 读取到受管文件并保持 API Key 仅存在进程内；加固 loopback Host/Origin/CSRF、CSP/HTML 转义和只读路由；强制高影响健康与排除项由用户确认并贯穿计划、替代和救场文本；Windows 打包排除 Android/macOS/Linux 后端和 `pywebview-android.jar`。
- Windows 运行时修复：桌面健康探针改用直连 `127.0.0.1`，不再受机器 `HTTP_PROXY`/`HTTPS_PROXY` 影响；窗口化 PyInstaller 进程没有 `sys.stderr` 时安全丢弃访问日志，并把 HTTP handler 异常写入本地启动日志，修复打包 EXE 启动后被代理返回 502 或断开连接的问题。
- Codex Security 数据边界：本轮曾完成一次基线扫描，当前可读取副本位于 `C:\tmp\mc-win-019fb725\security`，包含 48 文件范围清单、威胁模型、覆盖账本、19 条结构化发现（5 high、10 medium、4 low）和扫描清单；这些发现均已逐项本地复现、修复或按边界降级。插件在 `%TEMP%\codex-security-scans-lAod8P` 下保留两个封存目录，但当前进程无权读取其 ACL/内容，`report.md` 与 SARIF 没有可访问副本；另一次安全过滤拒绝只留下状态，没有正文可恢复。按用户要求不再调用、重试或补取 Codex Security；后续普通改动采用本地差异、测试、依赖和打包检查，仅在导入/同步/密钥/AI 文件访问等高风险边界变化时做本地专项审计。
- 自动化验证：完整 Windows 选择器在 Python 3.13.5 上 255 项通过、15 项非 Windows/外部模块跳过，在 Python 3.11.9 上 255 项通过、17 项跳过；ACL、named mutex、原子恢复与便携回滚专项在两版本各 32/32 通过，真实 `icacls` 证据确认目标目录不继承 `Everyone` 权限；`compileall`、actionlint 与 `git diff --check` 通过。完整 255 项矩阵先于最后两项桌面探针/无 stderr 修复，修复后两版本 `tests.test_windows_desktop` 各 8/8 通过，并重新完成全部打包、安装和真实运行验收。
- 打包与运行验收：`C:\tmp\mc-win-019fb725\security\windows-final-acceptance.ps1 -Execute` 通过 PyInstaller AMD64 GUI 构建、包内容排除检查、EXE smoke、便携 ZIP 解压 smoke、Inno 编译、静默安装/启动/卸载、无参数原生窗口和同一数据目录重复实例退出码 2；验收后没有残留 MealCircuit 进程或卸载注册表项。最终产物为 `dist\MealCircuit\MealCircuit.exe`（SHA-256 `517c6968e0f299a1d66de01d701809063f3a08f98120cad24d152d8e7ff9ed7a`）、`dist\MealCircuit-0.3.0-windows-x64-portable.zip`（`3143fca0ecb826059117b45838059d87d8710e862ae5cd5d6fb18f6c4ae6d0d4`）和 `dist\MealCircuit-0.3.0-windows-x64-setup.exe`（`ad819aafcd34a70388ef5180e21be525793349d9f127227660d4e129b0a2f95f`）。
- 缓存与清理：本轮临时根共约 727 MiB；主要目录为 `cache`（约 314 MiB，含 pip/uv/pycache/PyInstaller）、`bootstrap`（约 111 MiB）、`dist`（约 75 MiB）、`venv`（约 47 MiB）、`downloads`（约 23 MiB）、`python`（约 21 MiB）、`build`（约 19 MiB）、`runtime-home`（约 18 MiB）、`security`（约 7 MiB）和 `logs`（约 1.3 MiB）。人工测试结束后可整体删除 `C:\tmp\mc-win-019fb725`；删除前应先复制需要保留的安装包或日志。
- 剩余风险：最终 EXE 和安装包未做 Authenticode 签名，Windows 可能显示“未知发布者”；同账户进程可预占确定性 named mutex 造成 fail-closed 拒绝服务，但不能造成锁分裂、越界写入或误删未知数据；同账户管理员级句柄竞态不在本地应用可完全消除的边界内。上述均不阻断本机人工测试。
- 用户用法：直接双击 `C:\tmp\mc-win-019fb725\dist\MealCircuit\MealCircuit.exe` 进行免安装测试，或双击 `C:\tmp\mc-win-019fb725\dist\MealCircuit-0.3.0-windows-x64-setup.exe` 测试安装流程；当前受限 PowerShell 策略不影响 EXE 启动。

## 2026-08-01：桌面中文一致性与跨入口品牌图标统一

- 目标：修复桌面应用仅翻译导航外壳、正文仍为中文而造成的中英文混杂，并让 Cloudflare Pages、桌面 Web、Windows EXE 与安装包使用同一 MealCircuit 品牌图标。
- 改动：桌面应用固定使用内容完整的简体中文，忽略旧的英文设备偏好并移除未完成的语言切换入口；主题切换继续保留。`site/favicon.svg` 作为品牌图形来源，`mealcircuit/static/favicon.svg` 与其保持一致；由该 SVG 生成多尺寸透明 `packaging/windows/MealCircuit.ico`，分别接入 PyInstaller `EXE` 和 Inno Setup。Cloudflare Pages 的英文与 `/zh/` 双语产品站保持不变。
- 验证：Python 3.13.5 与 3.11.9 各运行 `tests.test_windows_desktop` 加 `WebAppTest.test_pages_and_material_form`，均为 10/10 通过；真实本地页面确认 `lang=zh-CN`，导航为“今天 / 计划 / 我的”，日期、存储提示和操作文案均为中文，未发现英文外壳文案，浏览器控制台无 warning/error。重新执行 `windows-final-acceptance.ps1 -Execute`，PyInstaller AMD64 GUI、EXE smoke、便携 ZIP、Inno 编译、安装/启动/重复实例/卸载全部通过；从最终 EXE 与 Setup 实际提取的图标均为 Pages 使用的深绿回路图标。
- 临时文件：本轮页面验收数据与日志位于 `C:\tmp\meal-circuit-ui-20260801`；SVG 渲染和图标比对文件位于 `C:\tmp\mealcircuit-brand-1024.png`、`C:\tmp\mealcircuit-exe-icon.png` 与 `C:\tmp\mealcircuit-setup-icon.png`，均可在验收结束后删除。最终构建产物仍位于 `C:\tmp\mc-win-019fb725\dist`。
- 剩余边界：桌面英文界面暂不提供，只有在全部领域页面完成同等质量翻译后才应重新开放语言选择；最终 Windows 产物仍未做 Authenticode 签名，公开下载时可能显示“未知发布者”。

## 2026-08-01：Windows CI 安装器下载修复

- 根因与修复：`release-builds / windows` 中 EXE 构建、打包与便携版冒烟测试均已通过，随后因 `files.jrsoftware.org` 上固定的 Inno Setup 6.7.3 地址返回 404 而失败。工作流改用 Inno Setup 官方下载页当前指向的 `jrsoftware/issrc` GitHub Release 固定资产，并同步更新供应链策略检查中的受信地址；版本、SHA-256 校验值和 Authenticode 校验保持不变。
- 验证：从新地址实际下载 `innosetup-6.7.3.exe` 到 `C:\tmp\innosetup-6.7.3-ci-fix.exe`，文件大小 10,592,232 字节，SHA-256 为 `9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732`，Authenticode 状态为 `Valid`、签名者为 Pyrsys B.V.；另运行工作流语法检查与 `git diff --check`。
- 临时文件：上述下载验证文件可在本轮验收结束后直接删除；没有新增项目依赖、全局工具或项目内缓存。
- 剩余风险：CI 仍依赖 GitHub Release 网络可用性，但固定发布资产与哈希、签名三重约束避免了静默版本漂移；Node 20 弃用提示来自已锁定的第三方 Actions，当前只是警告且不构成本次 Windows 失败原因。

## 2026-08-01：Windows CI 图标换行兼容修复

- 根因与修复：Draft PR #33 的本地、远端分支和 PR head 均已同步到 `a19ccfe`；`release-builds` 全部成功，但 `test` 的 Windows Python 3.11/3.13 同时失败。相同提交在 Windows 本机复现后确认唯一断言失败来自 SVG 换行：Git 检出为 CRLF，`Path.read_text()` 会统一换行为 LF，而 HTTP 静态响应保留原始字节。测试改为直接比较两个 SVG 的原始字节，既验证图标完全一致，也不依赖平台换行规则。
- 验证：原失败用例 `WebAppTest.test_pages_and_material_form` 在隔离的 Python 3.11.9 与 3.13.5 上均通过；`tools/release_check.py`、`compileall` 和 `git diff --check` 通过。修复后的完整 277 项本机矩阵未再出现图标失败，唯一错误是受限执行令牌没有 Windows Credential Manager 登录会话导致 `CredWrite` 返回 WinError 1312；该测试与本次两行断言修复无关，GitHub Windows runner 将作为最终矩阵依据。
- 临时文件与剩余风险：独立工作树位于 `C:\tmp\mc-windows-pr-fix`，测试解释器和缓存位于 `C:\tmp\mc-win-019fb725`；没有新增依赖或全局配置。推送后仍需等待 GitHub Actions 的 Python 3.11/3.13 作业均成功，才能把 PR #33 标记为全绿。

## 2026-08-01：Windows 8.3 路径兼容修复

- 根因与修复：GitHub Windows runner 的临时目录以 `RUNNER~1` 短路径提供，而 `Path.resolve()` 会返回 `runneradmin` 长路径。照片任务上下文先把已验证的相对受管路径转换成长路径绝对地址，AI 提交前的第二次受管目录校验再与短路径形式的数据目录做词法比较，因此误判为越界；现在验证后重新保存为受管相对路径，不放宽 UNC、重解析点或目录逃逸边界。三个路径断言改为按 `app_home()` / `db_path()` 的词法绝对路径契约比较，避免把同一目录的 8.3 与长路径别名误判为功能错误。
- 验证：在 `C:\tmp\mc-win-019fb725` 的隔离 Python 3.11.9 与 3.13.5 上运行四个原失败用例，并补充断言确认照片生成上下文始终保留 `uploads/...` 相对路径；另运行相关受管媒体和原子恢复回归、`compileall`、`tools/release_check.py` 与 `git diff --check`。所有测试缓存通过 `PYTHONDONTWRITEBYTECODE=1` 禁止写入仓库。
- 临时文件与剩余风险：未新增依赖或全局配置；测试日志与解释器仍集中在 `C:\tmp\mc-win-019fb725`。本轮不修改 `app_home()` 的词法路径实现，以免重新引入跟随 junction/reparse point 的安全风险；提交推送后仍需由 GitHub runner 的真实 `RUNNER~1` 环境完成最终确认。

## 2026-08-01：Windows 隔离环境中的 Android 模拟器验收与安全加固

- 目标：在不污染正式仓库和用户既有 Android 配置的前提下，于 Windows 上建立可一次性删除的完整 Android SDK、AVD、Gradle、Python 同步服务与报告环境；先于人工验收修复可复现的阻断、数据损坏风险和定向安全问题，并交付可直接安装运行的调试 APK。
- 改动范围：修正 Android 的相机权限、表单状态、营养数字校验、时区容错、主线程 I/O、同步账户切换、冲突收敛、未知记录上限、资源元数据与路径、响应体大小、重试分类、Portable Data、二维码配对和密钥轮换恢复；增加 Room/同步/协议/恶意输入回归测试。AGP 的内部 UTP 宿主配置约束 Netty `4.1.136.Final` 与 Protobuf `3.25.5`，这些依赖不进入 APK 运行时。
- 数据安全：同步账户切换或解除绑定只清理账户作用域状态，不删除领域记录或本地资源；冲突不能被后续写入绕过；资源下载使用受管规范路径、认证元数据、大小上限和原子替换；同步、AI 与导入响应均有流式上限；二维码配对要求用户手动输入的可信服务地址与二维码规范化地址完全一致后才发送密码；密钥轮换支持服务端已提交而本地尚未激活时的进程崩溃恢复。异常提示统一为中文。
- 安全检查：按用户要求未调用 Codex Security。使用本地代码审计、恶意输入测试、仓库 `tools/dependency_check.py` 和官方 OSV Scanner `2.3.8` 的离线 Maven 数据库。初次报告的 14 个受影响坐标仅位于 AGP/UTP 宿主工具配置，未进入 `releaseRuntimeClasspath` 或 APK DEX；约束更新后扫描 329 个包实例，0 个漏洞包、0 条漏洞记录。在线 OSV API 曾因 TLS EOF 未返回结果，因此未把该失败当作“在线零漏洞”证据。
- 自动验证：在临时源码副本中离线执行 `testDebugUnitTest assembleDebug lintDebug compileDebugAndroidTestKotlin connectedDebugAndroidTest`，构建成功；JVM 测试 16/16、真实模拟器仪器测试 14/14 通过。Lint 为 0 error、14 warning，其中 13 条是 `UseKtx` 风格建议，1 条 `ApplySharedPref` 对应故意使用同步 `commit()` 的密钥/身份故障关闭写入。AGP 还报告其 SDK XML 解析器最高理解 v3、已安装 SDK 元数据为 v4；未影响编译、安装或测试。
- 真实运行：API 35 Google APIs x86_64 模拟器通过 WHPX 启动；最终 APK 完成冷启动、导航、相机授权与拒绝后存活、饮食记录保存后强停重启仍存在、食品库 `NaN` 输入即时拒绝且不能保存。隔离 Python 同步服务实测 Python 写入 → Android 拉取 → Android 写入 → Python 恢复，并检查数据库、资源、日志和备份中没有账户密码、恢复密钥、API Key 或明文测试标记。
- 隔离位置：本轮所有 JDK、SDK、系统镜像、AVD、APK、构建输出、Gradle 缓存、OSV 数据库、Python venv、同步服务状态、日志、报告和截图都位于 `C:\tmp\mc-android-019fb725`。最终 APK 为 `final-validation-src-02\android\app\build\outputs\apk\debug\app-debug.apk`，大小 20,884,784 字节，SHA-256 为 `8B2CCEC88D12916A0B5B7F7BB04A4AAB744D928D1BD6C16895D2980A17E50604`；最终 OSV 报告为 `reports\osv-android-gradle-final-offline.json`。
- 隔离复核：任务前后 `%USERPROFILE%\.android` 按“顶层文件名、长度、UTC 修改 ticks、文件 SHA-256”生成的清单摘要均为 `05241f9bb14d8265dba4a2ea19b5801598857177820eee84a7b02a3aa9796826`；正式仓库不存在 Android `.gradle`、`.kotlin`、`app/build`、APK、AAB、DEX、CLASS、lint/test 报告或 `local.properties` 残留。
- 剩余风险：本轮覆盖一台 Windows 主机上的 API 35 x86_64 模拟器，不能等同于所有厂商 ROM、实体相机、低内存设备、弱网和 Play 商店签名分发；真正的外部同步服务证书、账号恢复和多实体设备矩阵仍需在对应生产环境验证。`_internal-unified-test-platform-*` 属于 AGP 内部配置名，升级 AGP 时必须保留依赖检查和 connected test 回归。
- 用户用法：当前模拟器可继续安装并启动上述调试 APK 进行人工点击。验收结束后可要求“清理这次 Windows/Android 验收环境”，届时先停止临时模拟器和同步服务，再核对并删除明确的临时根，不触碰正式仓库或用户既有 `.android`。

## 2026-08-01：Android 验收中止交接检查点

- 状态：应用户要求立即停止继续扩展、修复和验收，并将现有成果提交为本地检查点；这不是 Android 最终验收结论。所有子任务均已中止或结束，本轮不推送分支、不创建或更新 PR，也未调用 Codex Security。
- 当前验证：最新工作树通过 `python -m py_compile mealcircuit\portable.py sync_server\app.py tests\test_portable.py tests\test_sync_server.py`；临时源码副本 `C:\tmp\mc-android-019fb725\handoff-validation-src-10` 使用独立项目缓存 `handoff-project-cache-10` 离线执行 `compileDebugKotlin compileDebugUnitTestKotlin` 成功。更早的副本 09 曾通过 `testDebugUnitTest`，但它早于最后一轮同步、账户、服务端和冲突处理改动，不能代替最新全量验证。
- 未完成验证：停止前没有针对最新代码重跑 Python 全量测试、`testDebugUnitTest assembleDebug lintDebug compileDebugAndroidTestKotlin connectedDebugAndroidTest`、最终 APK 构建/哈希、真实模拟器冷启动与跨客户端同步、在线依赖扫描和 CI；因此上一节的完整验收数字与 APK 只代表此前检查点，不代表当前提交。
- 待接手重点：完成 AI 输入及全部来源的一致快照与提交前复核；验证未知记录满额轮转不会饿死队列或阻断 outbox；验证不同实体 kind 冲突与资源冲突在同步门内原子收敛；为永久缺失 blob 设置有界重试；为 `all_wifi` 资产补传调度不计费网络约束；复核并完成密钥轮换持久标记、分阶段恢复、登录/配对/注册回滚，以及服务端请求/密码资源限制、账户配额、轮换租约和全量重同步游标语义。
- 隔离位置：新增的交接编译副本、缓存和 Python 字节码缓存仍全部位于 `C:\tmp\mc-android-019fb725`；模拟器及 adb 可能仍在运行。后续清理前应先重新枚举进程与路径，再只删除这个明确的临时根。

## 2026-08-03：Android 验收收尾（快照一致性、unknown 轮换、冲突原子性、资产重试、网络约束、回滚与服务端复核）

- 状态：接手 08-01 中止的交接检查点，按“待接手重点”完成全部七项后在本轮提交。未推送分支、未创建 PR、未调用 Codex Security；所有构建、测试、模拟器与同步服务均在 `C:\tmp\mc-android-019fb725` 的新副本中运行，正式仓库无任何构建残留。
- AI 输入一致快照：`generateLatestTask` 现在把任务输入、任务主体、近 14 天记录、已发布签到、食品库、记忆、调整、偏好及其全部 head 修订 ID 放在同一个 `mutateTransaction`（同步门 + 单事务）内捕获；提交前在同一事务内重新校验全部来源的 head 修订 ID，任一来源在分析期间变化即拒绝保存。`MutationTransaction` 新增 `heads()`；`sourceSnapshot`/`provenance` 使用事务内捕获的版本。新增 instrumented 测试验证“快照后修改食品 → 提交被拒；重新捕获后提交成功”。
- unknown 满额轮换：`sync_unknown_entities` 增加 `reprocessAttempts` 列（Room 迁移 v2→v3，新增 schema 3.json 与 2→3 迁移测试）；重处理失败计数，达到 10 次后驱逐该行并释放容量（`summary.unknownEvicted`）。`SyncBudget` 新增 `tryReserve` 语义，`putUnknown` 在容量满时跳过并计数（`unknownSkipped`）而不再中止整个同步运行，outbox 推送与资产同步不再被未知记录容量阻断；单信封 16MiB 协议违规仍保留硬性拒绝。
- 冲突路径：复核确认按实体 kind 的 keep-local 解析、ASSET 冲突在同步门内的单事务提交与“文件缺失保持 unresolved 稍后重下”自愈路径；新增 instrumented 测试覆盖跨 kind 冲突 keep-local（head 保持本地 kind/revision、冲突关闭、本地 revision 重新入队）与 ASSET 冲突解析的文件+行原子提交、缺失文件保持 unresolved。
- 永久缺失 blob：下载循环对 404 缺失分块、超声明字节数、摘要不一致改为抛 `PermanentAssetException`，归类为永久失败（有界，零无限退避），资源保持 unresolved 由下次用户触发同步重试；`syncFailureDisposition` 新增该类型的 FAILURE 分类，单测把 404 加入永久状态清单。
- `all_wifi` 计费网络：引擎在计费网络跳过资产传输时置 `deferredAssetTransfer`，工作器随后用独立唯一名 + `NetworkType.UNMETERED` 约束排队后续任务（纯等待、无退避循环）；主同步任务保持 CONNECTED 约束不变。新增 `shouldDeferAssetTransfer` 纯函数单测与 UNMETERED workSpec instrumented 断言。
- 账户回滚：`confirmRegistration` 任一步失败（含恢复包 PUT 成功但本地激活失败）后尽力撤销已建 session、清除登录令牌与待注册状态，与 login/claimPairing 的 `throwAfterSessionCleanup` 回滚一致；新增 instrumented 测试验证失败后令牌、账户数据密钥与待注册状态全部清空且同步未启用。密钥轮换 `abort` 在本机无任何暂存时改为纯离线 no-op（不再需要 remoteDeviceId 或网络），与既有测试意图一致。
- 服务端复核与测试修复：复核确认请求体路由级限制（16KiB 认证/32MiB 推送/4MiB 分块/64KiB 小 JSON）、Argon2 固定成本 + 并发信号量 + 限流、六类账户配额、轮换租约心跳/过期接管、全量重同步负游标与并发写入回放、refresh 重用检测/设备撤销等均已实现并有测试；新增 blob 字节配额直测（`MEALCIRCUIT_SYNC_QUOTA_BYTES` 超限 413）。修复交接检查点遗留的 Python 集成测试：测试夹具补恢复包配置（服务端要求加密写入前必须先配置恢复密钥）与 capabilities 协商（拉取 limit 不得超过服务端有效上限）。
- 验证：Python 全量 `unittest discover -s tests` 205 项通过、1 项按设计跳过（PostgreSQL 地址）；Android JVM 单测 30/30、`assembleDebug`、`lintDebug` 0 error 14 warning（13 条 UseKtx + 1 条 ApplySharedPref，与既有基线一致）、`compileDebugAndroidTestKotlin`、真实 API 35 模拟器 `connectedDebugAndroidTest` 25/25 通过（0 跳过），其中真实同步服务的 Python→Android→新 Python 双向离线 revision 交换、合成 API Key/密码/恢复密钥服务端零明文扫描全部通过（`tools/cross_client_sync_test.py prepare/verify`，退出码 0）。模拟器人工流程：APK 安装、冷启动、今天/计划/我的三页导航、饮食记录保存后强停重启仍在、照片任务相机权限拒绝后应用存活且页面完整；截图存于 `artifacts\acceptance-*.png`。
- 最终产物：`handoff-validation-src-11\android\app\build\outputs\apk\debug\app-debug.apk`，大小 21,151,162 字节，SHA-256 `380F1F0A36C285F39A98A5FD3449657DEE0DA2DE90827292E3DA82A8058BFD4F`；androidTest APK 1,181,837 字节，SHA-256 `A2ACE60D9050D9D9CA85E7D920364AF046A1563A91B33BDC882A25B21DCC5598`。构建在 `handoff-validation-src-11` + 项目缓存 `handoff-project-cache-11`（GRADLE_USER_HOME 复用 `gradle-home`），同步服务运行于 `runtime-sync-acceptance`，全部位于 `C:\tmp\mc-android-019fb725`。
- 隔离复核：`%USERPROFILE%\.android` 顶层文件“文件名、长度、UTC ticks、SHA-256”清单摘要仍为 `05241f9bb14d8265dba4a2ea19b5801598857177820eee84a7b02a3aa9796826`，与验收前一致；正式仓库无 `.gradle`/`.kotlin`/`app/build`/APK/AAB/DEX/CLASS/lint 报告/`local.properties` 残留。
- 剩余风险：轮换中断恢复、永久缺失资产、计费网络、unknown 满额场景在代码层由单元/instrumented 测试覆盖，未在真实 UI 上逐一重演；`lintDebug` 14 条 warning 未清零（均为风格性，既有基线）；Android 仍只覆盖 API 35 x86_64 模拟器，实体相机、弱网、Play 分发与真实外部证书服务需生产环境验证。模拟器与同步服务进程已停止；需要视觉验收时按 `tools\enter-environment.ps1` 环境重启 AVD `MealCircuit_API35` 并安装上述 APK。
