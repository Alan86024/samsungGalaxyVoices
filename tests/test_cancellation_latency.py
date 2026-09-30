"""Opt-in native cancellation measurements; no NVDA session or playback involved."""

import argparse
from collections import deque
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
DATA = os.environ.get("SAMSUNG_GALAXY_TEST_DATA")
if not DATA:
    raise unittest.SkipTest("Set SAMSUNG_GALAXY_TEST_DATA to an installed voice fixture")
DATA = Path(DATA)
VOICE = Path(os.environ["SAMSUNG_GALAXY_TEST_VOICE"])
METADATA = json.loads((VOICE / "voice.json").read_text(encoding="utf-8"))
ENGINE = DATA / "engines" / METADATA["engineHash"] / "libsamsungtts.so"
HOST = Path(os.environ.get("SAMSUNG_GALAXY_TEST_HOST", ROOT / "addon" /
    "synthDrivers" / "_samsungGalaxyVoices" / "runtime" / "samsungGalaxyHost.exe"))
ANDROID = ROOT / "addon" / "synthDrivers" / "_samsungGalaxyVoices" / "android"
FRAME_LENGTH = struct.Struct("<I")
REFERENCE_TEXT = "Replacement speech contains only the new request."


class FileTime(ctypes.Structure):
    _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))


class MemoryCounters(ctypes.Structure):
    _fields_ = (("size", ctypes.c_ulong), ("page_faults", ctypes.c_ulong)) + tuple(
        (name, ctypes.c_size_t) for name in (
            "peak_working_set", "working_set", "peak_paged_pool", "paged_pool",
            "peak_nonpaged_pool", "nonpaged_pool", "pagefile", "peak_pagefile", "private_bytes",
        )
    )


def resource_usage(process):
    counters = MemoryCounters()
    counters.size = ctypes.sizeof(counters)
    if not ctypes.windll.psapi.GetProcessMemoryInfo(process._handle, ctypes.byref(counters), counters.size):
        raise ctypes.WinError()
    handles = ctypes.c_ulong()
    if not ctypes.windll.kernel32.GetProcessHandleCount(process._handle, ctypes.byref(handles)):
        raise ctypes.WinError()
    return {"privateMiB": round(counters.private_bytes / (1 << 20), 2),
        "workingSetMiB": round(counters.working_set / (1 << 20), 2), "handles": handles.value}


def cpu_seconds(process):
    values = [FileTime() for _ in range(4)]
    if not ctypes.windll.kernel32.GetProcessTimes(process._handle, *(
        ctypes.byref(value) for value in values
    )):
        raise ctypes.WinError()
    return sum((value.high << 32) | value.low for value in values[2:]) / 10_000_000


