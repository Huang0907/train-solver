import unittest

from trainsolve import prompts


class ExtractCodeTest(unittest.TestCase):
    def test_fenced_block(self):
        raw = "好的，代码如下：\n```c\n#include <stdio.h>\nint main(void){return 0;}\n```\n希望有帮助。"
        code = prompts.extract_code_block(raw)
        self.assertTrue(code.startswith("#include <stdio.h>"))
        self.assertTrue(code.endswith("\n"))

    def test_prefers_block_with_include(self):
        raw = "```text\nno code here\n```\n```c\n#include <stdio.h>\nint main(void){return 0;}\n```"
        code = prompts.extract_code_block(raw)
        self.assertIn("#include <stdio.h>", code)
        self.assertNotIn("no code here", code)

    def test_bare_code(self):
        raw = "#include <stdio.h>\nint main(void){return 0;}"
        self.assertIn("#include <stdio.h>", prompts.extract_code_block(raw))

    def test_empty_when_no_code(self):
        self.assertEqual(prompts.extract_code_block("我无法解答"), "")


class ExtractJsonTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(prompts.extract_json_object('{"answer": "B"}'), {"answer": "B"})

    def test_fenced_json_with_prose(self):
        raw = '分析如下：\n```json\n{"answer": "AC", "reason": "两个都对"}\n```\n完毕'
        data = prompts.extract_json_object(raw)
        self.assertEqual(data["answer"], "AC")

    def test_json_inside_sentence(self):
        raw = '选 B，理由：{"answer": "B"}'
        self.assertEqual(prompts.extract_json_object(raw)["answer"], "B")

    def test_invalid_returns_empty(self):
        self.assertEqual(prompts.extract_json_object("没有 JSON"), {})


class PromptContentTest(unittest.TestCase):
    def test_programming_prompt_contains_rules(self):
        question = {"id": 1, "qType": 5, "content": "题目正文", "language": 1, "point": 10}
        messages = prompts.build_programming_messages(question)
        self.assertEqual(messages[0]["role"], "system")
        user = messages[1]["content"]
        self.assertIn("题目正文", user)
        self.assertIn("directOutputCheat", user)
        self.assertIn("只输出一个 ```c 代码块", user)

    def test_objective_prompt_lists_options(self):
        question = {"id": 2, "qType": 0, "content": "哪个是关键字", "point": 2}
        options = [{"content": "int"}, {"content": "Int"}]
        messages = prompts.build_objective_messages(question, options)
        user = messages[1]["content"]
        self.assertIn("A. int", user)
        self.assertIn("B. Int", user)
        self.assertIn('{"answer": "B"}', user)

    def test_multi_choice_prompt_mentions_letter_order(self):
        question = {"id": 3, "qType": 1, "content": "多选"}
        messages = prompts.build_objective_messages(question, [{"content": "x"}])
        self.assertIn('{"answer": "AC"}', messages[1]["content"])

    def test_fix_bug_question_gets_extra_rules(self):
        question = {"id": 1595, "qType": 9, "content": "下面的程序有错：[code]main(){}[/code]", "language": 1}
        user = prompts.build_programming_messages(question)[1]["content"]
        self.assertIn("改错题", user)
        self.assertIn("全角符号", user)
        self.assertIn("保留题面原有的输出格式字符串", user)
        self.assertIn("循环边界", user)
        self.assertIn("≥ 1", user)
        # 普通编程题不应该带改错规则
        normal = prompts.build_programming_messages({"id": 1, "qType": 5, "content": "普通题"})[1]["content"]
        self.assertNotIn("改错题", normal)


if __name__ == "__main__":
    unittest.main()
