import sys
import os
import AppKit
import ApplicationServices as AS
sys.path.insert(0, os.path.abspath("."))
from qobuz_automation import QobuzAutomation, MacOSQobuzAccessibility

def dump_qobuz_album_ui():
    auto = QobuzAutomation()
    auto.ensure_running()
    
    # Force open an album
    auto._run(["open", "qobuzapp://album/0886443403192"]) # The Money Store (Hacker is on this)
    auto.sleep(3.0)
    
    ax = MacOSQobuzAccessibility()
    print("Dumping Qobuz UI near 'Hacker'...")
    
    # Dump all static texts and buttons
    for item in ax._iter_items(limit=5000):
        role = ax._role(item)
        if role in ("AXStaticText", "AXButton", "AXLink", "AXHeading"):
            texts = list(ax._texts(item))
            if any("hacker" in str(t).lower() or "デス・グリップス" in str(t).lower() for t in texts):
                print(f"FOUND TARGET TEXT: Role: {role}, Texts: {texts}, Frame: {ax._frame(item)}")
            elif "13" in texts: # Hacker is track 13
                print(f"FOUND NUMBER 13: Role: {role}, Texts: {texts}, Frame: {ax._frame(item)}")
            elif "play" in [str(t).lower() for t in texts] or "再生" in [str(t).lower() for t in texts]:
                print(f"FOUND PLAY BUTTON: Role: {role}, Texts: {texts}, Frame: {ax._frame(item)}")

if __name__ == "__main__":
    dump_qobuz_album_ui()
