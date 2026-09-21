#!/usr/bin/env python3
"""Normalize external world-context sources into core_server.

Supported collectors:
- Telegram channels/accounts the user can access, via Telethon (optional)
- X recent search via the official X API endpoint (optional, access/key required)
- RSS/Atom feeds via the Python standard library
- JSONL import for any other collector/export

No source credentials are stored in core_server. Telegram API credentials and X
bearer tokens are read only from environment variables in this collector process.
The collector does not bypass access controls, paywalls, CAPTCHAs, or private
content the configured account cannot normally access.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path

import requests

CORE_URL = os.environ.get('CORE_URL', 'http://127.0.0.1:5077').rstrip('/')
CORE_API_KEY = os.environ['CORE_API_KEY']


def headers():
    return {'X-API-Key': CORE_API_KEY, 'Content-Type': 'application/json'}


def get_json(path, params=None):
    r=requests.get(CORE_URL+path,headers=headers(),params=params or {},timeout=30); r.raise_for_status(); return r.json()


def post_json(path, data):
    r=requests.post(CORE_URL+path,headers=headers(),json=data,timeout=30); r.raise_for_status(); return r.json()


def register_feed(feed: dict):
    body={k:feed[k] for k in ('name','source_type','locator','topics','region','base_reliability','bias_notes','config','enabled') if k in feed}
    return post_json('/api/world/feeds/register',body)


def feed_state(name: str) -> dict:
    body=get_json('/api/runtime/world/feed',{'name':name}); return body.get('result') or {}


def set_cursor(name: str, cursor, last_seen_at=None):
    post_json('/api/runtime/world/feed/cursor',{'feed':name,'cursor':str(cursor),'last_seen_at':last_seen_at})


def ingest(feed: dict, *, external_id: str, timestamp: str, text: str, title: str='', url: str='', author: str='', metadata=None, geo=None):
    if not text.strip(): return None
    body={
        'feed':feed['name'],'source_type':feed['source_type'],'external_id':str(external_id or ''),
        'timestamp':timestamp,'title':title,'text':text,'url':url,'author':author,
        'topics':feed.get('topics') or [],'geo':geo or {},'metadata':metadata or {},
    }
    return post_json('/api/world/ingest',body)


def _iso(dt):
    if dt is None: return datetime.now(timezone.utc).isoformat()
    if isinstance(dt,str): return dt
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def poll_rss(feed: dict, limit=50):
    req=urllib.request.Request(feed['locator'],headers={'User-Agent':'agent-world-context/1.0'})
    with urllib.request.urlopen(req,timeout=20) as resp: raw=resp.read()
    root=ET.fromstring(raw); items=[]
    # RSS
    for item in root.findall('.//item')[:limit]:
        def txt(name):
            n=item.find(name); return (n.text or '').strip() if n is not None and n.text else ''
        title,desc,link,guid,pub=txt('title'),txt('description'),txt('link'),txt('guid'),txt('pubDate')
        try: published=_iso(parsedate_to_datetime(pub)) if pub else _iso(None)
        except Exception: published=_iso(None)
        items.append((guid or link or title,published,desc or title,title,link,'',{}))
    # Atom fallback
    if not items:
        ns={'a':'http://www.w3.org/2005/Atom'}
        for item in root.findall('.//a:entry',ns)[:limit]:
            title=(item.findtext('a:title',default='',namespaces=ns) or '').strip(); content=(item.findtext('a:content',default='',namespaces=ns) or item.findtext('a:summary',default='',namespaces=ns) or title).strip(); eid=(item.findtext('a:id',default='',namespaces=ns) or title).strip(); updated=(item.findtext('a:updated',default='',namespaces=ns) or _iso(None)).strip(); linknode=item.find('a:link',ns); link=linknode.attrib.get('href','') if linknode is not None else ''
            items.append((eid,updated,content,title,link,'',{}))
    count=0; last=''
    for eid,ts,text,title,url,author,meta in items:
        ingest(feed,external_id=eid,timestamp=ts,text=text,title=title,url=url,author=author,metadata=meta); count+=1; last=eid
    if last: set_cursor(feed['name'],last)
    return count


async def poll_telegram(feed: dict, limit=100):
    try:
        from telethon import TelegramClient
    except ImportError as e:
        raise RuntimeError('Telegram collector requires: pip install telethon') from e
    api_id=os.environ.get('TELEGRAM_API_ID'); api_hash=os.environ.get('TELEGRAM_API_HASH')
    if not api_id or not api_hash: raise RuntimeError('set TELEGRAM_API_ID and TELEGRAM_API_HASH')
    session=os.path.expanduser(os.environ.get('TELEGRAM_SESSION','~/.agent_world_context'))
    state=feed_state(feed['name']); min_id=int(state.get('last_cursor') or 0) if str(state.get('last_cursor') or '').isdigit() else 0
    count=0; max_id=min_id
    async with TelegramClient(session,int(api_id),api_hash) as client:
        entity=await client.get_entity(feed['locator'])
        messages=[]
        async for m in client.iter_messages(entity,limit=limit,min_id=min_id,reverse=True): messages.append(m)
        for m in messages:
            text=(getattr(m,'message',None) or '').strip()
            if not text: continue
            chat_username=getattr(entity,'username',None); url=f'https://t.me/{chat_username}/{m.id}' if chat_username else ''
            author=''; sender=getattr(m,'sender',None)
            if sender: author=getattr(sender,'username',None) or getattr(sender,'title',None) or str(getattr(sender,'id',''))
            ingest(feed,external_id=str(m.id),timestamp=_iso(m.date),text=text,url=url,author=author,metadata={'views':getattr(m,'views',None),'forwards':getattr(m,'forwards',None),'replies':getattr(getattr(m,'replies',None),'replies',None)})
            count+=1; max_id=max(max_id,int(m.id))
    if max_id>min_id: set_cursor(feed['name'],max_id)
    return count


def poll_x(feed: dict, limit=100):
    if os.environ.get('ALLOW_PAID_X', '').lower() not in {'1','true','yes'}:
        raise RuntimeError('X collection is disabled by default; set ALLOW_PAID_X=1 after reviewing API pricing')
    daily_limit = int(os.environ.get('X_DAILY_REQUEST_LIMIT', '0'))
    if daily_limit < 1:
        raise RuntimeError('set a positive X_DAILY_REQUEST_LIMIT before enabling X collection')
    gate = get_json('/api/owner/usage/check', {'service':'x','daily_request_limit':daily_limit}).get('result') or {}
    if not gate.get('allowed'):
        raise RuntimeError(f"X daily request limit reached ({gate.get('used_today', 0)}/{daily_limit})")
    token=os.environ.get('X_BEARER_TOKEN')
    if not token: raise RuntimeError('set X_BEARER_TOKEN for the official X API collector')
    base=os.environ.get('X_API_BASE_URL','https://api.x.com').rstrip('/')
    path=os.environ.get('X_RECENT_SEARCH_PATH','/2/tweets/search/recent')
    query=feed['locator']; state=feed_state(feed['name']); cursor=state.get('last_cursor') or ''
    saved = json.loads(cursor) if str(cursor).startswith('{') else {'since_id': str(cursor)}
    since_id = saved.get('since_id', '')
    params={'query':query,'max_results':max(10,min(limit,100)),'tweet.fields':'created_at,author_id,lang,geo,public_metrics','expansions':'author_id','user.fields':'username,name,verified'}
    if since_id: params['since_id']=since_id
    max_id = int(saved.get('high_watermark') or since_id or 0)
    next_token, count = saved.get('next_token'), 0
    for _ in range(max(1, min(20, int(feed.get('config', {}).get('max_pages', 5))))):
        if next_token: params['next_token'] = next_token
        r=requests.get(base+path,headers={'Authorization':f'Bearer {token}'},params=params,timeout=30)
        if r.status_code>=400: raise RuntimeError(f'X API HTTP {r.status_code}: {r.text[:1000]}')
        data=r.json()
        if data.get('errors'): raise RuntimeError('X API returned partial errors; cursor was not advanced')
        request_cost = os.environ.get('X_COST_PER_REQUEST_USD')
        post_json('/api/owner/usage', {'service':'x','operation':'recent_search','requests':1,
                  'units':{'posts':len(data.get('data') or [])},
                  **({'cost_usd':float(request_cost)} if request_cost else {})})
        users={u['id']:u for u in data.get('includes',{}).get('users',[])}
        for t in reversed(data.get('data') or []):
            u=users.get(t.get('author_id'),{}); username=u.get('username',''); tid=str(t.get('id','')); url=f'https://x.com/{username}/status/{tid}' if username else f'https://x.com/i/status/{tid}'
            ingest(feed,external_id=tid,timestamp=t.get('created_at') or _iso(None),text=t.get('text',''),url=url,author=username,geo=t.get('geo') or {},metadata={'lang':t.get('lang'),'public_metrics':t.get('public_metrics') or {},'verified':u.get('verified')})
            count+=1
            if tid.isdigit(): max_id=max(max_id,int(tid))
        next_token = data.get('meta', {}).get('next_token')
        # Checkpoint pages without moving since_id past unread older results.
        # Restarted polls reuse both since_id and next_token from this checkpoint.
        if next_token:
            set_cursor(feed['name'], json.dumps({'since_id': since_id, 'next_token': next_token, 'high_watermark': str(max_id)}))
        else:
            if max_id: set_cursor(feed['name'], str(max_id))
            break
    return count


def import_jsonl(feed: dict, path: str):
    count=0; last=''
    for i,line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(),1):
        if not line.strip(): continue
        d=json.loads(line); eid=str(d.get('id') or d.get('external_id') or i); ingest(feed,external_id=eid,timestamp=d.get('timestamp') or d.get('ts') or _iso(None),title=d.get('title',''),text=d.get('text') or d.get('content') or json.dumps(d,ensure_ascii=False),url=d.get('url',''),author=d.get('author',''),metadata=d.get('metadata') or {}); count+=1; last=eid
    if last: set_cursor(feed['name'],last)
    return count


def run_feed(feed: dict, limit=100):
    register_feed(feed)
    typ=feed['source_type'].lower()
    if typ in {'rss','atom'}: return poll_rss(feed,limit)
    if typ=='telegram': return asyncio.run(poll_telegram(feed,limit))
    if typ=='x': return poll_x(feed,limit)
    if typ=='jsonl': return import_jsonl(feed,feed['locator'])
    raise ValueError(f'unsupported source_type: {typ}')


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',default=str(Path(__file__).parent / 'examples' / 'news-feeds.json')); ap.add_argument('--register-only',action='store_true'); ap.add_argument('--only'); ap.add_argument('--interval',type=int,default=0); ap.add_argument('--limit',type=int,default=100); args=ap.parse_args(); cfg=json.loads(Path(args.config).read_text()); feeds=[f for f in cfg.get('feeds',[]) if f.get('enabled',True) and (not args.only or f.get('name')==args.only)]
    if not feeds: raise SystemExit('no enabled matching feeds')
    if args.register_only:
        for feed in feeds: register_feed(feed)
        return
    while True:
        for feed in feeds:
            try: print(f"[{feed['name']}] ingested {run_feed(feed,args.limit)}",flush=True)
            except Exception as e: print(f"[{feed.get('name')}] failed: {type(e).__name__}: {e}",flush=True)
        if args.interval<=0: break
        time.sleep(max(60,args.interval))


if __name__=='__main__': main()
