# TrainSolve · 程序设计训练平台自动刷题客户端

把「选题 → 写代码 → 自检 → 提交 → 等判分 → 按报错修改」这一整套重复劳动交给脚本。Windows 桌面版开箱即用，命令行版可脚本化，模型接 DeepSeek，可换任意 OpenAI 兼容网关。

![version](https://img.shields.io/badge/version-0.6.9-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey)

[下载最新版](https://github.com/Huang0907/train-solver/releases) · [使用说明](docs/使用说明-公开版.md) · [真实跑题记录](docs/DEMO.md)

> **它自动化的是你自己的练习账号。** 提交机会有限，默认配置是「只解题、不提交」；第一次请先试运行，确认没问题再自己开真提交。不要在考试或计时测验中使用。

---

## 它解决什么

平台一次刷题要手工做六件事：选标签、开题、读懂样例、写 C 代码、本地对样例、提交等判分。这个工具把六件事串成一条流水线，跑完一道自动接下一道；中途只在你要求时停下来问一句。

它不靠浏览器自动化取巧：取题、暂存、提交、判分都走平台前端使用的那套 HTTP 接口；「模拟真人打字」是可选分支，用于需要真实按键轨迹的场景。

## 亮点

| | |
|---|---|
| **纯 HTTP** | 直接调平台接口，不需要浏览器、不受编辑器「禁止粘贴」等前端限制影响 |
| **零第三方依赖** | 核心只用 Python 标准库（`tkinter` / `urllib` / `hashlib` / `subprocess` / `tomllib`） |
| **模型可插拔** | `Solver` 只有两个方法，换模型 / 换网关 / 换人工只需实现一个类 |
| **提交前自检** | 先用本地 gcc 或平台 `run-code` 编译跑样例，逐字节比对通过才提交，少浪费重做额度 |
| **提交留痕** | 每次提交都落 `submit_N.json`（平台原始返回 + 业务码）；码与记录状态冲突时报「提交状态未知」，不伪装成功 |
| **全自动** | `--auto` / `--count 0` 连续刷，遇到题库抽空、本周限额、连续未满分、连续模型故障自动停 |
| **安全默认** | 公开包默认 `dry_run = true` / `submit = false`；账号、密码、Key 不进 exe、不进仓库、不进日志 |
| **两种作答** | 默认 API 直接交；也可切「模拟真人打字」，在真实 Chrome 里逐字符键入并真实点击提交 |

## 架构

![TrainSolve 模块级架构](docs/architecture-detailed.png)

本机进程内分四层：**入口层**（桌面客户端、设置 / 更换模型、命令行）、**配置与编排**（配置加载、Runner、校验码）、**适配层**（Solver、TrainClient、自检 Judge、真人打字、打字引擎）与 **产物目录**，每块都标了对应源码文件；图下方四张卡片按同样的分层再汇总一遍。桌面客户端与命令行调用同一个 Runner；Runner 通过 `TrainClient` 对接训练平台、通过 `solver/` 对接 DeepSeek、通过 `judge.py` 做编译与样例比对，每次会话的全部证据落到 `out/<sessionId>/`。训练平台与 DeepSeek 是仅有的两个外部系统，都在本机边界之外；配置这一块 0.6.9 起多了 `configcrypt.py`（Windows DPAPI 加密、`.enc` 优先读），图中已补上。`tag` 与白名单、提交语义、安全默认见上图卡片与下文。

## 快速开始

### 1. 下载发布包

到 [Releases](https://github.com/Huang0907/train-solver/releases) 下载最新的 `TrainSolveClient-<版本>-public.zip`，解压得到：

```
TrainSolveClient.exe   主程序（已内置 Playwright，模拟打字开箱即用）
config.toml            配置（账号 / 密码 / API Key 全空；界面保存后变成加密的 config.toml.enc）
portable.txt           便携标记：只读同目录配置，不碰你机器的 %APPDATA%
使用说明.md            同版本说明
```

### 2. 填配置 → 先试运行 → 再开真提交

双击 exe，填平台学号 / 密码，在「更换模型…」里填 DeepSeek API Key 并保存。0.6.9 起保存会把配置**加密**写成 `config.toml.enc`（Windows DPAPI，只有当前 Windows 用户能解），同目录的明文 `config.toml` 随即被删掉。

默认是**试运行**（`dry_run = true` / `submit = false`）：只出题、生成答案、落盘，不提交。先跑一道，确认题目、代码、日志都对，再关掉客户端开真提交——界面没有这两个开关，改的是配置文件：

- **还没在界面保存过**（目录里仍是明文 `config.toml`）：用记事本改

  ```toml
  [run]
  submit = true
  dry_run = false
  ```

  再启动客户端；之后界面再保存，会带着这两个值一起加密。
- **已经加密成 `config.toml.enc`**：用源码 / 命令行版的 `trainsolve decrypt-config` 解出明文，改完再 `trainsolve encrypt-config --remove-plaintext`；没有 Python 环境就删掉 `config.toml.enc`，照 `config.example.toml` 重建一份明文再启动（账号与 Key 要重填）。

重启客户端即为真提交。**每次真提交都消耗平台一次提交机会。**

### 3. 用源码

```bash
python -m trainsolve gui          # 桌面客户端
python -m trainsolve --help       # 全部命令
python -m trainsolve login        # 登录并缓存 sid
python -m trainsolve run --count 1
```

Python 3.11+，核心零依赖。配置文件查找顺序：

1. 目录里有 `portable.txt` → **只认**同目录 `config.toml`（发布包都带）；
2. 否则当前目录 / exe 同目录有 `config.toml` → 用它；
3. 都没有 → `%APPDATA%\TrainSolve\config.toml`。

**同目录存在 `config.toml.enc` 时优先读它**（0.6.9 起界面保存默认加密）；明文与加密同时存在时只认 `.enc`。

仓库根的 `config.toml` 已被 `.gitignore` 挡住，不会提交。

## 模型配置

默认走 **DeepSeek 官方 API**：

```toml
[deepseek]
api_key = "sk-..."                       # 官方 Key
base_url = "https://api.deepseek.com"
model = "deepseek-flash"                 # 官方仅 deepseek-flash / deepseek-v4-pro
temperature = 0.2
max_tokens = 16000
timeout = 180
retries = 3
```

> 官方只认 `deepseek-flash` 与 `deepseek-v4-pro`；写成 `deepseek-v4.1-flash` 会被 HTTP 400 拒绝（那是本地网关的 `cn:` 私有 id）。

优先级：命令行 > 环境变量 > 配置文件。

```bash
python -m trainsolve run --api-key sk-xxx --model deepseek-flash

setx DEEPSEEK_API_KEY sk-xxx
setx DEEPSEEK_BASE_URL https://api.deepseek.com
setx DEEPSEEK_MODEL deepseek-flash
```

**备用端点（可选）**：默认只有主模型；客户端「新增备用」会写回 `[[deepseek.fallback]]`。有备用时开跑前逐个探活、运行中 503 / 超时 / 鉴权失败自动切换；**没有备用时探活失败就直接退出**，不会空开会话。

```bash
python -m trainsolve models           # 列出主 / 备端点
python -m trainsolve models --check   # 逐个发最小请求验证可用性
```

0.6.9 起 `--check` 与界面「测试连接」同口径：推理型模型把预算花在思维链上、正文为空时也算**可用**（探针预算 128），不再误报「网关没转发」。

## 桌面客户端

| 区域 | 内容 |
|---|---|
| 顶部 | 平台地址、学号、密码、登录、设置、连接状态 |
| 左侧「选题」 | 题型、难度、标签（可刷新）、数量、判题方式 |
| 左侧按钮 | 刷新标签、开始自动刷题、全自动连续刷、只看题、停止 |
| 左侧「人工模式」 | 本地自检我的代码、提交我的代码 |
| 右侧页签 | 日志、题目、代码（可编辑）、评分 |
| 底部 | 状态栏 + 进度条 |

网络与模型调用都在后台线程，UI 用队列收消息；点「停止」不会卡界面，而且**每一段都会看这个标志**：逐字打字时每敲一个字符查一次（本题标「已停止（打字过程中被中断，未提交）」），随机思考与题间等待按切片查，**解题算完、准备暂存/提交的最后一刻再查一次**，等待评分期间每轮查一次——命中就收尾，不暂存、不提交、不重答。只有一次已经发出去的模型 HTTP 请求要等它返回才停（返回后也不会再往下走）。关窗会取消所有待触发的定时器并请求停止。配置保存在 `%APPDATA%\TrainSolve\config.toml`（便携模式下在 exe 同目录）；0.6.9 起界面保存默认加密成同目录的 `config.toml.enc`。

## 命令行

| 命令 | 作用 |
|---|---|
| `gui` | 启动桌面客户端 |
| `login` | 登录并缓存 sid |
| `tags --qbase-type N` | 列出该题型的标签与可用性 |
| `run` | 主流程：选题 → 解题 → 自检 → 提交 → 评分 → 落盘 |
| `peek` | 只开会话把题打出来（不解题、不提交）；带 `--session` 只读 |
| `resume --session <id> --answers <file>` | 对已有会话续做并提交 |
| `redo --session <id> --mode reanswer\|half\|rescore` | 重新答题 / 折半重做 / 重新计分 |
| `cleanup [--session <id>] [--dry-run]` | 清理「未完成」会话（skip + cancel，真删记录） |
| `show --session <id>` | 查看该会话的产物与得分 |
| `prompt [record.json]` | 把记录渲染成提示词（调试用） |
| `models [--check]` | 列出 / 测试模型端点 |
| `encrypt-config [--source 明文] [--to 目标.enc] [--remove-plaintext]` | 把明文配置加密（DPAPI，仅当前用户可解） |
| `decrypt-config [--source 加密.enc] [--to 明文] [--force]` | 把加密配置解回明文（目标已存在时默认拒绝） |

### `run` 常用开关

| 开关 | 作用 |
|---|---|
| `--count N` / `--auto` | 做 N 道 / 一直做（`--count 0` 等同 `--auto`） |
| `--qbase-type 4 --level 2` | 题型（0 单选 1 多选 2 判断 3 填空 4 编程）、难度（0 随机 1 初学者 2 容易 3 中等 4 困难 5 极难） |
| `--tag -1` | 标签；`-1` 为平台随机题 |
| `--qtypes 5,6,7` | 题型白名单，默认只做编程类；`all` 不限制 |
| `--judge local\|server\|both\|none` | 本地 gcc / 平台编译 / 两者 / 不判 |
| `--max-repairs N` | 自检失败回灌修复的轮数（默认 3） |
| `--max-reanswers N` / `--no-auto-fix` | 提交后未满分时重答上限 / 直接关闭重答 |
| `--dry-run` | 只出题 + 生成答案 + 落盘，不暂存、不提交 |
| `--no-submit` | 自检通过后只落盘 |
| `--force` | 自检明确未通过也强制提交（默认不提交） |
| `--strict-verify` | 样例没还原出来也不提交 |
| `--max-failures N` | 全自动下连续未满分的熔断阈值（默认 3） |

0.6.9 起上表这些开关**只有显式写出来才生效**：没写的（`--tag` / `--level` / `--qbase-type` / `--count` / `--judge` / `--max-repairs` / `--score-timeout`）一律按配置文件里的值，不再被命令行默认值覆盖。

> `--dry-run` 与 `--no-submit` 会把会话留在「未完成」；`choose-q` 成功就产生一条记录。收尾用 `cleanup` 清掉，否则到期会变成 0 分。0.5.9 起 dry-run 不再被当成失败：填了 `--count` 就会跑满数量，不会做完第 1 题就停。

## 工作流程

```
login            POST api/student/auth                -> sid
list-tag         GET  api/student/list-tag            -> 可用标签
choose-q         POST api/student/choose-q            -> sessionId
record/{sid}     GET  api/student/record/{sid}        -> 题面 + answerPaper + 倒计时
solver.solve()   DeepSeek /chat/completions           -> C 代码或客观题答案
judge            本地 gcc 或 POST api/student/run-code -> 编译 + 样例逐字节比对
                 └─ 不通过 -> solver.repair() 回灌反馈，最多 N 轮
post-answer      POST api/student/post-answer         -> 暂存（带 verificationCode）
submit           POST api/student/submit?sessionId=   -> 交卷
record/{sid}     GET  轮询 state                      -> 已完成 / 评分中 / 失败
score-result     GET  api/student/score-result/{sid}  -> 逐题得分、失败用例
```

两个关键细节：

1. **`verificationCode`** = `md5("%Y-%m-%d %H:%M " + sessionId)`（本地时间，分钟粒度）；算错返回 `code=8`。工具会尝试 ±1..±8 小时偏移自动校准时区。
2. **判题逐字节比对**（`compareMode` 有 `exact` / `token`），并带 `directOutputCheat` 硬编码检测；提示词要求真实算法、输出逐字一致，工具不会去调参考答案接口取巧。

提交语义：`submit` 的原始返回与业务码都落盘；认不出的码按「无法确认」处理。业务码说成功、记录却回到「未完成」时，报**「提交状态未知：…提交被撤销」**并停止后续提交——以记录状态为准。

## 产物与排错

```
out/<sessionId>/
├─ statement.md / record.json       题面与原始响应
├─ Q<id>.c                          提交的源码
├─ answerPaper.json                 实际提交的答案
├─ score_<N>.json                   第 N 次提交的得分 / 失败用例
├─ submit_<N>.json                  第 N 次提交的原始返回 + 业务码
├─ record_before_reanswer_<N>.json  重答前的记录快照
└─ reanswer_<N>.json                重答结果
out/session.json                    登录会话缓存
```

提交出问题时先看 `submit_<N>.json` 的 `{attempt, code, raw}`（`attempt` 从 0 开始），再看 `record_after.json` 的 `state` 与 `timeline`。

## 配置参考

三节：`[platform]`、`[deepseek]`、`[run]`。完整示例见 `config.example.toml`，公开版模板见 `config.public.toml`。0.6.9 起界面保存默认加密为 `config.toml.enc`，同目录有 `.enc` 时优先读它；明文与加密二选一，改配置见「快速开始」。

| 节 | 关键项 | 默认 |
|---|---|---|
| `[platform]` | `base_url` / `timeout` | 平台地址 / `25` |
| `[deepseek]` | `api_key` / `base_url` / `model` | 空 / `https://api.deepseek.com` / `deepseek-flash` |
| `[run]` | `qbase_type` / `level` / `tag_id` | `4` / `2` / `-1` |
| `[run]` | `count` / `judge` / `qtypes` | `1` / `local` / `[5,6,7]` |
| `[run]` | `submit` / `dry_run` | `true` / `false`（**公开包改为 `false` / `true`**） |
| `[run]` | `max_repairs` / `auto_fix_from_judge` | `3` / `false` |
| `[run]` | `allow_unverified_submit` / `require_verified` | `false` / `false` |
| `[run]` | `cleanup_abandoned` / `auto_cleanup` | `true` / `true` |
| `[run]` | `max_consecutive_failures` / `_skips` / `_infra_errors` | `3` / `5` / `2` |

## 返回码

- `choose-q`：`0` 成功 / `1` 暂无新题 / `2` 本周已完成
- `post-answer`：`0` 成功 / `1` 记录不存在 / `2` 已提交 / `3` 试卷不存在 / `4` 学生不存在 / `5` 心跳超时 / `6` 已被购买 / `7` 超时 / `8` 校验码错误 / `9` 截止已过 / `10` 未开始
- 记录 `state`：`0` 未完成 / `1` 已完成 / `2` 评分中 / `3` 已跳过 / `4` 稍后做 / `5` 失败

`reanswer` / `half-redo` / `rescore` 返回业务码（`0` 成功、`1` 拒绝），不是布尔；记录不在「已完成」时 `reanswer` 一律返回 1。`half-redo` 消耗 2 次重置额度，并按当前暂存内容重新评分。

## 常见问题

**第一次真提交要注意什么？** 先用默认试运行跑一道，看 `out\<id>\` 里的题面、代码、评分是否都对，再改配置开真提交。

**为什么抽到的题不是我选的题型？** 标签选「随机（-1）」时平台可能返回白名单外的题型（如改错题）。工具会自动 `skip-this` 跳过，代价是一次抽题配额 + 约 60 秒限流等待；想稳就选具体标签。**指定了标签却一直跳过**时，多半是这个标签下的题本来就不在你的「题型」白名单里（例如标签里全是改错题 8/9，而白名单是 5/6/7）：日志会直接点名，并在连续两次跳过后停下，别让它继续烧配额。

**勾了「模拟真人打字提交」，为什么没提交？** 看左侧那行状态：**试运行（`dry_run=true`）** 只解题不提交，**不提交（`submit=false`）** 同理，只有 **真提交** 才会消耗作答机会。公开包默认就是试运行，要真提交得改配置文件里的 `dry_run` 与 `submit`（GUI 不提供这两个开关，避免误开；0.6.9 起配置默认已加密成 `config.toml.enc`，改法见「快速开始」）。注意：开着「模拟真人打字」时，试运行/不提交也**会真的打字并等待思考时间**，只是最后一律不点提交——所以一题耗时和真提交时差不多。

**提交后状态又回到「未完成」？** 那是自动重答把记录重开了。0.5.5 起默认关闭（`auto_fix_from_judge = false`），不要手动打开。

**模型报 400 / 401？** 400 多为模型 id 不对（官方只认 `deepseek-flash` / `deepseek-v4-pro`）；401 多为 Key 与接口不匹配（`wbk_` 是网关 Key，`sk-` 是官方 Key）。

**关掉窗口后台还在跑吗？** 关窗会请求停止并取消定时器；当前题若已进入提交阶段会做完该步再退。

**答题时要不要让 Chrome 一直在最前面？** 不用。逐字输入走 CDP 注入，不看窗口焦点；每 8 行会自己把光标点回编辑器（不弹窗、不抢前台）。锁屏不行——锁屏后渲染会被节流，别指望它在你离开时跑完。

**点「停止」为什么没立刻停？** 分几种情况看日志：逐字打字时是**每敲一个字符**查一次（命中即停、绝不提交）；随机思考、题间等待、等评分都是按切片查，通常一秒内就退。唯一要等的是**已经发出去的那次模型 HTTP 请求**——它返回后立刻停，而且**不会再暂存或提交**（0.6.6 起，解题后、提交前、重答前都会再查一次停止）。如果日志出现「已提交，评分未回」，那是提交已经发生、只是不等评分了，记录以平台为准。

## 已知限制

| 限制 | 说明 |
|---|---|
| 真提交链路 | 0.5.4 出过事，0.5.5 已修，2026-11-08 真平台复测通过；0.6.9 真机全流程 12/12（history 74→75）。第一次用仍建议先试运行 |
| 随机题跨界 | `tag=-1` 可能抽到白名单外题型，会跳过并产生一条 `state=3` 记录 |
| 评测数据是 CRLF | `gets()` 会把行尾 `\r` 读进来导致误判；提示词已要求 `s[strcspn(s,"\r\n")]='\0'` |
| `gets` 已废弃 | 平台编译器只给 warning；将来收紧标准需改 `fgets` |
| 客观题 beta | 答案写进 `answerPaper.q0..q3`，建议先 `--qbase-type 0 --count 1 --dry-run` |
| 改错题 / 设计题 | 改错题默认被白名单挡住（要做得 `--qtypes all`）；设计题不支持 |
| 无样例 | 默认仍提交，由平台判分反馈驱动修复；要保守用 `--strict-verify` |
| 自检未通过 | 默认不提交（交了也是 0 分）；确实要交加 `--force` |
| 评分排队 | `score-result` 可能长时间为空，工具轮询记录 `state`，默认等 600s |
| 配置加密 | `.enc` 用 Windows DPAPI，绑定当前用户与机器：换机器、换 Windows 用户、拷给别人都解不开；打包版没有 CLI，加密后改 `dry_run` / `submit` 要按「快速开始」重建明文 |
| 打字缩进 | 行首缩进不参与比较，由编辑器的智能缩进决定；自动补括号与焦点自愈依赖平台 CodeMirror 的 `.cm-line` 结构，平台改前端可能失效 |
| 高 DPI | 只实测 150% 一档；100% / 125% / 175% 与多屏不同缩放未实测 |
| 本周限额 | `choose-q` 返回 `2` 时提示并退出（退出码 3） |

## 开发与测试

```bash
python -m unittest discover -s tests     # 420 条，全部离线，不需要账号与网络
```

覆盖：校验码时区、样例还原与逐字节比对、提示词与改错模式、推理模型空回复重试、`--qtypes` 解析、主备端点排序与 failover、清理逻辑、离线管线（含 dry-run / 自检闸门 / 白名单跳过 / 重答停止条件 / 全自动熔断）、GUI 构造与「更换模型」往返、模拟打字离线用例、配置加密（DPAPI 往返 / `.enc` 优先 / 拒绝明文冒充加密）、打包脚本与版本一致性、只读冒烟 `--self-test` 回归。

`fixtures/` 是真实接口响应，可离线复现；更细的踩坑记录见 [docs/DEMO.md](docs/DEMO.md)。

## 打包发布

```powershell
powershell -ExecutionPolicy Bypass -File build_release.ps1
```

依次：跑测试 → 按 `trainsolve.__version__` 生成 Windows 版本资源 → PyInstaller 打 onefile exe（内置 Playwright driver）→ **解包后逐模块扫描凭据**（名单从本机 `config.toml` / `config.toml.enc` 读取，真实学号 / Key 不写进仓库）→ 校验版本资源 → 组装公开版目录 → 压缩 → 再扫一遍公开版（三个凭据字段必须为空）。

只产出 public 版：`release\TrainSolveClient-<版本>-public\`（目录 + zip）。公开版内是 exe、空配置、`portable.txt` 与使用说明；onefile 冷启动 4–5 秒。

## 模拟真人打字

平台会记录 `keySequence` / `inputSequence` 并禁止粘贴，直接调接口交答案在按键轨迹上可分辨。该分支把**作答**换成真实 Chrome 里逐字符键入 + 真实点击提交，其余仍走 API。

- 桌面版勾选「模拟真人打字提交」即可（driver 已内置）；
- **打字速度与思考区间在主界面也有一份**：左侧「模拟真人打字」下面两行「打字速度 [220] 字符/分」「随机思考 [min]–[max] 分钟」，随时可改、越界自动夹紧、写反自动交换，与设置里那对参数双向同步；开始跑时按主界面当前值生效；
- **答题不需要浏览器在前台**：逐字输入全程走 CDP 注入，Chrome 最小化或被你切到别的窗口都照常；每 8 行做一次焦点自愈——**先只做页面内 DOM 聚焦（不动光标）**，DOM 聚焦无效才用 CDP 鼠标点一下，点完立刻 `Ctrl+End` 把光标送回文末；打字过程中每 12 个按键再确认一次焦点，连续几下都打不进编辑器就重新聚焦。既不会把 Chrome 拽到前面，也不会把字打到正文中间。日志会写「后台可跑、不要求 Chrome 置前」；
- **中文注释/字符串也能打**：ASCII 走原生按键（带 Shift），非 ASCII 单字符走 `Input.insertText`（等价输入法上屏，一次仍只插 1 个字符，不碰平台「>50 字符才算粘贴」的阈值）；其余仍逐字符真实按键；
- **逐字符自适应 + 自动补全**（0.6.8 起，0.6.9 加固）：每敲一个字符前先读**光标前/后**——光标前是正确前缀就补下一个字符，编辑器自动补的 `)` 等尾巴用 `Delete` 删掉，丢键与多插在下一次按键前吸收；**行首缩进不再逐格对齐**（编辑器回车补 8 空格、打 `{` 时整行重排，硬对齐只会和它拉锯），打字校验、暂存同步、运行核对统一按「忽略行首空白」比较；
- 源码运行需 `pip install playwright`，Chrome 用 `--remote-debugging-port=9333` 启动（没开会自动拉起独立 profile）；
- 第一次建议 `--human-dry-run`（只打字、回读校验，不点提交）；
- 作答前可加一段**随机「思考时间」**：每题在 `--human-delay-min` ~ `--human-delay-max` 分钟区间内独立取一个值（0–60），期间按「停止」可立即中断且不提交。桌面版在设置 → 模拟人类输入里填同一对区间，**随时可改**；启动日志会打印本轮的「节奏：每题随机思考 3–12 分钟；随机退格 0.01 / 每次最多 2 字符」，方便核对是否生效；
- 打字过程中会**随机退格**：每字符按 `--backspace-rate`（默认 0.01）判定，删掉刚打的 1–`--backspace-max`（默认 2）个字符再原样补回——内容不变、不会越过行首，只是多几次真实按键，打断匀速机器流。桌面版同一节可调，填 0 关闭。

```powershell
python -m trainsolve run --count 1 --human --cpm 220
python -m trainsolve run --count 1 --human --cpm-min 180 --cpm-max 400
python -m trainsolve run --count 2 --human --human-delay-min 3 --human-delay-max 12
python -m trainsolve run --count 1 --human --backspace-rate 0.03 --backspace-max 3
```

关键实现：每字符一条原生按键（`Input.dispatchKeyEvent`，Shift 符号单独按下）；逐字符读光标前后、自动补全的尾巴用 `Delete` 收尾；行首缩进交给编辑器的智能缩进，比较时忽略；回读与按键共用同一 CDP 会话且连续两次一致才采信；提交前必须等到 `post-answer` 的 **HTTP 2xx + 业务 code=0 回执**（只等请求体不够，平台组件状态在响应回调里才更新）。

限制：只覆盖编程题；50 行约 1.5–3 分钟（倒计时仍在走）；依赖平台前端结构（选择器集中在 `trainsolve/human.py` 顶部）；需要真实 Chrome。详见 [docs/评估-模拟人类输入.md](docs/评估-模拟人类输入.md)。

## 版本历史

| 版本 | 主要变更 |
|---|---|
| **0.6.9**（2026-11-11） | 五组改动：**配置文件加密**（新增 `configcrypt.py`，Windows DPAPI 绑定当前用户，文件头 `TRAINSOLVE-ENC-v1`）——界面保存默认写 `config.toml.enc` 并删明文，加载器四条分支统一「同目录有 `.enc` 优先」，`.enc` 里却是明文直接报错；新增 `encrypt-config` / `decrypt-config`（输出必须 `.enc`、不得等于源文件，解密不覆盖已有明文），读不到/解不开时 CLI 退出码 2、界面弹窗给恢复办法。**模拟打字**：行首缩进不再逐格对齐（编辑器智能缩进会补 8 空格、`{` 整行重排），打字/校验/暂存同步统一按「忽略行首空白」比较；光标列号用 DOM Range 量，自动补的 `)` 按光标前后用 `Delete` 删尾巴；每 12 个按键确认一次焦点。**模型自检**：正文为空但思维链非空也算可用（探针预算 128，`models --check` 与界面「测试连接」同口径）。**CLI**：`run` / `peek` / `resume` 的开关改为只有显式给出才生效，不再覆盖配置文件。**界面**：回调异常落 `client-errors.log` 并弹窗、启动期配置错误也弹窗。**真机全流程验证**：Q3077 逐字输入 1178 字符 / 54 行、暂存 790 字符回执 `code=0`、提交 **12/12**（history 74→75，唯一新增本次会话）。**420 条测试**。 |
| **0.6.8**（2026-11-11） | 打字改为**逐字符自适应**：每敲一个字符前先读当前行，按真实内容决定下一个键——正确前缀只补下一个字符、多出字符（自动补全插的 `}`、多敲的键）立即退格、少字符补打，丢键与多插在下一次按键之前就被吸收；读到旧值时先稳定回读再判断，不会重复输入；随机退格删完立即原样补回、不丢进度。同时修三处停止/界面问题：**打字中断与思考等待中断漏走统一收尾**（被算成一道未满分题、还会打假的「遇错即停」）、**同步窗口内点停止仍会提交**（`wait_app_code` 支持中断 + 提交前闸门，dry-run 下也如实返回停止）、**主界面手输速度被设置页回滚**（打开设置前先写回配置）。日志补错字率，「行内修复」改称「丢键补打」。**390 条测试**。 |
| **0.6.7**（2026-11-11） | 修模拟输入三处：**焦点自愈会把光标点到正文中间**（0.6.5 引入——每 8 行无条件点一次鼠标，而打字器假设光标永远在文末，于是后续字符插进代码中间、整篇校验失败只能清空重打）；改为先纯 DOM 聚焦、需要点击时点完 `Ctrl+End` 回文末。**非 ASCII 字符处理是坏的**：汉字是 `isalpha()`，被编成 `Key中`+虚拟键码 20013 的非法键；中文标点/不间断空格直接 `ValueError` 让整题失败——改为非 ASCII 走 `Input.insertText`。另修 Shift 字母的 `unmodifiedText`（`A`→`a`），并给回读 `_settle`、行内校正 `_repair_line` 补上停止检查。 |
| **0.6.6**（2026-11-10） | 修「停止」只在主循环开头与打字循环里查的漏洞：**解题算完仍会暂存并 submit**（白烧一次作答机会）、修复轮、`wait_record`（最多 300 秒）、重答流程、题间等待、界面确认弹窗全都无视停止。现在解题后、提交前、修复前、评分轮询、重答前逐个查；`stopped` 收尾不暂存不提交并清理未提交会话；已提交的场景只放弃等评分、不动记录。新增 7 条用例，四处修复逐条造红。 |
| **0.6.5**（2026-11-10） | 三处体验修复：**停止立即中断**（逐字打字时每字符查标志、抛 `HumanStopped`，本题标「已停止、未提交」，不再打完才停）；**主界面常驻速度与思考**（打字速度、随机思考区间两行，与设置双向同步、写反自动交换）；**答题不要求浏览器焦点**（每 8 行焦点自愈，后台可跑、不把 Chrome 拽到前面）。 |
| **0.6.4**（2026-11-10） | 修真人模式的执行顺序：此前 `dry_run` / `submit=false` 会在「模拟真人打字」分支之前提前返回，导致配置好的**随机思考时间与逐字输入一次都没发生**。现在先按真人方式打完再决定交不交，日志区分 `dry-run：已按真人方式输入并校验，未提交`。 |
| **0.6.3**（2026-11-10） | 默认模型入口统一为官方 `deepseek-flash @ https://api.deepseek.com`：设置里的「模型」下拉框只列官方 id 且 flash 在第一位，去掉旧网关 `cn:` 选项；`--model` 帮助文字同步（原先还写着 deepseek-chat）。配置默认值本来就是官方 flash，无迁移。 |
| **0.6.2**（2026-11-10） | 诊断向：指定标签却抽到被排除的题型时，日志点名标签与题型，并在连续两次跳过后自动停（不再白烧抽题配额）；主界面常驻一行「试运行 / 不提交 / 真提交」，勾选框文案不再声称会提交。 |
| **0.6.1**（2026-11-10） | 随机思考时间区间可在设置里随时改（两个输入框，0–60，写反自动交换）；启动日志新增「节奏」一行，打印本题实际的思考区间与退格参数；架构图更新到当前模块（打字引擎标注随机退格）。 |
| **0.6.0**（2026-11-10） | 模拟输入两项：每题**随机思考时间**（`human_delay_min`~`human_delay_max`，0–60 分钟区间内独立取值，等待中「停止」可立即中断）；打字**随机退格**（`backspace_rate`/`backspace_max`，只删本行刚打的字符再原样补回）。 |
| **0.5.9**（2026-11-10） | 修 dry-run 只做 1 题（未提交不再计为失败，填数量跑满）；设置新增「遇到未满分也继续」（= `--keep-going`）。 |
| **0.5.8**（2026-11-09） | 仓库开源（MIT）；README 重写；构建扫描器不再硬编码真实学号 / Key 前缀，改从本机 `config.toml` 读。无运行时变更。 |
| **0.5.7**（2026-11-09） | `runner.py` 行尾统一为 LF（跨平台编辑不再产生伪 diff）。无运行时变更。 |
| **0.5.6**（2026-11-08） | 关窗时取消所有 `after()` 定时器（避免窗口销毁后回调打到已销毁控件）；只读冒烟 `--self-test` 纳入回归。 |
| **0.5.5**（2026-11-08） | 真提交事故修复：`auto_fix_from_judge` 默认关闭（平台「重答」会撤销已提交成绩）；每次提交落 `submit_N.json`；记录状态优先于业务码，`submit` 只发一次。平台复测 12/12。 |
| **0.5.4**（2026-11-06） | 交付面收窄为 public（full 版本取消）；抽题候选顺序固定；该版真提交实测暴露「判 0 后自动重答、成绩被撤销」的问题。 |
| **0.5.3**（2026-11-05） | 配置来源解析（R1→R4）：启动第一行打印「读的是哪份配置、安全开关是什么」；当前工作目录不再参与配置查找。 |
| **0.5.2** | 配置查找把工作目录也算进去，从仓库根启动会读到并静默改写仓库里的 `config.toml`；0.5.3 修复并加了来源日志。 |
| **0.5.1** | 声明 Per-Monitor V2 DPI 感知（150% 缩放下文字清晰，未覆盖档位见「已知限制」）。 |
| **0.5.0** | 首个功能完整的公开包：内置「模拟真人打字」、启动自动清理残留、提交前自检与「什么时候不会自动提交」说明。 |
| **0.3.0** | 早期公开包，只保留发布 zip（无变更记录）。 |

## 隐私与安全

- 账号、密码、API Key 只在本机配置文件，**不进 exe、不进仓库、不进日志**；
- 0.6.9 起界面保存的配置默认用 Windows DPAPI 加密（`config.toml.enc`，绑定当前 Windows 用户）：拷到别的机器/用户下打不开；这是防「文件被直接读走」，本机同一个 Windows 用户仍能解密；
- 公开包三个凭据字段必须为空，打包脚本用正则断言；
- 带 `portable.txt` 的目录只读同目录配置，不读写 `%APPDATA%`；把填好凭据的目录转发前请先清空；
- 仓库根的 `config.toml` 被 `.gitignore` 挡住。

## 许可

[MIT](LICENSE)。可自由使用、修改、分发；请保留版权声明。

## 免责

本工具自动化的是**你自己的练习账号**，用于减少重复劳动。平台前端禁止粘贴并对硬编码输出有检测，请自行确认使用方式符合课程要求；**不要在考试或计时测验中使用**。
