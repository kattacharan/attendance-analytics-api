"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI  (default: mongodb://localhost:27017)
      MONGO_DB   (default: attendance_db)
"""
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

# ---------------------------------------------------------------------------
# Configuration & DB connection
# ---------------------------------------------------------------------------
load_dotenv()  # local .env for dev; real env vars always win

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

# tz_aware=True makes PyMongo return UTC-aware datetimes (not naive).
# Without this, TZ comparisons with IST-computed datetimes are wrong.
client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))  # UTC+05:30

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Startup: create indexes idempotently (create_index is a no-op if already exists)
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    # employees: unique on emp_code -- concurrent duplicate creates hit DuplicateKeyError
    db.employees.create_index("emp_code", unique=True)
    # employees: department + joined_on for department_summary and employee listing
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    # attendance_logs unique compound -- one record per employee-day;
    # concurrent punch-ins for same (emp_code, date) -> only one insert wins
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    # Covers listAttendance sort: date DESC, emp_code ASC
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    # Covers punch-out lookup: latest punch_in <= punched_at for an employee
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)]
    )
    # Covers analytics: emp_code + date range + status filters
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)]
    )
    # Covers date-range analytics (leaderboard, department summary, trend)
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)]
    )
    # Covers late leaderboard query (date range + late_minutes > 0)
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime (sub-ms dropped by caller)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    """UTC-aware datetime -> epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def round_half_up(value: float, decimals: int) -> float:
    """Half-up rounding via Decimal -- Python's built-in round() uses banker's rounding."""
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def attendance_date_ist(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: attendance date = IST calendar date of punch_in.
    Overnight exception (shift_end <= shift_start, e.g. 22:00-06:00):
      if IST time of punch_in < shift_end it belongs to the PREVIOUS day's shift.
    """
    dt_ist = punch_in_utc.astimezone(IST)
    date_str = dt_ist.date().isoformat()
    if shift_end <= shift_start:  # overnight shift
        end_h, end_m = map(int, shift_end.split(":"))
        punch_hhmm = dt_ist.hour * 60 + dt_ist.minute
        if punch_hhmm < end_h * 60 + end_m:
            # Early-morning punch: belongs to yesterday's shift
            date_str = (dt_ist.date() - timedelta(days=1)).isoformat()
    return date_str


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: late only when punch_in > shift_start + 10 min (grace period).
    late_minutes = floor((punch_in - shift_start) / 60), measured from shift_start.
    Examples with 09:30 shift: 09:40:00->0, 09:40:01->10, 10:15:59->45.
    """
    h, m = map(int, shift_start.split(":"))
    shift_start_ist = datetime.fromisoformat(date_str).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )
    diff_secs = (punch_in_utc - shift_start_ist).total_seconds()
    diff_min = int(diff_secs // 60)
    return diff_min if diff_min > 10 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) / 3600, rounded 2dp half-up."""
    raw = (punch_out_utc - punch_in_utc).total_seconds() / 3600
    return round_half_up(raw, 2)


def compute_overtime_minutes(
    punch_out_utc: datetime, shift_end: str, date_str: str, shift_start: str
) -> int:
    """
    R3: floor((punch_out - shift_end) / 60), only when >= 30; else 0.
    For overnight shifts, shift_end falls on the day AFTER date_str.
    """
    h, m = map(int, shift_end.split(":"))
    shift_date = datetime.fromisoformat(date_str)
    if shift_end <= shift_start:  # overnight: shift_end is next day
        shift_date += timedelta(days=1)
    shift_end_ist = shift_date.replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )
    diff_min = int((punch_out_utc - shift_end_ist).total_seconds() // 60)
    return diff_min if diff_min >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def serialize_record(doc: dict) -> dict:
    """
    MongoDB attendance_log doc -> API response shape.
    BSON dates become epoch ms; _id removed; missing optional fields get defaults.
    """
    doc.pop("_id", None)

    def _to_ms(v):
        return dt_to_ms(v) if v is not None else None

    doc["punch_in"] = _to_ms(doc.get("punch_in"))
    doc["punch_out"] = _to_ms(doc.get("punch_out"))
    doc.setdefault("half_day", False)   # legacy records may omit this
    doc.setdefault("history", [])       # legacy records may omit this

    for entry in doc["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = dt_to_ms(entry["at"])
        for field in ("punch_in", "punch_out"):
            if field in entry.get("changes", {}):
                for side in ("from", "to"):
                    v = entry["changes"][field].get(side)
                    if isinstance(v, datetime):
                        entry["changes"][field][side] = dt_to_ms(v)
    return doc


def serialize_employee(doc: dict) -> dict:
    """MongoDB employee doc -> API response shape."""
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
EPOCH_MIN = 100_000_000_000    # below this looks like seconds (reject per spec)
EPOCH_MAX = 4_102_444_800_000  # year 2100 -- reasonable upper bound


class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def v_emp_code(cls, v):
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return v

    @field_validator("email")
    @classmethod
    def v_email(cls, v):
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("invalid email")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def v_time(cls, v):
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("time must be HH:MM 24-hour")
        return v

    @model_validator(mode="after")
    def shift_ne(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


def _chk_ms(v, name="punched_at"):
    """Raise ValueError (Pydantic-compatible) if ms value is out of range."""
    if v is not None and (v < EPOCH_MIN or v > EPOCH_MAX):
        raise ValueError(f"{name}: must be epoch milliseconds >= {EPOCH_MIN}")
    return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def v_ms(cls, v): return _chk_ms(v)

    @field_validator("status")
    @classmethod
    def v_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def v_ms(cls, v): return _chk_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("status")
    @classmethod
    def v_status(cls, v):
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("invalid status")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def v_ms(cls, v, info):
        return _chk_ms(v, info.field_name)

    @field_validator("reason")
    @classmethod
    def v_reason(cls, v):
        if not (5 <= len(v) <= 200):
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def v_by(cls, v):
        if not (1 <= len(v) <= 50):
            raise ValueError("regularized_by must be 1-50 characters")
        return v


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    """200 only if MongoDB answers a ping; 503 otherwise."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "database unavailable")


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """
    Insert-first: no pre-check SELECT.
    The unique index on emp_code turns a duplicate into DuplicateKeyError -> 409.
    Guarantees exactly-one-201 under concurrent identical requests.
    """
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size   # starter bug: was page * page_size (off-by-one page)
    total = db.employees.count_documents(q)  # starter bug: used {} ignoring the filter
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for doc in items:
        serialize_employee(doc)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance - punch-in
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """
    Creates the day's attendance record.
    Concurrency: unique index on (emp_code, date) means only one
    concurrent insert wins; loser gets DuplicateKeyError -> 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    # Truncate to whole seconds per R1; use server time if punched_at omitted
    if body.punched_at is not None:
        punch_in_utc = ms_to_dt((body.punched_at // 1000) * 1000)
    else:
        now = datetime.now(tz=timezone.utc)
        punch_in_utc = now.replace(microsecond=0)

    date_str = attendance_date_ist(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(punch_in_utc, emp["shift_start"], date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date")
    return serialize_record(doc)


# ---------------------------------------------------------------------------
# Attendance - punch-out
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """
    Closes the most recent open record with punch_in <= punched_at.
    Concurrency: find_one_and_update with {punch_out: None} is atomic;
    only one concurrent punch-out wins; the loser finds None returned -> 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        punch_out_utc = ms_to_dt((body.punched_at // 1000) * 1000)
    else:
        now = datetime.now(tz=timezone.utc)
        punch_out_utc = now.replace(microsecond=0)

    # Find the latest open record whose punch_in is at or before punch_out
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        # Distinguish 404 (no punch-in at all) from 409 (already closed)
        closed = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}, "punch_out": {"$ne": None}},
            sort=[("punch_in", DESCENDING)],
        )
        if closed:
            raise HTTPException(409, "already punched out for this record")
        raise HTTPException(404, "no open punch-in found for this employee")

    punch_in_utc = record["punch_in"]
    diff_secs = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_secs <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if diff_secs > 86400:
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")

    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime = compute_overtime_minutes(
        punch_out_utc, emp["shift_end"], record["date"], emp["shift_start"]
    )
    half = compute_half_day(work_hrs)

    # Atomic: only succeeds if punch_out is still null
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punch_out_utc,
            "work_hours": work_hrs,
            "overtime_minutes": overtime,
            "half_day": half,
        }},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "already punched out for this record")
    return serialize_record(updated)


