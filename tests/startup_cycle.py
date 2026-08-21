#!/usr/bin/env python3
"""Reproduce `python main.py` startup with dependency stubs and catch import cycles."""
import asyncio, json, pathlib, sys, types
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

class App:
    def __init__(self,*a,**k): self.routes=[]
    def _decorator(self,*a,**k): return lambda fn: fn
    on_event=get=post=patch=delete=api_route=_decorator
    def add_middleware(self,*a,**k): pass
    def add_api_websocket_route(self,*a,**k): pass
    def include_router(self,*a,**k): pass
class HTTPException(Exception):
    def __init__(self,status_code=500,detail=''): self.status_code=status_code; self.detail=detail
class Dummy: pass
class WebSocketDisconnect(Exception): pass
fa=types.ModuleType('fastapi'); fa.FastAPI=App; fa.Request=Dummy; fa.HTTPException=HTTPException
fa.WebSocket=Dummy; fa.WebSocketDisconnect=WebSocketDisconnect; fa.Depends=lambda x:x
responses=types.ModuleType('fastapi.responses')
for n in ['Response','HTMLResponse','JSONResponse','RedirectResponse','StreamingResponse']:
    setattr(responses,n,type(n,(),{'__init__':lambda self,*a,**k:None}))
cors=types.ModuleType('fastapi.middleware.cors'); cors.CORSMiddleware=Dummy
sys.modules.update({'fastapi':fa,'fastapi.responses':responses,'fastapi.middleware':types.ModuleType('fastapi.middleware'),'fastapi.middleware.cors':cors})
sys.modules['aiofiles']=types.ModuleType('aiofiles')
httpx=types.ModuleType('httpx'); httpx.AsyncClient=Dummy; httpx.Limits=Dummy; httpx.Timeout=Dummy; sys.modules['httpx']=httpx

uvicorn=types.ModuleType('uvicorn')
class Config:
    def __init__(self,*a,**k): self.args=a; self.kwargs=k
class Server:
    ran=False
    def __init__(self,c): self.config=c
    def run(self,*a,**k): Server.ran=True
uvicorn.Config=Config; uvicorn.Server=Server; sys.modules['uvicorn']=uvicorn
x=types.ModuleType('xhttp_siz10'); x.router=object(); sys.modules['xhttp_siz10']=x
t=types.ModuleType('telegram_bot')
async def noop(*a,**k): pass
t.start_bot=noop; t.stop_bot=noop; sys.modules['telegram_bot']=t
p=types.ModuleType('pages'); p.LOGIN_HTML=''; p.DASHBOARD_HTML=''; sys.modules['pages']=p

# Execute exactly as `python main.py`: module name is __main__, while relay imports `main`.
old_main=sys.modules.get('__main__'); module=types.ModuleType('__main__')
module.__file__=str(ROOT/'main.py'); module.__package__=None
for name in ['main','relay_vless','speed_limit']:
    sys.modules.pop(name,None)
sys.modules['__main__']=module
try:
    code=compile((ROOT/'main.py').read_text(encoding='utf-8'),str(ROOT/'main.py'),'exec')
    exec(code,module.__dict__)
    assert sys.modules.get('main') is module
    assert module.RELAY_BUF > 0
    extra=json.loads(module._xhttp_extra_json('auto'))
    assert extra['scMaxEachPostBytes']==4_000_000
    assert extra['scMinPostsIntervalMs']==1
    assert extra['xmux']['maxConnections']=='4-8'
    assert Server.ran
    print(f"python-main startup: alias=True RELAY_BUF={module.RELAY_BUF} xhttp-post={extra['scMaxEachPostBytes']} server.run=True OK")
finally:
    if old_main is not None: sys.modules['__main__']=old_main
