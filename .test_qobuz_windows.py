import sys
import os
import time
import AppKit
import ApplicationServices as AS
sys.path.insert(0, os.path.abspath("."))
from qobuz_automation import QobuzAutomation, MacOSQobuzAccessibility

def inspect_qobuz():
    auto = QobuzAutomation()
    auto.ensure_running()
    # auto.activate() # Try activating
    
    app = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.qobuz.desktop")
    if not app:
        print("Qobuz not running")
        return
    app = app[0]
    print(f"PID: {app.processIdentifier()}")
    
    app_element = AS.AXUIElementCreateApplication(app.processIdentifier())
    
    # Try fetching windows
    for i in range(5):
        main_win = MacOSQobuzAccessibility._attribute(app_element, AS.kAXMainWindowAttribute)
        windows = MacOSQobuzAccessibility._attribute(app_element, AS.kAXWindowsAttribute, ())
        print(f"[{i}] Main window: {main_win}")
        print(f"[{i}] Windows: {windows}")
        if windows:
            break
        time.sleep(0.5)

inspect_qobuz()
