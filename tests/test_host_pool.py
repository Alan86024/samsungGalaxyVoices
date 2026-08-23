import json
import os
import struct
import subprocess
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "addon" / "synthDrivers" / "_samsungGalaxyVoices"
FIXTURE = os.environ.get("SAMSUNG_GALAXY_TEST_FIXTURE")
if not FIXTURE:
	raise unittest.SkipTest("Proprietary Samsung integration fixture is not configured")
FIXTURE = Path(FIXTURE)
HOST = DATA / "runtime" / "samsungGalaxyHost.exe"
ENGINE = Path(os.environ.get(
	"SAMSUNG_GALAXY_TEST_ENGINE",
	FIXTURE / "engines" / "regular" / "libsamsungtts.so",
))
VOICE = Path(os.environ.get(
	"SAMSUNG_GALAXY_TEST_VOICE",
	FIXTURE / "voices" / "en-gb-l02",
))
FAMILY = os.environ.get("SAMSUNG_GALAXY_TEST_FAMILY", "l")
SPEAKER = os.environ.get("SAMSUNG_GALAXY_TEST_SPEAKER", "2")
ANDROID = DATA / "android"


def read_exact(stream, size):
	data = stream.read(size)
	if len(data) != size:
		raise RuntimeError("helper closed unexpectedly")
	return data


def read_frame(process):
	kind = read_exact(process.stdout, 1)
	size = struct.unpack("<I", read_exact(process.stdout, 4))[0]
	return kind, read_exact(process.stdout, size)


def send(process, kind, payload=b""):
	process.stdin.write(kind + struct.pack("<I", len(payload)) + payload)
	process.stdin.flush()


def start():
	process = subprocess.Popen(
		[str(HOST), "--server", str(ENGINE), str(VOICE), str(ANDROID), FAMILY, SPEAKER],
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		cwd=HOST.parent,
	)
	kind, payload = read_frame(process)
	if kind != b"R":
		raise RuntimeError((kind, payload))
	json.loads(payload)
	return process


def speak(process, text):
	send(process, b"P", struct.pack("<ii", 100, 100))
	if read_frame(process)[0] != b"K":
		raise RuntimeError("parameter update failed")
	send(process, b"S", text.encode())
	audio = 0
	while True:
		kind, payload = read_frame(process)
		if kind == b"A":
			audio += len(payload)
		elif kind == b"D":
			return audio
		elif kind == b"E":
			raise RuntimeError(payload.decode(errors="replace"))


def stop(process, force=False):
	if process.poll() is None:
		if force:
			process.kill()
		else:
			send(process, b"Q")
		try:
			process.wait(3)
		except subprocess.TimeoutExpired:
			process.kill()
			process.wait()


first = start()
second = start()
try:
	send(first, b"P", struct.pack("<ii", 100, 100))
	assert read_frame(first)[0] == b"K"
	send(first, b"S", ("The first engine must be interrupted immediately. " * 100).encode())
	assert read_frame(first)[0] == b"A"
	stop(first, force=True)
	started = time.perf_counter()
	bytes_rendered = speak(second, "The standby engine is speaking the replacement sentence.")
	print(f"standby first audio completed in {(time.perf_counter() - started) * 1000:.1f} ms; bytes={bytes_rendered}")
finally:
	stop(first, force=True)
	stop(second)
