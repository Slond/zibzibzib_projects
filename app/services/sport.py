"""Bounded training documents; the browser cannot invent unvalidated records."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Annotated, Literal
from zoneinfo import ZoneInfo
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EXERCISES = {"press", "front", "rdl", "curl", "calf", "squat", "bridge", "bench", "row"}
TEMPLATES = {"lower": ["front", "press", "rdl", "curl", "calf"],
             "upper": ["bench", "row"], "home": ["squat", "bridge"]}
ExerciseID = Literal["press", "front", "rdl", "curl", "calf", "squat", "bridge", "bench", "row"]
TemplateID = Literal["lower", "upper", "home"]
ZONE = ZoneInfo("Asia/Almaty")


def today():
    return datetime.now(ZONE).date().isoformat()


def valid_day(value: str):
    if len(value) != 10 or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Use YYYY-MM-DD")
    return value


Day = Annotated[str, Field(min_length=10, max_length=10)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WorkoutSet(StrictModel):
    weight: str = Field(default="", max_length=12)
    reps: str = Field(default="", max_length=4)
    done: bool = False

    @field_validator("weight")
    @classmethod
    def weight_value(cls, value):
        if not value:
            return value
        try:
            number = Decimal(value)
            if not re.fullmatch(r"\d+(?:\.\d+)?", value) or not number.is_finite() or not 0 <= number <= 2000:
                raise ValueError("Invalid weight")
        except InvalidOperation:
            raise ValueError("Invalid weight") from None
        return value

    @field_validator("reps")
    @classmethod
    def repetition_value(cls, value):
        if value and (not value.isdecimal() or not 1 <= int(value) <= 500):
            raise ValueError("Invalid repetitions")
        return value

    @model_validator(mode="after")
    def confirmed(self):
        if self.done and (self.weight == "" or self.reps == ""):
            raise ValueError("A completed set requires actual weight and repetitions")
        return self


class TrainingSession(StrictModel):
    template: TemplateID
    originalTemplate: TemplateID
    status: Literal["planned", "started", "done", "partial", "skipped", "moved"] = "planned"
    sets: dict[ExerciseID, Annotated[list[WorkoutSet], Field(min_length=3, max_length=3)]] = Field(default_factory=dict)
    customIds: list[ExerciseID] | None = Field(default=None, min_length=1, max_length=9)
    note: str = Field(default="", max_length=3000)
    feel: Literal["good", "tired", "low"] = "good"
    revisions: list[Annotated[str, Field(max_length=1000)]] = Field(default_factory=list, max_length=500)
    reason: str | None = Field(default=None, max_length=500)
    duration: int | None = Field(default=None, ge=1, le=600)
    startedAt: int | None = Field(default=None, ge=0, le=4102444800000)
    manual: bool = False
    movedFrom: Day | None = None
    movedTo: Day | None = None

    @field_validator("movedFrom", "movedTo")
    @classmethod
    def optional_date(cls, value):
        return valid_day(value) if value is not None else value

    @model_validator(mode="after")
    def consistent_result(self):
        ids = self.customIds or TEMPLATES[self.template]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate exercises")
        if any(key not in ids and any(s.done or s.weight or s.reps for s in sets)
               for key, sets in self.sets.items()):
            raise ValueError("Results must belong to the session exercises")
        done = sum(s.done for key in ids for s in self.sets.get(key, []))
        if self.status == "done" and done != len(ids) * 3:
            raise ValueError("Not all sets are complete")
        if self.status == "partial" and not 0 < done < len(ids) * 3:
            raise ValueError("Partial sessions require some unfinished sets")
        if self.status in {"planned", "skipped", "moved"} and done:
            raise ValueError("This status cannot contain confirmed sets")
        if self.status in {"done", "partial"} and self.duration is None:
            raise ValueError("Record actual duration")
        if self.status == "moved" and not self.movedTo:
            raise ValueError("A moved session requires its destination")
        return self


class Preferences(StrictModel):
    goal: str = Field(default="Регулярность и сила", min_length=1, max_length=80)
    time: str = Field(default="19:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    days: list[Annotated[int, Field(ge=0, le=6)]] = Field(default_factory=lambda: [1, 3, 5], min_length=1, max_length=7)

    @field_validator("days")
    @classmethod
    def unique_days(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Duplicate days")
        return value


class ChangeEvent(StrictModel):
    label: str = Field(max_length=1000)
    day: Day
    at: str = Field(max_length=80)
    id: int = Field(ge=0, le=9007199254740991)
    _day = field_validator("day")(valid_day)


class Snapshot(StrictModel):
    version: Literal[1] = 1
    sessions: dict[Day, TrainingSession] = Field(default_factory=dict, max_length=3000)
    events: list[ChangeEvent] = Field(default_factory=list, max_length=2000)
    preferences: Preferences = Field(default_factory=Preferences)
    undo: None = None

    @model_validator(mode="after")
    def session_dates(self):
        for key, session in self.sessions.items():
            valid_day(key)
            if key > today() and session.status in {"started", "done", "partial", "skipped"}:
                raise ValueError("Cannot record a future workout result")
            if session.status == "moved" and session.movedTo == key:
                raise ValueError("Cannot move to the same date")
        return self


class UndoRecord(StrictModel):
    snapshot: Snapshot
    label: str = Field(max_length=1000)


class SportState(Snapshot):
    undo: UndoRecord | None = None


class SportUpdate(StrictModel):
    revision: int = Field(ge=0, le=9007199254740991)
    state: SportState


def empty_workspace():
    return SportState().model_dump(exclude_none=True)
