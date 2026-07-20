import sys
import os
import time
import subprocess
import AppKit
import ApplicationServices as AS
sys.path.insert(0, os.path.abspath("."))
from qobuz_automation import QobuzAutomation, MacOSQobuzAccessibility

def test_qobuz_window():
    subprocess.run(["osascript", "-e", 'tell application "Qobuz" to close every window'])
    time.sleep(1)
    
    auto = QobuzAutomation()
    print("Is running?", auto.runner(["pgrep", "-x", "Qobuz"]) != "")
    
    ax = MacOSQobuzAccessibility()
    try:
        win = ax._main_window()
        print("Window found:", win)
    except Exception as e:
        print("Error getting window:", e)
        
    print("Trying to activate with open -a...")
    subprocess.run(["open", "-a", "Qobuz"])
    time.sleep(1)
    try:
        win = ax._main_window()
        print("Window found after open -a:", win)
    except Exception as e:
        print("Error after open -a:", e)

test_qobuz_window()
