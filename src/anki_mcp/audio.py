"""16-bit mono PCM: cutting a word out, and measuring its final release.

Pure standard library -- there is no ffmpeg in the image, and none is needed:
ElevenLabs returns raw PCM on request, so a clip can be cut, measured and
wrapped as WAV without decoding anything.

The measurement exists for word-final devoicing. A final stop is a closure
(near-silence) followed by a short high-frequency burst, and that burst is what
tells [bɛʁk] from [bɛʁ]. A native speaker's [k] in "Berg" (Commons
De-Berg2.ogg) peaks 9 dB below the vowel; the TTS voice's lands anywhere from
11 to 28 dB below it, varying by 8 dB between identical requests.
`release_db` scores a take so the best one can be kept.
"""

from __future__ import annotations

import io
import math
import wave
from array import array
from functools import reduce
from dataclasses import dataclass

SAMPLE_RATE = 24000
WINDOW_MS = 10
WINDOW = SAMPLE_RATE * WINDOW_MS // 1000

# Silence appended to every word clip, so no player can swallow the release
# along with the end of the file.
TAIL_PADDING_SECONDS = 0.3
# A cut through non-silent audio clicks, and a click after a word reads as a
# stop -- a false [t] on exactly the words this deck is drilling.
FADE_SECONDS = 0.01

# Without alignment: the last window this close to peak loudness is taken as
# the end of the vowel.
VOWEL_BELOW_PEAK_DB = 12.0
# A window counts as sound when it is within this of the peak.
SOUND_BELOW_PEAK_DB = 45.0
# The last high-frequency event (burst or fricative) within this of the HF peak.
LAST_EVENT_BELOW_HF_PEAK_DB = 25.0
# Closure duration: windows within this of the quietest point.
CLOSURE_SPAN_DB = 6.0
# Final hiss duration: windows within this of the final segment's loudest.
HISS_SPAN_DB = 12.0
# Release duration: windows within this of the release's own peak.
RELEASE_SPAN_DB = 15.0


@dataclass(frozen=True)
class Release:
    closure_depth_db: float
    closure_ms: int
    # The release's high-frequency peak relative to the word's loudest
    # (vowel) moment. Referenced to the vowel, not to the word's own
    # high-frequency peak: that peak is the [s] of "das" or the aspirated [tʰ]
    # of "Tag" in half the deck, which would cap those words' scores however
    # good their final stop. A native [k] in "Berg" measures about -9.
    release_db: float
    release_ms: int


def window_db(samples) -> list[float]:
    windows = [samples[start:start + WINDOW]
               for start in range(0, len(samples) - WINDOW + 1, WINDOW)]
    return [
        20 * math.log10(max(math.sqrt(sum(value * value for value in window) / len(window)), 1) / 32768)
        for window in windows
    ]


def high_passed(samples) -> list[int]:
    """Second difference: +12 dB/octave, so voicing drops out and bursts stay."""
    return [samples[index] - 2 * samples[index - 1] + samples[index - 2]
            for index in range(2, len(samples))]


def final_release(samples, final_segment_start: float | None = None) -> Release | None:
    """The word-final closure and release.

    Searching the whole clip finds the silence BEFORE the word just as happily
    as the closure at its end, and then reports the entire word as a
    "release". So the search starts where the final consonant does:
    `final_segment_start` (seconds into `samples`) comes from the aligner when
    there is one. Without it, the last window near peak loudness is taken as
    the end of the vowel -- a guess that mistakes a loud burst for the vowel,
    which is why the selection always passes the alignment. The closure is the
    quietest point between there and the last high-frequency event; the
    release is everything after it. A final fricative ("Los") has no real
    closure and shows as a shallow one.
    """
    broadband = window_db(samples)
    treble = window_db(high_passed(samples))
    if not broadband or not treble:
        return None
    peak = max(broadband)
    treble_peak = max(treble)
    vowel_end = (
        max(index for index, level in enumerate(broadband)
            if level >= peak - VOWEL_BELOW_PEAK_DB)
        if final_segment_start is None
        else max(0, round(final_segment_start * 1000 / WINDOW_MS) - 1)
    )
    sound_end = max(index for index, level in enumerate(broadband)
                    if level > peak - SOUND_BELOW_PEAK_DB)
    last_treble = max(
        (index for index in range(vowel_end + 1, min(sound_end + 1, len(treble)))
         if treble[index] >= treble_peak - LAST_EVENT_BELOW_HF_PEAK_DB),
        default=None,
    )
    if last_treble is None or last_treble <= vowel_end + 1:
        return None
    gap = range(vowel_end + 1, last_treble)
    closure = min(gap, key=lambda index: broadband[index])
    release = treble[closure + 1:sound_end + 1]
    return Release(
        closure_depth_db=round(peak - broadband[closure], 1),
        closure_ms=sum(WINDOW_MS for index in gap
                       if broadband[index] <= broadband[closure] + CLOSURE_SPAN_DB),
        release_db=round(max(release) - peak, 1),
        release_ms=sum(WINDOW_MS for level in release
                       if level >= max(release) - RELEASE_SPAN_DB),
    )


def final_hiss_ms(samples, final_segment_start: float) -> int:
    """How long the word's final high-frequency noise is sustained, in ms.

    Tells a fricative from a stop at the end of a word: [ç] (König, wenig) is
    a hiss held for its whole duration, while [k] is a burst of a window or
    two after a silent closure. Measures, from `final_segment_start` (seconds,
    from the aligner) to the end of sound, the longest unbroken run of windows
    whose high-passed level is within HISS_SPAN_DB of the segment's loudest.
    """
    broadband = window_db(samples)
    treble = window_db(high_passed(samples))
    if not broadband or not treble:
        return 0
    peak = max(broadband)
    start = max(0, round(final_segment_start * 1000 / WINDOW_MS) - 1)
    sound_end = max(index for index, level in enumerate(broadband)
                    if level > peak - SOUND_BELOW_PEAK_DB)
    segment = treble[start:min(sound_end + 1, len(treble))]
    if not segment:
        return 0
    loudest = max(segment)

    # The longest UNBROKEN run: a [k] burst plus a separate aspiration puff
    # can add up to a hiss's length without ever sustaining one.
    def extend_run(runs: tuple[int, int], loud: bool) -> tuple[int, int]:
        current, longest = runs
        current = current + 1 if loud else 0
        return current, max(longest, current)

    _, longest = reduce(extend_run, (level >= loudest - HISS_SPAN_DB for level in segment), (0, 0))
    return longest * WINDOW_MS


def cut(pcm: bytes, start_seconds: float, end_seconds: float) -> array:
    samples = array("h", pcm)
    return samples[round(start_seconds * SAMPLE_RATE):round(end_seconds * SAMPLE_RATE)]


def finish(samples: array) -> bytes:
    """Fade both edges of a cut clip and append the tail padding."""
    fade_length = min(round(FADE_SECONDS * SAMPLE_RATE), len(samples) // 2)
    faded = array("h", (
        round(sample * min(1.0, (position + 1) / (fade_length + 1),
                           (len(samples) - position) / (fade_length + 1)))
        for position, sample in enumerate(samples)
    ))
    padding = array("h", bytes(2 * round(TAIL_PADDING_SECONDS * SAMPLE_RATE)))
    return (faded + padding).tobytes()


def to_wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(pcm)
    return buffer.getvalue()
