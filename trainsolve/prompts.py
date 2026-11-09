"""提示词组装与模型输出解析。

设计要点
--------
1. 平台判题是 **stdout 逐字节比对**（比对模式 `exact`），并带 `directOutputCheat` 硬编码检测，
   所以提示词必须强调：真实计算、格式逐字一致、不得把样例输出直接 printf。
2. 输出解析要容忍模型加解释、加 ``` 围栏、加前后空行。
"""

from __future__ import annotations

import json
import re

SYSTEM_PROGRAMMER = (
    "你是一名资深的 C 语言教学助教，擅长把中文题目写成能通过严格输出比对的 C99 程序。"
    "你的答案只会被程序化提取，因此除代码块外不要输出任何内容。"
)

PROGRAMMING_RULES = """\
【硬性要求】
1. 只输出一个 ```c 代码块，代码块之外不要有任何文字、解释或编号。
2. 语言为 C99，只允许 #include <stdio.h> / <stdlib.h> / <string.h> / <math.h> / <ctype.h>。
3. 程序从 stdin 读入，向 stdout 输出，不读写任何文件，不使用系统调用。
4. 输出格式必须与题目「程序运行结果示例」逐字节一致：
   - 分隔符、大小写、空格、逗号（半角 "," 还是全角 "，"）都要按示例来；
   - 题目要求「输入提示」时，提示语必须原样输出且**不要额外加换行**，紧随其后输出数据；
   - 除非示例里明确有空行，否则不要输出多余空行；
   - 除最后一行外每行都要有换行符，最后一行建议也带换行（评测一般会忽略行尾差异）。
5. 严禁把示例输出直接 printf 成常量（平台有 directOutputCheat 硬编码检测，会被判作弊），
   必须写通用算法，能处理任意合法输入。
6. 不要输出中文提示之外的任何调试信息（如 "result:"、变量值等）。
7. 评测数据的行尾可能是 Windows 的 "\\r\\n"。若用 gets()/fgets() 读入整行，
   必须在比较或输出前剔除行尾残留的 '\\r'（例如 gets(s); s[strcspn(s, "\\r\\n")] = '\\0';），
   否则 "abc" 与 "abc\\r" 会被误判为不相等。"""

FIX_RULES = """\
【本题是「改错题」】
题面会给出一个**包含错误的程序**，请改正它并输出完整程序。常见错误：
- 全角符号混入代码（如把减号写成 "–"、逗号写成 "，"、分号写成 "；"）；
- 把比较写成赋值（`if (a + b = 50)` 应为 `==`）；
- 格式说明符与参数类型不匹配（如用 `%f` 打印 int，应改 `%d`），或列宽不对；
- 缺少 return 类型 / 缺少分号 / 缺少 #include <stdio.h>；
- 循环边界与示例输出不符（多输出或少输出一行）。

改错要求：
1. **保留题面原有的输出格式字符串**（制表符 `\\t`、列宽如 `%3d`、表头文字与空格一个都不能改）；
2. 只修错，不要重写成另一种算法或换掉输出排版；
3. **除了语法/类型错误，还要修逻辑错误**：循环边界、条件判断、变量取值范围。
   题面里的隐含约束必须体现在代码里，例如「男人、女人、小孩各有几人」意味着三者都 ≥ 1，
   必须排除某一类为 0 的解（循环起点从 1 开始，或加 `x > 0 && y > 0 && z > 0` 判断）；
4. 若题面给了示例输出，严格照它决定输出行数——从第几行开始、到第几行结束就只输出这些行；
5. 仍然只输出一个 ```c 代码块，不要解释。"""


def _statement(question: dict) -> str:
    content = question.get("content") or question.get("contentEn") or ""
    return str(content).strip()


def _quantity(question: dict) -> str:
    point = question.get("point")
    return f"（本题 {point:g} 分）" if isinstance(point, (int, float)) else ""


