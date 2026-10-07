

import streamlit as st
import pandas as pd
import geopandas as gpd
import requests
import re
import pydeck as pdk
import base64

from io import BytesIO
from collections import defaultdict
from shapely.geometry import Point

from pathlib import Path


# =====================================================
# CONFIG
# =====================================================

API_KEY = "89927ce8550f4ce5a2442f681a57ea59"

NDW_FILE = "20260901-tollCollectionNetwork.gpkg"
COUNTRY_FILE = "ne_10m_admin_0_countries.shp"

REPORT_COUNTRIES = {"NL", "BE", "LU", "DE", "FR"}

postcode_cache = {}
country_point_cache = {}

session = requests.Session()

# =====================================================
# background
# =====================================================


def set_background():

    with open("background.webp", "rb") as f:
        data = f.read()

    encoded = base64.b64encode(data).decode()

    st.markdown(
        f"""
        <style>

        .stApp {{
            background:
                linear-gradient(
                    rgba(0,40,80,0.65),
                    rgba(0,40,80,0.65)
                ),
                url("data:image/webp;base64,{data}");
            background-size: cover;
            background-position: center;
            background-repeat: no-repeat;
            background-attachment: fixed;
        }}

        .main .block-container {{
            background: transparent !important;
            padding-top: 1rem;
        }}

        </style>
        """,
        unsafe_allow_html=True
    )


# =====================================================
# DATA LADEN
# =====================================================

@st.cache_resource
def load_reference_data():

    tolwegen = gpd.read_file(NDW_FILE)

    ndw_wegen = set(
        tolwegen["charge_object_name"]
        .dropna()
        .astype(str)
        .str.upper()
    )

    countries = gpd.read_file(COUNTRY_FILE)

    if countries.crs is None:
        countries.set_crs("EPSG:4326", inplace=True)

    countries = countries.to_crs("EPSG:4326")

    countries["ISO_A2"] = (
        countries["ISO_A2"]
        .astype(str)
        .str.upper()
    )

    countries_small = countries[
        countries["ISO_A2"].isin(REPORT_COUNTRIES)
    ].copy()

    countries_sindex = countries_small.sindex

    return (
        ndw_wegen,
        countries_small,
        countries_sindex
    )

NDW_WEGEN, countries_small, countries_sindex = (
    load_reference_data()
)

# =====================================================
# GEOCODING
# =====================================================

def geocode_postcode(country, postcode):

    key = f"{country}_{postcode}"

    if key in postcode_cache:
        return postcode_cache[key]

    url = (
        "https://api.geoapify.com/v1/geocode/search"
        f"?postcode={postcode}"
        f"&country={country}"
        "&limit=1"
        f"&apiKey={API_KEY}"
    )

    r = session.get(url, timeout=30)
    r.raise_for_status()

    features = r.json().get("features", [])

    if not features:
        return None

    lon, lat = features[0]["geometry"]["coordinates"]

    postcode_cache[key] = (lon, lat)

    return lon, lat

# =====================================================
# ROUTING
# =====================================================

def get_route(start_lon, start_lat, end_lon, end_lat):

    url = (
        "https://api.geoapify.com/v1/routing"
        f"?waypoints={start_lat},{start_lon}|{end_lat},{end_lon}"
        "&mode=truck"
        "&format=geojson"
        "&details=route_details"
        f"&apiKey={API_KEY}"
    )

    r = session.get(url, timeout=60)
    r.raise_for_status()

    data = r.json()

    if not data.get("features"):
        return None

    return data["features"][0]

# =====================================================
# ROUTE HELPERS
# =====================================================

def extract_coords(route):

    geometry = route["geometry"]

    coords = geometry["coordinates"]

    if geometry["type"] == "MultiLineString":
        coords = [
            point
            for line in coords
            for point in line
        ]

    return coords

def extract_road_numbers(text):

    if not text:
        return []

    return list(
        set(
            re.findall(
                r"\b[AN]\d+\b",
                str(text).upper()
            )
        )
    )

def local_country_from_point(lon, lat):

    key = f"{round(lat,4)}_{round(lon,4)}"

    if key in country_point_cache:
        return country_point_cache[key]

    point = Point(lon, lat)

    idx = list(
        countries_sindex.query(
            point,
            predicate="intersects"
        )
    )

    if not idx:
        return None

    possible = countries_small.iloc[idx]

    for _, row in possible.iterrows():

        if row.geometry.intersects(point):

            country = row["ISO_A2"]

            country_point_cache[key] = country

            return country

    return None

