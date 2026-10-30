#!/usr/bin/env python
"""Measure how well voice recognition separates the people in this house.

Recording a few clips per person and scoring them against each other is the
only way to know where the threshold belongs -- it depends on the microphones,
the rooms and the voices, not on the model alone.

    mkdir -p data/voice-samples/zach data/voice-samples/mom
    # put 3+ clips of each person talking (5-10s, normal speaking voice) in
    # their folder: webm, wav, ogg, m4a, mp3 all work
    .venv/bin/python scripts/voice-check.py data/voice-samples

It prints how close two clips of the same person land, how close two different
people land, and the threshold that separates them -- if one exists.
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from house_agent.config import load_config  # noqa: E402
from house_agent.voice import (  # noqa: E402
    SAMPLE_RATE,
    _sherpa_embedder,
    _unit,
    decode_audio,
    generic_centre,
    speech_only,
    speech_seconds,
)

CLIP_TYPES = {".webm", ".wav", ".ogg", ".m4a", ".mp3", ".mp4", ".flac", ".opus"}


def load_clips(root: Path) -> dict[str, list[Path]]:
    people = {
        d.name: sorted(f for f in d.iterdir() if f.suffix.lower() in CLIP_TYPES)
        for d in sorted(root.iterdir())
        if d.is_dir()
    }
    return {name: clips for name, clips in people.items() if clips}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path, help="folder with one subfolder per person")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()

    if not args.samples.is_dir():
        print(f"No such folder: {args.samples}", file=sys.stderr)
        return 1
    people = load_clips(args.samples)
    if len(people) < 2:
        print(
            "Two people are needed to measure separation. Make a subfolder per\n"
            f"person under {args.samples} with a few clips in each.",
            file=sys.stderr,
        )
        return 1

    config = load_config(args.config).voice
    embed = _sherpa_embedder(Path(config.model_path), config.threads)

    # Embed every clip twice: as the whole recording, and speech-gated. The
    # difference shows how much of each voiceprint was really the room.
    prints: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {}
    print(f"Reading clips from {args.samples}\n")
    for person, clips in people.items():
        prints[person] = []
        for clip in clips:
            wav = decode_audio(clip.read_bytes())
            voiced = speech_only(wav)
            spoken = speech_seconds(wav)
            short = spoken < config.min_speech_seconds - 0.05
            flag = f"  << under {config.min_speech_seconds:.1f}s, barely evidence" if short else ""
            print(
                f"  {person:<12} {clip.name:<28} "
                f"{wav.size / SAMPLE_RATE:5.1f}s clip, {spoken:4.1f}s speech{flag}"
            )
            prints[person].append((_unit(embed(wav)), _unit(embed(voiced))))

    dim = len(next(iter(prints.values()))[0][0])
    every_clip = [pair for clips in prints.values() for pair in clips]
    for column, label in ((0, "whole clip"), (1, "speech only")):
        centre = generic_centre(dim)
        if len(prints) >= 2:
            household = np.mean([pair[column] for pair in every_clip], axis=0)
            centre = 0.5 * centre + 0.5 * household

        def score(a: np.ndarray, b: np.ndarray, centre: np.ndarray = centre) -> float:
            return float(np.dot(_unit(a - centre), _unit(b - centre)))

        same = [
            score(a[column], b[column])
            for clips in prints.values()
            for a, b in itertools.combinations(clips, 2)
        ]
        different = [
            score(a[column], b[column])
            for one, other in itertools.combinations(prints, 2)
            for a in prints[one]
            for b in prints[other]
        ]
        print(f"\n=== scored on the {label} ===")
        if not same:
            print("  (two or more clips per person are needed for same-voice scores)")
        else:
            print(f"  same person      {min(same):.3f} .. {max(same):.3f}   worst {min(same):.3f}")
        print(
            f"  different people {min(different):.3f} .. {max(different):.3f}   "
            f"worst {max(different):.3f}"
        )
        if same:
            gap = min(same) - max(different)
            if gap > 0:
                print(f"  gap  {gap:+.3f}  -- these voices are separable")
                print(
                    f"  suggested threshold = {min(same) - gap / 2:.2f}  "
                    f"(margin {min(0.08, gap / 2):.2f})"
                )
            else:
                print(
                    f"  gap  {gap:+.3f}  -- OVERLAPPING: no threshold tells these\n"
                    "        voices apart. Record longer clips, move the microphone\n"
                    "        closer, or enroll from several separate recordings."
                )
    print(
        "\nSet the winning numbers under [voice] in config.toml, then re-enroll\n"
        "everyone so the stored voiceprints are built the same way."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
