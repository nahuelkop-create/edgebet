import os
from datetime import datetime
from flask import Flask, render_template

from services.database import get_connection

app = Flask(__name__)


@app.route("/")
def dashboard():
    conn = get_connection()
    bets = conn.execute("SELECT * FROM bets ORDER BY created_at DESC").fetchall()
    conn.close()

    month = datetime.utcnow().strftime("%Y-%m")
    return render_template("dashboard.html", bets=bets, month=month)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
