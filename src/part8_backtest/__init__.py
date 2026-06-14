"""Part 8 — Backtester (standalone).

Replays HISTORICAL klines through the SAME strategy / regime / portfolio / risk
stack the live bot uses, fills via ``MockBroker``, and produces a performance
report. Fully independent of ``main.py`` and the live monitor:

    python -m src.part8_backtest --symbol BTC/USDT --days 90      # CLI report
    streamlit run src/part8_backtest/app.py                       # web page

The event-driven design makes this almost free: a backtest is just the live
chain with the data source swapped for a bar-by-bar replay and the broker
swapped for an instant-fill mock.
"""
