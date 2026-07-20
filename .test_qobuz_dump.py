import sys
import os
import AppKit
import ApplicationServices as AS
sys.path.insert(0, os.path.abspath("."))
from qobuz_automation import QobuzAutomation, MacOSQobuzAccessibility

def dump_qobuz_ui():
    auto = QobuzAutomation()
    auto.ensure_running()
    ax = MacOSQobuzAccessibility()
    
    print("Dumping visible texts and buttons...")
    count = 0
    for item in ax._iter_items(limit=10000):
        role = ax._role(item)
        if role in (AS.kAXButtonRole, AS.kAXStaticTextRole):
            frame = ax._frame(item)
            if frame and frame[2] > 0 and frame[3] > 0:
                texts = list(ax._texts(item))
                if texts:
                    print(f"Role: {role}, Texts: {texts}, Frame: {frame}")
        count += 1

dump_qobuz_ui()
