"""Standalone localhost service. Deliberately imports no hardware API routers."""
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from .defaults import bundle
from .runtime import execute
from .policy import campaign

app=FastAPI(title='CubOS Color Twin — simulation only')

class YamlInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    gantry_yaml: str=Field(max_length=200000)
    deck_yaml: str=Field(max_length=200000)
    protocol_yaml: str=Field(default='',max_length=200000)

class Policy(BaseModel):
    model_config=ConfigDict(extra='forbid')
    target: str=Field(default='#b77762',pattern=r'^#[0-9a-fA-F]{6}$')
    trials: int=Field(default=8,ge=1,le=96)
    initial: int=Field(default=4,ge=1,le=12)
    total: Literal[300]=300
    acquisition: Literal['ei','lcb','random']='ei'
    exploration: float=Field(default=.05,ge=0,le=1,allow_inf_nan=False)
    seed: int=Field(default=7,ge=0,le=1000000)

class CampaignInput(YamlInput):
    policy: Policy=Field(default_factory=Policy)

@app.get('/api/twin/defaults')
def defaults(profile: str = ''):
    if profile == 'side-exit':
        from .side_exit import bundle as side_bundle
        return side_bundle()
    return bundle()

@app.post('/api/twin/simulate')
def simulate(body: YamlInput):
    try: return execute(body.gantry_yaml,body.deck_yaml,body.protocol_yaml)
    except Exception as exc:
        raise HTTPException(422,detail=f'{type(exc).__name__}: {exc}') from exc

@app.post('/api/twin/campaign')
def optimize(body: CampaignInput):
    try: return campaign(body.gantry_yaml,body.deck_yaml,body.policy)
    except Exception as exc:
        raise HTTPException(422,detail=f'{type(exc).__name__}: {exc}') from exc

DIST=Path(__file__).resolve().parents[1]/'dist'
if DIST.is_dir(): app.mount('/',StaticFiles(directory=DIST,html=True),name='ui')
