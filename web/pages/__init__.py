# Pages package - Flask blueprints for modular page organization

from pages.dashboard import dashboard_bp, get_pipeline_version, get_disk_usage, DASHBOARD_VERSION, GITHUB_URL, HOSTNAME
from pages.api import api_bp
from pages.api_identify import api_identify_bp
from pages.api_services import api_services_bp
from pages.api_cluster import api_cluster_bp
from pages.archives import archives_bp
