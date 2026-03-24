#!/usr/bin/env python3
import argparse
import json
import re
import shutil
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple, Union


@dataclass
class PendingAdd:
    epoch_time: datetime
    link_obj: Dict[str, object]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Annotate epoch JSON traces with expected_duration on each links-add entry, "
            "computed as the elapsed time until the matching links-del event."
        )
    )
    parser.add_argument(
        "--epochs-dir",
        help="Directory containing NetSatBench epoch JSON files",
    )
    parser.add_argument(
        "--output-dir",
        help="Optional output directory. If omitted, files are updated in place.",
    )
    parser.add_argument(
        "--file-pattern",default="NetSatBench-epoch*.json",
        help="Override epoch filename pattern (takes precedence over Etcd).",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="Indentation to use when writing JSON (default: 2)",
    )
    return parser.parse_args()


def parse_epoch_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def link_key(link_obj: Dict[str, object]) -> Tuple[str, str]:
    ep1 = str(link_obj.get("endpoint1", ""))
    ep2 = str(link_obj.get("endpoint2", ""))
    return tuple(sorted((ep1, ep2)))



def list_epoch_files(epoch_dir: Union[str, Path], file_pattern: str) -> List[Path]:
    if not epoch_dir or not file_pattern:
        return []

    epoch_dir = Path(epoch_dir)

    def last_numeric_suffix(path: Path) -> int:
        """
        Extracts the last contiguous sequence of digits from the filename
        and returns it as an integer. If no digits are found, returns -1.
        """
        basename = path.name
        matches = re.findall(r"(\d+)", basename)
        return int(matches[-1]) if matches else -1

    files = sorted(epoch_dir.glob(file_pattern), key=last_numeric_suffix)
    return files


def load_epoch_files(epoch_dir: Union[str, Path], file_pattern: str) -> List[Tuple[Path, Dict[str, object]]]:
    files: List[Tuple[Path, Dict[str, object]]] = []
    for path in list_epoch_files(epoch_dir, file_pattern):
        with path.open("r", encoding="utf-8") as fh:
            files.append((path, json.load(fh)))
    return files


def annotate_expected_durations(epoch_docs: List[Tuple[Path, Dict[str, object]]]) -> Tuple[int, int]:
    pending_by_link: Dict[Tuple[str, str], Deque[PendingAdd]] = defaultdict(deque)
    matched = 0
    unmatched = 0

    for f, doc in epoch_docs:
        print(f"🖊️ Processing file {f}")
        epoch_time = parse_epoch_time(str(doc["time"]))

        for link_obj in doc.get("links-del", []):
            if not isinstance(link_obj, dict):
                continue
            queue = pending_by_link.get(link_key(link_obj))
            if not queue:
                continue
            pending = queue.popleft()
            duration_s = (epoch_time - pending.epoch_time).total_seconds()
            pending.link_obj["expected_duration"] = max(duration_s, 0.0)
            matched += 1

        # Process adds after dels so a delete and re-add in the same epoch closes the
        # previous lifetime first, then opens a fresh lifetime starting at this epoch.
        for link_obj in doc.get("links-add", []):
            if not isinstance(link_obj, dict):
                continue
            link_obj["expected_duration"] = None
            pending_by_link[link_key(link_obj)].append(PendingAdd(epoch_time=epoch_time, link_obj=link_obj))

    for queue in pending_by_link.values():
        unmatched += len(queue)

    return matched, unmatched


def write_epoch_files(
    epoch_docs: List[Tuple[Path, Dict[str, object]]],
    source_dir: Path,
    output_dir: Optional[Path],
    indent: int,
) -> None:
    target_dir = output_dir or source_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    if output_dir is not None:
        for path in source_dir.glob("*"):
            if path.is_file() and path.suffix != ".json":
                shutil.copy2(path, target_dir / path.name)

    for path, doc in epoch_docs:
        destination = target_dir / path.name
        with destination.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=indent)
            fh.write("\n")


def main() -> None:
    args = parse_args()
    source_dir = Path(args.epochs_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
    file_pattern = args.file_pattern

    if not source_dir.is_dir():
        raise SystemExit(f"Epoch directory not found: {source_dir}")

    epoch_docs = load_epoch_files(source_dir, file_pattern)
    if not epoch_docs:
        raise SystemExit(f"No JSON epoch files found in: {source_dir}")

    matched, unmatched = annotate_expected_durations(epoch_docs)
    write_epoch_files(epoch_docs, source_dir, output_dir, args.indent)

    destination = output_dir or source_dir
    print(f"Annotated {len(epoch_docs)} epoch files in {destination}")
    print(f"Matched add/del pairs: {matched}")
    print(f"Open adds without a later del: {unmatched}")


if __name__ == "__main__":
    main()
