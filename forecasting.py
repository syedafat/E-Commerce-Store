"""Small, transparent baseline forecast using dated weekly sales totals."""
from datetime import datetime, timezone
import math

import pandas as pd


def forecast_sales(records):
    points = []
    skipped_records = 0
    for row in records:
        raw_date = row.get("sale_date")
        try:
            if row.get("total_revenue") is None or row.get("profit_per_sale") is None:
                raise ValueError("missing financial fields")
            date = raw_date if isinstance(raw_date, datetime) else datetime.fromisoformat(str(raw_date))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            date = date.astimezone(timezone.utc)
            revenue = float(row.get("total_revenue", 0) or 0)
            profit = float(row.get("profit_per_sale", 0) or 0)
            if not math.isfinite(revenue) or not math.isfinite(profit):
                skipped_records += 1
                continue
            points.append((date, revenue, profit, 1))
        except (TypeError, ValueError):
            skipped_records += 1
            continue
    if not points:
        return {"available": False, "message": "Forecast unavailable: no valid dated sales records."}
    df = pd.DataFrame(points, columns=["date", "revenue", "profit", "transactions"])
    weekly = df.set_index("date")[["revenue", "profit", "transactions"]].resample("W-MON").sum().asfreq("W-MON", fill_value=0)
    if len(weekly) < 8:
        return {"available": False, "message": "At least eight weeks of dated sales history are needed for this baseline."}
    active_weeks = int((weekly["transactions"] > 0).sum())
    recent = weekly.tail(4)
    if active_weeks < 4 or not (recent["transactions"] > 0).any():
        return {"available": False, "message": "Forecast unavailable: at least four sales weeks overall and one sales week in the most recent four weeks are needed."}
    if not all(math.isfinite(float(value)) for value in weekly["revenue"].tolist() + weekly["profit"].tolist()):
        return {"available": False, "message": "Forecast unavailable: aggregated sales values exceed the supported numeric range."}
    actual = weekly["revenue"].tolist()
    errors = [abs(actual[i] - sum(actual[i-4:i]) / 4) for i in range(4, len(actual))]
    revenue_week = float(recent["revenue"].mean())
    profit_week = float(recent["profit"].mean())
    forecast_revenue = revenue_week * 4
    forecast_profit = profit_week * 4
    mae = sum(errors) / len(errors) if errors else None
    if not math.isfinite(forecast_revenue) or not math.isfinite(forecast_profit) or mae is not None and not math.isfinite(mae):
        return {"available": False, "message": "Forecast unavailable: calculated values exceed the supported numeric range."}
    history_values = [{"date": index.date().isoformat(), "revenue": round(float(row["revenue"]), 2)}
                      for index, row in weekly.iterrows()]
    next_week = weekly.index[-1] + pd.Timedelta(days=7)
    forecast_values = [{"date": (next_week + pd.Timedelta(days=7 * index)).date().isoformat(),
                        "revenue": round(revenue_week, 2)} for index in range(4)]
    return {
        "available": True,
        "horizon": "next 4 weeks",
        "revenue": round(forecast_revenue, 2),
        "profit": round(forecast_profit, 2),
        "mae": round(mae, 2) if mae is not None else None,
        "weeks": len(weekly),
        "as_of": max(point[0] for point in points).date().isoformat(),
        "skipped_records": skipped_records,
        "method": "Mean of the most recent four weekly totals",
        "history": history_values[-16:],
        "forecast_series": forecast_values,
    }
