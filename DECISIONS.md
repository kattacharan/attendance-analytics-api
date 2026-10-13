# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique `emp_code` enforces uniqueness and concurrent 409s; `(department, joined_on)` and `(joined_on)` back department summary matches and filtered pagination.
   - `attendance_logs`: Unique `(emp_code, date)` ensures one record per day; `(date DESC, emp_code ASC)` covers attendance listing; `(emp_code, punch_in DESC)` powers punch-out lookups; `(emp_code, date, status)` backs monthly summaries; `(date, late_minutes)` and `(date, status)` cover analytics.
   - *Considered & rejected:* Separate single-field indexes on `date` and `emp_code` were rejected because compound indexes avoid index intersection overhead and satisfy sorting and filtering in a single IXSCAN.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `insert_one()`. Relying on the DB unique constraint rather than check-then-insert, MongoDB atomically commits the first insert and raises `DuplicateKeyError` on the second. The loser is caught and returns 409.

3. **Ties.**
   Both employees are returned. The pipeline computes standard competition ranking via `$setWindowFields` with `$rank` and filters `rank <= limit` afterward. Neither tied candidate is dropped at the cutoff; ties are ordered by `emp_code` ascending.

4. **Headcount.**
   The aggregation pipeline starts from `employees` (`joined_on <= month_end`) and runs `$lookup` into `attendance_logs` with `preserveNullAndEmptyArrays: True`. Employees with zero logs remain in the pipeline and are counted in `headcount`.

5. **One thing you would change for 100x data.**
   Materialize daily department rollups upon punch-out/regularization into a summary collection, avoiding on-the-fly aggregations across tens of millions of raw log documents.
