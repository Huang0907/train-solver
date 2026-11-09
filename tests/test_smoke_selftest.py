"""只读冒烟的 `--self-test` 必须进回归（0.5.6）。

`scripts\\smoke_readonly.py --self-test` 原来只能手动跑：谁忘了跑就没人知道，白名单被
改松（例如把 `api/student/submit` 加进放行前缀）也不会有任何测试变红。

这条用例用子进程跑真实脚本，钉住三件事：
* 退出码 0；
* **恰好 10 条 PASS、0 条 FAIL**（少一条说明有分支没跑到，多一条说明清单变了却没同步这里）；
* 末尾 `RESULT: ALL PASS`。

注意：脚本输出里没有一个字面量 `10/10 PASS`，它逐行打印 `PASS  <name>`，最后一行是
`RESULT: ALL PASS`。所以这里按"10 条 PASS 且无 FAIL"判断，比字符串匹配 `10/10` 更结实。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "smoke_readonly.py"
EXPECTED_PASSES = 10


class SmokeSelfTestTest(unittest.TestCase):
    def _run_self_test(self, out_dir: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTHONUTF8", None)   # 让上面的编码设置真正生效
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--self-test", "--out-dir", str(out_dir)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            env=env,
            timeout=180,
        )

    def test_self_test_passes_with_full_guard_set(self):
        # 证据必须写临时目录：脚本默认会写到 D:\DS Bot\reports\evidence\...
        # 不隔离的话，每跑一次 unittest 就覆盖一次仓库外的证据目录。
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run_self_test(Path(tmp))
            evidence = Path(tmp) / "guard-selftest.txt"
            self.assertTrue(evidence.exists(), "自检没有把证据写进 --out-dir 指定的目录")
            written = evidence.read_text(encoding="utf-8")
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace").strip()
        self.assertEqual(proc.returncode, 0, f"自检退出码应为 0，实际 {proc.returncode}；stderr={err[:400]}")
        lines = out.splitlines()
        passes = [line for line in lines if line.startswith("PASS")]
        fails = [line for line in lines if line.startswith("FAIL")]
        self.assertEqual(fails, [], f"自检里有失败项：{fails}")
        self.assertEqual(
            len(passes), EXPECTED_PASSES,
            f"自检通过项数应为 {EXPECTED_PASSES}，实际 {len(passes)}；输出：{out[:800]}",
        )
        self.assertIn("RESULT: ALL PASS", out, "缺少 ALL PASS 结论行，截图/证据无法引用")
        self.assertIn("RESULT: ALL PASS", written, "落盘证据与终端输出不一致")

    def test_self_test_reports_failure_when_guard_is_broken(self):
        """负向：白名单被改松必须变成 FAIL，而不是"照样 ALL PASS"。

        不复制脚本到临时目录（它会按 `__file__` 反推仓库根），而是让子进程在导入前
        把 `ALLOWED_PATHS` 改宽，再调用同一套自检。
        """
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            driver = (
                "import sys\n"
                f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
                "import smoke_readonly as smoke\n"
                "smoke.ALLOWED_PATHS = smoke.ALLOWED_PATHS + ('api/student/submit',)\n"
                f"sys.exit(smoke.main(['--self-test', '--out-dir', {str(out_dir)!r}]))\n"
            )
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env.pop("PYTHONUTF8", None)
            proc = subprocess.run(
                [sys.executable, "-c", driver],
                cwd=str(REPO_ROOT),
                capture_output=True,
                env=env,
                timeout=180,
            )
        out = proc.stdout.decode("utf-8", "replace")
        self.assertNotEqual(proc.returncode, 0, f"白名单被放行 submit 却仍返回 0；输出：{out[:800]}")
        self.assertIn("FAIL", out, f"白名单被改松却没有 FAIL 行；输出：{out[:800]}")


if __name__ == "__main__":
    unittest.main()
