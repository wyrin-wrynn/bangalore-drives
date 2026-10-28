import numpy as np

R_EARTH_KM = 6371.0


def haversine_km(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = map(np.radians, (lat1, lng1, lat2, lng2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2
    return R_EARTH_KM * 2 * np.arcsin(np.sqrt(a))


class LocalXY:
    """Flat metre coordinates around a reference latitude: good to a fraction of a percent across a city."""

    def __init__(self, lat0):
        self.kx = 111_320 * np.cos(np.radians(lat0))
        self.ky = 110_540

    def __call__(self, lat, lng):
        return np.asarray(lng, float) * self.kx, np.asarray(lat, float) * self.ky
