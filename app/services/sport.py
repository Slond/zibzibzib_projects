"""Bounded training documents; the browser cannot invent unvalidated records."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.sport_catalog import SHARED_EXERCISE_IDS

EXERCISES = {"press", "front", "rdl", "curl", "calf", "squat", "bridge", "bench", "row"}
TEMPLATES = {"lower": ["front", "press", "rdl", "curl", "calf"],
             "upper": ["bench", "row"], "home": ["squat", "bridge"]}
TemplateID = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")]
ExerciseID = TemplateID
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


class ExercisePlan(StrictModel):
    id: ExerciseID
    sets: int = Field(default=3, ge=1, le=12)
    reps: str = Field(default="10–12", pattern=r"^\d{1,3}(?:[–-]\d{1,3})?$")
    rest: int = Field(default=90, ge=0, le=600)
    note: str = Field(default="", max_length=500)

    @field_validator("reps")
    @classmethod
    def repetition_range(cls, value):
        parts = [int(x) for x in value.replace("-", "–").split("–")]
        if not 1 <= parts[0] <= parts[-1] <= 500:
            raise ValueError("Invalid repetition range")
        return value


class ExerciseDefinition(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    en: str = Field(default="", max_length=120)
    muscle: str = Field(min_length=1, max_length=80)
    equipment: str = Field(min_length=1, max_length=80)
    instructions: str = Field(default="", max_length=4000)
    sourceUrl: str = Field(default="", max_length=2000)
    videoUrl: str = Field(default="", max_length=2000)
    imageUrl: str = Field(default="", max_length=2000)
    attribution: str = Field(default="", max_length=1000)
    licenseUrl: str = Field(default="", max_length=2000)
    bodyweight: bool = False

    @field_validator("name", "muscle", "equipment")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Cannot be blank")
        return value

    @field_validator("sourceUrl", "videoUrl", "imageUrl", "licenseUrl")
    @classmethod
    def web_link(cls, value):
        value = value.strip()
        if value:
            parts = urlsplit(value)
            if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password or any(ord(c) < 33 for c in value):
                raise ValueError("Use a public HTTP or HTTPS link")
        return value


class WorkoutPlan(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    place: str = Field(default="В зале", min_length=1, max_length=80)
    minutes: int = Field(default=45, ge=1, le=600)
    desc: str = Field(default="", max_length=2000)
    exercises: list[ExercisePlan] = Field(min_length=1, max_length=50)

    @field_validator("title", "place")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Cannot be blank")
        return value

    @model_validator(mode="after")
    def unique_exercises(self):
        ids = [e.id for e in self.exercises]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate exercises")
        return self


class TrainingSession(StrictModel):
    template: TemplateID
    originalTemplate: TemplateID
    status: Literal["planned", "started", "done", "partial", "skipped", "moved"] = "planned"
    sets: dict[ExerciseID, Annotated[list[WorkoutSet], Field(min_length=1, max_length=12)]] = Field(default_factory=dict)
    plan: WorkoutPlan | None = None
    originalPlan: WorkoutPlan | None = None
    customIds: list[ExerciseID] | None = Field(default=None, min_length=1, max_length=50)
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
        if self.plan:
            ids = [e.id for e in self.plan.exercises]
            prescribed = {e.id: e.sets for e in self.plan.exercises}
            if self.customIds is not None and self.customIds != ids:
                raise ValueError("Exercise order must match the saved plan")
        else:
            if self.template not in TEMPLATES:
                raise ValueError("Custom sessions require a saved plan")
            ids = self.customIds or TEMPLATES[self.template]
            prescribed = dict.fromkeys(ids, 3)
        if any(len(sets) != prescribed[key] for key, sets in self.sets.items() if key in ids):
            raise ValueError("Set count must match the saved plan")
        expected = sum(prescribed.values())
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate exercises")
        if any(key not in ids and any(s.done or s.weight or s.reps for s in sets)
               for key, sets in self.sets.items()):
            raise ValueError("Results must belong to the session exercises")
        done = sum(s.done for key in ids for s in self.sets.get(key, []))
        if self.status == "done" and done != expected:
            raise ValueError("Not all sets are complete")
        if self.status == "partial" and not 0 < done < expected:
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
    days: list[Annotated[int, Field(ge=0, le=6)]] = Field(default_factory=lambda: [1, 3, 5], min_length=0, max_length=7)

    week: dict[Annotated[str, Field(pattern=r"^[0-6]$")], TemplateID] | None = None
    scheduleStart: Day | None = None
    scheduleEnd: Day | None = None

    @field_validator("scheduleStart", "scheduleEnd")
    @classmethod
    def schedule_date(cls, value):
        return valid_day(value) if value is not None else value

    @model_validator(mode="after")
    def schedule_range(self):
        if self.week is not None and set(self.days) != {int(key) for key in self.week}:
            raise ValueError("Scheduled days must match weekly workouts")
        if self.scheduleStart and self.scheduleEnd:
            length = (date.fromisoformat(self.scheduleEnd) - date.fromisoformat(self.scheduleStart)).days
            if not 0 <= length < 366:
                raise ValueError("Schedule range must be within one year")
        return self

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
    exercises: dict[ExerciseID, ExerciseDefinition] = Field(default_factory=dict, max_length=500)
    deletedTemplates: list[TemplateID] = Field(default_factory=list, max_length=1000)
    templates: dict[TemplateID, WorkoutPlan] = Field(default_factory=dict, max_length=100)
    sessions: dict[Day, TrainingSession] = Field(default_factory=dict, max_length=3000)
    events: list[ChangeEvent] = Field(default_factory=list, max_length=2000)
    preferences: Preferences = Field(default_factory=Preferences)
    undo: None = None

    @model_validator(mode="after")
    def session_dates(self):
        archived = set(self.deletedTemplates)
        if archived & set(self.templates) or len(archived) != len(self.deletedTemplates):
            raise ValueError("Deleted workouts cannot remain in the library")
        active = (set(TEMPLATES) - archived) | set(self.templates)
        known = active | archived
        if set(self.exercises) & (EXERCISES | SHARED_EXERCISE_IDS):
            raise ValueError("Built-in exercises cannot be overwritten")
        exercise_ids = EXERCISES | SHARED_EXERCISE_IDS | set(self.exercises)

        def check_plan(plan):
            if plan and any(e.id not in exercise_ids for e in plan.exercises):
                raise ValueError("Plan references an unknown exercise")

        for plan in self.templates.values():
            check_plan(plan)
        if self.preferences.week and any(key not in active for key in self.preferences.week.values()):
            raise ValueError("Schedule references an unknown workout")
        for key, session in self.sessions.items():
            if session.template not in known or session.originalTemplate not in known:
                raise ValueError("Session references an unknown workout")
            if session.template in archived and not session.plan or session.originalTemplate in archived and not session.originalPlan:
                raise ValueError("Deleted workouts require historical snapshots")
            check_plan(session.plan)
            check_plan(session.originalPlan)
            if (set(session.sets) | set(session.customIds or [])) - exercise_ids:
                raise ValueError("Session references an unknown exercise")
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
    return upgrade_workspace(SportState().model_dump(exclude_none=True))


# The old program was fixed in JavaScript. These defaults preserve its exact
# prescription when an existing dated session first receives a frozen plan.
EXERCISE_DEFAULTS = {
    "front": (3, "8–10", 120), "press": (3, "10–12", 90),
    "rdl": (3, "10–12", 90), "curl": (3, "12–15", 60),
    "calf": (3, "15–20", 60), "squat": (3, "12–15", 60),
    "bridge": (3, "12–15", 60), "bench": (3, "8–12", 90),
    "row": (3, "10–12", 90),
}
DEFAULT_DESCRIPTIONS = {
    "lower": ("Ноги и ягодицы", "В зале", 55, "Тренировка нижней части тела."),
    "upper": ("Верх тела", "В зале", 35, "Тренировка груди и спины."),
    "home": ("Ноги · дома", "Дома", 20, "Тренировка без инвентаря."),
}


def default_exercise(exercise_id):
    sets, reps, rest = EXERCISE_DEFAULTS.get(exercise_id, (3, "10–12", 90))
    return {"id": exercise_id, "sets": sets, "reps": reps, "rest": rest, "note": ""}


def default_templates():
    return {key: {"title": title, "place": place, "minutes": minutes, "desc": description,
                  "exercises": [default_exercise(item) for item in TEMPLATES[key]]}
            for key, (title, place, minutes, description) in DEFAULT_DESCRIPTIONS.items()}


def upgrade_workspace(document):
    """Enrich a legacy document without overwriting any user value or DB revision.

    Plans belong to dated sessions. Editing a reusable workout afterwards cannot
    alter previous prescriptions or results. Undo keeps its own historical copy.
    """
    result = deepcopy(document)
    result.setdefault("exercises", {})
    result.setdefault("deletedTemplates", [])
    result["templates"] = {key: value for key, value in
                           {**default_templates(), **result.get("templates", {})}.items()
                           if key not in result["deletedTemplates"]}
    preferences = result["preferences"]
    if preferences.get("week") is None:
        preferences["week"] = {str(day): "upper" if day == 1 else "lower"
                               for day in preferences["days"]}
    for session in result["sessions"].values():
        if not session.get("plan"):
            plan = deepcopy(result["templates"][session["template"]])
            if session.get("customIds"):
                plan["exercises"] = [default_exercise(key) for key in session["customIds"]]
            session["plan"] = plan
        if not session.get("originalPlan"):
            original = result["templates"].get(session["originalTemplate"], session["plan"])
            session["originalPlan"] = deepcopy(original)
    if result.get("undo"):
        result["undo"]["snapshot"] = upgrade_workspace(result["undo"]["snapshot"])
    return result


def refresh_future_workouts(previous, document, current_day=None):
    """Apply edited prescriptions to upcoming, untouched sessions, including manual bookings.

    Calendar-day boundaries follow the account's existing Almaty timezone. Historical
    originalPlan snapshots, notes and dates stay intact; entered results are never reset.
    """
    result = deepcopy(document)
    changed = {key for key, plan in result["templates"].items()
               if key in previous["templates"] and plan != previous["templates"][key]}
    for day, session in result["sessions"].items():
        if day <= (current_day or today()) or session["template"] not in changed:
            continue
        if session["status"] != "planned" or session.get("startedAt") is not None:
            continue
        if any(s.get("done") or s.get("weight") or s.get("reps")
               for sets in session["sets"].values() for s in sets):
            continue
        session["plan"] = deepcopy(result["templates"][session["template"]])
        session["sets"] = {}
        session.pop("customIds", None)
    return result
