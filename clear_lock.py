#!/usr/bin/env python3
"""
Utility script to clear bot locks.
Useful when deploying to Render to ensure no stale locks remain.
"""

import os
import sys

def clear_lock():
    """Clears any existing bot lock files"""
    lock_file = "/tmp/gemaibot.lock"
    
    if os.path.exists(lock_file):
        try:
            os.unlink(lock_file)
            print(f"✅ Lock file {lock_file} removed successfully")
            return True
        except Exception as e:
            print(f"❌ Error removing lock file: {e}")
            return False
    else:
        print(f"ℹ️ No lock file found at {lock_file}")
        return True

def check_lock_status():
    """Checks the current lock status"""
    lock_file = "/tmp/gemaibot.lock"
    
    if os.path.exists(lock_file):
        try:
            with open(lock_file, 'r') as f:
                pid = f.read().strip()
                print(f"🔒 Lock file exists with PID: {pid}")
                return False
        except Exception as e:
            print(f"⚠️ Lock file exists but cannot read PID: {e}")
            return False
    else:
        print("✅ No lock file found - bot can start")
        return True

if __name__ == "__main__":
    print("=== BOT LOCK UTILITY ===")
    
    if len(sys.argv) > 1 and sys.argv[1] == "clear":
        print("Clearing bot lock...")
        if clear_lock():
            print("Lock cleared successfully")
        else:
            print("Failed to clear lock")
            sys.exit(1)
    else:
        print("Checking lock status...")
        check_lock_status()
        print("\nUsage:")
        print("  python clear_lock.py          - Check lock status")
        print("  python clear_lock.py clear    - Clear existing lock")
