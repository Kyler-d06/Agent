#!/usr/bin/env python3
"""Core-server extension for node-aware script execution across a Tailscale/Headscale mesh."""
from __future__ import annotations
import json, os, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

MESH_TOOLS = [
    {"name":"mesh_select_node","description":"Choose a currently available node for a preinstalled script using compatibility, power, capacity and CPU load.","method":"GET","path":"/api/mesh/select","input_schema":{"type":"object","properties":{"root":{"type":"string"},"path":{"type":"string"}},"required":["root","path"]}},
    {"name":"mesh_offload_script","description":"Select a compatible available node and run a granted, preinstalled script there. Returns a job ID; use mesh_get_job for the outcome.","method":"POST","path":"/api/mesh/offload","effect":"execute","requires_confirmation":True,"input_schema":{"type":"object","properties":{"root":{"type":"string"},"path":{"type":"string"},"args":{"type":"array","items":{"type":"string"}}},"required":["root","path"]}},
    {"name":"mesh_get_job","description":"Read status and bounded output of a mesh job.","method":"GET","path":"/api/mesh/job","input_schema":{"type":"object","properties":{"node":{"type":"string"},"id":{"type":"string"}},"required":["node","id"]}},
    {"name":"mesh_nodes","description":"List execution nodes with live hardware tier, roots, and capacity. Use this before choosing where a script should run.","method":"GET","path":"/api/mesh/nodes","input_schema":{"type":"object","properties":{},"required":[]}},
    {"name":"mesh_list_scripts","description":"Browse scripts that have been made visible to the agent on a mesh node. Private owner-only scripts are omitted.","method":"GET","path":"/api/mesh/scripts","input_schema":{"type":"object","properties":{"node":{"type":"string"},"root":{"type":"string"},"path":{"type":"string"}},"required":["node"]}},
    {"name":"mesh_inspect_script","description":"Inspect a granted mesh script's estimated weight, RAM/GPU requirements, compatibility and agent permission.","method":"GET","path":"/api/mesh/script","input_schema":{"type":"object","properties":{"node":{"type":"string"},"root":{"type":"string"},"path":{"type":"string"}},"required":["node","path"]}},
    {"name":"mesh_run_script","description":"Run a script granted to the agent on an appropriate mesh node. Confirm-permission scripts require human confirmation; autonomous scripts may run without it.","method":"POST","path":"/api/mesh/run","requires_confirmation":True,"input_schema":{"type":"object","properties":{"node":{"type":"string"},"root":{"type":"string"},"path":{"type":"string"},"args":{"type":"array","items":{"type":"string"}}},"required":["node","path"]}},
    {"name":"mesh_run_autonomous_script","description":"Run a mesh script only when its owner explicitly granted autonomous permission. The core refuses visible/confirm/private scripts.","method":"POST","path":"/api/mesh/run-autonomous","input_schema":{"type":"object","properties":{"node":{"type":"string"},"root":{"type":"string"},"path":{"type":"string"},"args":{"type":"array","items":{"type":"string"}}},"required":["node","path"]}},
    {"name":"mesh_script_ideas","description":"Suggest useful script/tool ideas appropriate to a node's actual hardware tier and current script library.","method":"GET","path":"/api/mesh/ideas","input_schema":{"type":"object","properties":{"node":{"type":"string"}},"required":["node"]}},
]

