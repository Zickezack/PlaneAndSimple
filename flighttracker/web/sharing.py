"""View data for the "Sharing" section (`templates/_sharing.html`) of a Suchabo or Trip."""

from sqlalchemy.orm import Session

from flighttracker.services.access import Actor, list_shares
from flighttracker.services.users import list_users, usernames


def sharing_view(
    db: Session,
    user: Actor,
    owner_id: int | None,
    *,
    search_id: int | None = None,
    trip_id: int | None = None,
) -> dict:
    return {
        "base_url": f"/searches/{search_id}" if search_id is not None else f"/trips/{trip_id}",
        "shares": list_shares(db, search_id=search_id, trip_id=trip_id),
        "owner_id": str(owner_id) if owner_id else "",
        "owner_name": usernames(db, {owner_id}).get(owner_id) if owner_id else None,
        # Only admins assign owners; they may pick any account. (value, text) pairs.
        "owner_options": [(str(u.id), u.username) for u in list_users(db)]
        if user.is_admin
        else None,
    }
