import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


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
		self.oldAddon = types.SimpleNamespace(
			name="samsungGalaxyVoices",
			path=str(self.old),
			isPendingInstall=False,
			requestRemove=mock.Mock(),
		)
		addon_handler = types.ModuleType("addonHandler")
		addon_handler.getAvailableAddons = lambda: [self.oldAddon]
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
			metadata = json.loads((voices / "legacy" / folder / "voice.json").read_text(encoding="utf-8"))
			self.assertRegex(metadata["engineHash"], r"^[0-9a-f]{64}$")
			engine = self.config / "samsungGalaxyVoices" / "engines" / metadata["engineHash"] / "libsamsungtts.so"
			self.assertTrue(engine.is_file())
		self.assertEqual("Stephanie", json.loads((voices / "legacy" / "en-us-l03" / "voice.json").read_text())["name"])
		julia = json.loads((voices / "legacy" / "en-us-l04" / "voice.json").read_text(encoding="utf-8"))
		self.assertEqual(self.dedicated_hash, julia["engineHash"])
		self.assertFalse((voices / "en-us-l03").exists())
		self.assertFalse((voices / "en-us-l04").exists())
		self.oldAddon.requestRemove.assert_called_once_with()

	def test_pending_copy_is_not_marked_for_removal(self):
		pending = types.SimpleNamespace(
			name="samsungGalaxyVoices",
			path=str(self.base / "samsungGalaxyVoices.pendingInstall"),
			isPendingInstall=True,
			requestRemove=mock.Mock(),
		)
		sys.modules["addonHandler"].getAvailableAddons = lambda: [self.oldAddon, pending]

		self.module.onInstall()

		self.oldAddon.requestRemove.assert_called_once_with()
		pending.requestRemove.assert_not_called()

	def test_failed_migration_does_not_remove_working_addon(self):
		self.module._migrateVoiceLayout = mock.Mock(side_effect=RuntimeError("migration failed"))

		with self.assertRaisesRegex(RuntimeError, "migration failed"):
			self.module.onInstall()

		self.oldAddon.requestRemove.assert_not_called()

	def test_public_upgrade_migrates_complete_flat_voices_without_bundled_engine(self):
		sys.modules["addonHandler"].getAvailableAddons = lambda: []
		self.module.onInstall()

		voices = self.config / "samsungGalaxyVoices" / "voices"
		self.assertTrue((voices / "legacy" / "en-us-l04" / "voice.json").is_file())
		self.assertFalse((voices / "en-us-l04").exists())
		self.assertTrue((voices / "en-us-l03").is_dir(), "An incomplete voice must not be moved")

	def test_migrates_a_newer_generation_to_its_own_folder(self):
		voices = self.config / "samsungGalaxyVoices" / "voices"
		source = voices / "en-us-l03--s24"
		(source / "assets").mkdir(parents=True)
		(source / "assets" / "cfg").write_bytes(b"name\0Stephanie\0")
		(source / "assets" / "lng").write_bytes(b"language")
		(source / "assets" / "regular.ivc").write_bytes(b"voice")
		(source / "voice.json").write_text(json.dumps({
			"id": "en_US_l03_s24", "language": "en_US", "family": "l", "speaker": 3,
			"name": "Stephanie", "generation": "s24", "catalogKey": "en_us_l03@s24",
			"version": "312501000", "engineHash": self.dedicated_hash,
		}), encoding="utf-8")
		sys.modules["addonHandler"].getAvailableAddons = lambda: []

		self.module.onInstall()

		self.assertTrue((voices / "s24" / "en-us-l03" / "voice.json").is_file())
		self.assertFalse(source.exists())

	def test_removes_exact_incompatible_voice_packages(self):
		voices = self.config / "samsungGalaxyVoices" / "voices"
		bad = voices / "en-in-l02"
		(bad / "assets").mkdir(parents=True)
		for filename in ("cfg", "lng", "regular.ivc"):
			(bad / "assets" / filename).write_bytes(b"voice")
		(bad / "voice.json").write_text(json.dumps({
			"id": "en_IN_l02", "language": "en_IN", "family": "l", "speaker": 2,
			"name": "Indian premium", "version": "312347000",
			"engineHash": self.dedicated_hash,
		}), encoding="utf-8")
		sys.modules["addonHandler"].getAvailableAddons = lambda: []

		self.module.onInstall()

		self.assertFalse(bad.exists())
		self.assertTrue(
			(self.config / "samsungGalaxyVoices" / "engines" / self.dedicated_hash / "libsamsungtts.so").is_file(),
			"The engine is still used by Julia and must be retained",
		)

		for folder, identifier, key, version in (
			("en-in-l02", "en_IN_l02_s24", "en_in_l02@s24", "312501000"),
			("en-us-l03", "en_US_l03_s24", "en_us_l03@s24", "312504000"),
			("en-us-g02", "en_US_g02_s24", "en_us_g02@s24", "312504000"),
		):
			voice = voices / "s24" / folder
			(voice / "assets").mkdir(parents=True, exist_ok=True)
			for filename in ("cfg", "lng", "regular.ivc"):
				(voice / "assets" / filename).write_bytes(b"voice")
			(voice / "voice.json").write_text(json.dumps({
				"id": identifier, "catalogKey": key, "generation": "s24",
				"version": version, "engineHash": self.dedicated_hash,
			}), encoding="utf-8")

		self.module.onInstall()

		for folder in ("en-in-l02", "en-us-l03", "en-us-g02"):
			self.assertFalse((voices / "s24" / folder).exists())


if __name__ == "__main__":
	unittest.main()
