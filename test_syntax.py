#!/usr/bin/env python3
"""
Simple syntax test script to verify all Python files compile correctly.
"""
import os
import sys
import py_compile
from pathlib import Path

def test_file_syntax(file_path):
    """Test if a Python file has correct syntax."""
    try:
        py_compile.compile(file_path, doraise=True)
        print(f"✅ {file_path} - OK")
        return True
    except py_compile.PyCompileError as e:
        print(f"❌ {file_path} - Syntax Error: {e}")
        return False
    except Exception as e:
        print(f"❌ {file_path} - Error: {e}")
        return False

def main():
    """Test syntax of all Python files."""
    print("🔍 Testing Python file syntax...")
    print("=" * 50)
    
    # Files to test
    test_files = [
        "bot.py",
        "app/document_processor.py",
        "app/services.py",
        "app/utils/network.py",
        "app/utils/health_monitor.py"
    ]
    
    success_count = 0
    total_count = len(test_files)
    
    for file_path in test_files:
        if os.path.exists(file_path):
            if test_file_syntax(file_path):
                success_count += 1
        else:
            print(f"⚠️  {file_path} - File not found")
    
    print("=" * 50)
    print(f"📊 Results: {success_count}/{total_count} files passed syntax check")
    
    if success_count == total_count:
        print("🎉 All files have correct syntax!")
        return 0
    else:
        print("❌ Some files have syntax errors!")
        return 1

if __name__ == "__main__":
    sys.exit(main())
