"""Validated workspace documents. All ownership and writes are server controlled."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")]
Status = Literal["saved", "applied", "screen", "technical", "final", "offer", "rejected", "withdrawn"]


def valid_day(value: str) -> str:
    if value:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("Use YYYY-MM-DD")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NextAction(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    date: str = Field(min_length=10, max_length=10)
    _date = field_validator("date")(valid_day)


class HistoryEntry(StrictModel):
    id: Identifier | None = None
    date: str = Field(min_length=10, max_length=10)
    status: Status | None = None
    text: str | None = Field(default=None, max_length=1000)
    completedTask: NextAction | None = None
    eventId: Identifier | None = None
    contactDate: str | None = None
    previousContact: str | None = None
    _date = field_validator("date")(valid_day)


class Note(StrictModel):
    date: str = Field(min_length=10, max_length=10)
    text: str = Field(min_length=1, max_length=6000)
    _date = field_validator("date")(valid_day)


class Application(StrictModel):
    id: Identifier
    company: str = Field(min_length=1, max_length=100)
    role: str = Field(min_length=1, max_length=150)
    status: Status
    applied: str = Field(default="", max_length=10)
    source: str = Field(default="Другое", max_length=100)
    salary: str = Field(default="", max_length=200)
    contact: str = Field(default="", max_length=500)
    priority: Literal["normal", "high"] = "normal"
    location: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=2000)
    cv: str = Field(default="", max_length=80)
    lastContact: str = Field(default="", max_length=10)
    history: list[HistoryEntry] = Field(default_factory=list, max_length=1000)
    notes: list[Note] = Field(default_factory=list, max_length=500)
    reason: str = Field(default="", max_length=6000)
    next: NextAction | None = None
    _days = field_validator("applied", "lastContact")(valid_day)

    @field_validator("company", "role")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Required field")
        return value.strip()

    @field_validator("url")
    @classmethod
    def safe_url(cls, value):
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
                raise ValueError("Use an http(s) URL")
        return value


class CalendarEvent(StrictModel):
    id: Identifier
    job: str = Field(default="", max_length=80)
    title: str = Field(min_length=1, max_length=150)
    type: Literal["interview", "ping", "apply"]
    date: str = Field(min_length=10, max_length=10)
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    duration: int = Field(ge=5, le=480)
    done: bool = False
    note: str = Field(default="", max_length=6000)
    completionId: Identifier | None = None
    _date = field_validator("date")(valid_day)


class Workspace(StrictModel):
    version: Literal[1] = 1
    jobs: list[Application] = Field(default_factory=list, max_length=2000)
    events: list[CalendarEvent] = Field(default_factory=list, max_length=5000)
    goal: int = Field(default=5, ge=1, le=100)

    @model_validator(mode="after")
    def references(self):
        job_ids = {j.id for j in self.jobs}
        if len(job_ids) != len(self.jobs) or len({e.id for e in self.events}) != len(self.events):
            raise ValueError("Duplicate record IDs")
        if any(e.job and e.job not in job_ids for e in self.events):
            raise ValueError("Event refers to an unknown application")
        return self


class WorkspaceUpdate(StrictModel):
    revision: int = Field(ge=0)
    state: Workspace


def empty_workspace():
    return Workspace().model_dump(mode="json", exclude_none=True)
