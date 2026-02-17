"""
Vast.ai Serverless PyWorker — starts backend services, then proxies requests.

In serverless mode, Vast's start_server.sh clones PYWORKER_REPO and runs this file.
Our entrypoint.sh is NOT used. So this script must:
  1. Start Xvfb (if not running)
  2. Start the FastAPI server (app_server.py) as a subprocess
  3. Wait for it to be healthy
  4. Run the Vast PyWorker to proxy autoscaler requests
"""
import os
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# 1. Start Xvfb if not running
# ---------------------------------------------------------------------------
os.environ.setdefault("DISPLAY", ":99")

try:
    r = subprocess.run(["pgrep", "-x", "Xvfb"], capture_output=True, timeout=5)
    if r.returncode != 0:
        print("[worker] Starting Xvfb...")
        subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "800x600x8",
             "-maxclients", "512", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
    else:
        print("[worker] Xvfb already running")
except Exception as e:
    print(f"[worker] Xvfb check failed: {e}")

# ---------------------------------------------------------------------------
# 2. Install mitmproxy CA if present
# ---------------------------------------------------------------------------
CA_CERT = "/root/.mitmproxy/mitmproxy-ca-cert.pem"
if os.path.exists(CA_CERT):
    try:
        subprocess.run(
            ["certutil", "-d", "sql:/root/.pki/nssdb", "-A", "-t", "C,,",
             "-n", "mitmproxy", "-i", CA_CERT],
            capture_output=True, timeout=10,
        )
        print("[worker] mitmproxy CA installed")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 3. Start FastAPI server as subprocess
# ---------------------------------------------------------------------------
# The app code lives in /root/service (deployed via rsync) or /workspace (Docker)
SERVICE_DIR = None
for candidate in ["/root/service", "/workspace"]:
    if os.path.exists(os.path.join(candidate, "app_server.py")):
        SERVICE_DIR = candidate
        break

if SERVICE_DIR is None:
    print("[worker] ERROR: Cannot find app_server.py in /root/service or /workspace")
    sys.exit(1)

print(f"[worker] Starting FastAPI from {SERVICE_DIR}...")
os.makedirs("/var/log/image-gen", exist_ok=True)

log_file = open("/var/log/image-gen/server.log", "w")

# Use system python3 (not venv python) — the FastAPI server needs system-installed
# packages (undetected-chromedriver, mitmproxy, etc.) that aren't in the PyWorker venv.
# start_server.sh creates a venv for this worker.py, but app_server.py must use system python.
SYSTEM_PYTHON = "/usr/bin/python3"
if not os.path.exists(SYSTEM_PYTHON):
    SYSTEM_PYTHON = "/usr/local/bin/python3"
if not os.path.exists(SYSTEM_PYTHON):
    SYSTEM_PYTHON = sys.executable  # fallback

server_proc = subprocess.Popen(
    [SYSTEM_PYTHON, "app_server.py"],
    cwd=SERVICE_DIR,
    stdout=log_file,
    stderr=subprocess.STDOUT,
    env={**os.environ, "DISPLAY": ":99"},
)

# ---------------------------------------------------------------------------
# 4. Wait for FastAPI to be healthy
# ---------------------------------------------------------------------------
print("[worker] Waiting for FastAPI health...")
import urllib.request

healthy = False
for i in range(60):
    try:
        req = urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=2)
        if req.status == 200:
            healthy = True
            break
    except Exception:
        pass
    time.sleep(1)

if not healthy:
    print("[worker] ERROR: FastAPI did not become healthy in 60s")
    # Print last 20 lines of log
    try:
        log_file.flush()
        with open("/var/log/image-gen/server.log") as f:
            lines = f.readlines()
            for line in lines[-20:]:
                print(f"  {line.rstrip()}")
    except Exception:
        pass
    sys.exit(1)

print("[worker] FastAPI is healthy!")

# ---------------------------------------------------------------------------
# 5. Run Vast PyWorker
# ---------------------------------------------------------------------------
from vastai import Worker, WorkerConfig, HandlerConfig, LogActionConfig

config = WorkerConfig(
    model_server_url="http://127.0.0.1",
    model_server_port=8080,
    model_log_file="/var/log/image-gen/server.log",
    handlers=[
        HandlerConfig(
            route="/generate",
            allow_parallel_requests=True,
            max_queue_time=180.0,
            workload_calculator=lambda payload: 100.0,
        ),
        HandlerConfig(
            route="/status",
            allow_parallel_requests=True,
            max_queue_time=5.0,
            workload_calculator=lambda payload: 1.0,
        ),
        HandlerConfig(
            route="/health",
            allow_parallel_requests=True,
            max_queue_time=5.0,
            workload_calculator=lambda payload: 1.0,
        ),
    ],
    log_action_config=LogActionConfig(
        on_load=["Uvicorn running on", "Application startup complete"],
        on_error=["ERROR", "Traceback"],
    ),
)

Worker(config).run()