class MeshPlatform:
    def __init__(self, app, get_db, ok, err, logged_tool):
        self.app, self.get_db, self.ok, self.err = app, get_db, ok, err
        self.key = os.environ.get("NODE_KEY", "")
        self._routes(logged_tool)

    def select_node(self, root, path):
        rows = [dict(r) for r in self.get_db().execute("SELECT name,status_url FROM nodes ORDER BY name LIMIT 32").fetchall()]
        def inspect(row):
            try:
                base = (row["status_url"] or "").rstrip("/")
                manifest = self._call(base, "/manifest", timeout=4)
                profile = manifest.get("node", {})
                if not manifest.get("ok") or root not in manifest.get("roots", []):
                    raise ValueError("node unavailable or root missing")
                if not profile.get("accepting_jobs", True):
                    raise ValueError("node busy or power policy prevents work")
                info = self._call(base, "/inspect", params={"root": root, "path": path}, timeout=4)
                analysis = info.get("analysis", {})
                if not info.get("ok") or analysis.get("permission") not in {"autonomous", "confirm"} or not analysis.get("compatible"):
                    raise ValueError("script missing, not granted, or incompatible")
                capacity = profile.get("capacity", manifest)
                if capacity.get("available_slots", 1) < 1:
                    raise ValueError("no free slots")
                score = 100 - float(profile.get("cpu_pct") or 0) + min(10, float(profile.get("ram_available_gb") or 0))
                return {"node": row["name"], "available": True, "score": score, "analysis": analysis}
            except Exception as exc:
                return {"node": row["name"], "available": False, "reason": str(exc)}
        with ThreadPoolExecutor(max_workers=4) as pool:
            candidates = list(pool.map(inspect, rows))
        ready = sorted((c for c in candidates if c["available"]), key=lambda c: (-c["score"], c["node"]))
        return {"selected": ready[0]["node"] if ready else None, "candidates": candidates}

    def _node(self, name):
        row=self.get_db().execute("SELECT * FROM nodes WHERE name=?",(name,)).fetchone()
        if not row: raise ValueError(f"unknown node '{name}'")
        base=(row["status_url"] or "").rstrip("/")
        if not base: raise ValueError(f"node '{name}' has no status_url/base URL")
        return dict(row), base

    def _call(self, base, path, method="GET", params=None, body=None, timeout=12):
        if not self.key: raise RuntimeError("NODE_KEY is not configured on core_server")
        url=base+path
        if params:
            url += "?" + urllib.parse.urlencode({k:v for k,v in params.items() if v not in (None,"")})
        data=json.dumps(body).encode() if body is not None else None
        req=urllib.request.Request(url,data=data,method=method,headers={"X-Node-Key":self.key,"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=timeout) as r:
            return json.loads(r.read().decode())

    def _ideas(self, manifest, entries):
        tier=manifest.get("node",{}).get("tier","edge")
        existing={e.get("name","").lower() for e in entries}
        edge=[
          ("uptime_watch.py","Monitor services/nodes and emit alerts when something goes offline."),
          ("rss_bridge.py","Poll lightweight RSS/Atom feeds and forward normalized events to core."),
          ("gpio_event_bridge.py","Turn Pi GPIO/sensor changes into event-bus messages."),
          ("lan_wake.py","Wake your laptop/desktop over LAN when heavy work is needed."),
          ("file_drop_watch.py","Watch a folder and notify/ingest when new files arrive."),
          ("network_probe.py","Periodic latency/DNS/connectivity checks from the Pi's location."),
          ("telegram_relay.py","Low-resource relay/watchdog for Telegram channels or bot notifications."),
        ]
        accelerated=[
          ("browser_research.py","Playwright research/automation task runner."),
          ("youtube_ingest.py","GPU/CPU transcription and Obsidian ingestion for YouTube."),
          ("doc_ingest.py","Convert and index PDFs/PPTX into the knowledge store."),
          ("capability_builder.py","Generate/test tools in Docker and publish them to the registry."),
          ("vision_ingest.py","Analyze screenshots/images and attach structured observations to events."),
          ("repo_maintenance.py","Clone/update/test GitHub repos and report compatibility changes."),
          ("local_model_job.py","Run queued local-model jobs when GPU/RAM pressure permits."),
        ]
        pool=edge if tier=="edge" else edge+accelerated
        return [{"name":n,"idea":d,"fit":tier} for n,d in pool if n.lower() not in existing][:12]

    def _routes(self, logged_tool):
        app=self.app
        @app.route('/api/mesh/select')
        @logged_tool('mesh_select_node')
        def mesh_select():
            from flask import request
            return self.ok(self.select_node(request.args.get('root', ''), request.args.get('path', '')))

        @app.route('/api/mesh/offload', methods=['POST'])
        @logged_tool('mesh_offload_script')
        def mesh_offload():
            from flask import request
            d = request.get_json(force=True)
            choice = self.select_node(d.get('root', ''), d.get('path', ''))
            if not choice['selected']:
                return self.err('no compatible available node', 409)
            _, base = self._node(choice['selected'])
            # Node admission rechecks capacity/power. Never retry an uncertain run
            # on another node: that could execute the same work twice.
            result = self._call(base, '/run', method='POST', body={
                'root': d.get('root', ''), 'path': d.get('path', ''), 'args': d.get('args', []), 'owner': False}, timeout=15)
            return self.ok({'node': choice['selected'], **result})

        @app.route('/api/mesh/job')
        @logged_tool('mesh_get_job')
        def mesh_job():
            from flask import request
            _, base = self._node(request.args.get('node', ''))
            return self.ok(self._call(base, '/job', params={'id': request.args.get('id', '')}))

        @app.route('/api/mesh/nodes')
        @logged_tool('mesh_nodes')
        def mesh_nodes():
            out=[]
            for r in self.get_db().execute("SELECT * FROM nodes ORDER BY name").fetchall():
                d=dict(r)
                try: d["manifest"]=self._call((d.get("status_url") or "").rstrip('/'),'/manifest')
                except Exception as e: d["manifest"]={"ok":False,"error":str(e)}
                out.append(d)
            return self.ok(out)

        @app.route('/api/mesh/scripts')
        @logged_tool('mesh_list_scripts')
        def mesh_scripts():
            try:
                _,base=self._node(__import__('flask').request.args.get('node',''))
                p={"root":__import__('flask').request.args.get('root',''),"path":__import__('flask').request.args.get('path',''),"agent_only":"1"}
                return self.ok(self._call(base,'/browse',params=p))
            except Exception as e: return self.err(str(e),502)

        @app.route('/api/mesh/script')
        @logged_tool('mesh_inspect_script')
        def mesh_script():
            try:
                req=__import__('flask').request
                _,base=self._node(req.args.get('node',''))
                data=self._call(base,'/inspect',params={"root":req.args.get('root',''),"path":req.args.get('path','')})
                if data.get('analysis',{}).get('permission')=='private': return self.err('script is owner-private',403)
                return self.ok(data)
            except Exception as e: return self.err(str(e),502)

        @app.route('/api/mesh/run',methods=['POST'])
        @logged_tool('mesh_run_script')
        def mesh_run():
            try:
                req=__import__('flask').request; d=req.get_json(force=True); _,base=self._node(d.get('node',''))
                info=self._call(base,'/inspect',params={"root":d.get('root',''),"path":d.get('path','')})
                a=info.get('analysis',{})
                if a.get('permission') not in ('confirm','autonomous'): return self.err(f"agent permission is {a.get('permission','private')}",403)
                if not a.get('compatible',False): return self.err('script is not compatible with requested node: '+', '.join(a.get('reasons',[])),409)
                return self.ok(self._call(base,'/run',method='POST',body={**d,"owner":False},timeout=15))
            except Exception as e: return self.err(str(e),502)

        @app.route('/api/mesh/run-autonomous',methods=['POST'])
        @logged_tool('mesh_run_autonomous_script')
        def mesh_run_auto():
            try:
                req=__import__('flask').request; d=req.get_json(force=True); _,base=self._node(d.get('node',''))
                info=self._call(base,'/inspect',params={"root":d.get('root',''),"path":d.get('path','')})
                a=info.get('analysis',{})
                if a.get('permission') != 'autonomous': return self.err(f"script is not autonomous (permission={a.get('permission','private')})",403)
                if not a.get('compatible',False): return self.err('script is not compatible with requested node: '+', '.join(a.get('reasons',[])),409)
                return self.ok(self._call(base,'/run',method='POST',body={**d,"owner":False},timeout=15))
            except Exception as e: return self.err(str(e),502)

        @app.route('/api/mesh/ideas')
        @logged_tool('mesh_script_ideas')
        def mesh_ideas():
            try:
                req=__import__('flask').request; _,base=self._node(req.args.get('node',''))
                m=self._call(base,'/manifest'); root=(m.get('roots') or [''])[0]
                b=self._call(base,'/browse',params={"root":root,"path":""})
                return self.ok({"node":req.args.get('node'),"tier":m.get('node',{}).get('tier'),"ideas":self._ideas(m,b.get('entries',[]))})
            except Exception as e: return self.err(str(e),502)

        # Owner-only proxy endpoints used by deterministic Telegram commands; intentionally not in /api/tools.
        @app.route('/api/owner/mesh/browse')
        def owner_browse():
            try:
                req=__import__('flask').request; _,base=self._node(req.args.get('node',''))
                return self.ok(self._call(base,'/browse',params={"root":req.args.get('root',''),"path":req.args.get('path',''),"agent_only":"0"}))
            except Exception as e: return self.err(str(e),502)

        @app.route('/api/owner/mesh/run',methods=['POST'])
        def owner_run():
            try:
                d=__import__('flask').request.get_json(force=True); _,base=self._node(d.get('node',''))
                return self.ok(self._call(base,'/run',method='POST',body={**d,"owner":True},timeout=15))
            except Exception as e: return self.err(str(e),502)

        @app.route('/api/owner/mesh/permission',methods=['POST'])
        def owner_permission():
            try:
                d=__import__('flask').request.get_json(force=True); _,base=self._node(d.get('node',''))
                return self.ok(self._call(base,'/permission',method='POST',body=d))
            except Exception as e: return self.err(str(e),502)

        @app.route('/api/owner/mesh/jobs')
        def owner_jobs():
            try:
                req=__import__('flask').request; _,base=self._node(req.args.get('node',''))
                return self.ok(self._call(base,'/jobs'))
            except Exception as e: return self.err(str(e),502)
