import sys
import os
import AppKit
import ApplicationServices as AS
sys.path.insert(0, os.path.abspath("."))
from qobuz_automation import MacOSQobuzAccessibility

def inspect_group():
    ax = MacOSQobuzAccessibility()
    app = ax._running_application()
    app_element = AS.AXUIElementCreateApplication(app.processIdentifier())
    w = ax._attribute(app_element, AS.kAXMainWindowAttribute)
    children = ax._attribute(w, AS.kAXChildrenAttribute, ())
    group = children[0]
    print(f"Group: {group}")
    group_children = ax._attribute(group, AS.kAXChildrenAttribute, ())
    print(f"Group children count: {len(group_children)}")
    for c in group_children:
        print(f"  Child Role: {ax._attribute(c, AS.kAXRoleAttribute)}")

inspect_group()
