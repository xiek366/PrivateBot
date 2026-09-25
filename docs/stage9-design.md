# Stage 9 设计文档：零基础分发（exe 打包 + 配置向导 + 自检 + 图文教程）

> 状态：**已确认**（用户回复「你自己决策」，授权按本文档推荐方案执行，不再逐项确认）
> 前置：Stage 1-7 已完成（Stage 7 待本地验收）
> 方案选型：用户在 A/B/C/D 四个方向中选定 **A + D**

---

## 1. 背景与目标

### 1.1 问题

当前项目对零基础用户的劝退点，按严重程度排序：

| 门槛 | 严重度 | 具体障碍 |
|---|---|---|
| NapCat + QQ 扫码登录 | 最高 | 需安装第三方 OneBot 实现、登录 QQ、对齐端口与 token，可能遇风控 |
| DeepSeek API Key | 高 | 需注册、充值、理解"密钥"概念、知道粘贴到哪 |
| Python 环境 + pip 依赖 | 中 | 需装解释器、处理 PATH、避开微软商店占位程序 |
| 手写 `.env` | 中 | 需在命令行编辑文本、字段填错、编码出错 |

### 1.2 本阶段目标

消灭上表中第 3、4 项门槛，并让第 1、2 项出错时用户能自助排错。

**明确不追求**：让用户在完全不了解 QQ 机器人生态的前提下跑起来。NapCat 安装与 QQ 扫码登录必须由用户完成，本阶段只提供清晰的图文教程。

### 1.3 交付形态

产出一个绿色可运行的目录（压缩包分发），用户解压后双击 `PrivateBot.exe`：

- 首次运行 → 弹出配置向导窗口 → 填 API Key 与 QQ 号 → 保存
- 之后每次双击 → 直接启动，控制台窗口显示运行日志

---

## 2. 范围边界

### 2.1 做什么

1. **运行时路径重构**（打包的前置改造，必做）
   - 打包后程序运行在临时解压目录，现有 `Path(__file__).parent` 基准会失效
   - 需引入统一的 `APP_ROOT` 概念：打包模式下为 exe 所在目录，源码模式下为项目根目录
2. **GUI 配置向导**（`tkinter`，Python 标准库）
   - 首次运行检测到配置缺失时自动弹出
   - 覆盖高频配置项，保存为标准 `.env` 格式
   - 提供"测试连接"按钮，验证 API Key 是否可用
3. **环境自检**
   - 启动前检查必填配置、目录可写性、OneBot 端口连通性、LLM 接口可用性
   - 失败时给出人话版错误提示与修复建议，而非 Python traceback
4. **打包**
   - PyInstaller **单目录（one-dir）**模式产出 `PrivateBot.exe`
   - 提供 `build.bat` 一键打包脚本
5. **图文教程**
   - `docs/beginner-guide.md`：从零开始的完整步骤，含 NapCat 安装、扫码登录、常见错误对照表

### 2.2 不做什么

- **不自动化安装/配置 NapCat**（方案 C 已排除；NapCat 版本变动与 QQ 风控会持续破坏此类自动化）
- **不改动现有业务逻辑**：`core/handler.py`、`core/llm.py`、`core/memory.py`、`core/state.py`、`core/proactive.py`、`core/session.py`、`persona/loader.py`、`adapter/onebot.py` 均不修改
- **不引入 GUI 重框架**：仅用 `tkinter`，不使用 PyQt / Kivy / Electron 等
- **不做自动更新机制**
- **不做安装包**（不使用 Inno Setup / MSI），只做绿色压缩包
- **不做多平台**：仅 Windows x64

---

## 3. 关键设计决策

### 3.1 打包形态：单目录，而非单文件

| 选项 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 单文件（`--onefile`） | 分发只有一个 exe，观感最好 | 每次启动解压到临时目录，启动慢 3-10 秒；`__file__` 基准失效更严重；杀软更敏感 | 否决 |
| **单目录（`--onedir`）** | 启动快；`.env` / `data/` 与 exe 同级，用户可直接看到和备份 | 是文件夹而非单个文件 | **采用** |

采用单目录后，数据布局直观：

