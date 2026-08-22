# -*- coding: utf-8 -*-
from __future__ import annotations
import asyncio, html, json, logging, os, re, threading, time, unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
import requests
from bs4 import BeautifulSoup

log=logging.getLogger("cozy-catalog")
SHEET_ID=os.getenv("GOOGLE_SHEET_ID","").strip()
CREDS=os.getenv("GOOGLE_CREDS_JSON","").strip()
WSNAME=os.getenv("LOTS_WORKSHEET_NAME","Lots").strip() or "Lots"
CATALOG_CHANNEL=os.getenv("CATALOG_CHANNEL","samuirental").strip().lstrip("@")
CATALOG_BOOTSTRAP_LIMIT=int(os.getenv("CATALOG_BOOTSTRAP_LIMIT","20") or 20)
CATALOG_BOOTSTRAP_IMPORT=os.getenv("CATALOG_BOOTSTRAP_IMPORT","1").lower() not in {"0","false","no","off"}
CATALOG_BOOTSTRAP_FULL=os.getenv("CATALOG_BOOTSTRAP_FULL","0").lower() in {"1","true","yes","on"}
MAX_PAGES=int(os.getenv("CATALOG_FULL_MAX_PAGES","400") or 400)
WORKERS=max(1,min(int(os.getenv("CATALOG_EXTRACT_WORKERS","4") or 4),8))
CACHE_TTL=max(10,int(os.getenv("CATALOG_CACHE_TTL","45") or 45))
OPENAI_API_KEY=os.getenv("OPENAI_API_KEY","").strip()
OPENAI_PROJECT=os.getenv("OPENAI_PROJECT","").strip()
OPENAI_ORG=os.getenv("OPENAI_ORG","").strip()
OPENAI_MODEL=os.getenv("OPENAI_MODEL","gpt-4o-mini").strip()

HEADERS=["lot_id","telegram_message_id","telegram_url","published_at","status","тип","район","спальни","ванные","бассейн","тип_бассейна","цена_месяц_thb","цена_сутки_thb","депозит_thb","комиссия_thb","до_моря_м","доступность","питомцы","электричество","вода","контакт_собственника","описание","исходный_текст","extracted_at","confidence","needs_review"]
_lock=threading.RLock(); _cache_lock=threading.RLock(); _cache=(0.0,[])

DISTRICTS=[
(("chaweng noi","чавенг ной"),"Чавенг Ной"),(("hua thanon","hua tanon","хуа танон"),"Хуа Танон"),
(("taling ngam","талинг нгам"),"Талинг Нгам"),(("plai laem","плай лаем","плай лэм"),"Плай Лаем"),
(("lipa noi","липа ной"),"Липа Ной"),(("bang rak","bangrak","банграк"),"Банграк"),
(("bang por","bangpo","bang po","банг по","бангпор"),"Банг По"),(("choeng mon","чонг мон","чоенг мон"),"Чонг Мон"),
(("na mueang","na muang","на муанг"),"На Муанг"),(("maenam","mae nam","майнам","маенам"),"Маенам"),
(("bophut","bo phut","boput","бо пут","бопхут"),"Бопхут"),(("chaweng","чавенг"),"Чавенг"),
(("lamai","ламай"),"Ламай"),(("nathon","натон"),"Натон"),(("koh phangan","ko phangan","ко панган","панган"),"Ко Панган")]

def _now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def _blank(v):
    s=str(v or "").strip()
    return "" if s.lower() in {"unknown","none","null","-","—","n/a","неизвестно"} else s
def _col(n):
    s=""
    while n: n,r=divmod(n-1,26); s=chr(65+r)+s
    return s
def _client():
    if not SHEET_ID or not CREDS: raise RuntimeError("Google Sheets credentials missing")
    import gspread
    from google.oauth2.service_account import Credentials
    c=Credentials.from_service_account_info(json.loads(CREDS),scopes=["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"])
    return gspread.authorize(c)
def ensure_lots_sheet():
    with _lock:
        sh=_client().open_by_key(SHEET_ID)
        try: ws=sh.worksheet(WSNAME)
        except Exception: ws=sh.add_worksheet(title=WSNAME,rows=3000,cols=len(HEADERS))
        vals=ws.get_all_values()
        if not vals: ws.append_row(HEADERS,value_input_option="RAW")
        else:
            h=list(vals[0]); changed=False
            for x in HEADERS:
                if x not in h: h.append(x); changed=True
            if changed: ws.update("A1",[h],value_input_option="RAW")
        return ws
