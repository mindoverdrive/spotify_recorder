import sys
import os
sys.path.insert(0, os.path.abspath("."))
from qobuz_automation import MacOSQobuzAccessibility

try:
    ax = MacOSQobuzAccessibility()
    print("AXTrusted:", ax.check())
    print("Main window:", ax._main_window())
except Exception as e:
    print("Error:", e)
