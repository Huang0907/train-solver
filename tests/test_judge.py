import json
import unittest
from pathlib import Path
from unittest import mock

from trainsolve import judge

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

STATEMENT = """从键盘任意输入一表示年份的正整数，编程输出该年份每月的天数。

程序运行结果示例：
Please enter the year:2016
31,29,31,30,31,30
31,31,30,31,30,31

输入提示："Please enter the year:"
输入格式："%d"
输出格式："%d"
"""


class StatementParsingTest(unittest.TestCase):
    def test_extract_input_prompt(self):
        self.assertEqual(judge.extract_input_prompt(STATEMENT), "Please enter the year:")

    def test_extract_example_block_stops_at_input_format(self):
        block = judge.extract_example_block(STATEMENT)
        self.assertIn("31,29,31,30,31,30", block)
        self.assertNotIn("输入提示", block)
        self.assertNotIn("输出格式", block)

    def test_extract_samples(self):
        samples = judge.extract_samples(STATEMENT)
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertTrue(sample.usable)
        self.assertEqual(sample.stdin, "2016\n")
        # stdout 不含终端回显：提示语与第一行数据同在一行
        self.assertEqual(sample.expected_stdout, "Please enter the year:31,29,31,30,31,30\n31,31,30,31,30,31\n")
        self.assertEqual(sample.alternates, ["Please enter the year:\n31,29,31,30,31,30\n31,31,30,31,30,31\n"])

    def test_real_fixture_statement(self):
        record = json.loads((FIXTURES / "record_Q3373.json").read_text(encoding="utf-8"))
        question = record["paper"]["questions"][0]
        samples = judge.extract_samples(question["content"])
        self.assertTrue(samples and samples[0].usable)
        self.assertEqual(samples[0].stdin, "2016\n")

    def test_no_prompt_is_not_usable(self):
        statement = "输出示例：\n1 2\n3 4\n"
        samples = judge.extract_samples(statement)
        self.assertTrue(samples)
        self.assertFalse(samples[0].usable)


class MultiPromptTest(unittest.TestCase):
    """Q173 风格：多个输入提示，且提示语自带换行。"""

    STATEMENT = (
        "不用标准库函数strcmp，自己编写一个函数MyStrcmp\n"
        "(2)在主函数中\n"
        '     如果小于0，输出"string s\\<string t.\\n"\n'
        "   **输入提示信息：\n"
        '   输入s之前提示："Input s\\n"\n'
        '   输入t之前提示："Input t\\n"\n'
        "程序运行示例：\n"
        "Input s\n"
        "hello\n"
        "Input t\n"
        "world\n"
        "string s\\<string t."
    )

    def test_restores_transcript(self):
        samples = judge.extract_samples(self.STATEMENT)
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertTrue(sample.usable)
        self.assertEqual(sample.stdin, "hello\nworld\n")
        self.assertEqual(sample.expected_stdout, "Input s\nInput t\nstring s<string t.\n")

    def test_backslash_before_angle_is_unescaped(self):
        samples = judge.extract_samples(self.STATEMENT)
        self.assertNotIn("\\<", samples[0].expected_stdout)

    def test_real_fixture(self):
        record = json.loads((FIXTURES / "record_Q173.json").read_text(encoding="utf-8"))
        question = record["paper"]["questions"][0]
        sample = judge.extract_samples(question["content"])[0]
        self.assertTrue(sample.usable)
        self.assertEqual(sample.stdin, "hello\nworld\n")
        self.assertEqual(sample.expected_stdout, "Input s\nInput t\nstring s<string t.\n")


