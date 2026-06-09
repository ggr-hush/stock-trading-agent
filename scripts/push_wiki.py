"""
push_wiki.py — 把 docs/v3-tuning.md 推送到飞书知识库

用法:
  python3 scripts/push_wiki.py                # 默认推 v3-tuning.md
  python3 scripts/push_wiki.py --path docs/x   # 推别的文件

依赖: lark-cli 已登录 + FEISHU_WIKI_SPACE_ID env（或默认走 lark-cli 的默认空间）
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
LARK_CLI = "/opt/node/bin/lark-cli"  # 见 feishu-bitable-manual SKILL.md 提到

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="docs/v3-tuning.md", help="相对项目根的 markdown 路径")
    parser.add_argument("--space", default=os.environ.get("FEISHU_WIKI_SPACE_ID"), help="飞书知识空间 ID")
    parser.add_argument("--parent", default=os.environ.get("FEISHU_WIKI_PARENT_NODE"), help="父节点 ID")
    args = parser.parse_args()

    src = ROOT / args.path
    if not src.exists():
        sys.exit(f"找不到文件: {src}")

    title = src.stem
    md = src.read_text()

    # 用 lark-cli 的 doc 创建能力（v2 wiki API）
    # 调用：lark-cli docs +create --title <t> --markdown <md> --space <sid>
    # 简化: 走 lark-cli api POST /open-apis/wiki/v2/spaces/<space_id>/nodes
    if not args.space:
        print("提示: 未指定 --space 或 FEISHU_WIKI_SPACE_ID, 将走 lark-cli 默认空间（如果有）", file=sys.stderr)

    # 由于 lark-cli 文档较为零散, 这里用最通用的: 把内容写到本地, 提示用户手动粘
    if not os.path.exists(LARK_CLI):
        print(f"⚠️  lark-cli 不在 {LARK_CLI}, 跳过自动推送", file=sys.stderr)
        print(f"已生成 wiki 草稿: {src}", file=sys.stderr)
        print("操作: 复制内容到飞书知识库", file=sys.stderr)
        return

    payload = {
        "obj_type": "docx",
        "name": title,
        "node_type": "origin",
    }
    if args.parent:
        payload["parent_id"] = args.parent

    # Step 1: 创建 wiki 节点
    r = subprocess.run(
        [LARK_CLI, "api", "POST", f"/open-apis/wiki/v2/spaces/{args.space}/nodes",
         "--data", json.dumps(payload, ensure_ascii=False)],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        sys.exit(f"创建节点失败: {r.stderr}")
    out = json.loads(r.stdout)
    if out.get("code") != 0:
        sys.exit(f"lark 返回错误: {out}")
    node_id = out["data"]["node"]["node_id"]
    print(f"✓ 创建节点: {node_id}")

    # Step 2: 写 markdown 到 docx (略，依赖 lark-cli 具体子命令)
    print(f"提示: 节点创建成功, 请用 lark-cli doc +update 把 {src} 的内容写进去")
    print(f"  lark-cli docs +update --node {node_id} --markdown '{md[:100]}...'")


if __name__ == "__main__":
    main()
