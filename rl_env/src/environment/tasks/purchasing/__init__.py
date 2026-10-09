"""Purchasing: plan purchase orders for a manufacturer from its policy PDFs.

One task per scenario in `task_configs.json` (written by `setup_data.py`). The
student gets the scenario database and the policy and memo PDFs, writes its
purchase orders into the database, and `scoring_script` grades the root-owned
copy against the reference agent's plan. See README.md.
"""

import json
import os
import shutil
import sys
from pathlib import Path

from karotte import Step, Task
from karotte.judges import ExecutableJudge
from pydantic import BaseModel

from environment import STUDENT_UID
from environment.paths import ROOT_DATA_DIR, STUDENT_DATA_DIR
from environment.submissions import collect_submission
from environment.system_prompts import get_system_prompt
from environment.tasks.purchasing.prompt import instructions

DATA = ROOT_DATA_DIR / "purchasing"
DATABASE = STUDENT_DATA_DIR / "scenario.sqlite"
DOCUMENTS = STUDENT_DATA_DIR / "documents"


class TaskConfig(BaseModel):
    task_id: str
    kind: str
    source_file: str
    current_date: str
    tests: str
    reference_orders: int


class PurchasingTask(Task):
    task_config: TaskConfig

    @property
    def system_prompt(self) -> str:
        return get_system_prompt(self.config.model, self.config.extra_config)

    def pre_hook(self):
        shutil.copyfile(DATA / "scenarios" / f"{self.id}.sqlite", DATABASE)
        if os.geteuid() == 0:
            os.chown(DATABASE, STUDENT_UID, STUDENT_UID)
        os.chmod(DATABASE, 0o644)
        return {
            "source_file": self.task_config.source_file,
            "current_date": self.task_config.current_date,
        }

    @property
    def steps(self):
        return [PlanPurchasesStep(self.id, config=self.config)]

    @property
    def tools(self):
        return ["bash", "view_lines_in_file", "replace_in_file"]


class PlanPurchasesStep(Step):
    saved_submissions: tuple[Path, ...] = ()
    submission_paths: tuple[Path, ...] = (DATABASE,)

    def __init__(self, task_id: str, config):
        super().__init__(config)
        self.task_id = task_id

    @property
    def instructions(self) -> str:
        return instructions(DATABASE, DOCUMENTS, self.config.use_hints)

    @property
    def judge(self):
        assert self.saved_submissions, "pre_scoring_hook has not run"
        return ExecutableJudge(
            [
                sys.executable,
                "-m",
                "environment.tasks.purchasing.scoring_script",
                str(self.saved_submissions[0]),
                self.task_id,
                "score_output.txt",
            ]
        )

    def pre_scoring_hook(self):
        self.saved_submissions = collect_submission(self.config, self.submission_paths)


def _register() -> None:
    configs = json.loads((Path(__file__).parent / "task_configs.json").read_text())
    for raw in configs:
        cfg = TaskConfig.model_validate(raw)
        name = "Task_" + cfg.task_id.replace("-", "_")
        globals()[name] = type(
            name, (PurchasingTask,), {"id": cfg.task_id, "task_config": cfg}
        )


_register()
