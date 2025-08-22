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
            # Проверяем содержимое файла блокировки
            with open(lock_file, 'r') as f:
                pid_str = f.read().strip()
                if pid_str.isdigit():
                    pid = int(pid_str)
                    print(f"Found lock file with PID: {pid}")
                    
                    # Проверяем, существует ли процесс
                    try:
                        os.kill(pid, 0)  # Проверяем существование процесса
                        print(f"⚠️ Process {pid} is still running!")
                        print("This lock is active and should not be cleared manually.")
                        return False
                    except OSError:
                        print(f"✅ Process {pid} is not running (stale lock)")
                        print("Safe to remove this lock file.")
                else:
                    print(f"⚠️ Lock file contains invalid PID: '{pid_str}'")
                    print("Safe to remove this corrupted lock file.")
            
            # Удаляем файл блокировки
            os.unlink(lock_file)
            print(f"✅ Lock file {lock_file} removed successfully")
            return True
            
        except Exception as e:
            print(f"❌ Error processing lock file: {e}")
            # Пытаемся принудительно удалить файл
            try:
                os.unlink(lock_file)
                print(f"✅ Lock file forcibly removed")
                return True
            except Exception as e2:
                print(f"❌ Failed to remove lock file: {e2}")
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
                pid_str = f.read().strip()
                if pid_str.isdigit():
                    pid = int(pid_str)
                    print(f"🔒 Lock file exists with PID: {pid}")
                    
                    # Проверяем, существует ли процесс
                    try:
                        os.kill(pid, 0)
                        print(f"✅ Process {pid} is running - lock is active")
                        return False
                    except OSError:
                        print(f"⚠️ Process {pid} is not running - lock is stale")
                        print("You can safely clear this lock with: python clear_lock.py clear")
                        return False
                else:
                    print(f"⚠️ Lock file contains invalid PID: '{pid_str}'")
                    print("This is a corrupted lock file that should be cleared")
                    return False
        except Exception as e:
            print(f"⚠️ Lock file exists but cannot read PID: {e}")
            print("This is a corrupted lock file that should be cleared")
            return False
    else:
        print("✅ No lock file found - bot can start")
        return True

def force_clear_lock():
    """Forcibly clears the lock file without checking process status"""
    lock_file = "/tmp/gemaibot.lock"
    
    if os.path.exists(lock_file):
        try:
            os.unlink(lock_file)
            print(f"✅ Lock file forcibly removed")
            return True
        except Exception as e:
            print(f"❌ Failed to remove lock file: {e}")
            return False
    else:
        print(f"ℹ️ No lock file found at {lock_file}")
        return True

if __name__ == "__main__":
    print("=== BOT LOCK UTILITY ===")
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "clear":
            print("Clearing bot lock...")
            if clear_lock():
                print("Lock cleared successfully")
            else:
                print("Failed to clear lock")
                sys.exit(1)
        elif sys.argv[1] == "force":
            print("Force clearing bot lock...")
            if force_clear_lock():
                print("Lock force cleared successfully")
            else:
                print("Failed to force clear lock")
                sys.exit(1)
        else:
            print(f"Unknown command: {sys.argv[1]}")
            sys.exit(1)
    else:
        print("Checking lock status...")
        check_lock_status()
        print("\nUsage:")
        print("  python clear_lock.py          - Check lock status")
        print("  python clear_lock.py clear    - Clear existing lock (safe)")
        print("  python clear_lock.py force    - Force clear lock (unsafe)")