```
PrivateBot/                     ← 用户解压得到这个目录
├── PrivateBot.exe              ← 双击这个
├── _internal/                  ← PyInstaller 运行时（用户无需关心）
├── .env                        ← 向导生成
├── data/
│   ├── personas/default.yaml   ← 随包分发
│   ├── style/reference.txt     ← 随包分发
│   ├── state/                  ← 运行时生成
│   ├── memories/               ← 运行时生成
│   └── sessions/               ← 运行时生成
└── logs/
```

### 3.2 窗口形态：控制台 + 向导窗口并存

程序本质是常驻服务，用户必须能感知"它在跑"和"它挂了"。

- 打包为 **console 程序**，保留黑窗口实时显示日志
- 教程中明确写："这个黑窗口不要关，关掉机器人就停了"
- 向导以独立窗口弹出，不与主进程抢占控制台

备选（不采用）：GUI 化 + 系统托盘。理由：需额外引入 `pystray` 依赖，且托盘交互的排错信息量远不如日志窗口，与"零基础能自助排错"的目标相悖。

### 3.3 配置写入位置：exe 同级 `.env`

- 键名与 `.env.example` **完全一致**，不引入新的配置格式（保持向后兼容，源码模式与打包模式共用同一套配置）
- 源码模式下基准是项目根目录，打包模式下基准是 exe 目录
- 优先级：`.env` 文件 > 代码内置默认值

### 3.4 向导覆盖范围：只问 4 个必填项

向导**不覆盖全部配置**，只问用户真正需要理解的：

| 字段 | 必填 | 说明 |
|---|---|---|
| `LLM_API_KEY` | 是 | DeepSeek 密钥 |
| `ALLOWED_QQ` | 是 | 谁能跟机器人聊天（明确提示"不是机器人自己的 QQ 号"） |
| `ONEBOT_ACCESS_TOKEN` | 是 | 需与 NapCat 配置一致（默认值与 NapCat 默认一致） |
| `ONEBOT_WS_PORT` | 是 | 默认 8080，与 NapCat 对齐 |

其余配置项（`MAX_HISTORY`、`SUMMARY_THRESHOLD`、`PROACTIVE_*` 等）**不进入向导**，使用代码默认值，仅高级用户在 `.env` 里手动调整。理由：向导每多一个字段，零基础用户放弃的概率就上升一分。

---

## 4. 协议契约

### 4.1 路径解析契约

新增 `paths.py`，全项目唯一的路径基准来源：

```python
APP_ROOT: Path      # 打包模式 = Path(sys.executable).parent；源码模式 = 项目根目录
ENV_PATH: Path      # APP_ROOT / ".env"
DATA_DIR: Path      # APP_ROOT / "data"
LOG_DIR: Path       # APP_ROOT / "logs"
```

判定方式：`getattr(sys, "frozen", False)` 为 `True` 表示 PyInstaller 打包运行。

**约束**：`config.py`、`main.py` 中所有现有路径解析（`_ENV_PATH`、`_parse_persona_path`、`_parse_dir`、`BASE_DIR`、`SESSION_DIR`）必须改为基于 `APP_ROOT`，不得再使用 `Path(__file__).resolve().parent`。

### 4.2 `.env` 文件契约

- 编码：UTF-8 无 BOM
- 格式：`KEY=VALUE`，每行一条，允许 `#` 注释行
- 向导写入时**保留模板中的注释与分组**，仅替换对应键的值（便于用户后续手动编辑）
- 写入前先写临时文件再原子替换，避免写一半崩溃损坏配置

### 4.3 自检结果契约

自检模块返回结构化结果，供控制台与向导共用：

| 检查项 | 失败时的用户可见提示 |
|---|---|
| 配置文件存在 | 未找到 .env，请运行配置向导 |
| 必填项齐全 | 缺少 XXX，请在配置向导中填写 |
| `data/` 可写 | 程序所在目录无写入权限，请换到非系统盘目录（如 D 盘） |
| OneBot 端口可占用 | 端口 8080 已被其它程序占用，请先关掉重复运行的 PrivateBot，或换一个端口（同时要把 NapCat 的端口改成一样的） |
| LLM 接口可用 | API Key 无效或余额不足（附 HTTP 状态码） |

> **端口检查的方向性说明**：PrivateBot 是 OneBot 反向 WebSocket 的**服务端**，NapCat 是**客户端**。
> 因此这里检查的是"本程序能否成功占用该端口"（`bind`），而不是"能否连上 NapCat"。
> 反了就会把正常情况误判为故障。

