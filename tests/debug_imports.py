import sys
import os

print("Simulating bot.py import...")
try:
    # Add project root to sys.path
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    sys.path.insert(0, project_root)

    # Try importing modules one by one
    print("Importing app.config...")
    print("app.config imported.")

    print("Importing app.database...")
    print("app.database imported.")

    print("Importing telegram modules...")
    print("telegram modules imported.")

    print("Importing app.handlers...")
    print("app.handlers imported.")

    print("Importing bot.py...")
    print("bot.py imported successfully.")

except Exception as e:
    print(f"IMPORT ERROR: {e}")
    sys.exit(1)
except SystemExit as e:
    print(f"SYSTEM EXIT called during import: {e}")
    sys.exit(e.code)
