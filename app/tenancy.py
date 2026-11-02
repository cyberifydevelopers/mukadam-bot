"""Who the code is working for. A request (see app/routers/google_login.py) or a background job runs `with use(user_id):`
and everything inside — database sessions, saved files, the Google login, the WhatsApp numbers — is that user's."""

from contextlib import contextmanager

from sqlalchemy.orm import Session

from app.database import FIRST_USER, _main_factory, current_user
from app.models import User


@contextmanager
def use(user_id: int | None):
    token = current_user.set(user_id)
    try:
        yield
    finally:
        current_user.reset(token)


def current_id() -> int | None:
    return current_user.get()


def is_first_user() -> bool:
    return current_user.get() in (None, FIRST_USER)


def user_ids() -> list[int | None]:
    """Everyone to run a background job for. [None] while nobody has signed in yet: the single mailbox set up in .env."""
    with _main_factory() as db:
        ids = [row[0] for row in db.query(User.id).order_by(User.id)]
    return ids or [None]


def find_or_create_user(db: Session, email: str) -> tuple[User, bool]:
    user = db.query(User).filter_by(email=email).first()
    if user is not None:
        return user, False
    user = User(email=email)
    db.add(user)
    db.commit()
    return user, True
