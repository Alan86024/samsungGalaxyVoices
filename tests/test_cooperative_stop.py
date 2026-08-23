import json
import os
import struct
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "addon" / "synthDrivers" / "_samsungGalaxyVoices" / "runtime" / "samsungGalaxyHost.exe"
DATA_ENV = os.environ.get("SAMSUNG_GALAXY_TEST_DATA")
if not DATA_ENV:
	raise unittest.SkipTest("Set SAMSUNG_GALAXY_TEST_DATA to an installed Samsung Galaxy Voices data folder")
DATA = Path(DATA_ENV)
ENGINE = DATA / "engines" / "regular" / "libsamsungtts.so"
VOICE = DATA / "voices" / "en-gb-l02"
ANDROID = ROOT / "addon" / "synthDrivers" / "_samsungGalaxyVoices" / "android"
REPLACEMENT = "Replacement speech should begin immediately and contain none of the cancelled sentence."


def read_exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise RuntimeError("Samsung helper closed its output unexpectedly")
    return data


def read_frame(process):
    kind = read_exact(process.stdout, 1)
    size = struct.unpack("<I", read_exact(process.stdout, 4))[0]
    return kind, read_exact(process.stdout, size)


def write_frame(process, kind, payload=b""):
    process.stdin.write(kind + struct.pack("<I", len(payload)) + payload)
    process.stdin.flush()


def start_host():
    process = subprocess.Popen(
        [str(HOST), "--server", str(ENGINE), str(VOICE), str(ANDROID), "l", "2"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        cwd=HOST.parent,
    )
    kind, payload = read_frame(process)
    if kind != b"R":
        raise RuntimeError(f"expected ready frame, got {kind!r}")
    json.loads(payload)
    return process


def stop_host(process):
    if process.poll() is None:
        write_frame(process, b"Q")
        try:
            process.wait(3)
        except subprocess.TimeoutExpired:
            process.kill()
    if process.returncode:
        raise RuntimeError(f"Samsung helper exited with {process.returncode}")


def speak(process, text):
    write_frame(process, b"S", text.encode())
    audio = bytearray()
    while True:
        kind, payload = read_frame(process)
        if kind == b"A":
            audio.extend(payload)
        elif kind == b"D":
            return bytes(audio)
        elif kind == b"E":
            raise RuntimeError(payload.decode("utf-8", "replace"))
        else:
            raise RuntimeError(f"unexpected frame {kind!r}")


def main(cycles):
    fresh_host = start_host()
    try:
        fresh = speak(fresh_host, REPLACEMENT)
    finally:
        stop_host(fresh_host)
    print(f"Fresh replacement bytes: {len(fresh)}")

    process = start_host()
    try:
        for cycle in range(1, cycles + 1):
            long_text = ("This old sentence must stop before replacement speech. " * 80).strip()
            write_frame(process, b"S", long_text.encode())
            kind, _ = read_frame(process)
            if kind != b"A":
                raise RuntimeError(f"expected first audio frame, got {kind!r}")
            started = time.perf_counter()
            write_frame(process, b"X")
            trailing_frames = 0
            trailing_bytes = 0
            while True:
                kind, payload = read_frame(process)
                if kind == b"A":
                    trailing_frames += 1
                    trailing_bytes += len(payload)
                    continue
                if kind == b"E":
                    raise RuntimeError(payload.decode("utf-8", "replace"))
                if kind != b"C":
                    raise RuntimeError(f"expected cancel frame, got {kind!r}")
                break
            elapsed_ms = (time.perf_counter() - started) * 1000
            replacement = speak(process, REPLACEMENT)
            delta = abs(len(replacement) - len(fresh))
            print(
                f"Cycle {cycle}: cancel {elapsed_ms:.1f} ms; "
                f"trailing {trailing_frames} frames/{trailing_bytes} bytes; "
                f"replacement {len(replacement)} bytes; delta {delta}"
            )
            if delta > 4096:
                raise RuntimeError("replacement audio differs materially from a fresh render")
    finally:
        stop_host(process)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
