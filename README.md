# Employee Attendance & Analytics API

A FastAPI and MongoDB service for tracking employee attendance, processing punch-in/punch-out with business rules, maintaining regularization audit trails, and running high-performance analytics pipelines.

Built for the HROne Engineering Technical Assignment (Contract v2.0).

---

## Tech Stack
- **Framework**: Python 3.12, FastAPI, Pydantic v2
- **Database**: MongoDB (via PyMongo with timezone-aware datetimes)
- **Server**: Uvicorn

---

## Features & Implementation Overview
- **Strict Business Rules (R1–R10)**:
  - IST calendar dates, truncation to whole seconds, and handling for overnight shifts (R1).
  - 10-minute grace period with late minutes calculated from shift start (R2).
  - Overtime qualification threshold ($\ge 30$ mins after shift end) (R3).
  - Half-up rounding across work hours, attendance percentages, and rates (R4, R8).
  - Half-day flagging ($< 4.50$ hours) contributing 0.5 to presence count (R5, R6).
- **Concurrency & Idempotency**:
  - Unique index on `(emp_code, date)` to eliminate double punch-in races.
  - Atomic `find_one_and_update` on open shifts to prevent duplicate punch-outs.
  - Optimistic locking on history array length for concurrent regularizations (`PATCH`).
- **MongoDB Aggregation Pipelines**:
  - **Department Summary**: Starts from `employees` to ensure zero-log employees are counted in headcount (R9).
  - **Late Leaderboard**: Olympic standard competition ranking (`$rank`) supporting ties at the cutoff limit.
  - **Department Trend**: Calendar day gap-filling via MongoDB `$densify` with 7-day moving averages.
- **Query Optimization (`/admin/explain`)**:
  - Verified index scans (`IXSCAN`) and zero collection scans (`COLLSCAN`) across all list and analytics endpoints.

---

## Local Setup & Run

### 1. Requirements & Dependencies
```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Environment Configuration
Create a `.env` file (or set environment variables):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 3. Seed Sample Data
```bash
python sample_seed.py
```

### 4. Start the Application
```bash
uvicorn app.main:app --port 8000 --reload
```

Interactive Swagger documentation is available at **[http://localhost:8000/docs](http://localhost:8000/docs)**.

---

## Running Automated E2E Tests
```bash
python e2e_test.py
```

---

## Repository Structure
- `app/main.py`: Complete production FastAPI application, schemas, and MongoDB aggregation pipelines.
- `REVIEW.md`: Detailed defect analysis of the starter kit code (17 defects diagnosed and resolved).
- `DECISIONS.md`: Design rationale for indexing, race conditions, ties, headcount, and scaling.
- `requirements.txt`: Minimal dependencies for runtime execution.
- `sample_seed.py`: Seeding utility for MongoDB.
- `e2e_test.py`: Full end-to-end verification test suite.
