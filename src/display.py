import os
import logging
from typing import List, Dict, Any
from PIL import Image, ImageFont, ImageDraw

try:
    from inky.auto import auto
    INKY_AVAILABLE = True
except ImportError:
    INKY_AVAILABLE = False
    logging.warning("Inky library not found or running in test environment. Display will be simulated.")

from src.config import Config

logger = logging.getLogger(__name__)

class InkyDisplayController:
    """Handles rendering text and images to the Inky e-ink display."""
    
    def __init__(self, config: Config):
        self.config = config
        self.display = auto() if INKY_AVAILABLE else None
        
        # Dimensions setup
        self.width = self.display.resolution[0] if self.display else 400
        self.height = self.display.resolution[1] if self.display else 300
        
        # Asset paths
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.logos_dir = os.path.join(self.base_dir, "assets", "logos")
        self.fonts_dir = os.path.join(self.base_dir, "assets", "fonts")
        
        # Default font fallbacks
        try:
            self.font_large = ImageFont.truetype(os.path.join(self.fonts_dir, "OpenSans-Bold.ttf"), 24)
            self.font_small = ImageFont.truetype(os.path.join(self.fonts_dir, "OpenSans-Regular.ttf"), 16)
        except OSError:
            logger.warning("Custom fonts not found. Falling back to default PIL font.")
            self.font_large = ImageFont.load_default()
            self.font_small = ImageFont.load_default()

    def _get_logo_path(self, airline_code: str) -> str:
        """Looks up the logo path for a given airline code."""
        logo_path = os.path.join(self.logos_dir, f"{airline_code}.png")
        if os.path.exists(logo_path):
            return logo_path
        return os.path.join(self.logos_dir, "default.png")

    def render_flights(self, flights: List[Dict[str, Any]]) -> None:
        """
        Renders the list of flights onto the display.
        
        Args:
            flights: A list of flight data dictionaries.
        """
        image = Image.new("P", (self.width, self.height), color=self._get_color("white"))
        draw = ImageDraw.Draw(image)
        
        if not flights:
            self._draw_text_centered(draw, "No flights nearby", self.font_large)
            self.push_to_display(image)
            return
        
        # Determine the closest flight or render a list. For simplicity, we render the first one.
        # In a real scenario, you might sort by proximity.
        target_flight = flights[0]
        
        # Draw Airline Logo
        logo_path = self._get_logo_path(target_flight["airline_code"])
        if os.path.exists(logo_path):
            try:
                logo = Image.open(logo_path).convert("P")
                # Resize logo if necessary and paste it
                logo.thumbnail((100, 100))
                image.paste(logo, (10, 10))
            except Exception as e:
                logger.error(f"Failed to load logo from {logo_path}: {e}")
        
        # Draw Flight Details
        text_x = 120
        y_offset = 20
        
        draw.text((text_x, y_offset), f"Callsign: {target_flight['callsign']}", fill=self._get_color("black"), font=self.font_large)
        y_offset += 30
        
        alt_ft = target_flight['altitude_m'] * 3.28084 if target_flight['altitude_m'] else 0
        vel_mph = target_flight['velocity_ms'] * 2.23694 if target_flight['velocity_ms'] else 0
        
        draw.text((text_x, y_offset), f"Alt: {alt_ft:.0f} ft", fill=self._get_color("black"), font=self.font_small)
        y_offset += 20
        draw.text((text_x, y_offset), f"Speed: {vel_mph:.0f} mph", fill=self._get_color("black"), font=self.font_small)
        y_offset += 20
        draw.text((text_x, y_offset), f"Country: {target_flight['origin_country']}", fill=self._get_color("black"), font=self.font_small)
        
        self.push_to_display(image)

    def _draw_text_centered(self, draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont) -> None:
        """Helper to draw text centered on the screen."""
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        x = (self.width - w) // 2
        y = (self.height - h) // 2
        draw.text((x, y), text, fill=self._get_color("black"), font=font)

    def _get_color(self, color_name: str) -> int:
        """Maps standard color names to Inky palette indices."""
        if not self.display:
            # Fallback PIL colors for simulation
            colors = {"white": 255, "black": 0, "red": 128}
            return colors.get(color_name.lower(), 0)
            
        if color_name.lower() == "white":
            return self.display.WHITE
        elif color_name.lower() == "black":
            return self.display.BLACK
        elif color_name.lower() in ["red", "yellow"]:
            # Depending on display type, this attribute might vary.
            return getattr(self.display, color_name.upper(), self.display.BLACK)
        return self.display.BLACK

    def push_to_display(self, image: Image.Image) -> None:
        """Pushes the Pillow image to the physical Inky display."""
        if self.display:
            self.display.set_image(image)
            self.display.show()
            logger.info("Image successfully pushed to Inky display.")
        else:
            # Save for debugging if display is not available
            debug_path = os.path.join(self.base_dir, "simulation_output.png")
            image.save(debug_path)
            logger.info(f"Display simulated. Output saved to {debug_path}")