def _invalidate():
    global _cache
    with _cache_lock: _cache=(0.0,[])

def _digits(t):
    s=(t or "").replace("\ufe0f","").replace("\u20e3","").replace("➖","-").replace("–","-").replace("—","-").replace("−","-")
    s=unicodedata.normalize("NFKC",s); out=[]
    for ch in s:
        try: out.append(str(int(unicodedata.digit(ch))))
        except Exception: out.append(ch)
    return "".join(out)
def extract_lot_id(text):
    raw=text or ""; norm=_digits(raw); lines=[x.strip() for x in norm.splitlines()[:30] if x.strip()]; head="\n".join(lines[:15])
    for pat in [r"(?i)(?:лот|lot)\s*(?:№|#|no\.?)?\s*[:\-]?\s*(\d{3,7})",r"(?:№|#)\s*(\d{3,7})",r"(?<!\d)\d{1,2}\s*-\s*(\d{3,7})(?!\d)"]:
        m=re.search(pat,head)
        if m:return m.group(1).lstrip("0") or "0"
    run=[]
    for line in lines:
        if re.fullmatch(r"\d",line): run=(run+[line])[-7:]
        else:
            if 3<=len(run)<=7:return "".join(run).lstrip("0") or "0"
            run=[]
    if 3<=len(run)<=7:return "".join(run).lstrip("0") or "0"
    if any(x in "\n".join(raw.splitlines()[:15]) for x in ("\u20e3","➖","🔤")):
        g=re.findall(r"(?<!\d)(\d{3,7})(?!\d)",head)
        if g:return g[-1].lstrip("0") or "0"
    return ""

def norm_district(v):
    s=_blank(v)
    if not s:return ""
    low=re.sub(r"\s+"," ",s.lower().replace("_"," ")).strip(); found=[]
    for aliases,name in DISTRICTS:
        if any(a in low for a in aliases): found.append(name)
    found=list(dict.fromkeys(found))
    if found:return " / ".join(found[:2])
    if any(x in low for x in ("prime samui","samui location","koh samui","самуи")) and len(low.split())<=5:return ""
    return s
def norm_type(v):
    s=_blank(v).lower()
    if not s:return ""
    for keys,name in [(("вилла","villa"),"вилла"),(("бунгало","bungalow"),"бунгало"),(("студ","studio"),"студия"),(("кондо","condo"),"кондо"),(("апартамент","квартир","apartment","flat"),"апартаменты"),(("дом","house","home"),"дом")]:
        if any(x in s for x in keys):return name
    return s
def norm_pool(v):
    s=_blank(v).lower()
    if not s:return ""
    if s in {"yes","y","true","да","есть","1"}:return "yes"
    if s in {"no","n","false","нет","0","отсутствует"}:return "no"
    if re.fullmatch(r"\d+(?:[.,]\d+)?",s):
        try:return "yes" if float(s.replace(",","."))>0 else "no"
        except:pass
    if any(x in s for x in ("приват","частн","общ","инфинити","infinity","private","shared","pool","бассейн")):return "yes"
    return ""
def norm_pool_type(v):
    s=_blank(v).lower()
    if any(x in s for x in ("инфинити","infinity")):return "infinity"
    if any(x in s for x in ("приват","частн","private")):return "private"
    if any(x in s for x in ("общ","shared","communal")):return "shared"
    return ""
def norm_pets(v):
    s=_blank(v).lower()
    if any(x in s for x in ("не допуска","не разреш","запрещ","no pets","not allowed")):return "no"
    if any(x in s for x in ("да","разреш","можно","allowed","pet friendly")):return "yes"
    return ""
def norm_status(v):
    s=_blank(v).lower()
    if any(x in s for x in ("сдан","rented","occupied")):return "rented"
    if any(x in s for x in ("архив","archive","inactive","removed")):return "archived"
    if any(x in s for x in ("уточн","needs_check","check")):return "needs_check"
    return "active"
def _num(v,decimal=False):
    s=_blank(v).replace(" ","").replace("'","").replace("’","")
    if not s:return ""
    m=re.fullmatch(r"\d+(?:[.,]\d+)?" if decimal else r"\d+",s)
    return m.group(0).replace(",",".") if m else _blank(v)
