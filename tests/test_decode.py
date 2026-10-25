import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

# Captured from a PB1150-class grill (PBA board) holding toward 350°F with no probes plugged in.
PBA_STATE = {
    "sc_12": "FE0C00000000000009060009060009060009060009060003050003010101FF",
    "sc_11": "FE0B000000000000090600090600090600090600090600030008020100000000000000000001000100000100000000FF",
}


class DecodeTest(unittest.TestCase):
    def test_real_pba_frames(self):
        d = server.decode(PBA_STATE, server.BOARDS["PBA"])
        self.assertEqual(d["set"], 350)
        self.assertEqual(d["grill"], 311)
        self.assertTrue(d["fahrenheit"])
        self.assertIsNone(d["p1"])
        self.assertIsNone(d["p1Target"])
        self.assertTrue(d["on"])
        self.assertTrue(d["fan"])
        self.assertTrue(d["auger"])
        self.assertFalse(d["igniter"])
        self.assertEqual(d["errors"], [])

    def test_round_trip_every_board(self):
        for name, board in server.BOARDS.items():
            with self.subTest(board=name):
                vals = {"on": True, "fan": True, "auger": True, "light": False, "grill": 227, "set": 225,
                        "p1": 161, "errors": ["Out of pellets"]}
                if "p1Target" in board["temps"]:
                    vals["p1Target"] = 203
                d = server.decode(server.encode(vals, board), board)
                for k, v in vals.items():
                    self.assertEqual(d[k], v, k)

    def test_short_or_garbage_frames_are_ignored(self):
        board = server.BOARDS["PBA"]
        self.assertEqual(server.decode({"sc_11": "", "sc_12": "FE0C01FF"}, board), {})
        self.assertEqual(server.decode({"sc_12": "not hex"}, board), {})


class StatsTest(unittest.TestCase):
    def test_lid_event_detected_and_setpoint_change_ignored(self):
        rows, t = [], 0
        for g, s in [(225, 225)] * 5 + [(200, 225), (205, 225), (222, 225)] + [(225, 275), (240, 275), (271, 275)]:
            rows.append({"t": t, "grill": g, "set": s})
            t += 20
        events, open_now = server.lid_events(rows)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["drop"], 25)
        self.assertIsNone(open_now)


try:
    import cv2
    import numpy
except ImportError:
    cv2 = None


@unittest.skipUnless(cv2, "OpenCV not installed")
class QrTest(unittest.TestCase):
    def test_codes_decode(self):
        import qr
        det = cv2.QRCodeDetector()
        for text in ("http://192.168.1.7:8880", "http://10.0.0.123:65535/", "x" * 106):
            with self.subTest(text=text):
                m = numpy.array([[0 if d else 255 for d in row] for row in qr.make(text)], dtype=numpy.uint8)
                m = numpy.pad(m, 4, constant_values=255)
                img = cv2.resize(m, (m.shape[1] * 8, m.shape[0] * 8), interpolation=cv2.INTER_NEAREST)
                self.assertEqual(det.detectAndDecode(img)[0], text)


if __name__ == "__main__":
    unittest.main()
