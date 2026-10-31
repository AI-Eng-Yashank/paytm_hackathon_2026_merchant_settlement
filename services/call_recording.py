"""
Full call recording: one audio file per conversation with both sides in order
(merchant, AI, merchant, AI ...), like a phone call recording.

Built from the per-turn clips with ffmpeg (bundled by the imageio-ffmpeg package, so nothing
to install system-wide). Each clip is resampled to 24 kHz mono and followed by a short pause,
then everything is joined into one MP3. The result is cached next to the clips and rebuilt
when the conversation gets new recorded turns.
"""

import subprocess
from pathlib import Path

import imageio_ffmpeg


GAP_S = 0.5          # pause between speakers
SAMPLE_RATE = 24000


def build(clips: list[Path], out_dir: Path) -> Path:
    """Join clips (in order) into out_dir/full-call-<n>.mp3 and return its path; reuse if already built."""
    out = out_dir / f"full-call-{len(clips)}.mp3"
    if out.exists() and out.stat().st_mtime >= max(c.stat().st_mtime for c in clips):
        return out
    for old in out_dir.glob("full-call-*.mp3"):
        old.unlink()

    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    for clip in clips:
        cmd += ["-i", str(clip)]
    filters = "".join(
        f"[{i}:a]aresample={SAMPLE_RATE},aformat=sample_fmts=s16:channel_layouts=mono,apad=pad_dur={GAP_S}[a{i}];"
        for i in range(len(clips)))
    filters += "".join(f"[a{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=0:a=1[out]"
    tmp = out.with_suffix(".tmp.mp3")
    cmd += ["-filter_complex", filters, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "64k", str(tmp)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[:500]}")
    tmp.replace(out)
    return out
