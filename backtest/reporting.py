"""Reproducible backtest exports. Never include account or Telegram secrets."""
import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path


def _json_safe(value):
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def export_run(directory, engine, result, frames, save_bars=False):
    """New directory only, with fingerprints of the exact input frames."""
    output = Path(directory)
    output.mkdir(parents=True, exist_ok=False)
    datasets = {}
    for name, frame in frames.items():
        if frame is None or frame.empty:
            continue
        csv = frame.to_csv(index_label="time").encode("utf-8")
        datasets[name] = dict(rows=len(frame), start=str(frame.index[0]),
                              end=str(frame.index[-1]), sha256=hashlib.sha256(csv).hexdigest())
        if save_bars:
            (output / f"{name}.csv").write_bytes(csv)
    manifest = dict(strategy=engine.strategy.name, strategy_params=engine.strategy.params,
                    risk=asdict(engine.risk.config), backtest=asdict(engine.cfg),
                    symbol_spec=asdict(engine.spec), datasets=datasets,
                    initial_balance=result.initial_balance, final_balance=result.final_balance,
                    metrics=result.metrics,
                    limitations=["Drawdown is based on closed-trade balance, not intratrade equity.",
                                 "OHLC simulation; no tick-level execution or swap model.",
                                 "Backtest daily loss/trade caps are not simulated.",
                                 "Non-finite metrics are stored as null."])
    news = Path(engine.cfg.news_file_path)
    if news.is_file():
        manifest["news_sha256"] = hashlib.sha256(news.read_bytes()).hexdigest()
    (output / "report.json").write_text(json.dumps(_json_safe(manifest), ensure_ascii=False,
                                                   indent=2, allow_nan=False), encoding="utf-8")
    engine.trades_to_frame(result).to_csv(output / "trades.csv", index=False)
    (output / "summary.txt").write_text(result.to_text(), encoding="utf-8")
    return output
