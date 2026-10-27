from datetime import date, datetime, time, timedelta, timezone
import html
import io
import math

import pandas as pd
from flask import Blueprint, abort, request, send_file, render_template
from pymongo.errors import PyMongoError
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle
from reportlab.lib.styles import getSampleStyleSheet

from authz import admin_required
from forecasting import forecast_sales
from models import product_info_collection, sales_collection

reports_bp = Blueprint("reports", __name__)


def number(value):
    if value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid financial value.") from exc
    if not math.isfinite(result):
        raise ValueError("Non-finite financial value.")
    return result


def spreadsheet_safe(value):
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@reports_bp.get("/reports")
@admin_required
def view_reports():
    try:
        start, end = _date_filter()
    except ValueError:
        abort(400, description="Choose a valid report date range of no more than one year.")
    return render_template("reports.html", start_date=start.isoformat() if start else "",
                           end_date=end.isoformat() if end else "")


def _date_filter():
    start = date.fromisoformat(request.args["start"]) if request.args.get("start") else None
    end = date.fromisoformat(request.args["end"]) if request.args.get("end") else None
    if start and end and (end < start or (end-start).days > 366):
        raise ValueError("Invalid date range")
    return start, end


def report_frame(kind, start=None, end=None):
    if kind in {"daily", "weekly", "monthly"}:
        query = {}
        date_clause = {}
        if start: date_clause["$gte"] = datetime.combine(start, time.min, timezone.utc)
        if end: date_clause["$lt"] = datetime.combine(end+timedelta(days=1), time.min, timezone.utc)
        if date_clause: query["sale_date"] = date_clause
        groups = {}
        for row in sales_collection.find(query, {"sale_date": 1, "total_revenue": 1, "discount_applied": 1, "profit_per_sale": 1}):
            raw_date = row.get("sale_date")
            try:
                when = raw_date if isinstance(raw_date, datetime) else datetime.fromisoformat(str(raw_date))
                if when.tzinfo is None: when = when.replace(tzinfo=timezone.utc)
                day = when.astimezone(timezone.utc).date()
            except (TypeError, ValueError):
                continue
            if kind == "daily": key = day.isoformat()
            elif kind == "weekly": key = (day-timedelta(days=day.weekday())).isoformat()
            else: key = day.strftime("%Y-%m")
            group = groups.setdefault(key, {"period_start": key, "sales_count": 0, "revenue": 0.0, "discount": 0.0, "profit": 0.0})
            group["sales_count"] += 1
            if row.get("total_revenue") is None or row.get("profit_per_sale") is None:
                raise ValueError("Missing financial fields.")
            group["revenue"] += number(row.get("total_revenue"))
            group["discount"] += number(row.get("discount_applied"))
            group["profit"] += number(row.get("profit_per_sale"))
        return pd.DataFrame(sorted(groups.values(), key=lambda group: group["period_start"]))
    if kind in {"sales", "pnl"}:
        query = {}
        date_clause = {}
        if start:
            date_clause["$gte"] = datetime.combine(start, time.min, timezone.utc)
        if end:
            date_clause["$lt"] = datetime.combine(end + timedelta(days=1), time.min, timezone.utc)
        if date_clause:
            query["sale_date"] = date_clause
        rows = list(sales_collection.find(query).sort("sale_date", -1))
        if kind == "pnl":
            required = ("total_revenue", "discount_applied", "profit_per_sale")
            if any(any(row.get(field) is None for field in required) for row in rows):
                raise ValueError("Missing financial fields.")
            return pd.DataFrame([{
                "sales_count": len(rows),
                "revenue_total": sum(number(x.get("total_revenue")) for x in rows),
                "discount_total": sum(number(x.get("discount_applied")) for x in rows),
                "profit_total": sum(number(x.get("profit_per_sale")) for x in rows),
            }])
        return pd.DataFrame(rows)
    if kind == "inventory":
        return pd.DataFrame(list(product_info_collection.find({"is_archived": {"$ne": True}})))
    if kind == "low-stock":
        return pd.DataFrame(list(product_info_collection.find({"is_archived": {"$ne": True}, "$expr": {"$and": [
            {"$gt": ["$available_quantity", 0]},
            {"$lte": ["$available_quantity", {"$ifNull": ["$reorder_level", 5]}]},
        ]}})))
    if kind == "products":
        query = {}
        date_clause = {}
        if start: date_clause["$gte"] = datetime.combine(start, time.min, timezone.utc)
        if end: date_clause["$lt"] = datetime.combine(end+timedelta(days=1), time.min, timezone.utc)
        if date_clause: query["sale_date"] = date_clause
        groups = {}
        for row in sales_collection.find(query, {"product_id": 1, "product_name": 1, "quantity_sold": 1,
                                                 "total_revenue": 1, "sale_date": 1}):
            key = str(row.get("product_id") or row.get("product_name") or "Unknown product")
            group = groups.setdefault(key, {"product_id": key, "product_name": row.get("product_name") or "Unknown product",
                                            "units_sold": 0, "revenue": 0.0})
            quantity = number(row.get("quantity_sold"))
            if row.get("total_revenue") is None:
                raise ValueError("Missing revenue field.")
            if quantity is None or quantity < 0 or int(quantity) != quantity:
                raise ValueError("Invalid product sales quantity.")
            group["units_sold"] += int(quantity)
            group["revenue"] += number(row.get("total_revenue"))
        return pd.DataFrame(sorted(groups.values(), key=lambda row: row["revenue"], reverse=True))
    if kind == "ai":
        rows = list(sales_collection.find({}, {"sale_date": 1, "total_revenue": 1, "profit_per_sale": 1}))
        forecast = forecast_sales(rows)
        return pd.DataFrame([forecast])
    abort(400, description="Choose a supported sales, financial, product, inventory, or forecast report.")