### 4.4 日志契约

- 控制台输出：与现状一致（`setup_logging` 不变）
- 文件输出：新增写入 `logs/privatebot.log`，按天轮转，保留 7 天
- 目的：用户关掉黑窗口后日志仍在，"把日志发我看看"才成为可行的求助路径

---

## 5. 文件清单与职责

### 5.1 新增文件

| 文件 | 职责 |
|---|---|
| `paths.py` | 统一的运行根目录与路径解析，打包/源码双模式 |
| `setup_wizard.py` | tkinter 配置向导窗口，读写 `.env` |
| `selfcheck.py` | 环境自检，返回结构化结果 + 人话提示 |
| `PrivateBot.spec` | PyInstaller 打包配置（one-dir、console、显式收集 Anaconda 的 Tcl/Tk DLL） |
| `build.bat` | 一键打包：准备独立构建环境、清理、PyInstaller、复制 data、产出压缩包 |
| `requirements-build.txt` | 仅构建期依赖（PyInstaller） |
| `docs/beginner-guide.md` | 零基础图文教程（纯文字） |
| `docs/stage9-design.md` | 本文档 |

### 5.2 修改文件

| 文件 | 修改点 | 影响面 |
|---|---|---|
| `config.py` | `_ENV_PATH`、`_parse_persona_path`、`_parse_dir` 的基准改为 `APP_ROOT` | 3 处路径常量 |
| `main.py` | `BASE_DIR` / `SESSION_DIR` 改用 `paths.DATA_DIR` | 2 处常量 |
| `config.py` | 顺带修正 `LLM_MODEL` 默认值与 `.env.example` 不一致（`deepseek-chat` vs `deepseek-flash`） | 1 行 |
| `config.py` | 新增 `setup_logging` 的文件 handler（或抽到 `paths.py`） | 新增逻辑 |
| `AGENTS.md` | 阶段表新增 Stage 9 行 | 1 行 |
| `.gitignore` | 放行 `build/`、`dist/`、`*.spec` 的取舍 | 视打包产物是否入库决定 |

### 5.3 新增依赖

| 依赖 | 用途 | 性质 | 是否进 `requirements.txt` |
|---|---|---|---|
| `tkinter` | 配置向导 GUI | Python 标准库 | 否（Windows 官方安装包自带） |
| `PyInstaller` | 打包工具 | **仅构建期使用** | 否，单独放 `requirements-build.txt` |

> 按 AGENTS.md §2.3，引入新依赖需用户同意。此处仅新增一个**构建期**工具，运行时不新增任何依赖，不违反"保持轻量"的约束。

---

## 6. 配置项

本阶段**不新增任何配置键**，全部沿用现有 `.env`。向导仅负责写入其中 4 个必填项（见 §3.4）。

---

## 7. 打包与分发流程

```
build.bat
  ├─ 0. 准备独立构建环境 .build-venv（依赖已就绪则跳过，避免每次构建都联网）
  ├─ 1. 检查 Python 是否可用
  ├─ 2. 清理 build/ dist/
  ├─ 3. pyinstaller PrivateBot.spec
  ├─ 4. 复制 data/personas、data/style 到 dist/PrivateBot/
  ├─ 5. 复制 .env.example 到 dist/PrivateBot/
  └─ 6. 打包 dist/PrivateBot → PrivateBot-win64.zip
```

> `.build-venv/` 是构建期产物，已加入 `.gitignore`。
> 用独立环境构建的原因：宿主（尤其 Anaconda base）可能装有干扰 PyInstaller 的杂包，
> 详见 §8.2 的实测记录。

`PrivateBot.spec` 关键配置：

- `console=True`（保留日志窗口）
- `datas`：不内嵌 `data/`，改为构建后复制（用户可直接看到并编辑人设与语料）
- `hiddenimports`：`websockets`、`httpx` 相关隐式导入需按实际报错补齐

---

## 8. 验收清单

> 标记说明：`[x]` = 已实测通过；`[~]` = 部分实测/未专门实测（附原因）；`[ ]` = 待用户侧人工验收。

### 8.1 源码模式（回归验证，确认未破坏现有功能）

