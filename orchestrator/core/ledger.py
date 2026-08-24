"""Task ledger: canonical record of every orchestration task and its state."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from orchestrator.core.storage import read_json, write_json_atomic

TASK_ID_RE = re.compile(r"^TASK-(\d{3,})$")


class TaskStatus(StrEnum):
    PLANNED = "PLANNED"
    BLOCKED = "BLOCKED"
    DISPATCHED = "DISPATCHED"
    WORKING = "WORKING"
    PR_OPEN = "PR_OPEN"
    REVIEWING = "REVIEWING"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    READY_TO_MERGE = "READY_TO_MERGE"
    MERGED = "MERGED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Task:
    id: str
    title: str
    status: TaskStatus = TaskStatus.PLANNED
    objective: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    agent: str | None = None
    branch: str | None = None
    pr: str | None = None
    last_result: str | None = None
    handoff: dict[str, str] | None = None
    role: str | None = None
    model: str | None = None
    effort: str | None = None
    risks: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data


def _coerce_status(value: object) -> TaskStatus:
    try:
        return TaskStatus(value)
    except ValueError:
        raise ValueError(f"unknown task status: {value!r}") from None


class Ledger:
    def __init__(self, path: Path, tasks: dict[str, Task] | None = None) -> None:
        self.path = path
        self.tasks: dict[str, Task] = tasks or {}

    @classmethod
    def load(cls, path: Path) -> Ledger:
        if not path.exists():
            return cls(path)
        data = read_json(path)
        known_fields = {f.name for f in fields(Task)}
        tasks: dict[str, Task] = {}
        for raw in data.get("tasks", []):
            fields_dict = dict(raw)
            fields_dict["status"] = _coerce_status(fields_dict.get("status"))
            task = Task(**{k: v for k, v in fields_dict.items() if k in known_fields})
            tasks[task.id] = task
        return cls(path, tasks)

    def save(self) -> None:
        ordered = sorted(self.tasks.values(), key=lambda t: t.id)
        write_json_atomic(self.path, {"tasks": [t.to_dict() for t in ordered]})

    def next_task_id(self) -> str:
        nums = [int(m.group(1)) for tid in self.tasks if (m := TASK_ID_RE.match(tid)) is not None]
        return f"TASK-{max(nums, default=0) + 1:03d}"

    def create_task(
        self,
        title: str,
        *,
        objective: str = "",
        acceptance_criteria: list[str] | None = None,
        dependencies: list[str] | None = None,
        agent: str | None = None,
        branch: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        risks: list[str] | None = None,
    ) -> Task:
        deps = dependencies or []
        unknown = [d for d in deps if d not in self.tasks]
        if unknown:
            raise ValueError(f"unknown dependency task(s): {', '.join(unknown)}")
        task = Task(
            id=self.next_task_id(),
            title=title,
            objective=objective,
            acceptance_criteria=acceptance_criteria or [],
            dependencies=list(deps),
            agent=agent,
            branch=branch,
            model=model,
            effort=effort,
            risks=risks or [],
        )
        self.tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task:
        try:
            return self.tasks[task_id]
        except KeyError:
            raise KeyError(f"no such task: {task_id}") from None

    def update_status(self, task_id: str, status: TaskStatus | str) -> Task:
        task = self.get(task_id)
        new_status = status if isinstance(status, TaskStatus) else _coerce_status(status)
        task.status = new_status
        task.updated_at = _now()
        return task

    def filter(self, status: TaskStatus | str | None = None) -> list[Task]:
        wanted = (
            None
            if status is None
            else (status if isinstance(status, TaskStatus) else _coerce_status(status))
        )
        ordered = sorted(self.tasks.values(), key=lambda t: t.id)
        if wanted is None:
            return ordered
        return [t for t in ordered if t.status == wanted]
