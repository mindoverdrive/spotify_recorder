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
    
    print("Dumping all roles...")
    counts = {}
    for item in ax._iter_items(limit=50000):
        role = ax._role(item)
        counts[role] = counts.get(role, 0) + 1

    print("Counts by role:", counts)

dump_qobuz_all()
