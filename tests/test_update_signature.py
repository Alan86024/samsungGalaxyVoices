import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "addon" / "globalPlugins" / "_signedWebUpdater.py"
MANIFEST = ROOT / "tests" / "fixtures" / "signed-test-manifest.json"
sys.path.insert(0, str(ROOT / "addon"))

for name in ("addonHandler", "core", "gui", "synthDriverHandler", "wx"):
	sys.modules[name] = types.ModuleType(name)
log_handler = types.ModuleType("logHandler")
log_handler.log = types.SimpleNamespace(error=lambda *args, **kwargs: None)
sys.modules["logHandler"] = log_handler
system_utils = types.ModuleType("systemUtils")
system_utils.ExecAndPump = lambda *args, **kwargs: None
sys.modules["systemUtils"] = system_utils
global_vars = types.ModuleType("globalVars")
global_vars.appArgs = types.SimpleNamespace(configPath=str(ROOT / "unused-test-config"))
sys.modules["globalVars"] = global_vars

spec = importlib.util.spec_from_file_location("signedUpdaterUnderTest", MODULE)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class SignatureTests(unittest.TestCase):
	def test_valid_signature_is_accepted(self):
		info = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
		updater._verifySignature(info)

	def test_changed_manifest_is_rejected(self):
		info = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
		info["url"] = "https://example.invalid/tampered.nvda-addon"
		with self.assertRaises(RuntimeError):
			updater._verifySignature(info)


if __name__ == "__main__":
	unittest.main()