def _sea_m(text):
    t=_digits(text or "").lower()
    if re.search(r"(первая\s+линия|first\s+line|beachfront|прям(?:ой|ым)\s+выход(?:ом)?\s+(?:к|на)\s+(?:морю|пляж))",t):return "0"
    metric=r"(\d{1,5})\s*(?:м\b|метр(?:а|ов)?\b|meters?\b)"; target=r"(?:до\s+)?(?:моря|пляжа|beach|sea)"
    for pat in (metric+r".{0,45}"+target,target+r".{0,45}"+metric):
        m=re.search(pat,t,re.I|re.S)
        if m:return m.group(1)
    return ""
def canonical(rec,source=""):
    out={h:_blank(rec.get(h,"")) for h in HEADERS}
    out["status"]=norm_status(rec.get("status","active")); out["тип"]=norm_type(rec.get("тип","")); out["район"]=norm_district(rec.get("район",""))
    out["спальни"]=_num(rec.get("спальни","")); out["ванные"]=_num(rec.get("ванные",""),True)
    out["бассейн"]=norm_pool(rec.get("бассейн","")); out["тип_бассейна"]=norm_pool_type(rec.get("тип_бассейна",""))
    if out["тип_бассейна"] and not out["бассейн"]:out["бассейн"]="yes"
    for k in ("цена_месяц_thb","цена_сутки_thb","депозит_thb","комиссия_thb"):out[k]=_num(rec.get(k,""))
    out["до_моря_м"]=_sea_m(source) if source else _num(rec.get("до_моря_м",""))
    out["питомцы"]=norm_pets(rec.get("питомцы","")); out["исходный_текст"]=source or _blank(rec.get("исходный_текст",""))
    return out
def _is_listing(text,lot=""):
    if lot:return True
    t=(text or "").lower()
    return any(k in t for k in ("вилла","дом","апартамент","квартира","студия","villa","house","apartment","condo","bungalow")) and any(k in t for k in ("бат","thb","฿","аренд","стоимость","цена"))

def _extract(text):
    lot=extract_lot_id(text)
    if not OPENAI_API_KEY:
        d={h:"" for h in HEADERS}; d["lot_id"]=lot; d["тип"]=norm_type(text); d["confidence"]="low"; d["needs_review"]="yes"
        m=re.search(r"(?i)(\d+(?:[.,]\d+)?)\s*(?:спальн|bedroom|br\b)",text or "")
        if m:d["спальни"]=m.group(1)
        if "бассейн" in text.lower() or "pool" in text.lower():d["бассейн"]="yes"
        return canonical(d,text)
    try:
        from openai import OpenAI
        c=OpenAI(api_key=OPENAI_API_KEY,project=OPENAI_PROJECT or None,organization=OPENAI_ORG or None,timeout=45)
        sys="""Извлеки только факты из объявления аренды недвижимости Cozy Asia и верни ТОЛЬКО JSON. Ничего не додумывай, неизвестное оставляй пустым. Не считай Cozy Asia/@cozy_asia контактом собственника. Денежные *_thb заполняй только если сумма явно в THB/батах; USD/EUR не конвертируй. до_моря_м только при явном расстоянии в метрах, минуты не переводить в метры. бассейн yes/no/пусто; тип_бассейна private/shared/infinity/пусто; питомцы yes/no/пусто. Ключи: lot_id, тип, район, спальни, ванные, бассейн, тип_бассейна, цена_месяц_thb, цена_сутки_thb, депозит_thb, комиссия_thb, до_моря_м, доступность, питомцы, электричество, вода, контакт_собственника, описание, confidence, needs_review."""
        r=c.chat.completions.create(model=OPENAI_MODEL,messages=[{"role":"system","content":sys},{"role":"user","content":text}],response_format={"type":"json_object"},temperature=0,max_tokens=900)
        d=json.loads(r.choices[0].message.content or "{}")
    except Exception as e:
        log.warning("extract failed: %s",e); d={}
    if lot:d["lot_id"]=lot
    d.setdefault("confidence","medium" if lot else "low"); d.setdefault("needs_review","no" if lot else "yes")
    return canonical(d,text)

