from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class Signup(BaseModel):
    email: str = Field(min_length=4, max_length=320)
    name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=10, max_length=256)
    company_name: str | None = Field(default=None, max_length=255)


class Login(BaseModel):
    email: str
    password: str


class CreateCompany(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    website_url: str | None = Field(default=None, max_length=2048)


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    website_url: str | None = None
    candidate_kind: Literal['standalone_product','suite','service'] = 'standalone_product'


class CompanyPatch(BaseModel):
    revision: int = Field(ge=1)
    fields: dict[str, Any] = Field(default_factory=dict)
    accept_fields: list[str] = Field(default_factory=list)


class ProductPatch(BaseModel):
    revision: int = Field(ge=1)
    fields: dict[str, Any] = Field(default_factory=dict)
    accept_fields: list[str] = Field(default_factory=list)


class VerifyProduct(BaseModel):
    revision: int = Field(ge=1)
    status: Literal['verified'] = 'verified'
    accept_fields: list[str] = Field(default_factory=list)



class GapAnswerItem(BaseModel):
    question_id: int
    answer: str | None = Field(default=None, max_length=5000)
    skip: bool = False

    @model_validator(mode='after')
    def _answer_required_unless_skipped(self):
        if not self.skip and not (self.answer or '').strip():
            raise ValueError('answer is required unless skip is true')
        return self


class GapAnswers(BaseModel):
    answers: list[GapAnswerItem] = Field(min_length=1, max_length=4)


class ChatInput(BaseModel):
    message: str | None = Field(default=None, max_length=4000)
    client_message_id: str | None = Field(default=None, max_length=100)
    action: Literal['upload_documents', 'answer_questions'] | None = None

    @model_validator(mode='after')
    def _message_or_action(self):
        if not self.action and not (self.message or '').strip():
            raise ValueError('message is required unless action is set')
        return self


COMPANY_EDITABLE = {'name','website_url','description','industry','size_band','headquarters',
                    'country','employee_count','founded_year','business_model','market_segment',
                    'value_proposition','icp','socials'}
PRODUCT_EDITABLE = {'name','website_url','candidate_kind','category','product_type','description',
                    'key_features','main_use_case','business_model','pricing_status','positioning',
                    'value_proposition','target_market','icp','pricing'}
PRODUCT_KINDS = {'standalone_product','suite','module','feature','service','integration','unknown'}


def ok(data: Any, **extra):
    return {'success': True, 'status': 'success', 'data': data, **extra}
