#!/usr/bin/env python3
import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Dict, List, Set, Tuple


EPOCH_FILE_RE = re.compile(r"epoch(\d+)\.json$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy epoch files to a new directory and remove run entries for the "
            "specified nodes from each epoch JSON."
        )
    )
    parser.add_argument(
        "--epochs-dir",
        required=True,
        help="Directory containing the source epoch JSON files",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where filtered files will be written",
    )
    parser.add_argument(
        "--nodes",
        required=True,
        help="Comma-separated list of node names to remove from the run section",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="Indentation to use when writing JSON (default: 2)",
    )
    return parser.parse_args()


def epoch_sort_key(path: Path) -> Tuple[int, str]:
    match = EPOCH_FILE_RE.search(path.name)
    if match:
        return (int(match.group(1)), path.name)
    return (10**18, path.name)


def parse_nodes(value: str) -> Set[str]:
    nodes = {item.strip() for item in value.split(",") if item.strip()}
    if not nodes:
        raise ValueError("No valid node names provided in --nodes.")
    return nodes


def load_epoch_files(epochs_dir: Path) -> List[Tuple[Path, Dict[str, object]]]:
    files: List[Tuple[Path, Dict[str, object]]] = []
    for path in sorted(epochs_dir.glob("*.json"), key=epoch_sort_key):
        with path.open("r", encoding="utf-8") as fh:
            files.append((path, json.load(fh)))
    return files


def remove_run_entries(epoch_docs: List[Tuple[Path, Dict[str, object]]], nodes_to_remove: Set[str]) -> int:
    removed_entries = 0
    for _, doc in epoch_docs:
        run_section = doc.get("run")
        if not isinstance(run_section, dict):
            continue

        for node in nodes_to_remove:
            if node in run_section:
                del run_section[node]
                removed_entries += 1

        if not run_section:
            doc.pop("run", None)

    return removed_entries


def copy_non_json_files(source_dir: Path, output_dir: Path) -> None:
    for path in source_dir.glob("*"):
        if path.is_file() and path.suffix != ".json":
            shutil.copy2(path, output_dir / path.name)


def write_epoch_files(
    epoch_docs: List[Tuple[Path, Dict[str, object]]],
    source_dir: Path,
    output_dir: Path,
    indent: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    copy_non_json_files(source_dir, output_dir)

    for path, doc in epoch_docs:
        destination = output_dir / path.name
        with destination.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=indent)
            fh.write("\n")


def main() -> None:
    args = parse_args()
    source_dir = Path(args.epochs_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not source_dir.is_dir():
        raise SystemExit(f"Epoch directory not found: {source_dir}")
    if output_dir == source_dir:
        raise SystemExit("--output-dir must be different from --epochs-dir.")

    try:
        nodes_to_remove = parse_nodes(args.nodes)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    epoch_docs = load_epoch_files(source_dir)
    if not epoch_docs:
        raise SystemExit(f"No JSON epoch files found in: {source_dir}")

    removed_entries = remove_run_entries(epoch_docs, nodes_to_remove)
    write_epoch_files(epoch_docs, source_dir, output_dir, args.indent)

    print(f"Copied {len(epoch_docs)} epoch files to {output_dir}")
    print(f"Removed {removed_entries} run entries for nodes: {', '.join(sorted(nodes_to_remove))}")


if __name__ == "__main__":
    main()