def _existing(ws):
    vals=ws.get_all_values()
    if not vals:return {},HEADERS[:]
    h=list(vals[0]); found={}
    for rn,row in enumerate(vals[1:],2):
        row=row+[""]*max(0,len(h)-len(row)); d=dict(zip(h,row)); mid=d.get("telegram_message_id","").strip()
        if mid:found[mid]=(rn,d)
    return found,h
def _record(p,old=None):
    d={h:"" for h in HEADERS}; d.update(_extract(p["text"])); d.update({"telegram_message_id":p["message_id"],"telegram_url":p["telegram_url"],"published_at":p["published_at"],"status":"active","исходный_текст":p["text"],"extracted_at":_now()})
    if old and _blank(old.get("контакт_собственника")):d["контакт_собственника"]=old["контакт_собственника"]
    if old and _blank(old.get("status")):d["status"]=norm_status(old["status"])
    return canonical(d,p["text"])
def normalize_existing_rows():
    with _lock:
        ws=ensure_lots_sheet(); vals=ws.get_all_values()
        if len(vals)<=1:return {"rows":0,"changed":0}
        h=list(vals[0]); out=[h]; changed=0
        for row in vals[1:]:
            pad=row+[""]*max(0,len(h)-len(row)); d=dict(zip(h,pad)); c=canonical(d,d.get("исходный_текст","")); nr=[c.get(x,d.get(x,"")) for x in h]
            if nr!=pad[:len(h)]:changed+=1
            out.append(nr)
        if changed:ws.update(f"A1:{_col(len(h))}{len(out)}",out,value_input_option="USER_ENTERED"); _invalidate()
        log.info("normalize rows=%s changed=%s",len(out)-1,changed); return {"rows":len(out)-1,"changed":changed}

def _parse_page(channel,before=""):
    url=f"https://t.me/s/{channel}"+(f"?before={before}" if before else "")
    r=requests.get(url,timeout=25,headers={"User-Agent":"Mozilla/5.0"}); r.raise_for_status(); soup=BeautifulSoup(r.text,"html.parser"); out=[]
    for msg in soup.select(".tgme_widget_message"):
        dp=(msg.get("data-post") or "").strip()
        if "/" not in dp:continue
        ch,mid=dp.rsplit("/",1)
        if ch.lower()!=channel.lower() or not mid.isdigit():continue
        node=msg.select_one(".tgme_widget_message_text")
        if node is None:continue
        text=html.unescape(node.get_text("\n",strip=True)).strip()
        if not text:continue
        tn=msg.select_one("time"); pub=(tn.get("datetime") or "").strip() if tn else ""
        ln=msg.select_one("a.tgme_widget_message_date"); link=(ln.get("href") or "").strip() if ln else f"https://t.me/{channel}/{mid}"
        out.append({"message_id":mid,"text":text,"published_at":pub,"telegram_url":link})
    return out
def _crawl(channel,wanted=None,max_pages=400):
    by={}; before=""; stagnant=0
    for page_no in range(1,max_pages+1):
        page=_parse_page(channel,before)
        if not page:break
        n=len(by)
        for p in page:by[p["message_id"]]=p
        added=len(by)-n; oldest=min(int(p["message_id"]) for p in page)
        log.info("crawl @%s page=%s total=%s added=%s oldest=%s",channel,page_no,len(by),added,oldest)
        if wanted and len(by)>=wanted:break
        stagnant=stagnant+1 if not added else 0
        if stagnant>=2 or oldest<=1:break
        nb=str(oldest)
        if nb==before:break
        before=nb; time.sleep(.1)
    arr=sorted(by.values(),key=lambda x:int(x["message_id"]))
    return arr[-wanted:] if wanted else arr

