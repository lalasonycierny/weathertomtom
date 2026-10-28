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
            target_hour_str =