"""Stage 6: assembly.

Concatenate, pace, level, encode, tag. The pauses and the loudness target are
what make a 40-minute listen tolerable.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mutagen.id3 import (
    APIC, CHAP, COMM, CTOC, CTOCFlags, ID3, TALB, TDRC, TIT2, TPE1, WOAS,
)
from mutagen.mp3 import MP3

from readcast import audio as A
from readcast.synth.runner import ChunkAudio

log = logging.getLogger("readcast.assemble")


@dataclass
class Chapter:
    title: str
    start_ms: int
    end_ms: int = 0


@dataclass
class Assembled:
    path: Path
    duration_s: float
    bytes: int
    chapters: list[Chapter] = field(default_factory=list)


def build_intro(template: str, meta: dict[str, Any]) -> str:
    """Fill the intro template, dropping any sentence whose field is missing.

    Never say "unknown author".
    """
    import re

    published = meta.get("published_at") or ""
    if published:
        from dateutil import parser as date_parser

        try:
            dt = date_parser.parse(str(published))
            day = dt.day
            published = f"{dt.strftime('%B')} {day}, {dt.year}"
        except (ValueError, OverflowError, TypeError):
            published = str(published)

    values = {
        "title": (meta.get("title") or "").strip(),
        "author": (meta.get("author") or "").strip(),
        "publication": (meta.get("publication") or "").strip(),
        "published_at": published.strip(),
    }
    out = []
    for sentence in re.split(r"(?<=\.)\s+", template.strip()):
        fields = re.findall(r"\{(\w+)[^}]*\}", sentence)
        if any(not values.get(f) for f in fields):
            continue
        rendered = sentence
        for field_name in fields:
            rendered = re.sub(
                r"\{" + field_name + r"(?::[^}]*)?\}", values[field_name], rendered
            )
        out.append(rendered.strip())
    joined = " ".join(out).strip()
    # A publication like "Wikimedia Foundation, Inc." already ends in a period,
    # and the template adds another. The engine reads "Inc.." as a stumble.
    return re.sub(r"([.!?])[.]+", r"\1", joined)


def plan_timeline(
    chunks: list[ChunkAudio], audio_cfg: dict[str, Any], work: Path
) -> tuple[list[Path], list[Chapter]]:
    """Interleave chunk audio with silence and note where each chapter starts."""
    pause_paragraph = int(audio_cfg.get("pause_paragraph_ms", 500))
    pause_heading = int(audio_cfg.get("pause_heading_ms", 1200))
    pause_intro = int(audio_cfg.get("pause_after_intro_ms", 1000))

    silences: dict[int, Path] = {}

    def silence(ms: int) -> Path | None:
        if ms <= 0:
            return None
        if ms not in silences:
            silences[ms] = A.write_silence(work / f"silence-{ms}.wav", ms)
        return silences[ms]

    parts: list[Path] = []
    chapters: list[Chapter] = []
    cursor_ms = 0.0

    for i, item in enumerate(chunks):
        if item.chunk.kind == "heading" and parts:
            gap = silence(pause_heading)
            if gap:
                parts.append(gap)
                cursor_ms += pause_heading
        if item.chunk.kind == "heading":
            chapters.append(Chapter(title=item.chunk.text, start_ms=int(cursor_ms)))

        parts.append(item.path)
        cursor_ms += item.duration_s * 1000

        if item.chunk.kind == "intro":
            gap = silence(pause_intro)
            if gap:
                parts.append(gap)
                cursor_ms += pause_intro
        elif item.chunk.ends_paragraph and i < len(chunks) - 1:
            gap = silence(pause_paragraph)
            if gap:
                parts.append(gap)
                cursor_ms += pause_paragraph

    for i, chapter in enumerate(chapters):
        chapter.end_ms = (
            chapters[i + 1].start_ms if i + 1 < len(chapters) else int(cursor_ms)
        )
    return parts, chapters


def write_tags(
    path: Path,
    *,
    meta: dict[str, Any],
    chapters: list[Chapter],
    cover: Path | None = None,
    album: str = "readcast",
) -> None:
    audio_file = MP3(path)
    if audio_file.tags is None:
        audio_file.add_tags()
    tags: ID3 = audio_file.tags

    tags.delall("TIT2"); tags.delall("TPE1"); tags.delall("TALB")
    tags.delall("TDRC"); tags.delall("WOAS"); tags.delall("COMM")
    tags.delall("APIC"); tags.delall("CHAP"); tags.delall("CTOC")

    tags.add(TIT2(encoding=3, text=[str(meta.get("title") or "readcast episode")]))
    if meta.get("author"):
        tags.add(TPE1(encoding=3, text=[str(meta["author"])]))
    tags.add(TALB(encoding=3, text=[album]))
    if meta.get("published_at"):
        tags.add(TDRC(encoding=3, text=[str(meta["published_at"])[:10]]))
    if meta.get("url"):
        tags.add(WOAS(url=str(meta["url"])))
        tags.add(
            COMM(encoding=3, lang="eng", desc="source", text=[str(meta["url"])])
        )
    if cover and Path(cover).is_file():
        tags.add(
            APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=Path(cover).read_bytes(),
            )
        )

    ids = []
    for i, chapter in enumerate(chapters):
        element_id = f"chp{i}"
        ids.append(element_id)
        tags.add(
            CHAP(
                element_id=element_id,
                start_time=chapter.start_ms,
                end_time=chapter.end_ms,
                sub_frames=[TIT2(encoding=3, text=[chapter.title])],
            )
        )
    if ids:
        tags.add(
            CTOC(
                element_id="toc",
                flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                child_element_ids=ids,
                sub_frames=[TIT2(encoding=3, text=["Chapters"])],
            )
        )
    audio_file.save(v2_version=3)


def ensure_cover(path: str | Path, title: str = "readcast") -> Path | None:
    """A plain generated cover. Replace data/feed/cover.jpg with a real one."""
    path = Path(path)
    if path.is_file():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    font = next(
        (
            f
            for f in (
                "/System/Library/Fonts/Supplemental/Futura.ttc",
                "/System/Library/Fonts/Helvetica.ttc",
                "/Library/Fonts/Arial.ttf",
            )
            if Path(f).is_file()
        ),
        None,
    )
    filters = "color=c=0x14161a:s=1400x1400"
    if font:
        safe = title.replace(":", r"\:").replace("'", "")
        filters += (
            f",drawtext=fontfile={font}:text='{safe}':fontcolor=0xf5f5f5:"
            "fontsize=140:x=(w-text_w)/2:y=(h-text_h)/2"
        )
    try:
        A.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", filters, "-frames:v", "1", str(path),
            ]
        )
    except A.AudioError as exc:
        log.warning("could not generate a cover image: %s", exc)
        return None
    return path


def assemble(
    chunks: list[ChunkAudio],
    out_path: str | Path,
    *,
    meta: dict[str, Any],
    audio_cfg: dict[str, Any] | None = None,
    cover: str | Path | None = None,
) -> Assembled:
    audio_cfg = audio_cfg or {}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    A.require_ffmpeg()

    work = Path(tempfile.mkdtemp(prefix="readcast-assemble-"))
    try:
        parts, chapters = plan_timeline(chunks, audio_cfg, work)
        joined = A.concat_wavs(parts, work / "joined.wav")
        leveled = A.loudnorm(
            joined,
            work / "leveled.wav",
            lufs=float(audio_cfg.get("lufs", -16)),
            true_peak=float(audio_cfg.get("true_peak", -1.5)),
            lra=float(audio_cfg.get("lra", 11)),
            sample_rate=int(audio_cfg.get("sample_rate", A.SAMPLE_RATE)),
        )
        A.encode_mp3(
            leveled,
            out_path,
            bitrate_kbps=int(audio_cfg.get("bitrate_kbps", 64)),
            sample_rate=int(audio_cfg.get("sample_rate", A.SAMPLE_RATE)),
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    duration = A.probe_duration(out_path)
    if chapters:
        chapters[-1].end_ms = int(duration * 1000)
    write_tags(out_path, meta=meta, chapters=chapters, cover=Path(cover) if cover else None)

    return Assembled(
        path=out_path,
        duration_s=duration,
        bytes=out_path.stat().st_size,
        chapters=chapters,
    )