def _import(posts,force=False):
    ws=ensure_lots_sheet(); existing,h=_existing(ws)
    s={"channel":CATALOG_CHANNEL,"inspected":len(posts),"listing_candidates":0,"inserted":0,"updated":0,"skipped":0,"needs_review":0,"errors":0,"lots":[]}; work=[]
    for p in posts:
        lot=extract_lot_id(p["text"])
        if not _is_listing(p["text"],lot):continue
        s["listing_candidates"]+=1; cur=existing.get(p["message_id"])
        if cur and not force and cur[1].get("исходный_текст","")==p["text"]:
            s["skipped"]+=1
            if cur[1].get("lot_id"):s["lots"].append(cur[1]["lot_id"])
        else:work.append((p,cur[1] if cur else None))
    records=[]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fs={ex.submit(_record,p,old):p["message_id"] for p,old in work}; done=0
        for f in as_completed(fs):
            try:records.append(f.result()); done+=1
            except Exception: s["errors"]+=1; log.exception("extract message=%s",fs[f])
            if done and (done%10==0 or done==len(work)):log.info("extract @%s %s/%s",CATALOG_CHANNEL,done,len(work))
    records.sort(key=lambda r:int(r.get("telegram_message_id") or 0)); inserts=[]; updates=[]
    for rec in records:
        mid=rec["telegram_message_id"]; cur=existing.get(mid); row=[rec.get(x,"") for x in h]
        (updates if cur else inserts).append((cur[0],row) if cur else row)
        if rec.get("lot_id"):s["lots"].append(rec["lot_id"])
        if rec.get("needs_review","").lower()=="yes":s["needs_review"]+=1
    with _lock:
        for rn,row in updates:
            try:ws.update(f"A{rn}:{_col(len(h))}{rn}",[row],value_input_option="USER_ENTERED"); s["updated"]+=1
            except Exception:s["errors"]+=1; log.exception("update row %s",rn)
        for i in range(0,len(inserts),40):
            chunk=inserts[i:i+40]
            try:ws.append_rows(chunk,value_input_option="USER_ENTERED"); s["inserted"]+=len(chunk); log.info("append @%s %s/%s",CATALOG_CHANNEL,min(i+len(chunk),len(inserts)),len(inserts))
            except Exception:s["errors"]+=len(chunk); log.exception("append chunk")
            time.sleep(.25)
    _invalidate(); s["lots"]=list(dict.fromkeys(s["lots"])); return s
def import_public_channel_latest(limit=20,force=False):return _import(_crawl(CATALOG_CHANNEL,max(1,min(int(limit or 20),100)),20),force)
def import_public_channel_all(force=False):return _import(_crawl(CATALOG_CHANNEL,None,MAX_PAGES),force)
def bootstrap_catalog():
    normalize_existing_rows()
    if not CATALOG_BOOTSTRAP_IMPORT:return {"disabled":True}
    return import_public_channel_all(False) if CATALOG_BOOTSTRAP_FULL else import_public_channel_latest(CATALOG_BOOTSTRAP_LIMIT,False)

def load_catalog_rows(force=False):
    global _cache
    now=time.time()
    with _cache_lock:
        ts,rows=_cache
        if rows and not force and now-ts<CACHE_TTL:return [dict(x) for x in rows]
    vals=ensure_lots_sheet().get_all_values(); out=[]
    if len(vals)>1:
        h=list(vals[0])
        for row in vals[1:]:
            row=row+[""]*max(0,len(h)-len(row)); d=dict(zip(h,row))
            if d.get("telegram_message_id","").strip():out.append(canonical(d,d.get("исходный_текст","")))
    with _cache_lock:_cache=(now,[dict(x) for x in out])
    return out
def catalog_status():
    rows=load_catalog_rows(True); lots=[r["lot_id"] for r in rows if r.get("lot_id")]; seen=set(); dup=set()
    for x in lots:
        if x in seen:dup.add(x)
        seen.add(x)
    return {"rows":len(rows),"active":sum(norm_status(r.get("status")) not in {"archived","rented"} for r in rows),"duplicates":sorted(dup),"last":[r.get("lot_id") or f"msg:{r.get('telegram_message_id')}" for r in rows[-5:]]}

def _int(v):
    s=_blank(v).replace(" ","").replace("'","")
    try:return int(float(s.replace(",","."))) if re.fullmatch(r"\d+(?:[.,]\d+)?",s) else None
    except:return None
def _looks(text):
    low=(text or "").lower()
    if re.search(r"\b(?:лот|lot)\s*(?:№|#)?\s*\d{3,7}\b",low):return True
    return any(x in low for x in ("ищу","нужен","нужна","подбери","покажи","вариант","есть ли","хочу снять","снять","арендовать","что есть")) and any(x in low for x in ("вилла","дом","квартир","апартамент","студия","кондо","жиль","спальн","бассейн","бат","thb"))
