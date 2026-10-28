from datetime import datetime, timedelta
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

app = FastAPI(
    title="Weather Route API",
    description="Kontrola dažďa na trase pomocou TomTom a Open-Meteo",
)

TOMTOM_API_KEY = "GxaDsxeJlBjhC5VZvYorbO3JA2mRzIg5"


def get_route_and_weather(start_query: str, end_query: str, depart_time: datetime):
    # 1. Geokódovanie štartu - Úplne statická URL adresa, parametre idú oddelene
    geo_url = "https://tomtom.com"
    
    start_res = requests.get(
        f"{geo_url}{start_query}.json", 
        params={"key": TOMTOM_API_KEY, "limit": 1}
    ).json()

    # 2. Geokódovanie cieľa
    end_res = requests.get(
        f"{geo_url}{end_query}.json", 
        params={"key": TOMTOM_API_KEY, "limit": 1}
    ).json()

    # Kontrola výsledkov vyhľadávania
    if not start_res.get("results") or not end_res.get("results"):
        raise HTTPException(
            status_code=404, detail="Nepodarilo sa nájsť zadané adresy štartu alebo cieľa."
        )

    # Vytiahnutie prvého nájdeného prvku zo zoznamu [0]
    start_data = start_res["results"][0]
    end_data = end_res["results"][0]

    start_pos = start_data["position"]
    end_pos = end_data["position"]

    start_coords = f"{start_pos['lat']},{start_pos['lon']}"
    end_coords = f"{end_pos['lat']},{end_pos['lon']}"

    # 3. Výpočet trasy cez TomTom Routing API
    depart_str = depart_time.strftime("%Y-%m-%dT%H:%M:%S")
    route_base_url = f"https://tomtom.com{start_coords}:{end_coords}/json"
    
    route_params = {
        "key": TOMTOM_API_KEY,
        "departAt": depart_str,
        "routeType": "fastest",
        "travelMode": "car"
    }
    
    route_res = requests.get(route_base_url, params=route_params).json()

    if "routes" not in route_res or not route_res["routes"]:
        raise HTTPException(status_code=400, detail="TomTom nedokázal naplánovať trasu.")

    legs = route_res["routes"][0]["legs"][0]
    points = legs["points"]
    total_duration = legs["summary"]["travelTimeInSeconds"]

    # Výber kontrolných bodov na trase (približne 8 bodov na celú trasu)
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
        meteo_url = (
            f"https://open-meteo.com{lat}&longitude={lon}"
            f"&hourly=precipitation,precipitation_probability&timezone=auto"
        )
        m_res = requests.get(meteo_url).json()

        rain_mm = 0.0
        rain_prob = 0
        if "hourly" in m_res:
            target_hour_str = eta.strftime("%Y-%m-%dT%H:00")
            try:
                idx = m_res["hourly"]["time"].index(target_hour_str)
                rain_mm = m_res["hourly"]["precipitation"][idx]
                rain_prob = m_res["hourly"]["precipitation_probability"][idx]
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
                input, button { display: block; width: 100%; margin: 10px 0; padding: 10px; border: 1px solid #ccc; border-radius: 4px; }
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
async def check_route(
    start: str, end: str, delay_minutes: int = Query(0, ge=0)
):
    try:
        depart_time = datetime.now() + timedelta(minutes=delay_minutes)
        data = get_route_and_weather(start, end, depart_time)

        rows = ""
        for r in data["report"]:
            status_color = "#dc3545" if r["danger"] else "#28a745"
            status_text = f"⚠️ Hrozí dážď ({r['rain_mm']} mm)" if r["danger"] else "✅ Sucho"
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
    except Exception as e:
        return f"<html><body><h2>Chyba na serveri:</h2><p>{str(e)}</p><a href='/'>Späť</a></body></html>"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