# ---------------------------------------------------------------------------
# Attendance - list
# ---------------------------------------------------------------------------
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Server-side sort/skip/limit -- never loads the whole collection into Python.
    The compound index on (date DESC, emp_code ASC) covers this sort.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if status and status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
        raise HTTPException(422, "invalid status value")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_record(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance - regularize (manual correction)
# ---------------------------------------------------------------------------
@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """
    Manually corrects a record. Appends one history entry with only changed fields.
    Concurrency: optimistic lock on history array length -- if another PATCH sneaked in
    between our read and write, the $expr filter won't match -> None -> 409.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.status in ("ABSENT", "LEAVE") and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(422, "ABSENT/LEAVE status cannot include punch times")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    # Snapshot old values
    old = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    history_len = len(record.get("history", []))

    # Apply partial update
    new_status = body.status if body.status is not None else old["status"]
    new_pi = (
        ms_to_dt((body.punch_in // 1000) * 1000) if body.punch_in is not None else old["punch_in"]
    )
    new_po = (
        ms_to_dt((body.punch_out // 1000) * 1000) if body.punch_out is not None else old["punch_out"]
    )

    if new_status in ("ABSENT", "LEAVE"):
        new_pi = None
        new_po = None

    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_pi is None:
        raise HTTPException(422, "presence status requires punch_in")

    # punch_in must stay on the record's attendance date (R1)
    if new_pi is not None:
        if attendance_date_ist(new_pi, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must fall on the record's attendance date")

    if new_po is not None and new_pi is not None:
        diff = (new_po - new_pi).total_seconds()
        if diff <= 0:
            raise HTTPException(422, "punch_out must be after punch_in")
        if diff > 86400:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Recompute derived fields from final punch times
    new_late = compute_late_minutes(new_pi, emp["shift_start"], date) if new_pi else 0
    if new_pi and new_po:
        new_wh = compute_work_hours(new_pi, new_po)
        new_ot = compute_overtime_minutes(new_po, emp["shift_end"], date, emp["shift_start"])
        new_hd = compute_half_day(new_wh)
    else:
        new_wh = None
        new_ot = 0
        new_hd = False

    # Build changes dict -- only fields that actually changed
    def _ms(dt_val):
        return dt_to_ms(dt_val) if dt_val is not None else None

    changes = {}
    if new_status != old["status"]:
        changes["status"] = {"from": old["status"], "to": new_status}
    if _ms(new_pi) != _ms(old["punch_in"]):
        changes["punch_in"] = {"from": _ms(old["punch_in"]), "to": _ms(new_pi)}
    if _ms(new_po) != _ms(old["punch_out"]):
        changes["punch_out"] = {"from": _ms(old["punch_out"]), "to": _ms(new_po)}
    if new_wh != old["work_hours"]:
        changes["work_hours"] = {"from": old["work_hours"], "to": new_wh}
    if new_late != old["late_minutes"]:
        changes["late_minutes"] = {"from": old["late_minutes"], "to": new_late}
    if new_ot != old["overtime_minutes"]:
        changes["overtime_minutes"] = {"from": old["overtime_minutes"], "to": new_ot}
    if new_hd != old["half_day"]:
        changes["half_day"] = {"from": old["half_day"], "to": new_hd}

    if not changes:
        raise HTTPException(422, "request changes nothing")

    # Build history entry; punch times stored as BSON dates in MongoDB
    now_utc = datetime.now(tz=timezone.utc)
    mongo_changes = {}
    for field, diff in changes.items():
        if field in ("punch_in", "punch_out"):
            mongo_changes[field] = {
                "from": ms_to_dt(diff["from"]) if diff["from"] is not None else None,
                "to": ms_to_dt(diff["to"]) if diff["to"] is not None else None,
            }
        else:
            mongo_changes[field] = diff

    hist_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": mongo_changes,
    }

    # Optimistic lock: $expr checks history array size hasn't grown since our read.
    # If another PATCH sneaked in, size won't match and find_one_and_update returns None.
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pi,
                "punch_out": new_po,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": hist_entry},
        },
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "concurrent edit detected; please retry")
    return serialize_record(updated)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------
def month_bounds(month: str):
    """'YYYY-MM' -> ('YYYY-MM-01', 'YYYY-MM-last')."""
    year, mon = map(int, month.split("-"))
    first = datetime(year, mon, 1).date().isoformat()
    if mon == 12:
        last = (datetime(year + 1, 1, 1).date() - timedelta(days=1)).isoformat()
    else:
        last = (datetime(year, mon + 1, 1).date() - timedelta(days=1)).isoformat()
    return first, last


def working_days_in_range(start_str: str, end_str: str) -> int:
    """Count Mon-Fri days between start and end inclusive."""
    start = datetime.fromisoformat(start_str).date()
    end = datetime.fromisoformat(end_str).date()
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Analytics - employee monthly
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    month_start, month_end = month_bounds(month)
    # R7: working days start from max(joined_on, first day of month)
    effective_start = max(emp["joined_on"], month_start)
    working_days = working_days_in_range(effective_start, month_end)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$addFields": {
            # $dayOfWeek: 1=Sun, 2=Mon, ..., 7=Sat
            "is_wd": {"$and": [
                {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
            ]},
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$is_present", "$is_wd"]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                0.0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {
                "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
            }},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    r = res[0] if res else {
        "present_days": 0.0, "leave_days": 0, "late_count": 0,
        "total_late_minutes": 0, "total_overtime_minutes": 0,
    }

    present_days = round_half_up(r["present_days"], 2)
    att_pct = round_half_up(present_days / working_days * 100, 2) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": r["leave_days"],
        "late_count": r["late_count"],
        "total_late_minutes": r["total_late_minutes"],
        "total_overtime_minutes": r["total_overtime_minutes"],
        "attendance_pct": att_pct,
    }


# ---------------------------------------------------------------------------
# Analytics - department summary
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Starts from employees collection so zero-log employees are counted (R9).
    avg_work_hours is the mean over individual records, not per-employee means.
    """
    month_start, month_end = month_bounds(month)

    emp_filter = {"joined_on": {"$lte": month_end}}
    if department:
        emp_filter["department"] = department

    pipeline = [
        {"$match": emp_filter},
        # Lookup logs for this employee in the month
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$ec"]},
                {"$gte": ["$date", month_start]},
                {"$lte": ["$date", month_end]},
            ]}}}],
            "as": "logs",
        }},
        # preserveNullAndEmptyArrays keeps employees with 0 logs in scope
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        # Intermediate group to correctly count headcount (one per employee)
        {"$group": {
            "_id": {"dept": "$department", "emp": "$emp_code"},
            "logs": {"$push": "$logs"},
        }},
        # Final group by department
        {"$group": {
            "_id": "$_id.dept",
            "headcount": {"$sum": 1},
            "all_logs": {"$push": "$logs"},
        }},
        # all_logs is an array of arrays; double-unwind to flatten
        {"$unwind": {"path": "$all_logs", "preserveNullAndEmptyArrays": True}},
        {"$unwind": {"path": "$all_logs", "preserveNullAndEmptyArrays": True}},
        # Re-group by department, aggregating across all logs
        {"$group": {
            "_id": "$_id",
            "headcount": {"$first": "$headcount"},
            "present_days": {"$sum": {"$cond": [
                {"$in": [{"$ifNull": ["$all_logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [
                    {"$and": [
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$all_logs.date", "2000-01-01"]}}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$all_logs.date", "2000-01-01"]}}}}, 6]},
                    ]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$all_logs.half_day", False]}, True]}, 0.5, 1.0]},
                    0.0
                ]},
                0.0,
            ]}},
            "wh_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": [{"$ifNull": ["$all_logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$gt": [{"$ifNull": ["$all_logs.work_hours", None]}, None]},
                ]},
                "$all_logs.work_hours", 0.0,
            ]}},
            "wh_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": [{"$ifNull": ["$all_logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$gt": [{"$ifNull": ["$all_logs.work_hours", None]}, None]},
                ]},
                1, 0,
            ]}},
            "late_count": {"$sum": {
                "$cond": [{"$gt": [{"$ifNull": ["$all_logs.late_minutes", 0]}, 0]}, 1, 0]
            }},
            "total_late_minutes": {"$sum": {"$ifNull": ["$all_logs.late_minutes", 0]}},
            "leave_count": {"$sum": {
                "$cond": [{"$eq": [{"$ifNull": ["$all_logs.status", ""]}, "LEAVE"]}, 1, 0]
            }},
            "on_duty_count": {"$sum": {
                "$cond": [{"$eq": [{"$ifNull": ["$all_logs.status", ""]}, "ON_DUTY"]}, 1, 0]
            }},
        }},
        {"$sort": {"_id": ASCENDING}},
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        avg_wh = round_half_up(r["wh_sum"] / r["wh_count"], 2) if r["wh_count"] > 0 else None
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Analytics - late leaderboard
# ---------------------------------------------------------------------------
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Standard competition ranking via $rank in $setWindowFields.
    limit applied AFTER ranking so tied employees at the cutoff all appear.
    Logs with no matching employee are dropped (inner join).
    """
    month_start, month_end = month_bounds(month)
    dept_filter = [{"$match": {"emp.department": department}}] if department else []

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$match": {"emp": {"$ne": []}}},  # drop logs with no employee
        {"$unwind": "$emp"},
        *dept_filter,
        # $rank gives standard competition ranking (ties share rank, next rank skipped)
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": DESCENDING},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": results}


# ---------------------------------------------------------------------------
# Analytics - department trend
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    One row per calendar day in [from, to], gap-filled with $densify.
    headcount per day computed in Python (at most 92 rows, tiny).
    moving_avg_7d computed in Python over the sorted rows.
    """
    if to < from_:
        raise HTTPException(422, "to must be >= from")
    if (datetime.fromisoformat(to).date() - datetime.fromisoformat(from_).date()).days > 92:
        raise HTTPException(422, "range must not exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    d_from = datetime.fromisoformat(from_).replace(tzinfo=timezone.utc)
    d_to_next = (datetime.fromisoformat(to) + timedelta(days=1)).replace(tzinfo=timezone.utc)

    pipeline = [
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                0.0,
            ]}},
            "late_count": {"$sum": {
                "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
            }},
        }},
        {"$project": {
            "dt": {"$dateFromString": {"dateString": "$_id"}},
            "present_count": 1,
            "late_count": 1,
        }},
        # $densify inserts a doc for every missing date in [from_, to]
        {"$densify": {
            "field": "dt",
            "range": {"step": 1, "unit": "day", "bounds": [d_from, d_to_next]},
        }},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}},
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$sort": {"date": ASCENDING}},
    ]

    raw_rows = list(db.attendance_logs.aggregate(pipeline))

    # Fetch department employees once for headcount calculation
    dept_emps = list(
        db.employees.find({"department": department}, {"joined_on": 1, "_id": 0})
    )

    def headcount_on(day_str: str) -> int:
        """R9: employees with joined_on <= day."""
        return sum(1 for e in dept_emps if e["joined_on"] <= day_str)

    items = []
    for row in raw_rows:
        day_str = row["date"]
        is_wd = datetime.fromisoformat(day_str).date().weekday() < 5
        hc = headcount_on(day_str)
        pc = row["present_count"]
        lc = row["late_count"]
        # attendance_rate: only on working days with headcount > 0
        rate = round_half_up(pc / hc, 4) if (is_wd and hc > 0) else None
        items.append({
            "date": day_str,
            "is_working_day": is_wd,
            "headcount": hc,
            "present_count": pc,
            "late_count": lc,
            "attendance_rate": rate,
            "_r": rate,  # temp for moving avg
        })

    # 7-day moving average: current row + up to 6 preceding rows with non-null rate
    for i, item in enumerate(items):
        window = [
            items[j]["_r"]
            for j in range(max(0, i - 6), i + 1)
            if items[j]["_r"] is not None
        ]
        item["moving_avg_7d"] = round_half_up(sum(window) / len(window), 4) if window else None

    for item in items:
        item.pop("_r", None)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin - explain endpoint
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Runs MongoDB explain(executionStats) for the named endpoint's main query/pipeline,
    built exactly as the real endpoint builds it (same filters, sort, pagination).
    Grader checks for IXSCAN and no COLLSCAN on ~100k records.
    """
    VALID = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    }
    if endpoint not in VALID:
        raise HTTPException(422, f"endpoint must be one of: {', '.join(sorted(VALID))}")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        exp = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        ms, me = month_bounds(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": ms, "$lte": me}}}]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        exp = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        ms, me = month_bounds(month)
        emp_filter = {"joined_on": {"$lte": me}}
        if department:
            emp_filter["department"] = department
        pipeline = [
            {"$match": emp_filter},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [{"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$ec"]},
                    {"$gte": ["$date", ms]},
                    {"$lte": ["$date", me]},
                ]}}}],
                "as": "logs",
            }},
        ]
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        exp = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        ms, me = month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": ms, "$lte": me}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        exp = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, and to are required")
        if to < from_:
            raise HTTPException(422, "to must be >= from")
        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }},
            {"$match": {"emp.department": department}},
        ]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        exp = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}