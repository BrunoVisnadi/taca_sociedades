import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session, DeclarativeBase

load_dotenv()  # lê .env em dev

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definida.")

# pool_pre_ping ajuda a recuperar conexões “adormecidas”
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = scoped_session(
    sessionmaker(bind=engine, autoflush=False, autocommit=False)
)

class Base(DeclarativeBase):
    pass
