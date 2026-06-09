"""
yaml_to_json.py — 一次性工具：把 config.yaml 镜像成 config.json

背景：v1 设计允许 YAML（人类可读）或 JSON（沙箱/无 PyYAML 环境）配置。
两个文件保持同步，loader 自动 fallback。

用法：
  pip install pyyaml
  python3 scripts/yaml_to_json.py
"""
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("需要 PyYAML: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).parent.parent
SRC = ROOT / "stock_trading_agent" / "config.yaml"
DST = ROOT / "stock_trading_agent" / "config.json"

with open(SRC) as f:
    data = yaml.safe_load(f)

DST.write_text(json.dumps(data, ensure_ascii=False, indent=2))
print(f"OK: {SRC} -> {DST}")
