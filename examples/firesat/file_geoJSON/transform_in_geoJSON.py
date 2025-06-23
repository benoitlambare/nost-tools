import math
import json
from geojson import Feature, FeatureCollection, Polygon
import os

geojson_file = "data.geojson"


def generate_circle_polygon(lat, lon, radius_meters, num_points=36):
    """
    Génère un polygone approximant un cercle autour du point lat/lon avec le rayon donné.
    """
    earth_radius = 6378137  # Rayon moyen de la Terre en mètres
    coordinates = []

    for i in range(num_points + 1):
        angle = 2 * math.pi * i / num_points
        dx = radius_meters * math.cos(angle)
        dy = radius_meters * math.sin(angle)

        dlat = dy / earth_radius
        dlon = dx / (earth_radius * math.cos(math.pi * lat / 180))

        lat_point = lat + dlat * 180 / math.pi
        lon_point = lon + dlon * 180 / math.pi
        coordinates.append([lon_point, lat_point])

    return [coordinates]  # GeoJSON attend une liste de listes


def convert_location_to_geojson(location_message):
    lat = location_message["latitude"]
    lon = location_message["longitude"]
    radius = location_message["radius"]
    norad_id = location_message["noradId"]
    date = location_message["time"]

    polygon = generate_circle_polygon(lat, lon, radius)

    feature = Feature(
        geometry=Polygon(polygon),
        properties={
            "id": f"NORAD_{norad_id}_mesh_1",
            "date": date,
            "resolution": 1,
            "price": 1500,
            "cloudCoveragePercent": 12,
        },
    )

    feature_collection = FeatureCollection([feature], name="Constellation")
    return feature_collection


def append_feature_to_file(new_feature, filepath="data.geojson"):
    # Charger l'existant ou créer un nouveau FeatureCollection
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            data = json.load(f)
            features = data.get("features", [])
    else:
        features = []

    # Ajouter le nouveau
    features.append(new_feature)

    # Réécrire tout
    feature_collection = FeatureCollection(features, name="Constellation")
    with open(filepath, "w") as f:
        json.dump(feature_collection, f, indent=2)
