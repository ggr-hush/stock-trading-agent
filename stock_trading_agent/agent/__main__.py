"""agent/__main__.py — v12.6: 让 `python -m stock_trading_agent.agent` 工作

转发到 cli.main()
"""
from .cli import main

if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
