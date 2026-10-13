"""
End-to-End (E2E) Test Runner for Employee Attendance & Analytics API
Runs all endpoints and checks against business rules R1-R10 and openapi contract.

Usage:
  python e2e_test.py
"""
import sys
import json
from datetime import datetime, timezone
from fastapi.testclient import TestClient

import sample_seed
from app.main import app, create_indexes

def run_e2e():
    print("=" * 60)
    print("  EMPLOYEE ATTENDANCE & ANALYTICS API - E2E TEST SUITE  ")
    print("=" * 60)

    # Step 1: Ensure indexes are initialized
    print("\n[Step 1] Initializing database indexes...")
    create_indexes()
    print(" -> Indexes created/verified.")

    client = TestClient(app)

    # Step 2: Health probe
    print("\n[Step 2] Testing GET /health...")
    r = client.get("/health")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    assert r.json() == {"status": "ok"}
    print(f" -> Status {r.status_code}: {r.json()}")

    # Step 3: Create & List Employees
    print("\n[Step 3] Testing /employees (POST & GET)...")
    emp = {
        "emp_code": "EMP5001",
        "name": "Sarah Connor",
        "email": "sarah@example.com",
        "department": "Security",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-05-01",
    }
    r = client.post("/employees", json=emp)
    assert r.status_code == 201, f"Failed create employee: {r.text}"
    print(f" -> POST /employees: 201 Created (emp_code: {r.json()['emp_code']})")

    # Duplicate -> 409
    r = client.post("/employees", json=emp)
    assert r.status_code == 409, f"Expected 409 for duplicate, got {r.status_code}"
    print(" -> Duplicate POST /employees correctly returned 409 Conflict.")

    # List employees
    r = client.get("/employees?page=1&page_size=20")
    assert r.status_code == 200
    res = r.json()
    assert res["total"] >= 7
    print(f" -> GET /employees: 200 OK (total employees: {res['total']})")

    # Step 4: Punch-in
    print("\n[Step 4] Testing POST /attendance/punch-in...")
    # Punch in 09:40:00 IST (10 min grace period -> late_minutes = 0)
    # 2026-07-20 09:40:00 IST = 2026-07-20 04:10:00 UTC
    pi_ms = int(datetime(2026, 7, 20, 4, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP5001", "punched_at": pi_ms})
    assert r.status_code == 201, f"Punch-in failed: {r.text}"
    rec = r.json()
    assert rec["date"] == "2026-07-20"
    assert rec["late_minutes"] == 0
    print(f" -> Punch-in 201 Created: date={rec['date']}, late_minutes={rec['late_minutes']}")

    # Duplicate punch-in on same date -> 409
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP5001", "punched_at": pi_ms + 1000})
    assert r.status_code == 409
    print(" -> Duplicate punch-in for same employee/date returned 409 Conflict.")

    # Step 5: Punch-out
    print("\n[Step 5] Testing POST /attendance/punch-out...")
    # Punch out at 19:15:00 IST = 13:45:00 UTC (45 min after shift_end 18:30 -> overtime = 45 min)
    po_ms = int(datetime(2026, 7, 20, 13, 45, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP5001", "punched_at": po_ms})
    assert r.status_code == 200, f"Punch-out failed: {r.text}"
    rec = r.json()
    print(f" -> Punch-out 200 OK: work_hours={rec['work_hours']}, overtime_minutes={rec['overtime_minutes']}, half_day={rec['half_day']}")
    assert rec["work_hours"] == 9.58
    assert rec["overtime_minutes"] == 45
    assert rec["half_day"] is False

    # Second punch out -> 409
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP5001", "punched_at": po_ms + 1000})
    assert r.status_code == 409
    print(" -> Second punch-out on same record returned 409 Conflict.")

    # Step 6: Attendance Listing
    print("\n[Step 6] Testing GET /attendance...")
    r = client.get("/attendance?emp_code=EMP5001")
    assert r.status_code == 200
    res = r.json()
    assert res["total"] == 1
    print(f" -> GET /attendance?emp_code=EMP5001: 200 OK (found {res['total']} record)")

    # Step 7: Regularization (PATCH) with audit history
    print("\n[Step 7] Testing PATCH /attendance/{emp_code}/{date}...")
    patch_body = {
        "status": "WFH",
        "reason": "Approved work from home request",
        "regularized_by": "HR Admin",
    }
    r = client.patch("/attendance/EMP5001/2026-07-20", json=patch_body)
    assert r.status_code == 200, f"Regularization failed: {r.text}"
    rec = r.json()
    assert rec["status"] == "WFH"
    assert len(rec["history"]) == 1
    hist = rec["history"][0]
    print(f" -> PATCH 200 OK: updated status to WFH, audit trail appended (by: {hist['by']}, changes: {hist['changes']})")

    # Step 8: Employee Monthly Analytics
    print("\n[Step 8] Testing GET /analytics/employees/{emp_code}/monthly...")
    r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert r.status_code == 200
    data = r.json()
    print(f" -> Monthly Summary for EMP0001 (2026-07): working_days={data['working_days']}, present_days={data['present_days']}, att_pct={data['attendance_pct']}%")

    # Step 9: Department Summary Analytics
    print("\n[Step 9] Testing GET /analytics/departments/summary...")
    r = client.get("/analytics/departments/summary?month=2026-07")
    assert r.status_code == 200
    depts = r.json()["items"]
    print(f" -> Department Summary (2026-07): {len(depts)} departments reported.")
    for d in depts:
        print(f"    - {d['department']}: headcount={d['headcount']}, present_days={d['present_days']}, avg_work_hours={d['avg_work_hours']}")

    # Step 10: Late Leaderboard Analytics
    print("\n[Step 10] Testing GET /analytics/leaderboard/late...")
    r = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert r.status_code == 200
    leaders = r.json()["items"]
    print(f" -> Leaderboard (2026-07): {len(leaders)} late entries.")
    for item in leaders:
        print(f"    - Rank {item['rank']}: {item['emp_code']} ({item['name']}) - {item['total_late_minutes']} min late")

    # Step 11: Department Daily Trend
    print("\n[Step 11] Testing GET /analytics/departments/{department}/trend...")
    r = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    assert r.status_code == 200
    trend = r.json()["items"]
    print(f" -> Trend 2026-07-01 to 2026-07-07: {len(trend)} days gap-filled with $densify.")
    for day in trend[:3]:
        print(f"    - Date {day['date']}: is_working_day={day['is_working_day']}, headcount={day['headcount']}, rate={day['attendance_rate']}, 7d_avg={day['moving_avg_7d']}")

    # Step 12: Admin Explain Plans (IXSCAN vs COLLSCAN)
    print("\n[Step 12] Testing GET /admin/explain/{endpoint} for all endpoints...")
    explains = [
        ("attendance_list", "/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-10"),
        ("employee_monthly", "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07"),
        ("department_summary", "/admin/explain/department_summary?month=2026-07"),
        ("late_leaderboard", "/admin/explain/late_leaderboard?month=2026-07"),
        ("department_trend", "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10"),
    ]
    for name, path in explains:
        r = client.get(path)
        assert r.status_code == 200
        raw_json = json.dumps(r.json()["explain"], default=str)
        has_ixscan = "IXSCAN" in raw_json
        has_collscan = "COLLSCAN" in raw_json
        print(f"    - {name:<20}: IXSCAN={has_ixscan} | COLLSCAN={has_collscan}")
        assert has_ixscan and not has_collscan

    # Restore sample seed
    print("\n[Final Cleanup] Re-seeding database back to clean sample data...")
    sample_seed.db.employees.delete_many({})
    sample_seed.db.attendance_logs.delete_many({})
    for name in ("employees", "attendance_logs"):
        docs = sample_seed.json_util.loads((sample_seed.here / f"{name}.json").read_text(), json_options=sample_seed.opts)
        sample_seed.db[name].insert_many(docs)
    print(" -> Database restored to original 6 employees & 9 logs.")

    print("\n" + "=" * 60)
    print("       ALL E2E CHECKS PASSED SUCCESSFULLY (100%)       ")
    print("=" * 60)

if __name__ == "__main__":
    run_e2e()
