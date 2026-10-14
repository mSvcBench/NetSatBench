import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DocumentationExampleTests(unittest.TestCase):
    def test_concrete_json_fences_are_valid(self):
        failures = []
        for path in sorted((ROOT / "docs").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"```json\s*\n(.*?)```", text, re.DOTALL):
                body = match.group(1)
                if "<" in body or "..." in body:
                    continue
                try:
                    json.loads(body)
                except json.JSONDecodeError as exc:
                    line = text.count("\n", 0, match.start()) + 1
                    failures.append(f"{path.relative_to(ROOT)}:{line}: {exc}")

        self.assertEqual(failures, [], "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