class NativeHost:
    def __init__(self):
        self.frames = queue.Queue()
        self.diagnostics = deque(maxlen=20)
        self.process = subprocess.Popen(
            [str(HOST), "--server", str(ENGINE), str(VOICE), str(ANDROID),
                str(METADATA["family"]), str(METADATA["speaker"])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=HOST.parent, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.readers = [
            threading.Thread(target=self._read_frames, daemon=True),
            threading.Thread(target=self._read_diagnostics, daemon=True),
        ]
        for reader in self.readers:
            reader.start()
        try:
            kind, payload = self.receive(15)
            if kind != b"R":
                raise RuntimeError(f"helper was not ready: {kind!r}: {payload!r}")
            self.metadata = json.loads(payload)
        except BaseException:
            self.close()
            raise

    def _read_exact(self, size):
        result = bytearray()
        while len(result) < size:
            part = self.process.stdout.read(size - len(result))
            if not part:
                raise EOFError("native helper output closed")
            result.extend(part)
        return bytes(result)

    def _read_frames(self):
        try:
            while True:
                kind = self._read_exact(1)
                size = FRAME_LENGTH.unpack(self._read_exact(4))[0]
                if size > 16 << 20:
                    raise RuntimeError("invalid native frame size")
                self.frames.put((kind, self._read_exact(size)))
        except Exception as error:
            self.frames.put((None, str(error).encode()))

    def _read_diagnostics(self):
        while line := self.process.stderr.readline(1024):
            self.diagnostics.append(line.decode("utf-8", "replace").strip())

    def receive(self, timeout=10):
        try:
            kind, payload = self.frames.get(timeout=timeout)
        except queue.Empty as error:
            raise RuntimeError(f"helper timeout; diagnostics={list(self.diagnostics)!r}") from error
        if kind in (b"E", None):
            raise RuntimeError(f"helper failure: {payload!r}; {list(self.diagnostics)!r}")
        return kind, payload

    def send(self, kind, payload=b""):
        self.process.stdin.write(kind + FRAME_LENGTH.pack(len(payload)) + payload)
        self.process.stdin.flush()

    def speak(self, text):
        started = time.perf_counter()
        self.send(b"S", text.encode("utf-8"))
        audio = bytearray()
        first_ms = None
        frame_sizes = []
        while True:
            kind, payload = self.receive()
            if kind == b"A":
                if first_ms is None:
                    first_ms = (time.perf_counter() - started) * 1000
                audio.extend(payload)
                frame_sizes.append(len(payload))
            elif kind == b"D":
                if not audio:
                    raise RuntimeError("completed request produced no audio")
                return bytes(audio), first_ms, sorted(set(frame_sizes))
            else:
                raise RuntimeError(f"unexpected synthesis frame {kind!r}")

    def close(self):
        if self.process.poll() is None:
            try:
                self.send(b"Q")
                self.process.wait(3)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(3)
        for reader in self.readers:
            reader.join(1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


def measure(cycles, short_text=False):
    host = NativeHost()
    try:
        reference, first_ms, sizes = host.speak(REFERENCE_TEXT)
        print(json.dumps({"ready": host.metadata, "referenceHash": hashlib.sha256(reference).hexdigest(),
            "referenceBytes": len(reference), "referenceFirstMs": round(first_ms, 2),
            "frameBytes": sizes, "resources": resource_usage(host.process)}), flush=True)
        phases = ("before-audio",) if short_text else ("before-audio", "after-audio")
        for phase in phases:
            for cycle in range(cycles):
                obsolete = "a" if short_text else "This obsolete sentence must never reach replacement speech. " * 80
                host.send(b"S", obsolete.encode())
                if phase == "after-audio":
                    if host.receive()[0] != b"A":
                        raise RuntimeError("missing initial audio")
                else:
                    time.sleep(0.01)
                started = time.perf_counter()
                cpu_before = cpu_seconds(host.process)
                host.send(b"X")
                discarded = 0
                while True:
                    kind, payload = host.receive()
                    if kind == b"A":
                        discarded += len(payload)
                    elif kind in (b"C", b"D"):
                        break
                    else:
                        raise RuntimeError(f"unexpected cancellation frame {kind!r}")
                cancel_ms = (time.perf_counter() - started) * 1000
                cancel_cpu_ms = (cpu_seconds(host.process) - cpu_before) * 1000
                # A short request can finish before the stop reaches it. Use
                # the parameter acknowledgement as a barrier before reuse.
                host.send(b"P", struct.pack("<ii", 100, 100))
                while True:
                    barrier_kind, _ = host.receive()
                    if barrier_kind == b"K":
                        break
                    if barrier_kind != b"C":
                        raise RuntimeError(f"unexpected barrier frame {barrier_kind!r}")
                replacement, replacement_ms, _ = host.speak(REFERENCE_TEXT)
                if (VOICE / "assets" / "tiny.ivc").is_file():
                    if not 0.5 <= len(replacement) / len(reference) <= 1.5:
                        raise RuntimeError("compact replacement duration changed")
                elif replacement != reference:
                    raise RuntimeError("replacement audio differs from the clean reference")
                print(json.dumps({"phase": phase, "cycle": cycle + 1,
                    "cancelMs": round(cancel_ms, 2), "cancelCpuMs": round(cancel_cpu_ms, 2),
                    "replacementFirstMs": round(replacement_ms, 2), "discardedBytes": discarded,
                    "diagnostics": list(host.diagnostics)[-1:],
                    "resources": resource_usage(host.process)}), flush=True)
        before = cpu_seconds(host.process)
        time.sleep(2)
        print(json.dumps({"idleCpuMs": round((cpu_seconds(host.process) - before) * 1000, 2)}), flush=True)
    finally:
        host.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--short-text", action="store_true", help="Cancel a single character before audio starts")
    arguments = parser.parse_args()
    measure(arguments.cycles, arguments.short_text)
