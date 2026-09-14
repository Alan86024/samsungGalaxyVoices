import importlib.util
from collections import OrderedDict
import ctypes
import io
import json
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path


class FileTime(ctypes.Structure):
	_fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))


def processCpuSeconds(process):
	creation = FileTime()
	exitTime = FileTime()
	kernel = FileTime()
	user = FileTime()
	if not ctypes.windll.kernel32.GetProcessTimes(
		process._handle,
		ctypes.byref(creation),
		ctypes.byref(exitTime),
		ctypes.byref(kernel),
		ctypes.byref(user),
	):
		raise ctypes.WinError()
	toTicks = lambda value: (value.high << 32) | value.low
	return (toTicks(kernel) + toTicks(user)) / 10_000_000

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "addon" / "synthDrivers" / "samsungGalaxyVoices.py"
sys.path.insert(0, str(ROOT / "addon"))
FIXTURE = os.environ.get("SAMSUNG_GALAXY_TEST_FIXTURE")
if not FIXTURE:
	raise unittest.SkipTest("Proprietary Samsung integration fixture is not configured")


class Notification:
	def __init__(self):
		self.event = threading.Event()

	def notify(self, **kwargs):
		self.event.set()


class Player:
	instances = []

	def __init__(self, *args, **kwargs):
		self.first_feed = threading.Event()
		self.bytes = 0
		self.data = bytearray()
		self.idle_calls = 0
		Player.instances.append(self)

	def feed(self, data, onDone=None):
		if not isinstance(data, bytes):
			raise RuntimeError(f"audio must be immutable bytes, got {type(data).__name__}")
		self.bytes += len(data)
		self.data.extend(data)
		if data:
			self.first_feed.set()
		if onDone is not None:
			onDone()

	def idle(self):
		self.idle_calls += 1

	def stop(self):
		pass

	def close(self):
		pass

	def pause(self, switch):
		pass


class BaseSynth:
	class VoiceSetting:
		def __init__(self, **kwargs): pass

	class RateSetting:
		def __init__(self, **kwargs): pass

	class PitchSetting:
		def __init__(self, **kwargs): pass

	class VolumeSetting:
		def __init__(self, **kwargs): pass

	def __init__(self):
		pass

	def terminate(self):
		pass


class VoiceInfo:
	def __init__(self, identifier, name, language=None):
		self.id = identifier
		self.name = name
		self.language = language


class Log:
	def _write(self, level, *args):
		if os.environ.get("SAMSUNG_GALAXY_TEST_TRACE") == "1":
			print(level, *args)

	def debug(self, *args, **kwargs):
		self._write("DEBUG", *args)

	def debugWarning(self, *args, **kwargs):
		self._write("WARNING", *args)

	def error(self, *args, **kwargs):
		self._write("ERROR", *args)


config = types.ModuleType("config")
config.conf = {"audio": {"outputDevice": "default"}}
sys.modules["config"] = config
global_vars = types.ModuleType("globalVars")
global_vars.appArgs = types.SimpleNamespace(configPath=str(DRIVER.parent / "test-user-config"))
sys.modules["globalVars"] = global_vars
log_handler = types.ModuleType("logHandler")
log_handler.log = Log()
sys.modules["logHandler"] = log_handler
nvwave = types.ModuleType("nvwave")
nvwave.WavePlayer = Player
sys.modules["nvwave"] = nvwave
commands = types.ModuleType("speech.commands")
class IndexCommand:
	def __init__(self, index):
		self.index = index


class PitchCommand:
	def __init__(self, offset=0):
		self.offset = offset


commands.IndexCommand = IndexCommand
commands.PitchCommand = PitchCommand
sys.modules["speech"] = types.ModuleType("speech")
sys.modules["speech.commands"] = commands
synth = types.ModuleType("synthDriverHandler")
synth.SynthDriver = BaseSynth
synth.VoiceInfo = VoiceInfo
synth.synthDoneSpeaking = Notification()
synth.synthIndexReached = Notification()
sys.modules["synthDriverHandler"] = synth

