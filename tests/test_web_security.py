
import pytest
import sys
import os
import importlib
from unittest.mock import MagicMock, patch

# Mock dependencies globally before any import
sys.modules['asyncpg'] = MagicMock()
sys.modules['asyncpg.pool'] = MagicMock()
sys.modules['google.genai'] = MagicMock()
sys.modules['google.genai.errors'] = MagicMock()
sys.modules['redis'] = MagicMock()
sys.modules['redis.exceptions'] = MagicMock()
sys.modules['telegram'] = MagicMock()
sys.modules['telegram.ext'] = MagicMock()
sys.modules['telegram.error'] = MagicMock()
sys.modules['hypercorn.config'] = MagicMock()
sys.modules['hypercorn.asyncio'] = MagicMock()
sys.modules['pytz'] = MagicMock()
sys.modules['psutil'] = MagicMock()

# Mock pydantic
mock_pydantic = MagicMock()
class MockBaseModel:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
mock_pydantic.BaseModel = MockBaseModel
mock_pydantic.ValidationError = Exception
sys.modules['pydantic'] = mock_pydantic

# Mock flask
mock_flask = MagicMock()
class MockFlask:
    def __init__(self, name):
        self.config = {}
    def route(self, rule, **options):
        def decorator(f):
            return f
        return decorator
mock_flask.Flask = MockFlask
mock_request = MagicMock()
mock_request.headers = {}
mock_flask.request = mock_request

class AbortError(Exception):
    def __init__(self, code, description=None):
        self.code = code
        self.description = description

def abort_side_effect(code, description=None):
    raise AbortError(code, description)

mock_flask.abort = MagicMock(side_effect=abort_side_effect)

sys.modules['flask'] = mock_flask

# Mock app.database
mock_db = MagicMock()
mock_db.db_pool = None
sys.modules['app.database'] = mock_db

# Mock app.config settings
@pytest.fixture
def mock_settings():
    with patch('app.config.settings') as mock:
        mock.TELEGRAM_BOT_TOKEN = "test_bot_token"
        mock.ADMIN_ID = 123
        mock.ADMIN_SECRET = None
        yield mock

def test_require_auth_missing_secret(mock_settings):
    """Test that require_auth aborts 500 if ADMIN_SECRET is missing"""
    # Reload app.web
    if 'app.web' in sys.modules:
        importlib.reload(sys.modules['app.web'])
    else:
        import app.web

    from app.web import require_auth

    # Mock request headers
    mock_request.headers = {'X-Auth-Token': 'test_bot_token'}

    # Create a dummy protected function
    @require_auth
    def protected():
        return "ok"

    # Ensure ADMIN_SECRET is not in env
    with patch.dict(os.environ, {}, clear=True):
        # Ensure settings.ADMIN_SECRET is None (default in fixture)

        # Expect 500
        with pytest.raises(AbortError) as excinfo:
            protected()
        assert excinfo.value.code == 500
        assert "misconfiguration" in str(excinfo.value.description)

def test_require_auth_fallback_removed(mock_settings):
    """Test that require_auth does NOT use TELEGRAM_BOT_TOKEN as fallback"""
    if 'app.web' in sys.modules:
        importlib.reload(sys.modules['app.web'])
    else:
        import app.web
    from app.web import require_auth

    # Set request token to bot token
    mock_request.headers = {'X-Auth-Token': 'test_bot_token'}

    @require_auth
    def protected():
        return "ok"

    # Ensure ADMIN_SECRET is not set
    with patch.dict(os.environ, {}, clear=True):
        # Even if we set settings.TELEGRAM_BOT_TOKEN (done in fixture),
        # require_auth should NOT use it.
        # It should see no secret and abort 500 (or 401 if we interpret "fallback removed" differently,
        # but my code raises 500 if secret is missing).

        with pytest.raises(AbortError) as excinfo:
            protected()
        assert excinfo.value.code == 500

def test_require_auth_success(mock_settings):
    """Test that require_auth works with ADMIN_SECRET"""
    mock_settings.ADMIN_SECRET = "secure_secret"

    if 'app.web' in sys.modules:
        importlib.reload(sys.modules['app.web'])
    else:
        import app.web
    from app.web import require_auth

    # Set correct token
    mock_request.headers = {'X-Auth-Token': 'secure_secret'}

    @require_auth
    def protected():
        return "ok"

    # Ensure ADMIN_SECRET is set in settings (via fixture modification)

    # Should succeed
    assert protected() == "ok"

def test_require_auth_wrong_token(mock_settings):
    """Test that require_auth rejects wrong token"""
    mock_settings.ADMIN_SECRET = "secure_secret"

    if 'app.web' in sys.modules:
        importlib.reload(sys.modules['app.web'])
    else:
        import app.web
    from app.web import require_auth

    mock_request.headers = {'X-Auth-Token': 'wrong_token'}

    @require_auth
    def protected():
        return "ok"

    with pytest.raises(AbortError) as excinfo:
        protected()
    assert excinfo.value.code == 401
