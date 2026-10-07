"""Which chutes are currently degraded.

The board is the single read the sorter consults before accepting a route: a bag
whose flight's current chute sits behind an active incident is refused there, so
a stale routing cache can never keep feeding a dead chute.
"""

from __future__ import annotations

from bagsort.store.store import Store


class DegradationBoard:
    """Committed view of the outages that are still in force."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def active(self) -> dict[str, str]:
        return {
            incident.chute_id: incident.id
            for incident in self._store.state().incidents.values()
            if incident.status == "active"
        }

    def incident_for(self, chute_id: str) -> str | None:
        return self.active().get(chute_id)

    def is_degraded(self, chute_id: str) -> bool:
        return chute_id in self.active()

    def is_routable(self, chute_id: str) -> bool:
        return not self.is_degraded(chute_id)

    def get(self, incident_id: str):
        return self._store.state().incidents.get(incident_id)

    def require(self, incident_id: str):
        from bagsort.errors import NotFoundError

        incident = self.get(incident_id)
        if incident is None:
            raise NotFoundError(f"incident {incident_id!r} is not on record")
        return incident

    def all(self) -> list:
        incidents = self._store.state().incidents
        return [incidents[key] for key in sorted(incidents)]
