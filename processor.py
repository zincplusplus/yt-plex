"""Post-processing: SponsorBlock, smart cut, poster, NFO metadata."""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.dom.minidom import parseString
from xml.etree.ElementTree import Element, SubElement, tostring

logger = logging.getLogger(__name__)

SB_API = "https://sponsor.ajay.app/api"


def get_sponsor_segments(video_id: str) -> list[dict]:
    """Fetch SponsorBlock segments for a video."""
    categories = ["sponsor", "selfpromo"]
    cats_param = json.dumps(categories, separators=(",", ":"))
    url = f"{SB_API}/skipSegments?videoID={video_id}&categories={cats_param}"

    try:
        req = Request(url, headers={"User-Agent": "yt-plex/2.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return [
                {"start": s["segment"][0], "end": s["segment"][1], "category": s.get("category", "unknown")}
                for s in data
                if "segment" in s and len(s["segment"]) == 2
            ]
    except HTTPError as e:
        if e.code != 404:
            logger.warning(f"SponsorBlock API error {e.code} for {video_id}")
        return []
    except (URLError, json.JSONDecodeError, Exception) as e:
        logger.warning(f"SponsorBlock request failed for {video_id}: {e}")
        return []


def find_sponsors_with_gemini(vtt_content: str, api_key: str) -> list[dict]:
    """Use Gemini AI to identify sponsor segments from subtitles."""
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    prompt = """You are analyzing YouTube video subtitles to find sponsor/ad segments.

Identify all sponsored ad reads — segments where the host is promoting a product or service.

Include the full segment from when they transition INTO the ad read to when they return to regular content. Err on the side of starting a few seconds early rather than late.

Return JSON only, no other text:
[{"sponsor": "Brand Name", "start_seconds": 123.4, "end_seconds": 189.2}]

If no sponsors found, return: []

Subtitles:
""" + vtt_content

    response = model.generate_content(prompt)
    text = response.text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def gemini_fallback(video_id: str, file_path: str, api_key: str):
    """When SponsorBlock has no data, use Gemini to find sponsors."""
    video = Path(file_path)
    stem = video.stem

    vtt_path = None
    for candidate in [video.parent / f"{stem}.en.vtt", video.parent / f"{stem}.vtt"]:
        if candidate.exists():
            vtt_path = candidate
            break
    if not vtt_path:
        for f in video.parent.glob(f"{stem}.*.vtt"):
            vtt_path = f
            break

    if not vtt_path:
        logger.info(f"No VTT subtitle file found for {video_id}, skipping Gemini fallback")
        return

    try:
        vtt_content = vtt_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to read VTT file: {e}")
        return

    try:
        segments = find_sponsors_with_gemini(vtt_content, api_key)
    except Exception as e:
        logger.error(f"Gemini sponsor detection failed: {e}")
        return

    if not segments:
        logger.info(f"Gemini found no sponsors in {video_id}")
        return

    names = [s.get("sponsor", "?") for s in segments]
    logger.info(f"Gemini found {len(segments)} sponsor(s) in {video_id}: {', '.join(names)}")

    return [{"start": s["start_seconds"], "end": s["end_seconds"], "category": "sponsor"} for s in segments]


async def smart_cut(input_path: str, segments: list[dict]) -> str:
    """Remove sponsor segments using smart cut with keyframe-aware splitting."""
    if not segments:
        return input_path

    input_file = Path(input_path)
    if not input_file.exists():
        return input_path

    output_path = str(input_file.with_stem(input_file.stem + "_cut"))

    duration = await _get_duration(input_path)
    if duration is None:
        logger.error("Could not determine video duration")
        return input_path

    keep_intervals = _compute_keep_intervals(segments, duration)
    if not keep_intervals:
        return input_path

    if len(keep_intervals) == 1 and keep_intervals[0][0] < 0.1 and abs(keep_intervals[0][1] - duration) < 0.1:
        return input_path

    logger.info(f"Smart cut: removing {len(segments)} segments, keeping {len(keep_intervals)} intervals")

    keyframes = await _get_keyframes(input_path)
    temp_dir = tempfile.mkdtemp(prefix="ytplex_smartcut_")
    segment_files = []

    try:
        for i, (start, end) in enumerate(keep_intervals):
            seg_path = os.path.join(temp_dir, f"seg_{i:04d}.mp4")
            kf_before = _find_keyframe_before(keyframes, start)

            if kf_before is not None and (start - kf_before) > 0.05:
                sliver_path = os.path.join(temp_dir, f"sliver_{i:04d}.mp4")
                body_path = os.path.join(temp_dir, f"body_{i:04d}.mp4")

                await _run_ffmpeg([
                    "-ss", str(kf_before), "-i", input_path,
                    "-t", str(start - kf_before),
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                    "-c:a", "aac", "-b:a", "192k", "-y", sliver_path,
                ])
                await _run_ffmpeg([
                    "-ss", str(start), "-i", input_path,
                    "-t", str(end - start),
                    "-c", "copy", "-avoid_negative_ts", "make_zero", "-y", body_path,
                ])

                if os.path.exists(sliver_path) and os.path.getsize(sliver_path) > 0:
                    concat_list = os.path.join(temp_dir, f"concat_{i:04d}.txt")
                    with open(concat_list, "w") as f:
                        f.write(f"file '{sliver_path}'\nfile '{body_path}'\n")
                    await _run_ffmpeg([
                        "-f", "concat", "-safe", "0", "-i", concat_list,
                        "-c", "copy", "-y", seg_path,
                    ])
                else:
                    os.rename(body_path, seg_path)
            else:
                await _run_ffmpeg([
                    "-ss", str(start), "-i", input_path,
                    "-t", str(end - start),
                    "-c", "copy", "-avoid_negative_ts", "make_zero", "-y", seg_path,
                ])

            if os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
                segment_files.append(seg_path)

        if not segment_files:
            logger.error("No segments produced")
            return input_path

        concat_list = os.path.join(temp_dir, "final_concat.txt")
        with open(concat_list, "w") as f:
            for seg in segment_files:
                f.write(f"file '{seg}'\n")

        await _run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c", "copy", "-y", output_path,
        ])

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            original_size = os.path.getsize(input_path)
            cut_size = os.path.getsize(output_path)
            os.replace(output_path, input_path)
            logger.info(f"Smart cut complete: {original_size / 1024 / 1024:.1f}MB -> {cut_size / 1024 / 1024:.1f}MB")
            return input_path
        else:
            logger.error("Output file empty or missing")
            return input_path

    except Exception as e:
        logger.error(f"Smart cut failed: {e}")
        return input_path
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _compute_keep_intervals(segments: list[dict], duration: float) -> list[tuple[float, float]]:
    sorted_segs = sorted(segments, key=lambda s: s["start"])
    merged = []
    for seg in sorted_segs:
        if merged and seg["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
        else:
            merged.append({"start": seg["start"], "end": seg["end"]})

    keep = []
    pos = 0.0
    for seg in merged:
        if seg["start"] > pos + 0.1:
            keep.append((pos, seg["start"]))
        pos = seg["end"]
    if pos < duration - 0.1:
        keep.append((pos, duration))
    return keep


async def _get_duration(path: str) -> Optional[float]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return float(stdout.decode().strip())
    except Exception:
        return None


async def _get_keyframes(path: str) -> list[float]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,flags",
            "-of", "csv=print_section=0", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        keyframes = []
        for line in stdout.decode().splitlines():
            parts = line.strip().split(",")
            if len(parts) >= 2 and "K" in parts[1]:
                try:
                    keyframes.append(float(parts[0]))
                except ValueError:
                    pass
        return sorted(keyframes)
    except Exception as e:
        logger.warning(f"Could not extract keyframes: {e}")
        return []


def _find_keyframe_before(keyframes: list[float], timestamp: float) -> Optional[float]:
    result = None
    for kf in keyframes:
        if kf <= timestamp:
            result = kf
        else:
            break
    return result


async def _run_ffmpeg(args: list[str]):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(f"ffmpeg error: {stderr.decode()[:500]}")


def write_poster(video_folder: Path):
    """Rename the downloaded thumbnail to poster.jpg for Plex."""
    for ext in ("jpg", "webp", "png"):
        for thumb in video_folder.glob(f"*.{ext}"):
            if thumb.stem == "poster":
                continue
            dest = video_folder / "poster.jpg"
            thumb.rename(dest)
            logger.info(f"Poster: {thumb.name} -> poster.jpg")
            return


def write_nfo(video_folder: Path, info: dict):
    """Write a Plex-compatible .nfo file for the video."""
    root = Element("movie")
    SubElement(root, "title").text = info.get("title", "")
    SubElement(root, "plot").text = info.get("description", "")

    upload_date = info.get("upload_date", "")
    if upload_date:
        aired = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
        SubElement(root, "aired").text = aired

    SubElement(root, "dateadded").text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SubElement(root, "studio").text = info.get("channel", "")

    uid = SubElement(root, "uniqueid", type="youtube", default="true")
    uid.text = info.get("video_id", "")

    duration = info.get("duration")
    if duration:
        fi = SubElement(root, "fileinfo")
        sd = SubElement(fi, "streamdetails")
        vid = SubElement(sd, "video")
        SubElement(vid, "durationinseconds").text = str(int(duration))

    raw_xml = tostring(root, encoding="unicode")
    pretty = parseString(raw_xml).toprettyxml(indent="  ", encoding=None)
    lines = pretty.split("\n")
    body = "\n".join(lines[1:])

    nfo_name = None
    for f in video_folder.glob("*.mp4"):
        nfo_name = f.stem + ".nfo"
        break
    if not nfo_name:
        nfo_name = "movie.nfo"

    nfo_path = video_folder / nfo_name
    nfo_path.write_text(body, encoding="utf-8")
    logger.info(f"NFO: wrote {nfo_path.name}")


async def post_process_one(video_id: str, downloads_dir: str,
                           use_sponsorblock: bool, gemini_api_key: str = "") -> bool:
    """Post-process a single video: SponsorBlock + smart_cut + poster + NFO.

    Finds the video folder by video_id, reads .info.json for metadata.
    Returns True on success, False on failure.
    """
    downloads = Path(downloads_dir)

    # Find the video folder containing this video_id
    video_folder = None
    video_file = None
    info_json = None

    for channel_dir in downloads.iterdir():
        if not channel_dir.is_dir():
            continue
        candidate = channel_dir / video_id
        if candidate.exists() and candidate.is_dir():
            video_folder = candidate
            break

    if not video_folder:
        logger.error(f"Could not find video folder for {video_id}")
        return False

    # Find .info.json
    for f in video_folder.glob("*.info.json"):
        info_json = f
        break

    if not info_json:
        logger.warning(f"No .info.json found for {video_id}, using minimal metadata")
        info = {"video_id": video_id}
    else:
        try:
            info = json.loads(info_json.read_text())
        except Exception as e:
            logger.warning(f"Could not read .info.json for {video_id}: {e}")
            info = {"video_id": video_id}

    # Find video file
    if not video_file:
        for f in video_folder.glob("*.mp4"):
            video_file = f
            break

    if not video_file:
        logger.error(f"No MP4 file found for {video_id}")
        return False

    file_path = str(video_file)
    logger.info(f"Post-processing: {video_id} ({video_file.name})")

    # SponsorBlock + smart cut (with Gemini fallback)
    loop = asyncio.get_running_loop()
    if use_sponsorblock:
        segments = await loop.run_in_executor(None, get_sponsor_segments, video_id)
        if segments:
            logger.info(f"SponsorBlock: {len(segments)} segments found for {video_id}")
            await smart_cut(file_path, segments)
        elif gemini_api_key:
            logger.info(f"SponsorBlock: no segments for {video_id}, trying Gemini fallback")
            gemini_segments = await loop.run_in_executor(
                None, gemini_fallback, video_id, file_path, gemini_api_key
            )
            if gemini_segments:
                await smart_cut(file_path, gemini_segments)
        else:
            logger.info(f"SponsorBlock: no segments for {video_id}")

    # Write poster.jpg and .nfo metadata
    try:
        write_poster(video_folder)
        write_nfo(video_folder, info)
    except Exception as e:
        logger.warning(f"Metadata write failed for {video_id}: {e}")

    return True