def _heuristic(text):
    low=(text or "").lower(); m=re.search(r"\b(?:лот|lot)\s*(?:№|#)?\s*(\d{3,7})\b",low)
    if m:return {"intent":"lot","lot_id":m.group(1)}
    s={"intent":"search" if _looks(text) else "other","types":[],"districts":[],"district_required":not any(x in low for x in ("желательно","предпочтительно","лучше бы")),"bedrooms_min":None,"bedrooms_max":None,"pool":"any","max_price_thb":None,"max_distance_sea_m":None,"pets":"any"}
    typ=norm_type(low)
    if typ in {"вилла","дом","апартаменты","студия","кондо","бунгало"}:s["types"]=[typ]
    for a,n in DISTRICTS:
        if any(x in low for x in a):s["districts"].append(n)
    words={"одна":1,"один":1,"две":2,"два":2,"три":3,"четыре":4,"пять":5,"шесть":6}
    m=re.search(r"\b(\d+|одна|один|две|два|три|четыре|пять|шесть)\s*(?:спальн|br\b)",low)
    if m:
        n=int(m.group(1)) if m.group(1).isdigit() else words[m.group(1)]; s["bedrooms_min"]=s["bedrooms_max"]=n
    if "бассейн" in low or "pool" in low:s["pool"]="no" if re.search(r"(без\s+бассейн|бассейн\s+не\s+нуж)",low) else "yes"
    m=re.search(r"(?:до|бюджет(?:ом)?|не\s+дороже)\s*([\d\s]+)\s*(тыс(?:яч)?|k)?\s*(?:бат|thb)?",low)
    if m:
        n=int(re.sub(r"\D","",m.group(1)) or 0); s["max_price_thb"]=n*1000 if m.group(2) else n
    m=re.search(r"(?:до|не\s+дальше)\s*(\d{2,5})\s*(?:м\b|метр)",low)
    if m:s["max_distance_sea_m"]=int(m.group(1))
    if any(x in low for x in ("с собак","с кош","с живот","питом")):s["pets"]="yes"
    return s
def parse_property_query(text):
    base=_heuristic(text)
    if base.get("intent")=="lot" or not _looks(text) or not OPENAI_API_KEY:return base
    try:
        from openai import OpenAI
        c=OpenAI(api_key=OPENAI_API_KEY,project=OPENAI_PROJECT or None,organization=OPENAI_ORG or None,timeout=30)
        sys="""Классифицируй запрос клиента к каталогу аренды Cozy Asia и верни ТОЛЬКО JSON. intent search/lot/other; lot_id; types массив (вилла/дом/апартаменты/студия/кондо/бунгало); districts массив; district_required false только если район пожелание; bedrooms_min; bedrooms_max; pool yes/no/any; max_price_thb; max_distance_sea_m; pets yes/no/any. Числа числами/null. 'до 100 тысяч'=100000."""
        r=c.chat.completions.create(model=OPENAI_MODEL,messages=[{"role":"system","content":sys},{"role":"user","content":text}],response_format={"type":"json_object"},temperature=0,max_tokens=350)
        d=json.loads(r.choices[0].message.content or "{}")
        if d.get("intent") not in {"search","lot","other"}:return base
        d["types"]=[norm_type(x) for x in d.get("types",[]) if norm_type(x)]; d["districts"]=list(dict.fromkeys(norm_district(x) for x in d.get("districts",[]) if norm_district(x)))
        d["pool"]=str(d.get("pool") or "any").lower(); d["pets"]=str(d.get("pets") or "any").lower(); return d
    except Exception as e:log.warning("query parse failed %s",e); return base
def _latest(rows):
    best={}
    for r in rows:
        key=r.get("lot_id") or f"msg:{r.get('telegram_message_id')}"
        if key not in best or int(r.get("telegram_message_id") or 0)>int(best[key].get("telegram_message_id") or 0):best[key]=r
    return list(best.values())
def _dmatch(rd,wanted):
    rd=norm_district(rd)
    return any((w:=norm_district(x)) and (w in rd or rd in w) for x in wanted)
