from datetime import date

import pytest

from bizdesk_mcp.demo import seed_demo
from bizdesk_mcp.service import BizDesk
from bizdesk_mcp.store import Store

TODAY = date(2026, 10, 15)


@pytest.fixture
def desk():
    return BizDesk(Store(":memory:"), today=lambda: TODAY)


@pytest.fixture
def demo_desk(desk):
    seed_demo(desk)
    return desk


@pytest.fixture
def anyio_backend():
    return "asyncio"
