from __future__ import annotations

import argparse
import getpass
import os

from argon2 import PasswordHasher
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from .app import Account, AuthSession, Blob, Change, Device, RegistrationClaim, RemoteEntity, new_id, now


def database_url() -> str:
    value = os.environ.get("MEALCIRCUIT_SYNC_DATABASE_URL")
    if not value:
        raise SystemExit("MEALCIRCUIT_SYNC_DATABASE_URL is required")
    return value


def password_twice() -> str:
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Repeat password: ")
    if len(first) < 12 or first != second:
        raise SystemExit("passwords differ or contain fewer than 12 characters")
    return first


def main() -> None:
    parser = argparse.ArgumentParser(description="MealCircuit Sync authentication and usage administration")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("accounts")
    create = sub.add_parser("create-account")
    create.add_argument("login_name")
    for name in ("disable", "enable", "reset-password", "usage"):
        command = sub.add_parser(name)
        command.add_argument("login_name")
    args = parser.parse_args()
    engine = create_engine(database_url(), future=True)
    hasher = PasswordHasher()
    with Session(engine) as session:
        if args.command == "accounts":
            for account in session.scalars(select(Account).order_by(Account.created_at)):
                print(f"{account.login_name}\t{'disabled' if account.disabled else 'active'}\t{account.created_at.isoformat()}")
            return
        login_name = args.login_name.strip().lower()
        account = session.scalar(select(Account).where(Account.login_name == login_name))
        if args.command == "create-account":
            if account:
                raise SystemExit("account already exists")
            password = password_twice()
            account = Account(
                id=new_id("account"),
                login_name=login_name,
                password_hash=hasher.hash(password),
                disabled=False,
                change_sequence=0,
                created_at=now(),
            )
            session.add(account)
            if session.get(RegistrationClaim, 1) is None:
                session.add(RegistrationClaim(id=1, claimed_at=now()))
            session.commit()
            print("account created; initialize it on the first client with sync-configure --bootstrap")
            return
        if not account:
            raise SystemExit("account not found")
        if args.command in {"disable", "enable"}:
            account.disabled = args.command == "disable"
            if account.disabled:
                for auth in session.scalars(
                    select(AuthSession).where(AuthSession.account_id == account.id)
                ):
                    auth.revoked = True
            session.commit()
            print(f"account {args.command}d")
        elif args.command == "reset-password":
            account.password_hash = hasher.hash(password_twice())
            for auth in session.scalars(
                select(AuthSession).where(AuthSession.account_id == account.id)
            ):
                auth.revoked = True
            session.commit()
            print("authentication password reset; encrypted data key and recovery envelope were not changed")
        elif args.command == "usage":
            device_count = session.scalar(
                select(func.count()).select_from(Device).where(Device.account_id == account.id)
            )
            entity_count = session.scalar(
                select(func.count()).select_from(RemoteEntity).where(RemoteEntity.account_id == account.id)
            )
            change_count = session.scalar(
                select(func.count()).select_from(Change).where(Change.account_id == account.id)
            )
            blob_bytes = session.scalar(
                select(func.coalesce(func.sum(Blob.byte_count), 0)).where(
                    Blob.account_id == account.id, Blob.complete.is_(True)
                )
            )
            print(
                f"account={account.login_name} devices={device_count} entities={entity_count} "
                f"changes={change_count} encrypted_blob_bytes={blob_bytes}"
            )


if __name__ == "__main__":
    main()