def get_step_country(step, coords):

    from_idx = step.get("from_index")
    to_idx = step.get("to_index")

    if from_idx is None:
        return None

    mid = int((from_idx + to_idx) / 2)

    if mid >= len(coords):
        return None

    lon, lat = coords[mid]

    return local_country_from_point(lon, lat)

# =====================================================
# ROUTE ANALYSE
# =====================================================

def analyse_route(route, coords):

    country_km = defaultdict(float)

    heffing_km = 0

    roads_detail = defaultdict(float)

    for leg in route["properties"].get("legs", []):

        for step in leg.get("steps", []):

            km = step.get("distance", 0) / 1000

            if km <= 0:
                continue

            country = get_step_country(
                step,
                coords
            )

            if country:
                country_km[country] += km

            if country != "NL":
                continue

            roads = extract_road_numbers(
                step.get("name", "")
            )

            roads = [
                road
                for road in roads
                if road in NDW_WEGEN
            ]

            if not roads:
                continue

            km_part = km / len(roads)

            for road in roads:
                roads_detail[road] += km_part

            heffing_km += km

    return country_km, heffing_km, roads_detail

# =====================================================
# SINGLE ROUTE
# =====================================================

def process_route(
    van_land,
    van_postcode,
    naar_land,
    naar_postcode
):

    start = geocode_postcode(
        van_land,
        van_postcode
    )

    end = geocode_postcode(
        naar_land,
        naar_postcode
    )

    if not start or not end:
        return None

    route = get_route(
        start[0],
        start[1],
        end[0],
        end[1]
    )

    if not route:
        return None

    coords = extract_coords(route)

    total_km = round(
        route["properties"]["distance"] / 1000,
        1
    )

    country_km, heffing_km, roads = analyse_route(
        route,
        coords
    )

    km_nl = round(country_km.get("NL", 0), 1)

    return {
        "Totale KM": total_km,
        "KM_NL": km_nl,
        "KM_BE": round(country_km.get("BE",0),1),
        "KM_DE": round(country_km.get("DE",0),1),
        "KM_FR": round(country_km.get("FR",0),1),
        "KM_LU": round(country_km.get("LU",0),1),
        "NL_HEFFING_KM": round(
            min(heffing_km, km_nl),
            1
        ),
        "NL_HEFFING_DETAIL":
            "; ".join(
                f"{k}={v:.1f}"
                for k,v in roads.items()
            ),
            "RouteCoords": coords
    }

# =====================================================
# STREAMLIT UI
# =====================================================

st.markdown("""
<style>

h1 {
    color: white !important;
    font-size: 2.5rem !important;
    font-weight: 700 !important;
}

h5 {
    color: #94A3B8 !important;
    margin-top: -10px !important;
}


/* Selectbox gebruikt actieve Streamlit-kleuren */
[data-baseweb="select"] > div {
    background-color: var(--background-color) !important;
    color: var(--text-color) !important;
}

/* Tekst */
[data-baseweb="select"] span {
    color: var(--text-color) !important;
}

/* Dropdown */
[role="listbox"] {
    background-color: var(--background-color) !important;
}

[role="option"] {
    background-color: var(--background-color) !important;
    color: var(--text-color) !important;
}

[role="option"]:hover {
    background-color: rgba(128,128,128,0.15) !important;
}

</style>
""", unsafe_allow_html=True)

st.markdown("""
# Europese Afstand & Tolcalculator
##### Transport • Routing • Europese Tolberekening
""")


tab1, tab2 = st.tabs(
    [
        "Enkele Route",
        "Excel Upload"
    ]
)
# =====================================================
# TAB 1
# =====================================================

