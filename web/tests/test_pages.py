"""
Test basic page rendering to catch template errors early.

These tests verify that all pages render without 500 errors.
They don't test functionality, just that templates compile and render.
"""

import pytest
import sys
import os

# Add web directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask


@pytest.fixture
def app():
    """Create test Flask application."""
    from pages.dashboard import dashboard_bp
    from pages.api import api_bp
    from pages.archives import archives_bp

    app = Flask(__name__,
                template_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates'),
                static_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static'))
    app.config['TESTING'] = True

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(archives_bp)

    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()


class TestPageRendering:
    """Test that all pages render without 500 errors."""

    def test_dashboard_renders(self, client):
        """Dashboard page should render."""
        response = client.get('/')
        assert response.status_code == 200, f"Dashboard returned {response.status_code}"

    def test_config_renders(self, client):
        """Config page should render (caught section.keys bug)."""
        response = client.get('/config')
        assert response.status_code == 200, f"Config returned {response.status_code}"

    def test_logs_renders(self, client):
        """Logs page should render."""
        response = client.get('/logs')
        assert response.status_code == 200, f"Logs returned {response.status_code}"

    def test_status_renders(self, client):
        """Status page should render."""
        response = client.get('/status')
        assert response.status_code == 200, f"Status returned {response.status_code}"

    def test_health_renders(self, client):
        """Health page should render."""
        response = client.get('/health')
        assert response.status_code == 200, f"Health returned {response.status_code}"

    def test_archives_renders(self, client):
        """Archives page should render."""
        response = client.get('/archives')
        assert response.status_code == 200, f"Archives returned {response.status_code}"

    def test_cluster_renders(self, client):
        """Cluster page should render."""
        response = client.get('/cluster')
        assert response.status_code == 200, f"Cluster returned {response.status_code}"

    def test_issues_renders(self, client):
        """Issues page should render."""
        response = client.get('/issues')
        assert response.status_code == 200, f"Issues returned {response.status_code}"

    def test_architecture_renders(self, client):
        """Architecture page should render."""
        response = client.get('/architecture')
        assert response.status_code == 200, f"Architecture returned {response.status_code}"


class TestAPIEndpoints:
    """Test that basic API endpoints respond."""

    def test_api_status(self, client):
        """API status endpoint should respond."""
        response = client.get('/api/status')
        assert response.status_code == 200
        assert response.content_type == 'application/json'

    def test_api_queue(self, client):
        """API queue endpoint should respond."""
        response = client.get('/api/queue')
        assert response.status_code == 200
        assert response.content_type == 'application/json'

    def test_api_disk(self, client):
        """API disk endpoint should respond."""
        response = client.get('/api/disk')
        assert response.status_code == 200
        assert response.content_type == 'application/json'
