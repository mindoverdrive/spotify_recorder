import sys
import os
import AppKit
import ApplicationServices as AS
sys.path.insert(0, os.path.abspath("."))
from qobuz_automation import QobuzAutomation, MacOSQobuzAccessibility

def dump_qobuz_all_texts():
    auto = QobuzAutomation()
    auto.ensure_running()
    
    ax = MacOSQobuzAccessibility()
    print("Dumping all texts...")
    
    with open("qobuz_texts.txt", "w") as f:
        for item in ax._iter_items(limit=20000):
            role = ax._role(item)
            if role in ("AXStaticText", "AXButton", "AXLink", "AXHeading"):
                texts = list(ax._texts(item))
                if texts:
                    f.write(f"Role: {role}, Texts: {texts}, Frame: {ax._frame(item)}\n")

if __name__ == "__main__":
    dump_qobuz_all_texts()
