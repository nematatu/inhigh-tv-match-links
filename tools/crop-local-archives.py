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

STOP_REQUESTED = False
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
    """端末では進捗を同じ画面に再描画し、リダイレクト時は1行ずつ出す。"""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.rendered_lines = 0

    def _home(self) -> None:
        if self.rendered_lines > 1:
            sys.stdout.write(f"\033[{self.rendered_lines - 1}A")

    def _clear(self) -> None:
        if not self.enabled or not self.rendered_lines:
            return
        self._home()
        for index in range(self.rendered_lines):
            sys.stdout.write("\033[2K\r")
            if index < self.rendered_lines - 1:
                sys.stdout.write("\n")
        self._home()
        sys.stdout.flush()
        self.rendered_lines = 0

    @staticmethod
    def _clip(value: str, width: int) -> str:
        value = str(value)
        if len(value) <= width:
            return value
        return value[: max(0, width - 3)] + "..."

    def event(self, message: str) -> None:
        if self.enabled:
            self._clear()
        print(message, flush=True)

    def progress(self, message: str, details: dict[str, Any]) -> None:
        if not self.enabled:
            timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
            print(f"[{timestamp}] {message}", flush=True)
            return

        columns = shutil.get_terminal_size((100, 24)).columns
        inner = max(64, min(columns - 2, 116))
        current = self._clip(details["current"], inner - 8)
        title = "インハイTV 試合別動画切り出し"
        lines = [
            "┌─ " + title + " " + "─" * max(0, inner - len(title) - 4) + "┐",
            self._row(
                f"完了 {details['completed']}/{details['total']}件 ({details['count_percent']:4.1f}%)"
                f"  |  映像 {details['time_percent']:4.1f}%  |  速度 {details['speed']:6.2f}倍速",
                inner,
            ),
            self._row(f"現在 {details['category']} / {current}", inner),
            self._row(
                f"残り {details['remaining_text']}  |  予測終了 {details['eta']}  |  経過 {details['elapsed_text']}",
                inner,
            ),
            "└" + "─" * inner + "┘",
        ]
        if self.rendered_lines:
            self._home()
        for index, line in enumerate(lines):
            sys.stdout.write("\033[2K\r" + line)
            if index < len(lines) - 1:
                sys.stdout.write("\n")
        sys.stdout.flush()
        self.rendered_lines = len(lines)

    @staticmethod
    def _row(value: str, inner: int) -> str:
        return "│ " + value[: max(0, inner - 2)].ljust(max(0, inner - 2)) + " │"

    def finish(self) -> None:
        if self.enabled and self.rendered_lines:
            self._clear()


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


def output_path(target: Path, entry: dict[str, Any], used: set[Path]) -> Path:
    sides = entry.get("sides") or []
    left = side_name(sides[0]) if len(sides) > 0 else "対戦者未確認"
    right = side_name(sides[1]) if len(sides) > 1 else "対戦者未確認"
    base = safe_name(f"{left}-{right}")
    directory = target / category_dir(entry)
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
        global STOP_REQUESTED, ACTIVE_PROCESS
        if STOP_REQUESTED:
            return
        STOP_REQUESTED = True
        if LOGGER:
            LOGGER.write(f"停止要求を受信しました（signal={signum}）。現在の試合を安全に終了します。")
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
) -> dict[str, Any]:
    count_percent = completed / total * 100 if total else 100.0
    time_percent = media_done / media_total * 100 if media_total else count_percent
    remaining = max(0.0, media_total - media_done)
    return {
        "completed": completed,
        "total": total,
        "count_percent": count_percent,
        "time_percent": time_percent,
        "current": current,
        "category": category,
        "speed": speed,
        "remaining_text": format_seconds(remaining / speed) if speed > 0 else "計算中",
        "eta": format_eta(remaining, speed),
        "elapsed_text": format_seconds(elapsed),
    }


def progress_line(completed: int, total: int, media_done: float, media_total: float, current: str, speed: float, elapsed: float) -> str:
    details = progress_details(completed, total, media_done, media_total, current, speed, elapsed)
    return (
        f"進捗 {details['completed']}/{details['total']}件 ({details['count_percent']:5.1f}%) "
        f"/ 時間 {details['time_percent']:5.1f}% | 処理中 {details['current']} | "
        f"速度 {details['speed']:4.2f}倍速 | 残り {details['remaining_text']} "
        f"| 予測終了 {details['eta']} | 経過 {details['elapsed_text']}"
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
        "schemaVersion": 2,
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
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"準備に失敗しました: {error}", file=sys.stderr)
        return 2

    media_total = sum(job.duration for job in jobs)
    existing_count = sum(1 for job in jobs if job.output.is_file() and job.output.stat().st_size > 0)
    print(f"対象: {len(jobs)}件 / 総時間: {format_seconds(media_total)}")
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

            def report(current: float) -> None:
                nonlocal current_done, last_report, last_progress
                current_done = max(current_done, current)
                now = time.monotonic()
                if now - last_report < args.interval and current < job.duration:
                    return
                last_report = now
                elapsed = max(0.001, now - started)
                speed = (media_done + current_done) / elapsed
                message = progress_line(state["completed"], len(jobs), media_done + current_done, media_total,
                                        job.output.name, speed, elapsed)
                details = progress_details(state["completed"], len(jobs), media_done + current_done, media_total,
                                           job.output.name, speed, elapsed, category_dir(job.entry))
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
    raise SystemExit(main())
