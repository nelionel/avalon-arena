"""Codex app-server transport and per-seat filesystem profiles.

Uses the signed-in local Codex client. Never copies credentials into a seat.
"""
from __future__ import annotations
import asyncio,json,os,pathlib,shutil,signal
from ..persistence import process_identity, write_json
try:
    import tomllib
except ImportError:
    tomllib = None

def codex_binary():
    explicit=os.environ.get("AVALON_CODEX_BINARY")
    if explicit:return explicit
    bundled="/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
    if pathlib.Path(bundled).is_file():return bundled
    found=shutil.which("codex")
    if not found:raise RuntimeError("Codex CLI is unavailable on this host")
    return found

class CodexRPC:
    def __init__(self,cwd,read_roots,write_roots,log_path,callback=None):
        self.cwd=pathlib.Path(cwd).resolve();self.read_roots=read_roots;self.write_roots=write_roots
        self.log_path=pathlib.Path(log_path);self.callback=callback;self.pending={};self.counter=0
        self.proc=None;self.reader=None;self.stderr=None;self.profile="avalon_seat"
        self.closing=False
    async def start(self):
        args=[codex_binary(),"app-server","--stdio"]
        def toml(v):
            if isinstance(v,bool):return str(v).lower()
            if isinstance(v,dict):return "{"+", ".join(json.dumps(str(k))+" = "+toml(x) for k,x in v.items())+"}"
            return json.dumps(v)
        def cfg(k,v):
            args.extend(["-c",k+"="+toml(v)])
        for feature in ["apps","plugins","computer_use","browser_use","browser_use_external","in_app_browser",
                        "image_generation","multi_agent","multi_agent_v2","memories","hooks","code_mode"]:
            cfg("features."+feature,False)
        cfg("features.code_mode_host",True)
        cfg("features.shell_tool",True)
        cfg("features.unified_exec",True)
        cfg("web_search","disabled")
        # Server-specific overrides; never change the user's saved configuration.
        config=pathlib.Path.home()/".codex/config.toml"
        if config.exists():
            if tomllib is None:raise RuntimeError("The Codex backend requires Python 3.11 or newer")
            original=tomllib.loads(config.read_text()).get("mcp_servers",{})
            disabled={}
            for name,server in original.items():
                transport={k:server[k] for k in ["url","command"] if k in server}
                if "url" in transport:
                    from urllib.parse import urlsplit,urlunsplit
                    u=urlsplit(transport["url"])
                    # Disabled transport still needs a valid shape; never put URL credentials on argv.
                    transport["url"]=urlunsplit((u.scheme,u.hostname+(":"+str(u.port) if u.port else ""),u.path,"",""))
                disabled[name]={**transport,"enabled":False}
            cfg("mcp_servers",disabled)
        prefix="permissions."+self.profile
        fs={":minimal":"read"}
        for path in self.read_roots:fs[str(pathlib.Path(path).resolve())]="read"
        for path in self.write_roots:fs[str(pathlib.Path(path).resolve())]="write"
        cfg(prefix,{"description":"One Avalon seat: own workspace plus public record", "filesystem":fs,"network":{"enabled":False}})
        cfg("default_permissions",self.profile)
        self.log_path.parent.mkdir(parents=True,exist_ok=True)
        profile_path=self.log_path.with_suffix(".profile.json")
        if not profile_path.exists():
            profile_path.write_text(json.dumps({"profile":self.profile,"filesystem":fs,"network_enabled":False,"binary":codex_binary()},indent=2))
        self.stderr=open(self.log_path,"a")
        self.proc=await asyncio.create_subprocess_exec(*args,cwd=self.cwd,stdin=asyncio.subprocess.PIPE,
             stdout=asyncio.subprocess.PIPE,stderr=self.stderr)
        write_json(self.log_path.with_suffix(".process.json"),
                   {"process":process_identity(self.proc.pid),"cwd":str(self.cwd)})
        self.reader=asyncio.create_task(self._read())
        info=await self.call("initialize",{"clientInfo":{"name":"avalon_local","title":"Avalon players","version":"0.1.0"},
                                           "capabilities":{"experimentalApi":True}})
        await self.send({"method":"initialized","params":{}})
        return info
    async def send(self,msg):
        self.proc.stdin.write((json.dumps(msg)+"\n").encode());await self.proc.stdin.drain()
    async def call(self,method,params,timeout=90):
        self.counter+=1;i=self.counter;f=asyncio.get_running_loop().create_future();self.pending[i]=f
        await self.send({"id":i,"method":method,"params":params})
        try:return await asyncio.wait_for(f,timeout)
        finally:self.pending.pop(i,None)
    async def _read(self):
        try:
            while line:=await self.proc.stdout.readline():
                try:msg=json.loads(line)
                except json.JSONDecodeError:continue
                if "id" in msg and "method" not in msg:
                    f=self.pending.get(msg["id"])
                    if f and not f.done():
                        if "error" in msg:f.set_exception(RuntimeError(json.dumps(msg["error"])))
                        else:f.set_result(msg.get("result",{}))
                elif "id" in msg:
                    # No player may grant new permissions or request user interaction.
                    await self.send({"id":msg["id"],"error":{"code":-32601,"message":"Unavailable in an Avalon seat"}})
                    if self.callback:self.callback({"method":"avalon/requestDenied","params":msg})
                elif self.callback:self.callback(msg)
        finally:
            for f in self.pending.values():
                if not f.done():f.set_exception(RuntimeError("Codex app-server closed"))
            if self.callback:
                self.callback({"method":"avalon/serverStopped" if self.closing else "avalon/serverClosed","params":{"expected":self.closing}})
    async def command(self,argv):
        return await self.call("command/exec",{"command":argv,"cwd":str(self.cwd),
             "permissionProfile":self.profile,"timeoutMs":15000,"outputBytesCap":8192})
    @staticmethod
    async def stop_orphan(log_path, expected_cwd):
        """Only after acquiring the game lease; never use a PID alone as ownership."""
        path=pathlib.Path(log_path).with_suffix(".process.json")
        if not path.exists():return
        saved=json.loads(path.read_text());owned=saved.get("process")
        if not owned:return
        live=process_identity(owned["pid"])
        if live!=owned:return  # gone, or PID reused by another process
        cwd=str(pathlib.Path(expected_cwd).resolve())
        if saved.get("cwd")!=cwd or cwd not in live["identity"] or "app-server" not in live["identity"]:
            raise RuntimeError("Cannot verify ownership of the old player process")
        os.kill(owned["pid"],signal.SIGTERM)
        for _ in range(50):
            if process_identity(owned["pid"])!=owned:return
            await asyncio.sleep(.2)
        raise RuntimeError("Old player process has not stopped; refusing concurrent player sessions")
    async def close(self):
        self.closing=True
        if self.proc:
            if self.proc.stdin and not self.proc.stdin.is_closing():self.proc.stdin.close()
            try:await asyncio.wait_for(self.proc.wait(),10)
            except asyncio.TimeoutError:
                self.proc.terminate()
                try:await asyncio.wait_for(self.proc.wait(),5)
                except asyncio.TimeoutError:self.proc.kill();await self.proc.wait()
        if self.reader:
            try:await self.reader
            except asyncio.CancelledError:pass
        if self.stderr:self.stderr.close()

