#!/usr/bin/env python3
"""Real TCP + RFC6455 + VLESS stress test; no third-party packages required."""
from __future__ import annotations
import asyncio, base64, collections, hashlib, logging, os, struct, sys, time, types
from pathlib import Path

ROOT=Path(sys.argv[1] if len(sys.argv)>1 else Path(__file__).resolve().parents[1])
CLIENTS=int(sys.argv[2]) if len(sys.argv)>2 else 16
MIB=int(sys.argv[3]) if len(sys.argv)>3 else 8
UID='11111111-2222-3333-4444-555555555555'; GUID='258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

fa=types.ModuleType('fastapi')
class WebSocketDisconnect(Exception):
    def __init__(self,code=1000): self.code=code
class WebSocket: pass
fa.WebSocket=WebSocket; fa.WebSocketDisconnect=WebSocketDisconnect; sys.modules['fastapi']=fa
m=types.ModuleType('main'); m.LINKS={UID:{'label':'stress','used_bytes':0,'limit_bytes':0,'speed_limit_bytes':0,'active':True}}
m.LINKS_LOCK=asyncio.Lock(); m.stats=collections.defaultdict(int); m.hourly_traffic=collections.defaultdict(int)
m.connections={}; m.error_logs=[]; m.logger=logging.getLogger('stress')
m.is_link_allowed=lambda x: bool(x and x.get('active',True)); m.is_ip_allowed=lambda *a: True
async def save(): pass
m.save_state=save; m.log_activity=lambda *a,**k: None
import datetime; m.now_ir=datetime.datetime.now; sys.modules['main']=m; sys.path.insert(0,str(ROOT))
import relay_vless as R

def head(n,masked=False,opcode=2):
    mask=0x80 if masked else 0
    if n<126:return bytes((0x80|opcode,mask|n))
    if n<65536:return bytes((0x80|opcode,mask|126))+struct.pack('!H',n)
    return bytes((0x80|opcode,mask|127))+struct.pack('!Q',n)

async def frame(reader):
    a,b=await reader.readexactly(2); op=a&15; masked=b&128; n=b&127
    if n==126:n=struct.unpack('!H',await reader.readexactly(2))[0]
    elif n==127:n=struct.unpack('!Q',await reader.readexactly(8))[0]
    key=await reader.readexactly(4) if masked else b''; data=await reader.readexactly(n)
    if masked and key!=b'\0\0\0\0':
        out=bytearray(data)
        for i in range(n):out[i]^=key[i&3]
        data=bytes(out)
    return op,data

class NetWS:
    def __init__(self,r,w,h):
        self.r=r; self.w=w; self.headers=h; self.scope={}; self.client=types.SimpleNamespace(host='127.0.0.1')
    async def accept(self,subprotocol=None):
        value=base64.b64encode(hashlib.sha1((self.headers['sec-websocket-key']+GUID).encode()).digest()).decode()
        self.w.write(f'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {value}\r\n\r\n'.encode()); await self.w.drain()
    async def receive(self):
        try:
            while True:
                op,data=await frame(self.r)
                if op==2:return {'type':'websocket.receive','bytes':data}
                if op==8:return {'type':'websocket.disconnect','code':1000}
        except (asyncio.IncompleteReadError,ConnectionError):return {'type':'websocket.disconnect','code':1006}
    async def send_bytes(self,data):
        self.w.write(head(len(data))); self.w.write(data)
        if self.w.transport.get_write_buffer_size()>=8*1024*1024:await self.w.drain()
    async def close(self,code=1000,reason=''):
        data=struct.pack('!H',code)+reason.encode(); self.w.write(head(len(data),opcode=8)); self.w.write(data); await self.w.drain()

def vless(port,payload=b''):
    return b'\0'+bytes(16)+b'\0\1'+port.to_bytes(2,'big')+b'\1\x7f\0\0\1'+payload

