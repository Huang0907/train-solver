"""抽题候选顺序（2026-11-06，0.5.4）。

规则（Chief 定的口径）：
* 可用 tag id **随机打乱**后依次尝试——不是按 id 升序（那会把抽题集中在前几个标签），
  也不是「标签优先、-1 其次」那种含糊说法；
* `tagId = -1`（平台随机题）**永远排在最后兜底**：只有所有具体标签都抽不到题才用它。
  事故背景：随机题可能抽到 `qtypes` 之外的题型，白耗一次配额 + 60s 限流等待。
"""

import unittest
from unittest import mock

from trainsolve.api import NoQuestionAvailable
from trainsolve.config import Config
from trainsolve.runner import Runner


class RecordingClient:
    """记录每次 choose 的 tagId；正 id 默认「没题」，-1 默认成功。"""

    def __init__(self, ids, *, succeed_on=None):
        self.ids = list(ids)
        self.calls: list[int] = []
        self.succeed_on = succeed_on
        self.available_tags_called = 0

    def available_tags(self, qbase_type, level=0):
        self.available_tags_called += 1
        return [{"id": value} for value in self.ids]

    def choose(self, qbase_type, level=0, tag_id=-1):
        self.calls.append(tag_id)
        if self.succeed_on is not None and tag_id == self.succeed_on:
            return "session-ok"
        if tag_id >= 0:
            raise NoQuestionAvailable(f"标签 {tag_id} 没题了")
        return "session-minus-one"


def make_runner(client, *, tag_id=-1):
    runner = object.__new__(Runner)
    runner.client = client
    runner.config = Config()
    runner.config.run.tag_id = tag_id
    runner.logs = []
    runner.log = runner.logs.append
    runner.should_stop = lambda: False
    return runner


class CandidateOrderTest(unittest.TestCase):
    def test_minus_one_is_always_last(self):
        client = RecordingClient([3, 1, 2])
        runner = make_runner(client)

        session = runner._pick_session()

        self.assertEqual(session, "session-minus-one")
        self.assertEqual(client.calls[-1], -1, "-1 必须是最后一个候选")
        self.assertEqual(len(client.calls), 4, "三个标签 + 一次兜底")
        self.assertEqual(sorted(client.calls[:-1]), [1, 2, 3], "具体标签都要试到")

    def test_tag_order_is_a_permutation_without_duplicates(self):
        client = RecordingClient([5, 2, 9, 2])
        runner = make_runner(client)

        runner._pick_session()

        tried = client.calls[:-1]
        self.assertEqual(sorted(tried), [2, 5, 9], "重复 id 只试一次，且集合不变")
        self.assertEqual(len(tried), len(set(tried)), "同一次调用内不许重复同一个 tag")
        self.assertEqual(client.calls[-1], -1)

    def test_order_comes_from_shuffle_not_id_order(self):
        """把 shuffle 换成「反转」，候选顺序就必须跟着反转——证明顺序真由打乱决定。"""
        client = RecordingClient([1, 2, 3])
        runner = make_runner(client)

        def fake_shuffle(values):
            values.reverse()

        with mock.patch("trainsolve.runner.random.shuffle", side_effect=fake_shuffle):
            runner._pick_session()

        self.assertEqual(client.calls[:-1], [3, 2, 1], f"实际：{client.calls}")

    def test_minus_one_not_used_when_some_tag_works(self):
        client = RecordingClient([1, 2, 3], succeed_on=2)
        runner = make_runner(client)

        session = runner._pick_session()

        self.assertEqual(session, "session-ok")
        self.assertNotIn(-1, client.calls, "有标签能用时不该走到兜底")

    def test_explicit_tag_skips_shuffle_and_fallback(self):
        client = RecordingClient([1, 2], succeed_on=7)
        runner = make_runner(client, tag_id=7)

        session = runner._pick_session()

        self.assertEqual(session, "session-ok")
        self.assertEqual(client.calls, [7], "配置指定了 tag_id 就只试它")
        self.assertEqual(client.available_tags_called, 0, "指定了标签就不该再拉标签列表")

    def test_empty_usable_ids_still_falls_back_to_minus_one(self):
        """标签列表里有垃圾数据（没有 id）时，至少要能用 -1 兜底，不能直接炸。"""
        class JunkClient(RecordingClient):
            def available_tags(self, qbase_type, level=0):
                return [{"name": "没有 id"}, {"id": "abc"}]

        client = JunkClient([])
        runner = make_runner(client)

        session = runner._pick_session()

        self.assertEqual(session, "session-minus-one")
        self.assertEqual(client.calls, [-1])


if __name__ == "__main__":
    unittest.main()