@reports_bp.get("/reports/export")
@admin_required
def export_report():
    kind = request.args.get("type", "")
    export_format = request.args.get("format", "")
    if export_format not in {"excel", "pdf", "csv"}:
        abort(400, description="Choose PDF, Excel, or CSV format.")
    try:
        start, end = _date_filter()
        df = report_frame(kind, start, end)
    except PyMongoError:
        abort(503)
    except ValueError:
        abort(400, description="Financial data contains invalid values; no totals were generated.")
    if df.empty:
        abort(400, description="No data is available for this report.")
    if "_id" in df.columns:
        df["_id"] = df["_id"].astype(str)
    for column in df.columns:
        df[column] = df[column].map(lambda value: value.isoformat() if isinstance(value, datetime) else value)
    filename = f"{kind}_report_{datetime.now(timezone.utc):%Y%m%d}"
    if export_format == "csv":
        for column in df.columns:
            df[column] = df[column].map(spreadsheet_safe)
        output = io.BytesIO(df.to_csv(index=False).encode("utf-8-sig"))
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=f"{filename}.csv", mimetype="text/csv; charset=utf-8")
    if export_format == "excel":
        for column in df.columns:
            df[column] = df[column].map(spreadsheet_safe)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name=kind[:31].capitalize())
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=f"{filename}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), repeatRows=1)
    styles = getSampleStyleSheet()
    story = [Paragraph(f"E-Commerce {html.escape(kind.upper())} Report", styles["Title"]),
             Paragraph(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}", styles["Normal"]), Spacer(1, 12)]
    columns = list(df.columns[:8])
    cells = [[Paragraph(html.escape(str(c)), styles["BodyText"]) for c in columns]]
    for row in df[columns].itertuples(index=False, name=None):
        cells.append([Paragraph(html.escape(str(value)[:250]), styles["BodyText"]) for value in row])
    table = LongTable(cells, repeatRows=1, splitByRow=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0a192f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(table)
    doc.build(story)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"{filename}.pdf", mimetype="application/pdf")
