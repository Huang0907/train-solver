"""配置加密（DPAPI）的离线测试。

只测「加密/解密/文件层/加载器优先读 .enc」这些不依赖平台的逻辑；
DPAPI 本身只在 Windows 上可用，非 Windows 自动跳过。
"""

import tempfile
import unittest
from pathlib import Path

from trainsolve import configcrypt
from trainsolve.config import resolve_config_path

SAMPLE = """[platform]
base_url = "http://127.0.0.1/train/"
username = "1234567890"
password = "secret-pass"

[deepseek]
api_key = "sk-test-key"
base_url = "https://api.deepseek.com"
model = "deepseek-flash"

[run]
dry_run = true
submit = false
"""


def _skip_without_dpapi() -> None:
    if not configcrypt.available():
        raise unittest.SkipTest("非 Windows，没有 DPAPI")


class CryptoRoundTripTest(unittest.TestCase):
    def test_加密后能原样解回(self):
        _skip_without_dpapi()
        blob = configcrypt.encrypt_bytes(SAMPLE.encode("utf-8"))
        self.assertEqual(configcrypt.decrypt_bytes(blob).decode("utf-8"), SAMPLE)

    def test_密文里看不到明文(self):
        _skip_without_dpapi()
        blob = configcrypt.encrypt_bytes(SAMPLE.encode("utf-8"))
        self.assertNotIn(b"secret-pass", blob)
        self.assertNotIn(b"sk-test-key", blob)

    def test_被改过的密文解不开且报错清楚(self):
        _skip_without_dpapi()
        blob = bytearray(configcrypt.encrypt_bytes(b"hello"))
        blob[-1] ^= 0xFF
        with self.assertRaises(configcrypt.ConfigCryptoError) as ctx:
            configcrypt.decrypt_bytes(bytes(blob))
        self.assertIn("解密失败", str(ctx.exception))


