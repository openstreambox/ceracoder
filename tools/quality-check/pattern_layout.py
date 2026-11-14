"""Layout of the quality test pattern, shared by the pipeline generator and the
analyser. Coordinates are for 1920x1080; the analyser scales them to the
decoded size."""

REF_W, REF_H = 1920, 1080
FPS = 30

# Frame counter: 20 bits, least significant bit on the left. Each bit is a
# pair of blocks (value, then its inverse), so the far end compares the two
# halves instead of guessing a brightness threshold.
FRAME_BITS = 20
FRAME_HALF = 48
FRAME_Y, FRAME_H = 1008, 64

# Pattern clock: wall-clock time of frame 0 in Unix milliseconds, 42 bits in
# two rows of 21 (same pair coding). Static for the whole run; the encoder
# sets it before the pipeline starts. Frame n was made at epoch + n/fps.
EPOCH_BITS = 42
EPOCH_PER_ROW = 21
EPOCH_HALF = 44
EPOCH_ROWS_Y = (864, 936)
EPOCH_H = 56

# Second marker: white for the first half of each second. The beep is centred
# on the moment it turns white (GStreamer centres each tick on its time).
MARK_X, MARK_Y, MARK_W, MARK_H = 1680, 624, 192, 192

# Moving content and noise.
BALL_Y, BALL_H = 600, 240
NOISE_X, NOISE_Y, NOISE_W, NOISE_H = 1600, 0, 320, 180

# Static area used to spot broken pictures (inside the colour bars, away from
# the text, the noise patch and the ball band).
STATIC_X, STATIC_Y, STATIC_W, STATIC_H = 0, 120, 1584, 460

# Sound: left 997 Hz, right 1499 Hz, both at -18 dBFS (sine peak); a 2 kHz,
# 50 ms beep at -12 dBFS on both channels, centred on each whole second.
LEFT_HZ, RIGHT_HZ, BEEP_HZ = 997, 1499, 2000
TONE_AMP = 0.125893
BEEP_AMP = 0.251189
BEEP_MS = 50
