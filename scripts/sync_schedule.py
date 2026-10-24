"""Makes the cron-job.org jobs match `schedule` in config.yaml: one job per entry, titled
"rentbot scan <name>". Existing rentbot jobs are updated in place, missing ones are created as copies
of the first, and leftovers are deleted. Needs CRONJOB_API_KEY in .env."""
import os
import time
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "rentbot scan"

load_dotenv(ROOT / ".env")
api = httpx.Client(base_url="https://api.cron-job.org",
                   headers={"Authorization": f"Bearer {os.environ['CRONJOB_API_KEY']}"}, timeout=20)


def call(method: str, path: str, **kw) -> httpx.Response:
    """cron-job.org allows only about one request per second, fewer when creating jobs."""
    for wait in (2, 5, 15, 30):
        r = api.request(method, path, **kw)
        if r.status_code != 429:
            return r.raise_for_status()
        time.sleep(wait)
    return r.raise_for_status()


schedule = yaml.safe_load((ROOT / "config.yaml").read_text())["schedule"]

jobs = sorted((j for j in call("GET", "/jobs").json()["jobs"] if j["title"].startswith(PREFIX)),
              key=lambda j: j["jobId"])
if not jobs:
    raise SystemExit(f'no cron-job.org job titled "{PREFIX}..." to copy the URL and token from')
template = call("GET", f"/jobs/{jobs[0]['jobId']}").json()["jobDetails"]

for i, entry in enumerate(schedule):
    job = {"title": f"{PREFIX} {entry['name']}", "enabled": True,
           "schedule": {"timezone": "Europe/London", "hours": entry["hours"], "minutes": entry["minutes"],
                        "mdays": [-1], "months": [-1], "wdays": entry.get("wdays", [-1]), "expiresAt": 0}}
    if i < len(jobs):
        call("PATCH", f"/jobs/{jobs[i]['jobId']}", json={"job": job})
        print(f"updated {jobs[i]['jobId']}: {job['title']}")
    else:
        copy = {k: template[k] for k in ("url", "requestMethod", "requestTimeout", "saveResponses",
                                         "notification", "extendedData")}
        job_id = call("PUT", "/jobs", json={"job": copy | job}).json()["jobId"]
        print(f"created {job_id}: {job['title']}")

for j in jobs[len(schedule):]:
    call("DELETE", f"/jobs/{j['jobId']}")
    print(f"deleted {j['jobId']}: {j['title']}")
