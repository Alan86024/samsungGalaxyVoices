import json
import struct
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "addon" / "synthDrivers" / "_samsungGalaxyVoices"
HOST = DATA / "runtime" / "samsungGalaxyHost.exe"
ANDROID = DATA / "android"


def read_exact(stream, size):
	data = stream.read(size)
	if len(data) != size:
		raise RuntimeError("host closed unexpectedly")
	return data


def read_frame(process):
	kind = read_exact(process.stdout, 1)
	size = struct.unpack("<I", read_exact(process.stdout, 4))[0]
	return kind, read_exact(process.stdout, size)


def write_frame(process, kind, payload=b""):
	process.stdin.write(kind + struct.pack("<I", len(payload)) + payload)
	process.stdin.flush()


def main(voice_path, engine_path, family, speaker, output_path=None, native_rate=None):
	process = subprocess.Popen(
		[str(HOST), "--server", str(engine_path), str(voice_path), str(ANDROID), family, str(speaker)],
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		cwd=HOST.parent,
	)
	try:
		kind, payload = read_frame(process)
		if kind != b"R":
			raise RuntimeError(payload.decode("utf-8", "replace"))
		metadata = json.loads(payload)
		if native_rate is not None:
			write_frame(process, b"P", struct.pack("<ii", native_rate, 100))
			kind, payload = read_frame(process)
			if kind != b"K":
				raise RuntimeError(payload.decode("utf-8", "replace"))
		write_frame(process, b"S", b"This is a Samsung voice download test.")
		audio = bytearray()
		while True:
			kind, payload = read_frame(process)
			if kind == b"A":
				audio.extend(payload)
			elif kind == b"D":
				break
			elif kind == b"E":
				raise RuntimeError(payload.decode("utf-8", "replace"))
		if output_path is not None:
			with wave.open(str(output_path), "wb") as output:
				output.setnchannels(1)
				output.setsampwidth(2)
				output.setframerate(metadata["sampleRate"])
				output.writeframes(audio)
		print(f"sampleRate={metadata['sampleRate']} audioBytes={len(audio)}")
	finally:
		if process.poll() is None:
			try:
				write_frame(process, b"Q")
			except OSError:
				pass
			process.wait(5)


if __name__ == "__main__":
	main(
		Path(sys.argv[1]),
		Path(sys.argv[2]),
		sys.argv[3],
		int(sys.argv[4]),
		Path(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] != "-" else None,
		int(sys.argv[6]) if len(sys.argv) > 6 else None,
	)
