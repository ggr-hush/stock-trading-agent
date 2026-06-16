# docs/raw/felix-quant/

**参考仓库**: https://github.com/XFX-939/felix-quant (XFX-939 的量化后端)

借鉴分析:
- `stock_trading_agent/engine/market_regime.py` ← `backend/app/services/classic_quant.py:market_regime_model`
- `stock_trading_agent/engine/reviews.py` ← `backend/app/services/review_service.py`
- `stock_trading_agent/engine/decision_engine.py` ← `backend/app/services/decision_engine.py:build_daily_decision`

**这是参考源码, 不是项目代码**, 删之前请确认借鉴点都迁移完成。
