import os
import tempfile

# Set env before any app imports
os.environ.setdefault("APP_PASSWORD", "test-pw")
os.environ.setdefault("SECRET_KEY", "test-secret-test-secret-test-secret")
os.environ.setdefault("FERNET_KEY", "")  # not needed for these tests
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mktemp(suffix='.db')}")
os.environ.setdefault("PUSHPRESS_BASE_URL", "https://members.pushpress.com")
os.environ.setdefault("PUSHPRESS_EMAIL", "test@example.com")
os.environ.setdefault("PUSHPRESS_PASSWORD", "test-pp-pw")
