import pytest
from httpx import ASGITransport

from tests.fake_jb2 import create_fake_jb2, seed_from_fixtures


@pytest.fixture
def fake_jb2():
    """(transport, state) for a fresh fake-JB2 instance — zero network.

    Build a client against it with:
        httpx.AsyncClient(transport=transport, base_url="http://fake-jb2")
    Mutate `state` directly to seed resources / inspect writes / inject errors.
    """
    state = seed_from_fixtures()
    app = create_fake_jb2(state)
    transport = ASGITransport(app=app)
    yield transport, state
