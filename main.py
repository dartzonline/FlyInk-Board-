import time
import logging
import sys
from src.config import load_config
from src.flight_api import FlightAPIClient, FlightAPIError
from src.display import InkyDisplayController

# Configure robust logging for production
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

def main():
    """
    Main entry point for the Flight Tracker application.
    Initializes configuration, API client, and Display controller,
    then enters the polling loop.
    """
    logger.info("Starting Inky Flight Tracker...")
    
    config = load_config()
    api_client = FlightAPIClient(config)
    display_controller = InkyDisplayController(config)
    
    logger.info(f"Configured update interval: {config.update_interval_seconds} seconds.")
    
    try:
        while True:
            logger.info("Polling for nearby flights...")
            try:
                flights = api_client.get_nearby_flights()
                logger.info(f"Found {len(flights)} flight(s) in the bounding box.")
                
                display_controller.render_flights(flights)
                
            except FlightAPIError as e:
                logger.error(f"Failed to update flights: {e}")
            except Exception as e:
                logger.exception(f"Unexpected error during update loop: {e}")
                
            logger.debug(f"Sleeping for {config.update_interval_seconds} seconds.")
            time.sleep(config.update_interval_seconds)
            
    except KeyboardInterrupt:
        logger.info("Shutdown signal received. Exiting gracefully.")
    finally:
        logger.info("Application terminated.")

if __name__ == "__main__":
    main()
