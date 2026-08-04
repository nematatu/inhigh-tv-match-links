#!/usr/bin/env python3
"""Create one local MP4 per available match without modifying the source archive.

The destination contains only the generated match videos.  Only the
destination is written; the original date/court files are never opened for
writing or replaced.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path


DEFAULT_SOURCE = Path("/Volumes/名称未設定/inhigh-tv-2026-badminton")
DEFAULT_TARGET = Path("/Volumes/名称未設定/inhigh-tv-2026-badminton-cropped")
DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data" / "matches.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="元の年月日・コート別アーカイブ")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="コピー側の出力先")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="試合データJSON")
    parser.add_argument("--reencode", action="store_true", help="フレーム単位で正確に切り出す（非常に時間がかかります）")
    parser.add_argument("--dry-run", action="store_true", help="実行せず、生成予定だけ表示")
    return parser.parse_args()


def side_name(side: dict) -> str:
    players = [str(player.get("name", "")).strip() for player in side.get("players", [])]
    players = [player for player in players if player]
    return "・".join(players) or str(side.get("school") or side.get("name") or "対戦者未確認").strip()


def safe_name(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or "対戦カード未確認"


def category_dir(entry: dict) -> str:
    if entry.get("tournamentType") == "team":
        return "TEAM-M" if entry.get("category") == "男子" else "TEAM-W"
    return str(entry.get("category") or "UNKNOWN")


def source_path(source: Path, entry: dict) -> Path:
    date = str(entry.get("date") or "")
    court = str(entry.get("court") or "").strip()
    return source / date / f"{date}_{int(court):02d}.mp4"


def duration_seconds(path: Path) -> float:
    command = [
        "/opt/homebrew/bin/ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(path),
    ]
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    return float(result.stdout.strip())


def output_path(target: Path, entry: dict, used: set[Path]) -> Path:
    sides = entry.get("sides") or []
    left = side_name(sides[0]) if len(sides) > 0 else "対戦者未確認"
    right = side_name(sides[1]) if len(sides) > 1 else "対戦者未確認"
    base = safe_name(f"{left}-{right}")
    directory = target / category_dir(entry)
    candidate = directory / f"{base}.mp4"
    # Reuse the deterministic card name when it already exists so a resumed
    # run does not create a second copy with a suffix.
    if candidate not in used:
        return candidate
    suffix = safe_name(f"{entry.get('matchNo') or '試合'}-{entry.get('orderName') or 'order'}")
    candidate = directory / f"{base}__{suffix}.mp4"
    index = 2
    while candidate in used or candidate.exists():
        candidate = directory / f"{base}__{suffix}-{index}.mp4"
        index += 1
    return candidate


def ffmpeg_command(source: Path, output: Path, start: float, duration: float, reencode: bool) -> list[str]:
    command = [
        "/opt/homebrew/bin/ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        "-map",
        "0",
    ]
    if reencode:
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac"]
    else:
        command += ["-c", "copy"]
    command += ["-avoid_negative_ts", "make_zero", "-y", str(output)]
    return command


def main() -> int:
    args = parse_args()
    if not args.data.is_file():
        print(f"データがありません: {args.data}", file=sys.stderr)
        return 2
    if not args.source.is_dir():
        print(f"元アーカイブがありません: {args.source}", file=sys.stderr)
        return 2
    args.target.mkdir(parents=True, exist_ok=True)

    data = json.loads(args.data.read_text(encoding="utf-8"))
    archive_durations = {
        (str(archive.get("date")), str(archive.get("court"))): float(archive["durationSeconds"])
        for archive in data.get("archives", [])
        if archive.get("date") and archive.get("court") and archive.get("durationSeconds")
    }
    entries = [
        entry
        for entry in data.get("matches", [])
        if entry.get("status") == "available"
        and entry.get("startSeconds") is not None
        and entry.get("date")
        and entry.get("court")
    ]
    grouped: dict[Path, list[dict]] = defaultdict(list)
    for entry in entries:
        grouped[source_path(args.source, entry)].append(entry)

    jobs: list[tuple[dict, Path, Path, float, float]] = []
    used: set[Path] = set()
    for source, group in sorted(grouped.items(), key=lambda pair: str(pair[0])):
        if not source.is_file():
            print(f"スキップ（元動画なし）: {source}", file=sys.stderr)
            continue
        archive_key = (source.parent.name, source.stem.rsplit("_", 1)[-1].lstrip("0") or "0")
        duration = archive_durations.get(archive_key)
        if duration is None:
            duration = duration_seconds(source)
        else:
            # The downloaded local file is a few seconds longer than the
            # official duration in the current archive.  ffmpeg stops at EOF,
            # so a small tail allowance avoids cutting that tail prematurely.
            duration += 60
        ordered = sorted(group, key=lambda entry: float(entry["startSeconds"]))
        for index, entry in enumerate(ordered):
            start = float(entry["startSeconds"])
            next_start = float(ordered[index + 1]["startSeconds"]) if index + 1 < len(ordered) else duration
            end = min(duration, next_start)
            if start < 0 or end <= start:
                print(f"スキップ（時間範囲不正）: {entry.get('id')}", file=sys.stderr)
                continue
            output = output_path(args.target, entry, used)
            used.add(output)
            jobs.append((entry, source, output, start, end - start))

    print(f"生成対象: {len(jobs)}件")
    if args.dry_run:
        for entry, source, output, start, duration in jobs:
            print(f"{entry.get('category')} {entry.get('matchNo')} {entry.get('orderName')} {start:.0f}s {duration:.0f}s -> {output}")
        return 0

    completed = 0
    skipped = 0
    manifest: list[dict] = []
    for index, (entry, source, output, start, duration) in enumerate(jobs, start=1):
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.is_file() and output.stat().st_size > 0:
            skipped += 1
            manifest.append({"id": entry.get("id"), "path": str(output.relative_to(args.target)), "status": "existing"})
            continue
        # Keep .mp4 as the final suffix so ffmpeg can infer the muxer.
        temporary = output.with_name(f"{output.stem}.part{output.suffix}")
        command = ffmpeg_command(source, temporary, start, duration, args.reencode)
        print(f"[{index}/{len(jobs)}] {output.name}", flush=True)
        try:
            subprocess.run(command, check=True)
            temporary.replace(output)
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"失敗: {output} ({error})", file=sys.stderr)
            manifest.append({"id": entry.get("id"), "path": str(output.relative_to(args.target)), "status": "error"})
            continue
        completed += 1
        manifest.append({
            "id": entry.get("id"),
            "matchNo": entry.get("matchNo"),
            "orderName": entry.get("orderName"),
            "category": category_dir(entry),
            "date": entry.get("date"),
            "court": entry.get("court"),
            "startSeconds": start,
            "durationSeconds": duration,
            "path": str(output.relative_to(args.target)),
            "status": "created",
        })

    manifest_path = args.target / "match-crops.json"
    manifest_path.write_text(json.dumps({"source": str(args.source), "matches": manifest}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"完了: 新規 {completed}件 / 既存 {skipped}件 / 対象 {len(jobs)}件")
    return 0 if completed + skipped == len(jobs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
