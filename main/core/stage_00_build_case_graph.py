"""Build the deploy-time graph artifact from the two source parquet files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from stage_01_case_data import load_case_data


def build_graph(data_path: Path, entity_path: Path, output_path: Path) -> None:
    case_data = load_case_data(data_path, entity_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(case_data["graph"] | {
            "indexes": case_data["indexes"],
            "issue_by_key": case_data["issue_by_key"],
            "issue_keys": case_data["issue_keys"],
        },
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-data", type=Path, required=True)
    parser.add_argument("--entity-data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_graph(args.review_data, args.entity_data, args.output)


if __name__ == "__main__":
    main()
