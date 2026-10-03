"""Who may see and change which Suchabo or Trip.

- Admins: everything, including Suchabos/Trips without owner.
- Owner: everything on their own Suchabos and Trips, including sharing them.
- Shared with "view": detail pages, flights, export. With "edit": additionally edit, merge,
  pause/resume and "poll now" – archiving, deleting and sharing stay with owner and admins.
- A Trip leg is accessed through its Trip, never on its own.
"""

from enum import IntEnum
from typing import Protocol

from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.orm import Session, selectinload

from flighttracker.i18n import Msg
from flighttracker.models import Search, Share, Trip, TripLeg, User
from flighttracker.services.users import find_user, pause_unattended


class Access(IntEnum):
    VIEW = 1
    EDIT = 2
    OWN = 3


class Actor(Protocol):
    id: int

    @property
    def is_admin(self) -> bool: ...


class ShareError(ValueError):
    def __init__(self, message: Msg):
        super().__init__(str(message))
        self.message = message


def _share_access(share: Share | None) -> Access | None:
    if share is None:
        return None
    return Access.EDIT if share.can_edit else Access.VIEW


def trip_access(session: Session, actor: Actor, trip: Trip) -> Access | None:
    if actor.is_admin or trip.owner_id == actor.id:
        return Access.OWN
    share = session.scalar(select(Share).where(Share.trip_id == trip.id, Share.user_id == actor.id))
    return _share_access(share)


def search_access(session: Session, actor: Actor, search: Search) -> Access | None:
    if actor.is_admin:
        return Access.OWN
    trip = session.scalar(select(Trip).join(TripLeg).where(TripLeg.search_id == search.id))
    if trip is not None:
        return trip_access(session, actor, trip)
    if search.owner_id == actor.id:
        return Access.OWN
    share = session.scalar(
        select(Share).where(Share.search_id == search.id, Share.user_id == actor.id)
    )
    return _share_access(share)


def _shared_with(actor: Actor, target: ColumnElement[bool], edit_only: bool) -> ColumnElement[bool]:
    conditions = [target, Share.user_id == actor.id]
    if edit_only:
        conditions.append(Share.can_edit)
    return exists().where(*conditions)


def visible_trips(actor: Actor, *, edit_only: bool = False) -> ColumnElement[bool] | None:
    """WHERE condition for the Trips `actor` may see (or edit); None = no restriction."""
    if actor.is_admin:
        return None
    return or_(Trip.owner_id == actor.id, _shared_with(actor, Share.trip_id == Trip.id, edit_only))


def visible_searches(actor: Actor, *, edit_only: bool = False) -> ColumnElement[bool] | None:
    """WHERE condition for the Suchabos `actor` may see (or edit), Trip legs through their Trip;
    None = no restriction (admin)."""
    if actor.is_admin:
        return None
    via_trip = (
        select(TripLeg.search_id)
        .join(Trip, Trip.id == TripLeg.trip_id)
        .where(visible_trips(actor, edit_only=edit_only))
    )
    is_leg = exists().where(TripLeg.search_id == Search.id)
    own_or_shared = or_(
        Search.owner_id == actor.id,
        _shared_with(actor, Share.search_id == Search.id, edit_only),
    )
    return or_(and_(~is_leg, own_or_shared), Search.id.in_(via_trip))


def list_shares(session: Session, *, search_id: int | None = None, trip_id: int | None = None):
    statement = select(Share).options(selectinload(Share.user)).join(User)
    if search_id is not None:
        statement = statement.where(Share.search_id == search_id)
    else:
        statement = statement.where(Share.trip_id == trip_id)
    return list(session.scalars(statement.order_by(User.username)))


def share_with(
    session: Session,
    *,
    owner_id: int | None,
    username: str,
    can_edit: bool,
    search_id: int | None = None,
    trip_id: int | None = None,
) -> Share:
    """Add or change the access of one user; sharing again only changes view/edit."""
    user = find_user(session, username.strip())
    if user is None:
        raise ShareError(Msg('There is no user "{name}".', name=username.strip()))
    if user.id == owner_id:
        raise ShareError(Msg('"{name}" is the owner already.', name=user.username))
    if user.is_admin:
        raise ShareError(
            Msg('"{name}" is an admin and sees everything already.', name=user.username)
        )
    share = session.scalar(
        select(Share).where(
            Share.user_id == user.id,
            Share.search_id.is_not_distinct_from(search_id),
            Share.trip_id.is_not_distinct_from(trip_id),
        )
    )
    if share is None:
        share = Share(search_id=search_id, trip_id=trip_id, user_id=user.id)
        session.add(share)
    share.can_edit = can_edit
    session.flush()
    pause_unattended(session)  # the last editor of a deactivated user's search may be gone
    return share


def remove_share(
    session: Session, share_id: int, *, search_id: int | None = None, trip_id: int | None = None
) -> bool:
    share = session.get(Share, share_id)
    if share is None or share.search_id != search_id or share.trip_id != trip_id:
        return False
    session.delete(share)
    session.flush()
    pause_unattended(session)
    return True


def set_trip_owner(trip: Trip, owner_id: int | None) -> None:
    """The Trip and its leg Suchabos always belong to the same user."""
    trip.owner_id = owner_id
    for leg in trip.legs:
        leg.search.owner_id = owner_id
