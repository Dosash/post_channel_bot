import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from tests.fakes import FakeBot  # noqa: E402


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    return Config(
        bot_token="123:TEST",
        admin_ids=[1],
        tz=ZoneInfo("Asia/Tashkent"),
        db_path=tmp_path / "bot.db",
        uploads_dir=uploads,
    )


@pytest.fixture
def bot():
    return FakeBot()