from synthDrivers._samsungGalaxyVoices import voiceStore as voice_store
data_dir = ROOT / "addon" / "synthDrivers" / "_samsungGalaxyVoices"
fixture_dir = Path(FIXTURE)
custom_voice_path = os.environ.get("SAMSUNG_GALAXY_TEST_VOICE")
custom_voice_id = os.environ.get("SAMSUNG_GALAXY_TEST_VOICE_ID", "en_IN_l02_s24")
custom_generation = os.environ.get("SAMSUNG_GALAXY_TEST_GENERATION", "legacy")


def fixtureVoice(identifier):
	if custom_voice_path and identifier == custom_voice_id:
		return {
			"name": os.environ.get("SAMSUNG_GALAXY_TEST_VOICE_NAME", "Integration test voice"),
			"path": custom_voice_path,
			"enginePath": os.environ["SAMSUNG_GALAXY_TEST_ENGINE"],
			"family": os.environ.get("SAMSUNG_GALAXY_TEST_FAMILY", "l"),
			"speaker": int(os.environ.get("SAMSUNG_GALAXY_TEST_SPEAKER", "2")),
			"language": os.environ.get("SAMSUNG_GALAXY_TEST_LANGUAGE", "en_IN"),
			"generation": custom_generation,
		}
	voicePath = fixture_dir / "voices" / identifier.replace("_", "-").lower()
	metadata = json.loads((voicePath / "voice.json").read_text(encoding="utf-8"))
	return {
		"name": metadata["name"],
		"path": str(voicePath),
		"enginePath": str(fixture_dir / "engines" / metadata["engineHash"] / "libsamsungtts.so"),
		"family": metadata["family"],
		"speaker": int(metadata["speaker"]),
		"language": metadata["language"],
		"generation": metadata.get("generation", "legacy"),
	}


initial_voice_id = custom_voice_id if custom_voice_path else "en_GB_l02"
voice_store.loadVoiceDefinitions = lambda: OrderedDict(((initial_voice_id, fixtureVoice(initial_voice_id)),))
compactFixture = (Path(fixtureVoice(initial_voice_id)["path"]) / "assets" / "tiny.ivc").is_file()


def assertReplacementAudio(reference, replacement, context):
	if not compactFixture:
		if replacement != reference:
			raise RuntimeError(f"{context} contains stale or incorrect speech")
		return
	# Compact models vary their timing between otherwise identical renders.
	ratio = len(replacement) / len(reference) if reference else 0
	if not replacement or not 0.5 <= ratio <= 1.5:
		raise RuntimeError(f"{context} has an unexpected duration")

spec = importlib.util.spec_from_file_location("samsungGalaxyVoices", DRIVER)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if os.environ.get("SAMSUNG_GALAXY_TEST_HOST"):
	module._HOST_PATH = os.environ["SAMSUNG_GALAXY_TEST_HOST"]


class BlockingFirstPutQueue:
	def __init__(self):
		self.entered = threading.Event()
		self.release = threading.Event()
		self.items = []

	def put(self, item):
		if not self.items:
			self.entered.set()
			if not self.release.wait(2):
				raise RuntimeError("test queue insertion was not released")
		self.items.append(item)


def verifyTerminalFramePublicationOrder():
	host = module._SamsungHost()
	process = types.SimpleNamespace(stdout=io.BytesIO(b"C\x00\x00\x00\x00"))
	messages = BlockingFirstPutQueue()
	host._process = process
	host._messages = messages
	reader = threading.Thread(target=host._reader, args=(process,), daemon=True)
	reader.start()
	if not messages.entered.wait(2):
		raise RuntimeError("terminal frame did not reach the message queue")
	eventWasEarly = host._terminalEvent.is_set()
	messages.release.set()
	reader.join(2)
	if eventWasEarly:
		raise RuntimeError("terminal event became visible before its frame was queued")
	if not host._terminalEvent.is_set():
		raise RuntimeError("terminal event was not set after its frame was queued")


