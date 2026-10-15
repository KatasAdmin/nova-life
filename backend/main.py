"""Nova Life v0.2 — authoritative game backend. No real-money features."""
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field
from pwdlib import PasswordHash
import jwt
from sqlalchemy import create_engine, ForeignKey, String, Integer, BigInteger, DateTime, select, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, Session, sessionmaker

DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///./nova_life.db').replace('postgres://','postgresql+psycopg://',1)
if DATABASE_URL.startswith('postgresql://'): DATABASE_URL=DATABASE_URL.replace('postgresql://','postgresql+psycopg://',1)
SECRET=os.environ.get('JWT_SECRET','')
if not SECRET: raise RuntimeError('Set JWT_SECRET to a long random secret')
ORIGINS=[x.strip().rstrip('/') for x in os.environ.get('FRONTEND_ORIGINS','https://katasadmin.github.io').split(',') if x.strip()]
START=datetime(2026,10,15,tzinfo=timezone.utc)
DAY_SECONDS=900 # 1 hour = 4 game days
EDUCATION={'economy':('Экономический колледж','Помощник бухгалтера'), 'medicine':('Медицинский колледж','Медицинский ассистент'), 'it':('IT-колледж','Младший разработчик'), 'law':('Юридический колледж','Помощник юриста')}

class Base(DeclarativeBase): pass
class Account(Base):
    __tablename__='accounts'
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid.uuid4()))
    email: Mapped[str]=mapped_column(String(320),unique=True,index=True)
    password_hash: Mapped[str]=mapped_column(String(255))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class Character(Base):
    __tablename__='characters'
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid.uuid4()))
    account_id: Mapped[str]=mapped_column(ForeignKey('accounts.id'),index=True)
    name: Mapped[str]=mapped_column(String(60))
    education: Mapped[str]=mapped_column(String(30))
    birth_day: Mapped[int]=mapped_column(Integer)
    last_settled_day: Mapped[int]=mapped_column(Integer)
    wallet_kop: Mapped[int]=mapped_column(BigInteger,default=15000)
    job: Mapped[str|None]=mapped_column(String(80),nullable=True)
    reputation: Mapped[int]=mapped_column(Integer,default=50)
    work_reputation: Mapped[int]=mapped_column(Integer,default=50)
    family_id: Mapped[str|None]=mapped_column(String(36),nullable=True)
    status: Mapped[str]=mapped_column(String(20),default='alive')
class Ledger(Base):
    __tablename__='ledger'
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid.uuid4()))
    character_id: Mapped[str]=mapped_column(ForeignKey('characters.id'),index=True)
    day: Mapped[int]=mapped_column(Integer)
    kind: Mapped[str]=mapped_column(String(30))
    title: Mapped[str]=mapped_column(String(120))
    amount: Mapped[int]=mapped_column(BigInteger)
    __table_args__=(UniqueConstraint('character_id','day','kind',name='unique_daily_entry'),)

engine=create_engine(DATABASE_URL,connect_args={'check_same_thread':False} if DATABASE_URL.startswith('sqlite:') else {},pool_pre_ping=True)
SessionLocal=sessionmaker(bind=engine)
@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine) # prototype only: migrations via Alembic before production
    yield
app=FastAPI(title='НОВАЯ ЖИЗНЬ API',version='0.2',lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=ORIGINS,allow_credentials=False,allow_methods=['GET','POST'],allow_headers=['Authorization','Content-Type'])
security=HTTPBearer()
passwords=PasswordHash.recommended()
def session():
    with SessionLocal() as db: yield db
def current_account(creds:HTTPAuthorizationCredentials=Depends(security),db:Session=Depends(session)):
    try:
        payload=jwt.decode(creds.credentials,SECRET,algorithms=['HS256']); aid=payload['sub']
    except Exception: raise HTTPException(401,'Нужно войти')
    account=db.get(Account,aid)
    if not account: raise HTTPException(401,'Аккаунт не найден')
    return account
class Credentials(BaseModel):
    email:EmailStr
    password:str=Field(min_length=8,max_length=128)
class NewCharacter(BaseModel):
    name:str=Field(min_length=2,max_length=50)
    education:str

