import unittest
from fractions import Fraction

from dji_goggles_bridge import decode_h264

try:
    import av
    import numpy as np
except ImportError:
    av = np = None


@unittest.skipIf(av is None or np is None, "Optional decode dependencies not installed")
class DecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        encoder = av.CodecContext.create("libx264", "w")
        encoder.width, encoder.height = 64, 48
        encoder.pix_fmt = "yuv420p"
        encoder.time_base = Fraction(1, 30)
        encoder.options = {"preset": "ultrafast", "tune": "zerolatency"}
        pixels = np.empty((48, 64, 3), dtype=np.uint8)
        pixels[:] = (210, 110, 30)  # RGB: obvious difference between red and blue.
        output = []
        for i in range(3):
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            frame.pts = i
            output.extend(bytes(packet) for packet in encoder.encode(frame))
        output.extend(bytes(packet) for packet in encoder.encode(None))
        cls.stream = b"".join(output)

    def test_arbitrary_byte_chunking_and_flush_preserve_all_pictures(self):
        chunks = (self.stream[i : i + 17] for i in range(0, len(self.stream), 17))
        frames = list(decode_h264(chunks))
        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[0].shape, (48, 64, 3))
        self.assertEqual(frames[0].dtype, np.uint8)
        frames[1][:] = 0
        self.assertTrue(frames[0].any(), "Returned images must remain independently usable")

    def test_rgb_bgr_and_gray_contract(self):
        rgb = list(decode_h264([self.stream], format="rgb24"))[0]
        bgr = list(decode_h264([self.stream], format="bgr24"))[0]
        gray = list(decode_h264([self.stream], format="gray"))[0]
        np.testing.assert_array_equal(rgb[..., ::-1], bgr)
        self.assertGreater(float(rgb[..., 0].mean()), float(rgb[..., 2].mean()) + 100)
        self.assertEqual(gray.shape, (48, 64))

    def test_invalid_pixel_format(self):
        with self.assertRaises(ValueError):
            next(decode_h264([], format="unknown"))
