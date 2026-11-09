# 实跑记录（2026-10-22）

账号：`2026****2222`（本人账号，已掩位）。全部通过本工具的命令行 / 客户端完成，没有用浏览器。

| 会话 | 题目 | 题型/难度 | 结果 |
|---|---|---|---|
| `34821daafb8f4328b72897e0e8f1fad5` | **Q608** 用 short int 求两个 7 位十进制数之和 | 编程 / 容易 | **✅ 12 / 12（满分）** |
| `3d7b89f75a164ef8bc8b4f7dc306d8f1` | **Q1595** 马克思手稿趣味题（改错题） | 改错 qType=9 / 容易 | 首次 **0/12** → 修复后重答 **✅ 12 / 12** |
| `2b7ec6043914417899d3479ed1d5d37c` | **Q1331** 单链表逆序（含输入/输出提示） | 复杂编程 qType=7 / 容易 | 首次 **0/12** → 修复后重答 **✅ 12 / 12** |
| `ec5ffd624a104cac85891e8f8b8ee352` | **Q173** 手写 MyStrcmp 比较两个字符串 | 编程 / 容易 | ⚠️ 10 / 12（dynamicTestScore 8/10，见下文根因） |

> 演示时默认模型：`DeepSeek-V4.1-Flash`（网关 `http://127.0.0.1:7864/v1`，模型 id `cn:deepseek-v4.1-flash`）。
> 当前默认主模型已改为官方 `deepseek-flash @ https://api.deepseek.com`；本地网关端点已于 2026-11-08 删除。

---

## 一、Q1331：第二次 0 分 —— 推理模型把 token 烧光 + 提示语里的 `\n` 没还原

```
python -m trainsolve run --count 1 --yes
→ Q1331（qType=7）… 自检第 3 轮通过 → 提交 → 得分 0 / 12
```

两层原因：

1. **模型只吐了「内心独白」**：这是推理型模型，思维链也算 completion token，
   `max_tokens=4096` 时它把预算烧在推理上，`content` 里根本没有代码块。
   实测同一题 `max_tokens=16000` 就能正常给出完整代码。
2. **本地自检误判通过**：题面声明的提示语里有转义 ——
   `"\\nPlease enter the linked list (non-number indicates end)\\n"`、`"\\nOriginal list:\\n"`、`"\\n\\nReversed list:\\n"`。
   旧实现只还原了 `\\<`，两条换行丢了，于是「期望输出」少了开头空行和 `Original list:` 前的换行，
   我们的错误程序反而和错误的期望对上了。

平台给出的权威差异（一眼就能看出）：

```
期望: '\nPlease enter …(non-number indicates end)\nNode value:…\nOriginal list:\n   3   6   4   4   3\n\nReversed list:\n   3   4   4   6   3'
实际: 'Please enter …(non-number indicates end)\nNode value:…Original list:\n   3   6   4   4   3\n\nReversed list:\n   3   4   4   6   3'
        ↑ 少了开头 \n                                          ↑ 少了这里 \n
```

修复：

- `deepseek.py`：`max_tokens` 默认 **16000**；`chat()` 识别 `finish_reason=length` 并告警；
  `_ask_for_code()` 在拿到的不是代码时**自动重试**（预算翻倍 + 「不要输出思考，只给 ```c 代码块」）；
- `judge.py`：新增 `decode_escapes()` 还原提示语里的 `\n`/`\t`/`\<\>`；
  新增 `extract_prompt_kinds()` 区分**输入提示**与**输出提示**，输出提示原样写进 stdout（它是补空行的关键）；
  提示语匹配前先判定「这一行是提示语还是用户输入」，开场白型提示不会误吞下一行。

重答后（代码先经本地 gcc + 平台 `run-code` 双验证，与权威期望逐字节一致）：

```
python -m trainsolve redo --session 2b7ec604… --mode reanswer
python -m trainsolve resume --session 2b7ec604… --solver stub --answers out/answers_Q1331.json --yes
→ ✅ 得分 12/12（dynamicTestScore 10/10）
```

---

## 二、Q1595：改错题被当成普通编程题 —— 0/12 → 12/12

演示时模型为 `DeepSeek-V4.1-Flash`（网关 `http://127.0.0.1:7864/v1`，模型 id `cn:deepseek-v4.1-flash`；当时主模型还是本地网关）。

```
python -m trainsolve run --qbase-type 4 --level 2 --count 1 --yes
→ 会话 3d7b89f7…，Q1595，qType=9，自检第 1 轮「未通过（仅编译）」→ 修复 1 轮「通过（仅编译）」→ 提交
→ 得分 0 / 12
```

两个真实缺陷被暴露：

1. **qType=9 是「改错题」**，题面给的是一个有 bug 的程序（全角 `–`、`if (3*x+2*y+z = 50)` 把 `==` 写成 `=`、
   `printf("%3f…")` 用 `%f` 打印 int），而提示词当时按普通编程题处理，模型从零重写，输出格式全错；
2. **样例没还原出来时仍然提交了**：`自检通过（仅编译）` 只代表编译过，不代表对，
   这条记录就这么白拿了 0 分。

