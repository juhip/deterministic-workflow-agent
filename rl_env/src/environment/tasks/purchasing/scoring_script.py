"""Grader entry point, run by `ExecutableJudge` as root with the environment's Python.

Usage: python -m environment.tasks.purchasing.scoring_script <submission> <task_id> <output>
"""

import json
import sys
from pathlib import Path

from environment.paths import ROOT_DATA_DIR
from environment.tasks.purchasing.grading import grade


def main() -> None:
    submission, task_id, output = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    data = ROOT_DATA_DIR / "purchasing"
    reference = json.loads((data / "references" / f"{task_id}.json").read_text())
    result = grade(data / "scenarios" / f"{task_id}.sqlite", reference, submission)
    result["metadata"]["task_id"] = task_id
    output.write_text(json.dumps(result))


if __name__ == "__main__":
    main()