def search_catalog(spec,limit=5):
    rows=[r for r in _latest(load_catalog_rows()) if norm_status(r.get("status")) not in {"archived","rented"}]
    if spec.get("intent")=="lot":
        a=[r for r in rows if r.get("lot_id")==str(spec.get("lot_id") or "")]; a.sort(key=lambda r:int(r.get("telegram_message_id") or 0),reverse=True); return a[:limit],False
    types=[norm_type(x) for x in spec.get("types",[]) if norm_type(x)]; ds=[norm_district(x) for x in spec.get("districts",[]) if norm_district(x)]
    try:bmin=int(spec["bedrooms_min"]) if spec.get("bedrooms_min") is not None else None
    except:bmin=None
    try:bmax=int(spec["bedrooms_max"]) if spec.get("bedrooms_max") is not None else None
    except:bmax=None
    maxp=_int(spec.get("max_price_thb")); maxd=_int(spec.get("max_distance_sea_m")); pool=str(spec.get("pool") or "any"); pets=str(spec.get("pets") or "any"); req=bool(spec.get("district_required",True))
    def score(r,relax=False):
        sc=0.; typ=norm_type(r.get("тип")); b=_int(r.get("спальни")); p=_int(r.get("цена_месяц_thb")); dist=_int(r.get("до_моря_м")); rp=norm_pool(r.get("бассейн")); pet=norm_pets(r.get("питомцы"))
        if types:
            if typ not in types:return None
            sc+=4
        if ds:
            ok=_dmatch(r.get("район",""),ds)
            if req and not relax and not ok:return None
            sc+=5 if ok else -2
        if bmin is not None and (b is None or b<bmin):return None
        if bmax is not None and (b is None or b>bmax):return None
        if bmin is not None:sc+=4
        if pool=="yes" and rp!="yes":return None
        if pool=="no" and rp!="no":return None
        if pool!="any":sc+=5
        if maxp is not None and (p is None or p>maxp):return None
        if maxp is not None:sc+=2
        if maxd is not None and (dist is None or dist>maxd):return None
        if maxd is not None:sc+=3
        if pets=="yes" and pet!="yes":return None
        if pets=="yes":sc+=3
        return sc+min(2,int(r.get("telegram_message_id") or 0)/100000)
    arr=[(score(r),r) for r in rows]; arr=[x for x in arr if x[0] is not None]; relaxed=False
    if not arr and ds:
        relaxed=True; arr=[(score(r,True),r) for r in rows]; arr=[x for x in arr if x[0] is not None]
    arr.sort(key=lambda x:(x[0],int(x[1].get("telegram_message_id") or 0)),reverse=True); return [r for _,r in arr[:limit]],relaxed
def _pooltxt(r):
    p=norm_pool(r.get("бассейн")); t=norm_pool_type(r.get("тип_бассейна"))
    if p=="no":return "без бассейна"
    if p!="yes":return "бассейн не указан"
    return {"private":"приватный бассейн","shared":"общий бассейн","infinity":"инфинити-бассейн"}.get(t,"есть бассейн")
def _money(v):
    n=_int(v); return f"{n:,}".replace(","," ") if n is not None else ""
def format_catalog_answer(spec,rows,relaxed=False):
    if spec.get("intent")=="lot":
        lot=str(spec.get("lot_id") or "")
        if not rows:return f"Лот №{lot} в каталоге @{CATALOG_CHANNEL} не найден."
        r=rows[0]; lines=[f"🏡 Лот №{r.get('lot_id') or lot}"]
        if r.get("район"):lines.append(f"📍 {r['район']}")
        a=[x for x in (r.get("тип"),f"{r['спальни']} сп." if r.get("спальни") else "",f"{r['ванные']} ванных" if r.get("ванные") else "",_pooltxt(r)) if x]; lines.append(" · ".join(a))
        if _money(r.get("цена_месяц_thb")):lines.append(f"💰 {_money(r['цена_месяц_thb'])} THB/мес.")
        if r.get("до_моря_м"):lines.append(f"🌊 До моря: {r['до_моря_м']} м")
        if r.get("доступность"):lines.append(f"🗓 {r['доступность']}")
        if r.get("telegram_url"):lines.append(f"🔗 {r['telegram_url']}")
        lines.append("\nАктуальность свободных дат лучше подтвердить у менеджера."); return "\n".join(lines)
    if not rows:return f"По каталогу @{CATALOG_CHANNEL} точных вариантов под эти условия сейчас не нашёл. Можно расширить бюджет, район или другие параметры."
    lines=["🏡 Нашёл подходящие варианты"+(" (по району показал ближайшие)" if relaxed else "")+f" в @{CATALOG_CHANNEL}:"]
    for i,r in enumerate(rows,1):
        x=f"{i}. Лот №{r.get('lot_id') or r.get('telegram_message_id')}"
        if r.get("район"):x+=f" — {r['район']}"
        lines.append(x); a=[r.get("тип",""),f"{r['спальни']} сп." if r.get("спальни") else "",_pooltxt(r),f"{_money(r['цена_месяц_thb'])} THB/мес." if _money(r.get("цена_месяц_thb")) else ""]
        lines.append("   "+" · ".join(y for y in a if y))
        if r.get("до_моря_м"):lines.append(f"   🌊 {r['до_моря_м']} м до моря")
        if r.get("доступность"):lines.append(f"   🗓 {r['доступность']}")
        if r.get("telegram_url"):lines.append(f"   🔗 {r['telegram_url']}")
    lines.append("\nДанные взяты из публикаций канала; актуальность свободных дат подтверждает менеджер."); return "\n".join(lines)
