import os
from pydantic import BaseModel, Field

class BoundingBox(BaseModel):
    """Represents a geographic bounding box for querying flights."""
    lamin: float = Field(default=40.0, description="Minimum latitude")
    lomin: float = Field(default=-74.5, description="Minimum longitude")
    lamax: float = Field(default=41.0, description="Maximum latitude")
    lomax: float = Field(default=-73.0, description="Maximum longitude")

class Config(BaseModel):
    """Application configuration for the Flight Tracker."""
    bounding_box: BoundingBox = Field(default_factory=BoundingBox)
    update_interval_seconds: int = Field(default=60, description="How often to poll the API")
    inky_display_type: str = Field(default="what", description="Type of inky display (e.g. what, phat, impression)")
    inky_color: str = Field(default="red", description="Color capability of inky display (e.g. red, yellow, black)")
    
    # Optional credentials for OpenSky Network
    opensky_username: str = Field(default_factory=lambda: os.getenv("OPENSKY_USERNAME", ""))
    opensky_password: str = Field(default_factory=lambda: os.getenv("OPENSKY_PASSWORD", ""))

def load_config() -> Config:
    """Loads and returns the application configuration."""
    return Config()