verifyTerminalFramePublicationOrder()
driver = module.SynthDriver()
driver.setPlaybackBufferMilliseconds(
	250 if os.environ.get("SAMSUNG_GALAXY_TEST_BUFFERED") == "1" else 0
)

if [driver._nativeRate(value) for value in (0, 50, 100)] != [50, 100, 1000]:
	raise RuntimeError("NVDA rate endpoints do not map to the intended Samsung range")
rateSteps = [driver._nativeRate(value) for value in range(0, 101, 5)]
if any(first >= second for first, second in zip(rateSteps, rateSteps[1:])):
	raise RuntimeError("Samsung rate mapping is not strictly increasing at NVDA's five-point steps")

events = driver._buildEvents([
	IndexCommand(1),
	"A sentence that wraps ",
	IndexCommand(2),
	"onto another visual line.",
	IndexCommand(3),
])
if events != [
	("index", 1, 0, ()),
	("text", "A sentence that wraps onto another visual line.", 0, (2, 3)),
]:
	raise RuntimeError(f"Say All text was not grouped across indexes: {events!r}")

try:
	replacementText = "Replacement speech is responsive and correct."
	for host in (driver._host, driver._standbyHost):
		host.start(driver._voice)
	initial_pids = (driver._host._process.pid, driver._standbyHost._process.pid)
	if custom_generation == "s24":
		class PolicyHost:
			def __init__(self, pid):
				self.pid = pid
				self.engineVersion = 512_506_251

			def start(self, voice):
				pass

		recoveringHost = PolicyHost(70001)
		coldHost = PolicyHost(70002)
		originalHostFactory = module._SamsungHost
		with driver._hostAvailable:
			savedHost = driver._host
			savedStandbyHost = driver._standbyHost
			savedRetiringHosts = driver._retiringHosts
			driver._host = None
			driver._standbyHost = None
			driver._retiringHosts = {recoveringHost}
		module._SamsungHost = lambda: coldHost

		def completeDelayedRecovery():
			time.sleep(0.6)
			with driver._hostAvailable:
				driver._retiringHosts.discard(recoveringHost)
				if driver._host is None:
					driver._host = recoveringHost
				else:
					driver._standbyHost = recoveringHost
				driver._hostAvailable.notify_all()

		recoveryThread = threading.Thread(target=completeDelayedRecovery)
		recoveryThread.start()
		try:
			selectedHost = driver._getHost(driver._voice)
			if selectedHost is not recoveringHost:
				raise RuntimeError("S24 recovery policy cold-started before a reusable helper returned")
		finally:
			recoveryThread.join()
			module._SamsungHost = originalHostFactory
			with driver._hostAvailable:
				driver._host = savedHost
				driver._standbyHost = savedStandbyHost
				driver._retiringHosts = savedRetiringHosts
				driver._hostAvailable.notify_all()
	synth.synthDoneSpeaking.event.clear()
	driver.speak(["A completed utterance should leave both warm engines in place."])
	if not synth.synthDoneSpeaking.event.wait(10):
		raise RuntimeError("completed utterance did not finish")
	player = Player.instances[-1]
	if player.idle_calls:
		raise RuntimeError("render drained the audio queue between utterances")
	player.first_feed.clear()
	synth.synthDoneSpeaking.event.clear()
	started = time.perf_counter()
	driver.cancel()
	driver.speak(["Routine speech reuses the already warm engine."])
	if not player.first_feed.wait(5):
		raise RuntimeError("routine replacement produced no audio")
	routine_latency = (time.perf_counter() - started) * 1000
	current_pids = (driver._host._process.pid, driver._standbyHost._process.pid)
	print(f"routine first-audio latency: {routine_latency:.1f} ms; pids={current_pids}")
	if current_pids != initial_pids:
		raise RuntimeError("routine cancellation replaced a warm helper")
	if not synth.synthDoneSpeaking.event.wait(10):
		raise RuntimeError("routine replacement did not finish")
	referencePlayer = Player.instances[-1]
	referenceOffset = len(referencePlayer.data)
	synth.synthDoneSpeaking.event.clear()
	driver.speak([replacementText])
	if not synth.synthDoneSpeaking.event.wait(10):
		raise RuntimeError("reference replacement did not finish")
	referenceAudio = bytes(referencePlayer.data[referenceOffset:])
	if not referenceAudio:
		raise RuntimeError("reference replacement produced no audio")
	Player.instances[-1].first_feed.clear()
	synth.synthDoneSpeaking.event.clear()
	driver.speak(["The cancelled sentence must never delay its replacement. " * 3])
	if not Player.instances[-1].first_feed.wait(10):
		raise RuntimeError("initial speech produced no audio")
	started = time.perf_counter()
	driver.cancel()
	driver.speak([replacementText])
	replacement = Player.instances[-1]
	if not replacement.first_feed.wait(5):
		raise RuntimeError("replacement speech produced no audio")
	latency = (time.perf_counter() - started) * 1000
	print(f"replacement first-audio latency: {latency:.1f} ms; bytes={replacement.bytes}")
	if latency > 1000:
		raise RuntimeError("replacement exceeded the one-second test ceiling")
	if not synth.synthDoneSpeaking.event.wait(10):
		raise RuntimeError("replacement did not finish")
	assertReplacementAudio(referenceAudio, bytes(replacement.data), "replacement audio")
	deadline = time.monotonic() + 10
	while driver._standbyHost is None and time.monotonic() < deadline:
		time.sleep(0.05)
	if driver._standbyHost is None:
		raise RuntimeError("interrupted helper was not replaced")
	replacement_pids = {driver._host._process.pid, driver._standbyHost._process.pid}
	print(f"replacement helper pids: {sorted(replacement_pids)}")
	if replacement_pids != set(initial_pids):
		raise RuntimeError("a short interruption replaced an otherwise reusable warm helper")

	rapidIterations = 30
	rapidText = (
		"Amy Green, United Kingdom, female, Galaxy S24 premium voice; status: installed and ready to use."
		if custom_generation == "s24"
		else "a"
	)
	for iteration in range(rapidIterations):
		current_player = Player.instances[-1]
		current_player.first_feed.clear()
		driver.speak([rapidText])
		if not current_player.first_feed.wait(5):
			raise RuntimeError(f"rapid request {iteration + 1} produced no audio")
		if custom_generation == "s24":
			with driver._hostLock:
				activePid = driver._activeHost.pid
			if activePid not in initial_pids:
				raise RuntimeError(
					f"rapid request {iteration + 1} cold-started helper {activePid}; "
					f"expected one of {initial_pids}"
				)
		driver.cancel()
	if custom_generation == "s24":
		for iteration in range(30):
			deadline = time.monotonic() + 5
			while time.monotonic() < deadline:
				with driver._hostLock:
					if driver._synthesizingHost is None:
						break
				time.sleep(0.005)
			driver.speak([rapidText])
			deadline = time.monotonic() + 5
			while time.monotonic() < deadline:
				with driver._hostLock:
					activeHost = driver._synthesizingHost
				if activeHost is not None:
					break
				time.sleep(0.005)
			else:
				raise RuntimeError(f"pre-audio request {iteration + 1} did not start")
			if activeHost.pid not in initial_pids:
				raise RuntimeError(
					f"pre-audio request {iteration + 1} cold-started helper {activeHost.pid}; "
					f"expected one of {initial_pids}"
				)
			driver.cancel()
	final_player = Player.instances[-1]
	final_player.first_feed.clear()
	synth.synthDoneSpeaking.event.clear()
	driver.speak([replacementText])
	if not final_player.first_feed.wait(5):
		raise RuntimeError("final rapid replacement produced no audio")
	if not synth.synthDoneSpeaking.event.wait(10):
		raise RuntimeError("final rapid replacement did not finish")
	assertReplacementAudio(referenceAudio, bytes(final_player.data), "final rapid replacement audio")
	deadline = time.monotonic() + 5
	while (driver._standbyHost is None or driver._retiringHosts) and time.monotonic() < deadline:
		time.sleep(0.05)
	churn_pids = {driver._host._process.pid, driver._standbyHost._process.pid}
	print(f"rapid-churn helper pids: {sorted(churn_pids)}")
	if churn_pids != set(initial_pids):
		raise RuntimeError("rapid interruption churned the warm helper pool")
	if len(churn_pids) != 2:
		raise RuntimeError("rapid interruption did not leave two warm helpers")

	for iteration in range(20):
		driver.speak(["b"])
		deadline = time.monotonic() + 2
		while driver._synthesizingHost is None and time.monotonic() < deadline:
			time.sleep(0.001)
		if driver._synthesizingHost is None:
			raise RuntimeError(f"pre-audio request {iteration + 1} never reached the helper")
		driver.cancel()
	preAudioPlayer = Player.instances[-1]
	preAudioPlayer.first_feed.clear()
	synth.synthDoneSpeaking.event.clear()
	driver.speak(["Speech remains responsive after pre-audio cancellations."])
	if not preAudioPlayer.first_feed.wait(5):
		raise RuntimeError("speech after pre-audio cancellation produced no audio")
	if not synth.synthDoneSpeaking.event.wait(10):
		raise RuntimeError("speech after pre-audio cancellation did not finish")
	deadline = time.monotonic() + 5
	while (driver._standbyHost is None or driver._retiringHosts) and time.monotonic() < deadline:
		time.sleep(0.05)
	preAudioPids = {driver._host._process.pid, driver._standbyHost._process.pid}
	print(f"pre-audio-cancel helper pids: {sorted(preAudioPids)}")
	if preAudioPids != set(initial_pids):
		raise RuntimeError("pre-audio cancellation churned the warm helper pool")
	if len(preAudioPids) != 2:
		raise RuntimeError("pre-audio cancellation did not leave two warm helpers")
	idleProcesses = (driver._host._process, driver._standbyHost._process)
	idleBefore = [processCpuSeconds(process) for process in idleProcesses]
	time.sleep(2)
	idleCpu = [processCpuSeconds(process) - before for process, before in zip(idleProcesses, idleBefore)]
	print(f"two-second idle CPU: {[round(value, 4) for value in idleCpu]}")
	if any(value > 0.1 for value in idleCpu):
		raise RuntimeError("an idle Samsung helper continued consuming CPU")

	if not custom_voice_path:
		voice_store.loadVoiceDefinitions = lambda: OrderedDict((
			("en_GB_l02", fixtureVoice("en_GB_l02")),
			("en_GB_g02", fixtureVoice("en_GB_g02")),
		))
		driver.refreshAvailableVoices()
		if tuple(driver._get_availableVoices()) != ("en_GB_l02", "en_GB_g02"):
			raise RuntimeError("downloaded voice did not enter the live voice list")
		fallbackName = driver.prepareVoicesForRemoval(("en_GB_l02",))
		if fallbackName != fixtureVoice("en_GB_g02")["name"] or driver._get_voice() != "en_GB_g02":
			raise RuntimeError("active voice removal did not select the available fallback")
		voice_store.loadVoiceDefinitions = lambda: OrderedDict((
			("en_GB_g02", fixtureVoice("en_GB_g02")),
		))
		driver.refreshAvailableVoices()
		driver._set_voice("en_GB_l02")
		if driver._get_voice() != "en_GB_g02" or tuple(driver._get_availableVoices()) != ("en_GB_g02",):
			raise RuntimeError("removed voice remained selectable or displaced the speaking fallback")
finally:
	driver.terminate()
