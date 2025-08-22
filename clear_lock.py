#!/usr/bin/env python3
"""
Utility script to clear bot locks.
Useful when deploying to Render to ensure no stale locks remain.
"""

import os
import sys

def cleanup_legacy_locks():
    """Cleans up legacy lock files that don't have container-specific naming"""
    legacy_lock_file = "/tmp/gemaibot.lock"
    
    if os.path.exists(legacy_lock_file):
        try:
            print(f"Found legacy lock file: {legacy_lock_file}")
            with open(legacy_lock_file, 'r') as f:
                pid_str = f.read().strip()
                if pid_str.isdigit():
                    pid = int(pid_str)
                    print(f"Legacy lock contains PID: {pid}")
                    
                    # Always treat legacy locks as stale since they're not container-specific
                    if pid == 1:
                        print("Legacy lock with PID 1 (container init) - removing")
                    else:
                        print("Legacy lock with non-container PID - removing")
                    
                    os.unlink(legacy_lock_file)
                    print("✅ Legacy lock file removed")
                    return True
                else:
                    print("Legacy lock contains invalid PID - removing")
                    os.unlink(legacy_lock_file)
                    print("✅ Legacy lock file removed")
                    return True
        except Exception as e:
            print(f"Error processing legacy lock: {e}")
            try:
                os.unlink(legacy_lock_file)
                print("✅ Legacy lock file forcibly removed")
                return True
            except Exception as e2:
                print(f"Failed to remove legacy lock: {e2}")
                return False
    return True

def get_lock_file_path():
    """Gets the container-specific lock file path"""
    container_id = os.environ.get('HOSTNAME', 'unknown')
    return f"/tmp/gemaibot.{container_id}.lock"

def clear_lock():
    """Clears any existing bot lock files"""
    # First clean up any legacy locks
    cleanup_legacy_locks()
    
    lock_file = get_lock_file_path()
    
    if os.path.exists(lock_file):
        try:
            # Проверяем содержимое файла блокировки
            with open(lock_file, 'r') as f:
                pid_str = f.read().strip()
                if pid_str.isdigit():
                    pid = int(pid_str)
                    print(f"Found lock file with PID: {pid}")
                    
                    # CRITICAL FIX: PID 1 is the container init process and should be treated as stale
                    # In containerized environments, PID 1 is always running but represents the container itself
                    if pid == 1:
                        print(f"⚠️ PID {pid} is container init process - this is a stale lock")
                        print("Safe to remove this lock file.")
                    else:
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
    # Check for legacy locks first
    legacy_lock_file = "/tmp/gemaibot.lock"
    if os.path.exists(legacy_lock_file):
        print(f"⚠️ Legacy lock file found: {legacy_lock_file}")
        print("This should be cleared for proper container operation")
    
    lock_file = get_lock_file_path()
    
    if os.path.exists(lock_file):
        try:
            with open(lock_file, 'r') as f:
                pid_str = f.read().strip()
                if pid_str.isdigit():
                    pid = int(pid_str)
                    print(f"🔒 Lock file exists with PID: {pid}")
                    
                    # CRITICAL FIX: PID 1 is the container init process and should be treated as stale
                    if pid == 1:
                        print(f"⚠️ PID {pid} is container init process - this is a stale lock")
                        print("You can safely clear this lock with: python clear_lock.py clear")
                        return False
                    else:
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
    # Clear legacy locks first
    cleanup_legacy_locks()
    
    lock_file = get_lock_file_path()
    
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

def clear_all_locks():
    """Clears all possible lock files - useful for deployment scenarios"""
    print("Clearing all possible lock files...")
    
    # Clear legacy lock
    legacy_cleared = cleanup_legacy_locks()
    
    # Clear current container-specific lock
    current_cleared = force_clear_lock()
    
    # Also check for any other gemaibot lock files
    import glob
    other_locks = glob.glob("/tmp/gemaibot.*.lock")
    other_cleared = 0
    
    for lock_file in other_locks:
        try:
            os.unlink(lock_file)
            print(f"✅ Removed additional lock: {lock_file}")
            other_cleared += 1
        except Exception as e:
            print(f"⚠️ Could not remove {lock_file}: {e}")
    
    total_cleared = (1 if legacy_cleared else 0) + (1 if current_cleared else 0) + other_cleared
    print(f"✅ Total locks cleared: {total_cleared}")
    return total_cleared > 0

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
        elif sys.argv[1] == "all":
            print("Clearing all possible locks...")
            if clear_all_locks():
                print("All locks cleared successfully")
            else:
                print("Failed to clear all locks")
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
        print("  python clear_lock.py all      - Clear all possible locks (deployment)")
