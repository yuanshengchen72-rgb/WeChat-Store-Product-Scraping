import sys

# Tkinter is imported by the GUI module.  In a frozen build the Tcl/Tk
# runtime must be copied and its paths exported *before* that import; doing
# it from inside run_gui is too late because _tkinter resolves init.tcl at
# module import time.
from wechat_shop_scraper.app_data import prepare_frozen_tk_runtime

prepare_frozen_tk_runtime()
from wechat_shop_scraper.gui import run_gui


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        from wechat_shop_scraper.selftest import run_self_test

        raise SystemExit(run_self_test())
    run_gui()
