"""API humaine des budgets ; aucune capacité technique ne vaut décision CP."""
from decimal import Decimal
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Header, Response
from pydantic import Field, model_validator
from app.agent_api import db_session, human
from app.runtime_api import Input, Scope, authorize, mutation as runtime_mutation
from app import budget_service as service
from app.memory_api import MemoryRoute, private_response

async def mutation(db, project_id, actor, callback):
    return await runtime_mutation(db, project_id, actor, callback, event='budget.mutation')


router = APIRouter(prefix='/api/projects/{project_id}/budgets', tags=['budgets'],
    route_class=MemoryRoute, dependencies=[Depends(private_response)])


class BudgetInput(Input):
    scope: Scope
    scope_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(default=0, ge=0, strict=True)
    period: Literal['daily','weekly','monthly']
    metric: Literal['tokens','seconds','cost']
    amount: Decimal = Field(gt=0, le=1_000_000_000, max_digits=16, decimal_places=6)
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    warning_percent: int = Field(default=80, ge=1, le=99, strict=True)
    enabled: bool = True

    @model_validator(mode='after')
    def valid_metric(self):
        if (self.metric == 'cost') != (self.currency is not None):
            raise ValueError('Le coût exige une devise ; les autres métriques n’en ont pas.')
        if self.metric == 'tokens' and self.amount != self.amount.to_integral_value():
            raise ValueError('Les tokens doivent être entiers.')
        return self


@router.get('')
async def home(project_id:str, response:Response, db=Depends(db_session), actor=Depends(human)):
    await authorize(db,actor,project_id)
    response.headers['Cache-Control']='no-store'
    return await service.snapshot(db,project_id)


@router.post('/versions', status_code=201)
async def revise(project_id:str, payload:BudgetInput, x_csrf_token:str|None=Header(default=None),
                 db=Depends(db_session), actor=Depends(human)):
    await authorize(db,actor,project_id,x_csrf_token,'budget.configure')
    return await mutation(db,project_id,actor,lambda:service.revise(db,project_id,actor[0],payload))


@router.post('/evaluate')
async def evaluate(project_id:str, x_csrf_token:str|None=Header(default=None),
                   db=Depends(db_session), actor=Depends(human)):
    await authorize(db,actor,project_id,x_csrf_token,'budget.configure')
    await service.evaluate(db,project_id)
    await db.commit()
    return await service.snapshot(db,project_id)


@router.post('/alerts/{identity}/acknowledge')
async def acknowledge(project_id:str, identity:UUID, payload:Input, x_csrf_token:str|None=Header(default=None),
                      db=Depends(db_session), actor=Depends(human)):
    await authorize(db,actor,project_id,x_csrf_token,'budget.configure')
    return await mutation(db,project_id,actor,lambda:service.acknowledge(db,project_id,str(identity),actor[0],payload.reason))


@router.post('/suspensions/{identity}/resume')
async def resume(project_id:str, identity:UUID, payload:Input, x_csrf_token:str|None=Header(default=None),
                 db=Depends(db_session), actor=Depends(human)):
    await authorize(db,actor,project_id,x_csrf_token,'agent.run.manage')
    return await mutation(db,project_id,actor,lambda:service.resume(db,project_id,str(identity),actor[0],payload.reason))
