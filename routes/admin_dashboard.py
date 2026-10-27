from datetime import date, datetime, time, timedelta, timezone

from flask import Blueprint, abort, render_template, request
from pymongo.errors import PyMongoError

from authz import admin_required
from forecasting import forecast_sales
from models import product_info_collection, sales_collection

admin_bp = Blueprint("admin_dashboard", __name__)


def number(value):
    if value is None:
        return None
    try:
        value = float(value)
        return value if value == value and abs(value) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


@admin_bp.get("/admin")
@admin_required
def admin_dashboard():
    today = datetime.now(timezone.utc).date()
    try:
        end_date = date.fromisoformat(request.args.get("end", today.isoformat()))
        start_date = date.fromisoformat(request.args.get("start", (today - timedelta(days=29)).isoformat()))
        if end_date < start_date or (end_date - start_date).days > 366:
            raise ValueError
    except ValueError:
        abort(400, description="Choose a valid date range of no more than one year.")
    start_at = datetime.combine(start_date, time.min, timezone.utc)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, timezone.utc)
    period_query = {"sale_date": {"$gte": start_at, "$lt": end_at}}
    try:
        active_products = {"is_archived": {"$ne": True}}
        product_count = product_info_collection.count_documents(active_products)
        products = product_info_collection.find(active_products, {"available_quantity": 1, "reorder_level": 1})
        sales_count = sales_collection.count_documents({})
        period_sales_count = sales_collection.count_documents(period_query)
        sales = sales_collection.find(period_query, {"total_revenue": 1, "profit_per_sale": 1, "sale_date": 1,
                                                       "product_id": 1, "product_name": 1, "quantity_sold": 1}).sort("sale_date", -1)
        recent = sales_collection.find({}, {"product_name": 1, "quantity_sold": 1, "total_revenue": 1, "sale_date": 1}).sort("sale_date", -1).limit(8)
        # Baseline uses a bounded history window to keep dashboard memory use predictable.
        history = list(sales_collection.find({"sale_date": {"$type": "date", "$lt": end_at}},
                                             {"total_revenue": 1, "profit_per_sale": 1, "sale_date": 1}).sort("sale_date", -1).limit(5000))
    except PyMongoError:
        abort(503)

    revenue = profit = loss = 0.0
    invalid_financial_records = 0
    by_day, best = {}, {}
    for sale in sales:
        sale_revenue, sale_profit = number(sale.get("total_revenue")), number(sale.get("profit_per_sale"))
        if sale_revenue is None or sale_profit is None:
            invalid_financial_records += 1
        if sale_revenue is not None:
            revenue += sale_revenue
        if sale_profit is not None:
            if sale_profit >= 0:
                profit += sale_profit
            else:
                loss += abs(sale_profit)
        raw_date = sale.get("sale_date")
        if isinstance(raw_date, datetime):
            day = raw_date.astimezone(timezone.utc).date().isoformat() if raw_date.tzinfo else raw_date.date().isoformat()
            by_day[day] = by_day.get(day, 0) + (sale_revenue or 0)
        name = sale.get("product_name") or "Unknown product"
        best[name] = best.get(name, 0) + max(0, int(number(sale.get("quantity_sold")) or 0))

    low_stock = out_of_stock = invalid_stock_records = inventory_units = 0
    for product in products:
        stock = number(product.get("available_quantity", 0))
        reorder = number(product.get("reorder_level", 5))
        if stock is None:
            invalid_stock_records += 1
            stock = 0
        stock = max(0, int(stock))
        inventory_units += stock
        reorder = max(0, int(reorder or 0))
        low_stock += int(0 < stock <= reorder)
        out_of_stock += int(stock <= 0)

    trend = [{"date": (start_date + timedelta(days=i)).isoformat(),
              "revenue": round(by_day.get((start_date + timedelta(days=i)).isoformat(), 0), 2)}
             for i in range((end_date - start_date).days + 1)]
    max_revenue = max((point["revenue"] for point in trend), default=0) or 1
    chart_points = [(42 + (653 * index / max(len(trend)-1, 1)),
                     220 - (point["revenue"] / max_revenue * 180))
                    for index, point in enumerate(trend)]
    forecast = forecast_sales(history)
    return render_template(
        "admin_dashboard.html", total_products=product_count, total_sales=sales_count,
        total_revenue=revenue, total_profit=profit, total_loss=loss,
        period_sales=period_sales_count, inventory_units=inventory_units,
        range_revenue=revenue, start_date=start_date.isoformat(), end_date=end_date.isoformat(),
        recent_sales=list(recent), best_sellers=sorted(best.items(), key=lambda item: item[1], reverse=True)[:5],
        trend=trend, chart_points=chart_points, low_stock_count=low_stock,
        out_of_stock_count=out_of_stock, invalid_financial_records=invalid_financial_records,
        invalid_stock_records=invalid_stock_records,
        ai_prediction=(f"Rs. {forecast['profit']:.2f}" if forecast.get("available") else forecast["message"]),
        forecast=forecast,
    )