with tab1:

    col1, col2 = st.columns(2)

    with col1:

        van_land = st.selectbox(
            "Van Land",
            ["NL", "BE", "DE", "FR", "LU"],
            key="van_land_tab1"
        )

        van_postcode = st.text_input(
            "Van Postcode",
            value="6222 NM",
            key="van_postcode_tab1"
        )

    with col2:

        naar_land = st.selectbox(
            "Naar Land",
            ["NL", "BE", "DE", "FR", "LU"],
            key="naar_land_tab1"
        )

        naar_postcode = st.text_input(
            "Naar Postcode",
            value="6413 WJ",
            key="naar_postcode_tab1"
        )

    st.subheader("Voertuig")

    voertuigklasse = st.selectbox(
        "Voertuigklasse",
        [
            "EURO 6+ (>32 ton)",
            "EURO 6 (>32 ton)",
            "EURO 5 (>32 ton)",
            "EURO 4 (>32 ton)"
        ],
        key="voertuigklasse_tab1"
    )

    tarieven = {
        "EURO 6+ (>32 ton)": 0.197,
        "EURO 6 (>32 ton)": 0.201,
        "EURO 5 (>32 ton)": 0.236,
        "EURO 4 (>32 ton)": 0.298
    }

    tarief = tarieven[voertuigklasse]

    if st.button(
        "🚛 Bereken",
        key="bereken_route"
    ):

        result = process_route(
            van_land,
            van_postcode,
            naar_land,
            naar_postcode
        )

        if result is None:

            st.error(
                "Route kon niet worden berekend."
            )

        else:

            niet_heffing = round(
                result["KM_NL"]
                - result["NL_HEFFING_KM"],
                1
            )

            tolkosten = round(
                result["NL_HEFFING_KM"]
                * tarief,
                2
            )

            st.success(
                "Route succesvol berekend."
            )

            col1, col2, col3, col4, col5 = st.columns(5)

            with col1:
                st.metric(
                    "Totale KM",
                    result["Totale KM"]
                )

            with col2:
                st.metric(
                    "NL Heffing KM",
                    result["NL_HEFFING_KM"]
                )

            with col3:
                st.metric(
                    "Niet-Heffing KM",
                    niet_heffing
                )

            with col4:
                st.metric(
                    "Tarief",
                    f"€ {tarief:.3f}"
                )

            with col5:
                st.metric(
                    "Tolkosten",
                    f"€ {tolkosten:.2f}"
                )

            st.divider()

            st.subheader("Landenverdeling")

            landen_df = pd.DataFrame(
                [
                    ["Nederland", result["KM_NL"]],
                    ["België", result["KM_BE"]],
                    ["Duitsland", result["KM_DE"]],
                    ["Frankrijk", result["KM_FR"]],
                    ["Luxemburg", result["KM_LU"]]
                ],
                columns=[
                    "Land",
                    "Kilometers"
                ]
            )

            st.dataframe(
                landen_df,
                use_container_width=True,
                hide_index=True
            )

            st.divider()

            st.subheader(
                "Nederlandse Heffingswegen"
            )

            detail = result.get(
                "NL_HEFFING_DETAIL",
                ""
            )

            if detail:

                st.info(detail)

            else:

                st.info(
                    "Geen heffingswegen gevonden."
                )

            st.divider()

            st.subheader("🗺️ Routekaart")

            route_coords = result.get(
                "RouteCoords",
                []
            )

            if len(route_coords) > 1:
                route_layer = pdk.Layer(
                    "PathLayer",
                    data=[
                        {
                            "path": route_coords
                        }
                    ],
                    get_path="path",
                    get_color=[0, 102, 204],
                    get_width=8,
                    width_min_pixels=4
                )

                markers_df = pd.DataFrame(
                    [
                        {
                            "lon": route_coords[0][0],
                            "lat": route_coords[0][1],
                            "punt": "Start"
                        },
                        {
                            "lon": route_coords[-1][0],
                            "lat": route_coords[-1][1],
                            "punt": "Einde"
                        }
                    ]
                )

                marker_layer = pdk.Layer(
                    "ScatterplotLayer",
                    data=markers_df,
                    get_position=["lon", "lat"],
                    get_fill_color=[255, 50, 50],
                    get_radius=300,  # veel kleiner
                    radius_min_pixels=6,
                    radius_max_pixels=12
                )

                start_lon = route_coords[0][0]
                start_lat = route_coords[0][1]

                end_lon = route_coords[-1][0]
                end_lat = route_coords[-1][1]

                center_lon = (start_lon + end_lon) / 2
                center_lat = (start_lat + end_lat) / 2

                view_state = pdk.ViewState(
                    longitude=center_lon,
                    latitude=center_lat,
                    zoom=9,
                    pitch=0
                )

                st.pydeck_chart(
                    pdk.Deck(
                        layers=[
                            route_layer,
                            marker_layer
                        ],
                        initial_view_state=view_state,
                        map_style="road"
                    )
                )

            st.divider()

            with st.expander(
                "Volledig resultaat weergeven"
            ):
                st.json(result)

# =====================================================
# TAB 2
# =====================================================

