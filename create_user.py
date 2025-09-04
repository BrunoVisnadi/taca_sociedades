# create_user.py
from werkzeug.security import generate_password_hash
from db import SessionLocal
from models import User, SocietyAccount
from sqlalchemy import select

def user():
    email = input("Email: ").strip().lower()
    password = input("Senha: ").strip()
    role = input("Role [normal/director/admin]: ").strip().lower() or "normal"

    sess = SessionLocal()
    try:
        if sess.query(User).filter(User.email == email).one_or_none():
            print("Já existe usuário com este e-mail.")
        else:
            u = User(email=email, password_hash=generate_password_hash(password), role=role, is_active=True)
            sess.add(u)
            sess.commit()
            print("Usuário criado.")
    finally:
        sess.close()


from werkzeug.security import generate_password_hash

def create_society_account(sess, edition_society_id: int, email: str, raw_password: str):
    email = email.strip().lower()
    acc = sess.execute(
        select(SocietyAccount).where(SocietyAccount.email == email)
    ).scalar_one_or_none()
    if acc:
        raise ValueError("E-mail já em uso")
    sess.add(SocietyAccount(
        edition_society_id=edition_society_id,
        email=email,
        password_hash=generate_password_hash(raw_password),
        is_active=True
    ))
    sess.commit()

if __name__ == '__main__':

    create_society_account(SessionLocal(), 2, "SDUFRJ", "terceiro3olugar3")