对应修复：

- `prompts.py` 增加 `FIX_RULES`：qType 8/9 自动切到「改错模式」——保留原输出格式字符串（`\t`、`%3d` 列宽）、
  只修错、并**修逻辑错误**（循环边界/条件/取值范围；题面说「男人、女人、小孩各有几人」就意味着三者都 ≥1）；
- `RunConfig.require_verified`（默认 True）：**没有通过「编译 + 样例逐字节比对」就跳过提交**，
  命令行要强交得显式加 `--force`；客户端的设置里是「提交前必须通过样例校验」勾选项；
- 单测覆盖：`test_prompts.py::test_fix_bug_question_gets_extra_rules`、
  `test_pipeline_offline.py::RequireVerifiedTest`（无样例 → 不提交；`--force` → 放行）。

修复后按平台返回的**权威期望输出**逐字节对齐（x=1..9、`%3d\t %5d\t %8d`），再重答：

```
python -m trainsolve redo --session 3d7b89… --mode reanswer     # 返回 1，记录重开（state=0，剩余 4765s）
python -m trainsolve resume --session 3d7b89… --solver stub --answers out/answers_Q1595.json --force --yes
→ ✅ 得分 12/12（dynamicTestScore 10/10）
```

顺便确认：**`reanswer`（重新答题）不折半**，折半的是另一个接口 `half-redo`。

修复后让模型重做这道题（只跑模型 + 本地判题，不动平台）：

```
第 1 版（只有语法/格式规则）：修好了全角 – / == / %d，但循环从 x=0 起 → 多输出一行（x=0、x=10）
第 2 版（补上「修逻辑错误」规则）：x、y 从 1 起，且 z>0 → 与平台权威期望逐字节一致 ✅
```

模型最终给出的改错结果：

```c
#include <stdio.h>
int main(void)
{
    int x, y, z;
    printf("Man \t Women \t Childern\n");
    for (x = 1; x < 16; x++)
        for (y = 1; y < 25; y++)
        {
            z = 30 - x - y;
            if (z > 0 && 3 * x + 2 * y + z == 50)
                printf("%3d\t %5d\t %8d\n", x, y, z);
        }
    return 0;
}
```

---

## 三、Q608：满分路径（12/12）

```bash
python -m trainsolve peek --qbase-type 4 --level 2 --tag 3      # 开题、打印题面
# 写代码 -> out/answers_Q608.json
python -m trainsolve resume --session 34821daafb8f4328b72897e0e8f1fad5 \
       --solver stub --answers out/answers_Q608.json --yes
```

题面要求：`short int` 变量、输入格式 `"a = %3hd%4hd, b = %3hd%4hd"`、输出 `"a + b = %d"`。

提交的代码：

```c
#include <stdio.h>

int main(void)
{
    short h1, l1, h2, l2;

    if (scanf("a = %3hd%4hd, b = %3hd%4hd", &h1, &l1, &h2, &l2) != 4) {
        return 1;
    }

    printf("a + b = %d\n", (h1 + h2) * 10000 + (l1 + l2));
    return 0;
}
```

工具做了什么：

1. 从题面还原样例 → `stdin="a = 1234567, b = 7654321\n"`、`expected="a + b = 8888888\n"`
   （这道题的示例不是终端回放，题面直接写明「第一行是输入，第二行是输出」，抽取器识别了这种写法）；
2. 本地 gcc 编译 + 样例逐字节比对 → 通过；
3. 平台 `run-code` 预检：`a = 9999999, b = 1111111 → 11111110`、`a = 0000001, b = 0000001 → 2`，均正确；
4. 暂存（校验码一次通过）→ 交卷 → 轮询记录状态 → 拿到 `12/12`。

平台返回的逐题明细：`score=12.0`、`dynamicTestScore=10.0`、`compareMode="token"`、`directOutputCheat=false`。

---

## 四、Q173：CRLF 踩坑（10/12）

题目：不用 `strcmp`，手写 `MyStrcmp`，用 `gets` 读两个字符串，按返回值输出
`string s>string t.` / `string s<string t.` / `string s=string t.`。

第一版代码在本地和平台预检都通过，正式提交得 **10/12**，平台给出的失败用例：

```
input     : "abcd\r\nabcd"
期望输出  : "Input s\nInput t\nstring s=string t."
实际输出  : "Input s\nInput t\nstring s>string t."
```

**根因**：评测数据行尾是 CRLF。`gets()` 遇到 `'\n'` 停止但会把前面的 `'\r'` 留在缓冲区，
于是 `s="abcd\r"`、`t="abcd"`，比较到第 5 个字符时 `'\r'(13) - '\0'(0) > 0`，
「相等」被误判成「大于」。

**修复**：

```c
gets(s);
s[strcspn(s, "\r\n")] = '\0';
```

用平台 `run-code`（不会产生答题记录）对三个用例复验：

| stdin | 输出 | 结论 |
|---|---|---|
| `abcd\r\nabcd` | `string s=string t.` | ✅ 修复前是 `>` |
| `hello\nworld\n` | `string s<string t.` | ✅ |
| `zebra\napple\n` | `string s>string t.` | ✅ |

