from sqlmodel import Session, create_engine
from app.config import settings

engine = create_engine(settings.DATABASE_URL)

def get_session():
    with Session(engine) as session:
        yield session# endpoint ko database session deta h, aur request khatam hone ke baad session band (cleanup) ho jata h