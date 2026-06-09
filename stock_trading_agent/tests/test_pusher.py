"""test_pusher.py — 飞书推送通道分发器

覆盖 3 种 FEISHU_PUSH_MODE:
  - app     : 强制走 OpenAPI app
  - webhook : 强制走 BITABLE_WEBHOOK
  - auto    : 优先 app, 缺凭据回退 webhook
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

import stock_trading_agent.feishu.pusher as ps
from stock_trading_agent.engine import data_fetcher as df


# ─────────── helpers ───────────

_ALL_KEYS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID",
             "FEISHU_BITABLE_WEBHOOK", "FEISHU_PUSH_MODE")

_FAKE_APP = {
    "FEISHU_APP_ID": "cli_test_app_id",
    "FEISHU_APP_SECRET": "test_app_secret",
    "FEISHU_CHAT_ID": "oc_test_chat_id",
}


def _reset():
    df._ENV_CACHE = {}
    df._UT_CACHE = None
    for k in _ALL_KEYS:
        os.environ.pop(k, None)


def _seed(**kw):
    """set up env: 默认 app 三件套齐, kw 可覆盖

    注意: 直接覆盖 df._ENV_CACHE (而不是依赖 load_env 兜底读 .env),
    这样测试不会被项目根 .env 里的真值污染。
    """
    _reset()
    base = {**_FAKE_APP, "FEISHU_BITABLE_WEBHOOK": "https://open.feishu.cn/hook/test_webhook", **kw}
    fake_env = {k: v for k, v in base.items() if v is not None}
    df._ENV_CACHE = fake_env  # 直接灌入, 跳过 .env 兜底


def _mock_post_ok(*args, **kwargs):
    """默认: token + message 都返回 code=0"""
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '{"code":0,"msg":"ok"}'
    resp.json.return_value = {"code": 0, "msg": "ok"}
    return resp


# ─────────── _send() 分发器 ───────────

def test_send_auto_uses_app_when_ready() -> None:
    """auto + app 三件套齐 → app 通道"""
    _seed(FEISHU_PUSH_MODE="auto")

    with patch.object(ps, "http_post", side_effect=_mock_post_ok) as mp:
        res = ps._send("hello", msg_type="text")
        assert res["ok"] is True, f"expected ok, got {res}"
        assert res["channel"] == "app", f"expected app, got {res.get('channel')}"
        assert mp.call_count == 2, f"expected 2 POSTs (token+message), got {mp.call_count}"
    print("  ✓ test_send_auto_uses_app_when_ready")
    _reset()


def test_send_auto_falls_back_to_webhook() -> None:
    """auto + 缺 chat_id → 回退 webhook (不应调到 _send_via_app)"""
    _seed(FEISHU_CHAT_ID=None, FEISHU_PUSH_MODE="auto")

    fake = MagicMock(status_code=200, text="ok")
    with patch.object(ps, "http_post", return_value=fake) as mp:
        res = ps._send("hello")
        assert res["ok"] is True, f"expected ok, got {res}"
        assert res["channel"] == "webhook", f"expected webhook, got {res.get('channel')}"
        assert mp.call_count == 1, f"webhook 只该 1 次 POST, got {mp.call_count}"
    print("  ✓ test_send_auto_falls_back_to_webhook")
    _reset()


def test_send_app_mode_missing_credentials_fails() -> None:
    """app 模式 + 缺凭据 → 明确报错, 不发请求, 不回退"""
    _seed(FEISHU_APP_ID=None, FEISHU_APP_SECRET=None, FEISHU_CHAT_ID=None, FEISHU_PUSH_MODE="app")

    with patch.object(ps, "http_post") as mp:
        res = ps._send("hello")
        assert res["ok"] is False, f"expected fail, got {res}"
        assert "APP_ID" in res.get("error", "") or "未齐" in res.get("error", "")
        assert mp.call_count == 0, f"缺凭据时不该发任何 HTTP, got {mp.call_count}"
    print("  ✓ test_send_app_mode_missing_credentials_fails")
    _reset()


def test_send_explicit_webhook_mode() -> None:
    """webhook 模式 → 即便 app 齐也走 webhook"""
    _seed(FEISHU_PUSH_MODE="webhook")  # app 三件套都齐, 但 mode=webhook

    fake = MagicMock(status_code=200, text="ok")
    with patch.object(ps, "http_post", return_value=fake) as mp:
        res = ps._send("hello")
        assert res["ok"] is True, f"expected ok, got {res}"
        assert res["channel"] == "webhook"
        assert mp.call_count == 1
    print("  ✓ test_send_explicit_webhook_mode")
    _reset()


def test_send_via_app_token_error() -> None:
    """app 通道: tenant_access_token 返回 code≠0 → 友好报错"""
    _seed(FEISHU_PUSH_MODE="app")

    bad_token = MagicMock()
    bad_token.json.return_value = {"code": 10003, "msg": "invalid app_id"}
    with patch.object(ps, "http_post", return_value=bad_token):
        res = ps._send("hello")
        assert res["ok"] is False
        assert "tenant_access_token 失败" in res.get("error", "")
        assert "invalid app_id" in res.get("error", "")
    print("  ✓ test_send_via_app_token_error")
    _reset()


def test_send_auto_no_app_no_webhook() -> None:
    """auto + 啥都没设 → 友好报错 (而不是抛异常)"""
    _seed(FEISHU_APP_ID=None, FEISHU_APP_SECRET=None, FEISHU_CHAT_ID=None,
          FEISHU_BITABLE_WEBHOOK=None, FEISHU_PUSH_MODE="auto")

    with patch.object(ps, "http_post") as mp:
        res = ps._send("hello")
        assert res["ok"] is False
        assert mp.call_count == 0
    print("  ✓ test_send_auto_no_app_no_webhook")
    _reset()

def test_send_placeholder_app_treated_as_missing() -> None:
    """auto + app 三件套都是 <...> 占位符 → 不该当 ready, 不该发请求"""
    _seed(FEISHU_APP_ID="<cli_xxx>", FEISHU_APP_SECRET="<sec>",
          FEISHU_CHAT_ID="<oc_xxx>", FEISHU_BITABLE_WEBHOOK=None,
          FEISHU_PUSH_MODE="auto")

    with patch.object(ps, "http_post") as mp:
        res = ps._send("hello")
        assert res["ok"] is False
        assert res["channel"] == "webhook"  # 回退 webhook, 但 webhook 也缺 → 报错
        assert mp.call_count == 0
    print("  ✓ test_send_placeholder_app_treated_as_missing")
    _reset()


def test_send_placeholder_webhook_treated_as_missing() -> None:
    """webhook 模式 + URL 是 <...> 占位符 → 不该发请求"""
    _seed(FEISHU_APP_ID=None, FEISHU_APP_SECRET=None, FEISHU_CHAT_ID=None,
          FEISHU_BITABLE_WEBHOOK="<your-bitable-webhook-url>",
          FEISHU_PUSH_MODE="webhook")

    with patch.object(ps, "http_post") as mp:
        res = ps._send("hello")
        assert res["ok"] is False
        assert "未设" in res.get("error", "")
        assert mp.call_count == 0
    print("  ✓ test_send_placeholder_webhook_treated_as_missing")
    _reset()


# ─────────── 8 个 push 函数都走 _send ───────────

def test_push_functions_use_dispatcher() -> None:
    """8 个 push 函数最终都过 _send 分发器"""
    import inspect
    funcs = ["push_pre_market", "push_pick", "push_risk_explain",
             "push_empty_day", "push_evening", "push_post_market",
             "push_weekly", "push_anomaly"]
    for name in funcs:
        fn = getattr(ps, name, None)
        assert fn is not None, f"missing {name}"
        src = inspect.getsource(fn)
        assert "_send(" in src, f"{name} 没调 _send()"
        assert "_send_webhook(" not in src, f"{name} 还在直接调 _send_webhook"
    print(f"  ✓ test_push_functions_use_dispatcher: 8 个 push 全走 _send")


# ─────────── runner ───────────

if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n✗ {failed} tests failed")
        sys.exit(1)
    print(f"\n✓ {len(tests)} tests passed")
