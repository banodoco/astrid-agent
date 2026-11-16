"""A real filesystem/SQLite calibration, not a generation or a speed result."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sqlite3
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    python_sync_calls = []
    original_fsync = os.fsync
    def observed_fsync(fd):
        python_sync_calls.append(fd)
        return original_fsync(fd)
    os.fsync = observed_fsync
    interval = {"pid": os.getpid(), "epoch_start_ns": time.time_ns()}
    with (args.output / "calibration.bin").open("wb") as handle:
        handle.write(b"native flush calibration")
        handle.flush()
        os.fsync(handle.fileno())
    connection = sqlite3.connect(args.output / "calibration.sqlite")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("CREATE TABLE proof(value TEXT NOT NULL)")
    connection.commit()
    connection.execute("INSERT INTO proof VALUES ('stored')")
    connection.commit()
    assert connection.execute("SELECT value FROM proof").fetchall() == [("stored",)]
    connection.close()
    interval["epoch_reply_ns"] = time.time_ns()
    os.fsync = original_fsync
    assert len(python_sync_calls) == 1
    (args.output / "calibration-interval.json").write_text(json.dumps(interval, indent=2) + "\n")
    print(json.dumps({"interval": interval, "explicit_python_fsync_calls": len(python_sync_calls),
                      "sqlite_commits_exercised": 2, "stored_result_verified": True}))


if __name__ == "__main__":
    main()
