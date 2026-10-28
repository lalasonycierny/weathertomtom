import os
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

app = FastAPI(
    title="Weather Route API",
    description="Kontrola dažďa na trase pomocou TomTom a Open-Meteo",
)

# API kľúč sa číta z premennej prostredia (nikdy ho nedávaj priamo do kódu)
TOMTOM_API_KEY2 = os.environ.get("TOMTOM_API_KEY2", "")

GEO_URL = "https://api.tomtom.com/search/2/geocode"
ROUTE_URL = "https://api.tomtom.com/routing/1/calculateRoute"
METEO_URL = "https://api.open-meteo.com/v1/forecast"


def geocode(query: str):
    res = requests.get(
        f"{GEO_URL}/{quote(query)}.json",
        params={"key": TOMTOM_API_KEY2, "limit": 1},
        timeout=10,
    ).json()
    if not res.get("results"):
        raise HTTPException(status_code=404, detail=f"Adresa sa nenašla: {query}")
    return res["results"][0]


def get_route_and_weather(start_query: str, end_query: str, depart_time: datetime):
    if not TOMTOM_API_KEY2:
        raise HTTPException(
            status_code=500,
            detail="Chýba premenná prostredia TOMTOM_API_KEY2.",
        )

    # 1. Geokódovanie štartu a cieľa
    start_data = geocode(start_query)
    end_data = geocode(end_query)

    start_pos = start_data["position"]
    end_pos = end_data["position"]

    start_coords = f"{start_pos['lat']},{start_pos['lon']}"
    end_coords = f"{end_pos['lat']},{end_pos['lon']}"

    # 2. Výpočet trasy cez TomTom Routing API
    route_res = requests.get(
        f"{ROUTE_URL}/{start_coords}:{end_coords}/json",
        params={
            "key": TOMTOM_API_KEY2,
            "departAt": depart_time.strftime("%Y-%m-%dT%H:%M:%S"),
            "routeType": "fastest",
            "travelMode": "car",
        },
        timeout=20,
    ).json()

    if not route_res.get("routes"):
        raise HTTPException(
            status_code=400, detail=f"TomTom nedokázal naplánovať trasu: {route_res}"
        )

    leg = route_res["routes"][0]["legs"][0]
    points = leg["points"]
    total_duration = leg["summary"]["travelTimeInSeconds"]

    # 3. Výber kontrolných bodov na trase (približne 8 bodov na celú trasu)
    step = max(1, len(points) // 8)
    sampled_points = []

    for i in range(0, len(points), step):
        pt = points[i]
        progress = i / len(points)
        point_eta = depart_time + timedelta(seconds=int(total_duration * progress))
        sampled_points.append(
            {"lat": pt["latitude"], "lon": pt["longitude"], "eta": point_eta}
        )

    # Pridanie finálneho cieľa cesty
    final_eta = depart_time + timedelta(seconds=total_duration)
    sampled_points.append(
        {"lat": end_pos["lat"], "lon": end_pos["lon"], "eta": final_eta}
    )

    # 4. Kontrola počasia cez Open-Meteo API pre každý bod cesty
    route_report = []
    for pt in sampled_points:
        lat, lon, eta = pt["lat"], pt["lon"], pt["eta"]

        m_res = requests.get(
            METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "precipitation,precipitation_probability",
                "timezone": "auto",
                "forecast_days": 3,
            },
            timeout=10,
        ).json()

        rain_mm = 0.0
        rain_prob = 0
        if "hourly" in m_res:
            target_hour_str = eta.strftime("%Y-%m-%dT%H:00")
            try:
                idx = m_res["hourly"]["time"].index(target_hour_str)
                rain_mm = m_res["hourly"]["precipitation"][idx] or 0.0
                rain_prob = m_res["hourly"]["precipitation_probability"][idx] or 0
            except ValueError:
                pass

        route_report.append(
            {
                "time": eta.strftime("%H:%M"),
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "rain_mm": rain_mm,
                "rain_prob": rain_prob,
                "danger": rain_mm > 0 or rain_prob > 20,
            }
        )

    return {
        "start": start_data["address"].get("freeformAddress", start_query),
        "end": end_data["address"].get("freeformAddress", end_query),
        "duration": str(timedelta(seconds=total_duration)),
        "report": route_report,
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    """Základná HTML stránka pre otestovanie aplikácie priamo v prehliadači."""
    return """
    <html>
        <head>
            <title>Meteo Trasa</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 40px; background: #f4f4f9; }
                form { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 400px; }
                input, button { display: block; width: 100%; margin: 10px 0; padding: 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
                button { background: #007bff; color: white; border: none; cursor: pointer; }
                button:hover { background: #0056b3; }
            </style>
        </head>
        <body>
            <h2>Plánovač trasy s kontrolou dažďa</h2>
            <form action="/check" method="get">
                <label>Odkiaľ:</label>
                <input type="text" name="start" value="Bratislava" required>
                <label>Kam:</label>
                <input type="text" name="end" value="Košice" required>
                <label>Čas odchodu (minút odteraz):</label>
                <input type="number" name="delay_minutes" value="0" min="0">
                <button type="submit">Skontrolovať trasu</button>
            </form>
        </body>
    </html>
    """


@app.get("/check", response_class=HTMLResponse)
async def check_route(start: str, end: str, delay_minutes: int = Query(0, ge=0)):
    try:
        depart_time = datetime.now() + timedelta(minutes=delay_minutes)
        data = get_route_and_weather(start, end, depart_time)

        rows = ""
        for r in data["report"]:
            status_color = "#dc3545" if r["danger"] else "#28a745"
            status_text = (
                f"⚠️ Hrozí dážď ({r['rain_mm']} mm)" if r["danger"] else "✅ Sucho"
            )
            rows += f"""
            <tr>
                <td style="padding:8px; border:1px solid #ddd;">{r['time']}</td>
                <td style="padding:8px; border:1px solid #ddd;">{r['lat']}, {r['lon']}</td>
                <td style="padding:8px; border:1px solid #ddd;">{r['rain_prob']}%</td>
                <td style="padding:8px; border:1px solid #ddd; color:{status_color}; font-weight:bold;">{status_text}</td>
            </tr>
            """

        return f"""
        <html>
            <head><title>Výsledok kontroly</title></head>
            <body style="font-family: Arial, sans-serif; margin: 40px;">
                <h2>Analýza trasy: {data['start']} → {data['end']}</h2>
                <p><strong>Celkový čas jazdy:</strong> {data['duration']}</p>
                <table style="width: 100%; border-collapse: collapse; margin-top: 20px;">
                    <tr style="background-color: #f2f2f2;">
                        <th style="padding:8px; border:1px solid #ddd; text-align:left;">Čas prejazdu</th>
                        <th style="padding:8px; border:1px solid #ddd; text-align:left;">GPS Súradnice</th>
                        <th style="padding:8px; border:1px solid #ddd; text-align:left;">Pravdepodobnosť zrážok</th>
                        <th style="padding:8px; border:1px solid #ddd; text-align:left;">Stav</th>
                    </tr>
                    {rows}
                </table>
                <br><a href="/">Späť na vyhľadávanie</a>
            </body>
        </html>
        """
    except HTTPException as e:
        return f"<html><body><h2>Chyba:</h2><p>{e.detail}</p><a href='/'>Späť</a></body></html>"
    except Exception as e:
        return f"<html><body><h2>Chyba na serveri:</h2><p>{str(e)}</p><a href='/'>Späť</a></body></html>"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
