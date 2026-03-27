#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Pattern, Tuple, Union


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite epoch JSON traces of links between node matching regexes to have the specified netem parameters (delay, loss, rate). "
        )
    )
    parser.add_argument(
        "--epochs-dir",
        required=True,
        help="Directory containing NetSatBench epoch JSON files",
    )
    parser.add_argument(
        "--output-dir",
        help="Optional output directory. If omitted, files are updated in place.",
    )
    parser.add_argument(
        "--file-pattern",
        default="NetSatBench-epoch*.json",
        help="Override epoch filename pattern (takes precedence over Etcd).",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="Indentation to use when writing JSON (default: 2)",
    )
    parser.add_argument(
        "--node-regex1",
        type=str,
        default="^sat\d+$",
        help="First node type to consider (default: ^sat\d+$, match any node that starts with 'sat' followed by digits)",
    )
    parser.add_argument(
        "--node-regex2",
        type=str,
        default="^grd\d+$",
        help="Second node type to consider (default: ^grd\d+$, match any node that starts with 'grd' followed by digits)",
    )
    parser.add_argument(
        "--delay",
        type=str,
        default="-1",
        help="Delay to apply to links between the specified node types (default: -1, means no change)",
    )
    parser.add_argument(
        "--loss",
        type=float,
        default=-1,
        help="Loss percentage to apply to links between the specified node types (default: -1, means no change)",
    )
    parser.add_argument(
        "--rate",
        type=str,
        default="-1",
        help="Rate to apply to links between the specified node types (default: -1, means no change)",
    )
    return parser.parse_args()


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


def load_epoch_files(
    epoch_dir: Union[str, Path], file_pattern: str
) -> List[Tuple[Path, Dict[str, object]]]:
    files: List[Tuple[Path, Dict[str, object]]] = []
    for path in list_epoch_files(epoch_dir, file_pattern):
        with path.open("r", encoding="utf-8") as fh:
            files.append((path, json.load(fh)))
    return files


def endpoint_pair_matches(
    endpoint1: str,
    endpoint2: str,
    node_pattern1: Pattern[str],
    node_pattern2: Pattern[str],
) -> bool:
    return (node_pattern1.match(endpoint1) and node_pattern2.match(endpoint2)) or (
        node_pattern1.match(endpoint2) and node_pattern2.match(endpoint1)
    )


def update_link_netem_params(
    link_obj: Dict[str, object], delay: str, loss: int, rate: str
) -> str:
    updates: List[str] = []
    if delay != "-1":
        link_obj["delay"] = delay
        updates.append(f"delay={delay}")
    if loss != -1:
        link_obj["loss"] = loss
        updates.append(f"loss={loss}")
    if rate != "-1":
        link_obj["rate"] = rate
        updates.append(f"rate={rate}")
    return " ".join(updates)


def iter_link_objects(doc: Dict[str, object]) -> Iterable[Dict[str, object]]:
    for section_name in ("links-add", "links-update"):
        for link_obj in doc.get(section_name, []):
            if isinstance(link_obj, dict):
                yield link_obj


def inject_netem_params(
    epoch_docs: List[Tuple[Path, Dict[str, object]]],
    node_regex1: str,
    node_regex2: str,
    delay: str,
    loss: int,
    rate: str,
) -> None:
    node_pattern1 = re.compile(node_regex1)
    node_pattern2 = re.compile(node_regex2)

    for path, doc in epoch_docs:
        #extract file name from path
        file_name = path.name
        print(f"🖊️ Processing epoch file {file_name}")

        for link_obj in iter_link_objects(doc):
            ep1 = str(link_obj.get("endpoint1", ""))
            ep2 = str(link_obj.get("endpoint2", ""))
            if not endpoint_pair_matches(ep1, ep2, node_pattern1, node_pattern2):
                continue

            updates = update_link_netem_params(link_obj, delay, loss, rate)
            if updates:
                print(f"  🎛️ Updated link between {ep1} and {ep2} with {updates}")


def write_epoch_files(
    epoch_docs: List[Tuple[Path, Dict[str, object]]],
    output_dir: Path,
    indent: int,
) -> None:
    for path, doc in epoch_docs:
        destination = output_dir / path.name
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
    
    if not output_dir:
        response = input(f"⚠️ No output directory specified. This will modify the epoch files in place in {source_dir}. Do you want to proceed? (yes/no): ")
        if response.lower() != "yes":
            print("❌ Operation cancelled by user.")
            return
        else:
            output_dir = source_dir
    else:
        if output_dir.exists() and not output_dir.is_dir():
            raise SystemExit(f"❌ Output path exists but is not a directory: {output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)
        if output_dir and any(output_dir.iterdir()):
            response = input(f"⚠️ Output directory {output_dir} is not empty. Do you want to proceed and potentially overwrite files? (yes/no): ")
            if response.lower() != "yes":
                print("❌ Operation cancelled by user.")
                return

    inject_netem_params(epoch_docs, args.node_regex1, args.node_regex2, args.delay, args.loss, args.rate)
    write_epoch_files(epoch_docs, output_dir, args.indent)


if __name__ == "__main__":
    main()
