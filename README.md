# FlyInk Board

![Inky Display](https://img.shields.io/badge/Display-Inky-blue)
![Python](https://img.shields.io/badge/Python-3.8+-blue)

A robust, production-ready Python application that fetches nearby commercial flights using the OpenSky Network API and renders the data, including airline logos, onto an Inky e-ink display.

**Author:** Dart

## Features

- **Live Flight Tracking:** Polls OpenSky Network API for flights within a configurable bounding box.
- **Hardware Agnostic:** Uses `inky.auto` to automatically detect your specific Inky display model (wHAT, pHAT, Impression).
- **Dynamic Assets:** Loads airline logos dynamically based on the flight's callsign.
- **Resilient:** Built with robust error handling, retries, and comprehensive logging.
- **Clean Architecture:** Modular, object-oriented design with type hinting.

## Project Structure

```
FlyInk-Board/
├── main.py              # Application entry point
├── requirements.txt     # Python dependencies
├── src/                 # Source code module
│   ├── config.py        # Configuration and environment variables
│   ├── flight_api.py    # OpenSky API client
│   └── display.py       # Inky display controller
└── assets/              # Static assets
    ├── logos/           # Airline logos (.png format, named by IATA/ICAO code)
    └── fonts/           # Custom TrueType fonts
```

## Setup & Installation

1. **Clone the repository**
   ```bash
   git clone git@github.com:dartzonline/FlyInk-Board-.git
   cd FlyInk-Board
   ```

2. **Set up a Virtual Environment (Recommended)**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Add Assets**
   - Place any `.png` airline logos into `assets/logos/` (e.g., `UAL.png`, `DAL.png`).
   - Place custom `.ttf` fonts in `assets/fonts/` (named `OpenSans-Bold.ttf` and `OpenSans-Regular.ttf`, or modify `src/display.py` to match your font names). If not provided, it falls back to PIL's default font.

## Configuration

By default, the script polls a bounding box over the New York area. You can modify the `BoundingBox` default values in `src/config.py` to track flights in your local area.

*Optional:* To increase API rate limits, set your OpenSky Network credentials as environment variables:
```bash
export OPENSKY_USERNAME="your_username"
export OPENSKY_PASSWORD="your_password"
```

## Usage

Run the main application loop:

```bash
python main.py
```

The application will begin polling the API every 60 seconds (configurable) and will update the Inky display with the closest or most relevant flight data.

## License

MIT License