这条经验已经固化进提示词第 7 条硬性要求，后续由模型生成的答案不会再踩。

### 为什么没有重做

平台有两个入口：`reanswer`（重新答题）和 `halfRedo`（**重做分数折半**，前端按钮就叫这个）。
当前重置额度 50 次、单次消耗 2。折半会把 10 分砍成 5~6 分，而 `reanswer` 是否影响已有得分平台没写明，
所以本次保留 10/12 的已完成记录。若要冒险冲 12/12：

```bash
python -m trainsolve redo --session ec5ffd624a104cac85891e8f8b8ee352 --mode reanswer
python -m trainsolve resume --session ec5ffd624a104cac85891e8f8b8ee352 \
       --solver stub --answers out/answers_Q173.json --yes
```
（`out/answers_Q173.json` 里已经是修好的版本。）

---

## 五、这次实跑验证到的能力
1. 纯 HTTP 走完 `登录 → 选题 → 取卷 → 暂存 → 交卷 → 轮询评分`，**校验码算法一次命中**；
2. 本地 gcc 判题与平台判题结论一致（Q608 直接拿到满分）；
3. 平台的 `run-code` 可以作为**不产生记录的预检沙箱**，用来验证边界用例、验证修复；
4. 平台评分会返回失败用例的输入/期望/实际，可以原样回灌给模型做 repair——这是自动修复闭环的做法
   （0.5.5 起默认关闭，因为重答会撤销刚才的提交、清空成绩）；
5. 三种题面样例写法都能被抽取器还原：
   - 单提示语不换行（Q3373：`Please enter the year:2016`）
   - 多提示语各自换行（Q173：`Input s` / `Input t`）
   - 题面直接声明「第一行是输入」（Q608）
6. 评分是异步队列：`score-result` 可能长时间为空，工具改为轮询记录的 `state`，Q608 一次轮询成功。

---

## 六、现在的自动化闭环（这几次事故之后收敛出来的）

上面三条 0 分/低分记录都是靠**手动 redo + 修代码**救回来的；同样的动作已经自动化进 runner：

```
读题干 → 写代码 → 题面样例自测（gcc / run-code，逐字节比对）
      → 不过就回灌 diff 让模型改（max_repairs，默认 3）
      → 提交 → 轮询判分
      → 未满分？把平台给的「失败用例（输入/期望/实际）」或编译错误回灌 → 改 → reanswer → 再提交
      → 这一环**0.5.5 起默认关闭**（auto_fix_from_judge=false）；开启后每次重答会撤销提交、清空成绩
```

对应开关：`--max-repairs`、`--max-reanswers`、`--no-auto-fix`、`--strict-verify`；
开启后重答只能改配置 `auto_fix_from_judge = true`；客户端「设置」里能改修复轮数与重答上限。

---

## 七、全自动连续刷题实跑（2026-10-22 19:22–19:32）

```
python -m trainsolve run --auto --yes
```

结果：**连续 10 题全部满分，累计 118/118**，中途零人工干预。

| 序号 | 题号 | 得分 | 过程亮点 |
|---|---|---|---|
| 1 | Q3726 | 12/12 | 本地自检 4 轮不过，直接提交后平台判 12/12（本地样例还原有偏差，但代码是对的） |
| 2 | Q2219 | 12/12 | 仅编译通过即提交，一次过 |
| 3 | Q3693 | 12/12 | 同上 |
| 4 | Q5870 | 11/11 | qType=7 复杂编程 |
| 5 | Q3156 | 12/12 | 自检 1 轮失败 → 回灌 diff → 第 2 轮通过 → 提交满分 |
| 6 | Q576 | — | 模型思维链把 32000 token 烧光，两个端点都失败 → **自动清理会话**，随后自动抽下一题继续 |
| 6' | Q2694 | 12/12 | 中间抽到 qType=9 改错题 → **白名单自动 skip** → 重抽 |
| 7 | Q5967 | 12/12 | 首交 0/12 → **平台失败用例回灌修复 → reanswer → 重交 12/12**（重答 1 次） |
| 8 | Q534 | 12/12 | — |
| 9 | Q4197 | 11/11 | — |
| 10 | Q2243 | 12/12 | — |

这轮顺带验证/修掉的问题：

1. **主备模型切换**：当时主模型（本地网关 `cn:deepseek-v4.1-flash`）报 503 `no_healthy_account`，
   自动切到官方 `deepseek-flash` 并“粘住”，全程没再撞网关；开跑前的健康检查也直接选中了备用。
2. **模型故障不能算答错**：Q576 两个端点都被截断，工具清理会话、记一次模型故障，然后**继续下一题**
   （连续 2 次才熔断）。
3. **多示例题解析**：`程序运行示例1/2/3：` 只取第一个，序号被剥掉；`输出提示：` 现在会被当作停止标记。
4. **后台运行时输出被块缓冲**：加 `line_buffering=True`，重定向到日志也能实时看到进度。
5. **“样例抽不出来就不提交”会卡死全自动**：默认改成照常提交（错了由平台反馈驱动修复），
   保守策略保留为 `--strict-verify`。
