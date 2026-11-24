import pathlib, sys, tempfile, csv
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import audit_apify_tasks as A

class Fake:
    def call(self, m, p, body=None):
        if p.startswith("/actor-tasks?"): return {"data": {"items": [{"id": "t1", "name": "fram-custom-goldman-test"}, {"id": "t2", "name": "task-2"}]}}
        if p.startswith("/schedules"): return {"data": {"items": [{"name": "fram-daily-x", "isEnabled": True, "actions": [{"actorTaskId": "t2"}]}]}}
        if p.endswith("/input"): return {"organizationSearch": ["Goldman Sachs"], "timeRange": "6m"} if "t1" in p else {"organizationSearch": ["CalSTRS"], "timeRange": "6m"}
        if "/runs" in p: return {"data": {"items": [{"status": "SUCCEEDED", "finishedAt": "2026-11-20T15:55:16Z"}]}}
rows = {r["org_name"]: r for r in A.audit(Fake())}
g = rows["Goldman Sachs Alternatives"]
assert g["tasks"] == "fram-custom-goldman-test" and g["state"] == "task, not scheduled" and g["last_run"] == "2026-11-20", g
assert any(r["state"] == "scheduled" for r in rows.values()), "CalSTRS-like firm in a scheduled task"
assert any(r["state"] == "no task" for r in rows.values())
p = pathlib.Path(tempfile.mkdtemp()) / "m.csv"; A.write(list(rows.values()), p)
assert next(csv.DictReader(open(p)))["org_name"]
rep = "\n".join(A.checks(list(rows.values()), A.audit.sched_info))
assert "fram-daily-x" in rep and "6m" in rep, rep       # the scheduled task looks back 6 months: flagged
print("test_audit OK")
