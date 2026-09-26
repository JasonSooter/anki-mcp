"""Measure whether a TTS clip keeps its word-final release burst.

A final stop is a closure (near-silence) followed by a short, quiet,
high-frequency burst. This prints, per clip:

* the energy of the last 200 ms in 10 ms windows, and how far before the end
  the last sound is -- so a clip cut off mid-burst shows as ENDS ON SOUND;
* the final closure and release, measured by anki_mcp.audio -- the same code
  the server selects takes with: the release's peak in a high-passed signal
  (which suppresses voicing, so a stop burst stands out the way it does to the
  ear) relative to the vowel, and how long it lasts.

A native speaker's [k] in "Berg" (Commons De-Berg2.ogg) measures -9.4 dB with
a 20 ms closure. A finished file has no alignment, so here the vowel's end is
guessed by loudness; treat a "NO closure+release" on a strong take with
suspicion and trust the server's own selection note.

    # Stored clips or a reference recording, no API calls:
    uv run python scripts/tts_tail_diagnostic.py --files a.mp3 De-Berg2.ogg

    # Live synthesis (needs ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID):
    uv run python scripts/tts_tail_diagnostic.py --words "der Berg" gelb --save out/
    uv run python scripts/tts_tail_diagnostic.py --words "der Berg" --path legacy --timestamps

`--path legacy` is the pre-fix request (the word alone after a lead-in);
`--path current` is whatever synthesize_word does now. Needs ffmpeg on PATH to
decode MP3/OGG; WAV is read natively.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import wave
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from anki_mcp import audio  # noqa: E402  -- the same measurement the server selects on

SAMPLE_RATE = audio.SAMPLE_RATE
WINDOW_MS = audio.WINDOW_MS
TAIL_MS = 200


def decode_pcm(clip: bytes) -> array:
    if clip[:4] == b"RIFF":
        with wave.open(io.BytesIO(clip)) as reader:
            if reader.getframerate() == SAMPLE_RATE and reader.getnchannels() == 1:
                return array("h", reader.readframes(reader.getnframes()))
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le",
         "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
        input=clip, capture_output=True, check=True,
    ).stdout
    return array("h", decoded)


def describe(label: str, clip: bytes) -> dict:
    samples = decode_pcm(clip)
    broadband = audio.window_db(samples)
    duration_ms = len(samples) * 1000 / SAMPLE_RATE
    floor = max(broadband) - audio.SOUND_BELOW_PEAK_DB
    last_sound_index = max(index for index, level in enumerate(broadband) if level > floor)
    gap_after_sound_ms = duration_ms - (last_sound_index + 1) * WINDOW_MS
    tail = broadband[-(TAIL_MS // WINDOW_MS):]
    # No alignment for a finished file, so the vowel's end is guessed by
    # loudness (see audio.final_release); the server measures from alignment.
    release = audio.final_release(samples)
    print(f"\n== {label}")
    print(f"   duration {duration_ms:.0f} ms, peak {max(broadband):.1f} dBFS")
    print("   last 200 ms (dBFS, 10 ms windows, oldest first):")
    print("   " + " ".join(f"{level:.0f}" for level in tail))
    print(f"   last sound ends {gap_after_sound_ms:.0f} ms before the end"
          f" -> {'ENDS ON SOUND (cut?)' if gap_after_sound_ms <= 20 else 'silence after sound'}")
    print("   final release: " + (
        f"closure {release.closure_ms} ms ({release.closure_depth_db} dB deep), "
        f"burst {release.release_db} dB re vowel, "
        f"{release.release_ms} ms within {audio.RELEASE_SPAN_DB:.0f} dB"
        if release else "NO closure+release found"))
    return {"label": label, "duration_ms": round(duration_ms),
            "gap_after_sound_ms": round(gap_after_sound_ms),
            "release": release.__dict__ if release else None}


def print_alignment(text: str, api_key: str, voice_id: str, model: str) -> None:
    body = json.dumps({"text": text, "model_id": model}).encode()
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{urllib.parse.quote(voice_id)}"
        "/with-timestamps?output_format=pcm_24000",
        data=body,
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        alignment = json.load(response)["alignment"]
    print(f"\n== /with-timestamps alignment for {text!r}")
    print("   " + " ".join(
        f"{character}@{start:.3f}-{end:.3f}"
        for character, start, end in zip(
            alignment["characters"],
            alignment["character_start_times_seconds"],
            alignment["character_end_times_seconds"])
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", nargs="*", default=[])
    parser.add_argument("--words", nargs="*", default=[])
    parser.add_argument("--path", choices=("legacy", "current"), default="current")
    parser.add_argument("--timestamps", action="store_true",
                        help="also print /with-timestamps alignment for each word")
    parser.add_argument("--model", default="eleven_multilingual_v2")
    parser.add_argument("--save", type=Path, help="directory to keep synthesised clips in")
    arguments = parser.parse_args()

    results = [describe(path, Path(path).read_bytes()) for path in arguments.files]

    if arguments.words:
        from anki_mcp import pronunciation

        api_key = os.environ["ELEVENLABS_API_KEY"]
        voice_id = os.environ["ELEVENLABS_VOICE_ID"]

        def synthesise(word: str) -> dict:
            clip, extension, selection = (
                (*pronunciation.synthesize_elevenlabs(
                    word, api_key, voice_id, arguments.model, previous_text="Auf Deutsch:"),
                 "legacy single take")
                if arguments.path == "legacy"
                else pronunciation.synthesize_word(
                    word, api_key, voice_id, arguments.model, previous_text="Auf Deutsch:")
            )
            print(f"\n-- {word}: {selection}")
            if arguments.save:
                arguments.save.mkdir(parents=True, exist_ok=True)
                (arguments.save / f"{arguments.path}-{word.replace(' ', '-')}{extension}").write_bytes(clip)
            if arguments.timestamps:
                # The alignment the server actually cuts from: the carrier's on
                # the current path, the bare word's on the legacy one.
                print_alignment(
                    word if arguments.path == "legacy" else pronunciation.carrier_text(word),
                    api_key, voice_id, arguments.model)
            return describe(f"{word} ({arguments.path} code path)", clip)

        results = results + [synthesise(word) for word in arguments.words]

    print("\n" + json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
