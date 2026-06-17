import logging
import requests
from typing import List, Dict, Any, Optional
from requests.auth import HTTPBasicAuth
from src.config import Config

logger = logging.getLogger(__name__)

class FlightAPIError(Exception):
    """Custom exception for errors during flight API requests."""
    pass

class FlightAPIClient:
    """Client for interacting with the OpenSky Network API."""
    
    BASE_URL = "https://opensky-network.org/api/states/all"
    
    def __init__(self, config: Config):
        self.config = config
        self.auth = None
        if self.config.opensky_username and self.config.opensky_password:
            self.auth = HTTPBasicAuth(self.config.opensky_username, self.config.opensky_password)

    def get_nearby_flights(self) -> List[Dict[str, Any]]:
        """
        Fetches flights within the configured bounding box.
        
        Returns:
            A list of dictionaries representing parsed flight data.
        """
        params = {
            "lamin": self.config.bounding_box.lamin,
            "lomin": self.config.bounding_box.lomin,
            "lamax": self.config.bounding_box.lamax,
            "lomax": self.config.bounding_box.lomax,
        }
        
        try:
            logger.debug(f"Requesting flight data from OpenSky API with params: {params}")
            response = requests.get(self.BASE_URL, params=params, auth=self.auth, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            return self._parse_states(data.get("states"))
        
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch flight data: {e}")
            raise FlightAPIError(f"Network error: {e}")
        except ValueError as e:
            logger.error(f"Failed to parse JSON response: {e}")
            raise FlightAPIError(f"Parse error: {e}")

    def _parse_states(self, states: Optional[List[List[Any]]]) -> List[Dict[str, Any]]:
        """Parses the raw states list from OpenSky into a structured dictionary."""
        if not states:
            return []
            
        parsed_flights = []
        for state in states:
            # OpenSky states vector mapping:
            # 0: icao24, 1: callsign, 2: origin_country, 3: time_position, 4: last_contact,
            # 5: longitude, 6: latitude, 7: baro_altitude, 8: on_ground, 9: velocity,
            # 10: true_track, 11: vertical_rate, 12: sensors, 13: geo_altitude, 14: squawk,
            # 15: spi, 16: position_source
            
            callsign = str(state[1]).strip() if state[1] else "UNKNOWN"
            airline_code = callsign[:3] if len(callsign) >= 3 else "N/A"
            
            flight = {
                "icao24": state[0],
                "callsign": callsign,
                "airline_code": airline_code,
                "origin_country": state[2],
                "longitude": state[5],
                "latitude": state[6],
                "altitude_m": state[7],
                "velocity_ms": state[9],
                "heading": state[10],
            }
            parsed_flights.append(flight)
            
        return parsed_flights