def build_programming_messages(question: dict, *, english: bool = False) -> list[dict]:
    qid = question.get("id")
    qtype = int(question.get("qType") or 5)
    language = question.get("language")
    lang_name = {1: "C", 2: "C++", 3: "Java", 4: "Python"}.get(int(language or 1), f"language={language}")
    header = f"题目 Q{qid}{_quantity(question)}，语言：{lang_name}。"
    body = _statement(question)
    if english and question.get("contentEn"):
        body = str(question["contentEn"]).strip()
    rules = PROGRAMMING_RULES
    if qtype in (8, 9):
        rules = FIX_RULES + "\n\n" + PROGRAMMING_RULES
    return [
        {"role": "system", "content": SYSTEM_PROGRAMMER},
        {"role": "user", "content": f"{header}\n\n{body}\n\n{rules}"},
    ]


def build_repair_messages(question: dict, code: str, feedback: str) -> list[dict]:
    body = _statement(question)
    extra = FIX_RULES + "\n\n" if int(question.get("qType") or 5) in (8, 9) else ""
    return [
        {"role": "system", "content": SYSTEM_PROGRAMMER},
        {
            "role": "user",
            "content": (
                f"题目 Q{question.get('id')}：\n\n{body}\n\n"
                f"下面是你上一版代码：\n```c\n{code}\n```\n\n"
                f"判题反馈：\n{feedback}\n\n"
                f"{extra}"
                "请修正后重新输出完整程序。仍然只输出一个 ```c 代码块，不要解释。"
            ),
        },
    ]


def build_objective_messages(question: dict, options: list[dict]) -> list[dict]:
    qtype = int(question.get("qType") or 0)
    kind = {0: "单选题", 1: "多选题", 2: "判断题", 3: "填空题", 4: "程序填空题"}.get(qtype, "客观题")
    rendered = []
    for index, option in enumerate(options):
        letter = chr(ord("A") + index)
        text = option.get("content") or option.get("text") or option.get("label") or option.get("title") or ""
        rendered.append(f"{letter}. {text}")
    options_block = "\n".join(rendered) if rendered else "（本题无可选项，答案为一串文本）"

    if qtype == 2:
        want = '{"answer": "A"}  # A=正确/对，B=错误/错'
    elif qtype == 1:
        want = '{"answer": "AC"}' + "  # 多选，按字母升序拼接"
    elif qtype in (0,):
        want = '{"answer": "B"}' + "  # 单选，只填一个字母"
    else:
        want = '{"answer": "填空内容"}  # 多个空用 ||| 分隔'

    return [
        {
            "role": "system",
            "content": "你是严谨的 C 语言课程助教，只输出一个 JSON 对象，不要解释。",
        },
        {
            "role": "user",
            "content": (
                f"{kind} Q{question.get('id')}{_quantity(question)}：\n\n"
                f"{_statement(question)}\n\n选项：\n{options_block}\n\n"
                f"请作答，输出格式：{want}"
            ),
        },
    ]


_FENCE = re.compile(r"```(?:[a-zA-Z0-9_+-]*)\s*\n(.*?)```", re.DOTALL)


def extract_code_block(text: str) -> str:
    """从模型输出里提取 C 代码。"""
    if not text:
        return ""
    blocks = _FENCE.findall(text)
    if blocks:
        # 取第一个包含 #include 的块，否则取最长的块
        for block in blocks:
            if "#include" in block:
                return block.strip("\n") + "\n"
        return max(blocks, key=len).strip("\n") + "\n"
    stripped = text.strip()
    if "#include" in stripped:
        return stripped + "\n"
    return ""


def extract_json_object(text: str) -> dict:
    """从模型输出里提取第一个 JSON 对象。"""
    if not text:
        return {}
    fenced = _FENCE.findall(text)
    candidates = fenced + [text]
    for candidate in candidates:
        candidate = candidate.strip()
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            try:
                value = json.loads(candidate[start : end + 1])
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
    return {}
