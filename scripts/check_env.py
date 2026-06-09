"""check_env.py — 一眼看穿 .env 状态

用法:
  python3 scripts/check_env.py           # 静态检查 (无网络)
  python3 scripts/check_env.py --ping    # 静态 + 真实调用 (LLM 1次 + Feishu token)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from stock_trading_agent.engine.data_fetcher import load_env  # noqa: E402


KEY_META = [
    ("MINIMAX_API_KEY",          "MiniMax M3 LLM",         "必填 (无 key 时 LLM 降级, 主流程不挂)"),
    ("FEISHU_APP_ID",            "飞书 App ID",            "PUSH_MODE=app/auto 时必填"),
    ("FEISHU_APP_SECRET",        "飞书 App Secret",        "PUSH_MODE=app/auto 时必填"),
    ("FEISHU_SINA_UT",           "东财 ut (自动)",        "可选, 留空=硬编码默认+自动从东财页面拉"),
    ("FEISHU_DASH_APP_TOKEN",    "飞书多维表格 token",     "必填 (写选股结果)"),
    ("FEISHU_DASH_DASHBOARD_ID", "飞书 dashboard ID",      "必填"),
    ("FEISHU_BITABLE_WEBHOOK",   "飞书自定机器人 webhook", "PUSH_MODE=webhook/auto 时作为 app 的 fallback"),
    ("FEISHU_CHAT_ID",           "飞书群 chat_id",         "PUSH_MODE=app/auto 时必填 (消息发到哪个群)"),
    ("FEISHU_PUSH_MODE",         "推送通道模式",          "app / webhook / auto, 默认 auto"),
    ("BOT_ENCRYPTION_KEY",       "Session Fernet 加密",    "可选 (留空=明文 session)"),
]


_PLACEHOLDER_TOKENS = {"your-", "xxx", "placeholder", "changeme", "todo", "<"}


def _is_placeholder(v: str) -> bool:
    """识别 .env 里的占位符

    触发任一条件即视为占位符:
      1. 尖括号包着: <...>
      2. 字面 "your-" / "xxx" / "placeholder" / "changeme" / "todo" 开头
      3. 全是 xxx / placeholder 字面
    """
    if not v:
        return True
    if v.startswith("<") and v.endswith(">"):
        return True
    low = v.lower()
    for tok in _PLACEHOLDER_TOKENS:
        if low.startswith(tok):
            return True
    return False


def _mask(v: str) -> str:
    if not v or _is_placeholder(v):
        return v
    if len(v) <= 8:
        return v[:2] + "***"
    return v[:4] + "***" + v[-4:]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ping", action="store_true",
                   help="除静态检查外, 真实调一次 LLM 和 Feishu token 端点")
    args = p.parse_args()

    env = load_env()
    env_file = _ROOT / ".env"
    hermes = Path.home() / ".hermes" / ".env"

    print("=" * 60)
    print(f"  Project root : {_ROOT}")
    print(f"  Local .env   : {env_file}  {'OK' if env_file.exists() else 'MISS'}")
    print(f"  Hermes .env  : {hermes}  {'OK' if hermes.exists() else 'MISS'}")
    print("=" * 60)
    print()
    print(f"  {'KEY':<28}  {'STATUS':<10}  {'VALUE':<24}  NOTE")
    print(f"  {'-' * 28}  {'-' * 10}  {'-' * 24}  ----")

    ok = 0
    fail = 0
    for k, label, note in KEY_META:
        v = env.get(k, "")
        push_mode = env.get("FEISHU_PUSH_MODE", "auto").lower()
        if k == "FEISHU_SINA_UT":
            nonblocking = True
        elif k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID"):
            has_webhook = bool(env.get("FEISHU_BITABLE_WEBHOOK", ""))
            nonblocking = (push_mode == "auto" and has_webhook) or push_mode == "webhook"
        elif k == "FEISHU_BITABLE_WEBHOOK":
            has_app = all(env.get(x) for x in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID"))
            nonblocking = (push_mode == "auto" and has_app) or push_mode == "app"
        else:
            nonblocking = False
        if not v:
            if nonblocking: status, val = "OK (fallback)", "(有替代通道)"; ok += 1
            else: status, val = "MISSING", "(unset)"; fail += 1
        elif _is_placeholder(v):
            if nonblocking: status, val = "OK (fallback)", "(有替代通道)"; ok += 1
            else: status, val = "PLACEHOLDER", v; fail += 1
        else:
            status, val = "OK", _mask(v); ok += 1
        print(f"  {k:<28}  {status:<10}  {val:<24}  {label}")

    print()
    print(f"  -> {ok} set / {fail} to-fill")
    print()
    pm = env.get("FEISHU_PUSH_MODE", "auto").lower()
    pm_descr = {"app": "强制 OpenAPI app (要 APP_ID/SECRET/CHAT_ID)", "webhook": "强制自定机器人 (要 BITABLE_WEBHOOK)", "auto": "优先 app, 回退 webhook (推荐, 默认)"}.get(pm, pm)
    print(f"  Push mode : {pm}  ({pm_descr})")

    if args.ping:
        print("=" * 60)
        print("  Connectivity test")
        print("=" * 60)
        _ping_llm(env)
        _ping_feishu(env)
        print()

    if fail:
        print("  Hint: PUSH_MODE=app 时阻塞 = MINIMAX + APP_ID/SECRET/CHAT_ID + 2 DASH; auto 时 BITABLE 与 APP_* 至少一组; 全 OK 后跑 agent daemon")
        return 1
    print("  All 8 keys ready. Try: python3 -m stock_trading_agent.agent daemon")
    return 0


def _ping_llm(env: dict) -> None:
    key = env.get("MINIMAX_API_KEY", "")
    if not key or _is_placeholder(key):
        print("  [LLM]    skip (key not set)")
        return
    try:
        from stock_trading_agent.llm.client import chat
        out = chat("ping", system="You only reply 'pong'.", model="MiniMax-M3", max_tokens=8)
        out = (out or "").strip()
        if out:
            print(f"  [LLM]    OK, response: {out[:30]!r}")
        else:
            print("  [LLM]    empty response (degraded or network issue)")
    except Exception as e:
        print(f"  [LLM]    error: {type(e).__name__}: {e}")


def _ping_feishu(env: dict) -> None:
    app_id = env.get("FEISHU_APP_ID", "")
    app_secret = env.get("FEISHU_APP_SECRET", "")
    if not app_id or _is_placeholder(app_id) or not app_secret or _is_placeholder(app_secret):
        print("  [Feishu] skip (APP_ID/SECRET not set)")
        return
    try:
        import requests
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=5,
        )
        data = r.json()
        if data.get("code") == 0 and data.get("tenant_access_token"):
            exp = data.get("expire", 0)
            print(f"  [Feishu] OK, tenant_access_token expire={exp}s")
        else:
            print(f"  [Feishu] auth failed: {data.get('msg') or data}")
    except Exception as e:
        print(f"  [Feishu] error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
