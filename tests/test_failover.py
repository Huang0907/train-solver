"""主/备模型自动切换（离线）。"""

import unittest

from trainsolve.solver.base import Answer, SolverError
from trainsolve.solver.failover import FailoverSolver


class FakeSolver:
    def __init__(self, label, fail=False):
        self.label = label
        self.fail = fail
        self.calls = 0

    def solve(self, question):
        self.calls += 1
        if self.fail:
            raise SolverError(f"{self.label} 503")
        return Answer(text=f"// from {self.label}", kind="code")

    def repair(self, question, previous, feedback):
        return self.solve(question)

    def health_check(self):
        if self.fail:
            raise SolverError(f"{self.label} down")


class FailoverTest(unittest.TestCase):
    def test_falls_back_to_second(self):
        primary, backup = FakeSolver("主", fail=True), FakeSolver("备")
        solver = FailoverSolver([primary, backup], log=lambda *_: None)
        answer = solver.solve({"id": 1})
        self.assertIn("备", answer.text)
        self.assertEqual(solver.active, 1)
        self.assertEqual(solver.switches, 1)

    def test_sticks_to_working_endpoint(self):
        primary, backup = FakeSolver("主", fail=True), FakeSolver("备")
        solver = FailoverSolver([primary, backup], log=lambda *_: None)
        solver.solve({"id": 1})
        solver.solve({"id": 2})
        self.assertEqual(primary.calls, 1)  # 只撞了一次，之后直接用备
        self.assertEqual(backup.calls, 2)

    def test_primary_ok_no_switch(self):
        primary, backup = FakeSolver("主"), FakeSolver("备")
        solver = FailoverSolver([primary, backup], log=lambda *_: None)
        answer = solver.solve({"id": 1})
        self.assertIn("主", answer.text)
        self.assertEqual(backup.calls, 0)
        self.assertEqual(solver.switches, 0)

    def test_all_fail_raises(self):
        solver = FailoverSolver([FakeSolver("主", fail=True), FakeSolver("备", fail=True)], log=lambda *_: None)
        with self.assertRaises(SolverError):
            solver.solve({"id": 1})

    def test_health_check_picks_healthy(self):
        solver = FailoverSolver([FakeSolver("主", fail=True), FakeSolver("备")], log=lambda *_: None)
        solver.health_check()
        self.assertEqual(solver.active, 1)

    def test_health_check_all_down(self):
        solver = FailoverSolver([FakeSolver("主", fail=True), FakeSolver("备", fail=True)], log=lambda *_: None)
        with self.assertRaises(SolverError):
            solver.health_check()

    def test_repair_uses_failover_too(self):
        primary, backup = FakeSolver("主", fail=True), FakeSolver("备")
        solver = FailoverSolver([primary, backup], log=lambda *_: None)
        answer = solver.repair({"id": 1}, "old", "第 1 行不一致")
        self.assertIn("备", answer.text)


if __name__ == "__main__":
    unittest.main()