async def echo_server():
    async def echo(r,w):
        try:
            while data:=await r.read(2*1024*1024):
                w.write(data)
                if w.transport.get_write_buffer_size()>=8*1024*1024:await w.drain()
            await w.drain()
        except Exception:pass
        w.close()
    s=await asyncio.start_server(echo,'127.0.0.1',0,limit=16*1024*1024); return s,s.sockets[0].getsockname()[1]

async def gateway_server():
    async def gateway(r,w):
        try:
            request=await r.readuntil(b'\r\n\r\n'); lines=request.decode('latin1').split('\r\n'); h={}
            for line in lines[1:]:
                if ':' in line:
                    k,v=line.split(':',1); h[k.strip().lower()]=v.strip()
            uid=lines[0].split()[1].split('/ws/',1)[1].split('?',1)[0]
            await R.websocket_tunnel(NetWS(r,w,h),uid)
        except Exception as exc:m.error_logs.append({'gateway':repr(exc)})
        w.close()
    s=await asyncio.start_server(gateway,'127.0.0.1',0,limit=16*1024*1024); return s,s.sockets[0].getsockname()[1]

async def connect(port,early=b''):
    r,w=await asyncio.open_connection('127.0.0.1',port,limit=16*1024*1024); key=base64.b64encode(os.urandom(16)).decode()
    lines=[f'GET /ws/{UID}?ed=4096 HTTP/1.1',f'Host: 127.0.0.1:{port}','Upgrade: websocket','Connection: Upgrade',f'Sec-WebSocket-Key: {key}','Sec-WebSocket-Version: 13']
    if early:lines.append('Sec-WebSocket-Protocol: '+base64.urlsafe_b64encode(early).decode().rstrip('='))
    w.write(('\r\n'.join(lines)+'\r\n\r\n').encode()); await w.drain(); response=await r.readuntil(b'\r\n\r\n'); assert response.startswith(b'HTTP/1.1 101')
    return r,w

async def case(gw,target,size,token,mode='normal'):
    prefix=b'seed'+bytes((token,)); vh=vless(target,prefix); r,w=await connect(gw,vh if mode=='early' else b'')
    if mode!='early':
        parts=(vh[:8],vh[8:20],vh[20:]) if mode=='split' else (vh,)
        for p in parts:w.write(head(len(p),True)+b'\0\0\0\0'+p)
    chunk=bytes((token,))*262144; expected=prefix+bytes((token,))*size
    async def up():
        left=size
        while left:
            p=chunk if left>=len(chunk) else chunk[:left]; w.write(head(len(p),True)); w.write(b'\0\0\0\0'); w.write(p); left-=len(p)
            if w.transport.get_write_buffer_size()>=8*1024*1024:await w.drain()
        await w.drain()
    async def down():
        op,hdr=await frame(r); assert op==2 and hdr==b'\0\0'; out=bytearray()
        while len(out)<len(expected):
            op,p=await frame(r); assert op==2; out.extend(p)
        assert bytes(out)==expected
    await asyncio.gather(up(),down()); w.write(head(2,True,8)+b'\0\0\0\0\x03\xe8'); await w.drain(); w.close(); await w.wait_closed()
    return len(expected)

async def main():
    target,tp=await echo_server(); gateway,gp=await gateway_server()
    try:
        for i,mode in enumerate(('normal','split','early'),1):
            got=await asyncio.wait_for(case(gp,tp,1024*1024,i,mode),30); print(f'correctness {mode}: {got}B OK')
        size=MIB*1024*1024; start=time.perf_counter()
        totals=await asyncio.wait_for(asyncio.gather(*[case(gp,tp,size,i+10) for i in range(CLIENTS)]),180)
        elapsed=time.perf_counter()-start; total=sum(totals)/1048576; await asyncio.sleep(.1)
        assert not m.connections and not m.error_logs,(m.connections,m.error_logs)
        print(f'stress {CLIENTS}x{MIB}MiB: {total:.1f}MiB in {elapsed:.3f}s = {total/elapsed:.1f}MiB/s each-way; leaks=0 errors=0')
    finally:
        target.close();gateway.close();await target.wait_closed();await gateway.wait_closed()
asyncio.run(main())
