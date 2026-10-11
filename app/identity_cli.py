"""Initialisation du premier administrateur humain."""

import argparse
import asyncio
import getpass

from sqlalchemy import func, select

from app.identity_models import User
from app.identity_service import audit, database_factory, normalize_email, password_hasher, seed_roles, validate_password


async def bootstrap_admin(email: str, display_name: str, password: str) -> None:
    if not 1 <= len(display_name.strip()) <= 120:
        raise ValueError("Le nom doit contenir entre 1 et 120 caractères.")
    factory = database_factory()
    try:
        async with factory() as db:
            count = await db.scalar(select(func.count()).select_from(User))
            if count:
                raise RuntimeError("Bootstrap refusé : des comptes existent déjà.")
            await seed_roles(db)
            admin = User(email=normalize_email(email), display_name=display_name.strip(),
                         password_hash=password_hasher.hash(password), is_active=True, is_platform_admin=True)
            db.add(admin)
            await db.flush()
            audit(db, "user.bootstrap", "success", actor_id=admin.id, target_type="user", target_id=admin.id)
            await db.commit()
    finally:
        await factory.kw["bind"].dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Identité Floppy-MimIC")
    parser.add_argument("command", choices=["bootstrap-admin"])
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    password = getpass.getpass("Mot de passe administrateur : ")
    validate_password(password)
    if password != getpass.getpass("Confirmation : "):
        raise SystemExit("Les mots de passe ne correspondent pas.")
    asyncio.run(bootstrap_admin(args.email, args.name, password))
    print("Administrateur initial créé.")


if __name__ == "__main__":
    main()
