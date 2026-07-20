import sys
import os
import AppKit
import ApplicationServices as AS
sys.path.insert(0, os.path.abspath("."))
from qobuz_automation import QobuzAutomation, MacOSQobuzAccessibility

def dump_qobuz_all():
    auto = QobuzAutomation()
    auto.ensure_running()
    ax = MacOSQobuzAccessibility()
    
    print("Dumping texts and buttons...")
    for item in ax._iter_items(limit=10000):
        role = ax._role(item)
        if role in (AS.kAXButtonRole, AS.kAXStaticTextRole):
            texts = list(ax._texts(item))
            if texts:
                try:
                    desc = ax._attribute(item, AS.kAXDescriptionAttribute)
                except Exception:
                    desc = None
                print(f"Role: {role}, Texts: {repr(texts)}, Desc: {desc}")

dump_qobuz_all()