with tab2:

    st.subheader("Excel Upload")

    voertuigklasse = st.selectbox(
        "Voertuigklasse",
        [
            "EURO 6+ (>32 ton)",
            "EURO 6 (>32 ton)",
            "EURO 5 (>32 ton)",
            "EURO 4 (>32 ton)"
        ],
        key="voertuigklasse_tab2"
    )

    tarieven = {
        "EURO 6+ (>32 ton)": 0.197,
        "EURO 6 (>32 ton)": 0.201,
        "EURO 5 (>32 ton)": 0.236,
        "EURO 4 (>32 ton)": 0.298
    }

    tarief = tarieven[voertuigklasse]

    st.markdown(
        """
        Upload een Excel-bestand met minimaal de volgende kolommen:

        - Van Land
        - Van Postcode
        - Naar Land
        - Naar Postcode
        """
    )

    # ============================================
    # VOORBEELDBESTAND
    # ============================================

    voorbeeld_df = pd.DataFrame(
        {
            "Van Land": ["NL", "BE"],
            "Van Postcode": ["6222 NM", "3600"],
            "Naar Land": ["DE", "FR"],
            "Naar Postcode": ["50667", "67000"]
        }
    )

    voorbeeld_buffer = BytesIO()

    voorbeeld_df.to_excel(
        voorbeeld_buffer,
        index=False
    )

    st.download_button(
        label="📥 Download Voorbeeld Excel",
        data=voorbeeld_buffer.getvalue(),
        file_name="Voorbeeld_KM_Tol.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    st.subheader("Voorbeeld structuur")

    st.dataframe(
        voorbeeld_df,
        use_container_width=True,
        hide_index=True
    )

    st.divider()

    # ============================================
    # UPLOAD
    # ============================================

    uploaded_file = st.file_uploader(
        "Upload Excel bestand",
        type=["xlsx"]
    )

    if uploaded_file is not None:

        try:

            df = pd.read_excel(uploaded_file)

            st.success(
                f"Bestand geladen ({len(df)} regels)"
            )

            st.subheader("Preview")

            st.dataframe(
                df.head(10),
                use_container_width=True
            )

            # ====================================
            # VALIDATIE
            # ====================================

            required_columns = [
                "Van Land",
                "Van Postcode",
                "Naar Land",
                "Naar Postcode"
            ]

            missing = [
                col
                for col in required_columns
                if col not in df.columns
            ]

            if missing:

                st.error(
                    "Ontbrekende kolommen: "
                    + ", ".join(missing)
                )

            else:

                if st.button(
                    "🚛 Verwerk Excel",
                    key="verwerk_excel"
                ):

                    results = []

                    progress_text = st.empty()

                    progress_bar = st.progress(0)

                    for i, row in df.iterrows():

                        progress_text.write(
                            f"Verwerken regel {i + 1} van {len(df)}"
                        )

                        try:

                            result = process_route(
                                row["Van Land"],
                                row["Van Postcode"],
                                row["Naar Land"],
                                row["Naar Postcode"]
                            )

                            if result is None:

                                result = {
                                    "Totale KM": None,
                                    "KM_NL": None,
                                    "KM_BE": None,
                                    "KM_DE": None,
                                    "KM_FR": None,
                                    "KM_LU": None,
                                    "NL_HEFFING_KM": None,
                                    "NL_HEFFING_DETAIL": None,
                                    "Voertuigklasse": voertuigklasse,
                                    "Tarief": tarief,
                                    "Tolkosten": None,
                                    "Niet-Heffing KM": None
                                }

                            else:

                                niet_heffing = round(
                                    result["KM_NL"]
                                    - result["NL_HEFFING_KM"],
                                    1
                                )

                                result["Voertuigklasse"] = voertuigklasse

                                result["Tarief"] = tarief

                                result["Niet-Heffing KM"] = (
                                    niet_heffing
                                )

                                result["Tolkosten"] = round(
                                    result["NL_HEFFING_KM"]
                                    * tarief,
                                    2
                                )

                        except Exception:

                            result = {
                                "Totale KM": None,
                                "KM_NL": None,
                                "KM_BE": None,
                                "KM_DE": None,
                                "KM_FR": None,
                                "KM_LU": None,
                                "NL_HEFFING_KM": None,
                                "NL_HEFFING_DETAIL": None,
                                "Voertuigklasse": voertuigklasse,
                                "Tarief": tarief,
                                "Tolkosten": None,
                                "Niet-Heffing KM": None
                            }

                        results.append(result)

                        progress_bar.progress(
                            (i + 1) / len(df)
                        )

                    progress_text.empty()

                    result_df = pd.DataFrame(results)

                    output_df = pd.concat(
                        [
                            df,
                            result_df
                        ],
                        axis=1
                    )

                    st.success(
                        f"{len(output_df)} routes verwerkt"
                    )

                    st.subheader("Resultaat")

                    st.dataframe(
                        output_df,
                        use_container_width=True
                    )

                    # ============================
                    # DOWNLOAD RESULTAAT
                    # ============================

                    output_buffer = BytesIO()

                    output_df.to_excel(
                        output_buffer,
                        index=False
                    )

                    st.download_button(
                        label="📥 Download Resultaat",
                        data=output_buffer.getvalue(),
                        file_name="output.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )

        except Exception as e:

            st.error(
                f"Fout bij verwerken bestand: {e}"
            )
