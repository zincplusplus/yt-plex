#!/usr/bin/env python3
"""Compute per-channel SponsorBlock latency: upload -> first segment submission.

Reads channel sources from data/sources.json, fetches recent videos via yt-dlp,
queries SponsorBlock for segments, then reports per-channel latency stats.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp

SB_API = "https://sponsor.ajay.app/api"
DEFAULT_CATEGORIES = ["sponsor", "selfpromo"]


@dataclass
class VideoLatency:
    channel: str
    video_id: str
    title: str
    upload_ts: int | None
    upload_source: str
    first_submission_ts: int | None
    latency_hours: float | None
    segment_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default="data/sources.json", help="Path to sources.json")
    parser.add_argument("--videos-per-channel", type=int, default=20, help="Recent videos to inspect per channel")
    parser.add_argument("--max-channels", type=int, default=0, help="If >0, only process first N channels")
    parser.add_argument(
        "--categories",
        default=",".join(DEFAULT_CATEGORIES),
        help="SponsorBlock categories csv (default: sponsor,selfpromo)",
    )
    parser.add_argument("--timeout", type=float, default=12.0, help="HTTP timeout seconds")
    parser.add_argument("--output", default="reports/sponsorblock_latency.csv", help="Output CSV path")
    return parser.parse_args()


def load_sources(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"sources file not found: {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError("sources file must be a JSON list")
    return data


def scan_channel_recent(url: str, limit: int) -> list[dict]:
    scan_url = url
    if "/videos" not in scan_url and "playlist" not in scan_url.lower():
        scan_url = scan_url.rstrip("/") + "/videos"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": f"1:{limit}",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(scan_url, download=False)

    entries = info.get("entries") or []
    out = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        out.append(
            {
                "video_id": e.get("id"),
                "title": e.get("title") or "",
                "upload_date": e.get("upload_date"),
                "timestamp": e.get("timestamp"),
            }
        )
    return out


def fetch_video_metadata(video_id: str) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {"quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "upload_date": info.get("upload_date"),
        "timestamp": info.get("timestamp"),
    }


def parse_upload_ts(upload_date: str | None, timestamp: int | None) -> tuple[int | None, str]:
    if isinstance(timestamp, (int, float)):
        return int(timestamp), "timestamp"
    if upload_date:
        try:
            dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
            return int(dt.timestamp()), "date_only"
        except ValueError:
            return None, "missing"
    return None, "missing"


def get_skip_segments(video_id: str, categories: list[str], timeout: float) -> list[dict]:
    cats_json = json.dumps(categories, separators=(",", ":"))
    query = urllib.parse.urlencode({"videoID": video_id, "categories": cats_json})
    url = f"{SB_API}/skipSegments?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "yt-plex-latency/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


def get_segment_info(uuids: list[str], timeout: float) -> list[dict]:
    if not uuids:
        return []

    all_rows: list[dict] = []
    for i in range(0, len(uuids), 10):
        chunk = uuids[i : i + 10]
        query = urllib.parse.urlencode([("UUID", u) for u in chunk])
        url = f"{SB_API}/segmentInfo?{query}"
        req = urllib.request.Request(url, headers={"User-Agent": "yt-plex-latency/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rows = json.loads(resp.read())
            if isinstance(rows, list):
                all_rows.extend(rows)
    return all_rows


def normalize_unix_ts(value: int | float | None) -> int | None:
    """Normalize unix timestamps to seconds.

    SponsorBlock can return millisecond timestamps for `timeSubmitted`.
    """
    if value is None:
        return None
    ts = int(value)
    if ts > 100_000_000_000:
        ts = ts // 1000
    return ts


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return math.nan
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return d0 + d1


def summarize_channel(rows: list[VideoLatency]) -> dict:
    total = len(rows)
    with_sb = [r for r in rows if r.first_submission_ts is not None and r.latency_hours is not None]
    lat = sorted(r.latency_hours for r in with_sb if r.latency_hours is not None)

    summary = {
        "videos_checked": total,
        "videos_with_sb": len(with_sb),
        "coverage_pct": (len(with_sb) / total * 100.0) if total else 0.0,
        "median_hours": statistics.median(lat) if lat else None,
        "p90_hours": percentile(lat, 0.9) if lat else None,
        "min_hours": min(lat) if lat else None,
        "max_hours": max(lat) if lat else None,
        "date_only_uploads": sum(1 for r in rows if r.upload_source == "date_only"),
        "missing_upload_ts": sum(1 for r in rows if r.upload_ts is None),
    }
    return summary


def fmt_num(v: float | None) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{v:.2f}"


def main() -> int:
    args = parse_args()
    sources_path = Path(args.sources)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    if not categories:
        categories = DEFAULT_CATEGORIES

    sources = load_sources(sources_path)
    if args.max_channels > 0:
        sources = sources[: args.max_channels]

    all_rows: list[VideoLatency] = []
    per_channel: dict[str, list[VideoLatency]] = {}

    for idx, source in enumerate(sources, start=1):
        name = source.get("name") or source.get("url") or f"channel_{idx}"
        url = source.get("url")
        if not url:
            continue

        print(f"[{idx}/{len(sources)}] scanning {name}")
        try:
            videos = scan_channel_recent(url, args.videos_per_channel)
        except Exception as e:
            print(f"  scan failed: {e}")
            continue

        channel_rows: list[VideoLatency] = []
        for v in videos:
            vid = v["video_id"]
            title = v.get("title", "")
            upload_ts, upload_source = parse_upload_ts(v.get("upload_date"), v.get("timestamp"))

            if upload_ts is None:
                try:
                    meta = fetch_video_metadata(vid)
                    upload_ts, upload_source = parse_upload_ts(meta.get("upload_date"), meta.get("timestamp"))
                except Exception:
                    upload_ts, upload_source = None, "missing"

            try:
                segs = get_skip_segments(vid, categories, args.timeout)
            except Exception:
                segs = []

            uuids = sorted({s.get("UUID") for s in segs if isinstance(s, dict) and s.get("UUID")})
            first_submission_ts: int | None = None
            if uuids:
                try:
                    info_rows = get_segment_info(uuids, args.timeout)
                    submitted = [r.get("timeSubmitted") for r in info_rows if isinstance(r, dict)]
                    submitted = [normalize_unix_ts(x) for x in submitted if isinstance(x, (int, float))]
                    submitted = [x for x in submitted if x is not None]
                    if submitted:
                        first_submission_ts = min(submitted)
                except Exception:
                    first_submission_ts = None

            latency_hours: float | None = None
            if upload_ts is not None and first_submission_ts is not None:
                latency_hours = max(0.0, (first_submission_ts - upload_ts) / 3600.0)

            row = VideoLatency(
                channel=name,
                video_id=vid,
                title=title,
                upload_ts=upload_ts,
                upload_source=upload_source,
                first_submission_ts=first_submission_ts,
                latency_hours=latency_hours,
                segment_count=len(uuids),
            )
            channel_rows.append(row)
            all_rows.append(row)

        per_channel[name] = channel_rows

    with output_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "channel",
                "video_id",
                "title",
                "upload_ts",
                "upload_source",
                "first_submission_ts",
                "latency_hours",
                "segment_count",
            ]
        )
        for r in all_rows:
            w.writerow(
                [
                    r.channel,
                    r.video_id,
                    r.title,
                    r.upload_ts,
                    r.upload_source,
                    r.first_submission_ts,
                    fmt_num(r.latency_hours),
                    r.segment_count,
                ]
            )

    print("\nPer-channel summary")
    print("channel | checked | with_sb | coverage% | median_h | p90_h | min_h | max_h | date_only")
    for channel, rows in per_channel.items():
        s = summarize_channel(rows)
        print(
            f"{channel} | {s['videos_checked']} | {s['videos_with_sb']} | {s['coverage_pct']:.1f} | "
            f"{fmt_num(s['median_hours'])} | {fmt_num(s['p90_hours'])} | {fmt_num(s['min_hours'])} | "
            f"{fmt_num(s['max_hours'])} | {s['date_only_uploads']}"
        )

    overall = summarize_channel(all_rows)
    print("\nOverall")
    print(json.dumps(overall, indent=2))
    print(f"\nWrote CSV: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