class FileLayerTest(unittest.TestCase):
    def test_写enc文件自动加密_读回自动解密(self):
        _skip_without_dpapi()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml.enc"
            configcrypt.write_config_text(target, SAMPLE)
            raw = target.read_bytes()
            self.assertTrue(raw.startswith(configcrypt.MAGIC))
            self.assertNotIn(b"secret-pass", raw)
            self.assertEqual(configcrypt.read_config_text(target), SAMPLE)

    def test_普通文件仍然按明文读写(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            configcrypt.write_config_text(target, SAMPLE)
            self.assertEqual(target.read_text(encoding="utf-8"), SAMPLE)
            self.assertEqual(configcrypt.read_config_text(target), SAMPLE)

    def test_加密文件与解密文件往返(self):
        _skip_without_dpapi()
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "config.toml"
            plain.write_text(SAMPLE, encoding="utf-8")
            enc = configcrypt.encrypt_config_file(plain)
            self.assertTrue(enc.name.endswith(".enc"))
            self.assertTrue(configcrypt.looks_encrypted(enc))
            back = configcrypt.decrypt_config_file(enc, overwrite=True)
            self.assertEqual(back.read_text(encoding="utf-8"), SAMPLE)

    def test_加密目标等于源文件要报错(self):
        """`encrypt --to` 指回源文件：写出来还是明文/把源文件重加密一遍，再删明文就没了。

        用已加密的源文件来测：文件名以 `.enc` 结尾，后缀守卫拦不住，只有同路径守卫能拦；
        没有这道守卫时它会把源文件悄悄重写一遍（密文每次不同），用例就会红。
        """
        _skip_without_dpapi()
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "config.toml"
            plain.write_text(SAMPLE, encoding="utf-8")
            src = configcrypt.encrypt_config_file(plain)
            before = src.read_bytes()
            with self.assertRaises(configcrypt.ConfigCryptoError):
                configcrypt.encrypt_config_file(src, src)
            self.assertEqual(src.read_bytes(), before, "报错时源文件不能被重写")

    def test_加密输出名必须enc结尾(self):
        """目标名不以 .enc 结尾时写出来仍是明文——必须拒绝，别谎报「已加密」。"""
        _skip_without_dpapi()
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "config.toml"
            plain.write_text(SAMPLE, encoding="utf-8")
            target = Path(tmp) / "secret.dat"
            with self.assertRaises(configcrypt.ConfigCryptoError):
                configcrypt.encrypt_config_file(plain, target)
            self.assertFalse(target.exists(), "被拒绝时不该留下任何输出文件")

    def test_解密目标已存在时默认拒绝覆盖(self):
        """0.6.9：明文已经在那儿时不许静默覆盖——那会把更新的那份冲掉。"""
        _skip_without_dpapi()
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "config.toml"
            plain.write_text("NEWER-CONTENT", encoding="utf-8")
            enc = configcrypt.encrypt_config_file(plain)
            plain.write_text("NEWER-CONTENT", encoding="utf-8")
            with self.assertRaises(configcrypt.ConfigCryptoError):
                configcrypt.decrypt_config_file(enc)
            self.assertEqual(plain.read_text(encoding="utf-8"), "NEWER-CONTENT")

    def test_enc后缀里装明文要报错而不是当明文读(self):
        """名字是 .enc、内容却是明文：说明「以为加密了其实没有」，必须报出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "config.toml.enc"
            fake.write_text("[platform]\ntag = 5\n", encoding="utf-8")
            self.assertFalse(configcrypt.looks_encrypted(fake))
            with self.assertRaises(configcrypt.ConfigCryptoError):
                configcrypt.read_config_text(fake)

    def test_后缀被改坏也能认出是加密文件(self):
        _skip_without_dpapi()
        with tempfile.TemporaryDirectory() as tmp:
            weird = Path(tmp) / "config.bin"
            weird.write_bytes(configcrypt.MAGIC + configcrypt.encrypt_bytes(b"[platform]\n"))
            self.assertTrue(configcrypt.looks_encrypted(weird))
            self.assertEqual(configcrypt.read_config_text(weird), "[platform]\n")


class ResolvePreferenceTest(unittest.TestCase):
    def test_同目录有enc时优先用它(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "config.toml"
            plain.write_text(SAMPLE, encoding="utf-8")
            enc = Path(tmp) / "config.toml.enc"
            enc.write_bytes(b"placeholder")     # 只测路径选择，不要求能解密
            resolved = resolve_config_path(str(plain))
            self.assertEqual(resolved.path, enc)

    def test_没有enc时仍然是明文路径(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "config.toml"
            plain.write_text(SAMPLE, encoding="utf-8")
            self.assertEqual(resolve_config_path(str(plain)).path, plain)

    def test_只存在enc时显式指定明文名也能解析(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "config.toml"
            enc = Path(tmp) / "config.toml.enc"
            enc.write_bytes(b"placeholder")
            resolved = resolve_config_path(str(plain))
            self.assertEqual(resolved.path, enc)


class LoaderTest(unittest.TestCase):
    def test_加载器能直接吃加密配置(self):
        _skip_without_dpapi()
        from trainsolve.config import load_config_with_source

        with tempfile.TemporaryDirectory() as tmp:
            enc = Path(tmp) / "config.toml.enc"
            configcrypt.write_config_text(enc, SAMPLE)
            config, resolved = load_config_with_source(str(enc))
            self.assertEqual(config.platform.username, "1234567890")
            self.assertEqual(config.deepseek.api_key, "sk-test-key")
            self.assertEqual(resolved.path, enc)

    def test_解密失败时报的是配置错误(self):
        _skip_without_dpapi()
        from trainsolve.config import ConfigSourceError, load_config_with_source

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "config.toml.enc"
            bad.write_bytes(configcrypt.MAGIC + b"not-a-real-blob")
            with self.assertRaises(ConfigSourceError) as ctx:
                load_config_with_source(str(bad))
            self.assertIn("配置读取失败", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
