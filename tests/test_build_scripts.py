"""打包脚本的纯逻辑测试：版本资源生成、密钥扫描匹配、public-only 交付契约。

不构建 exe（太慢且要 PyInstaller 全量环境），但把最容易出错的逻辑钉住：
1) VSVersionInfo 文本必须是**可被 PyInstaller 求值**的 Python 源码；
2) 敏感串要能同时抓 ASCII/UTF-8 与 UTF-16LE，且凭据/私有串两级不能混淆；
3) 自 0.5.4 起只产出 public，脚本里不许留 full 分支，但 full 残留的前置拦截必须留着。
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
for path in (str(REPO_ROOT), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import gen_version_info  # noqa: E402
import scan_exe_secrets  # noqa: E402


class VersionInfoTest(unittest.TestCase):
    def test_version_tuple_pads_to_four(self):
        self.assertEqual(gen_version_info.version_tuple("0.5.0"), (0, 5, 0, 0))
        self.assertEqual(gen_version_info.version_tuple("1.2"), (1, 2, 0, 0))
        self.assertEqual(gen_version_info.version_tuple("1"), (1, 0, 0, 0))

    def test_version_tuple_strips_prerelease_suffix(self):
        """预发布后缀里的数字不能进版本号，否则 rc1 看着比正式版还新。"""
        self.assertEqual(gen_version_info.version_tuple("0.6.0rc1"), (0, 6, 0, 0))
        self.assertEqual(gen_version_info.version_tuple("2.0.0-beta2"), (2, 0, 0, 0))
        self.assertEqual(gen_version_info.version_tuple("0.5.0.dev1"), (0, 5, 0, 0))

    def test_version_tuple_keeps_fourth_component(self):
        self.assertEqual(gen_version_info.version_tuple("1.2.3.4"), (1, 2, 3, 4))

    def test_text_is_valid_python(self):
        """生成的必须是能编译的 Python 源码，否则 PyInstaller 直接报错。"""
        text = gen_version_info.build_text("0.5.0")
        compile(text, "<version_info>", "exec")  # 语法不合法会在这里抛

    def test_text_carries_version_and_product(self):
        text = gen_version_info.build_text("0.5.0")
        self.assertIn("filevers=(0, 5, 0, 0)", text)
        self.assertIn("prodvers=(0, 5, 0, 0)", text)
        self.assertIn("StringStruct('FileVersion', '0.5.0')", text)
        self.assertIn("StringStruct('ProductVersion', '0.5.0')", text)
        self.assertIn("StringStruct('OriginalFilename', 'TrainSolveClient.exe')", text)

    def test_quote_in_description_is_escaped(self):
        """描述里带单引号不能把生成的源码打断。"""
        text = gen_version_info.build_text("0.5.0", description="it's broken")
        compile(text, "<version_info>", "exec")
        self.assertIn("it\\'s broken", text)

    def test_true_version_comes_from_package(self):
        """版本号必须取自 trainsolve.__version__，不许脚本里再写一份。"""
        from trainsolve import __version__

        self.assertIn(f"'{__version__}'", gen_version_info.build_text(__version__))

    @unittest.skipUnless(
        __import__("importlib").util.find_spec("PyInstaller"), "没装 PyInstaller"
    )
    def test_text_evaluates_with_pyinstaller_classes(self):
        """用 PyInstaller 自己的类求值一次：这才是它构建时真正做的事。"""
        from PyInstaller.utils.win32.versioninfo import (
            FixedFileInfo,
            StringFileInfo,
            StringStruct,
            StringTable,
            VarFileInfo,
            VarStruct,
            VSVersionInfo,
        )

        namespace = {
            "VSVersionInfo": VSVersionInfo,
            "FixedFileInfo": FixedFileInfo,
            "StringFileInfo": StringFileInfo,
            "StringTable": StringTable,
            "StringStruct": StringStruct,
            "VarFileInfo": VarFileInfo,
            "VarStruct": VarStruct,
        }
        info = eval(gen_version_info.build_text("0.5.0"), namespace)  # noqa: S307
        self.assertIsInstance(info, VSVersionInfo)
        entries = []
        for child in info.kids:
            for table in getattr(child, "kids", []) or []:
                for struct in getattr(table, "kids", []) or []:
                    value = getattr(struct, "val", None) or getattr(struct, "Value", None)
                    if value:
                        entries.append(value)
        self.assertIn("0.5.0", entries)
        self.assertIn("TrainSolveClient", entries)


class SecretScanTest(unittest.TestCase):
    def test_forbidden_and_private_are_disjoint(self):
        forbid = set(scan_exe_secrets.DEFAULT_FORBIDDEN)
        private = set(scan_exe_secrets.DEFAULT_PRIVATE)
        self.assertFalse(forbid & private, "同一条串不能既算凭据又算私有串")

    def test_build_passes_local_credentials_to_scanner(self):
        """真实凭据不进仓库：构建脚本必须从本机 config.toml 读出来传给扫描器，否则门禁恒过。"""
        script = (REPO_ROOT / "build_release.ps1").read_text(encoding="utf-8")
        self.assertIn("--forbid", script, "扫描器没收到本机凭据，门禁会恒过")
        self.assertIn("secretForbidden", script)
        self.assertIn("config.toml", script)

    def test_forbidden_lists_only_real_credentials(self):
        """本机代理地址/网关模型 id 是源码里的示例文案，不能当凭据拦构建。"""
        forbid = scan_exe_secrets.DEFAULT_FORBIDDEN
        self.assertNotIn("127.0.0.1:7864", forbid)
        self.assertNotIn("cn:deepseek-v4.1-flash", forbid)

    def test_hit_in_text(self):
        needles = ("sk-FAKE0123456789", "wbk_FAKEFAKE")
        self.assertEqual(scan_exe_secrets.hit_in_text("xx sk-FAKE0123456789 yy", needles), ["sk-FAKE0123456789"])
        self.assertEqual(scan_exe_secrets.hit_in_text("clean", needles), [])

    def test_hit_in_bytes_utf8(self):
        data = "前缀 sk-FAKE0123456789 后缀".encode("utf-8")
        self.assertEqual(
            scan_exe_secrets.hit_in_bytes(data, ("sk-FAKE0123456789",)), ["sk-FAKE0123456789"]
        )

    def test_hit_in_bytes_utf16le(self):
        """Windows 资源里常见 UTF-16，只搜 UTF-8 会漏。"""
        data = "学号 2000000001".encode("utf-16-le")
        self.assertEqual(
            scan_exe_secrets.hit_in_bytes(data, ("2000000001",)), ["2000000001"]
        )

    def test_iter_code_strings_walks_nested_code(self):
        """推导式和内嵌函数里的字面量也要扫到（它们在嵌套 code 里）。"""
        src = (
            "A = 'top-level-secret'\n"
            "B = [x for x in ['inside-comprehension']]\n"
            "def f():\n"
            "    return 'inside-function'\n"
        )
        code = compile(src, "<t>", "exec")
        found = list(scan_exe_secrets.iter_code_strings(code))
        self.assertIn("top-level-secret", found)
        self.assertIn("inside-comprehension", found)
        self.assertIn("inside-function", found)

    def test_report_clean_means_no_hits(self):
        report = scan_exe_secrets.ScanReport(exe="x.exe")
        self.assertTrue(report.clean)
        report.hits.append(("sk-FAKE0123456789", "PYZ:trainsolve.gui"))
        self.assertFalse(report.clean)
        # 只有私有串时仍算干净（不阻断构建）
        report2 = scan_exe_secrets.ScanReport(exe="x.exe")
        report2.warnings.append(("127.0.0.1:7864", "PYZ:trainsolve.gui"))
        self.assertTrue(report2.clean)


class PublicOnlyBuildTest(unittest.TestCase):
    """0.5.4 起交付面收窄为 public：脚本与文档都不许再描述一个不存在的 full 版本。

    这组用例是“规则变了”的护栏：谁把 full 分支贴回来，或者把文档改回“两个版本”，
    这里立刻红——不靠人记得。
    """

    BUILD_SCRIPT = REPO_ROOT / "build_release.ps1"

    def _script(self) -> str:
        return self.BUILD_SCRIPT.read_text(encoding="utf-8")

    def test_script_has_no_full_branch(self):
        text = self._script()
        for token in ("FullRoot", "FullZip", "_selfuse", "禁止外发", "使用说明-完整版"):
            self.assertNotIn(token, text, f"build_release.ps1 里仍有 full 分支残留：{token}")

    def test_script_only_ships_public_manual(self):
        text = self._script()
        self.assertIn("使用说明-公开版.md", text)
        self.assertIn("TrainSolveClient-$version-public", text)

    def test_script_still_rejects_full_in_release(self):
        """full 不再产出，但 release\\ 里出现历史 full 包时必须**打包前**拦住。"""
        text = self._script()
        self.assertIn("'*full*'", text, "release\\ 的 full 前置拦截被删了")
        guard = [line for line in text.splitlines() if "throw" in line and "full 包" in line]
        self.assertTrue(guard, "拦截逻辑里没有对 full 包的报错")

    def test_docs_do_not_describe_a_full_version(self):
        docs = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))]
        for path in docs:
            text = path.read_text(encoding="utf-8")
            for token in ("使用说明-完整版", "_selfuse", "FullRoot", "FullZip"):
                self.assertNotIn(
                    token, text, f"{path.name} 仍在描述已取消的 full 版本：{token}"
                )


class VersionConsistencyTest(unittest.TestCase):
    """版本号两处必须一致：`trainsolve.__version__` 与 `pyproject.toml` 的 project.version。

    打包脚本从 `__version__` 生成版本资源，而发布包名/文档引用的是 pyproject 那套写法；
    两处一旦分叉，exe 属性里的版本和包名会对不上。此前靠人工同时改两处，没有东西拦。
    """

    def test_package_version_matches_pyproject(self):
        import tomllib

        from trainsolve import __version__

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(__version__, data["project"]["version"])


class ScannerConsoleEncodingTest(unittest.TestCase):
    """回归：gbk 控制台下"被拦"时，证据必须还能打出来。

    2026-11-05 实测：`python scripts\scan_exe_secrets.py <exe>` 在 gbk 控制台崩在打印
    那一行（UnicodeEncodeError），退出码同样是 1 —— 和"命中凭据"撞在一起，门禁拦得住
    却说不清是哪条凭据、落在哪个模块。这条测试盯的就是这个。
    """

    def test_hit_report_survives_gbk_console(self):
        driver = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, {scripts!r})
            import scan_exe_secrets as s

            def fake_scan(exe, forbidden=None, private=None):
                report = s.ScanReport(exe=exe, entries=2, modules=3)
                report.hits.append(("wbk_FAKEFAKE", "PYZ:leaky"))
                return report

            s.scan = fake_scan
            raise SystemExit(s.main(["leaky.exe"]))
            """
        ).format(scripts=str(SCRIPTS))

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "gbk"   # 复现 Windows 控制台默认编码
        env.pop("PYTHONUTF8", None)       # UTF-8 模式会盖掉上面的设置，测不到坑

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "driver.py"
            path.write_text(driver, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(path)], capture_output=True, env=env, timeout=120
            )

        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace").strip()
        self.assertEqual(proc.returncode, 1, f"命中时退出码必须是 1，实际 {proc.returncode}")
        self.assertIn("wbk_FAKEFAKE", out, "gbk 控制台下丢了凭据名，证据打不出来")
        self.assertIn("PYZ:leaky", out, "gbk 控制台下丢了命中位置")
        self.assertEqual(err, "", f"不该有 traceback：{err[:400]}")


if __name__ == "__main__":
    unittest.main()
