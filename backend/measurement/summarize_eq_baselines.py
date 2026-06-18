"""Print span/median summary for eq_measurements/*.baseline.json files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "eq_measurements"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args(argv)
    for path in sorted(args.dir.glob("*.baseline.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        resp = data["response_db"]
        print(
            path.name,
            data.get("measurement_mode", "?"),
            f"span={max(resp) - min(resp):.1f}",
            f"median={sorted(resp)[len(resp) // 2]:.1f}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
