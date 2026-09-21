"""Android/Termux power telemetry and conservative mesh admission policy."""
import json
import os
import subprocess
import threading
import time

_cache = (0, {})
_lock = threading.Lock()


def is_android():
    return os.environ.get("NODE_DEVICE_TYPE", "").lower() == "android" or bool(os.environ.get("ANDROID_ROOT")) or "com.termux" in os.environ.get("PREFIX", "")


def battery_status():
    global _cache
    with _lock:
        if time.monotonic() - _cache[0] < 10:
            return dict(_cache[1])
        try:
            result = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=3, check=True)
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                raise ValueError("invalid battery telemetry")
        except (OSError, ValueError, subprocess.SubprocessError):
            data = {}
        _cache = (time.monotonic(), data)
        return dict(data)


def mobile_status():
    if not is_android():
        return {"is_android": False, "available": True, "reasons": []}
    data, reasons = battery_status(), []
    percentage, temperature = data.get("percentage"), data.get("temperature")
    plugged = data.get("plugged") in {"PLUGGED_AC", "PLUGGED_USB", "PLUGGED_WIRELESS", "PLUGGED_DOCK"}
    if not isinstance(percentage, (float, int)) or not 0 <= percentage <= 100:
        reasons.append("battery percentage unavailable")
    elif percentage < float(os.environ.get("NODE_MIN_BATTERY", "25")):
        reasons.append("battery below minimum")
    if os.environ.get("NODE_REQUIRE_CHARGING", "1") != "0" and not plugged:
        reasons.append("phone must be plugged in")
    if not isinstance(temperature, (float, int)) or not -20 <= temperature <= 100:
        reasons.append("battery temperature unavailable")
    elif temperature >= float(os.environ.get("NODE_MAX_BATTERY_TEMP_C", "40")):
        reasons.append("battery too warm")
    return {"is_android": True, "available": not reasons, "reasons": reasons,
            "battery_percentage": percentage, "battery_temperature_c": temperature, "plugged_in": plugged}
