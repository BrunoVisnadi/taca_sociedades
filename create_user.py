# create_user.py
from werkzeug.security import generate_password_hash
from db import SessionLocal
from models import User

if __name__ == "__main__":
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
