import sys
import os
import AppKit
import ApplicationServices as AS
sys.path.insert(0, os.path.abspath("."))
from qobuz_automation import MacOSQobuzAccessibility

def inspect_windows():
    ax = MacOSQobuzAccessibility()
    app = ax._running_application()
    app_element = AS.AXUIElementCreateApplication(app.processIdentifier())
    
    print("MainWindow:", ax._attribute(app_element, AS.kAXMainWindowAttribute))
    windows = ax._attribute(app_element, AS.kAXWindowsAttribute, ())
    for w in windows:
        title = ax._attribute(w, AS.kAXTitleAttribute)
        role = ax._attribute(w, AS.kAXRoleAttribute)
        subrole = ax._attribute(w, AS.kAXSubroleAttribute)
        print(f"Window: {w}, Title: {title}, Role: {role}, Subrole: {subrole}")
        children = ax._attribute(w, AS.kAXChildrenAttribute, ())
        print(f"  Children count: {len(children)}")
        for c in children:
            print(f"    Child Role: {ax._attribute(c, AS.kAXRoleAttribute)}, Subrole: {ax._attribute(c, AS.kAXSubroleAttribute)}")

inspect_windows()
