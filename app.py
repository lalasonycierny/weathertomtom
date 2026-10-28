import html
import json
import os
from datetime import datetime, timedelta
from urllib.parse import quote

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

import requests
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

app = FastAPI(
    title="Weather Route API",
    description="Kontrola dažďa na trase pomocou TomTom a Open-Meteo",
)

# API kľúč sa číta z premennej prostredia (nikdy ho nedávaj priamo do kódu)
TOMTOM_API_KEY2 = os.environ.get("TOMTOM_API_KEY2", "")

GEO_URL = "https://api.tomtom.com/search/2/geocode"
ROUTE_URL = "https://api.tomtom.com/routing/1/calculateRoute"
TILE_URL = "https://api.tomtom.com/map/1/tile/basic/main"
METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Časová zóna, v ktorej sa zadáva odchod a zobrazujú časy (Codespace beží v UTC)
APP_TZ_NAME = os.environ.get("APP_TIMEZONE", "Europe/Bratislava")
try:
    APP_TZ = ZoneInfo(APP_TZ_NAME) if ZoneInfo else None
except Exception:
    APP_TZ = None


def now_local() -> datetime:
    return datetime.now(APP_TZ) if APP_TZ else datetime.now()


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

    # Celá trasa pre vykreslenie na mape
    route_line = [[round(p["latitude"], 5), round(p["longitude"], 5)] for p in points]

    # 3. Výber kontrolných bodov na trase (približne 8 bodov na celú trasu)
    step = max(1, len(points) // 8)
    sampled_points = []
    last_idx = 0

    for i in range(0, len(points), step):
        pt = points[i]
        progress = i / len(points)
        point_eta = depart_time + timedelta(seconds=int(total_duration * progress))
        sampled_points.append(
            {"lat": pt["latitude"], "lon": pt["longitude"], "eta": point_eta}
        )
        last_idx = i

    # Ak je posledný vzorkovaný bod tesne pred cieľom, vynechaj ho (duplicita)
    if len(sampled_points) > 1 and (len(points) - 1 - last_idx) < step / 2:
        sampled_points.pop()

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
                "timezone": APP_TZ_NAME if APP_TZ else "auto",
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
                "lat": round(lat, 5),
                "lon": round(lon, 5),
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
        "route": route_line,
    }


# Šablóna mapy (Leaflet + dlaždice TomTom cez náš server, aby kľúč zostal tajný)
MAP_TEMPLATE = """
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<style>
    #map { height: 520px; width: 100%; border-radius: 8px; border: 1px solid #ddd; }
    .pt-label { font-weight: bold; padding: 1px 5px; }
    .legend { margin: 8px 0 0 0; font-size: 14px; }
    .dot { display:inline-block; width:12px; height:12px; border-radius:50%; margin:0 4px 0 12px; vertical-align:middle; }
</style>
<div id="map"></div>
<div class="legend">
    <span class="dot" style="background:#28a745"></span>sucho
    <span class="dot" style="background:#dc3545"></span>hrozí dážď
</div>
<script>
    const routeLine = __ROUTE__;
    const pts = __POINTS__;

    const map = L.map('map');
    L.tileLayer('/tiles/{z}/{x}/{y}.png', {
        maxZoom: 18,
        attribution: '&copy; TomTom'
    }).addTo(map);

    const line = L.polyline(routeLine, {color: '#007bff', weight: 5, opacity: 0.85}).addTo(map);
    map.fitBounds(line.getBounds(), {padding: [30, 30]});

    pts.forEach(function (p) {
        const color = p.danger ? '#dc3545' : '#28a745';
        const marker = L.circleMarker([p.lat, p.lon], {
            radius: 9, color: '#ffffff', weight: 2, fillColor: color, fillOpacity: 1
        }).addTo(map);
        marker.bindTooltip(p.time, {permanent: true, direction: 'top', offset: [0, -8], className: 'pt-label'});
        marker.bindPopup(
            '<b>' + p.time + '</b><br>' +
            'Pravdepodobnosť zrážok: ' + p.rain_prob + '%<br>' +
            'Zrážky: ' + p.rain_mm + ' mm<br>' +
            p.lat + ', ' + p.lon
        );
    });
</script>
"""


def build_map_html(data: dict) -> str:
    def safe_json(obj):
        return json.dumps(obj).replace("</", "<\\/")

    return MAP_TEMPLATE.replace("__ROUTE__", safe_json(data["route"])).replace(
        "__POINTS__", safe_json(data["report"])
    )


@app.get("/tiles/{z}/{x}/{y}.png")
def tiles(z: int, x: int, y: int):
    """Proxy pre dlaždice mapy TomTom, aby API kľúč nebol viditeľný v prehliadači."""
    if not TOMTOM_API_KEY2:
        return Response(status_code=500)
    try:
        r = requests.get(
            f"{TILE_URL}/{z}/{x}/{y}.png",
            params={"key": TOMTOM_API_KEY2},
            timeout=10,
        )
    except requests.RequestException:
        return Response(status_code=502)
    if r.status_code != 200:
        return Response(status_code=r.status_code)
    return Response(
        content=r.content,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


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
        depart_time = now_local() + timedelta(minutes=delay_minutes)
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

        map_html = build_map_html(data)
        start_txt = html.escape(data["start"])
        end_txt = html.escape(data["end"])

        return f"""
        <html>
            <head><title>Výsledok kontroly</title></head>
            <body style="font-family: Arial, sans-serif; margin: 40px;">
                <h2>Analýza trasy: {start_txt} → {end_txt}</h2>
                <p><strong>Celkový čas jazdy:</strong> {data['duration']}
                   &nbsp;|&nbsp; <strong>Odchod:</strong> {depart_time.strftime('%H:%M')}</p>
                {map_html}
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
        return f"<html><body><h2>Chyba:</h2><p>{html.escape(str(e.detail))}</p><a href='/'>Späť</a></body></html>"
    except Exception as e:
        return f"<html><body><h2>Chyba na serveri:</h2><p>{html.escape(str(e))}</p><a href='/'>Späť</a></body></html>"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
