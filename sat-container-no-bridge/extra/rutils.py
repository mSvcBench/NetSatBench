from typing import Mapping, Optional
from pathlib import Path


def replace_placeholders_in_file(
    input_path: str | Path,
    values: Mapping[str, str],
    output_path: Optional[str | Path] = None,
    ) -> None:
    """
    Replace {{key}} in the input file with values[key].

    - If output_path is None, overwrite input file
    - Otherwise, write result to output_path

    Raises KeyError if unresolved {{...}} placeholders remain.
    """
    in_path = Path(input_path)
    out_path = Path(output_path) if output_path else in_path

    text = in_path.read_text(encoding="utf-8")

    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)

    if "{{" in text or "}}" in text:
        raise KeyError("Unresolved {{...}} placeholder(s) remain in file")

    out_path.write_text(text, encoding="utf-8")