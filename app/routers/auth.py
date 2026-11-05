from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_db
from app.core.security import current_user, make_token, password_check, password_hash
from app.models.entities import Company, User
from app.schemas.api import Signup, Login, ok
from app.services.common import message, onboarding_conversation

router = APIRouter(prefix='/auth', tags=['auth'])


@router.post('/signup', status_code=201)
async def signup(body: Signup, db: AsyncSession = Depends(get_db)):
    email = body.email.strip().lower()
    if '@' not in email or '.' not in email.split('@')[-1]:
        raise HTTPException(422, detail={'code': 'VALIDATION_ERROR', 'message': 'Invalid email'})
    if await db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, detail={'code': 'EMAIL_EXISTS', 'message': 'Email already registered'})
    user = User(email=email, name=body.name.strip(), password_hash=password_hash(body.password))
    db.add(user)
    await db.flush()
    company_id = None
    if body.company_name:
        company = Company(owner_id=user.id, name=body.company_name.strip())
        db.add(company)
        await db.flush()
        await onboarding_conversation(db, company.id)
        await message(db, company.id, 'assistant', 'welcome',
                      {'text': 'Welcome to Coirei. Connect your website or add a document to start.',
                       'next_action': 'add_context'})
        company_id = company.id
    await db.commit()
    return ok({'access_token': make_token(user.id), 'token_type': 'bearer',
               'user': {'id': user.id, 'name': user.name, 'email': user.email},
               'company_id': company_id})


@router.post('/login')
async def login(body: Login, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if not user or not password_check(body.password, user.password_hash):
        raise HTTPException(401, detail={'code': 'INVALID_CREDENTIALS', 'message': 'Wrong email or password'})
    return ok({'access_token': make_token(user.id), 'token_type': 'bearer',
               'user': {'id': user.id, 'name': user.name, 'email': user.email}})


@router.get('/me')
async def me(user: User = Depends(current_user)):
    return ok({'id': user.id, 'email': user.email, 'name': user.name})
