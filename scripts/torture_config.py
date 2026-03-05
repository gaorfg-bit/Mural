#!/usr/bin/env python3
from __future__ import annotations

import json
import random
import sys
import time
from multiprocessing import Process
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mural.io_utils import ensure_private_dir, locked_file, atomic_save_json, load_json


CONFIG_DIR = Path.home() / ".config" / "mural"
CONFIG_PATH = CONFIG_DIR / "settings.json"


def writer(proc_id: int, n: int = 200) -> None:
    ensure_private_dir(CONFIG_DIR)
    for i in range(n):
        payload = {
            "proc": proc_id,
            "i": i,
            "t": time.time(),
            "rand": random.random(),
        }
        with locked_file(CONFIG_PATH):
            atomic_save_json(CONFIG_PATH, payload, mode=0o600)
        time.sleep(random.random() * 0.01)


def validator(n: int = 500) -> int:
    bad = 0
    for _ in range(n):
        if CONFIG_PATH.exists():
            try:
                with locked_file(CONFIG_PATH):
                    _ = load_json(CONFIG_PATH)
            except json.JSONDecodeError:
                bad += 1
        time.sleep(0.002)
    return bad


def main() -> None:
    p1 = Process(target=writer, args=(1, 200))
    p2 = Process(target=writer, args=(2, 200))
    p1.start()
    p2.start()

    bad = validator(800)

    p1.join()
    p2.join()

    final_bad = 0
    try:
        with locked_file(CONFIG_PATH):
            _ = load_json(CONFIG_PATH)
    except json.JSONDecodeError:
        final_bad = 1

    if bad != 0 or final_bad != 0:
        raise SystemExit(f"FAILED: JSON invalid reads={bad}, final={final_bad}")

    print("OK: no invalid JSON detected")


if __name__ == "__main__":
    main()