def answer_catalog_query(text):
    spec=parse_property_query(text)
    if spec.get("intent") not in {"search","lot"}:return ""
    rows,rel=search_catalog(spec,5); return format_catalog_answer(spec,rows,rel)

async def cmd_find(update,context):
    q=" ".join(getattr(context,"args",None) or []).strip()
    if not q:return await update.effective_message.reply_text("Пример: /find 3 спальни, бассейн, до 100000 бат")
    a=await asyncio.to_thread(answer_catalog_query,q); await update.effective_message.reply_text(a or "Не смог распознать параметры.",disable_web_page_preview=True)
async def cmd_lot(update,context):
    q=" ".join(getattr(context,"args",None) or []).strip()
    if not q:return await update.effective_message.reply_text("Пример: /lot 1176")
    a=await asyncio.to_thread(answer_catalog_query,f"лот {q}"); await update.effective_message.reply_text(a,disable_web_page_preview=True)
async def cmd_catalog_import(update,context):
    args=getattr(context,"args",None) or []; full=bool(args and str(args[0]).lower() in {"all","full","все"})
    await update.effective_message.reply_text(f"Импортирую {'всю историю' if full else 'последние публикации'} @{CATALOG_CHANNEL}…")
    try:
        s=await asyncio.to_thread(import_public_channel_all,False) if full else await asyncio.to_thread(import_public_channel_latest,CATALOG_BOOTSTRAP_LIMIT,False)
    except Exception as e:log.exception("import failed"); return await update.effective_message.reply_text(f"Импорт не выполнен: {type(e).__name__}: {e}")
    await update.effective_message.reply_text(f"Готово. Проверено {s['inspected']}, объявлений {s['listing_candidates']}, добавлено {s['inserted']}, обновлено {s['updated']}, пропущено {s['skipped']}, ошибок {s['errors']}.")
async def cmd_catalog_status(update,context):
    try:s=await asyncio.to_thread(catalog_status); await update.effective_message.reply_text(f"Lots: {s['rows']} строк; активных {s['active']}; дубли: {', '.join(s['duplicates'][:10]) or 'нет'}.")
    except Exception as e:await update.effective_message.reply_text(f"Каталог недоступен: {type(e).__name__}: {e}")
async def catch_catalog_updates(update,context):
    msg=getattr(update,"channel_post",None) or getattr(update,"edited_channel_post",None)
    if not msg:return
    chat=getattr(msg,"chat",None); username=(getattr(chat,"username","") or "").lstrip("@")
    if username.lower()!=CATALOG_CHANNEL.lower():return
    text=(getattr(msg,"text",None) or getattr(msg,"caption",None) or "").strip()
    if not text or not _is_listing(text,extract_lot_id(text)):return
    pub=""
    if getattr(msg,"date",None):
        try:pub=msg.date.astimezone(timezone.utc).isoformat(timespec="seconds")
        except:pub=str(msg.date)
    p={"message_id":str(msg.message_id),"text":text,"published_at":pub,"telegram_url":f"https://t.me/{CATALOG_CHANNEL}/{msg.message_id}"}
    try:
        ws=ensure_lots_sheet(); ex,h=_existing(ws); cur=ex.get(p["message_id"]); rec=await asyncio.to_thread(_record,p,cur[1] if cur else None); row=[rec.get(x,"") for x in h]
        with _lock:
            if cur:ws.update(f"A{cur[0]}:{_col(len(h))}{cur[0]}",[row],value_input_option="USER_ENTERED")
            else:ws.append_row(row,value_input_option="USER_ENTERED")
        _invalidate()
    except Exception:log.exception("channel ingest failed")
