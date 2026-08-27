import time
import subprocess
import json
import statistics
import os


def run_bench():
    repeats = 5
    times = []

    # Warmup
    subprocess.run(["python", "-c", "import bot"], capture_output=True)

    for _ in range(repeats):
        t0 = time.perf_counter()
        subprocess.run(["python", "-c", "import bot"], capture_output=True)
        times.append(time.perf_counter() - t0)

    times.sort()

    report = {
        "target": "bot.py import startup",
        "repeats": repeats,
        "times": times,
        "p95": times[int(len(times) * 0.95)] if len(times) >= 2 else times[-1],
        "p99": times[-1],
        "mean": statistics.mean(times),
    }

    os.makedirs("artifacts/perf", exist_ok=True)
    with open("artifacts/perf/baseline_import.json", "w") as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run_bench()
