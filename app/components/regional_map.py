from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from app.components.charts import dark_chart_layout


def _append_geometry_lines(geometry: dict, lon: list[float | None], lat: list[float | None]) -> None:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        polygons = [coordinates]
    elif geometry_type == "MultiPolygon":
        polygons = coordinates
    else:
        return
    if not isinstance(polygons, list):
        return
    for polygon in polygons:
        if not isinstance(polygon, list):
            continue
        for ring in polygon[:1]:
            if not isinstance(ring, list):
                continue
            for point in ring:
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    lon.append(float(point[0]))
                    lat.append(float(point[1]))
            lon.append(None)
            lat.append(None)


def _department_boundary_trace(department_geojson: dict) -> go.Scattergeo | None:
    lon: list[float | None] = []
    lat: list[float | None] = []
    for feature in department_geojson.get("features", []):
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if isinstance(geometry, dict):
            _append_geometry_lines(geometry, lon, lat)
    if not lon:
        return None
    return go.Scattergeo(
        lon=lon,
        lat=lat,
        mode="lines",
        line=dict(color="rgba(226,232,240,.45)", width=0.45),
        hoverinfo="skip",
        showlegend=False,
    )


def regional_demand_choropleth(
    frame: pd.DataFrame,
    geojson: dict,
    department_geojson: dict | None = None,
) -> go.Figure:
    hover = frame.assign(
        demand_label=frame["consumption_mw"].map(lambda value: f"{value:,.0f} MW"),
        renewable_label=frame["renewable_share"].map(lambda value: f"{value:.0%}"),
        production_label=frame["total_production_mw"].map(lambda value: f"{value:,.0f} MW"),
    )
    fig = go.Figure(
        go.Choropleth(
            geojson=geojson,
            locations=hover["region_code"],
            z=hover["demand_pressure"],
            featureidkey="properties.code",
            colorscale=[
                [0.0, "#38bdf8"],
                [0.35, "#22c55e"],
                [0.62, "#facc15"],
                [0.82, "#f97316"],
                [1.0, "#ef4444"],
            ],
            marker_line_color="rgba(219,234,254,.72)",
            marker_line_width=0.7,
            colorbar=dict(
                title="Pressure",
                tickformat=".0%",
                outlinewidth=0,
                thickness=12,
                len=0.72,
            ),
            customdata=hover[
                ["region_display", "demand_label", "production_label", "renewable_label"]
            ],
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "Demand: %{customdata[1]}<br>"
                "Production: %{customdata[2]}<br>"
                "Renewable share: %{customdata[3]}<extra></extra>"
            ),
        )
    )
    if department_geojson:
        boundary_trace = _department_boundary_trace(department_geojson)
        if boundary_trace is not None:
            fig.add_trace(boundary_trace)
    fig.update_geos(
        scope="europe",
        fitbounds="locations",
        visible=False,
        bgcolor="rgba(0,0,0,0)",
        projection_type="mercator",
    )
    fig.update_layout(
        **dark_chart_layout(
            height=610,
            margin=dict(l=0, r=0, t=12, b=0),
            geo=dict(
                bgcolor="rgba(0,0,0,0)",
                lakecolor="rgba(14, 165, 233, .18)",
                landcolor="rgba(15, 28, 44, .92)",
            ),
        )
    )
    return fig
