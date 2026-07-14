"""Runtime config from env. Internal-only tool — defaults are dev-friendly."""
import os

DB_PATH = os.environ.get("AMR_DB_PATH", "amr.db")

# JB2 read-only mirror (CR-001). Blank base_url => no live sync; app uses demo seed.
JB2_BASE_URL = os.environ.get("AMR_JB2_BASE_URL", "").rstrip("/")
JB2_TOKEN = os.environ.get("AMR_JB2_TOKEN", "")
# Every JB2 read must be filtered (INT-5). This is the default order filter applied
# to the jobs poll; override per deployment.
JB2_JOBS_FILTER = os.environ.get("AMR_JB2_JOBS_FILTER", "status[ne]=Closed")

JB2_ENABLED = bool(JB2_BASE_URL and JB2_TOKEN)
