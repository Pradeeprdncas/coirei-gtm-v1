from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.core.config import settings
from app.routers import auth, companies, onboarding, products
from app.services.llm import llm_health
from app.routers import projects

from app.routers import competitors

@asynccontextmanager
async def lifespan(app: FastAPI):
    secret = settings().jwt_secret
    if len(secret) < 32 or secret.startswith('REPLACE_WITH_'):
        raise RuntimeError('Set a strong JWT_SECRET in .env before running the API')
    yield


app = FastAPI(title='Coirei Simple Onboarding API', version='0.1.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[v.strip() for v in settings().cors_origins.split(',')],
                   allow_credentials=True, allow_methods=['GET','POST','PATCH','OPTIONS'],
                   allow_headers=['Authorization','Content-Type'])


@app.exception_handler(HTTPException)
async def on_http_error(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        err = {'code': exc.detail.get('code', 'ERROR'), 'message': exc.detail.get('message', 'Request failed'),
               'details': exc.detail.get('details', {})}
    else:
        err = {'code': 'ERROR', 'message': str(exc.detail), 'details': {}}
    return JSONResponse(status_code=exc.status_code, content={'success': False, 'error': err})


@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError):
    # Omit raw request body or input fields (could contain passwords).
    details = [{'loc': [str(x) for x in e.get('loc',[])], 'msg': e.get('msg','invalid')} for e in exc.errors()]
    return JSONResponse(status_code=422, content={'success': False, 'error': {
        'code': 'VALIDATION_ERROR', 'message': 'Invalid request', 'details': {'fields': details}}})


@app.get('/health')
async def health():
    return {'status': 'ok'}


@app.get('/health/llm')
async def health_llm():
    return await llm_health()


app.include_router(auth.router)
app.include_router(companies.router)
app.include_router(onboarding.router)
app.include_router(products.router)


app.include_router(projects.router)

app.include_router(competitors.router)

