#!/usr/bin/env python3
"""外付けHDDのアーカイブを試合単位に切り出す再開式CLI。

元の年月日・コート別MP4は読み取り専用で扱い、試合別MP4は別の出力先に
作成します。完成済みの出力は次回起動時にサイズを確認してスキップし、
``Ctrl+C`` で中断しても同じコマンドで続きから再開できます。
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import io
import json
import os
import re
import selectors
import signal
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


DEFAULT_SOURCE = Path("/Volumes/名称未設定/inhigh-tv-2026-badminton")
DEFAULT_TARGET = Path("/Volumes/名称未設定/inhigh-tv-2026-badminton-cropped")
DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data" / "matches.json"
STOP_GRACE_SECONDS = 15.0
ROUND_LABELS = frozenset({"1回戦", "2回戦", "3回戦", "4回戦", "準々決勝", "準決勝", "決勝"})

STOP_REQUESTED = False
STOP_SIGNAL: int | None = None
ACTIVE_PROCESS: subprocess.Popen[str] | None = None
LOGGER: "RunLogger | None" = None


@dataclass(frozen=True)
class Job:
    entry: dict[str, Any]
    source: Path
    output: Path
    start: float
    duration: float


class RunLogger:
    """コンソールと同じ内容をタイムスタンプ付きでログへ追記する。"""

    def __init__(self, path: Path, terminal: "TerminalUI"):
        self.path = path
        self.terminal = terminal
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a", encoding="utf-8", buffering=1)

    def write(self, message: str) -> None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
        line = f"[{timestamp}] {message}"
        self.terminal.event(line)
        self.handle.write(line + "\n")

    def progress(self, message: str, details: dict[str, Any]) -> None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
        self.handle.write(f"[{timestamp}] {message}\n")
        self.terminal.progress(message, details)

    def close(self) -> None:
        self.terminal.finish()
        self.handle.close()


class TerminalUI:
    """前のダウンロードCLIと同じ固定ダッシュボード式の進捗表示。"""

    def __init__(self, enabled: bool, output: Any | None = None):
        self.enabled = enabled
        self.output = output or sys.stdout
        self.rendered_lines = 0
        self.cursor_hidden = False
        self.last_rendered_at = 0.0

    @staticmethod
    def _bar(percent: float, width: int = 20) -> str:
        percent = min(100.0, max(0.0, percent))
        filled = min(width, int(percent / 100.0 * width))
        return "█" * filled + "░" * (width - filled)

    @staticmethod
    def _display_width(text: str) -> int:
        width = 0
        for character in str(text):
            if unicodedata.combining(character):
                continue
            width += 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
        return width

    @classmethod
    def _fit_line(cls, text: str, width: int) -> str:
        if width <= 0:
            return ""
        text = str(text)
        if cls._display_width(text) <= width:
            return text
        if width == 1:
            return "…"
        result: list[str] = []
        used = 0
        for character in text:
            character_width = (
                0
                if unicodedata.combining(character)
                else 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
            )
            if used + character_width > width - 1:
                break
            result.append(character)
            used += character_width
        return "".join(result) + "…"

    def _terminal_size(self) -> os.terminal_size:
        try:
            return os.get_terminal_size(self.output.fileno())
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
            return shutil.get_terminal_size(fallback=(120, 24))

    def _clear_live(self) -> None:
        if not self.enabled or self.rendered_lines <= 0:
            return
        for _ in range(self.rendered_lines):
            self.output.write("\033[1A\r\033[2K")
        self.rendered_lines = 0

    def _dashboard_lines(self, details: dict[str, Any]) -> list[str]:
        overall_percent = details["time_percent"]
        current_percent = details["current_percent"]
        current = details["current"]
        category_prefix = f"{details.get('category')}/" if details.get("category") and details["category"] != "-" else ""
        if category_prefix and not str(current).startswith(category_prefix):
            current = f"{details['category']}/{current}"
        lines = [
            "インハイTV 試合別動画切り出し",
            "進捗（この領域を更新します。Ctrl+Cで中断）",
            (
                f"[全体] [{self._bar(overall_percent)}] {overall_percent:5.1f}%"
                f" | 完了 {details['completed']}/{details['total']}件"
                f" ({details['count_percent']:4.1f}%)"
                f" | 残り {details['overall_remaining_text']}"
                f" | 終了予定 {details['overall_eta']}"
            ),
            (
                f"[現在] [{self._bar(current_percent, width=16)}] {current_percent:5.1f}%"
                f" | {current}"
            ),
            (
                f"        処理 {details['current_done_text']}"
                f" | 速度 {details['current_speed_text']}"
                f" | 残り {details['current_remaining_text']}"
                f" | 終了予定 {details['current_eta']}"
            ),
            f"経過 {details['elapsed_text']} | Ctrl+Cで中断・次回再開",
        ]
        width = max(1, self._terminal_size().columns - 1)
        return [self._fit_line(line, width) for line in lines]

    def event(self, message: str) -> None:
        if self.enabled:
            self._clear_live()
            if self.cursor_hidden:
                self.output.write("\033[?25h")
                self.cursor_hidden = False
        print(message, file=self.output, flush=True)

    def progress(self, message: str, details: dict[str, Any]) -> None:
        if not self.enabled:
            timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
            print(f"[{timestamp}] {message}", file=self.output, flush=True)
            return

        now = time.monotonic()
        if self.last_rendered_at and now - self.last_rendered_at < 0.2:
            return
        if not self.cursor_hidden:
            self.output.write("\033[?25l")
            self.cursor_hidden = True
        self._clear_live()
        lines = self._dashboard_lines(details)
        for line in lines:
            self.output.write(f"\r\033[2K{line}\n")
        self.output.flush()
        self.rendered_lines = len(lines)
        self.last_rendered_at = now

    def finish(self) -> None:
        if self.enabled:
            self._clear_live()
            if self.cursor_hidden:
                self.output.write("\033[?25h")
                self.output.flush()
                self.cursor_hidden = False


class OutputLock:
    """同じ出力先で二重実行しないためのプロセス間ロック。"""

    def __init__(self, target: Path):
        self.path = target / ".crop.lock"
        self.handle = None

    def __enter__(self) -> "OutputLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.handle.close()
            self.handle = None
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise RuntimeError(f"別の切り出し処理が実行中です（ロック: {self.path}）") from error
            raise
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()} started={datetime.now().astimezone().isoformat()}\n")
        self.handle.flush()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="元の年月日・コート別アーカイブ")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="試合別MP4の出力先")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="試合データJSON")
    parser.add_argument("--log", type=Path, help="ログファイル（既定: target/crop.log）")
    parser.add_argument("--state", type=Path, help="状態ファイル（既定: target/crop-state.json）")
    parser.add_argument("--interval", type=float, default=5.0, help="進捗表示の間隔（秒、既定: 5）")
    parser.add_argument("--reencode", action="store_true", help="フレーム単位で正確に切り出す（非常に時間がかかります）")
    parser.add_argument("--dry-run", action="store_true", help="実行せず、対象数と出力先だけ確認")
    parser.add_argument("--no-tui", action="store_true", help="端末の進捗画面を使わず、進捗を1行ずつ出力")
    return parser.parse_args()


def side_name(side: dict[str, Any]) -> str:
    players = [str(player.get("name", "")).strip() for player in side.get("players", [])]
    players = [player for player in players if player]
    return "・".join(players) or str(side.get("school") or side.get("name") or "対戦者未確認").strip()


def safe_name(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", value)
    value = re.sub(r"\s+", "", value).strip(".")
    return value or "対戦カード未確認"


def legacy_safe_name(value: str) -> str:
    """空白を残していた旧CLIの名前を探すための互換サニタイズ。"""
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or "対戦カード未確認"


def category_dir(entry: dict[str, Any]) -> str:
    if entry.get("tournamentType") == "team":
        return "TEAM-M" if entry.get("category") == "男子" else "TEAM-W"
    return str(entry.get("category") or "UNKNOWN")


def source_path(source: Path, entry: dict[str, Any]) -> Path:
    date = str(entry.get("date") or "")
    court = str(entry.get("court") or "").strip()
    try:
        court_number = int(court)
        filename = f"{date}_{court_number:02d}.mp4"
    except ValueError:
        filename = f"{date}_{court}.mp4"
    return source / date / filename


def find_tool(name: str) -> str:
    candidates = [shutil.which(name)]
    if name == "ffmpeg":
        candidates += ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    elif name == "ffprobe":
        candidates += ["/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError(f"{name} が見つかりません。Homebrew等でインストールしてPATHを確認してください。")


def duration_seconds(path: Path, ffprobe: str) -> float:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True,
        text=True,
        capture_output=True,
    )
    return float(result.stdout.strip())


def match_names(entry: dict[str, Any]) -> tuple[str, str]:
    sides = entry.get("sides") or []
    left = side_name(sides[0]) if len(sides) > 0 else "対戦者未確認"
    right = side_name(sides[1]) if len(sides) > 1 else "対戦者未確認"
    return left, right


def school_name(side: dict[str, Any]) -> str:
    return str(side.get("school") or side.get("name") or "学校名未確認").strip() or "学校名未確認"


# BIRD SCOREのGT-23だけは公開match.jsonのroundIdが空欄です。
# 大会公式トーナメント表で2回戦の位置を照合済みのため、この1件だけ補正します。
# これ以外の空欄を番号から推測することはしません。
VERIFIED_ROUND_OVERRIDES = {
    ("team", "GT-23"): "2回戦",
}


def round_name(entry: dict[str, Any]) -> str:
    """回戦ディレクトリ名を大会の表記へ正規化する。"""
    value = str(entry.get("round") or "").strip()
    if value in ROUND_LABELS:
        return value
    key = (str(entry.get("tournamentType") or ""), str(entry.get("matchNo") or ""))
    return VERIFIED_ROUND_OVERRIDES.get(key, "回戦未確認")


def legacy_output_candidates(target: Path, entry: dict[str, Any]) -> list[Path]:
    """旧形式・中間形式を新形式へ移行するための候補。"""
    left, right = match_names(entry)
    category = category_dir(entry)
    old_bases = [
        legacy_safe_name(f"{left}-{right}"),
        safe_name(f"{left}-{right}"),
    ]
    suffix = legacy_safe_name(f"{entry.get('matchNo') or '試合'}-{entry.get('orderName') or 'order'}")
    candidates: list[Path] = []

    # 旧形式: 種目ディレクトリ直下。
    flat_directory = target / category
    for base in old_bases:
        candidates.extend([flat_directory / f"{base}.mp4", flat_directory / f"{base}__{suffix}.mp4"])
        candidates.extend(sorted(flat_directory.glob(f"{base}__*.mp4")))

    # 中間形式: 団体は学校対戦、個人は回戦/選手対戦の下にファイルを置いていた。
    sides = entry.get("sides") or []
    if entry.get("tournamentType") == "team":
        left_school = school_name(sides[0]) if len(sides) > 0 else "学校名未確認"
        right_school = school_name(sides[1]) if len(sides) > 1 else "学校名未確認"
        group_names = [
            legacy_safe_name(f"{left_school}vs{right_school}"),
            safe_name(f"{left_school}vs{right_school}"),
        ]
        for group_name in group_names:
            for base in old_bases:
                candidates.extend([
                    target / category / group_name / f"{base}.mp4",
                    target / category / group_name / f"{base}__{suffix}.mp4",
                ])
    else:
        round_value = round_name(entry)
        round_names = [legacy_safe_name(round_value), safe_name(round_value)]
        matchup_names = [
            legacy_safe_name(f"{left}vs{right}"),
            safe_name(f"{left}vs{right}"),
        ]
        for round_directory_name in round_names:
            for matchup_name in matchup_names:
                for base in old_bases:
                    candidates.extend([
                        target / category / round_directory_name / matchup_name / f"{base}.mp4",
                        target / category / round_directory_name / matchup_name / f"{base}__{suffix}.mp4",
                    ])
    return list(dict.fromkeys(candidates))


def output_path(target: Path, entry: dict[str, Any], used: set[Path]) -> Path:
    left, right = match_names(entry)
    category = category_dir(entry)
    sides = entry.get("sides") or []
    if entry.get("tournamentType") == "team":
        base = safe_name(f"{left}-{right}")
        left_school = school_name(sides[0]) if len(sides) > 0 else "学校名未確認"
        right_school = school_name(sides[1]) if len(sides) > 1 else "学校名未確認"
        group_directory = safe_name(f"{left_school}vs{right_school}")
        directory = target / category / group_directory
    else:
        base = safe_name(f"{left}vs{right}")
        round_directory = safe_name(round_name(entry))
        directory = target / category / round_directory
    candidate = directory / f"{base}.mp4"
    if candidate not in used:
        return candidate
    suffix = safe_name(f"{entry.get('matchNo') or '試合'}-{entry.get('orderName') or 'order'}")
    candidate = directory / f"{base}__{suffix}.mp4"
    index = 2
    while candidate in used or candidate.exists():
        candidate = directory / f"{base}__{suffix}-{index}.mp4"
        index += 1
    return candidate


def build_jobs(data: dict[str, Any], source_root: Path, target: Path, ffprobe: str | None) -> list[Job]:
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
    grouped: dict[Path, list[dict[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(source_path(source_root, entry), []).append(entry)

    jobs: list[Job] = []
    used: set[Path] = set()
    for source, group in sorted(grouped.items(), key=lambda pair: str(pair[0])):
        if not source.is_file():
            print(f"警告: 元動画が見つからないため対象外: {source}", file=sys.stderr)
            continue
        archive_key = (source.parent.name, source.stem.rsplit("_", 1)[-1].lstrip("0") or "0")
        archive_duration = archive_durations.get(archive_key)
        if archive_duration is None:
            if ffprobe is None:
                raise RuntimeError(f"durationSecondsがなくffprobeが必要です: {source}")
            archive_duration = duration_seconds(source, ffprobe)
        else:
            # ローカル保存版の末尾を切り落とさないための余裕。ffmpegはEOFで停止します。
            archive_duration += 60.0
        ordered = sorted(group, key=lambda entry: float(entry["startSeconds"]))
        for index, entry in enumerate(ordered):
            start = float(entry["startSeconds"])
            next_start = float(ordered[index + 1]["startSeconds"]) if index + 1 < len(ordered) else archive_duration
            end = min(archive_duration, next_start)
            if start < 0 or end <= start:
                print(f"警告: 時間範囲が不正なため対象外: {entry.get('id')}", file=sys.stderr)
                continue
            output = output_path(target, entry, used)
            used.add(output)
            jobs.append(Job(entry=entry, source=source, output=output, start=start, duration=end - start))
    return jobs


def find_legacy_output(target: Path, job: Job, used: set[Path]) -> Path | None:
    for candidate in legacy_output_candidates(target, job.entry):
        if candidate in used or not candidate.is_file() or candidate.name.startswith("._"):
            continue
        try:
            if candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def completed_output_count(jobs: list[Job], target: Path) -> int:
    """新形式と旧形式を合わせた再開可能な完成件数を数える。"""
    count = 0
    legacy_used: set[Path] = set()
    for job in jobs:
        try:
            if job.output.is_file() and job.output.stat().st_size > 0:
                count += 1
                continue
        except OSError:
            pass
        legacy = find_legacy_output(target, job, legacy_used)
        if legacy is not None:
            legacy_used.add(legacy)
            count += 1
    return count


def migrate_legacy_outputs(jobs: list[Job], target: Path) -> tuple[int, list[str]]:
    """旧平置き完成動画を新しい分類ディレクトリへコピーする（旧ファイルは残す）。"""
    migrated = 0
    errors: list[str] = []
    legacy_used: set[Path] = set()
    for job in jobs:
        if job.output.is_file() and job.output.stat().st_size > 0:
            continue
        legacy = find_legacy_output(target, job, legacy_used)
        if legacy is None:
            continue
        legacy_used.add(legacy)
        job.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = job.output.with_name(f".{job.output.name}.legacy-{os.getpid()}.part")
        try:
            shutil.copy2(legacy, temporary)
            temporary.replace(job.output)
            migrated += 1
        except OSError as error:
            errors.append(f"{legacy} -> {job.output}: {error}")
            try:
                temporary.unlink()
            except OSError:
                pass
    return migrated, errors


def ffmpeg_command(ffmpeg: str, job: Job, temporary: Path, reencode: bool) -> list[str]:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-ss",
        f"{job.start:.3f}",
        "-i",
        str(job.source),
        "-t",
        f"{job.duration:.3f}",
        "-map",
        "0",
    ]
    if reencode:
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac"]
    else:
        command += ["-c", "copy"]
    command += ["-avoid_negative_ts", "make_zero", "-y", str(temporary)]
    return command


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def install_signal_handlers() -> None:
    def request_stop(signum: int, _frame: Any) -> None:
        global STOP_REQUESTED, STOP_SIGNAL, ACTIVE_PROCESS
        if STOP_REQUESTED:
            return
        STOP_REQUESTED = True
        STOP_SIGNAL = signum
        if ACTIVE_PROCESS is not None and ACTIVE_PROCESS.poll() is None:
            ACTIVE_PROCESS.terminate()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}時間{minutes:02d}分"
    return f"{minutes}分{seconds:02d}秒"


def format_eta(remaining: float, speed: float) -> str:
    if remaining <= 0:
        return "完了"
    if speed <= 0:
        return "計算中"
    return (datetime.now().astimezone() + timedelta(seconds=max(0.0, remaining / speed))).strftime("%Y-%m-%d %H:%M頃")


def progress_details(
    completed: int,
    total: int,
    media_done: float,
    media_total: float,
    current: str,
    speed: float,
    elapsed: float,
    category: str = "-",
    current_duration: float | None = None,
    current_done: float | None = None,
    current_elapsed: float | None = None,
) -> dict[str, Any]:
    count_percent = completed / total * 100 if total else 100.0
    time_percent = media_done / media_total * 100 if media_total else count_percent
    overall_remaining = max(0.0, media_total - media_done)
    if current_duration is None or current_done is None:
        current_percent = 0.0
        current_done_text = "-"
        current_speed_text = "-"
        current_remaining_text = "-"
        current_eta = "-"
    else:
        current_remaining = max(0.0, current_duration - current_done)
        current_percent = current_done / current_duration * 100 if current_duration > 0 else 100.0
        current_speed = current_done / current_elapsed if current_elapsed and current_elapsed > 0 else 0.0
        current_done_text = f"{format_seconds(current_done)} / {format_seconds(current_duration)}"
        current_speed_text = f"{current_speed:.2f}倍速" if current_speed > 0 else "計算中"
        current_remaining_text = format_seconds(current_remaining / current_speed) if current_speed > 0 else "計算中"
        current_eta = format_eta(current_remaining, current_speed)
    return {
        "completed": completed,
        "total": total,
        "count_percent": count_percent,
        "time_percent": time_percent,
        "current": current,
        "category": category,
        "speed": speed,
        "current_percent": current_percent,
        "current_done_text": current_done_text,
        "current_speed_text": current_speed_text,
        "current_remaining_text": current_remaining_text,
        "current_eta": current_eta,
        "overall_remaining_text": format_seconds(overall_remaining / speed) if speed > 0 else "計算中",
        "overall_eta": format_eta(overall_remaining, speed),
        "elapsed_text": format_seconds(elapsed),
    }


def progress_line(
    completed: int,
    total: int,
    media_done: float,
    media_total: float,
    current: str,
    speed: float,
    elapsed: float,
    current_duration: float | None = None,
    current_done: float | None = None,
    current_elapsed: float | None = None,
) -> str:
    details = progress_details(
        completed,
        total,
        media_done,
        media_total,
        current,
        speed,
        elapsed,
        current_duration=current_duration,
        current_done=current_done,
        current_elapsed=current_elapsed,
    )
    return (
        f"全体 {details['time_percent']:5.1f}% | 完了 {details['completed']}/{details['total']}件 "
        f"({details['count_percent']:5.1f}%) | 処理中 {details['current']} | "
        f"速度 {details['speed']:4.2f}倍速 | "
        f"動画残り {details['current_remaining_text']} | "
        f"全体処理終了予測 {details['overall_eta']} / 全体残り {details['overall_remaining_text']} | "
        f"経過 {details['elapsed_text']}"
    )


def run_ffmpeg(
    ffmpeg: str,
    job: Job,
    temporary: Path,
    reencode: bool,
    on_progress: Callable[[float], None],
) -> tuple[bool, str]:
    global ACTIVE_PROCESS
    command = ffmpeg_command(ffmpeg, job, temporary, reencode)
    try:
        process: subprocess.Popen[str] = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        ACTIVE_PROCESS = process
        selector = selectors.DefaultSelector()
        assert process.stdout is not None
        assert process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, "progress")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        error_lines: list[str] = []
        measured = 0.0
        stop_started: float | None = None
        while True:
            if STOP_REQUESTED and process.poll() is None:
                if stop_started is None:
                    stop_started = time.monotonic()
                elif time.monotonic() - stop_started > STOP_GRACE_SECONDS:
                    process.kill()
            events = selector.select(timeout=0.5)
            for key, _ in events:
                line = key.fileobj.readline()
                if not line:
                    try:
                        selector.unregister(key.fileobj)
                    except Exception:
                        pass
                    continue
                line = line.strip()
                if key.data == "progress":
                    if line.startswith("out_time_ms="):
                        try:
                            measured = min(job.duration, max(0.0, float(line.split("=", 1)[1]) / 1_000_000))
                            on_progress(measured)
                        except ValueError:
                            pass
                elif line:
                    error_lines.append(line)
            return_code = process.poll()
            if return_code is not None:
                break
        selector.close()
        process.wait()
        if STOP_REQUESTED:
            return False, "中断"
        if process.returncode != 0:
            return False, " ".join(error_lines[-3:]) or f"ffmpeg終了コード={process.returncode}"
        on_progress(job.duration)
        return True, ""
    except OSError as error:
        return False, str(error)
    finally:
        ACTIVE_PROCESS = None


def relative_output(target: Path, path: Path) -> str:
    try:
        return str(path.relative_to(target))
    except ValueError:
        return str(path)


def category_summary(jobs: list[Job]) -> str:
    counts: dict[str, int] = {}
    for job in jobs:
        category = category_dir(job.entry)
        counts[category] = counts.get(category, 0) + 1
    order = ("MS", "MD", "WS", "WD", "TEAM-M", "TEAM-W")
    return " / ".join(f"{category} {counts.get(category, 0)}件" for category in order)


def create_state(args: argparse.Namespace, jobs: list[Job]) -> dict[str, Any]:
    return {
        "schemaVersion": 4,
        "filenamePolicy": "whitespace-free",
        "status": "running",
        "startedAt": datetime.now().astimezone().isoformat(),
        "updatedAt": datetime.now().astimezone().isoformat(),
        "source": str(args.source),
        "target": str(args.target),
        "total": len(jobs),
        "totalMediaSeconds": sum(job.duration for job in jobs),
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "current": None,
        "jobs": [
            {
                "id": job.entry.get("id"),
                "path": relative_output(args.target, job.output),
                "status": "pending",
                "source": str(job.source),
                "startSeconds": job.start,
                "durationSeconds": job.duration,
            }
            for job in jobs
        ],
    }


def update_state(state: dict[str, Any], state_path: Path) -> None:
    state["updatedAt"] = datetime.now().astimezone().isoformat()
    atomic_write_json(state_path, state)


def main() -> int:
    global LOGGER, STOP_REQUESTED
    args = parse_args()
    if args.interval <= 0:
        print("--interval は0より大きい値で指定してください。", file=sys.stderr)
        return 2
    if not args.data.is_file():
        print(f"試合データがありません: {args.data}", file=sys.stderr)
        return 2
    if not args.source.is_dir():
        print(f"元アーカイブがありません: {args.source}", file=sys.stderr)
        return 2
    source_resolved = args.source.resolve()
    target_resolved = args.target.resolve()
    if target_resolved == source_resolved or source_resolved in target_resolved.parents:
        print("--target は元アーカイブの外側に指定してください（元動画保護のため）。", file=sys.stderr)
        return 2
    args.target.mkdir(parents=True, exist_ok=True)
    log_path = args.log or args.target / "crop.log"
    state_path = args.state or args.target / "crop-state.json"

    try:
        ffmpeg = find_tool("ffmpeg") if not args.dry_run else None
        ffprobe = find_tool("ffprobe") if not args.dry_run else None
        data = json.loads(args.data.read_text(encoding="utf-8"))
        jobs = build_jobs(data, args.source, args.target, ffprobe)
        unresolved_rounds = [
            f"{job.entry.get('tournamentType')} {job.entry.get('matchNo')} ({job.entry.get('id')})"
            for job in jobs
            if round_name(job.entry) == "回戦未確認"
        ]
        if unresolved_rounds:
            sample = "、".join(unresolved_rounds[:5])
            suffix = "" if len(unresolved_rounds) <= 5 else f" ほか{len(unresolved_rounds) - 5}件"
            raise RuntimeError(f"回戦未確認の試合があるため開始しません: {sample}{suffix}")
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"準備に失敗しました: {error}", file=sys.stderr)
        return 2

    media_total = sum(job.duration for job in jobs)
    existing_count = completed_output_count(jobs, args.target)
    print(f"対象: {len(jobs)}件")
    print(f"完了済み: {existing_count}件 / 残り: {len(jobs) - existing_count}件")
    print(f"種目別: {category_summary(jobs)}")
    if args.dry_run:
        for job in jobs:
            print(f"{category_dir(job.entry)} {job.entry.get('matchNo')} {job.entry.get('orderName')} "
                  f"{job.start:.0f}s {job.duration:.0f}s -> {job.output}")
        return 0

    lock = OutputLock(args.target)
    try:
        lock.__enter__()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2

    terminal = TerminalUI(enabled=sys.stdout.isatty() and not args.no_tui)
    LOGGER = RunLogger(log_path, terminal)
    install_signal_handlers()
    LOGGER.write("出力構成を確認中（既存の旧形式動画は削除せず、新しい分類先へコピーします）")
    migrated_count, migration_errors = migrate_legacy_outputs(jobs, args.target)
    if migrated_count:
        LOGGER.write(f"旧形式から新形式へコピー: {migrated_count}件（旧ファイルは残しています）")
    for migration_error in migration_errors:
        LOGGER.write(f"旧形式のコピー失敗: {migration_error}")
    existing_count = sum(1 for job in jobs if job.output.is_file() and job.output.stat().st_size > 0)
    state = create_state(args, jobs)
    state["completed"] = existing_count
    for item, job in zip(state["jobs"], jobs):
        if job.output.is_file() and job.output.stat().st_size > 0:
            item["status"] = "existing"
    update_state(state, state_path)

    started = time.monotonic()
    media_done = sum(job.duration for job in jobs if job.output.is_file() and job.output.stat().st_size > 0)
    last_progress = 0.0
    last_report = 0.0
    LOGGER.write(f"開始: 対象 {len(jobs)}件、完了済み {existing_count}件、残り {len(jobs) - existing_count}件")
    LOGGER.write(f"種目別出力: {category_summary(jobs)}（出力先直下に各ディレクトリを作成）")
    LOGGER.write(f"入力（変更しません）: {args.source}")
    LOGGER.write(f"出力: {args.target} / 状態: {state_path}")
    try:
        for index, (item, job) in enumerate(zip(state["jobs"], jobs), start=1):
            if job.output.is_file() and job.output.stat().st_size > 0:
                continue
            if STOP_REQUESTED:
                break
            item["status"] = "running"
            state["current"] = relative_output(args.target, job.output)
            update_state(state, state_path)
            job.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = job.output.with_name(f"{job.output.stem}.part{job.output.suffix}")
            if temporary.exists():
                LOGGER.write(f"前回の未完了ファイルを上書きします: {temporary.name}")
            current_done = 0.0
            current_started = time.monotonic()

            initial_current = relative_output(args.target, job.output)
            initial_message = progress_line(
                state["completed"], len(jobs), media_done, media_total,
                initial_current, 0.0, time.monotonic() - started,
                current_duration=job.duration, current_done=0.0, current_elapsed=0.001,
            )
            initial_details = progress_details(
                state["completed"], len(jobs), media_done, media_total,
                initial_current, 0.0, time.monotonic() - started, category_dir(job.entry),
                current_duration=job.duration, current_done=0.0, current_elapsed=0.001,
            )
            LOGGER.progress(initial_message, initial_details)

            def report(current: float) -> None:
                nonlocal current_done, last_report, last_progress
                current_done = max(current_done, current)
                now = time.monotonic()
                if now - last_report < args.interval and current < job.duration:
                    return
                last_report = now
                elapsed = max(0.001, now - started)
                speed = (media_done + current_done) / elapsed
                current_output = relative_output(args.target, job.output)
                message = progress_line(state["completed"], len(jobs), media_done + current_done, media_total,
                                        current_output, speed, elapsed,
                                        current_duration=job.duration,
                                        current_done=current_done,
                                        current_elapsed=max(0.001, now - current_started))
                details = progress_details(state["completed"], len(jobs), media_done + current_done, media_total,
                                           current_output, speed, elapsed, category_dir(job.entry),
                                           current_duration=job.duration,
                                           current_done=current_done,
                                           current_elapsed=max(0.001, now - current_started))
                LOGGER.progress(message, details)
                last_progress = current_done

            success, error_message = run_ffmpeg(ffmpeg, job, temporary, args.reencode, report)
            if success and not (temporary.is_file() and temporary.stat().st_size > 0):
                success = False
                error_message = "ffmpegが出力ファイルを作成しませんでした"
            if success:
                temporary.replace(job.output)
                item["status"] = "completed"
                state["completed"] += 1
                media_done += job.duration
                state["current"] = None
                update_state(state, state_path)
                elapsed = time.monotonic() - started
                speed = media_done / max(0.001, elapsed)
                message = progress_line(state["completed"], len(jobs), media_done, media_total,
                                        "次の試合を準備中", speed, elapsed)
                details = progress_details(state["completed"], len(jobs), media_done, media_total,
                                           "次の試合を準備中", speed, elapsed, "-")
                LOGGER.progress(message, details)
            elif error_message == "中断" or STOP_REQUESTED:
                item["status"] = "cancelled"
                state["cancelled"] += 1
                update_state(state, state_path)
                break
            else:
                item["status"] = "failed"
                state["failed"] += 1
                update_state(state, state_path)
                LOGGER.write(f"失敗: {job.output.name} ({error_message})")
    except KeyboardInterrupt:
        STOP_REQUESTED = True
    finally:
        if STOP_REQUESTED:
            if STOP_SIGNAL is not None:
                LOGGER.write(f"停止要求を受信しました（signal={STOP_SIGNAL}）。現在の試合を安全に終了しました。")
            state["status"] = "cancelled"
            if state.get("current"):
                state["cancelled"] = max(1, state["cancelled"])
            update_state(state, state_path)
            LOGGER.write(f"中断: {state['completed']}/{len(jobs)}件完了。次回はここから再開します。")
        elif state["failed"]:
            state["status"] = "partial"
            update_state(state, state_path)
            LOGGER.write(f"一部完了: 完了 {state['completed']}件 / 失敗 {state['failed']}件 / 対象 {len(jobs)}件")
        else:
            state["status"] = "completed"
            update_state(state, state_path)
            LOGGER.write(f"完了: {state['completed']}/{len(jobs)}件")
        LOGGER.close()
        LOGGER = None
        lock.__exit__(None, None, None)
    return 130 if STOP_REQUESTED else (1 if state["failed"] else 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n中断しました。状態ファイルを確認して、次回同じコマンドで再開してください。", file=sys.stderr)
        raise SystemExit(130)
