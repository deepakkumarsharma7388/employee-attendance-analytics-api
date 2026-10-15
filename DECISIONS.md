# Engineering Decisions

## 1. Indexes

I use indexes to make common employee and attendance lookups faster. A unique index on the employee code prevents duplicate employee identifiers. Attendance lookups should be indexed by employee code and date so that employee-specific history and date-range queries do not require scanning every record. A unique compound index on employee code and attendance date also helps prevent duplicate daily attendance records. I would avoid adding indexes to every field because indexes consume storage and increase write costs.

## 2. Punch-in race condition

Two identical punch-in requests can arrive at almost the same time. If the code only checks whether a record exists and then inserts it, both requests may pass the check. A database-level unique constraint on employee code and date is needed to guarantee that only one daily record is created. The losing request should return a conflict response rather than create a duplicate.

## 3. Leaderboard ties

Employees with equal late-minute totals should receive the same rank or be included together at the cutoff, according to the API contract. The result should not arbitrarily exclude an employee merely because another employee has the same value. A stable secondary sort can make response ordering predictable.

## 4. Department headcount

Department headcount should come from eligible employees, not only from attendance records. Otherwise, employees who have not yet logged attendance disappear from the summary. Employees who joined on or before the reporting period's end should be considered, even when they have zero attendance logs.

## 5. Scaling

For 100 times more data, I would review query plans, add only necessary indexes, and use bounded pagination. I would also move expensive analytics to aggregation pipelines or precomputed summaries if profiling showed that live calculations were too slow.
