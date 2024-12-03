
import pytest
from flask import Flask

from journalist_app import account, admin, api, col, main
from journalist_app.api import make_blueprint
from tests.utils.api_helper import get_api_headers
from tests.factories import SecureDropConfigFactory

@pytest.fixture
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    return app

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def config(tmp_path):
    return SecureDropConfigFactory.create(
        SECUREDROP_DATA_ROOT=tmp_path,
        GPG_KEY_DIR=tmp_path / "gpg",
        JOURNALIST_KEY="test_journalist_key",
        RQ_WORKER_NAME="test_worker",
    )

@pytest.fixture
def api_blueprint(app, config):
    app.config.from_object(config.JOURNALIST_APP_FLASK_CONFIG_CLS)
    blueprint = make_blueprint()
    app.register_blueprint(blueprint, url_prefix='/api/v1')
    return blueprint

def test_route_registration(app, api_blueprint):
    # Given
    expected_routes = [
        ("/api/v1/", ["GET"]),
        ("/api/v1/token", ["POST"]),
        ("/api/v1/sources", ["GET"]),
        ("/api/v1/sources/<source_uuid>", ["GET", "DELETE"]),
        ("/api/v1/sources/<source_uuid>/add_star", ["POST"]),
        ("/api/v1/sources/<source_uuid>/remove_star", ["DELETE"]),
        ("/api/v1/sources/<source_uuid>/flag", ["POST"]),
        ("/api/v1/sources/<source_uuid>/conversation", ["DELETE"]),
        ("/api/v1/sources/<source_uuid>/submissions", ["GET"]),
        ("/api/v1/sources/<source_uuid>/submissions/<submission_uuid>/download", ["GET"]),
        ("/api/v1/sources/<source_uuid>/replies/<reply_uuid>/download", ["GET"]),
        ("/api/v1/sources/<source_uuid>/submissions/<submission_uuid>", ["GET", "DELETE"]),
        ("/api/v1/sources/<source_uuid>/replies", ["GET", "POST"]),
        ("/api/v1/sources/<source_uuid>/replies/<reply_uuid>", ["GET", "DELETE"]),
        ("/api/v1/submissions", ["GET"]),
        ("/api/v1/replies", ["GET"]),
        ("/api/v1/seen", ["POST"]),
        ("/api/v1/user", ["GET"]),
        ("/api/v1/users", ["GET"]),
        ("/api/v1/logout", ["POST"]),
    ]

    # When
    registered_routes = [
        (rule.rule, list(rule.methods - {"HEAD", "OPTIONS"}))
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/api/v1")
    ]

    # Then
    for expected_route, expected_methods in expected_routes:
        assert any(
            expected_route == registered_route and set(expected_methods).issubset(set(registered_methods))
            for registered_route, registered_methods in registered_routes
        ), f"Route {expected_route} with methods {expected_methods} not found in registered routes"

    assert len(registered_routes) == len(expected_routes), "Number of registered routes doesn't match expected routes"

# ... [rest of the test file remains unchanged]
