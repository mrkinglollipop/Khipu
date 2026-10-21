"""Gateway transport fixtures use only the repository's disposable hub."""
import pytest

from khipu import db, routing, routing_gateway_copy as gc
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401


@pytest.fixture
def gateway_hub(hub, monkeypatch):  # noqa: F811
    conn = hub.connect()
    hub.migrate(conn=conn)
    monkeypatch.setattr(db, "connect", lambda **kwargs: hub.connect())
    original = gc.PostgresHub.publish
    def publish(self, publisher, projection, stale_after):
        original(self, publisher, projection, stale_after)
        with hub.connect() as conn:
            conn.execute("UPDATE routing_gateway_copies SET published_at=%s::timestamptz WHERE publisher=%s",
                         (routing._iso(routing._now()), publisher))
        return routing._iso(routing._now())
    monkeypatch.setattr(gc.PostgresHub, "publish", publish)
    return hub