def day_now(): return max(1,1+int((datetime.now(timezone.utc)-START).total_seconds()//DAY_SECONDS))
def entry(db,c,day,kind,title,amount):
    if db.scalar(select(Ledger.id).where(Ledger.character_id==c.id,Ledger.day==day,Ledger.kind==kind)): return
    db.add(Ledger(character_id=c.id,day=day,kind=kind,title=title,amount=amount)); c.wallet_kop+=amount

def settle(db,c):
    now=day_now()
    # One aggregate posting per interval prevents thousands of inserts and replay exploits.
    # Distinct from one-per-day ledger entries, unique key ensures no duplicate settlement.
    if now<=c.last_settled_day: return
    start=c.last_settled_day+1
    end=now
    if c.status!='alive': c.last_settled_day=now; return
    school_end=c.birth_day+729
    school_days=max(0,min(end,school_end)-start+1)
    living_days=end-start+1
    stipend=school_days*1500
    salary=living_days*2500 if c.job else 0
    # Entries keyed by the settlement end day; unique per category.
    if stipend: entry(db,c,end,'stipend',f'Стипендия за {school_days} дн.',stipend)
    if salary: entry(db,c,end,'salary',f'Зарплата за {living_days} дн.',salary)
    entry(db,c,end,'living',f'Проживание за {living_days} дн.',-living_days*1200)
    c.last_settled_day=now

def mine(db,account):
    c=db.scalar(select(Character).where(Character.account_id==account.id).order_by(Character.birth_day.desc()))
    if c: settle(db,c); db.commit(); db.refresh(c)
    return c

def snapshot(db,c):
    if not c:return {'world':{'day':day_now()},'character':None,'ledger':[]}
    logs=db.scalars(select(Ledger).where(Ledger.character_id==c.id).order_by(Ledger.day.desc()).limit(30)).all()
    return {'world':{'day':day_now()},'character':{'id':c.id,'name':c.name,'birthDay':c.birth_day,'status':c.status,'education':c.education,'job':{'title':c.job,'salaryKop':2500} if c.job else None,'walletKop':c.wallet_kop,'reputation':{'general':c.reputation,'work':c.work_reputation,'finance':50,'social':50},'experience':0,'home':'Общежитие','familyId':c.family_id,'parents':[],'children':[],'inventory':[]},'ledger':[{'id':l.id,'characterId':l.character_id,'day':l.day,'amount':l.amount,'kind':l.kind,'title':l.title} for l in logs]}

@app.get('/health')
def health(): return {'ok':True}
@app.post('/auth/register')
def register(payload:Credentials,db:Session=Depends(session)):
    email=str(payload.email).lower()
    if db.scalar(select(Account.id).where(Account.email==email)): raise HTTPException(409,'Email уже зарегистрирован')
    a=Account(email=email,password_hash=passwords.hash(payload.password));db.add(a);db.commit();db.refresh(a)
    return {'token':jwt.encode({'sub':a.id,'exp':datetime.now(timezone.utc)+timedelta(days=14)},SECRET,algorithm='HS256')}
@app.post('/auth/login')
def login(payload:Credentials,db:Session=Depends(session)):
    a=db.scalar(select(Account).where(Account.email==str(payload.email).lower()))
    if not a or not passwords.verify(payload.password,a.password_hash):raise HTTPException(401,'Неверная почта или пароль')
    return {'token':jwt.encode({'sub':a.id,'exp':datetime.now(timezone.utc)+timedelta(days=14)},SECRET,algorithm='HS256')}
@app.get('/me')
def me(a:Account=Depends(current_account),db:Session=Depends(session)):return snapshot(db,mine(db,a))
@app.post('/characters')
def create_character(payload:NewCharacter,a:Account=Depends(current_account),db:Session=Depends(session)):
    if payload.education not in EDUCATION:raise HTTPException(422,'Неизвестный колледж')
    if db.scalar(select(Character.id).where(Character.account_id==a.id)):raise HTTPException(409,'Персонаж уже создан')
    c=Character(account_id=a.id,name=payload.name.strip(),education=payload.education,birth_day=day_now(),last_settled_day=day_now());db.add(c);db.commit();return snapshot(db,c)
@app.post('/work/hire')
def hire(a:Account=Depends(current_account),db:Session=Depends(session)):
    c=mine(db,a)
    if not c:raise HTTPException(404,'Нет персонажа')
    if day_now()-c.birth_day<730:raise HTTPException(400,'Сначала закончите колледж')
    if c.job:raise HTTPException(409,'Вы уже работаете')
    c.job=EDUCATION[c.education][1];c.reputation=min(100,c.reputation+1);c.work_reputation=min(100,c.work_reputation+2);db.commit();return snapshot(db,c)
@app.post('/shop/food')
def buy_food(a:Account=Depends(current_account),db:Session=Depends(session)):
    c=mine(db,a)
    if not c:raise HTTPException(404,'Нет персонажа')
    if c.wallet_kop<800:raise HTTPException(400,'Недостаточно денег')
    # Demo purchase only. Inventory/nutrition not yet implemented.
    entry(db,c,day_now(),f'food-{uuid.uuid4()}','Продукты (демо)',-800)
    db.commit();return snapshot(db,c)
