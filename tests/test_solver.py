"""Solver 层：模型输出解析失败时的兜底（不联网，用假客户端）。"""

import unittest

from trainsolve.solver.base import SolverError
from trainsolve.solver.deepseek import DeepSeekSolver, _looks_like_c

GOOD = "```c\n#include <stdio.h>\nint main(void)\n{\n    printf(\"hi\\n\");\n    return 0;\n}\n```"
REASONING_ONLY = (
    "我们需要回答用户。需要只输出 c 代码块。需要实现题目。" * 20
)


class FakeChatClient:
    """按顺序返回预先准备的回复，并记录每次调用的 max_tokens。"""

    def __init__(self, replies, max_tokens=16000):
        self.replies = list(replies)
        self.max_tokens = max_tokens
        self.calls = []

    def chat(self, messages, *, temperature=None, max_tokens=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "temperature": temperature})
        return self.replies.pop(0)


class LooksLikeCTest(unittest.TestCase):
    def test_code(self):
        self.assertTrue(_looks_like_c("#include <stdio.h>\nint main(void){return 0;}"))

    def test_reasoning(self):
        self.assertFalse(_looks_like_c(REASONING_ONLY))

    def test_empty(self):
        self.assertFalse(_looks_like_c(""))


class AskForCodeTest(unittest.TestCase):
    QUESTION = {"id": 1331, "qType": 7, "content": "反转单链表", "language": 1}

    def test_good_first_answer(self):
        client = FakeChatClient([GOOD])
        answer = DeepSeekSolver(client, log=lambda *_: None).solve(self.QUESTION)
        self.assertIn("int main", answer.text)
        self.assertEqual(len(client.calls), 1)

    def test_reasoning_only_triggers_retry_with_double_budget(self):
        client = FakeChatClient([REASONING_ONLY, GOOD])
        answer = DeepSeekSolver(client, log=lambda *_: None).solve(self.QUESTION)
        self.assertIn("int main", answer.text)
        self.assertEqual(len(client.calls), 2)
        # 第二次调用应加倍预算，并带上「只输出代码」的提示
        self.assertEqual(client.calls[1]["max_tokens"], 32000)
        self.assertEqual(client.calls[1]["temperature"], 0)
        last_user = client.calls[1]["messages"][-1]["content"]
        self.assertIn("不要输出任何思考", last_user)

    def test_both_attempts_fail(self):
        client = FakeChatClient([REASONING_ONLY, REASONING_ONLY])
        with self.assertRaises(SolverError) as ctx:
            DeepSeekSolver(client, log=lambda *_: None).solve(self.QUESTION)
        self.assertIn("思维链", str(ctx.exception))

    def test_repair_also_retries(self):
        client = FakeChatClient([REASONING_ONLY, GOOD])
        answer = DeepSeekSolver(client, log=lambda *_: None).repair(self.QUESTION, "old code", "第 2 行不一致")
        self.assertIn("int main", answer.text)
        self.assertEqual(len(client.calls), 2)


if __name__ == "__main__":
    unittest.main()
