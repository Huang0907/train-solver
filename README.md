# train-solver

程序设计训练平台（`http://172.22.181.82/train/`）的自动刷题客户端：**选题 → 解题 → 本地自检 → 提交 → 等判分 → 按报错修改**，全流程可无人值守。桌面版开箱即用，命令行版可脚本化。

- 当前版本：**0.5.7**（2026-11-08）
- 发布包：[GitHub Releases](https://github.com/wsjkx/train-solver/releases)
- 源码仓库：[wsjkx/train-solver](https://github.com/wsjkx/train-solver)
- Python：3.11+，核心零第三方依赖

> **先读这一条**：默认配置是**只解题、不提交**（`dry_run = true` / `submit = false`）。真提交会消耗平台上有限的提交机会，第一次务必先试运行看清楚，再自己改为真提交。
>
> 0.5.4 曾在真平台暴露「提交被判 0/12 后自动重答、记录被重新打开、成绩被撤销」；0.5.5 已修复（默认关闭自动重答、以记录状态为准）。**2026-11-08 已用可弃会话在真平台复测通过**：submit 只发一次、记录停在 `state=1` / 12/12、未报「提交状态未知」。

---

## 目录

1. [它能做什么](#1-它能做什么)
2. [快速开始](#2-快速开始)
3. [模型配置](#3-模型配置)
4. [桌面客户端](#4-桌面客户端)
5. [命令行](#5-命令行)
6. [内部流程](#6-内部流程)
7. [产物目录](#7-产物目录)
8. [配置参考](#8-配置参考)
9. [返回码](#9-返回码)
10. [已知限制](#10-已知限制)
11. [测试](#11-测试)
12. [打包与发布](#12-打包与发布)
13. [模拟真人打字](#13-模拟真人打字)
14. [目录结构](#14-目录结构)
15. [数据与隐私](#15-数据与隐私)
16. [变更记录](#16-变更记录)
17. [免责声明](#17-免责声明)

---

## 1. 它能做什么

| 能力 | 说明 |
|---|---|
| 自动刷题 | 选题 → 生成代码 → 自检 → 暂存 → 提交 → 轮询判分，一题跑完自动下一题 |
| 本地自检 | 从题面还原「输入 / 期望输出」，用 gcc 或平台 `run-code` 编译运行，逐字节比对 |
| 有错就修 | 自检不通过时把 diff 回灌模型，最多 `max_repairs` 轮；提交后未满分可选按平台报错重答（默认关） |
| 题型白名单 | 默认只做编程类（qType 5/6/7），改错题、程序填空、设计题自动跳过 |
| 主备模型 | 支持多个模型端点，503 / 超时 / 鉴权失败自动切换（默认只有一个官方端点，可自行加备用） |
| 模拟真人打字 | 可选：作答改为在真实 Chrome 里逐字符键入并真实点击提交，取题 / 自检 / 评分仍走 API |
| 安全默认 | 公开包默认试运行；账号、密码、API Key 不进 exe、不进仓库 |
| 全自动熔断 | 连续未满分 / 连续跳过 / 连续模型故障都有上限，不会无脑重试 |

---

## 2. 快速开始

### 2.1 用发布包（推荐）

1. 到 [Releases](https://github.com/wsjkx/train-solver/releases) 下载最新 `TrainSolveClient-<版本>-public.zip`；
2. 解压到任意目录，得到四个文件：

   ```
   TrainSolveClient.exe   主程序（已内置 Playwright，模拟打字开箱即用）
   config.toml            配置（账号 / 密码 / API Key 全空）
   portable.txt           便携标记，程序只读同目录配置
   使用说明.md            同版本说明
   ```

3. 双击 exe，在顶部填平台学号 / 密码，在「更换模型…」里填 DeepSeek API Key，保存；
4. 先用默认的**试运行**跑一道：只出题、生成答案、落盘，不提交；
5. 确认题目和答案没问题后，把同目录 `config.toml` 改成：

   ```toml
   [run]
   submit = true
   dry_run = false
   ```

   重启客户端即为真提交。**每次真提交都消耗一次提交机会。**

> 启动日志会打印实际生效的配置路径，例如
> `配置：D:\...\config.toml（source=explicit；submit=False dry_run=True）`，
> 先看这一行确认自己用的哪份配置。

### 2.2 用源码

```bash
python -m trainsolve gui                # 启动桌面客户端
python -m trainsolve --help             # 查看全部命令
python -m trainsolve login              # 登录并缓存 sid
python -m trainsolve run --count 1      # 刷一道（默认试运行由配置决定）
```

`config.toml` 的查找顺序：

1. 目录里有 `portable.txt` → **只认**同目录 `config.toml`（发布包都带这个，不会去读 `%APPDATA%`）；
2. 否则 exe 同目录 / 当前目录有 `config.toml` → 用它；
3. 都没有 → `%APPDATA%\TrainSolve\config.toml`（开发运行时）。

把配置放到仓库根的 `config.toml`，即被 `.gitignore` 挡住，不会被提交。

---

## 3. 模型配置

默认走 **DeepSeek 官方 API**：

```toml
[deepseek]
api_key = "sk-..."                       # 官方 Key，sk- 开头
base_url = "https://api.deepseek.com"
model = "deepseek-flash"
temperature = 0.2
max_tokens = 16000
timeout = 180
retries = 3
```

官方只认 `deepseek-flash` 与 `deepseek-v4-pro` 两个模型 id；写 `deepseek-v4.1-flash` 会被 HTTP 400 拒绝（那是本地网关那种 `cn:` 前缀的私有 id）。

三种填法，优先级：命令行 > 环境变量 > 配置文件。

```bash
# A. 命令行
python -m trainsolve run --api-key sk-xxx --model deepseek-flash

# B. 环境变量（Windows 永久生效）
setx DEEPSEEK_API_KEY sk-xxx
setx DEEPSEEK_BASE_URL https://api.deepseek.com
setx DEEPSEEK_MODEL deepseek-flash

# C. config.toml（见上）
```

**备用端点（可选）**：默认只有主模型；在客户端「更换模型…」点「新增备用」，会写回 `[[deepseek.fallback]]`：

```toml
[[deepseek.fallback]]
api_key = "sk-..."
base_url = "https://api.deepseek.com"
model = "deepseek-v4-pro"
label = "备用"
```

有备用时：开跑前逐个探活选第一个可用的，运行中某个端点报 503 / 超时 / 鉴权失败会自动切过去。**没有备用时，探活失败就直接退出**，不会空开会话。

查 / 测端点：

```bash
python -m trainsolve models           # 列出主 / 备端点
python -m trainsolve models --check   # 对每个端点发一次最小请求验证
```

---

## 4. 桌面客户端

| 区域 | 内容 |
|---|---|
| 顶部 | 平台地址、学号、密码、登录、设置、连接状态 |
| 左侧「选题」 | 题型、难度、标签（可刷新）、数量、判题方式 |
| 左侧按钮 | 刷新标签、开始自动刷题、全自动连续刷、只看题、停止 |
| 左侧「人工模式」 | 本地自检我的代码、提交我的代码 |
| 右侧页签 | 日志、题目、代码（可编辑）、评分 |
| 底部 | 状态栏 + 进度条 |

- 网络 / 模型调用都在后台线程，UI 用队列收消息；点「停止」不会卡界面，当前题目做完才停；
- 关窗会取消所有待触发的定时器并请求停止，不会在窗口销毁后继续跑；
- 配置保存在 `%APPDATA%\TrainSolve\config.toml`（便携模式下只在 exe 同目录）；
- 保存设置、登录、换模型是三个会写配置的触发点，其余启动不会重写配置。

---

## 5. 命令行

| 命令 | 作用 |
|---|---|
| `gui` | 启动桌面客户端 |
| `login` | 登录并缓存 sid |
| `tags --qbase-type N` | 列出该题型的标签与可用性 |
| `run` | 主流程：选题 → 解题 → 自检 → 提交 → 评分 → 落盘 |
| `peek` | 只开一个会话把题目打出来（不解题、不提交）；带 `--session` 则只读 |
| `resume --session <id> --answers <file>` | 对已有会话续做并提交（人工写好代码走这条） |
| `redo --session <id> --mode reanswer\|half\|rescore` | 重新答题 / 折半重做 / 重新计分（消耗重置额度） |
| `cleanup [--session <id>] [--dry-run]` | 清理「未完成」会话（skip + cancel，真删记录） |
| `show --session <id>` | 查看该会话的产物与得分 |
| `prompt [record.json]` | 把记录渲染成提示词（调试用） |
| `models [--check]` | 列出 / 测试模型端点 |

### `run` 常用开关

| 开关 | 作用 |
|---|---|
| `--count N` / `--auto` | 做 N 道 / 一直做（`--count 0` 等同 `--auto`） |
| `--qbase-type 4 --level 2` | 题型（0 单选 1 多选 2 判断 3 填空 4 编程）、难度（0 随机 1 初学者 2 容易 3 中等 4 困难 5 极难） |
| `--tag -1` | 标签，`-1` 表示平台随机题；其它值为标签 id |
| `--qtypes 5,6,7` | 题型白名单，默认只做编程类；`all` 不限制 |
| `--judge local\|server\|both\|none` | 本地 gcc / 平台编译 / 两者 / 不判 |
| `--max-repairs N` | 自检失败时回灌修复的轮数（默认 3） |
| `--max-reanswers N` / `--no-auto-fix` | 提交后未满分时重答的上限 / 直接关闭重答 |
| `--dry-run` | 只出题 + 生成答案 + 落盘，不暂存、不提交 |
| `--no-submit` | 自检通过后只落盘，不提交 |
| `--yes` | 跳过提交前人工确认 |
| `--force` | 自检明确未通过（编译不过 / 样例对不上）也强制提交；默认不提交 |
| `--strict-verify` | 更严格：样例没还原出来也不提交 |
| `--max-failures N` | 全自动下连续未满分的熔断阈值（默认 3） |

> `--dry-run` 与 `--no-submit` 都会把会话留在「未完成」状态；`choose-q` 一旦成功就产生一条记录。收尾时用 `cleanup` 清掉，否则记录到期会变成 0 分。

---

## 6. 内部流程

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

两个从平台前端逆出来的细节：

1. **`verificationCode`** = `md5("%Y-%m-%d %H:%M " + sessionId)`（本地时间，分钟粒度）；缺失或算错返回 `code=8`。工具会尝试 ±1..±8 小时偏移自动校准时区。
2. **判题是逐字节比对**（`compareMode` 有 `exact` / `token` 两种），并带 `directOutputCheat` 硬编码检测；所以提示词要求真实算法、输出逐字一致，工具不会去调参考答案接口取巧。

提交语义（0.5.5 起）：`submit` 的原始返回与业务码都落盘；认不出的码按「无法确认」处理，不伪装成功。业务码说成功、记录却回到「未完成」时，一律报**「提交状态未知：…提交被撤销」**并停止后续提交——以记录状态为准，不以业务码自我安慰。

---

## 7. 产物目录

```
out/<sessionId>/
├─ statement.md                     题目正文 + 选项
├─ record.json                      原始取卷响应
├─ Q<id>.c                          提交的源码（客观题为 Q<id>.txt）
├─ answerPaper.json                 实际提交的答案结构
├─ score_<N>.json                   第 N 次提交的逐题得分 / 失败用例 / 编译输出
├─ record_after.json                提交后的记录
├─ summary.json                     汇总（含 submit 业务码）
├─ record_before_submit_<N>.json    第 N 次提交前的记录快照
├─ score_before_submit_<N>.json     第 N 次提交前的分数快照
├─ submit_<N>.json                  第 N 次提交的原始返回 + 解析出的业务码
├─ record_before_reanswer_<N>.json  第 N 次重答前的记录快照
├─ score_before_reanswer_<N>.json   第 N 次重答前的分数快照
├─ score_after_reanswer_<N>.json    第 N 次重答后重新拉取的分数
└─ reanswer_<N>.json                重答结果（code + state_before + state_after）
out/session.json                    登录会话缓存
```

`submit_<N>.json` 的 `{attempt, code, raw}` 是排查提交问题的一手证据；`attempt` 从 0 开始计。

---

## 8. 配置参考

三节：`[platform]`、`[deepseek]`、`[run]`。完整示例见 `config.example.toml`，公开版随包模板见 `config.public.toml`。

### `[platform]`

| 键 | 默认 | 说明 |
|---|---|---|
| `base_url` | `http://172.22.181.82/train/` | 平台地址 |
| `username` / `password` | 空 | 平台学号 / 密码 |
| `timeout` | `25.0` | 平台接口超时（秒） |

### `[deepseek]`

| 键 | 默认 | 说明 |
|---|---|---|
| `api_key` | 空 | 模型 Key（官方 `sk-` 开头） |
| `base_url` | `https://api.deepseek.com` | 接口地址 |
| `model` | `deepseek-flash` | 模型 id（官方仅此与 `deepseek-v4-pro`） |
| `temperature` / `max_tokens` | `0.2` / `16000` | 生成参数；推理模型思维链也占 completion 预算 |
| `timeout` / `retries` | `180` / `3` | 单次请求超时与重试 |
| `preferred` | `0` | 当前选中的端点（0=主模型） |

### `[run]` 关键项

| 键 | 默认 | 说明 |
|---|---|---|
| `qbase_type` / `level` / `tag_id` | `4` / `2` / `-1` | 题型 / 难度 / 标签（-1 随机） |
| `count` | `1` | 做几道；`0` = 全自动 |
| `judge` | `local` | 判题方式 |
| `submit` / `dry_run` | `true` / `false` | **公开版被改成 `false` / `true`** |
| `confirm` | `true` | 提交前是否人工确认 |
| `qtypes` | `[5,6,7]` | 题型白名单；空列表 = 不限制 |
| `max_repairs` | `3` | 自检失败回灌轮数 |
| `auto_fix_from_judge` | `false` | 提交后未满分是否自动重答（**会撤销刚提交的成绩**） |
| `max_reanswers` | `5` | 重答上限 |
| `allow_unverified_submit` | `false` | 自检明确未通过也提交（不推荐） |
| `require_verified` | `false` | 更严：样例没还原也不提交 |
| `cleanup_abandoned` | `true` | 放弃的会话用 skip+cancel 删记录 |
| `auto_cleanup` | `true` | 启动时清理已过期「未完成」会话与超龄 `%TEMP%\_MEI*` |
| `max_consecutive_failures` / `_skips` / `_infra_errors` | `3` / `5` / `2` | 全自动熔断阈值 |

---

## 9. 返回码

- `choose-q`：`0` 成功 / `1` 暂无新题 / `2` 本周已完成
- `post-answer`：`0` 成功 / `1` 记录不存在 / `2` 已提交 / `3` 试卷不存在 / `4` 学生不存在 / `5` 心跳超时 / `6` 已被购买 / `7` 超时 / `8` 校验码错误 / `9` 截止已过 / `10` 未开始
- 记录 `state`：`0` 未完成 / `1` 已完成 / `2` 评分中 / `3` 已跳过 / `4` 稍后做 / `5` 失败

`reanswer` / `half-redo` / `rescore` 返回的是**业务码**（`0` 成功、`1` 拒绝），不是布尔；记录不在「已完成」状态时 `reanswer` 一律返回 1。`half-redo` 会消耗 2 次重置额度，并按当前暂存内容重新评分。

---

## 10. 已知限制

| 限制 | 说明 |
|---|---|
| 真提交链路 | 0.5.4 出过事，0.5.5 已修，**2026-11-08 真平台复测通过**；第一次用仍建议先试运行一轮 |
| 随机题可能抽到白名单外 | 标签选「随机（-1）」时平台可能返回 qType 之外的题，工具会 `skip-this` 跳过；代价是一次抽题配额 + 约 60 秒限流等待。想要稳就选具体标签 |
| 评测数据是 CRLF | `gets()` / `fgets()` 会把行尾 `\r` 读进来导致误判；提示词已强制要求 `s[strcspn(s,"\r\n")]='\0'` |
| `gets` 已废弃 | 平台编译器只给 warning；将来收紧标准需要改 `fgets` |
| 客观题是 beta | 答案写进 `answerPaper.q0..q3`，选项编码按实际字段探测；建议先 `--qbase-type 0 --count 1 --dry-run` 试一次 |
| 改错题 qType=8/9 | 默认被白名单挡在门外；要做得 `--qtypes all`，提示词会切到「改错模式」 |
| 设计题 qType=10 | 不支持，会跳过 |
| 无样例 → 默认照交 | 题面没给运行示例时无法本地校验，默认仍提交，由平台判分反馈驱动修复；想保守用 `--strict-verify` |
| 自检未通过 → 默认不交 | 交了也是 0 分还占机会；确实要交加 `--force` |
| 模型故障 | 有备用则自动切换；全部不可用时不创建会话直接退出；出故障的会话用 skip+cancel 清掉 |
| 评分在队列里 | `score-result` 可能长时间为空，工具轮询记录 `state`，默认等 600s |
| 高 DPI | 只实测了 150% 一档；100% / 125% / 175% 与多屏不同缩放未实测 |
| 本周限额 | `choose-q` 返回 `2` 表示本周题量上限，工具提示并退出（退出码 3） |

---

## 11. 测试

全量离线测试 **328 条**，不需要平台账号与网络：

```bash
python -m unittest discover -s tests
```

覆盖：校验码时区、样例还原（四种写法）与逐字节比对、提示词组装与改错模式、推理模型空回复重试、`--qtypes` 解析、主备端点排序与 failover、清理逻辑、离线管线（取卷 → 组答案 → 提交 → 解析评分，含 dry-run 不提交 / 自检未过不提交 / 白名单跳过 / 重答停止条件 / 全自动熔断）、GUI 构造与「更换模型」往返、模拟打字离线用例、打包脚本与版本一致性、只读冒烟 `--self-test` 子进程回归。

`fixtures/` 里是真实接口响应，可离线复现；实跑记录见 [`docs/DEMO.md`](docs/DEMO.md)。

---

## 12. 打包与发布

```powershell
powershell -ExecutionPolicy Bypass -File build_release.ps1
```

依次执行：跑测试 → 按 `trainsolve.__version__` 生成 Windows 版本资源（exe 属性里的 FileVersion / ProductVersion 由此而来，脚本不手写版本号）→ PyInstaller 打 onefile exe（内置 Playwright driver）→ **解包后逐模块扫描凭据**（PYZ 是压缩的，扫原始字节会假阴性）→ 校验版本资源 → 组装公开版目录 → 压缩 → 再扫一遍公开版目录（三个凭据字段必须为空）。

- 只产出 **public** 版：`release\TrainSolveClient-<版本>-public\`（目录 + zip）。`release\` 里一旦出现其它版本形态，脚本在打包前直接报错退出。
- 公开版内容：`TrainSolveClient.exe`、`config.toml`（账号 / 密码 / Key 全空）、`portable.txt`、`使用说明.md`。
- onefile 冷启动 4–5 秒（要解压约 108 MB 到 `%TEMP%\_MEI*`）；强杀进程可能留下临时目录，正常退出会清理。

---

## 13. 模拟真人打字

平台前端会记录 `keySequence` / `inputSequence` 并禁止粘贴，直接调接口交答案在按键轨迹上是可分辨的。本功能把**作答**这一步换成真实 Chrome 里逐字符键入 + 真实点击提交，其余仍走 API。

- 桌面版：左侧勾选「模拟真人打字提交」即可（driver 已内置，无需安装）；
- 源码运行：`pip install playwright`，Chrome 用 `--remote-debugging-port=9333` 启动；没开会自动拉起独立 profile；
- 第一次建议 `--human-dry-run`（只打字、回读校验，不点提交）。

```powershell
python -m trainsolve run --count 1 --human --cpm 220
python -m trainsolve run --count 1 --human --cpm-min 180 --cpm-max 400
python -m trainsolve resume --session <id> --human --cpm 400
```

关键实现：每个字符一条原生按键（`Input.dispatchKeyEvent`，显式 `code`/`key`/虚键码，Shift 符号单独按下）；逐行回读对齐缩进；回读与按键共用同一 CDP 会话且「连续两次一致」才采信；提交前必须等到 `post-answer` 的 **HTTP 2xx + 业务 code=0 回执**（只等请求体不够：平台组件状态在响应回调里才更新，实测因此少交 6 个字符 / 交过旧前缀，判 0/12）。

限制：只覆盖编程题；50 行约 1.5–3 分钟（倒计时仍在走）；依赖平台前端结构（选择器集中在 `trainsolve/human.py` 顶部常量）；需要真实 Chrome。

更细的评估与按键机制分析见 [`docs/评估-模拟人类输入.md`](docs/评估-模拟人类输入.md)。

---

## 14. 目录结构

```
train-solver/
├─ dist/TrainSolveClient.exe        开发用 exe
├─ release/                         发布产物（只放公开版）
├─ build_release.ps1                一条命令产出公开版
├─ build_client.ps1                 只打开发用 exe
├─ config.public.toml               公开版随包模板（空账号、空 Key）
├─ config.example.toml              带注释的完整配置示例
├─ client_main.py                   GUI 入口
├─ docs/
│  ├─ 使用说明-公开版.md            随包说明
│  ├─ DEMO.md                       真实跑题记录与踩坑
│  ├─ 评估-模拟人类输入.md
│  └─ windows-automation-notes.md   Windows 自动化笔记
├─ trainsolve/
│  ├─ gui.py                        桌面客户端（tkinter）
│  ├─ cli.py                        命令行
│  ├─ runner.py                     选题 → 解题 → 自检 → 提交 → 重答编排
│  ├─ api.py                        平台接口
│  ├─ judge.py                      样例还原 + gcc 比对
│  ├─ verify.py                     verificationCode 校验
│  ├─ prompts.py                    提示词
│  ├─ human.py / humantype.py       浏览器作答 / 打字核心
│  ├─ leftovers.py                  残留会话清理
│  ├─ config.py                     配置模型与加载
│  └─ solver/                       base / deepseek / failover / stub / factory
├─ fixtures/                        真实响应样本
└─ tests/                           328 条离线测试
```

---

## 15. 数据与隐私

- 账号、密码、API Key 只存在于本机配置文件，**不进 exe、不进仓库、不进日志**（日志里 Key 永远打码）；
- 公开包三个凭据字段必须为空，打包脚本用正则断言，非空直接中止；
- 带 `portable.txt` 的目录只读同目录 `config.toml`，不会读取你机器上 `%APPDATA%` 的私有配置；把填好凭据的目录转发给别人前请先清空配置；
- 仓库根的 `config.toml` 被 `.gitignore` 挡住，不会随源码提交。

---

## 16. 变更记录

| 版本 | 日期 | 内容 |
|---|---|---|
| 0.5.7 | 2026-11-08 | `runner.py` 行尾统一为 LF（纯格式）；文档同步真提交复测结论与官方主模型；删除仓库内带学号的界面截图 |
| 0.5.6 | 2026-11-08 | 关窗取消全部 `after` 定时器并请求停止；`smoke_readonly.py --self-test` 挂进回归测试 |
| 0.5.5 | 2026-11-06 | 提交链路修复：接住 `submit` 返回值、以记录状态优先于业务码、默认关闭自动重答、提交/重答前后落盘快照 |
| 0.5.4 | 2026-11-06 | 真提交实测暴露「提交被判 0/12 后自动重答、记录被重开、成绩撤销」，成为 0.5.5 的修复动机 |
| 0.5.0–0.5.3 | 2026-11 | 逐步完善：主备模型、自动重答、公开版打包、自检闸门、清理与熔断 |
| 0.3.0 | 2026-10-22 | 早期版本 |

---

## 17. 免责声明

本工具自动化的是**你自己的练习账号**，用于把「选题—写代码—自检—提交」的重复劳动交给脚本。平台前端禁止粘贴代码，并对硬编码输出有检测；请自行确认使用方式符合课程要求，**不要在考试或计时测验中使用**。提交机会有限，真提交前请先用试运行确认。

---

## 18. 许可

MIT License，见 [LICENSE](LICENSE)。
