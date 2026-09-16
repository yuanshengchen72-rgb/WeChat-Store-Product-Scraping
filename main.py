import sys

from wechat_shop_scraper.gui import run_gui


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        from wechat_shop_scraper.selftest import run_self_test

        raise SystemExit(run_self_test())
    run_gui()
