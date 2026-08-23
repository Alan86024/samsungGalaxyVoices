import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "addon" / "installTasks.py"


class InstallMigrationTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory(prefix="samsung-migration-")
		self.base = Path(self.temp.name)
		self.config = self.base / "config"
		self.old = self.base / "old-addon"
		data = self.old / "synthDrivers" / "_samsungGalaxyVoices"
		(data / "engines" / "regular").mkdir(parents=True)
		(data / "engines" / "regular" / "libsamsungtts.so").write_bytes(b"legacy engine")
		for folder in ("en-gb-l02", "en-gb-g02"):
			assets = data / "voices" / folder / "assets"
			assets.mkdir(parents=True)
			(assets / "cfg").write_bytes(b"name\0Migrated Voice\0")
			(assets / "lng").write_bytes(b"language")
			(assets / "regular.ivc").write_bytes(b"voice")
		user_voice = self.config / "samsungGalaxyVoices" / "voices" / "en-us-l03"
		(user_voice / "assets").mkdir(parents=True)
		(user_voice / "assets" / "cfg").write_bytes(b"name\0Stephanie\0")
		(user_voice / "assets" / "lng").write_bytes(b"language")
		(user_voice / "assets" / "regular.ivc").write_bytes(b"voice")
		(user_voice / "voice.json").write_text(json.dumps({
			"id": "en_US_l03", "language": "en_US", "family": "l", "speaker": 3,
			"name": "Samsung TTS US English Voice 1",
		}), encoding="utf-8")
		dedicated_engine = b"dedicated voice engine"
		import hashlib
		self.dedicated_hash = hashlib.sha256(dedicated_engine).hexdigest()
		dedicated_path = self.config / "samsungGalaxyVoices" / "engines" / self.dedicated_hash
		dedicated_path.mkdir(parents=True)
		(dedicated_path / "libsamsungtts.so").write_bytes(dedicated_engine)
		newer_voice = self.config / "samsungGalaxyVoices" / "voices" / "en-us-l04"
		(newer_voice / "assets").mkdir(parents=True)
		(newer_voice / "assets" / "cfg").write_bytes(b"name\0Julia\0")
		(newer_voice / "assets" / "lng").write_bytes(b"language")
		(newer_voice / "assets" / "regular.ivc").write_bytes(b"voice")
		(newer_voice / "voice.json").write_text(json.dumps({
			"id": "en_US_l04", "language": "en_US", "family": "l", "speaker": 4,
			"name": "Julia", "engineHash": self.dedicated_hash,
		}), encoding="utf-8")
		addon_handler = types.ModuleType("addonHandler")
		addon_handler.getAvailableAddons = lambda: [types.SimpleNamespace(name="samsungGalaxyVoices", path=str(self.old))]
		sys.modules["addonHandler"] = addon_handler
		global_vars = types.ModuleType("globalVars")
		global_vars.appArgs = types.SimpleNamespace(configPath=str(self.config))
		sys.modules["globalVars"] = global_vars
		log_handler = types.ModuleType("logHandler")
		log_handler.log = types.SimpleNamespace(info=lambda *args, **kwargs: None)
		sys.modules["logHandler"] = log_handler
		spec = importlib.util.spec_from_file_location("installTasksUnderTest", MODULE)
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)

	def tearDown(self):
		self.temp.cleanup()

	def test_preserves_bundled_and_downloaded_voices(self):
		self.module.onInstall()
		voices = self.config / "samsungGalaxyVoices" / "voices"
		for folder in ("en-gb-l02", "en-gb-g02", "en-us-l03"):
			metadata = json.loads((voices / folder / "voice.json").read_text(encoding="utf-8"))
			self.assertRegex(metadata["engineHash"], r"^[0-9a-f]{64}$")
			engine = self.config / "samsungGalaxyVoices" / "engines" / metadata["engineHash"] / "libsamsungtts.so"
			self.assertTrue(engine.is_file())
		self.assertEqual("Stephanie", json.loads((voices / "en-us-l03" / "voice.json").read_text())["name"])
		julia = json.loads((voices / "en-us-l04" / "voice.json").read_text(encoding="utf-8"))
		self.assertEqual(self.dedicated_hash, julia["engineHash"])


if __name__ == "__main__":
	unittest.main()
