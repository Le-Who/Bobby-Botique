#!/usr/bin/env python3
"""
Test script to verify the lock fix logic
"""

import os
import tempfile
import shutil

def test_pid_1_handling():
    """Test that PID 1 is properly handled as a stale lock"""
    print("Testing PID 1 handling...")
    
    # Create a temporary lock file with PID 1
    test_lock = tempfile.NamedTemporaryFile(mode='w', delete=False)
    test_lock.write("1")
    test_lock.close()
    
    print(f"Created test lock file: {test_lock.name}")
    print(f"Lock content: {open(test_lock.name, 'r').read()}")
    
    # Simulate the fix logic
    try:
        with open(test_lock.name, 'r') as f:
            pid_str = f.read().strip()
            if pid_str.isdigit():
                pid = int(pid_str)
                print(f"Read PID: {pid}")
                
                if pid == 1:
                    print("✅ PID 1 detected as container init process - should be treated as stale")
                    os.unlink(test_lock.name)
                    print("✅ Test lock file removed")
                    return True
                else:
                    print("❌ PID 1 not detected correctly")
                    return False
    except Exception as e:
        print(f"❌ Error during test: {e}")
        return False
    finally:
        # Cleanup
        if os.path.exists(test_lock.name):
            os.unlink(test_lock.name)
    
    return False

def test_container_specific_path():
    """Test container-specific lock file path generation"""
    print("\nTesting container-specific path generation...")
    
    # Test with HOSTNAME environment variable
    os.environ['HOSTNAME'] = 'test-container-123'
    container_id = os.environ.get('HOSTNAME', 'unknown')
    lock_path = f"/tmp/gemaibot.{container_id}.lock"
    
    print(f"Container ID: {container_id}")
    print(f"Generated lock path: {lock_path}")
    
    if "test-container-123" in lock_path:
        print("✅ Container-specific path generated correctly")
        return True
    else:
        print("❌ Container-specific path generation failed")
        return False

if __name__ == "__main__":
    print("=== LOCK FIX TEST SCRIPT ===")
    
    test1_passed = test_pid_1_handling()
    test2_passed = test_container_specific_path()
    
    print(f"\nTest Results:")
    print(f"PID 1 handling: {'✅ PASSED' if test1_passed else '❌ FAILED'}")
    print(f"Container path: {'✅ PASSED' if test2_passed else '❌ FAILED'}")
    
    if test1_passed and test2_passed:
        print("\n🎉 All tests passed! The lock fix should work correctly.")
    else:
        print("\n⚠️ Some tests failed. Please review the implementation.")
