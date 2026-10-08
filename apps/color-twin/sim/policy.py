"""Small deterministic Gaussian-process BO over quantized three-color mixtures."""
import math
import random
from .defaults import protocol_for, STOCKS
from .runtime import execute, distance
import yaml


def kernel(a,b):
    return math.exp(-sum((x-y)**2 for x,y in zip(a,b))/(2*.3**2))


def cholesky(a):
    n=len(a); l=[[0.0]*n for _ in a]
    for i in range(n):
        for j in range(i+1):
            v=a[i][j]-sum(l[i][k]*l[j][k] for k in range(j))
            l[i][j]=math.sqrt(max(v,1e-12)) if i==j else v/l[j][j]
    return l


def forward(l,b):
    x=[]
    for i in range(len(b)):
        x.append((b[i]-sum(l[i][j]*x[j] for j in range(i)))/l[i][i])
    return x


def solve(l,b):
    y=forward(l,b); x=[0.0]*len(b)
    for i in range(len(b)-1,-1,-1):
        x[i]=(y[i]-sum(l[j][i]*x[j] for j in range(i+1,len(b))))/l[i][i]
    return x


def suggest(history, *, total=300, seed=7, initial=4, acquisition='ei', exploration=.05):
    rng=random.Random(seed+len(history)*7919)
    if total != 300:
        raise ValueError('The color experiment requires exactly 300 µL per well.')
    seen = {tuple(h['volumes']) for h in history}
    # Enumerate the complete feasible 5 µL grid; every color is 50–200 µL.
    candidates = [(red, yellow, total-red-yellow)
                  for red in range(50, 201, 5)
                  for yellow in range(50, 201, 5)
                  if 50 <= total-red-yellow <= 200
                  and (red, yellow, total-red-yellow) not in seen]
    if not candidates: raise ValueError('All candidate mixtures have been evaluated.')
    if len(history)<initial or acquisition=='random':
        return list(rng.choice(candidates)), 'Initial design' if len(history)<initial else 'Random baseline'
    xs=[[v/total for v in h['volumes']] for h in history]
    values=[h['loss']/100 for h in history]
    mean=sum(values)/len(values)
    scale=max(.02,math.sqrt(sum((v-mean)**2 for v in values)/len(values)))
    ys=[(v-mean)/scale for v in values]
    l=cholesky([[kernel(a,b)+(1e-5 if i==j else 0) for j,b in enumerate(xs)] for i,a in enumerate(xs)])
    alpha=solve(l,ys)
    best=min(ys); scored=[]
    for candidate in candidates:
        x=[v/total for v in candidate]; k=[kernel(x,a) for a in xs]
        mu=sum(a*b for a,b in zip(k,alpha)); projected=forward(l,k)
        sigma=math.sqrt(max(1e-10,1-sum(v*v for v in projected)))
        if acquisition=='ei':
            improve=best-mu-exploration; z=improve/sigma
            score=improve*.5*(1+math.erf(z/math.sqrt(2)))+sigma*math.exp(-z*z/2)/math.sqrt(2*math.pi)
        else:
            score=-mu+(1+exploration*5)*sigma
        scored.append((score,candidate))
    return list(max(scored)[1]), 'Expected improvement' if acquisition=='ei' else 'Lower confidence bound'


def campaign(gantry_yaml,deck_yaml,policy):
    history=[]; steps=[]; result=None; stop_reason=None
    stock_start = min(4000, yaml.safe_load(deck_yaml)['labware']['stocks']['working_volume_ul'])
    for trial in range(policy.trials):
        volumes,reason=suggest(history,total=policy.total,seed=policy.seed,initial=policy.initial,
                               acquisition=policy.acquisition,exploration=policy.exploration)
        if trial * 3 + 3 > 96:
            stop_reason = 'Tip rack exhausted. Load a fresh rack to continue.'
            break
        depleted = [stock['name'] for index, stock in enumerate(STOCKS)
                    if sum(h['volumes'][index] for h in history) + volumes[index] > stock_start + 1e-7]
        if depleted:
            stop_reason = f"Replenish {', '.join(depleted)} stock before the next proposed trial."
            break
        well=f'{chr(65+trial//12)}{trial%12+1}'
        from .side_exit import protocol as side_protocol
        generate = side_protocol if yaml.safe_load(deck_yaml)['labware']['tips'].get('side_exit') else protocol_for
        steps.extend(yaml.safe_load(generate(volumes,well,trial*3))['protocol'])
        text='# SIMULATION ONLY — generated from synthetic BO observations.\n'+yaml.safe_dump({'protocol':steps},sort_keys=False)
        result=execute(gantry_yaml,deck_yaml,text)
        observed=result['fluids'][f'plate.{well}']['color']
        history.append({'trial':trial+1,'well':well,'volumes':volumes,'color':observed,
                        'loss':distance(observed,policy.target),'reason':reason})
    if result is None:
        raise ValueError(stop_reason or 'No trial could be executed.')
    return {**result,'requested_trials':policy.trials,'stop_reason':stop_reason,'history':history,'protocol_yaml':text,'policy':policy.model_dump()}
