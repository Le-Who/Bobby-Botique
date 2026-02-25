import urllib.request
import json
import re

with open("d:/gemaibotv2/gemaibotv2/requirements.txt", "r") as f:
    lines = f.readlines()

for line in lines:
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    pkg = re.split(r"[=<>~]", line)[0].strip()
    if not pkg:
        continue
    try:
        url = f"https://pypi.org/pypi/{pkg}/json"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read())
            latest_version = data["info"]["version"]
            spec = line[len(pkg) :].strip()
            print(f"{pkg}: Current: {spec} | Latest: {latest_version}")
    except Exception as e:
        print(f"Error for {pkg}: {e}")