- [x] `python main.py` 在项目根目录下行为与 Stage 7 一致
      —— 实测 `APP_ROOT` = 项目根目录，`persona/style/state/memory/sessions` 五个目录全部解析到项目内；
      配置完整时 `ensure_ready()` 直接返回 True，不弹向导
- [x] `.env` 仍从项目根目录读取，`data/` 仍在项目根目录
- [~] 人设热重载、语料热重载仍生效 —— 相关代码未改动（`persona/loader.py` 零修改），未专门回归
- [~] `tools/mock_client.py` 全链路测试仍通过 —— `--case reject`（白名单外丢弃）在打包版实测 PASS；
      `--case echo` 需要真实 API Key，待用户侧验收

### 8.2 打包模式

- [x] `build.bat` 一次跑通，产出 `PrivateBot-win64.zip`（13.6 MB，1005 个条目）
- [x] 解压到非系统盘目录双击 exe 可启动（实测 `dist\PrivateBot\`，APP_ROOT 正确指向 exe 同级）
- [x] 首次启动（无 `.env`）自动弹出向导窗口（实测进程存活、向导窗口保持打开）
- [~] 向导"测试连接"能正确区分有效/无效 API Key —— 无效密钥路径实测返回 401 与人话提示；
      有效密钥路径待用户侧用真实 Key 验收
- [x] 保存后 `.env` 生成在 exe 同级，内容格式与 `.env.example` 一致
      —— 实测：保留模板注释与分组、仅替换 4 个键、UTF-8 无 BOM、先写临时文件再原子替换、无临时文件残留
- [x] 二次启动直接进主流程，不再弹向导
- [x] 运行数据（sessions/state/memories）写入 exe 同级 `data/`，而非临时目录
- [~] `logs/privatebot.log` 正常生成 —— 已实测生成且为 UTF-8 with BOM（记事本可直接正确显示中文）；
      按天轮转未实测
- [~] 断开 NapCat / 端口被占用时，错误提示为人话版本 —— 提示文案已实现，未构造故障场景实测

> 实测中发现并已修复的两个打包坑（Anaconda 环境特有，务必保留修复）：
> 1. Anaconda 的 `tcl86t.dll` / `tk86t.dll` 在 `Library\bin` 而非 `DLLs`，PyInstaller 解析不到，
>    会导致打包后 `_tkinter` 加载失败、配置向导直接失效。已在 `PrivateBot.spec` 里显式收集。
> 2. 宿主环境若装有过时的 `pathlib` backport，PyInstaller 会拒绝运行。因此 `build.bat` 使用
>    独立虚拟环境 `.build-venv` 构建，不受宿主环境污染。

### 8.3 零基础可用性

- [ ] 按 `docs/beginner-guide.md` 从零操作，全程无需命令行（待用户侧人工走查）
- [x] 教程包含 NapCat 安装与 QQ 扫码登录完整步骤
- [x] 教程包含常见错误对照表（端口占用、token 不匹配、Key 无效、目录无写权限）

---

## 9. 决策点与结论

用户回复「你自己决策」后，按本文档推荐方案拍板如下：

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 阶段号 | **Stage 9**（Stage 8 保留给 LLM JSON 输出等优化） |
| 2 | 打包产物是否入库 | **不入库**。`build/`、`dist/`、`PrivateBot-win64.zip` 加入 `.gitignore`；`PrivateBot.spec` 与 `build.bat` 属于构建脚本，**入库** |
| 3 | 是否引入 PyInstaller | **接受**，但仅作为构建期依赖，放 `requirements-build.txt`，运行时不新增任何依赖 |
| 4 | 窗口形态 | **控制台窗口 + 向导窗口**，不做系统托盘（托盘需额外依赖，且排错信息量远不如日志窗口） |
| 5 | 教程是否配截图 | **纯文字，无截图**。截图易随版本失效，改为写清"怎么判断成功"与常见错误对照表 |

补充实现约定（写代码时确定）：

- **启动自检的取舍**：配置类检查（`.env` / 必填项 / 目录可写）为**硬失败**，不通过就弹向导或退出；
  端口占用为**硬失败**（提前拦截，避免抛 traceback）；LLM 可用性为**仅告警**（网络抖动不该拦住启动）。
- **向导兜底模板**：`.env.example` 缺失时使用 `setup_wizard.py` 内置的最小模板。
  由于 `config.py` 中每个配置键都有代码默认值，向导只写 4 个必填项即可正常启动。