class FirstLineIsInputTest(unittest.TestCase):
    """Q608 风格：题面直接说明「第一行是输入，第二行是输出」。"""

    STATEMENT = (
        "请使用short int型的变量计算两个7位十进制整数的和。\n"
        '**输入格式要求："a = %3hd%4hd, b = %3hd%4hd"\n'
        '**输出格式要求："a + b = %d"\n'
        "程序运行示例如下（第一行是输入，第二行是输出。请注意：由于输入格式比较复杂，"
        "要求输入时必须要加上a = .....,b = ....这些）：\n"
        "a = 1234567, b = 7654321\n"
        "a + b = 8888888\n"
    )

    def test_marker_parenthetical_is_dropped(self):
        samples = judge.extract_samples(self.STATEMENT)
        self.assertEqual(samples[0].stdin, "a = 1234567, b = 7654321\n")
        self.assertEqual(samples[0].expected_stdout, "a + b = 8888888\n")

    def test_real_fixture(self):
        record = json.loads((FIXTURES / "record_Q608.json").read_text(encoding="utf-8"))
        question = record["paper"]["questions"][0]
        sample = judge.extract_samples(question["content"])[0]
        self.assertTrue(sample.usable)
        self.assertEqual(sample.stdin, "a = 1234567, b = 7654321\n")
        self.assertEqual(sample.expected_stdout, "a + b = 8888888\n")


class OutputPromptTest(unittest.TestCase):
    """Q1331 风格：题面同时声明「输入提示」与「输出提示」，且提示语里带 \\n 转义。"""

    def test_decode_escapes(self):
        self.assertEqual(judge.decode_escapes(r"\nA\tB"), "\nA\tB")
        self.assertEqual(judge.decode_escapes(r"string s\<string t."), "string s<string t.")

    def test_prompt_kinds(self):
        statement = (
            '**输入提示："\\nPlease enter the linked list (non-number indicates end)\\n"\n'
            '**输入提示："Node value:"\n'
            '**输出提示："\\nOriginal list:\\n"\n'
            '**输出格式："%4d"\n'
        )
        inputs, outputs = judge.extract_prompt_kinds(statement)
        self.assertEqual(inputs, ["\nPlease enter the linked list (non-number indicates end)\n", "Node value:"])
        self.assertEqual(outputs, ["\nOriginal list:\n"])

    def test_real_fixture(self):
        record = json.loads((FIXTURES / "record_Q1331.json").read_text(encoding="utf-8"))
        question = record["paper"]["questions"][0]
        sample = judge.extract_samples(question["content"])[0]
        self.assertTrue(sample.usable)
        # 开场白提示不带输入值，后面的 "Node value:" 才对应输入
        self.assertEqual(sample.stdin, "3\n4\n5\n6\n7\nend\n")
        self.assertEqual(
            sample.expected_stdout,
            "\nPlease enter the linked list (non-number indicates end)\n"
            + "Node value:" * 6
            + "\nOriginal list:\n   3   4   5   6   7\n\nReversed list:\n   7   6   5   4   3\n",
        )


class CompareTest(unittest.TestCase):
    def test_crlf_and_trailing_space_are_ignored(self):
        ok, _ = judge.compare("a\nb\n", "a\r\nb  \r\n")
        self.assertTrue(ok)

    def test_trailing_blank_lines_ignored(self):
        ok, _ = judge.compare("a\nb\n", "a\nb\n\n\n")
        self.assertTrue(ok)

    def test_fullwidth_comma_is_a_difference(self):
        ok, detail = judge.compare("1,2\n", "1，2\n")
        self.assertFalse(ok)
        self.assertIn("第 1 行不一致", detail)

    def test_missing_line_reported(self):
        ok, detail = judge.compare("a\nb\n", "a\n")
        self.assertFalse(ok)
        self.assertIn("期望: 'b'", detail)
        self.assertIn("缺少该行", detail)


class NormalizeTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(judge.normalize("a \r\n b\r\n\r\n"), "a\n b")


class MissingCompilerTest(unittest.TestCase):
    """没装编译器 = 无法校验（unchecked），不能当成「自检未通过」把答案拦下。"""

    def test_missing_compiler_is_unverified_but_not_failed(self):
        question = {"content": STATEMENT}
        with mock.patch("trainsolve.judge.find_compiler", return_value=None):
            result = judge.judge_program(question, "int main(void){ return 0; }")
        self.assertTrue(result.passed)
        self.assertFalse(result.verified)
        self.assertIn("未找到", result.feedback)


if __name__ == "__main__":
    unittest.main()
