#!/usr/bin/env python3
"""
DVD Ripper Web Dashboard
A minimal Flask web UI for monitoring the DVD auto-ripper pipeline.

https://github.com/mschober/dvd-auto-ripper
"""

import os
from flask import Flask

# Import blueprints from pages package
from pages.dashboard import dashboard_bp, get_pipeline_version, DASHBOARD_VERSION, GITHUB_URL
from pages.api import api_bp
from pages.api_identify import api_identify_bp
from pages.api_services import api_services_bp
from pages.api_cluster import api_cluster_bp
from pages.archives import archives_bp

# Create Flask app
app = Flask(__name__,
            template_folder='templates',
            static_folder='static')

# Register blueprints
app.register_blueprint(dashboard_bp)
app.register_blueprint(api_bp)
app.register_blueprint(api_identify_bp)
app.register_blueprint(api_services_bp)
app.register_blueprint(api_cluster_bp)
app.register_blueprint(archives_bp)


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    print(f"DVD Ripper Dashboard v{DASHBOARD_VERSION}")
    print(f"Pipeline version: {get_pipeline_version()}")
    print(f"Starting on http://{host}:{port}")
    print(f"Project: {GITHUB_URL}")
    app.run(host=host, port=port, debug=debug)
