"""Makes the cron-job.org jobs match `schedule` in config.yaml: one job per entry, titled
"rentbot scan <name>". Existing rentbot jobs are updated in place, missing ones are created as copies
of the first, and leftovers are deleted. Needs CRONJOB_API_KEY in .env."""
import os
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "rentbot scan"

load_dotenv(ROOT / ".env")
api = httpx.Client(base_url="https://api.cron-job.org",
                   headers={"Authorization": f"Bearer {os.environ['CRONJOB_API_KEY']}"}, timeout=20)
schedule = yaml.safe_load((ROOT / "config.yaml").read_text())["schedule"]

jobs = sorted((j for j in api.get("/jobs").raise_for_status().json()["jobs"] if j["title"].startswith(PREFIX)),
              key=lambda j: j["jobId"])
if not jobs:
    raise SystemExit(f'no cron-job.org job titled "{PREFIX}..." to copy the URL and token from')
template = api.get(f"/jobs/{jobs[0]['jobId']}").raise_for_status().json()["jobDetails"]

for i, entry in enumerate(schedule):
    job = {"title": f"{PREFIX} {entry['name']}", "enabled": True,
           "schedule": {"timezone": "Europe/London", "hours": entry["hours"], "minutes": entry["minutes"],
                        "mdays": [-1], "months": [-1], "wdays": [-1], "expiresAt": 0}}
    if i < len(jobs):
        api.patch(f"/jobs/{jobs[i]['jobId']}", json={"job": job}).raise_for_status()
        print(f"updated {jobs[i]['jobId']}: {job['title']}")
    else:
        copy = {k: template[k] for k in ("url", "requestMethod", "requestTimeout", "saveResponses",
                                         "notification", "extendedData")}
        job_id = api.put("/jobs", json={"job": copy | job}).raise_for_status().json()["jobId"]
        print(f"created {job_id}: {job['title']}")

for j in jobs[len(schedule):]:
    api.delete(f"/jobs/{j['jobId']}").raise_for_status()
    print(f"deleted {j['jobId']}: {j['title']}")
