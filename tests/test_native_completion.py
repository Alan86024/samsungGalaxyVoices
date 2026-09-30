"""Opt-in successful silent-input checks against an isolated native helper."""

import unittest

from test_cancellation_latency import NativeHost, REFERENCE_TEXT, VOICE


class NativeCompletionTests(unittest.TestCase):
    def test_silent_success_does_not_poison_the_next_request(self):
        host = NativeHost()
        try:
            reference, _, _ = host.speak(REFERENCE_TEXT)
            for text in ("?", ".", "!"):
                with self.subTest(text=text):
                    host.send(b"S", text.encode())
                    while True:
                        kind, _ = host.receive()
                        if kind == b"D":
                            break
                        self.assertEqual(b"A", kind)
                    replacement, _, _ = host.speak(REFERENCE_TEXT)
                    if (VOICE / "assets" / "tiny.ivc").is_file():
                        self.assertTrue(0.5 <= len(replacement) / len(reference) <= 1.5)
                    else:
                        self.assertEqual(reference, replacement)
        finally:
            host.close()


if __name__ == "__main__":
    unittest.main()
