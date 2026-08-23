import importlib.util
import shutil
import struct
import tempfile
import time
import types
import unittest
from pathlib import Path
import zipfile
import sys


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "addon" / "synthDrivers" / "_samsungGalaxyVoices" / "voiceStore.py"


class VoiceStoreTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory(prefix="samsung-voice-store-")
		global_vars = types.ModuleType("globalVars")
		global_vars.appArgs = types.SimpleNamespace(configPath=self.temp.name)
		sys.modules["globalVars"] = global_vars
		spec = importlib.util.spec_from_file_location("voiceStoreUnderTest", MODULE)
		self.store = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.store)
		self.package = Path(self.temp.name) / "voice.apk"
		engine = bytearray(64)
		engine[:6] = b"\x7fELF\x02\x01"
		struct.pack_into("<H", engine, 18, 183)
		with zipfile.ZipFile(self.package, "w") as archive:
			archive.writestr("assets/cfg", b"_locale\0en-US\0_variant\0l03\0name\0Stephanie\0")
			archive.writestr("assets/lng", b"language")
			archive.writestr("assets/regular.ivc", b"voice")
			archive.writestr(self.store.ENGINE_MEMBER, engine)
		self.store.REQUIRED_ASSETS = {
			"assets/cfg": (1, 1024), "assets/lng": (1, 1024),
		}
		self.store.MODEL_LIMITS = (1, 1024)
		self.store.ENGINE_LIMITS = (1, 1024)
		self.store.downloadMetadata = lambda code: {
			"url": "https://example.invalid/voice.apk", "size": self.package.stat().st_size,
			"version": "1", "productName": "Samsung TTS US English Voice 1",
			"package": "com.samsung.SMT.lang_en_us_l03",
		}
		self.store._downloadPackage = lambda metadata, destination, progress: shutil.copyfile(self.package, destination)

	def tearDown(self):
		self.temp.cleanup()

	def test_install_uses_embedded_name_and_matching_engine(self):
		name = self.store.installVoice("en_us_l03", lambda received, total: None)
		self.assertEqual("Stephanie", name)
		self.assertTrue(self.store.isInstalled("en_us_l03"))
		definitions = self.store.loadVoiceDefinitions()
		self.assertEqual("Stephanie", definitions["en_US_l03"]["name"])
		self.assertTrue(Path(definitions["en_US_l03"]["enginePath"]).is_file())
		self.assertEqual("US English - Stephanie", self.store.voiceLabel("en_us_l03"))

	def test_compact_voice_uses_tiny_model_and_loads_as_a_voice(self):
		with zipfile.ZipFile(self.package, "w") as archive:
			archive.writestr("assets/cfg", b"_locale\0cs-CZ\0_variant\0f00\0name\0Classic Czech\0")
			archive.writestr("assets/lng", b"language")
			archive.writestr("assets/tiny.ivc", b"tiny voice")
			engine = bytearray(64)
			engine[:6] = b"\x7fELF\x02\x01"
			struct.pack_into("<H", engine, 18, 183)
			archive.writestr(self.store.ENGINE_MEMBER, engine)
		self.store.downloadMetadata = lambda code: {
			"url": "https://example.invalid/voice.apk", "size": self.package.stat().st_size,
			"version": "1", "productName": "Samsung TTS Czech Classic",
			"package": "com.samsung.SMT.lang_cs_cz_f00",
		}
		name = self.store.installVoice("cs_cz_f00", lambda received, total: None)
		self.assertEqual("Classic Czech", name)
		metadata = self.store.readVoiceMetadata("cs_cz_f00")
		self.assertEqual("tiny.ivc", metadata["model"])
		self.assertTrue((Path(self.store.voicePath("cs_cz_f00")) / "assets" / "tiny.ivc").is_file())
		definitions = self.store.loadVoiceDefinitions()
		self.assertEqual("f", definitions["cs_CZ_f00"]["family"])

	def test_removal_collects_unreferenced_engine(self):
		self.store.installVoice("en_us_l03", lambda received, total: None)
		engine_path = Path(next(iter(self.store.loadVoiceDefinitions().values()))["enginePath"])
		self.store.removeVoices(("en_us_l03",))
		self.assertFalse(engine_path.exists())

	def test_catalog_size_cache_round_trips(self):
		cache = {"en_us_l03": {"size": 12345678, "checked": time.time()}}
		self.store.saveCatalogCache(cache)
		self.assertEqual(cache["en_us_l03"]["size"], self.store.loadCatalogCache()["en_us_l03"]["size"])

	def test_playback_buffer_setting_round_trips_and_is_bounded(self):
		settings = self.store.loadSettings()
		self.assertEqual(0, settings["playbackBufferMs"])
		settings["playbackBufferMs"] = 250
		self.store.saveSettings(settings)
		self.assertEqual(250, self.store.loadSettings()["playbackBufferMs"])
		settings["playbackBufferMs"] = 9999
		self.store.saveSettings(settings)
		self.assertEqual(500, self.store.loadSettings()["playbackBufferMs"])

	def test_rejects_a_second_simultaneous_install(self):
		self.assertTrue(self.store._installLock.acquire(blocking=False))
		try:
			with self.assertRaisesRegex(RuntimeError, "already in progress"):
				self.store.installVoice("en_us_l03", lambda received, total: None)
		finally:
			self.store._installLock.release()


if __name__ == "__main__":
	unittest.main()
