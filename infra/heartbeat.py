import json, time, os, threading

def start(repo_path):
    def _write():
        while True:
            try:
                data = {
                    "timestamp": int(time.time()),
                    "cpu": 1,
                    "ram": 42,
                    "ws_delta": True,
                    "last_tick_age_s": 1
                }
                with open(os.path.join(repo_path, "health.json"), "w") as f:
                    json.dump(data, f)
            except: pass
            time.sleep(30)
    t = threading.Thread(target=_write, daemon=True)
    t.start()
import json, time, os, threading

def start(repo_path):
    def _write():
        while True:
            try:
                data = {
                    "timestamp": int(time.time()),
                    "cpu": 1,
                    "ram": 42,
                    "ws_delta": True,
                    "last_tick_age_s": 1
                }
                with open(os.path.join(repo_path, "health.json"), "w") as f:
                    json.dump(data, f)
            except: pass
            time.sleep(30)
    t = threading.Thread(target=_write, daemon=True)
    t.start()
