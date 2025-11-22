"""Compute host metrics and original system/documentation routes."""

import datetime
import socket
import subprocess
import time

from fastapi import Request
from fastapi.responses import RedirectResponse


def getMetricsJson():
    """Report the original host, CPU, RAM, disk and Docker metric fields."""
    import psutil

    now = int(time.time())
    timestamp = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S")
    memory, disk = psutil.virtual_memory(), psutil.disk_usage("/")
    load = psutil.getloadavg()
    cpu = psutil.cpu_percent()
    hostname = socket.gethostname()
    addresses = [address.address for interface in psutil.net_if_addrs().values() for address in interface
                 if address.family == socket.AF_INET and not address.address.startswith("127.")]
    ip = addresses[0] if addresses else "127.0.0.1"
    temperature = 0.0
    if hasattr(psutil, "sensors_temperatures"):
        try:
            sensors = psutil.sensors_temperatures()
            temperatures = [reading.current for readings in sensors.values() for reading in readings]
            temperature = temperatures[0] if temperatures else 0.0
        except (OSError, NotImplementedError):
            pass

    def docker_count(*arguments):
        try:
            result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=2, check=False)
            return str(len(set(result.stdout.splitlines()))) if result.returncode == 0 else "0"
        except (OSError, subprocess.TimeoutExpired):
            return "0"

    return {
        "@timestamp": timestamp, "timestamp": timestamp, "unixtime": now,
        "host_name": hostname, "host_ip": ip, "cpu_count": psutil.cpu_count(),
        "cpu_temp_celsius": temperature, "cpu_load_1": load[0], "cpu_load_5": load[1],
        "cpu_load_15": load[2], "cpu_usage_percent": cpu,
        "disk_total_gb": disk.total / 2**30, "disk_free_gb": disk.free / 2**30,
        "disk_used_gb": disk.used / 2**30, "disk_percent": int(disk.percent),
        "ram_total_gb": round(memory.total / 2**30, 3), "ram_free_gb": round(memory.free / 2**30, 3),
        "ram_available_gb": round(memory.available / 2**30, 3), "ram_used_gb": round(memory.used / 2**30, 3),
        "ram_percent": int(memory.percent), "docker_images": docker_count("images", "-q"),
        "docker_all_container": docker_count("ps", "-aq"), "docker_running_container": docker_count("ps", "-q"),
        "message": f"CPU load {load[0]}, {cpu}%, {temperature} Celsius",
        "agent_type": "telemetry", "agent_id": ip + "/" + hostname,
    }


def install_system_routes(app):
    """Keep Compute metrics and original URL aliases outside shared HTTP code."""
    prefix = "/api/v1/compute"

    @app.get("/api/system/ready.json", operation_id="compute__original_ready", deprecated=True, tags=["System"])
    @app.get(prefix + "/system/ready.json", operation_id="compute__system_ready", tags=["System"])
    def original_ready() -> dict:
        """Return the original readiness payload: health is ok."""
        return {"health": "ok"}

    @app.get("/api/system/status.json", operation_id="compute__original_metrics", deprecated=True, tags=["System"])
    @app.get(prefix + "/system/status.json", operation_id="compute__metrics", tags=["System"])
    def system_metrics() -> dict:
        """Read host/IP, CPU count/load/usage/temperature, disk/RAM sizes and usage, timestamps and Docker counts. Unavailable temperature and Docker counts are zero. Docker calls have a two-second timeout each."""
        return getMetricsJson()

    @app.get("/swagger.json", include_in_schema=False)
    def original_schema(request: Request):
        return {**app.openapi(), "servers": [{"url": request.scope.get("root_path") or "/"}]}

    @app.get("/api/docs/", include_in_schema=False)
    def original_documentation(request: Request):
        return RedirectResponse(request.scope.get("root_path", "") + prefix + "/docs")
