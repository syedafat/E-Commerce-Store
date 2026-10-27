from flask import Blueprint, abort, render_template
from pymongo.errors import PyMongoError

from authz import admin_required
from forecasting import forecast_sales
from models import sales_collection

prediction_bp = Blueprint("prediction", __name__)


@prediction_bp.get("/realtime_prediction")
@admin_required
def predict_pnl():
    try:
        sales = list(sales_collection.find({}, {"sale_date": 1, "total_revenue": 1, "profit_per_sale": 1}))
    except PyMongoError:
        abort(503)
    forecast = forecast_sales(sales)
    return render_template("prediction.html", forecast=forecast, data_insufficient=not forecast.get("available"))
